"""Does a sub-element name mean the same feature on a nested copy as on the source?

`getElement` raises only when a name does not exist, so the replay's sub-element
check reported "ok" for a name that resolved to a completely different face --
NEST-027. A nested part with fewer faces than the source still answers `Face3`;
it is simply somebody else's. Measured there: `Face3` on a holed plate is the
886.9 mm2 top with two wires, and on a holeless box it is a 200.0 mm2 side with
one. The replay profiled the side as though it were the outline, and the only
symptom was a coverage warning about a path that collapsed to a line.

The pytest tier checks the comparison arithmetic against stand-ins. It cannot
show whether the numbers mean anything, which is the part that matters here: a
tolerance that is merely plausible on paper rejects every valid nest, and this
file is where that would show up. So every number here is measured on real
solids from the real pipeline.

The three things that have to be true, and are checked in order:

  1. **A genuine copy passes.** The same solid, rotated and moved by the nester.
     If this fails the check is unusable and no amount of catching matters.
  2. **The real pipeline's copies pass.** Not a hand-made copy -- the shapes
     `ShapePreparer.prepare_parts` actually produces, for all three part types
     of the committed fixture, on every face and every edge.
  3. **A different face fails.** NEST-014's geometry, driven through the real
     replay, which must leave the bad copy out and report it.

Run directly, or via tests/freecad_harness/run.sh. Writes
`.last_status_subnames`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_subnames")

_FIXTURE = os.path.join(_REPO, "tests", "Test_Files", "replay-fixture.FCStd")

import FreeCAD
import Part

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Nesting import shape_preparer

_failures = []
_checks = [0]

AREA_TOL = cam_replay.SUBNAME_AREA_TOLERANCE
LEN_TOL = cam_replay.SUBNAME_LENGTH_TOLERANCE


def emit(message=""):
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
        else:
            print(message)
    except Exception:
        pass


def check(condition, detail):
    _checks[0] += 1
    if not condition:
        _failures.append(detail)
        emit("  FAIL: %s" % detail)
    return bool(condition)


def ratio(delta, tolerance):
    """How much of the allowed tolerance a measured difference uses.

    Reported rather than asserted against a number of my own choosing: a check
    that sits at 0.1% of its tolerance and one at 99% of it are not the same
    check, and the reader should be able to see which this is.
    """
    return (delta / tolerance) if tolerance else float("inf")


# --------------------------------------------------------------------------
# 1. A genuine copy must pass
# --------------------------------------------------------------------------

def check_genuine_copy_passes():
    """The same solid, moved and rotated. This is what a nest actually makes."""
    emit("")
    emit("-- 1. a genuine rigid copy must not be refused --")
    plate = Part.makeBox(40, 25, 4.0, FreeCAD.Vector(-20, -12.5, -4.0)).cut(
        Part.makeCylinder(5, 4.0, FreeCAD.Vector(-10, 0, -4.0)))

    doc = FreeCAD.newDocument("sub_copy")
    src = doc.addObject("Part::Feature", "source")
    src.Shape = plate

    for label, (pos, yaw) in {
        "no move":      (FreeCAD.Vector(0, 0, 0), 0.0),
        "translated":   (FreeCAD.Vector(137.5, -22.25, 0), 0.0),
        "rotated 90":   (FreeCAD.Vector(10, 10, 0), 90.0),
        "rotated 37":   (FreeCAD.Vector(300, 12, 0), 37.0),
        "rotated 270":  (FreeCAD.Vector(-5, 88, 0), 270.0),
    }.items():
        clone = doc.addObject("Part::Feature", "copy_" + label.replace(" ", "_"))
        shape = plate.copy()
        shape.Placement = FreeCAD.Placement()
        clone.Shape = shape
        clone.Placement = FreeCAD.Placement(
            pos, FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), yaw))
        doc.recompute()

        names = ["Face%d" % (i + 1) for i in range(len(plate.Faces))]
        names += ["Edge%d" % (i + 1) for i in range(len(plate.Edges))]
        _missing, mismatched, detail = cam_replay.check_subnames_against_clones(
            names, [clone], src)

        worst_area = 0.0
        worst_len = 0.0
        for n in names:
            a = cam_replay.subelement_signature(plate, n)
            b = cam_replay.subelement_signature(clone.Shape, n)
            if a[0] == "Face":
                worst_area = max(worst_area, abs(a[1] - b[1]))
            else:
                worst_len = max(worst_len, abs(a[1] - b[1]))
        emit("  %-13s %d names  max|dArea| %.3e (%.2e of tol)  "
             "max|dLen| %.3e (%.2e of tol)"
             % (label, len(names), worst_area, ratio(worst_area, AREA_TOL),
                worst_len, ratio(worst_len, LEN_TOL)))
        check(not mismatched,
              "a genuine copy (%s) was reported as mismatched on %d name(s): %s"
              % (label, len(mismatched), sorted(lbl for n, lbl in mismatched)[:4]))
        check(not _missing,
              "a genuine copy (%s) lost names: %s" % (label, sorted(_missing)))
        check(all(v == "ok" for v in detail.values() if v),
              "%s did not report 'ok' everywhere: %s"
              % (label, {k: v for k, v in detail.items() if v != "ok"}))

    FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# 2. The real pipeline's copies must pass
# --------------------------------------------------------------------------

class _Rigid:
    """A clone-shaped wrapper around a shape moved the way a nest moves one."""

    def __init__(self, shape, yaw):
        moved = shape.copy()
        moved.Placement = FreeCAD.Placement(
            FreeCAD.Vector(120.5, -33.25, 0),
            FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), yaw))
        self.Label = "moved_yaw_%g" % yaw
        self.Shape = moved


def check_real_nesting_passes():
    """`prepare_parts`, not a hand-made copy.

    This is the check that says the tolerance is set from the pipeline rather
    than from a guess, and it is the only place the committed fixture's three
    part types are measured through the code that will be asked to accept them.
    """
    emit("")
    emit("-- 2. copies from the real ShapePreparer must not be refused --")
    if not os.path.exists(_FIXTURE):
        check(False, "the fixture is missing at %s" % _FIXTURE)
        return

    doc = FreeCAD.openDocument(_FIXTURE)
    bodies = [o for o in doc.Objects if o.TypeId.startswith("PartDesign::Body")]
    check(len(bodies) == 3,
          "expected the fixture's 3 bodies, found %d" % len(bodies))
    if not bodies:
        FreeCAD.closeDocument(doc.Name)
        return

    nest = FreeCAD.newDocument("sub_realnest")
    lay = nest.addObject("App::DocumentObjectGroup", "L")
    pgs = nest.addObject("App::DocumentObjectGroup", "P")
    mst = nest.addObject("App::DocumentObjectGroup", "M")
    ui = {"spacing": 5.0, "deflection": 0.05, "simplification": 0.1,
          "rotation_steps": 8, "add_labels": False, "font_path": "", "verbose": False}
    qty = {p.Label: {"quantity": 1, "rotation_steps": 8, "up_direction": "Z+",
                     "fill_sheet": False} for p in bodies}
    pre = shape_preparer.ShapePreparer(nest, {}, create_doc_objects=True,
                                       master_pool={}, shared_master_group=mst)
    pre.prepare_parts(ui, qty, {p.Label: p for p in bodies}, lay, pgs)
    nest.recompute()

    masters = {o.Label[len("master_shape_"):]: o for o in nest.Objects
               if getattr(o, "Label", "").startswith("master_shape_")}
    check(len(masters) == len(bodies),
          "expected %d master shapes, found %d" % (len(bodies), len(masters)))

    worst_area = 0.0
    worst_len = 0.0
    for body in bodies:
        master = masters.get(body.Label)
        if not check(master is not None,
                     "no master shape for %s" % body.Label):
            continue
        names = ["Face%d" % (i + 1) for i in range(len(body.Shape.Faces))]
        names += ["Edge%d" % (i + 1) for i in range(len(body.Shape.Edges))]
        # The same rigid motion the nester will apply, at several angles. The
        # source is the body the user's job was built on; the copy is the master
        # shape the nester produced, moved.
        for yaw in (0.0, 37.0, 90.0, 270.0):
            copy = _Rigid(master.Shape, yaw)
            _missing, mismatched, detail = \
                cam_replay.check_subnames_against_clones(names, [copy], body)
            check(not mismatched,
                  "%s at yaw %.0f: %d of %d names reported as a different "
                  "feature: %s" % (body.Label, yaw, len(mismatched),
                                   len(names),
                                   sorted(n for n, _l in mismatched)[:6]))
            bad = {k: v for k, v in detail.items() if v != "ok"}
            check(not bad, "%s at yaw %.0f: detail not 'ok': %s"
                  % (body.Label, yaw, bad))
            for n in names:
                a = cam_replay.subelement_signature(body.Shape, n)
                b = cam_replay.subelement_signature(copy.Shape, n)
                if a[0] == "Face":
                    worst_area = max(worst_area, abs(a[1] - b[1]))
                elif a[0] == "Edge":
                    worst_len = max(worst_len, abs(a[1] - b[1]))
        emit("  %-14s %d faces, %d edges checked at 4 angles"
             % (body.Label, len(body.Shape.Faces), len(body.Shape.Edges)))

    emit("  worst area difference  %.3e mm2  = %.2e of the %.0e tolerance"
         % (worst_area, ratio(worst_area, AREA_TOL), AREA_TOL))
    emit("  worst length difference %.3e mm   = %.2e of the %.0e tolerance"
         % (worst_len, ratio(worst_len, LEN_TOL), LEN_TOL))
    check(ratio(worst_area, AREA_TOL) < 1e-2,
          "the real pipeline's face areas use %.2e of the tolerance, which is "
          "not the headroom the tolerance was chosen for"
          % ratio(worst_area, AREA_TOL))
    check(ratio(worst_len, LEN_TOL) < 1e-2,
          "the real pipeline's edge lengths use %.2e of the tolerance"
          % ratio(worst_len, LEN_TOL))

    FreeCAD.closeDocument(nest.Name)
    FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# 3. A different face must fail
# --------------------------------------------------------------------------

def check_different_face_is_caught():
    """NEST-014's geometry: a holed plate and a holeless box, same names."""
    emit("")
    emit("-- 3. a name that means something else must be refused --")
    doc = FreeCAD.newDocument("sub_mismatch")
    thickness = 5.0
    plate = Part.makeBox(40, 25, thickness,
                         FreeCAD.Vector(0, 0, 0)).cut(
        Part.makeCylinder(6, thickness, FreeCAD.Vector(10, 12.5, 0)))
    box = Part.makeBox(40, 25, thickness, FreeCAD.Vector(0, 0, 0))

    src = doc.addObject("Part::Feature", "source_plate")
    src.Shape = plate
    box_obj = doc.addObject("Part::Feature", "part_holeless_1")
    box_obj.Shape = box
    good = doc.addObject("Part::Feature", "part_real_1")
    good.Shape = plate
    doc.recompute()

    emit("  source has %d faces, the holeless copy has %d"
         % (len(plate.Faces), len(box.Faces)))
    for n in ("Face1", "Face2", "Face3", "Face4", "Face5", "Face6"):
        try:
            a = cam_replay.subelement_signature(plate, n)
        except Exception:
            continue
        b = cam_replay.subelement_signature(box, n)
        if b is None:
            continue
        emit("    %s  area %9.4f vs %9.4f  wires %d vs %d  edges %2d vs %2d"
             % (n, a[1], b[1], a[2], b[2], a[3], b[3]))

    _missing, mismatched, detail = cam_replay.check_subnames_against_clones(
        ["Face3"], [box_obj, good], src)
    emit("  Face3 across a holeless copy and a real one: %s"
         % sorted(detail.items()))
    check(mismatched == {("Face3", "part_holeless_1")},
          "the holeless copy was not the only one flagged: %s"
          % sorted(mismatched))
    check(_missing == set(),
          "Face3 was reported missing rather than mismatched: %s" % sorted(_missing))
    # `detail` is the per-name roll-up and deliberately carries counts only --
    # one name is checked against different source geometry for different
    # operations, so a label in here could be the wrong copy's. The label
    # belongs in `mismatched` (above) and in the failure message (below).
    check("1 of 2" in detail["Face3"] and "same feature on 1" in detail["Face3"],
          "the per-name report does not separate agreeing from disagreeing "
          "copies: %s" % detail["Face3"])

    # **Face2 is the case where AREA is the only discriminator.** The table
    # above shows it: 200.0 against 125.0 with the same one wire and the same
    # four edges on both. That is the mistake NEST-027 actually describes --
    # profiling a side face as though it were the outline -- and it is invisible
    # to wire and edge counts alone.
    #
    # This assertion exists because of an injection: zeroing the area in
    # `subelement_signature`, keeping the counts, left the whole file green.
    # Face3 caught the missing hole by its wire count and carried the check on
    # its own, so nothing in the suite was testing the area at all. Found by
    # trying to break the test, which is the only way it could have been found.
    _m2, mismatched2, detail2 = cam_replay.check_subnames_against_clones(
        ["Face2"], [box_obj, good], src)
    emit("  Face2, where only the area differs: %s" % sorted(detail2.items()))
    check(mismatched2 == {("Face2", "part_holeless_1")},
          "Face2 was not flagged, so nothing here is testing the area "
          "comparison: %s" % sorted(mismatched2))

    # And the same for edges, where length is the only measure a differing
    # edge has: two boxes, same edge count, same curve types, same closed
    # flags, different lengths.
    wide = doc.addObject("Part::Feature", "part_wide_1")
    wide.Shape = Part.makeBox(40, 50, thickness, FreeCAD.Vector(0, 0, 0))
    doc.recompute()
    box_a = doc.addObject("Part::Feature", "src_box")
    box_a.Shape = Part.makeBox(40, 25, thickness, FreeCAD.Vector(0, 0, 0))
    doc.recompute()
    # Picked from the geometry directly, not through `subelement_signature`.
    # Asking the function under test which edges differ means that zeroing the
    # length in it empties the list, and the check then fails on "no edge
    # differs in length" -- a true sentence that says nothing about the defect
    # that was injected. Which is how injection 5 was caught the first time.
    differing = []
    a_edges = box_a.Shape.Edges
    b_edges = wide.Shape.Edges
    for i in range(min(len(a_edges), len(b_edges))):
        if abs(a_edges[i].Length - b_edges[i].Length) > 1e-9:
            differing.append(("Edge%d" % (i + 1),
                              a_edges[i].Length, b_edges[i].Length))
    emit("  edges whose LENGTH is the only difference: %s" % differing[:4])
    check(len(differing) > 0,
          "no edge of the two boxes differs in length, so the length "
          "comparison cannot be tested here")
    name = differing[0][0]
    _m3, mismatched3, _d3 = cam_replay.check_subnames_against_clones(
        [name], [wide], box_a)
    check(mismatched3 == {(name, "part_wide_1")},
          "%s differs only in length (%g against %g) and was not flagged: %s"
          % (name, differing[0][1], differing[0][2], sorted(mismatched3)))

    # Face7 exists only on the source: that is `missing`, a different failure.
    _missing7, mismatched7, _d7 = cam_replay.check_subnames_against_clones(
        ["Face7"], [box_obj], src)
    check(_missing7 == {"Face7"},
          "Face7 should be missing on the holeless copy, got %s" % sorted(_missing7))
    check(mismatched7 == set(),
          "Face7 was called a mismatch as well as missing: %s" % sorted(mismatched7))

    FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# 4. Through the real replay
# --------------------------------------------------------------------------

def check_replay_leaves_out_the_wrong_copy():
    """End to end: a Profile on Face3, one holeless part among the copies.

    The point is not that the check fires -- the previous function shows that --
    but that the replay *acts* on it: the good copies keep the operation and the
    bad one loses it, and the user is told. A check that reports and then cuts
    anyway is the NEST-015 shape all over again.
    """
    emit("")
    emit("-- 4. through the real replay --")
    from Path.Main import Job as PathJob
    from Path.Op import Profile as PathProfile

    doc = FreeCAD.newDocument("sub_replay")
    thickness = 6.0

    bracket = doc.addObject("Part::Feature", "Bracket")
    bracket.Shape = Part.makeBox(40, 25, thickness,
                                 FreeCAD.Vector(-20, -12.5, -thickness)).cut(
        Part.makeCylinder(4, thickness, FreeCAD.Vector(-10, 0, -thickness)))
    source_job = PathJob.Create("UserJob", [bracket], None)
    doc.recompute()
    model = source_job.Model.Group[0]

    top = ["Face%d" % (i + 1) for i, f in enumerate(model.Shape.Faces)
           if type(f.Surface).__name__ == "Plane"
           and abs(f.CenterOfMass.z) < 1e-9]
    check(len(top) == 1,
          "expected one top face to select, found %d" % len(top))
    chosen = top[0]

    prof = PathProfile.Create("Profile", None, source_job)
    doc.recompute()
    prof.Base = [(model, [chosen])]
    prof.HandleMultipleFeatures = "Individually"
    prof.Side = "Outside"
    prof.Direction = "CW"
    prof.UseComp = True
    prof.ToolController = source_job.Tools.Group[0]
    doc.recompute()
    check(len(prof.Path.Commands) > 4,
          "the source Profile produced only %d commands"
          % len(prof.Path.Commands))

    # The nest: two real copies and one holeless impostor.
    sheet = doc.addObject("App::DocumentObjectGroup", "Sheet_1")
    shapes = doc.addObject("App::DocumentObjectGroup", "Shapes_1")
    sheet.addObject(shapes)
    counter = [1]

    def nest(shape, cx, cy, angle):
        n = counter[0]
        counter[0] += 1
        container = doc.addObject("App::Part", "nested_Bracket_%d" % n)
        shapes.addObject(container)
        part = doc.addObject("Part::Feature", "part_Bracket_%d" % n)
        s = shape.copy()
        s.Placement = FreeCAD.Placement()
        part.Shape = s
        container.addObject(part)
        container.Placement = FreeCAD.Placement(
            FreeCAD.Vector(cx, cy, 0),
            FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), angle))
        doc.recompute()
        return part

    real_shape = bracket.Shape
    holeless = Part.makeBox(40, 25, thickness,
                            FreeCAD.Vector(-20, -12.5, -thickness))
    nest(real_shape, 40.0, 40.0, 0.0)
    holeless_part = nest(holeless, 140.0, 40.0, 37.0)
    nest(real_shape, 40.0, 120.0, 25.0)
    doc.recompute()

    layout = doc.addObject("App::DocumentObjectGroup", "Layout_1")
    for name, value in (("SheetWidth", 300.0), ("SheetHeight", 200.0),
                        ("SheetThickness", thickness)):
        layout.addProperty("App::PropertyLength", name, "Layout", "")
        setattr(layout, name, value)
    doc.recompute()

    flattened = cam_replay.flatten_sheet(doc, sheet, thickness)
    check(len(flattened) == 3,
          "expected 3 flattened parts, got %d" % len(flattened))
    if len(flattened) != 3:
        FreeCAD.closeDocument(doc.Name)
        return

    replay = cam_replay.create_replay_job(doc, layout, sheet, flattened.parts,
                                          source_job=source_job)
    check(replay is not None, "create_replay_job returned None")
    if replay is None:
        FreeCAD.closeDocument(doc.Name)
        return

    recipe = cam_replay.read_recipe(source_job)
    result = cam_replay.replay_recipe(recipe, replay.job, replay.clones)
    doc.recompute()

    emit("  failures: %s" % result.failures)
    emit("  subname_detail: %s" % result.subname_detail)
    emit("  %d operation(s) built" % len(result.operations))

    check(len(result.failures) > 0,
          "the replay reported nothing about a Profile whose selection means a "
          "different feature on one of three parts")
    said_something = [f for f in result.failures if "different feature" in f]
    check(len(said_something) > 0,
          "no failure said 'different feature': %s" % result.failures)
    # The name must be one the user recognises. The replay's own geometry is
    # `CAMPart_57`; the part they placed in the nest is `part_Bracket_2`. An
    # earlier version of this check looked for the part label and passed only
    # because the label it compared against did not exist -- and it went on to
    # miss a real bug in the drop logic below, which was matching on
    # `CAMPart_57` in one place and the display label in another.
    check(any("part_Bracket_2" in f for f in result.failures),
          "no failure named the offending part the way the user knows it: %s"
          % result.failures)
    check(not any("CAMPart_" in f for f in result.failures),
          "a failure still names the replay's internal CAMPart_NN label: %s"
          % result.failures)

    # The good copies must still have their operation: refusing one part must
    # not cost the other two theirs.
    by_part = {}
    for op in result.operations:
        for entry in (getattr(op, "Base", None) or []):
            obj = entry[0] if isinstance(entry, tuple) else entry
            lbl = getattr(obj, "Label", "?")
            if lbl.startswith("CAMPart") or lbl.startswith("part_"):
                by_part.setdefault(lbl, 0)
                by_part[lbl] += 1
    emit("  operations per target: %s" % sorted(by_part.items()))
    total_targets = len(replay.clones)
    check(len(result.operations) == total_targets - 1,
          "expected %d operations (%d copies less the one refused), got %d"
          % (total_targets - 1, total_targets, len(result.operations)))

    FreeCAD.closeDocument(doc.Name)


def run():
    check_genuine_copy_passes()
    check_real_nesting_passes()
    check_different_face_is_caught()
    check_replay_leaves_out_the_wrong_copy()


try:
    run()
    emit("")
    emit("sub-element agreement: %d checks, %d failure(s)"
         % (_checks[0], len(_failures)))
    status = 1 if _failures else 0
except Exception:
    traceback.print_exc()
    emit("sub-element agreement: CRASHED")
    status = 1

with open(_STATUS_FILE, "w") as fh:
    fh.write(str(status))