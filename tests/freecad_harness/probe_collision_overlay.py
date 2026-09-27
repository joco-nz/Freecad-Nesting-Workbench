"""Can the collision overlay be replaced by cheaper predicates?

RESULTS-collision.md decomposes `collision_intersection_ms` (18.4 s on the heavy
corpus) as:

    intersection().area   12.088 s  65.7%   83.4 us/call, 144,981 calls
    intersects()           4.364 s  23.7%   23.7 us/call, 184,197 calls
    hole probe             1.611 s   8.8%   measurement-only, not in production
    bookkeeping            0.323 s   1.8%

And the counters say something sharper: `collision_grazing_pairs` equals
`collision_intersects_true` -- 144,981 of 144,981. **The overlay rejects every
pair that `intersects` accepts.** On this workload the NFP already guarantees
non-overlap, so candidates sit exactly on the NFP boundary, `intersects` fires
on a touch, and the computed area is zero every single time.

(That is workload-dependent, and the counters show how: on the n70 fixture the
same figures are 98,366 true against 91,456 grazing -- 93%, not 100% -- and
`mask_hole_exploiting_placements` is 0 on both, so no internal-fit placement
ever occurs. An IFP placement genuinely overlaps its container, and that is the
case the overlay exists for.)

The candidate replacement is a predicate, not an overlay. For two polygons,
positive-area intersection is exactly `overlaps` or `contains` in either
direction; a boundary-only touch is none of them. So the question is empirical:
are two predicates cheaper than one constructive overlay, on this geometry?

Measured on the real corpus polygons rather than a synthetic stand-in. The pair
*structure* is constructed -- a grid of translated placements -- because
spying the live mask to capture its exact candidate set turned out to be
unreliable: an exception inside a spy is swallowed by the rotation evaluation's
bare `except` with `quiet=True`, so a broken probe reports "nothing captured"
and looks like a null result. Stating what is real and what is constructed
beats implying both are captured.

Run: $FREECADCMD tests/freecad_harness/probe_collision_overlay.py
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

REPS = int(os.environ.get("PROBE_COLLISION_REPS", "5"))


def main():
    import FreeCAD

    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.datatypes.shape import Shape

    Shape.clear_nfp_cache()
    Shape.clear_caches()

    doc = FreeCAD.newDocument("probe_overlay")
    parts, _quantities = hc.build_heavy_corpus(doc, 20260925, 1)
    layout = doc.addObject("App::DocumentObjectGroup", "L")
    pg = doc.addObject("App::DocumentObjectGroup", "P")
    shared = doc.addObject("App::DocumentObjectGroup", "M")
    ui = {"spacing": 5.0, "deflection": 0.05, "simplification": 0.1,
          "rotation_steps": 8, "add_labels": False, "font_path": "",
          "verbose": False}
    preparer = ShapePreparer(doc, {}, create_doc_objects=False, master_pool={},
                             shared_master_group=shared)
    shapes = preparer.prepare_parts(
        ui, {p.Label: {"quantity": 1, "rotation_steps": 8, "up_direction": "Z+",
                       "fill_sheet": False} for p in parts},
        {p.Label: p for p in parts}, layout, pg)

    plate = None
    small = []
    for shape in shapes:
        poly = getattr(shape, "original_polygon", None)
        if poly is None or poly.is_empty:
            continue
        label = (getattr(shape, "master_label", None) or shape.id)
        if "HeavyPlate" in str(label):
            plate = poly
        else:
            small.append(poly)

    if plate is None or not small:
        hc.emit("could not isolate the corpus polygons")
        return

    hc.emit("=== geometry under test (real corpus polygons) ===")
    hc.emit(f"  HeavyPlate   {len(plate.exterior.coords):5d} exterior vertices, "
            f"{len(plate.interiors):3d} holes, area {plate.area:,.0f}")
    for i, poly in enumerate(small):
        hc.emit(f"  small[{i}]      {len(poly.exterior.coords):5d} exterior vertices, "
                f"{len(poly.interiors):3d} holes, area {poly.area:,.0f}")

    _case("touching: small part just clear of the plate edge", plate, small,
          "touch")
    _case("overlapping: small part pushed into the plate", plate, small,
          "overlap")
    _case("disjoint: small part well away", plate, small, "disjoint")


def _case(title, plate, small, mode):
    """Build a real pair set in the requested relationship, then time the three
    ways of answering 'do these overlap by more than a tolerance'."""
    from shapely.affinity import translate

    rng = random.Random(4242)
    pairs = []
    for _ in range(4000):
        ex = small[rng.randrange(len(small))]
        if mode == "disjoint":
            # Far away from the plate's bounds.
            off = (plate.centroid.x - ex.centroid.x + 5000.0,
                   plate.centroid.y - ex.centroid.y)
        elif mode == "touch":
            # Push the small part until its bounds just meet the plate's. The
            # NFP guarantees candidates land exactly here, which is the case
            # that is 100% of this corpus's overlay calls.
            minx, miny, maxx, maxy = plate.bounds
            emin, eminy, emax, emaxy = ex.bounds
            off = (minx - emin + 0.0, plate.centroid.y - ex.centroid.y)
            off = (off[0], miny - (ex.centroid.y - eminy))
        else:  # overlap
            off = (plate.centroid.x - ex.centroid.x,
                   plate.centroid.y - ex.centroid.y)
        cand = translate(ex, xoff=off[0], yoff=off[1])
        pairs.append((plate, cand))

    truth = sum(1 for a, b in pairs if b.intersection(a).area > 1e-7)

    def bench(fn):
        samples = []
        for _ in range(REPS):
            started = time.perf_counter()
            hits = 0
            for a, b in pairs:
                if fn(a, b):
                    hits += 1
            samples.append((time.perf_counter() - started) * 1000)
        return min(samples), hits

    overlay_ms, overlay_hits = bench(lambda a, b: b.intersection(a).area > 1e-7)
    pred_ms, pred_hits = bench(
        lambda a, b: a.overlaps(b) or a.contains(b) or b.contains(a))
    inter_ms, _ = bench(lambda a, b: a.intersects(b))

    n = len(pairs)
    hc.emit("")
    hc.emit(f"=== {title} ===")
    hc.emit(f"  pairs {n:,}   true positive-area overlaps: {truth:,}")
    hc.emit(f"  intersects()            {inter_ms / 1000:8.3f} s  "
            f"{1000.0 * inter_ms / n:6.1f} us/pair")
    hc.emit(f"  intersection().area     {overlay_ms / 1000:8.3f} s  "
            f"{1000.0 * overlay_ms / n:6.1f} us/pair   hits {overlay_hits:,}")
    hc.emit(f"  overlaps+contains       {pred_ms / 1000:8.3f} s  "
            f"{1000.0 * pred_ms / n:6.1f} us/pair   hits {pred_hits:,}")
    agree = (truth == overlay_hits == pred_hits)
    hc.emit(f"  all three agree: {agree}")
    if agree and overlay_ms > 0:
        hc.emit(f"  predicates are {overlay_ms / max(pred_ms, 1e-9):.2f}x the "
                f"overlay cost  -> net saving on the overlay portion: "
                f"{100.0 * (1 - pred_ms / overlay_ms):+.1f}%")
    else:
        hc.emit("  <-- disagreement; the predicate form is not equivalent here")


if __name__ in ("__main__", "probe_collision_overlay"):
    main()
