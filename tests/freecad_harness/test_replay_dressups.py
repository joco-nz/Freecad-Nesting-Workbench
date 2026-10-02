"""Freecadcmd check that a dressed CAM setup survives the replay.

The pytest tier covers the read walk and the dressup table against stand-ins,
which is enough for the logic and not enough for the thing that matters: that
FreeCAD's real dressup modules produce real toolpaths when the replay rebuilds
them, headless, with no GUI and no view providers.

What this is actually guarding:

  * **a dressed operation is not in the Operations list.** The list carries the
    outermost dressup and the operation is reached through the dressup's link.
    A reader that looks only at the list sees an empty job. That is not
    hypothetical -- it is what the replay's own reader did to the fixture, for
    as long as the fixture existed.

  * **a step is one list entry, not one object.** A stack of two or three
    dressups rebuilds as a chain, and only the outermost goes in the list.

  * **nothing gets cut twice.** The post-processor emits the Operations list
    verbatim, so an operation listed alongside a dressup layered on it produces
    the contour twice. Measured on a lead-in profile: 24 cutting moves in the
    dressup's section, 20 in the operation's.

The geometry comes from `replay-fixture-CAM-Nested.FCStd`'s SourceShapes
group -- three analytic `PartDesign::Body` objects, copied as shapes. It is a
plain shape copy, so surfaces stay `Cylinder` and `Plane` rather than becoming
B-splines, and the fixture itself is not modified.

Run directly, or via tests/freecad_harness/run.sh. Writes
`.last_status_dressup`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_dressup")

FIXTURE = os.path.join(_REPO, "tests", "Test_Files",
                       "replay-fixture-CAM-Nested.FCStd")

import FreeCAD
import Part

from freecad.nestingworkbench.Tools.Cam import cam_replay

_failures = []
_checks = [0]


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
    """Record one check. Never raises -- a raised check aborts the run and
    loses the checks that would have explained why."""
    _checks[0] += 1
    if not condition:
        _failures.append(message)
        emit("  FAIL: %s" % message)
    return bool(condition)


def check_equal(actual, expected, message):
    return check(actual == expected,
                 "%s (got %r, expected %r)" % (message, actual, expected))


# -- geometry ------------------------------------------------------------

def load_fixture_shapes(doc):
    """Copy the fixture's SourceShapes bodies into `doc` as Part::Feature.

    Copied as shapes rather than rebuilt, for two reasons. The surfaces stay
    analytic -- a PartDesign body rebuilt from sketches would be equivalent
    here, but `transformGeometry` would not be, and that conversion is the
    failure this whole test tier exists to catch. And the fixture is not
    modified: it is the shared source of geometry for several checks, and
    reopening a file the previous run may have saved is a slow way to find out
    a test is not idempotent.
    """
    if not os.path.exists(FIXTURE):
        emit("  (no fixture at %s -- skipping the fixture geometry)"
             % FIXTURE)
        return []
    src = FreeCAD.openDocument(FIXTURE)
    group = None
    for obj in src.Objects:
        if obj.Label == "SourceShapes":
            group = obj
            break
    if group is None:
        emit("  (fixture has no SourceShapes group -- skipping)")
        FreeCAD.closeDocument(src.Name)
        return []
    out = []
    for body in group.Group:
        shape = body.Shape.copy()
        feature = doc.addObject("Part::Feature", body.Label)
        feature.Shape = shape
        out.append((body.Label, feature))
    doc.recompute()
    for _label, feature in out:
        surfaces = {type(f.Surface).__name__ for f in feature.Shape.Faces}
        check("BSplineSurface" not in surfaces,
              "%s has B-spline faces after the copy: %s" % (feature.Label, surfaces))
    FreeCAD.closeDocument(src.Name)
    return out


def box_part(doc, label, width, height, thickness, holes=()):
    """A plate with optional holes. Holes matter: a profile round an outer
    contour is one closed loop, while a plate with holes exercises the
    multiple-closed-loop path that a dressup has to add lead-ins to."""
    shape = Part.makeBox(width, height, thickness,
                         FreeCAD.Vector(-width / 2.0, -height / 2.0, -thickness))
    for cx, cy, radius in holes:
        shape = shape.cut(Part.makeCylinder(
            radius, thickness,
            FreeCAD.Vector(cx, cy, -thickness)))
    obj = doc.addObject("Part::Feature", label)
    obj.Shape = shape
    doc.recompute()
    return obj


def top_faces(feature):
    return ["Face%d" % (i + 1) for i, f in enumerate(feature.Shape.Faces)
            if type(f.Surface).__name__ == "Plane"
            and abs(f.CenterOfMass.z) < 1e-9]


#: Sheet size, and the stock box the Boundary dressup is given. They must
#: agree, or the boundary clips against a sheet that is not the one the parts
#: were nested onto.
SHEET_W = 900.0
SHEET_H = 420.0


# -- the source CAM setup ------------------------------------------------

def build_source_job(doc, shapes):
    """Build a CAM job whose operations are dressed up, headless.

    Deliberately covers five shapes of step, because each one used to break the
    read or the write:

      * a bare operation -- nothing to get wrong;
      * a Profile under one LeadInOut, the user's everyday case;
      * a Profile under a Dogbone, the other dressup in use;
      * a two-deep stack, Dogbone over LeadInOut over Profile;
      * a three-deep stack, Boundary over Dogbone over LeadInOut over Profile.

    Three deep is not hypothetical. FreeCAD's own `dressuptest.FCStd` has
    exactly that shape -- LeadInOut -> Dogbone -> Boundary over one operation --
    and it is the deepest the read walk has been shown to handle on a file the
    user did not build. It is also the shape that exercises the most of the
    write: three constructors, each layering on the object the previous one
    produced, and three links to get right.

    The list is written by `set_operation_order`, which is the point: a job
    whose operations are all dressed up has an Operations list containing no
    operations at all.
    """
    from Path.Main import Job as PathJob
    from Path.Op import Profile as PathProfile

    job = PathJob.Create("DressedSetup", [feature for _l, feature in shapes], None)
    doc.recompute()
    model = {label: clone for label, clone
             in zip([l for l, _f in shapes], job.Model.Group)}

    # Same shape the user builds: a Profile, then the dressup layered on it.
    # `PathProfile.Create` registers itself in the Operations list; the list is
    # corrected at the end, which is the same two-stage shape as the replay's
    # own write.
    steps = []
    for index, (label, _feature) in enumerate(shapes):
        clone = model[label]
        op = PathProfile.Create("Profile%s" % ("" if index == 0 else "00%d" % index),
                                None, job)
        doc.recompute()
        op.Base = [(clone, top_faces(clone))]
        op.HandleMultipleFeatures = "Individually"
        op.Side = "Outside"
        op.Direction = "CW"
        op.UseComp = True
        op.StartDepth = 0
        op.FinalDepth = -2
        op.ToolController = job.Tools.Group[0]
        doc.recompute()
        steps.append([op, []])

    tool = job.Tools.Group[0]

    def dressup(spec_class, base, kind):
        """Layer a dressup on `base` and return it, without listing it."""
        if kind == "LeadInOut":
            from Path.Dressup.Gui.LeadInOut import ObjectDressup
            obj = doc.addObject("Path::FeaturePython", "DressupLeadInOut")
            built = ObjectDressup(obj, base)
            built.LeadIn = True
            built.LeadOut = True
            built.StyleIn = "Arc"
            built.StyleOut = "Arc"
            built.RadiusIn = 2.0
            built.RadiusOut = 2.0
            built.AngleIn = 90
            built.AngleOut = 90
            return obj
        if kind == "Dogbone":
            from Path.Dressup.DogboneII import Proxy as Dogbone
            from Path.Dressup.DogboneII import Style as DogboneStyle
            obj = doc.addObject("Path::FeaturePython", "DressupDogbone")
            obj.Proxy = Dogbone(obj, base)
            # `Side` and `Style` are the properties that exist. There is no
            # BoneLength: the length comes from the tool and the algorithm.
            obj.Style = DogboneStyle.Dogbone
            return obj
        if kind == "Boundary":
            # `DressupPathBoundary(obj, base, job)` -- the job-taking
            # constructor. Boundary clips the path to a solid, so unlike the
            # other two it genuinely needs the job, and it is the one that
            # would notice if the replay's stock were wrong.
            #
            # Two things it needs that the constructor does not supply usefully:
            #
            #   * a STOCK solid. The constructor fits one to the base geometry,
            #     which is barely larger than the part, so `Inside=True` clips
            #     the contour away and the path comes back empty. A user points
            #     this at their sheet; here that is a box the size of the
            #     layout.
            #   * a SOLID, not a face. `PathBoundary` clips with
            #     `edge.common(shape)`, and an edge commoned with a planar face
            #     in 3D returns nothing -- measured 0 cutting moves against a
            #     400x200 face and 11 against a 400x200 box.
            #
            # Its properties are Stock, Inside, RetractThreshold,
            # RestMachiningPass and Offset. There is no FinalDepth: the depth
            # comes from the operation it wraps, which is already set.
            from Path.Dressup.Boundary import DressupPathBoundary
            obj = doc.addObject("Path::FeaturePython", "DressupPathBoundary")
            obj.Proxy = DressupPathBoundary(obj, base, job)
            stock = doc.addObject("Part::Feature", "SheetStock")
            stock.Shape = Part.makeBox(SHEET_W, SHEET_H, 4,
                                       FreeCAD.Vector(-SHEET_W / 2.0,
                                                      -SHEET_H / 2.0, -2))
            doc.recompute()
            obj.Stock = stock
            obj.Inside = True
            obj.Offset = 0.0
            return obj
        raise ValueError("unknown dressup kind %r" % kind)

    # shapes[0] bare, shapes[1] LeadInOut, shapes[2] Dogbone, shapes[3] two deep
    # and shapes[4] three deep.
    if len(steps) > 1:
        steps[1][1].append(dressup(None, steps[1][0], "LeadInOut"))
    if len(steps) > 2:
        steps[2][1].append(dressup(None, steps[2][0], "Dogbone"))
    if len(steps) > 3:
        lead = dressup(None, steps[3][0], "LeadInOut")
        steps[3][1].append(lead)
        steps[3][1].append(dressup(None, lead, "Dogbone"))
    if len(steps) > 4:
        lead = dressup(None, steps[4][0], "LeadInOut")
        bone = dressup(None, lead, "Dogbone")
        steps[4][1].append(lead)
        steps[4][1].append(bone)
        steps[4][1].append(dressup(None, bone, "Boundary"))

    doc.recompute()

    # Write the list the way a real job has it: one entry per step, the
    # outermost dressup where there is one.
    ordered = []
    for op, dressups in steps:
        ordered.append(dressups[-1] if dressups else op)
    cam_replay.set_operation_order(job, ordered)
    doc.recompute()

    # Every step must actually cut before it is worth replaying. A dressup that
    # constructs but yields an empty path is the documented failure mode of
    # skipping a module's setup, and it is invisible except here.
    for op, dressups in steps:
        outermost = dressups[-1] if dressups else op
        check(cam_replay.has_cutting_motion(outermost),
              "source step %s produced no cutting motion"
              % outermost.Label)
    return job, steps


def chain_of(entry):
    """The chain `entry` heads, innermost last: [operation, ...outermost]."""
    names, current, seen = [], entry, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names.append(current.Label)
        current = (getattr(current, "Base", None)
                   if cam_replay.is_dressup(current) else None)
    return list(reversed(names))


def chain_kinds(entry):
    """The chain as proxy class names, for comparing across documents where
    labels will have picked up suffixes."""
    names, current, seen = [], entry, set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        proxy = getattr(current, "Proxy", None)
        names.append(type(proxy).__name__ if proxy is not None else "?")
        current = (getattr(current, "Base", None)
                   if cam_replay.is_dressup(current) else None)
    return names


# -- the layout ----------------------------------------------------------

def build_layout(doc, shapes, thickness, sheets=1):
    """A layout whose nested parts are the same shapes the source job used.

    Several parts of each type per sheet, which is what makes the clone
    expansion observable: one source operation has to end up cutting several
    nested copies.
    """
    layout = doc.addObject("App::DocumentObjectGroup", "Layout_002")
    for name, value in (("SheetWidth", SHEET_W), ("SheetHeight", SHEET_H),
                        ("SheetThickness", thickness)):
        layout.addProperty("App::PropertyLength", name, "Layout", "")
        setattr(layout, name, value)

    # Sized so the largest part -- the 120x50 ClampPlate carrying the
    # three-deep stack -- fits at every grid position and at every angle, at
    # the largest radius from its centre. The containment check in
    # verify_replay is a real check, not decoration: an earlier grid that was
    # sized for 100 mm parts put the ClampPlate's corner outside the stock and
    # failed the sheet, which read at first like a replay fault and was not one.
    grid = []
    for row in range(2):
        for column in range(4):
            grid.append((100.0 + column * 210.0, 100.0 + row * 200.0,
                         (column * 23 + row * 11) % 90))

    groups = []
    for s in range(1, sheets + 1):
        sheet = doc.addObject("App::DocumentObjectGroup", "Sheet_%d" % s)
        layout.addObject(sheet)
        shapes_group = doc.addObject("App::DocumentObjectGroup", "Shapes_%d" % s)
        sheet.addObject(shapes_group)
        offset = 0.0 if s == 1 else 1200.0
        boundary = doc.addObject("Part::Feature", "Sheet_Boundary_%d" % s)
        boundary.Shape = Part.makePlane(SHEET_W, SHEET_H)
        boundary.Placement = FreeCAD.Placement(
            FreeCAD.Vector(offset, 0, 0), FreeCAD.Rotation())
        sheet.addObject(boundary)

        index = 0
        for label, feature in shapes:
            for cx, cy, angle in grid:
                container = doc.addObject("App::Part", "nested_%s_%d" % (label, index))
                shapes_group.addObject(container)
                part = doc.addObject("Part::Feature", "part_%s_%d" % (label, index))
                shape = feature.Shape.copy()
                shape.Placement = FreeCAD.Placement()
                part.Shape = shape
                container.addObject(part)
                container.Placement = FreeCAD.Placement(
                    FreeCAD.Vector(cx + offset, cy, 0),
                    FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), angle))
                index += 1
        doc.recompute()
        groups.append(sheet)
    return layout, groups


# -- the checks ----------------------------------------------------------

def check_read(job, steps):
    """The read half, against a job built the way a user builds one."""
    emit("\n-- read --")
    emit("Operations list: %s" % [o.Label for o in job.Operations.Group])

    recipe = cam_replay.read_recipe(job)
    emit(cam_replay.describe_recipe(recipe)[0])
    for line in cam_replay.describe_recipe(recipe)[1:]:
        emit("  %s" % line)

    check_equal(len(recipe), len(steps),
                "read %d step(s) from a %d-step job" % (len(recipe), len(steps)))
    check_equal(len(recipe.unresolved_dressups), 0,
                "unresolved dressups: %s"
                % [d.Label for d in recipe.unresolved_dressups])
    check_equal(len(recipe.normalised_entries), 0,
                "entries normalised away: %s"
                % [e.Label for e in recipe.normalised_entries])

    expected = []
    for _op, dressups in steps:
        expected.append([None] + [type(d.Proxy).__name__ for d in dressups])
    for index, item in enumerate(recipe):
        if index >= len(expected):
            break
        want = [k for k in expected[index] if k]
        got = item.dressups and [type(d.source.Proxy).__name__
                                 for d in item.dressups] or []
        check_equal(got, want, "step %d (%s) dressup stack" % (index, item.label))
    return recipe


def check_write(recipe, outcomes, steps, source_job):
    """The write half, on the jobs `replay_layout` produced."""
    emit("\n-- write --")
    source_kinds = [chain_kinds(dressups[-1] if dressups else op)
                    for op, dressups in steps]

    for outcome in outcomes:
        job = outcome.replay_job.job
        group = list(job.Operations.Group)
        label = outcome.sheet_label

        # **One entry per (source step, target part), not one per source step.**
        #
        # A source step is replayed once per part it applies to, so the list
        # holds sum(targets) entries. This fixture has five distinct shapes, so
        # every step lands on its own eight parts and there are 40 entries.
        #
        # The rule that replaces "the Nth entry is the Nth step" is: the list
        # holds exactly one entry per (source step, target part), whatever order
        # they come out in.
        #
        # It used to also require step-major order -- the source's step sequence,
        # each step repeated once per target -- and to compare entry N against
        # step N by index. Position ordering made that false on purpose: a part
        # is finished before another is started, and the parts are visited from
        # the sheet origin outwards. Comparing by index then compared unrelated
        # things and reported 11 chain-shape failures against a correct job.
        #
        # So the chains are compared as a multiset of (chain, target part) pairs,
        # which still catches a step that lost its dressup or gained one, and --
        # which a multiset of chains alone would miss -- a copy that landed on
        # the wrong part. Order is the subject of test_replay_order.py, which is
        # the only replay check whose fixture nests a part in a hole.
        clones = outcome.replay_job.clones

        # Parts are identified by label, not by `id()`. `id()` is unique, and
        # that is the problem: a failure prints the ids it found and the ids it
        # wanted, and neither says which part either of them is. Labels read.
        # The uniqueness this then relies on is checked rather than assumed.
        def label_of(target):
            return getattr(target, "Label", "?") or "?"

        labels = [label_of(c) for c in clones]
        check_equal(len(set(labels)), len(labels),
                    "nested part labels are not unique, so they cannot identify "
                    "a copy's target")

        expected = []
        unsplit = []
        for index, (op, dressups) in enumerate(steps):
            entry = dressups[-1] if dressups else op
            chain = list(chain_kinds(entry))
            base = getattr(op, "Base", None) or []
            try:
                geometry = base[0][0]
            except (TypeError, IndexError):
                geometry = None
            targets = cam_replay.clones_for_source(clones, geometry) \
                if geometry is not None else []
            # A step carrying an unsplittable dressup stays whole -- see
            # cam_replay.UNSPLITTABLE_DRESSUPS. The Boundary step therefore
            # contributes ONE entry covering all its parts, not one per part.
            blocked = any(
                cam_replay.dressup_kind(d)[1]
                in cam_replay.UNSPLITTABLE_DRESSUPS
                for d in dressups)
            if blocked and len(targets) > 1:
                unsplit.append(entry.Label)
                # One entry for all of them, so the expected target is the set
                # rather than any single part.
                expected.append((chain, frozenset(label_of(t) for t in targets)))
            else:
                expected.extend((chain, label_of(t)) for t in targets)
        if unsplit:
            emit("  left whole (unsplittable dressup): %s" % ", ".join(unsplit))

        emit("%s: %d list entr(ies) for %d step(s)"
             % (label, len(group), len(recipe)))

        check_equal(len(group), len(expected),
                    "%s: list has %d entries, expected %d (one per step per "
                    "target part)" % (label, len(group), len(expected)))

        # Chain and target together, as a multiset. Every entry must carry the
        # stack of the step it came from, and that step's copy of it.
        def pair_for(entry):
            chain = chain_kinds(entry)
            bottom = entry
            for _ in range(20):
                base = getattr(bottom, "Base", None)
                if cam_replay.is_dressup(bottom) and base is not None:
                    bottom = base
                    continue
                break
            targets = getattr(bottom, "Base", None) or []
            labels = [label_of(pair[0]) for pair in targets
                      if isinstance(pair, (list, tuple))]
            if len(labels) > 1:
                return (chain, frozenset(labels))
            return (chain, labels[0] if labels else None)

        # Chains are lists, so they are not orderable against each other; the
        # chain goes into the sort key as its own text.
        def sort_key(pair):
            chain, target = pair
            return ("/".join(map(str, chain)), str(target))

        check_equal(sorted(sort_key(p) for p in map(pair_for, group)),
                    sorted(sort_key(p) for p in expected),
                    "%s: (chain, target part) pairs over the list, as a "
                    "multiset" % label)

        for index, entry in enumerate(group):
            check(cam_replay.has_cutting_motion(entry),
                  "%s: entry %d (%s) produced no cutting motion"
                  % (label, index, entry.Label))
            # The property the split exists for: one target per entry. A copy
            # holding two parts is where the superlinear cost comes from.
            bottom = entry
            for _ in range(20):
                base = getattr(bottom, "Base", None)
                if cam_replay.is_dressup(bottom) and base is not None:
                    bottom = base
                    continue
                break
            # One target per entry -- but only for entries that ARE split
            # copies. An unsplit step's single entry legitimately covers them
            # all, and says so in its label: it has no nested part in it.
            targets = getattr(bottom, "Base", None) or []
            if "_replay_nested_" in entry.Label:
                check(len(targets) == 1,
                      "%s: entry %d (%s) is a split copy but targets %d parts"
                      % (label, index, entry.Label, len(targets)))
            elif len(targets) > 1:
                emit("  whole entry %d (%s) covers %d part(s) by design"
                     % (index, entry.Label, len(targets)))

        # Nothing in the list may be an operation that a dressup sits on. If it
        # is, the same contour is in the job twice.
        for entry in group:
            for other in group:
                if other is entry:
                    continue
                if getattr(other, "Base", None) is entry:
                    check(False,
                          "%s: %s is listed and is also wrapped by %s -- the "
                          "contour would be cut twice"
                          % (label, entry.Label, other.Label))

        check_settings_survived(outcomes, recipe, steps)
        check_source_links_are_reported(outcomes, source_job, steps)


def check_source_links_are_reported(outcomes, source_job, steps):
    """A dressup link into the SOURCE job must be REPORTED, not carried quietly.

    The rule: a replayed object may not hold a link into the job it was copied
    from. `ToolController` is handled -- captured only so the write half knows a
    tool was chosen, then remapped onto the new job's copy by
    `copy_tool_controller`. Every other job-local link has to be dealt with the
    same way.

    Boundary's `Stock` is the one that is not, and it is **known wrong rather
    than fixed**: carried verbatim it clips against the source job's stock, and
    repointing it at the replay job's stock is worse (see
    `DRESSUP_JOB_LINKS` for the measurement). So the contract this asserts is
    that the replay says so -- a warning naming the property and the object --
    rather than producing a job that looks fine and cuts to the wrong boundary.

    The consequence, which is the point of the assertion: the Boundary dressup
    is NOT safe to post from a replayed job until that is resolved. The
    warning is the mitigation, and this check is what stops the mitigation
    being quietly removed.
    """
    source_held = {id(o) for o in source_job.OutListRecursive}
    for entry in source_job.Operations.Group:
        source_held.add(id(entry))
        if cam_replay.is_dressup(entry):
            operation, _dressups = cam_replay.walk_stack(entry)
            if operation is not None:
                source_held.add(id(operation))

    examined = 0
    reported = 0
    for outcome in outcomes:
        if outcome.replay_job is None or outcome.result is None:
            continue
        for dressup in outcome.result.dressups:
            for name in dressup.PropertiesList:
                if not dressup.getTypeIdOfProperty(name).startswith(
                        "App::PropertyLink"):
                    continue
                if name in ("Base", "ToolController"):
                    continue
                value = getattr(dressup, name, None)
                if value is None or not hasattr(value, "Name"):
                    continue
                examined += 1
                if id(value) in source_held:
                    # Still pointing into the source job. That is the open
                    # question, and it must be visible.
                    text = "\n".join(outcome.result.warnings)
                    check(dressup.Label in text and name in text,
                          "%s: replayed %s.%s points into the source job and "
                          "nothing warned about it" % (outcome.sheet_label,
                                                       dressup.Label, name))
                    reported += 1
    check(examined > 0,
          "no job-local link was found on any replayed dressup, so this check "
          "examined nothing -- the Boundary branch stopped being built")
    check(reported > 0,
          "no job-local link into the source job was found, so the warning path "
          "is untested. If Boundary's Stock is ever fixed, this assertion is "
          "the one to revisit.")


def check_settings_survived(outcomes, recipe, steps):
    """The user's settings must come through, not just the structure.

    This check exists because of a bug the structural checks could not see.
    A LeadInOut constructor's `setup(obj)` binds `RadiusIn` and `RadiusOut` to
    an expression off the tool diameter, so a replay that ran it produced
    1.5x the tool radius in place of the user's 2 mm and every check above still
    passed -- the chain was right, the list was right, and the toolpath was
    present. Only the number was wrong.

    So: compare the replayed value against the source, after a recompute. The
    recompute is the part that matters, because that is when the expression wins.
    """
    for outcome in outcomes:
        job = outcome.replay_job.job
        for index, entry in enumerate(job.Operations.Group):
            chain, current, seen = [], entry, set()
            while current is not None and id(current) not in seen:
                seen.add(id(current))
                chain.append(current)
                current = (getattr(current, "Base", None)
                           if cam_replay.is_dressup(current) else None)
            # chain is outermost-first; the source step is innermost-last.
            if index >= len(recipe):
                continue
            source_specs = recipe.operations[index].dressups
            source_op = recipe.operations[index].source
            replayed_op = chain[-1]

            for name in ("Side", "Direction", "UseComp", "HandleMultipleFeatures"):
                if not hasattr(source_op, name):
                    continue
                want = getattr(source_op, name)
                got = getattr(replayed_op, name, None)
                check_equal(str(got), str(want),
                            "%s: step %d operation property %s"
                            % (outcome.sheet_label, index, name))

            if len(source_specs) != len(chain) - 1:
                continue
            # `chain` came out outermost-first and the recipe stores stacks
            # innermost-first, so the two pair up reversed. Getting this wrong
            # is not a no-op: it silently compares an inner dressup against the
            # operation, and every check then vacuously skips.
            for spec, made in zip(source_specs, reversed(chain[:-1])):
                for name in ("RadiusIn", "RadiusOut", "AngleIn", "AngleOut",
                             "StyleIn", "StyleOut"):
                    if name not in spec.properties or not hasattr(made, name):
                        continue
                    want = getattr(spec.source, name)
                    got = getattr(made, name)
                    check_equal(str(got), str(want),
                                "%s: step %d dressup %s.%s carried over"
                                % (outcome.sheet_label, index, spec.label, name))


def run():
    doc = FreeCAD.newDocument("replay_dressups")
    thickness = 2.0

    shapes = []
    for label, width, height, holes in (
            ("BottomStrap", 20.0, 80.0, ()),
            ("TopStrap", 20.0, 60.0, ((0.0, 0.0, 3.0),)),
            ("SimpleSpacer", 40.0, 40.0, ((-10.0, 0.0, 4.0), (10.0, 0.0, 4.0)))):
        shapes.append((label, box_part(doc, label, width, height,
                                       thickness, holes)))
    shapes.append(("WidePlate", box_part(doc, "WidePlate", 30.0, 100.0,
                                         thickness,
                                         ((0.0, -25.0, 4.0), (0.0, 25.0, 4.0)))))
    # A fifth part, carrying the three-deep stack. It needs two holes far enough
    # apart that the Boundary dressup has something to clip against, and it is
    # the widest of them so the boundary actually bites rather than passing the
    # whole contour through untouched -- a Boundary that clips nothing replays
    # fine and tests nothing.
    shapes.append(("ClampPlate", box_part(doc, "ClampPlate", 120.0, 50.0,
                                          thickness,
                                          ((-40.0, 0.0, 5.0), (40.0, 0.0, 5.0)))))
    doc.recompute()

    job, steps = build_source_job(doc, shapes)
    check(cam_replay.is_cam_job(job) is True, "the source job was not recognised")

    # The premise the whole read rests on, asserted on a real job rather than
    # taken on trust.
    listed = [o.Label for o in job.Operations.Group]
    emit("Operations list: %s" % listed)
    listed_ops = [o.Label for o in job.Operations.Group
                  if not cam_replay.is_dressup(o)]
    check_equal(len(listed_ops), 1,
                "expected exactly one bare operation in the list, got %s"
                % listed_ops)

    recipe = check_read(job, steps)

    layout, sheets = build_layout(doc, shapes, thickness, sheets=1)
    check_equal(len(sheets), 1, "expected 1 sheet, got %d" % len(sheets))

    found, warnings = cam_replay.resolve_layout_group(doc)
    check(found is not None, "no layout was auto-detected")
    check_equal(warnings, [], "a single layout should not warn")

    outcomes = cam_replay.replay_layout(doc, found, job)
    check_equal(len(outcomes), 1, "expected one outcome, got %d" % len(outcomes))
    if not outcomes:
        return

    outcome = outcomes[0]
    emit(cam_replay.describe_sheet_outcome(outcome)[0])
    check(outcome.ok is True, "the sheet failed: %s" % (outcome.errors,))
    check(outcome.verification is not None and outcome.verification.ok is True,
          "the sheet did not verify")
    if outcome.replay_job is None:
        check(False, "no replay job was produced")
        return

    # Recompute before comparing settings. An expression binding does not fire
    # on assignment; it fires on the next recompute, which is the only moment
    # the bug it enables is observable. Comparing before this would pass with
    # the bug present.
    doc.recompute()
    check_write(recipe, outcomes, steps, job)

    # The clone expansion is the other half of the feature, and it is only
    # observable if a source operation actually landed on several copies.
    job = outcome.replay_job.job
    for entry in job.Operations.Group:
        base = entry
        while cam_replay.is_dressup(base):
            base = base.Base
        geometry = getattr(base, "Base", None) or []
        targets = {id(pair[0]) for pair in geometry if isinstance(pair, (list, tuple))}
        check(len(targets) >= 1,
              "%s ended up on no nested geometry at all" % entry.Label)
        emit("  %-24s cuts %d nested part(s)" % (entry.Label, len(targets)))

    FreeCAD.closeDocument(doc.Name)


if __name__ in ("__main__", "test_replay_dressups"):
    status = 0
    try:
        run()
    except Exception:
        traceback.print_exc()
        _failures.append("raised")
    emit("\nreplay dressups: %d checks, %d failure(s)" % (_checks[0], len(_failures)))
    for failure in _failures:
        emit("  FAIL: %s" % failure)
    if _failures:
        status = 1
    try:
        with open(_STATUS_FILE, "w") as handle:
            handle.write(str(status))
    except OSError:
        pass
    emit("REPLAY_DRESSUP_STATUS=%d" % status)
    sys.exit(status)
