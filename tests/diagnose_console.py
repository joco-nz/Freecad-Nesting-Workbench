"""
Paste this ENTIRE block into FreeCAD Python console (View → Panels → Python console)

Works from BOTH:
  - Development checkout: /path/to/Freecad-Nesting-Workbench
  - Installed workbench: ~/.FreeCAD/Mod/Jet-Nesting/ (or similar)

The script auto-detects the workbench location and finds the test fixture.
"""

import json, os, sys, platform, time, traceback
from datetime import datetime

# ─── Auto-detect workbench root ──────────────────────────────────────────
# When pasted in console, __file__ doesn't exist. Use FreeCAD's module path.
try:
    import freecad.nestingworkbench
    # Module is at .../Freecad-Nesting-Workbench/freecad/nestingworkbench/__init__.py
    # Workbench root is two levels up from freecad/nestingworkbench/
    WB_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(freecad.nestingworkbench.__file__)))
except ImportError:
    # Fallback: check common install locations
    for base in (
        os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "Freecad-Nesting-Workbench"),
        os.path.join(FreeCAD.getHomePath(), "Mod", "Freecad-Nesting-Workbench"),
        "/usr/share/freecad/Mod/Freecad-Nesting-Workbench",
        "/usr/local/share/freecad/Mod/Freecad-Nesting-Workbench",
        os.path.expanduser("~/.local/share/FreeCAD/Mod/Freecad-Nesting-Workbench"),
    ):
        if os.path.isdir(base):
            WB_ROOT = base
            break
    else:
        # Last resort: try to find it via sys.path
        for p in sys.path:
            cand = os.path.join(p, "freecad", "nestingworkbench")
            if os.path.isdir(cand):
                WB_ROOT = os.path.dirname(os.path.dirname(cand))
                break
        else:
            raise RuntimeError("Could not locate Freecad-Nesting-Workbench. "
                               "Is it installed? Try: import freecad.nestingworkbench")

FIXTURE = os.path.join(WB_ROOT, "tests", "Test_Files", "replay-fixture.FCStd")
SHEET_W, SHEET_H = 1000.0, 600.0

os.environ["NESTING_ROTATION_WORKERS"] = "0"

log = []
def L(msg):
    line = f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {msg}"
    log.append(line); print(line)

try:
    import FreeCAD, Part, shapely, numpy as np
    L(f"Platform: {platform.platform()}")
    L(f"FreeCAD: {FreeCAD.Version()}")
    L(f"Shapely: {shapely.__version__}  GEOS: {shapely.geos_version}")
    L(f"NumPy: {np.__version__}")
    L(f"Workbench root: {WB_ROOT}")
    L(f"Fixture: {FIXTURE} ({'found' if os.path.exists(FIXTURE) else 'MISSING!'})")
    
    if not os.path.exists(FIXTURE):
        raise FileNotFoundError(f"Test fixture not found at {FIXTURE}")
    
    doc = FreeCAD.openDocument(FIXTURE)
    L(f"Loaded {doc.Name} ({len(doc.Objects)} objects)")
    L("ALL OBJECTS IN DOCUMENT:")
    for o in doc.Objects:
        L(f"  {o.Name:30} {o.TypeId:25} Label={o.Label} HasShape={hasattr(o,'Shape')}")
        if hasattr(o, 'Shape') and o.Shape:
            L(f"    -> Shape: {type(o.Shape).__name__} Vol={o.Shape.Volume:.1f} Area={o.Shape.Area:.1f}")
        if hasattr(o, 'Group') and o.Group:
            L(f"    -> Group children: {[c.Name for c in o.Group]}")
    
    SKIP = {"App::Origin","App::Line","App::Plane","App::Point","App::Part",
            "App::DocumentObjectGroup",
            "PartDesign::Line","PartDesign::Plane","PartDesign::Point",
            "PartDesign::CoordinateSystem"}
    cands = [o for o in doc.Objects if o.TypeId not in SKIP and hasattr(o,"Shape") and o.Shape and not o.Shape.isNull()]
    L(f"Candidates with Shape: {len(cands)}")
    for o in cands:
        L(f"  {o.Name} ({o.TypeId}) Label={o.Label} Shape={type(o.Shape).__name__}")
    tops = [o for o in cands if not any(
        other.Name!=o.Name and getattr(other,"Group",None) and any(getattr(c,"Name",None)==o.Name for c in other.Group)
        for other in cands)]
    L(f"Top-level parts: {len(tops)}")
    for o in tops: L(f"  {o.Name} ({o.TypeId}) Label={o.Label} Vol={o.Shape.Volume:.1f} Area={o.Shape.Area:.1f}")
    
    # Ensure workbench is on sys.path (needed for installed location)
    if WB_ROOT not in sys.path:
        sys.path.insert(0, WB_ROOT)
    
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.datatypes.shape import Shape
    from freecad.nestingworkbench.freecad_helpers import recursive_delete
    Shape.clear_nfp_cache(); Shape.clear_caches()
    
    # Build required parameters for ShapePreparer.prepare_parts()
    # ui_global_settings
    ui_params = {
        "spacing": 5.0,
        "deflection": 0.05,
        "simplification": 0.1,
        "rotation_steps": 4,
        "add_labels": False,
        "font_path": "",
        "verbose": True,
    }
    # quantities: label -> dict with quantity, rotation_steps, up_direction, fill_sheet
    quantities = {p.Label: {"quantity": 1, "rotation_steps": 4, "up_direction": "Z+", "fill_sheet": False} for p in tops}
    # master_shapes_map: label -> FreeCAD object
    master_shapes_map = {p.Label: p for p in tops}
    
    # Create layout and parts groups (needed by prepare_parts)
    layout_group = doc.addObject("App::DocumentObjectGroup", "DiagLayout")
    parts_group = doc.addObject("App::DocumentObjectGroup", "PartsToPlace")
    layout_group.addObject(parts_group)
    
    prep = ShapePreparer(doc, {}, create_doc_objects=True)
    parts = prep.prepare_parts(ui_params, quantities, master_shapes_map, layout_group, parts_group)
    L(f"Prepared {len(parts)} instances")
    for p in parts: L(f"  {p.id} area={p.area:.1f} holes={len(p.polygon.interiors) if p.polygon else 0}")
    
    # Clean up the temporary groups
    recursive_delete(doc, layout_group)
    
    from freecad.nestingworkbench.Tools.Nesting.nesting_logic import nest
    trials = []
    def tc(p,a,x,y): trials.append({"id":p.id,"a":a,"x":x,"y":y})
    def sc(p): L(f"START {p.id}")
    def ec(p,ok): L(f"END {p.id} {'OK' if ok else 'FAIL'}")
    def pc(c,t,m): 
        if c%10==0 or c==t: L(f"  {c}/{t} {m}")
    
    t0=time.perf_counter()
    sheets, unplaced, steps, et = nest(parts, SHEET_W, SHEET_H, rotation_steps=4,
        spacing=5, deflection=0.05, simplification=0.1, algorithm="Minkowski",
        search_direction=(0,-1), performance_logging=True, verbose=True,
        trial_callback=tc, part_start_callback=sc, part_end_callback=ec, progress_callback=pc)
    wt=time.perf_counter()-t0
    
    L(f"\n=== RESULT ===")
    L(f"Sheets: {len(sheets)}  Placed: {sum(len(s.parts) for s in sheets)}  Unplaced: {len(unplaced)}  Time: {wt:.2f}s")
    for i,s in enumerate(sheets):
        L(f" Sheet {i+1}: {len(s.parts)} parts, {s.used_area:.0f}mm² used ({(s.used_area/(s.width*s.height)*100):.1f}%)")
        for p in s.parts: 
            plc = p.shape.placement
            L(f"   {p.shape.id} @ ({plc.Base.x:.1f},{plc.Base.y:.1f}) a={p.angle}")
    for p in unplaced: L(f" UNPLACED: {p.id} area={p.area:.1f}")
    
    result = {
        "platform": platform.platform(), "freecad": ".".join(map(str,FreeCAD.Version()[:3])),
        "shapely": shapely.__version__, "geos": shapely.geos_version, "numpy": np.__version__,
        "sheets": len(sheets), "placed": sum(len(s.parts) for s in sheets), "unplaced": len(unplaced),
        "unplaced_ids": [p.id for p in unplaced], "wall_time": wt, "sheets_detail": [
            {"idx":i, "n":len(s.parts), "area":s.used_area, "eff":s.used_area/(s.width*s.height)*100,
             "parts":[{"id":p.shape.id,"x":p.shape.placement.Base.x,"y":p.shape.placement.Base.y,"a":p.angle} for p in s.parts]}
            for i,s in enumerate(sheets)]
    }
    
    print("\n=== COPY THIS JSON FOR COMPARISON ===")
    print(json.dumps(result, indent=2))
    
    FreeCAD.closeDocument(doc.Name)
    
except Exception as e:
    L(f"ERROR: {e}")
    traceback.print_exc()
    print("\n=== LOG ===")
    print("\n".join(log))