"""
Settings for pytest.

Tests run against a real Postgres instance because the schema depends on range
types and btree_gist exclusion constraints. There is no SQLite mode to fall
back to; if Postgres is unreachable, the suite should fail loudly rather than
silently testing something other than production behaviour.
"""

from .base import *  # noqa: F403
from .base import env

DEBUG = False

ALLOWED_HOSTS = ["testserver", "localhost"]

# Concrete models used only to exercise the abstract bases in apps/core.
INSTALLED_APPS = [*INSTALLED_APPS, "tests.testapp.apps.TestAppConfig"]  # noqa: F405

DATABASES["default"]["HOST"] = env("POSTGRES_HOST", default="localhost")  # noqa: F405
DATABASES["default"]["NAME"] = env("POSTGRES_DB", default="bctracker")  # noqa: F405

# Fast hashing so the role-isolation matrix, which creates many users, stays
# quick. Safe here precisely because no real password ever exists in tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# Plain static storage. The production backend is WhiteNoise's manifest storage,
# which raises on any asset missing from a collectstatic manifest — so without
# this, every test that renders a page would require a build step first.
STORAGES = {  # noqa: F405
    **STORAGES,  # noqa: F405
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# WhiteNoise scans STATIC_ROOT when it is instantiated and warns if the
# directory is absent. In production collectstatic runs during the image build;
# locally it may never have run, so make the directory rather than let a
# harmless warning appear in every test that renders a page.
STATIC_ROOT.mkdir(parents=True, exist_ok=True)  # noqa: F405

# Documents written during tests go to a throwaway directory; the conftest
# fixture points this at a tmp_path per session.
DOCUMENT_STORE_ROOT = BASE_DIR / ".pytest-documents"  # noqa: F405

# A fixed, published key. Tests must exercise the real envelope scheme rather
# than a stubbed one, and a key committed to a test settings file is worthless to
# an attacker precisely because it is published here.
DOCUMENT_MASTER_KEY = "YmN0cmFja2VyLXRlc3Qtb25seS1tYXN0ZXIta2V5MzI="  # 32 bytes of ASCII

# No ClamAV daemon in the test environment. Tests that care about scanning drive
# apps.documents.scanning directly or turn this back on with a stub client, so
# the fail-closed path is still covered — see tests/test_documents_scanning.py.
DOCUMENT_SCAN_ENABLED = False
DOCUMENT_SCAN_REQUIRED = False

# Login throttling off by default, because dozens of tests submit a deliberately
# wrong password and would otherwise lock one another out — the failures share an
# IP, and an actor's address repeats across a parametrized matrix. Switched back
# on per-test, with `settings.AXES_ENABLED = True`, in
# tests/test_login_throttling.py, which is where the throttle is asserted.
AXES_ENABLED = False

# Keep test output readable.
LOGGING["root"]["level"] = "WARNING"  # noqa: F405
