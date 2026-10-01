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
from freecad.nestingworkbench.Tools.Cam.cam_replay import Progress


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
