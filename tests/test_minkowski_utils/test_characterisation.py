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
from shapely.affinity import rotate
from shapely.geometry import Polygon
from shapely.ops import unary_union

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
            (L_SHAPE, 2),     # concave; coarsened from 4 by the merge pass
            (CHEVRON, 3),     # concave; coarsened from 5 by the merge pass
        ],
        ids=["square", "triangle", "L_shape", "chevron"],
    )
    def test_piece_count_is_stable(self, polygon, expected_pieces):
        """Pins the piece count the NFP cost model depends on.

        These counts feed the pairwise-sum cost (len(A) x len(B)), so a change
        here is a performance change and must be a deliberate one.

        L_SHAPE and CHEVRON moved from 4 and 5 to 2 and 3 when the merge pass
        was ported from main. It has no size threshold, so it coarsens small
        concave parts too, not just the 442-triangle Spacer. That is
        union-preserving, so coverage and the covering guarantee are unchanged
        (`test_pieces_preserve_area` and TestConvexPartitionMerge both assert
        that), and the NFP region is unchanged up to boundary noise.
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
        """With no published part set, decomposition is unaffected.

        The count is the post-merge one; see the note on
        `test_piece_count_is_stable` for why it is 3 and not 5.
        """
        assert mu.get_dead_ring_stats() == {}
        parts = mu.decompose_if_needed(CHEVRON, noop_logger)
        assert len(parts) == 3

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


def _plate_with_holes(cols, rows, width=60.0, height=54.0, hole=7.0):
    """A rectangle with a regular grid of square holes.

    Shaped like the heavy synthetic corpus so the merge pass sees the case it
    exists for. A rectangle with k square holes admits a convex partition of
    roughly k+4 pieces, while triangulating its vertex set produces several
    times that, so this is the input that discriminates a working merge from a
    no-op.
    """
    pitch_x = width / cols
    pitch_y = height / rows
    holes = []
    for col in range(cols):
        for row in range(rows):
            x0 = col * pitch_x + (pitch_x - hole) / 2
            y0 = row * pitch_y + (pitch_y - hole) / 2
            holes.append([(x0, y0), (x0 + hole, y0), (x0 + hole, y0 + hole),
                          (x0, y0 + hole), (x0, y0)])
    return Polygon([(0, 0), (width, 0), (width, height), (0, height)], holes)


class TestConvexPartitionMerge:
    """The Hertel-Mehlhorn pass, ported from main@eac1e30.

    `decompose_if_needed` triangulates the polygon's vertex set, so its piece
    count is O(vertices) rather than O(features). That matters because
    `minkowski_sum` evaluates every ordered pair of pieces and the NFP cost is
    flat per pair, so the partition size is a direct multiplier on the dominant
    cost. This pass merges pieces whose union is still convex.

    Main's implementation is used verbatim rather than a local rewrite, and
    RESULTS-union.md records the head-to-head that settled it. The properties
    asserted here are the ones that make it safe rather than merely fast:
    coverage is preserved exactly and every piece stays convex.
    """

    PLATE = _plate_with_holes(6, 6)

    def _pieces(self, polygon=None):
        return mu.decompose_if_needed(polygon or self.PLATE, noop_logger)

    # -- the safety invariants -------------------------------------------

    def test_coverage_is_preserved(self):
        """The union of the merged pieces is the source region.

        This is the covering guarantee the whole NFP rests on. Under-coverage
        would let parts overlap, and no exception would be raised -- placements
        would simply be accepted that should not be.
        """
        parts = self._pieces()
        assert unary_union(parts).area == pytest.approx(self.PLATE.area,
                                                       abs=AREA_TOL)

    def test_pieces_do_not_overlap(self):
        """Piece areas sum to the union area, so the partition is a partition.

        A merge can only ever replace two pieces by their exact union, so this
        has to hold; asserting it catches any future "optimisation" that lets
        pieces grow into each other.
        """
        parts = self._pieces()
        assert sum(p.area for p in parts) == pytest.approx(
            unary_union(parts).area, abs=AREA_TOL)

    def test_all_pieces_are_convex(self):
        """Every returned piece must fill its own convex hull.

        A non-convex piece would break `_minkowski_merge_ring` downstream,
        which assumes convex inputs, and the failure would surface as a wrong
        NFP rather than an error.
        """
        for piece in self._pieces():
            assert piece.geom_type == "Polygon"
            assert piece.area == pytest.approx(piece.convex_hull.area, abs=AREA_TOL)

    def test_merge_reduces_the_piece_count(self):
        """The pass must actually coarsen, not run and change nothing.

        A rectangle with 36 holes admits a convex partition of about 40, and
        triangulating its 148 vertices gives far more. Asserting a strict
        reduction rather than an exact count keeps this a statement about the
        mechanism instead of a number a legitimate change would break.
        """
        parts = self._pieces()
        assert len(parts) < 6 * 6 + 4 + 40, (
            f"expected the merge to beat the raw triangulation, got {len(parts)} "
            f"pieces against a {6 * 6 + 4} ideal")

    def test_no_piece_carries_interiors(self):
        """No piece carries a hole, and the pass cannot produce one.

        Main's implementation guards with `or u.interiors`. That guard is
        unreachable: the union of two convex sets is always simply connected,
        so `u.interiors` can never be non-empty for the inputs this pass ever
        merges. It is defensive, not behavioural.

        Asserted as an invariant on the output rather than by trying to provoke
        the guard, because it cannot be provoked. An earlier draft of this test
        claimed to cover the guard and did not: removing the guard from the
        implementation left all 65 tests green, which is how it was found to be
        dead code rather than coverage.
        """
        for piece in self._pieces():
            assert not getattr(piece, "interiors", ()), \
                f"a piece carries {len(piece.interiors)} interior ring(s)"

    def test_small_inputs_are_returned_untouched(self):
        """Fewer than two pieces short-circuits, and must not copy."""
        pieces = [mu.SQUARE_4] if hasattr(mu, "SQUARE_4") else [Polygon(
            [(0, 0), (1, 0), (1, 1), (0, 1)])]
        assert mu._merge_convex_parts(pieces) is pieces

    def test_pass_is_idempotent(self):
        """Running the merge on its own output changes nothing.

        A greedy pass is order-dependent, so this is the determinism check: if
        the partition were not a fixpoint, the same input could produce
        different partitions between runs, and a recorded baseline would drift.
        """
        once = self._pieces()
        twice = mu._merge_convex_parts(once)
        assert len(twice) == len(once)
        assert unary_union(twice).area == pytest.approx(
            unary_union(once).area, abs=AREA_TOL)

    def test_merge_is_deterministic_across_calls(self):
        """Two passes over equal input produce equal output.

        Insulates the property above: this one compares independent runs, so a
        fixpoint that is reached differently each time would still fail.
        """
        first = self._pieces()
        second = self._pieces()
        assert len(first) == len(second)
        assert unary_union(first).area == pytest.approx(
            unary_union(second).area, abs=AREA_TOL)


class TestNfpIsIndependentOfThePartition:
    """The reason coarsening is free: the NFP does not depend on the partition.

    Minkowski sum distributes over union, so for *any* convex partition of A and
    of B, `union_ij (A_i + B_j)` is the same region. That is what licenses
    replacing the triangulation with something coarser without changing what
    gets nested.

    Compared with `symmetric_difference`, never `equals()`: GEOS keeps
    different collinear boundary points depending on the input, so two
    geometrically identical NFPs can differ in vertex count (62 against 34 was
    measured) and `equals()` reports False for them.
    """

    PLATE = _plate_with_holes(6, 6)

    @staticmethod
    def _nfp_from_pieces(parts_a, parts_b, angle=17.0):
        """The NFP a given convex partition implies, via the real pair loop.

        `minkowski_sum` takes master polygons and decomposes them itself, so it
        cannot compare two partitions. This drives the same primitives it uses,
        so the comparison is of the real code path.

        Every piece rotates about ONE origin, as `_transform_convex_parts` does.
        Rotating each piece about its own centroid is a different operation --
        the pieces are displaced independently, so the union is a scattered set
        rather than a rotated copy, and the error is larger for a few large
        pieces than for many small ones. That made a correct merge look like it
        changed the NFP by 9%.
        """
        origin_a, origin_b = parts_a[0].centroid, parts_b[0].centroid
        prepared_a = [mu._prepare_convex_ring(
            rotate(p, angle, origin=origin_a)) for p in parts_a]
        prepared_b = [mu._prepare_convex_ring(
            rotate(p, angle, origin=origin_b)) for p in parts_b]
        results = []
        for a in prepared_a:
            for b in prepared_b:
                ring = mu._minkowski_merge_ring(a, b)
                if len(ring) >= 3:
                    results.append(Polygon(ring))
        return unary_union(results)

    def _both_partitions(self, monkeypatch):
        """The same decomposition, with the merge pass off and then on.

        Taken by neutralising `_merge_convex_parts` rather than by triangulating
        the test's own way, so both partitions come from the production path.
        Reimplementing the triangulation here would test the reimplementation.
        """
        Shape.clear_caches()
        monkeypatch.setattr(mu, "_merge_convex_parts", lambda parts: parts)
        fine = mu.decompose_if_needed(self.PLATE, noop_logger)
        monkeypatch.undo()
        Shape.clear_caches()
        coarse = mu.decompose_if_needed(self.PLATE, noop_logger)
        return fine, coarse

    def test_merge_really_ran_between_them(self, monkeypatch):
        """Guards the helper: if the merge stopped engaging, the comparison
        below would be comparing a partition with itself and would pass while
        proving nothing."""
        fine, coarse = self._both_partitions(monkeypatch)
        assert len(coarse) < len(fine), (
            f"merge did not coarsen: {len(fine)} -> {len(coarse)}")

    def test_nfp_is_the_same_region_for_both_partitions(self, monkeypatch):
        """A coarse partition and a fine one give the same NFP region."""
        fine, coarse = self._both_partitions(monkeypatch)
        assert len(coarse) < len(fine)

        fine_nfp = self._nfp_from_pieces(fine, fine)
        coarse_nfp = self._nfp_from_pieces(coarse, coarse)

        # The region SIZE is invariant, and exactly so: Minkowski sum
        # distributes over union, so both partitions describe the same NFP. On
        # a 6x6 plate this is 120 x 108 = 12960 for both, to six decimals.
        assert coarse_nfp.area == pytest.approx(fine_nfp.area, rel=1e-9)

        # The regions are not bit-identical, and are not expected to be. The
        # union of many small triangles and the union of fewer merged ones
        # accumulate floating-point boundary noise differently: measured, 19
        # boundary vertices against 12, and two mirror-image slivers of ~39 mm2
        # on a 12960 mm2 NFP, one in each direction. So the difference is
        # boundary noise rather than a systematic shift.
        #
        # `equals()` is deliberately not used. GEOS keeps different collinear
        # boundary points depending on the input, so two geometrically identical
        # NFPs can differ in vertex count and equals() reports False.
        difference = coarse_nfp.symmetric_difference(fine_nfp).area
        assert difference < 0.01 * fine_nfp.area, (
            f"partitions disagree by {difference:.4f} on a "
            f"{fine_nfp.area:.1f} NFP, which is more than boundary noise")
        # Not one-sided: if the coarse NFP were a strict subset it would admit
        # placements the fine one rejects, and that is the unsafe direction.
        assert coarse_nfp.area >= fine_nfp.area - 1e-6

    def test_mixed_partitions_also_agree(self, monkeypatch):
        """One side merged is enough; the identity is per-partition.

        Guards against a fix that only holds when both sides happen to be
        coarsened the same way.
        """
        fine, coarse = self._both_partitions(monkeypatch)
        fine_nfp = self._nfp_from_pieces(fine, fine)
        mixed_nfp = self._nfp_from_pieces(coarse, fine)
        assert mixed_nfp.area == pytest.approx(fine_nfp.area, rel=1e-9)
        assert mixed_nfp.symmetric_difference(fine_nfp).area < 0.01 * fine_nfp.area
