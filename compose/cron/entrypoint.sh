#!/bin/sh
# Entrypoint for the cron sidecar.
#
# Runs the cron daemon in the foreground so Docker owns the process lifecycle
# and job output reaches `docker compose logs`.
set -eu

CRONTAB=/etc/cron.d/bctracker

if [ ! -f "$CRONTAB" ]; then
    echo "cron: $CRONTAB is not mounted; nothing to schedule" >&2
    exit 1
fi

# cron silently ignores files in /etc/cron.d that are group- or world-writable,
# which is a confusing way to have no scheduled jobs at all. The file is mounted
# read-only, so fix the mode on a copy rather than in place.
cp "$CRONTAB" /etc/cron.d/bctracker-active
chmod 0644 /etc/cron.d/bctracker-active
chown root:root /etc/cron.d/bctracker-active
rm -f "$CRONTAB" 2>/dev/null || true

# The scheduled jobs need the same environment the web container gets. cron
# starts jobs with a near-empty environment, so export the relevant variables
# into a file cron reads. Secrets are written to a root-owned file inside the
# container only.
{
    echo "SHELL=/bin/sh"
    echo "PATH=/usr/local/bin:/usr/bin:/bin"
    for var in DJANGO_SETTINGS_MODULE DJANGO_SECRET_KEY DJANGO_ALLOWED_HOSTS \
               DJANGO_CSRF_TRUSTED_ORIGINS POSTGRES_DB POSTGRES_USER \
               POSTGRES_PASSWORD POSTGRES_HOST POSTGRES_PORT \
               BCTRACKER_MASTER_KEY DOCUMENT_STORE_ROOT ORG_TIME_ZONE \
               EMAIL_HOST EMAIL_PORT EMAIL_HOST_USER EMAIL_HOST_PASSWORD \
               DEFAULT_FROM_EMAIL LOG_LEVEL; do
        eval "value=\${$var:-}"
        [ -n "$value" ] && echo "$var=$value"
    done
} > /etc/environment.cron
chmod 0600 /etc/environment.cron

# Prepend the environment to the active crontab so every job inherits it.
cat /etc/environment.cron /etc/cron.d/bctracker-active > /etc/cron.d/bctracker-final
chmod 0644 /etc/cron.d/bctracker-final
rm -f /etc/cron.d/bctracker-active

echo "cron: scheduling from /etc/cron.d/bctracker-final"
exec cron -f -L 2
