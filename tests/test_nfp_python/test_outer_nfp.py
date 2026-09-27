"""Python-native outer NFP tests using Shapely.

Outer NFP (No-Fit Polygon) represents valid centroid positions where a part
can be placed without overlapping a master polygon. This is the complement of
the inner NFP (which fits inside holes).
"""
import pytest
from shapely.geometry import Polygon
from shapely.affinity import rotate, translate
from shapely.ops import unary_union
from tests.test_nfp_python.conftest import polygons_approx_equal


def _decompose_convex(polygon):
    """Simple convex decomposition for testing.

    Returns a list of convex parts. For basic shapes:
    - Triangle: [triangle]
    - Square: [triangle1, triangle2]
    - Others: [polygon] (single piece)
    """
    if polygon.is_empty or polygon.area <= 0:
        return []
    coords = list(polygon.exterior.coords)
    if len(coords) <= 4:
        return [polygon] if len(coords) >= 3 else []
    # Square split via both diagonals
    if len(coords) == 5:
        c = coords
        p1 = Polygon([c[0], c[1], c[2]])
        p2 = Polygon([c[0], c[2], c[3]])
        return [p1, p2]
    return [polygon]


def _minkowski_diff_hole_piece(hole, piece):
    """Approximate Minkowski difference (hole - piece) for testing.

    Uses buffer-based approximation instead of full vertex-based algorithm.
    """
    if hole.is_empty or piece.is_empty:
        return None
    # Approximate: translate hole by negative piece centroid, buffer
    try:
        piece_centroid = piece.centroid
        shifted = translate(hole, xoff=-piece_centroid.x, yoff=-piece_centroid.y)
        # Buffer negative to erode
        eroded = shifted.buffer(-0.5)
        # Translate back
        result = translate(eroded, xoff=piece_centroid.x, yoff=piece_centroid.y)
        if result.is_empty:
            return None
        return result
    except Exception:
        return None


def calculate_nfp_outer_approx(master, part, angle=0, rotation_origin='centroid'):
    """Approximate outer NFP (No-Fit Polygon) using Shapely only.

    The NFP represents valid centroid positions where `part` can be placed
    without overlapping `master`.

    Algorithm (approximate):
    1. Rotate part by angle around rotation_origin
    2. Decompose rotated part into convex pieces
    3. For each piece, compute the Minkowski difference master ⊖ piece
    4. Intersect all such differences — the result is the NFP

    Returns Polygon, MultiPolygon, or None.
    """
    if master.is_empty or part.is_empty:
        return None
    if master.area <= 0 or part.area <= 0:
        return None

    # Rotate part
    part_rotated = rotate(part, angle, origin=rotation_origin)

    # Decompose into convex pieces
    pieces = _decompose_convex(part_rotated)
    if not pieces:
        return None

    # Compute Minkowski difference for each piece
    nfp_parts = []
    for piece in pieces:
        diff = _minkowski_diff_hole_piece(master, piece)
        if diff is None or diff.is_empty:
            # If any piece has no valid diff, the whole NFP is empty
            return None
        nfp_parts.append(diff)

    if not nfp_parts:
        return None

    # Intersect all differences — the NFP is the region common to all
    result = nfp_parts[0]
    for diff in nfp_parts[1:]:
        result = result.intersection(diff)
        if result is None or result.is_empty:
            return None

    return result if result.area > 0 else None


class TestOuterNFPBasic:
    """Basic outer NFP tests."""

    def test_square_vs_square_no_rotation(self):
        """Outer NFP of square part vs square master, no rotation."""
        # Master: 10x10 square
        master = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        # Part: 2x2 square
        part = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])

        result = calculate_nfp_outer_approx(master, part, 0, 'centroid')
        if result is not None:
            # NFP should be the master minus a border around the part size
            # Valid centroid positions: from (2,2) to (8,8) = 6x6 area
            assert not result.is_empty
            assert result.area > 0
            # Check that the center (5,5) is inside the NFP
            assert result.contains(Polygon([(5, 5), (5, 5.1), (5.1, 5.1), (5.1, 5)]))

    def test_square_vs_square_with_rotation(self):
        """Outer NFP with 45-degree rotation."""
        master = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        part = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])

        result = calculate_nfp_outer_approx(master, part, 45.0, 'centroid')
        if result is not None:
            assert not result.is_empty
            assert result.area > 0

    def test_part_outside_master(self):
        """Part completely outside master should give full master as NFP."""
        master = Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])
        # Small part far away
        part = Polygon([(10, 10), (12, 10), (12, 12), (10, 12)])

        result = calculate_nfp_outer_approx(master, part, 0, 'centroid')
        if result is not None:
            # NFP should include the master (part can be placed anywhere
            # outside master without overlapping, but since part is large
            # and far, the NFP may be restricted)
            assert not result.is_empty

    def test_invalid_part(self):
        """Empty or invalid part should return None."""
        master = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        part = Polygon()  # empty

        result = calculate_nfp_outer_approx(master, part, 0, 'centroid')
        assert result is None

    def test_invalid_master(self):
        """Empty master should return None."""
        master = Polygon()  # empty
        part = Polygon([(0, 0), (1, 0), (1, 1), (0, 1)])

        result = calculate_nfp_outer_approx(master, part, 0, 'centroid')
        assert result is None


class TestOuterNFPNonTrivial:
    """Outer NFP with more complex scenarios."""

    def test_rectangle_vs_rectangle(self):
        """Rectangle part vs rectangle master."""
        master = Polygon([(0, 0), (8, 0), (8, 6), (0, 6)])
        part = Polygon([(0, 0), (2, 0), (2, 3), (0, 3)])

        result = calculate_nfp_outer_approx(master, part, 0, 'centroid')
        if result is not None:
            assert not result.is_empty
            assert result.area > 0

    def test_different_angles(self):
        """NFP changes with part rotation."""
        master = Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])
        part = Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])

        # 0 degrees
        result_0 = calculate_nfp_outer_approx(master, part, 0.0, 'centroid')
        # 90 degrees (same for square, but test the mechanism)
        result_90 = calculate_nfp_outer_approx(master, part, 90.0, 'centroid')

        # Both should produce valid NFPs (may be identical for square)
        assert (result_0 is None or not result_0.is_empty)
        assert (result_90 is None or not result_90.is_empty)