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
