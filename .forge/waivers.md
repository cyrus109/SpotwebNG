# Accepted exceptions

Findings listed here are known, deliberate, and must not be re-raised by an audit
without new evidence. Each needs a written justification and an owner.

---

## ENGINEERING: `ruff` rule B008 disabled

**Rule:** "Do not perform function call in argument defaults."
**Why waived:** `session: AsyncSession = Depends(get_session)` and `q: str = Query("")`
are FastAPI's documented dependency-injection idiom. FastAPI inspects those defaults to
build the dependency graph; "fixing" them would break the framework contract.
**Scope:** repository-wide, `app/` only.

## ENGINEERING: `mypy --strict` applied per-module, not globally

**Standard says:** `mypy --strict`.
**Current state:** `pyproject.toml` runs mypy over `app/` with `ignore_missing_imports`
(third-party stubs are partial), and enforces `disallow_untyped_defs` on the modules
carrying the real logic: `app.spotnet`, `app.filters`, `app.settings_store`,
`app.retention`.
**Why:** the codebase was written before typing was gated. Turning strict on everywhere
at once produces a large diff with no behavioural benefit and a real risk of masking
errors behind hurried annotations.
**Exit condition:** add one module to the strict override list per change that touches it.
Remove this waiver when the list covers `app/*`.

## ENGINEERING: Newznab `/api` is not versioned under `/v1`

**Standard says:** `/v1` versioning on HTTP APIs.
**Why waived:** `/api?t=caps|search|get` is an external contract fixed by Newznab
clients (Radarr, Sonarr, Prowlarr). Versioning the path would make the app unusable
with every existing client. The app's own endpoints are free to version later.
**Scope:** `app/newznab.py` only.

## SECURITY: PBKDF2-SHA256 instead of Argon2id

**Standard says:** Argon2id for password hashing.
**Current state:** PBKDF2-HMAC-SHA256, 240,000 iterations, 16-byte random salt per user,
constant-time comparison — all from the standard library.
**Why:** Argon2 requires a compiled dependency (`argon2-cffi`); this app is deliberately
pure-Python so it runs anywhere without a build toolchain. PBKDF2 at this iteration count
is an accepted OWASP configuration.
**Exit condition:** adopt Argon2id if a compiled dependency becomes acceptable, migrating
hashes lazily on next login.

## ENGINEERING: no Pydantic `response_model` on HTML routes

**Standard says:** Pydantic v2 I/O with `response_model` on every route.
**Why waived:** most routes render Jinja templates or return XML for the Newznab
contract; a response model adds nothing to an `HTMLResponse`. JSON endpoints
(`/setup/test`, `/settings/test`, `/retrieve`, `/setup/apikey`) return small, fixed
shapes and are covered by tests.
**Exit condition:** revisit if a JSON API for third parties is added.

## ENGINEERING: coverage gate — CLEARED 2026-08-01

Was 77% against an 80% gate. Route-level tests (`tests/test_routes.py`) brought it to 80%
and the CI gate now passes. Kept here as a record; no exception is in force.
