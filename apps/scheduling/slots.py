"""
Turning office hours into bookable times.

One pure function, ``generate``. It takes rows and returns times; it opens no
database connection and reads no settings, which is what makes the awkward cases
cheap to test — daylight saving, a closure that only covers part of a morning, an
appointment already in the diary, the minimum-notice cutoff.

The order of operations is deliberate and is the whole algorithm:

    1. expand the weekly rules across the requested dates
    2. add any extra windows the counselor opened for a specific date
    3. cut the day into slots, **stepping in local wall-clock time**
    4. drop slots that collide with a closure, a booking, or busy time elsewhere
    5. drop slots that fall inside the minimum-notice window

Step 3 is where time zones are won or lost. Slot boundaries are built as local
naive times and converted afterwards, so on the Sunday the clocks change the
counselor's 9am is still 9am. Stepping in UTC instead would silently move every
appointment by an hour twice a year.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

#: A slot is discarded if it starts within this many hours, unless the caller says
#: otherwise. Overridden per counselor from CounselorProfile.booking_notice_hours.
DEFAULT_NOTICE_HOURS = 24


@dataclass(frozen=True, order=True)
class Slot:
    """One bookable appointment time. Both ends timezone-aware."""

    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def generate(
    *,
    rules,
    overrides,
    zone,
    start_date,
    end_date,
    now,
    busy=(),
    notice_hours=DEFAULT_NOTICE_HOURS,
    default_minutes=60,
) -> list[Slot]:
    """Every slot a counselee could book between two dates, inclusive.

    ``rules`` and ``overrides`` are AvailabilityRule and AvailabilityOverride rows
    (or anything with the same attributes — the function never queries).
    ``busy`` is an iterable of ``(start, end)`` aware datetime pairs: existing
    bookings, and in due course the counselor's Google free/busy, which is why it
    is a plain sequence of intervals rather than a queryset.

    ``zone`` is the counselor's ``ZoneInfo``: office hours are stated in the
    counselor's time, not the counselee's and not the server's.
    """
    rules = list(rules)
    overrides = list(overrides)
    busy = [(start, end) for start, end in busy]
    cutoff = now + timedelta(hours=notice_hours)

    found: dict[tuple[datetime, datetime], Slot] = {}
    for day in _dates(start_date, end_date):
        blocked = _closures(day, overrides, zone)
        for start_time, end_time, minutes in _windows(day, rules, overrides, default_minutes):
            for slot in _cut(day, start_time, end_time, minutes, zone):
                if slot.start < cutoff:
                    continue
                if any(_overlaps(slot, start, end) for start, end in blocked):
                    continue
                if any(_overlaps(slot, start, end) for start, end in busy):
                    continue
                # Two rules can describe the same hour; the counselee should be
                # offered it once.
                found.setdefault((slot.start, slot.end), slot)

    return sorted(found.values())


def group_by_day(slots, zone):
    """``[(date, [slot, ...]), ...]`` in the given zone, for rendering.

    Grouping in the *viewer's* zone rather than the counselor's, because a
    counselee in another state should see their own Tuesday.
    """
    days: dict = {}
    for slot in slots:
        days.setdefault(slot.start.astimezone(zone).date(), []).append(slot)
    return sorted(days.items())


# --- internals ------------------------------------------------------------


def _dates(start_date, end_date):
    day = start_date
    while day <= end_date:
        yield day
        day += timedelta(days=1)


def _windows(day, rules, overrides, default_minutes):
    """The open windows on one date, as ``(start_time, end_time, slot_minutes)``.

    An all-day closure short-circuits the weekly pattern but not the extra
    windows a counselor opened for that date: "the office is shut, but I will see
    one person at 4" is a real thing to want, and expressing it needs the two
    kinds of override not to cancel each other out.
    """
    shut = any(
        override.date == day and not override.is_available and override.is_all_day
        for override in overrides
    )
    windows = (
        [] if shut else [(r.start_time, r.end_time, r.slot_minutes) for r in rules if r.covers(day)]
    )
    windows += [
        (override.start_time, override.end_time, default_minutes)
        for override in overrides
        if override.date == day and override.is_available
    ]
    return windows


def _closures(day, overrides, zone):
    """Absolute intervals blocked out on one date."""
    return [
        (
            _localize(day, override.start_time, zone),
            _localize(day, override.end_time, zone),
        )
        for override in overrides
        if override.date == day and not override.is_available and not override.is_all_day
    ]


def _cut(day, start_time, end_time, minutes, zone):
    """Slice one local window into whole slots.

    The cursor is a *naive* local datetime and the step is added to it before
    conversion, so the slots stay on the counselor's wall clock. A window that
    does not divide evenly loses its remainder rather than offering a short
    appointment.
    """
    cursor = datetime.combine(day, start_time)
    window_end = datetime.combine(day, end_time)
    step = timedelta(minutes=minutes)

    while cursor + step <= window_end:
        finish = cursor + step
        yield Slot(
            start=cursor.replace(tzinfo=zone),
            # Converted independently of the start, which is what makes a slot
            # spanning a clock change come out as the right wall-clock hour on
            # both sides rather than start + 60 minutes of absolute time.
            end=finish.replace(tzinfo=zone),
        )
        cursor = finish


def _localize(day, time_of_day, zone) -> datetime:
    """A local time on a date, as an absolute instant.

    ``replace(tzinfo=...)`` with a ZoneInfo resolves a nonexistent or repeated
    local time by rule (``fold=0``) rather than raising. For office hours that is
    the right trade: nobody schedules counseling for 2:30am on the Sunday the
    clocks go forward, and an exception there would take down a booking page.
    """
    return datetime.combine(day, time_of_day).replace(tzinfo=zone)


def _overlaps(slot: Slot, start, end) -> bool:
    """Half-open comparison, so back-to-back intervals do not collide.

    The same convention as ``Booking.range_for``. Getting it wrong here would
    hide every slot that merely touches an existing appointment.
    """
    return slot.start < end and start < slot.end
