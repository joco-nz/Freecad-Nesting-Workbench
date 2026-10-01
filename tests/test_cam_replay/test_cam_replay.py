"""Tests for the CAM recipe reader.

Background
----------
`read_recipe` is the read half of replaying a user's CAM setup onto nested
geometry. It classifies nothing -- the user already decided which features
belong in which operation -- so what it must get right is bookkeeping:
telling operations from dressups, capturing the settings without the identity
and computed results, and attaching dressups to the operation they wrap even
when the group lists them out of order.

Each of those has a failure mode that is silent rather than loud, which is why
they are pinned here:

  * Subscripting a dressup's `Base` raises `TypeError`, because a dressup
    holds a single `App::PropertyLink` and an operation holds a list. The first
    version of the reader did this and crashed on the first real job.

  * `Operations.Group` is NOT the dependency order. On a live job it read
    `['DressupLeadInOut', 'Profile', 'Drilling']` -- the dressup ahead of the
    operation it wraps. A single-pass reader raises `KeyError`. This is the
    ordinary case, not an edge case.

  * Capture must skip identity and result properties. Copying `Base` across
    would point a new operation at the old geometry; copying `Path` would carry
    a stale toolpath.

These run under plain CPython against stand-in objects (tests/conftest.py
stubs FreeCAD), which is possible because the reader is pure logic over the
shapes its caller supplies -- it touches no FreeCAD API and creates nothing.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import (
    NON_REPLAYABLE_PROPERTIES,
    capture_properties,
    describe_recipe,
    is_dressup,
    is_operation,
    read_recipe,
    resolve_source_object,
)


# -- stand-ins ------------------------------------------------------------
#
# Modelled on the real shapes: an operation's `Base` is a list of
# (geometry, subs) tuples, a dressup's `Base` is a bare object reference.

class _PathObject:
    """Anything a dressup can point at."""

    def __init__(self, type_id="Path::Feature"):
        self._type_id = type_id

    def isDerivedFrom(self, type_id):  # noqa: N802 - mirrors the FreeCAD API
        return self._type_id == type_id

    def getTypeId(self):  # noqa: N802
        return self._type_id


class _Geometry:
    """A plain model object, as a job's Model group holds."""

    def __init__(self, label="Bracket"):
        self.Label = label

    def isDerivedFrom(self, type_id):  # noqa: N802
        return False

    def getTypeId(self):  # noqa: N802
        return "Part::Feature"


class _PropertyBag:
    """A scripted object carrying App properties.

    Models the real API's separation of concerns, which the first version of
    these fakes got wrong: `getTypeIdOfProperty(name)` returns the property's
    *type string*, while `getattr(obj, name)` returns its *value*. Conflating
    the two made `capture_properties` call `.startswith` on a tool object.
    """

    def __init__(self, properties=None):
        # {name: (type_id, value)}
        self._properties = dict(properties or {})

    @property
    def PropertiesList(self):  # noqa: N802
        return list(self._properties)

    def getTypeIdOfProperty(self, name):  # noqa: N802
        return self._properties[name][0]

    def __getattr__(self, name):
        # Only reached for names not found the normal way.
        try:
            return object.__getattribute__(self, "_properties")[name][1]
        except KeyError:
            raise AttributeError(name)


class _FakeOp(_PropertyBag):
    """An operation: `Base` is a LinkSubList, i.e. a Python list."""

    def __init__(self, label="Profile", base=None, properties=None):
        super().__init__(properties)
        self.Label = label
        self.Base = list(base) if base is not None else []

    def isDerivedFrom(self, type_id):  # noqa: N802
        return type_id == "Path::Feature"

    def getTypeId(self):  # noqa: N802
        return "Path::FeaturePython"


class _FakeDressup(_PropertyBag):
    """A dressup: `Base` is a single link, NOT subscriptable."""

    def __init__(self, label="DressupLeadInOut", base=None, properties=None):
        super().__init__(properties)
        self.Label = label
        self.Base = base

    def isDerivedFrom(self, type_id):  # noqa: N802
        return type_id == "Path::Feature"

    def getTypeId(self):  # noqa: N802
        return "Path::FeaturePython"


class _FakeOperationsGroup:
    def __init__(self, group):
        self.Group = list(group)


class _FakeJob:
    def __init__(self, group, label="Job"):
        self.Operations = _FakeOperationsGroup(group)
        self.Label = label


# -- classification -------------------------------------------------------

class TestIsDressup:
    def test_dressup_recognised(self):
        op = _FakeOp()
        assert is_dressup(_FakeDressup(base=op)) is True

    def test_operation_recognised(self):
        geo = _Geometry()
        assert is_dressup(_FakeOp(base=[(geo, ["Face1"])])) is False

    def test_operation_with_empty_base_is_an_operation(self):
        # Nothing wrapped means nothing to attach; the recoverable reading.
        assert is_dressup(_FakeOp(base=[])) is False

    def test_missing_base_is_an_operation(self):
        op = _FakeOp()
        del op.Base
        assert is_dressup(op) is False

    def test_dressup_pointing_at_non_path_is_an_operation(self):
        # A Base that is a bare object but not a Path object is not a dressup.
        assert is_dressup(_FakeDressup(base=_Geometry())) is False

    def test_does_not_raise_on_dressup_base(self):
        # The original bug: subscripting a dressup's single link.
        dressup = _FakeDressup(base=_FakeOp())
        with pytest.raises(TypeError):
            dressup.Base[0]
        assert is_dressup(dressup) is True

    def test_operation_and_dressup_are_complementary(self):
        geo = _Geometry()
        op = _FakeOp(base=[(geo, ["Face1"])])
        assert is_operation(op) is True
        assert is_operation(_FakeDressup(base=op)) is False


# -- property capture -----------------------------------------------------

class TestCaptureProperties:
    def test_captures_settings(self):
        op = _FakeOp(properties={
            "Side": ("App::PropertyEnumeration", "Outside"),
            "UseComp": ("App::PropertyBool", True),
            "FinalDepth": ("App::PropertyDistance", -3.0),
        })
        captured = capture_properties(op)
        assert captured["Side"] == "Outside"
        assert captured["UseComp"] is True
        assert captured["FinalDepth"] == -3.0

    def test_excludes_identity_and_result_properties(self):
        op = _FakeOp(properties={
            "Side": ("App::PropertyEnumeration", "Outside"),
            "Base": ("App::PropertyLinkSubList", []),
            "Path": ("App::PropertyPythonObject", None),
            "Proxy": ("App::PropertyPythonObject", None),
            "Label": ("App::PropertyString", "Profile"),
            "Label2": ("App::PropertyString", ""),
            "Visibility": ("App::PropertyBool", True),
            "ExpressionEngine": ("App::PropertyExpressionEngine", []),
        })
        captured = capture_properties(op)
        assert "Side" in captured
        for excluded in NON_REPLAYABLE_PROPERTIES:
            assert excluded not in captured

    def test_keeps_tool_controller_so_the_write_half_can_remap_it(self):
        # A tool controller is a job-local link: useless across jobs, but the
        # write half needs to know one was selected in order to remap it.
        tc = _PathObject()
        op = _FakeOp(properties={"ToolController": ("App::PropertyLink", tc)})
        assert capture_properties(op)["ToolController"] is tc

    def test_skips_underscored_properties(self):
        op = _FakeOp(properties={
            "Side": ("App::PropertyEnumeration", "Outside"),
            "_geom_transform_matrix": ("App::PropertyPythonObject", None),
        })
        assert "_geom_transform_matrix" not in capture_properties(op)

    def test_skips_non_app_properties(self):
        op = _FakeOp(properties={
            "Side": ("App::PropertyEnumeration", "Outside"),
            "Custom": ("Part::PropertyThing", 1),
        })
        assert "Custom" not in capture_properties(op)

    def test_survives_a_property_whose_type_cannot_be_read(self):
        op = _FakeOp(properties={"Side": "App::PropertyEnumeration"})
        op.getTypeIdOfProperty = lambda name: (_ for _ in ()).throw(Exception("boom"))
        assert capture_properties(op) == {}  # skipped, not raised


# -- the recipe -----------------------------------------------------------

class TestReadRecipe:
    def test_empty_job_yields_empty_recipe(self):
        recipe = read_recipe(_FakeJob([]))
        assert len(recipe) == 0
        assert recipe.unresolved_dressups == []

    def test_job_without_operations_attribute(self):
        job = _FakeJob([])
        del job.Operations
        assert len(read_recipe(job)) == 0

    def test_reads_operations_in_group_order(self):
        a, b, c = _FakeOp("A"), _FakeOp("B"), _FakeOp("C")
        recipe = read_recipe(_FakeJob([a, b, c]))
        assert [op.label for op in recipe] == ["A", "B", "C"]

    def test_reads_a_step_from_a_dressup_alone(self):
        # The shape of a real job. A dressed operation is NOT listed; the
        # outermost dressup is, and the operation is reached through the link.
        # A reader that looks only at the list sees nothing at all here -- which
        # is what it did: 0 operations read from a 7-operation fixture.
        op = _FakeOp("Profile")
        dressup = _FakeDressup("DressupLeadInOut", base=op)
        recipe = read_recipe(_FakeJob([dressup]))
        assert [item.label for item in recipe] == ["Profile"]
        assert recipe.operations[0].dressup_labels == ["DressupLeadInOut"]
        assert recipe.operations[0].dressups[0].source is dressup

    def test_a_step_read_from_a_dressup_carries_the_operations_base(self):
        # The whole mechanism depends on this: the sub-element names come off
        # the operation at the bottom of the stack.
        geo = _Geometry()
        op = _FakeOp("Profile", base=[(geo, ["Edge4", "Edge7"])])
        recipe = read_recipe(_FakeJob([_FakeDressup("Lead", base=op)]))
        entries = recipe.operations[0].base_entries
        assert len(entries) == 1 and entries[0][0] is geo
        assert entries[0][1] == ["Edge4", "Edge7"]

    def test_a_two_deep_stack_reads_innermost_first(self):
        # Build order and read order are both innermost-first. FreeCAD's own
        # dressuptest.FCStd has a three-deep one.
        op = _FakeOp("Profile")
        lead = _FakeDressup("LeadInOut", base=op)
        bone = _FakeDressup("Dogbone", base=lead)
        recipe = read_recipe(_FakeJob([bone]))
        assert recipe.operations[0].dressup_labels == ["LeadInOut", "Dogbone"]

    def test_a_three_deep_stack_reads_innermost_first(self):
        op = _FakeOp("Profile")
        lead = _FakeDressup("LeadInOut", base=op)
        bone = _FakeDressup("Dogbone", base=lead)
        bound = _FakeDressup("Boundary", base=bone)
        recipe = read_recipe(_FakeJob([bound]))
        assert recipe.operations[0].dressup_labels == [
            "LeadInOut", "Dogbone", "Boundary"]

    def test_steps_keep_group_order_across_bare_and_dressed(self):
        recipe = read_recipe(_FakeJob([
            _FakeOp("Drilling"),
            _FakeDressup("Lead", base=_FakeOp("ProfileA")),
            _FakeOp("Drilling001"),
        ]))
        assert [item.label for item in recipe] == [
            "Drilling", "ProfileA", "Drilling001"]

    def test_a_redundant_operation_listing_is_dropped_not_reproduced(self):
        # Creating an operation registers it in the list, so a job built by
        # adding operations and then dressing them up lists both. Replaying
        # both would cut the same contour twice.
        op = _FakeOp("Profile")
        dressup = _FakeDressup("Lead", base=op)
        recipe = read_recipe(_FakeJob([op, dressup]))
        assert len(recipe) == 1
        assert recipe.normalised_entries == [op]

    def test_the_dropped_listing_takes_the_dressups_position(self):
        # Order is the job's process order, and a drop must not move a step.
        op = _FakeOp("Profile")
        recipe = read_recipe(_FakeJob([
            _FakeOp("Drilling"), op, _FakeDressup("Lead", base=op)]))
        assert [item.label for item in recipe] == ["Drilling", "Profile"]

    def test_a_dressup_over_an_unlisted_operation_is_not_unresolved(self):
        # An operation missing from the list is the NORMAL case, not a fault.
        # Reporting it as unresolvable is what made the fixture look empty.
        op = _FakeOp("DeletedOp")
        recipe = read_recipe(_FakeJob([_FakeDressup("Orphan", base=op)]))
        assert recipe.unresolved_dressups == []
        assert [item.label for item in recipe] == ["DeletedOp"]

    def test_two_dressups_on_one_operation_take_the_first_and_report(self):
        # Neither chain says it wraps the other, so there is no stack to read.
        # Applying both to one operation is not what either meant.
        op = _FakeOp("Profile")
        d1 = _FakeDressup("Lead", base=op)
        d2 = _FakeDressup("Boundary", base=op)
        recipe = read_recipe(_FakeJob([d1, d2]))
        assert len(recipe) == 1
        assert recipe.operations[0].dressup_labels == ["Lead"]
        assert recipe.normalised_entries == [d2]

    def test_an_unset_base_makes_a_dressup_indistinguishable_from_an_operation(self):
        # A documented limitation, pinned rather than left to be rediscovered.
        # `is_dressup` tells a dressup from an operation by the TYPE of Base --
        # a single link versus a link-sub-list. With Base unset there is nothing
        # to tell by, and the object reads as a bare operation.
        #
        # In a real file this cannot arise: every dressup proxy adds Base as a
        # required property, so an unset Base is a half-built object rather than
        # a saved one.
        orphan = _FakeDressup("Orphan", base=None)
        recipe = read_recipe(_FakeJob([_FakeOp("Profile"), orphan]))
        assert [item.label for item in recipe] == ["Profile", "Orphan"]
        assert recipe.unresolved_dressups == []

    def test_a_cyclic_stack_is_unresolved_rather_than_looping_forever(self):
        # Base is an ordinary link, so a file can say A wraps B and B wraps A.
        a = _FakeDressup("A", base=None)
        b = _FakeDressup("B", base=a)
        a.Base = b
        recipe = read_recipe(_FakeJob([a]))
        assert recipe.unresolved_dressups == [a]
        assert len(recipe) == 0

    def test_preserves_base_entries(self):
        geo = _Geometry()
        op = _FakeOp("Profile", base=[(geo, ["Face1", "Face2"])])
        recipe = read_recipe(_FakeJob([op]))
        entries = recipe.operations[0].base_entries
        assert len(entries) == 1
        assert entries[0][0] is geo
        assert entries[0][1] == ["Face1", "Face2"]

    def test_base_entries_are_copied_not_aliased(self):
        # A later reassignment on the source op must not mutate the recipe.
        geo = _Geometry()
        subs = ["Face1"]
        op = _FakeOp("Profile", base=[(geo, subs)])
        recipe = read_recipe(_FakeJob([op]))
        subs.append("Face2")
        op.Base = []
        assert recipe.operations[0].base_entries[0][1] == ["Face1"]

    def test_iteration_and_len_agree(self):
        recipe = read_recipe(_FakeJob([_FakeOp("A"), _FakeOp("B")]))
        assert len(list(recipe)) == len(recipe) == 2


# -- clone resolution -----------------------------------------------------

class TestResolveSourceObject:
    class _Clone:
        def __init__(self, objects):
            self.Objects = list(objects)

    def test_returns_original_from_clone(self):
        original = _Geometry("Bracket")
        assert resolve_source_object(self._Clone([original])) is original

    def test_returns_first_when_several(self):
        first, second = _Geometry("A"), _Geometry("B")
        assert resolve_source_object(self._Clone([first, second])) is first

    def test_none_for_clone_without_objects(self):
        assert resolve_source_object(self._Clone([])) is None

    def test_none_for_object_without_the_attribute(self):
        assert resolve_source_object(_Geometry()) is None

    def test_none_for_none(self):
        assert resolve_source_object(None) is None


# -- reporting ------------------------------------------------------------

class TestDescribeRecipe:
    def test_describes_operations(self):
        geo = _Geometry()
        recipe = read_recipe(_FakeJob([_FakeOp("Profile", base=[(geo, ["Face1"])])]))
        text = "\n".join(describe_recipe(recipe))
        assert "1. Profile" in text
        assert "base: 1 entry" in text

    def test_lists_a_dressup_stack_innermost_first(self):
        op = _FakeOp("Profile")
        lead = _FakeDressup("LeadInOut", base=op)
        recipe = read_recipe(_FakeJob([_FakeDressup("Dogbone", base=lead)]))
        text = "\n".join(describe_recipe(recipe))
        assert "1. Profile" in text
        assert "[LeadInOut -> Dogbone]" in text

    def test_warns_about_unresolved_dressup(self):
        # Reachable only via a cycle; see the unset-Base note in TestReadRecipe.
        a = _FakeDressup("A", base=None)
        b = _FakeDressup("B", base=a)
        a.Base = b
        recipe = read_recipe(_FakeJob([a]))
        assert "WARNING" in "\n".join(describe_recipe(recipe))

    def test_says_a_redundant_listing_was_dropped(self):
        # The replayed job is shaped differently from the source, and the user
        # should be able to see that it was deliberate.
        op = _FakeOp("Profile")
        recipe = read_recipe(_FakeJob([op, _FakeDressup("Lead", base=op)]))
        text = "\n".join(describe_recipe(recipe))
        assert "not cut twice" in text
        assert "Profile" in text

    def test_reports_an_empty_job_without_raising(self):
        text = "\n".join(describe_recipe(read_recipe(_FakeJob([]))))
        assert "No operations" in text

    def test_pluralises_base_entries(self):
        geo = _Geometry()
        op = _FakeOp("P", base=[(geo, ["Face1"]), (geo, ["Face2"])])
        text = "\n".join(describe_recipe(read_recipe(_FakeJob([op]))))
        assert "base: 2 entries" in text

    def test_lines_are_plain_text(self):
        # Must stay renderable in the Report view without a GUI.
        recipe = read_recipe(_FakeJob([_FakeOp("Profile")]))
        assert all(isinstance(line, str) for line in describe_recipe(recipe))


class TestDressupTablesAgree:
    """The constructor table and the ViewProvider table must cover the same set.

    Both are keyed by proxy module and both are hand-written, which means a
    dressup added to one and not the other is the easy mistake. The consequence
    is quiet and only visible in a GUI: the dressup builds and cuts, and its
    base operation appears at the document root because nothing claims it.
    That is exactly the defect a manual run found and no test in this repository
    can see, because a ViewProvider does not exist under `freecadcmd`.

    So this is the only guard available on the one part of the table that the
    harness structurally cannot check.
    """
    def test_every_replayable_dressup_has_a_view_provider(self):
        missing = [m for m in cam_replay.DRESSUP_BUILDERS
                   if m not in cam_replay.DRESSUP_VIEWPROVIDERS]
        assert missing == [], (
            "dressups replayable but with no ViewProvider entry: %s. They will "
            "build and cut, but the tree will not nest their base operation "
            "under them and double-click will not open their dialog." % missing)

    def test_no_view_provider_without_a_constructor(self):
        extra = [m for m in cam_replay.DRESSUP_VIEWPROVIDERS
                 if m not in cam_replay.DRESSUP_BUILDERS]
        assert extra == [], (
            "ViewProvider entries for dressups this module does not build: %s"
            % extra)

    def test_unsupported_dressups_are_not_claimed_as_replayable(self):
        # A dressup reported as unsupported must not also have a builder, or
        # the two tables disagree about whether it replays.
        overlap = [m for m in cam_replay.DRESSUP_UNSUPPORTED
                   if m in cam_replay.DRESSUP_BUILDERS]
        assert overlap == [], overlap

    def test_set_view_provider_is_a_no_op_without_a_view(self):
        # The headless path, and the one every test in this repository takes.
        # It must return False rather than raise, or the harness breaks.
        obj = type("_O", (), {
            "ViewObject": None,
            "Label": "x",
            "Proxy": None,
        })()
        assert cam_replay.set_view_provider(obj) is False
