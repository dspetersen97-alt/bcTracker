"""
Google Calendar integration.

**bcTracker is authoritative and the flow is one-way.** Appointments are pushed
to Google; nothing is read back except free/busy. That is a deliberate limit, not
a first draft. Two-way sync means resolving conflicts between two systems that
both believe they own the appointment, and the failure mode is an appointment
that quietly moves or disappears — for a counseling session, with a person
turning up at a door, the wrong thing to get wrong. If a counselor deletes the
event in Google, the booking still stands and we push it again.

Free/busy is read because it is the one thing Google knows and we do not: the
counselor's dentist appointment. It comes back as opaque intervals — no titles,
no attendees — and lands in ``services.bookable_slots(extra_busy=...)``, which
already speaks in bare ``(start, end)`` pairs.

The modules:

  * ``oauth`` — the consent handshake, the ``hd`` hosted-domain check, and revoke.
  * ``credentials`` — sealing, unsealing, and refreshing the stored tokens.
  * ``client`` — the three HTTP calls, and the error taxonomy the callers branch on.
  * ``sync`` — reconciling one counselor's bookings, used by the view and the cron
    command alike.

Nothing here is imported at module scope by the booking code. The integration is
optional, may be switched off entirely, and must never be able to stop somebody
booking an appointment: every call site treats a Google failure as something to
log and retry on the next tick.
"""
