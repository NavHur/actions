#!/usr/bin/env bash
# The complexity action's body: fetch fleet's measurer at FLEET_REF, install its pinned analyzers into a
# scratch HOME (a self-hosted runner's own ~/.local/bin stays untouched), and run the ratchet against BASE.
set -euo pipefail

if [ -z "${BASE:-}" ] || [ "$BASE" = 0000000000000000000000000000000000000000 ]; then
  echo "complexity: this event has no base commit (a new branch or a manual run); nothing to compare"
  exit 0
fi

scratch="${RUNNER_TEMP:-$(mktemp -d)}/complexity"
fleet="$scratch/fleet"
rm -rf "$scratch"
mkdir -p "$scratch/home"

git init -q "$fleet"
git -C "$fleet" fetch -q --depth 1 https://github.com/NavHur/fleet.git "$FLEET_REF"
git -C "$fleet" checkout -q FETCH_HEAD

HOME="$scratch/home" python3 "$fleet/lib/fleet/binaries.py" "$fleet/binaries.json" "$scratch/backup" --apply
export PBP_COMPLEXITY="$scratch/home/.local/bin/pbp-complexity"
export PBP_GODOT_GUARD="$scratch/home/.local/bin/godot-guard"

# The skill's own oxlint, for JS and TS files whose repository has none installed in this job.
skill="$fleet/skills/pedro-best-practices"
if git diff --name-only "$BASE" HEAD | grep -qE '\.(js|jsx|ts|tsx|mjs|cjs|mts|cts|svelte|vue|astro)$'; then
  (cd "$skill" && npx --yes pnpm@10 install --frozen-lockfile --silent)
fi

# host-check queues local validation on Pedro's hosts; a CI job is already scheduled by its runner.
HOST_CHECK_ACTIVE=1 python3 "$skill/scripts/measure_complexity.py" "$BASE" HEAD --check --over-only
