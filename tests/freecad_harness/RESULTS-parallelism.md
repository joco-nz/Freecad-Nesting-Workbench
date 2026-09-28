# Where the n70 GA run's time actually goes, and what can be taken out of it

Goal: 20–30 s off a ~120 s run. This is the search for that, including the
things that turned out not to work.

Every figure is from `tests/freecad_harness/run_ga.sh GA_CORPUS=n70` — the real
customer part, 1200×600, 2 Spacer + 60 Bottle Top + 60 Bottle Bottom, 8
generations, population 4, 122 parts placed, 2 sheets.

## 1. The map

One worker, 186.1 s wall, `nesting_s` 180.3 s:

| stage | s | % of generations |
|---|---:|---:|
| collision — `intersection().area` | 58.0 | 31.9% |
| NFP candidate generation | 36.6 | 20.1% |
| collision — `intersects()` | 21.8 | 12.0% |
| candidate geometry | 10.3 | 5.7% |
| collision — bookkeeping | 8.6 | 4.7% |
| NFP construction (the 72 misses) | 4.2 | 2.3% |

The two lines that matter are the top three: **64% of the run is the collision
test and the generation of the candidates it tests.**

The underlying ratio is stark: **639,410 collision tests to place 122 parts —
5,241 per part placed.** At 125 µs per test that is the 80 s it costs. Nothing
in the collision stage is doing wasted work per test; the problem is the number
of tests.

## 2. The one lever that works: the thread-pool width — 41.6 s, 22%

`_rotation_worker_limit()` returns `None` when `NESTING_ROTATION_WORKERS` is
unset, so `ThreadPoolExecutor()` takes the stdlib default of
`min(32, os.cpu_count() + 4)` — **8 threads on this 4-CPU box.** The docstring
already concedes the width "is measurable rather than assumed"; nobody measured
it.

Interleaved A/B, two runs per configuration, same process, same corpus:

| workers | wall (s) | placed | density |
|---:|---|---|---|
| 1 | 186.1, 188.5 | 122 | 0.418433 |
| 2 | 160.7, 162.3 | 122 | 0.418433 |
| **4** | **144.6, 148.4** | 122 | **0.418433** |
| 8 (the default) | 183.4, 184.6 | 122 | 0.418433 |

**4 workers is 22.3% faster than 1 worker, and 8 workers is no better than 1
worker at all.** The production default gives up ~40 s relative to the optimum.

The thread-time sums show the mechanism. These are per-worker accumulations, not
wall shares, so read the ratio:

| workers | rotation wall, summed | effective concurrency |
|---:|---:|---:|
| 1 | 174.7 s | 0.93× |
| 2 | 160.7 s | 1.12× |
| 4 | 267.4 s | 1.40× |
| 8 | 395.9 s | 2.16× |

At 8 workers the workers accumulate 2.3× the thread-time of the 1-worker run
for the same wall clock and the same work: pure contention, no throughput. The
collision stage reaches only 1.31× average concurrency against 4 available
cores, so the stage is substantially GIL-bound — Shapely releases the GIL
around GEOS calls, but the Python around them (the per-candidate loop, the
numpy slicing, the counters) does not.

**Density is bit-identical at every worker count** (0.418433, 122 placed), so
this is free. It is also already available: `NESTING_ROTATION_WORKERS=4` needs no
code change.

Scaling to the 128 s run in the report, 22.3% is ~28.5 s — the whole target, on
its own, from a configuration change.

## 3. What was tried and does not work

All measured on the real n70 geometry, face to face, min of 7. `probe3.py` and
`probe4.py`.

**Drop the interior rings and test the body only.** The Spacer carries 13
holes — 14 rings — and GEOS walks all of them for the overlay. The holes cannot
change an overlap verdict: a candidate inside a hole is not overlapping the
part's material. They matter for internal fit, which is a separate, separately
counted path (`hole_exploiting_placements`, `hole_sensitive_pairs`).

| part | holes | rings | full | body-only | speedup | verdicts agree |
|---|---:|---:|---:|---:|---:|---|
| Spacer | 13 | 14 | 1626.1 µs | 560.0 µs | **2.9×** | 7/7 |
| Bottle Top | 3 | 4 | 150.9 µs | 145.4 µs | 1.0× | 3/3 |
| Bottle Bottom | 2 | 3 | 343.3 µs | 329.1 µs | 1.0× | 7/7 |

Safe and 2.9× on the Spacer, and worth almost nothing here: the configuration
is 2 Spacers against 120 bottles. Weighted by part count, **1.11×, about 5 s.**
The cost is superlinear in ring count, so a 14-ring part pays and a 4-ring part
does not. Measured rather than assumed, because assuming it would have
recommended a change worth 5 s while claiming a 2.9× win.

**`shapely.prepare()`.** The engine already does this for NFP hole polygons.
On the collision pair: `intersects` 122.9 → 121.1 µs, `overlaps` 124.2 → 123.0
µs. Nothing. And nothing at all for the overlay (1630.6 vs 1635.9 µs), because a
prepared index helps predicates and a constructive overlay is not a predicate.

**`overlaps or contains` instead of the overlay.** 13.3× cheaper on real
geometry (124.2 vs 1635.9 µs) — a wider gap than the 6–10× recorded from the
synthetic corpus, which is itself a warning about measuring on stand-ins. Still
wrong: 89.4% of overlays have a positive area below the 1e-7 tolerance, which
the area test accepts and the predicate rejects. Confirmed on real geometry at
dx = −1e-8: area 2.111e-08, `overlaps` True.

Snapping coordinates to a grid does not rescue it. At 1e-6 and 1e-7 the
agreement gets *worse*, not better, and where it does agree it is on 7 samples
out of 7 with only one in the band — too thin a population to conclude from.

**The NFP/union work.** 2.3% of the run. Nothing there, whatever the union
costs in isolation.

## 4. The remaining lever, and it has never been measured

`step_size` — the NFP ring discretisation interval — defaults to a hardcoded
`5.0` mm and is not exposed as a parameter. It sets how many candidate positions
each NFP yields, and the candidate count multiplies *both* expensive stages:

```
collision + candidate generation = 43% + 20% = 63% of the run
```

At 145 candidates per rotation evaluation, halving `step_size` would be expected
to cut roughly 30% of the run, at the cost of packing density. **The
time/density curve for this dial is the one thing that should be measured next**,
because it is the only lever left that is large enough to matter once the pool
width is fixed, and it is a genuine trade rather than a free win.

## Summary

| lever | saving | status |
|---|---|---|
| thread-pool width 8 → 4 | **41.6 s (22.3%)** | measured, density-identical, available now via `NESTING_ROTATION_WORKERS=4` |
| `step_size` density dial | up to ~30%, costs density | unmeasured — the next thing to do |
| body-only overlay | ~5 s | measured, weighted 1.11×, not worth it here |
| `shapely.prepare` | ~0 | measured, no effect |
| `overlaps or contains` | — | 13.3× cheaper and **wrong**; rejected |
| NFP / union work | ~0 | 2.3% of the run |

**So: yes, 20–30 s is available, and most of it is not an optimisation but a
misconfiguration.** The pool defaults to twice the core count and pays ~40 s for
it. The code-level headroom that remains is the candidate-density dial, which
trades quality and has never been characterised.
