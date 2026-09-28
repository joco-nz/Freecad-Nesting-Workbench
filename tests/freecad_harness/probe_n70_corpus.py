import os
import sys

sys.path.insert(0, "/home/james/dev/Freecad-Nesting-Workbench")
import FreeCAD  # noqa: E402

sys.path.insert(0, "/home/james/dev/Freecad-Nesting-Workbench/tests/freecad_harness")
import harness_common as hc  # noqa: E402

doc = FreeCAD.openDocument(
    "/home/james/dev/Freecad-Nesting-Workbench/tests/Test_Files/"
    "n70-intercooler-spacer-bottle-nesting.FCStd")
hc.emit(f"objects: {len(doc.Objects)}")
for o in doc.Objects:
    has = bool(hasattr(o, "Shape") and o.Shape and not o.Shape.isNull())
    area = 0.0
    if has:
        try:
            area = o.Shape.Area
        except Exception:
            pass
    hc.emit(f"  {o.Name:26s} {o.TypeId:32s} {o.Label:24s} shape={has} area={area:.1f}")

tops = hc.discover_doc_parts(doc)
hc.emit("")
hc.emit(f"top-level parts: {len(tops)}")
for p in tops:
    hc.emit(f"  {p.Name:26s} {p.Label:24s} area={p.Shape.Area:.1f} "
            f"bbox={p.Shape.BoundBox}")
