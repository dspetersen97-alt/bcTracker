"""
Production settings for the deployed container.

TLS is terminated by Caddy in front of the app, so the HTTPS-dependent flags
below are safe to enable and the proxy header is trusted.
"""

from .base import *  # noqa: F403
from .base import env

DEBUG = False

ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS")

# Caddy terminates TLS and forwards X-Forwarded-Proto. This is only safe
# because nothing but the proxy can reach the app port on the compose network.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = True

# The container healthcheck probes http://127.0.0.1:8000/healthz directly, with
# no proxy in front of it to set X-Forwarded-Proto. Without this exemption the
# probe gets a 301 to an https URL nothing is listening on, so the container
# would never report healthy and dependent services would never start. Safe to
# exempt: the view is unauthenticated, sets no cookies, and returns only
# {"status": ...}. Externally, Caddy still redirects http to https itself.
SECURE_REDIRECT_EXEMPT = [r"^healthz$"]

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

# HSTS. Start with a short max-age and raise it once the deployment is known
# good — the directive is hard to walk back once browsers have cached it.
#
# Caddy sets this header too, and its `header` directive replaces rather than
# appends, so in the deployed stack Caddy's value is the one browsers see. Both
# read SECURE_HSTS_SECONDS with the same default so raising it is one change.
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=3600)
SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# Preload is deliberately off. Submitting to the browser preload list is a
# commitment that is slow and awkward to reverse, and it buys nothing for a
# single ministry hostname that users reach by bookmark. Silenced rather than
# left as a warning so that `check --deploy --fail-level WARNING` stays a
# meaningful gate in the entrypoint and in CI.
SECURE_HSTS_PRELOAD = False
SILENCED_SYSTEM_CHECKS = ["security.W021"]
