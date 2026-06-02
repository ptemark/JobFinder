# Progress — Job Finder

Milestone-by-milestone status, updated each Ralph iteration. The authoritative
task list and acceptance checks live in `spec/tasks.md`; this file is the
human-readable rollup.

## Milestone status

| Milestone | Scope | Status |
|-----------|-------|--------|
| M1 — Skeleton + DB | scaffold, settings, models, SQLite store | in progress |
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

## TODO verify (real-world unknowns to confirm)

- **Company board tokens** in `config/companies.yaml.example` are seeded as
  plausible Canadian/remote employers but the exact ATS slugs are UNCONFIRMED
  (`# TODO verify`): greenhouse `shopify`/`benevity`/`clio`, lever
  `jobber`/`thinkific`, ashby `wealthsimple`. Confirm each against the live feed
  and flip `verified: true`.
