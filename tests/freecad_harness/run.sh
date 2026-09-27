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
#
# FREECADCMD overrides the interpreter if freecadcmd is not where this expects.
set -eu

HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$HARNESS_DIR/../.." && pwd)
FREECADCMD=${FREECADCMD:-/home/james/freecad_env/usr/bin/freecadcmd}

if [ ! -x "$FREECADCMD" ]; then
    echo "freecadcmd not found at $FREECADCMD" >&2
    echo "set FREECADCMD to your FreeCAD command-line interpreter" >&2
    exit 2
fi

# Default to gating against the committed baseline so a bare invocation is the
# useful one rather than a bare print.
: "${NEST_BENCH_BASELINE:=$HARNESS_DIR/baseline/synthetic_v1.json}"
export NEST_BENCH_BASELINE

STATUS_FILE="$HARNESS_DIR/.last_status"
rm -f "$STATUS_FILE"

cd "$REPO_ROOT"
"$FREECADCMD" "$HARNESS_DIR/nest_benchmark.py" || true

if [ ! -f "$STATUS_FILE" ]; then
    echo "harness did not write $STATUS_FILE -- it probably did not run" >&2
    exit 3
fi

STATUS=$(cat "$STATUS_FILE")
case "$STATUS" in
    0) echo "harness: PASS" ;;
    1) echo "harness: GATE FAILED" >&2 ;;
    2) echo "harness: USAGE ERROR" >&2 ;;
    *) echo "harness: ERROR (status $STATUS)" >&2 ;;
esac
exit "$STATUS"
