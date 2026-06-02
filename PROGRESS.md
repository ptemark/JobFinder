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

## TODO verify (real-world unknowns to confirm)

- _none yet_ — company board tokens will be seeded with `# TODO verify` markers
  in `config/companies.yaml.example` when that task (T02) lands.
