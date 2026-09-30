"""Tests for flattening nested containers into CAM-referenced geometry.

Background
----------
CAM cannot see a part's placement while it sits inside an `App::Part`
container. Measured: an operation whose `Base` pointed at a child inside a
container produced a toolpath at the child's *local* coordinates -- `[-22.5,
22.5]` where the container placement put it at `~[118, 160]`. So nested parts
have to be flattened to top-level objects before any operation can reference
them.

The transform is applied as a `Placement` rather than `transformGeometry`, and
that is the load-bearing choice here. `transformGeometry` converts a
`Cylinder` face into a `BSplineSurface` and shifted face area 314.1593 ->
315.0023. Lead-in and lead-out arcs are computed against the real surface, so a
splined cylinder means the toolpath follows an approximation of what the user
drew. A `Placement` leaves the surface analytic.

The second thing flattening must not break is topology. Operations address
geometry by sub-element name, so `Edge7` on a source part has to mean the same
feature on every copy. A nested copy has identical topology to its source, and
a `Placement` preserves that; `transformGeometry` preserves face count but
changes the surfaces underneath the names.

Most of this file is pure logic over stand-ins and runs under plain pytest.
The geometry-preservation checks need real shapes and live in the freecadcmd
harness (tests/freecad_harness), because a stand-in cannot tell you whether a
face is still a `Cylinder`.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import (
    PART_LABEL_PREFIX,
    PROP_NESTED_LABEL,
    PROP_SOURCE_CONTAINER,
    PROP_SOURCE_OBJECT,
    combined_placement,
    find_part_in_container,
    flatten_container,
    z_offset_for_thickness,
)


# -- stand-ins ------------------------------------------------------------

class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z

    def __repr__(self):
        return "Vector(%g, %g, %g)" % (self.x, self.y, self.z)


class _Rotation:
    def __init__(self, angle=0.0):
        self.angle = angle


class _Placement:
    """Enough of FreeCAD.Placement to test the placement arithmetic."""

    def __init__(self, base=None, rotation=None):
        self.Base = base or _Vector()
        self.Rotation = rotation or _Rotation()

    def multiply(self, other):
        # Order-sensitive on purpose: container * child is the documented
        # order, and a reversed product would silently place parts wrong.
        return _Placement(
            _Vector(self.Base.x + other.Base.x,
                    self.Base.y + other.Base.y,
                    self.Base.z + other.Base.z),
            _Rotation(self.Rotation.angle + other.Rotation.angle),
        )


class _Shape:
    def __init__(self, zmin=0.0, zmax=6.0, null=False):
        self._zmin, self._zmax, self._null = zmin, zmax, null
        self.Placement = _Placement()
        self.BoundBox = type("_BB", (), {"ZMin": zmin, "ZMax": zmax})()
        self.copied = 0

    def isNull(self):  # noqa: N802
        return self._null

    def copy(self):
        self.copied += 1
        return _Shape(self._zmin, self._zmax, self._null)


class _Obj:
    """A stand-in document object that accepts dynamic properties.

    `addProperty` mirrors FreeCAD's: declare, then assign. Real objects allow a
    property to be set exactly this way, and the module relies on it, so the
    stand-in has to as well.

    The declared type is recorded in `_declared_types` so a test can assert
    *which* property type was used. That is not pedantry: a plain
    `App::PropertyLink` from a top-level feature to something inside an
    `App::Part` container is out of scope, and FreeCAD warns on every recompute.
    The freecadcmd harness pins the real behaviour; this lets the choice be
    pinned here too, where it runs in the fast tier.
    """

    def __init__(self, label, shape=None, placement=None):
        self.Label = label
        self.Shape = shape
        self.Placement = placement or _Placement()
        self._added = {}
        self._declared_types = {}

    def addProperty(self, type_id, name, group, doc):  # noqa: N802
        self._added[name] = (type_id, group, doc)
        self._declared_types[name] = type_id
        setattr(self, name, None)

    def getTypeIdOfProperty(self, name):  # noqa: N802
        return self._declared_types[name]

    def __getattr__(self, name):
        # Properties declared via addProperty read back as None until assigned,
        # exactly as a freshly declared FreeCAD property does.
        try:
            added = object.__getattribute__(self, "_added")
        except AttributeError:
            raise AttributeError(name)
        if name in added:
            return None
        raise AttributeError(name)


class _Container(_Obj):
    def __init__(self, label, children=(), placement=None):
        super().__init__(label, placement=placement)
        self.Group = list(children)


class _FakeDoc:
    def __init__(self):
        self.Objects = []
        self._n = 0

    def addObject(self, type_id, name):  # noqa: N802
        self._n += 1
        obj = _Obj(name)
        self.Objects.append(obj)
        return obj


def _monkeypatch_placement(monkeypatch):
    """Point the module's FreeCAD at the stand-ins."""
    fake = type("_F", (), {
        "Placement": _Placement,
        "Vector": _Vector,
    })()
    monkeypatch.setattr(cam_replay, "FreeCAD", fake)


# -- finding the part -----------------------------------------------------

class TestFindPartInContainer:
    def test_finds_the_part_child(self):
        part = _Obj("part_Bracket_1")
        container = _Container("nested_Bracket_1", [part, _Obj("boundary_x")])
        assert find_part_in_container(container) is part

    def test_ignores_siblings_that_are_not_parts(self):
        # The prefix carries an underscore so it cannot match these.
        container = _Container("nested_x", [
            _Obj("boundary_Bracket_1"), _Obj("bound_Bracket_1"), _Obj("label_x"),
        ])
        assert find_part_in_container(container) is None

    def test_returns_first_when_several_match(self):
        first = _Obj("part_Bracket_1")
        container = _Container("nested_x", [first, _Obj("part_Bracket_2")])
        assert find_part_in_container(container) is first

    def test_none_for_an_empty_container(self):
        assert find_part_in_container(_Container("nested_x", [])) is None

    def test_none_when_the_container_has_no_group(self):
        container = _Container("nested_x", [])
        del container.Group
        assert find_part_in_container(container) is None


# -- placement ------------------------------------------------------------

class TestCombinedPlacement:
    def test_multiplies_container_by_child(self):
        container = _Container("c", placement=_Placement(_Vector(60, 0, 0), _Rotation(37)))
        part = _Obj("p", placement=_Placement(_Vector(1, 2, 3), _Rotation(5)))
        result = combined_placement(container, part)
        assert (result.Base.x, result.Base.y, result.Base.z) == (61, 2, 3)
        assert result.Rotation.angle == 42

    def test_order_matters(self):
        # Reversing the product is a real bug, not a cosmetic one.
        container = _Container("c", placement=_Placement(_Vector(60, 0, 0), _Rotation(37)))
        part = _Obj("p", placement=_Placement(_Vector(1, 2, 3), _Rotation(5)))
        assert (container.Placement.multiply(part.Placement).Base
                != part.Placement.multiply(container.Placement).Base)

    def test_missing_placements_default_to_identity(self):
        container = _Obj("c")
        part = _Obj("p")
        result = combined_placement(container, part)
        assert (result.Base.x, result.Base.y, result.Base.z) == (0, 0, 0)

    def test_container_only(self):
        container = _Obj("c", placement=_Placement(_Vector(60, 0, 0), _Rotation(37)))
        part = _Obj("p")
        assert combined_placement(container, part).Base.x == 60


# -- Z normalisation ------------------------------------------------------

class TestZOffsetForThickness:
    @pytest.mark.parametrize("zmin, expected", [
        (0.0, -6.0),     # fresh part, bottom at 0
        (-6.0, 0.0),     # already normalised
        (2.0, -8.0),     # floating
    ])
    def test_offsets_bottom_to_minus_thickness(self, zmin, expected):
        shape = _Shape(zmin=zmin, zmax=zmin + 6.0)
        assert z_offset_for_thickness(shape, 6.0) == expected

    def test_is_idempotent(self):
        # Re-running on already-normalised geometry must be a no-op, or a
        # second run would sink the parts through the stock.
        once = z_offset_for_thickness(_Shape(zmin=0.0), 6.0)
        twice = z_offset_for_thickness(_Shape(zmin=0.0 + once), 6.0)
        assert once + twice == once

    def test_none_thickness_is_a_noop(self):
        assert z_offset_for_thickness(_Shape(), None) == 0.0

    def test_zero_thickness_is_a_noop(self):
        assert z_offset_for_thickness(_Shape(zmin=0.0), 0.0) == 0.0


# -- flattening -----------------------------------------------------------

class TestFlattenContainer:
    def test_returns_a_flattened_part(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        part = _Obj("part_Bracket_1", shape=_Shape())
        container = _Container("nested_Bracket_1", [part],
                               placement=_Placement(_Vector(60, 0, 0), _Rotation(37)))
        result = flatten_container(_FakeDoc(), container, 6.0)
        assert result is not None
        assert result.container is container
        assert result.z_offset == -6.0

    def test_none_when_no_part_child(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        container = _Container("nested_x", [_Obj("boundary_x", shape=_Shape())])
        assert flatten_container(_FakeDoc(), container, 6.0) is None

    def test_none_for_a_null_shape(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        part = _Obj("part_x", shape=_Shape(null=True))
        container = _Container("nested_x", [part])
        assert flatten_container(_FakeDoc(), container, 6.0) is None

    def test_none_when_the_shape_is_absent(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        container = _Container("nested_x", [_Obj("part_x", shape=None)])
        assert flatten_container(_FakeDoc(), container, 6.0) is None

    def test_copies_the_shape_rather_than_reusing_it(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        shape = _Shape()
        part = _Obj("part_x", shape=shape)
        container = _Container("nested_x", [part])
        flatten_container(_FakeDoc(), container, 6.0)
        # Reusing the source shape and moving its placement would relocate the
        # part still sitting in the layout.
        assert shape.copied == 1
        assert shape.Placement.Base.z == 0.0

    def test_strips_the_child_placement_before_applying_the_combined_one(
        self, monkeypatch
    ):
        _monkeypatch_placement(monkeypatch)
        part = _Obj("part_x", shape=_Shape(), placement=_Placement(_Vector(5, 5, 5)))
        container = _Container("nested_x", [part], placement=_Placement(_Vector(60, 0, 0)))
        doc = _FakeDoc()
        result = flatten_container(doc, container, 6.0)
        # 5 (child) + 60 (container), not 5 applied twice.
        assert doc.Objects[-1].Shape.Placement.Base.x == 65

    def test_records_identity_links(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        part = _Obj("part_Bracket_1", shape=_Shape())
        container = _Container("nested_Bracket_1", [part])
        result = flatten_container(_FakeDoc(), container, 6.0)
        assert result.obj.SourceObject is part
        assert result.obj.SourceContainer is container
        assert result.obj.NestedLabel == "nested_Bracket_1"

    def test_links_are_declared_as_xlink(self, monkeypatch):
        # A plain App::PropertyLink into an App::Part is out of scope and
        # warns on every recompute; XLink is the type that means "cross-scope".
        _monkeypatch_placement(monkeypatch)
        part = _Obj("part_x", shape=_Shape())
        result = flatten_container(_FakeDoc(), _Container("nested_x", [part]), 6.0)
        for prop in (PROP_SOURCE_OBJECT, PROP_SOURCE_CONTAINER):
            assert result.obj.getTypeIdOfProperty(prop) == "App::PropertyXLink"

    def test_existing_link_property_is_reused_not_redeclared(self, monkeypatch):
        # Flattening twice into the same object must not stack properties.
        _monkeypatch_placement(monkeypatch)
        part = _Obj("part_x", shape=_Shape())
        doc = _FakeDoc()
        first = flatten_container(doc, _Container("nested_x", [part]), 6.0)
        again = flatten_container(doc, _Container("nested_x", [part]), 6.0)
        assert list(first.obj._declared_types) == list(again.obj._declared_types)

    def test_identity_is_a_link_not_a_label(self, monkeypatch):
        # The whole point of PROP_SOURCE_OBJECT: no string matching anywhere.
        _monkeypatch_placement(monkeypatch)
        part = _Obj("part_x", shape=_Shape())
        container = _Container("nested_x", [part])
        result = flatten_container(_FakeDoc(), container, 6.0)
        assert result.obj.SourceObject is part
        assert result.obj.SourceObject is not result.obj.SourceContainer

    def test_applies_the_z_shift_to_the_placement_not_the_geometry(
        self, monkeypatch
    ):
        _monkeypatch_placement(monkeypatch)
        part = _Obj("part_x", shape=_Shape())
        container = _Container("nested_x", [part], placement=_Placement(_Vector(60, 12, 0)))
        doc = _FakeDoc()
        flatten_container(doc, container, 6.0)
        placed = doc.Objects[-1].Shape.Placement
        assert (placed.Base.x, placed.Base.y, placed.Base.z) == (60, 12, -6)

    def test_repr_is_readable(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        part = _Obj("part_x", shape=_Shape())
        result = flatten_container(_FakeDoc(), _Container("nested_x", [part]), 6.0)
        assert "z_offset" in repr(result)


# -- constants ------------------------------------------------------------

class TestConstants:
    def test_part_prefix_cannot_match_siblings(self):
        # A prefix without the underscore would match boundary_/bound_/label_.
        for sibling in ("boundary_x", "bound_x", "label_x", "outline_x"):
            assert not sibling.startswith(PART_LABEL_PREFIX)

    def test_property_names_are_distinct(self):
        names = [PROP_SOURCE_OBJECT, PROP_SOURCE_CONTAINER, PROP_NESTED_LABEL]
        assert len(set(names)) == len(names)
