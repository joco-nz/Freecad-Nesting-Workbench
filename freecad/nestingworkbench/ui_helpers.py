# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/ui_helpers.py

"""
UI helper functions and shared layout constants for the Nesting Workbench.
"""

import os
import re

from PySide import QtCore, QtWidgets

from freecad.nestingworkbench import nw_logger

TOOLTIP_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "Resources", "tooltips")
_SVG_WIDTH = re.compile(r'<svg\b[^>]*?\swidth="(\d+)"')


class _JumpSlider(QtWidgets.QSlider):
    """A QSlider whose handle jumps to the clicked point on the groove."""

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.LeftButton:
            style = self.style()
            groove = style.subControlRect(
                QtWidgets.QStyle.CC_Slider, self._option(), QtWidgets.QStyle.SC_SliderGroove, self)
            handle = style.subControlRect(
                QtWidgets.QStyle.CC_Slider, self._option(), QtWidgets.QStyle.SC_SliderHandle, self)
            if not handle.contains(event.pos()):
                span = groove.width() - handle.width()
                pos = event.pos().x() - groove.x() - handle.width() // 2
                self.setValue(QtWidgets.QStyle.sliderValueFromPosition(
                    self.minimum(), self.maximum(), pos, span))
        super().mousePressEvent(event)

    def _option(self):
        option = QtWidgets.QStyleOptionSlider()
        self.initStyleOption(option)
        return option


def make_slider(minimum, maximum, value, tooltip=None, ticks=False):
    """Create a horizontal slider that is easy to grab.

    The slider is at least 24 px tall, so the handle is easy to hit, and a click on the
    groove jumps the handle there. A drag on the handle still works.

    Args:
        minimum, maximum, value: the integer range and starting value.
        tooltip: an already-built `rich_tooltip` string, or None.
        ticks: True draws a tick under every step.
    """
    slider = _JumpSlider(QtCore.Qt.Horizontal)
    slider.setRange(minimum, maximum)
    slider.setValue(value)
    slider.setMinimumHeight(24)
    slider.setStyleSheet(
        "QSlider::groove:horizontal { height: 6px; border-radius: 3px; background: palette(mid); }"
        "QSlider::handle:horizontal { width: 16px; margin: -7px 0; border-radius: 8px;"
        " background: palette(highlight); }")
    if ticks:
        slider.setTickPosition(QtWidgets.QSlider.TicksBelow)
        slider.setTickInterval(1)
        slider.setPageStep(1)
    if tooltip:
        slider.setToolTip(tooltip)
    return slider


def rich_tooltip(context, text, image=None):
    """Translate a tooltip source string and return it as rich text.

    Args:
        context: translation context, the same one given to QT_TRANSLATE_NOOP.
        text: the QT_TRANSLATE_NOOP-marked source. Each "\n" starts a new line.
        image: optional diagram file name in Resources/tooltips/.

    Returns:
        HTML with one <p> per line and the diagram last. Qt word-wraps it. With a
        diagram, the tooltip is exactly the diagram's width and the text wraps to it.
    """
    translated = QtWidgets.QApplication.translate(context, text)
    parts = ["<p style='margin:0 0 4px 0'>%s</p>" % line for line in translated.split("\n") if line]
    if not image:
        return "".join(parts)
    path = os.path.normpath(os.path.join(TOOLTIP_DIR, image))
    if not os.path.isfile(path):
        # Qt draws a broken-image box for a missing file and says nothing.
        nw_logger.warn(f"[ui_helpers] Tooltip image missing: {path}")
        return "".join(parts)
    parts.append("<p style='margin:4px 0 0 0'><img src='%s'></p>" % path.replace("\\", "/"))
    with open(path, encoding="utf-8") as f:
        match = _SVG_WIDTH.search(f.read())
    if match is None:
        nw_logger.warn(f"[ui_helpers] Tooltip image has no width attribute: {path}")
        return "".join(parts)
    # Qt sizes a rich tooltip from its text, not its image, so a diagram sits in a wide
    # popup. A fixed-width table makes the popup the diagram's width.
    return "<table width='%s' cellspacing='0' cellpadding='0'><tr><td>%s</td></tr></table>" % (
        match.group(1), "".join(parts))


# Zero margins for nested composite layouts
MARGINS_NONE = (0, 0, 0, 0)

DIRECTION_DETENT_STEPS = (0.0, 5.0, 15.0, 30.0, 45.0)  # degrees; slider stops (0.0 = Off)
DIRECTION_DETENT_DEFAULT = 15.0
_DIAL_UNITS_PER_DEGREE = 2                                   # dial ints are half-degrees
_DIAL_MAX_UNITS = 360 * _DIAL_UNITS_PER_DEGREE - 1           # 719
_DRAG_UNITS_WHEN_OFF = _DIAL_UNITS_PER_DEGREE                # Off drags in whole degrees
_DIAL_ZERO_OFFSET_DEG = 270                                  # the QDial draws its value 0 at Down; maths angle 0 is Right


def make_double_spinbox(value, minimum, maximum, step=None, decimals=None, suffix="", tooltip=None):
    """Factory creating a configured QDoubleSpinBox."""
    spin = QtWidgets.QDoubleSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    if step is not None:
        spin.setSingleStep(step)
    if decimals is not None:
        spin.setDecimals(decimals)
    if suffix:
        spin.setSuffix(suffix)
    if tooltip:
        spin.setToolTip(tooltip)
    return spin


def make_int_spinbox(value, minimum, maximum, step=None, tooltip=None):
    """Factory creating a configured QSpinBox."""
    spin = QtWidgets.QSpinBox()
    spin.setRange(minimum, maximum)
    spin.setValue(value)
    if step is not None:
        spin.setSingleStep(step)
    if tooltip:
        spin.setToolTip(tooltip)
    return spin


def make_checkbox(text, checked=False, tooltip=None):
    """Factory creating a configured QCheckBox."""
    box = QtWidgets.QCheckBox(text)
    box.setChecked(checked)
    if tooltip:
        box.setToolTip(tooltip)
    return box


class LinkedSliderSpinBox(QtWidgets.QWidget):
    """A QSlider and QSpinBox two-way bound to the same integer value."""

    valueChanged = QtCore.Signal(int)

    def __init__(self, value, minimum, maximum, tooltip=None, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(*MARGINS_NONE)

        self.slider = make_slider(minimum, maximum, value)

        self.spinbox = QtWidgets.QSpinBox()
        self.spinbox.setRange(minimum, maximum)
        self.spinbox.setValue(value)
        if tooltip:
            self.spinbox.setToolTip(tooltip)

        layout.addWidget(self.slider)
        layout.addWidget(self.spinbox)

        self.slider.valueChanged.connect(self.spinbox.setValue)
        self.spinbox.valueChanged.connect(self.slider.setValue)
        self.slider.valueChanged.connect(self.valueChanged.emit)

    def value(self):
        return self.spinbox.value()

    def setValue(self, v):
        self.spinbox.setValue(v)


class DirectionDial(QtWidgets.QWidget):
    """A direction QDial that snaps to a user-chosen step ("detent").

    The detent steps are Off, 5, 15, 30 and 45 degrees (Off drags in whole degrees). The
    dial's integer unit is half a degree, so every step is a whole number of units and the
    manual angle field can hold half-degree values. Angles are maths angles: 0 = Right,
    counter-clockwise (90 = Up, 180 = Left, 270 = Down). degrees() / setDegrees() are the
    only API callers should use.
    """

    degreesChanged = QtCore.Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._step_units = int(DIRECTION_DETENT_DEFAULT * _DIAL_UNITS_PER_DEGREE)
        self._setting_degrees = False

        self.dial = QtWidgets.QDial()
        self.dial.setRange(0, _DIAL_MAX_UNITS)
        self.dial.setWrapping(True)
        self.dial.setNotchesVisible(True)
        self.dial.setToolTip(rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Drag to set the direction; snaps to the Snap step.")))

        self.angle_input = make_double_spinbox(
            0.0, 0.0, 359.5, step=1.0, decimals=1, suffix="°",
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
                "NestingPanel", "Type an exact direction in degrees (0 = Right, 90 = Up, 180 = Left, 270 = Down).")))
        self.angle_input.setWrapping(True)
        self.angle_input.setKeyboardTracking(False)

        self.step_slider = make_slider(
            0, len(DIRECTION_DETENT_STEPS) - 1, 0, ticks=True,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Detent step: Off, 5, 15, 30 or 45 degrees.")))
        self.step_label = QtWidgets.QLabel()
        self.step_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        self.step_label.setFixedWidth(self.step_label.fontMetrics().horizontalAdvance("45°") + 6)

        grid = QtWidgets.QGridLayout()
        grid.setContentsMargins(*MARGINS_NONE)
        grid.setColumnStretch(1, 1)
        grid.addWidget(QtWidgets.QLabel(QT_TRANSLATE_NOOP("NestingPanel", "Angle:")), 0, 0)
        grid.addWidget(self.angle_input, 0, 1, 1, 2)          # spans the slider + value columns
        grid.addWidget(QtWidgets.QLabel(QT_TRANSLATE_NOOP("NestingPanel", "Snap:")), 1, 0)
        grid.addWidget(self.step_slider, 1, 1)
        grid.addWidget(self.step_label, 1, 2)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(*MARGINS_NONE)
        layout.addWidget(self.dial, 0, QtCore.Qt.AlignHCenter)
        layout.addLayout(grid)

        self.dial.valueChanged.connect(self._on_dial_changed)
        self.step_slider.valueChanged.connect(self._on_step_slider_changed)
        self.angle_input.valueChanged.connect(self._on_angle_input_changed)
        self.setDetentStep(DIRECTION_DETENT_DEFAULT)
        self.angle_input.blockSignals(True)
        self.angle_input.setValue(self.degrees())
        self.angle_input.blockSignals(False)

    def degrees(self):
        """The direction as a maths angle: 0 = Right, counter-clockwise, 90 = Up."""
        raw_deg = self.dial.value() / _DIAL_UNITS_PER_DEGREE
        deg = (_DIAL_ZERO_OFFSET_DEG - raw_deg) % 360
        return 0.0 if deg == 0.0 else deg

    def setDegrees(self, degrees):
        """Set the direction exactly (no snapping: a restored value must round-trip)."""
        raw_deg = (_DIAL_ZERO_OFFSET_DEG - float(degrees)) % 360
        units = int(round(raw_deg * _DIAL_UNITS_PER_DEGREE)) % (_DIAL_MAX_UNITS + 1)
        old_val = self.dial.value()
        self._setting_degrees = True
        try:
            self.dial.setValue(units)
        finally:
            self._setting_degrees = False
        if old_val == units:
            self.angle_input.blockSignals(True)
            self.angle_input.setValue(self.degrees())
            self.angle_input.blockSignals(False)

    def detentStep(self):
        return float(DIRECTION_DETENT_STEPS[self.step_slider.value()])

    def setDetentStep(self, degrees):
        """Set the snap step to the nearest allowed stop."""
        index = min(range(len(DIRECTION_DETENT_STEPS)),
                    key=lambda i: abs(DIRECTION_DETENT_STEPS[i] - float(degrees)))
        if self.step_slider.value() != index:
            self.step_slider.setValue(index)
        self._on_step_slider_changed(index)

    def setDisabled(self, disabled):
        super().setDisabled(disabled)
        self.dial.setDisabled(disabled)
        self.step_slider.setDisabled(disabled)
        self.angle_input.setDisabled(disabled)

    def setEnabled(self, enabled):
        super().setEnabled(enabled)
        self.dial.setEnabled(enabled)
        self.step_slider.setEnabled(enabled)
        self.angle_input.setEnabled(enabled)

    def _on_step_slider_changed(self, index):
        step = DIRECTION_DETENT_STEPS[index]
        self._step_units = _DRAG_UNITS_WHEN_OFF if step == 0.0 else int(round(step * _DIAL_UNITS_PER_DEGREE))
        self.dial.setSingleStep(self._step_units)
        self.dial.setPageStep(self._step_units)
        self.dial.setNotchTarget(max(self._step_units, 1))
        if step == 0.0:
            self.step_label.setText(QT_TRANSLATE_NOOP("NestingPanel", "Off"))
        else:
            self.step_label.setText(f"{step:g}°")

    def _on_angle_input_changed(self, value):
        if self._setting_degrees:
            return
        self.setDegrees(value)

    def _on_dial_changed(self, units):
        # Snapping stays in raw units: 270 and 360 are both multiples of 5, 15, 30 and 45,
        # so a snapped raw angle is a snapped maths angle without conversion before snapping.
        if not self._setting_degrees:
            # Snap to the nearest multiple of the step; wrap 360° back to 0°.
            snapped = int(round(units / self._step_units)) * self._step_units
            snapped %= (_DIAL_MAX_UNITS + 1)
            if snapped != units:
                self.dial.setValue(snapped)   # re-enters this handler once, then is a no-op
                return
        self.angle_input.blockSignals(True)
        self.angle_input.setValue(self.degrees())
        self.angle_input.blockSignals(False)
        self.degreesChanged.emit(self.degrees())


class CollapsibleSection(QtWidgets.QWidget):
    """A collapsible section widget with a styled toggle button header and QFormLayout content area."""

    def __init__(self, title="", expanded=True, parent=None):
        super().__init__(parent)

        self.main_layout = QtWidgets.QVBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 8)
        self.main_layout.setSpacing(0)

        self.toggle_button = QtWidgets.QToolButton()
        self.toggle_button.setText(title)
        self.toggle_button.setCheckable(True)
        self.toggle_button.setChecked(expanded)
        self.toggle_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self.toggle_button.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self.toggle_button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)

        self.toggle_button.setStyleSheet("""
            QToolButton {
                background-color: rgba(128, 128, 128, 40);
                border: 1px solid palette(mid);
                border-radius: 4px;
                font-weight: bold;
                padding: 6px;
                text-align: left;
            }
            QToolButton:hover {
                background-color: palette(highlight);
                color: palette(highlighted-text);
                border-color: palette(highlight);
            }
            QToolButton:checked {
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
                background-color: rgba(128, 128, 128, 60);
            }
        """)

        # Parent it now: setVisible(True) on a parentless widget makes it a
        # top-level window, which flashes on screen until a layout reparents it.
        self.content_area = QtWidgets.QWidget(self)
        self.content_area.setObjectName("content_area")
        self.content_area.setStyleSheet("""
            QWidget#content_area {
                background-color: transparent;
                border: 1px solid palette(mid);
                border-top: none;
                border-bottom-left-radius: 4px;
                border-bottom-right-radius: 4px;
            }
        """)
        self.content_area.setVisible(expanded)

        self.content_layout = QtWidgets.QFormLayout(self.content_area)
        self.content_layout.setContentsMargins(12, 10, 12, 10)
        self.content_layout.setSpacing(8)

        self.main_layout.addWidget(self.toggle_button)
        self.main_layout.addWidget(self.content_area)

        self.toggle_button.clicked.connect(self.toggle)

    def toggle(self, checked=None):
        if checked is None:
            checked = self.toggle_button.isChecked()
        if checked:
            self.toggle_button.setArrowType(QtCore.Qt.DownArrow)
            self.content_area.setVisible(True)
        else:
            self.toggle_button.setArrowType(QtCore.Qt.RightArrow)
            self.content_area.setVisible(False)

    def setExpanded(self, expanded):
        self.toggle_button.setChecked(expanded)
        self.toggle(expanded)

    def addRow(self, label, widget=None):
        if widget is not None:
            self.content_layout.addRow(label, widget)
        else:
            self.content_layout.addRow(label)


def QT_TRANSLATE_NOOP(context, text):
    """
    Marks a string literal for translation extraction tools (lupdate-style).

    This is a true no-op: it always returns the original source text unchanged.
    Actual translation happens later, at display time, via FreeCAD's own Qt
    translation machinery (the .qm catalog loaded through addLanguagePath).
    Translating eagerly here would bake in whatever language was active at
    call time and break code that compares widget text against the English
    source literal (e.g. currentText() == "Physics").
    """
    return text




def show_warning_dialog(parent, title, text, informative_text=None, buttons=None):
    """
    Displays a warning message dialog.

    Args:
        parent (QWidget or None): Parent widget for the dialog.
        title (str): Window title.
        text (str): Main warning message.
        informative_text (str, optional): Additional guidance or steps.
        buttons (QMessageBox.StandardButtons, optional): Buttons to show. Defaults to Ok.
    """
    msg_box = QtWidgets.QMessageBox(parent) if parent is not None else QtWidgets.QMessageBox()
    msg_box.setIcon(QtWidgets.QMessageBox.Warning)
    msg_box.setWindowTitle(title)
    msg_box.setText(text)
    if informative_text:
        msg_box.setInformativeText(informative_text)
    if buttons is not None:
        msg_box.setStandardButtons(buttons)
    else:
        msg_box.setStandardButtons(QtWidgets.QMessageBox.Ok)
    return msg_box.exec_()


def closest_angle_index(angles, target_angle):
    """Index of the entry in *angles* nearest to *target_angle*. Ties take the lower index."""
    if not angles:
        return 0
    return min(range(len(angles)), key=lambda i: abs(angles[i] - target_angle))
