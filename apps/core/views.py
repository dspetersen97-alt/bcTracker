"""Operational endpoints. Nothing here may expose counselee data."""

from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache


@never_cache
def healthz(request):
    """Liveness/readiness probe for the container healthcheck.

    Checks the database round-trip, because a web process that cannot reach
    Postgres is not usable. Intentionally returns no version or configuration
    detail — this endpoint is reachable without authentication.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:
        return JsonResponse({"status": "error"}, status=503)
    return JsonResponse({"status": "ok"})
