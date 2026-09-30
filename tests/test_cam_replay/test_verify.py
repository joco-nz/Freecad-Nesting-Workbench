"""Tests for verifying a replayed job.

Background
----------
Everything upstream can succeed while the job still does not do what the source
did. The sharpest example is an operation whose base selection resolves to
nothing: it computes three commands — two comment lines and a positioning G0 —
and raises nothing. A command count would call that a pass, because three is
more than zero. So the emptiness test looks for *cutting motion* rather than
for commands.

The two severities are deliberately different, and the reason is the quality
of each check rather than the seriousness of each fault.

**No cutting motion is a failure.** Every replayed operation exists because the
user set it up on their own part, so one producing no motion is far more likely
to be a broken mapping than a legitimately empty feature. Blocking the sheet is
right: a loud failure that costs one run beats a quiet one that costs a
customer's parts.

**Partial coverage is a warning.** Deciding which toolpath move belongs to
which part means attributing motion to geometry, and a toolpath is a bare
sequence of points. The check compares bounding boxes. It will not catch a part
that is cut only partly, and it could be confused by one part sitting inside
another's toolpath extent — approximate enough that it should inform rather
than gate.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import (
    COVERAGE_MARGIN_MM,
    CUTTING_MOTIONS,
    ReplayResult,
    Verification,
    describe_verification,
    has_cutting_motion,
    motion_of,
    path_bounds,
    shape_bounds,
    uncovered_targets,
    verify_replay,
)


# -- stand-ins ------------------------------------------------------------

class _Cmd:
    def __init__(self, text, x=None, y=None):
        self._text = text
        self.X = x
        self.Y = y

    def __str__(self):
        return self._text


class _Path:
    def __init__(self, commands):
        self.Commands = list(commands)


class _Operation:
    def __init__(self, label, commands=(), base=None):
        self.Label = label
        self.Path = _Path(commands)
        self.Base = base if base is not None else []

    def __getattr__(self, name):
        if name == "Path":
            return None
        raise AttributeError(name)


class _Target:
    def __init__(self, label, box):
        self.Label = label
        self.Shape = _Shape(box)


class _Shape:
    def __init__(self, box):
        self._box = box

    def isNull(self):  # noqa: N802
        return False

    @property
    def BoundBox(self):  # noqa: N802
        return self._box


class _Box:
    def __init__(self, x0, y0, x1, y1):
        self.XMin, self.YMin, self.XMax, self.YMax = x0, y0, x1, y1


CUTTING = [_Cmd("Command (Profile) [ ]"),
           _Cmd("Command G0 [ Z:9 ]"),
           _Cmd("Command G1 [ X:0.0 Y:0.0 Z:-1 ]"),
           _Cmd("Command G2 [ I:1.0 J:0.0 K:0 X:5.0 Y:5.0 Z:-1 ]")]

# What a mis-selected operation actually produces: measured, not invented.
EMPTY = [_Cmd("Command (Profile) [ ]"),
         _Cmd("Command (Compensated Tool Path. Diameter: 5.0) [ ]"),
         _Cmd("Command G0 [ Z:9 ]")]


# -- motion parsing -------------------------------------------------------

class TestMotionOf:
    @pytest.mark.parametrize("text, expected", [
        ("Command G1 [ X:1.0 ]", "G1"),
        ("Command G2 [ I:1.0 ]", "G2"),
        ("Command G3 [ I:1.0 ]", "G3"),
        ("Command G0 [ Z:9 ]", "G0"),
        ("Command (Profile) [ ]", "(Profile)"),
        ("", ""),
    ])
    def test_extracts_the_motion(self, text, expected):
        assert motion_of(_Cmd(text)) == expected

    def test_handles_a_command_with_no_tokens(self):
        assert motion_of(_Cmd("")) == ""


class TestHasCuttingMotion:
    def test_true_for_a_real_cut(self):
        assert has_cutting_motion(_Operation("P", CUTTING)) is True

    def test_false_for_the_measured_empty_path(self):
        # Three commands, no cut. A count would call this a pass.
        assert has_cutting_motion(_Operation("P", EMPTY)) is False

    def test_false_for_travel_only(self):
        travel = [_Cmd("Command G0 [ X:1.0 Y:2.0 Z:9 ]")]
        assert has_cutting_motion(_Operation("P", travel)) is False

    def test_false_for_no_commands(self):
        assert has_cutting_motion(_Operation("P", [])) is False

    def test_false_for_a_null_path(self):
        op = _Operation("P", CUTTING)
        op.Path = None
        assert has_cutting_motion(op) is False

    def test_a_canned_drilling_cycle_counts_as_cutting(self):
        # Measured: a Drilling op emits G81 [ F:0 R:1 X:.. Y:.. Z:-6 ]. It
        # cuts, and a list of G1/G2/G3 would have called it empty.
        assert cam_replay.is_cutting_motion("G81") is True

    @pytest.mark.parametrize("code, cutting", [
        ("G0", False), ("G1", True), ("G2", True), ("G3", True),
        ("G81", True), ("G82", True), ("G83", True), ("G89", True),
        ("(Profile)", False), ("", False), ("", False),
    ])
    def test_cutting_is_decided_by_rule_not_by_list(self, code, cutting):
        assert cam_replay.is_cutting_motion(code) is cutting

    def test_only_g0_is_excluded_from_the_documented_list(self):
        # The tuple is documentation, and must not drift from the rule.
        for code in CUTTING_MOTIONS:
            assert cam_replay.is_cutting_motion(code) is True
        assert cam_replay.RAPID_MOTION not in CUTTING_MOTIONS


# -- bounds ---------------------------------------------------------------

class TestPathBounds:
    def test_reports_the_xy_extent(self):
        op = _Operation("P", [_Cmd("Command G1 [ ]", 1.0, 2.0),
                              _Cmd("Command G1 [ ]", 5.0, -3.0)])
        assert path_bounds(op) == (1.0, -3.0, 5.0, 2.0)

    def test_none_without_any_xy(self):
        assert path_bounds(_Operation("P", [_Cmd("Command G0 [ Z:9 ]")])) is None

    def test_none_for_a_null_path(self):
        op = _Operation("P", CUTTING)
        op.Path = None
        assert path_bounds(op) is None


class TestShapeBounds:
    def test_reports_the_extent(self):
        target = _Target("p", _Box(0, 0, 10, 20))
        assert shape_bounds(target) == (0, 0, 10, 20)

    def test_none_for_a_null_shape(self):
        class _Null:
            Label = "x"

            class Shape:
                @staticmethod
                def isNull():  # noqa: N802
                    return True

        assert shape_bounds(_Null()) is None

    def test_none_for_an_object_with_no_shape(self):
        assert shape_bounds(object()) is None


# -- coverage -------------------------------------------------------------

class TestUncoveredTargets:
    def test_a_target_under_the_path_is_covered(self):
        target = _Target("p", _Box(0, 0, 10, 10))
        op = _Operation("P", [_Cmd("Command G1 [ ]", 5.0, 5.0)],
                        base=[(target, [""])])
        assert uncovered_targets(op) == []

    def test_a_target_far_away_is_uncovered(self):
        target = _Target("far", _Box(500, 500, 510, 510))
        op = _Operation("P", [_Cmd("Command G1 [ ]", 5.0, 5.0)],
                        base=[(target, [""])])
        assert uncovered_targets(op) == ["far"]

    def test_a_profile_offset_outside_the_part_still_counts(self):
        # A profile op cuts about one tool radius beyond the outline, so the
        # path sits outside the part. Containment would miss it; overlap does
        # not, which is why this is not a containment test.
        target = _Target("p", _Box(0, 0, 10, 10))
        op = _Operation("P", [_Cmd("Command G1 [ ]", -2.0, -2.0),
                              _Cmd("Command G1 [ ]", 12.0, 12.0)],
                        base=[(target, [""])])
        assert uncovered_targets(op) == []

    def test_a_drill_inside_the_part_counts(self):
        target = _Target("p", _Box(0, 0, 40, 40))
        op = _Operation("P", [_Cmd("Command G1 [ ]", 20.0, 20.0)],
                        base=[(target, [""])])
        assert uncovered_targets(op) == []

    def test_reports_only_the_ones_it_misses(self):
        near = _Target("near", _Box(0, 0, 10, 10))
        far = _Target("far", _Box(500, 500, 510, 510))
        op = _Operation("P", [_Cmd("Command G1 [ ]", 5.0, 5.0)],
                        base=[(near, [""]), (far, [""])])
        assert uncovered_targets(op) == ["far"]

    def test_everything_uncovered_when_the_path_has_no_moves(self):
        target = _Target("p", _Box(0, 0, 10, 10))
        op = _Operation("P", [], base=[(target, [""])])
        assert uncovered_targets(op) == ["p"]

    def test_no_targets_means_nothing_uncovered(self):
        assert uncovered_targets(_Operation("P", CUTTING, base=[])) == []


# -- verification ---------------------------------------------------------

class TestVerifyReplay:
    def _result(self, operations, detail=None):
        result = ReplayResult()
        result.operations = operations
        result.subname_detail = detail or {}
        return result

    def test_passes_a_healthy_replay(self):
        target = _Target("p", _Box(0, 0, 10, 10))
        op = _Operation("P", CUTTING, base=[(target, [""])])
        verification = verify_replay(self._result([op]))
        assert verification.ok is True
        assert verification.operations_with_motion == 1

    def test_fails_on_an_operation_with_no_cutting_motion(self):
        op = _Operation("Empty", EMPTY)
        verification = verify_replay(self._result([op]))
        assert verification.ok is False
        assert "no cutting motion" in verification.failures[0]

    def test_the_failure_says_nothing_was_cut(self):
        # The consequence matters more than the symptom.
        op = _Operation("Empty", EMPTY)
        text = verify_replay(self._result([op])).failures[0]
        assert "Nothing was cut" in text

    def test_fails_loudly_rather_than_warning(self):
        op = _Operation("Empty", EMPTY)
        verification = verify_replay(self._result([op]))
        assert verification.warnings == []

    def test_a_failing_operation_is_not_also_coverage_checked(self):
        # No point warning that an empty path reaches nothing.
        target = _Target("p", _Box(500, 500, 510, 510))
        op = _Operation("Empty", EMPTY, base=[(target, [""])])
        verification = verify_replay(self._result([op]))
        assert verification.warnings == []

    def test_warns_on_partial_coverage(self):
        near = _Target("near", _Box(0, 0, 10, 10))
        far = _Target("far", _Box(500, 500, 510, 510))
        op = _Operation("P", CUTTING, base=[(near, [""]), (far, [""])])
        verification = verify_replay(self._result([op]))
        assert verification.ok is True
        assert any("far" in w for w in verification.warnings)

    def test_the_coverage_warning_says_it_is_approximate(self):
        far = _Target("far", _Box(500, 500, 510, 510))
        op = _Operation("P", CUTTING, base=[(far, [""])])
        text = "\n".join(verify_replay(self._result([op])).warnings)
        assert "approximate" in text

    def test_warns_about_a_partial_subname_resolution(self):
        op = _Operation("P", CUTTING)
        verification = verify_replay(self._result([op], {"Face1": "resolved on 1 of 2"}))
        assert any("resolved on 1 of 2" in w for w in verification.warnings)

    def test_says_nothing_about_a_resolved_subname(self):
        op = _Operation("P", CUTTING)
        verification = verify_replay(self._result([op], {"Face1": "ok"}))
        assert verification.warnings == []

    def test_carries_extra_warnings_through(self):
        op = _Operation("P", CUTTING)
        verification = verify_replay(self._result([op]), ["stock frame differs"])
        assert "stock frame differs" in verification.warnings

    def test_counts_what_it_checked(self):
        ops = [_Operation("A", CUTTING), _Operation("B", CUTTING),
               _Operation("C", EMPTY)]
        verification = verify_replay(self._result(ops))
        assert verification.operations_checked == 3
        assert verification.operations_with_motion == 2

    def test_an_empty_replay_verifies_cleanly(self):
        verification = verify_replay(ReplayResult())
        assert verification.ok is True
        assert verification.operations_checked == 0


class TestDescribeVerification:
    def test_failures_come_first(self):
        v = Verification()
        v.warnings.append("a warning")
        v.failures.append("a failure")
        text = "\n".join(describe_verification(v))
        assert text.index("a failure") < text.index("a warning")

    def test_a_clean_report_states_what_was_checked(self):
        v = Verification()
        v.operations_checked = 3
        v.operations_with_motion = 3
        assert "Verified 3 operation(s)" in "\n".join(describe_verification(v))

    def test_reports_the_shortfall(self):
        v = Verification()
        v.operations_checked = 3
        v.operations_with_motion = 1
        assert "1 of 3 operation(s)" in "\n".join(describe_verification(v))

    def test_no_counts_line_when_nothing_failed(self):
        v = Verification()
        v.operations_checked = 2
        v.operations_with_motion = 2
        assert "of 2 operation(s) produced" not in "\n".join(describe_verification(v))

    def test_lines_are_plain_text(self):
        v = Verification()
        v.failures.append("x")
        assert all(isinstance(line, str) for line in describe_verification(v))


# -- containment ----------------------------------------------------------

class TestPartsOutsideStock:
    def test_a_part_within_the_stock_is_fine(self):
        stock = _Target("stock", _Box(0, 0, 100, 100))
        part = _Target("p", _Box(10, 10, 20, 20))
        assert cam_replay.parts_outside_stock([part], stock) == []

    def test_a_part_past_the_edge_is_reported(self):
        stock = _Target("stock", _Box(0, 0, 100, 100))
        part = _Target("p", _Box(90, 10, 130, 20))
        assert cam_replay.parts_outside_stock([part], stock) == ["p"]

    def test_reports_only_the_offenders(self):
        stock = _Target("stock", _Box(0, 0, 100, 100))
        inside = _Target("in", _Box(10, 10, 20, 20))
        outside = _Target("out", _Box(200, 200, 210, 210))
        assert cam_replay.parts_outside_stock([inside, outside], stock) == ["out"]

    def test_a_part_touching_the_edge_is_inside(self):
        stock = _Target("stock", _Box(0, 0, 100, 100))
        part = _Target("p", _Box(0, 0, 100, 100))
        assert cam_replay.parts_outside_stock([part], stock) == []

    def test_nothing_reported_without_a_stock(self):
        part = _Target("p", _Box(0, 0, 10, 10))
        assert cam_replay.parts_outside_stock([part], None) == []


class TestVerifyReplayContainment:
    def _stock(self):
        return _Target("stock", _Box(0, 0, 100, 100))

    def test_a_part_off_the_stock_fails(self):
        result = ReplayResult()
        result.operations = [_Operation("P", CUTTING)]
        stray = _Target("stray", _Box(500, 500, 510, 510))
        verification = verify_replay(result, clones=[stray], stock=self._stock())
        assert verification.ok is False
        assert "outside the stock" in verification.failures[0]

    def test_the_failure_suggests_the_cause(self):
        # A part off the sheet almost always means the sheet origin was not
        # applied, so say so rather than making the operator work it out.
        result = ReplayResult()
        result.operations = [_Operation("P", CUTTING)]
        stray = _Target("stray", _Box(500, 500, 510, 510))
        verification = verify_replay(result, clones=[stray], stock=self._stock())
        assert "local coordinates" in verification.failures[0]

    def test_a_contained_part_passes(self):
        result = ReplayResult()
        result.operations = [_Operation("P", CUTTING)]
        part = _Target("p", _Box(10, 10, 20, 20))
        verification = verify_replay(result, clones=[part], stock=self._stock())
        assert verification.ok is True

    def test_containment_is_skipped_without_clones(self):
        result = ReplayResult()
        result.operations = [_Operation("P", CUTTING)]
        assert verify_replay(result).ok is True
