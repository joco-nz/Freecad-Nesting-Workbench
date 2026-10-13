#!/bin/bash
# Run nesting diagnostic with freecadcmd
# Usage: ./run_diagnose.sh [fixture_name]

set -e

REPO_ROOT="/home/james/dev/Freecad-Nesting-Workbench"
SCRIPT="$REPO_ROOT/tests/diagnose_nesting.py"
FIXTURE="${1:-replay-fixture.FCStd}"

# Check if freecadcmd is available
if ! command -v freecadcmd &> /dev/null; then
    echo "freecadcmd not found in PATH"
    echo "Try: export PATH=\$PATH:/usr/lib/freecad/bin  (or your FreeCAD install path)"
    exit 1
fi

echo "=== Running nesting diagnostic ==="
echo "Fixture: $FIXTURE"
echo "FreeCAD: $(freecadcmd --version 2>&1 | head -1)"
echo

# Update the script's fixture path if a different one was specified
if [[ "$FIXTURE" != "replay-fixture.FCStd" ]]; then
    sed -i "s|replay-fixture.FCStd|$FIXTURE|" "$SCRIPT"
fi

# Run diagnostic
cd "$REPO_ROOT"
freecadcmd "$SCRIPT" 2>&1 | tee "/tmp/nesting_diag_$(date +%Y%m%d_%H%M%S).out"

echo
echo "=== Done ==="
echo "JSON output above can be saved and compared with:"
echo "  python3 tests/compare_nesting_results.py result1.json result2.json"