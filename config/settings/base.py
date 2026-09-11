"""
Settings shared by every environment.

Anything security-relevant that must NOT differ between dev and production
lives here on purpose, so a misconfigured dev override can't silently weaken
production. Environment-specific files layer on top:

    config/settings/dev.py    local development
    config/settings/prod.py   the deployed container
    config/settings/test.py   pytest

Every secret is read from the environment. See .env.example for the full list.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()

# Read .env if present. In production the values come from the container
# environment instead, so a missing file is not an error.
env_file = BASE_DIR / ".env"
if env_file.exists():
    env.read_env(str(env_file))


# --- Identity -------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY")
DEBUG = False
ALLOWED_HOSTS: list[str] = []

AUTH_USER_MODEL = "accounts.User"

# Absolute base for links in outbound email. A request-derived host is not usable
# for mail sent from a scheduled job, and trusting the Host header to build a
# login link would let a spoofed request produce a link pointing elsewhere.
SITE_BASE_URL = env("SITE_BASE_URL", default="https://localhost").rstrip("/")

# The admin is a break-glass tool for a superuser, not a product surface. Moving
# it off /admin/ removes it from the path of automated credential-stuffing bots;
# the real protection is the superuser-only check in apps/core/admin_site.py.
ADMIN_URL_PATH = env("DJANGO_ADMIN_URL_PATH", default="admin")


# --- Applications ---------------------------------------------------------

DJANGO_APPS = [
    # Replaces django.contrib.admin so the superuser-only admin site is the
    # default one. See apps/core/admin_site.py for why.
    "apps.core.admin_config.BcTrackerAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.postgres",
]

THIRD_PARTY_APPS = [
    # TOTP second factor. Only the totp plugin is enabled: static recovery codes
    # and email/SMS devices are deliberately left out, since a weaker fallback
    # factor is the usual way a second factor stops meaning anything.
    "django_otp",
    "django_otp.plugins.otp_totp",
    "rules.apps.AutodiscoverRulesConfig",
    # Login throttling. Counts failures per (address, IP) pair and locks that
    # pair out for a cooloff period — see the AXES_ settings below.
    "axes",
]

LOCAL_APPS = [
    "apps.core",
    "apps.accounts",
    "apps.audit",
    # Owns Case and CaseMember, which every other app's access rules resolve
    # through, so it is listed before the apps that depend on it.
    "apps.counseling",
    "apps.documents",
    "apps.scheduling",
    "apps.messaging",
    # Sessions, invoices, and payments. Listed after scheduling because a session
    # record is raised from a booking that has been closed out.
    "apps.billing",
]

INSTALLED_APPS = [*DJANGO_APPS, *THIRD_PARTY_APPS, *LOCAL_APPS]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Outermost of ours, so the policy is attached to every response including
    # the ones returned by middleware below it (a redirect to the login page, a
    # 429 from the lockout handler, a 500 page).
    "apps.core.middleware.SecurityHeadersMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Must follow AuthenticationMiddleware: it wraps request.user with the
    # is_verified() check that MFAEnforcementMiddleware below relies on.
    "django_otp.middleware.OTPMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Assigns a request id used to correlate audit rows written by one request.
    "apps.core.middleware.RequestIDMiddleware",
    # A staff session that has not passed TOTP can reach nothing but the
    # enrolment and verification pages. Runs late so it sees the resolved,
    # OTP-aware user and can redirect before any view executes.
    "apps.accounts.middleware.MFAEnforcementMiddleware",
    "apps.accounts.middleware.LastSeenMiddleware",
    # Turns the AxesBackend's PermissionDenied into the lockout response. Must
    # come after AuthenticationMiddleware, and is late here because a locked-out
    # request has no session to keep current.
    "axes.middleware.AxesMiddleware",
    # Activates the viewer's own timezone for rendering. Last, so it wraps only
    # view execution and template rendering; it needs request.user, so it has to
    # follow AuthenticationMiddleware.
    "apps.core.middleware.TimezoneMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # The unread count in the header. Cheap, and zero queries for the
                # roles that have no place in a conversation.
                "apps.messaging.context.unread_messages",
                # The sidebar's links, built from the role. No queries at all —
                # see apps/core/navigation.py for why they are data and not markup.
                "apps.core.navigation.navigation",
            ],
        },
    },
]


# --- Database -------------------------------------------------------------

# Postgres only. The scheduling app relies on Postgres range types, btree_gist
# exclusion constraints, and JSONB, none of which SQLite can emulate — so there
# is deliberately no SQLite fallback, including for tests.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", default="bctracker"),
        "USER": env("POSTGRES_USER", default="bctracker"),
        "PASSWORD": env("POSTGRES_PASSWORD", default=""),
        "HOST": env("POSTGRES_HOST", default="db"),
        "PORT": env.int("POSTGRES_PORT", default=5432),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {"connect_timeout": 10},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# --- Passwords and authentication ----------------------------------------

# Argon2id first. Django still needs the others listed so existing hashes from
# a weaker algorithm can be upgraded transparently on next login.
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 12},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

AUTHENTICATION_BACKENDS = [
    # First, because it is the one that refuses. It authenticates nobody: it
    # raises PermissionDenied when the (address, IP) pair is locked out, which
    # stops the chain before ModelBackend can check a password.
    "axes.backends.AxesStandaloneBackend",
    # Object-level predicates next. It only ever answers permission checks that
    # carry an object; it cannot authenticate, so ModelBackend still owns login.
    "rules.permissions.ObjectPermissionBackend",
    "django.contrib.auth.backends.ModelBackend",
]

LOGIN_URL = "accounts:login"
LOGOUT_REDIRECT_URL = "accounts:login"
# Where to land after login. The real decision is made by
# apps.accounts.views.post_login_url; this value only matters for the few flows
# that bypass it, and it names the same page.
LOGIN_REDIRECT_URL = "core:home"

# --- Second factor and login links ---------------------------------------

OTP_TOTP_ISSUER = env("OTP_TOTP_ISSUER", default="bcTracker")
# Tolerate one 30-second step either side of now, for clock drift on the phone.
OTP_TOTP_SYNC = False
OTP_TOTP_TOLERANCE = 1

# Magic links are a *counselee* convenience, so they are short-lived: the window
# in which a forwarded or mailbox-breach link still works is the whole risk.
MAGIC_LINK_TTL_SECONDS = env.int("MAGIC_LINK_TTL_SECONDS", default=15 * 60)
# Invitations are handed to someone who has no password yet, so they need long
# enough to be acted on, and they are single-use like every other token.
INVITATION_TTL_SECONDS = env.int("INVITATION_TTL_SECONDS", default=7 * 24 * 60 * 60)
# Cap on live login links per address per window, so the form cannot be used to
# flood someone's inbox or burn the Workspace send quota.
LOGIN_LINK_MAX_PER_WINDOW = env.int("LOGIN_LINK_MAX_PER_WINDOW", default=3)
LOGIN_LINK_WINDOW_SECONDS = env.int("LOGIN_LINK_WINDOW_SECONDS", default=15 * 60)


# --- Login throttling (django-axes) ---------------------------------------

# On everywhere except the test suite, which switches it off so that the many
# deliberate wrong-password tests do not lock each other out; the throttle's own
# behaviour is asserted with it switched back on, in tests/test_login_throttling.py.
AXES_ENABLED = env.bool("AXES_ENABLED", default=True)

# Five, not three. A counselee mistyping a password they use fortnightly is far
# more common here than an attack, and locking a distressed person out of their
# counselor's booking page has a real cost. Five still reduces an online guessing
# attack to nothing against a 12-character minimum.
AXES_FAILURE_LIMIT = env.int("AXES_FAILURE_LIMIT", default=5)

# The lockout expires by itself. A lockout that needs an administrator to clear
# it hands an attacker a way to take a counselor offline for the evening by
# guessing at their address five times.
AXES_COOLOFF_TIME = timedelta(minutes=env.int("AXES_COOLOFF_MINUTES", default=15))

# Lock the *pair*, not either half. Locking by address alone means anyone who
# knows a counselor's email can lock them out from anywhere; locking by IP alone
# means one person's typos lock out the whole church office, which shares an
# address behind NAT. The pair is what "somebody is guessing at this account from
# there" actually means.
#
# This does not stop a distributed attack, and is not meant to: against that the
# defences are Argon2id, the 12-character minimum, and TOTP on every staff role.
AXES_LOCKOUT_PARAMETERS = [["username", "ip_address"]]

# Which POST field carries the address. Stated because axes defaults to
# ``User.USERNAME_FIELD``, which here is "email", while Django's
# AuthenticationForm posts it as "username" — so the default would find nothing,
# record every failure against a null address, and quietly degrade the pair above
# into a lockout by IP alone. tests/test_login_throttling.py is what caught it.
AXES_USERNAME_FORM_FIELD = "username"

# Attempts made *during* a cooloff do not extend it. Axes' default restarts the
# clock on every refused request, which would make the "paused for 15 minutes" on
# the lockout page untrue for the person most likely to read it — somebody who
# retries too early. An attacker gains nothing either way, since none of those
# attempts is allowed to check a password.
AXES_RESET_COOL_OFF_ON_FAILURE_DURING_LOCKOUT = False

# A successful sign-in clears the counter, so yesterday's typos plus today's do
# not add up to a lockout.
AXES_RESET_ON_SUCCESS = True

# One definition of "which IP is the client", shared with the audit trail, so a
# lockout and the rows explaining it cannot disagree about who was knocking.
AXES_CLIENT_IP_CALLABLE = "apps.audit.services.client_ip"

# Our own response, so a locked-out attempt says the same thing whether or not
# the address has an account here — see apps/accounts/lockout.py.
AXES_LOCKOUT_CALLABLE = "apps.accounts.lockout.locked_out"

# Axes' own AccessLog is a second, mutable record of who signed in and when. The
# audit trail already holds that, append-only and enforced by a database trigger,
# so the duplicate would only be a place for the two to disagree. AccessAttempt
# rows are kept: that is the counter the lockout is computed from.
AXES_DISABLE_ACCESS_LOG = True


# --- Sessions -------------------------------------------------------------

# Counseling records are sensitive and often accessed on shared machines in a
# church office, so sessions expire on browser close and idle out server-side.
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_AGE = env.int("SESSION_IDLE_TIMEOUT_SECONDS", default=30 * 60)
SESSION_SAVE_EVERY_REQUEST = True  # makes SESSION_COOKIE_AGE an *idle* timeout
SESSION_EXPIRE_AT_BROWSER_CLOSE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = False  # must stay readable for the CSRF token pattern
CSRF_COOKIE_SAMESITE = "Lax"


# --- Security defaults ----------------------------------------------------
# TLS-dependent flags are switched on in prod.py, where HTTPS is guaranteed.

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"

# Content-Security-Policy, sent by apps.core.middleware.SecurityHeadersMiddleware.
#
# This string is duplicated in compose/caddy/Caddyfile, which is the copy a
# browser sees in the deployed stack; tests/test_security_headers.py reads the
# Caddyfile and fails if the two drift apart. It lives here as well so that
# development and the test suite run under the same policy production does —
# otherwise the first thing to discover a violation would be a user.
#
# There is deliberately no 'unsafe-inline' anywhere, which is why every style in
# this project is in static/css/bctracker.css and there is no inline event
# handler. ``script-src 'self'`` rather than ``'none'``: the application ships no
# JavaScript at all, but the break-glass Django admin has its own, and a policy
# that breaks the admin is one somebody will switch off in an emergency.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "frame-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

# Nothing here needs a camera, a microphone, or a location. Denying them means a
# compromised or mistaken page cannot ask a counselee for any of it — and the
# prompt itself would be alarming on a counseling site.
PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), display-capture=(), geolocation=(), "
    "gyroscope=(), microphone=(), payment=(), usb=()"
)


# --- Internationalisation and time ---------------------------------------

# All timestamps are stored in UTC and rendered in the viewer's own timezone.
# Scheduling depends on this being true; see apps/scheduling/slots.py.
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# The ministry's local timezone, used for rendering when a user has not set
# their own, for interpreting counselor office hours, and — importantly — for
# every *stored* business date: the day a case opened, a membership ended, a
# window came into force. See apps/core/dates.py for why those must not come from
# the acting user's timezone.
ORG_TIME_ZONE = env("ORG_TIME_ZONE", default="America/New_York")


# --- Static files ---------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# There is deliberately no MEDIA_URL or MEDIA_ROOT. Counselee documents are
# never served by the web server or from a guessable URL; they are streamed by
# a permission-checked view out of the encrypted store below.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# Root of the encrypted document store (a mounted volume in production).
DOCUMENT_STORE_ROOT = Path(env("DOCUMENT_STORE_ROOT", default=str(BASE_DIR / "documents")))

# Upload limits. Anything larger is rejected before it is written to disk.
DOCUMENT_MAX_BYTES = env.int("DOCUMENT_MAX_BYTES", default=25 * 1024 * 1024)
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
# Uploads land in a temporary file owned by the app user only. Django's default
# is 0o600 on most platforms but is None — meaning "whatever umask says" — when
# a large upload is moved rather than copied, so it is stated.
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700


# --- Document encryption --------------------------------------------------

# CRITICAL: this key wraps the per-file key of every stored document. If it is
# lost, every document is permanently unreadable, backups included. It must be
# backed up sealed and offline, stored separately from the ciphertext and the
# database. See apps/documents/crypto.py for the scheme.
#
# Empty by default so `manage.py` commands that touch no documents still run on
# a fresh checkout; crypto.master_key() raises ImproperlyConfigured the moment a
# document is actually stored or read.
DOCUMENT_MASTER_KEY = env("BCTRACKER_MASTER_KEY", default="")


# --- Virus scanning -------------------------------------------------------

# ClamAV runs as its own compose service. Both flags default to on: an upload
# that could not be scanned is refused rather than stored, which is deliberately
# the inconvenient choice. dev.py and test.py turn scanning off; prod.py checks
# that they have not been carried into production.
DOCUMENT_SCAN_ENABLED = env.bool("DOCUMENT_SCAN_ENABLED", default=True)
DOCUMENT_SCAN_REQUIRED = env.bool("DOCUMENT_SCAN_REQUIRED", default=True)
CLAMAV_HOST = env("CLAMAV_HOST", default="clamav")
CLAMAV_PORT = env.int("CLAMAV_PORT", default=3310)
CLAMAV_UNIX_SOCKET = env("CLAMAV_UNIX_SOCKET", default="")
CLAMAV_TIMEOUT_SECONDS = env.int("CLAMAV_TIMEOUT_SECONDS", default=30)


# --- Email ----------------------------------------------------------------

# Any SMTP provider, defaulting to Google Workspace. Deliberately kept behind
# Django's email backend interface: Workspace has a low daily send cap and no
# bounce reporting, so moving to a transactional provider should be a settings
# change only.
#
# The default backend reads its host, username and password from the database
# first and from the values below second — see apps/core/mail.py for why, and for
# where the mailbox password is sealed. Overridable by environment so a ministry
# moving to a provider with an API rather than SMTP does not have to touch code,
# and so dev.py and test.py can swap it for something that never opens a socket.
EMAIL_BACKEND = env("EMAIL_BACKEND", default="apps.core.mail.ConfiguredEmailBackend")
EMAIL_HOST = env("EMAIL_HOST", default="smtp.gmail.com")
EMAIL_PORT = env.int("EMAIL_PORT", default=587)
# Implicit TLS for the providers that offer only port 465 — Zoho documents both.
# Derived rather than configured separately, for two reasons: Django raises if
# both are set, and there is no value of EMAIL_USE_TLS that turns encryption off,
# so a typo in .env cannot end with the mailbox password crossing the network in
# the clear.
EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL", default=False)
EMAIL_USE_TLS = not EMAIL_USE_SSL
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
# No placeholder default, and an empty value in .env is treated as absent rather
# than as an address. Both halves of that matter: `DEFAULT_FROM_EMAIL=` in the
# file overrides any default with "", and Django's SMTP backend then raises
# ValueError('Invalid address ""') from inside send_mail — which surfaced as a
# 500 on "create a counselee" in a bootstrapped install. A placeholder would be
# no better: mail would leave with a From address the mailbox is not allowed to
# send as, and Workspace would reject it. apps/core/mail.py's from_address() is
# what every send actually calls, and it falls back to the sending mailbox.
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="") or EMAIL_HOST_USER
# Django itself uses SERVER_EMAIL for error mail and refuses an empty one.
SERVER_EMAIL = DEFAULT_FROM_EMAIL or "bctracker@localhost"


# --- Google Calendar ------------------------------------------------------

# Off unless a client id is configured, so a fresh checkout and the test suite
# never reach for the network. Every entry point checks this rather than relying
# on a missing credential to fail late.
GOOGLE_CALENDAR_ENABLED = env.bool("GOOGLE_CALENDAR_ENABLED", default=False)

# Staff sign-in with a ministry Google account. Separate from the calendar flag
# although it shares the OAuth client: a ministry may well want appointments in
# Google without moving its staff logins there, or the reverse. Counselees are
# never included — see apps/accounts/sso.py.
GOOGLE_SSO_ENABLED = env.bool("GOOGLE_SSO_ENABLED", default=False)

GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")
GOOGLE_OAUTH_CLIENT_SECRET = env("GOOGLE_OAUTH_CLIENT_SECRET", default="")

# The ministry's Workspace domain. Load-bearing, not cosmetic: it is checked
# against the `hd` claim on the way back from Google, so a counselor cannot
# connect a personal gmail.com account and end up with ministry appointments in
# a calendar the ministry does not control. Empty means "any account", which
# prod.py refuses.
GOOGLE_WORKSPACE_DOMAIN = env("GOOGLE_WORKSPACE_DOMAIN", default="")

# What the appointment title looks like in a counselor's Google calendar.
#
# The default names nobody. A Google calendar is the least controlled surface
# this data reaches — it is on a phone lock screen, in a laptop notification, on
# a shared display in a meeting — and "Counseling — Ashford" read aloud by a
# smart speaker is a disclosure the ministry cannot take back. The counselor
# knows who they are seeing at 10am; the calendar only has to reserve the time.
# A counselor who wants names can turn them on for their own connection —
# GoogleCredential.include_names.
GOOGLE_EVENT_TITLE = env("GOOGLE_EVENT_TITLE", default="Counseling appointment")

# Seconds of slack when deciding whether a cached access token is still good.
# Google's tokens last an hour; refreshing a minute early costs one HTTP request
# and avoids a request failing mid-sync because the token expired between the
# check and the call.
GOOGLE_TOKEN_LEEWAY_SECONDS = env.int("GOOGLE_TOKEN_LEEWAY_SECONDS", default=90)

# How long a sync run may spend on the network before the cron tick gives up.
GOOGLE_HTTP_TIMEOUT_SECONDS = env.int("GOOGLE_HTTP_TIMEOUT_SECONDS", default=20)

# How far ahead the sync command looks. Comfortably past the default 60-day
# booking horizon, so an appointment booked at the far edge still reaches the
# calendar, without the cron tick walking the whole history every 15 minutes.
GOOGLE_SYNC_HORIZON_DAYS = env.int("GOOGLE_SYNC_HORIZON_DAYS", default=90)


# --- Billing --------------------------------------------------------------

# One currency. A ministry billing in two would need a rate per currency, a
# display rule per invoice, and a reconciliation that groups by it — none of
# which is free, and none of which anyone has asked for. Stated as a setting so
# the assumption is visible rather than spelled "$" in fourteen templates.
BILLING_CURRENCY = env("BILLING_CURRENCY", default="usd")

# Days from issue to due date, used as the default when an invoice is raised.
BILLING_DUE_DAYS = env.int("BILLING_DUE_DAYS", default=30)

# Prefix on the human-readable invoice number. The number is the reference a
# counselee quotes on a check, so it wants to look like a reference and not like
# a database id.
BILLING_INVOICE_PREFIX = env("BILLING_INVOICE_PREFIX", default="BC")


# --- Stripe ---------------------------------------------------------------

# Off unless a secret key is configured, exactly like the calendar: a fresh
# checkout and the whole test suite must never reach for the network. When it is
# off, invoices still work end to end — they are just paid by cash or check and
# recorded by hand, which is how a ministry that has not signed up for Stripe
# already operates.
STRIPE_ENABLED = env.bool("STRIPE_ENABLED", default=False)

STRIPE_SECRET_KEY = env("STRIPE_SECRET_KEY", default="")

# Verifies that a webhook really came from Stripe. Without it, the endpoint is
# an unauthenticated "mark this invoice paid" route open to the internet, so
# apps/billing/stripe/webhook.py refuses every request when it is empty rather
# than trusting the body. See the deploy check billing.E004.
STRIPE_WEBHOOK_SECRET = env("STRIPE_WEBHOOK_SECRET", default="")

# How far out of date a webhook's own timestamp may be before it is refused.
# Stripe's documented default, and the reason a captured request cannot be
# replayed tomorrow — the signature stays valid forever, the timestamp does not.
STRIPE_WEBHOOK_TOLERANCE_SECONDS = env.int("STRIPE_WEBHOOK_TOLERANCE_SECONDS", default=300)

STRIPE_API_BASE = env("STRIPE_API_BASE", default="https://api.stripe.com/v1")
STRIPE_HTTP_TIMEOUT_SECONDS = env.int("STRIPE_HTTP_TIMEOUT_SECONDS", default=20)

# What a counselee sees on the hosted payment page and on their card statement.
# Deliberately says nothing but the ministry's name: a card statement is read by
# whoever opens the post, and "Biblical counseling" on it is a disclosure the
# ministry did not make and cannot withdraw.
STRIPE_STATEMENT_DESCRIPTOR = env("STRIPE_STATEMENT_DESCRIPTOR", default="")
STRIPE_PAYMENT_LABEL = env("STRIPE_PAYMENT_LABEL", default="Counseling services")


# --- Backups and retention ------------------------------------------------

# Where the nightly encrypted dump is written. A volume in production, mounted
# only into the cron service — the web process has no reason to hold a copy of
# the whole database. Getting this directory OFF the host is a separate and
# mandatory step; see docs/restore-drill.md.
BACKUP_ROOT = Path(env("BACKUP_ROOT", default=str(BASE_DIR / "backups")))

# How long backups are kept locally. Short on purpose: this directory is a
# staging area for the off-box copy, not the archive. The most recent backup is
# never deleted regardless of age (apps/core/backups.py).
BACKUP_KEEP_DAYS = env.int("BACKUP_KEEP_DAYS", default=14)

# Paths to the Postgres client binaries. Named settings rather than a bare
# "pg_dump" in the code so a host that keeps them somewhere unusual — or a
# version mismatch that needs pinning to a specific installation — is a config
# change and not a patch.
PG_DUMP_PATH = env("PG_DUMP_PATH", default="pg_dump")
PG_RESTORE_PATH = env("PG_RESTORE_PATH", default="pg_restore")

# How long a spent or expired login link is kept before purge_expired_tokens
# deletes it. The token itself is useless by then — only a SHA-256 digest is
# stored and it is single-use — so this window exists for the question "where was
# this link used from", which is the row's only remaining value.
LOGIN_TOKEN_RETENTION_DAYS = env.int("LOGIN_TOKEN_RETENTION_DAYS", default=30)


# --- Logging --------------------------------------------------------------

# Console only: the container runtime captures stdout. Note that application
# logs are *not* the audit trail — see apps/audit for that.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", default="INFO")},
    "loggers": {
        "django.db.backends": {"level": "WARNING", "propagate": True},
    },
}
