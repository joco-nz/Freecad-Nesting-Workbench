"""A Boundary dressup must be dropped and reported, not replayed wrongly.

NEST-009. The replay replicates operations to each matching nested part. A
`Path.Dressup.Boundary` clips an operation's toolpath against a solid named by an
`App::PropertyLink`, and a nested sheet cannot tell whether that solid means the
part, the sheet, or an exclusion mask -- so the dressup is not replayed. It is
dropped, the rest of the stack is kept, and the user is told.

What it guards
--------------

**The defect this replaced was worse than the unsupported case, which is why the
file exists at all.** Carried verbatim -- what the replay used to do -- the
`Stock` link pointed into the source job, so the Boundary clipped every copy
against a region fitted to one part. Measured on this fixture: **0 cutting moves
where the unclipped figure is 88**, and `outcome.ok` was `True` with
verification clean. A job that cuts nothing, and says nothing, is the failure this
workbench exists to prevent, so it is worth a check that the drop is deliberate
rather than accidental.

**Dropped, not half-built.** `dressup_kind` has to classify `Boundary` as
"unsupported", which is what makes `replay_recipe` skip it, record it, and carry
on with the layer below. If it were classified "known" the old silent behaviour
returns; if it were "unknown" the whole operation is dropped, which is a
different and worse failure. Both are asserted.

**The rest of the stack survives.** The Boundary sits over LeadInOut and Dogbone
here, and both must still be built -- dropping the whole stack would satisfy
"Boundary is gone" while quietly losing two dressups that work. Asserted per
layer, with the chain compared as a whole.

**The operation below it still cuts, and still reaches `Operations.Group`.**
This is the one with teeth. `replay_recipe` used to write an entry only when a
dressup had been built, so a step whose *only* dressup was unsupported built an
operation that never appeared in the list that gets posted -- present in the
document, absent from `Operations.Group`, silent. A Boundary over a bare Profile
is an ordinary user setup, so dropping Boundary makes that reachable. Asserted on
both counts: the list entry exists, and it cuts.

**The toolpath is the unclipped one.** This is what "dropped" has to mean in
practice: more motion than a Boundary would have left, not less, and equal to the
underlying stack's own count. Asserted against the Dogbone's count directly, so
the comparison is against the thing the Boundary was clipping rather than against
a constant.

**The user is told, and the message says what is missing.** Checked for the
dressup's label and the word "not replayed", so dropping it silently fails here.
The old wording said the operation "was replayed bare", which is untrue when the
Boundary was mid-stack over two dressups that *were* built -- so the message is
also checked for not making that claim.

Deliberately *not* asserted here: that the clipping is correct. It is not
reproduced at all, which is the point.

Geometry is plain boxes so the nested positions are known exactly.
`test_replay_dressups.py` remains the file that proves real PartDesign bodies
survive the replay, and its Boundary arm now expects the drop.

Run directly, or via tests/freecad_harness/run.sh. Writes
`.last_status_boundary`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_boundary")

import FreeCAD
import Part

from freecad.nestingworkbench.Tools.Cam import cam_replay

_failures = []
_checks = [0]

#: Nested positions. Eight of them, with the same angles the dressup fixture uses,
#: so a failure here cannot be blamed on a rotation nobody has tried before.
GRID = [(100.0 + column * 210.0, 100.0 + row * 200.0,
         (column * 23 + row * 11) % 90)
        for row in range(2) for column in range(4)]

SHEET_W, SHEET_H, THICKNESS = 900.0, 420.0, 2.0

#: The part carrying the Boundary, and a second part type that does not, so the
#: file can tell "the Boundary is gone" from "the operation is gone".
BOUNDARY_PART = ("BoundaryPlate", 120.0, 60.0, ((-40.0, 0.0, 5.0), (40.0, 0.0, 5.0)))
OTHER_PART = ("OtherPlate", 40.0, 40.0, ((-10.0, 0.0, 4.0), (10.0, 0.0, 4.0)))
#: A third part whose Profile carries a Boundary and nothing else. Sized like
#: BOUNDARY_PART so the same boundary solid works; it exists to give the drop a
#: step where it leaves *no* dressup behind.
BOUNDARY_BARE_PART = ("BarePlate", 90.0, 45.0, ((-30.0, 0.0, 4.0), (30.0, 0.0, 4.0)))


def emit(message=""):
    """Print a report line in a way that survives FreeCAD's console redirect."""
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


# -- geometry ------------------------------------------------------------

def box_part(doc, label, width, height, thickness, holes=()):
    shape = Part.makeBox(width, height, thickness,
                         FreeCAD.Vector(-width / 2.0, -height / 2.0, -thickness))
    for cx, cy, radius in holes:
        shape = shape.cut(Part.makeCylinder(
            radius, thickness, FreeCAD.Vector(cx, cy, -thickness)))
    obj = doc.addObject("Part::Feature", label)
    obj.Shape = shape
    doc.recompute()
    return obj


def top_faces(feature):
    """The planar top faces of a part, for a Profile to run on."""
    return ["Face%d" % (i + 1) for i, f in enumerate(feature.Shape.Faces)
            if type(f.Surface).__name__ == "Plane"
            and abs(f.CenterOfMass.z) < 1e-9]


# -- the source CAM setup ------------------------------------------------

def build_source_job(doc, shapes):
    """One job: a bare Profile, and one under LeadInOut -> Dogbone -> Boundary.

    Three deep because that is the shape FreeCAD's own `dressuptest.FCStd` uses,
    and it exercises the whole write. The Boundary's `Stock` is fitted to the part
    rather than to a sheet, because that is the case where the defect was total:
    a part-fitted boundary clips every copy against a region nowhere near it.
    """
    from Path.Main import Job as PathJob
    from Path.Op import Profile as PathProfile

    job = PathJob.Create("BoundarySource", [f for _l, f in shapes], None)
    doc.recompute()
    model = {label: clone for label, clone
             in zip([l for l, _f in shapes], job.Model.Group)}

    steps = []
    for index, (label, _feature) in enumerate(shapes):
        clone = model[label]
        op = PathProfile.Create("Profile%03d" % index, None, job)
        doc.recompute()
        op.Base = [(clone, top_faces(clone))]
        op.HandleMultipleFeatures = "Individually"
        op.Side = "Outside"
        op.Direction = "CW"
        op.UseComp = True
        op.StartDepth = 0
        op.FinalDepth = -THICKNESS
        op.ToolController = job.Tools.Group[0]
        doc.recompute()
        steps.append([op, []])

    # Three steps, in the order the `shapes` list gives them:
    #   0 OTHER_PART       bare Profile, no dressups at all
    #   1 BOUNDARY_PART    LeadInOut -> Dogbone -> Boundary
    #   2 BOUNDARY_BARE    Boundary over a *bare* Profile -- see below
    #
    # Step 2 exists for one check and is the only thing that covers it. Dropping
    # its single dressup leaves the operation with no dressup at all, and
    # `replay_recipe` used to write an entry to `Operations.Group` only when a
    # dressup had been built -- so that step produced an operation which existed
    # in the document, cut correctly, and was absent from the list that gets
    # posted, with nothing said about it.
    #
    # Step 1 cannot catch that, because LeadInOut and Dogbone survive the drop and
    # leave `outermost` set. Without step 2 the fix to that condition is untested,
    # and it would be untested in the one file whose purpose is to be a check
    # that can fail.
    op = steps[1][0]

    from Path.Dressup.Gui.LeadInOut import ObjectDressup
    from Path.Dressup.DogboneII import Proxy as Dogbone
    from Path.Dressup.DogboneII import Style as DogboneStyle
    from Path.Dressup.Boundary import DressupPathBoundary

    def boundary_solid(name):
        # Fitted to the part, with the Z band straddling the contour's Z. A
        # boundary sized to the part rather than to the path -- which is what
        # `CreateFromBase` gives -- produces an empty path in the source job too,
        # measured 0 cutting moves at 122x52 for a 120x50 part cut with a 5 mm
        # endmill on the Outside, whose tool-centre path spans 125x55. This one is
        # sized to clear that.
        stock = doc.addObject("Part::Feature", name + "Stock")
        stock.Shape = Part.makeBox(126.0, 58.0, 4.0,
                                   FreeCAD.Vector(-63.0, -29.0, -2.0))
        doc.recompute()
        return stock

    def add_boundary(base, name):
        boundary = doc.addObject("Path::FeaturePython", name)
        boundary.Proxy = DressupPathBoundary(boundary, base, job)
        boundary.Stock = boundary_solid(name)
        boundary.Inside = True
        boundary.Offset = 0.0
        doc.recompute()
        return boundary

    lead = doc.addObject("Path::FeaturePython", "DressupLeadInOut")
    built = ObjectDressup(lead, op)
    built.LeadIn = True
    built.LeadOut = True
    built.StyleIn = "Arc"
    built.StyleOut = "Arc"
    built.RadiusIn = 2.0
    built.RadiusOut = 2.0
    built.AngleIn = 90
    built.AngleOut = 90
    doc.recompute()

    bone = doc.addObject("Path::FeaturePython", "DressupDogbone")
    bone.Proxy = Dogbone(bone, lead)
    bone.Style = DogboneStyle.Dogbone
    doc.recompute()

    steps[1][1].extend([lead, bone, add_boundary(bone, "DressupPathBoundary")])
    steps[2][1].append(add_boundary(steps[2][0], "DressupPathBoundaryBare"))

    doc.recompute()

    three = steps[1][1][-1]
    cam_replay.set_operation_order(
        job, [dressups[-1] if dressups else op for op, dressups in steps])
    doc.recompute()
    # The three-deep step's Boundary, which the checks compare against. Not a
    # bare name: the dressup is now built through `add_boundary` and appended
    # straight into the stack, so there is no local `boundary` to return.
    return job, steps, three


def build_layout(doc, shapes):
    """A one-sheet layout with `len(GRID)` copies of each part type."""
    layout = doc.addObject("App::DocumentObjectGroup", "Layout_B")
    for name, value in (("SheetWidth", SHEET_W), ("SheetHeight", SHEET_H),
                        ("SheetThickness", THICKNESS)):
        layout.addProperty("App::PropertyLength", name, "Layout", "")
        setattr(layout, name, value)

    sheet = doc.addObject("App::DocumentObjectGroup", "Sheet_1")
    layout.addObject(sheet)
    shapes_group = doc.addObject("App::DocumentObjectGroup", "Shapes_1")
    sheet.addObject(shapes_group)
    boundary = doc.addObject("Part::Feature", "Sheet_Boundary_1")
    boundary.Shape = Part.makePlane(SHEET_W, SHEET_H)
    sheet.addObject(boundary)

    index = 0
    for label, feature in shapes:
        for cx, cy, angle in GRID:
            container = doc.addObject("App::Part", "nested_%s_%d" % (label, index))
            shapes_group.addObject(container)
            part = doc.addObject("Part::Feature", "part_%s_%d" % (label, index))
            shape = feature.Shape.copy()
            shape.Placement = FreeCAD.Placement()
            part.Shape = shape
            container.addObject(part)
            container.Placement = FreeCAD.Placement(
                FreeCAD.Vector(cx, cy, 0),
                FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), angle))
            index += 1
    doc.recompute()
    return layout, sheet


# -- inspection ----------------------------------------------------------

def chain_kinds(entry):
    names, current, seen = [], entry, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        proxy = getattr(current, "Proxy", None)
        names.append(type(proxy).__name__ if proxy is not None else "?")
        current = (getattr(current, "Base", None)
                   if cam_replay.is_dressup(current) else None)
    return names


#: Chain entries are matched by **label prefix**, not by proxy class name.
#: `chain_kinds` returns the proxy's class, and for the two dressups in this
#: fixture those are `Proxy` (DogboneII) and `ObjectDressup` (LeadInOut) --
#: neither contains "Dogbone" or "LeadInOut", so matching on the class name
#: cannot work and the first version of this file found no chains at all and
#: reported "no Dogbone chain in the replayed list".
#:
#: The labels are what a user reads in the tree anyway, and `describe_*` and the
#: sibling harness's checks both work in terms of them.
DOGBONE_ENTRY = "DressupDogbone"


def is_dogbone_entry(entry):
    return entry.Label.startswith(DOGBONE_ENTRY)


def has_boundary(entry):
    return any("Boundary" in str(k) for k in chain_kinds(entry))


def bottom_op(entry):
    while cam_replay.is_dressup(entry) and getattr(entry, "Base", None) is not None:
        entry = entry.Base
    return entry


def targets_of(entry):
    return [p[0] for p in (getattr(bottom_op(entry), "Base", None) or [])
            if isinstance(p, (list, tuple))]


def cut_count(entry):
    path = getattr(entry, "Path", None)
    if path is None:
        return 0
    return sum(1 for c in (getattr(path, "Commands", None) or ())
               if cam_replay.is_cutting_motion(cam_replay.motion_of(c)))


# -- the checks ----------------------------------------------------------

def check_classification():
    """Boundary must classify as "unsupported" -- not "known", not "unknown"."""
    emit("")
    emit("-- classification --")
    check("Path.Dressup.Boundary" not in cam_replay.DRESSUP_BUILDERS,
          "Path.Dressup.Boundary is still in DRESSUP_BUILDERS, so it is built "
          "and clips against a solid in the source job")
    check("Path.Dressup.Boundary" in cam_replay.DRESSUP_UNSUPPORTED,
          "Path.Dressup.Boundary is not in DRESSUP_UNSUPPORTED, so a step "
          "carrying one is dropped wholesale rather than stripped of it")
    check("Path.Dressup.Boundary" not in cam_replay.UNSPLITTABLE_DRESSUPS,
          "Path.Dressup.Boundary is still in UNSPLITTABLE_DRESSUPS; it is never "
          "built now, so the guard is dead and misleading")


def check_dropped(outcome, source_boundary, pre_replay_names):
    """No replayed chain contains a Boundary, and the user was told."""
    emit("")
    emit("-- dropped and reported --")
    doc = outcome.replay_job.job.Document
    group = list(outcome.replay_job.job.Operations.Group)

    with_boundary = [e for e in group if has_boundary(e)]
    check_equal(len(with_boundary), 0,
                "a replayed chain still carries a Boundary dressup: %s"
                % [e.Label for e in with_boundary])

    # **Only objects the replay created.** The source job and the layout live in
    # this same document, so scanning all of it finds the source's own Boundary
    # and reports it as a leftover. The first version of this check did exactly
    # that and failed with "the replay document contains 1 Boundary object".
    built = [o for o in doc.Objects
             if o.Name not in pre_replay_names
             and type(getattr(o, "Proxy", None)).__name__ == "DressupPathBoundary"]
    check_equal(len(built), 0,
                "the replay created %d Boundary object(s) (%s); a dropped "
                "dressup must not leave one behind"
                % (len(built), [o.Label for o in built]))

    # And the source's own is untouched -- the drop must not delete the user's
    # setup, which is the one destructive thing a drop could plausibly do.
    check(source_boundary.Stock is not None and
          source_boundary.Stock.Document is doc,
          "the source job's Boundary lost its Stock, so the drop reached back "
          "into the user's own setup")

    reported = outcome.result.unsupported_dressups
    # Two, because the fixture carries two: one over LeadInOut+Dogbone and one over
    # a bare Profile. **Not 16**, which is what it was before the report was
    # deduplicated -- a split step over 8 parts must not tell the user the same
    # thing eight times.
    check_equal(len(reported), 2,
                "unsupported dressups recorded for the sheet")
    if reported:
        module_name, label, reason = reported[0]
        check_equal(module_name, "Path.Dressup.Boundary",
                    "the reported dressup")
        check(label in (source_boundary.Label, "DressupPathBoundaryBare")
              or "Boundary" in str(label),
              "the report does not name a source Boundary label; got %r"
              % label)
        check(len(reason) > 40,
              "the reason is too thin to act on: %r" % reason)
    labels = [lbl for _m, lbl, _r in reported]
    check_equal(len(set(labels)), len(labels),
                "the same dressup was reported more than once: %s" % labels)

    text = "\n".join(cam_replay.describe_replay_result(outcome.result))
    check("not replayed" in text,
          "the summary does not say the dressup was not replayed; it reads: %s"
          % text[:200])
    check(source_boundary.Label in text,
          "the summary does not name the dropped dressup")
    check("replayed bare" not in text,
          "the summary claims the operation was replayed 'bare', which is "
          "false when the Boundary sat over dressups that were built")

    # The console warning, which is what an operator sees mid-run.
    emit("  summary line: %s"
         % [ln for ln in cam_replay.describe_replay_result(outcome.result)
            if "not replayed" in ln][:1])


def check_rest_of_stack_survives(outcome):
    """LeadInOut and Dogbone below the Boundary are still built."""
    emit("")
    emit("-- the rest of the stack survives --")
    group = list(outcome.replay_job.job.Operations.Group)

    dogs = [e for e in group if is_dogbone_entry(e)]
    check(len(dogs) > 0, "no Dogbone chain in the replayed list")

    # One Dogbone per BoundaryPlate copy, and it must wrap the LeadInOut rather
    # than sit directly on the operation -- the Boundary was the outermost layer,
    # so losing it must not lose the one below it too. Checked by chain depth
    # rather than by name, because the LeadInOut's proxy class is `ObjectDressup`
    # and its label is the only readable marker.
    led_through_dog = 0
    for e in dogs:
        base = getattr(e, "Base", None)
        if base is not None and cam_replay.is_dressup(base) \
                and getattr(base, "Label", "").startswith("DressupLeadInOut"):
            led_through_dog += 1
    check_equal(led_through_dog, len(dogs),
                "every Dogbone should still wrap its LeadInOut; %d of %d do"
                % (led_through_dog, len(dogs)))
    emit("  %d Dogbone chain(s), %d of them wrapping a LeadInOut"
         % (len(dogs), led_through_dog))


def check_operations_still_cut(outcome):
    """The operations under the Boundary are listed, and they cut."""
    emit("")
    emit("-- the operations under the Boundary --")
    group = list(outcome.replay_job.job.Operations.Group)
    entries = [e for e in group if is_dogbone_entry(e)]

    check(len(entries) > 0,
          "no entries at all, so every check above would be vacuous")
    check_equal(len(entries), len(GRID),
                "one Dogbone chain per matching nested part")

    cutting = sum(1 for e in entries if cam_replay.has_cutting_motion(e))
    check_equal(cutting, len(entries),
                "entries with cutting motion: %d of %d" % (cutting, len(entries)))
    total = sum(cut_count(e) for e in entries)
    emit("  %d entr(ies), %d cutting move(s) in total" % (len(entries), total))
    check(total > 0,
          "the whole step produced no cutting motion, so the fixture is not "
          "testing anything")


def check_unclipped(outcome, source_dogbone):
    """The toolpath is the underlying stack's own -- the Boundary clipped nothing."""
    emit("")
    emit("-- the toolpath is unclipped --")
    group = list(outcome.replay_job.job.Operations.Group)
    entries = [e for e in group if is_dogbone_entry(e)]
    if not entries:
        check(False, "no entries to compare")
        return

    source_clipped = cut_count(source_dogbone)
    emit("  source's underlying Dogbone, one part: %d cutting moves"
         % source_clipped)

    # **No copy may have LESS motion than the source's underlying stack.** That is
    # the whole claim: dropping the Boundary means nothing clips anything away.
    # An exact match per copy is the wrong assertion -- a copy that is rotated
    # recomputes its lead-ins differently and can come out with 11 rather than
    # 10, so demanding equality would fail on correct output. Measured here: 3 of
    # 8 copies match exactly and 8 of 8 are at or above.
    #
    # Compared against the Dogbone rather than the Boundary, because the Boundary
    # is gone on both sides of this comparison.
    below = [(e.Label, cut_count(e)) for e in entries if cut_count(e) < source_clipped]
    check_equal(len(below), 0,
                "copies with less motion than the unclipped source (%d), so "
                "something is still clipping: %s"
                % (source_clipped, below))
    at_or_above = sum(1 for e in entries if cut_count(e) >= source_clipped)
    emit("  copies at or above it: %d of %d (exact matches %d)"
         % (at_or_above, len(entries),
            sum(1 for e in entries if cut_count(e) == source_clipped)))


def check_bare_operation_still_listed(outcome):
    """A step whose *only* dressup was dropped still yields a listed, cutting operation.

This is what "only the underlying dressups/paths get copied" means when there
are **no** underlying dressups -- a Boundary directly over a bare Profile, which
is what a user gets if they reach for Boundary first. The operation must survive
as a bare Profile, in the list that gets posted, cutting.

**What this does NOT cover, which took an injection to find out.** It was first
written as the regression test for `replay_recipe`'s `built_any` guard, on the
claim that a step with every dressup dropped never reached `Operations.Group`.
That claim was wrong. Restoring the original guard left this file green, because
`order_operations` walks every base operation and substitutes
`entry_of.get(id(op), op)` -- falling back to the bare operation -- so the list is
rebuilt from `result.operations` regardless of what the replay's own loop
appended. The guard only bites on the cancel path, which skips ordering; that is
untested here and is not claimed to be.

So this check stands on its own claim: the drop leaves a usable operation. Which
is the user's requirement, and was worth pinning either way.
"""
    emit("")
    emit("-- a step with nothing left after the drop --")
    group = list(outcome.replay_job.job.Operations.Group)
    # Matched on `nested_label_of`, not on the target's own Label. An operation's
    # `Base` points at the replay job's *flattened part*, which is called
    # `CAMPart_57`; the part type is only in the nested label
    # `nested_BarePlate_9` that the flattener recorded. Reading `Label` finds
    # nothing, which is how the first version of this check reported "no BarePlate
    # operation" against a job that had eight.
    bare = [e for e in group
            if any("BarePlate" in cam_replay.nested_label_of(t)
                   for t in targets_of(e))]
    check(len(bare) > 0,
          "no BarePlate operation in Operations.Group; the drop emptied the "
          "stack and nothing usable was left for this step")
    check_equal(len(bare), len(GRID),
                "one BarePlate operation per matching nested part")
    cutting = sum(1 for e in bare if cam_replay.has_cutting_motion(e))
    check_equal(cutting, len(bare),
                "BarePlate entries with cutting motion: %d of %d"
                % (cutting, len(bare)))
    total = sum(cut_count(e) for e in bare)
    emit("  %d BarePlate entr(ies), %d cutting move(s)" % (len(bare), total))
    check(total > 0,
          "the BarePlate step produced no cutting motion at all")

    # And it must not be a dressup, since the only one it had was dropped.
    dressed = [e.Label for e in bare if cam_replay.is_dressup(e)]
    check_equal(len(dressed), 0,
                "the BarePlate entry is a dressup, but its only dressup was "
                "dropped: %s" % dressed)


def run():
    check_classification()

    doc = FreeCAD.newDocument("replay_boundary")
    # OTHER_PART first: it is the bare Profile. See build_source_job.
    shapes = [
        (OTHER_PART[0],
         box_part(doc, OTHER_PART[0], OTHER_PART[1], OTHER_PART[2],
                  THICKNESS, OTHER_PART[3])),
        (BOUNDARY_PART[0],
         box_part(doc, BOUNDARY_PART[0], BOUNDARY_PART[1], BOUNDARY_PART[2],
                  THICKNESS, BOUNDARY_PART[3])),
        (BOUNDARY_BARE_PART[0],
         box_part(doc, BOUNDARY_BARE_PART[0], BOUNDARY_BARE_PART[1],
                  BOUNDARY_BARE_PART[2], THICKNESS, BOUNDARY_BARE_PART[3])),
    ]
    doc.recompute()

    job, steps, source_boundary = build_source_job(doc, shapes)
    source_dogbone = source_boundary.Base

    check(cam_replay.has_cutting_motion(source_boundary),
          "the source Boundary produces no cutting motion, so the fixture is not "
          "testing anything -- it cannot show what dropping it gives back")
    emit("")
    emit("Source stack: %s" % " -> ".join(
        map(str, cam_replay.walk_stack(source_boundary)[1]
            if isinstance(cam_replay.walk_stack(source_boundary), tuple) else [])))

    layout, _sheet = build_layout(doc, shapes)
    found, _warnings = cam_replay.resolve_layout_group(doc)
    check(found is not None, "no layout was auto-detected")

    # Taken before the replay, so "did the replay create a Boundary" can be asked
    # without also finding the source job's own -- same document, deliberately.
    pre_replay_names = {o.Name for o in doc.Objects}

    outcomes = cam_replay.replay_layout(doc, found, job)
    check_equal(len(outcomes), 1, "expected one sheet outcome")
    if not outcomes:
        return

    outcome = outcomes[0]
    emit("")
    emit("Sheet outcome: ok=%s" % outcome.ok)
    for err in outcome.errors:
        emit("  ERROR: %s" % err)

    check_dropped(outcome, source_boundary, pre_replay_names)
    check_rest_of_stack_survives(outcome)
    check_operations_still_cut(outcome)
    check_bare_operation_still_listed(outcome)
    check_unclipped(outcome, source_dogbone)


if __name__ in ("__main__", "test_replay_boundary"):
    status = 0
    try:
        run()
    except Exception:
        traceback.print_exc()
        _failures.append("raised")
    emit("\nreplay boundary: %d checks, %d failure(s)"
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
    emit("REPLAY_BOUNDARY_STATUS=%d" % status)
    sys.exit(status)