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

### T03 — Core data models  **[P0]**
- **Depends on:** T01
- **Files:** `src/jobfinder/models.py`, `tests/test_models.py`
- **Do:** Implement `RawPosting`, `Job`, `ScoreBreakdown`, and the `LocationBucket`,
  `Seniority`, `Status` enums per LLD §2. Implement the stable `Job.id` derivation
  `sha1(f"{source}:{source_id}")[:16]`.
- **Done when:** same `(source, source_id)` yields identical `id`; different inputs
  differ; enum round-trips to/from str. Tests assert id stability.

---

## Phase 1 — Persistence

### T04 — SQLite schema & connection  **[P0]**
- **Depends on:** T03
- **Files:** `src/jobfinder/store.py` (connect + DDL), `tests/test_store.py`
- **Do:** Implement connection with PRAGMAs (WAL, synchronous=NORMAL, busy_timeout=5000,
  foreign_keys=ON) and `init_db()` running the full DDL + indexes from LLD §7.2. Use an
  in-memory or temp-file DB in tests.
- **Done when:** `init_db()` creates all tables/indexes idempotently (safe to run twice);
  PRAGMAs verified via `PRAGMA` queries in a test.

### T05 — Job upsert & dedupe  **[P0]**
- **Depends on:** T04
- **Files:** `store.py` (`upsert_job`), `tests/test_store.py`
- **Do:** Implement `upsert_job` with `ON CONFLICT(source, source_id) DO UPDATE`,
  preserving `first_seen_at`, bumping `last_seen_at`, updating mutable fields,
  persisting `embedding` BLOB and `eligible`/`ineligible_reason`/`content_hash`.
- **Done when:** inserting the same job twice → exactly one row, `first_seen_at`
  unchanged, `last_seen_at` advanced. Test asserts this.

### T06 — Scores, status, runs, companies DAL  **[P0]**
- **Depends on:** T05
- **Files:** `store.py` (`save_score`, `set_status`, `start_run`/`finish_run`,
  company read/write, `prune`), `tests/test_store.py`
- **Do:** Implement the remaining operations from LLD §7.3, including `prune(not_seen_days)`
  and run bookkeeping. Cascade deletes via FK.
- **Done when:** saving a score then deleting its job cascades; `prune` removes only
  rows older than the cutoff; a run row records `started_at`/`finished_at`/`per_source_json`.

---

## Phase 2 — Fetch & normalize (the data in)

### T07 — Shared HTTP client (throttle, retry, cache)  **[P0]**
- **Depends on:** T02
- **Files:** `src/jobfinder/sources/http.py`, `tests/test_http.py`
- **Do:** Implement `get_json`/`get_text` per LLD §3.2: single `httpx.Client` with
  timeouts/http2/User-Agent, per-host throttle (≥`throttle_s`), retry on
  `{429,500,502,503,504}`+timeouts with backoff+jitter honoring `Retry-After`, on-disk
  cache keyed by sha1(url) with TTL, `--no-cache` bypass. Mock transport in tests
  (no real network).
- **Done when:** retry fires on a mocked 503 then succeeds; cache hit avoids a second
  transport call; throttle enforces min spacing (tested with a fake clock).

### T08 — Source protocol & registry  **[P0]**
- **Depends on:** T03, T07
- **Files:** `src/jobfinder/sources/base.py`, `tests/test_sources.py`
- **Do:** Define `Source` protocol, `SourceResult`, and a `SOURCES` registry keyed by
  name that constructs enabled adapters from settings. A source missing its required
  secret returns an empty `SourceResult` with a note rather than raising.
- **Done when:** registry yields only enabled sources; a secret-less optional source is
  constructible and returns empty cleanly.

### T09 — Normalizer: HTML, dates, helpers  **[P0]**
- **Depends on:** T03
- **Files:** `src/jobfinder/normalize.py` (`html_to_text`, `parse_date`), `tests/test_normalize.py`
- **Do:** Implement `html_to_text` (selectolax: drop script/style, get text, collapse
  whitespace, unescape entities) and `parse_date` handling ISO8601-with-offset → UTC and
  epoch-ms → UTC, failure → `None`. Per LLD §4.3.
- **Done when:** entity-laden HTML fixture → clean text; ISO and epoch-ms fixtures parse
  to correct UTC datetimes; garbage → `None`.

### T10 — Normalizer: location bucketing & seniority  **[P0]**
- **Depends on:** T09
- **Files:** `normalize.py` (`bucket_location`, `infer_seniority`, `normalize`),
  `tests/test_normalize.py`
- **Do:** Implement the ordered rules from LLD §4.1–§4.2 and the top-level
  `normalize(raw, company_hint, now) -> Job` that ties field extraction + helpers
  together and sets `date_unknown`.
- **Done when:** bucketing correct for remote-Canada, US-only-remote (→other), Vancouver,
  Toronto, other-Canada; seniority correct across junior/mid/senior/staff/manager/unknown
  titles. Tests cover each branch.

### T11 — Greenhouse adapter  **[P0]**
- **Depends on:** T08, T10
- **Files:** `src/jobfinder/sources/greenhouse.py`, `tests/fixtures/greenhouse_*.json`,
  `tests/test_sources.py`
- **Do:** Implement `fetch` hitting the LLD §3.3 endpoint (`?content=true`), mapping the
  verified fields, and **dropping postings older than `max_age_days` before returning**
  (no server-side filter). Guard every field access.
- **Done when:** parsing the committed fixture yields correct `RawPosting`s; a stale
  fixture row is excluded by the recency pre-filter; `kept_after_recency` reported.

### T12 — Lever adapter  **[P0]**
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

### T13 — Eligibility filters  **[P0]**
- **Depends on:** T10, T02
- **Files:** `src/jobfinder/filters.py`, `tests/test_filters.py`
- **Do:** Implement `is_eligible` per LLD §5: ordered cheapest-first gates (recency →
  role keyword → location → seniority/people-manager), returning `(bool, reason)`.
  `date_unknown` passes recency. Ineligible jobs are kept (flagged), not dropped.
- **Done when:** stale/non-backend/out-of-location/junior/manager each rejected with the
  correct reason; eligible role passes; `date_unknown` passes. Tests cover each.

### T14 — Resume extraction  **[P0]**
- **Depends on:** T02
- **Files:** `src/jobfinder/score.py` (`extract_resume`), `tests/fixtures/resume.*`,
  `tests/test_score.py`
- **Do:** Implement `extract_resume` per LLD §6.5: pdf (pypdf, pdfplumber fallback),
  docx (python-docx incl. tables), txt/md direct. Returns full text; clear error if missing.
- **Done when:** a committed sample resume in each supported format extracts non-empty
  text; missing file raises a clear error.

### T15 — Embeddings & profile vector  **[P0]**
- **Depends on:** T14
- **Files:** `score.py` (model load, `build_profile_vector`, `embed_job`), `tests/test_score.py`
- **Do:** Load `SentenceTransformer(settings.embed_model)`; build the profile vector per
  LLD §6.2 (targeting block prepended to full resume, chunk to ~256 tokens, mean-pool,
  L2-normalize); `embed_job` for a job's `title+desc` (char-capped). Cache model across calls.
- **Done when:** profile vector has expected dim and unit norm; embedding is deterministic
  for fixed input/model; long resume is chunked (tail not truncated). (Model download
  allowed once in test setup, or use a tiny test model — keep offline thereafter.)

### T16 — Scoring math & weights  **[P0]**
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

### T17 — Poll pipeline orchestration  **[P0]**
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

### T18 — Web API endpoints  **[P0]**
- **Depends on:** T06, T17
- **Files:** `src/jobfinder/web/app.py`, `web/api.py`, `web/schemas.py`, `tests/test_api.py`
- **Do:** FastAPI app (factory) binding loopback; implement `/api/jobs` (filters+sort
  per LLD §9.1/§9.2, `best`|`newest`, `NULLS LAST`, `include_ineligible` default false),
  `/api/jobs/{id}`, `POST /api/jobs/{id}/status`, `GET /api/runs/latest`. Use FastAPI
  TestClient over a seeded temp DB.
- **Done when:** filter/sort params return expected subsets/orders; status POST persists
  across a fresh client; ineligible hidden unless toggled; detail returns breakdown.

### T19 — Manual poll trigger endpoint  **[P0]**
- **Depends on:** T17, T18
- **Files:** `web/api.py` (`POST /api/poll`), `tests/test_api.py`
- **Do:** `POST /api/poll` spawns the pipeline as a non-blocking subprocess, returns
  `202 {run_id}`. (In tests, patch the spawn to assert it's invoked, not run for real.)
- **Done when:** endpoint returns 202 and triggers the pipeline invocation (mocked in test).

### T20 — Frontend (static SPA)  **[P0]**
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

### T21 — Ashby adapter  **[P1]**
- **Depends on:** T08, T10
- **Files:** `src/jobfinder/sources/ashby.py`, `tests/fixtures/ashby_*.json`, `tests/test_sources.py`
- **Do:** Implement `fetch` per LLD §3.5 (`?includeCompensation=true`), map verified
  fields incl. `workplaceType` → strong remote signal and compensation summary; recency
  pre-filter.
- **Done when:** fixture parses; remote `workplaceType` sets `is_remote`; stale dropped.

### T22 — Adzuna aggregator (optional, keyed)  **[P1]**
- **Depends on:** T08
- **Files:** `src/jobfinder/sources/adzuna.py`, `tests/fixtures/adzuna_*.json`, `tests/test_sources.py`
- **Do:** Implement `fetch` per LLD §3.6 against `ca/search`, passing
  `max_days_old=max_age_days` (source-side recency), `what`/`where`/`category` from
  config; hard throttle + aggressive cache; **skip cleanly when keys absent.**
- **Done when:** with no keys → empty result, no error; with fixture (keys faked) →
  parses Canadian backend postings.

### T23 — Board-token discovery  **[P1]**
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

### T24 — CLI commands  **[P0]**
- **Depends on:** T17, T18, T06
- **Files:** `src/jobfinder/cli.py`, `tests/test_cli.py`
- **Do:** Implement typer commands per LLD §10: `poll` (`--no-cache`, `--source`),
  `serve` (`--host/--port`), `add-company`, `export` (`--csv`), `init` (scaffold config,
  create `data/`, run DDL). Each validates settings first (fail-fast).
- **Done when:** `jobfinder init` produces a runnable config tree; `--help` documents all
  commands; `add-company` writes a verified entry; CLI tests pass via typer's runner.

### T25 — CSV export  **[P1]**
- **Depends on:** T06, T24
- **Files:** `cli.py` (`export`), `tests/test_cli.py`
- **Do:** Implement `export --csv PATH [--min-score N] [--bucket ...]` dumping current
  ranked matches with key columns.
- **Done when:** export produces a CSV whose rows match a filtered DB query; header
  correct.

### T26 — Hardening pass  **[P1]**
- **Depends on:** all P0 tasks
- **Files:** logging setup, defensive guards across adapters, `tests/*`
- **Do:** Add structured logging + `RotatingFileHandler` (LLD §12) with the per-source
  count funnel; ensure every adapter field access is guarded; confirm a network kill
  mid-poll leaves the DB consistent (idempotent re-run recovers).
- **Done when:** simulated mid-poll failure test leaves no partial/duplicate rows; logs
  show the `fetched→kept→eligible→scored` funnel per source.

### T27 — README & scheduling docs  **[P0]**
- **Depends on:** T24
- **Files:** `README.md`
- **Do:** Document setup (`pip install -e .` → `jobfinder init` → add resume → edit
  `profile.yaml`/`companies.yaml` → `jobfinder poll` → `jobfinder serve`), the
  model-swap note (MiniLM↔mpnet), the **cron line and Task Scheduler equivalent**, the
  Adzuna-key optionality, and a list of any `# TODO verify` company tokens to confirm.
- **Done when:** a fresh clone, following only the README, reaches a running dashboard
  showing ranked Canadian/remote backend roles scored against the resume.

### T28 — Definition-of-Done verification  **[P0]**
- **Depends on:** T01–T27
- **Files:** `PROGRESS.md` (final), full suite
- **Do:** Run the full `pytest` + `ruff`; perform the end-to-end manual run from a clean
  state; confirm spec §13 Definition of Done holds (clone → poll → serve shows ranked
  eligible fresh roles, filters + status + recency all working, fully local and free, no
  single source able to crash the run).
- **Done when:** all tests green, lint clean, the end-to-end DoD check passes, and
  `PROGRESS.md` shows every task complete. **Project is ready for use.**

---

## Completed Tasks Log

| # | Date | Task | Files | Notes |
|---|------|------|-------|-------|
| 1 | 2026-06-02 | T01 Repo scaffold & packaging | pyproject.toml, requirements.txt, .python-version, .gitignore, PROGRESS.md, src/jobfinder/{__init__,cli}.py, tests/{__init__,test_cli}.py | uv project (Python pinned 3.12 for later torch CPU wheels); `jobfinder = jobfinder.cli:app` entry point wired to no-op Typer app w/ root callback (empty group needs it for `--help`); deps added per-task per RALPH.md, full pinned target in requirements.txt (LLD §14); removed leftover IntelliJ `src/Main.java` stub; CI green (ruff format/check clean, 3 smoke tests pass, `--help` exits 0). |
| 2 | 2026-06-02 | T02 Settings & config loading | src/jobfinder/settings.py, config/{profile,companies,weights}.yaml.example, .env.example, tests/test_settings.py, tests/fixtures/config/* | pydantic-settings `Settings` (env+`.env`, `JOBFINDER_*` prefix; paths derived from `base_dir`); Adzuna secrets carry unprefixed `.env` aliases + `populate_by_name=True` so both env-load and direct construction work; `adzuna_enabled` true only with both keys. `Profile`/`Weights`/`CompaniesConfig` pydantic models w/ `extra=forbid` + fail-fast `load_*` helpers; weights validator rejects all-zero denominator. Deps pydantic/pydantic-settings/pyyaml (pre-approved LLD §14). 14 tests cover valid→typed, malformed→ValidationError, missing-Adzuna→flag. CI green. |

## Dependency summary (critical path)
T01→T02/T03 → T04→T05→T06 (store) ; T07→T08 + T09→T10 (fetch/normalize) ;
T11/T12 (sources) + T13 + T14→T15→T16 (score) → **T17 (pipeline)** →
T18→T19→T20 (dashboard) → T24/T27/T28 (release).
P1 tasks (T21–T23, T25, T26) extend coverage/polish but are not on the minimal
runnable path — the product is usable after T20 + T24 + T27, and *complete* at T28.
