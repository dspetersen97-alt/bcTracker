"""
Fill a development database with something to click around in.

An empty bcTracker is a login page: every screen in the application is a view of
a case, and there are no cases until somebody creates people, assigns them, and
books a session. Doing that by hand through the invitation flows takes twenty
minutes and produces a slightly different shape each time, which is a poor basis
for "does this page look right".

So this command builds one worked example — a counselor with two cases, a couple
seeing them together, a lapsed member, documents in both directions,
correspondence with an attachment, and a diary with a past and a future
appointment — and prints the credentials for it.

Three things it deliberately does **not** do:

  * **Run outside development.** It refuses unless ``DEBUG`` is on. Seeded
    accounts have a published password, and a "demo" counselee in a real database
    is a real person's file that nobody is responsible for.
  * **Invent counseling content.** The notes and messages are about homework and
    scheduling. A development database full of plausible disclosures is a
    liability, and anyone reviewing a screenshot should not have to wonder whether
    what is on it is real.
  * **Delete anything.** Everything is ``get_or_create``, so running it twice is
    safe and running it after a schema change tops up what is missing.

Everything that has a service goes through the service — documents are really
encrypted, messages really write their participant rows, and the audit trail ends
up populated, which is itself worth being able to look at.
"""

from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django_otp.plugins.otp_totp.models import TOTPDevice

from apps.accounts.models import Role, User
from apps.core.dates import org_today
from apps.counseling.models import (
    Case,
    CaseKind,
    CaseMember,
    CounseleeProfile,
    CounselorProfile,
)
from apps.documents.models import DocumentKind, Visibility
from apps.documents.services import store_document
from apps.messaging.services import post_message, start_thread
from apps.scheduling.models import AvailabilityRule, Booking, BookingStatus, Weekday

#: Published on purpose — see the refusal below. Long enough to satisfy the
#: password validators, so seeded accounts log in through the real form.
PASSWORD = "counsel-the-flock-2026"  # noqa: S105 — a development fixture, printed below

PDF = b"%PDF-1.7\n1 0 obj\n<< >>\nendobj\ntrailer\n%%EOF\n"


def _ten_oclock(day, counselor):
    """10am on ``day``, in the counselor's own timezone.

    Built local-then-aware rather than by arithmetic on a UTC instant, for the
    reason apps/scheduling/slots.py gives: 9am in the room is a different UTC hour
    in January than in July, and an appointment is a time in the room.
    """
    return datetime.combine(day, time(hour=10), tzinfo=counselor.zoneinfo)


class Command(BaseCommand):
    help = "Create a worked example in the development database. Refuses unless DEBUG."

    def add_arguments(self, parser):
        parser.add_argument(
            "--codes",
            action="store_true",
            help="Print a currently valid TOTP code for each staff account and exit.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "seed_demo refuses to run with DEBUG off. It creates accounts with a "
                "published password; in a real database those are real people's files "
                "with nobody responsible for them."
            )

        if options["codes"]:
            self._print_codes()
            return

        with transaction.atomic():
            people = self._people()
            cases = self._cases(people)
            self._diary(people, cases)
            self._paperwork(people, cases)
            self._correspondence(people, cases)

        self._report(people)

    # --- the worked example ------------------------------------------------

    def _user(self, email, role, first, last, **extra):
        user, created = User.objects.get_or_create(
            email=email,
            defaults={"role": role, "first_name": first, "last_name": last, **extra},
        )
        if created:
            user.set_password(PASSWORD)
            user.save(update_fields=["password"])
        if user.is_ministry_staff:
            # Confirmed rather than pending, so signing in does not begin with an
            # enrolment page. The secret is printed at the end for an authenticator
            # app, and ``--codes`` prints a live code for anyone without one.
            TOTPDevice.objects.get_or_create(
                user=user, name="primary", defaults={"confirmed": True}
            )
        return user

    def _people(self):
        pastor = self._user("pastor@example.org", Role.COUNSELOR, "Samuel", "Whitfield")
        deacon = self._user("deacon@example.org", Role.COUNSELOR, "Ruth", "Okafor")
        director = self._user("director@example.org", Role.ADMIN, "Miriam", "Vance")
        treasurer = self._user("treasurer@example.org", Role.FINANCIAL_ADMIN, "Harold", "Beck")
        ada = self._user("ada@example.org", Role.COUNSELEE, "Ada", "Ashford")
        ben = self._user("ben@example.org", Role.COUNSELEE, "Ben", "Ashford")
        joel = self._user("joel@example.org", Role.COUNSELEE, "Joel", "Marsh")
        # A member who has left, so "access ends with the relationship" is
        # something you can see rather than only read about.
        tess = self._user("tess@example.org", Role.COUNSELEE, "Tess", "Marsh")

        for counselor, credentials in (
            (pastor, "MDiv, ACBC certified"),
            (deacon, "ACBC in training"),
        ):
            CounselorProfile.objects.get_or_create(
                user=counselor,
                defaults={
                    "credentials": credentials,
                    "bio": "Available Tuesday and Thursday mornings.",
                },
            )
        for counselee in (ada, ben, joel, tess):
            CounseleeProfile.objects.get_or_create(
                user=counselee,
                defaults={
                    "emergency_contact_name": "Church office",
                    "emergency_contact_phone": "555-0100",
                    "intake_completed_on": org_today() - timedelta(days=30),
                },
            )
        return {
            "pastor": pastor,
            "deacon": deacon,
            "director": director,
            "treasurer": treasurer,
            "ada": ada,
            "ben": ben,
            "joel": joel,
            "tess": tess,
        }

    def _cases(self, people):
        marriage, _ = Case.objects.get_or_create(
            label="Ashford — marriage",
            defaults={
                "counselor": people["pastor"],
                "kind": CaseKind.COUPLE,
                "opened_on": org_today() - timedelta(days=45),
                "notes": "Meeting fortnightly. Working through the communication workbook.",
            },
        )
        individual, _ = Case.objects.get_or_create(
            label="Marsh — individual",
            defaults={
                "counselor": people["deacon"],
                "kind": CaseKind.INDIVIDUAL,
                "opened_on": org_today() - timedelta(days=10),
            },
        )
        for case, counselee in (
            (marriage, people["ada"]),
            (marriage, people["ben"]),
            (individual, people["joel"]),
            (individual, people["tess"]),
        ):
            CaseMember.objects.get_or_create(
                case=case,
                counselee=counselee,
                defaults={"joined_on": case.opened_on},
            )
        # Tess has moved away. Her row stays, which is the record of who was in
        # the room; what she can reach ends today.
        CaseMember.objects.filter(case=individual, counselee=people["tess"]).update(
            ended_on=org_today()
        )
        return {"marriage": marriage, "individual": individual}

    def _diary(self, people, cases):
        for counselor in (people["pastor"], people["deacon"]):
            for weekday in (Weekday.TUESDAY, Weekday.THURSDAY):
                AvailabilityRule.objects.get_or_create(
                    counselor=counselor,
                    weekday=weekday,
                    start_time="09:00",
                    end_time="12:00",
                    effective_from=org_today() - timedelta(days=60),
                    defaults={"slot_minutes": 60},
                )

        # Built directly rather than through ``scheduling.services.book``, which
        # would need a slot that happens to line up with the office hours above and
        # the minimum-notice rule at the moment this runs. Self-booking is the thing
        # to click on anyway; this is just history to look at.
        pastor = people["pastor"]
        upcoming = Booking.range_for(_ten_oclock(org_today() + timedelta(days=7), pastor), 60)
        Booking.objects.get_or_create(
            counselor=pastor,
            case=cases["marriage"],
            counselee=people["ada"],
            slot=upcoming,
            defaults={"status": BookingStatus.CONFIRMED, "created_by": people["ada"]},
        )
        held = Booking.range_for(_ten_oclock(org_today() - timedelta(days=7), pastor), 60)
        Booking.objects.get_or_create(
            counselor=people["pastor"],
            case=cases["marriage"],
            counselee=people["ben"],
            slot=held,
            defaults={
                "status": BookingStatus.COMPLETED,
                "created_by": people["pastor"],
                "counselor_note": "Worked through chapter three. Homework set.",
            },
        )

    def _paperwork(self, people, cases):
        if cases["marriage"].documents.exists():
            return
        # One from the counselor to the whole case, one from a counselee that the
        # other counselee must not be able to see. That pair is the documents
        # feature; everything else on the page is presentation.
        store_document(
            case=cases["marriage"],
            owner=people["pastor"],
            upload=SimpleUploadedFile("communication-workbook.pdf", PDF),
            title="Communication workbook",
            description="Chapters three and four before we next meet.",
            kind=DocumentKind.HOMEWORK,
            visibility=Visibility.CASE_SHARED,
        )
        store_document(
            case=cases["marriage"],
            owner=people["ada"],
            upload=SimpleUploadedFile("my-notes.pdf", PDF),
            title="My notes from Tuesday",
            kind=DocumentKind.OTHER,
        )

    def _correspondence(self, people, cases):
        if cases["marriage"].threads.exists():
            return
        hers = start_thread(
            case=cases["marriage"],
            author=people["ada"],
            subject="Moving next Tuesday",
            body="Could we start half an hour later? I can explain when we meet.",
            uploads=[SimpleUploadedFile("calendar.pdf", PDF)],
        )
        post_message(
            hers,
            author=people["pastor"],
            body="That is fine. I have moved it to 10:30.",
        )
        # Ben's own thread, so the isolation between spouses is visible from both
        # sides rather than described.
        start_thread(
            case=cases["marriage"],
            author=people["ben"],
            subject="A question about the reading",
            body="Chapter four assumes something I am not sure I agree with.",
        )

    # --- output ------------------------------------------------------------

    def _codes(self):
        """The six digits an authenticator app would be showing right now.

        Computed the way django_otp verifies them, so a code from here and a code
        from a phone are the same code — this is a convenience, not a bypass.
        """
        import time as wall_clock

        from django_otp.oath import TOTP

        now = wall_clock.time()
        for device in (
            TOTPDevice.objects.filter(confirmed=True).select_related("user").order_by("user__email")
        ):
            totp = TOTP(device.bin_key, device.step, device.t0, device.digits)
            totp.time = now
            remaining = device.step - int(now - device.t0) % device.step
            yield device.user, f"{totp.token():0{device.digits}d}", remaining

    def _print_codes(self):
        """Codes with their shelf life, because the useful question is "still?".

        A bare list of six-digit numbers is unreadable the moment you look away:
        there is no way to tell a code with 28 seconds left from one with 2.
        """
        self.stdout.write("Six-digit codes, valid for the seconds shown:\n")
        for user, code, remaining in self._codes():
            warn = "  <- expiring, re-run" if remaining <= 5 else ""
            self.stdout.write(f"  {code}   {remaining:>2}s   {user.email}{warn}")
        self.stdout.write(
            "\nThese are the codes. The long otpauth:// strings printed by a seed run are "
            "secrets\nfor an authenticator app, which generates codes like these from them."
        )

    def _report(self, people):
        self.stdout.write(self.style.SUCCESS("\nSeeded. Everyone's password:"))
        self.stdout.write(f"  {PASSWORD}\n")

        self.stdout.write("Staff - password, then a TOTP code:")
        for key in ("pastor", "deacon", "director", "treasurer"):
            user = people[key]
            device = TOTPDevice.objects.filter(user=user, confirmed=True).first()
            self.stdout.write(f"  {user.email:<26} {user.role}")
            if device is not None:
                self.stdout.write(f"    {device.config_url}")

        self.stdout.write("\nCounselees — password or an emailed link, no TOTP:")
        for key in ("ada", "ben", "joel", "tess"):
            user = people[key]
            note = " (membership ended - should now reach nothing)" if key == "tess" else ""
            self.stdout.write(f"  {user.email:<26} {user.full_name}{note}")

        self.stdout.write(
            "\nAdd a config URL above to an authenticator app, or run "
            "`python manage.py seed_demo --codes` for a live code.\n"
            "Magic links and reminder emails are printed to the runserver console."
        )
