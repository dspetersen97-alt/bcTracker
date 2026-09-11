"""
Every template renders, and none of them leaks its own commentary.

This file exists because of a bug that every other test in the suite missed. The
templates carry a lot of explanation — why a link is absent, why a stylesheet is
external — written as ``{# ... #}`` and wrapped over two or three lines for
readability. But ``{# ... #}`` is a **single-line** comment in the Django template
language: the lexer only recognises it when the closing ``#}`` is on the same line,
so a wrapped one is not a comment at all. It is text, and it is printed to the
page.

The suite did not notice because it asserts on status codes, on
``response.context``, and on the presence or absence of particular URLs — and a
page can pass all three while opening with ``{# External stylesheet, not a``. The
first person to look at it in a browser saw it immediately.

So the two tests here check the things a human eye checks and assertions usually
do not: that no comment syntax survives into the rendered output, and that no
template is written in a way that would put it there.
"""

import re
from pathlib import Path

import pytest
from django.template.loader import get_template
from django.test import RequestFactory

from apps.accounts.models import Role

#: Non-greedy across newlines, which is the whole point — a match containing a
#: newline is one the template engine will not treat as a comment.
COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)

TEMPLATE_ROOT = Path(__file__).resolve().parent.parent / "templates"


def template_files():
    return sorted(TEMPLATE_ROOT.rglob("*.html"))


def test_there_are_templates_to_check():
    """A guard on the glob, so a moved directory does not silently pass everything."""
    assert len(template_files()) > 20


@pytest.mark.parametrize("path", template_files(), ids=lambda p: str(p.name))
def test_no_comment_is_wrapped_over_more_than_one_line(path):
    """``{% comment %}`` is the multi-line form. ``{# #}`` is not.

    Reported per file rather than as one list, so a failure names the template to
    open instead of handing back a wall of paths.
    """
    source = path.read_text(encoding="utf-8")

    wrapped = [match.group(0) for match in COMMENT.finditer(source) if "\n" in match.group(0)]

    assert not wrapped, (
        f"{path.relative_to(TEMPLATE_ROOT)} has a {{# #}} comment spanning lines, which "
        f"Django prints to the page. Use {{% comment %}}...{{% endcomment %}}. "
        f"First one starts: {wrapped[0][:60]!r}"
    )


@pytest.mark.django_db
class TestRenderedOutput:
    """The same property asserted against real HTML, in case the regex is wrong.

    The check above reads source; this one reads what a browser would receive. If
    the comment syntax ever changes, or a comment arrives from an inclusion tag or
    a context variable, this is the test that still holds.
    """

    @pytest.fixture
    def rendered(self, make_user):
        """base.html for each role, since most of the commentary is in its navigation.

        Rendered directly rather than fetched through a view: the point is the
        template, and going through a view would mean four sign-ins, MFA for three
        of them, and a test that fails for reasons other than the one it is about.
        """

        def _rendered(role):
            request = RequestFactory().get("/")
            request.user = make_user(role)
            return get_template("base.html").render({"user": request.user}, request)

        return _rendered

    @pytest.mark.parametrize("role", list(Role))
    def test_no_comment_markers_reach_the_page(self, rendered, role):
        html = rendered(role)

        assert "{#" not in html
        assert "#}" not in html

    @pytest.mark.parametrize("role", list(Role))
    def test_no_template_tag_reaches_the_page_either(self, rendered, role):
        """The same class of mistake, one layer out: an unrendered ``{%`` or ``{{``.

        Cheap to assert here, and it catches a typo in a tag name that would
        otherwise render as literal text on a page nobody opened.
        """
        html = rendered(role)

        assert "{%" not in html
        assert "{{" not in html
