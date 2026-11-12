#!/bin/bash
set -euo pipefail

# Verifier for the thin review workspace: did the agent emit a valid cheat_review.json?
# This does NOT re-grade the original Seta task.

REWARD_PATH="${REWARD_PATH:-/logs/verifier/reward.txt}"
python3 /tests/validate_review.py \
  /evidence/out/cheat_review.json \
  /logs/agent/cheat_review.json
status=$?
if [ "$status" -eq 0 ]; then
  echo 1 > "$REWARD_PATH"
else
  echo 0 > "$REWARD_PATH"
fi
exit 0
