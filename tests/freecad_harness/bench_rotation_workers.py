#!/usr/bin/env freecadcmd
"""Block 5.3: rotation thread-pool width benchmark.

The experiment `make-faster.md` lists as outstanding ("Then test four rotation
workers versus eight as a separate, independent experiment", line 2114), and
the one that would settle main@eac1e30's claim that "serial evaluation is
2.4-3.3x faster" (nesting_strategy.py, where they deleted the pool).

Methodology, taken from the notebook's own warning at lines 100-105
----------------------------------------------------------------------
  "find_best_placement takes the best with a strict `<`, so on a metric tie the
   first future to finish wins and completion order is scheduling-dependent.
   The result is not provably width-neutral, and the timing drift above would
   swamp a real 2x. It needs a pinned seed and interleaved A/B in one session,
   not two runs on different days."

So this script, per make-faster.md:

  * pins the seed and the corpus, so every run sees identical geometry;
  * **interleaves** the widths round-robin within a single process, so machine
    drift and thermal effects hit every width equally instead of biasing
    whichever width ran first;
  * clears the NFP and decomposition caches before every single run, because
    they are class-level and would otherwise make runs 2..N all cache hits and
    measure nothing;
  * records the packing result alongside the timing, and checks whether the
    result is in fact invariant across widths rather than assuming it.

That last point is the notebook's own caveat and the more interesting output:
if the layout does change with width, then the pool is not merely a speed knob
and a width change is a behaviour change.

WIDTHS comes from NEST_BENCH_ROTATION_WORKERS_SWEEP (comma separated);
REPS from NEST_BENCH_REPS. Both default to what suits this 4-CPU box.
"""
import json
import os
import random
import statistics
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_bench")

# Defaults chosen for a 4-CPU machine. Width 8 deliberately oversubscribes 2x:
# with 4 CPUs available, "is the pool worth it" and "does oversubscription
# hurt" are the same question, and main's claim is about serial beating it.
WIDTHS = [int(w) for w in os.environ.get(
    "NEST_BENCH_ROTATION_WORKERS_SWEEP", "1,2,4,8").split(",") if w.strip()]
REPS = int(os.environ.get("NEST_BENCH_REPS", "5"))
SEED = int(os.environ.get("NEST_BENCH_SEED", "20260925"))
SHEET = os.environ.get("NEST_BENCH_SHEET", "450x350")
QUANTITY = int(os.environ.get("NEST_BENCH_QUANTITY", "3"))
SPACING = 5.0
DEFLECTION = 0.05
SIMPLIFICATION = 0.1
ROTATION_STEPS = 4


def emit(message=""):
    try:
        import FreeCAD

        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


def build_sources(doc, seed, quantity):
    """The corpus for this run.

    "synthetic" (default) rebuilds the same deterministic set as
    nest_benchmark.py. A path to a .FCStd opens it once and reuses the
    discovered parts across every rep, which is what makes the heavier n70
    corpus affordable here -- its Spacer is the 192-convex-piece part the
    notebook's original timings were taken on.

    Note the synthetic corpus is small: 18 parts nesting in ~1.1 s. That is
    light enough that thread-pool start-up could cancel a real parallel gain,
    so the n70 run exists as a second, heavier data point rather than trusting
    one number.
    """
    import FreeCAD
    import Part

    corpus = os.environ.get("NEST_BENCH_CORPUS", "synthetic")
    if corpus != "synthetic":
        if not os.path.exists(corpus):
            raise FileNotFoundError(corpus)
        source_doc = FreeCAD.openDocument(corpus)
        parts = [
            o for o in source_doc.Objects
            if o.TypeId not in _SKIP_TYPEIDS
            and hasattr(o, "Shape") and o.Shape and not o.Shape.isNull()
            and not any(
                getattr(other, "Group", None)
                and any(getattr(c, "Name", None) == o.Name for c in other.Group)
                for other in source_doc.Objects
                if other.Name != o.Name
                and getattr(other, "TypeId", "") not in _SKIP_TYPEIDS
            )
        ]
        return parts, {p.Label: quantity for p in parts}

    rng = random.Random(seed)
    parts = []

    def add(name, shape):
        obj = doc.addObject("Part::Feature", name)
        obj.Shape = shape
        parts.append(obj)
        return obj

    for i in range(3):
        add(f"Rect{i}", Part.makeBox(rng.randrange(40, 90), rng.randrange(30, 70), 10))
    add("LShape", Part.makeBox(80, 80, 10).cut(
        Part.makeBox(50, 50, 10, FreeCAD.Vector(30, 30, 0))))
    add("Triangle", Part.makePolygon([
        FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(70, 0, 0),
        FreeCAD.Vector(0, 60, 0), FreeCAD.Vector(0, 0, 0),
    ]).extrude(FreeCAD.Vector(0, 0, 10)))
    add("HoledPlate", Part.makeBox(100, 100, 10).cut(
        Part.makeCylinder(22, 20, FreeCAD.Vector(50, 50, -5))))

    return parts, {p.Label: quantity for p in parts}


_SKIP_TYPEIDS = {
    "App::Origin", "App::Line", "App::Plane", "App::Point", "App::Part",
    "PartDesign::Line", "PartDesign::Plane", "PartDesign::Point",
    "PartDesign::CoordinateSystem",
}


def one_run(width):
    """One timed nest at a given pool width, with cold caches.

    Returns a dict: timing plus a fingerprint of the packing result, so a
    width change that alters the layout is visible rather than assumed away.
    """
    import FreeCAD

    from freecad.nestingworkbench.Tools.Nesting import nesting_logic
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.datatypes.shape import Shape
    from freecad.nestingworkbench.Tools.Nesting.algorithms import minkowski_utils

    # Read at call time by nesting_strategy._rotation_worker_limit(), so this
    # can change between runs in one session.
    os.environ["NESTING_ROTATION_WORKERS"] = str(width)

    # Class-level and process-wide: without this, run 2 onwards is all cache
    # hits and measures nothing.
    Shape.clear_nfp_cache()
    Shape.clear_caches()
    minkowski_utils.clear_dead_ring_profiles()
    minkowski_utils.reset_dead_ring_stats()

    doc = FreeCAD.newDocument(f"w{width}_{time.time()}")
    parts, quantities = build_sources(doc, SEED, QUANTITY)

    layout_group = doc.addObject("App::DocumentObjectGroup", "Layout_bench")
    parts_group = doc.addObject("App::DocumentObjectGroup", "PartsToPlace")
    shared_group = doc.addObject("App::DocumentObjectGroup", "MasterShapes")

    ui = {
        "spacing": SPACING, "deflection": DEFLECTION,
        "simplification": SIMPLIFICATION, "rotation_steps": ROTATION_STEPS,
        "add_labels": False, "font_path": "", "verbose": False,
    }
    full_qty = {
        label: {"quantity": q, "rotation_steps": ROTATION_STEPS,
                "up_direction": "Z+", "fill_sheet": False}
        for label, q in quantities.items()
    }

    preparer = ShapePreparer(doc, {}, create_doc_objects=False,
                            master_pool={}, shared_master_group=shared_group)
    shapes = preparer.prepare_parts(ui, full_qty, {p.Label: p for p in parts},
                                    layout_group, parts_group)

    width_mm, height_mm = (float(v) for v in SHEET.lower().split("x"))
    started = time.perf_counter()
    sheets, unplaced, _steps, _elapsed = nesting_logic.nest(
        shapes, width_mm, height_mm, rotation_steps=ROTATION_STEPS,
        algorithm="Minkowski", rng=random.Random(SEED), quiet=True,
    )
    wall = time.perf_counter() - started

    used_area = 0.0
    for sheet in sheets:
        for part in sheet.parts:
            poly = part.shape.polygon
            if poly is not None and not poly.is_empty:
                used_area += poly.area
    density = (used_area / (width_mm * height_mm * len(sheets))) if sheets else 0.0

    return {
        "width": width,
        "wall": wall,
        "sheets": len(sheets),
        "placed": sum(len(s.parts) for s in sheets),
        "unplaced": len(unplaced),
        "density": round(density, 6),
    }

    # The document and its objects go out of scope here; FreeCAD frees them.


def main():
    emit("=== rotation worker width sweep ===")
    emit(f"  widths        {WIDTHS}")
    emit(f"  reps          {REPS} (interleaved: round-robin within one session)")
    emit(f"  seed          {SEED}")
    emit(f"  sheet         {SHEET}")
    emit(f"  cpus          {os.cpu_count()}")
    if (max(WIDTHS) or 1) > (os.cpu_count() or 1):
        emit(f"  NOTE: width {max(WIDTHS)} exceeds {os.cpu_count()} CPUs "
             f"(oversubscribed)")

    results = {w: [] for w in WIDTHS}
    for rep in range(REPS):
        # Interleaved, per make-faster.md: every width sees the same drift.
        for width in WIDTHS:
            try:
                r = one_run(width)
            except Exception:
                emit(f"  rep {rep} width {width} FAILED")
                traceback.print_exc()
                return 3
            results[width].append(r)
            emit(f"  rep {rep}  width {width:>2}  "
                 f"wall {r['wall']:6.3f}s  placed {r['placed']:>3}  "
                 f"sheets {r['sheets']}  density {r['density']:.6f}")

    emit("")
    emit("=== summary (min is the cleanest estimator under noise) ===")
    emit(f"  {'width':>5} {'min':>8} {'median':>8} {'max':>8} {'spread':>8}  result")
    baseline_wall = None
    for width in WIDTHS:
        walls = [r["wall"] for r in results[width]]
        lo, hi = min(walls), max(walls)
        med = statistics.median(walls)
        # Result fingerprint: do the layouts agree across reps at this width?
        fps = {(r["sheets"], r["placed"], r["unplaced"], r["density"])
               for r in results[width]}
        result_note = "stable" if len(fps) == 1 else f"VARIES {len(fps)}x"
        if baseline_wall is None:
            baseline_wall = lo
            rel = ""
        else:
            rel = f"  min vs width {WIDTHS[0]}: {lo / baseline_wall:.2f}x"
        emit(f"  {width:>5} {lo:>8.3f} {med:>8.3f} {hi:>8.3f} "
             f"{hi - lo:>8.3f}  {result_note}{rel}")

    # Is the packing result invariant across widths? The notebook says it is
    # not provably width-neutral; measure it rather than assume.
    all_fps = {}
    for width in WIDTHS:
        for r in results[width]:
            all_fps.setdefault(
                (r["sheets"], r["placed"], r["unplaced"], r["density"]), []
            ).append(width)
    emit("")
    emit(f"=== distinct packing results across all {len(results[WIDTHS[0]] * len(WIDTHS))} runs: {len(all_fps)} ===")
    for fp, ws in sorted(all_fps.items(), key=lambda kv: -len(kv[1])):
        emit(f"  sheets={fp[0]} placed={fp[1]} unplaced={fp[2]} "
             f"density={fp[3]:.6f}   widths {sorted(set(ws))}")
    if len(all_fps) == 1:
        emit("  -> packing result IS invariant across widths on this corpus")
    else:
        emit("  -> packing result is NOT width-invariant. The pool is not a "
             "pure speed knob here: find_best_placement breaks metric ties "
             "with a strict `<`, so on a tie the first future to finish wins "
             "and completion order is scheduling-dependent.")

    out = os.environ.get("NEST_BENCH_ROTATION_OUT")
    if out:
        with open(out, "w") as handle:
            json.dump({"widths": WIDTHS, "reps": REPS, "seed": SEED,
                       "sheet": SHEET, "cpus": os.cpu_count(),
                       "results": {str(w): results[w] for w in WIDTHS}},
                      handle, indent=2, sort_keys=True)
        emit(f"\nwrote {out}")
    return 0


if __name__ in ("__main__", "bench_rotation_workers"):
    try:
        _status = main()
    except Exception:
        traceback.print_exc()
        _status = 3
    try:
        with open(_STATUS_FILE, "w") as _handle:
            _handle.write(str(_status))
    except OSError:
        pass
    sys.exit(_status)
