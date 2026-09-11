"""Local development settings. Never used in the deployed container."""

from .base import *  # noqa: F403
from .base import env

DEBUG = True

ALLOWED_HOSTS = ["localhost", "127.0.0.1", "[::1]"]

# Postgres still runs in Docker for development, so the default host differs
# from production only in being reachable on localhost.
DATABASES["default"]["HOST"] = env("POSTGRES_HOST", default="localhost")  # noqa: F405

# Print emails to the console rather than sending real mail to counselees.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

# A long session is convenient while developing; production uses the real
# idle timeout from base.py.
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

# Scanning off by default locally, so uploads work without a ClamAV container.
# Documents stored this way are marked ScanStatus.SKIPPED rather than "clean", so
# a development database cannot be mistaken for a scanned one. Set
# DOCUMENT_SCAN_ENABLED=true in .env to exercise the real path against the
# compose service.
DOCUMENT_SCAN_ENABLED = env.bool("DOCUMENT_SCAN_ENABLED", default=False)
DOCUMENT_SCAN_REQUIRED = env.bool("DOCUMENT_SCAN_REQUIRED", default=False)

# A development master key, so `runserver` works on a fresh checkout without one
# in .env. Different from the test key on purpose: a document stored in the dev
# database must not decrypt with a key published in a settings file that CI uses,
# and vice versa. Overridden by BCTRACKER_MASTER_KEY when it is set.
DOCUMENT_MASTER_KEY = env(
    "BCTRACKER_MASTER_KEY",
    default="YmN0cmFja2VyLWRldi1vbmx5LW1hc3Rlci1rZXktMzI=",
)
