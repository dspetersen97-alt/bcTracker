"""
Opening a case, and naming the person it is for.

The page asks four things about the case and then one thing about the people: who is
this for. That last question is the hard one, for two reasons that pull in opposite
directions. A ministry that has been running for a few years has a roster too long
to put on the screen — and the counselee a new case is being opened for very often
does not have an account at all yet, which is the whole reason somebody is on this
page today.

So the box is the browser's own type-ahead: an ``<input list="…">`` over a
``<datalist>`` of the roster. Clicking it opens the list, typing narrows it, and
neither of those is a request to the server, which is what lets this exist at all in
an application with no JavaScript in it.

What is asserted here is mostly one promise, from several directions: **whatever an
administrator types into that box, they end up on a case with the right person on
it.** An option chosen from the list works. A name typed from memory works. A name
that belongs to nobody works too — it opens the case and hands the typed name on to
the page that creates the account, which puts them on the case and lands back on it.

The trap underneath the last one is that the name is somebody's name, so it travels
in the session and never in the URL, and it is offered back only on the case it was
typed on. A prefilled form is a courtesy; a prefilled form on next Tuesday's
unrelated visit would be a stranger's name in front of the wrong case.
"""

import re

import pytest
from django.urls import reverse

from apps.accounts.models import Role, User
from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.forms import counselee_option
from apps.counseling.models import Case, CaseMember
from apps.counseling.views import NEW_COUNSELEE_SESSION_KEY

URL = reverse("counseling:case_create")
NEW_COUNSELEE_URL = reverse("counseling:counselee_create")

pytestmark = pytest.mark.django_db


# --- helpers ---------------------------------------------------------------


def new_counselee(make_user, first, last, **kwargs):
    return make_user(Role.COUNSELEE, first_name=first, last_name=last, **kwargs)


@pytest.fixture
def roster(make_user):
    """A handful of counselees with names worth typing."""
    return {
        "ada": new_counselee(make_user, "Ada", "Ashford"),
        "ben": new_counselee(make_user, "Ben", "Ashford"),
        "cleo": new_counselee(make_user, "Cleo", "Brooks"),
    }


@pytest.fixture
def admin_client(client, sign_in, admin_user):
    sign_in(admin_user)
    return client


@pytest.fixture
def existing_case(counselor, make_user):
    """A case to edit, for the two tests about the other half of this template."""
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage", kind="couple")
    CaseMember.objects.create(case=case, counselee=new_counselee(make_user, "Ada", "Ashford"))
    return case


def opening(counselor, **overrides):
    """What the page posts when "Open case" is pressed."""
    return {
        "label": "The Ashfords",
        "counselor": counselor.pk,
        "kind": "couple",
        "notes": "Referred by the elders.",
        "counselee": "",
    } | overrides


def datalist(page: str) -> str:
    """Just the dropdown's options, so counting them means something."""
    match = re.search(r"<datalist[^>]*>.*?</datalist>", page, re.DOTALL)
    assert match, "the page has no datalist of counselees"
    return match.group(0)


def account_for(first, last, **overrides):
    """What the New Counselee page posts."""
    return {
        "first_name": first,
        "last_name": last,
        "email": f"{first}.{last}@example.org".lower(),
        "phone": "",
        "allow_magic_link": "on",
    } | overrides


# --- naming somebody who already has an account -----------------------------


class TestFindingSomebody:
    def test_the_roster_is_a_dropdown_and_not_a_list_on_the_page(self, admin_client, roster):
        """The complaint this replaced: every counselee the ministry has ever seen,
        listed on the page, to choose one."""
        page = admin_client.get(URL).content.decode()

        assert "Counselees on this case" not in page
        assert 'name="counselees"' not in page
        assert datalist(page).count("<option") == len(roster)

    def test_the_box_points_at_the_dropdown(self, admin_client, roster):
        """An ``input`` whose ``list`` names no ``datalist`` is a plain text box, and
        it looks exactly like a working one."""
        page = admin_client.get(URL).content.decode()

        box = re.search(r'<input[^>]*name="counselee"[^>]*>', page)
        assert box, "the page has no Find a counselee box"
        wanted = re.search(r'<datalist id="([^"]+)"', page)
        assert wanted and f'list="{wanted.group(1)}"' in box.group(0)

    def test_an_option_says_who_it_is_and_how_to_reach_them(self, admin_client, make_user):
        """Two people called Ada Ashford is not a hypothetical, and the address is
        what tells the options — and the two Adas — apart."""
        ada = new_counselee(make_user, "Ada", "Ashford", email="ada@example.org")

        page = admin_client.get(URL).content.decode()

        assert 'value="Ada Ashford (ada@example.org)"' in datalist(page)
        assert counselee_option(ada) in datalist(page)

    def test_choosing_from_the_list_opens_the_case_with_them_on_it(
        self, admin_client, roster, counselor
    ):
        response = admin_client.post(
            URL, opening(counselor, counselee=counselee_option(roster["ada"]))
        )

        assert response.status_code == 302
        case = Case.objects.get()
        assert [member.counselee for member in case.members.all()] == [roster["ada"]]
        assert response.headers["Location"] == reverse(
            "counseling:case_detail", args=[case.public_id]
        )

    def test_a_name_typed_from_memory_is_enough(self, admin_client, roster, counselor):
        """Somebody who typed "Ada Ashford" and never opened the list means Ada
        Ashford. Being sent back to pick her off a list she is already correctly named
        on would be pedantry."""
        admin_client.post(URL, opening(counselor, counselee="Ada Ashford"))

        assert Case.objects.get().members.get().counselee == roster["ada"]

    def test_an_address_on_its_own_is_enough(self, admin_client, roster, counselor):
        admin_client.post(URL, opening(counselor, counselee=roster["cleo"].email))

        assert Case.objects.get().members.get().counselee == roster["cleo"]

    def test_the_shape_of_the_typing_does_not_matter(self, admin_client, roster, counselor):
        """It is a name typed into a box, not a password."""
        admin_client.post(URL, opening(counselor, counselee="  ada   ASHFORD "))

        assert Case.objects.get().members.get().counselee == roster["ada"]

    def test_a_name_two_people_share_stops_and_asks(self, admin_client, make_user, counselor):
        """The one case that cannot be guessed at: guessing would put a case in front
        of the wrong person."""
        new_counselee(make_user, "Ada", "Ashford", email="ada@example.org")
        new_counselee(make_user, "Ada", "Ashford", email="a.ashford@example.org")

        response = admin_client.post(URL, opening(counselor, counselee="Ada Ashford"))

        assert response.status_code == 200
        assert response.context["form"].errors["counselee"]
        assert not Case.objects.exists()
        # And it did not read the ambiguity as a third person to create, either.
        assert User.objects.filter(role=Role.COUNSELEE).count() == 2

    def test_the_membership_is_audited_as_the_disclosure_it_is(
        self, admin_client, roster, counselor
    ):
        """Same verb as the membership page uses. The trail should not depend on which
        page somebody was put on a case from."""
        admin_client.post(URL, opening(counselor, counselee=counselee_option(roster["ada"])))

        event = AuditEvent.objects.get(verb=AuditVerb.CASE_MEMBER_ADDED)
        assert event.metadata["counselee_email"] == roster["ada"].email

    def test_a_case_can_still_be_opened_with_nobody_on_it(self, admin_client, counselor):
        """Sometimes the counselee is genuinely not settled yet. The case is not
        finished, and the message says so rather than the page refusing."""
        response = admin_client.post(URL, opening(counselor, counselee=""), follow=True)

        assert not CaseMember.objects.exists()
        assert any("Add the counselees next" in str(m) for m in response.context["messages"])


# --- naming somebody who has no account yet --------------------------------


class TestSomebodyWithNoAccountYet:
    def test_the_case_is_opened_and_the_account_is_next(self, admin_client, roster, counselor):
        """Not an error. A new case is very often the reason the person needs an
        account in the first place."""
        response = admin_client.post(URL, opening(counselor, counselee="Jo Kelling"))

        case = Case.objects.get()
        assert response.status_code == 302
        assert response.headers["Location"] == f"{NEW_COUNSELEE_URL}?case={case.public_id}"
        assert not case.members.exists()

    def test_the_name_does_not_travel_in_the_url(self, admin_client, counselor):
        """A counselee's name in a query string is a counselee's name in every access
        log the request touches."""
        response = admin_client.post(URL, opening(counselor, counselee="Jo Kelling"))

        assert "kelling" not in response.headers["Location"].lower()

    def test_the_name_arrives_typed_in(self, admin_client, counselor):
        admin_client.post(URL, opening(counselor, counselee="Jo Kelling"))
        case = Case.objects.get()

        page = admin_client.get(f"{NEW_COUNSELEE_URL}?case={case.public_id}")

        assert page.context["form"]["first_name"].value() == "Jo"
        assert page.context["form"]["last_name"].value() == "Kelling"

    @pytest.mark.parametrize(
        ("typed", "first", "last"),
        [
            ("Jo Kelling", "Jo", "Kelling"),
            ("Jo", "Jo", ""),
            ("Mary Jo Kelling", "Mary", "Jo Kelling"),
        ],
    )
    def test_the_first_space_splits_the_name(self, admin_client, counselor, typed, first, last):
        """The first space, and no cleverer than that: whatever it gets wrong is
        sitting in two prefilled fields on the very next page."""
        admin_client.post(URL, opening(counselor, counselee=typed))

        page = admin_client.get(f"{NEW_COUNSELEE_URL}?case={Case.objects.get().public_id}")

        assert page.context["form"]["first_name"].value() == first
        assert page.context["form"]["last_name"].value() == last

    def test_the_prefill_is_only_offered_on_the_case_it_was_typed_on(self, admin_client, counselor):
        """Otherwise an abandoned attempt is a stranger's name prefilled in front of
        an unrelated case."""
        admin_client.post(URL, opening(counselor, counselee="Jo Kelling"))
        somewhere_else = Case.objects.create(counselor=counselor, label="Someone else")

        page = admin_client.get(f"{NEW_COUNSELEE_URL}?case={somewhere_else.public_id}")

        assert not page.context["form"]["first_name"].value()

    def test_the_prefill_is_not_offered_to_a_page_reached_any_other_way(
        self, admin_client, counselor
    ):
        admin_client.post(URL, opening(counselor, counselee="Jo Kelling"))

        page = admin_client.get(NEW_COUNSELEE_URL)

        assert not page.context["form"]["first_name"].value()

    def test_the_whole_journey(self, admin_client, counselor):
        """Open a case for somebody who does not exist, create them, and land on a
        working case with them on it."""
        admin_client.post(URL, opening(counselor, counselee="Jo Kelling"))
        case = Case.objects.get()

        created = admin_client.post(
            f"{NEW_COUNSELEE_URL}?case={case.public_id}", account_for("Jo", "Kelling")
        )

        assert created.headers["Location"] == reverse(
            "counseling:case_detail", args=[case.public_id]
        )
        member = case.members.get()
        assert member.counselee.full_name == "Jo Kelling"
        assert member.counselee.role == Role.COUNSELEE
        assert AuditEvent.objects.filter(verb=AuditVerb.CASE_MEMBER_ADDED).count() == 1

    def test_the_name_is_used_once_and_then_gone(self, admin_client, counselor):
        admin_client.post(URL, opening(counselor, counselee="Jo Kelling"))
        case = Case.objects.get()

        admin_client.post(
            f"{NEW_COUNSELEE_URL}?case={case.public_id}", account_for("Jo", "Kelling")
        )

        assert NEW_COUNSELEE_SESSION_KEY not in admin_client.session

    def test_a_case_that_does_not_exist_creates_nobody(self, admin_client):
        """The case is resolved before the account is created. An account made and
        then found to have nowhere to go is worse than a 404 on the way in."""
        response = admin_client.post(
            f"{NEW_COUNSELEE_URL}?case=1234567890", account_for("Jo", "Kelling")
        )

        assert response.status_code == 404
        assert not User.objects.filter(role=Role.COUNSELEE).exists()

    def test_a_counselor_cannot_use_the_page_at_all(self, client, sign_in, counselor):
        """Creating an account is an administrator's job, and putting somebody on a
        case is the disclosure this app guards hardest."""
        sign_in(counselor)
        case = Case.objects.create(counselor=counselor, label="Theirs")

        response = client.post(
            f"{NEW_COUNSELEE_URL}?case={case.public_id}", account_for("Jo", "Kelling")
        )

        assert response.status_code == 403
        assert not CaseMember.objects.exists()

    def test_nobody_without_the_permission_can_open_a_case(self, client, sign_in, counselor):
        sign_in(counselor)

        response = client.post(URL, opening(counselor, counselee="Jo Kelling"))

        assert response.status_code == 403
        assert NEW_COUNSELEE_SESSION_KEY not in client.session


# --- the page itself -------------------------------------------------------


class TestThePage:
    def test_every_field_on_the_form_reaches_the_page(self, admin_client, roster):
        """The page lays its fields out one by one so the datalist can sit beside the
        box that uses it, which means a field added to the form can be left off it."""
        response = admin_client.get(URL)
        html = response.content.decode()

        for name in response.context["form"].fields:
            assert f'name="{name}"' in html, f"{name} is on the form but not on the page"

    def test_the_box_does_not_offer_the_browsers_own_history(self, admin_client, roster):
        """Saved form entries are a list of counselees' names, offered on the next
        open of the page — which in an office is not necessarily to the same person."""
        page = admin_client.get(URL).content.decode()

        box = re.search(r'<input[^>]*name="counselee"[^>]*>', page)
        assert box and 'autocomplete="off"' in box.group(0)

    def test_the_counselee_box_is_not_on_the_edit_page(self, admin_client, existing_case):
        """There is nobody to name there: membership is changed from the case page,
        which audits it as the disclosure it is."""
        html = admin_client.get(
            reverse("counseling:case_edit", args=[existing_case.public_id])
        ).content.decode()

        assert 'name="counselee"' not in html
        assert "<datalist" not in html

    def test_the_edit_page_offers_a_way_out_as_well_as_a_save(self, admin_client, existing_case):
        """Somebody who opened it to read the notes has to be able to leave without
        wondering whether closing the tab counted as anything."""
        html = admin_client.get(
            reverse("counseling:case_edit", args=[existing_case.public_id])
        ).content.decode()

        assert reverse("counseling:case_detail", args=[existing_case.public_id]) in html
        assert "Go back" in html
