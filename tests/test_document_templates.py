"""
The document template library, and what "using" one on a case means.

The ministry hands out the same paperwork over and over: an intake form, a set of
homework sheets, a handout on anxiety. Before this there was nowhere to keep them, so
they lived on whichever counselor's laptop had the latest copy — which is how two
counselors end up handing out two different intake forms.

So there is one shelf, and the promises worth asserting divide in three:

  * **Who the shelf is for.** Administrators put things on it and take them off;
    every counselor reads and downloads; a counselee cannot reach it at all and
    neither can billing. The refusals are in tests/test_access_matrix.py route by
    route — what is here is the queryset behind them, because a page that refuses is
    only half of "the library does not exist for you".
  * **Using one is a copy, not a reference.** This is the decision the whole feature
    rests on. The file lands on the case as an ordinary Document with its own key,
    its own name, and its own audit trail, so replacing the library's intake form
    next spring cannot rewrite what forty cases were given. Several tests here exist
    only to hold that line.
  * **The way in.** A library nobody can find is the bug tests/test_navigation.py was
    written about, so the "Use a template" button on a case's documents page is
    asserted here as well as the pages behind it.
"""

from functools import cache

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.audit.models import AuditEvent, AuditVerb
from apps.counseling.models import Case, CaseMember
from apps.documents import services
from apps.documents.models import Document, DocumentTemplate, Visibility
from tests.conftest import jpeg_bytes
from tests.test_documents_ingest import real_pdf

pytestmark = pytest.mark.django_db

LIBRARY = reverse("documents:template_library")


@cache
def a_pdf() -> bytes:
    """One PDF, generated once and reused.

    Cached rather than regenerated per call because reportlab stamps a creation time
    and a document id into what it writes: two calls to ``real_pdf()`` produce
    different bytes, and the tests below compare a copy on a case against what went
    into the library byte for byte.
    """
    return real_pdf()


@pytest.fixture
def case(counselor, counselee):
    case = Case.objects.create(counselor=counselor, label="Ashford — marriage")
    CaseMember.objects.create(case=case, counselee=counselee)
    return case


@pytest.fixture
def add_template(admin_user):
    """A template on the shelf, stored through the real service.

    A PDF by default, because a PDF is stored byte for byte: the copy-on-use tests
    compare what comes out of a case document against what went into the library, and
    a photograph would be re-encoded on the way through both.
    """

    def _add(name="Intake form", data=None, filename="intake.pdf", **kwargs):
        return services.store_template(
            uploaded_by=admin_user,
            upload=SimpleUploadedFile(filename, a_pdf() if data is None else data),
            name=name,
            **kwargs,
        )

    return _add


@pytest.fixture
def counselor_client(client, sign_in, counselor):
    """A signed-in counselor.

    Both of these hand back the *same* Client — Django's test client is one object per
    test, so signing in the second actor logs the first one out. A test may therefore
    use them one after the other, in the order it asks for them, but must not expect
    two sessions to be live at once.
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


def names_on(response) -> list[str]:
    return [template.name for template in response.context["templates"]]


class TestWhoTheShelfIsFor:
    """The queryset behind the refusals, asserted directly.

    The access matrix proves the routes refuse. This proves the *rows* are absent,
    which is the stronger claim and the one a future view gets for free.
    """

    def test_a_counselor_sees_every_template(self, counselor, add_template):
        add_template()

        assert DocumentTemplate.objects.for_actor(counselor).count() == 1

    def test_so_does_an_administrator(self, admin_user, add_template):
        add_template()

        assert DocumentTemplate.objects.for_actor(admin_user).count() == 1

    def test_a_counselee_sees_nothing(self, counselee, add_template):
        """Not a filtered subset — nothing. A template is not confidential, but the
        library is a staff workspace: what reaches a counselee is the copy a counselor
        deliberately put on their case."""
        add_template()

        assert not DocumentTemplate.objects.for_actor(counselee).exists()

    def test_nor_does_billing(self, financial_admin, add_template):
        add_template()

        assert not DocumentTemplate.objects.for_actor(financial_admin).exists()

    def test_a_deactivated_counselor_sees_nothing(self, counselor, add_template):
        """Inherited from ActorScopedQuerySet, and worth one assertion: disabling an
        account has to be enough on this model too."""
        add_template()
        counselor.is_active = False

        assert not DocumentTemplate.objects.for_actor(counselor).exists()


class TestAddingOne:
    def test_an_administrator_puts_a_form_on_the_shelf(self, admin_client):
        response = admin_client.post(
            reverse("documents:template_upload"),
            {
                "file": SimpleUploadedFile("intake.pdf", a_pdf()),
                "name": "Personal Data Inventory",
                "description": "The first-session form. Both spouses complete one.",
                "kind": "intake",
            },
        )

        assert response.status_code == 302
        template = DocumentTemplate.objects.get()
        assert template.name == "Personal Data Inventory"
        assert template.kind == "intake"
        assert template.content_type == "application/pdf"

    def test_it_is_stored_encrypted_like_anything_else(self, add_template):
        """The library is not the unencrypted corner of the document volume."""
        template = add_template()

        assert bytes(template.wrapped_dek), "no wrapped DEK, so nothing was sealed"
        assert template.storage_key is not None
        assert template.sha256

    def test_it_gets_a_preview(self, add_template):
        """A page of thumbnails is how somebody recognises the form they want."""
        template = add_template()

        assert template.has_thumbnail

    def test_adding_it_is_recorded(self, add_template):
        template = add_template(name="Homework — week two")

        event = AuditEvent.objects.get(verb=AuditVerb.DOCUMENT_TEMPLATE_ADDED)
        assert event.target_type == "documents.DocumentTemplate"
        assert event.target_id == str(template.pk)
        assert event.metadata["name"] == "Homework — week two"

    def test_a_file_type_nobody_accepts_is_refused(self, admin_client):
        response = admin_client.post(
            reverse("documents:template_upload"),
            {
                "file": SimpleUploadedFile("macro.exe", b"MZ\x90\x00"),
                "name": "Something else",
                "kind": "other",
            },
        )

        assert response.status_code == 200
        assert "not accepted" in response.content.decode()
        assert not DocumentTemplate.objects.exists()

    def test_a_template_with_no_name_falls_back_to_the_filename(self, admin_user):
        """The form insists on a name; the service does not, because a library row
        with no label at all would be unreachable rather than merely untidy."""
        template = services.store_template(
            uploaded_by=admin_user,
            upload=SimpleUploadedFile("consent.pdf", a_pdf()),
        )

        assert template.name == "consent.pdf"


class TestFindingSomethingOnTheShelf:
    @pytest.fixture
    def shelf(self, add_template):
        add_template(name="Intake form", description="For a first session.")
        add_template(
            name="Homework — week two",
            description="Reading on anxiety, with questions.",
            filename="anxiety-week-2.pdf",
        )
        add_template(name="Consent to counsel", filename="consent.pdf")

    def test_the_whole_shelf_is_listed_by_name(self, counselor_client, shelf):
        """Alphabetical, not newest first: a library is read by looking for something,
        and the most recent addition is nobody's first guess."""
        response = counselor_client.get(LIBRARY)

        assert names_on(response) == ["Consent to counsel", "Homework — week two", "Intake form"]

    def test_searching_narrows_it_by_name(self, counselor_client, shelf):
        response = counselor_client.get(LIBRARY, {"q": "intake"})

        assert names_on(response) == ["Intake form"]

    def test_and_by_what_the_template_is_about(self, counselor_client, shelf):
        """The description is searched too, which is what makes "anxiety" find a
        handout that does not have the word in its name."""
        response = counselor_client.get(LIBRARY, {"q": "anxiety"})

        assert names_on(response) == ["Homework — week two"]

    def test_and_by_the_filename_somebody_remembers(self, counselor_client, shelf):
        response = counselor_client.get(LIBRARY, {"q": "consent.pdf"})

        assert names_on(response) == ["Consent to counsel"]

    def test_case_does_not_matter(self, counselor_client, shelf):
        response = counselor_client.get(LIBRARY, {"q": "INTAKE"})

        assert names_on(response) == ["Intake form"]

    def test_a_term_that_matches_nothing_says_so(self, counselor_client, shelf):
        """Rather than an empty table, which reads as the page being broken."""
        response = counselor_client.get(LIBRARY, {"q": "zzz"})

        assert names_on(response) == []
        assert "matches" in response.content.decode()

    def test_an_empty_search_is_the_whole_shelf(self, counselor_client, shelf):
        response = counselor_client.get(LIBRARY, {"q": "   "})

        assert len(names_on(response)) == 3


class TestUsingOneOnACase:
    """The half of the feature that touches a counseling record."""

    def use(self, client, case, template, **overrides):
        payload = {"name": "Intake form", "visibility": Visibility.CASE_SHARED} | overrides
        return client.post(
            reverse("documents:template_use", args=[case.public_id, template.public_id]),
            payload,
            follow=True,
        )

    def test_a_counselor_puts_a_copy_on_their_case(self, counselor_client, case, add_template):
        template = add_template()

        response = self.use(counselor_client, case, template, name="Intake — the Ashfords")

        assert response.status_code == 200
        document = Document.objects.get(case=case)
        assert document.title == "Intake — the Ashfords"
        assert document.kind == template.kind

    def test_the_copy_holds_the_same_bytes(self, counselor_client, case, add_template):
        template = add_template()

        self.use(counselor_client, case, template)

        document = Document.objects.get(case=case)
        frames = services.open_document(document, actor=document.owner)
        assert b"".join(frames) == a_pdf()

    def test_but_not_the_same_key_or_the_same_blob(self, counselor_client, case, add_template):
        """Its own DEK and its own storage key, so the two share no keystream and
        withdrawing one cannot take the other's bytes with it."""
        template = add_template()

        self.use(counselor_client, case, template)

        document = Document.objects.get(case=case)
        assert document.storage_key != template.storage_key
        assert bytes(document.wrapped_dek) != bytes(template.wrapped_dek)

    def test_it_is_shared_with_the_case_by_default(self, counselor_client, case, add_template):
        """The opposite of an upload's default, on purpose: a template arrives on a
        case because somebody is giving it to somebody. A worksheet nobody can open is
        not homework."""
        template = add_template()

        self.use(counselor_client, case, template)

        assert Document.objects.get(case=case).is_shared_with_the_case

    def test_and_the_counselee_can_then_read_it(
        self, counselor_client, client, sign_in, case, counselee, add_template
    ):
        """The point of the whole feature, asserted end to end."""
        template = add_template()
        self.use(counselor_client, case, template)
        document = Document.objects.get(case=case)

        counselor_client.logout()
        sign_in(counselee)

        assert client.get(reverse("documents:detail", args=[document.public_id])).status_code == 200

    def test_a_counselor_can_still_keep_it_to_themselves(
        self, counselor_client, case, add_template
    ):
        template = add_template()

        self.use(counselor_client, case, template, visibility=Visibility.PRIVATE)

        assert not Document.objects.get(case=case).is_shared_with_the_case

    def test_the_copy_is_owned_by_whoever_used_it(
        self, counselor_client, case, counselor, add_template
    ):
        """Not by the administrator who put it in the library: the counselor is the one
        who handed it over, and provenance on a case is about that act."""
        template = add_template()

        self.use(counselor_client, case, template)

        assert Document.objects.get(case=case).owner == counselor

    def test_using_one_is_recorded_against_the_template_and_the_case(
        self, counselor_client, case, counselor, add_template
    ):
        template = add_template()

        self.use(counselor_client, case, template)

        document = Document.objects.get(case=case)
        event = AuditEvent.objects.get(verb=AuditVerb.DOCUMENT_TEMPLATE_USED)
        assert event.actor == counselor
        assert event.target_id == str(template.pk)
        assert event.case_id_snapshot == str(case.pk)
        # The join between the two rows, and the only record of it: the copy can be
        # renamed, so its title proves nothing about where it came from.
        assert event.metadata["document_public_id"] == document.public_id

    def test_the_copy_is_also_recorded_as_an_upload(self, counselor_client, case, add_template):
        """It went through ``store_document`` like anything else, and the trail says so
        — a document on a case with no record of arriving is the one state to avoid."""
        template = add_template()

        self.use(counselor_client, case, template)

        assert AuditEvent.objects.filter(verb=AuditVerb.DOCUMENT_UPLOADED).exists()

    def test_a_counselee_cannot_pull_one_onto_their_own_case(
        self, client, sign_in, counselee, case, add_template
    ):
        """They may upload to this case and may still not reach the library. Asserted
        with the write attempted, not only the page: the refusal has to happen before
        anything is stored."""
        template = add_template()
        sign_in(counselee)

        response = self.use(client, case, template)

        assert response.status_code == 403
        assert not Document.objects.filter(case=case).exists()

    def test_another_counselors_case_is_not_even_visible(
        self, client, sign_in, other_counselor, case, add_template
    ):
        """404 rather than 403: the case is not in their queryset, so its existence is
        not confirmed. The template is resolved after the case for exactly this
        reason."""
        template = add_template()
        sign_in(other_counselor)

        response = client.post(
            reverse("documents:template_use", args=[case.public_id, template.public_id]),
            {"name": "Intake form", "visibility": Visibility.CASE_SHARED},
        )

        assert response.status_code == 404
        assert not Document.objects.exists()


class TestTheCopyIsIndependent:
    """Three ways the library could have reached back into a counseling record.

    All three are why ``use_template`` copies instead of pointing.
    """

    @pytest.fixture
    def used(self, counselor_client, case, add_template):
        template = add_template(name="Intake form")
        counselor_client.post(
            reverse("documents:template_use", args=[case.public_id, template.public_id]),
            {"name": "Intake — the Ashfords", "visibility": Visibility.CASE_SHARED},
        )
        return template, Document.objects.get(case=case)

    def test_renaming_the_template_leaves_the_case_document_alone(self, used, admin_client):
        template, document = used

        admin_client.post(
            reverse("documents:template_edit", args=[template.public_id]),
            {"name": "Personal Data Inventory", "description": "", "kind": "intake"},
        )

        document.refresh_from_db()
        assert document.title == "Intake — the Ashfords"

    def test_withdrawing_the_template_leaves_it_readable(self, used, admin_client):
        """The one that matters most: an administrator tidying the shelf must not take
        a document off somebody's case."""
        template, document = used

        admin_client.post(reverse("documents:template_withdraw", args=[template.public_id]))

        frames = services.open_document(document, actor=document.owner)
        assert b"".join(frames) == a_pdf()

    def test_renaming_the_copy_leaves_the_template_alone(self, used, counselor_client):
        template, document = used

        counselor_client.post(
            reverse("documents:edit", args=[document.public_id]),
            {"title": "Something else entirely", "description": "", "kind": "intake"},
        )

        template.refresh_from_db()
        assert template.name == "Intake form"


class TestKeepingTheShelfTidy:
    def test_an_administrator_relabels_a_template(self, admin_client, add_template):
        template = add_template(name="Intake form")

        response = admin_client.post(
            reverse("documents:template_edit", args=[template.public_id]),
            {
                "name": "Personal Data Inventory",
                "description": "For a first session.",
                "kind": "intake",
            },
        )

        assert response.status_code == 302
        template.refresh_from_db()
        assert template.name == "Personal Data Inventory"
        assert AuditEvent.objects.filter(verb=AuditVerb.DOCUMENT_TEMPLATE_UPDATED).exists()

    def test_withdrawing_takes_it_off_the_shelf(self, admin_client, add_template):
        template = add_template()

        admin_client.post(reverse("documents:template_withdraw", args=[template.public_id]))

        assert not DocumentTemplate.objects.exists()

    def test_but_does_not_destroy_it(self, admin_client, add_template):
        """Soft, like every deletion here: the audit rows saying it was copied onto a
        case have to keep pointing at something that can still be named."""
        template = add_template()

        admin_client.post(reverse("documents:template_withdraw", args=[template.public_id]))

        assert DocumentTemplate.all_objects.filter(pk=template.pk).exists()
        assert AuditEvent.objects.filter(verb=AuditVerb.DOCUMENT_TEMPLATE_WITHDRAWN).exists()

    def test_a_withdrawn_template_cannot_be_used_any_more(
        self, counselor_client, admin_user, case, add_template
    ):
        """Withdrawn through the service rather than the admin's page, because the
        actor being asserted is the counselor: the two clients share one session."""
        template = add_template()
        services.withdraw_template(template, actor=admin_user)

        response = counselor_client.get(
            reverse("documents:template_use", args=[case.public_id, template.public_id])
        )

        assert response.status_code == 404

    def test_the_file_itself_cannot_be_swapped(self, admin_client, add_template):
        """A new version of a form is a new template. Replacing the bytes under one
        name would silently change what every counselor thought they were handing
        out, and the recorded hash would describe something that no longer exists."""
        template = add_template()

        admin_client.post(
            reverse("documents:template_edit", args=[template.public_id]),
            {
                "name": "Intake form",
                "description": "",
                "kind": "intake",
                "file": SimpleUploadedFile("other.jpg", jpeg_bytes()),
            },
        )

        template.refresh_from_db()
        assert template.content_type == "application/pdf"
        assert template.original_filename == "intake.pdf"


class TestDownloadingOneToPrint:
    def test_a_counselor_gets_the_file(self, counselor_client, add_template):
        template = add_template()

        response = counselor_client.get(
            reverse("documents:template_download", args=[template.public_id])
        )

        assert response.status_code == 200
        assert b"".join(response.streaming_content) == a_pdf()

    def test_the_download_is_recorded(self, counselor_client, counselor, add_template):
        """Not because a template is confidential, but because the library is a set of
        files the ministry hands to people, and who took what out of it is part of the
        record."""
        template = add_template()

        counselor_client.get(reverse("documents:template_download", args=[template.public_id]))

        event = AuditEvent.objects.get(verb=AuditVerb.DOCUMENT_TEMPLATE_DOWNLOADED)
        assert event.actor == counselor
        assert event.target_id == str(template.pk)

    def test_the_service_refuses_an_actor_who_may_not_read_the_library(
        self, counselee, add_template
    ):
        """The permission is re-checked where the ciphertext is opened, not only in the
        view: this is the function that produces plaintext and it takes nobody's word."""
        from django.core.exceptions import PermissionDenied

        template = add_template()

        with pytest.raises(PermissionDenied):
            services.open_template(template, actor=counselee)


class TestTheWayIn:
    """A library nobody can click to does not exist. See tests/test_navigation.py."""

    def test_a_case_offers_a_counselor_the_library(self, counselor_client, case):
        markup = page(counselor_client, reverse("documents:case_documents", args=[case.public_id]))

        assert LIBRARY in markup

    def test_and_carries_the_case_with_it(self, counselor_client, case):
        markup = page(counselor_client, reverse("documents:case_documents", args=[case.public_id]))

        assert f"{LIBRARY}?case={case.public_id}" in markup

    def test_a_counselee_is_offered_nothing_of_the_kind(self, client, sign_in, counselee, case):
        """Hiding the link is a courtesy and the permission is the control — but
        offering a counselee a button that 403s would be inviting the refusal."""
        sign_in(counselee)

        markup = page(client, reverse("documents:case_documents", args=[case.public_id]))

        assert LIBRARY not in markup

    def test_the_library_opened_for_a_case_offers_to_use_a_template(
        self, counselor_client, case, add_template
    ):
        template = add_template()

        markup = page(counselor_client, f"{LIBRARY}?case={case.public_id}")

        use = reverse("documents:template_use", args=[case.public_id, template.public_id])
        assert use in markup

    def test_opened_on_its_own_it_only_offers_a_download(self, counselor_client, add_template):
        add_template()

        markup = page(counselor_client, LIBRARY)

        assert "Use on this case" not in markup
        assert "Download" in markup

    def test_another_counselors_case_buys_nothing(
        self, client, sign_in, other_counselor, case, add_template
    ):
        """``?case=`` is request-controlled, so the case is resolved through
        ``Case.objects.for_actor`` and permission-checked. All a copied URL can do is
        offer buttons the actor was already entitled to."""
        add_template()
        sign_in(other_counselor)

        response = client.get(LIBRARY, {"case": case.public_id})

        assert response.status_code == 200
        assert response.context["case"] is None
        assert "Use on this case" not in response.content.decode()

    def test_a_nonsense_case_is_ignored_rather_than_refused(self, counselor_client, add_template):
        """The library is worth landing on either way; a 404 for a stale link would
        hide the shelf as well as the buttons."""
        add_template()

        response = counselor_client.get(LIBRARY, {"case": "not-a-public-id"})

        assert response.status_code == 200
        assert response.context["case"] is None

    def test_an_administrator_is_offered_the_upload_page(self, admin_client):
        assert reverse("documents:template_upload") in page(admin_client, LIBRARY)

    def test_a_counselor_is_not(self, counselor_client):
        """Only administrators decide which intake form is the current one, so the
        button is not there to be clicked and refused."""
        assert reverse("documents:template_upload") not in page(counselor_client, LIBRARY)
