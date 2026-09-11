"""
The frame around every page: the header that stays, and the menu that slides.

Both are asserted against the stylesheet's text, which is unusual enough to say
why. There is no JavaScript in this application, so the header being pinned and the
menu sliding are not behaviour a test client can drive — they are declarations in
static/css/bctracker.css, and the only thing a test can do is check that the
declarations are the ones that produce the behaviour. Thin, but not worthless: what
these catch is a rule deleted in a refactor, a hard-coded height that no longer
matches the header it was measured from, and the two ordering mistakes below, each
of which silently undoes the thing it looks like it is doing.

The ordering mistakes, since they are the reason this file exists at all:

  * a pinned header needs a z-index, and one wrong number puts it over the toast
    that says what just happened or under the content it is meant to float above;
  * ``prefers-reduced-motion`` rules are no more specific than the phone rules they
    override, so put earlier in the file they hand the movement straight back on a
    small screen — which is where somebody is most likely to be on a bus.
"""

import re
from pathlib import Path

from django.conf import settings

STYLESHEET = Path(settings.BASE_DIR) / "static" / "css" / "bctracker.css"


def css() -> str:
    return STYLESHEET.read_text(encoding="utf-8")


def rule(selector: str, source: str | None = None) -> str:
    """The declarations of the first rule for exactly this selector."""
    source = css() if source is None else source
    match = re.search(rf"(?m)^\s*{re.escape(selector)}\s*\{{([^}}]*)\}}", source)
    assert match, f"the stylesheet has no rule for {selector}"
    return match.group(1)


def media_block(condition: str, containing: str = "") -> tuple[str, int]:
    """The body of an ``@media (condition)`` block, and where in the file it starts.

    Brace-matched rather than pattern-matched, because a block contains nested rules
    and a lazy ``.*?`` would stop at the first inner ``}``. ``containing`` picks
    between blocks that ask the same question — there is more than one
    reduced-motion block, and each one is about a different thing that moves.

    The position comes back so a test can assert that one block is after another;
    with equal specificity, that is the difference between an override and a
    decoration.
    """
    source = css()
    at = 0
    while (start := source.find(f"@media ({condition})", at)) != -1:
        depth = 0
        opening = source.index("{", start)
        for index in range(opening, len(source)):
            if source[index] == "{":
                depth += 1
            elif source[index] == "}":
                depth -= 1
                if depth == 0:
                    body = source[opening + 1 : index]
                    if containing in body:
                        return body, start
                    at = index
                    break
        else:
            raise AssertionError(f"@media ({condition}) is never closed")
    raise AssertionError(f"no @media ({condition}) block mentions {containing!r}")


def z_index(selector: str) -> int:
    match = re.search(r"z-index:\s*(\d+)", rule(selector))
    assert match, f"{selector} has no z-index"
    return int(match.group(1))


def milliseconds(declarations: str, property_name: str) -> int:
    """The delay or duration stated for one property inside a ``transition``."""
    match = re.search(rf"{property_name}\s+(?:0s\s+\w+\s+)?(\d+)ms", declarations)
    assert match, f"no timing for {property_name} in: {declarations.strip()}"
    return int(match.group(1))


PHONE = "max-width: 45rem"
LESS_MOTION = "prefers-reduced-motion: reduce"


class TestTheHeaderStaysWhereItIs:
    def test_it_is_pinned(self):
        """The menu control and the way home are in the header, and a case list is
        long. Scrolling to the bottom of one should not mean scrolling back up to
        leave it."""
        declarations = rule(".site-header")

        assert "position: sticky" in declarations
        assert "top: 0" in declarations

    def test_it_is_sticky_rather_than_fixed(self):
        """A fixed header leaves the flow, and then the top of every page has to be
        padded out from under it — a second number to keep in step with the first."""
        assert "position: fixed" not in rule(".site-header")

    def test_a_confirmation_is_not_hidden_behind_it(self):
        """The toast stack is fixed near the top right, which is exactly where a
        pinned header is."""
        assert z_index(".site-header") < z_index(".toasts")

    def test_the_keyboard_can_still_get_past_it(self):
        """Skip to content is only visible while focused, and a header painted over
        it would leave a keyboard user pressing Enter on something invisible."""
        assert z_index(".site-header") < z_index(".skip-link:focus")

    def test_it_covers_the_menu_that_slides_under_it(self):
        """On a phone the menu is a fixed overlay starting just below the header. If
        the header lost, the top of the menu would be drawn over the brand."""
        assert z_index(".site-header") > int(
            re.search(r"z-index:\s*(\d+)", rule(".sidebar", media_block(PHONE)[0])).group(1)
        )

    def test_an_in_page_jump_lands_below_it(self):
        """Skip to content, or any anchor, would otherwise scroll its target to the
        very top of the viewport — underneath the pinned header."""
        assert "scroll-padding-top" in rule("html")


class TestItsHeightIsStatedOnce:
    """Four things have to agree about how tall the header is, so one of them says it.

    The sidebar sticks below the header, the phone overlay starts below the header,
    and the toast stack clears the header. Written out four times, the first change
    to the header's padding leaves three of them wrong — and wrong here means a menu
    tucked under a brand, which looks like a rendering bug rather than a number.
    """

    def test_the_variable_exists(self):
        assert re.search(r"--header-height:\s*[\d.]+rem", css())

    def test_the_header_itself_is_held_to_it(self):
        """Otherwise the variable is a guess about the header rather than a statement
        of it, and a page with no menu button would be shorter than everything below
        is expecting."""
        assert "min-height: var(--header-height)" in rule(".site-header")

    def test_everything_that_sits_below_the_header_measures_from_it(self):
        for selector, declarations in [
            (".sidebar", rule(".sidebar")),
            (".sidebar (on a phone)", rule(".sidebar", media_block(PHONE)[0])),
            (".toasts", rule(".toasts")),
        ]:
            assert "var(--header-height)" in declarations, (
                f"{selector} states its own idea of how tall the header is"
            )


class TestTheSidebarStaysWithThePage:
    def test_it_is_pinned_below_the_header(self):
        declarations = rule(".sidebar")

        assert "position: sticky" in declarations
        assert "top: var(--header-height)" in declarations

    def test_a_menu_too_long_for_the_window_can_be_scrolled(self):
        """A pinned column cannot grow past the bottom of the screen, so without
        these the last entry of a long menu is unreachable rather than just low."""
        declarations = rule(".sidebar")

        assert "max-height: calc(100vh - var(--header-height))" in declarations
        assert "overflow-y: auto" in declarations


class TestTheMenuSlidesRatherThanBlinks:
    def _collapsed(self, source=None):
        return rule(".nav-toggle:checked ~ .layout > .sidebar", source)

    def test_it_is_moved_out_of_the_way_rather_than_removed(self):
        """``display: none`` cannot be animated, and a column that vanishes between
        one frame and the next reads as something breaking rather than as a menu that
        went somewhere and can be brought back."""
        collapsed = self._collapsed()

        assert "display: none" not in collapsed
        assert "translateX(-100%)" in collapsed

    def test_the_content_gets_the_width_back(self):
        """The slide alone would leave a 14rem hole where the menu used to be: it is
        still a flex item. The negative margin takes it out of the row, and animates
        alongside the transform so the two happen as one movement."""
        collapsed = self._collapsed()

        assert "margin-left: -14rem" in collapsed
        assert "margin-left" in re.search(r"transition:([^;]*)", collapsed).group(1)

    def test_a_menu_nobody_can_see_is_not_a_row_of_tab_stops(self):
        """The reason it is visibility and not opacity. A keyboard user tabbing off
        the end of a page and landing in an invisible menu has no way of knowing
        where they are."""
        assert "visibility: hidden" in self._collapsed()

    def test_it_is_only_hidden_once_it_has_finished_leaving(self):
        """visibility is a step, not a slide, so it has to wait for the slide.
        Hidden at the start and the movement is invisible; hidden too late and it is
        briefly a tab stop nobody can see."""
        collapsed = self._collapsed()

        assert milliseconds(collapsed, "visibility") == milliseconds(collapsed, "transform")

    def test_it_comes_back_the_moment_it_is_asked_for(self):
        """No delay on the way in — there has to be something there to watch
        arrive."""
        assert "visibility 0s;" in rule(".sidebar")

    def test_it_slides_on_a_phone_too(self):
        """Where the menu is an overlay rather than a column, so there is no row to
        close up and no margin to animate — but the same slide."""
        phone = media_block(PHONE)[0]

        assert "translateX(-100%)" in rule(".sidebar", phone)
        assert "display: none" not in rule(".sidebar", phone)
        assert "transform: none" in self._collapsed(phone)


class TestLessMovementIsHonoured:
    def test_the_slide_is_dropped(self):
        block, _ = media_block(LESS_MOTION, ".sidebar")

        assert ".sidebar" in block
        assert "transition: none" in block

    def test_and_dropped_last_of_all(self):
        """The one that would pass a reading of the file and fail in a browser. These
        selectors are no more specific than the phone rules they override, so placed
        earlier they lose to them — and the movement comes back on exactly the small
        screen somebody is most likely to be reading on a bus.
        """
        _, less_motion_at = media_block(LESS_MOTION, ".sidebar")
        _, phone_at = media_block(PHONE)

        assert less_motion_at > phone_at, (
            "the reduced-motion block must come after the phone rules it overrides"
        )
