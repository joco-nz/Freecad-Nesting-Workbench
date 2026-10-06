"""Does the CAM replay carry a Path operation's StartPoint onto the nested part?

A user sets up CAM on their own parts, in the CAM workbench, **before** the
nesting ever runs -- that is the whole premise of `cam_replay` (see its module
docstring). `StartPoint` is one of the settings they set there: an absolute
`App::PropertyVectorDistance` saying where on the contour the feed should start,
which decides which wire is cut first and which vertex the lead-in lands on.
`Path.Op.Area` passes it straight through as `pathParams["start"]`
(`Path/Op/Area.py:282-283` and `:378-379`), so it is honoured exactly.

**It used not to be carried.** `capture_properties` took it verbatim, like any
other `App::Property*`, and `apply_properties` assigned it verbatim; meanwhile
`flatten_container` rigidly moved the geometry it was chosen against. The
replayed point still read in the source part's frame. Measured, before the fix:

    source job, plate at (35, -12) yaw 15, top face Z 0
        StartPoint                       (0.8458, -0.4461, 0.0)    0.000000 mm from its own contour

    nested copy 1, container at (140, 110) yaw 0, child yaw 12
        carried to                     (0.8458, -0.4461, 0.0)    141.963 mm from the part
        error                       162.7319 mm
        contour began at (114.814, 84.200) instead of (106.497, 123.326)  40.0000 mm
        G0 [ X:0.846 Y:-0.446 ] -- off the sheet, which measures Y 0..400

    nested copy 2, container at (430, 260) yaw 37
        carried to                     (0.8458, -0.4461, 0.0)    470.453 mm from the part
        error                       470.4529 mm
        contour began at (394.005, 257.918), which is RIGHT -- 0.0000 mm

That last row is why this file asserts on the **property** and only reports the
toolpath. The stale point is resolved to the *nearest point on the wire*, and a
rectangle's four corners are interchangeable under its own symmetry, so copy 2
came out right by accident. A toolpath-based assertion would have passed on half
the copies for entirely the wrong reason, and would be right by luck on any
symmetric part.

What it checks now
------------------
1. the point is carried, and lands on the copy's own contour;
2. `verify_replay` **notices when it is not** -- a check that has only ever passed
   is indistinguishable from one that does not work;
3. the committed fixture's one real user-set start point is carried, on real
   geometry rather than this fixture's;
4. the case with no single transform is **reported, not guessed** -- the same
   reasoning as NEST-009's `Stock`, which is wrong and said so rather than
   patched.

The transform it is checked against
-----------------------------------
`clone.Shape.Placement * inverse(source_geometry.Shape.Placement)`, because
`flatten_container` folds the container placement, the part's own placement, the
Z normalisation and the sheet-origin shift into the flattened object's placement,
and a nested copy is the same underlying geometry rigidly moved (module docstring
constraints 5 and 6).

`Shape.Placement` rather than `Placement`: `PathJob.Create` puts a
`draftobjects.clone.Clone` in the Model, and Draft's Clone restores
`obj.Placement` *after* assigning a Shape that already carries the original's
placement (`draftobjects/clone.py:122-148`), so the two are not guaranteed to
agree.

**Every factor of it is non-identity in this fixture**, so it cannot be right by
accident -- a transform missing the source-side inverse fails six checks:

    factor                                    exercised by
    -----------------------------------------  ---------------------------
    container placement (nester's position)    both copies
    container rotation (nester's angle)        copy 2
    child placement (up-direction rotation)    copy 1
    source entry placement (the user's part)   the plate itself

Run directly, or via tests/freecad_harness/run.sh. Writes
`.last_status_startpoint`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_startpoint")

FIXTURE = os.path.join(_REPO, "tests", "Test_Files",
                       "replay-fixture-CAM-Nested.FCStd")

import FreeCAD
import Part

from freecad.nestingworkbench.Tools.Cam import cam_replay

_failures = []
_checks = [0]

THICKNESS = 6.0

#: Set False to assert the pre-fix behaviour. It exists so that anyone who
#: reverts the replay sees *why* this file goes red, in the message rather than
#: in a diff -- and so the state this file describes is one greppable line rather
#: than an inference from the assertions.
START_POINT_IS_COPIED_VERBATIM = False


def emit(message=""):
    """Print a report line in a way that survives FreeCAD's console redirect.

    Under freecadcmd, FreeCAD.Console captures plain print() once a document
    exists, so report output silently disappears.
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
        emit("  FAIL: %s" % message)
    return bool(condition)


# -- the fixture ----------------------------------------------------------
#
# A plate with a through hole, deliberately asymmetric: the hole is off-centre,
# so the inner wire's vertices are not interchangeable with the outer wire's and
# a wrong start point cannot accidentally pick the right one. Seated at
# -THICKNESS..0, so `z_offset_for_thickness` is a no-op in the replay and the
# source and the clone share a Z frame. That isolates XY, which is what the
# start point actually selects -- see `probe` notes on Z below.

PLATE_W, PLATE_H = 60.0, 40.0
HOLE_R, HOLE_X, HOLE_Y = 10.0, -12.0, 0.0

#: The source plate's own placement. Non-identity on purpose: a user's part does
#: not sit on the world origin, they put it somewhere, and with the plate at the
#: origin `source_to_clone_placement`'s `inverse(source.Placement)` half is
#: multiplied by an identity and a correct transform is indistinguishable from a
#: correct-by-accident one that only handles the clone's side.
SOURCE_PLACEMENT = (FreeCAD.Vector(35.0, -12.0, 0.0), 15.0)


def build_plate(doc):
    shape = Part.makeBox(PLATE_W, PLATE_H, THICKNESS,
                         FreeCAD.Vector(-PLATE_W / 2, -PLATE_H / 2, -THICKNESS))
    shape = shape.cut(Part.makeCylinder(
        HOLE_R, THICKNESS + 2, FreeCAD.Vector(HOLE_X, HOLE_Y, -THICKNESS - 1)))
    plate = doc.addObject("Part::Feature", "Plate")
    plate.Shape = shape
    base, yaw = SOURCE_PLACEMENT
    plate.Placement = FreeCAD.Placement(
        base, FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), yaw))
    doc.recompute()
    return plate


def top_face_names(target):
    """The single planar face at the top of `target`, as a sub-element name."""
    faces = target.Shape.Faces
    ztop = max(f.CenterOfMass.z for f in faces)
    names = ["Face%d" % (i + 1) for i, f in enumerate(faces)
             if type(f.Surface).__name__ == "Plane"
             and abs(f.CenterOfMass.z - ztop) < 1e-7]
    return names, ztop


def distance_to_shape(point, shape, z=None):
    """Distance in mm from a point to a shape. 0.0 when it is on the shape.

    `z` overrides the point's Z before measuring, which is how the XY-only
    figures here are taken: a carried start point sits at the operation's
    clearance height, not on the contour, so a 3D distance measures the approach
    height rather than anything about the cut.
    """
    point = FreeCAD.Vector(point)
    if z is not None:
        point = FreeCAD.Vector(point.x, point.y, z)
    return shape.distToShape(Part.Vertex(point))[0]


def first_cut_xy(operation):
    """The XY of an operation's first cutting move, or None."""
    for command in operation.Path.Commands:
        if cam_replay.is_cutting_motion(cam_replay.motion_of(command)):
            x, y = getattr(command, "X", None), getattr(command, "Y", None)
            if x is not None and y is not None:
                return (x, y)
    return None


def xy_moves(operation):
    """Every XY the toolpath visits, rapids included."""
    return [(getattr(c, "X", None), getattr(c, "Y", None))
            for c in operation.Path.Commands
            if getattr(c, "X", None) is not None]


def expected_start_point(clone, source_entry, start_point, target_op=None):
    """Where `start_point` should land on `clone`.

    XY by the rigid motion. Z from `target_op`'s own `ClearanceHeight`, which is
    what `CommandSetStartPoint` writes and what the replay does -- see
    `carried_geometry_frame_points` for the measurement behind it. Passing
    `target_op=None` gives the pure rigid motion, XY *and* Z.
    """
    transform = cam_replay.source_to_clone_placement(clone, source_entry)
    if transform is None:
        return None
    point = transform.multVec(FreeCAD.Vector(start_point))
    if target_op is not None and hasattr(target_op, "ClearanceHeight"):
        point = FreeCAD.Vector(point.x, point.y,
                               float(target_op.ClearanceHeight.Value))
    return point


def build_source_job(doc, plate, start_point):
    """A user's own CAM job, built the way a user builds one.

    `UseComp = False` on purpose, for two measured reasons.

    **A compensated profile of a face lying exactly on the stock's top face
    degenerates to a line.** That is NEST-014, it is unresolved, and it has
    nothing to do with the start point -- it would put an unrelated failure in the
    middle of this measurement.

    **Compensation moves the first cutting move off the StartPoint.** The cut runs
    on the offset wire, so the point that decides where to begin is the nearest
    point on the *offset*. Measured on this fixture, source operation:

        UseComp=False   first cutting move (0.8458, -0.4461)   == StartPoint
        UseComp=True    first cutting move (-1.5690, -1.0931)   1.91 mm off it

    1.91 mm is the default 5 mm endmill's radius projected onto the corner. The
    *selection* is the same either way -- and both arms replay the same way -- but
    only the uncompensated arm makes "the first cutting move equals the expected
    start point" an exact statement, which is what this file asserts to 1e-6.
    """
    from Path.Main import Job as PathJob
    from Path.Op import Profile as PathProfile

    FreeCAD.setActiveDocument(doc.Name)
    job = PathJob.Create("UserSetup", [plate], None)
    doc.recompute()
    entry = job.Model.Group[0]
    names, _ztop = top_face_names(entry)

    op = PathProfile.Create("Profile", None, job)
    doc.recompute()
    op.Base = [(entry, names)]
    op.HandleMultipleFeatures = "Individually"
    op.Side = "Outside"
    op.Direction = "CW"
    op.UseComp = False
    op.ToolController = job.Tools.Group[0]
    op.StartPoint = start_point
    op.UseStartPoint = True
    doc.recompute()
    return job, entry, op


def build_layout(doc, plate, specs):
    """One sheet holding `specs`, each a `(label, cx, cy, angle, child_yaw)` nest.

    `child_yaw` is the part's own placement inside the container -- the
    up-direction rotation `combined_placement` says a nested part carries. Real,
    and on copy 1 it is the only rotation in that copy's transform, so a
    transform that dropped the child placement would be caught here.
    """
    layout = doc.addObject("App::DocumentObjectGroup", "Layout_1")
    for name, value in (("SheetWidth", 700.0), ("SheetHeight", 400.0),
                        ("SheetThickness", THICKNESS)):
        layout.addProperty("App::PropertyLength", name, "Layout", "")
        setattr(layout, name, value)

    sheet = doc.addObject("App::DocumentObjectGroup", "Sheet_1")
    layout.addObject(sheet)
    shapes = doc.addObject("App::DocumentObjectGroup", "Shapes_1")
    sheet.addObject(shapes)
    # The real nester creates one of these per sheet and the replay reads the
    # sheet's world origin from it. Without it the parts are never moved to local
    # coordinates.
    boundary = doc.addObject("Part::Feature", "Sheet_Boundary_1")
    boundary.Shape = Part.makePlane(700, 400)
    sheet.addObject(boundary)

    for label, cx, cy, angle, child_yaw in specs:
        container = doc.addObject("App::Part", label)
        shapes.addObject(container)
        part = doc.addObject("Part::Feature", "part_" + label.split("_", 1)[1])
        shape = plate.Shape.copy()
        shape.Placement = FreeCAD.Placement()
        part.Shape = shape
        if child_yaw:
            part.Placement = FreeCAD.Placement(
                FreeCAD.Vector(0, 0, 0),
                FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), child_yaw))
        container.addObject(part)
        container.Placement = FreeCAD.Placement(
            FreeCAD.Vector(cx, cy, 0),
            FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), angle))
    doc.recompute()
    return layout


# -- the run --------------------------------------------------------------

def run():
    doc = FreeCAD.newDocument("replay_startpoint")

    plate = build_plate(doc)
    check(len(plate.Shape.Faces) > 1,
          "the fixture needs more than one face to have a meaningful contour")

    # A vertex of the top face's outer wire: a real point on a real contour
    # rather than an arbitrary spot.
    names, _ztop = top_face_names(plate)
    start_point = plate.Shape.getElement(names[0]).OuterWire.OrderedVertexes[0].Point
    job, entry, op = build_source_job(doc, plate, start_point)

    emit("source StartPoint: (%.4f, %.4f, %.4f)"
         % (start_point.x, start_point.y, start_point.z))
    emit("  distance from its own contour: %.6f mm"
         % distance_to_shape(start_point, entry.Shape))
    check(distance_to_shape(start_point, entry.Shape) < 1e-6,
          "the fixture's start point is not on the source contour, so the whole "
          "measurement would be meaningless")
    check(op.UseStartPoint is True, "the source operation does not use its start point")
    source_first = first_cut_xy(op)
    check(source_first is not None, "the source Profile produced no cutting move")

    # Two copies, and the second one rotated: a pure translation is the easy case,
    # and a bug that only shows on a translated part is not the bug. Copy 1 carries
    # a child placement instead of a container rotation, so the two copies
    # exercise different factors of the transform (see SOURCE_PLACEMENT).
    specs = [("nested_Plate_1", 140.0, 110.0, 0.0, 12.0),
             ("nested_Plate_2", 430.0, 260.0, 37.0, 0.0)]
    layout = build_layout(doc, plate, specs)

    outcomes = cam_replay.replay_layout(doc, layout, job)
    check(len(outcomes) == 1, "expected one outcome for one sheet, got %d" % len(outcomes))
    if not outcomes:
        return
    outcome = outcomes[0]
    check(outcome.ok is True, "the replay failed outright: %s" % outcome.errors)
    check(len(outcome.result.operations) == 2,
          "expected one operation per nested copy, got %d"
          % len(outcome.result.operations))
    if len(outcome.result.operations) != 2:
        return

    stock_box = outcome.replay_job.stock.Shape.BoundBox
    emit("stock: X %s..%s  Y %s..%s" % (stock_box.XMin, stock_box.XMax,
                                        stock_box.YMin, stock_box.YMax))

    rows = []
    for rop in outcome.result.operations:
        rows.append(check_one(doc, rop, entry, start_point, source_first, stock_box))

    emit("")
    emit("  %-34s %10s %10s %10s %10s" %
         ("operation", "err mm", "carried XY", "want XY", "start shift"))
    for row in rows:
        emit("  %-34s %10.4f %10.4f %10.4f %10.4f" % row)

    # Nothing is stranded, so neither the replay nor the verification says so.
    stranded = [w for w in outcome.result.warnings if "NEST-024" in w]
    check(not stranded,
          "the replay reported a stranded start point on a plain split step: %s"
          % stranded)
    if outcome.verification is not None:
        warned = [w for w in outcome.verification.warnings if "StartPoint" in w]
        check(not warned,
              "verify_replay warned about a start point that was carried "
              "correctly: %s" % warned)

    check_verification_bites(doc, outcome, start_point)
    check_unsplit_step_is_reported(doc)

    emit("")
    emit("carried: the source job's StartPoint rides the rigid motion onto each "
         "nested copy in XY, and takes its Z from the operation's own clearance "
         "height. Both XY figures are 0.0000 mm on every copy, and no copy's "
         "toolpath leaves the stock.")

    FreeCAD.closeDocument(doc.Name)


def check_one(doc, rop, entry, start_point, source_first, stock_box):
    """Assert one replayed operation's carried start point, and return its numbers."""
    label = rop.Label
    target = rop.Base[0][0]
    rigid = expected_start_point(target, entry, start_point)
    check(rigid is not None,
          "%s: the transform could not be formed" % label)
    if rigid is None:
        return (label, float("nan"), float("nan"), float("nan"), float("nan"))
    expected = expected_start_point(target, entry, start_point, rop)

    # -- the flag survives, and it must: the point is only read when it is on --
    check(rop.UseStartPoint is True,
          "%s: UseStartPoint did not survive the replay" % label)

    # The transform is validated independently of the carrying, on XY: if the
    # expected point did not land on the copy's own contour then the comparison
    # below would be a formula checked against itself. Measured at the contour's
    # own Z, because the expected Z is the operation's clearance height and the
    # contour is not up there. 0.000000 mm on both copies.
    contour_z = max(f.CenterOfMass.z for f in target.Shape.Faces)
    want_on_part = distance_to_shape(expected, target.Shape, z=contour_z)
    check(want_on_part < 1e-6,
          "%s: the transform does not put the start point on the nested part "
          "(%.6f mm off), so the error figure below would be meaningless"
          % (label, want_on_part))

    carried = FreeCAD.Vector(rop.StartPoint)
    error = carried.sub(expected).Length
    # XY only, at the contour's height. The carried Z is the operation's clearance
    # height by design, so a 3D distance here would measure 5.0000 mm of approach
    # height and say nothing about the cut.
    carried_on_part = distance_to_shape(carried, target.Shape, z=contour_z)

    # -- Z comes from the operation's own height, not from the transform --
    #
    # A separate assertion from the XY one above because the two halves can each
    # be right while the other is wrong, and because this fixture is chosen so
    # they *disagree*: the source's start point is at Z 0 while the operation's
    # clearance height is 6 mm, so a regression to a rigid Z lands 6 mm out and
    # is caught here rather than passing unnoticed.
    clearance = getattr(rop, "ClearanceHeight", None)
    check(clearance is not None,
          "%s: has no ClearanceHeight, so the Z rule cannot be checked" % label)
    if clearance is not None:
        check(abs(carried.z - float(clearance.Value)) < 1e-6,
              "%s: StartPoint.z is %.4f but the operation's own ClearanceHeight "
              "is %.4f. A Z above the operation's heights reaches the toolpath "
              "-- measured, 6.0 on the committed fixture added a level the "
              "user's job never had."
              % (label, carried.z, float(clearance.Value)))
        check(abs(rigid.z - float(clearance.Value)) > 1e-6,
              "%s: the rigid motion's Z and the clearance height agree, so this "
              "fixture no longer distinguishes the two rules for Z" % label)

    # START_POINT_IS_COPIED_VERBATIM
    if START_POINT_IS_COPIED_VERBATIM:
        check(error > 1.0,
              "%s: the start point is no longer copied verbatim -- it is only "
              "%.4f mm from the source frame. If this is the fix landing, set "
              "START_POINT_IS_COPIED_VERBATIM to False." % (label, error))
    else:
        check(error < 1e-6,
              "%s: the start point was not carried onto the nested part "
              "(%.4f mm off, expected %s)" % (label, error, expected))
        check(carried_on_part < 1e-6,
              "%s: the carried start point is %.4f mm from the part it names"
              % (label, carried_on_part))

    # -- and the consequence: the off-sheet rapid is gone --
    #
    # Asserted rather than reported, because this is the one symptom that was
    # *always* visible before the fix -- the stale coordinate went into the G-code
    # ahead of the real positioning move, off a sheet whose origin is a corner.
    moves = xy_moves(rop)
    outside = [(round(x, 3), round(y, 3)) for x, y in moves
               if not (stock_box.XMin - 1e-6 <= x <= stock_box.XMax + 1e-6
                       and stock_box.YMin - 1e-6 <= y <= stock_box.YMax + 1e-6)]
    if START_POINT_IS_COPIED_VERBATIM:
        check(len(outside) == 1,
              "%s: expected exactly one XY outside the stock -- the rapid to the "
              "stale start point -- got %d: %s" % (label, len(outside), outside))
    else:
        check(not outside,
              "%s: the toolpath leaves the stock (%s)" % (label, outside))

    # -- and the contour starts where the source's did --
    #
    # Exact here rather than reported, because with the point carried it is no
    # longer luck-dependent: the symmetry coincidence that hid the defect is gone
    # along with the defect.
    got_first = first_cut_xy(rop)
    want_first = cam_replay.source_to_clone_placement(
        target, entry).multVec(FreeCAD.Vector(source_first[0], source_first[1], 0))
    check(got_first is not None, "%s: no cutting move" % label)
    shift = (FreeCAD.Vector(got_first[0], got_first[1], 0)
             .sub(FreeCAD.Vector(want_first.x, want_first.y, 0)).Length)
    if START_POINT_IS_COPIED_VERBATIM:
        emit("  %s: contour begins at (%.3f, %.3f), should be (%.3f, %.3f) -- %.4f mm"
             % (label, got_first[0], got_first[1], want_first.x, want_first.y, shift))
    else:
        check(shift < 1e-6,
              "%s: the contour begins %.4f mm from where the source's began"
              % (label, shift))
        emit("  %s: contour begins at (%.3f, %.3f), as it should -- %.4f mm"
             % (label, got_first[0], got_first[1], shift))

    return (label, error, carried_on_part, want_on_part, shift)


def check_verification_bites(doc, outcome, stale_value):
    """`verify_replay` must notice a start point that is not on the part.

    A healthy replay passes cleanly, and a check that has only ever passed is
    indistinguishable from one that does not work. So: put the pre-fix value
    back on a replayed operation -- the source job's coordinate verbatim, which
    is exactly what the replay used to emit -- and confirm the verification says
    so, then restore it.

    This is the check that would have caught NEST-024, and it has to be shown to
    fire for the same reason the defect was silent: nothing else in
    `verify_replay` looks at the point.
    """
    victim = outcome.result.operations[0]
    saved = FreeCAD.Vector(victim.StartPoint)

    check(cam_replay.point_outside_targets(victim, "StartPoint") is False,
          "%s: a correctly carried start point reads as stranded" % victim.Label)

    try:
        victim.StartPoint = FreeCAD.Vector(stale_value)
        doc.recompute()
        check(cam_replay.point_outside_targets(victim, "StartPoint") is True,
              "a start point off the part does not read as stranded; the "
              "verification would never have caught NEST-024")
        result = cam_replay.ReplayResult()
        result.operations = list(outcome.result.operations)
        verification = cam_replay.verify_replay(result)
        warned = [w for w in verification.warnings if "StartPoint" in w]
        check(len(warned) == 1,
              "expected exactly one StartPoint warning, got %d: %s"
              % (len(warned), verification.warnings))
        if warned:
            check(victim.Label in warned[0],
                  "the warning does not name the operation: %s" % warned[0])
            check("nothing to do with this part" in warned[0],
                  "the warning does not say what is wrong with it: %s" % warned[0])
        emit("  verify_replay with the pre-fix value on %s:" % victim.Label)
        emit("    %s" % (warned[0] if warned else "(nothing -- check does not fire)"))
        # It warns, it does not fail: the cut still happens, just from the wrong
        # vertex. Only the user can decide whether that matters for their part.
        check(verification.ok is True,
              "a stranded start point failed the verification; it must only warn")
    finally:
        victim.StartPoint = saved
        doc.recompute()

    check(cam_replay.point_outside_targets(victim, "StartPoint") is False,
          "the victim was not restored")


def check_unsplit_step_is_reported(doc):
    """A step left whole over many parts is REPORTED, not guessed at.

    `UNSPLITTABLE_DRESSUPS` names dressups whose step must stay one operation
    covering every part it targets. One start point cannot be right for all of
    them -- there is no single frame to move it out of -- so the replay must say
    so rather than silently pick one. Same decision as NEST-009's `Stock`: wrong
    and reported beats wrong and quiet.

    **The tuple is empty as of NEST-009**, because `Path.Dressup.Boundary` was its
    only entry and Boundary is now unsupported rather than replayed. So this check
    puts Boundary back for the duration, to exercise the policy itself.

    That is deliberate and not a workaround: `split_is_safe` and the stranded
    report are product behaviour that a future dressup could reach, and the
    alternative -- deleting this check because nothing reaches it today -- would
    leave the only code that says "I could not carry this" untested, at exactly
    the moment someone is most likely to need it. The dressup is still dropped,
    so no Boundary object is built either way; only the *step* is marked
    unsplittable, which is what drives the whole-unit path.
    """
    from Path.Main import Job as PathJob
    from Path.Op import Profile as PathProfile
    from Path.Dressup.Boundary import DressupPathBoundary

    unsplit_doc = FreeCAD.newDocument("replay_startpoint_unsplit")
    thickness = THICKNESS

    plate = unsplit_doc.addObject("Part::Feature", "Plate")
    plate.Shape = Part.makeBox(60, 40, thickness,
                               FreeCAD.Vector(-30, -20, -thickness))
    unsplit_doc.recompute()

    FreeCAD.setActiveDocument(unsplit_doc.Name)
    job = PathJob.Create("UnsplitSource", [plate], None)
    unsplit_doc.recompute()
    entry = job.Model.Group[0]

    names, _ztop = top_face_names(entry)
    op = PathProfile.Create("Profile", None, job)
    unsplit_doc.recompute()
    op.Base = [(entry, names)]
    op.Side = "Outside"
    op.Direction = "CW"
    op.UseComp = False
    op.ToolController = job.Tools.Group[0]
    check(len(op.Base) >= 1, "the source operation has no base selection")
    op.StartPoint = plate.Shape.getElement(names[0]).OuterWire.OrderedVertexes[0].Point
    op.UseStartPoint = True
    unsplit_doc.recompute()

    dressup = unsplit_doc.addObject("Path::FeaturePython", "DressupPathBoundary")
    dressup.Proxy = DressupPathBoundary(dressup, op, job)
    # A solid, and deliberately NOT the sheet. Measured: Boundary clips with
    # `edge.common(shape)`, the sheet stock spans `-thickness .. 0` while the
    # contour is cut at Z 0, so the cut edge lies exactly on the stock's top face
    # and the common degenerates -- 0 cutting moves where there should be some.
    # That is the reason NEST-009's `Stock` could not simply be repointed at the
    # replay stock. Centred and spanning Z through 0, so the contour is inside
    # it. Kept even though the dressup is now dropped, because the step has to
    # carry one for `split_is_safe` to see it.
    stock = unsplit_doc.addObject("Part::Feature", "ClipSolid")
    stock.Shape = Part.makeBox(700.0, 400.0, 4,
                               FreeCAD.Vector(-350.0, -200.0, -2))
    unsplit_doc.recompute()
    dressup.Stock = stock
    dressup.Inside = True
    dressup.Offset = 0.0
    job.Proxy.addOperation(dressup, op)
    unsplit_doc.recompute()

    layout = unsplit_doc.addObject("App::DocumentObjectGroup", "Layout_1")
    for name, value in (("SheetWidth", 700.0), ("SheetHeight", 400.0),
                        ("SheetThickness", thickness)):
        layout.addProperty("App::PropertyLength", name, "Layout", "")
        setattr(layout, name, value)
    sheet = unsplit_doc.addObject("App::DocumentObjectGroup", "Sheet_1")
    layout.addObject(sheet)
    shapes = unsplit_doc.addObject("App::DocumentObjectGroup", "Shapes_1")
    sheet.addObject(shapes)
    boundary = unsplit_doc.addObject("Part::Feature", "Sheet_Boundary_1")
    boundary.Shape = Part.makePlane(700, 400)
    sheet.addObject(boundary)
    for n in (1, 2, 3):
        container = unsplit_doc.addObject("App::Part", "nested_Plate_%d" % n)
        shapes.addObject(container)
        part = unsplit_doc.addObject("Part::Feature", "part_Plate_%d" % n)
        s = plate.Shape.copy()
        s.Placement = FreeCAD.Placement()
        part.Shape = s
        container.addObject(part)
        container.Placement = FreeCAD.Placement(
            FreeCAD.Vector(120.0 + (n - 1) * 180.0, 200.0, 0),
            FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), 0.0))
    unsplit_doc.recompute()

    # **The tuple is patched for the replay and restored immediately.** Empty as
    # of NEST-009, so without this the step splits into three operations and
    # nothing reaches the whole-unit branch under test. Restored in a `finally`
    # because the rest of the run -- and every other harness file -- reads it.
    _saved_unsplittable = cam_replay.UNSPLITTABLE_DRESSUPS
    cam_replay.UNSPLITTABLE_DRESSUPS = ("Path.Dressup.Boundary",)
    try:
        outcomes = cam_replay.replay_layout(unsplit_doc, layout, job)
    finally:
        cam_replay.UNSPLITTABLE_DRESSUPS = _saved_unsplittable

    check(len(outcomes) == 1, "expected one outcome, got %d" % len(outcomes))
    if not outcomes:
        FreeCAD.closeDocument(unsplit_doc.Name)
        return
    check(cam_replay.UNSPLITTABLE_DRESSUPS == _saved_unsplittable,
          "the unsplittable tuple was not restored")
    result = outcomes[0].result
    if not result.operations:
        emit("  the unsplit step built nothing; failures: %s" % result.failures)
        emit("  dressups built: %s" % [d.Label for d in result.dressups])
    check(not result.failures,
          "the unsplit step failed to replay: %s" % result.failures)

    # One operation covering all three parts -- the unsplittable dressup. This is
    # also the assertion that the step really was left whole, so the branch under
    # test is reached rather than assumed; the earlier version probed
    # `split_is_safe` with a stand-in and was satisfied by a predicate it had fed
    # itself.
    check(len(result.operations) == 1,
          "expected the step left whole as 1 operation, got %d"
          % len(result.operations))
    if len(result.operations) == 1:
        check(len(result.operations[0].Base) == 3,
              "the whole operation should still cover all 3 parts, got %d"
              % len(result.operations[0].Base))
        check(cam_replay.has_cutting_motion(result.operations[0]),
              "the whole operation cut nothing, so this is not measuring a "
              "start point on a working toolpath")

    stranded = [w for w in result.warnings if "NEST-024" in w]
    check(len(stranded) == 1,
          "expected exactly one stranded-start-point report for the whole step, "
          "got %d: %s" % (len(stranded), result.warnings))
    if stranded:
        check("left whole" in stranded[0],
              "the report does not say why: %s" % stranded[0])
        check("Profile" in stranded[0],
              "the report does not name the operation: %s" % stranded[0])
        emit("  whole-step report: %s" % stranded[0])

        # And the value was left alone rather than moved to a random part.
        built = result.operations[0]
        check(distance_to_shape(built.StartPoint, entry.Shape) < 1e-6,
              "the whole step's start point was moved despite having no single "
              "transform; it should be left verbatim")

        # And the verification says so too, independently of the replay's warning.
        # Matched on the verification's own wording: `verify_replay` folds the
        # replay's own `result.warnings` in, so counting every warning mentioning
        # "StartPoint" would find that one too and this assertion would not be
        # about the verification at all.
        verification = cam_replay.verify_replay(result)
        warned = [w for w in verification.warnings
                  if "not on any part it targets" in w]
        check(len(warned) == 1,
              "verify_replay did not warn about the stranded start point: %s"
              % verification.warnings)

    FreeCAD.closeDocument(unsplit_doc.Name)


def run_fixture_checks():
    """The committed fixture, whose one real user-set start point must carry.

    Everything above is a fixture built for this file. This is a real job on real
    geometry, and it is the only check here that would catch a regression in the
    98-operation case where 97 operations have `UseStartPoint` False -- which is
    exactly the shape of the committed fixture, and exactly the shape in which a
    bug in the gating would hide.
    """
    if not os.path.exists(FIXTURE):
        emit("no fixture at %s -- skipping the committed-fixture check" % FIXTURE)
        return

    from freecad.nestingworkbench.Tools.Cam import replay_progress  # noqa: F401

    doc = FreeCAD.openDocument(FIXTURE)
    source_jobs = [o for o in doc.Objects if cam_replay.is_cam_job(o)]
    check(len(source_jobs) == 1,
          "expected one job in the fixture, got %d" % len(source_jobs))
    if not source_jobs:
        FreeCAD.closeDocument(doc.Name)
        return
    job = source_jobs[0]

    # Find the operations that actually read a start point, walking the dressup
    # stacks -- a dressed operation is not in `Operations`, it is reached
    # through its dressup's link.
    readers = []
    seen = set()
    for entry in getattr(job.Operations, "Group", []) or []:
        op, _dressups = cam_replay.walk_stack(entry)
        if op is None or id(op) in seen:
            continue
        seen.add(id(op))
        if cam_replay.unmapped_geometry_frame_points(op):
            readers.append(op)

    emit("fixture: %d operation(s) read a start point, of %d in the job"
         % (len(readers), len(seen)))
    check(len(readers) == 1,
          "the fixture was expected to hold exactly one operation reading a start "
          "point, got %d (%s). If the fixture changed, re-measure rather than "
          "trust this number." % (len(readers), [o.Label for o in readers]))
    if not readers:
        FreeCAD.closeDocument(doc.Name)
        return

    source_op = readers[0]
    source_point = FreeCAD.Vector(source_op.StartPoint)
    source_entry = source_op.Base[0][0]
    emit("  %s reads StartPoint %s" % (source_op.Label, source_point))
    # **Not on the part, and that is correct.** Measured 5.02 mm off its own
    # source part. A user picks a start point with the mouse near a feature, not
    # by snapping to a vertex, and CAM resolves it to the nearest point on the
    # wire. So the check here is that the point *moved with the part*, not that
    # it landed on a surface -- and it is why the production check tests against
    # the part's bounding box rather than a distance to its faces.
    source_offset = distance_to_shape(source_point, source_entry.Shape)
    emit("    %.4f mm from its own source part -- a user-placed point, not a "
         "snapped vertex" % source_offset)
    check(source_offset < 20.0,
          "the fixture's start point is %.4f mm from its own part, which is far "
          "enough that this check would not be measuring what it claims"
          % source_offset)

    layout, _warnings = cam_replay.resolve_layout_group(doc)
    check(layout is not None, "no layout was found in the fixture")
    if layout is None:
        FreeCAD.closeDocument(doc.Name)
        return

    outcomes = cam_replay.replay_layout(doc, layout, job)
    check(len(outcomes) >= 1, "the fixture replayed to nothing")
    if not outcomes:
        FreeCAD.closeDocument(doc.Name)
        return

    carried = 0
    total_copies = 0
    for outcome in outcomes:
        result = outcome.result
        if result is None:
            continue
        for rop in result.operations:
            if not cam_replay.geometry_frame_point_in_use(rop, "StartPoint"):
                continue
            total_copies += 1
            targets = [entry[0] for entry in (getattr(rop, "Base", None) or [])]
            if len(targets) != 1:
                continue
            expected = expected_start_point(targets[0], source_entry,
                                            source_point, rop)
            if expected is None:
                continue
            off = FreeCAD.Vector(rop.StartPoint).sub(expected).Length
            if off < 1e-6:
                carried += 1
                emit("    %-40s StartPoint=%s" % (
                    rop.Label, tuple(round(v, 3) for v in rop.StartPoint)))
            else:
                emit("  NOT CARRIED: %s is %.4f mm from where it should be "
                     "(%s, expected %s)"
                     % (rop.Label, off, rop.StartPoint, expected))
        # The other 96 operations must not produce a word about start points.
        if outcome.verification is not None:
            noise = [w for w in outcome.verification.warnings
                     if "not on any part it targets" in w]
            check(not noise,
                  "%s: verification warned about a carried start point: %s"
                  % (outcome.sheet_label, noise))

    emit("  %d of %d operation(s) reading a start point carried it exactly"
         % (carried, total_copies))
    check(total_copies > 0,
          "the fixture's replayed job holds no operation reading a start point, "
          "so the committed-fixture check did not run")
    check(carried == total_copies,
          "%d of %d copies failed to carry the fixture's start point"
          % (carried, total_copies))

    # And the replay is silent, because every step on the fixture is split and
    # each copy has its own transform.
    unsplit = [w for w in outcomes[0].result.warnings if "NEST-024" in w]
    check(not unsplit,
          "the committed fixture reported a stranded start point; every step on "
          "it is split, so no transform should be missing: %s" % unsplit)

    FreeCAD.closeDocument(doc.Name)


if __name__ in ("__main__", "test_replay_startpoint"):
    status = 0
    try:
        run()
        run_fixture_checks()
    except Exception:
        traceback.print_exc()
        _failures.append("raised")
    emit("replay start point: %d checks, %d failure(s)"
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
    emit("REPLAY_STARTPOINT_STATUS=%d" % status)
    sys.exit(status)
