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

3. **Model entries are clones, and the clone hides custom properties.**
   `PathJob.Create` replaces base geometry with `draftobjects.clone.Clone`
   objects, and a custom property added to the original (`MyMeta`) is *not*
   visible on the clone. So the geometry the operations reference is the clone,
   while the identity a human recognises is the original's label. Both are
   recorded: `Base` targets the clone, and `SourceObject` is recovered from
   `clone.Objects[0]` so the write half can match nested copies by something
   more reliable than a label.

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
import FreeCAD

# -- job creation ---------------------------------------------------------
#
# The job is named after the sheet it machines, so a multi-sheet nest produces
# one clearly-labelled job per sheet rather than a single ambiguous one.

JOB_NAME_PREFIX = "CAM_Replay_"
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

    The important field is `clones`. `PathJob.Create` does not keep the
    geometry it is given: it replaces each base object with a
    `draftobjects.clone.Clone` in `job.Model.Group`. An operation's `Base` must
    therefore point at those clones, not at the objects passed in.

    Verified: three input features became `['Clone', 'Clone001', 'Clone002']`
    labelled `Model-p1` .. `Model-p3`, and `Model.Group[0] is not parts[0]`.
    Handing the caller its own input list back would be a quiet way to produce
    a job whose operations reference geometry the job does not contain, so
    `clones` is the list to use and `source_parts` is kept only for reporting.

    `warnings` is a list of human-readable strings, currently the Z frame
    comparison described on `compare_stock_frames`.
    """

    def __init__(self, job, clones, stock, sheet_group, source_parts, warnings):
        self.job = job
        self.clones = list(clones)
        self.stock = stock
        self.sheet_group = sheet_group
        self.source_parts = list(source_parts)
        self.warnings = list(warnings)

    def __repr__(self):
        return "<ReplayJob %s clones=%d stock=%s>" % (
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
    job_name = "%s%s" % (JOB_NAME_PREFIX, sheet_label or "Sheet")

    job = factory(job_name, parts, template_path)
    if job is None:
        return None
    doc.recompute()

    # The clones are the job's real geometry. Read them from the job rather
    # than assuming the caller's list survived.
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
    doc.recompute()

    warnings = []
    if source_job is not None:
        warnings.extend(compare_stock_frames(
            getattr(source_job, "Stock", None), stock, sheet_label
        ))

    return ReplayJob(job, clones, stock, sheet_group, parts, warnings)


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


class OperationRecipe:
    """One operation from the source job, with the dressups wrapped around it.

    Attributes:
        source: the original job object this was read from.
        properties: the captured settings, replayable onto a new object.
        dressups: list of (properties, source_dressup) in group order.
        base_entries: the source operation's `Base` as (object, subs) pairs.
            Retained so the write half can read the sub-element names off the
            source operation rather than re-deriving them; these are what make
            the replay possible at all, since a nested copy keeps identical
            topology and therefore identical sub-element numbering.
    """

    def __init__(self, source, properties, base_entries=None):
        self.source = source
        self.properties = properties
        self.dressups = []
        self.base_entries = list(base_entries or [])

    @property
    def label(self):
        return getattr(self.source, "Label", "<unnamed>")

    def add_dressup(self, properties, source_dressup):
        self.dressups.append((properties, source_dressup))

    def __repr__(self):
        return "<OperationRecipe %s (%d dressup(s))>" % (self.label, len(self.dressups))


class Recipe:
    """The source job's operations, in group order, with dressups attached.

    `operations` is in `Operations.Group` order, which is the order the job
    processes them. Note that this is not the order the dressups appear in the
    group: a dressup is listed where the user put it, and on a real job
    `['DressupLeadInOut', 'Profile', 'Drilling']` put the dressup ahead of the
    operation it wraps. Attaching in a second pass is what makes that
    representable.

    `unresolved_dressups` holds any dressup whose base operation was not in the
    group. These are reported rather than dropped: a dressup with no operation
    cannot be replayed, and silently losing one would produce a job that cuts
    differently from the source with nothing to say why.
    """

    def __init__(self, job):
        self.job = job
        self.operations = []
        self.unresolved_dressups = []

    def __len__(self):
        return len(self.operations)

    def __iter__(self):
        return iter(self.operations)

    def __repr__(self):
        return "<Recipe %d operation(s), %d unresolved dressup(s)>" % (
            len(self.operations), len(self.unresolved_dressups))


def read_recipe(job):
    """Read `job`'s operations and dressups into a `Recipe`.

    Two passes, in this order and for the reason recorded above: the first
    collects operations, the second attaches dressups to them. A single pass
    raises `KeyError` on a group that lists a dressup before its operation,
    which is the ordinary case rather than an edge case.

    `Base` entries are read from the source operation and kept on the recipe.
    They are (geometry object, sub-element list) pairs, and the sub-element
    names are the whole mechanism by which a replay can target a nested copy
    that has different placement but identical topology.

    An unresolvable dressup -- one whose `Base` is not in the group, which
    happens if the user deleted the operation but left the dressup -- is
    recorded on `recipe.unresolved_dressups` rather than raised. It cannot be
    replayed, and the caller decides whether that is worth warning about.
    """
    recipe = Recipe(job)

    operations_group = getattr(getattr(job, "Operations", None), "Group", None)
    if not operations_group:
        return recipe

    by_identity = {}
    for entry in operations_group:
        if is_dressup(entry):
            continue
        properties = capture_properties(entry)
        item = OperationRecipe(entry, properties, _read_base_entries(entry))
        recipe.operations.append(item)
        by_identity[id(entry)] = item

    for entry in operations_group:
        if not is_dressup(entry):
            continue
        owner = by_identity.get(id(entry.Base))
        if owner is None:
            recipe.unresolved_dressups.append(entry)
            continue
        owner.add_dressup(capture_properties(entry), entry)

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


def flatten_sheet(doc, sheet_group, sheet_thickness, group=None):
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

    result = FlattenResult()
    for container in get_nested_containers(sheet_group):
        flattened = flatten_container(doc, container, sheet_thickness, group=group)
        if flattened is None:
            result.skipped.append((container, "no part_* child, or its shape is null"))
            continue
        result.parts.append(flattened.obj)
        if flattened.z_offset:
            result.z_shifts.append((flattened.obj, flattened.z_offset))
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
    """Return the job clones that correspond to `source_geometry`.

    Returns every clone when the match cannot be narrowed, which is the safe
    direction: an operation that applies to a few too many parts is visible in
    the toolpath, whereas one that applies to too few silently cuts less than
    the source.

    Narrowing happens when the source geometry and the clones name the same
    part type. The chain back from a job clone to a part is long:

        Model-Bracket (source job's clone)
          -> Bracket                       (Objects[0], the user's original)
        Clone (replay job's clone)
          -> CAMPart_12                    (Objects[0], the flattened part)
          -> part_Bracket_1                (SourceObject)
          -> nested_Bracket_1              (SourceContainer)

    so the part type is recovered from `NestedLabel` as
    `nested_<type>_<n>` and compared against the source's label. That is a
    format extraction, not a document search: it is not the `findObjects`
    prefix match that makes `nested_Bracket_1` also return `nested_Bracket_10`
    and `nested_Bracket_11`.
    """
    # Unwrap first. An operation's Base points at the *source job's* clone,
    # labelled `Model-Bracket`, not at the user's `Bracket` -- so comparing
    # that label against `nested_Bracket_1` matches nothing and every
    # operation falls back to targeting every clone. Verified: with the
    # unwrap missing, a 2-bracket 1-spacer nest matched 3 of 3 for both.
    original = resolve_source_object(source_geometry) or source_geometry
    source_label = getattr(original, "Label", "")
    if not source_label:
        return list(clones)

    matched = []
    for clone in clones:
        original = getattr(clone, "Objects", None)
        flattened = original[0] if original else None
        if flattened is None:
            continue
        nested = getattr(flattened, PROP_NESTED_LABEL, "")
        if not nested:
            continue
        parts = nested.split("_")
        if len(parts) >= 3 and "_".join(parts[1:-1]) == source_label:
            matched.append(clone)

    return matched if matched else list(clones)


def create_operation_in(source_op, job, label_suffix="_replay"):
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


def create_dressup_in(source_dressup, base_op, job, label_suffix="_replay"):
    """Create a copy of `source_dressup` wrapping `base_op`, in `job`.

    `Path.Dressup.Gui.LeadInOut.Create` is not used: it touches a ViewProvider
    and raises `AttributeError: 'NoneType' object has no attribute 'Object'`
    under `freecadcmd`, which is where the tests run. Constructing
    `ObjectDressup` and registering with `job.Proxy.addOperation` works
    headless and produces the same object -- confirmed, and the resulting
    dressup picked up the lead-in and added a command over its base operation.

    Returns the new object, or None if the dressup's constructor is not one
    this module can drive. The constructor is taken from the source
    dressup's proxy module for the same reason operations are: so a Boundary or
    Dogbone dressup replays as readily as a LeadInOut.

    The document comes from the job rather than being passed in. There is no
    reason to hand this function a document that might not be the one holding
    the job, and doing so once produced `AttributeError: 'NoneType' object has
    no attribute 'addObject'` -- a dressup that silently failed to be created.
    """
    import importlib

    proxy = getattr(source_dressup, "Proxy", None)
    if proxy is None or base_op is None:
        return None
    doc = getattr(job, "Document", None) or FreeCAD.ActiveDocument
    if doc is None:
        return None
    try:
        module = importlib.import_module(type(proxy).__module__)
    except ImportError:
        return None
    constructor = getattr(module, "ObjectDressup", None)
    if constructor is None:
        return None

    name = "%s%s" % (getattr(source_dressup, "Label", "Dressup"), label_suffix)
    try:
        new_obj = doc.addObject("Path::FeaturePython", name)
        constructor(new_obj, base_op)
        job.Proxy.addOperation(new_obj, base_op)
    except Exception as exc:
        FreeCAD.Console.PrintWarning(
            "Could not recreate dressup '%s': %s\n"
            % (getattr(source_dressup, "Label", "?"), exc)
        )
        return None
    return new_obj


class ReplayResult:
    """The outcome of replaying a recipe into a job.

    `operations` are the new base operations, in replay order. `dressups` are
    the new dressup objects, keyed by nothing in particular -- they are
    reported, not looked up.

    `failures` holds human-readable strings for anything that could not be
    replayed. A replay that silently dropped an operation would produce a job
    that cuts less than the source, and nothing would say so.
    """

    def __init__(self):
        self.operations = []
        self.dressups = []
        self.failures = []
        self.warnings = []
        self.subname_detail = {}

    def __len__(self):
        return len(self.operations)

    def __repr__(self):
        return "<ReplayResult %d op(s), %d dressup(s), %d failure(s)>" % (
            len(self.operations), len(self.dressups), len(self.failures)
        )


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


def replay_recipe(recipe, job, clones, tool_cache=None):
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

    made = {}

    # -- pass one: base operations --
    for item in recipe:
        if is_dressup(item.source):
            continue
        new_op = create_operation_in(item.source, job)
        if new_op is None:
            result.failures.append(
                "Could not recreate operation '%s'; its type does not expose a "
                "Create function this module can drive." % item.label
            )
            continue

        remap = {}
        new_tc = copy_tool_controller(item.source, job, tool_cache)
        if new_tc is not None:
            remap["ToolController"] = new_tc

        applied, skipped = apply_properties(
            new_op, item.properties, remap=remap or None
        )

        # Re-point at the clones, keeping the source's sub-element selection.
        #
        # Each source entry becomes one entry per *matching* clone. Expanding
        # every selection to every clone would be wrong as soon as one
        # operation spans two part types: a selection of Face1 on a Bracket and
        # Face5 on a Spacer would put Face5 on the brackets.
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
                base_value.extend((clone, [""]) for clone in targets)
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
            base_value.extend((clone, subs) for clone in targets)

        if base_value:
            try:
                new_op.Base = base_value
            except Exception as exc:
                result.failures.append(
                    "Could not set Base on '%s': %s" % (item.label, exc)
                )
                continue

        result.operations.append(new_op)
        made[id(item.source)] = new_op

    # -- pass two: dressups, now that their bases exist --
    for item in recipe:
        for _props, source_dressup in item.dressups:
            base = made.get(id(source_dressup.Base))
            if base is None:
                result.failures.append(
                    "Dressup '%s' wraps an operation that was not replayed; it "
                    "cannot be replayed either."
                    % getattr(source_dressup, "Label", "?")
                )
                continue
            new_dressup = create_dressup_in(source_dressup, base, job)
            if new_dressup is None:
                result.failures.append(
                    "Could not recreate dressup '%s'."
                    % getattr(source_dressup, "Label", "?")
                )
                continue
            apply_properties(new_dressup, _props, skip=("Base",))
            result.dressups.append(new_dressup)

    for warning in result.failures:
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


def find_hole_nestings(parts):
    """Return `(outer, inner)` pairs where `inner` sits inside `outer`'s hole.

    A part counts as nested in a hole when its footprint is contained in one of
    the other part's interior rings. Containment rather than intersection,
    because a part merely overlapping another is the normal case in a nest and
    says nothing about ordering.

    Only interior rings count. A part lying within another's *outer* boundary
    would be overlapping material, which the nester does not produce.

    Returns an empty list when Shapely is unavailable, so ordering degrades to
    the user's own order rather than failing.
    """
    footprints = []
    for part in parts:
        footprint = part_footprint(part)
        if footprint is not None:
            footprints.append((part, footprint))

    nestings = []
    for outer, outer_fp in footprints:
        if not outer_fp.interiors:
            continue
        for inner, inner_fp in footprints:
            if inner is outer:
                continue
            for ring in outer_fp.interiors:
                try:
                    from shapely.geometry import Polygon
                    if Polygon(ring.coords).contains(inner_fp):
                        nestings.append((outer, inner))
                        break
                except Exception:
                    continue
    return nestings


def operation_touches_hole(operation, part):
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

    footprint = part_footprint(geometry)
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


def order_operations(job, operations, nestings, ownership=None):
    """Reorder `job`'s Operations.Group so hole nesting is respected.

    `operations` is the replayed operations in recipe order -- the user's own
    order. `nestings` is the `(outer, inner)` pairs from
    `find_hole_nestings`. `ownership` maps an operation to the part it cuts,
    needed to tell which side of a nesting it belongs to.

    Returns the new order. When there are no nestings, or nothing is out of
    order, the group is left alone entirely -- a run that does not need
    reordering should not churn the document.

    The sort is a topological sort that breaks ties by the original order, so
    the user's sequence is preserved wherever the constraints allow it. That is
    the whole point: the replay reproduces what the user set up, and only moves
    what physically has to move.
    """
    group = getattr(getattr(job, "Operations", None), "Group", None)
    if not group or not nestings or not operations:
        return list(operations)

    by_part = ownership or {}
    part_of = {}
    for operation, part in by_part.items():
        part_of[id(operation)] = part

    # Constraint: for each nesting, every hole-cutting op on `outer` must come
    # after every op that cuts `inner`.
    later_than = {id(op): set() for op in operations}
    has_constraint = False
    for outer, inner in nestings:
        outer_ops = [op for op in operations
                     if part_of.get(id(op)) is outer and operation_touches_hole(op, outer)]
        inner_ops = [op for op in operations if part_of.get(id(op)) is inner]
        if not outer_ops or not inner_ops:
            continue
        for op in outer_ops:
            for other in inner_ops:
                if id(op) != id(other):
                    later_than[id(op)].add(id(other))
                    has_constraint = True

    if not has_constraint:
        return list(operations)

    # Stable topological sort, preferring the original order at every step.
    order = list(operations)
    position = {id(op): i for i, op in enumerate(order)}
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
        chosen = min(ready, key=lambda op: position[id(op)])
        ordered.append(chosen)
        placed.add(id(chosen))
        remaining.remove(chosen)

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

    for name, note in sorted(result.subname_detail.items()):
        if note != "ok":
            verification.warnings.append(
                "Sub-element %s %s." % (name, note)
            )

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

    @property
    def ok(self):
        return not self.errors and (self.verification is None or self.verification.ok)

    @property
    def sheet_label(self):
        return getattr(self.sheet_group, "Label", "?")

    def __repr__(self):
        return "<SheetOutcome %s ok=%s>" % (self.sheet_label, self.ok)


def replay_sheet(doc, layout_group, sheet_group, source_job, post_processor=None,
                 template_path=None, job_factory=None):
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

    try:
        flattened = flatten_sheet(doc, sheet_group, thickness)
    except Exception as exc:
        outcome.errors.append("Could not flatten %s: %s" % (outcome.sheet_label, exc))
        return outcome

    if not len(flattened):
        outcome.errors.append(
            "%s yielded no parts to cut. Run nesting for this layout first."
            % outcome.sheet_label
        )
        return outcome

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

    try:
        result = replay_recipe(recipe, replay.job, replay.clones)
    except Exception as exc:
        outcome.errors.append("Could not replay into %s: %s" % (outcome.sheet_label, exc))
        return outcome
    outcome.result = result
    outcome.errors.extend(result.failures)

    # Ordering needs to know which part each replayed operation cuts, so each
    # op is attributed to the part its first base entry points at.
    try:
        nestings = find_hole_nestings(replay.clones)
        if nestings:
            ownership = {}
            for op in result.operations:
                base = getattr(op, "Base", None)
                if base and isinstance(base, (list, tuple)):
                    try:
                        ownership[op] = base[0][0]
                    except (TypeError, IndexError):
                        pass
            before = [getattr(o, "Label", "?") for o in result.operations]
            ordered = order_operations(replay.job, result.operations, nestings, ownership)
            after = [getattr(o, "Label", "?") for o in ordered]
            if before != after:
                outcome.ordering.append(
                    "%s: reordered for hole nesting, %s -> %s"
                    % (outcome.sheet_label, " then ".join(before),
                       " then ".join(after))
                )
    except Exception as exc:
        outcome.errors.append(
            "Could not order the operations for %s: %s" % (outcome.sheet_label, exc)
        )

    doc.recompute()

    try:
        verification = verify_replay(result, replay.warnings,
                                    clones=replay.clones, stock=replay.stock)
    except Exception as exc:
        outcome.errors.append("Could not verify %s: %s" % (outcome.sheet_label, exc))
        return outcome
    outcome.verification = verification

    if not outcome.ok:
        _mark_unverified(replay, outcome)

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
                  template_path=None, job_factory=None):
    """Replay `source_job` onto every sheet of `layout_group`.

    Returns a list of `SheetOutcome`, one per sheet. A sheet that fails does
    not stop the others: they are independent jobs and one bad sheet should not
    hide a good one.
    """
    from ...freecad_helpers import get_sheet_groups

    outcomes = []
    for sheet in get_sheet_groups(layout_group):
        outcomes.append(replay_sheet(
            doc, layout_group, sheet, source_job,
            post_processor=post_processor, template_path=template_path,
            job_factory=job_factory,
        ))
    return outcomes


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
        dressup_names = ", ".join(
            getattr(src, "Label", "?") for _props, src in item.dressups
        )
        parts = ["%d. %s" % (index, item.label)]
        if dressup_names:
            parts.append("[dressups: %s]" % dressup_names)
        if item.base_entries:
            parts.append("[base: %d %s]" % (
                len(item.base_entries),
                "entry" if len(item.base_entries) == 1 else "entries",
            ))
        lines.append("  ".join(parts))
    for dressup in recipe.unresolved_dressups:
        lines.append(
            "WARNING: dressup '%s' wraps an operation that is not in the job's "
            "operations group; it cannot be replayed."
            % getattr(dressup, "Label", "?")
        )
    return lines
