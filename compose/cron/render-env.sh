#!/bin/sh
# Print this container's configuration as crontab environment lines.
#
# cron starts every job with a near-empty environment, so whatever the container
# was configured with has to be written into the crontab itself or the scheduled
# work runs against Django's defaults instead.
#
# This used to be a list of variable names, and the list rotted. BACKUP_ROOT,
# SITE_BASE_URL, and every GOOGLE_/BILLING_/STRIPE_ setting arrived after it was
# written and were never added to it, so the nightly backup wrote to the image's
# own /app/backups instead of the mounted volume, reminder emails linked to
# https://localhost, and the Stripe reconciler reported itself unconfigured every
# night. All of it silently: an unset variable is a default, not an error.
#
# So the rule is inverted. Everything is passed through except the variables that
# describe the container rather than the deployment, which means a setting added to
# .env tomorrow reaches the scheduled jobs without anyone remembering this file.
#
# Written to stdout rather than straight into the crontab so that it can be tested
# without a container — see tests/test_deployment_plumbing.py.
set -eu

# Variables belonging to the image, the shell, or the locale. PATH and SHELL are
# written by the caller with values suitable for cron; the rest either mislead a
# job or say nothing about how this deployment is configured.
DROP='^(PATH|SHELL|PWD|OLDPWD|HOME|HOSTNAME|TERM|USER|LOGNAME|SHLVL|LANG|LC_[A-Z_]+|PYTHON[A-Z_]*|PIP_[A-Z_]+|GPG_KEY|_)='

# Well-formed single-line assignments only, and sorted so the file is stable
# between restarts. `env` prints a value containing a newline across several lines,
# and a continuation line in a crontab is a parse error that takes down every job
# in the file rather than just the variable that caused it.
env | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' | grep -Ev "$DROP" | sort
