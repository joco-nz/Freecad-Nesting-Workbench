"""A layout records the settings that produced it, and reads them back unchanged.

NEST-001 and NEST-002 were both properties the reload path reads and nothing
writes, so a saved layout could not describe its own run. Neither raised, neither
warned, and no existing test failed:

  NEST-002  `PartRotationSteps` and `PartRotationOverride` -- the shape table's
            per-part "Rotations"/"Override" columns. The nest honoured the
            override (the *resolved* step count reached the nester), and a
            reopened layout silently reverted to the global value.
  NEST-001  `Algorithm` -- read at `nesting_controller.py:431`, never written,
            so a Physics layout reopened as Minkowski. And `NestingDirection`,
            written from the Minkowski dial whatever ran, so a Physics run
            recorded a direction it never used.

`tests/test_layout_persistence/` guards the *shape* of the pairing -- every name
read on reload is a name that gets written -- structurally and under plain
CPython. This file guards the *values*, on a real document, because the values
are the part that can be wrong while the pairing looks correct: an override of 8
and a global of 8 are the same number, so only the checkbox distinguishes them.

Two dials are used throughout, at deliberately different readings
(`MinkowskiDial=90`, `PhysicsDial=270`), because a single dial would let a
write that always reads the Minkowski one pass by coincidence. That is the same
trap as NEST-024's two nested copies, where the second was right by luck.

Run directly, or via tests/freecad_harness/run.sh. Writes
`.last_status_persist`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_persist")

import FreeCAD
import Part

from freecad.nestingworkbench.Tools.Nesting.layout_manager import LayoutManager
from freecad.nestingworkbench.Tools.Nesting.nesting_job import NestingJob

_failures = []
_checks = [0]

#: Dial readings, one per algorithm, chosen to differ. 90 is the panel's default
#: (Left) and 270 is its opposite, so a write that takes the Minkowski dial for a
#: Physics run produces 90 where 270 is required.
MINKOWSKI_DIAL = 90
PHYSICS_DIAL = 270

#: The global rotation step count, and the per-part override written for one part
#: only. `OVERRIDE_STEPS != GLOBAL_STEPS` so that writing the *resolved* value in
#: place of the raw one is detectable on the non-overridden part, and
#: `OVERRIDE_STEPS` also differs from the default the reload path assumes (4).
GLOBAL_STEPS = 6
OVERRIDE_STEPS = 12
NO_OVERRIDE_STEPS = 9

_UI = {
    "spacing": 5.0, "deflection": 0.05, "simplification": 0.1,
    "rotation_steps": GLOBAL_STEPS, "add_labels": False,
    "font_path": "", "verbose": False,
}

#: `rotation_steps` is the resolved value the nester uses; the other two are the
#: raw pair for the master container's metadata. `_collect_job_parameters` builds
#: exactly this shape.
_QUANTITIES = {
    "A": {"quantity": 2, "rotation_steps": OVERRIDE_STEPS,
          "rotation_steps_override": OVERRIDE_STEPS, "rotation_override": True,
          "up_direction": "Z+", "fill_sheet": False},
    "B": {"quantity": 1, "rotation_steps": GLOBAL_STEPS,
          "rotation_steps_override": NO_OVERRIDE_STEPS, "rotation_override": False,
          "up_direction": "Z+", "fill_sheet": False},
}


def _params(algorithm, nesting_direction, physics_direction,
            use_random_direction=False):
    """A params dict shaped like `_collect_ui_params`' output.

    Both direction keys are always present, and which one the layout must record
    is the point: before the fix `nesting_direction` was the only key, so there
    was nothing for the write to branch on.
    """
    return {
        "sheet_width": 450, "sheet_height": 350, "spacing": 5.0,
        "sheet_thickness": 3.0, "deflection_angle": 30, "simplification": 0.1,
        "font_path": "", "show_bounds": True, "add_labels": False,
        "label_height": 25.0, "label_size": 10.0, "rotation_steps": GLOBAL_STEPS,
        "generations": 1, "population_size": 1,
        "nesting_direction": nesting_direction,
        "physics_direction": physics_direction,
        "algorithm": algorithm,
        "use_random_direction": use_random_direction,
    }


def emit(message=""):
    """Print a report line in a way that survives FreeCAD's console redirect.

    Under freecadcmd, FreeCAD.Console captures plain print() once a document
    exists, so report output silently disappears.
    """
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


def check(condition, message):
    _checks[0] += 1
    if not condition:
        _failures.append(message)
        emit("  FAIL: %s" % message)
    return bool(condition)


def masters_by_label(shared):
    """`{part label: master container}` from a shared master group."""
    out = {}
    for master in getattr(shared, "Group", []) or []:
        label = master.Label
        if label.startswith("master_"):
            label = label[len("master_"):]
        out[label] = master
    return out


def run_one(doc_name, algorithm, use_random_direction=False):
    """Lay out, commit, and return `(target layout group, master containers)`."""
    doc = FreeCAD.newDocument(doc_name)

    sources = {}
    for name, (length, width) in (("A", (60, 40)), ("B", (30, 30))):
        obj = doc.addObject("Part::Feature", name)
        obj.Shape = Part.makeBox(length, width, 10)
        sources[name] = obj
    doc.recompute()

    target = doc.addObject("App::DocumentObjectGroup", "Layout_target")
    manager = LayoutManager(doc, {}, create_doc_objects=False)
    layout = manager.create_layout("Layout_temp", sources, _QUANTITIES, _UI)

    job = NestingJob.from_ga_result(
        doc=doc, target_layout=target,
        params=_params(algorithm, MINKOWSKI_DIAL, PHYSICS_DIAL,
                       use_random_direction),
        preparer=None, layout_group=layout.layout_group,
        parts_group=layout.parts_group, sheets=[],
        shared_master_group=manager.shared_master_group,
    )
    shared = manager.shared_master_group
    masters = masters_by_label(shared)
    job.commit()
    return doc, target, masters


# -- NEST-002: the per-part rotation override ------------------------------

def check_master_metadata(doc, target, masters):
    """Every master carries the raw override pair, correctly typed and valued."""
    emit("master container metadata:")
    for label in sorted(masters):
        master = masters[label]
        want = _QUANTITIES[label]
        has_steps = hasattr(master, "PartRotationSteps")
        has_override = hasattr(master, "PartRotationOverride")
        check(has_steps,
              "%s: no PartRotationSteps, so the per-part rotation reverts to the "
              "global value on reopen" % label)
        check(has_override,
              "%s: no PartRotationOverride, so the Override checkbox cannot come "
              "back" % label)
        if not (has_steps and has_override):
            continue

        steps = master.PartRotationSteps
        override = master.PartRotationOverride

        emit("  %-3s steps=%-4s override=%-6s (raw %s / %s)"
             % (label, steps, override,
                want["rotation_steps_override"], want["rotation_override"]))

        check(isinstance(steps, int),
              "%s: PartRotationSteps is a %s, expected an integer"
              % (label, type(steps).__name__))
        check(isinstance(override, bool),
              "%s: PartRotationOverride is a %s, expected a bool -- it used to "
              "default to a list on the reload side, which is falsy by accident "
              "rather than by being off" % (label, type(override).__name__))

        check(steps == want["rotation_steps_override"],
              "%s: PartRotationSteps is %s, expected the raw spinbox reading %s"
              % (label, steps, want["rotation_steps_override"]))
        check(override == want["rotation_override"],
              "%s: PartRotationOverride is %s, expected %s"
              % (label, override, want["rotation_override"]))

        # The value the nester consumes must be unaffected by the metadata write.
        # This is the invariant that matters most: NEST-002 broke the reopen, and
        # the obvious wrong fix -- persisting the resolved count -- would break
        # the run instead.
        resolved = _QUANTITIES[label]["rotation_steps"]
        check(steps != resolved or resolved == want["rotation_steps_override"],
              "%s: the stored raw value and the resolved value are "
              "indistinguishable here (%s), so this fixture can no longer tell "
              "them apart" % (label, resolved))

    # B is not overridden, so it must record "off" even though its spinbox holds
    # a number the user typed. That is the whole point of the boolean.
    if "B" in masters and hasattr(masters["B"], "PartRotationOverride"):
        check(masters["B"].PartRotationOverride is False,
              "B: an un-overridden part recorded the override as on")
        if "A" in masters and hasattr(masters["A"], "PartRotationOverride"):
            check(masters["A"].PartRotationOverride is True,
                  "A: an overridden part recorded the override as off")


# -- NEST-001: which algorithm, and which dial -----------------------------

def check_direction_recorded(doc, target, algorithm, expected_dial):
    """The layout records the algorithm and the direction *that algorithm* used."""
    emit("layout direction record (%s):" % algorithm)

    check(hasattr(target, "Algorithm"),
          "the layout has no Algorithm property, so reopening it falls back to "
          "Minkowski whatever ran -- and NestingDirection then cannot be "
          "attributed to a dial")
    if not hasattr(target, "Algorithm"):
        return
    check(target.Algorithm == algorithm,
          "the layout records Algorithm=%r, expected %r"
          % (target.Algorithm, algorithm))

    check(hasattr(target, "NestingDirection"),
          "the layout has no NestingDirection")
    if not hasattr(target, "NestingDirection"):
        return
    got = target.NestingDirection
    check(got == expected_dial,
          "the layout records NestingDirection=%s; a %s run whose dials sit at "
          "Minkowski=%d Physics=%d must record %d"
          % (got, algorithm, MINKOWSKI_DIAL, PHYSICS_DIAL, expected_dial))

    check(hasattr(target, "RandomDirection"),
          "the layout has no RandomDirection, so a random run reloads as if a "
          "dial had been used")
    if hasattr(target, "RandomDirection"):
        check(target.RandomDirection is False,
              "the layout records RandomDirection=%s, expected False"
              % target.RandomDirection)


def check_random_recorded(doc_name):
    """A random run records the flag, so the dial's greyed-out state comes back."""
    doc, target, masters = run_one(doc_name, "Physics", use_random_direction=True)
    try:
        emit("layout direction record (Physics, random):")
        if hasattr(target, "RandomDirection"):
            check(target.RandomDirection is True,
                  "a random run recorded RandomDirection=%s, expected True"
                  % target.RandomDirection)
        else:
            check(False,
                  "the layout has no RandomDirection, so a random run reloads "
                  "as a dial run")
        # The dial reading is still recorded even when it was not used -- it is
        # what the control held, which is what the user expects to see again.
        if hasattr(target, "NestingDirection"):
            check(target.NestingDirection == PHYSICS_DIAL,
                  "a random Physics run recorded NestingDirection=%s, expected "
                  "the Physics dial's %d" % (target.NestingDirection, PHYSICS_DIAL))
    finally:
        FreeCAD.closeDocument(doc.Name)


def check_round_trip(doc_name):
    """Save, close, reopen: the properties survive, which is the whole claim.

    Everything above inspects a document in the session that built it. A layout
    is a file, so the claim has to hold against one.
    """
    import tempfile

    doc, target, masters = run_one(doc_name, "Physics")
    tmp = os.path.join(tempfile.gettempdir(), "layout_persist_roundtrip.FCStd")
    if os.path.exists(tmp):
        os.remove(tmp)
    try:
        doc.saveAs(tmp)
        expected = {
            "Algorithm": getattr(target, "Algorithm", None),
            "NestingDirection": getattr(target, "NestingDirection", None),
            "RandomDirection": getattr(target, "RandomDirection", None),
            "GlobalRotationSteps": getattr(target, "GlobalRotationSteps", None),
            "steps": {l: (getattr(m, "PartRotationSteps", None),
                          getattr(m, "PartRotationOverride", None))
                      for l, m in masters.items()},
        }
        doc_name_saved = doc.Name
        FreeCAD.closeDocument(doc_name_saved)

        reopened = FreeCAD.openDocument(tmp)
        layout_group = next((o for o in reopened.Objects
                             if o.Label == "Layout_target"), None)
        check(layout_group is not None,
              "the reopened document has no Layout_target group")
        if layout_group is not None:
            for name in ("Algorithm", "NestingDirection", "RandomDirection",
                         "GlobalRotationSteps"):
                check(getattr(layout_group, name, None) == expected[name],
                      "%s did not survive the round trip: %s, expected %s"
                      % (name, getattr(layout_group, name, None), expected[name]))
            for label, want in expected["steps"].items():
                master = next((o for o in reopened.Objects
                               if o.Label == "master_%s" % label), None)
                if master is None:
                    check(False, "master_%s did not survive the round trip" % label)
                    continue
                got = (getattr(master, "PartRotationSteps", None),
                       getattr(master, "PartRotationOverride", None))
                check(got == want,
                      "master_%s did not survive the round trip: %s, expected %s"
                      % (label, got, want))
        FreeCAD.closeDocument(reopened.Name)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def run():
    # -- NEST-002, on a Minkowski run --
    doc, target, masters = run_one("persist_mink", "Minkowski")
    try:
        emit("-- master metadata, Minkowski run --")
        check_master_metadata(doc, target, masters)
        emit("")
        emit("-- layout direction record, Minkowski --")
        check_direction_recorded(doc, target, "Minkowski", MINKOWSKI_DIAL)
    finally:
        FreeCAD.closeDocument(doc.Name)

    # -- NEST-001, on a Physics run: the dials differ, so this is the arm that
    #    a write which always reads the Minkowski dial cannot pass. --
    doc, target, masters = run_one("persist_phys", "Physics")
    try:
        emit("")
        emit("-- layout direction record, Physics --")
        check_direction_recorded(doc, target, "Physics", PHYSICS_DIAL)
    finally:
        FreeCAD.closeDocument(doc.Name)

    check_random_recorded("persist_random")
    check_round_trip("persist_roundtrip")

    emit("")
    emit("a layout now records the algorithm that ran, the direction that "
         "algorithm's dial held, whether that direction was used at all, and "
         "each part's raw rotation override. The reload path reads all of them.")


if __name__ in ("__main__", "test_layout_persistence"):
    status = 0
    try:
        run()
    except Exception:
        traceback.print_exc()
        _failures.append("raised")
    emit("layout persistence: %d checks, %d failure(s)"
         % (_checks[0], len(_failures)))
    for failure in _failures:
        emit("  FAIL: %s" % failure)
    if _failures:
        status = 1
    try:
        with open(_STATUS_FILE, "w") as handle:
            handle.write(str(status))
    except OSError:
        pass
    emit("LAYOUT_PERSIST_STATUS=%d" % status)
    sys.exit(status)
