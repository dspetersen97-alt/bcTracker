"""
The identifier a row is known by outside the database.

Every model whose id appears in a URL, in an email, or on an invoice carries a
``public_id``: ten digits, chosen at random, unrelated to the row's primary key.
The primary key stays a ``bigint`` sequence because that is what foreign keys and
indexes want; what changes is that nothing outside the application ever sees it.

Why this matters more than it looks
-----------------------------------

A sequential id in a URL is two disclosures, and neither of them is theoretical
for a counseling ministry:

  * **It is a counter.** ``/appointments/41/`` tells whoever holds the link that
    the ministry has recorded forty appointments. On a small install that is a
    caseload estimate, and a caseload estimate is the kind of thing a ministry
    would rather not publish to anybody who books a session.
  * **It is enumerable.** The scoping layer refuses a row an actor may not see,
    so guessing an id gets a 404 rather than a record — but a link pasted into a
    message, a support ticket, or a browser's history is a *guessable neighbour*
    of every other record of the same kind. Ten random digits are not.

Ten digits rather than a UUID because these are read aloud and typed. An invoice
reference is quoted on a check and looked up when the bank statement arrives (see
``apps/billing/numbering.py``), and "BC-4820193756" survives a phone call in a way
that "BC-1f3c9a2e-…" does not.

Not a secret, and not treated as one
------------------------------------

A public id is unguessable enough to stop enumeration and nothing more. Every
view still resolves its object through ``for_actor`` and still re-checks its
permission, exactly as before — the id being hard to guess is not why a counselee
cannot read their spouse's document. Anything that relied on the id alone would be
a capability URL, which is a different design with different rules about logging,
referrers, and email.

Zero-first ids are deliberately not generated. A value like ``0041882917`` reads
as a truncated number to a person and gets its leading zero eaten by every
spreadsheet the office pastes it into, so the first digit is always 1–9. That
costs a tenth of the space and leaves nine billion values, which is nine billion
more than a ministry will use.
"""

import re
import secrets

#: Ten digits. Fixed width, so the URL converter can match it exactly and a
#: shorter or longer path segment resolves nowhere rather than reaching a view.
PUBLIC_ID_LENGTH = 10

#: What a public id may look like, as a regex fragment shared by the URL converter
#: and the field's own validation. Anchored by whoever uses it.
PUBLIC_ID_PATTERN = r"[1-9][0-9]{9}"

#: How many times ``unique_public_id`` will try before giving up. A collision needs
#: two of nine billion values to coincide, so reaching two attempts is already
#: surprising and reaching this many means something is wrong with the generator
#: rather than that the space is full.
MAX_ATTEMPTS = 12

_PUBLIC_ID_RE = re.compile(PUBLIC_ID_PATTERN)


def generate_public_id() -> str:
    """Ten random digits, never starting with a zero.

    ``secrets`` rather than ``random``: the point of the value is that it cannot be
    guessed from its neighbours, and ``random`` is seeded from a state an observer
    of enough outputs can recover. The cost of using the stronger generator here is
    nothing, and the version that used ``random`` would look identical in review.
    """
    first = secrets.choice("123456789")
    rest = "".join(secrets.choice("0123456789") for _ in range(PUBLIC_ID_LENGTH - 1))
    return f"{first}{rest}"


class PublicIdExhausted(RuntimeError):
    """``unique_public_id`` could not find a free value. Always a bug, never capacity."""


def unique_public_id(model) -> str:
    """A public id no row of ``model`` is using yet.

    Checked against the table rather than trusted, even though a collision is a
    one-in-nine-billion event. The reason is not the odds: the column is unique, so
    a collision surfaces as an ``IntegrityError`` from whatever the actor was doing —
    booking an appointment, uploading a document — and a counselee told "that could
    not be saved" because two random numbers matched would never find out why.

    The check is not a lock, so two concurrent inserts could still pick the same
    value. The unique constraint is what makes that safe; this only makes it
    absurdly unlikely to be what anybody meets. The same division of labour as the
    booking exclusion constraint — see ``apps/scheduling/services.py``.

    ``all_objects`` where the model has it: a soft-deleted row still holds its id,
    and handing the same id to a new document while the old one is recoverable
    would make the trail ambiguous about which one was read.
    """
    manager = getattr(model, "all_objects", None) or model._default_manager
    for _attempt in range(MAX_ATTEMPTS):
        candidate = generate_public_id()
        if not manager.filter(public_id=candidate).exists():
            return candidate
    raise PublicIdExhausted(
        f"Could not find an unused public id for {model.__name__} in {MAX_ATTEMPTS} attempts."
    )


def looks_like_public_id(value) -> bool:
    """Whether ``value`` has the shape of a public id.

    For the places an id arrives in a query string rather than in the path, where the
    URL converter is not there to reject it — ``?case=``, ``?counselee=``, ``?booking=``.
    A shape check rather than a lookup: whether the row exists, and whether this actor
    may see it, is still the scoping layer's answer. This only stops a value that could
    not name anything from reaching ``reverse()``, which would raise
    ``NoReverseMatch`` and turn a mistyped link into a server error.
    """
    return isinstance(value, str) and _PUBLIC_ID_RE.fullmatch(value) is not None


def backfill_public_ids(apps, app_label: str, model_name: str) -> None:
    """Give every existing row a public id. Called from a ``RunPython`` step.

    A unique non-null column cannot be added to a populated table in one step, so
    each app's ``*_public_id`` migration adds the column nullable, calls this, and
    then tightens it. That ordering is the whole reason this function exists.

    Uses the historical model from ``apps`` rather than the real one, which is the
    rule for data migrations and matters here for an ordinary reason: the real
    ``Document`` manager hides soft-deleted rows, and a row skipped by the backfill
    would still be there when the column is made non-null, and the migration would
    fail on a deployment that had ever deleted a document.

    Ids are collected in memory and checked against that set rather than by
    re-querying, because the rows are not saved until the bulk update at the end
    and a query would not see the ones already assigned in this loop.
    """
    model = apps.get_model(app_label, model_name)
    rows = list(model.objects.filter(public_id__isnull=True).only("pk"))
    if not rows:
        return

    taken = set(model.objects.exclude(public_id__isnull=True).values_list("public_id", flat=True))
    for row in rows:
        candidate = generate_public_id()
        while candidate in taken:
            candidate = generate_public_id()
        taken.add(candidate)
        row.public_id = candidate

    model.objects.bulk_update(rows, ["public_id"], batch_size=500)


class PublicIdConverter:
    """Matches a public id in a URL, and nothing else.

    Narrower than ``str`` and narrower than ``int``, both on purpose. ``str`` would
    match a path segment containing a dot or a percent-escape, which is how a value
    a view treats as opaque becomes a way to smuggle something into it. ``int``
    would accept ``7`` and ``00000000041`` as well as the real thing, which would
    leave the old sequential ids quietly working — and a route that still answers to
    the id it was supposed to stop publishing has not been changed at all.

    The same reasoning as ``apps.accounts.urls.TokenConverter``, which is why the
    two look alike.
    """

    regex = PUBLIC_ID_PATTERN

    def to_python(self, value):
        return value

    def to_url(self, value):
        return str(value)
