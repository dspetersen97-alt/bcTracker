"""
Identity and roles.

The four roles are mutually exclusive and drive every scoping decision, so
``User.role`` is a single authoritative field rather than being mirrored into
Django Groups. One source of truth means one place to audit, and no possibility
of a user's group membership disagreeing with their role.

TOTP devices live in django_otp's own tables; what belongs here is the policy
about who must use one (``mfa_required``) and the single-use link machinery for
counselee magic links and new-account invitations (``LoginToken``).
"""

import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.models import PublicIdModel, TimeStampedModel
from apps.core.scoping import ActorScopedQuerySet

logger = logging.getLogger(__name__)


class Role(models.TextChoices):
    ADMIN = "admin", _("Administrator")
    COUNSELOR = "counselor", _("Counselor")
    FINANCIAL_ADMIN = "financial_admin", _("Financial administrator")
    COUNSELEE = "counselee", _("Counselee")


#: Roles belonging to ministry personnel rather than the people being counseled.
#: Staff handle other people's records, so MFA is mandatory for them.
STAFF_ROLES = frozenset({Role.ADMIN, Role.COUNSELOR, Role.FINANCIAL_ADMIN})


class UserQuerySet(ActorScopedQuerySet):
    """Who each role may see *as people*.

    Distinct from the case-scoped models: a user row is not reached through a
    Case, so the rules are stated here. They follow from what each role has to
    do, and nothing more:

      * a counselor needs the people on their own current cases, and no way to
        learn that another counselor's counselee exists;
      * a counselee needs only themself. Even in a couple's case, listing people
        is not how a counselee learns who else is in the room;
      * financial_admin needs counselors and counselees in order to bill, which
        is exactly the "which counselees does each counselor carry" visibility
        the ministry asked for — and no more, so administrators stay unlisted;
      * admin oversees the ministry and sees everyone.
    """

    def scope_for_admin(self, user):
        return self

    def scope_for_counselor(self, user):
        return self.filter(
            models.Q(pk=user.pk)
            | models.Q(
                case_memberships__case__counselor=user,
                case_memberships__ended_on__isnull=True,
                case_memberships__case__deleted_at__isnull=True,
            )
        ).distinct()

    def scope_for_counselee(self, user):
        return self.filter(pk=user.pk)

    def scope_for_financial_admin(self, user):
        return self.filter(
            models.Q(pk=user.pk) | models.Q(role__in=[Role.COUNSELOR, Role.COUNSELEE])
        ).distinct()


class UserManager(BaseUserManager.from_queryset(UserQuerySet)):
    """Manager for a user model keyed on email with no username field."""

    use_in_migrations = True

    def for_actor(self, user):
        return self.get_queryset().for_actor(user)

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("A user must have an email address.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra)
        # set_password hashes with Argon2id; passing None produces an unusable
        # password, which is what counselees using magic links only should have.
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        """Create the break-glass account.

        Superusers bypass the scoping layer through the admin, so this is
        intentionally separate from the product's own ADMIN role: being a
        ministry administrator does not make someone a superuser.
        """
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("role", Role.ADMIN)
        if extra["is_superuser"] is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        return self._create_user(email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin, PublicIdModel, TimeStampedModel):
    email = models.EmailField(_("email address"), unique=True)
    first_name = models.CharField(max_length=80, blank=True)
    last_name = models.CharField(max_length=80, blank=True)

    role = models.CharField(max_length=20, choices=Role.choices)

    # Rendering timezone for this user. Office hours are interpreted in the
    # counselor's zone; blank falls back to settings.ORG_TIME_ZONE.
    timezone_name = models.CharField(max_length=64, blank=True)

    phone = models.CharField(max_length=32, blank=True)

    # Set from the role on save; stored rather than derived so a future
    # exception ("this volunteer counselor cannot use TOTP") is expressible.
    mfa_required = models.BooleanField(default=False)

    # Counselees may opt into passwordless email links. Staff may not: a mail
    # inbox is a weaker factor than a password plus TOTP.
    allow_magic_link = models.BooleanField(default=False)

    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(
        default=False,
        help_text=_("Django admin access flag. Not related to the ministry staff roles."),
    )
    last_seen_at = models.DateTimeField(null=True, blank=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(role__in=[r.value for r in Role]),
                name="user_role_is_known",
            ),
            # Staff handle other people's counseling records; passwordless email
            # access is not an acceptable sole factor for them. Enforced in the
            # database so no code path can create such an account.
            #
            # sorted(), not list(): STAFF_ROLES is a frozenset, and iteration
            # order for a set of strings varies between processes because string
            # hashing is salted. An unstable order makes the autodetector see a
            # changed constraint at random, which would fail
            # `makemigrations --check` in CI for no reason.
            models.CheckConstraint(
                condition=~models.Q(role__in=sorted(STAFF_ROLES), allow_magic_link=True),
                name="staff_may_not_use_magic_link",
            ),
        ]
        indexes = [
            models.Index(fields=["role"]),
        ]

    def __str__(self) -> str:
        return self.email

    def save(self, *args, **kwargs):
        # Keep the MFA requirement in step with the role by default. An explicit
        # override survives because we only set it when the role changed.
        if self._state.adding:
            self.mfa_required = self.role in STAFF_ROLES
        super().save(*args, **kwargs)

    # --- convenience ------------------------------------------------------

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip() or self.email

    def get_full_name(self) -> str:
        return self.full_name

    def get_short_name(self) -> str:
        return self.first_name or self.email

    @property
    def zoneinfo(self):
        """The zone this person's times are stated in.

        Falls back to the ministry's own zone, and falls back again if the stored
        name is not one the system knows: a bad value in one profile must not be
        able to break a booking page for everyone. Scheduling reads this for both
        sides of an appointment — office hours are the counselor's wall clock, and
        the counselee is shown their own.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            return ZoneInfo(self.timezone_name or settings.ORG_TIME_ZONE)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning("User %s has an unusable timezone %r", self.pk, self.timezone_name)
            return ZoneInfo(settings.ORG_TIME_ZONE)

    @property
    def is_ministry_staff(self) -> bool:
        """True for the three personnel roles. Distinct from ``is_staff``."""
        return self.role in STAFF_ROLES

    def touch_last_seen(self) -> None:
        self.last_seen_at = timezone.now()
        self.save(update_fields=["last_seen_at", "updated_at"])


class TokenPurpose(models.TextChoices):
    MAGIC_LINK = "magic_link", _("Passwordless login link")
    INVITATION = "invitation", _("New account invitation")
    PASSWORD_RESET = "password_reset", _("Password reset")


class LoginTokenQuerySet(models.QuerySet):
    def usable(self):
        return self.filter(consumed_at__isnull=True, expires_at__gt=timezone.now())

    def expired(self):
        return self.filter(expires_at__lte=timezone.now())


class LoginToken(TimeStampedModel):
    """A single-use, time-limited link that logs someone in or sets a password.

    Only a SHA-256 digest of the token is stored. A database dump therefore
    yields no usable links, which matters because these credentials arrive by
    email and email is the weakest part of the chain already. The digest is
    unhashed-but-unguessable rather than password-hashed on purpose: the token is
    256 bits of ``secrets`` output, so there is nothing to brute-force and a slow
    KDF would only make lookup by digest impossible.
    """

    #: Bytes of entropy in the raw token. 32 bytes is far past guessable and
    #: still produces a URL-safe string short enough to survive email clients.
    TOKEN_BYTES = 32

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="login_tokens",
    )
    purpose = models.CharField(max_length=20, choices=TokenPurpose.choices)
    token_hash = models.CharField(max_length=64, unique=True, editable=False)

    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    # Where the link was asked for and where it was used. A mismatch is not
    # blocked — mobile users legitimately change networks — but it is the signal
    # an administrator would want when a link is suspected of being intercepted.
    requested_ip = models.GenericIPAddressField(null=True, blank=True)
    consumed_ip = models.GenericIPAddressField(null=True, blank=True)

    objects = models.Manager.from_queryset(LoginTokenQuerySet)()

    class Meta:
        indexes = [
            models.Index(fields=["user", "purpose", "-created_at"]),
            models.Index(fields=["expires_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.purpose} for {self.user_id} expiring {self.expires_at:%Y-%m-%d %H:%M}"

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode()).hexdigest()

    @classmethod
    def issue(cls, *, user, purpose, ttl_seconds, requested_ip=None):
        """Create a token and return ``(instance, raw_token)``.

        The raw token is returned once and never stored, so a caller that fails
        to send it has to issue a new one.
        """
        raw = secrets.token_urlsafe(cls.TOKEN_BYTES)
        token = cls.objects.create(
            user=user,
            purpose=purpose,
            token_hash=cls.hash_token(raw),
            expires_at=timezone.now() + timedelta(seconds=ttl_seconds),
            requested_ip=requested_ip,
        )
        return token, raw

    @property
    def is_usable(self) -> bool:
        return self.consumed_at is None and self.expires_at > timezone.now()

    def consume(self, *, ip=None) -> bool:
        """Claim the token, returning whether this caller won it.

        Written as a conditional UPDATE rather than check-then-save because a
        link that arrives twice at once — a mail client prefetching it, or a
        double-click — would otherwise pass an ``is_usable`` check in both
        requests and be spent twice. The database decides, so exactly one caller
        can get True.
        """
        now = timezone.now()
        claimed = (
            type(self)
            .objects.filter(pk=self.pk, consumed_at__isnull=True, expires_at__gt=now)
            .update(consumed_at=now, consumed_ip=ip, updated_at=now)
        )
        if not claimed:
            return False
        self.consumed_at = now
        self.consumed_ip = ip
        return True
