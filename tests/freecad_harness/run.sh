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
GA_STATUS="$HARNESS_DIR/.last_status_ga"
PANEL_STATUS="$HARNESS_DIR/.last_status_panel"
UNITS_STATUS="$HARNESS_DIR/.last_status_units"
PYTEST_STATUS="$HARNESS_DIR/.last_status_pytest"
REPLAY_STATUS="$HARNESS_DIR/.last_status_replay"
DRESSUP_STATUS="$HARNESS_DIR/.last_status_dressup"
rm -f "$BENCH_STATUS" "$TEST_STATUS" "$GA_STATUS" "$PANEL_STATUS" \
      "$UNITS_STATUS" "$PYTEST_STATUS" "$REPLAY_STATUS" "$DRESSUP_STATUS"

cd "$REPO_ROOT"
"$FREECADCMD" "$HARNESS_DIR/nest_benchmark.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_master_promotion.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_ga_loop.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_panel_teardown.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_document_units.py" || true
# The two replay checks. These need real FreeCAD objects -- analytic surfaces,
# actual dressup proxies, an actual Operations list -- because what they guard
# is precisely the things a stand-in cannot show: that a dressed operation is
# absent from the list it is dressed up in, and that the post-processor emits
# that list verbatim. Neither is expressible in pytest.
"$FREECADCMD" "$HARNESS_DIR/test_replay_flatten.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_replay_dressups.py" || true

for f in "$BENCH_STATUS" "$TEST_STATUS" "$GA_STATUS" "$PANEL_STATUS" \
         "$UNITS_STATUS" "$REPLAY_STATUS" "$DRESSUP_STATUS"; do
    if [ ! -f "$f" ]; then
        echo "harness did not write $f -- it probably did not run" >&2
        exit 3
    fi
done

STATUS=$(cat "$BENCH_STATUS")
TESTS=$(cat "$TEST_STATUS")

# The harness's own statistics -- the code that decides whether a run is
# comparable -- live in harness_common.py and are pure Python, so they belong in
# the fast suite rather than only behind a freecadcmd run. Runs the whole tests/
# tree so one command is the whole contract.
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

GA=$(cat "$GA_STATUS")
if [ "$GA" -ne 0 ]; then
    echo "GA-loop integration test FAILED (status $GA)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

PANEL=$(cat "$PANEL_STATUS")
if [ "$PANEL" -ne 0 ]; then
    echo "panel-teardown regression test FAILED (status $PANEL)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

UNITS=$(cat "$UNITS_STATUS")
if [ "$UNITS" -ne 0 ]; then
    echo "document-unit regression test FAILED (status $UNITS)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

REPLAY=$(cat "$REPLAY_STATUS")
if [ "$REPLAY" -ne 0 ]; then
    echo "replay-flatten regression test FAILED (status $REPLAY)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

DRESSUP=$(cat "$DRESSUP_STATUS")
if [ "$DRESSUP" -ne 0 ]; then
    echo "replay-dressup regression test FAILED (status $DRESSUP)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

PYTEST_RC=$(cat "$PYTEST_STATUS")
if [ "$PYTEST_RC" -ne 0 ]; then
    echo "pytest suite FAILED (status $PYTEST_RC)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

exit "$STATUS"
