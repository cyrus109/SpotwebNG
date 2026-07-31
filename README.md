# SpotwebNG — a modern Spotweb

**SpotwebNG** is a modern, self-hosted **Spotweb** alternative — a clean reimagining of the
classic [Spotweb](https://github.com/spotweb/spotweb) Spotnet/Usenet community indexer, rebuilt
in Python/FastAPI with a fast Netflix-style web UI and a **Newznab-compatible API**.

If you're looking for a faster, better-looking, actively modern replacement for the old PHP
Spotweb — this is it.

> **Keywords:** spotweb, spotweb alternative, modern spotweb, spotnet, usenet, newznab indexer,
> nzb, self-hosted usenet, FastAPI.

## Why SpotwebNG

The original Spotweb is powerful but dated: PHP, end-of-life dependencies, a heavy server-rendered
UI, and a security audit that ships switched off. SpotwebNG keeps what Spotnet does well and
modernizes everything around it:

- ⚡ **Newznab API** — `caps`, `search`, `tvsearch`, `movie`, `get`. Drop it into **Radarr,
  Sonarr, Prowlarr** or any Newznab client as a regular indexer.
- 🎬 **Modern web UI** — Netflix-style poster grid with a one-click **table-view toggle** and
  rich detail pages.
- 🧭 **Setup wizard** — choose from a catalog of **68 Usenet providers** (host/port/SSL autofill);
  you just add your username and password.
- 🐘 **Postgres** via SQLAlchemy 2.0 async (a local SQLite file works out of the box for dev).
- 🐳 **Docker-first** deployment.

## Run it

### Quick look (no infra — SQLite + demo data)
```bash
git clone https://github.com/Mx4flav0r/SpotwebNG.git
cd SpotwebNG
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 7979
# open http://localhost:7979  (first run redirects to /setup)
```

### Docker (recommended)

```bash
docker compose up -d          # http://localhost:7979
```

One container, one volume. The database, spot covers and cached NZBs all live in `/data`,
so they survive an image upgrade. The app generates its own Newznab API key on first boot
(look for it in `docker compose logs`), and the setup wizard creates the administrator —
until you complete it, the instance is open, so do it first.

### Production (Postgres, Docker)
```bash
SPOTWEBNEXT_API_KEY=your-strong-key docker compose up --build
```

## Use it as an indexer

In any Newznab client (Radarr / Sonarr / Prowlarr), add a **Newznab** indexer pointing at:

- URL: `http://<your-host>:7979`
- API key: the key shown in the setup wizard

It serves `GET /api?t=search&q=…&apikey=…&cat=…` and works alongside your other indexers.

## What works today

- **Native Spotnet retrieval** — reads spot headers from `free.pt` over NNTP, parses the
  Spotnet wire format (category/subcategory/size packed into the `From` header), and keeps
  an incremental bookmark with article-renumber protection.
- **Full-spot enrichment** — fetches each spot's `X-XML` payload for its description, cover
  image and NZB segment list. Covers are served locally; **`t=get` returns a real NZB**.
- **Spotweb-compatible filtering** — the `tree` syntax (`cat0_z0_a9`, `~cat0_z3` to exclude),
  OR within a dimension and AND across them, with live counts on every option.
- **Newznab API** for Radarr / Sonarr / Prowlarr, with per-client API keys.
- **Accounts** — administrators configure the instance, viewers only browse.
- **Retention** — keep spots listed but drop their heavy parts, or delete them outright.

## Roadmap

- Comments and reports groups (ratings, spam reports).
- Saved filters and per-user watchlist / seen state.
- Desktop clients (see `docs/` once published).

## Configuration (env, prefix `SPOTWEBNEXT_`)

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | local SQLite file | set to `postgresql+asyncpg://…` for production |
| `DEFAULT_API_KEY` | a key generated on first boot (see the logs, or Settings → API access) | the Newznab key clients authenticate with |
| `SEED_DEMO_DATA` | `true` | seed demo spots on first boot; set `false` in production |

## License

MIT — same spirit as the original Spotweb.
