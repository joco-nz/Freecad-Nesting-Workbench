# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/length_field.py

"""
A unit-aware spin box that keeps millimetres as the canonical value.

Why not just put a QDoubleSpinBox in the panel and convert at the edges
-----------------------------------------------------------------------
Because the conversion has to happen in exactly one place, and a
QDoubleSpinBox cannot do it. It has no idea what a unit is, so the panel would
either store display units and convert on read -- meaning the stored value and
the number on screen are two different things -- or store millimetres and
update the suffix by hand, which is the same amount of work with none of the
input handling. FreeCADGui.UiLoader already hands out FreeCAD's own
``Gui::QuantitySpinBox``, which parses "1/2 in", "2ft 6in" and "1' 11\\\"" and
renders the document's units, so the only work left is stopping it from
corrupting our value.

The corruption is real, and it is the reason this class exists at all
-----------------------------------------------------------------------
The widget rounds to the display unit's precision every time a value is set
into it, and what comes back out is the rounded number. Measured on
FreeCAD 26.3 under the imperial-decimal schema:

    set 600 mm  ->  reads back 599.948 mm
    set 12.5 mm ->  reads back 12.446 mm     (a Part Spacing)
    set 0.5 mm  ->  reads back 0.508 mm
    set 0.5 mm  ->  reads back 0.0 mm         (under Building US, ft-in)

None of those is a rounding artefact worth arguing about -- 12.446 mm of part
spacing instead of 12.5 mm is a different nest, and the last one silently
deletes the gap. So the rule here is one-directional: the millimetre value
lives in Python, is written into the widget for display, and is *never* read
back out. ``mm()`` returns the stored figure; only a real user edit replaces
it, and a user edit is exact (typing "0.5 mm" reads back 0.5 mm, not 0.508).

Why the widget is left unbounded
--------------------------------
A bounded ``Gui::QuantitySpinBox`` is a trap. Set a value above its maximum
and the text updates to the right number while the Quantity behind it goes
stale, and it stays stale through the next user edit too:

    max = 10000 mm, value 20000 mm ->  text '787.40 in'   value 12.7 mm
    user then types "500 in"        ->  text '500 in'      value 12.7 mm

So the widget's min/max are left at their defaults (+/-DBL_MAX) and the range
is enforced in Python instead. That keeps ``property("value")`` trustworthy,
which is what makes the user-edit read path in ``_on_value_changed`` sound.
"""

import FreeCAD
import FreeCADGui
import math


def _create_widget():
    """A bare ``Gui::QuantitySpinBox``.

    Via UiLoader rather than a Python type because FreeCAD does not expose the
    C++ class to Python: it is reachable only through the Qt meta-object, which
    is why everything below goes through setProperty/property instead of
    setValue/setRange.

    Note the deliberate absence of ``setValue``. It is a real slot, and PySide
    picks an overload for it -- the wrong one, which writes 0.0 rather than
    the value. setProperty("value", Quantity) is the only reliable setter.
    """
    return FreeCADGui.UiLoader().createWidget("Gui::QuantitySpinBox")


class LengthField:
    """A millimetre-canonical, unit-aware spin box.

    Not a QWidget subclass: the workbench's call sites read a value and set a
    value, and both are far clearer as ``mm()`` / ``set_mm()`` than as
    ``value()`` / ``setValue()`` when the thing being moved is millimetres and
    the thing on screen may be inches. Use ``widget()`` for anything that
    genuinely needs the Qt object.

    Args:
        mm_min: Optional lower bound in mm, applied to user edits.
        mm_max: Optional upper bound in mm, applied to user edits.
        single_step_mm: Optional arrow-key increment in mm.
        tooltip: Optional tooltip text. Callers that mention a unit should
            build it with units.format_length so it follows the document.
    """

    def __init__(self, mm_min=None, mm_max=None, single_step_mm=None,
                 tooltip=None, parent=None):
        self._mm_min = None if mm_min is None else float(mm_min)
        self._mm_max = None if mm_max is None else float(mm_max)
        self._mm = 0.0
        # Set while this object writes the widget, so the valueChanged it
        # provokes is not mistaken for the user having edited the field --
        # which is the whole mechanism by which a rounded display value would
        # otherwise climb back into the canonical one.
        self._seeding = False
        # The widget's own value as of the last accepted change. A spin box
        # signals valueChanged even when it REJECTS what was typed and snaps
        # back to the previous number, so "it signalled" is not by itself
        # evidence that the value changed -- and the number it snaps back to
        # is the rounded one. Comparing against this is what keeps a stray
        # keystroke from quietly rewriting 42.0 mm as 41.91 mm.
        self._last_widget_mm = None

        self._widget = _create_widget()
        if parent is not None:
            try:
                self._widget.setParent(parent)
            except Exception:
                pass
        if tooltip:
            self._widget.setToolTip(tooltip)
        # Not set: the widget's own minimum/maximum. See the module docstring.
        if single_step_mm is not None:
            self._widget.setProperty("singleStep", float(single_step_mm))
        self._widget.setProperty("value", FreeCAD.Units.Quantity(0.0, "mm"))
        try:
            self._widget.valueChanged.connect(self._on_value_changed)
        except Exception:
            pass

    # -- reading ----------------------------------------------------------
    def mm(self):
        """The canonical value in millimetres. Never a rounded display value."""
        return self._mm

    def widget(self):
        """The underlying ``Gui::QuantitySpinBox``, for layout and signals."""
        return self._widget

    def text(self):
        """Whatever the field currently shows, for diagnostics and messages."""
        try:
            return self._widget.text()
        except Exception:
            return ""

    # -- writing ----------------------------------------------------------
    def set_mm(self, mm):
        """Set the canonical value and re-render the field.

        The value is stored as given, without clamping. A layout written by a
        previous run is the user's data, and quietly replacing a 20000 mm
        sheet with the field's 10000 mm maximum would lose it while showing
        the user a number that disagrees with the document. Bounds are for
        typed input, not for restored settings -- see ``_on_value_changed``.
        """
        try:
            mm = float(mm)
        except (TypeError, ValueError):
            return
        if not math.isfinite(mm):
            return
        self._mm = mm
        self.refresh()

    def refresh(self):
        """Re-render the field from the canonical value.

        Also the schema-change path: the widget re-reads the document's unit
        system when it is given a value, so re-seeding is what makes a switch
        from a metric document to an imperial one take effect in an
        already-open panel.
        """
        self._seeding = True
        try:
            self._widget.setProperty("value",
                                     FreeCAD.Units.Quantity(self._mm, "mm"))
            self._last_widget_mm = self._read_widget_mm()
        except Exception:
            pass
        finally:
            self._seeding = False

    # -- internals --------------------------------------------------------
    def _on_value_changed(self, *_args):
        """A user changed the field: adopt the new value.

        Reached on every keystroke-commit, arrow key and paste. Not reached by
        our own writes, which ``_seeding`` suppresses -- and suppressing them
        is load-bearing rather than tidy, because an adopted rounded value is
        how 12.5 mm of Part Spacing becomes 12.446 mm.
        """
        if self._seeding:
            return
        value = self._read_widget_mm()
        if value is None or value == self._last_widget_mm:
            # Nothing actually changed, or the spin box rejected the entry and
            # snapped back. Either way the value on screen is the one already
            # in force, so there is nothing to adopt -- and adopting it would
            # substitute the rounded display figure for the canonical one.
            return
        self._last_widget_mm = value
        clamped = self._clamp(value)
        self._mm = clamped
        if clamped != value:
            # Out of range. Re-render so the field shows the value that will
            # actually be used; a bounded QDoubleSpinBox snapped its own text
            # to the bound, and leaving "0.5 mm" above a 1 mm minimum while
            # the run used 1.0 mm is precisely the kind of quiet divergence
            # this class exists to prevent.
            self.refresh()

    def _read_widget_mm(self):
        """The widget's value in mm, or None if it cannot be trusted.

        ``property("value")`` is a ``Base.Quantity`` whose ``Value`` is
        already the internal millimetre figure, whatever unit the user typed
        it in. The wrapper's PySide type is QAbstractSpinBox, so the Quantity
        overloads are not directly callable and the property system is the
        only channel -- which is also the one that behaves.
        """
        try:
            value = self._widget.property("value")
        except Exception:
            return None
        if value is None:
            return None
        try:
            return float(value.Value)
        except AttributeError:
            pass
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _clamp(self, mm):
        """Apply the field's range, as a bounded spin box would have."""
        if self._mm_min is not None and mm < self._mm_min:
            return self._mm_min
        if self._mm_max is not None and mm > self._mm_max:
            return self._mm_max
        return mm
