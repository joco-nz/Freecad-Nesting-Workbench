"""Behavioural half of the layout-cleanup check: drive a GA run, count leftovers.

The structural half is tests/test_ga_layout_cleanup/, which pins that every
layout deletion goes through `_delete_layouts`. This one counts what is actually
left in the document, so the structural test cannot pass on a code shape that
still leaks.

Paths covered:
  1. a run to completion, headless
  2. an early exit via the cancel callback, headless
  3. an early exit with a draw_callback set -- the GUI shape, where the
     coordinator hands work to a main-thread owner instead of doing it itself.
     The stand-in owner here implements the full payload protocol, because a
     partial one leaves `_pending_layouts` as None and the worker crashes with
     "'NoneType' object is not iterable" -- which is not a finding, just an
     incomplete harness.
  4. an exception raised part way through, with a draw_callback set -- the path
     that was deleting from the worker thread

Run:  NEST_BENCH_CORPUS=tests/Test_Files/n70-...FCStd \\
          /home/james/freecad_env/usr/bin/freecadcmd \\
              tests/freecad_harness/probe_ga_layout_cleanup.py
"""
import os
import sys
import time
import importlib.util

sys.path.insert(0, "/home/james/dev/Freecad-Nesting-Workbench")
sys.path.insert(0, "/home/james/dev/Freecad-Nesting-Workbench/tests/freecad_harness")
_LOG = open("/tmp/opencode/cleanup_probe.txt", "w")

# App::Origin and its datum children are attached to a Part::Feature by FreeCAD
# itself rather than by a group, so they always look ungrouped. Counting them
# makes every run look like a 59-object leak.
DATUM = ("App::Origin", "App::Line", "App::Plane", "App::Point")
WANT = {"Spacer": 2, "Bottle Top": 6, "Bottle Bottom": 6}


def e(msg):
    _LOG.write(str(msg) + "\n")
    _LOG.flush()


def census(doc):
    grouped = set()
    for obj in doc.Objects:
        for child in (getattr(obj, "Group", None) or ()):
            grouped.add(child.Name)
    layouts = [o for o in doc.Objects if o.Label.startswith("Layout")]
    # A Layout group is legitimate if it is the caller's target, or the winner
    # (renamed Layout_temp), or the shared master group.
    keepers = {"Layout_target", "Layout_temp", "MasterShapes"}
    strays = [o.Label for o in layouts if o.Label not in keepers]
    orphans = [o.Label for o in doc.Objects
               if o.Name not in grouped
               and o.TypeId not in DATUM
               and o.TypeId != "App::DocumentObjectGroup"]
    return layouts, strays, orphans


def report(doc, tag):
    layouts, strays, orphans = census(doc)
    e("")
    e("--- " + tag + " ---")
    e("  objects in the document   : %d" % len(doc.Objects))
    e("  Layout* groups            : %d  %s"
      % (len(layouts), [o.Label for o in layouts]))
    e("  UNEXPECTED layout groups  : %d  %s" % (len(strays), strays))
    e("  ungrouped run objects     : %d  %s"
      % (len(orphans), orphans[:10]))
    e("  VERDICT: %s"
      % ("CLEAN" if not strays and not orphans else "LEAKED"))


def main():
    import FreeCAD
    import harness_common as hc
    from freecad.nestingworkbench.Tools.Nesting.ga_coordinator import GACoordinator
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.datatypes.shape import Shape

    # Loading by path: bench_ga's guard fires on `__name__` of either
    # "__main__" or "bench_ga", so a plain import runs a whole benchmark and
    # sys.exit()s, and `except Exception` does not catch SystemExit. That is why
    # the first attempt at this produced an empty log and looked like a silent
    # failure rather than a broken harness.
    spec = importlib.util.spec_from_file_location(
        "ga_driver",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "bench_ga.py"))
    bench_ga = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench_ga)

    corpus = os.environ.get(
        "NEST_BENCH_CORPUS",
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "..", "Test_Files",
                     "n70-intercooler-spacer-bottle-nesting.FCStd"))
    cfg = bench_ga.cfg()
    # The n70 config, not the synthetic default: the Spacer is 515mm wide and
    # the default sheet is 300x220, so nothing fits.
    cfg["sheet"] = "1200x600"
    cfg["rotation_steps"] = 8
    cfg["generations"] = 2
    cfg["population"] = 2
    os.environ["NESTING_ROTATION_WORKERS"] = "4"
    width, height = (float(v) for v in cfg["sheet"].lower().split("x"))
    e("corpus : " + corpus)
    e("config : sheet=%s rotations=%s generations=%s population=%s"
      % (cfg["sheet"], cfg["rotation_steps"], cfg["generations"],
         cfg["population"]))

    def ui_params():
        return {
            "sheet_width": width, "sheet_height": height,
            "spacing": cfg["spacing"], "deflection": cfg["deflection"],
            "simplification": cfg["simplification"],
            "rotation_steps": cfg["rotation_steps"],
            "add_labels": False, "font_path": "", "show_bounds": False,
            "label_height": 25.0, "label_size": 10.0, "verbose": False,
            "performance_logging": True, "algorithm": "Minkowski",
            "compactness_weight": 0.0, "generations": cfg["generations"],
            "population_size": cfg["population"],
            "random_seed": cfg["seed"], "sheet_thickness": 3.0,
            "deflection_angle": 30.0, "nesting_direction": 0,
            "use_random_direction": False,
        }

    def fresh():
        Shape.clear_nfp_cache()
        Shape.clear_caches()
        return FreeCAD.newDocument("p%d" % int(time.time() * 1000))

    def setup(doc):
        parts = hc.discover_doc_parts(FreeCAD.openDocument(corpus))
        target = doc.addObject("App::DocumentObjectGroup", "Layout_target")
        full = {p.Label: {"quantity": WANT[p.Label],
                          "rotation_steps": cfg["rotation_steps"],
                          "up_direction": "Z+", "fill_sheet": False}
                for p in parts}
        return parts, target, full

    def make_draw(coord, tally):
        """A stand-in main thread implementing the whole payload protocol.

        A partial one is worse than none: create_population and
        build_next_generation are executed by the owner and their result parked
        in `_pending_layouts`, which the worker reads back. Omit those and the
        worker reads None and dies with "'NoneType' object is not iterable" --
        a harness fault that looks exactly like a product crash.
        """
        def draw(payload):
            if payload.get("create_population"):
                coord._pending_layouts = coord.layout_manager.create_ga_population(
                    payload["master_map"], payload["quantities"],
                    payload["ui_params"], payload["population_size"],
                    payload["rotation_steps"],
                    verbose=payload.get("verbose", False))
            elif payload.get("build_next_generation"):
                coord._pending_layouts = coord._build_next_generation(
                    payload["gen"], payload["layouts"], payload["elites"],
                    payload["master_map"], payload["quantities"],
                    payload["ui_params"], payload["rotation_steps"],
                    payload["mutation_rate"], payload["immigrant_ratio"],
                    payload.get("verbose", False))
            elif payload.get("spawn_fill_part"):
                payload["result_holder"][0] = payload["spawn_fn"]()
            elif payload.get("cleanup_layouts"):
                victims = payload.get("layouts") or ()
                tally["handed"] += len(victims)
                for layout in victims:
                    if layout != payload.get("best_layout"):
                        tally["deleted"] += 1
                        coord.layout_manager.delete_layout(
                            layout, verbose=payload.get("verbose", False))
            elif payload.get("ga_finalize"):
                # What the controller does: finalize renames the winner to
                # Layout_temp and promotes it. Without this the winner is left
                # called Layout_GA_1, which is a harness artefact and not a
                # leak -- a trap this probe fell into first.
                result_holder = payload["result_holder"]
                result_holder[0] = coord._finalize(
                    payload["best_layout"], payload["best_efficiency"],
                    payload["total_time"], payload["target_layout"],
                    payload["ui_params"])
                coord.doc.recompute()
        return draw

    def run_case(name, doc, cancel, draw_cb, full, parts, target):
        coord = GACoordinator(
            doc=doc, shape_preparer=ShapePreparer(doc, {}),
            ui_callbacks={}, draw_callback=draw_cb, worker=None)
        tally = {"handed": 0, "deleted": 0}
        if draw_cb is not None:
            draw_cb = make_draw(coord, tally)
            coord.draw_callback = draw_cb
        job = coord.run(target, ui_params(), full,
                        {p.Label: p for p in parts}, {},
                        {"cancel_callback": cancel}, False, None)
        return coord, job, tally

    # 1. completion, headless
    doc = fresh()
    parts, target, full = setup(doc)
    e("")
    e("=== 1: run to completion, headless ===")
    coord, job, _ = run_case("completion", doc, lambda: False, None,
                             full, parts, target)
    e("  job returned: %s" % ("yes" if job is not None else "no"))
    report(doc, "after completion, headless")
    FreeCAD.closeDocument(doc.Name)

    # 2. early exit, headless
    doc = fresh()
    parts, target, full = setup(doc)
    counter = {"i": 0}

    def cancel_headless():
        # Trip after some nesting has happened, not on the first call: counting
        # every call trips it before any layout exists, which cleans up nothing
        # and proves nothing.
        counter["i"] += 1
        return counter["i"] > 30

    e("")
    e("=== 2: early exit, headless ===")
    coord, job, _ = run_case("cancel", doc, cancel_headless, None,
                             full, parts, target)
    e("  cancel checks: %d" % counter["i"])
    report(doc, "after early exit, headless")
    FreeCAD.closeDocument(doc.Name)

    # 3. early exit, GUI shape
    doc = fresh()
    parts, target, full = setup(doc)
    counter = {"i": 0}

    def cancel_gui():
        counter["i"] += 1
        return counter["i"] > 30

    e("")
    e("=== 3: early exit, draw_callback set (GUI shape) ===")
    coord, job, tally = run_case("cancel-gui", doc, cancel_gui, object(),
                                  full, parts, target)
    e("  cancel checks: %d   handed %d layouts, deleted %d"
      % (counter["i"], tally["handed"], tally["deleted"]))
    report(doc, "after early exit, GUI shape")
    FreeCAD.closeDocument(doc.Name)

    # 4. exception part way through, GUI shape -- the path that was wrong
    doc = fresh()
    parts, target, full = setup(doc)
    from freecad.nestingworkbench.Tools.Nesting.layout_manager import LayoutManager
    # Patched on the class, not the instance: run() constructs its own
    # LayoutManager and assigns it, so anything installed beforehand is
    # discarded. The first version of this case patched the instance and
    # recorded zero calls while the run proceeded normally.
    real_create = LayoutManager.create_layout
    calls = {"n": 0}

    def failing_create(self_, *a, **k):
        calls["n"] += 1
        # The fourth call is inside a later generation, after the first
        # population exists, so the coordinator's error handler has real layouts
        # to dispose of -- which is the case under test.
        if calls["n"] >= 4:
            raise RuntimeError("simulated failure inside a later generation")
        return real_create(self_, *a, **k)

    LayoutManager.create_layout = failing_create

    e("")
    e("=== 4: an exception part way through, draw_callback set ===")
    coord = GACoordinator(doc=doc, shape_preparer=ShapePreparer(doc, {}),
                          ui_callbacks={}, draw_callback=None, worker=None)
    tally = {"handed": 0, "deleted": 0}
    coord.draw_callback = make_draw(coord, tally)
    raised = None
    try:
        coord.run(target, ui_params(), full, {p.Label: p for p in parts}, {},
                  {}, False, None)
    except BaseException as exc:
        raised = exc
    e("  create_layout calls before failing: %d" % calls["n"])
    e("  propagated out of run(): %s: %s" % (type(raised).__name__, raised))
    e("  handed %d layouts to the main thread, deleted %d"
      % (tally["handed"], tally["deleted"]))
    report(doc, "after an exception, GUI shape")
    LayoutManager.create_layout = real_create
    FreeCAD.closeDocument(doc.Name)


try:
    import FreeCAD  # noqa: F401 -- needed before any document is created
    main()
except BaseException:
    import traceback
    e(traceback.format_exc())
_LOG.close()
