"""
The knowledge base: the shelf, the search, and the notes underneath.

What the ministry has learned used to live in people's heads and in four copies of
the same handout. This is the one shelf for it, and the promises worth asserting are:

  * **It is the counselors' own shelf.** Everybody who counsels may add to it,
    everybody may comment on anybody's contribution, and nobody needs an
    administrator's permission to be useful. That is what makes this a different
    feature from the template library, where an administrator decides what is
    current — see tests/test_document_templates.py.
  * **A counselee cannot reach it at all**, and neither can billing. The routes are
    asserted role by role in tests/test_access_matrix.py; what is here is the
    queryset behind them, because a refusal is only half of "this does not exist for
    you".
  * **Search is the feature, not the list.** "Anxiety" has to find the handout whose
    title never says it, including when the only place the word appears is a note a
    colleague left. Several tests below exist only for that sentence.
  * **A file on the shelf is stored like any other file** — scanned, encrypted, its
    own DEK — because there is one document volume and no unencrypted corner of it.
"""

from functools import cache

import pytest
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.audit.models import AuditEvent, AuditVerb
from apps.core.navigation import links_for
from apps.knowledge import services
from apps.knowledge.models import Resource, ResourceComment, ResourceKind
from tests.test_documents_ingest import real_pdf

pytestmark = pytest.mark.django_db

HOME = reverse("knowledge:home")


@cache
def a_pdf() -> bytes:
    """One PDF, generated once and reused — reportlab stamps a creation time into
    what it writes, so two calls do not produce the same bytes and a download test
    would have nothing stable to compare against."""
    return real_pdf()


@pytest.fixture
def contribute(counselor):
    """Something on the shelf, put there through the real service.

    A link by default, because most of the assertions below are about titles, search
    and comments, and a link-only resource is the cheap shape — no scanner, no blob.
    Pass ``upload`` for the tests that are about the file half.
    """

    def _contribute(title="Handling anxious thoughts", contributed_by=None, **kwargs):
        kwargs.setdefault("url", "https://example.org/anxious-thoughts")
        return services.store_resource(
            contributed_by=contributed_by or counselor,
            title=title,
            **kwargs,
        )

    return _contribute


@pytest.fixture
def counselor_client(client, sign_in, counselor):
    """A signed-in counselor.

    This and ``admin_client`` hand back the *same* Client — Django's test client is
    one object per test — so a test may use them one after the other, in the order it
    asks for them, but must not expect two live sessions.
    """
    sign_in(counselor)
    return client


@pytest.fixture
def admin_client(client, sign_in, admin_user):
    """A signed-in administrator. See counselor_client about the shared session."""
    sign_in(admin_user)
    return client


def page(client, url):
    response = client.get(url)
    assert response.status_code == 200, f"{url} answered {response.status_code}"
    return response.content.decode()


def titles_on(response) -> list[str]:
    return [resource.title for resource in response.context["resources"]]


class TestWhoTheShelfIsFor:
    def test_a_counselor_sees_everything_on_it(self, counselor, contribute):
        contribute()

        assert Resource.objects.for_actor(counselor).count() == 1

    def test_so_does_an_administrator(self, admin_user, contribute):
        contribute()

        assert Resource.objects.for_actor(admin_user).count() == 1

    def test_a_counselor_sees_a_colleagues_contribution(
        self, other_counselor, counselor, contribute
    ):
        """The whole point of the feature: a shelf only one person can read is a
        filing cabinet."""
        contribute(contributed_by=counselor)

        assert Resource.objects.for_actor(other_counselor).count() == 1

    def test_a_counselee_sees_nothing(self, counselee, contribute):
        """Not a filtered subset — nothing. A handout is no secret, but this shelf
        carries counselors' notes to each other about how counseling went, and none of
        that is written to be read by the person being counselled."""
        contribute()

        assert not Resource.objects.for_actor(counselee).exists()

    def test_nor_does_billing(self, financial_admin, contribute):
        contribute()

        assert not Resource.objects.for_actor(financial_admin).exists()

    def test_a_deactivated_counselor_sees_nothing(self, counselor, contribute):
        contribute()
        counselor.is_active = False

        assert not Resource.objects.for_actor(counselor).exists()

    def test_a_comment_is_only_as_visible_as_its_resource(self, counselor, counselee, contribute):
        """The whole access rule for a comment. There is no state on one that could
        make it more or less visible than the thing it is written on."""
        resource = contribute()
        services.add_comment(resource, author=counselor, body="Worked well.")

        assert ResourceComment.objects.for_actor(counselor).count() == 1
        assert not ResourceComment.objects.for_actor(counselee).exists()


class TestContributing:
    def test_a_counselor_adds_a_link(self, counselor_client, counselor):
        response = counselor_client.post(
            reverse("knowledge:add"),
            {
                "title": "Handling anxious thoughts",
                "summary": "A worksheet we hand out after a first session.",
                "topics": "anxiety, worry",
                "kind": ResourceKind.ARTICLE,
                "url": "https://example.org/anxious-thoughts",
            },
        )

        assert response.status_code == 302
        resource = Resource.objects.get()
        assert resource.contributed_by == counselor
        assert resource.has_link
        assert not resource.has_file

    def test_and_a_file(self, counselor_client):
        response = counselor_client.post(
            reverse("knowledge:add"),
            {
                "title": "Anxiety worksheet",
                "kind": ResourceKind.HANDOUT,
                "url": "",
                "file": SimpleUploadedFile("worksheet.pdf", a_pdf()),
            },
        )

        assert response.status_code == 302
        resource = Resource.objects.get()
        assert resource.has_file
        assert resource.content_type == "application/pdf"

    def test_or_both_at_once(self, counselor_client):
        """A link to the article and the handout printed from it are one resource, not
        two rows somebody has to notice are related."""
        counselor_client.post(
            reverse("knowledge:add"),
            {
                "title": "Anxiety worksheet",
                "kind": ResourceKind.HANDOUT,
                "url": "https://example.org/anxious-thoughts",
                "file": SimpleUploadedFile("worksheet.pdf", a_pdf()),
            },
        )

        resource = Resource.objects.get()
        assert resource.has_file and resource.has_link

    def test_an_administrator_may_contribute_too(self, admin_client):
        response = admin_client.post(
            reverse("knowledge:add"),
            {"title": "A talk on grief", "kind": ResourceKind.TRAINING, "url": "https://e.org/g"},
        )

        assert response.status_code == 302
        assert Resource.objects.count() == 1

    def test_a_counselee_cannot(self, client, sign_in, counselee):
        """Asserted with the write attempted rather than only the page, because the
        refusal has to happen before anything is stored."""
        sign_in(counselee)

        response = client.post(
            reverse("knowledge:add"),
            {"title": "Something", "kind": ResourceKind.OTHER, "url": "https://example.org/x"},
        )

        assert response.status_code == 403
        assert not Resource.objects.exists()

    def test_a_title_with_nothing_behind_it_is_refused(self, counselor_client):
        """Neither a file nor a link is not a resource — it is a title nobody can
        open. The error is on the form rather than on either field, because neither
        one of the two is the one that is missing."""
        response = counselor_client.post(
            reverse("knowledge:add"),
            {"title": "Something", "kind": ResourceKind.OTHER, "url": ""},
        )

        assert response.status_code == 200
        assert "Add a file or a link" in response.content.decode()
        assert not Resource.objects.exists()

    def test_a_file_type_nobody_accepts_is_refused(self, counselor_client):
        response = counselor_client.post(
            reverse("knowledge:add"),
            {
                "title": "Something",
                "kind": ResourceKind.OTHER,
                "url": "",
                "file": SimpleUploadedFile("macro.exe", b"MZ\x90\x00"),
            },
        )

        assert response.status_code == 200
        assert "not accepted" in response.content.decode()
        assert not Resource.objects.exists()

    def test_a_pasted_address_gets_a_scheme(self, counselor_client):
        """What a counselor copies out of a chat is ``example.org/article``. Refusing
        that would be pedantry; ``assume_scheme="https"`` completes it, and not to
        http on the way to Django 6 making that the default."""
        counselor_client.post(
            reverse("knowledge:add"),
            {"title": "An article", "kind": ResourceKind.ARTICLE, "url": "example.org/article"},
        )

        assert Resource.objects.get().url == "https://example.org/article"

    def test_the_file_is_stored_encrypted_like_anything_else(self, contribute):
        """The knowledge base is not the unencrypted corner of the document volume."""
        resource = contribute(upload=SimpleUploadedFile("worksheet.pdf", a_pdf()), url="")

        assert resource.storage_key is not None
        assert bytes(resource.wrapped_dek), "no wrapped DEK, so nothing was sealed"
        assert resource.sha256

    def test_a_link_only_resource_touches_no_storage(self, contribute):
        """The row and nothing else. Worth asserting because the null storage key is
        what the ``knowledge_resource_file_is_whole`` constraint is about."""
        resource = contribute()

        assert resource.storage_key is None
        assert not resource.sha256

    def test_contributing_is_recorded(self, contribute, counselor):
        resource = contribute(title="Handling anxious thoughts")

        event = AuditEvent.objects.get(verb=AuditVerb.KNOWLEDGE_RESOURCE_ADDED)
        assert event.actor == counselor
        assert event.target_type == "knowledge.Resource"
        assert event.target_id == str(resource.pk)
        assert event.metadata["title"] == "Handling anxious thoughts"


class TestFindingSomethingOnTheShelf:
    @pytest.fixture
    def shelf(self, contribute, counselor):
        """Three resources, and each one's link says only its own subject.

        Worth the explicit URLs: the search covers the link too, so leaving the
        fixture's default ``anxious-thoughts`` address on all three would have every
        search for "anxious" match everything and the tests below assert nothing.
        """
        anxiety = contribute(
            title="Handling anxious thoughts",
            summary="A worksheet for the spiral of what-ifs.",
            topics="anxiety, worry",
        )
        contribute(
            title="After a loss",
            summary="Reading for the first weeks of grief.",
            topics="grief",
            url="https://example.org/after-a-loss",
        )
        contribute(
            title="Communication in marriage",
            upload=SimpleUploadedFile("listening-exercise.pdf", a_pdf()),
            url="",
        )
        return anxiety

    def test_the_whole_shelf_is_listed_newest_first(self, counselor_client, shelf):
        """Newest first, unlike the template library's alphabetical shelf: this page
        is read to see what colleagues have added, and search is how somebody looks
        for one they already know about."""
        response = counselor_client.get(HOME)

        assert titles_on(response) == [
            "Communication in marriage",
            "After a loss",
            "Handling anxious thoughts",
        ]

    def test_searching_a_topic_finds_it(self, counselor_client, shelf):
        """The example from the request: looking for homework on anxiety, search
        "Anxiety", get what is relevant."""
        response = counselor_client.get(HOME, {"q": "Anxiety"})

        assert titles_on(response) == ["Handling anxious thoughts"]

    def test_the_summary_is_searched_too(self, counselor_client, shelf):
        response = counselor_client.get(HOME, {"q": "what-ifs"})

        assert titles_on(response) == ["Handling anxious thoughts"]

    def test_and_the_address_of_a_link(self, counselor_client, shelf):
        """Somebody who remembers where an article lives, not what it was called."""
        response = counselor_client.get(HOME, {"q": "after-a-loss"})

        assert titles_on(response) == ["After a loss"]

    def test_and_the_filename(self, counselor_client, shelf):
        response = counselor_client.get(HOME, {"q": "listening-exercise"})

        assert titles_on(response) == ["Communication in marriage"]

    def test_and_a_note_a_colleague_left(self, counselor_client, shelf, other_counselor):
        """The one that justifies searching comments at all: the useful sentence about
        a handout is often not the contributor's."""
        services.add_comment(
            shelf, author=other_counselor, body="I used this one for panic attacks."
        )

        response = counselor_client.get(HOME, {"q": "panic"})

        assert titles_on(response) == ["Handling anxious thoughts"]

    def test_a_removed_note_stops_pulling_it_into_results(
        self, counselor_client, shelf, counselor, other_counselor
    ):
        comment = services.add_comment(
            shelf, author=other_counselor, body="I used this one for panic attacks."
        )
        services.remove_comment(comment, actor=other_counselor)

        response = counselor_client.get(HOME, {"q": "panic"})

        assert titles_on(response) == []

    def test_three_matching_notes_are_still_one_resource(self, counselor_client, shelf, counselor):
        """``distinct()``. Without it a search would list the same handout once per
        comment, which reads as three handouts."""
        for _ in range(3):
            services.add_comment(shelf, author=counselor, body="Good for panic.")

        response = counselor_client.get(HOME, {"q": "panic"})

        assert titles_on(response) == ["Handling anxious thoughts"]

    def test_case_does_not_matter(self, counselor_client, shelf):
        response = counselor_client.get(HOME, {"q": "GRIEF"})

        assert titles_on(response) == ["After a loss"]

    def test_a_term_that_matches_nothing_says_so(self, counselor_client, shelf):
        response = counselor_client.get(HOME, {"q": "zzz"})

        assert titles_on(response) == []
        assert "matches" in response.content.decode()

    def test_an_empty_search_is_the_whole_shelf(self, counselor_client, shelf):
        response = counselor_client.get(HOME, {"q": "  "})

        assert len(titles_on(response)) == 3

    def test_the_shelf_says_how_many_notes_each_has(self, counselor_client, shelf, counselor):
        """Counted in the view, in one query, and filtered to live comments — the
        number is what tells a counselor that colleagues have said something."""
        services.add_comment(shelf, author=counselor, body="Worked well.")
        removed = services.add_comment(shelf, author=counselor, body="Ignore me.")
        services.remove_comment(removed, actor=counselor)

        response = counselor_client.get(HOME, {"q": "anxious"})

        assert [r.comment_count for r in response.context["resources"]] == [1]


class TestTalkingAboutOne:
    def test_a_counselor_leaves_a_note(self, counselor_client, contribute, counselor):
        resource = contribute()

        response = counselor_client.post(
            reverse("knowledge:comment", args=[resource.public_id]),
            {"body": "Used this with two cases; the second page is the useful one."},
            follow=True,
        )

        assert response.status_code == 200
        comment = ResourceComment.objects.get()
        assert comment.author == counselor
        assert comment.resource == resource

    def test_a_note_appears_on_the_page(self, counselor_client, contribute, other_counselor):
        resource = contribute()
        services.add_comment(resource, author=other_counselor, body="The second page is best.")

        markup = page(counselor_client, reverse("knowledge:detail", args=[resource.public_id]))

        assert "The second page is best." in markup

    def test_an_empty_note_is_not_stored(self, counselor_client, contribute):
        """Redirected with a message rather than re-rendered with a red field: an
        empty textarea somebody tabbed past does not need a scolding."""
        resource = contribute()

        response = counselor_client.post(
            reverse("knowledge:comment", args=[resource.public_id]), {"body": "   "}
        )

        assert response.status_code == 302
        assert not ResourceComment.objects.exists()

    def test_leaving_a_note_is_recorded_without_the_words(
        self, counselor_client, contribute, counselor
    ):
        """The count and not the text. A second copy of what somebody wrote, outside
        the rules that govern the first, is what the trail should not become."""
        resource = contribute()

        counselor_client.post(
            reverse("knowledge:comment", args=[resource.public_id]),
            {"body": "Used this with two cases."},
        )

        event = AuditEvent.objects.get(verb=AuditVerb.KNOWLEDGE_COMMENT_ADDED)
        assert event.actor == counselor
        assert event.metadata["resource_public_id"] == resource.public_id
        assert event.metadata["characters"] == len("Used this with two cases.")
        assert "Used this" not in str(event.metadata)

    def test_a_counselee_cannot_comment(self, client, sign_in, counselee, contribute):
        resource = contribute()
        sign_in(counselee)

        response = client.post(
            reverse("knowledge:comment", args=[resource.public_id]), {"body": "Hello?"}
        )

        # 404, not 403: the resource is not in their queryset, so the page they are
        # posting to does not exist as far as they are concerned.
        assert response.status_code == 404
        assert not ResourceComment.objects.exists()

    def test_the_author_removes_their_own_note(self, counselor_client, contribute, counselor):
        resource = contribute()
        comment = services.add_comment(resource, author=counselor, body="Second thoughts.")

        counselor_client.post(reverse("knowledge:comment_remove", args=[comment.public_id]))

        assert not ResourceComment.objects.exists()
        assert ResourceComment.all_objects.filter(pk=comment.pk).exists()
        assert AuditEvent.objects.filter(verb=AuditVerb.KNOWLEDGE_COMMENT_REMOVED).exists()

    def test_another_counselor_cannot_remove_it(
        self, client, sign_in, contribute, counselor, other_counselor
    ):
        """403 rather than 404: they can see the note — that is the point of a shared
        shelf — and what they may not do is take somebody else's down."""
        resource = contribute()
        comment = services.add_comment(resource, author=counselor, body="Worked well.")
        sign_in(other_counselor)

        response = client.post(reverse("knowledge:comment_remove", args=[comment.public_id]))

        assert response.status_code == 403
        assert ResourceComment.objects.filter(pk=comment.pk).exists()

    def test_an_administrator_can(self, admin_client, contribute, counselor, admin_user):
        """Somebody has to be able to take down a note that should not have been
        written, and it cannot be only the person who wrote it."""
        resource = contribute()
        comment = services.add_comment(resource, author=counselor, body="Indiscreet.")

        admin_client.post(reverse("knowledge:comment_remove", args=[comment.public_id]))

        assert not ResourceComment.objects.exists()
        event = AuditEvent.objects.get(verb=AuditVerb.KNOWLEDGE_COMMENT_REMOVED)
        # Recorded because an administrator taking down a colleague's note is exactly
        # the action an access review would ask about.
        assert event.metadata["by_the_author"] is False

    def test_the_contributor_cannot_remove_a_colleagues_note(
        self, client, sign_in, contribute, counselor, other_counselor
    ):
        """The permission is the author's or an administrator's, deliberately not the
        contributor's: somebody who could delete the notes on their own contribution
        could quietly remove a colleague's "this one did not go well"."""
        resource = contribute(contributed_by=counselor)
        comment = services.add_comment(resource, author=other_counselor, body="Did not land.")
        sign_in(counselor)

        response = client.post(reverse("knowledge:comment_remove", args=[comment.public_id]))

        assert response.status_code == 403
        assert ResourceComment.objects.filter(pk=comment.pk).exists()


class TestKeepingTheShelfTidy:
    def test_the_contributor_relabels_their_own(self, counselor_client, contribute):
        resource = contribute()

        response = counselor_client.post(
            reverse("knowledge:edit", args=[resource.public_id]),
            {
                "title": "Handling anxious thoughts (revised)",
                "summary": "",
                "topics": "anxiety",
                "kind": ResourceKind.HANDOUT,
                "url": resource.url,
            },
        )

        assert response.status_code == 302
        resource.refresh_from_db()
        assert resource.title == "Handling anxious thoughts (revised)"
        assert AuditEvent.objects.filter(verb=AuditVerb.KNOWLEDGE_RESOURCE_UPDATED).exists()

    def test_a_moved_article_can_be_repointed(self, counselor_client, contribute):
        """The link is editable where the file is not: a moved article is still the
        same article, and the notes underneath still describe it."""
        resource = contribute()

        counselor_client.post(
            reverse("knowledge:edit", args=[resource.public_id]),
            {
                "title": resource.title,
                "summary": "",
                "topics": "",
                "kind": ResourceKind.HANDOUT,
                "url": "https://example.org/moved",
            },
        )

        resource.refresh_from_db()
        assert resource.url == "https://example.org/moved"

    def test_the_file_cannot_be_swapped(self, counselor_client, contribute):
        """A new version of a handout is a new resource. Replacing the bytes under an
        existing title would make the notes underneath describe something nobody can
        see any more, and the recorded hash would name a file that no longer exists."""
        resource = contribute(upload=SimpleUploadedFile("worksheet.pdf", a_pdf()), url="")

        counselor_client.post(
            reverse("knowledge:edit", args=[resource.public_id]),
            {
                "title": resource.title,
                "summary": "",
                "topics": "",
                "kind": ResourceKind.HANDOUT,
                "url": "",
                "file": SimpleUploadedFile("other.pdf", a_pdf()),
            },
        )

        resource.refresh_from_db()
        assert resource.original_filename == "worksheet.pdf"

    def test_another_counselor_cannot_relabel_it(
        self, client, sign_in, contribute, counselor, other_counselor
    ):
        """403, not 404: a colleague can read it and may not rewrite it. Tidying
        somebody else's contribution is an administrator's job."""
        resource = contribute(contributed_by=counselor)
        sign_in(other_counselor)

        response = client.get(reverse("knowledge:edit", args=[resource.public_id]))

        assert response.status_code == 403

    def test_an_administrator_can(self, admin_client, contribute):
        resource = contribute()

        response = admin_client.get(reverse("knowledge:edit", args=[resource.public_id]))

        assert response.status_code == 200

    def test_removing_takes_it_off_the_shelf(self, counselor_client, contribute):
        resource = contribute()

        counselor_client.post(reverse("knowledge:remove", args=[resource.public_id]))

        assert not Resource.objects.exists()

    def test_but_does_not_destroy_it(self, counselor_client, contribute):
        """Soft, like every deletion here. Its comments go with it, and both come back
        if the wrong row was removed."""
        resource = contribute()

        counselor_client.post(reverse("knowledge:remove", args=[resource.public_id]))

        assert Resource.all_objects.filter(pk=resource.pk).exists()
        assert AuditEvent.objects.filter(verb=AuditVerb.KNOWLEDGE_RESOURCE_REMOVED).exists()

    def test_a_removed_resource_is_gone_from_the_shelf(self, counselor_client, contribute):
        resource = contribute()
        counselor_client.post(reverse("knowledge:remove", args=[resource.public_id]))

        response = counselor_client.get(HOME)

        assert titles_on(response) == []

    def test_and_cannot_be_opened_any_more(self, counselor_client, contribute):
        resource = contribute()
        counselor_client.post(reverse("knowledge:remove", args=[resource.public_id]))

        response = counselor_client.get(reverse("knowledge:detail", args=[resource.public_id]))

        assert response.status_code == 404


class TestOpeningTheFile:
    def test_a_counselor_downloads_a_handout(self, counselor_client, contribute):
        resource = contribute(upload=SimpleUploadedFile("worksheet.pdf", a_pdf()), url="")

        response = counselor_client.get(reverse("knowledge:download", args=[resource.public_id]))

        assert response.status_code == 200
        assert b"".join(response.streaming_content) == a_pdf()

    def test_the_download_is_recorded(self, counselor_client, contribute, counselor):
        resource = contribute(upload=SimpleUploadedFile("worksheet.pdf", a_pdf()), url="")

        counselor_client.get(reverse("knowledge:download", args=[resource.public_id]))

        event = AuditEvent.objects.get(verb=AuditVerb.KNOWLEDGE_RESOURCE_DOWNLOADED)
        assert event.actor == counselor
        assert event.target_id == str(resource.pk)

    def test_a_link_only_resource_has_nothing_to_download(self, counselor_client, contribute):
        """404 rather than an empty file: there is no file, and an empty download
        looks like a broken one."""
        resource = contribute()

        response = counselor_client.get(reverse("knowledge:download", args=[resource.public_id]))

        assert response.status_code == 404

    def test_the_service_refuses_an_actor_who_may_not_read_the_shelf(self, counselee, contribute):
        """The permission is re-checked where the ciphertext is opened, not only in
        the view: this is the function that produces plaintext, and it takes nobody's
        word for anything."""
        resource = contribute(upload=SimpleUploadedFile("worksheet.pdf", a_pdf()), url="")

        with pytest.raises(PermissionDenied):
            services.open_resource(resource, actor=counselee)


class TestTheWayIn:
    """A shelf nobody can click to does not exist. See tests/test_navigation.py."""

    def test_a_counselor_is_offered_the_knowledge_base(self, counselor_client, counselor):
        markup = page(counselor_client, reverse("core:home"))

        assert HOME in markup
        assert any(link.key == "knowledge" for link in links_for(counselor))

    def test_an_administrator_is_too(self, admin_client, admin_user):
        assert HOME in page(admin_client, reverse("core:home"))
        assert any(link.key == "knowledge" for link in links_for(admin_user))

    def test_a_counselee_is_not(self, client, sign_in, counselee):
        """Not merely unusable — unmentioned. A link to a page that answers 403 is a
        worse experience than no link, and it tells the counselee the shelf exists."""
        sign_in(counselee)

        assert HOME not in page(client, reverse("core:home"))
        assert not any(link.key == "knowledge" for link in links_for(counselee))

    def test_neither_is_billing(self, client, sign_in, financial_admin):
        sign_in(financial_admin)

        assert HOME not in page(client, reverse("core:home"))

    def test_the_shelf_offers_a_counselor_the_contribute_page(self, counselor_client):
        assert reverse("knowledge:add") in page(counselor_client, HOME)

    def test_a_resource_links_to_its_own_page(self, counselor_client, contribute):
        resource = contribute()

        markup = page(counselor_client, HOME)

        assert reverse("knowledge:detail", args=[resource.public_id]) in markup

    def test_the_page_of_a_file_resource_offers_the_download(self, counselor_client, contribute):
        resource = contribute(upload=SimpleUploadedFile("worksheet.pdf", a_pdf()), url="")

        markup = page(counselor_client, reverse("knowledge:detail", args=[resource.public_id]))

        assert reverse("knowledge:download", args=[resource.public_id]) in markup

    def test_the_page_of_a_link_resource_offers_the_link(self, counselor_client, contribute):
        resource = contribute()

        markup = page(counselor_client, reverse("knowledge:detail", args=[resource.public_id]))

        assert resource.url in markup
        assert reverse("knowledge:download", args=[resource.public_id]) not in markup
