import tempfile
import time
import unittest
from pathlib import Path

from apps.api.local_first import LocalFirstService, classify_shot_zone, derive_game_stats, format_pct


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


if __name__ == "__main__":
    unittest.main()
