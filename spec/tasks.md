# Implementation Task List — Personal Job Discovery & Matching Tool

**Companion docs:** `job-finder-spec.md`, `job-finder-hld.md`, `job-finder-lld.md`
**Audience:** a Claude Code + Ralph loop. Implement tasks **strictly in order**. When
every task is checked off, the project is complete and runnable.

## How to use this list (Ralph loop operating rules)
- Do **one task per iteration**. Do not start a task until all its `Depends on` are done.
- Each task lists **Files**, **Do**, and **Done when** (the acceptance check). A task is
  complete only when its `Done when` holds **and** `pytest` is green **and** `ruff` is clean.
- Tests use **committed fixtures only — never live network calls.** Keeps the loop
  deterministic and free.
- After each task: update `PROGRESS.md` (task id, status, notes). If blocked on a
  real-world unknown (e.g. a real board token), insert a `# TODO verify` and continue;
  collect all such TODOs in README.
- Never implement an application-submission/POST-to-apply path. Read-only against
  job sources. No paid services.
- Keep commits small: one task ≈ one commit.

Legend: **[P0]** must-have for a working product · **[P1]** completeness · **[P2]** polish.

---

## Phase 0 — Project skeleton

### T01 — Repo scaffold & packaging  **[P0]**  `[x] Complete`
- **Depends on:** none
- **Files:** `pyproject.toml`, `requirements.txt`, `.gitignore`, `README.md` (stub),
  `src/jobfinder/__init__.py`, `tests/__init__.py`, `PROGRESS.md`
- **Do:** Create the package layout from LLD §1. Pin deps from LLD §14. Register the
  `jobfinder = "jobfinder.cli:app"` entry point. `.gitignore` must cover
  `data/`, `config/resume.*`, `.env`. Add empty `cli.py` with a no-op `app` so the
  entry point imports.
- **Done when:** `pip install -e .` succeeds; `jobfinder --help` exits 0; `pytest`
  collects 0 tests without error; `ruff check` clean.

### T02 — Settings & config loading  **[P0]**  `[x] Complete`
- **Depends on:** T01
- **Files:** `src/jobfinder/settings.py`, `config/*.example` files, `.env.example`,
  `tests/test_settings.py`, `tests/fixtures/config/*`
- **Do:** Implement `settings.py` per LLD §11.4 with pydantic-settings: resolves paths,
  reads `.env`, exposes `throttle_s`, `cache_ttl_s`, `embed_model`, db/log/cache paths,
  `max_age_days`, `retention_days`. Provide `profile.yaml.example`,
  `companies.yaml.example`, `weights.yaml.example`, `.env.example` (LLD §11). Loaders
  for profile/companies/weights with pydantic validation (fail-fast, clear errors).
- **Done when:** loading a valid fixture config returns typed objects; loading a
  malformed one raises a precise validation error; missing optional Adzuna keys → flag
  set, no crash. Tests cover both paths.

### T03 — Core data models  **[P0]**  `[x] Complete`
- **Depends on:** T01
- **Files:** `src/jobfinder/models.py`, `tests/test_models.py`
- **Do:** Implement `RawPosting`, `Job`, `ScoreBreakdown`, and the `LocationBucket`,
  `Seniority`, `Status` enums per LLD §2. Implement the stable `Job.id` derivation
  `sha1(f"{source}:{source_id}")[:16]`.
- **Done when:** same `(source, source_id)` yields identical `id`; different inputs
  differ; enum round-trips to/from str. Tests assert id stability.

---

## Phase 1 — Persistence

### T04 — SQLite schema & connection  **[P0]**  `[x] Complete`
- **Depends on:** T03
- **Files:** `src/jobfinder/store.py` (connect + DDL), `tests/test_store.py`
- **Do:** Implement connection with PRAGMAs (WAL, synchronous=NORMAL, busy_timeout=5000,
  foreign_keys=ON) and `init_db()` running the full DDL + indexes from LLD §7.2. Use an
  in-memory or temp-file DB in tests.
- **Done when:** `init_db()` creates all tables/indexes idempotently (safe to run twice);
  PRAGMAs verified via `PRAGMA` queries in a test.

### T05 — Job upsert & dedupe  **[P0]**  `[x] Complete`
- **Depends on:** T04
- **Files:** `store.py` (`upsert_job`), `tests/test_store.py`
- **Do:** Implement `upsert_job` with `ON CONFLICT(source, source_id) DO UPDATE`,
  preserving `first_seen_at`, bumping `last_seen_at`, updating mutable fields,
  persisting `embedding` BLOB and `eligible`/`ineligible_reason`/`content_hash`.
- **Done when:** inserting the same job twice → exactly one row, `first_seen_at`
  unchanged, `last_seen_at` advanced. Test asserts this.

### T06 — Scores, status, runs, companies DAL  **[P0]**  `[x] Complete`
- **Depends on:** T05
- **Files:** `store.py` (`save_score`, `set_status`, `start_run`/`finish_run`,
  company read/write, `prune`), `tests/test_store.py`
- **Do:** Implement the remaining operations from LLD §7.3, including `prune(not_seen_days)`
  and run bookkeeping. Cascade deletes via FK.
- **Done when:** saving a score then deleting its job cascades; `prune` removes only
  rows older than the cutoff; a run row records `started_at`/`finished_at`/`per_source_json`.

---

## Phase 2 — Fetch & normalize (the data in)

### T07 — Shared HTTP client (throttle, retry, cache)  **[P0]**  `[x] Complete`
- **Depends on:** T02
- **Files:** `src/jobfinder/sources/http.py`, `tests/test_http.py`
- **Do:** Implement `get_json`/`get_text` per LLD §3.2: single `httpx.Client` with
  timeouts/http2/User-Agent, per-host throttle (≥`throttle_s`), retry on
  `{429,500,502,503,504}`+timeouts with backoff+jitter honoring `Retry-After`, on-disk
  cache keyed by sha1(url) with TTL, `--no-cache` bypass. Mock transport in tests
  (no real network).
- **Done when:** retry fires on a mocked 503 then succeeds; cache hit avoids a second
  transport call; throttle enforces min spacing (tested with a fake clock).

### T08 — Source protocol & registry  **[P0]**  `[x] Complete`
- **Depends on:** T03, T07
- **Files:** `src/jobfinder/sources/base.py`, `tests/test_sources.py`
- **Do:** Define `Source` protocol, `SourceResult`, and a `SOURCES` registry keyed by
  name that constructs enabled adapters from settings. A source missing its required
  secret returns an empty `SourceResult` with a note rather than raising.
- **Done when:** registry yields only enabled sources; a secret-less optional source is
  constructible and returns empty cleanly.

### T09 — Normalizer: HTML, dates, helpers  **[P0]**  `[x] Complete`
- **Depends on:** T03
- **Files:** `src/jobfinder/normalize.py` (`html_to_text`, `parse_date`), `tests/test_normalize.py`
- **Do:** Implement `html_to_text` (selectolax: drop script/style, get text, collapse
  whitespace, unescape entities) and `parse_date` handling ISO8601-with-offset → UTC and
  epoch-ms → UTC, failure → `None`. Per LLD §4.3.
- **Done when:** entity-laden HTML fixture → clean text; ISO and epoch-ms fixtures parse
  to correct UTC datetimes; garbage → `None`.

### T10 — Normalizer: location bucketing & seniority  **[P0]**  `[x] Complete`
- **Depends on:** T09
- **Files:** `normalize.py` (`bucket_location`, `infer_seniority`, `normalize`),
  `tests/test_normalize.py`
- **Do:** Implement the ordered rules from LLD §4.1–§4.2 and the top-level
  `normalize(raw, company_hint, now) -> Job` that ties field extraction + helpers
  together and sets `date_unknown`.
- **Done when:** bucketing correct for remote-Canada, US-only-remote (→other), Vancouver,
  Toronto, other-Canada; seniority correct across junior/mid/senior/staff/manager/unknown
  titles. Tests cover each branch.

### T11 — Greenhouse adapter  **[P0]**  `[x] Complete`
- **Depends on:** T08, T10
- **Files:** `src/jobfinder/sources/greenhouse.py`, `tests/fixtures/greenhouse_*.json`,
  `tests/test_sources.py`
- **Do:** Implement `fetch` hitting the LLD §3.3 endpoint (`?content=true`), mapping the
  verified fields, and **dropping postings older than `max_age_days` before returning**
  (no server-side filter). Guard every field access.
- **Done when:** parsing the committed fixture yields correct `RawPosting`s; a stale
  fixture row is excluded by the recency pre-filter; `kept_after_recency` reported.

### T12 — Lever adapter  **[P0]**  `[x] Complete`
- **Depends on:** T08, T10
- **Files:** `src/jobfinder/sources/lever.py`, `tests/fixtures/lever_*.json`,
  `tests/test_sources.py`
- **Do:** Implement `fetch` per LLD §3.4 (`?mode=json&limit=100`, paginate via `skip`),
  map verified fields (`text`, `categories.location`, `createdAt` epoch-ms,
  `hostedUrl`/`applyUrl`), company name from `companies.yaml` entry, optional salary.
- **Done when:** fixture parses correctly; pagination stops cleanly; epoch-ms date
  normalized; company hint applied.

---

## Phase 3 — Filter & score (the ranking)

### T13 — Eligibility filters  **[P0]**  `[x] Complete`
- **Depends on:** T10, T02
- **Files:** `src/jobfinder/filters.py`, `tests/test_filters.py`
- **Do:** Implement `is_eligible` per LLD §5: ordered cheapest-first gates (recency →
  role keyword → location → seniority/people-manager), returning `(bool, reason)`.
  `date_unknown` passes recency. Ineligible jobs are kept (flagged), not dropped.
- **Done when:** stale/non-backend/out-of-location/junior/manager each rejected with the
  correct reason; eligible role passes; `date_unknown` passes. Tests cover each.

### T14 — Resume extraction  **[P0]**  `[x] Complete`
- **Depends on:** T02
- **Files:** `src/jobfinder/score.py` (`extract_resume`), `tests/fixtures/resume.*`,
  `tests/test_score.py`
- **Do:** Implement `extract_resume` per LLD §6.5: pdf (pypdf, pdfplumber fallback),
  docx (python-docx incl. tables), txt/md direct. Returns full text; clear error if missing.
- **Done when:** a committed sample resume in each supported format extracts non-empty
  text; missing file raises a clear error.
- **Deps added (`uv add`):** `pypdf` (primary PDF text extraction), `pdfplumber`
  (layout-tolerant PDF fallback), `python-docx` (docx paragraphs + tables) — all from
  the LLD §14 target set, pulled in by the first task to import them.

### T15 — Embeddings & profile vector  **[P0]**  `[x] Complete`
- **Depends on:** T14
- **Files:** `score.py` (model load, `build_profile_vector`, `embed_job`), `tests/test_score.py`
- **Do:** Load `SentenceTransformer(settings.embed_model)`; build the profile vector per
  LLD §6.2 (targeting block prepended to full resume, chunk to ~256 tokens, mean-pool,
  L2-normalize); `embed_job` for a job's `title+desc` (char-capped). Cache model across calls.
- **Done when:** profile vector has expected dim and unit norm; embedding is deterministic
  for fixed input/model; long resume is chunked (tail not truncated). (Model download
  allowed once in test setup, or use a tiny test model — keep offline thereafter.)
- **Deps added (`uv add`):** `sentence-transformers` (the core local-embedding model loader,
  the one intentionally-heavy dep per RALPH/LLD §14) and `numpy` (vector mean-pool +
  L2-normalize math) — both from the LLD §14 target set, pulled in by the first task to
  embed text.

### T16 — Scoring math & weights  **[P0]**  `[x] Complete`
- **Depends on:** T15, T13
- **Files:** `score.py` (`score_job`), `config/weights.yaml(.example)`, `tests/test_score.py`
- **Do:** Implement components + weighted final per LLD §6.3–§6.4: semantic cosine
  (clamped 0–1), skill match over {java,kotlin,python,aws}, location bonus map, linear
  recency decay (date_unknown→0.3), normalized weighted sum → 0–100; return full
  `ScoreBreakdown`. Skip re-embedding when `content_hash` unchanged.
- **Done when:** **ordering test passes** — a senior remote Java/AWS role outranks a
  junior onsite frontend role, and skill weight makes a Java/AWS role beat a
  higher-semantic off-stack role; breakdown components stored.

---

## Phase 4 — Pipeline (wire the poll together)

### T17 — Poll pipeline orchestration  **[P0]**  `[x] Complete`
- **Depends on:** T06, T11, T12, T13, T16
- **Files:** `src/jobfinder/pipeline.py`, `tests/test_pipeline.py`
- **Do:** Implement `run_poll` per LLD §8: start run → build profile vector → for each
  enabled source `fetch` (wrapped in a **bulkhead** try/except so one failure can't abort
  the run) → normalize → filter → embed+score eligible/new → upsert → record per-source
  counts → `prune` → finish run. Return a `RunSummary`.
- **Done when:** end-to-end over Greenhouse+Lever fixtures stores ranked, scored,
  eligible jobs; a source raising is isolated (others still complete, error recorded);
  re-running is idempotent (no duplicate rows). New-since-last-poll derivable.

---

## Phase 5 — Dashboard (the data out)

### T18 — Web API endpoints  **[P0]**  `[x] Complete`
- **Depends on:** T06, T17
- **Files:** `src/jobfinder/web/app.py`, `web/api.py`, `web/schemas.py`, `tests/test_api.py`
- **Do:** FastAPI app (factory) binding loopback; implement `/api/jobs` (filters+sort
  per LLD §9.1/§9.2, `best`|`newest`, `NULLS LAST`, `include_ineligible` default false),
  `/api/jobs/{id}`, `POST /api/jobs/{id}/status`, `GET /api/runs/latest`. Use FastAPI
  TestClient over a seeded temp DB.
- **Done when:** filter/sort params return expected subsets/orders; status POST persists
  across a fresh client; ineligible hidden unless toggled; detail returns breakdown.

### T19 — Manual poll trigger endpoint  **[P0]**  `[x] Complete`
- **Depends on:** T17, T18
- **Files:** `web/api.py` (`POST /api/poll`), `tests/test_api.py`
- **Do:** `POST /api/poll` spawns the pipeline as a non-blocking subprocess, returns
  `202 {run_id}`. (In tests, patch the spawn to assert it's invoked, not run for real.)
- **Done when:** endpoint returns 202 and triggers the pipeline invocation (mocked in test).

### T20 — Frontend (static SPA)  **[P0]**  `[x] Complete`
- **Depends on:** T18
- **Files:** `web/static/index.html`, `app.js`, `styles.css`
- **Do:** Build the no-build-step page from LLD §9.3: ranked cards (score, title,
  company, location badge, prominent **"Xd ago"** age badge, matched-skill chips, NEW
  indicator, apply link), sidebar filters, best|newest sort toggle, status buttons that
  POST and optimistically update, a "Poll now" button hitting `/api/poll`.
- **Done when:** `jobfinder serve` (after a poll) renders ranked jobs at
  `http://127.0.0.1:8000`, filters/sort work, marking a job dismissed hides it and
  persists across restart. (Manual acceptance + the API tests from T18 back this.)

---

## Phase 6 — Remaining sources & discovery

### T21 — Ashby adapter  **[P1]**  `[x] Complete`
- **Depends on:** T08, T10
- **Files:** `src/jobfinder/sources/ashby.py`, `tests/fixtures/ashby_*.json`, `tests/test_sources.py`
- **Do:** Implement `fetch` per LLD §3.5 (`?includeCompensation=true`), map verified
  fields incl. `workplaceType` → strong remote signal and compensation summary; recency
  pre-filter.
- **Done when:** fixture parses; remote `workplaceType` sets `is_remote`; stale dropped.

### T22 — Adzuna aggregator (optional, keyed)  **[P1]**  `[x] Complete`
- **Depends on:** T08
- **Files:** `src/jobfinder/sources/adzuna.py`, `tests/fixtures/adzuna_*.json`, `tests/test_sources.py`
- **Do:** Implement `fetch` per LLD §3.6 against `ca/search`, passing
  `max_days_old=max_age_days` (source-side recency), `what`/`where`/`category` from
  config; hard throttle + aggressive cache; **skip cleanly when keys absent.**
- **Done when:** with no keys → empty result, no error; with fixture (keys faked) →
  parses Canadian backend postings.

### T23 — Board-token discovery  **[P1]**  `[x] Complete`
- **Depends on:** T22, T06
- **Files:** `src/jobfinder/discovery.py`, `tests/test_discovery.py`
- **Do:** Implement `harvest_tokens`: scan aggregator URLs for
  `boards.greenhouse.io/{token}`, `jobs.lever.co/{site}`, `jobs.ashbyhq.com/{board}`;
  append **unverified** entries to `companies.yaml`/companies table (dedup). Wire into
  pipeline after fetch.
- **Done when:** given fixture URLs, extracts correct tokens, dedups against existing,
  marks `verified=false`.

---

## Phase 7 — CLI, polish, release

### T24 — CLI commands  **[P0]**  `[x] Complete`
- **Depends on:** T17, T18, T06
- **Files:** `src/jobfinder/cli.py`, `tests/test_cli.py`
- **Do:** Implement typer commands per LLD §10: `poll` (`--no-cache`, `--source`),
  `serve` (`--host/--port`), `add-company`, `export` (`--csv`), `init` (scaffold config,
  create `data/`, run DDL). Each validates settings first (fail-fast).
- **Done when:** `jobfinder init` produces a runnable config tree; `--help` documents all
  commands; `add-company` writes a verified entry; CLI tests pass via typer's runner.

### T25 — CSV export  **[P1]**  `[x] Complete`
- **Depends on:** T06, T24
- **Files:** `cli.py` (`export`), `tests/test_cli.py`
- **Do:** Implement `export --csv PATH [--min-score N] [--bucket ...]` dumping current
  ranked matches with key columns.
- **Done when:** export produces a CSV whose rows match a filtered DB query; header
  correct.

### T26 — Hardening pass  **[P1]**  `[x] Complete`
- **Depends on:** all P0 tasks
- **Files:** logging setup, defensive guards across adapters, `tests/*`
- **Do:** Add structured logging + `RotatingFileHandler` (LLD §12) with the per-source
  count funnel; ensure every adapter field access is guarded; confirm a network kill
  mid-poll leaves the DB consistent (idempotent re-run recovers).
- **Done when:** simulated mid-poll failure test leaves no partial/duplicate rows; logs
  show the `fetched→kept→eligible→scored` funnel per source.

### T27 — README & scheduling docs  **[P0]**  `[x] Complete`
- **Depends on:** T24
- **Files:** `README.md`
- **Do:** Document setup (`pip install -e .` → `jobfinder init` → add resume → edit
  `profile.yaml`/`companies.yaml` → `jobfinder poll` → `jobfinder serve`), the
  model-swap note (MiniLM↔mpnet), the **cron line and Task Scheduler equivalent**, the
  Adzuna-key optionality, and a list of any `# TODO verify` company tokens to confirm.
- **Done when:** a fresh clone, following only the README, reaches a running dashboard
  showing ranked Canadian/remote backend roles scored against the resume.

### T28 — Definition-of-Done verification  **[P0]**  `[x] Complete`
- **Depends on:** T01–T27
- **Files:** `PROGRESS.md` (final), full suite
- **Do:** Run the full `pytest` + `ruff`; perform the end-to-end manual run from a clean
  state; confirm spec §13 Definition of Done holds (clone → poll → serve shows ranked
  eligible fresh roles, filters + status + recency all working, fully local and free, no
  single source able to crash the run).
- **Done when:** all tests green, lint clean, the end-to-end DoD check passes, and
  `PROGRESS.md` shows every task complete. **Project is ready for use.**

---

## Phase 8 — M7 enhancements (post-v1)

*User-requested improvements on top of the shipped v1 (spec §15, M7; HLD §3.7; LLD §16).
Same Ralph rules: one task per iteration, fixtures-only tests, `pytest`+`ruff` gate.
T29/T30/T33 are independent of the Sheets tasks (T31/T32), so the remote-filter, Applied
tab and restyle land even before the Google credential is set up.*

### T29 — Tighten remote/Canada location filtering  **[P0]**  `[x] Complete`
- **Depends on:** T10 (done)
- **Files:** `src/jobfinder/normalize.py` (`bucket_location` + regexes), `tests/test_normalize.py`
- **Do:** Implement the LLD §4.1 (M7) rules: a remote posting that names **any** non-Canada
  country/region buckets `OTHER`, not just the old `us only|united states only|emea`
  phrasings. Replace `_REMOTE_NON_CANADA_RE` with a broad word-boundary matcher
  (`us|usa|u.s.|united states|emea|latam|apac|uk|europe|india|us-based|us only`), checked
  **after** a positive Canada signal (Canada/North-America/Canadian-city/`bc`/`on`) so a
  Canada cue still wins; a bare "Remote" with no country named stays `REMOTE`. Guard the
  matcher so it can't fire on Canadian-province tokens.
- **Done when:** "Remote — US", "Remote (United States)", "Remote, EMEA", "US-based",
  "Remote LATAM" → `OTHER`; "Remote - Canada", "Remote (North America)", bare "Remote",
  "Remote - Canada & US" → `REMOTE`. All existing normalize tests still pass; new cases
  added. `ruff` clean.

### T30 — Hide `applied` from default list + Applied-tab query  **[P0]**  `[x] Complete`
- **Depends on:** T18 (done), T06 (done)
- **Files:** `src/jobfinder/store.py` (`_job_where`, constants), `src/jobfinder/web/schemas.py`
  (`StatusResponse`), `tests/test_store.py`, `tests/test_api.py`
- **Do:** Extend the default-listing hide in `store._job_where` from `!= dismissed` to
  `NOT IN (dismissed, applied)` (add `_APPLIED_STATE` beside `_DISMISSED_STATE`, citing
  `models.Status` — no magic strings). An explicit `status=applied` still returns them (the
  **Applied** tab's query); `get_job_detail` keeps an applied job reachable. Add
  `sheet_synced: bool` to `StatusResponse` (default False; T32 sets it true on a real sync).
- **Done when:** the default `/api/jobs` total drops by one when a job is marked `applied`
  and the job is absent from the default list but returned under `status=applied`; persists
  across a fresh client; detail still resolves. Regression test added; existing status tests
  unaffected.

### T31 — Google Sheets sync client  **[P1]**  `[x] Complete`
- **Depends on:** T07 (done), T02 (done)
- **Files:** `src/jobfinder/sheets.py`, `tests/test_sheets.py`, `tests/fixtures/sheets_*.json`
- **Do:** Implement `sync_applied(job, *, settings, client=None, now=None) -> SyncResult`
  per LLD §16: gate on `settings.sheets_enabled` (skip cleanly when unconfigured); mint an
  OAuth2 token from the service-account key via `google-auth`; read the Link column for
  idempotency; on a new URL, append a row via `spreadsheets:batchUpdate`/`appendCells`
  writing Company/Position/Link values **and** the Response cell's **yellow**
  `backgroundColor` in one request (`SHEETS_APPLIED_RGB` constant). Reuse the existing
  `HttpClient`; bulkhead all network in try/except so it returns `error` rather than raising.
- **Done when:** with no creds → `skipped`, zero requests; with faked creds + `MockTransport`
  → builds the correct `appendCells` request (4 cells, yellow on Response) and returns
  `appended`; a Link already present returns `duplicate` (no append); a mocked 500 returns
  `error`, never raises. No real network. **Dep added (`uv add`):** `google-auth` (LLD §14).

### T32 — Wire Sheets sync into the status endpoint + config  **[P1]**  `[ ] Pending`
- **Depends on:** T31, T30
- **Files:** `src/jobfinder/settings.py`, `.env.example`, `.gitignore`,
  `src/jobfinder/web/api.py`, `tests/test_api.py`
- **Do:** Add the M7 settings (`google_sheets_credentials`, `job_tracker_sheet_id`,
  `job_tracker_sheet_gid`, `sheets_enabled` helper) + `.env.example` entries (LLD §11.3/4);
  gitignore the key file. In `POST /api/jobs/{id}/status`, after the authoritative
  `set_status`, call `sheets.sync_applied` **only when `state == applied`**, map its result
  to `StatusResponse.sheet_synced`, and surface any `error` in logs — never 500 the request.
- **Done when:** marking `applied` persists the status **and** invokes `sync_applied` once
  (patched in test — no network); a patched Sheets `error` still returns 200 with
  `sheet_synced=false`; non-`applied` states never call Sheets; unconfigured → status works,
  `sheet_synced=false`.

### T33 — Dashboard: All/Applied tabs + restyle  **[P0]**  `[x] Complete`
- **Depends on:** T30 (T32 optional — the `sheet_synced` note degrades gracefully)
- **Files:** `src/jobfinder/web/static/index.html`, `app.js`, `styles.css`, `tests/test_api.py`
- **Do:** Add a `role="tablist"` with **All** / **Applied** tabs (LLD §9.3): **All** queries
  with no `status` (backend hides applied+dismissed), **Applied** queries `status=applied`
  `sort=newest`. Extend `handleStatusClick` to optimistically remove a card from **All** on
  `applied` (reusing the dismiss-remove path) and reflect `sheet_synced` in a small note.
  Restyle `styles.css` only — tighter grid, refined type/badges/score chip, sticky tab bar,
  subtle elevation/hover — within the existing CSS-variable palette and the
  `:focus-visible` + text-on-every-badge a11y rules. No framework, no build step, no
  `innerHTML`, no `console.log`.
- **Done when:** `node --check app.js` clean; `GET /` still 200 with the new shell; the
  Applied tab lists only applied jobs and the All tab excludes them; marking a job applied
  removes it from All live. Manual acceptance + the T30/T32 API tests back this.

---

## Completed Tasks Log

| # | Date | Task | Files | Notes |
|---|------|------|-------|-------|
| 32 | 2026-06-09 | T31 Google Sheets sync client | src/jobfinder/sheets.py, src/jobfinder/sources/http.py, tests/test_sheets.py, tests/fixtures/sheets_metadata.json, tests/fixtures/sheets_values.json, pyproject.toml, uv.lock, spec/tasks.md | First of the M7 P1 Sheets pair (T31 client → T32 wire-in). New `sheets.sync_applied(job, *, settings, client=None) -> SyncResult` (LLD §16): the tool's only outbound **write**, targeting the user's **own** tracking sheet (`Company\|Position\|Response\|Link`), never an apply endpoint (§3 guardrail intact). Flow: **(1) gate** — `if not settings.sheets_enabled: return SyncResult("skipped", …)` with **zero network** (the Adzuna-key degradation pattern); **(2) token** — `_access_token(settings)` mints an OAuth2 bearer from the service-account key via `google-auth` (`service_account.Credentials.from_service_account_file(scopes=SHEETS_SCOPE)` → `creds.refresh(Request())`), the **one** thing google-auth does for us (HLD §3.7/D13 — the heavy `google-api-python-client` SDK deliberately avoided); creds cached per key-path in module `_creds_cache`, refreshed only when `not creds.valid`. Isolated as its own helper precisely so tests monkeypatch it to a fixed `"fake-token"` (no real key file, no token-exchange network, LLD §16.2). **(3) resolve worksheet** — `GET /v4/spreadsheets/{id}?fields=sheets.properties(sheetId,title,index)` (ttl_s=0, never cached) → `(sheetId, title)`: pick the sheet whose `sheetId == job_tracker_sheet_gid` when the gid is configured, else the first sheet. Needed because `appendCells` requires the **numeric** sheetId and the idempotency read needs the **title** for its A1 range. **(4) idempotency read** — `GET …/values/{title}!D:D` (Link column = D, `Company\|Position\|Response\|Link` order; ttl_s=0); if `job.url` is already a row → `return SyncResult("duplicate", …)`, **no append**. **(5) append** — one `POST …:batchUpdate` `appendCells` request writing 4 cells in column order: Company/Position as `userEnteredValue.stringValue`, **Response = blank, yellow `backgroundColor` only** (`SHEETS_APPLIED_RGB = {red:1,green:1,blue:0}` constant, the user's "applied, waiting" convention, spec §15), Link as stringValue; `fields="userEnteredValue,userEnteredFormat.backgroundColor"` so values + colour land atomically. **Bulkhead:** the whole network body is wrapped in `except (httpx.HTTPError, GoogleAuthError, OSError, ValueError, KeyError)` → `SyncResult("error", str(exc))`, **logged, never raised** — the authoritative status write has already committed (HLD §3.7/D14, LLD §16.1 step 5). `SyncResult{status,detail}` frozen dataclass; the API (T32) maps `status=="appended"` → `sheet_synced=True`, all else → False. **Reuses the existing shared `HttpClient`** for both REST calls — but it was GET-only, so added `HttpClient.post_json(url, *, json_body, headers)`: generalised the private `_request_with_retry` to take `method`/`json_body` (POST → `self._client.post(url, json=…)`), so POSTs flow through the same per-host **throttle + transient-error retry** but **bypass the cache** (a write has no idempotent-GET semantics); GET path byte-for-byte unchanged (existing 19 http/sheets tests green). A local `HttpClientProto` Protocol types the injected client (the get_json/post_json slice) so the module is fully mockable. Job is duck-typed via an `AppliedJob` Protocol (`company`/`title`/`url`) — both the domain `Job` and a stored row satisfy it, so T32 can pass either. **`settings.sheets_enabled`/`google_sheets_credentials`/`job_tracker_sheet_id`/`job_tracker_sheet_gid` are read but added to the real `Settings` in T32** (LLD §15 build order: T31 client → T32 settings/wire); T31's tests therefore drive the sync with a `SimpleNamespace` stand-in exposing exactly those attrs — keeping the task boundary clean and the module independently testable. 5 offline tests via `httpx.MockTransport` + the faked token (a `Recorder` handler routing metadata-GET / values-GET / batchUpdate-POST by method+path, recording every request): **unconfigured → `skipped` with `requests == []`** (zero network); **append builds the correct request** — `Authorization: Bearer fake-token`, `appendCells.sheetId==0`, the right `fields`, 4 values with Company/Position/Link stringValues and the Response cell carrying **no `userEnteredValue`** but `backgroundColor == SHEETS_APPLIED_RGB`; **existing Link → `duplicate`, no POST issued**; **gid=123456 selects the matching worksheet** (asserts `sheetId==123456` in the append body); **a mocked 500 on batchUpdate → `error`, never raises** (rides the retry then is caught). Fixtures `sheets_metadata.json` (two worksheets for the gid-selection test) + `sheets_values.json` (Link column with one existing URL for the duplicate test). **Dep added (`uv add google-auth>=2.29,<3.0`** → google-auth 2.53.0 + pyasn1 transitives; LLD §14 — the *only* M7 dep, Sheets REST reuses httpx). No network anywhere in the suite. CI green (279 tests, ruff + format clean). **T31 done; only T32 (wire `sync_applied` into `POST /api/jobs/{id}/status` + add the M7 settings/.env/.gitignore) remains — the final M7 task; after it the whole project T01–T33 is complete.** |
| 31 | 2026-06-09 | T33 Dashboard: All/Applied tabs + restyle | src/jobfinder/web/static/index.html, app.js, styles.css, tests/test_api.py, spec/tasks.md | Final M7 frontend task; the backend it relies on (T30 default-hide of applied+dismissed, `status=applied` query, `StatusResponse.sheet_synced`) already shipped, so this is static-assets-only — **no Python/API change**. **index.html:** added a `role="tablist"` (`aria-label="Job views"`) above the results list with two `role="tab"` buttons — **All** (`data-tab="all"`, `aria-selected="true"`) and **Applied** (`data-tab="applied"`) — plus a `#status-note` `role="status" aria-live="polite"` paragraph (hidden by default) for the non-error applied/Sheets confirmation. **app.js (LLD §9.3):** module-level `currentTab` (`TAB_ALL`/`TAB_APPLIED`). `buildQuery` branches on the tab — **Applied** forces `status=applied&sort=newest&include_ineligible=false` (ignores the sidebar filters); **All** reads the filter form as before (blank fields dropped → the backend's default already hides applied+dismissed, so All sends no `status`). New `handleTabClick` (event-delegated off `#tabs`, `handle`-prefixed) flips `aria-selected` across the tabs, clears the status note, and re-queries. `handleStatusClick` now (a) captures the POST result and, **on `applied`**, shows a `#status-note` reflecting `result.sheet_synced` ("added to tracking sheet." vs "tracking sheet not configured.") — clearing it for other states; (b) replaces the old dismissed-only card-removal with `isCardVisibleAfterStatus(state)`, a single predicate covering both views: on the **Applied** tab only `applied` stays (re-clicking Applied keeps the card; Interested/Dismissed remove it); on the **All** tab an explicit sidebar status filter keeps matching cards, else `applied`+`dismissed` are removed live (the §9.3 "removes it from All live", generalising the prior dismiss-remove). New `showStatusNote`/`clearStatusNote` helpers mirror the `showError`/`clearError` pair but target the non-error region. Still no `innerHTML` (all `createElement`/`textContent`), no `console.log`, same-origin only. **styles.css (restyle only — no markup/JS coupling):** sticky `.tabs` bar (`position:sticky; top:0`) with an underline-style active `.tab[aria-selected="true"]` (accent text + 2px accent bottom-border — text label is always present, so colour is never the sole signal); `.status-note` in the new `--accent-soft` tint; card refresh — `box-shadow` (new `--shadow`/`--shadow-hover` vars) with a subtle hover elevation + border-darken `transition`, rounder corners, tighter list `gap`; refined `.badge--score` chip (rounded-square, larger) and slightly heavier, tighter `.job-card__title`. Stays within the existing CSS-variable palette; the global `:focus-visible` ring already covers the new tab buttons (they're `<button>`s). **Test:** `test_api.py::test_serves_static_spa_with_all_applied_tabs` asserts `GET /` (200) serves the new shell carrying `role="tablist"` + both `data-tab` values; the existing T30 API tests (`test_applied_hidden_from_default_listing_and_shown_under_applied_tab`, the `sheet_synced` default) back the data contract the tabs consume. `node --check app.js` clean. No new deps. CI green (269 tests, ruff + format clean). **T33 done — all M7 P0 work (T29/T30/T33) complete; only the optional P1 Sheets pair (T31 client → T32 wire-in) remains.** |
| 30 | 2026-06-08 | T30 Hide `applied` from default list + Applied-tab query | src/jobfinder/store.py, src/jobfinder/web/schemas.py, tests/test_store.py, tests/test_api.py, spec/tasks.md | Second M7 task; mirrors the T28 dismissed-hide for `applied`. In `store._job_where` the default (no explicit `status` filter) branch now excludes **both** states: `COALESCE(st.state, :default_state) NOT IN (:dismissed_state, :applied_state)` (was `!= :dismissed_state`). Added module constant `_APPLIED_STATE = "applied"` beside the existing `_DEFAULT_STATE`/`_DISMISSED_STATE` (comment cites `models.Status.APPLIED` — the file's established literal+comment idiom; `Status` is TYPE_CHECKING-only, so no runtime import added). Because `query_jobs`/`count_jobs`/CSV export all share `_job_where`, the hide is inherited consistently. An explicit `status=applied` still returns applied jobs (the **Applied** tab's query, used with `sort=newest`), and `get_job_detail` (queries by id, not `_job_where`) keeps an applied job reachable in detail — both unchanged. `StatusResponse` gains `sheet_synced: bool = False` (LLD §9.1/§16): False for non-applied states / unconfigured sync / handled Sheets error; **T32** flips it True on a real append — the status write stays authoritative regardless. Updated the existing `test_status_post_persists_across_fresh_client` assertion from `{"ok": True}` to `{"ok": True, "sheet_synced": False}` (the new default field). New tests: `test_store.py::test_default_listing_hides_dismissed_and_applied_but_explicit_filter_returns_them` (three jobs — untouched/applied/dismissed; default `query_jobs`+`count_jobs` return only the untouched one; `JobFilters(status="applied")` returns just the applied) and `test_api.py::test_applied_hidden_from_default_listing_and_shown_under_applied_tab` (marking `applied` drops the default total by one + removes the card, `status=applied&sort=newest` returns it, detail + hide persist across a fresh `TestClient` over the same DB). No new deps. CI green (268 tests, ruff + format clean). **T30 done; remaining M7: T31/T32 (Sheets sync client + settings/wire, P1), T33 (Applied tab + restyle).** |
| 29 | 2026-06-08 | T29 Tighten remote/Canada location filtering | src/jobfinder/normalize.py, tests/test_normalize.py, spec/tasks.md | First M7 (post-v1) task. Replaced the narrow `_REMOTE_NON_CANADA_RE = /remote.*(us only\|united states only\|emea)/` with the broad word-boundary matcher `\bu\.s\.?a?\.?\|\b(?:us\|usa\|united states\|emea\|latam\|apac\|uk\|europe\|india)\b` (LLD §4.1.1b), so a remote posting naming **any** non-Canada region buckets `OTHER` (still remote), not just the three old phrasings. `bucket_location` now applies the §4.1 order explicitly: **1a** a positive Canada cue → `REMOTE` (new `_has_canada_signal` = `_CANADA_REMOTE_SIGNAL_RE` /canada\|north america\|anywhere/ **OR** the existing Vancouver/Toronto/other-Canada bucket regexes — reused, no dup patterns); **1b** else a named non-Canada token → `OTHER`; **1c** else (no country named) → `REMOTE`. **Canada checked first** so a Canada cue wins when both co-occur — "Remote - Canada & US" → `REMOTE`. Word-boundary anchoring guards against mid-word hits ("uk" in "Ukraine") and the matcher contains no Canadian province codes (bc/on/…), so the §4.1 "guard against matching provinces" holds via both the regex shape and the 1a-first order. The separate `\bu\.s\.?a?\.?` branch (only a leading `\b`, ends in punctuation) handles the dotted "U.S." form that a trailing `\b` would reject. Tests: extended the `bucket_location` parametrize with the eight T29 "Done when" cases — "Remote — US"/"Remote (United States)"/"Remote, EMEA"/"US-based — Remote"/"Remote LATAM"/"Remote, APAC"/"Remote (UK)"/"Remote - India" → `OTHER`; "Remote - Canada"/"Remote (North America)" → `REMOTE`; bare "US-based" (no remote signal) → `OTHER` via the final fallthrough — plus a dedicated `test_bucket_location_canada_signal_wins_over_stray_non_canada_token` for "Remote - Canada & US" → `REMOTE`. All pre-existing normalize cases ("Remote (US only)"/"Remote - EMEA" → OTHER, "Remote, Canada"/bare "Remote" → REMOTE, "Remote, Vancouver" remote-wins-over-city) still pass unchanged. No new deps (stdlib `re`). CI green (266 tests, ruff + format clean). **T29 done; remaining M7: T30 (applied-hide + Applied-tab), T31/T32 (Sheets sync, P1), T33 (Applied tab + restyle).** |
| 28 | 2026-06-06 | T28 Definition-of-Done verification | src/jobfinder/store.py, tests/test_api.py, PROGRESS.md, spec/tasks.md | **Final P0 task — project complete.** Ran full local CI (`ruff format --check` + `ruff check` clean; **231 pytest green**) and a real end-to-end DoD run (spec §13) from a **fresh `git clone`** to a temp base_dir: `jobfinder init` scaffolded `config/` from the committed examples + created `data/` + ran the DDL; dropped in a résumé (`config/resume.md`, `profile.yaml` repointed); `jobfinder poll` (live network — the sanctioned manual-acceptance path, not a test); `jobfinder serve` on loopback. **Live poll funnel — Ashby/Wealthsimple `fetched=29 → kept_after_recency=17 → eligible=4 → scored=4`**: real Canadian/remote backend roles ranked against the résumé (top 52.1, remote senior Java/Kotlin). **Cost & Safety invariants confirmed live:** Adzuna **skipped cleanly with no keys** (zero requests, info note); `serve` bound `127.0.0.1` (loopback only, §5); no apply-path exercised. **Per-source bulkhead confirmed live (the §13 "no single source can crash the run"):** the unverified placeholder greenhouse (`shopify`/`benevity`/`clio`) and lever (`jobber`/`thinkific`) seed tokens **all 404'd**, each isolated + recorded in `errors`, and the poll still completed and persisted the Ashby results. **Dashboard verified via the running server:** `GET /` → 200 HTML shell; `/api/jobs?sort=best` ranked by `final DESC` (52.1/50.3/44.3/41.4), `sort=newest` by `posted_at` (age 7→10→16→17); matched-skill chips ({java,kotlin,python}); `age_days` recency surfaced; **status POST → 200 persists across a full server restart**; `/api/runs/latest` returns the per-source funnel incl. the isolated 404 errors. CSV export (`jobfinder export --csv`) wrote the 3 ranked rows with full columns, dismissed excluded. **Defect found *and fixed* during verification (DoD did not hold before):** the default `/api/jobs` listing did **not** hide dismissed jobs — spec §7 ("eligible ⇒ not already marked `dismissed`") and §13 ("dismissing hides it and persists across restart") — so a dismissed job reappeared on reload (live: default list stayed total=4 after a dismiss+restart). Fix in `store._job_where`: when no explicit `status` filter is given, add `COALESCE(st.state, :default_state) != :dismissed_state` (new module constants `_DEFAULT_STATE`/`_DISMISSED_STATE`, citing models.Status — no magic strings); an explicit `status=dismissed` still surfaces them so the user can review/undo, and `get_job_detail` (queries by id, not `_job_where`) keeps a dismissed job reachable in detail. Shared `_job_where` means `query_jobs`/`count_jobs`/CSV export all inherit the hide consistently. Added regression test `test_dismissed_hidden_from_default_listing` (dismiss drops the default total by one, hidden job absent, still returned under `status=dismissed`); existing explicit-filter test unaffected. **Re-verified live after the fix:** dismissing a job dropped the default best list **4→3** and the hide **persisted across a server restart**, while `status=dismissed` still returned it. Updated `PROGRESS.md` (milestone table M4/M5/M6 → done, added the T13/T21–T28 task-log rows + the live token-verification note: ashby `wealthsimple` resolves, the greenhouse/lever seeds 404 and need real slugs). No new deps. CI green (231 tests, ruff + format clean). **All tasks T01–T28 complete; the project meets spec §13 Definition of Done — fresh clone → poll → serve shows ranked, eligible, fresh Canadian/remote backend roles scored against the résumé, filters + status + recency working, fully local and free, no single source able to crash a poll.** |
| 27 | 2026-06-06 | T26 Hardening pass | src/jobfinder/logging_setup.py, src/jobfinder/cli.py, src/jobfinder/pipeline.py, tests/test_logging_setup.py, tests/test_pipeline.py, spec/tasks.md | New `logging_setup.setup_logging(settings, *, level=INFO)` (LLD §12): configures the **root** logger with a console `StreamHandler` **and** a `RotatingFileHandler` over `settings.log_dir/jobfinder.log` sized `_MAX_BYTES=1_000_000` × `_BACKUP_COUNT=5` (the LLD §12 "5 × 1 MB"), both using a `_JsonFormatter` that emits one JSON object per record (`ts`/`level`/`logger`/`message`, plus `exc` when `record.exc_info` is set so a bulkhead `log.exception` is fully captured) — the §12 "JSON-ish line", greppable + machine-parseable. **Idempotent:** each installed handler is tagged with a `_MANAGED` attribute; a repeat call removes+closes exactly its own prior handlers before re-adding, so successive CLI invocations / the spawned poll process never stack duplicate lines (and pytest's `caplog` handler is left untouched). Creates `log_dir` (mkdir parents) on setup. **Wiring:** replaced the three bare `logging.basicConfig(level=INFO)` calls — `cli.poll`, `cli.serve` (now also logs to file), and `pipeline.main` — with `setup_logging(settings)` (built/validated settings first so the log path resolves under `base_dir`); the per-source funnel `fetched→kept_after_recency→eligible→scored` line already emitted by `pipeline._poll_source` (LLD §12) now lands in the rotating file too. **Adapter guards (the task's "ensure every adapter field access is guarded"):** verified — every greenhouse/lever/ashby/adzuna payload read already uses `.get`/`isinstance` (the only `[...]` subscripts in the source files are writes to the local Adzuna `params` dict, not external-payload reads), so no adapter change was needed; recorded here rather than touching working code. **Mid-poll crash consistency (the Done-when "no partial/duplicate rows"):** new `_KillSource` raises `KeyboardInterrupt` (a `BaseException` the §8 bulkhead deliberately does **not** catch — it catches only `Exception` for provider errors), simulating a process kill *after* an earlier source has persisted its jobs. `test_run_poll_crash_mid_poll_leaves_db_consistent_and_recovers`: the interrupt propagates out of `run_poll`, but because every store write commits per-job, the killed poll leaves greenhouse's 2 jobs **committed whole** (scored + embedded) and its `poll_runs` row **unfinished** (`latest_run` is `None` — the dashboard sees no "latest" poll); a clean re-run then recovers — exactly 4 rows (2 greenhouse upserted idempotently, re-embed skipped since content unchanged → `scored==0`; 2 lever new → `scored==2`), all scored, and the recovery run finishes. Plus `test_run_poll_logs_the_funnel_per_source` (caplog asserts the `greenhouse funnel: fetched=4 kept=2 eligible=2 scored=2` INFO line) and 3 `test_logging_setup.py` tests (rotating handler installed with the right maxBytes/backupCount/path + log_dir created; a record lands in the file as parseable JSON with the funnel message; three setup calls → still exactly 2 handlers + a single log line, proving idempotency). No new deps (stdlib `logging.handlers`/`json`). CI green (230 tests, ruff + format clean). **Only T28 (P0 DoD verification) remains.** |
| 26 | 2026-06-06 | T25 CSV export filters | src/jobfinder/cli.py, tests/test_cli.py, spec/tasks.md | Completed the `export` command (T24 shipped `--csv` only) with the two LLD §10 filters `--min-score N` and repeatable `--bucket`. **`--min-score`** threads straight into the store's `JobFilters(min_score=...)` so the `s.final >= :min_score` gate runs in SQL (final is the 0–100 score — the right scalar for the existing single-valued filter); unscored/ineligible jobs are already excluded by `JobFilters.include_ineligible=False` (export shows the current ranked, eligible matches, spec §8). **`--bucket`** is repeatable (LLD §10 writes it `--bucket ...`), which the single-valued `JobFilters.bucket` used by the dashboard `<select>` can't express — rather than add a parallel multi-bucket field to the shared store contract (duplication, and a second consumer for the API to ignore), the rows are filtered by set membership **after** `query_jobs` returns them in `best` order: `[row for row in rows if row["location_bucket"] in buckets]`. A docstring + the existing `_VALID_BUCKETS` comment record the deliberate DB-vs-Python split. **`_validate_buckets`** fails fast (exit 1, `_fail`) on a typo'd bucket — mirroring `add-company`'s ATS validation — listing the accepted values from the new module-scope `_VALID_BUCKETS = tuple(b.value for b in LocationBucket)` (LLD §4.1; no magic strings). No change to `_write_csv`/`_export_row`/`_EXPORT_COLUMNS` (header unchanged, the task's "header correct"). 3 offline tests added (typer `CliRunner` over a store-seeded temp DB, no network/model, via a `_seed_scored_job` helper + a `_export_company_ids` CSV reader keyed on the `url` column): `--min-score 75` drops the 40-scored job keeping the 90; `--bucket remote --bucket toronto` returns exactly those two buckets (Vancouver excluded — proves repeatable + set membership); unknown `--bucket mars` exits 1 with "unknown bucket". No new deps (stdlib `csv`/`io` already used; reuses `JobFilters`/`query_jobs`). CI green (225 tests, ruff + format clean). **T25 done; remaining: T26 (hardening, P1) then T28 (P0 DoD verification).** |
| 25 | 2026-06-05 | T23 Board-token discovery | src/jobfinder/discovery.py, src/jobfinder/pipeline.py, src/jobfinder/settings.py, src/jobfinder/cli.py, tests/test_discovery.py, spec/tasks.md | `discovery.harvest_tokens` (LLD §3.6): scans a batch of polled URLs for ATS board references and appends any **previously unknown** board to `companies.yaml` as an **unverified** `CompanyEntry`, so a later poll can pick it up once a human verifies it (spec §5: discovery only *suggests*, never auto-verifies). Two layers: pure `extract_tokens(urls) -> dict[ats, set[token]]` (the testable regex core) and the I/O `harvest_tokens`. Patterns (`_PATTERNS`, LLD §3.6) capture the first path segment after the host — `boards\.greenhouse\.io/(token)`, `jobs\.lever\.co/(site)`, `jobs\.ashbyhq\.com/(board)`, token charset `[A-Za-z0-9_-]+`. **Two deliberate false-positive guards:** the literal-dot `boards\.greenhouse\.io` does **not** match the API host `boards-api.greenhouse.io` (so a `…/v1/boards/…` URL never yields "v1"), and `_RESERVED_SEGMENTS={"embed"}` drops the `boards.greenhouse.io/embed/job_board` route. `harvest_tokens(urls, *, companies_path, conn=None, now=None)`: loads the existing `companies.yaml` (absent → empty `CompaniesConfig`, never raises), dedups discovered tokens against the per-ATS known set, appends new ones `verified=False`, writes the file back, and — when a `conn` is given — also records each new `(ats, token)` in the `companies` **table** via `store.add_company` (the persistent ledger whose docstring already cited T23; `ON CONFLICT DO NOTHING` dedups + never downgrades a verified row). Returns the list of newly added `(ats, token)` pairs (empty when nothing new), and early-returns before any file write when no tokens or no *new* tokens are found (so a no-discovery poll leaves no `companies.yaml` side effect). **DRY:** extracted the `companies.yaml` serializer into `settings.save_companies(path, config)` (companion to `load_companies`, mkdirs the parent, dumps the three ATS lists in model-field order); discovery uses it and the CLI's `add-company` now delegates to it — deleting cli's duplicate `_write_companies` (and its now-unused `yaml` import). **Pipeline wiring (LLD §8 order — after the fetch loop, before prune):** `run_poll` threads a `discovered_urls: list[str]` accumulator through `_poll_source`/`_process_posting`, which appends each normalized `Job.url` (uniform across sources — greenhouse `absolute_url`, lever `hostedUrl`, ashby `jobUrl`, **adzuna `redirect_url`**, the §3.6 named target), then calls `harvest_tokens(discovered_urls, companies_path=config_dir/'companies.yaml', conn=conn, now=now)`. New `RunSummary.discovered: int` (default 0, so existing constructions are unaffected) surfaced by the CLI poll funnel as "Discovered N new board token(s)". No network anywhere — discovery only reads URLs the poll already fetched (Cost & Safety §3). 6 offline tests: `extract_tokens` finds all three providers + dedups a repeated token + excludes the API host and `embed` route; empty-on-no-matches; `harvest` writes all-new as unverified; dedups against an existing **verified** entry (acme preserved verified+named, only newco added, no dup row); no-matches writes no file; `conn` path records `verified=0` in the companies table **and** the YAML. No new deps (stdlib `re`). CI green (222 tests, ruff + format clean). **T23 done; remaining: T25 (export filters), T26 (hardening) — both P1 — then T28 (P0 DoD verification).** |
| 24 | 2026-06-05 | T22 Adzuna aggregator (optional, keyed) | src/jobfinder/sources/adzuna.py, src/jobfinder/normalize.py, src/jobfinder/settings.py, src/jobfinder/sources/__init__.py, .env.example, tests/fixtures/adzuna_search.json, tests/test_sources.py, spec/tasks.md | `AdzunaSource` (LLD §3.6): the one **optional keyed** source. `fetch` first checks credentials — **missing `app_id`/`app_key` → empty `SourceResult` + an info note, never a request, never a raise** (HLD §5.1, spec §5: "skip cleanly when keys absent"); the adapter is still *constructed* without keys (base.py contract). With keys it queries `GET https://api.adzuna.com/v1/api/jobs/ca/search/{page}` through the shared `HttpClient`, params = `app_id`/`app_key`/`what` (Settings default "backend software engineer") + `results_per_page` + **`max_days_old=max_age_days`** (source-side recency, the LLD §3.6 primary filter), with optional `where`/`category` added only when configured. Pagination walks the `{page}` path segment, **continuing while a page is full (`len==results_per_page`) and stopping on the first short/empty page**, hard-capped at `ADZUNA_MAX_PAGES=3` (free-tier guard — "throttle hard, cache aggressively": the shared per-host throttle + 6h on-disk cache cover politeness, LLD §3.2). **Client-side recency backstop** mirrors the ATS adapters so the `fetched→kept_after_recency` funnel (LLD §12) is reported and nothing stale slips through even though Adzuna also filters server-side: `parse_date(created,"adzuna")` (ISO8601 → UTC, `adzuna ∉ EPOCH_MS_SOURCES`), `(now-created).days > max_age_days` dropped, `created` absent → kept + flagged date_unknown (spec §7). Single-host bulkhead: an `httpx.HTTPError`/`json.JSONDecodeError` (or non-`results`-list shape) on a page is logged + appended to `errors` and stops paging **without losing pages already gathered**; non-object/id-less postings skipped+noted but counted in `fetched`; `now` injectable; all field access `.get`/`isinstance`-guarded. **Config (the "how" the LLD §3.6 leaves open):** added `adzuna_what`/`adzuna_where`/`adzuna_category` to `Settings` (env aliases `ADZUNA_WHAT`/`WHERE`/`CATEGORY`, sensible Canadian-backend defaults) so the whole optional source — keys *and* query — is configured in one place (`.env`), no new config file; `.env.example` documents them as commented optionals. `build_adzuna_source(settings)` threads those through + the default client and `register_source`s on import; `sources/__init__` now imports `adzuna` alongside greenhouse/lever/ashby (so `poll --source adzuna` and the default all-sources poll both pick it up). **normalize:** new `_extract_adzuna` (registered in `_EXTRACTORS`) — `title`, `description` via `html_to_text` (Adzuna snippets carry HTML/entities), **company from the in-payload `company.display_name`** (unlike Lever/Ashby, Adzuna ships the name, so no `company_hint` needed — adapter passes `company_hint=None`), `location_raw` from `location.display_name`, `is_remote=False` (text decides), `url` from `redirect_url` (also the field discovery T23 will scan for board tokens). Fixture has fresh-remote-Canada/stale-April/date-unknown-Vancouver/id-less rows. 7 offline tests via `httpx.MockTransport` (no network, no keys real): no-keys clean skip (handler asserts zero requests); fixture parse (fetched 4, id-less noted); recency drop = {ad-1001 fresh, ad-1003 date-unknown}; normalize round-trip (REMOTE bucket + HTML-stripped Java/AWS body + in-payload company; Vancouver date-unknown); **pagination walks page1(full)→page2(short) then stops** (call list asserted); page-500 isolated (no raise, noted); non-`results`-list shape noted. No new deps (httpx already pinned). CI green (216 tests, ruff + format clean). **T22 unblocks T23 (board-token discovery); remaining: T23, T25, T26 (P1) and T28 (P0 DoD verification).** |
| 23 | 2026-06-05 | T21 Ashby adapter | src/jobfinder/sources/ashby.py, src/jobfinder/normalize.py, src/jobfinder/sources/__init__.py, tests/fixtures/ashby_jobs.json, tests/test_sources.py, spec/tasks.md | `AshbySource` (LLD §3.5): per configured Ashby board token hits `GET https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true` (no auth) through the shared `HttpClient`, emitting `RawPosting`s with the verbatim payload (incl. the optional `compensation.scrapeableCompensationSalarySummary`). Ashby returns `{"jobs":[...]}` in one response (no pagination), so the adapter is structurally the Greenhouse twin: **recency gate pre-normalize** (no server-side date filter) — `posted_at` = `publishedAt` **or** `updatedAt` (whichever present, ISO8601 → UTC via `parse_date(..., "ashby")`, ISO path since `ashby ∉ EPOCH_MS_SOURCES`); `(now-posted_at).days > max_age_days` dropped, both-null → kept + flagged date_unknown (spec §7); `fetched` counts all, `kept_after_recency` survivors (LLD §12). Per-board bulkhead (`httpx.HTTPError`/`json.JSONDecodeError`/non-dict/no-jobs-list logged + appended to `errors`, only that board abandoned); non-object/id-less postings skipped+noted but still counted; `now` injectable; all field access `.get`/`isinstance`-guarded. Factory `build_ashby_source` loads `companies.yaml`'s ashby list + the default client and `register_source`s on import; `sources/__init__` now imports `ashby` alongside greenhouse/lever (re-exported). **No duplicated date logic:** the `publishedAt`-or-`updatedAt` selection is extracted into `normalize.ashby_posted_value(payload)`, shared by the adapter's recency pre-filter **and** the new `_extract_ashby` extractor. `_extract_ashby` (registered in `_EXTRACTORS`): `descriptionPlain` → `descriptionHtml`-stripped fallback, company from `company_hint` (Ashby payload carries no company name), `location_raw` from `location`, **`is_remote = workplaceType == "Remote"`** (the strong remote signal, OR-ed with text by `bucket_location`), `url` from `jobUrl`. Compensation is **not** a `Job` field (the §2 model has none — same as Lever's optional salary) so it rides along only in `raw`. Fixture has fresh/stale(Jan `updatedAt`, null `publishedAt`)/date-unknown(both null, `descriptionHtml`-only)/id-less rows. 5 offline tests via `httpx.MockTransport` (parse + fetched/error counts; recency drop = {fresh,date-unknown}; **`workplaceType:Remote` → `is_remote=True`+bucket REMOTE**, Hybrid → False, descriptionHtml fallback, RawPosting→`normalize` round-trip incl. company-hint + publishedAt date; one-board-404 isolated; non-`jobs`-list shape noted). No new deps (httpx already pinned). CI green (209 tests, ruff + format clean). **First P1 source done; T22 Adzuna (which unblocks T23 discovery) and T25/T26 polish remain before T28.** |
| 22 | 2026-06-05 | T27 README & scheduling docs | README.md, src/jobfinder/__main__.py, tests/test_cli.py, spec/tasks.md | Rewrote the stub `README.md` into the full setup-to-dashboard guide the DoD (spec §13) requires, sourced from the live code/config rather than restated from spec: the §13 guardrails (no auto-apply, $0, loopback-only, recency cutoff, gitignored résumé/.env/data); **Requirements** (Python 3.11+, one-time ~300 MB model download cached offline); **Quick start** (`pip install -e .` → `jobfinder init` → drop in `config/resume.pdf` → edit `profile.yaml`/`companies.yaml` → `jobfinder poll` → `jobfinder serve` at 127.0.0.1:8000), noting init copies examples and never clobbers (matches cli.py `_INIT_CONFIG_PAIRS`) and a `uv`/`uv run` alternative. **Configuration** documents all four files from the actual `*.example`/`settings.py` field set — profile keys table, the weights defaults (0.35/0.30/0.20/0.15, ≥1 must be >0), the companies-token URL patterns per ATS, and `.env` Adzuna optionality (both keys required, skips cleanly when absent). **Model swap** MiniLM↔mpnet via `embed_model` (re-poll to re-embed). **CLI reference** table for all five commands incl. `--no-cache`/`--source`/`--csv`. **Scheduling** — the task's required cron line **and** Task Scheduler equivalent (`Register-ScheduledTask` + GUI steps), plus a launchd plist; all use the working-dir-anchored interpreter so `config/`+`data/` resolve. **⚠️ Verify the starter seeds** table lists every `# TODO verify` token from `companies.yaml.example` (shopify/benevity/clio/jobber/thinkific/wealthsimple) with how to confirm a token against the live feed — satisfies the task's "list any `# TODO verify` company tokens" clause. **Cross-task change (justified — the cron/launchd/Windows examples all invoke `python -m jobfinder poll`, which raised "package cannot be directly executed" with no `__main__.py`):** added a 2-line `src/jobfinder/__main__.py` delegating to the Typer `app`, so the documented `python -m` form (explicit interpreter, no PATH lookup — the robust scheduler idiom) actually works; documenting a broken command would violate RALPH's no-shortcuts rule. Also corrected the cron example's log redirect from `data/logs/cron.log` (not created until T26 logging) to `data/poll.log` (`data/` exists after init). 1 offline subprocess test (`python -m jobfinder --help` → exit 0, lists `poll`; no network, no model). No new deps. CI green (204 tests, ruff + format clean). **Only P0 task left is T28 (Definition-of-Done verification); P1 source/polish tasks T21–T23, T25, T26 remain optional off the minimal runnable path.** |
| 21 | 2026-06-05 | T24 CLI commands | src/jobfinder/cli.py, tests/test_cli.py, spec/tasks.md | Five typer commands (LLD §10) on the existing `app`, each fail-fast-validating settings first. `_validated_settings(require_config=True)` builds `Settings()` and (for poll/serve) loads `profile.yaml`+`weights.yaml`, mapping `FileNotFoundError`/`ValidationError`/`ValueError` to a clean "run `jobfinder init` first" exit 1 (`_fail` → stderr + `typer.Exit(1)`); `require_config=False` for the pre-config commands (`init`/`add-company`/`export`). **poll** (`--no-cache`, repeatable `--source`): `--no-cache` installs a bypass `HttpClient` via `configure_default_client` *before* sources are built; `--source` routes through `build_sources(settings, only=…)`, else `run_poll` builds defaults; prints the per-source `fetched/kept/eligible/scored` funnel + prune count (`_echo_summary`, LLD §12), source errors shown as `ERROR …`. **serve** (`--host`/`--port`, default `127.0.0.1:8000`): `uvicorn.run(create_app(settings), …)` — loopback default (Cost & Safety §5), binding is uvicorn's job so the app factory stays transport-agnostic. **add-company** `ATS TOKEN [--name]`: rejects ATS outside greenhouse/lever/ashby (`_VALID_ATS`), loads-or-creates `companies.yaml`, dedupes on token (re-add promotes to `verified=True`, never downgrades), writes back via `model_dump`+`yaml.safe_dump`. **export** (`--csv PATH`, else stdout): `init_db` then `query_jobs(sort="best")`, writes `_EXPORT_COLUMNS` header + rows (`final` rounded or blank, remote yes/no, status COALESCEd to `new`) — T24 ships `--csv` only; the `--min-score`/`--bucket` filters are T25's scope. **init**: copies the committed `*.example` → target for the four config files (kept if present, never clobbered — Cost & Safety §4), `mkdir data/`, runs the DDL via `init_db`. `--help` documents all five. 14 offline tests added (typer `CliRunner`): init scaffolds+is idempotent; add-company writes/creates/promotes/rejects-unknown; poll invokes run_poll + funnel output, source-selection passes `only`, `--no-cache` installs the bypass client, missing-config fails fast; serve binds loopback (uvicorn patched); export writes CSV + stdout header. Heavy/transport seams (`run_poll`, `build_sources`, `configure_default_client`, `uvicorn.run`) patched so the suite stays offline + model-free. No new deps (typer/uvicorn/yaml already present). CI green (203 tests, ruff + format clean). **T25 (export filters) and T27 (README) now unblocked.** |
| 20 | 2026-06-05 | T20 Frontend (static SPA) | src/jobfinder/web/static/{index.html,app.js,styles.css}, src/jobfinder/web/app.py, tests/test_api.py | No-build-step vanilla dashboard (LLD §9.3). `index.html`: header with **Poll now** button + live `run-status`, a `role="alert"` region for surfaced errors, a sidebar `<form>` of labelled filters (sort best/newest, location bucket, source, seniority, status, min_score, max_age_days, include_ineligible checkbox) and a `<ul>` results list. `app.js` (`"use strict"`, no framework, **same-origin only**): `buildQuery()` reads the form into an `/api/jobs` query (drops blanks, sends the checkbox as an explicit bool); `loadJobs()` fetches + renders ranked cards built entirely via `document.createElement`/`textContent` (no `innerHTML` → job text can't inject markup), toggling `aria-busy` on the results region. Each card shows the rounded **score** badge (with `aria-label` "Match score N of 100"), title as an apply link (`target=_blank rel="noopener noreferrer"`, plain text when no url), company, a **location** badge + `remote` badge, the prominent **"Xd ago"** age badge (`ageText` → "Date unknown" when `date_unknown`/null), matched-skill chips, and a **NEW** badge when `is_new_since_last_poll`. Status buttons (Interested/Applied/Dismissed) sit in a `role="group"` with an `aria-label`; `handleStatusClick` (event-delegated off the list) POSTs `/api/jobs/{id}/status`, then **optimistically** flips `aria-pressed` on the sibling buttons and **removes a dismissed card** unless the dismissed filter is active — failures surface in the alert, never swallowed. `handlePollNow` POSTs `/api/poll`, disables the button while in-flight, shows the returned `run_id`; `loadRunStatus()` reads `/api/runs/latest` (treats 404 as "No polls yet"). All event handlers are `handle`-prefixed; **no `console.log`**. `styles.css`: plain CSS (no CSS-in-JS), badges/states always carry text (colour never the sole signal), `:focus-visible` ring retained on every control, responsive single-column under 720px. **app.py:** updated the two stale "once T20 lands"/"built in T20" comments to present tense; the existing existence-guarded `StaticFiles` mount at `/` (html=True) now activates because the assets exist — API routes still resolve since the router is included **before** the mount. 4 offline tests added to test_api.py: `GET /` serves the HTML shell ("Job Finder", `text/html`); `GET /app.js` (asserts it talks to `/api/jobs`) and `/styles.css` both 200. `node --check app.js` clean. No new deps (FastAPI's `StaticFiles` already present). CI green (189 tests, ruff + format clean). **All P0 dashboard tasks (T18–T20) done; T24 CLI `serve` wires uvicorn to host the app on loopback.** |
| 19 | 2026-06-05 | T19 Manual poll trigger endpoint | src/jobfinder/web/{api,schemas}.py, src/jobfinder/pipeline.py, tests/{test_api,test_pipeline}.py | `POST /api/poll` → **202** `PollResponse{run_id}` (LLD §9.1). The endpoint **reserves** the run row itself (`start_run` on the per-request `get_conn` connection — committed, so the child sees it under WAL), hands that `run_id` to a non-blocking spawn, and returns immediately so a slow/hanging source can never block the dashboard. `spawn_poll(settings, run_id)` (module-level in `web/api.py`, monkeypatchable) launches `subprocess.Popen([sys.executable, "-m", "jobfinder.pipeline", "--run-id", str(run_id)])` with `start_new_session=True` (detaches so it outlives a server restart; POSIX-only, ignored elsewhere), `stdout/stderr=DEVNULL`, and `env={**os.environ, "JOBFINDER_base_dir": str(settings.base_dir)}` so the child resolves the **same** DB/config the server uses (fixed argv, no shell, only the int run_id — not user input). This request's process never touches the network; the fetch happens out-of-process (Cost & Safety §1/§5). **Cross-task changes (justified, per the established precedent of a downstream task completing an upstream contract):** (1) `run_poll(..., run_id: int | None = None)` — when given, it **finishes the reserved row** instead of calling `start_run`, so exactly one `poll_runs` row exists per trigger (the §9.1 "return run_id then spawn" contract needs the id *before* the poll runs); default `None` preserves the T17 cron/CLI path unchanged. (2) `pipeline.main(argv)` + `__main__` so the module is **spawnable today** without the T24 typer CLI: `python -m jobfinder.pipeline [--run-id N]` builds `Settings()` from env and calls `run_poll`; `--run-id` finishes a reserved row, omitting it opens a fresh run (bare cron path). `Settings` moved from the pipeline's `TYPE_CHECKING` block to a runtime import for `main`. New schema `PollResponse{run_id:int}`. 3 offline tests: poll endpoint returns 202 + int run_id, spawn patched (no subprocess/model/network), reserved run row exists **unfinished** (not yet the "latest" run) and the spawn got that same id + the right base_dir; `spawn_poll` builds the exact argv + `JOBFINDER_base_dir` env with `Popen` patched (no real process); `run_poll(run_id=reserved)` reuses the row (one run total, stamped finished). No new deps (stdlib subprocess/sys/os/argparse). CI green (187 tests, ruff + format clean). **T20 static frontend is next (the last P0 dashboard task).** |
| 18 | 2026-06-04 | T18 Web API endpoints | src/jobfinder/web/{__init__,app,schemas,api}.py, src/jobfinder/store.py, src/jobfinder/score.py, tests/test_api.py, pyproject.toml, requirements.txt | FastAPI app factory `create_app(settings=None, *, now=None)` (LLD §9): stashes settings + the validated `Profile` + an **injectable clock** on `app.state`, runs `init_db` on startup (serving before a poll yields an empty list, not an error), includes the `/api` router, and mounts `static/` **only if the dir exists** (guarded — the T20 SPA assets aren't built yet; API is fully usable without them). Loopback binding is the server's job (uvicorn host in the T24 `serve`), so the factory stays transport-agnostic and never calls out (Cost & Safety §5). `web/api.py` router (prefix `/api`): `GET /jobs` (filters `bucket/source/seniority/min_score/status/max_age_days/include_ineligible`, `sort∈{best,newest}` via `Literal`, `limit/offset`) → `JobListResponse{items,total}`; `GET /jobs/{id}` → `JobDetail` (full desc + `breakdown`), 404 if absent; `POST /jobs/{id}/status` body validated against the `Status` enum (unknown → 422), 404 if job absent; `GET /runs/latest` → latest finished run summary, 404 before any poll. Per-request DB conn via a `Depends(get_conn)` generator (opened from `settings.db_path`, closed after) so the dashboard never holds the DB open across the poll's writes (busy_timeout covers overlap, LLD §7.1). **Store additions (completing the LLD §7.3 `query_jobs` contract, deferred from T06):** `JobFilters` dataclass + `query_jobs`/`count_jobs` sharing one parameterized `_job_where` (no dup), `get_job_detail`, `latest_run`, `previous_run_finished_at`. List/detail SQL left-joins `scores` + `status` so unscored ineligible jobs and untouched jobs (status COALESCEs to `'new'`) still appear; `best`=`final DESC NULLS LAST, posted_at DESC NULLS LAST`, `newest` swaps the keys (LLD §9.2). `max_age_days`/`min_score` filters keep `date_unknown`/NULL-posted jobs visible (flagged, never silently dropped, spec §7). **new-since-last-poll** (`JobCard.is_new_since_last_poll`) compares `first_seen_at` to the **previous** finished run's `finished_at` (`previous_run_finished_at`, OFFSET 1) per LLD §7.3 — lexicographic UTC-ISO compare; no prior run ⇒ all new. **Reuse:** extracted public `score.matched_skills(text, skills)` (word-boundary, case-insensitive) shared by the scorer's skill component **and** the card's matched-skill chips; `_skill_score` now delegates to it. Ineligible (unscored) jobs surface `score=0.0` (faithful to LLD's `score: float`) and an empty `breakdown`. Deps: `fastapi`, `uvicorn[standard]` (LLD §14 target set; pinned to the §14 ranges, not uv's `>=current`); FastAPI's `Depends`/`Query`/`Path`/`Body` added to ruff `flake8-bugbear.extend-immutable-calls` (the call-in-default is the framework's intended idiom, not a B008 bug). 18 offline tests via FastAPI `TestClient` over a store-seeded temp DB (no model, no network): default hides ineligible + total; best/newest order; bucket/min_score/source filters; include_ineligible toggle (D surfaces, score 0.0); age_days + matched_skills {java,aws}; new-since-last-poll flags (A/C new, B/D not); detail desc+breakdown; ineligible empty breakdown; unknown-job 404; **status POST persists across a brand-new app/client**; status filter (dismissed shown / hidden from `new`); invalid-state 422; unknown-job-status 404; runs/latest payload; runs/latest 404 with no runs. CI green (184 tests, ruff + format clean). **T19 manual poll-trigger endpoint is next.** |
| 17 | 2026-06-04 | T17 Poll pipeline orchestration | src/jobfinder/pipeline.py, src/jobfinder/models.py, src/jobfinder/store.py, src/jobfinder/sources/{greenhouse,lever}.py, tests/test_pipeline.py | `run_poll(settings, *, sources=None, model=None, now=None) -> RunSummary` (LLD §8). Builds the profile vector once (`extract_resume(base_dir/profile.resume_path)` → `build_profile_vector`), opens the DB + a `poll_runs` row, then iterates the enabled sources (default `build_sources(settings)`; **injectable** so tests run offline with `httpx.MockTransport`-backed real adapters + the session `embed_model`). Each source runs inside a **bulkhead** (`try/except Exception` + `log.exception`, the one place RALPH sanctions a broad catch): a raising source records `summary.error` and the poll continues — verified by `_BoomSource` leaving the healthy source's jobs intact. Per posting: `normalize(raw, company_hint=raw.company_hint, …)` → `is_eligible` (sets `job.eligible`/`ineligible_reason`) → `content_hash = sha1(title\ndescription)`. **Eligible + new/changed** ⇒ `embed_job` + `score_job`; **eligible + unchanged re-see** ⇒ reuse the stored embedding blob and keep the prior score (skip re-embedding, LLD §6.4). Ineligible jobs are still upserted (flagged, never dropped, LLD §5). `upsert_job` runs **before** `save_score` so the scores→jobs FK (LLD §7.2) is satisfied (the §8 pseudocode's save-then-upsert order would violate the immediate FK). Closes with `prune(not_seen_days=settings.retention_days)` (operational setting per §8/§11.4; recency still uses `profile.max_age_days`) + `finish_run` storing the per-source `fetched→kept→eligible→scored` funnel JSON (LLD §12, also logged at INFO). Idempotent: re-poll upserts in place (`first_seen_at` preserved, `last_seen_at` bumped) so new-since-last-poll is derivable. **Cross-task changes (justified, per the T05/T13 precedent of a downstream task completing an upstream contract):** added `RawPosting.company_hint` (Lever payloads carry no company name — the only way to thread it from fetch to normalize) and populated it in both adapters (`company.name or company.token`); added `store.get_job(conn, id)` reader so the pipeline can check stored `content_hash`/`embedding` for the re-embed gate. `RunSummary`/`SourceSummary` dataclasses defined in pipeline.py (LLD §8 references `RunSummary` without a shape). `discovery.harvest_tokens` from the §8 pseudocode is **omitted** — `discovery.py` is T23 (P1), which explicitly wires itself into the pipeline later; not a dependency of T17. 5 offline tests (real model via fixture, MockTransport, zero network): end-to-end Greenhouse+Lever stores 4 ranked/scored/eligible jobs with a remote Java/AWS role on top out-scoring the Vancouver Python role; failing-source isolation; ineligible US-only role stored `eligible=0`/`location_out`/unscored; idempotent re-poll (no dup rows, `scored==0` on the unchanged second pass, embeddings preserved, `first_seen_at` kept + `last_seen_at` bumped); prune of unseen rows past retention. No new deps. CI green (166 tests, ruff + format clean). **M4 pipeline done; T18 web API is next.** |
| 16 | 2026-06-04 | T16 Scoring math & weights | src/jobfinder/score.py, config/weights.yaml.example, tests/test_score.py | `score_job(job, profile_vec, job_vec, *, profile, weights, now) -> ScoreBreakdown` (LLD §6.3–§6.4). Takes the two **pre-computed L2-normalized vectors** rather than re-embedding, so the function stays pure + model-free and the load-bearing ranking test is deterministic & offline (the §8 pipeline does the embed→score in one step; split here only for testability). Four components: `semantic = _clamp01(_cosine(profile_vec, job_vec))` (clamps the [-1,1] cosine to [0,1] per §6.3); `skill = _skill_score(title\\ndescription, must_have_skills)` = fraction of must-haves matched **word-boundary, case-insensitive** (`\\bjava\\b` so "java" ≠ "javascript"), saturating at 1.0; `location = _LOCATION_BONUS[bucket]` map {remote 1.0, vancouver 0.85, toronto 0.7, other_canada 0.4, other 0.0} (§6.3); `recency = _recency_score` linear `clamp(1 - age_days/max_age_days)` with `date_unknown`/`posted_at is None` → fixed `_DATE_UNKNOWN_RECENCY=0.3` so undated jobs still rank (spec §7). Final = weight-normalized sum `(Σ wᵢ·cᵢ)/(Σ wᵢ)` → `round(100·final01, 1)`; denominator guaranteed > 0 by the `Weights` validator (settings.py, T02). All component constants are module-scope `UPPER_SNAKE` citing §6.3. `weights.yaml.example` carries the §6.4 defaults (0.35/0.30/0.20/0.15). **Both load-bearing T16 tests pass:** `test_skill_weight_beats_higher_semantic_off_stack` — hand-built vectors give the off-stack role the *higher* cosine yet the Java/AWS role wins because the 0.30 skill weight flips `final`; `test_senior_remote_java_aws_outranks_junior_onsite_frontend` — full end-to-end through the **real model** (session `embed_model` fixture), senior remote Java/AWS outranks junior onsite frontend. Plus per-component tests (cosine clamp, skill word-boundary, location map, recency decay + date_unknown=0.3, full-breakdown values arithmetic-checked). **Recovery note:** the T16 code+tests were committed in a prior iteration under a mislabeled message (`00246d3`, "T01 …") with `tests/test_score.py` left **ruff-format-dirty** (CI red) and the task still `[~]`; this iteration reformatted the test file (whitespace-only), reran full CI green, and marked T16 `[x]`. No new deps. CI green (161 tests, ruff + format clean). **M3 scoring complete; T17 pipeline is next.** |
| 15 | 2026-06-04 | T15 Embeddings & profile vector | src/jobfinder/score.py, tests/conftest.py, tests/test_score.py | `load_model(name)` (LLD §6.1) caches `SentenceTransformer` instances in a module dict and **lazy-imports `sentence_transformers`/torch inside the call** so importing `score.py` (e.g. for résumé extraction) never pulls torch — preserving the T14 cheap-import property. `render_targeting(profile)` renders the role+must-have-skills+seniority block. `build_profile_vector(profile, resume_text, *, model)` (LLD §6.2): prepend targeting to the full résumé, split via the pure `_chunk_text` into ≤`_PROFILE_CHUNK_MAX_WORDS=180`-word windows (≈256 tokens — conservative so the model never truncates a chunk's tail; word-based keeps chunking pure + offline-testable without the tokenizer), `encode(chunks, normalize_embeddings=True)`, mean-pool, `_l2_normalize`. `embed_job(job, *, model)` (LLD §6.3): char-cap `title\ndescription` to `_JOB_CHAR_CAP=5000` then encode+normalize. Both accept an injected `Encoder` Protocol (SentenceTransformer-compatible) so the chunk/pool/normalize math is unit-tested **fully offline** with a deterministic recording fake, while dim(384)/unit-norm/determinism are asserted against the real model via a session-scoped `embed_model` fixture in new `tests/conftest.py`. The fixture's first-run model download is the **one sanctioned network touch** (tasks.md T15 carve-out); cached on disk after, offline thereafter, and reused by T16. `_l2_normalize` shared by both functions (no dup); returns the zero vector unchanged. 13 new tests: `_chunk_text` split/tail-preserved/empty, targeting block contents, long-résumé chunking asserted via the recording fake (one batched encode, >1 chunk, tail word present, pooled vec unit-norm), real-model dim+unit-norm, determinism ×2 (`array_equal`), `embed_job` unit-norm+determinism+char-cap on a megabyte description, `load_model` caches per name (monkeypatched ST, no download). Deps added per task (see above). CI green (145 tests, ruff clean). **M3 scoring continues in T16.** |
| 14 | 2026-06-04 | T14 Resume extraction | src/jobfinder/score.py, tests/fixtures/resume.{txt,md,docx,pdf}, tests/test_score.py | `extract_resume(path) -> str` (LLD §6.5): dispatches on the lowercased suffix — `.txt`/`.md` read UTF-8 directly; `.docx` via python-docx walking **paragraphs then table cells** in document order (LLD requires tables, not just paragraphs); `.pdf` via pypdf, **falling back to pdfplumber when pypdf yields no non-whitespace text** (empty/garbled layout). Missing file → `FileNotFoundError` with a clear message; unsupported suffix on an existing file → `ValueError` listing supported formats (fail-fast). Heavy extractors (pypdf/pdfplumber/docx) are **lazy-imported inside their branch** so importing `score.py` stays cheap and never pulls torch (the T15 sentence-transformers code will live in the same module but extraction must not trigger it). Suffix set is module-scope frozensets citing §6.5. **Fixtures generated once** by a throwaway script (not committed): a senior-backend CV carrying Java/Kotlin/Python/AWS so T15/T16 scoring tests reuse them; the `.pdf` is a hand-built minimal PDF-1.4 (text content stream + computed xref) so no PDF-writing dep was needed, and `.docx` includes a 1-row skills table to exercise the table-walk. Fixtures live under `tests/fixtures/resume.*` — **not** caught by the `config/resume.*` gitignore (verified `git check-ignore`). 8 offline tests: all 4 formats extract non-empty text containing every must-have skill, docx table cell surfaced, str-path accepted, pdfplumber fallback forced via monkeypatched empty-pypdf reader (recovers the real fixture), missing-file and unsupported-format sad paths. Deps added per task (see above). CI green (136 tests, ruff clean). |
| 13 | 2026-06-04 | T13 Eligibility filters | src/jobfinder/filters.py, src/jobfinder/normalize.py, tests/test_filters.py | `is_eligible(job, *, profile, now) -> (bool, reason)` (LLD §5): ordered cheapest-first gates that short-circuit before any embedding — (1) **recency** `(now-posted_at).days > max_age_days → "stale"`, with `posted_at is None` (date_unknown) passing by design so it's kept + ranked low, never silently dropped (spec §7); (2) **role-keyword** pre-check (`not _matches_role_keyword → "not_backend_role"`), case-insensitive substring of any `profile.role_keywords` over `title\ndescription` — gated behind `profile.role_keyword_required` (default True, so default behaviour == the LLD reference; the flag only lets a user disable the keyword pre-filter and lean on the semantic scorer); (3) **location** `bucket == OTHER → "location_out"`; (4) **seniority** `JUNIOR or is_people_manager(title) → "seniority_out"`. Reasons are module-scope `REASON_*` string constants persisted to `jobs.ineligible_reason`. **No duplicated regex:** people-manager detection extracted into `normalize.is_people_manager(title)` (reuses the existing `_MANAGER_RE`/`_IC_OVERRIDE_RE` from `infer_seniority`, LLD §4.2) — necessary because manager/director titles infer to `UNKNOWN` seniority which the filter otherwise keeps, so the manager gate must be explicit; a "Principal Engineer" IC override still passes. Pure function: no I/O, no global state. 11 offline tests: eligible passes; each reason fires (stale/non-backend/location_out/junior/people-manager); date_unknown passes recency; keyword matched via description; role gate skipped when `role_keyword_required=False`; staff-IC passes despite "Principal" in title; recency short-circuits before role gate. No new deps (stdlib). CI green (127 tests, ruff clean). |

## Dependency summary (critical path)
T01→T02/T03 → T04→T05→T06 (store) ; T07→T08 + T09→T10 (fetch/normalize) ;
T11/T12 (sources) + T13 + T14→T15→T16 (score) → **T17 (pipeline)** →
T18→T19→T20 (dashboard) → T24/T27/T28 (release).
P1 tasks (T21–T23, T25, T26) extend coverage/polish but are not on the minimal
runnable path — the product is usable after T20 + T24 + T27, and *complete* at T28.

**M7 (post-v1):** T29 (remote filter) ; T30 (applied-hide + Applied tab) → T33 (tabs +
restyle) ; T31 (sheets client) → T32 (wire into status endpoint). T29/T30/T33 are
independent of the Sheets pair (T31→T32), so the filter + Applied-tab + UI polish ship
even if the Google credential isn't configured yet.
