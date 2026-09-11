# Backup and restore

A backup nobody has restored is a guess. This is the runbook for both halves:
what the system does on its own, and what a person has to do — including the
rehearsal, which is the only part that proves the rest works.

## What runs on its own

| When | Command | What it does |
| --- | --- | --- |
| 02:15 daily | `backup_database` | `pg_dump` streamed straight into envelope encryption, written to `BACKUP_ROOT`, read back and verified, then backups past `BACKUP_KEEP_DAYS` are deleted |
| 03:15 Sundays | `decrypt_backup` | Reads the newest backup back again, to catch a file that has decayed since it was written |
| every 30 min | `purge_expired_tokens` | Deletes expired login links, expired session rows, and stale login-failure records |

Each nightly run leaves two files in `BACKUP_ROOT`:

```
bctracker-20260910T021500Z.dump.enc        the encrypted custom-format dump
bctracker-20260910T021500Z.dump.enc.json   the manifest: wrapped key, digest, sizes
```

The manifest holds the data key for that dump, **wrapped with
`BCTRACKER_MASTER_KEY`** — it is not a secret on its own, and it is useless
without the master key. Keep the pair together; the dump cannot be read without
its manifest.

A backup that fails verification is renamed to `…​.dump.enc.unverified` so nothing
mistakes it for one that can be restored, and both the failure and every
successful run are recorded in the audit trail (`backup.created`,
`backup.failed`).

## What a person has to do

### 1. Get the backups off this host — mandatory

`BACKUP_ROOT` lives on the same pool as the database. It survives a mistake, a
bad migration, or a dropped table. It does not survive the host, the pool, or the
building. Replicate the directory somewhere else on a schedule; on TrueNAS SCALE
a periodic snapshot task plus a replication task to a remote target is the
straightforward option.

The files are already encrypted, so the destination does not have to be trusted
with the contents — only with keeping them.

### 2. Keep the master key somewhere the backups are not

`BCTRACKER_MASTER_KEY` decrypts every document **and** every database backup.
Lose it and the backups are noise. Store it beside the backups and a single
compromise reads everything.

Sealed and offline, in a different place from the ciphertext. Write down who
holds it. Test that they can be reached.

### 3. Rehearse the restore — quarterly, and after any change to this stack

The drill goes into a scratch database. Nothing below touches the live one.

```bash
# 1. Pick a backup and look at it. This also verifies it end to end: every frame
#    is authenticated, and the archive's table of contents is checked for this
#    application's tables.
docker compose exec cron python manage.py decrypt_backup --list
docker compose exec cron python manage.py decrypt_backup

# 2. Confirm the master key in this environment is the one that sealed it. The
#    fingerprint printed by the command above must match; if it does not, the
#    command refuses rather than producing garbage.

# 3. Decrypt it to a plaintext dump. This file is the entire database in the
#    clear — put it somewhere temporary, and delete it in step 6.
docker compose exec cron python manage.py decrypt_backup --output /tmp/drill.dump

# 4. Restore into a scratch database.
docker compose exec db createdb -U bctracker drill
docker compose exec db pg_restore --no-owner --no-privileges \
    --dbname drill /tmp/drill.dump     # copy the file into the db container first

# 5. Check it is really there. Counts, not vibes.
docker compose exec db psql -U bctracker -d drill -c \
    'select count(*) from accounts_user; select count(*) from counseling_case;
     select count(*) from documents_document; select max(created_at) from audit_auditevent;'

# 6. Clean up.
rm /tmp/drill.dump
docker compose exec db dropdb -U bctracker drill
```

Then the part that is easy to skip and is the whole point:

### 4. Prove a document still opens

The database and the document store are two backups, and a restore that recovers
one without the other is not a recovery. The manifest records how many files the
document store held at the time of the dump:

```bash
docker compose exec cron python manage.py decrypt_backup   # "documents: N file(s)"
```

Compare that with what the restored document volume actually contains. Then sign
in as a counselor on the restored copy and download a document. If it opens, the
master key, the database, and the file store all agree — which is the only
statement a drill can make that is worth anything.

### 5. Write down when you did it

Date, which backup, how long it took, and what went wrong. The third number is
the one that matters during an outage, and nobody can estimate it from memory.

## If a restore is real, not a drill

1. Stop the web service first: `docker compose stop web caddy`. A half-restored
   database serving counselees is worse than an outage.
2. Restore into a **new** database and point the app at it, rather than restoring
   over the live one. If the restore turns out to be incomplete you still have
   what you started with.
3. Restore the document volume from the same date. A database newer than the file
   store shows documents that cannot be downloaded; a file store newer than the
   database has files nothing refers to.
4. Run `python manage.py migrate` afterwards and check the output. The manifest
   records the migrations that were applied when the dump was taken; if the
   running code is newer, this is the step that reconciles them.
5. Expect everybody to be signed out. Session rows are in the dump, but the
   secret key and cookie state may not line up; ask staff to sign in again and to
   have their TOTP app to hand.
