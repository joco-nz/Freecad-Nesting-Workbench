# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/Tools/Stacker/stacker.py

"""
This module contains the SheetStacker class, which handles the logic for 
finding, stacking, and unstacking the generated sheet layouts.
"""

import FreeCAD
import ast
from freecad.nestingworkbench import nw_logger
from ...freecad_helpers import (
    find_back_boundary, find_back_group, find_sheet_boundary, get_layout_group,
    get_sheet_groups, get_all_objects_recursive,
)

class SheetStacker:
    """Handles the logic for finding, stacking, and unstacking sheet layouts."""
    def __init__(self, layout_group=None):
        self.doc = FreeCAD.ActiveDocument
        if layout_group:
            self.layout_group = layout_group
        else:
            self.layout_group = get_layout_group(self.doc)

    def toggle_stack(self):
        """Public method to stack or unstack the sheets."""
        if not self.layout_group:
            nw_logger.info("No valid packed layout found to stack/unstack.")
            return
        
        if not hasattr(self.layout_group, "IsStacked"):
             self.layout_group.addProperty("App::PropertyBool", "IsStacked", "Nesting")
             self.layout_group.IsStacked = False
        
        if self.layout_group.IsStacked:
            self._unstack()
        else:
            self._stack()
        
        self.doc.recompute()

    def _stack(self):
        """Moves all objects in sheets 2 and higher to overlay sheet 1, and every back side to the origin."""
        sheet_groups = get_sheet_groups(self.layout_group)
        back_groups = [find_back_group(sg) for sg in sheet_groups]
        if len(sheet_groups) < 2 and not any(back_groups):
            nw_logger.info("Stacking requires two or more sheets, or a sheet with a back side.")
            return

        # Before moving anything, resolve every front and back boundary.
        # When any is missing, log an error naming the sheet and abort before writing OriginalPlacements or moving any object.
        boundaries = []
        back_boundaries = []
        for sg, back in zip(sheet_groups, back_groups):
            b = find_sheet_boundary(sg)
            if not b:
                nw_logger.error(f"Cannot stack: sheet '{sg.Label}' has no boundary. Stacking aborted.")
                return
            boundaries.append(b)
            back_boundary = find_back_boundary(back) if back else None
            if back and not back_boundary:
                nw_logger.error(f"Cannot stack: the back side of '{sg.Label}' has no boundary. Stacking aborted.")
                return
            back_boundaries.append(back_boundary)

        # Before any movement, store the current state of all objects in the layout.
        # This ensures that unstacking will always restore to the state right before stacking.
        if not hasattr(self.layout_group, "OriginalPlacements"):
            self.layout_group.addProperty("App::PropertyMap", "OriginalPlacements", "Nesting")

        placements_dict = {}
        all_objects = get_all_objects_recursive(self.layout_group)
        for obj in all_objects:
            if not hasattr(obj, 'Placement'):
                continue
            # Store placement as a string representation of a tuple:
            # (Base.x, Base.y, Base.z, Rotation.Q[0], Rotation.Q[1], Rotation.Q[2], Rotation.Q[3])
            p = obj.Placement
            placement_str = str((p.Base.x, p.Base.y, p.Base.z, p.Rotation.Q[0], p.Rotation.Q[1], p.Rotation.Q[2], p.Rotation.Q[3]))
            placements_dict[obj.Name] = placement_str
        self.layout_group.OriginalPlacements = placements_dict

        # Front: for i >= 1, move_vec = -boundary.Placement.Base (with z = 0); sheet 1 stays.
        # Back: every sheet's back side moves by -back_boundary.Placement.Base, sheet 1's included.
        # A back object moves once, by the back offset only, never also by the front's.
        for i, sheet_group in enumerate(sheet_groups):
            back = back_groups[i]
            back_names = {o.Name for o in get_all_objects_recursive(back)} if back else set()
            if i >= 1:
                base = boundaries[i].Placement.Base
                self._shift([o for o in get_all_objects_recursive(sheet_group) if o.Name not in back_names],
                            FreeCAD.Vector(-base.x, -base.y, 0.0))
            if back:
                base = back_boundaries[i].Placement.Base
                self._shift(get_all_objects_recursive(back), FreeCAD.Vector(-base.x, -base.y, 0.0))

        self.layout_group.IsStacked = True
        nw_logger.info("Sheets are now stacked.")
        
    @staticmethod
    def _shift(objects, move_vec):
        for obj in objects:
            obj.Placement = FreeCAD.Placement(move_vec, FreeCAD.Rotation()).multiply(obj.Placement)

    def _unstack(self):
        """Restores all objects in the layout to their original positions."""
        if not hasattr(self.layout_group, "OriginalPlacements"):
            nw_logger.error("Original placement data not found. Cannot unstack.")
            return
            
        placements_dict = self.layout_group.OriginalPlacements
        all_objects = get_all_objects_recursive(self.layout_group)
        for obj in all_objects:
            if not hasattr(obj, 'Placement'):
                continue
            if obj.Name in placements_dict:
                placement_str = placements_dict[obj.Name]
                try:
                    # Use ast.literal_eval for safely evaluating the string representation of the tuple
                    data = ast.literal_eval(placement_str)
                except (ValueError, SyntaxError):
                    nw_logger.warn(f"Could not parse placement data for '{obj.Name}'. Skipping.")
                    continue
                base = FreeCAD.Vector(data[0], data[1], data[2])
                rot = FreeCAD.Rotation(data[3], data[4], data[5], data[6])
                obj.Placement = FreeCAD.Placement(base, rot)
        
        self.layout_group.IsStacked = False
        nw_logger.info("Sheets are now unstacked.")
