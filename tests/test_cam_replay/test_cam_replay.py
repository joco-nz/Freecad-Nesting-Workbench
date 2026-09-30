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

    def test_attaches_dressup_to_its_operation(self):
        a = _FakeOp("Profile")
        dressup = _FakeDressup("DressupLeadInOut", base=a)
        recipe = read_recipe(_FakeJob([a, dressup]))
        assert len(recipe) == 1
        assert len(recipe.operations[0].dressups) == 1
        assert recipe.operations[0].dressups[0][1] is dressup

    def test_attaches_when_dressup_precedes_its_operation(self):
        # The real observed order. A single-pass reader raises KeyError here.
        a = _FakeOp("Profile")
        dressup = _FakeDressup("DressupLeadInOut", base=a)
        recipe = read_recipe(_FakeJob([dressup, a, _FakeOp("Drilling")]))
        assert [op.label for op in recipe] == ["Profile", "Drilling"]
        assert len(recipe.operations[0].dressups) == 1

    def test_multiple_dressups_attach_to_one_operation(self):
        a = _FakeOp("Profile")
        d1 = _FakeDressup("Lead", base=a)
        d2 = _FakeDressup("Boundary", base=a)
        recipe = read_recipe(_FakeJob([d1, a, d2]))
        assert len(recipe.operations) == 1
        assert len(recipe.operations[0].dressups) == 2

    def test_records_rather_than_raises_on_unresolvable_dressup(self):
        orphan = _FakeDressup("Orphan", base=_FakeOp("DeletedOp"))
        recipe = read_recipe(_FakeJob([_FakeOp("Profile"), orphan]))
        assert len(recipe) == 1
        assert len(recipe.unresolved_dressups) == 1
        assert recipe.unresolved_dressups[0] is orphan

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

    def test_lists_dressups(self):
        a = _FakeOp("Profile")
        recipe = read_recipe(_FakeJob([a, _FakeDressup("LeadInOut", base=a)]))
        text = "\n".join(describe_recipe(recipe))
        assert "dressups: LeadInOut" in text

    def test_warns_about_unresolved_dressup(self):
        orphan = _FakeDressup("Orphan", base=_FakeOp("DeletedOp"))
        recipe = read_recipe(_FakeJob([orphan]))
        assert "WARNING" in "\n".join(describe_recipe(recipe))

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
