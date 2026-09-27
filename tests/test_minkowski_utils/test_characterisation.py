"""Characterisation tests for the real minkowski_utils geometry code.

These replace the earlier hand-written Shapely approximations. The previous
tests exercised stand-ins written inside the test file (`piece.buffer(-0.01)`
standing in for a Minkowski difference, and so on) and were heavily guarded
with `if result is not None:`, so they would have passed whether or not the
workbench's own NFP code was correct. These call the workbench functions
directly and pin their actual output.

Why this matters for the integration work: the areas asserted below are the
"before" numbers. Blocks 2.1 (IFP hole-edge sweeps), 2.2 (polygon
discretisation) and 3.1 (type-keyed caches) all change this code, and the
plan's per-block gate is "neutral or better" against a recorded baseline. This
file is that baseline for the pure-geometry tier.

`tests/conftest.py` installs inert FreeCAD/Part stubs when the real modules
are absent. minkowski_utils uses no FreeCAD API at all, so these tests exercise
the genuine implementation under plain CPython.
"""
import math

import pytest
from shapely.geometry import Polygon

from freecad.nestingworkbench.Tools.Nesting.algorithms import minkowski_utils as mu
from freecad.nestingworkbench.datatypes.shape import Shape


# Canonical corpus. Areas are chosen to be exactly representable so the
# assertions below are about the algorithm, not floating-point drift.
SQUARE_4 = Polygon([(0, 0), (4, 0), (4, 4), (0, 4)])          # area 16
SQUARE_2 = Polygon([(-1, -1), (1, -1), (1, 1), (-1, 1)])        # area 4
TRIANGLE = Polygon([(0, 3), (-2, -1), (2, -1)])                 # area 8
L_SHAPE = Polygon([(0, 0), (4, 0), (4, 1), (1, 1), (1, 4), (0, 4)])   # area 7
CHEVRON = Polygon([(0, 0), (4, 0), (4, 4), (3, 4), (2, 2), (1, 4), (0, 4)])  # area 14

HOLE_SQUARE = Polygon([(6, 6), (14, 6), (14, 14), (6, 14)])    # 8x8, convex, area 64
HOLE_L = Polygon([(6, 6), (14, 6), (14, 10), (10, 10), (10, 14), (6, 14)])  # area 48

AREA_TOL = 1e-6


def noop_logger(*args, **kwargs):
    """Swallows logger output; the workbench only needs a callable."""


@pytest.fixture(autouse=True)
def _isolated_caches():
    """Keeps class-level decomposition state from bleeding between tests.

    decompose_if_needed memoises on polygon WKT, so without this a test could
    observe a neighbour's cached pieces and pass for the wrong reason.
    """
    Shape.clear_caches()
    mu.clear_dead_ring_profiles()
    mu.reset_dead_ring_stats()
    yield
    Shape.clear_caches()
    mu.clear_dead_ring_profiles()
    mu.reset_dead_ring_stats()


class TestDecomposeIfNeeded:
    """Piece counts and the area-conservation invariant."""

    @pytest.mark.parametrize(
        "polygon, expected_pieces",
        [
            (SQUARE_4, 1),    # already convex
            (TRIANGLE, 1),    # already convex
            (L_SHAPE, 4),     # concave
            (CHEVRON, 5),     # concave
        ],
        ids=["square", "triangle", "L_shape", "chevron"],
    )
    def test_piece_count_is_stable(self, polygon, expected_pieces):
        """Pins the piece count the NFP cost model depends on.

        These counts feed the pairwise-sum cost (len(A) x len(B)), so a change
        here is a performance change and must be a deliberate one.
        """
        parts = mu.decompose_if_needed(polygon, noop_logger)
        assert len(parts) == expected_pieces

    @pytest.mark.parametrize(
        "polygon", [SQUARE_4, TRIANGLE, L_SHAPE, CHEVRON],
        ids=["square", "triangle", "L_shape", "chevron"],
    )
    def test_pieces_preserve_area(self, polygon):
        """Decomposition partitions the polygon: piece areas sum to its area.

        This is the covering guarantee the NFP relies on. If a future
        optimisation (convex merging, say) breaks it, placements would be
        silently lost, so the invariant is asserted rather than assumed.
        """
        parts = mu.decompose_if_needed(polygon, noop_logger)
        assert parts, "expected at least one piece"
        assert sum(p.area for p in parts) == pytest.approx(polygon.area, abs=AREA_TOL)

    def test_convex_input_is_not_split(self, polygon=SQUARE_4):
        """A convex polygon decomposes to itself, not to triangles.

        Guards against a regression where a convex fast path silently started
        triangulating and multiplying the pairwise-sum count.
        """
        parts = mu.decompose_if_needed(SQUARE_4, noop_logger)
        assert len(parts) == 1
        assert parts[0].equals(SQUARE_4)

    def test_empty_polygon_yields_no_parts(self):
        assert mu.decompose_if_needed(Polygon(), noop_logger) == []

    def test_repeated_calls_are_cached(self):
        """Second call must be served from cache and agree with the first."""
        first = mu.decompose_if_needed(L_SHAPE, noop_logger)
        assert Shape.decomposition_cache, "expected a cache entry after a miss"
        second = mu.decompose_if_needed(L_SHAPE, noop_logger)
        assert len(first) == len(second)
        assert [p.area for p in first] == pytest.approx([p.area for p in second])

    def test_invalid_input_is_repaired_not_rejected(self):
        """A self-touching polygon is buffered, not dropped."""
        bowtie = Polygon([(0, 0), (2, 2), (2, 0), (0, 2)])
        assert not bowtie.is_valid
        parts = mu.decompose_if_needed(bowtie, noop_logger)
        assert parts, "expected repaired geometry to decompose"
        assert all(p.area > 0 for p in parts)


class TestMinkowskiSumConvex:
    """Convex + convex sums, pinned to exact areas."""

    def test_square_plus_square(self):
        # 4x4 and 2x2 -> 6x6
        result = mu.minkowski_sum_convex(SQUARE_4, SQUARE_2)
        assert result.area == pytest.approx(36.0, abs=AREA_TOL)
        assert result.is_valid

    def test_square_plus_triangle(self):
        result = mu.minkowski_sum_convex(SQUARE_4, TRIANGLE)
        assert result.area == pytest.approx(56.0, abs=AREA_TOL)
        assert result.is_valid

    def test_result_is_convex_and_valid(self):
        result = mu.minkowski_sum_convex(SQUARE_4, TRIANGLE)
        assert result.is_valid
        # Convex hull of the result must equal the result itself.
        assert result.equals(result.convex_hull)

    def test_commutative(self):
        """A + B and B + A describe the same region."""
        a = mu.minkowski_sum_convex(SQUARE_4, TRIANGLE)
        b = mu.minkowski_sum_convex(TRIANGLE, SQUARE_4)
        assert a.area == pytest.approx(b.area, abs=AREA_TOL)


class TestMinkowskiSum:
    """Full (possibly concave) NFP construction."""

    @pytest.mark.parametrize(
        "a, b, expected_area",
        [
            (SQUARE_4, SQUARE_2, 36.0),
            (SQUARE_4, TRIANGLE, 56.0),
            (L_SHAPE, SQUARE_2, 27.0),
            (L_SHAPE, L_SHAPE, 37.0),
            (CHEVRON, SQUARE_2, 36.0),
        ],
        ids=["sq_x_sq", "sq_x_tri", "L_x_sq", "L_x_L", "chevron_x_sq"],
    )
    def test_nfp_area_is_stable(self, a, b, expected_area):
        """Pins NFP area for the canonical corpus.

        A drop in NFP area for a concave master means placements are being
        lost; a rise means the region has become over-permissive.
        """
        result = mu.minkowski_sum(a, 0, False, b, 0, False, noop_logger)
        assert result.geom_type == "Polygon"
        assert result.is_valid
        assert result.area == pytest.approx(expected_area, abs=AREA_TOL)

    def test_nfp_is_larger_than_either_input(self):
        """The forbidden region must contain both operands."""
        result = mu.minkowski_sum(L_SHAPE, 0, False, L_SHAPE, 0, False, noop_logger)
        assert result.area > L_SHAPE.area

    def test_rotation_changes_nfp(self):
        """A rotated part produces a different forbidden region."""
        unrotated = mu.minkowski_sum(SQUARE_4, 0, False, TRIANGLE, 0, False, noop_logger)
        rotated = mu.minkowski_sum(SQUARE_4, 0, False, TRIANGLE, 45, False, noop_logger)
        assert unrotated.area != pytest.approx(rotated.area, abs=AREA_TOL)


class TestMinkowskiDifferenceConvex:
    def test_square_minus_square(self):
        # 4x4 eroded by 2x2 -> 2x2
        result = mu.minkowski_difference_convex(SQUARE_4, SQUARE_2)
        assert result.area == pytest.approx(4.0, abs=AREA_TOL)
        assert result.is_valid

    def test_erosion_by_oversized_piece_is_empty(self):
        huge = Polygon([(-50, -50), (50, -50), (50, 50), (-50, 50)])
        result = mu.minkowski_difference_convex(SQUARE_4, huge)
        assert result is None or result.is_empty


class TestInnerFitPolygon:
    """IFP areas for convex holes.

    For a convex hole, vertex erosion is exact. main@eac1e30 adds hole-edge
    sweeps (`e (+) -P`) on top of exactly this erosion, and a measured
    comparison confirmed those sweeps remove nothing for a convex hole: every
    case below is byte-identical between the two implementations. These
    assertions are therefore a hard gate on block 2.1 -- a port that changes
    any of them is wrong.
    """

    def test_convex_hole_convex_part_square(self):
        # 8x8 hole, 2x2 part -> (8-2)^2 = 36
        result = mu.calculate_inner_fit_polygon(HOLE_SQUARE, 0, SQUARE_2, 0, noop_logger)
        assert result is not None
        assert result.area == pytest.approx(36.0, abs=AREA_TOL)

    def test_convex_hole_convex_part_triangle(self):
        result = mu.calculate_inner_fit_polygon(HOLE_SQUARE, 0, TRIANGLE, 0, noop_logger)
        assert result is not None
        assert result.area == pytest.approx(16.0, abs=AREA_TOL)

    def test_convex_hole_rotated_part(self):
        result = mu.calculate_inner_fit_polygon(HOLE_SQUARE, 0, SQUARE_2, 45, noop_logger)
        assert result is not None
        assert result.area == pytest.approx(26.7452, abs=1e-3)

    def test_ifp_is_contained_in_the_hole(self):
        """Every valid centroid position must lie inside the hole's exterior."""
        result = mu.calculate_inner_fit_polygon(HOLE_SQUARE, 0, SQUARE_2, 0, noop_logger)
        assert result is not None
        assert HOLE_SQUARE.contains(result)

    def test_oversized_part_fits_nowhere(self):
        huge = Polygon([(-50, -50), (50, -50), (50, 50), (-50, 50)])
        assert mu.calculate_inner_fit_polygon(HOLE_SQUARE, 0, huge, 0, noop_logger) is None

    def test_empty_hole_yields_none(self):
        assert mu.calculate_inner_fit_polygon(Polygon(), 0, SQUARE_2, 0, noop_logger) is None

    def test_empty_part_yields_none(self):
        assert mu.calculate_inner_fit_polygon(HOLE_SQUARE, 0, Polygon(), 0, noop_logger) is None


class TestInnerFitPolygonNonConvexHole:
    """IFP areas for a non-convex (L-shaped) hole.

    Block 2.1 changes these values, and the change is an improvement, so they
    are recorded as pre-port values rather than as invariants. A measured
    comparison against main@eac1e30 gave:

        L hole / triangle   8.0  ->  4.0   (halved)
        L hole / square@45 12.7452 -> 11.7452
        L hole / square    20.0  -> 20.0   (unchanged)

    The port must not make any of these *larger* -- a larger IFP is a more
    permissive region, i.e. a part allowed to straddle the hole wall. That is
    the regression to guard, and it is asserted below. See
    test_ifp_convexity.py for the explicit before/after gate.
    """

    def test_l_hole_square_part_pre_port_area(self):
        result = mu.calculate_inner_fit_polygon(HOLE_L, 0, SQUARE_2, 0, noop_logger)
        assert result is not None
        assert result.area == pytest.approx(20.0, abs=AREA_TOL)

    def test_l_hole_triangle_part_pre_port_area(self):
        result = mu.calculate_inner_fit_polygon(HOLE_L, 0, TRIANGLE, 0, noop_logger)
        assert result is not None
        # Pre-port this is 8.0; main's sweep-aware version gives 4.0.
        assert result.area == pytest.approx(8.0, abs=AREA_TOL)

    def test_l_hole_rotated_square_pre_port_area(self):
        result = mu.calculate_inner_fit_polygon(HOLE_L, 0, SQUARE_2, 45, noop_logger)
        assert result is not None
        # Pre-port 12.7452; main gives 11.7452.
        assert result.area == pytest.approx(12.7452, abs=1e-3)

    @pytest.mark.parametrize(
        "angle, current_area",
        [(0, 20.0), (0, 8.0), (45, 12.7452)],
        ids=["square", "triangle", "square_rotated"],
    )
    def test_ifp_never_exceeds_current_permissiveness(self, angle, current_area):
        """The IFP must never grow past the pre-port area.

        A larger IFP admits centroid positions where the part crosses the
        hole wall. This assertion holds both before and after block 2.1, which
        is why it is the durable form of the check.
        """
        part = SQUARE_2 if angle == 45 or current_area == 20.0 else TRIANGLE
        result = mu.calculate_inner_fit_polygon(HOLE_L, 0, part, angle, noop_logger)
        assert result is not None
        assert result.area <= current_area + AREA_TOL


class TestDeadRingPruning:
    """Dead-ring pruning is inert until a part set is published."""

    def test_no_profiles_means_no_pruning(self):
        """With no published part set, decomposition is unaffected."""
        assert mu.get_dead_ring_stats() == {}
        parts = mu.decompose_if_needed(CHEVRON, noop_logger)
        assert len(parts) == 5

    def test_profiles_enable_counters(self):
        """Publishing a part set arms the counters without changing geometry."""
        mu.set_dead_ring_profiles([(10.0, 10.0, 100.0)])
        try:
            parts = mu.decompose_if_needed(CHEVRON, noop_logger)
            # A part larger than every ring cannot occupy any of them, so the
            # exterior survives and the result stays a valid decomposition.
            assert sum(p.area for p in parts) == pytest.approx(CHEVRON.area, abs=AREA_TOL)
            assert mu.get_dead_ring_stats() != {}
        finally:
            mu.clear_dead_ring_profiles()

    def test_stats_snapshot_is_a_copy(self):
        """Callers get a snapshot, not a live reference to the counters."""
        mu.set_dead_ring_profiles([(10.0, 10.0, 100.0)])
        try:
            snapshot = mu.get_dead_ring_stats()
            snapshot["rings_seen"] = 9999
            assert mu.get_dead_ring_stats().get("rings_seen") != 9999
        finally:
            mu.clear_dead_ring_profiles()
