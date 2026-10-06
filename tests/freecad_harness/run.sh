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
ORDER_STATUS="$HARNESS_DIR/.last_status_order"
STARTPOINT_STATUS="$HARNESS_DIR/.last_status_startpoint"
BOUNDARY_STATUS="$HARNESS_DIR/.last_status_boundary"
RIGID_STATUS="$HARNESS_DIR/.last_status_rigid"
SUBNAME_STATUS="$HARNESS_DIR/.last_status_subnames"
CLEARANCE_STATUS="$HARNESS_DIR/.last_status_clearance"
PERSIST_STATUS="$HARNESS_DIR/.last_status_persist"
rm -f "$BENCH_STATUS" "$TEST_STATUS" "$GA_STATUS" "$PANEL_STATUS" \
      "$UNITS_STATUS" "$PYTEST_STATUS" "$REPLAY_STATUS" "$DRESSUP_STATUS" \
      "$ORDER_STATUS" "$STARTPOINT_STATUS" "$BOUNDARY_STATUS" \
      "$RIGID_STATUS" "$SUBNAME_STATUS" "$CLEARANCE_STATUS" "$PERSIST_STATUS"

cd "$REPO_ROOT"
"$FREECADCMD" "$HARNESS_DIR/nest_benchmark.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_master_promotion.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_ga_loop.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_panel_teardown.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_document_units.py" || true
# The three replay checks. These need real FreeCAD objects -- analytic surfaces,
# actual dressup proxies, an actual Operations list -- because what they guard
# is precisely the things a stand-in cannot show: that a dressed operation is
# absent from the list it is dressed up in, and that the post-processor emits
# that list verbatim. Neither is expressible in pytest.
#
# The order check is the odd one out: the other two build their own geometry and
# neither nests a part in a hole, so the ordering step returned before its write
# in every gated run. It is the only one that runs the committed fixture, which
# is the only one with nesting in it -- which is exactly why a bug in the
# ordering write sat behind a green gate (NEST-015).
"$FREECADCMD" "$HARNESS_DIR/test_replay_flatten.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_replay_dressups.py" || true
"$FREECADCMD" "$HARNESS_DIR/test_replay_order.py" || true
# A characterisation test, and the only gated file whose subject is currently
# wrong: it asserts that the replay copies a Profile's StartPoint verbatim onto
# the nested copy, measured, so that an unintended change fails here. It is in
# the gate for the same reason `test_replay_dressups.py` is -- the warning
# asserted there is the mitigation, and here there is not even that. Flipping
# START_POINT_IS_COPIED_VERBATIM in that file is the whole of the change when
# the replay is fixed.
"$FREECADCMD" "$HARNESS_DIR/test_replay_startpoint.py" || true
# Boundary is unsupported: dropped from the replay and reported once per source
# dressup. The write half of layout persistence is below.
"$FREECADCMD" "$HARNESS_DIR/test_replay_boundary.py" || true
# The nester centres a part with a rigid transform, and must not re-fit the
# geometry doing it: transformShape, not transformGeometry. NEST-007.
"$FREECADCMD" "$HARNESS_DIR/test_shape_preparer_rigid.py" || true
# A sub-element name that resolves is not the same as a name that means the same
# thing: getElement raises only when a name is absent, so `Face3` on a copy with
# fewer faces than the source answered fine and profiled the wrong feature.
# Compared against the source geometry; NEST-027.
"$FREECADCMD" "$HARNESS_DIR/test_replay_subnames.py" || true
# `spacing` and the CAM tool are independent controls and nothing related them:
# a nest tighter than the tool is wide would be cut through without a word.
"$FREECADCMD" "$HARNESS_DIR/test_tool_clearance.py" || true
# The write half of layout persistence: a layout records the algorithm that ran,
# the direction that algorithm's dial held, whether that direction was used, and
# each part's raw rotation override. The reload half needs a panel and lives in
# `probe_layout_restore.py`, run by hand on the `freecad` binary.
"$FREECADCMD" "$HARNESS_DIR/test_layout_persistence.py" || true

for f in "$BENCH_STATUS" "$TEST_STATUS" "$GA_STATUS" "$PANEL_STATUS" \
         "$UNITS_STATUS" "$REPLAY_STATUS" "$DRESSUP_STATUS" "$ORDER_STATUS" \
         "$STARTPOINT_STATUS" "$BOUNDARY_STATUS" "$RIGID_STATUS" \
         "$SUBNAME_STATUS" "$CLEARANCE_STATUS" "$PERSIST_STATUS"; do
    if [ ! -f "$f" ]; then
        echo "harness: DID NOT RUN -- did not write $f" >&2
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

# Every check below folds into $STATUS, and the verdict is printed last, once
# $STATUS is final. It used to be printed first, which meant a red gate read
#
#     harness: PASS
#     replay-flatten regression test FAILED (status 1)
#
# -- a verdict of PASS immediately above the line explaining it. The exit code
# was always correct, so this never failed a build; it just made a failing run
# read like a passing one to anyone skimming, which is the exact moment a gate
# is worth having.
#
# The perf gate is a separate concern from the regression tests: both are
# reported, and the run fails if either did.
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

ORDER=$(cat "$ORDER_STATUS")
if [ "$ORDER" -ne 0 ]; then
    echo "replay-order regression test FAILED (status $ORDER)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

STARTPOINT=$(cat "$STARTPOINT_STATUS")
if [ "$STARTPOINT" -ne 0 ]; then
    echo "replay-start-point regression test FAILED (status $STARTPOINT)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

BOUNDARY=$(cat "$BOUNDARY_STATUS")
if [ "$BOUNDARY" -ne 0 ]; then
    echo "replay-boundary regression test FAILED (status $BOUNDARY)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

RIGID=$(cat "$RIGID_STATUS")
if [ "$RIGID" -ne 0 ]; then
    echo "shape-preparer-rigidity regression test FAILED (status $RIGID)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

SUBNAME=$(cat "$SUBNAME_STATUS")
if [ "$SUBNAME" -ne 0 ]; then
    echo "replay-subname-agreement regression test FAILED (status $SUBNAME)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

CLEARANCE=$(cat "$CLEARANCE_STATUS")
if [ "$CLEARANCE" -ne 0 ]; then
    echo "tool-clearance regression test FAILED (status $CLEARANCE)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

PERSIST=$(cat "$PERSIST_STATUS")
if [ "$PERSIST" -ne 0 ]; then
    echo "layout-persistence regression test FAILED (status $PERSIST)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

PYTEST_RC=$(cat "$PYTEST_STATUS")
if [ "$PYTEST_RC" -ne 0 ]; then
    echo "pytest suite FAILED (status $PYTEST_RC)" >&2
    [ "$STATUS" -eq 0 ] && STATUS=1
fi

case "$STATUS" in
    0) echo "harness: PASS" ;;
    1) echo "harness: GATE FAILED" >&2 ;;
    2) echo "harness: USAGE ERROR" >&2 ;;
    *) echo "harness: ERROR (status $STATUS)" >&2 ;;
esac

exit "$STATUS"
