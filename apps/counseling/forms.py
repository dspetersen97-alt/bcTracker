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


#: The id the ``<datalist>`` on the New Case page is rendered with. Named once so
#: that the element in the template and the ``list`` attribute on the widget cannot
#: drift apart into a box with nothing behind it.
COUNSELEE_DATALIST_ID = "counselee-options"


def counselee_option(user) -> str:
    """How one counselee appears in the New Case dropdown — and what typing it means.

    The address is part of the text for two reasons. It is what makes each option
    unique, and two people with the same name is not a hypothetical in a ministry
    that has been running for a few years; uniqueness is what lets the string be
    read back afterwards as exactly one person. It also gives the browser's own
    type-ahead more to match on, so an administrator who remembers the address and
    not the spelling of the surname still finds them.
    """
    return f"{user.full_name} ({user.email})"


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
    """Opening a case, with the counselee named at the same time.

    A case with nobody on it is not yet a case: nothing can be booked against it,
    no document can be uploaded to it, and it appears in the list looking done. So
    the page asks who it is for — but it does not answer that question by putting
    four hundred names on the screen and asking somebody to find one.

    "Find a counselee" is a plain text box with a ``<datalist>`` behind it. Clicking
    it opens the roster and typing narrows it, and both of those are the browser's
    own doing: there is no JavaScript in this application, and the previous answer —
    a search box with its own submit button, a list of checkboxes, and a draft in
    the session so a search did not throw away the half-filled form — was a round
    trip to the server for every narrowing of the list.

    What was typed is read back in ``clean_counselee``. A name that matches nobody is
    not an error; it is a counselee who has no account yet, which is the ordinary
    state of affairs when a case is being opened. The case is opened either way and
    the view carries the name on to the page that creates the account.

    One counselee rather than several, which is the one thing lost with the
    checkboxes. A couple's second spouse is added from the case page, where each
    membership is audited as the disclosure it is; a datalist is a control for
    naming one thing, and there is no scriptless way to make it name two.
    """

    #: For the template's ``<datalist>`` element. Read off the form rather than
    #: imported separately, so the element and the widget's ``list`` below cannot
    #: come to disagree about which one is which.
    datalist_id = COUNSELEE_DATALIST_ID

    counselee = forms.CharField(
        required=False,
        label=_("Find a counselee"),
        widget=forms.TextInput(
            attrs={
                "list": COUNSELEE_DATALIST_ID,
                # The browser's own saved-input history for this box would be a list
                # of counselees' names, offered on the next open of the page — which
                # in an office is not necessarily to the same person.
                "autocomplete": "off",
            }
        ),
        help_text=_(
            "Click for the list, or start typing. Nobody by that name yet? Type it "
            "anyway and you will be taken straight to creating the account."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: The memberships ``save`` created, for the view to audit.
        self.members = []
        #: ``(first, last)`` when what was typed named nobody, for the view to carry
        #: on to the New Counselee page. ``None`` when an existing counselee was named.
        self.person_to_create = None
        self._roster = None

    def roster(self):
        """Every counselee this form offers — and, just as much, will accept.

        Evaluated once and kept, because the same list is both rendered into the
        datalist and used to read back what was typed. Two evaluations of the
        queryset could disagree if somebody were created in between, and the shape
        that disagreement takes is the form rejecting a name the page itself offered.
        """
        if self._roster is None:
            self._roster = list(eligible_counselees())
        return self._roster

    def options(self):
        """The text of each ``<option>``, for the template to render."""
        return [counselee_option(user) for user in self.roster()]

    def clean_counselee(self):
        """Read what was typed as a person, or as a person to create.

        Three spellings are accepted as naming somebody: the option text the dropdown
        inserts, an address on its own, and a full name on its own. The last one
        matters — an administrator who typed "Ada Ashford" without ever opening the
        list means Ada Ashford, and being told to pick her from a list she is already
        correctly named on would be pedantry.

        A full name two people share is the one thing that has to stop and ask,
        because guessing there would put a case in front of the wrong person.

        Whitespace is collapsed rather than stripped, so "Ada  Ashford" is Ada
        Ashford, and case is ignored: this is a name typed into a box, not a
        password.
        """
        typed = " ".join(self.cleaned_data["counselee"].split())
        if not typed:
            return None

        wanted = typed.casefold()
        matches = [user for user in self.roster() if counselee_option(user).casefold() == wanted]
        if not matches:
            matches = [
                user
                for user in self.roster()
                if user.email.casefold() == wanted or user.full_name.casefold() == wanted
            ]
        if len(matches) > 1:
            raise forms.ValidationError(
                _(
                    "More than one person is called that. Choose one from the list, "
                    "which shows the email address as well."
                )
            )
        if matches:
            return matches[0]

        # Nobody. The first space splits the name, so "Mary Jo Kelling" is Mary and
        # "Jo Kelling" rather than three guesses — and whatever it gets wrong is
        # visible in two prefilled fields on the very next page.
        first, _space, last = typed.partition(" ")
        self.person_to_create = (first, last)
        return None

    def save(self, commit=True):
        """The case and its membership, or neither.

        Wrapped by the view in a transaction. Membership is the fact every access
        rule in the application resolves through, so half of this landing would
        leave a case whose roster does not match what was submitted.
        """
        case = super().save(commit=commit)
        counselee = self.cleaned_data.get("counselee")
        if commit and counselee is not None:
            self.members = [CaseMember.objects.create(case=case, counselee=counselee)]
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
