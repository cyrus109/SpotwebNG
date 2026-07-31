<div align="center">

# SpotwebNG

**A modern Spotweb.** Self-hosted Spotnet indexer for Usenet, with a Newznab API
for Radarr, Sonarr and Prowlarr — and a UI you'll actually want to browse.

[![CI](https://github.com/FornaxMedia/SpotwebNG/actions/workflows/forge-ci.yml/badge.svg)](https://github.com/FornaxMedia/SpotwebNG/actions)
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

| Filtering by category | Spot detail |
|---|---|
| ![Filters](docs/img/filters.png) | ![Detail](docs/img/detail.png) |

<details>
<summary>Settings</summary>

![Settings](docs/img/settings.png)

</details>

## Run it

```bash
git clone https://github.com/FornaxMedia/SpotwebNG.git
cd SpotwebNG
docker compose up -d
```

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

## Use it as an indexer

In Radarr, Sonarr or Prowlarr, add a **Newznab** indexer:

- **URL** — `http://your-host:7979/api`
- **API key** — Settings → API access. Create one key per client, so you can revoke a
  single client without breaking the others.

Supports `t=caps`, `search`, `tvsearch`, `movie`, `music` and `get`. `t=get` returns a
**real NZB**, assembled from the spot's Usenet segments — not a stub.

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
