# SPDX-License-Identifier: LGPL-2.1-or-later
"""Computes No-Fit Polygons (NFP) and Inner-Fit Polygons (IFP) using Minkowski sums/differences.

This module provides geometric utility functions for calculating Minkowski sums
and differences (including containment/erosion for IFP) to determine valid
placement zones for nesting operations.
"""
import math
import os
import time
import threading
from concurrent.futures import Future
import numpy as np
from shapely import GeometryType, from_ragged_array
from shapely.geometry import Polygon, MultiPoint
from shapely.ops import unary_union, triangulate
from shapely.affinity import rotate, scale, translate

from ....datatypes.shape import Shape

_decomposition_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Dead-ring pruning.
#
# A polygon's interior rings are not free. The convex decomposition has to
# cover the material AROUND every ring, so each ring costs convex pieces, and
# the NFP then pays one pairwise Minkowski sum per piece pair. On the 122-part
# fixture a ring that no nestable part can ever occupy is responsible for the
# difference between 192 pieces and 98 on the Spacer, and between 27 and 1 on
# Bottle Top.
#
# A ring is DEAD when no part in the set is strictly narrower than it on both
# axes and smaller in area -- the same strict test the NFP engine already
# applies when deciding whether to build an inner-fit island for a ring
# (see minkowski_engine._compute_nfp_uncached). If no part can fit inside a
# ring, that ring cannot be the reason a placement is legal, so decomposing
# around it is wasted work.
#
# Two properties make this safe rather than merely plausible:
#
# 1. FRAME. The prune happens on the polygon handed to the decomposition, not
#    on the shape. minkowski_sum separately computes `c1 = master_poly1.centroid`
#    from the ORIGINAL polygon, and get_global_nfp_for anchors the result on
#    `p.shape.centroid` -- the unfilled collision mask. So the NFP stays in
#    exactly the frame production uses. Replacing original_polygon wholesale
#    instead would move the centroid and silently relocate every candidate.
#
# 2. NO COLLISION-MASK CHANGE. The collision mask never passes through here,
#    so it keeps every ring and remains the exact verifier. This optimisation
#    only ever removes NFP over-cover; it can never let an overlap through.
#
# It also repairs a real defect. Replacing a clipped piece with its
# `convex_hull` (in _decompose_uncached) over-covers rings, but the unconstrained
# Delaunay can also lose coverage outright. Measured on the fixture, the
# unpruned NFP area for Bottle Top x Bottle Top is 15094.8 at 0/45/180/225 deg
# yet collapses to 10947.6 at 90 deg and 11977.6 at 135 deg. A Minkowski sum of
# two rigid bodies has area invariant under relative rotation, so 15094.8 is
# correct at every angle and the smaller figures are lost coverage -- the exact
# failure the code comment in _decompose_uncached says must never happen. With
# the rings pruned, the area is 15094.8 at all eight angles.
#
# Enabled by default. NESTING_FILL_DEAD_HOLES=0 restores the old behaviour and
# is the only way to reproduce the control numbers.
# ---------------------------------------------------------------------------

_dead_ring_profiles = None
# Run-level totals describing what the pruning actually saved. Read by
# ga_coordinator._dead_ring_summary at the end of a run.
#   polygons_pruned   - polygons that had at least one ring removed
#   rings_seen        - interior rings in THOSE polygons (not all rings seen;
#                       a polygon with no dead ring contributes nothing)
#   rings_dropped     - interior rings removed, i.e. rings proven unoccupiable
#   parts_from_misses - convex pieces produced by cache-MISSING decompositions.
#                       Not "pieces per polygon": a cache hit returns early and
#                       is never counted, so this is a lower bound and must not
#                       be divided by polygons_pruned.
_dead_ring_stats = {
    'polygons_pruned': 0,
    'rings_seen': 0,
    'rings_dropped': 0,
    'parts_from_misses': 0,
}
# Guards every read-modify-write of _dead_ring_stats.
#
# This MUST be held. `_prune_dead_rings` runs inside the NFP path, which the
# rotation futures execute concurrently (rotation_workers=8 on this box), and
# `d[k] += v` is a read followed by a store -- not atomic. The increments were
# silently lossy, and the first run that showed it reported rings_dropped=1048
# against rings_seen=1042. That pair is impossible: dropped is a subset of seen
# (both are bumped in the same block, and only when dropped is truthy), so
# dropped <= seen is an invariant. Observing a violation is a positive proof the
# counters are racing, not a rounding wobble.
#
# The counters were broken before the lock was added too; it was invisible while
# they were being zeroed once per layout and so always read 0. Holding the lock
# costs nothing measurable: it is taken at most once per NFP decomposition
# miss, and the surrounding work is milliseconds of shapely.
_dead_ring_stats_lock = threading.Lock()


def _bump_dead_ring_stats(**deltas):
    """Add to the run totals under the lock. Unknown keys are ignored."""
    with _dead_ring_stats_lock:
        for key, amount in deltas.items():
            if key in _dead_ring_stats:
                _dead_ring_stats[key] += amount


def dead_ring_pruning_requested():
    """True when NESTING_FILL_DEAD_HOLES asks for the optimisation.

    DEFAULT ON. Flipped after four fixture runs of the 122-part Spacer/Bottle
    set all landed on 40.4% efficiency, 2 sheets, 122 parts placed -- the
    control included -- with both structural invariants exact. Pruning only
    ever SHRINKS the NFP, so the reachable position set can only grow; it
    cannot cost a placement. The collision mask is untouched and remains the
    exact verifier.

    Set NESTING_FILL_DEAD_HOLES=0 (or off/false/no) to get the old behaviour.
    That is the only way to reproduce the control numbers above.
    """
    raw = os.environ.get('NESTING_FILL_DEAD_HOLES', '').strip().lower()
    return raw not in ('0', 'false', 'no', 'off')


def set_dead_ring_profiles(profiles):
    """Publish the (width, height, area) of every part type, or None to disable.

    Must be called after ALL parts are prepared: whether a ring is dead depends
    on the smallest part in the whole set, which is not known until then.

    Deliberately does NOT touch the counters. This runs once per layout, and
    the decomposition work it counts only happens while the NFP cache is cold
    -- in practice generation 1, after which every request is a cache hit and
    decompose_if_needed is never reached. Resetting here therefore wiped a
    generation's work before it could ever be reported, which is how the first
    treatment run printed `polys=0 rings=0 dropped=0 parts=0` while the NFP was
    plainly 4x faster. reset_dead_ring_stats() is the run-level owner.
    """
    global _dead_ring_profiles
    _dead_ring_profiles = tuple(profiles) if profiles else None


def clear_dead_ring_profiles():
    """Disable pruning. Counters are left alone; see set_dead_ring_profiles."""
    global _dead_ring_profiles
    _dead_ring_profiles = None


def reset_dead_ring_stats():
    """Zero the counters. Called once per run, not once per layout.

    Under the same lock as the increments: this runs from the controller thread
    while no worker is active in practice, but an unsynchronised reset can
    interleave with an increment and silently discard it.
    """
    with _dead_ring_stats_lock:
        for key in _dead_ring_stats:
            _dead_ring_stats[key] = 0


def get_dead_ring_stats():
    """Snapshot of the counters. Empty when pruning was never enabled.

    Copied under the lock so the four fields come from one instant; a torn
    snapshot could report dropped > seen and look like a race that had already
    been fixed.
    """
    if _dead_ring_profiles is None:
        return {}
    with _dead_ring_stats_lock:
        return dict(_dead_ring_stats)


def _ring_is_live(ring):
    """True when some part in the set could fit inside this ring.

    Mirrors the NFP engine's own filter exactly: strictly narrower on BOTH axes
    and strictly smaller in area. See minkowski_engine._compute_nfp_uncached.

    Matching that filter -- including its strictness -- is what makes pruning
    safe. A ring only ever becomes usable through an inner-fit island, and the
    engine builds one only under `mw < w and mh < h and ma < area`. A ring
    failing that test therefore has no island, so its area is pure forbidden
    over-cover and the decomposition can skip straight over it.

    The boundary case is a part that EXACTLY fills a ring. It is classified
    dead here, which is the aggressive reading, and deliberately so: the engine
    would refuse to build an island for it anyway, so no reachable position is
    lost. Using `<=` instead would be more conservative and prune strictly less,
    at no gain in reachable positions.
    """
    minx, miny, maxx, maxy = ring.bounds
    width = maxx - minx
    height = maxy - miny
    area = Polygon(ring.coords).area
    for mw, mh, ma in _dead_ring_profiles:
        if mw < width and mh < height and ma < area:
            return True
    return False


def _prune_dead_rings(polygon):
    """Return `polygon` with every provably-dead interior ring removed.

    Returns the input unchanged when there is nothing to prune, when no part set
    has been published, or when pruning is disabled -- so it is a safe no-op for
    any caller in any state, rather than assuming the registry is armed.
    """
    if not _dead_ring_profiles:
        return polygon
    if polygon is None or polygon.is_empty:
        return polygon
    if polygon.geom_type != 'Polygon' or not polygon.interiors:
        return polygon

    live = [ring for ring in polygon.interiors if _ring_is_live(ring)]
    dropped = len(polygon.interiors) - len(live)
    if not dropped:
        return polygon

    pruned = Polygon(polygon.exterior.coords, live)
    if pruned.is_empty or not pruned.is_valid:
        # Never trade correctness for the speed-up: an unusable prune is
        # discarded and the caller decomposes the original.
        return polygon

    # rings_seen is bumped in the same call as rings_dropped and is a superset
    # of it, so the two must never be updated in separate critical sections.
    _bump_dead_ring_stats(
        polygons_pruned=1,
        rings_seen=len(polygon.interiors),
        rings_dropped=dropped,
    )
    return pruned


def decompose_if_needed(polygon, logger):
    """Decomposes a non-convex polygon into convex parts (triangles)."""
    if not polygon or polygon.is_empty:
        return []
    
    # Ensure valid geometry
    if not polygon.is_valid:
        polygon = polygon.buffer(0)

    # Drop interior rings that no nestable part can occupy, BEFORE the cache
    # key is taken, so the pruned polygon gets its own cache entry and the
    # unpruned one is never polluted. The polygon the caller centres on is
    # untouched -- only what gets triangulated changes. _prune_dead_rings is a
    # no-op when the part set has not been published.
    polygon = _prune_dead_rings(polygon)

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
        if _dead_ring_profiles:
            _bump_dead_ring_stats(parts_from_misses=len(parts))
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


def _minkowski_merge_ring(prepared_a, prepared_b):
    """Edge-merge two prepared convex rings into the open result ring.

    Returns the list of unique vertices (no closing point). Building the
    Shapely ``Polygon`` from the ring is intentionally left to the caller so
    that bulk construction can batch every pair polygon into a single
    ``from_ragged_array`` call instead of constructing one Polygon object per
    pair.
    """
    vertices_a, edges_a = prepared_a
    vertices_b, edges_b = prepared_b
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
    return result


def _minkowski_sum_convex_prepared(prepared_a, prepared_b, phase_stats=None):
    """Compute a convex Minkowski sum from prepared convex rings."""
    t_merge = time.perf_counter()
    result = _minkowski_merge_ring(prepared_a, prepared_b)
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
    # Phase 1: merge every convex pair into a raw open ring. This is pure
    # Python arithmetic (no per-pair Shapely geometry objects), which keeps
    # the GIL held only briefly; the Polygon construction is deferred and
    # batched in one call below.
    rings = []              # open result rings, aligned with pair_indexes
    pair_indexes = []       # (index_a, index_b) aligned with rings
    fallback_indexes = []   # (index_a, index_b) needing the reference result
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
            t_merge = time.perf_counter()
            try:
                ring = _minkowski_merge_ring(prepared_piece_a, prepared_piece_b)
            except (TypeError, ValueError, IndexError):
                fallback_indexes.append((index_a, index_b))
                continue
            phase_stats["convex_merge_ms"] += (time.perf_counter() - t_merge) * 1000
            if len(ring) < 3:
                # A ring with fewer than 3 vertices cannot form a polygon;
                # pair polygon construction would raise for it, so it falls
                # back to the reference result exactly as the legacy
                # per-pair Polygon() path did.
                fallback_indexes.append((index_a, index_b))
                continue
            phase_stats["convex_result_points"] += len(ring)
            rings.append(ring)
            pair_indexes.append((index_a, index_b))

    # Phase 2: construct every pair polygon in a single Shapely call. One
    # from_ragged_array call builds the whole batch from the flat coordinate
    # buffer instead of constructing a Polygon per pair, which serialized on
    # the GIL across the GA's concurrent rotation threads.
    t_polygon = time.perf_counter()
    if rings:
        coords = []
        ring_offsets = [0]
        for ring in rings:
            coords.extend(ring)
            ring_offsets.append(len(coords))
        coord_arr = np.asarray(coords, dtype=np.float64)
        ring_idx = np.asarray(ring_offsets, dtype=np.int64)
        poly_idx = np.arange(len(rings) + 1, dtype=np.int64)
        result_polygons = from_ragged_array(
            GeometryType.POLYGON, coord_arr, offsets=(ring_idx, poly_idx)
        )
    else:
        result_polygons = np.empty(0, dtype=object)
    phase_stats["convex_polygon_create_ms"] += (time.perf_counter() - t_polygon) * 1000

    # Phase 3: assemble minkowski_parts, applying the per-pair checks only
    # when requested (validate_convex_pairs) and falling back to the
    # reference result for any pair that failed to merge or failed the checks.
    minkowski_parts = []
    if validate_convex_pairs:
        for (index_a, index_b), result in zip(pair_indexes, result_polygons):
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
    else:
        for result in result_polygons:
            t_append = time.perf_counter()
            minkowski_parts.append(result)
            phase_stats["convex_pair_append_ms"] += (
                time.perf_counter() - t_append
            ) * 1000
    for index_a, index_b in fallback_indexes:
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