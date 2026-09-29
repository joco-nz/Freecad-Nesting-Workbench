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
from ...constants import *
from ... import FONTS_DIR, DEFAULT_FONT
from ...freecad_helpers import set_visibility

_MINKOWSKI_DIR_MAX = 359

# Direction naming and the default live in constants (DIRECTION_LABELS,
# DEFAULT_DIRECTION_DIAL) so NestingJob can share the default without importing
# this module. See the comment there for why the dial reading is not the
# compass bearing.
_DIRECTION_LABELS = DIRECTION_LABELS
_DEFAULT_DIRECTION_DIAL = DEFAULT_DIRECTION_DIAL

_DEFAULTS = {
    "sheet_width": 600.0,
    "sheet_height": 600.0,
    "part_spacing": 12.5,
    "sheet_thickness": 3.0,
    "deflection_angle": 30.0,
    "add_labels": False,
    "simulate_nesting": False,
    "verbose_logging": False,
    "performance_logging": False,
    "candidate_geometry_cache": CANDIDATE_GEOMETRY_CACHE_DEFAULT,
    "rotation_angles": MINKOWSKI_ROTATION_PRESETS,
}

class NestingPanel(QtWidgets.QWidget):
    """
    Defines the user interface for the main nesting task panel, including
    all input fields, buttons, and the table of shapes.
    """
    def __init__(self, parent=None):
        super(NestingPanel, self).__init__(parent)
        FreeCAD.Console.PrintMessage("NestingPanel initialized.\n")
        self.setWindowTitle("Nesting Tool")
        self.selected_shapes_to_process = []
        self.hidden_originals = []
        self.current_layout = None
        self.selected_font_path = ""
        # Re-entrancy guard for reject(). The in-panel "Cancel Nesting" button
        # calls controller.request_cancel(), which closes the dialog via
        # ui.reject() once the run is not in flight -- and that lands back
        # here, which calls request_cancel() again. Without this the pair
        # recurses until RecursionError. Set for the duration of the call only;
        # a second, genuine Cancel press later still gets through.
        self._rejecting = False
        self._setup_ui()
        self.set_default_font()
    
    def accept(self):
        """Called when the user clicks Standard Button OK / Apply."""
        if hasattr(self, 'controller'):
            self.controller.finalize_job()
        return True

    def reject(self):
        """Called when the user clicks Standard Button Cancel / Close.

        Routed through controller.request_cancel() rather than cancel_job()
        directly. Those are not interchangeable: request_cancel() sets
        cancel_requested, which is the only thing the running worker's
        _check_cancel consults, and it also unblocks the worker if it is waiting
        on a main-thread draw handover. Calling cancel_job() straight from here
        -- which is what this used to do -- tidies a job that does not exist yet
        and closes the panel, so the nesting run carried on invisibly to
        completion with nothing on screen to indicate it. The user was left
        looking at a closed dialog and a still-running nest.

        request_cancel() falls through to cancel_job() when no run is in flight,
        so the idle case still cleans up.
        """
        if self._rejecting:
            # Already unwinding a reject() that came through here from
            # request_cancel(). Re-entering would bounce between the two
            # forever; the outer call does the work.
            return True
        self._rejecting = True
        try:
            if hasattr(self, 'controller'):
                self.controller.request_cancel()

            # Also ensure visibility is restored if controller didn't fully run
            for obj in self.hidden_originals:
                 set_visibility(obj, True)
        finally:
            self._rejecting = False

        return True

    def _setup_ui(self):
        main_layout = QtWidgets.QVBoxLayout()
        form_layout = QtWidgets.QFormLayout()
        
        # Algorithm Selection
        self.algorithm_dropdown = QtWidgets.QComboBox()
        self.algorithm_dropdown.addItems(["Minkowski", "Physics"])
        self.algorithm_dropdown.setCurrentIndex(0) # Default to Minkowski
        self.algorithm_dropdown.currentTextChanged.connect(self._on_algorithm_change)
        form_layout.addRow("Nesting Algorithm:", self.algorithm_dropdown)

        font_layout = QtWidgets.QHBoxLayout()
        table_button_layout = QtWidgets.QHBoxLayout()
        action_button_layout = QtWidgets.QHBoxLayout()

        self.sheet_width_input = QtWidgets.QDoubleSpinBox(); self.sheet_width_input.setRange(1, 10000); self.sheet_width_input.setValue(_DEFAULTS["sheet_width"])
        self.sheet_height_input = QtWidgets.QDoubleSpinBox(); self.sheet_height_input.setRange(1, 10000); self.sheet_height_input.setValue(_DEFAULTS["sheet_height"])
        self.sheet_thickness_input = QtWidgets.QDoubleSpinBox(); self.sheet_thickness_input.setRange(0.1, 1000); self.sheet_thickness_input.setValue(_DEFAULTS["sheet_thickness"])
        self.part_spacing_input = QtWidgets.QDoubleSpinBox(); self.part_spacing_input.setRange(0, 1000); self.part_spacing_input.setValue(_DEFAULTS["part_spacing"])
        
        # Deflection is now specified as an angle (degrees) for more intuitive control
        # Internally converted to linear deflection: deflection_mm = angle / 200.0
        self.deflection_input = QtWidgets.QDoubleSpinBox()
        self.deflection_input.setRange(1, 90)
        self.deflection_input.setValue(_DEFAULTS["deflection_angle"])  # 30° default for faster processing
        self.deflection_input.setSingleStep(1)
        self.deflection_input.setDecimals(0)
        self.deflection_input.setSuffix("°")
        self.deflection_input.setToolTip(
            "<b>Curve Angle (Tessellation Quality):</b><br>"
            "Maximum angular deviation when approximating curves.<br><br>"
            "<b>Smaller (5-10°):</b> Smoother curves, more points, slower.<br>"
            "<b>Larger (20-45°):</b> Coarser curves, fewer points, faster.<br><br>"
            "<i>Tip: 10° is good for most parts. Use 5° for precision, 30°+ for speed.</i>"
        )
        
        self.simplification_input = QtWidgets.QDoubleSpinBox(); self.simplification_input.setRange(0.001, 10.0); self.simplification_input.setValue(1.0); self.simplification_input.setSingleStep(0.1); self.simplification_input.setDecimals(3)
        self.simplification_input.setToolTip(
            "<b>Simplify (Point Reduction):</b><br>"
            "Tolerance (mm) for dropping boundary points. Larger = coarser, faster.<br><br>"
            "<b>Measured</b> on a 122-part nest (3 sheets, all parts placed at every "
            "setting), wall clock against material yield:<br>"
            "<table cellspacing='0' cellpadding='2'>"
            "<tr><td><b>0.1</b></td><td>12.3s</td><td>57.4%</td></tr>"
            "<tr><td><b>0.25</b></td><td>10.4s</td><td>57.2%</td></tr>"
            "<tr><td><b>0.5</b></td><td>9.6s</td><td>57.2%</td></tr>"
            "<tr><td><b>1.0</b> (default)</td><td>8.3s</td><td>57.1%</td></tr>"
            "<tr><td><b>2.0</b></td><td>7.9s</td><td>56.7%</td></tr>"
            "</table><br>"
            "<b>Read this as:</b> above 0.1 the curve is flat &mdash; 0.1 to 1.0 costs "
            "about a third of the time to gain 0.3% yield. Dropping to 0.25 saves ~15% "
            "of the time for ~0.1% yield. The large jump is <i>below</i> 0.1, where "
            "4x the time buys 0.1% yield.<br><br>"
            "<b>Tip:</b> set this to your machine's precision tolerance (1mm for a "
            "router). Below ~0.25 it stops being worth the time unless a part has fine "
            "internal detail."
        )

        self.shape_table = QtWidgets.QTableWidget()
        self.shape_table.setColumnCount(6)
        self.shape_table.setHorizontalHeaderLabels(["Shape", "Quantity", "Rotations", "Override", "Up Dir", "Fill"])

        # Angles: 360 (1 step), 180 (2), 120 (3), 90 (4), 45 (8), 30 (12), 15 (24), 10 (36), 5 (72), 1 (360)
        self.rotation_angles = _DEFAULTS["rotation_angles"]
        

        # Two Minkowski groups rather than one, split by what the user is
        # deciding: how the part is oriented (Nesting Settings) versus how hard
        # the run works to get a better answer (Optimizations).
        self.minkowski_settings_group = QtWidgets.QGroupBox("Nesting Settings")
        minkowski_form_layout = QtWidgets.QFormLayout()

        self.minkowski_optimization_group = QtWidgets.QGroupBox("Optimizations")
        minkowski_opt_layout = QtWidgets.QFormLayout()

        # Direction Dial for Minkowski
        minkowski_dial_widget, self.minkowski_direction_dial, self.minkowski_direction_label, \
            self.minkowski_direction_buttons = self._build_direction_control()

        # Random Direction Checkbox for Minkowski
        self.minkowski_random_checkbox = QtWidgets.QCheckBox("Use Random Direction")
        self.minkowski_random_checkbox.setToolTip("If checked, each part will use a randomized placement weighting.")
        self.minkowski_random_checkbox.stateChanged.connect(
            lambda state: self._set_direction_control_enabled(
                not self.minkowski_random_checkbox.isChecked()))

        minkowski_form_layout.addRow("Nesting Direction:", minkowski_dial_widget)
        minkowski_form_layout.addRow(self.minkowski_random_checkbox)

        self.clear_cache_checkbox = QtWidgets.QCheckBox("Clear NFP Cache")
        self.clear_cache_checkbox.setChecked(False)
        self.clear_cache_checkbox.setToolTip("Forces recalculation of No-Fit Polygons. Slower, but resolves potential caching issues.")

        # Genetic options for Minkowski
        self.minkowski_population_size_input = QtWidgets.QSpinBox()
        self.minkowski_population_size_input.setRange(1, 500)
        self.minkowski_population_size_input.setValue(1)
        self.minkowski_population_size_input.setToolTip("Set to 1 for a single pass. Increase with generations for Genetic Algorithm.")
        
        self.minkowski_generations_input = QtWidgets.QSpinBox()
        self.minkowski_generations_input.setRange(1, 1000)
        self.minkowski_generations_input.setValue(1) # Default to 1 (No Genetic Loop)
        self.minkowski_generations_input.setToolTip("Set to 1 for a single pass. Increase to optimize using Genetic Algorithm.")

        # Deliberately not persisted, like Generations and Population Size:
        # it is meaningless without a GA configuration next to it, and a stale
        # target armed against a default single-pass run is a surprise.
        # 0 = off, following the Rotation Threads "Auto" convention.
        self.minkowski_target_sheets_input = QtWidgets.QSpinBox()
        self.minkowski_target_sheets_input.setRange(0, 100)
        self.minkowski_target_sheets_input.setValue(0)
        self.minkowski_target_sheets_input.setSpecialValueText("Off")
        self.minkowski_target_sheets_input.setToolTip(
            "Stop the run as soon as one layout places every part on this "
            "many sheets.\n\n"
            "Off (0) by default, which is the normal behaviour: run every "
            "generation, and stop early only when the search stops improving.\n\n"
            "A target is a maximum, not a goal to beat. Nothing can do better "
            "than every part on one sheet, so the run ends the moment one "
            "layout achieves it and the layouts and generations still to come "
            "are skipped. Note that a target looser than the natural result "
            "(3 target, 1 sheet needed) is therefore met by the very first "
            "layout.\n\n"
            "This does not make the search find a good layout sooner -- it only "
            "caps the time once the target is met. If the target is not "
            "achievable the run behaves exactly as it does now and finishes on "
            "the usual rules, and the log says the target was not reached.\n\n"
            "Fill parts are best-effort: they are placed after the search ends "
            "and are not counted towards the target.\n\n"
            "Only affects GA runs (population/generations > 1)."
        )

        self.candidate_geometry_cache_checkbox = QtWidgets.QCheckBox("Candidate Geometry Cache")
        self.candidate_geometry_cache_checkbox.setChecked(_DEFAULTS["candidate_geometry_cache"])
        self.candidate_geometry_cache_checkbox.setToolTip(
            "Caches translated candidate polygons within one nesting run. "
            "Collision and validity checks are still performed for every candidate."
        )

        self.minkowski_compactness_input = QtWidgets.QDoubleSpinBox()
        self.minkowski_compactness_input.setRange(0.0, 10.0)
        self.minkowski_compactness_input.setSingleStep(0.1)
        self.minkowski_compactness_input.setValue(0.0)
        self.minkowski_compactness_input.setToolTip(
            "Weight of the compactness fitness term (0 = off).\n"
            "Rewards GA layouts that leave one large contiguous open area on "
            "the last sheet\ninstead of scattered gaps. 1.0 blends equally "
            "with the bounding-box score;\nhigher values favor compactness "
            "over bounding box. Only affects GA selection\n"
            "(population/generations > 1), not individual part placement."
        )

        self.minkowski_compactness_help = QtWidgets.QPushButton("?")
        self.minkowski_compactness_help.setFixedSize(20, 20)
        self.minkowski_compactness_help.setToolTip("Click to learn more about how the Compactness function works.")
        self.minkowski_compactness_help.clicked.connect(self._show_compactness_info)

        # -- performance dials -------------------------------------------------
        # Both of these were environment variables and are now fields, because
        # the measurement says they are the two largest levers in a run and a
        # control nobody can reach is not a control. Defaults are unchanged, so
        # leaving them alone reproduces the previous behaviour exactly.
        self.minkowski_step_size_input = QtWidgets.QDoubleSpinBox()
        self.minkowski_step_size_input.setRange(0.1, 100.0)
        self.minkowski_step_size_input.setValue(5.0)
        self.minkowski_step_size_input.setSingleStep(0.5)
        self.minkowski_step_size_input.setDecimals(2)
        self.minkowski_step_size_input.setToolTip(
            "Spacing between candidate positions, in mm.\n\n"
            "Every position a part can occupy comes from sampling the boundary "
            "of a No-Fit Polygon at this interval, and a boundary of length L "
            "yields about L / step positions. Each one is then tested against "
            "the parts already placed, so the total number of collision tests "
            "scales with this value and it is the main control over how long a "
            "run takes.\n\n"
            "Larger: fewer positions, faster, and coarser packing.\n"
            "Smaller: more positions, slower, and finer packing.\n\n"
            "Only affects jobs that need positions away from the sheet corners "
            "and edges. Where parts simply line up against each other or the "
            "sheet border, the extra positions are tested and discarded and "
            "changing this has no visible effect on the result. Where parts "
            "interlock or have curved or closely spaced features, it does.\n\n"
            "If you raise it, check that the packing and the number of sheets "
            "are unchanged before keeping the change.")

        self.minkowski_rotation_workers_input = QtWidgets.QSpinBox()
        self.minkowski_rotation_workers_input.setRange(0, 64)
        self.minkowski_rotation_workers_input.setValue(0)
        self.minkowski_rotation_workers_input.setSpecialValueText("Auto")
        self.minkowski_rotation_workers_input.setToolTip(
            "How many rotations are checked at the same time.\n\n"
            "Each rotation is one candidate orientation of the part, checked "
            "independently, so this is how many of them are worked on "
            "concurrently. A separate pool is started for every part placed.\n\n"
            "Auto uses one thread per CPU core, which is the right setting for "
            "most machines. Going above the core count is usually slower: the "
            "geometry work runs outside the interpreter lock, but the code "
            "around it does not, so extra threads spend their time waiting on "
            "each other and on the shared NFP cache rather than doing work.\n\n"
            "Does not affect the packing, only how long it takes.")

        perf_form_layout = QtWidgets.QFormLayout()
        perf_form_layout.addRow("Candidate Step (mm):", self.minkowski_step_size_input)
        perf_form_layout.addRow("Rotation Threads:", self.minkowski_rotation_workers_input)

        mink_compactness_layout = QtWidgets.QHBoxLayout()
        mink_compactness_layout.addWidget(self.minkowski_compactness_input)
        mink_compactness_layout.addWidget(self.minkowski_compactness_help)
        mink_compactness_layout.addStretch()
        mink_compactness_layout.setContentsMargins(0, 0, 0, 0)

        # Rotation Steps for Minkowski
        self.minkowski_rotation_steps_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.minkowski_rotation_steps_slider.setRange(0, len(self.rotation_angles) - 1)
        self.minkowski_rotation_steps_slider.setValue(3) # Default 90 deg
        self.minkowski_rotation_display_label = QtWidgets.QLabel("")
        self.minkowski_rotation_display_label.setFixedWidth(100)
        self.minkowski_rotation_steps_slider.valueChanged.connect(lambda: self._update_rotation_label())

        mink_rot_layout = QtWidgets.QHBoxLayout()
        mink_rot_layout.addWidget(self.minkowski_rotation_steps_slider)
        mink_rot_layout.addWidget(self.minkowski_rotation_display_label)
        minkowski_form_layout.addRow("Rotation Angle:", mink_rot_layout)
        self.minkowski_settings_group.setLayout(minkowski_form_layout)

        # Optimizations. Compactness and Clear NFP Cache were not on the
        # requested list but are Minkowski-only controls, and this is the only
        # group they belong in now that the old single Minkowski group is
        # split. Both are genuinely optimisation knobs, so they sit here rather
        # than in an orphan group.
        minkowski_opt_layout.addRow("Generations:", self.minkowski_generations_input)
        minkowski_opt_layout.addRow("Population Size:", self.minkowski_population_size_input)
        minkowski_opt_layout.addRow("Stop At Sheets:", self.minkowski_target_sheets_input)
        minkowski_opt_layout.addRow(perf_form_layout)
        minkowski_opt_layout.addRow(self.candidate_geometry_cache_checkbox)
        minkowski_opt_layout.addRow("Compactness:", mink_compactness_layout)
        minkowski_opt_layout.addRow(self.clear_cache_checkbox)
        self.minkowski_optimization_group.setLayout(minkowski_opt_layout)

        self.physics_settings_group = QtWidgets.QGroupBox("Physics Nesting Settings")
        physics_form_layout = QtWidgets.QFormLayout()

        # Direction Dial for Physics. Shares the default and the label map with
        # the Minkowski dial above: the two are read by the same conversion and
        # a per-algorithm divergence here would be invisible until someone ran
        # the other algorithm and got a different nest.
        physics_dial_widget, self.physics_direction_dial, self.physics_direction_label, \
            self.physics_direction_buttons = self._build_direction_control()

        self.physics_random_checkbox = QtWidgets.QCheckBox("Use Random Direction")
        self.physics_random_checkbox.stateChanged.connect(
            lambda state: self._set_direction_control_enabled(
                not self.physics_random_checkbox.isChecked(),
                dial=self.physics_direction_dial,
                buttons=self.physics_direction_buttons))

        self.physics_step_size_input = QtWidgets.QDoubleSpinBox(); self.physics_step_size_input.setRange(0.1, 100); self.physics_step_size_input.setValue(5.0)
        self.physics_max_spawn_input = QtWidgets.QSpinBox(); self.physics_max_spawn_input.setRange(1, 1000); self.physics_max_spawn_input.setValue(100)
        self.physics_max_nesting_steps_input = QtWidgets.QSpinBox(); self.physics_max_nesting_steps_input.setRange(1, 5000); self.physics_max_nesting_steps_input.setValue(500)
        
        # Annealing controls
        self.physics_anneal_steps_input = QtWidgets.QSpinBox(); self.physics_anneal_steps_input.setRange(0, 500); self.physics_anneal_steps_input.setValue(25)
        self.anneal_rotate_checkbox = QtWidgets.QCheckBox("Anneal Rotate"); self.anneal_rotate_checkbox.setChecked(True)
        self.anneal_translate_checkbox = QtWidgets.QCheckBox("Anneal Translate"); self.anneal_translate_checkbox.setChecked(True)
        self.anneal_random_shake_checkbox = QtWidgets.QCheckBox("Random Shake Direction")

        self.physics_anneal_rot_steps = QtWidgets.QSpinBox(); self.physics_anneal_rot_steps.setRange(0, 500); self.physics_anneal_rot_steps.setValue(10)
        self.physics_anneal_rot_curve_type = QtWidgets.QComboBox()
        self.physics_anneal_rot_curve_type.addItems(["Logarithmic", "Linear", "Power 1.5", "Quadratic", "Exponential"])
        self.physics_anneal_rot_min = QtWidgets.QDoubleSpinBox(); self.physics_anneal_rot_min.setRange(0.0, 360.0); self.physics_anneal_rot_min.setValue(1.0)
        self.physics_anneal_rot_max = QtWidgets.QDoubleSpinBox(); self.physics_anneal_rot_max.setRange(0.0, 360.0); self.physics_anneal_rot_max.setValue(90.0)
        
        self.physics_anneal_curve_type = QtWidgets.QComboBox()
        self.physics_anneal_curve_type.addItems(["Logarithmic", "Linear", "Power 1.5", "Quadratic", "Exponential"])
        
        self.physics_anneal_min_amp = QtWidgets.QDoubleSpinBox(); self.physics_anneal_min_amp.setRange(0.0, 1000.0); self.physics_anneal_min_amp.setValue(0.1)
        self.physics_anneal_max_amp = QtWidgets.QDoubleSpinBox(); self.physics_anneal_max_amp.setRange(0.0, 5000.0); self.physics_anneal_max_amp.setValue(100.0)
        
        self.physics_improvement_threshold_input = QtWidgets.QDoubleSpinBox()
        self.physics_improvement_threshold_input.setRange(0.000001, 1.0)
        self.physics_improvement_threshold_input.setValue(0.01)
        self.physics_improvement_threshold_input.setSingleStep(0.01)
        self.physics_improvement_threshold_input.setDecimals(6)
        self.physics_improvement_threshold_input.setToolTip("Minimum score improvement required to reset simulation cycle. Prevents infinite loops from noise.")

        physics_form_layout.addRow("Gravity Direction:", physics_dial_widget)
        physics_form_layout.addRow(self.physics_random_checkbox)
        
        # Rotation Steps for Physics
        # Mapping: 1, 4 (90), 8 (45), 12 (30), 24 (15), 36 (10), 72 (5), 180 (2), 360 (1)
        self.physics_rotation_steps_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        self.physics_rotation_steps_slider.setRange(0, 8) 
        self.physics_rotation_steps_slider.setValue(1) # Default 90 deg (4 steps)
        self.physics_rotation_display_label = QtWidgets.QLabel("")
        self.physics_rotation_display_label.setFixedWidth(120)
        self.physics_rotation_steps_slider.valueChanged.connect(lambda: self._update_rotation_label())

        physics_form_layout.addRow("Step Size:", self.physics_step_size_input)
        physics_form_layout.addRow("Max Spawn Attempts:", self.physics_max_spawn_input)
        physics_form_layout.addRow("Max Nesting Steps:", self.physics_max_nesting_steps_input)

        physics_form_layout.addRow(QtWidgets.QLabel("")) # Spacer
        physics_form_layout.addRow(QtWidgets.QLabel("--- Annealing (Shake) ---"))
        
        # Anneal Rotate Logic
        physics_form_layout.addRow(self.anneal_rotate_checkbox)
        phys_rot_layout = QtWidgets.QHBoxLayout()
        phys_rot_layout.addWidget(self.physics_rotation_steps_slider)
        phys_rot_layout.addWidget(self.physics_rotation_display_label)
        physics_form_layout.addRow("Rotation Steps:", phys_rot_layout)
        physics_form_layout.addRow("Rot Anneal Steps:", self.physics_anneal_rot_steps)
        physics_form_layout.addRow("Rot Curve Type:", self.physics_anneal_rot_curve_type)
        physics_form_layout.addRow("Rot Min Angle:", self.physics_anneal_rot_min)
        physics_form_layout.addRow("Rot Max Angle:", self.physics_anneal_rot_max)
        
        # Anneal Translate Logic
        physics_form_layout.addRow(self.anneal_translate_checkbox)
        physics_form_layout.addRow("Anneal Steps:", self.physics_anneal_steps_input)
        physics_form_layout.addRow("Improvement Threshold:", self.physics_improvement_threshold_input)
        physics_form_layout.addRow(QtWidgets.QLabel("")) # Spacer
        physics_form_layout.addRow("Curve Type:", self.physics_anneal_curve_type)
        physics_form_layout.addRow("Min Amplitude:", self.physics_anneal_min_amp)
        physics_form_layout.addRow("Max Amplitude:", self.physics_anneal_max_amp)
        physics_form_layout.addRow(self.anneal_random_shake_checkbox)

        self.physics_settings_group.setLayout(physics_form_layout)

        # Helpers and Logging are NOT tied to the algorithm, even though they
        # sit in the same panel as the Minkowski groups.
        #
        # Everything in them is read unconditionally by NestingController
        # ._collect_ui_params (font_path, add_labels, label_size, label_height,
        # show_bounds, verbose, performance_logging, simulate, sound), and the
        # Physics algorithm nests through the same controller. Hiding these
        # when Physics is selected would not merely tidy the panel, it would
        # make those controls unreachable for half the algorithms -- so the
        # algorithm toggle only touches the Minkowski groups.
        self.helpers_group = QtWidgets.QGroupBox("Helpers")
        helpers_layout = QtWidgets.QVBoxLayout()
        self.logging_group = QtWidgets.QGroupBox("Logging")
        logging_box_layout = QtWidgets.QVBoxLayout()

        # Set initial visibility
        self.minkowski_settings_group.setVisible(True)
        self.minkowski_optimization_group.setVisible(True)
        self.physics_settings_group.setVisible(False)


        self.show_bounds_checkbox = QtWidgets.QCheckBox("Show Bounds"); self.show_bounds_checkbox.setChecked(True)
        self.add_labels_checkbox = QtWidgets.QCheckBox("Add Identifier Labels"); self.add_labels_checkbox.setChecked(_DEFAULTS["add_labels"])
        self.label_height_input = QtWidgets.QDoubleSpinBox(); self.label_height_input.setRange(0, 1000); self.label_height_input.setValue(25.0)
        self.label_height_input.setToolTip("The height (Z-offset) for the identifier labels.")
        self.label_size_input = QtWidgets.QDoubleSpinBox(); self.label_size_input.setRange(1, 100); self.label_size_input.setValue(10.0)
        self.label_size_input.setToolTip("The text size for identifier labels in mm.")
        self.simulate_nesting_checkbox = QtWidgets.QCheckBox("Simulate Nesting (slower)"); self.simulate_nesting_checkbox.setChecked(_DEFAULTS["simulate_nesting"])
        self.verbose_logging_checkbox = QtWidgets.QCheckBox("Verbose Logging"); self.verbose_logging_checkbox.setChecked(_DEFAULTS["verbose_logging"])
        self.verbose_logging_checkbox.setToolTip("Enables detailed logging of the nesting process in the FreeCAD console.")
        self.performance_logging_checkbox = QtWidgets.QCheckBox("Performance Logging")
        self.performance_logging_checkbox.setChecked(_DEFAULTS["performance_logging"])
        self.performance_logging_checkbox.setToolTip("Enables performance and timing diagnostics in the FreeCAD console.")
        self.sound_checkbox = QtWidgets.QCheckBox("Play sound on completion"); self.sound_checkbox.setChecked(True)
        
        self.nest_button = QtWidgets.QPushButton("Run Nesting")
        self.cancel_button = QtWidgets.QPushButton("Cancel Nesting")
        self.cancel_button.setEnabled(False)

        self.add_parts_button = QtWidgets.QPushButton("Add Selected")
        self.remove_parts_button = QtWidgets.QPushButton("Remove Selected")
        
        self.font_select_button = QtWidgets.QPushButton("Select Font")
        self.font_label = QtWidgets.QLabel("No Font Selected")
        self.font_label.setWordWrap(True)
        font_layout.addWidget(self.font_select_button)
        font_layout.addWidget(self.font_label)
        
        self.status_label = QtWidgets.QLabel("Select master shapes to nest.")
        self.status_label.setWordWrap(True)

        label_options_layout = QtWidgets.QHBoxLayout()
        label_options_layout.addWidget(self.add_labels_checkbox)
        label_options_layout.addWidget(QtWidgets.QLabel("Size:"))
        label_options_layout.addWidget(self.label_size_input)
        label_options_layout.addWidget(QtWidgets.QLabel("Height (Z):"))
        label_options_layout.addWidget(self.label_height_input)
        label_options_layout.addStretch()

        form_layout.addRow("Sheet Width:", self.sheet_width_input)
        form_layout.addRow("Sheet Height:", self.sheet_height_input)
        form_layout.addRow("Sheet Thickness:", self.sheet_thickness_input)
        form_layout.addRow("Part Spacing:", self.part_spacing_input)
        
        # Advanced Curve Settings
        curve_settings_layout = QtWidgets.QHBoxLayout()
        curve_settings_layout.addWidget(QtWidgets.QLabel("Curve:"))
        curve_settings_layout.addWidget(self.deflection_input)
        curve_settings_layout.addWidget(QtWidgets.QLabel("Simplify:"))
        curve_settings_layout.addWidget(self.simplification_input)
        
        form_layout.addRow("Bounds Resolution:", curve_settings_layout)

        form_layout.addRow(self.minkowski_settings_group)
        form_layout.addRow(self.minkowski_optimization_group)
        form_layout.addRow(self.physics_settings_group)

        # Helpers: font, labels, and the display/feedback switches. Laid out
        # with QFormLayout inside the box so the label rows keep their
        # right-aligned colon styling instead of losing it to nested hboxes.
        helpers_form = QtWidgets.QFormLayout()
        helpers_form.addRow("Identifier Font:", font_layout)
        helpers_form.addRow(label_options_layout)
        helpers_form.addRow(self.simulate_nesting_checkbox)
        helpers_form.addRow(self.show_bounds_checkbox)
        helpers_form.addRow(self.sound_checkbox)
        helpers_layout.addLayout(helpers_form)
        self.helpers_group.setLayout(helpers_layout)

        logging_box_layout.addWidget(self.verbose_logging_checkbox)
        logging_box_layout.addWidget(self.performance_logging_checkbox)
        self.logging_group.setLayout(logging_box_layout)

        form_layout.addRow(self.helpers_group)
        form_layout.addRow(self.logging_group)
        
        table_button_layout.addWidget(self.add_parts_button)
        table_button_layout.addWidget(self.remove_parts_button)

        action_button_layout.addWidget(self.nest_button)
        action_button_layout.addWidget(self.cancel_button)

        main_layout.addLayout(form_layout)
        main_layout.addWidget(self.shape_table)
        main_layout.addLayout(table_button_layout)
        main_layout.addLayout(action_button_layout)
        
        self.progressBar = QtWidgets.QProgressBar()
        self.progressBar.setRange(0, 100)
        self.progressBar.setValue(0)
        self.progressBar.setTextVisible(True)
        self.progressBar.setVisible(False) # Hidden by default
        main_layout.addWidget(self.progressBar)

        main_layout.addWidget(self.status_label)
        main_layout.addStretch()
        
        self.setLayout(main_layout)

        # Connect signals

        # Link label inputs to the add labels checkbox
        def toggle_label_inputs(state):
            enabled = state == QtCore.Qt.Checked
            self.label_size_input.setEnabled(enabled)
            self.label_height_input.setEnabled(enabled)
        
        self.add_labels_checkbox.stateChanged.connect(toggle_label_inputs)
        toggle_label_inputs(QtCore.Qt.Checked if self.add_labels_checkbox.isChecked() else QtCore.Qt.Unchecked)

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
        
        # Ensure initial labels are correct
        self._update_rotation_label()
        
        # Load initial selection
        self.controller.load_selection()

    # -- Nesting direction control ---------------------------------------
    #
    # Built by a method rather than inline because there are two of them
    # (Minkowski and Physics) that must behave identically, and when they were
    # written out separately the label map had already been duplicated once and
    # the two defaults had already drifted once. A single builder means a
    # divergence would have to be reintroduced deliberately.
    #
    # The buttons are generated from the label map and placed by NAME at their
    # compass positions around the dial, so adding a fifth name to
    # DIRECTION_LABELS adds a button automatically -- it just needs a clock
    # position as well, and lands below the dial until one is given.

    def _build_direction_control(self):
        """Dial + readout + four cardinal buttons.

        Returns (container_widget, dial, label, buttons) so the caller can wire
        the "Use Random Direction" checkbox to disable the whole control. The
        buttons are returned rather than looked up later because the checkbox
        has to grey them out too: leaving them live while the dial is disabled
        would let a user set a direction that is then ignored, which looks like
        a bug in the run rather than in the UI.
        """
        dial = QtWidgets.QDial()
        dial.setRange(0, _MINKOWSKI_DIR_MAX)
        dial.setValue(_DEFAULT_DIRECTION_DIAL)
        dial.setWrapping(True)
        dial.setNotchesVisible(True)

        # Seeded from the value, not written as a literal. Previously this was
        # a hardcoded "Down" beside a hardcoded setValue(0), so the two had to
        # be kept in agreement by hand and nothing checked them.
        label = QtWidgets.QLabel(
            _DIRECTION_LABELS.get(_DEFAULT_DIRECTION_DIAL, ""))
        label.setAlignment(QtCore.Qt.AlignCenter)

        def update_label(value):
            text = _DIRECTION_LABELS.get(value, "")
            label.setText(text if text else f"{value}°")
        dial.valueChanged.connect(update_label)

        # Buttons sit at their compass positions around the dial: Up at 12
        # o'clock, Right at 3, Down at 6, Left at 9, in a 3x3 grid with the dial
        # in the middle cell.
        #
        # The grid is keyed on the NAME, not on the dial reading, because the
        # dial reading is not the compass bearing. It is rotated by a quarter
        # turn and flipped: 0 is Down but is drawn at the bottom, 90 is Left
        # but is drawn on the right. Placing buttons by reading would have put
        # Left on the right-hand side, which is precisely the confusion this
        # layout exists to remove.
        _CLOCK_POSITION = {"Up": (0, 1), "Right": (1, 2),
                           "Down": (2, 1), "Left": (1, 0)}
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(4)
        grid.addWidget(dial, 1, 1, QtCore.Qt.AlignCenter)

        buttons = []
        for value, name in sorted(_DIRECTION_LABELS.items()):
            button = QtWidgets.QPushButton(name)
            # setValue on the dial, so the readout and anything else watching
            # valueChanged stay in step. Writing the label directly instead
            # would skip both, and the label would then disagree with the value
            # the run actually uses.
            button.clicked.connect(lambda _checked=False, v=value: dial.setValue(v))
            button.setToolTip(f"Set nesting direction to {name}")
            # Small and square: the grid cells have to be roughly the size of
            # the dial, and long words like "Right" would otherwise widen the
            # whole control.
            button.setFixedSize(40, 28)
            position = _CLOCK_POSITION.get(name)
            if position is None:
                # A name added to DIRECTION_LABELS with no clock position
                # defined. Ignoring it silently would look like a button had
                # been provided and was missing, so it goes below the dial
                # rather than nowhere.
                grid.addWidget(button, 3, 0, 1, 3, QtCore.Qt.AlignCenter)
            else:
                # AlignCenter, because a fixed-size widget is otherwise placed
                # at the top-left of its cell. The cells are as wide as the
                # dial, so without this the 12 and 6 o'clock buttons sit visibly
                # off the vertical axis of the needle while the 3 and 9 stay
                # put -- correct by row, lopsided by eye.
                grid.addWidget(button, position[0], position[1],
                               QtCore.Qt.AlignCenter)
            buttons.append(button)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(grid)
        layout.addWidget(label)
        container = QtWidgets.QWidget()
        container.setLayout(layout)
        return container, dial, label, buttons

    def _set_direction_control_enabled(self, enabled, dial=None, buttons=None):
        """Enable or disable a direction control as a unit.

        Called by both "Use Random Direction" checkboxes. Defaults to the
        Minkowski control, which is the one whose checkbox is connected without
        arguments; the Physics one passes its own.
        """
        if dial is None:
            dial = self.minkowski_direction_dial
        if buttons is None:
            buttons = self.minkowski_direction_buttons
        dial.setEnabled(enabled)
        for button in buttons:
            button.setEnabled(enabled)

    def add_part_row(self, row_index, label, quantity=1, rotation_steps=4, override_rotation=False, 
                       up_direction="Z+", fill_sheet=False):
        """Helper function to create and populate a single row in the parts table."""
        label_item = QtWidgets.QTableWidgetItem(label)
        label_item.setFlags(label_item.flags() & ~QtCore.Qt.ItemIsEditable)

        quantity_spinbox = QtWidgets.QSpinBox()
        quantity_spinbox.setRange(1, 500)
        quantity_spinbox.setValue(quantity)

        rotation_widget = QtWidgets.QWidget()
        rotation_layout = QtWidgets.QHBoxLayout(rotation_widget)
        rotation_layout.setContentsMargins(0, 0, 0, 0)
        
        rotation_slider = QtWidgets.QSlider(QtCore.Qt.Horizontal)
        rotation_slider.setRange(0, 360) # Allow 0 for no rotation
        rotation_slider.setValue(rotation_steps)
        
        rotation_spinbox = QtWidgets.QSpinBox()
        rotation_spinbox.setRange(0, 360) # Allow 0 for no rotation
        rotation_spinbox.setValue(rotation_steps)
        rotation_spinbox.setToolTip("Override global rotation steps for this part. 0 or 1 means no rotation.")

        rotation_slider.valueChanged.connect(rotation_spinbox.setValue)
        rotation_spinbox.valueChanged.connect(rotation_slider.setValue)
        
        rotation_layout.addWidget(rotation_slider)
        rotation_layout.addWidget(rotation_spinbox)

        override_checkbox = QtWidgets.QCheckBox()
        override_checkbox.setChecked(override_rotation)
        override_checkbox.stateChanged.connect(rotation_widget.setEnabled)
        rotation_widget.setEnabled(override_rotation) # Disabled by default unless overridden

        up_dir_combo = QtWidgets.QComboBox()
        up_dir_combo.addItems(["Z+", "Z-", "Y+", "Y-", "X+", "X-"])
        up_dir_combo.setCurrentText(up_direction)
        up_dir_combo.setToolTip("Define which direction is 'up' for this part when projecting to 2D.")

        fill_checkbox = QtWidgets.QCheckBox()
        fill_checkbox.setChecked(fill_sheet)
        fill_checkbox.setToolTip("If checked, this part will be used to fill remaining space after all other parts are placed.")

        self.shape_table.setItem(row_index, 0, label_item)
        self.shape_table.setCellWidget(row_index, 1, quantity_spinbox)
        self.shape_table.setCellWidget(row_index, 2, rotation_widget)
        self.shape_table.setCellWidget(row_index, 3, override_checkbox)
        self.shape_table.setCellWidget(row_index, 4, up_dir_combo)
        self.shape_table.setCellWidget(row_index, 5, fill_checkbox)

    def select_font_file(self):
        """Opens a file dialog to let the user select a font file."""
        default_font_dir = FONTS_DIR
        if not os.path.isdir(default_font_dir):
            default_font_dir = "" # Fallback if fonts dir doesn't exist

        file_dialog_result = QtWidgets.QFileDialog.getOpenFileName(
            self, 
            "Select Font File", 
            default_font_dir, # Set the default directory
            "Font Files (*.ttf *.otf)"
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
            FreeCAD.Console.PrintWarning(f"[NestingPanel] Failed to set default font: {e}\n")

    def log_message(self, message, level="message"):
        """Displays a message in the status label and logs to the console."""
        try:
            self.status_label.setText(message)
        except RuntimeError:
            # The widget C++ object has been deleted (panel closed), but Python object persists.
            # We can just log to console and ignore the UI update.
            pass

        if level == "warning":
            FreeCAD.Console.PrintWarning(message + "\n")
        else:
            FreeCAD.Console.PrintMessage(message + "\n")
        
        # Process UI events to make sure the label updates immediately
        # We wrap this too, just in case
        try:
            FreeCADGui.updateGui()
        except RuntimeError:
            pass  # GUI window closed

    def load_persisted_settings(self):
        """Loads settings from FreeCAD preferences."""
        prefs = FreeCAD.ParamGet(PREFS_PATH)
        self.sheet_width_input.setValue(prefs.GetFloat(PROP_SHEET_WIDTH, 600.0))
        self.sheet_height_input.setValue(prefs.GetFloat(PROP_SHEET_HEIGHT, 600.0))
        self.part_spacing_input.setValue(prefs.GetFloat(PROP_PART_SPACING, 12.5))
        self.sheet_thickness_input.setValue(prefs.GetFloat(PROP_SHEET_THICKNESS, 3.0))
        self.label_size_input.setValue(prefs.GetFloat(PROP_LABEL_SIZE, 10.0))
        self.deflection_input.setValue(prefs.GetFloat(PROP_DEFLECTION_ANGLE, 30.0) or 30.0)
        self.simplification_input.setValue(prefs.GetFloat(PROP_SIMPLIFICATION, 1.0))
        self.minkowski_compactness_input.setValue(prefs.GetFloat("GACompactnessWeight", 0.0))
        self.verbose_logging_checkbox.setChecked(prefs.GetBool("VerboseLogging", False))
        self.performance_logging_checkbox.setChecked(prefs.GetBool("PerformanceLogging", False))
        self.candidate_geometry_cache_checkbox.setChecked(
            prefs.GetBool("CandidateGeometryCache",
                               CANDIDATE_GEOMETRY_CACHE_DEFAULT)
        )
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
        self.minkowski_step_size_input.setValue(
            prefs.GetFloat("MinkowskiStepSize", 5.0) or 5.0)
        self.minkowski_rotation_workers_input.setValue(
            prefs.GetInt("MinkowskiRotationWorkers", 0))
        mink_rot_steps = prefs.GetInt("MinkowskiRotationSteps", 4) # Default 90 deg (4 steps)
        if mink_rot_steps > 0:
            target_angle = 360.0 / mink_rot_steps
            closest_idx = 0
            min_diff = float('inf')
            for i, angle in enumerate(self.rotation_angles):
                diff = abs(angle - target_angle)
                if diff < min_diff:
                    min_diff = diff
                    closest_idx = i
            self.minkowski_rotation_steps_slider.setValue(closest_idx)
            
        # Physics
        phys_rot_steps = prefs.GetInt("PhysicsRotationSteps", 4) # Default 90 deg (4 steps)
        if phys_rot_steps > 0:
            target_angle = 360.0 / phys_rot_steps
            phys_angles = PHYSICS_ROTATION_PRESETS
            closest_idx = 0
            min_diff = float('inf')
            for i, angle in enumerate(phys_angles):
                diff = abs(angle - target_angle)
                if diff < min_diff:
                    min_diff = diff
                    closest_idx = i
            self.physics_rotation_steps_slider.setValue(closest_idx)
        
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
        except RuntimeError:
            pass # Widget deleted
        except Exception as e:
            FreeCAD.Console.PrintWarning(f"UI Update Error: {e}\n")

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
        """Handles switching between nesting algorithms.

        Toggles the Minkowski groups and the Physics group. Helpers and Logging
        are deliberately left alone: their controls are read unconditionally by
        the controller, so they apply to whichever algorithm is running.
        """
        is_minkowski = algo_name == "Minkowski"
        self.minkowski_settings_group.setVisible(is_minkowski)
        self.minkowski_optimization_group.setVisible(is_minkowski)
        self.physics_settings_group.setVisible(algo_name == "Physics")
        # Ensure the rotation label/steps are immediately clarified for the new algorithm
        self._update_rotation_label()

    def reset_progress(self):
        """Resets and hides the progress bar."""
        try:
            self.progressBar.setValue(0)
            self.progressBar.setVisible(False)
        except RuntimeError: pass  # Widget deleted

    def _show_compactness_info(self):
        """Shows an informative dialog explaining the GA Compactness function."""
        msg_box = QtWidgets.QMessageBox(self)
        msg_box.setWindowTitle("GA Compactness Optimization")
        msg_box.setIcon(QtWidgets.QMessageBox.Information)
        msg_box.setText(
            "<b>Compactness Optimization in Genetic Algorithm (GA)</b><br><br>"
            "This option controls selection pressure when utilizing the Genetic Algorithm optimizer.<br><br>"
            "<b>How it works:</b><br>"
            "<ul>"
            "<li><b>Weight = 0.0 (default):</b> Layouts with the same sheet count are compared purely based on the bounding box "
            "dimensions of the parts on the last sheet.</li>"
            "<li><b>Weight > 0.0:</b> Evaluates layouts using a blend of the last sheet's bounding box and the deficit of the "
            "<b>largest contiguous open area</b> remaining on that sheet.</li>"
            "<li><b>Benefits:</b> Rewards packing arrangements that group parts tightly, leaving a single large contiguous "
            "remnant of material rather than scattered gaps.</li>"
            "</ul><br>"
            "<b>Details:</b><br>"
            "<ul>"
            "<li>Deficit is computed exactly using Shapely polygon arithmetic: <i>sheet_area - largest_open_area(last_sheet)</i>.</li>"
            "<li>The blended score is scaled so that compactness never outranks the number of sheets or unplaced-part penalties.</li>"
            "<li>This weight only affects the selection phase of GA runs (population/generations > 1), not individual part placement.</li>"
            "</ul>"
        )
        msg_box.setStandardButtons(QtWidgets.QMessageBox.Ok)
        msg_box.exec_()
        
