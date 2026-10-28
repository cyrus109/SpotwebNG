# Deploying and upgrading

Your retrieved index is the expensive part (days of Usenet reading). Everything is designed so
that **an upgrade never costs a re-download**.

## Layout

| What | Where | Survives upgrade? |
|---|---|---|
| SQLite database, covers, NZB cache, backups, secret key | the `/data` volume (`spotwebng-data`) | yes |
| Application code | the image | replaced |

## Update a server (seconds)

CI builds and publishes `ghcr.io/cyrus109/spotwebng` (`latest`, `X.Y.Z`, commit sha) on every
push to `main`. On the server:

```bash
docker compose pull
docker compose up -d
```

No build, nothing re-fetched from Usenet. If you would rather build on the server:
`git pull && docker compose up -d --build`. (If the GHCR package is private, `docker login ghcr.io`
first.)

## What happens at start-up

1. **Schema upgrade (Alembic).** Missing tables/columns are added; existing data is never
   recreated. If the schema revision is about to change, the SQLite file is first copied to
   `/data/backups/pre-migrate-<from>-to-<to>-<time>.db` (last 5 kept).
2. **Derived-data jobs** run in the background (search index, flags, …) over rows you already
   have. Progress shows under *Settings → Status & backup*. New features that need extra data
   ship as jobs, not as "retrieve everything again".
3. **Automatic backups** (*Settings → Automatic backups*, default 3 daily) write a full export
   to `/data/backups`.

## Big databases on a slow disk

SQLite is tuned for a multi-gigabyte index: a 16 MiB page cache per connection, memory-mapped reads,
temp tables in RAM, and a 128 MiB cap on the write-ahead log (`journal_size_limit`). Background
housekeeping checkpoints the log after imports, backups, jobs and every retrieval run, and a watchdog
checks it every five minutes (passive above 256 MB, truncating above 1 GB). The *Storage* line in
Status & backup shows the log size when it is over 50 MB.

Upgrading from a release before the `(enriched, posted_at)` index (migration 0008) takes a few minutes
on a large database the first time: the app copies the database aside (`pre-migrate-*`, the newest two
are kept, and it is skipped if the disk is too full), builds the index, and only then starts serving.
If the old write-ahead log is already huge, you can empty it once, offline, while the container is
stopped:

```bash
docker compose stop spotwebng
docker run --rm -v spotwebng-data:/data python:3.11-slim python -c \
  "import sqlite3; c=sqlite3.connect('/data/spotwebng.db'); print(c.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())"
docker compose start spotwebng
```

Counting the sidebar and filter numbers scans every spot, which takes minutes on a cold slow disk, so
a page view never does it. The counts are kept in memory and in `/data/facets.json`, recounted in the
background about 30 s after a retrieval that added spots (or a prune, import or spotter block), and
on demand through *Refresh the sidebar counts*. Until then the numbers stay as they were; the
*Storage* line in Status & backup shows when they were last updated. After the first start on a new
version they appear once the initial recount finishes (a few minutes on a big database).

## The secret key

Stored credentials (Usenet password, SABnzbd / Radarr / Sonarr keys, notification tokens) are
encrypted with a key kept **outside the database**: `SPOTWEBNG_SECRET_KEY` if set (generate one
with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`),
otherwise `/data/secret.key`, created on first start. A copied database or backup alone does not
expose the credentials. **Keep a copy of the key**: if it is lost the credentials read as empty
and must be entered again (your spots are unaffected). Existing plaintext credentials are
encrypted automatically at the first start of this version.

## Rolling back

```bash
docker compose down
# restore the pre-migration copy
docker run --rm -v spotwebng-data:/data alpine sh -c \
  'cp /data/backups/pre-migrate-….db /data/spotwebng.db && rm -f /data/spotwebng.db-wal /data/spotwebng.db-shm'
# pin the previous image in docker-compose.yml, e.g. ghcr.io/cyrus109/spotwebng:0.2.0
docker compose up -d
```

## Moving to a new server

Either copy the volume, or *Export database* on the old one and *Import* on the new one
(spots, comments, download history and retrieval position — no Usenet re-read).

## Rules for contributors (keeps upgrades cheap)

- **Additive first.** New nullable columns / new tables in an Alembic revision
  (`app/migrations/versions`, use the helpers in `app/migrations/helpers.py`); drop or rename
  only one release after nothing reads the old thing.
- **Derive, don't re-fetch.** Data computable from stored rows → a job in `app/jobs.py`
  (versioned, idempotent, resumable). Data only Usenet has → store it at ingest from now on;
  re-scan fills older rows in place.
- Export/import carries every new table; importers ignore unknown columns.
