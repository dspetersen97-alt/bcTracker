"""
The Back link that every page but Home carries.

There is no JavaScript in this application, so Back cannot ask the browser for its
history: it is a link with a real ``href``, built from the ``Referer`` header. That
makes it a header-driven link rendered onto every authenticated page, which is worth
testing carefully for one reason above all the others — a link built from a
request-controlled value without a host check is an open redirect, and an open
redirect on a signed-in page is what a phishing message is looking for.

So the promises asserted here are, in order of how much they matter:

  * Back never points off this site.
  * Back is absent rather than broken when there is nowhere to go: a page opened from
    a bookmark, a form that came back to itself, Home, and a session that has given a
    password and not yet a code.
  * Back goes where somebody came from, query string and all, because a filtered list
    that comes back unfiltered is not where they were.
"""

import re

import pytest
from django.urls import reverse

from apps.counseling.models import Case
from tests.conftest import TEST_PASSWORD

CASES = reverse("counseling:case_list")
HOME = reverse("core:home")

pytestmark = pytest.mark.django_db


def back_link(page: str) -> str | None:
    """The Back link's markup, or ``None`` if the page does not offer one."""
    match = re.search(r'<p class="back">.*?</p>', page, re.DOTALL)
    return match.group(0) if match else None


def target_of(page: str) -> str | None:
    link = back_link(page)
    if link is None:
        return None
    href = re.search(r'href="([^"]*)"', link)
    assert href, "the Back link has no href — it is not a link at all"
    return href.group(1)


@pytest.fixture
def admin_client(client, sign_in, admin_user):
    sign_in(admin_user)
    return client


@pytest.fixture
def case(counselor):
    return Case.objects.create(counselor=counselor, label="The Ashfords")


class TestWhereBackGoes:
    def test_a_page_reached_from_another_offers_the_way_back(self, admin_client, case):
        detail = reverse("counseling:case_detail", args=[case.public_id])

        page = admin_client.get(detail, headers={"referer": f"http://testserver{CASES}"})

        assert target_of(page.content.decode()) == CASES

    def test_the_previous_page_keeps_its_query(self, admin_client, case):
        """A list that comes back unfiltered is not the page somebody left."""
        detail = reverse("counseling:case_detail", args=[case.public_id])

        page = admin_client.get(
            detail, headers={"referer": f"http://testserver{CASES}?status=closed"}
        )

        assert target_of(page.content.decode()) == f"{CASES}?status=closed"

    def test_it_is_a_relative_link_and_nothing_else(self, admin_client, case):
        """Only the path and query survive, so the href cannot be an absolute URL to
        anywhere — a second line of defence behind the host check."""
        detail = reverse("counseling:case_detail", args=[case.public_id])

        page = admin_client.get(detail, headers={"referer": f"http://testserver{CASES}"})

        assert target_of(page.content.decode()).startswith("/")
        assert "testserver" not in back_link(page.content.decode())


class TestWhenThereIsNowhereToGo:
    def test_home_does_not_offer_it(self, admin_client):
        """Back on Home would be a button that leaves the place everything else goes
        back to, and the sidebar already has Home on it."""
        page = admin_client.get(HOME, headers={"referer": f"http://testserver{CASES}"})

        assert back_link(page.content.decode()) is None

    def test_a_page_opened_cold_offers_nothing(self, admin_client, case):
        """A bookmark, a link from an email, a typed address. Absent rather than
        pointing at Home: a Back that sometimes means Home has to be learned by
        pressing it."""
        detail = reverse("counseling:case_detail", args=[case.public_id])

        page = admin_client.get(detail)

        assert back_link(page.content.decode()) is None

    def test_a_form_that_came_back_to_itself_does_not_offer_it(self, admin_client, counselor):
        """A failed submission is posted from the page it re-renders, so its referer is
        its own URL. Back there would reload the form and lose what was typed."""
        url = reverse("counseling:case_create")

        page = admin_client.post(
            url,
            {"label": "", "counselor": counselor.pk, "kind": "individual"},
            headers={"referer": f"http://testserver{url}"},
        )

        assert page.status_code == 200
        assert back_link(page.content.decode()) is None

    def test_the_code_prompt_offers_nothing(self, client, counselor, enrol_totp):
        """Between the password and the TOTP code there is nowhere to go back to —
        every page bounces to the prompt — which is the same reason the sidebar is not
        rendered there either."""
        enrol_totp(counselor)
        client.post("/login/", {"username": counselor.email, "password": TEST_PASSWORD})

        page = client.get(
            reverse("accounts:mfa_verify"), headers={"referer": f"http://testserver{CASES}"}
        )

        assert back_link(page.content.decode()) is None


class TestWhatItWillNotBeTalkedInto:
    @pytest.mark.parametrize(
        "referer",
        [
            "https://phish.example.org/sign-in",
            "//phish.example.org/sign-in",
            "http://testserver.phish.example.org/cases/",
            "javascript:alert(1)",
        ],
    )
    def test_a_referer_from_anywhere_else_is_refused(self, admin_client, case, referer):
        """The header is request-controlled, so this link is an open redirect on every
        signed-in page if the host is not checked."""
        detail = reverse("counseling:case_detail", args=[case.public_id])

        page = admin_client.get(detail, headers={"referer": referer})

        assert back_link(page.content.decode()) is None
        assert "phish.example.org" not in page.content.decode()
