# Deploying bcTracker

The first install, in order, on a single host the ministry controls. Written to be
followed rather than read: every command here is one you actually run, and the
steps are sequenced so that nothing has to be undone.

Two of these steps are irreversible if skipped, and both are about the master key
and the backups. They are steps 4 and 11, and neither is optional.

For restoring — drills and emergencies — see **[restore-drill.md](restore-drill.md)**.
For what each setting means, `.env.example` is the reference; this document is the
order to do things in.

---

## What you need first

| | |
| --- | --- |
| A host | TrueNAS SCALE, or any Linux host with Docker Engine and Compose v2. 4 GB RAM is comfortable; ClamAV is the hungry part |
| A hostname | An A record pointing at the host. Automatic TLS needs ports 80 and 443 reachable **from the internet**; a LAN-only variant is in step 5 |
| An encrypted dataset | For counseling documents and backups. Step 2 |
| A mailbox | Google Workspace, with 2FA on the sending account and an **App Password** — not the account password |
| A decision | Who holds the master key, and where. Step 4 |

Optional, and better added after the base stack is known good: Google Calendar
and Workspace sign-in, and Stripe. Both are off unless configured, and step 12
turns them on.

---

## The scripted version

`scripts/bootstrap.sh` does the mechanical part of steps 2, 3, 6, and 9 — it
generates the three secrets, writes `.env` from `.env.example`, optionally points
the document and backup volumes at an encrypted dataset, builds, starts, waits for
the application to report healthy, and creates the first administrator:

```bash
git clone https://github.com/dspetersen97-alt/bcTracker.git /mnt/apps/bctracker
cd /mnt/apps/bctracker
sudo sh scripts/bootstrap.sh \
    --host counseling.example.org \
    --email counseling@example.org \
    --data-dir /mnt/tank/bctracker \
    --admin you@example.org
```

`sh scripts/bootstrap.sh --help` lists the rest. Two flags worth knowing now:
`--internal-tls` for a LAN-only install (certificates from Caddy's own CA, and it
defaults the hostname to `localhost`), and `--print-config`, which shows the `.env`
it would write without writing anything.

What it will **not** decide for you, and why:

| | |
| --- | --- |
| The hostname | Every link in every invitation and reminder is built from it. A wrong one does not fail; it sends counselees somewhere the ministry does not control. So `--host` is required |
| Mail credentials | Left **empty** rather than filled with a plausible address. Empty means the first send raises an error somebody notices |
| Google, Stripe | Left off. Each refuses to start half-configured, so there is nothing to defer |
| The master key | Generated, but its custody is step 4 and no script can do that part |

It refuses to overwrite an existing `.env`, because the master key inside one is
usually the only copy of it.

**The script is a head start, not a substitute for this document.** It cannot seal
the master key (step 4), cannot prove mail actually sends (step 7), and cannot get
backups off the host or rehearse a restore (step 11) — and those are the three
steps whose absence is invisible until the day it matters. It prints that list when
it finishes. The rest of this document is the long form, and is what to read when
something does not work.

---

## 1. Get the code onto the host

```bash
git clone https://github.com/dspetersen97-alt/bcTracker.git /mnt/apps/bctracker
cd /mnt/apps/bctracker
```

Everything below runs from that directory. Note what is **not** in the clone and
never will be: `.env`, the master key, and any counselee data. The repository is
public; the deployment's secrets live only on this host.

---

## 2. Decide where the data actually lands

Compose declares six named volumes: `pgdata`, `documents`, `backups`, `clamav_db`,
`caddy_data`, `caddy_config`. Left alone, Docker puts them all under
`/var/lib/docker/volumes`, which works and is the wrong place for two of them.

**`documents` and `backups` belong on an encrypted dataset.** Create one in the
TrueNAS UI (Datasets → Add Dataset → Encryption), then point those two volumes at
it with an override file Compose picks up automatically — `bootstrap.sh
--data-dir /mnt/tank/bctracker` writes exactly this, including the ownership below:

```yaml
# docker-compose.override.yml — not in git; it describes this host only.
services:
  web:
    volumes:
      - /mnt/tank/bctracker/documents:/var/lib/bctracker/documents
  cron:
    volumes:
      - /mnt/tank/bctracker/documents:/var/lib/bctracker/documents
      - /mnt/tank/bctracker/backups:/var/lib/bctracker/backups
```

Then, on the host — and this part matters:

```bash
mkdir -p /mnt/tank/bctracker/documents /mnt/tank/bctracker/backups
chown -R 1000:1000 /mnt/tank/bctracker
chmod 700 /mnt/tank/bctracker/documents /mnt/tank/bctracker/backups
```

The containers run as uid 1000 (`bctracker`), and the image pre-creates those
paths so that a *named* volume inherits their ownership. A **bind mount does not**
— it arrives with whatever the host directory has, so without the `chown` above
the web process cannot write an upload and the nightly backup fails every night.

Encryption at rest protects against a stolen or decommissioned drive. It does
nothing for a running host, where the dataset is unlocked. That is what backups
off-box and step 11 are for.

---

## 3. Write `.env`

`scripts/bootstrap.sh` writes this file — see *The scripted version* above; the
rest of this step is what it does and the values it leaves for you. Read it either
way, because two of these are worth understanding before something goes wrong.

```bash
cp .env.example .env
chmod 600 .env
```

Generate the two secrets. `make keygen` does this if you have a local virtualenv;
on a bare server, use Python from the image you are about to build, or these:

```bash
python3 -c "import secrets; print('DJANGO_SECRET_KEY=' + secrets.token_urlsafe(64))"
python3 -c "import base64,os; print('BCTRACKER_MASTER_KEY=' + base64.b64encode(os.urandom(32)).decode())"
```

Then fill in every value. These are the ones with no safe default — the stack
refuses to start without them:

| Setting | Value |
| --- | --- |
| `DJANGO_SETTINGS_MODULE` | `config.settings.prod`. Compose sets this for the containers regardless; setting it here keeps host-side `manage.py` honest |
| `DJANGO_SECRET_KEY` | From the command above. Changing it later signs everybody out |
| `DJANGO_ALLOWED_HOSTS` | `counseling.example.org` — the public hostname only. The loopback address the container's own healthcheck uses is added by `config/settings/prod.py`, deliberately, so it is not yours to remember |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://counseling.example.org` — **with the scheme**, or every form post is rejected |
| `POSTGRES_PASSWORD` | Anything long and random. Only the containers ever see it |
| `BCTRACKER_MASTER_KEY` | From the command above. Read step 4 before you continue |
| `SITE_BASE_URL` | `https://counseling.example.org`. Every link in every email is built from this, never from a request |
| `SITE_HOSTNAME` | The same host without the scheme. Caddy requests a certificate for it |
| `ORG_TIME_ZONE` | The ministry's own zone. Office hours are interpreted here |
| `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | The Workspace mailbox and its **App Password** |

Leave `DOCUMENT_STORE_ROOT`, `BACKUP_ROOT`, `POSTGRES_HOST` and `CLAMAV_HOST` as
they are; Compose sets them per service and the values in `.env` are for
reference and for host-side commands.

Leave `SECURE_HSTS_SECONDS` at `3600` for now. Step 13 raises it.

---

## 4. Settle the master key — before anyone uploads anything

`BCTRACKER_MASTER_KEY` encrypts every document, every message attachment, every
Google refresh token, and every database backup.

- **Lose it and all of that is permanently unreadable**, backups included. There
  is no recovery, and no key-rotation command: treat this value as permanent.
- **Store it next to the ciphertext and the encryption has bought nothing.** A
  copy of the backups directory plus a copy of `.env` is the whole archive in the
  clear.

So: seal a copy offline — printed in an envelope in a safe, or in a password
manager the ministry controls — **somewhere the backups are not**. Write down who
holds it and check that they can be reached. Do it now, while the only thing the
key protects is an empty database.

Backup manifests record a *fingerprint* of the key that sealed them, so a restore
can tell you whether the key you have is the right one. It cannot tell you a key
you no longer have.

---

## 5. DNS and TLS, before the first start

Caddy asks Let's Encrypt for a certificate the moment it starts, and that
requires the hostname in `SITE_HOSTNAME` to resolve to this host and ports 80 and
443 to reach it. Confirm the record has propagated before step 6 — Caddy will
retry, but a failing ACME loop in the logs is a confusing first impression.

**LAN-only install.** Edit `compose/caddy/Caddyfile`: use the internal hostname
as the site address and add `tls internal`, which issues from Caddy's own CA.
Browsers will warn until that CA is trusted on each device. `SITE_BASE_URL` and
`DJANGO_ALLOWED_HOSTS` must match whatever name you chose.

---

## 6. Start it

```bash
make docker-build      # docker compose build
make docker-up         # docker compose up -d
make docker-logs       # docker compose logs -f
```

What should happen, in this order:

1. `db` starts and reports healthy (`pg_isready`).
2. `web` waits for Postgres, applies **all migrations**, then runs
   `manage.py check --deploy --fail-level WARNING`. A misconfiguration stops the
   container here rather than serving in a weak state — see the table in
   *Troubleshooting* for what each check id means.
3. `web` starts gunicorn and its healthcheck begins passing.
4. `caddy` starts — it waits for `web` to be healthy — and obtains a certificate.
5. `clamav` downloads signatures. **This takes several minutes**, and until it
   finishes, uploads are refused rather than accepted unscanned. Nothing else is
   affected; the site is usable meanwhile.
6. `cron` starts and prints the crontab it scheduled from.

```bash
docker compose ps
```

Expect `db`, `web`, `caddy` and `clamav` healthy, and `cron` running with no
health status at all — its healthcheck is disabled on purpose, because the image's
probe checks an HTTP port that service does not serve.

Then, from anywhere:

```bash
curl -sS https://counseling.example.org/healthz
# {"status": "ok"}
```

---

## 7. Prove email works, before it carries an invitation

Every account is created by emailing somebody a link. If SMTP is wrong, the
account exists, the link does not arrive, and there is nothing in the interface
that says so.

```bash
docker compose exec web python manage.py shell -c \
  "from django.core.mail import send_mail; \
   send_mail('bcTracker test', 'It works.', None, ['you@example.org'])"
```

If that fails: the password must be a Workspace **App Password**, the port is 587
with STARTTLS, and `DEFAULT_FROM_EMAIL` has to be an address the account is
allowed to send as. Workspace has no bounce webhook, so a rejected message is
invisible to this application — which is why this step is a step.

---

## 8. Create the break-glass superuser

```bash
docker compose exec web python manage.py createsuperuser
```

This is the Django admin credential, not a role anybody works in. It is the one
account that bypasses the scoping layer, so it belongs to one named person, with
a long unique password, used only when something is broken. Keep a note of who
holds it; `DJANGO_ADMIN_URL_PATH` moves the admin off `/admin/`.

At first sign-in it will require TOTP enrolment — staff accounts are sent to
`/mfa/setup/` and cannot go anywhere else until a device is confirmed. Have an
authenticator app to hand.

Nothing is registered in that admin: `Document`, `Message`, and attachments are
excluded by design, and the rest simply has a better interface. Do the real work
in the application.

---

## 9. Create the ministry's own accounts

**Staff accounts are created from the command line.** There is deliberately no
self-service signup, and — worth knowing before you look for it — no UI for
creating a counselor, a second administrator, or a `financial_admin` either. Only
counselees have a creation page, because creating one is part of a counselor's or
administrator's ordinary work. A staff role decides who may read a counselee's
file, so assigning one is not something a session should be able to do.

```bash
docker compose exec web python manage.py invite_staff \
    --email pastor@example.org --role counselor \
    --first-name Paul --last-name Miller
```

`--role` takes `admin`, `counselor`, or `financial_admin`, and is required: the
difference between them is who can reach counseling records, so it gets typed out.

What the command does, and what it will not:

- Creates the account with **no usable password** and emails a single-use link to
  set one. Good for seven days (`INVITATION_TTL_SECONDS`); re-run with
  `--reinvite` to issue another, which retires the first.
- **Prints the link instead of mailing it when no mailbox is configured** — this is
  how the first administrator gets in on a host where SMTP is not set up yet,
  because until that account exists there is nobody who could set it up. Force it
  with `--print-link`. A printed link has travelled through a person, and the
  audit trail records which of the two happened.
- Sets `mfa_required` from the role, so every staff account is sent to
  `/mfa/setup/` on first sign-in and cannot go anywhere else until a device is
  confirmed. Counselees are not.
- Refuses an address that already has an account unless `--reinvite`, and refuses
  even then if the role does not match: **this command does not promote anybody.**
  Changing what somebody can see should be its own deliberate act, not a side
  effect of re-sending an invitation.
- Refuses `--role counselee`. They are created inside the application, on a case.
- Does **not** grant `is_superuser`. The product's `admin` role has its own
  interface; superuser is the break-glass account from step 8, and a day-to-day
  administrator should not have it.

Then, in the application itself, as an `admin`:

1. **New counselee** creates the account and emails the invitation in one step.
2. **Open a case** assigns a counselor and adds the counselee(s) as members. Access
   to everything — documents, messages, appointments, invoices — derives from
   case membership, so until this exists nobody can see anything.
3. Each counselor sets their own office hours before counselees can book.

`manage.py seed_demo` exists for development and refuses to run unless `DEBUG` is
on. Do not go looking for a way around that: seeded accounts have a published
password.

---

## 10. Smoke-test the thing you just deployed

Worth twenty minutes now rather than a support conversation later. Wait until
ClamAV reports healthy before the upload steps.

- Sign in as a counselor, set office hours, and check the times shown match
  `ORG_TIME_ZONE`.
- Sign in as a counselee (a second browser profile) and book an appointment.
  Confirm the counselor sees it.
- Upload a document as the counselee; download it as the counselor. Then the
  reverse. A PDF and a photo — the photo has its EXIF stripped.
- Send a message each way, with an attachment.
- As a `financial_admin`, confirm the documents and messages pages are refused.
  That restriction is the point of the role.
- Check the audit trail shows the downloads.
- Confirm the appointment reminder email arrives (or run
  `send_appointment_reminders` by hand — step 11).

---

## 11. The scheduled jobs, and getting backups off this host

The `cron` sidecar runs seven jobs; times are **UTC**, not the ministry's zone
(`compose/cron/bctracker.cron` lists them, with the reasoning for each schedule).

First, confirm the jobs inherited this deployment's configuration. cron gives its
jobs a nearly empty environment, so the sidecar renders one — and a job that
silently read a default would write real backups somewhere nothing replicates:

```bash
docker compose logs cron | head
docker compose exec cron sh -c 'grep -c "^BACKUP_ROOT=" /etc/environment.cron'   # 1
docker compose exec cron sh -c 'grep -c "^SITE_BASE_URL=" /etc/environment.cron' # 1
```

Counting rather than printing on purpose: that file holds the master key and the
database password.

Any job can be run by hand, and the two that spend money or send mail take
`--dry-run`:

```bash
docker compose exec cron python manage.py send_appointment_reminders
docker compose exec cron python manage.py send_invoice_reminders --dry-run
docker compose exec cron python manage.py reconcile_stripe --dry-run
```

Then take the first backup yourself instead of waiting for 02:15 UTC:

```bash
make backup                                              # in the cron service
docker compose exec cron ls -l /var/lib/bctracker/backups
docker compose exec cron python manage.py decrypt_backup --list
```

You should see a `.dump.enc` and its `.dump.enc.json` manifest. A file named
`.unverified` means the backup was written but could not be read back, and
nothing is pruned while that is true.

**Now the mandatory part.** `BACKUP_ROOT` is on the same pool as the database, so
it survives a mistake and not a fire. Replicate it off this host on a schedule —
on TrueNAS, a periodic snapshot task plus replication to a remote target. The
files are already encrypted, so the destination has to be reliable, not trusted.

Then rehearse a restore, following **[restore-drill.md](restore-drill.md)**, while
the database still holds nothing real. A backup nobody has restored is a guess,
and the first rehearsal is the one that finds the surprises.

---

## 12. Optional: Google and Stripe

Add these once the base stack is known good, one at a time, so a failure has one
possible cause. Both read `.env` at startup, so after editing it:

```bash
docker compose up -d          # recreates what changed
```

**Google.** One OAuth client of type "Web application" in the Google Cloud
console, with the Calendar API enabled on the project. Register both redirect
URIs character for character, built from `SITE_BASE_URL`:

| Setting | Redirect URI to register |
| --- | --- |
| `GOOGLE_CALENDAR_ENABLED` | `https://…/availability/google/callback/` |
| `GOOGLE_SSO_ENABLED` | `https://…/login/google/callback/` |

`GOOGLE_WORKSPACE_DOMAIN` is required for either: it is checked against the
hosted-domain claim Google returns, which is what stops a counselor connecting a
personal account and writing ministry appointments to a calendar nobody can
audit. Half-configured, the container refuses to start (`accounts.E001`–`E002`,
`scheduling.E001`–`E003`). Then have one counselor use **Connect your Google
Calendar** on their availability page (`/availability/google/`) and check the next
appointment appears — titled from `GOOGLE_EVENT_TITLE`, with no counselee name on
it.

**Stripe.** Two secrets that are easy to confuse, both from the dashboard:
`STRIPE_SECRET_KEY` (`sk_live_…`) and `STRIPE_WEBHOOK_SECRET` (`whsec_…`, shown
once when you create the endpoint). Register the endpoint at

```
https://counseling.example.org/billing/stripe/webhook/
```

for the events `checkout.session.completed` and
`payment_intent.payment_failed`. That endpoint is the only unauthenticated route
in this application and its whole job is to change a balance, so with no signing
secret it would be a "mark this invoice paid" URL open to the internet — the
container refuses to start in that state (`billing.E004`). Test with a real
invoice for a small amount, then:

```bash
docker compose exec cron python manage.py reconcile_stripe --dry-run
```

Expect it to report nothing: the webhook should already have recorded the
payment, and reconciliation is the net underneath it.

---

## 13. Raise HSTS

Once the site has been reachable over HTTPS for a few days without trouble, set

```
SECURE_HSTS_SECONDS=31536000
```

in `.env` and `docker compose up -d`. Both Django and the Caddyfile read that one
variable. It is left low until now because the header tells browsers to refuse
plain HTTP for this hostname for that long, and it cannot be withdrawn from a
browser that has already cached it.

---

## Upgrading

Migrations run automatically on start and are not reversible, so the backup comes
first:

```bash
make backup                              # and check it verified
git pull
make docker-build
docker compose up -d
docker compose logs -f web               # migrations, then the deploy checks
```

Read that log. The entrypoint applies migrations and then runs
`check --deploy --fail-level WARNING`, so a new required setting shows up as a
container that will not start rather than as a broken page.

**Rolling back.** If the new version applied migrations, the previous image will
not run against the new schema. So: `git checkout` the previous commit, rebuild,
and restore the backup you took above per
[restore-drill.md](restore-drill.md#if-a-restore-is-real-not-a-drill). If no
migration ran, rebuilding the older commit is enough on its own.

---

## Troubleshooting

**`web` never becomes healthy, so `caddy` never starts.** Read
`docker compose logs web`. In order of likelihood: a deploy check failed (see the
table below), Postgres is not up, or a migration failed. `caddy` waits for `web`
to report healthy, so a silent site with a healthy database is almost always
this.

**Deploy checks.** Each is a refusal to serve in a state somebody would regret.

| Id | What it means |
| --- | --- |
| `documents.E001`–`E004` | `BCTRACKER_MASTER_KEY` missing, the published development key, not base64, or not 32 bytes |
| `documents.E005` | Virus scanning switched off — uploads would be stored unscanned |
| `documents.E006`, `backups.E001` | The document store or backups directory is inside a directory the web server serves |
| `backups.E002`–`E003` | `pg_dump` or `pg_restore` not on `PATH`. The image installs both; this fires when running outside it |
| `accounts.E001`–`E002` | Google sign-in on without an OAuth client, or without `GOOGLE_WORKSPACE_DOMAIN` |
| `scheduling.E001`–`E003` | Calendar sync on without a client, without a domain, or with `SITE_BASE_URL` still `localhost` |
| `billing.E001`–`E002` | `BILLING_DUE_DAYS` negative, or `BILLING_CURRENCY` not a three-letter code |
| `billing.E003`–`E005` | Stripe on without a secret key, **without a webhook secret**, or with a localhost `SITE_BASE_URL` |
| `billing.W001`, `W002` | A Stripe *test* key in production; a webhook tolerance far from Stripe's 300s |

**`exec /app/compose/web/entrypoint.sh: no such file or directory`** — and the file
is plainly there. The missing thing is the *interpreter*: a CRLF line ending makes
the shebang read `#!/bin/sh\r`. It happens when the image is built on a Windows
checkout, because `docker compose build` copies the working tree from disk and git
is not involved. `.gitattributes` pins these files to LF and the Dockerfile strips
carriage returns as well, so a current checkout cannot produce this; an older one
can. Confirm with `head -1 compose/web/entrypoint.sh | od -c`, then
`git add --renormalize . && git checkout -- .` and rebuild.

**`FATAL: password authentication failed for user "bctracker"`, on a stack that
worked yesterday.** The database volume is older than the password in `.env`.
Postgres reads `POSTGRES_PASSWORD` only when it first creates the cluster, so
regenerating `.env` — `scripts/bootstrap.sh --force`, or an edit — leaves a
password that cannot authenticate against the existing volume. Put the previous
`POSTGRES_PASSWORD` back, or `ALTER USER bctracker PASSWORD` inside the running
database to match. `docker compose down -v` also resolves it, by destroying the
database; only do that on an install with nothing in it yet.

**Certificate never issues.** `docker compose logs caddy`. The hostname must
resolve to this host from the public internet and ports 80 and 443 must reach it;
Let's Encrypt also rate-limits repeated failures for the same name. For LAN-only,
use `tls internal` (step 5).

**Every form post fails CSRF verification.** `DJANGO_CSRF_TRUSTED_ORIGINS` needs
the scheme: `https://counseling.example.org`, not the bare hostname.

**Uploads are refused.** ClamAV is still downloading signatures — check
`docker compose ps`. This is deliberate: an unscanned upload is not accepted in
its place.

**Emails do not arrive.** Step 7. Workspace reports nothing back to this
application, so test SMTP directly rather than inferring it from a missing
invitation.

**Stripe deliveries get a 400.** The signing secret does not match the endpoint,
or the delivery is older than `STRIPE_WEBHOOK_TOLERANCE_SECONDS`. The response is
deliberately bare — a reply that distinguished the two would help whoever is
probing — but every refusal is recorded; check the audit trail and the Stripe
dashboard's delivery log together.

**Scheduled work appears to do nothing.** `docker compose logs cron`. Times in
the crontab are UTC. If a job runs but behaves as though nothing is configured,
check that the setting reached cron (step 11).

---

## What this deployment deliberately does not do

- **Scale horizontally.** One host, one gunicorn, three workers. Fine for a
  ministry; not a design that survives being pointed at a larger practice.
- **Rotate the master key.** There is no command, and re-encrypting the archive
  would be a project. See step 4.
- **Purge anything.** Counseling records soft-delete and stay. The retention
  machinery is built and the policy decision is the ministry's to make; a
  disclosure of abuse in a document may carry a preservation obligation that
  conflicts with deleting on a schedule, which is worth legal input.
- **Hide counselee correspondence from an administrator.** The `admin` role can
  read a conversation — read-only, and every read audited. If the ministry would
  rather it could not, that is one predicate in `apps/messaging/rules.py`; see
  the note under Roles in the [README](../README.md).
