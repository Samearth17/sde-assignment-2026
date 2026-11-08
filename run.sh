#!/usr/bin/env bash
# Controller entry point.
# Contract: ./run.sh <SIM_URL> runs until the scenario finishes, with no manual steps.
# Crash-safe: kill at any point, restart with the same SIM_URL, and it resumes.
set -euo pipefail

SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"

# Use venv if available, otherwise system python
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.venv/bin/activate" ]; then
    source "$SCRIPT_DIR/.venv/bin/activate"
fi

# Crash-safe DB — persists across restarts
DB_PATH="${SCRIPT_DIR}/controller_state.db"

echo "Starting controller against $SIM_URL (db: $DB_PATH)" >&2
exec python3 -m controller.main --url "$SIM_URL" --db "$DB_PATH"
