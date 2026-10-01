"""Validate that a .FCStd can drive a CAM replay, and report what it contains.

Run it against a candidate fixture before writing the real test. It answers
two questions:

  1. Does the file satisfy the structural contract the replay depends on?
  2. Does the replay, run against it, do something sensible?

It is a diagnostic, not a gate. Nothing is saved: the document is opened,
inspected, and closed. If the dry run is included the document is left mutated
in memory and simply discarded, so a crash mid-run costs nothing.

Run directly:

    freecadcmd tests/freecad_harness/validate_replay_fixture.py <file.FCStd>

Options, as environment variables because freecadcmd's own parser eats
anything starting with a dash:

    REPLAY_FIXTURE_FILE    path to the .FCStd      (or pass it as argv[1])
    REPLAY_FIXTURE_JOB     name of the CAM job to treat as the recipe
    REPLAY_FIXTURE_NOREPLAY=1   skip the dry run, structure only

Exit status: 0 when nothing failed, 1 when something did, 2 on a usage error.
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import FreeCAD

from freecad.nestingworkbench.Tools.Cam import cam_replay


# -- reporting ------------------------------------------------------------

_failures = []
_warnings = []
_notes = []


def emit(message=""):
    """Print a line in a way that survives FreeCAD's console redirect.

    Once a document exists, FreeCAD.Console captures plain print(), so output
    silently disappears. Same reason the other harness scripts use this.
    """
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


def ok(message):
    emit("  [ ok ] %s" % message)


def warn(message, detail=None):
    """Record and print a warning, with optional indented detail.

    Detail is printed but not recorded: a warning is already one line long,
    and the summary should not grow a paragraph per one.
    """
    _warnings.append(message)
    emit("  [warn] %s" % message)
    if detail:
        for line in str(detail).split("\n"):
            emit("         %s" % line)


def fail(message, detail=None):
    _failures.append(message)
    emit("  [FAIL] %s" % message)
    if detail:
        for line in str(detail).split("\n"):
            emit("         %s" % line)


def note(message):
    _notes.append(message)
    emit("  [note] %s" % message)


def heading(text):
    emit("")
    emit(text)
    emit("-" * len(text))


# -- what this validator assumes -----------------------------------------

def print_assumptions():
    """State, before checking anything, what this thinks the starting point is.

    Printed first on purpose. A misunderstanding about what the file is meant
    to contain should be visible immediately, rather than showing up later as
    a confusing structural failure.
    """
    heading("WHAT THIS VALIDATOR ASSUMES")
    emit(
        "  1. SOURCE GEOMETRY -- sketch-based PartDesign::Body objects.\n"
        "     These are what a CAM job is set up on, and what gets nested.\n"
        "\n"
        "  2. A nesting LAYOUT, i.e. the RESULT of running the nester on that\n"
        "     geometry:\n"
        "       a Layout_* group carrying SheetWidth / SheetHeight /\n"
        "       SheetThickness, and one Sheet_N group per sheet, each holding\n"
        "         - Sheet_Boundary_N : a plane whose Placement is the sheet's\n"
        "                             world origin\n"
        "         - Shapes_N         : a subgroup of nested_* App::Part\n"
        "                             containers, each holding a part_*\n"
        "                             Part::Feature with the geometry\n"
        "\n"
        "  3. A CAM JOB whose Operations.Group holds the operations and\n"
        "     dressups set up on the source bodies. That is the recipe. It\n"
        "     references the SOURCE geometry, not the nested copies.\n"
        "\n"
        "  Then: the user selects the job, the replay finds the layout\n"
        "  automatically, and applies the recipe to EVERY sheet.\n"
        "\n"
        "  Note the distinction in 1 and 2, because it is easy to get wrong and\n"
        "  the existing n70 fixture gets it wrong: that file holds three\n"
        "  PartDesign bodies and NOTHING else -- no layout, no sheets, no nested\n"
        "  parts. It is an INPUT to the nester, not a nesting result, so it\n"
        "  cannot drive a replay on its own. A replay fixture needs all three."
    )
    emit("")
    emit("  If that is not what you built, stop here and say so.")


# -- structure ------------------------------------------------------------

def validate_layout(doc):
    """Check the layout side. Returns the layout group, or None."""
    heading("LAYOUT")

    layout, layout_warnings = cam_replay.resolve_layout_group(doc)
    for warning in layout_warnings:
        warn(warning)

    if layout is None:
        # Distinguish "wrong kind of file" from "incomplete file", because the
        # n70 fixture is a perfectly good nester input and a useless replay
        # fixture, and conflating those sends people looking in the wrong place.
        bodies = [o for o in doc.Objects
                  if o.TypeId in ("PartDesign::Body", "Part::Feature",
                                  "Part::Part2DObject")]
        if bodies and not any(o.Label.startswith(("Layout_", "__temp_Layout",
                                                   "Sheet_", "nested_"))
                              for o in doc.Objects):
            fail("No layout group, but this document holds %d source object(s)."
                 % len(bodies),
                 "It looks like SOURCE GEOMETRY only -- an input to the\n"
                 "nester, not a nesting result. The n70 fixture is exactly\n"
                 "this: three PartDesign bodies and nothing else. To drive a\n"
                 "replay you also need a Layout_* group with Sheet_N groups,\n"
                 "Sheet_Boundary_N planes and nested_* containers. Run the\n"
                 "nester on this geometry and save the result alongside it.")
        else:
            fail("No layout group found.",
                 "Expected an App::DocumentObjectGroup whose Label starts "
                 "with 'Layout_' or '__temp_Layout'.")
        return None
    ok("Layout group: %r (%s)" % (layout.Label, layout.TypeId))

    width, height, thickness = cam_replay.read_sheet_dimensions(layout)
    emit("         sheet dimensions: %g x %g x %g mm" % (width, height, thickness))
    for prop in ("SheetWidth", "SheetHeight", "SheetThickness"):
        if not hasattr(layout, prop):
            warn("Layout has no %s property; defaults were used." % prop)

    from freecad.nestingworkbench.freecad_helpers import get_sheet_groups
    sheets = get_sheet_groups(layout)
    if not sheets:
        fail("Layout has no Sheet_* groups.",
             "The replay iterates Sheet_* children. Run nesting first.")
        return None
    ok("%d sheet group(s): %s" % (len(sheets), ", ".join(s.Label for s in sheets)))
    if len(sheets) < 2:
        warn("Only one sheet. The one-job-per-sheet path is then untested at scale.")

    return layout


def validate_sheet(sheet, thickness):
    """Check one sheet's structure. Returns a list of nested containers."""
    from freecad.nestingworkbench.freecad_helpers import get_nested_containers

    heading("SHEET %s" % sheet.Label)

    # -- the boundary, which is the one that is genuinely required --
    boundary = [c for c in sheet.Group if c.Label.startswith("Sheet_Boundary_")]
    if not boundary:
        fail("No Sheet_Boundary_* child.",
             "sheet_origin_for() reads the sheet's world origin from it. Without "
             "it the parts are never moved to local coordinates, so they keep "
             "their world X offset, leave the stock, and the toolpath runs off "
             "the material. This is not cosmetic.")
    else:
        origin = cam_replay.sheet_origin_for(sheet)
        ok("Sheet_Boundary_*: %r, origin (%.2f, %.2f)"
           % (boundary[0].Label, origin.x, origin.y))
        if abs(origin.x) < 1e-9 and abs(origin.y) < 1e-9:
            note("origin is (0, 0). Correct for a single sheet; later sheets "
                 "are normally offset in X.")

    containers = get_nested_containers(sheet)
    if not containers:
        fail("No nested_* containers found.",
             "get_nested_containers() looks for an App::Part labelled "
             "'nested_*' inside a 'Shapes_*' subgroup OF this sheet group. "
             "Checked %d child group(s): %s"
             % (len(sheet.Group), [c.Label for c in sheet.Group]))
        return []

    ok("%d nested container(s)" % len(containers))

    # -- per-container sanity --
    bad_type = [c.Label for c in containers if c.TypeId != "App::Part"]
    if bad_type:
        fail("Containers that are not App::Part: %s" % bad_type)

    no_part = []
    null_shape = []
    for container in containers:
        part = cam_replay.find_part_in_container(container)
        if part is None:
            no_part.append(container.Label)
            continue
        shape = getattr(part, "Shape", None)
        if shape is None or shape.isNull():
            null_shape.append(part.Label)
    if no_part:
        fail("%d container(s) have no part_* child: %s" % (len(no_part), no_part))
    if null_shape:
        fail("%d part(s) have a null shape: %s" % (len(null_shape), null_shape))

    return containers


def survey_source(doc):
    """Report the source geometry the CAM job is set up on.

    Reported unconditionally, including for a file that turns out to hold
    source geometry only. A source-only document is the n70 fixture's shape,
    and telling it what it *does* contain is more use than "no layout found".
    """
    heading("SOURCE GEOMETRY")
    # A CAM tool bit is itself a PartDesign::Body, so filter by name: a tool
    # reported as nestable source geometry is a false positive that would send
    # the fixture builder looking for a part that does not exist.
    bodies = [o for o in doc.Objects
              if o.TypeId == "PartDesign::Body"
              and "endmill" not in o.Label.lower()
              and "drill" not in o.Label.lower()
              and not o.Label.lower().startswith(("tool", "cutter"))]
    tool_bits = [o for o in doc.Objects
                 if o.TypeId == "PartDesign::Body" and o not in bodies]
    if tool_bits:
        emit("  (%d tool bit(s) excluded: %s)"
             % (len(tool_bits), [t.Label for t in tool_bits]))
    sketches = [o for o in doc.Objects
                if o.TypeId == "Sketcher::SketchObject"]
    features = [o for o in doc.Objects
                if o.TypeId == "Part::Feature"
                and not o.Label.startswith("part_")]

    if bodies:
        ok("%d PartDesign::Body: %s" % (len(bodies), [b.Label for b in bodies]))
    else:
        warn("No PartDesign::Body in this document.",
             "The fixture is meant to use sketch-based geometry, which is what "
             "real parts are. Every current check uses Part::Feature built "
             "from primitives, which has different topology.")
    emit("  %d sketch(es)" % len(sketches))
    if features:
        emit("  %d other Part::Feature(s): %s"
             % (len(features), [f.Label for f in features][:8]))
    if not bodies and not features:
        fail("No source geometry found.",
             "A replay fixture needs geometry for the CAM job to reference.")


def survey_parts(sheets_with_containers):
    """Report the part inventory, and flag the cases the replay is weakest on."""
    heading("PART INVENTORY")

    by_type = {}
    rotations = set()
    total = 0

    for sheet, containers in sheets_with_containers:
        for container in containers:
            part = cam_replay.find_part_in_container(container)
            if part is None:
                continue
            # The type is the middle of nested_<Type>_<n>, which is exactly
            # what clones_for_source parses, so report what it will see.
            pieces = container.Label.split("_")
            part_type = "_".join(pieces[1:-1]) if len(pieces) >= 3 else container.Label
            entry = by_type.setdefault(part_type, {"count": 0,
                                                   "per_sheet": {},
                                                   "containers": []})
            entry["count"] += 1
            entry["per_sheet"][sheet.Label] = \
                entry["per_sheet"].get(sheet.Label, 0) + 1
            entry["containers"].append(container)
            total += 1
            angle = container.Placement.Rotation.Angle
            rotations.add(round(__import__("math").degrees(angle) % 360, 1))

    for part_type in sorted(by_type):
        entry = by_type[part_type]
        per_sheet = ", ".join(
            "%s:%d" % (name.split("_")[-1], count)
            for name, count in sorted(entry["per_sheet"].items()))
        emit("  %-20s %3d   (%s)" % (part_type, entry["count"], per_sheet))
    emit("  %d part(s) across %d type(s)" % (total, len(by_type)))

    if not by_type:
        return {}

    # -- the gaps this fixture is meant to close --
    over_ten = {t: e["count"] for t, e in by_type.items() if e["count"] > 10}
    if over_ten:
        ok("Crosses the 10-per-type boundary: %s"
           % ", ".join("%s (%d)" % (t, c) for t, c in sorted(over_ten.items())))
    else:
        warn("No part type has more than 10 copies.",
             "Label uniquification only starts to matter at 10+, which is where "
             "findObjects(Label=...) over-matched. This fixture will not "
             "exercise that.")

    underscored = [t for t in by_type if "_" in t]
    if underscored:
        ok("Part type(s) with an underscore in the name: %s" % ", ".join(underscored))
    else:
        note("No part type has an underscore in its name. That case is verified "
             "in the pytest tier but not here.")

    if len(rotations) > 1:
        ok("Distinct rotations present: %d" % len(rotations))
    else:
        warn("Every part has the same rotation. Rotated geometry is the normal "
             "case and exercises the placement arithmetic.")

    return by_type


def survey_holes(sheets_with_containers):
    """Report which parts have holes, and whether any could host a nested part.

    This is the expensive part -- part_footprint is ~15 ms per part -- and it is
    reported with timing because its cost at n70's scale is currently assumed
    rather than known.
    """
    heading("INTERNAL FEATURES")

    parts = []
    for _sheet, containers in sheets_with_containers:
        for container in containers:
            part = cam_replay.find_part_in_container(container)
            if part is not None:
                parts.append(part)

    start = time.perf_counter()
    footprints = {}
    for part in parts:
        footprint = cam_replay.part_footprint(part)
        if footprint is not None:
            footprints[id(part)] = footprint
    elapsed = time.perf_counter() - start

    with_holes = {i: f for i, f in footprints.items() if f.interiors}
    emit("  %d part(s), %d footprint(s) in %.0f ms (%.1f ms each)"
         % (len(parts), len(footprints), 1000 * elapsed,
            1000 * elapsed / max(len(parts), 1)))
    emit("  %d part(s) have internal features" % len(with_holes))
    if not with_holes:
        warn("No part has an internal feature.",
             "Holes and slots are what the hole-nesting ordering and the "
             "interior/exterior distinction are about.")

    # Circular vs not: the current harness only ever uses circles.
    circular = 0
    non_circular = 0
    for footprint in with_holes.values():
        for ring in footprint.interiors:
            coords = list(ring.coords)
            xs = [c[0] for c in coords]
            ys = [c[1] for c in coords]
            width = max(xs) - min(xs)
            height = max(ys) - min(ys)
            if width > 1e-6 and abs(width - height) < 1e-3:
                circular += 1
            else:
                non_circular += 1
    emit("  interior rings that look circular: %d, non-circular: %d"
         % (circular, non_circular))
    if non_circular == 0 and circular:
        note("All internal features look circular. Slots and rectangular "
             "pockets are a different ring count and sub-element set, and are "
             "untested outside the pytest tier.")

    # Would anything actually host a nested part?
    start = time.perf_counter()
    nestings = cam_replay.find_hole_nestings(parts)
    nest_elapsed = time.perf_counter() - start
    emit("  find_hole_nestings: %.0f ms, %d nesting(s) found"
         % (1000 * nest_elapsed, len(nestings)))
    if not nestings:
        note("No hole nesting found, so the ordering step will not reorder "
             "anything in this fixture.")

    if parts:
        projected = 1000 * (nest_elapsed + elapsed) * 122 / max(len(parts), 1)
        emit("  extrapolated to 122 parts: ~%.0f ms" % projected)

    return with_holes


# -- the CAM job ----------------------------------------------------------

def _nested_type(container):
    """Return the part type encoded in `container`'s label, or None.

    The middle of `nested_<type>_<n>` -- the same parse `clones_for_source`
    performs, and deliberately not `findObjects(Label=...)`, which is a prefix
    match and so would return `nested_Bracket_1` for `nested_Bracket_10`.
    """
    pieces = container.Label.split("_")
    if len(pieces) < 3:
        return None
    return "_".join(pieces[1:-1])


def _containers_for_type(containers, source_label):
    """Containers whose label claims to be copies of `source_label`.

    This is a claim made by the layout's naming, not a result. What the replay
    actually does is decided by `clones_for_source` against real job clones,
    which is checked in `check_identity_against_replay` once the dry run has
    produced them. The two are kept apart deliberately -- see the note on that
    function for what happens when they are confused.
    """
    return [c for c in containers
            if _nested_type(c) is not None and _nested_type(c) == source_label]


def _diagnose_no_operations(doc, job, recipe):
    """Say WHY a job has no operations, when the reason is diagnosable.

    "No operations" on its own sends the reader hunting. The common cause is
    specific and easy to fix: the operations exist in the document, each dressup
    points at one, but they were never added to `Operations.Group` -- which is
    what actually defines a job's process order. Adding a dressup puts the
    *dressup* in the group, so it is easy to end up with a group of dressups
    and no operations, each pointing at something real that nothing lists.

    Checked rather than asserted, because the opposite case -- no operations
    anywhere -- is a genuinely different problem with a different fix.
    """
    by_label = {o.Label: o for o in doc.Objects}

    dressup_bases = {}
    for dressup in recipe.unresolved_dressups:
        base = getattr(dressup, "Base", None)
        if base is not None:
            dressup_bases[getattr(base, "Label", "?")] = dressup.Label

    in_group = {getattr(o, "Label", "?")
                for o in (getattr(job.Operations, "Group", []) or [])}
    present = sorted(label for label in dressup_bases if label in by_label)

    if present:
        fail("The job has no operations in Operations.Group, but %d operation "
             "object(s) exist in this document and are wrapped by a dressup."
             % len(present),
             "The operations are not in the job's process order, so the replay "
             "has nothing to read.\n"
             "  present but unlisted: %s\n"
             "  Operations.Group currently holds: %s\n"
             "\n"
             "Fix: add each operation to the job, e.g.\n"
             "  job.Proxy.addOperation(doc.getObject('%s'))\n"
             "and put the dressups after the operation they wrap. In the CAM "
             "workbench, creating the operation first and then dressing it up "
             "produces the right order."
             % (", ".join(present), sorted(in_group) or "nothing",
                present[0]))
    else:
        fail("The job has no operations, and none are wrapped by a dressup "
             "either.",
             "Found %d dressup(s) whose base is missing. The job needs at "
             "least one operation to replay." % len(recipe.unresolved_dressups))


def validate_job(doc, wanted_name=None, all_containers=()):
    """Check the CAM job and inventory the recipe. Returns the job, or None."""
    heading("CAM JOB")

    jobs = [o for o in doc.Objects if cam_replay.is_cam_job(o)]
    if not jobs:
        fail("No CAM job in this document.",
             "The replay reads Operations.Group from a job the user selects. "
             "is_cam_job() wants a Path::Feature carrying Operations, Model "
             "and Stock.")
        return None, []

    for job in jobs:
        emit("  found job %r (%s)" % (job.Label, job.TypeId))

    if wanted_name:
        matching = [j for j in jobs if j.Label == wanted_name]
        if not matching:
            fail("No job named %r. Candidates: %s"
                 % (wanted_name, [j.Label for j in jobs]))
            return None, []
        job = matching[0]
    else:
        job = jobs[0]
        if len(jobs) > 1:
            warn("%d jobs present; using %r. Set REPLAY_FIXTURE_JOB to choose."
                 % (len(jobs), job.Label))

    ok("Using job %r" % job.Label)

    recipe = cam_replay.read_recipe(job)
    if not len(recipe):
        _diagnose_no_operations(doc, job, recipe)
        return job, []
    if recipe.unresolved_dressups:
        warn("%d dressup(s) did not resolve to an operation: %s"
             % (len(recipe.unresolved_dressups),
                [getattr(d, "Label", "?") for d in recipe.unresolved_dressups]),
             "A dressup whose stack does not bottom out in an operation cannot "
             "be replayed. Check the Base link, and note that Base is a single "
             "link, not a link-sub-list.")

    emit("  Operations.Group, in order:")
    group = getattr(job.Operations, "Group", []) or []
    order = [o.Label for o in group]
    emit("    %s" % order)
    for entry in group:
        if cam_replay.is_dressup(entry):
            emit("      %-22s dressup, wraps %s"
                 % (entry.Label, getattr(entry.Base, "Label", "?")))
        else:
            emit("      %-22s operation, %s"
                 % (entry.Label, type(entry.Proxy).__module__))

    emit("  recipe as read:")
    for item in recipe:
        subs = sorted({s for _g, subs_list in item.base_entries
                       for s in subs_list})
        emit("    %-22s %d property/-ies, %d base entry/-ies, subs=%s"
             % (item.label, len(item.properties), len(item.base_entries),
                subs if subs else "whole object"))
        if not subs:
            note("%s selects the WHOLE object, so its holes and its external "
                 "boundary are not separable. Ordering treats it as "
                 "hole-cutting, which is the safe direction." % item.label)

    # -- identity: does each op's geometry correspond to a nested part type? --
    #
    # This is the cross-check that matters most, because it is the one place
    # the replay can be confidently wrong without anything raising. An
    # operation whose source part has no matching nested parts still replays;
    # clones_for_source falls back to "every clone", so the operation would cut
    # the wrong feature on parts it was never meant to touch, and every
    # downstream check would pass.
    #
    # What is reported here is a NAMING claim: this many containers are called
    # nested_<this part>_<n>. It is the cheapest thing available without running
    # the replay, and it is what the structure-only mode can honestly say.
    #
    # It used to report something else and wrong. This called
    # `clones_for_source([stub], geometry)` per container, and that function
    # returns *every* clone when it cannot narrow -- a deliberate safety
    # property, documented at length, and correct for the replay. Called with a
    # one-element stub it can never narrow, so it always returned that one
    # element, which is truthy, so the `or` short-circuited and the real label
    # match was never consulted. Every operation reported the total part count:
    # 48 for all 7 on the tracked fixture, where the truth is 23, 23 and 2.
    #
    # The check that matters therefore never ran, in a file whose comment
    # claimed it was the one that mattered most. What the replay really does is
    # checked separately, in check_identity_against_replay.
    emit("  identity cross-check (operation -> source part -> nested parts "
         "by name):")
    identity_by_operation = []
    for item in recipe:
        for geometry, _subs in item.base_entries:
            original = cam_replay.resolve_source_object(geometry) or geometry
            label = getattr(original, "Label", "?")
            containers = _containers_for_type(all_containers, label)
            identity_by_operation.append((item, label, len(containers)))
            if containers:
                emit("    %-22s -> %r -> %d nested part(s) by name"
                     % (item.label, label, len(containers)))
            else:
                fail("Operation %r references source part %r, which has no "
                     "nested part named for it." % (item.label, label),
                     "No container is labelled nested_%s_<n>. The replay would "
                     "fall back to targeting every clone, cutting a feature on "
                     "parts it was not set up for -- visibly, in the toolpath, "
                     "rather than as an error.\n"
                     "The nested label must be nested_<that part's label>_<n>."
                     % label)
    if all_containers and not identity_by_operation:
        warn("No operation carries a Base, so identity cannot be checked.",
             "Operations that select the whole object still replay; they just "
             "cannot be cross-checked against the nested naming.")

    if recipe.normalised_entries:
        note("%d list entr(ies) normalised away: %s"
             % (len(recipe.normalised_entries),
                [getattr(e, "Label", "?") for e in recipe.normalised_entries]))
        emit("  (an operation listed separately from a dressup layered on it is "
             "read as part of that dressup's step, so it is not cut twice -- "
             "this is normal, and is what the CAM workbench produces)")

    for item in recipe:
        stack = item.dressup_labels
        if stack:
            emit("    %-22s dressup stack: %s" % (item.label, " -> ".join(stack)))
        for spec in item.dressups:
            kind, module = cam_replay.dressup_kind(spec.source)
            if kind == "known":
                verdict = "replayable"
            elif kind == "unsupported":
                verdict = "NOT replayed -- %s" % \
                    cam_replay.DRESSUP_UNSUPPORTED.get(module, "no reason given")
            else:
                verdict = "unrecognised proxy %r" % (module,)
            emit("      %-22s %-12s %s" % (spec.label, kind, verdict))

    tools = getattr(job, "Tools", None)
    if tools is not None and getattr(tools, "Group", None):
        ok("%d tool controller(s): %s"
           % (len(tools.Group), [t.Label for t in tools.Group]))
    else:
        warn("The job has no tool controller, so operations replay without a tool.")

    return job, identity_by_operation


# -- dry run --------------------------------------------------------------

def check_identity_against_replay(outcome, identity_by_operation):
    """Check what the replay ACTUALLY targeted, against the real job clones.

    This is the check the naming cross-check cannot be, and the one that matters.

    `clones_for_source` is given a real list of clones here, not a per-container
    stub, so its "return everything rather than guess narrow" fallback is
    visible instead of hidden: when it fires, the match count equals the total
    clone count, and that is the case worth failing on, because it means the
    operation is about to cut a feature on every part on the sheet.

    The expected count comes from the naming cross-check, so the two are
    independent: naming says how many containers *claim* to be this part, and
    this says how many clones the replay *chose*.
    """
    replay = outcome.replay_job
    total = len(replay.clones)
    if not total:
        return

    # What the naming cross-check expected, keyed by the SOURCE operation's
    # label. The replay appends `_replay` to each label, so the pairing strips
    # that; it is a name, not a position, because position assumes an ordering
    # the two lists need not share.
    expected_by_label = {}
    for item, label, expected in identity_by_operation:
        expected_by_label.setdefault(item.label, (label, expected))

    emit("")
    emit("    identity, as the replay resolved it (%d clone(s) on the sheet):"
         % total)
    for operation in outcome.result.operations:
        name = operation.Label
        if name.endswith("_replay"):
            name = name[:-len("_replay")]
        source = expected_by_label.get(name)
        targets = list(getattr(operation, "Base", None) or [])
        nested_kinds = set()
        for pair in targets:
            geometry = pair[0] if isinstance(pair, (list, tuple)) else pair
            original = getattr(geometry, "Objects", None)
            flattened = original[0] if original else None
            nested = getattr(flattened, cam_replay.PROP_NESTED_LABEL, "")
            if nested:
                pieces = nested.split("_")
                if len(pieces) >= 3:
                    nested_kinds.add("_".join(pieces[1:-1]))
        emit("      %-22s targets %2d of %d clone(s), nested types: %s"
             % (operation.Label, len(targets), total,
                ", ".join(sorted(nested_kinds)) or "(none)"))

        if len(targets) == total and total > 1:
            if len(nested_kinds) > 1 or not nested_kinds:
                fail("%s: %s targets ALL %d nested parts."
                     % (outcome.sheet_label, operation.Label, total),
                     "clones_for_source could not narrow this operation to the "
                     "part type it was set up on, and fell back to every clone. "
                     "The toolpath would cut this feature on parts it was never "
                     "meant for. Check that the operation's source part is named "
                     "the same way its nested containers are "
                     "(nested_<part label>_<n>).")
            else:
                note("%s: %s targets all %d parts, which is correct -- there is "
                     "only one part type on this sheet." %
                     (outcome.sheet_label, operation.Label, total))
        elif not targets:
            note("%s: %s ended up on no nested part at all." %
                 (outcome.sheet_label, operation.Label))
        elif source is not None:
            # Naming and reality disagree. The naming cross-check counted the
            # containers that CLAIM to be this part type; this counts the clones
            # the replay actually built. They should match, and when they do not,
            # something between the two lost parts -- a container skipped during
            # flattening, a null shape, a part the flattener could not seat.
            #
            # What this deliberately does NOT catch, because nothing can: an
            # operation whose Base has been re-pointed at the wrong source part.
            # Both checks read the operation's current geometry, so a re-pointed
            # operation is consistently wrong in both places and the two agree.
            # The replay is faithful to its input, and the document does not
            # record what the input was meant to be. The report above is what
            # makes that visible -- a human compares the part type against the
            # source parts -- but no automated check can assert the intent.
            _expected_label, expected = source
            if expected and len(targets) != expected:
                fail("%s: %s targets %d clone(s), but %r has %d nested part(s) "
                     "by name."
                     % (outcome.sheet_label, operation.Label, len(targets),
                        _expected_label, expected),
                     "The layout names %d container(s) for this part type and "
                     "the replay built %d clone(s) for it. Something dropped "
                     "parts in between -- check for containers with no part_* "
                     "child or a null shape, which flatten_container records in "
                     "result.skipped."
                     % (expected, len(targets)))


def dry_run(doc, layout, job, identity_by_operation=()):
    """Actually run the replay, and report what it did."""
    heading("REPLAY DRY RUN")
    if layout is None or job is None:
        warn("Skipped: no layout or no job.")
        return

    start = time.perf_counter()
    try:
        outcomes = cam_replay.replay_layout(doc, layout, job)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        fail("replay_layout raised: %s" % exc)
        return
    elapsed = time.perf_counter() - start

    for outcome in outcomes:
        emit("")
        emit("  %s" % outcome.sheet_label)
        for line in cam_replay.describe_sheet_outcome(outcome):
            emit("    %s" % line)

        if outcome.replay_job is not None and outcome.result is not None:
            replay = outcome.replay_job
            ok("stock %g x %g x %g mm"
               % (replay.stock.Length, replay.stock.Width, replay.stock.Height))
            # Checked against the ACTUAL stock, not a hardcoded range. The
            # first version asserted X 0..300 and reported four parts outside
            # a 400 mm sheet that were plainly inside it.
            for label in cam_replay.parts_outside_stock(replay.clones, replay.stock):
                fail("%s: %s is outside the stock" % (outcome.sheet_label, label))
            for operation in outcome.result.operations:
                if not cam_replay.has_cutting_motion(operation):
                    fail("%s: %s has no cutting motion"
                         % (outcome.sheet_label, operation.Label))
            if identity_by_operation:
                check_identity_against_replay(outcome, identity_by_operation)

    emit("")
    emit("  replay wall clock: %.0f ms" % (1000 * elapsed))
    emit("  (nothing was saved; the document is discarded)")


# -- main -----------------------------------------------------------------

def main(path):
    print_assumptions()

    heading("FILE")
    if not os.path.isfile(path):
        emit("  [FAIL] not a file: %s" % path)
        return 2
    emit("  %s" % path)
    emit("  %d bytes" % os.path.getsize(path))

    doc = FreeCAD.openDocument(path)
    emit("  opened %r: %d object(s)" % (doc.Name, len(doc.Objects)))

    layout = validate_layout(doc)

    sheets_with_containers = []
    if layout is not None:
        from freecad.nestingworkbench.freecad_helpers import get_sheet_groups
        _w, _h, thickness = cam_replay.read_sheet_dimensions(layout)
        for sheet in get_sheet_groups(layout):
            containers = validate_sheet(sheet, thickness)
            if containers:
                sheets_with_containers.append((sheet, containers))

    # Unconditional: a source-only document should still be told what it holds.
    survey_source(doc)

    if sheets_with_containers:
        survey_parts(sheets_with_containers)
        survey_holes(sheets_with_containers)
    else:
        heading("PART INVENTORY")
        fail("No nested parts found, so there is nothing to replay onto.")

    all_containers = [c for _sheet, containers in sheets_with_containers
                      for c in containers]
    job, identity_by_operation = validate_job(
        doc, os.environ.get("REPLAY_FIXTURE_JOB") or None, all_containers)

    if not os.environ.get("REPLAY_FIXTURE_NOREPLAY"):
        dry_run(doc, layout, job, identity_by_operation)
    elif identity_by_operation:
        heading("IDENTITY, AS THE REPLAY WOULD RESOLVE IT")
        warn("Skipped: the dry run is what produces the job clones.",
             "The naming cross-check above reports how many containers claim to "
             "be each operation's part type. Whether clones_for_source then "
             "narrows to them -- or falls back to every clone, and cuts the "
             "feature on parts it was never set up for -- is only observable "
             "with a real clone list, so it needs the dry run.")

    heading("SUMMARY")
    emit("  %d failure(s), %d warning(s), %d note(s)"
         % (len(_failures), len(_warnings), len(_notes)))
    if _failures:
        emit("")
        for failure in _failures:
            emit("  FAIL: %s" % failure)
    if _warnings:
        emit("")
        for warning in _warnings:
            emit("  WARN: %s" % warning)
    if not _failures:
        emit("")
        emit("  Structurally usable as a replay fixture.")

    FreeCAD.closeDocument(doc.Name)
    return 1 if _failures else 0


if __name__ in ("__main__", "validate_replay_fixture"):
    # Under freecadcmd, sys.argv is [freecadcmd, <script>, <user args...>]
    # and __name__ is the script basename without its extension. So the user's
    # path is argv[2], not argv[1]: taking argv[1] made the validator open
    # itself, which FreeCAD reports as an iostream error on a .py.
    _args = sys.argv[2:] if len(sys.argv) > 2 else []
    _path = os.environ.get("REPLAY_FIXTURE_FILE", "") or (_args[0] if _args else "")
    if not _path:
        emit("usage: REPLAY_FIXTURE_FILE=<path> freecadcmd "
             "validate_replay_fixture.py")
        emit("   or: freecadcmd validate_replay_fixture.py <path>")
        sys.exit(2)
    try:
        _status = main(_path)
    except Exception:
        import traceback
        traceback.print_exc()
        emit("validator raised")
        _status = 2
    sys.exit(_status)
