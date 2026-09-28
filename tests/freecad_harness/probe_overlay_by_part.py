"""Does the body-only overlay scale to the parts that dominate the run?

The Spacer probe showed dropping the interior rings makes the overlay 3.0x
cheaper with identical verdicts. But the n70 configuration is 2 Spacer + 60
Bottle Top + 60 Bottle Bottom, so most of the 368,954 overlays per run are
bottle-bottle -- small parts, few rings, where body-only is close to a no-op.
Measuring only the Spacer would report a 3x speedup on the 1.6% of the work that
is expensive and say nothing about the 98.4% that is cheap.

So: all three real part types, self-vs-self, which is the dominant pairing.
"""
import sys
import time
import traceback

sys.path.insert(0, "/home/james/dev/Freecad-Nesting-Workbench")
_LOG = open("/tmp/opencode/probe4.txt", "w")


def e(msg):
    _LOG.write(str(msg) + "\n")
    _LOG.flush()


def run():
    import numpy as np
    import FreeCAD
    from shapely import affinity
    from shapely.geometry import Polygon

    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer

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

    tol = 1e-7
    nested = [("Spacer  (Pad)", polys["Pad"]),
              ("BottleTop", polys["Pad001"]),
              ("BottleBot", polys["Pad002"])]

    def body(g):
        return Polygon(g.exterior)

    def bench(fn, reps=7):
        out = []
        for _ in range(reps):
            t = time.perf_counter()
            fn()
            out.append((time.perf_counter() - t) * 1e6)
        return min(out)

    e("=== the three nested part types, as the n70 configuration uses them ===")
    e("  %-14s %10s %7s %8s %6s" % ("part", "area mm2", "holes", "ext verts", "rings"))
    for name, g in nested:
        e("  %-14s %10.0f %7d %8d %6d"
          % (name, g.area, len(g.interiors), len(g.exterior.coords),
             1 + len(g.interiors)))

    e("\n=== overlay cost, full vs exterior-ring only, self-vs-self ===")
    e("  %-14s %11s %11s %9s %11s" % ("part", "full", "body-only", "speedup",
                                      "full/pair"))
    summary = []
    for name, g in nested:
        width = g.bounds[2] - g.bounds[0]
        offsets = [0.0, -1e-7, -1e-6, -1e-5, -1e-4, -1e-3, -1e-2, -1e-1,
                   -0.25, -0.5]
        cs = [affinity.translate(g, width + d, 0.0) for d in offsets]
        cs = [c for c in cs if c.intersects(g)]
        if len(cs) < 3:
            e("  %-14s  (only %d overlapping samples)" % (name, len(cs)))
            continue
        cbs = [body(c) for c in cs]
        gb = body(g)
        a_full = bench(lambda: [c.intersection(g).area for c in cs])
        a_body = bench(lambda: [c.intersection(gb).area for c in cbs])
        i_full = bench(lambda: [c.intersects(g) for c in cs])
        i_body = bench(lambda: [c.intersects(gb) for c in cbs])
        agree = sum(1 for c, cb in zip(cs, cbs)
                    if (c.intersection(g).area > tol)
                    == (cb.intersection(gb).area > tol))
        e("  %-14s %10.1fus %10.1fus %8.1fx %10.1fus  (n=%d, %d/%d verdicts agree)"
          % (name, a_full, a_body, a_full / max(1e-9, a_body),
             a_full / len(cs), len(cs), agree, len(cs)))
        e("  %-14s intersects %8.1fus -> %8.1fus  (%.1fx)"
          % ("", i_full, i_body, i_full / max(1e-9, i_body)))
        summary.append((name, len(g.interiors), a_full, a_body))

    e("\n=== weighted by what the n70 configuration actually nests ===")
    e("  2 Spacer + 60 Bottle Top + 60 Bottle Bottom = 122 parts.")
    e("  Pairs are dominated by bottle-bottle, so the weighted speedup is much")
    e("  closer to the bottles' figure than the Spacer's 3.0x.")
    if summary:
        by = {n: (h, af, ab) for n, h, af, ab in summary}
        wt_full = wt_body = 0.0
        for name, count in (("Spacer  (Pad)", 2), ("BottleTop", 60),
                            ("BottleBot", 60)):
            key = name.split()[0].strip("(").rstrip(")") if False else name
            rec = by.get(key)
            if rec is None:
                continue
            h, af, ab = rec
            wt_full += af * count
            wt_body += ab * count
        if wt_body > 0:
            e("  weighted by part count: %.1fus -> %.1fus  = %.2fx"
              % (wt_full, wt_body, wt_full / wt_body))
        for name, h, af, ab in summary:
            e("  %-14s %d holes -> %d holes dropped, %.1fus -> %.1fus"
              % (name, h, h, af, ab))


try:
    run()
except Exception:
    e(traceback.format_exc())
_LOG.close()
