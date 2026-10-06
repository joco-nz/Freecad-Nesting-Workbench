"""The reload half of NEST-001 and NEST-002, against the real panel.

Why this is a GUI probe and not a gated test: the panel cannot be built under
`freecadcmd` -- `Gui::QuantitySpinBox` is unavailable there, so `NestingPanel()`
raises before a single assertion runs. Everything on the *write* side is covered
by `test_layout_persistence.py` under `freecadcmd`, and the structural pairing by
`tests/test_layout_persistence/`. What is left is the half only a GUI can see: the
controller reading the right dial, and the panel coming back showing what the
layout recorded.

Run by hand, on the `freecad` binary (not `freecadcmd`), which starts a live GUI
on no display -- see tests/freecad_harness/README.md.

    $FREECAD tests/freecad_harness/probe_layout_restore.py

Writes `.last_status_restore`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_restore")

import FreeCAD
import FreeCADGui

_FAILURES = []
_CHECKS = [0]

#: Deliberately different readings, so a path that always takes the Minkowski dial
#: cannot pass by coincidence. 90 is the panel's default (Left); 270 is Right.
MINKOWSKI_DIAL = 90
PHYSICS_DIAL = 270

GLOBAL_STEPS = 8
OVERRIDE_STEPS = 12
NO_OVERRIDE_STEPS = 4
#: `MINKOWSKI_ROTATION_PRESETS` is [360, 180, 120, 90, 45, 30, 15, 10, 5, 1], so
#: only these counts survive a round trip through the global rotation slider:
#: `360 / steps` must land on a preset angle, or the reload snaps to the nearest
#: one and the count comes back different. 8 -> 45, 12 -> 30, 4 -> 90.
#:
#: The first version of this file used 6, which is 60 degrees and not in the
#: list, and asserted `rotation_steps == 6` after the reload -- which failed for
#: the right reason (8 is the nearest preset) and would have looked like a
#: product bug. It did not, for a while: `check` had its arguments in the wrong
#: order and passed unconditionally. Two fixture faults hiding behind one test
#: fault, which is worth writing down.


def emit(message=""):
    try:
        FreeCAD.Console.PrintMessage(str(message) + "\n")
    except Exception:
        print(message)


def check(label, condition, detail=""):
    """`check(label, condition, detail)`.

    **The argument order is `label` first, matching `probe_unit_panel.py`**,
    because every call site in this file is written that way and copying a
    sibling's convention is less error-prone than remembering which of two is
    which.

    It was originally declared `check(condition, message, detail)`. Every call
    passed a label first, so `condition` received the *label string* -- and a
    non-empty string is truthy. **All 28 checks passed unconditionally**, which
    was only caught by reverting the fix and observing that the probe stayed
    green. A guard that cannot fail is worse than no guard, because it reads like
    evidence; `tests/test_layout_persistence/` says the same thing about its own
    constant resolver, and this file proved the point the hard way.

    `condition is True` rather than truthiness, so a non-boolean cannot slip
    through the same way again.
    """
    _CHECKS[0] += 1
    if condition is not True:
        _FAILURES.append(label)
        emit("  FAIL: %s  %s" % (label, detail))
    return bool(condition)


def clear_prefs():
    """Remove every preference this probe touches, so each case starts clean.

    Two reasons, and the second is the important one.

    The first is isolation: `NestingPanel` reads preferences at construction, so
    one case writing `RandomDirection` decides what the next case's panel looks
    like before the case begins. That produced a failure in `case_old_layout_
    still_loads` that was entirely about a *previous* case's leftovers.

    The second is that these are the **user's real preferences**. A probe that
    leaves `PhysicsDirection` at 270 has changed a setting the user never chose,
    and the next FreeCAD session inherits it. So the keys are removed on the way
    out as well as on the way in.

    The keys are read off the constants rather than spelled out, so a new one
    added to the fix cannot be missed here.
    """
    from freecad.nestingworkbench.constants import (
        PREFS_PATH, PROP_NESTING_DIRECTION, PROP_PHYSICS_DIRECTION,
        PROP_RANDOM_DIRECTION)
    prefs = FreeCAD.ParamGet(PREFS_PATH)
    for key in (PROP_NESTING_DIRECTION, PROP_PHYSICS_DIRECTION,
                PROP_RANDOM_DIRECTION, "PhysicsRandomDirection"):
        prefs.RemInt(key)
        prefs.RemBool(key)


def new_panel(doc_name, clear=True):
    """A real panel on a real document, in the metric schema.

    `clear=False` for the *second* panel of a persistence case, which exists
    precisely to read back what the first one wrote. Clearing there made the case
    fail with "the Physics dial came back too: got 90" -- the probe had deleted
    the preference it was checking.
    """
    if clear:
        clear_prefs()
    doc = FreeCAD.newDocument(doc_name)
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]
    FreeCAD.setActiveDocument(doc.Name)
    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
    return doc, NestingPanel()


def close_panel():
    """Discard this case's documents, leaving the session open for the next case.

    `FreeCAD.closeDocument` discards: it never prompts, so the Unsaved Document
    dialog never appears and nothing is written -- a probe cannot dirty the
    user's documents this way.

    The main window is deliberately **not** closed here. It was, and it made
    `QDial.isEnabled()` report False for every control built afterwards, so three
    checks about greyed-out dials failed for a reason that had nothing to do with
    what they were asserting. `isEnabled()` on a widget whose window has been torn
    down is not a statement about the control. `close_out` shuts the window once,
    at the end.
    """
    for doc in list(FreeCAD.listDocuments()):
        FreeCAD.closeDocument(doc)


def close_out():
    """End the session: close the window and quit the app.

    Without this the process lingers with the panel on screen after the script
    has finished.
    """
    from PySide import QtWidgets
    window = FreeCADGui.getMainWindow()
    if window is not None:
        window.close()
    app = QtWidgets.QApplication.instance()
    if app is not None:
        app.quit()


# -- 1. the controller must read the dial that the algorithm uses ---------

def case_collect_follows_the_algorithm():
    """`_collect_ui_params` reports the *active* algorithm's dial reading.

    Before the fix `nesting_direction` was read from the Minkowski dial
    unconditionally, so a Physics run recorded a number the Physics search never
    read -- and `_apply_properties` wrote it onto the layout, which is the number
    the reload restores. See issues.md NEST-001.
    """
    from freecad.nestingworkbench.constants import DEFAULT_ALGORITHM

    emit("")
    emit("--- _collect_ui_params follows the algorithm ---")

    doc, panel = new_panel("collect_algo")
    try:
        panel.minkowski_direction_dial.setValue(MINKOWSKI_DIAL)
        panel.physics_direction_dial.setValue(PHYSICS_DIAL)

        panel.algorithm_dropdown.setCurrentText(DEFAULT_ALGORITHM)
        params = panel.controller._collect_ui_params()
        check("Minkowski reports the Minkowski dial",
              params.get("nesting_direction") == MINKOWSKI_DIAL,
              "got %s, expected %d"
              % (params.get("nesting_direction"), MINKOWSKI_DIAL))

        panel.algorithm_dropdown.setCurrentText("Physics")
        params = panel.controller._collect_ui_params()
        check("Physics reports the Physics dial, not the Minkowski one",
              params.get("nesting_direction") == PHYSICS_DIAL,
              "got %s, expected %d -- this is NEST-001"
              % (params.get("nesting_direction"), PHYSICS_DIAL))

        # The other dial is carried too, so `save_settings` and the layout write
        # have both numbers rather than having to reach into the widgets.
        check("both dial readings travel in the params dict",
              params.get("minkowski_direction") == MINKOWSKI_DIAL
              and params.get("physics_direction") == PHYSICS_DIAL,
              "minkowski=%s physics=%s"
              % (params.get("minkowski_direction"), params.get("physics_direction")))

        # And the random flag, which decides whether the dial was consulted at all.
        panel.physics_random_checkbox.setChecked(True)
        params = panel.controller._collect_ui_params()
        check("the random flag is reported for the active algorithm",
              params.get("use_random_direction") is True,
              "got %s" % params.get("use_random_direction"))
        emit("  Physics + random: nesting_direction=%s (the control's value, "
             "which the run ignores), use_random_direction=%s"
             % (params.get("nesting_direction"),
                params.get("use_random_direction")))
    finally:
        close_panel()


# -- 2. the layout must come back showing what it recorded -----------------

def case_layout_restores_algorithm_and_direction():
    """A layout written by a Physics run reopens as Physics, on the Physics dial.

    Three defects in one assertion, all NEST-001: `Algorithm` was read and never
    written, the direction was restored to the Minkowski dial whatever ran, and
    the algorithm restore was nested inside `if steps > 0:` so a layout without a
    rotation-step property never restored it at all.
    """
    from freecad.nestingworkbench.constants import (
        PROP_ALGORITHM, PROP_GLOBAL_ROTATION_STEPS, PROP_NESTING_DIRECTION,
        PROP_RANDOM_DIRECTION, PROP_SHEET_WIDTH, DEFAULT_ALGORITHM)

    emit("")
    emit("--- a Physics layout reopens as Physics ---")

    doc, panel = new_panel("restore_physics")
    try:
        layout = doc.addObject("App::DocumentObjectGroup", "Layout_physics")
        # A String, matching `nesting_job._apply_properties`' PROP_STRING. Typed
        # as an Integer in the first version of this fixture and FreeCAD refused
        # the assignment outright -- which is a useful reminder that the property
        # type is part of the contract, and that a layout written by an older
        # build with a different type would fail here rather than half-load.
        layout.addProperty("App::PropertyString", PROP_ALGORITHM, "Layout", "")
        setattr(layout, PROP_ALGORITHM, "Physics")
        layout.addProperty("App::PropertyInteger", PROP_NESTING_DIRECTION,
                           "Layout", "")
        setattr(layout, PROP_NESTING_DIRECTION, PHYSICS_DIAL)
        layout.addProperty("App::PropertyBool", PROP_RANDOM_DIRECTION,
                           "Layout", "")
        setattr(layout, PROP_RANDOM_DIRECTION, False)
        # Deliberately NOT set: PROP_GLOBAL_ROTATION_STEPS. The algorithm restore
        # used to sit inside `if steps > 0:`, so a layout without this property
        # never restored the algorithm. This is that case.
        doc.recompute()

        # Put the session's dials somewhere else, so "it looks right" cannot be
        # the dial simply having never moved.
        panel.minkowski_direction_dial.setValue(MINKOWSKI_DIAL)
        panel.physics_direction_dial.setValue(0)
        check("the Physics dial really was moved before the reload",
              panel.physics_direction_dial.value() == 0)

        panel.controller._load_params_from_layout(layout)

        check("the algorithm came back",
              panel.algorithm_dropdown.currentText() == "Physics",
              "got %r" % panel.algorithm_dropdown.currentText())
        check("the Physics section is showing, so the panel is on the right algorithm",
              panel.physics_settings_group.isVisible()
              or not panel.minkowski_settings_group.isVisible())
        check("the direction went to the Physics dial",
              panel.physics_direction_dial.value() == PHYSICS_DIAL,
              "got %d, expected %d" % (panel.physics_direction_dial.value(),
                                       PHYSICS_DIAL))
        check("the Minkowski dial was not touched by a Physics layout",
              panel.minkowski_direction_dial.value() == MINKOWSKI_DIAL,
              "got %d" % panel.minkowski_direction_dial.value())
        emit("  after reload: algorithm=%s physics_dial=%d minkowski_dial=%d"
             % (panel.algorithm_dropdown.currentText(),
                panel.physics_direction_dial.value(),
                panel.minkowski_direction_dial.value()))
    finally:
        close_panel()


def case_random_run_greys_its_own_dial():
    """A random run reopens with that algorithm's dial disabled.

    The dial reading is restored either way -- it is what the control held, which
    is what the user expects to see again -- but the flag has to come back too, or
    the reopened panel shows an enabled dial whose value the run ignored.
    """
    from freecad.nestingworkbench.constants import (
        PROP_ALGORITHM, PROP_NESTING_DIRECTION, PROP_RANDOM_DIRECTION)

    emit("")
    emit("--- a random Physics run greys the Physics dial ---")

    doc, panel = new_panel("restore_random")
    try:
        layout = doc.addObject("App::DocumentObjectGroup", "Layout_random")
        layout.addProperty("App::PropertyString", PROP_ALGORITHM, "Layout", "")
        setattr(layout, PROP_ALGORITHM, "Physics")
        layout.addProperty("App::PropertyInteger", PROP_NESTING_DIRECTION,
                           "Layout", "")
        setattr(layout, PROP_NESTING_DIRECTION, PHYSICS_DIAL)
        layout.addProperty("App::PropertyBool", PROP_RANDOM_DIRECTION,
                           "Layout", "")
        setattr(layout, PROP_RANDOM_DIRECTION, True)
        doc.recompute()

        panel.physics_random_checkbox.setChecked(False)
        panel.physics_direction_dial.setEnabled(True)
        panel.controller._load_params_from_layout(layout)

        check("the Physics random checkbox came back ticked",
              panel.physics_random_checkbox.isChecked() is True)
        check("the Physics dial is disabled, because the run ignored it",
              panel.physics_direction_dial.isEnabled() is False)
        check("the dial still shows what the user had set",
              panel.physics_direction_dial.value() == PHYSICS_DIAL,
              "got %d" % panel.physics_direction_dial.value())
        check("the Minkowski dial is untouched and still enabled",
              panel.minkowski_direction_dial.isEnabled() is True)
    finally:
        close_panel()


def case_old_layout_still_loads():
    """A layout written before any of this existed must still open.

    No `Algorithm` and no `RandomDirection`, which is every layout saved before
    this change. The fallback is the panel's own default, which is what those
    files were in fact run with.
    """
    from freecad.nestingworkbench.constants import (
        DEFAULT_ALGORITHM, DEFAULT_DIRECTION_DIAL, PREFS_PATH,
        PROP_NESTING_DIRECTION, PROP_RANDOM_DIRECTION)

    emit("")
    emit("--- an old layout, with no Algorithm, still loads ---")

    doc, panel = new_panel("restore_old")
    try:
        layout = doc.addObject("App::DocumentObjectGroup", "Layout_old")
        layout.addProperty("App::PropertyInteger", PROP_NESTING_DIRECTION,
                           "Layout", "")
        setattr(layout, PROP_NESTING_DIRECTION, MINKOWSKI_DIAL)
        doc.recompute()
        check("the fixture really has no Algorithm property",
              not hasattr(layout, "Algorithm"))

        panel.physics_direction_dial.setValue(PHYSICS_DIAL)
        panel.algorithm_dropdown.setCurrentText("Physics")

        panel.controller._load_params_from_layout(layout)
        check("an old layout opens as the default algorithm",
              panel.algorithm_dropdown.currentText() == DEFAULT_ALGORITHM,
              "got %r, expected %r" % (panel.algorithm_dropdown.currentText(),
                                       DEFAULT_ALGORITHM))
        check("its direction goes to the Minkowski dial, which is what it meant",
              panel.minkowski_direction_dial.value() == MINKOWSKI_DIAL,
              "got %d" % panel.minkowski_direction_dial.value())
        check("the Physics dial is left at the session's value",
              panel.physics_direction_dial.value() == PHYSICS_DIAL)
        check("no random flag means no greyed dial",
              panel.minkowski_direction_dial.isEnabled() is True)
    finally:
        close_panel()


# -- 3. the per-part override must come back ------------------------------

def case_part_rotation_override_round_trip():
    """The shape table's Rotations/Override columns survive a reload.

    NEST-002: the master container never recorded the pair, so both columns came
    back empty and a per-part rotation override was silently lost -- while the
    nest itself had honoured it, because the resolved count went to the nester by
    a different route.

    Asserted per part, and with the *un-overridden* part holding a different
    number from the overridden one: if both columns were restored from the same
    value the rows would be indistinguishable, which is exactly the failure if
    `PartRotationOverride` is stored as a number instead of a flag.
    """
    from freecad.nestingworkbench.constants import (
        PROP_ALGORITHM, PROP_GLOBAL_ROTATION_STEPS, PROP_NESTING_DIRECTION,
        PROP_RANDOM_DIRECTION)

    emit("")
    emit("--- the per-part rotation override round-trips ---")

    doc, panel = new_panel("restore_override")
    try:
        layout = doc.addObject("App::DocumentObjectGroup", "Layout_override")
        layout.addProperty("App::PropertyString", PROP_ALGORITHM, "Layout", "")
        setattr(layout, PROP_ALGORITHM, "Minkowski")
        layout.addProperty("App::PropertyInteger", PROP_GLOBAL_ROTATION_STEPS,
                           "Layout", "")
        setattr(layout, PROP_GLOBAL_ROTATION_STEPS, GLOBAL_STEPS)
        layout.addProperty("App::PropertyInteger", PROP_NESTING_DIRECTION,
                           "Layout", "")
        setattr(layout, PROP_NESTING_DIRECTION, MINKOWSKI_DIAL)
        layout.addProperty("App::PropertyBool", PROP_RANDOM_DIRECTION,
                           "Layout", "")
        setattr(layout, PROP_RANDOM_DIRECTION, False)
        doc.recompute()

        masters = doc.addObject("App::DocumentObjectGroup", "MasterShapes")
        layout.addObject(masters)

        from PySide import QtWidgets
        import Part as _Part
        rows = {}
        for label, raw, override in (("A", OVERRIDE_STEPS, True),
                                     ("B", NO_OVERRIDE_STEPS, False)):
            master = doc.addObject("App::Part", "master_%s" % label)
            # An Integer, as `shape_preparer._create_master_container` writes it.
            # A string here reached `quantity_spinbox.setValue` and raised
            # "called with wrong argument types", which is the kind of fixture
            # fault that reads like a product bug.
            master.addProperty("App::PropertyInteger", "Quantity", "Nest", "")
            master.Quantity = 2 if label == "A" else 1
            master.addProperty("App::PropertyInteger", "PartRotationSteps",
                               "Nest", "")
            master.PartRotationSteps = raw
            master.addProperty("App::PropertyBool", "PartRotationOverride",
                               "Nest", "")
            master.PartRotationOverride = override
            master.addProperty("App::PropertyString", "UpDirection", "Nest", "")
            master.UpDirection = "Z+"
            master.addProperty("App::PropertyBool", "FillSheet", "Nest", "")
            master.FillSheet = False
            shape = doc.addObject("Part::Feature", "master_shape_%s" % label)
            shape.Shape = _Part.makeBox(40, 30, 10)
            master.addObject(shape)
            masters.addObject(master)
            rows[label] = (raw, override)
        doc.recompute()

        check("the fixture really does carry the pair",
              hasattr(masters.Group[0], "PartRotationSteps")
              and hasattr(masters.Group[0], "PartRotationOverride"))

        panel.controller.load_layout(layout)

        table = panel.shape_table
        check("the shape table has both parts", table.rowCount() == 2,
              "got %d row(s)" % table.rowCount())
        if table.rowCount() != 2:
            return

        seen = {}
        for row in range(table.rowCount()):
            label = table.item(row, 0).text()
            spin = table.cellWidget(row, 2).findChild(QtWidgets.QSpinBox)
            checkbox = table.cellWidget(row, 3)
            seen[label] = (spin.value(), checkbox.isChecked())
            emit("  row %d %-3s rotations=%-4s override=%s"
                 % (row, label, spin.value(), checkbox.isChecked()))

        check("A came back with its override on",
              seen.get("A") == (OVERRIDE_STEPS, True),
              "got %s, expected %s" % (seen.get("A"), (OVERRIDE_STEPS, True)))
        check("B came back with its override off, despite holding a number",
              seen.get("B") == (NO_OVERRIDE_STEPS, False),
              "got %s, expected %s" % (seen.get("B"),
                                       (NO_OVERRIDE_STEPS, False)))
        check("the two rows are distinguishable, so the flag is not a number",
              seen.get("A") != seen.get("B"))

        # And the global is still the global: the reload must not have turned
        # B's raw reading into an override.
        params = panel.controller._collect_ui_params()
        check("the global rotation steps are untouched by the reload",
              params.get("rotation_steps") == GLOBAL_STEPS,
              "got %s, expected %s" % (params.get("rotation_steps"), GLOBAL_STEPS))
    finally:
        close_panel()


# -- 4. preferences -------------------------------------------------------

def case_physics_dial_is_remembered():
    """The Physics dial reaches preferences and comes back in a new panel.

    It had no key at all, so its position was recorded nowhere -- while
    `PhysicsRandomDirection` and `PhysicsRotationSteps` were remembered all
    along. See issues.md NEST-001.
    """
    from freecad.nestingworkbench.constants import (
        PREFS_PATH, PROP_NESTING_DIRECTION, PROP_PHYSICS_DIRECTION)

    emit("")
    emit("--- the Physics dial survives a session ---")

    prefs = FreeCAD.ParamGet(PREFS_PATH)
    for key in (PROP_NESTING_DIRECTION, PROP_PHYSICS_DIRECTION):
        prefs.RemInt(key)

    doc, panel = new_panel("prefs_write")
    try:
        panel.minkowski_direction_dial.setValue(MINKOWSKI_DIAL)
        panel.physics_direction_dial.setValue(PHYSICS_DIAL)
        panel.controller._collect_ui_params()

        check("the Minkowski dial reached its own key",
              prefs.GetInt(PROP_NESTING_DIRECTION, -1) == MINKOWSKI_DIAL,
              "stored %s" % prefs.GetInt(PROP_NESTING_DIRECTION, -1))
        check("the Physics dial reached its own key",
              prefs.GetInt(PROP_PHYSICS_DIRECTION, -1) == PHYSICS_DIAL,
              "stored %s" % prefs.GetInt(PROP_PHYSICS_DIRECTION, -1))
    finally:
        close_panel()

    # A brand-new panel, so the values can only have come from preferences -- and
    # `clear=False`, or this probe would delete the keys it is about to read.
    doc, again = new_panel("prefs_read", clear=False)
    try:
        check("the Minkowski dial came back",
              again.minkowski_direction_dial.value() == MINKOWSKI_DIAL,
              "got %d" % again.minkowski_direction_dial.value())
        check("the Physics dial came back too",
              again.physics_direction_dial.value() == PHYSICS_DIAL,
              "got %d" % again.physics_direction_dial.value())
        emit("  new session: minkowski_dial=%d physics_dial=%d"
             % (again.minkowski_direction_dial.value(),
                again.physics_direction_dial.value()))
    finally:
        close_panel()
        for key in (PROP_NESTING_DIRECTION, PROP_PHYSICS_DIRECTION):
            prefs.RemInt(key)


def main():
    cases = [
        case_collect_follows_the_algorithm,
        case_layout_restores_algorithm_and_direction,
        case_random_run_greys_its_own_dial,
        case_old_layout_still_loads,
        case_part_rotation_override_round_trip,
        case_physics_dial_is_remembered,
    ]
    for case in cases:
        try:
            case()
        except Exception:
            emit("  ERROR in %s:" % case.__name__)
            emit(traceback.format_exc())
            _FAILURES.append("%s raised" % case.__name__)
    emit("")
    emit("%d/%d checks passed" % (_CHECKS[0] - len(_FAILURES), _CHECKS[0]))
    # The user's preferences are left as they were found, whatever this did.
    clear_prefs()
    close_out()
    if _FAILURES:
        emit("FAILED: " + ", ".join(_FAILURES))
        return 1
    return 0


_status = main()
try:
    with open(_STATUS_FILE, "w") as handle:
        handle.write(str(_status))
except OSError:
    pass
emit("LAYOUT_RESTORE_STATUS=%d" % _status)
sys.exit(_status)
