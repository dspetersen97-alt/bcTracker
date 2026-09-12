"""
The glue between the image, compose, the cron sidecar, and the settings module.

None of this is Python and none of it runs during the rest of the suite, which is
exactly why it is worth asserting. A mistake in here does not raise: it produces a
container that never reports healthy, or a nightly backup written to a directory
nobody replicates, or a scheduled job that reads a default and reports success.
Each test below is a coupling that has already been got wrong once.

Config files are parsed rather than imported, for the same reason
tests/test_security_headers.py parses the Caddyfile: a Dockerfile is not Python,
and the value that matters is the one the deployed stack actually uses.
"""

import base64
import importlib
import os
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from django.conf import settings

BASE = Path(settings.BASE_DIR)
DOCKERFILE = BASE / "Dockerfile"
COMPOSE_FILE = BASE / "docker-compose.yml"
RENDER_ENV = BASE / "compose" / "cron" / "render-env.sh"
BOOTSTRAP = BASE / "scripts" / "bootstrap.sh"
CADDYFILE = BASE / "compose" / "caddy" / "Caddyfile"
WEB_ENTRYPOINT = BASE / "compose" / "web" / "entrypoint.sh"
ENV_EXAMPLE = BASE / ".env.example"
MAKEFILE = BASE / "Makefile"

SHELL = shutil.which("sh") or shutil.which("bash")
#: The script generates secrets with whichever of these it finds.
HAS_SECRET_TOOL = bool(shutil.which("openssl") or shutil.which("python3"))


def health_probe():
    """The (host, path) the image's own HEALTHCHECK asks for."""
    match = re.search(
        r"urlopen\(\s*'http://(?P<host>[^:/']+):\d+(?P<path>/[^']*)'", DOCKERFILE.read_text()
    )
    assert match, "no HEALTHCHECK urlopen() found in the Dockerfile"
    return match["host"], match["path"]


def compose_services():
    """Service name -> the raw YAML block for that service."""
    body = COMPOSE_FILE.read_text().split("\nservices:\n", 1)[1].split("\nvolumes:\n", 1)[0]
    parts = re.split(r"^  ([\w-]+):$", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def services_mounting(volume):
    return {
        name
        for name, block in compose_services().items()
        if re.search(rf"^\s*- {volume}:", block, re.MULTILINE)
    }


def prod_settings(monkeypatch, *, hostname="counseling.example.org"):
    """Import config.settings.prod with a plausible production environment.

    Reloaded rather than merely imported because another test may have imported it
    first, and the values under test are computed at import time.
    """
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", hostname)
    monkeypatch.setenv("DJANGO_CSRF_TRUSTED_ORIGINS", f"https://{hostname}")
    return importlib.reload(importlib.import_module("config.settings.prod"))


class TestTheContainerCanPassItsOwnHealthcheck:
    """Why this is not hypothetical.

    `depends_on: web: condition: service_healthy` means caddy does not start until
    the web container reports healthy, and the probe is an HTTP request to
    127.0.0.1. Django validates the Host header before anything else, so with only
    the public hostname in ALLOWED_HOSTS the probe gets a 400, the container is
    never healthy, and the site never comes up on a fresh deployment — with nothing
    in the log but a rejected host.
    """

    def test_the_probe_host_is_allowed(self, monkeypatch):
        host, _ = health_probe()

        assert host in prod_settings(monkeypatch).ALLOWED_HOSTS

    def test_the_public_hostname_is_still_the_operators_to_set(self, monkeypatch):
        """The loopback address is added to what .env says, not substituted for it."""
        assert "counseling.example.org" in prod_settings(monkeypatch).ALLOWED_HOSTS

    def test_the_probe_path_is_exempt_from_the_https_redirect(self, monkeypatch):
        """The probe speaks plain HTTP with no proxy in front to set
        X-Forwarded-Proto, so without the exemption SECURE_SSL_REDIRECT answers it
        with a 301 to a URL nothing is listening on."""
        _, path = health_probe()
        exemptions = prod_settings(monkeypatch).SECURE_REDIRECT_EXEMPT

        assert any(re.compile(pattern).search(path.lstrip("/")) for pattern in exemptions)

    @pytest.mark.django_db
    def test_the_probe_really_gets_a_200(self, client, monkeypatch, settings):
        """The two assertions above, joined up and actually requested."""
        host, path = health_probe()
        settings.ALLOWED_HOSTS = prod_settings(monkeypatch).ALLOWED_HOSTS

        response = client.get(path, headers={"host": host})

        assert response.status_code == 200


def deploy_gate():
    """The check command the web entrypoint runs, read out of the entrypoint itself.

    Parsed rather than written out here so the test cannot come to be about a
    command nobody runs. If the flags are ever softened, the assertions below start
    describing the softer gate — which is why one of them is about the flags.
    """
    match = re.search(r"^python (manage\.py check [^\n]*)$", WEB_ENTRYPOINT.read_text(), re.M)
    assert match, "compose/web/entrypoint.sh no longer runs a deployment check before serving"
    return match[1].split()


def production_environment():
    """A plausible deployed environment, as .github/workflows/ci.yml sets one up.

    Every value here is a secret or a hostname, and none of them is what the gate is
    about — they exist so that the checks which are about them pass, leaving the
    security warnings as the only thing that can fail. The two postgres binaries are
    pointed at this interpreter because ``backups`` only asks whether the path names
    an executable, and a developer's laptop is not required to have postgres
    installed for a Django settings module to be worth checking.
    """
    return os.environ | {
        "DJANGO_SETTINGS_MODULE": "config.settings.prod",
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(48),
        "DJANGO_ALLOWED_HOSTS": "counseling.example.org",
        "DJANGO_CSRF_TRUSTED_ORIGINS": "https://counseling.example.org",
        "SECURE_HSTS_SECONDS": "31536000",
        "BCTRACKER_MASTER_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "PG_DUMP_PATH": sys.executable,
        "PG_RESTORE_PATH": sys.executable,
    }


class TestTheGateTheEntrypointRunsBeforeServing:
    """The one that was missing, and cost a deployment.

    ``compose/web/entrypoint.sh`` runs ``check --deploy --fail-level WARNING`` after
    migrating and before gunicorn, so a single deploy *warning* is not advice — it is
    a container that boots, applies migrations, prints one WARNING line and exits 1,
    forever. That is what happened when X_FRAME_OPTIONS became SAMEORIGIN so a PDF
    could be shown in a frame of our own preview route: the whole suite was green,
    because pytest runs under the test settings and never runs this gate at all.

    CI runs it, which would have caught it a few minutes later. This runs it here, in
    the suite a change is written against, under the settings module the container
    uses. The subprocess is the point: these checks read settings at import time, and
    importing the production module into a running test process would leave the rest
    of the suite configured for production.
    """

    def test_the_flags_are_still_the_strict_ones(self):
        """The assertion below is only worth anything while the gate is this strict —
        drop ``--fail-level WARNING`` and it passes on a container that would refuse
        to serve for a different reason."""
        command = deploy_gate()

        assert "--deploy" in command
        assert ["--fail-level", "WARNING"] == command[-2:]

    def test_the_production_settings_pass_it(self):
        completed = subprocess.run(  # noqa: S603 — manage.py from this repo, fixed argv
            [sys.executable, *deploy_gate()],
            cwd=BASE,
            env=production_environment(),
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 0, (
            "the web container would not start:\n"
            f"{completed.stdout}\n{completed.stderr}\n"
            "Either fix the setting the check names, or — if the check is wrong about "
            "this deployment — add its id to SILENCED_SYSTEM_CHECKS in "
            "config/settings/prod.py with the reasoning written out."
        )

    def test_nothing_silenced_is_an_error(self, monkeypatch):
        """Silencing is for a check that has weighed a trade-off and come out on the
        other side, which is a judgment about a Warning. An ``E`` is the check saying
        the deployment is broken — silencing one of those does not make it work, it
        only moves the discovery of it to a counselor."""
        for identifier in prod_settings(monkeypatch).SILENCED_SYSTEM_CHECKS:
            assert not re.search(r"\.E\d+$", identifier), (
                f"{identifier} is an error, and an error is not a judgment call"
            )


class TestTheProxyIsGivenEveryVariableItReads:
    """A ``{$VAR}`` nobody passes is not an error in a Caddyfile — it is a blank line.

    Caddy substitutes an unset variable with nothing and then parses what is left, so
    a placeholder Compose does not supply silently deletes the directive it was
    standing in. That is survivable for a header and not for these: an empty
    ``CADDY_TLS`` is a LAN install asking Let's Encrypt for a certificate it can never
    be given, and an empty ``SITE_HOSTNAME`` is a site block with no address.
    """

    def test_every_placeholder_is_passed_to_the_container(self):
        caddy = compose_services()["caddy"]

        for name in sorted(set(re.findall(r"\{\$([A-Z_][A-Z0-9_]*)", CADDYFILE.read_text()))):
            assert re.search(rf"(?m)^\s+{name}:", caddy), (
                f"the Caddyfile reads {name} and the caddy service never passes it, "
                "so it resolves to nothing"
            )

    def test_how_tls_is_issued_is_one_of_them(self):
        """Named rather than left to the loop above, because this is the placeholder
        that replaced a generated copy of this file — see tests/test_security_headers.py
        for what the copy cost — and a loop over whatever happens to be in the file
        would pass just as happily once somebody deleted it."""
        assert re.search(r"(?m)^\t\{\$CADDY_TLS\}$", CADDYFILE.read_text()), (
            "compose/caddy/Caddyfile no longer lets a host choose how TLS is issued"
        )


@pytest.mark.skipif(SHELL is None, reason="needs a POSIX shell to run the cron env script")
class TestTheCronSidecarInheritsTheConfiguration:
    """cron gives its jobs a near-empty environment.

    So compose/cron/render-env.sh decides what the scheduled backup, reminder, sync,
    and reconciliation jobs are configured with. It used to name the variables it
    knew about, and the ones added later were missed — which is a silent failure,
    because a job reading BACKUP_ROOT's default writes a real encrypted backup to a
    directory that disappears with the container.
    """

    def render(self, environment):
        result = subprocess.run(  # noqa: S603 — a fixed script from this repo
            [SHELL, RENDER_ENV.as_posix()],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return dict(line.split("=", 1) for line in result.stdout.splitlines())

    def documented_settings(self):
        return re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), re.MULTILINE)

    def test_every_setting_in_env_example_reaches_the_jobs(self):
        """The regression test for the whole class of bug: a setting somebody adds
        to .env tomorrow must arrive without anyone remembering this script."""
        names = self.documented_settings()
        assert "BACKUP_ROOT" in names, "sanity check on parsing .env.example"

        rendered = self.render({name: f"value-of-{name}" for name in names})

        assert set(names) <= set(rendered)
        assert rendered["BACKUP_ROOT"] == "value-of-BACKUP_ROOT"

    def test_the_settings_the_omission_actually_broke(self):
        """Named individually because each was a real, silent misbehaviour: backups
        written off-volume, emails linking to https://localhost, and a Stripe
        reconciler that reported itself unconfigured every night."""
        rendered = self.render(
            {
                "BACKUP_ROOT": "/var/lib/bctracker/backups",
                "SITE_BASE_URL": "https://counseling.example.org",
                "STRIPE_ENABLED": "true",
                "STRIPE_SECRET_KEY": "sk_live_not_real",
                "GOOGLE_CALENDAR_ENABLED": "true",
            }
        )

        assert rendered["BACKUP_ROOT"] == "/var/lib/bctracker/backups"
        assert rendered["SITE_BASE_URL"] == "https://counseling.example.org"
        assert rendered["STRIPE_SECRET_KEY"] == "sk_live_not_real"
        assert rendered["GOOGLE_CALENDAR_ENABLED"] == "true"

    def test_what_describes_the_container_is_left_out(self):
        """PATH and SHELL are written by the entrypoint with values suited to cron,
        so passing the image's through would override them."""
        rendered = self.render(
            {
                # A usable PATH, because the script has to find `env` itself. What
                # is asserted is that it does not pass its own on.
                "PATH": "/usr/bin:/bin",
                "SHELL": "/bin/false",
                "HOME": "/root",
                "HOSTNAME": "abc123",
                "PYTHONUNBUFFERED": "1",
                "LANG": "C.UTF-8",
                "DJANGO_SETTINGS_MODULE": "config.settings.prod",
            }
        )

        # Named exclusions rather than exact equality: a Windows shell injects
        # SYSTEMROOT and WINDIR into any child environment, which says nothing about
        # this script and does not exist in the container.
        assert "DJANGO_SETTINGS_MODULE" in rendered
        assert not {"PATH", "SHELL", "HOME", "HOSTNAME", "PYTHONUNBUFFERED", "LANG"} & set(rendered)

    def test_a_value_containing_a_newline_cannot_break_the_file(self):
        """A continuation line in a crontab is a parse error that stops every job in
        the file, so one unusable value must not take the rest down with it. Such a
        value is truncated at the newline; no setting this application reads can
        contain one, and a mangled timezone is a better outcome than no backups."""
        rendered = self.render({"BROKEN": "first\nsecond", "ORG_TIME_ZONE": "America/New_York"})

        assert rendered["ORG_TIME_ZONE"] == "America/New_York"
        assert rendered["BROKEN"] == "first"
        assert not any(line == "second" for line in rendered)

    def test_the_entrypoint_is_what_calls_it(self):
        entrypoint = (BASE / "compose" / "cron" / "entrypoint.sh").read_text()

        assert "render-env.sh" in entrypoint
        assert "chmod 0600 /etc/environment.cron" in entrypoint


@pytest.mark.skipif(SHELL is None, reason="needs a POSIX shell to run the bootstrap script")
@pytest.mark.skipif(not HAS_SECRET_TOOL, reason="needs openssl or python3 to generate secrets")
class TestTheBootstrapScript:
    """scripts/bootstrap.sh writes the configuration a first install starts from.

    Which makes it the one place where "a sensible default" and "a catastrophe"
    are the same shape. A defaulted master key means every deployment sharing one,
    a defaulted database password means a known one, and a defaulted hostname
    means invitation links pointing at somebody else's domain — none of which
    fails at startup. So what is asserted here is mostly what the script refuses
    to decide, and that it does not quietly ship the example file's placeholders.

    ``--print-config`` is the seam: it renders exactly what would be written to
    .env, without Docker and without touching the filesystem.
    """

    def render(self, *args):
        result = subprocess.run(  # noqa: S603 — a fixed script from this repo
            [SHELL, BOOTSTRAP.as_posix(), *args, "--print-config"],
            capture_output=True,
            text=True,
            check=True,
        )
        return {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in result.stdout.splitlines()
            if line and not line.startswith("#") and "=" in line
        }

    def checkout(self, tmp_path):
        """A throwaway checkout, so the write path can be run for real.

        Only the files the script reads. It derives the repository root from its
        own location, so a copy under tmp_path is a complete world to it.
        """
        root = tmp_path / "checkout"
        (root / "scripts").mkdir(parents=True)
        (root / "compose" / "caddy").mkdir(parents=True)
        for name in ("manage.py", "docker-compose.yml", ".env.example"):
            shutil.copy(BASE / name, root / name)
        shutil.copy(BOOTSTRAP, root / "scripts" / "bootstrap.sh")
        shutil.copy(CADDYFILE, root / "compose" / "caddy" / "Caddyfile")
        return root

    def run_in(self, root, *args, check=True):
        return subprocess.run(  # noqa: S603 — a fixed script from this repo
            [SHELL, (root / "scripts" / "bootstrap.sh").as_posix(), *args],
            capture_output=True,
            text=True,
            check=check,
        )

    # --- what it generates ------------------------------------------------

    def test_every_documented_setting_is_carried_over(self):
        """The example file is the template, not a second list to keep in step."""
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text(), re.MULTILINE))

        assert documented <= set(self.render("--host", "counseling.example.org"))

    def test_the_secrets_are_generated_rather_than_defaulted(self):
        first = self.render("--host", "counseling.example.org")
        second = self.render("--host", "counseling.example.org")

        for name in ("DJANGO_SECRET_KEY", "BCTRACKER_MASTER_KEY", "POSTGRES_PASSWORD"):
            assert len(first[name]) >= 32, name
            # Two runs differing is the whole property: a value baked into the
            # script would be a shared secret across every install of this
            # application, which is not a secret.
            assert first[name] != second[name], name

    def test_the_master_key_is_one_the_application_will_accept(self):
        """32 bytes of base64, or documents.E004 stops the container — and finding
        that out from a failed deploy check is a worse way to learn it."""
        key = self.render("--host", "counseling.example.org")["BCTRACKER_MASTER_KEY"]

        assert len(base64.b64decode(key, validate=True)) == 32

    def test_it_configures_the_production_settings_module(self):
        """The example ships config.settings.dev, which has DEBUG on, no HSTS, no
        secure cookies, and prints email to the console instead of sending it."""
        assert self.render("--host", "x.example.org")["DJANGO_SETTINGS_MODULE"].endswith(".prod")

    def test_the_hostname_reaches_every_setting_derived_from_it(self):
        """Four settings, one answer. Getting one of them wrong produces a site
        that serves, and rejects its own forms, or mails links to the wrong host."""
        config = self.render("--host", "counseling.example.org")

        assert config["SITE_HOSTNAME"] == "counseling.example.org"
        assert config["DJANGO_ALLOWED_HOSTS"] == "counseling.example.org"
        assert config["SITE_BASE_URL"] == "https://counseling.example.org"
        assert config["DJANGO_CSRF_TRUSTED_ORIGINS"] == "https://counseling.example.org"

    def test_a_hostname_is_never_guessed(self):
        result = subprocess.run(  # noqa: S603 — a fixed script from this repo
            [SHELL, BOOTSTRAP.as_posix(), "--print-config"],
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode != 0
        assert "--host" in result.stderr
        assert not result.stdout

    def test_a_lan_install_may_default_to_localhost(self):
        """The one case where a hostname can be inferred, because the operator has
        said the site is not on the internet."""
        config = self.render("--internal-tls")

        assert config["SITE_HOSTNAME"] == "localhost"

    # --- what it refuses to pretend is configured -------------------------

    def test_mail_is_left_empty_rather_than_plausible(self):
        """The example's sending address is counseling@example.org. Carried over,
        it would look configured and silently fail to deliver every invitation;
        empty, the first send raises."""
        config = self.render("--host", "counseling.example.org")

        assert config["EMAIL_HOST_USER"] == ""
        assert config["EMAIL_HOST_PASSWORD"] == ""
        assert config["DEFAULT_FROM_EMAIL"] == ""

    def test_no_value_anywhere_is_left_pointing_at_the_example_domain(self):
        """Values only — the comments explaining each setting still use it."""
        config = self.render("--host", "counseling.example.org")

        leftovers = {name: value for name, value in config.items() if "example.org" in value}
        assert leftovers == {
            "DJANGO_ALLOWED_HOSTS": "counseling.example.org",
            "DJANGO_CSRF_TRUSTED_ORIGINS": "https://counseling.example.org",
            "SITE_BASE_URL": "https://counseling.example.org",
            "SITE_HOSTNAME": "counseling.example.org",
        }

    def test_the_optional_integrations_stay_off(self):
        """Each is off-unless-configured and refuses to start half-configured, so
        the bootstrap leaves them alone rather than half-filling them in."""
        config = self.render("--host", "counseling.example.org")

        assert config["GOOGLE_CALENDAR_ENABLED"] == ""
        assert config["GOOGLE_SSO_ENABLED"] == ""
        assert config["STRIPE_ENABLED"] == ""
        # Not cosmetic: with a domain left in place, switching sign-in on would be
        # one variable away from accepting accounts in somebody else's Workspace.
        assert config["GOOGLE_WORKSPACE_DOMAIN"] == ""

    def test_the_admin_path_is_moved_off_the_default(self):
        config = self.render("--host", "counseling.example.org")

        assert config["DJANGO_ADMIN_URL_PATH"] not in ("admin", "")

    # --- the write path ---------------------------------------------------

    def test_it_writes_a_configuration_and_stops_when_asked(self, tmp_path):
        root = self.checkout(tmp_path)

        self.run_in(root, "--host", "bc.example.org", "--no-start")

        written = (root / ".env").read_text()
        assert "BCTRACKER_MASTER_KEY=" in written
        assert "SITE_HOSTNAME=bc.example.org" in written
        # The comments explaining every setting came with it.
        assert "it must be an app password rather than" in written

    def test_an_existing_configuration_is_never_overwritten(self, tmp_path):
        """The refusal that matters most in this script. The master key in an
        existing .env is usually the only copy, and every document and backup
        already written is unreadable without it."""
        root = self.checkout(tmp_path)
        (root / ".env").write_text("BCTRACKER_MASTER_KEY=the-only-copy\n")

        result = self.run_in(root, "--host", "bc.example.org", "--no-start", check=False)

        assert result.returncode != 0
        assert (root / ".env").read_text() == "BCTRACKER_MASTER_KEY=the-only-copy\n"

    def test_force_is_the_way_past_that(self, tmp_path):
        root = self.checkout(tmp_path)
        (root / ".env").write_text("BCTRACKER_MASTER_KEY=the-only-copy\n")

        self.run_in(root, "--host", "bc.example.org", "--no-start", "--force")

        assert "the-only-copy" not in (root / ".env").read_text()

    def test_a_lan_install_says_so_in_one_variable(self, tmp_path):
        """How TLS is issued is the whole of a LAN install's difference to the proxy,
        so it is a value in .env.

        It used to be a generated copy of the Caddyfile with ``tls internal`` added,
        mounted over the tracked one. That copy was written once and never again, so
        it went on serving the headers of the day it was made — which is how a
        deployment came to refuse to frame its own document preview long after the
        policy said it could. The copy must not come back.
        """
        root = self.checkout(tmp_path)

        self.run_in(root, "--internal-tls", "--no-start")

        assert "CADDY_TLS=tls internal" in (root / ".env").read_text()
        assert not (root / "compose" / "caddy" / "Caddyfile.local").exists()

    def test_and_needs_no_file_describing_this_host(self, tmp_path):
        """The other half: with the mount gone, a LAN install has nothing to
        override, and an override is a file an upgrade cannot reason about."""
        root = self.checkout(tmp_path)

        self.run_in(root, "--internal-tls", "--no-start")

        assert not (root / "docker-compose.override.yml").exists()

    def test_an_internet_facing_install_leaves_it_empty(self, tmp_path):
        """Written out as an empty value rather than left absent, so that the
        setting an operator has to change for a LAN install is in front of them with
        its comment, and so Caddy asks Let's Encrypt for a real certificate."""
        root = self.checkout(tmp_path)

        self.run_in(root, "--host", "bc.example.org", "--no-start")

        written = (root / ".env").read_text()

        assert re.search(r"(?m)^CADDY_TLS=$", written)
        # Only the line, not the file: the comment carried over from .env.example is
        # where `tls internal` is explained, and it belongs in front of the operator.
        assert not re.search(r"(?m)^CADDY_TLS=tls internal$", written)

    def test_the_two_services_that_hold_documents_are_given_the_same_directory(self, tmp_path):
        """The nightly purge deletes the files whose rows it removes. Pointed at a
        different directory from the one uploads land in, it would find none of
        them and report success."""
        root = self.checkout(tmp_path)

        self.run_in(root, "--host", "bc.example.org", "--data-dir", "/srv/enc", "--no-start")

        override = (root / "docker-compose.override.yml").read_text()
        assert override.count("/srv/enc/documents:/var/lib/bctracker/documents") == 2
        assert "/srv/enc/backups:/var/lib/bctracker/backups" in override

    def test_no_override_is_written_when_nothing_needs_overriding(self, tmp_path):
        """A file describing this host is a file an upgrade cannot reason about, so
        the default install does not leave one behind."""
        root = self.checkout(tmp_path)

        self.run_in(root, "--host", "bc.example.org", "--no-start")

        assert not (root / "docker-compose.override.yml").exists()

    def test_the_command_it_offers_to_run_exists(self):
        """--admin execs a management command. A rename here is a broken install
        that only shows up at the very end of a real deployment."""
        from django.core.management import get_commands

        invoked = re.findall(r"manage\.py (\w+)", BOOTSTRAP.read_text())

        assert invoked, "the script no longer runs any management command"
        assert set(invoked) <= set(get_commands())

    def test_it_checks_for_a_database_volume_from_an_earlier_install(self):
        """The compose file pins `name:`, so a second checkout in a second directory
        is the same Compose project and inherits the same pgdata volume — whose
        cluster still has the earlier install's POSTGRES_PASSWORD. Since this script
        always generates a new one, the stack would start unable to reach its own
        database, and say so only as a restart loop. It refuses up front instead.
        """
        script = BOOTSTRAP.read_text()
        compose = COMPOSE_FILE.read_text()

        assert re.search(r"^name:\s*\S+", compose, re.MULTILINE), (
            "the compose file no longer pins a project name, so the script's "
            "derivation of it has nothing to read"
        )
        assert re.search(r"^  pgdata:", compose, re.MULTILINE), "no pgdata volume declared"
        assert "COMPOSE_PROJECT_NAME" in script, "the script ignores Compose's own override"
        assert "${project}_pgdata" in script, "the script no longer looks for the volume"


class TestBackupsAreWrittenWhereTheyArePersisted:
    """`backup_database` is only useful in a container that has the volume."""

    def test_the_make_target_runs_in_a_service_that_mounts_the_volume(self):
        match = re.search(r"^backup:\n\t(?P<command>.+)$", MAKEFILE.read_text(), re.MULTILINE)
        assert match, "no backup target found in the Makefile"
        service = re.search(r"docker compose exec (\S+)", match["command"])
        assert service, "the backup target no longer execs into a compose service"

        assert service[1] in services_mounting("backups")

    def test_the_web_service_cannot_write_one_at_all(self):
        """Deliberate: the process serving counselees has no reason to be able to
        produce an encrypted copy of the entire database."""
        assert "web" not in services_mounting("backups")

    def test_every_service_with_the_volume_is_told_where_it_is(self):
        for name in services_mounting("backups"):
            assert "BACKUP_ROOT:" in compose_services()[name]


class TestTheFilesAUnixToolParses:
    """Line endings, which cost an afternoon once and would cost it again.

    `docker compose build` copies the working tree from disk; git is not involved.
    A Windows clone with the default core.autocrlf=true has CRLF in the working
    tree even though every committed blob is LF, so the image gets `#!/bin/sh\r`
    and the container dies on every start with

        exec /app/compose/web/entrypoint.sh: no such file or directory

    naming the script, which exists, rather than the interpreter, which does not.
    A CRLF crontab is worse: cron runs, and appends \r to the command it runs.
    """

    #: Read by /bin/sh, cron, Caddy, or Compose's env-file parser, all of which
    #: treat a trailing carriage return as data.
    LF_ONLY = (
        "compose/web/entrypoint.sh",
        "compose/cron/entrypoint.sh",
        "compose/cron/render-env.sh",
        "compose/cron/bctracker.cron",
        "compose/caddy/Caddyfile",
        "scripts/bootstrap.sh",
        ".env.example",
    )

    @pytest.mark.parametrize("name", LF_ONLY)
    def test_the_working_tree_copy_has_no_carriage_returns(self, name):
        assert b"\r" not in (BASE / name).read_bytes()

    @pytest.mark.parametrize("name", LF_ONLY)
    def test_gitattributes_pins_it_that_way_for_every_checkout(self, name):
        """The state above is the symptom; this is the mechanism that holds it."""
        patterns = [
            line.split()[0]
            for line in (BASE / ".gitattributes").read_text().splitlines()
            if "eol=lf" in line and not line.startswith("#")
        ]
        target = PurePosixPath(name)
        assert any(target.match(pattern) for pattern in patterns), (
            f"{name} is not covered by an eol=lf rule in .gitattributes"
        )

    def test_the_image_strips_them_anyway(self):
        """.gitattributes governs checkouts; a build context can arrive any way at
        all — a zip download, a copy off a Windows share, a tarball made before the
        attributes existed. The Dockerfile is the last place to catch it, so every
        script it makes executable is also stripped."""
        run = re.search(r"^RUN chmod \+x.*?(?=\n\n)", DOCKERFILE.read_text(), re.S | re.M)
        assert run, "no chmod +x block found in the Dockerfile"
        chmod, _, sed = run[0].partition("sed -i")
        assert sed, "the chmod block no longer strips carriage returns"

        executable = set(re.findall(r"[\w./-]+\.sh", chmod))
        stripped = set(re.findall(r"[\w./-]+\.(?:sh|cron)", sed))
        assert executable <= stripped
