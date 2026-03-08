#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

bash scripts/ensure-web.sh
node apps/web/tests/evidenceNavigation.test.mjs
node apps/web/tests/pbpAdvancedFilters.test.mjs
node apps/web/tests/seasonFiltersAndPeriodScope.test.mjs
