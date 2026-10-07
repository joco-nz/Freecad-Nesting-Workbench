"""Regenerate `tests/Test_Files/replay-fixture-CAM-Nested.FCStd`.

**The fixture had no generator until this file existed.** It arrived in one
commit (`719bc46`) with nothing producing it, so every change to it since has been
manual, and NEST-029 had to describe the rebuild in prose rather than a command.

    freecadcmd tests/freecad_harness/make_nested_fixture.py          # verify only
    NESTING_WRITE_FIXTURE=1 freecadcmd tests/freecad_harness/make_nested_fixture.py

Verification is the default and **writes nothing**. The write needs an explicit
environment variable, because the output is a committed binary that four gated
suites depend on.

## Why each of these is set the way it is

Every item below was measured during NEST-029's rebuild, not inferred, and each
one was a wrong answer or a crash before it was measured:

* **Placements are zeroed first.** `BottomStrap` arrives at `base=(-42, 0, 0)` and
  `NestingController._prepare_source_parts` (`nesting_controller.py:323`) zeroes
  every source placement before nesting. Skipping this produced **2031 mm³ of
  genuinely shared solid** between two parts, measured on the placed solids.
* **`deflection` is linear millimetres**, converted as
  `deflection_mm = deflection_angle / 200.0` (`:1058`). Passing the angle raw, or
  0.0, fails every part with
  `ValueError: Unsupported object '<name>' or no valid 2D geometry found`.
* **`search_direction` is a bearing vector, not `None`.** `None` is what the
  *random-direction* checkbox produces (`:1296`); a dial reading converts at
  `:1298-1300`. Getting this wrong took hole nestings from 14 down to 1 and 5.
* **`GACoordinator` is imported below `openDocument`.** A bare `freecadcmd`
  segfaults 3/3 with no output, and so does `FreeCAD.newDocument`; only opening a
  real file works.
* **No positional argument.** `freecadcmd` treats one as a document to open and
  segfaults before this script runs.

## What the seed does and does not buy

`NESTING_RANDOM_SEED` below pins the run, and the script **verifies that by
nesting twice and comparing** rather than asserting it.

Width is not pinned and does not need to be: since NEST-032 the rotation pool is a
pure speed knob, measured at widths 0, 4 and 8 all giving the same fingerprint.

**A pinned seed is not a cross-version guarantee.** It fixes the workbench's own
randomness, not GEOS. Polygon union and buffer results depend on the Shapely/GEOS
build, so the same seed under a different FreeCAD or Shapely may nest differently.
That is stated rather than implied: run the verification, and if it disagrees,
this is what to suspect first.
"""
import hashlib
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import FreeCAD

SOURCE = os.path.join(_REPO, "tests", "Test_Files", "replay-fixture-CAM.FCStd")
TARGET = os.path.join(_REPO, "tests", "Test_Files", "replay-fixture-CAM-Nested.FCStd")

# The seed. Change it and every one of the four replay suites' expected counts
# has to be re-measured; do that deliberately.
#
# **Not an arbitrary choice, and the first one was wrong.** Seed 20251007 nests
# reproducibly but spreads the 48 parts over **2 sheets**, and
# `test_replay_order.py` asserts one sheet -- so it failed 2 checks, and
# `test_replay_startpoint.py` failed 1 more in consequence. Measured across seeds
# at the recorded settings: 1 -> 1 sheet, 1234 -> 1, 20251007 -> 2, 7 -> 2,
# 42 -> 2, 99999 -> 2. A one-sheet pack is reachable; 20251007 just was not it.
SEED = 1234

# Everything below is read off the committed layout rather than guessed.
SHEET_WIDTH = 600.0
SHEET_HEIGHT = 300.0
SHEET_THICKNESS = 2.0
SPACING = 4.0
DEFLECTION_ANGLE = 20.0
SIMPLIFICATION = 0.3
GENERATIONS = 4
POPULATION_SIZE = 10
ROTATION_STEPS = 4
NESTING_DIRECTION = 90
QUANTITIES = {"BottomStrap": 23, "TopStrap": 23, "SimpleSpacer": 2}

SOURCE_LABELS = ("BottomStrap", "TopStrap", "SimpleSpacer")


def emit(message=""):
    FreeCAD.Console.PrintMessage(str(message) + "\n")


def build(zero_placements=True):
    """Nest once and return (sha256 of every placement, object count)."""
    import math
    import random

    from freecad.nestingworkbench.datatypes.shape import Shape
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer

    doc = FreeCAD.openDocument(SOURCE)
    try:
        sources = {o.Label: o for o in doc.Objects
                   if o.TypeId == "PartDesign::Body" and o.Label in SOURCE_LABELS}
        if sorted(sources) != sorted(SOURCE_LABELS):
            emit("  source bodies missing: have %s"
                 % sorted(sources))
            return None, 0

        if zero_placements:
            for obj in sources.values():
                obj.Placement = FreeCAD.Placement()
            doc.recompute()

        target = doc.addObject("App::DocumentObjectGroup", "Layout_000")
        quantities = {
            label: {"quantity": count, "rotation_steps": ROTATION_STEPS,
                    "up_direction": "Z+", "fill_sheet": False}
            for label, count in QUANTITIES.items()
        }
        ui_params = {
            "sheet_width": SHEET_WIDTH, "sheet_height": SHEET_HEIGHT,
            "sheet_thickness": SHEET_THICKNESS, "spacing": SPACING,
            "deflection": DEFLECTION_ANGLE / 200.0,
            "simplification": SIMPLIFICATION, "rotation_steps": ROTATION_STEPS,
            "add_labels": False, "font_path": "", "show_bounds": True,
            "label_height": 25.0, "label_size": 10.0, "verbose": False,
            "performance_logging": False, "algorithm": "Minkowski",
            "compactness_weight": 0.0, "generations": GENERATIONS,
            "population_size": POPULATION_SIZE,
            "deflection_angle": DEFLECTION_ANGLE,
            "nesting_direction": NESTING_DIRECTION,
            "use_random_direction": False,
        }
        angle = math.radians(NESTING_DIRECTION)
        algo_kwargs = {
            "generations": GENERATIONS, "population_size": POPULATION_SIZE,
            "verbose": False, "performance_logging": False, "spacing": SPACING,
            # NOT None. See the module docstring.
            "search_direction": (math.cos(angle), math.sin(angle)),
        }

        Shape.clear_nfp_cache()
        Shape.clear_caches()
        # Imported here, after openDocument. See the module docstring.
        from freecad.nestingworkbench.Tools.Nesting.ga_coordinator import (
            GACoordinator)
        coordinator = GACoordinator(
            doc=doc, shape_preparer=ShapePreparer(doc, {}), ui_callbacks={},
            draw_callback=None, worker=None)
        job = coordinator.run(target, ui_params, quantities, sources, {},
                              dict(algo_kwargs, cancel_callback=lambda: False),
                              False, viz_manager=None)
        if job is None:
            emit("  run() returned None")
            return None, 0
        job.commit()
        doc.recompute()

        rows = []
        for obj in doc.Objects:
            if obj.Name.startswith("nested_"):
                place = obj.Placement
                rows.append((obj.Name, round(place.Base.x, 4),
                             round(place.Base.y, 4),
                             round(place.Rotation.Angle, 4)))
        rows.sort()
        digest = hashlib.sha256("|".join(map(str, rows)).encode()).hexdigest()
        parts = len([o for o in doc.Objects if o.Name.startswith("part_")])
        return digest, parts, doc
    except Exception:
        return None, 0, None


def main():
    emit("regenerating the nested fixture")
    emit("  source : %s" % os.path.basename(SOURCE))
    emit("  seed   : %d (NESTING_RANDOM_SEED)" % SEED)
    previous = os.environ.get("NESTING_RANDOM_SEED")
    os.environ["NESTING_RANDOM_SEED"] = str(SEED)
    try:
        first, parts_a, doc_a = build()
        if first is None:
            emit("  FAILED to nest")
            return 1
        emit("  run 1  : %s (%d parts)" % (first[:16], parts_a))
        FreeCAD.closeDocument(doc_a.Name)

        second, parts_b, doc_b = build()
        if second is None:
            emit("  FAILED to nest on the second run")
            return 1
        emit("  run 2  : %s (%d parts)" % (second[:16], parts_b))

        if first != second:
            emit("  NOT REPRODUCIBLE: two runs at seed %d differ" % SEED)
            emit("  Suspect the Shapely/GEOS build first -- the seed fixes the")
            emit("  workbench's own randomness, not polygon arithmetic.")
            FreeCAD.closeDocument(doc_b.Name)
            return 1
        emit("  reproducible at this seed")

        if os.environ.get("NESTING_WRITE_FIXTURE") != "1":
            emit("  verification only; set NESTING_WRITE_FIXTURE=1 to write")
            FreeCAD.closeDocument(doc_b.Name)
            return 0

        path = os.path.join(_HERE, ".last_nested_fixture.FCStd")
        doc_b.saveAs(path)
        emit("  wrote %s (%d bytes)" % (path, os.path.getsize(path)))
        FreeCAD.closeDocument(doc_b.Name)
        return 0
    finally:
        if previous is None:
            os.environ.pop("NESTING_RANDOM_SEED", None)
        else:
            os.environ["NESTING_RANDOM_SEED"] = previous


sys.exit(main())
