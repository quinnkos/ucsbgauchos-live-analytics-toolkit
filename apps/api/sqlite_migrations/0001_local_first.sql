CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    team_id TEXT PRIMARY KEY,
    school_name TEXT NOT NULL,
    abbreviation TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL,
    conference_name TEXT NOT NULL DEFAULT '',
    conference_abbreviation TEXT NOT NULL DEFAULT '',
    raw_payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS seasons (
    season_id TEXT NOT NULL,
    season_type TEXT NOT NULL,
    label TEXT NOT NULL,
    is_locked INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (season_id, season_type)
);

CREATE TABLE IF NOT EXISTS schedule_games (
    season_id TEXT NOT NULL,
    season_type TEXT NOT NULL,
    team_id TEXT NOT NULL,
    game_id TEXT NOT NULL,
    game_date TEXT NOT NULL,
    opponent_team_id TEXT NOT NULL,
    opponent_name TEXT NOT NULL,
    home_away TEXT NOT NULL,
    schedule_source TEXT NOT NULL,
    schedule_verified_at TEXT,
    raw_payload TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (season_id, season_type, team_id, game_id)
);

CREATE INDEX IF NOT EXISTS schedule_games_scope_idx
    ON schedule_games (season_id, season_type, team_id, game_date);

CREATE INDEX IF NOT EXISTS schedule_games_opponent_idx
    ON schedule_games (opponent_team_id);

CREATE TABLE IF NOT EXISTS pbp_ingests (
    id TEXT PRIMARY KEY,
    game_id TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source_url TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    archive_key TEXT NOT NULL,
    archive_path TEXT NOT NULL,
    content_type TEXT NOT NULL,
    payload_size_bytes INTEGER NOT NULL,
    request_attempts INTEGER NOT NULL DEFAULT 1,
    raw_metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS pbp_ingests_game_idx
    ON pbp_ingests (game_id, fetched_at DESC);

CREATE TABLE IF NOT EXISTS pbp_plays (
    game_id TEXT NOT NULL,
    play_key TEXT NOT NULL,
    espn_play_id TEXT NOT NULL,
    sequence_number INTEGER NOT NULL,
    period_number INTEGER NOT NULL,
    period_display TEXT NOT NULL,
    clock TEXT NOT NULL,
    clock_seconds INTEGER NOT NULL,
    team_id TEXT NOT NULL,
    athlete_id TEXT NOT NULL DEFAULT '',
    assist_athlete_id TEXT NOT NULL DEFAULT '',
    play_type TEXT NOT NULL,
    text TEXT NOT NULL,
    scoring_play INTEGER NOT NULL DEFAULT 0,
    shooting_play INTEGER NOT NULL DEFAULT 0,
    score_value INTEGER NOT NULL DEFAULT 0,
    points_attempted INTEGER NOT NULL DEFAULT 0,
    home_score INTEGER NOT NULL DEFAULT 0,
    away_score INTEGER NOT NULL DEFAULT 0,
    wallclock TEXT NOT NULL DEFAULT '',
    ingest_id TEXT NOT NULL,
    raw_payload TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (game_id, play_key)
);

CREATE INDEX IF NOT EXISTS pbp_plays_game_sequence_idx
    ON pbp_plays (game_id, sequence_number, play_key);

CREATE TABLE IF NOT EXISTS game_player_stats (
    game_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    player_key TEXT NOT NULL,
    athlete_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    games_played INTEGER NOT NULL DEFAULT 1,
    points INTEGER NOT NULL DEFAULT 0,
    rebounds INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    turnovers INTEGER NOT NULL DEFAULT 0,
    steals INTEGER NOT NULL DEFAULT 0,
    blocks INTEGER NOT NULL DEFAULT 0,
    personal_fouls INTEGER NOT NULL DEFAULT 0,
    fgm INTEGER NOT NULL DEFAULT 0,
    fga INTEGER NOT NULL DEFAULT 0,
    fg3m INTEGER NOT NULL DEFAULT 0,
    fg3a INTEGER NOT NULL DEFAULT 0,
    ftm INTEGER NOT NULL DEFAULT 0,
    fta INTEGER NOT NULL DEFAULT 0,
    layup_m INTEGER NOT NULL DEFAULT 0,
    layup_a INTEGER NOT NULL DEFAULT 0,
    dunk_m INTEGER NOT NULL DEFAULT 0,
    dunk_a INTEGER NOT NULL DEFAULT 0,
    mid_m INTEGER NOT NULL DEFAULT 0,
    mid_a INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, team_id, player_key)
);

CREATE TABLE IF NOT EXISTS game_team_stats (
    game_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    games_played INTEGER NOT NULL DEFAULT 1,
    points INTEGER NOT NULL DEFAULT 0,
    rebounds INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    turnovers INTEGER NOT NULL DEFAULT 0,
    steals INTEGER NOT NULL DEFAULT 0,
    blocks INTEGER NOT NULL DEFAULT 0,
    personal_fouls INTEGER NOT NULL DEFAULT 0,
    fgm INTEGER NOT NULL DEFAULT 0,
    fga INTEGER NOT NULL DEFAULT 0,
    fg3m INTEGER NOT NULL DEFAULT 0,
    fg3a INTEGER NOT NULL DEFAULT 0,
    ftm INTEGER NOT NULL DEFAULT 0,
    fta INTEGER NOT NULL DEFAULT 0,
    layup_m INTEGER NOT NULL DEFAULT 0,
    layup_a INTEGER NOT NULL DEFAULT 0,
    dunk_m INTEGER NOT NULL DEFAULT 0,
    dunk_a INTEGER NOT NULL DEFAULT 0,
    mid_m INTEGER NOT NULL DEFAULT 0,
    mid_a INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (game_id, team_id)
);

CREATE TABLE IF NOT EXISTS season_player_stats (
    season_id TEXT NOT NULL,
    season_type TEXT NOT NULL,
    team_id TEXT NOT NULL,
    player_key TEXT NOT NULL,
    athlete_id TEXT NOT NULL,
    player_name TEXT NOT NULL,
    games_played INTEGER NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    rebounds INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    turnovers INTEGER NOT NULL DEFAULT 0,
    steals INTEGER NOT NULL DEFAULT 0,
    blocks INTEGER NOT NULL DEFAULT 0,
    personal_fouls INTEGER NOT NULL DEFAULT 0,
    fgm INTEGER NOT NULL DEFAULT 0,
    fga INTEGER NOT NULL DEFAULT 0,
    fg3m INTEGER NOT NULL DEFAULT 0,
    fg3a INTEGER NOT NULL DEFAULT 0,
    ftm INTEGER NOT NULL DEFAULT 0,
    fta INTEGER NOT NULL DEFAULT 0,
    layup_m INTEGER NOT NULL DEFAULT 0,
    layup_a INTEGER NOT NULL DEFAULT 0,
    dunk_m INTEGER NOT NULL DEFAULT 0,
    dunk_a INTEGER NOT NULL DEFAULT 0,
    mid_m INTEGER NOT NULL DEFAULT 0,
    mid_a INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season_id, season_type, team_id, player_key)
);

CREATE TABLE IF NOT EXISTS season_team_stats (
    season_id TEXT NOT NULL,
    season_type TEXT NOT NULL,
    team_id TEXT NOT NULL,
    games_played INTEGER NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0,
    rebounds INTEGER NOT NULL DEFAULT 0,
    assists INTEGER NOT NULL DEFAULT 0,
    turnovers INTEGER NOT NULL DEFAULT 0,
    steals INTEGER NOT NULL DEFAULT 0,
    blocks INTEGER NOT NULL DEFAULT 0,
    personal_fouls INTEGER NOT NULL DEFAULT 0,
    fgm INTEGER NOT NULL DEFAULT 0,
    fga INTEGER NOT NULL DEFAULT 0,
    fg3m INTEGER NOT NULL DEFAULT 0,
    fg3a INTEGER NOT NULL DEFAULT 0,
    ftm INTEGER NOT NULL DEFAULT 0,
    fta INTEGER NOT NULL DEFAULT 0,
    layup_m INTEGER NOT NULL DEFAULT 0,
    layup_a INTEGER NOT NULL DEFAULT 0,
    dunk_m INTEGER NOT NULL DEFAULT 0,
    dunk_a INTEGER NOT NULL DEFAULT 0,
    mid_m INTEGER NOT NULL DEFAULT 0,
    mid_a INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (season_id, season_type, team_id)
);

CREATE TABLE IF NOT EXISTS build_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    season_id TEXT NOT NULL,
    season_type TEXT NOT NULL,
    requested_team_id TEXT NOT NULL,
    current_game_id TEXT NOT NULL DEFAULT '',
    current_opponent_team_id TEXT NOT NULL DEFAULT '',
    current_game_index INTEGER NOT NULL DEFAULT 0,
    total_games INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    force_rebuild INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS build_jobs_status_idx
    ON build_jobs (status, created_at DESC);
