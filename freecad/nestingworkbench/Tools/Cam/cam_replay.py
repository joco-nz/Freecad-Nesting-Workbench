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
   search for labels.

Scope
-----
This module is the read half only. It touches no geometry and creates no
document objects, which is what makes it testable under plain pytest with
stand-in objects -- the classification, the property capture and the ordering
are all pure logic over shapes the caller supplies.
"""
import FreeCAD

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
