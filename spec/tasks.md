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
| 15 | 2026-06-04 | T15 Embeddings & profile vector | src/jobfinder/score.py, tests/conftest.py, tests/test_score.py | `load_model(name)` (LLD §6.1) caches `SentenceTransformer` instances in a module dict and **lazy-imports `sentence_transformers`/torch inside the call** so importing `score.py` (e.g. for résumé extraction) never pulls torch — preserving the T14 cheap-import property. `render_targeting(profile)` renders the role+must-have-skills+seniority block. `build_profile_vector(profile, resume_text, *, model)` (LLD §6.2): prepend targeting to the full résumé, split via the pure `_chunk_text` into ≤`_PROFILE_CHUNK_MAX_WORDS=180`-word windows (≈256 tokens — conservative so the model never truncates a chunk's tail; word-based keeps chunking pure + offline-testable without the tokenizer), `encode(chunks, normalize_embeddings=True)`, mean-pool, `_l2_normalize`. `embed_job(job, *, model)` (LLD §6.3): char-cap `title\ndescription` to `_JOB_CHAR_CAP=5000` then encode+normalize. Both accept an injected `Encoder` Protocol (SentenceTransformer-compatible) so the chunk/pool/normalize math is unit-tested **fully offline** with a deterministic recording fake, while dim(384)/unit-norm/determinism are asserted against the real model via a session-scoped `embed_model` fixture in new `tests/conftest.py`. The fixture's first-run model download is the **one sanctioned network touch** (tasks.md T15 carve-out); cached on disk after, offline thereafter, and reused by T16. `_l2_normalize` shared by both functions (no dup); returns the zero vector unchanged. 13 new tests: `_chunk_text` split/tail-preserved/empty, targeting block contents, long-résumé chunking asserted via the recording fake (one batched encode, >1 chunk, tail word present, pooled vec unit-norm), real-model dim+unit-norm, determinism ×2 (`array_equal`), `embed_job` unit-norm+determinism+char-cap on a megabyte description, `load_model` caches per name (monkeypatched ST, no download). Deps added per task (see above). CI green (145 tests, ruff clean). **M3 scoring continues in T16.** |
| 14 | 2026-06-04 | T14 Resume extraction | src/jobfinder/score.py, tests/fixtures/resume.{txt,md,docx,pdf}, tests/test_score.py | `extract_resume(path) -> str` (LLD §6.5): dispatches on the lowercased suffix — `.txt`/`.md` read UTF-8 directly; `.docx` via python-docx walking **paragraphs then table cells** in document order (LLD requires tables, not just paragraphs); `.pdf` via pypdf, **falling back to pdfplumber when pypdf yields no non-whitespace text** (empty/garbled layout). Missing file → `FileNotFoundError` with a clear message; unsupported suffix on an existing file → `ValueError` listing supported formats (fail-fast). Heavy extractors (pypdf/pdfplumber/docx) are **lazy-imported inside their branch** so importing `score.py` stays cheap and never pulls torch (the T15 sentence-transformers code will live in the same module but extraction must not trigger it). Suffix set is module-scope frozensets citing §6.5. **Fixtures generated once** by a throwaway script (not committed): a senior-backend CV carrying Java/Kotlin/Python/AWS so T15/T16 scoring tests reuse them; the `.pdf` is a hand-built minimal PDF-1.4 (text content stream + computed xref) so no PDF-writing dep was needed, and `.docx` includes a 1-row skills table to exercise the table-walk. Fixtures live under `tests/fixtures/resume.*` — **not** caught by the `config/resume.*` gitignore (verified `git check-ignore`). 8 offline tests: all 4 formats extract non-empty text containing every must-have skill, docx table cell surfaced, str-path accepted, pdfplumber fallback forced via monkeypatched empty-pypdf reader (recovers the real fixture), missing-file and unsupported-format sad paths. Deps added per task (see above). CI green (136 tests, ruff clean). |
| 13 | 2026-06-04 | T13 Eligibility filters | src/jobfinder/filters.py, src/jobfinder/normalize.py, tests/test_filters.py | `is_eligible(job, *, profile, now) -> (bool, reason)` (LLD §5): ordered cheapest-first gates that short-circuit before any embedding — (1) **recency** `(now-posted_at).days > max_age_days → "stale"`, with `posted_at is None` (date_unknown) passing by design so it's kept + ranked low, never silently dropped (spec §7); (2) **role-keyword** pre-check (`not _matches_role_keyword → "not_backend_role"`), case-insensitive substring of any `profile.role_keywords` over `title\ndescription` — gated behind `profile.role_keyword_required` (default True, so default behaviour == the LLD reference; the flag only lets a user disable the keyword pre-filter and lean on the semantic scorer); (3) **location** `bucket == OTHER → "location_out"`; (4) **seniority** `JUNIOR or is_people_manager(title) → "seniority_out"`. Reasons are module-scope `REASON_*` string constants persisted to `jobs.ineligible_reason`. **No duplicated regex:** people-manager detection extracted into `normalize.is_people_manager(title)` (reuses the existing `_MANAGER_RE`/`_IC_OVERRIDE_RE` from `infer_seniority`, LLD §4.2) — necessary because manager/director titles infer to `UNKNOWN` seniority which the filter otherwise keeps, so the manager gate must be explicit; a "Principal Engineer" IC override still passes. Pure function: no I/O, no global state. 11 offline tests: eligible passes; each reason fires (stale/non-backend/location_out/junior/people-manager); date_unknown passes recency; keyword matched via description; role gate skipped when `role_keyword_required=False`; staff-IC passes despite "Principal" in title; recency short-circuits before role gate. No new deps (stdlib). CI green (127 tests, ruff clean). |
| 12 | 2026-06-03 | T12 Lever adapter | src/jobfinder/sources/lever.py, src/jobfinder/sources/__init__.py, tests/fixtures/lever_postings.json, tests/test_sources.py | `LeverSource` (LLD §3.4): per configured lever site hits `GET /v0/postings/{site}?mode=json&limit=100` (no auth) through the shared `HttpClient` and emits `RawPosting`s carrying the verbatim payload. Lever returns a bare JSON **array** and paginates via `skip`/`limit` (unlike Greenhouse's whole-board dict), so `_fetch_site` walks pages until a page shorter than `page_limit` is returned (= last page), with a `LEVER_MAX_PAGES=50` defensive cap so a feed that never shortens can't loop forever; `page_limit` is a constructor arg (default `LEVER_PAGE_LIMIT=100`) so pagination is testable without 100+ fixtures. **Recency gate runs here, pre-normalize** (Lever has no server-side date filter): `parse_date(createdAt,"lever")` epoch-ms→UTC; `(now-posted_at).days > max_age_days` → dropped before normalize/embed/score (spec §5, LLD §3.4); `createdAt=null/absent` → kept + flagged date_unknown downstream (never silently dropped, spec §7). Company name is **not** in the Lever payload (LLD §3.4) — it is supplied at normalize time from `companies.yaml` via `company_hint` (the existing `_extract_lever` already does `company_hint or ""`); no normalize change needed. `fetched` counts every posting; `kept_after_recency` counts survivors; funnel feeds LLD §12. Per-site bulkhead: HTTP error / `json.JSONDecodeError` / non-list payload is logged + appended to `errors` and only that site is abandoned; a non-object or id-less posting is skipped+noted but still counted in `fetched`. `now` injectable (default `datetime.now(UTC)`); all field access guarded (`.get`/`isinstance`). Factory `build_lever_source(settings)` loads `companies.yaml`'s lever list + the default client and `register_source`s on import; `sources/__init__` now imports `lever` alongside `greenhouse` (re-exported, no `# noqa`). Fixture has fresh/stale/date-unknown(`description` HTML fallback)/id-less rows; 6 offline tests via `httpx.MockTransport` (parse + fetched/error counts, recency drop = {fresh,date-unknown}, RawPosting→`normalize` round-trip incl. company-hint + epoch-ms date + descriptionPlain/HTML-fallback, **pagination walks skip=0→skip=2 then stops on short page (call count asserted)**, one-site-404 isolated from the healthy site, non-array shape noted). No new deps (httpx already pinned). CI green (116 tests, ruff clean). |
| 11 | 2026-06-03 | T11 Greenhouse adapter | src/jobfinder/sources/greenhouse.py, src/jobfinder/sources/__init__.py, tests/fixtures/greenhouse_jobs.json, tests/test_sources.py | `GreenhouseSource` (LLD §3.3): per configured greenhouse board hits `GET /v1/boards/{token}/jobs?content=true` (no auth) through the shared `HttpClient` and emits `RawPosting`s carrying the verbatim payload. **Recency gate runs here, pre-normalize** (Greenhouse has no server-side date filter): `parse_date(updated_at,"greenhouse")`; if `(now-posted_at).days > max_age_days` the posting is dropped so it never reaches normalize/embed/score (spec §5, LLD §3.3); `updated_at=null` → kept and flagged date_unknown downstream (never silently dropped, spec §7). `fetched` counts every list item; `kept_after_recency` counts survivors; the funnel feeds LLD §12. Per-board bulkhead: HTTP error / `json.JSONDecodeError` / non-dict payload / missing `jobs` list is logged + appended to `errors` and only that board is skipped; a non-object or id-less posting is skipped+noted but still counted in `fetched`. `now` injectable (default `datetime.now(UTC)`) for deterministic tests; all field access guarded (`.get`). Factory `build_greenhouse_source(settings)` loads `companies.yaml`'s greenhouse list + the default client and `register_source`s on import; `sources/__init__` imports the module (re-exported in `__all__`, no `# noqa`) so registration happens whenever the package loads. Fixture has fresh/stale/date-unknown/id-less rows; 5 offline tests via `httpx.MockTransport` (parse + fetched/error counts, recency drop = {fresh,date-unknown}, RawPosting→`normalize` round-trip with entity-decoded body, one-board-404 isolated from the healthy board, shape-mismatch noted). No new deps (httpx already pinned). CI green (110 tests, ruff clean). |
| 10 | 2026-06-03 | T10 Normalizer: location bucketing & seniority | src/jobfinder/normalize.py, tests/test_normalize.py | Added `bucket_location`, `infer_seniority`, and top-level `normalize` (LLD §4.1–§4.3) to the T09 module. `bucket_location(location_raw, is_remote) -> (LocationBucket, bool)`: ordered rules — remote signal = source `is_remote` OR `/remote/i` in text; remote pinned to non-Canada (`/remote.*(us only\|united states only\|emea)/i`) → OTHER (still remote), else remote → REMOTE (Canada-eligible by default per §4.1.1 "no country exclusion"); then `/vancouver\|,bc\|british columbia/`→VANCOUVER, `/toronto\|,on\|ontario/`→TORONTO, `/canada\|montreal\|calgary\|.../`→OTHER_CANADA, else OTHER; returns the effective remote flag so a source signal and a text signal converge. `infer_seniority(title, description)`: first-match-wins on title — people-manager/exec (`principal\|director\|vp\|head of\|manager\b`) → UNKNOWN (filter excludes separately) unless clearly IC (`staff\|principal engineer`) → STAFF; then `\bstaff\b`→STAFF, `senior\|sr.\|lead`→SENIOR, `intern\|junior\|grad\|entry`→JUNIOR, `mid\|intermediate\|ii\|2`→MID; a generic title falls back to unambiguous senior/junior cues in the body (numeric mid cues are title-only — too noisy in prose). `normalize(raw, *, company_hint, now) -> Job` dispatches per-source extraction via `_EXTRACTORS` (greenhouse: entity-decode `content` with stdlib `html.unescape` → `html_to_text`, company = `company_name`\|hint, date from `updated_at`; lever: `descriptionPlain`\|stripped `description`, company = hint, epoch-ms `createdAt`), then applies bucket/seniority helpers and sets `date_unknown = posted_at is None`; an unregistered source raises `ValueError` (fail-fast — Ashby/Adzuna extractors land with T21/T22, matching the M2 build order greenhouse/lever/normalize). 31 new tests: 10 bucket branches (remote-CA, plain remote, US-only→other, EMEA→other, Vancouver, Toronto, Montréal, Ottawa-Canada, NY→other, empty) + source-remote-flag + remote-wins-over-city; 13 seniority titles (staff/principal-IC/senior/sr./lead/junior/intern/II/intermediate/plain/manager/director/principal-product-manager) + desc-fallback + numeric-prose-ignored; greenhouse & lever normalize round-trips + date_unknown + unknown-source raise. No new deps (stdlib `re`/`html`). CI green (105 tests, ruff clean). |
| 9 | 2026-06-03 | T09 Normalizer: HTML, dates, helpers | src/jobfinder/normalize.py, tests/test_normalize.py | New pure module (no I/O, LLD §4.3). `html_to_text` (selectolax): decompose `script`/`style`, `.text(separator=" ")` decodes entities + keeps adjacent blocks apart, `str.split()` collapses every whitespace run incl. `\xa0` from `&nbsp;`; empty/whitespace-only → `""`; `tree.body or tree.root` guard for fragments. `parse_date(value, source)` source-dispatched per §4.3: `EPOCH_MS_SOURCES={"lever"}` → `datetime.fromtimestamp(v/1000, tz=UTC)` accepting int/float/numeric-str; all other sources → `datetime.fromisoformat` (3.12 handles `Z`; naive assumed UTC, aware → `astimezone(UTC)`); any unparseable input → `None` so caller sets `date_unknown`. `bool` explicitly rejected (it's an `int` subclass, never a valid epoch). Constants `_MS_PER_SECOND`, `_NON_CONTENT_TAGS`, `EPOCH_MS_SOURCES` each cite §4. T10 adds `bucket_location`/`infer_seniority`/`normalize` into this module. 14 offline tests (entity/tag strip, nbsp + block-separation collapse, empty; ISO offset/Z/naive→UTC; epoch int + numeric-str; None/garbage-iso/garbage-epoch/bool sad paths). No new deps (selectolax already pinned LLD §14). CI green (74 tests, ruff clean). |
| 8 | 2026-06-02 | T08 Source protocol & registry | src/jobfinder/sources/base.py, tests/test_sources.py | LLD §3.1 contract. `SourceResult` dataclass (source/raw/fetched/kept_after_recency/errors, list defaults). `Source` runtime_checkable `Protocol` (`name` + `fetch(*, max_age_days, throttle_s) -> SourceResult`). Registry = module-global `SOURCES: dict[name, SourceFactory]` + `register_source` (adapters self-register at import time in T11/T12/T21/T22; re-register overwrites, idempotent import). `build_sources(settings, *, only=None, registry=None)` constructs the enabled subset: `only` honors the CLI `--source` selection (LLD §10) and raises `ValueError` fast on an unknown name; `registry` injectable for isolation so tests never touch global `SOURCES`. Enablement is split per LLD: name-selection here vs secret-skip inside the adapter — an optional keyed source (Adzuna) is still *constructed* without its secret and its `fetch` returns an empty result + note, never raises (HLD §5.1). `Settings`/`RawPosting` imported under `TYPE_CHECKING` (no runtime cycle). 10 offline tests: result defaults, protocol satisfaction, single/all/subset build, unknown-name raise, global register overwrite, optional-source skip-without-key + run-with-key. No new deps. CI green (60 tests, ruff clean). |
| 7 | 2026-06-02 | T07 Shared HTTP client (throttle, retry, cache) | src/jobfinder/sources/{__init__,http}.py, tests/test_http.py, requirements.txt, pyproject.toml | `HttpClient` wraps one `httpx.Client` (LLD §3.2 timeouts 10s/connect 5s, http2=True, descriptive UA). Per-host monotonic throttle gate (≥`throttle_s`); retry ≤3 attempts on `{429,500,502,503,504}`+connect/read timeouts with `0.5*2**n`+jitter backoff, honors integer `Retry-After` on 429; on-disk JSON cache key=`sha1(full-url-incl-query)` under `data/http_cache/`, wall-clock TTL, cache hit skips network+throttle, `no_cache` bypass. All time/IO seams injectable (transport/monotonic/sleep/wall_clock/rng) → 14 offline deterministic tests (retry-then-succeed, exhaust→raise, 404 no-retry, timeout retried, cache hit/miss/expired/corrupt, per-host throttle, Retry-After). Module-level `get_json`/`get_text` (LLD §3.2 signature) delegate to a lazy `Settings`-built default client (`configure/reset_default_client` for CLI wiring + test isolation). Dep: added `httpx[http2]` — http2 extra (h2) required by the `http2=True` client; was already in the LLD §14 target set. CI green (51 tests, ruff clean). |
| 6 | 2026-06-02 | T06 Scores/status/runs/companies/prune DAL | src/jobfinder/store.py, tests/test_store.py | Remaining LLD §7.3 ops. `save_score`/`set_status` upsert on their PK (re-write replaces, never duplicates). `start_run` opens a `poll_runs` row (`started_at`, returns AUTOINCREMENT id), `finish_run` stamps `finished_at` + `per_source_json` funnel. `add_company` = `ON CONFLICT(ats,token) DO NOTHING` — discovery dedup that never downgrades a verified entry; paired with `get_companies` reader (optional `ats` filter). `prune(not_seen_days)` deletes `last_seen_at < cutoff` (lexicographic ISO compare — sound because all timestamps are UTC `isoformat`), returns rowcount, cascades scores/status via the §7.2 FKs. Added `_now()` helper; `now` injectable on every clock-using op for deterministic tests. 6 new tests (score upsert, cascade delete, status upsert, run bookkeeping, company dedup/preserve-verified, prune+cascade). Module docstring updated (ops no longer "added by later tasks"). M1 store layer complete. No new deps (stdlib json/datetime). CI green (37 tests, ruff clean). |
| 1 | 2026-06-02 | T01 Repo scaffold & packaging | pyproject.toml, requirements.txt, .python-version, .gitignore, PROGRESS.md, src/jobfinder/{__init__,cli}.py, tests/{__init__,test_cli}.py | uv project (Python pinned 3.12 for later torch CPU wheels); `jobfinder = jobfinder.cli:app` entry point wired to no-op Typer app w/ root callback (empty group needs it for `--help`); deps added per-task per RALPH.md, full pinned target in requirements.txt (LLD §14); removed leftover IntelliJ `src/Main.java` stub; CI green (ruff format/check clean, 3 smoke tests pass, `--help` exits 0). |
| 2 | 2026-06-02 | T02 Settings & config loading | src/jobfinder/settings.py, config/{profile,companies,weights}.yaml.example, .env.example, tests/test_settings.py, tests/fixtures/config/* | pydantic-settings `Settings` (env+`.env`, `JOBFINDER_*` prefix; paths derived from `base_dir`); Adzuna secrets carry unprefixed `.env` aliases + `populate_by_name=True` so both env-load and direct construction work; `adzuna_enabled` true only with both keys. `Profile`/`Weights`/`CompaniesConfig` pydantic models w/ `extra=forbid` + fail-fast `load_*` helpers; weights validator rejects all-zero denominator. Deps pydantic/pydantic-settings/pyyaml (pre-approved LLD §14). 14 tests cover valid→typed, malformed→ValidationError, missing-Adzuna→flag. CI green. |
| 5 | 2026-06-02 | T05 Job upsert & dedupe | src/jobfinder/store.py, src/jobfinder/models.py, tests/test_store.py | `upsert_job` uses `INSERT ... ON CONFLICT(source, source_id) DO UPDATE` (LLD §7.3): idempotent re-poll — `first_seen_at` omitted from the SET (preserved), `last_seen_at` bumped, mutable fields + `embedding`/`eligible`/`ineligible_reason`/`content_hash` refreshed. `_job_params` coerces bool→int, StrEnum→value, datetime→ISO text, `raw`→JSON. Added `eligible`/`ineligible_reason`/`content_hash` to the `Job` model: the LLD §2 listing abbreviates them out but the §7.2 DDL, §8 pipeline (assigns them pre-upsert) and T05 all require them on the persisted record (defaults `True`/`None`/`None`, keyword-only callers unaffected). `Job` imported under `TYPE_CHECKING` to avoid a runtime cycle. No new deps (stdlib json). 2 new tests (insert-with-coercion, dedupe idempotency). CI green (31 tests, ruff clean). |
| 4 | 2026-06-02 | T04 SQLite schema & connection | src/jobfinder/store.py, tests/test_store.py | `connect()` applies LLD §7.1 PRAGMAs (WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON) + `sqlite3.Row` factory + auto-creates parent dir (skips for `:memory:`); `init_db()` runs the full §7.2 DDL via `executescript` (all `IF NOT EXISTS` → idempotent). T04 scope is connect+DDL only; upserts/scores/runs/prune land in T05/T06. 5 tests: PRAGMAs verified on a file-backed db (WAL needs a real file, not `:memory:`), parent-dir creation, all tables+indexes present, idempotent re-run preserves rows, UNIQUE(source,source_id) rejects dupes. No new deps (stdlib sqlite3). CI green (29 tests, ruff clean). |
| 3 | 2026-06-02 | T03 Core data models | src/jobfinder/models.py, tests/test_models.py | `RawPosting` (frozen), `Job`, `ScoreBreakdown` dataclasses + `LocationBucket`/`Seniority`/`Status` enums (LLD §2). Used stdlib `StrEnum` instead of the LLD's illustrative `(str, Enum)` — ruff UP042 mandates it and it's the modern 3.11+ idiom; members still `==` their string value and round-trip to the TEXT columns. Stable dedupe id `make_job_id` = `sha1("{source}:{source_id}")[:16]` (HLD §4.4) with `Job.make_id` alias. 10 tests: id stability/distinctness across source/length+hex, enum round-trips, frozen RawPosting, dataclass defaults. No new deps. CI green (24 tests, ruff clean). |

## Dependency summary (critical path)
T01→T02/T03 → T04→T05→T06 (store) ; T07→T08 + T09→T10 (fetch/normalize) ;
T11/T12 (sources) + T13 + T14→T15→T16 (score) → **T17 (pipeline)** →
T18→T19→T20 (dashboard) → T24/T27/T28 (release).
P1 tasks (T21–T23, T25, T26) extend coverage/polish but are not on the minimal
runnable path — the product is usable after T20 + T24 + T27, and *complete* at T28.
