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


def run_job_checks(doc, flattened, thickness):
    """Build a real job from the flattened parts and check what comes back.

    This is the step the pytest tier cannot cover, because the behaviour under
    test is `PathJob.Create` refusing to keep the geometry it is given. It
    replaces the input features with `draftobjects.clone.Clone` objects, and
    the clones are what an operation's `Base` must reference. Verified: three
    inputs became `['Clone', 'Clone001', 'Clone002']` labelled `Model-p1` ..
    `Model-p3`, with `Model.Group[0] is not parts[0]`.

    Also checked here, because both are real behaviours rather than documented
    ones:

      * the default stock is a `StockFromBase` fitted to the model, which
        measures Z -7.0 .. 1.0 for a 6 mm part -- a millimetre of margin
        either side. The replay stock must be the sheet exactly, -6.0 .. 0.0,
        because operation depths are derived from the stock and this is the
        only place the Z frame is decided;
      * the stock frame comparison warns about that difference rather than
        refusing to run, since the cut is still correct against the new stock.
    """
    from Path.Main import Job as PathJob

    parts = [item.obj for _label, _container, _part, item in flattened]

    # A source job with the DEFAULT stock, which is what a user who set up CAM
    # on their own part will have.
    src = doc.addObject("Part::Feature", "SourceBracket")
    src.Shape = Part.makeBox(40, 25, thickness, FreeCAD.Vector(-20, -12.5, -thickness))
    source_job = PathJob.Create("SourceJob", [src], None)
    doc.recompute()
    source_frame = cam_replay.describe_stock_frame(source_job.Stock)
    check(source_frame is not None, "the source job has no stock frame")
    if source_frame:
        check(abs(source_frame[2] - thickness) > 0.5,
              "expected the default stock to be padded around the model, got "
              "Z %s..%s for a %s mm part"
              % (source_frame[0], source_frame[1], thickness))

    layout = doc.addObject("App::DocumentObjectGroup", "Layout_1")
    for name, value in (("SheetWidth", 600.0), ("SheetHeight", 400.0),
                        ("SheetThickness", thickness)):
        layout.addProperty("App::PropertyLength", name, "Layout", "")
        setattr(layout, name, value)
    doc.recompute()

    check(cam_replay.read_sheet_dimensions(layout) == (600.0, 400.0, thickness),
          "layout dimensions did not read back")

    sheet = doc.addObject("App::DocumentObjectGroup", "Sheet_1")

    replay = cam_replay.create_replay_job(
        doc, layout, sheet, parts, source_job=source_job
    )
    check(replay is not None, "create_replay_job returned None")
    if replay is None:
        return

    # -- the clones are what an operation must target --
    check(len(replay.clones) == len(parts),
          "expected %d clones, got %d" % (len(parts), len(replay.clones)))
    check(replay.clones != replay.source_parts,
          "clones are the same objects as the inputs; the job did not clone")
    for clone, original in zip(replay.clones, replay.source_parts):
        check(clone is not original,
              "%s: the job kept the input object instead of cloning it"
              % original.Label)
        check(clone.isDerivedFrom("Part::FeaturePython"),
              "%s: clone is a %s" % (original.Label, clone.TypeId))
        ob, nb = original.Shape.BoundBox, clone.Shape.BoundBox
        check(abs(ob.XMin - nb.XMin) < 1e-6 and abs(ob.YMin - nb.YMin) < 1e-6,
              "%s: clone placement differs from the original" % original.Label)

    # -- the stock is the sheet, and only the sheet --
    stock = replay.stock
    check(stock is not None, "no stock on the replay job")
    if stock is None:
        return
    check(abs(float(stock.Length) - 600.0) < 1e-6,
          "stock Length is %s, expected 600" % stock.Length)
    check(abs(float(stock.Width) - 400.0) < 1e-6,
          "stock Width is %s, expected 400" % stock.Width)
    check(abs(float(stock.Height) - thickness) < 1e-6,
          "stock Height is %s, expected %s" % (stock.Height, thickness))

    frame = cam_replay.describe_stock_frame(stock)
    check(frame is not None, "the replay stock has no shape")
    if frame:
        zmin, zmax, height = frame
        check(abs(zmin + thickness) < 1e-6,
              "stock bottom is Z=%s, expected %s" % (zmin, -thickness))
        check(abs(zmax) < 1e-6,
              "stock top is Z=%s, expected 0 so Z0 is the top of the stock" % zmax)
        check(abs(height - thickness) < 1e-6,
              "stock is %s mm thick, expected %s" % (height, thickness))

    # -- the parts sit on the stock --
    for clone in replay.clones:
        bb = clone.Shape.BoundBox
        check(abs(bb.ZMin + thickness) < 1e-6,
              "%s: bottom at Z=%s, expected %s" % (clone.Label, bb.ZMin, -thickness))
        check(abs(bb.ZMax) < 1e-6,
              "%s: top at Z=%s, expected 0" % (clone.Label, bb.ZMax))

    # -- the Z frame difference is reported, not fatal --
    check(len(replay.warnings) > 0,
          "expected a warning: the source job's default stock is padded around "
          "the model, so its Z frame differs from the replay stock")
    check("re-derived" in "\n".join(replay.warnings),
          "the Z frame warning does not explain that depths are re-derived")
    check(all(w.startswith("Sheet_1") for w in replay.warnings),
          "warnings are not attributed to the sheet: %s" % replay.warnings)

    # -- and the parts are inside the stock --
    sb = stock.Shape.BoundBox
    for clone in replay.clones:
        bb = clone.Shape.BoundBox
        inside = (bb.XMin >= sb.XMin - 1e-6 and bb.XMax <= sb.XMax + 1e-6
                  and bb.YMin >= sb.YMin - 1e-6 and bb.YMax <= sb.YMax + 1e-6)
        check(inside,
              "%s (X %s..%s Y %s..%s) is not inside the sheet (X %s..%s Y %s..%s)"
              % (clone.Label, bb.XMin, bb.XMax, bb.YMin, bb.YMax,
                 sb.XMin, sb.XMax, sb.YMin, sb.YMax))

    lines = cam_replay.describe_replay_job(replay)
    check(all(isinstance(line, str) for line in lines),
          "describe_replay_job returned non-strings")


def run_replay_checks(doc):
    """Replay a real recipe into a real job and check the toolpaths.

    Takes its own document. Sharing one with the flatten checks made FreeCAD
    uniquify the source part to `Bracket001` while the hand-written nested
    labels still said `Bracket`, and the identity matching then correctly found
    no match -- which looked like a code bug and was a fixture bug.

    This is the step the whole feature exists for, and it is the first place
    the read half and the write half meet, so it is worth checking end to end
    rather than in pieces.

    The source job is built the way a user would build one: a Profile on the
    top face with a LeadInOut dressup, and a Drilling op for the hole. Two
    part types, so the identity matching in `clones_for_source` is exercised
    as well as the general case.
    """
    from Path.Main import Job as PathJob
    from Path.Op import Profile as PathProfile
    from Path.Op import Drilling as PathDrilling
    from Path.Dressup.Gui.LeadInOut import ObjectDressup

    doc = FreeCAD.newDocument("replay_ops")
    thickness = 6.0

    def make_source(name, cx, cy, w, h, hr):
        obj = doc.addObject("Part::Feature", name)
        shape = Part.makeBox(w, h, thickness, FreeCAD.Vector(-w / 2, -h / 2, -thickness))
        shape = shape.cut(Part.makeCylinder(
            hr, thickness, FreeCAD.Vector(cx - w / 4, cy, -thickness)))
        obj.Shape = shape
        return obj

    # -- the user's source job, on two different part types --
    bracket = make_source("Bracket", 0, 0, 40, 25, 3)
    spacer = make_source("Spacer", 0, 0, 30, 30, 2)
    source_job = PathJob.Create("UserJob", [bracket, spacer], None)
    doc.recompute()
    model = source_job.Model.Group

    def top_faces(target):
        return ["Face%d" % (i + 1) for i, f in enumerate(target.Shape.Faces)
                if type(f.Surface).__name__ == "Plane"
                and abs(f.CenterOfMass.z) < 1e-9]

    prof = PathProfile.Create("Profile", None, source_job)
    doc.recompute()
    prof.Base = [(model[0], top_faces(model[0]))]
    prof.HandleMultipleFeatures = "Individually"
    prof.Side = "Outside"
    prof.Direction = "CW"
    prof.UseComp = True
    prof.ToolController = source_job.Tools.Group[0]
    doc.recompute()

    drill = PathDrilling.Create("Drilling", None, source_job)
    doc.recompute()
    drill.Base = [(model[1], [""])]
    drill.ToolController = source_job.Tools.Group[0]
    doc.recompute()

    dressup = doc.addObject("Path::FeaturePython", "DressupLeadInOut")
    ObjectDressup(dressup, prof)
    source_job.Proxy.addOperation(dressup, prof)
    dressup.LeadIn = True
    dressup.LeadOut = True
    dressup.StyleIn = "Arc"
    dressup.StyleOut = "Line"
    dressup.RadiusIn = 5
    dressup.RadiusOut = 7
    dressup.AngleIn = 90
    dressup.AngleOut = 45
    doc.recompute()

    emit("source Operations.Group: %s" % [o.Label for o in source_job.Operations.Group])
    check(len(prof.Path.Commands) > 5, "the source Profile produced no path")
    check(len(drill.Path.Commands) > 2, "the source Drilling produced no path")

    # -- a nest of both part types --
    # Shapes_1 must be a child of the Sheet group: get_nested_containers looks
    # for a Shapes_* subgroup inside the sheet, not beside it.
    sheet = doc.addObject("App::DocumentObjectGroup", "Sheet_1")
    shapes = doc.addObject("App::DocumentObjectGroup", "Shapes_1")
    sheet.addObject(shapes)

    def nest(label, source_shape, cx, cy, angle):
        # Named the way the nester names them: nested_<source label>_<n>. The
        # source label is read back rather than assumed, because FreeCAD may
        # have uniquified it.
        label = "%s_%d" % (getattr(label, "Label", "Part"), nest.counter)
        nest.counter += 1
        container = doc.addObject("App::Part", "nested_" + label)
        shapes.addObject(container)
        part = doc.addObject("Part::Feature", "part_" + label)
        shape = source_shape.copy()
        shape.Placement = FreeCAD.Placement()
        part.Shape = shape
        container.addObject(part)
        container.Placement = FreeCAD.Placement(
            FreeCAD.Vector(cx, cy, 0), FreeCAD.Rotation(FreeCAD.Vector(0, 0, 1), angle))
        doc.recompute()
        return container, part

    nest.counter = 1
    nest(bracket, bracket.Shape, 40.0, 40.0, 0.0)
    nest(bracket, bracket.Shape, 140.0, 40.0, 37.0)
    nest(spacer, spacer.Shape, 40.0, 120.0, 25.0)
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
        return

    replay = cam_replay.create_replay_job(
        doc, layout, sheet, flattened.parts, source_job=source_job)
    check(replay is not None, "create_replay_job returned None")
    if replay is None:
        return

    recipe = cam_replay.read_recipe(source_job)
    check(len(recipe) == 2,
          "expected 2 operations in the recipe, got %d" % len(recipe))
    check(len(recipe.unresolved_dressups) == 0,
          "the recipe left %d dressup unresolved" % len(recipe.unresolved_dressups))

    result = cam_replay.replay_recipe(recipe, replay.job, replay.clones)
    doc.recompute()

    emit("replayed: %r" % result)
    check(len(result.failures) == 0,
          "replay reported failures: %s" % result.failures)
    check(len(result.operations) == 2,
          "expected 2 replayed operations, got %d" % len(result.operations))
    check(len(result.dressups) == 1,
          "expected 1 replayed dressup, got %d" % len(result.dressups))
    if len(result.failures) or len(result.operations) != 2:
        return

    # -- every replayed operation produced a real toolpath --
    for new_op in result.operations:
        check(new_op.Path is not None and len(new_op.Path.Commands) > 5,
              "%s: replayed path is empty (%s commands)"
              % (new_op.Label, len(new_op.Path.Commands) if new_op.Path else 0))

    # -- the dressup picked up its lead-in --
    dress = result.dressups[0]
    base = dress.Base
    check(dress.StyleIn == "Arc" and dress.StyleOut == "Line",
          "dressup styles did not carry over: %s / %s" % (dress.StyleIn, dress.StyleOut))
    check(abs(float(dress.RadiusIn) - 5.0) < 1e-6 and abs(float(dress.RadiusOut) - 7.0) < 1e-6,
          "dressup radii did not carry over: %s / %s" % (dress.RadiusIn, dress.RadiusOut))
    check(len(dress.Path.Commands) >= len(base.Path.Commands),
          "the dressup produced fewer commands than the op it wraps (%d < %d)"
          % (len(dress.Path.Commands), len(base.Path.Commands)))

    # -- the tool was copied, not linked across --
    for new_op in result.operations:
        tc = getattr(new_op, "ToolController", None)
        check(tc is not None, "%s has no tool controller after replay" % new_op.Label)
        if tc is not None:
            check(tc in replay.job.Tools.Group,
                  "%s: the tool controller is not in the replay job's tool table"
                  % new_op.Label)
            check(all(tc is not source_job.Tools.Group[0]
                      for _ in [0]),
                  "%s: the replayed op still points at the SOURCE job's tool"
                  % new_op.Label)

    # -- the identity matching kept the part types apart --
    # Asked for the whole clone list, not one clone at a time: the fallback for
    # an unmatched source is "every clone", so asking about a single clone
    # always answers yes and matches nothing. An earlier version of this check
    # did that and reported 3 Brackets in a nest of 2.
    brackets = cam_replay.clones_for_source(replay.clones, model[0])
    spacers = cam_replay.clones_for_source(replay.clones, model[1])
    check(len(brackets) == 2,
          "expected 2 clones to match Bracket, got %d" % len(brackets))
    check(len(spacers) == 1,
          "expected 1 clone to match Spacer, got %d" % len(spacers))

    profile_op = [o for o in result.operations if "Profile" in o.Label][0]
    profile_targets = {id(entry[0]) for entry in profile_op.Base}
    bracket_ids = {id(c) for c in brackets}
    check(profile_targets == bracket_ids,
          "the Profile should target only the Bracket clones: %d targets, "
          "%d brackets" % (len(profile_targets), len(bracket_ids)))

    # -- the toolpaths cover the nested parts --
    for new_op in result.operations:
        xs = [c.X for c in new_op.Path.Commands if getattr(c, "X", None) is not None]
        if not xs:
            continue
        targets = {id(entry[0]) for entry in new_op.Base}
        boxes = [replay.clones.index(t).__class__ for t in targets if t in replay.clones]
        emit("  %-24s ncmds=%-4d X %s..%s" % (
            new_op.Label, len(new_op.Path.Commands),
            round(min(xs), 2), round(max(xs), 2)))
        check(max(xs) > 20, "%s: the toolpath does not reach past the first part"
              % new_op.Label)

    # -- the replayed cut is the SAME cut, just moved --
    #
    # Present-and-non-empty is a weak claim. This compares the actual motion:
    # the source Profile's path length must equal the replayed Profile's, since
    # the nested copies are the same part at the same scale. If the replay
    # silently cut a different feature, the lengths would differ.
    def path_length(op, cutting_only=True):
        """Length of a toolpath in mm.

        Counts cutting moves (G1/G2/G3) by default. Including G0 rapids
        measures the distance *between* parts, which scales with how the nest
        is laid out rather than with what was cut -- the first version of this
        check included them and reported 343.73 mm for a path that cuts
        288.87 mm.
        """
        total = 0.0
        last = None
        for cmd in op.Path.Commands:
            text = str(cmd)
            motion = text.split()[1] if len(text.split()) > 1 else ""
            if cutting_only and motion not in ("G1", "G2", "G3"):
                if motion.startswith("G0"):
                    last = None
                continue
            x, y = getattr(cmd, "X", None), getattr(cmd, "Y", None)
            if x is None or y is None:
                last = None
                continue
            if last is not None:
                total += ((x - last[0]) ** 2 + (y - last[1]) ** 2) ** 0.5
            last = (x, y)
        return total

    profile_replay = [o for o in result.operations if "Profile" in o.Label][0]
    source_len = path_length(prof)
    replay_len = path_length(profile_replay)
    n_bracket_targets = sum(1 for entry in profile_replay.Base
                            if entry[0] in brackets)
    emit("  profile path length: source %.2f mm, replay %.2f mm over %d part(s)"
         % (source_len, replay_len, n_bracket_targets))
    check(abs(replay_len - source_len * n_bracket_targets) < 1.0,
          "replayed profile path is %.2f mm; %d brackets of a %.2f mm source "
          "path would be %.2f mm"
          % (replay_len, n_bracket_targets, source_len,
             source_len * n_bracket_targets))

    # -- and the drill only ever touched the spacer --
    drill_replay = [o for o in result.operations if "Drilling" in o.Label][0]
    check(all(entry[0] in spacers for entry in drill_replay.Base),
          "the replayed Drilling op targets something other than the spacer")

    run_verification_checks(doc, replay, result)

    # -- nothing is empty, and the report says so --
    report = "\n".join(cam_replay.describe_replay_result(result))
    check("All sub-element selections resolved" in report,
          "the report does not state that the sub-element check passed: %s" % report)
    emit(report)


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
    # Positions must be inside the sheet. The sheet is
    # Polygon([(0,0), (width,0), (width,height), (0,height)]) and the nester
    # requires full containment (Sheet.is_placement_valid), so a part centred
    # on the origin would hang off the corner. An earlier version of this
    # script did exactly that and reported it as a stock bug.
    specs = [
        # label,          cx,   cy,  angle, child yaw
        ("Bracket_1", 30.0, 30.0, 0.0, 0.0),
        ("Bracket_2", 90.0, 42.0, 37.0, 0.0),
        ("Bracket_3", 30.0, 100.0, 71.0, 25.0),
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

    run_job_checks(doc, flattened, thickness)

    run_replay_checks(doc)
    run_ordering_checks(None)

    FreeCAD.closeDocument("replay_ops")
    FreeCAD.closeDocument("replay_flatten")


def run_ordering_checks(doc):
    """Nest a part inside another's hole and check the operation order.

    This is the one case where the replay must NOT simply reproduce the user's
    order. A plug sitting in a plate's hole is held by the ring of material
    around it; cut the hole first and the plug falls out, and every operation
    after that is cutting a loose piece.

    The check builds exactly that, then asserts the plate's hole-cutting
    operation ends up after the plug's.

    It also asserts the negative: that the footprint test is specific to holes.
    A part sitting *beside* the plate, not in it, must not read as nested --
    which is what makes the test a real check rather than a tautology.
    """
    thickness = 6.0
    doc = FreeCAD.newDocument("replay_order")

    plate = doc.addObject("Part::Feature", "Plate")
    plate_shape = Part.makeBox(80, 80, thickness,
                               FreeCAD.Vector(-40, -40, -thickness))
    plate_shape = plate_shape.cut(Part.makeCylinder(
        25, thickness, FreeCAD.Vector(0, 0, -thickness)))
    plate.Shape = plate_shape

    plug = doc.addObject("Part::Feature", "Plug")
    plug.Shape = Part.makeBox(20, 20, thickness, FreeCAD.Vector(-10, -10, -thickness))
    doc.recompute()

    # -- the footprints, and that the test is specific to holes --
    plate_fp = cam_replay.part_footprint(plate)
    plug_fp = cam_replay.part_footprint(plug)
    check(plate_fp is not None, "no footprint for the plate")
    check(plug_fp is not None, "no footprint for the plug")
    if plate_fp is None or plug_fp is None:
        FreeCAD.closeDocument("replay_order")
        return

    check(len(plate_fp.interiors) == 1,
          "expected the plate to have 1 hole, got %d" % len(plate_fp.interiors))
    check(len(plug_fp.interiors) == 0,
          "the plug should have no holes")

    # World coordinates: a shape spanning X -40..40 must keep those bounds.
    # This is the trap that makes the naive approach wrong.
    check(abs(plate_fp.bounds[0] + 40) < 1.0,
          "the plate footprint is not in world coordinates: bounds %s"
          % (tuple(round(v, 1) for v in plate_fp.bounds),))

    nestings = cam_replay.find_hole_nestings([plate, plug])
    check(nestings == [(plate, plug)],
          "expected the plug to be found inside the plate's hole, got %d pair(s)"
          % len(nestings))

    # A part beside the plate, not in it, must not be reported as nested.
    beside = doc.addObject("Part::Feature", "Beside")
    beside.Shape = Part.makeBox(20, 20, thickness, FreeCAD.Vector(60, 0, -thickness))
    doc.recompute()
    beside_fp = cam_replay.part_footprint(beside)
    if beside_fp is not None:
        outside = cam_replay.find_hole_nestings([plate, beside])
        check(outside == [],
              "a part beside the plate was wrongly reported as nested in its hole")
    emit("  nestings found: %d" % len(nestings))

    # -- the operation order, on a real job --
    from Path.Main import Job as PathJob
    from Path.Op import Drilling as PathDrilling
    from Path.Op import Profile as PathProfile

    source_job = PathJob.Create("OrderJob", [plate, plug], None)
    doc.recompute()
    model = source_job.Model.Group

    # The plate's hole, as a drilling op; the plate's outline, as a profile.
    # Created hole-first so the source order is deliberately wrong.
    plate_drill = PathDrilling.Create("PlateHole", None, source_job)
    doc.recompute()
    plate_drill.Base = [(model[0], [""])]
    plate_drill.ToolController = source_job.Tools.Group[0]
    doc.recompute()

    plug_profile = PathProfile.Create("PlugOutline", None, source_job)
    doc.recompute()
    plug_profile.Base = [(model[1], [""])]
    plug_profile.Side = "Outside"
    plug_profile.Direction = "CW"
    plug_profile.UseComp = True
    plug_profile.ToolController = source_job.Tools.Group[0]
    doc.recompute()

    source_order = [o.Label for o in source_job.Operations.Group]
    emit("  source order: %s" % source_order)
    check(source_order.index("PlateHole") < source_order.index("PlugOutline"),
          "the source order was expected to be wrong for this test to mean "
          "anything, got %s" % source_order)

    # Stand in for the replay: the same two operations, same ownership.
    replayed_plate_op = plate_drill
    replayed_plug_op = plug_profile
    operations = [replayed_plate_op, replayed_plug_op]
    ownership = {replayed_plate_op: plate, replayed_plug_op: plug}

    ordered = cam_replay.order_operations(
        source_job, operations, nestings, ownership)
    new_order = [o.Label for o in ordered]
    emit("  ordered:      %s" % new_order)
    check(new_order.index("PlateHole") > new_order.index("PlugOutline"),
          "the plate's hole must be cut after the plug, got %s" % new_order)

    # -- and the negative: no nestings means no reordering --
    class _Job:
        def __init__(self, group):
            self.Operations = type("_O", (), {"Group": list(group)})()

    untouched = _Job(operations)
    same = cam_replay.order_operations(untouched, operations, [], ownership)
    check(same == operations,
          "with no nestings the order must be left exactly as it was")
    check(untouched.Operations.Group == operations,
          "with no nestings the job's group must not be reassigned")

    # -- a cycle must not hang --
    # Each part treated as nesting inside the other: unsatisfiable.
    both = [(plate, plug), (plug, plate)]
    cyclic = cam_replay.order_operations(_Job(operations), operations, both, ownership)
    check(len(cyclic) == len(operations),
          "a cyclic constraint dropped operations")

    FreeCAD.closeDocument("replay_order")


def run_verification_checks(doc, replay, result):
    """Verify the replayed job produced real cuts, and that the checks bite.

    A healthy replay must pass cleanly. Then the checks are shown to actually
    fire when they should, because a verification that has only ever passed is
    indistinguishable from one that never works.
    """
    verification = cam_replay.verify_replay(result, replay.warnings)

    check(len(verification.failures) == 0,
          "a healthy replay reported failures: %s" % verification.failures)
    check(verification.operations_checked == len(result.operations),
          "checked %d of %d operations"
          % (verification.operations_checked, len(result.operations)))
    check(verification.operations_with_motion == verification.operations_checked,
          "%d of %d operations had no cutting motion"
          % (verification.operations_with_motion, verification.operations_checked))
    check(verification.ok is True, "verification.ok is False on a clean replay")

    # The Z frame warning is carried through, not swallowed.
    check(any("spans Z" in w or "thick" in w for w in verification.warnings),
          "the stock frame warning did not reach the verification: %s"
          % verification.warnings)

    for operation in result.operations:
        uncovered = cam_replay.uncovered_targets(operation)
        check(uncovered == [],
              "%s: no toolpath near %s" % (operation.Label, uncovered))

    # -- the emptiness check must actually fire --
    class _Empty:
        Label = "EmptyOp"

        class Path:
            Commands = []

    empty_result = cam_replay.ReplayResult()
    empty_result.operations = [_Empty()]
    empty_verification = cam_replay.verify_replay(empty_result)
    check(empty_verification.ok is False,
          "an operation with no path did not fail the verification")
    check(len(empty_verification.failures) == 1,
          "expected exactly one failure, got %s" % empty_verification.failures)
    check("no cutting motion" in empty_verification.failures[0],
          "the failure does not explain itself: %s" % empty_verification.failures[0])

    # -- the coverage check must fire, and only warn --
    stray = doc.addObject("Part::Feature", "Stray")
    stray.Shape = Part.makeBox(10, 10, 6, FreeCAD.Vector(0, 0, -6))
    doc.recompute()

    good = result.operations[0]
    saved_base = good.Base
    try:
        good.Base = list(saved_base) + [(stray, [""])]
        warn_result = cam_replay.ReplayResult()
        warn_result.operations = [good]
        warn_verification = cam_replay.verify_replay(warn_result)
        check(warn_verification.ok is True,
              "partial coverage failed the verification; it must only warn")
        check(any("Stray" in w for w in warn_verification.warnings),
              "partial coverage did not warn about the stray part: %s"
              % warn_verification.warnings)
        check(any("approximate" in w for w in warn_verification.warnings),
              "the coverage warning does not say it is approximate")
    finally:
        good.Base = saved_base
        doc.recompute()

    lines = cam_replay.describe_verification(verification)
    check(all(isinstance(line, str) for line in lines),
          "describe_verification returned non-strings")
    emit("\n".join("  " + line for line in lines))


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
