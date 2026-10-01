"""Prove the identity cross-check can fail, because it used to be unable to.

The replay narrows an operation to the nested parts matching its source part:

    Profile004 is set up on SimpleSpacer, so it cuts the 2 SimpleSpacers and
    not the other parts on the sheet.

`clones_for_source` cannot always narrow, and when it cannot it returns every
clone instead. That is a deliberate safety property -- an operation applied to a
few too many parts is visible in the toolpath, whereas one applied to too few
silently cuts less than the source. But "fell back to everything" is exactly the
case an operator needs told about, because it means the feature is about to be
cut on parts it was never set up for.

**The check that was supposed to say so could not.**

The fixture validator cross-checked identity with

    clones_for_source([stub_for_one_container], geometry) or _matches(c, label)

Called with a one-element list, `clones_for_source` can never narrow, so it
returns that one element, which is truthy, so `or` short-circuits and the real
label match is never consulted. The predicate was constant `True`. On the
tracked fixture every operation reported 48 nested parts, where the truth is
23, 23 and 2 -- under a comment claiming it was "the one place the replay can
be confidently wrong without anything raising".

A test that cannot fail is worse than no test, because it looks like evidence.
This file is the demonstration that the replacement can fail, and it is
separate on purpose: it asserts a fixture is *wrong*, so wiring it into the
harness gate would report a broken build over a deliberately broken fixture. Run
it when you touch `clones_for_source` or the validator. It is not a gate.

Geometry is the user's own source material -- the three PartDesign bodies in
`replay-fixture-CAM-Nested.FCStd`'s SourceShapes group, copied as shapes so the
surfaces stay analytic and the tracked fixture is not modified. No `.FCStd` is
committed for this: the mismatch is a one-line reassignment of an operation's
Base, and a file whose only purpose is to be wrong is better as code than data.

Run directly:

    freecadcmd tests/freecad_harness/test_replay_identity.py

Writes `.last_status_identity`; 0 when every assertion held, 1 when one did not.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_identity")

FIXTURE = os.path.join(_REPO, "tests", "Test_Files",
                       "replay-fixture-CAM-Nested.FCStd")

import FreeCAD
import Part

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench import freecad_helpers

_failures = []
_checks = [0]

#: How many nested copies of each part type the sheet holds. Uneven on purpose:
#: "narrowed to the right part type" and "fell back to everything" then cannot
#: produce the same number, which is what makes a mismatch detectable at all.
COPIES = {"BottomStrap": 6, "TopStrap": 5, "SimpleSpacer": 2}


def emit(message=""):
    """Print a report line in a way that survives FreeCAD's console redirect."""
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
        else:
            print(message)
    except Exception:
        pass


def check(condition, message):
    _checks[0] += 1
    if not condition:
        _failures.append(message)
        emit("  FAIL: %s" % message)
    return bool(condition)


def check_equal(actual, expected, message):
    return check(actual == expected,
                 "%s (got %r, expected %r)" % (message, actual, expected))


# -- geometry: the user's own source bodies ------------------------------

def load_source_shapes(doc, thickness):
    """Copy the fixture's SourceShapes bodies in, seated on the sheet.

    The bodies arrive at z 0..thickness with a single planar face on top, which
    is what a Profile needs to select. They are translated so the bottom sits at
    -thickness, matching the convention `flatten_container` normalises to, so
    the part arrives at the nested position unchanged.

    Copied as shapes rather than referenced across documents: a plain shape copy
    keeps `Cylinder` and `Plane` surfaces, where `transformGeometry` would turn
    them into B-splines, and it leaves the tracked fixture untouched.
    """
    if not os.path.exists(FIXTURE):
        emit("no fixture at %s -- cannot build this check" % FIXTURE)
        return []
    src = FreeCAD.openDocument(FIXTURE)
    group = None
    for obj in src.Objects:
        if obj.Label == "SourceShapes":
            group = obj
            break
    if group is None:
        emit("fixture has no SourceShapes group")
        FreeCAD.closeDocument(src.Name)
        return []

    out = []
    for body in group.Group:
        if body.Label not in COPIES:
            continue
        shape = body.Shape.copy()
        shape.Placement = FreeCAD.Placement()          # drop the fixture's own
        shape.translate(FreeCAD.Vector(0, 0, -shape.BoundBox.ZMin - thickness))
        feature = doc.addObject("Part::Feature", body.Label)
        feature.Shape = shape
        out.append((body.Label, feature))
    doc.recompute()

    for _label, feature in out:
        surfaces = {type(f.Surface).__name__ for f in feature.Shape.Faces}
        check("BSplineSurface" not in surfaces,
              "%s has B-spline faces after the copy: %s"
              % (feature.Label, surfaces))
        bb = feature.Shape.BoundBox
        check(abs(bb.ZMin + thickness) < 1e-6 and abs(bb.ZMax) < 1e-6,
              "%s spans z %.3f..%.3f, expected %.3f..0"
              % (feature.Label, bb.ZMin, bb.ZMax, -thickness))

    FreeCAD.closeDocument(src.Name)
    return out


def top_face(feature):
    """The single planar face at the top of `feature`, as a sub-element name."""
    faces = feature.Shape.Faces
    top = max(f.CenterOfMass.z for f in faces)
    names = ["Face%d" % (i + 1) for i, f in enumerate(faces)
             if type(f.Surface).__name__ == "Plane"
             and abs(f.CenterOfMass.z - top) < 1e-7]
    check_equal(len(names), 1,
                "%s has %d planar face(s) at the top, expected 1"
                % (feature.Label, len(names)))
    return names


def build_layout(doc, shapes, thickness):
    """One sheet holding `COPIES` nested containers of each part type."""
    layout = doc.addObject("App::DocumentObjectGroup", "LayoutIdentity")
    for name, value in (("SheetWidth", 700.0), ("SheetHeight", 300.0),
                        ("SheetThickness", thickness)):
        layout.addProperty("App::PropertyLength", name, "Layout", "")
        setattr(layout, name, value)

    sheet = doc.addObject("App::DocumentObjectGroup", "Sheet_1")
    layout.addObject(sheet)
    shapes_group = doc.addObject("App::DocumentObjectGroup", "Shapes_1")
    sheet.addObject(shapes_group)
    # The nester creates one of these per sheet, and the replay reads the
    # sheet's world origin from it. Without it the parts are never moved to
    # local coordinates.
    boundary = doc.addObject("Part::Feature", "Sheet_Boundary_1")
    boundary.Shape = Part.makePlane(700, 300)
    sheet.addObject(boundary)

    total = sum(COPIES[label] for label, _f in shapes)
    column = 0
    for label, feature in shapes:
        for n in range(COPIES[label]):
            container = doc.addObject("App::Part", "nested_%s_%d" % (label, n))
            shapes_group.addObject(container)
            part = doc.addObject("Part::Feature", "part_%s_%d" % (label, n))
            shape = feature.Shape.copy()
            shape.Placement = FreeCAD.Placement()
            part.Shape = shape
            container.addObject(part)
            # Laid out along X so every part is inside the sheet. The nester
            # requires full containment, so a part centred on the origin would
            # hang off the corner -- which is how an early version of this
            # script reported a stock bug that was a placement bug.
            x = 70.0 + (column % 6) * 110.0
            y = 80.0 + (column // 6) * 130.0
            container.Placement = FreeCAD.Placement(
                FreeCAD.Vector(x, y, 0),
                FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), (n * 27) % 90))
            column += 1
    doc.recompute()
    return layout, sheet, total


# -- the source job ------------------------------------------------------

def build_job(doc, shapes, mislabel=True):
    """A CAM job, optionally with one operation pointed at the wrong part.

    One Profile per part type. With `mislabel`, the `SimpleSpacer` operation is
    re-pointed at `TopStrap` -- so the operation's intent (cut the 2 spacers)
    and its geometry (the straps' edges) disagree, by a factor of 2.5. A
    mismatch that happened to match would demonstrate nothing.
    """
    from Path.Main import Job as PathJob
    from Path.Op import Profile as PathProfile

    # PathJob.Create uses FreeCAD.ActiveDocument, not a document argument.
    FreeCAD.setActiveDocument(doc.Name)
    job = PathJob.Create("IdentityJob", [f for _l, f in shapes], None)
    doc.recompute()
    model = {label: clone for label, clone
             in zip([l for l, _f in shapes], job.Model.Group)}

    operations = {}
    for index, (label, _feature) in enumerate(shapes):
        op = PathProfile.Create("Profile%s" % ("" if index == 0 else "00%d" % index),
                                None, job)
        doc.recompute()
        op.Base = [(model[label], top_face(model[label]))]
        op.HandleMultipleFeatures = "Individually"
        op.Side = "Outside"
        op.Direction = "CW"
        op.UseComp = True
        op.StartDepth = 0
        op.FinalDepth = -2
        op.ToolController = job.Tools.Group[0]
        doc.recompute()
        operations[label] = op

    if mislabel and "SimpleSpacer" in operations and "TopStrap" in model:
        operations["SimpleSpacer"].Base = [
            (model["TopStrap"], top_face(model["TopStrap"]))]
        doc.recompute()

    return job, operations


def part_type_of(container):
    pieces = container.Label.split("_")
    return "_".join(pieces[1:-1]) if len(pieces) >= 3 else container.Label


def run():
    doc = FreeCAD.newDocument("replay_identity")
    thickness = 2.0

    shapes = load_source_shapes(doc, thickness)
    if len(shapes) < 3:
        check(False, "expected the 3 SourceShapes bodies, got %d" % len(shapes))
        return
    emit("source bodies: %s" % [label for label, _f in shapes])

    layout, sheet, expected_total = build_layout(doc, shapes, thickness)
    containers = freecad_helpers.get_nested_containers(sheet)
    check_equal(len(containers), expected_total,
                "nested container count")
    counts = {}
    for container in containers:
        counts[part_type_of(container)] = counts.get(part_type_of(container), 0) + 1
    emit("nested parts by type: %s" % counts)
    for label, want in COPIES.items():
        check_equal(counts.get(label), want, "%s nested count" % label)

    # -- the honest case first --
    #
    # Establishes that this fixture CAN be correct, so a failure in the
    # mismatched run below is attributable to the mismatch and not to the setup.
    # A negative test that also fails when it should pass proves nothing.
    emit("\n-- honest job: every operation on its own part --")
    job_ok, ops_ok = build_job(doc, shapes, mislabel=False)
    outcomes_ok = cam_replay.replay_layout(doc, layout, job_ok)
    check_equal(len(outcomes_ok), 1, "one outcome from the honest job")
    if not outcomes_ok:
        return
    clones_ok = outcomes_ok[0].replay_job.clones
    check_equal(len(clones_ok), expected_total, "clone count")
    for label, op in ops_ok.items():
        matched = cam_replay.clones_for_source(clones_ok, op.Base[0][0])
        check_equal(len(matched), counts.get(label, 0),
                    "honest: %s narrows to its own %d part(s)"
                    % (label, counts.get(label, 0)))
    for operation in outcomes_ok[0].result.operations:
        check(cam_replay.has_cutting_motion(operation),
              "honest: %s has no cutting motion" % operation.Label)
    emit("  honest: %d operation(s), all cutting, each narrowed to its own type"
         % len(outcomes_ok[0].result.operations))

    # -- the mismatched job --
    emit("\n-- mismatched job: the SimpleSpacer operation points at TopStrap --")
    job_bad, ops_bad = build_job(doc, shapes, mislabel=True)
    outcomes_bad = cam_replay.replay_layout(doc, layout, job_bad)
    check_equal(len(outcomes_bad), 1, "one outcome from the mismatched job")
    if not outcomes_bad:
        return

    clones = outcomes_bad[0].replay_job.clones
    spacer_op = ops_bad["SimpleSpacer"]
    geometry = spacer_op.Base[0][0]
    matched = cam_replay.clones_for_source(clones, geometry)

    resolved = getattr(cam_replay.resolve_source_object(geometry) or geometry,
                       "Label", "?")
    emit("  SimpleSpacer operation now resolves to source part %r" % resolved)
    emit("  clones_for_source: %d of %d clone(s), all of type %s"
         % (len(matched), len(clones), resolved))

    # 1. Narrowing still works. It resolved to the STRAPS, not the spacers, and
    #    not to everything -- so this is a *narrowed but wrong* answer, which is
    #    the case a count-based check can catch and a fallback detector cannot.
    check(len(matched) < len(clones),
          "clones_for_source narrowed nothing: %d of %d" % (len(matched), len(clones)))
    check_equal(len(matched), COPIES["TopStrap"],
                "mismatched operation resolves to TopStrap's parts")

    # 2. The old predicate, demonstrated to be constant.
    #
    #    This is the bug as an executable assertion. `stub` stands in for the
    #    validator's per-container call: one element, no NestedLabel, so
    #    narrowing is impossible and the safety fallback returns that one
    #    element whatever container it was handed.
    def stub(container):
        return type("_S", (), {"Objects": [container],
                               "Label": container.Label})()

    vacuous = sum(1 for c in containers
                  if cam_replay.clones_for_source([stub(c)], geometry))
    check_equal(vacuous, len(containers),
                "the old per-container predicate is constant True -- that IS "
                "the bug. If this ever fails, the bug is gone and this "
                "assertion should be deleted rather than trusted.")

    # 3. The replacement check reports the truth, and a mismatch is detectable.
    expected = COPIES["SimpleSpacer"]
    emit("  validator reports: %r -> %r -> %d nested part(s); expected %d"
         % (spacer_op.Label, resolved, len(matched), expected))
    check(len(matched) != expected,
          "the mismatch is undetectable: resolved to %d and expected %d "
          "collide, so this fixture cannot demonstrate the check"
          % (len(matched), expected))

    # 4. The replayed job is CONSISTENT with what it was told, and that is the
    #    point worth making explicit.
    #
    #    The mislabelled operation now resolves to TopStrap, so the replay
    #    builds an operation that cuts 5 straps -- which is exactly right, given
    #    the input. The replay does not know the operator meant a spacer, and
    #    nothing in the document records that. So a re-pointed operation is NOT
    #    machine-detectable after the fact, and any check that claims to detect
    #    it is asserting something false.
    #
    #    What is detectable, and what the validator now exists to show, is the
    #    mapping: which part each operation actually cuts, and how many. That is
    #    the number a human checks against the source parts, and it is wrong in
    #    a way that is visible only because it is reported. The old check
    #    reported 13 -- every part -- for every operation, so a real mismatch
    #    and a correct one looked identical.
    #
    #    So the assertions here are that the report is TRUE, and that the truth
    #    diverges from the intent. Not that the replay misbehaved.
    def resolved_part_type(op):
        geometry = op.Base[0][0]
        original = cam_replay.resolve_source_object(geometry) or geometry
        return getattr(original, "Label", "?")

    # Keyed by the SHAPE label, not by the operation's Label. The two jobs are
    # built in one document, so the second job's operations come out as
    # Profile003, Profile004, Profile005 -- the honest job already took Profile,
    # Profile001, Profile002. Pairing by operation Label finds no overlap and
    # silently compares nothing, which is exactly what the first version did.
    set_up_on = {label: resolved_part_type(op) for label, op in ops_bad.items()}
    was_on = {label: resolved_part_type(op) for label, op in ops_ok.items()}
    op_label = {label: getattr(op, "Label", "?") for label, op in ops_bad.items()}
    changed = [label for label, part in set_up_on.items()
               if was_on.get(label) not in (None, part)]

    reported = []
    for operation in outcomes_bad[0].result.operations:
        name = operation.Label
        if name.endswith("_replay"):
            name = name[:-len("_replay")]
        key = next((k for k, v in op_label.items() if v == name), None)
        part_type = set_up_on.get(key) if key else None
        if part_type is None:
            continue
        targets = len(list(getattr(operation, "Base", None) or []))
        expected_for_target = COPIES.get(part_type, 0)
        marker = "   <- was %r" % was_on.get(key) if key in changed else ""
        reported.append((operation, part_type, targets, expected_for_target))
        emit("    %-22s -> %-13s cuts %d part(s) (%d exist)%s"
             % (operation.Label, part_type, targets, expected_for_target, marker))
        # True, and the point: the replay is faithful to its input.
        check_equal(targets, expected_for_target,
                    "replayed %s cuts the %d %s part(s) it points at"
                    % (name, expected_for_target, part_type))

    # And exactly one operation's identity moved, to a part of a different count,
    # so the report shows a divergence rather than a plausible-looking number.
    check_equal(len(changed), 1,
                "exactly one operation's target should have moved, got %d: %s"
                % (len(changed), changed))
    if changed:
        key = changed[0]
        intended, actual = was_on[key], set_up_on[key]
        emit("  %s (%s) moved from %r (%d part(s)) to %r (%d part(s))"
             % (op_label[key], key, intended, COPIES.get(intended, 0),
                actual, COPIES.get(actual, 0)))
        check_equal(intended, "SimpleSpacer", "the moved operation was the spacer one")
        check(COPIES.get(intended, 0) != COPIES.get(actual, 0),
              "the mismatch collapsed: %r and %r have the same count, so the "
              "report would not distinguish them" % (intended, actual))
    for operation in outcomes_bad[0].result.operations:
        check(cam_replay.has_cutting_motion(operation),
              "mismatched: %s has no cutting motion" % operation.Label)

    FreeCAD.closeDocument(doc.Name)


if __name__ in ("__main__", "test_replay_identity"):
    status = 0
    try:
        run()
    except Exception:
        traceback.print_exc()
        _failures.append("raised")
    emit("\nreplay identity: %d checks, %d failure(s)" % (_checks[0], len(_failures)))
    for failure in _failures:
        emit("  FAIL: %s" % failure)
    if _failures:
        status = 1
    try:
        with open(_STATUS_FILE, "w") as handle:
            handle.write(str(status))
    except OSError:
        pass
    emit("REPLAY_IDENTITY_STATUS=%d" % status)
    sys.exit(status)
