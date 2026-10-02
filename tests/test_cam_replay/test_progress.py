"""Tests for the replay's progress reporting.

Background
----------
A replay is not quick: `replay_layout` measures **43 seconds** on the committed
fixture for one sheet of 48 parts and 7 operations, and it loops over sheets,
so a three-sheet nest is over two minutes of a frozen UI. `Progress` is the
seam a dialog, a Tasks-panel widget or a bare Console line hangs off.

Three properties are pinned here, in order of how much they would hurt if they
broke:

  * **A dead widget must not cost the run.** The callback is supplied by
    whoever is drawing, and the drawing can go away mid-run -- that is what the
    nester's `except RuntimeError` exists for. A raised callback here would
    abort a 43-second CAM job.

  * **The counts must be true.** A progress bar that lies is worse than no
    bar, which is why the stage is named and the count is *within* it rather
    than one weighted percentage across nine stages of different cost.

  * **`None` must change nothing.** Every existing caller, and the whole
    harness, runs with no callback.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import (
    Progress,
    describe_timings,
)


class _Sink:
    """Records every event the reporter emits."""

    def __init__(self):
        self.events = []

    def __call__(self, stage, current, total, message=None):
        self.events.append((stage, current, total, message))

    @property
    def stages(self):
        return [e[0] for e in self.events]

    def items(self, stage):
        return [e for e in self.events if e[0] == stage]


# -- absence --------------------------------------------------------------

class TestNoCallback:
    def test_every_method_is_a_no_op(self):
        # The shape a call site actually uses, so a typo here is caught here
        # rather than in the 43-second run.
        p = Progress()
        p.stage("a")
        p.item(1, 2, "x")
        p.done("y")

    def test_tolerates_no_prefix(self):
        Progress().stage("a")

    def test_tolerates_none_detail(self):
        Progress(None).item(1, 1)

    def test_is_what_the_harness_uses(self):
        # `replay_layout`'s default must stay None or the harness would be
        # building a reporter on every call for nothing.
        import inspect
        params = inspect.signature(cam_replay.replay_layout).parameters
        assert "progress_callback" in params
        assert params["progress_callback"].default is None


# -- the happy path -------------------------------------------------------

class TestEmission:
    def test_a_stage_reports_zero_of_zero(self):
        # Nothing has been counted yet, and claiming otherwise would mean
        # inventing a denominator for the stage.
        sink = _Sink()
        Progress(sink).stage("Flattening")
        assert sink.events == [("Flattening", 0, 0, "Flattening")]

    def test_items_carry_the_stage_and_the_count(self):
        sink = _Sink()
        p = Progress(sink)
        p.stage("Flattening")
        p.item(23, 48, "nested_Bracket_23")
        assert sink.events[-1] == ("Flattening", 23, 48, "nested_Bracket_23")

    def test_the_prefix_prepends_the_message(self):
        sink = _Sink()
        p = Progress(sink, prefix="Sheet_2")
        p.stage("Flattening")
        p.item(1, 2, "nested_a")
        assert sink.events[-1][3] == "Sheet_2 nested_a"

    def test_the_prefix_does_not_disturb_the_count(self):
        sink = _Sink()
        p = Progress(sink, prefix="Sheet_2")
        p.stage("Verifying")
        p.item(7, 7)
        assert sink.events[-1][1:3] == (7, 7)

    def test_done_closes_the_stage(self):
        sink = _Sink()
        p = Progress(sink)
        p.stage("Verifying")
        p.done("verified 7 operation(s)")
        assert sink.events[-1][3] == "verified 7 operation(s)"

    def test_a_message_is_always_a_string(self):
        # A widget doing setFormat() with None would raise, and that raise
        # would then be attributed to the replay.
        sink = _Sink()
        p = Progress(sink)
        p.stage("a")
        p.item(1, 1)
        p.done()
        assert all(isinstance(e[3], str) for e in sink.events)

    def test_stages_are_announced_in_pipeline_order(self):
        sink = _Sink()
        p = Progress(sink)
        for name in ("Reading", "Flattening", "Building", "Verifying"):
            p.stage(name)
        assert sink.stages == ["Reading", "Flattening", "Building", "Verifying"]


# -- a callback that breaks -----------------------------------------------

class TestABrokenCallback:
    """The run must survive. This is the property that matters most here."""

    def test_a_raising_callback_does_not_propagate(self):
        def boom(*_):
            raise RuntimeError("wrapped object has been deleted")

        p = Progress(boom)
        p.stage("a")
        p.item(1, 1, "x")
        p.done("y")

    def test_it_is_tried_once_then_goes_quiet(self):
        # Forty-eight parts would otherwise produce forty-eight warnings, and
        # the second one costs the reader more than it tells them. The stage
        # call is the one that raises here, so nothing after it is attempted.
        calls = []

        def boom(*_):
            calls.append(1)
            raise RuntimeError("gone")

        p = Progress(boom)
        p.stage("a")
        for i in range(48):
            p.item(i + 1, 48, "part")
        p.done("y")
        assert len(calls) == 1

    def test_it_is_tried_once_when_the_stage_survives(self):
        # The same, for a widget that dies on the first *item* rather than on
        # the stage. Two attempts: the stage, then the item that failed.
        calls = []

        def boom(*_):
            calls.append(1)
            raise RuntimeError("gone")

        p = Progress(boom)
        p.stage("a")          # this one raises too
        assert len(calls) == 1
        p = Progress(boom)
        p._failed = False     # stand in for a stage that was drawn fine
        p.item(1, 48, "part")
        p.item(2, 48, "part")
        assert len(calls) == 2

    def test_a_non_runtime_error_is_also_survived(self):
        # Not just the widget-deleted case. Any bug in whatever is drawing
        # should cost the drawing, not the CAM job.
        def boom(*_):
            raise ZeroDivisionError("a bug in the progress widget")

        p = Progress(boom)
        p.stage("a")
        p.item(1, 1)

    def test_it_records_why_it_stopped(self):
        # Observable without a real FreeCAD, and a UI can put it in its log
        # rather than the user having to go hunting in the Report view.
        p = Progress(lambda *a: (_ for _ in ()).throw(RuntimeError("gone")))
        p.stage("a")
        assert p.warning is not None
        assert "RuntimeError" in p.warning

    def test_it_starts_with_nothing_to_report(self):
        assert Progress().warning is None

    def test_a_still_working_run_is_not_silenced(self):
        # One sheet's callback dying must not silence the sheets after it.
        # `replay_layout` builds a fresh `Progress` per sheet, so the next
        # sheet gets a reporter that has not given up -- verified here by
        # driving it the way `replay_layout` does, one object per sheet.
        seen = []

        def flaky(stage, current, total, message=None):
            seen.append((sheet, stage))
            if sheet == 1:
                raise RuntimeError("sheet 1's widget died")

        for sheet in (1, 2):
            Progress(flaky, prefix="Sheet_%d" % sheet).stage("Verifying")

        assert [s for s, _ in seen] == [1, 2]
        assert seen[-1] == (2, "Verifying")

    def test_a_given_up_reporter_stays_given_up(self):
        # The complement: stickiness is per object, so a long sheet cannot
        # start shouting again halfway through.
        calls = []
        p = Progress(lambda *a: calls.append(1) or (_ for _ in ()).throw(
            RuntimeError("gone")))
        p.stage("a")
        p.stage("b")
        p.stage("c")
        assert len(calls) == 1


# -- honesty --------------------------------------------------------------

class TestTheCountsAreTrue:
    """The whole reason the callback takes a stage name and a within-stage
    count rather than one number. A stage announced with a count it cannot
    support would be a bar that lies."""

    def test_a_stage_with_no_items_reports_no_count_on_close(self):
        # `create_replay_job` is one indivisible call. There is no count for it
        # and pretending otherwise would mean making one up. The stage itself
        # opens at 0 of 0, which is the honest "nothing counted yet".
        sink = _Sink()
        p = Progress(sink)
        p.stage("Building the replay job")
        p.done()
        events = sink.items("Building the replay job")
        assert events[0][1:3] == (0, 0)
        assert events[-1][1:3] == (None, None)

    def test_counts_never_exceed_their_total(self):
        # The call sites are responsible for this, but the property is worth
        # pinning because a reversed pair would render as a bar over 100%.
        sink = _Sink()
        p = Progress(sink)
        p.stage("Flattening")
        for i in range(1, 49):
            p.item(i, 48, "part")
        assert max(e[1] for e in sink.items("Flattening")) == 48
        assert max(e[2] for e in sink.items("Flattening")) == 48

    def test_a_zero_total_does_not_divide_by_zero(self):
        # What a UI does with `current/total` is its business, but emitting it
        # must not blow up on the way out.
        sink = _Sink()
        p = Progress(sink)
        p.stage("nothing to do")
        p.item(0, 0)
        assert sink.events[-1][1:3] == (0, 0)


class _Clock:
    """A clock that only moves when told to.

    Timings are the one thing here that cannot be asserted on with a real
    clock: a test that sleeps is a test that is slow and flaky, and one that
    tolerates a tolerance is one that will pass when the thing it guards is
    broken. So `Progress` takes the clock as an argument.
    """

    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class TestTimings:
    def test_each_stage_is_banked_when_the_next_one_starts(self):
        clock = _Clock()
        p = Progress(clock=clock)
        p.stage("first")
        clock.advance(2.0)
        p.stage("second")
        clock.advance(3.0)

        assert p.timing_rows() == [("first", 2.0, 1), ("second", 3.0, 1)]
        assert p.elapsed == 5.0

    def test_the_running_stage_is_banked_only_once(self):
        # `timing_rows()` closes the last stage, so reading the timings twice
        # must not double-count it. It also must not zero the running stage
        # out from under the caller.
        clock = _Clock()
        p = Progress(clock=clock)
        p.stage("only")
        clock.advance(4.0)

        assert p.timing_rows() == [("only", 4.0, 1)]
        clock.advance(4.0)
        assert p.timing_rows() == [("only", 4.0, 1)]
        assert p.elapsed == 4.0

    def test_finish_is_idempotent(self):
        clock = _Clock()
        p = Progress(clock=clock)
        p.stage("work")
        clock.advance(1.0)
        p.finish()
        p.finish()
        assert p.elapsed == 1.0

    def test_a_repeated_stage_is_merged_and_counted(self):
        # A multi-sheet run enters the same stage name once per sheet. Six
        # rows per stage per sheet would be unreadable in a report; six rows
        # with a count is one table for the whole run.
        clock = _Clock()
        p = Progress(clock=clock)
        p.stage("Reading")
        clock.advance(1.0)
        p.stage("Other")
        clock.advance(1.0)
        p.stage("Reading")
        clock.advance(2.0)

        rows = dict((name, (secs, runs)) for name, secs, runs in p.timing_rows())
        assert rows == {"Reading": (3.0, 2), "Other": (1.0, 1)}

    def test_events_do_not_change_the_timings(self):
        # `item()` counts events but is not a stage boundary. If an event were
        # treated as one, a stage with 48 parts would be split into 48 rows.
        clock = _Clock()
        p = Progress(clock=clock)
        p.stage("Flattening")
        for _ in range(48):
            p.item(1, 48, "part")
        clock.advance(0.5)
        p.stage("next")
        clock.advance(0.5)

        assert p.timing_rows() == [("Flattening", 0.5, 1), ("next", 0.5, 1)]

    def test_no_progress_callback_still_timings(self):
        # Timings are not a side effect of there being a widget to feed.
        clock = _Clock()
        p = Progress(clock=clock)
        p.stage("work")
        clock.advance(2.0)
        p.finish()
        assert p.timing_rows() == [("work", 2.0, 1)]

    def test_a_dead_callback_does_not_stop_the_timings(self):
        # The seam gives up after a callback raises, so that a broken widget
        # does not cost the run. The timings must survive that: they are what
        # says the run was slow, and losing them would hide exactly the case
        # where a UI was misbehaving.
        clock = _Clock()

        def boom(*_a):
            raise RuntimeError("no widget")

        p = Progress(boom, clock=clock)
        p.stage("work")
        clock.advance(3.0)
        p.finish()
        assert p.timing_rows() == [("work", 3.0, 1)]
        assert p.warning is not None


class TestDescribeTimings:
    def _rows(self):
        return [("Ordering", 4.0, 1), ("Replaying", 3.0, 2), ("Reading", 1.0, 1)]

    def test_largest_first(self):
        # The table answers "where did the time go", and that is a ranking
        # question. Pipeline order is the progress bar's business.
        text = "\n".join(describe_timings(self._rows()))
        assert text.index("Ordering") < text.index("Replaying")
        assert text.index("Replaying") < text.index("Reading")

    def test_repeats_are_shown(self):
        text = "\n".join(describe_timings(self._rows()))
        assert "(x2)" in text
        assert "(x1)" not in text

    def test_shares_are_reported_against_the_accounted_total(self):
        text = "\n".join(describe_timings(self._rows()))
        assert "50.0%" in text      # 4 of 8
        assert "12.5%" in text      # 1 of 8

    def test_the_unaccounted_gap_is_shown_when_it_matters(self):
        # Time spent outside any stage is still time the operator spent. A
        # table that quietly drops it points at the wrong stage, which is what
        # the event-delta table did -- it lost 6.2s of a 10.4s run.
        text = "\n".join(describe_timings(self._rows(), timed_seconds=8.0,
                                         wall_clock=10.0))
        assert "unaccounted" in text
        assert "outside any stage" in text
        assert "2.00s" in text

    def test_no_gap_line_when_the_stages_account_for_it_all(self):
        text = "\n".join(describe_timings(self._rows(), timed_seconds=8.0,
                                         wall_clock=8.02))
        assert "unaccounted" not in text

    def test_nothing_to_report_gives_nothing(self):
        assert describe_timings([]) == []

    def test_a_single_zero_stage_does_not_divide_by_zero(self):
        text = "\n".join(describe_timings([("Idle", 0.0, 1)]))
        assert "Idle" in text
