#!/usr/bin/env freecad
"""Viability probe: can the workbench's worker/thread path be tested at all?

Run with the GUI binary, NOT freecadcmd:

    /home/james/freecad_env/usr/bin/freecad \
        tests/freecad_harness/probe_gui_session.py

Why this exists
---------------
master-merge.md §5 and the block 4.2/5.3 notes record that two planned blocks
are blocked on a question nobody had answered: are blocks 1.1
(`_MainThreadRelay` FIFO guard) and 1.2 (`_retire_worker`, the second-run
crash) testable before they are landed, or only after?

Both are about a QThread and the Qt event loop. `freecadcmd` has no GUI, so
`FreeCAD.GuiUp` is False, `ViewObject` is None, and a signal posted across
threads is never delivered -- there is no loop to deliver it. Xvfb and
xvfb-run are installed, so a virtual display was the obvious route.

The result is better than that: **no Xvfb is needed at all.** FreeCAD 26.3's
`freecad` binary starts with a live GUI on no display at all, reporting
GuiUp = 1. This probe verifies that and the four properties the worker path
actually depends on, so the answer is a measurement rather than a hope.

Findings, all verified on this box
----------------------------------
  1. `freecad` runs headless-of-display with a real GUI: GuiUp = 1,
     QApplication exists, main thread == app.thread().
  2. QThread genuinely runs: a worker thread reports it is *not* the app
     thread, so this is real threading and not a collapsed single thread.
  3. Cross-thread queued-signal delivery works: a relay posted 5 kicks from a
     worker thread and received all 5 on the main thread. This is exactly the
     mechanism `_MainThreadRelay` is built on.
  4. ViewObject is fully functional: `Visibility = False` takes effect. So the
     visual half of 4.2 -- master outlines, labels, visibility -- becomes
     assertable, which block 4.2's notes had to leave unverified.
  5. The real `NestingWorker` + `GACoordinator` pair runs on a thread and
     marshals a `create_population` draw payload across the boundary.

One trap this probe exists to record
------------------------------------
Signals need the event loop pumped *after* the worker finishes, not only while
it runs. The first attempt here waited on `isRunning()` and checked a `done`
signal immediately, saw nothing, and would have concluded the signal never
arrived. Pumping ~200 further iterations delivered it. Any worker-mode test
that waits and then asserts without draining the queue will produce a false
negative.

What is still needed for a worker-mode GA test
---------------------------------------------
The stand-in draw handler used here only counted payloads. `create_population`
is supposed to run `LayoutManager.create_ga_population` and stash the result
in `coordinator._pending_layouts`; without that, `_run_generation` receives
None and raises. So a real worker-mode test needs a faithful reproduction of
`NestingController._handle_draw_request` -- roughly 50 lines -- or it should
drive the controller itself. That is tractable and is the next piece of work,
not a blocker.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_probe")

_FAILURES = []
_CHECKS = [0]


def emit(message=""):
    try:
        import FreeCAD

        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


def check(label, condition, detail=""):
    _CHECKS[0] += 1
    if condition:
        emit(f"  PASS  {label}")
    else:
        emit(f"  FAIL  {label}  {detail}")
        _FAILURES.append(label)


def pump(app, predicate, limit=200000):
    """Pumps the event loop until predicate() or the limit is reached.

    Must also be called *after* the condition is satisfied, to drain anything
    already queued -- see the module docstring's trap.
    """
    spins = 0
    while not predicate() and spins < limit:
        app.processEvents()
        spins += 1
    return spins


def drain(app, iterations=500):
    for _ in range(iterations):
        app.processEvents()


def main():
    import FreeCAD
    import Part
    from PySide import QtCore, QtWidgets

    emit("=== GUI session viability ===")
    # FreeCAD.GuiUp is an int (1), not a bool, so this must be a truthiness
    # test. The workbench consistently writes `if FreeCAD.GuiUp:` for the same
    # reason; `is True` here would have reported a false negative.
    check("FreeCAD reports a live GUI with no Xvfb",
          bool(FreeCAD.GuiUp), f"GuiUp={FreeCAD.GuiUp!r}")
    check("FreeCADGui exposes updateGui", hasattr(FreeCADGui_probe := __import__("FreeCADGui"), "updateGui"))

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    check("a QApplication exists", app is not None)
    check("this is the app's main thread",
          QtCore.QThread.currentThread() == app.thread())

    # --- 2. a real thread
    class Thread(QtCore.QThread):
        def __init__(self):
            super().__init__()
            self.was_main = None

        def run(self):
            self.was_main = QtCore.QThread.currentThread() == app.thread()

    thread = Thread()
    thread.start()
    pump(app, lambda: not thread.isRunning())
    thread.wait(10000)
    drain(app)
    check("a QThread runs off the main thread",
          thread.was_main is False, f"was_main={thread.was_main}")

    # --- 3. cross-thread queued delivery, the _MainThreadRelay mechanism
    class Relay(QtCore.QObject):
        kick = QtCore.Signal()

        def __init__(self):
            super().__init__()
            self.hits = 0
            self.kick.connect(self._drain, QtCore.Qt.QueuedConnection)

        def _drain(self):
            self.hits += 1

    relay = Relay()

    class Poster(QtCore.QThread):
        def __init__(self, target):
            super().__init__()
            self.target = target

        def run(self):
            for _ in range(5):
                self.target.kick.emit()

    poster = Poster(relay)
    poster.start()
    pump(app, lambda: not poster.isRunning())
    poster.wait(10000)
    drain(app)
    check("queued signals cross the thread boundary",
          relay.hits == 5, f"hits={relay.hits}, wanted 5")

    # --- 4. ViewObject
    doc = FreeCAD.newDocument("probe")
    box = doc.addObject("Part::Feature", "x")
    box.Shape = Part.makeBox(10, 10, 10)
    doc.recompute()
    view = box.ViewObject
    check("ViewObject is live", view is not None)
    if view is not None:
        view.Visibility = False
        check("Visibility writes take effect", view.Visibility is False,
              f"Visibility={view.Visibility}")

    # --- 5. the real workbench worker pair
    try:
        from freecad.nestingworkbench.Tools.Nesting.ga_coordinator import GACoordinator
        from freecad.nestingworkbench.Tools.Nesting.nesting_controller import NestingWorker
        from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer

        worker_doc = FreeCAD.newDocument("probe_worker")
        sources = {}
        for label, (length, width) in (("Rect0", (60, 40)), ("Rect1", (45, 30))):
            obj = worker_doc.addObject("Part::Feature", label)
            obj.Shape = Part.makeBox(length, width, 10)
            sources[label] = obj
        worker_doc.recompute()
        target = worker_doc.addObject("App::DocumentObjectGroup", "Layout_target")

        ui = {
            "sheet_width": 300.0, "sheet_height": 220.0, "spacing": 5.0,
            "deflection": 0.05, "simplification": 0.1, "rotation_steps": 4,
            "add_labels": False, "font_path": "", "show_bounds": False,
            "label_height": 25.0, "label_size": 10.0, "verbose": False,
            "performance_logging": False, "algorithm": "Minkowski",
            "compactness_weight": 0.0, "generations": 1, "population_size": 1,
            "random_seed": 7, "sheet_thickness": 3.0, "deflection_angle": 30.0,
            "nesting_direction": 0, "use_random_direction": False,
        }
        quantities = {
            label: {"quantity": 2, "rotation_steps": 4,
                    "up_direction": "Z+", "fill_sheet": False}
            for label in sources
        }
        algo = {
            "generations": 1, "population_size": 1, "verbose": False,
            "performance_logging": False, "spacing": 5.0,
            "search_direction": (0, -1), "cancel_callback": lambda: False,
        }
        worker = NestingWorker(
            coordinator=None,
            run_args=(target, ui, quantities, sources, {}, algo, False, None),
            cancel_check_fn=lambda: False,
            parent=None,
        )
        coordinator = GACoordinator(
            doc=worker_doc,
            shape_preparer=ShapePreparer(worker_doc, {}),
            ui_callbacks={},
            draw_callback=worker.request_draw_on_main_thread,
            worker=worker,
        )
        worker.coordinator = coordinator

        seen = []

        def on_draw(payload):
            # Deliberately does NOT reproduce _handle_draw_request: this probe
            # only proves the payload crosses the boundary, which is the
            # viability question. The coordinator is then expected to fail
            # because _pending_layouts was never populated -- see the docstring.
            seen.append(sorted(payload.keys()))
            worker.notify_draw_complete()

        worker.draw_requested.connect(on_draw)
        outcome = {}
        worker.finished_signal.connect(lambda job: outcome.__setitem__("job", job))
        worker.error_signal.connect(lambda m: outcome.__setitem__("error", m))

        worker.start()
        spins = pump(app, lambda: not worker.isRunning())
        worker.wait(30000)
        drain(app)

        check("the workbench worker thread starts and finishes",
              not worker.isRunning(), f"spins={spins}")
        check("a draw payload was marshalled to the main thread",
              bool(seen), f"payloads={seen}")
        check("the worker surfaced a terminal outcome",
              "job" in outcome or "error" in outcome,
              f"outcome keys={sorted(outcome)}")
        check("an incomplete draw handler fails the run rather than hanging",
              outcome.get("error") is not None,
              "expected the stub handler to leave _pending_layouts unset")
    except Exception:
        emit(traceback.format_exc())
        _FAILURES.append("workbench worker probe raised")

    emit("")
    emit(f"{_CHECKS[0] - len(_FAILURES)}/{_CHECKS[0]} checks passed")
    if _FAILURES:
        emit("FAILED: " + ", ".join(_FAILURES))
        return 1
    return 0


if __name__ in ("__main__", "probe_gui_session"):
    try:
        _status = main()
    except Exception:
        traceback.print_exc()
        _status = 3
    try:
        with open(_STATUS_FILE, "w") as _handle:
            _handle.write(str(_status))
    except OSError:
        pass
    sys.exit(_status)
