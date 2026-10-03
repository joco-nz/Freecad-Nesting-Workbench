#!/usr/bin/env freecad
"""Positional check for the Nesting panel: is every control in the right box?

Run with `freecad`, NOT `freecadcmd`:

    freecad tests/freecad_harness/probe_gui_layout_positions.py

Why this exists
---------------
Every other panel test asserts on *state* -- a checkbox reads back as checked,
a LengthField holds millimetres, a collapsed section is collapsed. None of them
assert on *position*. So a widget parented into the wrong container passes all
of them, and that is the failure mode left over now that field creation has
been extracted into builders: extracting code moves widgets onto `self`, but
nothing checks that the layout assembly still puts them where they were.

Two properties are checked.

**Containment.** Every tracked control is located by walking its parents up to
the enclosing CollapsibleSection, and that section's title is asserted. Panel
level controls must resolve to no section at all. This is what catches a
control that drifted into a neighbouring section -- a plausible mistake when
adjacent blocks are cut and moved.

**Parentage.** Every tracked control must have been parented into the tree at
all -- `parentWidget()` non-None. A widget created but never added to a layout
keeps a null parent, so it exists on `self`, passes every containment check
above, and is simply not on screen.

An earlier version of this probe counted layouts per widget and asserted at
most one, on the theory that a double-add would show. It cannot work, for two
reasons. `QLayout.indexOf()` recurses into nested layouts, so a control in
`form_layout` is also found by the `main_layout` that nests it -- three false
failures on the first run. And Qt removes a widget from its previous layout
when it is added to a second one, so the count could never exceed one however
the code misbehaved. A check that cannot fail is not a check; the observable
consequence of a double-add is that the first layout silently loses a widget,
which shows up as a containment failure rather than as a count.

A note on what this cannot see: it cannot tell you a widget is in the right
section but the wrong cell, nor that two rows are transposed. `_two_column_grid`
order is asserted separately in probe_unit_panel. Together they cover position;
neither covers whether the result looks right, which is what a human is for.
"""

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_probe_gui_layout")

_CHECKS = [0]
_FAILURES = []

NESTING_SETTINGS = "Nesting Settings"
OPTIMIZATIONS = "Optimizations"
PHYSICS = "Physics Nesting Settings"
HELPERS = "Helpers"
LOGGING = "Logging"

# attribute -> the section it must live in. None means panel level, outside
# every section. LengthField entries name the wrapper; the probe resolves .widget()
# because the wrapper is not a QWidget and has no place in the tree.
EXPECTED = {
    # panel level
    "algorithm_dropdown": None,
    "sheet_width_input": None,
    "sheet_height_input": None,
    "sheet_thickness_input": None,
    "part_spacing_input": None,
    "deflection_input": None,
    "simplification_input": None,
    "shape_table": None,
    "progressBar": None,
    "status_label": None,
    "nest_button": None,
    "cancel_button": None,
    "add_parts_button": None,
    "remove_parts_button": None,
    # Nesting Settings -- the direction dial and its checkbox
    "minkowski_direction_dial": NESTING_SETTINGS,
    "minkowski_random_checkbox": NESTING_SETTINGS,
    "minkowski_rotation_steps_slider": NESTING_SETTINGS,
    # Optimizations -- the GA dials and the perf dials
    "minkowski_generations_input": OPTIMIZATIONS,
    "minkowski_population_size_input": OPTIMIZATIONS,
    "minkowski_target_sheets_input": OPTIMIZATIONS,
    "minkowski_step_size_input": OPTIMIZATIONS,
    "minkowski_rotation_workers_input": OPTIMIZATIONS,
    "candidate_geometry_cache_checkbox": OPTIMIZATIONS,
    "minkowski_compactness_input": OPTIMIZATIONS,
    "clear_cache_checkbox": OPTIMIZATIONS,
    # Physics
    "physics_direction_dial": PHYSICS,
    "physics_random_checkbox": PHYSICS,
    "physics_step_size_input": PHYSICS,
    "physics_max_spawn_input": PHYSICS,
    "physics_max_nesting_steps_input": PHYSICS,
    "anneal_rotate_checkbox": PHYSICS,
    "anneal_translate_checkbox": PHYSICS,
    "anneal_random_shake_checkbox": PHYSICS,
    "physics_anneal_curve_type": PHYSICS,
    "physics_anneal_rot_min": PHYSICS,
    "physics_anneal_min_amp": PHYSICS,
    # Helpers
    "font_select_button": HELPERS,
    "font_label": HELPERS,
    "add_labels_checkbox": HELPERS,
    "label_size_input": HELPERS,
    "label_height_input": HELPERS,
    "simulate_nesting_checkbox": HELPERS,
    "show_bounds_checkbox": HELPERS,
    "sound_checkbox": HELPERS,
    # Logging
    "verbose_logging_checkbox": LOGGING,
    "performance_logging_checkbox": LOGGING,
}


def emit(message=""):
    try:
        import FreeCAD

        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


def check(label, condition, detail=""):
    _CHECKS[0] += 1
    if condition:
        emit(f"  PASS  {label}")
    else:
        emit(f"  FAIL  {label}  {detail}")
        _FAILURES.append(label)


def drain(app, iterations=400):
    for _ in range(iterations):
        app.processEvents()


def resolve(value):
    """LengthField exposes a non-QWidget wrapper; use its spin box instead."""
    return value.widget() if hasattr(value, "widget") else value


def enclosing_section(panel, widget):
    """The CollapsibleSection a widget sits inside, or None for panel level."""
    from freecad.nestingworkbench.ui_helpers import CollapsibleSection

    node = widget.parentWidget()
    while node is not None and node is not panel:
        if isinstance(node, CollapsibleSection):
            return node
        node = node.parentWidget()
    return None


def main():
    import FreeCAD
    from PySide import QtWidgets

    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

    emit("=== panel layout positions ===")
    check("FreeCAD reports a live GUI", bool(FreeCAD.GuiUp),
          f"GuiUp={FreeCAD.GuiUp!r}")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    doc = FreeCAD.newDocument("positions")
    FreeCAD.setActiveDocument(doc.Name)

    panel = NestingPanel()
    panel.show()
    drain(app)

    # Gather every layout in the panel once, for the double-add check.
    layouts = [panel.layout()] + list(panel.findChildren(QtWidgets.QLayout))
    layouts = [layout for layout in layouts if layout is not None]

    by_section = {}
    for attr, expected in sorted(EXPECTED.items()):
        value = getattr(panel, attr, None)
        if value is None:
            check(f"{attr} exists on the panel", False, "attribute missing")
            continue
        widget = resolve(value)
        if not isinstance(widget, QtWidgets.QWidget):
            check(f"{attr} resolves to a widget", False,
                  f"got {type(widget).__name__}")
            continue

        section = enclosing_section(panel, widget)
        found = section.toggle_button.text() if section is not None else None
        by_section.setdefault(found, []).append(attr)
        check(f"{attr} is in {expected or 'the panel, outside every section'}",
              found == expected, f"found it in {found or 'no section'}")

        # Never added to a layout: exists on self, invisible on screen.
        check(f"{attr} is parented into the panel",
              widget.parentWidget() is not None,
              "no parent -- created but never added to a layout")

    emit("")
    emit("  --- distribution ---")
    for title in (NESTING_SETTINGS, OPTIMIZATIONS, PHYSICS, HELPERS, LOGGING):
        emit(f"    {title:<26} {len(by_section.get(title, [])):>2} controls")
    emit(f"    {'(panel level)':<26} {len(by_section.get(None, [])):>2} controls")

    # Every section must actually be in the panel's own layout. A section built
    # but never added would satisfy every containment check above, because a
    # widget inside it would still resolve to the right section.
    main_layout = panel.layout()
    sections = [getattr(panel, name) for name in
                ("minkowski_settings_group", "minkowski_optimization_group",
                 "physics_settings_group", "helpers_group", "logging_group")]
    for section in sections:
        title = section.toggle_button.text()
        placed = any(layout.indexOf(section) >= 0 for layout in layouts)
        check(f"the {title} section is in the panel's layout", placed,
              "built but never added")
    check("the panel has a main layout", main_layout is not None)

    panel.dispose()
    panel.deleteLater()
    drain(app)
    FreeCAD.closeDocument(doc.Name)
    return 0


if __name__ in ("__main__", "probe_gui_layout_positions"):
    try:
        _status = main()
    except Exception:
        traceback.print_exc()
        _status = 3
    emit("")
    emit(f"{_CHECKS[0] - len(_FAILURES)}/{_CHECKS[0]} checks passed")
    if _FAILURES:
        emit("FAILED: " + ", ".join(_FAILURES))
    try:
        with open(_STATUS_FILE, "w") as _handle:
            _handle.write(str(_status))
    except OSError:
        pass
    sys.exit(_status)
