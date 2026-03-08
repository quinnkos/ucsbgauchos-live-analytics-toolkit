import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  DEFAULT_GRAPH_METRIC_KEYS,
  TEAM_CUMULATIVE_METRIC_KEYS,
  filterTeamCumulativeMetricOptions,
  graphTeamIdForSide,
  normalizeSelectedGraphMetrics
} from "../src/gameGraphs.js";

const appSource = readFileSync(resolve("apps/web/src/App.jsx"), "utf8");
const stylesSource = readFileSync(resolve("apps/web/src/styles.css"), "utf8");

{
  assert.equal(appSource.includes("Graphs"), true, "Game Data should expose a Graphs subtab");
  assert.equal(appSource.includes('gameDataSubtab === "graphs"'), true, "App should render a Graphs branch");
  assert.equal(appSource.includes("/api/pbp/graphs"), true, "Graphs should load from the dedicated API endpoint");
}

{
  const activeLiveSideUses = (appSource.match(/activeLiveSide === "ucsb"/g) || []).length;
  const setActiveLiveSideUses = (appSource.match(/setActiveLiveSide\("opponent"\)/g) || []).length;
  assert.ok(activeLiveSideUses >= 2, "Graphs and Live Stats should both read the shared team side state");
  assert.ok(setActiveLiveSideUses >= 2, "Graphs and Live Stats should both write the shared team side state");
  assert.equal(appSource.includes("Shot Chart"), false, "Graphs subtab should no longer render a shot chart");
  assert.equal(appSource.includes("graphShowAttempts"), true, "Shot family attempts toggle should exist");
  assert.equal(appSource.includes("filterTeamCumulativeMetricOptions"), true, "Team cumulative chart should filter its metric options");
  assert.equal(stylesSource.includes(".graphs-layout {\n  display: flex;\n  flex-direction: column;"), true, "Graphs layout should stack cards vertically to eliminate overlap");
}

{
  assert.deepEqual(
    normalizeSelectedGraphMetrics(["PTS", "AST", "BAD"], ["PTS", "REB", "AST"]),
    ["PTS", "AST"]
  );
  assert.deepEqual(
    normalizeSelectedGraphMetrics([], ["PTS", "REB", "AST", "TO"]),
    DEFAULT_GRAPH_METRIC_KEYS
  );
  assert.deepEqual(
    filterTeamCumulativeMetricOptions([
      { key: "PTS", label: "Points" },
      { key: "FGM", label: "FG Made" },
      { key: "PF", label: "Personal Fouls" },
      { key: "LAYUP_A", label: "Layup Attempted" }
    ]).map((metric) => metric.key),
    ["PTS", "PF"]
  );
  assert.equal(TEAM_CUMULATIVE_METRIC_KEYS.includes("MIDR_M"), false);
  assert.equal(TEAM_CUMULATIVE_METRIC_KEYS.includes("LAYUP_A"), false);
  assert.equal(TEAM_CUMULATIVE_METRIC_KEYS.includes("FGM"), false);
  assert.equal(TEAM_CUMULATIVE_METRIC_KEYS.includes("FGA"), false);
}

{
  assert.equal(graphTeamIdForSide("ucsb", "2540", "300", []), "2540");
  assert.equal(graphTeamIdForSide("opponent", "2540", "300", []), "300");
  assert.equal(
    graphTeamIdForSide("opponent", "2540", "", [{ id: "2540" }, { id: "22" }]),
    "22"
  );
}

console.log("graphsSubtab tests passed");
