"""
Create a ministry staff account and invite them to set a password.

This is how the *first* administrator comes into existence, and it can print the
invitation link instead of mailing it: on an empty database there is nobody to
configure SMTP, and an installation that cannot create its first user until mail
works is an installation that cannot be started. It is also the recovery path
when there is no administrator left who can sign in.

Day to day, an administrator now creates staff from the application instead — see
``apps/accounts/views.py::user_create``. That page and this command deliberately
share ``issue_invitation`` and the same refusals, so the rules about who may
exist live in one place; what the page adds is that the person making the
decision does not need shell access to the host.

What it deliberately will not do:

  * Create a counselee. That would skip the case they were created for.
  * Change an existing person's role. Promoting somebody is a decision with
    consequences for records they can already see, and it should be visible as
    its own act rather than a side effect of re-running an invite.
  * Set a password. The invitee sets their own, and then enrols a TOTP device —
    staff accounts are unusable without one.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction

from apps.accounts.models import STAFF_ROLES, Role, User
from apps.accounts.services import invitation_path, issue_invitation
from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.core.mail import mail_is_configured


class Command(BaseCommand):
    help = "Create a staff account (admin, counselor, financial_admin) and invite them."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True, help="The person's email address.")
        parser.add_argument(
            "--role",
            required=True,
            choices=sorted(role.value for role in STAFF_ROLES),
            # No default: the difference between these three is who may read a
            # counselee's file, and it should be typed out rather than assumed.
            help="Which staff role. Counselees are created inside the application.",
        )
        parser.add_argument("--first-name", default="")
        parser.add_argument("--last-name", default="")
        parser.add_argument(
            "--reinvite",
            action="store_true",
            help=(
                "The account already exists: issue a fresh invitation instead of "
                "refusing. Its previous invitation stops working once this one is used."
            ),
        )
        parser.add_argument(
            "--print-link",
            action="store_true",
            help=(
                "Print the invitation link rather than emailing it. Implied when no "
                "mail account is configured."
            ),
        )

    def handle(self, *args, **options):
        email = self._clean_email(options["email"])
        role = options["role"]

        if role == Role.COUNSELEE:  # pragma: no cover — argparse choices exclude it
            raise CommandError("Counselees are created in the application, on a case.")

        existing = User.objects.filter(email__iexact=email).first()
        # Mail is configured or it is not; there is no third state, and the
        # difference decides whether this command can deliver anything. Asked of
        # apps.core.mail rather than of the environment, because the settings now
        # live in the database as well — a ministry that configured SMTP from the
        # web UI should not be told at the console that it has no mail account.
        mail_configured = mail_is_configured()
        send = mail_configured and not options["print_link"]

        with transaction.atomic():
            if existing is not None:
                user = self._existing(existing, role=role, reinvite=options["reinvite"])
                created = False
            else:
                user = self._create(
                    email=email,
                    role=role,
                    first_name=options["first_name"],
                    last_name=options["last_name"],
                )
                created = True

            # Inside the transaction, like the counselee flow: an account nobody
            # can claim is worse than a failed command, because the address is
            # now taken and the next attempt will be refused.
            token, raw = issue_invitation(user=user, send=send)

        self._report(
            user=user,
            created=created,
            sent=send,
            mail_configured=mail_configured,
            raw_token=raw,
            expires_at=token.expires_at,
        )

    # --- validation -------------------------------------------------------

    def _clean_email(self, raw):
        """Normalize and check the address before an account is built around it.

        Worth doing here rather than leaving it to the database: ``email`` is
        unique, so a typo becomes an account nobody can sign in to and an address
        that is now taken. Normalized the same way the login form does it, so
        ``Pastor@Example.org`` and ``pastor@example.org`` cannot become two people.
        """
        email = User.objects.normalize_email(raw).strip()
        try:
            validate_email(email)
        except ValidationError as error:
            raise CommandError(f"{raw!r} is not a usable email address.") from error
        return email

    # --- the two paths ----------------------------------------------------

    def _create(self, *, email, role, first_name, last_name):
        user = User.objects.create_user(
            email=email,
            password=None,  # set from the invitation link
            role=role,
            first_name=first_name,
            last_name=last_name,
            # Not passed, and not passable: a database constraint refuses a staff
            # account that can be reached with nothing but an emailed link.
            allow_magic_link=False,
        )
        if role == Role.COUNSELOR:
            # So their availability page has something to edit on first sign-in.
            # The page would create this itself; doing it here means a counselor
            # invited and never signed in still appears as a real practice.
            from apps.counseling.models import CounselorProfile

            CounselorProfile.objects.get_or_create(user=user)
        record(
            AuditVerb.USER_CREATED,
            target=user,
            role=user.role,
            # No actor: this was done at the console, not by a signed-in person.
            # The trail says so rather than attributing it to nobody in particular.
            source="manage.py invite_staff",
        )
        return user

    def _existing(self, user, *, role, reinvite):
        if not reinvite:
            raise CommandError(
                f"{user.email} already has an account ({user.role}). "
                "Pass --reinvite to send them a new invitation link."
            )
        if user.role != role:
            raise CommandError(
                f"{user.email} is already {user.role}, not {role}. This command does not "
                "change roles: a role decides what counseling records somebody can "
                "reach, so change it deliberately and on its own."
            )
        if not user.is_active:
            raise CommandError(
                f"{user.email} is deactivated. Reactivate the account first; "
                "inviting a disabled account produces a link that cannot be used."
            )
        return user

    # --- output -----------------------------------------------------------

    def _report(self, *, user, created, sent, mail_configured, raw_token, expires_at):
        verb = "Created" if created else "Re-invited"
        self.stdout.write(self.style.SUCCESS(f"{verb} {user.email} as {user.role}."))

        if sent:
            self.stdout.write(f"An invitation was emailed to {user.email}.")
        else:
            reason = (
                "no mail account is configured (set one under Email settings, or "
                "EMAIL_HOST_USER / EMAIL_HOST_PASSWORD in .env)"
                if not mail_configured
                else "asked for with --print-link"
            )
            self.stdout.write(f"Not emailed — {reason}. Give them this link:")
            self.stdout.write("")
            self.stdout.write(f"    {settings.SITE_BASE_URL}{invitation_path(raw_token)}")
            self.stdout.write("")
            self.stdout.write(
                "It is single-use and is not recoverable from the database — only a "
                "digest is stored — so re-run with --reinvite if it is lost."
            )

        self.stdout.write(f"The link expires {expires_at:%Y-%m-%d %H:%M} UTC.")
        self.stdout.write(
            "They set a password from it and are then required to enrol an "
            "authenticator app before they can reach anything: every staff account "
            "needs a second factor."
        )
