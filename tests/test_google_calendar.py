"""
The Google Calendar integration.

Nothing here touches the network. ``requests`` is called from exactly three
places in the package — ``client._request``, ``credentials._refresh`` /
``revoke_at_google``, and ``oauth.finish`` — so a fake installed over those is a
complete seam, and a test that starts reaching Google would show up as a
connection error rather than passing quietly against a live calendar.

Four groups of promise, in descending order of how badly a regression would hurt:

  * **A booking survives Google.** Google being down, rate limiting, or refusing
    an expired grant must never undo an appointment that was made and emailed.
    Every failure class is asserted against a real ``services.book``.
  * **Only the time leaves this system.** No attendees, no notes, no case label,
    and no counselee name unless the counselor has explicitly asked for it. A
    Google calendar is outside the four-role access model entirely.
  * **A refresh token is never stored in the clear**, and a sealed token is bound
    to its row.
  * **An admin cannot arrange where a counselor's appointments are sent.** That
    row of the access matrix lives in tests/test_access_matrix.py; the predicate
    behind it is asserted here.
"""

import base64
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.models import Case, CaseMember, CounselorProfile
from apps.documents import crypto
from apps.scheduling import services
from apps.scheduling.google import credentials, oauth, sync
from apps.scheduling.google.client import BOOKING_ID_PROPERTY, CalendarClient
from apps.scheduling.google.errors import (
    AuthorizationLost,
    EventGone,
    GoogleNotConfigured,
    GoogleRefused,
    TransientGoogleError,
)
from apps.scheduling.models import (
    AvailabilityRule,
    Booking,
    BookingStatus,
    GoogleCredential,
    Weekday,
)

pytestmark = pytest.mark.django_db


# --- fakes ----------------------------------------------------------------


class FakeResponse:
    """Enough of a ``requests.Response`` for the two things this package reads.

    ``payload=None`` means "not JSON", which is a real Google failure mode — an
    HTML error page from a proxy in front of the API — and one the taxonomy has to
    survive rather than raise ``ValueError`` out of.
    """

    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    @property
    def content(self):
        return self.text.encode()

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class Transport:
    """Records the calls made and hands back queued responses.

    A queue rather than one canned response, because the two behaviours most
    worth pinning down — the single 401 retry, and *not* refreshing a token that
    is still good — are both about how many calls happen.
    """

    def __init__(self, *responses):
        self.queued = list(responses)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(SimpleNamespace(args=args, kwargs=kwargs))
        if not self.queued:
            raise AssertionError("the code made more HTTP calls than the test queued")
        response = self.queued.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def count(self):
        return len(self.calls)


def id_token_for(claims: dict) -> str:
    """A JWT-shaped string carrying these claims.

    Unsigned, because ``oauth._id_token_claims`` deliberately does not verify a
    token it received directly from Google's token endpoint over TLS. A test that
    signed it would be asserting something the code does not do.
    """
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


# --- fixtures -------------------------------------------------------------


@pytest.fixture
def google_on(settings):
    """The integration configured, as a ministry with a Workspace domain has it."""
    settings.GOOGLE_CALENDAR_ENABLED = True
    settings.GOOGLE_OAUTH_CLIENT_ID = "client-id.apps.googleusercontent.com"
    settings.GOOGLE_OAUTH_CLIENT_SECRET = "client-secret"
    settings.GOOGLE_WORKSPACE_DOMAIN = "ministry.example.org"
    settings.SITE_BASE_URL = "https://counseling.example.org"
    return settings


@pytest.fixture
def credential(counselor, google_on):
    return credentials.store_tokens(
        counselor=counselor,
        refresh_token="refresh-token-from-google",
        access_token="access-token-from-google",
        expires_in=3600,
        google_email="counselor@ministry.example.org",
    )


@pytest.fixture
def practice(counselor, counselee):
    """A counselor with hours all week, one case, one counselee on it.

    The counselee is given a real-looking name rather than the fixture default,
    because half the assertions in this file are that a particular string does
    *not* appear in what leaves the system — and "Counselee 3" would pass those
    tests by being unlikely rather than by being withheld.
    """
    counselee.first_name = "Naomi"
    counselee.last_name = "Ashford"
    counselee.save(update_fields=["first_name", "last_name"])
    CounselorProfile.objects.create(user=counselor)
    for weekday in Weekday.values:
        AvailabilityRule.objects.create(
            counselor=counselor,
            weekday=weekday,
            start_time="09:00",
            end_time="17:00",
            slot_minutes=60,
        )
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
    CaseMember.objects.create(case=case, counselee=counselee)
    return SimpleNamespace(case=case, counselor=counselor, counselee=counselee)


@pytest.fixture
def booking(practice, counselee):
    """A confirmed appointment three days out, made without touching Google."""
    now = timezone.now().replace(minute=0, second=0, microsecond=0)
    return Booking.objects.create(
        counselor=practice.counselor,
        case=practice.case,
        counselee=counselee,
        slot=Booking.range_for(now + timedelta(days=3), 60),
        status=BookingStatus.CONFIRMED,
        created_by=practice.counselor,
    )


@pytest.fixture
def no_token_refresh(monkeypatch):
    """Hand the client a token without an exchange.

    The client's job is turning status codes into the error taxonomy; making every
    one of those tests also stub a token endpoint would be noise, and would hide
    which call count belongs to which layer.
    """
    monkeypatch.setattr(credentials, "access_token", lambda credential: "an-access-token")


# --- sealing the refresh token --------------------------------------------


class TestSealingTokens:
    def test_the_refresh_token_round_trips(self, credential):
        assert credentials.refresh_token_of(credential) == "refresh-token-from-google"

    def test_no_column_on_the_row_holds_the_token_in_the_clear(self, credential):
        """The point of the whole envelope: a database dump is not calendar access."""
        credential.refresh_from_db()
        raw = b"".join(
            bytes(value or b"")
            for value in (
                credential.wrapped_dek,
                credential.dek_nonce,
                credential.refresh_token_sealed,
                credential.access_token_sealed,
            )
        )

        assert b"refresh-token-from-google" not in raw
        assert b"access-token-from-google" not in raw

    def test_a_sealed_token_moved_to_another_row_will_not_open(
        self, credential, other_counselor, google_on
    ):
        """The storage key is the AAD, so ciphertext is bound to the row it was written for.

        Worth asserting rather than trusting: without the binding, an attacker with
        UPDATE on this table could copy one counselor's sealed grant onto their own
        row and read another counselor's calendar with it.
        """
        theirs = credentials.store_tokens(
            counselor=other_counselor,
            refresh_token="somebody-elses-token",
            google_email="other@ministry.example.org",
        )
        # Everything needed to open it, moved wholesale — DEK, nonce, ciphertext.
        theirs.wrapped_dek = credential.wrapped_dek
        theirs.dek_nonce = credential.dek_nonce
        theirs.refresh_token_sealed = credential.refresh_token_sealed
        theirs.save()

        with pytest.raises(crypto.DecryptionError):
            credentials.refresh_token_of(theirs)

    def test_a_refresh_response_with_no_refresh_token_keeps_the_stored_one(self, credential):
        """Google returns a refresh token only on first authorization.

        Overwriting it with the empty string would break the connection silently,
        and the symptom would surface an hour later in a cron log.
        """
        credentials.store_tokens(
            counselor=credential.counselor,
            access_token="a-newer-access-token",
            expires_in=3600,
        )
        credential.refresh_from_db()

        assert credentials.refresh_token_of(credential) == "refresh-token-from-google"

    def test_a_connection_cannot_be_created_without_a_refresh_token(self, counselor, google_on):
        """ "Connected" with nothing to refresh is worse than a visible failure."""
        with pytest.raises(AuthorizationLost):
            credentials.store_tokens(counselor=counselor, access_token="only-an-access-token")

        assert not GoogleCredential.objects.filter(counselor=counselor).exists()

    def test_re_keying_drops_the_cached_access_token(self, credential):
        """It was sealed under the old DEK, so keeping it would only fail later."""
        credentials.store_tokens(
            counselor=credential.counselor, refresh_token="a-replacement-refresh-token"
        )
        credential.refresh_from_db()

        assert not bytes(credential.access_token_sealed or b"")
        assert credential.access_token_expires_at is None

    def test_marking_a_connection_revoked_destroys_the_ciphertext(self, credential):
        credentials.mark_revoked(credential, reason="Google said no.")
        credential.refresh_from_db()

        assert not bytes(credential.refresh_token_sealed or b"")
        assert credential.is_usable is False
        assert credential.revoked_at is not None


# --- getting a usable access token ----------------------------------------


class TestAccessTokens:
    def test_a_cached_token_is_used_without_contacting_google(self, credential, monkeypatch):
        transport = Transport()
        monkeypatch.setattr(credentials.requests, "post", transport)

        assert credentials.access_token(credential) == "access-token-from-google"
        assert transport.count == 0

    def test_a_token_about_to_expire_is_refreshed_early(self, credential, monkeypatch, settings):
        """The leeway is the point: a token with two seconds left fails a three-second call."""
        settings.GOOGLE_TOKEN_LEEWAY_SECONDS = 90
        credential.access_token_expires_at = timezone.now() + timedelta(seconds=30)
        credential.save(update_fields=["access_token_expires_at"])
        transport = Transport(
            FakeResponse(200, {"access_token": "a-fresh-one", "expires_in": 3600})
        )
        monkeypatch.setattr(credentials.requests, "post", transport)

        assert credentials.access_token(credential) == "a-fresh-one"
        assert transport.count == 1

    def test_a_refreshed_token_is_sealed_and_reused(self, credential, monkeypatch):
        credential.access_token_expires_at = timezone.now() - timedelta(hours=1)
        credential.save(update_fields=["access_token_expires_at"])
        transport = Transport(
            FakeResponse(200, {"access_token": "a-fresh-one", "expires_in": 3600})
        )
        monkeypatch.setattr(credentials.requests, "post", transport)

        credentials.access_token(credential)
        credential.refresh_from_db()

        # Second call, no further HTTP: the queue is empty and Transport would raise.
        assert credentials.access_token(credential) == "a-fresh-one"

    def test_an_invalid_grant_revokes_the_connection_and_says_so(self, credential, monkeypatch):
        """The terminal case. Recorded on the row, because only the counselor can fix it."""
        credential.access_token_expires_at = None
        credential.save(update_fields=["access_token_expires_at"])
        monkeypatch.setattr(
            credentials.requests,
            "post",
            Transport(FakeResponse(400, {"error": "invalid_grant"})),
        )

        with pytest.raises(AuthorizationLost):
            credentials.access_token(credential)

        credential.refresh_from_db()
        assert credential.is_usable is False
        assert "reconnect" in credential.last_error.lower()

    @pytest.mark.parametrize(
        "response",
        [
            FakeResponse(503, text="Service Unavailable"),
            FakeResponse(400, {"error": "temporarily_unavailable"}),
            FakeResponse(200, {"no_token_at_all": True}),
        ],
    )
    def test_a_recoverable_refusal_leaves_the_connection_alone(
        self, credential, monkeypatch, response
    ):
        """A retryable failure must not mark a working grant dead."""
        credential.access_token_expires_at = None
        credential.save(update_fields=["access_token_expires_at"])
        monkeypatch.setattr(credentials.requests, "post", Transport(response))

        with pytest.raises(TransientGoogleError):
            credentials.access_token(credential)

        credential.refresh_from_db()
        assert credential.is_usable is True

    def test_an_unreachable_google_is_transient_not_terminal(self, credential, monkeypatch):
        import requests as requests_module

        credential.access_token_expires_at = None
        credential.save(update_fields=["access_token_expires_at"])
        monkeypatch.setattr(
            credentials.requests,
            "post",
            Transport(requests_module.ConnectionError("no route to host")),
        )

        with pytest.raises(TransientGoogleError):
            credentials.access_token(credential)

    def test_an_unconfigured_server_refuses_before_reaching_for_the_network(
        self, credential, settings
    ):
        settings.GOOGLE_CALENDAR_ENABLED = False
        credential.access_token_expires_at = None
        credential.save(update_fields=["access_token_expires_at"])

        with pytest.raises(GoogleNotConfigured):
            credentials.access_token(credential)


# --- the consent handshake ------------------------------------------------


class TestTheStateParameter:
    def test_the_state_is_one_shot(self, google_on):
        """A replayed callback must fail even when it carries the right value."""
        session = {}
        url = oauth.start(session)
        state = session[oauth.STATE_SESSION_KEY]
        assert f"state={state}" in url

        assert oauth.state_is_valid(session, state) is True
        assert oauth.state_is_valid(session, state) is False

    def test_a_wrong_or_missing_state_is_refused(self, google_on):
        session = {}
        oauth.start(session)

        assert oauth.state_is_valid(session, "not-the-state") is False
        assert oauth.state_is_valid({}, "anything") is False
        assert oauth.state_is_valid({oauth.STATE_SESSION_KEY: "x"}, "") is False

    def test_the_consent_url_asks_for_offline_access_and_nothing_broad(self, google_on):
        url = oauth.start({})

        # Without access_type=offline there is no refresh token, and the connection
        # stops working within the hour.
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        assert "calendar.events" in url
        # Never the full calendar scope: reading the titles of a counselor's other
        # appointments is not something this application has any use for.
        assert "auth%2Fcalendar&" not in url
        assert "calendar.readonly" not in url


class TestTheHostedDomainCheck:
    def _exchange(self, monkeypatch, claims, **body):
        payload = {
            "refresh_token": "a-refresh-token",
            "access_token": "an-access-token",
            "expires_in": 3600,
            "id_token": id_token_for(claims),
            "scope": " ".join(oauth.SCOPES),
            **body,
        }
        monkeypatch.setattr(oauth.requests, "post", Transport(FakeResponse(200, payload)))

    def test_a_ministry_account_is_accepted(self, counselor, google_on, monkeypatch):
        self._exchange(
            monkeypatch,
            {"hd": "ministry.example.org", "email": "counselor@ministry.example.org"},
        )

        credential = oauth.finish(counselor=counselor, code="an-auth-code")

        assert credential.google_email == "counselor@ministry.example.org"
        assert credentials.refresh_token_of(credential) == "a-refresh-token"

    def test_a_personal_account_is_refused_and_stores_nothing(
        self, counselor, google_on, monkeypatch
    ):
        """The whole reason GOOGLE_WORKSPACE_DOMAIN is load-bearing.

        A counselor signed into a personal account in the same browser would
        otherwise end up with ministry appointment times in a calendar the ministry
        cannot administer, audit, or revoke.
        """
        self._exchange(monkeypatch, {"email": "someone@gmail.com"})

        with pytest.raises(oauth.DomainNotAllowed):
            oauth.finish(counselor=counselor, code="an-auth-code")

        assert not GoogleCredential.objects.exists()

    def test_an_account_on_a_lookalike_domain_is_refused(self, counselor, google_on, monkeypatch):
        """Matched on the ``hd`` claim, not the address suffix.

        An attacker who controls ministry.example.org.evil.test can make an address
        that ends the right way; only Google's own assertion about which
        organization owns the account is worth anything.
        """
        self._exchange(
            monkeypatch,
            {
                "hd": "ministry.example.org.evil.test",
                "email": "counselor@ministry.example.org.evil.test",
            },
        )

        with pytest.raises(oauth.DomainNotAllowed):
            oauth.finish(counselor=counselor, code="an-auth-code")

    def test_an_unverified_address_is_refused(self, counselor, google_on, monkeypatch):
        self._exchange(
            monkeypatch,
            {
                "hd": "ministry.example.org",
                "email": "counselor@ministry.example.org",
                "email_verified": False,
            },
        )

        with pytest.raises(oauth.DomainNotAllowed):
            oauth.finish(counselor=counselor, code="an-auth-code")

    def test_a_stale_authorization_code_is_a_refusal_not_a_retry(
        self, counselor, google_on, monkeypatch
    ):
        """Codes are single-use, so retrying one is pointless."""
        monkeypatch.setattr(
            oauth.requests,
            "post",
            Transport(FakeResponse(400, {"error": "invalid_grant"})),
        )

        with pytest.raises(GoogleRefused):
            oauth.finish(counselor=counselor, code="already-used")


# --- turning status codes into decisions ----------------------------------


class TestTheErrorTaxonomy:
    """``client._interpret`` is the only place a Google status code is read.

    Every branch is asserted, because the difference between "retry next tick",
    "give up", and "make the counselor reconnect" is the difference between a
    self-healing integration and one that either nags or silently stops.
    """

    @pytest.mark.parametrize(
        ("status", "payload", "expected"),
        [
            (404, None, EventGone),
            (410, None, EventGone),
            (412, None, EventGone),
            (429, None, TransientGoogleError),
            (500, None, TransientGoogleError),
            (503, None, TransientGoogleError),
            (400, {"error": {"message": "Bad Request"}}, GoogleRefused),
            (
                403,
                {"error": {"message": "Rate Limit", "errors": [{"reason": "rateLimitExceeded"}]}},
                TransientGoogleError,
            ),
            (
                403,
                {
                    "error": {
                        "message": "Forbidden",
                        "errors": [{"reason": "insufficientPermissions"}],
                    }
                },
                GoogleRefused,
            ),
        ],
    )
    def test_each_status_maps_to_one_decision(
        self, credential, monkeypatch, no_token_refresh, status, payload, expected
    ):
        client = CalendarClient(credential)
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            Transport(FakeResponse(status, payload)),
        )

        with pytest.raises(expected):
            client.insert({"summary": "x"})

    def test_a_401_is_retried_exactly_once(self, credential, monkeypatch, no_token_refresh):
        """A token good by our clock and refused by Google is a clock disagreement.

        One forced refresh fixes it. A second 401 is a real authorization failure,
        and looping on it is how an application earns a rate limit.
        """
        transport = Transport(
            FakeResponse(401, {"error": {"message": "Invalid Credentials"}}),
            FakeResponse(200, {"id": "event-1", "etag": "etag-1"}),
        )
        monkeypatch.setattr("apps.scheduling.google.client.requests.request", transport)

        written = CalendarClient(credential).insert({"summary": "x"})

        assert written["id"] == "event-1"
        assert transport.count == 2

    def test_a_second_401_revokes_the_connection(self, credential, monkeypatch, no_token_refresh):
        transport = Transport(
            FakeResponse(401, {"error": {"message": "Invalid Credentials"}}),
            FakeResponse(401, {"error": {"message": "Invalid Credentials"}}),
        )
        monkeypatch.setattr("apps.scheduling.google.client.requests.request", transport)

        with pytest.raises(AuthorizationLost):
            CalendarClient(credential).insert({"summary": "x"})

        credential.refresh_from_db()
        assert credential.is_usable is False

    def test_a_204_with_no_body_is_success(self, credential, monkeypatch, no_token_refresh):
        """What a successful DELETE returns."""
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            Transport(FakeResponse(204)),
        )

        assert CalendarClient(credential).delete("event-1") is None

    def test_a_non_json_success_is_transient_rather_than_a_crash(
        self, credential, monkeypatch, no_token_refresh
    ):
        """A proxy in front of the API returning HTML must not raise ValueError."""
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            Transport(FakeResponse(200, text="<html>gateway</html>")),
        )

        with pytest.raises(TransientGoogleError):
            CalendarClient(credential).insert({"summary": "x"})

    def test_an_etag_is_sent_as_if_match(self, credential, monkeypatch, no_token_refresh):
        """So a write is refused if the counselor moved the event since we wrote it."""
        transport = Transport(FakeResponse(200, {"id": "event-1", "etag": "etag-2"}))
        monkeypatch.setattr("apps.scheduling.google.client.requests.request", transport)

        CalendarClient(credential).update("event-1", {"summary": "x"}, etag='"etag-1"')

        assert transport.calls[0].kwargs["headers"]["If-Match"] == '"etag-1"'


class TestFreeBusy:
    def test_busy_periods_come_back_as_bare_intervals(
        self, credential, monkeypatch, no_token_refresh
    ):
        """No titles, no attendees, no ids — the endpoint cannot return them.

        That is why freeBusy is the endpoint used rather than events.list: reading
        the counselor's actual diary would mean this application holding the
        contents of their personal calendar to answer "busy or not".
        """
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            Transport(
                FakeResponse(
                    200,
                    {
                        "calendars": {
                            "primary": {
                                "busy": [
                                    {
                                        "start": "2026-03-02T14:00:00Z",
                                        "end": "2026-03-02T15:00:00Z",
                                    }
                                ]
                            }
                        }
                    },
                )
            ),
        )
        start = timezone.now()

        busy = CalendarClient(credential).freebusy(start=start, end=start + timedelta(days=1))

        assert len(busy) == 1
        assert busy[0][0] < busy[0][1]
        assert busy[0][0].tzinfo is not None

    def test_a_calendar_google_will_not_report_on_is_not_treated_as_free(
        self, credential, monkeypatch, no_token_refresh
    ):
        """Offering a slot because we could not check is how two people get one hour."""
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            Transport(
                FakeResponse(
                    200,
                    {"calendars": {"primary": {"errors": [{"reason": "notFound"}]}}},
                )
            ),
        )
        start = timezone.now()

        with pytest.raises(GoogleRefused):
            CalendarClient(credential).freebusy(start=start, end=start + timedelta(days=1))


# --- what actually gets written -------------------------------------------


class TestWhatTheEventSays:
    def test_no_counseling_content_leaves_the_system(self, booking, credential):
        """The access model stops at the API boundary, so the safe amount is none."""
        booking.request_note = "I have been having thoughts of self-harm."
        booking.counselor_note = "Discussed the events of 12 March."
        booking.save()

        body = json.dumps(sync.event_body(booking, credential=credential))

        assert "self-harm" not in body
        assert "12 March" not in body
        assert booking.case.label not in body

    def test_nobody_is_invited(self, booking, credential):
        """A Google attendee would be emailed an invitation by Google.

        That puts a counseling appointment into whatever calendar their address
        belongs to — often a shared family or employer one — and exposes their
        address to everyone who can see the counselor's event.
        """
        body = sync.event_body(booking, credential=credential)

        assert "attendees" not in body
        assert body["guestsCanInviteOthers"] is False
        assert booking.counselee.email not in json.dumps(body)

    def test_the_counselee_is_not_named_by_default(self, booking, credential, settings):
        settings.GOOGLE_EVENT_TITLE = "Counseling appointment"

        body = sync.event_body(booking, credential=credential)

        assert body["summary"] == "Counseling appointment"
        assert booking.counselee.last_name not in json.dumps(body)

    def test_a_counselor_can_ask_for_names(self, booking, credential):
        """Off by default, and their decision to make. Not an admin's."""
        credential.include_names = True

        body = sync.event_body(booking, credential=credential)

        assert booking.counselee.full_name in body["summary"]

    def test_an_unconfirmed_request_is_marked_tentative(self, booking, credential):
        """So a glance at the calendar distinguishes an appointment from an ask."""
        booking.status = BookingStatus.REQUESTED

        assert sync.event_body(booking, credential=credential)["status"] == "tentative"
        booking.status = BookingStatus.CONFIRMED
        assert sync.event_body(booking, credential=credential)["status"] == "confirmed"

    def test_the_event_is_private_busy_and_tagged_as_ours(self, booking, credential):
        body = sync.event_body(booking, credential=credential)

        assert body["visibility"] == "private"
        assert body["transparency"] == "opaque"
        assert body["extendedProperties"]["private"][BOOKING_ID_PROPERTY] == str(booking.pk)

    def test_the_description_is_a_link_and_nothing_else(self, booking, credential, google_on):
        body = sync.event_body(booking, credential=credential)

        expected = "https://counseling.example.org" + reverse(
            "scheduling:detail", kwargs={"pk": booking.pk}
        )
        assert expected in body["description"]
        # Nothing else. The link is there so a counselor's route from "10am on my
        # phone" to "who is this" is one tap, through a page that still checks
        # for_actor and the permission.
        assert body["description"].strip() == f"Booked in bcTracker.\n{expected}"


class TestPushingOneBooking:
    def test_an_insert_records_the_event_id_and_etag(self, booking, credential, monkeypatch):
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            Transport(FakeResponse(200, {"id": "event-1", "etag": '"etag-1"'})),
        )
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")

        assert sync.push_booking(booking) is True

        booking.refresh_from_db()
        assert booking.google_event_id == "event-1"
        assert booking.google_etag == '"etag-1"'
        assert booking.google_synced_at is not None

    def test_a_cancellation_removes_the_event(self, booking, credential, monkeypatch):
        booking.google_event_id = "event-1"
        booking.status = BookingStatus.CANCELLED
        # A check constraint refuses a cancelled booking with no cancelled_at, which
        # is why this is set rather than left to the status alone.
        booking.cancelled_at = timezone.now()
        booking.save()
        transport = Transport(FakeResponse(204))
        monkeypatch.setattr("apps.scheduling.google.client.requests.request", transport)
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")

        sync.push_booking(booking)

        assert transport.calls[0].args[0] == "DELETE"
        booking.refresh_from_db()
        assert booking.google_event_id == ""

    @pytest.mark.parametrize("status", [BookingStatus.COMPLETED, BookingStatus.NO_SHOW])
    def test_a_session_that_happened_stays_in_the_calendar(
        self, booking, credential, monkeypatch, status
    ):
        """Not "anything inactive". Deleting these rewrites where the week went.

        A no-show in particular is a fact somebody may need to point at later, and
        it should not vanish from the counselor's own calendar.
        """
        booking.google_event_id = "event-1"
        booking.status = status
        booking.save()
        transport = Transport(FakeResponse(200, {"id": "event-1", "etag": '"etag-2"'}))
        monkeypatch.setattr("apps.scheduling.google.client.requests.request", transport)
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")

        sync.push_booking(booking)

        assert transport.calls[0].args[0] == "PUT"
        booking.refresh_from_db()
        assert booking.google_event_id == "event-1"

    def test_an_event_deleted_in_google_is_put_back(self, booking, credential, monkeypatch):
        """bcTracker is authoritative. A counselor tidying up must not lose the hour."""
        booking.google_event_id = "event-gone"
        booking.save()
        transport = Transport(
            FakeResponse(410),
            FakeResponse(200, {"id": "event-2", "etag": '"etag-2"'}),
        )
        monkeypatch.setattr("apps.scheduling.google.client.requests.request", transport)
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")

        sync.push_booking(booking)

        assert [call.args[0] for call in transport.calls] == ["PUT", "POST"]
        booking.refresh_from_db()
        assert booking.google_event_id == "event-2"

    def test_a_transient_failure_is_silent_and_leaves_no_banner(
        self, booking, credential, monkeypatch
    ):
        """ "Sync failed" for one timeout trains counselors to ignore the banner."""
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request", Transport(FakeResponse(503))
        )
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")

        assert sync.push_booking(booking) is False

        credential.refresh_from_db()
        assert credential.last_error == ""

    def test_a_refusal_is_shown_to_the_counselor(self, booking, credential, monkeypatch):
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            Transport(
                FakeResponse(
                    403,
                    {"error": {"message": "no", "errors": [{"reason": "insufficientPermissions"}]}},
                )
            ),
        )
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")

        assert sync.push_booking(booking) is False

        credential.refresh_from_db()
        assert credential.last_error != ""

    def test_a_lost_authorization_is_audited(self, booking, credential, monkeypatch):
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            Transport(FakeResponse(401), FakeResponse(401)),
        )
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")

        sync.push_booking(booking)

        assert AuditEvent.objects.filter(verb=AuditVerb.GOOGLE_DISCONNECTED).exists()

    def test_nothing_is_pushed_when_the_integration_is_off(self, booking, credential, settings):
        settings.GOOGLE_CALENDAR_ENABLED = False

        assert sync.credential_for(booking.counselor) is None
        # No transport installed, so a call would raise rather than pass quietly.
        assert sync.push_booking(booking) is False

    def test_nothing_is_pushed_for_a_revoked_connection(self, booking, credential):
        credentials.mark_revoked(credential, reason="Google said no.")

        assert sync.credential_for(booking.counselor) is None
        assert sync.push_booking(booking) is False


class TestWhatNeedsPushing:
    def test_a_booking_never_sent_is_pending(self, booking, credential):
        assert booking in list(sync.bookings_needing_push(booking.counselor))

    def test_a_booking_already_sent_is_not_sent_again(self, booking, credential, monkeypatch):
        """``queryset.update`` does not touch ``auto_now``, which is what makes
        ``google_synced_at < updated_at`` an exact staleness test rather than one
        that re-dirties the row it just cleaned."""
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            Transport(FakeResponse(200, {"id": "event-1", "etag": '"etag-1"'})),
        )
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")
        sync.push_booking(booking)

        assert list(sync.bookings_needing_push(booking.counselor)) == []

    def test_a_change_after_a_push_makes_it_pending_again(self, booking, credential):
        booking.google_synced_at = timezone.now() - timedelta(minutes=5)
        Booking.objects.filter(pk=booking.pk).update(google_synced_at=booking.google_synced_at)
        booking.request_note = "Running ten minutes late."
        booking.save()

        assert booking in list(sync.bookings_needing_push(booking.counselor))

    def test_a_cancellation_still_holding_an_event_id_is_pending(self, booking, credential):
        """The case that makes the integration trustworthy.

        Missing it leaves a cancelled session on the counselor's phone, and they
        turn up for it.
        """
        # Synced *after* it was last changed, so the staleness test alone would say
        # there is nothing to do. Holding an event id is what makes it pending.
        Booking.objects.filter(pk=booking.pk).update(
            status=BookingStatus.CANCELLED,
            cancelled_at=timezone.now(),
            google_event_id="event-1",
            google_synced_at=timezone.now() + timedelta(minutes=5),
        )

        pending = list(sync.bookings_needing_push(booking.counselor))
        assert [b.pk for b in pending] == [booking.pk]

    def test_a_cancellation_with_no_event_is_left_alone(self, booking, credential):
        Booking.objects.filter(pk=booking.pk).update(
            status=BookingStatus.CANCELLED,
            cancelled_at=timezone.now(),
            google_synced_at=timezone.now(),
        )

        assert list(sync.bookings_needing_push(booking.counselor)) == []

    def test_last_month_is_not_rewritten(self, practice, counselee, credential):
        """Otherwise every cron tick does work proportional to the whole history."""
        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        old = Booking.objects.create(
            counselor=practice.counselor,
            case=practice.case,
            counselee=counselee,
            slot=Booking.range_for(now - timedelta(days=30), 60),
            status=BookingStatus.COMPLETED,
            created_by=practice.counselor,
        )

        assert old not in list(sync.bookings_needing_push(practice.counselor))

    def test_a_dead_grant_stops_the_run_rather_than_hammering_google(
        self, practice, counselee, credential, monkeypatch
    ):
        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        for days in (3, 4, 5):
            Booking.objects.create(
                counselor=practice.counselor,
                case=practice.case,
                counselee=counselee,
                slot=Booking.range_for(now + timedelta(days=days), 60),
                status=BookingStatus.CONFIRMED,
                created_by=practice.counselor,
            )
        transport = Transport(FakeResponse(401), FakeResponse(401))
        monkeypatch.setattr("apps.scheduling.google.client.requests.request", transport)
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")

        tally = sync.sync_counselor(practice.counselor)

        assert tally["authorization_lost"] is True
        assert tally["pushed"] == 0
        # Two calls for the first booking — the 401 and its one retry — and then it
        # stops, rather than three bookings' worth of failed refreshes.
        assert transport.count == 2


# --- reading free/busy back into the booking page -------------------------


class TestBusyTimeFromGoogle:
    @pytest.mark.parametrize(
        "response",
        [
            FakeResponse(503),
            FakeResponse(403, {"error": {"message": "no", "errors": [{"reason": "forbidden"}]}}),
            FakeResponse(200, {"calendars": {"primary": {"errors": [{"reason": "notFound"}]}}}),
            FakeResponse(200, text="<html>gateway</html>"),
        ],
    )
    def test_every_failure_means_no_busy_time_rather_than_no_slots(
        self, practice, credential, monkeypatch, response
    ):
        """Deliberately failing open, and the reasoning is worth restating.

        Treating an unreachable Google as "completely busy" empties the booking page
        and stops counselees booking at all — the worse outcome, and the one nobody
        diagnoses. Treating it as free risks offering a slot the counselor has
        something else in, which they can simply decline.
        """
        monkeypatch.setattr("apps.scheduling.google.client.requests.request", Transport(response))
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")
        start = timezone.now()

        assert (
            sync.busy_from_google(practice.counselor, start=start, end=start + timedelta(days=1))
            == []
        )

    def test_a_counselor_who_turned_it_off_is_not_queried(self, practice, credential, monkeypatch):
        credential.block_slots_from_calendar = False
        credential.save(update_fields=["block_slots_from_calendar"])
        start = timezone.now()

        # No transport installed: a call would raise, so this asserts none is made.
        assert (
            sync.busy_from_google(practice.counselor, start=start, end=start + timedelta(days=1))
            == []
        )

    def test_a_busy_hour_in_google_is_not_offered(self, practice, credential, monkeypatch):
        """The other half of the value: no double-booking across two calendars."""
        offered = services.bookable_slots(
            counselor=practice.counselor, counselee=practice.counselee, use_google=False
        )
        target = offered[0]
        monkeypatch.setattr(
            credentials,
            "access_token",
            lambda c: "a-token",
        )
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            lambda *args, **kwargs: FakeResponse(
                200,
                {
                    "calendars": {
                        "primary": {
                            "busy": [
                                {
                                    "start": target.start.isoformat(),
                                    "end": (target.start + timedelta(minutes=60)).isoformat(),
                                }
                            ]
                        }
                    }
                },
            ),
        )

        with_google = services.bookable_slots(
            counselor=practice.counselor, counselee=practice.counselee
        )

        assert target.start not in [slot.start for slot in with_google]

    def test_taking_a_slot_does_not_ask_google_again(self, practice, credential):
        """``is_bookable`` deliberately does not consult Google.

        A counselee has already been offered this time. Letting a third party
        retract it between the page and the POST would produce "that time is no
        longer available" for a slot that was available a second ago, and the
        counselee has no way to understand why.
        """
        target = services.bookable_slots(
            counselor=practice.counselor, counselee=practice.counselee, use_google=False
        )[0]

        # No transport installed, so any HTTP call raises rather than passing.
        assert services.is_bookable(
            counselor=practice.counselor,
            counselee=practice.counselee,
            start=target.start,
            minutes=target.minutes,
        )


# --- the promise that matters most ----------------------------------------


class TestABookingSurvivesGoogle:
    """An appointment made and emailed must not be undone by a calendar API.

    ``services._push_to_google`` catches ``Exception`` rather than ``GoogleError``
    for exactly this reason, so the parametrization includes a failure the taxonomy
    does not describe at all.
    """

    @pytest.mark.parametrize(
        "failure",
        [
            FakeResponse(503),
            FakeResponse(401),
            FakeResponse(403, {"error": {"message": "no", "errors": [{"reason": "forbidden"}]}}),
            RuntimeError("something nobody predicted"),
        ],
    )
    def test_booking_succeeds_when_google_does_not(
        self, practice, credential, monkeypatch, failure
    ):
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            lambda *args, **kwargs: (_ for _ in ()).throw(failure)
            if isinstance(failure, Exception)
            else failure,
        )
        target = services.bookable_slots(
            counselor=practice.counselor, counselee=practice.counselee, use_google=False
        )[0]

        booking = services.book(
            case=practice.case,
            counselee=practice.counselee,
            start=target.start,
            minutes=target.minutes,
            created_by=practice.counselee,
        )

        assert booking.pk is not None
        assert booking.status == BookingStatus.REQUESTED
        assert booking.google_event_id == ""

    def test_a_successful_booking_reaches_the_calendar(self, practice, credential, monkeypatch):
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            lambda *args, **kwargs: FakeResponse(200, {"id": "event-1", "etag": '"etag-1"'}),
        )
        target = services.bookable_slots(
            counselor=practice.counselor, counselee=practice.counselee, use_google=False
        )[0]

        booking = services.book(
            case=practice.case,
            counselee=practice.counselee,
            start=target.start,
            minutes=target.minutes,
            created_by=practice.counselee,
        )

        booking.refresh_from_db()
        assert booking.google_event_id == "event-1"


# --- the cron command -----------------------------------------------------


class TestTheSyncCommand:
    def _run(self, **options):
        import io

        from django.core.management import call_command

        out = io.StringIO()
        call_command("sync_google_calendar", stdout=out, stderr=out, **options)
        return out.getvalue()

    def test_a_ministry_without_google_gets_a_sentence_not_a_traceback(self, settings):
        settings.GOOGLE_CALENDAR_ENABLED = False

        assert "switched off" in self._run()

    def test_a_typo_in_an_address_does_not_sync_everybody(self, google_on, credential):
        """``_counselors`` returns None for "no such counselor" and Ellipsis for "all".

        Conflating the two would mean a mistyped ``--counselor`` silently walking
        every calendar in the ministry.
        """
        output = self._run(counselor="nobody@example.org")

        assert "No counselor with the address" in output

    def test_the_dry_run_names_no_counselee(self, booking, credential, google_on):
        """Cron output lands in a container log, which is outside the access model."""
        output = self._run(dry_run=True)

        assert "1 appointment(s) would be sent" in output
        assert booking.counselee.last_name not in output
        assert booking.counselee.email not in output

    def test_a_run_sends_what_is_pending(self, booking, credential, google_on, monkeypatch):
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            lambda *args, **kwargs: FakeResponse(200, {"id": "event-1", "etag": '"etag-1"'}),
        )

        output = self._run()

        assert "1 appointment(s) sent" in output
        booking.refresh_from_db()
        assert booking.google_event_id == "event-1"

    def test_a_dead_grant_does_not_fail_the_cron_job(
        self, booking, credential, google_on, monkeypatch
    ):
        """A job that exits non-zero on something self-healing is an alert nobody reads."""
        monkeypatch.setattr(credentials, "access_token", lambda c: "a-token")
        monkeypatch.setattr(
            "apps.scheduling.google.client.requests.request",
            lambda *args, **kwargs: FakeResponse(401),
        )

        output = self._run()

        assert "1 disconnected" in output.replace("disconnected.", "disconnected")


# --- who may connect a calendar -------------------------------------------


class TestOnlyACounselorConnectsACalendar:
    def test_a_counselor_may_manage_their_own(self, counselor):
        assert counselor.has_perm("scheduling.manage_own_google_calendar")

    @pytest.mark.parametrize("role_fixture", ["admin_user", "counselee", "financial_admin"])
    def test_nobody_else_may(self, request, role_fixture):
        """The admin exclusion is the deliberate one.

        Technically they have nothing to authorize — the consent flow runs in the
        counselor's own browser against their own Google account. And it should stay
        that way: an admin who could choose where a counselor's appointment times
        are sent could send them somewhere the counselor does not read.
        """
        user = request.getfixturevalue(role_fixture)

        assert not user.has_perm("scheduling.manage_own_google_calendar")


class TestTheSettingsPage:
    def test_a_counselor_sees_what_will_and_will_not_be_sent(
        self, counselor, credential, google_on, sign_in
    ):
        client = sign_in(counselor)

        response = client.get(reverse("scheduling:google_settings"))

        assert response.status_code == 200
        body = response.content.decode()
        assert "counselor@ministry.example.org" in body
        assert "no names, no notes" in body

    def test_switching_names_on_is_stated_plainly_and_audited(
        self, counselor, credential, google_on, sign_in
    ):
        """The consequence is on a lock screen, and this is the last place to say so."""
        client = sign_in(counselor)

        response = client.post(
            reverse("scheduling:google_settings"),
            {"calendar_id": "primary", "include_names": "on", "block_slots_from_calendar": "on"},
            follow=True,
        )

        assert "Counselee names <strong>are</strong> appearing" in response.content.decode()
        event = AuditEvent.objects.filter(verb=AuditVerb.GOOGLE_SETTINGS_CHANGED).get()
        assert event.metadata["include_names"] is True

    def test_connecting_needs_a_post(self, counselor, google_on, sign_in):
        """A GET would let an emailed link start a handshake and mint a state token."""
        client = sign_in(counselor)

        assert client.get(reverse("scheduling:google_connect")).status_code == 405

    def test_a_post_sends_the_counselor_to_google(self, counselor, google_on, sign_in):
        client = sign_in(counselor)

        response = client.post(reverse("scheduling:google_connect"))

        assert response.status_code == 302
        assert response["Location"].startswith(oauth.AUTH_URL)
        assert client.session[oauth.STATE_SESSION_KEY]

    def test_a_forged_callback_is_refused_and_recorded(self, counselor, google_on, sign_in):
        """No state in the session, so this is either CSRF or a stale tab.

        Without the check, a crafted callback carrying an attacker's authorization
        code would connect the counselor's account to the attacker's calendar — and
        every appointment they book would land in it.
        """
        client = sign_in(counselor)

        response = client.get(
            reverse("scheduling:google_callback"),
            {"code": "an-attackers-code", "state": "invented"},
        )

        assert response.status_code == 302
        assert not GoogleCredential.objects.exists()
        event = AuditEvent.objects.filter(verb=AuditVerb.GOOGLE_CONNECT_REFUSED).get()
        assert event.metadata["reason"] == "state_mismatch"

    def test_the_whole_handshake(self, counselor, google_on, sign_in, monkeypatch):
        client = sign_in(counselor)
        started = client.post(reverse("scheduling:google_connect"))
        assert started.status_code == 302
        state = client.session[oauth.STATE_SESSION_KEY]
        monkeypatch.setattr(
            oauth.requests,
            "post",
            Transport(
                FakeResponse(
                    200,
                    {
                        "refresh_token": "a-refresh-token",
                        "access_token": "an-access-token",
                        "expires_in": 3600,
                        "id_token": id_token_for(
                            {
                                "hd": "ministry.example.org",
                                "email": "counselor@ministry.example.org",
                            }
                        ),
                        "scope": " ".join(oauth.SCOPES),
                    },
                )
            ),
        )

        response = client.get(
            reverse("scheduling:google_callback"),
            {"code": "an-auth-code", "state": state},
        )

        assert response.status_code == 302
        credential = GoogleCredential.objects.get(counselor=counselor)
        assert credential.google_email == "counselor@ministry.example.org"
        assert credential.include_names is False
        assert AuditEvent.objects.filter(verb=AuditVerb.GOOGLE_CONNECTED).exists()

    def test_a_declined_consent_screen_is_not_an_error(self, counselor, google_on, sign_in):
        client = sign_in(counselor)
        client.post(reverse("scheduling:google_connect"))
        state = client.session[oauth.STATE_SESSION_KEY]

        response = client.get(
            reverse("scheduling:google_callback"),
            {"error": "access_denied", "state": state},
            follow=True,
        )

        assert not GoogleCredential.objects.exists()
        assert "was not connected" in response.content.decode()

    def test_disconnecting_destroys_the_credential(
        self, counselor, credential, google_on, sign_in, monkeypatch
    ):
        monkeypatch.setattr(credentials.requests, "post", Transport(FakeResponse(200)))
        client = sign_in(counselor)

        response = client.post(reverse("scheduling:google_disconnect"))

        assert response.status_code == 302
        assert not GoogleCredential.objects.filter(counselor=counselor).exists()
        assert AuditEvent.objects.filter(verb=AuditVerb.GOOGLE_DISCONNECTED).exists()

    def test_disconnecting_works_even_when_google_does_not_answer(
        self, counselor, credential, google_on, sign_in, monkeypatch
    ):
        """A counselor clicking disconnect must not stay connected because Google is slow."""
        import requests as requests_module

        monkeypatch.setattr(
            credentials.requests,
            "post",
            Transport(requests_module.ConnectionError("no route to host")),
        )
        client = sign_in(counselor)

        response = client.post(reverse("scheduling:google_disconnect"), follow=True)

        assert not GoogleCredential.objects.filter(counselor=counselor).exists()
        assert "Google did not confirm" in response.content.decode()

    def test_disconnecting_leaves_the_event_ids_alone(
        self, counselor, credential, booking, google_on, sign_in, monkeypatch
    ):
        """So reconnecting later updates the events rather than inserting a second set."""
        Booking.objects.filter(pk=booking.pk).update(google_event_id="event-1")
        monkeypatch.setattr(credentials.requests, "post", Transport(FakeResponse(200)))
        client = sign_in(counselor)

        client.post(reverse("scheduling:google_disconnect"))

        booking.refresh_from_db()
        assert booking.google_event_id == "event-1"

    def test_the_page_asks_an_unconfigured_server_to_be_configured(
        self, counselor, settings, sign_in
    ):
        settings.GOOGLE_CALENDAR_ENABLED = False
        client = sign_in(counselor)

        response = client.get(reverse("scheduling:google_settings"))

        assert "has not been set up on this server" in response.content.decode()

    def test_office_hours_offers_the_connection(self, counselor, google_on, sign_in):
        """Here rather than in account settings: it changes which hours are offered."""
        client = sign_in(counselor)

        response = client.get(reverse("scheduling:availability"))

        assert reverse("scheduling:google_settings") in response.content.decode()


# --- refusing a half-configured deployment --------------------------------


class TestTheDeployChecks:
    def _run(self):
        from django.core.checks import run_checks

        return {problem.id for problem in run_checks(include_deployment_checks=True)}

    def test_an_integration_left_off_raises_nothing(self, settings):
        settings.GOOGLE_CALENDAR_ENABLED = False

        assert not {"scheduling.E001", "scheduling.E002", "scheduling.E003"} & self._run()

    def test_switching_it_on_without_a_client_is_refused(self, settings):
        settings.GOOGLE_CALENDAR_ENABLED = True
        settings.GOOGLE_OAUTH_CLIENT_ID = ""
        settings.GOOGLE_OAUTH_CLIENT_SECRET = ""

        assert "scheduling.E001" in self._run()

    def test_switching_it_on_without_a_workspace_domain_is_refused(self, google_on):
        """The check the settings comment promises, and the strongest of the three.

        With no ``hd`` claim to compare against, any Google account can be
        authorized — including a personal one the ministry cannot audit or revoke.
        """
        google_on.GOOGLE_WORKSPACE_DOMAIN = ""

        assert "scheduling.E002" in self._run()

    def test_a_development_site_url_is_refused(self, google_on):
        """The redirect URI is built from it and must match Google's registration."""
        google_on.SITE_BASE_URL = "https://localhost"

        assert "scheduling.E003" in self._run()

    def test_a_fully_configured_integration_passes(self, google_on):
        assert not {"scheduling.E001", "scheduling.E002", "scheduling.E003"} & self._run()
