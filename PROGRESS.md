# Progress — Job Finder

Milestone-by-milestone status, updated each Ralph iteration. The authoritative
task list and acceptance checks live in `spec/tasks.md`; this file is the
human-readable rollup.

## Milestone status

| Milestone | Scope | Status |
|-----------|-------|--------|
| M1 — Skeleton + DB | scaffold, settings, models, SQLite store | done |
| M2 — Sources + normalizer | http client, Greenhouse/Lever, normalize | not started |
| M3 — Resume + filters + scoring | extraction, embeddings, scoring math | not started |
| M4 — Dashboard | FastAPI API + static SPA | not started |
| M5 — Ashby + Adzuna + discovery | extra sources, board-token harvest | not started |
| M6 — Polish | CLI, README, export, hardening | not started |

## Task log

| Task | Status | Notes |
|------|--------|-------|
| T01 — Repo scaffold & packaging | done | uv project; `jobfinder` entry point wired to no-op Typer `app`; deps added per-task (see RALPH.md), full pinned target in requirements.txt (LLD §14); Python pinned to 3.12 for later torch CPU wheels. |
| T02 — Settings & config loading | done | pydantic-settings `Settings` (env + `.env`, `JOBFINDER_*` prefix, paths from `base_dir`); Adzuna secrets via unprefixed `.env` aliases, `adzuna_enabled` gated on both keys. `Profile`/`Weights`/`CompaniesConfig` models with fail-fast `load_*` helpers; `*.example` configs + `.env.example` shipped. 14 tests (valid/malformed/missing-secret). |
| T03 — Core data models | done | `RawPosting` (frozen), `Job`, `ScoreBreakdown` dataclasses + `LocationBucket`/`Seniority`/`Status` enums per LLD §2; `StrEnum` (ruff UP042-mandated, modern equivalent of the LLD's `(str, Enum)`) so members round-trip to the TEXT columns. Stable id via `make_job_id` = `sha1("{source}:{source_id}")[:16]` (+ `Job.make_id` alias). 10 tests: id stability/distinctness/length, enum round-trips, frozen RawPosting, dataclass defaults. |
| T04 — SQLite schema & connection | done | `connect()` applies LLD §7.1 PRAGMAs (WAL, synchronous=NORMAL, busy_timeout=5000, foreign_keys=ON) + `sqlite3.Row` + auto parent-dir; `init_db()` runs the full §7.2 DDL via `executescript` (all `IF NOT EXISTS` → idempotent). 5 tests (PRAGMAs on file db, parent-dir, all tables/indexes, idempotent re-run, UNIQUE dup rejected). No new deps. |
| T05 — Job upsert & dedupe | done | `upsert_job` = `INSERT ... ON CONFLICT(source, source_id) DO UPDATE` (LLD §7.3): idempotent re-poll preserves `first_seen_at` (omitted from SET), bumps `last_seen_at`, refreshes mutable fields incl. `embedding`/`eligible`/`ineligible_reason`/`content_hash`. `_job_params` coerces bool→int, StrEnum→value, datetime→ISO, `raw`→JSON. Added `eligible`/`ineligible_reason`/`content_hash` to `Job` (DDL §7.2 + pipeline §8 require them; LLD §2 listing abbreviates them out). 2 new tests (insert+coercion, dedupe idempotency). M1 store layer continues in T06. |
| T06 — Scores/status/runs/companies/prune DAL | done | Remaining LLD §7.3 ops: `save_score` & `set_status` upsert on their PK (re-score/re-status replace, never duplicate); `start_run`/`finish_run` open then close a `poll_runs` row (`started_at`, then `finished_at`+`per_source_json` funnel); `add_company` = `ON CONFLICT(ats,token) DO NOTHING` (discovery dedup, never downgrades a verified entry) + `get_companies` reader; `prune(not_seen_days)` deletes by lexicographic ISO `last_seen_at < cutoff`, returns rowcount, cascades scores/status via FK. `now` injectable on all clock-using ops for deterministic tests. 6 new tests (score upsert, cascade delete, status upsert, run bookkeeping, company dedup/preserve, prune+cascade). **M1 store layer complete.** No new deps (stdlib). |

## TODO verify (real-world unknowns to confirm)

- **Company board tokens** in `config/companies.yaml.example` are seeded as
  plausible Canadian/remote employers but the exact ATS slugs are UNCONFIRMED
  (`# TODO verify`): greenhouse `shopify`/`benevity`/`clio`, lever
  `jobber`/`thinkific`, ashby `wealthsimple`. Confirm each against the live feed
  and flip `verified: true`.
