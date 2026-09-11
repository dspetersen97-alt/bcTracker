# bcTracker

A practice-management platform for a Biblical Counseling ministry: secure
document exchange between counselors and counselees, counselor-configured office
hours with counselee self-booking, internal messaging, and session-based
invoicing — behind an access model designed so that a counselor cannot reach a
counselee who is not theirs.

Self-hosted by design. Counseling records stay on hardware the ministry controls.

## Status

**v1 is complete.** All seven steps are in place:

1. Foundation — project structure, Postgres, scoping layer, append-only audit
   trail, admin lockdown, Docker stack, CI.
2. Accounts — the four roles, staff TOTP MFA, counselee password *and* magic
   link, invitations, idle session timeout, and the access-matrix suite.
3. Counseling core — counselor and counselee profiles, `Case`/`CaseMember`,
   admin creation flows, role dashboards.
4. Documents — envelope encryption, both upload directions, ClamAV scanning,
   EXIF stripping, encrypted thumbnails, audited downloads.
5. Scheduling — office hours, slot generation, counselee self-booking with a
   Postgres exclusion constraint against double-booking, cancel and reschedule,
   reminder emails.
6. Google Workspace — appointment push, free/busy, staff sign-in. Optional, off
   unless configured; see below.
7. Hardening — security headers and CSP, `django-axes` login throttling, the
   retention purge, verified encrypted backups with a written restore drill, and
   `tests/test_role_matrix_attacks.py`, which attacks the access model rather
   than describing it.

**v2 is complete.** Messaging is built and usable: a conversation belongs to a
case, its audience is the counselor and **exactly one** counselee, and nothing in
it can be edited or deleted. Two counselees on the same case correspond with their
counselor separately and see nothing of each other's threads — there is no
"shared" setting on a thread at all, which is stricter than documents, where a
counselor may deliberately share a worksheet with the whole case. Notification
emails say only that something is waiting; no subject, no case, no name.

Files can be attached to a message, up to five at a time. They go through the same
acceptance pipeline as a document — size cap, magic-byte check, ClamAV, EXIF strip
— and the same envelope encryption, but they are **not** `Document` rows, on
purpose: a document is either private to its uploader and the counselor or shared
with the whole case, and neither of those describes "the two people in this
conversation". So an attachment lives on the message and derives who may open it
from the thread, with nothing of its own to configure. There is no preview and no
inline rendering; every download is an audited attachment. See the docstrings in
`apps/messaging/models.py` and `apps/documents/ingest.py`.

**v3 is complete.** Counseling sessions, invoices, and payments:

- A **session record** is the billable fact, created either by closing out a
  booking (completed, missed, or cancelled too late to fill the slot) or by hand
  for one that never went through the diary. The applicable rate is **snapshotted
  onto it** — raising the fee schedule next year does not silently reprice last
  year's work.
- A **fee schedule** of dated rates per charge kind, optionally overridden per
  counselor. No rate configured is not the same as a rate of zero: the first is a
  gap the billing page lists as work to do, the second is a decision, and a
  session recorded under either is recorded rather than lost.
- **Invoices** are drafted, issued, and then immutable. Money never changes by
  editing: a mistaken bill is voided, an uncollectable one is written off, and a
  bounced check or refund is a *negative payment row* pointing at the one it
  reverses. Amounts are integer cents throughout.
- **Payment by card** through a Stripe-hosted Checkout page, with cash and checks
  recorded by hand. Card details never touch this application, and Stripe is
  entirely optional — see below.
- `financial_admin` finally has the job the role was drawn for, and still cannot
  reach a document, a message, or a session note. `tests/test_billing_access.py`
  asserts that from both directions.

Four deliberate departures from the original plan, each narrowing rather than
widening:

1. Staff Google sign-in is built directly on Google's OIDC endpoints instead of
   `django-allauth` — one provider, and allauth brings its own account model,
   templates, and signup flows that would have to be switched off.
2. Workspace 2FA is **not** accepted in place of TOTP; Google's response says
   nothing reliable about whether that sign-in used a second factor.
3. Stripe is called with plain `requests` over four endpoints rather than through
   the `stripe` SDK, and the webhook signature is verified by hand
   (`apps/billing/stripe/webhook.py`) rather than by `Webhook.construct_event`.
   The SDK is a large dependency with its own release cadence, carrying an API
   surface this application will never use; what is actually needed is a customer,
   a Checkout Session, a session read-back, and an HMAC comparison. The
   verification is ~40 lines and reads the same header format the SDK does,
   including multiple `v1=` signatures during a secret rotation, with a
   `compare_digest` on every candidate. It is covered by
   `tests/test_stripe_webhook.py` far more thoroughly than a call into a library
   could be.
4. Payment goes through a **Checkout Session** rather than a Stripe hosted
   invoice. A hosted invoice would mean Stripe holding its own copy of the line
   items — which say what the money was for, and therefore that somebody is in
   counseling — plus a second invoice number and a second set of dunning emails
   that this ministry has deliberately decided not to send. A Checkout Session
   carries one line, `STRIPE_PAYMENT_LABEL`, and an amount; the invoice itself
   stays here.

**v4 is complete.** The round of work that came from actually using the first
three — navigation, and the four or five places a counselor or an administrator
had to leave the application to get something done:

- **A home page and a collapsible sidebar.** Every role lands somewhere that says
  what they can do, and the same list of ways in is on every page.
  `apps/core/navigation.py` is the single source of it, so a feature is offered to
  a role in one place rather than in each template. The toggle is a checkbox and a
  label — CSP has no `unsafe-inline` and there is no JS build step, so a script
  would be the one thing that could not ship. The header holds the brand and the
  signed-in address, and nothing that navigates.
- **Accounts are created in the application.** *Add a person* creates any of the
  four roles and emails the invitation in one step; `manage.py invite_staff`
  remains for the bootstrap, when there is nobody to sign in as. Assigning a role
  is still the one thing that decides who may read a counselee's file, so the page
  belongs to `admin` alone and its refusals are audited.
- **Mail is configured from the web UI, and the password is sealed.** The settings
  live in a singleton row, the password under the same envelope encryption as
  documents — a database dump has the host and username and not the secret — and
  the environment is only a fallback. There is a *send a test message* button that
  reports the provider's own words, and incomplete mail is an `Info` deploy check
  rather than a 500 on the first counselee somebody creates. That 500 is what
  prompted all of this: an install passed every check and then failed on the one
  page it was built to do first. Any SMTP provider will do — Google Workspace and
  Zoho are both documented on the page itself, including Zoho's port 465. The
  encryption is one choice rather than two switches, so the pair Django refuses
  outright, and the pair that sends a mailbox password in the clear, are both
  unreachable from the form and from the table.
- **Documents can be read without downloading them.** Narrowly: PDF, JPEG, PNG and
  plain text, and only when the stored content type *and* the first decrypted
  frame agree. Served `Content-Disposition: inline` with `nosniff` and audited
  exactly like a download, and shown on the document's own page rather than in a new
  tab. That is the one thing `frame-src`/`frame-ancestors` are `'self'` for; they
  name no other origin, and `object-src` stays `'none'`, so nothing is ever handed
  to a plugin and nothing framed is anything but a type on that allowlist.
- **A document can be filed against an appointment.** Homework is handed in *for* a
  session, so `Document.booking` records which one, offered from the diary and from
  the session page. The link is a label and never a route: the appointment is
  resolved through `Booking.objects.for_actor` narrowed to the case being uploaded
  to, and a stale id loses the link rather than the upload.
- **A counselee's file on one page.** Their cases, the next appointment, past
  sessions, everything sent in, and the notes the viewer may read — which
  previously meant three tabs. It gathers, so what it gathers is per membership the
  *viewer* holds: another counselor's case for the same person is not on it, a
  spouse's private upload is not filed under them, and `financial_admin` cannot
  open it at all.

## Roles

| Role | Can see |
| --- | --- |
| `admin` | Every case, counselor, and counselee; creates accounts of any role, and configures the ministry's mail |
| `counselor` | Only cases assigned to them — nothing about anyone else's counselees |
| `financial_admin` | Who is assigned to whom, and every fee, invoice, and payment. **Never documents, messages, or what happened in a session** |
| `counselee` | Their own scheduling and their own uploads |

A **superuser** is separate from all four: a break-glass Django admin account,
not a role anyone uses day to day.

**`admin` and messages: read-only, and worth a second look.** An administrator can
read a conversation — every read is audited — but cannot start one, reply, close,
or reopen it. That follows the line already drawn for session notes, and it is the
one page in the application where reading and writing part company. If the ministry
would rather an administrator not read counselee correspondence at all, the change
is a single predicate in `apps/messaging/rules.py`; `tests/test_messaging_access.py`
asserts both halves as they stand today.

## Local development

Requires Python 3.13 (matching the container) and Docker for Postgres.
Python 3.14 also works for local development.

```bash
make install                 # create .venv, install dev dependencies
cp .env.example .env
make keygen                  # generates the two secrets; paste into .env
make dev-db                  # start Postgres only
make migrate
make superuser
make run                     # http://localhost:8000
```

Run the checks CI runs:

```bash
make check                   # lint, format, missing migrations, tests
```

Tests require a running Postgres. There is deliberately no SQLite mode: the
schema depends on range types and `btree_gist` exclusion constraints that SQLite
cannot emulate, so a passing SQLite suite would be testing something other than
what ships.

## Deployment

**For a first install, follow [`docs/deployment.md`](docs/deployment.md)** — the
ordered runbook, from datasets and DNS through the first accounts, the scheduled
jobs, and upgrades. What follows here is the shape of the thing; that document is
the sequence to do it in.

```bash
sudo sh scripts/bootstrap.sh --host counseling.example.org --admin you@example.org
```

That generates the three secrets, writes `.env` from `.env.example`, builds, starts,
waits for the application to report healthy, and creates the first administrator.
It will not invent a hostname, and it writes **no mail password at all**: the
sending mailbox is finished in the application, under *Email settings*, where the
password is sealed with the master key rather than left in a file. Until that is
done nothing is emailed and nothing breaks either — that administrator's
invitation link is printed instead. `--help` lists the rest; `--print-config`
shows what it would write.

By hand, which is the same thing more slowly:

```bash
cp .env.example .env         # fill in every value; set SITE_HOSTNAME
make docker-build
make docker-up
```

Caddy obtains and renews TLS certificates automatically, which requires the host
to be reachable from the internet on ports 80 and 443. For a LAN-only install,
see the note at the top of `compose/caddy/Caddyfile`.

Only Caddy publishes a port. The application, database, and virus scanner are
reachable only on the internal compose network — that is what makes trusting
Caddy's `X-Forwarded-*` headers safe.

Mount the `documents` volume on an **encrypted TrueNAS dataset**.

### The master key

`BCTRACKER_MASTER_KEY` decrypts every stored document.

**If it is lost, every document becomes permanently unreadable — including in
every backup.** Back it up sealed and offline, stored separately from the
database and document backups. Storing it next to the ciphertext defeats the
encryption; losing it destroys the archive. Decide who holds it before anyone
uploads a real document.

### Google Workspace (optional)

Both integrations are off unless configured, and the application does not reach
for the network at all while they are. Each needs one OAuth client of type "Web
application" in the Google Cloud console; the redirect URIs must be registered
character for character, and are built from `SITE_BASE_URL`:

| Setting | What it turns on | Redirect URI to register |
| --- | --- | --- |
| `GOOGLE_CALENDAR_ENABLED` | Each counselor may opt in to having their appointments pushed to their own Google calendar, and to not being offered times they are busy elsewhere | `/availability/google/callback/` |
| `GOOGLE_SSO_ENABLED` | Ministry staff may sign in with their Workspace account instead of a password here | `/login/google/callback/` |

`GOOGLE_WORKSPACE_DOMAIN` is load-bearing for both and is checked against the
hosted-domain claim Google returns, not against the email address — a Workspace
domain can have aliases, and matching on the address would accept a lookalike
domain that merely ends the right way. Without it, a counselor could connect a
personal account and have ministry appointments written to a calendar the
ministry cannot audit; staff sign-in refuses to run at all. Deploy checks
(`accounts.E001`–`E002`, `scheduling.E001`–`E003`) stop a half-configured
container from starting.

What is deliberately *not* built:

- **Nothing about a counselee leaves this system.** A pushed event carries the
  time and `GOOGLE_EVENT_TITLE`, no attendees, and no name unless the counselor
  has explicitly switched names on. A Google calendar is outside the four-role
  access model, and anything on it is readable on a lock screen.
- **Google is never authoritative.** Sync is one-way. If a counselor deletes the
  event in Google, the appointment still stands and is re-pushed.
- **Signing in with Google does not satisfy the second factor.** Staff still
  enter a TOTP code afterwards; Google's response says nothing reliable about
  whether *that* sign-in used one.
- **Signing in never creates an account or grants a role.** An address matching
  no existing staff row is refused.

Refresh tokens are sealed with the same envelope encryption as documents, so
`BCTRACKER_MASTER_KEY` custody covers them too.

### Stripe (optional)

Off unless configured, like the calendar, and nothing reaches for the network
while it is. With `STRIPE_ENABLED` unset there is no card button, the webhook
refuses every request, and `reconcile_stripe` exits saying so — invoices still
work end to end, paid by cash or check and recorded by hand.

Switching it on needs two secrets that are easy to confuse:

| Setting | Where it comes from | What it does |
| --- | --- | --- |
| `STRIPE_SECRET_KEY` | Dashboard → API keys (`sk_live_…`) | Authorises the four outbound calls. Never the publishable key; there is no client-side Stripe here |
| `STRIPE_WEBHOOK_SECRET` | Shown once when the endpoint is created (`whsec_…`) | Proves a delivery came from Stripe |

Register the endpoint at `/billing/stripe/webhook/` under `SITE_BASE_URL`, for the
events `checkout.session.completed` and `payment_intent.payment_failed`.

**That endpoint is the only unauthenticated route in this application**, and its
whole job is to change a balance. With no signing secret it would be a "mark this
invoice paid" URL open to the internet, so `webhook.py` refuses everything when the
secret is empty and deploy check `billing.E004` stops a container starting that
way. Every refusal is recorded and answered with a bare 400: a response that
distinguished a bad signature from a stale timestamp would help whoever is probing
build a better attempt.

The webhook is the primary path and **not** the only one. `reconcile_stripe` runs
nightly and asks the question from the other side — for every invoice that still
shows a balance and once sent somebody to Stripe, does Stripe think it was paid? A
delivery can be missed for reasons entirely outside this application (a restart
during the retry window, a rolled secret, an hour of TLS failure), and every one of
them ends with a counselee who has paid being reminded that they owe money. Both
paths post through the same idempotent service keyed on the PaymentIntent, so
neither can double-charge the ledger, whichever arrives first.

```bash
python manage.py reconcile_stripe --dry-run      # report differences, change nothing
python manage.py send_invoice_reminders --dry-run
```

Reconciliation also reports two things it will not fix, because they need a
person: webhook deliveries that verified and then failed to match an invoice
(money in Stripe's account the ministry's books know nothing about), and invoices
with more money against them than they asked for (usually a card payment landing
the same day as a check — harmless, and somebody owes a refund).

### Backups

`backup_database` runs nightly in the cron sidecar and writes two files to the
`backups` volume: a `pg_dump --format=custom` archive encrypted with the same
envelope scheme as documents, and a JSON manifest holding the wrapped key, sizes,
a sha256, the migration heads, and a count of the document store.

Every backup is **verified, not merely written**: the new file is decrypted end to
end and then read by `pg_restore --list`, which is what catches an archive that
exited zero over the wrong or an empty database. A backup that fails verification
is renamed `.unverified` and nothing is pruned, so the last known-good copy is
never deleted to make room for a bad one. A weekly cron tick re-verifies the
newest backup, because rot happens after the write.

```bash
python manage.py backup_database                 # what cron runs
python manage.py decrypt_backup --list
python manage.py decrypt_backup                  # verify the newest
python manage.py decrypt_backup --output /tmp/restore.dump
```

Restoring is deliberately not automated — `pg_restore` into a live database is
irreversible and the right target differs between a drill and an emergency. The
last step belongs to a person following **`docs/restore-drill.md`**, which also
covers the quarterly rehearsal and the off-box copy.

Getting the volume **off this host** is a separate step and is not optional:
encryption at rest protects against a stolen drive, not a failed pool. And note
that the backups are sealed with `BCTRACKER_MASTER_KEY` — the manifest records a
fingerprint of the key that wrote it, so a restore can tell whether the key on
hand is the right one, but it cannot recover a key that is gone.

## Architecture notes

**Authorization.** `apps/core/scoping.py` defines `for_actor(user)`, and views
resolve objects only through it. It is fail-closed: an unknown role, an inactive
user, or a role that has not been explicitly granted access all get `none()`
rather than everything. Unauthorized objects return 404 rather than 403, so the
existence of another counselee's record is not disclosed.

The real guarantee is the test suite, not the code — `tests/` grows an access
matrix over (role × object × view), including a meta-test that fails if a route
is added without an entry. Postgres row-level security was considered and
rejected: it would restate the same logic in a second language, with migrations
bypassing it anyway.

**The Django admin is the main threat to that model**, because it queries the
default manager directly and bypasses scoping entirely. So admin access requires
`is_superuser`, no product role can reach it, and `Document` and `Message` are
never registered there. `tests/test_admin_lockdown.py` fails if that changes.

**The audit trail** (`apps/audit`) is append-only, enforced by a database trigger
rather than by revoking privileges — the application connects as the role that
owns the schema, and a table owner keeps full privileges on its own tables, so a
`REVOKE` would look like protection while providing none. Document access uses
`record_or_raise`: if the access cannot be recorded, the file is not served.

**Soft delete** is the default for counseling records, including for bulk
`.delete()`. Accidental loss of counseling history is unrecoverable and worse
than clutter, and a future retention policy needs the deletion timestamp.

**Money is integer cents, everywhere** — no float ever touches an amount, and
there is no `Decimal` at the boundary either. `apps/billing/money.py` owns the one
formatting function, so an amount is rendered in exactly one place; a total is
recomputed from its lines by a single service rather than incremented as they are
added, and database check constraints hold the arithmetic
(`line_total_is_quantity_times_unit`, `a_reversal_is_negative`,
`invoice_is_not_due_before_it_is_issued`). Stripe is denominated the same way,
which removes the last conversion.

**Nothing that says what the money was for leaves the billing app.** An invoice
line describes a *kind* of session and a date, never a note or a topic; the emails
carry the number, the amount, and the due date and nothing else, because a bill read
over somebody's shoulder should say only that they owe a ministry money; and the
Stripe Checkout page and card statement carry `STRIPE_PAYMENT_LABEL`. `StripeEvent`
deliberately does **not** store webhook payloads — that would be the one copy of
billing data in this database no access rule governs.

**Two clocks, deliberately.** Timestamps are stored in UTC and rendered in each
viewer's own timezone — scheduling depends on that. Stored *dates*, though — the
day a case opened, a membership ended, a window came into force — come from the
ministry's timezone via `apps/core/dates.py`, never from the acting user's.
Mixing the two is how a case gets closed the day before it opened; see
`tests/test_business_dates.py`.

## Layout

```
config/settings/     base, dev, prod, test — secrets from the environment
apps/core/           base models, scoping layer, admin hardening, request ids
apps/accounts/       custom user model, the four roles, login flows, MFA, staff SSO
apps/counseling/     cases and profiles — the source of truth for who may see whom
apps/documents/      envelope encryption, scanning, upload and download
apps/scheduling/     office hours, slots, bookings, google/ integration
apps/messaging/      case conversations — one counselor, one counselee, no edits
apps/billing/        sessions, fee schedule, invoices, payments, stripe/
apps/audit/          append-only accountability trail
compose/             Caddyfile, container entrypoints, cron schedule
scripts/             bootstrap.sh — first-install configuration and startup
tests/               pytest suite; testapp/ holds test-only concrete models
```

Every app's queryset scoping resolves through `apps/counseling`, so there is one
place to audit the answer to "may this actor go near this counselee".
