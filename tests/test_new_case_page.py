"""
Opening a case without losing what has been typed.

The page asks for four things about the case and then for the people on it, and
the people are the hard part. A ministry that has been running for a few years has
a roster too long to scroll, so the list is a search result; and the counselee who
should be on the new case is quite often somebody who does not have an account yet,
so there is a way out to create one. Both of those are round trips to the server —
there is no JavaScript in this application — and a round trip is where a
half-filled form gets thrown away.

So what is asserted here is mostly one promise, from several directions: **nothing
an administrator typed is lost by using the page as it invites them to.** Searching
keeps it. Leaving to create a counselee keeps it. Coming back keeps it, and ticks
the person who has just been created without un-ticking the one already chosen.

The other half is the trap underneath that. Narrowing a ``ModelMultipleChoiceField``
narrows what it will *accept*, so a search that filtered out somebody already
chosen would turn the final submission into "that is not a valid choice" — about
somebody the administrator ticked, on a page that no longer lists them. There is a
test for exactly that, and it is the reason ``CaseCreateForm._offered`` adds the
chosen back in.
"""

import re

import pytest
from django.urls import reverse

from apps.accounts.models import Role
from apps.counseling.models import Case, CaseMember
from apps.counseling.views import CASE_DRAFT_SESSION_KEY

URL = reverse("counseling:case_create")

pytestmark = pytest.mark.django_db


# --- helpers ---------------------------------------------------------------


def offered_names(response):
    """Who the page is offering to put on the case, by full name."""
    return [user.full_name for user in response.context["form"].fields["counselees"].queryset]


def new_counselee(make_user, first, last):
    return make_user(Role.COUNSELEE, first_name=first, last_name=last)


@pytest.fixture
def roster(make_user):
    """A handful of counselees with names worth searching for."""
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


def draft(**overrides):
    """What the page posts when a button other than "Open case" is pressed."""
    return {
        "label": "The Ashfords",
        "kind": "couple",
        "notes": "Referred by the elders.",
        "find": "",
        "counselees": [],
    } | overrides


# --- searching -------------------------------------------------------------


class TestFindingSomebody:
    def test_the_whole_roster_is_offered_when_nothing_is_searched_for(self, admin_client, roster):
        response = admin_client.get(URL)

        assert set(offered_names(response)) == {user.full_name for user in roster.values()}

    def test_searching_narrows_the_list(self, admin_client, roster):
        admin_client.post(URL, draft(find="brooks", search="1"))
        response = admin_client.get(URL)

        assert offered_names(response) == ["Cleo Brooks"]

    def test_a_surname_finds_a_couple(self, admin_client, roster):
        admin_client.post(URL, draft(find="ashford", search="1"))
        response = admin_client.get(URL)

        assert sorted(offered_names(response)) == ["Ada Ashford", "Ben Ashford"]

    def test_a_full_name_is_matched_word_by_word(self, admin_client, roster):
        """No column holds "Ada Ashford", so each word is matched against each column
        and all of them have to land somewhere."""
        admin_client.post(URL, draft(find="ada ashford", search="1"))
        response = admin_client.get(URL)

        assert offered_names(response) == ["Ada Ashford"]

    def test_an_address_is_searchable_too(self, admin_client, make_user):
        user = new_counselee(make_user, "Ada", "Ashford")
        new_counselee(make_user, "Cleo", "Brooks")

        admin_client.post(URL, draft(find=user.email, search="1"))
        response = admin_client.get(URL)

        assert offered_names(response) == ["Ada Ashford"]

    def test_searching_is_not_opening_the_case(self, admin_client, roster):
        response = admin_client.post(URL, draft(find="ashford", search="1"))

        assert response.status_code == 302
        assert response.headers["Location"] == URL
        assert not Case.objects.exists()

    def test_the_search_term_is_not_put_in_the_url(self, admin_client, roster):
        """A counselee's surname in a query string is a counselee's surname in every
        access log the request touches."""
        response = admin_client.post(URL, draft(find="ashford", search="1"))

        assert "ashford" not in response.headers["Location"].lower()

    def test_what_was_typed_survives_the_search(self, admin_client, roster, counselor):
        admin_client.post(
            URL,
            draft(find="ashford", search="1", counselor=counselor.pk, notes="Marriage."),
        )
        page = admin_client.get(URL)

        assert page.context["form"]["label"].value() == "The Ashfords"
        assert page.context["form"]["notes"].value() == "Marriage."
        assert page.context["form"]["kind"].value() == "couple"
        assert str(page.context["form"]["counselor"].value()) == str(counselor.pk)
        assert page.context["form"]["find"].value() == "ashford"

    def test_somebody_already_ticked_stays_ticked_and_stays_offered(self, admin_client, roster):
        """The trap. Narrowing the list narrows what the form will accept, so a
        search for the second spouse must not throw the first one away."""
        admin_client.post(URL, draft(find="brooks", search="1", counselees=[roster["ada"].pk]))
        response = admin_client.get(URL)

        assert sorted(offered_names(response)) == ["Ada Ashford", "Cleo Brooks"]
        assert str(roster["ada"].pk) in str(response.context["form"]["counselees"].value())

    def test_a_case_can_be_opened_while_the_list_is_narrowed(self, admin_client, roster, counselor):
        """The same trap, at the moment it would actually bite: the submission that
        opens the case carries a search term and somebody the term does not match."""
        response = admin_client.post(
            URL,
            draft(
                find="brooks",
                counselor=counselor.pk,
                counselees=[roster["ada"].pk, roster["cleo"].pk],
            ),
        )

        assert response.status_code == 302, (
            f"the form should not have rejected the choice: {response.context['form'].errors}"
        )
        case = Case.objects.get()
        assert {member.counselee for member in case.members.all()} == {
            roster["ada"],
            roster["cleo"],
        }

    def test_a_hand_built_choice_is_an_empty_result_rather_than_an_error(
        self, admin_client, roster, counselor
    ):
        """The submitted ids reach a ``pk__in``, and nothing stops a crafted request
        putting a word there. That deserves a form error, not a 500."""
        response = admin_client.post(
            URL, draft(find="ashford", counselor=counselor.pk, counselees=["not-a-number"])
        )

        assert response.status_code == 200
        assert response.context["form"].errors["counselees"]


# --- leaving to create somebody -------------------------------------------


class TestCreatingACounseleeWithoutLosingTheCase:
    def test_the_way_out_is_a_submission_so_the_draft_can_be_kept(self, admin_client, roster):
        response = admin_client.post(URL, draft(create_counselee="1"))

        assert response.status_code == 302
        assert reverse("counseling:counselee_create") in response.headers["Location"]
        assert f"next={URL}" in response.headers["Location"].replace("%2F", "/")
        assert not Case.objects.exists()

    def test_coming_back_finds_the_case_as_it_was_left(self, admin_client, roster, counselor):
        admin_client.post(
            URL,
            draft(
                create_counselee="1",
                counselor=counselor.pk,
                counselees=[roster["ada"].pk],
                label="The Ashfords",
            ),
        )

        page = admin_client.get(URL)

        assert page.context["form"]["label"].value() == "The Ashfords"
        assert str(counselor.pk) == str(page.context["form"]["counselor"].value())

    def test_the_new_counselee_is_ticked_alongside_the_one_already_chosen(
        self, admin_client, roster, make_user
    ):
        """``?counselee=`` is how the New Counselee page hands somebody back. It adds
        to the draft rather than replacing it, or an administrator adding the second
        spouse would lose the first."""
        newcomer = new_counselee(make_user, "Dot", "Ashford")
        admin_client.post(URL, draft(create_counselee="1", counselees=[roster["ada"].pk]))

        page = admin_client.get(f"{URL}?counselee={newcomer.public_id}")

        chosen = {str(value) for value in page.context["form"]["counselees"].value()}
        assert chosen == {str(roster["ada"].pk), str(newcomer.pk)}

    def test_the_whole_journey(self, admin_client, roster, counselor):
        """Half-fill the page, go and create somebody, come back, open the case."""
        admin_client.post(
            URL,
            draft(
                create_counselee="1",
                counselor=counselor.pk,
                counselees=[roster["ada"].pk],
            ),
        )
        created = admin_client.post(
            f"{reverse('counseling:counselee_create')}?next={URL}",
            {
                "first_name": "Dot",
                "last_name": "Ashford",
                "email": "dot@example.org",
                "phone": "",
                "allow_magic_link": "on",
            },
        )
        landing = admin_client.get(created.headers["Location"])

        # Whatever the page came back ticked, submitted as a browser would submit it.
        opened = admin_client.post(
            URL,
            draft(
                counselor=counselor.pk,
                counselees=[str(value) for value in landing.context["form"]["counselees"].value()],
            ),
        )

        assert opened.status_code == 302
        case = Case.objects.get()
        assert {member.counselee.full_name for member in case.members.all()} == {
            "Ada Ashford",
            "Dot Ashford",
        }
        assert CaseMember.objects.count() == 2

    def test_a_draft_is_used_once_and_not_kept(self, admin_client, roster):
        """It is a page somebody was in the middle of, not a preference. Finding last
        Tuesday's abandoned case typed in would be worse than an empty form."""
        admin_client.post(URL, draft(create_counselee="1"))

        admin_client.get(URL)
        second_visit = admin_client.get(URL)

        assert second_visit.context["form"]["label"].value() in (None, "")
        assert CASE_DRAFT_SESSION_KEY not in admin_client.session

    def test_nobody_without_the_permission_can_leave_a_draft(self, client, sign_in, counselor):
        """The buttons are behind the same permission as the page itself."""
        sign_in(counselor)

        response = client.post(URL, draft(create_counselee="1"))

        assert response.status_code == 403
        assert CASE_DRAFT_SESSION_KEY not in client.session


# --- the page itself -------------------------------------------------------


class TestThePage:
    def test_every_field_on_the_form_reaches_the_page(self, admin_client, roster):
        """The page lays its fields out one by one so the search box can have its
        button beside it, which means a field added to the form can be left off it."""
        response = admin_client.get(URL)
        html = response.content.decode()

        for name in response.context["form"].fields:
            assert f'name="{name}"' in html, f"{name} is on the form but not on the page"

    def test_the_search_button_comes_before_the_one_that_opens_the_case(self, admin_client):
        """Enter in a text field presses the first submit button in the form.
        Searching again by accident is harmless; opening a case is not."""
        html = admin_client.get(URL).content.decode()

        assert html.index('name="search"') < html.index("Open case")

    def test_leaving_to_create_somebody_does_not_have_to_pass_validation_first(self, admin_client):
        """Both of the buttons that are not "Open case" skip the browser's own
        validation, or an empty label would stop somebody going to look for a name."""
        html = admin_client.get(URL).content.decode()

        for button in ("search", "create_counselee"):
            match = re.search(rf'<button[^>]*name="{button}"[^>]*>', html)
            assert match and "formnovalidate" in match.group(0), button

    def test_the_search_box_is_not_on_the_edit_page(self, admin_client, existing_case):
        """There is nobody to search for there: membership is changed from the case
        page, which audits it as the disclosure it is."""
        case = existing_case

        html = admin_client.get(
            reverse("counseling:case_edit", args=[case.public_id])
        ).content.decode()

        assert 'name="find"' not in html
        assert 'name="counselees"' not in html

    def test_the_edit_page_offers_a_way_out_as_well_as_a_save(self, admin_client, existing_case):
        """Somebody who opened it to read the notes has to be able to leave without
        wondering whether closing the tab counted as anything."""
        case = existing_case

        html = admin_client.get(
            reverse("counseling:case_edit", args=[case.public_id])
        ).content.decode()

        assert reverse("counseling:case_detail", args=[case.public_id]) in html
        assert "Go back" in html
