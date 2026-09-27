"""Shared fixtures for Python NFP tests."""
import math
import pytest
from shapely.geometry import Polygon


@pytest.fixture
def square():
    """Unit square centered at origin."""
    return Polygon([(-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)])


@pytest.fixture
def triangle():
    """Equilateral triangle centered at origin."""
    h = 1.0 / (2 * 0.75**0.5)
    return Polygon([(0, h), (-0.5, -h/2), (0.5, -h/2)])


@pytest.fixture
def l_shape():
    """L-shaped polygon (concave)."""
    return Polygon([(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)])


@pytest.fixture
def square_with_hole():
    """Square with a circular hole (approximated as polygon).

    Returns outer_coords, hole_coords.
    """
    outer_coords = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
    # Approximate circle as 16-gon
    hole = []
    for i in range(16):
        angle = 2 * math.pi * i / 16
        hole.append((0.3 * math.cos(angle), 0.3 * math.sin(angle)))
    return outer_coords, hole


@pytest.fixture
def complex_shape():
    """A more complex concave shape for stress testing."""
    return Polygon([
        (0, 0), (3, 0), (3, 1), (2, 1), (2, 2), (3, 2), (3, 3),
        (0, 3), (0, 0)
    ])


def polygons_approx_equal(a, b, tolerance=1e-7):
    """Check if two Shapely polygons are approximately equal.

    Compares area, exterior coordinates (within tolerance), and interior rings.
    """
    if a.is_empty != b.is_empty:
        return False

    if a.is_empty and b.is_empty:
        return True

    # Compare area (most sensitive to gross errors)
    if abs(a.area - b.area) > tolerance:
        return False

    # Compare exterior coordinates
    ext_a = list(a.exterior.coords)
    ext_b = list(b.exterior.coords)

    # Coordinates may start at different points, so find the best alignment
    if len(ext_a) != len(ext_b):
        return False

    # Try all cyclic alignments
    for offset in range(len(ext_a)):
        aligned = ext_a[offset:] + ext_a[:offset]
        matches = True
        for a_pt, b_pt in zip(aligned, ext_b):
            if abs(a_pt[0] - b_pt[0]) > tolerance or abs(a_pt[1] - b_pt[1]) > tolerance:
                matches = False
                break
        if matches:
            break
    else:
        return False

    # Compare interior rings (if any)
    if len(a.interiors) != len(b.interiors):
        return False

    for ring_a, ring_b in zip(a.interiors, b.interiors):
        ra = list(ring_a.coords)
        rb = list(ring_b.coords)
        if len(ra) != len(rb):
            return False
        # Same cyclic alignment check as exterior
        found = False
        for offset in range(len(ra)):
            aligned = ra[offset:] + ra[:offset]
            matches = True
            for a_pt, b_pt in zip(aligned, rb):
                if abs(a_pt[0] - b_pt[0]) > tolerance or abs(a_pt[1] - b_pt[1]) > tolerance:
                    matches = False
                    break
            if matches:
                found = True
                break
        if not found:
            return False

    return True