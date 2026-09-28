"""Undo the preference writes made by probe_ui_performance_dials.py.

That probe has to write real preferences to prove the save/load round trip, and
that mutates the user's FreeCAD configuration. Run this afterwards, or remove
MinkowskiStepSize and MinkowskiRotationWorkers by hand under
Preferences -> NestingWorkbench.

    /home/james/freecad_env/usr/bin/freecad tests/freecad_harness/reset_ui_dial_prefs.py
"""
import sys

sys.path.insert(0, "/home/james/dev/Freecad-Nesting-Workbench")
import FreeCAD
from freecad.nestingworkbench.constants import PREFS_PATH

prefs = FreeCAD.ParamGet(PREFS_PATH)
for key in ("MinkowskiStepSize", "MinkowskiRotationWorkers"):
    sentinel = prefs.GetFloat(key, -999.0)
    for remover in (prefs.RemFloat, prefs.RemInt, prefs.RemBool,
                    prefs.RemString):
        remover(key)
    gone = prefs.GetFloat(key, -999.0)
    FreeCAD.Console.PrintMessage(
        "%s: %s -> %s  %s\n" % (key, sentinel, gone,
                                "cleared" if gone == -999.0 else "STILL SET"))
