"""Tests for the lead-in check's progress display.

Background
----------
The audit's *scan* is 72-96 ms over 98 operations and needs no feedback. Its
*search* is a FreeCAD recompute per candidate at about 22 ms, bounded at 48
candidates per operation -- so one operation is 0.08 s when the first candidate
works and about a second when none does, and a sheet with many offenders is tens
of seconds of a frozen GUI with no way to stop it.

`LeadInProgressView` is pure and is what these tests cover, for the same reason
`ReplayProgressView` is: a progress bar cannot be exercised headless, and one
that has only ever been looked at once is indistinguishable from one that does
not work.

What is asserted, and why each matters
--------------------------------------
* **The bar counts candidates, not operations.** The committed fixture's single
  real offender is fixed on the *first* candidate, so an operations-based bar
  sits still for a whole second and then jumps to 100% -- which reads as a hung
  program rather than a fast one.
* **The candidate count resets per operation**, so 0-48 does not run five times
  over with no indication that the operation changed.
* **Cancel is latched.** A click that lands while a widget is being destroyed
  must not be un-done by the next poll reading a torn-down object.
* **A report-only run never says it fixed anything.** "3 fixed" on a run that
  changed no properties is a lie about the document.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam.lead_in_progress import (
    LeadInProgressView,
    _NO_COUNT,
)


class _Clock:
    """A clock the test sets by hand.

    A scripted iterator has to be counted to predict its last value, and the
    first version of these tests did that and was off by one -- which reads as
    the display being wrong rather than the test.
    """

    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


def _view(clock=None, **kwargs):
    return LeadInProgressView(clock=clock or _Clock(), **kwargs)


# -- the bar --------------------------------------------------------------

def test_the_bar_starts_empty():
    view = _view()
    assert view.bar_range() == (0, 0)


def test_the_bar_counts_candidates_not_operations():
    """Candidate 3 of 48, with operation 1 of 5 alongside.

    The fixture's real offender is fixed on candidate 1, so a bar that only
    knows about operations would read 0% then 100% with nothing in between.
    """
    view = _view(total_operations=5)
    view.update(operation_index=1, operation_total=5, label="part_Bracket_2")
    view.update(candidates=3, candidate_total=48)
    assert view.bar_range() == (48, 3)
    assert "candidate 3 of 48" in view.detail_text()
    assert "operation 1 of 5" in view.detail_text()


def test_the_candidate_count_resets_when_the_operation_changes():
    """Otherwise 0-48 runs five times with no sign the operation changed."""
    view = _view(total_operations=3)
    view.update(operation_index=1, operation_total=3, label="first")
    view.update(candidates=48, candidate_total=48)
    assert view.bar_range() == (48, 48)
    view.update(operation_index=2, operation_total=3, label="second")
    view.update(candidates=1, candidate_total=48)
    assert view.bar_range() == (48, 1)


def test_the_same_operation_reported_again_does_not_reset():
    """`resolve_conflicts` reports an operation, then `resolve_audit` reports
    its candidates. The second must not wipe the operation's own index."""
    view = _view(total_operations=2)
    view.update(operation_index=2, operation_total=2, label="second")
    view.update(candidates=5, candidate_total=48)
    view.update(candidates=6, candidate_total=48)
    assert view.bar_range() == (48, 6)
    assert "operation 2 of 2" in view.detail_text()


def test_the_bar_never_exceeds_its_maximum():
    view = _view()
    view.update(candidates=999, candidate_total=48)
    assert view.bar_range() == (48, 48)


def test_an_unknown_candidate_budget_gives_the_marquee():
    """`(0, 0)` is Qt's busy indicator, and the honest rendering of work that
    cannot say where it is."""
    view = _view(total_operations=4)
    view.update(operation_index=1, operation_total=4, label="part_A")
    assert view.bar_range() == (0, 0)
    assert _NO_COUNT in view.detail_text()


# -- the tally ------------------------------------------------------------

def test_a_report_only_run_never_says_it_fixed_anything():
    """"3 fixed" on a run that changed no properties is a lie."""
    view = _view(dry_run=True, total_operations=3)
    view.update(operation_index=1, operation_total=3, label="a")
    view.update(candidates=1, candidate_total=48, outcome="fixable")
    view.update(operation_index=2, operation_total=3, label="b")
    view.update(candidates=4, candidate_total=48, outcome="fixable")
    tally = view.tally_text()
    assert "2 could be fixed" in tally
    assert "fixed" not in tally.replace("could be fixed", "")


def test_an_applying_run_says_fixed():
    view = _view(total_operations=3)
    view.update(candidates=1, candidate_total=48, outcome="fixed")
    assert "1 fixed" in view.tally_text()


def test_the_tally_separates_unfixable_from_unchecked():
    """Three different things, and conflating any two of them misleads."""
    view = _view(total_operations=4)
    view.update(candidates=1, candidate_total=48, outcome="fixed")
    view.update(candidates=48, candidate_total=48, outcome="unresolved")
    view.update(candidates=2, candidate_total=48, outcome="skipped")
    tally = view.tally_text()
    assert "1 fixed" in tally
    assert "1 need doing by hand" in tally
    assert "1 not looked at" in tally


def test_the_tally_starts_empty_rather_than_saying_zero_needs_doing():
    """Before anything is found, it says nothing about needing doing."""
    view = _view(total_operations=10)
    assert view.tally_text() == "0 fixed"


# -- the headline ---------------------------------------------------------

def test_the_headline_names_the_job():
    """FreeCAD's Tasks panel shows several at once."""
    view = _view(subject="Job_Replay_Sheet_1")
    assert "Job_Replay_Sheet_1" in view.stage_text()


def test_the_headline_says_what_it_is_doing():
    view = _view(subject="Job")
    assert "Fixing lead-ins" in view.stage_text()


def test_a_report_only_headline_says_so():
    """So nobody reads a report as though the job was changed."""
    view = _view(subject="Job", dry_run=True)
    assert "report only" in view.stage_text()
    assert "Checking lead-ins" in view.stage_text()


def test_the_headline_names_the_operation_being_worked_on():
    view = _view(subject="Job", total_operations=2)
    view.update(operation_index=2, operation_total=2,
                label="DressupLeadInOut001_replay_nested_TopStrap_14")
    assert "TopStrap_14" in view.stage_text()


# -- cancelling -----------------------------------------------------------

def test_cancelling_is_latched():
    """A click that lands while the widget is destroyed must not be undone."""
    view = _view(total_operations=2)
    view.update(operation_index=1, operation_total=2, label="a")
    view.mark_cancelled()
    assert view.cancelled is True
    # Further updates do not clear it.
    view.update(candidates=2, candidate_total=48)
    assert view.cancelled is True
    assert "Cancelling" in view.detail_text()


def test_cancelling_says_it_is_finishing_the_current_candidate():
    """Not "stopping": the search cannot be interrupted mid-recompute, and a
    panel claiming otherwise sets up a wait that then seems to hang."""
    view = _view()
    view.mark_cancelled()
    assert "finishing the current candidate" in view.detail_text()


# -- the clock ------------------------------------------------------------

def test_the_clock_counts_up():
    clock = _Clock()
    view = _view(clock=clock)
    assert view.elapsed_text() == "0.0s elapsed"
    clock.value = 0.5
    view.update(candidates=1, candidate_total=48)
    assert view.elapsed_text() == "0.5s elapsed"


def test_the_clock_switches_to_minutes():
    clock = _Clock()
    view = _view(clock=clock)
    clock.value = 61.0
    view.update(candidates=1, candidate_total=48)
    assert view.elapsed_text() == "1m 01s elapsed"


def test_a_clock_that_goes_backwards_does_not_show_negative_time():
    """A negative elapsed time reads as a broken display rather than a
    measurement nobody can take."""
    values = iter([10.0, 5.0])
    view = LeadInProgressView(clock=lambda: next(values))
    view.update(candidates=1, candidate_total=48)
    assert not view.elapsed_text().startswith("-")


# -- before anything has happened -----------------------------------------

def test_it_says_starting_before_the_first_operation():
    view = _view(total_operations=4)
    assert view.detail_text() == "Starting."
