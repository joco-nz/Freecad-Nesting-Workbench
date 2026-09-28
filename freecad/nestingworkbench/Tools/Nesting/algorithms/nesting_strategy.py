# SPDX-License-Identifier: LGPL-2.1-or-later

import math
import os
import random
import time
import threading
import copy
from datetime import datetime
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import numpy as np
from shapely.affinity import rotate, translate
from shapely.geometry import Polygon, GeometryCollection

import FreeCAD
from ....datatypes.sheet import Sheet
from ....datatypes.placed_part import PlacedPart
from ....datatypes.shape import Shape
from . import genetic_utils
from .minkowski_engine import MinkowskiEngine


def _candidate_geometry_key(prefix, x, y):
    """Build the complete run-local candidate geometry cache key."""
    return (prefix, float(x), float(y))


def _step_size(kwargs):
    """Resolve the NFP discretisation interval: env, then kwargs, then 5.0 mm.

    `NESTING_STEP_SIZE` exists so the time/density curve can be characterised
    without editing code or building a UI, the same reason
    `NESTING_ROTATION_WORKERS` exists. Both are reported in the run output so a
    measurement taken with an override is self-describing.
    """
    raw = os.environ.get('NESTING_STEP_SIZE', '').strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    try:
        return float(kwargs.get("step_size", 5.0))
    except (TypeError, ValueError):
        return 5.0


def _rotation_worker_limit():
    """Resolve the rotation thread-pool size, or None for the stdlib default.

    ``ThreadPoolExecutor()`` with no argument defaults to
    ``min(32, os.cpu_count() + 4)``, which is 8 on the 4-CPU development box --
    twice the core count. The candidate-geometry work is GEOS-bound and releases
    the GIL, so oversubscription is not automatically harmful, but it is also not
    automatically helpful: it trades throughput for cache pressure and
    contention on the shared NFP cache. The effective width is therefore
    measurable rather than assumed.

    ``NESTING_ROTATION_WORKERS`` overrides it. This exists so the 4-vs-8
    comparison can be run without editing code; unset means "stdlib default",
    which is the production behaviour and is what the fixture baseline used.
    """
    raw = os.environ.get('NESTING_ROTATION_WORKERS', '').strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


# Measurement-only. Ring area is needed to classify an interior ring as able or
# unable to hold a nestable part, and a LinearRing exposes no area of its own.
#
# This is deliberately NOT a WeakKeyDictionary keyed on the geometry. Shapely
# geometries have no __dict__, and such a dict would re-hash the whole WKB on
# every lookup -- 47.6 us for a 600-vertex polygon here, ~9 s across a fixture
# run -- while missing anyway, because list(poly.interiors) returns fresh
# wrapper objects on each call. Shapely also compares geometries by VALUE, so a
# value-keyed cache would merge distinct-but-identical placements.
#
# Callers reach this through _part_profile, which memoises on the owning Shape
# and so keeps each ring's area computed once per placement rather than once
# per candidate-mask call.
def _ring_area(ring):
    """Area of an interior LinearRing, as a Polygon."""
    return Polygon(ring).area


def _holes_touched(candidate, holes, hole_bbox, cx0, cy0, cx1, cy1):
    """True if `candidate` reaches into `holes`, cheaply.

    The caller's already-computed candidate extents reject almost every pair
    before GEOS is involved: rings sit strictly inside their part, and most
    candidates are nowhere near a hole. Without this the accepted-candidate
    touch test dominates the instrumentation.
    """
    if holes is None:
        return False
    if hole_bbox is None:
        return False
    if cx1 < hole_bbox[0] or cx0 > hole_bbox[2]:
        return False
    if cy1 < hole_bbox[1] or cy0 > hole_bbox[3]:
        return False
    return candidate.intersects(holes)


def _part_profile(owner, poly):
    """Everything the hole measurement needs about one placed part, memoised.

    Returns (width, height, area, rings, hole_geometry, hole_bbox) where `rings`
    is a list of (area, vertex_count, bounds) per interior ring.

    Caching is keyed on `owner` -- the Shape, an ordinary Python object that
    accepts attributes. Two alternatives were measured and rejected:

    - Shapely geometries have no __dict__, so attributes cannot be attached to
      the geometry itself.
    - A WeakKeyDictionary keyed on the geometry re-hashes the whole WKB on
      every lookup: 47.6 us for a 600-vertex polygon on this machine, ~9 s over
      a fixture run at 60 parts x 3099 calls. It would miss regardless, because
      `list(poly.interiors)` hands back fresh wrapper objects each call. Shapely
      also hashes and compares geometries by VALUE, so such a dict would merge
      distinct-but-identical placements.

    The `is` test on the polygon is what makes this safe. `set_rotation` and
    `move` rebind `shape.polygon`, so a rebound polygon fails the identity check
    and is recomputed rather than returning another placement's numbers.

    `owner` is None for the candidate under test, which is a fresh rotation each
    call and not worth caching.
    """
    if owner is not None:
        cached = getattr(owner, '_nfp_hole_profile', None)
        if cached is not None and cached[0] is poly:
            return cached[1]
    min_x, min_y, max_x, max_y = poly.bounds
    ring_objs = list(poly.interiors)
    rings = []
    for ring in ring_objs:
        rmin_x, rmin_y, rmax_x, rmax_y = ring.bounds
        rings.append((
            _ring_area(ring),
            len(ring.coords),
            (rmin_x, rmin_y, rmax_x, rmax_y),
        ))
    if ring_objs:
        hole_geom = _rings_geometry(ring_objs)
        hole_bbox = (
            min(r.bounds[0] for r in ring_objs),
            min(r.bounds[1] for r in ring_objs),
            max(r.bounds[2] for r in ring_objs),
            max(r.bounds[3] for r in ring_objs),
        )
    else:
        hole_geom = hole_bbox = None
    profile = (max_x - min_x, max_y - min_y, poly.area, rings,
               hole_geom, hole_bbox)
    if owner is not None:
        owner._nfp_hole_profile = (poly, profile)
    return profile


def _rings_geometry(rings):
    """One geometry covering all of a part's interior rings.

    A single ring becomes a Polygon; several become a GeometryCollection, which
    keeps `intersects` exact without paying for a union on every call.
    """
    polys = [Polygon(r) for r in rings]
    return polys[0] if len(polys) == 1 else GeometryCollection(polys)


class CandidateGeometryKeyTracker:
    """Track candidate geometry keys across one or more optimizers.

    This is measurement-only. It deliberately does not retain polygons or
    affect candidate decisions; the shared scope lets GA diagnostics measure
    reuse across layouts as well as within a single layout.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._keys = set()

    def observe(self, prefix, points, indices):
        """Return (new, repeated) counts for the supplied candidate rows."""
        unique_count = 0
        repeat_count = 0
        with self._lock:
            for index in indices:
                key = _candidate_geometry_key(
                    prefix, points[index, 0], points[index, 1]
                )
                if key in self._keys:
                    repeat_count += 1
                else:
                    self._keys.add(key)
                    unique_count += 1
        return unique_count, repeat_count

    def clear(self):
        """Release all diagnostic keys retained by this run."""
        with self._lock:
            self._keys.clear()


class CandidateGeometryCache:
    """Per-run cache of translated candidate polygons.

    Only geometry is cached. Collision, sheet-boundary, and scoring results
    remain dependent on the current sheet layout and are always recomputed.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._polygons = {}

    def get(self, key):
        with self._lock:
            return self._polygons.get(key)

    def put(self, key, polygon):
        with self._lock:
            self._polygons[key] = polygon

    def clear(self):
        """Release all translated polygons retained by this run."""
        with self._lock:
            self._polygons.clear()

    def __len__(self):
        with self._lock:
            return len(self._polygons)


class PlacementOptimizer:
    """
    Handles the geometric logic of finding the best position for a part on a sheet.
    """
    def __init__(
        self, engine, rotation_steps, search_direction, log_callback=None,
        trial_callback=None, rng=None, performance_logging=False,
        candidate_geometry_key_tracker=None, candidate_geometry_cache=None
    ):
        self.engine = engine
        self.rotation_steps = max(1, rotation_steps)
        self.search_direction = search_direction
        self.log_callback = log_callback
        self.trial_callback = trial_callback  # Called for each trial placement in simulation mode
        self.rng = rng or random  # Seeded random.Random for reproducible runs, or the global module
        self.verbose = False
        self.performance_logging = performance_logging
        self._perf_lock = threading.Lock()
        self._active_workers = 0
        # Measurement-only tracker. A tracker can be shared by the GA
        # coordinator to measure key reuse across layouts; it never stores
        # geometry and is not used for caching or decisions. Avoid creating
        # an empty tracker when Performance Logging is disabled.
        self._candidate_geometry_key_tracker = (
            candidate_geometry_key_tracker
            if candidate_geometry_key_tracker is not None
            else (CandidateGeometryKeyTracker() if performance_logging else None)
        )
        self._candidate_geometry_cache = candidate_geometry_cache
        self._perf_stats = {
            'rotation_evaluations': 0,
            'successful_rotations': 0,
            'candidate_points': 0,
            'valid_candidate_points': 0,
            'nfp_ms': 0.0,
            'candidate_validity_ms': 0.0,
            'score_ms': 0.0,
            'bounds_survivors': 0,
            'sheet_candidates': 0,
            'sheet_rejections': 0,
            'sheet_boundary_candidates': 0,
            'collision_candidates': 0,
            'collision_rejections': 0,
            'bbox_rejections': 0,
            'polygon_checks': 0,
            'candidate_geometry_ms': 0.0,
            'sheet_difference_ms': 0.0,
            'collision_intersection_ms': 0.0,
            # The collision stage's decomposition. These are not decoration:
            # the absorption loop below does `self._perf_stats[key] += ...`, so
            # a key listed there but absent here raises KeyError inside the
            # per-rotation future loop. `quiet=True` -- which every benchmark and
            # every GA layout sets -- routes self.log to nothing, so the
            # exception is swallowed with no output at all: every rotation
            # evaluation dies, no candidates are produced, and the run reports
            # zero placed parts and zero NFP computations while looking
            # otherwise healthy. Every key in that allowlist must appear here.
            'collision_intersects_ms': 0.0,
            'collision_overlay_ms': 0.0,
            'collision_hole_probe_ms': 0.0,
            'collision_intersects_calls': 0,
            'collision_overlay_calls': 0,
            'collision_hole_probe_calls': 0,
            'collision_overlay_area_zero': 0,
            'collision_overlay_area_sub_tol': 0,
            'collision_overlay_area_over_tol': 0,
            'collision_overlay_area_total': 0.0,
            # Measurement-only candidate-path counters.
            'placement_wall_ms': 0.0,
            'rotation_wall_ms': 0.0,
            'candidate_evaluation_wall_ms': 0.0,
            'candidate_geometries_built': 0,
            'candidate_geometry_observations': 0,
            'candidate_geometry_cache_hits': 0,
            'candidate_geometry_cache_misses': 0,
            'candidate_geometry_cache_ms': 0.0,
            'candidate_geometry_cache_entries': 0,
            'bbox_checks': 0,
            'bbox_overlap_pairs': 0,
            'exact_collision_checks': 0,
            # Split of exact_collision_checks by the intersects prefilter.
            # Their sum must equal exact_collision_checks.
            'collision_intersects_true': 0,
            'collision_intersects_false': 0,
            # Subset of collision_intersects_true whose overlay area came out
            # within tolerance. The remainder are genuine overlaps, and
            # intersects_true - grazing_pairs == collision_rejections holds
            # exactly because the loop breaks at the first real overlap, so
            # each rejected candidate consumes exactly one overlapping pair.
            #
            # DO NOT read this as "the interiors touch". A grazing pair only
            # means the overlay reported area <= tolerance, and a tolerance of
            # 1e-7 is equally satisfied by an overlap of area 1e-8. On the
            # 122-part fixture the grazing set is overwhelmingly sub-micron
            # slivers with genuinely overlapping interiors -- 148,124 of
            # 152,431, measured -- not exact contacts. A DE-9IM zero-area gate
            # was built on the mistaken reading that this set was exact touch,
            # found it could only settle 2.8% of it, and was reverted as a
            # net loss. See the revert of 34bbe67 before proposing it again.
            'collision_grazing_pairs': 0,
            # Interior-ring population of the collision mask, and the number of
            # placements that are legal only because a hole is empty space.
            # Summed, except mask_batch_max which is a max.
            'mask_hole_rings': 0,
            'mask_hole_vertices': 0,
            'mask_exterior_vertices': 0,
            'mask_hole_sensitive_pairs': 0,
            'mask_hole_exploiting_placements': 0,
            'mask_candidate_rings': 0,
            'mask_subthreshold_hole_rings': 0,
            'mask_subthreshold_hole_vertices': 0,
            'mask_calls': 0,
            'mask_batch_candidates': 0,
            'mask_batch_max': 0,
            'candidate_geometry_unique': 0,
            'candidate_geometry_repeats': 0,
            'max_concurrent_rotations': 0,
            'rotation_workers': 0,
        }

    def log(self, message):
        if self.log_callback:
            self.log_callback(message)

    def find_best_placement(self, part, sheet):
        """
        Parallel evaluation of rotations to find best spot.
        """
        if part.original_polygon is None and part.polygon is not None:
            part.original_polygon = part.polygon
            
        # Pre-group placed parts by (master_label, angle)
        placed_parts_grouped = defaultdict(list)
        for p in sheet.parts:
            key = (p.shape.source_freecad_object.Label, p.angle)
            placed_parts_grouped[key].append(p)
            
        direction = self.search_direction
        if direction is None:
             angle_rad = self.rng.uniform(0, 2 * math.pi)
             direction = (math.cos(angle_rad), math.sin(angle_rad))

        best_result = {'metric': float('inf')}
        
        part_rotation_steps = getattr(part, 'rotation_steps', None)
        if part_rotation_steps is None or part_rotation_steps < 1:
            part_rotation_steps = self.rotation_steps
        part_rotation_steps = max(1, part_rotation_steps)
        
        gene_angle = getattr(part, 'gene_angle', None)
        if gene_angle is not None:
            angles = [gene_angle % 360.0]
        else:
            angles = [i * (360.0 / part_rotation_steps) for i in range(part_rotation_steps)]
        
        # Parallel evaluation — one thread per rotation. Candidate point-in-polygon
        # rejection runs on the CPU via shapely.
        import time as _time
        t0_parallel = _time.perf_counter()
        total_nfp_ms = 0.0
        total_validity_ms = 0.0
        total_score_ms = 0.0
        # Recorded so the report line is self-describing: a run measured with
        # the env override must say so, otherwise the 4-vs-8 comparison is
        # unattributable. max_workers=None means the stdlib default.
        worker_limit = _rotation_worker_limit()
        with self._perf_lock:
            self._perf_stats['rotation_workers'] = (
                worker_limit
                if worker_limit is not None
                else min(32, (os.cpu_count() or 1) + 4)
            )
        with ThreadPoolExecutor(max_workers=worker_limit) as executor:
            futures = {
                executor.submit(
                    self._evaluate_rotation_tracked,
                    angle,
                    part,
                    placed_parts_grouped,
                    sheet,
                    direction,
                ): angle
                for angle in angles
            }

            for future in as_completed(futures):
                try:
                    res = future.result()
                    if res:
                        total_nfp_ms += res.get('_t_nfp_ms', 0)
                        total_validity_ms += res.get('_t_validity_ms', 0)
                        total_score_ms += res.get('_t_score_ms', 0)
                        with self._perf_lock:
                            self._perf_stats['rotation_evaluations'] += 1
                            self._perf_stats['successful_rotations'] += int(
                                res.get('x') is not None)
                            self._perf_stats['candidate_points'] += res.get(
                                '_candidate_points', 0)
                            self._perf_stats['valid_candidate_points'] += res.get(
                                '_valid_candidate_points', 0)
                            self._perf_stats['nfp_ms'] += res.get('_t_nfp_ms', 0)
                            self._perf_stats['candidate_validity_ms'] += res.get(
                                '_t_validity_ms', 0)
                            self._perf_stats['score_ms'] += res.get('_t_score_ms', 0)
                            for key in (
                                'bounds_survivors', 'sheet_candidates',
                                'sheet_rejections', 'sheet_boundary_candidates',
                                'collision_candidates',
                                'collision_rejections', 'bbox_rejections',
                                'polygon_checks',
                            ):
                                self._perf_stats[key] += res.get(f'_{key}', 0)
                            self._perf_stats['candidate_geometry_ms'] += res.get(
                                '_candidate_geometry_ms', 0)
                            self._perf_stats['sheet_difference_ms'] += res.get(
                                '_sheet_difference_ms', 0)
                            self._perf_stats['collision_intersection_ms'] += res.get(
                                '_collision_intersection_ms', 0)
                            # The collision stage's decomposition. The probe
                            # keys arrive here underscore-prefixed -- that is the
                            # convention every other absorbed key uses, and
                            # reading them unprefixed yields a silent 0 rather
                            # than an error, because .get() defaults.
                            for _key in ('collision_intersects_ms',
                                          'collision_overlay_ms',
                                          'collision_hole_probe_ms'):
                                self._perf_stats[_key] = self._perf_stats.get(
                                    _key, 0.0) + res.get(f'_{_key}', 0.0)
                            self._perf_stats['rotation_wall_ms'] += res.get(
                                '_t_wall_ms', 0)
                            self._perf_stats['candidate_evaluation_wall_ms'] += res.get(
                                '_t_candidate_evaluation_ms', 0)
                            for key in (
                                'candidate_geometries_built',
                                'candidate_geometry_cache_hits',
                                'candidate_geometry_cache_misses',
                                'candidate_geometry_cache_ms',
                                'bbox_checks', 'bbox_overlap_pairs',
                                'exact_collision_checks',
                                'collision_intersects_true',
                                'collision_intersects_false',
                                'collision_grazing_pairs',
                                'mask_hole_rings', 'mask_hole_vertices',
                                'mask_exterior_vertices',
                                'mask_hole_sensitive_pairs',
                                'mask_hole_exploiting_placements',
                                'mask_candidate_rings',
                                'mask_subthreshold_hole_rings',
                                'mask_subthreshold_hole_vertices',
                                'mask_calls', 'mask_batch_candidates',
                                'collision_intersects_calls',
                                'collision_overlay_calls',
                                'collision_hole_probe_calls',
                                'collision_overlay_area_zero',
                                'collision_overlay_area_sub_tol',
                                'collision_overlay_area_over_tol',
                                'collision_overlay_area_total',
                            ):
                                self._perf_stats[key] += res.get(f'_{key}', 0)
                            self._perf_stats['mask_batch_max'] = max(
                                self._perf_stats['mask_batch_max'],
                                res.get('_mask_batch_max', 0),
                            )
                            self._perf_stats['candidate_geometry_cache_entries'] = max(
                                self._perf_stats['candidate_geometry_cache_entries'],
                                res.get('_candidate_geometry_cache_entries', 0),
                            )
                        if res['metric'] < best_result['metric']:
                            best_result = res
                            # Call trial callback from main thread for each better result found
                            if self.trial_callback and best_result.get('x') is not None:
                                self.trial_callback(part, best_result['angle'], best_result['x'], best_result['y'])
                except Exception as e:
                    self.log(f"Error in rotation evaluation thread: {e}")

        dt_parallel = (_time.perf_counter() - t0_parallel) * 1000
        with self._perf_lock:
            self._perf_stats['placement_wall_ms'] += dt_parallel
        if self.performance_logging:
            self.log(f"[TIMING] '{getattr(part, 'id', '?')}': wall={dt_parallel:.0f}ms "
                     f"nfp={total_nfp_ms:.0f}ms validity={total_validity_ms:.0f}ms "
                     f"score={total_score_ms:.0f}ms "
                     f"({len(angles)} rotations, {len(sheet.parts)} placed)")
        if self.verbose:
            self.log(f"  -> Parallel eval: {len(angles)} rotations in {dt_parallel:.1f}ms "
                     f"(ideal speedup: {len(angles)}x, pool workers: {min(len(angles), os.cpu_count() or 1)})")
        best_result['_t_nfp_ms'] = total_nfp_ms
        best_result['_t_validity_ms'] = total_validity_ms
        best_result['_t_score_ms'] = total_score_ms
        
        if self.verbose:
            self.log(f"  -> Best result for {part.id}: {best_result}")



        if best_result.get('x') is not None:
             part.set_rotation(best_result['angle'], reposition=False)
             curr = part.centroid
             part.move(best_result['x'] - curr.x, best_result['y'] - curr.y)
             return part
        return None

    def _evaluate_rotation_tracked(
        self, angle, part, placed_parts_grouped, sheet, direction
    ):
        """Track rotation-worker occupancy without changing evaluation behavior."""
        if self.performance_logging:
            with self._perf_lock:
                self._active_workers += 1
                self._perf_stats['max_concurrent_rotations'] = max(
                    self._perf_stats['max_concurrent_rotations'],
                    self._active_workers,
                )
        try:
            return self._evaluate_rotation(
                angle, part, placed_parts_grouped, sheet, direction
            )
        finally:
            if self.performance_logging:
                with self._perf_lock:
                    self._active_workers -= 1

    @staticmethod
    def _candidate_geometry_key_prefix(part, angle):
        source = getattr(part, 'source_freecad_object', None)
        document = getattr(source, 'Document', None)
        if source is None:
            source_identity = (None, None, id(part))
        else:
            source_identity = (
                getattr(document, 'Name', ''),
                getattr(source, 'Name', ''),
                id(source),
            )
        return (
            source_identity,
            float(getattr(part, 'spacing', 0.0)),
            float(getattr(part, 'deflection', 0.0)),
            float(getattr(part, 'simplification', 0.0)),
            angle % 360.0,
        )

    def _record_candidate_geometry_keys(self, part, angle, points, indices):
        """Count reuse for rows that actually construct candidate geometry."""
        if (
            not self.performance_logging
            or self._candidate_geometry_key_tracker is None
            or points is None
            or indices is None
            or len(indices) == 0
        ):
            return
        prefix = self._candidate_geometry_key_prefix(part, angle)
        unique_count, repeat_count = self._candidate_geometry_key_tracker.observe(
            prefix, points, indices
        )
        with self._perf_lock:
            self._perf_stats['candidate_geometry_observations'] += len(indices)
            self._perf_stats['candidate_geometry_unique'] += unique_count
            self._perf_stats['candidate_geometry_repeats'] += repeat_count

    def _evaluate_rotation(self, angle, part, placed_parts_grouped, sheet, direction):
        """
        Evaluates placing the part at a given rotation angle on the sheet.
        
        MATHEMATICAL SCORING RATIONALE & TRADEOFFS:
        In nesting algorithms, placement scoring guides candidate selection by balancing multiple objectives.
        While this nester defaults to a gravity-aligned vector projection, complex multi-objective 
        nesting can evaluate candidates using a composite score:
            score = (0.4 * y_norm) + (0.3 * x_norm) + (0.2 * waste_ratio) + (0.1 * contact_score)
            
        Where:
        - y_norm (weight 0.4): Normalised vertical height. Pushing parts to the bottom (gravity bias) 
          is critical for bottom-up sheet packing. A high weight preserves vertical space.
        - x_norm (weight 0.3): Normalised horizontal position. Directs parts toward one side (e.g., left),
          ensuring parts pack tightly in columns.
        - waste_ratio (weight 0.2): Ratio of local bounding box waste (empty space inside the part's 
          rectangular bounds). Lower waste is preferred for irregular/asymmetric shapes.
        - contact_score (weight 0.1): Reward for touching/nesting along existing parts (interlocking).
          Helps fit concave sections together.
          
        Tuning Guide:
        - To maximize strip-packing density, increase the gravity/side weights (y_norm/x_norm).
        - To improve placement of highly irregular/concave shapes, increase contact_score and waste_ratio.
        """
        import time as _time, threading
        t0 = _time.perf_counter()
        thread_id = threading.current_thread().name

        rotated_poly = rotate(part.original_polygon, angle, origin='centroid')
        if not rotated_poly: return {'metric': float('inf')}

        # Candidate positions are centroid positions — express the rotated
        # bounds relative to the centroid for corner seeds and bounds checks.
        centroid = rotated_poly.centroid
        min_x, min_y, max_x, max_y = rotated_poly.bounds
        extents = (min_x - centroid.x, min_y - centroid.y,
                   max_x - centroid.x, max_y - centroid.y)
        w_bin, h_bin = self.engine.bin_width, self.engine.bin_height
        corners = np.array([
            [-extents[0],         -extents[1]        ],
            [w_bin - extents[2],  -extents[1]        ],
            [-extents[0],         h_bin - extents[3] ],
            [w_bin - extents[2],  h_bin - extents[3] ],
        ], dtype=np.float64)

        pts_arr = self.engine.get_incremental_candidates(part, angle, sheet, corners, extents)
        t_nfp = _time.perf_counter()

        best = {'metric': float('inf')}
        t_validity = t_nfp
        valid_mask = None
        validity_probe = {}
        geometry_key_prefix = (
            self._candidate_geometry_key_prefix(part, angle)
            if self._candidate_geometry_cache is not None else None
        )
        if pts_arr is not None and len(pts_arr):
            valid_mask = self._exact_candidate_mask(
                rotated_poly,
                pts_arr,
                sheet,
                probe=validity_probe,
                candidate_geometry_cache=self._candidate_geometry_cache,
                geometry_key_prefix=geometry_key_prefix,
                performance_logging=self.performance_logging,
            )
            t_validity = _time.perf_counter()
            best_idx, metric = MinkowskiEngine.score_gravity(pts_arr, valid_mask, direction, rng=self.rng)
            if best_idx is not None:
                best = {'x': float(pts_arr[best_idx, 0]), 'y': float(pts_arr[best_idx, 1]),
                        'angle': angle, 'metric': metric}

        # Notify better result found
        if self.trial_callback and best.get('x') is not None:
             self.trial_callback(part, angle, best['x'], best['y'])

        t_end = _time.perf_counter()
        geometry_indices = validity_probe.pop('_geometry_candidate_indices', None)
        self._record_candidate_geometry_keys(
            part, angle, pts_arr, geometry_indices
        )
        if self.performance_logging:
            self.log(f"    [{thread_id}] angle={angle:.0f}: NFP={((t_nfp-t0)*1000):.1f}ms, "
                     f"validity={((t_validity-t_nfp)*1000):.1f}ms, "
                     f"score={((t_end-t_validity)*1000):.1f}ms, "
                     f"total={((t_end-t0)*1000):.1f}ms")

        best['_t_nfp_ms'] = (t_nfp - t0) * 1000
        best['_t_validity_ms'] = (t_validity - t_nfp) * 1000
        best['_t_score_ms'] = (t_end - t_validity) * 1000
        best['_t_wall_ms'] = (t_end - t0) * 1000
        best['_t_candidate_evaluation_ms'] = (t_validity - t_nfp) * 1000
        best['_candidate_points'] = len(pts_arr) if pts_arr is not None else 0
        best['_valid_candidate_points'] = int(valid_mask.sum()) if valid_mask is not None else 0
        for key, value in validity_probe.items():
            best[f'_{key}'] = value
        return best

    @staticmethod
    def _exact_candidate_mask(
        rotated_poly, points, sheet, area_tolerance=1e-7, probe=None,
        candidate_geometry_cache=None, geometry_key_prefix=None,
        performance_logging=False,
    ):
        """Validate NFP candidates against the actual transformed polygons.

        NFP boundaries are a candidate generator, not the final collision
        proof. This exact check is especially important for internal-fit
        candidates, where a discretized or invalid IFP can otherwise admit a
        centroid whose part crosses the containing part's boundary.

        Two redundant geometry steps are skipped without changing acceptance:

        - The GEOS ``candidate.difference(bin_polygon)`` sheet check only runs
          for candidates whose bounding box crosses the actual sheet boundary.
          A polygon is always contained in its own bbox, so a candidate whose
          bbox lies wholly inside the sheet rectangle has a provably empty
          difference; the bounds pre-filter already guarantees that hold to
          within ``area_tolerance``.
        - A translated candidate geometry is constructed lazily, only when it
          is actually needed: candidates whose bbox crosses the sheet boundary
          or whose bbox overlaps an existing part's bbox.

        Candidate bounding boxes are computed from the already-known rotated
        extents with numpy, so the collision stage screens bbox overlaps
        vectorially and only runs exact ``intersection`` checks for the
        overlapping pairs. The existing-part iteration order, the collision
        short-circuit, and the area-tolerance semantics are unchanged.
        """
        if probe is not None:
            probe['candidate_count'] = len(points)

        min_x, min_y, max_x, max_y = rotated_poly.bounds
        rotated_centroid = rotated_poly.centroid
        rminx, rminy = min_x - rotated_centroid.x, min_y - rotated_centroid.y
        rmaxx, rmaxy = max_x - rotated_centroid.x, max_y - rotated_centroid.y

        valid = (
            (points[:, 0] + rminx >= -area_tolerance)
            & (points[:, 0] + rmaxx <= sheet.width + area_tolerance)
            & (points[:, 1] + rminy >= -area_tolerance)
            & (points[:, 1] + rmaxy <= sheet.height + area_tolerance)
        )
        if probe is not None:
            probe['bounds_survivors'] = int(valid.sum())
            probe['candidate_geometries_built'] = 0
            probe['bbox_checks'] = 0
            probe['bbox_overlap_pairs'] = 0
            probe['exact_collision_checks'] = 0
            probe['collision_intersects_true'] = 0
            probe['collision_intersects_false'] = 0
            probe['collision_grazing_pairs'] = 0
            # Hole-population measurement. See the annotated block below.
            probe['mask_hole_rings'] = 0
            probe['mask_hole_vertices'] = 0
            probe['mask_exterior_vertices'] = 0
            probe['mask_hole_sensitive_pairs'] = 0
            probe['mask_hole_exploiting_placements'] = 0
            probe['mask_candidate_rings'] = 0
            probe['mask_subthreshold_hole_rings'] = 0
            probe['mask_subthreshold_hole_vertices'] = 0
            probe['mask_calls'] = 0
            probe['mask_batch_candidates'] = 0
            probe['mask_batch_max'] = 0
        if not valid.any():
            return valid

        existing_shapes = [
            placed.shape
            for placed in sheet.parts
            if placed.shape and placed.shape.polygon
        ]
        existing_polygons = [shape.polygon for shape in existing_shapes]
        if not existing_polygons:
            if probe is not None:
                probe['sheet_candidates'] = int(valid.sum())
                probe['collision_candidates'] = int(valid.sum())
            return valid

        # ---- Measurement-only: interior-ring population of the collision mask.
        # Nothing in this block influences `valid`, `overlaps` or candidate
        # ordering; it exists to size the hole population before deciding
        # whether any hole-related change is worth making.
        #
        # `hole_geoms[i]` is existing part i's interior rings as one geometry,
        # or None; `hole_bounds[i]` is the union bbox of those rings. Both are
        # indexed per existing part, never merged into one run-wide collection:
        # a merged GeometryCollection of ~163 rings made `intersects` on the
        # accepted-candidate path cost ~45 s over a fixture run, which was the
        # bulk of this instrumentation's own overhead. Rings lie strictly inside
        # their part, so a candidate that misses a part's bbox cannot reach its
        # holes -- testing per part is exact, not an approximation.
        hole_geoms = None
        hole_bounds = None
        if probe is not None:
            # Parts that could be nested into a hole. The test mirrors the NFP
            # engine's own filter in _compute_nfp_uncached -- strictly narrower
            # bbox on both axes AND smaller area -- so "sub-threshold" here
            # means exactly "the NFP will not build a legal-position island for
            # this ring".
            #
            # The three per-axis minima prune the scan: a ring no larger than
            # the smallest part in any one dimension cannot pass the strict
            # test, so most rings are classified without the inner `any()`.
            profiles = [
                _part_profile(shape, poly)
                for shape, poly in zip(existing_shapes, existing_polygons)
            ]
            cand_profile = _part_profile(None, rotated_poly)
            metrics = [(p[0], p[1], p[2]) for p in profiles]
            metrics.append((cand_profile[0], cand_profile[1], cand_profile[2]))
            min_w = min(m[0] for m in metrics)
            min_h = min(m[1] for m in metrics)
            min_a = min(m[2] for m in metrics)

            hole_geoms = []
            hole_bounds = []
            for poly, (_pw, _ph, _pa, rings, geom, bbox) in zip(
                    existing_polygons, profiles):
                probe['mask_hole_rings'] += len(rings)
                for r_area, r_verts, (r0x, r0y, r1x, r1y) in rings:
                    probe['mask_hole_vertices'] += r_verts
                    rw, rh = r1x - r0x, r1y - r0y
                    if (rw <= min_w or rh <= min_h or r_area <= min_a
                            or not any(mw < rw and mh < rh and ma < r_area
                                       for mw, mh, ma in metrics)):
                        probe['mask_subthreshold_hole_rings'] += 1
                        probe['mask_subthreshold_hole_vertices'] += r_verts
                hole_geoms.append(geom)
                hole_bounds.append(bbox)
                probe['mask_exterior_vertices'] += len(poly.exterior.coords)

            # The candidate's own rings matter for `existing.intersects(...)`
            # cost, and for a candidate that is itself re-tested as an existing
            # part in a later call of the same run.
            _cw, _ch, _ca, cand_rings, _cg, _cb = cand_profile
            probe['mask_candidate_rings'] += len(cand_rings)
            for r_area, r_verts, (r0x, r0y, r1x, r1y) in cand_rings:
                probe['mask_hole_vertices'] += r_verts
                rw, rh = r1x - r0x, r1y - r0y
                if (rw <= min_w or rh <= min_h or r_area <= min_a
                        or not any(mw < rw and mh < rh and ma < r_area
                                   for mw, mh, ma in metrics)):
                    probe['mask_subthreshold_hole_rings'] += 1
                    probe['mask_subthreshold_hole_vertices'] += r_verts
            probe['mask_exterior_vertices'] += len(rotated_poly.exterior.coords)

        bin_polygon = Polygon(
            [(0, 0), (sheet.width, 0), (sheet.width, sheet.height), (0, sheet.height)]
        )
        if probe is not None:
            probe['sheet_candidates'] = int(valid.sum())

        # Candidate bounding boxes are exact arithmetic on the known rotated
        # extents — no Shapely geometry or GEOS call is required.
        idx = np.flatnonzero(valid)
        if probe is not None:
            # Batch size decides whether shapely.prepare() on the existing
            # polygons could amortise: preparing costs real time, so it only
            # pays if one call tests many candidates. Counted after the
            # no-existing-parts early return, so it reflects only calls that
            # actually do collision work.
            batch = int(idx.size)
            probe['mask_calls'] += 1
            probe['mask_batch_candidates'] += batch
            probe['mask_batch_max'] = max(
                probe.get('mask_batch_max', 0), batch)
        c_minx = points[idx, 0] + rminx
        c_miny = points[idx, 1] + rminy
        c_maxx = points[idx, 0] + rmaxx
        c_maxy = points[idx, 1] + rmaxy

        # The difference check can only ever reject candidates whose bbox
        # actually crosses the sheet boundary within the tolerance window.
        needs_sheet = (
            (c_minx < 0.0)
            | (c_miny < 0.0)
            | (c_maxx > sheet.width)
            | (c_maxy > sheet.height)
        )

        existing_bounds = np.asarray(
            [polygon.bounds for polygon in existing_polygons], dtype=np.float64
        )
        e_minx = existing_bounds[:, 0]
        e_miny = existing_bounds[:, 1]
        e_maxx = existing_bounds[:, 2]
        e_maxy = existing_bounds[:, 3]

        # Vectorially screen every candidate against every existing bbox. Only
        # candidates with at least one bbox overlap (or a boundary-crossing
        # bbox that needs the sheet check) require a translated geometry.
        overlap_any = np.zeros(len(idx), dtype=bool)
        overlap_chunk = 4096
        for start in range(0, len(idx), overlap_chunk):
            sl = slice(start, start + overlap_chunk)
            ov = (
                (c_maxx[sl, None] > e_minx[None, :])
                & (c_minx[sl, None] < e_maxx[None, :])
                & (c_maxy[sl, None] > e_miny[None, :])
                & (c_miny[sl, None] < e_maxy[None, :])
            )
            overlap_any[sl] = ov.any(axis=1)
            if probe is not None:
                probe['bbox_checks'] += int(ov.size)
                probe['bbox_overlap_pairs'] += int(ov.sum())

        needs_geometry = needs_sheet | overlap_any
        exact_rows = np.flatnonzero(needs_geometry)
        if probe is not None:
            # Keep this measurement-only detail out of the public probe
            # aggregation. The caller consumes it after validation so the
            # key counters describe only rows that really build geometry.
            probe['_geometry_candidate_indices'] = idx[exact_rows]

        geometry_ms = 0.0
        cache_ms = 0.0
        sheet_difference_ms = 0.0
        collision_intersection_ms = 0.0
        # Sub-costs of the collision stage, because "18 seconds in collision" is
        # not actionable and `collision_intersection_ms` does not decompose on
        # its own. Three separable pieces:
        #   intersects_ms  the GEOS boolean predicate, run on every pair
        #   overlay_ms     candidate.intersection(existing).area, run only when
        #                  intersects says True
        #   hole_probe_ms  the interior-ring measurement probe
        #
        # The last one is why the split matters. `_holes_touched` is documented
        # as measurement-only and is correctly gated on `probe is not None`, so
        # it never runs in production -- but it sits *inside* the
        # collision_intersection_ms window, so a logged run overstates the real
        # cost by exactly its own overhead. Splitting it out is what makes
        # `collision_intersection_ms` usable as a production figure.
        intersects_ms = 0.0
        overlay_ms = 0.0
        hole_probe_ms = 0.0
        intersects_calls = 0
        overlay_calls = 0
        hole_probe_calls = 0
        overlay_area_zero = 0
        overlay_area_sub_tol = 0
        overlay_area_over_tol = 0
        overlay_area_total = 0.0
        sheet_rejections = 0
        collision_rejections = 0
        bbox_rejections = 0
        polygon_checks = 0
        cache_hits = 0
        cache_misses = 0

        for row in exact_rows:
            index = idx[row]
            candidate = None
            # Set when a pair is seen whose outcome depends on an existing
            # part's interior ring rather than its solid body.
            row_hole_touched = False
            if candidate_geometry_cache is not None and geometry_key_prefix is not None:
                cache_key = _candidate_geometry_key(
                    geometry_key_prefix, points[index, 0], points[index, 1]
                )
                if performance_logging:
                    cache_start = time.perf_counter()
                candidate = candidate_geometry_cache.get(cache_key)
                if performance_logging:
                    cache_ms += (time.perf_counter() - cache_start) * 1000
                if candidate is not None:
                    cache_hits += 1
                else:
                    cache_misses += 1

            if candidate is None:
                geometry_start = time.perf_counter()
                candidate = translate(
                    rotated_poly,
                    xoff=float(points[index, 0] - rotated_centroid.x),
                    yoff=float(points[index, 1] - rotated_centroid.y),
                )
                geometry_ms += (time.perf_counter() - geometry_start) * 1000
                if candidate_geometry_cache is not None and geometry_key_prefix is not None:
                    if performance_logging:
                        cache_start = time.perf_counter()
                    candidate_geometry_cache.put(cache_key, candidate)
                    if performance_logging:
                        cache_ms += (time.perf_counter() - cache_start) * 1000
                if probe is not None:
                    probe['candidate_geometries_built'] += 1

            if needs_sheet[row]:
                difference_start = time.perf_counter()
                if candidate.difference(bin_polygon).area > area_tolerance:
                    valid[index] = False
                    sheet_rejections += 1
                    sheet_difference_ms += (
                        time.perf_counter() - difference_start) * 1000
                    continue
                sheet_difference_ms += (
                    time.perf_counter() - difference_start) * 1000

            # Collision stage: overlapping existing parts in ascending original
            # order, short-circuiting on the first overlap, exactly as before.
            # bbox_rejections counts the non-overlap pairs encountered before
            # any collision break (one pair per existing part otherwise).
            prev_overlap = -1
            collision = False
            if overlap_any[row]:
                overlapping = np.flatnonzero(
                    (c_maxx[row] > e_minx)
                    & (c_minx[row] < e_maxx)
                    & (c_maxy[row] > e_miny)
                    & (c_miny[row] < e_maxy)
                )
                for e_pos in overlapping:
                    bbox_rejections += int(e_pos) - prev_overlap - 1
                    prev_overlap = int(e_pos)
                    checks_start = time.perf_counter()
                    # Boolean prefilter. `intersects` is True whenever the two
                    # interiors overlap, so any pair whose intersection area
                    # exceeds the tolerance always reaches the area test. A
                    # False result means the geometries meet at most along a
                    # boundary, where the intersection has zero area and the
                    # area comparison would also have been False -- so the
                    # accept/reject decision is unchanged.
                    #
                    # Measured on the 122-part fixture (551,931 exact checks):
                    # 67.7% return False and skip the overlay. End-to-end this
                    # is worth 9.3% of collision cost (282.3 -> 256.0 us per
                    # exact check) -- much less than the skip rate suggests,
                    # because `intersects` is itself not free on parts this
                    # complex. An earlier 96.8% figure was a measurement
                    # error: it conflated "disjoint" with "touching", since
                    # area <= tol holds for both. See make-faster.md.
                    existing = existing_polygons[e_pos]
                    t_intersects = time.perf_counter()
                    hit = existing.intersects(candidate)
                    intersects_ms += (time.perf_counter() - t_intersects) * 1000
                    intersects_calls += 1
                    if hit:
                        t_overlay = time.perf_counter()
                        overlap_area = candidate.intersection(
                            existing).area
                        overlaps = overlap_area > area_tolerance
                        overlay_ms += (time.perf_counter() - t_overlay) * 1000
                        overlay_calls += 1
                        # How the overlays are decided, three ways. The overlay
                        # is 65% of the collision stage and `overlaps or
                        # contains` measures 6-10x cheaper for the same verdict
                        # (probe_collision_overlay.py), so the obvious
                        # optimisation is to drop it. Whether that is *exactly*
                        # equivalent turns on this split:
                        #
                        #   zero    an exact boundary touch. The predicate
                        #           rejects it too, so the two agree.
                        #   sub-tol positive but at or under the tolerance.
                        #           The area test ACCEPTS these; a predicate
                        #           calls them overlaps and REJECTS. The two
                        #           disagree, and the area test is right.
                        #   over    a real overlap, rejected by both.
                        #
                        # The middle group is the whole question. An earlier
                        # version of this counter was named `..._below_tol` and
                        # only tested `<= 0.0`, so it lumped sub-tolerance and
                        # real overlaps together and the three-way split was not
                        # visible. It read as 95.8% sub-tolerance when the true
                        # figure is 89.3% -- the difference is exactly the real
                        # rejections.
                        if overlap_area <= 0.0:
                            overlay_area_zero += 1
                        elif overlap_area <= area_tolerance:
                            overlay_area_sub_tol += 1
                        else:
                            overlay_area_over_tol += 1
                        overlay_area_total += overlap_area
                        if probe is not None:
                            probe['collision_intersects_true'] += 1
                            if not overlaps:
                                # Within tolerance. Note this is not
                                # necessarily an exact touch -- see the
                                # collision_grazing_pairs comment.
                                probe['collision_grazing_pairs'] += 1
                            # Measurement-only: the pair's verdict depends on
                            # this part's interior rings rather than its solid
                            # body, so filling holes could flip it.
                            t_hole = time.perf_counter()
                            if (hole_geoms is not None
                                    and _holes_touched(
                                        candidate, hole_geoms[e_pos],
                                        hole_bounds[e_pos],
                                        c_minx[row], c_miny[row],
                                        c_maxx[row], c_maxy[row])):
                                probe['mask_hole_sensitive_pairs'] += 1
                                row_hole_touched = True
                            hole_probe_ms += (time.perf_counter() - t_hole) * 1000
                            hole_probe_calls += 1
                    else:
                        overlaps = False
                        if probe is not None:
                            probe['collision_intersects_false'] += 1
                            # A candidate lying wholly inside a hole also lands
                            # here, and it is the most hole-dependent placement
                            # of all -- so the touch test must run on this
                            # branch too, not only on the intersects-True one.
                            t_hole = time.perf_counter()
                            if (hole_geoms is not None
                                    and _holes_touched(
                                        candidate, hole_geoms[e_pos],
                                        hole_bounds[e_pos],
                                        c_minx[row], c_miny[row],
                                        c_maxx[row], c_maxy[row])):
                                row_hole_touched = True
                            hole_probe_ms += (time.perf_counter() - t_hole) * 1000
                            hole_probe_calls += 1
                    collision_intersection_ms += (
                        time.perf_counter() - checks_start) * 1000
                    polygon_checks += 1
                    if probe is not None:
                        probe['exact_collision_checks'] += 1
                    if overlaps:
                        collision = True
                        break
                if not collision:
                    bbox_rejections += len(existing_polygons) - prev_overlap - 1
            else:
                bbox_rejections += len(existing_polygons)
            if collision:
                valid[index] = False
                collision_rejections += 1
            elif row_hole_touched and probe is not None:
                # Measurement-only, and the cost side of any hole-filling
                # proposal. This placement is legal only because it occupies
                # empty space: a hole in an existing part, or the free space
                # beside one. Filling holes would forbid the hole case
                # outright, so this counter is the sheet yield that filling
                # would destroy.
                probe['mask_hole_exploiting_placements'] += 1

        if probe is not None:
            probe['candidate_geometry_ms'] = probe.get(
                'candidate_geometry_ms', 0.0) + geometry_ms
            probe['candidate_geometry_cache_ms'] = probe.get(
                'candidate_geometry_cache_ms', 0.0) + cache_ms
            probe['candidate_geometry_cache_hits'] = cache_hits
            probe['candidate_geometry_cache_misses'] = cache_misses
            probe['candidate_geometry_cache_entries'] = (
                len(candidate_geometry_cache)
                if candidate_geometry_cache is not None else 0
            )
            probe['sheet_difference_ms'] = probe.get(
                'sheet_difference_ms', 0.0) + sheet_difference_ms
            probe['collision_intersection_ms'] = probe.get(
                'collision_intersection_ms', 0.0) + collision_intersection_ms
            # The collision stage's own decomposition. Reported alongside the
            # total so the total can be checked against the parts, and so the
            # measurement-only probe's cost is visible rather than inflating
            # the number it is supposed to be explaining.
            for key, value in (
                    ('collision_intersects_ms', intersects_ms),
                    ('collision_overlay_ms', overlay_ms),
                    ('collision_hole_probe_ms', hole_probe_ms)):
                probe[key] = probe.get(key, 0.0) + value
            for key, value in (
                    ('collision_intersects_calls', intersects_calls),
                    ('collision_overlay_calls', overlay_calls),
                    ('collision_hole_probe_calls', hole_probe_calls)):
                probe[key] = probe.get(key, 0) + value
            probe['collision_overlay_area_zero'] = probe.get(
                'collision_overlay_area_zero', 0) + overlay_area_zero
            probe['collision_overlay_area_sub_tol'] = probe.get(
                'collision_overlay_area_sub_tol', 0) + overlay_area_sub_tol
            probe['collision_overlay_area_over_tol'] = probe.get(
                'collision_overlay_area_over_tol', 0) + overlay_area_over_tol
            probe['collision_overlay_area_total'] = probe.get(
                'collision_overlay_area_total', 0.0) + overlay_area_total
            probe['sheet_rejections'] = probe.get('sheet_rejections', 0) + sheet_rejections
            probe['collision_rejections'] = probe.get('collision_rejections', 0) + collision_rejections
            # Skipped candidates (no overlap, inside sheet) were screened
            # vectorially against every existing bbox — one bbox rejection each.
            probe['bbox_rejections'] = probe.get('bbox_rejections', 0) + (
                (len(idx) - len(exact_rows)) * len(existing_polygons)
                + bbox_rejections
            )
            probe['polygon_checks'] = probe.get('polygon_checks', 0) + polygon_checks
            # Every bounds survivor that passes the sheet check reaches the
            # collision stage (either via exact intersection or bbox screening).
            probe['collision_candidates'] = probe.get(
                'collision_candidates', 0) + len(idx) - sheet_rejections
            probe['sheet_boundary_candidates'] = probe.get(
                'sheet_boundary_candidates', 0) + int(needs_sheet.sum())

        return valid

class Nester:
    """
    The main nesting algorithm class. 
    It orchestrates the nesting process using PlacementOptimizer and MinkowskiEngine.
    """
    def __init__(self, width, height, rotation_steps=1, **kwargs):
        self.bin_width = width
        self.bin_height = height
        self.spacing = kwargs.get("spacing", 0)
        self.search_direction = kwargs.get("search_direction", (0, -1)) # Default Down
        
        # Logging control
        self.quiet = kwargs.get("quiet", False)  # If True, suppress per-part logs
        self.verbose = kwargs.get("verbose", False)  # If True, enable extra detailed logs
        self.performance_logging = kwargs.get("performance_logging", False)
        self.log_callback = kwargs.get("log_callback")
        self.trial_callback = kwargs.get("trial_callback")  # For visualizing trial placements
        self.part_start_callback = kwargs.get("part_start_callback")  # Called when starting to place a part
        self.part_end_callback = kwargs.get("part_end_callback")  # Called after part is placed
        self.progress_callback = kwargs.get("progress_callback") # Called with (current, total)
        self.cancel_callback = kwargs.get("cancel_callback") # Called to check if nesting should abort
        self.spawn_more_callback = kwargs.get("spawn_more_callback")  # Mints fill-part instances on the main thread
        
        # NFP ring discretisation interval: samples one candidate position
        # every `step_size` mm along each No-Fit Polygon boundary, so it sets the
        # candidate count. On the n70 GA run that count (639,410) drives both the
        # collision stage and candidate generation, together 63% of the run, so
        # this is the largest remaining time/density dial.
        #
        # It was reachable only from the Physics panel, so the Minkowski nester
        # -- every layout in a GA run -- always got the hardcoded 5.0. The env
        # override makes it measurable without a UI change; the UI question is
        # separate and is answered in RESULTS-parallelism.md.
        step_size = _step_size(kwargs)
        self.engine = MinkowskiEngine(
            width, height, step_size, log_callback=self.log_callback,
            verbose=self.verbose,
            performance_logging=self.performance_logging,
            search_direction=self.search_direction, rng=kwargs.get("rng"))
        # quiet (multi-layout GA) silences the optimizer's per-placement [TIMING] lines
        self.optimizer = PlacementOptimizer(
            self.engine, rotation_steps, self.search_direction,
            None if self.quiet else self.log_callback,
            self.trial_callback, rng=kwargs.get("rng"),
            performance_logging=self.performance_logging,
            candidate_geometry_key_tracker=kwargs.get("candidate_geometry_key_tracker"),
            candidate_geometry_cache=kwargs.get("candidate_geometry_cache")
        )
        self.optimizer.verbose = self.verbose

        self.parts_to_place = []
        self.sheets = []
        self.update_callback = None # Can be set externally

    def get_perf_stats(self):
        """Return aggregated candidate-evaluation and NFP timings."""
        with self.optimizer._perf_lock:
            stats = dict(self.optimizer._perf_stats)
        stats.update({
            f'nfp_{key}': value
            for key, value in self.engine.get_perf_stats().items()
        })
        return stats


    def log(self, message, level="message"):
        if self.log_callback:
            self.log_callback(message)
        else:
            if level == "warning":
                FreeCAD.Console.PrintWarning(f"NESTER: {message}\n")
            else:
                FreeCAD.Console.PrintMessage(f"NESTER: {message}\n")

    def nest(self, parts, sort=True):
        """
        Main entry point for nesting.

        NOTE: GA optimization is now handled at the controller level using LayoutManager.
        This method just runs standard greedy nesting.
        """
        # Cleanup debug objects — only safe from the main thread
        try:
            from PySide.QtCore import QThread, QCoreApplication
            app = QCoreApplication.instance()
            if app and QThread.currentThread() == app.thread():
                doc = FreeCAD.ActiveDocument
                if doc and doc.getObject("MinkowskiDebug"):
                    doc.removeObject("MinkowskiDebug")
                    doc.recompute()
        except Exception:
            pass  # Cleanup of debug objects; swallow exceptions if GUI or document is unavailable

        return self._nest_standard(parts, sort=sort)

    def _nest_standard(self, parts, sort=True, quiet=None):
        """
        Standard greedy nesting strategy.
        
        Args:
            parts: List of parts to nest
            sort: Whether to sort by area (largest first)
            quiet: If True, suppresses logging and progress callbacks. Defaults to self.quiet.
                   Simulation callbacks (part_start/update/part_end) are not gated —
                   they only exist when the user asked to watch the run.
        """
        # Use instance quiet setting if not explicitly passed
        if quiet is None:
            quiet = self.quiet
        all_parts = list(parts)
        current_parts = [p for p in all_parts if getattr(p, 'fill_sheet', False) is not True]
        fill_parts = [p for p in all_parts if getattr(p, 'fill_sheet', False) is True]
        if sort:
            current_parts.sort(key=lambda p: p.area, reverse=True)
            fill_parts.sort(key=lambda p: p.area, reverse=True)

        sheets = []
        unplaced_parts = []
        total_parts = len(current_parts)
        _part_timings = []  # (part_id, elapsed_s, placed)
        self.engine.reset_perf_stats()

        for i, part in enumerate(current_parts):
            if self.cancel_callback and self.cancel_callback():
                self.log("Nesting cancelled by user.")
                break

            if self.verbose and not quiet:
                self.log(f"Processing part {i+1}/{total_parts}: {part.id}")
            
            if not quiet and self.progress_callback:
                self.progress_callback(i + 1, total_parts, f"Placing {part.id}...")
            
            import time as _time
            _t0_part = _time.perf_counter()
            start_part_time = datetime.now()
            placed = False

            # Notify start of part placement (for highlighting master shapes)
            if self.part_start_callback:
                self.part_start_callback(part)

            for sheet_idx, sheet in enumerate(sheets):
                if (sheet.width * sheet.height - sheet.used_area) < part.area: continue

                if self._attempt_placement_on_sheet(part, sheet):
                    placed = True
                    if self.verbose and not quiet:
                        elapsed = (datetime.now() - start_part_time).total_seconds()
                        self.log(f"  -> Placed on Sheet {sheet_idx+1} ({elapsed:.4f}s)")

                    if self.update_callback:
                        self.update_callback(part, sheet)
                    break

            if not placed:
                new_sheet = Sheet(len(sheets), self.bin_width, self.bin_height, spacing=self.spacing)
                if self._attempt_placement_on_sheet(part, new_sheet):
                    sheets.append(new_sheet)
                    placed = True
                    if self.verbose and not quiet:
                        elapsed = (datetime.now() - start_part_time).total_seconds()
                        self.log(f"  -> Placed on New Sheet {len(sheets)} ({elapsed:.4f}s)")

                    if self.update_callback:
                        self.update_callback(part, new_sheet)
                else:
                    unplaced_parts.append(part)
                    if not quiet:
                        self.log(f"  -> FAILED to place in {(datetime.now() - start_part_time).total_seconds():.4f}s")

            _part_timings.append((part.id, _time.perf_counter() - _t0_part, placed))

            # Notify end of part placement (for unhighlighting master shapes)
            if self.part_end_callback:
                self.part_end_callback(part, placed)


        was_cancelled = self.cancel_callback and self.cancel_callback()
        if fill_parts and not was_cancelled:
            self._nest_fill_parts(sheets, fill_parts, unplaced_parts, quiet, _part_timings)


        if self.performance_logging and not quiet and _part_timings:
            self._log_timing_summary(_part_timings)

        return sheets, unplaced_parts

    def _nest_fill_parts(self, sheets, fill_parts, unplaced_parts, quiet, part_timings=None):
        """Round-robin fill phase: cycle through every fill-enabled part type,
        placing one instance per turn, until no type fits anywhere.

        - Only fills EXISTING sheets; never creates a new sheet (except when
          the run consists solely of fill parts and no sheet exists yet).
        - A type is retired permanently the first time a placement fails —
          failure is the signal that no remaining gap fits that type.
        - A hard per-type cap (free area / part area) guarantees termination
          even if placement erroneously keeps succeeding.
        """
        from collections import deque

        if not sheets:
            sheets.append(Sheet(0, self.bin_width, self.bin_height, spacing=self.spacing))

        # Group by explicit master_label — NEVER parse part.id (display string,
        # no format guarantee; parsing it once caused exponential spawn growth).
        queues, spawners, order = {}, {}, []
        for part in fill_parts:
            part_type = getattr(part, 'master_label', None) or part.id
            if part_type not in queues:
                queues[part_type] = deque()
                spawners[part_type] = getattr(part, 'spawn_next', None)
                order.append(part_type)
            queues[part_type].append(part)

        total_area = sum(s.width * s.height for s in sheets)
        caps = {t: int(total_area / max(queues[t][0].area, 1e-9)) + 2 for t in order}
        attempts = {t: 0 for t in order}

        active = deque(order)
        while active:
            if self.cancel_callback and self.cancel_callback():
                self.log("Nesting cancelled by user.")
                break

            part_type = active.popleft()
            queue = queues[part_type]

            if not queue:
                spawn_fn = spawners[part_type]
                if spawn_fn is None or attempts[part_type] >= caps[part_type]:
                    continue  # type exhausted — do not re-queue
                try:
                    new_part = (self.spawn_more_callback(spawn_fn)
                                if self.spawn_more_callback else spawn_fn())
                except Exception as e:
                    self.log(f"Could not spawn fill part '{part_type}': {e}", level="warning")
                    continue
                if new_part is None:
                    continue
                queue.append(new_part)

            part = queue.popleft()
            attempts[part_type] += 1

            if self.part_start_callback:
                self.part_start_callback(part)

            import time as _time
            _t0_part = _time.perf_counter()
            placed = False
            for sheet in sheets:
                if (sheet.width * sheet.height - sheet.used_area) < part.area:
                    continue
                if self._attempt_placement_on_sheet(part, sheet):
                    placed = True
                    if self.update_callback:
                        self.update_callback(part, sheet)
                    break

            _dt_part = _time.perf_counter() - _t0_part
            if part_timings is not None:
                part_timings.append((part.id, _dt_part, placed))
            if self.performance_logging and not quiet:
                self.log(f"[TIMING] fill '{part.id}' ({part_type}): "
                         f"{_dt_part * 1000:.0f}ms {'placed' if placed else 'FAILED'} "
                         f"(attempt {attempts[part_type]})")

            if self.part_end_callback:
                self.part_end_callback(part, placed)

            if placed:
                active.append(part_type)  # round-robin: give the next type a turn
            else:
                unplaced_parts.append(part)
                if self.performance_logging and not quiet:
                    self.log(f"Fill type '{part_type}' retired after {attempts[part_type]} attempts.")

    def _log_timing_summary(self, part_timings):
        total_s = sum(t for _, t, _ in part_timings)
        cache = self.engine.get_perf_stats()
        total_lookups = cache['cache_hits'] + cache['cache_misses']
        hit_pct = cache['cache_hits'] / total_lookups * 100 if total_lookups else 0
        self.log(
            f"[TIMING] {len(part_timings)} parts in {total_s:.2f}s | "
            f"NFP cache: {cache['cache_hits']} hits ({hit_pct:.0f}%) / "
            f"{cache['cache_misses']} misses, compute={cache['nfp_compute_ms']:.0f}ms"
        )
        with Shape.convex_pair_probe_lock:
            pair_requests = Shape.convex_pair_probe_requests
            pair_unique = len(Shape.convex_pair_probe_keys)
        pair_repeats = pair_requests - pair_unique
        self.log(
            f"[PERF] Convex pair probe: {pair_requests} requests / "
            f"{pair_unique} unique / {pair_repeats} repeats"
        )
        slowest = sorted(part_timings, key=lambda x: -x[1])[:5]
        self.log("[TIMING] Slowest: " + ", ".join(
            f"{pid}={t:.2f}s{'(unplaced)' if not ok else ''}" for pid, t, ok in slowest
        ))

    def _attempt_placement_on_sheet(self, part, sheet):
        """Delegates to PlacementOptimizer."""
        placed_part = self.optimizer.find_best_placement(part, sheet)
        
        if placed_part:
            # We trust the PlacementOptimizer (and NFP engine) to have found a valid spot.
            placed_part.placement = placed_part.get_final_placement(sheet.get_origin())
            new_placed_part = PlacedPart(placed_part)
            sheet.add_part(new_placed_part)
            return True
        return False
