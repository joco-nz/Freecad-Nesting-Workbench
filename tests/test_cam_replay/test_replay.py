"""Tests for replaying a recipe's operations into a new job.

Background
----------
This is where the read half and the write half meet, and it is where the
replay can go quietly wrong. A replayed operation that exists, is not empty,
and is attached to the right job can still be cutting a *different feature* --
and the result is plausible G-code that removes the wrong material.

Three things are pinned here, each because it fails silently:

**Two passes.** `Operations.Group` was observed on a live job as
`['DressupLeadInOut', 'Profile', 'Drilling']` -- the dressup ahead of the
operation it wraps. A single pass raises `KeyError` when the dressup cannot find
its base. That is the ordinary case, not an edge case.

**Sub-element names are verified, not trusted.** Source sub-names
(`Face1`, `Edge7`) are passed through to the flattened copies, which works
because a `Placement` leaves topology untouched. But nothing about that
guarantees the names still mean what they meant, so every name is resolved
against the target geometry first. A name that does not resolve aborts its
operation rather than being dropped: a loud failure is the only acceptable
outcome for "cut the wrong feature".

**The tool is copied, not linked across.** An operation in job B will accept
job A's tool controller with no error at all -- verified. But it leaves the new
job depending on the source job surviving, so deleting the source job breaks
the replay. `Path.Tool.Controller.copyTC` makes the new job self-contained,
which is the entire point of creating a new job.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import (
    ReplayResult,
    apply_properties,
    check_subnames_against_clones,
    check_tool_clearance,
    tool_diameters,
    clones_for_source,
    create_dressup_in,
    create_operation_in,
    describe_replay_result,
    nested_label_of,
    resolve_subnames,
)


# -- stand-ins ------------------------------------------------------------

class _Shape:
    """A shape that resolves a fixed set of sub-element names."""

    def __init__(self, names):
        self._names = set(names)

    def getElement(self, name):  # noqa: N802
        if name in self._names:
            return "element:" + name
        raise ValueError("no element named %r" % name)


class _Clone:
    def __init__(self, label, original=None, flattened=None):
        self.Label = label
        self.Objects = [flattened] if flattened else ([original] if original else [])


class _Flat:
    def __init__(self, nested_label):
        self.Label = "CAMPart"
        if nested_label is not None:
            setattr(self, cam_replay.PROP_NESTED_LABEL, nested_label)


class _Part:
    """A plain `Part::Feature` in the job's Model -- a flattened part itself.

    Deliberately has no `Objects` attribute at all, because a real
    `Part::Feature` does not either. This is what
    `adopt_flattened_parts_as_model` leaves in the Model in place of the Clones
    `PathJob.Create` made.
    """

    def __init__(self, nested_label):
        self.Label = "CAMPart"
        if nested_label is not None:
            setattr(self, cam_replay.PROP_NESTED_LABEL, nested_label)


class _Original:
    def __init__(self, label):
        self.Label = label


class _Target:
    def __init__(self, name, names, nested_label=None, original=None):
        self.Label = name
        self.Shape = _Shape(names)
        self.Objects = [_Flat(nested_label)] if nested_label is not None else []
        if original is not None:
            self.Objects = [original]


# -- sub-element resolution ----------------------------------------------

class TestResolveSubnames:
    def test_keeps_names_that_resolve(self):
        shape = _Shape(["Face1", "Face2"])
        assert resolve_subnames(["Face1", "Face2"], shape) == ["Face1", "Face2"]

    def test_drops_names_that_do_not_resolve(self):
        shape = _Shape(["Face1"])
        assert resolve_subnames(["Face1", "Face99"], shape) == ["Face1"]

    def test_keeps_the_empty_string_as_whole_object(self):
        assert resolve_subnames([""], _Shape([])) == [""]

    def test_preserves_order(self):
        shape = _Shape(["Face1", "Face2", "Face3"])
        assert resolve_subnames(["Face3", "Face1"], shape) == ["Face3", "Face1"]

    def test_preserves_duplicates(self):
        # A selection naming the same edge twice is the user's business.
        shape = _Shape(["Edge1"])
        assert resolve_subnames(["Edge1", "Edge1"], shape) == ["Edge1", "Edge1"]

    def test_never_raises_for_any_input(self):
        resolve_subnames(["Face1", "", "nonsense"], _Shape([]))


class _Element:
    """A stand-in sub-element carrying the measures `subelement_signature` reads.

    Real geometry is what proves these numbers mean anything -- see
    `tests/freecad_harness/test_replay_subnames.py` -- but the comparison logic
    is pure arithmetic and can be exercised here, which is the point of this
    tier.
    """

    def __init__(self, kind, area=0.0, wires=0, edges=0, length=0.0,
                 closed=False):
        self.ShapeType = kind
        self.Area = area
        self.Wires = [_object() for _ in range(wires)]
        self.Edges = [_object() for _ in range(edges)]
        self.Length = length
        self.Closed = closed


class _MeasuredShape:
    """A shape whose named elements carry `subelement_signature` metrics."""

    def __init__(self, elements):
        self._elements = dict(elements)

    def isNull(self):  # noqa: N802
        # Real `Shape` objects carry this and `cam_replay` reads it before
        # trusting a shape, so the stand-in has to as well -- otherwise a test
        # would pass only because the fake was more forgiving than the thing.
        return False

    def getElement(self, name):  # noqa: N802
        if name in self._elements:
            return self._elements[name]
        raise ValueError("no element named %r" % name)


class _Source:
    """An object whose `Shape` is what a sub-element name resolves against."""

    def __init__(self, shape, label="source"):
        self.Shape = shape
        self.Label = label


class _MeasuredTarget:
    def __init__(self, label, shape):
        self.Label = label
        self.Shape = shape


def _object():
    return object()


def _plate_faces():
    """A holed plate's seven faces, by index -- NEST-014's geometry."""
    return {
        "Face1": _Element("Face", area=125.0, wires=1, edges=4),
        "Face2": _Element("Face", area=200.0, wires=1, edges=4),
        "Face3": _Element("Face", area=886.9027, wires=2, edges=5),
        "Face4": _Element("Face", area=200.0, wires=1, edges=4),
        "Face5": _Element("Face", area=886.9027, wires=2, edges=5),
        "Face6": _Element("Face", area=125.0, wires=1, edges=4),
        "Face7": _Element("Face", area=125.0, wires=1, edges=4),
    }


def _box_faces():
    """A holeless box's six faces -- the same names, different features."""
    return {
        "Face1": _Element("Face", area=125.0, wires=1, edges=4),
        "Face2": _Element("Face", area=125.0, wires=1, edges=4),
        "Face3": _Element("Face", area=200.0, wires=1, edges=4),
        "Face4": _Element("Face", area=200.0, wires=1, edges=4),
        "Face5": _Element("Face", area=1000.0, wires=1, edges=4),
        "Face6": _Element("Face", area=1000.0, wires=1, edges=4),
    }


class TestCheckSubnamesAgainstClones:
    def test_reports_ok_when_every_name_resolves_everywhere(self):
        clones = [_Target("c1", ["Face1"]), _Target("c2", ["Face1"])]
        missing, mismatched, detail = check_subnames_against_clones(["Face1"], clones)
        assert missing == set()
        assert mismatched == set()
        assert detail["Face1"] == "ok"

    def test_reports_a_name_that_resolves_nowhere(self):
        clones = [_Target("c1", ["Face1"]), _Target("c2", ["Face1"])]
        missing, mismatched, detail = check_subnames_against_clones(["Face9"], clones)
        assert missing == {"Face9"}
        assert mismatched == set()
        assert "0 of 2" in detail["Face9"]

    def test_reports_a_name_that_resolves_on_only_some(self):
        # The alarming case: the same operation would cut a different feature
        # on different parts of one nest.
        clones = [_Target("c1", ["Face1"]), _Target("c2", ["Face2"])]
        missing, mismatched, detail = check_subnames_against_clones(["Face1"], clones)
        assert missing == set()
        assert mismatched == set()
        assert "1 of 2" in detail["Face1"]

    def test_treats_the_empty_string_as_always_present(self):
        clones = [_Target("c1", []), _Target("c2", [])]
        missing, mismatched, detail = check_subnames_against_clones([""], clones)
        assert missing == set()
        assert mismatched == set()
        assert detail[""] == "whole object"

    def test_no_clones_means_nothing_resolves(self):
        missing, _mismatched, _detail = check_subnames_against_clones(["Face1"], [])
        assert missing == {"Face1"}


class TestSubelementSignatureComparison:
    """NEST-027: a name that resolves is not the same as a name that means the
    same thing. Existence alone reported "ok" for a name addressing a
    completely different face."""

    def test_flags_a_name_that_resolves_to_a_different_face(self):
        source = _Source(_MeasuredShape(_plate_faces()))
        box = _MeasuredTarget("part_A", _MeasuredShape(_box_faces()))
        _missing, mismatched, detail = check_subnames_against_clones(
            ["Face3"], [box], source)
        assert mismatched == {("Face3", "part_A")}
        assert "different feature" in detail["Face3"]

    def test_accepts_an_identical_copy(self):
        source = _Source(_MeasuredShape(_plate_faces()))
        same = _MeasuredTarget("part_A", _MeasuredShape(_plate_faces()))
        _missing, mismatched, detail = check_subnames_against_clones(
            ["Face3"], [same], source)
        assert mismatched == set()
        assert detail["Face3"] == "ok"

    def test_catches_a_wire_count_difference_at_equal_area(self):
        # The area alone would let this through: 886.9027 either way. What
        # differs is that one is the holed top and the other is not.
        faces = _plate_faces()
        faces["Face3"] = _Element("Face", area=886.9027, wires=1, edges=4)
        source = _Source(_MeasuredShape(_plate_faces()))
        other = _MeasuredTarget("part_A", _MeasuredShape(faces))
        _missing, mismatched, _detail = check_subnames_against_clones(
            ["Face3"], [other], source)
        assert mismatched == {("Face3", "part_A")}

    def test_flags_a_shape_type_change(self):
        source = _Source(_MeasuredShape(
            {"Face1": _Element("Face", area=10.0, wires=1, edges=4)}))
        other = _MeasuredTarget("part_A", _MeasuredShape(
            {"Face1": _Element("Edge", length=10.0)}))
        _missing, mismatched, _detail = check_subnames_against_clones(
            ["Face1"], [other], source)
        assert mismatched == {("Face1", "part_A")}

    def test_a_tiny_area_difference_within_tolerance_is_accepted(self):
        # The nesting moves geometry, so it is never bit-identical; measured
        # worst case is 1.421e-14 mm2 (NEST-007).
        faces = _plate_faces()
        faces["Face3"] = _Element("Face", area=886.9027 + 1e-12, wires=2, edges=5)
        source = _Source(_MeasuredShape(_plate_faces()))
        moved = _MeasuredTarget("part_A", _MeasuredShape(faces))
        _missing, mismatched, _detail = check_subnames_against_clones(
            ["Face3"], [moved], source)
        assert mismatched == set()

    def test_a_half_percent_area_difference_is_refused(self):
        faces = _plate_faces()
        faces["Face3"] = _Element("Face", area=886.9027 * 1.005, wires=2, edges=5)
        source = _Source(_MeasuredShape(_plate_faces()))
        other = _MeasuredTarget("part_A", _MeasuredShape(faces))
        _missing, mismatched, _detail = check_subnames_against_clones(
            ["Face3"], [other], source)
        assert mismatched == {("Face3", "part_A")}

    def test_compares_edges_by_length(self):
        source = _Source(_MeasuredShape(
            {"Edge1": _Element("Edge", length=75.398224, closed=True)}))
        same = _MeasuredTarget("part_A", _MeasuredShape(
            {"Edge1": _Element("Edge", length=75.398224, closed=True)}))
        other = _MeasuredTarget("part_B", _MeasuredShape(
            {"Edge1": _Element("Edge", length=120.0, closed=True)}))
        _missing, mismatched, detail = check_subnames_against_clones(
            ["Edge1"], [same, other], source)
        assert mismatched == {("Edge1", "part_B")}
        assert "1 of 2" in detail["Edge1"]
        assert "same feature on 1" in detail["Edge1"]

    def test_counts_both_agreeing_and_mismatching_copies(self):
        source = _Source(_MeasuredShape(_plate_faces()))
        clones = [_MeasuredTarget("part_A", _MeasuredShape(_box_faces())),
                  _MeasuredTarget("part_B", _MeasuredShape(_box_faces())),
                  _MeasuredTarget("part_C", _MeasuredShape(_plate_faces()))]
        _missing, mismatched, detail = check_subnames_against_clones(
            ["Face3"], clones, source)
        assert mismatched == {("Face3", "part_A"), ("Face3", "part_B")}
        assert "different feature on 2 of 3" in detail["Face3"]
        assert "same feature on 1" in detail["Face3"]

    def test_a_source_that_resolves_nothing_does_not_refuse_everything(self):
        # The source cannot say what the name means, so the copy is not accused
        # of contradicting it -- only of not existing there, which is `missing`.
        empty = _Source(_MeasuredShape({}))
        clone = _MeasuredTarget("part_A", _MeasuredShape(_plate_faces()))
        _missing, mismatched, detail = check_subnames_against_clones(
            ["Face3"], [clone], empty)
        assert mismatched == set()
        assert detail["Face3"] == "ok"

    def test_without_a_source_only_existence_is_checked(self):
        clone = _MeasuredTarget("part_A", _MeasuredShape(_box_faces()))
        _missing, mismatched, detail = check_subnames_against_clones(
            ["Face3"], [clone])
        assert mismatched == set()
        assert detail["Face3"] == "ok"

    def test_a_name_the_source_cannot_explain_is_missing_not_mismatched(self):
        # Two different failures, and the report must not confuse them. A name
        # that resolves nowhere on the copies is `missing`; the source's own
        # inability to resolve it is not additional evidence of a mismatch.
        source = _Source(_MeasuredShape({}))
        clone = _MeasuredTarget("part_A", _MeasuredShape(_box_faces()))
        missing, mismatched, _detail = check_subnames_against_clones(
            ["Face9"], [clone], source)
        assert mismatched == set()
        assert missing == {"Face9"}


# -- identity matching ----------------------------------------------------

class TestClonesForSource:
    def test_narrows_to_the_matching_part_type(self):
        brackets = [_Target("b1", [], nested_label="nested_Bracket_1")]
        spacers = [_Target("s1", [], nested_label="nested_Spacer_1")]
        clones = brackets + spacers
        assert clones_for_source(clones, _Original("Bracket")) == brackets
        assert clones_for_source(clones, _Original("Spacer")) == spacers

    def test_matches_every_copy_of_a_part_type(self):
        clones = [_Target("b1", [], nested_label="nested_Bracket_1"),
                  _Target("b2", [], nested_label="nested_Bracket_2")]
        assert len(clones_for_source(clones, _Original("Bracket"))) == 2

    def test_falls_back_to_all_clones_when_nothing_matches(self):
        # Safer direction: too many parts is visible in the toolpath, too few
        # silently cuts less than the source.
        clones = [_Target("b1", [], nested_label="nested_Bracket_1")]
        assert clones_for_source(clones, _Original("Widget")) == clones

    def test_unwraps_a_source_job_clone_before_comparing(self):
        # The source operation's Base points at `Model-Bracket`, not at the
        # user's `Bracket`. Comparing the clone's label matched nothing, and
        # every operation fell back to targeting every clone.
        source_clone = _Clone("Model-Bracket", original=_Original("Bracket"))
        clones = [_Target("b1", [], nested_label="nested_Bracket_1"),
                  _Target("s1", [], nested_label="nested_Spacer_1")]
        assert clones_for_source(clones, source_clone) == [clones[0]]

    def test_handles_a_part_type_containing_an_underscore(self):
        clones = [_Target("a", [], nested_label="nested_My_Part_1"),
                  _Target("b", [], nested_label="nested_Other_1")]
        matched = clones_for_source(clones, _Original("My_Part"))
        assert matched == [clones[0]]

    def test_falls_back_for_an_unlabelled_source(self):
        clones = [_Target("b1", [], nested_label="nested_Bracket_1")]
        assert clones_for_source(clones, _Original("")) == clones

    def test_never_returns_none(self):
        assert clones_for_source([], _Original("Bracket")) == []


class TestNestedLabelOf:
    """Reading the Model's identity, whichever kind of object is in there.

    `clones_for_source` used to walk `Clone -> Objects[0] -> NestedLabel` and
    nothing else. When the Model holds the flattened parts themselves that path
    is empty, so nothing matched, so the function returned every entry, so every
    operation targeted all 48 parts regardless of type -- and 5 of 7 stopped
    cutting because their sub-element names resolved on only 23 of 48. Measured
    on the committed fixture, not inferred.
    """

    def test_reads_the_label_off_the_object_itself(self):
        assert nested_label_of(_Part("nested_Bracket_1")) == "nested_Bracket_1"

    def test_falls_back_to_the_clone_path(self):
        clone = _Target("c1", [], nested_label="nested_Bracket_1")
        assert nested_label_of(clone) == "nested_Bracket_1"

    def test_owns_the_label_in_preference_to_the_link(self):
        # If an entry somehow carries both, its own label is the one that
        # describes it. Getting this backwards would narrow on a stale link.
        entry = _Target("c1", [], nested_label="nested_Stale_1")
        setattr(entry, cam_replay.PROP_NESTED_LABEL, "nested_Real_1")
        assert nested_label_of(entry) == "nested_Real_1"

    def test_empty_for_an_object_with_neither(self):
        assert nested_label_of(_Part(None)) == ""

    def test_empty_for_a_link_whose_target_is_unlabelled(self):
        assert nested_label_of(_Clone("Clone", flattened=_Flat(None))) == ""

    def test_survives_an_object_with_no_objects_property(self):
        # A `Part::Feature` really does not have one, so `getattr` with a
        # default is the whole of the defensiveness needed.
        assert nested_label_of(object()) == ""


class TestModelIdentitySurvivesTheSwap:
    """`clones_for_source` against a Model that holds the flattened parts."""

    def _model(self):
        return [_Part("nested_Bracket_1"), _Part("nested_Bracket_2"),
                _Part("nested_Spacer_1"), _Part("nested_Spacer_2")]

    def test_narrows_to_the_matching_part_type(self):
        model = self._model()
        brackets = model[:2]
        spacers = model[2:]
        assert clones_for_source(model, _Original("Bracket")) == brackets
        assert clones_for_source(model, _Original("Spacer")) == spacers

    def test_does_not_silently_match_everything(self):
        # The regression that matters. Returning all four here is the failure
        # that made the swapped job cut less, and it looked fine -- no
        # exception, no warning, just a short toolpath.
        model = self._model()
        assert len(clones_for_source(model, _Original("Bracket"))) < len(model)

    def test_works_across_a_mix_of_clones_and_parts(self):
        # Not what the replay produces today, but it must not be the thing that
        # breaks first if the job is hand-edited.
        model = [_Target("c1", [], nested_label="nested_Bracket_1"),
                 _Part("nested_Bracket_2"), _Part("nested_Spacer_1")]
        assert clones_for_source(model, _Original("Bracket")) == model[:2]

    def test_falls_back_to_all_when_nothing_matches(self):
        model = self._model()
        assert clones_for_source(model, _Original("Widget")) == model


# -- property application -------------------------------------------------

class TestApplyProperties:
    class _Target:
        def __init__(self, readonly=()):
            self.readonly = set(readonly)
            self.set = {}

        def __setattr__(self, name, value):
            if name in ("readonly", "set"):
                object.__setattr__(self, name, value)
                return
            if name in self.readonly:
                raise RuntimeError("read-only property")
            self.set[name] = value

    def test_assigns_properties(self):
        target = self._Target()
        applied, skipped = apply_properties(target, {"Side": "Outside", "UseComp": True})
        assert applied == ["Side", "UseComp"]
        assert skipped == []
        assert target.set["Side"] == "Outside"

    def test_skips_named_properties(self):
        target = self._Target()
        applied, skipped = apply_properties(target, {"Base": "x", "Side": "y"}, skip=("Base",))
        assert applied == ["Side"]
        assert skipped == ["Base"]
        assert "Base" not in target.set

    def test_remaps_a_property(self):
        target = self._Target()
        apply_properties(target, {"ToolController": "old"},
                         remap={"ToolController": "new"})
        assert target.set["ToolController"] == "new"

    def test_skips_a_property_whose_remap_is_none(self):
        # A source operation with no tool controller remaps to None, and there
        # is nothing to assign.
        target = self._Target()
        applied, skipped = apply_properties(target, {"ToolController": "old"},
                                            remap={"ToolController": None})
        assert applied == []
        assert skipped == ["ToolController"]

    def test_collects_rather_than_raises_on_a_readonly_property(self):
        target = self._Target(readonly=("CycleTime",))
        applied, skipped = apply_properties(target, {"CycleTime": "x", "Side": "y"})
        assert applied == ["Side"]
        assert skipped == ["CycleTime"]

    def test_no_properties_is_a_noop(self):
        applied, skipped = apply_properties(self._Target(), {})
        assert applied == [] and skipped == []


# -- replay bookkeeping ---------------------------------------------------

class TestReplayResult:
    def test_starts_empty(self):
        result = ReplayResult()
        assert len(result) == 0
        assert result.failures == []

    def test_repr_counts_things(self):
        result = ReplayResult()
        result.failures.append("nope")
        assert "1 failure" in repr(result)

    def test_report_states_a_clean_sub_element_check(self):
        result = ReplayResult()
        result.operations = [object()]
        text = "\n".join(describe_replay_result(result))
        assert "All sub-element selections resolved" in text

    def test_report_surfaces_a_failure(self):
        result = ReplayResult()
        result.failures.append("something went wrong")
        assert "WARNING: something went wrong" in "\n".join(describe_replay_result(result))

    def test_report_surfaces_a_partial_subname_resolution(self):
        result = ReplayResult()
        result.subname_detail["Face1"] = "resolved on 1 of 2"
        text = "\n".join(describe_replay_result(result))
        assert "resolved on 1 of 2" in text
        assert "All sub-element selections resolved" not in text


# -- construction guards --------------------------------------------------

class TestConstructionGuards:
    """These need a real FreeCAD to exercise, and are covered by the
    freecadcmd harness. What is pinned here is that they decline rather than
    raise when handed something they cannot rebuild."""

    def test_operation_without_a_proxy_is_not_created(self):
        assert create_operation_in(_Original("x"), None) is None

    def test_dressup_without_a_base_is_not_created(self):
        assert create_dressup_in(_Original("x"), None, None) is None

    def test_dressup_without_a_proxy_is_not_created(self):
        assert create_dressup_in(_Original("x"), _Original("base"), None) is None


# -- tool clearance --------------------------------------------------------

class _Quantity:
    def __init__(self, value):
        self.Value = value


class _Tool:
    def __init__(self, diameter):
        self.Diameter = _Quantity(diameter)


class _Controller:
    def __init__(self, label, diameter):
        self.Label = label
        self.Tool = _Tool(diameter)


class _ToolsGroup:
    def __init__(self, controllers):
        self.Group = controllers


class _Job:
    def __init__(self, controllers):
        self.Tools = _ToolsGroup(controllers)


class _BoxFootprint:
    """A footprint that is an axis-aligned box, with a real `distance`.

    The geometry is not the point of this tier -- the freecadcmd suite measures
    that. What matters here is that the bounding-box prune does not change the
    answer, so `distance` has to behave like Shapely's for disjoint and
    overlapping boxes.
    """

    def __init__(self, x0, y0, x1, y1):
        self.bounds = (x0, y0, x1, y1)
        self.is_empty = False

    def distance(self, other):
        dx = max(0.0, max(self.bounds[0] - other.bounds[2],
                          other.bounds[0] - self.bounds[2]))
        dy = max(0.0, max(self.bounds[1] - other.bounds[3],
                          other.bounds[1] - self.bounds[3]))
        return (dx * dx + dy * dy) ** 0.5


class _BoxPart:
    """A part carrying only a footprint, which is all `tightest_part_gap` reads.

    `Shape` is present and null because `_footprint_for` falls back to
    `part_footprint` when there is no cache -- so a stand-in that has a Shape
    attribute at all must not accidentally be sliced.
    """

    def __init__(self, label, footprint):
        self.Label = label
        self._footprint = footprint
        self.Shape = None


def _box_part(label, x0, y0, x1, y1):
    return _BoxPart(label, _BoxFootprint(x0, y0, x1, y1))


class TestToolDiameters:
    def test_reads_the_widest_first(self):
        job = _Job([_Controller("small", 3.0), _Controller("big", 12.0)])
        assert tool_diameters(job) == [("big", 12.0), ("small", 3.0)]

    def test_a_job_with_no_tools_yields_nothing(self):
        assert tool_diameters(_Job([])) == []

    def test_a_controller_with_no_tool_is_skipped_not_fatal(self):
        broken = _Controller("broken", 0.0)
        broken.Tool = None
        assert tool_diameters(_Job([broken])) == []

    def test_a_zero_diameter_is_not_a_clearance(self):
        # 0 would warn about everything, and a tool that measures nothing wide
        # is not something to warn about.
        assert tool_diameters(_Job([_Controller("zero", 0.0)])) == []


class TestCheckToolClearance:
    def _run(self, parts, diameters):
        cache = {id(p): p._footprint for p in parts}

        class _Cache:
            def get(self, part):
                return cache[id(part)]

        job = _Job([_Controller(label, value) for label, value in diameters])
        return check_tool_clearance(parts, job, _Cache())

    def test_silent_when_the_tool_fits_between_the_parts(self):
        parts = [_box_part("a", 0, 0, 10, 10), _box_part("b", 20, 0, 30, 10)]
        assert self._run(parts, [("t", 5.0)]) is None

    def test_warns_when_the_parts_are_closer_than_the_tool(self):
        parts = [_box_part("a", 0, 0, 10, 10), _box_part("b", 13, 0, 23, 10)]
        warning = self._run(parts, [("t", 5.0)])
        assert warning is not None
        assert "13.0000" in warning or "3.0000" in warning

    def test_the_warning_names_both_parts(self):
        parts = [_box_part("bracket_1", 0, 0, 10, 10),
                 _box_part("bracket_2", 13, 0, 23, 10)]
        warning = self._run(parts, [("endmill", 5.0)])
        assert "bracket_1" in warning and "bracket_2" in warning

    def test_the_warning_states_the_shortfall_and_the_fix(self):
        parts = [_box_part("a", 0, 0, 10, 10), _box_part("b", 13, 0, 23, 10)]
        warning = self._run(parts, [("endmill", 5.0)])
        assert "2.0000" in warning, "shortfall not stated: %s" % warning
        assert "PartSpacing" in warning, "no remedy offered: %s" % warning

    def test_silent_at_exactly_one_tool_diameter(self):
        # The cutter only just grazes its neighbour there and removes nothing,
        # so reporting it would be noise.
        parts = [_box_part("a", 0, 0, 10, 10), _box_part("b", 15, 0, 25, 10)]
        assert self._run(parts, [("t", 5.0)]) is None

    def test_judged_against_the_widest_tool_not_the_first(self):
        parts = [_box_part("a", 0, 0, 10, 10), _box_part("b", 13, 0, 23, 10)]
        assert self._run(parts, [("narrow", 1.0), ("wide", 20.0)]) is not None

    def test_no_warning_without_a_tool(self):
        parts = [_box_part("a", 0, 0, 10, 10), _box_part("b", 1, 0, 11, 10)]
        assert self._run(parts, []) is None

    def test_a_single_part_cannot_be_too_close_to_anything(self):
        assert self._run([_box_part("only", 0, 0, 10, 10)], [("t", 99.0)]) is None

    def test_the_bound_prune_does_not_hide_the_closest_pair(self):
        # The tight pair (far_b/near, 3 mm) is not the first pair examined --
        # far_a/far_b is, at 490 mm. A prune that stopped tightening its bound
        # after the first hit would keep 490 and skip the 3.
        parts = [_box_part("far_a", 0, 0, 10, 10),
                 _box_part("far_b", 500, 0, 510, 10),
                 _box_part("near", 513, 0, 523, 10)]
        warning = self._run(parts, [("t", 5.0)])
        assert warning is not None
        assert "far_b" in warning and "near" in warning

    def test_the_bounds_gap_never_exceeds_the_true_distance(self):
        """The prune's precondition, tested directly rather than by proxy.

        `tightest_part_gap` skips a pair when `_bounds_gap` says it is at least
        as far as the best found so far. That is only sound if `_bounds_gap` is
        a true **lower** bound -- a footprint lies inside its own bounding box,
        so the boxes cannot be closer than the shapes are. Get that wrong and a
        prune can skip the very pair it should have measured, and the warning
        would then quote a comfortable neighbour instead of the tight one: the
        quietest way to be wrong.

        Tried and failed to catch this through `tightest_part_gap`, because with
        axis-aligned boxes the bound equals the true distance exactly, so any
        uniform perturbation leaves the ordering unchanged. So it is asserted
        here, over rotated and overlapping shapes as well, which is where a box
        bound and a true distance genuinely differ.
        """
        import random

        from shapely.affinity import rotate
        from shapely.geometry import Polygon, box

        rng = random.Random(20260907)
        worst = None
        for trial in range(400):
            ax0, ay0 = rng.uniform(0, 100), rng.uniform(0, 100)
            a = box(ax0, ay0, ax0 + rng.uniform(0.5, 60), ay0 + rng.uniform(0.5, 60))
            if trial % 3 == 0:
                a = rotate(a, rng.uniform(0, 90), origin="centroid")
            bx0, by0 = rng.uniform(0, 100), rng.uniform(0, 100)
            b = box(bx0, by0, bx0 + rng.uniform(0.5, 60), by0 + rng.uniform(0.5, 60))
            if trial % 4 == 0:
                b = rotate(b, rng.uniform(0, 90), origin="centroid")
            bound = cam_replay._bounds_gap(a.bounds, b.bounds)
            truth = a.distance(b)
            if bound > truth + 1e-12 and (worst is None or bound - truth > worst[0]):
                worst = (bound - truth, bound, truth, trial)
        assert worst is None, (
            "_bounds_gap exceeded the true distance by %.6f mm "
            "(bound %.6f, truth %.6f, trial %d)" % worst)

    def test_the_bounds_gap_agrees_for_axis_aligned_boxes(self):
        from shapely.geometry import box
        a = box(0, 0, 10, 10)
        b = box(23, 4, 33, 14)
        assert abs(cam_replay._bounds_gap(a.bounds, b.bounds) - 13.0) < 1e-12

    def test_the_bounds_gap_is_zero_for_a_box_inside_another(self):
        from shapely.geometry import box
        outer = box(0, 0, 100, 100)
        inner = box(40, 40, 60, 60)
        assert cam_replay._bounds_gap(outer.bounds, inner.bounds) == 0.0

    def test_the_bound_prune_agrees_with_brute_force(self):
        parts = [_box_part("a", 0, 0, 10, 10),
                 _box_part("b", 100, 0, 110, 10),
                 _box_part("c", 200, 0, 210, 10),
                 _box_part("d", 13, 0, 23, 10),
                 _box_part("e", 150, 0, 160, 10)]
        from freecad.nestingworkbench.Tools.Cam import cam_replay as cr
        best = None
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                d = parts[i]._footprint.distance(parts[j]._footprint)
                if best is None or d < best:
                    best = d
        got = cr.tightest_part_gap(
            parts, type("C", (), {"get": lambda _s, p: p._footprint})())
        assert got is not None
        assert abs(got[0] - best) < 1e-12, "%r vs %r" % (got[0], best)
