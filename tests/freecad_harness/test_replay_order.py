"""Freecadcmd check that the replayed job's operation ORDER is right.

This tier exists because of a specific hole in the gate. Both other replay
harnesses build their own geometry, and neither builds hole nesting, so
`order_operations` returned early before its write in every gated run. The one
fixture that does nest parts -- `replay-fixture-CAM-Nested.FCStd`, 15 nestings
-- was only ever read by `validate_replay_fixture.py`, which is a diagnostic and
is not gated.

So the write was only ever reached by a fixture nothing asserted against, and
what it wrote was wrong: `order_operations` is handed the base operations and
was writing them straight into `Operations.Group`, which must hold the
outermost dressup. 98 entries, of which 0 were dressups, with all 105 dressups
built, linked and unlisted (NEST-015). Every gated check passed, because every
gated check was looking at the operations that *were* listed.

This runs the committed fixture end to end and asserts what the order must be:

  * **the list holds entries, not bare operations.** The invariant the replay's
    own docstring states three times.
  * **every step maps to exactly one nested part.** One operation per
    (process step, target part), so a step's copies are the split, not a repeat.
  * **a part's steps stay in the order the user wrote them.** Now a hard
    constraint, not a preference: with it left as a tie-break, both spacers came
    out boundary-then-holes (NEST-016).
  * **hole nesting holds.** Every hole-cutting step on an outer part is behind
    every step of the part nested in its hole.
  * **parts are contiguous.** One part finished before another is started, so
    the torch is not making round trips.

Every one of these is checked against the real FreeCAM modules and real
toolpaths, not stand-ins. Run via tests/freecad_harness/run.sh. Writes
`.last_status_order`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_order")

FIXTURE = os.path.join(_REPO, "tests", "Test_Files",
                       "replay-fixture-CAM-Nested.FCStd")

import FreeCAD

from freecad.nestingworkbench.Tools.Cam import cam_replay

_failures = []
_checks = [0]


def emit(message=""):
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
        else:
            print(message)
    except Exception:
        pass


def check(condition, message):
    _checks[0] += 1
    if not condition:
        _failures.append(message)
        emit("  FAIL: %s" % message)
    return bool(condition)


def check_equal(actual, expected, message):
    return check(actual == expected,
                 "%s (got %r, expected %r)" % (message, actual, expected))


# -- the order, from the replayed job ------------------------------------

def part_of_base_op(op):
    """The nested part a base operation cuts, or None."""
    base = getattr(op, "Base", None)
    if not base or not isinstance(base, (list, tuple)):
        return None
    try:
        return base[0][0]
    except (TypeError, IndexError):
        return None


def entry_checks(outcome, result, job):
    """Every invariant of the Operations list, on a real replay."""
    emit("")
    emit("-- the Operations list --")
    group = list(job.Operations.Group)
    check_equal(len(group), len(result.operations),
                "one list entry per replayed step")

    # `entry_of` is keyed by id(base operation), so the keys are ints. Reading
    # these keys as objects silently yields zero parts and turns every check
    # below into a vacuous pass, which is how the first version of this file
    # reported a clean run.
    check_equal(len(result.entry_of), len(result.operations),
                "every replayed step has an outermost entry")

    # Labels, not reprs. Every object here prints as
    # `<Path::FeaturePython object>`, so reporting the objects dumps 98
    # identical lines and names none of the operations at fault.
    bare = [getattr(o, "Label", o) for o in group
            if not cam_replay.is_dressup(o)]
    check_equal(bare[:5], [],
                "the list must hold outermost dressups, not bare operations "
                "(%d bare of %d, first: %s)"
                % (len(bare), len(group), ", ".join(map(str, bare[:5]))))

    # Part, indexed by the *entry* that stands for the step, since the list is
    # what this test is about.
    part_of_entry = {}
    op_of_entry = {}
    for op in result.operations:
        part = part_of_base_op(op)
        check(part is not None,
              "%s does not name the nested part it cuts" % op.Label)
        entry = result.entry_of.get(id(op))
        check(entry is not None,
              "%s has no outermost entry" % op.Label)
        if part is not None and entry is not None:
            part_of_entry[id(entry)] = part
            op_of_entry[id(entry)] = op

    for entry in group:
        op = op_of_entry.get(id(entry))
        if op is None:
            continue
        geometry = getattr(op, "Base", None) or []
        ids = {id(pair[0]) for pair in geometry
               if isinstance(pair, (list, tuple))}
        check_equal(len(ids), 1,
                    "%s cuts %d parts, expected exactly 1"
                    % (entry.Label, len(ids)))

    parts = [part_of_entry.get(id(entry)) for entry in group]
    check(all(part is not None for part in parts),
          "every list entry resolved to a nested part")
    if not all(part is not None for part in parts):
        return None, None

    emit("  %d entries over %d nested parts"
         % (len(group), len(set(parts))))
    return result, parts


def order_checks(outcome, result, group, parts):
    """The ordering itself: within-part source order, nesting, contiguity."""
    base_of_entry = {id(entry): key for key, entry in result.entry_of.items()}
    part_of_base = {id(op): part_of_base_op(op) for op in result.operations}
    ops_by_key = {id(op): op for op in result.operations}
    source_index = {id(op): i for i, op in enumerate(result.operations)}
    final_keys = [base_of_entry.get(id(e)) for e in group]

    # -- within-part source order --
    emit("")
    emit("-- within-part source order --")
    per_part = {}
    for i, key in enumerate(final_keys):
        part = part_of_base.get(key)
        if part is not None:
            per_part.setdefault(part, []).append(source_index[key])
    broken = {p: seq for p, seq in per_part.items() if seq != sorted(seq)}
    check_equal(len(broken), 0,
                "parts whose steps moved relative to each other: %s"
                % sorted(p.Label for p in broken))
    for part in sorted(broken, key=lambda p: p.Label)[:3]:
        emit("    %s: source indices %s" % (part.Label, per_part[part]))

    # -- hole nesting --
    emit("")
    emit("-- hole nesting --")
    footprints = cam_replay.FootprintCache()
    nestings = cam_replay.find_hole_nestings(outcome.replay_job.clones,
                                             footprints)
    at = {key: i for i, key in enumerate(final_keys)}
    applicable = 0
    for outer, inner in nestings:
        outer_keys = [k for k in final_keys
                      if part_of_base.get(k) is outer
                      and cam_replay.operation_touches_hole(ops_by_key[k],
                                                            outer, footprints)]
        inner_keys = [k for k in final_keys
                      if part_of_base.get(k) is inner]
        if not outer_keys or not inner_keys:
            continue
        applicable += 1
        check(min(at[k] for k in outer_keys) >= max(at[k] for k in inner_keys),
              "hole-cutting steps on %s are not all behind %s, which is nested "
              "in its hole" % (outer.Label, inner.Label))
    check(applicable > 0,
          "no nesting was applicable, so the ordering rules went unchecked")
    emit("  %d nesting(s) found, %d applicable to this step split"
         % (len(nestings), applicable))

    # -- contiguity --
    emit("")
    emit("-- contiguity --")
    spans = {}
    for i, part in enumerate(parts):
        if part not in spans:
            spans[part] = []
        if spans[part] and spans[part][-1][1] == i - 1:
            spans[part][-1] = (spans[part][-1][0], i)
        else:
            spans[part].append((i, i))
    split = {p: v for p, v in spans.items() if len(v) > 1}
    total = sum(len(v) for v in spans.values())
    emit("  %d part(s) over %d stretch(es), %d part(s) split"
         % (len(spans), total, len(split)))
    for part in sorted(split, key=lambda p: p.Label)[:3]:
        emit("    %s: %s" % (part.Label,
                              ", ".join("%d-%d" % s for s in split[part])))
    check(total == len(spans),
          "%d part(s) are split across %d stretches, expected each part to be "
          "cut in one run" % (len(split), total - len(spans)))


def travel_report(outcome, result, group):
    """How far the torch travels, in the order chosen and in the recipe order.

    Reported rather than asserted. The fixture's tool controller has
    `HorizRapid = 0.0 mm/s` -- a plasma torch has no traverse feed rate -- so
    machine time cannot be derived from the tool table and distances are the
    honest measure.
    """
    import math

    def traverse(entries):
        rapid = cut = 0.0
        x = y = z = None
        for entry in entries:
            path = getattr(entry, "Path", None)
            for command in (path.Commands if path else []) or []:
                cx = getattr(command, "X", None)
                cy = getattr(command, "Y", None)
                cz = getattr(command, "Z", None)
                cx = float(cx) if cx is not None else x
                cy = float(cy) if cy is not None else y
                cz = float(cz) if cz is not None else z
                if x is not None and (cx, cy, cz) != (x, y, z):
                    step = math.sqrt((cx - x) ** 2 + (cy - y) ** 2
                                     + (cz - z) ** 2)
                    if cam_replay.is_cutting_motion(
                            cam_replay.motion_of(command)):
                        cut += step
                    else:
                        rapid += step
                x, y, z = cx, cy, cz
        return rapid, cut

    recipe = [result.entry_of.get(id(o), o) for o in result.operations]
    ordered = list(outcome.replay_job.job.Operations.Group)
    if recipe == ordered:
        emit("  the recipe order survived; nothing to compare against")
        return
    recipe_rapid, recipe_cut = traverse(recipe)
    final_rapid, final_cut = traverse(ordered)
    emit("  recipe order: rapid %8.1f mm, cutting %8.1f mm"
         % (recipe_rapid, recipe_cut))
    emit("  final order:  rapid %8.1f mm, cutting %8.1f mm"
         % (final_rapid, final_cut))
    emit("  change:       rapid %+8.1f mm, cutting %+8.1f mm"
         % (final_rapid - recipe_rapid, final_cut - recipe_cut))
    check(abs(final_cut - recipe_cut) < 1.0,
          "ordering changed the cutting distance by %.1f mm; it must not cut "
          "any more or less" % (final_cut - recipe_cut))


def tool_table_checks():
    """The job must offer only the tool its operations cut with.

    `PathJob.Create` puts a `TC: 5mm Endmill` in every new job. The replay adds
    the user's own controller beside it, and the job's SetupSheet resolves the
    active tool from the controllers present -- so while both are there it
    prompts, once per operation created. The user sees it as "still there in
    the new Job".

    The check is deliberately about *when* the default goes, not only that it
    goes: pruning after the operations exist is too late to stop the prompt.
    """
    emit("")
    emit("-- tool table --")
    doc = FreeCAD.openDocument(FIXTURE)
    try:
        layout, _ = cam_replay.resolve_layout_group(doc)
        sources = [o for o in doc.Objects if cam_replay.is_cam_job(o)]
        if layout is None or not sources:
            check(False, "the fixture did not resolve, so the tool table went "
                  "unchecked")
            return

        # Watch the tool table from the first progress event of the replay, so
        # "before any operation was created" is observed rather than assumed.
        seen = []
        originals = {}

        def watch(stage, current, total, message=None):
            if stage != "Replaying the recipe":
                return
            if "job" not in originals:
                # The replay built the job in the previous stage, so it exists
                # by the time this stage starts. Looked up once rather than
                # held from before the run, because there is nothing to hold
                # then.
                for candidate in doc.Objects:
                    if getattr(candidate, "Label", "").startswith("Job_Replay_") \
                            and hasattr(candidate, "Tools"):
                        originals["job"] = candidate
                        break
            job = originals.get("job")
            if job is None:
                return
            tools = list(getattr(job.Tools, "Group", []) or [])
            seen.append((current, len(tools),
                         [getattr(t, "Label", "?") for t in tools]))

        source_job = sources[0]
        outcomes = cam_replay.replay_layout(doc, layout, source_job,
                                            progress_callback=watch)
        if not outcomes or outcomes[0].replay_job is None:
            check(False, "no replay job was produced")
            return
        job = outcomes[0].replay_job.job

        tools = list(getattr(job.Tools, "Group", []) or [])
        labels = [getattr(t, "Label", "?") for t in tools]
        emit("  tool table after the replay: %s" % (labels or "(empty)",))
        check_equal(len(tools), 1,
                    "expected exactly one tool controller, got %d: %s"
                    % (len(tools), labels))
        check(not any("5mm" in label for label in labels),
              "the default '5mm Endmill' controller is still in the tool table: "
              "%s" % labels)

        # Every operation points at a controller that exists.
        for entry in job.Operations.Group:
            bottom = entry
            for _ in range(20):
                if not cam_replay.is_dressup(bottom):
                    break
                base = getattr(bottom, "Base", None)
                if base is None:
                    break
                bottom = base
            controller = getattr(bottom, "ToolController", None)
            check(controller is not None and controller in tools,
                  "%s cuts with %r, which is not in the tool table %s"
                  % (entry.Label, getattr(controller, "Label", None), labels))

        # The property that matters: never an operation in existence while two
        # controllers are in the table.
        #
        # Checking the *first* event proves nothing -- at that point the replay
        # has not copied the user's controller in yet, so the table holds only
        # the default and the count is 1 by accident. The default is dropped
        # after `copy_tool_controller` runs and before the first operation is
        # created, so the table is briefly 2 controllers with nothing built,
        # which is harmless, and 1 from the first operation onward, which is
        # the point.
        with_operations = [row for row in seen if (row[0] or 0) >= 1]
        check(with_operations,
              "no replay event was seen with an operation built, so the tool "
              "table's timing went unchecked")
        for current, count, labels in with_operations:
            if count != 1:
                check(False,
                      "with %d operation(s) built the job held %d tool "
                      "controllers %s; the SetupSheet prompts on that"
                      % (current, count, labels))
                break
        else:
            ok_seen = len(with_operations)
            emit("  %d replay event(s) with an operation built, all with "
                 "exactly 1 tool controller" % ok_seen)
        if with_operations:
            emit("  tool table at the first built operation: %s"
                 % (with_operations[0][2] or "(none)",))
    finally:
        FreeCAD.closeDocument(doc.Name)


def cancel_checks():
    """A cancelled replay must stop, keep what it built, and say so.

    Cancelling is an engine feature, not a widget feature. The engine had no
    notion of being stopped at all until this, so the Cancel button on the Tasks
    panel would have been a control that did nothing.

    The rule being pinned: a cancel **keeps** the job. Deleting it would be the
    wrong instinct -- an operator who cancels wants to see how far it got, and
    that is also what makes a cancel debuggable. The job goes into the document
    labelled `_UNVERIFIED` and nothing may be posted from it.
    """
    emit("")
    emit("-- cancelling --")
    doc = FreeCAD.openDocument(FIXTURE)
    try:
        layout, _ = cam_replay.resolve_layout_group(doc)
        sources = [o for o in doc.Objects if cam_replay.is_cam_job(o)]
        if layout is None or not sources:
            check(False, "the fixture did not resolve, so cancel went unchecked")
            return

        # Cancel a few operations into "Replaying the recipe" -- the one stage
        # with 98 countable steps in it, and so the only place a cancel can be
        # observed to stop partway through real work rather than between two
        # indivisible calls.
        #
        # Driven off the progress callback rather than off a call counter. A
        # counter reached the threshold during "Flattening nested parts", which
        # emits 50 events before any operation exists, so the run was cancelled
        # before it started and there was nothing to keep. What a Cancel button
        # does is set a flag while the work is visible, and this is that.
        state = {"cancel": False, "built": 0}

        def watch(stage, current, total, message=None):
            if stage == "Replaying the recipe" and current:
                state["built"] = current
                if current >= 5:
                    state["cancel"] = True

        outcomes = cam_replay.replay_layout(doc, layout, sources[0],
                                            progress_callback=watch,
                                            cancel_check=lambda: state["cancel"])
        check_equal(len(outcomes), 1, "expected one sheet, got %d" % len(outcomes))
        if not outcomes:
            return
        outcome = outcomes[0]

        check(outcome.ok is False,
              "a cancelled sheet reported ok -- a half-built job that verifies "
              "cleanly is the failure this exists to prevent")
        check(any("ancel" in e for e in outcome.errors),
              "the cancel was not reported: %s" % (outcome.errors,))

        job = outcome.replay_job
        check(job is not None,
              "a cancelled replay left no job, so there is nothing to inspect")
        if job is None:
            return
        check(job.job.Label.endswith(cam_replay.UNVERIFIED_SUFFIX),
              "a cancelled job is not labelled %s, so it could be posted from "
              "by accident: %s" % (cam_replay.UNVERIFIED_SUFFIX, job.job.Label))
        built = len(list(job.job.Operations.Group))
        check(built > 0,
              "a cancelled replay kept no operations at all; the operator is "
              "left with an empty job and no way to see where it stopped")
        check(built < len(outcome.result.operations),
              "a cancelled replay dressed every operation (%d of %d), so the "
              "cancel did not actually stop anything"
              % (built, len(outcome.result.operations)))

        # The NEST-015 invariant, on the cancel path.
        #
        # Pass one builds every base operation before pass two dresses them, so
        # a cancelled replay has a complete `operations` list and a partial
        # Operations list. Ordering reads that mismatch as "repopulate", writes
        # the bare operations over the entries, and orphans every dressup. That
        # is what happened: cancelled 5 operations in, the list came back
        # holding 98 bare operations.
        bare = [o.Label for o in job.job.Operations.Group
                if not cam_replay.is_dressup(o)]
        check_equal(bare[:3], [],
                    "the cancelled job's list holds bare operations, not "
                    "entries: %d of %d, first %s"
                    % (len(bare), built, ", ".join(map(str, bare[:3]))))
        emit("  cancelled once %d operation(s) had been built; kept %d, all "
             "entries, labelled %s"
             % (state["built"], built, job.job.Label))
        check(state["built"] >= 5,
              "the cancel never fired during the replay stage; built reached "
              "only %d" % state["built"])
    finally:
        FreeCAD.closeDocument(doc.Name)


def run():
    if not os.path.exists(FIXTURE):
        emit("no fixture at %s -- cannot check the order" % FIXTURE)
        _failures.append("fixture missing")
        return

    emit("replay order: committed fixture, end to end")
    doc = FreeCAD.openDocument(FIXTURE)
    try:
        layout, _warnings = cam_replay.resolve_layout_group(doc)
        check(layout is not None, "the fixture's layout was not resolved")
        if layout is None:
            return
        sources = [o for o in doc.Objects if cam_replay.is_cam_job(o)]
        check_equal(len(sources), 1,
                    "expected one CAM job in the fixture, found %d"
                    % len(sources))
        if not sources:
            return

        outcomes = cam_replay.replay_layout(doc, layout, sources[0])
        check(outcomes, "no sheet was replayed")
        if not outcomes:
            return
        outcome = outcomes[0]
        check(outcome.ok is True, "the sheet failed: %s" % (outcome.errors,))
        check(outcome.result is not None,
              "no replay result; errors were %s" % (outcome.errors,))
        if outcome.result is None or outcome.replay_job is None:
            return

        result, job = outcome.result, outcome.replay_job.job
        emit(cam_replay.describe_sheet_outcome(outcome)[0])

        _, parts = entry_checks(outcome, result, job)
        if parts is None:
            return
        order_checks(outcome, result, list(job.Operations.Group), parts)
        travel_report(outcome, result, job)
    finally:
        FreeCAD.closeDocument(doc.Name)

    tool_table_checks()
    cancel_checks()


if __name__ in ("__main__", "test_replay_order"):
    status = 0
    try:
        run()
    except Exception:
        traceback.print_exc()
        _failures.append("raised")
    emit("\nreplay order: %d checks, %d failure(s)" % (_checks[0],
                                                       len(_failures)))
    for failure in _failures:
        emit("  FAIL: %s" % failure)
    if _failures:
        status = 1
    try:
        with open(_STATUS_FILE, "w") as handle:
            handle.write(str(status))
    except OSError:
        pass
    emit("REPLAY_ORDER_STATUS=%d" % status)
