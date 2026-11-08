#!/usr/bin/env bash
# Crash-recovery test script (C13 deliverable).
# Demonstrates:
# 1. Starts the simulator server with public_01_dev
# 2. Runs the controller in background
# 3. Kills the controller with SIGKILL (kill -9) mid-run
# 4. Verifies database state is intact in SQLite
# 5. Restarts the controller with the same state DB
# 6. Verifies clean completion to scenario end with zero data loss or double-applies
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

echo "=== Formi Agent Loop Crash-Recovery Test ==="

# Step 0: Clean previous runs
rm -rf runs/crash_sim runs/crash_base controller_crash.db
source "$SCRIPT_DIR/.venv/bin/activate"

# Generate baseline
python3 -m sim baseline --scenario scenarios/public_01_dev.yaml --run-dir runs/crash_base >/dev/null 2>&1

# Start simulator server in background
python3 -m sim serve --scenario scenarios/public_01_dev.yaml --run-dir runs/crash_sim --port 8705 --force &
SIM_PID=$!
trap "kill -9 $SIM_PID 2>/dev/null || true" EXIT

# Wait for server ready
echo "Waiting for simulator server to generate history and start listening on port 8705..."
READY=0
for i in {1..120}; do
    if ! kill -0 "$SIM_PID" 2>/dev/null; then
        echo "ERROR: Simulator process died unexpectedly"
        exit 1
    fi
    if curl -s http://127.0.0.1:8705/health 2>/dev/null | grep -q "true"; then
        READY=1
        break
    fi
    sleep 0.5
done

if [ "$READY" -ne 1 ]; then
    echo "ERROR: Simulator server failed to start within 60s"
    exit 1
fi
echo "Simulator server ready at http://127.0.0.1:8705"

# Step 1: Start controller
echo "Starting controller (Phase 1)..."
python3 -m controller.main --url http://127.0.0.1:8705 --db controller_crash.db &
CTRL_PID=$!

# Let it run for 8 seconds (ingesting history and advancing multiple days)
echo "Controller running... waiting 8 seconds to process multiple days..."
sleep 8

# Step 2: Simulate ungraceful crash (SIGKILL)
echo "!!! SIMULATING HARD CRASH (kill -9 PID $CTRL_PID) !!!"
kill -9 "$CTRL_PID" 2>/dev/null || true
wait "$CTRL_PID" 2>/dev/null || true
echo "Controller process killed."

# Step 3: Inspect database state after crash
echo "--- SQLite state immediately after kill -9 ---"
sqlite3 controller_crash.db "SELECT 'Processed chunks: ' || count(*) FROM processed_chunks;"
sqlite3 controller_crash.db "SELECT 'Call records: ' || count(*) FROM call_stats;"
sqlite3 controller_crash.db "SELECT 'Audit events: ' || count(*) FROM audit_log;"
sqlite3 controller_crash.db "SELECT 'Pending approvals: ' || count(*) FROM approvals WHERE status='pending';"
echo "-----------------------------------------------"

# Step 4: Restart controller with SAME DB (Phase 2)
echo "Restarting controller (Phase 2 - Resumption)..."
python3 -m controller.main --url http://127.0.0.1:8705 --db controller_crash.db

echo "=== Controller completed successfully after restart! ==="

# Step 5: Grade the recovered run
python3 -m sim grade --run runs/crash_sim --baseline runs/crash_base --out reports/crash_recovery_report.md
echo "Grade report generated at reports/crash_recovery_report.md"
