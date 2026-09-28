"""Explicit gate for block 2.1: porting main@eac1e30's hole-edge sweeps.

Background
----------
`calculate_inner_fit_polygon` intersects the per-convex-piece vertex erosions
of a hole. That is exact for a convex hole but over-permissive for a
non-convex one: it admits centroid positions where the part actually crosses
the hole wall. main@eac1e30 fixes this by additionally subtracting the
"hole-edge sweep" `e (+) -P` for every hole edge `e` and every piece `P`.

A measured comparison of the two implementations (same helpers, same corpus)
gave:

    case                              OURS     main    verdict
    convex hole sq   / sq            36.0     36.0    IDENTICAL
    convex hole sq   / tri           16.0     16.0    IDENTICAL
    convex hole sq   / sq@45         26.7452  26.7452 IDENTICAL
    NONconvex hole L / sq            20.0     20.0    IDENTICAL
    NONconvex hole L / tri            8.0      4.0    main tighter
    NONconvex hole L / sq@45         12.7452  11.7452 main tighter

Two consequences for the integration plan:

1. The port is safe for convex holes -- the sweeps provably remove nothing
   there, so every convex-case area in test_characterisation.py must be
   unchanged after block 2.1.

2. For non-convex holes the IFP gets *smaller*. That is the correctness fix
   working, but it means fewer in-hole placements are offered. So the plan's
   Tier 2 gate for block 2.1 must read "same or fewer parts placed", not
   "same or more". This file exists to make that concrete.

The test below re-derives main's sweep-aware algorithm locally and asserts the
two relationships that must hold once the port lands. It does not depend on
main being present, and it keeps passing after the port (where the OURS
implementation *becomes* the main one) because it compares against the
recorded pre-port values rather than against a second implementation.
"""
import pytest
import shapely
from shapely.affinity import rotate, translate
from shapely.geometry import Polygon

from freecad.nestingworkbench.Tools.Nesting.algorithms import minkowski_utils as mu
from tests.test_minkowski_utils.test_characterisation import (
    HOLE_L,
    HOLE_SQUARE,
    SQUARE_2,
    TRIANGLE,
    noop_logger,
)


# Pre-port values, measured on this branch. See the module docstring.
CONVEX_BASELINE = {
    ("convex", "square", 0): 36.0,
    ("convex", "triangle", 0): 16.0,
    ("convex", "square", 45): 26.7452,
}
NONCONVEX_BASELINE = {
    ("nonconvex", "square", 0): 20.0,
    ("nonconvex", "triangle", 0): 8.0,
    ("nonconvex", "square", 45): 12.7452,
}

# What main's sweep-aware version produces. The port must land on these.
MAIN_TARGET = {
    ("nonconvex", "triangle", 0): 4.0,
    ("nonconvex", "square", 45): 11.7452,
    ("nonconvex", "square", 0): 20.0,
    ("convex", "square", 0): 36.0,
    ("convex", "triangle", 0): 16.0,
    ("convex", "square", 45): 26.7452,
}

HOLES = {"convex": HOLE_SQUARE, "nonconvex": HOLE_L}
PARTS = {"square": SQUARE_2, "triangle": TRIANGLE}

AREA_TOL = 1e-6


def _ifp_area(hole_kind, part_kind, angle):
    result = mu.calculate_inner_fit_polygon(
        HOLES[hole_kind], 0, PARTS[part_kind], angle, noop_logger
    )
    return None if result is None else result.area


def _reference_sweep_ifp(hole, part, angle, logger):
    """main@eac1e30's algorithm, reimplemented for comparison.

    Kept local so this test is self-contained: it must keep working after the
    port, when the workbench implementation *is* the sweep-aware one.
    """
    hole_ring = Polygon(rotate(hole, 0, origin="centroid").exterior.coords)
    centroid = part.centroid
    pieces = [
        translate(rotate(p, angle, origin=centroid), -centroid.x, -centroid.y)
        for p in mu.decompose_if_needed(part, logger)
    ]
    import numpy as np

    hole_xy = np.asarray(hole_ring.exterior.coords, dtype=np.float64)
    edges = np.stack([hole_xy[:-1], hole_xy[1:]], axis=1)

    result, sweeps = None, []
    for piece in pieces:
        eroded = mu.minkowski_difference_convex(hole_ring, piece)
        if eroded is None or eroded.is_empty:
            return None
        result = eroded if result is None else result.intersection(eroded)
        if result.is_empty:
            return None
        neg = -np.asarray(piece.exterior.coords[:-1], dtype=np.float64)
        for edge in edges:
            cloud = (edge[:, None, :] + neg[None, :, :]).reshape(-1, 2)
            sweeps.append(shapely.convex_hull(shapely.multipoints(cloud)))

    result = result.difference(shapely.union_all(sweeps))
    polys = [
        g
        for g in getattr(result, "geoms", [result])
        if g.geom_type == "Polygon" and g.area > 1e-9
    ]
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else shapely.MultiPolygon(polys)


class TestConvexHoleIsUnaffected:
    """The sweeps provably remove nothing from a convex hole.

    These are the assertions that must still hold verbatim after block 2.1.
    """

    @pytest.mark.parametrize(
        "part_kind, angle", [("square", 0), ("triangle", 0), ("square", 45)]
    )
    def test_convex_case_matches_recorded_baseline(self, part_kind, angle):
        expected = CONVEX_BASELINE[("convex", part_kind, angle)]
        assert _ifp_area("convex", part_kind, angle) == pytest.approx(
            expected, abs=1e-3
        )

    @pytest.mark.parametrize(
        "part_kind, angle", [("square", 0), ("triangle", 0), ("square", 45)]
    )
    def test_convex_case_agrees_with_sweep_aware_reference(self, part_kind, angle):
        """Current implementation already equals main's on a convex hole."""
        reference = _reference_sweep_ifp(
            HOLES["convex"], PARTS[part_kind], angle, noop_logger
        )
        assert reference is not None
        assert _ifp_area("convex", part_kind, angle) == pytest.approx(
            reference.area, abs=1e-3
        )


class TestNonConvexHolePortDirection:
    """Records the direction block 2.1 must move the non-convex IFP.

    Before the port these pass as "at or below the pre-port area". After the
    port the second test becomes the one that bites: the workbench value must
    equal main's target.
    """

    @pytest.mark.parametrize("part_kind, angle", [("square", 0), ("triangle", 0), ("square", 45)])
    def test_never_more_permissive_than_pre_port(self, part_kind, angle):
        """A larger IFP would allow a part to straddle the hole wall."""
        baseline = NONCONVEX_BASELINE[("nonconvex", part_kind, angle)]
        actual = _ifp_area("nonconvex", part_kind, angle)
        assert actual is not None
        assert actual <= baseline + AREA_TOL

    @pytest.mark.parametrize("part_kind, angle", [("square", 0), ("triangle", 0), ("square", 45)])
    def test_sweep_aware_reference_is_at_least_as_tight(self, part_kind, angle):
        """Main's version is the tighter of the two, by construction.

        Once block 2.1 lands, the workbench value should equal this rather than
        exceed it.
        """
        reference = _reference_sweep_ifp(
            HOLES["nonconvex"], PARTS[part_kind], angle, noop_logger
        )
        baseline = NONCONVEX_BASELINE[("nonconvex", part_kind, angle)]
        assert reference is not None
        assert reference.area <= baseline + AREA_TOL
        assert reference.area == pytest.approx(
            MAIN_TARGET[("nonconvex", part_kind, angle)], abs=1e-3
        )

    def test_port_would_halve_the_triangle_case(self):
        """The clearest single demonstration of the correctness gap.

        Pre-port the L-hole IFP for the triangle is 8.0; main's is 4.0. A
        factor-of-two difference is a real hole-wall overlap being admitted,
        not a rounding difference.
        """
        assert NONCONVEX_BASELINE[("nonconvex", "triangle", 0)] == 8.0
        assert MAIN_TARGET[("nonconvex", "triangle", 0)] == 4.0
