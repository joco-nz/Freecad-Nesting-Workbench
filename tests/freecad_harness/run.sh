#!/bin/sh
# Runs the Tier 2/3 nesting benchmark and reports the gate result.
#
# freecadcmd does not reliably propagate a script's exit code, so the harness
# writes tests/freecad_harness/.last_status; this wrapper reads that back and
# exits with it, so the usual shell/CI contract holds.
#
#   tests/freecad_harness/run.sh                              # gate vs committed baseline
#   NEST_BENCH_OUT=/tmp/b.json tests/freecad_harness/run.sh   # record
#   NEST_BENCH_BASELINE=... tests/freecad_harness/run.sh      # gate vs another baseline
#   tests/freecad_harness/run.sh --jobs 4                     # parallel (default 4)
#
# FREECADCMD overrides the interpreter if freecadcmd is not where this expects.
set -eu

# Parse --jobs N
JOBS=4
while [ $# -gt 0 ]; do
    case "$1" in
        --jobs)
            JOBS="$2"
            shift 2
            ;;
        --jobs=*)
            JOBS="${1#*=}"
            shift
            ;;
        *)
            echo "usage: $0 [--jobs N]" >&2
            exit 2
            ;;
    esac
done

# Validate jobs is a positive integer
case "$JOBS" in
    ''|*[!0-9]*) echo "error: --jobs requires a positive integer" >&2; exit 2 ;;
    *) [ "$JOBS" -ge 1 ] || { echo "error: --jobs must be >= 1" >&2; exit 2; } ;;
esac

HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$HARNESS_DIR/../.." && pwd)
FREECADCMD=${FREECADCMD:-/home/james/freecad_env/usr/bin/freecadcmd}

if [ ! -x "$FREECADCMD" ]; then
    echo "freecadcmd not found at $FREECADCMD" >&2
    echo "set FREECADCMD to your FreeCAD command-line interpreter" >&2
    exit 2
fi

# The GUI binary, for the two checks that need FreeCADGui:
# test_command_audit_lead_ins.py and test_panel_construction.py
GUI_FREECAD=${GUI_FREECAD:-/home/james/freecad_env/usr/bin/freecad}
if [ ! -x "$GUI_FREECAD" ]; then
    echo "freecad (GUI) not found at $GUI_FREECAD" >&2
    echo "set GUI_FREECAD to your FreeCAD GUI binary" >&2
    echo "it needs no display: FreeCAD 26.3 starts with a live GUI on none" >&2
    exit 2
fi

# Default to gating against the committed baseline so a bare invocation is the
# useful one rather than a bare print.
: "${NEST_BENCH_BASELINE:=$HARNESS_DIR/baseline/synthetic_v1.json}"
export NEST_BENCH_BASELINE

# Status files (one per suite + aggregates)
BENCH_STATUS="$HARNESS_DIR/.last_status"
TEST_STATUS="$HARNESS_DIR/.last_status_test"
GA_STATUS="$HARNESS_DIR/.last_status_ga"
PANEL_STATUS="$HARNESS_DIR/.last_status_panel"
UNITS_STATUS="$HARNESS_DIR/.last_status_units"
PYTEST_STATUS="$HARNESS_DIR/.last_status_pytest"
REPLAY_STATUS="$HARNESS_DIR/.last_status_replay"
DRESSUP_STATUS="$HARNESS_DIR/.last_status_dressup"
ORDER_STATUS="$HARNESS_DIR/.last_status_order"
STARTPOINT_STATUS="$HARNESS_DIR/.last_status_startpoint"
BOUNDARY_STATUS="$HARNESS_DIR/.last_status_boundary"
RIGID_STATUS="$HARNESS_DIR/.last_status_rigid"
SUBNAME_STATUS="$HARNESS_DIR/.last_status_subnames"
CLEARANCE_STATUS="$HARNESS_DIR/.last_status_clearance"
LEADIN_STATUS="$HARNESS_DIR/.last_status_leadin"
LEADINCMD_STATUS="$HARNESS_DIR/.last_status_leadincmd"
TOOLTIP_STATUS="$HARNESS_DIR/.last_status_tooltip"
PANELBUILD_STATUS="$HARNESS_DIR/.last_status_panelbuild"
ROTATIONDET_STATUS="$HARNESS_DIR/.last_status_rotationdet"
PERSIST_STATUS="$HARNESS_DIR/.last_status_persist"
VERIFYSTAGE_STATUS="$HARNESS_DIR/.last_status_verifystate"

# Clean previous status files
rm -f "$BENCH_STATUS" "$TEST_STATUS" "$GA_STATUS" "$PANEL_STATUS" \
      "$UNITS_STATUS" "$PYTEST_STATUS" "$REPLAY_STATUS" "$DRESSUP_STATUS" \
      "$LEADIN_STATUS" "$LEADINCMD_STATUS" \
      "$ORDER_STATUS" "$STARTPOINT_STATUS" "$BOUNDARY_STATUS" \
      "$RIGID_STATUS" "$SUBNAME_STATUS" "$CLEARANCE_STATUS" \
      "$TOOLTIP_STATUS" "$PANELBUILD_STATUS" "$PERSIST_STATUS" \
      "$ROTATIONDET_STATUS" "$VERIFYSTAGE_STATUS"

cd "$REPO_ROOT"

# The lead-in audit can farm its copy-only checks out to worker processes, but
# this script already runs $JOBS groups at once. Nesting the two puts more
# runnable processes on the box than it has cores, and this workload -- a full
# CAM replay, memory-hungry -- does not gain from that. So when the groups
# already use the machine, run every suite in one process; when they do not
# (`--jobs 1`), leave the default so the lead-in audit can use the cores itself.
if [ "$JOBS" -gt 1 ]; then
    export LEADIN_WORKERS=1
fi

# Build the test list: each entry is "interpreter|script|status_file|description"
# Interpreter: "cmd" for freecadcmd, "gui" for GUI_FREECAD
# The suites are independent - each writes its own status file.
# We split them into JOBS groups and run each group in background.

# Heavy suites first for better load balancing
set -- \
    "cmd|$HARNESS_DIR/test_lead_in_audit.py|$LEADIN_STATUS|lead-in audit" \
    "cmd|$HARNESS_DIR/test_rotation_determinism.py|$ROTATIONDET_STATUS|rotation determinism" \
    "cmd|$HARNESS_DIR/test_replay_order.py|$ORDER_STATUS|replay order" \
    "cmd|$HARNESS_DIR/test_replay_startpoint.py|$STARTPOINT_STATUS|replay startpoint" \
    "cmd|$HARNESS_DIR/test_tool_clearance.py|$CLEARANCE_STATUS|tool clearance" \
    "cmd|$HARNESS_DIR/nest_benchmark.py|$BENCH_STATUS|nest benchmark" \
    "cmd|$HARNESS_DIR/test_master_promotion.py|$TEST_STATUS|master promotion" \
    "cmd|$HARNESS_DIR/test_ga_loop.py|$GA_STATUS|GA loop" \
    "cmd|$HARNESS_DIR/test_panel_teardown.py|$PANEL_STATUS|panel teardown" \
    "cmd|$HARNESS_DIR/test_document_units.py|$UNITS_STATUS|document units" \
    "cmd|$HARNESS_DIR/test_replay_flatten.py|$REPLAY_STATUS|replay flatten" \
    "cmd|$HARNESS_DIR/test_replay_dressups.py|$DRESSUP_STATUS|replay dressups" \
    "cmd|$HARNESS_DIR/test_replay_boundary.py|$BOUNDARY_STATUS|replay boundary" \
    "cmd|$HARNESS_DIR/test_shape_preparer_rigid.py|$RIGID_STATUS|shape preparer rigid" \
    "cmd|$HARNESS_DIR/test_replay_subnames.py|$SUBNAME_STATUS|replay subnames" \
    "gui|$HARNESS_DIR/test_command_audit_lead_ins.py|$LEADINCMD_STATUS|lead-in command" \
    "cmd|$HARNESS_DIR/test_tooltip_assets.py|$TOOLTIP_STATUS|tooltip assets" \
    "gui|$HARNESS_DIR/test_panel_construction.py|$PANELBUILD_STATUS|panel construction" \
    "cmd|$HARNESS_DIR/test_layout_persistence.py|$PERSIST_STATUS|layout persistence" \
    "cmd|$HARNESS_DIR/test_verify_stage.py|$VERIFYSTAGE_STATUS|verify stage"

# Total tests
TOTAL=$#

# Distribute tests into JOBS groups (round-robin for simplicity)
# Each group gets its own temp script to run
GROUP_DIR=$(mktemp -d)
trap 'rm -rf "$GROUP_DIR"' EXIT INT TERM

i=0
while [ $# -gt 0 ]; do
    entry="$1"
    shift
    group=$((i % JOBS))
    echo "$entry" >> "$GROUP_DIR/group_$group"
    i=$((i + 1))
done

# Run each group in background
pids=""
for g in $(seq 0 $((JOBS - 1))); do
    if [ -f "$GROUP_DIR/group_$g" ]; then
        (
            while IFS='|' read -r interp script status_file desc; do
                if [ "$interp" = "gui" ]; then
                    runner="$GUI_FREECAD"
                else
                    runner="$FREECADCMD"
                fi
                "$runner" "$script" || true
            done < "$GROUP_DIR/group_$g"
        ) &
        pids="$pids $!"
    fi
done

# Wait for all groups
for pid in $pids; do
    wait "$pid"
done

# Pure-Python pytest suite (runs after all freecadcmd suites)
PYTHON=${PYTHON:-python3}
if "$PYTHON" -c 'import pytest' 2>/dev/null; then
    if "$PYTHON" -m pytest -q tests/ >/dev/null 2>&1; then
        echo 0 > "$PYTEST_STATUS"
    else
        echo 1 > "$PYTEST_STATUS"
        "$PYTHON" -m pytest -q tests/ || true
    fi
else
    echo 0 > "$PYTEST_STATUS"
    echo "note: pytest not available, skipped the pure-Python suite" >&2
fi

# Every check below folds into $STATUS, and the verdict is printed last, once
# $STATUS is final.
STATUS=0

# Check that all status files were written
for f in "$BENCH_STATUS" "$TEST_STATUS" "$GA_STATUS" "$PANEL_STATUS" \
         "$UNITS_STATUS" "$REPLAY_STATUS" "$DRESSUP_STATUS" "$ORDER_STATUS" \
         "$LEADIN_STATUS" "$LEADINCMD_STATUS" \
         "$STARTPOINT_STATUS" "$BOUNDARY_STATUS" "$RIGID_STATUS" \
         "$SUBNAME_STATUS" "$CLEARANCE_STATUS" "$TOOLTIP_STATUS" \
         "$PANELBUILD_STATUS" "$PERSIST_STATUS" "$ROTATIONDET_STATUS" \
         "$VERIFYSTAGE_STATUS" "$PYTEST_STATUS"; do
    if [ ! -f "$f" ]; then
        echo "harness: DID NOT RUN -- did not write $f" >&2
        exit 3
    fi
done

# Aggregate
if [ "$(cat "$TEST_STATUS")" -ne 0 ]; then
    echo "master-promotion regression test FAILED (status $(cat "$TEST_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$GA_STATUS")" -ne 0 ]; then
    echo "GA-loop integration test FAILED (status $(cat "$GA_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$PANEL_STATUS")" -ne 0 ]; then
    echo "panel-teardown regression test FAILED (status $(cat "$PANEL_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$UNITS_STATUS")" -ne 0 ]; then
    echo "document-unit regression test FAILED (status $(cat "$UNITS_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$REPLAY_STATUS")" -ne 0 ]; then
    echo "replay-flatten regression test FAILED (status $(cat "$REPLAY_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$DRESSUP_STATUS")" -ne 0 ]; then
    echo "replay-dressup regression test FAILED (status $(cat "$DRESSUP_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$LEADIN_STATUS")" -ne 0 ]; then
    echo "lead-in audit regression test FAILED (status $(cat "$LEADIN_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$LEADINCMD_STATUS")" -ne 0 ]; then
    echo "lead-in command regression test FAILED (status $(cat "$LEADINCMD_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$ORDER_STATUS")" -ne 0 ]; then
    echo "replay-order regression test FAILED (status $(cat "$ORDER_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$STARTPOINT_STATUS")" -ne 0 ]; then
    echo "replay-start-point regression test FAILED (status $(cat "$STARTPOINT_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$BOUNDARY_STATUS")" -ne 0 ]; then
    echo "replay-boundary regression test FAILED (status $(cat "$BOUNDARY_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$RIGID_STATUS")" -ne 0 ]; then
    echo "shape-preparer-rigidity regression test FAILED (status $(cat "$RIGID_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$SUBNAME_STATUS")" -ne 0 ]; then
    echo "replay-subname-agreement regression test FAILED (status $(cat "$SUBNAME_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$CLEARANCE_STATUS")" -ne 0 ]; then
    echo "tool-clearance regression test FAILED (status $(cat "$CLEARANCE_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$TOOLTIP_STATUS")" -ne 0 ]; then
    echo "tooltip-assets regression test FAILED (status $(cat "$TOOLTIP_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$PANELBUILD_STATUS")" -ne 0 ]; then
    echo "panel-construction regression test FAILED (status $(cat "$PANELBUILD_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$ROTATIONDET_STATUS")" -ne 0 ]; then
    echo "rotation-determinism regression test FAILED (status $(cat "$ROTATIONDET_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$PERSIST_STATUS")" -ne 0 ]; then
    echo "layout-persistence regression test FAILED (status $(cat "$PERSIST_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$VERIFYSTAGE_STATUS")" -ne 0 ]; then
    echo "verify-stage regression test FAILED (status $(cat "$VERIFYSTAGE_STATUS"))" >&2
    STATUS=1
fi

if [ "$(cat "$PYTEST_STATUS")" -ne 0 ]; then
    echo "pytest suite FAILED (status $(cat "$PYTEST_STATUS"))" >&2
    STATUS=1
fi

case "$STATUS" in
    0) echo "harness: PASS" ;;
    1) echo "harness: GATE FAILED" >&2 ;;
    2) echo "harness: USAGE ERROR" >&2 ;;
    *) echo "harness: ERROR (status $STATUS)" >&2 ;;
esac

exit "$STATUS"