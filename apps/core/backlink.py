"""
The Back link that every page but Home carries.

Why this is a server-side thing at all
--------------------------------------

The obvious implementation is one line of JavaScript, and this application does not
have any: the Content-Security-Policy has no ``'unsafe-inline'``, so a handler would
have to be an external file, and a control that stops working when a script fails is
a control that strands somebody on a page with no way out. See the same reasoning at
the sidebar in templates/base.html.

So Back is a link with an ``href``, and the only thing the server knows about where
somebody came from is the ``Referer`` header. That is a real limitation and worth
being honest about: it is one step, not a history stack, so Back followed by Back
returns to the page Back was first pressed on. A browser's own Back button does not
behave that way, and it is still there and still works. What this offers is the thing
people actually asked for — a visible way out of a page they opened from a list, on a
phone where the browser's chrome is a swipe rather than a button.

What is refused, and why
------------------------

  * **Anything not on this site.** ``Referer`` is a header a request controls, so a
    Back link built from it without a host check is an open redirect rendered onto
    every authenticated page — precisely the shape of link a phishing message wants
    to send. The check is Django's own, the same one ``safe_next`` uses.
  * **The page it is already on.** A form that failed validation was posted from
    itself, so its referer is its own URL; a Back that reloads the page and throws
    away what was typed would be worse than no Back at all.
  * **Home**, because Home is where Back would go and the sidebar already has it.
  * **Any page a half-signed-in session can reach.** Between the password and the
    TOTP code there is nowhere to go back to — every link bounces to the code prompt
    — and that judgement is made once, in apps/accounts/middleware.py, by the same
    function that decides whether to render the menu.

Only the path and the query are kept from the referer, never the scheme or host: the
link stays relative, so it cannot become an absolute URL to somewhere else even if
the host check is one day loosened by mistake. The query is kept because going back
to a filtered list and finding it unfiltered is not going back.
"""

from urllib.parse import urlsplit

from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from apps.accounts.middleware import session_is_fully_authenticated


def back_link(request):
    """Context processor: where Back goes, or ``None`` for pages that do not offer it.

    ``None`` rather than a fallback to Home, so that the template shows nothing at
    all. A Back button that sometimes means Home is a button whose behaviour has to
    be learned by pressing it.
    """
    return {"back_url": previous_page(request)}


def previous_page(request) -> str | None:
    """The page this request came from, as a path this site will accept."""
    if not session_is_fully_authenticated(request):
        return None
    if request.path == reverse("core:home"):
        return None

    referer = request.META.get("HTTP_REFERER", "")
    if not referer or not url_has_allowed_host_and_scheme(
        url=referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return None

    parts = urlsplit(referer)
    if parts.path == request.path:
        return None
    return parts.path + (f"?{parts.query}" if parts.query else "")
