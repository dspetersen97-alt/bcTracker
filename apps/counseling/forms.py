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
    sign in to it. That part is inherited from ``accounts.forms.UserCreateForm``,
    so the two pages that can create a counselee cannot diverge.
"""

from django import forms
from django.utils.translation import gettext_lazy as _

from apps.accounts.forms import UserCreateForm
from apps.accounts.models import Role, User
from apps.counseling.models import (
    Case,
    CaseMember,
    CounseleeProfile,
    CounselorProfile,
)


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


class CaseCreateForm(CaseForm):
    """Opening a case, with the people on it chosen at the same time.

    A case with nobody on it is not yet a case: nothing can be booked against it,
    no document can be uploaded to it, and it appears in the list looking done.
    The old page ended on "add the counselees next", which is one more step
    between an administrator and a working case, and a step easy to leave undone.

    Checkboxes rather than a multi-select: picking two people out of a native
    multi-select needs ctrl-click, which is the sort of thing that quietly loses
    the second spouse. The list is every active counselee, which is fine for a
    ministry's roster and would not be for a hospital's.
    """

    counselees = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label=_("Counselees on this case"),
        help_text=_("More than one for a couple or a family. They can be added later too."),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["counselees"].queryset = eligible_counselees()
        self.fields["counselees"].label_from_instance = (
            lambda user: f"{user.full_name} ({user.email})"
        )
        #: The memberships ``save`` created, for the view to audit.
        self.members = []

    def save(self, commit=True):
        """The case and its memberships, or neither.

        Wrapped by the view in a transaction. Membership is the fact every access
        rule in the application resolves through, so half of this landing would
        leave a case whose roster does not match what was submitted.
        """
        case = super().save(commit=commit)
        if commit:
            self.members = [
                CaseMember.objects.create(case=case, counselee=counselee)
                for counselee in self.cleaned_data["counselees"]
            ]
        return case


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


class CounseleeCreateForm(UserCreateForm):
    """Create a counselee's account and invite them, in one step.

    ``UserCreateForm`` with the role decided rather than asked. It is reached from
    a case — from the New Case page, or from adding a member to an existing one —
    and on that path the only kind of person being created is a counselee. Asking
    would be offering an administrator the chance to accidentally make a colleague
    out of somebody who came for counseling.

    Everything else, including the invitation and its fallback link, is inherited:
    the two paths must not drift apart, because a counselee created here and one
    created on the New Person page have to be the same thing.
    """

    FIXED_ROLE = Role.COUNSELEE

    #: Dropping the inherited field. Django's form metaclass treats None as
    #: "remove this", so the page never renders a role question.
    role = None

    allow_magic_link = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Allow signing in with an emailed link"),
        help_text=_(
            "Counselees who would struggle with a password can sign in from a link "
            "instead. They can still set a password later."
        ),
    )


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
