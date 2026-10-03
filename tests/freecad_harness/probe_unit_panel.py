#!/usr/bin/env freecad
"""Viability probe and regression check for unit-aware panel fields.

Run with the GUI binary, NOT freecadcmd:

    /home/james/freecad_env/usr/bin/freecad \
        tests/freecad_harness/probe_unit_panel.py

Why this is a probe and not a test in run.sh
--------------------------------------------
`freecadcmd` has no `FreeCADGui.UiLoader`, so `Gui::QuantitySpinBox` cannot be
constructed at all there:

    AttributeError: module 'FreeCADGui' has no attribute 'UiLoader'

No Xvfb is needed. FreeCAD 26.3's `freecad` binary starts a live GUI on no
display at all (see the "GUI sessions" section of this directory's README).

What is asserted, and why each one is a real risk
--------------------------------------------------
The invariant is that a field's millimetre value is never replaced by the
rounded figure its widget displays. A unit-aware spin box rounds whatever is
written into it to the display unit's precision, and under the imperial-decimal
schema that is measurable:

    set 600 mm  -> reads back 599.948 mm
    set 12.5 mm -> reads back 12.446 mm      (Part Spacing)
    set 0.5 mm  -> reads back 0.0 mm          (Building US, ft-in -- 1/16" floor)

The last one is the reason this work exists: a 0.5 mm part gap would become no
gap at all. So each case below is one route by which the rounded number could
climb back into the canonical value, and the checks are the routes failing.

Two more traps this file records
--------------------------------
1. A **bounded** QuantitySpinBox is worse than an unbounded one. Set a value
   above its maximum and the text updates correctly while the Quantity behind
   it goes stale -- and it stays stale through the next user edit:

       max = 10000 mm, value 20000 mm -> text '787.40 in'   value 12.7 mm
       then the user types "500 in"    -> text '500 in'      value 12.7 mm

   So the widget is deliberately left unbounded and the range is enforced in
   Python. `case_bounded_widget_is_a_trap` demonstrates the failure rather than
   assuming it.

2. `w.setValue(Quantity(...))` **silently writes 0.0**. PySide resolves the
   overload to the wrong slot, and the widget reports the value as changed.
   Everything therefore goes through setProperty/property.
   `case_setvalue_overload_writes_zero` demonstrates that too.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import FreeCAD
import FreeCADGui

FreeCADGui.showMainWindow()

_STATUS_FILE = os.path.join(_HERE, ".last_status_unit_panel")
_FAILURES = []
_CHECKS = [0]


def emit(message=""):
    try:
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


def _close(a, b, tol=1e-6):
    return abs(float(a) - float(b)) <= tol


def _imperial():
    return [i for i, n in enumerate(FreeCAD.Units.listSchemas())
            if "imperial" in n.lower()]


def _type(field, text):
    field.widget().lineEdit().setText(text)
    field.widget().editingFinished.emit()


# --------------------------------------------------------------------------
# Case 1: the canonical value survives every imperial schema
# --------------------------------------------------------------------------
def case_canonical_value_survives():
    from freecad.nestingworkbench.length_field import LengthField

    emit("")
    emit("--- canonical mm is never replaced by a rounded display value ---")

    doc = FreeCAD.newDocument("canonical")
    enums = doc.getEnumerationsOfProperty("UnitSystem")

    for index in _imperial():
        name = FreeCAD.Units.listSchemas()[index]
        doc.UnitSystem = enums[index]
        field = LengthField(mm_min=0, mm_max=10000)
        for mm in (600.0, 12.5, 3.0, 0.5):
            field.set_mm(mm)
            check(f"{name}: set_mm({mm}) stays exact", _close(field.mm(), mm),
                  f"mm()={field.mm()}")
        emit(f"         {name} shows 0.5 mm as {field.text()!r}")

    # The same across a schema change, which is the reopen-the-panel case: the
    # field keeps its millimetres and only the rendering moves.
    doc.UnitSystem = enums[6]
    field = LengthField(mm_min=0, mm_max=10000)
    field.set_mm(12.5)
    for index in _imperial():
        name = FreeCAD.Units.listSchemas()[index]
        doc.UnitSystem = enums[index]
        field.refresh()
        check(f"{name}: re-render keeps 12.5", _close(field.mm(), 12.5),
              f"mm()={field.mm()} shows {field.text()!r}")

    FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# Case 2: typed entry is exact
# --------------------------------------------------------------------------
def case_user_entry_exact():
    from freecad.nestingworkbench.length_field import LengthField

    emit("")
    emit("--- typed entry is exact, in every form FreeCAD parses ---")

    doc = FreeCAD.newDocument("user_entry")
    enums = doc.getEnumerationsOfProperty("UnitSystem")

    doc.UnitSystem = enums[3]  # Imperial decimal
    for text, want in (("0.5 mm", 0.5), ("1/2 in", 12.7), ("1' 11\"", 584.2),
                       ("1' 6\"", 457.2), ("12.5 mm", 12.5), ("0.49 in", 12.446)):
        field = LengthField(mm_min=0, mm_max=10000)
        field.set_mm(600.0)
        _type(field, text)
        check(f"imperial: typed {text!r} is {want}", _close(field.mm(), want, 1e-3),
              f"mm()={field.mm()} shows {field.text()!r}")

    # A negative entry is refused by the range, and the field re-renders to
    # say so. Every length in the panel has a non-negative bound -- Part
    # Spacing was already 0..1000 as a QDoubleSpinBox -- so this is the
    # correct outcome, not a loss: a negative gap is not a gap the user meant.
    field = LengthField(mm_min=0, mm_max=10000)
    field.set_mm(600.0)
    _type(field, "-3 in")
    check("a negative entry clamps to the field's lower bound",
          _close(field.mm(), 0.0) and field.text() == "0.00 in",
          f"mm()={field.mm()} shows {field.text()!r}")
    # Where the range does allow negatives, they are honoured.
    field = LengthField(mm_min=-1000, mm_max=10000)
    field.set_mm(600.0)
    _type(field, "-3 in")
    check("a negative entry is honoured where the range allows it",
          _close(field.mm(), -76.2, 1e-3), f"mm()={field.mm()}")

    # A bare number takes the document's unit. The widget resolves it against
    # its own schema, which is the one thing it is better at than we are.
    field = LengthField(mm_min=0, mm_max=10000)
    field.set_mm(600.0)
    _type(field, "12")
    check("imperial: bare '12' means 12 in", _close(field.mm(), 304.8, 1e-3),
          f"mm()={field.mm()}")
    doc.UnitSystem = enums[6]
    field = LengthField(mm_min=0, mm_max=10000)
    field.set_mm(600.0)
    _type(field, "12")
    check("metric: bare '12' means 12 mm", _close(field.mm(), 12.0), f"mm()={field.mm()}")

    # ft-in fractions: the schema that renders 0.5 mm as a bare "0".
    doc.UnitSystem = enums[5]
    field = LengthField(mm_min=0, mm_max=10000)
    field.set_mm(0.5)
    check("ft-in: 0.5 mm is kept, not rounded to nothing", _close(field.mm(), 0.5),
          f"mm()={field.mm()} shows {field.text()!r}")
    _type(field, "1/2 in")
    check("ft-in: typed 1/2 in is 12.7", _close(field.mm(), 12.7), f"mm()={field.mm()}")

    FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# Case 3: bounds, rejection, and re-render idempotence
# --------------------------------------------------------------------------
def case_bounds_and_rejection():
    from freecad.nestingworkbench.length_field import LengthField

    emit("")
    emit("--- bounds apply to typed input, not to restored settings ---")

    doc = FreeCAD.newDocument("bounds")
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]

    # Restored settings are the user's data. A saved 20000 mm sheet clamped to
    # the field's 10000 mm maximum would be silently rewritten, and the field
    # would then show a number disagreeing with the document it came from.
    field = LengthField(mm_min=1, mm_max=10000)
    field.set_mm(20000.0)
    check("set_mm does not clamp a restored value", _close(field.mm(), 20000.0),
          f"mm()={field.mm()}")

    # Typed input is bounded, as it was by the QDoubleSpinBox it replaced, and
    # the field re-renders so the number on screen is the one that will be
    # used. Without that the field would read "0.5 mm" while the run used 1.0.
    _type(field, "50000 mm")
    check("typed 50000 clamps to the maximum", _close(field.mm(), 10000.0),
          f"mm()={field.mm()}")
    check("the field re-rendered to the clamped value", field.text() == "10000.00 mm",
          repr(field.text()))

    field = LengthField(mm_min=0, mm_max=1000)
    field.set_mm(0.0)
    _type(field, "-5 mm")
    check("typed -5 clamps to the minimum", _close(field.mm(), 0.0), f"mm()={field.mm()}")
    check("the field re-rendered to the minimum", field.text() == "0.00 mm",
          repr(field.text()))

    field = LengthField(mm_min=1, mm_max=10000)
    field.set_mm(600.0)
    _type(field, "0.5 mm")
    check("typed 0.5 under a 1 mm floor clamps and re-renders",
          _close(field.mm(), 1.0) and field.text() == "1.00 mm",
          f"mm()={field.mm()} shows {field.text()!r}")

    # A rejected entry must not quietly substitute the rounded display figure
    # for the canonical one. A spin box signals valueChanged even when it
    # refuses what was typed and snaps back -- and what it snaps back to is the
    # rounded number, so without a change check 42.0 mm becomes 41.91 mm the
    # first time anyone mistypes the field.
    field = LengthField(mm_min=0, mm_max=10000)
    field.set_mm(42.0)
    _type(field, "abc")
    check("a rejected entry leaves the value alone", _close(field.mm(), 42.0),
          f"mm()={field.mm()}")
    field = LengthField(mm_min=0, mm_max=10000)
    field.set_mm(600.0)
    _type(field, "600.00 mm")
    check("re-typing the displayed value is a no-op", _close(field.mm(), 600.0),
          f"mm()={field.mm()}")

    field = LengthField(mm_min=0, mm_max=10000)
    field.set_mm(12.5)
    for _ in range(3):
        field.refresh()
    check("repeated refresh does not drift the value", _close(field.mm(), 12.5),
          f"mm()={field.mm()}")

    FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# Case 4: the two widget traps, demonstrated rather than assumed
# --------------------------------------------------------------------------
def case_setvalue_overload_writes_zero():
    emit("")
    emit("--- the setValue() overload writes zero (why setProperty is used) ---")

    doc = FreeCAD.newDocument("setvalue")
    doc.UnitSystem = doc.getEnumerationsOfProperty("UnitSystem")[3]
    widget = FreeCADGui.UiLoader().createWidget("Gui::QuantitySpinBox")
    widget.setProperty("value", FreeCAD.Units.Quantity("600 mm"))
    before = widget.text()
    emitted = []
    widget.valueChanged.connect(emitted.append)
    widget.setValue(FreeCAD.Units.Quantity("600 mm"))
    after = widget.text()
    emit(f"         setProperty gave {before!r}; setValue gave {after!r}")
    check("setProperty wrote the value", before == "23.62 in", repr(before))
    check("setValue(Quantity) zeroes the field instead", after == "0.00", repr(after))
    check("and it announced a change to 0", 0.0 in emitted, repr(emitted))
    FreeCAD.closeDocument(doc.Name)


def case_bounded_widget_is_a_trap():
    emit("")
    emit("--- a bounded widget's Quantity goes stale (why bounds live in Python) ---")

    doc = FreeCAD.newDocument("bounded")
    doc.UnitSystem = doc.getEnumerationsOfProperty("UnitSystem")[3]
    widget = FreeCADGui.UiLoader().createWidget("Gui::QuantitySpinBox")
    widget.setProperty("maximum", 10000.0)          # mm
    widget.setProperty("value", FreeCAD.Units.Quantity("600 mm"))
    widget.setProperty("value", FreeCAD.Units.Quantity("20000 mm"))
    stale_text = widget.text()
    stale_value = widget.property("value")
    widget.lineEdit().setText("500 in")
    widget.editingFinished.emit()
    typed_text = widget.text()
    typed_value = widget.property("value")
    emit(f"         after set 20000 mm: text {stale_text!r} value {stale_value!r}")
    emit(f"         after typing 500 in: text {typed_text!r} value {typed_value!r}")
    check("the text showed the right number", stale_text == "787.40 in", repr(stale_text))
    check("while the Quantity behind it was stale",
          not _close(stale_value.Value, 20000.0, 1.0), repr(stale_value))
    check("and it stayed stale through the next edit",
          not _close(typed_value.Value, 12700.0, 1.0), repr(typed_value))
    FreeCAD.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# Case 5: the real panel
# --------------------------------------------------------------------------
def case_panel():
    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
    from freecad.nestingworkbench.length_field import LengthField
    from freecad.nestingworkbench.Tools.Nesting.nesting_controller import (
        _DocumentUnitWatcher,
    )

    emit("")
    emit("--- the NestingPanel's own fields ---")

    doc = FreeCAD.newDocument("panel")
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]
    FreeCAD.setActiveDocument(doc.Name)

    panel = NestingPanel()

    length_names = [
        "sheet_width_input", "sheet_height_input", "sheet_thickness_input",
        "part_spacing_input", "simplification_input", "minkowski_step_size_input",
        "physics_step_size_input", "physics_anneal_min_amp",
        "physics_anneal_max_amp", "label_height_input", "label_size_input",
    ]
    for name in length_names:
        field = getattr(panel, name, None)
        check(f"{name} is a LengthField", isinstance(field, LengthField),
              type(field).__name__)
    check("every length field is registered for re-render",
          len(panel._length_fields) == len(length_names),
          f"{len(panel._length_fields)} registered")

    # The rotation-angle control is an ANGLE and must not have been swept up.
    from PySide import QtWidgets
    check("the curve-angle field is still a plain spin box",
          isinstance(panel.deflection_input, QtWidgets.QDoubleSpinBox)
          and not isinstance(panel.deflection_input, LengthField),
          type(panel.deflection_input).__name__)


# --------------------------------------------------------------------------
# Case 6: the collapsible groups
# --------------------------------------------------------------------------
def case_collapsible_groups():
    """Helpers and Logging collapse, and stay fully functional while collapsed.

    The property that matters is not "the title is a toggle" -- it is that a
    collapsed group's children are HIDDEN BUT ALIVE. Everything in these two
    groups is read unconditionally by _collect_ui_params, so a collapse that
    destroyed or disabled the children would silently change what a run uses:
    verbose logging off, labels off, no font. That is the failure worth a test,
    and a bare isCheckable() assertion would not catch it.
    """
    import FreeCAD as _fc
    import FreeCADGui as _fgui

    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

    emit("")
    emit("--- collapsible groups ---")

    doc = _fc.newDocument("collapse")
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]
    _fc.setActiveDocument(doc.Name)
    panel = NestingPanel()

    for name in ("minkowski_settings_group", "helpers_group", "logging_group"):
        group = getattr(panel, name)
        check(f"{name} is checkable", group.toggle_button.isCheckable(),
              "not a toggle")
        check(f"{name} starts collapsed", group.isExpanded() is False,
              f"isExpanded={group.isExpanded()}")
        # Collapsed must mean HIDDEN, never disabled. Disabling the group
        # greys out its controls and reads as broken rather than tucked away,
        # and it is the easy way to "collapse" something by mistake -- state
        # reads still succeed, so nothing else in this file would notice.
        check(f"{name} is not disabled by collapsing", group.isEnabled(),
              "the group is disabled rather than merely collapsed")

    # Children are alive and readable while collapsed.
    helpers, logging = panel.helpers_group, panel.logging_group
    check("Helpers children are hidden while collapsed",
          not panel.verbose_logging_checkbox.isVisible(),
          "a collapsed child is still visible")
    check("...but still hold their state",
          panel.verbose_logging_checkbox.isChecked() is False
          and panel.add_labels_checkbox.isChecked() is False)
    check("Logging children are hidden while collapsed",
          not panel.performance_logging_checkbox.isVisible())

    # The real test: _collect_ui_params must see through the collapse. Turn the
    # controls on while they are invisible, then read the params a run would
    # use. A collapse that hid the state rather than the pixels fails here.
    panel.verbose_logging_checkbox.setChecked(True)
    panel.add_labels_checkbox.setChecked(True)
    panel.label_size_input.set_mm(7.5)
    panel.sound_checkbox.setChecked(False)
    params = panel.controller._collect_ui_params()
    check("a collapsed group's controls still reach the run params",
          params['verbose'] is True and params['add_labels'] is True,
          f"verbose={params['verbose']} add_labels={params['add_labels']}")
    check("...including its unit-aware fields",
          _close(params['label_size'], 7.5), f"label_size={params['label_size']}")

    # Expanding must actually reveal the children, or the toggle is decorative.
    panel.show()
    for _ in range(20):
        _fgui.updateGui()
    helpers.setExpanded(True)
    logging.setExpanded(True)
    for _ in range(20):
        _fgui.updateGui()
    check("expanding Helpers reveals its children",
          panel.verbose_logging_checkbox.isVisible(),
          "children stayed hidden after expanding")
    check("expanding Logging reveals its children",
          panel.performance_logging_checkbox.isVisible())

    # THE assertion this case exists for. A checkable QGroupBox that is
    # unchecked only DISABLES its children -- measured, two identical groups:
    # 138px with FreeCAD's house pattern (setCheckable + setChecked only) and
    # 40px with the contents hidden. The greyed-but-visible version looked like
    # it worked, passed an isCheckable() test, and reclaimed no space at all.
    # So the property worth locking down is the height, not the flag.
    # Expanding must actually reveal the children, or the toggle is decorative.
    panel.show()
    for _ in range(20):
        _fgui.updateGui()
    helpers.setExpanded(True)
    logging.setExpanded(True)
    panel.minkowski_settings_group.setExpanded(True)
    for _ in range(20):
        _fgui.updateGui()
    check("expanding Helpers reveals its children",
          panel.verbose_logging_checkbox.isVisible(),
          "children stayed hidden after expanding")
    check("expanding Logging reveals its children",
          panel.performance_logging_checkbox.isVisible())
    check("expanding Nesting Settings reveals the dial",
          panel.minkowski_direction_dial.isVisible(),
          "the dial stayed hidden after expanding")

    # THE assertion this case exists for. A checkable QGroupBox that is
    # unchecked only DISABLES its children -- measured, two identical groups:
    # 138px with FreeCAD's house pattern (setCheckable + setChecked only) and
    # 40px with the contents hidden. The greyed-but-visible version looked like
    # it worked, passed an isCheckable() test, and reclaimed no space at all.
    # So the property worth locking down is the height, not the flag.
    expanded_height = helpers.sizeHint().height()
    logging_expanded = logging.sizeHint().height()
    settings = panel.minkowski_settings_group
    settings_expanded = settings.sizeHint().height()
    helpers.setExpanded(False)
    logging.setExpanded(False)
    settings.setExpanded(False)
    for _ in range(20):
        _fgui.updateGui()
    collapsed_height = helpers.sizeHint().height()
    logging_collapsed = logging.sizeHint().height()
    settings_collapsed = settings.sizeHint().height()
    check("collapsing Helpers reclaims its height",
          collapsed_height < expanded_height,
          f"collapsed={collapsed_height}px expanded={expanded_height}px")
    check("collapsing Logging reclaims its height",
          logging_collapsed < logging_expanded,
          f"collapsed={logging_collapsed}px expanded={logging_expanded}px")
    check("collapsing Nesting Settings reclaims its height",
          settings_collapsed < settings_expanded,
          f"collapsed={settings_collapsed}px expanded={settings_expanded}px")
    check("a collapsed group is title-sized, not content-sized",
          collapsed_height < 80,
          f"collapsed Helpers is {collapsed_height}px, which still looks expanded")

    # And the unit refresh must survive a collapse: LengthFields live inside
    # Helpers, and re-rendering a hidden widget is the interaction most likely
    # to be quietly wrong.
    helpers.setExpanded(False)
    doc.UnitSystem = enums[3]
    panel.refresh_unit_display()
    check("a collapsed group's unit fields still re-render",
          panel.label_size_input.text() == "0.30 in",
          repr(panel.label_size_input.text()))
    check("...with the millimetres intact",
          _close(panel.label_size_input.mm(), 7.5),
          f"mm()={panel.label_size_input.mm()}")

    # A fresh panel must start collapsed again -- the state is deliberately not
    # persisted, so this asserts the absence of a preference write.
    panel.dispose()
    panel.deleteLater()
    again = NestingPanel()
    check("a reopened panel starts collapsed again",
          again.helpers_group.isExpanded() is False
          and again.logging_group.isExpanded() is False)
    again.dispose()
    again.deleteLater()
    _fc.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# Case 7: the direction dial
# --------------------------------------------------------------------------
def case_direction_dial():
    """No buttons, 15-degree steps, and a readout that is not the dial value.

    The readout is the part that would otherwise be quietly wrong. A QDial
    reading is not a compass bearing -- it is rotated a quarter turn and
    flipped -- so displaying the reading told the user 90 where the run
    searched 180. That was survivable when the only reachable values were the
    four the buttons snapped to, and wrong at 20 of the 24 positions now
    reachable.
    """
    from PySide import QtWidgets

    import FreeCAD as _fc

    from freecad.nestingworkbench.constants import (
        DEFAULT_DIRECTION_DIAL, DIRECTION_LABELS, DIRECTION_STEP_DEGREES,
        dial_to_bearing)
    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

    emit("")
    emit("--- direction dial ---")

    doc = _fc.newDocument("dial")
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]
    _fc.setActiveDocument(doc.Name)
    panel = NestingPanel()

    dial = panel.minkowski_direction_dial

    # 1. The four buttons are gone.
    from PySide import QtCore
    # Search the section's CONTENT AREA. Searching the section itself became
    # vacuous the moment it became a CollapsibleSection -- its contents live
    # inside content_area, so findChildren there finds nothing and the check
    # passes while testing nothing. Searching the whole panel instead is also
    # wrong, just differently: it picks up Add Selected / Run Nesting / Cancel
    # Nesting, which are supposed to be there, and fails for the right reason
    # at the wrong scope. The assertion is about the direction buttons, so it
    # stays scoped to the section that used to hold them.
    settings_content = panel.minkowski_settings_group.content_area
    buttons = [w for w in settings_content.findChildren(QtWidgets.QPushButton)]
    check("the direction buttons are gone", not buttons,
          f"still present: {[b.text() for b in buttons]}")

    # 2. Fifteen degree steps, on both the click and the keyboard path.
    check("the dial steps 15 degrees per click",
          dial.singleStep() == DIRECTION_STEP_DEGREES,
          f"singleStep={dial.singleStep()}")
    check("the dial's page step matches, so PageUp cannot skip the grid",
          dial.pageStep() == DIRECTION_STEP_DEGREES,
          f"pageStep={dial.pageStep()}")
    check("the default (90) is on the 15-degree grid",
          DEFAULT_DIRECTION_DIAL % DIRECTION_STEP_DEGREES == 0)

    # Every step lands on the grid, and every cardinal the old buttons offered
    # is still reachable -- which is what justifies dropping the buttons.
    on_grid = [v for v in range(360) if v % DIRECTION_STEP_DEGREES == 0]
    check("the step divides 360 exactly", len(on_grid) == 360 // DIRECTION_STEP_DEGREES,
          f"{len(on_grid)} positions")
    reachable = {dial_to_bearing(v) for v in on_grid}
    for name_dial, name in DIRECTION_LABELS.items():
        bearing = dial_to_bearing(name_dial)
        check(f"'{name}' is still reachable at a {DIRECTION_STEP_DEGREES}° step",
              bearing in reachable, f"bearing {bearing} unreachable")

    # 3. The readout gives the bearing, and names it on a cardinal.
    cardinals = {dial_to_bearing(d): n for d, n in DIRECTION_LABELS.items()}
    mismatches = []
    for value in on_grid:
        dial.setValue(value)
        bearing = dial_to_bearing(value)
        text = panel.minkowski_direction_label.text()
        want_name = cardinals.get(bearing, "")
        # The bearing must appear in the text, and the name must lead when the
        # bearing is a cardinal.
        if f"{bearing}°" not in text:
            mismatches.append((value, text, f"bearing {bearing} missing"))
        if want_name and not text.startswith(want_name):
            mismatches.append((value, text, want_name))
        # The reading must not be shown as if it were the bearing. Compared as
        # a NUMBER rather than a substring: "0°" is a substring of "270°", so
        # the string form reports a leak at dial reading 0 that is not one.
        import re
        shown = [int(m) for m in re.findall(r"(\d+)", text)]
        if value != bearing and value in shown:
            mismatches.append((value, text, "raw reading shown as the direction"))
    check("every one of the 24 positions reads back as its bearing",
          not mismatches, f"{len(mismatches)} wrong, first: {mismatches[:3]}")

    dial.setValue(DEFAULT_DIRECTION_DIAL)
    check("the default reads as a named direction",
          panel.minkowski_direction_label.text().startswith("Left"),
          repr(panel.minkowski_direction_label.text()))

    panel.dispose()
    panel.deleteLater()
    _fc.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# Case 8: the direction dial and its checkbox persist
# --------------------------------------------------------------------------
def case_direction_persists():
    """The dial reading and both random-direction checkboxes survive a reopen.

    The dial is stored as a READING, not a bearing, and snapped to the step on
    load. Both matter: a stored bearing would need a second inverse conversion,
    and an unsnapped value from a build with a different step would land
    between notches, where the dial and the readout would both show something
    the user never chose.
    """
    import FreeCAD as _fc

    from freecad.nestingworkbench.constants import (
        PREFS_PATH, PROP_NESTING_DIRECTION, PROP_RANDOM_DIRECTION,
        DIRECTION_STEP_DEGREES)
    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

    emit("")
    emit("--- direction persistence ---")

    prefs = _fc.ParamGet(PREFS_PATH)
    for key in (PROP_NESTING_DIRECTION, PROP_RANDOM_DIRECTION, "PhysicsRandomDirection"):
        prefs.RemInt(key)
        prefs.RemBool(key)

    doc = _fc.newDocument("dialpersist")
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]
    _fc.setActiveDocument(doc.Name)

    first = NestingPanel()
    first.minkowski_direction_dial.setValue(30)
    # Both checkboxes, and they are independent: the Minkowski one disables the
    # Minkowski dial, the Physics one the Physics dial, and checking one must
    # not be read back as the other.
    first.minkowski_random_checkbox.setChecked(True)
    first.physics_random_checkbox.setChecked(True)
    first.controller._collect_ui_params()
    check("the dial reading reached preferences",
          prefs.GetInt(PROP_NESTING_DIRECTION, -1) == 30,
          f"stored {prefs.GetInt(PROP_NESTING_DIRECTION, -1)}")
    check("the random-direction checkbox reached preferences",
          prefs.GetBool(PROP_RANDOM_DIRECTION, False) is True)
    check("the Physics checkbox has its own key",
          prefs.GetBool("PhysicsRandomDirection", False) is True)
    first.dispose()
    first.deleteLater()

    reopened = NestingPanel()
    check("the dial came back on reopen",
          reopened.minkowski_direction_dial.value() == 30,
          f"got {reopened.minkowski_direction_dial.value()}")
    check("the Minkowski checkbox came back on reopen",
          reopened.minkowski_random_checkbox.isChecked() is True)
    check("the Physics checkbox came back on reopen",
          reopened.physics_random_checkbox.isChecked() is True)
    # A restored "random" run must not leave its dial enabled, or the direction
    # would be settable and then ignored -- which reads as a bug in the run.
    check("a restored random-direction run leaves the dial disabled",
          reopened.minkowski_direction_dial.isEnabled() is False,
          "the dial is enabled under a random-direction run")

    # A stored value off the grid must be snapped, or the dial and its readout
    # would show something the user never chose.
    prefs.SetInt(PROP_NESTING_DIRECTION, 37)
    reopened.dispose()
    reopened.deleteLater()
    snapped = NestingPanel()
    loaded = snapped.minkowski_direction_dial.value()
    check("an off-grid stored reading is snapped on load",
          loaded % DIRECTION_STEP_DEGREES == 0, f"got {loaded}")
    check("...to the nearest step",
          loaded in (30, 45), f"37 snapped to {loaded}, expected 30 or 45")
    snapped.dispose()
    snapped.deleteLater()

    for key in (PROP_NESTING_DIRECTION, PROP_RANDOM_DIRECTION, "PhysicsRandomDirection"):
        prefs.RemInt(key)
        prefs.RemBool(key)
    _fc.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# Case 9: Generations and Population Size persist
# --------------------------------------------------------------------------
def case_ga_fields_persist():
    """Both GA fields survive a panel reopen.

    They used to reset to 1 every session unless a layout that had recorded
    them was reopened, so a GA configuration was effectively per-layout rather
    than per-user. The default stays 1 -- see the measurement recorded in
    ui_nesting.load_persisted_settings -- so this asserts the round trip, not a
    higher default.
    """
    import FreeCAD as _fc

    from freecad.nestingworkbench.constants import (
        PREFS_PATH, PROP_GENERATIONS, PROP_POPULATION_SIZE)
    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

    emit("")
    emit("--- Generations and Population Size persistence ---")

    # A clean store, so a previous session cannot make this pass by accident.
    prefs = _fc.ParamGet(PREFS_PATH)
    for key in (PROP_GENERATIONS, PROP_POPULATION_SIZE):
        prefs.RemInt(key)
        prefs.RemString(key)

    doc = _fc.newDocument("gapersist")
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]
    _fc.setActiveDocument(doc.Name)

    first = NestingPanel()
    check("Generations defaults to 1 on a clean store",
          first.minkowski_generations_input.value() == 1,
          f"got {first.minkowski_generations_input.value()}")
    check("Population Size defaults to 1 on a clean store",
          first.minkowski_population_size_input.value() == 1,
          f"got {first.minkowski_population_size_input.value()}")

    first.minkowski_generations_input.setValue(7)
    first.minkowski_population_size_input.setValue(12)
    # save_settings is reached through _collect_ui_params, which is also what
    # a real run calls -- so this exercises the production path rather than a
    # direct call that could drift from it.
    params = first.controller._collect_ui_params()
    check("the collected params carry the new values",
          params['generations'] == 7 and params['population_size'] == 12,
          f"{params['generations']} / {params['population_size']}")
    check("they reached preferences under the layout group's property names",
          prefs.GetInt(PROP_GENERATIONS, -1) == 7
          and prefs.GetInt(PROP_POPULATION_SIZE, -1) == 12,
          f"stored {prefs.GetInt(PROP_GENERATIONS, -1)} / "
          f"{prefs.GetInt(PROP_POPULATION_SIZE, -1)}")
    first.dispose()
    first.deleteLater()

    reopened = NestingPanel()
    check("Generations came back on reopen",
          reopened.minkowski_generations_input.value() == 7,
          f"got {reopened.minkowski_generations_input.value()}")
    check("Population Size came back on reopen",
          reopened.minkowski_population_size_input.value() == 12,
          f"got {reopened.minkowski_population_size_input.value()}")

    reopened.dispose()
    reopened.deleteLater()
    for key in (PROP_GENERATIONS, PROP_POPULATION_SIZE):
        prefs.RemInt(key)
    _fc.closeDocument(doc.Name)


# --------------------------------------------------------------------------
# Case 8: the two-column grids
# --------------------------------------------------------------------------
def case_two_column_grids():
    """The sheet and Optimizations blocks are grids, and still hold everything.

    Structural, because a layout is not observable from a value -- the
    assertion is that each widget is present in the grid, that the two are
    distinct layouts, and that neither has lost a field to the conversion.
    """
    from PySide import QtWidgets

    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

    emit("")
    emit("--- two-column grids ---")

    doc = FreeCAD.newDocument("grids")
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]
    FreeCAD.setActiveDocument(doc.Name)
    panel = NestingPanel()

    def grid_of(widget):
        """The QGridLayout holding a section's contents, or None.

        A CollapsibleSection's own layout is the QVBoxLayout that stacks the
        toggle button over the content area, so the grid is one level down. Both
        are checked so this keeps working whichever shape a section has.
        """
        layout = widget.layout()
        if isinstance(layout, QtWidgets.QGridLayout):
            return layout
        content_area = getattr(widget, "content_area", None)
        if content_area is not None:
            inner = content_area.layout()
            if isinstance(inner, QtWidgets.QGridLayout):
                return inner
        return None

    def occupied(grid):
        """The widgets actually placed in a grid, in row-major order."""
        found = []
        for index in range(grid.count()):
            item = grid.itemAt(index)
            child = item.widget()
            if child is not None:
                row, column = grid.getItemPosition(index)[:2]
                found.append((row * 10 + column, child))
        return [w for _, w in sorted(found, key=lambda pair: pair[0])]

    # -- the sheet block --
    sheet_grid = None
    for layout in panel.findChildren(QtWidgets.QGridLayout):
        widgets = occupied(layout)
        if panel.sheet_width_input.widget() in widgets:
            sheet_grid = layout
            break
    check("the sheet fields are in a QGridLayout", sheet_grid is not None,
          "the sheet block is not a grid")
    if sheet_grid is not None:
        cells = occupied(sheet_grid)
        expected = [panel.sheet_width_input.widget(),
                    panel.sheet_height_input.widget(),
                    panel.sheet_thickness_input.widget(),
                    panel.part_spacing_input.widget()]
        check("all four sheet fields are placed", set(expected) <= set(cells),
              f"{len(set(cells) & set(expected))}/4 placed")
        # 2 columns x 4 grid cells (label, field) x 2 rows.
        check("the sheet grid is 2 columns wide", sheet_grid.columnCount() == 4,
              f"columnCount={sheet_grid.columnCount()}")
        check("the sheet grid is 2 rows of fields", sheet_grid.rowCount() == 2,
              f"rowCount={sheet_grid.rowCount()}")
        positions = {(sheet_grid.getItemPosition(i)[:2])
                     for i in range(sheet_grid.count())
                     if sheet_grid.itemAt(i).widget() is panel.sheet_width_input.widget()}
        check("width sits in the left column", positions == {(0, 1)}, str(positions))
        positions = {(sheet_grid.getItemPosition(i)[:2])
                     for i in range(sheet_grid.count())
                     if sheet_grid.itemAt(i).widget() is panel.sheet_height_input.widget()}
        check("height sits beside it", positions == {(0, 3)}, str(positions))
        check("thickness is below width", _at(sheet_grid, panel.sheet_thickness_input.widget()) == (1, 1),
              str(_at(sheet_grid, panel.sheet_thickness_input.widget())))
        check("spacing is below height", _at(sheet_grid, panel.part_spacing_input.widget()) == (1, 3),
              str(_at(sheet_grid, panel.part_spacing_input.widget())))

    # -- the Optimizations block --
    opt_grid = grid_of(panel.minkowski_optimization_group)
    check("the Optimizations group holds a QGridLayout", opt_grid is not None,
          type(panel.minkowski_optimization_group.layout()).__name__)
    if opt_grid is not None:
        check("the Optimizations grid is 4 cells wide (label, field) x 2",
              opt_grid.columnCount() == 4, f"columnCount={opt_grid.columnCount()}")
        check("the Optimizations grid is 4 rows", opt_grid.rowCount() == 4,
              f"rowCount={opt_grid.rowCount()}")
        # 8 entries, row-major, so this is the requested order:
        #   1 Generations        | 2 Population Size
        #   3 Stop At Sheets     | 4 Candidate Step
        #   5 Rotation Threads   | 6 Candidate Geometry Cache
        #   7 Compactness        | 8 Clear NFP Cache
        expected_order = [
            ("minkowski_generations_input", panel.minkowski_generations_input, (0, 1)),
            ("minkowski_population_size_input", panel.minkowski_population_size_input, (0, 3)),
            ("minkowski_target_sheets_input", panel.minkowski_target_sheets_input, (1, 1)),
            ("Candidate Step", panel.minkowski_step_size_input.widget(), (1, 3)),
            ("minkowski_rotation_workers_input", panel.minkowski_rotation_workers_input, (2, 1)),
            ("candidate_geometry_cache_checkbox", panel.candidate_geometry_cache_checkbox, (2, 2)),
            ("clear_cache_checkbox", panel.clear_cache_checkbox, (3, 2)),
        ]
        for label, widget, want in expected_order:
            check(f"{label} is at row {want[0]} col {want[1]}",
                  _at(opt_grid, widget) == want, str(_at(opt_grid, widget)))
        # The two checkboxes span their whole column, since they have no label.
        for label, widget in (("Candidate Geometry Cache",
                               panel.candidate_geometry_cache_checkbox),
                              ("Clear NFP Cache", panel.clear_cache_checkbox)):
            spans = [opt_grid.getItemPosition(i)[2:]
                     for i in range(opt_grid.count())
                     if opt_grid.itemAt(i).widget() is widget]
            check(f"{label} spans both of its cells", spans == [(1, 2)], str(spans))
        # Compactness is a labelled field whose cell holds a layout, not a
        # widget, so it is placed by that layout instead.
        compact_cells = [opt_grid.getItemPosition(i)[:2]
                         for i in range(opt_grid.count())
                         if opt_grid.itemAt(i).widget() is None
                         and opt_grid.itemAt(i).layout() is not None]
        check("Compactness occupies a labelled cell", (3, 1) in compact_cells,
              str(compact_cells))

    # Two distinct grids, not one shared or accidentally the same object.
    check("the sheet grid and the Optimizations grid are separate objects",
          sheet_grid is not None and opt_grid is not None
          and sheet_grid is not opt_grid)

    # The behaviour behind the widgets is untouched by the layout change.
    panel.sheet_width_input.set_mm(600.0)
    check("the grid did not disturb the value",
          _close(panel.sheet_width_input.mm(), 600.0),
          f"mm()={panel.sheet_width_input.mm()}")

    panel.dispose()
    panel.deleteLater()
    FreeCAD.closeDocument(doc.Name)


def _at(grid, widget):
    """(row, column) of ``widget`` in ``grid``, or None."""
    for index in range(grid.count()):
        if grid.itemAt(index).widget() is widget:
            return grid.getItemPosition(index)[:2]
    return None

    # The whole point: the panel shows inches and hands back millimetres.
    panel.sheet_width_input.set_mm(600.0)
    panel.part_spacing_input.set_mm(12.5)
    doc.UnitSystem = enums[3]
    panel.refresh_unit_display()
    check("sheet width re-rendered in inches",
          panel.sheet_width_input.text() == "23.62 in",
          repr(panel.sheet_width_input.text()))
    check("part spacing re-rendered in inches",
          panel.part_spacing_input.text() == "0.49 in",
          repr(panel.part_spacing_input.text()))
    check("...and the millimetres the run uses are untouched",
          _close(panel.sheet_width_input.mm(), 600.0)
          and _close(panel.part_spacing_input.mm(), 12.5),
          f"{panel.sheet_width_input.mm()} / {panel.part_spacing_input.mm()}")

    params = panel.controller._collect_ui_params()
    check("the collected params are millimetres, not inches",
          _close(params['sheet_width'], 600.0) and _close(params['spacing'], 12.5),
          f"width={params['sheet_width']} spacing={params['spacing']}")
    check("and they are plain floats",
          isinstance(params['sheet_width'], float)
          and not hasattr(params['sheet_width'], 'Value'),
          type(params['sheet_width']).__name__)

    # A schema change raises no signal, so the pre-run refresh is the only
    # thing that catches it. set_mm re-renders in whatever the current schema
    # is, so the point of the check is that the millimetres survive the
    # rendering and that refreshing again is harmless.
    doc.UnitSystem = enums[3]
    panel.sheet_width_input.set_mm(400.0)
    check("set_mm renders in the current schema",
          panel.sheet_width_input.text() == "15.75 in",
          repr(panel.sheet_width_input.text()))
    panel.refresh_unit_display()
    check("the pre-run refresh leaves the rendering alone",
          panel.sheet_width_input.text() == "15.75 in",
          repr(panel.sheet_width_input.text()))
    check("with the millimetres intact", _close(panel.sheet_width_input.mm(), 400.0),
          f"mm()={panel.sheet_width_input.mm()}")

    # The document observer: switching documents must re-resolve the document
    # and re-render the open fields. The new document is deliberately imperial
    # and the old one metric, so "followed the new document" cannot pass by
    # accident.
    emit("")
    emit("--- a document switch re-resolves and re-renders ---")
    controller = panel.controller
    check("the controller is watching documents", controller._unit_watcher is not None)
    check("it started on the first document",
          controller.doc.Name == doc.Name,
          f"doc={getattr(controller.doc, 'Name', None)}")

    other = FreeCAD.newDocument("panel_other")
    other.UnitSystem = other.getEnumerationsOfProperty("UnitSystem")[6]
    FreeCAD.setActiveDocument(other.Name)
    FreeCADGui.updateGui()
    check("the switch re-resolved the controller's document",
          controller.doc.Name == other.Name,
          f"doc={getattr(controller.doc, 'Name', None)}")
    check("and the open fields followed the new document's units",
          panel.sheet_width_input.text() == "400.00 mm",
          repr(panel.sheet_width_input.text()))
    check("with the millimetres intact", _close(panel.sheet_width_input.mm(), 400.0),
          f"mm()={panel.sheet_width_input.mm()}")

    # Re-activating the SAME document must be a no-op. FreeCAD hands the
    # observer a fresh Python wrapper every time, so an identity test would
    # miss this and drop the user's shape table for nothing.
    victim = FreeCAD.newDocument("panel_same")
    victim.UnitSystem = victim.getEnumerationsOfProperty("UnitSystem")[3]
    FreeCAD.setActiveDocument(victim.Name)
    FreeCADGui.updateGui()
    panel.selected_shapes_to_process = [object()]
    FreeCAD.setActiveDocument(victim.Name)
    FreeCADGui.updateGui()
    check("re-activating the same document keeps the panel's state",
          panel.selected_shapes_to_process and controller.doc.Name == victim.Name,
          f"selected={panel.selected_shapes_to_process} "
          f"doc={getattr(controller.doc, 'Name', None)}")
    panel.selected_shapes_to_process = []

    # The teardown contract. FreeCAD holds the observer as a raw pointer to a
    # Python object, so a registration that outlives the panel is a crash on
    # the next activation rather than a leak. Asserted behaviourally, because
    # nothing public reports what is registered: after dispose, a switch must
    # not reach the controller.
    panel.dispose()
    check("dispose unregisters the observer", controller._unit_watcher is None)
    check("dispose is idempotent", _dispose_again(panel))
    controller.doc = other
    after = FreeCAD.newDocument("panel_after_dispose")
    FreeCAD.setActiveDocument(after.Name)
    FreeCADGui.updateGui()
    check("a document switch after dispose does not reach the controller",
          controller.doc.Name == other.Name,
          f"doc={getattr(controller.doc, 'Name', None)}")

    panel.deleteLater()
    for name in ("panel_after_dispose", "panel_same", "panel_other", "panel"):
        if FreeCAD.getDocument(name):
            FreeCAD.closeDocument(name)


def _dispose_again(panel):
    try:
        panel.dispose()
        return True
    except Exception as exc:
        emit(f"         second dispose raised {exc}")
        return False


def main():
    for case in (case_canonical_value_survives,
                 case_user_entry_exact,
                 case_bounds_and_rejection,
                 case_setvalue_overload_writes_zero,
                 case_bounded_widget_is_a_trap,
                 case_panel,
                 case_collapsible_groups,
                 case_direction_dial,
                 case_direction_persists,
                 case_ga_fields_persist,
                 case_two_column_grids):
        try:
            case()
        except Exception:
            emit(f"  ERROR in {case.__name__}:")
            emit(traceback.format_exc())
            _FAILURES.append(f"{case.__name__} raised")
    emit("")
    emit(f"{_CHECKS[0] - len(_FAILURES)}/{_CHECKS[0]} checks passed")
    if _FAILURES:
        emit("FAILED: " + ", ".join(_FAILURES))
        return 1
    return 0


_status = main()
try:
    with open(_STATUS_FILE, "w") as _handle:
        _handle.write(str(_status))
except OSError:
    pass
sys.exit(_status)
