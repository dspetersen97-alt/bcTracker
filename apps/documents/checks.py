"""
Deploy-time checks for the document store.

These run under ``manage.py check --deploy``, which the container entrypoint calls
before gunicorn starts. The point is that a production container with a missing
master key or with virus scanning switched off refuses to boot, rather than
accepting uploads and discovering the problem when someone tries to open one.

Registered in apps.py rather than left as documentation, because a deployment
note is not a control.
"""

from django.conf import settings
from django.core.checks import Error, Warning, register

#: Keys published in a settings file, and therefore worthless. Listed so a
#: production container that inherited one from a copied .env fails to start.
PUBLISHED_KEYS = frozenset(
    {
        "YmN0cmFja2VyLXRlc3Qtb25seS1tYXN0ZXIta2V5MzI=",
        "YmN0cmFja2VyLWRldi1vbmx5LW1hc3Rlci1rZXktMzI=",
    }
)


@register("documents", deploy=True)
def check_master_key(app_configs, **kwargs):
    from apps.documents.crypto import KEY_SIZE

    key = getattr(settings, "DOCUMENT_MASTER_KEY", "") or ""
    if not key:
        return [
            Error(
                "BCTRACKER_MASTER_KEY is not set, so no document can be stored or read.",
                hint=(
                    'Generate one with: python -c "import base64,os; '
                    'print(base64.b64encode(os.urandom(32)).decode())" and back it up '
                    "sealed and offline, separately from the database and document backups."
                ),
                id="documents.E001",
            )
        ]

    if key in PUBLISHED_KEYS:
        return [
            Error(
                "BCTRACKER_MASTER_KEY is a development key that is published in this "
                "repository. Every document stored under it should be treated as "
                "unencrypted.",
                hint="Generate a real key and re-upload anything already stored.",
                id="documents.E002",
            )
        ]

    import base64

    try:
        decoded = base64.b64decode(key, validate=True)
    except (ValueError, TypeError):
        return [Error("BCTRACKER_MASTER_KEY is not valid base64.", id="documents.E003")]
    if len(decoded) != KEY_SIZE:
        return [
            Error(
                f"BCTRACKER_MASTER_KEY must decode to {KEY_SIZE} bytes, got {len(decoded)}.",
                id="documents.E004",
            )
        ]
    return []


@register("documents", deploy=True)
def check_scanning_is_on(app_configs, **kwargs):
    problems = []
    if not settings.DOCUMENT_SCAN_ENABLED:
        problems.append(
            Error(
                "Virus scanning is disabled. Uploads from counselees would be stored "
                "and later handed to a counselor without being checked.",
                hint="Unset DOCUMENT_SCAN_ENABLED, and run the clamav compose service.",
                id="documents.E005",
            )
        )
    elif not settings.DOCUMENT_SCAN_REQUIRED:
        problems.append(
            Warning(
                "DOCUMENT_SCAN_REQUIRED is off, so an upload is accepted when ClamAV "
                "cannot be reached.",
                hint="Leave it on. A failing upload is better than an unscanned one.",
                id="documents.W001",
            )
        )
    return problems


@register("documents", deploy=True)
def check_store_is_not_served(app_configs, **kwargs):
    """The document volume must not be inside anything the web server publishes.

    A store placed under STATIC_ROOT would be served by WhiteNoise at a guessable
    URL with no permission check at all. It would still be ciphertext, but it
    would be ciphertext anyone could fetch and keep.
    """
    from pathlib import Path

    store = Path(settings.DOCUMENT_STORE_ROOT).resolve()
    published = [
        Path(settings.STATIC_ROOT).resolve(),
        *(Path(directory).resolve() for directory in settings.STATICFILES_DIRS),
    ]
    for directory in published:
        if store == directory or directory in store.parents:
            return [
                Error(
                    f"DOCUMENT_STORE_ROOT ({store}) is inside a directory the web "
                    f"server publishes ({directory}).",
                    hint="Move the document store outside STATIC_ROOT and STATICFILES_DIRS.",
                    id="documents.E006",
                )
            ]
    return []
