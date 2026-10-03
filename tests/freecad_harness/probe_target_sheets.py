#!/usr/bin/env freecad
"""End-to-end guard for the panel's "Stop At Sheets" dial.

Run with the GUI binary, NOT freecadcmd:

    /home/james/freecad_env/usr/bin/freecad \
        tests/freecad_harness/probe_target_sheets.py

Why this exists
---------------
The dial was dead. `GACoordinator` reads its target from
`algo_kwargs['target_sheets']`, and its behaviour was correct and well covered:
test_ga_loop.py has five cases for it (met, not-met-by-dropping-parts,
unreachable, fill-only, and a target hit being a success rather than a cancel).
What did not exist was any path from the widget to that dict.

    ui_nesting.NestingPanel            spinbox built, "Stop At Sheets:", 0-100
      -> controller._collect_ui_params        did not read it
      -> controller._prepare_algo_kwargs      did not set algo_kwargs['target_sheets']
      -> ga_coordinator.run                   reads algo_kwargs['target_sheets']

So the coordinator was handed no target, defaulted it to 0, and the feature was
permanently off. The dial did nothing at all, silently: no error, no warning,
just a run that went to completion.

It survived because every existing test passes `target_sheets` *directly* into
`ALGO_KWARGS`, bypassing the panel. The engine was tested; the wiring never was.
That is the lesson this file exists to enforce, so the assertions go through the
panel rather than around it -- and the strongest one runs a real GA on
panel-derived kwargs, which is the only version of this check that would have
caught the original bug.

What is asserted
----------------
1. The value survives panel -> _collect_ui_params -> _prepare_algo_kwargs.
2. A GA run driven by those kwargs reports the target met and stops on the
   first layout, with a job still returned (a target hit is a success, not a
   cancel -- if it travelled the cancel path the fill phase and the completion
   message would be skipped for a run that succeeded).
3. 0 means off, and the run then completes on the usual rules.
4. It is still not persisted, which is a decision the widget documents and one
   that is easy to break by accident by adding a `save_settings` line.

Fixture safety
--------------
Every document here is created with `FreeCAD.newDocument`; no corpus file is
ever opened, so there is nothing to write back. `_close_out` closes documents
with `FreeCAD.closeDocument`, which *discards* -- it does not prompt and does
not save -- and nothing in this file calls `doc.save()`. That is deliberate: a
fixture must come out of a run byte-identical.

Reused, not duplicated: `build_doc`, `quantities_for` and `make_coordinator`
come from test_ga_loop.py -- see `_load_ga_fixtures` for why that takes
importlib rather than a plain import.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import FreeCAD
import FreeCADGui

FreeCADGui.showMainWindow()

_STATUS_FILE = os.path.join(_HERE, ".last_status_target_sheets")
_FAILURES = []
_CHECKS = [0]
_OPENED_DOCS = []

# The fixture's sheet, which is not what this file is about. Generations,
# population and rotation threads are set on the panel instead, so the GA
# configuration really is panel-driven: 2 x 2 = 4 layouts, and stopping after
# 1 cannot be coincidence. Rotation threads are pinned to 1 for determinism --
# above 1 the tie-break draw in score_gravity runs in worker threads against one
# shared random.Random, so consumption order is scheduling dependent. The
# harness pins this everywhere it measures; see run_ga.sh.
SHEET_W, SHEET_H = 300.0, 220.0


def emit(message=""):
    try:
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


def _load_ga_fixtures():
    """Load test_ga_loop.py's fixtures without running its suite.

    A plain `import test_ga_loop` does not work, and fails in a way worth
    recording: that module ends in `sys.exit()`, and its entry guard is
    `if __name__ in ("__main__", "test_ga_loop")` -- deliberately, so pytest can
    collect it. Importing it therefore runs all 36 of its cases and then exits
    the process, silently swallowing this probe. The first version of this file
    did exactly that and reported 36/36 while checking nothing at all.

    So the module is loaded under a different name, which makes that guard False
    and leaves the helpers defined without the suite running. Copying them
    instead would let the fixture and the thing under test drift apart with
    nothing to notice.
    """
    import importlib.util

    path = os.path.join(_HERE, "test_ga_loop.py")
    spec = importlib.util.spec_from_file_location("_ga_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ga = _load_ga_fixtures()


def new_doc(name):
    """A tracked, unsaved document. Never opened from disk, never saved."""
    doc = FreeCAD.newDocument(name)
    _OPENED_DOCS.append(doc.Name)
    return doc


def close_out():
    """Discard every document, then shut the GUI down.

    Two separate problems, and the existing GUI probes only solve the first.

    `FreeCAD.closeDocument` discards: it does not prompt, so the Unsaved Document
    dialog never appears and nothing is written. That is why every probe here
    closes its documents explicitly rather than leaving them for the exit path,
    and why none of them call `doc.save()`.

    The GUI close is the part that is easy to miss. `sys.exit()` at the end of
    the script leaves the main window on screen -- the process is gone but the
    window still has to be dismissed by hand. `getMainWindow().close()` plus
    `QApplication.quit()` makes the session end on its own.
    """
    for name in list(_OPENED_DOCS):
        try:
            if name in FreeCAD.listDocuments():
                FreeCAD.closeDocument(name)
        except Exception as exc:
            emit(f"  note: closing {name} raised {exc}")
    _OPENED_DOCS.clear()

    try:
        from PySide import QtWidgets

        FreeCADGui.getMainWindow().close()
        app = QtWidgets.QApplication.instance()
        if app is not None:
            app.quit()
    except Exception as exc:
        emit(f"  note: GUI close raised {exc}")


def make_panel(doc_name):
    """A panel on a fresh document, with the algorithm and GA budget set.

    Every value comes from the widget, because the whole point is that the
    widget is the source.
    """
    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

    doc = new_doc(doc_name)
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]
    FreeCAD.setActiveDocument(doc.Name)

    panel = NestingPanel()
    panel.algorithm_dropdown.setCurrentText("Minkowski")
    panel.minkowski_generations_input.setValue(2)
    panel.minkowski_population_size_input.setValue(2)
    panel.minkowski_rotation_workers_input.setValue(1)
    return doc, panel


def dispose(panel):
    """Tear a panel down the way probe_unit_panel does, tolerating a reraise."""
    try:
        panel.dispose()
        panel.deleteLater()
    except Exception as exc:
        emit(f"  note: panel dispose raised {exc}")


def panel_kwargs(panel):
    """The production path from widget to coordinator kwargs.

    _collect_ui_params then _prepare_algo_kwargs, in that order, because that is
    the order the controller uses them in.
    """
    params = panel.controller._collect_ui_params()
    return params, panel.controller._prepare_algo_kwargs(params)


def ga_ui_params(params):
    """Panel params, with only the sheet size and seed replaced.

    Sheet size comes from persisted preferences, so it is whatever the last
    session left; substituting the fixture's is honest -- it is not the subject
    of this file. Everything else, including the target, is panel-derived.
    """
    out = dict(params)
    out["sheet_width"] = SHEET_W
    out["sheet_height"] = SHEET_H
    out["algorithm"] = "Minkowski"
    out["random_seed"] = 1234
    return out


def run_ga(panel):
    """One GA run on panel-derived kwargs. Returns (coordinator, job, perf).

    Deliberately takes no target argument and sets none. An earlier version
    accepted `target_sheets` and assigned it into the kwargs, which quietly made
    the end-to-end assertions decorative: with the controller's wiring reverted,
    the wiring checks failed but the run still stopped on layout 1 and the GA
    assertions passed. The run has to be driven by whatever the panel produced,
    or it is not testing the panel.
    """
    params, kwargs = panel_kwargs(panel)
    params = ga_ui_params(params)
    kwargs["cancel_callback"] = lambda: False
    kwargs["generations"] = params["generations"]
    kwargs["population_size"] = params["population_size"]
    # performance_logging populates _ga_perf, and layout_evaluations is the
    # counter that proves whether the run stopped early. Read off the panel for
    # the same reason as everything else: injecting it would let this check pass
    # while the control was dead.
    kwargs["performance_logging"] = params["performance_logging"] = True
    # Drop the panel's Qt log widget before it can be called off-thread.
    #
    # `_prepare_algo_kwargs` puts `log_callback=NestingPanel.log_message` -- a Qt
    # widget -- into algo_kwargs, and `nesting_strategy` calls it from the
    # rotation worker threads (nesting_strategy.py:449,1402). With performance
    # logging on that is thousands of [TIMING]/[GA PERF] lines, and touching a
    # widget from a worker aborts the process:
    #
    #     GUI API 'FreeCADGui.updateGui' may only be used from the main thread.
    #     terminate called after throwing an instance of 'Py::RuntimeError'
    #
    # Production never hits this, because `_run_generation` replaces log_callback
    # with a console sink whenever `draw_callback` is set. This probe passes
    # `draw_callback=None` -- the synchronous path bench_ga and test_ga_loop use
    # -- so the substitution does not happen and the widget survives into the
    # run. bench_ga.py passes a null sink for the same reason; production routes
    # to the console. Either beats the widget.
    #
    # Worth knowing as a trap rather than only as a fix: any caller that combines
    # panel-derived kwargs, performance_logging, and draw_callback=None will
    # abort, and the stack points at the coordinator rather than at the widget.
    kwargs["log_callback"] = lambda msg, level=None: None

    doc, sources, target = ga.build_doc("target_probe_ga")
    _OPENED_DOCS.append(doc.Name)

    coordinator = ga.make_coordinator(doc)
    job = coordinator.run(
        target, params, ga.quantities_for(sources), sources, {},
        kwargs, False, viz_manager=None,
    )
    return coordinator, job, dict(coordinator._ga_perf or {})


def case_target_reaches_the_engine():
    """A target set on the dial must stop a real run on its first layout."""
    emit("")
    emit("--- Stop At Sheets: panel dial reaches the engine ---")

    _, panel = make_panel("target_wiring")
    try:
        panel.minkowski_target_sheets_input.setValue(1)

        params, kwargs = panel_kwargs(panel)
        check("the dial survives _collect_ui_params",
              params.get("target_sheets") == 1,
              f"target_sheets={params.get('target_sheets')!r}")
        check("...and reaches algo_kwargs, which is where the coordinator reads it",
              kwargs.get("target_sheets") == 1,
              f"target_sheets={kwargs.get('target_sheets')!r}")

        coordinator, job, perf = run_ga(panel)
        check("the run reports the target as met",
              coordinator._target_met is True,
              f"_target_met={coordinator._target_met}")
        # The actual early exit. The panel asked for 2 generations x 2 layouts,
        # so a run that stopped on the first layout cannot have finished.
        check("the run stopped on the first layout instead of all four",
              perf.get("layout_evaluations") == 1,
              f"layout_evaluations={perf.get('layout_evaluations')}")
        # A target hit must not travel the cancel path, or the fill phase and
        # the completion message are skipped for a run that succeeded.
        check("a target hit still returns a job",
              job is not None, f"got {type(job).__name__}")
        if job is not None:
            check("...holding the target layout's sheets",
                  len(job.sheets) >= 1, f"sheets={len(job.sheets)}")
            check("...with nothing left unplaced",
                  not getattr(job, "unplaced", []),
                  f"unplaced={len(getattr(job, 'unplaced', []) or [])}")
    finally:
        dispose(panel)


def case_off_means_off():
    """0 is the documented default and must leave the run alone."""
    emit("")
    emit("--- Stop At Sheets: 0 means off ---")

    _, panel = make_panel("target_off")
    try:
        check("a fresh panel defaults the dial to off",
              panel.minkowski_target_sheets_input.value() == 0,
              f"got {panel.minkowski_target_sheets_input.value()}")

        _, kwargs = panel_kwargs(panel)
        check("off reaches the coordinator as 0, not as absent",
              kwargs.get("target_sheets") == 0,
              f"target_sheets={kwargs.get('target_sheets')!r}")

        coordinator, job, perf = run_ga(panel)
        check("an off target is not reported as met",
              coordinator._target_met is False,
              f"_target_met={coordinator._target_met}")
        check("an off target does not stop the run early",
              perf.get("layout_evaluations", 0) > 1,
              f"layout_evaluations={perf.get('layout_evaluations')}")
        check("the run still returns a job", job is not None,
              f"got {type(job).__name__}")
    finally:
        dispose(panel)


def case_still_not_persisted():
    """A stale target armed against a later default run is a surprise.

    The widget says so deliberately, and it is the kind of decision that gets
    reversed by one `save_settings` line nobody connects to this behaviour.
    """
    emit("")
    emit("--- Stop At Sheets: still not persisted ---")

    _, panel = make_panel("target_persist")
    panel.minkowski_target_sheets_input.setValue(3)
    panel.controller._collect_ui_params()
    dispose(panel)

    from freecad.nestingworkbench.Tools.Nesting.ui_nesting import NestingPanel

    doc = new_doc("target_persist_reopen")
    enums = doc.getEnumerationsOfProperty("UnitSystem")
    doc.UnitSystem = enums[6]
    FreeCAD.setActiveDocument(doc.Name)

    reopened = NestingPanel()
    try:
        check("a reopened panel does not inherit an armed target",
              reopened.minkowski_target_sheets_input.value() == 0,
              f"got {reopened.minkowski_target_sheets_input.value()}")
    finally:
        dispose(reopened)


def main():
    for case in (case_target_reaches_the_engine,
                 case_off_means_off,
                 case_still_not_persisted):
        try:
            case()
        except Exception:
            emit(f"  ERROR in {case.__name__}:")
            emit(traceback.format_exc())
            _FAILURES.append(f"{case.__name__} raised")
    emit("")
    emit(f"{_CHECKS[0] - len(_FAILURES)}/{_CHECKS[0]} checks passed")
    if _FAILURES:
        emit("FAILED: " + ", ".join(_FAILURES))
        return 1
    return 0


_status = main()
try:
    with open(_STATUS_FILE, "w") as _handle:
        _handle.write(str(_status))
except OSError:
    pass

# After the verdict, and before sys.exit, or the window outlives the process and
# has to be closed by hand.
close_out()
sys.exit(_status)