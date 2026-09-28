# SPDX-License-Identifier: LGPL-2.1-or-later
import FreeCAD
from ...freecad_helpers import recursive_delete
from ...constants import *

class NestingJob:
    """
    Manages a single nesting session using the Sandbox Pattern.
    Receives a completed GA layout via from_ga_result() and either commits
    it to the target layout or discards it on cancel.

    NOTE: Direct instantiation is not supported. Always use from_ga_result().
    """
    @classmethod
    def from_ga_result(cls, doc, target_layout, params, preparer, layout_group, parts_group, sheets,
                       shared_master_group=None, dispose_shared_master_group=None):
        """Creates a NestingJob from a completed GA layout.

        This is the sole entry point. The GACoordinator and LayoutManager own
        the sandbox lifecycle; this class only handles commit/cancel.

        Args:
            dispose_shared_master_group: callable that removes that group, or
                None. Supplied by the coordinator, which owns the LayoutManager
                and so the cached reference to it. Called by cleanup().
            shared_master_group: The document-level group holding the pooled
                master shapes in headless mode, or None in simulate mode. A
                headless layout has no MasterShapes child of its own -- see
                LayoutManager._get_shared_master_group -- so without this,
                commit() finds no masters, silently skips promoting them, and
                leaves them parented outside every layout while the previous
                run's master row in target_layout is neither replaced nor
                deleted.
        """
        job = cls.__new__(cls)
        job.doc = doc
        job.target_layout = target_layout
        job.params = params
        job.preparer = preparer
        job.temp_layout = layout_group
        job.parts_group = parts_group
        job.sheets = sheets
        job.shared_master_group = shared_master_group

        job._owned_object_names = set()
        if layout_group: job._owned_object_names.add(layout_group.Name)
        if parts_group: job._owned_object_names.add(parts_group.Name)
        # The shared master group is disposed by the run's teardown, which is
        # what stops one MasterShapes row accumulating per run.
        #
        # It lives at document level, so it survives the teardown of every layout
        # in the run and `delete_layout` cannot reach it. Nothing removed it:
        # cleanup() -- which runs on commit, on supersede, and on cancel -- did
        # not own it. The next run's LayoutManager builds a *fresh* run-scoped
        # group, so FreeCAD suffixed the old one: MasterShapes, MasterShapes001,
        # MasterShapes002, one per run. Measured with the real panel and a real
        # QThread: one row after run 1, two after run 2.
        #
        # Disposal goes through `dispose_shared_master_group` rather than adding
        # the group to `_owned_object_names`. An earlier version did the latter
        # and it was wrong: the group is not owned by anything the job can see
        # being removed, and LayoutManager caches a reference to it for the run.
        # Deleting it behind that cache left a stale wrapper, and the next
        # reader raised "Cannot access attribute 'Group' of deleted object".
        # `dispose_shared_master_group` already clears the cache before removing
        # the object, for exactly that reason, and is idempotent.
        job.dispose_shared_master_group = dispose_shared_master_group

        return job

    def commit(self):
        """Promotes the temporary results to the target layout."""
        
        to_remove = []
        for child in self.target_layout.Group:
            if child.Label.startswith("Sheet_"):
                to_remove.append(child)
        
        for child in to_remove:
            recursive_delete(self.doc, child)
            
        self._promote_masters()

        # IMPORTANT: explicitly removeObject from temp first, because FreeCAD's addObject
        # does NOT automatically remove from the old group. If sheets remain in
        # temp_layout.Group when cleanup() calls recursive_delete(temp_layout), it will
        # walk into the sheets' children and delete Shapes_ groups and nested_xxx containers.
        sheets_to_move = [c for c in self.temp_layout.Group if c.Label.startswith("Sheet_")]
        for sheet in sheets_to_move:
            self.temp_layout.removeObject(sheet)
            self.target_layout.addObject(sheet)

        self.cleanup()
        
        self._apply_properties(self.target_layout)
        
        return self.target_layout

    def _find_master_group(self):
        """The group holding this run's master shapes, or None.

        Simulate mode: a ``MasterShapes`` child of the temp layout.

        Headless: the document-level shared group, because a headless layout
        deliberately has no master group of its own -- see
        ``LayoutManager._get_shared_master_group`` for why parenting them under
        a layout would let one layout's teardown destroy another's masters.
        """
        child = next(
            (c for c in self.temp_layout.Group if c.Label.startswith("MasterShapes")),
            None,
        )
        if child is not None:
            return child, False  # (group, is_shared)
        shared = self.shared_master_group
        if shared is not None and getattr(shared, "Group", None):
            return shared, True
        return None, False

    def _promote_masters(self):
        """Moves this run's master shapes under the target layout.

        Also disposes of the previous run's master row: without that, a re-nest
        left the stale masters in place alongside the new geometry.
        """
        source, is_shared = self._find_master_group()

        if source is None or not len(source.Group):
            # Nothing to promote. An empty shared group left by an earlier
            # commit is the expected case on a re-run, and is not an error.
            if is_shared and source is not None:
                FreeCAD.Console.PrintMessage(
                    "[NestingJob] Shared master group is empty; the previous "
                    "run's masters are already committed\n")
            return

        # Delete the old row first. Doing this before adopting matters: once the
        # shared masters are re-parented into a new group under target_layout,
        # that group is itself labelled "MasterShapes" and would otherwise be
        # found by this lookup and deleted.
        old_masters = next(
            (c for c in self.target_layout.Group if c.Label.startswith("MasterShapes")),
            None,
        )
        if old_masters is not None:
            recursive_delete(self.doc, old_masters)

        for master in source.Group:
            if master.Label.startswith("temp_master_"):
                master.Label = master.Label.replace("temp_master_", "master_")

        if is_shared:
            # Re-parent into a group of our own, then hand that to the target
            # layout. The shared group is left empty *here*, and removed by the
            # cleanup() that commit() calls immediately afterwards, because it is
            # job-owned.
            #
            # It used to be left in place, on the reasoning that a later run
            # would pick the emptied shell back up. It does not: the group is
            # run-scoped, so every run builds a fresh one and the shell sat in
            # the document for good, holding on to the MasterShapes name. That
            # assumption is what produced the 001, 002, 003 suffixing.
            target_group = self.doc.addObject("App::DocumentObjectGroup", "MasterShapes")
            target_group.Label = "MasterShapes"
            for master in list(source.Group):
                # addObject does not remove from the previous group, so the
                # source must be emptied explicitly or each master would be
                # claimed by two parents.
                source.removeObject(master)
                target_group.addObject(master)
            self.target_layout.addObject(target_group)
            FreeCAD.Console.PrintMessage(
                f"[NestingJob] Promoted {len(target_group.Group)} shared master "
                f"shape(s) into '{self.target_layout.Label}'\n")
        else:
            self.temp_layout.removeObject(source)
            self.target_layout.addObject(source)

    def cleanup(self):
        """Destroys the sandbox."""
        for name in list(self._owned_object_names):
            obj = self.doc.getObject(name)
            if obj:
                recursive_delete(self.doc, obj)
        self._owned_object_names.clear()

        # Then the shared master group, through its owner. On commit this runs
        # after _promote_masters has re-parented the masters into a new group
        # under the target layout, so the source is empty. On supersede or
        # cancel the masters inside are this run's own and go with it. Either
        # way the row does not survive to be suffixed 001 on the next run.
        dispose = getattr(self, 'dispose_shared_master_group', None)
        if dispose is not None:
            try:
                dispose()
            except Exception as exc:
                FreeCAD.Console.PrintWarning(
                    f"[NestingJob] Could not dispose the shared master group: "
                    f"{exc}\n")
        
        self.temp_layout = None
        self.parts_group = None

    def _apply_properties(self, target_layout):
        p = self.params
        self._set_prop(target_layout, PROP_LENGTH, PROP_SHEET_WIDTH, p['sheet_width'])
        self._set_prop(target_layout, PROP_LENGTH, PROP_SHEET_HEIGHT, p['sheet_height'])
        self._set_prop(target_layout, PROP_LENGTH, PROP_PART_SPACING, p['spacing'])
        self._set_prop(target_layout, PROP_LENGTH, PROP_SHEET_THICKNESS, p['sheet_thickness'])
        self._set_prop(target_layout, PROP_FLOAT, PROP_DEFLECTION_ANGLE, p.get('deflection_angle', 30))
        self._set_prop(target_layout, PROP_FLOAT, PROP_SIMPLIFICATION, p.get('simplification', 1.0))
        self._set_prop(target_layout, PROP_FILE, PROP_FONT_FILE, p['font_path'])
        self._set_prop(target_layout, PROP_BOOL, PROP_SHOW_BOUNDS, p['show_bounds'])
        self._set_prop(target_layout, PROP_BOOL, PROP_ADD_LABELS, p['add_labels'])
        self._set_prop(target_layout, PROP_LENGTH, PROP_LABEL_HEIGHT, p['label_height'])
        self._set_prop(target_layout, PROP_FLOAT, PROP_LABEL_SIZE, p['label_size'])
        self._set_prop(target_layout, PROP_INTEGER, PROP_GLOBAL_ROTATION_STEPS, p['rotation_steps'])
        self._set_prop(target_layout, PROP_INTEGER, PROP_GENERATIONS, p.get('generations', 1))
        self._set_prop(target_layout, PROP_INTEGER, PROP_POPULATION_SIZE, p.get('population_size', 1))

        # Save Nesting Direction as a vector/tuple if possible, or just the dial value
        # For simplicity and transparency in the UI, we'll save the dial value (degrees)
        dial_val = p.get('nesting_direction', 0)
        self._set_prop(target_layout, PROP_INTEGER, PROP_NESTING_DIRECTION, dial_val)

    def _set_prop(self, obj, type_str, name, val):
        if not hasattr(obj, name):
            obj.addProperty(type_str, name, "Layout", "")
        setattr(obj, name, val)
