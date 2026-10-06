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
from ... import FONTS_DIR, DEFAULT_FONT, TOOLTIPS_DIR
from ...freecad_helpers import set_visibility
from ...length_field import LengthField
from ... import units
from ...ui_helpers import (
    MARGINS_NONE,
    CollapsibleSection,
    make_checkbox,
    make_double_spinbox,
    make_int_spinbox,
    tooltip_with_image,
)

#: The diagram in the Candidate Step tooltip, and the popup width to pin it to.
#:
#: Two constants rather than reading the width back out of the SVG at panel
#: build time, which is what the Nightly branch's `rich_tooltip` does. The
#: duplication is deliberate and is the point: Qt sizes a tooltip from its text
#: and not from its image, so the popup needs an explicit width, and the only
#: thing that could make the two numbers disagree is somebody editing the SVG.
#: `test_tooltip_assets.py` asserts they agree, and that the image actually
#: loads, so that costs a gate run rather than a runtime read on every panel
#: build -- and unlike a runtime read, it fails before a user ever hovers the
#: field.
_CANDIDATE_STEP_DIAGRAM = "NestingWorkbench_CandidateStep.svg"
_CANDIDATE_STEP_DIAGRAM_WIDTH = 320

_MINKOWSKI_DIR_MAX = 359

# Spacing for the two-column grids. Slightly tighter than a QFormLayout's
# default, because these rows are 4 and 8 fields rather than 1 or 2, and the
# default row gap starts to dominate once the labels are short.
_GRID_COLUMN_SPACING = 12
_GRID_ROW_SPACING = 6

# Fixed width for the dial's column in the Nesting Settings grid. A QDial is a
# circle and has no reason to grow with the panel, and pinning it stops the
# slider beside it from claiming the surplus width. Comfortably wider than the
# 100px the widget asks for, so the readout below the dial is not clipped.
_DIAL_COLUMN_WIDTH = 120


def _snap_to_step(value, step=None):
    """Round ``value`` to the nearest multiple of the dial's step.

    A stored reading from a build with a different step would otherwise land
    between notches: the dial would show the value it snapped to, the readout
    would follow, and the stored number the user last chose would be
    unreachable. Snapping is done on load rather than on store so the stored
    figure stays whatever the dial actually held.
    """
    step = DIRECTION_STEP_DEGREES if step is None else step
    try:
        value = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_DIRECTION_DIAL
    return int(round(value / float(step))) * int(step)


def _place(grid, widget, row, column, span):
    """Add ``widget`` to ``grid``, accepting a widget or a nested layout.

    addWidget() only takes a QWidget, so a field that is a QHBoxLayout (the
    Compactness spin box and its help button) has to go in through addLayout().
    Both are the same placement; only the call differs, and the branch belongs
    here rather than being written out at each of the call sites.
    """
    if isinstance(widget, QtWidgets.QLayout):
        grid.addLayout(widget, row, column, 1, span)
    else:
        grid.addWidget(widget, row, column, 1, span)

# Direction naming and the default live in constants (DIRECTION_LABELS,
# DEFAULT_DIRECTION_DIAL) so NestingJob can share the default without importing
# this module. See the comment there for why the dial reading is not the
# compass bearing.
#
# DIRECTION_LABELS is imported wholesale by the constants star-import and is
# used directly below, so it needs no local alias. It used to have one -- the
# _DIRECTION_LABELS name the four cardinal buttons were generated from -- and
# that was the only reader, so the alias went with the buttons.
_DEFAULT_DIRECTION_DIAL = DEFAULT_DIRECTION_DIAL

# DIRECTION_LABELS is keyed on the DIAL reading; the readout has to be keyed on
# the BEARING, because that is the number the search uses. Derived here rather
# than hand-written so the two cannot disagree: 0 -> Down is a fact about the
# conversion, and repeating it in a second table is how it would start
# disagreeing.
_BEARING_LABELS = {dial_to_bearing(dial): name
                   for dial, name in DIRECTION_LABELS.items()}

_DEFAULTS = {
    # Every length below is in millimetres, and stays in millimetres all the
    # way to the field. The field converts for display; nothing upstream of it
    # ever sees a display unit.
    "sheet_width": 600.0,
    "sheet_height": 600.0,
    "part_spacing": 12.5,
    "sheet_thickness": 3.0,
    "simplification": 1.0,
    "minkowski_step_size": 5.0,
    "physics_step_size": 5.0,
    "physics_anneal_min_amp": 0.1,
    "physics_anneal_max_amp": 100.0,
    "label_height": 25.0,
    "label_size": 10.0,
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
        # The unit schema the fields are currently rendered in. None means "not
        # yet", so the first refresh_unit_display always does its work.
        self._rendered_schema = None
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
        
        # Algorithm Selection
        self.algorithm_dropdown = QtWidgets.QComboBox()
        self.algorithm_dropdown.addItems(["Minkowski", "Physics"])
        self.algorithm_dropdown.setCurrentIndex(0) # Default to Minkowski
        self.algorithm_dropdown.currentTextChanged.connect(self._on_algorithm_change)


        self._build_length_fields()

        self.shape_table = QtWidgets.QTableWidget()
        self.shape_table.setColumnCount(6)
        self.shape_table.setHorizontalHeaderLabels(["Shape", "Quantity", "Rotations", "Override", "Up Dir", "Fill"])

        # Angles: 360 (1 step), 180 (2), 120 (3), 90 (4), 45 (8), 30 (12), 15 (24), 10 (36), 5 (72), 1 (360)
        self.rotation_angles = _DEFAULTS["rotation_angles"]
        

        # Two Minkowski groups rather than one, split by what the user is
        # deciding: how the part is oriented (Nesting Settings) versus how hard
        # the run works to get a better answer (Optimizations).
        #
        # Nesting Settings collapses like Helpers and Logging do, and starts
        # collapsed. Its dial is the tallest single control in the panel by a
        # wide margin, and it is a set-and-forget choice -- a run uses whatever
        # direction it is left on. See ui_helpers.CollapsibleSection for why a
        # toggle flag alone is not enough.
        # Collapsed by default, matching the state this section has always
        # opened in. See ui_helpers.CollapsibleSection for why a checkable
        # QGroupBox could not do this and what setVisible-not-setEnabled buys.
        self.minkowski_settings_group = CollapsibleSection(
            "Nesting Settings", expanded=False)

        # Opens expanded: it was a plain QGroupBox before, so it was always
        # visible. Making it collapsible must not also tuck it away.
        self.minkowski_optimization_group = CollapsibleSection(
            "Optimizations", expanded=True)

        # Direction Dial for Minkowski
        minkowski_dial_widget, self.minkowski_direction_dial, self.minkowski_direction_label = \
            self._build_direction_control()

        # Random Direction Checkbox for Minkowski
        self.minkowski_random_checkbox = make_checkbox(
            "Use Random Direction")
        self.minkowski_random_checkbox.setToolTip("If checked, each part will use a randomized placement weighting.")
        self.minkowski_random_checkbox.stateChanged.connect(
            lambda state: self._set_direction_control_enabled(
                not self.minkowski_random_checkbox.isChecked()))

        self.clear_cache_checkbox = make_checkbox("Clear NFP Cache")
        self.clear_cache_checkbox.setToolTip("Forces recalculation of No-Fit Polygons. Slower, but resolves potential caching issues.")

        self._build_minkowski_ga_fields()

        mink_compactness_layout = self._build_optimization_fields()

        self._build_minkowski_perf_dials()

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

        self._build_direction_grid(minkowski_dial_widget, mink_rot_layout)

        # Optimizations. Compactness and Clear NFP Cache were not on the
        # requested list but are Minkowski-only controls, and this is the only
        # group they belong in now that the old single Minkowski group is
        # split. Both are genuinely optimisation knobs, so they sit here rather
        # than in an orphan group.
        # A two-column grid rather than the QFormLayout these rows started
        # as. Reading order is unchanged -- the entries are the same and in
        # the same sequence, so entry N is still where it was -- but four rows
        # of labels have become two, which is the difference between the
        # Optimizations box needing a scrollbar and not.
        #
        # Candidate Geometry Cache and Clear NFP Cache are passed with no
        # label: a checkbox carries its own text, and giving it a label column
        # of its own would leave a gap where the word should be.
        self.minkowski_optimization_group.addLayout(self._two_column_grid([
            ("Generations:", self.minkowski_generations_input),
            ("Population Size:", self.minkowski_population_size_input),
            ("Stop At Sheets:", self.minkowski_target_sheets_input),
            ("Candidate Step:", self.minkowski_step_size_input.widget()),
            ("Rotation Threads:", self.minkowski_rotation_workers_input),
            (None, self.candidate_geometry_cache_checkbox),
            ("Compactness:", mink_compactness_layout),
            (None, self.clear_cache_checkbox),
        ]))

        self._build_physics_section()

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
        # Collapsed by default, and checkable so the title becomes the toggle.
        #
        # These two are diagnostic controls -- a font picker, a log verbosity
        # switch -- and between them they were taller than the four sheet
        # fields at the top of the panel, which is a poor trade for a run that
        # changes neither. The state is deliberately NOT persisted: every open
        # starts in the same shape, so the space saving is guaranteed rather
        # than a one-off that decays the first time somebody expands a group.
        #
        # setCheckable alone is NOT enough, and looks like it works. An
        # unchecked checkable QGroupBox only DISABLES its children; it does not
        # hide them. Measured on a two-widget group, the height is 64px checked
        # and 64px unchecked, and the child reports isHidden()=False in both
        # states. So the first version of this greyed the controls out and
        # reclaimed no space at all -- the worst of both, looking broken and
        # saving nothing. CollapsibleSection hides an explicit content_area
        # instead; see ui_helpers for the measurement and for why it uses
        # setVisible rather than setEnabled.
        #
        # Nothing here conflicts with the algorithm toggle above, which shows
        # and hides the Minkowski groups by setVisible. A collapsed group's
        # children are hidden but alive, so _collect_ui_params still reads
        # them and refresh_unit_display can still re-render their fields.
        self.helpers_group = CollapsibleSection("Helpers", expanded=False)
        self.logging_group = CollapsibleSection("Logging", expanded=False)

        self._set_initial_section_visibility()

        self._build_helper_fields()
        self._build_action_buttons()

        self.font_select_button = QtWidgets.QPushButton("Select Font")
        self.font_label = QtWidgets.QLabel("No Font Selected")
        self.font_label.setWordWrap(True)
        self._assemble_panel_layout()

        # Connect signals

        # Link label inputs to the add labels checkbox
        def toggle_label_inputs(state):
            enabled = state == QtCore.Qt.Checked
            self.label_size_input.widget().setEnabled(enabled)
            self.label_height_input.widget().setEnabled(enabled)

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

        # Render the fields and the unit-bearing tooltips for the document as
        # it stands now. Done after the settings load so the first thing the
        # user sees is already in their units.
        self.refresh_unit_display(force=True)

        # Ensure initial labels are correct
        self._update_rotation_label()

        # Load initial selection
        self.controller.load_selection()

    def _assemble_panel_layout(self):
        """Create the panel's layouts and parent every control built above.

        Takes no arguments. form_layout and main_layout are used only here and
        by the algorithm row, which is also placed here -- the dropdown itself
        is created and connected in _setup_ui, immediately before the fields,
        so the order in which a signal can first fire is unchanged.

        The order of the seven items in main_layout is asserted in
        probe_gui_layout_positions: form, shape table, table buttons, action
        buttons, progress bar, status label, stretch. Which section each control
        ended up in is asserted there too.
        """
        main_layout = QtWidgets.QVBoxLayout()
        form_layout = QtWidgets.QFormLayout()

        form_layout.addRow("Nesting Algorithm:", self.algorithm_dropdown)

        font_layout = QtWidgets.QHBoxLayout()
        table_button_layout = QtWidgets.QHBoxLayout()
        action_button_layout = QtWidgets.QHBoxLayout()

        # Built here rather than in _setup_ui: both are consumed only by the
        # helpers and logging forms below, so as locals they cannot be left
        # behind by a future edit to the field builders.
        helpers_layout = QtWidgets.QVBoxLayout()
        logging_box_layout = QtWidgets.QVBoxLayout()

        font_layout.addWidget(self.font_select_button)
        font_layout.addWidget(self.font_label)
        
        self.status_label = QtWidgets.QLabel("Select master shapes to nest.")
        self.status_label.setWordWrap(True)

        label_options_layout = QtWidgets.QHBoxLayout()
        label_options_layout.addWidget(self.add_labels_checkbox)
        label_options_layout.addWidget(QtWidgets.QLabel("Size:"))
        label_options_layout.addWidget(self.label_size_input.widget())
        label_options_layout.addWidget(QtWidgets.QLabel("Height (Z):"))
        label_options_layout.addWidget(self.label_height_input.widget())
        label_options_layout.addStretch()

        # No unit in the row labels: the fields show their own unit inline,
        # and a label that says "mm" beside a field reading "23.62 in" is
        # worse than no label at all.
        #
        # Two columns, so width/height pair across the top and
        # thickness/spacing across the bottom. A sheet is described by its
        # width and height together, and stacking those two as rows one and two
        # read as two unrelated numbers; the same applies to the two numbers
        # that describe the gap between parts.
        form_layout.addRow(self._two_column_grid([
            ("Sheet Width:", self.sheet_width_input.widget()),
            ("Sheet Height:", self.sheet_height_input.widget()),
            ("Sheet Thickness:", self.sheet_thickness_input.widget()),
            ("Part Spacing:", self.part_spacing_input.widget()),
        ]))

        # Advanced Curve Settings. The Curve field is an ANGLE, not a length,
        # so it stays a plain QDoubleSpinBox with a degree suffix; Simplify is
        # a linear tolerance and is unit-aware.
        curve_settings_layout = QtWidgets.QHBoxLayout()
        curve_settings_layout.addWidget(QtWidgets.QLabel("Curve:"))
        curve_settings_layout.addWidget(self.deflection_input)
        curve_settings_layout.addWidget(QtWidgets.QLabel("Simplify:"))
        curve_settings_layout.addWidget(self.simplification_input.widget())
        
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
        self.helpers_group.addLayout(helpers_layout)

        logging_box_layout.addWidget(self.verbose_logging_checkbox)
        logging_box_layout.addWidget(self.performance_logging_checkbox)
        self.logging_group.addLayout(logging_box_layout)

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


    def _build_direction_grid(self, minkowski_dial_widget, mink_rot_layout):
        """The three-control arrangement at the top of Nesting Settings.

        Both arguments are locals owned further up _setup_ui, passed in rather
        than read off self: the dial's container, from _build_direction_control,
        and the slider-plus-readout row. Naming them is most of the point -- a
        reader previously had to scan eighty lines to learn that this block is
        handed its two inputs rather than building them.

        Kept whole rather than split at its comment boundaries. The five comment
        paragraphs are one argument -- why a QFormLayout could not express this,
        why the dial spans both rows, why column 0 is sized to the label and not
        the checkbox, the measured 348px/118px failure, why the dial gets a
        fixed width -- and cutting it would leave fragments citing a rationale
        stored somewhere else.

        Everything it creates is local to here and nothing is returned.

        The arrangement is asserted in probe_unit_panel's case_direction_dial:
        two columns, the checkbox at (0,0), the dial's container at (0,1)
        spanning both rows, and the rotation angle as ONE layout cell at (1,0).
        """
        # Layout of the three controls:
        #
        #     Use Random Direction  |   Nesting Direction
        #     ----------------------+   (dial, spanning both rows)
        #     Rotation Angle        |
        #
        # Built as a QGridLayout rather than the QFormLayout it replaces,
        # because QFormLayout cannot express this: its label column is
        # shared, so a labelled "Rotation Angle" row would start to the right of
        # the checkbox and run its slider underneath the dial. That is what the
        # first attempt did, and the two controls overlapped in width rather
        # than stacking in columns. A grid has a label cell per row and a
        # spanning cell for the dial, so the left column is genuinely its own
        # column and the slider cannot grow into the dial's space.
        #
        # The dial spans both rows on the right because it is the tallest of
        # the three by an order of magnitude. It defines the group's height, and
        # the two smaller controls stack alongside it instead of the group being
        # three rows tall.
        #
        # The checkbox sits above the slider because it qualifies the dial it
        # sits beside -- it decides whether the dial is used at all -- and the
        # rotation angle is the one control of the three that is independent of
        # both.
        #
        # Column 0 is sized to its widest cell, and the widest is the
        # "Use Random Direction" checkbox, not the "Rotation Angle:" label. That
        # left the left column far wider than either control needs and pushed
        # the slider right until it ran under the dial. Sizing the column to the
        # LABEL instead and letting the checkbox keep its own natural size
        # within it is what makes the two rows of column 1 start at the same x,
        # which is the whole point of the arrangement.
        direction_grid = QtWidgets.QGridLayout()
        direction_grid.setHorizontalSpacing(_GRID_COLUMN_SPACING)
        direction_grid.setVerticalSpacing(_GRID_ROW_SPACING)
        direction_grid.setContentsMargins(*MARGINS_NONE)

        # Column 0 is the whole left side -- the checkbox above, the rotation
        # angle below -- and the dial owns column 1 across both rows. The
        # rotation angle's label and slider are stacked in a VBox and placed as
        # ONE cell, which is what makes the left column a column: laid out as
        # separate cells the slider took the full width of the grid and ran
        # underneath the dial, because a slider has a large sizeHint and the
        # grid gives one cell whatever width it asks for.
        rotation_angle_column = QtWidgets.QVBoxLayout()
        rotation_angle_column.setContentsMargins(*MARGINS_NONE)
        rotation_angle_column.setSpacing(2)
        rotation_angle_label = QtWidgets.QLabel("Rotation Angle:")
        rotation_angle_label.setAlignment(
            QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        rotation_angle_column.addWidget(rotation_angle_label)
        rotation_angle_column.addLayout(mink_rot_layout)
        rotation_angle_column.addStretch()

        direction_grid.addWidget(self.minkowski_random_checkbox, 0, 0)
        _place(direction_grid, rotation_angle_column, 1, 0, 1)
        direction_grid.addWidget(minkowski_dial_widget, 0, 1, 2, 1)

        # Column widths. A QSlider's sizeHint is wide enough to claim the
        # panel on its own: measured, the left column took 348px and left the
        # dial 118px, because a grid gives column 0 every pixel the dial does
        # not ask for. setColumnMinimumWidth is a floor, not a ceiling, so it
        # does nothing here.
        #
        # The dial is instead given a FIXED width -- it is a circle, so it has
        # no reason to grow -- and column 0 takes what is left. A
        # maximumWidth on the slider's containing widget is what actually stops
        # it claiming the surplus, and the slider itself is told it may shrink,
        # so a narrow panel shortens the slider rather than clipping the dial.
        self.minkowski_rotation_steps_slider.setMinimumWidth(0)
        minkowski_dial_widget.setFixedWidth(_DIAL_COLUMN_WIDTH)
        direction_grid.setColumnStretch(0, 1)
        direction_grid.setColumnStretch(1, 0)
        # The dial is the tall one, so it decides the group's height. Give it
        # the slack in column 1 and let the left column's rows share what is
        # left, which is what the two row-stretches below ask for.
        direction_grid.setRowStretch(0, 1)
        direction_grid.setRowStretch(1, 1)
        self.minkowski_settings_group.addLayout(direction_grid)


    def _build_physics_section(self):
        """The Physics Nesting Settings section: dial, limits, and annealing.

        One method rather than a fields builder plus a form builder, because
        the block interleaves the two -- each widget is created and immediately
        added to the form, with spacer rows and the "--- Annealing (Shake) ---"
        separator sitting between them. Splitting it would mean either
        returning twenty widgets or rewriting it as two passes, which is churn
        on a block that is currently verified, to satisfy a line-count target.
        So it stays cohesive and runs slightly over budget.

        Returns nothing. physics_form_layout and physics_dial_widget are both
        created and consumed here, so there is nothing to hand back -- which is
        also why this cannot hit the bug the optimisation-fields builder did,
        where a documented return value was simply never written and the caller
        passed None into the layout.

        Must be called before _set_initial_section_visibility, which reads
        self.physics_settings_group.

        The dial itself is not here: _build_direction_control is shared with
        Minkowski, deliberately, so the two algorithms cannot diverge on the
        default, the label map or the step.
        """
        # Expanded, for the same reason as Optimizations: previously a plain
        # QGroupBox and therefore always visible.
        self.physics_settings_group = CollapsibleSection(
            "Physics Nesting Settings", expanded=True)
        physics_form_layout = QtWidgets.QFormLayout()

        # Direction Dial for Physics. Shares the default, the label map and the
        # step with the Minkowski dial above: the two are read by the same
        # conversion and a per-algorithm divergence here would be invisible
        # until someone ran the other algorithm and got a different nest.
        physics_dial_widget, self.physics_direction_dial, self.physics_direction_label = \
            self._build_direction_control()

        self.physics_random_checkbox = make_checkbox("Use Random Direction")
        self.physics_random_checkbox.stateChanged.connect(
            lambda state: self._set_direction_control_enabled(
                not self.physics_random_checkbox.isChecked(),
                dial=self.physics_direction_dial))

        self.physics_step_size_input = LengthField(mm_min=0.1, mm_max=100)
        self.physics_step_size_input.set_mm(_DEFAULTS["physics_step_size"])
        self._length_fields.append(self.physics_step_size_input)
        self.physics_max_spawn_input = make_int_spinbox(100, 1, 1000)
        self.physics_max_nesting_steps_input = make_int_spinbox(500, 1, 5000)
        
        # Annealing controls
        self.physics_anneal_steps_input = make_int_spinbox(25, 0, 500)
        self.anneal_rotate_checkbox = make_checkbox(
            "Anneal Rotate", checked=True)
        self.anneal_translate_checkbox = make_checkbox(
            "Anneal Translate", checked=True)
        self.anneal_random_shake_checkbox = make_checkbox(
            "Random Shake Direction")

        self.physics_anneal_rot_steps = make_int_spinbox(10, 0, 500)
        self.physics_anneal_rot_curve_type = QtWidgets.QComboBox()
        self.physics_anneal_rot_curve_type.addItems(["Logarithmic", "Linear", "Power 1.5", "Quadratic", "Exponential"])
        self.physics_anneal_rot_min = make_double_spinbox(1.0, 0.0, 360.0)
        self.physics_anneal_rot_max = make_double_spinbox(90.0, 0.0, 360.0)
        
        self.physics_anneal_curve_type = QtWidgets.QComboBox()
        self.physics_anneal_curve_type.addItems(["Logarithmic", "Linear", "Power 1.5", "Quadratic", "Exponential"])
        
        self.physics_anneal_min_amp = LengthField(mm_min=0.0, mm_max=1000.0)
        self.physics_anneal_min_amp.set_mm(_DEFAULTS["physics_anneal_min_amp"])
        self._length_fields.append(self.physics_anneal_min_amp)
        self.physics_anneal_max_amp = LengthField(mm_min=0.0, mm_max=5000.0)
        self.physics_anneal_max_amp.set_mm(_DEFAULTS["physics_anneal_max_amp"])
        self._length_fields.append(self.physics_anneal_max_amp)
        
        self.physics_improvement_threshold_input = make_double_spinbox(
            0.01, 0.000001, 1.0, step=0.01, decimals=6)
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

        physics_form_layout.addRow("Step Size:", self.physics_step_size_input.widget())
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
        physics_form_layout.addRow("Min Amplitude:", self.physics_anneal_min_amp.widget())
        physics_form_layout.addRow("Max Amplitude:", self.physics_anneal_max_amp.widget())
        physics_form_layout.addRow(self.anneal_random_shake_checkbox)

        self.physics_settings_group.addLayout(physics_form_layout)


    def _set_initial_section_visibility(self):
        """Which sections are on screen before the algorithm is chosen.

        Physics starts hidden because Minkowski is the default algorithm. This
        is the same setVisible pair _on_algorithm_change uses, so it is the
        algorithm toggle's starting state and nothing more -- it does not touch
        the collapsed state, which is set in each CollapsibleSection's
        constructor.
        """
        # Set initial visibility
        self.minkowski_settings_group.setVisible(True)
        self.minkowski_optimization_group.setVisible(True)
        self.physics_settings_group.setVisible(False)




    def _build_helper_fields(self):
        """The Helpers and Logging contents: labels, and the display switches.

        Both sections read unconditionally by NestingController whatever the
        algorithm is, which is why they sit apart from the algorithm-specific
        sections and are never hidden by the algorithm toggle.

        The two label LengthFields append to self._length_fields, so this must
        run after _build_length_fields has reassigned that list.
        """
        self.show_bounds_checkbox = make_checkbox("Show Bounds", checked=True)
        self.add_labels_checkbox = make_checkbox(
            "Add Identifier Labels", checked=_DEFAULTS["add_labels"])
        self.label_height_input = LengthField(mm_min=0, mm_max=1000)
        self.label_height_input.set_mm(_DEFAULTS["label_height"])
        self.label_height_input.widget().setToolTip("The height (Z-offset) for the identifier labels.")
        self._length_fields.append(self.label_height_input)
        self.label_size_input = LengthField(mm_min=1, mm_max=100)
        self.label_size_input.set_mm(_DEFAULTS["label_size"])
        self.label_size_input.widget().setToolTip("The text size for identifier labels.")
        self._length_fields.append(self.label_size_input)
        self.simulate_nesting_checkbox = make_checkbox(
            "Simulate Nesting (slower)",
            checked=_DEFAULTS["simulate_nesting"])
        self.verbose_logging_checkbox = make_checkbox(
            "Verbose Logging", checked=_DEFAULTS["verbose_logging"])
        self.verbose_logging_checkbox.setToolTip("Enables detailed logging of the nesting process in the FreeCAD console.")
        self.performance_logging_checkbox = make_checkbox(
            "Performance Logging",
            checked=_DEFAULTS["performance_logging"])
        self.performance_logging_checkbox.setToolTip("Enables performance and timing diagnostics in the FreeCAD console.")
        self.sound_checkbox = make_checkbox(
            "Play sound on completion", checked=True)
        


    def _build_action_buttons(self):
        """Run, cancel, and the parts-table add/remove pair.

        Cancel starts disabled; a run enables it. Kept apart from the helper
        fields because these are the panel's only controls that act rather than
        configure.
        """
        self.nest_button = QtWidgets.QPushButton("Run Nesting")
        self.cancel_button = QtWidgets.QPushButton("Cancel Nesting")
        self.cancel_button.setEnabled(False)

        self.add_parts_button = QtWidgets.QPushButton("Add Selected")
        self.remove_parts_button = QtWidgets.QPushButton("Remove Selected")
        


    def _build_length_fields(self):
        """The sheet dimensions, part spacing, deflection, and simplification.

        Four LengthFields and one angle spin box. The LengthFields go into
        self._length_fields, which refresh_unit_display walks to re-render
        every unit-bearing widget in the document's units -- so the list is
        built here and appended to by whichever builder owns each field.

        This block reassigns self._length_fields rather than appending to it, so
        it has to run before anything appends. It is the first builder called.

        The two tooltips held here are quoted by _refresh_unit_tooltips, which
        rewrites the parts that mention a measurement when the document's unit
        system changes -- which is why the simplify table stays in millimetres
        and only its closing recommendation follows the units.
        """
        # Every length in the panel is a LengthField: a unit-aware spin box
        # that shows the document's units and parses whatever FreeCAD's
        # quantity parser does ("1/2 in", "1' 11\"", "2ft 6in"), while the
        # value the rest of the workbench receives stays a plain millimetre
        # float. See length_field.py for why the value is kept in Python rather
        # than read back out of the widget.
        self.sheet_width_input = LengthField(mm_min=1, mm_max=10000)
        self.sheet_height_input = LengthField(mm_min=1, mm_max=10000)
        self.sheet_thickness_input = LengthField(mm_min=0.1, mm_max=1000)
        self.part_spacing_input = LengthField(mm_min=0, mm_max=1000)
        self.part_spacing_input.setToolTip(
            "<b>Part Spacing:</b><br>"
            "Clearance between part <b>outlines</b>.<br><br>"
            "Each outline is grown by half this value, so the gap left between "
            "two parts is this number.<br><br>"
            "<b>This has no knowledge of your tool.</b> A cutter is as wide as "
            "its diameter and swings beyond the outline it follows, so parts "
            "nearer together than the tool is wide will be cut into each other. "
            "Set this to at least the diameter of your widest tool.<br><br>"
            "<i>Measured on a 48-part nest at 4 mm spacing: the closest pair "
            "came out at 3.63 mm, so the result tracks this value closely but "
            "not exactly.</i>"
        )
        self.sheet_width_input.set_mm(_DEFAULTS["sheet_width"])
        self.sheet_height_input.set_mm(_DEFAULTS["sheet_height"])
        self.sheet_thickness_input.set_mm(_DEFAULTS["sheet_thickness"])
        self.part_spacing_input.set_mm(_DEFAULTS["part_spacing"])
        # Bounds above are the same ones the QDoubleSpinBoxes these replaced
        # had, so nothing that used to be rejected is now accepted.
        self._length_fields = [
            self.sheet_width_input, self.sheet_height_input,
            self.sheet_thickness_input, self.part_spacing_input,
        ]
        
        # Deflection is now specified as an angle (degrees) for more intuitive control
        # Internally converted to linear deflection: deflection_mm = angle / 200.0
        # 30 degrees by default, for faster processing than the 0.05mm linear
        # deflection this replaced.
        self.deflection_input = make_double_spinbox(
            _DEFAULTS["deflection_angle"], 1, 90, step=1, decimals=0,
            suffix="°")
        self.deflection_input.setToolTip(
            "<b>Curve Angle (Tessellation Quality):</b><br>"
            "Maximum angular deviation when approximating curves.<br><br>"
            "<b>Smaller (5-10°):</b> Smoother curves, more points, slower.<br>"
            "<b>Larger (20-45°):</b> Coarser curves, fewer points, faster.<br><br>"
            "<i>Tip: 10° is good for most parts. Use 5° for precision, 30°+ for speed.</i>"
        )
        
        self.simplification_input = LengthField(mm_min=0.001, mm_max=10.0, single_step_mm=0.1)
        self.simplification_input.set_mm(_DEFAULTS["simplification"])
        self._length_fields.append(self.simplification_input)
        self._simplification_tooltip = (
            "<b>Simplify (Point Reduction):</b><br>"
            "Tolerance for dropping boundary points. Larger = coarser, faster.<br><br>"
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
            "<b>Tip:</b> set this to your machine's precision tolerance "
            "({router_tolerance} for a router). Below ~0.25 it stops being worth "
            "the time unless a part has fine internal detail."
        )
        # The measured table above is quoted in millimetres because that is what
        # it was measured in, and it is left that way deliberately: the figures
        # are a record of an experiment, not a live readout. Only the tip's
        # recommendation follows the document's units.


    def _build_minkowski_ga_fields(self):
        """The three GA dials: population, generations, and a sheet target.

        Pure widget creation onto self. They are not added to a layout here --
        all three land in the Optimizations two-column grid further down, which
        is why the panel's field construction must all finish before its layout
        assembly begins.
        """
        # Genetic options for Minkowski
        self.minkowski_population_size_input = make_int_spinbox(1, 1, 500)
        self.minkowski_population_size_input.setToolTip("Set to 1 for a single pass. Increase with generations for Genetic Algorithm.")
        
        # 1 is the default: no genetic loop.
        self.minkowski_generations_input = make_int_spinbox(1, 1, 1000)
        self.minkowski_generations_input.setToolTip("Set to 1 for a single pass. Increase to optimize using Genetic Algorithm.")

        # Deliberately not persisted, like Generations and Population Size:
        # it is meaningless without a GA configuration next to it, and a stale
        # target armed against a default single-pass run is a surprise.
        # 0 = off, following the Rotation Threads "Auto" convention.
        self.minkowski_target_sheets_input = make_int_spinbox(0, 0, 100)
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



    def _build_optimization_fields(self):
        """Candidate-geometry cache, compactness weight, and the help button.

        Returns the compactness row's layout rather than storing it: it is
        consumed by exactly one caller, the Optimizations grid, and a builder
        that leaves its widgets on self but keeps its layout as a local is how
        a layout ends up owned by nobody.
        """
        self.candidate_geometry_cache_checkbox = make_checkbox(
            "Candidate Geometry Cache",
            checked=_DEFAULTS["candidate_geometry_cache"])
        self.candidate_geometry_cache_checkbox.setToolTip(
            "Caches translated candidate polygons within one nesting run. "
            "Collision and validity checks are still performed for every candidate."
        )

        self.minkowski_compactness_input = make_double_spinbox(
            0.0, 0.0, 10.0, step=0.1)
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

        mink_compactness_layout = QtWidgets.QHBoxLayout()
        mink_compactness_layout.addWidget(self.minkowski_compactness_input)
        mink_compactness_layout.addWidget(self.minkowski_compactness_help)
        mink_compactness_layout.addStretch()
        mink_compactness_layout.setContentsMargins(0, 0, 0, 0)
        return mink_compactness_layout



    def _build_minkowski_perf_dials(self):
        """Candidate step size and rotation thread count.

        The two largest levers in a run per RESULTS-parallelism.md, which is
        why they were promoted from environment variables to fields at all.

        `_step_size_tooltip` lives on self because _refresh_unit_tooltips reads
        it, and the LengthField is appended to _length_fields because
        refresh_unit_display walks that list rather than the widget tree.
        """
        # -- performance dials -------------------------------------------------
        # Both of these were environment variables and are now fields, because
        # the measurement says they are the two largest levers in a run and a
        # control nobody can reach is not a control. Defaults are unchanged, so
        # leaving them alone reproduces the previous behaviour exactly.
        self.minkowski_step_size_input = LengthField(mm_min=0.1, mm_max=100.0, single_step_mm=0.5)
        self.minkowski_step_size_input.set_mm(_DEFAULTS["minkowski_step_size"])
        self._length_fields.append(self.minkowski_step_size_input)
        self._step_size_tooltip = (
            "Spacing between candidate positions.<br><br>"
            "Every position a part can occupy comes from sampling the boundary "
            "of a No-Fit Polygon at this interval, and a boundary of length L "
            "yields about L / step positions. Each one is then tested against "
            "the parts already placed, so the total number of collision tests "
            "scales with this value and it is the main control over how long a "
            "run takes.<br><br>"
            "Larger: fewer positions, faster, and coarser packing.<br>"
            "Smaller: more positions, slower, and finer packing.<br><br>"
            "Only affects jobs that need positions away from the sheet corners "
            "and edges. Where parts simply line up against each other or the "
            "sheet border, the extra positions are tested and discarded and "
            "changing this has no visible effect on the result. Where parts "
            "interlock or have curved or closely spaced features, it does.<br><br>"
            "Below: the same part offered to the same L-shaped neighbour. Left, "
            "candidate points every 5 mm and the part fits the corner. Right, "
            "every 20 mm, too few land inside the corner, and the part "
            "overhangs instead.<br><br>"
            "If you raise it, check that the packing and the number of sheets "
            "are unchanged before keeping the change."
        )

        self.minkowski_rotation_workers_input = make_int_spinbox(0, 0, 64)
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



    def refresh_unit_display(self, force=False):
        """Re-render every unit-aware field for the active document.

        Called when the panel is built, when the user switches document (via
        the controller's document observer), and immediately before a run --
        see the note on the last of those below.

        The fields hold millimetres, so this cannot lose a value; the widget
        inside each one re-reads the document's unit system when it is handed
        a new figure, and re-seeding it is the whole of the re-render. Cheap
        enough to be unconditional, but it returns early when the schema has
        not moved so that the common case does no work at all.
        """
        doc = getattr(self.controller, "doc", None) or FreeCAD.ActiveDocument
        schema = units.doc_unit_schema(doc)
        if not force and schema == self._rendered_schema:
            return
        self._rendered_schema = schema
        for field in self._length_fields:
            field.refresh()
        self._refresh_unit_tooltips(schema)

    def _refresh_unit_tooltips(self, schema):
        """Rebuild the tooltips whose text quotes a measurement in mm.

        Only two, and only because both quote a number the user is meant to
        compare against their own machine. The rest of the panel's help text
        deliberately names no unit at all, because the field shows its unit
        inline and a tooltip asserting "mm" beside a field reading "0.49 in"
        is simply wrong.
        """
        try:
            self.simplification_input.widget().setToolTip(
                self._simplification_tooltip.format(
                    router_tolerance=units.format_length(1.0, schema)))
            self.minkowski_step_size_input.widget().setToolTip(
                # Built here rather than at construction, because this tooltip
                # is re-set on every unit change and a string captured once
                # would go stale against whatever else this method does. The
                # diagram goes through `tooltip_with_image` for its path
                # handling and popup width; the prose is unchanged.
                tooltip_with_image(
                    self._step_size_tooltip,
                    os.path.join(TOOLTIPS_DIR, _CANDIDATE_STEP_DIAGRAM),
                    _CANDIDATE_STEP_DIAGRAM_WIDTH))
        except RuntimeError:
            pass  # Panel closed; the widgets are gone.

    def dispose(self):
        """Release anything the panel registered outside itself.

        Reached from NestingTaskPanel.cleanup, which is the one teardown path
        every close route goes through. The document observer holds a raw
        pointer to a Python object on the C++ side, so leaving it registered
        after the panel is collected is a crash rather than a leak.
        """
        dispose = getattr(self.controller, "dispose", None)
        if dispose is not None:
            try:
                dispose()
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    f"[NestingPanel] Controller teardown failed: {exc}\n")

    # -- Two-column form grid --------------------------------------------
    #
    # Built by a method for the same reason the direction control is: it is
    # used by two groups, and a layout written out twice is two layouts free to
    # drift. Both uses are the panel's densest blocks of labelled fields, and
    # both are halved in height by pairing them.
    #
    # Entries fill left to right, then down. A None label means the widget
    # carries its own text (a checkbox) and should occupy the whole of its
    # half-row rather than sit in a field column beside an empty label.

    def _two_column_grid(self, entries):
        """Lay ``entries`` out as two label/field columns, filling row-major.

        Args:
            entries: list of (label, widget) pairs, or (None, widget) for a
                widget that spans both cells of its column. Flattened into
                one widget per column-pair, so a trailing odd entry still works
                (it just leaves the last cell empty) rather than being dropped.

        Returns:
            The QGridLayout, ready to hand to a group or addRow.
        """
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(_GRID_COLUMN_SPACING)
        grid.setVerticalSpacing(_GRID_ROW_SPACING)

        for index, (label, widget) in enumerate(entries):
            row, column = divmod(index, 2)
            if label is None:
                # No label of its own: let the widget take the full width of
                # this column so it reads as a checkbox, not a field with a
                # blank caption.
                _place(grid, widget, row, column * 2, 2)
                continue
            text = QtWidgets.QLabel(label)
            # Right-aligned with a trailing colon, matching the QFormLayout
            # rows elsewhere in the panel, so the two styles read as one.
            text.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            grid.addWidget(text, row, column * 2)
            _place(grid, widget, row, column * 2 + 1, 1)

        # Fields absorb the slack; the label columns stay at their natural
        # width, so the two field columns end up the same size and the rows
        # line up across the pair.
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        return grid

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
        """Dial + readout, no surrounding buttons.

        Returns (container_widget, dial, label) so the caller can wire the
        "Use Random Direction" checkbox to disable the whole control.

        The four cardinal buttons are gone. They existed to snap the dial to
        the four directions DIRECTION_LABELS names, which is a job a 15-degree
        step does better: 24 positions include all four cardinals, so nothing
        is lost by dropping the buttons, and the grid they sat in was three
        rows tall for a control the panel has to fit above a table.

        The readout is a direction NAME where the bearing lands on a cardinal
        and a bearing in degrees otherwise -- NOT the dial reading. The two
        differ by a quarter turn and a flip (see constants.dial_to_bearing), so
        a raw reading would have told the user 90 where the run searched 180.
        That was survivable when the only reachable values were the four the
        buttons snapped to, and wrong at 20 of the 24 positions now reachable.
        """
        dial = QtWidgets.QDial()
        dial.setRange(0, _MINKOWSKI_DIR_MAX)
        dial.setValue(_DEFAULT_DIRECTION_DIAL)
        dial.setWrapping(True)
        dial.setNotchesVisible(True)
        # Both, not just singleStep: a mouse click and an arrow key are the
        # same gesture to the user, and the default page step of 10 would skip
        # past a 15-degree grid and land somewhere that is not a step.
        dial.setSingleStep(DIRECTION_STEP_DEGREES)
        dial.setPageStep(DIRECTION_STEP_DEGREES)
        dial.setToolTip(
            f"Nesting direction. Click or use the arrow keys to move in "
            f"{DIRECTION_STEP_DEGREES}° steps.\n\n"
            f"The dial reading is not the compass bearing -- it is rotated a "
            f"quarter turn and flipped. The readout below gives the direction "
            f"the run will actually search.")

        label = QtWidgets.QLabel("")
        label.setAlignment(QtCore.Qt.AlignCenter)

        def update_label(value):
            bearing = dial_to_bearing(value)
            name = _BEARING_LABELS.get(bearing, "")
            label.setText(f"{name} ({bearing}°)" if name else f"{bearing}°")
        dial.valueChanged.connect(update_label)
        update_label(_DEFAULT_DIRECTION_DIAL)

        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(dial)
        layout.addWidget(label)
        container = QtWidgets.QWidget()
        container.setLayout(layout)
        return container, dial, label


    def _set_direction_control_enabled(self, enabled, dial=None):
        """Enable or disable a direction control as a unit.

        Called by both "Use Random Direction" checkboxes, each passing its own
        dial, and by the two restore paths. `dial` is effectively required now:
        the only caller that used to omit it was the preference restore, and it
        therefore greyed out the Minkowski dial whatever algorithm was selected.

        There used to be a button list here as well. Greying out the dial alone
        is not enough while the surrounding buttons stayed live, because a user
        could then set a direction that the run ignores -- which reads as a bug
        in the run rather than in the UI. With the buttons gone the dial is the
        only way to set a direction, so disabling it is sufficient.
        """
        if dial is None:
            dial = self.minkowski_direction_dial
        dial.setEnabled(enabled)

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
        """Loads settings from FreeCAD preferences.

        The length settings are read and written as bare millimetre floats and
        stay that way on purpose. Preferences outlive any one document, so a
        schema that is imperial today and metric next week must not leave a
        user's 600 mm sheet stored as 23.62 -- the stored figure is the
        document-independent one, and the fields convert for display.
        """
        prefs = FreeCAD.ParamGet(PREFS_PATH)
        self.sheet_width_input.set_mm(prefs.GetFloat(PROP_SHEET_WIDTH, 600.0))
        self.sheet_height_input.set_mm(prefs.GetFloat(PROP_SHEET_HEIGHT, 600.0))
        self.part_spacing_input.set_mm(prefs.GetFloat(PROP_PART_SPACING, 12.5))
        self.sheet_thickness_input.set_mm(prefs.GetFloat(PROP_SHEET_THICKNESS, 3.0))
        self.label_size_input.set_mm(prefs.GetFloat(PROP_LABEL_SIZE, 10.0))
        self.deflection_input.setValue(prefs.GetFloat(PROP_DEFLECTION_ANGLE, 30.0) or 30.0)
        self.simplification_input.set_mm(prefs.GetFloat(PROP_SIMPLIFICATION, 1.0))
        self.minkowski_compactness_input.setValue(prefs.GetFloat("GACompactnessWeight", 0.0))
        self.verbose_logging_checkbox.setChecked(prefs.GetBool("VerboseLogging", False))
        self.performance_logging_checkbox.setChecked(prefs.GetBool("PerformanceLogging", False))
        self.candidate_geometry_cache_checkbox.setChecked(
            prefs.GetBool("CandidateGeometryCache",
                               CANDIDATE_GEOMETRY_CACHE_DEFAULT)
        )
        self.physics_improvement_threshold_input.setValue(prefs.GetFloat("PhysicsStabilityTolerance", 0.01))

        self.physics_anneal_curve_type.setCurrentText(prefs.GetString("PhysicsAnnealCurveType", "Logarithmic"))
        self.physics_anneal_min_amp.set_mm(prefs.GetFloat("PhysicsAnnealMinAmp", 0.1))
        self.physics_anneal_max_amp.set_mm(prefs.GetFloat("PhysicsAnnealMaxAmp", 100.0))

        self.physics_anneal_rot_steps.setValue(prefs.GetInt("PhysicsAnnealRotSteps", 10))
        self.physics_anneal_rot_curve_type.setCurrentText(prefs.GetString("PhysicsAnnealRotCurveType", "Logarithmic"))
        self.physics_anneal_rot_min.setValue(prefs.GetFloat("PhysicsAnnealRotMin", 1.0))
        self.physics_anneal_rot_max.setValue(prefs.GetFloat("PhysicsAnnealRotMax", 90.0))

        # Generations and Population Size are persisted so a GA configuration
        # survives closing the panel, which it did not: both used to reset to
        # 1 every session unless you reopened a layout that had recorded them.
        #
        # The default stays 1. Measured on the heavy corpus at a contested sheet
        # size and on the n70 customer part, population 1/3/4/10 all return the
        # same sheet count, placed count and density while wall clock goes 18s
        # -> 86s and 49s -> 197s. Layout 0 is exempt from the population's
        # shuffle-and-rotate (layout_manager.create_ga_population), so it is
        # the best layout every time and best-of-N converges on it; crossover
        # does run (offspring 0 -> 2 -> 16 across those populations). Raising
        # the default would multiply the cost of everyone's first run for no
        # measured gain, so it is left where it is until the search strategy
        # makes a larger population worth setting.
        self.minkowski_generations_input.setValue(
            prefs.GetInt(PROP_GENERATIONS, 1))
        self.minkowski_population_size_input.setValue(
            prefs.GetInt(PROP_POPULATION_SIZE, 1))

        # Direction, as a DIAL reading and not a bearing -- the same form the
        # layout group stores it in, and the form dial_to_bearing converts.
        # Snapped to the step, because a stored value from a build with a
        # different step would otherwise land between notches and silently
        # disagree with the readout the user is looking at.
        #
        # **Both dials, each from its own key.** The Physics dial had no key and
        # so was never restored -- its position was recorded nowhere at all, while
        # `PhysicsRandomDirection` and `PhysicsRotationSteps` were remembered all
        # along. See issues.md NEST-001.
        self.minkowski_direction_dial.setValue(
            _snap_to_step(prefs.GetInt(PROP_NESTING_DIRECTION,
                                       _DEFAULT_DIRECTION_DIAL)))
        self.physics_direction_dial.setValue(
            _snap_to_step(prefs.GetInt(PROP_PHYSICS_DIRECTION,
                                       _DEFAULT_DIRECTION_DIAL)))
        self.minkowski_random_checkbox.setChecked(
            prefs.GetBool(PROP_RANDOM_DIRECTION, False))
        self.physics_random_checkbox.setChecked(
            prefs.GetBool("PhysicsRandomDirection", False))
        # Restored after both checkboxes, and against the **active** algorithm's
        # checkbox rather than the Minkowski one as it did before: this call was
        # made with no dial, so `_set_direction_control_enabled` fell back to the
        # Minkowski dial whichever algorithm was selected. Harmless at startup,
        # where the algorithm defaults to Minkowski -- and wrong for every session
        # that opens with Physics selected from a layout.
        active_random = (self.physics_random_checkbox.isChecked()
                         if self.algorithm_dropdown.currentText() == 'Physics'
                         else self.minkowski_random_checkbox.isChecked())
        active_dial = (self.physics_direction_dial
                       if self.algorithm_dropdown.currentText() == 'Physics'
                       else self.minkowski_direction_dial)
        self._set_direction_control_enabled(not active_random, dial=active_dial)

        # Load Rotation Steps (Isolated)
        # Minkowski
        self.minkowski_step_size_input.set_mm(
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
        
