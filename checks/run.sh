#!/usr/bin/env bash
# The checks action's body: the file-size and complexity actions' bodies in one job, so a repository
# pays one rounded-up runner minute per push for both. Each check runs even when the other fails, and
# each failure gets its own annotation; the job fails when either check did.
set -uo pipefail

actions="$(cd "$(dirname "$0")/.." && pwd)"
failed=""
for check in file-size complexity; do
  echo "── $check"
  if bash "$actions/$check/run.sh"; then
    echo "$check: passed"
  else
    echo "::error title=$check::$check failed; its output is above"
    failed="$failed $check"
  fi
done

if [ -n "$failed" ]; then
  echo "checks: failed:$failed"
  exit 1
fi
