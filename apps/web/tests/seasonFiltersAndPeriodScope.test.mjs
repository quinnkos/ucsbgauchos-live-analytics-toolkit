import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const appSource = readFileSync(resolve("apps/web/src/App.jsx"), "utf8");

{
  assert.equal(appSource.includes("periodScope"), true, "periodScope state should exist");
  assert.equal(appSource.includes("setPeriodScope"), true, "setPeriodScope setter should exist");
}

{
  assert.equal(appSource.includes("seasonFilterMode"), true, "seasonFilterMode state should exist");
}

{
  const fullGameCount = (appSource.match(/Full Game/g) || []).length;
  assert.ok(fullGameCount >= 2, "Full Game toggle should appear in both Season Data and Live Stats");
  const firstHalfCount = (appSource.match(/1st Half/g) || []).length;
  assert.ok(firstHalfCount >= 2, "1st Half toggle should appear in both Season Data and Live Stats");
  const secondHalfCount = (appSource.match(/2nd Half/g) || []).length;
  assert.ok(secondHalfCount >= 2, "2nd Half toggle should appear in both Season Data and Live Stats");
}

{
  assert.equal(appSource.includes("period_scope="), true, "live stats should pass period_scope to API");
}

{
  assert.equal(appSource.includes("season-filter-row"), true, "season filter row CSS class should exist");
  assert.equal(appSource.includes("Vs Selected Opponent"), true, "Vs Selected Opponent option should exist");
  assert.equal(appSource.includes('value="conference"'), true, "Conference option should exist");
  assert.equal(appSource.includes("Non-Conf"), false, "Non-Conference option should not exist");
}

{
  assert.equal(
    appSource.includes('label="(i)"'),
    false,
    "Season Data info button should be removed"
  );
}

{
  assert.equal(
    appSource.includes("filteredSeasonPlayers"),
    true,
    "filtered season players state should be used"
  );
  assert.equal(
    appSource.includes("hasActiveFilters"),
    true,
    "hasActiveFilters check should exist"
  );
}

{
  assert.equal(
    appSource.includes("/player/filtered"),
    false,
    "season filters should use the existing player endpoint instead of a special filtered path"
  );
}

{
  assert.equal(
    appSource.includes("No matching games for the selected filters"),
    true,
    "Empty state message should exist for no matching games"
  );
}

console.log("seasonFiltersAndPeriodScope tests passed");
