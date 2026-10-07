# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/Tools/Nesting/ui_nesting.py

"""
This module contains the NestingPanel class, which defines the user interface
for the main nesting task panel.
"""

from PySide import QtCore, QtWidgets
import FreeCAD
import FreeCADGui
import os
from ...constants import (
    MINKOWSKI_ROTATION_PRESETS, PHYSICS_ROTATION_PRESETS, PREFS_PATH, PREF_SHEET_SEQUENCE, PROP_DEFLECTION_ANGLE, PROP_LABEL_SIZE,
    PROP_PART_SPACING, PROP_SIMPLIFICATION,
    SIM_SINGLE,
)
from ... import FONTS_DIR, DEFAULT_FONT
from freecad.nestingworkbench import nw_logger
from freecad.nestingworkbench.ui_helpers import (
    MARGINS_NONE,
    QT_TRANSLATE_NOOP,
    rich_tooltip,
    make_double_spinbox,
    make_int_spinbox,
    make_checkbox,
    make_slider,
    LinkedSliderSpinBox,
    DirectionDial,
    DIRECTION_DETENT_DEFAULT,
    CollapsibleSection,
    closest_angle_index,
)
from .algorithms.minkowski_engine import DEFAULT_CANDIDATE_SPACING
from .algorithms import sheet_sequence
from .worker_sizing import auto_core_count, balanced_worker_count, get_worker_override


_DEFAULTS = {
    "default_sheet_width": 800.0,
    "default_sheet_height": 800.0,
    "part_spacing": 6.35,
    "sheet_thickness": 19.0,
    "deflection_angle": 30.0,
    "verbose_logging": False,
    "rotation_angles": MINKOWSKI_ROTATION_PRESETS,
    "candidate_spacing": DEFAULT_CANDIDATE_SPACING,
    # Defaults from the 2026-09-03 GA sizing benchmark:
    # the cheapest configuration that is no worse than seeded greedy on every
    # benchmarked corpus. Heavier settings are opt-in presets, not the default.
    "ga_population": 1,
    "ga_generations": 1,
}

# (label, population, generations) — from the 2026-09-03 GA sizing benchmark.
# Re-run the sizing sweep before changing any number here.
_GA_PRESETS = [
    (QT_TRANSLATE_NOOP("NestingPanel", "Default (1 pop, 1 gen)"), 1, 1),
    (QT_TRANSLATE_NOOP("NestingPanel", "Balanced (10 pop, 10 gen)"), 10, 10),
    (QT_TRANSLATE_NOOP("NestingPanel", "Thorough (10 pop, 20 gen)"), 10, 20),
]
_GA_CUSTOM_LABEL = QT_TRANSLATE_NOOP("NestingPanel", "Custom")
_SIM_LABEL = QT_TRANSLATE_NOOP("NestingPanel", "Simulation:")
_SIM_TIP = QT_TRANSLATE_NOOP(
    "NestingPanel",
    "Draws the nesting while it runs, which is slower. Off draws only the result. "
    "Single draws one layout at a time. All draws every genetic-algorithm population "
    "member at once, one row each, while GA worker processes evaluate them; "
    "elsewhere it behaves like Single. Trial positions and master highlighting "
    "appear only with one worker process.")
_WORKERS_LABEL = QT_TRANSLATE_NOOP("NestingPanel", "GA worker processes:")
# {0} is the number of workers Auto will start for the current population, e.g. "Auto - (5)".
_WORKERS_AUTO_FMT = QT_TRANSLATE_NOOP("NestingPanel", "Auto - ({0})")
_WORKERS_TIP = QT_TRANSLATE_NOOP(
    "NestingPanel",
    "How many processes evaluate the genetic algorithm's population at once. "
    "Auto shows the number it will use: one per physical CPU core and at most one per "
    "population member, fewer if that fills every batch of layouts equally. More "
    "workers than physical cores is usually slower. 1 evaluates one layout at a time, "
    "which also skips the worker start-up cost on small jobs.")

_GA_POP_TOOLTIP = QT_TRANSLATE_NOOP(
    "NestingPanel",
    "<b>Population Size:</b><br>"
    "Number of candidate layouts evaluated per generation.<br><br>"
    "<b>Default (1):</b> Single seeded placement; matches greedy on both benchmarked "
    "corpora in under 0.11s.<br>"
    "<b>Balanced (10):</b> paired with 10 generations, 1.02% better than greedy on "
    "rectangular parts and 0.79% on irregular parts, at 16.4x the Default's run time "
    "(1.84s).<br>"
    "<b>Larger (20-40):</b> measured no better than 10 on either corpus, and costs "
    "proportionally more time."
)

_GA_GEN_TOOLTIP = QT_TRANSLATE_NOOP(
    "NestingPanel",
    "<b>Generations:</b><br>"
    "Number of evolutionary optimization cycles.<br><br>"
    "<b>Default (1):</b> Single pass without evolutionary iteration.<br>"
    "<b>Balanced (10):</b> Paired with population 10; the winning layout appeared by "
    "generation 3 on average (generation 6 at the latest) on rectangular parts.<br>"
    "<b>Thorough (20):</b> best measured packing, 1.67% better than greedy on "
    "rectangular parts.<br>"
    "<b>Large (&gt;20):</b> fitness was identical at 20 and 40 generations on both "
    "corpora, and stagnation detection halts early anyway, so extra generations cost "
    "time without changing the result."
)

_GA_PRESET_TOOLTIP = QT_TRANSLATE_NOOP(
    "NestingPanel",
    "<b>Optimization Preset:</b><br>"
    "Predefined population and generation configurations, sized from benchmark runs.<br><br>"
    "<b>Default (1 pop, 1 gen):</b> Fast seeded placement; matched greedy exactly on "
    "both corpora, in 0.11s or less.<br>"
    "<b>Balanced (10 pop, 10 gen):</b> 1.02% better than greedy on rectangular parts "
    "and 0.79% on irregular parts, 16.4x the Default's run time (1.84s).<br>"
    "<b>Thorough (10 pop, 20 gen):</b> Best measured packing, 1.67% better than greedy "
    "on rectangular parts, 17.1x the Default's run time (1.92s)."
)

_GA_WARNING_TEXT = QT_TRANSLATE_NOOP(
    "NestingPanel",
    "Note: Populations > 40 or generations > 20 offer diminishing returns and substantially increase runtime."
)

class NestingPanel(QtWidgets.QWidget):
    """
    Defines the user interface for the main nesting task panel, including
    all input fields, buttons, and the table of shapes.
    """
    def __init__(self, parent=None):
        super(NestingPanel, self).__init__(parent)
        tr = QtWidgets.QApplication.translate
        nw_logger.info("NestingPanel initialized.")
        self.setWindowTitle(tr("NestingPanel", "Nesting Tool"))
        self.selected_shapes_to_process = []
        self.hidden_originals = []
        self.current_layout = None
        self.selected_font_path = ""
        self.rotation_angles = _DEFAULTS["rotation_angles"]
        self._setup_ui()
        self.set_default_font()
    
    def accept(self):
        """Called when the user clicks Standard Button OK / Apply."""
        if hasattr(self, 'controller'):
            self.controller.finalize_job()
            self.controller.on_panel_closed()
        return True

    def reject(self):
        """Called when the user clicks Standard Button Cancel / Close."""
        if hasattr(self, 'controller'):
            self.controller.cancel_job()
            self.controller.on_panel_closed()
            
        # Also ensure visibility is restored if controller didn't fully run
        for obj in self.hidden_originals:
             if hasattr(obj, "ViewObject"):
                 obj.ViewObject.Visibility = True
                 
        return True

    def _build_algorithm_group(self):
        """Builds the Nesting Settings group: the algorithm dropdown, each algorithm's
        parameters page (only the selected one is shown), and the shared Advanced section."""
        tr = QtWidgets.QApplication.translate
        group = QtWidgets.QGroupBox(tr("NestingPanel", "Nesting Settings"))
        form_layout = QtWidgets.QFormLayout()

        # The Advanced section places widgets the Minkowski page creates, so it comes after.
        self.minkowski_settings_group = self._build_minkowski_group()
        self.physics_settings_group = self._build_physics_group()
        self.advanced_section = self._build_advanced_section()

        self.algorithm_dropdown = QtWidgets.QComboBox()
        self.algorithm_dropdown.addItems([
            QT_TRANSLATE_NOOP("NestingPanel", "Minkowski"),
            QT_TRANSLATE_NOOP("NestingPanel", "Physics"),
        ])
        self.algorithm_dropdown.setCurrentIndex(0)
        self.algorithm_dropdown.setToolTip(rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
            "NestingPanel",
            "Chooses the placement method.\n"
            "<b>Minkowski:</b> fits parts edge to edge from precomputed outlines.\n"
            "<b>Physics:</b> drops parts under simulated gravity.")))
        self.algorithm_dropdown.currentTextChanged.connect(self._on_algorithm_change)

        form_layout.addRow(tr("NestingPanel", "Nesting Algorithm:"), self.algorithm_dropdown)
        form_layout.addRow(self.minkowski_settings_group)
        form_layout.addRow(self.physics_settings_group)
        form_layout.addRow(self.advanced_section)
        group.setLayout(form_layout)
        return group

    def _build_sheet_and_boundary_inputs(self):
        """Builds sheet dimension and boundary resolution inputs inside a group box."""
        tr = QtWidgets.QApplication.translate
        group = QtWidgets.QGroupBox(tr("NestingPanel", "Sheet Setup"))
        form_layout = QtWidgets.QFormLayout()

        self.sheet_width_input = make_double_spinbox(
            _DEFAULTS["default_sheet_width"], 1, 10000,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Width of the stock sheet, in mm."))
        )
        self.sheet_height_input = make_double_spinbox(
            _DEFAULTS["default_sheet_height"], 1, 10000,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Height of the stock sheet, in mm."))
        )
        self.sheet_thickness_input = make_double_spinbox(
            _DEFAULTS["sheet_thickness"], 0.1, 1000,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Thickness of the stock sheet, in mm."))
        )
        self.sheet_flip_combo = QtWidgets.QComboBox()
        self.sheet_flip_combo.addItem(tr("NestingPanel", "None (one side)"), "none")
        self.sheet_flip_combo.addItem(tr("NestingPanel", "About X (top edge to bottom edge)"), "x")
        self.sheet_flip_combo.addItem(tr("NestingPanel", "About Y (left edge to right edge)"), "y")
        self.sheet_flip_combo.setToolTip(rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
            "NestingPanel",
            "Turn this sheet over after the first side is cut, to machine its back. Library sheets set this in the library; Custom sheets set it here.")))
        self.part_spacing_input = make_double_spinbox(
            _DEFAULTS["part_spacing"], 0, 1000,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
                "NestingPanel",
                "Minimum gap kept between nested parts, in mm.\n"
                "0 lets parts touch."))
        )

        self.deflection_input = make_double_spinbox(
            _DEFAULTS["deflection_angle"], 1, 90,
            step=1, decimals=0, suffix="°",
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
                "NestingPanel",
                "<b>Curve Angle (Tessellation Quality):</b><br>"
                "Maximum angular deviation when approximating curves.<br><br>"
                "<b>Smaller (5-10°):</b> Smoother curves, more points, slower.<br>"
                "<b>Larger (20-45°):</b> Coarser curves, fewer points, faster.<br><br>"
                "<i>Tip: 10° is good for most parts. Use 5° for precision, 30°+ for speed.</i>"
            ))
        )

        self.simplification_input = make_double_spinbox(
            1.0, 0.001, 10.0,
            step=0.1, decimals=3,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
                "NestingPanel",
                "<b>Simplification (Point Reduction):</b><br>"
                "Tolerance (mm) for removing redundant boundary points.<br><br>"
                "<b>Smaller (0.1-0.5):</b> More detailed boundaries, slower nesting.<br>"
                "<b>Larger (1.0-5.0):</b> Simpler boundaries, faster nesting.<br><br>"
                "<i>Tip: Set this to your machine's precision tolerance (e.g., 1mm for routers).</i>"
            ))
        )

        self.sheet_combo = QtWidgets.QComboBox()
        self.sheet_combo.setToolTip(rich_tooltip(
            "NestingPanel",
            QT_TRANSLATE_NOOP(
                "NestingPanel",
                "Pick a sheet from the sheet library to fill in its size and thickness.\n"
                "Those fields are then locked, so change them in the library.\n"
                "Custom lets you type them.",
            ),
        ))
        self.sheet_library_button = QtWidgets.QToolButton()
        self.sheet_library_button.setText(tr("NestingPanel", "Library…"))
        self.sheet_library_button.setToolTip(rich_tooltip(
            "NestingPanel",
            QT_TRANSLATE_NOOP(
                "NestingPanel",
                "Open the sheet library to add or edit sheets.",
            ),
        ))
        self.sheet_library_button.clicked.connect(self._open_sheet_library)
        self.sheet_combo.currentIndexChanged.connect(self._on_library_sheet_changed)

        sheet_row = QtWidgets.QHBoxLayout()
        sheet_row.setContentsMargins(*MARGINS_NONE)
        sheet_row.addWidget(self.sheet_combo, 1)
        sheet_row.addWidget(self.sheet_library_button)
        self._sheet_library = None
        self._resolved_sheets = {}

        self.sheet_material_label = QtWidgets.QLabel("—")
        self.sheet_material_label.setToolTip(rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
            "NestingPanel",
            "Material of the library sheet. Shows — for a Custom size or a sheet with no material.")))

        self._sheet_rows = [
            sheet_sequence.custom_row(_DEFAULTS["default_sheet_width"], _DEFAULTS["default_sheet_height"], _DEFAULTS["sheet_thickness"])
        ]
        self._updating_editor = False

        self.sheet_list = QtWidgets.QListWidget()
        self.sheet_list.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        self.sheet_list.setFixedHeight(self.sheet_list.fontMetrics().height() * 5 + 16)
        self.sheet_list.setToolTip(rich_tooltip(
            "NestingPanel",
            QT_TRANSLATE_NOOP(
                "NestingPanel",
                "Sheets are used in this order. The last sheet repeats when more are needed.\n"
                "'+ back' means that sheet is turned over to machine its back: the layout shows its back side above it, and Create CAM Job makes a second job.",
            ),
        ))
        self.sheet_list.currentRowChanged.connect(self._on_sheet_list_row_changed)

        self.sheet_add_button = QtWidgets.QToolButton()
        self.sheet_add_button.setText(tr("NestingPanel", "Add"))
        self.sheet_add_button.setToolTip(rich_tooltip(
            "NestingPanel",
            QT_TRANSLATE_NOOP("NestingPanel", "Add a copy of the selected sheet."),
        ))
        self.sheet_add_button.clicked.connect(self._on_sheet_add)

        self.sheet_remove_button = QtWidgets.QToolButton()
        self.sheet_remove_button.setText(tr("NestingPanel", "Remove"))
        self.sheet_remove_button.setToolTip(rich_tooltip(
            "NestingPanel",
            QT_TRANSLATE_NOOP("NestingPanel", "Remove the selected sheet from the sequence."),
        ))
        self.sheet_remove_button.clicked.connect(self._on_sheet_remove)

        self.sheet_up_button = QtWidgets.QToolButton()
        self.sheet_up_button.setText(tr("NestingPanel", "Up"))
        self.sheet_up_button.setToolTip(rich_tooltip(
            "NestingPanel",
            QT_TRANSLATE_NOOP("NestingPanel", "Move the selected sheet earlier in the sequence."),
        ))
        self.sheet_up_button.clicked.connect(self._on_sheet_up)

        self.sheet_down_button = QtWidgets.QToolButton()
        self.sheet_down_button.setText(tr("NestingPanel", "Down"))
        self.sheet_down_button.setToolTip(rich_tooltip(
            "NestingPanel",
            QT_TRANSLATE_NOOP("NestingPanel", "Move the selected sheet later in the sequence."),
        ))
        self.sheet_down_button.clicked.connect(self._on_sheet_down)

        button_row = QtWidgets.QHBoxLayout()
        button_row.setContentsMargins(*MARGINS_NONE)
        button_row.addWidget(self.sheet_add_button)
        button_row.addWidget(self.sheet_remove_button)
        button_row.addWidget(self.sheet_up_button)
        button_row.addWidget(self.sheet_down_button)
        button_row.addStretch()

        sheet_list_col = QtWidgets.QVBoxLayout()
        sheet_list_col.setContentsMargins(*MARGINS_NONE)
        sheet_list_col.addWidget(self.sheet_list)
        sheet_list_col.addLayout(button_row)

        form_layout.addRow(tr("NestingPanel", "Sheets:"), sheet_list_col)
        form_layout.addRow(tr("NestingPanel", "Sheet:"), sheet_row)
        form_layout.addRow(tr("NestingPanel", "Material:"), self.sheet_material_label)
        form_layout.addRow(tr("NestingPanel", "Sheet Width:"), self.sheet_width_input)
        form_layout.addRow(tr("NestingPanel", "Sheet Height:"), self.sheet_height_input)
        form_layout.addRow(tr("NestingPanel", "Sheet Thickness:"), self.sheet_thickness_input)
        form_layout.addRow(tr("NestingPanel", "Flip:"), self.sheet_flip_combo)
        form_layout.addRow(tr("NestingPanel", "Part Spacing:"), self.part_spacing_input)

        self.sheet_width_input.valueChanged.connect(self._on_sheet_spinbox_changed)
        self.sheet_height_input.valueChanged.connect(self._on_sheet_spinbox_changed)
        self.sheet_thickness_input.valueChanged.connect(self._on_sheet_spinbox_changed)
        self.sheet_flip_combo.currentIndexChanged.connect(self._on_sheet_spinbox_changed)

        curve_settings_layout = QtWidgets.QHBoxLayout()
        curve_settings_layout.addWidget(QtWidgets.QLabel(tr("NestingPanel", "Curve:")))
        curve_settings_layout.addWidget(self.deflection_input)
        curve_settings_layout.addWidget(QtWidgets.QLabel(tr("NestingPanel", "Simplify:")))
        curve_settings_layout.addWidget(self.simplification_input)
        form_layout.addRow(tr("NestingPanel", "Bounds Resolution:"), curve_settings_layout)

        group.setLayout(form_layout)
        self._refresh_sheet_list()
        self._load_row_into_editor(0)
        self._update_sheet_buttons()
        return group

    def _sheet_row_text(self, index):
        """Display text for row index in the sheet list."""
        tr = QtWidgets.QApplication.translate
        row = self._sheet_rows[index]
        name = row["sheet_name"] or tr("NestingPanel", "Custom")
        w = float(row["width"])
        h = float(row["height"])
        t = float(row["thickness"])
        text = f"{index + 1}. {name} — {w:g} × {h:g} × {t:g} mm"
        flip = row["flip"]
        if flip in ("x", "y"):
            text += tr("NestingPanel", " + back (flip %1)").replace("%1", flip.upper())
        if index == len(self._sheet_rows) - 1:
            text += tr("NestingPanel", " (repeats)")
        return text

    def _refresh_sheet_list(self):
        """Rewrites every item's text and updates item count to match self._sheet_rows."""
        selected = self.selected_sheet_row()
        self.sheet_list.blockSignals(True)
        self.sheet_list.clear()
        for i in range(len(self._sheet_rows)):
            self.sheet_list.addItem(self._sheet_row_text(i))
        if self._sheet_rows:
            target = max(0, min(selected, len(self._sheet_rows) - 1))
            self.sheet_list.setCurrentRow(target)
        self.sheet_list.blockSignals(False)

    def _update_sheet_buttons(self):
        """Recomputes button enabled states based on current selection and row count."""
        count = len(self._sheet_rows)
        row = self.selected_sheet_row()
        self.sheet_add_button.setEnabled(count > 0 and 0 <= row < count)
        self.sheet_remove_button.setEnabled(count > 1 and 0 <= row < count)
        self.sheet_up_button.setEnabled(count > 1 and row > 0)
        self.sheet_down_button.setEnabled(count > 1 and 0 <= row < count - 1)

    def _selected_flip(self):
        return self.sheet_flip_combo.itemData(self.sheet_flip_combo.currentIndex()) or "none"

    def _load_row_into_editor(self, index):
        """Load sheet row index into combo and spinboxes."""
        if not (0 <= index < len(self._sheet_rows)):
            return
        row = self._sheet_rows[index]
        self._updating_editor = True
        try:
            self.sheet_combo.blockSignals(True)
            sheet_id = row.get("sheet_id", "")
            if sheet_id and sheet_id in self._resolved_sheets:
                combo_idx = self.sheet_combo.findData(sheet_id)
                self.sheet_combo.setCurrentIndex(combo_idx if combo_idx >= 0 else 0)
                self.apply_library_sheet(self._resolved_sheets[sheet_id])
            else:
                self.sheet_combo.setCurrentIndex(0)
                self.sheet_width_input.setValue(float(row["width"]))
                self.sheet_height_input.setValue(float(row["height"]))
                self.sheet_thickness_input.setValue(float(row["thickness"]))
                flip_idx = self.sheet_flip_combo.findData(row["flip"])
                self.sheet_flip_combo.setCurrentIndex(flip_idx if flip_idx >= 0 else 0)
                self.release_library_sheet()
            self.sheet_combo.blockSignals(False)
        finally:
            self._updating_editor = False

    def _on_sheet_list_row_changed(self, row):
        if row >= 0:
            self._load_row_into_editor(row)
            self._update_sheet_buttons()

    def _on_sheet_spinbox_changed(self, *args):
        if self._updating_editor:
            return
        # write the value into the selected row only when the combo is on Custom
        combo_id = self.sheet_combo.itemData(self.sheet_combo.currentIndex()) or ""
        if combo_id:
            return
        row_idx = self.selected_sheet_row()
        if not (0 <= row_idx < len(self._sheet_rows)):
            return
        self._sheet_rows[row_idx] = sheet_sequence.custom_row(
            self.sheet_width_input.value(),
            self.sheet_height_input.value(),
            self.sheet_thickness_input.value(),
            self._selected_flip(),
        )
        item = self.sheet_list.item(row_idx)
        if item is not None:
            item.setText(self._sheet_row_text(row_idx))

    def _on_sheet_add(self):
        sel = self.selected_sheet_row()
        if not (0 <= sel < len(self._sheet_rows)):
            sel = len(self._sheet_rows) - 1
        new_row = dict(self._sheet_rows[sel])
        insert_idx = sel + 1
        self._sheet_rows.insert(insert_idx, new_row)
        self.set_sheet_sequence(self._sheet_rows, selected=insert_idx)

    def _on_sheet_remove(self):
        if len(self._sheet_rows) <= 1:
            return
        sel = self.selected_sheet_row()
        if not (0 <= sel < len(self._sheet_rows)):
            return
        del self._sheet_rows[sel]
        new_sel = min(sel, len(self._sheet_rows) - 1)
        self.set_sheet_sequence(self._sheet_rows, selected=new_sel)

    def _on_sheet_up(self):
        sel = self.selected_sheet_row()
        if sel <= 0:
            return
        self._sheet_rows[sel - 1], self._sheet_rows[sel] = self._sheet_rows[sel], self._sheet_rows[sel - 1]
        self.set_sheet_sequence(self._sheet_rows, selected=sel - 1)

    def _on_sheet_down(self):
        sel = self.selected_sheet_row()
        if sel < 0 or sel >= len(self._sheet_rows) - 1:
            return
        self._sheet_rows[sel], self._sheet_rows[sel + 1] = self._sheet_rows[sel + 1], self._sheet_rows[sel]
        self.set_sheet_sequence(self._sheet_rows, selected=sel + 1)

    def selected_sheet_row(self):
        """Current selected row index in the sheet list (0-based), or 0."""
        row = self.sheet_list.currentRow()
        return max(0, row) if self._sheet_rows else 0

    def sheet_sequence(self):
        """Copies of the rows, in order."""
        return [dict(r) for r in self._sheet_rows]

    def set_sheet_sequence(self, rows, selected=0):
        """Replace every row (non-empty list), select *selected* (clamped), load it
        into the editor, refresh the list and the buttons."""
        if not rows:
            raise ValueError("sheet sequence must be a non-empty list")
        self._sheet_rows = [dict(r) for r in rows]
        target = max(0, min(selected, len(self._sheet_rows) - 1))
        self.sheet_list.blockSignals(True)
        self.sheet_list.clear()
        for i in range(len(self._sheet_rows)):
            self.sheet_list.addItem(self._sheet_row_text(i))
        self.sheet_list.setCurrentRow(target)
        self.sheet_list.blockSignals(False)
        self._load_row_into_editor(target)
        self._update_sheet_buttons()

    def _open_sheet_library(self):
        """Open the Library window on the Sheets tab, then refresh the Sheet combo."""
        from ...Tools.Library.ui_library import LibraryDialog
        LibraryDialog(self, tab="sheets").exec_()
        converted = self.populate_library_sheets()
        if converted:
            rows_str = ", ".join(str(r) for r in converted)
            self.log_message(
                f"Sheet row(s) {rows_str} are no longer in the sheet library; "
                f"they are now Custom with their last size.",
                level="warning")

    def _reconcile_rows_with_library(self):
        """Reconcile all sheet rows with current library state. Returns 1-based converted row numbers."""
        converted = []
        for i, row in enumerate(self._sheet_rows):
            sheet_id = row.get("sheet_id", "")
            if not sheet_id:
                continue
            record = self._resolved_sheets.get(sheet_id)
            is_selectable = False
            if record is not None:
                combo_idx = self.sheet_combo.findData(sheet_id)
                if combo_idx > 0:
                    is_selectable = True
            if is_selectable:
                self._sheet_rows[i] = sheet_sequence.library_row(record)
            else:
                self._sheet_rows[i] = sheet_sequence.custom_row(
                    row["width"], row["height"], row["thickness"], row["flip"]
                )
                converted.append(i + 1)
        return converted

    def populate_library_sheets(self, select_id=None):
        """Refill the Sheet combo from disk, reconcile sequence rows, and reload editor.

        Returns the list of 1-based row numbers that were converted to Custom.
        """
        from ...sheet_library import model, store
        tr = QtWidgets.QApplication.translate
        self._sheet_library = store.load_library()
        combo = self.sheet_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(tr("NestingPanel", "Custom"), "")
        hidden = []
        rows = model.sheet_rows(self._sheet_library)
        self._resolved_sheets = {sid: rec for _d, sid, _n, rec, err in rows if err is None}
        for depth, sheet_id, name, _sheet, error in rows:
            if error:
                hidden.append(f"'{name}' ({error})")
            else:
                combo.addItem(name, sheet_id)
        combo.blockSignals(False)

        if hidden:
            nw_logger.warn(f"Sheet library: {len(hidden)} sheet(s) left out of the picker: {'; '.join(hidden)}")

        converted = self._reconcile_rows_with_library()
        self._refresh_sheet_list()
        self._load_row_into_editor(self.selected_sheet_row())
        self._update_sheet_buttons()
        return converted

    def apply_library_sheet(self, sheet):
        """Show a library sheet and lock what it decides."""
        self.sheet_width_input.setValue(sheet["width"])
        self.sheet_height_input.setValue(sheet["height"])
        self.sheet_thickness_input.setValue(sheet["thickness"])
        flip_idx = self.sheet_flip_combo.findData(sheet["flip"])
        self.sheet_flip_combo.setCurrentIndex(flip_idx if flip_idx >= 0 else 0)
        self.sheet_material_label.setText(sheet["material"] or "—")
        for widget in (self.sheet_width_input, self.sheet_height_input, self.sheet_thickness_input, self.sheet_flip_combo):
            widget.setEnabled(False)

    def release_library_sheet(self):
        """Custom: give width, height, thickness and flip back to the user; values are kept."""
        for widget in (self.sheet_width_input, self.sheet_height_input, self.sheet_thickness_input, self.sheet_flip_combo):
            widget.setEnabled(True)
        self.sheet_material_label.setText("—")

    def _on_library_sheet_changed(self, index):
        if self._updating_editor:
            return
        sheet_id = self.sheet_combo.itemData(index) or ""
        row_idx = self.selected_sheet_row()
        if not (0 <= row_idx < len(self._sheet_rows)):
            return
        if not sheet_id:
            self._sheet_rows[row_idx] = sheet_sequence.custom_row(
                self.sheet_width_input.value(),
                self.sheet_height_input.value(),
                self.sheet_thickness_input.value(),
                self._selected_flip(),
            )
            self.release_library_sheet()
        else:
            record = self._resolved_sheets[sheet_id]
            self._sheet_rows[row_idx] = sheet_sequence.library_row(record)
            self.apply_library_sheet(record)
        item = self.sheet_list.item(row_idx)
        if item is not None:
            item.setText(self._sheet_row_text(row_idx))

    def _build_minkowski_group(self):
        """Builds the Minkowski parameters page shown inside Nesting Settings."""
        tr = QtWidgets.QApplication.translate
        page = QtWidgets.QWidget()
        form_layout = QtWidgets.QFormLayout()
        form_layout.setContentsMargins(*MARGINS_NONE)
        form_layout.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)

        self.minkowski_direction_dial = DirectionDial()

        self.minkowski_random_checkbox = make_checkbox(
            tr("NestingPanel", "Use Random Direction"),
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "If checked, each part will use a randomized placement weighting."))
        )
        self.minkowski_random_checkbox.stateChanged.connect(lambda state: self.minkowski_direction_dial.setDisabled(state))

        form_layout.addRow(tr("NestingPanel", "Nesting Direction:"), self.minkowski_direction_dial)
        form_layout.addRow(self.minkowski_random_checkbox)

        self.minkowski_population_size_input = make_int_spinbox(
            _DEFAULTS["ga_population"], 1, 500,
            tooltip=QtWidgets.QApplication.translate("NestingPanel", _GA_POP_TOOLTIP)
        )

        self.minkowski_generations_input = make_int_spinbox(
            _DEFAULTS["ga_generations"], 1, 1000,
            tooltip=QtWidgets.QApplication.translate("NestingPanel", _GA_GEN_TOOLTIP)
        )

        self.ga_preset_dropdown = QtWidgets.QComboBox()
        for label, _, _ in _GA_PRESETS:
            self.ga_preset_dropdown.addItem(
                QtWidgets.QApplication.translate("NestingPanel", label)
            )
        self.ga_preset_dropdown.addItem(
            QtWidgets.QApplication.translate("NestingPanel", _GA_CUSTOM_LABEL)
        )
        self.ga_preset_dropdown.setCurrentIndex(0)
        self.ga_preset_dropdown.setToolTip(
            QtWidgets.QApplication.translate("NestingPanel", _GA_PRESET_TOOLTIP)
        )

        self.ga_warning_label = QtWidgets.QLabel()
        self.ga_warning_label.setStyleSheet("color: #d97706; font-size: 11px;")
        self.ga_warning_label.setWordWrap(True)
        self.ga_warning_label.setVisible(False)

        self._updating_ga_preset = False

        def _on_ga_preset_changed(idx):
            if self._updating_ga_preset:
                return
            self._updating_ga_preset = True
            try:
                if 0 <= idx < len(_GA_PRESETS):
                    _, pop, gen = _GA_PRESETS[idx]
                    self.minkowski_population_size_input.setValue(pop)
                    self.minkowski_generations_input.setValue(gen)
            finally:
                self._updating_ga_preset = False
            _sync_ga_warning_and_preset()

        def _sync_ga_warning_and_preset():
            pop = self.minkowski_population_size_input.value()
            gen = self.minkowski_generations_input.value()

            # Soft warning on extreme settings
            # Thresholds from the GA sizing benchmark: fitness was identical at 20
            # and 40 generations on both corpora, and populations above 10 never
            # improved on it, so 20 generations is where extra work stops buying
            # anything. Keep this above the Thorough preset (10/20) or the panel
            # warns about its own recommendation.
            if pop > 40 or gen > 20:
                self.ga_warning_label.setText(
                    QtWidgets.QApplication.translate("NestingPanel", _GA_WARNING_TEXT)
                )
                self.ga_warning_label.setVisible(True)
            else:
                self.ga_warning_label.setVisible(False)

            if not self._updating_ga_preset:
                self._updating_ga_preset = True
                try:
                    matched_idx = -1
                    for i, (_, p, g) in enumerate(_GA_PRESETS):
                        if pop == p and gen == g:
                            matched_idx = i
                            break
                    if matched_idx != -1:
                        self.ga_preset_dropdown.setCurrentIndex(matched_idx)
                    else:
                        self.ga_preset_dropdown.setCurrentIndex(len(_GA_PRESETS))
                finally:
                    self._updating_ga_preset = False

        self.ga_preset_dropdown.currentIndexChanged.connect(_on_ga_preset_changed)
        self.minkowski_population_size_input.valueChanged.connect(lambda *a: _sync_ga_warning_and_preset())
        self.minkowski_generations_input.valueChanged.connect(lambda *a: _sync_ga_warning_and_preset())

        self.minkowski_compactness_input = make_double_spinbox(
            1.0, 0.0, 10.0, step=0.1,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
                "NestingPanel",
                "Shapes the leftover on the last sheet when layouts use the same number of sheets.\n"
                "<b>Higher:</b> favours one large offcut; parts may spread out.\n"
                "0 turns it off: parts pack tightest and the leftover may scatter.\n"
                "It never adds a sheet or leaves a part unplaced. Needs Generations and Population Size above 1.\n"
                "<b>Default:</b> 1.0."))
        )

        self.minkowski_rotation_steps_slider = make_slider(
            0, len(self.rotation_angles) - 1, 3,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
                "NestingPanel", "Sets the angle step parts may be rotated by while nesting.\n"
                "<b>Smaller steps:</b> tighter fits, slower nesting.")))
        self.minkowski_rotation_display_label = QtWidgets.QLabel("")
        self.minkowski_rotation_display_label.setFixedWidth(100)
        self.minkowski_rotation_steps_slider.valueChanged.connect(lambda: self._update_rotation_label())

        mink_rot_layout = QtWidgets.QHBoxLayout()
        mink_rot_layout.addWidget(self.minkowski_rotation_steps_slider)
        mink_rot_layout.addWidget(self.minkowski_rotation_display_label)
        mink_rot_layout.setContentsMargins(*MARGINS_NONE)

        form_layout.addRow(tr("NestingPanel", "Rotation Angle:"), mink_rot_layout)

        self.minkowski_candidate_spacing_input = make_double_spinbox(
            _DEFAULTS["candidate_spacing"], 1.0, 100.0, step=0.5,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
                "NestingPanel",
                "Sets distance between candidate points when checking part placement. A smaller value "
                "means more candidate points which makes it more likely to pack tightly. This can make "
                "nesting slower, especially for large parts.\n"
                "Left: 5 mm spacing. Right: 20 mm, which misses the tight corner."),
                image="NestingWorkbench_CandidateSpacing.svg"))

        translate = QtWidgets.QApplication.translate
        # Detected once: physical_core_count() shells out to sysctl on macOS.
        # When it falls back to logical processors, the coordinator warns at
        # run time (WPS-004).
        auto_cores, _ = auto_core_count()
        self.worker_processes_input = make_int_spinbox(
            0, 0, os.cpu_count() or 1,
            tooltip=translate("NestingPanel", _WORKERS_TIP))

        def update_auto_label(population):
            self.worker_processes_input.setSpecialValueText(
                translate("NestingPanel", _WORKERS_AUTO_FMT).format(
                    balanced_worker_count(population, auto_cores)))

        self.minkowski_population_size_input.valueChanged.connect(update_auto_label)
        update_auto_label(self.minkowski_population_size_input.value())

        ga_section = CollapsibleSection(tr("NestingPanel", "Genetic Algorithm"), expanded=True)
        ga_section.addRow(tr("NestingPanel", "Preset:"), self.ga_preset_dropdown)
        ga_section.addRow(tr("NestingPanel", "Generations:"), self.minkowski_generations_input)
        ga_section.addRow(tr("NestingPanel", "Population Size:"), self.minkowski_population_size_input)
        ga_section.addRow(tr("NestingPanel", "Compactness:"), self.minkowski_compactness_input)
        ga_section.addRow(self.ga_warning_label)
        form_layout.addRow(ga_section)

        page.setLayout(form_layout)
        return page

    def _build_physics_group(self):
        """Builds the Physics parameters page shown inside Nesting Settings."""
        tr = QtWidgets.QApplication.translate
        page = QtWidgets.QWidget()
        form_layout = QtWidgets.QFormLayout()
        form_layout.setContentsMargins(*MARGINS_NONE)
        form_layout.setLabelAlignment(QtCore.Qt.AlignLeft | QtCore.Qt.AlignTop)

        self.physics_direction_dial = DirectionDial()

        self.physics_random_checkbox = make_checkbox(
            tr("NestingPanel", "Use Random Direction"),
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "If checked, randomized gravity direction will be applied."))
        )
        self.physics_random_checkbox.stateChanged.connect(lambda state: self.physics_direction_dial.setDisabled(state))

        self.physics_step_size_input = make_double_spinbox(
            5.0, 0.1, 100.0,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Simulation step size in mm per iteration. Smaller values increase accuracy but take longer."))
        )

        self.physics_max_spawn_input = make_int_spinbox(
            100, 1, 1000,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Maximum number of attempts to find an initial collision-free position for each part."))
        )

        self.physics_max_nesting_steps_input = make_int_spinbox(
            500, 1, 5000,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Maximum physics simulation steps per cycle before freezing placement."))
        )

        self.physics_anneal_steps_input = make_int_spinbox(
            25, 0, 500,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Number of simulated annealing shake steps to settle parts into compact gaps."))
        )

        self.anneal_rotate_checkbox = make_checkbox(
            tr("NestingPanel", "Anneal Rotate"),
            checked=True,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Allow rotational perturbation during the annealing phase."))
        )

        self.anneal_translate_checkbox = make_checkbox(
            tr("NestingPanel", "Anneal Translate"),
            checked=True,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Allow translational perturbation during the annealing phase."))
        )

        self.anneal_random_shake_checkbox = make_checkbox(
            tr("NestingPanel", "Random Shake Direction"),
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Apply randomized perturbation direction during annealing instead of gravity direction."))
        )

        self.physics_anneal_rot_steps = make_int_spinbox(
            10, 0, 500,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Number of rotational perturbation iterations during annealing."))
        )

        self.physics_anneal_rot_curve_type = QtWidgets.QComboBox()
        self.physics_anneal_rot_curve_type.addItems(["Logarithmic", "Linear", "Power 1.5", "Quadratic", "Exponential"])
        self.physics_anneal_rot_curve_type.setToolTip(tr("NestingPanel", "Decay curve profile for rotational annealing temperature/amplitude over iterations."))

        self.physics_anneal_rot_min = make_double_spinbox(
            1.0, 0.0, 360.0,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Minimum rotation angle (degrees) applied at the end of annealing."))
        )

        self.physics_anneal_rot_max = make_double_spinbox(
            90.0, 0.0, 360.0,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Maximum rotation angle (degrees) applied at the start of annealing."))
        )

        self.physics_anneal_curve_type = QtWidgets.QComboBox()
        self.physics_anneal_curve_type.addItems(["Logarithmic", "Linear", "Power 1.5", "Quadratic", "Exponential"])
        self.physics_anneal_curve_type.setToolTip(tr("NestingPanel", "Decay curve profile for translation annealing amplitude over iterations."))

        self.physics_anneal_min_amp = make_double_spinbox(
            0.1, 0.0, 1000.0,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Minimum translation step (mm) applied at the end of annealing."))
        )

        self.physics_anneal_max_amp = make_double_spinbox(
            100.0, 0.0, 5000.0,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Maximum translation step (mm) applied at the start of annealing."))
        )

        self.physics_improvement_threshold_input = make_double_spinbox(
            0.01, 0.000001, 1.0, step=0.01, decimals=6,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Minimum score improvement required to reset simulation cycle. Prevents infinite loops from noise."))
        )

        self.physics_rotation_steps_slider = make_slider(
            0, len(PHYSICS_ROTATION_PRESETS) - 1, 1,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP(
                "NestingPanel", "Sets the angle step parts may be rotated by while nesting.\n"
                "<b>Smaller steps:</b> tighter fits, slower nesting.")))
        self.physics_rotation_display_label = QtWidgets.QLabel("")
        self.physics_rotation_display_label.setFixedWidth(120)
        self.physics_rotation_steps_slider.valueChanged.connect(lambda: self._update_rotation_label())

        phys_rot_layout = QtWidgets.QHBoxLayout()
        phys_rot_layout.addWidget(self.physics_rotation_steps_slider)
        phys_rot_layout.addWidget(self.physics_rotation_display_label)
        phys_rot_layout.setContentsMargins(*MARGINS_NONE)

        form_layout.addRow(tr("NestingPanel", "Gravity Direction:"), self.physics_direction_dial)
        form_layout.addRow(self.physics_random_checkbox)
        form_layout.addRow(tr("NestingPanel", "Step Size:"), self.physics_step_size_input)
        form_layout.addRow(tr("NestingPanel", "Max Spawn Attempts:"), self.physics_max_spawn_input)
        form_layout.addRow(tr("NestingPanel", "Max Nesting Steps:"), self.physics_max_nesting_steps_input)

        anneal_section = CollapsibleSection(tr("NestingPanel", "Annealing (Shake)"), expanded=True)
        anneal_section.addRow(self.anneal_rotate_checkbox)
        anneal_section.addRow(tr("NestingPanel", "Rotation Steps:"), phys_rot_layout)
        anneal_section.addRow(tr("NestingPanel", "Rot Anneal Steps:"), self.physics_anneal_rot_steps)
        anneal_section.addRow(tr("NestingPanel", "Rot Curve Type:"), self.physics_anneal_rot_curve_type)
        anneal_section.addRow(tr("NestingPanel", "Rot Min Angle:"), self.physics_anneal_rot_min)
        anneal_section.addRow(tr("NestingPanel", "Rot Max Angle:"), self.physics_anneal_rot_max)
        anneal_section.addRow(self.anneal_translate_checkbox)
        anneal_section.addRow(tr("NestingPanel", "Anneal Steps:"), self.physics_anneal_steps_input)
        anneal_section.addRow(tr("NestingPanel", "Improvement Threshold:"), self.physics_improvement_threshold_input)
        anneal_section.addRow(tr("NestingPanel", "Curve Type:"), self.physics_anneal_curve_type)
        anneal_section.addRow(tr("NestingPanel", "Min Amplitude:"), self.physics_anneal_min_amp)
        anneal_section.addRow(tr("NestingPanel", "Max Amplitude:"), self.physics_anneal_max_amp)
        anneal_section.addRow(self.anneal_random_shake_checkbox)
        form_layout.addRow(anneal_section)

        page.setLayout(form_layout)
        return page

    def _build_advanced_section(self):
        """Builds the one Advanced section. Simulation and Verbose Logging apply to every
        algorithm; the widgets in _minkowski_advanced_widgets are shown only while
        Minkowski is selected (_on_algorithm_change)."""
        tr = QtWidgets.QApplication.translate
        self.simulate_combo = QtWidgets.QComboBox()
        # Order must match SIM_OFF, SIM_SINGLE, SIM_ALL.
        self.simulate_combo.addItem(tr("NestingPanel", "Off"))
        self.simulate_combo.addItem(tr("NestingPanel", "Single"))
        self.simulate_combo.addItem(tr("NestingPanel", "All"))
        self.simulate_combo.setCurrentIndex(SIM_SINGLE)
        self.simulate_combo.setToolTip(rich_tooltip("NestingPanel", _SIM_TIP))

        self.verbose_logging_checkbox = make_checkbox(
            tr("NestingPanel", "Verbose Logging"),
            checked=_DEFAULTS["verbose_logging"],
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Enables detailed logging of the nesting process in the FreeCAD console."))
        )

        self.clear_cache_checkbox = make_checkbox(
            tr("NestingPanel", "Clear NFP Cache"),
            checked=False,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Forces recalculation of No-Fit Polygons. Slower, but resolves potential caching issues."))
        )

        spacing_label = QtWidgets.QLabel(tr("NestingPanel", "Candidate Spacing:"))
        workers_label = QtWidgets.QLabel(tr("NestingPanel", _WORKERS_LABEL))
        self._minkowski_advanced_widgets = [
            spacing_label, self.minkowski_candidate_spacing_input,
            workers_label, self.worker_processes_input,
            self.clear_cache_checkbox,
        ]

        section = CollapsibleSection(tr("NestingPanel", "Advanced"), expanded=False)
        section.addRow(tr("NestingPanel", _SIM_LABEL), self.simulate_combo)
        section.addRow(self.verbose_logging_checkbox)
        section.addRow(spacing_label, self.minkowski_candidate_spacing_input)
        section.addRow(workers_label, self.worker_processes_input)
        section.addRow(self.clear_cache_checkbox)
        return section

    def _build_font_layout(self):
        """Builds the font selection button and label layout."""
        tr = QtWidgets.QApplication.translate
        font_layout = QtWidgets.QHBoxLayout()
        self.font_select_button = QtWidgets.QPushButton(tr("NestingPanel", "Select Font"))
        self.font_label = QtWidgets.QLabel(tr("NestingPanel", "No Font Selected"))
        self.font_label.setWordWrap(True)
        font_layout.addWidget(self.font_select_button)
        font_layout.addWidget(self.font_label)
        return font_layout

    def _build_label_options_row(self):
        """Builds the label options row with size and height inputs."""
        tr = QtWidgets.QApplication.translate
        self.add_labels_checkbox = make_checkbox(
            tr("NestingPanel", "Add Identifier Labels"),
            checked=True,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Adds an identifier label to each nested part."))
        )

        self.label_size_input = make_double_spinbox(
            10.0, 1, 100,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "The text size for identifier labels in mm."))
        )

        self.label_height_input = make_double_spinbox(
            25.0, 0, 1000,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "The height (Z-offset) for the identifier labels."))
        )

        label_options_layout = QtWidgets.QHBoxLayout()
        label_options_layout.addWidget(self.add_labels_checkbox)
        label_options_layout.addWidget(QtWidgets.QLabel(tr("NestingPanel", "Size:")))
        label_options_layout.addWidget(self.label_size_input)
        label_options_layout.addWidget(QtWidgets.QLabel(tr("NestingPanel", "Height (Z):")))
        label_options_layout.addWidget(self.label_height_input)
        label_options_layout.addStretch()
        return label_options_layout

    def _build_display_options_row(self):
        """Builds the Show Bounds and Play sound row, shown for every algorithm."""
        tr = QtWidgets.QApplication.translate
        self.show_bounds_checkbox = make_checkbox(
            tr("NestingPanel", "Show Bounds"),
            checked=True,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Draws the sheet and part bounds in the 3D view while nesting."))
        )
        self.sound_checkbox = make_checkbox(
            tr("NestingPanel", "Play sound on completion"),
            checked=True,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Plays a sound when nesting finishes."))
        )
        row = QtWidgets.QHBoxLayout()
        row.addWidget(self.show_bounds_checkbox)
        row.addWidget(self.sound_checkbox)
        row.addStretch()
        return row

    def _build_parts_table_and_buttons(self, main_layout):
        """Builds the parts table and Add/Remove buttons."""
        tr = QtWidgets.QApplication.translate
        self.shape_table = QtWidgets.QTableWidget()
        self.shape_table.setColumnCount(5)
        self.shape_table.setHorizontalHeaderLabels([
            tr("NestingPanel", "Shape"),
            tr("NestingPanel", "Quantity"),
            tr("NestingPanel", "Rotations"),
            tr("NestingPanel", "Up Dir"),
            tr("NestingPanel", "Fill"),
        ])
        self.shape_table.horizontalHeader().setSectionResizeMode(3, QtWidgets.QHeaderView.ResizeToContents)

        self.add_parts_button = QtWidgets.QPushButton(tr("NestingPanel", "Add Selected"))
        self.remove_parts_button = QtWidgets.QPushButton(tr("NestingPanel", "Remove Selected"))

        table_button_layout = QtWidgets.QHBoxLayout()
        table_button_layout.addWidget(self.add_parts_button)
        table_button_layout.addWidget(self.remove_parts_button)

        main_layout.addWidget(self.shape_table)
        main_layout.addLayout(table_button_layout)

    def _build_action_buttons(self):
        """Builds the main Run Nesting and Cancel action buttons."""
        tr = QtWidgets.QApplication.translate
        self.nest_button = QtWidgets.QPushButton(tr("NestingPanel", "Run Nesting"))
        self.cancel_button = QtWidgets.QPushButton(tr("NestingPanel", "Cancel Nesting"))
        self.cancel_button.setEnabled(False)

        action_button_layout = QtWidgets.QHBoxLayout()
        action_button_layout.addWidget(self.nest_button)
        action_button_layout.addWidget(self.cancel_button)
        return action_button_layout

    def _build_progress_and_status(self, main_layout):
        """Builds the progress bar and status message label."""
        tr = QtWidgets.QApplication.translate
        self.progressBar = QtWidgets.QProgressBar()
        self.progressBar.setRange(0, 100)
        self.progressBar.setValue(0)
        self.progressBar.setTextVisible(True)
        self.progressBar.setVisible(False)

        self.status_label = QtWidgets.QLabel(tr("NestingPanel", "Select master shapes to nest."))
        self.status_label.setWordWrap(True)

        main_layout.addWidget(self.progressBar)
        main_layout.addWidget(self.status_label)

    def _setup_ui(self):
        tr = QtWidgets.QApplication.translate
        main_layout = QtWidgets.QVBoxLayout()
        form_layout = QtWidgets.QFormLayout()

        self.sheet_setup_group = self._build_sheet_and_boundary_inputs()
        form_layout.addRow(self.sheet_setup_group)

        self.algorithm_group = self._build_algorithm_group()
        form_layout.addRow(self.algorithm_group)

        font_layout = self._build_font_layout()
        form_layout.addRow(tr("NestingPanel", "Identifier Font:"), font_layout)

        label_options_layout = self._build_label_options_row()
        form_layout.addRow(label_options_layout)

        form_layout.addRow(self._build_display_options_row())
        main_layout.addLayout(form_layout)

        self._build_parts_table_and_buttons(main_layout)

        action_button_layout = self._build_action_buttons()
        main_layout.addLayout(action_button_layout)

        self._build_progress_and_status(main_layout)
        main_layout.addStretch()
        self.setLayout(main_layout)

        # Only after setLayout: until then the groups have no parent widget, and
        # setVisible(True) on a parentless widget pops it up as its own window.
        self._on_algorithm_change(self.algorithm_dropdown.currentText())

        # Connect signals
        def toggle_label_inputs(enabled):
            self.label_size_input.setEnabled(enabled)
            self.label_height_input.setEnabled(enabled)

        self.add_labels_checkbox.toggled.connect(toggle_label_inputs)
        toggle_label_inputs(self.add_labels_checkbox.isChecked())

        # Keep direction dial detent steps synchronized between pages
        def _sync_detent_mink_to_phys(val):
            step = self.minkowski_direction_dial.detentStep()
            if abs(self.physics_direction_dial.detentStep() - step) > 1e-4:
                self.physics_direction_dial.setDetentStep(step)

        def _sync_detent_phys_to_mink(val):
            step = self.physics_direction_dial.detentStep()
            if abs(self.minkowski_direction_dial.detentStep() - step) > 1e-4:
                self.minkowski_direction_dial.setDetentStep(step)

        self.minkowski_direction_dial.step_slider.valueChanged.connect(_sync_detent_mink_to_phys)
        self.physics_direction_dial.step_slider.valueChanged.connect(_sync_detent_phys_to_mink)

        # Connect the nesting controller
        from .nesting_controller import NestingController
        self.controller = NestingController(self)
        self.nest_button.clicked.connect(self.controller.execute_nesting)
        self.cancel_button.clicked.connect(self.controller.request_cancel)
        self.font_select_button.clicked.connect(self.select_font_file)
        self.show_bounds_checkbox.stateChanged.connect(self.controller.toggle_bounds_visibility)
        self.add_parts_button.clicked.connect(self.controller.add_selected_shapes)
        self.remove_parts_button.clicked.connect(self.controller.remove_selected_shapes)

        self.load_persisted_settings()
        self._update_rotation_label()
        self.controller.load_selection()

    def add_part_row(self, row_index, label, quantity=1, rotation_steps=4, override_rotation=False, 
                       up_vector=None, fill_sheet=False):
        """Helper function to create and populate a single row in the parts table."""
        tr = QtWidgets.QApplication.translate
        label_item = QtWidgets.QTableWidgetItem(label)
        label_item.setFlags(label_item.flags() & ~QtCore.Qt.ItemIsEditable)

        quantity_spinbox = make_int_spinbox(
            quantity, 1, 500,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Number of copies of this part to place."))
        )

        rotation_widget = QtWidgets.QWidget()
        rotation_layout = QtWidgets.QHBoxLayout(rotation_widget)
        rotation_layout.setContentsMargins(*MARGINS_NONE)

        override_checkbox = make_checkbox("", checked=override_rotation)
        override_checkbox.setToolTip(tr("NestingPanel", "Override global rotation steps for this part."))

        rotation_spinbox = make_int_spinbox(
            rotation_steps, 0, 360,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "Override global rotation steps for this part. 0 or 1 means no rotation."))
        )
        rotation_spinbox.setEnabled(override_rotation)
        override_checkbox.toggled.connect(rotation_spinbox.setEnabled)

        rotation_layout.addWidget(override_checkbox)
        rotation_layout.addWidget(rotation_spinbox)

        if up_vector is None:
            up_vector = FreeCAD.Vector(0, 0, 1)

        up_vector_widget = QtWidgets.QWidget()
        up_vector_layout = QtWidgets.QHBoxLayout(up_vector_widget)
        up_vector_layout.setContentsMargins(*MARGINS_NONE)

        tooltip = rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "The local axis of this part that points 'up' before projecting to 2D. Default (0, 0, 1) is the part's own Z axis."))
        up_x_spinbox = make_double_spinbox(up_vector.x, -1.0, 1.0, decimals=2, tooltip=tooltip)
        up_x_spinbox.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        up_y_spinbox = make_double_spinbox(up_vector.y, -1.0, 1.0, decimals=2, tooltip=tooltip)
        up_y_spinbox.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)
        up_z_spinbox = make_double_spinbox(up_vector.z, -1.0, 1.0, decimals=2, tooltip=tooltip)
        up_z_spinbox.setButtonSymbols(QtWidgets.QAbstractSpinBox.NoButtons)

        up_vector_layout.addWidget(up_x_spinbox)
        up_vector_layout.addWidget(up_y_spinbox)
        up_vector_layout.addWidget(up_z_spinbox)

        fill_checkbox = make_checkbox(
            "", checked=fill_sheet,
            tooltip=rich_tooltip("NestingPanel", QT_TRANSLATE_NOOP("NestingPanel", "The quantity is always placed. If checked, extra copies are then added to fill the remaining space."))
        )

        self.shape_table.setItem(row_index, 0, label_item)
        self.shape_table.setCellWidget(row_index, 1, quantity_spinbox)
        self.shape_table.setCellWidget(row_index, 2, rotation_widget)
        self.shape_table.setCellWidget(row_index, 3, up_vector_widget)
        self.shape_table.setCellWidget(row_index, 4, fill_checkbox)

    def select_font_file(self):
        """Opens a file dialog to let the user select a font file."""
        tr = QtWidgets.QApplication.translate
        default_font_dir = FONTS_DIR
        if not os.path.isdir(default_font_dir):
            default_font_dir = "" # Fallback if fonts dir doesn't exist

        file_dialog_result = QtWidgets.QFileDialog.getOpenFileName(
            self, 
            tr("NestingPanel", "Select Font File"), 
            default_font_dir, # Set the default directory
            tr("NestingPanel", "Font Files (*.ttf *.otf)")
        )
        font_path = file_dialog_result[0]

        if font_path:
            self.selected_font_path = font_path
            # Display just the filename for a cleaner UI
            self.font_label.setText(os.path.basename(font_path))

    def set_default_font(self):
        """Checks for and sets a default font on initialization."""
        try:
            default_font_path = DEFAULT_FONT

            if os.path.exists(default_font_path):
                self.selected_font_path = default_font_path
                self.font_label.setText(os.path.basename(default_font_path))
        except Exception as e:
            nw_logger.warn(f"[NestingPanel] Failed to set default font: {e}")

    def log_message(self, message, level="message"):
        """Displays a message in the status label and logs to the console."""
        try:
            self.status_label.setText(message)
        except RuntimeError as e:
            # The widget C++ object has been deleted (panel closed), but Python object persists.
            # We can just log to console and ignore the UI update.
            nw_logger.debug(f"[NestingPanel] status_label.setText skipped (widget deleted): {e}")

        if level == "warning":
            nw_logger.warn(message)
        else:
            nw_logger.info(message)
        
        # Process UI events to make sure the label updates immediately
        # We wrap this too, just in case
        try:
            FreeCADGui.updateGui()
        except RuntimeError as e:
            nw_logger.debug(f"[NestingPanel] updateGui skipped (GUI window closed): {e}")

    def load_persisted_settings(self):
        """Loads settings from FreeCAD preferences."""
        prefs = FreeCAD.ParamGet(PREFS_PATH)
        text = prefs.GetString(PREF_SHEET_SEQUENCE, "")
        if text:
            try:
                rows = sheet_sequence.from_json(text)
            except ValueError as e:
                nw_logger.warn(f"Invalid saved sheet sequence ({e}); using default Custom sheet.")
                rows = None
        else:
            rows = None

        if rows:
            self.set_sheet_sequence(rows)
            converted = self.populate_library_sheets()
            if converted:
                rows_str = ", ".join(str(r) for r in converted)
                self.log_message(
                    f"Sheet row(s) {rows_str} from last time are no longer in the sheet library; "
                    f"they are now Custom with their last size.",
                    level="warning",
                )
        else:
            self.populate_library_sheets()

        self.part_spacing_input.setValue(prefs.GetFloat(PROP_PART_SPACING, 6.35))
        self.label_size_input.setValue(prefs.GetFloat(PROP_LABEL_SIZE, 10.0))
        self.deflection_input.setValue(prefs.GetFloat(PROP_DEFLECTION_ANGLE, 30.0) or 30.0)
        self.simplification_input.setValue(prefs.GetFloat(PROP_SIMPLIFICATION, 1.0))
        self.minkowski_compactness_input.setValue(prefs.GetFloat("GACompactnessWeight", 1.0))
        self.verbose_logging_checkbox.setChecked(prefs.GetBool("VerboseLogging", False))
        self.worker_processes_input.setValue(get_worker_override())
        self.physics_improvement_threshold_input.setValue(prefs.GetFloat("PhysicsStabilityTolerance", 0.01))
        
        self.physics_anneal_curve_type.setCurrentText(prefs.GetString("PhysicsAnnealCurveType", "Logarithmic"))
        self.physics_anneal_min_amp.setValue(prefs.GetFloat("PhysicsAnnealMinAmp", 0.1))
        self.physics_anneal_max_amp.setValue(prefs.GetFloat("PhysicsAnnealMaxAmp", 100.0))
        
        self.physics_anneal_rot_steps.setValue(prefs.GetInt("PhysicsAnnealRotSteps", 10))
        self.physics_anneal_rot_curve_type.setCurrentText(prefs.GetString("PhysicsAnnealRotCurveType", "Logarithmic"))
        self.physics_anneal_rot_min.setValue(prefs.GetFloat("PhysicsAnnealRotMin", 1.0))
        self.physics_anneal_rot_max.setValue(prefs.GetFloat("PhysicsAnnealRotMax", 90.0))
        
        # Load Rotation Steps (Isolated)
        # Minkowski
        mink_rot_steps = prefs.GetInt("MinkowskiRotationSteps", 4) # Default 90 deg (4 steps)
        if mink_rot_steps > 0:
            target_angle = 360.0 / mink_rot_steps
            self.minkowski_rotation_steps_slider.setValue(
                closest_angle_index(self.rotation_angles, target_angle)
            )
            
        # Physics
        phys_rot_steps = prefs.GetInt("PhysicsRotationSteps", 4) # Default 90 deg (4 steps)
        if phys_rot_steps > 0:
            target_angle = 360.0 / phys_rot_steps
            self.physics_rotation_steps_slider.setValue(
                closest_angle_index(PHYSICS_ROTATION_PRESETS, target_angle)
            )

        detent = prefs.GetFloat("DirectionDetentStep", DIRECTION_DETENT_DEFAULT)
        self.minkowski_direction_dial.setDetentStep(detent)
        self.physics_direction_dial.setDetentStep(detent)
        
    def update_progress(self, current, total, message=None):
        """Updates the progress bar."""
        try:
            if total > 0:
                percentage = int((float(current) / float(total)) * 100)
                self.progressBar.setValue(percentage)
                self.progressBar.setVisible(True)
                
                if message:
                    self.progressBar.setFormat(f"%p% - {message}")
                else:
                    self.progressBar.setFormat("%p%")
                
                # Force UI update
                FreeCADGui.updateGui()
            else:
                self.progressBar.setValue(0)
                self.progressBar.setVisible(False)
        except RuntimeError as e:
            nw_logger.debug(f"[NestingPanel] update_progress widget deleted: {e}")
        except Exception as e:
            nw_logger.warn(f"UI Update Error: {e}")

    def _update_rotation_label(self):
        algo = self.algorithm_dropdown.currentText()
        
        if algo == "Physics":
            value = self.physics_rotation_steps_slider.value()
            angles = PHYSICS_ROTATION_PRESETS
            if value < len(angles):
                angle = angles[value]
                steps = int(360 / angle) if angle > 0 else 1
                self.physics_rotation_display_label.setText(f"{angle}° ({steps} steps)")
        else:
            value = self.minkowski_rotation_steps_slider.value()
            angles = self.rotation_angles
            if value < len(angles):
                angle = angles[value]
                self.minkowski_rotation_display_label.setText(f"{angle}°")

    def _on_algorithm_change(self, algo_name):
        """Handles switching between nesting algorithms."""
        is_minkowski = algo_name == "Minkowski"
        self.minkowski_settings_group.setVisible(is_minkowski)
        self.physics_settings_group.setVisible(algo_name == "Physics")
        for widget in self._minkowski_advanced_widgets:
            widget.setVisible(is_minkowski)
        # Ensure the rotation label/steps are immediately clarified for the new algorithm
        self._update_rotation_label()

    def reset_progress(self):
        """Resets and hides the progress bar."""
        try:
            self.progressBar.setValue(0)
            self.progressBar.setVisible(False)
        except RuntimeError as e:
            nw_logger.debug(f"[NestingPanel] reset_progress widget deleted: {e}")


