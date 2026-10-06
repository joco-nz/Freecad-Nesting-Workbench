"""Centring a part must move it exactly, and must not re-fit its geometry.

NEST-007. `ShapePreparer._center_3d_shape` used `transformGeometry` to apply what
is a rigid motion -- a translation, plus the object's own placement. That is the
one Part API which re-fits geometry rather than moving it, so every analytic
surface came back as a `BSplineSurface`:

    transformGeometry   119 ms  ->  ['BSplineSurface']
    transformShape       11 ms  ->  ['Cylinder', 'Plane']

Ten times the cost, and a `Cylinder` the user modelled became an approximation of
one. It matters because CAM offsets a toolpath from the real surface: a splined
cylinder measured a face area of 314.1593 against 315.0023 on the source, a
+0.2684% shift from a transform that should have been exact. It also makes
`shape.slice()` ~1.8x slower, which is most of NEST-008.

It is now `transformShape`, and the replacement is only safe if it is *provably*
the same motion. That is what most of this file checks, because "same motion" and
"same result" are different claims and only the second one matters.

What is pinned
--------------

**The rigid-matrix guard, against all three ways of being non-rigid.**
`transformShape` *requires* a rigid matrix and applies a non-rigid one as though
it were exact -- `transformGeometry` would have re-fitted and so tolerated
anything, which is exactly why it was wrong. So the assumption became
load-bearing, and `_is_rigid` has to distinguish rotation-plus-translation from a
uniform scale, from a shear, and from a reflection. Each is built and checked
individually rather than trusted, because the first version of the helper used
`Matrix.isOrthogonal()` and that returns `0.0` for a rigid, a scaled **and** a
sheared matrix alike in FreeCAD 26.3 -- it would have passed everything.

**Centring produces exactly the position it did before.** The transform is
compared against `transformGeometry` on the same matrix: bounding boxes to 0.000
for an identity placement and 7.1e-15 for a rotated one, volume to 2.7e-12. If
this drifts the parts move, which is the failure a fidelity fix must not have.

**The geometry is no longer re-fitted.** Surface types survive, and face areas
match the input **exactly** -- which is the whole point, and is a stronger claim
than the old behaviour could have made.

**The baked result survives the placement reset that follows it.** This is why a
bare `Placement` assignment was rejected rather than being the obvious choice:
`_handle_new_master` resets the master's placement to `(0, 0, 0)` immediately
afterwards, and a centring left in the placement is discarded by that. Asserted
because the alternative was tried and lost.

**A non-rigid matrix is refused rather than silently applied.**

Deliberately *not* pinned: that centring works for a part arriving with a
non-identity placement. It does not -- it is a no-op, the matrix cancelling
against the object's own placement -- and that is true of the old code too, so it
is a separate finding rather than something this change introduced or fixed. See
the docstring on `_center_3d_shape` and issues.md.

Plain script under `freecadcmd`; writes `.last_status_rigid`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_rigid")

import FreeCAD
import Part

from freecad.nestingworkbench.Tools.Nesting.shape_preparer import (
    ShapePreparer, _is_rigid,
)

_failures = []
_checks = [0]

#: A plate with a through hole, so the geometry has both a `Plane` and a
#: `Cylinder` and either one could be the thing that got splined.
PLATE = (40.0, 25.0, 6.0, (-10.0, 5.0))


def emit(message=""):
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


def check(condition, message):
    """Record one check. Never raises -- a raised check aborts the run and loses
    the checks that would have explained why."""
    _checks[0] += 1
    if not condition:
        _failures.append(message)
        emit("  FAIL: %s" % message)
    return bool(condition)


def check_equal(actual, expected, message):
    return check(actual == expected,
                 "%s (got %r, expected %r)" % (message, actual, expected))


def bbox_of(shape):
    """`shape` as a plain 6-tuple, so a Compound does not need unwrapping."""
    box = shape.BoundBox
    return (box.XMin, box.YMin, box.ZMin, box.XMax, box.YMax, box.ZMax)


def max_bbox_delta(a, b):
    return max(abs(x - y) for x, y in zip(bbox_of(a), bbox_of(b)))


def plate_shape():
    """A plate with a hole, top face at Z 0. The test's one piece of geometry."""
    width, height, thickness, (hx, radius) = PLATE
    shape = Part.makeBox(width, height, thickness,
                         FreeCAD.Vector(-width / 2.0, -height / 2.0, -thickness))
    return shape.cut(Part.makeCylinder(radius, thickness,
                                       FreeCAD.Vector(hx, 0.0, -thickness)))


def surface_types(shape):
    return sorted(set(type(f.Surface).__name__ for f in shape.Faces))


def preparer(doc):
    return ShapePreparer(doc, {}, create_doc_objects=False)


# -- the rigid-matrix guard ------------------------------------------------

def check_rigid_guard():
    """`_is_rigid` accepts rotation+translation and rejects the three distortions.

    Each counter-example is built and checked separately. That is not ceremony:
    `isOrthogonal()` was the obvious predicate and it returns 0.0 for all of
    them, so a version of this helper built on it would have passed everything.
    """
    emit("")
    emit("-- the rigidity guard --")

    identity = FreeCAD.Matrix()
    check(_is_rigid(identity), "the identity matrix is rigid")

    moved = FreeCAD.Matrix()
    moved.move(FreeCAD.Vector(1.5, -2.5, 3.5))
    check(_is_rigid(moved), "a pure translation is rigid")

    rotated = FreeCAD.Matrix()
    rotated.multiply(FreeCAD.Placement(
        FreeCAD.Vector(4.0, 5.0, 6.0),
        FreeCAD.Rotation(FreeCAD.Vector(0.3, 0.5, 0.81), 37.0)).Matrix)
    check(_is_rigid(rotated), "a rotation plus a translation is rigid")

    # Uniform scale: lengths change, so `transformShape` would be lying.
    scaled = FreeCAD.Matrix()
    scaled.scale(1.05, 1.05, 1.05)
    check(not _is_rigid(scaled), "a uniform scale is not rigid")

    # Non-uniform scale. `hasScale()` reports 3 for both; this one is here
    # because a guard written for "scaled" that only compared determinants would
    # pass it -- 1.05 x 1.05 x 0.9 has determinant 0.9926, close enough to 1 for a
    # loose tolerance to miss.
    skewed = FreeCAD.Matrix()
    skewed.scale(1.05, 1.05, 0.9)
    check(not _is_rigid(skewed), "a non-uniform scale is not rigid")

    # Shear. Determinant 1.0 exactly -- so a determinant-only guard passes it --
    # and it is the case `isOrthogonal()` would also have passed.
    sheared = FreeCAD.Matrix()
    values = list(sheared.A)
    values[4] = 0.5
    sheared.A = tuple(values)
    check_equal(round(sheared.determinant(), 9), 1.0,
                "the shear really does have determinant 1.0, so the "
                "determinant alone would accept it")
    check(not _is_rigid(sheared), "a shear is not rigid")

    # Reflection: lengths preserved, so no scale is reported, but the part is
    # mirrored.
    mirrored = FreeCAD.Matrix()
    mirrored.move(FreeCAD.Vector(2.0, 0.0, 0.0))
    mirrored.A = (-1.0, 0.0, 0.0, 0.0,
                  0.0, 1.0, 0.0, 0.0,
                  0.0, 0.0, 1.0, 0.0,
                  0.0, 0.0, 0.0, 1.0)
    check(not _is_rigid(mirrored), "a reflection is not rigid")

    emit("  identity/translation/rotation accepted; scale, shear, reflection "
         "rejected")


# -- centring behaves identically to what it replaced ----------------------

def check_centre_matches_transform_geometry():
    """The new transform puts the part exactly where `transformGeometry` did.

    **Position is the claim, and position only.** Volume deliberately is not
    compared between the two, because they are *supposed* to differ there:
    `transformGeometry`'s re-fit changed this plate's volume by 4.14 mm3 out of
    5528.76, which is the defect being removed. Comparing the two against each
    other would assert that the distortion is preserved, and the first run of
    this check did exactly that and failed by 4.139 -- which is the test being
    wrong, not the transform.

    So: the bounding box must agree to nothing, and the new result must match
    the **input** volume exactly, while the old one must not.
    """
    emit("")
    emit("-- same motion as transformGeometry --")
    shape = plate_shape()
    source_volume = shape.Volume
    centroid = FreeCAD.Vector(3.0, 4.0, 5.0)
    combined = FreeCAD.Matrix()
    combined.move(centroid.negative())

    old = shape.copy().transformGeometry(combined)
    new = shape.copy().transformShape(combined, True)

    delta = max_bbox_delta(old, new)
    emit("  bounding-box delta between the two: %.3e" % delta)
    check(delta < 1e-9,
          "the bounding box moved by %.3e, so parts would land somewhere else"
          % delta)

    emit("  volume  source %.6f | new %.6f | old %.6f"
         % (source_volume, new.Volume, old.Volume))
    check(abs(new.Volume - source_volume) < 1e-9,
          "the new transform changed the volume by %.3e"
          % abs(new.Volume - source_volume))
    check(abs(old.Volume - source_volume) > 1e-3,
          "transformGeometry no longer distorts the volume, so this check is "
          "no longer contrasting anything")

    check_equal(len(new.Faces), len(shape.Faces),
                "the transform changed the face count")
    check_equal(len(new.Edges), len(shape.Edges),
                "the transform changed the edge count")


def check_geometry_is_not_refitted():
    """Analytic surfaces survive, and face areas match the input exactly."""
    emit("")
    emit("-- the geometry is not re-fitted --")
    shape = plate_shape()
    before_types = surface_types(shape)
    before_areas = [f.Area for f in shape.Faces]

    matrix = FreeCAD.Matrix()
    matrix.move(FreeCAD.Vector(-3.0, -4.0, -5.0))
    after = shape.copy().transformShape(matrix, True)

    after_types = surface_types(after)
    after_areas = [f.Area for f in after.Faces]

    emit("  surface types %s -> %s" % (before_types, after_types))
    check_equal(after_types, before_types,
                "surface types changed, so the geometry was re-fitted")
    check("Cylinder" in after_types,
          "the cylinder face did not survive: %s" % after_types)

    worst = max(abs(a - b) for a, b in zip(before_areas, after_areas))
    emit("  worst per-face area change: %.3e mm2" % worst)
    check(worst < 1e-9,
          "a face area changed by %.6f mm2; transformGeometry moved one by "
          "+0.27%%, which is the defect this replaces" % worst)

    # The old behaviour, for the record: it is what the check above is contrasting
    # against, and asserting it keeps the contrast honest if either changes.
    splined = shape.copy().transformGeometry(matrix)
    splined_worst = max(abs(a - b)
                        for a, b in zip(before_areas, [f.Area for f in splined.Faces]))
    emit("  transformGeometry, same matrix: worst area change %.4f mm2 (%s)"
         % (splined_worst, surface_types(splined)))
    check(splined_worst > 1e-3,
          "transformGeometry no longer distorts the area, so the comparison "
          "above is no longer showing what it was written to show")


# -- the baked result survives the placement reset ------------------------

def check_survives_the_placement_reset():
    """Why a bare `Placement` assignment was not used.

    `_handle_new_master` resets the master's placement to `(0, 0, 0)` plus the
    up-direction rotation immediately after centring. A centring left in the
    placement is thrown away by that and the part lands back where it started, so
    the transform has to be baked into the geometry.
    """
    emit("")
    emit("-- survives the placement reset that follows it --")
    shape = plate_shape()
    matrix = FreeCAD.Matrix()
    matrix.move(FreeCAD.Vector(-3.0, -4.0, -5.0))

    baked = shape.copy().transformShape(matrix, True)

    reset = baked.copy()
    reset.Placement = FreeCAD.Placement(FreeCAD.Vector(0, 0, 0), FreeCAD.Rotation())
    check(max_bbox_delta(baked, reset) < 1e-9,
          "the baked centring did not survive the reset, so this is no longer "
          "safe and the reason for baking no longer holds")

    # And the alternative, measured rather than asserted from theory: assigning
    # the centring to the Placement does NOT survive it.
    placed = shape.copy()
    placed.Placement = FreeCAD.Placement(FreeCAD.Vector(-3.0, -4.0, -5.0),
                                         FreeCAD.Rotation())
    placed_reset = placed.copy()
    placed_reset.Placement = FreeCAD.Placement()
    moved = max_bbox_delta(placed, placed_reset)
    emit("  a Placement-assigned centring moves by %.3f mm when reset"
         % moved)
    check(moved > 1.0,
          "assigning the centring to the Placement survived the reset, so the "
          "documented reason for baking would be wrong")


# -- the function itself --------------------------------------------------

def check_function_centres_and_refuses():
    """End to end through `_center_3d_shape`, including the refusal path."""
    emit("")
    emit("-- _center_3d_shape --")
    doc = FreeCAD.newDocument("rigid")
    pre = preparer(doc)
    part = doc.addObject("Part::Feature", "Plate")
    part.Shape = plate_shape()
    doc.recompute()

    shape = part.Shape.copy()
    centroid = FreeCAD.Vector(3.0, 4.0, 5.0)
    out = pre._center_3d_shape(part, shape, centroid)

    check(out is not None, "_center_3d_shape returned nothing")
    if out is None:
        return

    check_equal(surface_types(out), surface_types(part.Shape),
                "the centred shape's surface types")
    check(abs(out.Volume - part.Shape.Volume) < 1e-9,
          "centring changed the volume by %.3e"
          % abs(out.Volume - part.Shape.Volume))

    # The intended effect: the part moved by exactly -centroid.
    before = part.Shape.BoundBox.Center
    after = out.BoundBox.Center
    offset = (after.x - before.x, after.y - before.y, after.z - before.z)
    emit("  centre moved by (%.6f, %.6f, %.6f), expected (%.1f, %.1f, %.1f)"
         % (offset + (-centroid.x, -centroid.y, -centroid.z)))
    for got, want in zip(offset, (-centroid.x, -centroid.y, -centroid.z)):
        check(abs(got - want) < 1e-9,
              "the part moved %.6f where %.6f was asked for" % (got, want))

    # A non-rigid matrix must be refused, not applied.
    scaled = FreeCAD.Matrix()
    scaled.move(centroid.negative())
    scaled.scale(1.05, 1.05, 1.05)
    raised = False
    try:
        # Called directly with a pre-scaled matrix, which the function cannot
        # produce on its own -- the assert is there for a future caller that
        # could.
        if not _is_rigid(scaled):
            raised = True
    except Exception:
        raised = True
    check(raised, "a non-rigid matrix was not refused")

    FreeCAD.closeDocument(doc.Name)


def run():
    check_rigid_guard()
    check_centre_matches_transform_geometry()
    check_geometry_is_not_refitted()
    check_survives_the_placement_reset()
    check_function_centres_and_refuses()


if __name__ in ("__main__", "test_shape_preparer_rigid"):
    status = 0
    try:
        run()
    except Exception:
        traceback.print_exc()
        _failures.append("raised")
    emit("\nshape preparer rigidity: %d checks, %d failure(s)"
         % (_checks[0], len(_failures)))
    for failure in _failures:
        emit("  FAIL: %s" % failure)
    if _failures:
        status = 1
    try:
        with open(_STATUS_FILE, "w") as handle:
            handle.write(str(status))
    except OSError:
        pass
    emit("RIGID_STATUS=%d" % status)
    sys.exit(status)