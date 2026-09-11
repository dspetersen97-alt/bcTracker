"""
Forms for opening cases and creating the people on them.

Two things these do that a plain ModelForm would not:

  * every queryset offered in a choice field is narrowed to eligible, active
    users, so the form cannot be used to assign a case to a financial
    administrator or to add a counselor as a counselee. The model's
    ``limit_choices_to`` says the same thing, but a form field built by hand
    would not inherit it, so it is stated here too.
  * creating a counselee creates the account and sends the invitation as one
    action. Splitting them is how an account ends up existing with no way to
    sign in to it.
"""

from django import forms
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import Role, User
from apps.accounts.services import invite
from apps.audit.models import AuditVerb
from apps.audit.services import record
from apps.counseling.models import Case, CaseMember, CounseleeProfile, CounselorProfile


def eligible_counselors():
    return User.objects.filter(role=Role.COUNSELOR, is_active=True).order_by(
        "last_name", "first_name"
    )


def eligible_counselees():
    return User.objects.filter(role=Role.COUNSELEE, is_active=True).order_by(
        "last_name", "first_name"
    )


class CaseForm(forms.ModelForm):
    class Meta:
        model = Case
        fields = ["label", "counselor", "kind", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 4})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["counselor"].queryset = eligible_counselors()
        self.fields["counselor"].label_from_instance = lambda user: user.full_name


class CaseCounselorForm(CaseForm):
    """What the assigned counselor may change: everything but the assignment.

    Reassignment moves who can see a case's documents, so it stays an
    administrator's decision — see counseling.manage_case_members.
    """

    class Meta(CaseForm.Meta):
        fields = ["label", "kind", "notes"]

    def __init__(self, *args, **kwargs):
        # Skip CaseForm.__init__: there is no counselor field to narrow.
        forms.ModelForm.__init__(self, *args, **kwargs)


class CaseMemberForm(forms.ModelForm):
    """Add an existing counselee to a case."""

    class Meta:
        model = CaseMember
        fields = ["counselee", "joined_on"]
        widgets = {"joined_on": forms.DateInput(attrs={"type": "date"})}

    def __init__(self, *args, case, **kwargs):
        super().__init__(*args, **kwargs)
        self.case = case
        already_on = case.members.values_list("counselee_id", flat=True)
        self.fields["counselee"].queryset = eligible_counselees().exclude(pk__in=already_on)
        self.fields["counselee"].label_from_instance = (
            lambda user: f"{user.full_name} ({user.email})"
        )

    def save(self, commit=True):
        member = super().save(commit=False)
        member.case = self.case
        if commit:
            member.save()
        return member


class CounseleeCreateForm(forms.Form):
    """Create a counselee's account and invite them, in one step."""

    first_name = forms.CharField(max_length=80)
    last_name = forms.CharField(max_length=80)
    email = forms.EmailField()
    phone = forms.CharField(max_length=32, required=False)
    allow_magic_link = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Allow signing in with an emailed link"),
        help_text=_(
            "Counselees who would struggle with a password can sign in from a link "
            "instead. They can still set a password later."
        ),
    )

    def clean_email(self):
        email = User.objects.normalize_email(self.cleaned_data["email"]).strip()
        if User.objects.filter(email__iexact=email).exists():
            # Safe to say so: only an administrator sees this form, and a silent
            # failure here would have them create the person twice.
            raise forms.ValidationError(_("Someone with this address already has an account."))
        return email

    @transaction.atomic
    def save(self, *, created_by, request=None):
        user = User.objects.create_user(
            email=self.cleaned_data["email"],
            password=None,  # set from the invitation link
            role=Role.COUNSELEE,
            first_name=self.cleaned_data["first_name"],
            last_name=self.cleaned_data["last_name"],
            phone=self.cleaned_data["phone"],
            allow_magic_link=self.cleaned_data["allow_magic_link"],
        )
        CounseleeProfile.objects.create(user=user)
        record(
            AuditVerb.USER_CREATED,
            actor=created_by,
            target=user,
            request=request,
            role=user.role,
        )
        # Inside the transaction on purpose: if the invitation cannot be sent, the
        # account should not exist either. An account nobody can sign in to is
        # worse than a failed form, because the address is now taken.
        invite(user=user, request=request, invited_by=created_by)
        return user


class CounselorProfileForm(forms.ModelForm):
    class Meta:
        model = CounselorProfile
        fields = [
            "credentials",
            "bio",
            "default_session_minutes",
            "booking_notice_hours",
            "booking_horizon_days",
            "accepting_new_cases",
        ]
        widgets = {"bio": forms.Textarea(attrs={"rows": 4})}


class CounseleeProfileForm(forms.ModelForm):
    class Meta:
        model = CounseleeProfile
        fields = [
            "date_of_birth",
            "address",
            "emergency_contact_name",
            "emergency_contact_phone",
            "referred_by",
        ]
        widgets = {
            "date_of_birth": forms.DateInput(attrs={"type": "date"}),
            "address": forms.Textarea(attrs={"rows": 3}),
        }
