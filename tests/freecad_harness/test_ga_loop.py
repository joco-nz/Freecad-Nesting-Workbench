#!/usr/bin/env freecadcmd
"""Integration test for the GA loop and the commit path.

Why this exists
---------------
Until now nothing in tests/ constructed `GACoordinator` or
`NestingController`. The harness drives `nesting_logic.nest()` directly and
4.1/4.2 were verified by driving `LayoutManager` + `NestingJob` directly. So
the GA loop itself -- generations, population, early stop, survivor cleanup,
`_finalize`, `materialize_layout_objects`, and the commit that follows -- had no
coverage at all. That matters directly: the `_job_committed` gate added in 4.2
lives in `GACoordinator.run()`'s `finally`, and it decides whether the pooled
master group is deleted out from under a pending commit. Untested, it is a
loaded gun pointed at every run.

How the loop is driven
----------------------
`GACoordinator.__init__` takes `ui_callbacks`, `draw_callback` and `worker` as
optional, and with `draw_callback=None` it runs synchronously on the calling
thread. That is the same path the non-worker (single-threaded) execution takes,
so this is real coverage rather than a mock. What it cannot cover is the
QThread/Qt event loop, which needs a GUI session -- see RESULTS-5.3 and the
xvfb probe for that.

Writing this test immediately found a headless blocker: `GACoordinator.run()`
called `FreeCADGui.updateGui()` unconditionally, and `FreeCADGui` imports under
freecadcmd but has no `updateGui`, so the loop died on its first redraw. Fixed
by routing those three sites through `freecad_helpers.refresh_gui()`.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_ga")

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


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
PARTS = (("Rect0", 60, 40), ("Rect1", 45, 30), ("Tri", 40, 35))

UI_PARAMS = {
    "sheet_width": 300.0,
    "sheet_height": 220.0,
    "spacing": 5.0,
    "deflection": 0.05,
    "simplification": 0.1,
    "rotation_steps": 4,
    "add_labels": False,
    "font_path": "",
    "show_bounds": False,
    "label_height": 25.0,
    "label_size": 10.0,
    "verbose": False,
    "performance_logging": True,
    "algorithm": "Minkowski",
    "compactness_weight": 0.0,
    "generations": 2,
    "population_size": 2,
    "random_seed": 1234,
    "sheet_thickness": 3.0,
    "deflection_angle": 30.0,
    "nesting_direction": 0,
    "use_random_direction": False,
}

ALGO_KWARGS = {
    "generations": 2,
    "population_size": 2,
    "verbose": False,
    "performance_logging": True,
    "spacing": 5.0,
    "search_direction": (0, -1),
}

STALE_MASTER_LABELS = ("master_STALE",)


def build_doc(name):
    import FreeCAD
    import Part

    from freecad.nestingworkbench.datatypes.shape import Shape

    doc = FreeCAD.newDocument(name)
    sources = {}
    for label, length, width in PARTS:
        obj = doc.addObject("Part::Feature", label)
        obj.Shape = Part.makeBox(length, width, 10)
        sources[label] = obj
    doc.recompute()

    target = doc.addObject("App::DocumentObjectGroup", "Layout_target")
    # A previous run's master row, so replacement is observable.
    stale_group = doc.addObject("App::DocumentObjectGroup", "MasterShapes")
    stale_group.Label = "MasterShapes"
    target.addObject(stale_group)
    for label in STALE_MASTER_LABELS:
        stale_group.addObject(doc.addObject("Part::Feature", label))

    Shape.clear_nfp_cache()
    Shape.clear_caches()
    return doc, sources, target


def quantities_for(sources):
    return {
        label: {"quantity": 2, "rotation_steps": 4,
                "up_direction": "Z+", "fill_sheet": False}
        for label in sources
    }


def make_coordinator(doc):
    from freecad.nestingworkbench.Tools.Nesting.ga_coordinator import GACoordinator
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer

    return GACoordinator(
        doc=doc,
        shape_preparer=ShapePreparer(doc, {}),
        ui_callbacks={},          # optional; run() tolerates its absence
        draw_callback=None,       # -> synchronous, on this thread
        worker=None,
    )


def master_objects(doc, exclude_previous_run=True):
    """Master-ish object labels in the document.

    The target layout is seeded with a stale master row so that replacement is
    observable. Those objects are not this run's, so assertions about "what the
    run left behind" must exclude them -- an abandoned run has no reason to
    delete a previous run's committed masters.
    """
    labels = sorted(
        o.Label for o in doc.Objects
        if o.Label.startswith(("master_", "temp_master_", "master_shape_", "bound_"))
    )
    if not exclude_previous_run:
        return labels
    return [l for l in labels if l not in STALE_MASTER_LABELS]


def reachable_from(root):
    seen = set()

    def walk(group):
        for child in getattr(group, "Group", []) or []:
            if child.Name in seen:
                continue
            seen.add(child.Name)
            walk(child)

    walk(root)
    return seen


# --------------------------------------------------------------------------
# Case 1: a full GA run, then commit
# --------------------------------------------------------------------------
def case_success():
    emit("")
    emit("--- GA run, then commit ---")
    doc, sources, target = build_doc("ga_success")
    coordinator = make_coordinator(doc)

    job = coordinator.run(
        target, UI_PARAMS, quantities_for(sources), sources, {},
        dict(ALGO_KWARGS, cancel_callback=lambda: False), False, viz_manager=None,
    )

    check("run() returns a NestingJob", job is not None
          and type(job).__name__ == "NestingJob", f"got {type(job).__name__}")
    check("the job owns the masters, so the finally block must not dispose them",
          coordinator._job_committed is True)
    check("the target layout is untouched until commit",
          not any(c.Label.startswith("Sheet_") for c in target.Group),
          f"got {[c.Label for c in target.Group]}")

    # The GA must actually have run a GA, not silently short-circuited.
    perf = coordinator._ga_perf or {}
    check("performance counters were captured", bool(perf), "empty _ga_perf")
    check("more than one layout was evaluated",
          perf.get("layout_evaluations", 0) > 1,
          f"layout_evaluations={perf.get('layout_evaluations')}")
    # Offspring alone is the wrong probe: at population_size 2 the arithmetic is
    # n_immigrants = max(1, int(1 * 0.15)) = 1 and n_offspring = 1 - 1 = 0, so a
    # population of 2 legitimately breeds no offspring at all. The breeding step
    # running is what matters.
    bred = perf.get("offspring_layouts", 0) + perf.get("immigrant_layouts", 0)
    check("the generation loop bred a next generation", bred > 0,
          f"offspring={perf.get('offspring_layouts')} "
          f"immigrants={perf.get('immigrant_layouts')}")

    placed = sum(len(s.parts) for s in job.sheets)
    check("the winner placed parts", placed > 0, f"placed={placed}")

    # Committed masters must be reachable from the target, and the stale row
    # replaced. This is the 4.1 guarantee, now exercised through the real GA.
    job.commit()
    children = [c.Label for c in target.Group]
    check("commit added sheet groups",
          any(c.startswith("Sheet_") for c in children), f"got {children}")
    committed = [c for c in target.Group if c.Label.startswith("MasterShapes")]
    check("commit promoted a MasterShapes group", len(committed) == 1,
          f"got {len(committed)}")
    if committed:
        labels = sorted(c.Label for c in committed[0].Group)
        check("the promoted masters are this run's",
              all(not l.startswith("master_STALE") for l in labels), f"got {labels}")
    check("the previous run's stale master was deleted",
          all(doc.getObject(l) is None for l in STALE_MASTER_LABELS))

    from_target = reachable_from(target)
    orphans = [o.Label for o in doc.Objects
               if o.Label.startswith(("master_", "temp_master_", "master_shape_", "bound_"))
               and o.Name not in from_target]
    check("no master objects orphaned outside the target layout", not orphans,
          f"orphaned {orphans}")

    # The winner's part features must have been materialized, or Sheet.draw has
    # nothing to re-parent.
    nested = [o for o in doc.Objects if o.Label.startswith("nested_")]
    check("the winning layout's parts were materialized", len(nested) > 0,
          f"nested_* objects: {len(nested)}")


# --------------------------------------------------------------------------
# Case 2: cancelled run
# --------------------------------------------------------------------------
def case_cancel():
    emit("")
    emit("--- cancelled run ---")
    doc, sources, target = build_doc("ga_cancel")
    coordinator = make_coordinator(doc)

    job = coordinator.run(
        target, UI_PARAMS, quantities_for(sources), sources, {},
        dict(ALGO_KWARGS, cancel_callback=lambda: True), False, viz_manager=None,
    )

    check("a cancelled run returns no job", job is None, f"got {type(job).__name__}")
    check("a cancelled run never claims the masters",
          coordinator._job_committed is False)
    check("the shared master group was disposed",
          master_objects(doc) == [],
          f"left {master_objects(doc)}")
    leftovers = [o.Label for o in doc.Objects
                 if o.Label.startswith("Layout_GA") or o.Label == "Layout_temp"]
    check("no GA layout groups were left behind", not leftovers,
          f"left {leftovers}")


# --------------------------------------------------------------------------
# Case 3: a run that raises
# --------------------------------------------------------------------------
def case_error():
    emit("")
    emit("--- run that raises ---")
    doc, sources, target = build_doc("ga_error")
    coordinator = make_coordinator(doc)

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic failure in _finalize")

    coordinator._finalize = boom

    job = coordinator.run(
        target, UI_PARAMS, quantities_for(sources), sources, {},
        dict(ALGO_KWARGS, cancel_callback=lambda: False), False, viz_manager=None,
    )

    check("a failed run returns no job", job is None, f"got {type(job).__name__}")
    check("a failed run never claims the masters",
          coordinator._job_committed is False)
    check("the shared master group was disposed after the failure",
          master_objects(doc) == [],
          f"left {master_objects(doc)}")
    check("the target layout still holds its previous contents",
          any(c.Label.startswith("MasterShapes") for c in target.Group))


def main():
    for case in (case_success, case_cancel, case_error):
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


if __name__ in ("__main__", "test_ga_loop"):
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
