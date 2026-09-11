"""
The three Calendar API calls, and nothing else.

``upsert``, ``delete``, ``freebusy``. Written against the REST endpoints directly
because that is all this integration needs, and because a discovery-document
client would put four more packages and a runtime HTTP fetch between a booking
and a calendar entry.

Every response is funnelled through ``_request``, which turns status codes into
the error taxonomy in ``errors.py``. That is the only place a status code is
interpreted, so a caller cannot accidentally treat a 401 as a 500.

One retry, on one class of failure: a 401 after a token that looked valid, which
means the token was revoked or expired between the check and the call. Anything
else is either terminal or the cron tick's problem — retrying a 503 in a loop
inside a request is how a slow dependency becomes an outage.
"""

import logging

import requests
from django.conf import settings

from apps.scheduling.google import credentials
from apps.scheduling.google.errors import (
    AuthorizationLost,
    EventGone,
    GoogleRefused,
    TransientGoogleError,
)

logger = logging.getLogger(__name__)

API_ROOT = "https://www.googleapis.com/calendar/v3"

#: Marks an event as ours, so a future incremental sync can recognise an echo of
#: our own write and a human looking at raw event data can tell where it came from.
BOOKING_ID_PROPERTY = "bctracker_booking_id"


class CalendarClient:
    """Calls made as one counselor, against one calendar."""

    def __init__(self, credential):
        self.credential = credential

    # --- the calls --------------------------------------------------------

    def insert(self, body: dict) -> dict:
        return self._request("POST", f"/calendars/{self._calendar}/events", json=body)

    def update(self, event_id: str, body: dict, *, etag: str = "") -> dict:
        """Replace an event we already wrote.

        ``If-Match`` when we hold an etag, so a write is refused if somebody moved
        the event in Google since we last saw it. We are authoritative and will
        overwrite it, but the refusal is worth having: it tells the sync that the
        counselor edited the event, which is a signal worth logging rather than
        silently steamrolling.
        """
        headers = {"If-Match": etag} if etag else {}
        return self._request(
            "PUT",
            f"/calendars/{self._calendar}/events/{event_id}",
            json=body,
            headers=headers,
        )

    def delete(self, event_id: str) -> None:
        self._request("DELETE", f"/calendars/{self._calendar}/events/{event_id}")

    def freebusy(self, *, start, end) -> list[tuple]:
        """Busy intervals on this calendar between two aware datetimes.

        Returns ``(start, end)`` pairs and nothing else — no titles, no attendees,
        no event ids. Google's freeBusy endpoint does not return them, which is
        exactly why it is the endpoint used: reading the counselor's actual events
        would mean this application holding the contents of their personal diary
        to answer a question that only needs "busy or not".
        """
        body = {
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "items": [{"id": self.credential.calendar_id}],
        }
        payload = self._request("POST", "/freeBusy", json=body)
        calendars = payload.get("calendars") or {}
        entry = calendars.get(self.credential.calendar_id) or {}

        if entry.get("errors"):
            # A calendar the grant cannot see. Raised rather than treated as "free":
            # offering a slot because we could not check is the failure that puts two
            # appointments in one hour.
            reasons = ", ".join(e.get("reason", "?") for e in entry["errors"])
            raise GoogleRefused(f"Google would not report free/busy: {reasons}")

        return [
            (parse_google_datetime(period["start"]), parse_google_datetime(period["end"]))
            for period in entry.get("busy", [])
            if period.get("start") and period.get("end")
        ]

    # --- plumbing ---------------------------------------------------------

    @property
    def _calendar(self) -> str:
        from urllib.parse import quote

        # A calendar id is an email-shaped string, and "primary" is a literal. Quoted
        # because it lands in a path segment and an unescaped one would silently
        # address a different resource.
        return quote(self.credential.calendar_id, safe="")

    def _request(self, method: str, path: str, *, json=None, headers=None, _retried=False):
        token = credentials.access_token(self.credential)
        request_headers = {"Authorization": f"Bearer {token}"}
        if headers:
            request_headers.update(headers)

        try:
            response = requests.request(
                method,
                f"{API_ROOT}{path}",
                json=json,
                headers=request_headers,
                timeout=settings.GOOGLE_HTTP_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise TransientGoogleError(f"Could not reach Google Calendar: {exc}") from exc

        return self._interpret(response, method, path, json=json, headers=headers, retried=_retried)

    def _interpret(self, response, method, path, *, json, headers, retried):
        status = response.status_code

        if status == 401 and not retried:
            # The token was accepted by our own expiry check and refused by Google.
            # Force one refresh and try again; a second 401 is a real authorization
            # failure, not a clock disagreement.
            self.credential.access_token_expires_at = None
            self.credential.save(update_fields=["access_token_expires_at", "updated_at"])
            return self._request(method, path, json=json, headers=headers, _retried=True)
        if status == 401:
            credentials.mark_revoked(
                self.credential,
                reason="Google rejected the connection. Please reconnect your calendar.",
            )
            raise AuthorizationLost("Google returned 401 after a refresh.")

        if status in (404, 410):
            # 410 is what Google returns for an event already deleted. Both mean the
            # same thing to us: the id we hold is stale.
            raise EventGone(f"Google returned {status} for {method} {path}.")

        if status == 412:
            # If-Match failed — the event changed in Google since we wrote it.
            raise EventGone("The Google event has been changed since it was written.")

        if status == 403:
            detail = _error_detail(response)
            if any(word in detail for word in ("rateLimitExceeded", "userRateLimitExceeded")):
                raise TransientGoogleError(f"Google is rate limiting: {detail}")
            raise GoogleRefused(f"Google refused the request: {detail}")

        if status == 429 or status >= 500:
            raise TransientGoogleError(f"Google returned {status} for {method} {path}.")

        if status >= 400:
            raise GoogleRefused(f"Google returned {status}: {_error_detail(response)}")

        if status == 204 or not response.content:
            return {}
        try:
            body = response.json()
        except ValueError as exc:
            raise TransientGoogleError("Google's response was not JSON.") from exc
        return body if isinstance(body, dict) else {}


def _error_detail(response) -> str:
    """Google's own message, if it sent one. Only ever logged, never shown."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    error = body.get("error")
    if isinstance(error, dict):
        reasons = " ".join(e.get("reason", "") for e in error.get("errors", []))
        return f"{error.get('message', '')} {reasons}".strip()
    return str(error)[:200]


def parse_google_datetime(raw: str):
    """An RFC 3339 timestamp from Google into an aware datetime.

    ``fromisoformat`` handles the "Z" suffix from Python 3.11 on, which is the
    only form Google's freeBusy uses. Kept as a named function so the one place
    that parses Google's timestamps is greppable.
    """
    from datetime import datetime

    return datetime.fromisoformat(raw)
