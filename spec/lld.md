# Low-Level Design — Personal Job Discovery & Matching Tool

**Status:** Draft v1
**Companion docs:** `job-finder-spec.md` (requirements), `job-finder-hld.md` (architecture)
**Scope of this document:** Implementation-level detail — module/file layout, concrete
interfaces and signatures, exact source contracts and field mappings, the SQLite DDL,
the scoring math, API request/response schemas, config schemas, algorithms in
pseudocode, error/retry semantics, and the test matrix. This is the document an
engineer (or a Claude Code + Ralph loop) implements directly against.

> ATS endpoint shapes and field names below were verified against live 2026 responses
> (Greenhouse, Lever, Ashby). Treat the documented field names as the contract; guard
> every access defensively since employer payloads vary.

---

## 1. Project layout (authoritative)

```
jobfinder/
  pyproject.toml
  requirements.txt              # pinned
  .env.example
  README.md
  config/
    profile.yaml
    companies.yaml
    weights.yaml                # scoring weights (separate so tuning ≠ profile edits)
    resume.<pdf|docx|txt|md>    # gitignored
  src/jobfinder/
    __init__.py
    settings.py                 # pydantic-settings: env + paths
    models.py                   # dataclasses/pydantic: Job, RawPosting, Score, ...
    sources/
      __init__.py
      base.py                   # Source protocol + SourceResult + registry
      greenhouse.py
      lever.py
      ashby.py
      adzuna.py
      http.py                   # shared httpx client, throttle, cache, retry
    normalize.py                # raw -> Job; html_to_text; bucket_location; infer_seniority; parse_date
    filters.py                  # eligibility gates
    score.py                    # embeddings + weighted scoring
    store.py                    # SQLite DAL (schema, upserts, queries)
    pipeline.py                 # orchestrates poll
    discovery.py                # extract board tokens from aggregator URLs
    sheets.py                   # M7: Google Sheet application-tracker sync (google-auth + httpx)
    web/
      __init__.py
      app.py                    # FastAPI app factory
      api.py                    # routers
      schemas.py                # request/response pydantic models
      static/
        index.html
        app.js
        styles.css
    cli.py                      # typer entrypoints
  tests/
    fixtures/                   # committed JSON/XML payloads, sample resume
    test_normalize.py
    test_filters.py
    test_score.py
    test_store.py
    test_sources.py             # adapters parse fixtures (no network)
    test_api.py
    test_sheets.py              # M7: Sheets sync against a mocked transport (no network)
  data/                         # gitignored: jobs.db, logs/, http_cache/
```

**Entry points (`pyproject.toml`):**
```toml
[project.scripts]
jobfinder = "jobfinder.cli:app"
```

---

## 2. Core data models (`models.py`)

```python
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

class LocationBucket(str, Enum):
    REMOTE = "remote"
    VANCOUVER = "vancouver"
    TORONTO = "toronto"
    OTHER_CANADA = "other_canada"
    OTHER = "other"

class Seniority(str, Enum):
    JUNIOR = "junior"; MID = "mid"; SENIOR = "senior"
    STAFF = "staff"; UNKNOWN = "unknown"

class Status(str, Enum):
    NEW = "new"; INTERESTED = "interested"
    APPLIED = "applied"; DISMISSED = "dismissed"

@dataclass(frozen=True)
class RawPosting:
    source: str
    source_id: str
    payload: dict          # original provider object (verbatim)

@dataclass
class Job:
    id: str                # sha1(f"{source}:{source_id}")[:16]
    source: str
    source_id: str
    company: str
    title: str
    description: str       # plain text, HTML stripped
    location_raw: str
    is_remote: bool
    location_bucket: LocationBucket
    seniority: Seniority
    url: str               # canonical posting/apply URL
    posted_at: datetime | None
    date_unknown: bool
    first_seen_at: datetime
    last_seen_at: datetime
    embedding: bytes | None = None     # float32 LE blob
    raw: dict = field(default_factory=dict)

@dataclass
class ScoreBreakdown:
    final: float           # 0..100
    semantic: float        # 0..1 cosine
    skill: float           # 0..1
    location: float        # 0..1
    recency: float         # 0..1
    scored_at: datetime
```

`id` derivation is the dedupe key (HLD §4.4). Same `(source, source_id)` always maps to
the same row; re-polls upsert.

---

## 3. Source adapters

### 3.1 Protocol (`sources/base.py`)

```python
from typing import Protocol, Iterable

class Source(Protocol):
    name: str
    def fetch(self, *, max_age_days: int, throttle_s: float) -> "SourceResult": ...

@dataclass
class SourceResult:
    source: str
    raw: list[RawPosting]          # already recency-filtered where the API allows
    fetched: int                   # total returned by provider
    kept_after_recency: int        # after dropping stale (where date available pre-normalize)
    errors: list[str]
```

Registry: `SOURCES: dict[str, Callable[[Settings], Source]]` so the pipeline iterates
enabled sources by config. A source whose required secret is absent (e.g. Adzuna)
registers but `fetch` returns an empty `SourceResult` with a note, never raises.

### 3.2 Shared HTTP (`sources/http.py`)

- One `httpx.Client` with `timeout=httpx.Timeout(10.0, connect=5.0)`, `http2=True`,
  a descriptive `User-Agent`.
- **Throttle:** monotonic-clock gate ensuring ≥ `throttle_s` (default 1.0) between
  calls *per host*.
- **Retry:** max 3 attempts on `{429, 500, 502, 503, 504}` and connect/read timeouts;
  exponential backoff `0.5 * 2**n` with jitter; honors `Retry-After` on 429.
- **Cache:** on-disk JSON cache under `data/http_cache/`, key = sha1(url), TTL default
  6h (config). Cache hit skips network and throttle. `--no-cache` flag bypasses.

```python
def get_json(url: str, *, params: dict | None = None, ttl_s: int = 21600) -> Any: ...
def get_text(url: str, *, params: dict | None = None, ttl_s: int = 21600) -> str: ...  # XML
```

### 3.3 Greenhouse (`sources/greenhouse.py`)

- **Endpoint:** `GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true`
  (no auth). Returns `{"jobs": [ ... ]}`. **No server-side filtering** → fetch full
  board, drop stale before returning.
- **Field map** (verified):

| Job field | Greenhouse path | Notes |
|---|---|---|
| `source_id` | `id` | integer → str |
| `title` | `title` | |
| `description` (HTML) | `content` | HTML-encoded; needs unescape + strip (needs `?content=true`) |
| `location_raw` | `location.name` | |
| `url` | `absolute_url` | |
| `posted_at` | `updated_at` | ISO8601 w/ offset; Greenhouse exposes no separate created date publicly → use `updated_at` |
| `company` | `company_name` (fallback: board token) | |

- Recency: parse `updated_at`; if older than `max_age_days`, exclude pre-normalize.

### 3.4 Lever (`sources/lever.py`)

- **Endpoint:** `GET https://api.lever.co/v0/postings/{site}?mode=json&limit=100`
  (no auth). Returns a JSON array. Supports source-side params (`location`,
  `commitment`, `team`, `level`, `skip`, `limit`) — use `limit` + `skip` to paginate.
- **Field map** (verified):

| Job field | Lever path | Notes |
|---|---|---|
| `source_id` | `id` | |
| `title` | `text` | |
| `description` (HTML) | `descriptionPlain` if present else `description` | prefer plain |
| `location_raw` | `categories.location` | |
| `url` | `hostedUrl` | apply = `applyUrl` |
| `posted_at` | `createdAt` (epoch ms) | → UTC datetime |
| `company` | from companies.yaml entry | Lever payload has no company name |
| salary (optional) | `salaryRange` | `{currency,min,max}` |

### 3.5 Ashby (`sources/ashby.py`)

- **Endpoint:** `GET https://api.ashbyhq.com/posting-api/job-board/{job_board_name}?includeCompensation=true`
  (no auth). Returns `{"jobs": [ ... ]}` (guard for shape).
- **Field map** (verified):

| Job field | Ashby path | Notes |
|---|---|---|
| `source_id` | `id` | uuid |
| `title` | `title` | |
| `description` | `descriptionPlain` / `descriptionHtml` | prefer plain |
| `location_raw` | `location` | plus `workplaceType` ("Remote"/"Hybrid"/"OnSite") |
| `is_remote` | `workplaceType == "Remote"` | strong remote signal |
| `url` | `jobUrl` | apply = `applyUrl` |
| `posted_at` | `publishedAt` / `updatedAt` | whichever present |
| compensation | `compensation.scrapeableCompensationSalarySummary` | |

### 3.6 Adzuna (`sources/adzuna.py`) — optional aggregator

- **Endpoint:** `GET https://api.adzuna.com/v1/api/jobs/ca/search/{page}` with
  `app_id`, `app_key` (from `.env`), `what="backend software engineer"`,
  `where`/`category` as configured, and **`max_days_old=max_age_days`** so the source
  itself excludes stale postings.
- Personal-use only; throttle hard, cache aggressively, stay within free tier. If keys
  absent → skip cleanly.
- Used for (a) coverage beyond the seed list and (b) **board-token discovery**
  (`discovery.py`): scan `redirect_url`/company URLs for `boards.greenhouse.io/{token}`,
  `jobs.lever.co/{site}`, `jobs.ashbyhq.com/{board}` and append unverified entries to
  `companies.yaml`.

---

## 4. Normalization (`normalize.py`)

Pure functions; no I/O; fully unit-tested against fixtures.

```python
def normalize(raw: RawPosting, *, company_hint: str | None, now: datetime) -> Job
def html_to_text(html: str) -> str            # selectolax: parse, drop script/style, get_text, collapse ws
def parse_date(value, source: str) -> datetime | None   # handles ISO8601±offset and epoch ms
def bucket_location(location_raw: str, is_remote: bool) -> tuple[LocationBucket, bool]
def infer_seniority(title: str, description: str) -> Seniority
```

### 4.1 `bucket_location` rules (ordered) — **tightened in M7 (T29)**
1. Remote signal = `is_remote` true OR `/remote/i` in location. When remote:
   a. **Positive Canada signal** (`/canada|remote\s*-?\s*(canada|north america)|anywhere/i`,
      or a Canadian city/`bc`/`on` cue) → `REMOTE`.
   b. **Positive non-Canada signal** — a remote posting that names *any* non-Canada
      country/region → `OTHER` (still remote). This is the M7 change: replace the narrow
      `/remote.*(us only|united states only|emea)/i` with a broad non-Canada matcher,
      e.g. `/\b(us|usa|u\.s\.|united states|emea|latam|apac|uk|europe|india|us-based|us only)\b/i`
      (word-boundary; guard against matching Canadian provinces). So "Remote — US",
      "Remote (United States)", "Remote, EMEA", "US-based — Remote" all bucket `OTHER`.
   c. **No country named at all** → `REMOTE` (Canada-eligible by default, unchanged — a
      bare "Remote" stays in scope; the debug/include-ineligible toggle still surfaces
      anything wrongly dropped, HLD §3.4).
2. `/vancouver|, ?bc|british columbia/i` → `VANCOUVER`.
3. `/toronto|, ?on|ontario/i` → `TORONTO`.
4. `/canada|montreal|calgary|ottawa|…/i` → `OTHER_CANADA`.
5. else → `OTHER`.
(`is_remote` returned alongside.) **Order matters:** check the non-Canada matcher (1b)
*before* defaulting to `REMOTE` (1c), and ensure the Canada signal (1a) wins when both a
Canada cue and a stray token co-occur (e.g. "Remote - Canada & US"). Tests cover
"Remote — US", "Remote (United States)", "Remote, EMEA", "US-based", "Remote LATAM"
→ `OTHER`, and "Remote - Canada", "Remote (North America)", bare "Remote" → `REMOTE`.

### 4.2 `infer_seniority` rules (ordered; first match wins)
- `/principal|director|vp|head of|manager\b/i` → treat as out-of-scope IC check upstream
  (returns `STAFF` only if `/staff|principal engineer/i` clearly IC; else flag for filter).
- `/staff/i` → `STAFF`; `/senior|sr\.?|lead/i` → `SENIOR`;
  `/intern|new ?grad|graduate|junior|jr\.?|entry/i` → `JUNIOR`;
  `/mid|intermediate|ii\b|2\b/i` → `MID`; else `UNKNOWN`.

### 4.3 `parse_date`
- Greenhouse/Ashby: ISO8601 with offset → `datetime.fromisoformat`, convert to UTC.
- Lever: epoch ms → `datetime.fromtimestamp(v/1000, tz=UTC)`.
- Failure → `None`, caller sets `date_unknown=True`.

---

## 5. Filtering (`filters.py`)

Applied **in order, cheapest first**; short-circuits before embedding.

```python
def is_eligible(job: Job, *, profile: Profile, now: datetime) -> tuple[bool, str | None]:
    # 1. recency
    if job.posted_at and (now - job.posted_at).days > profile.max_age_days:
        return False, "stale"
    # 2. role gate (keyword pre-check; semantic gate happens in scorer threshold)
    if not _role_keywords(job): return False, "not_backend_role"
    # 3. location
    if job.location_bucket == LocationBucket.OTHER: return False, "location_out"
    # 4. seniority
    if job.seniority in {Seniority.JUNIOR} or _is_people_manager(job):
        return False, "seniority_out"
    return True, None
```
- `date_unknown` jobs pass the recency gate (kept, ranked low — never silently dropped).
- Ineligible jobs are **still stored** with `eligible=0` and the reason, so the
  dashboard debug toggle can surface false negatives.

---

## 6. Scoring (`score.py`)

### 6.1 Model loading
```python
model = SentenceTransformer(settings.embed_model)   # default "all-MiniLM-L6-v2"
# swappable to "all-mpnet-base-v2" via config; downloaded once, cached in HF cache dir
```

### 6.2 Profile vector (built once per poll)
```
resume_text   = extract_resume(config/resume.*)       # full text, see 6.5
targeting_txt = render_targeting(profile)             # role + must-have skills + seniority
profile_text  = targeting_txt + "\n\n" + resume_text  # targeting prepended (dominates)
chunks        = chunk(profile_text, max_tokens≈256)   # respect model input limit
profile_vec   = l2_normalize(mean(model.encode(chunks, normalize_embeddings=True)))
```

### 6.3 Per-job vector & components
```
job_text   = f"{title}\n{description}"[:CHAR_CAP]
job_vec    = model.encode(job_text, normalize_embeddings=True)   # only for eligible+new jobs
semantic   = cosine(profile_vec, job_vec)            # in [−1,1] → clamp to [0,1]

skill      = min(1.0, hits/needed) where hits = count of must-have skills
             {java, kotlin, python, aws} matched in title+description (word-boundary, case-insensitive)
location   = {remote:1.0, vancouver:0.85, toronto:0.7, other_canada:0.4, other:0.0}[bucket]
recency    = clamp(1 - age_days / max_age_days, 0, 1)   # linear decay over 0..21d; date_unknown → 0.3
```

### 6.4 Final score
```
final01 = (w.semantic*semantic + w.skill*skill + w.location*location + w.recency*recency)
          / (w.semantic + w.skill + w.location + w.recency)
final   = round(100 * final01, 1)
```
Default weights (`weights.yaml`), tuned so stated priorities dominate raw similarity:
```yaml
semantic: 0.35
skill:    0.30      # heavy — Java/Kotlin/Python/AWS must steer
location: 0.20
recency:  0.15
```
Store full `ScoreBreakdown`. Re-embedding is skipped when a job's `id` already has an
embedding and its content hash is unchanged (perf, HLD §5.4).

### 6.5 Resume extraction
```python
def extract_resume(path) -> str:
    # .pdf  -> pypdf; fallback pdfplumber on empty/garbled text
    # .docx -> python-docx (paragraphs + tables)
    # .txt/.md -> read_text(utf-8)
    # returns full text; raises clear error if file missing
```

---

## 7. Persistence (`store.py`)

### 7.1 PRAGMAs (on connect)
```sql
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

### 7.2 DDL
```sql
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  source TEXT NOT NULL, source_id TEXT NOT NULL,
  company TEXT, title TEXT NOT NULL, description TEXT,
  location_raw TEXT, is_remote INTEGER, location_bucket TEXT,
  seniority TEXT, url TEXT,
  posted_at TEXT, date_unknown INTEGER DEFAULT 0,
  eligible INTEGER DEFAULT 1, ineligible_reason TEXT,
  content_hash TEXT,
  embedding BLOB,
  first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  raw_json TEXT,
  UNIQUE(source, source_id)
);
CREATE TABLE IF NOT EXISTS scores (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  final REAL, semantic REAL, skill REAL, location REAL, recency REAL,
  scored_at TEXT
);
CREATE TABLE IF NOT EXISTS status (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  state TEXT NOT NULL DEFAULT 'new', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS poll_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at TEXT, finished_at TEXT, per_source_json TEXT
);
CREATE TABLE IF NOT EXISTS companies (
  ats TEXT NOT NULL, token TEXT NOT NULL, name TEXT,
  verified INTEGER DEFAULT 0, added_at TEXT,
  PRIMARY KEY (ats, token)
);
CREATE INDEX IF NOT EXISTS ix_jobs_posted ON jobs(posted_at);
CREATE INDEX IF NOT EXISTS ix_jobs_bucket ON jobs(location_bucket);
CREATE INDEX IF NOT EXISTS ix_jobs_elig   ON jobs(eligible);
CREATE INDEX IF NOT EXISTS ix_scores_final ON scores(final);
```

### 7.3 Key operations
```python
def upsert_job(conn, job: Job) -> None       # ON CONFLICT(id) DO UPDATE; preserves first_seen_at, bumps last_seen_at
def save_score(conn, job_id, sb: ScoreBreakdown) -> None
def set_status(conn, job_id, state: Status) -> None
def query_jobs(conn, *, filters: JobФilters, sort: str) -> list[Row]   # joins scores+status
def prune(conn, *, not_seen_days: int) -> int   # delete jobs with last_seen_at older than N
def start_run(conn)->int; def finish_run(conn, run_id, per_source: dict)->None
```
`upsert_job` uses `INSERT ... ON CONFLICT(source, source_id) DO UPDATE SET ...` so the
poll is **idempotent** and crash-safe (HLD §4.4). New-since-last-poll is computed by
comparing `first_seen_at` to the previous run's `finished_at`.

---

## 8. Pipeline (`pipeline.py`)

```
def run_poll(settings) -> RunSummary:
    run_id = store.start_run()
    profile = load_profile(); profile_vec = score.build_profile_vector(profile)
    per_source = {}
    for src in enabled_sources(settings):
        try:
            res = src.fetch(max_age_days=profile.max_age_days, throttle_s=settings.throttle_s)
        except Exception as e:
            per_source[src.name] = {"error": repr(e)}; log.exception(...); continue   # bulkhead
        kept = 0
        for raw in res.raw:
            job = normalize(raw, company_hint=..., now=now)
            ok, reason = filters.is_eligible(job, profile=profile, now=now)
            job.eligible, job.ineligible_reason = ok, reason
            if ok and (new_or_changed(job)):
                job.embedding = score.embed_job(job)
                store.save_score(job.id, score.score_job(job, profile_vec, profile))
            store.upsert_job(job); kept += 1
        per_source[src.name] = {"fetched": res.fetched, "kept": kept, "errors": res.errors}
    discovery.harvest_tokens(...)        # append unverified companies
    store.prune(not_seen_days=settings.retention_days)   # default 30
    store.finish_run(run_id, per_source)
    return RunSummary(...)
```
One source failing never aborts the run. The poll is a short-lived process invoked by
cron/Task Scheduler (HLD §2.2).

---

## 9. Web API (`web/`)

FastAPI app; binds `127.0.0.1:8000`; serves `static/` at `/`.

### 9.1 Endpoints

| Method | Path | Query / body | Response |
|---|---|---|---|
| GET | `/api/jobs` | `bucket, source, seniority, min_score, status, max_age_days, sort∈{best,newest}, include_ineligible, limit, offset` | `JobListResponse` |
| GET | `/api/jobs/{id}` | — | `JobDetailResponse` (full desc + breakdown) |
| POST | `/api/jobs/{id}/status` | `{ "state": "interested\|applied\|dismissed\|new" }` | `{ "ok": true, "sheet_synced": bool }` |
| POST | `/api/poll` | — | `202 {"run_id": n}` (spawns pipeline subprocess, non-blocking) |

`POST .../status` with `state=applied` **also** fires the Sheets sync (§16): the status is
written first (authoritative), then `sheets.sync_applied(job)` runs best-effort — its
outcome surfaces as `sheet_synced` (false when unconfigured or on a handled Sheets error),
but a Sheets failure never 500s the status write. Other states don't call Sheets.
| GET | `/api/runs/latest` | — | last run summary + counts |

### 9.2 Response schemas (`schemas.py`)
```python
class JobCard(BaseModel):
    id: str; title: str; company: str
    location_bucket: str; is_remote: bool
    posted_at: datetime | None; age_days: int | None; date_unknown: bool
    score: float; matched_skills: list[str]
    status: str; is_new_since_last_poll: bool
    url: str

class JobListResponse(BaseModel):
    items: list[JobCard]; total: int

class JobDetail(JobCard):
    description: str
    breakdown: dict   # {semantic, skill, location, recency, final}
```
- Default sort `best` = `scores.final DESC, posted_at DESC NULLS LAST`.
- `newest` = `posted_at DESC NULLS LAST, scores.final DESC`.
- `include_ineligible=false` by default (debug toggle to surface filtered-out jobs).
- **Default listing hides `applied` and `dismissed`** (M7/T30): when no explicit `status`
  filter is given, `_job_where` excludes both states (extend the existing dismissed-hide
  in `store._job_where` from `!= dismissed` to `NOT IN (dismissed, applied)`; add a
  module constant `_APPLIED_STATE` beside `_DISMISSED_STATE`). An explicit `status=applied`
  still returns them — that query backs the **Applied** tab. `get_job_detail` (by id) keeps
  an applied/dismissed job reachable in detail. `StatusResponse` gains `sheet_synced: bool`.

### 9.3 Frontend (`static/`)
- Single `index.html` + vanilla `app.js` (fetch → render cards) + `styles.css`. No build
  step (HLD §3.5). Card shows score, title, company, location badge, **"Xd ago"** age
  badge, matched-skill chips, NEW indicator, apply link. Sidebar filters + sort toggle.
  Status buttons POST and optimistically update.
- **Tabs (M7/T33):** two tab buttons above the list — **All** (default) and **Applied** —
  in a `role="tablist"`. Switching tabs re-queries `/api/jobs`: **All** sends no `status`
  (backend hides applied+dismissed); **Applied** sends `status=applied` with `sort=newest`.
  On a successful `applied` POST the card is optimistically removed from **All** (the same
  remove-on-dismiss path already in `handleStatusClick`, extended to `applied`); an
  `applied` confirmation reflects `sheet_synced` (e.g. a small "�added to sheet" / "sheet
  not configured" note). Keep all rendering via `createElement`/`textContent` (no
  `innerHTML`), `handle`-prefixed listeners, no `console.log`.
- **Restyle (M7/T33):** refresh `styles.css` only — tighter card grid, clearer type scale,
  refined badges/score chip, sticky tab bar, subtle elevation/hover; stay within the
  existing CSS-variable palette and the `:focus-visible` ring + text-on-every-badge
  accessibility rules. No new assets, no framework, no build step (keeps the footprint
  claim in HLD §3.5 intact). Colour never the sole signal.

---

## 10. CLI (`cli.py`, typer)

```
jobfinder poll      [--no-cache] [--source greenhouse|lever|ashby|adzuna ...]
jobfinder serve     [--host 127.0.0.1] [--port 8000]
jobfinder add-company <ats> <token> [--name NAME]      # writes companies.yaml (verified=1)
jobfinder export    [--csv PATH] [--min-score N] [--bucket ...]
jobfinder init      # scaffolds config/ from examples, creates data/, runs DDL
```
Each command loads+validates settings first (fail-fast).

---

## 11. Configuration schemas

### 11.1 `profile.yaml`
```yaml
role_keywords: ["backend", "software engineer", "developer", "backend engineer"]
must_have_skills: ["java", "kotlin", "python", "aws"]
seniority_include: ["mid", "senior", "staff"]
seniority_exclude: ["junior", "intern", "manager", "director", "principal_nonic"]
locations_priority: ["remote", "vancouver", "toronto", "other_canada"]
max_age_days: 21
retention_days: 30
resume_path: "config/resume.pdf"
embed_model: "all-MiniLM-L6-v2"   # or all-mpnet-base-v2
role_keyword_required: true
```

### 11.2 `companies.yaml`
```yaml
greenhouse: [{token: "acme", name: "Acme", verified: false}]
lever:      [{token: "acmeco", name: "AcmeCo", verified: false}]
ashby:      [{token: "acme", name: "Acme", verified: false}]
```

### 11.3 `.env.example`
```
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
# Optional M7 application-tracker sync (Google Sheet). Absent → marking "applied"
# still works locally and the sync is skipped. The key file is gitignored.
GOOGLE_SHEETS_CREDENTIALS=        # path to the service-account JSON key, e.g. config/google-service-account.json
JOB_TRACKER_SHEET_ID=            # the spreadsheet id from its URL (.../d/<THIS>/edit)
JOB_TRACKER_SHEET_GID=           # optional worksheet gid (default: first sheet)
```
`.gitignore` must also cover `config/google-service-account.json` (or whatever path the
user sets) and the cached token, alongside the existing `config/resume.*` / `.env`.

### 11.4 `settings.py` (pydantic-settings)
Resolves paths, reads `.env`, exposes `throttle_s`, `cache_ttl_s`, db/log paths,
`embed_model`. Missing optional secret → corresponding source disabled, logged once.
**M7:** add `google_sheets_credentials: Path | None`, `job_tracker_sheet_id: str | None`,
`job_tracker_sheet_gid: int | None` (env aliases above). A `sheets_enabled` helper is true
only when both the credentials path *and* the sheet id are present — the §16 sync checks
it and no-ops (info note) otherwise, exactly like the Adzuna-key gate.

---

## 12. Error handling & logging

- **Logging:** stdlib `logging`, JSON-ish formatter, console + `RotatingFileHandler`
  (`data/logs/jobfinder.log`, 5×1MB). Per-poll INFO line per source with the count
  funnel `fetched → kept_after_recency → eligible → scored`.
- **Network:** timeouts + bounded retry/backoff in `http.py` (§3.2); exhausted retries
  → source-level error recorded, poll continues.
- **Parsing:** every field access guarded (`.get(...)`); a single malformed posting is
  skipped + counted, not fatal.
- **Config:** pydantic validation errors surface a precise message and exit non-zero.
- **DB:** all writes in transactions; `busy_timeout` covers poll/dashboard overlap.

---

## 13. Test matrix (`tests/`, fixtures only — zero network, deterministic, free)

| Test file | Asserts |
|---|---|
| `test_sources.py` | each adapter parses its committed fixture into `RawPosting`s with correct `source_id`; recency pre-filter drops a stale fixture row |
| `test_normalize.py` | `bucket_location` for remote-CA / Vancouver / Toronto / other-CA / US-only-remote; `infer_seniority` across titles; `parse_date` ISO+offset and epoch-ms; `html_to_text` strips tags & entities |
| `test_filters.py` | stale, non-backend, out-of-location, junior, people-manager all rejected with correct reason; `date_unknown` passes |
| `test_score.py` | determinism (fixed seed/model) and **ordering**: senior remote Java/AWS role outranks junior onsite frontend role (spec §12 M3); skill weight makes a Java/AWS role beat a higher-semantic non-stack role |
| `test_store.py` | upsert idempotency (same job twice → one row, `first_seen_at` preserved); cascade delete; prune by `last_seen_at`; query filter/sort SQL |
| `test_api.py` | `/api/jobs` filter+sort params; status POST persists; `include_ineligible` toggle; new-since-last-poll flag; **(M7)** default list hides `applied`+`dismissed`, `status=applied` returns the Applied tab, `applied` POST returns `sheet_synced` and (patched) calls `sheets.sync_applied` once |
| `test_normalize.py` *(M7)* | `bucket_location` tightening: "Remote — US"/"Remote (United States)"/"Remote, EMEA"/"US-based"/"Remote LATAM" → `OTHER`; "Remote - Canada"/"Remote (North America)"/bare "Remote" → `REMOTE`; "Remote - Canada & US" → `REMOTE` (Canada signal wins) |
| `test_sheets.py` *(M7)* | append builds the right `appendCells` request (Company/Position/Link values + **yellow** Response `backgroundColor`); idempotency skips when the Link is already present; unconfigured → no request, returns "skipped"; a Sheets HTTP error is caught and reported, never raised — all via `httpx.MockTransport`, no network, creds faked |

Gate: `pytest` green **and** `ruff` clean = milestone done (spec §12).

---

## 14. Dependencies (pinned ranges, `requirements.txt`)

```
httpx>=0.27,<1.0
selectolax>=0.3,<0.4
sentence-transformers>=3.0,<4.0   # pulls torch (CPU), transformers
numpy>=1.26,<3.0
fastapi>=0.110,<1.0
uvicorn[standard]>=0.29,<1.0
pydantic>=2.6,<3.0
pydantic-settings>=2.2,<3.0
typer>=0.12,<1.0
PyYAML>=6.0,<7.0
pypdf>=4.0,<6.0
pdfplumber>=0.11,<1.0     # fallback resume extraction
python-docx>=1.1,<2.0
google-auth>=2.29,<3.0    # M7: service-account JWT → OAuth2 token for the Sheets sync
pytest>=8.0 ; ruff>=0.4   # dev
```
M7 adds **only** `google-auth` (token signing); the Sheets REST calls reuse the existing
`httpx` client — the heavyweight `google-api-python-client` SDK is deliberately avoided
(HLD §3.7 decision D13). No new vector/native deps.
Footprint note: `sentence-transformers` (via torch) is the heavyweight; it is the one
justified by the core matching requirement. Everything else is light. No native vector
extension in the default install (numpy brute-force cosine; `sqlite-vec` is the
documented scale-out per HLD §4.2).

---

## 15. Build order (maps to spec milestones)
M1 settings/models/store(+DDL) → M2 http/greenhouse/lever/normalize → M3 filters/score
→ M4 web/api/static → M5 ashby/adzuna/discovery/cache+throttle → M6 cli polish/export/
prune/README. Each milestone ships with its fixtures and tests before the next begins.
**M7 (post-v1):** T29 remote-filter tightening (normalize) → T30 applied-hide + Applied-tab
query (store/api) → T31 `sheets.py` client → T32 settings/.env + wire sync into the status
endpoint → T33 frontend tabs + restyle. T29/T30/T33 are independent of T31/T32, so the
filter+tab+UI work lands even if the Sheets credential isn't ready.

---

## 16. Sheets sync (`sheets.py`) — M7 application tracker

Pure-ish module: one public entry point, no global state, fully mockable.

```python
def sync_applied(job: JobRowOrCard, *, settings: Settings,
                 client: HttpClient | None = None, now: datetime | None = None) -> SyncResult
```

`SyncResult` = `{"status": "appended"|"skipped"|"duplicate"|"error", "detail": str}`.
The API maps `status == "appended"` → `sheet_synced=True`, everything else → `False`
(only "error" is a real failure; it is logged, never raised).

### 16.1 Flow
1. **Gate:** if `not settings.sheets_enabled` → return `skipped` (info note, no network) —
   the Adzuna-key pattern (§3.6/§11.4).
2. **Token:** build service-account credentials from `settings.google_sheets_credentials`
   via `google.oauth2.service_account.Credentials.from_service_account_file(..., scopes=
   ["https://www.googleapis.com/auth/spreadsheets"])`; `creds.refresh(Request())` to mint
   an OAuth2 access token (the one thing `google-auth` does for us). Cache the creds object
   across calls; it auto-refreshes near expiry.
3. **Idempotency read:** `GET .../v4/spreadsheets/{id}/values/{tab}!D:D` (the Link column;
   `tab` resolved from `gid` or defaulting to the first sheet's title) through the existing
   `HttpClient` with the `Authorization: Bearer <token>` header. If `job.url` is already in
   the returned column → return `duplicate` (no append).
4. **Append + format (one call):** `POST .../v4/spreadsheets/{id}:batchUpdate` with an
   `appendCells` request on the target sheet:
   ```json
   {"requests": [{"appendCells": {
     "sheetId": <gid>,
     "fields": "userEnteredValue,userEnteredFormat.backgroundColor",
     "rows": [{"values": [
       {"userEnteredValue": {"stringValue": "<company>"}},
       {"userEnteredValue": {"stringValue": "<title>"}},
       {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 0}}},
       {"userEnteredValue": {"stringValue": "<url>"}}
     ]}]
   }}]}
   ```
   The third cell (Response) carries **no value, only the yellow background** — the user's
   "applied, waiting to hear back" convention (spec §15). One request sets all four cells
   atomically. Return `appended`.
5. **Bulkhead:** wrap the network in `try/except (httpx.HTTPError, RefreshError, ...)`;
   on failure return `error` with the message (the API layer logs it). Never raises into
   the request handler — the status write already committed.

### 16.2 Notes
- **Column order is the contract** (`Company | Position | Response | Link` = A|B|C|D);
  `appendCells` writes left-to-right from column A, so the Response cell is always the 3rd
  value. README documents not to reorder the sheet's columns.
- **Yellow** = `{red:1, green:1, blue:0}` (RGB 255,255,0); a `SHEETS_APPLIED_RGB` module
  constant keeps the convention in one place, swappable if the user prefers a softer shade.
- All testing uses `httpx.MockTransport` with a faked token (monkeypatch the
  creds/refresh) — zero network, deterministic, free (spec §14 test rule).
