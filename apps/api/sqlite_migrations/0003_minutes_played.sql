ALTER TABLE game_player_stats ADD COLUMN seconds_played INTEGER NOT NULL DEFAULT 0;

ALTER TABLE season_player_stats ADD COLUMN seconds_played INTEGER NOT NULL DEFAULT 0;
