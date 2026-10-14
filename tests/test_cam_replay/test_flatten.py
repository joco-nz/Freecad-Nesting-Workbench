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
import math
import sys
import types

import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import (
    PART_LABEL_PREFIX,
    PROP_NESTED_LABEL,
    PROP_SOURCE_CONTAINER,
    PROP_SOURCE_OBJECT,
    RIGID_COPY_TOLERANCE_MM,
    CARRIED_POINT_TOLERANCE_MM,
    carried_point_displacement,
    combined_placement,
    find_part_in_container,
    flatten_container,
    source_to_clone_placement,
    z_offset_for_thickness,
)


# -- stand-ins ------------------------------------------------------------

class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        # `FreeCAD.Vector(v)` copies a vector; accepting one here too, or the
        # copy silently becomes a vector whose `.x` is another vector.
        if isinstance(x, _Vector):
            x, y, z = x.x, x.y, x.z
        self.x, self.y, self.z = x, y, z

    def __repr__(self):
        return "Vector(%g, %g, %g)" % (self.x, self.y, self.z)

    def __eq__(self, other):
        # By value. Without this, `!=` on two vectors is an identity test that
        # is always True, and any assertion about placements differing passes
        # for the wrong reason -- which is how `test_order_matters` below came
        # to be vacuous.
        return (self.x, self.y, self.z) == (other.x, other.y, other.z)

    __hash__ = None

    def __add__(self, other):
        return _Vector(self.x + other.x, self.y + other.y, self.z + other.z)

    def sub(self, other):
        return _Vector(self.x - other.x, self.y - other.y, self.z - other.z)

    def negated(self):
        return _Vector(-self.x, -self.y, -self.z)


class _Rotation:
    """A yaw in radians. Z-only, which is all the placement maths here needs."""

    def __init__(self, angle=0.0):
        self.angle = angle

    def inverted(self):
        return _Rotation(-self.angle)

    def multVec(self, v):  # noqa: N802
        c, s = math.cos(self.angle), math.sin(self.angle)
        return _Vector(c * v.x - s * v.y, s * v.x + c * v.y, v.z)


class _Placement:
    """Enough of FreeCAD.Placement to test the placement arithmetic."""

    def __init__(self, base=None, rotation=None):
        self.Base = base or _Vector()
        self.Rotation = rotation or _Rotation()

    def multiply(self, other):
        # Order-sensitive on purpose: container * child is the documented
        # order, and a reversed product would silently place parts wrong.
        #
        # A real composition, so the left factor's rotation is applied to the
        # right factor's offset. Adding the two offsets instead is only right
        # when the left factor is unrotated, and it quietly hid that from the
        # assertions in `TestCombinedPlacement`.
        return _Placement(
            self.Rotation.multVec(other.Base) + self.Base,
            _Rotation(self.Rotation.angle + other.Rotation.angle),
        )

    def inverse(self):
        rotation = self.Rotation.inverted()
        return _Placement(rotation.multVec(self.Base).negated(), rotation)

    def multVec(self, v):  # noqa: N802
        rotated = self.Rotation.multVec(v)
        return _Vector(rotated.x + self.Base.x,
                       rotated.y + self.Base.y,
                       rotated.z + self.Base.z)


class _Shape:
    def __init__(self, zmin=0.0, zmax=6.0, null=False,
                 xmin=0.0, xmax=10.0, ymin=0.0, ymax=20.0):
        self._zmin, self._zmax, self._null = zmin, zmax, null
        self._xmin, self._xmax = xmin, xmax
        self._ymin, self._ymax = ymin, ymax
        self.Placement = _Placement()
        self.BoundBox = type("_BB", (), {
            "XMin": xmin, "XMax": xmax, "YMin": ymin, "YMax": ymax,
            "ZMin": zmin, "ZMax": zmax})()
        self.copied = 0

    def isNull(self):  # noqa: N802
        return self._null

    def copy(self):
        self.copied += 1
        return _Shape(self._zmin, self._zmax, self._null,
                      self._xmin, self._xmax, self._ymin, self._ymax)

    def optimalBoundingBox(self, *args):  # noqa: N802
        # The box is stored in the shape's own frame, which is what the real
        # one answers once the placement has been stripped -- so no argument
        # is needed and none is read.
        return self.BoundBox


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
        # The child's offset is rotated by the container's yaw before the two
        # are added, so it lands at 60 + (cos37 - 2 sin37, sin37 + 2 cos37),
        # not at 60 + (1, 2). This expectation was (61, 2, 3) until the
        # stand-in was made to compose properly; the stand-in was the thing
        # being asserted on, and it disagreed with FreeCAD. Written out
        # rather than pinned, because `_Rotation`'s angle is in radians.
        c, s = math.cos(37), math.sin(37)
        assert result.Base.x == pytest.approx(60 + c - 2 * s, abs=1e-9)
        assert result.Base.y == pytest.approx(s + 2 * c, abs=1e-9)
        assert result.Base.z == pytest.approx(3.0, abs=1e-9)
        assert result.Rotation.angle == 42

    def test_order_matters(self):
        # Reversing the product is a real bug, not a cosmetic one. Only
        # meaningful now that `_Vector` compares by value and `multiply`
        # composes; both of those were hiding this assertion.
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


# -- the geometry-frame bridge ---------------------------------------------

#: A part as the CAM job holds it: local box 10 x 20 x 2, sitting somewhere.
SOURCE_BOX = dict(xmin=0.0, xmax=10.0, ymin=0.0, ymax=20.0, zmin=0.0, zmax=2.0)
#: The nester's frame shift for `SimpleSpacer`, measured on the fixture:
#: (0, +2.089059, -1). The sizes are untouched -- it is a pure translation.
BAKED_SHIFT = (0.0, 2.089059, -1.0)


def _shifted(box, shift):
    dx, dy, dz = shift
    return dict(xmin=box["xmin"] + dx, xmax=box["xmax"] + dx,
                ymin=box["ymin"] + dy, ymax=box["ymax"] + dy,
                zmin=box["zmin"] + dz, zmax=box["zmax"] + dz)


def _centre(box):
    return _Vector((box["xmin"] + box["xmax"]) / 2.0,
                   (box["ymin"] + box["ymax"]) / 2.0,
                   (box["zmin"] + box["zmax"]) / 2.0)


class TestSourceToClonePlacement:
    """The bridge between a part's CAM frame and its nested copy's.

    The nesting side bakes its frame shift into the *geometry*, so the
    placements on either side of this function do not add up to the transform.
    These tests exist because every test that already covered this function
    computed its expectation by calling it -- which is why a 2.087 mm error
    survived the fix for the start point that depends on it.
    """

    def _pair(self, clone_box, source_placement=None, clone_placement=None):
        source = _Obj("Model-Bracket", shape=_Shape(**SOURCE_BOX))
        if source_placement is not None:
            source.Shape.Placement = source_placement
        clone = _Obj("CAMPart_1", shape=_Shape(**clone_box))
        if clone_placement is not None:
            clone.Shape.Placement = clone_placement
        return source, clone

    def test_carries_a_frame_shift_baked_into_the_geometry(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        # Non-identity on both sides on purpose. With either at the origin the
        # missing term is partly a no-op and a wrong transform can look right.
        source, clone = self._pair(
            _shifted(SOURCE_BOX, BAKED_SHIFT),
            source_placement=_Placement(_Vector(5.0, 7.0, 3.0), _Rotation(0.3)),
            clone_placement=_Placement(_Vector(100.0, 200.0, 5.0), _Rotation(1.1)))
        transform = source_to_clone_placement(clone, source)

        got = transform.multVec(
            source.Shape.Placement.multVec(_centre(SOURCE_BOX)))
        want = clone.Shape.Placement.multVec(_centre(_shifted(SOURCE_BOX, BAKED_SHIFT)))
        for axis in "xyz":
            assert abs(getattr(got, axis) - getattr(want, axis)) < 1e-9, axis

    def test_the_placement_only_bridge_is_the_wrong_answer(self, monkeypatch):
        # Stated explicitly, because "the old code was simpler" is not a reason
        # to keep it. The gap between the two is the whole baked shift, which
        # the clone's own rotation turns off the y axis -- so this compares
        # lengths, the one thing a rotation cannot change.
        _monkeypatch_placement(monkeypatch)
        source, clone = self._pair(
            _shifted(SOURCE_BOX, BAKED_SHIFT),
            source_placement=_Placement(_Vector(5.0, 7.0, 3.0)),
            clone_placement=_Placement(_Vector(100.0, 200.0, 5.0), _Rotation(0.8)))
        placement_only = clone.Shape.Placement.multiply(
            source.Shape.Placement.inverse())
        centre = _centre(SOURCE_BOX)
        a = placement_only.multVec(source.Shape.Placement.multVec(centre))
        b = source_to_clone_placement(clone, source).multVec(
            source.Shape.Placement.multVec(centre))
        gap = math.sqrt(sum((getattr(a, k) - getattr(b, k)) ** 2 for k in "xyz"))
        assert gap == pytest.approx(math.sqrt(sum(s * s for s in BAKED_SHIFT)))

    def test_unchanged_when_there_is_no_baked_shift(self, monkeypatch):
        # The common case has to reduce to what it always did, or this fix
        # would be trading one wrong answer for another.
        _monkeypatch_placement(monkeypatch)
        source, clone = self._pair(
            SOURCE_BOX,
            source_placement=_Placement(_Vector(5.0, 7.0, 3.0), _Rotation(0.3)),
            clone_placement=_Placement(_Vector(100.0, 200.0, 5.0), _Rotation(1.1)))
        transform = source_to_clone_placement(clone, source)
        expected = clone.Shape.Placement.multiply(source.Shape.Placement.inverse())
        assert abs(transform.Base.x - expected.Base.x) < 1e-9
        assert abs(transform.Base.y - expected.Base.y) < 1e-9
        assert abs(transform.Base.z - expected.Base.z) < 1e-9
        assert abs(transform.Rotation.angle - expected.Rotation.angle) < 1e-9

    def test_refuses_when_the_two_are_not_the_same_part_moved(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        grown = dict(SOURCE_BOX, xmax=SOURCE_BOX["xmax"] + 1.0)
        source, clone = self._pair(grown)
        # Better a point left alone than one carried to a guess: the caller
        # reports it as stranded, the same channel a dropped point takes.
        assert source_to_clone_placement(clone, source) is None

    def test_tolerates_a_difference_inside_the_tolerance(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        nudged = dict(SOURCE_BOX,
                      xmax=SOURCE_BOX["xmax"] + RIGID_COPY_TOLERANCE_MM / 10.0)
        source, clone = self._pair(nudged)
        assert source_to_clone_placement(clone, source) is not None

    def test_none_without_a_shape_on_either_side(self, monkeypatch):
        _monkeypatch_placement(monkeypatch)
        good = _Shape(**SOURCE_BOX)
        assert source_to_clone_placement(_Obj("c", shape=None),
                                         _Obj("s", shape=good)) is None
        assert source_to_clone_placement(_Obj("c", shape=good),
                                         _Obj("s", shape=None)) is None
        assert source_to_clone_placement(_Obj("c", shape=_Shape(null=True)),
                                         _Obj("s", shape=good)) is None


# -- reporting a displaced carry -------------------------------------------

class _PointOp:
    """An operation stand-in with a start point, and one solid it targets."""

    def __init__(self, label, point, target, in_use=True):
        self.Label = label
        self.StartPoint = point
        self.UseStartPoint = in_use
        self.Base = [(target, [])]


def _install_fake_part(monkeypatch, distances):
    """Stand in for `Part`, reporting a distance per probe coordinate.

    `Part.Vertex` only learns the point, so the stand-in is keyed on it. That
    is enough: it makes the measurement a lookup, which is what makes the
    tolerance decisions below testable at all.
    """
    class _Vertex:
        def __init__(self, x, y, z):
            self.key = (round(x, 9), round(y, 9), round(z, 9))

        def distToShape(self, shape):  # noqa: N802
            return (distances[self.key], None, None)

    fake = types.ModuleType("Part")
    fake.Vertex = _Vertex
    monkeypatch.setitem(sys.modules, "Part", fake)


class TestCarriedPointDisplacement:
    """A carried point must keep its source's distance to its own part.

    The branch that skips the check when the two points sit at different heights
    is the one worth having a test for: it is the branch that turns the warning
    off, and code that can disable a check without saying so is how the last one
    went unnoticed.
    """

    def _pair(self, monkeypatch, own, now, z=(0.0, 0.0)):
        _monkeypatch_placement(monkeypatch)
        _install_fake_part(monkeypatch, {
            (z[0], 1.0, z[1]): own,
            (z[0] + 100.0, 2.0, z[1]): now,
        })
        source = _PointOp("Profile", _Vector(0.0, 1.0, z[0]),
                          _Obj("Model-x", shape=_Shape()), True)
        target = _PointOp("Profile_replay", _Vector(100.0, 2.0, z[1]),
                          _Obj("CAMPart_1", shape=_Shape()), True)
        return source, target

    def test_quiet_when_the_carried_point_agrees(self, monkeypatch):
        source, target = self._pair(monkeypatch, 5.1505, 5.1505)
        assert carried_point_displacement(source, target, "StartPoint") is None

    def test_reports_when_it_does_not(self, monkeypatch):
        source, target = self._pair(monkeypatch, 5.1505, 6.0047)
        note = carried_point_displacement(source, target, "StartPoint")
        assert note is not None
        # The numbers have to be in it: an operator reading this needs to know
        # how far out the point is, not just that something is.
        assert "6.0047" in note and "5.1505" in note and "0.8542" in note
        assert "Profile_replay" in note and "Profile" in note

    def test_a_difference_inside_the_tolerance_is_not_reported(self, monkeypatch):
        source, target = self._pair(monkeypatch, 5.0, 5.0 + CARRIED_POINT_TOLERANCE_MM / 2)
        assert carried_point_displacement(source, target, "StartPoint") is None

    def test_skipped_when_the_two_sit_at_different_heights(self, monkeypatch):
        # A carried Z comes from the copy's own ClearanceHeight, so the heights
        # legitimately differ and a 3D distance would report that instead.
        _monkeypatch_placement(monkeypatch)
        _install_fake_part(monkeypatch, {(0.0, 1.0, 5.0): 5.1505,
                                         (100.0, 2.0, 9.0): 99.0})
        source = _PointOp("Profile", _Vector(0.0, 1.0, 5.0),
                          _Obj("Model-x", shape=_Shape()), True)
        target = _PointOp("Profile_replay", _Vector(100.0, 2.0, 9.0),
                          _Obj("CAMPart_1", shape=_Shape()), True)
        assert carried_point_displacement(source, target, "StartPoint") is None

    def test_quiet_for_a_point_the_operation_does_not_read(self, monkeypatch):
        source, target = self._pair(monkeypatch, 5.1505, 6.0047)
        target.UseStartPoint = False
        assert carried_point_displacement(source, target, "StartPoint") is None

    def test_quiet_when_there_is_no_part_to_measure_against(self, monkeypatch):
        source, target = self._pair(monkeypatch, 5.1505, 6.0047)
        target.Base = []
        assert carried_point_displacement(source, target, "StartPoint") is None
        target.Base = [(None, [])]
        assert carried_point_displacement(source, target, "StartPoint") is None


# -- constants ------------------------------------------------------------

class TestConstants:
    def test_part_prefix_cannot_match_siblings(self):
        # A prefix without the underscore would match boundary_/bound_/label_.
        for sibling in ("boundary_x", "bound_x", "label_x", "outline_x"):
            assert not sibling.startswith(PART_LABEL_PREFIX)

    def test_property_names_are_distinct(self):
        names = [PROP_SOURCE_OBJECT, PROP_SOURCE_CONTAINER, PROP_NESTED_LABEL]
        assert len(set(names)) == len(names)
