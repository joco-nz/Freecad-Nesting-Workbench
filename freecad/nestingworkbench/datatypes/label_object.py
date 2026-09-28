# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/datatypes/label_object.py

"""
This module defines a custom FreeCAD scripted object for representing a text label.
"""

import FreeCAD

from ..freecad_helpers import get_view_object

class LabelObject:
    """A scripted object representing a text label."""

    def __init__(self, obj):
        """Called when a new object is created."""
        obj.Proxy = self
        # Use the default view provider. Unguarded this raised AttributeError
        # under a GUI-less FreeCAD, where ViewObject exists but is None.
        obj_view = get_view_object(obj)
        if obj_view is not None:
            obj_view.Proxy = 0

    def execute(self, fp):
        """Called on recompute. Does nothing as shape is set externally."""
        pass

class ViewProviderLabel:
    """A view provider for the LabelObject."""

    def __init__(self, vobj):
        """Called when the view object is created."""
        vobj.Proxy = self

    def attach(self, vobj):
        """Set up the view object's properties."""
        vobj.ShapeColor = (0.9, 0.9, 0.9) # Light gray

def create_label_object(name="Label"):
    """Helper function to create a new LabelObject in the active document."""
    doc = FreeCAD.ActiveDocument
    obj = doc.addObject("Part::FeaturePython", name)
    LabelObject(obj)
    if FreeCAD.GuiUp:
        ViewProviderLabel(obj.ViewObject)
    return obj