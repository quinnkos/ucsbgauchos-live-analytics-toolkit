export const DEFAULT_GRAPH_METRIC_KEYS = ["PTS", "REB", "AST", "TO"];
export const TEAM_CUMULATIVE_METRIC_KEYS = [
  "PTS",
  "REB",
  "AST",
  "TO",
  "STL",
  "BLK",
  "PF"
];

export const GRAPH_LINE_COLORS = [
  "#116149",
  "#a55233",
  "#3b5ea7",
  "#8f2e21",
  "#5f7f2e",
  "#7b3fb0",
  "#1f7a8c",
  "#6b5d00"
];

export const SHOT_FAMILY_COLORS = {
  FG: "#116149",
  "3PT": "#a55233",
  LAYUP: "#3b5ea7",
  MIDRANGE: "#8f2e21",
  DUNK: "#5f7f2e"
};

export function formatElapsedGameTime(totalSeconds) {
  const safeSeconds = Math.max(0, Number(totalSeconds) || 0);
  const minutes = Math.floor(safeSeconds / 60);
  const seconds = Math.floor(safeSeconds % 60);
  return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

export function toggleGraphMetric(selectedKeys, key) {
  if (selectedKeys.includes(key)) {
    return selectedKeys.filter((entry) => entry !== key);
  }
  return [...selectedKeys, key];
}

export function normalizeSelectedGraphMetrics(selectedKeys, availableMetricKeys) {
  const available = new Set(availableMetricKeys || []);
  const filtered = (selectedKeys || []).filter((key) => available.has(key));
  if (filtered.length) {
    return filtered;
  }
  const defaults = DEFAULT_GRAPH_METRIC_KEYS.filter((key) => available.has(key));
  if (defaults.length) {
    return defaults;
  }
  return availableMetricKeys.slice(0, 1);
}

export function filterTeamCumulativeMetricOptions(metricOptions = []) {
  const allowed = new Set(TEAM_CUMULATIVE_METRIC_KEYS);
  return (metricOptions || []).filter((metric) => allowed.has(metric?.key));
}

export function graphTeamIdForSide(side, ucsbTeamId, opponentTeamId, teams = []) {
  if (side === "opponent") {
    if (opponentTeamId) {
      return opponentTeamId;
    }
    const fallbackOpponent = teams.find((team) => team.id && team.id !== ucsbTeamId);
    return fallbackOpponent?.id || "";
  }
  return ucsbTeamId;
}
