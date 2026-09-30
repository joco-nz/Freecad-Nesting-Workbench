# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/task_panel_manager.py

"""
This module contains the NestingTaskPanel class, which is responsible for
creating, showing, and managing the lifecycle of the FreeCAD Task Panel.
"""

import FreeCAD
import FreeCADGui

# Import the UI panel class
from .Tools.Nesting.ui_nesting import NestingPanel

class NestingTaskPanel:
    """Manages the FreeCAD Task Panel dialog."""
    def __init__(self, cleanup_callback=None):
        self.form = NestingPanel()
        self._cleanup_callback = cleanup_callback
        self.task_widget = FreeCADGui.Control.showDialog(self)
    
    def accept(self):
        """Called by FreeCAD when the dialog's 'OK' button is clicked."""
        if hasattr(self.form, "accept"):
            self.form.accept()
        self.cleanup()
        return True

    def reject(self):
        """Called by FreeCAD when the dialog is closed or 'Cancel' is clicked."""
        if hasattr(self.form, "reject"):
            self.form.reject()
        self.cleanup()
        return True

    def cleanup(self):
        """Resets the command's panel instance and releases panel resources."""
        # The panel registers a FreeCAD document observer, which FreeCAD holds
        # as a raw pointer to a Python object. This is the one teardown path
        # every close route goes through, so it is where that registration has
        # to be undone -- leaving it behind means a later document switch calls
        # into a collected object.
        dispose = getattr(self.form, "dispose", None)
        if dispose is not None:
            try:
                dispose()
            except Exception:
                pass
        if self._cleanup_callback:
            self._cleanup_callback()
