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
    clones_for_source,
    create_dressup_in,
    create_operation_in,
    describe_replay_result,
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


class TestCheckSubnamesAgainstClones:
    def test_reports_ok_when_every_name_resolves_everywhere(self):
        clones = [_Target("c1", ["Face1"]), _Target("c2", ["Face1"])]
        missing, detail = check_subnames_against_clones(["Face1"], clones)
        assert missing == set()
        assert detail["Face1"] == "ok"

    def test_reports_a_name_that_resolves_nowhere(self):
        clones = [_Target("c1", ["Face1"]), _Target("c2", ["Face1"])]
        missing, detail = check_subnames_against_clones(["Face9"], clones)
        assert missing == {"Face9"}
        assert "0 of 2" in detail["Face9"]

    def test_reports_a_name_that_resolves_on_only_some(self):
        # The alarming case: the same operation would cut a different feature
        # on different parts of one nest.
        clones = [_Target("c1", ["Face1"]), _Target("c2", ["Face2"])]
        missing, detail = check_subnames_against_clones(["Face1"], clones)
        assert missing == set()
        assert "1 of 2" in detail["Face1"]

    def test_treats_the_empty_string_as_always_present(self):
        clones = [_Target("c1", []), _Target("c2", [])]
        missing, detail = check_subnames_against_clones([""], clones)
        assert missing == set()
        assert detail[""] == "whole object"

    def test_no_clones_means_nothing_resolves(self):
        missing, _ = check_subnames_against_clones(["Face1"], [])
        assert missing == {"Face1"}


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
