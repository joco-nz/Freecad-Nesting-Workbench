"""Reproduce the leftover-layout report with the real threading, in a real GUI.

The structural tests establish that `run()` deletes layouts on the wrong thread
on the error path. They cannot establish whether the *normal* paths leak in the
panel, and the reported leftovers -- `MasterShapes`, `MasterShapes001`,
`Layout_GA_*` -- are not obviously from an error path at all.

This drives the real `NestingController` and the real `NestingWorker`, so the
worker is a genuine QThread and the draw payloads travel by genuine Qt signals.
Nothing is reimplemented: the payload dispatch is the controller's own
`_handle_draw_request`, reached through the real `_execute_ga_nesting`.

Two runs back to back, because a second `MasterShapes` in the document is
evidence that one run's group survived into the next -- which a single run
cannot show.

Run with the GUI binary, not freecadcmd. No display is needed: FreeCAD 26.3
starts with a live GUI on no display at all, which is what
probe_gui_session.py established.

    /home/james/freecad_env/usr/bin/freecad \
        tests/freecad_harness/probe_gui_layout_leak.py
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/home/james/dev/Freecad-Nesting-Workbench")
sys.path.insert(0, _HERE)
_LOG = open("/tmp/opencode/gui_leak.txt", "w")


def e(msg):
    _LOG.write(str(msg) + "\n")
    _LOG.flush()


# App::Origin and its datum children hang off a Part::Feature rather than a
# group, so they always look ungrouped. Counting them makes every run look like a
# 59-object leak.
DATUM = ("App::Origin", "App::Line", "App::Plane", "App::Point")
WANT = {"Spacer": 2, "Bottle Top": 6, "Bottle Bottom": 6}
# What a finished run is entitled to leave behind: the caller's target, the
# winning layout, the shared master group, and whatever NestingJob still holds.
EXPECTED_PREFIXES = ("Layout_target", "Layout_temp", "MasterShapes")


def strays(doc):
    """Layout groups that are not one of the legitimate survivors."""
    out = []
    for obj in doc.Objects:
        label = obj.Label
        if not label.startswith(("Layout", "MasterShapes")):
            continue
        if any(label == p or label.startswith(p) and label[len(p):].isdigit()
               for p in EXPECTED_PREFIXES):
            continue
        out.append(label)
    return out


def census(doc, tag):
    layouts = [o.Label for o in doc.Objects if o.Label.startswith("Layout")]
    masters = [o.Label for o in doc.Objects if o.Label.startswith("MasterShapes")]
    bad = strays(doc)
    e("")
    e("--- " + tag + " ---")
    e("  objects in document : %d" % len(doc.Objects))
    e("  Layout* groups      : %d  %s" % (len(layouts), layouts))
    e("  MasterShapes groups : %d  %s" % (len(masters), masters))
    e("  UNEXPECTED          : %d  %s" % (len(bad), bad))
    e("  VERDICT: %s" % ("CLEAN" if not bad else "LEAKED"))
    return bad


def main():
    import FreeCAD
    import FreeCADGui
    from PySide import QtCore, QtWidgets
    import harness_common as hc
    import importlib.util
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel
    from freecad.nestingworkbench.Tools.Nesting.nesting_controller import NestingController
    from freecad.nestingworkbench.datatypes.shape import Shape

    e("GuiUp = %s" % FreeCAD.GuiUp)
    e("main thread: %r" % QtCore.QThread.currentThread())

    # bench_ga's guard fires on __name__ of "__main__" or "bench_ga", so a plain
    # import runs a whole benchmark and sys.exit()s. SystemExit is not an
    # Exception, so it kills the probe silently. Load by path under a neutral
    # name.
    spec = importlib.util.spec_from_file_location(
        "ga_driver", os.path.join(_HERE, "bench_ga.py"))
    bench_ga = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench_ga)

    corpus = os.environ.get(
        "NEST_BENCH_CORPUS",
        os.path.join(_HERE, "..", "Test_Files",
                     "n70-intercooler-spacer-bottle-nesting.FCStd"))
    cfg = bench_ga.cfg()
    cfg["sheet"] = "1200x600"
    cfg["rotation_steps"] = 8
    cfg["generations"] = 2
    cfg["population"] = 2
    os.environ["NESTING_ROTATION_WORKERS"] = "4"
    width, height = (float(v) for v in cfg["sheet"].lower().split("x"))
    e("corpus : %s" % corpus)
    e("config : generations=%s population=%s rotations=%s"
      % (cfg["generations"], cfg["population"], cfg["rotation_steps"]))

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    panel = NestingPanel()
    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument("gui_leak")
    controller = NestingController(panel)

    parts = hc.discover_doc_parts(FreeCAD.openDocument(corpus))
    by_label = {p.Label: p for p in parts}
    full = {p.Label: {"quantity": WANT[p.Label],
                      "rotation_steps": cfg["rotation_steps"],
                      "up_direction": "Z+", "fill_sheet": False}
            for p in parts}
    ui_params = {
        "sheet_width": width, "sheet_height": height,
        "spacing": cfg["spacing"], "deflection": cfg["deflection"],
        "simplification": cfg["simplification"],
        "rotation_steps": cfg["rotation_steps"],
        "add_labels": False, "font_path": "", "show_bounds": False,
        "label_height": 25.0, "label_size": 10.0, "verbose": False,
        "performance_logging": True, "algorithm": "Minkowski",
        "compactness_weight": 0.0, "generations": cfg["generations"],
        "population_size": cfg["population"], "random_seed": cfg["seed"],
        "sheet_thickness": 3.0, "deflection_angle": 30.0,
        "nesting_direction": 0, "use_random_direction": False,
    }

    # A target group standing in for the one the user picks, created up front so
    # it is not itself part of any leak.
    target = doc.addObject("App::DocumentObjectGroup", "Layout_target")

    def pump(worker, timeout=600.0):
        """Run the Qt event loop until the worker finishes.

        The worker is a real QThread and the draw payloads need the main thread
        to keep turning, so the loop cannot be replaced by a blocking wait.
        """
        deadline = time.time() + timeout
        while worker.isRunning() and time.time() < deadline:
            app.processEvents(QtCore.QEventLoop.AllEvents, 50)
            FreeCADGui.updateGui()
            time.sleep(0.005)
        app.processEvents(QtCore.QEventLoop.AllEvents, 50)
        return not worker.isRunning()

    def one_run(index):
        Shape.clear_nfp_cache()
        Shape.clear_caches()
        controller.is_running = False
        controller.cancel_requested = False
        controller._execute_ga_nesting(
            target, ui_params, full, by_label, {}, {}, False, None)  # no cancel
        worker = controller._worker
        e("")
        e("=== run %d: worker is %r, has draw_callback=%r ==="
          % (index, worker, bool(worker.coordinator.draw_callback)))
        finished = pump(worker)
        e("  worker finished cleanly: %s" % finished)
        e("  cancel_requested=%s  current_job=%s"
          % (controller.cancel_requested,
             type(controller.current_job).__name__
             if controller.current_job else None))
        controller._worker = None
        return finished

    one_run(1)
    census(doc, "after a successful run")

    # ---- the early exit, which is what the report is about ----------------
    #
    # `_on_nesting_finished` only cleans up `if job:` -- and run()'s own comment
    # records that _finalize returns None when there is no best layout, which is
    # exactly the cancel case. `current_job` is only set on the success branch,
    # so `cancel_job()` then finds nothing either. Both cleanup paths can be
    # skipped together, with the coordinator's layouts and master group left in
    # the document.
    #
    # Cancelling after the first generation has completed is the interesting
    # case, because then a best layout exists and the job is real. Cancelling
    # before anything is nested is the other half; both are run.
    # Trip the cancel on observable progress rather than on a call count. A
    # count is useless here: the callback is called a handful of times early in
    # a run, so both counts tried either fired before a population existed (and
    # cleaned up nothing) or never fired at all (and was not a cancel). Waiting
    # for a Layout_GA group to exist guarantees the population is on the books,
    # which is the case that can leak.
    def ga_layouts_present(threshold):
        def check():
            if threshold == 0 or len(_ga_groups(controller.doc)) >= threshold:
                controller.cancel_requested = True
            return controller.cancel_requested
        return check

    for label, threshold in (("with 1 GA layout on the books", 1),
                            ("with 2 GA layouts on the books", 2)):
        e("")
        e("############ early exit: cancel %s ############" % label)
        fresh = FreeCAD.newDocument("cancel_%d" % threshold)
        # The first argument to _execute_ga_nesting is a target *group*, not a
        # document. Passing the document made the coordinator adopt it as a
        # layout group, which is why the census below reported zero objects --
        # including zero for the target this scenario had just created.
        tgt = fresh.addObject("App::DocumentObjectGroup", "Layout_target")
        Shape.clear_nfp_cache()
        Shape.clear_caches()
        controller.doc = fresh
        controller.is_running = False
        controller.cancel_requested = False
        checks = {"n": 0}

        def check(threshold=threshold):
            checks["n"] += 1
            if threshold == 0 or len(_ga_groups(controller.doc)) >= threshold:
                controller.cancel_requested = True
            return controller.cancel_requested

        controller._check_cancel = check
        controller._execute_ga_nesting(
            tgt, ui_params, full, by_label, {},
            {"cancel_callback": check}, False, None)
        worker = controller._worker
        finished = pump(worker)
        e("  cancel checks: %d   worker finished cleanly: %s"
          % (checks["n"], finished))
        e("  cancel_requested=%s  current_job=%s"
          % (controller.cancel_requested,
             type(controller.current_job).__name__
             if controller.current_job else None))
        e("  GA layouts present when it returned: %s"
          % _ga_groups(fresh))
        bad = census(fresh, "after an early exit, cancel %s" % label)
        e("  --> %s" % ("LEAKED" if bad else "clean"))
        controller._worker = None

def _ga_groups(doc):
    """Layout groups still carrying a GA name -- the ones a run should have
    deleted. `Layout_temp` and `Layout_target` are legitimate survivors."""
    try:
        return [o.Label for o in doc.Objects
                if o.Label.startswith("Layout_GA")]
    except ReferenceError:
        return ["<document deleted>"]



try:
    import FreeCAD  # noqa: F401 -- the panel needs a document before it opens
    import FreeCADGui  # noqa: F401
    main()
except BaseException:
    import traceback
    e(traceback.format_exc())
_LOG.close()
