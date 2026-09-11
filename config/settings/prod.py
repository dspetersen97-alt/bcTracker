"""
Production settings for the deployed container.

TLS is terminated by Caddy in front of the app, so the HTTPS-dependent flags
below are safe to enable and the proxy header is trusted.
"""

from .base import *  # noqa: F403
from .base import env

DEBUG = False

# The loopback address is appended rather than left to whoever writes .env,
# because it is not a deployment choice: the image's HEALTHCHECK probes
# http://127.0.0.1:8000/healthz, and with only the public hostname allowed that
# probe gets a 400 (DisallowedHost), the web service never reports healthy, and
# caddy — which waits for exactly that — never starts. The site simply never comes
# up, with nothing in the log but a rejected host.
#
# Safe to allow: nothing outside the compose network can reach the app port, and
# every link this application puts in an email is built from SITE_BASE_URL rather
# than from the host on the request, so a Host header cannot redirect a login link.
ALLOWED_HOSTS = [*env.list("DJANGO_ALLOWED_HOSTS"), "127.0.0.1"]
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

# W021: preload is off, deliberately, just above.
#
# W019: X_FRAME_OPTIONS is SAMEORIGIN rather than DENY. The check says "unless
# there is a good reason for your site to serve other parts of itself in a frame",
# and this site has exactly one: the document page shows a PDF in a frame of its
# own preview route instead of sending a counselor to a new tab, and X-Frame-Options
# is enforced on the framed response. DENY there is an empty box.
#
# What the check is protecting against — somebody else's page framing ours to
# collect a click meant for us — is still refused, by SAMEORIGIN and again by
# ``frame-ancestors 'self'`` in CONTENT_SECURITY_POLICY. Both are asserted in
# tests/test_security_headers.py, which is what makes silencing this honest rather
# than convenient: relax either of them to allow another origin and that suite
# fails, so this entry cannot quietly come to mean more than it says.
#
# Silenced rather than left as a warning because the entrypoint runs
# `check --deploy --fail-level WARNING` before gunicorn starts. A warning nobody
# can act on there is not a warning, it is a container that will not boot.
SILENCED_SYSTEM_CHECKS = ["security.W019", "security.W021"]
