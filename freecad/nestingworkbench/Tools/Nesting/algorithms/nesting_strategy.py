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


# NEST-034 fix -- verify-on-accept (default on). When a candidate passes the
# SIMPLIFIED mask collision check, re-check it against the un-simplified
# buffered profile (Shape.verify_polygon) before accepting it, so spacing is
# measured on the geometry that is actually drawn. The mask's stacked DP
# simplification can consume the whole spacing budget while reporting a
# clean sheet; this gate closes that hole. Set
# NESTING_VERIFY_ON_ACCEPT=0 to A/B against the old behaviour.
VERIFY_ON_ACCEPT = os.environ.get(
    "NESTING_VERIFY_ON_ACCEPT", "1").strip().lower() not in ("0", "false", "off", "")

# NEST-036 repair-push. When the verify gate rejects a mask-accepted
# candidate, the mask's simplification optimism -- not a real dead end -- is
# usually why: the NFP proposes positions where the SIMPLIFIED polygons
# graze, and at simplification 0.3 that grazing carries 0.05-1.0 mm of true
# penetration, so a reject-only gate dropped ~99% of candidates and starved
# the search (8/54 parts placed). Repair pushes such a candidate out of
# penetration by the measured depth and re-tests BOTH predicates (mask and
# verify, bounds included) at the new coordinate; only when the bounded loop
# cannot find a legal nearby position does the row fall back to rejection.
# The gate itself is untouched: a repaired row is accepted only after the
# same verify predicate passed. Set NESTING_VERIFY_REPAIR=0 for the
# reject-only behaviour (bit-exact to the pre-repair gate).
VERIFY_REPAIR = os.environ.get(
    "NESTING_VERIFY_REPAIR", "1").strip().lower() not in ("0", "false", "off", "")

# Bounded so a boxed-in candidate (pushes that cancel or oscillate) costs a
# few exact checks and then rejects, instead of searching forever. The
# measured penetration at sim 0.3 is 0.05-1.0 mm; the first step covers
# depth*1.25 + 0.01, so one or two iterations converge in the common case.
_REPAIR_MAX_ATTEMPTS = 6

# How far past the sheet edge a bounds repair lands when pulling an
# overhanging candidate on-sheet: the overshoot the collision pushes use as
# their floor, so both repair legs step by the same quantum.
_REPAIR_EDGE_MARGIN = 0.01


def _candidate_geometry_key(prefix, x, y):
    """Build the complete run-local candidate geometry cache key."""
    return (prefix, float(x), float(y))


def _step_size(kwargs):
    """Resolve the NFP discretisation interval: env, then kwargs, then 5.0 mm.

    The UI field wins when it has been set, so a user who moves it sees the
    effect; the env is the scripted and benchmark entry point and applies when
    the caller did not choose. Unset everywhere is the 5.0 mm default.

    `NESTING_STEP_SIZE` exists so the time/density curve can be characterised
    without building a UI, the same reason `NESTING_ROTATION_WORKERS` exists.
    Both are now also panel fields, because a control nobody can reach is not a
    control, and these two are the largest levers measured in a run.
    """
    explicit = kwargs.get("step_size") if kwargs else None
    if explicit is not None:
        try:
            value = float(explicit)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    raw = os.environ.get('NESTING_STEP_SIZE', '').strip()
    if raw:
        try:
            value = float(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return 5.0


def _rotation_workers_explicit(kwargs=None):
    """Whether the pool width was chosen rather than inferred from the core count.

    Separate from `_rotation_worker_limit` because that one always resolves, and
    once resolved a chosen 4 and an inferred 4 are the same integer. The
    distinction is what a report needs: a number in the output that might be a
    decision or might be a default cannot attribute a result.
    """
    explicit = kwargs.get('rotation_workers') if kwargs else None
    try:
        if explicit is not None and int(explicit) > 0:
            return True
    except (TypeError, ValueError):
        pass
    raw = os.environ.get('NESTING_ROTATION_WORKERS', '').strip()
    if raw:
        try:
            return int(raw) > 0
        except ValueError:
            pass
    return False


def _rotation_worker_limit(kwargs=None):
    """Resolve the rotation execution width. Returns 0 for "no pool at all".

    One core per thread is the default. This used to be `None`, meaning "let
    ThreadPoolExecutor decide", and the stdlib decides `min(32, cpu_count() + 4)`
    -- twice the core count, which was measured to be no faster than a single
    worker while a pool sized to the core count was 22% faster. The default is
    now the measured one rather than the inherited one.

    Auto is `os.cpu_count()`, not `cpu_count() + 4`. The oversubscribed default
    assumes the work is embarrassingly parallel and releases the GIL. Shapely
    does release the GIL around GEOS calls, but the surrounding Python does not,
    so the extra threads contend for the interpreter and the shared NFP cache
    instead of adding throughput.

    Precedence: an explicit positive value from the caller (the "Rotation
    Threads" field), then `NESTING_ROTATION_WORKERS` for scripted and benchmark
    runs, then the core count. An unset field and 0 both mean auto at the UI
    layer; 0 arriving through the environment is honoured, because it is how
    the benchmark harness asks for true serial.

    `NESTING_ROTATION_WORKERS` deliberately survives the UI field: the benchmark
    harness pins the width for reproducible work counters and never builds an
    `algo_kwargs`, so the env is its only channel.
    """
    explicit = kwargs.get('rotation_workers') if kwargs else None
    try:
        if explicit is not None and int(explicit) > 0:
            return int(explicit)
    except (TypeError, ValueError):
        pass
    raw = os.environ.get('NESTING_ROTATION_WORKERS', '').strip()
    if raw:
        try:
            value = int(raw)
            # 0 is meaningful and explicit: "no pool at all". It is not the
            # same as 1, which still builds and tears down a ThreadPoolExecutor
            # with a single worker on every placement. Distinguishing the two is
            # what makes the serial-versus-pool comparison measurable at all.
            if value >= 0:
                return value
        except ValueError:
            pass
    # Positive by default: max_workers must be >= 1, so a fallback of 0 would
    # be a silently different execution model. cpu_count() can return None on
    # platforms that cannot answer, hence the fallback.
    return max(1, os.cpu_count() or 1)


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


def _push_vector(cand, inter, partner):
    """One partner's repair step: away from the overlap sliver, by its depth.

    Direction is from the penetration's own centroid toward the candidate's
    body -- for a shallow sliver that is the contact normal, and unlike a
    partner-centroid direction it is also correct when the candidate is
    inside a hole of the partner (the sliver sits at the hole wall, so the
    vector points into the hole, away from the material). Depth is
    sliver-area over sliver-perimeter times two: a l x d rectangle has
    area l*d and perimeter ~2l, so 2A/P ~ d. The 1.25 factor and the 0.01 mm
    floor absorb the estimate's error; an undershoot is corrected by the
    next iteration's fresh intersection rather than by a bigger margin,
    because overshoot costs density while undershoot only costs one more
    pass. None when no direction exists (concentric shapes) -- the caller
    rejects the row, which is the pre-repair behaviour.
    """
    ic = inter.centroid
    cc = cand.centroid
    dx = cc.x - ic.x
    dy = cc.y - ic.y
    norm = math.hypot(dx, dy)
    if norm < 1e-9:
        pc = partner.centroid
        dx, dy = cc.x - pc.x, cc.y - pc.y
        norm = math.hypot(dx, dy)
        if norm < 1e-9:
            return None
    depth = 0.0
    if inter.length > 1e-12:
        depth = 2.0 * inter.area / inter.length
    step = depth * 1.25 + 0.01
    return (dx / norm * step, dy / norm * step)


def _repair_candidate(x, y, rotated_verify, rotated_poly, rotated_centroid,
                      sheet, existing_verify, existing_polygons,
                      area_tolerance, bin_polygon,
                      max_attempts=_REPAIR_MAX_ATTEMPTS):
    """NEST-036 repair-push: move one rejected candidate to a legal position.

    Called for a row the verify stage just failed -- by the bounds leg
    (verify bbox overhanging the sheet, the empty-sheet corner-flush case)
    or by the exact collision check. Returns ``((x, y), attempts)`` when
    BOTH predicates -- the mask's and the verify's, bounds and
    sheet-containment legs included -- hold at the final coordinate, or
    ``(None, attempts)`` when the bounded loop gives up. The coordinates
    are plain floats: the caller never lets this touch the engine's shared
    candidate array, it only reports the new position back.

    Bounds are an interval intersection, not a push: the position window
    in which BOTH geometries sit strictly on the sheet is derived from
    their bboxes (which differ), and the candidate is clamped into it --
    when the window is empty the part cannot sit on this sheet at this
    angle at all, and the caller has other rotations to try. Collisions
    are a push: away from the penetration sliver by its measured depth.
    Both are re-tested every iteration because either move can break what
    the other proved -- the clamp can shove the candidate into a partner,
    a collision push can shove it off the sheet. Re-testing both is what
    lets the caller promise that a kept row's final coordinate passes
    everything downstream re-checks.
    """
    vx0, vy0, vx1, vy1 = rotated_verify.bounds
    mx0, my0, mx1, my1 = rotated_poly.bounds
    cx, cy = rotated_centroid.x, rotated_centroid.y
    ev_bounds = [polygon.bounds for polygon in existing_verify]
    mp_bounds = [polygon.bounds for polygon in existing_polygons]
    n_verify = len(existing_verify)
    n_mask = len(existing_polygons)
    attempts = 0
    # The position window in which BOTH geometries sit strictly on the
    # sheet -- their bboxes differ, so one coordinate has to satisfy both.
    # Strict [0, w] x [0, h] rather than the stage's +-area_tolerance
    # window: a bbox strictly inside the sheet makes the sheet-difference
    # leg structurally true, and everything the stage accepts lies inside
    # this window too. Invariant across iterations (only x/y move), so an
    # empty window means no position exists for this part at this angle.
    win_x0 = max(cx - vx0, cx - mx0)
    win_x1 = min(sheet.width + cx - vx1, sheet.width + cx - mx1)
    win_y0 = max(cy - vy0, cy - my0)
    win_y1 = min(sheet.height + cy - vy1, sheet.height + cy - my1)
    if win_x0 > win_x1 or win_y0 > win_y1:
        return None, attempts

    while attempts < max_attempts:
        # Bounds legs (arithmetic only): clamp into the window -- the
        # repair for an overhanging candidate is to pull it on-sheet, and
        # both geometries move by the same coordinate.
        bx, by = x, y
        if x < win_x0:
            x = min(win_x0 + _REPAIR_EDGE_MARGIN, win_x1)
        elif x > win_x1:
            x = max(win_x1 - _REPAIR_EDGE_MARGIN, win_x0)
        if y < win_y0:
            y = min(win_y0 + _REPAIR_EDGE_MARGIN, win_y1)
        elif y > win_y1:
            y = max(win_y1 - _REPAIR_EDGE_MARGIN, win_y0)
        if x != bx or y != by:
            # Pulled on-sheet: the collision legs must be re-proven at the
            # new coordinate before this position can be reported clean.
            attempts += 1
            continue
        mbx0, mby0 = x + mx0 - cx, y + my0 - cy
        mbx1, mby1 = x + mx1 - cx, y + my1 - cy
        # Rebuilt every iteration: after a push, a lazily-built candidate
        # mask would test the PREVIOUS position against the NEW bbox and
        # the sheet rect, silently passing rows that still penetrate.
        cand_mask = translate(rotated_poly, xoff=x - cx, yoff=y - cy)
        # Unreachable while the clamp above holds (both bboxes are then
        # strictly inside the sheet); kept as the difference-parity check
        # it is, in case the window is ever loosened. Mirrors the mask
        # stage's needs_sheet branch.
        if (mbx0 < 0.0 or mby0 < 0.0
                or mbx1 > sheet.width or mby1 > sheet.height):
            if bin_polygon is None:
                return None, attempts
            if cand_mask.difference(bin_polygon).area > area_tolerance:
                return None, attempts

        vbx0, vby0 = x + vx0 - cx, y + vy0 - cy
        vbx1, vby1 = x + vx1 - cx, y + vy1 - cy
        cand_verify = translate(rotated_verify, xoff=x - cx, yoff=y - cy)
        push_x = 0.0
        push_y = 0.0
        violators = 0
        for k in range(n_verify):
            push = None
            violation = False
            eb = ev_bounds[k]
            if (vbx1 > eb[0] and vbx0 < eb[2]
                    and vby1 > eb[1] and vby0 < eb[3]):
                existing = existing_verify[k]
                if existing.intersects(cand_verify):
                    inter = cand_verify.intersection(existing)
                    if inter.area > area_tolerance:
                        violation = True
                        push = _push_vector(cand_verify, inter, existing)
            # The mask leg runs only when the verify leg found nothing for
            # this partner: one push per partner, from the geometry that
            # actually violates (verify is the bigger polygon, so its depth
            # dominates when both do -- and the next iteration re-checks
            # whichever one still bites).
            if not violation and k < n_mask:
                mb = mp_bounds[k]
                if (mbx1 > mb[0] and mbx0 < mb[2]
                        and mby1 > mb[1] and mby0 < mb[3]):
                    existing_m = existing_polygons[k]
                    if existing_m.intersects(cand_mask):
                        inter_m = cand_mask.intersection(existing_m)
                        if inter_m.area > area_tolerance:
                            violation = True
                            push = _push_vector(cand_mask, inter_m, existing_m)
            if not violation:
                continue
            if push is None:
                # A real penetration with no usable direction (concentric
                # shapes): unrepairable, reject exactly as the gate did.
                return None, attempts
            push_x += push[0]
            push_y += push[1]
            violators += 1

        if violators == 0:
            return (x, y), attempts
        x += push_x
        y += push_y
        attempts += 1
    return None, attempts


def _verify_stage(valid, points, rotated_verify, rotated_centroid, sheet,
                  existing_verify, area_tolerance, probe,
                  rotated_poly=None, existing_polygons=None,
                  bin_polygon=None):
    """NEST-034 verify-on-accept: re-check accepted rows on un-simplified geometry.

    `valid` is the mask's decision. Every row still True here was accepted on
    SIMPLIFIED polygons whose stacked DP error can exceed the spacing budget
    (measured: two stacked DP stages compose to 4x the per-stage tolerance),
    so the mask can pass a placement whose drawn part sits closer than
    spacing. `rotated_verify` is the candidate's un-simplified buffered
    profile, already rotated by the same rigid motion as the mask polygon;
    `existing_verify` holds the placed parts' verify polygons in sheet frame.

    `points` are MASK centroids, so the candidate's verify polygon must
    receive the identical translation the mask candidate got
    (``points[i] - rotated_centroid``) -- rotating/translating it about its
    own centroid would de-register it by the centroid offset between the two
    polygons. When the two origins coincide the offset is zero and both
    translations are the same arithmetic.

    The predicate is the mask's own, unchanged: bounds within the same
    tolerance window, collision when intersection area exceeds
    `area_tolerance`. An empty `existing_verify` means an empty sheet, where
    only the bounds leg can still reject (the un-simplified polygon's bbox
    is not the mask's bbox). `valid` is updated in place; nothing returned.

    NEST-036 repair (VERIFY_REPAIR): either rejection -- bounds or
    collision -- is first offered to `_repair_candidate`, which pulls an
    overhanging row on-sheet, pushes a penetrating row out of penetration,
    and re-proves BOTH predicates at the new coordinate. Only when that
    bounded loop fails does the row reject as before, so with the repair
    off the decisions here are bit-identical to the reject-only gate.
    Bounds repair matters most on an empty sheet, where the engine's only
    candidates are corner-flush (exact in mask space; the verify bbox
    overhangs by the DP error) and an unrepaired bounds leg would reject
    every rotation of an otherwise placeable part. The repaired coordinates
    never touch `points` -- the caller may be holding the engine's shared
    candidate cache -- they leave through the probe under the private key
    `_verify_repaired`, which `_evaluate_rotation` pops before the probe's
    keys are copied anywhere else, so without a probe (or without the mask
    geometry `rotated_poly`/`existing_polygons`) the repair is skipped and
    this function rejects exactly as before -- the probe-less call also
    makes a repair pointless, since a kept row's new coordinate could not
    reach the scorer.
    """
    if rotated_verify is None:
        return
    acc = np.flatnonzero(valid)
    if acc.size == 0:
        return

    t_verify = time.perf_counter()
    candidates = int(acc.size)
    bounds_rejections = 0
    screen_pairs = 0
    exact_checks = 0
    rejections = 0
    repairs = 0
    bounds_repairs = 0
    repair_failures = 0
    repair_attempts = 0
    repair_ms = 0.0
    repaired_positions = None
    repair_enabled = (
        VERIFY_REPAIR
        and probe is not None
        and rotated_poly is not None
        and existing_polygons is not None
    )

    # Verify extents relative to the MASK centroid (translation semantics
    # above): candidate i's verify bbox is points[i] + these offsets.
    vx0, vy0, vx1, vy1 = rotated_verify.bounds
    rminx, rminy = vx0 - rotated_centroid.x, vy0 - rotated_centroid.y
    rmaxx, rmaxy = vx1 - rotated_centroid.x, vy1 - rotated_centroid.y

    px = points[acc, 0]
    py = points[acc, 1]
    in_sheet = (
        (px + rminx >= -area_tolerance)
        & (px + rmaxx <= sheet.width + area_tolerance)
        & (py + rminy >= -area_tolerance)
        & (py + rmaxy <= sheet.height + area_tolerance)
    )
    bad_bounds = np.flatnonzero(~in_sheet)
    if bad_bounds.size:
        if repair_enabled:
            # Bounds rejection first offered to the repair: pull the
            # overhanging candidate on-sheet (empty-sheet corner-flush
            # candidates fail here by construction -- flush in mask space,
            # the verify bbox grows past the wall by the DP error). The
            # repair re-proves bounds AND collisions at the new coordinate,
            # so a bounds-repaired row never reaches the collision leg
            # below with unproven partners.
            for j in bad_bounds:
                index = int(acc[j])
                t_repair = time.perf_counter()
                new_pos, attempts = _repair_candidate(
                    float(points[index, 0]), float(points[index, 1]),
                    rotated_verify, rotated_poly, rotated_centroid,
                    sheet, existing_verify, existing_polygons,
                    area_tolerance, bin_polygon,
                )
                repair_ms += (time.perf_counter() - t_repair) * 1000
                repair_attempts += attempts
                if new_pos is not None:
                    repairs += 1
                    bounds_repairs += 1
                    if (new_pos[0] != float(points[index, 0])
                            or new_pos[1] != float(points[index, 1])):
                        if repaired_positions is None:
                            repaired_positions = {}
                        repaired_positions[index] = new_pos
                else:
                    valid[index] = False
                    bounds_rejections += 1
                    repair_failures += 1
        else:
            bounds_rejections = int(bad_bounds.size)
            valid[acc[bad_bounds]] = False
    acc = acc[in_sheet]

    if acc.size and existing_verify:
        ev_bounds = np.asarray(
            [polygon.bounds for polygon in existing_verify], dtype=np.float64
        )
        e_minx, e_miny = ev_bounds[:, 0], ev_bounds[:, 1]
        e_maxx, e_maxy = ev_bounds[:, 2], ev_bounds[:, 3]
        px = points[acc, 0]
        py = points[acc, 1]
        c_minx = px + rminx
        c_miny = py + rminy
        c_maxx = px + rmaxx
        c_maxy = py + rmaxy
        chunk = 4096
        for start in range(0, acc.size, chunk):
            sl = slice(start, start + chunk)
            ov = (
                (c_maxx[sl, None] > e_minx[None, :])
                & (c_minx[sl, None] < e_maxx[None, :])
                & (c_maxy[sl, None] > e_miny[None, :])
                & (c_miny[sl, None] < e_maxy[None, :])
            )
            screen_pairs += int(ov.sum())
            for r_local in np.flatnonzero(ov.any(axis=1)):
                index = int(acc[start + r_local])
                cand_verify = translate(
                    rotated_verify,
                    xoff=float(points[index, 0] - rotated_centroid.x),
                    yoff=float(points[index, 1] - rotated_centroid.y),
                )
                hit = False
                for e_pos in np.flatnonzero(ov[r_local]):
                    exact_checks += 1
                    existing = existing_verify[int(e_pos)]
                    if existing.intersects(cand_verify):
                        if cand_verify.intersection(existing).area > area_tolerance:
                            hit = True
                            break
                if hit:
                    if repair_enabled:
                        t_repair = time.perf_counter()
                        new_pos, attempts = _repair_candidate(
                            float(points[index, 0]), float(points[index, 1]),
                            rotated_verify, rotated_poly, rotated_centroid,
                            sheet, existing_verify, existing_polygons,
                            area_tolerance, bin_polygon,
                        )
                        repair_ms += (time.perf_counter() - t_repair) * 1000
                        repair_attempts += attempts
                        if new_pos is not None:
                            repairs += 1
                            if (new_pos[0] != float(points[index, 0])
                                    or new_pos[1] != float(points[index, 1])):
                                if repaired_positions is None:
                                    repaired_positions = {}
                                repaired_positions[index] = new_pos
                        else:
                            valid[index] = False
                            rejections += 1
                            repair_failures += 1
                    else:
                        valid[index] = False
                        rejections += 1

    verify_ms = (time.perf_counter() - t_verify) * 1000
    if probe is not None:
        probe['verify_candidates'] = probe.get('verify_candidates', 0) + candidates
        probe['verify_bounds_rejections'] = probe.get(
            'verify_bounds_rejections', 0) + bounds_rejections
        probe['verify_screen_pairs'] = probe.get(
            'verify_screen_pairs', 0) + screen_pairs
        probe['verify_exact_checks'] = probe.get(
            'verify_exact_checks', 0) + exact_checks
        probe['verify_rejections'] = probe.get('verify_rejections', 0) + rejections
        probe['verify_ms'] = probe.get('verify_ms', 0.0) + verify_ms
        if repairs or repair_failures:
            probe['verify_repairs'] = probe.get('verify_repairs', 0) + repairs
            probe['verify_bounds_repairs'] = probe.get(
                'verify_bounds_repairs', 0) + bounds_repairs
            probe['verify_repair_failures'] = probe.get(
                'verify_repair_failures', 0) + repair_failures
            probe['verify_repair_attempts'] = probe.get(
                'verify_repair_attempts', 0) + repair_attempts
            probe['verify_repair_ms'] = probe.get(
                'verify_repair_ms', 0.0) + repair_ms
        if repaired_positions:
            existing_side = probe.get('_verify_repaired')
            if existing_side:
                existing_side.update(repaired_positions)
            else:
                probe['_verify_repaired'] = repaired_positions


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


class _RotationTotals:
    """Accumulator for the sub-phase timings of one placement's rotations.

    Replaces three locals that were incremented from inside the future loop.
    The pool path and the serial path both fill one of these, so the totals are
    summed in one place afterwards rather than in each branch. The fields are
    plain floats because the serial path -- and the pool's drain loop, which
    runs on this thread -- are the only writers; `_evaluate_rotation` returns
    per-rotation timings rather than accumulating them itself.
    """

    __slots__ = ("nfp_ms", "validity_ms", "score_ms")

    def __init__(self):
        self.nfp_ms = 0.0
        self.validity_ms = 0.0
        self.score_ms = 0.0


class PlacementOptimizer:
    """
    Handles the geometric logic of finding the best position for a part on a sheet.
    """
    def __init__(
        self, engine, rotation_steps, search_direction, log_callback=None,
        trial_callback=None, rng=None, performance_logging=False,
        candidate_geometry_key_tracker=None, candidate_geometry_cache=None,
        rotation_workers=None, rotation_workers_explicit=True
    ):
        # Resolved once, here, rather than per call. find_best_placement runs
        # once per part placed -- 122 times in a GA run -- so re-reading the
        # environment and the UI value on each call is 122 redundant lookups.
        # None means "auto": defer to the stdlib default.
        self.rotation_workers = rotation_workers
        self.rotation_workers_explicit = rotation_workers_explicit
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
            # 1 when the width was inferred from the core count rather than
            # chosen. A report that names only the width cannot say whether a
            # number was a decision or a default.
            'rotation_workers_auto': 0,
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
        if getattr(part, 'verify_polygon', None) is None:
            # NEST-034: no un-simplified geometry for this shape (verify
            # build failed at creation, reload/legacy wrapper). Fall back to
            # the mask polygon -- perfectly registered by construction, and
            # _evaluate_rotation skips the stage when the two are the same
            # object, so this part keeps exactly today's behaviour.
            part.verify_polygon = part.original_polygon
        if getattr(part, 'verify_original_polygon', None) is None:
            # Pristine candidate source: same registration argument. When
            # the original itself was just adopted from polygon (reload
            # path), this lands on the same object and disables the stage.
            part.verify_original_polygon = part.verify_polygon
            
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
        
        # One rng per rotation, minted here on this thread in `angles` order.
        #
        # `score_gravity` breaks a metric tie with `rng.randrange`
        # (minkowski_engine.py:617), and every rotation's evaluation ran that
        # against the *shared* `self.rng`. Under the pool that draw happens
        # concurrently from several worker threads, so which tie-index each
        # rotation received depended on thread interleaving: measured 29
        # distinct drawing threads for one seeded pooled run, against 1 with
        # `NESTING_ROTATION_WORKERS=0`. A seeded run was therefore not
        # reproducible no matter what the fold order did.
        #
        # Minting children up front keeps the random tie-break -- which is worth
        # keeping, since always taking `tied[0]` biases placement toward
        # whichever candidate happens to be first -- while making each rotation's
        # draw depend only on its own index. Cost is one `getrandbits` per
        # rotation per part placed.
        rotation_rngs = [random.Random(self.rng.getrandbits(64))
                         for _ in angles]

        # Rotations are evaluated either serially or through a pool. Candidate
        # point-in-polygon rejection runs on the CPU via shapely, which releases
        # the GIL around the GEOS call but not around the surrounding Python.
        import time as _time
        t0_parallel = _time.perf_counter()
        # Recorded so the report line is self-describing: a run measured with
        # the env override must say so, otherwise the 4-vs-8 comparison is
        # unattributable. max_workers=None means the stdlib default.
        # Was the width chosen or inferred? Captured before the fallback, or the
        # flag is always zero and the counter is decoration.
        was_auto = not self.rotation_workers_explicit
        worker_limit = self.rotation_workers
        if worker_limit is None:
            worker_limit = _rotation_worker_limit()
        with self._perf_lock:
            # The width actually used, and whether it was chosen. Both are
            # recorded because neither is visible in the output otherwise, and
            # two runs with identical packing can differ in wall clock by 22%
            # purely on this. 0 is a real width here: it means "no pool", which
            # is a different execution model from a one-worker pool.
            self._perf_stats['rotation_workers'] = worker_limit
            self._perf_stats['rotation_workers_auto'] = int(was_auto)

        totals = _RotationTotals()
        if worker_limit:
            with ThreadPoolExecutor(max_workers=worker_limit) as executor:
                futures = {
                    executor.submit(
                        self._evaluate_rotation_tracked,
                        angle,
                        part,
                        placed_parts_grouped,
                        sheet,
                        direction,
                        rotation_rngs[index],
                    ): index
                    for index, angle in enumerate(angles)
                }

                for future in as_completed(futures):
                    try:
                        res = future.result()
                    except Exception as e:
                        self.log(f"Error in rotation evaluation thread: {e}")
                        continue
                    if res:
                        best_result = self._absorb_rotation_result(
                            res, part, best_result, totals)
        else:
            # No pool. Same evaluation, same order as the angles list, on this
            # thread. Two consequences, both measured in
            # tests/freecad_harness/bench_rotation_workers.py:
            #   - no pool construction or teardown per placement. The pool was
            #     built once per part placed -- 122 times in a GA run.
            #   - the result stops depending on completion order. Taking the
            #     best with a strict `<` means a metric tie is won by whichever
            #     rotation finishes first, which under a pool is a scheduling
            #     detail. Serial evaluation is order-stable by construction.
            for index, angle in enumerate(angles):
                try:
                    res = self._evaluate_rotation_tracked(
                        angle, part, placed_parts_grouped, sheet, direction,
                        rotation_rngs[index])
                except Exception as e:
                    self.log(f"Error in rotation evaluation: {e}")
                    continue
                if res:
                    best_result = self._absorb_rotation_result(
                        res, part, best_result, totals)

        total_nfp_ms = totals.nfp_ms
        total_validity_ms = totals.validity_ms
        total_score_ms = totals.score_ms

        dt_parallel = (_time.perf_counter() - t0_parallel) * 1000
        with self._perf_lock:
            self._perf_stats['placement_wall_ms'] += dt_parallel
        if self.performance_logging:
            self.log(f"[TIMING] '{getattr(part, 'id', '?')}': wall={dt_parallel:.0f}ms "
                     f"nfp={total_nfp_ms:.0f}ms validity={total_validity_ms:.0f}ms "
                     f"score={total_score_ms:.0f}ms "
                     f"({len(angles)} rotations, {len(sheet.parts)} placed)")
        if self.verbose:
            self.log(f"  -> {'Serial' if not worker_limit else 'Pool'} eval: "
                     f"{len(angles)} rotations in {dt_parallel:.1f}ms "
                     f"(workers: {worker_limit or 0})")
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

    def _absorb_rotation_result(self, res, part, best_result, totals):
        """Fold one evaluated rotation into the perf counters and the best-so-far.

        Extracted from the future loop so the pool and serial paths cannot
        drift apart: they were one block before, duplicated by hand the moment
        a second caller appeared. Everything the caller had to do -- accumulate
        the three sub-phase timings, add ~50 counters under the lock, then
        apply the strict `<` that decides the winner -- lives here.

        Returns the updated best_result, because the strict `<` compares
        against the incumbent and the winner is not necessarily `res`.
        """
        totals.nfp_ms += res.get('_t_nfp_ms', 0)
        totals.validity_ms += res.get('_t_validity_ms', 0)
        totals.score_ms += res.get('_t_score_ms', 0)
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
            # The collision stage's decomposition. The probe keys arrive here
            # underscore-prefixed -- that is the convention every other absorbed
            # key uses, and reading them unprefixed yields a silent 0 rather
            # than an error, because .get() defaults.
            for _key in ('collision_intersects_ms',
                          'collision_overlay_ms',
                          'collision_hole_probe_ms'):
                self._perf_stats[_key] = self._perf_stats.get(
                    _key, 0.0) + res.get(f'_{_key}', 0.0)
            # NEST-034 verify-on-accept counters. .get()-defaulted like the
            # ms keys above: they are absent from the perf initialiser, and
            # a bare [] += here would raise inside the rotation future loop
            # where quiet=True swallows it and the run still looks healthy.
            for _key in ('verify_candidates', 'verify_bounds_rejections',
                         'verify_screen_pairs', 'verify_exact_checks',
                         'verify_rejections', 'verify_fallback_pairs',
                         'verify_repairs', 'verify_bounds_repairs',
                         'verify_repair_failures',
                         'verify_repair_attempts', 'verify_repair_ms',
                         'verify_ms'):
                self._perf_stats[_key] = self._perf_stats.get(
                    _key, 0) + res.get(f'_{_key}', 0)
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
            # Call trial callback for each better result found
            if self.trial_callback and best_result.get('x') is not None:
                self.trial_callback(part, best_result['angle'], best_result['x'], best_result['y'])
        return best_result

    def _evaluate_rotation_tracked(
        self, angle, part, placed_parts_grouped, sheet, direction, rng=None
    ):
        """Track rotation-worker occupancy without changing evaluation behavior.

        `rng` is this rotation's own tie-break generator, minted by the caller.
        It is a parameter rather than `self.rng` because the shared instance is
        drawn from concurrently once the pool is on: measured 29 distinct drawing
        threads for one seeded pooled run, against 1 serial.
        """
        if self.performance_logging:
            with self._perf_lock:
                self._active_workers += 1
                self._perf_stats['max_concurrent_rotations'] = max(
                    self._perf_stats['max_concurrent_rotations'],
                    self._active_workers,
                )
        try:
            return self._evaluate_rotation(
                angle, part, placed_parts_grouped, sheet, direction, rng
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

    def _evaluate_rotation(self, angle, part, placed_parts_grouped, sheet, direction,
                          rng=None):
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

        # NEST-034 verify-on-accept: the candidate's un-simplified twin,
        # derived from the PRISTINE build-frame source -- the same contract
        # original_polygon has for the mask, so a part that was placed in an
        # earlier evaluation is re-derived from scratch rather than
        # double-rotated. Rotated about the ORIGINAL polygon's centroid --
        # the exact point origin='centroid' resolved to on the line above --
        # so mask and verify polygons receive one rigid motion and stay
        # registered. Skipped when the part carries no pristine verify
        # source (the find_best_placement fallback made it the mask polygon
        # itself) or when the gate is off.
        rotated_verify = None
        if VERIFY_ON_ACCEPT:
            verify_source = getattr(part, 'verify_original_polygon', None)
            if (verify_source is not None
                    and verify_source is not part.original_polygon):
                origin_center = part.original_polygon.centroid
                rotated_verify = rotate(
                    verify_source, angle,
                    origin=(origin_center.x, origin_center.y))

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
                rotated_verify=rotated_verify,
            )
            # NEST-036 repair-push side channel: rows the verify stage kept
            # alive by moving them. pts_arr is the engine's own candidate
            # cache array, so the move lands on a copy that scoring and
            # best-extraction read -- the cache (and the geometry-key
            # recording below, which reflects where the mask actually built
            # its translated polygons) stays on the pristine positions.
            repaired = validity_probe.pop('_verify_repaired', None)
            t_validity = _time.perf_counter()
            score_pts = pts_arr
            if repaired:
                score_pts = pts_arr.copy()
                for _idx, (_rx, _ry) in repaired.items():
                    score_pts[_idx, 0] = _rx
                    score_pts[_idx, 1] = _ry
            best_idx, metric = MinkowskiEngine.score_gravity(
                score_pts, valid_mask, direction, rng=rng)
            if best_idx is not None:
                best = {'x': float(score_pts[best_idx, 0]), 'y': float(score_pts[best_idx, 1]),
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
        performance_logging=False, rotated_verify=None,
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
            # NEST-034 verify-on-accept. Initialised here so a run reports
            # 0 rather than omitting them (gate off, empty sheet, no
            # survivors); _verify_stage then adds its own counts.
            probe['verify_candidates'] = 0
            probe['verify_bounds_rejections'] = 0
            probe['verify_screen_pairs'] = 0
            probe['verify_exact_checks'] = 0
            probe['verify_rejections'] = 0
            probe['verify_fallback_pairs'] = 0
            probe['verify_ms'] = 0.0
            # NEST-036 repair-push counters, same 0-not-omitted rationale.
            probe['verify_repairs'] = 0
            probe['verify_bounds_repairs'] = 0
            probe['verify_repair_failures'] = 0
            probe['verify_repair_attempts'] = 0
            probe['verify_repair_ms'] = 0.0
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
            # Empty sheet: the mask had no collision partner, but the
            # verify geometry can still reject a bounds breach -- the
            # un-simplified polygon's bbox is not the mask's bbox. That is
            # exactly the empty-sheet corner-flush case NEST-036's repair
            # must see, so the mask geometry travels along even though
            # there are no partners.
            _verify_stage(valid, points, rotated_verify, rotated_centroid,
                          sheet, [], area_tolerance, probe,
                          rotated_poly=rotated_poly,
                          existing_polygons=[])
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

        # NEST-034 verify-on-accept: survivors of the mask check are
        # re-checked on un-simplified geometry. Runs after every mask
        # rejection, so it only ever sees candidates the mask accepted.
        if rotated_verify is not None:
            existing_verify = []
            fallback_pairs = 0
            for shape, poly in zip(existing_shapes, existing_polygons):
                verify = getattr(shape, 'verify_polygon', None)
                if verify is None:
                    # No un-simplified geometry for this shape: pair is
                    # checked on the mask polygon -- today's behaviour.
                    verify = poly
                    fallback_pairs += 1
                existing_verify.append(verify)
            if probe is not None:
                probe['verify_fallback_pairs'] = probe.get(
                    'verify_fallback_pairs', 0) + fallback_pairs
            _verify_stage(valid, points, rotated_verify, rotated_centroid,
                          sheet, existing_verify, area_tolerance, probe,
                          rotated_poly=rotated_poly,
                          existing_polygons=existing_polygons,
                          bin_polygon=bin_polygon)

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
        # Kept on the nester so a run can report what it actually used. Both are
        # performance dials with no visible effect on the packing, so a reader
        # of the output has no other way to tell what produced a result.
        self.step_size = step_size
        self.rotation_workers = _rotation_worker_limit(kwargs)
        self.rotation_workers_explicit = _rotation_workers_explicit(kwargs)
        self.optimizer = PlacementOptimizer(
            self.engine, rotation_steps, self.search_direction,
            None if self.quiet else self.log_callback,
            self.trial_callback, rng=kwargs.get("rng"),
            performance_logging=self.performance_logging,
            candidate_geometry_key_tracker=kwargs.get("candidate_geometry_key_tracker"),
            candidate_geometry_cache=kwargs.get("candidate_geometry_cache"),
            rotation_workers=self.rotation_workers,
            rotation_workers_explicit=self.rotation_workers_explicit
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
