"""
Response headers, and the policy they carry.

The Content-Security-Policy exists in two places: ``CONTENT_SECURITY_POLICY`` in
config/settings/base.py, sent by ``apps.core.middleware.SecurityHeadersMiddleware``,
and a copy in compose/caddy/Caddyfile, which is the one a browser sees in the
deployed stack because Caddy's ``header`` directive replaces rather than appends.

Two copies is a deliberate trade — the app must carry its own policy so that
development and this test suite run under the same rules production does, and
Caddy must carry one so that responses which never reach Django still have it.
What makes it safe is that the drift is caught here: the tests below read the
Caddyfile and compare, so a directive relaxed in one place fails until it is
relaxed in both, on purpose.

The other half of a strict policy is that the templates can live under it. There
is no ``unsafe-inline`` anywhere, so an inline ``<script>`` or ``style=`` would
work in every developer's browser with the middleware disabled and break in
production. The last class here refuses one at the source.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

pytestmark = pytest.mark.django_db

CADDYFILE = Path(settings.BASE_DIR) / "compose" / "caddy" / "Caddyfile"
TEMPLATES = Path(settings.BASE_DIR) / "templates"


def caddy_header(name: str) -> str:
    """The value Caddy sends for a header, read out of the Caddyfile.

    Parsed rather than imported because a Caddyfile is not Python. The pattern is
    deliberately narrow: if the file is restructured so this stops matching, the
    test fails rather than quietly passing on a header nobody is sending.
    """
    match = re.search(rf'^\s*{re.escape(name)}\s+"(.*)"\s*$', CADDYFILE.read_text(), re.MULTILINE)
    assert match, f"{name} is not set in {CADDYFILE.name}"
    return match.group(1)


class TestEveryResponseCarriesThePolicy:
    @pytest.mark.parametrize(
        "url_name",
        ["accounts:login", "accounts:magic_link_request", "healthz"],
    )
    def test_an_anonymous_page_does(self, client, url_name):
        response = client.get(reverse(url_name))

        assert response["Content-Security-Policy"] == settings.CONTENT_SECURITY_POLICY

    def test_a_signed_in_page_does(self, counselor, sign_in):
        response = sign_in(counselor).get(reverse("counseling:dashboard"))

        assert response["Content-Security-Policy"] == settings.CONTENT_SECURITY_POLICY

    def test_a_redirect_does(self, client):
        """Redirects are most of what an unauthenticated visitor gets, and a
        redirect body is still a document a browser will render if it arrives."""
        response = client.get(reverse("counseling:dashboard"))

        assert response.status_code == 302
        assert "Content-Security-Policy" in response

    def test_a_404_does(self, client):
        response = client.get("/no-such-page/")

        assert response.status_code == 404
        assert "Content-Security-Policy" in response

    def test_a_document_download_does(self, counselor, scenario, sign_in):
        """The one response that is somebody else's file. It is served as an
        attachment, but a policy on it costs nothing and covers the day something
        renders one inline."""
        world = scenario(counselor)

        response = sign_in(counselor).get(
            reverse("documents:download", args=[world.document.public_id])
        )

        assert response.status_code == 200
        assert "Content-Security-Policy" in response
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Cross-Origin-Resource-Policy"] == "same-origin"

    def test_the_other_headers_are_there_too(self, client):
        response = client.get(reverse("accounts:login"))

        assert response["Permissions-Policy"] == settings.PERMISSIONS_POLICY
        assert response["X-Frame-Options"] == "DENY"
        assert response["X-Content-Type-Options"] == "nosniff"
        assert response["Referrer-Policy"] == "same-origin"
        assert response["Cross-Origin-Opener-Policy"] == "same-origin"


class TestWhatThePolicySays:
    @pytest.mark.parametrize("escape", ["unsafe-inline", "unsafe-eval", "*", "http:", "https:"])
    def test_it_allows_no_way_out(self, escape):
        """Each of these is the usual way a policy stops meaning anything: one
        template needs an inline handler, and a wildcard gets added for all of
        them."""
        assert escape not in settings.CONTENT_SECURITY_POLICY

    @pytest.mark.parametrize(
        "directive",
        [
            "default-src 'self'",
            # No fallback: default-src does not cover these two, so a policy
            # without them still allows a plugin or a framed page.
            "object-src 'none'",
            "frame-ancestors 'none'",
            # Stops an injected <base> from re-pointing every relative URL, and an
            # injected form from posting a counselee's answers somewhere else.
            "base-uri 'self'",
            "form-action 'self'",
        ],
    )
    def test_it_states_the_directives_that_do_not_fall_back(self, directive):
        assert directive in settings.CONTENT_SECURITY_POLICY

    def test_no_capability_is_left_open(self):
        for capability in ["camera", "microphone", "geolocation", "display-capture"]:
            assert f"{capability}=()" in settings.PERMISSIONS_POLICY


class TestTheTwoCopiesAgree:
    def test_the_content_security_policy_matches(self):
        """If this fails, decide which copy is right and change both — a policy
        tested here and a different one served to counselees is worse than either.
        """
        assert caddy_header("Content-Security-Policy") == settings.CONTENT_SECURITY_POLICY

    def test_the_permissions_policy_matches(self):
        assert caddy_header("Permissions-Policy") == settings.PERMISSIONS_POLICY

    def test_the_referrer_policy_matches(self):
        assert caddy_header("Referrer-Policy") == settings.SECURE_REFERRER_POLICY

    def test_the_frame_options_match(self):
        assert caddy_header("X-Frame-Options") == settings.X_FRAME_OPTIONS


class TestTheTemplatesCanLiveUnderIt:
    """A policy nothing was written against is one somebody eventually relaxes."""

    def _templates(self):
        return sorted(TEMPLATES.rglob("*.html"))

    def _markup(self, path):
        """A template's markup with its comments removed — both syntaxes.

        The comments in these templates explain *why* there is no inline style or
        script, so searching the raw text finds the explanation and calls it the
        violation.

        Both forms are stripped because ``{# … #}`` only works on one line, so the
        longer explanations are ``{% comment %}`` blocks; see tests/test_templates.py
        for the bug that taught us the difference.
        """
        source = path.read_text(encoding="utf-8")
        source = re.sub(r"{#.*?#}", "", source, flags=re.DOTALL)
        return re.sub(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", "", source, flags=re.DOTALL)

    def test_there_are_templates_to_check(self):
        """Guards the three tests below against silently passing on an empty list
        if the templates ever move."""
        assert len(self._templates()) > 10

    def test_none_of_them_has_an_inline_script(self):
        offenders = [
            path.relative_to(TEMPLATES).as_posix()
            for path in self._templates()
            if "<script" in self._markup(path)
        ]

        assert offenders == []

    def test_none_of_them_has_a_style_attribute_or_a_style_block(self):
        """Every style belongs in static/css/bctracker.css. Not a matter of taste:
        ``style-src 'self'`` blocks both, so one of these is a layout that silently
        breaks in production only."""
        offenders = [
            path.relative_to(TEMPLATES).as_posix()
            for path in self._templates()
            if re.search(r"<style|\sstyle=", self._markup(path))
        ]

        assert offenders == []

    def test_none_of_them_has_a_javascript_url_or_an_event_handler(self):
        offenders = [
            path.relative_to(TEMPLATES).as_posix()
            for path in self._templates()
            if re.search(r"javascript:|\son(click|change|submit|load|error)=", self._markup(path))
        ]

        assert offenders == []
