"""Freecadcmd check that the lead-in audit works on a real replayed job.

The pytest tier covers the geometry reading against stand-in commands. That is
enough for the logic and not enough for the thing that matters: whether FreeCAD's
real Profile and real LeadInOut modules produce toolpaths whose lead-ins this
code can find, tell apart from the cut, and move.

What this is guarding
---------------------

  * **the whole path, on a job built by the replay.** 110 operations over 54
    parts, reconciled one by one. A reader that works on a synthetic square and
    fails on a real profile is not a reader.

  * **the clashes are found, at their measured distances.** The fixture's worst
    offender crosses its neighbour outright at 0.0000 mm, and the nearest near
    miss is 0.0437 mm. Both figures are pinned, because two different things go
    wrong quietly here: geometry read as chords instead of sampled arcs, and a
    lead-in reporting the cut it is attached to as a conflict. The first loses
    conflicts -- the largest arc sag on this fixture is 14.5239 mm, well over any
    tool radius. The second invents them.

  * **the geometry is the cut and nothing else.** The body's own wire link is
    not drawn, and the attachment where a lead-in meets its contour is not
    reported as a crossing. Both defects were found in this file's own
    measurements, and both produced findings invisible in the GUI.

  * **the sheet edge is checked, and says so when it cannot be.** A pierce point
    off the stock is a different failure from a clash, and a job whose stock
    cannot be read must not report a clean sheet that silently omits the edge.

  * **a fix is a real fix.** After the search, a fresh scan of the same job must
    find nothing. Anything less and the search accepted a candidate it never
    verified.

  * **lead-out is checked too, and checked together with lead-in.** The fixture
    has `LeadOut` true on only two dressups, so this also turns it on for a real
    two-ended operation and asserts that moving the start point moves both ends
    and that the audit judges both. A search that tested only the lead-in would
    fix the reported conflict by creating an unreported one.

Run directly, or via tests/freecad_harness/run.sh. Writes
`.last_status_leadin`; 0 pass, 1 fail.
"""
import math
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_STATUS_FILE = os.path.join(_HERE, ".last_status_leadin")

FIXTURE = os.path.join(_REPO, "tests", "Test_Files",
                       "replay-fixture-CAM-Nested.FCStd")

import FreeCAD

from freecad.nestingworkbench.Tools.Cam import cam_replay, lead_in_audit
from freecad.nestingworkbench.Tools.Cam import lead_in_progress
from freecad.nestingworkbench.Tools.Cam.lead_in_audit import (
    cut_moves,
    lead_runs,
    moves_to_line,
)

_failures = []
_checks = [0]

#: The worst clash on the committed fixture, and what it measured against.
#:
#: These are **not** looked up by name when a check needs the offender --
#: `_worst` finds it from the scan by what it is. The constants below are the
#: *measurements* the scan is pinned to, and `_nearest_marginal` re-derives
#: which operation they belong to. FreeCAD appends a counter to a duplicate
#: label on every replay, so a name written here is stale by the third replay;
#: a measured distance is not.
#:
#: The tightest clash is an exact crossing at 0.0000 mm, so it cannot be used to
#: tell a sampled arc from a chorded one -- both give zero. `MARGINAL_DISTANCE_MM`
#: is the nearest *near* miss, and it is that figure the distance check pins.
VICTIM = "DressupLeadInOut001_replay_nested_BottomStrap_6"
VICTIM_PART = "part_BottomStrap_6"
NEIGHBOUR_PART = "part_TopStrap_1"

#: Distance of the tightest exact crossing. Exactly 0 by definition; asserted so
#: that a geometry regression which turns a crossing into a near miss is caught.
CROSSING_DISTANCE_MM = 0.0

#: The nearest near miss, at the default tool-radius margin. Measured on the
#: current (re-nested) fixture: the nearest near miss is a lead-in on
#: BottomStrap_23 at 0.0277 mm from a neighbour's cut path. (The pre-re-nest
#: fixture measured 0.0437 mm; these are layout measurements, so they move when
#: the sheet is re-nested and must be re-measured then -- see `probe_pins`.)
MARGINAL_DISTANCE_MM = 0.0277

#: Tolerances. The distance is checked tightly because it is a known number; the
#: counts are checked loosely because they are structural.
DISTANCE_TOL_MM = 0.0005


def emit(message=""):
    try:
        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
        else:
            print(message)
    except Exception:
        print(message)


def check(condition, message, detail=None):
    _checks[0] += 1
    if condition:
        emit("  [ ok ] %s" % message)
        return True
    emit("  [FAIL] %s" % message)
    if detail:
        for line in str(detail).split("\n"):
            emit("         %s" % line)
    _failures.append(message)
    return False


def replay(doc):
    """Run a replay and return its job."""
    layout, _warnings = cam_replay.resolve_layout_group(doc)
    outcomes = cam_replay.replay_layout(doc, layout, doc.getObject("Job"))
    for outcome in outcomes:
        if outcome.replay_job is not None:
            return outcome.replay_job.job
    return None


# -- the read half on a real job ------------------------------------------

def check_scan(doc, job):
    emit("")
    emit("== the scan reads every operation on a real replay ==")
    result = lead_in_audit.scan_job(job)

    check(len(result.audits) == 110,
          "all 110 LeadInOut operations were examined (got %d)"
          % len(result.audits))
    check(not result.unreadable,
          "none were unreadable (%d were: %s)"
          % (len(result.unreadable),
             "; ".join("%s: %s" % (d.Label, why)
                       for d, why in result.unreadable[:3])))

    runs = sum(len(a.lead_runs) for a in result.audits)
    # 142 lead-ins and 28 lead-outs on this fixture, one per operation at least.
    check(runs == 170,
          "every lead run was found (got %d across 110 operations: 142 "
          "lead-ins and 28 lead-outs)" % runs)
    lead_outs = sum(1 for a in result.audits for r in a.lead_runs
                    if r.kind == "lead-out")
    check(lead_outs == 28,
          "the lead-outs were found too (%d of them -- the fixture has "
          "LeadOut true on two dressups)" % lead_outs)

    # Multi-wire operations exist on this fixture, and one lead-in per wire is
    # the case a single-lead-in reader gets wrong.
    multi = [a for a in result.audits if len(a.lead_runs) > 1]
    check(bool(multi),
          "at least one operation profiles several wires and has one lead-in "
          "per wire (%d such operations)" % len(multi))

    for audit in multi[:3]:
        for run in audit.lead_runs:
            expected = float(getattr(audit.dressup, "RadiusIn", 0) or 0)
            if run.kind != "lead-in" or expected <= 0:
                continue
            check(abs(run.length - expected) < 0.01,
                  "%s: lead-in on wire %d is RadiusIn long (%.4f vs %.4f)"
                  % (audit.label, run.wire, run.length, expected))
            break

    emit("  scan took %.3f s" % result.seconds)
    return result


def _find(audits, label):
    """The audit for `label`, matching on `Name` as well as `Label`.

    Matching on the label alone is not enough here, because a fresh replay of
    the same fixture produces a *different* label: `split_label_suffix` includes
    the nested part's own label, and FreeCAD's uniqueness rule on duplicates
    appends a counter -- `nested_BottomStrap_23` becomes
    `nested_BottomStrap_023` when the sheet already holds a `_23`.
    """
    for audit in audits:
        if audit.label == label or audit.name == label:
            return audit
    return None


def _worst(result):
    """The audit holding the closest conflict, whatever it is called.

    **Not looked up by name.** This file replays the fixture repeatedly, and
    each replay consumes more of FreeCAD's object-name counter -- by the tenth,
    the operation that was `DressupLeadInOut001_replay_nested_TopStrap_14` on
    the first replay comes back as `..._TopStrap_162`. Every check here that
    wants "the offender" therefore finds it from the scan by what it *is*
    rather than by what it is called. The alternative -- looking it up by a
    fixed name -- broke as soon as this file grew a few more replays.
    """
    best = None
    for audit in result.audits:
        worst = audit.worst
        if worst is not None and (best is None or worst.distance < best[1]):
            best = (audit, worst.distance)
    return None if best is None else best[0]


def _candidates_tried(detail):
    """The `N` in "... found after N candidates", or None.

    Parsed with a pattern rather than by scraping digits: the detail also
    carries a coordinate pair, so "the digits in this string" is
    "59.80822.4942" and raises. The first version of this did that.
    """
    import re
    match = re.search(r"after (\d+) candidate", detail)
    return int(match.group(1)) if match else None


def check_known_conflict(result):
    emit("")
    emit("== the known conflict is found at its measured distance ==")
    audit = _worst(result)
    if not check(audit is not None,
                 "an operation has a conflict",
                 "no audit in %d carries one" % len(result.audits)):
        return None

    check(audit.status == lead_in_audit.OperationAudit.UNRESOLVED,
          "it is reported as unresolved rather than clean")
    check(audit.part is not None and audit.part,
          "it is attributed to a named part (%s)" % audit.part)

    worst = audit.worst
    if not check(worst is not None, "it has a conflict"):
        return audit
    # The worst clash is an exact crossing. Pinning it to 0.0000 is a real check
    # -- a regression that turned a crossing into a 0.2 mm near miss would fail
    # it -- but it cannot detect chorded arcs, so it is paired with the marginal
    # distance below.
    check(abs(worst.distance - CROSSING_DISTANCE_MM) < DISTANCE_TOL_MM,
          "the closest conflict is an exact crossing at %.4f mm (expected "
          "%.4f +/- %.4f)"
          % (worst.distance, CROSSING_DISTANCE_MM, DISTANCE_TOL_MM),
          "The fixture's worst offender crosses outright. If this is now a near "
          "miss, the geometry is being read wrong.")
    check(worst.other_part is not None and worst.other_part,
          "and names the part in the way (%s)" % worst.other_part)
    # Attributed to a lead *run*, not to a crossing of the body's own cut: on
    # the current (re-nested) fixture the worst offender is a lead-out (it was a
    # lead-in before the re-nest), so the check is "a lead run" rather than a
    # fixed kind. Which end is worst is a layout fact and moves when the sheet is
    # re-nested; what must hold is that the reader attributes the clash to the
    # lead geometry rather than to the cut path it is attached to.
    check(worst.run.kind in ("lead-in", "lead-out"),
          "and it is a lead run (%s), not the cut path" % worst.run.kind)

    # The distance that actually proves arcs are sampled and not chorded: the
    # nearest *near* miss. Chording a `G3` pulls the cut path up to 14.5239 mm
    # inside the material on this fixture, so a chorded audit reports clear
    # where a sampled one finds a clash.
    marginal = _nearest_marginal(result)
    if check(marginal is not None, "there is a near miss as well as a crossing"):
        distance, owner = marginal
        check(abs(distance - MARGINAL_DISTANCE_MM) < DISTANCE_TOL_MM,
              "the nearest near miss measures %.4f mm, as measured "
              "independently (expected %.4f +/- %.4f, on %s)"
              % (distance, MARGINAL_DISTANCE_MM, DISTANCE_TOL_MM, owner.label),
              "A different figure means arcs are being chorded rather than "
              "sampled. Measured on this fixture, the largest arc sag is "
              "14.5239 mm: a 52.3926 mm sampled path against a 37.8687 mm "
              "chord. Chording never invents a conflict, it only loses them.")

    emit("  offender: %s (%s) at %.4f mm from %s"
         % (audit.label, audit.part, worst.distance, worst.other_part))
    return audit


def _nearest_marginal(result):
    """`(distance, audit)` for the closest conflict that is not a crossing.

    Crossings all measure 0.0 and so cannot distinguish a sampled arc from a
    chorded one. This is the figure that does.
    """
    best = None
    for audit in result.audits:
        for conflict in audit.conflicts:
            if conflict.distance <= 0.0:
                continue
            if best is None or conflict.distance < best[0]:
                best = (conflict.distance, audit)
    return best


def check_margin_is_the_tool_radius(result, job):
    emit("")
    emit("== the margin is the operation's own tool radius ==")
    victim = _worst(result)
    tool = victim.operation.ToolController.Tool
    check(abs(victim.margin - float(tool.Diameter.Value) / 2.0) < 1e-9,
          "margin %.4f mm is the tool radius (tool %.4f mm across)"
          % (victim.margin, float(tool.Diameter.Value)))

    # The margin has to be a decision, not a constant, and both criteria have to be
    # live. Counting only conflicts misses the 3 operations whose only finding is
    # an off-sheet pierce, because they have no `Conflict` at all -- which is the
    # point of keeping the two apart.
    emit("")
    emit("== the margin decides, and both criteria are live ==")
    exact = lead_in_audit.scan_job(job, margin=0.0)
    at_zero = len(exact.offending)
    crossings = sum(1 for a in exact.audits for c in a.conflicts
                    if c.distance <= 0.0)
    emit("  margin 0.0: %d operations, of which %d have an exact crossing"
         % (at_zero, crossings))
    check(crossings >= 5,
          "margin 0.0 mm still finds the %d exact crossings -- a crossing is a "
          "clash at any margin" % crossings)
    # Floors measured on the current (re-nested) fixture: 7 at margin 0.0,
    # 12 at the tool radius (0.6), 39 at 2.0. These move when the sheet is
    # re-nested; they are re-measured with the fixture (see `probe_pins`).
    for margin, expected_min in ((0.0, 7), (0.6, 12), (2.0, 39)):
        at = lead_in_audit.scan_job(job, margin=margin)
        found = len(at.offending)
        check(found >= expected_min,
              "margin %.1f mm finds at least %d (found %d)"
              % (margin, expected_min, found))


# -- the write half -------------------------------------------------------

def check_progress_is_reported(doc, job):
    """The seam has to deliver both kinds of event, and the right shapes.

    `resolve_conflicts` reports an operation; `resolve_audit` reports its
    candidates. They are different call sites with different arguments, and the
    view takes them by keyword -- passing candidates positionally would land them
    in the operation-index parameter and make the bar count operations by the
    number of candidates tried. That is a real number and a wrong one, so it is
    asserted rather than assumed.
    """
    emit("")
    emit("== progress is reported, per operation and per candidate ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    events = []

    def seam(**kwargs):
        events.append(dict(kwargs))
        # Prove the view can take every event the search actually emits.
        lead_in_progress.LeadInProgressView().update(**kwargs)

    result = lead_in_audit.scan_job(fresh, margin=2.0)
    lead_in_audit.resolve_conflicts(result, dry_run=True, limit=2,
                                    progress=seam)
    counts = result.counts()
    emit("  %d events for %d fixable + %d unresolved"
         % (len(events), counts.get("fixable", 0),
            counts.get("unresolved", 0)))
    check(bool(events), "the seam was called (%d times)" % len(events))

    per_operation = [e for e in events if e.get("operation_index") is not None]
    per_candidate = [e for e in events if e.get("candidates") is not None]
    check(bool(per_operation), "operations are reported (%d)"
          % len(per_operation))
    check(bool(per_candidate), "candidates are reported (%d)"
          % len(per_candidate))

    # Candidates must not be carrying an operation index, and vice versa.
    check(all("operation_index" not in e for e in per_candidate),
          "candidate events carry no operation index",
          "Otherwise the candidates are landing in the operation-index "
          "parameter and the bar is counting operations by candidates tried.")
    check(all("candidates" not in e for e in per_operation),
          "operation events carry no candidate count")

    outcomes = [e["outcome"] for e in events if e.get("outcome")]
    check("fixable" in outcomes,
          "a dry run reports 'fixable', not 'fixed' (%s)" % sorted(set(outcomes)))
    check("fixed" not in outcomes,
          "and never 'fixed', because nothing moved")

    # And on a real run it does say fixed.
    events2 = []
    fresh2 = replay(FreeCAD.openDocument(FIXTURE))
    result2 = lead_in_audit.scan_job(fresh2)
    lead_in_audit.resolve_conflicts(result2, dry_run=False, limit=2,
                                    progress=lambda **kw: events2.append(kw))
    check("fixed" in [e.get("outcome") for e in events2],
          "a real run reports 'fixed'")


def check_cancel_stops_between_operations(doc, job):
    """Cancelling keeps what was done, and says what was not reached.

    The cancel is triggered from the **progress seam**, not from counting polls.
    Counting polls is not deterministic here: `cancel_check` is read both between
    operations and between candidates, so "cancel after two polls" lands in a
    different place depending on how many candidates each operation happened to
    try. The first version of this check counted polls and asserted every
    skipped operation said "not reached" -- and failed, because the one that was
    cancelled *mid-search* correctly says something else.
    """
    emit("")
    emit("== cancelling stops the search and keeps what it fixed ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    result = lead_in_audit.scan_job(fresh, margin=2.0)
    todo = len(result.offending)
    emit("  %d operation(s) to search" % todo)

    state = {"reached": 0, "cancel": False}

    def seam(operation_index=None, **_kwargs):
        if operation_index is not None:
            state["reached"] = operation_index
            if operation_index >= 3:
                state["cancel"] = True

    lead_in_audit.resolve_conflicts(result, dry_run=False, limit=2,
                                    progress=seam,
                                    cancel_check=lambda: state["cancel"])
    counts = result.counts()
    emit("  %s" % counts)

    check(result.cancelled is True, "the run reports that it was cancelled")
    searched = (counts.get(lead_in_audit.OperationAudit.FIXED, 0)
                + counts.get(lead_in_audit.OperationAudit.UNRESOLVED, 0))
    check(searched < todo,
          "it stopped early: %d of %d searched" % (searched, todo))
    check(counts.get(lead_in_audit.OperationAudit.SKIPPED, 0) == todo - searched,
          "and the other %d are SKIPPED" % (todo - searched))

    not_reached = result.not_reached
    check(len(not_reached) == todo - searched,
          "result.not_reached holds exactly the %d SKIPPED ones"
          % (todo - searched))
    # Two legitimate reasons to be SKIPPED, and they say different things:
    # cancelled between candidates, and never reached at all. Neither may claim
    # the operation could not be fixed.
    for audit in not_reached:
        check("could not be fixed" not in audit.detail
              and "cleared every" not in audit.detail,
              "%s does not claim its cut is unfixable (%s)"
              % (audit.label, audit.detail))
        # A cancellation is not a verdict on the cut. Marking these would tell
        # the user their recipe is wrong about something nobody looked at.
        check(not audit.dressup.Label.startswith(lead_in_audit.CONFLICT_PREFIX),
              "%s was not marked %s" % (audit.label,
                                        lead_in_audit.CONFLICT_PREFIX))
    check(any("not reached" in a.detail for a in not_reached),
          "at least one says plainly it was not reached")

    lines = lead_in_audit.describe_result(result, dry_run=False)
    joined = "\n".join(lines)
    check("Cancelled" in joined, "the report says it was cancelled")
    check("not looked at" in joined, "and how many were not reached")


def check_cancel_mid_operation_restores_the_start_point(doc, job):
    """A cancel between candidates must not leave the cut moved.

    This is the failure that makes cancelling worse than not offering it: the
    search was halfway through moving `StartPoint` when the user stopped it, and
    left it on the last candidate it tried.

    Driven directly through `resolve_audit` rather than `resolve_conflicts`, so
    the cancel lands at a known candidate of a known operation.
    """
    emit("")
    emit("== cancelling mid-operation leaves the operation as it was ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))

    # Find an operation whose search genuinely takes several candidates. The
    # known offender is fixed on candidate **1**, so a cancel at candidate 3
    # never fires and the test would assert that a completed fix was a
    # cancelled one. Which operation is slowest is measured here rather than
    # assumed: a dry run restores everything, so it is safe to run first.
    survey = lead_in_audit.scan_job(fresh, margin=2.0)
    lead_in_audit.resolve_conflicts(survey, dry_run=True)
    hardest = None
    for candidate in survey.fixable:
        counts_tried = _candidates_tried(candidate.detail)
        if counts_tried is None:
            continue
        if hardest is None or counts_tried > hardest[0]:
            hardest = (counts_tried, candidate)
    if not check(hardest is not None,
                 "a dry run found some fixable operation to work with"):
        return

    how_many, template = hardest
    emit("  slowest operation needs %.0f candidate(s): %s"
         % (how_many, template.label))
    if not check(how_many >= 3,
                 "and it needs at least 3, so a mid-operation cancel is real "
                 "(needs %.0f)" % how_many):
        return

    dressup = template.dressup
    operation = dressup.Base
    result = lead_in_audit.scan_job(fresh, margin=2.0)
    prepared, index, _unreadable = lead_in_audit._prepare(fresh, 2.0)
    entry = [p for p in prepared if p[0].Name == dressup.Name][0]
    _d, _op, _part, margin, runs, body = entry
    target = _find(result.audits, dressup.Name)
    target.body = body
    target.margin = margin
    target.status = lead_in_audit.OperationAudit.UNRESOLVED
    target.conflicts = lead_in_audit.conflicts_for(runs, operation, target.part,
                                                   index, margin)

    before_point = FreeCAD.Vector(operation.StartPoint)
    before_use = bool(operation.UseStartPoint)
    before_commands = len(dressup.Path.Commands)

    accepted, tried, detail = lead_in_audit.resolve_audit(
        target, index, fresh, limit=48, dry_run=False,
        cancel_check=_stop_after(2))

    check(accepted is False, "no candidate was accepted (%s)" % detail)
    check("cancelled" in detail, "and it says it was cancelled (%s)" % detail)
    check(target.status == lead_in_audit.OperationAudit.SKIPPED,
          "the operation is SKIPPED, not fixed or unfixable (%s)"
          % target.status)
    check(tried > 0, "it had actually tried %d candidate(s)" % tried)
    check(FreeCAD.Vector(operation.StartPoint) == before_point,
          "StartPoint is back where it was found (%s vs %s)"
          % (operation.StartPoint, before_point),
          "A search stopped halfway with StartPoint moved would have changed "
          "the user's cut without fixing anything.")
    check(bool(operation.UseStartPoint) == before_use,
          "and UseStartPoint is back as it was")
    check(len(dressup.Path.Commands) == before_commands,
          "and the dressup's path is the same length (%d vs %d)"
          % (len(dressup.Path.Commands), before_commands))
    check(not dressup.Label.startswith(lead_in_audit.CONFLICT_PREFIX),
          "and it was not marked %s" % lead_in_audit.CONFLICT_PREFIX)


def _stop_after(n):
    """A `cancel_check` that says stop once the nth candidate has been tried.

    Counting is driven by the progress seam rather than by the poll itself,
    because `cancel_check` is polled *before* each candidate -- so a poll count
    is one ahead of the candidates tried, which is the sort of off-by-one that
    makes a test pass for the wrong reason.
    """
    state = {"seen": 0}

    def check_cancel():
        state["seen"] += 1
        return state["seen"] > n

    return check_cancel


def check_a_cancelled_progress_widget_is_not_a_crash(doc, job):
    """`cancel_check` is polled while Qt may be tearing the panel down.

    A `ReferenceError` escaping the poll would be read as a cancel by one side
    and an error by the other, and the run would either stop for no reason or
    abandon fixes that were working.
    """
    emit("")
    emit("== a progress seam that raises does not stop the search ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    result = lead_in_audit.scan_job(fresh)

    def broken_progress(**_kwargs):
        raise RuntimeError("the widget was closed")

    lead_in_audit.resolve_conflicts(result, dry_run=False, limit=2,
                                    progress=broken_progress)
    counts = result.counts()
    emit("  %s" % counts)
    check(counts.get(lead_in_audit.OperationAudit.FIXED, 0) > 0,
          "the search ran to completion anyway (%s)" % counts)

    def broken_cancel():
        raise RuntimeError("the widget was closed")

    fresh2 = replay(FreeCAD.openDocument(FIXTURE))
    result2 = lead_in_audit.scan_job(fresh2, margin=2.0)
    lead_in_audit.resolve_conflicts(result2, dry_run=False, limit=2,
                                    cancel_check=broken_cancel)
    check(result2.cancelled is False,
          "a cancel_check that raises is not a cancel (%s)"
          % result2.cancelled)
    check(result2.counts().get(lead_in_audit.OperationAudit.FIXED, 0) > 0,
          "and the search carried on (%s)" % result2.counts())


def check_dry_run_writes_nothing(doc, job):
    emit("")
    emit("== report-only mode writes nothing at all ==")
    # Resolved from a scan rather than by name: see `_worst`.
    dressup = _worst(lead_in_audit.scan_job(job)).dressup
    operation = dressup.Base
    before = (dressup.Label, bool(operation.UseStartPoint),
              FreeCAD.Vector(operation.StartPoint), len(dressup.Path.Commands))

    result = lead_in_audit.scan_job(job)
    flagged = len(result.offending)
    lead_in_audit.resolve_conflicts(result, dry_run=True)

    after = (dressup.Label, bool(operation.UseStartPoint),
             FreeCAD.Vector(operation.StartPoint), len(dressup.Path.Commands))
    check(before == after,
          "label, UseStartPoint, StartPoint and the path are all unchanged",
          "before: %s\nafter:  %s" % (before, after))
    check(not hasattr(dressup, lead_in_audit.PROP_CONFLICT),
          "no conflict property was added")
    check(len(lead_in_audit.scan_job(job).offending) == flagged,
          "the %d conflict(s) are still there, so nothing was quietly fixed"
          % flagged)


def check_dry_run_says_what_is_fixable(doc, job):
    """Report-only must answer "how many can be fixed", not "none can".

    This was wrong and wrong in the worst way. The dry run *found* the fix --
    `resolve_audit` returned `True` with a detail string -- and then let the
    status fall through to `UNRESOLVED`, because `resolve_conflicts` recorded
    only the failures. Report-only on a job where every conflict was fixable
    reported every one of them as needing doing by hand, and offered to mark
    them all `CONFLICT_`. Read literally, that says the feature cannot fix
    anything on this nest.
    """
    emit("")
    emit("== report-only says what could be fixed ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    result = lead_in_audit.scan_job(fresh)
    flagged = len(result.offending)
    check(flagged > 0, "the fresh job has %d conflict(s) to classify" % flagged)

    lead_in_audit.resolve_conflicts(result, dry_run=True)
    counts = result.counts()
    fixable = counts.get(lead_in_audit.OperationAudit.FIXABLE, 0)
    unresolved = counts.get(lead_in_audit.OperationAudit.UNRESOLVED, 0)
    emit("  %s" % counts)

    # Every operation the scan flagged must come back as one or the other --
    # nothing may be silently dropped -- and the fixable count must be large,
    # because the bug this check exists for reported *every* operation as
    # unfixable even when the search had found a start point for each.
    #
    # It is not asserted that all of them are fixable. On the current (re-nested)
    # fixture 3 of 12 genuinely are not: their neighbours sit close enough that no
    # start point on the wire clears the 0.6 mm margin even at the escalated
    # budget, and the search says so. An earlier version of this check asserted
    # `unresolved == 0`, which was true on the 48-part fixture because it had one
    # conflict and it happened to be fixable -- and would now be asserting a
    # falsehood about three real operations.
    check(fixable + unresolved == flagged,
          "all %d flagged came back classified (%d fixable + %d unresolved)"
          % (flagged, fixable, unresolved),
          "An operation was dropped between the scan and the report.")
    # Floor is the measured fixable count on the current fixture (9 of 12). It is
    # deliberately just under the total so it still means "most are fixable,
    # not none" after a re-nest changes the split, while still failing the bug
    # this check exists for (a search that reports every operation unfixable).
    check(fixable >= 9,
          "most are found fixable (%d of %d)" % (fixable, flagged),
          "If fixable is 0 while the search succeeded, the verdict it produced "
          "is being thrown away.")

    # An unresolved one must say *why*, and must not claim to have been fixed.
    for audit in result.unresolved[:3]:
        check("no start point" in audit.detail or "cancelled" in audit.detail,
              "%s says why (%s)" % (audit.label, audit.detail))
        check("moved the start point" not in audit.detail,
              "%s does not claim it moved" % audit.label)

    lines = lead_in_audit.describe_result(result, dry_run=True)
    emit("  %s" % lines[0])
    check("could be moved to a clear start point" in lines[0],
          "the summary says how many could be moved")
    check("%d need doing by hand" % unresolved in lines[0],
          "and how many need doing by hand (%d)" % unresolved)

    # And the fixable ones name a start point, so the user can see it is real.
    for audit in result.fixable[:2]:
        check("a start point at" in audit.detail,
              "%s names the start point that clears it (%s)"
              % (audit.label, audit.detail))


def check_dry_run_writes_no_marks(doc, job):
    """Report-only must not leave a `CONFLICT_` on anything.

    A second bug, found while fixing the first: `mark_conflict` writes a Label
    and a property, and the unfixable path called it regardless of `dry_run`. So
    running the report to find out whether there was anything here marked every
    operation it *couldn't* fix -- which is the opposite of writing nothing, and
    the marks survive because nothing removes them.

    `check_dry_run_writes_nothing` above never caught this: the fixture's one
    conflict is fixable, so the unfixable path was never reached. This uses a
    margin and a candidate budget chosen so that some conflicts come out
    fixable and some do not -- which is also the only honest way to test the
    mixed report.
    """
    emit("")
    emit("== report-only leaves no CONFLICT_ marks, even on unfixable ones ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    # 2.0 mm flags 32; a two-candidate budget cannot clear most of them, so the
    # result is a genuine mix. (The 2 that come out unresolved are a budget
    # artefact, not genuinely unfixable -- the point is that the path is taken.)
    result = lead_in_audit.scan_job(fresh, margin=2.0)
    lead_in_audit.resolve_conflicts(result, dry_run=True, limit=2)
    counts = result.counts()
    emit("  %s" % counts)

    check(counts.get(lead_in_audit.OperationAudit.FIXABLE, 0) > 0,
          "some were fixable (%d)"
          % counts.get(lead_in_audit.OperationAudit.FIXABLE, 0))
    check(counts.get(lead_in_audit.OperationAudit.UNRESOLVED, 0) > 0,
          "and some were not (%d), so the unfixable path was exercised"
          % counts.get(lead_in_audit.OperationAudit.UNRESOLVED, 0))

    marked = [a.label for a in result.audits
              if a.dressup.Label.startswith(lead_in_audit.CONFLICT_PREFIX)]
    check(not marked,
          "no dressup was marked %s (%d were: %s)"
          % (lead_in_audit.CONFLICT_PREFIX, len(marked), marked[:3]))
    with_property = [a.label for a in result.audits
                     if getattr(a.dressup, lead_in_audit.PROP_CONFLICT, "")]
    check(not with_property,
          "and no conflict property was set (%d were)"
          % len(with_property))

    lines = lead_in_audit.describe_result(result, dry_run=True, limit=2)
    check("Nothing has been changed" in lines[0] or
          any("Nothing has been changed" in line for line in lines),
          "the report says nothing was changed")
    check(any("Report and fix" in line for line in lines),
          "and says what to run next")


def check_unfixable_marking_happens_on_a_real_run(doc, job):
    """The counterpart: a real run *does* mark what it could not fix."""
    emit("")
    emit("== a real run marks what it could not fix ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    result = lead_in_audit.scan_job(fresh, margin=2.0)
    lead_in_audit.resolve_conflicts(result, dry_run=False, limit=2)
    counts = result.counts()
    emit("  %s" % counts)

    unresolved = [a for a in result.audits
                  if a.status == lead_in_audit.OperationAudit.UNRESOLVED]
    check(bool(unresolved),
          "%d operation(s) could not be fixed within the budget (%s)"
          % (len(unresolved), counts))
    for audit in unresolved[:3]:
        check(audit.dressup.Label.startswith(lead_in_audit.CONFLICT_PREFIX),
              "%s is marked %s" % (audit.label, lead_in_audit.CONFLICT_PREFIX))
        check(bool(getattr(audit.dressup, lead_in_audit.PROP_CONFLICT, "")),
              "%s has its conflict property set" % audit.label)
        check("marked %s" % lead_in_audit.CONFLICT_PREFIX in audit.detail,
              "%s says it was marked, rather than promising a fix" % audit.label)
        lead_in_audit.clear_conflict(audit.dressup)

    lines = lead_in_audit.describe_result(result, dry_run=False, limit=2)
    check(any(lead_in_audit.CONFLICT_PREFIX in line for line in lines),
          "the report tells the user they are marked")
    check(any("moved to a clear start point" in line for line in lines),
          "and separately what was moved")


def check_own_body_is_not_drawn_across_the_gap_between_wires(doc, job):
    """The two geometry defects, measured on the real replay.

    Both produced findings that were not there, and both were invisible in the
    GUI:

      * **The link between wires.** A Profile over two wires is joined by a
        `G0`, which `cut_moves` correctly drops. Joining the moves either side
        of it into one polyline therefore *draws* that link. On this fixture, 28
        of 110 operations have such a join, and the largest spans 34.941 mm --
        against 48.9727 mm of real cut on the operation it belongs to. Every
        clearance in the audit is measured against this geometry, so a fabricated
        segment can invent a conflict as readily as hide one.

      * **The attachment.** A body wire closes to within 8.039e-14 mm of its own
        start, and a lead-in's end sits on that start at exactly 0. So the
        attachment arrives as a noise-level crossing of two segments that only
        appear to cross -- which is how six own-body crossings were reported, none
        of them visible in the GUI.
    """
    emit("")
    emit("== the body geometry is the cut, and nothing else ==")
    # Its own replay: the shared `job` has been fixed by now, so its lead-ins
    # have moved and the counts below would not be the fixture's.
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    prepared, index, _unreadable = lead_in_audit._prepare(fresh, None)

    joined = 0
    over_length = []
    for dressup, _op, _part, _margin, _runs, body in prepared:
        geometry = lead_in_audit.body_geometry(body)
        if geometry is None:
            continue
        wires = lead_in_audit._body_wires(body)
        if len(wires) < 2:
            continue
        joined += 1
        # **Summed over the wires, and from the sampled points.** Summing
        # `hypot(x1-x0, y1-y0)` over the moves would use each arc's *chord*,
        # which understates the real path by up to 14.5239 mm on this fixture --
        # the same chording error `CutMove.points` exists to prevent, reintroduced
        # in the test, and it would report every multi-wire body as too long.
        real = sum(lead_in_audit.moves_to_line(wire).length
                   for wire in wires)
        if geometry.length > real + 1e-6:
            over_length.append((dressup.Label, geometry.length, real))
        # A body of more than one wire must be a MultiLineString. If it were a
        # LineString the wires would be joined and the check above would fail --
        # but asserting the type as well means the failure names the cause.
        check(geometry.geom_type == "MultiLineString",
              "%s: a %d-wire body is a MultiLineString (%s)"
              % (dressup.Label, len(wires), geometry.geom_type))

    check(joined > 0,
          "multi-wire operations exist on this fixture (%d of %d)"
          % (joined, len(prepared)))
    check(not over_length,
          "no body geometry is longer than the moves it is made of "
          "(%d are: %s)"
          % (len(over_length),
             "; ".join("%s %.4f vs %.4f" % (label, length, real)
                       for label, length, real in over_length[:3])),
          "A body longer than its own moves has a line in it that the torch "
          "never cuts. Measured on this fixture: 34.9409 mm of one body, drawn "
          "across the part between two wires.")

    # And the attachment: no run may be reported as crossing its own operation.
    own = 0
    for dressup, op, _part, _margin, runs, _body in prepared:
        geometry = lead_in_audit.body_geometry(_body)
        if geometry is None:
            continue
        for run in runs:
            if lead_in_audit._crosses_away_from_attachment(run.geometry,
                                                           geometry):
                own += 1
    check(own == 0,
          "no lead run crosses its own operation's cut path (%d do)"
          % own,
          "Every lead-in ends on the contour it leads into, so this is the "
          "attachment, not a crossing. A body wire closes to within 8.039e-14 "
          "mm of its start, which is enough for Shapely to call it a crossing.")


def check_the_sheet_edge_is_checked(doc, job):
    """A pierce point off the sheet is a different failure from a clash.

    Nothing on this fixture has a `Drilling`, so the case has to be found in the
    geometry rather than built: three lead-ins on the 54-part fixture pierce off
    the edge of the sheet, and the stock is 600 x 300 mm.
    """
    emit("")
    emit("== the sheet edge is checked, and reports what it could not check ==")
    # Its own replay. The shared `job` has had its start points moved by
    # `check_the_fix`, and the three offenders on this fixture are among them --
    # so a scan of the fixed job finds the sheet clean, which is the right answer
    # for a fixed job and the wrong thing to measure here.
    result = lead_in_audit.scan_job(replay(FreeCAD.openDocument(FIXTURE)))
    check(result.sheet is not None,
          "the stock outline was read from the job's own Stock")
    if result.sheet is not None:
        bounds = result.sheet.bounds
        check(abs((bounds[2] - bounds[0]) - 600.0) < 1e-6
              and abs((bounds[3] - bounds[1]) - 300.0) < 1e-6,
              "and it is 600 x 300 mm (%.1f x %.1f)"
              % (bounds[2] - bounds[0], bounds[3] - bounds[1]))

    off = [(a, o) for a in result.audits for o in (a.off_sheet or [])]
    check(bool(off),
          "lead-ins piercing off the sheet were found (%d of them)"
          % len(off))
    for audit, finding in off[:3]:
        check(finding.distance < 0.0,
              "%s: the pierce point is outside the sheet (%.4f mm)"
              % (audit.label, finding.distance))
        check("off the edge of the sheet" in finding.describe(),
              "%s: and the report says so (%s)"
              % (audit.label, finding.describe()))
        # Both kinds of finding must set the status, or one of them is invisible.
        check(audit.status == lead_in_audit.OperationAudit.UNRESOLVED,
              "%s is reported as needing attention" % audit.label)
        check(audit.needs_attention,
              "%s: needs_attention is true" % audit.label)

    lines = lead_in_audit.describe_result(result, limit=3)
    joined_lines = "\n".join(lines)
    check("pierce off the edge of the sheet" in joined_lines,
          "the report has a section for it")

    # And a job whose stock cannot be read must say the check did not run,
    # rather than reporting a clean sheet that silently excludes the edge.
    blank_doc = FreeCAD.openDocument(FIXTURE)
    blank = replay(blank_doc)
    stock = getattr(blank, "Stock", None)
    check(stock is not None, "the fresh replay has a Stock to remove")
    # `removeObject` takes the document object and clears the job's reference on
    # the way, which is what `stock_polygon` looks at. Removing it before the
    # replay would not work: the replay builds a fresh stock and repoints the
    # new job at it.
    blank_doc.removeObject(stock.Name)
    blind = lead_in_audit.scan_job(blank)
    check(blind.sheet is None,
          "with no Stock there is no outline to test against")
    check(all(a.off_sheet is None for a in blind.audits),
          "and every operation records the off-sheet check as not run, rather "
          "than as clear")
    blind_lines = "\n".join(lead_in_audit.describe_result(blind, limit=2))
    check("sheet edge was not checked" in blind_lines,
          "and the report says the edge was not checked",
          "Reporting a clean sheet here would claim the edge is fine when "
          "nothing looked at it.")


def check_lead_runs_are_indexed_too(doc, job):
    """An index of bodies only cannot see a lead-in crossing a lead-in.

    A lead-in is not part of anybody's cut path -- it is its own geometry -- so
    with bodies alone there is no entry for one to collide with. This is the
    interference the original report of this problem was a photograph of.

    Asserted structurally rather than by finding a clash, because this fixture
    has none: the closest pair of runs on different operations is 4.2926 mm, so
    the capability is real and the fixture does not exercise it. Asserting "a
    clash was found" here would need a fixture nobody has.
    """
    emit("")
    emit("== lead runs are indexed, not only bodies ==")
    # Its own replay, because an earlier check turned `LeadOut` on for one of
    # the shared job's operations and left it there.
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    prepared, index, _unreadable = lead_in_audit._prepare(fresh, None)
    bodies = [e for e in index.entries if e[4] == lead_in_audit.ROLE_BODY]
    lead_ins = [e for e in index.entries if e[4] == lead_in_audit.ROLE_LEAD_IN]
    lead_outs = [e for e in index.entries if e[4] == lead_in_audit.ROLE_LEAD_OUT]

    check(len(bodies) == 110, "every body is indexed (%d)" % len(bodies))
    check(len(lead_ins) == 142,
          "every lead-in is indexed (%d)" % len(lead_ins))
    check(len(lead_outs) == 28,
          "every lead-out is indexed (%d)" % len(lead_outs))
    check(len(index.entries) == 280,
          "so the index holds 280 entries in all (%d)"
          % len(index.entries))

    # Each run must be able to recognise its own entry, or it reports itself at a
    # distance of zero.
    seen = set()
    for dressup, op, _part, _margin, runs, _body in prepared:
        for run in runs:
            seen.add((id(op), run.run_index))
    check(len(seen) == 170,
          "every run carries a distinct (operation, run_index) (%d of 170)"
          % len(seen))

    # And the junction test has to actually drop the attachment on real
    # geometry, where a lead-in's end sits on its own wire at exactly 0.
    same_operation = 0
    for audit in lead_in_audit.scan_job(fresh).audits:
        for conflict in audit.conflicts:
            if conflict.other_operation is audit.operation:
                same_operation += 1
    check(same_operation == 0,
          "and no run is reported as conflicting with its own operation (%d "
          "are)" % same_operation)


def check_the_fix(doc, job):
    emit("")
    emit("== a fix is a real fix ==")
    # Resolved from a scan, not by name: see `_worst`.
    offender = _worst(lead_in_audit.scan_job(job))
    dressup = offender.dressup
    operation = dressup.Base
    check(not bool(operation.UseStartPoint),
          "the operation starts with UseStartPoint False, as the replay left it")

    result = lead_in_audit.scan_job(job)
    started = time.time()
    lead_in_audit.resolve_conflicts(result)
    elapsed = time.time() - started

    victim = _find(result.audits, dressup.Name)
    check(victim.status == lead_in_audit.OperationAudit.FIXED,
          "the offender is recorded as fixed (%s)" % victim.detail)
    check(bool(operation.UseStartPoint),
          "UseStartPoint was set True, without which StartPoint is not read")
    check(not dressup.Label.startswith(lead_in_audit.CONFLICT_PREFIX),
          "it is not marked %s" % lead_in_audit.CONFLICT_PREFIX)
    emit("  fixed in %.2f s (%s)" % (elapsed, victim.detail))

    # A fresh scan must find this operation clear, and the number left over must
    # be exactly the ones the search could not fix -- no more. An earlier
    # version asserted zero left over, which was true when the fixture had one
    # conflict; on the 54-part fixture 5 operations are genuinely unfixable at
    # this margin, and they are left marked rather than moved.
    after = lead_in_audit.scan_job(job)
    left = [a for a in after.offending if a.dressup.Name == dressup.Name]
    check(not left,
          "a fresh scan finds the fixed operation clear",
          "the fix moved StartPoint but the conflict is still there")
    unresolved_before = len(result.unresolved)
    check(len(after.offending) == unresolved_before,
          "and the %d still conflicting are exactly the ones the search could "
          "not fix (%d)" % (unresolved_before, len(after.offending)),
          "A fix that leaves fewer conflicts than the search reported fixing "
          "means some fixed operation was re-flagged.")

    # The number, not just the absence of a complaint.
    prep, index, _unreadable = lead_in_audit._prepare(job, None)
    entry = [p for p in prep if p[0].Name == dressup.Name][0]
    _d, op, part, margin, runs, _body = entry
    worst = None
    for run in runs:
        for other_part, _od, other_op, other_geometry, _role, _ri in index.entries:
            if other_op is op or other_part == part:
                continue
            distance = run.geometry.distance(other_geometry)
            if worst is None or distance < worst[0]:
                worst = (distance, other_part)
    check(worst is not None and worst[0] > margin,
          "every lead-in now clears %s by more than the %.4f mm margin "
          "(closest is %.4f mm from %s)"
          % (part, margin, worst[0] if worst else -1,
             worst[1] if worst else "?"))

    # And running it again must be a no-op rather than a second move.
    again = lead_in_audit.scan_job(job)
    lead_in_audit.resolve_conflicts(again)
    check(again.counts().get(lead_in_audit.OperationAudit.FIXED, 0) == 0,
          "a second run changes nothing (%s)" % again.counts())


def check_no_tool_means_reported_not_guessed(doc, job):
    """A job with no readable tool must not be audited against an invented margin.

    The margin has to come from somewhere, and the only honest source is the
    tool. With none readable the audit reports every operation as unchecked
    rather than falling back to a default -- a default would be a number nobody
    chose, testing interference against it, and finding nothing, and saying the
    sheet was clean.
    """
    emit("")
    emit("== no readable tool means nothing is claimed ==")
    empty = replay(FreeCAD.openDocument(FIXTURE))
    for controller in list(empty.Tools.Group):
        empty.Tools.removeObject(controller)
    for entry in empty.Operations.Group:
        base = getattr(entry, "Base", None)
        if base is not None and hasattr(base, "ToolController"):
            base.ToolController = None
        if hasattr(entry, "ToolController"):
            entry.ToolController = None

    result = lead_in_audit.scan_job(empty)
    check(not result.audits,
          "no operation was audited (%d were)" % len(result.audits))
    check(len(result.unreadable) == 110,
          "all 110 are reported as unchecked (%d)" % len(result.unreadable))
    check(bool(result.unreadable)
          and "tool radius" in result.unreadable[0][1],
          "and the reason says why ('%s')"
          % (result.unreadable[0][1] if result.unreadable else ""))

    lines = lead_in_audit.describe_result(result)
    check(any("Could not be checked" in line for line in lines),
          "the report says so rather than reporting a clean sheet")
    emit("  %s" % lines[0])


def check_unfixable_is_marked(doc, job):
    """Force an unfixable operation and check what happens to it.

    Built rather than found, because the fixture's one real conflict is
    fixable and an unfixable case that only appears on someone's nest is an
    unfixable case nobody has tested.
    """
    emit("")
    emit("== an operation that cannot be fixed is marked, not moved ==")
    # Its own replay. The shared `job` has been fixed by `check_the_fix` by now,
    # so it has no conflicts left to be unable to fix -- which is what the first
    # version of this check hit, as a `NoneType` from `_worst`.
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    dressup = _worst(lead_in_audit.scan_job(fresh)).dressup
    operation = dressup.Base
    before_label = dressup.Label
    before_point = FreeCAD.Vector(operation.StartPoint)

    # A margin nothing can clear: larger than the sheet.
    result = lead_in_audit.scan_job(fresh, margin=1.0e6)
    check(bool(result.offending),
          "an impossible margin flags every operation (%d)"
          % len(result.offending))
    lead_in_audit.resolve_conflicts(result, limit=2)

    unresolved = [a for a in result.audits
                  if a.status == lead_in_audit.OperationAudit.UNRESOLVED]
    check(len(unresolved) == len(result.audits),
          "all of them are left unresolved (%d of %d)"
          % (len(unresolved), len(result.audits)))
    for audit in unresolved[:3]:
        check(audit.dressup.Label.startswith(lead_in_audit.CONFLICT_PREFIX),
              "%s is marked %s" % (audit.label, lead_in_audit.CONFLICT_PREFIX))
        check(bool(getattr(audit.dressup, lead_in_audit.PROP_CONFLICT, "")),
              "%s has the conflict property set" % audit.label)
        check("CONFLICT_CONFLICT" not in audit.dressup.Label,
              "%s has one prefix, not two" % audit.label)

    victim = [a for a in unresolved if a.dressup.Name == dressup.Name][0]
    check(FreeCAD.Vector(victim.operation.StartPoint) == before_point,
          "the failed search left the start point where it found it",
          "a search that gave up halfway, with StartPoint on its last "
          "candidate, would have moved the user's cut without fixing it.")

    for audit in unresolved:
        lead_in_audit.clear_conflict(audit.dressup)
    check(dressup.Label == before_label,
          "clearing the marks restores the labels")


# -- lead-out -------------------------------------------------------------

def _pick_conflicting(doc, job):
    """`(dressup, operation)` for the known offender, found by Name.

    Matched on `Name` rather than `Label` because a replay's labels are not
    reproducible across runs -- FreeCAD appends a counter to a duplicate label,
    so `nested_BottomStrap_23` can come back as `nested_BottomStrap_023` -- and
    the internal `Name` is stable.

    **Any operation with a conflict, not specifically the known one.** A second
    replay in the same FreeCAD session numbers its objects differently
    (`DressupLeadInOut007_...` rather than `DressupLeadInOut001_...`), so a
    lookup by that exact name finds nothing here. The point of the check is an
    operation with both ends in trouble, and any of them serves.

    **Prefers one whose lead-in is also in conflict.** The caller adds a long
    lead-out and asserts that both ends are then reported, which only holds if
    the operation's lead-in conflicts to begin with. Returning the first
    unresolved operation regardless picked, on the current (re-nested) fixture,
    an operation whose lead-in is clear -- so the assertion saw only the
    lead-out. On this fixture exactly one operation has both ends in conflict
    (`BottomStrap_12`, the one whose lead-in *and* lead-out no single start
    point clears), and that is the one this now selects.
    """
    result = lead_in_audit.scan_job(job)
    fallback = None
    for audit in result.audits:
        if audit.status != lead_in_audit.OperationAudit.UNRESOLVED:
            continue
        if fallback is None:
            fallback = audit
        if any(c.run.kind == "lead-in" for c in audit.conflicts):
            return audit.dressup, audit.operation
    if fallback is not None:
        return fallback.dressup, fallback.operation
    return None, None


def check_lead_out_is_judged_too(doc, job):
    """Turn on `LeadOut` for a real operation and check both ends together.

    The fixture has `LeadOut` false everywhere, so this is the only coverage the
    lead-out half gets. A Profile with both ends dressed places its lead-in and
    its lead-out from the same start point, so moving it moves both -- and a
    search that tested only the lead-in would accept a candidate that made the
    lead-out worse.
    """
    emit("")
    emit("== a lead-in and a lead-out are judged together ==")
    pairs = lead_in_audit.lead_in_out_dressups(job)
    if not pairs:
        check(False, "the job has LeadInOut dressups to test")
        return
    dressup, operation = pairs[0]

    dressup.LeadOut = True
    dressup.StyleOut = "Perpendicular"
    dressup.RadiusOut = 5.0
    dressup.recompute()

    lead_ins, lead_outs, _body = lead_runs(cut_moves(operation.Path),
                                           cut_moves(dressup.Path))
    if not check(lead_ins is not None and lead_outs,
                 "a lead-out is now found on %s (%d found)"
                 % (dressup.Label, len(lead_outs or []))):
        return
    check(abs(moves_to_length(lead_outs) - 5.0) < 0.01,
          "the lead-out is RadiusOut long (%.4f vs 5.0000)"
          % moves_to_length(lead_outs))

    # Both must be judged, so the scan has to see both.
    result = lead_in_audit.scan_job(job)
    prep, _index, _unreadable = lead_in_audit._prepare(job, None)
    entry = [p for p in prep if p[0].Name == dressup.Name][0]
    audit_runs = entry[4]
    kinds = sorted(set(run.kind for run in audit_runs))
    check(kinds == ["lead-in", "lead-out"],
          "the scan reports both kinds for it (%s)" % kinds)

    # The search has to judge both ends *of one operation* and accept a
    # candidate only when every run is clear.
    #
    # **Checked on a fresh replay**, because an earlier check in this file
    # already fixed the offender, and once its lead-in is clear the thing under
    # test -- both ends in trouble at once -- no longer exists. The first
    # version ran against the already-fixed job, saw only a lead-out, and passed
    # for the wrong reason.
    #
    # The assertion is deliberately **comparative** rather than "nothing can be
    # fixed". An earlier attempt gave the lead-out a 400 mm radius on the
    # assumption that nothing could clear it, and it was wrong: the walk reaches
    # the sheet's edge, where a 400 mm lead-out points off the sheet into empty
    # space and genuinely has nothing in the way. The real property is that the
    # joint test rejects candidates the lead-in alone would accept, and that is
    # what this measures.
    emit("")
    emit("== a search cannot fix a lead-in by breaking its lead-out ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    target, target_op = _pick_conflicting(fresh, fresh)
    if not check(target is not None,
                 "an operation with a conflict was available to test with"):
        return

    saved_style = target.StyleOut
    saved_radius = target.RadiusOut
    saved_leadout = target.LeadOut
    saved_point = FreeCAD.Vector(target_op.StartPoint)
    saved_use = bool(target_op.UseStartPoint)

    # A lead-out long enough to reach the neighbours whatever the start point.
    target.LeadOut = True
    target.StyleOut = "Perpendicular"
    target.RadiusOut = 400.0
    target.recompute()

    try:
        result = lead_in_audit.scan_job(fresh)
        trial = _find(result.audits, target.Name)
        if not check(trial is not None and trial.status ==
                     lead_in_audit.OperationAudit.UNRESOLVED,
                     "the operation is a conflict in its own right"):
            return
        kinds = sorted(set(c.run.kind for c in trial.conflicts))
        check(kinds == ["lead-in", "lead-out"],
              "both ends are reported as conflicting (%s)" % kinds)

        prep, index, _unreadable = lead_in_audit._prepare(fresh, None)
        _d, op, part, margin, runs, body = [
            p for p in prep if p[0].Name == target.Name][0]

        origin = lead_in_audit._origin_of(op, runs)
        lead_in_only = 0
        joint = 0
        inspected = 0
        for xy in lead_in_audit.geodesic_start_point_candidates(
                body, 0, origin, limit=8):
            if not lead_in_audit._apply_start_point(op, xy):
                continue
            lead_ins, lead_outs, _body = lead_runs(
                cut_moves(op.Path), cut_moves(target.Path))
            if lead_ins is None:
                continue
            fresh_runs = lead_in_audit._wire_indices(lead_ins, lead_outs)
            inspected += 1
            only_in = [r for r in fresh_runs if r.kind == "lead-in"]
            if not lead_in_audit.conflicts_for(only_in, op, part, index, margin):
                lead_in_only += 1
            if not lead_in_audit.conflicts_for(fresh_runs, op, part, index,
                                               margin):
                joint += 1

        check(inspected >= 4, "%d candidates were readable" % inspected)
        check(lead_in_only > 0,
              "the lead-in alone clears at some candidate (%d of %d)"
              % (lead_in_only, inspected))
        check(joint < lead_in_only,
              "the joint test rejects candidates the lead-in alone accepts "
              "(%d accepted jointly, %d on the lead-in alone)" % (joint,
                                                                 lead_in_only),
              "This is the property that matters: accepting on the lead-in "
              "alone would fix the reported conflict by creating an "
              "unreported one.")
    finally:
        lead_in_audit.clear_conflict(target)
        target.LeadOut = saved_leadout
        target.StyleOut = saved_style
        target.RadiusOut = saved_radius
        target_op.UseStartPoint = saved_use
        target_op.StartPoint = saved_point
        target.recompute()


def moves_to_length(lead_outs):
    from freecad.nestingworkbench.Tools.Cam.lead_in_audit import moves_to_line
    total = 0.0
    for group in lead_outs:
        geometry = moves_to_line(group)
        if geometry is not None:
            total += geometry.length
    return total


# -- the command ----------------------------------------------------------

def check_command_is_importable():
    emit("")
    emit("== the command registers ==")
    try:
        from freecad.nestingworkbench.commands import command_audit_lead_ins
        check(hasattr(command_audit_lead_ins, "AuditLeadInsCommand"),
              "the command class is there")
        resources = command_audit_lead_ins.AuditLeadInsCommand().GetResources()
        check(bool(resources.get("MenuText")),
              "it has a menu text (%s)" % resources.get("MenuText"))
        check(bool(resources.get("ToolTip")), "it has a tooltip")
    except Exception as exc:
        check(False, "the command imports", traceback.format_exc())


def main():
    doc = FreeCAD.openDocument(FIXTURE)
    emit("Lead-in audit check on %s" % os.path.basename(FIXTURE))

    job = replay(doc)
    if not check(job is not None, "the fixture replays"):
        return finish()

    result = check_scan(doc, job)
    check_known_conflict(result)
    check_margin_is_the_tool_radius(result, job)
    check_dry_run_writes_nothing(doc, job)
    check_dry_run_says_what_is_fixable(doc, job)
    check_dry_run_writes_no_marks(doc, job)
    check_unfixable_marking_happens_on_a_real_run(doc, job)
    check_the_fix(doc, job)
    check_progress_is_reported(doc, job)
    check_cancel_stops_between_operations(doc, job)
    check_cancel_mid_operation_restores_the_start_point(doc, job)
    check_a_cancelled_progress_widget_is_not_a_crash(doc, job)
    check_no_tool_means_reported_not_guessed(doc, job)
    check_unfixable_is_marked(doc, job)
    check_lead_out_is_judged_too(doc, job)
    # These read the geometry itself rather than a report, and they must run
    # before anything mutates the shared `job` -- `check_the_fix` above has
    # already moved start points on it.
    check_own_body_is_not_drawn_across_the_gap_between_wires(doc, job)
    check_lead_runs_are_indexed_too(doc, job)
    check_the_sheet_edge_is_checked(doc, job)
    check_command_is_importable()

    return finish()


def finish():
    emit("")
    emit("== summary ==")
    if _failures:
        emit("%d of %d checks FAILED:" % (len(_failures), _checks[0]))
        for message in _failures:
            emit("   - %s" % message)
    else:
        emit("all %d checks passed" % _checks[0])
    try:
        with open(_STATUS_FILE, "w") as handle:
            handle.write("1" if _failures else "0")
    except Exception:
        pass
    return 1 if _failures else 0


if __name__ in ("__main__", "test_lead_in_audit"):
    try:
        main()
    except Exception:
        emit("harness raised:")
        for line in traceback.format_exc().split("\n"):
            emit("   %s" % line)
        with open(_STATUS_FILE, "w") as handle:
            handle.write("1")
        raise
