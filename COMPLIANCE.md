# SpotwebNG — FORGE compliance audit

> ## ✅ REMEDIATION 2026-07-27 — all audit findings fixed
> Every BLOCKER and MAJOR below is closed, plus the correctness set. Verified live, not assumed:
>
> | Finding | Before | After |
> |---|---|---|
> | Usenet password in `/setup` HTML | leaked in `value=""` | never rendered; blank = unchanged |
> | `/setup/test` port scanner + CRLF | any host:port, raw injection | non-news ports and CRLF rejected |
> | `spot.website` → `javascript:` XSS | rendered raw | `safe_website()` http(s) allowlist |
> | Password change = takeover | no current password, sessions kept | current password required, **all sessions revoked** |
> | `limit=-1` dumps index | 13,271 items | 1 item (clamped) |
> | Default API key in public repo | `changeme-spotwebnext-key` | generated per install, logged once |
> | `/docs` route map | public | disabled (404) |
> | Auth path matching | `startswith` opened `/apikeys` | whole-segment `is_open_path()` |
> | Parser crash freezes ingestion | `int('²')` raised forever | `safe_int()` + call-site guard |
> | Enrich batch loses 50 spots | one commit, `NntpError` only | per-spot commit, catches `Exception` |
> | Zip bomb / unbounded segments | unbounded | 64 segments, 12 MB fetch, 32 MB inflate |
> | Retrieve lock not shared | races, UNIQUE violations | one `RETRIEVE_LOCK` for both paths |
> | `hide_adult` on "/" | did nothing | applied to the empty tree |
> | Facet latency | 133 queries, **1.19 s** | grouped scans + indexes, **0.04 s (33×)** |
> | Retention `IN` > 32767 | hard Postgres failure | chunked 2,000 + off-thread unlink |
> | Pagination | no tiebreaker, unbounded offset | `id` tiebreaker, clamped |
> | SQLite locking | rollback journal, 5 s | **WAL** + 30 s busy timeout |
> | Sessions | never swept | swept each retrieval cycle |
> | Subcat truncation | cut mid-code | whole-code packing |
> | `-0000` dates | read as local time | read as UTC |
> | Lint / deps / health / logs | 8 findings, `>=` pins, fake health | **clean**, pinned, DB-backed `/healthz`, logging configured |
>
> **98 tests** (was 82), ruff clean. Still open by choice: no account created (yours to set),
> repo still public, CSRF tokens, and the uv/mypy/coverage gates the CI expects.

> **Update after `/forge:forge-init` (same day).** Governance moved; code did not.
>
> | | Before init | After init |
> |---|---|---|
> | Gate liveness | all MISSING | **pre-commit + pre-push + CI all installed** |
> | Secret allowlist | missing | `.gitleaks.toml` present, tree scans clean |
> | Constitution | none | `CLAUDE.md` stamped and filled |
> | `spot_data/` (2.8 GB) ignored | **no** | **yes** (added manually — init only covers its own dirs) |
> | SECURITY score | 3/8 | **3/8 — unchanged** |
> | ENGINEERING score | 3/10 | **4/10** (gates now live; everything else stands) |
>
> Measured against the newly installed gates, the project **does not pass its own hooks yet**:
> ruff 8 findings · `ruff format --check` would reformat **26 files** · coverage **75%** vs the
> CI's `--cov-fail-under=80` · no `uv.lock`/`pyproject.toml` so the uv-based CI cannot run at all.
> Every BLOCKER below is still open — nothing was fixed, by design.

**Date:** 2026-07-26 · **Commit:** `1e45a48` (working tree far ahead — ~4,000 lines uncommitted)
**Mode:** read-only. Nothing was modified. Fixes go through `/forge`; accepted exceptions belong in `.forge/waivers.md`.

This project was never `forge-init`'d, so there is no prior COMPLIANCE.md to diff against and
no `.forge/waivers.md` — nothing here can be waived yet. Treat this as the baseline.

---

## Scores

| Standard | MUSTs met | Score |
|---|---|---|
| SECURITY.md | 3 / 8 | **38%** |
| ENGINEERING.md | 3 / 10 | **30%** |
| LLM-SECURITY.md | n/a | not applicable — no LLM/agent surface in this app |

### SECURITY.md detail

| MUST | Verdict | Evidence |
|---|---|---|
| Validate all input server-side, allowlists over denylists | **PARTIAL** | `settings_store.CHOICES`/`CLAMPS` and `filters._TOKEN` are proper allowlists. But `provider_host`/`provider_port` (`web.py:221`) are unvalidated → SSRF, and `username`/`password` are never CRLF-filtered before entering NNTP commands. |
| Parameterized queries / ORM bindings only | **MET** | Every query is a bound SQLAlchemy expression. The one interpolation (`filters._match_code`) can only contain `[a-z]\d+` — regex-gated then `int()`-normalised. |
| Deny-by-default authz on every route | **FAIL** | `auth.is_authorised` returns `True` when no account exists — the live install has none, so all 17 routes are open. `main.OPEN_PREFIXES` also uses bare `startswith`, so a future `/apikeys` route would silently bypass the guard. `/docs` + `/openapi.json` are public. |
| Secrets only in env/secret manager, never in code/config/logs | **FAIL** | Usenet password stored plaintext in `settings` and **rendered into HTML** (`setup.html:51`). Default API key `changeme-spotwebnext-key` is hardcoded (`config.py:35`) and documented in a public README. |
| Vetted crypto — Argon2id, TLS 1.2+ | **PARTIAL** | TLS verified properly via `ssl.create_default_context()` in both NNTP paths. Password hashing is PBKDF2-SHA256 @ 240k iterations — defensible, but the standard names Argon2id. |
| Pin dependencies · pip-audit in CI · SBOM | **FAIL** | `requirements.txt` uses `>=` throughout; no lockfile, no CI, no SBOM. |
| Structured logging without secrets · audit events on auth | **FAIL** | No `logging.basicConfig` anywhere, no structured logging, and zero audit events on login/logout/password-change. |
| Generic external errors, detail internal | **PARTIAL** | Mostly fine, but `/retrieve` returns `f"Retrieval failed: {exc}"` (`web.py:205`) and `/setup/test` reflects the remote server's banner verbatim — both leak internals. |

### ENGINEERING.md detail

| MUST | Verdict | Evidence |
|---|---|---|
| uv-managed (uv.lock) · ruff · mypy --strict · pytest | **PARTIAL** | pytest: strong (82 tests, all pass). No `uv.lock`, no `pyproject.toml`, no mypy config. Ruff is clean-ish: **8 findings, 2 auto-fixable**. |
| Type hints on public signatures · never bare `except:` | **MET** | Hints are consistently present; **zero** bare excepts. (Several broad `except Exception` blocks exist and are flagged as correctness issues below, not style violations.) |
| Pydantic v2 I/O with `response_model` on every route | **FAIL** | 0 Pydantic request models, 0 `response_model` across 17 routes. Forms are parsed as raw `Form(...)`/`await request.form()`. |
| DI for auth/DB/settings · OAuth2/JWT · rate-limit auth routes | **PARTIAL** | DB/session DI is clean (`Depends(get_session)`, 56 usages). Auth is cookie+opaque-token, not OAuth2/JWT. **No rate limiting on `/login`** — unlimited password guessing. |
| RFC 9457 problem+json · explicit CORS · `/v1` versioning | **FAIL** | No problem+json, no CORS middleware, no versioning. (The Newznab `/api` contract is externally fixed and cannot be versioned — waiver candidate.) |
| PRIVATE GitHub repo at project start | **FAIL** | Repo is **public** while containing an app that stores Usenet credentials. |
| Conventional Commits · feature branches · PR required · atomic commits | **FAIL** | One commit total; ~4,000 lines uncommitted on the default branch; no branches, no PRs. |
| Test pyramid · unit+integration per feature | **MET** | 9 test files, 82 tests: unit (parser, filters, settings) + integration (fake NNTP server, ASGI client). Behaviour-asserting, not implementation-coupled. |
| Coverage ≥80% enforced in CI | **FAIL** | No coverage measurement, no CI. |
| Gates live (pre-commit/pre-push/CI) | **FAIL** | No `.pre-commit-config.yaml`, nothing installed, no CI workflow. |

---

## Quick wins (auto-fixable / minutes)

1. `ruff check --fix` — clears 2 of 8 findings (unused imports in `auth.py`, `test_settings_store.py`).
2. Remaining 6 are `E402` (module-level imports not at top) in `retriever.py` and `test_retriever.py` — artefacts of appending code to existing files. Move the imports up.
3. Add `spot_data/` and `backups_*.db` to `.gitignore` — **2.8 GB is currently untracked-but-not-ignored** in a public repo.
4. `value=""` on the provider password field, with "leave blank to keep" handling.
5. Generate the default API key with `secrets.token_hex(16)` on first boot instead of shipping a constant.
6. Pin `requirements.txt` to `==` from the working `.venv`.

## Architectural (needs a triaged task)

1. **Close the auth bootstrap.** Fail-closed when no account exists (serve only account creation), fix `OPEN_PREFIXES` to exact/segment matching, disable `/docs` in production, rate-limit `/login`, require the current password on change and invalidate sessions.
2. **Lock down `/setup/test`.** Require auth, allowlist ports, reject CRLF in credentials, stop reflecting remote banners. Same CRLF validation on the stored settings the retriever consumes.
3. **Scheme-allowlist `spot.website`** (`http`/`https` only) at write and render — currently a stored-XSS vector from any Usenet poster.
4. **Bound untrusted input:** clamp `limit` (`limit=-1` currently dumps the whole index — verified 13,271 items), cap segment count/bytes in `fetch_binary`, and use incremental decompression against zip bombs.
5. **Make ingestion crash-proof.** `parse_xover_row` raises on `int()` for exotic digits; one malformed post freezes retrieval permanently. Guard at the call site.
6. **Facet performance:** 125–133 `COUNT(*)` scans per filtered page (~1.2 s measured). Replace with grouped queries; add the missing `ix_spots_spot_category`.
7. **Observability:** structured logging + auth audit events + a `/healthz` that actually touches the database.
8. **Supply chain / CI:** lockfile, pip-audit, SBOM, coverage gate, pre-commit and pre-push hooks.
9. **Repo governance:** commit the work in atomic Conventional Commits on a feature branch; decide public-vs-private deliberately given the credential-handling code.

## Consolidation plan (governance docs)

Only 3 markdown files exist (`README.md`, `scaffold/README.md`, this file) and **zero** rule-ish
governance docs — there is no sprawl to consolidate. Two proposals, neither destructive:

- `scaffold/README.md` describes a legacy PHP fetcher scaffold that the native Python engine has now
  replaced. **Propose archiving** it (move under `docs/archive/`) rather than deleting, and note in
  `README.md` that Phase 3 is implemented.
- `README.md`'s Roadmap is now inaccurate (it lists Phase 3 as pending; it is live). **Propose** updating
  rather than adding a second status document.

If this project adopts FORGE properly, run `/forge:forge-init` to stamp the constitution, gates and
`.forge/waivers.md` — several findings above (Newznab `/api` versioning, PBKDF2-vs-Argon2id) are
legitimate waiver candidates that should be written down rather than silently ignored.

## Could not check

- **Runtime behaviour under load** — no load test was run; the 1.2 s facet figure is a single-request measurement on the live 15,387-spot database.
- **Whether the exposed Usenet password has been rotated.** It is currently readable from `/setup` by anyone on the LAN; assume compromised until rotated.
- **Full git history** — only the last 50 commits are scanned, and this repo has exactly 1. History is clean of secrets; the risk is prospective (an unguarded `git add -A`).
- **Auth correctness beyond presence heuristics** — the deny-by-default failure above was confirmed by reading `auth.is_authorised` and the live settings table, but no penetration test was performed.
- **`forge-comply.sh` mis-reported `test files: 0`** — the repo has 9 test files and 82 passing tests. The script's discovery glob does not match this layout; scores above use the verified numbers, not the script's.
- **Postgres deployment path** — reviewed statically only; several SQLite-vs-Postgres divergences were identified (unbounded `IN` parameter lists, idle-in-transaction sessions, missing rollback after unique violations) but the compose stack was not launched.
