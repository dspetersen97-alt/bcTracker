"""
Account URLs.

Every route here is named, because tests/test_access_matrix.py enumerates the
URLconf by name and fails if a route is not listed in the matrix. An unnamed
route would slip past that check.
"""

from django.urls import path, register_converter

from apps.accounts import views


class TokenConverter:
    """Matches a ``secrets.token_urlsafe`` value and nothing else.

    Narrower than ``str`` on purpose: a token that cannot contain a slash, a dot,
    or a percent-escape cannot be used to smuggle path traversal or a redirect
    into a view that treats it as opaque.
    """

    regex = r"[A-Za-z0-9_-]{20,128}"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(TokenConverter, "token")

app_name = "accounts"

urlpatterns = [
    path("login/", views.BcTrackerLoginView.as_view(), name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("password/", views.password_change, name="password_change"),
    # Staff sign-in with a ministry Google account. Never a counselee, and never a
    # substitute for the second factor — see apps/accounts/sso.py.
    path("login/google/", views.google_login, name="google_login"),
    path("login/google/callback/", views.google_login_callback, name="google_login_callback"),
    # Passwordless login, for counselees who have opted in.
    path("login/link/", views.magic_link_request, name="magic_link_request"),
    path("login/link/sent/", views.magic_link_sent, name="magic_link_sent"),
    path("login/link/<token:token>/", views.magic_link_consume, name="magic_link_consume"),
    # First-password setup from an administrator's invitation.
    path("invitation/<token:token>/", views.invitation_accept, name="invitation_accept"),
    # Second factor.
    path("mfa/setup/", views.mfa_setup, name="mfa_setup"),
    path("mfa/verify/", views.mfa_verify, name="mfa_verify"),
    # The account page: who you are signed in as, and the security settings that
    # belong to the person rather than to the counseling work. The site root is
    # the counseling dashboard — see config/urls.py.
    path("account/", views.home, name="home"),
]
