"""
A deliberate attempt to defeat the access model.

The rest of the suite asks "does each role get what it should". This file asks the
opposite question — given that the matrix is right, what is the way *round* it —
and it does not re-assert anything tests/test_access_matrix.py already covers.
The attacks are grouped by the layer they aim at:

  * **the session**, not the role: a staff member who has typed a password but no
    TOTP code, and an account switched off while it was signed in. Both hold a
    valid session cookie belonging to a real counselor, so every per-role test in
    the suite passes for them.
  * **the form**, not the view: a field nobody was offered — ``role``,
    ``is_superuser``, another person's profile id — posted to a page the actor is
    perfectly entitled to use.
  * **the redirect**, not the page: the ``?next=`` parameter and the ``Host``
    header, which decide where a signed-in browser and an emailed link end up.
  * **the id**, not the route: a pk that is legitimate somewhere else in the
    system, used where its parent does not own it.
  * **the code**, not the request: two structural tripwires that fail when a
    future view reads around the scoping layer or a future form exposes a
    privilege field, because those are the changes that would make the whole
    matrix wrong at once, and neither shows up as a failing assertion anywhere.

Two invariants about the matrix *itself* are asserted at the end. The matrix is a
hand-written table, and the failure mode of a hand-written table is somebody
widening a cell to make a test pass.

What is deliberately not attempted here: SQL injection, template injection, and
CSRF, which are the framework's job and are not weakened by anything in this
codebase; and the crypto, which tests/test_documents_crypto.py attacks directly.
"""

import ast
from pathlib import Path

import pytest
from django.conf import settings
from django.core import mail
from django.urls import reverse

from apps.accounts.middleware import MFA_EXEMPT_URL_NAMES
from apps.accounts.models import Role
from apps.counseling.models import Case, CaseMember, CounseleeProfile
from tests.conftest import TEST_PASSWORD
from tests.test_access_matrix import MATRIX
from tests.test_scoping_contract import UNSCOPED_BY_DESIGN, needs_scoping, our_models

pytestmark = pytest.mark.django_db

#: Routes that answer a signed-in counselor with a page or an action, taken from
#: the matrix rather than listed again. Anything that already refuses them cannot
#: leak, so these are the rows worth attacking — and a route added to the matrix
#: with a 200 is swept from the moment it lands.
SERVED_TO_A_COUNSELOR = sorted(
    name for name, (_, _, expected) in MATRIX.items() if expected["counselor"] in (200, 302)
)

#: The same set minus the routes the MFA gate lets through by name — the login
#: form, the code form, and signing out. They are not holes: each either ends the
#: session or advances the enrolment, and the list itself is pinned below so it
#: cannot grow without this file being edited too.
GATED_FOR_A_COUNSELOR = [name for name in SERVED_TO_A_COUNSELOR if name not in MFA_EXEMPT_URL_NAMES]


def _kwargs_for(route, scenario, actor):
    kwargs, method, _ = MATRIX[route]
    if callable(kwargs):
        kwargs = kwargs(scenario(actor))
    return kwargs, method


class TestASessionThatStillOwesASecondFactor:
    """Password accepted, TOTP not yet entered.

    This is the most dangerous session state in the application, because the user
    *is* authenticated: ``request.user`` is a real counselor, every predicate is
    true of them, and every queryset scopes to their own caseload. Only the
    middleware stands in the way, so it is swept over every route rather than
    spot-checked — a view that installed its own decorator and bypassed the
    middleware would look correct in the matrix and be wrong here.
    """

    @pytest.fixture
    def half_signed_in(self, client, enrol_totp):
        def _half(user):
            # A confirmed device already exists, which is the state of a counselor
            # who has been using the system: the gate sends them to the code form
            # rather than to enrolment.
            enrol_totp(user)
            response = client.post("/login/", {"username": user.email, "password": TEST_PASSWORD})
            assert response.status_code == 302, "password login should have succeeded"
            return client

        return _half

    def test_the_password_alone_really_does_authenticate(self, half_signed_in, counselor):
        """The premise of everything below.

        If the password step left the session anonymous, the sweep would be
        asserting that anonymous visitors are refused — which is true and useless.
        """
        client = half_signed_in(counselor)

        assert client.session["_auth_user_id"] == str(counselor.pk)

    @pytest.mark.parametrize("route", GATED_FOR_A_COUNSELOR)
    def test_no_route_serves_them_anything(
        self, route, client, counselor, half_signed_in, scenario
    ):
        kwargs, method = _kwargs_for(route, scenario, counselor)
        half_signed_in(counselor)

        response = getattr(client, method)(reverse(route, kwargs=kwargs))

        assert response.status_code == 302, (
            f"{method.upper()} {route} answered {response.status_code} for a session "
            "that has not passed TOTP; it should have been sent to the code form"
        )
        assert response["Location"] in {
            reverse("accounts:mfa_verify"),
            reverse("accounts:mfa_setup"),
        }, f"{route} redirected to {response['Location']} rather than the second factor"

    def test_an_action_is_not_performed_on_the_way_past_the_gate(
        self, client, counselor, half_signed_in, scenario
    ):
        """A redirect is not enough on its own: the middleware must run *before* the
        view, not after it has already done the work."""
        objects = scenario(counselor)
        half_signed_in(counselor)

        client.post(reverse("counseling:case_close", kwargs={"pk": objects.case.pk}))

        objects.case.refresh_from_db()
        assert objects.case.closed_on is None

    def test_the_exemption_list_has_not_grown(self):
        """Every name here runs for a half-authenticated session, so adding one is a
        decision that belongs in this file as well as in the middleware."""
        assert set(MFA_EXEMPT_URL_NAMES) == {
            "accounts:mfa_setup",
            "accounts:mfa_verify",
            "accounts:logout",
            "accounts:login",
            "healthz",
        }


class TestASessionWhoseAccountWasSwitchedOff:
    """Deactivation must take effect on the next request, not the next login.

    Switching an account off is what the ministry does when someone leaves, and it
    is the only lever available in a hurry. If a live session survived it, the
    lever would do nothing for whoever is already signed in — which is precisely
    the person it is aimed at.

    The assertion is that they get *exactly what an anonymous visitor gets*, taken
    from the matrix's own anonymous column, so there is one definition of "no
    session" in the suite rather than two.
    """

    @pytest.fixture
    def switched_off(self, client, sign_in):
        def _switch_off(user):
            sign_in(user)
            # Through the queryset, so nothing here depends on a model save hook
            # having flushed the session.
            type(user).objects.filter(pk=user.pk).update(is_active=False)
            return client

        return _switch_off

    def test_the_cookie_is_still_there(self, switched_off, counselor, client):
        """The premise: this is a deactivated account holding a valid session, not a
        browser that was signed out."""
        switched_off(counselor)

        assert client.session.get("_auth_user_id") == str(counselor.pk)

    @pytest.mark.parametrize("route", SERVED_TO_A_COUNSELOR)
    def test_every_route_answers_as_though_nobody_is_signed_in(
        self, route, client, counselor, switched_off, scenario
    ):
        kwargs, method = _kwargs_for(route, scenario, counselor)
        _, _, expected = MATRIX[route]
        switched_off(counselor)

        response = getattr(client, method)(reverse(route, kwargs=kwargs))

        assert response.status_code == expected["anonymous"], (
            f"{method.upper()} {route} answered {response.status_code} for a "
            f"deactivated session; an anonymous visitor gets {expected['anonymous']}"
        )
        if response.status_code == 302:
            assert "/login/" in response["Location"], (
                f"{route} redirected a deactivated session to "
                f"{response['Location']} rather than the login form"
            )

    def test_they_cannot_still_act_on_their_own_case(
        self, client, counselor, switched_off, scenario
    ):
        objects = scenario(counselor)
        switched_off(counselor)

        client.post(reverse("counseling:case_close", kwargs={"pk": objects.case.pk}))

        objects.case.refresh_from_db()
        assert objects.case.closed_on is None


class TestFieldsNobodyWasOffered:
    """Posting what the form does not render.

    Every one of these posts is to a page the actor is entitled to use, with their
    own valid data plus one extra field. A ``ModelForm`` with an explicit
    ``fields`` list ignores the extra, which is why these pass — and why the
    structural test further down exists to keep that list explicit.
    """

    def test_a_counselee_cannot_promote_themselves_on_their_own_intake_form(
        self, client, counselee, sign_in
    ):
        sign_in(counselee)

        client.post(
            reverse("counseling:counselee_profile_edit"),
            {
                "emergency_contact_name": "Ruth Ashford",
                "role": Role.ADMIN,
                "is_superuser": "on",
                "is_staff": "on",
                "mfa_required": "",
            },
        )

        counselee.refresh_from_db()
        assert counselee.role == Role.COUNSELEE
        assert counselee.is_superuser is False
        assert counselee.is_staff is False
        # The legitimate part of the post still went through, so this is not passing
        # because the whole form was rejected.
        assert counselee.counselee_profile.emergency_contact_name == "Ruth Ashford"

    def test_a_counselee_cannot_write_to_someone_elses_intake_by_naming_them(
        self, client, make_user, sign_in
    ):
        attacker = make_user(Role.COUNSELEE)
        victim = make_user(Role.COUNSELEE)
        victim_profile = CounseleeProfile.objects.create(user=victim, address="17 Elm Row")
        sign_in(attacker)

        client.post(
            reverse("counseling:counselee_profile_edit"),
            {"address": "somewhere else", "user": victim.pk, "id": victim_profile.pk},
        )

        victim_profile.refresh_from_db()
        assert victim_profile.address == "17 Elm Row"
        assert attacker.counselee_profile.address == "somewhere else"

    def test_a_counselor_cannot_edit_another_counselors_practice_settings_by_id(
        self, client, counselor, other_counselor, sign_in
    ):
        from apps.counseling.models import CounselorProfile

        victim = CounselorProfile.objects.create(user=other_counselor, credentials="ACBC")
        sign_in(counselor)

        client.post(
            reverse("counseling:counselor_profile_edit"),
            {
                "credentials": "self-taught",
                "default_session_minutes": 60,
                "booking_notice_hours": 24,
                "booking_horizon_days": 60,
                "user": other_counselor.pk,
                "id": victim.pk,
            },
        )

        victim.refresh_from_db()
        assert victim.credentials == "ACBC"

    def test_changing_a_password_cannot_change_a_role(self, client, counselee, sign_in):
        sign_in(counselee)

        client.post(
            reverse("accounts:password_change"),
            {
                "old_password": TEST_PASSWORD,
                "new_password1": "a-quite-different-password",
                "new_password2": "a-quite-different-password",
                "role": Role.ADMIN,
                "is_superuser": "on",
            },
        )

        counselee.refresh_from_db()
        assert counselee.check_password("a-quite-different-password"), (
            "the password change itself should have worked"
        )
        assert counselee.role == Role.COUNSELEE
        assert counselee.is_superuser is False

    def test_a_document_cannot_be_moved_onto_another_case(
        self, client, counselee, sign_in, scenario, make_user
    ):
        """Relabelling is the counselee's to do. Which case a document belongs to is
        the only thing on that row that decides who can read it."""
        objects = scenario(counselee)
        elsewhere = Case.objects.create(counselor=make_user(Role.COUNSELOR), label="Other")
        sign_in(counselee)

        client.post(
            reverse("documents:edit", kwargs={"pk": objects.document.pk}),
            {"title": "Week one", "kind": objects.document.kind, "case": elsewhere.pk},
        )

        objects.document.refresh_from_db()
        assert objects.document.case_id == objects.case.pk


class TestWhereTheBrowserIsSentNext:
    """``?next=`` is the one piece of the redirect chain an attacker controls.

    A login page that will forward anywhere is a phishing tool: the link is
    genuinely ours, the sign-in genuinely works, and the counselee lands on a
    lookalike page still believing they are here.
    """

    OFF_SITE = (
        "https://evil.example.org/",
        # Protocol-relative: a browser reads this as a host, and code that only
        # checks for a leading "http" reads it as a path.
        "//evil.example.org/",
        "https:/\\evil.example.org/",
        "\\\\evil.example.org/",
    )

    @pytest.mark.parametrize("target", OFF_SITE)
    def test_signing_in_never_leaves_the_site(self, client, counselee, target):
        response = client.post(
            f"{reverse('accounts:login')}?next={target}",
            {"username": counselee.email, "password": TEST_PASSWORD},
        )

        assert response.status_code == 302
        assert "evil.example.org" not in response["Location"]
        assert response["Location"].startswith("/")

    def test_an_ordinary_next_is_still_honoured(self, client, counselee):
        """Otherwise the test above would pass on a view that ignores ``next``
        entirely, and would keep passing if somebody later made it work."""
        destination = reverse("documents:my_documents")

        response = client.post(
            f"{reverse('accounts:login')}?next={destination}",
            {"username": counselee.email, "password": TEST_PASSWORD},
        )

        assert response["Location"] == destination

    @pytest.mark.parametrize("target", OFF_SITE)
    def test_the_second_factor_does_not_forward_off_site_either(
        self, client, counselor, enrol_totp, target
    ):
        """The verify step redirects too, and it is the one page a staff member is
        certain to pass through."""
        _, code = enrol_totp(counselor)
        client.post("/login/", {"username": counselor.email, "password": TEST_PASSWORD})

        response = client.post(f"/mfa/verify/?next={target}", {"code": code()})

        assert response.status_code == 302
        assert "evil.example.org" not in response["Location"]


class TestLinksInEmail:
    def test_a_login_link_is_built_from_the_configured_address_not_the_request(
        self, client, counselee, settings
    ):
        """Host-header poisoning, the classic against emailed one-time links.

        ``ALLOWED_HOSTS`` is opened up on purpose: with it closed Django rejects the
        request before any view runs, so the test would prove nothing about the link
        and everything about a setting a deployment can get wrong.
        """
        settings.ALLOWED_HOSTS = ["*"]
        settings.SITE_BASE_URL = "https://counseling.example.org"

        client.post(
            reverse("accounts:magic_link_request"),
            {"email": counselee.email},
            HTTP_HOST="evil.example.org",
        )

        body = "\n".join(message.body for message in mail.outbox)
        assert "https://counseling.example.org/" in body
        assert "evil.example.org" not in body


class TestAnIdThatBelongsSomewhereElse:
    """A pk is not a permission, and a nested pk is not one either.

    ``/cases/<pk>/members/<member_pk>/end/`` has two ids in it. Checking only the
    first is the easiest mistake in the codebase to make, and for an admin — who
    may legitimately reach both cases — it would not surface as a refusal that
    somebody notices.
    """

    def test_a_membership_cannot_be_ended_through_a_different_case(
        self, client, admin_user, make_user, sign_in
    ):
        theirs = Case.objects.create(counselor=make_user(Role.COUNSELOR), label="Theirs")
        elsewhere = Case.objects.create(counselor=make_user(Role.COUNSELOR), label="Elsewhere")
        member = CaseMember.objects.create(case=elsewhere, counselee=make_user(Role.COUNSELEE))
        sign_in(admin_user)

        response = client.post(
            reverse(
                "counseling:case_member_end",
                kwargs={"pk": theirs.pk, "member_pk": member.pk},
            )
        )

        assert response.status_code == 404
        member.refresh_from_db()
        assert member.ended_on is None

    def test_a_counselor_cannot_reach_across_from_their_own_case(
        self, client, counselor, other_counselor, make_user, sign_in
    ):
        """The same attack from the role that has a case of its own to use as the
        first id — and here the refusal must not confirm the membership exists."""
        mine = Case.objects.create(counselor=counselor, label="Mine")
        theirs = Case.objects.create(counselor=other_counselor, label="Theirs")
        member = CaseMember.objects.create(case=theirs, counselee=make_user(Role.COUNSELEE))
        sign_in(counselor)

        response = client.post(
            reverse(
                "counseling:case_member_end",
                kwargs={"pk": mine.pk, "member_pk": member.pk},
            )
        )

        assert response.status_code in (403, 404)
        member.refresh_from_db()
        assert member.ended_on is None


# --- structural tripwires -------------------------------------------------
#
# The two tests below read the source rather than making requests. Both cover a
# change that would not fail any existing assertion: a view that reads a model
# directly is only wrong for the actors nobody wrote a test for, and a form that
# grows a privilege field is only wrong for the post nobody thought to try.


def view_modules():
    """Every views module in the project."""
    apps_dir = Path(settings.BASE_DIR) / "apps"
    return sorted(apps_dir.glob("*/views.py")) + sorted(apps_dir.glob("*/views/*.py"))


def scoped_model_names():
    """Model class names that hold data about a person and are scoped.

    Taken from tests/test_scoping_contract.py's own definitions — both which
    models hold data about a person and which are exempt with a reason — so the
    two files cannot drift into disagreeing. The exemption that matters here is
    ``LoginToken``: the invitation view looks one up for a visitor who is not
    signed in yet, so there is no actor to scope to, and the security property is
    that the digest is unguessable rather than that the row is filtered.
    """
    return {
        model.__name__
        for model in our_models()
        if needs_scoping(model) and model._meta.label not in UNSCOPED_BY_DESIGN
    }


#: Keyword arguments that scope a read to the person making the request as
#: narrowly as ``for_actor`` would. ``AvailabilityRule.objects.filter(
#: counselor=request.user)`` is not a hole.
SELF_SCOPING_KWARGS = {"user", "owner", "counselor", "counselee", "actor"}

#: Manager methods that write rather than read. A view that creates a row is not
#: disclosing anything, so it is not this test's business.
WRITING_METHODS = {"create", "bulk_create"}


def unscoped_reads(path):
    """Yield ``(line, expression)`` for each unscoped manager read in a module."""
    models = scoped_model_names()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        manager = node.func.value
        if not (isinstance(manager, ast.Attribute) and manager.attr == "objects"):
            continue
        if not (isinstance(manager.value, ast.Name) and manager.value.id in models):
            continue

        method = node.func.attr
        if method == "for_actor" or method in WRITING_METHODS:
            continue
        scoped = any(
            keyword.arg in SELF_SCOPING_KWARGS
            and isinstance(keyword.value, ast.Attribute)
            and keyword.value.attr == "user"
            for keyword in node.keywords
        )
        if not scoped:
            yield node.lineno, f"{manager.value.id}.objects.{method}(...)"


class TestNoViewReadsAroundTheScopingLayer:
    """``for_actor`` is the whole authorization model, and it is opt-in.

    ``Document.objects.get(pk=pk)`` in a view is valid Python, passes every
    existing test for the actor who is supposed to see that document, and hands it
    to everyone else as well. Nothing in Django can prevent it, so it is caught
    here instead.

    Scope, stated honestly: this looks at ``<Model>.objects`` in views modules
    only. It does not catch a read through a related manager (``case.members``),
    which is safe exactly when the parent was resolved through ``for_actor``, and
    it does not look at services, which are called by views that have already
    scoped what they pass in.
    """

    def test_every_manager_read_in_a_view_is_scoped(self):
        offenders = [
            f"{path.relative_to(settings.BASE_DIR)}:{line}  {expression}"
            for path in view_modules()
            for line, expression in unscoped_reads(path)
        ]
        assert not offenders, (
            "These reads in views bypass the scoping layer, so they return rows "
            "regardless of who is asking: "
            + "; ".join(offenders)
            + ". Resolve through Model.objects.for_actor(request.user), or scope "
            "the filter to request.user."
        )

    def test_the_tripwire_catches_what_it_claims_to(self, tmp_path):
        """A regex or an AST walk that matches nothing passes forever."""
        module = tmp_path / "views.py"
        module.write_text(
            "def detail(request, pk):\n"
            "    a = Document.objects.get(pk=pk)\n"
            "    b = Document.objects.for_actor(request.user).get(pk=pk)\n"
            "    c = AvailabilityRule.objects.filter(counselor=request.user)\n"
            "    d = Document.objects.create(case=b)\n"
            "    return a, b, c, d\n",
            encoding="utf-8",
        )

        found = list(unscoped_reads(module))

        assert [expression for _, expression in found] == ["Document.objects.get(...)"]

    def test_it_is_looking_at_the_real_views(self):
        """Guards against the glob quietly matching nothing after a reshuffle."""
        modules = view_modules()

        assert len(modules) >= 4, [str(path) for path in modules]
        assert "Document" in scoped_model_names()


class TestNoFormOffersAPrivilege:
    """A form is the other way a field becomes writable.

    ``fields = "__all__"`` is the dangerous form of this, because it is correct on
    the day it is written and wrong the day somebody adds a column.
    """

    #: Fields that decide what an account may do rather than what it is. None of
    #: them belongs on a form: roles are set when an account is created, and the
    #: MFA obligation is derived from the role.
    FORBIDDEN = {
        "role",
        "is_superuser",
        "is_staff",
        "is_active",
        "mfa_required",
        "user_permissions",
    }

    def model_forms(self):
        """Every ModelForm this project defines, including subclasses."""
        import importlib
        import pkgutil

        from django.forms import ModelForm

        import apps

        for module in pkgutil.iter_modules(apps.__path__, prefix="apps."):
            for name in (f"{module.name}.forms", f"{module.name}.views"):
                try:
                    importlib.import_module(name)
                except ModuleNotFoundError:
                    continue

        def descendants(cls):
            for subclass in cls.__subclasses__():
                yield subclass
                yield from descendants(subclass)

        return [
            form
            for form in descendants(ModelForm)
            if getattr(form, "__module__", "").startswith("apps.")
        ]

    def test_no_form_exposes_a_privilege_field(self):
        offenders = {
            f"{form.__module__}.{form.__name__}": sorted(self.FORBIDDEN & set(form.base_fields))
            for form in self.model_forms()
            if self.FORBIDDEN & set(form.base_fields)
        }
        assert not offenders, f"These forms would let a POST change a privilege: {offenders}"

    def test_no_form_takes_whatever_fields_the_model_happens_to_have(self):
        offenders = sorted(
            f"{form.__module__}.{form.__name__}"
            for form in self.model_forms()
            if getattr(form._meta, "fields", None) in (None, "__all__")
            or getattr(form._meta, "exclude", None)
        )
        assert not offenders, (
            'These forms use fields = "__all__" or exclude, so a column added to '
            f"the model later becomes writable without anybody deciding to: {offenders}"
        )

    def test_the_walk_found_the_forms(self):
        names = {form.__name__ for form in self.model_forms()}

        assert {"CaseForm", "CounseleeProfileForm", "DocumentEditForm"} <= names, sorted(names)


class TestTheMatrixCannotBeQuietlyWidened:
    """Invariants about the table itself.

    tests/test_access_matrix.py asserts that each cell is true of the running
    application. These two assert that particular cells may not be *changed*,
    because both describe a promise made outside the code — one in the
    instruction document, one to every visitor.
    """

    def test_billing_is_refused_every_documents_route(self):
        """The reason financial_admin exists as a separate role.

        Not "financial_admin sees no documents today" — that is asserted at
        runtime elsewhere — but "no future edit may grant it a documents route",
        which is the form the requirement was actually given in.
        """
        granted = sorted(
            name
            for name, (_, _, expected) in MATRIX.items()
            if name.startswith("documents:") and expected["financial_admin"] not in (403, 404)
        )
        assert not granted, (
            "The matrix grants billing a documents route: "
            f"{granted}. financial_admin must never reach an uploaded document."
        )

    def test_only_the_sign_in_pages_are_open_to_anonymous(self):
        """Every 200 here is a page served to the internet, so each one is named."""
        expected_open = {
            # The container's own healthcheck, and it returns nothing about anyone.
            "healthz",
            # The three doors: a password form, a request for a link, and the page
            # that says the link has been sent.
            "accounts:login",
            "accounts:magic_link_request",
            "accounts:magic_link_sent",
        }
        open_routes = {
            name for name, (_, _, expected) in MATRIX.items() if expected["anonymous"] == 200
        }

        assert open_routes == expected_open, (
            "The set of pages an anonymous visitor is served has changed: "
            f"unexpected {sorted(open_routes - expected_open)}, "
            f"no longer open {sorted(expected_open - open_routes)}"
        )
