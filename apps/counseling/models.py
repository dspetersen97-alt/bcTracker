"""
Cases, and the membership that defines who may see whom.

``CaseMember`` is the single fact every other app's access rules resolve
through. Documents, bookings, messages, and invoices all reach a counselee only
by way of a Case, which is why this app owns almost nothing else: there is one
place to audit the answer to "is this actor allowed near this person".

Two shapes matter for a counseling ministry:

  * A case has **one counselor and one or more counselees**, because marriage and
    family counseling is one case, not two parallel ones. The couple share
    appointments and a billing relationship.
  * Counselees in a shared case see **nothing of each other's** documents or
    messages. A spouse's private disclosure to the counselor is not case-shared
    material, and defaulting the other way would be a disclosure the ministry
    could not take back. That default lives on the Document model; what lives
    here is the membership it is checked against.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role
from apps.core.dates import org_today
from apps.core.models import PublicIdModel, SoftDeleteModel, SoftDeleteQuerySet, TimeStampedModel
from apps.core.scoping import ActorScopedQuerySet, CaseScopedQuerySet


class CaseKind(models.TextChoices):
    INDIVIDUAL = "individual", _("Individual")
    COUPLE = "couple", _("Couple")
    FAMILY = "family", _("Family")


class CaseStatus(models.TextChoices):
    ACTIVE = "active", _("Active")
    ON_HOLD = "on_hold", _("On hold")
    CLOSED = "closed", _("Closed")


class CaseQuerySet(CaseScopedQuerySet, SoftDeleteQuerySet):
    """Scoping for the Case model itself.

    ``case_path = ""`` because the filters apply to these rows directly rather
    than through a relation — see CaseScopedQuerySet.
    """

    case_path = ""

    def active(self):
        return self.filter(status=CaseStatus.ACTIVE)


class CaseManager(models.Manager.from_queryset(CaseQuerySet)):
    """Default manager: hides soft-deleted cases and offers ``for_actor``."""

    def get_queryset(self):
        return super().get_queryset().alive()

    def for_actor(self, user):
        return self.get_queryset().for_actor(user)


class Case(PublicIdModel, SoftDeleteModel, TimeStampedModel):
    counselor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="cases",
        limit_choices_to={"role": Role.COUNSELOR, "is_active": True},
    )
    label = models.CharField(
        max_length=120,
        help_text=_("How staff refer to this case, e.g. “Smith — marriage”."),
    )
    kind = models.CharField(max_length=20, choices=CaseKind.choices, default=CaseKind.INDIVIDUAL)
    status = models.CharField(max_length=20, choices=CaseStatus.choices, default=CaseStatus.ACTIVE)

    # org_today, not localdate: see apps/core/dates.py. The date a case opened
    # is a fact about the ministry, not about whose screen it was typed on.
    opened_on = models.DateField(default=org_today)
    closed_on = models.DateField(null=True, blank=True)

    # Presenting concern, goals, and similar. Visible to the assigned counselor
    # and to admins; never to financial_admin, which sees only that the case
    # exists and who is on it.
    notes = models.TextField(blank=True)

    objects = CaseManager()
    all_objects = models.Manager.from_queryset(CaseQuerySet)()

    class Meta:
        ordering = ["-opened_on", "label"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(closed_on__isnull=True)
                | models.Q(closed_on__gte=models.F("opened_on")),
                name="case_closed_on_after_opened_on",
            ),
            models.CheckConstraint(
                condition=~models.Q(status=CaseStatus.CLOSED) | models.Q(closed_on__isnull=False),
                name="closed_case_has_a_closed_date",
            ),
        ]
        indexes = [
            models.Index(fields=["counselor", "status"]),
            models.Index(fields=["status", "-opened_on"]),
        ]

    def __str__(self) -> str:
        return self.label

    def clean(self):
        if self.counselor_id and self.counselor.role != Role.COUNSELOR:
            raise ValidationError(
                {"counselor": _("A case must be assigned to someone with the counselor role.")}
            )

    @property
    def counselees(self):
        """The users on this case, through their live memberships."""
        from apps.accounts.models import User

        return User.objects.filter(case_memberships__case=self, case_memberships__ended_on=None)

    def close(self, *, on=None):
        self.status = CaseStatus.CLOSED
        self.closed_on = on or org_today()
        self.save(update_fields=["status", "closed_on", "updated_at"])


class CaseMemberQuerySet(CaseScopedQuerySet):
    case_path = "case"

    def current(self):
        return self.filter(ended_on__isnull=True)

    def scope_for_counselee(self, user):
        """A counselee sees only their *own* membership, not their spouse's.

        This is where the "nothing of the other's" rule starts. The generic
        case-scoped version would return every membership row on a shared case,
        which would tell spouse A that spouse B is on the case — true, and known
        to them already in a couple's case, but not something to establish by
        accident for a family case with an estranged member.
        """
        return self.filter(counselee=user)


class CaseMember(PublicIdModel, TimeStampedModel):
    """One counselee's participation in one case.

    Not soft-deleted: a membership that ends is closed with ``ended_on`` rather
    than removed, because appointment and billing history refer to it and the
    dates are the record of who was in the room.
    """

    case = models.ForeignKey(Case, on_delete=models.CASCADE, related_name="members")
    counselee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="case_memberships",
        limit_choices_to={"role": Role.COUNSELEE},
    )
    joined_on = models.DateField(default=org_today)
    ended_on = models.DateField(null=True, blank=True)

    objects = models.Manager.from_queryset(CaseMemberQuerySet)()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["case", "counselee"], name="uniq_case_counselee"),
            models.CheckConstraint(
                condition=models.Q(ended_on__isnull=True)
                | models.Q(ended_on__gte=models.F("joined_on")),
                name="membership_ended_after_joined",
            ),
        ]
        # Reverse of the FK order: the hot query is "which cases is this
        # counselee on", asked on every scoped read for a counselee.
        indexes = [models.Index(fields=["counselee", "case"])]

    def __str__(self) -> str:
        return f"{self.counselee} on {self.case}"

    def clean(self):
        if self.counselee_id and self.counselee.role != Role.COUNSELEE:
            raise ValidationError(
                {"counselee": _("Only someone with the counselee role can be a case member.")}
            )

    def end(self, *, on=None):
        self.ended_on = on or org_today()
        self.save(update_fields=["ended_on", "updated_at"])


class CounselorProfileQuerySet(ActorScopedQuerySet):
    """Practice information, not counseling content.

    Credentials and a bio are shown to counselees when they pick an appointment,
    so every signed-in role may read them. Stated per role rather than by
    returning ``self`` from ``for_actor``, so it stays obvious that this was a
    decision about this model and not the default leaking through.
    """

    def scope_for_admin(self, user):
        return self

    def scope_for_counselor(self, user):
        return self

    def scope_for_financial_admin(self, user):
        return self

    def scope_for_counselee(self, user):
        return self


class CounselorProfile(TimeStampedModel):
    """Practice details for a counselor.

    Separate from User because these fields are meaningless for the other three
    roles, and a nullable-for-most-rows column is worse than a table that only
    exists for the people it describes.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="counselor_profile",
        limit_choices_to={"role": Role.COUNSELOR},
    )
    credentials = models.CharField(
        max_length=120,
        blank=True,
        help_text=_("e.g. “ACBC Certified”. Shown to counselees."),
    )
    bio = models.TextField(blank=True)

    # Scheduling defaults. The scheduling app reads these when generating slots;
    # they live here because they are properties of how this counselor works.
    default_session_minutes = models.PositiveSmallIntegerField(default=60)
    booking_notice_hours = models.PositiveSmallIntegerField(
        default=24,
        help_text=_("Minimum notice a counselee must give when booking."),
    )
    booking_horizon_days = models.PositiveSmallIntegerField(
        default=60,
        help_text=_("How far ahead counselees may book."),
    )
    accepting_new_cases = models.BooleanField(default=True)

    objects = models.Manager.from_queryset(CounselorProfileQuerySet)()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(default_session_minutes__gte=15)
                & models.Q(default_session_minutes__lte=480),
                name="session_length_is_plausible",
            ),
        ]

    def __str__(self) -> str:
        return f"Counselor profile for {self.user}"


class CounseleeProfileQuerySet(ActorScopedQuerySet):
    """Reachable through case membership, not through the Case FK chain.

    A profile belongs to a person rather than to a case, so it cannot use
    CaseScopedQuerySet. The rules are the same in spirit: a counselor sees the
    profiles of the people currently on their own cases and nobody else's.
    """

    def scope_for_admin(self, user):
        return self

    def scope_for_counselor(self, user):
        return self.filter(
            user__case_memberships__case__counselor=user,
            user__case_memberships__ended_on__isnull=True,
            user__case_memberships__case__deleted_at__isnull=True,
        ).distinct()

    def scope_for_counselee(self, user):
        return self.filter(user=user)

    def scope_for_financial_admin(self, user):
        # None. A date of birth, home address, and emergency contact are not
        # billing data. Billing identifies a counselee by name and email, which
        # it gets from the user record. If invoicing ever needs a mailing
        # address, that is a decision to make out loud rather than to inherit.
        return self.none()


class CounseleeProfile(PublicIdModel, TimeStampedModel):
    """Intake details for a counselee.

    Deliberately thin. Everything here is what the ministry needs to run an
    appointment and reach someone in an emergency; anything clinical belongs in
    a document or a session note, where it is encrypted and audited on access.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="counselee_profile",
        limit_choices_to={"role": Role.COUNSELEE},
    )
    date_of_birth = models.DateField(null=True, blank=True)
    address = models.TextField(blank=True)

    emergency_contact_name = models.CharField(max_length=120, blank=True)
    emergency_contact_phone = models.CharField(max_length=32, blank=True)

    referred_by = models.CharField(max_length=120, blank=True)
    intake_completed_on = models.DateField(null=True, blank=True)

    objects = models.Manager.from_queryset(CounseleeProfileQuerySet)()

    class Meta:
        verbose_name = "counselee profile"

    def __str__(self) -> str:
        return f"Counselee profile for {self.user}"
