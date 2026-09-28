#!/bin/sh
# GA-level perf gate. ~4 min, not run by run.sh.
#
# run.sh is the seconds-long every-commit contract on a single cold nest.
# This one drives the coordinator, which is the regime the time is actually spent
# in: 19 layouts re-evaluating candidates, with the NFP stage amortised over all
# of them. A single nest and a GA run are different enough that optimising one
# tells you nothing about the other -- measured, the NFP stage is 4.5% of the n70
# GA configuration but over half of a single cold nest.
#
#   tests/freecad_harness/run_ga.sh                            # gate
#   NEST_BENCH_REPS=1 tests/freecad_harness/run_ga.sh          # faster, noisier
#   GA_CORPUS=n70 tests/freecad_harness/run_ga.sh              # the real part
set -eu

HARNESS_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$HARNESS_DIR/../.." && pwd)
FREECADCMD=${FREECADCMD:-/home/james/freecad_env/usr/bin/freecadcmd}

if [ ! -x "$FREECADCMD" ]; then
    echo "freecadcmd not found at $FREECADCMD" >&2
    exit 2
fi

GA_CORPUS=${GA_CORPUS:-heavy}

if [ "$GA_CORPUS" = "heavy" ]; then
    # Committed baseline, so this runs in CI and catches regressions.
    NEST_BENCH_CORPUS=heavy
    NEST_BENCH_QUANTITIES=${NEST_BENCH_QUANTITIES:-HeavyPlate=2,Small0=30,Small1=30,Small2=30,SmallL=30}
    NEST_BENCH_SHEET=${NEST_BENCH_SHEET:-1200x600}
    NEST_BENCH_GA_GENERATIONS=${NEST_BENCH_GA_GENERATIONS:-3}
    NEST_BENCH_GA_POPULATION=${NEST_BENCH_GA_POPULATION:-2}
    NEST_BENCH_GA_BASELINE=${NEST_BENCH_GA_BASELINE:-$HARNESS_DIR/baseline/ga_heavy_v1.json}
else
    # The real customer part, and the configuration that is actually run. The
    # corpus is gitignored, so its baseline cannot be committed -- the same
    # situation as tier 3, and the same reason this stays a separate runner
    # rather than a flag on run_ga.sh.
    N70=${N70:-tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd}
    if [ ! -f "$N70" ]; then
        echo "n70 corpus not found at $N70" >&2
        echo "set N70 to the .FCStd path, or use GA_CORPUS=heavy" >&2
        exit 2
    fi
    NEST_BENCH_CORPUS=$N70
    NEST_BENCH_QUANTITIES=${NEST_BENCH_QUANTITIES:-Spacer=2,Bottle Top=60,Bottle Bottom=60}
    NEST_BENCH_SHEET=${NEST_BENCH_SHEET:-1200x600}
    NEST_BENCH_GA_GENERATIONS=${NEST_BENCH_GA_GENERATIONS:-8}
    NEST_BENCH_GA_POPULATION=${NEST_BENCH_GA_POPULATION:-4}
    if [ -z "${NEST_BENCH_GA_BASELINE:-}" ]; then
        echo "note: n70's baseline cannot be committed, so none is set." >&2
        echo "      this run records nothing and gates nothing; set" >&2
        echo "      NEST_BENCH_GA_BASELINE to compare against a local one." >&2
    fi
fi

# Every one of these must be *exported*. Setting a shell variable without
# exporting it leaves it in this script only, and bench_ga.py -- a different
# process -- silently falls back to its own defaults. That is how the first
# version of this runner recorded both GA baselines at generations 2,
# population 2 while appearing to ask for 8 and 4: three layouts, six parts
# placed, and no error anywhere.
export NEST_BENCH_CORPUS NEST_BENCH_QUANTITIES NEST_BENCH_SHEET
export NEST_BENCH_GA_BASELINE
export NEST_BENCH_GA_GENERATIONS NEST_BENCH_GA_POPULATION
export NEST_BENCH_ROTATION_STEPS=${NEST_BENCH_ROTATION_STEPS:-8}
export NEST_BENCH_GA_REPS=${NEST_BENCH_GA_REPS:-1}
export NEST_BENCH_REPS=${NEST_BENCH_REPS:-1}
export NEST_BENCH_ROTATION_WORKERS=${NEST_BENCH_ROTATION_WORKERS:-1}
export NEST_BENCH_GA_OUT=${NEST_BENCH_GA_OUT:-}

# Fail loudly rather than recording a baseline for a configuration nobody asked
# for. bench_ga.py would simply use its defaults.
if [ -z "${NEST_BENCH_GA_GENERATIONS:-}" ] || [ -z "${NEST_BENCH_GA_POPULATION:-}" ]; then
    echo "internal error: generations/population did not reach this script" >&2
    exit 3
fi

cd "$REPO_ROOT"
"$FREECADCMD" "$HARNESS_DIR/bench_ga.py" || true

STATUS_FILE="$HARNESS_DIR/.last_status_bench_ga"
if [ ! -f "$STATUS_FILE" ]; then
    echo "bench_ga did not write $STATUS_FILE -- it probably did not run" >&2
    exit 3
fi

STATUS=$(cat "$STATUS_FILE")
case "$STATUS" in
    0) echo "ga gate: PASS" ;;
    1) echo "ga gate: GATE FAILED" >&2 ;;
    2) echo "ga gate: USAGE ERROR (config drift, or a missing baseline)" >&2 ;;
    *) echo "ga gate: ERROR (status $STATUS)" >&2 ;;
esac
exit "$STATUS"
