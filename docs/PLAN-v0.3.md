# Plan v0.3 — platform, performance and features

Goal: ship the nine milestones below **without ever forcing a user to re-read Usenet**.
Every milestone is one or more commits, each with tests, and `pytest` + `ruff` green.

## 0. The "never re-download" contract

Deploying a new version must cost seconds, and upgrading must never discard retrieved data.

1. **One data volume (`/data`)** holds the database, covers, NZB cache, backups and the secret
   key. Containers are disposable; the volume is the install.
2. **Prebuilt images.** CI builds and pushes `ghcr.io/cyrus109/spotwebng:{latest,vX.Y.Z,sha}` with
   layer caching (dependencies layer first). A server runs `docker compose pull && up -d`:
   no build on the server, nothing re-fetched from Usenet.
3. **Versioned schema (Alembic), applied automatically at start-up.** Additive-first
   ("expand, then contract"): new nullable columns/tables first, destructive cleanups only a
   release later. A **pre-migration backup** (SQLite file copy into `/data/backups`) is taken
   whenever the schema revision changes, so a bad release can be rolled back by restoring it
   and pinning the previous image tag.
4. **Derived-data jobs (`app/jobs.py`).** Anything computable from data we already hold
   (flags, normalised titles, search index, spotter ids) is a *versioned, idempotent,
   resumable background job* with progress in the Status card. A new feature that needs
   derived data ships a job — never a re-download.
5. **Capture raw material now.** Fields only Usenet can provide (signature, spotter key, key
   id) are stored from this release on, so later features (reputation, verification) can be
   computed locally. Re-scan fills them in place for older rows (the retriever already
   back-fills known rows without touching enrichment).
6. **Export format versioning.** Export/import is the escape hatch for server moves; the format
   carries a version, importers ignore unknown columns, and new tables join the export.
7. **Scheduled auto-backups** (nightly export to `/data/backups`, keep N).

## Milestones

### M1 — Platform
- Alembic baseline (idempotent: creates missing tables, adds missing columns), run at start-up
  from `init_db`; existing databases are stamped, never recreated. Test: migrated schema ==
  model metadata; upgrade from an "old" schema fixture.
- Pre-migration backup, `/healthz` reports app + schema version.
- `app/jobs.py` framework + `job_state` table; Status card shows running jobs.
- Scheduled backups (setting: keep N, daily) using the existing export stream.
- CI: `docker-publish` workflow (GHCR, buildx cache), `docs/DEPLOY.md`, compose uses the image.

### M2 — Secrets at rest
- Fernet key file `/data/secret.key` (0600, created on first run). Provider password, SABnzbd,
  Radarr/Sonarr keys and notification tokens stored as `enc:…`; transparent decrypt; startup
  job encrypts legacy plaintext. Backups/DB dumps no longer leak secrets without the key file.

### M3 — Raw capture, signature verification, spotter block/trust
(Format confirmed from Spotweb's source.)
- New nullable spot columns: `keyid`, `spotter_id`, `signature` (headersign), `verified`
  (0 unknown / 1 ok / 2 failed); table `spotters` (spotter_id, pubkey modulus, first/last seen,
  `status` neutral|trusted|blocked, note).
- Verification: signed string `title + header-minus-signature + poster` (raw latin-1 bytes),
  RSA PKCS#1 v1.5 + SHA-1, Spotnet base64 transform (`-s`→`/`, `-p`→`+`), trusted keys for
  keyid 2/3/4, self-signed (keyid 7) key from the From header + hashcash, user signature over
  `<message-id>`. `spotter_id` = crc32(modulus bytes) little-endian, base64 without `/+=`.
  Strict mode: hashcash **and** signature.
- Adds `cryptography` (pinned). Comments: same verification (`<msgid>` or
  `<msgid>`+body+`\r\n`+from) once bodies are loaded; badge replaces "unverified".
- Block/trust spotter (admin); blocked posters filtered everywhere (list, API, watchlists).

### M4 — Performance
- `is_adult` indexed column (job + set at ingest) replacing `LIKE` scans.
- `title_key` column (normalised title) via job; powers duplicate grouping (M9).
- Full-text search: SQLite FTS5 external-content table + triggers (job builds it; search falls
  back to LIKE until ready); Postgres `pg_trgm` GIN index.
- Cached facet counts and result totals (short TTL, invalidated after retrieval).
- Keyset paging for new/old sort, capped numbered pages otherwise.
- Parallel NNTP: `connections` setting (clamped 1–8) used by the history walk (concurrent
  XOVER windows, written in order so the floor stays monotonic) and by enrichment.
- Lazy enrichment: `enrich_older_than_days` (older spots enrich on view / send).

### M5 — Spot health check
- Assemble the NZB, `STAT` a sample of its real segment ids (≤30, first/last/random) →
  `health` (unknown/complete/partial/dead), `health_pct`, `health_checked_at` on spots.
- Spot page badge + "Check now"; Send checks first (setting: block dead, warn partial);
  watchlists use it before auto-sending.

### M6 — Watchlists, auto-send, notifications
- Tables `watches`, `watch_hits`. A watch = text query + tree filter + size range + action
  (notify / send). Evaluated after each retrieval for spots newer than the watch's cursor;
  skips downloaded, blocked, adult-hidden, dead spots. Watchlist page with live "matches now".
- Notifications: webhook, Discord, Telegram, ntfy (secrets encrypted). Failure alerts after N
  consecutive retrieval errors.

### M7 — Radarr / Sonarr
- Settings + test (`/api/v3/system/status`, profiles, root folders). Library cache (10 min):
  Radarr `imdbId`, Sonarr `imdbId`/`tvMazeId` → green "in library" badge.
- "Add to Radarr/Sonarr" via IMDb lookup (`imdb:ttXXXX`), copying the lookup object and adding
  profile/root-folder/monitored/search. Duplicate (400 "already") handled gracefully.

### M8 — Import from classic Spotweb
- CLI `python -m app.spotweb_import --url …` (MySQL/PostgreSQL/SQLite; optional driver).
  Keyset-paged read of `spots` ⟕ `spotsfull`, `commentsxover` ⟕ `commentsfull`,
  `spotstatelist` (download≠NULL → downloads), `spotteridblacklist` (→ blocked spotters).
  Message-ids stored without `<>` there → normalised. Reuses `parse_full_xml`. Resumable.

### M9 — Stats, duplicates, scoped keys, PWA
- `/stats` (spots per day, top posters, category mix, downloads over time, DB size) with inline
  SVG charts, cached.
- Duplicate grouping by `title_key`: "N releases" badge, siblings list, best-release pick.
- API keys with scope (`full` / `download`) + request counters.
- Installable PWA (manifest, service worker for the shell) and keyboard shortcuts (`/`, `g`, `j/k`, `?`).

## Status

All nine milestones are implemented (migrations/jobs/backups/CI image, encrypted secrets,
signatures + posters, performance, health checks, watchlists/notifications, Radarr/Sonarr,
Spotweb import, stats/duplicates/scoped keys/PWA). Not verified in this environment: the Docker image build
(no daemon available), service-worker registration (the embedded browser refuses it), and signature
verification against real Spotnet data (tested against our own signer and Spotweb's documented algorithm).

## Order and rules
M1 → M2 → M3 → M4 → M5 → M6 → M7 → M8 → M9. Each milestone: implement, tests, `ruff`,
full `pytest`, README/docs, commit. Migrations are additive; each new table/column ships
with its Alembic revision. Nothing is pushed without being asked.
