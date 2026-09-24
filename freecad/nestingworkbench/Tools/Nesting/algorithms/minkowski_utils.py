# SPDX-License-Identifier: LGPL-2.1-or-later
"""Computes No-Fit Polygons (NFP) and Inner-Fit Polygons (IFP) using Minkowski sums/differences.

This module provides geometric utility functions for calculating Minkowski sums
and differences (including containment/erosion for IFP) to determine valid
placement zones for nesting operations.
"""
import math
import time
import threading
from concurrent.futures import Future
from shapely.geometry import Polygon, MultiPoint
from shapely.ops import unary_union, triangulate
from shapely.affinity import rotate, scale, translate

from ....datatypes.shape import Shape

_decomposition_lock = threading.Lock()


def decompose_if_needed(polygon, logger):
    """Decomposes a non-convex polygon into convex parts (triangles)."""
    if not polygon or polygon.is_empty:
        return []
    
    # Ensure valid geometry
    if not polygon.is_valid:
        polygon = polygon.buffer(0)
    
    # Use WKT for cache key
    cache_key = polygon.wkt
    with _decomposition_lock:
        if cache_key in Shape.decomposition_cache:
            return Shape.decomposition_cache[cache_key]
    with Shape.decomposition_inflight_lock:
        future = Shape.decomposition_inflight.get(cache_key)
        if future is None:
            future = Future()
            Shape.decomposition_inflight[cache_key] = future
            is_owner = True
        else:
            is_owner = False

    if not is_owner:
        return future.result()

    try:
        parts = _decompose_uncached(polygon, logger, cache_key)
        future.set_result(parts)
        return parts
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with Shape.decomposition_inflight_lock:
            Shape.decomposition_inflight.pop(cache_key, None)


def _decompose_uncached(polygon, logger, cache_key):
    def cache_result(
        parts,
        source_vertices=0,
        triangles=0,
        clipped_pieces=0,
        merged_pieces=0,
    ):
        stats = {
            "source_vertices": source_vertices,
            "triangles": triangles,
            "clipped_pieces": clipped_pieces,
            "merged_pieces": merged_pieces,
            "retained_pieces": len(parts),
        }
        with _decomposition_lock:
            Shape.decomposition_cache[cache_key] = parts
            Shape.decomposition_stats[cache_key] = stats
        return parts

    if polygon.geom_type == 'MultiPolygon':
        all_decomposed_parts = []
        combined_stats = {
            "source_vertices": 0,
            "triangles": 0,
            "clipped_pieces": 0,
            "merged_pieces": 0,
        }
        for p in polygon.geoms:
            child_parts = decompose_if_needed(p, logger)
            all_decomposed_parts.extend(child_parts)
            with _decomposition_lock:
                child_stats = Shape.decomposition_stats.get(p.wkt, {})
            for name in combined_stats:
                combined_stats[name] += child_stats.get(name, 0)
        return cache_result(all_decomposed_parts, **combined_stats)

    source_vertices = max(0, len(polygon.exterior.coords) - 1)

    # If already convex-ish (triangle or area match convex hull), return as is
    # Using a strict tolerance for robustness
    is_convex = False
    if polygon.geom_type == 'Polygon' and not polygon.interiors:
        if len(polygon.exterior.coords) <= 4: # Triangle (4 coords including closure)
            is_convex = True
        elif math.isclose(polygon.area, polygon.convex_hull.area, rel_tol=1e-7):
            is_convex = True
            
    if is_convex:
        return cache_result([polygon], source_vertices=source_vertices)
    
    try:
        # Delaunay triangulation is unconstrained (ignores polygon edges), so
        # triangles can cross the boundary of a concave polygon. Clip each
        # triangle to the polygon and keep the convex hull of every clipped
        # piece: hulls stay inside the (convex) triangle, so pieces remain
        # convex, and their union always COVERS the polygon. Coverage can only
        # err toward slight over-cover, which rejects a placement
        # conservatively — under-cover would miss collisions and let parts
        # overlap (the rep-point filter previously used here did exactly that).
        triangles = triangulate(polygon)
        decomposed = []
        clipped_pieces = 0
        for tri in triangles:
            if tri.area <= 1e-9:
                continue
            clip = tri.intersection(polygon)
            if clip.is_empty or clip.area <= 1e-9:
                continue
            if math.isclose(clip.area, tri.area, rel_tol=1e-9):
                decomposed.append(tri)
            else:
                pieces = clip.geoms if hasattr(clip, 'geoms') else [clip]
                for piece in pieces:
                    if piece.geom_type == 'Polygon' and piece.area > 1e-9:
                        clipped_pieces += 1
                        decomposed.append(piece.convex_hull)

        if not decomposed:
            # An empty shell list means "no collision constraint" downstream —
            # never return that for a real polygon.
            decomposed = [polygon.convex_hull]

        return cache_result(
            decomposed,
            source_vertices=source_vertices,
            triangles=len(triangles),
            clipped_pieces=clipped_pieces,
        )
    except Exception as e:
        logger(f"      - Triangulation failed: {e}. Falling back to convex hull.", level="warning")

    return cache_result([polygon.convex_hull], source_vertices=source_vertices)


def _transform_convex_parts(parts, master_polygon, angle, reflect, origin):
    """Return cached rotated/reflected convex pieces for one shape transform."""
    if hasattr(origin, "x") and hasattr(origin, "y"):
        origin_key = (round(origin.x, 12), round(origin.y, 12))
        scale_origin = (origin.x, origin.y)
    elif isinstance(origin, str):
        origin_key = origin
        scale_origin = origin
    else:
        origin_key = tuple(round(value, 12) for value in origin)
        scale_origin = origin
    cache_key = (
        master_polygon.wkt,
        round(angle % 360.0, 10),
        bool(reflect),
        origin_key,
    )
    with Shape.transformed_parts_cache_lock:
        cached_parts = Shape.transformed_parts_cache.get(cache_key)
    if cached_parts is not None:
        return cached_parts

    transformed_parts = []
    for part in parts:
        transformed = rotate(part, angle, origin=origin)
        if reflect:
            transformed = scale(
                transformed,
                xfact=-1.0,
                yfact=-1.0,
                origin=scale_origin,
            )
        transformed_parts.append(transformed)

    with Shape.transformed_parts_cache_lock:
        existing_parts = Shape.transformed_parts_cache.get(cache_key)
        if existing_parts is not None:
            return existing_parts
        Shape.transformed_parts_cache[cache_key] = transformed_parts
    return transformed_parts


def _minkowski_sum_convex_reference(poly1, poly2):
    """Reference implementation retained for fallback and geometry checks."""
    v1 = poly1.exterior.coords
    v2 = poly2.exterior.coords
    sum_vertices = [
        (p1[0] + p2[0], p1[1] + p2[1])
        for p1 in v1
        for p2 in v2
    ]
    return MultiPoint(sum_vertices).convex_hull


def _convex_ring_vertices(polygon):
    """Return unique vertices in counter-clockwise order without closure."""
    vertices = list(polygon.exterior.coords[:-1])
    if len(vertices) < 3:
        raise ValueError("Convex polygon must have at least three vertices")

    area2 = sum(
        x1 * y2 - x2 * y1
        for (x1, y1), (x2, y2) in zip(vertices, vertices[1:] + vertices[:1])
    )
    if area2 < 0:
        vertices.reverse()

    start = min(range(len(vertices)), key=lambda i: (vertices[i][1], vertices[i][0]))
    return vertices[start:] + vertices[:start]


def _prepare_convex_ring(polygon):
    """Prepare normalized vertices and edge vectors for repeated sums."""
    vertices = _convex_ring_vertices(polygon)
    edges = [
        (
            vertices[(i + 1) % len(vertices)][0] - x,
            vertices[(i + 1) % len(vertices)][1] - y,
        )
        for i, (x, y) in enumerate(vertices)
    ]
    return vertices, edges


def _minkowski_sum_convex_prepared(prepared_a, prepared_b, phase_stats=None):
    """Compute a convex Minkowski sum from prepared convex rings."""
    vertices_a, edges_a = prepared_a
    vertices_b, edges_b = prepared_b
    t_merge = time.perf_counter()
    result = [(vertices_a[0][0] + vertices_b[0][0],
               vertices_a[0][1] + vertices_b[0][1])]
    i = j = 0
    epsilon = 1e-12
    while i < len(edges_a) or j < len(edges_b):
        edge_a = edges_a[i] if i < len(edges_a) else None
        edge_b = edges_b[j] if j < len(edges_b) else None
        if edge_b is None:
            edge = edge_a
            i += 1
        elif edge_a is None:
            edge = edge_b
            j += 1
        else:
            cross = edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0]
            if cross > epsilon:
                edge = edge_a
                i += 1
            elif cross < -epsilon:
                edge = edge_b
                j += 1
            else:
                edge = (edge_a[0] + edge_b[0], edge_a[1] + edge_b[1])
                i += 1
                j += 1
        result.append((result[-1][0] + edge[0], result[-1][1] + edge[1]))

    result.pop()
    if phase_stats is not None:
        phase_stats["convex_merge_ms"] += (time.perf_counter() - t_merge) * 1000
    t_polygon = time.perf_counter()
    polygon = Polygon(result)
    if phase_stats is not None:
        phase_stats["convex_polygon_create_ms"] += (
            time.perf_counter() - t_polygon
        ) * 1000
        phase_stats["convex_result_points"] += len(result)
    return polygon


def _minkowski_sum_convex_linear(poly1, poly2, phase_stats=None):
    """Compute a convex Minkowski sum by preparing each ring once."""
    t_prepare = time.perf_counter()
    prepared_a = _prepare_convex_ring(poly1)
    prepared_b = _prepare_convex_ring(poly2)
    if phase_stats is not None:
        phase_stats["convex_prepare_ms"] += (time.perf_counter() - t_prepare) * 1000
    return _minkowski_sum_convex_prepared(prepared_a, prepared_b, phase_stats)


def minkowski_sum_convex(poly1, poly2, phase_stats=None):
    """Compute the Minkowski sum of two convex polygons in linear time."""
    try:
        result = _minkowski_sum_convex_linear(poly1, poly2, phase_stats)
        if phase_stats is not None:
            t_valid = time.perf_counter()
        is_valid = result.is_valid
        if phase_stats is not None:
            phase_stats["convex_validity_ms"] += (time.perf_counter() - t_valid) * 1000
            t_empty = time.perf_counter()
        is_empty = result.is_empty
        if phase_stats is not None:
            phase_stats["convex_empty_ms"] += (time.perf_counter() - t_empty) * 1000
            t_area = time.perf_counter()
        area = result.area
        if phase_stats is not None:
            phase_stats["convex_area_ms"] += (time.perf_counter() - t_area) * 1000
        if is_valid and not is_empty and area > 0:
            return result
    except (TypeError, ValueError, IndexError):
        pass
    if phase_stats is not None:
        phase_stats["convex_fallbacks"] += 1
        t_fallback = time.perf_counter()
        result = _minkowski_sum_convex_reference(poly1, poly2)
        phase_stats["convex_fallback_ms"] += (time.perf_counter() - t_fallback) * 1000
        return result
    return _minkowski_sum_convex_reference(poly1, poly2)

def minkowski_difference_convex(poly1, poly2):
    """
    Computes the erosion of poly1 by poly2, which is the Inner-Fit Polygon.
    This is NOT the Inner-Fit Polygon calculation (calculate_inner_fit_polygon), which would enlarge the polygon.
    """
    if not poly1 or poly1.is_empty or not poly2 or poly2.is_empty:
        return None

    # Erosion P ⊖ Q is the intersection of P translated by each of the negated vertices of Q.
    v2 = poly2.exterior.coords
    
    # Start with the first translated polygon
    first_translation = translate(poly1, xoff=-v2[0][0], yoff=-v2[0][1])
    
    # Intersect with the rest
    eroded_poly = first_translation
    for i in range(1, len(v2)):
        translated_poly = translate(poly1, xoff=-v2[i][0], yoff=-v2[i][1])
        eroded_poly = eroded_poly.intersection(translated_poly)
        # If the intersection is empty, we can stop early
        if eroded_poly.is_empty:
            return None
    
    return eroded_poly

def calculate_inner_fit_polygon(master_poly1, angle1, master_poly2, angle2, logger):
    """
    Computes the Inner-Fit Polygon for master_poly2 inside master_poly1.
    The IFP represents valid positions for poly2's CENTROID where poly2 fits inside poly1.
    This is Hole - Part. If Part is not convex, this is (Hole - P1) ∩ (Hole - P2) ...
    """
    if not master_poly1 or master_poly1.is_empty or not master_poly2 or master_poly2.is_empty:
        return None
    
    # Transform both polygons around their centroids
    poly1_transformed = rotate(master_poly1, angle1, origin='centroid')
    poly2_convex_parts = decompose_if_needed(master_poly2, logger)
    poly2_centroid = master_poly2.centroid
    poly2_convex_transformed = _transform_convex_parts(
        poly2_convex_parts,
        master_poly2,
        angle2,
        False,
        poly2_centroid,
    )
    
    # Translate both polygons so poly2's centroid is at the origin
    # This makes the Minkowski difference compute placement zones relative to poly2's centroid
    poly1_exterior_only = Polygon(poly1_transformed.exterior.coords)
    
    pairwise_diffs = []
    for p2 in poly2_convex_transformed:
        # Keep every decomposed piece in the candidate part's coordinate frame.
        # Re-centering each piece on its own centroid loses its offset from the
        # candidate centroid and produces an IFP for the pieces independently,
        # rather than for the complete candidate shape.
        p2_at_origin = translate(
            p2,
            xoff=-poly2_centroid.x,
            yoff=-poly2_centroid.y,
        )
        
        # Compute Inner-Fit Polygon
        diff = minkowski_difference_convex(poly1_exterior_only, p2_at_origin)
        pairwise_diffs.append(diff)
    
    if not pairwise_diffs:
        return None
    
    # Intersection of all pairwise differences
    final_difference = pairwise_diffs[0]
    for i in range(1, len(pairwise_diffs)):
        if final_difference is None or final_difference.is_empty:
            return None
        if pairwise_diffs[i] is None or pairwise_diffs[i].is_empty:
            return None
        final_difference = final_difference.intersection(pairwise_diffs[i])

    if final_difference is None or final_difference.is_empty:
        return None
    if not final_difference.is_valid:
        final_difference = final_difference.buffer(0)
    return final_difference if not final_difference.is_empty else None

def minkowski_sum(master_poly1, angle1, reflect1, master_poly2, angle2, reflect2, logger,
                  rot_origin1=None, rot_origin2=None, timings=None,
                  validate_convex_pairs=True):
    """
    Computes the Minkowski sum of two polygons.
    It uses the pre-cached decomposition of the master polygons and rotates
    the individual convex parts before summing them.
    """
    if master_poly1.is_empty or master_poly2.is_empty:
        return master_poly1.buffer(0) if master_poly2.is_empty else master_poly2.buffer(0)

    t_decompose = time.perf_counter()
    # Get the pre-decomposed convex parts from the cache.
    poly1_convex_parts = decompose_if_needed(master_poly1, logger)
    poly2_convex_parts = decompose_if_needed(master_poly2, logger)
    if timings is not None:
        timings["decompose_ms"] = (time.perf_counter() - t_decompose) * 1000
        timings["parts_a"] = len(poly1_convex_parts)
        timings["parts_b"] = len(poly2_convex_parts)
        with _decomposition_lock:
            stats_a = Shape.decomposition_stats.get(master_poly1.wkt, {})
            stats_b = Shape.decomposition_stats.get(master_poly2.wkt, {})
        for name, stats in (("a", stats_a), ("b", stats_b)):
            timings[f"source_vertices_{name}"] = stats.get("source_vertices", 0)
            timings[f"triangles_{name}"] = stats.get("triangles", 0)
            timings[f"clipped_pieces_{name}"] = stats.get("clipped_pieces", 0)
            timings[f"merged_pieces_{name}"] = stats.get("merged_pieces", 0)

    # CRITICAL: Use the MASTER polygon's centroid for all transformations
    # to keep the decomposed convex parts in their correct relative positions.
    c1 = master_poly1.centroid
    c2 = master_poly2.centroid

    t_transform = time.perf_counter()
    use_origin1 = c1 if (rot_origin1 is None or rot_origin1 == 'centroid') else rot_origin1
    use_origin2 = c2 if (rot_origin2 is None or rot_origin2 == 'centroid') else rot_origin2
    poly1_convex_transformed = _transform_convex_parts(
        poly1_convex_parts, master_poly1, angle1, reflect1, use_origin1
    )
    poly2_convex_transformed = _transform_convex_parts(
        poly2_convex_parts, master_poly2, angle2, reflect2, use_origin2
    )
    if timings is not None:
        timings["transform_ms"] = (time.perf_counter() - t_transform) * 1000

    t_sum = time.perf_counter()
    minkowski_parts = []
    pair_keys = set()
    phase_stats = {
        "convex_prepare_ms": 0.0,
        "convex_merge_ms": 0.0,
        "convex_polygon_create_ms": 0.0,
        "convex_fallback_ms": 0.0,
        "convex_fallbacks": 0,
        "convex_validity_ms": 0.0,
        "convex_empty_ms": 0.0,
        "convex_area_ms": 0.0,
        "convex_result_points": 0,
        "convex_pair_loop_ms": 0.0,
        "convex_pair_checks_ms": 0.0,
        "convex_pair_validity_ms": 0.0,
        "convex_pair_empty_ms": 0.0,
        "convex_pair_area_ms": 0.0,
        "convex_pair_append_ms": 0.0,
        "convex_pair_valid_count": 0,
        "convex_pair_non_empty_count": 0,
        "convex_pair_positive_area_count": 0,
    }
    t_pair_prepare = time.perf_counter()
    prepared_a = [_prepare_convex_ring(piece) for piece in poly1_convex_transformed]
    prepared_b = [_prepare_convex_ring(piece) for piece in poly2_convex_transformed]
    phase_stats["convex_prepare_ms"] += (time.perf_counter() - t_pair_prepare) * 1000
    polygon_key_a = master_poly1.wkb
    polygon_key_b = master_poly2.wkb
    t_pair_loop = time.perf_counter()
    for index_a, prepared_piece_a in enumerate(prepared_a):
        for index_b, prepared_piece_b in enumerate(prepared_b):
            pair_key = (
                polygon_key_a,
                round(angle1 % 360.0, 10),
                bool(reflect1),
                polygon_key_b,
                round(angle2 % 360.0, 10),
                bool(reflect2),
                index_a,
                index_b,
            )
            pair_keys.add(pair_key)
            try:
                result = _minkowski_sum_convex_prepared(
                    prepared_piece_a, prepared_piece_b, phase_stats
                )
                if not validate_convex_pairs:
                    t_append = time.perf_counter()
                    minkowski_parts.append(result)
                    phase_stats["convex_pair_append_ms"] += (
                        time.perf_counter() - t_append
                    ) * 1000
                    continue
                t_check = time.perf_counter()
                valid_result = result.is_valid
                phase_stats["convex_pair_validity_ms"] += (
                    time.perf_counter() - t_check
                ) * 1000
                if valid_result:
                    phase_stats["convex_pair_valid_count"] += 1
                t_check = time.perf_counter()
                non_empty_result = not result.is_empty
                phase_stats["convex_pair_empty_ms"] += (
                    time.perf_counter() - t_check
                ) * 1000
                if non_empty_result:
                    phase_stats["convex_pair_non_empty_count"] += 1
                t_check = time.perf_counter()
                positive_area = result.area > 0
                phase_stats["convex_pair_area_ms"] += (
                    time.perf_counter() - t_check
                ) * 1000
                if positive_area:
                    phase_stats["convex_pair_positive_area_count"] += 1
                if valid_result and non_empty_result and positive_area:
                    t_append = time.perf_counter()
                    minkowski_parts.append(result)
                    phase_stats["convex_pair_append_ms"] += (
                        time.perf_counter() - t_append
                    ) * 1000
                    continue
            except (TypeError, ValueError, IndexError):
                pass
            phase_stats["convex_fallbacks"] += 1
            t_fallback = time.perf_counter()
            t_append = time.perf_counter()
            minkowski_parts.append(
                _minkowski_sum_convex_reference(
                    poly1_convex_transformed[index_a],
                    poly2_convex_transformed[index_b],
                )
            )
            phase_stats["convex_pair_append_ms"] += (
                time.perf_counter() - t_append
            ) * 1000
            phase_stats["convex_fallback_ms"] += (time.perf_counter() - t_fallback) * 1000
    phase_stats["convex_pair_loop_ms"] = (time.perf_counter() - t_pair_loop) * 1000
    phase_stats["convex_pair_checks_ms"] = (
        phase_stats["convex_pair_validity_ms"]
        + phase_stats["convex_pair_empty_ms"]
        + phase_stats["convex_pair_area_ms"]
    )
    if timings is not None:
        timings["convex_sum_ms"] = (time.perf_counter() - t_sum) * 1000
        timings.update(phase_stats)
        timings["convex_pairs"] = len(minkowski_parts)
        timings["convex_pair_requests"] = len(minkowski_parts)
        timings["convex_pair_unique"] = len(pair_keys)
        timings["convex_pair_repeats"] = len(minkowski_parts) - len(pair_keys)
    with Shape.convex_pair_probe_lock:
        Shape.convex_pair_probe_requests += len(minkowski_parts)
        Shape.convex_pair_probe_keys.update(pair_keys)

    t_union = time.perf_counter()
    result = unary_union(minkowski_parts)
    if timings is not None:
        timings["union_ms"] = (time.perf_counter() - t_union) * 1000
    return result