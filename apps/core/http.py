"""
Small request helpers that more than one app needs.

``safe_next`` is here rather than in ``accounts`` because two apps now pass an
administrator back to where they were: creating a person can be reached from the
sign-in flow, from the New Person page, and from the New Case page. Copying the
host check into the second caller is how one copy of it eventually gets it wrong,
and getting it wrong turns ``?next=`` into an open redirect on an authenticated
page — which is exactly the kind of link a phishing message wants to send.
"""

from urllib.parse import urlencode

from django.utils.http import url_has_allowed_host_and_scheme


def safe_next(request, default: str) -> str:
    """The ``next`` the request asked for, if it points at this site.

    POST before GET, so a form that carries the value through a redisplay wins
    over whatever is still in the address bar.
    """
    candidate = request.POST.get("next") or request.GET.get("next") or ""
    if candidate and url_has_allowed_host_and_scheme(
        url=candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return default


def with_query(url: str, **params) -> str:
    """Add query parameters to a URL that may already have some.

    Used to hand a caller back something it did not know when it sent the actor
    away — the New Case page gets ``?counselee=<pk>`` for the person just
    created, so returning to a half-filled form does not mean hunting for them in
    a list of everybody.
    """
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"
