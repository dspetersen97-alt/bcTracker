#!/bin/sh
# First-install bootstrap: write the configuration, then bring the stack up.
#
# Run it from the repository root:
#
#   sh scripts/bootstrap.sh --host counseling.example.org --email counseling@example.org
#   sh scripts/bootstrap.sh --internal-tls --admin you@example.org     # LAN / evaluation
#   sh scripts/bootstrap.sh --help
#
# WHAT THIS SCRIPT WILL AND WILL NOT DECIDE FOR YOU
#
# Every setting falls into one of three kinds, and the difference is the whole
# design of this script:
#
#   generated  The three secrets — DJANGO_SECRET_KEY, BCTRACKER_MASTER_KEY, and the
#              database password. There is no such thing as a sensible default for
#              these, so they are generated fresh. A deployment that ships with a
#              known key is not a deployment with a placeholder in it; it is a
#              deployment whose documents anyone holding this repository can read.
#
#   defaulted  Ports, paths, timeouts, retention windows, the billing currency, the
#              invoice prefix. These have a right answer for a single ministry, and
#              it is already written in .env.example — which this script edits rather
#              than replaces, so every comment explaining a value comes along with
#              it and there is only one list of settings to maintain.
#
#   refused    The public hostname, and the mailbox that sends invitations. A guess
#              here does not fail: it works, quietly, wrongly. An invented hostname
#              produces invitation links pointing somewhere nobody controls, so the
#              hostname must be given. Mail credentials are left EMPTY rather than
#              filled with an example address, because empty means sending raises an
#              error somebody notices, and counseling@example.org means the ministry
#              finds out when a counselee says they never got the link.
#
# Integrations — Google Calendar, Workspace sign-in, Stripe — stay off. Each is
# off-unless-configured by design, and each refuses to start half-configured, so
# there is nothing to defer and nothing to undo. See docs/deployment.md step 12.
#
# The script is safe to re-run: it will not overwrite a configuration file that
# already exists (use --force), and bringing an already-running stack up again is
# how you apply a change to .env.
set -eu

# --- what the caller asked for ---------------------------------------------

host=""
timezone="America/New_York"
email=""
smtp_password="${SMTP_PASSWORD:-}"
data_dir=""
admin_email=""
internal_tls="no"
start_stack="yes"
print_config="no"
force="no"

usage() {
    cat <<'USAGE'
Usage: sh scripts/bootstrap.sh [options]

  --host NAME           Public hostname counselees will use. Required unless
                        --internal-tls is given, in which case it defaults to
                        localhost. Never guessed: see the header of this script.
  --internal-tls        Issue certificates from Caddy's own CA instead of Let's
                        Encrypt, for a LAN-only or evaluation install. Browsers
                        warn until that CA is trusted on each device.
  --timezone TZ         The ministry's timezone, for office hours and stored
                        dates. Default: America/New_York.
  --email ADDRESS       The mailbox that sends invitations and reminders. Its
                        password is read from the SMTP_PASSWORD environment
                        variable, or prompted for. Omit it and the site runs
                        with no mail at all — see --admin.
  --admin ADDRESS       Create the first administrator and print a single-use
                        link for them to set a password. Needs no working mail,
                        which is what makes an install without --email usable.
  --data-dir PATH       Put the documents and backups volumes under PATH — an
                        encrypted dataset — instead of leaving them in Docker's
                        own storage. Writes docker-compose.override.yml.
  --no-start            Write the configuration and stop. Nothing is built.
  --print-config        Print the .env that would be written, and exit. Writes
                        nothing, needs no Docker.
  --force               Overwrite .env and docker-compose.override.yml if they
                        already exist. Refused by default: the master key in an
                        existing .env is the only copy of it.
  -h, --help            This.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --host) host="${2:?--host needs a hostname}"; shift 2 ;;
        --timezone) timezone="${2:?--timezone needs a zone}"; shift 2 ;;
        --email) email="${2:?--email needs an address}"; shift 2 ;;
        --admin) admin_email="${2:?--admin needs an address}"; shift 2 ;;
        --data-dir) data_dir="${2:?--data-dir needs a path}"; shift 2 ;;
        --internal-tls) internal_tls="yes"; shift ;;
        --no-start) start_stack="no"; shift ;;
        --print-config) print_config="yes"; shift ;;
        --force) force="yes"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "bootstrap: unknown option $1" >&2; usage >&2; exit 1 ;;
    esac
done

die() { echo "bootstrap: $*" >&2; exit 1; }
say() { echo "$*"; }
step() { echo; echo "== $*"; }

# --- where we are ----------------------------------------------------------

# The script lives in scripts/, so the repository root is one level up. Derived
# rather than assumed so it can be run from anywhere.
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
[ -f manage.py ] && [ -f docker-compose.yml ] && [ -f .env.example ] ||
    die "$root does not look like a bcTracker checkout"

if [ -z "$host" ]; then
    if [ "$internal_tls" = "yes" ]; then
        host="localhost"
    else
        die "--host is required. A hostname cannot be defaulted: every link in
       every invitation and reminder email is built from it, so a wrong one
       sends counselees somewhere the ministry does not control. Use
       --internal-tls for a LAN-only install, which defaults to localhost."
    fi
fi

# --- generating secrets ----------------------------------------------------

random_hex() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$1"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import secrets,sys; print(secrets.token_hex(int(sys.argv[1])))" "$1"
    else
        die "need either openssl or python3 to generate secrets"
    fi
}

# The master key is read as base64 and must decode to exactly 32 bytes; anything
# else is refused by deploy check documents.E004 rather than half-working.
random_base64_32() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 32
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"
    else
        die "need either openssl or python3 to generate secrets"
    fi
}

# --- rendering .env --------------------------------------------------------

# .env.example is the template, so its comments — which are where each setting is
# actually explained — survive into the deployed configuration, and so that a
# setting added to the example needs no change here to be carried through.
render_env() {
    umask 077
    overrides=$(mktemp)

    scheme_host="https://$host"
    {
        echo "DJANGO_SETTINGS_MODULE=config.settings.prod"
        echo "DJANGO_SECRET_KEY=$(random_hex 48)"
        echo "DJANGO_ALLOWED_HOSTS=$host"
        echo "DJANGO_CSRF_TRUSTED_ORIGINS=$scheme_host"
        # A generated admin path keeps the break-glass Django admin out of the way
        # of bots that only ever try /admin/. The real protection is the
        # superuser-only check; this just keeps the logs readable.
        echo "DJANGO_ADMIN_URL_PATH=manage-$(random_hex 4)"
        echo "POSTGRES_PASSWORD=$(random_hex 24)"
        echo "BCTRACKER_MASTER_KEY=$(random_base64_32)"
        echo "SITE_BASE_URL=$scheme_host"
        echo "SITE_HOSTNAME=$host"
        echo "ORG_TIME_ZONE=$timezone"
        # Empty, not an example address, when no mailbox was given. See the header.
        echo "EMAIL_HOST_USER=$email"
        echo "EMAIL_HOST_PASSWORD=$smtp_password"
        echo "DEFAULT_FROM_EMAIL=$email"
        # The example ships a sample Workspace domain. Left in place it would sit
        # there looking configured; blank, it stops Google sign-in from being
        # switched on without the one setting that decides which accounts may use
        # it (accounts.E002, scheduling.E002).
        echo "GOOGLE_WORKSPACE_DOMAIN="
    } > "$overrides"

    {
        echo "# bcTracker configuration for $host."
        echo "#"
        echo "# Generated by scripts/bootstrap.sh on $(date -u '+%Y-%m-%d %H:%M UTC')."
        echo "# Never commit this file, and keep it readable only by root (0600)."
        echo "#"
        echo "# The three secrets below were generated for this deployment and exist"
        echo "# nowhere else. BCTRACKER_MASTER_KEY decrypts every document, attachment,"
        echo "# and backup; it cannot be rotated and cannot be recovered. Seal a copy"
        echo "# offline, away from the backups, before anybody uploads anything real."
        echo "# See docs/deployment.md step 4."
        echo "#"
        echo "# Everything else is the documented default, carried over from"
        echo "# .env.example along with the comments explaining it."

        # First file: the overrides. Second: the template. Keys are matched exactly
        # and values copied verbatim, so a base64 secret full of + and / needs no
        # escaping.
        awk '
        # A CRLF template would put a carriage return inside every value, and
        # Compose passes that through to the container as data.
        { sub(/\r$/, "") }
        NR == FNR {
            eq = index($0, "=")
            if (eq > 1) { override[substr($0, 1, eq - 1)] = substr($0, eq + 1) }
            next
        }
        # The template opens with a "copy this file and fill it in" preamble, which
        # the header above has just replaced and which would otherwise tell the
        # operator to generate keys that are already here.
        !body {
            if ($0 ~ /^#/) next
            if ($0 == "") { body = 1; print; next }
            body = 1
        }
        {
            eq = index($0, "=")
            if (eq > 1) {
                key = substr($0, 1, eq - 1)
                if (key ~ /^[A-Z][A-Z0-9_]*$/ && key in override) {
                    print key "=" override[key]
                    applied[key] = 1
                    next
                }
            }
            print
        }
        END {
            first = 1
            for (key in override) {
                if (!(key in applied)) {
                    if (first) {
                        print ""
                        print "# --- Added by scripts/bootstrap.sh ---"
                        print "# Not found in .env.example, which probably means one of"
                        print "# the two files has drifted from the other."
                        first = 0
                    }
                    print key "=" override[key]
                }
            }
        }
    ' "$overrides" .env.example
    }

    rm -f "$overrides"
}

if [ "$print_config" = "yes" ]; then
    render_env
    exit 0
fi

# --- preflight -------------------------------------------------------------

step "Checking the host"

if [ "$start_stack" = "yes" ]; then
    command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH"
    docker compose version >/dev/null 2>&1 ||
        die "'docker compose' (v2) is not available. This stack does not use docker-compose v1."
    docker info >/dev/null 2>&1 ||
        die "cannot talk to the Docker daemon. Start it, or run this as a user in the docker group."
    say "docker: ok"
fi

if [ -e .env ] && [ "$force" != "yes" ]; then
    die ".env already exists, so this looks like a re-run.

       Refusing to overwrite it, because the BCTRACKER_MASTER_KEY inside it is
       very likely the only copy, and every document and backup already written
       is unreadable without it. To bring the existing stack up, run
       'make docker-up'. To start over from an empty database, move .env aside
       yourself and pass --force."
fi

if [ -n "$email" ] && [ -z "$smtp_password" ]; then
    if [ -t 0 ]; then
        printf 'App Password for %s (input hidden, enter to skip): ' "$email"
        stty -echo 2>/dev/null || true
        read -r smtp_password || smtp_password=""
        stty echo 2>/dev/null || true
        echo
    fi
    [ -n "$smtp_password" ] ||
        say "note: no mail password given; set EMAIL_HOST_PASSWORD in .env before inviting anyone."
fi

# --- configuration ---------------------------------------------------------

step "Writing .env"

render_env > .env
chmod 600 .env
say "wrote .env (0600) for $host, timezone $timezone"
[ -n "$email" ] || say "mail is NOT configured: EMAIL_HOST_USER and EMAIL_HOST_PASSWORD are empty."

admin_path=$(awk -F= '$1 == "DJANGO_ADMIN_URL_PATH" { print $2 }' .env)

if [ -n "$data_dir" ] || [ "$internal_tls" = "yes" ]; then
    step "Writing docker-compose.override.yml"

    if [ -e docker-compose.override.yml ] && [ "$force" != "yes" ]; then
        die "docker-compose.override.yml already exists; pass --force to replace it."
    fi

    # Compose keys a service's volumes on the mount target, so naming a target
    # again replaces that one mount and leaves the rest of the service's list
    # alone. Only what changes belongs in here.
    {
        echo "# Generated by scripts/bootstrap.sh. This file describes THIS host, so it is"
        echo "# not in git and an upgrade will not touch it."
        echo "services:"

        if [ "$internal_tls" = "yes" ]; then
            # The tracked Caddyfile is left alone — tests read it as the record of
            # what the ministry serves, and an upgrade would collide with an edit —
            # and a copy with `tls internal` added is mounted over it instead.
            awk '{ print } /^\{\$SITE_HOSTNAME\} \{$/ { print "\ttls internal" }' \
                compose/caddy/Caddyfile > compose/caddy/Caddyfile.local
            echo "  caddy:"
            echo "    volumes:"
            echo "      - ./compose/caddy/Caddyfile.local:/etc/caddy/Caddyfile:ro"
        fi

        if [ -n "$data_dir" ]; then
            # Both services get the same documents directory: cron's retention purge
            # deletes the files whose rows it removes, and would find nothing.
            echo "  web:"
            echo "    volumes:"
            echo "      - $data_dir/documents:/var/lib/bctracker/documents"
            echo "  cron:"
            echo "    volumes:"
            echo "      - $data_dir/documents:/var/lib/bctracker/documents"
            echo "      - $data_dir/backups:/var/lib/bctracker/backups"
        fi
    } > docker-compose.override.yml

    say "wrote docker-compose.override.yml"

    if [ -n "$data_dir" ]; then
        # A bind mount arrives with the host directory's ownership, unlike a named
        # volume, which inherits the image's. Without this the web process cannot
        # store an upload and every nightly backup fails — so if it cannot be done
        # here it has to be said out loud rather than left to be discovered.
        #
        # Neither step aborts the run. The configuration is already written and
        # correct; what is missing is one command as root.
        prepared="no"
        if mkdir -p "$data_dir/documents" "$data_dir/backups" 2>/dev/null &&
            chown -R 1000:1000 "$data_dir" 2>/dev/null &&
            chmod 700 "$data_dir/documents" "$data_dir/backups" 2>/dev/null; then
            prepared="yes"
            say "prepared $data_dir for uid 1000 (the bctracker user in the containers)"
        fi

        if [ "$prepared" = "no" ]; then
            say "WARNING: could not prepare $data_dir — run these as root before starting:"
            say "    mkdir -p $data_dir/documents $data_dir/backups"
            say "    chown -R 1000:1000 $data_dir"
            say "    chmod 700 $data_dir/documents $data_dir/backups"
            say "Without them, uploads and the nightly backup fail with a permission error."
        fi
    fi
fi

if [ "$start_stack" != "yes" ]; then
    step "Stopped before starting, as asked"
    say "Review .env, then: make docker-build && make docker-up"
    exit 0
fi

# --- build and start -------------------------------------------------------

# Two failures where the log line names a symptom and not the cause. Both have
# happened; neither is guessable from the message alone.
explain_failure() {
    log=$(docker compose logs --tail 40 web 2>/dev/null || echo "")
    case "$log" in
        *"password authentication failed"*)
            say ""
            say "The database volume already existed. Postgres reads POSTGRES_PASSWORD"
            say "only when it first creates the cluster, so the freshly generated password"
            say "in .env cannot authenticate against a volume initialised with an older"
            say "one. Either put the previous POSTGRES_PASSWORD back into .env, or — if"
            say "that database holds nothing you need — 'docker compose down -v' to"
            say "discard it and run this script again."
            ;;
        *"entrypoint.sh: no such file or directory"*)
            say ""
            say "The entrypoint exists; the interpreter in its shebang does not. That is"
            say "a CRLF line ending, from building on a checkout that has them. Confirm"
            say "with 'head -1 compose/web/entrypoint.sh | od -c' and fix the checkout:"
            say "'git add --renormalize . && git checkout -- .' with .gitattributes in"
            say "place, then rebuild."
            ;;
    esac
}

step "Building the image"
docker compose build

step "Starting the stack"
docker compose up -d

step "Waiting for the application to report healthy"
say "(it applies migrations and runs the deployment checks first)"

container=$(docker compose ps -q web)
[ -n "$container" ] || die "the web service did not start at all; see: docker compose logs web"

waited=0
limit=300
while :; do
    state=$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo "gone")
    case "$state" in
        healthy) say "healthy after ${waited}s"; break ;;
        gone)
            say ""
            docker compose logs --tail 40 web
            explain_failure
            die "the web container exited. The log above is the reason." ;;
    esac
    if [ "$waited" -ge "$limit" ]; then
        say ""
        docker compose logs --tail 40 web
        explain_failure
        die "still $state after ${limit}s. The log above is the reason; the deploy
       check ids in it are explained in docs/deployment.md (Troubleshooting)."
    fi
    [ $((waited % 15)) -eq 0 ] && [ "$waited" -gt 0 ] && say "  still $state (${waited}s)"
    sleep 5
    waited=$((waited + 5))
done

step "The stack"
docker compose ps

# --- the first administrator ------------------------------------------------

if [ -n "$admin_email" ]; then
    step "Creating the first administrator"
    docker compose exec -T web python manage.py invite_staff \
        --email "$admin_email" --role admin
fi

# --- what is left for a person ---------------------------------------------

step "Done — and four things are now yours to do"

cat <<SUMMARY

The site is at https://$host/ and the break-glass Django admin at
https://$host/$admin_path/ (nothing is registered in it; create a superuser with
'docker compose exec web python manage.py createsuperuser' if you ever need it).

1. SEAL THE MASTER KEY, TODAY. It encrypts every document, attachment, and
   backup, there is no rotation and no recovery, and right now it exists in
   exactly one place:

       grep '^BCTRACKER_MASTER_KEY=' .env

   Put a copy somewhere sealed and offline, and NOT where the backups go — the
   two together are the archive in the clear. Write down who holds it.
SUMMARY

if [ -z "$email" ]; then
    cat <<'SUMMARY'

2. CONFIGURE MAIL. EMAIL_HOST_USER and EMAIL_HOST_PASSWORD are empty, so no
   invitation or reminder can be sent, and creating a counselee in the interface
   will fail at the point it tries. Set them in .env (a Workspace App Password,
   not the account password), 'docker compose up -d', then test:

       docker compose exec web python manage.py shell -c \
         "from django.core.mail import send_mail; \
          send_mail('bcTracker test', 'It works.', None, ['you@example.org'])"

   Until then, 'manage.py invite_staff' prints links instead of mailing them.
SUMMARY
else
    cat <<SUMMARY

2. TEST MAIL before it has to carry an invitation. Workspace reports nothing
   back to this application, so a rejected message is invisible:

       docker compose exec web python manage.py shell -c \\
         "from django.core.mail import send_mail; \\
          send_mail('bcTracker test', 'It works.', None, ['$email'])"
SUMMARY
fi

cat <<'SUMMARY'

3. GET BACKUPS OFF THIS HOST, then rehearse a restore while the database is
   still empty of anything real. The nightly job writes an encrypted dump to a
   volume on this same pool, which survives a mistake and not a fire. See
   docs/restore-drill.md.

4. CREATE THE PEOPLE. Staff accounts — counselors, administrators,
   financial_admin — have no signup page on purpose:

       docker compose exec web python manage.py invite_staff \
           --email pastor@example.org --role counselor

   Counselees are created inside the application, by an administrator, as part
   of opening a case.

Optional integrations (Google Calendar, Workspace sign-in, Stripe) are off and
stay off until configured; docs/deployment.md step 12 turns them on. Step 13
raises HSTS once this deployment has been known good for a few days.
SUMMARY
