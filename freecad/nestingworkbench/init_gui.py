# SPDX-License-Identifier: LGPL-2.1-or-later
# freecad/nestingworkbench/init_gui.py


import os

import FreeCADGui

from freecad.nestingworkbench import ICONS_DIR


class NestingWorkbench(FreeCADGui.Workbench):
    """
    Defines the Nesting Workbench.
    """
    MenuText = "Nesting"
    ToolTip = "A workbench for 2D nesting of shapes."
    # Absolute path: the workbench selector reads this at registration time,
    # before Initialize() has added the icon directory to the search path.
    Icon = os.path.join(ICONS_DIR, "Nesting_Workbench.svg")

    def GetClassName(self):
        return "Gui::PythonWorkbench"

    def Initialize(self):
        """This function is executed when the workbench is activated."""
        # Add the icon directory to the global search path here rather than at
        # import time, so an unactivated workbench does not affect icon lookup
        # for the rest of FreeCAD. All icon names are Nesting_*-prefixed.
        FreeCADGui.addIconPath(ICONS_DIR)
        # Import the command modules. This executes the FreeCADGui.addCommand()
        # in each file, making the commands available to FreeCAD.
        from freecad.nestingworkbench.commands import command_nest
        from freecad.nestingworkbench.commands import command_stack_sheets
        from freecad.nestingworkbench.commands import command_manual_nester
        from freecad.nestingworkbench.commands import command_export_sheets
        from freecad.nestingworkbench.commands import command_create_cam_job
        from freecad.nestingworkbench.commands import command_replay_cam
        from freecad.nestingworkbench.commands import command_audit_lead_ins
        from freecad.nestingworkbench.commands import command_create_silhouette
        # Create Menu (Dropdown)
        #
        # Replay sits next to Create CAM Job rather than inside it. They are
        # different operations -- one builds a job from a template, the other
        # applies the user's own CAM setup to a nest -- and keeping them as
        # separate entries is what lets the replay stay independent of that
        # command, which is somebody else's work.
        self.appendMenu(["Nesting"], [
            'Nesting_Run',
            'Nesting_StackSheets',
            'Nesting_ManualNester',
            'Nesting_Export',
            'Nesting_CreateCAMJob',
            'Nesting_ReplayCAMSetup',
            'Nesting_AuditLeadIns',
            'Nesting_CreateSilhouette'
        ])
        self.appendToolbar("Nesting", [
            'Nesting_Run',
            'Nesting_StackSheets',
            'Nesting_ManualNester',
            'Nesting_Export',
            'Nesting_CreateCAMJob',
            'Nesting_ReplayCAMSetup',
            # The lead-in check is deliberately NOT in the toolbar, though it is
            # in the menu. It acts on a replayed job, so it is meaningless until
            # a replay has run, and a toolbar button that is inert for most of a
            # session is one people learn to click without reading. In the menu
            # it sits next to the replay that produces the jobs it works on.
            'Nesting_CreateSilhouette'
        ])

    def Activated(self):
        """This function is executed when the workbench is activated."""
        return

    def Deactivated(self):
        """This function is executed when the workbench is deactivated."""
        return

# Add the workbench to FreeCAD's list of available workbenches
FreeCADGui.addWorkbench(NestingWorkbench())
