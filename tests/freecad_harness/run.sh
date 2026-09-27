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

BENCH_STATUS="$HARNESS_DIR/.last_status"
TEST_STATUS="$HARNESS_DIR/.last_status_test"
rm -f "$BENCH_STATUS" "$TEST_STATUS"

cd "$REPO_ROOT"
"$FREECADCMD" "$HARNESS_DIR/nest_benchmark.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_master_promotion.py" || true

for f in "$BENCH_STATUS" "$TEST_STATUS"; do
    if [ ! -f "$f" ]; then
        echo "harness did not write $f -- it probably did not run" >&2
        exit 3
    fi
done

STATUS=$(cat "$BENCH_STATUS")
TESTS=$(cat "$TEST_STATUS")

case "$STATUS" in
    0) echo "harness: PASS" ;;
    1) echo "harness: GATE FAILED" >&2 ;;
    2) echo "harness: USAGE ERROR" >&2 ;;
    *) echo "harness: ERROR (status $STATUS)" >&2 ;;
esac

# The regression test is a separate concern from the perf gate: report both,
# and fail if either did.
if [ "$TESTS" -ne 0 ]; then
    echo "master-promotion regression test FAILED (status $TESTS)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

exit "$STATUS"
