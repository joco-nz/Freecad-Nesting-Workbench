"""Freecadcmd check that flattening preserves geometry and topology.

The pytest tier covers flattening's logic against stand-ins, but it cannot
answer the question that matters most here: does the geometry actually survive?

A stand-in has no surfaces, so it cannot tell you that flattening turned a
`Cylinder` into a `BSplineSurface`. That is the failure this check exists for.
`transformGeometry` performs exactly that conversion, and it is what
`cam_manager` currently uses to place parts for CAM. Since the lead-in and
lead-out arc maths are computed against the real surface, a splined cylinder
means the toolpath follows an approximation of what the user drew -- silently,
with no error anywhere.

Topology matters for a second reason. An operation's `Base` stores
sub-element names (`Face1`, `Edge7`), so those names have to keep addressing
the same features after flattening. That is what lets the source operation's
recipe be replayed against every copy.

Also checked here, because it is a real behaviour of the module rather than a
theoretical one:

  * the combined placement is `container * child`, and order-sensitive;
  * Z normalisation puts each part's bottom at `-thickness` and is idempotent;
  * every flattened part carries a `SourceObject` link, so matching never
    compares labels.

Run directly, or via tests/freecad_harness/run.sh. Writes
`.last_status_replay`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_replay")

import FreeCAD
import Part

from freecad.nestingworkbench.Tools.Cam import cam_replay

_failures = []
_checks = [0]


def emit(message=""):
    """Print a report line in a way that survives FreeCAD's console redirect.

    Under freecadcmd, FreeCAD.Console captures plain print() once a document
    exists, so check output silently disappears. The first version of this
    script used print() and reported nothing at all while still writing a
    failing status file.
    """
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


def check(condition, message):
    _checks[0] += 1
    if not condition:
        _failures.append(message)


def surface_types(shape):
    """Return the sorted set of surface type names on a shape's faces."""
    return sorted({type(face.Surface).__name__ for face in shape.Faces})


def build_nested(doc, label, cx, cy, angle, source_shape, child_rotation=None):
    """Build the workbench's real structure: a container holding a part."""
    shapes_group = doc.addObject("App::DocumentObjectGroup", "Shapes_1")

    container = doc.addObject("App::Part", "nested_" + label)
    shapes_group.addObject(container)

    part = doc.addObject("Part::Feature", "part_" + label)
    shape = source_shape.copy()
    shape.Placement = FreeCAD.Placement()
    part.Shape = shape
    if child_rotation is not None:
        # A child placement is real: the up-direction rotation applied when the
        # master shape was built.
        part.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 0), child_rotation)
    container.addObject(part)

    container.Placement = FreeCAD.Placement(
        FreeCAD.Vector(cx, cy, 0), FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), angle)
    )
    doc.recompute()
    return container, part


def run():
    doc = FreeCAD.newDocument("replay_flatten")

    # A cylinder, so the surface type is unambiguous: analytic, and
    # transformGeometry would visibly change it.
    source = doc.addObject("Part::Feature", "Bracket")
    source.Shape = Part.makeCylinder(10, 6)
    doc.recompute()
    source_surfaces = surface_types(source.Shape)
    check("Cylinder" in source_surfaces,
          "expected the source to have cylinder faces, got %s" % source_surfaces)
    source_edge_count = len(source.Shape.Edges)
    source_edge_lengths = sorted(round(e.Length, 9) for e in source.Shape.Edges)

    thickness = 6.0
    # Realistic placements only. The nester rotates about Z, and the
    # up-direction rotation is baked into the master shape so parts arrive
    # Z-aligned. A child rotation about X or Y would tip a part onto its side,
    # which is not a thing the nesting side produces.
    specs = [
        # label,          cx,   cy,  angle, child yaw
        ("Bracket_1", 0.0, 0.0, 0.0, 0.0),
        ("Bracket_2", 60.0, 12.0, 37.0, 0.0),
        ("Bracket_3", 0.0, 60.0, 71.0, 25.0),
    ]

    flattened = []
    for label, cx, cy, angle, child_yaw in specs:
        container, part = build_nested(
            doc, label, cx, cy, angle, source.Shape,
            FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), child_yaw) if child_yaw else None,
        )
        result = cam_replay.flatten_container(doc, container, thickness)
        check(result is not None, "%s: flatten_container returned None" % label)
        if result is None:
            continue
        flattened.append((label, container, part, result))

    check(len(flattened) == len(specs),
          "expected %d flattened parts, got %d" % (len(specs), len(flattened)))
    if not flattened:
        return

    # -- the links are in scope --
    #
    # A plain App::PropertyLink from a top-level feature to something inside
    # the layout's App::Part containers is out of scope, and FreeCAD warns on
    # every recompute. `_add_link_property` uses PropertyXLink precisely to
    # avoid that, so assert the property type rather than trusting the absence
    # of a warning in this run's console output.
    for label, _container, _part, result in flattened:
        for prop in (cam_replay.PROP_SOURCE_OBJECT, cam_replay.PROP_SOURCE_CONTAINER):
            type_id = result.obj.getTypeIdOfProperty(prop)
            check(type_id == "App::PropertyXLink",
                  "%s: %s is %s, expected App::PropertyXLink (a plain Link into "
                  "an App::Part is out of scope and warns on every recompute)"
                  % (label, prop, type_id))

    # -- geometry survived --
    for label, container, part, result in flattened:
        shape = result.obj.Shape
        got = surface_types(shape)
        check("BSplineSurface" not in got,
              "%s: geometry was degraded to BSpline (%s); flattening must use "
              "Placement, not transformGeometry" % (label, got))
        check(got == source_surfaces,
              "%s: surface types changed %s -> %s" % (label, source_surfaces, got))
        check(len(shape.Edges) == source_edge_count,
              "%s: edge count changed %d -> %d"
              % (label, source_edge_count, len(shape.Edges)))
        lengths = sorted(round(e.Length, 9) for e in shape.Edges)
        check(lengths == source_edge_lengths,
              "%s: per-edge lengths changed" % label)

    # -- combined placement is container * child, in that order --
    #
    # Only meaningful when the child actually carries a rotation AND the
    # container sits off that rotation's axis. With an identity child the two
    # products are identical, and a rotation about Y leaves a point already on
    # the Y axis where it was -- both of which made an earlier version of this
    # check pass vacuously, or fail for the wrong reason.
    order_checked = 0
    for label, container, part, result in flattened:
        expected = cam_replay.combined_placement(container, part)
        got = result.obj.Shape.Placement
        check(abs(got.Base.x - expected.Base.x) < 1e-6
              and abs(got.Base.y - expected.Base.y) < 1e-6,
              "%s: XY placement (%s, %s) != expected (%s, %s)"
              % (label, got.Base.x, got.Base.y, expected.Base.x, expected.Base.y))

        if part.Placement.Rotation.Angle == 0.0:
            continue
        order_checked += 1
        reversed_product = part.Placement.multiply(container.Placement)
        same = (abs(got.Base.x - reversed_product.Base.x) < 1e-6
                and abs(got.Base.y - reversed_product.Base.y) < 1e-6)
        check(not same,
              "%s: placement (%s, %s) also matches the REVERSED product (%s, %s); "
              "the multiplication order is not being honoured"
              % (label, got.Base.x, got.Base.y,
                 reversed_product.Base.x, reversed_product.Base.y))
    check(order_checked > 0,
          "no part carried a child rotation, so multiplication order went unchecked")

    # -- Z normalisation --
    #
    # For a Z-aligned part the convention is exact: bottom at -thickness, top
    # at 0, so the stock spans -thickness..0 and Z0 is its top face. That is the
    # frame the operations resolve their depths against.
    #
    # Deliberately NOT asserted: that a part with a rotation about X or Y would
    # also land with its top at 0. It would not, and it should not -- a tilted
    # part has no single "top" to align, and forcing one would shear the
    # geometry rather than place it. The nesting side only ever rotates about
    # Z, so the tilted case does not arise in practice.
    for label, container, part, result in flattened:
        bb = result.obj.Shape.BoundBox
        check(abs(bb.ZMin + thickness) < 1e-6,
              "%s: bottom at Z=%s, expected %s" % (label, bb.ZMin, -thickness))
        check(abs(bb.ZMax) < 1e-6,
              "%s: top at Z=%s, expected 0 (Z0 = top of stock)"
              % (label, bb.ZMax))
        check(abs((bb.ZMax - bb.ZMin) - thickness) < 1e-6,
              "%s: Z extent is %s, expected the %s sheet thickness"
              % (label, bb.ZMax - bb.ZMin, thickness))

    # idempotent: a second flatten of the same geometry must not sink it
    for label, container, part, result in flattened:
        again = cam_replay.z_offset_for_thickness(result.obj.Shape, thickness)
        check(abs(again) < 1e-9,
              "%s: re-normalising moved it by %s; the offset is not idempotent"
              % (label, again))

    # -- identity is a link, not a label --
    for label, container, part, result in flattened:
        obj = result.obj
        check(hasattr(obj, cam_replay.PROP_SOURCE_OBJECT),
              "%s: no %s property" % (label, cam_replay.PROP_SOURCE_OBJECT))
        check(obj.SourceObject is part,
              "%s: %s does not point at the source part"
              % (label, cam_replay.PROP_SOURCE_OBJECT))
        check(obj.SourceContainer is container,
              "%s: %s does not point at the source container"
              % (label, cam_replay.PROP_SOURCE_CONTAINER))
        check(obj.NestedLabel == container.Label,
              "%s: NestedLabel is %r" % (label, obj.NestedLabel))

    # -- the layout itself was not disturbed --
    for label, container, part, result in flattened:
        check(part.Shape.Placement.Base.x != result.obj.Shape.Placement.Base.x
              or container.Placement.Base.x == 0.0,
              "%s: the source part appears to have been moved" % label)
        check(not result.obj.Shape is part.Shape,
              "%s: flattened geometry is the source shape, not a copy" % label)

    # -- a container with no part child is reported, not raised --
    empty = doc.addObject("App::Part", "nested_Empty")
    empty_group = doc.addObject("App::DocumentObjectGroup", "Shapes_2")
    empty_group.addObject(empty)
    doc.recompute()
    check(cam_replay.flatten_container(doc, empty, thickness) is None,
          "a container with no part_* child should flatten to None")

    # -- report text renders --
    result = cam_replay.FlattenResult()
    for _label, _container, _part, item in flattened:
        result.parts.append(item.obj)
        if item.z_offset:
            result.z_shifts.append((item.obj, item.z_offset))
    lines = cam_replay.describe_flatten(result)
    check(all(isinstance(line, str) for line in lines),
          "describe_flatten returned non-strings")
    check(any("Flattened" in line for line in lines),
          "describe_flatten did not report a part count")

    FreeCAD.closeDocument("replay_flatten")


if __name__ in ("__main__", "test_replay_flatten"):
    status = 0
    try:
        run()
    except Exception:
        traceback.print_exc()
        _failures.append("raised")
    emit("replay flatten: %d checks, %d failure(s)" % (_checks[0], len(_failures)))
    for failure in _failures:
        emit("  FAIL: %s" % failure)
    if _failures:
        status = 1
    try:
        with open(_STATUS_FILE, "w") as handle:
            handle.write(str(status))
    except OSError:
        pass
    emit("REPLAY_FLATTEN_STATUS=%d" % status)
    sys.exit(status)
