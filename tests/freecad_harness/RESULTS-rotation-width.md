# Serial against the rotation pool: main@eac1e30's 2.4–3.3× claim, measured

Question: main deleted the rotation `ThreadPoolExecutor` outright, on the claim
that

> Serial evaluation across rotations. Measured against a per-placement
> ThreadPoolExecutor, serial evaluation is 2.4-3.3x faster due to eliminating
> thread pool creation/teardown overhead and GIL contention.

— `nesting_strategy.py`, at the site of the deletion.

`bench_rotation_workers.py` was written to settle this and never run. Its own
docstring calls the claim unproven; the harness README lists the comparison as
outstanding. This file is the result.

**Short answer: the claim does not reproduce.** Serial is 45% faster on the
light corpus and 2% *slower* on the realistic one. The default stays at one
thread per core.

## 1. Why the comparison needed new code to run at all

There was no way to ask for serial. The smallest expressible width was 1, and
that still builds and tears down a `ThreadPoolExecutor` on every placement — so
"serial" and "width 1" were indistinguishable, and the comparison the benchmark
was built for could not be expressed.

`NESTING_ROTATION_WORKERS=0` now selects the no-pool path. **The default is
unchanged**: an unset width still resolves to `os.cpu_count()`.

Both paths now fold results through one `_absorb_rotation_result`, so the ~50
perf counters cannot drift between them. That is not cosmetic — a key listed in
the absorption but absent from `_perf_stats` raises `KeyError` inside the loop,
and `quiet=True` sends it nowhere.

## 2. Method

`bench_rotation_workers.py`, which already handles the traps:

- **Interleaved**, round-robin within one process, so drift and thermal effects
  hit every width equally. `RESULTS-parallelism.md` §2 records why this matters:
  the same configuration measured minutes apart gave 144.6 s, 155.4 s, then
  174.6 s.
- **Caches cleared before every single run.** They are class-level, so run 2
  onwards would otherwise be all cache hits and measure nothing.
- **Result fingerprinted** alongside the timing, so a width change that alters
  the layout is visible rather than assumed away.
- **Paired by rep index**, since interleaving makes rep *i* of each arm the same
  drift conditions. Reported as a paired t below.

Two corpora, because one is not enough — see §5. Seed 20260925, 4 CPUs.

| corpus | parts | sheet | what it is |
|---|---|---|---|
| synthetic | 18 | 450×350 | 3 rectangles, an L, a triangle, a holed plate; ~1.1 s per nest |
| n70 | 18 | 700×500 | the real intercooler spacer corpus; ~4.3 s per nest |

## 3. Synthetic — serial wins, decisively

n=8 per arm, interleaved:

| width | min | median | max | sd |
|---:|---:|---:|---:|---:|
| **0 (serial)** | **0.782** | **0.837** | 0.876 | 0.037 |
| 4 | 1.160 | 1.217 | 1.250 | 0.036 |

Paired difference (pool − serial): mean **+0.378 s**, sd 0.061, **t = +17.6**,
n=8. Mann-Whitney U=0, p=0.0008.

Serial is **45% faster**, and the effect is an order of magnitude larger than
the noise. This is not in doubt.

Result identical both arms: 1 sheet, 18 placed, density 0.563184.

## 4. n70 — the pool wins, marginally

n=8 per arm, interleaved:

| width | min | median | max | sd |
|---:|---:|---:|---:|---:|
| 0 (serial) | 4.250 | 4.363 | 4.428 | 0.054 |
| **4** | **4.061** | **4.281** | 4.371 | 0.111 |

Paired difference (pool − serial): mean **−0.103 s**, sd 0.147, **t = −1.98**,
n=8. Mann-Whitney U=12, p=0.036.

The pool is **1.9% faster by median**, 4.4% by min. Read the sd: the effect is
0.103 s against a per-rep spread of 0.147 s. **This is marginal, not
established.** It is the opposite sign to §3, which is the actual finding.

Result identical both arms: 6 sheets, 18 placed, density 0.353970.

End-to-end through the GA (`bench_ga.py`, population 3 × 2 generations, n70,
alternating arms to counter drift):

| width | walls (s) | mean |
|---:|---|---:|
| 0 (serial) | 4.910, 4.927 | 4.919 |
| 4 | 4.804, 4.778 | 4.791 |

Pool 2.6% faster. Two pairs, consistent direction, n=2 — **indicative only**,
and for a second reason. The two arms are *not* doing identical work:

| counter | serial | pool=4 |
|---|---:|---:|
| rotations | 57 | 57 |
| candidates | 5912 | 5877 |
| exact_collision_checks | 5734 | 5753 |
| candidate_geometries built | 2184 | **3185** |

46% more candidate geometry is built under the pool. The cause is §6: the
shared `self.rng` is consumed in a different order, so the pool sees different
random values, builds different candidate geometry, and misses the geometry
cache more often. A serial arm is not simply "the same work without the
threads", and the 2.6% is not a clean speed ratio. The nesting result is
unchanged (2 sheets, 6 placed, 0.35397 efficiency in both).

This is worth stating plainly: **at width > 1 the GA's own work counters cannot
be compared against a serial run, even to explain a speed difference.** The
harness already warns about this for baseline gating; it applies to timing
comparisons too.

## 5. Why the answer is corpus-dependent

Both corpora use `rotation_steps=4`, so the pool has the same nominal
parallelism available in each. What differs is how long each rotation spends
inside GEOS versus Python.

The n70 corpus contains the Spacer — a 192-convex-piece part. Its candidate work
is heavy and shapely-bound, so more of each rotation is spent in GIL-released
calls and threads genuinely add throughput. The synthetic parts are simple
polygons; evaluation is fast, the per-placement pool construction dominates, and
threads only add contention.

This is exactly the mechanism `RESULTS-parallelism.md` §2 identified — the
collision stage reached only 1.31× concurrency against 4 cores because "the
Python around [the GEOS calls] does not [release the GIL]". Serial removes pool
overhead and loses parallelism; which wins depends on the ratio of those two
costs, and that ratio is a property of the geometry.

**It also means the synthetic corpus cannot settle this question alone**, which
is what `bench_rotation_workers.py` warns about in its own docstring: 18 parts
nesting in ~1.1 s is "light enough that thread-pool start-up could cancel a real
parallel gain". Reading §3 as the answer would have inverted the conclusion.

## 6. Reproducibility is not a reason to go serial

Work counts are **not** stable at width 4. Three identical runs moved **28 of
the 82 work counters** — `bbox_checks` 54588/55137/51821,
`collision_intersects_true` 2484/2238/2455, and so on.

This was investigated as a reason to prefer serial, and it is not one:

- **The packing result is stable.** All three runs returned identical output: 1
  sheet, 18 placed, density 0.563184, same per-label split. Users get the same
  nest.
- **We promise nothing about reproducibility.** There is no seed field in the
  panel. `random_seed` is read from `ui_params` (`nesting_controller.py:1187`)
  but never populated, so `ga_coordinator.py:498` falls through to
  `random.randrange(2**32)` and prints it. Every run is a fresh search, and a GA
  that returns different layouts is the algorithm working.
- **Where determinism is required it already holds.** The harness pins width 1,
  where all 40 count counters are stable, and the baseline gate refuses to
  compare across widths. The measurement path is reproducible.

The actual cause is narrower than result selection, and worth recording: the
shared `self.rng` is drawn from *inside* the evaluation
(`nesting_strategy.py:805`, `score_gravity(..., rng=self.rng)`), so concurrent
threads consume it in scheduling order and produce genuinely different candidate
points. Making the pool fold its results in a fixed order does **not** fix this —
measured, 28 counters still moved. Only a per-angle RNG derived from the run
seed would, which changes search behaviour and needs its own benchmark.

## 7. What is left: width 1 is the bad default

Not serial. Width 1 is worse than both neighbours on n70. From the wider sweep
(0, 1, 4, 8 — n=3 per width, so indicative only, and a different session from
§4's n=8):

| width | min | median |
|---:|---:|---:|
| 0 (serial) | 4.296 | 4.356 |
| **1** | **4.682** | **4.779** |
| 4 | 4.090 | 4.265 |
| 8 | 4.109 | 4.237 |

It pays the whole per-placement pool construction and teardown — 122 times in a
GA run — and receives no parallelism for it, because one worker executes
rotations sequentially anyway. That is the waste worth removing, and it is a
one-line change with a measured case behind it. **Not done here**, because the
default at `os.cpu_count()` is already the right answer for the width that
matters; this is a note for whoever tunes it next.

## Summary

| lever | measured | status |
|---|---|---|
| serial as the default | +45% synthetic / **−1.9% n70** | **rejected** — sign flips by corpus; the 2.4–3.3× claim does not reproduce |
| width 1 instead of core count | **12% worse** than width 4 on n70 | **not done** — identified, out of scope for a change whose point was measurement |
| width 0 as a selectable mode | — | **shipped** (`1484930`) — makes the comparison expressible |
| `bench_ga.py` pin clamp | — | **fixed** (`max(1, …)` measured a serial GA run as a 1-worker pool) |
| deterministic tie-break in the pool | 0 of 28 counters fixed | **rejected and stashed** — the concurrent RNG draw dominates |

**So: leave the pool at one thread per core.** The default is unchanged, and the
reason it is unchanged is now measured rather than inherited.

## Reproducing

```sh
FREECAD=/path/to/freecadcmd

# both corpora, serial against the core count, interleaved
NEST_BENCH_ROTATION_WORKERS_SWEEP=0,4 NEST_BENCH_REPS=8 \
    $FREECAD tests/freecad_harness/bench_rotation_workers.py

NEST_BENCH_CORPUS=tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd \
NEST_BENCH_SHEET=700x500 NEST_BENCH_QUANTITY=6 \
NEST_BENCH_ROTATION_WORKERS_SWEEP=0,4 NEST_BENCH_REPS=8 \
    $FREECAD tests/freecad_harness/bench_rotation_workers.py

# end-to-end, alternating arms to counter drift
for w in 0 4 0 4; do
  NEST_BENCH_CORPUS=tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd \
  NEST_BENCH_GA_SHEET=700x500 NEST_BENCH_QUANTITY=2 \
  NEST_BENCH_GA_POPULATION=3 NEST_BENCH_GA_GENERATIONS=2 \
  NEST_BENCH_ROTATION_WORKERS=$w NEST_BENCH_REPS=1 \
      $FREECAD tests/freecad_harness/bench_ga.py
done
```
