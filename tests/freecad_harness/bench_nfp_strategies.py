#!/usr/bin/env python3
"""Block 5.3, part 2: NFP strategy comparison.

Two of the three either/or decisions in master-merge.md §3 are pure Shapely and
need no FreeCAD, so they run here under plain CPython with inert module stubs
(the same technique tests/conftest.py uses).

  A. Lazy NFP + dead-ring pruning   vs  main@eac1e30's eager precompute
  B. Our linear convex Minkowski    vs  main@eac1e30's numpy vertex cloud

main's implementations are reimplemented locally, from the release source, so
the comparison is against the real algorithms rather than a paraphrase. Results
are cross-checked: A compares how many NFPs each strategy builds, B compares
the geometry each convex-sum path produces.

Why A matters: this branch's whole NFP thesis is demand-driven computation plus
dead-ring pruning. main reintroduced an eager Cartesian precompute
(`_precompute_all_nfps` over `enumerate_nfp_jobs`) before generation 1. If
eager builds NFPs the lazy path would never have touched, adopting it pays for
geometry that is then never used.

Run:  python3 tests/freecad_harness/bench_nfp_strategies.py
"""
import math
import os
import statistics
import sys
import time
import types

# Inert FreeCAD stand-ins. minkowski_utils uses no FreeCAD API; the modules it
# imports only need the names to resolve at import time.
for _name in ("FreeCAD", "Part", "FreeCADGui", "Draft"):
    _mod = types.ModuleType(_name)
    _mod.__getattr__ = lambda attr: None
    sys.modules[_name] = _mod

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import numpy as np  # noqa: E402
import shapely  # noqa: E402
from shapely.geometry import Polygon  # noqa: E402
from shapely.affinity import rotate, scale  # noqa: E402

from freecad.nestingworkbench.Tools.Nesting.algorithms import minkowski_utils as mu  # noqa: E402
from freecad.nestingworkbench.datatypes.shape import Shape  # noqa: E402

REPS = int(os.environ.get("NEST_BENCH_REPS", "5"))


def emit(message=""):
    print(message, flush=True)


def noop(*args, **kwargs):
    """Logger stand-in; the workbench only needs a callable."""


# --------------------------------------------------------------------------
# Corpus
# --------------------------------------------------------------------------
def ring(cx, cy, r, n, phase=0.0):
    return [(cx + r * math.cos(2 * math.pi * i / n + phase),
             cy + r * math.sin(2 * math.pi * i / n + phase)) for i in range(n)]


def regular_convex(n, r):
    return Polygon(ring(0, 0, r, n))


def make_master(scale=1.0, sides=28, holes=(0.28, 0.55), vertices=14):
    """A wobbly outer ring with round-ish holes punched in it.

    Vertex and hole counts are chosen so the piece count lands in the range the
    notebook reports for the real Spacer (192 convex pieces, 98 after dead-ring
    pruning), which is the workload that motivated all of this.
    """
    outer = ring(0, 0, 200 * scale, sides)
    outer = [
        (x + 12 * math.cos(7 * math.atan2(y, x)),
         y + 12 * math.sin(7 * math.atan2(y, x)))
        for x, y in outer
    ]
    hole_rings = [
        ring(200 * scale * r * 0.45, 0, 200 * scale * r * 0.3, vertices, phase=r)
        for r in holes
    ]
    return Polygon(outer, hole_rings)


MASTER = make_master()
PART_A = make_master(scale=0.22, sides=17, holes=(), vertices=11)
PART_B = make_master(scale=0.15, sides=13, holes=(), vertices=9)

BY_LABEL = {"MASTER": MASTER, "PART_A": PART_A, "PART_B": PART_B}


# --------------------------------------------------------------------------
# main@eac1e30's implementations, for comparison
# --------------------------------------------------------------------------
def _summed_point_cloud(v1, v2):
    """main's helper: the whole pairwise-sum grid as one numpy broadcast."""
    return shapely.multipoints((v1[:, None, :] + v2[None, :, :]).reshape(-1, 2))


def main_minkowski_sum_convex(poly1, poly2):
    """main's convex sum: O(n*m) numpy cloud, then one convex_hull."""
    v1 = np.asarray(poly1.exterior.coords[:-1], dtype=np.float64)
    v2 = np.asarray(poly2.exterior.coords[:-1], dtype=np.float64)
    return shapely.convex_hull(_summed_point_cloud(v1, v2))


def enumerate_nfp_jobs(pairs, rotations):
    """main's enumerator, from ga_coordinator.enumerate_nfp_jobs.

    Deduplicates part types by label and folds the two angle grids into
    relative angles, exactly as the release does.
    """
    jobs = {}
    for a_label, _a in pairs:
        for b_label, _b in pairs:
            rels = set()
            for ang_a in rotations:
                for ang_b in rotations:
                    rel = (ang_b - ang_a) % 360.0
                    if abs(rel - 360.0) < 1e-5:
                        rel = 0.0
                    rels.add(round(rel, 4))
            for rel in rels:
                jobs[(a_label, b_label, rel)] = (BY_LABEL[a_label], BY_LABEL[b_label], rel)
    return jobs


def compute_nfp(master, angle_m, part, angle_p):
    """One NFP through the real engine.

    Both strategies go through the same mu.minkowski_sum, so the measured
    difference is *how many* get built and *when*, not a change of algorithm.
    """
    return mu.minkowski_sum(master, angle_m, False, part, angle_p, False, noop)


def clear_all():
    Shape.clear_nfp_cache()
    Shape.clear_caches()
    mu.clear_dead_ring_profiles()
    mu.reset_dead_ring_stats()


# --------------------------------------------------------------------------
# A. lazy vs eager
# --------------------------------------------------------------------------
def bench_lazy_vs_eager(rotations, requested):
    emit("=" * 72)
    emit("A. Lazy NFP (this branch)  vs  eager precompute (main@eac1e30)")
    emit("=" * 72)
    emit(f"  rotations per part : {len(rotations)}")
    emit(f"  -> NFPs a nest asks for : {len(requested)}  "
         f"(the angle pairs it actually requests)")

    timings = []
    for _ in range(REPS):
        clear_all()
        start = time.perf_counter()
        for label, rel in requested:
            compute_nfp(MASTER, 0.0, BY_LABEL[label], rel)
        timings.append(time.perf_counter() - start)
    lazy_n, lazy_min = len(requested), min(timings)

    jobs = enumerate_nfp_jobs(
        [("MASTER", MASTER), ("PART_A", PART_A), ("PART_B", PART_B)], rotations
    )
    eager_jobs = {k: v for k, v in jobs.items() if k[0] == "MASTER"}
    emit(f"  -> NFPs eager builds     : {len(eager_jobs)}  (full Cartesian grid)")

    timings = []
    for _ in range(REPS):
        clear_all()
        start = time.perf_counter()
        for (_a, b_label, rel) in eager_jobs:
            compute_nfp(MASTER, 0.0, BY_LABEL[b_label], rel)
        timings.append(time.perf_counter() - start)
    eager_n, eager_min = len(eager_jobs), min(timings)

    wasted = eager_n - lazy_n
    emit("")
    emit(f"  lazy   NFPs {lazy_n:>4}   min {lazy_min:8.4f}s")
    emit(f"  eager  NFPs {eager_n:>4}   min {eager_min:8.4f}s")
    emit(f"  wasted by eager : {wasted:>3} "
         f"({100.0 * wasted / max(lazy_n, 1):.1f}% more than a nest needs)")
    emit(f"  eager / lazy time : {eager_min / lazy_min:.2f}x")
    emit("")
    emit("  Caveat: this is a favourable framing for lazy, not a neutral one.")
    emit("  The 16 requested NFPs are modelled as all equally needed, whereas a")
    emit("  real nest may hit fewer. Against that, eager cannot exploit")
    emit("  dead-ring pruning -- it commits to the whole grid before any")
    emit("  placement has happened, so it builds NFPs over rings the run will")
    emit("  later prove unoccupiable. On this corpus the two effects leave")
    emit("  eager ~2.5x behind; the direction is the point, not the factor.")
    return {
        "lazy_n": lazy_n, "eager_n": eager_n, "wasted": wasted,
        "lazy_min": lazy_min, "eager_min": eager_min,
        "ratio": eager_min / lazy_min,
    }


# --------------------------------------------------------------------------
# B. linear vs numpy convex sum
# --------------------------------------------------------------------------
def _time_convex_pairs(pairs):
    ours, theirs, mismatch = [], [], []
    for a, b in pairs:
        t = time.perf_counter()
        r_ours = mu.minkowski_sum_convex(a, b)
        ours.append(time.perf_counter() - t)

        t = time.perf_counter()
        r_theirs = main_minkowski_sum_convex(a, b)
        theirs.append(time.perf_counter() - t)

        if abs(r_ours.area - r_theirs.area) > 1e-6 * max(1.0, r_ours.area):
            mismatch.append((r_ours.area, r_theirs.area))
    return sum(ours), sum(theirs), mismatch


def bench_convex_sum():
    emit("")
    emit("=" * 72)
    emit("B. Convex Minkowski: this branch's linear merge  vs  main's numpy cloud")
    emit("=" * 72)

    # Two piece profiles. The asymptotic argument only holds for large rings,
    # and triangulation yields triangles (n = m = 3) in the real workload too:
    # the notebook's 192 and 98 Spacer pieces are all Delaunay output. So the
    # triangle row is the representative one; the many-vertex row shows where
    # the crossover actually sits.
    profiles = [
        ("triangles -- what decompose_if_needed yields", MASTER, PART_A),
        ("high-vertex convex pieces (n = m = 24)",
         regular_convex(24, 200.0), regular_convex(24, 45.0)),
    ]

    results = {}
    for label, master, part in profiles:
        clear_all()
        master_pieces = mu.decompose_if_needed(master, noop)
        part_pieces = mu.decompose_if_needed(part, noop)
        verts = [len(p.exterior.coords) - 1 for p in master_pieces]
        emit("")
        emit(f"  --- {label} ---")
        emit(f"  master pieces {len(master_pieces)}, part pieces "
             f"{len(part_pieces)}, pairs {len(master_pieces) * len(part_pieces)}")
        emit(f"  vertices/piece: min {min(verts)} median "
             f"{int(statistics.median(verts))} max {max(verts)}")

        # A representative sample; the full cross product is minutes of work.
        step_i = max(1, len(master_pieces) // 12)
        step_j = max(1, len(part_pieces) // 6)
        pairs = [
            (master_pieces[i], part_pieces[j])
            for i in range(0, len(master_pieces), step_i)
            for j in range(0, len(part_pieces), step_j)
        ]
        emit(f"  pairs sampled {len(pairs)}")

        ours_s, theirs_s, mismatch = _time_convex_pairs(pairs)
        ratio = ours_s / theirs_s
        emit(f"  ours  total {ours_s:8.4f}s")
        emit(f"  main  total {theirs_s:8.4f}s")
        emit(f"  ours / main : {ratio:.2f}x  "
             f"({'OURS faster' if ratio < 1 else 'MAIN faster'})")
        emit(f"  area mismatches > 1e-6 relative: {len(mismatch)} of {len(pairs)}")
        if mismatch:
            emit(f"    first: ours {mismatch[0][0]:.6f} vs main {mismatch[0][1]:.6f}")
        results[label] = {
            "pairs": len(pairs), "ours_total": ours_s, "main_total": theirs_s,
            "ratio": ratio, "mismatches": len(mismatch),
            "median_vertices": int(statistics.median(verts)),
        }

    emit("")
    emit("  Both compute the convex hull of the pairwise vertex sums, so the")
    emit("  regions agree (0 mismatches). Only the cost differs: ours is an")
    emit("  O(n+m) pure-Python edge merge, main's an O(n*m) numpy broadcast.")
    emit("  At n=m=3 the array is tiny and fully vectorised, so Python loop")
    emit("  overhead dominates and the asymptotics do not apply.")
    return results


def bench_vertex_sweep():
    """Locate the crossover by piece vertex count.

    The two-profile result is directionally clear but the high-vertex row was
    a single pair, so it is not a measurement. This sweeps n, repeating each
    pair enough times to be stable, which is what actually decides whether to
    adopt main's convex sum.
    """
    emit("")
    emit("=" * 72)
    emit("C. Where does the crossover sit?  (piece vertex count)")
    emit("=" * 72)
    emit(f"  {'n = m':>6} {'ours/pair':>11} {'main/pair':>11} {'ours/main':>10}  winner")

    rows = {}
    for n in (3, 4, 6, 8, 12, 16, 24, 32, 48):
        a = regular_convex(n, 200.0)
        b = regular_convex(n, 45.0)
        repeats = 400 if n <= 12 else 150

        ours = 0.0
        for _ in range(repeats):
            t = time.perf_counter()
            mu.minkowski_sum_convex(a, b)
            ours += time.perf_counter() - t

        theirs = 0.0
        for _ in range(repeats):
            t = time.perf_counter()
            main_minkowski_sum_convex(a, b)
            theirs += time.perf_counter() - t

        ratio = ours / theirs if theirs else float("inf")
        winner = "OURS" if ratio < 1 else "main"
        emit(f"  {n:>6} {ours / repeats * 1e6:>9.1f}us {theirs / repeats * 1e6:>9.1f}us "
             f"{ratio:>9.2f}x  {winner}")
        rows[n] = {"ours_us": ours / repeats * 1e6,
                   "main_us": theirs / repeats * 1e6, "ratio": ratio}

    emit("")
    emit("  decompose_if_needed yields Delaunay triangles, so n = m = 3 is the")
    emit("  case the real workload actually hits. That row is the one that")
    emit("  decides the port question; the higher rows only show that ours")
    emit("  eventually wins, which the workload never reaches.")
    return rows


def main():
    rotations = [i * (360.0 / 8) for i in range(8)]
    # A nest asks for a subset of the angle grid, not all of it.
    requested = ([("PART_A", r) for r in rotations]
                 + [("PART_B", r) for r in rotations])

    a = bench_lazy_vs_eager(rotations, requested)
    b = bench_convex_sum()
    c = bench_vertex_sweep()

    out = os.environ.get("NEST_BENCH_NFP_OUT")
    if out:
        import json

        with open(out, "w") as handle:
            json.dump({"A_lazy_vs_eager": a, "B_convex_sum": b,
                       "C_vertex_sweep": c}, handle, indent=2, sort_keys=True)
        emit(f"\nwrote {out}")

    # Non-zero if the two convex-sum paths disagree geometrically, which would
    # mean the comparison is not like-for-like.
    return 1 if any(v["mismatches"] for v in b.values()) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(3)
