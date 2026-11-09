#!/usr/bin/env bash
# The file-size action's body, which the checks action also runs. file_size.py is a verbatim copy of
# NavHur/fleet's (its CI fails while they differ).
set -euo pipefail

if [ -z "${BASE:-}" ] || [ "$BASE" = 0000000000000000000000000000000000000000 ]; then
  echo "file-size: this event has no base commit (a new branch or a manual run); nothing to compare"
  exit 0
fi

python3 "$(dirname "$0")/file_size.py" check --base "$BASE"
