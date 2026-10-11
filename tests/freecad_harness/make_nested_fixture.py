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

## What verification checks, and why the second one was missing

Two questions, and the order matters:

1. **Does the committed file still match the constants below?** Read straight
   out of `Document.xml`; no FreeCAD, no nesting, under a second.
2. **Does a fresh run at this seed reproduce that file?** Two nests, compared,
   then compared against the committed digest.

The second one is what this script always did, and it was not enough on its
own: two agreeing runs prove the run is *self-consistent*, not that it is
*correct*. Held against stale constants that describes a completely different
layout and still reports "reproducible at this seed". So the digest of the
committed fixture is recorded in `EXPECTED_DIGEST` and compared directly.

The constants here drifted exactly that way once. The fixture was re-nested in
`c24417a` -- deliberately, to a layout the NEST-034 verify gate considers clean
-- with five settings changed and the seed taken from the run recorded in
`collision-defect-status.md`. This script kept describing the previous
configuration, and nothing noticed, because nothing compared the output to
anything.

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
# **This is the seed of the layout that is committed, and it was not chosen
# here.** It is the seed recorded against run G2 in `collision-defect-status.md`
# ("run F, sim 0.001, step 0.1, seed 3355455059 -- 54/54, MET, tightest
# 3.0000"), which is the run whose layout became this fixture in `c24417a`.
#
# The previous value, 1234, was correct for the fixture as it stood *before* the
# NEST-036 re-nest and described a configuration that no longer exists on disk.
# Every constant in this block was wrong by enough to matter, not by a rounding
# error, which is why `EXPECTED_DIGEST` below exists.
SEED = 3355455059

# The placement digest of the committed fixture, read out of
# `replay-fixture-CAM-Nested.FCStd` rather than copied from a log.
#
# **This is the check that was missing, and its absence is why the constants
# above went stale unnoticed.** Verification used to compare only run 1 with run
# 2 -- "did the same thing happen twice?" -- which is self-consistency, not
# correctness. Held against the pre-NEST-036 constants it would have reported
# "reproducible at this seed" and written a 48-part fixture over a 54-part one
# without a word, because nothing ever asked whether the output was the *right*
# output. The fixture is read by five gated suites; that failure would have been
# silent and would have arrived as a pile of re-baselined counts.
#
# So verification asks both questions now, and the second is the one that
# matters. Regenerating this fixture is a rare, deliberate act, so the extra
# cost is one string comparison.
#
# Recompute it after a deliberate re-nest with `fixture_digest(TARGET)` below,
# which reads the committed file directly and needs no FreeCAD and no nesting.
EXPECTED_DIGEST = (
    "d5986c5a5762b93e874fe57b8a164a518d63169543afbbfb58a3d3bd8ae78028")
EXPECTED_PARTS = 54

# Everything below is read off the committed layout rather than guessed -- with
# the emphasis on *off*, because they were all wrong once. The layout records
# its own sheet size, spacing, direction, simplification and deflection, so
# these can be checked against the file at any time.
SHEET_WIDTH = 600.0
SHEET_HEIGHT = 300.0
SHEET_THICKNESS = 2.0
SPACING = 3.0
DEFLECTION_ANGLE = 20.0
SIMPLIFICATION = 0.001
# The GA discretisation interval, in mm. Unset in `algo_kwargs` meant the 5.0 mm
# default, and the run that produced this fixture used 0.1 -- so this was
# silently five times coarser than intended. `_step_size` resolves env, then
# kwarg, then 5.0, so it has to be passed explicitly to be 0.1.
STEP_SIZE_MM = 0.1
GENERATIONS = 4
POPULATION_SIZE = 10
ROTATION_STEPS = 4
NESTING_DIRECTION = 45
QUANTITIES = {"BottomStrap": 26, "TopStrap": 26, "SimpleSpacer": 2}

SOURCE_LABELS = ("BottomStrap", "TopStrap", "SimpleSpacer")


def emit(message=""):
    FreeCAD.Console.PrintMessage(str(message) + "\n")


def fixture_digest(path=TARGET):
    """Placement digest of a saved fixture, read straight out of the FCStd.

    Deliberately FreeCAD-free and nesting-free: it unzips `Document.xml` and
    hashes the `nested_*` placements, so the committed fixture can be checked
    against `EXPECTED_DIGEST` in well under a second. That is the cheap half of
    verification, and the half that would have caught the stale constants on its
    own -- without it, every check costs a full GA run to reach a conclusion
    about a file that is sitting on disk.

    The row format matches `build()`'s exactly -- name, x, y, rotation angle,
    each rounded to 4 dp and joined with "|" -- because a digest is only
    comparable to another digest if the rows are built the same way.
    """
    import re
    import zipfile

    with zipfile.ZipFile(path) as archive:
        xml = archive.read("Document.xml").decode("utf-8", "replace")
    placement = re.compile(
        r'<PropertyPlacement Px="([-\d.]+)" Py="([-\d.]+)" Pz="[-\d.]+"'
        r' Q0="[-\d.eE+]+" Q1="[-\d.eE+]+" Q2="[-\d.eE+]+" Q3="[-\d.eE+]+"'
        r' A="([-\d.eE+]+)"')
    rows = []
    for name, body in re.findall(r'<Object name="([^"]+)"(.*?)</Object>', xml, re.S):
        if not name.startswith("nested_"):
            continue
        found = placement.search(body)
        if found:
            rows.append((name, round(float(found.group(1)), 4),
                         round(float(found.group(2)), 4),
                         round(float(found.group(3)), 4)))
    rows.sort()
    return hashlib.sha256("|".join(map(str, rows)).encode()).hexdigest(), len(rows)


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
            # Explicit, and it was missing. See STEP_SIZE_MM.
            "step_size": STEP_SIZE_MM,
            # Also missing, and it decides *which* layout is committed. The run
            # this fixture came from was recorded as "target 1 sheet" (see the
            # measured params in `collision-defect-status.md`), which stops the
            # GA as soon as one sheet is achieved. Left at the default of 0 the
            # GA runs all 4 generations x 10 population and commits the best of
            # forty layouts -- measured 731 s against the recorded 129 s, and a
            # different set of placements, because the best of forty is not
            # necessarily the first.
            "target_sheets": 1,
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
    emit("verifying the nested fixture")
    emit("  source : %s" % os.path.basename(SOURCE))
    emit("  seed   : %d (NESTING_RANDOM_SEED)" % SEED)

    # The cheap half, and the one that matters most: is the file on disk still
    # the layout these constants describe? Costs nothing -- no FreeCAD, no
    # nesting -- and it is the check whose absence let this drift.
    if os.path.exists(TARGET):
        try:
            have, have_parts = fixture_digest(TARGET)
        except Exception as error:
            emit("  could not read %s: %s" % (os.path.basename(TARGET), error))
            return 1
        if have != EXPECTED_DIGEST:
            emit("  COMMITTED FIXTURE DOES NOT MATCH THE CONSTANTS BELOW")
            emit("    committed : %s (%d parts)" % (have, have_parts))
            emit("    expected  : %s (%d parts)" % (EXPECTED_DIGEST, EXPECTED_PARTS))
            emit("  The fixture and this script disagree. Re-read the constants")
            emit("  from the committed layout before regenerating anything.")
            return 1
        emit("  committed fixture matches EXPECTED_DIGEST (%d parts)"
             % have_parts)
    else:
        emit("  no committed fixture to check against")
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
        emit("  reproducible at this seed (two runs agree)")

        # The expensive half, and the one that answers "can this script still
        # produce the fixture that is committed?" -- as opposed to the cheap
        # half above, which only asks whether the file still matches a constant.
        if first != EXPECTED_DIGEST:
            emit("  REGENERATION WOULD CHANGE THE FIXTURE")
            emit("    regenerated : %s (%d parts)" % (first, parts_a))
            emit("    committed   : %s (%d parts)" % (EXPECTED_DIGEST, EXPECTED_PARTS))
            emit("  Either a constant has drifted again, or this FreeCAD/Shapely")
            emit("  build nests differently -- the seed pins the workbench's own")
            emit("  randomness, not GEOS. Do not write the fixture on this run.")
            FreeCAD.closeDocument(doc_b.Name)
            return 1
        emit("  and it reproduces the committed fixture exactly")

        if os.environ.get("NESTING_WRITE_FIXTURE") != "1":
            emit("  verification only; set NESTING_WRITE_FIXTURE=1 to write")
            FreeCAD.closeDocument(doc_b.Name)
            return 0

        # Both checks have passed by this point, so what is written is the
        # committed fixture, not merely a self-consistent one.
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
