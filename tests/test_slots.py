"""
Slot generation, with no database at all.

``apps/scheduling/slots.py`` is a pure function precisely so this file can exist:
every awkward case in scheduling is a calendar arithmetic case, and calendar
arithmetic is where a plausible-looking implementation quietly moves somebody's
appointment by an hour twice a year. The rules and overrides below are unsaved
model instances — real field types and real ``covers``/``is_all_day`` behaviour,
no connection.

The daylight-saving tests are the reason to read this file. America/New_York is
used throughout rather than UTC, because a zone with no transitions cannot fail
the assertions that matter.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apps.scheduling import slots
from apps.scheduling.models import AvailabilityOverride, AvailabilityRule, Weekday

EASTERN = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

#: Well before every date used here, so the minimum-notice cutoff only bites in
#: the tests that set out to exercise it.
LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


def rule(weekday, start, end, *, minutes=60, effective_from=None, effective_to=None):
    """An unsaved AvailabilityRule. ``start``/``end`` as "HH:MM" strings."""
    return AvailabilityRule(
        weekday=weekday,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
        slot_minutes=minutes,
        effective_from=effective_from or date(2020, 1, 1),
        effective_to=effective_to,
    )


def closed(on, start=None, end=None):
    return AvailabilityOverride(
        date=on,
        is_available=False,
        start_time=time.fromisoformat(start) if start else None,
        end_time=time.fromisoformat(end) if end else None,
    )


def opened(on, start, end):
    return AvailabilityOverride(
        date=on,
        is_available=True,
        start_time=time.fromisoformat(start),
        end_time=time.fromisoformat(end),
    )


def generate(rules=(), overrides=(), *, start, end=None, now=LONG_AGO, **kwargs):
    return slots.generate(
        rules=list(rules),
        overrides=list(overrides),
        zone=EASTERN,
        start_date=start,
        end_date=end or start,
        now=now,
        **kwargs,
    )


def local_starts(found):
    """The wall-clock start of each slot in the counselor's zone.

    Asserting on local times rather than UTC ones, because the counselor's claim
    is about their own clock and that is what has to survive a transition.
    """
    return [slot.start.astimezone(EASTERN).strftime("%Y-%m-%d %H:%M") for slot in found]


# --- the ordinary case ----------------------------------------------------


def test_a_three_hour_window_becomes_three_hourly_slots():
    # 3 March 2026 is a Tuesday.
    found = generate([rule(Weekday.TUESDAY, "09:00", "12:00")], start=date(2026, 3, 3))

    assert local_starts(found) == [
        "2026-03-03 09:00",
        "2026-03-03 10:00",
        "2026-03-03 11:00",
    ]
    assert {slot.minutes for slot in found} == {60}


def test_a_window_that_does_not_divide_evenly_loses_the_remainder():
    """Better to offer two clean hours than a 20-minute appointment nobody wants."""
    found = generate([rule(Weekday.TUESDAY, "09:00", "11:20")], start=date(2026, 3, 3))

    assert local_starts(found) == ["2026-03-03 09:00", "2026-03-03 10:00"]


def test_a_rule_only_applies_on_its_own_weekday():
    monday_rule = [rule(Weekday.MONDAY, "09:00", "10:00")]

    assert generate(monday_rule, start=date(2026, 3, 3)) == []
    assert local_starts(generate(monday_rule, start=date(2026, 3, 2))) == ["2026-03-02 09:00"]


def test_the_effective_range_bounds_the_rule():
    bounded = [
        rule(
            Weekday.TUESDAY,
            "09:00",
            "10:00",
            effective_from=date(2026, 3, 10),
            effective_to=date(2026, 3, 10),
        )
    ]

    assert generate(bounded, start=date(2026, 3, 3)) == []
    assert generate(bounded, start=date(2026, 3, 10)) != []
    assert generate(bounded, start=date(2026, 3, 17)) == []


def test_two_rules_describing_the_same_hour_offer_it_once():
    """Overlapping windows are legal — "9–12 hourly, plus 11–13 for a long one"."""
    found = generate(
        [
            rule(Weekday.TUESDAY, "09:00", "12:00"),
            rule(Weekday.TUESDAY, "10:00", "13:00"),
        ],
        start=date(2026, 3, 3),
    )

    assert local_starts(found) == [
        "2026-03-03 09:00",
        "2026-03-03 10:00",
        "2026-03-03 11:00",
        "2026-03-03 12:00",
    ]


def test_slots_come_back_in_time_order_across_days():
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "11:00")],
        start=date(2026, 3, 3),
        end=date(2026, 3, 17),
    )

    assert local_starts(found) == sorted(local_starts(found))
    assert len(found) == 6


# --- daylight saving ------------------------------------------------------


def test_nine_am_stays_nine_am_across_the_spring_transition():
    """The assertion this module exists for.

    US daylight saving begins on 8 March 2026. Stepping the slot cursor in UTC
    instead of local time would silently move the Monday-after appointment to
    10am, and nothing else in the system would notice.
    """
    found = generate(
        [rule(Weekday.MONDAY, "09:00", "10:00")],
        start=date(2026, 3, 2),
        end=date(2026, 3, 9),
    )

    assert local_starts(found) == ["2026-03-02 09:00", "2026-03-09 09:00"]
    # And the proof that the zone really did change underneath: the same wall
    # clock is a different absolute instant on either side.
    assert [slot.start.astimezone(UTC).hour for slot in found] == [14, 13]


def test_nine_am_stays_nine_am_across_the_autumn_transition():
    # Daylight saving ends on 1 November 2026.
    found = generate(
        [rule(Weekday.MONDAY, "09:00", "10:00")],
        start=date(2026, 10, 26),
        end=date(2026, 11, 2),
    )

    assert local_starts(found) == ["2026-10-26 09:00", "2026-11-02 09:00"]
    assert [slot.start.astimezone(UTC).hour for slot in found] == [13, 14]


def test_a_window_over_a_repeated_hour_keeps_the_wall_clock():
    """The documented trade in ``_cut``: wall clock wins over absolute duration.

    On 1 November 2026 the local hour 01:00–01:59 happens twice. Both ends of a
    slot are converted independently, so 01:00–02:00 comes out as the counselor's
    stated hours — and is really two hours long, because 01:00 resolves to EDT and
    02:00 to EST.

    ``Slot.minutes`` still says 60. That is not a second bug: subtracting two aware
    datetimes that share a ``tzinfo`` object gives the wall-clock difference by
    design, so the property reports the hour the counselor meant. It does mean
    ``services.is_bookable``'s length comparison and ``Booking.range_for``'s
    absolute arithmetic disagree for a slot spanning a transition. US and European
    clocks change at 1–2am, so no real office hour is affected, and the
    alternative — stepping the cursor in absolute time — would break the
    transition guarantee above, which matters every single week.
    """
    found = generate([rule(Weekday.SUNDAY, "01:00", "03:00")], start=date(2026, 11, 1))

    assert local_starts(found) == ["2026-11-01 01:00", "2026-11-01 02:00"]
    assert [slot.minutes for slot in found] == [60, 60]
    absolute = [(slot.end.astimezone(UTC) - slot.start.astimezone(UTC)) for slot in found]
    assert absolute == [timedelta(hours=2), timedelta(hours=1)]


def test_a_window_over_a_nonexistent_hour_does_not_raise():
    """02:00–03:00 does not exist on 8 March 2026. It must not take the page down.

    ``fold=0`` resolves it by rule instead. An exception here would mean a
    counselor with early-morning hours could break their own booking page for one
    Sunday a year.
    """
    found = generate([rule(Weekday.SUNDAY, "02:00", "04:00")], start=date(2026, 3, 8))

    assert len(found) == 2
    assert all(slot.start.tzinfo is not None for slot in found)


# --- overrides ------------------------------------------------------------


def test_an_all_day_closure_empties_the_day():
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        [closed(date(2026, 3, 3))],
        start=date(2026, 3, 3),
    )

    assert found == []


def test_an_all_day_closure_does_not_cancel_an_extra_window_on_the_same_date():
    """ "The office is shut, but I will see one person at 4" is a real thing to want."""
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        [closed(date(2026, 3, 3)), opened(date(2026, 3, 3), "16:00", "17:00")],
        start=date(2026, 3, 3),
    )

    assert local_starts(found) == ["2026-03-03 16:00"]


def test_a_partial_closure_removes_only_the_slots_it_covers():
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        [closed(date(2026, 3, 3), "10:00", "11:00")],
        start=date(2026, 3, 3),
    )

    assert local_starts(found) == ["2026-03-03 09:00", "2026-03-03 11:00"]


def test_a_closure_touching_a_slot_boundary_does_not_remove_it():
    """Half-open comparison. Otherwise a meeting ending at 10 would eat the 10am."""
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        [closed(date(2026, 3, 3), "08:00", "09:00")],
        start=date(2026, 3, 3),
    )

    assert "2026-03-03 09:00" in local_starts(found)


def test_an_override_only_affects_its_own_date():
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "10:00")],
        [closed(date(2026, 3, 10))],
        start=date(2026, 3, 3),
        end=date(2026, 3, 17),
    )

    assert local_starts(found) == ["2026-03-03 09:00", "2026-03-17 09:00"]


def test_an_extra_window_uses_the_default_length():
    """An override has no slot_minutes of its own, so the counselor's default applies."""
    found = generate(
        overrides=[opened(date(2026, 3, 3), "16:00", "17:30")],
        start=date(2026, 3, 3),
        default_minutes=45,
    )

    assert [slot.minutes for slot in found] == [45, 45]


# --- busy time ------------------------------------------------------------


def busy_at(day, hour, minutes=60):
    start = datetime.combine(day, time(hour)).replace(tzinfo=EASTERN)
    return (start, start + timedelta(minutes=minutes))


def test_an_existing_appointment_removes_its_slot():
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        start=date(2026, 3, 3),
        busy=[busy_at(date(2026, 3, 3), 10)],
    )

    assert local_starts(found) == ["2026-03-03 09:00", "2026-03-03 11:00"]


def test_back_to_back_appointments_do_not_hide_each_other():
    """The same half-open convention as ``Booking.range_for``.

    Getting this wrong would hide every slot merely touching an existing
    appointment, which on a full morning means offering nothing at all.
    """
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        start=date(2026, 3, 3),
        busy=[busy_at(date(2026, 3, 3), 9)],
    )

    assert local_starts(found) == ["2026-03-03 10:00", "2026-03-03 11:00"]


def test_a_long_appointment_removes_every_slot_it_straddles():
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        start=date(2026, 3, 3),
        busy=[busy_at(date(2026, 3, 3), 9, minutes=120)],
    )

    assert local_starts(found) == ["2026-03-03 11:00"]


def test_an_appointment_running_into_a_slot_removes_all_of_it():
    """A 90-minute session from 9 takes the 10am too. Half of an hour is no use."""
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        start=date(2026, 3, 3),
        busy=[busy_at(date(2026, 3, 3), 9, minutes=90)],
    )

    assert local_starts(found) == ["2026-03-03 11:00"]


# --- minimum notice ------------------------------------------------------


def test_slots_inside_the_notice_window_are_not_offered():
    # Monday 09:00 Eastern. The Tuesday morning is 24-27 hours away.
    now = datetime(2026, 3, 2, 9, tzinfo=EASTERN)
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        start=date(2026, 3, 3),
        now=now,
        notice_hours=26,
    )

    assert local_starts(found) == ["2026-03-03 11:00"]


def test_no_notice_offers_everything_still_in_the_future():
    now = datetime(2026, 3, 3, 10, 30, tzinfo=EASTERN)
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "12:00")],
        start=date(2026, 3, 3),
        now=now,
        notice_hours=0,
    )

    assert local_starts(found) == ["2026-03-03 11:00"]


# --- grouping ------------------------------------------------------------


def test_group_by_day_uses_the_viewers_zone_not_the_counselors():
    """A counselee in another state should see their own Tuesday.

    A 23:00 Eastern slot is already Wednesday in London, and grouping it under
    Tuesday would put it under a heading the viewer does not recognise.
    """
    found = generate([rule(Weekday.TUESDAY, "22:00", "23:00")], start=date(2026, 3, 3))

    eastern = slots.group_by_day(found, EASTERN)
    london = slots.group_by_day(found, ZoneInfo("Europe/London"))

    assert [day for day, _ in eastern] == [date(2026, 3, 3)]
    assert [day for day, _ in london] == [date(2026, 3, 4)]


def test_group_by_day_returns_days_in_order_with_their_slots():
    found = generate(
        [rule(Weekday.TUESDAY, "09:00", "11:00")],
        start=date(2026, 3, 3),
        end=date(2026, 3, 17),
    )

    grouped = slots.group_by_day(found, EASTERN)

    assert [day for day, _ in grouped] == [date(2026, 3, 3), date(2026, 3, 10), date(2026, 3, 17)]
    assert all(len(day_slots) == 2 for _, day_slots in grouped)


def test_no_rules_means_no_slots():
    assert generate(start=date(2026, 3, 3), end=date(2026, 4, 3)) == []
