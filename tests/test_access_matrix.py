"""
The access matrix.

Every route in the project appears in ``MATRIX`` with the status each kind of
actor should get. Two things make this worth more than the sum of its
assertions:

  * ``test_every_route_is_in_the_matrix`` walks the URLconf and fails if a route
    exists that nobody has decided the access rules for. Adding a view without
    thinking about the four roles is therefore a build failure, not something
    discovered later.
  * Statuses are asserted for *anonymous* as well as each role, so a view that
    forgets ``login_required`` shows up here rather than in production.

This file covers route reachability for an actor who is legitimately connected to
the object — so a refusal here is about the *action*, not about the row being
invisible. The cross-actor cases (counselor A getting 404 on counselor B's case,
counselee A seeing nothing of counselee B on a shared case) are asserted in
tests/test_case_access.py, where the point is the 404 rather than the route.
apps/core/scoping.py holds the row-visibility rules and
tests/test_scoping_contract.py makes sure every model about a person has them.
"""

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, reverse

from apps.accounts.models import Role

pytestmark = pytest.mark.django_db

#: A token that is syntactically valid but belongs to nothing. Routes taking a
#: token are exercised with this, so the assertion is about who may *reach* the
#: view, not about a particular token's state.
UNKNOWN_TOKEN = "x" * 43

#: Actor keys used in the matrix. "anonymous" is not signed in; every other key
#: is fully signed in, meaning staff have already cleared TOTP.
ACTORS = ("anonymous", "counselee", "counselor", "admin", "financial_admin")

#: Convenience slices, so a matrix row that treats a whole group alike says so.
SIGNED_IN = ACTORS[1:]
STAFF = ("counselor", "admin", "financial_admin")

#: Routes deliberately not enumerated one by one, with the reason.
EXEMPT_PREFIXES = {
    # The admin has dozens of generated routes and one access rule covering all
    # of them: is_superuser. Asserting it per route would be noise, so it is
    # asserted once here and thoroughly in tests/test_admin_lockdown.py.
    "admin:",
}

#: name -> (kwargs, method, {actor: expected status})
#:
#: ``kwargs`` is either a dict or a callable taking the ``scenario`` namespace —
#: a case belonging to the acting counselor, with the acting counselee on it —
#: and returning the dict. Callable form is what lets a route be exercised with a
#: public id the actor can legitimately reach.
MATRIX = {
    "healthz": (
        {},
        "get",
        # Deliberately open: the container's own healthcheck calls it with no
        # session. It returns no data about anyone.
        dict.fromkeys(ACTORS, 200),
    ),
    "accounts:home": (
        {},
        "get",
        {"anonymous": 302} | dict.fromkeys(SIGNED_IN, 200),
    ),
    # The one page that assigns a role, and therefore decides who may read a
    # counseling record. Administrators only — a counselor who could create an
    # administrator could promote themselves.
    "accounts:user_create": (
        {},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            "counselor": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    # --- the home page and the ministry's own settings ---------------------
    "core:home": (
        {},
        "get",
        {"anonymous": 302} | dict.fromkeys(SIGNED_IN, 200),
    ),
    "core:mail_settings": (
        {},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            # Deliberately closed to the other staff roles: the stored password
            # cannot be read back, but the host and username can, and changing
            # where the ministry's mail goes is not a counseling decision.
            "counselor": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "core:mail_test": (
        {},
        "post",
        {
            # 302 for the administrator: it sends to their own address and
            # redirects back to the settings page with the outcome.
            "anonymous": 302,
            "admin": 302,
            "counselor": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "accounts:login": (
        {},
        "get",
        dict.fromkeys(ACTORS, 200),
    ),
    "accounts:logout": (
        {},
        "post",
        dict.fromkeys(ACTORS, 302),
    ),
    "accounts:password_change": (
        {},
        "get",
        {"anonymous": 302} | dict.fromkeys(SIGNED_IN, 200),
    ),
    # Staff Google sign-in. Open to anonymous by nature — signing in is what these
    # are for — and a redirect for everyone here because the test settings leave it
    # switched off, which is the state a ministry that has not configured it is in.
    # Who may actually *complete* one is tests/test_google_sso.py's subject, and the
    # answer is staff only.
    "accounts:google_login": (
        {},
        "post",
        dict.fromkeys(ACTORS, 302),
    ),
    "accounts:google_login_callback": (
        {},
        "get",
        dict.fromkeys(ACTORS, 302),
    ),
    "accounts:magic_link_request": (
        {},
        "get",
        # Already signed in? There is nothing to ask for; go to the landing page.
        {"anonymous": 200} | dict.fromkeys(SIGNED_IN, 302),
    ),
    "accounts:magic_link_sent": (
        {},
        "get",
        dict.fromkeys(ACTORS, 200),
    ),
    "accounts:magic_link_consume": (
        {"token": UNKNOWN_TOKEN},
        # POST, because a GET only renders the confirmation button and would say
        # 200 for a token that does not exist.
        "post",
        dict.fromkeys(ACTORS, 400),
    ),
    "accounts:invitation_accept": (
        {"token": UNKNOWN_TOKEN},
        "get",
        dict.fromkeys(ACTORS, 400),
    ),
    "accounts:mfa_setup": (
        {},
        "get",
        # Signed-in staff already have a confirmed device, so enrolment refuses
        # and sends them to verify — see the note in views.mfa_setup. A counselee
        # has no device and may enrol one voluntarily.
        {"anonymous": 302, "counselee": 200} | dict.fromkeys(STAFF, 302),
    ),
    "accounts:mfa_verify": (
        {},
        "get",
        # Staff here are already verified, so it redirects on. A counselee with no
        # device is sent to setup.
        dict.fromkeys(ACTORS, 302),
    ),
    # --- counseling: dashboards -------------------------------------------
    "counseling:dashboard": (
        {},
        "get",
        # A router, not a page: everyone is sent somewhere, including anonymous
        # (to login).
        dict.fromkeys(ACTORS, 302),
    ),
    "counseling:counselor_dashboard": (
        {},
        "get",
        # Redirect rather than 403 for the wrong role: this is not a refusal, it
        # is the wrong door. Each role has a dashboard of its own to be sent to.
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 302,
            "financial_admin": 302,
            "counselee": 302,
        },
    ),
    "counseling:my_cases": (
        {},
        "get",
        {"anonymous": 302, "counselee": 200} | dict.fromkeys(STAFF, 302),
    ),
    "counseling:caseload_index": (
        {},
        "get",
        # The one counseling page financial_admin holds a permission for. A
        # counselor is refused outright: which counselees another counselor
        # carries is not theirs to see, and there is no other door to send them to.
        {
            "anonymous": 302,
            "admin": 200,
            "financial_admin": 200,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    # --- counseling: cases -------------------------------------------------
    "counseling:case_list": (
        {},
        "get",
        # Counselees are redirected to my_cases: the columns here would tell them
        # how many other people are on a shared case.
        {"anonymous": 302, "counselee": 302} | dict.fromkeys(STAFF, 200),
    ),
    "counseling:case_create": (
        {},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            "counselor": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "counseling:case_detail": (
        lambda s: {"public_id": s.case.public_id},
        "get",
        # Everyone connected to the case may read the page; what differs is what
        # is on it. The counselee's own case and financial_admin's billing view
        # both land here, and case_detail decides per role whether the notes
        # appear — see the show_notes assertions in tests/test_case_access.py.
        {"anonymous": 302} | dict.fromkeys(SIGNED_IN, 200),
    ),
    "counseling:case_edit": (
        lambda s: {"public_id": s.case.public_id},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            "counselor": 200,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "counseling:case_close": (
        lambda s: {"public_id": s.case.public_id},
        "post",
        {
            "anonymous": 302,
            "admin": 302,
            "counselor": 302,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    # --- counseling: membership -------------------------------------------
    "counseling:case_member_add": (
        lambda s: {"public_id": s.case.public_id},
        "get",
        # Administrator only, including against the case's own counselor: adding a
        # member is the action in this app with a real disclosure consequence.
        {
            "anonymous": 302,
            "admin": 200,
            "counselor": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "counseling:case_member_end": (
        lambda s: {"public_id": s.case.public_id, "member_public_id": s.member.public_id},
        "post",
        {
            "anonymous": 302,
            "admin": 302,
            "counselor": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    # --- counseling: people ------------------------------------------------
    "counseling:counselee_create": (
        {},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            "counselor": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "counseling:counselee_detail": (
        lambda s: {"public_id": s.counselee.public_id},
        "get",
        # One person's file: sessions, documents, and notes on one page. Staff who
        # counsel, and nobody else.
        #
        # ``counselee`` is 403 rather than 200: this is not how anybody reaches
        # their own record — a route keyed on a person's id would invite trying
        # somebody else's — and their own version of it is their dashboard.
        # ``financial_admin`` is 403 for the reason every documents route refuses
        # them, only more so, because gathering the record is what this page does.
        {
            "anonymous": 302,
            "admin": 200,
            "counselor": 200,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "counseling:counselor_profile_edit": (
        {},
        "get",
        # A counselor's own practice settings, and only theirs — not an admin's to
        # edit on their behalf, because there is no counselor id in this URL for
        # an admin to point at.
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "counseling:counselee_profile_edit": (
        {},
        "get",
        # The id-less form is the counselee's own intake page. Staff are refused
        # here and use the ``_for`` route, so this URL can never mean "somebody".
        {"anonymous": 302, "counselee": 200} | dict.fromkeys(STAFF, 403),
    ),
    "counseling:counselee_profile_edit_for": (
        lambda s: {"public_id": s.profile.public_id},
        "get",
        # financial_admin is refused: a date of birth and an emergency contact are
        # not billing data. Counselee is refused because their own page has no id.
        {
            "anonymous": 302,
            "admin": 200,
            "counselor": 200,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    # --- documents ---------------------------------------------------------
    #
    # Read the financial_admin column of this section downwards: it is 403 or 404
    # on every route without exception, and that column is the whole reason the
    # role exists as a separate one. The two values mean different things and both
    # are deliberate. A route keyed by a *case* is 403 — billing can see that the
    # case exists, so pretending the URL is not there would be a lie, and the
    # honest answer is a refusal. A route keyed by a *document* is 404, because
    # the document is not in ``Document.objects.for_actor`` for them at all, and
    # confirming it exists would already be the disclosure.
    "documents:my_documents": (
        {},
        "get",
        # 403 rather than an empty page for financial_admin: a page reading
        # "nothing here" is a claim about content, and the point is that billing
        # does not get to ask the question.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "documents:case_documents": (
        lambda s: {"case_public_id": s.case.public_id},
        "get",
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "documents:upload": (
        lambda s: {"case_public_id": s.case.public_id},
        "get",
        # A counselee uploading is half the product — the two-way exchange the
        # instruction document asks for — so 200 here is a feature, not a leniency.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "documents:detail": (
        lambda s: {"public_id": s.document.public_id},
        "get",
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "documents:download": (
        lambda s: {"public_id": s.document.public_id},
        "get",
        # A real decryption, from a blob the scenario fixture actually wrote. A 404
        # here for the connected actors would mean the store or the key is wrong,
        # which is worth failing over.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "documents:preview": (
        lambda s: {"public_id": s.document.public_id},
        "get",
        # The same disclosure as a download and therefore the same column, which is
        # the point: "view" is not a lesser permission. The scenario's document is a
        # photo, so it is on the inline allowlist; a type that is not would be 404
        # for everybody, which tests/test_document_preview.py asserts.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "documents:thumbnail": (
        lambda s: {"public_id": s.document.public_id},
        "get",
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "documents:edit": (
        lambda s: {"public_id": s.document.public_id},
        "get",
        # The uploader may relabel their own file. Only the label: the bytes are
        # not replaceable, because the recorded hash describes what was uploaded.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "documents:share": (
        lambda s: {"public_id": s.document.public_id},
        "post",
        # The counselee owns this document and is still refused. Publishing to the
        # whole case is the counselor's decision alone — on a family case the
        # uploader may not know who else would receive it.
        {
            "anonymous": 302,
            "counselee": 403,
            "counselor": 302,
            "admin": 302,
            "financial_admin": 404,
        },
    ),
    "documents:delete": (
        lambda s: {"public_id": s.document.public_id},
        "post",
        # Withdrawing is allowed to the uploader, and it is a soft delete: the
        # counselor keeps a record that the document existed.
        {
            "anonymous": 302,
            "counselee": 302,
            "counselor": 302,
            "admin": 302,
            "financial_admin": 404,
        },
    ),
    # --- the template library ----------------------------------------------
    #
    # Two patterns to read across these rows, and they are the whole design:
    #
    #   * A counselee and a financial_admin get **404** everywhere the route names a
    #     template, and 403 on the two that name none. The library is not theirs to
    #     see at all — ``DocumentTemplate.objects.for_actor`` returns none() for both
    #     — so a template does not exist as far as they are concerned, and the routes
    #     that take no id refuse the question rather than showing an empty shelf.
    #   * A **counselor reads and uses; an administrator changes.** The counselor's
    #     403 on upload, edit and withdraw is the point of the split: replacing the
    #     intake form changes what every other counselor hands out.
    "documents:template_library": (
        {},
        "get",
        {
            "anonymous": 302,
            "counselee": 403,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "documents:template_upload": (
        {},
        "get",
        {
            "anonymous": 302,
            "counselee": 403,
            "counselor": 403,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "documents:template_download": (
        lambda s: {"public_id": s.template.public_id},
        "get",
        # A real decryption of a blob the scenario wrote, as documents:download is.
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "documents:template_thumbnail": (
        lambda s: {"public_id": s.template.public_id},
        "get",
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "documents:template_edit": (
        lambda s: {"public_id": s.template.public_id},
        "get",
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 403,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "documents:template_withdraw": (
        lambda s: {"public_id": s.template.public_id},
        "post",
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 403,
            "admin": 302,
            "financial_admin": 404,
        },
    ),
    "documents:template_use": (
        lambda s: {"case_public_id": s.case.public_id, "public_id": s.template.public_id},
        "get",
        # 403 rather than 404 for the two refused roles, unlike every other template
        # row: this route names a case they can legitimately see, and the view resolves
        # and refuses the *case* before it looks the template up. The counselee's 403
        # is the one worth pausing on — they may upload to this case, and may still not
        # pull a ministry template onto it.
        {
            "anonymous": 302,
            "counselee": 403,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    # --- the knowledge base -------------------------------------------------
    #
    # One pattern across every row, and it is the feature's whole access rule: this
    # shelf is for counselors and administrators. A counselee and a financial_admin
    # get 404 wherever the route names a row — ``Resource.objects.for_actor`` returns
    # none() for both, so nothing on the shelf exists as far as they are concerned —
    # and 403 on the two routes that name none, because an empty page would be a claim
    # about what colleagues have written and a refusal is not.
    #
    # Counselor and admin differ nowhere here, unlike the template library above.
    # That is the point of the two features being separate: an administrator decides
    # which intake form is current, and nobody decides which handout a colleague
    # found helpful. The rows the two roles could differ on — editing and removing
    # somebody *else's* contribution — are in tests/test_knowledge_base.py, since the
    # scenario's counselor is the contributor here.
    "knowledge:home": (
        {},
        "get",
        {
            "anonymous": 302,
            "counselee": 403,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "knowledge:add": (
        {},
        "get",
        {
            "anonymous": 302,
            "counselee": 403,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "knowledge:detail": (
        lambda s: {"public_id": s.resource.public_id},
        "get",
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "knowledge:download": (
        lambda s: {"public_id": s.resource.public_id},
        "get",
        # A real decryption of a blob the scenario wrote, as documents:download is.
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "knowledge:edit": (
        lambda s: {"public_id": s.resource.public_id},
        "get",
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "knowledge:remove": (
        lambda s: {"public_id": s.resource.public_id},
        "post",
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 302,
            "admin": 302,
            "financial_admin": 404,
        },
    ),
    "knowledge:comment": (
        lambda s: {"public_id": s.resource.public_id},
        "post",
        # The matrix posts no body, so the two 302s are the "a note needs something in
        # it" redirect rather than a comment being written. That is the right thing to
        # assert here — this row is about who may reach the route — and the comment
        # actually appearing is asserted in tests/test_knowledge_base.py.
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 302,
            "admin": 302,
            "financial_admin": 404,
        },
    ),
    "knowledge:comment_remove": (
        lambda s: {"public_id": s.comment.public_id},
        "post",
        # The counselor's 302 is them removing their own note; the admin's is the
        # override. A second counselor gets a 403 here, which is in the dedicated file.
        {
            "anonymous": 302,
            "counselee": 404,
            "counselor": 302,
            "admin": 302,
            "financial_admin": 404,
        },
    ),
    # --- messaging ---------------------------------------------------------
    #
    # The pattern to read across these rows: financial_admin never reaches
    # anything (403 where the route is keyed by a case it can legitimately see,
    # 404 where it is keyed by a thread that does not exist as far as it is
    # concerned), and an admin reads but does not write. The reading and writing
    # halves of the thread page are one route, so the refusal an admin gets on
    # POST is asserted in tests/test_messaging_access.py rather than here.
    "messaging:index": (
        {},
        "get",
        # 403 rather than an empty page for financial_admin, exactly as with
        # documents:my_documents: the refusal is the statement.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "messaging:case_threads": (
        lambda s: {"case_public_id": s.case.public_id},
        "get",
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "messaging:start": (
        lambda s: {"case_public_id": s.case.public_id},
        "get",
        # An admin is refused here and served on every reading route above. They
        # oversee the ministry; they are not a party to the counseling, and the
        # audience of a conversation is not theirs to enter.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 403,
            "financial_admin": 403,
        },
    ),
    "messaging:thread": (
        lambda s: {"public_id": s.thread.public_id},
        "get",
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "messaging:attachment": (
        lambda s: {"public_id": s.attachment.public_id},
        "get",
        # Identical to the messaging:thread row above, and that identity is the
        # point: an attachment has no audience of its own, so a file is reachable by
        # exactly the people who can read the message it came with. If these two
        # rows ever diverge, something has grown a second rule.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 404,
        },
    ),
    "messaging:close": (
        lambda s: {"public_id": s.thread.public_id},
        "post",
        # The counselee started this conversation and still cannot close it: the
        # thread is the channel their counselor reaches them on.
        {
            "anonymous": 302,
            "counselee": 403,
            "counselor": 302,
            "admin": 403,
            "financial_admin": 404,
        },
    ),
    "messaging:reopen": (
        # The *closed* thread, so this row asserts a reopening that works rather
        # than a permission that cannot apply — the same reason the scenario
        # carries a second booking in the past.
        lambda s: {"public_id": s.closed_thread.public_id},
        "post",
        {
            "anonymous": 302,
            "counselee": 403,
            "counselor": 302,
            "admin": 403,
            "financial_admin": 404,
        },
    ),
    # --- scheduling: office hours ------------------------------------------
    #
    # None of these routes carries a counselor id, so they cannot be pointed at
    # somebody else's diary — the same construction as the practice-settings page,
    # and the reason an admin gets 403 rather than a way in. The rows keyed by a public id
    # are 404 for every actor but the owning counselor: apps/scheduling/views.py's
    # ``own_availability_or_404`` filters on ``counselor=request.user`` on top of
    # the scoping queryset, because that queryset deliberately lets a counselee
    # *read* the hours of a counselor they book with.
    "scheduling:availability": (
        {},
        "get",
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "scheduling:availability_add": (
        {},
        "get",
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "scheduling:availability_edit": (
        lambda s: {"public_id": s.rule.public_id},
        "get",
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 404,
            "financial_admin": 404,
            "counselee": 404,
        },
    ),
    "scheduling:availability_delete": (
        lambda s: {"public_id": s.rule.public_id},
        "post",
        {
            "anonymous": 302,
            "counselor": 302,
            "admin": 404,
            "financial_admin": 404,
            "counselee": 404,
        },
    ),
    "scheduling:override_add": (
        {},
        "get",
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 403,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "scheduling:override_delete": (
        lambda s: {"public_id": s.override.public_id},
        "post",
        {
            "anonymous": 302,
            "counselor": 302,
            "admin": 404,
            "financial_admin": 404,
            "counselee": 404,
        },
    ),
    # --- scheduling: booking ----------------------------------------------
    #
    # Read the financial_admin column here against the documents section above.
    # It is *not* uniformly refused, and that is the deliberate difference: an
    # appointment that happened is what gets invoiced, so billing may read a
    # booking and a case's list of them. What it may not reach is the ministry-wide
    # diary (a caseload-shaped disclosure) or any action, and no template on a
    # route it can reach renders a note. See apps/scheduling/rules.py.
    "scheduling:book": (
        lambda s: {"case_public_id": s.case.public_id},
        "get",
        # A counselee choosing their own time is the point of the feature. A
        # counselor gets the page too, read-only, to see what is on offer; the
        # view refuses their POST because they cannot be their own counselee.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "scheduling:schedule": (
        lambda s: {"case_public_id": s.case.public_id},
        "get",
        # The counselor's own way in, not bound by the published hours. A counselee
        # is refused: booking outside what is offered is not theirs to do.
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
            "counselee": 403,
        },
    ),
    "scheduling:appointments": (
        {},
        "get",
        # 403 for financial_admin rather than an empty diary, for the same reason
        # documents:my_documents is: the refusal is the statement.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "scheduling:case_appointments": (
        lambda s: {"case_public_id": s.case.public_id},
        "get",
        # Keyed by a case and gated on counseling.view_case, so everyone who can
        # read the case can read its sessions. This is billing's route.
        {"anonymous": 302} | dict.fromkeys(SIGNED_IN, 200),
    ),
    "scheduling:detail": (
        lambda s: {"public_id": s.booking.public_id},
        "get",
        # 200 for financial_admin, and the note is withheld by show_notes rather
        # than by hiding the page — asserted in tests/test_scheduling_access.py.
        {"anonymous": 302} | dict.fromkeys(SIGNED_IN, 200),
    ),
    "scheduling:confirm": (
        lambda s: {"public_id": s.booking.public_id},
        "post",
        # Accepting a request is the counselor's decision. A counselee confirming
        # their own request would make the requested state meaningless.
        {
            "anonymous": 302,
            "counselor": 302,
            "admin": 302,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    "scheduling:cancel": (
        lambda s: {"public_id": s.booking.public_id},
        "get",
        # Both sides may cancel; the counselee's is the whole point of self-booking.
        {
            "anonymous": 302,
            "counselee": 200,
            "counselor": 200,
            "admin": 200,
            "financial_admin": 403,
        },
    ),
    "scheduling:reschedule": (
        lambda s: {"public_id": s.booking.public_id},
        "get",
        # Refused to the counselee on purpose: rescheduling bypasses the office
        # hours, so their route is to cancel and book again from what is offered.
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 200,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    "scheduling:outcome": (
        # The past booking, because record_outcome requires the appointment to
        # have happened — a future one is 403 even for its own counselor.
        lambda s: {"public_id": s.past_booking.public_id},
        "get",
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 200,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    "scheduling:note": (
        # A future booking on purpose: unlike outcome, writing a note does not
        # wait for the session. The admin 403 is the deliberate asymmetry —
        # view_booking_note lets them read one, change_booking_note does not let
        # them write one.
        lambda s: {"public_id": s.booking.public_id},
        "get",
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 403,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    "scheduling:meeting_link": (
        lambda s: {"public_id": s.booking.public_id},
        "get",
        # The admin 403 is the same asymmetry as the note, for a different reason:
        # whoever sets the link decides which room a counseling session happens in,
        # and an administrator who could change it could point a counselee at a
        # meeting the counselor is not in. See scheduling/rules.py.
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 403,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    # --- the Google connection ---
    #
    # The admin 403 on all five is the point, and it is the only place in this
    # matrix where an admin is excluded from something a counselor can do without
    # an id being involved. An administrator who could arrange where a counselor's
    # appointment times are sent could send them to a calendar the counselor does
    # not read. See the note on manage_own_google_calendar in scheduling/rules.py.
    #
    # The integration is off in the test settings, so a counselor reaching these
    # gets the "not set up on this server" page or a redirect back to it — which is
    # the right thing to assert here. Whether the handshake works is
    # tests/test_google_calendar.py's job.
    "scheduling:google_settings": (
        {},
        "get",
        {
            "anonymous": 302,
            "counselor": 200,
            "admin": 403,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    "scheduling:google_connect": (
        {},
        "post",
        {
            "anonymous": 302,
            "counselor": 302,
            "admin": 403,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    "scheduling:google_callback": (
        {},
        "get",
        {
            "anonymous": 302,
            # 302 rather than 200: there is no state in the session, so the
            # counselor is bounced back with "that link had expired".
            "counselor": 302,
            "admin": 403,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    "scheduling:google_disconnect": (
        {},
        "post",
        {
            "anonymous": 302,
            "counselor": 302,
            "admin": 403,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    "scheduling:google_resync": (
        {},
        "post",
        {
            "anonymous": 302,
            "counselor": 302,
            "admin": 403,
            "counselee": 403,
            "financial_admin": 403,
        },
    ),
    # --- billing ------------------------------------------------------------
    #
    # This is the one section where ``financial_admin`` is served everywhere and the
    # **counselor** is the column to read downwards: 200 on the two reading routes
    # and 403 on every action. That is deliberate and it is the separation the four
    # roles exist for — a counselor negotiating money with the person they are
    # counseling is the conflict of interest billing is kept away from. They can read
    # a bill on their own case, because a counselee will ask them about it.
    #
    # The counselee column is the mirror image: they see and pay their own invoice and
    # can do nothing else. "I paid it, mark it paid" is not a permission.
    "billing:index": (
        {},
        "get",
        # 302 for a counselee — the wrong door, not a locked one; they are sent to
        # their own invoices. 403 for a counselor, who has no ministry-wide view of
        # money and nowhere else to be sent.
        {
            "anonymous": 302,
            "admin": 200,
            "financial_admin": 200,
            "counselor": 403,
            "counselee": 302,
        },
    ),
    "billing:fees": (
        {},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            "financial_admin": 200,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:fee_add": (
        {},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            "financial_admin": 200,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:fee_end": (
        lambda s: {"public_id": s.fee.public_id},
        "post",
        # 403 rather than 404 for the two refused roles because the permission is
        # checked before the row is looked up: a rate list is ministry configuration,
        # not an object whose existence is a disclosure.
        {
            "anonymous": 302,
            "admin": 302,
            "financial_admin": 302,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:session_amend": (
        # The session left off every invoice on purpose. change_session is refused
        # once a session is on a live invoice, so pointing this row at an invoiced one
        # would assert a 403 that says nothing about the role.
        lambda s: {"public_id": s.session.public_id},
        "get",
        # 403, not 404: a counselor and the counselee can both legitimately see this
        # session — it is their case and their hour — so the refusal is about the
        # action of repricing it.
        {
            "anonymous": 302,
            "admin": 200,
            "financial_admin": 200,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:case_invoices": (
        lambda s: {"case_public_id": s.case.public_id},
        "get",
        # Keyed by a case and gated on it, like scheduling:case_appointments: everyone
        # connected to the case reaches the page and what is on it differs. A
        # counselee sees the invoices addressed to them and none of a spouse's, which
        # InvoiceQuerySet decides — asserted in tests/test_billing_access.py.
        {"anonymous": 302} | dict.fromkeys(SIGNED_IN, 200),
    ),
    "billing:invoice_create": (
        lambda s: {"case_public_id": s.case.public_id},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            "financial_admin": 200,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:my_invoices": (
        {},
        "get",
        {"anonymous": 302, "counselee": 200} | dict.fromkeys(STAFF, 302),
    ),
    "billing:invoice_detail": (
        # The issued one. A draft is refused to the payer, which is a different
        # assertion and belongs in tests/test_billing_access.py.
        lambda s: {"public_id": s.invoice.public_id},
        "get",
        {"anonymous": 302} | dict.fromkeys(SIGNED_IN, 200),
    ),
    "billing:pay": (
        lambda s: {"public_id": s.invoice.public_id},
        "post",
        # 403 for everyone, the payer included, because Stripe is switched off in the
        # test settings — the state a ministry that has not connected it is in, and
        # ``invoice_is_payable_by_card`` says so rather than offering a dead end. That
        # the payer and nobody else may pay when it *is* configured is asserted in
        # tests/test_billing_stripe.py, which turns it on deliberately.
        {"anonymous": 302} | dict.fromkeys(SIGNED_IN, 403),
    ),
    "billing:line_add": (
        lambda s: {"public_id": s.draft_invoice.public_id},
        "post",
        # The draft, because change_invoice is refused on anything issued. A counselee
        # gets 403 rather than 404: the draft *is* in their queryset, and it is the
        # permission layer that refuses it — see visible_invoice_or_404.
        {
            "anonymous": 302,
            "admin": 302,
            "financial_admin": 302,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:line_remove": (
        lambda s: {
            "public_id": s.draft_invoice.public_id,
            "line_public_id": s.draft_line.public_id,
        },
        "post",
        {
            "anonymous": 302,
            "admin": 302,
            "financial_admin": 302,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:invoice_issue": (
        lambda s: {"public_id": s.draft_invoice.public_id},
        "post",
        {
            "anonymous": 302,
            "admin": 302,
            "financial_admin": 302,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:invoice_void": (
        # The issued invoice with nothing paid against it: voiding is refused once
        # money has arrived, so the part-paid one would 403 for every actor.
        lambda s: {"public_id": s.invoice.public_id},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            "financial_admin": 200,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:invoice_write_off": (
        lambda s: {"public_id": s.invoice.public_id},
        "get",
        {
            "anonymous": 302,
            "admin": 200,
            "financial_admin": 200,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:payment_record": (
        lambda s: {"public_id": s.invoice.public_id},
        "get",
        # The payer is refused their own invoice here, which is the row worth pausing
        # on: recording money is the office's, and a counselee marking their own bill
        # paid is an assertion rather than a payment.
        {
            "anonymous": 302,
            "admin": 200,
            "financial_admin": 200,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:payment_reverse": (
        lambda s: {
            "public_id": s.part_paid_invoice.public_id,
            "payment_public_id": s.payment.public_id,
        },
        "post",
        {
            "anonymous": 302,
            "admin": 302,
            "financial_admin": 302,
            "counselor": 403,
            "counselee": 403,
        },
    ),
    "billing:stripe_webhook": (
        {},
        "post",
        # The only route in this application a stranger may reach, and the only row
        # here where anonymous is not a 302. 400 for every actor because no signature
        # is supplied, and being signed in is worth nothing to it: the HMAC over the
        # raw body is the whole of its authentication. tests/test_stripe_webhook.py
        # is where the forgeries are.
        dict.fromkeys(ACTORS, 400),
    ),
}


def named_routes(resolver=None, namespace=""):
    """Every named route in the project, as ``namespace:name`` strings."""
    resolver = resolver or get_resolver()
    for entry in resolver.url_patterns:
        if isinstance(entry, URLResolver):
            prefix = f"{namespace}{entry.namespace}:" if entry.namespace else namespace
            yield from named_routes(entry, prefix)
        elif isinstance(entry, URLPattern) and entry.name:
            yield f"{namespace}{entry.name}"


def test_every_route_is_in_the_matrix():
    """A new view must state who may reach it before it can ship.

    This is the check that keeps the rest of this file honest: without it the
    matrix would describe whichever routes existed when it was written.
    """
    covered = set(MATRIX)
    missing = sorted(
        name
        for name in named_routes()
        if name not in covered and not any(name.startswith(prefix) for prefix in EXEMPT_PREFIXES)
    )
    assert not missing, (
        "These routes are not in tests/test_access_matrix.py's MATRIX. Decide "
        f"what each role should get and add them: {missing}"
    )


def test_the_matrix_has_no_routes_that_do_not_exist():
    """Catches a renamed or removed view leaving a stale, vacuous entry behind."""
    existing = set(named_routes())
    stale = sorted(name for name in MATRIX if name not in existing)
    assert not stale, f"MATRIX names routes that no longer exist: {stale}"


def test_the_matrix_covers_every_actor():
    incomplete = {
        name: sorted(set(ACTORS) - set(expected)) for name, (_, _, expected) in MATRIX.items()
    }
    incomplete = {name: missing for name, missing in incomplete.items() if missing}
    assert not incomplete, f"MATRIX entries missing actors: {incomplete}"


@pytest.mark.parametrize("route", sorted(MATRIX))
@pytest.mark.parametrize("actor", ACTORS)
def test_route_returns_the_expected_status(route, actor, client, make_user, sign_in, scenario):
    kwargs, method, expected = MATRIX[route]

    user = None if actor == "anonymous" else make_user(Role(actor))
    if callable(kwargs):
        # Build the objects before signing in, so the acting user is the one the
        # case belongs to rather than a bystander with a valid public id.
        kwargs = kwargs(scenario(user))
    if user is not None:
        sign_in(user)

    response = getattr(client, method)(reverse(route, kwargs=kwargs))

    assert response.status_code == expected[actor], (
        f"{actor} {method.upper()} {route} returned {response.status_code}, "
        f"expected {expected[actor]}"
    )


class TestRoleSeparationOnTheLandingPage:
    """The landing page is the only page yet, so it is where role separation shows."""

    def test_only_an_admin_is_offered_user_management(self, client, make_user, sign_in):
        admin = make_user(Role.ADMIN)
        sign_in(admin)

        assert client.get(reverse("accounts:home")).context["can_manage_users"] is True

    @pytest.mark.parametrize("role", [Role.COUNSELOR, Role.FINANCIAL_ADMIN, Role.COUNSELEE])
    def test_nobody_else_is(self, client, make_user, sign_in, role):
        sign_in(make_user(role))

        assert client.get(reverse("accounts:home")).context["can_manage_users"] is False

    def test_a_counselee_is_not_shown_staff_navigation(self, client, counselee, sign_in):
        sign_in(counselee)

        assert client.get(reverse("accounts:home")).context["is_staff_role"] is False
