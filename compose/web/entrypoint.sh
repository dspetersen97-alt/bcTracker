#!/bin/sh
# Entrypoint for the web container.
#
# Applies migrations before serving. Safe with multiple gunicorn workers because
# only this process runs migrate, before any worker starts; and safe to re-run
# because Django migrations are idempotent.
set -eu

echo "web: waiting for postgres at ${POSTGRES_HOST:-db}:${POSTGRES_PORT:-5432}"
until pg_isready -h "${POSTGRES_HOST:-db}" -p "${POSTGRES_PORT:-5432}" \
        -U "${POSTGRES_USER:-bctracker}" -q; do
    sleep 1
done

echo "web: applying migrations"
python manage.py migrate --noinput

# Fail fast on a misconfigured deployment rather than serving in a weak state:
# --deploy turns missing HSTS, insecure cookies, and DEBUG=True into errors.
echo "web: running deployment checks"
python manage.py check --deploy --fail-level WARNING

exec "$@"
