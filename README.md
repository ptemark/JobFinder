# Job Finder

A local, single-user tool that discovers recent **backend software-engineering** job
postings from public ATS feeds (Greenhouse, Lever, Ashby) and keyless job aggregators
(Remotive, The Muse, and optionally Adzuna), filters them to your targeting criteria,
scores each posting against your full résumé using free local embeddings, and presents
the ranked matches in a local web dashboard. **You apply manually** via the linked
posting — the tool never submits an application.

- **$0 to run.** No paid APIs, no cloud hosting. Everything runs on your machine.
- **Local & private.** The dashboard binds to loopback only; your résumé, database and
  logs never leave the machine. `config/resume.*`, `.env` and `data/` are gitignored.
- **Fresh results only.** Postings older than `max_age_days` (default 21) are out of
  scope — never shown, scored, or embedded.
- **Read-only.** Only public JSON ATS feeds are fetched; nothing is ever POSTed to an
  application endpoint.

---

## Requirements

- **Python 3.11+**
- ~300 MB of disk for the embedding model, downloaded **once** on the first poll and
  cached locally (offline thereafter).

The heavy dependency is `sentence-transformers` (+ CPU torch); everything else is light.

---

## Quick start

From a fresh clone:

```bash
# 1. Install the package and its dependencies (editable install).
pip install -e .

# 2. Scaffold config/ from the examples, create data/, and build the database.
jobfinder init

# 3. Add your résumé. Supported: pdf, docx, txt, md. Stays local (gitignored).
cp /path/to/your_resume.pdf config/resume.pdf

# 4. Edit your targeting and the companies to poll (see "Configuration" below).
$EDITOR config/profile.yaml
$EDITOR config/companies.yaml

# 5. Run a poll: fetch → normalize → filter → score → store.
#    The first run downloads the embedding model (one time).
jobfinder poll

# 6. Start the dashboard and open it in your browser.
jobfinder serve
# → http://127.0.0.1:8000
```

`jobfinder init` copies each `*.example` to its live counterpart and **never clobbers**
an existing file, so it is safe to re-run.

> Prefer [`uv`](https://docs.astral.sh/uv/)? `uv sync` then prefix commands with
> `uv run` (e.g. `uv run jobfinder poll`). `uv` is the project's dependency manager;
> `pip install -e .` works for everyone else.

---

## Configuration

`jobfinder init` creates four files under the repo root. All are gitignored.

### `config/profile.yaml` — your targeting

| Key | Meaning |
|---|---|
| `role_keywords` | Any match marks a posting as a backend role (eligibility gate). |
| `must_have_skills` | Skills bonus-weighted heavily so they steer ranking (default: java, kotlin, python, aws). |
| `seniority_include` / `seniority_exclude` | Levels to keep / drop. |
| `locations_priority` | Buckets in priority order: remote > vancouver > toronto > other_canada. |
| `max_age_days` | **Hard** recency cutoff in days (default 21). Older postings are never shown, scored, or embedded. |
| `retention_days` | Delete jobs not seen in this many days, to keep the DB small (default 30). |
| `resume_path` | Path to your full résumé. |
| `embed_model` | Embedding model name (see **Model swap** below). |
| `role_keyword_required` | Require a `role_keywords` match to pass the role gate (default true). |

### `config/weights.yaml` — ranking weights

The final 0–100 score is a normalized weighted sum of four components. Defaults:

```yaml
semantic: 0.35   # résumé ↔ posting cosine similarity
skill:    0.30   # heavy — Java/Kotlin/Python/AWS steer the match
location: 0.20   # remote > vancouver > toronto > other_canada
recency:  0.15   # newer postings rank above older within the cutoff window
```

At least one weight must be greater than 0.

### `config/companies.yaml` — which boards to poll

The fetch universe is a list of ATS **board tokens**, keyed by source. A token is the
board slug in the feed URL:

| ATS | Feed URL pattern |
|---|---|
| greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs` |
| lever | `api.lever.co/v0/postings/{token}` |
| ashby | `api.ashbyhq.com/posting-api/job-board/{token}` |

Add a verified token from the CLI (dedupes on token, promotes to verified):

```bash
jobfinder add-company greenhouse shopify --name Shopify
```

#### ⚠️ Verify the starter seeds

The shipped `companies.yaml.example` includes plausible Canadian / remote-friendly
employers, but **their exact board tokens are unconfirmed** (marked `verified: false`
with a `# TODO verify` comment). Confirm each against the live feed before trusting it,
then set `verified: true`:

| ATS | Token | Company |
|---|---|---|
| greenhouse | `shopify` | Shopify |
| greenhouse | `benevity` | Benevity |
| greenhouse | `clio` | Clio |
| lever | `jobber` | Jobber |
| lever | `thinkific` | Thinkific |
| ashby | `wealthsimple` | Wealthsimple |

To check a token, open the feed URL above in a browser — a valid token returns JSON job
data, an invalid one returns an error.

### Aggregator sources (no config required)

Two cross-employer aggregators run automatically with **no keys** — they're the easiest
way to "find anything that fits" rather than naming companies:

| Source | Coverage | Config |
|---|---|---|
| **Remotive** | Remote software-dev roles (great for the remote bucket) | none |
| **The Muse** | Software-engineering roles across Vancouver, Toronto, Ottawa, Montreal + remote | optional `THEMUSE_API_KEY` raises the rate limit |
| **Adzuna** | Broad Canadian aggregator | requires both keys (below) |

The pipeline's role gate + résumé scoring narrow every aggregator's results down to
relevant backend roles, so they're safe to run wide.

### `.env` — optional secrets

Used for the **optional** Adzuna aggregator and the **optional** The Muse key. Leave any
blank to run without them — Adzuna is skipped cleanly when its keys are absent, and The
Muse simply runs key-free at a lower rate limit. Both Adzuna keys are required for it to
enable. Free-tier keys: Adzuna <https://developer.adzuna.com/>, The Muse
<https://www.themuse.com/developers/api/v2>.

```dotenv
ADZUNA_APP_ID=
ADZUNA_APP_KEY=
THEMUSE_API_KEY=
```

Operational tunables (throttle, cache TTL, paths) can be overridden with `JOBFINDER_*`
environment variables, but the defaults are sensible.

---

## Model swap (quality vs. speed)

The default embedding model is **`all-MiniLM-L6-v2`** — fast and CPU-friendly. For
higher-quality matching at the cost of speed and a larger download, switch to
**`all-mpnet-base-v2`** by editing `config/profile.yaml`:

```yaml
embed_model: "all-mpnet-base-v2"
```

The new model downloads once on the next poll. Re-poll afterward so jobs are re-embedded
with the new model.

---

## CLI reference

| Command | What it does |
|---|---|
| `jobfinder init` | Scaffold `config/` from examples, create `data/`, build the DB. |
| `jobfinder poll` | Run one poll: fetch, normalize, filter, score, store. |
| `jobfinder poll --no-cache` | Bypass the on-disk HTTP cache for this poll. |
| `jobfinder poll --source greenhouse` | Restrict to named sources (repeatable). |
| `jobfinder serve` | Serve the dashboard (`--host` / `--port`; defaults to `127.0.0.1:8000`). |
| `jobfinder add-company <ats> <token> [--name N]` | Append a verified board token. |
| `jobfinder export [--csv PATH]` | Dump current ranked matches as CSV (stdout if no path). |

Run `jobfinder --help` (or `jobfinder <command> --help`) for full details. Every command
validates its configuration first and fails fast with a clear message.

---

## Scheduling automatic polls

`jobfinder poll` is a short-lived process. Schedule it to run on a cadence so the
dashboard always has fresh results. (Throttling and on-disk caching keep every poll
within free-tier rate limits.)

### Linux / macOS — cron

Run `crontab -e` and add a line. Use absolute paths and point the working directory at
your clone so `config/` and `data/` resolve. Example: every day at 08:00.

```cron
0 8 * * *  cd /absolute/path/to/JobFinder && /absolute/path/to/python -m jobfinder poll >> data/poll.log 2>&1
```

(Find your interpreter with `which python` inside the environment where you ran
`pip install -e .`. If you used `uv`, the entry point is `/path/to/JobFinder/.venv/bin/jobfinder`.)

### macOS — launchd (alternative to cron)

Create `~/Library/LaunchAgents/com.jobfinder.poll.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.jobfinder.poll</string>
  <key>WorkingDirectory</key><string>/absolute/path/to/JobFinder</string>
  <key>ProgramArguments</key>
  <array>
    <string>/absolute/path/to/python</string>
    <string>-m</string>
    <string>jobfinder</string>
    <string>poll</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>8</integer><key>Minute</key><integer>0</integer></dict>
</dict>
</plist>
```

Load it with `launchctl load ~/Library/LaunchAgents/com.jobfinder.poll.plist`.

### Windows — Task Scheduler

Create a daily task that runs the poll. From an elevated PowerShell:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Path\To\python.exe" `
             -Argument "-m jobfinder poll" -WorkingDirectory "C:\Path\To\JobFinder"
$trigger = New-ScheduledTaskTrigger -Daily -At 8:00AM
Register-ScheduledTask -TaskName "JobFinder Poll" -Action $action -Trigger $trigger
```

Or use the Task Scheduler GUI: **Create Basic Task** → trigger *Daily* → action *Start a
program* → program `python.exe`, arguments `-m jobfinder poll`, "Start in" set to your
clone directory.

Leave `jobfinder serve` running separately (or start it on demand) to view results; the
**Poll now** button in the dashboard triggers a poll manually too.

---

## How it works

```
Sources (ATS feeds) → Normalizer → SQLite (dedupe + history) → Scorer (embeddings + filters) → Dashboard
```

- **Sources** are public, read-only JSON feeds behind a common interface, fetched through
  a shared throttled + cached HTTP client.
- **Recency cutoff** is enforced before any embedding work, so stale postings never reach
  the model.
- **Scoring** combines résumé↔posting semantic similarity, must-have skill matches,
  location bucket, and a recency decay into a 0–100 score with a stored component
  breakdown (the dashboard shows *why* a job scored as it did).
- **Resilience:** each source runs in a bulkhead — one source failing never aborts the
  poll; the error is recorded in the run summary and surfaced in the dashboard.

Data lives in `data/jobs.db` (SQLite). Job status (new / interested / applied /
dismissed) persists across restarts.

---

## Development

This project uses [`uv`](https://docs.astral.sh/uv/) and `ruff`. The full local CI
mirrors the GitHub Actions pipeline:

```bash
uv run ruff format --check .
uv run ruff check .
uv run pytest -q
```

Tests use committed fixtures only — they never make live network calls.
