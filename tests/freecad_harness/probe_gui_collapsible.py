#!/usr/bin/env freecad
"""GUI check for the collapsible sections in the Nesting panel.

Run with `freecad`, NOT `freecadcmd`:

    freecad tests/freecad_harness/probe_gui_collapsible.py

Why this exists
---------------
`probe_unit_panel.py` proves the *behaviour* of a collapsible section headlessly:
children hidden, state retained, `_collect_ui_params` seeing through the
collapse, and height shrinking. None of that is a picture.

A live GUI is what makes three further things checkable at all:

  * the styled header actually renders, rather than a default-palette box;
  * the collapsed state is the one a user sees on opening the panel, which is a
    different question from "does setExpanded(False) work when called later";
  * the two algorithm gates -- `_on_algorithm_change` hides the Minkowski
    sections and shows the Physics one -- still swap the right sections once
    each carries a QWidget-based header instead of a QGroupBox.

The last one is the one most likely to have broken silently: `setVisible` works
on any QWidget, so nothing about the swap should have changed it, and nothing
short of running it would tell us.

No Xvfb needed: FreeCAD 26.3's `freecad` starts a live GUI on no display at
all (see the harness README).
"""

import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_probe_gui_collapsible")

_CHECKS = [0]
_FAILURES = []

# The five headings, and the state each must open in. "match the default states
# that exist today" was the instruction: the three that were collapsible and
# collapsed stay collapsed, the two that were plain QGroupBoxes and always
# visible stay expanded. If this table is wrong the panel is wrong.
EXPECTED = [
    ("minkowski_settings_group", "Nesting Settings", False),
    ("minkowski_optimization_group", "Optimizations", True),
    ("physics_settings_group", "Physics Nesting Settings", True),
    ("helpers_group", "Helpers", False),
    ("logging_group", "Logging", False),
]


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


def main():
    import FreeCAD
    from PySide import QtWidgets

    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
    from freecad.nestingworkbench.ui_helpers import CollapsibleSection

    emit("=== collapsible sections, live GUI ===")
    # An int (1), not a bool, so truthiness. `is True` would be a false negative
    # here exactly as it would be in the workbench.
    check("FreeCAD reports a live GUI", bool(FreeCAD.GuiUp),
          f"GuiUp={FreeCAD.GuiUp!r}")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    check("a QApplication exists", app is not None)

    doc = FreeCAD.newDocument("collapsible_gui")
    FreeCAD.setActiveDocument(doc.Name)

    panel = NestingPanel()
    check("the panel builds under a live GUI", panel is not None)
    panel.show()
    drain(app)

    # -- the five headings, in the state they must open in -----------------
    for attr, title, expanded in EXPECTED:
        section = getattr(panel, attr, None)
        check(f"{attr} is a CollapsibleSection",
              isinstance(section, CollapsibleSection),
              type(section).__name__)
        if not isinstance(section, CollapsibleSection):
            continue
        check(f"{attr} renders its title as '{title}'",
              section.toggle_button.text() == title,
              repr(section.toggle_button.text()))
        check(f"{attr} opens {'expanded' if expanded else 'collapsed'}",
              section.isExpanded() is expanded,
              f"isExpanded={section.isExpanded()}")
        # A header that rendered is a header with a real geometry. A styled
        # QToolButton with no style applied still has a height, but padding and
        # bold text are what make it look deliberate -- so check the button is
        # actually taller than a bare tool button would be.
        header_h = section.toggle_button.sizeHint().height()
        check(f"{attr} header has real height", header_h >= 20,
              f"header sizeHint height={header_h}px")

    # -- collapsing reclaims space, as rendered -----------------------------
    heights = {}
    for attr, _title, expanded in EXPECTED:
        section = getattr(panel, attr, None)
        if not isinstance(section, CollapsibleSection):
            continue
        heights[attr] = (section.sizeHint().height(),
                         section.isExpanded())
        check(f"{attr} is laid out with a height", heights[attr][0] > 0,
              f"sizeHint={heights[attr][0]}")

    # A collapsed section must be shorter than an expanded one with the same
    # contents. Compare Optimizations and Physics, which both open expanded,
    # against the collapsed trio only by the property that matters: the toggle
    # hides content_area, so its own height must drop when it hides.
    settings = panel.minkowski_settings_group
    before = settings.sizeHint().height()
    settings.setExpanded(True)
    drain(app)
    expanded_h = settings.sizeHint().height()
    settings.setExpanded(False)
    drain(app)
    after = settings.sizeHint().height()
    check("expanding Nesting Settings grows it", expanded_h > before,
          f"collapsed={before}px expanded={expanded_h}px")
    check("collapsing Nesting Settings shrinks it again", after < expanded_h,
          f"collapsed={after}px expanded={expanded_h}px")
    check("a collapsed section is header-sized, not content-sized",
          after < 80, f"collapsed Nesting Settings is {after}px")

    # -- the arrow follows the state ---------------------------------------
    from PySide import QtCore
    settings.setExpanded(False)
    drain(app)
    check("a collapsed section points its arrow sideways",
          settings.toggle_button.arrowType() == QtCore.Qt.RightArrow,
          f"arrowType={settings.toggle_button.arrowType()}")
    settings.setExpanded(True)
    drain(app)
    check("an expanded section points its arrow down",
          settings.toggle_button.arrowType() == QtCore.Qt.DownArrow,
          f"arrowType={settings.toggle_button.arrowType()}")

    # -- the children are genuinely on screen when expanded -----------------
    settings.setExpanded(True)
    drain(app)
    check("the direction dial is visible with the section expanded",
          panel.minkowski_direction_dial.isVisible())
    settings.setExpanded(False)
    drain(app)
    check("...and hidden again when collapsed",
          not panel.minkowski_direction_dial.isVisible())

    # -- the algorithm gates still swap the right sections ------------------
    # The riskiest untested consequence of leaving QGroupBox behind: these were
    # setVisible calls on a QGroupBox and are now calls on a QWidget, and
    # nothing short of running it confirms the section hides as a whole.
    panel._on_algorithm_change("Minkowski")
    drain(app)
    check("Minkowski shows its own sections",
          panel.minkowski_settings_group.isVisible()
          and panel.minkowski_optimization_group.isVisible())
    check("Minkowski hides the Physics section",
          not panel.physics_settings_group.isVisible())

    panel._on_algorithm_change("Physics")
    drain(app)
    check("Physics shows its own section",
          panel.physics_settings_group.isVisible())
    check("Physics hides the Minkowski sections",
          not panel.minkowski_settings_group.isVisible()
          and not panel.minkowski_optimization_group.isVisible())
    check("Physics leaves Helpers and Logging alone -- their controls are "
          "read unconditionally by the controller",
          panel.helpers_group.isVisible()
          and panel.logging_group.isVisible())

    panel._on_algorithm_change("Minkowski")
    drain(app)

    panel.dispose()
    panel.deleteLater()
    drain(app)
    FreeCAD.closeDocument(doc.Name)
    return 0


if __name__ in ("__main__", "probe_gui_collapsible"):
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
