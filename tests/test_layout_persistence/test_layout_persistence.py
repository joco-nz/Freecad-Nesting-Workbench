"""Every property the layout and master reload reads must be one that gets written.

NEST-002 was a property read on reload and never written, so a per-part rotation
override produced a correct nest and a layout that silently reverted on reopen.
`PartRotationOverride`, read on the line above it, had the identical defect and
was not in the issue. Neither raised, neither warned, and no test failed -- the
shape table's "Override" column looked wired and was not.

That is a class, not an incident, and it has a shape that can be checked without a
GUI: a `getattr(obj, "Name", default)` in a reload path whose `"Name"` is absent
from the corresponding `addProperty` calls. This file pins the property names on
both sides for the two reload paths that have one:

  * the master container -- `NestingController._load_shapes_from_layout` reads
    five names off it, `shape_preparer._create_master_container` writes them;
  * the layout group -- `NestingController._load_params_from_layout` reads them
    off a layout, `nesting_job.NestingJob._apply_properties` writes them.

Source inspection rather than a live round trip, deliberately. The round trip is
the *behavioural* check and it needs a real panel, which cannot be built under
freecadcmd (`Gui::QuantitySpinBox` is unavailable there) -- so it lives in
`tests/freecad_harness/test_layout_persistence.py` and in a GUI probe. This tier
is the one that runs on every commit in twelve seconds, so it is where the general
guard belongs.

The comparison is one-directional on purpose. A property written and never read is
harmless; a property read and never written is NEST-002.

These run under plain CPython: the modules are read as text, so nothing is
imported and no FreeCAD stub is involved.
"""
import ast
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))

_CONTROLLER = os.path.join(_REPO, "freecad", "nestingworkbench", "Tools",
                           "Nesting", "nesting_controller.py")
_PREPARER = os.path.join(_REPO, "freecad", "nestingworkbench", "Tools",
                         "Nesting", "shape_preparer.py")
_JOB = os.path.join(_REPO, "freecad", "nestingworkbench", "Tools",
                    "Nesting", "nesting_job.py")
_CONSTANTS = os.path.join(_REPO, "freecad", "nestingworkbench", "constants.py")


def _constant_map():
    """`constants.py`'s `NAME = "value"` assignments, as a dict.

    **This is load-bearing, and the first version of this file omitted it.**
    `NestingJob._apply_properties` writes property names as *constants* --
    `self._set_prop(target_layout, PROP_LENGTH, PROP_SHEET_WIDTH, ...)` -- and
    `nesting_controller` reads some of them the same way. A helper that only
    recognises string literals therefore matches **nothing** in the write half,
    so `_set_prop_names` returned an empty set, every written property looked
    unwritten, and the layout guard passed for the wrong reason: it was comparing
    one name against an empty set and only `Algorithm` -- the single literal in
    the read half -- ever reached the assertion.

    A guard that is quietly vacuous is worse than no guard, because it reads like
    evidence. So the resolver is here, and `test_the_constant_map_is_not_empty`
    fails loudly if `constants.py` ever stops parsing the way this expects.
    """
    tree = ast.parse(open(_CONSTANTS).read(), filename=_CONSTANTS)
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        value = node.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            out[node.targets[0].id] = value.value
    return out


_CONSTANTS_BY_NAME = _constant_map()


def _string_arg(node, constants):
    """The string a call argument denotes: a literal, or a known constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _function(path, name):
    """Return the AST node of one top-level (or method) function."""
    tree = ast.parse(open(path).read(), filename=path)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == name:
            return node
    raise AssertionError("%s has no function %r" % (path, name))


def _getattr_names(node, constants=None):
    """The names read by `getattr(x, "Name", ...)` inside `node`."""
    constants = _CONSTANTS_BY_NAME if constants is None else constants
    names = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not (isinstance(func, ast.Name) and func.id == "getattr"):
            continue
        if len(child.args) >= 2:
            name = _string_arg(child.args[1], constants)
            if name:
                names.add(name)
    return names


def _addproperty_names(node, constants=None):
    """The names created by `obj.addProperty(type, "Name", ...)`."""
    constants = _CONSTANTS_BY_NAME if constants is None else constants
    names = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if not isinstance(func, ast.Attribute) or func.attr != "addProperty":
            continue
        if len(child.args) >= 2:
            name = _string_arg(child.args[1], constants)
            if name:
                names.add(name)
    return names


def _set_prop_names(node, constants=None):
    """The names written by `self._set_prop(obj, type, NAME, value)`."""
    constants = _CONSTANTS_BY_NAME if constants is None else constants
    names = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        attr = getattr(func, "attr", getattr(func, "id", ""))
        if attr != "_set_prop":
            continue
        if len(child.args) >= 3:
            name = _string_arg(child.args[2], constants)
            if name:
                names.add(name)
    return names


def _dict_key_names(node, constants=None):
    """The string keys of every dict literal inside `node`, constants resolved.

    `props_map` in `_load_params_from_layout` is keyed by module constants, so
    its keys are only visible once those are resolved -- which is how
    `NestingDirection` and the rest reach the assertion at all.
    """
    constants = _CONSTANTS_BY_NAME if constants is None else constants
    names = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Dict):
            continue
        for key in child.keys:
            if key is None:
                continue
            name = _string_arg(key, constants)
            if name:
                names.add(name)
    return names


def test_the_constant_map_is_not_empty():
    """The resolver above is what makes this file able to see the write half.

    If `constants.py` stops being a flat list of `NAME = "value"` assignments --
    a class body, a computed value, a rename -- every helper here silently
    returns fewer names and the guards above narrow their own scope. That is the
    failure mode this file exists to prevent, so it gets its own check.
    """
    assert len(_CONSTANTS_BY_NAME) > 20, (
        "constants.py yielded only %d name(s): %s. The helpers above resolve "
        "property names through this map, so a parser change here would make "
        "every guard in this file vacuous."
        % (len(_CONSTANTS_BY_NAME), sorted(_CONSTANTS_BY_NAME)))
    for expected in ("PROP_SHEET_WIDTH", "PROP_NESTING_DIRECTION",
                     "PROP_RANDOM_DIRECTION"):
        assert expected in _CONSTANTS_BY_NAME, (
            "%s is missing from constants.py's resolved names; this file's "
            "guards read and write through it." % expected)


#: Read on reload by `_load_shapes_from_layout`, written by
#: `_create_master_container`. The intersection is empty today and must not be.
MASTER_PROPERTIES = ("Quantity", "PartRotationOverride", "PartRotationSteps",
                     "UpDirection", "FillSheet")

#: Read on reload by `_load_params_from_layout`. Not all of these are read by
#: `getattr` -- some are the *keys* of the `props_map` dict that drives one -- so
#: both spellings are collected and unioned.
LAYOUT_PROPERTIES = ("SheetWidth", "SheetHeight", "PartSpacing", "SheetThickness",
                     "Simplification", "LabelSize", "Generations", "PopulationSize",
                     "NestingDirection", "Algorithm", "DeflectionAngle",
                     "GlobalRotationSteps", "FontFile", "RandomDirection")


def test_master_container_reload_properties_are_all_written():
    read = _getattr_names(_function(_CONTROLLER, "_load_shapes_from_layout"))
    written = _addproperty_names(_function(_PREPARER, "_create_master_container"))

    missing = {name for name in MASTER_PROPERTIES
               if name in read and name not in written}
    assert not missing, (
        "_load_shapes_from_layout reads %s off a master container, but "
        "_create_master_container never creates %s. A property read and never "
        "written reloads as its default: the control looks wired and is not. "
        "(NEST-002 was PartRotationSteps and PartRotationOverride.)"
        % (sorted(missing), sorted(missing)))


def test_every_master_property_read_on_reload_is_covered_by_this_test():
    """If the reload path grows a property, this file must grow with it.

    Otherwise the guard above silently stops covering the new one -- a check that
    quietly narrows its own scope is how a class of defect gets a second instance
    after being fixed once.
    """
    read = _getattr_names(_function(_CONTROLLER, "_load_shapes_from_layout"))
    tracked = set(MASTER_PROPERTIES)
    # These two are read off the *shape* object rather than the container.
    untracked = read - tracked - {"Group", "Shape", "Label"}
    assert not untracked, (
        "_load_shapes_from_layout reads %s off a master container, which "
        "MASTER_PROPERTIES does not list. Add it, and add the matching "
        "addProperty to _create_master_container if it should survive a reload."
        % sorted(untracked))


def test_layout_group_reload_properties_are_all_written():
    node = _function(_CONTROLLER, "_load_params_from_layout")
    read = _getattr_names(node) | _dict_key_names(node)
    written = _set_prop_names(_function(_JOB, "_apply_properties"))

    missing = {name for name in LAYOUT_PROPERTIES
               if name in read and name not in written}
    assert not missing, (
        "_load_params_from_layout restores %s from a layout group, but "
        "NestingJob._apply_properties never writes %s. Reopening such a layout "
        "leaves the control at whatever the session happens to hold, silently."
        % (sorted(missing), sorted(missing)))


@pytest.mark.parametrize("prop", ["Algorithm", "RandomDirection"])
def test_layout_records_the_properties_the_direction_needs(prop):
    """The algorithm and the random flag are what make a direction recoverable.

    `NestingDirection` on its own is ambiguous: the same integer means a
    different search direction under each algorithm, because each algorithm has
    its own dial. So recording the dial without recording which dial it was is
    the NEST-001 defect, and both of these are what disambiguate it.

    Pinned separately from the round trip because they are the two names the
    branch-on-algorithm fix adds, and a future edit that removes one should fail
    here with a sentence about directions rather than in a GUI probe.
    """
    written = _set_prop_names(_function(_JOB, "_apply_properties"))
    assert prop in written, (
        "NestingJob._apply_properties does not record %r on the layout, so a "
        "reopened layout cannot tell which dial NestingDirection came from." % prop)
