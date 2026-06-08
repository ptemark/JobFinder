# Design Spec — Personal Job Discovery & Matching Tool

**Owner:** (you)
**Implementation:** Claude Code + Ralph loop, working against the milestones below.
**Cost target:** $0 to run. No paid APIs, no cloud hosting required. Runs locally.

---

## 1. Goal

A local tool that automatically discovers recent software-engineering job postings
from public ATS feeds, filters them to the user's targeting criteria, scores each
posting against the user's resume using free local embeddings, and surfaces ranked
matches in a local web dashboard. Applying is done manually by the user via the
linked posting (the tool does NOT auto-submit applications).

## 2. User profile / targeting (hardcoded defaults, editable in config)

- **Role:** mid-to-senior **backend software engineer / developer**
- **Core skills:** Java, Kotlin, Python; **AWS** experience
- **Location priority (ranked):**
  1. Full remote (Canada-eligible)
  2. Vancouver, BC
  3. Toronto, ON
  4. Other, Canada or US visa-eligible
- **Country:** Canada
- **Seniority filter:** include "mid", "intermediate", "senior", "staff"-adjacent;
  exclude "intern", "junior"/"new grad", "principal/director/manager" unless IC.
- **Recency cutoff (hard):** ignore any posting older than **21 days** (configurable
  `max_age_days`, default 21). Postings past cutoff are out of scope entirely — not
  shown, not scored, and not embedded. Within the cutoff, **more recent = higher
  priority** (recency is both a hard filter AND a ranking signal).

These live in `config/profile.yaml` so they can be changed without code edits.

## 3. Non-goals / explicit guardrails (DO NOT do these)

- **No auto-apply.** Never POST to any application-submission endpoint. Read-only
  against job sources. (The one sanctioned outbound *write* is the optional
  application-tracking sync to the user's **own** Google Sheet — see §15 — which records
  that the user applied; it never submits an application to an employer.)
- **No scraping of sites that prohibit it.** Use only public JSON ATS feeds and
  official APIs with permissive terms (see §5). No headless-browser scraping of
  LinkedIn/Indeed. No circumventing rate limits or bot protection.
- **No credentials in code.** Any API key (e.g. Adzuna) loaded from `.env`, never
  committed. Provide `.env.example`.
- **Respect rate limits.** Adzuna free tier is rate-limited and its terms restrict
  non-personal/commercial aggregation; this tool is personal-use only. Throttle all
  external calls (configurable delay, default 1 req/sec/source) and cache responses.
- **No paid services.** If a step would require payment, stop and surface it in the
  dashboard as "source unavailable (would require paid tier)" rather than spending.

## 4. Architecture overview

```
┌─────────────┐   ┌──────────────┐   ┌─────────────┐   ┌──────────────┐
│  Sources    │──▶│  Normalizer  │──▶│   SQLite    │──▶│   Scorer     │
│ (ATS feeds, │   │ (unified Job │   │ (dedupe +   │   │ (embeddings  │
│  aggregator)│   │   schema)    │   │  history)   │   │  + filters)  │
└─────────────┘   └──────────────┘   └─────────────┘   └──────┬───────┘
                                                              │
                                              ┌───────────────▼────────────┐
                                              │  Local web dashboard (FastAPI│
                                              │  + static frontend)          │
                                              └──────────────────────────────┘
```

- **Language:** Python 3.11+.
- **Storage:** SQLite (stdlib `sqlite3`), file at `data/jobs.db`.
- **Embeddings:** `sentence-transformers`, model `all-MiniLM-L6-v2` (default, fast,
  CPU-friendly). Config option to swap to `all-mpnet-base-v2` for higher quality.
- **Backend:** FastAPI + Uvicorn serving JSON + a static SPA (vanilla or lightweight).
- **Scheduler:** a `poll` CLI command run on demand or via cron / Task Scheduler.
- **Packaging:** single repo, `pip install -e .`, `requirements.txt` pinned.

## 5. Data sources (priority order; each behind a common `Source` interface)

All are public, read-only, no-OAuth JSON feeds unless noted. The fetch universe is
defined by a **company board-token list** (`config/companies.yaml`) PLUS optional
aggregator search. Hybrid discovery: seed list + aggregator-driven expansion.

1. **Greenhouse Job Board API** (no auth)
   `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
   - Returns full job list for a company; no server-side filtering, so filter client-side.
   - Single-job detail: `.../jobs/{id}`.
2. **Lever Postings API** (no auth)
   `GET https://api.lever.co/v0/postings/{company}?mode=json`
   - Supports query params: team, department, location, commitment, level, skip, limit.
3. **Ashby** (no auth) — public job board JSON; supports `includeCompensation=true`.
4. **Workable / Recruitee / Personio** public endpoints — implement if time allows
   (lower priority; behind the same `Source` interface).
5. **Adzuna API** (key required, free tier, rate-limited) — used ONLY as an aggregator
   to (a) surface postings from companies not in the seed list and (b) auto-discover
   new company board tokens to add. Country = `ca`. Key from `.env`.
   - NOTE: Adzuna terms restrict aggregation for non-personal use; this is personal use
     only and must stay within free-tier limits. If key absent, skip gracefully.

**Date-aware fetching (minimize work on stale postings):** wherever a source
supports server-side date/recency filtering, use it so old postings are never
fetched in full:
- **Lever:** no date param, but `posted_at` is present per posting — discard past
  cutoff immediately after the list call, before any detail fetch or embedding.
- **Adzuna:** supports `max_days_old` — set it to `max_age_days` so the aggregator
  only returns recent postings.
- **Greenhouse / Ashby:** list endpoints return the whole active board with no
  server-side date filter. Fetch the list, read each posting's `updated_at` /
  posted date, and **drop anything past cutoff before normalizing, scoring, or
  embedding.** Stale postings must not reach the scorer.
The cutoff is enforced once, centrally, in the pipeline (see §7) so every source
benefits regardless of its native capabilities.

**Company discovery:** when a posting is found via aggregator that links to a
Greenhouse/Lever/Ashby board, extract the board token and append it to
`companies.yaml` (dedup) so future polls hit the cheaper direct feed.

A starter `companies.yaml` should ship with ~15-25 known Canadian / remote-friendly
tech employers using these ATSs (the loop should populate plausible seeds and mark
them `# verify` — user will confirm).

## 6. Unified Job schema (normalizer output)

```python
Job(
  id: str,                # stable hash of (source, source_id)
  source: str,            # "greenhouse" | "lever" | "ashby" | "adzuna" | ...
  source_id: str,
  company: str,
  title: str,
  description: str,       # plain text, HTML stripped
  location_raw: str,
  is_remote: bool,
  location_bucket: str,   # "remote" | "vancouver" | "toronto" | "other_canada" | "other"
  seniority: str,         # inferred: "junior"|"mid"|"senior"|"staff"|"unknown"
  url: str,               # canonical apply/posting URL
  posted_at: datetime|None,
  first_seen_at: datetime,
  raw: dict,              # original payload for debugging
)
```

## 7. Filtering rules (applied before scoring)

A posting is **eligible** if ALL hold:
- **Within recency cutoff:** `posted_at` (or `updated_at` fallback) is within
  `max_age_days` (default 21) of now. This gate is checked FIRST and FAST — before
  any embedding work — so stale postings are dropped cheaply. A posting with no
  parseable date is treated as eligible but flagged `date_unknown` (so you can see
  it rather than silently lose it), and sorted below dated postings of equal score.
- Title or description indicates a backend SWE/developer role (keyword + embedding gate).
- `location_bucket` in {remote, vancouver, toronto, other_canada}; non-Canada remote
  excluded. **Remote is Canada-eligible only when there is a positive Canada/North-America
  signal** — `is_remote` plus an explicit `canada | remote (north america) | anywhere`
  cue — OR no country is named at all. A remote posting that names a *non-Canada* country
  or region (e.g. "Remote — US", "Remote (United States)", "Remote, EMEA", "US-based",
  "Remote LATAM") buckets `other` and is excluded. Tightened from the prior rule, which
  only excluded the narrow "US only / EMEA" phrasings and let plain "Remote — US"
  through (see §7 location gate; LLD §4.1).
- `seniority` not in {junior, intern}; not a pure manager/director role.
- Not already marked `dismissed` **or `applied`** by the user. (`applied` jobs move to
  their own tab — §9 — so the main ranked list shows only roles still to act on.)

Eligible postings are ranked; ineligible ones are stored but hidden by default
(viewable via a filter toggle, for debugging false negatives).

## 8. Scoring

### 8.1 Profile / resume ingestion
- The user's **full resume** is the primary semantic signal. Read it from
  `config/resume.{pdf,docx,txt,md}` (auto-detect extension). Extract the **entire**
  text:
  - PDF → `pypdf` (or `pdfplumber` fallback for messy layouts).
  - docx → `python-docx`.
  - txt/md → read directly.
- Build the **profile text** = full resume text + the structured targeting block from
  `profile.yaml` (role, must-have skills, seniority). The structured block is
  **prepended and weighted** so it dominates: the resume gives breadth/context, but
  the Java/Kotlin/Python/AWS + backend + mid-senior signals must steer the match.
- If the resume is long, chunk it and mean-pool the chunk embeddings into a single
  profile vector (handles the model's input-length limit without losing the tail).
- Resume file is **gitignored** (personal data, stays local).

### 8.2 Per-job scoring
- Embed each eligible job's `title + description` (truncate to model max).
- `semantic_score = cosine_similarity(profile_vector, job_vector)`.
- **Boosts / weights (all configurable in `profile.yaml`):**
  - **Skill match** — explicit bonus per must-have skill present (Java, Kotlin,
    Python, AWS). This is weighted heavily so the user's stated priorities beat raw
    resume similarity.
  - **Location** — bucket bonus: remote > vancouver > toronto > other_canada.
  - **Recency** — since stale postings are already filtered at 21 days, recency here
    is a **ranking** signal: a decay over the 0–21 day window (e.g. linear or
    exponential, newest ≈ full bonus, ~21 days ≈ 0). Default weight high enough that,
    between two otherwise-similar matches, the newer one ranks above the older.
- `final_score = weighted_sum(semantic_score, skill_match, location, recency)`,
  normalized 0–100. Store the **component breakdown** alongside the total so the
  dashboard can show *why* a job scored as it did.
- **Default sort:** by `final_score` desc. Provide an alternate **"newest first"**
  sort in the dashboard for when the user wants pure recency.
- **No LLM API call.** Scoring stays fully local and free. Do NOT add a paid rerank
  step unless the user explicitly enables it later.

## 9. Dashboard (local web UI)

- `GET /` serves the SPA. Backend endpoints: `/api/jobs` (filter/sort params),
  `/api/jobs/{id}` (detail + score breakdown), `POST /api/jobs/{id}/status`
  (mark interested / applied / dismissed), `POST /api/poll` (trigger a refresh).
- **Tabs:** the dashboard has two top-level tabs — **All** (the active ranked list,
  excluding `applied` and `dismissed`) and **Applied** (only jobs the user has marked
  `applied`, most-recently-applied first). Marking a job `applied` removes it from **All**
  and surfaces it under **Applied**, so the working list shows only roles still to act on
  (mirrors the existing `dismissed` hide).
- **List view:** ranked cards — score, title, company, location bucket badge,
  **posted date + "Xd ago" age badge** (prominent, since recency matters), top
  matching skills, "new since last poll" indicator.
- **Filters & sort:** location bucket, source, seniority, min score, status, age
  (e.g. ≤7d / ≤14d / ≤21d). Sort toggle: **best match** (default) or **newest first**.
- **Detail view:** full description, score breakdown, direct apply link (opens posting).
- **Status tracking:** per-job status (new/interested/applied/dismissed) persisted in DB.
  Marking a job `applied` also triggers the optional Google Sheet tracking sync (§15).
- **Styling:** a polished, modern card/tab visual treatment — denser, clearer hierarchy,
  refined typography and badges — without becoming bulky (no heavier framework; still the
  single static page + vanilla JS + plain CSS of HLD §3.5).
- Runs at `http://localhost:8000`. No auth (local only). No external calls from the
  browser; frontend talks only to local backend.

## 10. CLI

- `jobfinder poll` — fetch all sources, normalize, dedupe, store, score.
- `jobfinder serve` — start the dashboard.
- `jobfinder add-company <ats> <token>` — append to companies.yaml.
- `jobfinder export [--csv path]` — dump current ranked matches.

## 11. Repo layout

```
jobfinder/
  pyproject.toml / requirements.txt
  .env.example
  config/
    profile.yaml
    companies.yaml
    resume.pdf          # user's full resume (pdf/docx/txt/md); gitignored
  src/jobfinder/
    sources/            # base.py + greenhouse.py, lever.py, ashby.py, adzuna.py...
    normalize.py
    db.py
    score.py
    filters.py
    cli.py
    web/                # FastAPI app + static frontend
  tests/
  data/                 # jobs.db, gitignored
  README.md
```

## 12. Milestones (Ralph loop targets — implement in order)

Each milestone has acceptance criteria the loop must satisfy (and ideally a test)
before moving on. "Done" = criteria met AND `pytest` green AND `ruff` clean.

### M1 — Skeleton + DB
- Repo scaffolding, deps pinned, `pip install -e .` works.
- SQLite schema + `db.py` with upsert + dedupe by `id`.
- **Accept:** `jobfinder --help` runs; inserting the same job twice yields one row.

### M2 — Greenhouse + Lever sources + normalizer
- `Source` interface; Greenhouse and Lever implementations; HTML→text; schema mapping.
- Location bucketing + seniority inference heuristics + posted-date parsing per source.
- **Accept:** `jobfinder poll` against 3 seed companies stores normalized jobs;
  unit tests cover bucketing (remote/vancouver/toronto/other), seniority parsing, and
  posted-date extraction with fixture payloads (committed sample JSON, no live calls
  in tests).

### M3 — Resume ingestion + filters + scoring
- `sentence-transformers` integration (model auto-downloaded once, cached locally).
- Full-resume extraction (pdf/docx/txt/md) → chunked, mean-pooled profile vector,
  combined with the weighted targeting block from `profile.yaml`.
- Eligibility filter per §7, with the **21-day recency gate enforced before embedding**
  (stale jobs never reach the model).
- Cosine scoring + skill/location/recency-decay boosts; component breakdown stored.
- **Accept:** given fixture jobs + a sample resume + profile,
  (a) a posting older than 21 days is dropped before scoring (asserted: it never gets
      embedded);
  (b) scores are deterministic and ranked sensibly — a senior remote Java/AWS role
      outranks a junior onsite frontend role;
  (c) of two near-identical strong matches, the **more recently posted one ranks higher**
  — all in committed tests.

### M4 — Dashboard
- FastAPI endpoints + SPA per §9; status persistence; poll trigger.
- **Accept:** `jobfinder serve` → localhost:8000 lists ranked jobs, filters work,
  marking a job "dismissed" hides it and persists across restart.

### M5 — Ashby + Adzuna + discovery
- Ashby source; Adzuna aggregator behind `.env` key (skips cleanly if absent);
  board-token auto-discovery appends to companies.yaml.
- Throttling + response caching to respect rate limits.
- **Accept:** with no Adzuna key, poll still succeeds using direct ATS feeds; with a
  key, Canadian backend roles appear and at least one new board token is discovered.

### M6 — Polish
- README with setup, cron/Task Scheduler instructions, model-swap note.
- CSV export. Graceful error handling per source (one source failing ≠ whole poll fails).
- **Accept:** killing network mid-poll leaves DB consistent; README lets a fresh
  clone reach a running dashboard.

### M7 — Applied tracking, Sheets sync, remote filter & UI polish
*(Post-v1 enhancement milestone, added after the M1–M6 product shipped. Targets the
four user-requested improvements; see §7, §9, §15 and tasks T29–T33.)*
- **Stricter remote filter:** non-Canada remote postings ("Remote — US", "Remote, EMEA",
  etc.) bucket `other` and drop out of the eligible list (§7).
- **Applied tab:** `applied` jobs are hidden from the default list and shown under a
  dedicated tab; the default listing now hides `applied` as well as `dismissed` (§9).
- **Google Sheet sync:** marking a job `applied` appends a row (Company, Position, blank
  Response cell shaded **yellow**, Link) to the user's tracking sheet, deduped, skipped
  cleanly when unconfigured (§15).
- **UI restyle:** a more polished, non-bulky card/tab visual treatment (§9).
- **Accept:** a US-only remote fixture buckets `other` and is excluded; marking a job
  `applied` removes it from **All**, shows it under **Applied**, and (when configured)
  appends exactly one correctly-formatted row to a fake Sheets endpoint — with no
  credentials, the status write still succeeds and the sync is skipped; the dashboard
  renders the two tabs with the new styling. All tests green, `ruff` clean.

## 13. Definition of Done (overall)

- `git clone` → follow README → `jobfinder poll` → `jobfinder serve` shows a ranked
  list of real, eligible Canadian/remote backend roles scored against the resume,
  with working filters and status tracking, running entirely free and locally,
  with all tests green and no source able to crash the whole run.

## 14. Ralph-loop operating notes

- Work strictly milestone-by-milestone; do not start Mn+1 until Mn acceptance passes.
- After each change: run `pytest` and `ruff`; if red, fix before proceeding.
- Tests must use committed fixtures, never live network calls (keeps the loop
  deterministic and free).
- If blocked by a missing real-world fact (e.g. a company's exact board token),
  insert a clearly-marked `# TODO verify` placeholder and continue; surface all such
  TODOs in the README so the user can confirm.
- Keep a `PROGRESS.md` updated with milestone status each iteration.

## 15. Application tracking — Google Sheet sync (M7)

When the user marks a job **`applied`** in the dashboard, the tool appends a row to the
user's personal job-search tracking spreadsheet so the sheet stays in lockstep with the
app — no manual re-entry.

**Target sheet (the user's existing tracker).** Columns, in order:
`Company | Position | Response | Link`. The **Response** column is the user's
outcome tracker, *color-coded*: a freshly-applied row is left blank with the
**Response cell shaded yellow** ("applied, waiting to hear back"); the user later recolors
it as they hear back. So the sync writes:
- **Company** ← job company
- **Position** ← job title
- **Response** ← *empty text, cell background set to yellow* (the convention's "waiting" state)
- **Link** ← canonical posting URL

**Behavior:**
- Server-side only. The browser never calls Google; the local backend performs the write
  when it receives the `applied` status update (preserves "browser talks only to the local
  backend", §9). The status write is the source of truth — it **always** persists; the
  Sheet sync is a **best-effort** side effect (a Sheets failure is logged and surfaced, but
  never rolls back the status or 500s the request).
- **Idempotent:** before appending, check whether a row with the same **Link** already
  exists; if so, skip (re-marking `applied`, or a retry, never duplicates a row).
- **Opt-in & graceful:** active only when Google credentials + a sheet id are configured
  (see below). With them absent, marking `applied` still works locally and the sync is
  skipped cleanly with an info note — exactly the Adzuna-key pattern (§5, M5).

**Auth & config (no secrets in code; $0).** A **Google Cloud service account** (free):
its JSON key path and the target sheet id come from `.env`
(`GOOGLE_SHEETS_CREDENTIALS`, `JOB_TRACKER_SHEET_ID`, optional worksheet/`gid`); the key
file is **gitignored**. The user shares the sheet (Editor) with the service account's
email once. The implementation signs a service-account JWT with the lightweight
`google-auth` library to obtain an access token, then calls the **Sheets REST API**
(`spreadsheets:batchUpdate` with an `appendCells` request that sets the four cell values
**and** the Response cell's yellow `backgroundColor` in one call) over the **existing
`httpx`** client — no heavyweight Google SDK, keeping footprint minimal (HLD §3.7).

This is the only outbound write the tool performs, and it targets the **user's own**
spreadsheet — it is **not** an application submission (the §3 no-auto-apply guardrail
still holds in full).
