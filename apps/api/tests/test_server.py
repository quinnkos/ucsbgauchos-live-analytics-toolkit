import unittest
from unittest.mock import patch

from apps.api.server import (
    _build_pbp_display_rows,
    _extract_team_stat_rows,
    _call_openai_chat,
    apply_pbp_filters,
    canonicalize_insights_payload,
    build_live_stats_from_pbp,
    dataset_filename,
    generate_insights,
    normalize_dataset_name,
    normalize_team_id,
    openai_schema,
    parse_pbp_filters,
    parse_player_table_rows,
    parse_team_table_rows,
    PbpFilterValidationError,
    PbpFilters,
    season_year_from_label,
    validate_insights_payload,
)


class TeamIdTests(unittest.TestCase):
    def test_normalize_team_id(self) -> None:
        self.assertEqual(normalize_team_id("UCSB"), "ucsb")
        self.assertEqual(normalize_team_id("uc-irvine"), "uc-irvine")
        self.assertEqual(normalize_team_id("2540"), "2540")
        with self.assertRaises(ValueError):
            normalize_team_id("***")


class SeasonYearTests(unittest.TestCase):
    def test_season_year_from_label(self) -> None:
        self.assertEqual(season_year_from_label("2025-26"), 2026)
        self.assertEqual(season_year_from_label("2025-2026"), 2026)


class EspnTeamStatsParsingTests(unittest.TestCase):
    def test_extract_team_stat_rows(self) -> None:
        payloads = [
            {
                "displayName": "Overall",
                "statistics": [
                    {"name": "wins", "displayName": "Wins", "value": 21, "displayValue": "21"},
                    {"name": "losses", "displayName": "Losses", "value": 9, "displayValue": "9"},
                ],
            }
        ]
        rows = _extract_team_stat_rows("2540", payloads)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["team_id"], "2540")
        self.assertIn("row_key", rows[0])
        self.assertEqual(rows[0]["split"], "Overall")


class DatasetAliasTests(unittest.TestCase):
    def test_dataset_aliases(self) -> None:
        self.assertEqual(normalize_dataset_name("team"), "team")
        self.assertEqual(normalize_dataset_name("player"), "players")
        self.assertEqual(normalize_dataset_name("players"), "players")
        self.assertEqual(normalize_dataset_name("pbp"), "pbp")
        self.assertEqual(dataset_filename("player"), "player.csv")
        self.assertTrue(dataset_filename("pbp").startswith("pbp-"))
        with self.assertRaises(ValueError):
            normalize_dataset_name("games")


class PdfTableParsingTests(unittest.TestCase):
    def test_parse_team_table_rows(self) -> None:
        table = [
            ["SCORING", "2217", "2062"],
            ["Points Per Game", "79.2", "73.6"],
            ["Score by Periods", "1st", "2nd", "OT", "Total"],
            ["UC Santa Barbara", "1053", "1128", "36", "2217"],
            ["Opponents", "914", "1110", "38", "2062"],
        ]
        rows = parse_team_table_rows(table)
        self.assertGreaterEqual(len(rows), 4)
        self.assertEqual(rows[0]["metric"], "SCORING")
        self.assertEqual(rows[0]["team"], "2217")
        self.assertEqual(rows[0]["opp"], "2062")

    def test_parse_player_table_rows(self) -> None:
        table = [
            [
                "#",
                "Player",
                "GP-GS",
                "MIN",
                "AVG/MIN",
                "FG-FGA",
                "FG%",
                "PTS",
                "AVG/P",
            ],
            ["20", "Mahaney, Aidan", "28-28", "922", "32.9", "150-340", ".441", "426", "15.2"],
            ["", "TEAM TOTALS", "", "", "", "", "", "", ""],
        ]
        rows = parse_player_table_rows(table)
        self.assertEqual(len(rows), 2)  # Mahaney + computed Team row
        self.assertEqual(rows[0]["player"], "Mahaney, Aidan")
        self.assertEqual(rows[0]["gp_gs"], "28-28")
        self.assertEqual(rows[1]["player"], "Team")
        self.assertEqual(rows[1]["gp_gs"], "28-28")
        self.assertEqual(rows[1]["pts"], "426")
        self.assertEqual(rows[1]["avg_pts"], "15.2")


class InsightValidationTests(unittest.TestCase):
    def test_validate_insights_payload(self) -> None:
        datasets = [
            {
                "team_id": "ucsb",
                "dataset": "team",
                "columns": ["row_key", "metric", "team", "opp"],
                "rows_by_key": {
                    "scoring": {
                        "row_key": "scoring",
                        "metric": "SCORING",
                        "team": "2217",
                        "opp": "2062",
                    }
                },
            },
            {
                "team_id": "ucsb",
                "dataset": "players",
                "columns": ["row_key", "player", "pts"],
                "rows_by_key": {
                    "mahaney_aidan_20": {
                        "row_key": "mahaney_aidan_20",
                        "player": "Mahaney, Aidan",
                        "pts": "426",
                    }
                },
            },
            {
                "team_id": "pbp",
                "dataset": "pbp",
                "columns": ["id", "clock", "text", "row_key"],
                "rows_by_key": {
                    "play_123": {
                        "id": "123",
                        "clock": "19:59",
                        "text": "Jump ball won by UCSB",
                        "row_key": "play_123",
                    }
                },
            },
        ]

        payload = {
            "insights": [
                {
                    "insight": "UCSB scored more than opponents.",
                    "evidence": [
                        {
                            "team_id": "ucsb",
                            "dataset": "team",
                            "row_key": "scoring",
                            "fields": ["team", "opp"],
                        },
                        {
                            "team_id": "pbp",
                            "dataset": "pbp",
                            "row_key": "play_123",
                            "fields": ["clock", "text"],
                        },
                        {
                            "team_id": "ucsb",
                            "dataset": "players",
                            "row_key": "mahaney_aidan_20",
                            "fields": ["player", "pts"],
                        },
                    ],
                }
            ]
        }

        ok, errors = validate_insights_payload(payload, datasets)
        self.assertTrue(ok)
        self.assertEqual(errors, [])

    def test_validate_insights_payload_rejects_unknown_and_extra_fields(self) -> None:
        datasets = [
            {
                "team_id": "pbp",
                "dataset": "pbp",
                "columns": ["row_key", "clock", "text"],
                "rows_by_key": {"play_1": {"row_key": "play_1", "clock": "19:59", "text": "tip"}},
            }
        ]
        payload = {
            "insights": [
                {
                    "insight": "Test",
                    "evidence": [
                        {
                            "team_id": "2540",
                            "dataset": "pbp",
                            "row_key": "play_1",
                            "fields": ["clock"],
                            "extra": "not allowed",
                        }
                    ],
                    "extra_item": True,
                }
            ],
            "extra_top": "bad",
        }
        ok, errors = validate_insights_payload(payload, datasets)
        self.assertFalse(ok)
        self.assertTrue(any("unknown top-level keys" in err for err in errors))
        self.assertTrue(any("contains unknown keys" in err for err in errors))
        self.assertTrue(any("references unknown dataset (2540/pbp)" in err for err in errors))


class InsightReliabilityTests(unittest.TestCase):
    def _dataset_contexts(self):
        return [
            {
                "team_id": "pbp",
                "dataset": "pbp",
                "columns": ["row_key", "clock", "text"],
                "rows": [{"row_key": "play_1", "clock": "19:59", "text": "Jump ball won by UCSB"}],
                "rows_by_key": {"play_1": {"row_key": "play_1", "clock": "19:59", "text": "Jump ball won by UCSB"}},
            },
            {
                "team_id": "ucsb",
                "dataset": "team",
                "columns": ["row_key", "team", "opp"],
                "rows": [{"row_key": "scoring", "team": "2217", "opp": "2062"}],
                "rows_by_key": {"scoring": {"row_key": "scoring", "team": "2217", "opp": "2062"}},
            },
        ]

    def test_canonicalize_maps_single_pbp_context(self) -> None:
        payload = {
            "insights": [
                {
                    "insight": "PBP trend",
                    "evidence": [
                        {
                            "team_id": "2540",
                            "dataset": "pbp",
                            "row_key": "play_1",
                            "fields": ["clock", "text"],
                        }
                    ],
                }
            ]
        }
        normalized = canonicalize_insights_payload(payload, self._dataset_contexts())
        ref = normalized["insights"][0]["evidence"][0]
        self.assertEqual(ref["team_id"], "pbp")
        self.assertEqual(ref["dataset"], "pbp")

    def test_generate_insights_repairs_invalid_output(self) -> None:
        initial = {
            "insights": [
                {
                    "insight": "UCSB has momentum.",
                    "evidence": [
                        {"team_id": "pbp", "dataset": "pbp", "row_key": "missing_row", "fields": ["clock", "text"]}
                    ],
                }
            ]
        }
        repaired = {
            "insights": [
                {
                    "insight": "UCSB has momentum in the last 5 minutes.",
                    "evidence": [
                        {"team_id": "pbp", "dataset": "pbp", "row_key": "play_1", "fields": ["clock", "text"]}
                    ],
                }
            ]
        }
        with patch("apps.api.server._call_openai_chat", side_effect=[initial, repaired]) as mock_call:
            payload = generate_insights("Give me one trend.", self._dataset_contexts())
        self.assertEqual(mock_call.call_count, 2)
        ok, errors = validate_insights_payload(payload, self._dataset_contexts())
        self.assertTrue(ok, msg=f"expected valid payload, got errors: {errors}")

    def test_generate_insights_fixes_2540_pbp_without_retry(self) -> None:
        initial = {
            "insights": [
                {
                    "insight": "Recent pace shift.",
                    "evidence": [
                        {"team_id": "2540", "dataset": "pbp", "row_key": "play_1", "fields": ["clock", "text"]}
                    ],
                }
            ]
        }
        with patch("apps.api.server._call_openai_chat", return_value=initial) as mock_call:
            payload = generate_insights("Summarize pace.", self._dataset_contexts())
        self.assertEqual(mock_call.call_count, 1)
        ok, errors = validate_insights_payload(payload, self._dataset_contexts())
        self.assertTrue(ok, msg=f"expected valid payload, got errors: {errors}")

    def test_openai_schema_locks_allowed_pairs(self) -> None:
        schema = openai_schema([("pbp", "pbp"), ("ucsb", "team")])
        evidence_items = schema["schema"]["properties"]["insights"]["items"]["properties"]["evidence"]["items"]
        self.assertIn("anyOf", evidence_items)
        self.assertEqual(len(evidence_items["anyOf"]), 2)
        first = evidence_items["anyOf"][0]["properties"]
        self.assertIn("const", first["team_id"])
        self.assertIn("const", first["dataset"])


class OpenAIParsingTests(unittest.TestCase):
    @patch("apps.api.server.requests.post")
    def test_call_openai_chat_rejects_malformed_json(self, mock_post) -> None:
        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self):
                return {"choices": [{"message": {"content": "not-json"}}]}

        mock_post.return_value = _Resp()
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}):
            with self.assertRaises(RuntimeError):
                _call_openai_chat([{"role": "system", "content": "x"}], schema=openai_schema())


class PbpTableFormattingTests(unittest.TestCase):
    def test_pbp_table_drops_internal_columns_and_reorders(self) -> None:
        rows = [
            {
                "row_key": "play_1",
                "id": "100",
                "sequence": 1,
                "period": "1st",
                "clock": "19:59",
                "text": "Made Jumper.",
                "type": "Made Shot",
                "team_id": "2540",
                "scoring_play": True,
                "shooting_play": True,
                "wallclock": "2026-02-26T01:00:00Z",
            }
        ]
        columns = [
            "id",
            "sequence",
            "period",
            "clock",
            "text",
            "type",
            "team_id",
            "scoring_play",
            "shooting_play",
            "wallclock",
            "row_key",
        ]
        out_columns, out_rows = _build_pbp_display_rows(rows, columns, ucsb_team_id="2540")
        self.assertEqual(out_columns[:3], ["team_id", "type", "text"])
        for forbidden in ("id", "sequence", "scoring_play", "shooting_play", "wallclock"):
            self.assertNotIn(forbidden, out_columns)
            self.assertNotIn(forbidden, out_rows[0])
        self.assertEqual(out_rows[0]["team_id"], "UCSB")
        self.assertEqual(out_rows[0]["row_key"], "play_1")

    def test_pbp_table_team_labels_use_opponent_for_non_ucsb(self) -> None:
        rows = [
            {"row_key": "play_1", "team_id": "300", "type": "Turnover", "text": "Bad pass", "clock": "18:01"},
            {"row_key": "play_2", "team_id": "2540", "type": "Steal", "text": "Stolen by UCSB", "clock": "17:55"},
        ]
        columns = ["row_key", "team_id", "type", "text", "clock"]
        _, out_rows = _build_pbp_display_rows(rows, columns, ucsb_team_id="2540")
        self.assertEqual(out_rows[0]["team_id"], "Opponent")
        self.assertEqual(out_rows[1]["team_id"], "UCSB")


class LiveStatsRegressionTests(unittest.TestCase):
    def test_live_stats_content_unchanged_by_pbp_table_formatting(self) -> None:
        raw_rows = [
            {
                "team_id": "2540",
                "type": "Made Shot",
                "text": "Made Jumper",
                "scoring_play": True,
                "shooting_play": True,
                "score_value": 2,
                "points_attempted": 2,
                "athlete_id": "11",
                "assist_athlete_id": "22",
            },
            {
                "team_id": "300",
                "type": "Turnover",
                "text": "Lost ball turnover",
                "scoring_play": False,
                "shooting_play": False,
                "score_value": 0,
                "points_attempted": 0,
                "athlete_id": "44",
                "assist_athlete_id": "",
            },
        ]
        with patch("apps.api.server.load_pbp_rows", return_value=raw_rows):
            with patch("apps.api.server.get_service") as mock_service:
                mock_service.return_value.player_seconds_for_game.return_value = {
                    "2540": {"11": 600, "22": 600},
                    "300": {"44": 600},
                }
                live = build_live_stats_from_pbp(ucsb_team_id="2540", opponent_team_id="300")
        ucsb_rows = live["ucsb_team"]["rows"]
        pts_row = next(row for row in ucsb_rows if row["stat_key"] == "pts")
        ast_row = next(row for row in ucsb_rows if row["stat_key"] == "ast")
        self.assertEqual(pts_row["value"], "2")
        self.assertEqual(ast_row["value"], "1")

    def test_live_player_rows_include_minutes_played(self) -> None:
        raw_rows = [
            {
                "team_id": "2540",
                "type": "Made Shot",
                "text": "Made Jumper",
                "scoring_play": True,
                "shooting_play": True,
                "score_value": 2,
                "points_attempted": 2,
                "athlete_id": "11",
                "assist_athlete_id": "22",
            }
        ]
        with patch("apps.api.server.load_pbp_rows", return_value=raw_rows):
            with patch("apps.api.server.get_service") as mock_service:
                mock_service.return_value.player_seconds_for_game.return_value = {
                    "2540": {"11": 600, "22": 1200}
                }
                with patch("apps.api.server.resolve_athlete_name", side_effect=lambda athlete_id: f"Player {athlete_id}"):
                    live = build_live_stats_from_pbp(ucsb_team_id="2540", opponent_team_id="300")
        player_rows = live["ucsb_players"]["rows"]
        minutes_by_player = {row["Player"]: row["MIN"] for row in player_rows}
        self.assertEqual(minutes_by_player["Player 11"], "10.0")
        self.assertEqual(minutes_by_player["Player 22"], "20.0")
        self.assertEqual(minutes_by_player["Team"], "30.0")


class LiveStatsPeriodScopeTests(unittest.TestCase):
    def test_live_stats_period_scope_filters_rows(self) -> None:
        raw_rows = [
            {
                "team_id": "2540",
                "type": "Made Shot",
                "text": "Made Jumper",
                "period": "1st Half",
                "scoring_play": True,
                "shooting_play": True,
                "score_value": 2,
                "points_attempted": 2,
                "athlete_id": "11",
                "assist_athlete_id": "",
            },
            {
                "team_id": "2540",
                "type": "Made Shot",
                "text": "Made Three",
                "period": "2nd Half",
                "scoring_play": True,
                "shooting_play": True,
                "score_value": 3,
                "points_attempted": 3,
                "athlete_id": "11",
                "assist_athlete_id": "",
            },
        ]
        with patch("apps.api.server.load_pbp_rows", return_value=raw_rows):
            with patch("apps.api.server.get_service") as mock_service:
                mock_service.return_value.player_seconds_for_game.return_value = {
                    "2540": {"11": 600}
                }
                live_1st = build_live_stats_from_pbp(ucsb_team_id="2540", opponent_team_id="300", period_scope="1st")
                live_2nd = build_live_stats_from_pbp(ucsb_team_id="2540", opponent_team_id="300", period_scope="2nd")
                live_full = build_live_stats_from_pbp(ucsb_team_id="2540", opponent_team_id="300", period_scope="full")

        pts_1st = next(r for r in live_1st["ucsb_team"]["rows"] if r["stat_key"] == "pts")
        pts_2nd = next(r for r in live_2nd["ucsb_team"]["rows"] if r["stat_key"] == "pts")
        pts_full = next(r for r in live_full["ucsb_team"]["rows"] if r["stat_key"] == "pts")
        self.assertEqual(pts_1st["value"], "2")
        self.assertEqual(pts_2nd["value"], "3")
        self.assertEqual(pts_full["value"], "5")


class PbpAdvancedFilterTests(unittest.TestCase):
    def test_parse_pbp_filters_back_compat_empty(self) -> None:
        parsed = parse_pbp_filters({})
        self.assertEqual(parsed, PbpFilters())

    def test_parse_pbp_filters_last_n(self) -> None:
        parsed = parse_pbp_filters(
            {
                "team_id": ["UCSB,Opponent"],
                "type": ["Made Shot"],
                "text": ["jumper"],
                "period": ["2,OT"],
                "clock_mode": ["last_n"],
                "clock_last_n_minutes": ["2.5"],
            }
        )
        self.assertEqual(parsed.team_ids, ["UCSB", "Opponent"])
        self.assertEqual(parsed.periods, ["2", "ot"])
        self.assertEqual(parsed.clock_mode, "last_n")
        self.assertEqual(parsed.clock_last_n_minutes, 2.5)

    def test_parse_pbp_filters_invalid_clock(self) -> None:
        with self.assertRaises(PbpFilterValidationError):
            parse_pbp_filters({"clock_mode": ["range"], "clock_from": ["5:80"], "clock_to": ["2:00"]})

    def test_parse_pbp_filters_invalid_range_order(self) -> None:
        with self.assertRaises(PbpFilterValidationError):
            parse_pbp_filters({"clock_mode": ["range"], "clock_from": ["1:00"], "clock_to": ["5:00"]})

    def test_apply_pbp_filters_combined(self) -> None:
        rows = [
            {"team_id": "2540", "type": "Made Shot", "text": "made jumper", "period": "2nd", "clock": "01:45"},
            {"team_id": "300", "type": "Turnover", "text": "bad pass", "period": "2nd", "clock": "01:20"},
            {"team_id": "2540", "type": "Made Shot", "text": "corner three", "period": "1st", "clock": "00:40"},
        ]
        filters = parse_pbp_filters(
            {
                "team_id": ["UCSB"],
                "type": ["Made Shot"],
                "text": ["jumper"],
                "period": ["2"],
                "clock_mode": ["last_n"],
                "clock_last_n_minutes": ["2"],
            }
        )
        out = apply_pbp_filters(rows, filters, ucsb_team_id="2540")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "made jumper")

    def test_apply_pbp_filters_clock_range_inclusive(self) -> None:
        rows = [
            {"team_id": "2540", "type": "Made Shot", "text": "a", "period": "2nd", "clock": "05:00"},
            {"team_id": "2540", "type": "Made Shot", "text": "b", "period": "2nd", "clock": "03:00"},
            {"team_id": "2540", "type": "Made Shot", "text": "c", "period": "2nd", "clock": "02:00"},
            {"team_id": "2540", "type": "Made Shot", "text": "d", "period": "2nd", "clock": "01:59"},
        ]
        filters = parse_pbp_filters(
            {"clock_mode": ["range"], "clock_from": ["05:00"], "clock_to": ["02:00"], "period": ["2"]}
        )
        out = apply_pbp_filters(rows, filters, ucsb_team_id="2540")
        self.assertEqual([row["text"] for row in out], ["a", "b", "c"])

    def test_apply_pbp_filters_last_n_edge_cases(self) -> None:
        rows = [
            {"team_id": "2540", "type": "Made Shot", "text": "0:30 play", "period": "4th", "clock": "00:30"},
            {"team_id": "2540", "type": "Made Shot", "text": "1:00 play", "period": "4th", "clock": "01:00"},
            {"team_id": "2540", "type": "Made Shot", "text": "1:01 play", "period": "4th", "clock": "01:01"},
        ]
        filters = parse_pbp_filters({"clock_mode": ["last_n"], "clock_last_n_minutes": ["1"], "period": ["4"]})
        out = apply_pbp_filters(rows, filters, ucsb_team_id="2540")
        self.assertEqual([row["text"] for row in out], ["0:30 play", "1:00 play"])

    def test_apply_pbp_filters_general_q(self) -> None:
        rows = [
            {"team_id": "2540", "type": "Made Shot", "text": "Corner three", "period": "2nd", "clock": "01:15"},
            {"team_id": "300", "type": "Turnover", "text": "Bad pass", "period": "2nd", "clock": "01:05"},
        ]
        filters = parse_pbp_filters({"q": ["turnover"]})
        out = apply_pbp_filters(rows, filters, ucsb_team_id="2540")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["type"], "Turnover")


if __name__ == "__main__":
    unittest.main()
