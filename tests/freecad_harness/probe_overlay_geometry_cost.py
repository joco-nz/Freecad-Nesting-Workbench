"""Why the overlay costs 157us, and whether the holes are the reason.

Established so far, on the real n70 Spacer (117,773 mm2, 13 holes, 110 exterior
verts, 14 rings total), face to face:

    intersects()            123 us/pair
    intersection().area    1636 us/pair     <- 13.3x the predicate
    overlaps or contains    124 us/pair
    shapely.prepare(...)   123 -> 121 us     <- does nothing, and nothing for an overlay

So preparation is not the lever. The hypothesis this tests is that ring count is:
GEOS must walk all 14 rings for both the predicate and the constructive overlay,
and 13 of those rings are holes, which cannot change the verdict of an overlap
test. A candidate inside a hole is not overlapping the part's material.

The holes DO matter for internal fit, but that is a separate, separately counted
path (hole_exploiting_placements, hole_sensitive_pairs). So testing exterior-ring
geometry should be both far cheaper and unchanged in meaning -- and unlike a
predicate swap it needs no tolerance argument to be safe.
"""
import sys
import time
import traceback

sys.path.insert(0, "/home/james/dev/Freecad-Nesting-Workbench")
_LOG = open("/tmp/opencode/probe3.txt", "w")


def e(msg):
    _LOG.write(str(msg) + "\n")
    _LOG.flush()


def run():
    import numpy as np
    import shapely
    from shapely import affinity
    from shapely.geometry import Polygon

    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    import FreeCAD

    n70 = ("/home/james/dev/Freecad-Nesting-Workbench/tests/Test_Files/"
           "n70-intercooler-spacer-bottle-nesting.FCStd")
    doc = FreeCAD.openDocument(n70)
    objs = [o for o in doc.Objects
            if o.TypeId not in ("App::Origin", "App::Line", "App::Plane", "App::Point")
            and hasattr(o, "Shape") and o.Shape and not o.Shape.isNull()
            and any(getattr(c, "Name", None) == o.Name for other in doc.Objects
                    if other.Name != o.Name
                    for c in (getattr(other, "Group", None) or []))]
    lay = doc.addObject("App::DocumentObjectGroup", "L")
    pgs = doc.addObject("App::DocumentObjectGroup", "P")
    mst = doc.addObject("App::DocumentObjectGroup", "M")
    ui = {"spacing": 5.0, "deflection": 0.05, "simplification": 0.1,
          "rotation_steps": 8, "add_labels": False, "font_path": "", "verbose": False}
    qty = {p.Label: {"quantity": 1, "rotation_steps": 8, "up_direction": "Z+",
                     "fill_sheet": False} for p in objs}
    pre = ShapePreparer(doc, {}, create_doc_objects=False, master_pool={},
                        shared_master_group=mst)
    shapes = pre.prepare_parts(ui, qty, {p.Label: p for p in objs}, lay, pgs)
    polys = {}
    for s in shapes:
        p = getattr(s, "original_polygon", None)
        if p is not None and not p.is_empty:
            polys[str(getattr(s, "master_label", None) or s.id)] = p
    part = max(polys.values(), key=lambda g: len(g.interiors))
    tol = 1e-7

    e("part: %.0f mm2, %d holes, %d exterior verts, %d rings"
      % (part.area, len(part.interiors), len(part.exterior.coords),
         1 + len(part.interiors)))
    width = part.bounds[2] - part.bounds[0]

    def body(g):
        return Polygon(g.exterior)

    fixed = part
    fixed_body = body(fixed)

    def cand(dx):
        return affinity.translate(part, width + dx, 0.0)

    # ---- verdict agreement, across the whole overlap range ---------------
    e("\n=== does dropping the holes change the accept/reject verdict? ===")
    agree = total = rejected_by_body_only = 0
    for d in np.linspace(-width * 0.05, 2.0, 400):
        c = cand(float(d))
        if not c.intersects(fixed):
            continue
        total += 1
        full_v = c.intersection(fixed).area > tol
        body_v = body(c).intersection(fixed_body).area > tol
        if full_v == body_v:
            agree += 1
        else:
            rejected_by_body_only += (not full_v) and body_v
    e("  %d/%d agree over a %.1f mm sweep" % (agree, total, width * 0.05 + 2.0))
    e("  disagreements: %d, of which body-only rejected and full accepted: %d"
      % (total - agree, rejected_by_body_only))
    e("  the unsafe direction is a body-only REJECT that full ACCEPTS, which")
    e("  would let parts overlap. The count above is that count: %d"
      % rejected_by_body_only)

    # ---- the same, on the grazing band specifically -----------------------
    e("\n=== and in the grazing band, where the tolerance decides ===")
    band = [0.0, -1e-8, -1e-7, -5e-7, -1e-6, -5e-6, -1e-5, -5e-5, -1e-4, -1e-3]
    e("  %16s %14s %14s %8s %8s" % ("dx (mm)", "area full", "area body", "full>tol", "body>tol"))
    b_agree = 0
    for d in band:
        c = cand(d)
        if not c.intersects(fixed):
            e("  %16.10f %14s" % (d, "disjoint"))
            continue
        af = c.intersection(fixed).area
        ab = body(c).intersection(fixed_body).area
        fv, bv = af > tol, ab > tol
        b_agree += (fv == bv)
        e("  %16.10f %14.3e %14.3e %8s %8s" % (d, af, ab, fv, bv))
    e("  %d/%d agree in the band" % (b_agree, len(band)))

    # ---- cost ------------------------------------------------------------
    offsets = [0.0, -1e-7, -1e-6, -1e-5, -1e-4, -1e-3, -1e-2, -1e-1, -0.5, -1.0]
    cs = [cand(d) for d in offsets if cand(d).intersects(fixed)]
    cbs = [body(c) for c in cs]

    def bench(fn, reps=7):
        out = []
        for _ in range(reps):
            t = time.perf_counter()
            fn()
            out.append((time.perf_counter() - t) * 1e6)
        return min(out)

    e("\n=== cost per call: full geometry vs exterior-ring only (min of 7) ===")
    e("  n = %d pairs" % len(cs))
    e("  %-26s %11s %11s %9s" % ("", "full", "body-only", "speedup"))
    for label, fn_full, fn_body in (
            ("intersects()",
             lambda: [c.intersects(fixed) for c in cs],
             lambda: [c.intersects(fixed_body) for c in cbs]),
            ("intersection().area",
             lambda: [c.intersection(fixed).area for c in cs],
             lambda: [c.intersection(fixed_body).area for c in cbs]),
            ("overlaps or contains",
             lambda: [c.overlaps(fixed) or fixed.contains(c) for c in cs],
             lambda: [c.overlaps(fixed_body) or fixed_body.contains(c) for c in cbs])):
        a = bench(fn_full)
        b = bench(fn_body)
        e("  %-26s %10.1fus %10.1fus %8.1fx" % (label, a, b, a / max(1e-9, b)))

    e("\n=== what the cache pays to build a body-only geometry ===")
    t = time.perf_counter()
    for _ in range(20):
        for c in cs:
            body(c)
    e("  Polygon(exterior): %.1f us per geometry, built once and reused for every"
      % ((time.perf_counter() - t) * 1e6 / (20 * len(cs))))
    e("  collision test it serves, so it amortises immediately.")

    # ---- and the same question for a holeless part, as a control ----------
    holeless = Polygon(part.exterior)
    e("\n=== control: a part with no holes at all ===")
    e("  %d exterior verts, 1 ring" % len(holeless.exterior.coords))
    cs_h = [affinity.translate(holeless, holeless.bounds[2] - holeless.bounds[0] + d, 0.0)
            for d in offsets]
    cs_h = [c for c in cs_h if c.intersects(holeless)]
    a = bench(lambda: [c.intersection(holeless).area for c in cs_h])
    e("  intersection().area: %.1f us/pair over %d pairs" % (a, len(cs_h)))
    e("  a holeless part of the same outline costs the same as the body-only")
    e("  form of the holed one, which is the whole claim.")


try:
    run()
except Exception:
    e(traceback.format_exc())
_LOG.close()
