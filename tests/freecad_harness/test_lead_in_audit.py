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
from contextlib import contextmanager

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
#: `_worst` finds it from the scan by what it is. The constants below describe
#: the scan rather than pin a measurement, and `_nearest_marginal` re-derives
#: which operation is the interesting one. FreeCAD appends a counter to a
#: duplicate label on every replay, so a name written here is stale by the
#: third replay.
#:
#: The tightest clash is an exact crossing at 0.0000 mm, so it cannot be used to
#: tell a sampled arc from a chorded one -- both give zero. `MAX_ARC_SAG_MM` is
#: what the near-miss check is measured against, and it is a property of the
#: geometry reader rather than of the nest.
VICTIM = "DressupLeadInOut001_replay_nested_BottomStrap_6"
VICTIM_PART = "part_BottomStrap_6"
NEIGHBOUR_PART = "part_TopStrap_1"

#: Distance of the tightest exact crossing. Exactly 0 by definition; asserted so
#: that a geometry regression which turns a crossing into a near miss is caught.
CROSSING_DISTANCE_MM = 0.0

#: The largest distance by which chording can hide a real clash, in mm.
#:
#: **This, and not a measurement, is what the near-miss check is for.** The
#: point of the check is to prove arcs are sampled rather than chorded, and
#: chording *loses* conflicts rather than inventing them -- it pulls a cut path
#: up to `MAX_ARC_SAG_MM` inside the material, so anything closer than that than
#: the true arc would be found clear by a chorded reader. So the assertion is
#: that the nearest near miss is **tighter than the sag**: chord it, and that
#: clash disappears with it.
#:
#: The figure is the `G3` of `DressupLeadInOut006_replay_nested_SimpleSpacer_2`
#: on this fixture: a 52.3926 mm sampled path against a 37.8687 mm chord.
#:
#: It replaces `MARGINAL_DISTANCE_MM`, which pinned the exact nearest near miss
#: at 0.0277 mm. That number is a fact about **the nest**, not about the code:
#: commit `8f0c946` re-nested the fixture and moved it to 0.1481 mm, which
#: failed the check for doing its job perfectly well. Re-pinning it would have
#: been worse than useless -- it would assert whatever the layout happened to
#: produce, and catch nothing. The inequality catches chording just as hard and
#: survives the next re-nest. The exact figure is recorded in
#: `tests/freecad_harness/baseline/`, which is where layout measurements live.
MAX_ARC_SAG_MM = 14.5239

#: Tolerances. The crossing distance is checked tightly because it is exactly
#: 0 by definition; the counts are checked loosely because they are structural.
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


@contextmanager
def group(items, description):
    """`check` a property of every item in `items`, and fail if there are none.

    **A loop of `check(...)` calls over an empty list runs zero times.** Nothing
    fails, nothing is counted, and the suite gets quieter without getting
    greener -- which is the same failure as a stale pin wearing a passing
    costume, and strictly worse, because nobody goes looking.

    That is not hypothetical. Four assertions in
    `check_the_sheet_edge_is_checked` sat inside a loop over the lead-ins that
    pierce off the sheet edge, and when commit `8f0c946` re-nested the fixture
    and left it with none, all four quietly stopped running. The suite showed
    one red check instead of five, and the four that had vanished were not
    counted at all.

    So the emptiness is reported as a check of its own, **before** the loop, and
    the items are yielded rather than iterated by the caller -- there is no
    shape here that can skip it.

        with group(offenders, "lead-ins piercing off the edge of the sheet") as o:
            for audit, finding in o[:3]:
                check(...)
    """
    items = list(items)
    check(bool(items), "the %s were found (%d of them)"
          % (description, len(items)))
    yield items


def replay(doc):
    """Run a replay and return its job."""
    layout, _warnings = cam_replay.resolve_layout_group(doc)
    outcomes = cam_replay.replay_layout(doc, layout, doc.getObject("Job"))
    for outcome in outcomes:
        if outcome.replay_job is not None:
            return outcome.replay_job.job
    return None


#: How many extra lead-outs the suite turns on for itself. Six on top of
#: whatever the fixture asks for, which is currently 2.
BUILT_LEAD_OUTS = 6


def _lead_out_dressups(job):
    """How many `LeadInOut` dressups on this job ask for a lead-out.

    The lead-out census is read off the job rather than written down, so that
    turning a lead-out off in the fixture -- one click in the CAM workbench --
    cannot quietly turn four checks red here without saying why.

    **Not circular.** This counts dressup *flags*, not wires, so it cannot agree
    with a reader that has lost lead-outs: if the scan finds fewer than the
    flags ask for, the check fails.
    """
    count = 0
    for entry in getattr(getattr(job, "Operations", None), "Group", []) or []:
        _op, dressups = cam_replay.walk_stack(entry)
        for dressup in dressups or []:
            if getattr(dressup, "LeadOut", False):
                count += 1
    return count


def _enable_lead_outs(job, want=BUILT_LEAD_OUTS):
    """Turn lead-outs on for `want` dressups that have them off. Returns how many.

    The fixture decides how many lead-outs a replay carries, and it now decides
    **two**. Two is enough to catch a reader that loses lead-outs outright and
    not enough to notice one that loses some of them, so the suite builds its
    own rather than being at the mercy of the file -- the same reason
    `_force_a_pierce_at_the_sheet_edge` builds its own pierce.

    Measured on this fixture: turning six back on leaves the lead-in count at
    **142**, so this adds lead-outs without disturbing anything else.
    """
    pairs = lead_in_audit.lead_in_out_dressups(job)
    off = [pair for pair in pairs if not getattr(pair[0], "LeadOut", False)]
    changed = 0
    for dressup, _operation in off[:want]:
        dressup.LeadOut = True
        dressup.StyleOut = "Perpendicular"
        dressup.RadiusOut = 5.0
        dressup.recompute()
        changed += 1
    if changed:
        job.Document.recompute()
    return changed


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
    lead_ins = sum(1 for a in result.audits for r in a.lead_runs
                   if r.kind == "lead-in")
    lead_outs = sum(1 for a in result.audits for r in a.lead_runs
                    if r.kind == "lead-out")
    want_outs = _lead_out_dressups(job)

    # **142 lead-ins, and not derived.** The lead-in count is the same whether
    # or not a dressup asks for a lead-out -- measured, not assumed -- so it is
    # a property of the fixture rather than of this moment, and pinning it is
    # pinning something real. It was 142 when the fixture also carried 28
    # lead-outs.
    check(lead_ins == 142,
          "every lead-in was found (%d of 142, one per operation at least)"
          % lead_ins)
    check(lead_outs == want_outs,
          "every lead-out that was asked for was found (%d, and %d dressups "
          "ask for one)" % (lead_outs, want_outs))
    check(runs == 142 + want_outs,
          "every lead run was found (got %d across 110 operations: %d "
          "lead-ins and %d lead-outs)" % (runs, lead_ins, lead_outs))

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
    # nearest *near* miss. See `MAX_ARC_SAG_MM` for why this is an inequality
    # against the sag rather than a pin on the measurement.
    marginal = _nearest_marginal(result)
    if check(marginal is not None, "there is a near miss as well as a crossing"):
        distance, owner = marginal
        check(distance < MAX_ARC_SAG_MM,
              "the nearest near miss is %.4f mm, tighter than the %.4f mm a "
              "chorded reader could hide behind (on %s)"
              % (distance, MAX_ARC_SAG_MM, owner.label),
              "Above the largest arc sag, chording would swallow this clash: "
              "the chord of a G3 cuts up to 14.5239 mm inside the true arc, so "
              "the clash would be reported clear. Chording never invents a "
              "conflict, it only loses them.")

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
    # **Floors, not pins.** A count is a fact about the nest and moves whenever
    # the sheet is re-nested, so the exact figure cannot be asserted -- but a
    # *floor* can, and it is what keeps a regression visible: if a change to the
    # geometry reader stops finding clashes, the count drops through the floor
    # whatever the layout is doing. Measured on the current (re-nested) fixture:
    # 36 operations at margin 0.0, 40 at the tool radius, 52 at 3.0 -- so these
    # floors sit well clear of the current values and survive the next re-nest.
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
    # cancelled one. Which operation is hard is measured here rather than
    # assumed: a dry run restores everything, so it is safe to run first.
    #
    # The survey stops as soon as one operation has needed HARD_CANDIDATES,
    # rather than resolving all 110 to keep the maximum. That maximum was never
    # what this check asserts on -- see the note on `_stop_when_hard` for the
    # measurement, and for why computing it cost a quarter of the gate.
    survey = lead_in_audit.scan_job(fresh, margin=2.0)
    lead_in_audit.resolve_conflicts(survey, dry_run=True,
                                    cancel_check=_stop_when_hard(survey))
    hardest = None          # the best seen so far, for the failure message
    chosen = None           # the first that actually clears the bar
    for candidate in survey.fixable:
        counts_tried = _candidates_tried(candidate.detail)
        if counts_tried is None:
            continue
        if hardest is None or counts_tried > hardest[0]:
            hardest = (counts_tried, candidate)
        if counts_tried >= HARD_CANDIDATES and chosen is None:
            chosen = (counts_tried, candidate)
    hardest = chosen if chosen is not None else hardest
    if not check(hardest is not None,
                 "a dry run found some fixable operation to work with"):
        return

    how_many, template = hardest
    emit("  using %s, which needs %.0f candidate(s)"
         % (template.label, how_many))
    if not check(how_many >= HARD_CANDIDATES,
                 "and it needs at least %d, so a mid-operation cancel is real "
                 "(needs %.0f)" % (HARD_CANDIDATES, how_many)):
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


#: How many candidates an operation must need before a mid-operation cancel is a
#: real event. The check only ever asserts "at least this many"; the value is a
#: floor, not a measurement of the fixture's worst case.
HARD_CANDIDATES = 3


def _stop_when_hard(survey):
    """A `cancel_check` that ends a survey once one operation needs enough tries.

    The survey runs to answer "is there an operation whose search genuinely takes
    several candidates?". It used to answer a harder question -- *which* operation
    takes the most -- by resolving all 110 and keeping the maximum. Measured on
    the committed fixture, that is **142.8 s**, which was two thirds of a 223 s
    check and about a quarter of the whole gate; and it bought nothing, because
    the check asserts a floor of `HARD_CANDIDATES` and **24 of the 27 fixable
    operations clear it** (63, 51, 50, 22, 19, 13, ...). The maximum is not what
    makes this check meaningful; the existence of one hard operation is.

    So the survey stops as soon as it has one. `resolve_conflicts` documents
    `cancel_check` as stopping *between operations*, which is what makes this
    cheap and safe: by the time it is polled, the previous operation has finished
    and is already in `survey.fixable`, so the operation that triggered the stop
    is the one the test goes on to use. Returning False while `fixable` is empty
    lets the first operation run.

    Cancelling does leave the survey partial, which is why nothing downstream
    reads it: the check re-scans and re-prepares from `fresh` before touching the
    operation. A dry run writes nothing either way, so stopping early cannot
    leave a mark.
    """
    def check_cancel():
        for candidate in survey.fixable:
            tried = _candidates_tried(candidate.detail)
            if tried is not None and tried >= HARD_CANDIDATES:
                return True
        return False

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


#: How far to try extending `RadiusIn` past its authored value, in mm.
#:
#: The re-nested fixture has **no** lead run piercing off the sheet, where the
#: one it replaced had three, and none within a kerf of the edge either -- the
#: closest is 2.3105 mm. So both edge cases are **built** rather than found,
#: exactly as `FORCED_RADIUS_IN_MM` builds the lead-in/lead-in one.
#:
#: A fixed radius cannot express them, because "off the material" and "on the
#: material within a kerf of the edge" are two points on the same walk outwards,
#: and which one a radius lands on depends on how that operation's lead-in is
#: aimed. Measured: extending the *closest* operation by 4 mm takes its pierce
#: from 2.3105 mm to 9.3223 mm -- **further** from the edge, because that
#: lead-in points inward and a longer one reaches deeper into the nest. Of the
#: 14 runs nearest the edge, 6 point outward; the best goes from 3.2169 mm to
#: 0.3565 mm. So the radius is swept per operation rather than guessed, and the
#: direction is tested first to keep the sweep short.
FORCED_EDGE_EXTEND_MM = 40.0

#: The direction test: extend by this much and see whether the pierce point got
#: closer to the edge. One recompute per candidate, against up to 160 in the
#: sweep it decides whether to run.
FORCED_EDGE_PROBE_MM = 4.0

#: Sweep step, in mm. The window between "within a kerf of the edge" and "off
#: the sheet" is about a millimetre wide, so a coarse step would step straight
#: over the finding being looked for.
FORCED_EDGE_STEP_MM = 0.25

#: How many operations to try before giving up on finding one near the edge.
FORCED_EDGE_TRIES = 14


def _force_a_pierce_at_the_sheet_edge(job, want_off_sheet):
    """Raise `RadiusIn` until a real pierce point reaches the sheet edge.

    Returns `(dressup, saved_radius, applied_radius)` with the radius **left
    applied** at the value that produced the finding, or `(None, None, None)`.
    The caller reverts, so nothing here depends on the nest staying this tight
    or this loose.

    `want_off_sheet` picks which side of the edge to aim for: `True` for a
    pierce on air, `False` for one still on the material but within the margin.
    A candidate qualifies only when `_off_sheet` returns a finding whose sign
    matches, so the two cases cannot be satisfied by the same radius.

    Candidates are ranked nearest-the-edge-first, because a pierce that is
    already near the boundary is the cheapest to move onto it. Then each is
    **swept**, not set to a fixed radius, for the reason in
    `FORCED_EDGE_EXTEND_MM`: the lead-ins point in different directions and the
    same radius means something different to each of them.
    """
    try:
        from shapely.geometry import Point
    except ImportError:
        return None, None, None

    sheet = lead_in_audit.stock_polygon(job)
    if sheet is None:
        return None, None, None
    prepared, index, _unreadable = lead_in_audit._prepare(job, None)
    by_name = {entry[0].Name: entry for entry in prepared}

    def pierce_distance(runs):
        """How far the first run's pierce point is from the edge, or None."""
        for run in runs:
            geometry = run.geometry
            if geometry is None:
                continue
            try:
                coords = list(geometry.coords)
                pierce = coords[0] if run.kind == lead_in_audit.ROLE_LEAD_IN \
                    else coords[-1]
                return Point(pierce[0], pierce[1]).distance(sheet.boundary)
            except Exception:
                continue
        return None

    ranked = []
    seen_dressups = set()
    for dressup, operation in lead_in_audit.lead_in_out_dressups(job):
        entry = by_name.get(dressup.Name)
        if entry is None or dressup.Name in seen_dressups:
            continue
        seen_dressups.add(dressup.Name)
        try:
            distance = pierce_distance(entry[4])
        except Exception:
            continue
        if distance is not None:
            ranked.append((distance, dressup, entry))
    ranked.sort(key=lambda item: item[0])

    for distance, dressup, entry in ranked[:FORCED_EDGE_TRIES]:
        _d, op, part, margin, _runs, _body = entry
        saved = float(dressup.RadiusIn)

        def apply(radius):
            dressup.RadiusIn = radius
            dressup.recompute()
            fresh = lead_in_audit.index_after(op, dressup, part, index)
            return fresh, (lead_in_audit._off_sheet(fresh, sheet, margin)
                           if fresh else [])

        # Direction test first: only a lead-in that reaches *towards* the edge
        # can be walked onto it, and a sweep through 40 mm of the wrong
        # direction is 160 wasted recomputes.
        probe, _found = apply(saved + FORCED_EDGE_PROBE_MM)
        nearer = pierce_distance(probe) if probe else None
        dressup.RadiusIn = saved
        dressup.recompute()
        lead_in_audit.index_after(op, dressup, part, index)
        if nearer is None or nearer >= distance:
            continue

        radius = saved + FORCED_EDGE_STEP_MM
        while radius <= saved + FORCED_EDGE_EXTEND_MM:
            fresh, found = apply(radius)
            for finding in found:
                if finding.off_the_sheet is bool(want_off_sheet):
                    return dressup, saved, radius
            radius += FORCED_EDGE_STEP_MM
        dressup.RadiusIn = saved
        dressup.recompute()
        lead_in_audit.index_after(op, dressup, part, index)
    return None, None, None


def check_the_sheet_edge_is_checked(doc, job):
    """A pierce point too near the sheet edge is a different failure from a clash.

    Nothing on this fixture has a `Drilling`, and after the re-nest nothing
    pierces off the edge or lands within a kerf of it either -- the closest is
    2.3105 mm. So both cases are **built**, by raising `RadiusIn` on a real
    operation until its pierce point reaches the edge. See
    `FORCED_EDGE_EXTEND_MM` for why the radius has to be swept rather than
    chosen.

    Building them is not a workaround; it is what makes this check possible at
    all. The version that relied on the fixture supplying three offenders
    stopped running four of its five assertions when the re-nest left it with
    none, and a suite that quietly checks less is worse than a red one -- see
    `group`.
    """
    emit("")
    emit("== the sheet edge is checked, and reports what it could not check ==")
    # Its own replay. The shared `job` has had its start points moved by
    # `check_the_fix`, and the operations near the edge are among them -- so a
    # scan of the fixed job finds the sheet clean, which is the right answer
    # for a fixed job and the wrong thing to measure here.
    fresh_job = replay(FreeCAD.openDocument(FIXTURE))

    near, near_saved, near_radius = _force_a_pierce_at_the_sheet_edge(
        fresh_job, want_off_sheet=False)
    if not check(near is not None,
                 "a pierce point within a kerf of the edge was produced on real "
                 "geometry (RadiusIn %.2f mm)" % (near_radius or 0.0)):
        return
    try:
        result = lead_in_audit.scan_job(fresh_job)
        _check_a_pierce_within_the_kerf_of_the_edge(result, near)
    finally:
        near.RadiusIn = near_saved
        near.recompute()

    target, saved_radius, applied = _force_a_pierce_at_the_sheet_edge(
        fresh_job, want_off_sheet=True)
    if not check(target is not None,
                 "a pierce point off the edge was produced on real geometry "
                 "(RadiusIn %.2f mm)" % (applied or 0.0)):
        return
    try:
        result = lead_in_audit.scan_job(fresh_job)
        _check_sheet_edge(result)
        _check_the_edge_is_not_checked_when_there_is_no_stock()
    finally:
        target.RadiusIn = saved_radius
        target.recompute()


def _check_a_pierce_within_the_kerf_of_the_edge(result, target):
    """The clearance case: on the material, too close to its edge to be useful.

    This is the finding NEST-038 made expressible. The pierce is **not** off the
    sheet, so every version of this check before the margin existed reported a
    clean sheet here -- correctly, as far as it went, and uselessly.
    """
    audit = _find(result.audits, target.Name)
    if not check(audit is not None and audit.off_sheet,
                 "the scan reports it, though the pierce is on the material"):
        return
    worst = audit.worst_off_sheet
    check(worst is not None and not worst.off_the_sheet,
          "and says so: the distance is positive, %.4f mm inside the edge"
          % (worst.distance if worst else 0.0))
    said = worst.describe() if worst else ""
    check("inside the edge" in said and "off the edge" not in said,
          "and the wording does not claim it is off the sheet", said)
    check("tool radius" in said,
          "and quotes the tool radius it was measured against", said)
    check(audit.status == lead_in_audit.OperationAudit.UNRESOLVED,
          "and it is flagged for fixing, not reported clear (%s)"
          % audit.status)
    check(audit.needs_attention, "and needs_attention is true")


def _check_sheet_edge(result):
    """The assertions themselves, over whatever the scan found."""
    check(result.sheet is not None,
          "the stock outline was read from the job's own Stock")
    if result.sheet is not None:
        bounds = result.sheet.bounds
        check(abs((bounds[2] - bounds[0]) - 600.0) < 1e-6
              and abs((bounds[3] - bounds[1]) - 300.0) < 1e-6,
              "and it is 600 x 300 mm (%.1f x %.1f)"
              % (bounds[2] - bounds[0], bounds[3] - bounds[1]))

    off = [(a, o) for a in result.audits for o in (a.off_sheet or [])]
    with group(off, "lead runs reaching past the edge of the sheet") as found:
        for audit, finding in found[:3]:
            check(finding.distance < 0.0,
                  "%s: the pierce point is outside the sheet (%.4f mm)"
                  % (audit.label, finding.distance))
            check("off the edge of the sheet" in finding.describe(),
                  "%s: and the report says so (%s)"
                  % (audit.label, finding.describe()))
            # Both kinds of finding must set the status, or one of them is
            # invisible.
            check(audit.status == lead_in_audit.OperationAudit.UNRESOLVED,
                  "%s is reported as needing attention" % audit.label)
            check(audit.needs_attention,
                  "%s: needs_attention is true" % audit.label)

    lines = lead_in_audit.describe_result(result, limit=3)
    joined_lines = "\n".join(lines)
    check("pierce off the edge of the sheet" in joined_lines,
          "the report has a section for it")


def _check_the_edge_is_not_checked_when_there_is_no_stock():
    """A job whose stock cannot be read must say the check did not run.

    Reporting a clean sheet here would claim the edge is fine when nothing
    looked at it -- which is the quiet-checker failure this whole module
    exists to avoid, and the reason this half is kept even now that the first
    half builds its own case.
    """
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
    #
    # **No lead-outs are built here, unlike in
    # `check_the_census_with_built_lead_outs`.** Enabling `LeadOut` on this
    # particular dressup makes its own lead-in stop attaching to its own body
    # and the scan then reports the two as clashing, which trips the
    # own-operation check below. That is NEST-042 and it is not this check's
    # subject; the census check carries the wider lead-out coverage, and this
    # one still asserts that both of the fixture's lead-outs are indexed.
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    prepared, index, _unreadable = lead_in_audit._prepare(fresh, None)
    bodies = [e for e in index.entries if e[4] == lead_in_audit.ROLE_BODY]
    lead_ins = [e for e in index.entries if e[4] == lead_in_audit.ROLE_LEAD_IN]
    lead_outs = [e for e in index.entries if e[4] == lead_in_audit.ROLE_LEAD_OUT]
    want_outs = _lead_out_dressups(fresh)
    want_total = 110 + 142 + want_outs

    check(len(bodies) == 110, "every body is indexed (%d)" % len(bodies))
    check(len(lead_ins) == 142,
          "every lead-in is indexed (%d)" % len(lead_ins))
    check(len(lead_outs) == want_outs,
          "every lead-out that was asked for is indexed (%d, and %d dressups "
          "ask for one)" % (len(lead_outs), want_outs))
    check(len(index.entries) == want_total,
          "so the index holds %d entries in all (%d)"
          % (want_total, len(index.entries)))

    # Each run must be able to recognise its own entry, or it reports itself at a
    # distance of zero.
    seen = set()
    for dressup, op, _part, _margin, runs, _body in prepared:
        for run in runs:
            seen.add((id(op), run.run_index))
    check(len(seen) == 142 + want_outs,
          "every run carries a distinct (operation, run_index) (%d of %d)"
          % (len(seen), 142 + want_outs))

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

    # And running it again must converge rather than oscillate.
    #
    # **The property is convergence, not "one pass is enough".** A search that
    # moves an operation's lead-in can move it into a neighbour's path, and the
    # neighbour's own search is the only thing that notices -- so an operation
    # can be fixable only on a second pass, because its neighbour's final
    # position is not known until the first one ends. `reverify` catches that at
    # the end of each run rather than re-searching inside it, so the tool
    # settles at two passes and stays there.
    #
    # Asserted as "the third run moves nothing", which is the anti-oscillation
    # guarantee the original "a second run changes nothing" was reaching for.
    # Pinned on the *second* run instead, it asserted single-pass convergence,
    # which is a stronger claim than the design makes and which was false for a
    # reason worth having.
    again = lead_in_audit.scan_job(job)
    lead_in_audit.resolve_conflicts(again)
    second = again.counts().get(lead_in_audit.OperationAudit.FIXED, 0)
    emit("  second run: %s" % again.counts())
    third = lead_in_audit.scan_job(job)
    lead_in_audit.resolve_conflicts(third)
    third_fixed = third.counts().get(lead_in_audit.OperationAudit.FIXED, 0)
    check(third_fixed == 0,
          "the run converges: a third run moves nothing (%d moved, after %d on "
          "the second)" % (third_fixed, second),
          "A run that keeps moving operations is oscillating. Each pass fixes "
          "what the previous pass disturbed, which means the search is not "
          "measuring against the job as it now stands.")


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
    #
    # **The candidate budget is `MAX_CANDIDATES`, not the 8 this used to walk.**
    # Eight is 8 mm of travel along the wire, which on the current layout is not
    # enough to turn the lead-in away from `part_SimpleSpacer_1`: measured, the
    # lead-in alone first clears at candidate **20**, so an 8-candidate window
    # could only ever have read 0 of 8 and asserted its own premise was false.
    # 48 is the first stage's own ceiling, so the check now asks whether the
    # search's real budget is enough to demonstrate the property -- which is the
    # question that was actually worth asking.
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
                body, 0, origin, limit=lead_in_audit.MAX_CANDIDATES):
            if not lead_in_audit._apply_start_point(op, xy):
                continue
            # **Through `index_after`, exactly as `_conflicts_after` does it.**
            # This loop used to re-read the paths and call `conflicts_for`
            # against the index the run had built up front, which is a stale
            # index the moment a candidate moves the operation -- so the
            # candidate's own lead-in was measured against its old body, crossed
            # it away from its new attachment, and every candidate was rejected.
            # That is why this check read "0 of 8" for as long as it existed,
            # and it is the defect this file now guards rather than a property
            # of the layout. Measuring by hand instead of going through the
            # production path would have kept it passing for the wrong reason
            # again.
            fresh_runs = lead_in_audit.index_after(op, target, part, index)
            if fresh_runs is None:
                continue
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

#: `RadiusIn` the check inflates to, in mm.
#:
#: The fixture has no lead-in/lead-in clash of its own -- the nest is tight
#: enough that the lead-ins (3-4.8 mm) are longer than the gaps (2.70 mm)
#: they have to fit into, but they all come to rest on a *body*. To exercise
#: the lead-run half on real geometry, one lead-in is made long enough to span
#: the gap and land across a neighbour's lead-in.
#:
#: 30 mm is measured, not guessed: at 20 mm the inflated lead-in still stops
#: short of the neighbour's lead run, and at 30 it reaches it. It is set on a
#: dressup the user owns and reverted afterwards, so nothing here depends on
#: the nest staying this tight or this loose.
FORCED_RADIUS_IN_MM = 30.0

#: How many operations to try before giving up on finding one to inflate.
FORCED_RADIUS_TRIES = 25


def check_a_lead_in_crossing_a_lead_in_is_found_and_fixed():
    """The half of this module that had never once fired.

    A lead-in is not only a thing that reaches into other people's material --
    it is itself a cut path, and another operation's lead-in can run into it.
    On plasma or waterjet that is the worse of the two failures: reaching a
    contour re-cuts material, while crossing another lead run bridges the arc
    across a kerf it has already opened, drops the flame and errors the
    controller.

    **The fixture cannot supply this case on its own**, which is why the check
    makes one. Scanning the 54-part nest at its own tool radius finds 49
    conflicts and **none of them** against a lead run; the closest genuine pair
    of lead runs is 11 same-operation lead-in/lead-out runs touching at the
    closed loop's shared start point, which is where one run's attachment is.
    A check that has never gone off cannot be relied on to go off, so the case
    is built: `RadiusIn` is raised on a real operation until its lead-in spans
    the gap onto a neighbour's lead-in, and then the ordinary scan has to find
    it -- at the ordinary margin, with no special flag telling it to look.

    And then the fix, because detection alone is half the ask: the search has
    to move the start point until the crossing is gone, and a re-scan has to
    agree. The re-scan is not a formality -- it is `reverify` in miniature,
    and it is what would catch a fix accepted against a neighbour that had
    already moved.
    """
    emit("")
    emit("== a lead-in reaching another lead-in is found, and fixed ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    if not check(fresh is not None, "a fresh document replayed"):
        return

    survey = lead_in_audit.scan_job(fresh)
    prepared, _index, _unreadable = lead_in_audit._prepare(fresh, None)
    by_name = {entry[0].Name: entry for entry in prepared}

    # Offenders first, nearest clash first: the tightest pairs are the ones a
    # 30 mm lead-in is most likely to reach, and the walk is short.
    offenders = [a for a in survey.audits if a.conflicts]
    offenders.sort(key=lambda a: a.worst.distance)

    target = None
    inflated = None
    for audit in offenders[:FORCED_RADIUS_TRIES]:
        dressup = audit.dressup
        saved = dressup.RadiusIn
        dressup.RadiusIn = FORCED_RADIUS_IN_MM
        dressup.recompute()
        entry = by_name.get(dressup.Name)
        if entry is None:
            dressup.RadiusIn = saved
            dressup.recompute()
            continue
        _d, op, part, margin, _stale_runs, _body = entry
        # The runs `index_after` returns, not the ones `_prepare` captured
        # before the inflation. An earlier version of this check used the stale
        # list, and so reported a clash between a 3 mm lead-in and a neighbour
        # that the 30 mm one had not reached -- found, printed, and then
        # contradicted by the scan it went on to run.
        runs = lead_in_audit.index_after(op, dressup, part, _index)
        if runs is None:
            # Restored on this path too, not only on the "no clash" one: a
            # dressup left at 30 mm because we gave up reading it would go on
            # to the checks below with a lead-in it does not really have.
            dressup.RadiusIn = saved
            dressup.recompute()
            continue
        hits = [c for c in lead_in_audit.conflicts_for(runs, op, part, _index,
                                                        margin)
                if c.role != lead_in_audit.ROLE_BODY]
        if hits:
            target, inflated = dressup, saved
            emit("  forced on %s: RadiusIn %.1f -> %s"
                 % (dressup.Label, FORCED_RADIUS_IN_MM,
                    "; ".join(c.summary() for c in hits[:2])))
            break
        dressup.RadiusIn = saved
        dressup.recompute()

    if not check(target is not None,
                 "a lead-in reaching another operation's lead-in was produced "
                 "on real geometry"):
        return

    try:
        result = lead_in_audit.scan_job(fresh)
        trial = _find(result.audits, target.Name)
        if not check(trial is not None and trial.status ==
                     lead_in_audit.OperationAudit.UNRESOLVED,
                     "the ordinary scan reports it at the ordinary margin "
                     "(status %s)"
                     % (trial.status if trial else "missing")):
            return

        run_clashes = [c for c in trial.conflicts
                       if c.role != lead_in_audit.ROLE_BODY]
        check(bool(run_clashes),
              "and names it as a lead run, not a cut path (%d found, roles %s)"
              % (len(run_clashes), sorted(set(c.role for c in trial.conflicts))))
        if run_clashes:
            said = run_clashes[0].summary()
            check("cut path" not in said,
                  "the sentence does not claim it is a cut path", said)
            check("lead-in" in said or "lead-out" in said,
                  "the sentence names the lead run", said)

        report = "\n".join(lead_in_audit.describe_result(result, dry_run=True))
        check("rather than its cut path" in report,
              "and the report gives lead runs a section of their own")
        # Matched on the invariant tail, not on "1 reach"/"2 reach": the
        # headline agrees with itself about the verb, and a check pinned to one
        # plural form would fail the day the count moved from 2 to 1.
        headline = lead_in_audit.describe_result(result, dry_run=True)[0]
        check("another operation's lead-in or lead-out." in headline,
              "and the headline counts them (%d)"
              % lead_in_audit._lead_run_conflicts(result))

        # The fix, on the job as it stands.
        lead_in_audit.resolve_conflicts(result, dry_run=False,
                                        limit=lead_in_audit.MAX_CANDIDATES)
        moved = _find(result.audits, target.Name)
        check(moved is not None and moved.status !=
              lead_in_audit.OperationAudit.UNRESOLVED,
              "the search moved it clear (%s)"
              % (moved.status if moved else "missing"))

        after = lead_in_audit.scan_job(fresh)
        recheck = _find(after.audits, target.Name)
        check(recheck is not None and recheck.status ==
              lead_in_audit.OperationAudit.CLEAN,
              "and a fresh scan agrees it is clear (%s)"
              % (recheck.status if recheck else "missing"))
        check(lead_in_audit._lead_run_conflicts(after) == 0,
              "with no lead-run clash left anywhere on the sheet (%d)"
              % lead_in_audit._lead_run_conflicts(after))
    finally:
        lead_in_audit.clear_conflict(target)
        target.RadiusIn = inflated
        target.recompute()


def check_the_lead_run_count_is_always_stated(job):
    """Zero is a measurement the reader is entitled to.

    It used to be silently absent on every layout this was developed against,
    which is indistinguishable from "not checked" -- and is how a check that
    never fires survives for years.
    """
    emit("")
    emit("== the report states the lead-run count even when it is zero ==")
    survey = lead_in_audit.scan_job(job)
    lines = lead_in_audit.describe_result(survey)
    emit("  %s" % lines[0])
    check(any("another operation's lead-in or lead-out." in line
              for line in lines),
          "the headline says how many lead runs were reached (%d)"
          % lead_in_audit._lead_run_conflicts(survey))


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
    check_a_lead_in_crossing_a_lead_in_is_found_and_fixed()
    check_the_lead_run_count_is_always_stated(job)
    # These read the geometry itself rather than a report, and they must run
    # before anything mutates the shared `job` -- `check_the_fix` above has
    # already moved start points on it.
    check_own_body_is_not_drawn_across_the_gap_between_wires(doc, job)
    check_lead_runs_are_indexed_too(doc, job)
    check_the_census_with_built_lead_outs(doc)
    check_the_sheet_edge_is_checked(doc, job)
    check_command_is_importable()

    return finish()


# -- the census, with lead-outs the suite built ---------------------------

def check_the_census_with_built_lead_outs(doc):
    """The same census as `check_scan`, on a job the suite gave more lead-outs.

    `check_scan` reads the shared job, which several later checks depend on and
    which `check_lead_out_is_judged_too` already mutates, so it cannot be
    touched here. This is the same count on its own replay with six lead-outs
    turned on -- which is the point: the fixture now carries two, and a reader
    that silently loses *some* lead-outs would pass on two and fail on eight.

    The lead-in count is asserted again, unchanged, because turning a lead-out on
    has to add a lead-out and nothing else.
    """
    emit("")
    emit("== the census holds up with lead-outs the suite built ==")
    fresh = replay(FreeCAD.openDocument(FIXTURE))
    if not check(fresh is not None, "the fixture replays a second time"):
        return

    had = _lead_out_dressups(fresh)
    built = _enable_lead_outs(fresh)
    now = _lead_out_dressups(fresh)
    emit("   the fixture asks for %d lead-out(s); this turned %d more on"
         % (had, built))
    check(now == had + built,
          "turning %d lead-out(s) on left %d asking for one" % (built, now))

    result = lead_in_audit.scan_job(fresh)
    lead_ins = sum(1 for a in result.audits for r in a.lead_runs
                   if r.kind == "lead-in")
    lead_outs = sum(1 for a in result.audits for r in a.lead_runs
                    if r.kind == "lead-out")
    check(lead_ins == 142,
          "the built lead-outs did not disturb the lead-ins (%d of 142)"
          % lead_ins)
    check(lead_outs == now,
          "all %d of them were found (%d)" % (now, lead_outs))
    check(now > had,
          "this check is pointless as written: the fixture already asks for %d "
          "lead-outs, so nothing was actually added" % had)


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
