# Local-First Data Pipeline Revamp Spec (UCSB 2025-2026 Regular Season)

Date: 2026-03-03

## Goals

This document defines the intended changes for a local-first revamp of season data and play-by-play (PBP) pipelines.

Primary goals:
- Remove `data/` as an application runtime dependency and stop writing state into the repo.
- Build season data from **per-game play-by-play** for **UCSB's 2025-2026 regular season schedule** only.
- Only support teams: `UCSB` and teams appearing as **opponents on UCSB's 2025-2026 regular season schedule**.
- Provide an app UX that can trigger data building on demand and show progress.
- Preserve current table shapes/column names as much as possible; append new columns at the end.

Non-goals (for this local-first phase):
- No real user accounts/auth.
- No always-on hosting requirement.
- No full NCAA D1 backfill.
- No UI redesign.

## Key Constraints

- Season scope is fixed to **2025-2026 regular season only**.
- Opponent list is derived from **UCSB schedule** (not a full NCAA team list).
- Shot-zone stats must be derived **heuristically** from PBP play text when structured fields are missing.
- The existing PDF/checked-in tables may be used as **reference only** (for schema/column naming), but **must not be included** in the final computations.

## Current State (Baseline)

- API in [apps/api/server.py](/Users/ndkoster/Developer/basketball-live-analytics-toolkit/apps/api/server.py) reads/writes JSON/CSV under `data/`.
- PBP updates overwrite `data/pbp-<gameId>.json`.
- Season "team/player tables" can come from PDFs for UCSB/UCR or ESPN APIs.
- Web UI in [apps/web/src/App.jsx](/Users/ndkoster/Developer/basketball-live-analytics-toolkit/apps/web/src/App.jsx) has:
  - Game ID dropdown for a small hard-coded set.
  - Manual "Update" button for PBP.
  - Team/opponent selection is not conference-grouped.

## UI Freeze (Non-Negotiable)

Do not redesign the UI layout, panels, or styling.

Allowed UI changes only:
- Add minimal controls needed for the new pipeline (button/toggle/status text) inside existing panels.
- Replace the contents of existing dropdowns with schedule/team data (no new panel layouts).
- Fix broken wiring/labels due to backend changes.

Any change beyond this must be explicitly called out and is out of scope by default.

## Proposed Local-First Architecture

### Storage (Local Only)

Use:
- SQLite (local) as system of record for normalized and derived data.
- "Object storage archive" as local files on disk behind an interface (S3-compatible later).

Local storage paths:
- A `var/` directory (ignored by git) is the default root for local artifacts:
  - `var/object_store/` for raw payload archives (organized by source/team/game/time).
  - `var/state.sqlite3` for the SQLite database (configurable).

SQLite configuration:
- `SQLITE_DB_PATH` (default: `var/state.sqlite3`)
- Schema creation and incremental migrations must be handled by the app.
  - Recommended: a `schema_migrations` table + ordered SQL (or Python) migrations.

### Data Model (High Level)

Tables (minimum viable set):
- `teams`
  - `team_id` (ESPN team id string)
  - `school_name`
  - `abbreviation`
  - `conference_name` (for UI grouping)
- `seasons`
  - `season_id` (e.g. `2025-2026`)
  - `season_type` (`regular`)
- `schedule_games`
  - `season_id`
  - `team_id` (UCSB team id)
  - `game_id` (ESPN event/competition id; canonical)
  - `game_date`
  - `opponent_team_id`
  - `home_away`
  - Unique constraint on `(season_id, team_id, game_id)`
- `pbp_ingests`
  - `game_id`
  - `fetched_at`
  - `source_url`
  - `payload_sha256`
  - `archive_path` (local path under `var/object_store/`)
  - HTTP metadata (status, etag/last-modified if available)
- `pbp_plays`
  - `game_id`
  - `sequence`/`play_id` (whichever ESPN provides; choose one canonical key)
  - normalized fields used by UI + stats computations (clock, period, team_id, text, athlete_id, etc.)
  - raw JSON blob column for future debugging (optional)
  - Unique constraint on `(game_id, play_key)`
- Derived tables (or views):
  - `game_player_stats`
  - `game_team_stats`
  - `season_player_stats`
  - `season_team_stats`

### Object Archive Interface

Create an abstraction `ObjectStore` with:
- `put_bytes(key: str, content: bytes, content_type: str) -> StoredObjectRef`
- `get_bytes(key: str) -> bytes`

Local implementation writes to `var/object_store/<key>`.

## Season Data Build: What Happens When a User Requests It

Trigger: in the app, user selects a supported team and clicks "Build season data" (or loading season data triggers build if missing).

Build pipeline stages:
1. Ensure UCSB schedule is resolved for 2025-2026 regular season:
   - Discover all ESPN `game_id`s for UCSB regular season.
   - Discover opponent `team_id` for each game.
   - Persist into `schedule_games`.
   - This step is required and must be verifiable for success.
2. For each `game_id` in schedule:
   - Fetch PBP from ESPN.
   - Respect throttling and retries:
     - global per-process rate limit (e.g. 1 request/sec)
     - exponential backoff on errors
     - idempotent ingest: archive payload, hash, insert ingest record, upsert plays
3. For each game:
   - Convert PBP plays into a per-game "game stats table" similar to current live-stats derivation:
     - player-level stats
     - team-level stats
   - Persist per-game derived stats.
4. Aggregate:
   - Build season-level aggregated tables from game tables.
   - Compute percentage stats and append makes/attempts columns.

Progress reporting:
- The API exposes a build job id and status.
- Web shows a progress UI with stages and "game N / 32" updates.
- For this local-only phase, build jobs may run in-process in the API server as long as status/progress is persisted in SQLite and exposed via API.

## ESPN Data Discovery (Research-Driven but Runtime Triggered)

Important: do not precompute for all teams.

### Schedule Discovery (UCSB only)

At runtime, resolve UCSB schedule by querying ESPN endpoints:
- Determine where ESPN exposes the team schedule for season 2025-2026 and regular season games.
- Extract the canonical `game_id` list and opponent team ids.

The resolved schedule is persisted so subsequent calls do not re-fetch unless forced.

### Opponents Scope

Supported teams are:
- UCSB
- Any `opponent_team_id` appearing in `schedule_games` for UCSB season `2025-2026` regular season.

## Stats Computation Details

### Preserve Existing Table Shapes

Preserve existing player table columns and naming used by the current app/API.

Notable baseline behavior to keep:
- Player table includes a final "Team" row at the bottom representing team totals/aggregates (already in current code).
- Team-based stats must continue to be computed, not only player-based stats.

### Add New Columns (Append Only)

Append at end of player/team tables:
- For each existing percentage stat, include makes/attempts columns:
  - Example: `FG%` => append `FGM`, `FGA` (and keep `FG%`).
  - Example: `3P%` => append `3PM`, `3PA`.
  - Example: `FT%` => append `FTM`, `FTA`.
- Add shot zone breakdown (heuristic) with makes/attempts and percentage:
  - `LAYUP_M`, `LAYUP_A`, `LAYUP_PCT`
  - `DUNK_M`, `DUNK_A`, `DUNK_PCT`
  - `MID_M`, `MID_A`, `MID_PCT`

Heuristic definitions (initial version):
- Layup: play text contains `layup` (case-insensitive) and not `dunk`.
- Dunk: play text contains `dunk`.
- Midrange: play text contains `jumper` or `pullup` or `fadeaway` and does not contain `three point`/`3-pt` and is not a layup/dunk.

Notes:
- These heuristics should be implemented in one place and unit tested.
- If ESPN structured fields provide better classification, the code should be structured to incorporate them later without rewriting call sites.

### Per-Game vs Season Aggregates

Compute and persist:
- Per-game player stats and team stats.
- Season totals and season averages (if the current UI expects per-game rates, preserve it).

Percentage calculation rules:
- Percentages must be computed from aggregated makes/attempts, not averaged percentages.
- For display, keep current formatting conventions (string percent or decimal), matching existing app behavior.

## Throttling and ESPN Safety

Implement a shared throttling mechanism used by all ESPN HTTP calls:
- Configurable minimum delay between requests.
- Retry with exponential backoff.
- Circuit-breaker style behavior if repeated failures occur (stop build and present error).

Build-time sequencing:
- Default sequential fetching per game to avoid load; optionally allow low concurrency (2-3) but never unbounded.

## UI/UX Changes (Minimal Only)

### Team Selection UX

Update team selection behavior to:
- Search by school name/abbreviation.
- Dropdown grouped by conference (e.g., Big West, America East, etc.).
- Conferences and team list sourced from ESPN teams endpoint(s), cached in SQLite.

Scope constraints shown in UI:
- Season dropdown: fixed to `2025-2026`.
- Season type dropdown: fixed to `Regular Season`.
- Make it explicit this build is limited to "UCSB schedule opponents only".

### Game Selection UX (Avoid Long Game ID Lists)

Replace the current "Game ID" raw dropdown with:
- A schedule list grouped by month/date:
  - Each option label like `YYYY-MM-DD vs UC Irvine` (game_id should not be the primary label).
- Optional filters: opponent, home/away.

### Progress UI

Add a "Build status" panel/modal area (within existing panels):
- Stage: `Schedule discovery`, `PBP ingest`, `Deriving game stats`, `Aggregating season stats`.
- Current game progress: `7/32` with current opponent label.
- Errors show the last failed step and allow retry/force rebuild.

## API Changes (Local Dev)

New endpoints (suggested):
- `POST /api/build/ucsb/2025-2026/regular/schedule` (force rebuild option)
- `POST /api/build/ucsb/2025-2026/regular/season?team_id=<id>` (team must be UCSB or schedule opponent)
- `GET /api/build/jobs/<job_id>` (status/progress)
- `GET /api/teams` (with conference grouping)
- `GET /api/season/<season_id>/team/<team_id>/players` (player season table)
- `GET /api/season/<season_id>/team/<team_id>/games` (schedule list)
- `GET /api/pbp?game_id=<id>` (SQLite-backed)

Backward compatibility:
- Keep existing endpoints temporarily, but migrate the web UI to new endpoints during this revamp.

## Configuration Files

Checked-in config:
- `config/season_scope.json`
  - defines available seasons/types (for now: only 2025-2026 regular season)
- `config/ucsb_2025_2026_regular_schedule.json`
  - populated only for UCSB and only with discovered schedule data
  - serves as a stable reference and can be validated against SQLite

## Acceptance Criteria (Local-First Phase)

Hard acceptance criteria:
1. `data/` is no longer read/written at runtime by the API.
2. Building UCSB schedule for 2025-2026 regular season produces a non-empty list of game ids and opponents, persisted in SQLite.
3. Building season data for:
   - UCSB
   - UCR (if present on the UCSB schedule for 2025-2026 regular season)
   successfully ingests PBP for each scheduled game id (with throttling) and produces derived per-game and season tables.
4. The app displays progress during builds and shows an actionable error if a step fails.
5. Player table retains existing columns and includes the "Team" row; new columns are appended at the end.
6. Team-based stats continue to be computed in the new pipeline (not just player stats).
7. Percentage stats use computed makes/attempts; shot-zone makes/attempts/% are present.
8. UI changes remain minimal and within the "UI Freeze" section.

Verification checks:
- Unit tests for shot-zone heuristics and percentage computations.
- A test that schedule discovery returns consistent keys and that build job state transitions are correct.

## Long-Term Plan (Future Phases and Migrations)

This section outlines how to evolve the local-first implementation into a cloud-hosted, multi-user system without rewriting core logic.

### Phase L1: Cloud Storage Migration

Objectives:
- Replace local file archive with S3-compatible object storage (Cloudflare R2 / AWS S3).
- Keep the API and data model stable; only change `ObjectStore` implementation and configuration.

Work items:
- Add `S3ObjectStore` implementation.
- Add object retention and lifecycle policies.
- Add content-addressed keys and deduplication (by sha256).

### Phase L1.5: SQLite -> Postgres Migration

Objectives:
- Move from local SQLite to Postgres for multi-user concurrency and durability.
- Keep API shapes stable; migrate data model with minimal end-user impact.

Work items:
- Introduce Postgres-backed storage adapter and migrations.
- Add a one-time migration tool to copy SQLite state to Postgres.

### Phase L2: Multi-User Workspaces (No Always-On Requirement)

Objectives:
- Introduce per-user workspaces and persist saved insights/prompts.
- Allow sharing via workspace membership (optional).

Work items:
- Auth integration (managed provider preferred).
- Add `users`, `workspaces`, membership, and RBAC.
- Scope all saved objects by `(workspace_id, user_id)`.

### Phase L3: Background Workers and Realtime Updates

Objectives:
- Move builds and refreshes off request thread.
- Provide SSE updates for build progress and PBP update notifications.

Work items:
- Introduce worker process and job queue.
- Use Redis for job state, locks, and rate limiting across instances.

### Phase L4: Deployment Hardening

Objectives:
- Run API/worker in containers.
- Standardize migrations and CI/CD.
- Add backups and observability.

Work items:
- Dockerfiles, compose for dev, and a deploy recipe (Fly/Render/AWS).
- Metrics and structured logging; error reporting.
- Automated DB backups and restore drills.

### Phase L5: Scope Expansion

Objectives:
- Expand beyond UCSB schedule opponents if desired.
- Add additional seasons/types beyond 2025-2026 regular season.

Work items:
- Generalize schedule discovery to arbitrary teams.
- Add caching and partial rebuilds per opponent/team.
- Add admin tools for backfills and data integrity.
