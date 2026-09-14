"""
The navigation, as data rather than as markup.

It used to be a chain of ``{% if user.role == ... %}`` blocks in base.html. That
worked while there was one place to render it; it stopped working the moment
there were two — a collapsible sidebar and a home page whose whole content is
"here is what you can do". Two role-branched copies of the same list is how one
of them quietly loses a link, and tests/test_navigation.py exists because a
missing link is a missing feature.

So the list lives here, once, and both templates render it. Two rules it keeps:

  * **Order is the product decision.** Each role's first entry is the page that
    role opens most, because it is also where the home page's first button goes.
  * **A link is a courtesy, never a control.** Every page named here checks its
    own permission when it is opened. Nothing in this module is consulted by an
    authorization decision, and adding an entry grants nobody anything — see the
    same note in apps/counseling/views.py.
"""

from dataclasses import dataclass

from django.urls import reverse

from apps.accounts.middleware import session_is_fully_authenticated
from apps.accounts.models import Role


@dataclass(frozen=True)
class NavLink:
    #: Stable name for the entry, so a template can single one out — the unread
    #: badge on Messages — without matching on the label a translator will change.
    key: str
    label: str
    url: str
    #: One line for the home page's buttons. The sidebar ignores it.
    blurb: str = ""


def _link(key, label, route, blurb="", args=None) -> NavLink:
    return NavLink(key=key, label=label, url=reverse(route, args=args or []), blurb=blurb)


def links_for(user) -> list[NavLink]:
    """What this user can do, in the order they are likely to want it."""
    if not (user and user.is_authenticated):
        return []

    match user.role:
        case Role.COUNSELOR:
            return [
                _link(
                    "caseload",
                    "My caseload",
                    "counseling:counselor_dashboard",
                    "The people you are walking with, and their cases.",
                ),
                _link(
                    "appointments",
                    "Appointments",
                    "scheduling:appointments",
                    "What is coming up, and what has already happened.",
                ),
                _link(
                    "messages",
                    "Messages",
                    "messaging:index",
                    "Correspondence with the counselees on your cases.",
                ),
                _link(
                    "office_hours",
                    "Office hours",
                    "scheduling:availability",
                    "The times counselees may book with you.",
                ),
                _link(
                    "templates",
                    "Document templates",
                    "documents:template_library",
                    "The ministry's forms and handouts, ready to put on a case.",
                ),
                _link(
                    "practice",
                    "Practice settings",
                    "counseling:counselor_profile_edit",
                    "Session length, booking notice, and whether you are taking new cases.",
                ),
            ]
        case Role.ADMIN:
            return [
                _link("cases", "Cases", "counseling:case_list", "Every case in the ministry."),
                _link(
                    "appointments",
                    "Appointments",
                    "scheduling:appointments",
                    "The ministry's diary.",
                ),
                _link("billing", "Billing", "billing:index", "Sessions, invoices and payments."),
                _link(
                    "new_case",
                    "Open a case",
                    "counseling:case_create",
                    "Assign a counselor and put the counselees on it.",
                ),
                _link(
                    "new_user",
                    "Add a person",
                    "accounts:user_create",
                    "Create an account for a counselee, counselor or administrator.",
                ),
                _link(
                    "templates",
                    "Document templates",
                    "documents:template_library",
                    "The forms and handouts every counselor can use.",
                ),
                _link(
                    "mail_settings",
                    "Email settings",
                    "core:mail_settings",
                    "How invitations, reminders and invoices are sent.",
                ),
            ]
        case Role.FINANCIAL_ADMIN:
            return [
                _link(
                    "billing",
                    "Billing",
                    "billing:index",
                    "Sessions waiting to be invoiced, and what is outstanding.",
                ),
                _link(
                    "caseloads",
                    "Caseloads",
                    "counseling:caseload_index",
                    "Which counselees each counselor carries.",
                ),
            ]
        case _:
            return [
                _link(
                    "my_cases",
                    "My counseling",
                    "counseling:my_cases",
                    "Your case, and the way in to booking your next appointment.",
                ),
                _link(
                    "appointments",
                    "My appointments",
                    "scheduling:appointments",
                    "What is booked, and what has already happened.",
                ),
                _link(
                    "messages",
                    "Messages",
                    "messaging:index",
                    "Write to your counselor, privately.",
                ),
                _link(
                    "documents",
                    "My documents",
                    "documents:my_documents",
                    "What you have sent in, and what has been shared with you.",
                ),
                _link(
                    "invoices", "My invoices", "billing:my_invoices", "What is owed, and paying."
                ),
                _link(
                    "details",
                    "My details",
                    "counseling:counselee_profile_edit",
                    "Contact details and who to reach in an emergency.",
                ),
            ]


def navigation(request):
    """Context processor: the links, and whether to show navigation at all.

    ``is_signed_in`` is not ``user.is_authenticated``. A staff session that has
    given its password and not yet its TOTP code is authenticated, and is held at
    the code prompt by apps/accounts/middleware.py — so a sidebar rendered for it is
    a menu of pages that all bounce back to where it already is. The answer comes
    from that middleware rather than being worked out again here, because two
    definitions of "signed in" is one definition and one bug.
    """
    return {
        "nav_links": links_for(getattr(request, "user", None)),
        "is_signed_in": session_is_fully_authenticated(request),
    }
