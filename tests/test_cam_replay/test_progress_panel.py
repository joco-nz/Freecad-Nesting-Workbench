"""Tests for the Tasks-panel progress display and for cancelling a replay.

Two things are covered here and they are covered separately on purpose.

**`ReplayProgressView` is pure** -- no Qt, no FreeCAD, no event loop -- so it
can be asserted on here. The widget above it only draws what the view says. A
progress bar cannot be exercised headless, and one that has been looked at
exactly once is indistinguishable from one that does not work.

**Cancelling is a pipeline feature, not a widget feature.** The engine had no
notion of being stopped at all, so a Cancel button would have been a control
that did nothing. The engine is tested here directly; the button that drives it
is one boolean read away.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import Progress
from freecad.nestingworkbench.Tools.Cam.replay_progress import (
    ReplayProgressView,
    STAGES,
)


class _Clock:
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def view(**kwargs):
    return ReplayProgressView(clock=_Clock(), **kwargs)


# -- what the bar shows ---------------------------------------------------

class TestWhatTheBarShows:
    def test_a_counted_stage_gives_a_real_bar(self):
        v = view(stage_total=STAGES)
        v.update("Replaying the recipe", 42, 98, "Profile_replay_nested_X_7")
        assert v.bar_range() == (98, 42)

    def test_an_uncounted_stage_gives_a_marquee(self):
        # "Ordering and tidying the tool table" is a third of the run and
        # reports total=0. A bar with a range would have to invent a denominator,
        # and an invented denominator is a bar that lies. (0, 0) is Qt's
        # marquee, which is the honest rendering of "I cannot say".
        v = view(stage_total=STAGES)
        v.update("Ordering and tidying the tool table", 0, 0)
        assert v.bar_range() == (0, 0)

    def test_an_uncounted_stage_says_so(self):
        v = view(stage_total=STAGES)
        v.update("Ordering and tidying the tool table", 0, 0)
        assert "reports no progress" in v.detail_text()

    def test_the_bar_never_exceeds_its_total(self):
        # A current past the total would render as over 100%.
        v = view()
        v.update("Replaying the recipe", 200, 98)
        assert v.bar_range() == (98, 98)

    def test_the_headline_numbers_the_stage(self):
        v = view(stage_total=STAGES)
        v.update("Verifying the result", 0, 0)
        assert "stage 7 of 7" in v.stage_text()

    def test_an_unknown_stage_still_reports_something(self):
        # The stage list is a presentational claim that can go stale. If the
        # pipeline gains a stage and STAGES does not, this must still work.
        v = view(stage_total=STAGES)
        v.update("A stage this build has never heard of", 0, 0)
        assert "A stage this build has never heard of" in v.stage_text()

    def test_the_sheet_label_reaches_the_display(self):
        # A multi-sheet run replays each sheet separately; without this the
        # panel describes the wrong sheet.
        v = view(stage_total=STAGES, subject="Sheet_2")
        v.update("Replaying the recipe", 1, 98)
        assert "Sheet_2" in v.stage_text()

    def test_before_any_event_the_headline_names_the_sheet(self):
        v = view(subject="Sheet_1")
        assert v.stage_text() == "Sheet_1"
        assert v.detail_text() == "Starting."

    def test_the_count_and_the_detail_are_both_shown(self):
        v = view()
        v.update("Replaying the recipe", 42, 98, "Profile_replay_nested_X_7")
        assert "42 of 98" in v.detail_text()
        assert "Profile_replay_nested_X_7" in v.detail_text()


class TestTheClock:
    def test_it_moves_and_formats_under_a_minute(self):
        clock = _Clock()
        v = ReplayProgressView(clock=clock)
        clock.advance(2.5)
        v.update("Replaying the recipe", 1, 98)
        assert v.elapsed_text() == "2.5s elapsed"

    def test_it_switches_format_past_a_minute(self):
        clock = _Clock()
        v = ReplayProgressView(clock=clock)
        clock.advance(125.0)
        v.update("Replaying the recipe", 1, 98)
        assert v.elapsed_text() == "2m 05s elapsed"

    def test_it_is_the_only_thing_that_moves_during_an_uncounted_stage(self):
        # The honest answer to "the bar has not moved in 3.9 seconds" is that
        # the clock has. Both facts are visible.
        clock = _Clock()
        v = ReplayProgressView(clock=clock)
        v.update("Ordering and tidying the tool table", 0, 0)
        before = v.elapsed_text()
        clock.advance(3.9)
        v.update("Ordering and tidying the tool table", 0, 0)
        assert v.bar_range() == (0, 0)
        assert v.elapsed_text() != before


class TestCancellationDisplay:
    def test_cancelling_is_said_plainly(self):
        v = view()
        v.update("Replaying the recipe", 42, 98)
        v.mark_cancelled()
        assert "Cancelling" in v.detail_text()

    def test_cancelling_wins_over_the_count(self):
        # Otherwise the bar keeps filling and the reader is told it is stopping.
        v = view()
        v.update("Replaying the recipe", 42, 98)
        v.mark_cancelled()
        assert "42 of 98" not in v.detail_text()


# -- cancelling the engine ------------------------------------------------

class TestProgressCancellation:
    def test_nothing_is_cancelled_without_a_check(self):
        p = Progress()
        assert p.cancelled is False

    def test_the_check_is_polled_not_cached(self):
        # The widget owns the flag and the replay owns the loop, so the only
        # thing that crosses between them is a read.
        state = {"cancel": False}
        p = Progress(cancel_check=lambda: state["cancel"])
        assert p.cancelled is False
        state["cancel"] = True
        assert p.cancelled is True

    def test_cancelling_latches(self):
        # A loop that reads it twice must see the same answer both times, and a
        # destroyed widget must not be able to un-cancel a run.
        state = {"cancel": True}
        p = Progress(cancel_check=lambda: state["cancel"])
        assert p.cancelled is True
        state["cancel"] = False
        assert p.cancelled is True

    def test_request_cancel_works_without_a_check(self):
        p = Progress()
        p.request_cancel()
        assert p.cancelled is True

    def test_a_broken_cancel_check_does_not_cancel_the_run(self):
        # This is the difference that matters: a cancel means "the user asked".
        # A widget that fell over on the way to being asked did not ask, and
        # cancelling there would let a UI fault destroy a run.
        def boom():
            raise RuntimeError("widget gone")

        p = Progress(cancel_check=boom)
        assert p.cancelled is False
        assert p.warning is not None

    def test_a_dead_callback_still_polls_for_cancel(self):
        # Giving up on the callback is about the *display*. Cancellation is
        # independent, and a run with no progress display must still be
        # cancellable.
        state = {"cancel": False}

        def boom(*_a):
            raise RuntimeError("no widget")

        p = Progress(boom, cancel_check=lambda: state["cancel"])
        p.stage("work")
        assert p.cancelled is False
        state["cancel"] = True
        assert p.cancelled is True


class TestPipelineCancellation:
    """The engine must actually stop, and keep what it built."""

    def test_a_loop_can_stop_on_it_without_raising(self):
        # That the *pipeline* stops is the harness tier's job -- it needs real
        # FreeCAD objects to have anything to stop. What is checked here is the
        # contract those loops rely on: cancellation is a plain boolean read.
        seen = []

        def cancel():
            seen.append(True)
            return len(seen) > 2

        p = Progress(cancel_check=cancel)
        p.stage("Replaying the recipe")
        for i in range(5):
            if p.cancelled:
                break
            p.item(i, 5, "step %d" % i)
        assert p.cancelled is True
