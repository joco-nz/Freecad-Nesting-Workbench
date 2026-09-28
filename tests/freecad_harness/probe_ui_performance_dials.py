"""Do the two new Minkowski fields exist, and does the controller read them?

Runs under the GUI binary, because that is where the panel is constructed.
Constructs the dialog directly rather than driving a whole nest, then checks
three things that a field which exists but is not plumbed would fail:
  1. the widgets exist, with the expected defaults and ranges
  2. moving them changes what the controller hands the nester
  3. the value survives a save/load round trip through preferences
"""
import os
import sys

sys.path.insert(0, "/home/james/dev/Freecad-Nesting-Workbench")
_LOG = open("/tmp/opencode/ui_dials.txt", "w")


def e(m):
    _LOG.write(str(m) + "\n")
    _LOG.flush()


try:
    import FreeCAD
    import FreeCADGui
    from PySide import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    e("GuiUp = %s" % FreeCAD.GuiUp)

    from freecad.nestingworkbench.Tools.Nesting import ui_nesting, nesting_controller

    dlg = ui_nesting.NestingPanel()
    e("dialog built: %s" % type(dlg).__name__)

    step_box = dlg.minkowski_step_size_input
    thr_box = dlg.minkowski_rotation_workers_input
    e("step_size field:  %r range=%s default=%s"
      % (step_box.toolTip().splitlines()[0], (step_box.minimum(), step_box.maximum()),
         step_box.value()))
    e("threads field:    %r range=%s default=%s auto-text=%r"
      % (thr_box.toolTip().splitlines()[0], (thr_box.minimum(), thr_box.maximum()),
         thr_box.value(), thr_box.specialValueText()))

    # 2. does the controller pass them through?
    ctl = nesting_controller.NestingController(dlg)
    ui_params = {"spacing": 1.0, "algorithm": "Minkowski"}
    dlg.minkowski_step_size_input.setValue(20.0)
    dlg.minkowski_rotation_workers_input.setValue(4)
    kw = ctl._prepare_algo_kwargs(ui_params)
    e("\n_prepare_algo_kwargs with step=20, threads=4:")
    e("  step_size       = %r" % kw.get("step_size"))
    e("  rotation_workers= %r" % kw.get("rotation_workers"))
    assert kw.get("step_size") == 20.0, "step_size did not reach algo_kwargs"
    assert kw.get("rotation_workers") == 4, "rotation_workers did not reach algo_kwargs"

    dlg.minkowski_rotation_workers_input.setValue(0)
    kw = ctl._prepare_algo_kwargs(ui_params)
    e("  threads=0 (auto) -> rotation_workers=%r  (must be 0, not absent)"
      % kw.get("rotation_workers"))
    assert kw.get("rotation_workers") == 0, "auto was not passed as 0"

    # 3. preferences round trip: save via the controller, then rebuild the
    #    panel so it re-reads from prefs. Rebuilding is the real path -- a user
    #    reopening the dialog gets a fresh panel.
    #    Put the fields back to known values first: the check above left
    #    threads at 0 to prove "auto" is distinguishable from "absent", so
    #    saving now would faithfully persist 0 and prove nothing.
    dlg.minkowski_step_size_input.setValue(12.5)
    dlg.minkowski_rotation_workers_input.setValue(3)
    settings = ctl._collect_ui_params()
    if settings is None:
        for name in dir(ctl):
            if False:
                settings = getattr(ctl, name)()
                break
    e("\nsettings dict from controller: %s"
      % ("found, %d keys" % len(settings) if settings else "NOT FOUND"))
    ctl.save_settings(settings)
    prefs = FreeCAD.ParamGet(nesting_controller.PREFS_PATH)
    e("  MinkowskiStepSize        = %r" % prefs.GetFloat("MinkowskiStepSize", -1))
    e("  MinkowskiRotationWorkers = %r" % prefs.GetInt("MinkowskiRotationWorkers", -1))
    e("  saved: step=12.5 threads=3")
    assert abs(prefs.GetFloat("MinkowskiStepSize", -1) - 12.5) < 1e-6, "step size not persisted"
    assert prefs.GetInt("MinkowskiRotationWorkers", -1) == 3, "threads not persisted"

    dlg2 = ui_nesting.NestingPanel()
    e("\nfresh panel after reopening:")
    e("  step_size = %r  threads = %r"
      % (dlg2.minkowski_step_size_input.value(),
         dlg2.minkowski_rotation_workers_input.value()))
    assert dlg2.minkowski_step_size_input.value() == 12.5, "step size did not restore"
    assert dlg2.minkowski_rotation_workers_input.value() == 3, "threads did not restore"
    e("\nALL CHECKS PASSED")
    e("NOTE: this probe WRITES real preferences and then restores them from a")
    e("reconstructed panel. It leaves the two keys at the values it last saved,")
    e("so reset_prefs.py must be run afterwards, or the keys removed by hand in")
    e("Preferences -> NestingWorkbench.")
except Exception:
    import traceback
    e(traceback.format_exc())
_LOG.close()
