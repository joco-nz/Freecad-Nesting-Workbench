"""Unit tests for the harness's statistics helpers.

These run in plain CPython, no FreeCAD. `harness_common` installs inert
stand-ins for the FreeCAD modules precisely so it can be imported here -- so the
logic that decides *whether a benchmark run is comparable* is covered by the
fast test suite rather than only by an expensive freecadcmd run.

What is being tested is a gate, so the cases that matter are the ones where the
gate must say no.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "freecad_harness"))

import harness_common as hc  # noqa: E402


class TestTimingKey:
    @pytest.mark.parametrize("key", [
        "wall_seconds",
        "nfp_phase_union_ms",
        "nfp_phase_union_ms_worst",
        "nfp_phase_assemble_ms_worst",
        "nfp_total_s",
        "nfp_convex_sum_s",
    ])
    def test_timings_recognised(self, key):
        assert hc.is_timing_key(key) is True

    @pytest.mark.parametrize("key", [
        "nfp_convex_pairs",
        "nfp_work_parts_a",
        "placed",
        "sheets",
        "layout_evaluations",
        "mask_hole_rings",
        "work_counts_exact",
        "reps",
    ])
    def test_counts_recognised(self, key):
        assert hc.is_timing_key(key) is False

    def test_worst_suffix_is_a_timing(self):
        """Regression: `*_ms_worst` used to be reported as a work count.

        The phase accumulator's worst-single-NFP figures do not end in `_ms`, so
        the old `endswith("_ms")` rule filed them under "reproducible counts that
        changed" -- reporting +309% on a timing as though it were deterministic.
        """
        assert hc.is_timing_key("nfp_phase_prepare_ms_worst") is True

    def test_wall_seconds_is_a_timing(self):
        """Regression: `wall_seconds` ends in `_seconds`, not `_s`.

        Caught by this file, before it could bite. It is stored at the top level
        of a record rather than inside a perf dict, so nothing routed it through
        this classifier yet -- but a wall clock filed as a deterministic count is
        the same mistake as the one above, waiting for the first caller.
        """
        assert hc.is_timing_key("wall_seconds") is True
        assert hc.is_timing_key("wall_min") is False


class TestClassifyStability:
    def test_all_stable(self):
        perfs = [{"a": 1, "t_ms": 5.0}, {"a": 1, "t_ms": 6.0}]
        results = [{"sheets": 1, "placed": 18}, {"sheets": 1, "placed": 18}]
        counts, timings, result_keys = hc.classify_stability(perfs, results)
        assert counts == []
        assert result_keys == []

    def test_timing_drift_is_not_a_count(self):
        """Timings are expected to drift; that must not read as instability."""
        perfs = [{"t_ms": 5.0, "n": 7}, {"t_ms": 9.0, "n": 7}]
        counts, timings, _ = hc.classify_stability(perfs, [{"s": 1}, {"s": 1}])
        assert counts == []
        assert timings == ["t_ms"]

    def test_count_drift_is_caught(self):
        """The case that matters: a work count that should be exact moved."""
        perfs = [{"nfp_convex_pairs": 3332}, {"nfp_convex_pairs": 3377}]
        counts, timings, _ = hc.classify_stability(perfs, [{"s": 1}, {"s": 1}])
        assert counts == ["nfp_convex_pairs"]
        assert timings == []

    def test_result_drift_is_caught(self):
        perfs = [{"n": 1}, {"n": 1}]
        results = [{"sheets": 1, "placed": 18}, {"sheets": 1, "placed": 17}]
        _, _, result_keys = hc.classify_stability(perfs, results)
        assert result_keys == ["placed"]

    def test_unhashable_result_field(self):
        """placed_by_label is a dict, so it cannot go in a set directly."""
        perfs = [{"n": 1}, {"n": 1}]
        results = [
            {"placed_by_label": {"Rect0": 3, "LShape": 3}},
            {"placed_by_label": {"Rect0": 3, "LShape": 3}},
        ]
        _, _, result_keys = hc.classify_stability(perfs, results)
        assert result_keys == []

    def test_unhashable_result_field_detects_real_change(self):
        perfs = [{"n": 1}, {"n": 1}]
        results = [
            {"placed_by_label": {"Rect0": 3}},
            {"placed_by_label": {"Rect0": 2}},
        ]
        _, _, result_keys = hc.classify_stability(perfs, results)
        assert result_keys == ["placed_by_label"]


class TestCollapse:
    def test_timings_take_the_minimum(self):
        folded = hc.collapse([{"t_ms": 9.0, "t2_ms": 4.0},
                              {"t_ms": 5.0, "t2_ms": 7.0},
                              {"t_ms": 6.0, "t2_ms": 6.0}])
        assert folded == {"t_ms": 5.0, "t2_ms": 4.0}

    def test_counts_take_one_value(self):
        folded = hc.collapse([{"n": 3332}, {"n": 3332}, {"n": 3332}])
        assert folded == {"n": 3332}

    def test_a_varying_count_is_never_averaged(self):
        """Regression: the mean of a work count is not a count.

        Averaging a count that should be deterministic is how a real regression
        gets smoothed into a plausible-looking number. `collapse` takes the first
        value; `classify_stability` is what flags it, and main() turns that into
        a hard failure. Neither step may quietly produce a mean.
        """
        samples = [{"n": 10}, {"n": 20}, {"n": 30}]
        assert hc.collapse(samples)["n"] == 10
        assert hc.collapse(samples)["n"] != sum(s["n"] for s in samples) / 3
        counts, _, _ = hc.classify_stability(samples, [{"s": 1}] * 3)
        assert counts == ["n"]


class TestNoiseBlock:
    def test_reports_spread_and_exactness(self):
        block = hc.noise_block(5, 1, [], ["a_ms", "b_ms"], [1.0, 1.2])
        assert block["reps"] == 5
        assert block["rotation_workers"] == 1
        assert block["work_counts_exact"] is True
        assert block["wall_seconds_spread_pct"] == 20.0
        assert block["timings_vary"] == 2

    def test_records_which_counts_moved(self):
        """The record must name them, so a later reader knows what to distrust."""
        block = hc.noise_block(3, 1, ["nfp_convex_pairs"], [], [1.0, 1.0])
        assert block["work_counts_exact"] is False
        assert block["work_counts_unstable"] == ["nfp_convex_pairs"]

    def test_zero_wall_does_not_divide_by_zero(self):
        block = hc.noise_block(2, 1, [], [], [0.0, 0.0])
        assert block["wall_seconds_spread_pct"] == 0.0
