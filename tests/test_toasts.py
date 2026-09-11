"""
Where a message goes, and how it leaves.

A confirmation ("Case updated.") appears as a toast in the top right corner and
fades out on its own. Anything at warning level or above keeps the banner in the
flow of the page. The distinction is the point of the whole arrangement, so it is
what these tests are about — not the appearance, which nothing can assert.

Two things are worth stating about what is asserted here. First, the promise being
kept is *a message somebody has to act on does not time out*: if a warning ever
lands in the fading stack, that is the bug, and half of this file is watching for
it. Second, the toast fades and can be dismissed with no JavaScript at all — the
CSP has no 'unsafe-inline' and there is no script anywhere in this application —
so the mechanism is a CSS animation and a checkbox the page can see with
``:has()``. The template and the stylesheet have to agree on those hooks, and
nothing else in the suite would notice if a rename broke the pair, so the
stylesheet is read as a file here.

base.html is rendered directly with a request carrying real message storage,
rather than by signing somebody in and doing something worth confirming: the
levels are the subject, and going through a view would mean a database, MFA, and a
test that fails for reasons other than the one it is about.
"""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib import messages as django_messages
from django.contrib.messages import constants
from django.contrib.messages.storage import default_storage
from django.contrib.messages.storage.base import Message
from django.template.loader import get_template
from django.test import RequestFactory

from apps.core.templatetags.message_tags import banners, toasts

STYLESHEET = Path(settings.BASE_DIR) / "static" / "css" / "bctracker.css"


# --- helpers ---------------------------------------------------------------


def render_base(*added):
    """base.html, with ``added`` put through the real messages framework.

    Each argument is ``(level, text)``. The storage is attached by hand because
    there is no middleware in front of a RequestFactory request; from there on the
    context processor and the template do exactly what they do in a browser.
    """
    request = RequestFactory().get("/")
    request.session = {}
    request._messages = default_storage(request)
    for level, text in added:
        django_messages.add_message(request, level, text)
    return get_template("base.html").render({}, request)


def container(html, css_class):
    """The inside of one message list, or "" if the page did not render it."""
    match = re.search(rf'<ul class="{css_class}"[^>]*>(.*?)</ul>', html, re.S)
    return match.group(1) if match else ""


def stylesheet_rule(selector):
    """The body of one rule, found by a selector standing at the start of a line."""
    css = STYLESHEET.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", css)
    assert match, f"the stylesheet has no rule for {selector}"
    return match.group(1)


# --- which messages go where ----------------------------------------------


class TestWhereAMessageLands:
    def test_a_confirmation_becomes_a_toast(self):
        html = render_base((constants.SUCCESS, "Case updated."))

        assert "Case updated." in container(html, "toasts")
        assert "Case updated." not in container(html, "messages")

    @pytest.mark.parametrize("level", [constants.WARNING, constants.ERROR])
    def test_anything_to_act_on_stays_on_the_page(self, level):
        """The promise. A warning in the fading stack is a warning somebody misses,
        and the one this application actually sends lists the weeks a recurring
        series had to skip."""
        html = render_base((level, "Those weeks were skipped."))

        assert "Those weeks were skipped." in container(html, "messages")
        assert "Those weeks were skipped." not in container(html, "toasts")

    def test_one_action_can_produce_both_at_once(self):
        """Booking a weekly series does exactly this: a confirmation of what was
        booked, and a warning about what could not be."""
        html = render_base(
            (constants.SUCCESS, "Booked 8 weekly sessions."),
            (constants.WARNING, "These weeks were skipped: 03 Mar 2026."),
        )

        assert "Booked 8 weekly sessions." in container(html, "toasts")
        assert "These weeks were skipped" in container(html, "messages")

    def test_an_aside_is_a_toast_as_well(self):
        """INFO, which in this application is "Google Calendar was not connected" —
        nothing went wrong and there is nothing to do about it."""
        html = render_base((constants.INFO, "Google Calendar was not connected."))

        assert "Google Calendar was not connected." in container(html, "toasts")

    def test_an_empty_container_is_not_rendered(self):
        """A fixed, empty list in the corner and a stray gap above the content."""
        html = render_base((constants.SUCCESS, "Saved."))

        assert 'class="toasts"' in html
        assert 'class="messages"' not in html

    def test_a_page_with_nothing_to_say_has_neither(self):
        html = render_base()

        assert 'class="toasts"' not in html
        assert 'class="messages"' not in html

    def test_the_level_is_still_on_the_element(self):
        """So the stripe can differ, and so a stylesheet can tell them apart at all."""
        html = render_base((constants.INFO, "Nothing was connected."))

        assert 'class="toast toast-info"' in container(html, "toasts")

    def test_a_toast_is_announced_rather_than_only_seen(self):
        """It appears away from where the reader was looking and then leaves, so a
        live region is the only thing that makes it available to anyone who is not
        watching that corner."""
        html = render_base((constants.SUCCESS, "Saved."))

        assert re.search(r'<ul class="toasts"[^>]*role="status"', html)


class TestTheSplitItself:
    """The filters on their own, where the level boundary is easy to see."""

    @pytest.mark.parametrize(
        "level", [constants.DEBUG, constants.INFO, constants.SUCCESS, 25, constants.WARNING - 1]
    )
    def test_everything_below_warning_may_fade(self, level):
        queue = [Message(level, "text")]

        assert toasts(queue) == queue
        assert banners(queue) == []

    @pytest.mark.parametrize("level", [constants.WARNING, constants.ERROR, 50])
    def test_warning_and_above_never_does(self, level):
        queue = [Message(level, "text")]

        assert banners(queue) == queue
        assert toasts(queue) == []

    def test_the_two_together_are_the_whole_queue(self):
        """Nothing may fall between them, whatever level a caller invents."""
        queue = [Message(level, f"at {level}") for level in (5, 10, 20, 25, 30, 40, 99)]

        assert len(toasts(queue)) + len(banners(queue)) == len(queue)

    def test_the_order_within_a_group_is_kept(self):
        queue = [Message(constants.SUCCESS, "First."), Message(constants.INFO, "Second.")]

        assert [str(message) for message in toasts(queue)] == ["First.", "Second."]


# --- leaving, with no JavaScript ------------------------------------------


class TestHowAToastLeaves:
    def test_the_stylesheet_fades_it_out(self):
        css = STYLESHEET.read_text(encoding="utf-8")

        assert "@keyframes toast-out" in css
        assert "visibility: hidden" in css, (
            "the fade has to end hidden, or the dismiss checkbox stays a tab stop on "
            "a toast nobody can see"
        )
        assert "animation" in stylesheet_rule(".toast")

    def test_a_banner_is_left_alone(self):
        """The other half of the same promise, asserted against the stylesheet: a
        banner that faded would be a warning that timed out."""
        assert "animation" not in stylesheet_rule(".message")
        assert "animation" not in stylesheet_rule(".messages")

    def test_it_can_be_dismissed_and_the_stylesheet_knows_how(self):
        """A checkbox inside the toast, so ``:has()`` can hide the toast around it.
        No id, no form, no script — and the template and the stylesheet have to name
        the same thing or the button does nothing at all."""
        markup = container(render_base((constants.SUCCESS, "Saved.")), "toasts")

        assert '<input type="checkbox"' in markup
        assert "Dismiss this message" in markup, "the control needs a name to be read out"
        assert "display: none" in stylesheet_rule(".toast:has(input:checked)")

    def test_reading_it_can_be_given_more_time(self):
        """Seven seconds is not long for anybody, and it is nothing at all for
        somebody using a magnifier."""
        css = STYLESHEET.read_text(encoding="utf-8")

        assert "animation-play-state: paused" in css
        assert ".toasts:hover .toast" in css
        assert ".toasts:focus-within .toast" in css

    def test_less_motion_can_be_asked_for(self):
        """Reduced motion drops the slide and keeps the fade — a fade is not what
        causes trouble, and dropping it would mean the toast never left.

        More than one block answers that query now: the sliding sidebar has its own,
        which has to sit after the rules it overrides. So the toast's block is picked
        out by what it mentions rather than by being the first one found.
        """
        css = STYLESHEET.read_text(encoding="utf-8")
        blocks = re.findall(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", css, re.S)

        assert blocks, "the stylesheet does not answer prefers-reduced-motion"
        ours = [block for block in blocks if ".toast" in block]
        assert ours, "nothing answers prefers-reduced-motion for the toasts"
        assert "translateX" not in ours[0]
