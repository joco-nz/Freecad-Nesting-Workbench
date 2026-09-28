"""Documents the centroid-vs-bounding-box property the workbench relies on.

Scope note: these assert properties of Shapely itself, not of workbench code.
They are here because `shape_processor.get_2d_profile_from_obj` derives a
part's `source_centroid` from the Shapely polygon centroid, and every
asymmetric part is a case where using the bounding-box centre instead would
place the part wrongly. Keeping the property pinned makes the reason for that
choice explicit.

Testing the workbench's own profile extraction needs a real FreeCAD (it
tessellates via `Part`), so it belongs to the freecadcmd end-to-end harness,
not to this tier. For the same reason nothing here exercises the
NFP/minkowski code -- that is covered against the real functions in
tests/test_minkowski_utils/.
"""
import pytest
from shapely.geometry import Polygon
from shapely.affinity import translate


class TestPolygonCentroidAlignment:
    """Test that polygon centroid calculations are correct for asymmetric shapes."""

    def test_triangle_centroid_vs_bbox_center(self):
        """Verify that a triangle's centroid differs from its bounding box center.

        This is the fundamental issue that the fix addresses. For asymmetric shapes,
        the centroid (center of mass) is different from the bounding box center.
        """
        # Right triangle with vertices at (0,0), (1,0), (0,1)
        triangle = Polygon([(0, 0), (1, 0), (0, 1)])

        # Centroid (center of mass) of a triangle is at the average of vertices
        expected_centroid_x = (0 + 1 + 0) / 3
        expected_centroid_y = (0 + 0 + 1) / 3

        assert abs(triangle.centroid.x - expected_centroid_x) < 1e-9
        assert abs(triangle.centroid.y - expected_centroid_y) < 1e-9

        # Bounding box center
        minx, miny, maxx, maxy = triangle.bounds
        bbox_center_x = (minx + maxx) / 2
        bbox_center_y = (miny + maxy) / 2

        # These should be different for a right triangle
        assert abs(triangle.centroid.x - bbox_center_x) > 0.01
        assert abs(triangle.centroid.y - bbox_center_y) > 0.01

    def test_asymmetric_l_shape_centroid(self):
        """L-shape centroid should differ from bounding box center."""
        l_shape = Polygon([(0, 0), (2, 0), (2, 1), (1, 1), (1, 2), (0, 2)])

        minx, miny, maxx, maxy = l_shape.bounds
        bbox_center_x = (minx + maxx) / 2
        bbox_center_y = (miny + maxy) / 2

        centroid_x = l_shape.centroid.x
        centroid_y = l_shape.centroid.y

        # L-shape is asymmetric, so centroid != bbox center
        assert abs(centroid_x - bbox_center_x) > 0.01
        assert abs(centroid_y - bbox_center_y) > 0.01

    def test_symmetric_rectangle_centroid_matches_bbox(self):
        """For symmetric shapes, centroid should match bounding box center."""
        rect = Polygon([(0, 0), (2, 0), (2, 1), (0, 1)])

        minx, miny, maxx, maxy = rect.bounds
        bbox_center_x = (minx + maxx) / 2
        bbox_center_y = (miny + maxy) / 2

        centroid_x = rect.centroid.x
        centroid_y = rect.centroid.y

        # For a rectangle, centroid matches bbox center
        assert abs(centroid_x - bbox_center_x) < 1e-9
        assert abs(centroid_y - bbox_center_y) < 1e-9

    def test_octagon_centroid(self):
        """Octagon centroid should match bbox center due to symmetry."""
        # Regular-ish octagon
        octagon = Polygon([
            (0, 0), (2, 0), (3, 1), (3, 3), (2, 4), (0, 4), (-1, 3), (-1, 1)
        ])

        minx, miny, maxx, maxy = octagon.bounds
        bbox_center_x = (minx + maxx) / 2
        bbox_center_y = (miny + maxy) / 2

        centroid_x = octagon.centroid.x
        centroid_y = octagon.centroid.y

        # Due to symmetry, centroid should match bbox center
        assert abs(centroid_x - bbox_center_x) < 1e-9
        assert abs(centroid_y - bbox_center_y) < 1e-9