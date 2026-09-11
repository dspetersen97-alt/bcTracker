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

# The scheduled jobs need the same configuration the web container gets, and cron
# hands them almost none of it. render-env.sh turns this container's environment
# into crontab assignment lines, and explains why it passes everything through
# rather than naming the variables it knows about. Secrets land in a root-owned
# file inside the container only.
{
    echo "SHELL=/bin/sh"
    echo "PATH=/usr/local/bin:/usr/bin:/bin"
    "$(dirname "$0")/render-env.sh"
} > /etc/environment.cron
chmod 0600 /etc/environment.cron

# Prepend the environment to the active crontab so every job inherits it.
cat /etc/environment.cron /etc/cron.d/bctracker-active > /etc/cron.d/bctracker-final
chmod 0644 /etc/cron.d/bctracker-final
rm -f /etc/cron.d/bctracker-active

echo "cron: scheduling from /etc/cron.d/bctracker-final"
exec cron -f -L 2
