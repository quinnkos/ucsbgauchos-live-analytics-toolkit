import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from apps.api.local_first import (
    BuildService,
    LocalFirstService,
    classify_shot_zone,
    compute_seconds_played_from_pbp,
    derive_game_stats,
    format_pct,
)


class ShotZoneHeuristicTests(unittest.TestCase):
    def test_classify_shot_zone_from_espn_play_type(self) -> None:
        self.assertEqual(classify_shot_zone("LayUpShot", 2), "layup")
        self.assertEqual(classify_shot_zone("DunkShot", 2), "dunk")
        self.assertEqual(classify_shot_zone("JumpShot", 2), "mid")
        self.assertIsNone(classify_shot_zone("JumpShot", 3))

    def test_derive_game_stats_uses_espn_play_type(self) -> None:
        team_rows, player_rows = derive_game_stats(
            [
                {
                    "team_id": "2540",
                    "athlete_id": "1",
                    "assist_athlete_id": "",
                    "scoring_play": 1,
                    "shooting_play": 1,
                    "score_value": 2,
                    "points_attempted": 2,
                    "play_type": "JumpShot",
                    "text": "made jumper",
                },
                {
                    "team_id": "2540",
                    "athlete_id": "1",
                    "assist_athlete_id": "",
                    "scoring_play": 1,
                    "shooting_play": 1,
                    "score_value": 2,
                    "points_attempted": 2,
                    "play_type": "LayUpShot",
                    "text": "made layup",
                },
                {
                    "team_id": "2540",
                    "athlete_id": "1",
                    "assist_athlete_id": "",
                    "scoring_play": 0,
                    "shooting_play": 1,
                    "score_value": 0,
                    "points_attempted": 2,
                    "play_type": "DunkShot",
                    "text": "missed dunk",
                },
                {
                    "team_id": "2540",
                    "athlete_id": "1",
                    "assist_athlete_id": "",
                    "scoring_play": 0,
                    "shooting_play": 1,
                    "score_value": 0,
                    "points_attempted": 2,
                    "play_type": "TipShot",
                    "text": "missed tip shot",
                },
            ],
            {"2540": {"1": "Guard One"}},
        )
        _, team_stats = team_rows[0]
        player_stats = player_rows[0]
        self.assertEqual(team_stats["mid_m"], 1)
        self.assertEqual(team_stats["mid_a"], 1)
        self.assertEqual(team_stats["layup_m"], 1)
        self.assertEqual(team_stats["layup_a"], 1)
        self.assertEqual(team_stats["dunk_a"], 1)
        self.assertEqual(team_stats["dunk_m"], 0)
        self.assertEqual(team_stats["dunks"], 1)
        self.assertEqual(team_stats["tips"], 1)
        self.assertEqual(player_stats["dunks"], 1)
        self.assertEqual(player_stats["tips"], 1)

    def test_derive_game_stats_ignores_zero_stat_administrative_participants(self) -> None:
        team_rows, player_rows = derive_game_stats(
            [
                {
                    "team_id": "27",
                    "athlete_id": "21813",
                    "assist_athlete_id": "",
                    "scoring_play": 0,
                    "shooting_play": 0,
                    "score_value": 0,
                    "points_attempted": 0,
                    "play_type": "Coach's Challenge (Overturned)",
                    "text": "UC Riverside Coach's Challenge (Upheld) UC Riverside charged with a timeout",
                },
                {
                    "team_id": "27",
                    "athlete_id": "5174613",
                    "assist_athlete_id": "",
                    "scoring_play": 1,
                    "shooting_play": 1,
                    "score_value": 2,
                    "points_attempted": 2,
                    "play_type": "LayUpShot",
                    "text": "made layup",
                },
            ],
            {"27": {"5174613": "Marqui Worthy Jr."}},
        )
        _, team_stats = team_rows[0]
        self.assertEqual(team_stats["points"], 2)
        self.assertEqual(len(player_rows), 1)
        self.assertEqual(player_rows[0]["athlete_id"], "5174613")


class MinutesPlayedTests(unittest.TestCase):
    def test_compute_seconds_played_from_pbp_tracks_substitutions(self) -> None:
        plays = [
            {
                "sequence_number": 1,
                "period_number": 1,
                "clock_seconds": 1200,
                "team_id": "2540",
                "athlete_id": "",
                "play_type": "Start Game",
                "text": "Start game",
            },
            {
                "sequence_number": 2,
                "period_number": 1,
                "clock_seconds": 900,
                "team_id": "2540",
                "athlete_id": "1",
                "play_type": "Substitution",
                "text": "Guard One subbing out",
            },
            {
                "sequence_number": 3,
                "period_number": 1,
                "clock_seconds": 900,
                "team_id": "2540",
                "athlete_id": "6",
                "play_type": "Substitution",
                "text": "Bench One subbing in",
            },
            {
                "sequence_number": 4,
                "period_number": 1,
                "clock_seconds": 0,
                "team_id": "2540",
                "athlete_id": "",
                "play_type": "End Period",
                "text": "End of 1st half",
            },
        ]
        seconds = compute_seconds_played_from_pbp(
            plays,
            {"2540": {"1", "2", "3", "4", "5"}},
        )
        self.assertEqual(seconds["2540"]["1"], 300)
        self.assertEqual(seconds["2540"]["6"], 900)
        self.assertEqual(seconds["2540"]["2"], 1200)
        self.assertEqual(sum(seconds["2540"].values()), 6000)

    def test_compute_seconds_played_from_pbp_infers_missing_starter_on_sub_out(self) -> None:
        plays = [
            {
                "sequence_number": 1,
                "period_number": 1,
                "clock_seconds": 1200,
                "team_id": "2540",
                "athlete_id": "",
                "play_type": "Start Game",
                "text": "Start game",
            },
            {
                "sequence_number": 2,
                "period_number": 1,
                "clock_seconds": 600,
                "team_id": "2540",
                "athlete_id": "1",
                "play_type": "Substitution",
                "text": "Guard One subbing out",
            },
            {
                "sequence_number": 3,
                "period_number": 1,
                "clock_seconds": 600,
                "team_id": "2540",
                "athlete_id": "6",
                "play_type": "Substitution",
                "text": "Bench One subbing in",
            },
            {
                "sequence_number": 4,
                "period_number": 1,
                "clock_seconds": 0,
                "team_id": "2540",
                "athlete_id": "",
                "play_type": "End Period",
                "text": "Final",
            },
        ]
        seconds = compute_seconds_played_from_pbp(plays, {"2540": set()})
        self.assertEqual(seconds["2540"]["1"], 600)
        self.assertEqual(seconds["2540"]["6"], 600)

    def test_derive_and_aggregate_persist_minutes_played(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            with service.connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO pbp_plays (
                        game_id, play_key, espn_play_id, sequence_number, period_number, period_display,
                        clock, clock_seconds, team_id, athlete_id, assist_athlete_id, play_type, text,
                        scoring_play, shooting_play, score_value, points_attempted, home_score, away_score,
                        wallclock, ingest_id, raw_payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "game-1",
                            "play_1",
                            "1",
                            1,
                            1,
                            "1st",
                            "20:00",
                            1200,
                            "2540",
                            "",
                            "",
                            "Start Game",
                            "Start game",
                            0,
                            0,
                            0,
                            0,
                            0,
                            0,
                            "",
                            "ingest-1",
                            "{}",
                        ),
                        (
                            "game-1",
                            "play_2",
                            "2",
                            2,
                            1,
                            "1st",
                            "15:00",
                            900,
                            "2540",
                            "1",
                            "",
                            "LayUpShot",
                            "made layup",
                            1,
                            1,
                            2,
                            2,
                            2,
                            0,
                            "",
                            "ingest-1",
                            "{}",
                        ),
                        (
                            "game-1",
                            "play_3",
                            "3",
                            3,
                            1,
                            "1st",
                            "10:00",
                            600,
                            "2540",
                            "1",
                            "",
                            "Substitution",
                            "Guard One subbing out",
                            0,
                            0,
                            0,
                            0,
                            0,
                            0,
                            "",
                            "ingest-1",
                            "{}",
                        ),
                        (
                            "game-1",
                            "play_4",
                            "4",
                            4,
                            1,
                            "1st",
                            "10:00",
                            600,
                            "2540",
                            "6",
                            "",
                            "Substitution",
                            "Bench One subbing in",
                            0,
                            0,
                            0,
                            0,
                            0,
                            0,
                            "",
                            "ingest-1",
                            "{}",
                        ),
                        (
                            "game-1",
                            "play_5",
                            "5",
                            5,
                            1,
                            "1st",
                            "00:00",
                            0,
                            "2540",
                            "6",
                            "",
                            "LayUpShot",
                            "made layup",
                            1,
                            1,
                            2,
                            2,
                            2,
                            0,
                            "",
                            "ingest-1",
                            "{}",
                        ),
                    ],
                )
            with patch("apps.api.local_first.fetch_team_roster", return_value={"1": "Guard One", "6": "Bench One"}):
                with patch.object(service.build_service, "starting_lineups_for_game", return_value={"2540": {"1"}}):
                    self.assertTrue(service.derive_game_stats("game-1"))
            service.aggregate_season()
            payload = service.player_dataset("2540")
            player_minutes = {row["Player"]: row["MIN"] for row in payload["rows"]}
            self.assertEqual(player_minutes["Guard One"], "10.0")
            self.assertEqual(player_minutes["Bench One"], "10.0")
            self.assertEqual(player_minutes["Team"], "20.0")


class PercentageFromMakesAttemptsTests(unittest.TestCase):
    def test_format_pct(self) -> None:
        self.assertEqual(format_pct(7, 10), ".700")
        self.assertEqual(format_pct(0, 0), "")

    def test_player_dataset_uses_makes_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            with service.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO season_player_stats (
                        season_id, season_type, team_id, player_key, athlete_id, player_name, games_played, points, rebounds, assists,
                        turnovers, steals, blocks, personal_fouls, fgm, fga, fg3m, fg3a, ftm, fta,
                        layup_m, layup_a, dunk_m, dunk_a, mid_m, mid_a, dunks, tips
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2025-2026",
                        "regular",
                        "2540",
                        "1",
                        "1",
                        "Guard One",
                        2,
                        18,
                        6,
                        4,
                        2,
                        1,
                        0,
                        3,
                        7,
                        10,
                        3,
                        5,
                        1,
                        2,
                        2,
                        3,
                        0,
                        0,
                        2,
                        4,
                        1,
                        2,
                    ),
                )
            payload = service.player_dataset("2540")
            self.assertEqual(
                payload["columns"][-17:],
                [
                    "FGM",
                    "FGA",
                    "FG%",
                    "3PM",
                    "3PA",
                    "3P%",
                    "MIDR_M",
                    "MIDR_A",
                    "MIDR%",
                    "LAYUP_M",
                    "LAYUP_A",
                    "LAYUP%",
                    "DUNKS",
                    "TIPS",
                    "FTM",
                    "FTA",
                    "FT%",
                ],
            )
            self.assertEqual(payload["rows"][0]["FG%"], ".700")
            self.assertEqual(payload["rows"][0]["3P%"], ".600")
            self.assertEqual(payload["rows"][0]["MIDR%"], ".500")
            self.assertEqual(payload["rows"][0]["LAYUP%"], ".667")
            self.assertEqual(payload["rows"][0]["DUNKS"], "1")
            self.assertEqual(payload["rows"][0]["TIPS"], "2")
            self.assertEqual(payload["rows"][0]["FTM"], "1")
            self.assertEqual(payload["rows"][0]["FTA"], "2")
            self.assertEqual(payload["rows"][0]["FT%"], ".500")
            self.assertEqual(payload["rows"][-1]["Player"], "Team")

    def test_team_dataset_reports_fg_breakdown_mismatch_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            with service.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO season_team_stats (
                        season_id, season_type, team_id, games_played, points, rebounds, assists, turnovers,
                        steals, blocks, personal_fouls, fgm, fga, fg3m, fg3a, ftm, fta, layup_m, layup_a,
                        dunk_m, dunk_a, mid_m, mid_a, dunks, tips
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "2025-2026",
                        "regular",
                        "2540",
                        1,
                        24,
                        10,
                        4,
                        3,
                        2,
                        1,
                        5,
                        10,
                        20,
                        2,
                        5,
                        4,
                        6,
                        2,
                        4,
                        1,
                        2,
                        3,
                        7,
                        1,
                        2,
                    ),
                )
            payload = service.team_dataset("2540")
            metric_to_value = {row["metric"]: row["value"] for row in payload["rows"]}
            self.assertEqual(metric_to_value["FTM"], "4")
            self.assertEqual(metric_to_value["FTA"], "6")
            self.assertEqual(metric_to_value["FT%"], ".667")
            self.assertEqual(metric_to_value["FGM_BREAKDOWN"], "MISMATCH (+2)")
            self.assertEqual(metric_to_value["FGA_BREAKDOWN"], "MISMATCH (+2)")
            self.assertIn("includes 2 tracked tip attempts", metric_to_value["FG_BREAKDOWN_NOTE"])


class ScheduleValidationTests(unittest.TestCase):
    def test_validate_schedule_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            reference = service.load_schedule_reference()["games"]
            service.validate_schedule(reference)
            with self.assertRaises(RuntimeError):
                bad = list(reference)
                bad[0] = {**bad[0], "home_away": "away"}
                service.validate_schedule(bad)

    def test_parse_schedule_payload(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            payload = {
                "events": [
                    {
                        "id": "401809115",
                        "date": "2026-02-08T03:00Z",
                        "competitions": [
                            {
                                "competitors": [
                                    {"team": {"id": "2540", "displayName": "UC Santa Barbara Gauchos"}, "homeAway": "home"},
                                    {"team": {"id": "300", "displayName": "UC Irvine Anteaters"}, "homeAway": "away"},
                                ]
                            }
                        ],
                    }
                ]
            }
            games = service.parse_schedule_payload(payload, "2540")
            self.assertEqual(games[0]["game_id"], "401809115")
            self.assertEqual(games[0]["opponent_team_id"], "300")
            self.assertEqual(games[0]["home_away"], "home")

    def test_parse_schedule_payload_for_opponent_anchor_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            payload = {
                "events": [
                    {
                        "id": "401900001",
                        "date": "2026-01-10T03:00Z",
                        "competitions": [
                            {
                                "competitors": [
                                    {"team": {"id": "27", "displayName": "UC Riverside Highlanders"}, "homeAway": "away"},
                                    {"team": {"id": "30", "displayName": "Cal Poly Mustangs"}, "homeAway": "home"},
                                ]
                            }
                        ],
                    }
                ]
            }
            games = service.parse_schedule_payload(payload, "27")
            self.assertEqual(games[0]["game_id"], "401900001")
            self.assertEqual(games[0]["opponent_team_id"], "30")
            self.assertEqual(games[0]["home_away"], "away")


class BuildJobTransitionTests(unittest.TestCase):
    def test_build_job_transitions_to_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            service.verify_and_persist_schedule = lambda team_id="2540", force=False: [  # type: ignore[assignment]
                {"game_id": "1", "date": "2026-01-01", "opponent_team_id": "27", "opponent_name": "UC Riverside", "home_away": "home"}
            ]
            service.ingest_game = lambda game_id, force=False: {"rows": 10}  # type: ignore[assignment]
            service.derive_game_stats = lambda game_id: True  # type: ignore[assignment]
            service.aggregate_season = lambda: None  # type: ignore[assignment]
            job = service.start_build("season", "2540", force=False)
            deadline = time.time() + 5
            current = job
            while time.time() < deadline:
                current = service.get_job(job["job_id"])
                if current["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.05)
            self.assertEqual(current["status"], "succeeded")
            self.assertEqual(current["stage"], "aggregate_season_stats")

    def test_opponent_build_uses_selected_team_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            seen_team_ids: list[str] = []

            def fake_verify(team_id="2540", force=False):  # type: ignore[no-untyped-def]
                seen_team_ids.append(team_id)
                return [
                    {
                        "game_id": "99",
                        "date": "2026-01-02",
                        "opponent_team_id": "30",
                        "opponent_name": "Cal Poly",
                        "home_away": "away",
                    }
                ]

            service.verify_and_persist_schedule = fake_verify  # type: ignore[assignment]
            service.ingest_game = lambda game_id, force=False: {"rows": 5}  # type: ignore[assignment]
            service.derive_game_stats = lambda game_id: True  # type: ignore[assignment]
            service.aggregate_season = lambda: None  # type: ignore[assignment]

            job = service.start_build("season", "27", force=False)
            deadline = time.time() + 5
            current = job
            while time.time() < deadline:
                current = service.get_job(job["job_id"])
                if current["status"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.05)

            self.assertEqual(current["status"], "succeeded")
            self.assertIn("27", seen_team_ids)

    def test_incomplete_jobs_are_failed_on_service_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            with service.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO build_jobs (
                        job_id, job_type, status, stage, season_id, season_type, requested_team_id,
                        current_game_id, current_opponent_team_id, current_game_index, total_games,
                        message, error_message, force_rebuild, created_at, started_at, finished_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, '', '', 0, 0, '', '', 0, ?, ?, NULL, ?)
                    """,
                    (
                        "stale-job",
                        "season",
                        "running",
                        "derive_game_stats",
                        "2025-2026",
                        "regular",
                        "2540",
                        "2026-03-03T00:00:00+00:00",
                        "2026-03-03T00:00:00+00:00",
                        "2026-03-03T00:00:00+00:00",
                    ),
                )
                conn.commit()
            restarted = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            job = restarted.get_job("stale-job")
            self.assertEqual(job["status"], "failed")
            self.assertIn("interrupted", job["error_message"])


class ScopeGuardTests(unittest.TestCase):
    def test_player_and_team_datasets_reject_out_of_scope_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
            with self.assertRaises(ValueError):
                service.player_dataset("999999")
            with self.assertRaises(ValueError):
                service.team_dataset("999999")


def _setup_filtered_service(tmpdir: str):
    """Create a service with two games and PBP data for filtered aggregation tests."""
    root = Path(tmpdir)
    service = LocalFirstService(db_path=root / "state.sqlite3", object_store_root=root / "object_store")
    with service.connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO teams (team_id, school_name, abbreviation, display_name, conference_name, conference_abbreviation)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("2540", "UC Santa Barbara", "UCSB", "UC Santa Barbara Gauchos", "Big West", "BW"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO teams (team_id, school_name, abbreviation, display_name, conference_name, conference_abbreviation)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("27", "UC Riverside", "UCR", "UC Riverside Highlanders", "Big West", "BW"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO teams (team_id, school_name, abbreviation, display_name, conference_name, conference_abbreviation)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            ("999", "Non-Conf Team", "NCT", "Non-Conf Team", "Mountain West", "MW"),
        )
        conn.execute(
            "INSERT INTO schedule_games (season_id, season_type, team_id, game_id, game_date, opponent_team_id, opponent_name, home_away, schedule_source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2025-2026", "regular", "2540", "game-home-conf", "2026-01-10", "27", "UC Riverside", "home", "test"),
        )
        conn.execute(
            "INSERT INTO schedule_games (season_id, season_type, team_id, game_id, game_date, opponent_team_id, opponent_name, home_away, schedule_source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("2025-2026", "regular", "2540", "game-away-nonconf", "2026-01-15", "999", "Non-Conf Team", "away", "test"),
        )
        conn.execute(
            """INSERT INTO game_player_stats (
                game_id, team_id, player_key, athlete_id, player_name, games_played,
                points, rebounds, assists, turnovers, steals, blocks, personal_fouls,
                fgm, fga, fg3m, fg3a, ftm, fta, layup_m, layup_a, dunk_m, dunk_a, mid_m, mid_a, dunks, tips, seconds_played
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("game-home-conf", "2540", "1", "1", "Guard One", 1, 20, 5, 3, 2, 1, 0, 2, 8, 15, 2, 5, 2, 3, 3, 5, 0, 0, 3, 5, 0, 0, 1200),
        )
        conn.execute(
            """INSERT INTO game_player_stats (
                game_id, team_id, player_key, athlete_id, player_name, games_played,
                points, rebounds, assists, turnovers, steals, blocks, personal_fouls,
                fgm, fga, fg3m, fg3a, ftm, fta, layup_m, layup_a, dunk_m, dunk_a, mid_m, mid_a, dunks, tips, seconds_played
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("game-away-nonconf", "2540", "1", "1", "Guard One", 1, 15, 4, 2, 1, 0, 1, 3, 6, 12, 1, 3, 2, 2, 2, 4, 0, 0, 3, 5, 0, 0, 1100),
        )
        # PBP plays for period-scope testing
        for gid, plays in [
            ("game-home-conf", [
                ("p1", 1, 1, 1200, "2540", "1", "", "LayUpShot", "made layup", 1, 1, 2, 2),
                ("p2", 2, 1, 600, "2540", "1", "", "JumpShot", "made jumper", 1, 1, 2, 2),
                ("p3", 3, 1, 500, "2540", "1", "", "JumpShot", "made jumper", 1, 1, 2, 2),
                ("p4", 4, 1, 400, "2540", "1", "", "FreeThrow", "made free throw", 1, 0, 1, 1),
                ("p5", 5, 1, 300, "2540", "1", "", "JumpShot", "made three", 1, 1, 3, 3),
                ("p6", 6, 2, 1200, "2540", "1", "", "JumpShot", "made three", 1, 1, 3, 3),
                ("p7", 7, 2, 900, "2540", "1", "", "LayUpShot", "made layup", 1, 1, 2, 2),
                ("p8", 8, 2, 700, "2540", "1", "", "JumpShot", "made jumper", 1, 1, 2, 2),
                ("p9", 9, 2, 500, "2540", "1", "", "JumpShot", "made three", 1, 1, 3, 3),
            ]),
            ("game-away-nonconf", [
                ("q1", 1, 1, 1200, "2540", "1", "", "LayUpShot", "made layup", 1, 1, 2, 2),
                ("q2", 2, 1, 900, "2540", "1", "", "JumpShot", "made jumper", 1, 1, 2, 2),
                ("q3", 3, 1, 700, "2540", "1", "", "JumpShot", "made jumper", 1, 1, 2, 2),
                ("q4", 4, 1, 600, "2540", "1", "", "FreeThrow", "made free throw", 1, 0, 1, 1),
                ("q5", 5, 2, 1200, "2540", "1", "", "JumpShot", "made three", 1, 1, 3, 3),
                ("q6", 6, 2, 800, "2540", "1", "", "JumpShot", "made jumper", 1, 1, 2, 2),
                ("q7", 7, 2, 400, "2540", "1", "", "JumpShot", "made three", 1, 1, 3, 3),
            ]),
        ]:
            conn.executemany(
                """INSERT INTO pbp_plays (
                    game_id, play_key, espn_play_id, sequence_number, period_number, period_display,
                    clock, clock_seconds, team_id, athlete_id, assist_athlete_id, play_type, text,
                    scoring_play, shooting_play, score_value, points_attempted, home_score, away_score,
                    wallclock, ingest_id, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, '', 'test', '{}')""",
                [
                    (gid, pk, pk, seq, per, f"{per}{'st' if per==1 else 'nd'} Half", "20:00", cs, tid, aid, aaid, pt, txt, sp, shp, sv, pa)
                    for pk, seq, per, cs, tid, aid, aaid, pt, txt, sp, shp, sv, pa in plays
                ],
            )
    return service


class FilteredSeasonAggregationTests(unittest.TestCase):
    def test_filter_home_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            rows = service.build_service.filtered_season_player_rows("2540", filter_mode="home")
            player_rows = [r for r in rows if r["Player"] != "Team"]
            self.assertEqual(len(player_rows), 1)
            self.assertEqual(player_rows[0]["PTS"], "20")

    def test_filter_away_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            rows = service.build_service.filtered_season_player_rows("2540", filter_mode="away")
            player_rows = [r for r in rows if r["Player"] != "Team"]
            self.assertEqual(len(player_rows), 1)
            self.assertEqual(player_rows[0]["PTS"], "15")

    def test_filter_conference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            rows = service.build_service.filtered_season_player_rows("2540", filter_mode="conference")
            player_rows = [r for r in rows if r["Player"] != "Team"]
            self.assertEqual(len(player_rows), 1)
            self.assertEqual(player_rows[0]["PTS"], "20")

    def test_filter_vs_opponent(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            rows = service.build_service.filtered_season_player_rows(
                "2540", filter_mode="vs_selected_opponent", opponent_team_id="27"
            )
            player_rows = [r for r in rows if r["Player"] != "Team"]
            self.assertEqual(len(player_rows), 1)
            self.assertEqual(player_rows[0]["PTS"], "20")

    def test_filter_vs_opponent_no_games(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            rows = service.build_service.filtered_season_player_rows(
                "2540", filter_mode="vs_selected_opponent", opponent_team_id="300"
            )
            self.assertEqual(rows, [])

    def test_filter_all_returns_all_games(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            rows = service.build_service.filtered_season_player_rows("2540")
            player_rows = [r for r in rows if r["Player"] != "Team"]
            self.assertEqual(len(player_rows), 1)
            self.assertEqual(player_rows[0]["PTS"], "35")

    def test_team_total_row_correct_under_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            rows = service.build_service.filtered_season_player_rows("2540", filter_mode="home")
            team_row = [r for r in rows if r["Player"] == "Team"]
            self.assertEqual(len(team_row), 1)
            self.assertEqual(team_row[0]["PTS"], "20")

    def test_player_dataset_uses_same_endpoint_for_filtered_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            payload = service.player_dataset("2540", filter_mode="home", period_scope="full")
            player_rows = [r for r in payload["rows"] if r["Player"] != "Team"]
            self.assertEqual(len(player_rows), 1)
            self.assertEqual(player_rows[0]["PTS"], "20")


class PeriodScopeTests(unittest.TestCase):
    def test_period_scope_1st_half(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            with patch.object(service.build_service, "starting_lineups_for_game", return_value={"2540": set()}):
                rows = service.build_service.filtered_season_player_rows("2540", period_scope="1st")
            player_rows = [r for r in rows if r["Player"] != "Team"]
            self.assertEqual(len(player_rows), 1)
            total_pts = int(player_rows[0]["PTS"])
            self.assertGreater(total_pts, 0)
            self.assertLess(total_pts, 35)

    def test_period_scope_2nd_half(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            with patch.object(service.build_service, "starting_lineups_for_game", return_value={"2540": set()}):
                rows = service.build_service.filtered_season_player_rows("2540", period_scope="2nd")
            player_rows = [r for r in rows if r["Player"] != "Team"]
            self.assertEqual(len(player_rows), 1)
            total_pts = int(player_rows[0]["PTS"])
            self.assertGreater(total_pts, 0)
            self.assertLess(total_pts, 35)

    def test_period_scope_full_matches_precomputed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            rows = service.build_service.filtered_season_player_rows("2540", period_scope="full")
            player_rows = [r for r in rows if r["Player"] != "Team"]
            self.assertEqual(player_rows[0]["PTS"], "35")

    def test_period_scope_combined_with_home_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            service = _setup_filtered_service(tmpdir)
            with patch.object(service.build_service, "starting_lineups_for_game", return_value={"2540": set()}):
                rows = service.build_service.filtered_season_player_rows(
                    "2540", filter_mode="home", period_scope="1st"
                )
            player_rows = [r for r in rows if r["Player"] != "Team"]
            if player_rows:
                self.assertLess(int(player_rows[0]["PTS"]), 20)


if __name__ == "__main__":
    unittest.main()
