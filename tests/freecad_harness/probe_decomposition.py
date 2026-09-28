"""How many convex pieces does each corpus part actually decompose into?

The heavy corpus is designed around one dial: a plate drilled with a grid of
holes should decompose into roughly one convex piece per hole, because that is
what makes n70's Spacer expensive -- 229 pieces, so 192x192 = 36,864 convex
pairs for a single Spacer-against-Spacer NFP.

That is an assumption about the decomposition, and it was wrong on the first
measurement: the heavy plate produced 5,880 total convex pairs where n70
produces 516,144, roughly 100x less. The hole grid is not the dial it was
assumed to be. This probe measures the piece count directly so the corpus gets
designed against a number instead of a hope.

It goes through `ShapePreparer.prepare_parts` rather than converting faces
itself, because the conversion and the discretisation settings are part of what
is being measured -- a hand-rolled conversion would answer a different question.

Run: $FREECADCMD tests/freecad_harness/probe_decomposition.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_common as hc  # noqa: E402

SETTINGS = {
    "spacing": 5.0, "deflection": 0.05, "simplification": 0.1,
    "rotation_steps": 8, "add_labels": False, "font_path": "",
    "verbose": False,
}


def prepared_polygons(doc, parts, quantity=1):
    """The polygons the nest would actually decompose, via the real preparer."""
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer

    layout = doc.addObject("App::DocumentObjectGroup", "Probe_layout")
    parts_group = doc.addObject("App::DocumentObjectGroup", "Probe_parts")
    shared = doc.addObject("App::DocumentObjectGroup", "Probe_masters")
    quantities = {
        p.Label: {"quantity": quantity, "rotation_steps": 8,
                  "up_direction": "Z+", "fill_sheet": False}
        for p in parts
    }
    preparer = ShapePreparer(doc, {}, create_doc_objects=False, master_pool={},
                             shared_master_group=shared)
    shapes = preparer.prepare_parts(SETTINGS, quantities,
                                    {p.Label: p for p in parts}, layout,
                                    parts_group)
    return [getattr(s, "original_polygon", None) for s in shapes]


def main():
    import FreeCAD
    from freecad.nestingworkbench.Tools.Nesting.algorithms import minkowski_utils as mu

    doc = FreeCAD.newDocument("probe_decomp")

    n70 = os.path.join(_REPO, "tests", "Test_Files",
                       "n70-intercooler-spacer-bottle-nesting.FCStd")
    corpora = [("heavy", hc.build_heavy_corpus(doc, 20260925, 1)[0])]
    if os.path.exists(n70):
        corpora.append(("n70", hc.discover_doc_parts(FreeCAD.openDocument(n70))))
    else:
        hc.emit("n70 corpus not present; skipping the reference row")

    for name, parts in corpora:
        hc.emit(f"=== {name} ===")
        polygons = prepared_polygons(doc, parts)
        for part, polygon in zip(parts, polygons):
            if polygon is None or polygon.is_empty:
                hc.emit(f"  {part.Label:14s} no polygon")
                continue
            pieces = mu.decompose_if_needed(polygon, logger=None)
            count = len(pieces) if pieces is not None else 0
            holes = len(polygon.interiors)
            hc.emit(f"  {part.Label:14s} area {part.Shape.Area:11.1f}  "
                    f"holes {holes:4d}  -> {count:5d} convex pieces"
                    f"   self-pairs {count * count:>12,d}")
        hc.emit("")


if __name__ in ("__main__", "probe_decomposition"):
    main()
