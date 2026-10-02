"""Tests for the hole-nesting operation order.

Background
----------
The user's own operation order for a part is authoritative and is reproduced
exactly. There is one exception, and it is physical rather than editorial: when
a part is nested *inside* another part's hole, the outer part's hole must be
cut after everything sitting inside it. Cut the hole first and the inner part
falls out — it is held only by the ring of material around it — and every
operation after that point is cutting a loose piece.

So the rule is a partial order, not a re-sort. Everything else keeps the order
the user chose, and the group is not touched at all unless hole nesting
actually happened.

The detection is a trap worth knowing about. It must not be done with
`shape_processor.get_2d_profile_from_obj`, which returns a polygon *centred on
the shape* rather than in world coordinates: a shape spanning X 100..120 yields
a profile spanning -10..10. Comparing that against world positions gives a
confident wrong answer — a part sitting well outside a hole reads as inside it.
`part_footprint` slices the shape instead and keeps world coordinates.
"""
import pytest

from freecad.nestingworkbench.Tools.Cam import cam_replay
from freecad.nestingworkbench.Tools.Cam.cam_replay import (
    find_hole_nestings,
    operation_touches_hole,
    order_operations,
    part_footprint,
)


# -- ordering -------------------------------------------------------------

class _Operation:
    def __init__(self, label, base=None):
        self.Label = label
        self.Base = base if base is not None else []


class _Part:
    def __init__(self, label, footprint=None):
        self.Label = label
        self._footprint = footprint


class _Job:
    def __init__(self, group):
        self.Operations = type("_O", (), {"Group": list(group)})()


def _hole_fp(outer_bounds, hole_bounds):
    from shapely.geometry import Polygon
    x0, y0, x1, y1 = outer_bounds
    hx0, hy0, hx1, hy1 = hole_bounds
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)],
                   [[(hx0, hy0), (hx1, hy0), (hx1, hy1), (hx0, hy1)]])


def _plain_fp(bounds):
    from shapely.geometry import Polygon
    x0, y0, x1, y1 = bounds
    return Polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


def test_reorders_so_the_hole_is_cut_after_what_it_holds(monkeypatch):
    plate = _Part("Plate")
    plug = _Part("Plug")
    hole_op = _Operation("PlateHole")
    plug_op = _Operation("PlugOutline")
    # Whole-object selections, so both are conservatively hole-cutting.
    monkeypatch.setattr(cam_replay, "operation_touches_hole", lambda *a: True)

    nestings = [(plate, plug)]
    ownership = {hole_op: plate, plug_op: plug}
    ordered = order_operations(_Job([hole_op, plug_op]),
                               [hole_op, plug_op], nestings, ownership)
    assert [o.Label for o in ordered] == ["PlugOutline", "PlateHole"]


def test_leaves_the_group_alone_without_nestings(monkeypatch):
    a, b = _Operation("A"), _Operation("B")
    job = _Job([a, b])
    monkeypatch.setattr(cam_replay, "operation_touches_hole", lambda *a: True)
    ordered = order_operations(job, [a, b], [], {a: _Part("x"), b: _Part("y")})
    assert ordered == [a, b]
    assert job.Operations.Group == [a, b]


def test_leaves_the_group_alone_when_nothing_is_hole_cutting(monkeypatch):
    a, b = _Operation("A"), _Operation("B")
    job = _Job([a, b])
    monkeypatch.setattr(cam_replay, "operation_touches_hole", lambda *a: False)
    ordered = order_operations(job, [a, b], [(_Part("x"), _Part("y"))],
                               {a: _Part("x"), b: _Part("y")})
    assert ordered == [a, b]
    assert job.Operations.Group == [a, b]


def test_preserves_the_user_order_where_the_constraints_allow(monkeypatch):
    """The user's sequence survives wherever nothing forces a move.

    Three operations on the outer part, one on the inner. Only the two that
    cut the hole have to move behind the inner operation; the third, which
    does not touch the hole, keeps its place.
    """
    outer, inner = _Part("Outer"), _Part("Inner")
    perimeter = _Operation("Perimeter")     # does not cut the hole
    hole_b = _Operation("HoleB")
    hole_c = _Operation("HoleC")
    inner_op = _Operation("InnerCut")

    monkeypatch.setattr(
        cam_replay, "operation_touches_hole",
        # `cache` is a third positional argument, passed so ordering can share
        # the footprints `find_hole_nestings` already sliced.
        lambda op, part, cache=None: op.Label in ("HoleB", "HoleC"))

    operations = [perimeter, hole_b, hole_c, inner_op]
    ownership = {perimeter: outer, hole_b: outer, hole_c: outer, inner_op: inner}
    ordered = order_operations(_Job(operations), operations,
                               [(outer, inner)], ownership)
    labels = [o.Label for o in ordered]

    assert labels.index("InnerCut") < labels.index("HoleB")
    assert labels.index("InnerCut") < labels.index("HoleC")
    # The perimeter is unconstrained, so it stays first where the user put it.
    assert labels[0] == "Perimeter"


def test_terminates_on_a_cycle(monkeypatch):
    # Each part nesting inside the other is unsatisfiable; the sort must not
    # loop and must not drop operations.
    a, b = _Operation("A"), _Operation("B")
    monkeypatch.setattr(cam_replay, "operation_touches_hole", lambda *a: True)
    pa, pb = _Part("A"), _Part("B")
    ordered = order_operations(_Job([a, b]), [a, b], [(pa, pb), (pb, pa)],
                               {a: pa, b: pb})
    assert len(ordered) == 2


def test_handles_an_empty_job():
    assert order_operations(_Job([]), [], [], {}) == []


def test_handles_a_job_with_no_operations_group(monkeypatch):
    # No group to reorder: returns the input rather than raising.
    monkeypatch.setattr(cam_replay, "operation_touches_hole", lambda *a: True)
    job = type("_J", (), {"Operations": None})()
    a = _Operation("A")
    assert order_operations(job, [a], [(_Part("x"), _Part("y"))], {}) == [a]


# -- hole detection -------------------------------------------------------

class TestFindHoleNestings:
    def test_finds_a_part_inside_a_hole(self, monkeypatch):
        plate = _Part("Plate")
        plug = _Part("Plug")
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k: {
            "Plate": _hole_fp((-40, -40, 40, 40), (-25, -25, 25, 25)),
            "Plug": _plain_fp((-10, -10, 10, 10)),
        }[p.Label])
        assert find_hole_nestings([plate, plug]) == [(plate, plug)]

    def test_ignores_a_part_beside_the_hole(self, monkeypatch):
        plate = _Part("Plate")
        beside = _Part("Beside")
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k: {
            "Plate": _hole_fp((-40, -40, 40, 40), (-25, -25, 25, 25)),
            # Outside the hole entirely: X 50..70.
            "Beside": _plain_fp((50, -10, 70, 10)),
        }[p.Label])
        assert find_hole_nestings([plate, beside]) == []

    def test_does_not_report_a_part_as_nested_in_itself(self, monkeypatch):
        plate = _Part("Plate")
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k:
                            _hole_fp((-40, -40, 40, 40), (-25, -25, 25, 25)))
        assert find_hole_nestings([plate]) == []

    def test_a_part_with_no_holes_never_hosts(self, monkeypatch):
        solid = _Part("Solid")
        plug = _Part("Plug")
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k: {
            "Solid": _plain_fp((-40, -40, 40, 40)),
            "Plug": _plain_fp((-10, -10, 10, 10)),
        }[p.Label])
        assert find_hole_nestings([solid, plug]) == []

    def test_skips_parts_with_no_footprint(self, monkeypatch):
        plate = _Part("Plate")
        unknown = _Part("Unknown")
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k:
                            _hole_fp((-40, -40, 40, 40), (-25, -25, 25, 25))
                            if p is plate else None)
        assert find_hole_nestings([plate, unknown]) == []

    def test_empty_input(self):
        assert find_hole_nestings([]) == []


class _Boxed:
    """A part stand-in that also has a shape, for the bounding-box prune.

    The prune reads `part.Shape.BoundBox`. Without a box a part cannot be
    pruned on evidence, so it is treated as overlapping everything -- which is
    why every other test here still passes without one.
    """

    def __init__(self, label, x0, y0, x1, y1):
        self.Label = label
        box = type("_B", (), {"XMin": x0, "YMin": y0, "XMax": x1,
                              "YMax": y1})()
        shape = type("_S", (), {"BoundBox": box, "isNull": lambda s: False})()
        self.Shape = shape


class TestTheBoundingBoxPrune:
    """The prune exists for speed, so every check here is about soundness.

    Dropping a real nesting would silently produce the wrong operation order,
    which cuts the hole before the part it holds and drops it on the floor.
    That is the failure mode, so the prune is only allowed to be wrong in the
    other direction: keeping a pair that turns out not to nest.
    """

    def test_it_does_not_slice_parts_whose_boxes_are_disjoint(self, monkeypatch):
        calls = []

        def counting(part, **kwargs):
            calls.append(part.Label)
            return _plain_fp((0, 0, 1, 1))

        far = _Boxed("Far", 500, 500, 520, 520)
        near = _Boxed("Near", 0, 0, 20, 20)
        monkeypatch.setattr(cam_replay, "part_footprint", counting)
        assert find_hole_nestings([far, near]) == []
        # One slice at most: the near one is asked for as a possible outer, and
        # with no interiors it cannot host anything.
        assert len(calls) <= 2

    def test_a_nest_with_no_overlapping_boxes_slices_nothing(self, monkeypatch):
        monkeypatch.setattr(
            cam_replay, "part_footprint",
            lambda p, **k: pytest.fail("sliced a part the prune should have "
                                       "removed: %r" % p.Label))
        parts = [_Boxed("A", 0, 0, 10, 10), _Boxed("B", 100, 0, 110, 10),
                 _Boxed("C", 0, 100, 10, 110)]
        assert find_hole_nestings(parts) == []

    def test_it_keeps_a_nesting_whose_boxes_overlap(self, monkeypatch):
        plate = _Boxed("Plate", -40, -40, 40, 40)
        plug = _Boxed("Plug", -10, -10, 10, 10)
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k: {
            "Plate": _hole_fp((-40, -40, 40, 40), (-25, -25, 25, 25)),
            "Plug": _plain_fp((-10, -10, 10, 10)),
        }[p.Label])
        assert find_hole_nestings([plate, plug]) == [(plate, plug)]

    def test_a_part_with_no_readable_box_is_never_pruned(self, monkeypatch):
        # The stand-ins used everywhere else have no `Shape` at all. They must
        # keep working, so an unreadable box means "may overlap anything".
        plate = _Part("Plate")
        plug = _Part("Plug")
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k: {
            "Plate": _hole_fp((-40, -40, 40, 40), (-25, -25, 25, 25)),
            "Plug": _plain_fp((-10, -10, 10, 10)),
        }[p.Label])
        assert find_hole_nestings([plate, plug]) == [(plate, plug)]

    def test_a_null_shape_is_still_consulted_rather_than_pruned(self, monkeypatch):
        # The prune must not treat an unreadable box as "empty" and drop the
        # pair before the footprint is ever asked for. With the footprint
        # stubbed to a real polygon, the pair nests -- which is the point: the
        # decision came from the footprint, not from the missing box.
        nullish = _Part("Null")
        nullish.Shape = type("_S", (), {"isNull": lambda s: True})()
        plate = _Part("Plate")
        asked = []
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k: (
            asked.append(p.Label),
            {"Plate": _hole_fp((-40, -40, 40, 40), (-25, -25, 25, 25)),
             "Null": _plain_fp((-10, -10, 10, 10))}[p.Label])[1])
        assert find_hole_nestings([plate, nullish]) == [(plate, nullish)]
        assert "Null" in asked

    def test_the_prune_does_not_change_the_order_of_results(self, monkeypatch):
        # `order_operations` consumes these in sequence, so a different order
        # for the same nestings could change what it does.
        parts = [_Boxed("P%d" % i, i * 2, 0, i * 2 + 8, 8) for i in range(4)]
        fps = {
            p.Label: (_hole_fp((p.Shape.BoundBox.XMin, -40,
                                p.Shape.BoundBox.XMax, 40), (-5, -5, 5, 5))
                      if p.Label == "P0" else _plain_fp((-2, -2, 2, 2)))
            for p in parts}
        monkeypatch.setattr(cam_replay, "part_footprint",
                            lambda p, **k: fps[p.Label])
        got = find_hole_nestings(parts)
        assert [inner.Label for _o, inner in got] == ["P1", "P2", "P3"]
        assert all(outer.Label == "P0" for outer, _i in got)


class TestFootprintCache:
    """`operation_touches_hole` re-derived footprints `find_hole_nestings` had
    already sliced: 21 calls over 3 distinct shapes, 11.40 s of the 11.71 s that
    ordering took on the committed fixture."""

    def test_it_computes_a_footprint_once_per_part(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cam_replay, "part_footprint",
                            lambda p, **k: calls.append(p.Label)
                            or _plain_fp((0, 0, 1, 1)))
        cache = cam_replay.FootprintCache()
        part = _Part("A")
        for _ in range(5):
            cache.get(part)
        assert calls == ["A"]
        assert cache.misses == 1
        assert cache.hits == 4

    def test_it_caches_a_none_footprint_too(self, monkeypatch):
        # None means "cannot tell", and callers treat it as a distinct answer.
        # Caching it is what stops the same unknown being re-derived.
        calls = []
        monkeypatch.setattr(cam_replay, "part_footprint",
                            lambda p, **k: calls.append(1) or None)
        cache = cam_replay.FootprintCache()
        part = _Part("A")
        assert cache.get(part) is None
        assert cache.get(part) is None
        assert len(calls) == 1

    def test_distinct_parts_get_distinct_footprints(self, monkeypatch):
        monkeypatch.setattr(cam_replay, "part_footprint",
                            lambda p, **k: _plain_fp((0, 0, 1, 1)))
        cache = cam_replay.FootprintCache()
        a, b = _Part("A"), _Part("B")
        assert cache.get(a) is not cache.get(b)
        assert cache.misses == 2

    def test_no_cache_behaves_exactly_as_before(self, monkeypatch):
        monkeypatch.setattr(cam_replay, "part_footprint",
                            lambda p, **k: _plain_fp((0, 0, 1, 1)))
        part = _Part("A")
        assert cam_replay._footprint_for(part, None) is not None

    def test_ordering_hands_the_cache_to_operation_touches_hole(self, monkeypatch):
        # The wiring itself. `cache` is passed positionally because the tests
        # replace this function with a `lambda *a: True`.
        seen = []

        def spy(op, part, cache=None):
            seen.append(cache)
            return True

        outer, inner = _Part("x"), _Part("y")
        a, b = _Operation("A"), _Operation("B")
        cache = cam_replay.FootprintCache()
        monkeypatch.setattr(cam_replay, "operation_touches_hole", spy)
        order_operations(_Job([a, b]), [a, b], [(outer, inner)],
                         {a: outer, b: inner}, cache)
        assert seen and all(c is cache for c in seen)


# -- hole-cutting classification ----------------------------------------

class TestOperationTouchesHole:
    def test_a_whole_object_selection_is_conservatively_a_hole(self, monkeypatch):
        # Internal and external cutting are not separable, and assuming it
        # misses the hole is the silent failure this guards against.
        part = _Part("Plate")
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k: None)
        op = _Operation("P", [(part, [""])])
        assert operation_touches_hole(op, part) is True

    def test_no_base_is_conservatively_a_hole(self, monkeypatch):
        part = _Part("Plate")
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k: None)
        assert operation_touches_hole(_Operation("P"), part) is True

    def test_a_part_with_no_holes_cannot_be_cut_in_one(self, monkeypatch):
        part = _Part("Solid")
        monkeypatch.setattr(cam_replay, "part_footprint", lambda p, **k:
                            _plain_fp((-40, -40, 40, 40)))
        op = _Operation("P", [(part, ["Face1"])])
        assert operation_touches_hole(op, part) is False

    def test_an_unresolvable_sub_element_is_conservatively_a_hole(self, monkeypatch):
        part = _Part("Plate")
        # A footprint WITH a hole, so the test reaches the sub-element lookup
        # rather than short-circuiting on "this part has no holes".
        monkeypatch.setattr(
            cam_replay, "part_footprint", lambda p, **k:
            _hole_fp((-40, -40, 40, 40), (-25, -25, 25, 25)))

        class _Broken:
            @property
            def Shape(self):
                raise Exception("no shape")

        op = _Operation("P", [(_Broken(), ["Face1"])])
        assert operation_touches_hole(op, part) is True

    def test_a_sub_element_inside_a_hole_counts(self, monkeypatch):
        part = _Part("Plate")
        monkeypatch.setattr(
            cam_replay, "part_footprint", lambda p, **k:
            _hole_fp((-40, -40, 40, 40), (-25, -25, 25, 25)))

        class _Element:
            def __init__(self, x, y):
                self.CenterOfMass = type("_C", (), {"x": x, "y": y})()

        class _Geometry:
            class Shape:
                @staticmethod
                def getElement(name):  # noqa: N802
                    return _Element(0.0, 0.0)   # dead centre, inside the hole

        op = _Operation("P", [(_Geometry(), ["Face7"])])
        assert operation_touches_hole(op, part) is True
