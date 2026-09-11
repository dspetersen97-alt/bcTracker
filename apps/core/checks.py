"""
Deploy-time checks for backups and for outgoing mail.

These run under ``manage.py check --deploy``, which the container entrypoint calls
before gunicorn starts, so a container configured in a way that makes the nightly
backup impossible refuses to boot instead of appearing to work for a month.
Registered in apps.py rather than written in the README, because a deployment note
is not a control.
"""

import shutil
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Info, register


@register("backups", deploy=True)
def check_backup_root_is_not_served(app_configs, **kwargs):
    """The backup directory must not be inside anything the web server publishes.

    A dump placed under STATIC_ROOT would be served by WhiteNoise at a guessable
    URL with no permission check. It is encrypted, but so is a stolen safe: there
    is no reason to hand out the ciphertext of the entire database along with the
    manifest that says which key opens it.
    """
    root = Path(settings.BACKUP_ROOT).resolve()
    published = [
        Path(settings.STATIC_ROOT).resolve(),
        *(Path(directory).resolve() for directory in settings.STATICFILES_DIRS),
    ]
    for directory in published:
        if root == directory or directory in root.parents:
            return [
                Error(
                    f"BACKUP_ROOT ({root}) is inside a directory the web server "
                    f"publishes ({directory}).",
                    hint="Move BACKUP_ROOT outside STATIC_ROOT and STATICFILES_DIRS.",
                    id="backups.E001",
                )
            ]
    return []


@register("backups", deploy=True)
def check_backup_tools_are_available(app_configs, **kwargs):
    """``pg_dump`` and ``pg_restore`` must both be present.

    Both, not just the first: a container that can write backups but cannot read
    them back would pass every nightly run and fail the one that mattered, because
    verification — and any restore — needs ``pg_restore``.
    """
    problems = []
    binaries = (("PG_DUMP_PATH", "backups.E002"), ("PG_RESTORE_PATH", "backups.E003"))
    for setting, identifier in binaries:
        binary = getattr(settings, setting)
        if shutil.which(binary) is None:
            problems.append(
                Error(
                    f"{setting} is {binary!r}, which is not an executable on this host, "
                    "so the nightly backup cannot run.",
                    hint=(
                        "Install the postgresql-client package in the image (the "
                        "Dockerfile does), or point the setting at the binary."
                    ),
                    id=identifier,
                )
            )
    return problems


@register("mail", deploy=True)
def check_mail_is_configured(app_configs, **kwargs):
    """Say so at startup when this deployment cannot send email.

    Deliberately ``Info`` and not ``Warning``. The entrypoint runs
    ``check --deploy --fail-level WARNING``, and an application that cannot send
    mail is degraded rather than broken — an administrator can still create
    accounts and hand the invitation link over another way, which is exactly the
    state a brand new install is in before anybody has typed an app password. A
    warning here would make a fresh install refuse to boot at the one moment its
    operator has no way to fix it.

    The value of it being here at all is that "an install passed every check and
    then 500ed on the first counselee" is what happened before mail settings
    moved into the database. Now the state is named in the startup log, and the
    web UI has a page that fixes it.
    """
    from apps.core import mail

    reason = mail.unconfigured_reason()
    if not reason:
        return []
    return [
        Info(
            reason,
            hint=(
                "Sign in as an administrator and open Email settings, or set "
                "EMAIL_HOST_USER and EMAIL_HOST_PASSWORD in .env. Until then, "
                "invitations must be copied from the screen by hand."
            ),
            id="mail.I001",
        )
    ]
