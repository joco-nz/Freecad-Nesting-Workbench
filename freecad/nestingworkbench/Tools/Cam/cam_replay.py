# SPDX-License-Identifier: LGPL-2.1-or-later
"""
Read a CAM job's operations and dressups into a replayable recipe.

Background
----------
A user sets up CAM on their source parts in the CAM workbench: Profile and
Drilling operations, LeadInOut dressups, per-feature settings. The nesting
workbench then lays those parts out on a sheet. This module is the read half of
carrying that setup across to the nested copies.

It classifies nothing. The user has already decided which features belong in
which operation; that work is done, in the CAM workbench, before the nesting
ever runs. Re-deriving it here would mean inventing a second definition of
"a hole" and a second definition of "an internal feature" and hoping the two
agreed. So the recipe is read as authored.

What a recipe is
----------------
A source job's `Operations.Group` is a flat, ordered list holding both
operations and the dressups wrapped around them. A dressup's `Base` points at
the operation it modifies; an operation's `Base` is a list of (geometry,
sub-elements) pairs. Those two shapes are what the reader tells apart.

The read produces, per operation, in group order: the captured properties, and
the dressups attached to it. Ordering is preserved because the group order is
the processing order, and the user chose it deliberately -- see the ordering
notes on `Recipe` for why that is not the same as the order the ops appear in.

Constraints this module exists to respect
-----------------------------------------
1. **Dressups are told apart by property type, not by inspection.** A dressup's
   `Base` is a single `App::PropertyLink` (not subscriptable); an operation's is
   an `App::PropertyLinkSubList` (a Python list of tuples). Subscripting a
   dressup's `Base` raises `TypeError: 'FeaturePython' object is not
   subscriptable` -- the first version of this reader did exactly that.

2. **The group is not the dependency order.** Observed on a real job:
   `['DressupLeadInOut', 'Profile', 'Drilling']`. The dressup sits *before* the
   operation it wraps. A reader that walked the group once and built the
   attachment as it went raised `KeyError`. Hence `Recipe` attaches in a second
   pass, and `read_recipe` reports an unresolvable dressup rather than losing it.

3. **The source job's Model entries are clones, and the clone hides custom
   properties.**
   `PathJob.Create` replaces base geometry with `draftobjects.clone.Clone`
   objects, and a custom property added to the original (`MyMeta`) is *not*
   visible on the clone. So the geometry the operations reference is the clone,
   while the identity a human recognises is the original's label. Both are
   recorded: `Base` targets the clone, and `SourceObject` is recovered from
   `clone.Objects[0]` so the write half can match nested copies by something
   more reliable than a label.

   The replay job does not repeat that. `adopt_flattened_parts_as_model` puts
   the flattened parts into the Model and drops the Clones `PathJob.Create`
   wrapped around them, so a replayed job's geometry is a plain
   `Part::Feature` with nothing behind it and no custom properties to hide. The
   `SourceObject` / `SourceContainer` links survive on it, which is what the
   write half reads; `nested_label_of` reads them from either kind of entry.

4. **Labels cannot be trusted for lookups, though they cannot collide.**
   `findObjects(Label="nested_Bracket_1")` returns `_1`, `_10` *and* `_11` --
   it is a prefix match, and the 122-part corpus is exactly where that shows up.
   Separately, duplicate labels cannot actually happen: assigning the same
   Label twice yields `Bracket` and `Bracket001`, even with the
   `DuplicateLabels` preference set to 0. So the hazard is over-matching, not
   collision, and the fix is to carry an explicit identity rather than to
   search for labels. Every flattened part therefore carries `SourceObject` and
   `SourceContainer` links.

5. **A container placement is invisible to CAM.**
   An operation whose `Base` points at a child inside an `App::Part` produced a
   toolpath at the child's *local* coordinates -- `[-22.5, 22.5]` where world
   placement put it at `~[118, 160]`. Nested parts cannot be referenced where
   they sit, so they have to be flattened to top-level objects first. This is
   what `flatten_sheet` is for.

6. **`transformGeometry` destroys the geometry the toolpath is cut from.**
   It converts analytic surfaces to B-splines: a `Cylinder` face came back as a
   `BSplineSurface` with area 314.1593 -> 315.0023. Lead-in and lead-out arc
   maths run against the real surface, so a splined cylinder means the toolpath
   follows an approximation. `flatten_container` assigns a `Placement` instead,
   which leaves a `Cylinder` a `Cylinder` -- and, because the topology is left
   untouched, keeps the sub-element names an operation's `Base` refers to
   pointing at the same features. A nested copy has identical topology to its
   source: same edge count, same per-edge length and curve type.

7. **Depths are derived, not authored.** An operation recomputes
   `StartDepth`/`FinalDepth` from the stock box and the geometry on every
   recompute, so setting them has no lasting effect and capturing them is
   harmless. The consequence is that the Z frame is enforced by the stock
   rather than carried from the user's part, which makes it a single place to
   get right.

Scope
-----
Two halves, and the line between them is where a document gets written.

The recipe reader (`read_recipe` and what it calls) touches no geometry and
creates no objects, which is what makes it testable under plain pytest with
stand-in objects. The flattener (`flatten_sheet` and what it calls) does
create objects, and is covered by the freecadcmd harness instead.
"""
import math
import time

import FreeCAD

# -- job creation ---------------------------------------------------------
#
# The job is named after the sheet it machines, so a multi-sheet nest produces
# one clearly-labelled job per sheet rather than a single ambiguous one.

JOB_NAME_PREFIX = "CAM_Replay_"

#: Job properties that describe the *setup* rather than the geometry, and are
#: therefore copied from the source job rather than created fresh.
#:
#: `Machine` is the one that matters. FreeCAD derives the post processor from
#: it, so a job that has lost its Machine has lost the answer to "what
#: post-processor does this need?" even when its own `PostProcessor` is empty
#: and consistent. Measured on the tracked fixture:
#:
#:     source   PostProcessor=''   Machine='Origarmi Plasma'
#:     replayed PostProcessor=''   Machine=''
#:
#: and nothing warned, because `Machine` is in neither
#: `NON_REPLAYABLE_PROPERTIES` nor anything else the replay touched.
#: Job properties the replay does NOT copy, and why.
#:
#: A hand-picked list of what to copy is the same mistake as a hand-picked list
#: of dressups: it will be incomplete, and nothing notices. The first version of
#: this named four properties and lost `OrderOutputBy`, which is the order the
#: post processor emits operations in -- the source said `Operation` (the job's
#: process order) and the replay said `Fixture`. Everything else it did copy
#: looked right only because the two jobs happened to agree on the defaults.
#:
#: So the rule is inverted: copy every job property, and skip these.
#:
#:   * the replayed job's own structure. It has its own Model, Operations,
#:     SetupSheet, Stock and Tools, built for this sheet; the source's point at
#:     the source geometry, and copying them would rewire the new job to the old
#:     one's parts.
#:
#:   * computed or derived values. `CycleTime` is a measurement, `Path` is an
#:     accumulated toolpath, `LastPostProcess*` records a post that has not
#:     happened. Copying any of them states something untrue about the new job.
#:
#:   * identity. `Label` is set deliberately from the source and the sheet, and
#:     `Proxy` is the job's own brain.
#:
#:   * presentation. `Visibility` is deliberately NOT copied: the source job is
#:     usually hidden, and the point of the replayed one is that the user can
#:     see what was made.
JOB_PROPERTIES_NOT_COPIED = frozenset((
    "Group", "Model", "Operations", "SetupSheet", "Stock", "Tools",
    "Proxy",
    "CycleTime", "Path", "LastPostProcessDate", "LastPostProcessOutput",
    "Label", "Label2", "ExpressionEngine",
    "Visibility", "_ElementMapVersion", "_GroupTouched",
))


def job_settings_to_copy(source_job, job):
    """Return the property names worth carrying from `source_job` to `job`.

    Every property both objects have, minus `JOB_PROPERTIES_NOT_COPIED`. Sorted,
    so a report over it is stable.
    """
    if source_job is None:
        return []
    names = set(getattr(source_job, "PropertiesList", ()) or ())
    names &= set(getattr(job, "PropertiesList", ()) or ())
    return sorted(names - JOB_PROPERTIES_NOT_COPIED)


def replay_job_name(source_job, sheet_label):
    """Return the name for a replayed job.

    `<source>_Replay_<sheet>`, so the tree shows which job a replayed one came
    from. The sheet label has to stay in the name: `_UNVERIFIED` is appended to
    it on failure, and the harness asserts the job names its sheet, so anything
    that drops the sheet from the label breaks both.
    """
    source = getattr(source_job, "Label", "") if source_job is not None else ""
    source = source or "CAM"
    if sheet_label:
        return "%s_Replay_%s" % (source, sheet_label)
    return "%s_Replay" % source


def remove_tool_controllers(job, controllers):
    """Remove `controllers` from `job`'s tool table. Returns their labels.

    Split out from `prune_unused_tool_controllers` because the replay now needs
    to remove a *known* set of controllers at a known moment -- the ones the
    fresh job arrived with -- rather than discovering the unused ones once the
    operations exist to be inspected.

    **Labels are read before anything is removed.** This is not a style point.
    `_remove_with_orphans` deletes the controller, and reading `Label` off a
    deleted FreeCAD object raises `ReferenceError: Cannot access attribute
    'Label' of deleted object` -- which `getattr(controller, "Label", "?")` does
    *not* catch, because `getattr`'s default only swallows `AttributeError`. The
    exception escaped the whole prune, so the step aborted half-done and the
    console said only "Could not tidy the tool table". Measured: exactly that,
    on the fixture, every run.
    """
    if not controllers:
        return []
    tools = getattr(job, "Tools", None)
    doc = getattr(job, "Document", None)

    removed = []
    for controller in list(controllers):
        # First, while the object can still answer.
        try:
            label = controller.Label
        except Exception:
            label = "?"

        # `Tools` is a group, and a group's own removal recurses into what it
        # holds. That matters because a tool bit is not one object: a
        # controller links a Part::FeaturePython wrapper, which wraps a
        # PartDesign::Body, which carries an Attributes object. Walking that
        # chain by hand was tried and is wrong in a way that is easy to miss --
        # removing the controller leaves the wrapper claimed by nothing, and
        # every layer then sits at the document root looking like unrelated
        # debris. Measured, in order: the wrapper, then the Body, then
        # Attributes, each appearing only after the one before it was pruned.
        try:
            tools.removeObject(controller)
        except Exception:
            try:
                list(getattr(tools, "Group", []) or []).remove(controller)
            except Exception:
                continue
        # FreeCAD will not do this in one call, and the order matters more than
        # it looks. Measured:
        #
        #   Tools.removeObject(controller)  drops it from the group but leaves
        #                                     the object in the document, still
        #                                     claiming its tool bit;
        #   doc.removeObject(bit)           is refused-ish while the bit is still
        #                                     claimed, and the bit's own body
        #                                     then survives.
        #
        # So: the controller goes first, then its bit, then whatever the bit
        # orphaned, until nothing more is left claiming. The chain depth is
        # discovered rather than assumed, because it is FreeCAD's shape and it
        # differs between tool bits.
        _remove_with_orphans(doc, controller)
        removed.append(label)
    return removed


def prune_unused_tool_controllers(job):
    """Remove tool controllers in `job` that no operation references.

    `PathJob.Create` gives a new job a default `TC: 5mm Endmill`, and the
    replay then copies the user's own controller in alongside it. Nothing
    references the endmill, and it is not merely untidy: the job's SetupSheet
    resolves the active tool from the controllers present, so a replayed job
    offered a machine head the operations never use.

    Only a controller that nothing references is removed, and only when at
    least one controller *is* referenced -- so a replay that failed before any
    operation got a tool cannot end up with no tools at all.

    This is the safety net. The default is normally gone before the first
    operation is created, because leaving it there is what makes the SetupSheet
    prompt; see `remove_tool_controllers` and its call site.

    Returns the labels removed, for the report.
    """
    tools = getattr(job, "Tools", None)
    group = list(getattr(tools, "Group", None) or [])
    if len(group) < 2:
        return []

    used = set()
    for obj in _job_objects(job):
        controller = getattr(obj, "ToolController", None)
        if controller is not None:
            used.add(id(controller))

    if not used:
        return []

    unused = [c for c in group if id(c) not in used]
    return remove_tool_controllers(job, unused)


def _remove_with_orphans(doc, obj, _depth=0):
    """Remove `obj` from the document, then anything left claiming nothing.

    A tool bit is not one object: the controller links a
    `Part::FeaturePython` wrapper, which wraps a `PartDesign::Body`, which
    carries an `Attributes` object. Removing the controller leaves the wrapper
    at the document root, and removing that leaves the Body, and each one
    arrives at the root in turn looking like unrelated debris. The only safe
    order is outside-in, and the only reliable way to get the order right is to
    re-check rather than assume a depth.
    """
    if doc is None or obj is None:
        return
    try:
        links = [o for o in obj.OutList
                 if o is not None and hasattr(o, "Name")]
    except Exception:
        links = []
    try:
        doc.removeObject(obj.Name)
    except Exception:
        return
    if _depth > 8:                       # a cycle would otherwise spin
        return
    for child in links:
        try:
            if child.InList:
                continue
        except Exception:
            continue
        _remove_with_orphans(doc, child, _depth + 1)


def _job_objects(job):
    """Every object in `job` worth checking for a job-local link.

    The Operations group and, through it, the operations underneath any
    dressups. Not the whole document: a link from something the replay did not
    create is not the replay's business, and the document holds the source job
    and the layout too.
    """
    seen = set()
    pending = [job]
    group = getattr(getattr(job, "Operations", None), "Group", None) or []
    pending.extend(group)
    model = getattr(getattr(job, "Model", None), "Group", None) or []
    pending.extend(model)
    out = []
    while pending:
        obj = pending.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        out.append(obj)
        base = getattr(obj, "Base", None)
        if base is not None and not isinstance(base, (list, tuple)):
            pending.append(base)
    return out


def copy_job_setup(source_job, job):
    """Copy the source job's settings onto `job`, and report what moved.

    The replay reproduces a configuration, so the settings come from the job
    being replayed rather than from a dialog asking again. Asking again can
    only produce a second answer to a question already answered, and it will
    sometimes be a wrong one: FreeCAD's own Job builds its `PostProcessor`
    enumeration from `allEnabledLegacyPostProcessors()` while
    `allEnabledPostProcessors()` is the more obvious-looking call, and the
    difference includes `monokrom_plasma` and `generic_plasma`. Choosing from
    the wrong list yields a value the job then refuses.

    Every property is attempted, minus `JOB_PROPERTIES_NOT_COPIED`, rather than
    a named few. A property that fails to copy is reported rather than skipped,
    because a job that quietly kept a default where the source had a setting
    produces different G-code and says nothing about it.
    """
    copied, failed = [], []
    for name in job_settings_to_copy(source_job, job):
        try:
            setattr(job, name, getattr(source_job, name))
            copied.append(name)
        except Exception as exc:
            failed.append("%s (%s: %s)" % (name, type(exc).__name__, exc))
            FreeCAD.Console.PrintWarning(
                "Could not copy %s from the source job: %s\n" % (name, exc))
    return copied, failed

STOCK_LABEL_PREFIX = "Stock_Replay_"

# Tolerance for comparing two stock Z frames, in millimetres. Loose enough to
# absorb the float noise a Placement round-trip introduces, tight enough that
# a 1 mm difference -- which is what a default StockFromBase against an
# explicit box actually produces -- is still reported.
STOCK_FRAME_TOLERANCE = 0.01


def read_sheet_dimensions(layout_group):
    """Return `(width, height, thickness)` from a layout group's properties.

    The dimensions live on the `Layout_*` group, not on the individual
    `Sheet_*` groups, so the layout group is what has to be passed in. Falls
    back to `cam_manager`'s defaults when a property is absent, so an older
    document without them still produces a usable job rather than raising.

    `height` is the layout's `SheetHeight`; the naming asymmetry between the
    property names and the stock's own `Length`/`Width`/`Height` is the
    layout's, not this module's.
    """
    from ...constants import (
        PROP_SHEET_HEIGHT,
        PROP_SHEET_THICKNESS,
        PROP_SHEET_WIDTH,
    )

    defaults = (600.0, 600.0, 3.0)
    if layout_group is None:
        return defaults
    values = []
    for prop, fallback in zip(
        (PROP_SHEET_WIDTH, PROP_SHEET_HEIGHT, PROP_SHEET_THICKNESS), defaults
    ):
        value = getattr(layout_group, prop, None)
        try:
            values.append(float(value) if value is not None else fallback)
        except (TypeError, ValueError):
            values.append(fallback)
    return tuple(values)


def sheet_origin_for(sheet_group):
    """Return the world-space X/Y offset a sheet's parts were placed at.

    Sheets are laid out side by side, so sheet 2's parts sit at a non-zero
    world X. Each sheet's G-code should start at X0 Y0, so the origin is
    subtracted from the parts and the stock is built at the origin -- the same
    choice `cam_manager` makes.

    Returns a zero vector when the sheet has no `Sheet_Boundary_*` child, which
    is the case for documents that predate that structure.
    """
    origin = FreeCAD.Vector(0, 0, 0)
    if sheet_group is None:
        return origin
    for child in getattr(sheet_group, "Group", ()):
        if getattr(child, "Label", "").startswith("Sheet_Boundary_"):
            placement = getattr(child, "Placement", None)
            if placement is not None:
                return FreeCAD.Vector(placement.Base.x, placement.Base.y, 0)
    return origin


def translate_to_sheet_local(parts, sheet_origin):
    """Shift `parts` back to sheet-local coordinates, in place.

    Placement-only, so the geometry itself is untouched and stays analytic.
    The sheet origin is zero for the first sheet, so this is a no-op there.
    """
    if not sheet_origin:
        return parts
    dx, dy = -sheet_origin.x, -sheet_origin.y
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return parts
    for part in parts:
        placement = getattr(part, "Placement", None)
        if placement is None:
            continue
        base = placement.Base
        part.Placement = FreeCAD.Placement(
            FreeCAD.Vector(base.x + dx, base.y + dy, base.z),
            placement.Rotation,
        )
    return parts


class ReplayJob:
    """A new job, plus what the write half needs to target it correctly.

    The important field is `clones` -- the objects an operation's `Base` must
    point at. It is read back out of `job.Model.Group` rather than taken from
    the caller's list, because `PathJob.Create` decides what actually ends up in
    the Model: given three features it produced `['Clone', 'Clone001',
    'Clone002']` labelled `Model-p1` .. `Model-p3`, with
    `Model.Group[0] is not parts[0]`. Building an operation against the input
    list instead would quietly produce a job whose geometry it does not contain.

    `adopt_flattened_parts_as_model` then puts the flattened parts themselves
    there and drops those Clones, so in practice `clones` and the caller's
    `parts` are the same objects. The field kept the name because it is the
    list everything downstream targets; the caller's own list is not kept, so
    that nothing can target it by accident.

    `warnings` is a list of human-readable strings, currently the Z frame
    comparison described on `compare_stock_frames`.
    """

    def __init__(self, job, clones, stock, sheet_group, warnings):
        self.job = job
        self.clones = list(clones)
        self.stock = stock
        self.sheet_group = sheet_group
        self.warnings = list(warnings)

    def __repr__(self):
        return "<ReplayJob %s model=%d stock=%s>" % (
            getattr(self.job, "Label", "?"), len(self.clones),
            getattr(self.stock, "Label", "none"),
        )


def describe_stock_frame(stock):
    """Return `(z_min, z_max, height)` for a stock object, or None.

    A stock with no shape -- which is possible before it is built -- yields None
    rather than raising, so a frame comparison can be skipped.
    """
    shape = getattr(stock, "Shape", None)
    if shape is None or shape.isNull():
        return None
    box = shape.BoundBox
    return (box.ZMin, box.ZMax, box.ZLength)


def compare_stock_frames(source_stock, new_stock, sheet_label=""):
    """Warn when the source job's stock frame differs from the new one.

    Returns a list of warning strings, empty when the frames agree.

    This is a warning rather than an error because the consequence is subtle
    rather than fatal. Depths are derived by the operation from the stock on
    every recompute, so the replayed operations will cut correctly against the
    new stock whatever the source used. What differs is what the *user saw*
    while setting up: the depths displayed in the source job were derived
    against the source's stock.

    The mismatch is not hypothetical. A default `StockFromBase` around a 6 mm
    part measured Z -7.0 .. 1.0 -- a millimetre of margin above and below --
    while the nesting stock is built at exactly -6.0 .. 0.0. A user who
    checked their depths against the source job is looking at a different
    frame from the one their parts will be cut in.
    """
    warnings = []
    source_frame = describe_stock_frame(source_stock)
    new_frame = describe_stock_frame(new_stock)
    prefix = ("%s: " % sheet_label) if sheet_label else ""

    if new_frame is None:
        return ["%sthe new stock has no shape, so the Z frame could not be "
                "checked." % prefix]
    if source_frame is None:
        return warnings

    source_zmin, source_zmax, source_height = source_frame
    new_zmin, new_zmax, new_height = new_frame

    if abs(source_height - new_height) > STOCK_FRAME_TOLERANCE:
        warnings.append(
            "%ssource job stock is %s mm thick but the sheet is %s mm; the "
            "replayed operations will cut to the sheet thickness."
            % (prefix, round(source_height, 3), round(new_height, 3))
        )
    if (abs(source_zmin - new_zmin) > STOCK_FRAME_TOLERANCE
            or abs(source_zmax - new_zmax) > STOCK_FRAME_TOLERANCE):
        warnings.append(
            "%ssource job stock spans Z %s..%s but the replay stock spans "
            "Z %s..%s; depths are re-derived from the stock, so the cut is "
            "correct, but they will not match the values shown in the source "
            "job." % (
                prefix,
                round(source_zmin, 3), round(source_zmax, 3),
                round(new_zmin, 3), round(new_zmax, 3),
            )
        )
    return warnings


def adopt_flattened_parts_as_model(doc, job, parts):
    """Put `parts` into `job.Model.Group` in place of the Clones over them.

    `PathJob.Create` does not keep the geometry it is handed: it wraps each
    item in a `draftobjects.clone.Clone` and puts *those* in the Model, leaving
    the originals as unreferenced strays. A Clone is a link, so the flattened
    parts then have to outlive the job -- which is why they could not be
    removed, and why a per-sheet 48-object group sat in the document.

    But the Clone buys nothing. CAM reads a shape and resolves sub-element
    names against it; it does not care that the shape arrived by reference.
    Measured: a plain `Part::Feature` in `Model.Group`, with a Profile over it,
    produces 17 commands and 10 cutting moves, and survives with nothing behind
    it.

    So the originals are promoted into the Model and the Clones are dropped.
    That removes a whole set of objects, a whole group, and the link that made
    the originals untouchable.

    Refuses rather than half-does. If any Model entry is not a Clone pointing
    at one of `parts`, nothing is swapped and the job is left exactly as
    `PathJob.Create` built it -- a job with working Clones is untidy, a job with
    half of them replaced is broken.
    """
    clones = list(getattr(getattr(job, "Model", None), "Group", None) or [])
    if not clones or not parts:
        return 0, "no model entries to swap"

    wanted = {id(p) for p in parts}
    pairs = []
    for clone in clones:
        upstream = getattr(clone, "Objects", None)
        if not upstream or id(upstream[0]) not in wanted:
            return 0, ("model entry %r is not a clone of a flattened part"
                       % getattr(clone, "Label", "?"))
        pairs.append((clone, upstream[0]))

    model = job.Model
    for clone, part in pairs:
        try:
            model.removeObject(clone)
        except Exception:
            pass
        for group in [g for g in part.InList
                      if getattr(g, "TypeId", "") == "App::DocumentObjectGroup"]:
            try:
                group.removeObject(part)
            except Exception:
                pass
        model.addObject(part)
    doc.recompute()
    for clone, _part in pairs:
        try:
            doc.removeObject(clone.Name)
        except Exception:
            pass
    doc.recompute()
    return len(pairs), ""


def create_replay_job(doc, layout_group, sheet_group, flattened_parts,
                      source_job=None, post_processor=None, template_path=None,
                      job_factory=None):
    """Create a new CAM job for one sheet's flattened parts.

    Returns a `ReplayJob`, or None when there is nothing to machine.

    Three things happen here, and each has a reason it cannot be skipped:

    1. **Parts are moved to sheet-local coordinates.** Sheets are laid out side
       by side, so a sheet other than the first has its parts at a non-zero
       world X. Subtracting the sheet origin means each sheet's G-code starts
       at X0 Y0 and the stock can sit at the origin.

    2. **`PathJob.Create` is called, which clones the geometry.** The clones in
       `job.Model.Group` are what operations must reference; see `ReplayJob`.

    3. **The stock is replaced with an explicit box.** The default is a
       `StockFromBase` fitted to the model, which measured Z -7.0 .. 1.0 for a
       6 mm part. The nesting job needs the stock to *be* the sheet:
       `SheetWidth` x `SheetHeight` x `SheetThickness`, spanning
       `-thickness` to 0, so Z0 is the top of the stock. This is the single
       place the Z frame is decided, and because operation depths are derived
       from the stock, getting it right here is what makes the depths right
       everywhere else.

    `job_factory` defaults to the App-level `Path.Main.Job.Create`, which works
    under `freecadcmd`. A caller with a GUI can pass the GUI-level factory
    instead, which additionally wires up a view provider; the replay does not
    depend on which was used.
    """
    from Path.Main import Job as PathJob
    from Path.Main import Stock as PathStock

    parts = list(flattened_parts)
    if not parts:
        return None

    width, height, thickness = read_sheet_dimensions(layout_group)
    origin = sheet_origin_for(sheet_group)
    translate_to_sheet_local(parts, origin)

    factory = job_factory
    if factory is None:
        factory = PathJob.Create

    sheet_label = getattr(sheet_group, "Label", "") or ""
    job_name = replay_job_name(source_job, sheet_label)

    job = factory(job_name, parts, template_path)
    if job is None:
        return None
    doc.recompute()

    # The model-level `PathJob.Create` is the one that works headless, so it
    # does not attach a view. `Main/Gui/Job.py`'s `Create` does that as a
    # separate step, and the replay has to do it too or the job comes out with
    # the generic icon, no task panel on double-click, and no group extension --
    # which is what "the job does not have Operations, SetupSheet or Tools
    # under it" looks like from the tree.
    set_job_view_provider(job)

    # The flattened parts become the job's geometry directly, and the Clones
    # `PathJob.Create` wrapped around them are dropped. A Model of plain
    # `Part::Feature` objects needs nothing behind it, so the parts stop being a
    # build artefact that has to be kept alive for the job's sake. See
    # `adopt_flattened_parts_as_model` for why the Clone is not worth the link
    # it forces.
    adopted, _why_not = adopt_flattened_parts_as_model(doc, job, parts)
    if adopted:
        FreeCAD.Console.PrintMessage(
            "Used the %d flattened part(s) directly as the job's geometry "
            "rather than clones of them.\n" % adopted)

    # The Model is the job's real geometry. Read it back from the job rather
    # than assuming the caller's list survived -- after the swap it is the
    # parts themselves, not clones of them.
    clones = list(getattr(getattr(job, "Model", None), "Group", []) or [])

    if getattr(job, "Stock", None) is not None:
        doc.removeObject(job.Stock.Name)

    stock = PathStock.CreateBox(job)
    stock.Label = "%s%s" % (STOCK_LABEL_PREFIX, sheet_label or "Sheet")
    stock.Length = width
    stock.Width = height
    stock.Height = thickness
    # Top face at Z0, bottom at -thickness, matching the Z normalisation
    # flatten_container applied to the parts.
    stock.Placement = FreeCAD.Placement(
        FreeCAD.Vector(0, 0, -float(thickness)), FreeCAD.Rotation()
    )
    job.Stock = stock

    if post_processor:
        try:
            job.PostProcessor = post_processor
            job.PostProcessorOutputFile = ""
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Could not set post processor '%s' on %s: %s\n"
                % (post_processor, job_name, exc)
            )
    else:
        _copied, job_setup_failures = copy_job_setup(source_job, job)
        for note in job_setup_failures:
            FreeCAD.Console.PrintWarning(
                "%s kept its own value for %s; the replayed job will not "
                "match the source there.\n" % (job_name, note))
    doc.recompute()

    warnings = []
    if source_job is not None:
        warnings.extend(compare_stock_frames(
            getattr(source_job, "Stock", None), stock, sheet_label
        ))

    return ReplayJob(job, clones, stock, sheet_group, warnings)


def describe_replay_job(replay):
    """Return a human-readable summary of a `ReplayJob`.

    Plain text over `FreeCAD.Console`, as with the other describe helpers.
    """
    lines = [
        "Created %s with %d part(s), stock %s x %s x %s, Z0 at the top of the "
        "stock." % (
            getattr(replay.job, "Label", "?"), len(replay.clones),
            getattr(replay.stock, "Length", "?"),
            getattr(replay.stock, "Width", "?"),
            getattr(replay.stock, "Height", "?"),
        )
    ]
    for warning in replay.warnings:
        lines.append("WARNING: %s" % warning)
    return lines


# -- flattening -----------------------------------------------------------
#
# The property a flattened part carries to identify where it came from. It is a
# link rather than a label because label lookups cannot be trusted: see the
# module docstring, constraint 4. One link per flattened part is enough to match
# it to the source operation in the write half without any string comparison.

PROP_SOURCE_OBJECT = "SourceObject"
PROP_SOURCE_CONTAINER = "SourceContainer"
PROP_NESTED_LABEL = "NestedLabel"

# The child of a `nested_*` container holding the geometry to cut. The same
# prefix `cam_manager` keys on, so the two halves of the workbench agree on
# which object in a container is the part.
PART_LABEL_PREFIX = "part_"


# Properties that describe an object's identity or its computed result rather
# than the settings a user chose, and so must not be copied to a new operation.
#
#   Base             - points at the OLD geometry/operation; the write half
#                      re-points this deliberately
#   Path             - the computed toolpath, a result not a setting
#   Proxy            - the scripted behaviour, reattached by the constructor
#   ExpressionEngine - bound to the old object's name; carries no meaning alone
#   Label/Label2     - display names, re-derived by the caller
#   Visibility       - view state, not a machining setting
#
# Verified by capture-then-replay against a live job: 39 properties on a
# Profile and 17 on a LeadInOut were set without error once these were removed.
NON_REPLAYABLE_PROPERTIES = frozenset({
    "Base",
    "Path",
    "Proxy",
    "ExpressionEngine",
    "Label",
    "Label2",
    "Visibility",
})


def is_dressup(obj):
    """Return True if `obj` is a dressup (wraps an operation) rather than an
    operation (wraps geometry).

    The two are distinguished by the *type* of their `Base` property, which is
    the only reliable signal:

      - a dressup holds a single `App::PropertyLink` pointing at another
        Path object. Not subscriptable.
      - an operation holds an `App::PropertyLinkSubList`, which FreeCAD hands
        back as a Python list of (object, [sub-element]) tuples.

    Checking `isinstance(obj.Base, (list, tuple))` and returning early is what
    keeps this from raising. An object with no `Base` at all, or an empty one,
    is reported as an operation: there is nothing wrapped, so there is nothing
    to attach, and treating it as a bare operation is the recoverable reading.
    """
    base = getattr(obj, "Base", None)
    if not base:
        return False
    if isinstance(base, (list, tuple)):
        return False
    return hasattr(base, "isDerivedFrom") and base.isDerivedFrom("Path::Feature")


def is_operation(obj):
    """Return True if `obj` is an operation rather than a dressup."""
    return not is_dressup(obj)


def resolve_source_object(clone):
    """Return the original object behind a job's Model clone, or None.

    `PathJob.Create` replaces the base geometry passed to it with a
    `draftobjects.clone.Clone`, so the objects an operation's `Base` actually
    references are clones. The clone keeps a back-reference in `Objects`, and
    `job.Proxy.baseObject()` unwraps it, but the write half needs the original
    for identity matching and cannot take a `job` argument everywhere.

    Also records why the clone is not enough on its own: a custom property
    added to the original is not visible on the clone. Verified with a `MyMeta`
    string added to a `Part::Feature` before job creation -- `hasattr(clone,
    "MyMeta")` was False afterwards. Anything the user set on their own part
    lives on the original, not on what the job references.
    """
    if clone is None:
        return None
    originals = getattr(clone, "Objects", None)
    if originals:
        return originals[0]
    return None


def capture_properties(obj):
    """Return the replayable settings of `obj` as a dict.

    Every `App::Property*` on the object is captured except those in
    `NON_REPLAYABLE_PROPERTIES`, which describe identity or a computed result
    rather than a user setting. A property whose type cannot be read is
    skipped rather than raised on: one unreadable property should not cost the
    whole recipe, and the caller can see what was skipped by diffing keys
    against `obj.PropertiesList`.

    Note that `ToolController` is deliberately *captured*. It is a job-local
    link into the source job's tool table and cannot be assigned to an
    operation in a different job, but the write half needs to know a tool was
    selected in order to remap it onto the new job's equivalent.
    """
    captured = {}
    for name in getattr(obj, "PropertiesList", ()):
        if name in NON_REPLAYABLE_PROPERTIES or name.startswith("_"):
            continue
        try:
            type_id = obj.getTypeIdOfProperty(name)
        except Exception:
            continue
        if not type_id.startswith("App::Property"):
            continue
        try:
            captured[name] = getattr(obj, name)
        except Exception:
            continue
    return captured


class DressupSpec:
    """One dressup in a stack, in the order it must be re-applied.

    `source` is the source job's dressup object, so its proxy identifies which
    kind of dressup this is and its properties carry the user's settings.
    """

    def __init__(self, source, properties):
        self.source = source
        self.properties = properties

    @property
    def label(self):
        return getattr(self.source, "Label", "<unnamed>")

    def __repr__(self):
        return "<DressupSpec %s>" % self.label


class OperationRecipe:
    """One process step: a Path operation and the dressup stack above it.

    A "process step" is one entry in the job's Operations list. When an
    operation is dressed up, the list holds the *outermost dressup*, not the
    operation -- the operation is reached through the dressup's link instead.
    So a step is recovered by walking down from a list entry to the operation
    at the bottom, and the dressups collected on the way give the stack in
    order, innermost first.

    Attributes:
        source: the operation object, found at the bottom of the stack.
        properties: its captured settings, replayable onto a new object.
        dressups: `DressupSpec` list, innermost first. Replay rebuilds them in
            that order, each layered on the previous object.
        base_entries: the operation's `Base` as (object, subs) pairs. These are
            what make the replay possible at all, since a nested copy keeps
            identical topology and therefore identical sub-element numbering.
        outermost: the object the Operations list should carry for this step --
            the top of the dressup stack, or the operation itself if bare. This
            is what goes in the new job's Operations list; see the write half.
    """

    def __init__(self, source, properties, base_entries=None, outermost=None):
        self.source = source
        self.properties = properties
        self.dressups = []
        self.base_entries = list(base_entries or [])
        self.outermost = outermost if outermost is not None else source

    @property
    def label(self):
        return getattr(self.source, "Label", "<unnamed>")

    def add_dressup(self, spec):
        self.dressups.append(spec)

    @property
    def dressup_labels(self):
        return [d.label for d in self.dressups]

    def __repr__(self):
        return "<OperationRecipe %s (%d dressup(s): %s)>" % (
            self.label, len(self.dressups), " -> ".join(self.dressup_labels) or "none")


class Recipe:
    """The source job's operations, in group order, with dressups attached.

    `operations` is in `Operations.Group` order, which is the order the job
    processes them. Note that this is not the order the dressups appear in the
    group: a dressup is listed where the user put it, and on a real job
    `['DressupLeadInOut', 'Profile', 'Drilling']` put the dressup ahead of the
    operation it wraps. Attaching in a second pass is what makes that
    representable.

    `unresolved_dressups` holds any dressup whose stack does not bottom out in
    an operation. These are reported rather than dropped: a dressup with no
    operation cannot be replayed, and silently losing one would produce a job
    that cuts differently from the source with nothing to say why.

    `normalised_entries` holds list entries that were dropped as redundant --
    an operation listed separately from a dressup layered on it. Reproducing
    them would cut the same contour twice; see `read_recipe`.
    """

    def __init__(self, job):
        self.job = job
        self.operations = []
        self.unresolved_dressups = []
        self.normalised_entries = []

    def __len__(self):
        return len(self.operations)

    def __iter__(self):
        return iter(self.operations)

    def __repr__(self):
        return "<Recipe %d step(s), %d unresolved dressup(s), %d normalised>" % (
            len(self.operations), len(self.unresolved_dressups),
            len(self.normalised_entries))


def walk_stack(entry):
    """Follow `entry` down its dressup stack to the operation at the bottom.

    Returns `(operation, dressups)`, or `(None, dressups)` if the walk ran off
    the end -- a dressup with no `Base`, or a cycle, which FreeCAD's link
    system does not prevent and a malformed file can contain.

    `dressups` comes back **innermost first**, which is the order they must be
    re-applied: you cannot build the outer one until the inner one exists.

    The cycle guard matters more than it looks. `Base` is an ordinary link, so a
    file can say dressup A sits on dressup B and B sits on A, and without the
    `seen` set this is an infinite loop rather than an error.
    """
    dressups = []
    current = entry
    seen = {id(current)}
    while is_dressup(current):
        dressups.append(current)
        base = getattr(current, "Base", None)
        if base is None or id(base) in seen:
            return None, list(reversed(dressups))
        seen.add(id(base))
        current = base
    return current, list(reversed(dressups))


def read_recipe(job):
    """Read `job`'s operations and dressups into a `Recipe`.

    The unit of reading is the **process step**: one entry in the job's
    Operations list. That matters because a dressed operation is *not* listed.
    The list carries the outermost dressup, and the operation underneath is
    reached through the dressup's link rather than being an entry of its own --
    so a reader that looks only at the list sees nothing at all in a job whose
    operations are dressed up. On the replay fixture that was 0 operations read
    from 7 that exist.

    So the read walks. For each list entry that is a dressup, `walk_stack`
    follows the links down to the operation and returns the stack in order.
    That is one step. An entry that is not a dressup is a bare step, unless a
    dressup already claimed the operation it points at.

    The converse case -- an operation listed *and* a dressup layered on it --
    happens when operations are added to a job and then dressed up, because
    creating an operation registers it in the list. The redundant listing is
    dropped rather than reproduced, so a replayed step cannot be cut twice.
    The drop is recorded on `recipe.normalised_entries` rather than being
    silent, since it means the source job and the replayed job are shaped
    differently.

    `Base` entries are read from the source operation and kept on the recipe.
    They are (geometry object, sub-element list) pairs, and the sub-element
    names are the whole mechanism by which a replay can target a nested copy
    that has different placement but identical topology.

    An unresolvable dressup -- one whose stack does not bottom out in an
    operation -- is recorded on `recipe.unresolved_dressups` rather than
    raised. It cannot be replayed, and the caller decides whether that is worth
    warning about.
    """
    recipe = Recipe(job)

    operations_group = getattr(getattr(job, "Operations", None), "Group", None)
    if not operations_group:
        return recipe

    by_identity = {}
    claimed = {}

    for entry in operations_group:
        if not is_dressup(entry):
            continue
        operation, dressups = walk_stack(entry)
        if operation is None:
            recipe.unresolved_dressups.append(entry)
            continue
        claimed[id(operation)] = entry

    for entry in operations_group:
        if not is_dressup(entry):
            # A bare operation, unless a dressup already sits on top of it.
            if id(entry) in claimed:
                recipe.normalised_entries.append(entry)
                continue
            item = OperationRecipe(entry, capture_properties(entry),
                                   _read_base_entries(entry))
            recipe.operations.append(item)
            by_identity[id(entry)] = item
            continue

        operation, dressups = walk_stack(entry)
        if operation is None:
            continue                      # already recorded as unresolved

        item = by_identity.get(id(operation))
        if item is None:
            item = OperationRecipe(
                operation, capture_properties(operation),
                _read_base_entries(operation), outermost=entry,
            )
            recipe.operations.append(item)
            by_identity[id(operation)] = item
        elif id(entry) is not id(item.outermost):
            # Two list entries resolving to one operation: two independent
            # dressup chains over the same cut. Replaying both would apply two
            # sets of dressups to one operation, which is not what either chain
            # meant. Take the first and report the second.
            recipe.normalised_entries.append(entry)
            continue

        for source_dressup in dressups:
            item.add_dressup(DressupSpec(source_dressup,
                                        capture_properties(source_dressup)))

    return recipe


def _read_base_entries(operation):
    """Return an operation's `Base` as a list of (object, subs) pairs.

    An operation's `Base` is an `App::PropertyLinkSubList`, which FreeCAD
    returns as a list of (object, [sub-names]) tuples. The list is copied so a
    later reassignment on the source operation cannot mutate the recipe.
    """
    base = getattr(operation, "Base", None)
    if not base or not isinstance(base, (list, tuple)):
        return []
    entries = []
    for item in base:
        try:
            geometry, subs = item
        except (TypeError, ValueError):
            continue
        entries.append((geometry, list(subs) if isinstance(subs, (list, tuple)) else [subs]))
    return entries


class FlattenResult:
    """The outcome of flattening one sheet's nested containers.

    Attributes:
        parts: the flattened `Part::Feature` objects, in container order.
        skipped: (container, reason) pairs for containers that yielded nothing.
            Reported rather than raised: one unusable container should not cost
            the whole sheet, but it does mean fewer parts get cut, and the
            operator needs to know which.
        z_shifts: (object, offset) for each part whose Z was moved, kept so a
            caller can report parts that arrived somewhere unexpected.
    """

    def __init__(self):
        self.parts = []
        self.skipped = []
        self.z_shifts = []

    def __len__(self):
        return len(self.parts)

    def __iter__(self):
        return iter(self.parts)

    def __repr__(self):
        return "<FlattenResult %d part(s), %d skipped>" % (len(self.parts), len(self.skipped))


def find_part_in_container(container):
    """Return the `part_*` child of a `nested_*` container, or None.

    Matching is on the label prefix, which is the convention the nesting side
    writes (`shape_preparer` creates `part_<id>`) and the convention
    `cam_manager` already reads. The prefix carries an underscore precisely so
    it cannot match a sibling such as `boundary_*` or `label_*`.
    """
    for child in getattr(container, "Group", ()):
        if getattr(child, "Label", "").startswith(PART_LABEL_PREFIX):
            return child
    return None


def combined_placement(container, part):
    """Return the world placement of `part` as nested inside `container`.

    A nested part carries its position twice: the container holds the placement
    the nester chose (position and rotation on the sheet), and the child holds
    the up-direction rotation applied when the master shape was built. The
    world transform is the product, `container.Placement * part.Placement`, in
    that order.

    Order matters and is not commutative. A probe with a container at
    `(60, 0, 0)` rotated 37 degrees and an identity child produced
    `Pos=(60, 0, 0) Yaw=37`; with the multiplication reversed the position
    would be rotated by the child's own rotation instead.
    """
    container_placement = getattr(container, "Placement", None) or FreeCAD.Placement()
    part_placement = getattr(part, "Placement", None) or FreeCAD.Placement()
    return container_placement.multiply(part_placement)


def z_offset_for_thickness(shape, sheet_thickness):
    """Return the Z translation that puts a shape's bottom at `-thickness`.

    The convention `cam_manager` already uses: stock spans `-thickness` to `0`,
    parts sit on the stock with their own bottom at `-thickness`, so Z0 is the
    top of the stock. Reimplemented rather than imported, because this feature
    is deliberately standalone.

    The formula is `-thickness - shape.BoundBox.ZMin`, which is idempotent: a
    part already at `ZMin == -thickness` gets an offset of exactly 0. Measured
    across three starting positions for a 6 mm part:

        ZMin  0.0 -> offset -6.0 -> -6.0 .. 0.0
        ZMin -6.0 -> offset  0.0 -> -6.0 .. 0.0
        ZMin  2.0 -> offset -8.0 -> -6.0 .. 0.0

    So it is safe to apply unconditionally, including to geometry that has
    already been normalised by a previous run.
    """
    if sheet_thickness is None:
        return 0.0
    return -float(sheet_thickness) - float(shape.BoundBox.ZMin)


class FlattenedPart:
    """One container flattened, with the Z shift that was applied.

    Returned rather than a bare object so the Z shift travels with the part. A
    caller reporting on a run needs to know which geometry arrived somewhere
    unexpected, and that information is lost the moment a function returns
    sometimes-a-tuple-sometimes-an-object.
    """

    def __init__(self, obj, container, z_offset):
        self.obj = obj
        self.container = container
        self.z_offset = z_offset

    def __repr__(self):
        return "<FlattenedPart %s z_offset=%.3f>" % (
            getattr(self.obj, "Label", "?"), self.z_offset)


def flatten_container(doc, container, sheet_thickness, group=None, name_prefix="CAMPart"):
    """Flatten one `nested_*` container into a single top-level `Part::Feature`.

    Returns a `FlattenedPart`, or None if the container holds no `part_*` child
    or the child has a null shape.

    The transform is applied as a `Placement`, never via `transformGeometry`.
    This is the single most important choice in the module and it is not a
    style preference. `transformGeometry` converts analytic surfaces into
    B-splines: a `Cylinder` face came back as a `BSplineSurface`, with face area
    shifted 314.1593 -> 315.0023. The lead-in and lead-out arc maths run
    against the real geometry, so a splined cylinder means the toolpath follows
    an approximation of the surface the user drew. A `Placement` leaves a
    `Cylinder` a `Cylinder`.

    It is also what makes the replay addressable at all. Operations reference
    geometry by sub-element name (`Face1`, `Edge7`), so the topology has to
    survive flattening. A nested copy has identical topology to its source --
    same edge count, same per-edge length and curve type -- so the source
    operation's sub-element names address the same features on every copy.

    Three properties are attached so the write half can match a flattened part
    back to its source without comparing labels:
    `SourceObject` (the part's own original), `SourceContainer` (the container
    it came from) and `NestedLabel`.
    """
    part = find_part_in_container(container)
    if part is None:
        return None

    shape = getattr(part, "Shape", None)
    if shape is None or shape.isNull():
        return None

    # Copy, then strip the child's placement before applying the combined one.
    # Assigning to a Placement leaves the underlying geometry untouched, which
    # is the whole point -- see the docstring above.
    flattened = shape.copy()
    flattened.Placement = FreeCAD.Placement()
    flattened.Placement = combined_placement(container, part)

    offset = z_offset_for_thickness(flattened, sheet_thickness)
    if offset:
        placement = flattened.Placement
        base = placement.Base
        flattened.Placement = FreeCAD.Placement(
            FreeCAD.Vector(base.x, base.y, base.z + offset),
            placement.Rotation,
        )

    obj = doc.addObject("Part::Feature", "%s_%s" % (name_prefix, len(doc.Objects)))
    obj.Shape = flattened
    if group is not None:
        group.addObject(obj)

    _add_link_property(obj, PROP_SOURCE_OBJECT, part,
                       "The nested part this geometry was flattened from")
    _add_link_property(obj, PROP_SOURCE_CONTAINER, container,
                       "The nested_* container this geometry came from")
    if not hasattr(obj, PROP_NESTED_LABEL):
        obj.addProperty("App::PropertyString", PROP_NESTED_LABEL, "CAM Replay",
                        "Label of the nested container, for reporting")
    setattr(obj, PROP_NESTED_LABEL, getattr(container, "Label", ""))

    return FlattenedPart(obj, container, offset)


def _add_link_property(obj, name, value, doc_string):
    """Add a cross-scope link property to `obj` if it is not already present.

    `App::PropertyXLink`, not `App::PropertyLink`. A plain Link to an object
    that lives inside an `App::Part` container is out of that container's
    scope, and FreeCAD says so on every recompute:

        Part::Feature: CAMPart_12 links are out of scope.
        Out of scope links to: part_Bracket_1

    Measured: the identical link as an XLink produced no warning, whether it
    pointed at the child or at the container. XLink exists for exactly this --
    a link whose target is not a sibling or a child -- and since a flattened
    part deliberately sits outside the layout while pointing back into it, this
    is the correct property type rather than a way of muting a message.

    Linking to the child and to the container are both fine; the module records
    both, because the write half needs the part and a report needs the
    container.
    """
    if not hasattr(obj, name):
        obj.addProperty("App::PropertyXLink", name, "CAM Replay", doc_string)
    setattr(obj, name, value)


class Progress:
    """Two-level progress reporting for a replay run.

    A replay is not quick, though it got a great deal quicker: the committed
    fixture is 48 parts with 7 source operations, and `replay_layout` took
    **43.5s** for one sheet before the per-part split and the footprint cache,
    and takes **~10.5s** now. It was worth getting that far down before deciding
    a progress bar was needed, because a bar is the honest answer to a slow
    command and a workaround is the honest answer to a slow *implementation*.

    `replay_layout` loops over sheets, so the numbers multiply: a three-sheet
    nest is half a minute, not two minutes. This is the seam that a dialog, a
    Tasks-panel widget or a bare Console line can all hang off.

    The callback is `callback(stage, current, total, message=None)`.

    **Why four arguments and not the nester's three.** `nesting_strategy` takes
    `progress_callback(current, total, message)` and a controller turns that
    into a bar. That works there because the nester has one kind of work:
    `progress_callback(i + 1, total_parts, "Placing X...")` over a loop of
    parts, so `current/total` means the whole run and one percentage is
    honest. A replay has nine stages of different kinds -- walking the source
    job's dressup graph, transforming 48 shapes, computing seven toolpaths,
    re-reading sub-element names off 23 parts per operation, verifying. No
    single ratio across those is knowable without measuring, and an invented
    weighting is a bar that lies, which is worse than no bar. So the stage is
    named and the count is *within* it. Both are always true, and a UI is free
    to render "stage 3 of 9" beside "part 23 of 48".

    `current`/`total` are zero at the start of a stage, before anything has
    been counted. A stage whose work is one indivisible call reports nothing
    further: that call is the bar's blind spot, and claiming a count for it
    would mean inventing one.

    Optional throughout. `None` is the default, so the harness and every
    existing caller are unaffected.

    **It also times itself.** `timing_rows()` gives `(stage, seconds, runs)`
    per stage, and `elapsed` is the time accounted for. Timing lives here rather
    than in whoever wants the numbers for two reasons:

      * There is already one boundary per stage in this class, and a caller who
        wanted timings would otherwise have to bracket each stage again. Two
        sets of stage boundaries drift apart the first time one is edited.
      * A caller timing from *progress events* rather than from stage
        boundaries gets the wrong answer. Events land at arbitrary points
        within a stage, so the deltas between them are attributed correctly but
        nothing brackets the work before the first event or after the last one.

    `clock` is injectable so the pure-Python tier can assert on the numbers
    without sleeping. `runs` counts how many separate times a stage was entered,
    so a multi-sheet run aggregates into one row per stage rather than one row
    per stage per sheet.
    """

    def __init__(self, callback=None, prefix="", clock=None,
                 cancel_check=None):
        self._callback = callback
        self._prefix = ("%s " % prefix) if prefix else ""
        self._stage = ""
        self._failed = False
        #: Why reporting stopped, or None. Exposed rather than only printed, so
        #: a caller can put it in its own log and so it stays observable in the
        #: pure-Python tier where `FreeCAD.Console` is a stand-in returning None.
        self.warning = None

        self._clock = clock or time.perf_counter
        #: `[stage, seconds, runs]`, merged by stage name, in first-seen order.
        self.timings = []
        self._opened_at = None
        self._stage_events = 0
        #: Seconds accounted for by the stages. Less than the run's wall clock,
        #: because work outside any stage is not counted; that gap is worth
        #: seeing too, which is why callers report both.
        self.elapsed = 0.0

        self._cancel_check = cancel_check
        self._cancelled = False

    @property
    def cancelled(self):
        """Whether the run should stop at the next opportunity.

        Latched. Once true it stays true, so a caller that reads it in a loop
        and a caller that reads it once see the same answer, and a cancel can
        never be un-done by a widget that has already been destroyed.

        `cancel_check` is polled rather than `request_cancel()` being called,
        because the widget that owns the Cancel button lives in the GUI thread
        and the replay runs there too -- a poll is one boolean read and works
        whether the button sets a flag, a checkbox, or is simply gone.
        """
        if self._cancelled:
            return True
        if self._cancel_check is not None:
            try:
                if self._cancel_check():
                    self._cancelled = True
            except Exception:
                # A cancel check that raises is a broken UI, not a request to
                # stop. Cancel means "the user asked"; a widget that fell over
                # on the way to being asked did not ask. Dropping the check
                # rather than cancelling keeps a UI fault from costing the run.
                self._cancel_check = None
                self.warning = "cancel check raised; cancelling is disabled"
                try:
                    FreeCAD.Console.PrintWarning(
                        "Replay %s.\n" % self.warning)
                except Exception:
                    pass
        return self._cancelled

    def request_cancel(self):
        """Ask for the run to stop at the next opportunity."""
        self._cancelled = True

    def stage(self, name):
        """Announce a new stage. Resets the within-stage count."""
        self._close_stage()
        self._stage = name
        self._stage_events = 0
        self._opened_at = self._clock()
        self._emit(0, 0, name)

    def item(self, current, total, detail=None):
        """Report position within the current stage."""
        self._stage_events += 1
        self._emit(current, total, detail)

    def done(self, detail=None):
        """Close the current stage."""
        self._emit(None, None, detail)

    def _close_stage(self):
        """Bank the running stage's elapsed time, if one is running."""
        if self._opened_at is None:
            return
        seconds = self._clock() - self._opened_at
        self._opened_at = None
        self.elapsed += seconds
        for row in self.timings:
            if row[0] == self._stage:
                row[1] += seconds
                row[2] += 1
                break
        else:
            self.timings.append([self._stage, seconds, 1])

    def finish(self):
        """Close the running stage. Idempotent."""
        self._close_stage()

    def timing_rows(self):
        """`[(stage, seconds, runs)]`, banking the running stage first.

        The row for the last stage is only correct after it has been closed, so
        this closes it rather than reporting an undercount. Idempotent, so
        reading the timings twice does not double-count anything.
        """
        self._close_stage()
        return [(stage, seconds, runs)
                for stage, seconds, runs in self.timings]

    def _emit(self, current, total, message):
        if self._callback is None or self._failed:
            return
        try:
            self._callback(self._stage, current, total,
                           self._prefix + (message or ""))
        except Exception as exc:
            # A dead widget must not cost a 10-second run, and must not repeat
            # itself on every one of the 48 parts. One warning, then silent.
            #
            # The print is guarded as well. This is the one place the recovery
            # path could itself raise: `FreeCAD.Console` is a stand-in
            # returning None outside a real FreeCAD, and an unguarded
            # `PrintWarning` there raises AttributeError straight through the
            # except block that exists to prevent exactly that. Recovery code
            # that can fail is not recovery.
            self._failed = True
            self.warning = ("progress callback raised %s: %s"
                            % (type(exc).__name__, exc))
            try:
                FreeCAD.Console.PrintWarning("Replay %s.\n" % self.warning)
            except Exception:
                pass


def flatten_sheet(doc, sheet_group, sheet_thickness, group=None,
                  progress=None):
    """Flatten every `nested_*` container under `sheet_group`.

    Containers are found with the shared `get_nested_containers` helper rather
    than by walking the group here, so the replay and the existing CAM job
    creation agree on where nested parts live.

    A container that yields no part is recorded in `result.skipped` and the rest
    of the sheet continues. A null shape or a missing `part_*` child is a
    document-state problem the operator should see, not something to raise on
    and abandon the sheet for.
    """
    from ...freecad_helpers import get_nested_containers

    if progress is not None:
        progress.stage("Flattening nested parts")
    containers = list(get_nested_containers(sheet_group))
    total = len(containers)

    result = FlattenResult()
    for index, container in enumerate(containers, start=1):
        if progress is not None:
            progress.item(index, total, getattr(container, "Label", ""))
        flattened = flatten_container(doc, container, sheet_thickness, group=group)
        if flattened is None:
            result.skipped.append((container, "no part_* child, or its shape is null"))
            continue
        result.parts.append(flattened.obj)
        if flattened.z_offset:
            result.z_shifts.append((flattened.obj, flattened.z_offset))
    if progress is not None:
        progress.done("%d part(s) from %d container(s)" % (len(result.parts), total))
    return result


def describe_flatten(result):
    """Return a human-readable summary of a `FlattenResult`.

    Plain text over `FreeCAD.Console` for the same reason as
    `describe_recipe`: it reaches the Report view when a GUI is up and still
    works under `freecadcmd`.
    """
    lines = ["Flattened %d part(s)." % len(result.parts)]
    for obj, offset in result.z_shifts:
        lines.append(
            "  %s: Z shifted by %.3f mm to sit on the stock"
            % (getattr(obj, "Label", "?"), offset)
        )
    for container, reason in result.skipped:
        lines.append(
            "WARNING: %s was skipped (%s); it will not be cut."
            % (getattr(container, "Label", "?"), reason)
        )
    return lines


# -- operation replay ----------------------------------------------------
#
# The part that actually cuts. Everything up to here has been preparation.

# Sub-element names are passed through from the source operation rather than
# re-resolved, and this is a deliberate choice with a real cost.
#
# It works because a flattened copy has identical topology to its source: the
# flattening is a Placement, which leaves the geometry untouched, so the same
# edge count, the same per-edge length and curve type, and therefore the same
# `Face1`/`Edge7` numbering. Verified by resolving a source operation's
# sub-names against a flattened copy and against one rotated 37 degrees --
# 1/1 resolved, and the resolved face was still the same physical top face.
#
# The cost is that nothing checks the names still mean what they meant. If the
# topology ever diverges -- a part rebuilt by a different FreeCAD version, a
# source edited after nesting -- a valid-looking name would silently address
# the wrong feature, and the result would be plausible G-code cutting the
# wrong thing. So every sub-name is resolved against each clone before the
# operation is assigned, and a name that does not resolve aborts that
# operation rather than being dropped. A loud failure is the only acceptable
# outcome for "cut the wrong feature".

# Properties an operation must not have copied from the source, beyond the
# generic NON_REPLAYABLE_PROPERTIES set. `ToolController` is in that set
# already for the recipe reader, but it is listed here for the same reason it
# is not: the write half has to remap it, so the reader kept it deliberately
# and this comment records that the two halves agree.


def resolve_subnames(subs, shape):
    """Return the sub-element names in `subs` that exist on `shape`.

    FreeCAD's `getElement` raises on an unknown name, so this is the only way
    to find out whether a source operation's selection still means anything
    after flattening. Names are returned in their original order and
    duplicates are preserved, because a selection listing the same edge twice
    is the user's business rather than this function's.
    """
    resolved = []
    for name in subs:
        if not name:
            # The empty string means "the whole object", which every shape has.
            resolved.append(name)
            continue
        try:
            shape.getElement(name)
        except Exception:
            continue
        resolved.append(name)
    return resolved


def check_subnames_against_clones(subs, clones):
    """Return `(clones_missing, detail)` for sub-names that fail on any clone.

    `clones_missing` is the set of names that resolved nowhere. `detail` is a
    per-name account of what resolved where, for the report.

    A name resolving on some clones and not others is the alarming case: the
    operation would cut a different feature on different parts of the same
    nest. It is reported separately from a name that resolves on none.
    """
    missing = set()
    detail = {}
    for name in subs:
        if not name:
            detail[name] = "whole object"
            continue
        hits = 0
        for clone in clones:
            shape = getattr(clone, "Shape", None)
            if shape is None:
                continue
            try:
                shape.getElement(name)
            except Exception:
                continue
            hits += 1
        if hits == 0:
            missing.add(name)
            detail[name] = "resolved on 0 of %d" % len(clones)
        elif hits < len(clones):
            detail[name] = "resolved on %d of %d" % (hits, len(clones))
        else:
            detail[name] = "ok"
    return missing, detail


def clones_for_source(clones, source_geometry):
    """Return the job's Model entries that correspond to `source_geometry`.

    Returns every entry when the match cannot be narrowed, which is the safe
    direction: an operation that applies to a few too many parts is visible in
    the toolpath, whereas one that applies to too few silently cuts less than
    the source.

    Narrowing happens when the source geometry and the clones name the same
    part type. The chain back from a job clone to a part is long:

        Model-Bracket (source job's clone)
          -> Bracket                       (Objects[0], the user's original)
        CAMPart_12 (the replay job's Model entry -- the flattened part itself)
          -> part_Bracket_1                (SourceObject)
          -> nested_Bracket_1              (SourceContainer)

    so the part type is recovered from `NestedLabel` as
    `nested_<type>_<n>` and compared against the source's label. That is a
    format extraction, not a document search: it is not the `findObjects`
    prefix match that makes `nested_Bracket_1` also return `nested_Bracket_10`
    and `nested_Bracket_11`.

    `nested_label_of` reads that label from either kind of Model entry -- the
    flattened part itself, or the Clone `PathJob.Create` would have made over
    it. That fallback is not decoration: reading only the Clone path left the
    lookup matching nothing, so every operation fell back to targeting every
    part and five of seven stopped cutting.
    """
    # Unwrap first. An operation's Base points at the *source job's* clone,
    # labelled `Model-Bracket`, not at the user's `Bracket` -- so comparing
    # that label against `nested_Bracket_1` matches nothing and every
    # operation falls back to targeting every entry. Verified: with the
    # unwrap missing, a 2-bracket 1-spacer nest matched 3 of 3 for both.
    original = resolve_source_object(source_geometry) or source_geometry
    source_label = getattr(original, "Label", "")
    if not source_label:
        return list(clones)

    matched = []
    for clone in clones:
        nested = nested_label_of(clone)
        if not nested:
            continue
        parts = nested.split("_")
        if len(parts) >= 3 and "_".join(parts[1:-1]) == source_label:
            matched.append(clone)

    return matched if matched else list(clones)


def nested_label_of(model_entry):
    """Return `model_entry`'s `NestedLabel`, or "" if it has none.

    Two kinds of thing live in a job's Model, and the difference matters here.

    A **`draftobjects.clone.Clone`** -- what `PathJob.Create` makes out of
    whatever you hand it -- carries the label one hop away, on whatever it was
    cloned from:

        Clone  ->  Objects[0]  ->  NestedLabel

    A **plain `Part::Feature`**, which is what the replay now puts there so the
    job's geometry needs nothing behind it, carries it itself:

        Part::Feature  ->  NestedLabel

    Reading only the Clone path silently stopped matching the moment the Model
    held anything else, and the failure is the quiet kind: nothing matched, so
    this returned every entry, so every operation targeted all 48 parts
    regardless of type, and 5 of 7 stopped cutting because their sub-element
    names resolved on only 23 of 48. Measured, not inferred.
    """
    nested = getattr(model_entry, PROP_NESTED_LABEL, "")
    if nested:
        return nested
    upstream = getattr(model_entry, "Objects", None)
    if upstream:
        return getattr(upstream[0], PROP_NESTED_LABEL, "")
    return ""


# -- view providers --------------------------------------------------------
#
# **This is what makes the replayed job look and behave like a CAM job, and no
# automated tier here can check it.** A ViewProvider does not exist under
# `freecadcmd`, so every check in this repository passes with or without the
# code below. The only verification is opening the result in the GUI.
#
# What breaks without it, from a real manual run:
#
#   * the tree renders flat. CAM's nesting is
#     `ViewProviderDressup.claimChildren()` returning `[self.obj.Base]`
#     (`Path/Dressup/Gui/LeadInOut.py:812`), so with no ViewProvider the base
#     operation has no parent and FreeCAD puts it at the document root. The
#     document is correct throughout -- measured: the job exists, its Operations
#     group is claimed by it, every dressup's Base points at its own replayed
#     operation, and each operation is claimed exactly once. Only the display
#     and the claim are missing.
#   * the job is read-only. `setEdit` lives on the same object, so
#     double-clicking a replayed operation or dressup does not open its task
#     dialog.
#
# The class names are not consistent, which is why this is a table: four kinds
# use `ViewProviderDressup`, and the other three each have their own name.
# Measured headless, and the availability is uneven:
#
#   Path.Dressup.Gui.LeadInOut / DogboneII / Mirror / RampEntry   import, have it
#   Path.Dressup.Gui.Array       imports, class is DressupArrayViewProvider
#   Path.Dressup.Gui.Boundary    ImportError -- "Cannot load Gui module in
#                                console application"
#   Path.Op.Gui.Base             AttributeError -- FreeCADGui.addCommand
#
# So every lookup is guarded and every one has a fallback, and none of it runs
# headless at all.

#: proxy module -> (Gui module, ViewProvider class)
DRESSUP_VIEWPROVIDERS = {
    "Path.Dressup.Gui.LeadInOut": ("Path.Dressup.Gui.LeadInOut",
                                   "ViewProviderDressup"),
    "Path.Dressup.Gui.Mirror": ("Path.Dressup.Gui.Mirror", "ViewProviderDressup"),
    "Path.Dressup.Gui.RampEntry": ("Path.Dressup.Gui.RampEntry",
                                   "ViewProviderDressup"),
    "Path.Dressup.DogboneII": ("Path.Dressup.Gui.DogboneII", "ViewProviderDressup"),
    "Path.Dressup.Array": ("Path.Dressup.Gui.Array", "DressupArrayViewProvider"),
    "Path.Dressup.Boundary": ("Path.Dressup.Gui.Boundary",
                              "DressupPathBoundaryViewProvider"),
    "Path.Dressup.Tags": ("Path.Dressup.Gui.Tags", "PathDressupTagViewProvider"),
}

#: Operations do NOT share one view provider class with a fixed signature --
#: see `OPERATION_GUI_MODULE` below for why each needs its own resources. What
#: is shared is the base class every one of them derives from.

import importlib


def _view_provider_class(gui_module, class_name):
    """Import `gui_module` and return a ViewProvider class from it.

    Falls back to any class in the module whose name mentions a view provider,
    which is what carries this through when FreeCAD renames one. **Raises**
    rather than returning None: a missing view provider is a real defect, and a
    caller that cannot see it has no way to report one. The version this
    replaced returned None and warned, and a warning nobody reads is how a whole
    manual round went by believing the operations were covered.
    """
    module = importlib.import_module(gui_module)
    found = getattr(module, class_name, None)
    if found is not None:
        return found
    for name in dir(module):
        if "ViewProvider" not in name:
            continue
        candidate = getattr(module, name)
        if isinstance(candidate, type):
            return candidate
    raise LookupError("no view provider class in %s" % gui_module)


#: An operation's view provider needs a `CommandResources` object, and the only
#: thing that carries one is the command its Gui module registers at import:
#:
#:     Path.Op.Gui.Profile.Command.res   ->   ViewProvider(vobj, res)
#:
#: `res` supplies the icon, the operation name and the task-page class. This
#: is the mistake that cost a whole manual round: the view provider is
#: constructed with TWO arguments, and calling it with one raises a TypeError
#: that a `try` around the whole thing swallows. The operation then has no view
#: provider at all -- wrong icon, and double-click does nothing -- and nothing
#: anywhere says so, because the swallow reported success.
OPERATION_GUI_MODULE = "Path.Op.Gui.%s"

#: The job's own view provider, and the extension that makes it a group in the
#: tree. Without the extension the job's Operations, Model, Tools, Stock and
#: SetupSheet nest by ordinary link claims rather than as a CAM job's contents,
#: which is what "the job does not have them under it" looks like.
#: `Main/Gui/Job.py:2060`.
JOB_VIEWPROVIDER = ("Path.Main.Gui.Job", "ViewProvider")
JOB_GROUP_EXTENSION = "Gui::ViewProviderGroupExtensionPython"


def _attach_view_provider(obj, provider, failures):
    """Attach `provider(view)` to `obj`'s view. Returns True if set.

    `failures` collects what went wrong. This function does not swallow
    anything: a view provider that fails to attach is why a replayed job comes
    out with the wrong icon and no double-click, and the first version of this
    code hid exactly that behind a bare `except` and a `return False` -- which
    cost a whole manual test round, because a swallowed failure is
    indistinguishable from success.

    `provider` takes the view and whatever else it needs, so the two-argument
    operation case is a lambda rather than a special case here.
    """
    if failures is None:
        failures = []
    view = getattr(obj, "ViewObject", None)
    if view is None:
        return False                # headless; a view provider is meaningless
    label = getattr(obj, "Label", "?")
    try:
        view.Proxy = provider(view)
    except Exception as exc:
        failures.append("%s: %s: %s" % (label, type(exc).__name__, exc))
        return False
    return True


def set_view_provider(obj, proxy_module=None, is_dressup=None, failures=None):
    """Attach the right ViewProvider to a replayed operation or dressup.

    Returns True if one was set. A no-op returning False when there is no GUI,
    which is the normal case in every test in this repository.

    `failures` is appended to rather than warned about, so the caller can put
    the whole set in one place in the sheet report.
    """
    if failures is None:
        failures = []
    view = getattr(obj, "ViewObject", None)
    if view is None:
        return False

    if is_dressup is None:
        is_dressup = is_dressup_object(obj)

    if is_dressup:
        entry = DRESSUP_VIEWPROVIDERS.get(proxy_module)
        if entry is None:
            failures.append(
                "%s: no view provider entry for %r, so its base operation "
                "will not nest under it in the tree"
                % (getattr(obj, "Label", "?"), proxy_module))
            return False
        gui_module, class_name = entry
        try:
            provider = _view_provider_class(gui_module, class_name)
        except Exception as exc:
            failures.append("%s: %s: %s" % (getattr(obj, "Label", "?"),
                                            type(exc).__name__, exc))
            return False
        return _attach_view_provider(obj, provider, failures)

    # An operation: the provider needs the resources its Gui module registered.
    name = (proxy_module or "").rsplit(".", 1)[-1]
    label = getattr(obj, "Label", "?")
    try:
        gui = importlib.import_module(OPERATION_GUI_MODULE % name)
        base = importlib.import_module("Path.Op.Gui.Base")
    except Exception as exc:
        failures.append("%s: no Gui module for %r: %s: %s"
                        % (label, proxy_module, type(exc).__name__, exc))
        return False
    resources = getattr(getattr(gui, "Command", None), "res", None)
    if resources is None:
        failures.append(
            "%s: %r registered no command resources, so it has no icon and no "
            "task page" % (label, gui.__name__))
        return False
    if not _attach_view_provider(
            obj, lambda v: base.ViewProvider(v, resources), failures):
        return False
    # Matches `Op/Gui/Base.py` `Create`, which also makes the operation
    # visible; without it a freshly built operation is hidden in the tree.
    try:
        view.Visibility = True
    except Exception:
        pass
    # And turn off "cancel deletes me", which `__init__` turns ON.
    #
    # `ViewProvider.__init__` sets `deleteOnReject = True`: during the initial
    # edit session, a Cancel means "abandon creating this operation".
    # `setEdit` copies that flag into the TaskPanel and only afterwards resets
    # it, so the panel holds True and `TaskPanel.reject()` runs
    # `removeObject(self.obj.Name)`.
    #
    # Correct for an operation the GUI is creating, and wrong here: this
    # operation already exists. There is no creation session to abandon, so
    # Cancel must just close the dialog -- measured on a replayed Profile, where
    # it deleted the Profile.
    #
    # A job loaded from a file does not have this problem, and the saved state
    # shows why. The proxy persisted for a Profile is
    #
    #     {"OpName", "OpIcon", "OpPageModule", "OpPageClass"}
    #
    # -- `deleteOnReject` is not in it, so a restored proxy is built without
    # `__init__` and simply does not have the attribute. That is why
    # `deleteObjectsOnReject()` guards with `hasattr`. A replayed operation is
    # the one case where the flag exists and is True, because the replay
    # constructs its view provider rather than restoring it.
    #
    # Dressups are unaffected: none of `Dressup/Gui/*.py` has the flag, which
    # is why a dressup dialog cancels cleanly and an operation dialog did not.
    clear = getattr(view.Proxy, "setDeleteObjectsOnReject", None)
    if callable(clear):
        try:
            clear(False)
        except Exception as exc:
            failures.append("%s: could not clear delete-on-cancel (%s: %s)"
                            % (label, type(exc).__name__, exc))
            return False
    return True


def set_job_view_provider(job, failures=None):
    """Attach the job's own view provider, and make it a group in the tree.

    `Main/Gui/Job.py`'s `Create` does exactly these two things after calling the
    model-level `PathJob.Create`. The replay calls the model-level one, because
    that is the one that works headless, and so it has to do the view half
    itself.

    Without the group extension the job's Operations, Model, Tools, Stock and
    SetupSheet still appear beneath it -- FreeCAD nests by link claim -- but it
    is not a CAM job in the tree: it has the generic icon, it is not editable,
    and it does not present itself the way a job does.
    """
    if failures is None:
        failures = []
    view = getattr(job, "ViewObject", None)
    if view is None:
        return False
    try:
        module = importlib.import_module(JOB_VIEWPROVIDER[0])
        view.Proxy = module.ViewProvider(view)
        view.addExtension(JOB_GROUP_EXTENSION)
    except Exception as exc:
        failures.append("%s: %s: %s" % (getattr(job, "Label", "job"),
                                        type(exc).__name__, exc))
        return False
    return True


def is_dressup_object(obj):
    """True if `obj` is a dressup, decided by its proxy module.

    Not `is_dressup`, which decides by the *type* of `Base` and so is a
    statement about the replay's data model. This asks what kind of thing it is,
    which is the question a ViewProvider lookup is asking.
    """
    proxy = getattr(obj, "Proxy", None)
    if proxy is None:
        return False
    return type(proxy).__module__.startswith("Path.Dressup")


#: Dressups that make splitting unsafe, with the measured reason.
#:
#: `Path.Dressup.Boundary` clips an operation's toolpath against a `Stock`
#: **link**, and after the replay that link still points at the SOURCE job's
#: stock -- a known defect, NEST-009, reported as a warning rather than fixed.
#: One Boundary over a whole step clips every copy in one pass, so a partly
#: wrong stock leaves *some* motion behind. Eight Boundaries each clip
#: independently, and measured on the dressup fixture, **6 of 8 copies produced
#: a 4-command path with no cutting motion at all**. A forced recompute does not
#: recover them. So splitting a Boundary step multiplies a known-wrong reference
#: and turns "clipped slightly wrong" into "cut nothing", which is exactly the
#: kind of quiet failure this feature exists to prevent.
#:
#: The guard is deliberately narrow. It is not "dressups are risky" -- LeadInOut,
#: Dogbone and Tags all split correctly and are verified to.
UNSPLITTABLE_DRESSUPS = ("Path.Dressup.Boundary",)


def split_is_safe(item):
    """Return True if `item` may be replayed once per target part.

    False only when the step's stack carries a dressup in
    `UNSPLITTABLE_DRESSUPS`, which is left whole.
    """
    for spec in getattr(item, "dressups", ()) or ():
        _kind, module_name = dressup_kind(spec.source)
        if module_name in UNSPLITTABLE_DRESSUPS:
            return False
    return True


def _split_suffix_of(operation):
    """Return the split label suffix for `operation`, or plain "_replay".

    Read back off the operation's single Base entry rather than threaded through
    from pass one, because pass two walks a flat list of copies and the suffix is
    a property of the copy rather than of the loop position.
    """
    base = getattr(operation, "Base", None) or []
    # Exactly one entry, or it is not a split copy. A step left whole covers
    # every part, so taking its first entry would name it after an arbitrary one
    # of them -- `DressupPathBoundary_replay_nested_ClampPlate_32` on an
    # operation that cuts all eight.
    if len(base) != 1:
        return "_replay"
    try:
        clone = base[0][0]
    except (TypeError, IndexError):
        return "_replay"
    return split_label_suffix(clone)


def split_label_suffix(clone, suffix="_replay"):
    """Return the label suffix for one split copy of an operation.

    Includes the nested part's own label, because at 98 operations
    `Profile002_replay001` says nothing about which copy is which, and the
    question you ask when a sheet fails is "which part". The `nested_` prefix
    is kept rather than stripped: it is what the part is called in the layout,
    so the two names are traceable to each other.

    Falls back to the plain suffix when the part carries no `NestedLabel`, so
    this never raises over something cosmetic.
    """
    nested = nested_label_of(clone)
    if not nested:
        return suffix
    return "%s_%s" % (suffix, nested)


def create_operation_in(source_op, job, label_suffix="_replay",
                        view_failures=None):
    """Create a new operation of the same kind as `source_op`, in `job`.

    The operation type is recovered from the source operation's proxy module
    rather than hardcoded, which is what keeps the replay working for Pocket,
    Slot, Engrave and anything else the user built. `type(source_op.Proxy)` is
    the proxy *class*, which has no `Create`; its module does. Verified with
    `Path.Op.Profile` and `Path.Op.Drilling`.

    Returns the new object, or None if its module offers no `Create` -- which
    would mean a scripted operation this module does not know how to rebuild,
    and is worth reporting rather than guessing at.
    """
    import importlib

    proxy = getattr(source_op, "Proxy", None)
    if proxy is None:
        return None
    module_name = type(proxy).__module__
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    factory = getattr(module, "Create", None)
    if factory is None:
        return None

    name = "%s%s" % (getattr(source_op, "Label", "Op"), label_suffix)
    try:
        new_op = factory(name, None, job)
    except TypeError:
        # Not every Create takes a job. Drilling's does; a third-party
        # operation's may not.
        try:
            new_op = factory(name)
        except Exception:
            return None
    except Exception:
        return None
    # So the result is editable and carries the right icon. No-op headless.
    set_view_provider(new_op, module_name, is_dressup=False,
                     failures=view_failures)
    return new_op


def copy_tool_controller(source_op, job, cache=None):
    """Return a tool controller in `job` equivalent to `source_op`'s.

    Returns None when the source operation has no tool controller, which is
    legitimate -- some operations are not tool-dependent.

    The controller is **copied** into the new job with
    `Path.Tool.Controller.copyTC`, not linked across. A cross-job link does
    work; verified, an operation in job B accepted job A's controller with no
    error. But it leaves the new job depending on the source job surviving,
    so deleting the source job breaks the replay. `copyTC` makes the new job
    self-contained, which is the point of creating a new job at all.

    `cache` maps a source controller to its copy, so ten operations sharing
    one 5 mm endmill produce one copied controller rather than ten.
    """
    from Path.Tool import Controller as PathToolController

    source_tc = getattr(source_op, "ToolController", None)
    if source_tc is None:
        return None

    if cache is not None and id(source_tc) in cache:
        return cache[id(source_tc)]

    try:
        new_tc = PathToolController.copyTC(source_tc, job)
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Could not copy tool controller '%s': %s\n"
            % (getattr(source_tc, "Label", "?"), exc)
        )
        return None

    if cache is not None:
        cache[id(source_tc)] = new_tc
    return new_tc


def _build_object_dressup(obj, base, module, job):
    """Build a dressup whose class is literally named `ObjectDressup`.

    Covers LeadInOut, Mirror and RampEntry. All three are `ObjectDressup(obj,
    base)`: the constructor adds the properties, links `Base` and gives
    defaults. That is all that is called.

    **The constructor's own `setup(obj)` is deliberately not run**, and this is
    the single most easily-repeated mistake in this module.

    The reasoning that invites the mistake is: "I measured Boundary, Dogbone,
    Dragknife, AxisMap and ZCorrect constructing to a 0-command path, so I must
    be skipping a required setup." That inference is wrong, and it was made here
    and then acted on. A dressup that produces no path from its constructor is
    usually one that produces no path *because nothing has been assigned to it
    yet* -- and what assigns to it is the replay, immediately afterwards, from
    the user's captured properties.

    What `setup` actually does is write **defaults**, and in LeadInOut's case
    bind the radii to an expression:

        expr = f"{baseOp.Name}.ToolController.Tool.Diameter.Value/2*1.5"
        obj.setExpression("RadiusIn", expr)
        obj.setExpression("RadiusOut", expr)

    So calling it, then applying the captured properties, then recomputing gives
    the tool-diameter default rather than the user's setting. Measured: a user
    who asked for 5 mm in and 7 mm out got 3.75 mm and 3.75 mm -- the 1.5x
    diameter for the job's 5 mm tool -- with no error reported anywhere.

    A replay reproduces a *configuration*. `setup` manufactures a default one.
    Those are different jobs, and only the first belongs here.
    """
    constructor = getattr(module, "ObjectDressup", None)
    if constructor is None:
        raise TypeError("no ObjectDressup in %s" % module.__name__)
    constructor(obj, base)
    return obj


def _build_with_job_constructor(obj, base, module, job):
    """Build Boundary or Array. Both are `Proxy(obj, base, job)`, and both
    assign the proxy themselves.

    Unlike the others, these are not called through their module's `Create`,
    because `Create` also runs `job.Proxy.addOperation(obj, base, True)` --
    `removeBefore=True` is correct about the base, but it is still this
    module's job to shape the Operations list, and it does that in one place
    at the end. See `set_operation_order`.
    """
    class_name = {"Path.Dressup.Boundary": "DressupPathBoundary",
                  "Path.Dressup.Array": "DressupArray"}.get(module.__name__)
    if class_name is None:
        raise TypeError("%s is not a job-constructor dressup" % module.__name__)
    constructor = getattr(module, class_name, None)
    if constructor is None:
        raise TypeError("no %s in %s" % (class_name, module.__name__))
    # The constructor's return value is deliberately discarded. These proxies
    # are written `def __init__(self, obj, ...): ...; return obj` to chain
    # inside their own Create, but a `return` from `__init__` does not change
    # what the class call yields -- it yields an instance of the proxy.
    # Putting that into an `App::PropertyLinkList` raises `Type must be
    # App.DocumentObject or None, not DressupPathBoundary`.
    obj.Proxy = constructor(obj, base, job)
    return obj


def _build_dogbone(obj, base, module, job):
    """Build a DogboneII dressup.

    `DogboneII.Create(base, name)` adds nothing of its own beyond
    `Proxy(obj, base)` -- it takes no job, and adding the dressup to the
    Operations list is done separately, by the GUI command
    (`Gui/DogboneII.py`: `job.Proxy.addOperation(obj, base)`) and by nothing
    else. So a headless caller has to do both halves itself. The proxy is run
    directly rather than through `Create` so that this function owns the
    document object and `Create` cannot quietly make a second one under a
    different name.

    There is no `setup` step; the proxy's `execute` does the work.
    """
    proxy_class = getattr(module, "Proxy", None)
    if proxy_class is None:
        raise TypeError("no Proxy in %s" % module.__name__)
    obj.Proxy = proxy_class(obj, base)
    return obj


def _build_tags(obj, base, module, job):
    """Build a Tags dressup: `ObjectTagDressup(obj, base)`, and nothing else.

    `Tags.Create` follows that with `dbo.setup(obj, True)` -- the boolean means
    "read from the tool table". That is not run here, for the reason in
    `_build_object_dressup`: it derives a configuration from the machine rather
    than from the user, and the replay's job is the latter. The user's tag
    assignments arrive as captured properties.

    This is the least-verified entry in the table. Tags is the one dressup in it
    that no available fixture exercises, so it has been reasoned about rather
    than measured. If it turns out to need `setup`, the fix belongs here and
    nowhere else -- and the check for it is that the replayed Tags produces a
    toolpath containing the tagged moves, not merely that it constructs.
    """
    constructor = getattr(module, "ObjectTagDressup", None)
    if constructor is None:
        raise TypeError("no ObjectTagDressup in %s" % module.__name__)
    constructor(obj, base)
    return obj


#: How to build each dressup this module can replay, keyed by the *proxy's*
#: module name -- which is what identifies the kind, robustly, where a Label is
#: not.
#:
#: A table, not a convention, because there is no convention to follow. Ten
#: dressups in four construction patterns, and no common entry point: three
#: take `(obj, base)`, two take `(obj, base, job)`, one is `(obj, base)` plus a
#: `setup` that must be called or the object yields an empty path, and four
#: take no base at all.
#:
#: Dragknife, AxisMap and ZCorrect are absent deliberately. They take no base,
#: so they are not layered on an operation at all -- they configure the job
#: (dragknife compensation, axis remapping, Z correction) and are reached
#: through the job rather than appearing in the Operations list. They are
#: reported as unsupported rather than silently skipped, because a user who
#: relied on Z correction would otherwise get a job that cuts at the wrong
#: height with nothing said about it.
DRESSUP_BUILDERS = {
    "Path.Dressup.Gui.LeadInOut": _build_object_dressup,
    "Path.Dressup.Gui.Mirror": _build_object_dressup,
    "Path.Dressup.Gui.RampEntry": _build_object_dressup,
    "Path.Dressup.DogboneII": _build_dogbone,
    "Path.Dressup.Boundary": _build_with_job_constructor,
    "Path.Dressup.Array": _build_with_job_constructor,
    "Path.Dressup.Tags": _build_tags,
}

#: Dressups known to exist but deliberately not replayable, with the reason.
#: Reported rather than dropped -- see `DRESSUP_BUILDERS`.
DRESSUP_UNSUPPORTED = {
    "Path.Dressup.Gui.Dragknife": "dragknife compensation is a job setting, not "
                                  "a dressup over an operation",
    "Path.Dressup.Gui.AxisMap": "axis remapping is a job setting, not a "
                                "dressup over an operation",
    "Path.Dressup.Gui.ZCorrect": "Z correction is a job setting, not a "
                                 "dressup over an operation",
}


def dressup_kind(source_dressup):
    """Return (kind, module_name) for `source_dressup`.

    `kind` is "known", "unsupported" or "unknown", which is a distinction that
    matters to the user: "this replay does not do ZCorrect" is actionable,
    "some dressup went wrong" is not.
    """
    proxy = getattr(source_dressup, "Proxy", None)
    if proxy is None:
        return "unknown", None
    module_name = type(proxy).__module__
    if module_name in DRESSUP_BUILDERS:
        return "known", module_name
    if module_name in DRESSUP_UNSUPPORTED:
        return "unsupported", module_name
    return "unknown", module_name


def create_dressup_in(source_dressup, base_op, job, label_suffix="_replay",
                     view_failures=None):
    """Create a copy of `source_dressup` wrapping `base_op`, in `job`.

    Returns the new object, or None if it could not be built. The reason is
    reported by `dressup_kind` before this is called, so None means a genuine
    failure rather than an unknown dressup type.

    **This does not add the dressup to the job's Operations list.** The list is
    the replay's to shape, because the rule is uniform and none of the
    dressup modules can be relied on to follow it: `addOperation(dressup, base)`
    inserts the dressup *before* the base and, with `removeBefore` unset, leaves
    the base in the list too. Two entries, one contour, cut twice -- measured at
    24 cutting moves in the dressup's section and 20 in the base's. Only
    `set_operation_order` writes the list, and only the outermost goes in.

    `Path.Dressup.Gui.LeadInOut.Create` is not called: it ends in
    `ViewProviderDressup(obj.ViewObject)` and `setEdit`, and with no ViewProvider
    under `freecadcmd` that raises `AttributeError: 'NoneType' object has no
    attribute 'Object'`. `FREECAD_TESTING` short-circuits part of it in recent
    builds but not reliably enough to depend on. The table does the same work
    without the view.

    The document comes from the job rather than being passed in. There is no
    reason to hand this function a document that might not be the one holding
    the job, and doing so once produced `AttributeError: 'NoneType' object has
    no attribute 'addObject'` -- a dressup that silently failed to be created.
    """
    import importlib

    if base_op is None:
        return None
    kind, module_name = dressup_kind(source_dressup)
    if kind != "known":
        return None

    doc = getattr(job, "Document", None) or FreeCAD.ActiveDocument
    if doc is None:
        return None
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None

    label = getattr(source_dressup, "Label", "Dressup")
    name = "%s%s" % (label, label_suffix)
    try:
        obj = doc.addObject("Path::FeaturePython", name)
        DRESSUP_BUILDERS[module_name](obj, base_op, module, job)
        if getattr(obj, "Proxy", None) is None:
            raise RuntimeError("constructor left the object without a proxy")
        # So `claimChildren()` nests the base operation under this dressup, and
        # double-click opens its task dialog. No-op headless; see the block
        # above for why nothing automated here can see it either way.
        set_view_provider(obj, module_name, is_dressup=True,
                         failures=view_failures)
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Could not recreate dressup '%s' (%s): %s\n"
            % (label, module_name, exc)
        )
        try:
            doc.removeObject(name)
        except Exception:
            pass
        return None
    return obj


def set_operation_order(job, entries):
    """Replace the job's Operations list with `entries`, in that order.

    **The one place this module writes `Operations.Group`**, and it replaces
    rather than appends because two things upstream put entries there that must
    not survive.

    First, the operation factories self-register. `Path.Op.Profile.Create(name,
    None, job)` on its own leaves `[Profile]` in the list -- creating an
    operation is what puts it there. So after pass one the list holds every
    base operation, including the ones that are about to be dressed up.

    Second, `addOperation(dressup, base)` inserts the dressup *before* the base
    and, with `removeBefore` unset, leaves the base in the list too. `Array` and
    `Boundary` pass `removeBefore=True` and get this right; `LeadInOut`,
    `Tags` and `Mirror` do not.

    Either way the result is the same: two list entries for one contour, and
    the post-processor emits the list verbatim. Measured on a lead-in profile,
    24 cutting moves in the dressup's section and 20 in the base's -- the same
    profile cut twice.

    So the rule is enforced here rather than trusted to each module: **a
    process step contributes exactly one entry, the outermost dressup if the
    operation is dressed up, or the operation itself if bare.** The operation
    underneath a dressup is still created and still linked -- it has to be, the
    dressup needs it to work from -- it is simply not a step in its own right.

    If a source job does list an operation separately from a dressup layered on
    it, this drops the redundant entry rather than reproducing it. That is a
    deliberate difference between the source job's shape and the replayed one's,
    recorded on `Recipe.normalised_entries`, and it is the conservative
    direction to be wrong in.
    """
    group = getattr(getattr(job, "Operations", None), "Group", None)
    if group is None:
        return False
    kept = [entry for entry in entries if entry is not None]
    job.Operations.Group = kept
    return True


class ReplayResult:
    """The outcome of replaying a recipe into a job.

    `operations` are the new base operations, in replay order. `dressups` are
    the new dressup objects, keyed by nothing in particular -- they are
    reported, not looked up.

    `failures` holds human-readable strings for anything that could not be
    replayed. A replay that silently dropped an operation would produce a job
    that cuts less than the source, and nothing would say so.

    `entry_of` maps `id(base operation)` to **the outermost dressup wrapping
    it**, and it is what `Operations.Group` must list. `operations` is not: a
    dressed operation's list entry is its dressup, never the operation beneath
    it. Without this map the ordering step wrote the base operations over the
    dressup entries -- measured, 98 entries of which **0 were dressups**, with
    all 105 dressups left built, linked and unlisted. They would not have been
    posted, so the user's lead-in and lead-out would have silently vanished from
    the toolpath while every structural check still passed.
    """

    def __init__(self):
        self.entry_of = {}
        self.operations = []
        self.dressups = []
        self.failures = []
        self.warnings = []
        self.subname_detail = {}
        self.unsupported_dressups = []

    def __len__(self):
        return len(self.operations)

    def __repr__(self):
        return "<ReplayResult %d op(s), %d dressup(s), %d failure(s), "\
               "%d unsupported>" % (
            len(self.operations), len(self.dressups), len(self.failures),
            len(self.unsupported_dressups)
        )


def expression_bound(obj):
    """Return the set of property paths on `obj` that carry an expression.

    FreeCAD exposes these through `ExpressionEngine` as (path, expression)
    pairs. An expression is not a value: it recomputes, and it wins over
    anything assigned to the property.

    That matters here because a dressup constructor can install one. LeadInOut's
    `setup` binds `RadiusIn` and `RadiusOut` to the tool diameter, and a replay
    that ran it produced 1.5x the tool radius in place of the user's setting,
    silently. Comparing the source's bindings against the replayed object's is
    how that gets caught rather than discovered on the machine.
    """
    try:
        engine = obj.ExpressionEngine
    except Exception:
        return set()
    paths = set()
    for entry in engine or ():
        try:
            paths.add(entry[0])
        except (TypeError, IndexError):
            continue
    return paths


#: Dressup properties that are links into the SOURCE job, mapped to where they
#: belong in the replayed job.
#:
#: `capture_properties` takes every `App::Property*` verbatim, including links.
#: That is right for values and wrong for links: a link carried across jobs
#: resolves -- FreeCAD does not object -- and the replayed object then depends
#: on the source job surviving, which is the same reason `copy_tool_controller`
#: copies rather than links.
#:
#: `ToolController` is handled separately, on the operations, by remapping onto
#: the copied controller.
#:
#: **`Stock` is deliberately NOT in this map, and is the one open question.**
#: It is Boundary's -- the solid the dressup clips against -- and carried
#: verbatim it clips against the SOURCE job's stock: fitted to the source part,
#: in the source Z frame, at the source part's position. That is wrong on any
#: sheet but the first, where parts are at the origin, and on the first it
#: nearly coincides with the right answer, which is what makes it easy to miss.
#:
#: The obvious fix is wrong. Repointing `Stock` at the replay job's own stock
#: looks right -- the sheet IS the boundary, and decision 3 already makes the
#: job's stock the sheet -- but the sheet stock spans `-thickness .. 0` while
#: the contour is cut at z 0, so the cut edge lies exactly on the stock's top
#: face and `edge.common(shape)` degenerates. Measured: 30 cutting moves
#: before, 0 after.
#:
#: So the state is that the link is wrong and REPORTED, not silently wrong and
#: not silently patched. Resolving it means choosing between copying the
#: user's stock into the sheet's frame and giving Boundary a boundary that is
#: not the stock at all. Neither is mechanical. See issues.md.
DRESSUP_JOB_LINKS = {}


# -- points in the geometry's frame ----------------------------------------
#
# The other thing `capture_properties` takes verbatim that is not a plain value:
# a property whose value is a **point in the frame of the geometry the
# operation's `Base` names**. Copy such a value across to a nested copy and it
# still reads in the source part's frame, while the geometry it was chosen
# against has been rigidly moved -- so it names a place that is no longer on the
# part. See issues.md NEST-024.
#
# `StartPoint` is the case that matters. `Path.Op.Area` hands it straight to
# `Path.fromShapes` as `pathParams["start"]`
# (`Path/Op/Area.py:282-283` and `:378-379`), which takes the point's XY as a
# hint for where to begin and cuts from the nearest point on the wire. Measured
# on the harness fixture, uncompensated:
#
#     correct start on the nested part -> begins there, 0.000000 mm from it
#     stale start, copy at (140, 110)   -> begins 40.0000 mm away, wrong corner
#     stale start, copy at (430, 260)   -> begins 0.0000 mm away, by luck
#
# The last line is why this is not detectable from a toolpath: a rectangle's four
# corners are interchangeable under its own symmetry, so the nearest point to a
# coordinate 470 mm wrong still came out at the right one. The stale coordinate
# also reaches the G-code as a rapid, which on a sheet whose origin is a corner
# puts the tool off the stock entirely.
#
# `EndPoint` is the same kind of value on the operations that have one.
#
# **Deliberately NOT in this table**, and the reason is the point: `Helix`
# carries `StartPoint`/`EndPoint` too, and they are *not* part-frame values --
# they describe where the helix sits in machine coordinates, and a Helix is tied
# to the tool, not to a part. No per-operation exception is needed, because a
# Helix names no part geometry and so never reaches the transform: see
# `carried_geometry_frame_points`, whose caller has already resolved which
# single source object this copy came from, and there is none.
#
# `Surface` is left to the same rule rather than special-cased. It has a `Base`,
# so when that Base names a part its start point *is* part-relative and is
# carried; when it does not, there is no transform to apply.
GEOMETRY_FRAME_POINTS = ("StartPoint", "EndPoint")

#: Which property says whether a `GEOMETRY_FRAME_POINTS` entry is one the
#: operation actually reads, where the flag is named consistently.
#:
#: Measured by creating one operation of each kind and reading `PropertiesList`,
#: because the flag is not uniform and guessing it would be a silent behaviour
#: change:
#:
#:   op           StartPoint  UseStartPoint  EndPoint  UseEndPoint  SortingMode
#:   Profile      yes         False           --        --            Automatic
#:   PocketShape  yes         False           --        --            Automatic
#:   Slot         yes         False           --        --            --
#:   Waterline    yes         False           --        --            --
#:   Surface      yes         False           --        --            --
#:   Engrave      yes         -- (absent)     yes      False         Automatic
#:   Drilling     yes         -- (absent)     yes      False         Automatic
#:   Helix        yes         -- (absent)     yes      False         Automatic
#:   Adaptive     -- (absent) --              --       --            --
#:
#: So: `EndPoint` always has `UseEndPoint`. `StartPoint` has `UseStartPoint` on
#: the operations built on `Path.Op.Base`, and on the rest it is consumed as the
#: seed of a nearest-neighbour sort, which `SortingMode == "Automatic"` turns on.
#: An operation with neither flag does not read the point at all.
GEOMETRY_FRAME_POINT_FLAGS = {"StartPoint": "UseStartPoint", "EndPoint": "UseEndPoint"}

#: The fallback flag for a `StartPoint` on an operation that has no
#: `UseStartPoint`. `SortingMode` gates the TSP sort that consumes it, and
#: `"Automatic"` is the mode that does the sorting; `"Manual"` uses the order the
#: user listed in `Base`.
GEOMETRY_FRAME_POINT_SORT_FLAG = ("SortingMode", "Automatic")


def geometry_frame_point_in_use(obj, name):
    """Whether `obj` actually reads `name`, or merely carries it.

    FreeCAD gives nearly every operation a `StartPoint`, and it defaults to
    `(0, 0, 0)` with `UseStartPoint` False. Transforming a point nothing reads
    would be churn on every replay, and warning about one would be noise: the
    committed fixture is 98 operations and exactly one has `UseStartPoint` True,
    so an ungated check reports 97 false alarms. The flags are CAM's own, read
    rather than inferred, so this stays right as CAM changes which operation has
    which.
    """
    flag = GEOMETRY_FRAME_POINT_FLAGS.get(name)
    if flag is not None and hasattr(obj, flag):
        return bool(getattr(obj, flag))
    if name == "StartPoint" and hasattr(obj, GEOMETRY_FRAME_POINT_SORT_FLAG[0]):
        return getattr(obj, GEOMETRY_FRAME_POINT_SORT_FLAG[0]) == \
            GEOMETRY_FRAME_POINT_SORT_FLAG[1]
    return False


def source_to_clone_placement(clone, source_geometry):
    """The rigid motion taking `source_geometry`'s frame to `clone`'s.

    `flatten_container` folds the container placement, the part's own placement,
    the Z normalisation and the sheet-origin shift into the flattened object's
    placement, and a nested copy is the same underlying geometry rigidly moved
    (module docstring constraints 5 and 6). So this is the whole transform and
    nothing else is needed.

    **`Shape.Placement`, not `Placement`.** `PathJob.Create` puts a
    `draftobjects.clone.Clone` in the Model, and Draft's Clone restores
    `obj.Placement` *after* assigning a Shape that already carries the original's
    placement (`draftobjects/clone.py:122-148`), so the two are not guaranteed
    to agree.

    Returns None when either side has no shape to read, which is the only case
    where this cannot be formed.
    """
    source_shape = getattr(source_geometry, "Shape", None)
    clone_shape = getattr(clone, "Shape", None)
    if source_shape is None or clone_shape is None:
        return None
    if source_shape.isNull() or clone_shape.isNull():
        return None
    return clone_shape.Placement.multiply(source_shape.Placement.inverse())


def carried_geometry_frame_points(source_op, transform, target_op=None):
    """Map `GEOMETRY_FRAME_POINTS` to their values moved by `transform`.

    Only the points `source_op` actually reads, so an operation with
    `UseStartPoint` False contributes nothing. A point the source does not carry,
    or carries but does not read, is absent from the result rather than mapped to
    `None` -- `apply_properties` treats a `None` remap as "skip this property",
    which would drop a value the replay should have left alone.

    **XY rides the rigid motion. Z does not**, and that is measured rather than
    preferred.

    A `StartPoint`'s Z is a *derived* value, not an authored coordinate:
    `CommandSetStartPoint` writes `obj.StartPoint.z = obj.ClearanceHeight.Value`
    (`Path/Op/Gui/Base.py:1716-1719`), so on the committed fixture the source
    operation's Z is 5.000 mm and its `ClearanceHeight` is 5.000 mm -- the same
    number. Transforming it moves a derived value into a frame it was never
    authored in.

    What that costs, measured on the fixture's `Profile005`:

        Z ridden rigidly   StartPoint.z 5.000 -> 6.000, Z levels [0, 3, 5, **6**]
        Z from ClearanceHeight            StartPoint.z = 5.000, Z levels [0, 3, 5]

    The 6.000 is a millimetre above the operation's own clearance height, and it
    reaches the toolpath. Z is emitted whenever it exceeds the operation's heights
    -- measured on that same operation, z of 0, 3 and 5 leave the levels at
    `[0, 3, 5]` while z of 6 and 40 add a level each.

    And Z has no say in *where* the cut begins, which is what the point is for.
    Same operation, same XY, z swept 0 -> 40: the first cutting move is
    `(42.6, 166.408)` every single time. `Path.fromShapes` uses the XY to pick
    the point on the wire.

    So the Z is taken from the target operation's own `ClearanceHeight` -- the
    same rule the GUI writes, so a job set up in the CAM workbench round-trips
    unchanged -- and the transform's Z is used only when the target has no
    `ClearanceHeight` to read. `compare_stock_frames` already reports the source
    and replay Z frames disagreeing, which is the honest place for that.
    """
    carried = {}
    if transform is None:
        return carried
    for name in GEOMETRY_FRAME_POINTS:
        if not hasattr(source_op, name):
            continue
        if not geometry_frame_point_in_use(source_op, name):
            continue
        try:
            carried[name] = transform.multVec(FreeCAD.Vector(getattr(source_op, name)))
        except Exception:
            # A point that will not transform is reported by the caller's
            # verification, not silently carried wrong. Leaving it out means it
            # is copied verbatim, which is the pre-fix behaviour and visible.
            continue
        if name == "StartPoint" and target_op is not None:
            clearance = getattr(target_op, "ClearanceHeight", None)
            if clearance is not None:
                try:
                    carried[name] = FreeCAD.Vector(
                        carried[name].x, carried[name].y, float(clearance.Value))
                except Exception:
                    pass
    return carried


def unmapped_geometry_frame_points(source_op):
    """Names of `GEOMETRY_FRAME_POINTS` that `source_op` reads.

    Used by the report for the cases where no transform can be formed -- an
    operation left whole over many parts, or one naming no geometry. The value is
    then still in the source job's frame and the caller has to say so.
    """
    return [name for name in GEOMETRY_FRAME_POINTS
            if hasattr(source_op, name) and geometry_frame_point_in_use(source_op, name)]


def unmapped_job_links(dressup, properties):
    """Return captured job-local link properties that the replay did not remap.

    `Base` is deliberately excluded: the replay owns it, having just built it.
    Everything else that is a link carried across from the source job is a
    candidate for having been repointed and was not.
    """
    found = []
    for name in properties:
        if name in DRESSUP_JOB_LINKS or name == "Base":
            continue
        try:
            type_id = dressup.getTypeIdOfProperty(name)
        except Exception:
            continue
        if not type_id.startswith("App::PropertyLink"):
            continue
        value = getattr(dressup, name, None)
        if value is not None and hasattr(value, "Name"):
            found.append((name, value))
    return found


def apply_properties(target, properties, skip=(), remap=None):
    """Assign captured properties onto `target`.

    Properties in `skip` are left alone. `remap` maps a property name to a
    replacement value, which is how `ToolController` is pointed at the new
    job's copy rather than the source job's.

    A property that refuses to be assigned is collected and returned rather
    than raised on. One read-only or type-incompatible property on one
    operation should not cost the whole replay, but the caller has to hear
    about it.
    """
    applied, skipped = [], []
    for name, value in properties.items():
        if name in skip:
            skipped.append(name)
            continue
        if remap and name in remap:
            value = remap[name]
            if value is None:
                skipped.append(name)
                continue
        try:
            setattr(target, name, value)
            applied.append(name)
        except Exception:
            skipped.append(name)
    return applied, skipped


def replay_recipe(recipe, job, clones, tool_cache=None,
                  view_failures=None, progress=None, obsolete_tools=None):
    """Recreate `recipe`'s operations in `job`, targeting `clones`.

    Two passes, in that order, and the order is not a preference.
    `Operations.Group` was observed on a live job as
    `['DressupLeadInOut', 'Profile', 'Drilling']` -- the dressup ahead of the
    operation it wraps. A single pass raises `KeyError` when the dressup cannot
    find its base. The first version of the probe did exactly that.

    `clones` must be the job's model clones, not the objects passed to
    `PathJob.Create`. See `ReplayJob`.

    Every source operation that cannot be recreated lands in
    `result.failures` with a reason. Nothing is dropped quietly.
    """
    result = ReplayResult()
    if tool_cache is None:
        tool_cache = {}
    if view_failures is None:
        view_failures = []
    result.view_provider_failures = view_failures

    if progress is not None:
        progress.stage("Replaying the recipe")
    # One unit per operation the job will actually contain.
    #
    # That is no longer `len(recipe)`: a source step is now split into one
    # operation per target part, so the fixture builds 98 where the recipe held
    # 7. The total is not known until pass one has resolved every step's base
    # selection, which is why `split_total` is filled in at the head of pass two
    # and read from here. Counting `len(recipe)` would have read "7 of 7" while
    # 98 operations were being built.
    steps = len(recipe)
    split_total = [0]
    built = [0]

    def report(label):
        if progress is not None:
            built[0] += 1
            progress.item(built[0], split_total[0] or steps, label)

    made = {}

    # -- pass one: base operations --
    #
    # **One operation per target part, not one operation over all of them.**
    # FreeCAD's Profile is superlinear in the number of Base entries it holds.
    # Measured on the committed fixture: one Profile over 23 parts costs
    # **8.85 s**, while twenty-three Profiles over one part each cost **0.58 s**
    # including creation -- stable across three forced passes. The cost is per
    # operation rather than per target, so the fix is to make the operations
    # match the parts.
    #
    # What that costs, also measured. Cutting length rises by **6.5 mm on
    # 3760 mm, +0.17%**, because LeadInOut already inserts one lead-in per
    # *target* rather than per operation -- 230 cut moves on the bare Profile
    # and 253 with the dressup, across 23 parts, so exactly one per part either
    # way, and a split adds no twenty-fourth. The rapid *count* is unchanged at
    # 68; only the distance moves, because one operation links between adjacent
    # parts more tightly than many operations with boundaries between them.
    #
    # Each part keeps the source operation's whole selection, so the user's
    # per-part recipe is reproduced intact. What is multiplied is how many
    # times, which is the one thing their setup could not have expressed an
    # opinion about: a source job machines a single part, so it says nothing
    # about where the other twenty-two copies go. `order_operations` is what
    # bounds that freedom.
    for item in recipe:
        if progress is not None and progress.cancelled:
            result.failures.append(
                "Cancelled after %d of %d operation(s) were built."
                % (built[0], split_total[0] or steps)
            )
            break
        if is_dressup(item.source):
            continue

        # Resolve the base selection BEFORE creating anything: how many
        # operations to build is how many entries there are, so creating one up
        # front and splitting afterwards would mean building it twice.
        #
        # Each source entry becomes one entry per *matching* clone. Expanding
        # every selection to every clone would be wrong as soon as one
        # operation spans two part types: a selection of Face1 on a Bracket and
        # Face5 on a Spacer would put Face5 on the brackets.
        #
        # **The source object travels with each entry.** The geometry a point in
        # the geometry's frame is expressed relative to *is* this object, so a
        # copy needs to know which one it came from to move such a point. That
        # used to be discarded here, when `(geometry, subs)` became
        # `(clone, subs)`, and discarding it is what made NEST-024 unfixable
        # without a second pass over the recipe.
        base_value = []
        for geometry, subs in item.base_entries:
            targets = clones_for_source(clones, geometry) if clones else []
            if not targets:
                result.failures.append(
                    "Operation '%s' has base geometry but the job has no "
                    "clones to point it at." % item.label
                )
                continue
            if not subs or "" in subs:
                base_value.extend((clone, [""], geometry) for clone in targets)
                continue
            missing, detail = check_subnames_against_clones(subs, targets)
            for name, note in detail.items():
                if name:
                    result.subname_detail[name] = note
            if missing:
                result.failures.append(
                    "Operation '%s' selects %s, which %s. Not replayed, because "
                    "it would cut a different feature than intended."
                    % (item.label, ", ".join(sorted(missing)),
                       "does not exist on the nested geometry"
                       if len(missing) == len(subs) else
                       "resolves on only some of the nested parts")
                )
                continue
            base_value.extend((clone, subs, geometry) for clone in targets)

        # What to build. Each unit is `(label_for, base_entries_to_assign,
        # source_geometry)`.
        #
        # Split: one unit per target, each holding a single-entry Base, and
        # named after its part so the job is diagnosable at 98 operations.
        # Whole: one unit carrying the full Base, for a step whose stack carries
        # an unsplittable dressup -- see `UNSPLITTABLE_DRESSUPS` for why that is
        # measured rather than guessed.
        # Neither: one unit with no Base at all, for a source operation that
        # carries no base selection, replayed exactly as it always was.
        #
        # `source_geometry` is None whenever the unit does not resolve to exactly
        # one source object, which is what makes the point-carrying below
        # well-defined rather than a guess: a whole unit covers many parts, so
        # there is no single frame to move a point out of.
        #
        # The whole unit's Base is rebuilt as `(clone, subs)` pairs. `base_value`
        # holds triples, and assigning that straight to `Base` fails --
        # `Expects sequence of items of type DocObj, (DocObj,SubName)`, caught by
        # the unsplit-start-point check in the harness.
        if not split_is_safe(item):
            units = [(None,
                      [(clone, subs) for clone, subs, _g in base_value] or None,
                      None)]
        elif base_value:
            units = [(clone, [(clone, subs)], geometry)
                     for clone, subs, geometry in base_value]
        else:
            units = [(None, None, None)]

        # -- the cases where a carried point cannot be moved --
        #
        # A unit with `source_geometry` None reads a point in the source job's
        # frame and there is no single frame to move it out of: either the step
        # was left whole over many parts, or it named no geometry at all. That
        # is reported rather than patched, on the same reasoning as
        # `DRESSUP_JOB_LINKS` -- see its note for why the obvious fix is wrong.
        #
        # Reported **once per source step**, not once per unit: a whole step
        # builds one unit anyway, and a split step always has its transform, so
        # this cannot fire 98 times on the committed fixture.
        if all(unit[0] is None for unit in units):
            stranded = unmapped_geometry_frame_points(item.source)
            if stranded:
                plural = len(stranded) > 1
                result.warnings.append(
                    "Operation '%s' reads %s, but it %s, so no single transform "
                    "applies and %s left in the source job's frame. Check the "
                    "toolpath before running it. See issues.md NEST-024."
                    % (item.label, " and ".join(stranded),
                       ("was left whole over %d part(s)"
                        % len(units[0][1] or []))
                       if units[0][1] else "names no geometry",
                       "were" if plural else "was")
                )

        # Hoisted out of the split loop: one tool controller per source
        # operation, shared by every copy. `copy_tool_controller` caches on the
        # source, so this is the same controller asked once rather than N times.
        #
        # **The tool controller is shared; the point remap cannot be.** A carried
        # point depends on which clone the copy targets, so it is added per unit
        # below into a copy of this dict rather than into this dict itself.
        remap = {}
        new_tc = copy_tool_controller(item.source, job, tool_cache)
        if new_tc is not None:
            remap["ToolController"] = new_tc

        created = []
        for label_for, base_entries, source_geometry in units:
            if obsolete_tools:
                # The controllers this job arrived with, removed *before* the
                # first operation object exists.
                #
                # `PathJob.Create` puts a `TC: 5mm Endmill` in every new job,
                # and the replay adds the user's own controller next to it. The
                # job's SetupSheet resolves the active tool from the controllers
                # present, so leaving both in place makes it prompt -- once per
                # operation created, all 98 of them on the fixture. The user
                # reports it as "still there in the new Job"; it is there for
                # the whole of pass one, and the prune at the end of the sheet
                # never ran because it raised (see `remove_tool_controllers`).
                #
                # Done here rather than before the loop because the user's
                # controller is *copied in* inside this loop, by
                # `copy_tool_controller`. Removing the defaults first would leave
                # a window with no tool at all, which is the same prompt from
                # the other direction.
                removed = remove_tool_controllers(job, obsolete_tools)
                obsolete_tools = None
                if removed:
                    FreeCAD.Console.PrintMessage(
                        "Removed the tool controller(s) the new job came with, "
                        "before creating any operation: %s\n" % ", ".join(removed))
            new_op = create_operation_in(
                item.source, job,
                label_suffix=split_label_suffix(label_for)
                if label_for is not None else "_replay",
                view_failures=view_failures)
            if new_op is None:
                result.failures.append(
                    "Could not recreate operation '%s'; its type does not expose "
                    "a Create function this module can drive." % item.label
                )
                continue

            # Move any point that is expressed in the source geometry's frame
            # into this copy's frame. Per unit, not hoisted: the same source
            # operation becomes one copy per part and each copy sits somewhere
            # different, so a point carried once would be right for one of them.
            #
            # `label_for` is the clone this unit targets, and None for a unit that
            # was left whole -- see the note on `units`.
            unit_remap = dict(remap)
            if label_for is not None and source_geometry is not None:
                transform = source_to_clone_placement(label_for, source_geometry)
                unit_remap.update(
                    carried_geometry_frame_points(item.source, transform, new_op))

            apply_properties(new_op, item.properties, remap=unit_remap or None)

            if base_entries is not None:
                try:
                    new_op.Base = base_entries
                except Exception as exc:
                    result.failures.append(
                        "Could not set Base on '%s': %s" % (new_op.Label, exc)
                    )
                    continue

            result.operations.append(new_op)
            created.append(new_op)

        if created:
            made[id(item.source)] = created

    # -- pass two: the dressup stacks, innermost dressup first --
    #
    # A stack is rebuilt in one go, dressup on dressup, because the outer one
    # needs the inner one to exist before it can be layered on. The operation
    # at the bottom is the first layer's base; each subsequent dressup takes the
    # previous object. `recipe` stores a stack innermost-first, which is
    # already the build order.
    #
    # Only the outermost goes in the list, and the list is written once at the
    # end. See `set_operation_order` for why, and the note on the double cut
    # in `create_dressup_in`.
    #
    # Each split copy gets its own stack, and a copy's steps stay adjacent and
    # in source order. That adjacency is what preserves the user's per-part
    # recipe -- holes before the boundary, internals before the outer boundary
    # -- without needing to tell a hole cut from a boundary cut, which a
    # whole-object selection does not allow. Position ordering may move a
    # copy's group relative to another copy's; it may never reorder within one.
    split_total[0] = sum(len(copies) for copies in made.values())
    ordered = []
    for item in recipe:
        if progress is not None and progress.cancelled:
            # `ordered` keeps whatever finished, and `set_operation_order`
            # writes that prefix. The job is kept and labelled `_UNVERIFIED` by
            # the caller, which is the point: an operator who cancels wants to
            # see how far it got, not to find the evidence deleted.
            result.failures.append(
                "Cancelled after %d of %d operation(s) were built."
                % (built[0], split_total[0] or steps)
            )
            break
        bases = made.get(id(item.source)) or []
        if not bases:
            # Pass one already recorded why. Anything left in the stack cannot
            # be built on nothing.
            continue

        for new_op in bases:
            report(new_op.Label)
            if progress is not None and progress.cancelled:
                # Polled per copy, not per recipe item. The two are not the same
                # distance apart: the fixture's first step lands on 23 parts, so
                # a poll only at the item boundary would run 23 operations past
                # a cancel. Each copy is independent -- it owns its own stack --
                # so breaking here leaves nothing half-built.
                result.failures.append(
                    "Cancelled after %d of %d operation(s) were built."
                    % (built[0], split_total[0] or steps)
                )
                break
            copy_suffix = _split_suffix_of(new_op)
            layer = new_op
            outermost = new_op
            built_any = False
            for spec in item.dressups:
                kind, module_name = dressup_kind(spec.source)
                if kind == "unsupported":
                    result.unsupported_dressups.append(
                        (module_name or "?", spec.label, DRESSUP_UNSUPPORTED[module_name])
                    )
                    continue
                if kind == "unknown":
                    result.failures.append(
                        "Dressup '%s' is of a kind this replay does not recognise "
                        "(proxy %s), so it was not applied."
                        % (spec.label, module_name or "none")
                    )
                    continue

                # The dressup carries the split suffix, not just the
                # operation. The dressup is the entry listed in
                # `Operations.Group`, so its label is the one the user sees in
                # the tree -- naming only the operation left 98 entries reading
                # `DressupLeadInOut_replay001`.
                new_dressup = create_dressup_in(
                    spec.source, layer, job, label_suffix=copy_suffix,
                    view_failures=view_failures)
                if new_dressup is None:
                    result.failures.append(
                        "Could not recreate dressup '%s'."
                        % spec.label
                    )
                    continue
                apply_properties(new_dressup, spec.properties, skip=("Base",))

                # A link carried across from the source job resolves, so nothing
                # raises; the replayed dressup just points at an object in another
                # job. Say so rather than leaving it to be found on a second sheet.
                for link_name, target in unmapped_job_links(new_dressup,
                                                            spec.properties):
                    # Named by the REPLAYED label, not the source's: the reader is
                    # looking at the job that was just created, and the source job
                    # may not even be open.
                    result.warnings.append(
                        "%s.%s still points at %r, which belongs to the source "
                        "job. It will be evaluated there, not on this sheet. Check "
                        "it before posting."
                        % (new_dressup.Label, link_name,
                           getattr(target, "Label", "?"))
                    )

                # A constructor that installed an expression the source did not have
                # is now recomputing over the value just applied, and the user's
                # setting will be replaced on the next recompute. Caught here rather
                # than on the machine -- see `expression_bound`.
                stray = expression_bound(new_dressup) - expression_bound(spec.source)
                if stray:
                    result.warnings.append(
                        "Dressup '%s' recomputes %s from an expression, which will "
                        "override the value carried over from the source job. The "
                        "replayed job will not cut the same as the source."
                        % (spec.label, ", ".join(sorted(stray)))
                    )

                result.dressups.append(new_dressup)
                layer = new_dressup
                outermost = new_dressup
                built_any = True

            if built_any or not item.dressups:
                ordered.append(outermost)
                # What belongs in `Operations.Group` for this copy. Recorded
                # rather than left implicit, because the ordering step is handed
                # the BASE operations and has to write these instead.
                result.entry_of[id(new_op)] = outermost

    # One write, in recipe order. See `set_operation_order`.
    set_operation_order(job, ordered)

    for warning in result.failures:
        FreeCAD.Console.PrintWarning("%s\n" % warning)
    for _kind, label, _reason in result.unsupported_dressups:
        FreeCAD.Console.PrintWarning(
            "Dressup '%s' was not replayed; see the summary.\n" % label)
    for warning in result.warnings:
        FreeCAD.Console.PrintWarning("%s\n" % warning)
    return result


def describe_replay_result(result):
    """Return a human-readable summary of a `ReplayResult`.

    Reports the sub-element check explicitly, because it is the check that
    stands between a plausible-looking job and one that cuts the wrong
    feature.
    """
    lines = [
        "Replayed %d operation(s) and %d dressup(s)."
        % (len(result.operations), len(result.dressups))
    ]
    for kind, label, reason in result.unsupported_dressups:
        lines.append(
            "WARNING: dressup '%s' (%s) was not replayed -- %s. Its operation "
            "was replayed bare, so this cut has no dressup applied."
            % (label, kind, reason)
        )
    partial = {n: d for n, d in result.subname_detail.items() if d != "ok"}
    for name, note in sorted(partial.items()):
        lines.append("WARNING: sub-element %s %s." % (name, note))
    for failure in result.failures:
        lines.append("WARNING: %s" % failure)
    if not result.failures and not partial:
        lines.append("All sub-element selections resolved on every nested part.")
    return lines


# -- operation ordering ---------------------------------------------------
#
# The user's operation order for an individual part is authoritative and is
# reproduced exactly. The one thing that has to change is the case where a part
# is nested INSIDE another part's hole: the outer part's hole must be cut
# *after* everything sitting inside it, or the inner part falls out while
# still held only by the ring of material around it, and its own operations are
# then cutting a loose piece.
#
# So the rule is a partial order, not a re-sort. Everything else keeps the
# order the user chose, and the group is only touched when hole nesting
# actually happened.
#
# Detecting the nesting. The nester can place a part inside another's hole --
# the inner-fit-polygon rings are in the production candidate path
# (minkowski_engine.get_incremental_candidates) -- but it records nothing about
# which part went into which hole, so the relationship is recovered from the
# finished layout: a part is inside another's hole when its footprint is
# contained in one of that part's interior rings.
#
# A trap worth naming: this must NOT be done with
# `shape_processor.get_2d_profile_from_obj`. That returns a polygon *centred
# on the shape*, not in world coordinates. Measured: a shape spanning
# X 100..120 yields a profile spanning -10..10. Comparing that against world
# positions gives a confident wrong answer -- a part sitting well outside a
# hole reads as inside it. `part_footprint` below slices the shape instead and
# keeps world coordinates, so the two polygons are in the same frame.

# Z fraction at which a footprint is taken. Mid-thickness, so a part with a
# step or a boss on one face is still measured through its main body.
FOOTPRINT_Z_FRACTION = 0.5


class FootprintCache:
    """Footprints for one sheet's parts, computed once.

    `part_footprint` slices the shape with OCC and discretises the wires. On the
    committed fixture the first call measured **543 ms** and later ones about
    150 ms. It was being called twice over the same shapes with nothing shared
    between them: once over all 48 parts by `find_hole_nestings`, then again
    from `operation_touches_hole` inside `order_operations` -- **21 times over 3
    distinct shapes**, measured. That duplication was 11.40 s of
    `order_operations`' 11.71 s.

    Scoped to a run rather than cached at module level, on purpose. A global
    cache keyed on anything cheap goes stale: a shape can be edited while its
    object `Name` stays the same, and `id()` is reused once an object is
    collected, so a later part could inherit an earlier part's footprint. Within
    one sheet's replay the parts are fixed, which is exactly the window a cache
    is safe over.

    `hits` and `misses` are counted rather than merely implied, so a caller can
    report what the cache did instead of asserting that it helped.
    """

    def __init__(self):
        self._by_key = {}
        self.hits = 0
        self.misses = 0

    def get(self, part):
        key = id(part)
        if key in self._by_key:
            self.hits += 1
            return self._by_key[key]
        self.misses += 1
        footprint = part_footprint(part)
        self._by_key[key] = footprint
        return footprint


def _footprint_for(part, cache):
    """`part`'s footprint, via `cache` when there is one."""
    if cache is None:
        return part_footprint(part)
    return cache.get(part)


def part_position(part):
    """Return `(x, y)` at the centre of `part`'s XY bounds, or None.

    The XY centre, not the first vertex: nearest-neighbour travel is a coarse
    preference and a representative point serves it. Returns None rather than a
    guess when the bounds cannot be read, and the caller reads that as "no
    position to prefer" rather than as the origin.
    """
    shape = getattr(part, "Shape", None)
    if shape is None:
        return None
    try:
        if shape.isNull():
            return None
        centre = shape.BoundBox.Center
    except Exception:
        return None
    return (float(centre.x), float(centre.y))


def _footprint_box(part):
    """Return `(x0, y0, x1, y1)` for `part`, or None if it cannot be read."""
    shape = getattr(part, "Shape", None)
    if shape is None:
        return None
    try:
        if shape.isNull():
            return None
        bounds = shape.BoundBox
    except Exception:
        return None
    return (bounds.XMin, bounds.YMin, bounds.XMax, bounds.YMax)


def _boxes_may_overlap(a, b):
    """True if two XY boxes could overlap, or if either could not be read.

    An unreadable box returns True. A part whose extent cannot be established
    cannot be pruned on evidence that is not there, and the unsafe direction
    here is dropping a nesting that is really there.
    """
    if a is None or b is None:
        return True
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def part_footprint(part, z_fraction=FOOTPRINT_Z_FRACTION):
    """Return a Shapely Polygon of `part`'s footprint in WORLD coordinates.

    Slices the shape at `z_fraction` of its own height and turns the resulting
    closed wires into a polygon: the longest wire is the outer boundary, the
    rest are holes.

    Returns None when the slice yields nothing usable, or when Shapely is
    unavailable. Callers treat None as "cannot tell" and fall back to the
    conservative ordering rather than assuming no hole nesting.

    Note this deliberately does not reuse `get_2d_profile_from_obj`, which
    centres its output -- see the note above the function.
    """
    try:
        from shapely.geometry import Polygon
    except ImportError:
        return None

    shape = getattr(part, "Shape", None)
    if shape is None or shape.isNull():
        return None

    box = shape.BoundBox
    height = box.ZMax - box.ZMin
    if height <= 1e-9:
        return None
    z = box.ZMin + height * float(z_fraction)

    try:
        wires = shape.slice(FreeCAD.Vector(0, 0, 1), z)
    except Exception:
        return None
    if not wires:
        return None

    rings = []
    for wire in wires:
        try:
            points = wire.discretize(Deflection=0.05)
        except TypeError:
            try:
                points = wire.discretize(Number=64)
            except Exception:
                continue
        except Exception:
            continue
        if points and len(points) >= 3:
            rings.append(Polygon([(p.x, p.y) for p in points]))

    if not rings:
        return None
    rings.sort(key=lambda p: p.area, reverse=True)
    outer = rings[0]
    holes = [r for r in rings[1:] if r.area > 0 and outer.contains(r)]
    if not holes:
        return outer
    try:
        return Polygon(outer.exterior.coords, [h.exterior.coords for h in holes])
    except Exception:
        return outer


def find_hole_nestings(parts, cache=None):
    """Return `(outer, inner)` pairs where `inner` sits inside `outer`'s hole.

    A part counts as nested in a hole when its footprint is contained in one of
    the other part's interior rings. Containment rather than intersection,
    because a part merely overlapping another is the normal case in a nest and
    says nothing about ordering.

    Only interior rings count. A part lying within another's *outer* boundary
    would be overlapping material, which the nester does not produce.

    Returns an empty list when Shapely is unavailable, so ordering degrades to
    the user's own order rather than failing.

    **The bounding-box prune.** A footprint is sliced out of its own shape, so
    it lies within that shape's XY extent. An inner part inside an outer part's
    hole must therefore have a box that overlaps the outer part's box, and every
    pair whose boxes are disjoint can be dropped without slicing anything.

    On the committed fixture that is 2256 ordered pairs reduced to 34, and 48
    slices reduced to 21 -- which is most of `find_hole_nestings`' 7.36 s. On a
    nest with no hole nesting at all, where no two parts' boxes touch, it is the
    entire cost: zero slices.

    It has no false negatives by construction, and a part whose box cannot be
    read is never pruned. Both facts are pinned by `TestFindHoleNestings`.

    `cache` is an optional `FootprintCache` shared with the ordering step,
    which needs the same footprints and was measuring 11.40 s re-deriving them.
    """
    try:
        from shapely.geometry import Polygon
    except ImportError:
        return []

    parts = list(parts)
    boxes = [(part, _footprint_box(part)) for part in parts]

    candidates = []
    for outer, outer_box in boxes:
        for inner, inner_box in boxes:
            if outer is inner:
                continue
            if _boxes_may_overlap(outer_box, inner_box):
                candidates.append((outer, inner))
    if not candidates:
        return []

    nestings = []
    for outer, inner in candidates:
        outer_fp = _footprint_for(outer, cache)
        if outer_fp is None or not outer_fp.interiors:
            continue
        inner_fp = _footprint_for(inner, cache)
        if inner_fp is None:
            continue
        for ring in outer_fp.interiors:
            try:
                if Polygon(ring.coords).contains(inner_fp):
                    nestings.append((outer, inner))
                    break
            except Exception:
                continue
    return nestings


def operation_touches_hole(operation, part, cache=None):
    """Return True if `operation` would cut one of `part`'s holes.

    Conservative by design: returns True when it cannot tell, including for a
    whole-object selection where internal and external cutting are not
    separable. An operation wrongly assumed to miss the hole would be ordered
    before the parts it holds, which is the failure this exists to prevent, and
    it is a silent one.

    A specific sub-selection is judged by where its elements sit: an element
    whose centre falls inside a hole ring is a hole feature.
    """
    base = getattr(operation, "Base", None)
    if not base or not isinstance(base, (list, tuple)):
        return True

    geometry = None
    subs = []
    for item in base:
        try:
            geometry, subs_for_item = item
        except (TypeError, ValueError):
            return True
        subs.extend(subs_for_item if isinstance(subs_for_item, (list, tuple))
                    else [subs_for_item])
    if geometry is None:
        return True
    if not subs or "" in subs:
        return True

    try:
        from shapely.geometry import Point, Polygon
    except ImportError:
        return True

    footprint = _footprint_for(geometry, cache)
    if footprint is None:
        return True
    if not footprint.interiors:
        # No holes to cut, so nothing can be a hole feature.
        return False

    for name in subs:
        if not name:
            return True
        try:
            element = geometry.Shape.getElement(name)
        except Exception:
            return True
        try:
            centre = element.CenterOfMass
        except Exception:
            return True
        point = Point(centre.x, centre.y)
        for ring in footprint.interiors:
            if Polygon(ring.coords).contains(point):
                return True
    return False


def order_operations(job, operations, nestings, ownership=None, cache=None,
                     entry_of=None, start_point=None):
    """Reorder `job`'s Operations.Group so hole nesting is respected.

    `operations` is the replayed operations in recipe order -- the user's own
    order. `nestings` is the `(outer, inner)` pairs from
    `find_hole_nestings`. `ownership` maps an operation to the part it cuts,
    needed to tell which side of a nesting it belongs to.

    **`entry_of` maps `id(operation)` to the outermost dressup wrapping it, and
    it is what gets written.** The group must list the dressup, never the
    operation beneath it. This function is handed the base operations, because
    that is what the ordering decisions are about, so writing what it was given
    replaced every list entry with a bare operation: measured, 98 entries of
    which **0 were dressups**, with all 105 dressups built, linked and unlisted.
    They would not have been posted, so the user's lead-in and lead-out would
    have silently vanished from the toolpath while every structural check still
    passed. The dressup harness missed it because its fixture has no hole
    nesting, and this write is only reached when there is one.

    **`start_point` is where the chain begins**, XY, defaulting to the sheet
    origin. The parts have already been moved to sheet-local coordinates, so
    (0, 0) is a corner of the sheet rather than an arbitrary point in space.

    Returns the new order. When there is nothing to order -- no operations, or
    none of them carrying a part whose position can be read -- the group is left
    alone entirely: a run that cannot improve the order should not churn the
    document. An order that turns out unchanged is not written either.

    Two rules decide the order among operations that are free to go anywhere:

    * **Where things are.** The parts have been laid out by the nest, so their
      positions on the sheet are real, and cutting near where the torch already
      is costs less than cutting far away. A single-part source job says nothing
      about where to go between its parts, because it never had any. This fills
      that in rather than overriding a choice the user made.
    * **A part is finished before another is started.** Not a preference between
      equal options -- leaving a part half cut and returning to it is a round
      trip, and the return costs more than the entire chain of nearest-neighbour
      decisions saves.
    """
    group = getattr(getattr(job, "Operations", None), "Group", None)
    if not group or not operations:
        return list(operations)

    by_part = ownership or {}
    part_of = {}
    for operation, part in by_part.items():
        part_of[id(operation)] = part

    # Constraint: for each nesting, every hole-cutting op on `outer` must come
    # after every op that cuts `inner`.
    later_than = {id(op): set() for op in operations}
    for outer, inner in nestings or ():
        # `cache` goes positionally on purpose. The tests replace
        # `operation_touches_hole` with `lambda *a: True`, which takes no
        # keyword arguments, so passing it by name would break every one of them.
        outer_ops = [op for op in operations
                     if part_of.get(id(op)) is outer
                     and operation_touches_hole(op, outer, cache)]
        inner_ops = [op for op in operations if part_of.get(id(op)) is inner]
        if not outer_ops or not inner_ops:
            continue
        for op in outer_ops:
            for other in inner_ops:
                if id(op) != id(other):
                    later_than[id(op)].add(id(other))

    # A part's own steps keep their source order -- as a hard constraint, not as
    # a tie-break preference.
    #
    # It has to be hard, because the two rules the user gave can otherwise be
    # satisfied in the wrong order. On the committed fixture the spacers carry
    # two hole steps then their outer boundary (Profile004, Profile005,
    # Profile006), and hole nesting holds the two hole steps back until the
    # nested parts are finished. With within-part order left to the tie-break,
    # the boundary step was not held back with them, so it went first and the
    # result was boundary-then-holes on both spacers -- measured, source indices
    # [96, 92, 94] and [97, 93, 95]. That frees the spacer before the parts
    # nested in its hole have been machined, and the parts fall out.
    #
    # Chained in source order, so the boundary is held behind the holes, and the
    # holes are held behind the nested parts. That is the user's rule 3 -- all
    # of a part's internal work before its boundary -- falling out of the
    # nesting constraint rather than being enforced beside it.
    steps_by_part = {}
    for op in operations:
        part = part_of.get(id(op))
        if part is not None:
            steps_by_part.setdefault(part, []).append(op)
    for steps in steps_by_part.values():
        for earlier, later in zip(steps, steps[1:]):
            later_than[id(later)].add(id(earlier))

    # Topological sort, with the free choices made by position rather than by
    # the original order.
    #
    # **What the tie-break must not break.** Two things, and both are the user's:
    #
    #   * A part's own steps stay in the order they were written. That is
    #     already a hard constraint by this point, so it cannot be broken here.
    #     What is left is a preference for doing them back to back.
    #   * Hole nesting stays hard. `ready` only ever offers operations whose
    #     constraints are already satisfied, so no preference is consulted for an
    #     operation that cannot legally go here yet.
    #
    # The first is why staying on the current part outranks distance: leaving a
    # part half done to start a nearer one, and coming back for it, costs more
    # travel than the distance saved.
    order = list(operations)
    position = {id(op): i for i, op in enumerate(order)}
    where = {}
    for op in operations:
        part = part_of.get(id(op))
        where[id(op)] = part_position(part) if part is not None else None

    # A part with no readable position is not skipped and does not stop the sort.
    # Position is a preference and the nesting constraints are a requirement, so
    # when no position can be read the tie-break falls back to the source order
    # and the constraints are still honoured. Bailing out instead would drop a
    # real hole-nesting constraint because a bounding box could not be read.

    cursor = list(start_point) if start_point else [0.0, 0.0]
    current_part = None

    remaining = list(order)
    ordered = []
    placed = set()
    while remaining:
        # An operation is ready when everything it must follow is already
        # placed. Ops with no dependencies are ready, since all() of an empty
        # set is True.
        ready = [op for op in remaining
                 if all(dep in placed for dep in later_than[id(op)])]
        if not ready:
            # A cycle: the constraints cannot all hold. Keep the user's order
            # for the rest rather than looping, and say so.
            ordered.extend(remaining)
            break

        def rank(op):
            source = position[id(op)]
            if current_part is not None and part_of.get(id(op)) is current_part:
                return (0.0, source)
            at = where[id(op)]
            if at is None:
                return (float("inf"), source)
            return (math.hypot(at[0] - cursor[0], at[1] - cursor[1]), source)

        chosen = min(ready, key=rank)
        ordered.append(chosen)
        placed.add(id(chosen))
        remaining.remove(chosen)
        if part_of.get(id(chosen)) is not current_part:
            current_part = part_of.get(id(chosen))
            at = where[id(chosen)]
            if at is not None:
                cursor = [at[0], at[1]]

    if entry_of:
        # Translate before writing. The sort ran over base operations; the list
        # holds their outermost dressups. Without this the group ends up holding
        # bare operations and every dressup is built, linked and never posted.
        ordered = [entry_of.get(id(op), op) for op in ordered]

    if group is not None and ordered != list(group):
        job.Operations.Group = ordered
    return ordered


# -- verification ---------------------------------------------------------
#
# The point of this step is that a replay which goes wrong should be visible.
#
# Everything upstream can succeed while the job still does not do what the
# source did. The sharpest example: an operation whose base selection resolves
# to nothing computes 3 commands and no cutting motion, and raises nothing.
# Measured early on, on a mis-selected hole. A job like that posts, looks
# plausible, and removes less material than intended.
#
# Two levels of check, deliberately different in severity.
#
# **An operation that cuts nothing is a failure.** Not a warning. Every
# replayed operation exists because the user set it up on their own part, and
# one that produces no motion is far more likely to be a broken mapping than a
# legitimately empty feature. Blocking the sheet is the right response: a loud
# failure that costs one run beats a quiet one that costs a customer's parts.
#
# **An operation that covers only some of its targets is a warning.** Deciding
# whether a given toolpath move belongs to a given part means attributing
# motion to geometry, and a toolpath is a sequence of points with no part
# labels on it. The check here compares bounding boxes, which is a heuristic:
# it will not catch a part that is cut but only partly, and it could in
# principle be confused by a part sitting inside another's toolpath extent. It
# is worth having because the failure it *does* catch -- an operation
# silently covering fewer parts than it targets -- is quiet and expensive, but
# it is a warning rather than a gate because the check itself is approximate.

# A toolpath shorter than this, in mm, is treated as no motion at all. Chosen
# as "smaller than any plausible feature on a sheet" rather than derived; the
# emptiness test below is the real gate and this only guards the degenerate
# case of a path that exists but goes nowhere.
MIN_PATH_LENGTH_MM = 0.001

# Extra room around a target part when testing whether the toolpath reached
# it: the tool radius, plus room for a lead-in arc and for the tolerance of a
# bounding-box comparison on a rotated part.
COVERAGE_MARGIN_MM = 2.0

# Rapid travel, the one G-code that never removes material. Everything else
# that is a G-code does.
RAPID_MOTION = "G0"

# The feed motions, named for readability rather than used for the decision.
# Kept in step with FreeCAD's own motion handling.
CUTTING_MOTIONS = ("G1", "G2", "G3", "G73", "G76", "G81", "G82", "G83", "G84",
                   "G85", "G86", "G87", "G88", "G89")


def is_cutting_motion(code):
    """Return True if a motion code removes material.

    Decided by rule, not by membership of a list: a G-code that is not `G0`
    cuts. Enumerating the cutting codes was the first version and it was wrong
    the moment a Drilling operation was replayed -- a drilling cycle emits
    `G81 [ F:0 R:1 X:.. Y:.. Z:-6 ]`, which is a canned cycle and does cut, but
    is not in a list of `G1/G2/G3`. The verification then reported "produced no
    cutting motion" for a perfectly good drilling operation.

    So the rule is the reliable one and the tuple above is documentation.
    """
    if not code or not code.startswith("G"):
        return False
    return code != RAPID_MOTION


def motion_of(command):
    """Return the motion code of a Path command, or "" if it has none.

    `str(command)` renders as `Command G1 [ X:... ]`, so the second token is
    the motion. Parsed rather than matched on attributes, because an arc
    carries its centre in I/J/K and its end in X/Y/Z, and both forms have to
    be recognised as motion.
    """
    tokens = str(command).split()
    if len(tokens) < 2:
        return ""
    return tokens[1]


def has_cutting_motion(operation):
    """Return True if `operation`'s toolpath contains any cutting move.

    Not a command count. A path that fails to resolve produces three commands
    -- two comment lines and a positioning G0 -- which is non-empty as a list
    and empty as a cut. Counting commands would call that a pass.
    """
    path = getattr(operation, "Path", None)
    if path is None:
        return False
    for command in getattr(path, "Commands", ()) or ():
        if is_cutting_motion(motion_of(command)):
            return True
    return False


def path_bounds(operation):
    """Return `(minx, miny, maxx, maxy)` of an operation's XY moves, or None."""
    path = getattr(operation, "Path", None)
    if path is None:
        return None
    xs, ys = [], []
    for command in getattr(path, "Commands", ()) or ():
        x, y = getattr(command, "X", None), getattr(command, "Y", None)
        if x is None or y is None:
            continue
        xs.append(float(x))
        ys.append(float(y))
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def shape_bounds(obj):
    """Return `(minx, miny, maxx, maxy)` of a document object's shape."""
    shape = getattr(obj, "Shape", None)
    if shape is None or shape.isNull():
        return None
    box = shape.BoundBox
    return (box.XMin, box.YMin, box.XMax, box.YMax)


def uncovered_targets(operation, margin=COVERAGE_MARGIN_MM):
    """Return the labels of target parts the toolpath shows no sign of cutting.

    Approximate, and stated as such: the toolpath is one sequence of points
    with no part labels on it, so the only thing available is to ask whether it
    passes anywhere near each target. A part is judged covered when the
    toolpath's XY extent, grown by `margin`, overlaps the part's own extent.

    `margin` absorbs the tool radius and a lead-in arc. A profile op cuts
    *outside* the part, so its path sits about one tool radius beyond the
    outline; a drilling op cuts inside it, at the hole centres. Overlap in
    either direction catches both, which is why this is not a containment test.
    """
    targets = []
    base = getattr(operation, "Base", None)
    if base and isinstance(base, (list, tuple)):
        for entry in base:
            try:
                targets.append(entry[0])
            except (TypeError, IndexError):
                continue
    if not targets:
        return []

    bounds = path_bounds(operation)
    if bounds is None:
        return [getattr(t, "Label", "?") for t in targets]

    px0, py0, px1, py1 = bounds
    gx0, gy0 = px0 - margin, py0 - margin
    gx1, gy1 = px1 + margin, py1 + margin

    uncovered = []
    for target in targets:
        box = shape_bounds(target)
        if box is None:
            continue
        tx0, ty0, tx1, ty1 = box
        overlaps = (tx0 <= gx1 and tx1 >= gx0) and (ty0 <= gy1 and ty1 >= gy0)
        if not overlaps:
            uncovered.append(getattr(target, "Label", "?"))
    return uncovered


def parts_outside_stock(clones, stock, tolerance=1e-6):
    """Return the labels of parts that do not lie within the stock.

    Not approximate, and not a warning. A part outside the stock means the
    toolpath runs off the material, which is a scrapped part and possibly a
    crashed tool.

    This check exists because the pipeline got it wrong during development: a
    sheet whose parts were never moved to local coordinates kept their world X
    offsets, left the sheet, and every other check still passed. The coverage
    check looked at whether the toolpath reached its targets and it did --
    the targets were simply in the wrong place.
    """
    shape = getattr(stock, "Shape", None)
    if shape is None or shape.isNull():
        return []
    bounds = shape.BoundBox

    outside = []
    for clone in clones:
        shape = getattr(clone, "Shape", None)
        if shape is None or shape.isNull():
            continue
        part = shape.BoundBox
        inside = (part.XMin >= bounds.XMin - tolerance
                  and part.XMax <= bounds.XMax + tolerance
                  and part.YMin >= bounds.YMin - tolerance
                  and part.YMax <= bounds.YMax + tolerance)
        if not inside:
            outside.append(getattr(clone, "Label", "?"))
    return outside


class Verification:
    """The outcome of checking a replayed job.

    `failures` are conditions that should stop the run. `warnings` are
    conditions an operator should know about but which are approximate enough
    not to gate on.
    """

    def __init__(self):
        self.failures = []
        self.warnings = []
        self.operations_checked = 0
        self.operations_with_motion = 0

    @property
    def ok(self):
        return not self.failures

    def __repr__(self):
        return "<Verification %d checked, %d failure(s), %d warning(s)>" % (
            self.operations_checked, len(self.failures), len(self.warnings)
        )


def point_outside_targets(operation, name, margin=COVERAGE_MARGIN_MM):
    """Return True if `operation`'s `name` lies outside every target's footprint.

    The check for a point in the geometry's frame that did not make the move --
    `GEOMETRY_FRAME_POINTS`. Approximate in the same way and for the same reason
    as `uncovered_targets`: bounding boxes, grown by `margin`, because a precise
    test is not worth its cost here.

    Measured on the committed fixture's 48 parts, 1567 edges and 613 faces:

        `Shape.distToShape`   5.59 ms per part on the contour, 9.62 ms off it
                              -> 548-943 ms across the fixture's 98 operations
        this bounding-box test 0.31 ms per part -> 31 ms across the same 98

    So the precise version costs 5-9% of a 10.5s replay, on every replay, to
    answer a question a box answers exactly as well: on that fixture a carried
    point is inside 48 of 48 boxes and a stale one inside 0 of 48. `margin`
    absorbs the same slack `uncovered_targets` uses it for.

    Only asks about a point the operation actually reads, so the 97 of 98
    operations on the fixture with `UseStartPoint` False cost nothing and say
    nothing.
    """
    if not hasattr(operation, name):
        return False
    if not geometry_frame_point_in_use(operation, name):
        return False
    try:
        point = FreeCAD.Vector(getattr(operation, name))
    except Exception:
        return False

    base = getattr(operation, "Base", None)
    targets = []
    if base and isinstance(base, (list, tuple)):
        for entry in base:
            try:
                targets.append(entry[0])
            except (TypeError, IndexError):
                continue
    if not targets:
        # Nothing to be inside of. An operation naming no geometry cannot be
        # checked this way, and the replay already reports that case separately.
        return False

    for target in targets:
        box = shape_bounds(target)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        if (x0 - margin <= point.x <= x1 + margin
                and y0 - margin <= point.y <= y1 + margin):
            return False
    return True


def verify_replay(result, extra_warnings=(), clones=None, stock=None):
    """Check a `ReplayResult` and return a `Verification`.

    `extra_warnings` carries things the caller already knows, such as the Z
    frame comparison from the job builder, so everything an operator needs to
    see arrives in one place. `clones` and `stock`, when given, enable the
    containment check.

    Fails on any replayed operation with no cutting motion, and on any part
    that does not lie within the stock. Warns on any operation whose toolpath
    does not reach all the parts it targets, and on any unresolved
    sub-element from the replay itself.
    """
    verification = Verification()

    if clones and stock is not None:
        stray = parts_outside_stock(clones, stock)
        if stray:
            verification.failures.append(
                "%d part(s) lie outside the stock (%s). The toolpath would run "
                "off the material. This usually means a sheet's parts were not "
                "moved to local coordinates -- check the sheet's origin."
                % (len(stray), ", ".join(sorted(stray)[:5]))
            )

    for operation in result.operations:
        verification.operations_checked += 1
        label = getattr(operation, "Label", "?")
        if has_cutting_motion(operation):
            verification.operations_with_motion += 1
        else:
            n = len(getattr(getattr(operation, "Path", None), "Commands", ()) or ())
            verification.failures.append(
                "Operation '%s' produced no cutting motion (%d command(s), all "
                "travel or comments). Its base selection almost certainly did "
                "not survive the move to the nested geometry. Nothing was cut "
                "for it." % (label, n)
            )
            continue

        uncovered = uncovered_targets(operation)
        if uncovered:
            verification.warnings.append(
                "Operation '%s' shows no toolpath near %d of %d target part(s) "
                "(%s). This check compares bounding boxes and is approximate, "
                "so check the toolpath before assuming the parts were skipped."
                % (label, len(uncovered),
                   len(operation.Base) if getattr(operation, "Base", None) else 0,
                   ", ".join(sorted(uncovered)[:5]))
            )

        # A point in the geometry's frame that is not on any of the parts this
        # operation targets. It was either not carried, or carried for a unit
        # with no single transform -- and either way the toolpath starts from
        # whatever the wire nearest it happens to be. See NEST-024.
        stranded = [name for name in GEOMETRY_FRAME_POINTS
                    if point_outside_targets(operation, name)]
        if stranded:
            verification.warnings.append(
                "Operation '%s' reads %s, but %s not on any part it targets. "
                "It is still in the source job's frame, so the toolpath begins "
                "at the nearest point on the wire to a coordinate that has "
                "nothing to do with this part. Check it before running."
                % (label, " and ".join(stranded),
                   "it is" if len(stranded) == 1 else "they are")
            )

    for name, note in sorted(result.subname_detail.items()):
        if note != "ok":
            verification.warnings.append(
                "Sub-element %s %s." % (name, note)
            )

    verification.warnings.extend(result.warnings)
    verification.warnings.extend(extra_warnings)
    return verification


def describe_verification(verification):
    """Return a human-readable report of a `Verification`.

    Failures first, because they are the ones that stop the run.
    """
    lines = []
    if verification.failures:
        lines.append("%d failure(s):" % len(verification.failures))
        for failure in verification.failures:
            lines.append("  FAIL: %s" % failure)
    if verification.warnings:
        lines.append("%d warning(s):" % len(verification.warnings))
        for warning in verification.warnings:
            lines.append("  WARNING: %s" % warning)
    if verification.ok:
        lines.append(
            "Verified %d operation(s), all with cutting motion."
            % verification.operations_checked
        )
    if verification.operations_with_motion < verification.operations_checked:
        lines.append(
            "%d of %d operation(s) produced cutting motion."
            % (verification.operations_with_motion, verification.operations_checked)
        )
    return lines


# -- orchestration --------------------------------------------------------
#
# The pipeline, in one place: flatten a sheet, build a job, replay the recipe
# into it, order the operations, verify. The command layer above this only
# chooses options and reports.

# Appended to a job's label when verification fails. The job is kept rather
# than deleted -- you asked to be able to dig into what went wrong -- but a
# half-built job left under a plausible name is the kind of thing someone
# later mistakes for a working one. The label says so.
UNVERIFIED_SUFFIX = "_UNVERIFIED"


def is_cam_job(obj):
    """Return True if `obj` looks like a FreeCAD CAM job.

    A job is a `Path::FeaturePython` carrying an `Operations` group, a `Model`
    group and a `Stock`. Checking for the three is more robust than checking
    the proxy class, which differs between FreeCAD versions and between the
    App-level and GUI-level constructors.
    """
    if obj is None:
        return False
    if not obj.isDerivedFrom("Path::Feature"):
        return False
    return all(hasattr(obj, attr) for attr in ("Operations", "Model", "Stock"))


def resolve_layout_group(doc, strict=False):
    """Return `(layout_group, warnings)` for `doc`.

    Uses the shared `get_layout_group` helper, which prefers `__temp_Layout`
    and otherwise takes the most recent `Layout_*` group. That is the
    auto-detection the existing CAM command's selection check points at.

    The ambiguity is worth surfacing rather than resolving silently. A document
    can hold several layout groups -- the GA creates one per layout and renames
    the winner, and `__temp_Layout` is a separate temporary -- so "the most
    recent" is a guess whenever more than one exists. The guess is reported
    with the candidates, because picking the wrong layout means replaying
    somebody else's nest and calling it a success.

    `strict` turns the ambiguity into a hard stop by returning None, for a
    caller that would rather ask than guess.
    """
    from ...freecad_helpers import get_layout_group

    warnings = []
    candidates = [obj for obj in getattr(doc, "Objects", ())
                  if obj.isDerivedFrom("App::DocumentObjectGroup")
                  and obj.Label.startswith("Layout_")]
    candidates += [obj for obj in getattr(doc, "Objects", ())
                   if obj.isDerivedFrom("App::DocumentObjectGroup")
                   and obj.Label.startswith("__temp_Layout")]

    if len(candidates) > 1:
        names = sorted(obj.Label for obj in candidates)
        message = (
            "%d layout group(s) are present in this document (%s). Using "
            "%s. Select a specific layout if this is not the one you meant."
            % (len(candidates), ", ".join(names),
               "the most recent" if not strict else "none")
        )
        if strict:
            return None, [message]
        warnings.append(message)

    return get_layout_group(doc), warnings


class SheetOutcome:
    """Everything that happened to one sheet."""

    def __init__(self, sheet_group, replay_job=None, result=None,
                 verification=None, ordering=None, errors=None):
        self.sheet_group = sheet_group
        self.replay_job = replay_job
        self.result = result
        self.verification = verification
        self.ordering = ordering or []
        self.errors = errors or []
        #: `[(stage, seconds, runs)]` for this sheet, from `Progress`. Empty
        #: when no progress seam was in use. Carried on the outcome rather than
        #: returned separately so that whoever ran the pipeline is the one who
        #: can see where the time went, whether that is the command's report or
        #: the fixture validator.
        self.timings = []
        #: Seconds the stages accounted for. Below the wall clock when work
        #: happened outside any stage, which is itself worth reporting.
        self.timed_seconds = 0.0

    @property
    def ok(self):
        return not self.errors and (self.verification is None or self.verification.ok)

    @property
    def sheet_label(self):
        return getattr(self.sheet_group, "Label", "?")

    def __repr__(self):
        return "<SheetOutcome %s ok=%s>" % (self.sheet_label, self.ok)


def replay_sheet(doc, layout_group, sheet_group, source_job, post_processor=None,
                 template_path=None, job_factory=None, progress=None):
    """Run the whole pipeline for one sheet.

    Returns a `SheetOutcome`. Nothing is deleted on failure: an operator asked
    to be able to dig into a bad run needs the job still there to look at, and
    the verification failure says so loudly in the report.

    The order is fixed and each stage depends on the previous one:

      1. read the recipe from the source job, once per run rather than per
         sheet -- every sheet machines the same parts, so the recipe is the
         same and the geometry is not;
      2. flatten the sheet's nested containers;
      3. build a job sized to the sheet;
      4. replay the recipe into that job;
      5. order the operations for hole nesting;
      6. verify.
    """
    outcome = SheetOutcome(sheet_group)
    if sheet_group is None:
        outcome.errors.append("No sheet to replay.")
        return outcome

    _width, _height, thickness = read_sheet_dimensions(layout_group)

    if progress is not None:
        progress.stage("Reading the source CAM setup")
    try:
        recipe = read_recipe(source_job)
    except Exception as exc:
        outcome.errors.append("Could not read the recipe from %s: %s"
                              % (getattr(source_job, "Label", "the job"), exc))
        return outcome

    if not len(recipe):
        outcome.errors.append(
            "%s has no operations, so there is nothing to replay."
            % getattr(source_job, "Label", "The source job")
        )
        return outcome

    # Staging. The flattened parts are built here and end up in the job's Model,
    # so this group is temporary: they only need somewhere to sit while the job
    # is created, which is the moment the user would otherwise see forty-eight
    # objects at the document root per sheet. It is dropped again once they have
    # been adopted -- see `adopt_flattened_parts_as_model`.
    job_name = replay_job_name(source_job,
                               getattr(sheet_group, "Label", "") or "")
    try:
        parts_group = doc.addObject(
            "App::DocumentObjectGroup", "%s_parts" % job_name)
        parts_group.Label = "%s_parts" % job_name
        doc.recompute()
    except Exception as exc:
        parts_group = None
        FreeCAD.Console.PrintWarning(
            "Could not create a group for %s's flattened parts: %s\n"
            % (outcome.sheet_label, exc))

    try:
        flattened = flatten_sheet(doc, sheet_group, thickness, group=parts_group,
                                 progress=progress)
    except Exception as exc:
        outcome.errors.append("Could not flatten %s: %s" % (outcome.sheet_label, exc))
        return outcome

    if not len(flattened):
        outcome.errors.append(
            "%s yielded no parts to cut. Run nesting for this layout first."
            % outcome.sheet_label
        )
        return outcome

    if progress is not None:
        progress.stage("Building the replay job")
    try:
        replay = create_replay_job(
            doc, layout_group, sheet_group, flattened.parts,
            source_job=source_job, post_processor=post_processor,
            template_path=template_path, job_factory=job_factory,
        )
    except Exception as exc:
        outcome.errors.append("Could not create a job for %s: %s"
                              % (outcome.sheet_label, exc))
        return outcome

    if replay is None:
        outcome.errors.append("Could not create a job for %s." % outcome.sheet_label)
        return outcome
    outcome.replay_job = replay

    # The staging group is empty now: `adopt_flattened_parts_as_model` moved its
    # members into the job's Model and took them out. Remove it rather than
    # leave an empty `<job>_parts` at the document root per sheet.
    #
    # If the swap was refused -- a custom `job_factory` whose Model is not clones
    # of our parts -- the group still holds them and the check below leaves it
    # alone, which is the right outcome: those parts are still what the
    # Clones point at.
    if parts_group is not None and not len(getattr(parts_group, "Group", []) or []):
        try:
            doc.removeObject(parts_group.Name)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                "Could not remove the now-empty staging group for %s: %s\n"
                % (outcome.sheet_label, exc))

    # What the job arrived with. Snapshot now, before the replay adds the
    # user's own controller, so the two can be told apart.
    obsolete_tools = list(getattr(getattr(replay.job, "Tools", None),
                                  "Group", None) or [])

    try:
        result = replay_recipe(recipe, replay.job, replay.clones, progress=progress,
                               obsolete_tools=obsolete_tools)
    except Exception as exc:
        outcome.errors.append("Could not replay into %s: %s" % (outcome.sheet_label, exc))
        return outcome
    outcome.result = result
    outcome.errors.extend(result.failures)
    for note in result.view_provider_failures:
        FreeCAD.Console.PrintWarning(
            "View provider: %s. The replayed job will look and behave "
            "partly like a plain object.\n" % note)

    # Safety net, not the main event. `replay_recipe` drops what the job came
    # with before its first operation; anything still unreferenced here came
    # from a custom `job_factory` whose job was not empty to begin with.
    try:
        removed_tools = prune_unused_tool_controllers(replay.job)
        if removed_tools:
            doc.recompute()
            FreeCAD.Console.PrintMessage(
                "Removed unused tool controller(s) from %s: %s\n"
                % (replay.job.Label, ", ".join(removed_tools)))
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Could not tidy the tool table on %s: %s\n"
            % (replay.job.Label, exc))

    # Ordering needs to know which part each replayed operation cuts, so each
    # op is attributed to the part its first base entry points at.
    if progress is not None:
        progress.stage("Ordering and tidying the tool table")
    try:
        if progress is not None and progress.cancelled:
            # Deliberately not ordering a cancelled replay. Pass one builds
            # every base operation before pass two dresses them, so after a
            # cancel `result.operations` is complete while the list holds only
            # the stacks that were finished. Ordering reads that as "98
            # operations, 23 entries", writes all 98 back into the list, and
            # every dressup it replaced becomes an unlisted orphan -- the
            # NEST-015 failure reached by another road. Measured before this
            # guard: cancelled 5 operations in, and the list came back holding
            # 98 bare operations with every dressed one orphaned.
            #
            # The job is `_UNVERIFIED` and must not be posted from, so its list
            # is left exactly as far as the replay got.
            FreeCAD.Console.PrintMessage(
                "Cancelled before ordering: leaving %d of %d operation(s) "
                "dressed in the list.\n"
                % (len(list(replay.job.Operations.Group)),
                   len(result.operations)))
        else:
            # One cache for both steps. `find_hole_nestings` and
            # `order_operations` need the same footprints; before this they
            # each derived their own, and the ordering step did it 21 times
            # over 3 distinct shapes.
            footprints = FootprintCache()
            nestings = find_hole_nestings(replay.clones, footprints)

            # Built unconditionally, not under `if nestings`. Ordering by where
            # the parts sit needs to know which part each operation cuts, and
            # that has nothing to do with hole nesting -- a flat nest has no
            # nesting at all and is exactly the case with the most to gain from
            # cutting near where the torch already is.
            ownership = {}
            for op in result.operations:
                base = getattr(op, "Base", None)
                if base and isinstance(base, (list, tuple)):
                    try:
                        ownership[op] = base[0][0]
                    except (TypeError, IndexError):
                        pass

            # Compared as ENTRIES, not as the base operations that are passed
            # in: `ordered` comes back translated to the outermost dressups, so
            # comparing it against the base operations would report every entry
            # as moved every time, however little actually changed.
            before = [result.entry_of.get(id(o), o) for o in result.operations]
            ordered = order_operations(replay.job, result.operations, nestings,
                                       ownership, footprints,
                                       entry_of=result.entry_of)
            FreeCAD.Console.PrintMessage(
                "Footprints: %d sliced, %d reused, across %d nesting(s).\n"
                % (footprints.misses, footprints.hits, len(nestings)))
            if list(ordered) != before:
                # Counted, not listed. At 98 operations the "a then b then c"
                # form ran to several thousand characters of labels in the
                # Report view and said nothing actionable. What is worth
                # saying is how much moved and under which rule.
                moved = sum(1 for a, b in zip(before, ordered) if a is not b)
                outcome.ordering.append(
                    "%s: %d of %d operation(s) reordered -- a part is finished "
                    "before another is started, working across the sheet from "
                    "the corner in; %d part(s) nested in another's hole are "
                    "finished before that hole is cut"
                    % (outcome.sheet_label, moved, len(ordered), len(nestings))
                )
    except Exception as exc:
        outcome.errors.append(
            "Could not order the operations for %s: %s" % (outcome.sheet_label, exc)
        )

    # Its own stage, because it is not cheap and it was not being counted.
    # This recompute was measured at 24.0s of a 43.5s run -- the single largest
    # item in the whole replay -- while sitting between two stages, so it
    # appeared in neither. A timing table that silently omits the largest cost
    # is worse than no table, because it points at the wrong thing.
    if progress is not None:
        progress.stage("Recomputing the toolpaths")
    doc.recompute()
    if progress is not None:
        progress.done()

    if progress is not None:
        progress.stage("Verifying the result")
    try:
        verification = verify_replay(result, replay.warnings,
                                    clones=replay.clones, stock=replay.stock)
    except Exception as exc:
        outcome.errors.append("Could not verify %s: %s" % (outcome.sheet_label, exc))
        return outcome
    outcome.verification = verification

    if not outcome.ok:
        _mark_unverified(replay, outcome)

    if progress is not None:
        progress.done("verified %d operation(s)"
                      % (0 if verification is None
                         else verification.operations_checked))
    return outcome


def _mark_unverified(replay, outcome):
    """Label a job that did not replay cleanly, and say so loudly.

    Runs on *any* failure, not only a failed verification. An operation that
    could not be replayed at all -- because its sub-element selection did not
    survive the move to the nested geometry -- leaves a job that cuts less than
    the source, and that is exactly as dangerous as a job whose operations
    produce no motion. The first version of this only labelled on a
    verification failure, and a real replay failure slipped through unlabelled.

    The job is kept either way. Deleting the evidence would be the wrong
    instinct: the point of keeping it is that the failure can be investigated.
    """
    job = replay.job
    try:
        if not job.Label.endswith(UNVERIFIED_SUFFIX):
            job.Label = "%s%s" % (job.Label, UNVERIFIED_SUFFIX)
    except Exception:
        pass
    reasons = []
    if outcome.verification is not None and not outcome.verification.ok:
        reasons.append("%d verification failure(s)"
                       % len(outcome.verification.failures))
    if outcome.errors:
        reasons.append("%d replay error(s)" % len(outcome.errors))
    FreeCAD.Console.PrintError(
        "%s did not replay cleanly (%s) and is left in the document, labelled "
        "%s, for inspection. Nothing should be posted from it.\n"
        % (outcome.sheet_label, ", ".join(reasons) or "unknown reason",
           getattr(job, "Label", "?"))
    )


def replay_layout(doc, layout_group, source_job, post_processor=None,
                  template_path=None, job_factory=None, progress_callback=None,
                  cancel_check=None, sheets=None):
    """Replay `source_job` onto every sheet of `layout_group`.

    Returns a list of `SheetOutcome`, one per sheet. A sheet that fails does
    not stop the others: they are independent jobs and one bad sheet should not
    hide a good one.

    `progress_callback(stage, current, total, message=None)` is optional and
    reported per sheet, with the sheet label prefixed to every message so a
    multi-sheet run can be told apart. Default `None`, so the harness runs this
    with no callback at all and is unaffected. See `Progress` for why the
    callback is shaped this way rather than like the nester's.

    `cancel_check()` is polled at every point the pipeline can stop, and returns
    True to ask for the run to finish. It is a poll rather than a callback
    because the thing holding the Cancel button lives on the GUI thread, where
    this runs, and a boolean read is all that needs to cross. Cancelling keeps
    what was built and marks the job `_UNVERIFIED` rather than deleting it -- an
    operator who cancels wants to see how far it got.

    `sheets` restricts the run to a subset of the layout's sheets, in the
    order given. `None`, the default, means all of them. It exists because a
    per-sheet progress display has to open and close around each sheet, and
    without it the only way to do that is to call `replay_sheet` directly and
    lose this function's error handling.
    """
    from ...freecad_helpers import get_sheet_groups

    outcomes = []
    for sheet in (sheets if sheets is not None
                  else get_sheet_groups(layout_group)):
        progress = Progress(progress_callback,
                            prefix=getattr(sheet, "Label", "") or "",
                            cancel_check=cancel_check)
        outcome = replay_sheet(
            doc, layout_group, sheet, source_job,
            post_processor=post_processor, template_path=template_path,
            job_factory=job_factory, progress=progress)
        # Banked here, not inside `replay_sheet`, because this is the frame that
        # owns the seam: the sheet does not know when its last stage really
        # ended, and a `Progress` that closed its own final stage would report a
        # stage that ran to the end of the *run*.
        outcome.timings = progress.timing_rows()
        outcome.timed_seconds = progress.elapsed
        outcomes.append(outcome)
    return outcomes


def describe_timings(timings, timed_seconds=None, wall_clock=None, indent="  "):
    """Return report lines for where a replay's time went.

    Sorted by cost, largest first, because that is the question the table is
    asked. The pipeline's own stage order is the progress bar's business.

    `wall_clock` is the caller's own measurement of the whole run. When it is
    given and does not match `timed_seconds`, the difference is printed rather
    than hidden: time spent outside any stage is still time the operator spent,
    and a table that quietly drops it points at the wrong stage.
    """
    if not timings:
        return []
    rows = sorted(timings, key=lambda row: -row[1])
    total = sum(seconds for _name, seconds, _runs in rows)
    width = max(len(name) for name, _s, _r in rows)

    lines = ["%swhere the time went:" % indent]
    for name, seconds, runs in rows:
        share = (100.0 * seconds / total) if total > 0 else 0.0
        times = "  (x%d)" % runs if runs > 1 else ""
        lines.append("%s%-*s %8.2fs %5.1f%%%s"
                     % (indent, width, name, seconds, share, times))

    accounted = timed_seconds if timed_seconds is not None else total
    lines.append("%s%-*s %8.2fs" % (indent, width, "accounted for", accounted))
    if wall_clock is not None:
        gap = wall_clock - accounted
        if gap > 0.05:
            lines.append("%s%-*s %8.2fs  outside any stage"
                         % (indent, width, "unaccounted", gap))
        lines.append("%s%-*s %8.2fs" % (indent, width, "wall clock",
                                        wall_clock))
    return lines


def describe_sheet_outcome(outcome):
    """Return a human-readable report for one `SheetOutcome`."""
    lines = ["%s:" % outcome.sheet_label]
    if outcome.replay_job is not None:
        lines.extend("  " + line for line in describe_replay_job(outcome.replay_job))
    if outcome.result is not None:
        lines.extend("  " + line for line in describe_replay_result(outcome.result))
    if outcome.verification is not None:
        lines.extend("  " + line for line in describe_verification(outcome.verification))
    for note in outcome.ordering:
        lines.append("  %s" % note)
    for error in outcome.errors:
        lines.append("  FAIL: %s" % error)
    if outcome.ok:
        lines.append("  OK")
    return lines


def describe_recipe(recipe):
    """Return a human-readable summary of `recipe` for the report view.

    Deliberately plain text over `FreeCAD.Console`, not `FreeCADGui.ReportView`:
    Console output reaches the Report view when a GUI is up, and unlike
    `ReportView` it also works under `freecadcmd`, so the same code path is
    exercised by the headless harness.

    Each line is `n. <label>  [dressups: <names>]  [base: <n> entr(y|ies)]`,
    so an operator can see at a glance what will be replayed before running it.
    """
    lines = []
    if not recipe.operations:
        lines.append("No operations found in %s." % getattr(recipe.job, "Label", "the job"))
    for index, item in enumerate(recipe.operations, start=1):
        dressup_names = " -> ".join(item.dressup_labels)
        parts = ["%d. %s" % (index, item.label)]
        if dressup_names:
            parts.append("[%s]" % dressup_names)
        if item.base_entries:
            parts.append("[base: %d %s]" % (
                len(item.base_entries),
                "entry" if len(item.base_entries) == 1 else "entries",
            ))
        lines.append("  ".join(parts))
    for dressup in recipe.unresolved_dressups:
        lines.append(
            "WARNING: dressup '%s' does not wrap an operation; it cannot be "
            "replayed." % getattr(dressup, "Label", "?")
        )
    for entry in getattr(recipe, "normalised_entries", []):
        lines.append(
            "  '%s' is listed separately from a dressup layered on it; replayed "
            "as part of that dressup's step, so it is not cut twice."
            % getattr(entry, "Label", "?")
        )
    return lines
