#!/usr/bin/env bash
# Runs both checks' bodies so one job reports each failure; either failure fails the job.
set -uo pipefail

if [ -z "${BASE:-}" ] || [ "$BASE" = 0000000000000000000000000000000000000000 ]; then
  echo "checks: this event has no base commit (a new branch or a manual run); nothing to compare"
  exit 0
fi

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
