"""Is the union cost reducible, or is 445,616 inputs the honest answer?

The counters say the union is 71% of NFP time, that 8 calls out of 120 hold
98.4% of the inputs, and that the worst single call turns 343,658 input vertices
into 57 output vertices -- 0.02% retention, at 0.38% area retention. Almost
everything handed to the union is redundant.

"Redundant" is not the same as "removable", and this probe is about telling those
apart. Four measurements, cheapest and most decisive first:

1. **Calibration.** How long does GEOS take to union N polygons that are *simple
   and disjoint*, against N polygons of the same size that overlap heavily?
   That ratio is the cost of overlap, isolated from the cost per polygon, and it
   is what says whether 47 us per input is bad or merely what overlap costs.
2. **Exact duplicates.** Provably safe to drop -- a duplicate contributes
   nothing a non-duplicate does not. O(n) to find by WKB, and it is the only
   reduction here that needs no geometric argument.
3. **Whether deduplication actually pays.** Interleaved A/B, min of 3, because
   one run of each at this scale is noise and run order would bias it.
4. **Envelope containment**, reported as a count but explicitly *not* used to
   filter. With 54% of inputs sharing an envelope, the containment scan is
   O(n^2) -- 3 billion comparisons on the worst call. That is the finding, and
   measuring it is cheaper than attempting it.

Wraps minkowski_utils.unary_union rather than editing the function, so
production code is untouched and the measurement is of the real call.

Run: $FREECADCMD tests/freecad_harness/probe_union_cost.py
"""
import os
import random
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_common as hc  # noqa: E402

RUNS = int(os.environ.get("PROBE_UNION_REPS", "3"))


def main():
    import FreeCAD
    from shapely.geometry import box
    from shapely.ops import unary_union

    from freecad.nestingworkbench.Tools.Nesting import nesting_logic
    from freecad.nestingworkbench.Tools.Nesting.algorithms import minkowski_utils as mu
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.datatypes.shape import Shape

    captured = []
    real_union = mu.unary_union

    def spy(parts):
        if parts:
            captured.append(list(parts))
        return real_union(parts)

    mu.unary_union = spy
    try:
        _run_one_nest(nesting_logic, ShapePreparer, Shape, FreeCAD)
    finally:
        mu.unary_union = real_union

    if not captured:
        hc.emit("no union calls captured")
        return

    biggest = max(captured, key=len)
    _report_shape(biggest, unary_union)
    _calibrate(biggest, box, unary_union)
    _duplicates(biggest, unary_union)


def _report_shape(geoms, unary_union):
    total_vertices = sum(len(g.exterior.coords) for g in geoms)
    distinct_wkb = len({g.wkb for g in geoms})
    distinct_env = len({g.bounds for g in geoms})
    total_area = sum(g.area for g in geoms)

    result = unary_union(geoms)
    result_vertices = len(result.exterior.coords) if result.geom_type == "Polygon" else 0

    hc.emit("")
    hc.emit("=== worst single union call ===")
    hc.emit(f"  inputs                {len(geoms):,}")
    hc.emit(f"  input vertices        {total_vertices:,}")
    hc.emit(f"  distinct WKB          {distinct_wkb:,}  "
            f"({100.0 * (1 - distinct_wkb / max(1, len(geoms))):.1f}% exact duplicates)")
    hc.emit(f"  distinct envelopes    {distinct_env:,}  "
            f"({100.0 * (1 - distinct_env / max(1, len(geoms))):.1f}% share one)")
    hc.emit(f"  sum of input areas    {total_area:,.0f}")
    hc.emit(f"  result vertices       {result_vertices:,}")
    hc.emit(f"  vertex retention      {100.0 * result_vertices / max(1, total_vertices):.2f}%")
    hc.emit(f"  area retention        {100.0 * result.area / max(1.0, total_area):.2f}%")


def _calibrate(geoms, box, unary_union):
    """Cost per polygon, with overlap and without.

    Same count, same shape, laid out disjointly versus stacked on one spot. The
    ratio is what overlap costs; the disjoint figure is what GEOS can do when
    there is nothing to actually merge.
    """
    n = len(geoms)
    reference = geoms[0]
    minx, miny, maxx, maxy = reference.bounds
    w = max(maxx - minx, 1e-6)
    h = max(maxy - miny, 1e-6)

    # Disjoint: a grid with one polygon per cell, so no two envelopes overlap.
    side = int(n ** 0.5) + 1
    spread = side * 1.5
    disjoint = [box((i % side) * spread, (i // side) * spread,
                    (i % side) * spread + w, (i // side) * spread + h)
                for i in range(n)]
    # Stacked: every polygon in the same place, so every pair overlaps.
    stacked = [box(0, 0, w, h) for _ in range(n)]

    def timed(parts, reps):
        samples = []
        for _ in range(reps):
            started = time.perf_counter()
            unary_union(parts)
            samples.append((time.perf_counter() - started) * 1000)
        return min(samples)

    dis_ms = timed(disjoint, RUNS)
    sta_ms = timed(stacked, RUNS)

    hc.emit("")
    hc.emit("=== calibration: same count and shape, overlap vs not ===")
    hc.emit(f"  {n:,} disjoint boxes      {dis_ms:9.2f} ms  "
            f"({1000.0 * dis_ms / n:6.2f} us each)")
    hc.emit(f"  {n:,} fully stacked boxes  {sta_ms:9.2f} ms  "
            f"({1000.0 * sta_ms / n:6.2f} us each)")
    hc.emit(f"  overlap cost factor      {sta_ms / max(dis_ms, 1e-9):.1f}x")
    hc.emit("")
    hc.emit("  So there are two separate costs: the per-polygon cost GEOS pays")
    hc.emit("  regardless, and the extra cost of polygons that actually overlap.")
    hc.emit("  A reduction only helps the second one.")


def _duplicates(geoms, unary_union):
    """Dropping exact duplicates is provably safe. Does it pay?"""
    seen = {}
    unique = []
    for geom in geoms:
        key = geom.wkb
        if key not in seen:
            seen[key] = geom
            unique.append(geom)

    hc.emit("")
    hc.emit("=== exact-duplicate removal (sound: a duplicate adds nothing) ===")
    hc.emit(f"  inputs {len(geoms):,} -> unique {len(unique):,}  "
            f"({100.0 * (1 - len(unique) / max(1, len(geoms))):.1f}% removed)")

    reduced = unary_union(unique)
    full = unary_union(geoms)
    hc.emit(f"  result identical: "
            f"{reduced.equals(full) and abs(reduced.area - full.area) < 1e-6}")
    if not (reduced.equals(full) and abs(reduced.area - full.area) < 1e-6):
        hc.emit("  <-- deduplication changed the result, discard the timing")
        return

    full_ms, uniq_ms = [], []
    for rep in range(RUNS):
        started = time.perf_counter()
        unary_union(geoms)
        full_ms.append((time.perf_counter() - started) * 1000)
        started = time.perf_counter()
        unary_union(unique)
        uniq_ms.append((time.perf_counter() - started) * 1000)
        hc.emit(f"  rep {rep}  full {full_ms[-1] / 1000:7.3f}s   "
                f"deduped {uniq_ms[-1] / 1000:7.3f}s")

    dedup_ms = _time_dedup(geoms)
    hc.emit("")
    hc.emit(f"  full    min {min(full_ms) / 1000:7.3f}s")
    hc.emit(f"  deduped min {min(uniq_ms) / 1000:7.3f}s  "
            f"({min(uniq_ms) / min(full_ms):.3f}x)")
    hc.emit(f"  dedup pass {dedup_ms:7.3f}s (WKB hashing, 3 reps averaged)")
    hc.emit(f"  net     {(min(uniq_ms) + dedup_ms - min(full_ms)):+.3f}s")


def _time_dedup(geoms):
    started = time.perf_counter()
    for _ in range(3):
        seen = set()
        for geom in geoms:
            seen.add(geom.wkb)
    return (time.perf_counter() - started) * 1000 / 3


def _run_one_nest(nesting_logic, ShapePreparer, Shape, FreeCAD):
    """One heavy-corpus nest, so the probe sees a realistic worst-case call."""
    corpus = os.environ.get("NEST_BENCH_CORPUS", "heavy")
    sheet = os.environ.get("NEST_BENCH_SHEET", "1200x600")
    steps = int(os.environ.get("NEST_BENCH_ROTATION_STEPS", "8"))

    Shape.clear_nfp_cache()
    Shape.clear_caches()

    doc = FreeCAD.newDocument("probe_union")
    if corpus == "heavy":
        parts, quantities = hc.build_heavy_corpus(
            doc, 20260925, 2,
            {"HeavyPlate": 2, "Small0": 30, "Small1": 30, "Small2": 30,
             "SmallL": 30})
    elif corpus == "synthetic":
        parts, quantities = hc.build_synthetic_corpus(doc, 20260925, 3)
    else:
        source = FreeCAD.openDocument(corpus)
        parts = hc.discover_doc_parts(source)
        quantities = {p.Label: 1 for p in parts}

    layout = doc.addObject("App::DocumentObjectGroup", "L")
    parts_group = doc.addObject("App::DocumentObjectGroup", "P")
    shared = doc.addObject("App::DocumentObjectGroup", "M")
    ui = {"spacing": 5.0, "deflection": 0.05, "simplification": 0.1,
          "rotation_steps": steps, "add_labels": False, "font_path": "",
          "verbose": False}
    full_qty = {label: {"quantity": q, "rotation_steps": steps,
                        "up_direction": "Z+", "fill_sheet": False}
                for label, q in quantities.items()}
    preparer = ShapePreparer(doc, {}, create_doc_objects=False, master_pool={},
                             shared_master_group=shared)
    shapes = preparer.prepare_parts(ui, full_qty, {p.Label: p for p in parts},
                                    layout, parts_group)
    width, height = (float(v) for v in sheet.lower().split("x"))
    nesting_logic.nest(shapes, width, height, rotation_steps=steps,
                       algorithm="Minkowski", rng=random.Random(20260925),
                       quiet=True)


if __name__ in ("__main__", "probe_union_cost"):
    main()
