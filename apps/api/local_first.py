from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
VAR_DIR = REPO_ROOT / "var"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "sqlite_migrations"

SEASON_ID = "2025-2026"
SEASON_TYPE = "regular"
SEASON_YEAR = 2026
ROOT_TEAM_ID = "2540"
ROOT_TEAM_NAME = "UC Santa Barbara Gauchos"
ESPN_SITE_ROOT = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball"
ESPN_CORE_ROOT = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/mens-college-basketball"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

PLAYER_BASE_COLUMNS = [
    "Player",
    "GP-GS",
    "MIN",
    "PTS",
    "PPG",
    "REB",
    "RPG",
    "AST",
    "APG",
    "TO",
    "STL",
    "BLK",
    "PF",
]
PLAYER_APPEND_COLUMNS = [
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
]
PLAYER_TABLE_COLUMNS = ["row_key", *PLAYER_BASE_COLUMNS, *PLAYER_APPEND_COLUMNS]
LIVE_TEAM_COLUMNS = [
    "row_key",
    "team_id",
    "split",
    "stat_key",
    "stat_name",
    "value",
    "numeric_value",
    "display_value",
    "rank",
    "abbreviation",
]

_LAST_REQUEST_AT = 0.0
_REQUEST_LOCK = threading.Lock()
_GROUP_CACHE: Dict[str, Dict[str, Any]] = {}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(REPO_ROOT / ".env")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sqlite_db_path() -> Path:
    raw = os.environ.get("SQLITE_DB_PATH", "var/state.sqlite3").strip() or "var/state.sqlite3"
    return (REPO_ROOT / raw).resolve() if not raw.startswith("/") else Path(raw)


def object_store_root() -> Path:
    raw = os.environ.get("OBJECT_STORE_ROOT", "var/object_store").strip() or "var/object_store"
    return (REPO_ROOT / raw).resolve() if not raw.startswith("/") else Path(raw)


def request_timeout_seconds() -> int:
    return int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "45"))


def request_retry_count() -> int:
    return int(os.environ.get("REQUEST_RETRY_COUNT", "4"))


def min_request_delay_seconds() -> float:
    return float(os.environ.get("REQUEST_MIN_DELAY_SECONDS", "1.0"))


def max_backoff_seconds() -> int:
    return int(os.environ.get("REQUEST_MAX_BACKOFF_SECONDS", "8"))


def _respect_rate_limit() -> None:
    global _LAST_REQUEST_AT
    with _REQUEST_LOCK:
        elapsed = time.time() - _LAST_REQUEST_AT
        if elapsed < min_request_delay_seconds():
            time.sleep(min_request_delay_seconds() - elapsed)
        _LAST_REQUEST_AT = time.time()


def request_json(url: str) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(1, request_retry_count() + 1):
        try:
            _respect_rate_limit()
            response = requests.get(url, timeout=request_timeout_seconds(), headers=REQUEST_HEADERS)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError(f"Expected JSON object from {url}")
            return payload
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == request_retry_count():
                break
            time.sleep(min(2 ** (attempt - 1), max_backoff_seconds()))
    raise RuntimeError(f"Failed to fetch ESPN payload from {url}: {last_error}")


def schedule_config() -> Dict[str, Any]:
    return json.loads((CONFIG_DIR / "ucsb_2025_2026_regular_schedule.json").read_text(encoding="utf-8"))


def season_scope_config() -> Dict[str, Any]:
    return json.loads((CONFIG_DIR / "season_scope.json").read_text(encoding="utf-8"))


def extract_ref_id(ref_url: str) -> str:
    token = ref_url.rstrip("/").split("/")[-1]
    return token.split("?")[0]


def normalize_team_id(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]", "", str(value or "").lower()).strip("-")
    if not cleaned:
        raise ValueError("team_id must contain letters/numbers/hyphens")
    return cleaned


def team_id_matches(left: str, right: str) -> bool:
    return normalize_team_id(left) == normalize_team_id(right)


def stat_line() -> Dict[str, int]:
    return {
        "points": 0,
        "rebounds": 0,
        "assists": 0,
        "turnovers": 0,
        "steals": 0,
        "blocks": 0,
        "personal_fouls": 0,
        "fgm": 0,
        "fga": 0,
        "fg3m": 0,
        "fg3a": 0,
        "ftm": 0,
        "fta": 0,
        "layup_m": 0,
        "layup_a": 0,
        "dunk_m": 0,
        "dunk_a": 0,
        "mid_m": 0,
        "mid_a": 0,
        "dunks": 0,
        "tips": 0,
    }


def format_pct(makes: int, attempts: int) -> str:
    return f"{makes / attempts:.3f}".lstrip("0") if attempts > 0 else ""


def field_goal_breakdown_metrics(
    *,
    fgm: int,
    fga: int,
    fg3m: int,
    fg3a: int,
    mid_m: int,
    mid_a: int,
    layup_m: int,
    layup_a: int,
    dunk_m: int,
    dunk_a: int,
    tips: int,
) -> List[Tuple[str, str, str]]:
    two_pm = max(fgm - fg3m, 0)
    two_pa = max(fga - fg3a, 0)
    listed_two_pm = mid_m + layup_m + dunk_m
    listed_two_pa = mid_a + layup_a + dunk_a
    make_gap = two_pm - listed_two_pm
    attempt_gap = two_pa - listed_two_pa

    make_status = "OK" if make_gap == 0 else f"MISMATCH ({make_gap:+d})"
    attempt_status = "OK" if attempt_gap == 0 else f"MISMATCH ({attempt_gap:+d})"

    reasons: List[str] = []
    if attempt_gap != 0:
        tip_note = f"; includes {tips} tracked tip attempt{'s' if tips != 1 else ''}" if tips else ""
        reasons.append(
            f"FGA gap {attempt_gap:+d}: 2PT attempts not fully partitioned into MIDR/LAYUP/DUNK{tip_note}"
        )
    if make_gap != 0:
        tip_note = " and may include made tip-ins" if tips else ""
        reasons.append(
            f"FGM gap {make_gap:+d}: 2PT makes include shots outside listed make buckets{tip_note}"
        )
    if not reasons:
        reasons.append("FG totals reconcile across listed 3PT, MIDR, LAYUP, and DUNK buckets")

    return [
        ("fgm_breakdown", "FGM_BREAKDOWN", make_status),
        ("fga_breakdown", "FGA_BREAKDOWN", attempt_status),
        ("fg_breakdown_note", "FG_BREAKDOWN_NOTE", " | ".join(reasons)),
    ]


def player_display_row(
    *,
    row_key: str,
    player_name: str,
    games_played: int,
    games_started: int,
    points: int,
    rebounds: int,
    assists: int,
    turnovers: int,
    steals: int,
    blocks: int,
    personal_fouls: int,
    ftm: int,
    fta: int,
    fgm: int,
    fga: int,
    fg3m: int,
    fg3a: int,
    mid_m: int,
    mid_a: int,
    layup_m: int,
    layup_a: int,
    dunks: int,
    tips: int,
) -> Dict[str, str]:
    gp = max(int(games_played), 0)
    return {
        "row_key": row_key,
        "Player": player_name,
        "GP-GS": f"{gp}-{max(int(games_started), 0)}",
        "MIN": "",
        "PTS": str(points),
        "PPG": f"{points / gp:.1f}" if gp else "",
        "REB": str(rebounds),
        "RPG": f"{rebounds / gp:.1f}" if gp else "",
        "AST": str(assists),
        "APG": f"{assists / gp:.1f}" if gp else "",
        "TO": str(turnovers),
        "STL": str(steals),
        "BLK": str(blocks),
        "PF": str(personal_fouls),
        "FGM": str(fgm),
        "FGA": str(fga),
        "FG%": format_pct(fgm, fga),
        "3PM": str(fg3m),
        "3PA": str(fg3a),
        "3P%": format_pct(fg3m, fg3a),
        "MIDR_M": str(mid_m),
        "MIDR_A": str(mid_a),
        "MIDR%": format_pct(mid_m, mid_a),
        "LAYUP_M": str(layup_m),
        "LAYUP_A": str(layup_a),
        "LAYUP%": format_pct(layup_m, layup_a),
        "DUNKS": str(dunks),
        "TIPS": str(tips),
        "FTM": str(ftm),
        "FTA": str(fta),
        "FT%": format_pct(ftm, fta),
    }


def normalize_play_type(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def classify_shot_zone(play_type: str, points_attempted: int) -> Optional[str]:
    normalized = normalize_play_type(play_type)
    if normalized == "dunkshot":
        return "dunk"
    if normalized == "layupshot" and points_attempted == 2:
        return "layup"
    if normalized == "jumpshot" and points_attempted == 2:
        return "mid"
    return None


@dataclass(frozen=True)
class StoredObjectRef:
    key: str
    path: str
    content_type: str
    size_bytes: int


class LocalObjectStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or object_store_root()
        self.root.mkdir(parents=True, exist_ok=True)

    def put_bytes(self, key: str, content: bytes, content_type: str) -> StoredObjectRef:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return StoredObjectRef(key=key, path=str(path), content_type=content_type, size_bytes=len(content))

    def get_bytes(self, key: str) -> bytes:
        return (self.root / key).read_bytes()


def load_migrations() -> Sequence[Tuple[str, str]]:
    migrations: List[Tuple[str, str]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = path.stem.split("_", 1)[0]
        migrations.append((version, path.read_text(encoding="utf-8")))
    if not migrations:
        raise RuntimeError(f"No SQLite migrations found in {MIGRATIONS_DIR}")
    return tuple(migrations)


class RuntimeDB:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or sqlite_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def migrate(self) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
            for version, sql in load_migrations():
                if version in applied:
                    continue
                conn.executescript(sql)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, now_iso()),
                )
            conn.commit()

    def fetch_all(self, query: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def fetch_one(self, query: str, params: Sequence[Any] = ()) -> Optional[Dict[str, Any]]:
        with self.connect() as conn:
            row = conn.execute(query, params).fetchone()
            return dict(row) if row else None

    def execute(self, query: str, params: Sequence[Any] = ()) -> None:
        with self.connect() as conn:
            conn.execute(query, params)
            conn.commit()

    def executemany(self, query: str, params: Iterable[Sequence[Any]]) -> None:
        with self.connect() as conn:
            conn.executemany(query, list(params))
            conn.commit()


def raw_pbp_source_url(game_id: str, page_index: int, limit: int = 1000) -> str:
    return f"{ESPN_CORE_ROOT}/events/{game_id}/competitions/{game_id}/plays?limit={limit}&pageIndex={page_index}"


def fetch_schedule_payload(team_id: str) -> Dict[str, Any]:
    url = f"{ESPN_SITE_ROOT}/teams/{team_id}/schedule?season={SEASON_YEAR}&seasontype=2"
    return request_json(url)


def parse_schedule_payload(payload: Dict[str, Any], team_id: str) -> List[Dict[str, str]]:
    games: List[Dict[str, str]] = []
    normalized_team_id = normalize_team_id(team_id)
    for event in payload.get("events") or []:
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        competitors = competitions[0].get("competitors") or []
        ours = next(
            (
                item
                for item in competitors
                if team_id_matches(str(item.get("team", {}).get("id") or ""), normalized_team_id)
            ),
            None,
        )
        opp = next(
            (
                item
                for item in competitors
                if not team_id_matches(str(item.get("team", {}).get("id") or ""), normalized_team_id)
            ),
            None,
        )
        if not ours or not opp:
            continue
        games.append(
            {
                "game_id": str(event.get("id") or ""),
                "date": str(event.get("date") or "")[:10],
                "opponent_team_id": str(opp.get("team", {}).get("id") or ""),
                "opponent_name": str(opp.get("team", {}).get("displayName") or ""),
                "home_away": str(ours.get("homeAway") or ""),
            }
        )
    return games


def validate_schedule(live_games: Sequence[Dict[str, str]]) -> None:
    reference_games = schedule_config()["games"]
    live = [(g["game_id"], g["date"], g["opponent_team_id"], g["home_away"]) for g in live_games]
    ref = [(g["game_id"], g["date"], g["opponent_team_id"], g["home_away"]) for g in reference_games]
    if live != ref:
        raise RuntimeError("Live ESPN UCSB schedule does not match config/ucsb_2025_2026_regular_schedule.json")


def fetch_team_detail(team_id: str) -> Dict[str, Any]:
    return request_json(f"{ESPN_SITE_ROOT}/teams/{team_id}")


def fetch_group_detail(group_id: str) -> Dict[str, Any]:
    if group_id in _GROUP_CACHE:
        return _GROUP_CACHE[group_id]
    payload = request_json(f"{ESPN_CORE_ROOT}/groups/{group_id}?lang=en&region=us")
    _GROUP_CACHE[group_id] = payload
    return payload


def fetch_team_roster(team_id: str) -> Dict[str, str]:
    payload = request_json(f"{ESPN_SITE_ROOT}/teams/{team_id}/roster?season={SEASON_YEAR}")
    roster: Dict[str, str] = {}
    for athlete in payload.get("athletes") or []:
        athlete_id = str(athlete.get("id") or "").strip()
        if athlete_id:
            roster[athlete_id] = str(athlete.get("displayName") or athlete.get("fullName") or athlete_id)
    return roster


def supported_team_ids() -> List[str]:
    cfg = schedule_config()
    ids = {cfg["team_id"]}
    ids.update(game["opponent_team_id"] for game in cfg["games"])
    return sorted(ids)


def _participant_id(play: Dict[str, Any], accepted_types: Sequence[str]) -> str:
    for participant in play.get("participants") or []:
        if not isinstance(participant, dict):
            continue
        if str(participant.get("type") or "").lower() in accepted_types:
            athlete = participant.get("athlete") or {}
            ref = str(athlete.get("$ref") or "")
            if ref:
                return extract_ref_id(ref)
    return ""


def _first_participant_id(play: Dict[str, Any]) -> str:
    participants = play.get("participants") or []
    if not participants:
        return ""
    first = participants[0]
    if not isinstance(first, dict):
        return ""
    athlete = first.get("athlete") or {}
    ref = str(athlete.get("$ref") or "")
    return extract_ref_id(ref) if ref else ""


def fetch_pbp_archive(game_id: str) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    page_index = 1
    all_items: List[Dict[str, Any]] = []
    while True:
        source_url = raw_pbp_source_url(game_id, page_index)
        payload = request_json(source_url)
        items = payload.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError(f"Invalid PBP items payload for game {game_id}")
        all_items.extend(item for item in items if isinstance(item, dict))
        if page_index >= int(payload.get("pageCount") or 1) or not items:
            break
        page_index += 1
    archive_payload = {
        "game_id": game_id,
        "source_url": source_url,
        "page_count": page_index,
        "items": all_items,
    }
    normalized: List[Dict[str, Any]] = []
    for play in all_items:
        team_ref = str((play.get("team") or {}).get("$ref") or "")
        normalized.append(
            {
                "game_id": game_id,
                "play_key": str(play.get("id") or play.get("sequenceNumber") or ""),
                "espn_play_id": str(play.get("id") or ""),
                "sequence_number": int(play.get("sequenceNumber") or 0),
                "period_number": int((play.get("period") or {}).get("number") or 0),
                "period_display": str((play.get("period") or {}).get("displayValue") or ""),
                "clock": str((play.get("clock") or {}).get("displayValue") or ""),
                "clock_seconds": int((play.get("clock") or {}).get("value") or 0),
                "team_id": extract_ref_id(team_ref) if team_ref else "",
                "athlete_id": _participant_id(
                    play,
                    ("shooter", "scorer", "rebounder", "blocker", "stealer", "committer", "fouler"),
                )
                or _first_participant_id(play),
                "assist_athlete_id": _participant_id(play, ("assister",)),
                "play_type": str((play.get("type") or {}).get("text") or ""),
                "text": str(play.get("text") or ""),
                "scoring_play": 1 if play.get("scoringPlay") else 0,
                "shooting_play": 1 if play.get("shootingPlay") else 0,
                "score_value": int(play.get("scoreValue") or 0),
                "points_attempted": int(play.get("pointsAttempted") or 0),
                "home_score": int(play.get("homeScore") or 0),
                "away_score": int(play.get("awayScore") or 0),
                "wallclock": str(play.get("wallclock") or ""),
                "raw_payload": json.dumps(play, ensure_ascii=True),
            }
        )
    return archive_payload, normalized


def archive_payload_bytes(payload: Dict[str, Any]) -> Tuple[bytes, str]:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True).encode("utf-8")
    return raw, hashlib.sha256(raw).hexdigest()


def _is_free_throw(play: Dict[str, Any]) -> bool:
    return "free throw" in str(play.get("text") or "").lower() or "freethrow" in str(
        play.get("play_type") or ""
    ).lower()


def _update_zone(row: Dict[str, int], zone: Optional[str], made: bool) -> None:
    if zone == "layup":
        row["layup_a"] += 1
        if made:
            row["layup_m"] += 1
    elif zone == "dunk":
        row["dunk_a"] += 1
        if made:
            row["dunk_m"] += 1
    elif zone == "mid":
        row["mid_a"] += 1
        if made:
            row["mid_m"] += 1


def derive_game_stats(
    plays: Sequence[Dict[str, Any]],
    athlete_names_by_team: Dict[str, Dict[str, str]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    team_totals: Dict[str, Dict[str, int]] = {}
    player_totals: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def team_row(team_id: str) -> Dict[str, int]:
        if team_id not in team_totals:
            team_totals[team_id] = stat_line()
        return team_totals[team_id]

    def player_row(team_id: str, athlete_id: str) -> Dict[str, Any]:
        key = (team_id, athlete_id)
        if key not in player_totals:
            player_totals[key] = {
                "team_id": team_id,
                "player_key": athlete_id,
                "athlete_id": athlete_id,
                "player_name": athlete_names_by_team.get(team_id, {}).get(athlete_id, athlete_id),
                **stat_line(),
            }
        return player_totals[key]

    for play in plays:
        team_id = str(play.get("team_id") or "").strip()
        if not team_id:
            continue
        team_stats = team_row(team_id)
        athlete_id = str(play.get("athlete_id") or "").strip()
        assist_id = str(play.get("assist_athlete_id") or "").strip()
        player_stats = player_row(team_id, athlete_id) if athlete_id else None
        scoring_play = bool(play.get("scoring_play"))
        shooting_play = bool(play.get("shooting_play"))
        score_value = int(play.get("score_value") or 0)
        points_attempted = int(play.get("points_attempted") or 0)
        play_type_raw = str(play.get("play_type") or play.get("type") or "")
        play_type = normalize_play_type(play_type_raw)

        if scoring_play:
            team_stats["points"] += score_value
            if player_stats:
                player_stats["points"] += score_value

        if _is_free_throw(play):
            team_stats["fta"] += 1
            if player_stats:
                player_stats["fta"] += 1
            if scoring_play and score_value == 1:
                team_stats["ftm"] += 1
                if player_stats:
                    player_stats["ftm"] += 1

        if shooting_play and points_attempted in (2, 3):
            team_stats["fga"] += 1
            if player_stats:
                player_stats["fga"] += 1
            if points_attempted == 3:
                team_stats["fg3a"] += 1
                if player_stats:
                    player_stats["fg3a"] += 1
            zone = classify_shot_zone(play_type_raw, points_attempted)
            _update_zone(team_stats, zone, scoring_play)
            if player_stats:
                _update_zone(player_stats, zone, scoring_play)
            if play_type == "dunkshot":
                team_stats["dunks"] += 1
                if player_stats:
                    player_stats["dunks"] += 1
            if play_type == "tipshot":
                team_stats["tips"] += 1
                if player_stats:
                    player_stats["tips"] += 1

        if scoring_play and score_value in (2, 3):
            team_stats["fgm"] += 1
            if player_stats:
                player_stats["fgm"] += 1
            if score_value == 3:
                team_stats["fg3m"] += 1
                if player_stats:
                    player_stats["fg3m"] += 1

        if "rebound" in play_type:
            team_stats["rebounds"] += 1
            if player_stats:
                player_stats["rebounds"] += 1
        if "turnover" in play_type:
            team_stats["turnovers"] += 1
            if player_stats:
                player_stats["turnovers"] += 1
        if "steal" in play_type:
            team_stats["steals"] += 1
            if player_stats:
                player_stats["steals"] += 1
        if "block" in play_type:
            team_stats["blocks"] += 1
            if player_stats:
                player_stats["blocks"] += 1
        if "foul" in play_type:
            team_stats["personal_fouls"] += 1
            if player_stats:
                player_stats["personal_fouls"] += 1

        if assist_id:
            assister = player_row(team_id, assist_id)
            assister["assists"] += 1
            team_stats["assists"] += 1

    return list(team_totals.items()), list(player_totals.values())


def aggregate_rows(rows: Sequence[Dict[str, Any]], key_fields: Sequence[str]) -> List[Dict[str, Any]]:
    grouped: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    stat_fields = tuple(stat_line().keys())
    for row in rows:
        key = tuple(row[field] for field in key_fields)
        if key not in grouped:
            grouped[key] = {field: row[field] for field in key_fields}
            grouped[key]["games_played"] = 0
            if "athlete_id" in row:
                grouped[key]["athlete_id"] = row["athlete_id"]
            if "player_name" in row:
                grouped[key]["player_name"] = row["player_name"]
            if "player_key" in row:
                grouped[key]["player_key"] = row["player_key"]
            for field in stat_fields:
                grouped[key][field] = 0
        grouped[key]["games_played"] += int(row.get("games_played") or 0)
        for field in stat_fields:
            grouped[key][field] += int(row.get(field) or 0)
    return list(grouped.values())


class BuildService:
    def __init__(self, db: RuntimeDB, object_store: Optional[LocalObjectStore] = None) -> None:
        self.db = db
        self.object_store = object_store or LocalObjectStore()
        self._lock = threading.Lock()
        self.db.migrate()
        self.recover_incomplete_jobs()

    def recover_incomplete_jobs(self) -> None:
        timestamp = now_iso()
        self.db.execute(
            """
            UPDATE build_jobs
            SET status = 'failed',
                message = CASE
                    WHEN message = '' THEN 'Build interrupted before completion.'
                    ELSE message
                END,
                error_message = CASE
                    WHEN error_message = '' THEN 'Build interrupted by API restart or worker exit. Re-run the build.'
                    ELSE error_message
                END,
                finished_at = COALESCE(finished_at, ?),
                updated_at = ?
            WHERE status IN ('queued', 'running')
            """,
            (timestamp, timestamp),
        )

    def ensure_supported_teams(self) -> List[Dict[str, Any]]:
        existing = self.db.fetch_all("SELECT * FROM teams ORDER BY conference_name, display_name")
        if existing:
            return existing
        for team_id in supported_team_ids():
            payload = fetch_team_detail(team_id)
            team = payload.get("team") or {}
            groups = team.get("groups") or {}
            group_id = str(groups.get("id") or "")
            group_payload = fetch_group_detail(group_id) if group_id else {}
            school_name = str(team.get("location") or team.get("displayName") or team_id)
            display_name = str(team.get("displayName") or school_name)
            self.db.execute(
                """
                INSERT OR REPLACE INTO teams (
                    team_id, school_name, abbreviation, display_name, conference_name,
                    conference_abbreviation, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    team_id,
                    school_name,
                    str(team.get("abbreviation") or ""),
                    display_name,
                    str(group_payload.get("shortName") or group_payload.get("name") or "Independent / Other"),
                    str(group_payload.get("abbreviation") or ""),
                    json.dumps(payload, ensure_ascii=True),
                ),
            )
        return self.db.fetch_all("SELECT * FROM teams ORDER BY conference_name, display_name")

    def verify_and_persist_schedule(self, team_id: str = ROOT_TEAM_ID, force: bool = False) -> List[Dict[str, Any]]:
        normalized_team_id = normalize_team_id(team_id)
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS count FROM schedule_games WHERE season_id = ? AND season_type = ? AND team_id = ?",
            (SEASON_ID, SEASON_TYPE, normalized_team_id),
        )
        if force or not row or int(row["count"]) == 0:
            live_payload = fetch_schedule_payload(normalized_team_id)
            live_games = parse_schedule_payload(live_payload, normalized_team_id)
            if team_id_matches(normalized_team_id, ROOT_TEAM_ID):
                validate_schedule(live_games)
            self.db.execute(
                "INSERT OR REPLACE INTO seasons (season_id, season_type, label, is_locked) VALUES (?, ?, ?, 1)",
                (SEASON_ID, SEASON_TYPE, "2025-2026 Regular Season"),
            )
            self.db.execute(
                "DELETE FROM schedule_games WHERE season_id = ? AND season_type = ? AND team_id = ?",
                (SEASON_ID, SEASON_TYPE, normalized_team_id),
            )
            self.db.executemany(
                """
                INSERT OR REPLACE INTO schedule_games (
                    season_id, season_type, team_id, game_id, game_date, opponent_team_id,
                    opponent_name, home_away, schedule_source, schedule_verified_at, raw_payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        SEASON_ID,
                        SEASON_TYPE,
                        normalized_team_id,
                        game["game_id"],
                        game["date"],
                        game["opponent_team_id"],
                        game["opponent_name"],
                        game["home_away"],
                        "espn_verified",
                        now_iso(),
                        json.dumps(game, ensure_ascii=True),
                    )
                    for game in live_games
                ],
            )
            self.ensure_supported_teams()
        return self.db.fetch_all(
            """
            SELECT game_id, game_date AS date, opponent_team_id, opponent_name, home_away
            FROM schedule_games
            WHERE season_id = ? AND season_type = ? AND team_id = ?
            ORDER BY game_date, game_id
            """,
            (SEASON_ID, SEASON_TYPE, normalized_team_id),
        )

    def _set_job(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        current_game_index: int,
        total_games: int,
        message: str,
        error_message: str = "",
        current_game_id: str = "",
        current_opponent_team_id: str = "",
    ) -> None:
        started_at = now_iso() if status in {"running", "succeeded", "failed"} else None
        existing = self.db.fetch_one("SELECT started_at FROM build_jobs WHERE job_id = ?", (job_id,))
        self.db.execute(
            """
            UPDATE build_jobs
            SET status = ?,
                stage = ?,
                current_game_index = ?,
                total_games = ?,
                message = ?,
                error_message = ?,
                current_game_id = ?,
                current_opponent_team_id = ?,
                started_at = COALESCE(started_at, ?),
                finished_at = CASE WHEN ? IN ('succeeded', 'failed') THEN ? ELSE finished_at END,
                updated_at = ?
            WHERE job_id = ?
            """,
            (
                status,
                stage,
                current_game_index,
                total_games,
                message,
                error_message,
                current_game_id,
                current_opponent_team_id,
                existing["started_at"] if existing else started_at,
                status,
                now_iso(),
                now_iso(),
                job_id,
            ),
        )

    def get_job(self, job_id: str) -> Dict[str, Any]:
        job = self.db.fetch_one("SELECT * FROM build_jobs WHERE job_id = ?", (job_id,))
        if not job:
            raise ValueError("job not found")
        return job

    def start_schedule_job(self, force: bool = False) -> Dict[str, Any]:
        return self._start_job("schedule", ROOT_TEAM_ID, force)

    def start_season_job(self, team_id: str, force: bool = False) -> Dict[str, Any]:
        allowed = {normalize_team_id(team["team_id"]) for team in self.ensure_supported_teams()}
        if normalize_team_id(team_id) not in allowed:
            raise ValueError("team_id must be UCSB or a UCSB regular-season opponent")
        return self._start_job("season", team_id, force)

    def _start_job(self, job_type: str, team_id: str, force: bool) -> Dict[str, Any]:
        with self._lock:
            existing = self.db.fetch_one(
                """
                SELECT * FROM build_jobs
                WHERE job_type = ? AND requested_team_id = ? AND status IN ('queued', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (job_type, team_id),
            )
            if existing:
                return existing
            job_id = str(uuid.uuid4())
            self.db.execute(
                """
                INSERT INTO build_jobs (
                    job_id, job_type, status, stage, season_id, season_type, requested_team_id,
                    current_game_index, total_games, message, error_message, force_rebuild,
                    created_at, updated_at
                ) VALUES (?, ?, 'queued', 'queued', ?, ?, ?, 0, 0, '', '', ?, ?, ?)
                """,
                (job_id, job_type, SEASON_ID, SEASON_TYPE, team_id, 1 if force else 0, now_iso(), now_iso()),
            )
            target = self._run_schedule_job if job_type == "schedule" else self._run_season_job
            thread = threading.Thread(target=target, args=(job_id, team_id, force), daemon=True)
            thread.start()
            return self.get_job(job_id)

    def _run_schedule_job(self, job_id: str, _: str, force: bool) -> None:
        try:
            games = self.verify_and_persist_schedule(ROOT_TEAM_ID, force=force)
            self._set_job(
                job_id,
                status="succeeded",
                stage="schedule_discovery",
                current_game_index=len(games),
                total_games=len(games),
                message=f"Verified {len(games)} UCSB schedule games.",
            )
        except Exception as exc:  # noqa: BLE001
            self._set_job(
                job_id,
                status="failed",
                stage="schedule_discovery",
                current_game_index=0,
                total_games=0,
                message="Schedule build failed.",
                error_message=str(exc),
            )

    def _run_season_job(self, job_id: str, team_id: str, force: bool) -> None:
        try:
            games = self.verify_and_persist_schedule(team_id, force=force)
            self._set_job(
                job_id,
                status="running",
                stage="schedule_discovery",
                current_game_index=0,
                total_games=len(games),
                message="Schedule verified.",
            )
            if force:
                self.db.execute("DELETE FROM pbp_ingests")
                self.db.execute("DELETE FROM pbp_plays")
                self.db.execute("DELETE FROM game_player_stats")
                self.db.execute("DELETE FROM game_team_stats")
                self.db.execute("DELETE FROM season_player_stats")
                self.db.execute("DELETE FROM season_team_stats")

            for index, game in enumerate(games, start=1):
                self._set_job(
                    job_id,
                    status="running",
                    stage="pbp_ingest",
                    current_game_index=index,
                    total_games=len(games),
                    message=f"Ingesting {game['date']} vs {game['opponent_name']}",
                    current_game_id=game["game_id"],
                    current_opponent_team_id=game["opponent_team_id"],
                )
                self.ingest_game(game["game_id"], force=force)
                self._set_job(
                    job_id,
                    status="running",
                    stage="derive_game_stats",
                    current_game_index=index,
                    total_games=len(games),
                    message=f"Deriving game stats for {game['opponent_name']}",
                    current_game_id=game["game_id"],
                    current_opponent_team_id=game["opponent_team_id"],
                )
                self.derive_and_store_game_stats(game["game_id"], force=force)

            self._set_job(
                job_id,
                status="running",
                stage="aggregate_season_stats",
                current_game_index=len(games),
                total_games=len(games),
                message=f"Aggregating season stats for {team_id}",
            )
            self.aggregate_season_stats(force=force)
            self._set_job(
                job_id,
                status="succeeded",
                stage="aggregate_season_stats",
                current_game_index=len(games),
                total_games=len(games),
                message=f"Season build completed for {team_id}.",
            )
        except Exception as exc:  # noqa: BLE001
            current = self.get_job(job_id)
            self._set_job(
                job_id,
                status="failed",
                stage=str(current.get("stage") or "unknown"),
                current_game_index=int(current.get("current_game_index") or 0),
                total_games=int(current.get("total_games") or 0),
                message="Season build failed.",
                error_message=str(exc),
                current_game_id=str(current.get("current_game_id") or ""),
                current_opponent_team_id=str(current.get("current_opponent_team_id") or ""),
            )

    def ingest_game(self, game_id: str, force: bool = False) -> Dict[str, Any]:
        if not force and self.db.fetch_one("SELECT 1 FROM pbp_plays WHERE game_id = ? LIMIT 1", (game_id,)):
            latest = self.latest_pbp_metadata(game_id)
            count_row = self.db.fetch_one("SELECT COUNT(*) AS count FROM pbp_plays WHERE game_id = ?", (game_id,))
            return {
                "rows": int((count_row or {}).get("count") or 0),
                "source_url": latest.get("source_url") or raw_pbp_source_url(game_id, 1),
                "archive_path": latest.get("archive_path") or "",
                "updated_at": latest.get("fetched_at") or "",
            }
        archive_payload, plays = fetch_pbp_archive(game_id)
        raw_bytes, sha256 = archive_payload_bytes(archive_payload)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        key = f"espn/pbp/{SEASON_ID}/{game_id}/{stamp}_{sha256}.json"
        stored = self.object_store.put_bytes(key, raw_bytes, "application/json")
        ingest_id = str(uuid.uuid4())
        self.db.execute(
            """
            INSERT INTO pbp_ingests (
                id, game_id, fetched_at, source_url, http_status, payload_sha256, archive_key,
                archive_path, content_type, payload_size_bytes, request_attempts, raw_metadata
            ) VALUES (?, ?, ?, ?, 200, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ingest_id,
                game_id,
                now_iso(),
                archive_payload["source_url"],
                sha256,
                stored.key,
                stored.path,
                stored.content_type,
                stored.size_bytes,
                request_retry_count(),
                json.dumps({"page_count": archive_payload["page_count"]}, ensure_ascii=True),
            ),
        )
        if force:
            self.db.execute("DELETE FROM pbp_plays WHERE game_id = ?", (game_id,))
        self.db.executemany(
            """
            INSERT OR REPLACE INTO pbp_plays (
                game_id, play_key, espn_play_id, sequence_number, period_number, period_display,
                clock, clock_seconds, team_id, athlete_id, assist_athlete_id, play_type, text,
                scoring_play, shooting_play, score_value, points_attempted, home_score, away_score,
                wallclock, ingest_id, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    game_id,
                    play["play_key"],
                    play["espn_play_id"],
                    play["sequence_number"],
                    play["period_number"],
                    play["period_display"],
                    play["clock"],
                    play["clock_seconds"],
                    play["team_id"],
                    play["athlete_id"],
                    play["assist_athlete_id"],
                    play["play_type"],
                    play["text"],
                    play["scoring_play"],
                    play["shooting_play"],
                    play["score_value"],
                    play["points_attempted"],
                    play["home_score"],
                    play["away_score"],
                    play["wallclock"],
                    ingest_id,
                    play["raw_payload"],
                )
                for play in plays
            ],
        )
        return {
            "rows": len(plays),
            "source_url": archive_payload["source_url"],
            "archive_path": stored.path,
            "updated_at": now_iso(),
        }

    def raw_pbp_rows(self, game_id: Optional[str] = None) -> List[Dict[str, Any]]:
        target = game_id or self.default_game_id()
        rows = self.db.fetch_all(
            """
            SELECT espn_play_id AS id, sequence_number AS sequence, period_display AS period, clock, text,
                   play_type AS type, team_id, home_score, away_score, scoring_play, shooting_play,
                   score_value, points_attempted, wallclock, athlete_id, assist_athlete_id, play_key
            FROM pbp_plays
            WHERE game_id = ?
            ORDER BY sequence_number, play_key
            """,
            (target,),
        )
        return rows

    def derive_and_store_game_stats(self, game_id: str, force: bool = False) -> bool:
        rows = self.raw_pbp_rows(game_id)
        if not rows:
            self.db.execute("DELETE FROM game_team_stats WHERE game_id = ?", (game_id,))
            self.db.execute("DELETE FROM game_player_stats WHERE game_id = ?", (game_id,))
            return False
        team_ids = sorted({str(row["team_id"]) for row in rows if row.get("team_id")})
        athlete_names = {team_id: fetch_team_roster(team_id) for team_id in team_ids}
        team_rows, player_rows = derive_game_stats(rows, athlete_names)
        if force:
            self.db.execute("DELETE FROM game_team_stats WHERE game_id = ?", (game_id,))
            self.db.execute("DELETE FROM game_player_stats WHERE game_id = ?", (game_id,))
        else:
            self.db.execute("DELETE FROM game_team_stats WHERE game_id = ?", (game_id,))
            self.db.execute("DELETE FROM game_player_stats WHERE game_id = ?", (game_id,))
        self.db.executemany(
            """
            INSERT INTO game_team_stats (
                game_id, team_id, games_played, points, rebounds, assists, turnovers, steals, blocks,
                personal_fouls, fgm, fga, fg3m, fg3a, ftm, fta, layup_m, layup_a, dunk_m, dunk_a, mid_m, mid_a, dunks, tips
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    game_id,
                    team_id,
                    1,
                    stats["points"],
                    stats["rebounds"],
                    stats["assists"],
                    stats["turnovers"],
                    stats["steals"],
                    stats["blocks"],
                    stats["personal_fouls"],
                    stats["fgm"],
                    stats["fga"],
                    stats["fg3m"],
                    stats["fg3a"],
                    stats["ftm"],
                    stats["fta"],
                    stats["layup_m"],
                    stats["layup_a"],
                    stats["dunk_m"],
                    stats["dunk_a"],
                    stats["mid_m"],
                    stats["mid_a"],
                    stats["dunks"],
                    stats["tips"],
                )
                for team_id, stats in team_rows
            ],
        )
        self.db.executemany(
            """
            INSERT INTO game_player_stats (
                game_id, team_id, player_key, athlete_id, player_name, games_played, points, rebounds, assists,
                turnovers, steals, blocks, personal_fouls, fgm, fga, fg3m, fg3a, ftm, fta,
                layup_m, layup_a, dunk_m, dunk_a, mid_m, mid_a, dunks, tips
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    game_id,
                    row["team_id"],
                    row["player_key"],
                    row["athlete_id"],
                    row["player_name"],
                    1,
                    row["points"],
                    row["rebounds"],
                    row["assists"],
                    row["turnovers"],
                    row["steals"],
                    row["blocks"],
                    row["personal_fouls"],
                    row["fgm"],
                    row["fga"],
                    row["fg3m"],
                    row["fg3a"],
                    row["ftm"],
                    row["fta"],
                    row["layup_m"],
                    row["layup_a"],
                    row["dunk_m"],
                    row["dunk_a"],
                    row["mid_m"],
                    row["mid_a"],
                    row["dunks"],
                    row["tips"],
                )
                for row in player_rows
            ],
        )
        return True

    def aggregate_season_stats(self, force: bool = False) -> None:
        if force:
            self.db.execute("DELETE FROM season_player_stats")
            self.db.execute("DELETE FROM season_team_stats")
        else:
            self.db.execute("DELETE FROM season_player_stats WHERE season_id = ? AND season_type = ?", (SEASON_ID, SEASON_TYPE))
            self.db.execute("DELETE FROM season_team_stats WHERE season_id = ? AND season_type = ?", (SEASON_ID, SEASON_TYPE))
        game_players = self.db.fetch_all("SELECT * FROM game_player_stats")
        game_teams = self.db.fetch_all("SELECT * FROM game_team_stats")
        aggregated_players = aggregate_rows(game_players, ("team_id", "player_key"))
        aggregated_teams = aggregate_rows(game_teams, ("team_id",))
        self.db.executemany(
            """
            INSERT INTO season_player_stats (
                season_id, season_type, team_id, player_key, athlete_id, player_name, games_played, points,
                rebounds, assists, turnovers, steals, blocks, personal_fouls, fgm, fga, fg3m, fg3a,
                ftm, fta, layup_m, layup_a, dunk_m, dunk_a, mid_m, mid_a, dunks, tips
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    SEASON_ID,
                    SEASON_TYPE,
                    row["team_id"],
                    row["player_key"],
                    row.get("athlete_id") or row["player_key"],
                    row.get("player_name") or row["player_key"],
                    row["games_played"],
                    row["points"],
                    row["rebounds"],
                    row["assists"],
                    row["turnovers"],
                    row["steals"],
                    row["blocks"],
                    row["personal_fouls"],
                    row["fgm"],
                    row["fga"],
                    row["fg3m"],
                    row["fg3a"],
                    row["ftm"],
                    row["fta"],
                    row["layup_m"],
                    row["layup_a"],
                    row["dunk_m"],
                    row["dunk_a"],
                    row["mid_m"],
                    row["mid_a"],
                    row["dunks"],
                    row["tips"],
                )
                for row in aggregated_players
            ],
        )
        self.db.executemany(
            """
            INSERT INTO season_team_stats (
                season_id, season_type, team_id, games_played, points, rebounds, assists, turnovers,
                steals, blocks, personal_fouls, fgm, fga, fg3m, fg3a, ftm, fta, layup_m, layup_a,
                dunk_m, dunk_a, mid_m, mid_a, dunks, tips
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    SEASON_ID,
                    SEASON_TYPE,
                    row["team_id"],
                    row["games_played"],
                    row["points"],
                    row["rebounds"],
                    row["assists"],
                    row["turnovers"],
                    row["steals"],
                    row["blocks"],
                    row["personal_fouls"],
                    row["fgm"],
                    row["fga"],
                    row["fg3m"],
                    row["fg3a"],
                    row["ftm"],
                    row["fta"],
                    row["layup_m"],
                    row["layup_a"],
                    row["dunk_m"],
                    row["dunk_a"],
                    row["mid_m"],
                    row["mid_a"],
                    row["dunks"],
                    row["tips"],
                )
                for row in aggregated_teams
            ],
        )

    def default_game_id(self) -> str:
        return schedule_config()["games"][0]["game_id"]

    def schedule_for_team(self, team_id: str) -> List[Dict[str, Any]]:
        normalized_team_id = normalize_team_id(team_id)
        games = self.verify_and_persist_schedule(normalized_team_id, force=False)
        return [
            {
                **game,
                "label": f"{game['date']} {'vs' if game['home_away'] == 'home' else 'at'} {game['opponent_name']}",
            }
            for game in games
        ]

    def season_team_rows(self, team_id: str) -> List[Dict[str, str]]:
        row = self.db.fetch_one(
            """
            SELECT * FROM season_team_stats
            WHERE season_id = ? AND season_type = ? AND team_id = ?
            """,
            (SEASON_ID, SEASON_TYPE, team_id),
        )
        if not row:
            return []
        metrics = [
            ("points", "Points", str(row["points"])),
            ("rebounds", "Rebounds", str(row["rebounds"])),
            ("assists", "Assists", str(row["assists"])),
            ("turnovers", "Turnovers", str(row["turnovers"])),
            ("steals", "Steals", str(row["steals"])),
            ("blocks", "Blocks", str(row["blocks"])),
            ("personal_fouls", "Personal Fouls", str(row["personal_fouls"])),
            ("fgm", "FGM", str(row["fgm"])),
            ("fga", "FGA", str(row["fga"])),
            ("fg_pct", "FG%", format_pct(int(row["fgm"]), int(row["fga"]))),
            ("fg3m", "3PM", str(row["fg3m"])),
            ("fg3a", "3PA", str(row["fg3a"])),
            ("fg3_pct", "3P%", format_pct(int(row["fg3m"]), int(row["fg3a"]))),
            ("mid_m", "MIDR_M", str(row["mid_m"])),
            ("mid_a", "MIDR_A", str(row["mid_a"])),
            ("mid_pct", "MIDR%", format_pct(int(row["mid_m"]), int(row["mid_a"]))),
            ("layup_m", "LAYUP_M", str(row["layup_m"])),
            ("layup_a", "LAYUP_A", str(row["layup_a"])),
            ("layup_pct", "LAYUP%", format_pct(int(row["layup_m"]), int(row["layup_a"]))),
            ("dunks", "DUNKS", str(row.get("dunks") or 0)),
            ("tips", "TIPS", str(row.get("tips") or 0)),
            ("ftm", "FTM", str(row["ftm"])),
            ("fta", "FTA", str(row["fta"])),
            ("ft_pct", "FT%", format_pct(int(row["ftm"]), int(row["fta"]))),
        ]
        metrics.extend(
            field_goal_breakdown_metrics(
                fgm=int(row["fgm"]),
                fga=int(row["fga"]),
                fg3m=int(row["fg3m"]),
                fg3a=int(row["fg3a"]),
                mid_m=int(row["mid_m"]),
                mid_a=int(row["mid_a"]),
                layup_m=int(row["layup_m"]),
                layup_a=int(row["layup_a"]),
                dunk_m=int(row["dunk_m"]),
                dunk_a=int(row["dunk_a"]),
                tips=int(row.get("tips") or 0),
            )
        )
        return [
            {"row_key": f"{team_id}_{key}", "metric": label, "value": value, "team": value, "opp": ""}
            for key, label, value in metrics
        ]

    def season_player_rows(self, team_id: str) -> List[Dict[str, str]]:
        rows = self.db.fetch_all(
            """
            SELECT * FROM season_player_stats
            WHERE season_id = ? AND season_type = ? AND team_id = ?
            ORDER BY points DESC, player_name
            """,
            (SEASON_ID, SEASON_TYPE, team_id),
        )
        if not rows:
            return []
        display_rows: List[Dict[str, str]] = []
        totals = stat_line()
        max_gp = 0
        for row in rows:
            gp = int(row["games_played"] or 0)
            max_gp = max(max_gp, gp)
            for key in totals:
                totals[key] += int(row[key] or 0)
            display_rows.append(
                player_display_row(
                    row_key=f"{team_id}_{row['player_key']}",
                    player_name=str(row["player_name"]),
                    games_played=gp,
                    games_started=0,
                    points=int(row["points"]),
                    rebounds=int(row["rebounds"]),
                    assists=int(row["assists"]),
                    turnovers=int(row["turnovers"]),
                    steals=int(row["steals"]),
                    blocks=int(row["blocks"]),
                    personal_fouls=int(row["personal_fouls"]),
                    ftm=int(row["ftm"]),
                    fta=int(row["fta"]),
                    fgm=int(row["fgm"]),
                    fga=int(row["fga"]),
                    fg3m=int(row["fg3m"]),
                    fg3a=int(row["fg3a"]),
                    mid_m=int(row["mid_m"]),
                    mid_a=int(row["mid_a"]),
                    layup_m=int(row["layup_m"]),
                    layup_a=int(row["layup_a"]),
                    dunks=int(row.get("dunks") or 0),
                    tips=int(row.get("tips") or 0),
                )
            )
        display_rows.append(
            player_display_row(
                row_key=f"{team_id}_team",
                player_name="Team",
                games_played=max_gp,
                games_started=max_gp,
                points=totals["points"],
                rebounds=totals["rebounds"],
                assists=totals["assists"],
                turnovers=totals["turnovers"],
                steals=totals["steals"],
                blocks=totals["blocks"],
                personal_fouls=totals["personal_fouls"],
                ftm=totals["ftm"],
                fta=totals["fta"],
                fgm=totals["fgm"],
                fga=totals["fga"],
                fg3m=totals["fg3m"],
                fg3a=totals["fg3a"],
                mid_m=totals["mid_m"],
                mid_a=totals["mid_a"],
                layup_m=totals["layup_m"],
                layup_a=totals["layup_a"],
                dunks=totals["dunks"],
                tips=totals["tips"],
            )
        )
        return display_rows

    def latest_pbp_metadata(self, game_id: str) -> Dict[str, Any]:
        row = self.db.fetch_one(
            "SELECT * FROM pbp_ingests WHERE game_id = ? ORDER BY fetched_at DESC LIMIT 1",
            (game_id,),
        )
        return row or {}


class LocalFirstService:
    def __init__(self, db_path: Optional[Path] = None, object_store_root: Optional[Path] = None) -> None:
        self.db = RuntimeDB(db_path)
        self.build_service = BuildService(self.db, LocalObjectStore(object_store_root))
        self._job_lock = threading.Lock()

    def connect(self) -> sqlite3.Connection:
        return self.db.connect()

    def load_schedule_reference(self) -> Dict[str, Any]:
        return schedule_config()

    def parse_schedule_payload(self, payload: Dict[str, Any], team_id: str = ROOT_TEAM_ID) -> List[Dict[str, str]]:
        return parse_schedule_payload(payload, team_id)

    def validate_schedule(self, live_games: Sequence[Dict[str, str]]) -> None:
        validate_schedule(live_games)

    def verify_and_persist_schedule(self, team_id: str = ROOT_TEAM_ID, force: bool = False) -> List[Dict[str, Any]]:
        return self.build_service.verify_and_persist_schedule(team_id, force=force)

    def supported_teams_payload(self, force_refresh: bool = False) -> Dict[str, Any]:
        self.verify_and_persist_schedule(ROOT_TEAM_ID, force=False)
        if force_refresh:
            self.db.execute("DELETE FROM teams")
        teams = [
            {
                "team_id": row["team_id"],
                "school_name": row["school_name"],
                "abbreviation": row["abbreviation"],
                "display_name": row["display_name"],
                "conference_name": row["conference_name"],
                "conference_abbreviation": row["conference_abbreviation"],
            }
            for row in self.build_service.ensure_supported_teams()
        ]
        return {
            "season": SEASON_ID,
            "season_year": SEASON_YEAR,
            "season_type": SEASON_TYPE,
            "teams": teams,
            "last_updated": now_iso(),
            "scope_note": season_scope_config().get("scope_note", ""),
        }

    def _allowed_team_ids(self) -> List[str]:
        ids = supported_team_ids()
        return [normalize_team_id(team_id) for team_id in ids]

    def list_schedule_games(self, team_id: str) -> List[Dict[str, Any]]:
        normalized_team_id = normalize_team_id(team_id)
        if normalized_team_id not in self._allowed_team_ids():
            raise ValueError("team_id must be UCSB or a UCSB regular-season opponent")
        return self.build_service.schedule_for_team(normalized_team_id)

    def player_dataset(self, team_id: str) -> Dict[str, Any]:
        normalized_team_id = normalize_team_id(team_id)
        if normalized_team_id not in self._allowed_team_ids():
            raise ValueError("team_id must be UCSB or a UCSB regular-season opponent")
        rows = self.build_service.season_player_rows(normalized_team_id)
        return {
            "team_id": normalized_team_id,
            "columns": PLAYER_TABLE_COLUMNS,
            "rows": rows,
        }

    def team_dataset(self, team_id: str) -> Dict[str, Any]:
        normalized_team_id = normalize_team_id(team_id)
        if normalized_team_id not in self._allowed_team_ids():
            raise ValueError("team_id must be UCSB or a UCSB regular-season opponent")
        rows = self.build_service.season_team_rows(normalized_team_id)
        return {
            "team_id": normalized_team_id,
            "columns": ["row_key", "metric", "value", "team", "opp"],
            "rows": rows,
        }

    def load_pbp_rows(self, game_id: str) -> List[Dict[str, Any]]:
        return self.build_service.raw_pbp_rows(game_id)

    def pbp_summary(self, game_id: str) -> Dict[str, Any]:
        latest = self.build_service.latest_pbp_metadata(game_id)
        count_row = self.db.fetch_one("SELECT COUNT(*) AS count FROM pbp_plays WHERE game_id = ?", (game_id,))
        return {
            "rows": int((count_row or {}).get("count") or 0),
            "updated_at": latest.get("fetched_at") or "",
            "source_url": latest.get("source_url") or raw_pbp_source_url(game_id, 1),
            "archive_path": latest.get("archive_path") or "",
        }

    def ingest_game(self, game_id: str, force: bool = False) -> Dict[str, Any]:
        return self.build_service.ingest_game(game_id, force=force)

    def derive_game_stats(self, game_id: str) -> bool:
        return self.build_service.derive_and_store_game_stats(game_id)

    def aggregate_season(self) -> None:
        self.build_service.aggregate_season_stats()

    def _set_job(
        self,
        job_id: str,
        *,
        status: str,
        stage: str,
        current_game_index: int,
        total_games: int,
        message: str,
        error_message: str = "",
        current_game_id: str = "",
        current_opponent_team_id: str = "",
    ) -> None:
        existing = self.db.fetch_one("SELECT started_at, finished_at FROM build_jobs WHERE job_id = ?", (job_id,)) or {}
        timestamp = now_iso()
        started_at = existing.get("started_at") or (timestamp if status in {"running", "succeeded", "failed"} else None)
        finished_at = timestamp if status in {"succeeded", "failed"} else existing.get("finished_at")
        self.db.execute(
            """
            UPDATE build_jobs
            SET status = ?,
                stage = ?,
                current_game_index = ?,
                total_games = ?,
                current_game_id = ?,
                current_opponent_team_id = ?,
                message = ?,
                error_message = ?,
                started_at = ?,
                finished_at = ?,
                updated_at = ?
            WHERE job_id = ?
            """,
            (
                status,
                stage,
                current_game_index,
                total_games,
                current_game_id,
                current_opponent_team_id,
                message,
                error_message,
                started_at,
                finished_at,
                timestamp,
                job_id,
            ),
        )

    def get_job(self, job_id: str) -> Dict[str, Any]:
        job = self.db.fetch_one("SELECT * FROM build_jobs WHERE job_id = ?", (job_id,))
        if not job:
            raise ValueError("job not found")
        return job

    def start_build(self, job_type: str, team_id: str, force: bool = False) -> Dict[str, Any]:
        normalized_team_id = normalize_team_id(team_id)
        if job_type not in {"schedule", "season"}:
            raise ValueError("job_type must be 'schedule' or 'season'")
        if job_type == "season" and normalized_team_id not in self._allowed_team_ids():
            raise ValueError("team_id must be UCSB or a UCSB regular-season opponent")
        target_team_id = ROOT_TEAM_ID if job_type == "schedule" else normalized_team_id
        with self._job_lock:
            existing = self.db.fetch_one(
                """
                SELECT * FROM build_jobs
                WHERE job_type = ? AND requested_team_id = ? AND status IN ('queued', 'running')
                ORDER BY created_at DESC LIMIT 1
                """,
                (job_type, target_team_id),
            )
            if existing:
                return existing
            job_id = str(uuid.uuid4())
            timestamp = now_iso()
            self.db.execute(
                """
                INSERT INTO build_jobs (
                    job_id, job_type, status, stage, season_id, season_type, requested_team_id,
                    current_game_id, current_opponent_team_id, current_game_index, total_games,
                    message, error_message, force_rebuild, created_at, started_at, finished_at, updated_at
                ) VALUES (?, ?, 'queued', 'queued', ?, ?, ?, '', '', 0, 0, '', '', ?, ?, NULL, NULL, ?)
                """,
                (
                    job_id,
                    job_type,
                    SEASON_ID,
                    SEASON_TYPE,
                    target_team_id,
                    1 if force else 0,
                    timestamp,
                    timestamp,
                ),
            )
            thread = threading.Thread(
                target=self._run_job,
                args=(job_id, job_type, target_team_id, force),
                daemon=True,
            )
            thread.start()
            return self.get_job(job_id)

    def _run_job(self, job_id: str, job_type: str, team_id: str, force: bool) -> None:
        try:
            schedule_team_id = ROOT_TEAM_ID if job_type == "schedule" else team_id
            games = self.verify_and_persist_schedule(schedule_team_id, force=force if job_type == "schedule" else force)
            self._set_job(
                job_id,
                status="running",
                stage="schedule_discovery",
                current_game_index=0 if job_type == "season" else len(games),
                total_games=len(games),
                message=f"Verified {len(games)} schedule games for {schedule_team_id}.",
            )
            if job_type == "schedule":
                self._set_job(
                    job_id,
                    status="succeeded",
                    stage="schedule_discovery",
                    current_game_index=len(games),
                    total_games=len(games),
                    message=f"Verified {len(games)} schedule games for {schedule_team_id}.",
                )
                return

            if force:
                self.db.execute("DELETE FROM pbp_ingests")
                self.db.execute("DELETE FROM pbp_plays")
                self.db.execute("DELETE FROM game_player_stats")
                self.db.execute("DELETE FROM game_team_stats")
                self.db.execute("DELETE FROM season_player_stats")
                self.db.execute("DELETE FROM season_team_stats")

            for index, game in enumerate(games, start=1):
                self._set_job(
                    job_id,
                    status="running",
                    stage="pbp_ingest",
                    current_game_index=index,
                    total_games=len(games),
                    message=f"Ingesting {game['date']} vs {game['opponent_name']}",
                    current_game_id=game["game_id"],
                    current_opponent_team_id=game["opponent_team_id"],
                )
                summary = self.ingest_game(game["game_id"], force=force)
                if int(summary.get("rows") or 0) == 0:
                    self._set_job(
                        job_id,
                        status="running",
                        stage="pbp_ingest",
                        current_game_index=index,
                        total_games=len(games),
                        message=f"No ESPN play-by-play is available yet for {game['opponent_name']}; skipped.",
                        current_game_id=game["game_id"],
                        current_opponent_team_id=game["opponent_team_id"],
                    )
                    continue
                self._set_job(
                    job_id,
                    status="running",
                    stage="derive_game_stats",
                    current_game_index=index,
                    total_games=len(games),
                    message=f"Deriving game stats for {game['opponent_name']}",
                    current_game_id=game["game_id"],
                    current_opponent_team_id=game["opponent_team_id"],
                )
                self.derive_game_stats(game["game_id"])

            self._set_job(
                job_id,
                status="running",
                stage="aggregate_season_stats",
                current_game_index=len(games),
                total_games=len(games),
                message=f"Aggregating season stats for {team_id}",
            )
            self.aggregate_season()
            self._set_job(
                job_id,
                status="succeeded",
                stage="aggregate_season_stats",
                current_game_index=len(games),
                total_games=len(games),
                message=f"Season build completed for {team_id}.",
            )
        except Exception as exc:  # noqa: BLE001
            current = self.db.fetch_one("SELECT * FROM build_jobs WHERE job_id = ?", (job_id,)) or {}
            self._set_job(
                job_id,
                status="failed",
                stage=str(current.get("stage") or "unknown"),
                current_game_index=int(current.get("current_game_index") or 0),
                total_games=int(current.get("total_games") or 0),
                message="Build failed.",
                error_message=str(exc),
                current_game_id=str(current.get("current_game_id") or ""),
                current_opponent_team_id=str(current.get("current_opponent_team_id") or ""),
            )


_SERVICE: Optional[LocalFirstService] = None
_SERVICE_LOCK = threading.Lock()


def get_service() -> LocalFirstService:
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                _SERVICE = LocalFirstService()
    return _SERVICE
