"""Request-scoped plumbing shared by the whole app."""

import uuid
from contextvars import ContextVar

from django.conf import settings
from django.utils import timezone

# Holds the current request's id so audit rows written deep in a call stack can
# be correlated without threading the request through every function signature.
# A ContextVar (not a thread local) so it stays correct under async views.
_request_id: ContextVar[str] = ContextVar("request_id", default="")


def get_request_id() -> str:
    return _request_id.get()


class RequestIDMiddleware:
    """Assign every request a unique id, echoed in the response headers.

    The id is generated here rather than trusted from an inbound header: a
    client-supplied value would let a caller forge or collide audit trail
    correlation ids.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = uuid.uuid4().hex
        request.request_id = request_id
        token = _request_id.set(request_id)
        try:
            response = self.get_response(request)
        finally:
            _request_id.reset(token)
        response["X-Request-ID"] = request_id
        return response


class SecurityHeadersMiddleware:
    """Send the headers Django has no setting for.

    Caddy sets these too, and in the deployed stack its copies are the ones a
    browser sees — but only for responses that go through Caddy. Development, the
    test suite, and anything reaching gunicorn directly would otherwise have no
    Content-Security-Policy at all, which is how a template picks up an inline
    ``<script>`` that works everywhere except production. Setting them here means
    the policy is in force wherever this application runs, and
    tests/test_security_headers.py asserts the two copies still agree.

    Nothing is templated per-request: there are no nonces because there is no
    inline script or style anywhere in this project, and a policy that never
    varies cannot be got wrong by a view that forgets to vary it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.setdefault("Content-Security-Policy", settings.CONTENT_SECURITY_POLICY)
        response.setdefault("Permissions-Policy", settings.PERMISSIONS_POLICY)
        # Only meaningful on documents, which are served as attachments, but it
        # costs nothing and covers a future view that renders one inline.
        response.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        return response


class TimezoneMiddleware:
    """Render every time in the viewer's own zone.

    Everything is stored UTC, and without this every template would render UTC,
    which for a scheduling product is not a cosmetic problem: a counselee reading
    "Tuesday 14:00" for a 10am appointment misses it. ``timezone.activate``
    affects template rendering and form parsing, not what is stored.

    Reset after the response because the activation is thread-local and worker
    threads are reused: leaving it set would render one user's page in the
    previous user's zone.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            timezone.activate(user.zoneinfo)
        try:
            return self.get_response(request)
        finally:
            timezone.deactivate()
