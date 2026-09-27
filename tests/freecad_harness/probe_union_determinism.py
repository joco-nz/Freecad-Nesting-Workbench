"""Is unary_union deterministic, and is union_ms measuring a stable thing?

The duplicate-removal check in probe_union_cost reported that unioning
54,756 polygons and then unioning the 54,750 unique ones gave a *different*
result. That should be impossible -- removing exact duplicates cannot change a
union. So either the check is wrong or the union is not deterministic.

It matters well beyond that one check. If two calls on identical input give
different geometry, then:

  * the NFP polygon is not a pure function of its inputs;
  * `union_ms`, at 71% of NFP time, is timing a process with no fixed answer;
  * and any optimisation of the union would be built on sand.

So this is checked first, with a minimal case: the same input list twice, and
the same input list reordered.

Run: $FREECADCMD tests/freecad_harness/probe_union_determinism.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_common as hc  # noqa: E402


def main():
    from shapely.geometry import box
    from shapely.ops import unary_union

    hc.emit("=== 1. identical input, unioned twice ===")
    parts = [box(i % 10, i // 10, i % 10 + 1, i // 10 + 1) for i in range(500)]
    first = unary_union(parts)
    second = unary_union(parts)
    hc.emit(f"  equals       {first.equals(second)}")
    hc.emit(f"  area delta   {abs(first.area - second.area):.3e}")
    hc.emit(f"  wkb identical {first.wkb == second.wkb}")
    hc.emit(f"  vertex counts {len(first.geoms) if first.geom_type.startswith('Multi') else 1}"
            f" / {len(second.geoms) if second.geom_type.startswith('Multi') else 1}")

    hc.emit("")
    hc.emit("=== 2. same input, reversed order ===")
    third = unary_union(list(reversed(parts)))
    hc.emit(f"  equals       {first.equals(third)}")
    hc.emit(f"  area delta   {abs(first.area - third.area):.3e}")
    hc.emit(f"  wkb identical {first.wkb == third.wkb}")

    hc.emit("")
    hc.emit("=== 3. duplicates removed ===")
    with_dupes = [box(0, 0, 1, 1)] * 3 + [box(2, 0, 3, 1)] * 3
    without = [box(0, 0, 1, 1), box(2, 0, 3, 1)]
    a = unary_union(with_dupes)
    b = unary_union(without)
    hc.emit(f"  equals       {a.equals(b)}")
    hc.emit(f"  area delta   {abs(a.area - b.area):.3e}")

    hc.emit("")
    hc.emit("=== 4. heavily overlapping, single output ===")
    # 4000 boxes all covering the origin, so the answer is one small polygon.
    # This is the shape the real worst-case call has.
    stacked = [box(-i * 0.001, -i * 0.001, 10 + i * 0.001, 10 + i * 0.001)
               for i in range(4000)]
    x = unary_union(stacked)
    y = unary_union(stacked)
    hc.emit(f"  equals across calls   {x.equals(y)}")
    hc.emit(f"  wkb identical         {x.wkb == y.wkb}")
    hc.emit(f"  vertices {len(x.exterior.coords) if x.geom_type == 'Polygon' else -1}")

    hc.emit("")
    hc.emit("=== 5. the real worst-case shape, via the real pipeline ===")
    _real_case()


def _real_case():
    """The actual NFP inputs, unioned twice, to see whether the real case is
    stable. A toy case that is deterministic says nothing about a case with
    54,756 inputs and 51% shared envelopes."""
    import FreeCAD
    from shapely.ops import unary_union

    from freecad.nestingworkbench.Tools.Nesting import nesting_logic
    from freecad.nestingworkbench.Tools.Nesting.algorithms import minkowski_utils as mu
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.datatypes.shape import Shape

    captured = []
    real = mu.unary_union

    def spy(parts):
        if parts:
            captured.append(list(parts))
        return real(parts)

    mu.unary_union = spy
    try:
        Shape.clear_nfp_cache()
        Shape.clear_caches()
        doc = FreeCAD.newDocument("probe_determinism")
        parts, quantities = hc.build_heavy_corpus(
            doc, 20260925, 2,
            {"HeavyPlate": 2, "Small0": 30, "Small1": 30, "Small2": 30,
             "SmallL": 30})
        layout = doc.addObject("App::DocumentObjectGroup", "L")
        pg = doc.addObject("App::DocumentObjectGroup", "P")
        shared = doc.addObject("App::DocumentObjectGroup", "M")
        ui = {"spacing": 5.0, "deflection": 0.05, "simplification": 0.1,
              "rotation_steps": 8, "add_labels": False, "font_path": "",
              "verbose": False}
        qty = {k: {"quantity": v, "rotation_steps": 8, "up_direction": "Z+",
                   "fill_sheet": False} for k, v in quantities.items()}
        pre = ShapePreparer(doc, {}, create_doc_objects=False, master_pool={},
                            shared_master_group=shared)
        shapes = pre.prepare_parts(ui, qty, {p.Label: p for p in parts},
                                   layout, pg)
        import random
        nesting_logic.nest(shapes, 1200.0, 600.0, rotation_steps=8,
                           algorithm="Minkowski",
                           rng=random.Random(20260925), quiet=True)
    finally:
        mu.unary_union = real

    if not captured:
        hc.emit("  no calls captured")
        return

    biggest = max(captured, key=len)
    hc.emit(f"  inputs {len(biggest):,}")
    r1 = unary_union(biggest)
    r2 = unary_union(biggest)
    hc.emit(f"  equals across calls    {r1.equals(r2)}")
    hc.emit(f"  wkb identical          {r1.wkb == r2.wkb}")
    hc.emit(f"  area delta             {abs(r1.area - r2.area):.3e}")
    hc.emit(f"  type {r1.geom_type} vs {r2.geom_type}")

    # And the duplicate-removal case that flagged this, done properly.
    seen = {}
    unique = []
    for geom in biggest:
        key = geom.wkb
        if key not in seen:
            seen[key] = geom
            unique.append(geom)
    hc.emit(f"  duplicates removed     {len(biggest) - len(unique):,} of {len(biggest):,}")
    r3 = unary_union(unique)
    hc.emit(f"  deduped equals original {r3.equals(r1)}")
    hc.emit(f"  deduped area delta      {abs(r3.area - r1.area):.3e}")


if __name__ in ("__main__", "probe_union_determinism"):
    main()
