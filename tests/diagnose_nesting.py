#!/usr/bin/env freecadcmd
"""
Cross-platform nesting diagnostic script.

Run from FreeCAD Python console:
    exec(open("/path/to/diagnose_nesting.py").read())

Or from terminal:
    freecadcmd diagnose_nesting.py

Outputs JSON to stdout and detailed log to /tmp/nesting_diag_<platform>.log
"""
import json
import os
import sys
import platform
import time
import traceback
from datetime import datetime

# ---------------------------------------------------------------------------
# Configuration — tweak these before running
# ---------------------------------------------------------------------------
FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "Test_Files",
    "replay-fixture.FCStd"
)

# Force serial execution to eliminate thread scheduling variance
os.environ["NESTING_ROTATION_WORKERS"] = "0"

# Optional: disable verify/repair to isolate NFP/candidate generation
# os.environ["NESTING_VERIFY_ON_ACCEPT"] = "0"
# os.environ["NESTING_VERIFY_REPAIR"] = "0"

# Sheet size from the fixture (adjust if different)
SHEET_WIDTH = 1000.0
SHEET_HEIGHT = 600.0

# Nesting parameters
NEST_PARAMS = {
    "spacing": 5.0,
    "deflection": 0.05,
    "simplification": 0.1,
    "rotation_steps": 4,
    "algorithm": "Minkowski",
    "search_direction": (0, -1),
    "performance_logging": True,
    "verbose": True,
}

# ---------------------------------------------------------------------------
# Diagnostic logging
# ---------------------------------------------------------------------------
LOG_LINES = []
PLATFORM_TAG = f"{platform.system()}_{platform.machine()}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
LOG_PATH = f"/tmp/nesting_diag_{PLATFORM_TAG}.log"

def log(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    LOG_LINES.append(line)
    print(line)

def log_section(title):
    log("=" * 60)
    log(f"  {title}")
    log("=" * 60)

# ---------------------------------------------------------------------------
# Main diagnostic
# ---------------------------------------------------------------------------
def run_diagnostic():
    import FreeCAD
    import Part
    
    log_section("ENVIRONMENT")
    log(f"Platform: {platform.platform()}")
    log(f"Python: {sys.version}")
    log(f"FreeCAD: {FreeCAD.Version()}")
    
    # Shapely/GEOS version
    try:
        import shapely
        log(f"Shapely: {shapely.__version__}")
        log(f"GEOS: {shapely.geos_version}")
        # GEOS version tuple for comparison
        geos_tuple = tuple(map(int, shapely.geos_version.split('.')[:3]))
        log(f"GEOS tuple: {geos_tuple}")
    except Exception as e:
        log(f"Shapely version check failed: {e}")
    
    # NumPy/BLAS info
    try:
        import numpy as np
        log(f"NumPy: {np.__version__}")
        log(f"BLAS: {np.show_config()}")
    except Exception as e:
        log(f"NumPy check failed: {e}")
    
    log_section("LOADING FIXTURE")
    if not os.path.exists(FIXTURE_PATH):
        log(f"ERROR: Fixture not found at {FIXTURE_PATH}")
        return {"error": "fixture not found", "path": FIXTURE_PATH}
    
    log(f"Opening: {FIXTURE_PATH}")
    doc = FreeCAD.openDocument(FIXTURE_PATH)
    log(f"Document: {doc.Name}, Objects: {len(doc.Objects)}")
    
    # Discover parts (same logic as harness)
    SKIP_TYPEIDS = {
        "App::Origin", "App::Line", "App::Plane", "App::Point", "App::Part",
        "PartDesign::Line", "PartDesign::Plane", "PartDesign::Point",
        "PartDesign::CoordinateSystem",
    }
    
    candidates = [
        o for o in doc.Objects
        if o.TypeId not in SKIP_TYPEIDS
        and hasattr(o, "Shape") and o.Shape and not o.Shape.isNull()
    ]
    log(f"Candidate objects: {len(candidates)}")
    for o in candidates:
        log(f"  {o.Label} ({o.TypeId}) - Volume: {o.Shape.Volume:.3f}, Area: {o.Shape.Area:.3f}")
    
    # Find top-level parts
    tops = []
    for o in candidates:
        parented = any(
            other.Name != o.Name
            and getattr(other, "Group", None)
            and any(getattr(c, "Name", None) == o.Name for c in other.Group)
            for other in candidates
        )
        if not parented:
            tops.append(o)
    log(f"Top-level parts: {len(tops)}")
    for o in tops:
        log(f"  {o.Label} ({o.Name})")
    
    # Build parts list with quantities (all qty=1 for this fixture)
    parts = tops
    quantities = {p.Label: 1 for p in parts}
    
    log_section("PREPARING SHAPES")
    # Import the nesting modules
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.datatypes.shape import Shape
    
    Shape.clear_nfp_cache()
    Shape.clear_caches()
    
    preparer = ShapePreparer(doc, {})
    prepared_parts = preparer.prepare_parts(parts, quantities, {})
    log(f"Prepared {len(prepared_parts)} part instances")
    
    for i, p in enumerate(prepared_parts):
        log(f"  [{i}] {p.id} area={p.area:.3f} rot_steps={getattr(p, 'rotation_steps', '?')}")
        if hasattr(p, 'polygon') and p.polygon:
            log(f"      bbox={p.polygon.bounds}, centroid={p.polygon.centroid.x:.3f},{p.polygon.centroid.y:.3f}")
            log(f"      interiors={len(p.polygon.interiors)}")
            for j, interior in enumerate(p.polygon.interiors):
                log(f"        hole[{j}]: area={Polygon(interior).area:.3f}, verts={len(interior.coords)}")
    
    log_section("RUNNING NESTING")
    from freecad.nestingworkbench.Tools.Nesting.nesting_logic import nest
    
    # Add callbacks for detailed logging
    placement_details = []
    
    def trial_callback(part, angle, x, y):
        placement_details.append({
            "part": part.id,
            "angle": angle,
            "x": x,
            "y": y,
            "type": "trial"
        })
    
    def part_start_callback(part):
        log(f"START placing {part.id}")
    
    def part_end_callback(part, placed):
        log(f"END placing {part.id} -> {'PLACED' if placed else 'FAILED'}")
    
    def progress_callback(current, total, msg):
        if current % 10 == 0 or current == total:
            log(f"Progress: {current}/{total} - {msg}")
    
    start_time = time.perf_counter()
    sheets, unplaced, steps, elapsed = nest(
        prepared_parts,
        SHEET_WIDTH,
        SHEET_HEIGHT,
        rotation_steps=NEST_PARAMS["rotation_steps"],
        simulate=False,
        algorithm=NEST_PARAMS["algorithm"],
        spacing=NEST_PARAMS["spacing"],
        deflection=NEST_PARAMS["deflection"],
        simplification=NEST_PARAMS["simplification"],
        search_direction=NEST_PARAMS["search_direction"],
        performance_logging=NEST_PARAMS["performance_logging"],
        verbose=NEST_PARAMS["verbose"],
        trial_callback=trial_callback,
        part_start_callback=part_start_callback,
        part_end_callback=part_end_callback,
        progress_callback=progress_callback,
    )
    wall_time = time.perf_counter() - start_time
    
    log_section("RESULTS")
    log(f"Wall time: {wall_time:.3f}s (nest() returned {elapsed:.3f}s)")
    log(f"Sheets used: {len(sheets)}")
    log(f"Parts placed: {sum(len(s.parts) for s in sheets)}")
    log(f"Parts unplaced: {len(unplaced)}")
    log(f"Steps: {steps}")
    
    for i, sheet in enumerate(sheets):
        log(f"\nSheet {i+1} ({sheet.width}x{sheet.height}):")
        log(f"  Parts: {len(sheet.parts)}")
        log(f"  Used area: {sheet.used_area:.3f}")
        log(f"  Efficiency: {(sheet.used_area / (sheet.width * sheet.height) * 100):.1f}%")
        for p in sheet.parts:
            log(f"  - {p.shape.id} at ({p.placement.Base.x:.3f}, {p.placement.Base.y:.3f}) angle={p.angle}")
    
    if unplaced:
        log(f"\nUNPLACED ({len(unplaced)}):")
        for p in unplaced:
            log(f"  - {p.id} area={p.area:.3f}")
    
    # Performance stats
    log_section("PERFORMANCE STATS")
    # Get perf stats from the nester (need to access the nester instance)
    # The nest() function creates a new nester each call, so we need to re-run with access
    # For now, log what we captured
    
    # Build result dict
    result = {
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "freecad": ".".join(map(str, FreeCAD.Version()[:3])),
            "shapely": getattr(shapely, "__version__", "unknown"),
            "geos": getattr(shapely, "geos_version", "unknown"),
            "numpy": getattr(__import__("numpy"), "__version__", "unknown"),
        },
        "fixture": os.path.basename(FIXTURE_PATH),
        "sheet": {"width": SHEET_WIDTH, "height": SHEET_HEIGHT},
        "params": NEST_PARAMS,
        "sheets_used": len(sheets),
        "parts_placed": sum(len(s.parts) for s in sheets),
        "parts_unplaced": len(unplaced),
        "unplaced_ids": [p.id for p in unplaced],
        "wall_time_s": wall_time,
        "nest_time_s": elapsed,
        "steps": steps,
        "sheet_details": [
            {
                "index": i,
                "parts": len(s.parts),
                "used_area": s.used_area,
                "efficiency_pct": (s.used_area / (s.width * s.height) * 100) if s.width * s.height > 0 else 0,
                "placements": [
                    {
                        "part_id": p.shape.id,
                        "x": p.placement.Base.x,
                        "y": p.placement.Base.y,
                        "angle": p.angle,
                    }
                    for p in s.parts
                ]
            }
            for i, s in enumerate(sheets)
        ],
        "placement_trials": placement_details[:100],  # Limit size
    }
    
    # Save detailed log
    with open(LOG_PATH, "w") as f:
        f.write("\n".join(LOG_LINES))
    log(f"\nDetailed log saved to: {LOG_PATH}")
    
    FreeCAD.closeDocument(doc.Name)
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__" or __name__ == "diagnose_nesting":
    try:
        result = run_diagnostic()
        # Output JSON to stdout for easy capture/comparison
        print("\n=== JSON RESULT ===")
        print(json.dumps(result, indent=2))
        
        # Exit code for CI
        if result.get("parts_unplaced", 0) > 0:
            sys.exit(1)
        sys.exit(0)
    except Exception as e:
        log(f"FATAL ERROR: {e}")
        traceback.print_exc()
        with open(LOG_PATH, "w") as f:
            f.write("\n".join(LOG_LINES))
        sys.exit(2)