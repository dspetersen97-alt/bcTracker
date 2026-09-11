"""
Shared pytest fixtures.

Tests run against a real Postgres instance — see config/settings/test.py for why
there is no SQLite mode.
"""

import time
from types import SimpleNamespace

import pytest
from django.contrib.auth import get_user_model

from apps.accounts.models import Role

User = get_user_model()

#: The password every fixture user is given. Long enough to satisfy the 12-char
#: validator, so tests exercise the same rules production does.
TEST_PASSWORD = "test-password-not-real"


def jpeg_bytes(size=(16, 16), colour=(200, 190, 170), **save_kwargs) -> bytes:
    """A real JPEG, small enough to be cheap.

    Generated rather than committed as a fixture file, so a test can ask for one
    carrying EXIF or an awkward orientation without a binary blob in the repo.
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG", **save_kwargs)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _document_store(settings, tmp_path):
    """Point the encrypted document store at a per-test temporary directory.

    autouse so no test can accidentally write counselee documents into the
    developer's real store.
    """
    store = tmp_path / "documents"
    store.mkdir()
    settings.DOCUMENT_STORE_ROOT = store
    return store


@pytest.fixture
def make_user(db):
    """Create a user with a given role.

    Counts up an email suffix so callers never have to invent unique addresses.
    """
    counter = {"n": 0}

    def _make(role=Role.COUNSELEE, **kwargs):
        counter["n"] += 1
        kwargs.setdefault("email", f"{role}{counter['n']}@example.org")
        kwargs.setdefault("first_name", role.capitalize())
        kwargs.setdefault("last_name", str(counter["n"]))
        password = kwargs.pop("password", TEST_PASSWORD)
        return User.objects.create_user(role=role, password=password, **kwargs)

    return _make


@pytest.fixture
def admin_user(make_user):
    return make_user(Role.ADMIN)


@pytest.fixture
def counselor(make_user):
    return make_user(Role.COUNSELOR)


@pytest.fixture
def other_counselor(make_user):
    """A second counselor, used to prove cross-counselor isolation."""
    return make_user(Role.COUNSELOR)


@pytest.fixture
def financial_admin(make_user):
    return make_user(Role.FINANCIAL_ADMIN)


@pytest.fixture
def counselee(make_user):
    return make_user(Role.COUNSELEE, allow_magic_link=True)


@pytest.fixture
def superuser(db):
    """The break-glass account. Distinct from the product's ADMIN role."""
    return User.objects.create_superuser(
        email="breakglass@example.org",
        password=TEST_PASSWORD,
    )


# --- authenticated clients ------------------------------------------------


@pytest.fixture
def enrol_totp(db):
    """Give a user a confirmed TOTP device and return ``(device, code_fn)``.

    ``code_fn()`` produces a currently-valid six-digit code, so tests drive the
    real ``verify_token`` path rather than stubbing it out. django-otp refuses a
    code it has already seen, so each call advances past the last used step.
    """
    from django_otp.oath import TOTP
    from django_otp.plugins.otp_totp.models import TOTPDevice

    def _enrol(user):
        device = TOTPDevice.objects.create(user=user, name="primary", confirmed=True)

        def code():
            totp = TOTP(device.bin_key, device.step, device.t0, device.digits)
            totp.time = time.time()
            return f"{totp.token():0{device.digits}d}"

        return device, code

    return _enrol


@pytest.fixture
def scenario(db, make_user):
    """One case with one counselee on it, and one of everything hanging off it.

    If the actor is a counselor the case is theirs; if they are a counselee they
    are on it. That makes a single set of objects usable for every row of the
    access matrix: the actor is legitimately connected to the case, so a refusal
    is about the *action* rather than about not being able to see the row. The
    cross-actor cases — another counselor's case, another counselee's profile —
    are asserted separately, where the point is the 404.

    The document is stored through the real service, encryption and all, so the
    download row of the matrix asserts a genuine 200 rather than a 404 from a
    missing blob. It is owned by the counselee, which is the ownership that makes
    every actor's expected status meaningful: the counselee acting is its
    uploader, the counselor acting has it on their case, and financial_admin is
    the one actor for whom it does not exist. It is a photo rather than a text
    file so that it has a thumbnail, which is the only way the thumbnail route can
    assert anything but a 404.

    There are **two** bookings because the scheduling permissions divide on time,
    not only on role: confirming, cancelling, and rescheduling need an appointment
    that has not happened, and recording an outcome needs one that has. One
    booking could not exercise both halves.

    There are **two message threads** for the same kind of reason: closing needs
    an open conversation and reopening needs a closed one. Both are started by the
    counselee, so their participants are the counselee and the counselor, which is
    what makes every actor's expected status meaningful — the counselee acting is
    in the conversation, the counselor acting carries the case, an admin may read
    it, and financial_admin is the actor for whom it does not exist.

    The open thread carries a **real attachment**, sent by the counselee through the
    real ingest pipeline. Its access follows the thread's and nothing else, so the
    row for it in the matrix should read exactly like the row for the thread —
    which is the claim worth being able to see at a glance.

    Billing needs **three invoices**, and for the same reason scheduling needs two
    bookings: the permissions divide on state. Issuing and editing lines need a
    draft, voiding needs an issued invoice with no money against it, and reversing a
    payment needs a payment. One invoice could not be all three at once, so the
    matrix rows point at ``draft_invoice``, ``invoice``, and ``part_paid_invoice``
    respectively. There is a **fourth** session left uninvoiced on purpose, because
    ``billing.change_session`` is refused once a session is on a live invoice, and a
    row asserting a permission that could not apply asserts nothing.

    Everything billing is built through ``apps.billing.services`` rather than by
    hand, so an invoice in this fixture is footed, numbered, and statused the way a
    real one is. The acting user is used as the biller where the role fits, so no row
    depends on a permission check the services layer does not make.
    """
    from datetime import timedelta

    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.utils import timezone

    from apps.billing import services as billing
    from apps.billing.models import Fee, FeeKind, PaymentMethod
    from apps.counseling.models import Case, CaseMember, CounseleeProfile
    from apps.documents.services import store_document
    from apps.messaging.services import close_thread, start_thread
    from apps.scheduling.models import (
        AvailabilityOverride,
        AvailabilityRule,
        Booking,
        BookingStatus,
        Weekday,
    )

    def _build(actor=None):
        role = getattr(actor, "role", None)
        counselor = actor if role == Role.COUNSELOR else make_user(Role.COUNSELOR)
        counselee = actor if role == Role.COUNSELEE else make_user(Role.COUNSELEE)
        case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
        member = CaseMember.objects.create(case=case, counselee=counselee)
        profile, _ = CounseleeProfile.objects.get_or_create(user=counselee)
        document = store_document(
            case=case,
            owner=counselee,
            upload=SimpleUploadedFile("worksheet.jpg", jpeg_bytes()),
            title="Week one worksheet",
        )

        rule = AvailabilityRule.objects.create(
            counselor=counselor,
            weekday=Weekday.TUESDAY,
            start_time="09:00",
            end_time="12:00",
        )
        override = AvailabilityOverride.objects.create(
            counselor=counselor,
            date=timezone.localdate() + timedelta(days=30),
            is_available=False,
        )

        # Built directly rather than through ``services.book``, which would need
        # office hours arranged to line up with a slot the notice rule also allows
        # — brittle, and beside the point here. The matrix asserts who may *reach*
        # a route; the service's own rules are asserted in tests/test_booking.py.
        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        booking = Booking.objects.create(
            counselor=counselor,
            case=case,
            counselee=counselee,
            slot=Booking.range_for(now + timedelta(days=3), 60),
            status=BookingStatus.REQUESTED,
            created_by=counselee,
        )
        past_booking = Booking.objects.create(
            counselor=counselor,
            case=case,
            counselee=counselee,
            slot=Booking.range_for(now - timedelta(days=7), 60),
            status=BookingStatus.CONFIRMED,
            created_by=counselor,
        )

        thread = start_thread(
            case=case,
            author=counselee,
            subject="A question about the homework",
            body="Could we go over the second page at our next session?",
            # A real file through the real pipeline, for the reason the document
            # above is: the attachment row of the matrix has to assert a genuine
            # 200 rather than a 404 from a blob that was never written.
            uploads=[SimpleUploadedFile("page-two.jpg", jpeg_bytes())],
        )
        closed_thread = start_thread(
            case=case,
            author=counselee,
            subject="Something already dealt with",
            body="Never mind, we covered it.",
        )
        close_thread(closed_thread, actor=counselor)

        # The ministry's standard session rate, backdated so that a session recorded
        # today resolves against it. Without a Fee row every session would come out
        # not billable, and every invoice row below would be asserting the empty case.
        fee = Fee.objects.create(
            kind=FeeKind.SESSION,
            amount_cents=8500,
            effective_from=timezone.localdate() - timedelta(days=365),
        )
        biller = actor if role == Role.FINANCIAL_ADMIN else make_user(Role.FINANCIAL_ADMIN)

        def _session():
            return billing.record_session(case=case, counselee=counselee, actor=biller)

        session = _session()
        invoice = billing.issue_invoice(
            billing.create_invoice(
                case=case, counselee=counselee, sessions=[_session()], actor=biller
            ),
            actor=biller,
        )
        draft_invoice = billing.create_invoice(
            case=case, counselee=counselee, sessions=[_session()], actor=biller
        )
        part_paid_invoice = billing.issue_invoice(
            billing.create_invoice(
                case=case, counselee=counselee, sessions=[_session()], actor=biller
            ),
            actor=biller,
        )
        # Part paid, not settled: a paid invoice takes no further payment and cannot
        # be voided, so paying it off would make the reversal row untestable.
        payment = billing.record_payment(
            part_paid_invoice,
            amount_cents=1000,
            method=PaymentMethod.CHECK,
            reference="check 1041",
            actor=biller,
        )

        return SimpleNamespace(
            case=case,
            member=member,
            counselor=counselor,
            counselee=counselee,
            profile=profile,
            document=document,
            rule=rule,
            override=override,
            booking=booking,
            past_booking=past_booking,
            thread=thread,
            closed_thread=closed_thread,
            attachment=thread.messages.first().attachments.first(),
            fee=fee,
            biller=biller,
            session=session,
            invoice=invoice,
            draft_invoice=draft_invoice,
            draft_line=draft_invoice.lines.first(),
            part_paid_invoice=part_paid_invoice,
            payment=payment,
        )

    return _build


@pytest.fixture
def sign_in(client, enrol_totp):
    """Log a user in the way a browser would, clearing any MFA obligation.

    Goes through the real login form and the real TOTP verification rather than
    ``client.force_login``, because the thing most worth testing about this app is
    the gate between those two steps.
    """

    def _sign_in(user, *, password=TEST_PASSWORD):
        response = client.post(
            "/login/",
            {"username": user.email, "password": password},
            follow=False,
        )
        assert response.status_code == 302, "password login should have succeeded"
        if user.mfa_required:
            _, code = enrol_totp(user)
            verified = client.post("/mfa/verify/", {"code": code()})
            assert verified.status_code == 302, "TOTP verification should have succeeded"
        return client

    return _sign_in
