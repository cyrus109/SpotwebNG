<div align="center">

# SpotwebNG

**A modern Spotweb.** Self-hosted Spotnet indexer for Usenet, with a Newznab API
for Radarr, Sonarr and Prowlarr — and a UI you'll actually want to browse.

[![CI](https://github.com/cyrus109/SpotwebNG/actions/workflows/forge-ci.yml/badge.svg)](https://github.com/cyrus109/SpotwebNG/actions)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED?logo=docker&logoColor=white)](#run-it)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)

![SpotwebNG browsing spots](docs/img/browse.png)

</div>

## What it is

Spotnet is a Usenet community that publishes signed "spots" — posts describing content
available on Usenet. [Spotweb](https://github.com/spotweb/spotweb) has indexed them since
2011 and still works, but it is PHP from another era.

**SpotwebNG is a ground-up rebuild** in Python: the same Spotnet retrieval engine and
category system, a Newznab API your existing tools already speak, and a browsing
experience built for how people actually look for things.

It is not a fork. The wire format, category tree and filter grammar were re-derived from
the Spotweb source so behaviour matches; everything else is new.

## Why you might want it

|  | Spotweb | SpotwebNG |
|---|---|---|
| Stack | PHP 7, jQuery | Python 3.11, FastAPI, async SQLAlchemy |
| Install | web server + PHP + database | `docker compose up -d` |
| Browsing | dense tables | poster grid with cover art, or a table if you prefer |
| Filtering | category tree | the same tree grammar, with **live counts on every option** |
| Accounts | full ACL system | admin / viewer roles, throttled login |
| API | Newznab | Newznab, per-client keys with last-used tracking |

## Screenshots

![Filtering by category](docs/img/filters.png)

*Sub-filters are contextual: Movies get Format, Source, Language and Genre; Games get
Platform, Format and Genre. Every option shows how many spots it would leave.*

## Run it

```bash
git clone https://github.com/cyrus109/SpotwebNG.git
cd SpotwebNG
docker compose up -d --build
```

To update later: `git pull && docker compose up -d --build`. Your data stays in the volume.

Open <http://localhost:7979>. The setup wizard asks for an administrator login first —
**complete it, because until you do the instance is open to anyone who can reach it** —
then your Usenet provider, picked from a built-in catalogue of 68 with host, port and TLS
filled in for you. The connection is tested before anything is saved.

Everything lives in one Docker volume: the database, cached cover art and assembled NZBs.

<details>
<summary>Without Docker</summary>

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 7979
```

SQLite is the zero-config default; set `SPOTWEBNEXT_DATABASE_URL` to a
`postgresql+asyncpg://` URL to use Postgres instead.

</details>

## Connect your download tools

SpotwebNG speaks the **Newznab API**, which almost every Usenet tool already
understands. Get a key from **Settings → API access** (one per client, so you can
revoke one without breaking the rest), then point tools at:

```
http://your-host:7979/api
```

Worth knowing before you start: **SABnzbd and NZBGet are downloaders, not
searchers.** They fetch NZB files but do not query indexers. The search side is
handled by Prowlarr or the *arr apps, which then hand the NZB to your downloader.

### Indexer managers — configure once, share everywhere

| Tool | Where | What to enter |
|---|---|---|
| **Prowlarr** | Indexers → Add → **Generic Newznab** | URL `http://your-host:7979`, API path `/api`, your API key |
| **NZBHydra2** | Config → Indexers → Add → **Newznab** | Host `http://your-host:7979`, API key, then *Check caps* |

This is the recommended route: add SpotwebNG once in Prowlarr and it syncs to
Radarr, Sonarr, Lidarr and Readarr automatically.

### The *arr apps — add it directly

In **Settings → Indexers → Add → Newznab**:

| App | Categories to select | Search it uses |
|---|---|---|
| **Radarr** | 2000 Movies | `t=movie` with `imdbid` or a title |
| **Sonarr** | 5000 TV | `t=tvsearch` with season and episode |
| **Lidarr** | 3000 Music | `t=music` with artist and album |
| **Readarr** | 7000 Books | `t=book` with author and title |
| **Whisparr** | 6000 Erotica | `t=search` — see the note below |

Leave *Additional Parameters* empty. If a client reports "indexer does not
support this search", check that its category is present in
`http://your-host:7979/api?t=caps`.

**Adult content is hidden by default.** While *Settings → Display → Hide adult
content* is on, the 6000 range is not advertised in caps, is not returned by any
search, and its covers are not served — so Whisparr will find nothing until an
administrator turns the setting off. Only administrators can change it.

Mylar3 asks for comics (category 7030), which Spotnet does not file separately,
so it has nothing useful to match against.

### Downloaders — subscribe to a feed

The API returns a standard RSS 2.0 feed whose items carry an NZB `<enclosure>`,
so a downloader can grab new spots without any indexer app in between.

**SABnzbd** — Config → RSS → add a feed:

```
http://your-host:7979/api?t=search&cat=2040&apikey=YOUR_KEY
```

Set a filter (for example `Accept` on a title pattern), tick *Read the feed*, and
SABnzbd will download matching spots as they appear. Swap `cat=` for the
categories you want; `2040` is Movies/HD, `5040` is TV/HD, `3040` lossless music.

**NZBGet** has no built-in RSS reader — use Prowlarr or an *arr app in front of
it, or one of the community RSS extension scripts.

### Mobile and browser

- **nzb360** (Android) — add under *Indexers* as Newznab with the URL and key.
- **LunaSea** / **nzbUnity** — anywhere that accepts a Newznab indexer works.

### Check it yourself

```bash
curl "http://your-host:7979/api?t=caps"                                   # no key needed
curl "http://your-host:7979/api?t=search&q=ubuntu&apikey=YOUR_KEY"        # a search
curl -O "http://your-host:7979/api?t=get&id=123&apikey=YOUR_KEY"          # a real NZB
```

If `t=caps` answers but a client refuses, it is nearly always the API path: some
clients want the base URL, others the full `/api`.

## How it works

- **Retrieval** — reads spot headers from the `free.pt` newsgroup over NNTP in XOVER
  batches, unpacking the metadata Spotnet packs into the `From` header. The position
  bookmark is validated by message-id, so article renumbering on the server cannot make
  the indexer skip or replay a range.
- **Enrichment** — fetches each spot's full XML payload for its description, cover art and
  NZB segment list. Covers are cached and served locally.
- **Filtering** — implements Spotweb's `tree` grammar (`cat0_z0_a9`, `~` to exclude): OR
  within a dimension, AND across dimensions. All 283 Spotnet category codes are included
  under their real names.
- **Retention** — keep spots listed while dropping their heavy parts, or delete them
  outright. Without a policy an index grows forever.
- **Full history** — see below.

## Retrieving the full history

By default a first run only fetches the most recent articles (*First-run backfill*, 20,000
by default, up to 500,000). To index everything your Usenet provider still keeps — often
years of spots — either:

- turn on **Retrieve the server's full history** in *Settings → Retrieval & retention*, so
  the background loop does it automatically, or
- press **Full history** on the browse toolbar for a one-off run (admins only).

How it works: after fetching new spots, a second pass walks backwards from the oldest article
already scanned down to the server's oldest article, in 5,000-article chunks. The backfill
size is ignored while this is on. A bookmark (`spots_floor`) is saved after every chunk, so
it resumes after a restart and is validated by message-id like the forward bookmark.
Duplicates are skipped, so it is safe to turn on for an existing install.

Things to know:

- It can take hours and the database grows a lot. Use Postgres for very large histories.
- While it runs, the plain **Refresh** button answers "already running".
- Consider turning off *Background enrichment* first; otherwise covers and NZB references are
  fetched for every spot.
- If retention is set to delete old spots, the backfill skips spots older than the cutoff.
- Progress shows in the container log (`docker compose logs -f`) as `floor` in the
  `spot retrieval:` line, falling towards the server's oldest article.

## Configuration

Everything is configurable in the UI. Environment variables (prefix `SPOTWEBNEXT_`) exist
for deployments:

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | SQLite file | `postgresql+asyncpg://…` for Postgres |
| `SEED_DEMO_DATA` | `true` | demo spots on first boot so the UI is not empty |
| `APP_NAME` | `SpotwebNG` | shown in the UI and in Newznab caps |

`SPOTWEBNG_DATA` sets where cover art and NZBs are written; the Docker image points it at
the volume.

## Status

Working today: retrieval, enrichment, filtering, the Newznab API, accounts and retention.
Not yet: comments and reports groups, saved filters, per-user watchlists.

Contributions welcome. Tests run with `pytest`; `ruff` and `mypy` gate every commit.

## Credits

The Spotnet wire format, category tree and filter semantics are derived from
[Spotweb](https://github.com/spotweb/spotweb) by the Spotweb contributors. This project
reimplements them and shares no code.

## License

Apache 2.0 — see [LICENSE](LICENSE).

<!--
Keywords: spotweb alternative, modern spotweb, spotnet indexer, usenet indexer,
newznab indexer, nzb indexer, self-hosted usenet, radarr sonarr prowlarr indexer,
spotweb docker, python usenet indexer, spotnet.
-->
