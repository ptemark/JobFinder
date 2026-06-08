# High-Level Design — Personal Job Discovery & Matching Tool

**Status:** Draft v1
**Author:** (you), with design support
**Source of truth for requirements:** `job-finder-spec.md`
**Scope of this document:** Architecture, component design, data model, key technology
decisions with tradeoffs, cross-cutting concerns, and risks. Implementation-level code
is out of scope (the spec's milestones cover that).

---

## 1. Overview

### 1.1 Purpose
A local, single-user tool that automatically discovers recent backend software
engineering job postings from public ATS feeds, filters them to the user's targeting
criteria (Canada: remote > Vancouver > Toronto; mid–senior; Java/Kotlin/Python/AWS),
scores each posting against the user's full resume using free local embeddings, and
presents ranked results in a local web dashboard. The user applies manually via the
linked posting. The system never auto-applies.

### 1.2 Goals
- Zero recurring cost; runs entirely on the user's machine.
- Fresh results only: postings older than 21 days are out of scope.
- High-quality ranking driven by the full resume, with the user's stated skill and
  location priorities weighted to dominate.
- Low operational burden: one scheduled command keeps data current.

### 1.3 Non-goals
- No auto-submission of applications (read-only against application endpoints). The sole
  outbound *write* is the optional tracking sync to the user's **own** Google Sheet
  (§3.7) — recording that the user applied, never submitting to an employer.
- No multi-user, no multi-tenancy, no remote hosting, no authentication.
- No scraping of sites that prohibit it; only public JSON ATS feeds + permissive APIs.
- No paid services or paid API tiers (the Google Sheets API and a service account are free).

### 1.4 Context (system boundary)

```
        ┌────────────────────────── User's machine ──────────────────────────┐
        │                                                                     │
External│   cron / Task Scheduler ──triggers──▶ [ poll pipeline ]             │
ATS &   │                                            │                        │
APIs ───┼──HTTP(S) GET──▶ [ Source adapters ] ──▶ [ Normalize ] ──▶ [ SQLite ]│
(GH,    │                                                              │  ▲   │
Lever,  │                                        [ Scorer (embeddings) ]┘  │   │
Ashby,  │                                                              ▼  │   │
Adzuna) │   Browser ◀──localhost HTTP──▶ [ FastAPI dashboard ] ────────────┘   │
        │                                    │ mark "applied"                  │
Google  │                                    ▼                                 │
Sheets ◀┼──HTTPS write (append row)──── [ Sheets sync ]                        │
 API    │                                                                     │
        └─────────────────────────────────────────────────────────────────────┘
```
Egress is outbound HTTPS to job sources (reads) **plus** an optional write to the user's
own Google Sheet when a job is marked `applied` (§3.7). The dashboard binds to localhost;
the browser never calls Google — the Sheets write is server-side.

---

## 2. Architecture

### 2.1 Style
A **modular monolith** packaged as a single Python application with two entry points
(a CLI `poll` job and a `serve` web app) sharing one library core and one SQLite file.

**Decision & rationale.** For a single-user, single-machine tool, a monolith is the
correct default: no network hops, no serialization between components, trivial to run
and debug. Microservices, a message queue, or a separate worker process would add
operational weight (multiple processes to supervise, IPC, failure modes) with no
benefit at this scale. The internal module boundaries (sources / normalize / store /
score / web) are kept clean so the pieces remain independently testable and a future
extraction is possible, but they run in-process.

*Rejected:* (a) **Worker + broker (Celery/Redis)** — overkill; introduces a broker
dependency and a daemon for a job that runs a few times a day. (b) **Serverless /
cloud functions** — violates the local, zero-cost, no-account constraints.

### 2.2 Execution model
Two independent invocations over a shared database:

- **Poll pipeline** (`jobfinder poll`): a short-lived batch process. Fetch → normalize
  → dedupe/store → score. Triggered by the OS scheduler (cron on macOS/Linux, Task
  Scheduler on Windows). Idempotent: safe to run repeatedly; re-running only updates.
- **Dashboard** (`jobfinder serve`): a long-lived local web server the user opens in a
  browser. Read-mostly; the only writes are user status changes and an optional manual
  "poll now" trigger that shells out to the same pipeline.

**Decision & rationale.** Decoupling ingestion (batch, scheduled) from presentation
(on-demand, interactive) via a shared store is the classic, robust pattern: the
dashboard never blocks on network I/O, and a failed poll never takes down the UI. The
user chose a local scheduler over an always-on daemon, so the pipeline is deliberately
a stateless one-shot process that exits — nothing to supervise, no memory growth, no
crash-restart logic. SQLite's locking is sufficient for the rare case of a poll and a
dashboard write overlapping (see §4.4).

### 2.3 Component inventory

| Component | Responsibility | Key dependency |
|---|---|---|
| **Source adapters** | Fetch raw postings from one provider each, behind a common interface | `httpx` |
| **Normalizer** | Map raw payloads → unified `Job`; strip HTML; bucket location; infer seniority; parse dates | `selectolax` (HTML→text) |
| **Store** | Persist jobs, embeddings, user status, poll metadata; dedupe | stdlib `sqlite3` |
| **Scorer** | Embed resume + jobs; compute final score + breakdown | `sentence-transformers` |
| **Filter** | Apply hard eligibility gates (recency, location, role, seniority) | — |
| **Pipeline** | Orchestrate poll: sources → normalize → filter → store → score | — |
| **Web/dashboard** | Serve ranked results (All / Applied tabs), filters, status updates, manual poll | `FastAPI` + `uvicorn` |
| **Sheets sync** | On `applied`, append a row to the user's tracking sheet (best-effort, opt-in) | `google-auth` + `httpx` |
| **CLI** | `poll`, `serve`, `add-company`, `export` | `typer` |
| **Config** | Load profile, companies, weights, secrets | `pydantic-settings`, `PyYAML` |

---

## 3. Component design

### 3.1 Source adapters
- A `Source` protocol: `fetch(max_age_days) -> Iterable[RawPosting]`. Each provider
  (Greenhouse, Lever, Ashby, Adzuna) implements it independently.
- **Recency pushed down per source** (see spec §5): Adzuna uses `max_days_old`; Lever
  and Greenhouse/Ashby have no server-side date filter, so the adapter fetches the
  active board and discards stale postings *before* returning them — so the expensive
  stages (normalize/embed) never see them.
- **Isolation:** one source raising does not abort the poll. Each adapter is wrapped so
  failures are logged, counted, and surfaced, and other sources still complete
  (spec §12 M6). This is a deliberate **bulkhead** around an unreliable dependency.
- **Politeness:** per-source throttle (default ≥1s between calls) and on-disk HTTP
  response cache keyed by URL with a short TTL, to respect rate limits (esp. Adzuna's
  free tier) and make re-runs cheap.

**Decision & rationale (HTTP client).** `httpx` over stdlib `urllib` and over
`requests`: `httpx` gives timeouts, connection pooling, and an HTTP/2-capable, modern
API in one well-maintained dependency, and keeps the door open to async fan-out across
sources if poll latency ever matters. `requests` is fine but in maintenance mode and
sync-only; `urllib` is free but verbose and error-prone for retries/timeouts. Footprint
cost is low and justified.

### 3.2 Normalizer
- Pure functions, no I/O: `raw -> Job`. Trivially unit-testable against committed
  fixtures (no live calls in tests — keeps CI deterministic and free).
- **HTML→text:** `selectolax` (fast, lenient C parser) rather than `BeautifulSoup`
  + a parser backend. Descriptions are messy employer HTML; selectolax is faster and
  has a smaller surface than bs4+lxml for our single use (strip tags, get text).
- **Location bucketing:** rule-based mapping of `location_raw`/remote flags →
  {remote, vancouver, toronto, other_canada, other}. Conservative: only confident
  Canada-eligible remote is bucketed `remote`. **A remote posting that names any
  non-Canada country/region (US, EMEA, LATAM, …) buckets `other`** — the rule excludes by
  positive non-Canada signal, not just the narrow "US only/EMEA" phrasings, so plain
  "Remote — US" no longer leaks into `remote` (spec §7, LLD §4.1).
- **Seniority inference:** keyword/regex heuristics → {junior, mid, senior, staff,
  unknown}. Errs toward `unknown` (kept, ranked low) rather than wrong exclusion.
- **Date parsing:** normalize each source's date field to UTC `posted_at`; unparseable
  → `None`, flagged `date_unknown` downstream.

### 3.3 Scorer (matching engine)
- **Model:** `all-MiniLM-L6-v2` default (fast, CPU-friendly, 384-dim), config-swappable
  to `all-mpnet-base-v2` (768-dim, higher quality) — a documented quality/speed lever.
  Model downloads once and is cached locally; all inference is offline and free.
- **Profile vector:** built from the **full resume** (PDF/docx/txt/md auto-detected and
  fully extracted; long resumes chunked and mean-pooled so the tail isn't truncated)
  **plus** a structured targeting block that is weighted to dominate, so the
  Java/Kotlin/Python/AWS + backend + mid–senior signals steer matches rather than
  generic resume similarity (spec §8.1).
- **Per-job score:** `final = weighted_sum(semantic_cosine, skill_match, location,
  recency)`, normalized 0–100, with the **component breakdown stored** so the dashboard
  can explain *why* a job ranked where it did. Recency is a decay over the 0–21d window;
  skill-match bonuses are heavy by default.
- **No external API.** Scoring is fully local. An LLM rerank is explicitly out unless
  the user opts in later (spec §8.2).

**Decision & rationale (matching approach).** Local bi-encoder embeddings + cosine,
with weighted boosts, beats the alternatives on the cost/quality/effort frontier:
- *vs. keyword/TF-IDF only:* misses semantic equivalence ("Spring Boot microservices"
  ↔ "backend services in Java"); cheap but blunt. We keep keyword signal as a *boost*,
  not the primary ranker.
- *vs. LLM-API scoring per job:* highest quality but recurring cost and rate limits —
  violates the zero-cost constraint. Left as an optional future toggle.
- *vs. fine-tuned/cross-encoder reranker:* better ranking but heavier (slower, larger
  download) and unnecessary at this candidate volume. The mpnet swap is a sufficient
  quality lever for now.

### 3.4 Filter
Hard eligibility gates applied **before** scoring, cheapest first (spec §7): recency
(21d) → role keyword/semantic gate → location bucket ∈ Canada set → seniority not
junior/intern/manager → not user-dismissed. Ineligible jobs are stored but hidden by
default (a debug toggle reveals them, to catch false negatives). `date_unknown` jobs
are kept and sorted low rather than dropped.

### 3.5 Web/dashboard
- **Backend:** FastAPI + Uvicorn. Endpoints: `GET /api/jobs` (filter/sort), `GET
  /api/jobs/{id}`, `POST /api/jobs/{id}/status`, `POST /api/poll` (shells the pipeline).
- **Frontend:** a single static page (vanilla JS or a tiny build-free framework) served
  by FastAPI. No SPA build toolchain — keeps footprint and maintenance low.
- **Views:** **two tabs — All / Applied** (All excludes `applied`+`dismissed`; Applied
  shows only `applied`, newest-applied first); ranked cards (score, title, company,
  location badge, prominent "Xd ago" age badge, matched skills, "new since last poll");
  filters (location, source, seniority, min score, status, age ≤7/14/21d); sort toggle
  best-match | newest-first; detail view with full description + score breakdown + apply
  link; per-job status. Marking `applied` removes the card from **All** and triggers the
  server-side Sheets sync (§3.7).
- **Styling:** a refreshed, denser, more polished card/tab treatment kept deliberately
  lightweight — still the single static page + vanilla JS + plain CSS (no framework, no
  build step), so the footprint claim below is unchanged.
- Binds to `127.0.0.1` only; no auth (local trust boundary); browser talks only to the
  local backend (no third-party calls from the page — the Sheets write is server-side).

**Decision & rationale (web stack).** FastAPI + a static page over (a) a heavier
SPA (React/Vite) — unnecessary build tooling and dependency weight for a handful of
views; (b) a server-rendered template stack (Flask+Jinja) — fine, but FastAPI gives
typed request/response models via pydantic (already in the stack) and an async server
that comfortably handles the manual-poll trigger without blocking. The interactivity
here (filter/sort/status) is light enough for vanilla JS, honoring "minimize footprint."

### 3.6 CLI & config
- **CLI:** `typer` (built on Click) for `poll | serve | add-company | export`. Typed,
  self-documenting `--help`, minimal boilerplate.
- **Config:** `profile.yaml` (targeting + scoring weights), `companies.yaml` (ATS board
  tokens, grows via auto-discovery), `resume.{pdf,docx,txt,md}` (gitignored), `.env`
  (Adzuna key + Google Sheets credentials/sheet id, all optional). Loaded/validated via
  `pydantic-settings` so a malformed config fails fast with a clear message rather than
  mid-poll.

### 3.7 Sheets sync (application tracker)
- **Trigger:** the dashboard's `POST /api/jobs/{id}/status` with `state=applied`. The
  status write persists first and is authoritative; the sync is a **best-effort** side
  effect invoked after it (spec §15).
- **What it writes:** one appended row to the user's tracking sheet
  (`Company | Position | Response | Link`) — Company=company, Position=title, Link=url,
  and the **Response cell left blank but shaded yellow** (the user's "applied, waiting to
  hear back" colour convention). Done in a single Sheets `spreadsheets:batchUpdate`
  `appendCells` request that carries both the values and the cell `backgroundColor`.
- **Idempotency:** read the sheet's Link column first; if the posting URL is already
  present, skip the append (re-marking / retries never duplicate).
- **Isolation & opt-in:** wrapped like a source bulkhead — a Sheets error is logged and
  surfaced in the response but never rolls back the status or fails the request. Active
  only when `GOOGLE_SHEETS_CREDENTIALS` + `JOB_TRACKER_SHEET_ID` are set; absent → skipped
  with an info note (the Adzuna-key degradation pattern, §5.1).

**Decision & rationale (Sheets client).** `google-auth` (service-account JWT → OAuth2
token) + the **existing `httpx`** client calling the Sheets **REST** API, chosen over the
official `google-api-python-client`: the SDK is a heavy transitive dependency
(`google-api-core`, `googleapis-common-protos`, `uritemplate`, a discovery-doc layer) for
what is two REST calls (read the Link column; `appendCells`). `google-auth` alone handles
the only genuinely fiddly part — signing the service-account assertion — and we already
have `httpx` for everything else, honoring "minimize footprint." A **service account**
over an OAuth user-consent flow because it is headless (no browser round-trip from a
local/cron process) and the user grants access by sharing the sheet once; the key is
gitignored and read from `.env` (HLD §5.1, spec §15).

---

## 4. Data design

### 4.1 Store choice
**SQLite** (single file at `data/jobs.db`), stdlib `sqlite3`.

**Decision & rationale.** For single-user local persistence with relational queries
(filter/sort/join status), SQLite is the textbook fit: zero-config, serverless, a
single portable file, ACID, and present in the Python stdlib. Postgres would add a
server to install and run; a document store (e.g. TinyDB/JSON files) would lose
querying and integrity. The access pattern — modest writes during poll, read-mostly
from the dashboard — is squarely in SQLite's comfort zone.

### 4.2 Vector storage & search
Store each job's embedding as a `float32` BLOB in the jobs table; compute cosine in
Python (numpy) over the eligible candidate set at scoring time. **No vector index.**

**Decision & rationale.** Candidate volume is small (low thousands of active postings,
far fewer after the 21-day + eligibility filters). Brute-force cosine over a few
thousand 384-dim vectors is sub-millisecond and needs no extra dependency. A dedicated
vector index only earns its keep at ~hundreds of thousands of vectors; below that it is
pure overhead. **Documented scale-out path:** if the corpus ever grows past that range,
add the `sqlite-vec` extension (keeps everything in SQLite, actively maintained — note
its predecessor `sqlite-vss` is deprecated, so it should not be used). This keeps the
default install free of native extensions while leaving a clean upgrade.

### 4.3 Logical schema (indicative)

```
jobs(
  id TEXT PRIMARY KEY,        -- hash(source, source_id)
  source TEXT, source_id TEXT,
  company TEXT, title TEXT, description TEXT,
  location_raw TEXT, is_remote INT, location_bucket TEXT,
  seniority TEXT, url TEXT,
  posted_at TEXT,            -- ISO8601 UTC, nullable
  date_unknown INT,
  first_seen_at TEXT, last_seen_at TEXT,
  embedding BLOB,            -- float32 vector
  raw_json TEXT
)
scores(job_id TEXT PK→jobs.id, final REAL, semantic REAL, skill REAL,
       location REAL, recency REAL, scored_at TEXT)
status(job_id TEXT PK→jobs.id, state TEXT,   -- new|interested|applied|dismissed
       updated_at TEXT)
poll_runs(id INTEGER PK, started_at TEXT, finished_at TEXT,
          per_source_json TEXT)             -- counts, errors, for "new since last poll"
companies(token TEXT, ats TEXT, name TEXT, verified INT, added_at TEXT,
          PRIMARY KEY(ats, token))
```
Indexes on `jobs(posted_at)`, `jobs(location_bucket)`, `scores(final)` for fast
dashboard filter/sort.

### 4.4 Concurrency & integrity
- WAL mode enabled → concurrent dashboard reads during a poll write without blocking.
- Poll writes wrapped per-job in short transactions; upsert keyed on `id` makes the
  whole poll **idempotent** and crash-safe (a killed poll leaves committed rows
  consistent; the next run reconciles).
- The rare poll-write vs. dashboard-status-write contention is handled by SQLite's
  busy-timeout/retry; acceptable given the single-user, low-write profile.

### 4.5 Data lifecycle / retention
- Postings past the 21-day window are excluded from results but retained briefly for
  "new since last poll" diffing and debugging; a prune step in `poll` deletes jobs not
  seen in N days (config, default 30) to keep the DB small.
- Resume and `.env` are gitignored; the DB lives under `data/` (gitignored). All
  personal data stays on the machine.

---

## 5. Cross-cutting concerns

### 5.1 Configuration & secrets
Single source of truth in `config/`. Secrets only in `.env` (never committed;
`.env.example` provided). The system degrades gracefully without optional secrets:
absent Adzuna key → that source is skipped, direct ATS feeds still run (spec §12 M5).

### 5.2 Observability
- Structured logging to console + a rotating file under `data/logs/`. Each poll logs
  per-source counts (fetched / kept-after-recency / eligible / scored) and errors.
- `poll_runs` table is the durable record the dashboard reads for run history and the
  "new since last poll" indicator.

### 5.3 Error handling & resilience
- **Per-source bulkhead** (one source's failure is contained; §3.1).
- Network calls: timeouts + bounded retries with backoff; cached responses reused on
  transient failure where possible.
- Config validation fails fast at startup, not mid-run.
- Pipeline is idempotent and resumable by re-running.

### 5.4 Performance
- Dominant cost is embedding inference (CPU). Mitigations: filter to eligible set
  *before* embedding; embed only new/changed postings (skip jobs whose `id` already
  has a current embedding); batch `encode()` calls. Expected poll time: seconds to a
  couple of minutes depending on source count and model choice.
- Dashboard queries are indexed and operate on a small dataset → effectively instant.

### 5.5 Security & privacy
- Trust boundary is the local machine; dashboard binds to loopback, no auth by design.
- Only outbound traffic is GETs to public job feeds (+ optional Adzuna with a key).
- No application data is ever transmitted; the tool never POSTs to apply endpoints.
- Personal data (resume, DB, logs) never leaves the device and is gitignored.

### 5.6 Portability & deployment
- Pure-Python install (`pip install -e .`), pinned `requirements.txt`, Python 3.11+.
- No native extensions in the default path (embeddings model is pip-installed; vector
  search is numpy). Runs on macOS/Linux/Windows.
- Scheduling via OS scheduler; README documents the cron line and the Task Scheduler
  equivalent. No container required (single-machine, single-user choice); a Dockerfile
  is an optional future nicety, not part of the baseline.

### 5.7 Testing strategy
- Unit tests for normalizer (bucketing, seniority, date parsing), filter gates, and
  scoring determinism — all against **committed fixtures, zero live network**, keeping
  the suite deterministic, fast, and free.
- A scoring sanity test asserts ordering (senior remote Java/AWS role > junior onsite
  frontend role) per spec §12 M3.
- `ruff` for lint/format; tests + lint are the milestone "done" gate.

---

## 6. Key decisions summary

| # | Decision | Chosen | Main alternative(s) | Why |
|---|---|---|---|---|
| D1 | Architecture style | Modular monolith, in-process | Microservices / worker+broker | No benefit at single-user scale; far lower op burden |
| D2 | Execution | Scheduled one-shot poll + separate local server, shared DB | Always-on daemon | User chose OS scheduler; stateless process = nothing to supervise |
| D3 | Datastore | SQLite (stdlib) | Postgres / JSON files | Zero-config, ACID, portable, perfect for single-user local |
| D4 | Vector search | Brute-force cosine (numpy) over BLOBs | sqlite-vec / FAISS now | Tiny corpus; index is overhead until ~100k+; sqlite-vec documented as scale-out |
| D5 | Matching | Local bi-encoder embeddings + weighted boosts | TF-IDF only / LLM-API scoring | Best cost/quality/effort balance; stays free and offline |
| D6 | Embedding model | MiniLM default, mpnet swap | mpnet-only / larger models | Fast on CPU; quality lever available without code change |
| D7 | HTTP client | httpx | requests / urllib | Modern, timeouts/pooling, async-ready, well-maintained |
| D8 | Web stack | FastAPI + static page | React SPA / Flask+Jinja | Typed APIs via pydantic, no build toolchain, light footprint |
| D9 | HTML parsing | selectolax | BeautifulSoup+lxml | Faster, smaller surface for strip-to-text |
| D10 | CLI / config | typer + pydantic-settings/YAML | argparse + ad-hoc parsing | Typed, self-documenting, fail-fast validation |
| D11 | Recency handling | Push down per source; central 21d gate before embedding | Filter only at the end | Avoids wasted fetch/compute; honors "don't even look at stale" |
| D12 | Sheets sync auth | Service account JSON (`google-auth`) | OAuth user-consent flow | Headless (no browser from cron/local); share-sheet-once grant; key gitignored |
| D13 | Sheets client | `google-auth` + existing `httpx` on the REST API | `google-api-python-client` SDK | Two REST calls don't justify the SDK's heavy transitive deps; reuse httpx |
| D14 | Applied jobs | Hidden from default list + own tab; status authoritative, sync best-effort | Keep in list / sync transactional | Matches user workflow; a Sheets failure must never lose the local status |

---

## 7. Risks & mitigations

| Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|
| ATS feed shape changes / undocumented | A source breaks | Med | Per-source bulkhead + fixtures; failure is isolated and visible, not fatal |
| Adzuna free-tier limits / ToS (personal-use only, rate-limited) | Aggregator unusable | Med | Throttle + cache; personal-use only; degrade gracefully if key absent |
| Greenhouse/Ashby have no server-side date filter | Must fetch full boards | High (known) | Accept fetch; drop stale before normalize/embed so compute is unaffected |
| Seniority/role heuristics misclassify | Good jobs hidden | Med | Err toward `unknown` (kept, low rank); debug toggle to review filtered-out jobs |
| Embedding compute slow on weak CPU | Long polls | Low–Med | Embed only eligible + new jobs; batch; MiniLM default |
| Resume parsing (messy PDFs) loses text | Weaker matches | Med | pdfplumber fallback for tricky layouts; chunk+pool full text |
| Stale company list misses employers | Coverage gaps | Med | Aggregator-driven board-token auto-discovery appends to companies.yaml |
| Harvest API deprecation (Aug 31 2026) | None to us | n/a | We use the public Job Board API, not Harvest; noted to avoid accidental use |
| Sheets API error / bad creds / sheet not shared | `applied` row not written | Med | Best-effort bulkhead: status still persists, error surfaced; opt-in, skipped if unconfigured |
| Sheets schema drift (user reorders columns) | Row written to wrong columns | Low–Med | Fixed `Company\|Position\|Response\|Link` contract documented; README notes to keep column order |
| Remote-bucket false negatives (real Canada-remote dropped) | Good role hidden | Low–Med | Keep `other`-bucketed jobs viewable via the debug/include-ineligible toggle (§3.4) |

---

## 8. Open items / future work
- Optional opt-in LLM rerank of the top-N matches (would add cost; off by default).
- Optional Dockerfile for portability across machines.
- Additional sources (Workable, Recruitee, Personio) behind the same `Source` interface.
- `sqlite-vec` migration if corpus ever outgrows brute-force search.
- Email/desktop notification of top new matches (currently dashboard-only).
