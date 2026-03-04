# Analytics Dashboard

Monorepo with:
- Python API: `apps/api`
- React/Vite web UI: `apps/web`

## Local-first scope

This phase is limited to:
- `2025-2026`
- `Regular Season`
- `UCSB` plus opponents on UCSB's 2025-2026 regular-season schedule

Runtime storage now uses:
- SQLite at `var/state.sqlite3` by default
- raw ESPN archive files under `var/object_store/`

The API should not read or write under repo `data/` at runtime.

## Prereqs

- Python `3.9+`
- Node.js + npm

## Setup

Recommended:

```bash
npm run setup
cp -n .env.example .env
```

Manual equivalent:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
npm --prefix apps/web install
cp -n .env.example .env
```

## Environment

Useful defaults in `.env`:

- `API_HOST=0.0.0.0`
- `API_PORT=8001`
- `SQLITE_DB_PATH=var/state.sqlite3`
- `OBJECT_STORE_ROOT=var/object_store`

Optional:

- `OPENAI_API_KEY` for `/api/insights`
- `OPENAI_MODEL`, `OPENAI_BASE_URL`
- `VITE_API_BASE_URL` if you do not want the default dev proxy

## Run

```bash
npm run dev
```

Or separately:

```bash
npm run dev:api
npm run dev:web
```

## Build flow

1. Start the app with `npm run dev`
2. In the existing UI:
   - choose a supported opponent from the conference-grouped dropdown
   - click `Refresh Schedule` to verify UCSB's locked regular-season schedule in SQLite
   - click `Build Season Data` to ingest PBP, archive raw payloads, derive game stats, and aggregate season stats
3. Use the schedule-based game dropdown in the Game Data panel to inspect PBP and live stats

## Tests

```bash
npm run test
```

## Notes

- Checked-in scope config: `config/season_scope.json`
- Checked-in UCSB schedule reference: `config/ucsb_2025_2026_regular_schedule.json`
- SQLite migrations are applied automatically from `apps/api/sqlite_migrations/`
