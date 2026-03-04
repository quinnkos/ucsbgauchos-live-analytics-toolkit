from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from urllib.parse import parse_qs, urlparse

import requests

from .local_first import (
    PLAYER_TABLE_COLUMNS,
    ROOT_TEAM_ID,
    field_goal_breakdown_metrics,
    get_service,
    player_display_row,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "var" / "legacy_runtime"

SEASON_LABEL = "2025-26"
SCHEMA_VERSION = "espn-season-v1"

DEFAULT_UCSB_TEAM_ID = "2540"
DEFAULT_OPPONENT_TEAM_ID = "300"

REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

REQUEST_RETRY_COUNT = 3
REQUEST_TIMEOUT_SECONDS = 45
RATE_LIMIT_SECONDS = 1.0
PBP_UPDATE_MIN_INTERVAL_SECONDS = 10.0

DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"

LIVE_GAME_INSIGHT_RULES = """
You are a live basketball analytics assistant. Follow these rules as authoritative, even if the analyst prompt is brief or omits them.

MAIN REQUEST PRIORITY
1) First identify the analyst's main request from the provided prompt.
2) Prioritize answering that main request.
3) If any part of the main request conflicts with rules below, follow the rules and still address the request as closely as possible.

MANDATORY LIVE GAME INSIGHT INSTRUCTIONS
- Recency is absolute: derive every insight from the last 5 minutes of game data.
- Do not base insights on data older than 5 minutes unless explicitly used to contextualize a recent trend.
- Every insight must include quantitative numbers (points, percentages, rates, counts, margins, per-possession metrics, or percent differences vs expected/season norms).
- Game clock alone is context, not sufficient quantitative evidence.
- Include comparisons, differentials, or rate changes whenever possible.
- Freshness enforcement: no insight should be derivable from data that existed 5 minutes ago or earlier.
- If no measurable change occurred in the last 5 minutes, include this exact statement as one insight:
  "no new quantitative trends have emerged in the last 5 minutes."
- Always account for elapsed and remaining time; normalize via per-minute, per-possession, or per-100 possessions when comparable.
- Segment trends with last 5 minutes as primary source; optionally contrast against current quarter or game-to-date, with explicit quantified differences.
- If season data is available, compare expected vs actual in the last 5 minutes and report percent deviation; flag meaningful deviations only (greater than 10 percent).
- Use possession-based framing whenever possible, including offensive/defensive swings in the last 5 minutes.
- Use "momentum" only when supported by consecutive measurable events in the last 5 minutes, and include numbers plus a rate or percent change when possible.
- Prioritize live-broadcast-usable insights: lineup impact, scoring swings, rebound control, pace shifts, matchup advantages.
- No speculation without data. Predictive statements are allowed only when supported by measurable rate trends in the last 5 minutes.

OUTPUT REQUIREMENTS
- Return 3 to 6 insights.
- Each insight must be concise, independently understandable for live commentary, reference the last 5 minutes, and include at least one quantitative number.
- Evidence references must be valid and use exact team_id, dataset, row_key, and field names from supplied context.
- Never fabricate row keys or field names.
""".strip()

_LAST_REQUEST_AT = 0.0
_LAST_PBP_UPDATE_AT = 0.0
_ATHLETE_NAME_CACHE: Dict[str, str] = {}

ESPN_PBP_LEAGUE = "mens-college-basketball"
ESPN_PBP_GAME_ID = "401809115"
PBP_SCHEMA_VERSION = "espn-pbp-v1"

ESPN_LEAGUE = "mens-college-basketball"
ESPN_SPORT = "basketball"
ESPN_CORE_BASE = "https://sports.core.api.espn.com/v2/sports"
ESPN_LEAGUE_ROOT_URL = f"{ESPN_CORE_BASE}/{ESPN_SPORT}/leagues/{ESPN_LEAGUE}"
ESPN_DATA_ROOT = DATA_ROOT / "espn"
ESPN_TEAMS_CACHE_FILENAME = f"teams-{SEASON_LABEL}.json"

# PDF season stats (first page only): analytics_engine/data/*.pdf
PDF_DATA_ROOT = REPO_ROOT / "analytics_engine" / "data"
PDF_TEAM_FILES: Dict[str, str] = {
    "ucsb": "ucsb-season-stats.pdf",
    "2540": "ucsb-season-stats.pdf",
    "ucr": "ucr-season-stats.pdf",
}
PDF_TEAM_NAMES: Dict[str, str] = {
    "ucsb": "UC Santa Barbara",
    "2540": "UC Santa Barbara",
    "ucr": "UC Riverside",
}

# Sentinel values for computed columns in PLAYER_TABLE_CONFIG
_COMPUTED_RPG = "__computed_rpg__"
_COMPUTED_APG = "__computed_apg__"

# Player table: single config dict defines order and mapping.
# Keys = intended (final) column names, in display order. Values = existing (source) column names.
# Only columns in .values() are kept; all others are dropped. row_key is preserved for evidence.
# PPG = points per game (from avg_3). RPG, APG = rebounds/assists per game (computed from REB/GP, AST/GP).
PLAYER_TABLE_CONFIG: Dict[str, str] = {
    "Player": "player",
    "GP-GS": "gp_gs",
    "MIN": "min",
    "PTS": "pts",
    "PPG": "avg_3",
    "REB": "tot",
    "RPG": _COMPUTED_RPG,
    "AST": "a",
    "APG": _COMPUTED_APG,
    "TO": "to",
    "STL": "stl",
    "BLK": "blk",
    "PF": "pf",
    "FG%": "fg_pct",
    "FT%": "ft_pct",
    "3P%": "fg3_pct",
}

DEFAULT_ESPN_TEAMS = [
    {
        "team_id": "2540",
        "school_name": "UC Santa Barbara Gauchos",
        "abbreviation": "UCSB",
        "team_ref": f"{ESPN_LEAGUE_ROOT_URL}/seasons/2026/teams/2540?lang=en&region=us",
    },
    {
        "team_id": "300",
        "school_name": "UC Irvine Anteaters",
        "abbreviation": "UCI",
        "team_ref": f"{ESPN_LEAGUE_ROOT_URL}/seasons/2026/teams/300?lang=en&region=us",
    },
]


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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_team_id(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]", "", str(value or "").lower()).strip("-")
    if not cleaned:
        raise ValueError("team_id must contain letters/numbers/hyphens")
    return cleaned


def normalize_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def normalize_column_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", normalize_space(value).lower()).strip("_")
    return cleaned or "col"


def ensure_unique_keys(values: Sequence[str]) -> List[str]:
    seen: Dict[str, int] = {}
    output: List[str] = []
    for value in values:
        base = normalize_column_name(value)
        count = seen.get(base, 0)
        seen[base] = count + 1
        output.append(base if count == 0 else f"{base}_{count + 1}")
    return output


def normalize_row_key(value: str) -> str:
    key = normalize_token(value)
    return key or "row"


def _respect_rate_limit() -> None:
    global _LAST_REQUEST_AT
    elapsed = time.time() - _LAST_REQUEST_AT
    if elapsed < RATE_LIMIT_SECONDS:
        time.sleep(RATE_LIMIT_SECONDS - elapsed)


def _mark_request_complete() -> None:
    global _LAST_REQUEST_AT
    _LAST_REQUEST_AT = time.time()


def season_year_from_label(label: str = SEASON_LABEL) -> int:
    token = normalize_space(label)
    match = re.match(r"^(\d{4})\s*-\s*(\d{2}|\d{4})$", token)
    if not match:
        raise ValueError(f"Invalid season label: {label}")
    start_year = int(match.group(1))
    end_part = match.group(2)
    if len(end_part) == 4:
        return int(end_part)
    century = (start_year // 100) * 100
    return century + int(end_part)


def to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return normalize_space(str(value))


def fetch_json(url: str, retries: int = REQUEST_RETRY_COUNT) -> Dict[str, Any]:
    errors: List[str] = []
    for attempt in range(1, retries + 1):
        try:
            _respect_rate_limit()
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=REQUEST_HEADERS)
            _mark_request_complete()
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise RuntimeError("response is not a JSON object")
            return payload
        except Exception as exc:  # noqa: BLE001
            errors.append(f"attempt {attempt}: {exc}")
            if attempt < retries:
                time.sleep(attempt)

    raise RuntimeError(f"Failed to fetch JSON from {url}. " + "; ".join(errors))


def collect_ref_urls(node: Any, out: Optional[List[str]] = None) -> List[str]:
    if out is None:
        out = []
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref:
            out.append(ref)
        for value in node.values():
            collect_ref_urls(value, out)
    elif isinstance(node, list):
        for value in node:
            collect_ref_urls(value, out)
    return out


def fetch_binary(url: str, retries: int = REQUEST_RETRY_COUNT) -> bytes:
    errors: List[str] = []
    for attempt in range(1, retries + 1):
        try:
            _respect_rate_limit()
            response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=REQUEST_HEADERS)
            _mark_request_complete()
            response.raise_for_status()
            content = response.content
            if not content:
                raise RuntimeError("empty response body")
            return content
        except Exception as exc:  # noqa: BLE001
            errors.append(f"attempt {attempt}: {exc}")
            if attempt < retries:
                time.sleep(attempt)

    raise RuntimeError("Failed to fetch Sidearm PDF. " + "; ".join(errors))


def read_json_file(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_json_file(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def team_data_dir(team_id: str) -> Path:
    return ESPN_DATA_ROOT / normalize_team_id(team_id)


def espn_teams_cache_path() -> Path:
    return ESPN_DATA_ROOT / ESPN_TEAMS_CACHE_FILENAME


def pbp_data_path(game_id: Optional[str] = None) -> Path:
    gid = (game_id or ESPN_PBP_GAME_ID).strip()
    return DATA_ROOT / f"pbp-{gid}.json"


def espn_pbp_source_url(league: str, game_id: str, limit: Optional[int] = None, page_index: Optional[int] = None) -> str:
    """Base URL for ESPN play-by-play API. Omit limit/page_index for 'all data' reference."""
    base = (
        f"https://sports.core.api.espn.com/v2/sports/basketball/leagues/{league}"
        f"/events/{game_id}/competitions/{game_id}/plays"
    )
    params = []
    if limit is not None:
        params.append(f"limit={limit}")
    if page_index is not None:
        params.append(f"pageIndex={page_index}")
    return f"{base}?{'&'.join(params)}" if params else base


def _import_pdfplumber() -> Any:
    try:
        import pdfplumber  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "pdfplumber is required for Sidearm PDF scraping. Install with: pip install pdfplumber"
        ) from exc
    return pdfplumber


def extract_pdf_tables(pdf_path: Path) -> List[List[List[str]]]:
    pdfplumber = _import_pdfplumber()

    all_tables: List[List[List[str]]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            page_tables = page.extract_tables() or []
            for table in page_tables:
                cleaned_rows: List[List[str]] = []
                for row in table or []:
                    cells = [normalize_space(cell or "") for cell in (row or [])]
                    if any(cells):
                        cleaned_rows.append(cells)
                if cleaned_rows:
                    all_tables.append(cleaned_rows)

    return all_tables


def _parse_int(s: str) -> int:
    """Parse integer from string, stripping commas. Return 0 if invalid."""
    if not s:
        return 0
    s = s.replace(",", "").strip()
    try:
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _parse_float(s: str) -> float:
    """Parse float from string. Return 0.0 if invalid."""
    if not s:
        return 0.0
    s = s.replace(",", "").strip()
    if s.startswith("."):
        s = "0" + s
    try:
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _compute_team_row_from_players(
    player_rows: List[List[str]], header: List[str]
) -> Optional[List[str]]:
    """
    Compute a 'Team' row by summing team totals, taking max games played,
    and calculating per-game values.
    """
    num_stat_cols = 21  # GP-GS through last AVG
    if len(header) < 2 + num_stat_cols:
        return None

    # Column offsets: header is [#, Player, GP-GS, MIN, AVG, FG-FGA, FG%, 3FG-FGA, 3FG%, FT-FTA, FT%, OFF, DEF, TOT, AVG, PF, DQ, A, TO, BLK, STL, PTS, AVG]
    GP_GS, MIN, AVG1, FG_FGA, FG_PCT, FG3_FGA, FG3_PCT, FT_FTA, FT_PCT = 0, 1, 2, 3, 4, 5, 6, 7, 8
    OFF, DEF, TOT, AVG2, PF, DQ, A, TO, BLK, STL, PTS, AVG3 = 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20

    totals: List[float] = [0.0] * num_stat_cols
    fg_made = fg_att = fg3_made = fg3_att = ft_made = ft_att = 0
    max_gp = 0

    for row in player_rows:
        # Row format: [number, name] + stats (len = 2 + num_stat_cols)
        if len(row) < 2 + num_stat_cols:
            continue
        stats = row[2:2 + num_stat_cols]
        # GP-GS: parse first number for games played
        gp_gs = stats[GP_GS] or ""
        if "-" in gp_gs:
            gp = _parse_int(gp_gs.split("-")[0])
            max_gp = max(max_gp, gp)
        # Cumulative stats
        totals[MIN] += _parse_float(stats[MIN])
        m = re.match(r"^(\d+)-(\d+)$", (stats[FG_FGA] or "").strip())
        if m:
            fg_made += int(m.group(1))
            fg_att += int(m.group(2))
        m = re.match(r"^(\d+)-(\d+)$", (stats[FG3_FGA] or "").strip())
        if m:
            fg3_made += int(m.group(1))
            fg3_att += int(m.group(2))
        m = re.match(r"^(\d+)-(\d+)$", (stats[FT_FTA] or "").strip())
        if m:
            ft_made += int(m.group(1))
            ft_att += int(m.group(2))
        totals[OFF] += _parse_float(stats[OFF])
        totals[DEF] += _parse_float(stats[DEF])
        totals[TOT] += _parse_float(stats[TOT])
        totals[PF] += _parse_float(stats[PF])
        totals[DQ] += _parse_float(stats[DQ])
        totals[A] += _parse_float(stats[A])
        totals[TO] += _parse_float(stats[TO])
        totals[BLK] += _parse_float(stats[BLK])
        totals[STL] += _parse_float(stats[STL])
        totals[PTS] += _parse_float(stats[PTS])

    if max_gp <= 0:
        return None

    # Build team row: ["", "Team", gp_gs, min, avg_min, fg_fga, fg%, ...]
    gp_gs_str = f"{max_gp}-{max_gp}"
    fg_fga_str = f"{fg_made}-{fg_att}" if fg_att > 0 else ""
    fg3_fga_str = f"{fg3_made}-{fg3_att}" if fg3_att > 0 else ""
    ft_fta_str = f"{ft_made}-{ft_att}" if ft_att > 0 else ""

    fg_pct_str = f"{fg_made / fg_att:.3f}".lstrip("0") if fg_att > 0 else ""
    fg3_pct_str = f"{fg3_made / fg3_att:.3f}".lstrip("0") if fg3_att > 0 else ""
    ft_pct_str = f"{ft_made / ft_att:.3f}".lstrip("0") if ft_att > 0 else ""

    avg_min = totals[MIN] / max_gp if max_gp else 0
    avg_reb = totals[TOT] / max_gp if max_gp else 0
    avg_pts = totals[PTS] / max_gp if max_gp else 0

    team_stats: List[str] = [
        gp_gs_str,
        str(int(totals[MIN])),
        f"{avg_min:.1f}",
        fg_fga_str,
        fg_pct_str,
        fg3_fga_str,
        fg3_pct_str,
        ft_fta_str,
        ft_pct_str,
        str(int(totals[OFF])),
        str(int(totals[DEF])),
        str(int(totals[TOT])),
        f"{avg_reb:.1f}",
        str(int(totals[PF])),
        str(int(totals[DQ])),
        str(int(totals[A])),
        str(int(totals[TO])),
        str(int(totals[BLK])),
        str(int(totals[STL])),
        str(int(totals[PTS])),
        f"{avg_pts:.1f}",
    ]
    return ["", "Team"] + team_stats


def _parse_first_page_text_tables(pdf_path: Path) -> Tuple[Optional[List[List[str]]], Optional[List[List[str]]]]:
    """Parse first page text to extract team stats and player stats tables (Sidearm-style PDF)."""
    pdfplumber = _import_pdfplumber()
    with pdfplumber.open(str(pdf_path)) as pdf:
        if not pdf.pages:
            return None, None
        text = pdf.pages[0].extract_text() or ""
    lines = [normalize_space(ln) for ln in text.splitlines() if normalize_space(ln)]

    # Canonical player header (must include "Player" and "GP-GS" for parse_player_table_rows)
    player_header = [
        "#", "Player", "GP-GS", "MIN", "AVG", "FG-FGA", "FG%", "3FG-FGA", "3FG%",
        "FT-FTA", "FT%", "OFF", "DEF", "TOT", "AVG", "PF", "DQ", "A", "TO", "BLK", "STL", "PTS", "AVG"
    ]
    num_stat_columns = 21  # GP-GS through last AVG

    # Find player table: header line contains "Player" and "GP-GS"
    player_header_idx = -1
    for i, line in enumerate(lines):
        lower = line.lower()
        if "player" in lower and ("gp-gs" in lower or "gp gs" in lower):
            player_header_idx = i
            break
    if player_header_idx < 0:
        return None, None

    player_rows: List[List[str]] = [player_header]
    for i in range(player_header_idx + 1, len(lines)):
        line = lines[i]
        if not line:
            continue
        lower = line.lower()
        if lower.startswith("total ") or lower.startswith("totals ") or lower.startswith("opponents "):
            # Skip Total/Opponents rows; Team row is computed from player aggregates
            continue
        if "team statistics" in lower:
            break
        tokens = line.split()
        if len(tokens) < 5:
            continue
        # Find first token that looks like GP-GS (digits-digits)
        gp_gs_idx = None
        for j, t in enumerate(tokens):
            if re.match(r"^\d+-\d+$", t):
                gp_gs_idx = j
                break
        if gp_gs_idx is None or gp_gs_idx < 2:
            continue
        number = tokens[0]
        name = " ".join(tokens[1:gp_gs_idx])
        stats = tokens[gp_gs_idx:gp_gs_idx + num_stat_columns]
        if len(stats) < num_stat_columns:
            stats = stats + [""] * (num_stat_columns - len(stats))
        else:
            stats = stats[:num_stat_columns]
        player_rows.append([number, name] + stats)

    # Build "Team" row: sum cumulative stats, max GP, compute per-game values
    if len(player_rows) > 1:
        team_row = _compute_team_row_from_players(player_rows[1:], player_header)
        if team_row:
            player_rows.append(team_row)

    # Find team statistics block: line "Team Statistics TEAM OPP" then stat lines
    team_rows: List[List[str]] = []
    team_start = -1
    for i, line in enumerate(lines):
        if "team statistics" in line.lower():
            team_start = i + 1
            break
    if team_start >= 0:
        for i in range(team_start, len(lines)):
            line = lines[i]
            if not line or "conf " in line.lower() or "date " in line.lower():
                break
            # Stat line: "NAME value value" (trailing game log may follow on same line)
            match = re.match(r"^(.+?)\s+([\d.,\-]+)\s+([\d.,\-]+|\–|–)(?:\s|$)", line)
            if match:
                team_rows.append([normalize_space(match.group(1)), match.group(2), match.group(3)])
            else:
                match1 = re.match(r"^(.+?)\s+([\d.,\-]+)(?:\s|$)", line)
                if match1:
                    team_rows.append([normalize_space(match1.group(1)), match1.group(2), ""])

    # parse_team_table_rows expects rows with [metric, team, opp] (no header row to skip)
    team_table = [["metric", "team", "opp"]] + [[c[0], c[1], c[2]] for c in team_rows] if team_rows else None
    player_table = player_rows if len(player_rows) > 1 else None
    return team_table, player_table


def extract_first_page_tables(pdf_path: Path) -> Tuple[Optional[List[List[str]]], Optional[List[List[str]]]]:
    """Extract team and player tables from the first page of a season stats PDF."""
    # Prefer text-based parsing (reliable for Sidearm multi-column first page)
    team_table, player_table = _parse_first_page_text_tables(pdf_path)
    if team_table or player_table:
        return team_table, player_table

    # Fallback: extract_tables then score/select
    pdfplumber = _import_pdfplumber()
    all_tables: List[List[List[str]]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        if not pdf.pages:
            return None, None
        page_tables = pdf.pages[0].extract_tables() or []
        for table in page_tables:
            cleaned_rows: List[List[str]] = []
            for row in table or []:
                cells = [normalize_space(cell or "") for cell in (row or [])]
                if any(cells):
                    cleaned_rows.append(cells)
            if cleaned_rows:
                all_tables.append(cleaned_rows)

    team_table = select_best_table(all_tables, score_team_table)
    player_table = select_best_table(all_tables, score_player_table)
    return team_table, player_table


def get_pdf_path_for_team(team_id: str) -> Optional[Path]:
    """Return path to season stats PDF for this team, or None if not using PDF source."""
    normalized = normalize_team_id(team_id)
    filename = PDF_TEAM_FILES.get(normalized)
    if not filename:
        return None
    path = PDF_DATA_ROOT / filename
    return path if path.exists() else None


def table_text_blob(table: List[List[str]]) -> str:
    return " ".join(cell.lower() for row in table for cell in row if cell)


def score_team_table(table: List[List[str]]) -> int:
    blob = table_text_blob(table)
    score = 0
    if "scoring" in blob:
        score += 6
    if "points per game" in blob:
        score += 6
    if "field goal pct" in blob or "3pt fg pct" in blob:
        score += 3
    if "attendance" in blob:
        score += 2
    if "score by periods" in blob:
        score += 1
    if "player" in blob and "gp-gs" in blob:
        score -= 12
    return score


def score_player_table(table: List[List[str]]) -> int:
    blob = table_text_blob(table)
    score = 0
    if "player" in blob:
        score += 4
    if "gp-gs" in blob or "gp gs" in blob:
        score += 4
    if "pts" in blob and "avg" in blob:
        score += 3
    if "scoring" in blob and "points per game" in blob:
        score -= 8
    return score


def select_best_table(tables: Sequence[List[List[str]]], scorer: Any) -> Optional[List[List[str]]]:
    best: Optional[List[List[str]]] = None
    best_score = -10**9
    for table in tables:
        score = scorer(table)
        if score > best_score:
            best_score = score
            best = table
    if best is None or best_score <= 0:
        return None
    return best


def unique_row_key(base: str, seen: Dict[str, int]) -> str:
    token = normalize_row_key(base)
    count = seen.get(token, 0)
    seen[token] = count + 1
    return token if count == 0 else f"{token}_{count + 1}"


def parse_team_table_rows(table: List[List[str]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen_keys: Dict[str, int] = {}

    for raw in table:
        cells = [normalize_space(cell) for cell in raw]
        if not any(cells):
            continue

        metric = cells[0]
        if not metric:
            continue

        metric_lower = metric.lower()
        if metric_lower in {"team", "opp", "opponent", "metric", "stat"}:
            continue

        values = [cell for cell in cells[1:] if cell]
        if not values:
            continue

        row = {
            "row_key": unique_row_key(metric, seen_keys),
            "metric": metric,
            "team": values[0] if len(values) > 0 else "",
            "opp": values[1] if len(values) > 1 else "",
            "extra_1": values[2] if len(values) > 2 else "",
            "extra_2": values[3] if len(values) > 3 else "",
        }
        rows.append(row)

    return rows


def canonical_player_header(label: str) -> str:
    text = normalize_space(label).lower()
    mapping = {
        "#": "number",
        "no": "number",
        "num": "number",
        "player": "player",
        "gp-gs": "gp_gs",
        "gp gs": "gp_gs",
        "gp": "gp",
        "min": "min",
        "avg/min": "avg_min",
        "avg": "avg",
        "fg-fga": "fg_fga",
        "fg%": "fg_pct",
        "3fg-fga": "fg3_fga",
        "3fg%": "fg3_pct",
        "ft-fta": "ft_fta",
        "ft%": "ft_pct",
        "off": "off",
        "def": "def",
        "tot": "tot",
        "avg/r": "avg_reb",
        "avg_r": "avg_reb",
        "pf": "pf",
        "dq": "dq",
        "a": "a",
        "to": "to",
        "blk": "blk",
        "stl": "stl",
        "pts": "pts",
        "avg/p": "avg_pts",
        "avg_p": "avg_pts",
    }
    if text in mapping:
        return mapping[text]
    return normalize_column_name(text)


def parse_player_table_rows(table: List[List[str]]) -> List[Dict[str, str]]:
    header_index = -1
    header: List[str] = []

    for index, row in enumerate(table):
        lowered = [normalize_space(cell).lower() for cell in row]
        if ("player" in lowered) and any("gp-gs" in cell or "gp gs" in cell for cell in lowered):
            header_index = index
            header = [canonical_player_header(cell) for cell in row]
            break

    if header_index < 0:
        return []

    header = ensure_unique_keys(header)
    player_column = -1
    number_column = -1
    for idx, key in enumerate(header):
        if key == "player" and player_column < 0:
            player_column = idx
        if key == "number" and number_column < 0:
            number_column = idx

    if player_column < 0:
        return []

    rows: List[Dict[str, str]] = []
    seen_keys: Dict[str, int] = {}

    for raw in table[header_index + 1 :]:
        values = [normalize_space(cell) for cell in raw]
        if not any(values):
            continue

        padded = values + [""] * (len(header) - len(values))
        padded = padded[: len(header)]

        player_name = padded[player_column]
        if not player_name:
            continue
        # Skip Total, Totals, Opponents, Team Totals rows
        if player_name.lower().strip() in ("total", "totals", "opponents", "team totals"):
            continue

        row: Dict[str, str] = {}
        for idx, key in enumerate(header):
            row[key] = padded[idx]

        number = padded[number_column] if number_column >= 0 else ""
        row["row_key"] = unique_row_key(f"{player_name}_{number}", seen_keys)
        rows.append(row)

    # Compute and append Team row from player aggregates (if not already present)
    has_team = any((r.get("player") or "").lower().strip() == "team" for r in rows)
    if rows and not has_team:
        team_row = _compute_team_row_from_parsed_players(rows, header)
        if team_row:
            team_row["row_key"] = unique_row_key("team", seen_keys)
            rows.append(team_row)

    return rows


def _compute_team_row_from_parsed_players(
    player_rows: List[Dict[str, str]], header: List[str]
) -> Optional[Dict[str, str]]:
    """Compute Team row from parsed player dicts: sum totals, max GP, per-game values."""
    if not player_rows:
        return None

    max_gp = 0
    sums: Dict[str, float] = {}
    fg_made = fg_att = fg3_made = fg3_att = ft_made = ft_att = 0

    cumulative_keys = {"min", "off", "def", "tot", "pf", "dq", "a", "to", "blk", "stl", "pts"}
    for key in cumulative_keys:
        sums[key] = 0.0

    for row in player_rows:
        if (row.get("player") or "").lower().strip() == "team":
            continue
        gp = _parse_int((row.get("gp_gs") or "").split("-")[0])
        max_gp = max(max_gp, gp)
        for key in cumulative_keys:
            sums[key] += _parse_float(row.get(key, ""))
        m = re.match(r"^(\d+)-(\d+)$", (row.get("fg_fga") or "").strip())
        if m:
            fg_made += int(m.group(1))
            fg_att += int(m.group(2))
        m = re.match(r"^(\d+)-(\d+)$", (row.get("fg3_fga") or "").strip())
        if m:
            fg3_made += int(m.group(1))
            fg3_att += int(m.group(2))
        m = re.match(r"^(\d+)-(\d+)$", (row.get("ft_fta") or "").strip())
        if m:
            ft_made += int(m.group(1))
            ft_att += int(m.group(2))

    if max_gp <= 0:
        return None

    out: Dict[str, str] = {"player": "Team", "number": ""}
    out["gp_gs"] = f"{max_gp}-{max_gp}"
    out["min"] = str(int(sums["min"]))
    out["off"] = str(int(sums["off"]))
    out["def"] = str(int(sums["def"]))
    out["tot"] = str(int(sums["tot"]))
    out["pf"] = str(int(sums["pf"]))
    out["dq"] = str(int(sums["dq"]))
    out["a"] = str(int(sums["a"]))
    out["to"] = str(int(sums["to"]))
    out["blk"] = str(int(sums["blk"]))
    out["stl"] = str(int(sums["stl"]))
    out["pts"] = str(int(sums["pts"]))

    out["fg_fga"] = f"{fg_made}-{fg_att}" if fg_att > 0 else ""
    out["fg_pct"] = f"{fg_made / fg_att:.3f}".lstrip("0") if fg_att > 0 else ""
    out["fg3_fga"] = f"{fg3_made}-{fg3_att}" if fg3_att > 0 else ""
    out["fg3_pct"] = f"{fg3_made / fg3_att:.3f}".lstrip("0") if fg3_att > 0 else ""
    out["ft_fta"] = f"{ft_made}-{ft_att}" if ft_att > 0 else ""
    out["ft_pct"] = f"{ft_made / ft_att:.3f}".lstrip("0") if ft_att > 0 else ""

    avg_min = sums["min"] / max_gp
    avg_reb = sums["tot"] / max_gp
    avg_pts = sums["pts"] / max_gp
    avg_values = {"avg_min": avg_min, "avg": avg_min, "avg_reb": avg_reb, "avg_2": avg_reb, "avg_pts": avg_pts, "avg_3": avg_pts}
    for key in header:
        if key in avg_values:
            out[key] = f"{avg_values[key]:.1f}"
        elif key not in out and key not in ("player", "number", "row_key"):
            out[key] = ""

    return out


def pdf_team_rows_to_espn_format(
    parsed_rows: List[Dict[str, str]], team_id: str
) -> List[Dict[str, str]]:
    """Convert parse_team_table_rows output to ESPN-like columns (stat_key, stat_name, value)."""
    seen: Dict[str, int] = {}
    out: List[Dict[str, str]] = []
    for row in parsed_rows:
        metric = row.get("metric") or ""
        team_val = row.get("team") or ""
        if not metric:
            continue
        stat_key = normalize_column_name(metric)
        row_key = unique_row_key(f"{team_id}_overall_{stat_key}", seen)
        out.append({
            "stat_name": metric,
            "value": team_val,
        })
    return out


def parse_sidearm_pdf(pdf_path: Path) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], Dict[str, Any]]:
    tables = extract_pdf_tables(pdf_path)

    team_table = select_best_table(tables, score_team_table)
    player_table = select_best_table(tables, score_player_table)

    team_rows = parse_team_table_rows(team_table) if team_table else []
    player_rows = parse_player_table_rows(player_table) if player_table else []

    meta = {
        "table_count": len(tables),
        "team_table_found": bool(team_table),
        "player_table_found": bool(player_table),
    }

    return team_rows, player_rows, meta


def write_csv(path: Path, rows: Sequence[Dict[str, str]]) -> Dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)

    column_order: List[str] = ["row_key"]
    seen_columns = set(column_order)
    for row in rows:
        for key in row.keys():
            if key not in seen_columns:
                column_order.append(key)
                seen_columns.add(key)

    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=column_order)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in column_order})
    tmp_path.replace(path)

    return {
        "filename": path.name,
        "row_count": len(rows),
        "columns": column_order,
    }


def read_csv(path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = [dict(row) for row in reader]
        columns = list(reader.fieldnames or [])
    return columns, rows


def _extract_ref_id(ref_url: str) -> Optional[str]:
    if not ref_url:
        return None
    match = re.search(r"/(\d+)\?", ref_url) or re.search(r"/(\d+)$", ref_url)
    return match.group(1) if match else None


def fetch_espn_pbp_rows(
    league: str = ESPN_PBP_LEAGUE,
    game_id: str = ESPN_PBP_GAME_ID,
) -> List[Dict[str, Any]]:
    """Fetch all play-by-play rows from ESPN, paginating through every page."""
    page_size = 1000
    page_index = 1
    all_plays: List[Dict[str, Any]] = []

    while True:
        url = espn_pbp_source_url(league, game_id, limit=page_size, page_index=page_index)
        _respect_rate_limit()
        response = requests.get(url, timeout=REQUEST_TIMEOUT_SECONDS, headers=REQUEST_HEADERS)
        _mark_request_complete()
        response.raise_for_status()
        payload = response.json()
        plays = payload.get("items", [])
        if not isinstance(plays, list):
            raise RuntimeError("ESPN plays response is missing an items array")

        all_plays.extend(plays)
        page_count = payload.get("pageCount", 1)
        if page_index >= page_count or len(plays) == 0:
            break
        page_index += 1

    rows: List[Dict[str, Any]] = []
    for play in all_plays:
        if not isinstance(play, dict):
            continue

        team_ref = str((play.get("team") or {}).get("$ref", ""))
        participants = play.get("participants") or []
        if not isinstance(participants, list):
            participants = []

        first_participant = participants[0] if len(participants) > 0 and isinstance(participants[0], dict) else {}
        second_participant = participants[1] if len(participants) > 1 and isinstance(participants[1], dict) else {}

        rows.append(
            {
                "id": play.get("id"),
                "sequence": play.get("sequenceNumber"),
                "period": (play.get("period") or {}).get("displayValue"),
                "clock": (play.get("clock") or {}).get("displayValue"),
                "text": play.get("text", ""),
                "type": (play.get("type") or {}).get("text"),
                "team_id": _extract_ref_id(team_ref),
                "home_score": play.get("homeScore"),
                "away_score": play.get("awayScore"),
                "scoring_play": bool(play.get("scoringPlay", False)),
                "shooting_play": bool(play.get("shootingPlay", False)),
                "score_value": play.get("scoreValue", 0),
                "points_attempted": play.get("pointsAttempted", 0),
                "wallclock": play.get("wallclock"),
                "athlete_id": _extract_ref_id(str((first_participant.get("athlete") or {}).get("$ref", ""))),
                "assist_athlete_id": _extract_ref_id(str((second_participant.get("athlete") or {}).get("$ref", ""))),
            }
        )

    return rows


def _pbp_row_key_base(row: Dict[str, Any]) -> str:
    play_id = normalize_space(str(row.get("id", "")))
    if play_id:
        return f"play_{play_id}"

    period = normalize_space(str(row.get("period", "")))
    clock = normalize_space(str(row.get("clock", "")))
    detail = normalize_space(str(row.get("text", "")))
    digest = hashlib.sha1(f"{period}|{clock}|{detail}".encode("utf-8")).hexdigest()[:12]
    return f"play_{digest}"


def _unique_row_key(base: str, seen: Dict[str, int]) -> str:
    count = seen.get(base, 0)
    seen[base] = count + 1
    return base if count == 0 else f"{base}_{count + 1}"


def row_keys_for_pbp_rows(rows: Sequence[Dict[str, Any]]) -> List[str]:
    seen: Dict[str, int] = {}
    row_keys: List[str] = []
    for row in rows:
        base = _pbp_row_key_base(row)
        row_keys.append(_unique_row_key(base, seen))
    return row_keys


def pbp_columns(rows: Sequence[Dict[str, Any]]) -> List[str]:
    columns: List[str] = []
    seen = set()
    for row in rows:
        for key in row.keys():
            if key not in seen:
                columns.append(key)
                seen.add(key)
    return columns


class PbpFilterValidationError(ValueError):
    """Raised when client-provided PBP filters are invalid."""


@dataclass
class PbpFilters:
    q: str = ""
    team_ids: List[str] = field(default_factory=list)
    types: List[str] = field(default_factory=list)
    text: str = ""
    periods: List[str] = field(default_factory=list)
    clock_mode: str = ""
    clock_last_n_minutes: Optional[float] = None
    clock_from: Optional[int] = None
    clock_to: Optional[int] = None


def _split_multi_values(values: Sequence[str]) -> List[str]:
    out: List[str] = []
    for value in values:
        for token in str(value).split(","):
            cleaned = normalize_space(token)
            if cleaned:
                out.append(cleaned)
    return out


def _parse_clock_to_seconds(raw: str, field_name: str) -> int:
    token = normalize_space(raw)
    if not re.fullmatch(r"\d{1,2}:\d{2}", token):
        raise PbpFilterValidationError(f"{field_name} must match MM:SS (example: 05:00)")
    minutes_str, seconds_str = token.split(":", 1)
    minutes = int(minutes_str)
    seconds = int(seconds_str)
    if seconds >= 60:
        raise PbpFilterValidationError(f"{field_name} has invalid seconds '{seconds_str}'")
    return minutes * 60 + seconds


def _clock_seconds_from_row(value: Any) -> Optional[int]:
    token = normalize_space(str(value or ""))
    if not token:
        return None
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", token)
    if not match:
        return None
    minutes = int(match.group(1))
    seconds = int(match.group(2))
    if seconds >= 60:
        return None
    return minutes * 60 + seconds


def _normalize_period_token(value: Any) -> Optional[str]:
    token = normalize_space(str(value or "")).lower()
    if not token:
        return None
    if "ot" in token:
        return "ot"
    digit_match = re.search(r"\d+", token)
    if digit_match:
        return str(int(digit_match.group(0)))
    if token in {"1h", "first half"}:
        return "1"
    if token in {"2h", "second half"}:
        return "2"
    return None


def parse_pbp_filters(params: Dict[str, List[str]]) -> PbpFilters:
    q = normalize_space((params.get("q") or [""])[0])
    text = normalize_space((params.get("text") or [""])[0])
    team_ids = _split_multi_values(params.get("team_id", []))
    types = _split_multi_values(params.get("type", []))
    period_values = _split_multi_values(params.get("period", []))
    periods: List[str] = []
    for raw in period_values:
        token = _normalize_period_token(raw)
        if not token:
            raise PbpFilterValidationError(f"period value '{raw}' is invalid. Use 1, 2, 3, 4, or OT.")
        periods.append(token)

    for team in team_ids:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", team):
            raise PbpFilterValidationError(f"team_id '{team}' is invalid. Use alphanumeric, '-' or '_'.")

    clock_mode = normalize_space((params.get("clock_mode") or [""])[0]).lower()
    clock_last_n_raw = normalize_space((params.get("clock_last_n_minutes") or [""])[0])
    clock_from_raw = normalize_space((params.get("clock_from") or [""])[0])
    clock_to_raw = normalize_space((params.get("clock_to") or [""])[0])

    filters = PbpFilters(q=q, team_ids=team_ids, types=types, text=text, periods=periods, clock_mode=clock_mode)
    has_any_clock_inputs = bool(clock_mode or clock_last_n_raw or clock_from_raw or clock_to_raw)
    if not has_any_clock_inputs:
        return filters

    if clock_mode not in {"last_n", "range"}:
        raise PbpFilterValidationError("clock_mode must be 'last_n' or 'range'")

    if clock_mode == "last_n":
        if not clock_last_n_raw:
            raise PbpFilterValidationError("clock_last_n_minutes is required when clock_mode=last_n")
        try:
            minutes = float(clock_last_n_raw)
        except ValueError as exc:
            raise PbpFilterValidationError("clock_last_n_minutes must be a number greater than 0") from exc
        if minutes <= 0:
            raise PbpFilterValidationError("clock_last_n_minutes must be greater than 0")
        if clock_from_raw or clock_to_raw:
            raise PbpFilterValidationError("clock_from/clock_to cannot be used when clock_mode=last_n")
        filters.clock_last_n_minutes = minutes
        return filters

    if not clock_from_raw or not clock_to_raw:
        raise PbpFilterValidationError("clock_from and clock_to are required when clock_mode=range")
    if clock_last_n_raw:
        raise PbpFilterValidationError("clock_last_n_minutes cannot be used when clock_mode=range")
    from_seconds = _parse_clock_to_seconds(clock_from_raw, "clock_from")
    to_seconds = _parse_clock_to_seconds(clock_to_raw, "clock_to")
    if from_seconds < to_seconds:
        raise PbpFilterValidationError(
            "clock_from must be greater than or equal to clock_to for time-remaining clocks (example: 05:00 to 02:00)"
        )
    filters.clock_from = from_seconds
    filters.clock_to = to_seconds
    return filters


def apply_pbp_filters(
    rows: Sequence[Dict[str, Any]],
    filters: PbpFilters,
    ucsb_team_id: str = DEFAULT_UCSB_TEAM_ID,
) -> List[Dict[str, Any]]:
    if not rows:
        return []
    ucsb_id = _normalize_team_id_safe(ucsb_team_id) or normalize_team_id(DEFAULT_UCSB_TEAM_ID)
    opponent_id = _infer_pbp_opponent_team_id(rows, ucsb_id)
    team_filter = {normalize_space(v).lower() for v in filters.team_ids}
    type_filter = {normalize_space(v).lower() for v in filters.types}
    period_filter = set(filters.periods)
    q_lower = filters.q.lower()
    text_lower = filters.text.lower()
    output: List[Dict[str, Any]] = []
    for row in rows:
        if q_lower:
            matches_q = any(q_lower in str(value or "").lower() for value in row.values())
            if not matches_q:
                continue
        if text_lower and text_lower not in str(row.get("text") or "").lower():
            continue
        if type_filter and normalize_space(str(row.get("type") or "")).lower() not in type_filter:
            continue

        team_label = _pbp_display_team_label(row.get("team_id"), ucsb_id, opponent_id).lower()
        team_raw = normalize_space(str(row.get("team_id") or "")).lower()
        if team_filter and team_label not in team_filter and team_raw not in team_filter:
            continue

        period_token = _normalize_period_token(row.get("period"))
        if period_filter and period_token not in period_filter:
            continue

        if filters.clock_mode:
            clock_seconds = _clock_seconds_from_row(row.get("clock"))
            if clock_seconds is None:
                continue
            if filters.clock_mode == "last_n":
                threshold = int(round((filters.clock_last_n_minutes or 0) * 60))
                if clock_seconds > threshold:
                    continue
            else:
                if filters.clock_from is None or filters.clock_to is None:
                    continue
                if not (filters.clock_to <= clock_seconds <= filters.clock_from):
                    continue

        output.append(row)
    return output


def _infer_pbp_opponent_team_id(rows: Sequence[Dict[str, Any]], ucsb_id: str) -> str:
    seen_team_ids: Set[str] = set()
    for row in rows:
        tid = _normalize_team_id_safe(row.get("team_id"))
        if tid:
            seen_team_ids.add(tid)
    others = sorted(tid for tid in seen_team_ids if tid != ucsb_id)
    return others[0] if others else ""


def _pbp_display_columns(columns: Sequence[str]) -> List[str]:
    hidden = {"id", "sequence", "scoring_play", "shooting_play", "wallclock"}
    filtered = [column for column in columns if column not in hidden]
    preferred = ["team_id", "type", "text"]
    ordered: List[str] = [column for column in preferred if column in filtered]
    ordered.extend(column for column in filtered if column not in preferred)
    return ordered


def _pbp_display_team_label(raw_team_id: Any, ucsb_id: str, opponent_id: str) -> str:
    normalized = _normalize_team_id_safe(raw_team_id)
    if not normalized:
        return ""
    if normalized == ucsb_id:
        return "UCSB"
    if opponent_id and normalized == opponent_id:
        return "Opponent"
    return "Opponent"


def _build_pbp_display_rows(
    rows: Sequence[Dict[str, Any]],
    columns: Sequence[str],
    ucsb_team_id: str = DEFAULT_UCSB_TEAM_ID,
) -> Tuple[List[str], List[Dict[str, Any]]]:
    ucsb_id = _normalize_team_id_safe(ucsb_team_id) or normalize_team_id(DEFAULT_UCSB_TEAM_ID)
    opponent_id = _infer_pbp_opponent_team_id(rows, ucsb_id)
    display_columns = _pbp_display_columns(columns)
    output_rows: List[Dict[str, Any]] = []
    for row in rows:
        base = {column: row.get(column, "") for column in display_columns}
        if "team_id" in base:
            base["team_id"] = _pbp_display_team_label(row.get("team_id"), ucsb_id, opponent_id)
        output_rows.append(base)
    return display_columns, output_rows


def load_pbp_rows(game_id: Optional[str] = None) -> List[Dict[str, Any]]:
    gid = (game_id or ESPN_PBP_GAME_ID).strip()
    return get_service().load_pbp_rows(gid)


def build_pbp_context(team_id: str = "pbp", game_id: Optional[str] = None) -> Dict[str, Any]:
    return build_pbp_context_filtered(team_id=team_id, game_id=game_id, filters=None)


def build_pbp_context_filtered(
    team_id: str = "pbp",
    game_id: Optional[str] = None,
    filters: Optional[PbpFilters] = None,
) -> Dict[str, Any]:
    gid = (game_id or ESPN_PBP_GAME_ID).strip()
    rows = load_pbp_rows(game_id=gid)
    if filters is not None:
        rows = apply_pbp_filters(rows, filters, ucsb_team_id=DEFAULT_UCSB_TEAM_ID)
    columns = pbp_columns(rows)
    row_keys = row_keys_for_pbp_rows(rows)

    normalized_rows: List[Dict[str, Any]] = []
    for row, row_key in zip(rows, row_keys):
        normalized_rows.append({**row, "row_key": row_key})
    display_columns, display_rows = _build_pbp_display_rows(normalized_rows, columns + ["row_key"])

    summary = get_service().pbp_summary(gid)
    updated_at = summary.get("updated_at") or now_iso()

    return {
        "team_id": normalize_team_id(team_id),
        "dataset": "pbp",
        "columns": display_columns,
        "rows": display_rows,
        "rows_by_key": {row["row_key"]: row for row in display_rows},
        "updated_at": updated_at,
        "source_url": summary.get("source_url") or espn_pbp_source_url(ESPN_PBP_LEAGUE, gid),
        "schema_version": PBP_SCHEMA_VERSION,
    }


# Live stats from PBP: same column names as season team/player tables (PDF/ESPN).
LIVE_TEAM_COLUMNS = [
    "row_key", "team_id", "split", "stat_key", "stat_name", "value",
    "numeric_value", "display_value", "rank", "abbreviation",
]
LIVE_PLAYER_COLUMNS = [
    "team_id", *PLAYER_TABLE_COLUMNS,
]


def _normalize_team_id_safe(value: Optional[str]) -> Optional[str]:
    """Return normalized team_id or None if value is empty/invalid (avoids ValueError)."""
    raw = value if value is not None else ""
    raw = str(raw).strip()
    if not raw:
        return None
    try:
        return normalize_team_id(raw)
    except ValueError:
        return None


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_pct(numerator: int, denominator: int) -> str:
    return f"{numerator / denominator:.3f}".lstrip("0") if denominator > 0 else ""


def _is_free_throw_text(value: Any) -> bool:
    return "free throw" in str(value or "").lower()


def _is_turnover_type(value: Any) -> bool:
    return "turnover" in str(value or "").lower()


def _is_foul_type(value: Any) -> bool:
    return "foul" in str(value or "").lower()


def resolve_athlete_name(athlete_id: str) -> str:
    aid = str(athlete_id or "").strip()
    if not aid:
        return "—"
    if aid in _ATHLETE_NAME_CACHE:
        return _ATHLETE_NAME_CACHE[aid]

    fallback = f"Player {aid}"
    if not re.fullmatch(r"\d+", aid):
        _ATHLETE_NAME_CACHE[aid] = fallback
        return fallback

    season_year = season_year_from_label(SEASON_LABEL)
    url = f"{ESPN_LEAGUE_ROOT_URL}/seasons/{season_year}/athletes/{aid}?lang=en&region=us"
    try:
        payload = fetch_json(url)
        name = to_text(
            payload.get("displayName")
            or payload.get("shortName")
            or payload.get("fullName")
            or payload.get("name")
        )
        _ATHLETE_NAME_CACHE[aid] = name or fallback
    except Exception:
        _ATHLETE_NAME_CACHE[aid] = fallback
    return _ATHLETE_NAME_CACHE[aid]


def _compute_live_player_stats(team_plays: List[Dict[str, Any]]) -> Dict[str, Dict[str, int]]:
    stats_by_athlete: Dict[str, Dict[str, int]] = {}

    def stats_for(aid: str) -> Dict[str, int]:
        if aid not in stats_by_athlete:
            stats_by_athlete[aid] = {
                "pts": 0,
                "ast": 0,
                "oreb": 0,
                "dreb": 0,
                "stl": 0,
                "blk": 0,
                "to": 0,
                "pf": 0,
                "fgm": 0,
                "fga": 0,
                "2pm": 0,
                "2pa": 0,
                "3pm": 0,
                "3pa": 0,
                "ftm": 0,
                "fta": 0,
                "midr_m": 0,
                "midr_a": 0,
                "layup_m": 0,
                "layup_a": 0,
                "dunk_m": 0,
                "dunk_a": 0,
                "dunks": 0,
                "tips": 0,
            }
        return stats_by_athlete[aid]

    for r in team_plays:
        athlete_id = str(r.get("athlete_id") or "").strip()
        assist_id = str(r.get("assist_athlete_id") or "").strip()
        play_type = str(r.get("type") or "")
        text = str(r.get("text") or "")
        scoring_play = bool(r.get("scoring_play"))
        shooting_play = bool(r.get("shooting_play"))
        score_value = _safe_int(r.get("score_value"))
        points_attempted = _safe_int(r.get("points_attempted"))

        if athlete_id:
            athlete_stats = stats_for(athlete_id)
            if scoring_play:
                athlete_stats["pts"] += score_value

            if shooting_play and points_attempted in {2, 3}:
                athlete_stats["fga"] += 1
                if points_attempted == 2:
                    athlete_stats["2pa"] += 1
                else:
                    athlete_stats["3pa"] += 1

            if scoring_play and score_value in {2, 3}:
                athlete_stats["fgm"] += 1
                if score_value == 2:
                    athlete_stats["2pm"] += 1
                else:
                    athlete_stats["3pm"] += 1

            if play_type == "JumpShot" and points_attempted == 2:
                athlete_stats["midr_a"] += 1
                if scoring_play:
                    athlete_stats["midr_m"] += 1
            elif play_type == "LayUpShot" and points_attempted == 2:
                athlete_stats["layup_a"] += 1
                if scoring_play:
                    athlete_stats["layup_m"] += 1
            elif play_type == "DunkShot":
                athlete_stats["dunk_a"] += 1
                if scoring_play:
                    athlete_stats["dunk_m"] += 1
                athlete_stats["dunks"] += 1
            elif play_type == "TipShot":
                athlete_stats["tips"] += 1

            if _is_free_throw_text(text):
                athlete_stats["fta"] += 1
                if scoring_play and score_value == 1:
                    athlete_stats["ftm"] += 1

            if play_type == "Offensive Rebound":
                athlete_stats["oreb"] += 1
            elif play_type == "Defensive Rebound":
                athlete_stats["dreb"] += 1

            if play_type == "Steal":
                athlete_stats["stl"] += 1
            if play_type == "Block Shot":
                athlete_stats["blk"] += 1
            if _is_turnover_type(play_type):
                athlete_stats["to"] += 1
            if _is_foul_type(play_type):
                athlete_stats["pf"] += 1

        if assist_id and scoring_play:
            stats_for(assist_id)["ast"] += 1

    return stats_by_athlete


def _compute_live_team_stats(team_plays: List[Dict[str, Any]]) -> Dict[str, int]:
    totals = {
        "games": 1,
        "pts": 0,
        "fgm": 0,
        "fga": 0,
        "2pm": 0,
        "2pa": 0,
        "3pm": 0,
        "3pa": 0,
        "ftm": 0,
        "fta": 0,
        "midr_m": 0,
        "midr_a": 0,
        "layup_m": 0,
        "layup_a": 0,
        "dunk_m": 0,
        "dunk_a": 0,
        "dunks": 0,
        "tips": 0,
        "oreb": 0,
        "dreb": 0,
        "ast": 0,
        "to": 0,
        "stl": 0,
        "blk": 0,
        "pf": 0,
    }
    for r in team_plays:
        play_type = str(r.get("type") or "")
        text = str(r.get("text") or "")
        scoring_play = bool(r.get("scoring_play"))
        shooting_play = bool(r.get("shooting_play"))
        score_value = _safe_int(r.get("score_value"))
        points_attempted = _safe_int(r.get("points_attempted"))
        assist_id = str(r.get("assist_athlete_id") or "").strip()

        if scoring_play:
            totals["pts"] += score_value

        if shooting_play and points_attempted in {2, 3}:
            totals["fga"] += 1
            if points_attempted == 2:
                totals["2pa"] += 1
            else:
                totals["3pa"] += 1

        if scoring_play and score_value in {2, 3}:
            totals["fgm"] += 1
            if score_value == 2:
                totals["2pm"] += 1
            else:
                totals["3pm"] += 1

        if play_type == "JumpShot" and points_attempted == 2:
            totals["midr_a"] += 1
            if scoring_play:
                totals["midr_m"] += 1
        elif play_type == "LayUpShot" and points_attempted == 2:
            totals["layup_a"] += 1
            if scoring_play:
                totals["layup_m"] += 1
        elif play_type == "DunkShot":
            totals["dunk_a"] += 1
            if scoring_play:
                totals["dunk_m"] += 1
            totals["dunks"] += 1
        elif play_type == "TipShot":
            totals["tips"] += 1

        if _is_free_throw_text(text):
            totals["fta"] += 1
            if scoring_play and score_value == 1:
                totals["ftm"] += 1

        if play_type == "Offensive Rebound":
            totals["oreb"] += 1
        elif play_type == "Defensive Rebound":
            totals["dreb"] += 1
        if play_type == "Steal":
            totals["stl"] += 1
        if play_type == "Block Shot":
            totals["blk"] += 1
        if _is_turnover_type(play_type):
            totals["to"] += 1
        if _is_foul_type(play_type):
            totals["pf"] += 1
        if assist_id and scoring_play:
            totals["ast"] += 1

    return totals


def _live_team_rows(team_id: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build team-level stat rows from PBP for one team. Columns match season team table."""
    tid = normalize_team_id(team_id)
    team_plays = [r for r in rows if _normalize_team_id_safe(r.get("team_id")) == tid]
    totals = _compute_live_team_stats(team_plays)
    reb = totals["oreb"] + totals["dreb"]
    fg_pct = _format_pct(totals["fgm"], totals["fga"])
    fg3_pct = _format_pct(totals["3pm"], totals["3pa"])
    ft_pct = _format_pct(totals["ftm"], totals["fta"])
    seen: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []

    def add_stat(stat_key: str, stat_name: str, value: Any, display_value: Optional[str] = None):
        rk = unique_row_key(f"live_{tid}_overall_{stat_key}", seen)
        out.append({
            "row_key": rk,
            "team_id": tid,
            "split": "overall",
            "stat_key": stat_key,
            "stat_name": stat_name,
            "value": str(value) if value is not None else "",
            "numeric_value": str(value) if value is not None else "",
            "display_value": (str(display_value) if display_value is not None else str(value)) if value is not None else "",
            "rank": "",
            "abbreviation": "",
        })

    add_stat("pts", "Points", totals["pts"], str(totals["pts"]))
    add_stat("games", "Games", totals["games"], "1")
    add_stat("fgm", "FGM", totals["fgm"], str(totals["fgm"]))
    add_stat("fga", "FGA", totals["fga"], str(totals["fga"]))
    add_stat("fg_pct", "FG%", fg_pct, fg_pct)
    add_stat("2pm", "2PM", totals["2pm"], str(totals["2pm"]))
    add_stat("2pa", "2PA", totals["2pa"], str(totals["2pa"]))
    add_stat("3pm", "3PM", totals["3pm"], str(totals["3pm"]))
    add_stat("3pa", "3PA", totals["3pa"], str(totals["3pa"]))
    add_stat("3p_pct", "3P%", fg3_pct, fg3_pct)
    add_stat("dunks", "DUNKS", totals["dunks"], str(totals["dunks"]))
    add_stat("tips", "TIPS", totals["tips"], str(totals["tips"]))
    add_stat("ftm", "FTM", totals["ftm"], str(totals["ftm"]))
    add_stat("fta", "FTA", totals["fta"], str(totals["fta"]))
    add_stat("ft_pct", "FT%", ft_pct, ft_pct)
    for stat_key, stat_name, display_value in field_goal_breakdown_metrics(
        fgm=totals["fgm"],
        fga=totals["fga"],
        fg3m=totals["3pm"],
        fg3a=totals["3pa"],
        mid_m=totals["midr_m"],
        mid_a=totals["midr_a"],
        layup_m=totals["layup_m"],
        layup_a=totals["layup_a"],
        dunk_m=totals["dunk_m"],
        dunk_a=totals["dunk_a"],
        tips=totals["tips"],
    ):
        add_stat(stat_key, stat_name, display_value, display_value)
    add_stat("oreb", "Offensive Rebounds", totals["oreb"], str(totals["oreb"]))
    add_stat("dreb", "Defensive Rebounds", totals["dreb"], str(totals["dreb"]))
    add_stat("reb", "Rebounds", reb, str(reb))
    add_stat("ast", "Assists", totals["ast"], str(totals["ast"]))
    add_stat("to", "Turnovers", totals["to"], str(totals["to"]))
    add_stat("stl", "Steals", totals["stl"], str(totals["stl"]))
    add_stat("blk", "Blocks", totals["blk"], str(totals["blk"]))
    add_stat("pf", "Personal Fouls", totals["pf"], str(totals["pf"]))
    return out


def _live_player_rows(team_id: str, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build player-level stat rows from PBP for one team. Columns match season player table."""
    tid = normalize_team_id(team_id)
    team_plays = [r for r in rows if _normalize_team_id_safe(r.get("team_id")) == tid]
    stats_by_athlete = _compute_live_player_stats(team_plays)
    all_athletes: Set[str] = set(stats_by_athlete)
    for r in team_plays:
        aid = str(r.get("athlete_id") or "").strip()
        if aid:
            all_athletes.add(aid)
        assist_id = str(r.get("assist_athlete_id") or "").strip()
        if assist_id:
            all_athletes.add(assist_id)
    seen: Dict[str, int] = {}
    out: List[Dict[str, Any]] = []
    totals = {
        "pts": 0,
        "reb": 0,
        "ast": 0,
        "to": 0,
        "stl": 0,
        "blk": 0,
        "pf": 0,
        "fgm": 0,
        "fga": 0,
        "fg3m": 0,
        "fg3a": 0,
        "ftm": 0,
        "fta": 0,
        "midr_m": 0,
        "midr_a": 0,
        "layup_m": 0,
        "layup_a": 0,
        "dunks": 0,
        "tips": 0,
    }
    for aid in sorted(all_athletes):
        stats = stats_by_athlete.get(aid, {})
        pts = int(stats.get("pts", 0))
        ast = int(stats.get("ast", 0))
        reb = int(stats.get("oreb", 0)) + int(stats.get("dreb", 0))
        stl = int(stats.get("stl", 0))
        blk = int(stats.get("blk", 0))
        to = int(stats.get("to", 0))
        pf = int(stats.get("pf", 0))
        fgm = int(stats.get("fgm", 0))
        fga = int(stats.get("fga", 0))
        fg3m = int(stats.get("3pm", 0))
        fg3a = int(stats.get("3pa", 0))
        ftm = int(stats.get("ftm", 0))
        fta = int(stats.get("fta", 0))
        midr_m = int(stats.get("midr_m", 0))
        midr_a = int(stats.get("midr_a", 0))
        layup_m = int(stats.get("layup_m", 0))
        layup_a = int(stats.get("layup_a", 0))
        dunks = int(stats.get("dunks", 0))
        tips = int(stats.get("tips", 0))
        totals["pts"] += pts
        totals["reb"] += reb
        totals["ast"] += ast
        totals["to"] += to
        totals["stl"] += stl
        totals["blk"] += blk
        totals["pf"] += pf
        totals["fgm"] += fgm
        totals["fga"] += fga
        totals["fg3m"] += fg3m
        totals["fg3a"] += fg3a
        totals["ftm"] += ftm
        totals["fta"] += fta
        totals["midr_m"] += midr_m
        totals["midr_a"] += midr_a
        totals["layup_m"] += layup_m
        totals["layup_a"] += layup_a
        totals["dunks"] += dunks
        totals["tips"] += tips
        rk = unique_row_key(f"live_{tid}_player_{aid}", seen)
        out.append(
            {
                "team_id": tid,
                **player_display_row(
                    row_key=rk,
                    player_name=resolve_athlete_name(aid),
                    games_played=1,
                    games_started=0,
                    points=pts,
                    rebounds=reb,
                    assists=ast,
                    turnovers=to,
                    steals=stl,
                    blocks=blk,
                    personal_fouls=pf,
                    ftm=ftm,
                    fta=fta,
                    fgm=fgm,
                    fga=fga,
                    fg3m=fg3m,
                    fg3a=fg3a,
                    mid_m=midr_m,
                    mid_a=midr_a,
                    layup_m=layup_m,
                    layup_a=layup_a,
                    dunks=dunks,
                    tips=tips,
                ),
            }
        )
    out.append(
        {
            "team_id": tid,
            **player_display_row(
                row_key=unique_row_key(f"live_{tid}_player_team", seen),
                player_name="Team",
                games_played=1,
                games_started=1,
                points=totals["pts"],
                rebounds=totals["reb"],
                assists=totals["ast"],
                turnovers=totals["to"],
                steals=totals["stl"],
                blocks=totals["blk"],
                personal_fouls=totals["pf"],
                ftm=totals["ftm"],
                fta=totals["fta"],
                fgm=totals["fgm"],
                fga=totals["fga"],
                fg3m=totals["fg3m"],
                fg3a=totals["fg3a"],
                mid_m=totals["midr_m"],
                mid_a=totals["midr_a"],
                layup_m=totals["layup_m"],
                layup_a=totals["layup_a"],
                dunks=totals["dunks"],
                tips=totals["tips"],
            ),
        }
    )
    return out


def build_live_stats_from_pbp(
    ucsb_team_id: Optional[str] = None,
    opponent_team_id: Optional[str] = None,
    game_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Build four datasets (ucsb_team, ucsb_players, opponent_team, opponent_players) from PBP only."""
    rows = load_pbp_rows(game_id=game_id)
    ucsb_id = _normalize_team_id_safe(ucsb_team_id or DEFAULT_UCSB_TEAM_ID) or normalize_team_id(DEFAULT_UCSB_TEAM_ID)
    team_ids_in_pbp = set()
    for r in rows:
        tid_raw = r.get("team_id")
        if not tid_raw:
            continue
        try:
            team_ids_in_pbp.add(normalize_team_id(str(tid_raw)))
        except ValueError:
            pass
    opp_id = _normalize_team_id_safe(opponent_team_id or "") or ""
    if opp_id and opp_id not in team_ids_in_pbp:
        opp_id = ""
    if not opp_id and len(team_ids_in_pbp) >= 2:
        other = team_ids_in_pbp - {ucsb_id}
        opp_id = other.pop() if other else (DEFAULT_OPPONENT_TEAM_ID if ucsb_id == DEFAULT_UCSB_TEAM_ID else "")

    def dataset(team_id: str, kind: str):
        tid = normalize_team_id(team_id)
        if kind == "team":
            rws = _live_team_rows(tid, rows)
            cols = list(LIVE_TEAM_COLUMNS)
        else:
            rws = _live_player_rows(tid, rows)
            cols = list(LIVE_PLAYER_COLUMNS)
        return {"columns": cols, "rows": rws}

    return {
        "ucsb_team": dataset(ucsb_id, "team"),
        "ucsb_players": dataset(ucsb_id, "players"),
        "opponent_team": dataset(opp_id, "team") if opp_id else {"columns": LIVE_TEAM_COLUMNS, "rows": []},
        "opponent_players": dataset(opp_id, "players") if opp_id else {"columns": LIVE_PLAYER_COLUMNS, "rows": []},
    }


def update_pbp_data(force: bool = False, game_id: Optional[str] = None) -> Dict[str, Any]:
    global _LAST_PBP_UPDATE_AT

    gid = (game_id or ESPN_PBP_GAME_ID).strip()
    if not force:
        elapsed = time.time() - _LAST_PBP_UPDATE_AT
        if elapsed < PBP_UPDATE_MIN_INTERVAL_SECONDS:
            raise RuntimeError(
                f"PBP update is rate-limited. Try again in {PBP_UPDATE_MIN_INTERVAL_SECONDS - elapsed:.1f}s."
            )

    summary = get_service().ingest_game(gid, force=force)
    get_service().derive_game_stats(gid)
    get_service().aggregate_season()
    _LAST_PBP_UPDATE_AT = time.time()

    return {
        "rows": summary.get("rows", 0),
        "columns": pbp_columns(load_pbp_rows(gid)),
        "source_url": summary.get("source_url") or espn_pbp_source_url(ESPN_PBP_LEAGUE, gid),
        "file": summary.get("archive_path", ""),
        "updated_at": summary.get("updated_at", now_iso()),
        "schema_version": PBP_SCHEMA_VERSION,
    }


def build_manifest(
    team_id: str,
    school_name: str,
    source_urls: Sequence[str],
    file_metadata: Sequence[Dict[str, Any]],
    season_year: int,
) -> Dict[str, Any]:
    return {
        "season": SEASON_LABEL,
        "season_year": season_year,
        "team_id": team_id,
        "school_name": school_name,
        "league": ESPN_LEAGUE,
        "source_urls": list(source_urls),
        "files": list(file_metadata),
        "last_updated": now_iso(),
        "schema_version": SCHEMA_VERSION,
    }


def _items_to_refs(items: Any) -> List[str]:
    refs: List[str] = []
    if not isinstance(items, list):
        return refs
    for item in items:
        if isinstance(item, dict):
            ref = item.get("$ref")
            if isinstance(ref, str) and ref:
                refs.append(ref)
    return refs


def _discover_season_ref(league_root: Dict[str, Any], target_year: int) -> str:
    seasons_ref = ((league_root.get("seasons") or {}).get("$ref") if isinstance(league_root, dict) else None) or ""
    if not seasons_ref:
        return f"{ESPN_LEAGUE_ROOT_URL}/seasons/{target_year}?lang=en&region=us"

    seasons_payload = fetch_json(seasons_ref)
    refs = _items_to_refs(seasons_payload.get("items"))
    for ref in refs:
        if f"/seasons/{target_year}" in ref:
            return ref
    if refs:
        return refs[-1]
    return f"{ESPN_LEAGUE_ROOT_URL}/seasons/{target_year}?lang=en&region=us"


def fetch_espn_teams(force_refresh: bool = False) -> Dict[str, Any]:
    service = get_service()
    service.verify_and_persist_schedule(force=False)
    return service.supported_teams_payload(force_refresh=force_refresh)


def _expand_ref_collection(ref_url: str, max_items: int = 500) -> List[Dict[str, Any]]:
    payload = fetch_json(ref_url)
    items = payload.get("items")
    if not isinstance(items, list):
        return [payload]

    expanded: List[Dict[str, Any]] = []
    for item in items[:max_items]:
        if isinstance(item, dict):
            ref = item.get("$ref")
            if isinstance(ref, str) and ref:
                try:
                    expanded.append(fetch_json(ref))
                except Exception:
                    continue
            else:
                expanded.append(item)
    return expanded


def _discover_team_ref(team_id: str, force_refresh_teams: bool = False) -> Tuple[str, str]:
    teams_payload = fetch_espn_teams(force_refresh=force_refresh_teams)
    for team in teams_payload.get("teams", []):
        if normalize_team_id(team.get("team_id", "")) != team_id:
            continue
        team_ref = to_text(team.get("team_ref"))
        if team_ref:
            return team_ref, to_text(team.get("school_name") or team_id)

    season_year = season_year_from_label(SEASON_LABEL)
    fallback_ref = f"{ESPN_LEAGUE_ROOT_URL}/seasons/{season_year}/teams/{team_id}?lang=en&region=us"
    return fallback_ref, team_id


def _discover_refs(payload: Dict[str, Any], token: str, team_id: str = "") -> List[str]:
    refs = collect_ref_urls(payload)
    out: List[str] = []
    for ref in refs:
        if token not in ref:
            continue
        if team_id and f"/{team_id}/" not in ref:
            continue
        out.append(ref)
    deduped: List[str] = []
    seen: Set[str] = set()
    for ref in out:
        if ref not in seen:
            deduped.append(ref)
            seen.add(ref)
    return deduped


def _extract_team_stat_rows(team_id: str, stats_payloads: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    seen_row_keys: Dict[str, int] = {}
    seen_stats: Set[Tuple[str, str]] = set()

    def visit(node: Any, split_hint: str = "overall") -> None:
        if isinstance(node, dict):
            local_split = to_text(node.get("displayName") or node.get("name") or split_hint) or "overall"
            stats = node.get("statistics")
            if isinstance(stats, list):
                for stat in stats:
                    if not isinstance(stat, dict):
                        continue
                    stat_key = normalize_column_name(
                        to_text(stat.get("name") or stat.get("abbreviation") or stat.get("displayName"))
                    )
                    if not stat_key:
                        continue
                    dedupe_key = (local_split, stat_key)
                    if dedupe_key in seen_stats:
                        continue
                    seen_stats.add(dedupe_key)
                    row = {
                        "row_key": unique_row_key(f"{team_id}_{local_split}_{stat_key}", seen_row_keys),
                        "team_id": team_id,
                        "split": local_split,
                        "stat_key": stat_key,
                        "stat_name": to_text(stat.get("displayName") or stat.get("name") or stat_key),
                        "value": to_text(stat.get("displayValue") if stat.get("displayValue") is not None else stat.get("value")),
                        "numeric_value": to_text(stat.get("value")),
                        "display_value": to_text(stat.get("displayValue")),
                        "rank": to_text(stat.get("rank")),
                        "abbreviation": to_text(stat.get("abbreviation")),
                    }
                    rows.append(row)

            for child_key, child_value in node.items():
                if child_key == "statistics":
                    continue
                visit(child_value, local_split if child_key in {"splits", "categories", "types"} else split_hint)
            return

        if isinstance(node, list):
            for item in node:
                visit(item, split_hint)

    for payload in stats_payloads:
        visit(payload, "overall")

    if rows:
        return rows

    # Fallback: emit scalar top-level keys as a small table if no structured statistics list exists.
    for payload in stats_payloads:
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            if isinstance(value, (dict, list)):
                continue
            stat_key = normalize_column_name(key)
            row = {
                "row_key": unique_row_key(f"{team_id}_overall_{stat_key}", seen_row_keys),
                "team_id": team_id,
                "split": "overall",
                "stat_key": stat_key,
                "stat_name": key,
                "value": to_text(value),
                "numeric_value": to_text(value if isinstance(value, (int, float)) else ""),
                "display_value": to_text(value),
                "rank": "",
                "abbreviation": "",
            }
            rows.append(row)

    return rows


def _extract_player_stat_map(stats_payloads: Sequence[Dict[str, Any]]) -> Dict[str, str]:
    stat_map: Dict[str, str] = {}

    def visit(node: Any, split_hint: str = "overall") -> None:
        if isinstance(node, dict):
            local_split = normalize_column_name(to_text(node.get("displayName") or node.get("name") or split_hint)) or "overall"
            stats = node.get("statistics")
            if isinstance(stats, list):
                for stat in stats:
                    if not isinstance(stat, dict):
                        continue
                    base_key = normalize_column_name(
                        to_text(stat.get("name") or stat.get("abbreviation") or stat.get("displayName"))
                    )
                    if not base_key:
                        continue
                    key = base_key if local_split in {"", "overall"} else f"{local_split}_{base_key}"
                    if key in stat_map:
                        continue
                    value = stat.get("displayValue") if stat.get("displayValue") is not None else stat.get("value")
                    stat_map[key] = to_text(value)

            for child_key, child_value in node.items():
                if child_key == "statistics":
                    continue
                visit(child_value, local_split if child_key in {"splits", "categories", "types"} else split_hint)
            return

        if isinstance(node, list):
            for item in node:
                visit(item, split_hint)

    for payload in stats_payloads:
        visit(payload, "overall")
    return stat_map


def scrape_team(team_id: str) -> Dict[str, Any]:
    team_id = normalize_team_id(team_id)
    data_dir = team_data_dir(team_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    season_year = season_year_from_label(SEASON_LABEL)
    source_urls: Set[str] = {ESPN_LEAGUE_ROOT_URL}

    team_ref, fallback_name = _discover_team_ref(team_id, force_refresh_teams=False)
    source_urls.add(team_ref)
    team_payload = fetch_json(team_ref)
    school_name = to_text(
        team_payload.get("displayName")
        or team_payload.get("shortDisplayName")
        or team_payload.get("name")
        or fallback_name
        or team_id
    )

    stats_refs: List[str] = []
    direct_stats_ref = to_text((team_payload.get("statistics") or {}).get("$ref"))
    if direct_stats_ref:
        stats_refs.append(direct_stats_ref)
    stats_refs.extend(_discover_refs(team_payload, "/statistics", team_id))

    deduped_stats_refs: List[str] = []
    for ref in stats_refs:
        if ref and ref not in deduped_stats_refs:
            deduped_stats_refs.append(ref)

    team_stats_payloads: List[Dict[str, Any]] = []
    for ref in deduped_stats_refs:
        source_urls.add(ref)
        try:
            team_stats_payloads.extend(_expand_ref_collection(ref, max_items=80))
        except Exception:
            continue
    if not team_stats_payloads:
        team_stats_payloads = [team_payload]

    team_rows = _extract_team_stat_rows(team_id, team_stats_payloads)
    if not team_rows:
        raise RuntimeError("Failed to extract team season stats from ESPN Core API")

    roster_refs: List[str] = []
    direct_athletes_ref = to_text((team_payload.get("athletes") or {}).get("$ref"))
    direct_roster_ref = to_text((team_payload.get("roster") or {}).get("$ref"))
    if direct_athletes_ref:
        roster_refs.append(direct_athletes_ref)
    if direct_roster_ref:
        roster_refs.append(direct_roster_ref)
    roster_refs.extend(_discover_refs(team_payload, "/athletes", team_id))
    roster_refs.extend(_discover_refs(team_payload, "/roster", team_id))

    roster_entries: List[Dict[str, Any]] = []
    roster_source_ref = ""
    for ref in roster_refs:
        try:
            entries = _expand_ref_collection(ref, max_items=40)
        except Exception:
            continue
        if entries:
            roster_entries = entries
            roster_source_ref = ref
            source_urls.add(ref)
            break

    player_rows: List[Dict[str, str]] = []
    for entry in roster_entries:
        if not isinstance(entry, dict):
            continue

        athlete_ref = ""
        athlete_payload: Dict[str, Any] = {}
        athlete_obj = entry.get("athlete")
        if isinstance(athlete_obj, dict):
            athlete_ref = to_text(athlete_obj.get("$ref"))
            if athlete_ref:
                try:
                    athlete_payload = fetch_json(athlete_ref)
                    source_urls.add(athlete_ref)
                except Exception:
                    athlete_payload = {}
            else:
                athlete_payload = athlete_obj
        elif "$ref" in entry:
            athlete_ref = to_text(entry.get("$ref"))
            if athlete_ref:
                try:
                    athlete_payload = fetch_json(athlete_ref)
                    source_urls.add(athlete_ref)
                except Exception:
                    athlete_payload = {}
        else:
            athlete_payload = entry

        athlete_id = to_text(athlete_payload.get("id") or _extract_ref_id(athlete_ref))
        if not athlete_id:
            continue

        stat_refs = []
        direct_stat_ref = to_text((entry.get("statistics") or {}).get("$ref"))
        if direct_stat_ref:
            stat_refs.append(direct_stat_ref)
        stat_refs.extend(_discover_refs(athlete_payload, "/statistics", athlete_id))
        stat_refs.extend(_discover_refs(entry, "/statistics", athlete_id))

        deduped_stat_refs: List[str] = []
        for ref in stat_refs:
            if ref and ref not in deduped_stat_refs:
                deduped_stat_refs.append(ref)

        athlete_stat_payloads: List[Dict[str, Any]] = []
        for ref in deduped_stat_refs:
            source_urls.add(ref)
            try:
                athlete_stat_payloads.extend(_expand_ref_collection(ref, max_items=20))
            except Exception:
                continue

        player_row: Dict[str, str] = {
            "row_key": f"athlete_{athlete_id}",
            "team_id": team_id,
            "athlete_id": athlete_id,
            "player": to_text(
                athlete_payload.get("displayName")
                or athlete_payload.get("shortName")
                or athlete_payload.get("fullName")
                or athlete_payload.get("name")
            ),
            "short_name": to_text(athlete_payload.get("shortName")),
            "jersey": to_text(athlete_payload.get("jersey")),
            "position": to_text((athlete_payload.get("position") or {}).get("abbreviation")),
        }
        player_row.update(_extract_player_stat_map(athlete_stat_payloads))
        player_rows.append(player_row)

    if not player_rows:
        raise RuntimeError(
            "Failed to extract player season stats from ESPN Core API. "
            f"Roster source ref: {roster_source_ref or 'unavailable'}"
        )

    file_entries: List[Dict[str, Any]] = []
    file_entries.append(write_csv(data_dir / "team.csv", team_rows))
    file_entries.append(write_csv(data_dir / "player.csv", player_rows))

    manifest = build_manifest(
        team_id=team_id,
        school_name=school_name,
        source_urls=sorted(source_urls),
        file_metadata=file_entries,
        season_year=season_year,
    )
    write_json_file(data_dir / "manifest.json", manifest)

    return {
        "team_id": team_id,
        "school_name": school_name,
        "season": SEASON_LABEL,
        "season_year": season_year,
        "source_url": ESPN_LEAGUE_ROOT_URL,
        "files": file_entries,
        "last_updated": manifest["last_updated"],
        "parse_meta": {
            "team_stat_rows": len(team_rows),
            "player_rows": len(player_rows),
        },
    }


def ensure_team_data(team_id: str) -> None:
    team_id = normalize_team_id(team_id)
    data_dir = team_data_dir(team_id)
    required = [data_dir / "team.csv", data_dir / "player.csv"]
    if all(path.exists() for path in required):
        return
    scrape_team(team_id)


def normalize_dataset_name(dataset: str) -> str:
    value = normalize_column_name(dataset)
    if value == "team":
        return "team"
    if value in {"player", "players"}:
        return "players"
    if value in {"pbp", "play_by_play", "playbyplay"}:
        return "pbp"
    raise ValueError("dataset must be one of: team, player, players, pbp")


def dataset_filename(dataset: str) -> str:
    canonical = normalize_dataset_name(dataset)
    mapping = {
        "team": "team.csv",
        "players": "player.csv",
        "pbp": f"pbp-{ESPN_PBP_GAME_ID}.json",
    }
    return mapping[canonical]


def dataset_needs_refresh(dataset: str, columns: Sequence[str], rows: Sequence[Dict[str, str]]) -> bool:
    if not columns or len(columns) <= 1:
        return True
    if len(rows) == 0:
        return True
    if dataset == "team":
        return "stat_key" not in columns
    if dataset == "players":
        return "player" not in columns
    if dataset == "pbp":
        return "text" not in columns or "clock" not in columns
    return True


def build_dataset_context_from_pdf(team_id: str, dataset: str) -> Optional[Dict[str, Any]]:
    """Build context from first page of team's season stats PDF. Returns None if no PDF for this team."""
    pdf_path = get_pdf_path_for_team(team_id)
    if not pdf_path:
        return None

    team_table, player_table = extract_first_page_tables(pdf_path)
    normalized_id = normalize_team_id(team_id)
    school_name = PDF_TEAM_NAMES.get(normalized_id, team_id)

    if dataset == "team":
        if not team_table:
            return {"team_id": normalized_id, "dataset": "team", "columns": [], "rows": [], "school_name": school_name}
        parsed = parse_team_table_rows(team_table)
        rows = pdf_team_rows_to_espn_format(parsed, normalized_id)
        default_team_columns = ["row_key", "team_id", "split", "stat_key", "stat_name", "value", "numeric_value", "display_value", "rank", "abbreviation"]
        columns = list(rows[0].keys()) if rows else default_team_columns
        return {
            "team_id": normalized_id,
            "dataset": "team",
            "columns": columns,
            "rows": rows,
            "rows_by_key": {r.get("row_key", ""): r for r in rows},
            "school_name": school_name,
        }

    if dataset == "players":
        if not player_table:
            return {"team_id": normalized_id, "dataset": "players", "columns": [], "rows": [], "school_name": school_name}
        rows = parse_player_table_rows(player_table)
        columns = list(rows[0].keys()) if rows else ["row_key", "player"]
        return {
            "team_id": normalized_id,
            "dataset": "players",
            "columns": columns,
            "rows": rows,
            "rows_by_key": {r.get("row_key", ""): r for r in rows},
            "school_name": school_name,
        }

    return None


def _player_table_config() -> Dict[str, str]:
    """Player table config: intended_name -> existing_name. Env PLAYER_TABLE_CONFIG=key:val,key2:val2 overrides."""
    raw = os.environ.get("PLAYER_TABLE_CONFIG", "").strip()
    if raw:
        out: Dict[str, str] = {}
        for part in raw.split(","):
            part = part.strip()
            if ":" in part:
                key, val = part.split(":", 1)
                out[key.strip()] = val.strip()
        return out
    return dict(PLAYER_TABLE_CONFIG)


def _parse_gp_from_gp_gs(gp_gs: str) -> Optional[int]:
    """Extract GP (games played) from GP-GS stat, e.g. '28-28' -> 28, '10-5' -> 10."""
    if not gp_gs or not isinstance(gp_gs, str):
        return None
    match = re.match(r"^(\d+)-", str(gp_gs).strip())
    return int(match.group(1)) if match else None


def _apply_player_column_config(context: Dict[str, Any]) -> None:
    """In-place: apply PLAYER_TABLE_CONFIG to player dataset (order + mapping; drop columns not in config)."""
    if context.get("dataset") != "players":
        return
    config = _player_table_config()
    if not config:
        return
    # Final column order: row_key first (for evidence), then config keys in order
    existing_columns = set(context.get("columns") or [])
    final_columns = ["row_key"] if "row_key" in existing_columns else []
    final_columns += list(config.keys())
    rows = context.get("rows") or []
    for row in rows:
        new_row: Dict[str, str] = {}
        if "row_key" in existing_columns:
            new_row["row_key"] = row.get("row_key", "")
        gp = _parse_gp_from_gp_gs(row.get("gp_gs", ""))
        reb_raw = row.get("tot", "") or row.get("REB", "")
        ast_raw = row.get("a", "") or row.get("AST", "")
        for intended_name, existing_name in config.items():
            if existing_name == _COMPUTED_RPG:
                try:
                    reb = float(str(reb_raw).replace(",", ""))
                    val = round(reb / gp, 1) if gp and gp > 0 else None
                    new_row[intended_name] = str(val) if val is not None else ""
                except (ValueError, TypeError):
                    new_row[intended_name] = ""
            elif existing_name == _COMPUTED_APG:
                try:
                    ast = float(str(ast_raw).replace(",", ""))
                    val = round(ast / gp, 1) if gp and gp > 0 else None
                    new_row[intended_name] = str(val) if val is not None else ""
                except (ValueError, TypeError):
                    new_row[intended_name] = ""
            else:
                new_row[intended_name] = row.get(existing_name, "")
        row.clear()
        row.update(new_row)
    context["columns"] = final_columns
    context["rows_by_key"] = {row.get("row_key", ""): row for row in rows}


def build_dataset_context(team_id: str, dataset: str) -> Dict[str, Any]:
    canonical_dataset = normalize_dataset_name(dataset)
    if canonical_dataset == "pbp":
        return build_pbp_context(team_id or "pbp")

    team_id = normalize_team_id(team_id)
    service = get_service()

    if canonical_dataset == "players":
        payload = service.player_dataset(team_id)
        return {
            "team_id": team_id,
            "dataset": canonical_dataset,
            "columns": payload["columns"],
            "rows": payload["rows"],
            "rows_by_key": {row.get("row_key", ""): row for row in payload["rows"]},
        }

    if canonical_dataset == "team":
        payload = service.team_dataset(team_id)
        return {
            "team_id": team_id,
            "dataset": canonical_dataset,
            "columns": payload["columns"],
            "rows": payload["rows"],
            "rows_by_key": {row.get("row_key", ""): row for row in payload["rows"]},
        }

    raise RuntimeError(f"Unsupported dataset: {canonical_dataset}")


def rows_to_csv_text(columns: Sequence[str], rows: Sequence[Dict[str, str]]) -> str:
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(columns))
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in columns})
    return buffer.getvalue().strip()


def validate_insights_payload(payload: Any, datasets: Sequence[Dict[str, Any]]) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    if not isinstance(payload, dict):
        return False, ["Payload must be an object"]

    extra_top_level = sorted(set(payload.keys()) - {"insights"})
    if extra_top_level:
        errors.append("Payload contains unknown top-level keys: " + ", ".join(extra_top_level))

    insights = payload.get("insights")
    if not isinstance(insights, list):
        return False, ["'insights' must be an array"]

    by_dataset: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for data in datasets:
        by_dataset[(data["team_id"], data["dataset"])] = data

    for insight_index, item in enumerate(insights):
        if not isinstance(item, dict):
            errors.append(f"insights[{insight_index}] must be an object")
            continue

        extra_insight_keys = sorted(set(item.keys()) - {"insight", "evidence"})
        if extra_insight_keys:
            errors.append(
                f"insights[{insight_index}] contains unknown keys: " + ", ".join(extra_insight_keys)
            )

        if not isinstance(item.get("insight"), str) or not item.get("insight", "").strip():
            errors.append(f"insights[{insight_index}].insight must be a non-empty string")

        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"insights[{insight_index}].evidence must be an array")
            continue

        for evidence_index, ref in enumerate(evidence):
            if not isinstance(ref, dict):
                errors.append(f"insights[{insight_index}].evidence[{evidence_index}] must be an object")
                continue

            extra_ref_keys = sorted(set(ref.keys()) - {"team_id", "dataset", "row_key", "fields"})
            if extra_ref_keys:
                errors.append(
                    f"insights[{insight_index}].evidence[{evidence_index}] contains unknown keys: "
                    + ", ".join(extra_ref_keys)
                )

            team_id = ref.get("team_id")
            dataset = ref.get("dataset")
            row_key = ref.get("row_key")
            fields = ref.get("fields")

            if not isinstance(team_id, str) or not team_id:
                errors.append(f"insights[{insight_index}].evidence[{evidence_index}].team_id must be non-empty string")
                continue
            if dataset not in {"team", "players", "pbp"}:
                errors.append(
                    f"insights[{insight_index}].evidence[{evidence_index}].dataset must be 'team', 'players', or 'pbp'"
                )
                continue
            if not isinstance(row_key, str) or not row_key:
                errors.append(f"insights[{insight_index}].evidence[{evidence_index}].row_key must be non-empty string")
                continue
            if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
                errors.append(f"insights[{insight_index}].evidence[{evidence_index}].fields must be string array")
                continue

            try:
                normalized_team_id = normalize_team_id(team_id)
            except ValueError:
                errors.append(f"insights[{insight_index}].evidence[{evidence_index}].team_id is invalid")
                continue

            dataset_context = by_dataset.get((normalized_team_id, dataset))
            if dataset_context is None:
                errors.append(
                    f"insights[{insight_index}].evidence[{evidence_index}] references unknown dataset ({team_id}/{dataset})"
                )
                continue

            row = dataset_context["rows_by_key"].get(row_key)
            if row is None:
                errors.append(
                    f"insights[{insight_index}].evidence[{evidence_index}] row_key '{row_key}' not found in {team_id}/{dataset}"
                )
                continue

            columns = set(dataset_context["columns"])
            for field in fields:
                if field not in columns:
                    errors.append(
                        f"insights[{insight_index}].evidence[{evidence_index}] unknown field '{field}' for {team_id}/{dataset}"
                    )

    return len(errors) == 0, errors


def allowed_evidence_pairs(datasets: Sequence[Dict[str, Any]]) -> List[Tuple[str, str]]:
    pairs: Set[Tuple[str, str]] = set()
    for data in datasets:
        team_id = data.get("team_id")
        dataset = data.get("dataset")
        if not isinstance(team_id, str) or not isinstance(dataset, str):
            continue
        try:
            normalized_team_id = normalize_team_id(team_id)
            normalized_dataset = normalize_dataset_name(dataset)
        except ValueError:
            continue
        pairs.add((normalized_team_id, normalized_dataset))
    return sorted(pairs)


def format_allowed_evidence_pairs(pairs: Sequence[Tuple[str, str]]) -> str:
    if not pairs:
        return "(none)"
    return "\n".join(f"- team_id={team_id}, dataset={dataset}" for team_id, dataset in pairs)


def canonicalize_insights_payload(payload: Any, datasets: Sequence[Dict[str, Any]]) -> Any:
    if not isinstance(payload, dict):
        return payload

    insights = payload.get("insights")
    if not isinstance(insights, list):
        return payload

    allowed_pairs = set(allowed_evidence_pairs(datasets))
    pbp_team_ids = sorted(team_id for team_id, dataset in allowed_pairs if dataset == "pbp")

    normalized_payload: Dict[str, Any] = dict(payload)
    normalized_insights: List[Any] = []
    for item in insights:
        if not isinstance(item, dict):
            normalized_insights.append(item)
            continue
        normalized_item: Dict[str, Any] = dict(item)
        evidence = normalized_item.get("evidence")
        if not isinstance(evidence, list):
            normalized_insights.append(normalized_item)
            continue

        normalized_evidence: List[Any] = []
        for ref in evidence:
            if not isinstance(ref, dict):
                normalized_evidence.append(ref)
                continue

            normalized_ref: Dict[str, Any] = dict(ref)
            team_id_raw = normalized_ref.get("team_id")
            dataset_raw = normalized_ref.get("dataset")
            if not isinstance(team_id_raw, str) or not isinstance(dataset_raw, str):
                normalized_evidence.append(normalized_ref)
                continue

            try:
                normalized_team_id = normalize_team_id(team_id_raw)
                normalized_dataset = normalize_dataset_name(dataset_raw)
            except ValueError:
                normalized_evidence.append(normalized_ref)
                continue

            if (normalized_team_id, normalized_dataset) not in allowed_pairs:
                # If exactly one PBP context is available, map any PBP team_id to that canonical context.
                if normalized_dataset == "pbp" and len(pbp_team_ids) == 1:
                    normalized_team_id = pbp_team_ids[0]

            normalized_ref["team_id"] = normalized_team_id
            normalized_ref["dataset"] = normalized_dataset
            normalized_evidence.append(normalized_ref)

        normalized_item["evidence"] = normalized_evidence
        normalized_insights.append(normalized_item)

    normalized_payload["insights"] = normalized_insights
    return normalized_payload


def openai_schema(allowed_pairs: Optional[Sequence[Tuple[str, str]]] = None) -> Dict[str, Any]:
    normalized_pairs = sorted(set(allowed_pairs or []))
    evidence_item_schema: Dict[str, Any]
    if normalized_pairs:
        evidence_item_schema = {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {
                        "team_id": {"type": "string", "const": team_id},
                        "dataset": {"type": "string", "const": dataset},
                        "row_key": {"type": "string"},
                        "fields": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["team_id", "dataset", "row_key", "fields"],
                    "additionalProperties": False,
                }
                for team_id, dataset in normalized_pairs
            ]
        }
    else:
        evidence_item_schema = {
            "type": "object",
            "properties": {
                "team_id": {"type": "string"},
                "dataset": {"type": "string", "enum": ["team", "players", "pbp"]},
                "row_key": {"type": "string"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["team_id", "dataset", "row_key", "fields"],
            "additionalProperties": False,
        }

    return {
        "name": "insights_schema",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "insights": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "insight": {"type": "string"},
                            "evidence": {
                                "type": "array",
                                "items": evidence_item_schema,
                            },
                        },
                        "required": ["insight", "evidence"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["insights"],
            "additionalProperties": False,
        },
    }


def _call_openai_chat(messages: List[Dict[str, str]], schema: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    model = os.environ.get("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)
    endpoint = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/") + "/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {
            "type": "json_schema",
            "json_schema": schema or openai_schema(),
        },
    }

    response = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
    )
    response.raise_for_status()
    raw = response.json()
    content = raw["choices"][0]["message"].get("content", "")
    if not isinstance(content, str):
        raise RuntimeError("OpenAI response did not include JSON text")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI response was not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("OpenAI response JSON must be an object")
    return parsed


def generate_insights(prompt: str, dataset_contexts: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    pbp_sections: List[str] = []
    extra_sections: List[str] = []
    for context in dataset_contexts:
        context_text = rows_to_csv_text(context["columns"], context["rows"])
        section = f"### Context: team_id={context['team_id']} dataset={context['dataset']}\n{context_text}"
        if context["dataset"] == "pbp":
            pbp_sections.append(section)
        else:
            extra_sections.append(section)

    system_prompt = LIVE_GAME_INSIGHT_RULES

    if not pbp_sections:
        raise RuntimeError("At least one PBP context is required.")

    allowed_pairs = allowed_evidence_pairs(dataset_contexts)
    schema = openai_schema(allowed_pairs=allowed_pairs)
    schema_text = json.dumps(schema["schema"], ensure_ascii=True, separators=(",", ":"))
    allowed_pairs_text = format_allowed_evidence_pairs(allowed_pairs)

    user_prompt = (
        f"Analyst prompt:\n{prompt}\n\n"
        "Task:\n"
        "- Identify the analyst's main request from the prompt and prioritize it.\n"
        "- Then produce insights that satisfy the mandatory live-game rules in system instructions.\n\n"
        "Output constraints:\n"
        "- Return JSON only with this exact schema:\n"
        + schema_text
        + "\n"
        "- Evidence dataset references must use only these allowed (team_id, dataset) pairs:\n"
        + allowed_pairs_text
        + "\n"
        "- If you cannot cite an allowed dataset reference, omit that evidence item. Never invent IDs.\n\n"
        "Primary context (full play-by-play; use this as the main basis of analysis):\n"
        + "\n\n".join(pbp_sections)
    )
    if extra_sections:
        user_prompt += "\n\nAdditional season contexts:\n" + "\n\n".join(extra_sections)

    max_attempts = 3
    candidate: Optional[Dict[str, Any]] = None
    errors: List[str] = ["Model was not called"]
    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
        else:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are repairing previously invalid JSON output. Return JSON only. "
                        "Preserve valid content when possible, but strictly follow the schema and allowed evidence pairs."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Validation errors:\n"
                        + "\n".join(errors)
                        + "\n\nAllowed evidence pairs:\n"
                        + allowed_pairs_text
                        + "\n\nRequired schema:\n"
                        + schema_text
                        + "\n\nPrevious JSON:\n"
                        + json.dumps(candidate or {}, ensure_ascii=True)
                    ),
                },
            ]
        candidate = _call_openai_chat(messages, schema=schema)
        candidate = canonicalize_insights_payload(candidate, dataset_contexts)
        valid, errors = validate_insights_payload(candidate, dataset_contexts)
        if valid:
            return candidate

    raise RuntimeError("Model output failed schema/evidence validation: " + "; ".join(errors))


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "AnalyticsAPI/0.3"

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        if path == "/api/health":
            self._send_json(200, {"ok": True, "timestamp": now_iso()})
            return

        if path == "/api/espn/teams":
            try:
                query = urlparse(self.path).query.lower()
                force = any(token in query for token in ("force=1", "refresh=1", "force=true", "refresh=true"))
                payload = fetch_espn_teams(force_refresh=force)
                self._send_json(200, payload)
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        match = re.match(r"^/api/season/([0-9-]+)/team/([a-zA-Z0-9-]+)/games$", path)
        if match:
            try:
                team_id = normalize_team_id(match.group(2))
                rows = get_service().list_schedule_games(team_id)
                self._send_json(200, {"season_id": match.group(1), "games": rows})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        match = re.match(r"^/api/season/([0-9-]+)/team/([a-zA-Z0-9-]+)/players$", path)
        if match:
            try:
                team_id = normalize_team_id(match.group(2))
                payload = get_service().player_dataset(team_id)
                visible_columns = [column for column in payload["columns"] if column != "row_key"]
                self._send_json(200, {"team_id": team_id, "columns": visible_columns, "rows": payload["rows"]})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        match = re.match(r"^/api/season/([0-9-]+)/team/([a-zA-Z0-9-]+)/team$", path)
        if match:
            try:
                team_id = normalize_team_id(match.group(2))
                payload = get_service().team_dataset(team_id)
                visible_columns = [column for column in payload["columns"] if column != "row_key"]
                self._send_json(200, {"team_id": team_id, "columns": visible_columns, "rows": payload["rows"]})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        match = re.match(r"^/api/build/jobs/([a-f0-9-]+)$", path)
        if match:
            try:
                self._send_json(200, get_service().get_job(match.group(1)))
            except Exception as exc:  # noqa: BLE001
                self._send_json(404, {"error": str(exc)})
            return

        match = re.match(r"^/api/espn/season/([a-zA-Z0-9-]+)/([a-zA-Z0-9_]+)$", path)
        if match:
            try:
                team_id = normalize_team_id(match.group(1))
                dataset_raw = match.group(2).lower()
                dataset = "players" if dataset_raw in {"player", "players"} else normalize_dataset_name(dataset_raw)
                if dataset == "pbp":
                    raise ValueError("Use /api/pbp for play-by-play dataset")

                context = build_dataset_context(team_id, dataset)
                school_name = context.get("school_name") or team_id
                hidden_columns = {"row_key"}
                visible_columns = [column for column in context["columns"] if column not in hidden_columns]
                self._send_json(
                    200,
                    {
                        "team_id": team_id,
                        "school_name": school_name,
                        "dataset": dataset,
                        "columns": visible_columns,
                        "rows": context["rows"],
                        "row_key": "row_key",
                        "updated_at": now_iso(),
                        "source_urls": [],
                        "schema_version": SCHEMA_VERSION,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/pbp":
            try:
                query = urlparse(self.path).query
                params = parse_qs(query, keep_blank_values=True)
                game_id = (params.get("game_id") or [ESPN_PBP_GAME_ID])[0] or ESPN_PBP_GAME_ID
                filters = parse_pbp_filters(params)
                context = build_pbp_context_filtered("pbp", game_id=game_id, filters=filters)
                hidden_columns = {"row_key"}
                visible_columns = [column for column in context["columns"] if column not in hidden_columns]
                self._send_json(
                    200,
                    {
                        "team_id": context["team_id"],
                        "dataset": "pbp",
                        "columns": visible_columns,
                        "rows": context["rows"],
                        "row_key": "row_key",
                        "updated_at": context["updated_at"],
                        "source_url": context["source_url"],
                        "schema_version": context["schema_version"],
                    },
                )
            except PbpFilterValidationError as exc:
                self._send_json(400, {"error": str(exc)})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/pbp/live-stats":
            try:
                query = urlparse(self.path).query
                params = {}
                for part in query.split("&"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        params[k.strip()] = v.strip()
                ucsb = params.get("ucsb", DEFAULT_UCSB_TEAM_ID)
                opponent = params.get("opponent", "")
                game_id = params.get("game_id") or ESPN_PBP_GAME_ID
                live = build_live_stats_from_pbp(ucsb_team_id=ucsb, opponent_team_id=opponent or None, game_id=game_id)
                hidden = {"row_key"}
                for key in ("ucsb_team", "ucsb_players", "opponent_team", "opponent_players"):
                    cols = live[key].get("columns", [])
                    live[key]["columns"] = [c for c in cols if c not in hidden]
                self._send_json(200, live)
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/schools":
            try:
                teams_payload = fetch_espn_teams(force_refresh=False)
                self._send_json(
                    200,
                    {
                        "season": teams_payload.get("season", SEASON_LABEL),
                        "season_year": teams_payload.get("season_year", season_year_from_label()),
                        "schools": teams_payload.get("teams", []),
                        "source": "espn-core-api-cache",
                        "last_updated": teams_payload.get("last_updated", now_iso()),
                        "warning": teams_payload.get("warning", ""),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        match = re.match(r"^/api/data/([a-zA-Z0-9-]+)/([a-zA-Z0-9_]+)$", path)
        if match:
            try:
                team_id = normalize_team_id(match.group(1))
                dataset = normalize_dataset_name(match.group(2).lower())

                context = build_dataset_context(team_id, dataset)
                hidden_columns = {"row_key"}
                visible_columns = [column for column in context["columns"] if column not in hidden_columns]

                self._send_json(
                    200,
                    {
                        "team_id": team_id,
                        "school_name": manifest.get("school_name", team_id),
                        "dataset": dataset,
                        "columns": visible_columns,
                        "rows": context["rows"],
                        "row_key": "row_key",
                        "updated_at": now_iso(),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path

        match = re.match(r"^/api/espn/season/([a-zA-Z0-9-]+)/update$", path)
        if match:
            try:
                body = self._read_json()
                force = bool(body.get("force", False))
                team_id = normalize_team_id(match.group(1))
                job = get_service().start_build("season", team_id, force=force)
                self._send_json(200, {"ok": True, "job": job})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/build/ucsb/2025-2026/regular/schedule":
            try:
                force = False
                query = urlparse(self.path).query.lower()
                if any(token in query for token in ("force=1", "force=true")):
                    force = True
                job = get_service().start_build("schedule", ROOT_TEAM_ID, force=force)
                self._send_json(200, {"ok": True, "job": job})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/build/ucsb/2025-2026/regular/season":
            try:
                query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                team_id = normalize_team_id((query.get("team_id") or [ROOT_TEAM_ID])[0])
                force = (query.get("force") or ["false"])[0].lower() in {"1", "true", "yes"}
                job = get_service().start_build("season", team_id, force=force)
                self._send_json(200, {"ok": True, "job": job})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/pbp/update":
            try:
                body = self._read_json()
                force = bool(body.get("force", False))
                # Use game_id from request so the selected Game ID in the UI determines the ESPN URL queried
                game_id = str(body.get("game_id") or ESPN_PBP_GAME_ID).strip() or ESPN_PBP_GAME_ID
                summary = update_pbp_data(force=force, game_id=game_id)
                self._send_json(200, {"ok": True, "summary": summary})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        match = re.match(r"^/api/scrape/([a-zA-Z0-9-]+)$", path)
        if match:
            try:
                team_id = normalize_team_id(match.group(1))
                job = get_service().start_build("season", team_id, force=False)
                self._send_json(200, {"ok": True, "job": job})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/evidence/validate":
            try:
                body = self._read_json()
                refs = body.get("refs", [])
                if not isinstance(refs, list):
                    raise ValueError("refs must be an array")

                grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
                results: List[Dict[str, Any]] = []

                for ref in refs:
                    if not isinstance(ref, dict):
                        results.append({"valid": False, "reason": "invalid reference object", "ref": ref})
                        continue

                    try:
                        team_id = normalize_team_id(str(ref.get("team_id", "")))
                    except ValueError:
                        results.append({"valid": False, "reason": "invalid team_id", "ref": ref})
                        continue

                    try:
                        dataset = normalize_dataset_name(str(ref.get("dataset", "")))
                    except ValueError:
                        results.append({"valid": False, "reason": "unknown dataset", "ref": ref})
                        continue

                    key = (team_id, dataset)
                    if key not in grouped:
                        grouped[key] = build_dataset_context(team_id, dataset)

                    row_key = str(ref.get("row_key", ""))
                    fields = ref.get("fields", [])
                    context = grouped[key]
                    row = context["rows_by_key"].get(row_key)
                    if row is None:
                        results.append({"valid": False, "reason": "row_key not found", "ref": ref})
                        continue
                    if not isinstance(fields, list) or any(field not in context["columns"] for field in fields):
                        results.append({"valid": False, "reason": "field mismatch", "ref": ref})
                        continue

                    results.append({"valid": True, "ref": ref})

                self._send_json(200, {"results": results})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/insights":
            try:
                body = self._read_json()
                prompt = str(body.get("prompt", "")).strip()
                contexts = body.get("contexts", [])
                if not prompt:
                    raise ValueError("prompt is required")
                if not isinstance(contexts, list):
                    raise ValueError("contexts must be an array")

                dataset_contexts: List[Dict[str, Any]] = []
                for context in contexts:
                    if not isinstance(context, dict):
                        continue
                    dataset = normalize_dataset_name(str(context.get("dataset", "")))
                    if dataset == "pbp":
                        team_id = str(context.get("team_id", "pbp")) or "pbp"
                        game_id = context.get("game_id") or ESPN_PBP_GAME_ID
                        dataset_contexts.append(build_pbp_context(team_id, game_id=game_id))
                    else:
                        team_id = normalize_team_id(str(context.get("team_id", "")))
                        dataset_contexts.append(build_dataset_context(team_id, dataset))

                if not dataset_contexts:
                    raise ValueError("At least one valid context is required")

                payload = generate_insights(prompt, dataset_contexts)
                self._send_json(200, {"ok": True, **payload})
            except requests.HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else 502
                detail = exc.response.text if exc.response is not None else str(exc)
                self._send_json(status, {"error": detail})
            except Exception as exc:  # noqa: BLE001
                self._send_json(500, {"error": str(exc)})
            return

        self._send_json(404, {"error": "Not found"})


def run_server() -> None:
    load_dotenv(REPO_ROOT / ".env")
    host = os.environ.get("API_HOST", "0.0.0.0")
    port = int(os.environ.get("API_PORT", "8001"))
    get_service()

    server = ThreadingHTTPServer((host, port), ApiHandler)
    print(f"Analytics API listening on http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
