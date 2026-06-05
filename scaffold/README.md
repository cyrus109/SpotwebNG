# scaffold/ — quarantined legacy retriever (Phase 1 only)

This directory will hold the **legacy Spotweb `retrieve.php`** fetcher, running
as a sealed cron container whose *only* job is to populate the Postgres `spots`
table from Usenet.

## Rules of quarantine
- It writes to the DB **only**. It never serves HTTP, never touches the API or UI.
- It is a **temporary bootstrap**, not a permanent dependency.
- Tracking issue: **"Delete PHP retriever"** — closed in Phase 3 when the native
  Python engine (`app/engine/`) replaces it.

## Why it exists
Porting Spotnet's signed-XML spot parsing + RSA verification + NNTP retrieval
(~3.5k lines of PHP) is the single hardest, riskiest piece. Phase 1 ships a
working Newznab source *now* by reusing the proven PHP fetcher,
then Phase 3 ports it to Python and diffs Python-fetched rows against
PHP-fetched rows in the same DB as a correctness oracle.

## Status
Not yet wired. To enable: drop the upstream `spotweb` tree here, add a thin
adapter that maps its DB writes to the SpotwebNext `spots` schema, then
uncomment the `retriever` service in `docker-compose.yml`.
