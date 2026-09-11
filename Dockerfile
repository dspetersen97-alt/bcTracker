# Python 3.13 is the newest version Django 5.2 LTS officially supports.
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings.prod

# libmagic1: identify uploads by content, not by the extension a client claims.
# postgresql-client: pg_dump for backups.
# cron: drives scheduled reminders and sync in the cron sidecar service.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1 \
        postgresql-client \
        cron \
    && rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user. A web process that can be made to write outside its
# own data directory is a much bigger problem when that data is counseling files.
RUN groupadd --gid 1000 bctracker \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin bctracker

WORKDIR /app

# Requirements first so a code change does not invalidate the dependency layer.
COPY requirements/ requirements/
RUN pip install -r requirements/base.txt

COPY --chown=bctracker:bctracker . .

# Git checkouts on Windows lose the executable bit, so set it here rather than
# depending on how the repo was cloned.
RUN chmod +x compose/web/entrypoint.sh compose/cron/entrypoint.sh

# Collected static files are baked into the image; the documents and backups
# volumes are mounted at runtime and must be writable by the runtime user. The
# directories are created here on purpose: Docker copies ownership from the image
# path when it initialises a named volume, so without them the volumes would
# arrive owned by root and the cron jobs — which run as bctracker — could not
# write a backup.
RUN DJANGO_SECRET_KEY=build-time-placeholder \
    DJANGO_ALLOWED_HOSTS=localhost \
    DJANGO_CSRF_TRUSTED_ORIGINS=https://localhost \
    python manage.py collectstatic --noinput \
    && mkdir -p /var/lib/bctracker/documents /var/lib/bctracker/backups \
    && chown -R bctracker:bctracker /var/lib/bctracker /app/staticfiles

USER bctracker

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

ENTRYPOINT ["/app/compose/web/entrypoint.sh"]

# 3 workers with 2 threads suits a ministry-sized user count on modest hardware.
# The long timeout accommodates streaming decryption of large document downloads.
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "3", \
     "--threads", "2", \
     "--timeout", "120", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
