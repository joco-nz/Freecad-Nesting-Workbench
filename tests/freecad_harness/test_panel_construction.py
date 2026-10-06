"""Does the Nesting panel actually build?

Written because it did not. `ui_nesting.py:811` called `setToolTip` on a
`LengthField` rather than on the widget inside it, which raised during
`_build_length_fields` and stopped `NestingPanel()` being constructed at all --
so the workbench's main entry point failed on every click, and a one-line typo
shipped in a commit whose gate was green.

**The gate could not have caught it, and that is the reason this file exists.**
Nothing gated constructs the panel:

* `test_panel_teardown.py` mentions `NestingPanel` but allocates it with
  `NestingPanel.__new__(NestingPanel)`, deliberately skipping `__init__`
  because it tests `reject()`/`dispose()` and does not need a built panel.
* `test_document_units.py` does not touch it either.
* `freecadcmd` **cannot** build the panel at all: `LengthField` constructs
  `Gui::QuantitySpinBox` through `FreeCADGui.UiLoader()`, and `freecadcmd` has
  no `UiLoader`. Measured:
  `AttributeError: module 'FreeCADGui' has no attribute 'UiLoader'`.

So this runs on the **`freecad` GUI binary** instead, which starts with a live
GUI on no display at all -- no Xvfb needed. It reproduces the original failure
exactly: with the bug present this reports
`AttributeError: 'LengthField' object has no attribute 'setToolTip'`.

Two things asserted beyond "it does not raise":

* **The tooltips are on real widgets.** The Candidate Step diagram and the Part
  Spacing tool-clearance note are both set through `.widget()`, and this reads
  the tooltip back off the widget the panel actually owns -- so a wrapper used
  by mistake cannot pass by leaving the string somewhere unreachable.
* **`LengthField` has no `setToolTip`.** Stated outright, because that is the
  trap: `LengthField` is a wrapper exposing `.widget()`, and the plain spin boxes
  from `make_double_spinbox` *do* take `setToolTip` directly, so the correct
  call differs between two adjacent kinds of field and nothing in the type says
  so.

Run directly, or via tests/freecad_harness/run.sh, which uses the GUI binary for
this one. Writes `.last_status_panelbuild`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_panelbuild")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import FreeCAD
from PySide import QtWidgets

from freecad.nestingworkbench.length_field import LengthField
from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

_failures = []
_checks = [0]


def emit(message=""):
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
        else:
            print(message)
    except Exception:
        pass


def check(condition, detail):
    _checks[0] += 1
    if not condition:
        _failures.append(detail)
        emit("  FAIL: %s" % detail)
    return bool(condition)


def check_it_builds():
    emit("")
    emit("-- 1. the panel constructs --")
    form = NestingPanel()
    check(form is not None, "NestingPanel() returned None")
    emit("  built")
    return form


def check_the_wrapper_trap_is_stated():
    """The trap itself, asserted rather than assumed."""
    emit("")
    emit("-- 2. LengthField is a wrapper, and that is the trap --")
    check(not hasattr(LengthField, "setToolTip"),
          "LengthField has gained a setToolTip; if tooltips are now settable on "
          "the wrapper, re-check which object every tooltip call site means")
    check(hasattr(LengthField, "widget"),
          "LengthField no longer exposes .widget()")
    emit("  LengthField.setToolTip present: %s"
         % hasattr(LengthField, "setToolTip"))
    emit("  LengthField.widget present   : %s" % hasattr(LengthField, "widget"))


def check_the_tooltips_landed(form):
    emit("")
    emit("-- 3. the tooltips are on the widgets the panel owns --")
    spacing = form.part_spacing_input.widget()
    tip = spacing.toolTip()
    emit("  Part Spacing tooltip: %d chars" % len(tip))
    check("no knowledge of your tool" in tip,
          "the Part Spacing tooltip is not on the widget; got %r" % tip[:80])
    check("outlines" in tip,
          "the Part Spacing tooltip lost its explanation of what it measures")

    step = form.minkowski_step_size_input.widget()
    step_tip = step.toolTip()
    emit("  Candidate Step tooltip: %d chars" % len(step_tip))
    check("<img src=" in step_tip,
          "the Candidate Step tooltip has no diagram on the widget")
    check("<table width=" in step_tip,
          "the Candidate Step tooltip lost its pinned popup width")
    check("Below: the same part" in step_tip,
          "the Candidate Step tooltip lost its caption for the diagram")

    # Every LengthField the panel owns should be reachable as a widget, because
    # that is the only correct route to any of its properties.
    wrappers = [name for name in dir(form)
                if name.endswith("_input") and hasattr(getattr(form, name, None), "widget")]
    emit("  %d LengthField-wrapping inputs on the panel" % len(wrappers))
    for name in wrappers:
        field = getattr(form, name)
        try:
            field.widget().toolTip()
        except Exception as exc:
            check(False, "%s has no usable widget(): %s" % (name, exc))


def check_dispose_is_clean(form):
    emit("")
    emit("-- 4. it disposes --")
    try:
        form.dispose()
        check(True, "")
    except Exception as exc:
        check(False, "dispose() raised %s: %s" % (type(exc).__name__, exc))


def run():
    check_the_wrapper_trap_is_stated()
    form = check_it_builds()
    if form is not None:
        try:
            check_the_tooltips_landed(form)
        finally:
            check_dispose_is_clean(form)


try:
    run()
    emit("")
    emit("panel construction: %d checks, %d failure(s)"
         % (_checks[0], len(_failures)))
    status = 1 if _failures else 0
except Exception:
    traceback.print_exc()
    emit("panel construction: CRASHED")
    status = 1

with open(_STATUS_FILE, "w") as fh:
    fh.write(str(status))

try:
    import FreeCADGui
    FreeCADGui.getMainWindow().close()
except Exception:
    pass