"""Authentication forms, and the one form that creates an account."""

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import STAFF_ROLES, Role, User
from apps.accounts.services import invite_or_hand_over
from apps.audit.models import AuditVerb
from apps.audit.services import record


class EmailAuthenticationForm(AuthenticationForm):
    """Password login, keyed on email.

    AuthenticationForm calls its first field ``username`` regardless of
    USERNAME_FIELD, so this only relabels it. The error message stays Django's
    single "email address and password didn't match": saying which of the two was
    wrong would confirm whether an address has an account here.
    """

    username = forms.EmailField(
        label=_("Email address"),
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "username"}),
    )

    error_messages = {
        **AuthenticationForm.error_messages,
        "invalid_login": _("That email address and password didn't match an active account."),
    }


class MagicLinkRequestForm(forms.Form):
    """Ask for a passwordless login link."""

    email = forms.EmailField(
        label=_("Email address"),
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
    )


class TOTPCodeForm(forms.Form):
    """A six-digit authenticator code.

    Validation of the code itself is not here: it needs the device, and doing it
    in the view keeps the throttling and audit decisions in one place.
    """

    code = forms.CharField(
        label=_("Authentication code"),
        min_length=6,
        max_length=6,
        strip=True,
        widget=forms.TextInput(
            attrs={
                "autofocus": True,
                "autocomplete": "one-time-code",
                "inputmode": "numeric",
                "pattern": "[0-9]*",
            }
        ),
    )

    def clean_code(self):
        code = self.cleaned_data["code"]
        if not code.isdigit():
            raise ValidationError(_("Enter the six digits from your authenticator app."))
        return code


class SetPasswordForm(forms.Form):
    """Choose a first or replacement password.

    Django ships SetPasswordForm already; this one exists because ours is used
    from a token flow where there is no logged-in user to bind to, and because
    the validators need the user for the similarity check.
    """

    new_password1 = forms.CharField(
        label=_("New password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password", "autofocus": True}),
        help_text=_("At least 12 characters. A passphrase of a few words is ideal."),
    )
    new_password2 = forms.CharField(
        label=_("Confirm new password"),
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        cleaned = super().clean()
        first = cleaned.get("new_password1")
        second = cleaned.get("new_password2")
        if first and second and first != second:
            self.add_error("new_password2", _("The two passwords didn't match."))
        if first:
            # Passing the user runs UserAttributeSimilarityValidator, which is
            # what stops someone using their own email address as a password.
            password_validation.validate_password(first, self.user)
        return cleaned

    def save(self):
        self.user.set_password(self.cleaned_data["new_password1"])
        self.user.save(update_fields=["password", "updated_at"])
        return self.user


class UserCreateForm(forms.Form):
    """Create any one of the four kinds of account, and invite the person to it.

    One form for all four roles rather than four forms, because the fields are the
    same and the differences are worth having in one place where they can be read
    together:

      * the role decides whether a second factor is compulsory (staff) and whether
        an emailed sign-in link is even offered (counselees only — a mailbox is a
        weaker factor than a password plus TOTP, and the database refuses a staff
        account with one, so the checkbox is forced off rather than left to fail);
      * the role decides which profile row is created alongside the account, so
        that a counselor invited today already has a practice to configure and a
        counselee already has an intake form to fill in;
      * no password is ever set here. The invitee sets their own from the emailed
        link, which is what keeps "the office knows their password" from being true.

    Choosing a role is the single most consequential thing an administrator can do
    in this application — it is the input to every access rule in the codebase —
    so the page that does it checks ``accounts.manage_users`` and every creation
    lands in the audit trail with the actor on it.

    Subclasses may fix the role instead of asking for it: see
    ``apps.counseling.forms.CounseleeCreateForm``, reached from a case, where the
    only kind of person being added is a counselee.
    """

    #: Set by a subclass to drop the role question and decide it. ``None`` means
    #: the form asks.
    FIXED_ROLE = None

    role = forms.ChoiceField(
        choices=Role.choices,
        initial=Role.COUNSELEE,
        label=_("Role"),
        help_text=_(
            "Counselees can only see their own case. Counselors see the cases "
            "assigned to them. Administrators see everything except payment card "
            "details. Financial administrators see billing and who is on whose "
            "caseload, and never a document or a message."
        ),
    )
    first_name = forms.CharField(max_length=80, label=_("First name"))
    last_name = forms.CharField(max_length=80, label=_("Last name"))
    email = forms.EmailField(label=_("Email address"))
    phone = forms.CharField(max_length=32, required=False, label=_("Phone"))
    allow_magic_link = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Allow signing in with an emailed link"),
        help_text=_(
            "Counselees only. Someone who would struggle with a password can sign in "
            "from a link emailed to them each time. Staff accounts always use a "
            "password and an authenticator app."
        ),
    )

    #: Set by ``save()``: the invitation link, when it could not be emailed. The
    #: view shows it to the administrator; nothing stores it.
    invitation_link = ""

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"]).strip()
        if User.objects.filter(email__iexact=email).exists():
            # Safe to say so here: only an administrator reaches this form, and a
            # vaguer message would have them create the same person twice. The
            # login and magic-link forms stay silent about the same fact.
            raise ValidationError(_("Someone with this address already has an account."))
        return email

    def chosen_role(self):
        """The role being created, whether it was asked for or fixed."""
        return self.FIXED_ROLE or self.cleaned_data.get("role")

    def clean(self):
        cleaned = super().clean()
        if self.chosen_role() in {role.value for role in STAFF_ROLES}:
            # Forced off rather than rejected: the box is ticked by default because
            # most accounts created here are counselees, and an administrator who
            # picks "Counselor" should not have to notice a checkbox that does not
            # apply. Forcing it *off* is the safe direction, and the database
            # constraint stands behind this either way.
            cleaned["allow_magic_link"] = False
        return cleaned

    @transaction.atomic
    def save(self, *, created_by, request=None):
        role = self.chosen_role()
        user = User.objects.create_user(
            email=self.cleaned_data["email"],
            password=None,  # set by the invitee, from the invitation link
            role=role,
            first_name=self.cleaned_data["first_name"],
            last_name=self.cleaned_data["last_name"],
            phone=self.cleaned_data["phone"],
            allow_magic_link=self.cleaned_data["allow_magic_link"],
        )
        # Imported here, not at module scope: counseling imports accounts, so the
        # dependency only runs one way at import time.
        from apps.counseling.models import CounseleeProfile, CounselorProfile

        if role == Role.COUNSELEE:
            CounseleeProfile.objects.create(user=user)
        elif role == Role.COUNSELOR:
            CounselorProfile.objects.create(user=user)

        record(
            AuditVerb.USER_CREATED,
            actor=created_by,
            target=user,
            request=request,
            role=user.role,
        )
        # Inside the transaction, and never fatal: see invite_or_hand_over. An
        # account that exists with an invitation nobody received is recoverable —
        # the link is on the next screen — where a rolled-back form would leave the
        # administrator wondering which half happened.
        self.invitation_link = invite_or_hand_over(
            user=user, request=request, invited_by=created_by
        )
        return user
