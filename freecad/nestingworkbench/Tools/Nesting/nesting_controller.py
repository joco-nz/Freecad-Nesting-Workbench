# SPDX-License-Identifier: LGPL-2.1-or-later

import FreeCAD
import FreeCADGui
import Part
import os
import time
import math
import threading
from PySide import QtWidgets
from PySide.QtCore import QThread, Signal
from ...datatypes.shape import Shape
from .shape_preparer import ShapePreparer
from .algorithms import minkowski_utils
from .layout_manager import LayoutManager, Layout
from .ga_coordinator import GACoordinator
from ...freecad_helpers import recursive_delete, set_visibility
from ...constants import *
from ... import units
from ...length_field import LengthField
from .nesting_job import NestingJob
from ... import DEFAULT_FONT

try:
    from .nesting_logic import nest, NestingDependencyError
    from .visualization_manager import VisualizationManager
except ImportError:
    pass  # Optional dependency missing, proceed without visualization

# How long a handover to the main thread may take before the worker gives up on
# it. Generous, because drawing a layout or recomputing a document is real work,
# but finite, because an unbounded wait on a shared event is a hang.
_DRAW_HANDOVER_TIMEOUT_S = 300.0


class _DocumentUnitWatcher:
    """Notifies the controller when the user switches document.

    FreeCAD exposes addDocumentObserver/removeDocumentObserver and calls
    ``slotActivateDocument`` on a duck-typed Python object -- verified, and the
    only document event that is dependable from Python. Nothing fires for a
    change to the active document's UnitSystem, which is why the controller
    also re-checks the schema just before a run.

    Kept as a separate object rather than a bound method on the controller
    because the registration is a raw pointer on the C++ side: if the object
    were collected while still registered, the next activation would call into
    freed memory. The controller holds it as an attribute and drops the
    registration in dispose(), which NestingTaskPanel.cleanup always reaches.
    """

    def __init__(self, on_activate):
        self._on_activate = on_activate

    def slotActivateDocument(self, doc):
        try:
            self._on_activate(doc)
        except Exception as exc:
            # Never let an exception escape into FreeCAD's C++ activation
            # path; it would abort the document switch itself.
            FreeCAD.Console.PrintWarning(
                f"[NestingController] Document unit refresh failed: {exc}\n")


class NestingWorker(QThread):
    """Runs nesting computation on a background thread.
    
    Communicates with main thread via Qt signals for:
    - Status/progress updates (non-blocking)
    - Document operations like sheet.draw() (blocking synchronization)
    - Completion/error reporting
    """
    status_changed = Signal(str)
    progress_updated = Signal(int, int, str)   # current, total, message
    draw_requested = Signal(object)             # payload dict for main-thread drawing
    finished_signal = Signal(object)            # NestingJob result (or None)
    error_signal = Signal(str)                  # error message + traceback
    
    def __init__(self, coordinator, run_args, cancel_check_fn, parent=None):
        """
        Args:
            coordinator: GACoordinator instance
            run_args: tuple of (target_layout, ui_params, quantities, master_map,
                      rotation_params, algo_kwargs, is_simulating, viz_manager)
            cancel_check_fn: callable that returns True if cancel requested
        """
        super().__init__(parent)
        self.coordinator = coordinator
        self.run_args = run_args
        self.cancel_check_fn = cancel_check_fn
        self._draw_event = threading.Event()
    
    def run(self):
        """Execute nesting on worker thread."""
        try:
            (target_layout, ui_params, quantities, master_map,
             rotation_params, algo_kwargs, is_simulating, viz_manager) = self.run_args
            
            job = self.coordinator.run(
                target_layout, ui_params, quantities, master_map,
                rotation_params, algo_kwargs, is_simulating, viz_manager=viz_manager
            )
            self.finished_signal.emit(job)
        except Exception as e:
            import traceback
            self.error_signal.emit(f"{e}\n{traceback.format_exc()}")
    
    def request_draw_on_main_thread(self, payload, timeout=None):
        """Called from the worker; hand work to the main thread and wait for it.

        Returns True if the main thread acknowledged, False if it did not.

        The wait is bounded. An unbounded wait on a single event shared by
        every handover is a freeze waiting to happen: if the acknowledgement is
        lost -- the handler raises, the panel is torn down mid-flight, the
        coordinator is replaced -- this thread never returns, FreeCAD stops
        responding, and the only remedy is killing the process. That is not a
        hypothetical; it is what a report of "FreeCAD locked and became
        unresponsive" looks like from the inside.

        A timeout converts that into a visible error and an unwinding run.
        Default is generous, because a legitimate handover -- drawing a whole
        layout, or finalize plus a document recompute -- can be slow.
        """
        self._draw_event.clear()
        self.draw_requested.emit(payload)
        limit = _DRAW_HANDOVER_TIMEOUT_S if timeout is None else timeout
        if not self._draw_event.wait(limit):
            FreeCAD.Console.PrintError(
                f"[Nesting] Main thread did not acknowledge a "
                f"{sorted(k for k in payload if k not in ('verbose',))} request "
                f"within {limit:.0f}s. Continuing without it; the run may be "
                f"incomplete. This used to block forever.\n")
            return False
        return True
    
    def notify_draw_complete(self):
        """Called from main thread after draw finishes."""
        self._draw_event.set()

class NestingController:
    """
    Main controller for the Nesting Workbench.
    Handles UI interaction, Job creation, and Layout management.
    """
    def __init__(self, ui_panel):
        self.ui = ui_panel
        self.doc = FreeCAD.ActiveDocument
        self.current_job = None
        self.shape_preparer = ShapePreparer(self.doc, {})
        self.is_running = False
        self.cancel_requested = False
        self._saved_source_placements = []

        self.viz_manager = VisualizationManager()

        # Initialize default fonts
        default_font = DEFAULT_FONT
        self.ui.selected_font_path = default_font
        if hasattr(self.ui, 'font_label'):
            self.ui.font_label.setText(os.path.basename(default_font))

        # Watch for document switches so the panel's length fields can follow
        # the new document's unit system. See _DocumentUnitWatcher.
        self._unit_watcher = _DocumentUnitWatcher(self._on_activate_document)
        try:
            FreeCAD.addDocumentObserver(self._unit_watcher)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"[NestingController] Could not watch for document changes; "
                f"units will not follow a document switch: {exc}\n")
            self._unit_watcher = None

    def dispose(self):
        """Unregister the document observer. Idempotent.

        Called from NestingTaskPanel.cleanup. Must run while the panel is still
        alive: FreeCAD holds the observer as a raw pointer and calls back into
        it without consulting Python's garbage collector.
        """
        watcher = getattr(self, "_unit_watcher", None)
        if watcher is None:
            return
        self._unit_watcher = None
        try:
            FreeCAD.removeDocumentObserver(watcher)
        except Exception as exc:
            FreeCAD.Console.PrintWarning(
                f"[NestingController] Could not unregister the document "
                f"observer: {exc}\n")

    def _on_activate_document(self, doc):
        """The user switched documents: follow the new one's units.

        The panel is cached for the life of the command (see
        NestingCommand._task_panel), so it outlives a switch between two open
        documents and would otherwise keep resolving against the first one --
        which is how shapes from one document end up nested into another.

        A run already in flight is left alone. Its shapes, placements and
        target group all belong to the document it started in, and re-pointing
        self.doc at another one mid-run would only make that worse; the next
        run picks the new document up.
        """
        if self.is_running:
            FreeCAD.Console.PrintWarning(
                "[NestingController] Document switched while a nest was in "
                "flight. This run continues in the document it started in.\n")
            return
        # Compared by name, not identity. FreeCAD hands the observer a
        # Document across the C++/Python boundary, which is a fresh wrapper
        # each time -- `is` never matches, not even for the document already
        # open. So an identity test here would miss every same-document
        # re-activation and drop the user's shape table for nothing.
        if getattr(doc, "Name", None) == getattr(self.doc, "Name", None):
            return

        self.doc = doc
        # Anything the panel is holding belongs to the document we just left.
        # FreeCAD owns those objects' lifetime, so they cannot be restored into
        # a new document -- and worse, adding them to one would fail. Drop the
        # references and let the panel load whatever the new document has
        # selected.
        self._drop_document_state()
        self._safe_ui(self.ui.refresh_unit_display, True)

    def _drop_document_state(self):
        """Forget the shapes and layout of the document we have left."""
        for obj in list(getattr(self.ui, "hidden_originals", ()) or ()):
            try:
                set_visibility(obj, True)
            except Exception:
                pass  # The object died with its document; nothing to restore.
        self._saved_source_placements = []
        self.current_job = None
        self.ui.hidden_originals = []
        self.ui.selected_shapes_to_process = []
        self.ui.current_layout = None
        try:
            self.ui.shape_table.setRowCount(0)
        except RuntimeError:
            pass  # Panel closed.
        if self.doc is not None:
            try:
                self.ui.status_label.setText(
                    "Document changed. Select the shapes to nest.")
            except RuntimeError:
                pass
        self._safe_ui(self.ui.update_progress, 0, 0)

    def execute_nesting(self):
        FreeCAD.Console.PrintMessage("\n--- NESTING START ---\n")

        # Re-render the length fields against the document as it is right now.
        # The document observer covers a document switch, but FreeCAD raises
        # no event when the active document's UnitSystem itself changes, and
        # this is the last point at which a stale unit can reach the run --
        # the values read below are already millimetres, so what is actually
        # at risk is the user reading a sheet size off the screen that is not
        # the one about to be nested.
        self._safe_ui(self.ui.refresh_unit_display)

        self._restore_source_placements()
        if self.current_job:
            self.current_job.cleanup()
            self.current_job = None

        self._prepare_source_parts()
        Shape.clear_caches()
        # Once per run, not once per layout: the decomposition work the
        # dead-ring counters describe only happens while the NFP cache is cold.
        minkowski_utils.reset_dead_ring_stats()

        target_layout = self._ensure_target_layout()
        if not target_layout:
             self._restore_source_placements()
             return # Standard error handled in helper
             
        is_simulating = self.ui.simulate_nesting_checkbox.isChecked()
        set_visibility(target_layout, False)

        ui_params = self._collect_ui_params()
        ui_params, quantities, master_map, rotation_params = self._collect_job_parameters(ui_params)
        
        algo_kwargs = self._prepare_algo_kwargs(ui_params)
        verbose = self.ui.verbose_logging_checkbox.isChecked()
        performance_logging = self.ui.performance_logging_checkbox.isChecked()
        algo_kwargs['verbose'] = verbose
        algo_kwargs['performance_logging'] = performance_logging
        
        rot_steps = ui_params.get('rotation_steps', 1)
        ann_steps = algo_kwargs.get('anneal_steps', 25) if ui_params.get('algorithm') == 'Physics' else 0
        FreeCAD.Console.PrintMessage(f"Algorithm Selected: {ui_params.get('algorithm', 'Unknown')}\n")
        FreeCAD.Console.PrintMessage(f"  -> UI Rotation Steps Slider: {rot_steps}\n")
        FreeCAD.Console.PrintMessage(f"  -> UI Anneal Steps Input: {ann_steps}\n")
        
        algo_kwargs['cancel_callback'] = self._check_cancel
        
        prefs = FreeCAD.ParamGet(PREFS_PATH)
        prefs.SetBool("VerboseLogging", verbose)
        prefs.SetBool("PerformanceLogging", performance_logging)
        
        
        def progress_cb(current, total, message=None):
            try:
                self.ui.update_progress(current, total, message)
            except RuntimeError: pass  # Widget deleted (panel closed)
            
        self.ui.reset_progress()
        algo_kwargs['progress_callback'] = progress_cb
        
        self.is_running = True
        self.cancel_requested = False
        self.ui.nest_button.setEnabled(False)
        self.ui.cancel_button.setEnabled(True)
        self._execute_ga_nesting(target_layout, ui_params, quantities, master_map, 
                                 rotation_params, algo_kwargs, is_simulating, self.viz_manager)

    def _prepare_source_parts(self):
        """Save, hide, and normalize selected source parts before copying."""
        saved = []
        seen = set()
        for obj in getattr(self.ui, "selected_shapes_to_process", []):
            if obj is None or id(obj) in seen or not hasattr(obj, "Placement"):
                continue
            seen.add(id(obj))
            saved.append((obj, FreeCAD.Placement(obj.Placement)))
            obj.Placement = FreeCAD.Placement()
            set_visibility(obj, False)
        self._saved_source_placements = saved
        if saved:
            self.doc.recompute()

    def _restore_source_placements(self):
        """Restore source placements after preparation and nesting complete."""
        if not self._saved_source_placements:
            return
        saved = self._saved_source_placements
        self._saved_source_placements = []
        for obj, placement in saved:
            try:
                obj.Placement = placement
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    f"[NestingController] Could not restore placement for "
                    f"'{getattr(obj, 'Label', obj)}': {exc}\n"
                )
        self.doc.recompute()
    
    def load_selection(self):
        FreeCAD.Console.PrintMessage("Loading selection via Controller...\n")
        selection = FreeCADGui.Selection.getSelection()
        self.ui.shape_table.setRowCount(0)

        if not selection:
            FreeCAD.Console.PrintMessage("  -> No selection found.\n")
            self.ui.status_label.setText("Warning: No shapes selected.")
            self.ui.nest_button.setEnabled(False)
            return

        first_selected = selection[0]
        if first_selected.isDerivedFrom("App::DocumentObjectGroup") and first_selected.Label.startswith("Layout_"):
            FreeCAD.Console.PrintMessage(f"  -> Detected layout selection: {first_selected.Label}\n")
            self.load_layout(first_selected)
        else:
            FreeCAD.Console.PrintMessage(f"  -> Detected {len(selection)} shapes.\n")
            self.load_shapes(selection)

    def load_layout(self, layout_group):
        """Loads the parameters and shapes from a layout group."""
        self.ui.current_layout = layout_group
        self.ui.nest_button.setEnabled(True)
        self.ui.selected_shapes_to_process = []
        self.ui.hidden_originals = []

        self._load_params_from_layout(layout_group)
        
        self._load_shapes_from_layout(layout_group)

    def _load_params_from_layout(self, layout_group):
        """Extracts algorithm parameters from layout properties.

        The length properties are App::PropertyLength, so what comes back is
        a Quantity. float() on one of those is its millimetre figure, which is
        the unit the fields want -- there is no schema anywhere in this path,
        and none is needed.
        """
        props_map = {
            PROP_SHEET_WIDTH: self.ui.sheet_width_input,
            PROP_SHEET_HEIGHT: self.ui.sheet_height_input,
            PROP_PART_SPACING: self.ui.part_spacing_input,
            PROP_SHEET_THICKNESS: self.ui.sheet_thickness_input,
            PROP_SIMPLIFICATION: self.ui.simplification_input,
            PROP_LABEL_SIZE: self.ui.label_size_input,
            PROP_GENERATIONS: self.ui.minkowski_generations_input,
            PROP_POPULATION_SIZE: self.ui.minkowski_population_size_input,
            PROP_NESTING_DIRECTION: self.ui.minkowski_direction_dial,
        }

        for prop, widget in props_map.items():
            val = getattr(layout_group, prop, None)
            if val is None:
                continue
            if isinstance(widget, LengthField):
                # length_mm refuses a property that is not actually a length
                # rather than adopting whatever figure it carries -- a
                # hand-edited document can put anything under that name, and
                # float() on a non-Length would be a number in the wrong unit.
                mm = units.length_mm(val)
                if mm is not None:
                    widget.set_mm(mm)
            else:
                widget.setValue(val)
            
        deflection_angle = getattr(layout_group, PROP_DEFLECTION_ANGLE, None)
        if deflection_angle is not None:
            self.ui.deflection_input.setValue(deflection_angle)
        elif hasattr(layout_group, 'Deflection'):
            self.ui.deflection_input.setValue(layout_group.Deflection * 200.0)
            

        
        font_path = getattr(layout_group, PROP_FONT_FILE, None)
        if font_path and os.path.exists(font_path):
            self.ui.selected_font_path = font_path
            self.ui.font_label.setText(os.path.basename(font_path))

        steps = getattr(layout_group, PROP_GLOBAL_ROTATION_STEPS, 0)
        if steps > 0:
            target_angle = 360.0 / steps
            algo = getattr(layout_group, "Algorithm", "Minkowski")
            self.ui.algorithm_dropdown.setCurrentText(algo)
            
            angles = PHYSICS_ROTATION_PRESETS if algo == "Physics" else self.ui.rotation_angles
            slider = self.ui.physics_rotation_steps_slider if algo == "Physics" else self.ui.minkowski_rotation_steps_slider
            
            closest_idx = 0
            min_diff = float('inf')
            for i, angle in enumerate(angles):
                diff = abs(angle - target_angle)
                if diff < min_diff:
                    min_diff, closest_idx = diff, i
            slider.setValue(closest_idx)

    def _load_shapes_from_layout(self, layout_group):
        """Identifies master shapes and their quantities/overrides."""
        master_shapes_group = next((c for c in layout_group.Group if c.Label.startswith("MasterShapes")), None)
        
        if not master_shapes_group:
            FreeCAD.Console.PrintWarning(f"  WARNING: No MasterShapes group found in '{layout_group.Label}'\n")
            self.ui.status_label.setText("Warning: Could not find 'MasterShapes' group.")
            return

        shapes_to_load = []
        quantities, overrides, steps_map, up_dirs, fill_map = {}, {}, {}, {}, {}
        
        for master in master_shapes_group.Group:
            if not hasattr(master, "Group"): continue
            
            shape_obj = next((child for child in master.Group if child.Label.startswith("master_shape_")), None)
            if shape_obj and hasattr(shape_obj, "Shape"):
                shapes_to_load.append(shape_obj)
                label = shape_obj.Label
                
                quantities[label] = getattr(master, "Quantity", 1)
                
                overrides[label] = getattr(master, "PartRotationOverride", [])
                steps_map[label] = getattr(master, "PartRotationSteps", 0)
                up_dirs[label] = getattr(master, "UpDirection", "Z+")
                fill_map[label] = getattr(master, "FillSheet", False)
        
        self.load_shapes(
            shapes_to_load, is_reloading_layout=True, initial_quantities=quantities,
            initial_overrides=overrides, initial_rotation_steps=steps_map,
            initial_up_directions=up_dirs, initial_fill_sheet=fill_map
        )

    def _extract_parts_from_selection(self, selection):
        """
        Extracts parts from Assembly containers only.
        Regular Part objects are used directly without extracting children.
        """
        parts = []
        
        def is_assembly(obj):
            """Check if object is an Assembly container (not a regular Part/Body)."""
            type_id = obj.TypeId if hasattr(obj, 'TypeId') else ''
            if 'Assembly' in type_id:
                return True
            if type_id == 'App::Part' and hasattr(obj, 'Group'):
                for child in obj.Group:
                    child_type = child.TypeId if hasattr(child, 'TypeId') else ''
                    if 'Link' in child_type or 'Assembly' in child_type:
                        return True
            return False
        
        def extract_from_assembly(obj):
            """Recursively extract nestable parts from an assembly."""
            if hasattr(obj, 'Group'):
                for child in obj.Group:
                    child_type = child.TypeId if hasattr(child, 'TypeId') else ''
                    if 'Constraint' in child_type or 'Origin' in child_type:
                        continue
                    if hasattr(child, 'LinkedObject') and child.LinkedObject:
                        linked = child.LinkedObject
                        if hasattr(linked, 'Shape') and linked.Shape and not linked.Shape.isNull():
                            parts.append(linked)
                    elif hasattr(child, 'Shape') and child.Shape and not child.Shape.isNull():
                        if is_assembly(child):
                            extract_from_assembly(child)
                        else:
                            parts.append(child)
        
        for obj in selection:
            if is_assembly(obj):
                extract_from_assembly(obj)
            else:
                parts.append(obj)
        
        return parts

    def load_shapes(self, selection, is_reloading_layout=False, initial_quantities=None, 
                     initial_overrides=None, initial_rotation_steps=None,
                     initial_up_directions=None, initial_fill_sheet=None):
        """Loads a selection of shapes into the UI."""
        self.ui.nest_button.setEnabled(True)
        
        selection_counts = {}

        if not is_reloading_layout:
            extracted = self._extract_parts_from_selection(selection)
            if extracted:
                # Count occurrences before deduping
                for obj in extracted:
                    if obj in selection_counts:
                        selection_counts[obj] += 1
                    else:
                        selection_counts[obj] = 1
                
                selection = extracted
                FreeCAD.Console.PrintMessage(f"  -> Extracted {len(selection)} parts from selection.\n")
        
        self.ui.selected_shapes_to_process = list(dict.fromkeys(selection)) 
        
        if not is_reloading_layout:
            self.ui.current_layout = None
            self.ui.hidden_originals = list(self.ui.selected_shapes_to_process)
        
        self.ui.shape_table.setRowCount(len(self.ui.selected_shapes_to_process))
        for i, obj in enumerate(self.ui.selected_shapes_to_process):
            display_label = obj.Label
            if display_label.startswith("master_shape_"):
                display_label = display_label.replace("master_shape_", "")
            
            qty = selection_counts.get(obj, 1)
            
            if initial_quantities and obj.Label in initial_quantities:
                qty = initial_quantities[obj.Label]
                
            steps = 4
            override = False
            if initial_rotation_steps and obj.Label in initial_rotation_steps:
                steps = initial_rotation_steps[obj.Label]
            if initial_overrides and obj.Label in initial_overrides:
                override = initial_overrides[obj.Label]
            
            up_dir = "Z+"
            if initial_up_directions and obj.Label in initial_up_directions:
                up_dir = initial_up_directions[obj.Label]
                
            fill = False
            if initial_fill_sheet and obj.Label in initial_fill_sheet:
                fill = initial_fill_sheet[obj.Label]

            add_row_fn = getattr(self.ui, 'add_part_row', getattr(self.ui, '_add_part_row', None))
            if add_row_fn:
                 add_row_fn(i, display_label, quantity=qty, rotation_steps=steps, 
                            override_rotation=override, up_direction=up_dir, fill_sheet=fill)
        
        self.ui.shape_table.resizeColumnsToContents()
        self.ui.status_label.setText(f"{len(selection)} unique object(s) selected. Specify quantities and nest.")

    def add_selected_shapes(self):
        """Adds the currently selected FreeCAD objects to the shape table."""
        selection = FreeCADGui.Selection.getSelection()
        if not selection:
            self.ui.status_label.setText("Select shapes in the 3D view or tree to add them.")
            return

        extracted = self._extract_parts_from_selection(selection)
        selection_counts = {}
        if extracted:
            for obj in extracted:
                selection_counts[obj] = selection_counts.get(obj, 0) + 1
            selection = extracted

        existing_labels = [self.ui.shape_table.item(row, 0).text() for row in range(self.ui.shape_table.rowCount())]
        
        added_count = 0
        
        unique_selection = list(dict.fromkeys(selection))
        
        for obj in unique_selection:
            if obj.Label not in existing_labels:
                row_position = self.ui.shape_table.rowCount()
                self.ui.shape_table.insertRow(row_position)
                
                qty = selection_counts.get(obj, 1)
                
                add_row_fn = getattr(self.ui, 'add_part_row', getattr(self.ui, '_add_part_row', None))
                if add_row_fn:
                    add_row_fn(row_position, obj.Label, quantity=qty)
                    
                self.ui.selected_shapes_to_process.append(obj)
                added_count += 1
        
        self.ui.shape_table.resizeColumnsToContents()
        self.ui.status_label.setText(f"Added {added_count} new shape(s).")

        if self.ui.shape_table.rowCount() > 0:
            self.ui.nest_button.setEnabled(True)

    def remove_selected_shapes(self):
        """Removes the selected rows from the shape table."""
        selected_items = self.ui.shape_table.selectedItems()
        selected_rows = sorted(list(set(item.row() for item in selected_items)), reverse=True)
        for row in selected_rows:
            label_to_remove = self.ui.shape_table.item(row, 0).text()
            self.ui.selected_shapes_to_process = [obj for obj in self.ui.selected_shapes_to_process if obj.Label != label_to_remove]
            self.ui.shape_table.removeRow(row)
        self.ui.status_label.setText(f"Removed {len(selected_rows)} shape(s).")

        if self.ui.shape_table.rowCount() == 0:
            self.ui.nest_button.setEnabled(False)

    def _get_rotation_steps(self):
        """Returns the orientation count based on algorithm-specific angle mapping."""
        algo = self.ui.algorithm_dropdown.currentText()
        
        if algo == "Physics":
            idx = self.ui.physics_rotation_steps_slider.value()
            # Mapping: 360(1), 90(4), 45(8), 30(12), 15(24), 10(36), 5(72), 2(180), 1(360)
            angles = PHYSICS_ROTATION_PRESETS
            if idx < len(angles):
                angle = angles[idx]
                return int(360 / angle)
        else:
            idx = self.ui.minkowski_rotation_steps_slider.value()
            angles = self.ui.rotation_angles
            if idx < len(angles):
                angle = angles[idx]
                return int(360 / angle)
        return 1

    def _execute_ga_nesting(self, target_layout, ui_params, quantities, master_map, 
                            rotation_params, algo_kwargs, is_simulating, viz_manager=None):
        """GA optimization on a background thread."""
        
        self._worker = NestingWorker(
            coordinator=None, # Set below
            run_args=(target_layout, ui_params, quantities, master_map,
                      rotation_params, algo_kwargs, is_simulating, viz_manager),
            cancel_check_fn=self._check_cancel,
            parent=None
        )
        
        coordinator = GACoordinator(
            doc=self.doc,
            shape_preparer=self.shape_preparer,
            # _safe_ui here as well as on the signal slots below. GACoordinator
            # already wraps these in its own try/except RuntimeError, so this is
            # belt-and-braces for the headless path -- but the two must not
            # disagree, and a guard that only lives in the caller cannot protect
            # a call site someone adds later.
            ui_callbacks={
                'set_status': lambda msg: self._safe_ui(self.ui.status_label.setText, msg),
                'update_progress': lambda c, t, m: self._safe_ui(self.ui.update_progress, c, t, m),
                'reset_progress': lambda: self._safe_ui(self.ui.reset_progress),
                'play_sound': lambda: self._safe_ui(
                    QtWidgets.QApplication.beep) if self._safe_ui(
                        self.ui.sound_checkbox.isChecked) else None,
            },
            draw_callback=self._worker.request_draw_on_main_thread,
            worker=self._worker
        )
        self._worker.coordinator = coordinator
        
        # Routed through _safe_ui: these are queued cross-thread signals, so the
        # slot can run after the panel has been closed and its widgets destroyed.
        # Guarding at the emit site in GACoordinator would not help -- emit()
        # returns long before the slot executes.
        self._worker.status_changed.connect(
            lambda msg: self._safe_ui(self.ui.status_label.setText, msg))
        self._worker.progress_updated.connect(
            lambda c, t, m: self._safe_ui(self.ui.update_progress, c, t, m))
        self._worker.draw_requested.connect(self._handle_draw_request)
        self._worker.finished_signal.connect(self._on_nesting_finished)
        self._worker.error_signal.connect(self._on_nesting_error)
        
        self._worker.start()

    def _safe_ui(self, fn, *args):
        """Invoke a widget method, tolerating a panel that has been closed.

        The worker outlives the panel. Its signals are queued, so a slot can run
        long after the widgets it touches were destroyed, and PySide6 then raises
        RuntimeError from the C++ side. GACoordinator already absorbs this for the
        callbacks it calls directly, but that guard cannot cover the worker path:
        emit() returns long before the slot executes, so the slot itself has to
        absorb it. request_cancel guards the same hazard locally; this is the
        shared form for the signal handlers.

        Only for direct widget calls. Wrapping job cleanup or document work in
        this would swallow a real failure as though the panel had merely closed.
        """
        try:
            return fn(*args)
        except RuntimeError:
            return None

    def _handle_draw_request(self, payload):
        """Main-thread handler for draw requests from worker.

        The worker is captured on entry and used throughout, rather than read
        from `self._worker` each time. `_on_nesting_finished` and `cancel_job`
        both set `self._worker = None`, and a `draw_requested` signal already
        sitting in the event queue can be delivered after that. The old code then
        raised AttributeError in its `finally`, so `notify_draw_complete` was
        never called and the worker's `wait()` never returned -- an unrecoverable
        freeze. With the reference captured, the acknowledgement still happens,
        and if there is genuinely no worker left there is nothing waiting on it.
        """
        worker = self._worker
        if worker is None:
            return
        try:
            if payload.get('updateGui_only'):
                FreeCADGui.updateGui()
            elif payload.get('create_population'):
                layouts = worker.coordinator.layout_manager.create_ga_population(
                    payload['master_map'], payload['quantities'], 
                    payload['ui_params'], payload['population_size'],
                    payload['rotation_steps'], verbose=payload.get('verbose', False)
                )
                worker.coordinator._pending_layouts = layouts
            elif payload.get('build_next_generation'):
                layouts = worker.coordinator._build_next_generation(
                    payload['gen'], payload['layouts'], payload['elites'], 
                    payload['master_map'], payload['quantities'], payload['ui_params'], 
                    payload['rotation_steps'], payload['mutation_rate'], 
                    payload['immigrant_ratio'], payload.get('verbose', False)
                )
                worker.coordinator._pending_layouts = layouts
            elif payload.get('spawn_fill_part'):
                payload['result_holder'][0] = payload['spawn_fn']()
            elif payload.get('cleanup_layouts'):
                for layout in payload['layouts']:
                    if layout != payload['best_layout']:
                        worker.coordinator.layout_manager.delete_layout(layout, verbose=payload.get('verbose', False))
            elif payload.get('sheets'):
                for sheet in payload['sheets']:
                    sheet.draw(payload['doc'], payload['ui_params'], payload['layout_group'],
                              parts_to_place_group=payload['parts_group'],
                              verbose=payload.get('verbose', False))
                if payload.get('hide_layout'):
                    lg = payload['layout_group']
                    if lg:
                        set_visibility(lg, False)
                FreeCADGui.updateGui()
            elif payload.get('ga_finalize'):
                # _finalize() and doc.recompute() must run on main thread (ViewObject + recompute)
                coordinator = worker.coordinator
                job = coordinator._finalize(
                    payload['best_layout'], payload['best_efficiency'],
                    payload['total_time'], payload['target_layout'], payload['ui_params']
                )
                payload['result_holder'][0] = job
                recompute_start = time.perf_counter()
                coordinator.doc.recompute()
                coordinator._record_doc_recompute(time.perf_counter() - recompute_start)
            elif payload.get('doc_recompute_only'):
                coordinator = worker.coordinator
                recompute_start = time.perf_counter()
                coordinator.doc.recompute()
                coordinator._record_doc_recompute(time.perf_counter() - recompute_start)
        finally:
            worker.notify_draw_complete()

    def _on_nesting_finished(self, job):
        """Main-thread handler for nesting completion."""
        if self.cancel_requested:
            if job:
                job.cleanup()
            self.cancel_job()
        else:
            self.current_job = job
            self._restore_source_placements()
            
        # State first, then widgets. With _safe_ui in place the widget calls
        # cannot raise, so the order is not what fixes the reported bug -- the
        # guard is. This ordering is belt-and-braces: it keeps the invariant
        # ("a run is never left looking live") true on its own terms, so a later
        # unguarded widget call cannot strand a worker reference either.
        self.is_running = False
        self.cancel_requested = False
        self._worker = None
        self._safe_ui(self.ui.nest_button.setEnabled, True)
        self._safe_ui(self.ui.cancel_button.setEnabled, False)
        self._safe_ui(self.ui.reset_progress)

    def _on_nesting_error(self, error_msg):
        """Main-thread handler for nesting errors."""
        FreeCAD.Console.PrintError(f"Nesting Error: {error_msg}\n")
        self._restore_source_placements()
        self.is_running = False
        self.cancel_requested = False
        self._worker = None
        self._safe_ui(self.ui.status_label.setText,
                      f"Error: {error_msg.split(chr(10))[0]}")
        self._safe_ui(self.ui.nest_button.setEnabled, True)
        self._safe_ui(self.ui.cancel_button.setEnabled, False)
        self._safe_ui(self.ui.reset_progress)
    
    def finalize_job(self):
        """Called when User clicks OK."""
        if self.current_job:
            final_layout = self.current_job.commit()
            
            self.ui.current_layout = final_layout
            
            if final_layout:
                set_visibility(final_layout, True)

            if final_layout and hasattr(final_layout, "Group"):
                for child in final_layout.Group:
                    if child.Label.startswith("MasterShapes"):
                        set_visibility(child, False)
                    elif child.Label.startswith("Sheet_"):
                        set_visibility(child, True)
            
            self.current_job = None
            FreeCAD.Console.PrintMessage("Job Finalized & Committed.\n")
            self.doc.recompute()

    def request_cancel(self):
        """Cancel a run, or dismiss the panel when nothing is running.

        Reached from two places: the in-panel "Cancel Nesting" button, and the
        dialog's own Cancel/Close via NestingPanel.reject(). Both need the same
        thing while a run is live -- set cancel_requested, which is the only
        flag the worker's _check_cancel reads, and unblock the worker if it is
        parked on a main-thread draw handover.
        """
        if self.is_running:
            self.cancel_requested = True
            self._safe_ui(self.ui.status_label.setText, "Cancelling... Please wait.")
            self._safe_ui(self.ui.cancel_button.setEnabled, False)  # prevent double-click

            # If worker is running, also unblock it from any draw wait
            if getattr(self, '_worker', None):
                self._worker.notify_draw_complete()  # unblock if waiting for draw
        else:
            self.cancel_job()
            # Only dismiss the panel when this came from the in-panel button.
            # reject() calls us, so calling back would bounce between the two;
            # NestingPanel guards that with _rejecting, and this is the other
            # half of the same guard -- the panel is already closing, so there
            # is nothing to dismiss.
            if not getattr(self.ui, '_rejecting', False) and hasattr(self.ui, 'reject'):
                self.ui.reject()

    def _check_cancel(self):
        return self.cancel_requested

    def cancel_job(self):
        """Called when User clicks Cancel."""
        self._restore_source_placements()
        if self.current_job:
            target = self.current_job.target_layout
            
            self.current_job.cleanup()
            
            if target:
                try: 
                    has_content = any(
                        child.Label.startswith("Sheet_") or child.Label.startswith("MasterShapes")
                        for child in (target.Group if hasattr(target, "Group") else [])
                    )
                    
                    if not has_content:
                        recursive_delete(self.doc, target)
                        if hasattr(self.ui, 'current_layout') and self.ui.current_layout == target:
                            self.ui.current_layout = None
                        FreeCAD.Console.PrintMessage("Removed empty target layout.\n")
                    else:
                        set_visibility(target, True)

                        if hasattr(target, "Group"):
                            for child in target.Group:
                                if child.Label.startswith("Sheet_"):
                                    set_visibility(child, True)
                                if child.Label.startswith("MasterShapes"):
                                    set_visibility(child, False)
                except Exception as e:
                    FreeCAD.Console.PrintWarning(f"[NestingController] Cancel cleanup failed for child: {e}\n")
            
            self.current_job = None
            FreeCAD.Console.PrintMessage("Job Cancelled.\n")
            self.doc.recompute()
    
    def toggle_bounds_visibility(self):
        is_visible = self.ui.show_bounds_checkbox.isChecked()
        
        # If a job is active, use its temp_layout (where current results are)
        # Otherwise use the committed current_layout
        if self.current_job and self.current_job.temp_layout:
            target_layout = self.current_job.temp_layout
        else:
            target_layout = getattr(self.ui, 'current_layout', None)
        
        if not target_layout: 
            return
        
        found_count = 0
        
        # Recursively find and toggle bounds visibility
        def set_show_bounds(obj, depth=0):
            nonlocal found_count
            indent = "  " * depth
            
            if obj.Label.startswith("boundary_"):
                found_count += 1
                set_visibility(obj, is_visible)

            if hasattr(obj, "BoundaryObject") and obj.BoundaryObject:
                found_count += 1
                set_visibility(obj.BoundaryObject, is_visible)
                
            if hasattr(obj, "Group"):
                for child in obj.Group:
                    set_show_bounds(child, depth + 1)
                    
        set_show_bounds(target_layout)
        self.doc.recompute()

    def _ensure_target_layout(self):
        """Determines the target layout, creating a default one if none exists."""
        target = getattr(self.ui, 'current_layout', None)
        
        if target:
            try:
                if target not in self.doc.Objects: target = None
            except Exception as e:
                FreeCAD.Console.PrintWarning(f"[NestingController] Target validation failed: {e}\n")
                target = None
            
        if not target and hasattr(self.ui, 'selected_shapes_to_process') and self.ui.selected_shapes_to_process:
             # Logic to find parent layout derived previously...
             # Simplified for brevity/robustness
             pass 

        if not target:
            base_name = "Layout"
            i = 0
            existing_labels = [o.Label for o in self.doc.Objects]
            while f"{base_name}_{i:03d}" in existing_labels: i += 1
            target = self.doc.addObject("App::DocumentObjectGroup", f"{base_name}_{i:03d}")
            target.Label = f"{base_name}_{i:03d}"
            self.ui.current_layout = target
            
        return target

    def _collect_ui_params(self):
        """Snapshot the panel into the dict the run is driven from.

        Every length comes out of its field as a plain millimetre float, which
        is the contract the rest of the workbench is written against: the
        units are a presentation concern and stop here.
        """
        deflection_angle = self.ui.deflection_input.value()
        deflection_mm = deflection_angle / 200.0

        settings_dict = {
            'sheet_width': self.ui.sheet_width_input.mm(),
            'sheet_height': self.ui.sheet_height_input.mm(),
            'spacing': self.ui.part_spacing_input.mm(),
            'sheet_thickness': self.ui.sheet_thickness_input.mm(),
            'deflection': deflection_mm,
            'deflection_angle': deflection_angle,
            'simplification': self.ui.simplification_input.mm(),
            'rotation_steps': self._get_rotation_steps(),
            'add_labels': self.ui.add_labels_checkbox.isChecked(),
            'font_path': getattr(self.ui, 'selected_font_path', None),
            'show_bounds': self.ui.show_bounds_checkbox.isChecked(),
            'label_height': self.ui.label_height_input.mm(),
            'label_size': self.ui.label_size_input.mm(),
            'generations': self.ui.minkowski_generations_input.value(),
            'population_size': self.ui.minkowski_population_size_input.value(),
            # Read here and passed on by _prepare_algo_kwargs. It used to be
            # read by neither, so the coordinator's `target_sheets` was always
            # absent and the Stop At Sheets dial did nothing at all -- see
            # issues.md and probe_target_sheets.py. Not persisted, deliberately,
            # as the widget's own comment says; save_settings names its
            # preference keys explicitly, so adding the key here cannot leak it.
            'target_sheets': self.ui.minkowski_target_sheets_input.value(),
            'compactness_weight': self.ui.minkowski_compactness_input.value(),
            'verbose': self.ui.verbose_logging_checkbox.isChecked(),
            'performance_logging': self.ui.performance_logging_checkbox.isChecked(),
            'candidate_geometry_cache': self.ui.candidate_geometry_cache_checkbox.isChecked(),
            'nesting_direction': self.ui.minkowski_direction_dial.value(),
            'algorithm': self.ui.algorithm_dropdown.currentText(),
            'use_random_direction': (self.ui.physics_random_checkbox.isChecked() if self.ui.algorithm_dropdown.currentText() == 'Physics' else self.ui.minkowski_random_checkbox.isChecked()),
            'stability_tolerance': self.ui.physics_improvement_threshold_input.value(),
            'anneal_curve': self.ui.physics_anneal_curve_type.currentText(),
            'anneal_min_amp': self.ui.physics_anneal_min_amp.mm(),
            'anneal_max_amp': self.ui.physics_anneal_max_amp.mm(),
            'anneal_rot_steps': self.ui.physics_anneal_rot_steps.value(),
            'anneal_rot_curve': self.ui.physics_anneal_rot_curve_type.currentText(),
            'anneal_rot_min': self.ui.physics_anneal_rot_min.value(),
            'anneal_rot_max': self.ui.physics_anneal_rot_max.value()
        }
        
        self.save_settings(settings_dict)
        
        return settings_dict

    def save_settings(self, settings):
        """Saves current UI settings to FreeCAD preferences.

        Lengths are stored as millimetre floats and never as display units.
        Preferences are not tied to a document, so an imperial session and a
        metric one share this store; writing "23.62" in place of 600 would
        make the two sessions overwrite each other's settings with a number
        25.4 times apart.

        Generations and Population Size are written under the same property
        names the layout group already records them as, so a value has one name
        wherever it is stored. Both stay at 1 unless the user set them; see
        load_persisted_settings for the measurement behind not defaulting the
        population higher.
        """
        prefs = FreeCAD.ParamGet(PREFS_PATH)
        prefs.SetFloat(PROP_SHEET_WIDTH, float(settings['sheet_width']))
        prefs.SetFloat(PROP_SHEET_HEIGHT, float(settings['sheet_height']))
        prefs.SetFloat(PROP_PART_SPACING, float(settings['spacing']))
        prefs.SetFloat(PROP_SHEET_THICKNESS, float(settings['sheet_thickness']))
        prefs.SetFloat(PROP_DEFLECTION_ANGLE, float(settings.get('deflection_angle', 10)))  # Save angle, not mm
        prefs.SetFloat(PROP_SIMPLIFICATION, float(settings['simplification']))
        prefs.SetFloat("GACompactnessWeight", float(settings['compactness_weight']))
        prefs.SetBool(
            "CandidateGeometryCache",
            bool(settings.get('candidate_geometry_cache',
                               CANDIDATE_GEOMETRY_CACHE_DEFAULT)),
        )
        
        mink_steps = int(360 / self.ui.rotation_angles[self.ui.minkowski_rotation_steps_slider.value()])
        prefs.SetInt("MinkowskiRotationSteps", mink_steps)
        prefs.SetFloat("MinkowskiStepSize",
                       float(self.ui.minkowski_step_size_input.mm()))
        prefs.SetInt("MinkowskiRotationWorkers",
                     int(self.ui.minkowski_rotation_workers_input.value()))
        
        phys_angles = PHYSICS_ROTATION_PRESETS
        phys_steps = int(360 / phys_angles[self.ui.physics_rotation_steps_slider.value()])
        prefs.SetInt("PhysicsRotationSteps", phys_steps)

        prefs.SetBool(PROP_ADD_LABELS, bool(settings['add_labels']))
        prefs.SetBool(PROP_SHOW_BOUNDS, bool(settings['show_bounds']))
        prefs.SetInt(PROP_GENERATIONS, int(settings.get('generations', 1)))
        prefs.SetInt(PROP_POPULATION_SIZE, int(settings.get('population_size', 1)))
        # The dial reading, not the bearing. It is what the widget holds, what
        # _load_params_from_layout reads off a layout group, and what
        # dial_to_bearing converts; storing the bearing here would need a
        # second inverse conversion and would break the step, since a bearing
        # snapped to 15 is a different reading than a reading snapped to 15.
        prefs.SetInt(PROP_NESTING_DIRECTION,
                     int(self.ui.minkowski_direction_dial.value()))
        prefs.SetBool(PROP_RANDOM_DIRECTION,
                      bool(self.ui.minkowski_random_checkbox.isChecked()))
        prefs.SetBool("PhysicsRandomDirection",
                      bool(self.ui.physics_random_checkbox.isChecked()))
        prefs.SetFloat(PROP_LABEL_HEIGHT, float(settings['label_height']))
        prefs.SetFloat(PROP_LABEL_SIZE, float(settings['label_size']))
        prefs.SetFloat("PhysicsStabilityTolerance", float(settings.get('stability_tolerance', 0.01)))
        prefs.SetString("PhysicsAnnealCurveType", str(settings.get('anneal_curve', "Logarithmic")))
        prefs.SetFloat("PhysicsAnnealMinAmp", float(self.ui.physics_anneal_min_amp.mm()))
        prefs.SetFloat("PhysicsAnnealMaxAmp", float(self.ui.physics_anneal_max_amp.mm()))
        
        prefs.SetInt("PhysicsAnnealRotSteps", int(settings.get('anneal_rot_steps', 10)))
        prefs.SetString("PhysicsAnnealRotCurveType", str(settings.get('anneal_rot_curve', "Logarithmic")))
        prefs.SetFloat("PhysicsAnnealRotMin", float(settings.get('anneal_rot_min', 1.0)))
        prefs.SetFloat("PhysicsAnnealRotMax", float(settings.get('anneal_rot_max', 90.0)))
        if settings['font_path']:
             prefs.SetString("FontPath", str(settings['font_path']))

    def _collect_job_parameters(self, ui_settings):
        quantities = {}
        master_map = {}
        rotation_params = {}
        
        global_rot = ui_settings['rotation_steps']
        
        for row in range(self.ui.shape_table.rowCount()):
            try:
                label = self.ui.shape_table.item(row, 0).text()
                qty = self.ui.shape_table.cellWidget(row, 1).value()
                
                rot_widget = self.ui.shape_table.cellWidget(row, 2)
                rot_val = rot_widget.findChild(QtWidgets.QSpinBox).value()
                override = self.ui.shape_table.cellWidget(row, 3).isChecked()
                
                up_dir_combo = self.ui.shape_table.cellWidget(row, 4)
                up_direction = up_dir_combo.currentText() if up_dir_combo else "Z+"
                
                fill_checkbox = self.ui.shape_table.cellWidget(row, 5)
                fill_sheet = fill_checkbox.isChecked() if fill_checkbox else False
                
                quantities[label] = {
                    'quantity': qty,
                    'rotation_steps': rot_val if override else global_rot,
                    'up_direction': up_direction,
                    'fill_sheet': fill_sheet
                }
                
                rotation_params[label] = (rot_val, override)
            except Exception as e:
                FreeCAD.Console.PrintWarning(f"[NestingController] Skipping row {row} in shape table: {e}\n")
                continue
            
        for obj in self.ui.selected_shapes_to_process:
             try:
                 lbl = obj.Label.replace("master_shape_", "")
                 if lbl in quantities:
                     master_map[obj.Label] = obj
             except Exception as e:
                 FreeCAD.Console.PrintWarning(f"[NestingController] Failed to map object {obj.Label if hasattr(obj, 'Label') else 'unknown'}: {e}\n")
             
        return ui_settings, quantities, master_map, rotation_params

    def _prepare_algo_kwargs(self, ui_params):
        algo_kwargs = {}
        algorithm = ui_params.get('algorithm', 'Minkowski')
        
        if algorithm == 'Physics':
            if self.ui.physics_random_checkbox.isChecked():
                algo_kwargs['physics_direction'] = None 
            else:
                # Dial value is CCW from 6 o'clock, and is not the compass
                # bearing: 0=Down(0,-1), 90=Left(-1,0), 180=Up(0,1),
                # 270=Right(1,0). dial_to_bearing owns that conversion so the
                # panel's readout and the search direction cannot disagree.
                angle_deg = dial_to_bearing(self.ui.physics_direction_dial.value())
                angle_rad = math.radians(angle_deg)
                algo_kwargs['physics_direction'] = (math.cos(angle_rad), math.sin(angle_rad))
            
            algo_kwargs['step_size'] = self.ui.physics_step_size_input.mm()
            algo_kwargs['max_spawn_count'] = self.ui.physics_max_spawn_input.value()
            algo_kwargs['max_nesting_steps'] = self.ui.physics_max_nesting_steps_input.value()
            algo_kwargs['anneal_steps'] = self.ui.physics_anneal_steps_input.value()
            algo_kwargs['anneal_rotate_enabled'] = self.ui.anneal_rotate_checkbox.isChecked()
            algo_kwargs['anneal_translate_enabled'] = self.ui.anneal_translate_checkbox.isChecked()
            algo_kwargs['anneal_random_shake_direction'] = self.ui.anneal_random_shake_checkbox.isChecked()
            algo_kwargs['stability_tolerance'] = ui_params.get('stability_tolerance', 0.01)
            algo_kwargs['anneal_curve'] = ui_params.get('anneal_curve', "Logarithmic")
            algo_kwargs['anneal_min_amp'] = self.ui.physics_anneal_min_amp.mm()
            algo_kwargs['anneal_max_amp'] = self.ui.physics_anneal_max_amp.mm()
            algo_kwargs['anneal_rot_steps'] = ui_params.get('anneal_rot_steps', 10)
            algo_kwargs['anneal_rot_curve'] = ui_params.get('anneal_rot_curve', "Logarithmic")
            algo_kwargs['anneal_rot_min'] = ui_params.get('anneal_rot_min', 1.0)
            algo_kwargs['anneal_rot_max'] = ui_params.get('anneal_rot_max', 90.0)
        else:
            if self.ui.minkowski_random_checkbox.isChecked():
                algo_kwargs['search_direction'] = None
            else:
                angle_deg = dial_to_bearing(self.ui.minkowski_direction_dial.value())
                angle_rad = math.radians(angle_deg)
                algo_kwargs['search_direction'] = (math.cos(angle_rad), math.sin(angle_rad))
            
            algo_kwargs['population_size'] = self.ui.minkowski_population_size_input.value()
            algo_kwargs['generations'] = self.ui.minkowski_generations_input.value()
            # The Stop At Sheets dial, alongside the two GA dials it belongs
            # with. GACoordinator reads it from algo_kwargs (run() and
            # _run_generation), not from ui_params, so collecting it without
            # passing it on is exactly the bug probe_target_sheets.py guards.
            # 0 means off, which is the spinbox's special-value text.
            algo_kwargs['target_sheets'] = ui_params.get('target_sheets', 0) or 0
            algo_kwargs['clear_nfp_cache'] = self.ui.clear_cache_checkbox.isChecked()
            algo_kwargs['candidate_geometry_cache'] = ui_params.get(
                'candidate_geometry_cache', CANDIDATE_GEOMETRY_CACHE_DEFAULT
            )
            # The two performance dials. 0 threads means "auto" and is passed
            # through as 0 so the nester can tell "user chose auto" from
            # "nobody said anything" -- both land on the stdlib default, but
            # only one of them is a decision.
            algo_kwargs['step_size'] = self.ui.minkowski_step_size_input.mm()
            algo_kwargs['rotation_workers'] = (
                self.ui.minkowski_rotation_workers_input.value())

        algo_kwargs['spacing'] = ui_params['spacing']
        algo_kwargs['random_seed'] = ui_params.get('random_seed')
        
        if hasattr(self.ui, 'log_message'):
            algo_kwargs['log_callback'] = self.ui.log_message
            
        return algo_kwargs
