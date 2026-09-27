#!/bin/sh
# Tier 2: the heavy-corpus perf gate. ~3.5 minutes, not run by run.sh.
#
# run.sh is the fast contract -- synthetic corpus, seconds, safe to run on every
# commit. This is the one that costs minutes and catches throughput and
# algorithmic regressions the light corpus is too small to show.
#
# Why a synthetic heavy corpus rather than the n70 file: n70 is a real customer
# part in a gitignored file, so its measurements are real but unreproducible for
# anyone else and CI cannot run them. This corpus matches n70's intensity
# without its geometry, and the baseline is committable.
#
#   tests/freecad_harness/run_heavy.sh                          # gate
#   NEST_BENCH_REPS=1 tests/freecad_harness/run_heavy.sh        # faster, noisier
#   HEAVY_CORPUS=tests/Test_Files/n70-...FCStd \
#     NEST_BENCH_QUANTITIES='Spacer=2,Bottle Top=60,Bottle Bottom=60' \
#     tests/freecad_harness/run_heavy.sh                        # the real part
set -eu

HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$HARNESS_DIR/../.." && pwd)
FREECADCMD=${FREECADCMD:-/home/james/freecad_env/usr/bin/freecadcmd}

if [ ! -x "$FREECADCMD" ]; then
    echo "freecadcmd not found at $FREECADCMD" >&2
    echo "set FREECADCMD to your FreeCAD command-line interpreter" >&2
    exit 2
fi

# The heavy synthetic corpus: one plate that decomposes into 234 convex pieces
# (n70's Spacer is 253), plus small parts sized to fit its 36 holes -- which is
# what stops decompose_if_needed pruning every one of those rings away and
# collapsing the plate back to a single piece. See harness_common.build_heavy_corpus.
HEAVY_CORPUS=${HEAVY_CORPUS:-heavy}
if [ "$HEAVY_CORPUS" = "heavy" ]; then
    NEST_BENCH_QUANTITIES=${NEST_BENCH_QUANTITIES:-HeavyPlate=2,Small0=30,Small1=30,Small2=30,SmallL=30}
    NEST_BENCH_SHEET=${NEST_BENCH_SHEET:-1200x600}
    NEST_BENCH_BASELINE=${NEST_BENCH_BASELINE:-$HARNESS_DIR/baseline/heavy_v1.json}
else
    # The real part, run locally against a baseline that cannot be committed.
    echo "note: corpus is $HEAVY_CORPUS, so the baseline is only as good as the path you point at" >&2
    : "${NEST_BENCH_BASELINE:=$HARNESS_DIR/baseline/n70_local.json}"
fi

export NEST_BENCH_CORPUS="$HEAVY_CORPUS"
export NEST_BENCH_QUANTITIES NEST_BENCH_SHEET NEST_BENCH_BASELINE
export NEST_BENCH_ROTATION_STEPS=${NEST_BENCH_ROTATION_STEPS:-8}
export NEST_BENCH_REPS=${NEST_BENCH_REPS:-3}
export NEST_BENCH_ROTATION_WORKERS=${NEST_BENCH_ROTATION_WORKERS:-1}

cd "$REPO_ROOT"
"$FREECADCMD" "$HARNESS_DIR/nest_benchmark.py" || true

STATUS_FILE="$HARNESS_DIR/.last_status"
if [ ! -f "$STATUS_FILE" ]; then
    echo "harness did not write $STATUS_FILE -- it probably did not run" >&2
    exit 3
fi

STATUS=$(cat "$STATUS_FILE")
case "$STATUS" in
    0) echo "heavy gate: PASS" ;;
    1) echo "heavy gate: GATE FAILED" >&2 ;;
    2) echo "heavy gate: USAGE ERROR (config drift, or a missing baseline)" >&2 ;;
    *) echo "heavy gate: ERROR (status $STATUS)" >&2 ;;
esac
exit "$STATUS"
