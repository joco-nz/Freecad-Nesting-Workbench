# Block 5.3 — benchmark results

The three either/or decisions in `master-merge.md` §3, measured rather than
argued. Reproduce with:

```sh
FREECAD=/home/james/freecad_env/usr/bin/freecadcmd

# 1. rotation pool width (interleaved, one session)
NEST_BENCH_REPS=7 $FREECAD tests/freecad_harness/bench_rotation_workers.py

# 1b. same, on the real n70 geometry
NEST_BENCH_CORPUS=tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd \
NEST_BENCH_SHEET=700x500 NEST_BENCH_QUANTITY=6 NEST_BENCH_REPS=3 \
    $FREECAD tests/freecad_harness/bench_rotation_workers.py

# 2 + 3. lazy vs eager NFP, and the convex-sum crossover (plain CPython)
python3 tests/freecad_harness/bench_nfp_strategies.py
```

Machine: 4 CPUs (AMD Ryzen 7 1700, `nproc`=4), 13 GB. FreeCAD 26.3.0.

---

## 1. Rotation thread pool — the pool is a large win here

The experiment `make-faster.md:2114` lists as outstanding, and the one that
would settle main@eac1e30's claim that *"serial evaluation is 2.4–3.3x faster"*.

### Synthetic corpus (18 parts, ~1.1 s per nest)

| width | min | median | max | vs width 1 |
|---|---|---|---|---|
| 1 | 1.039 s | 1.151 s | 1.182 s | 1.00× |
| 2 | 1.087 s | 1.130 s | 1.219 s | 1.05× |
| 4 | 1.036 s | 1.159 s | 1.270 s | 1.00× |
| 8 | 1.058 s | 1.115 s | 1.192 s | 1.02× |

**No difference.** The workload is too small: 18 boxes nesting in ~1.1 s, so
pool start-up costs as much as it saves. This is why the small corpus is not
decisive and the n70 run exists.

### n70 corpus (18 real parts, 6 sheets, ~11–18 s per nest)

| width | min | median | max | vs width 1 |
|---|---|---|---|---|
| 1 | 18.140 s | 18.214 s | 18.389 s | 1.00× |
| 2 | 12.014 s | 12.133 s | 12.834 s | **0.66×** |
| 4 | 10.574 s | 11.019 s | 11.896 s | **0.58×** |
| 8 | 10.068 s | 10.780 s | 11.253 s | **0.56×** |

**1.80× faster at width 8.** Near-linear to 2 workers (1.51×), then diminishing
(1.14×, then 1.05×). Width 8 already oversubscribes 4 CPUs, and still wins
slightly — the pool is not being hurt by contention at this width.

Packing result identical at every width on both corpora
(18 placed, 6 sheets, density 0.353970; 1 distinct fingerprint across 12 runs).

### Reading

main's "serial is 2.4–3.3× faster" is **not reproduced** — the sign is
reversed here. Two honest caveats:

- **4 CPUs.** The curve saturates by design on this box. The branch's own
  `nesting_strategy` docstring warns the candidate-geometry work is *"GEOS-bound
  and releases the GIL"*, so oversubscription should hurt more on a many-core
  machine. main's own benchmark comment (in `ga_coordinator`) shows the same
  saturate-early shape for the precompute pool on 24 cores: 1w 1.611s, 2w 1.341s
  (1.20×), 4w 1.411s, 8w 1.505s — worse at 4 and 8 than at 2. So the *shape* of
  the curve is probably similar on a big box; the absolute factor will not be
  1.8×.
- The notebook's own caution (`make-faster.md:100-105`) predicted the result
  would not be provably width-neutral, because `find_best_placement` breaks
  ties with a strict `<` so the first future to finish wins. **On these two
  corpora it is width-invariant.** That is a measurement, not a proof — a
  corpus with genuine metric ties could still diverge.

**Decision: keep the pool.** The evidence to delete it does not exist; the
evidence to keep it does. Revisit on a many-core machine before treating
width 4–8 as the right default.

---

## 2. Lazy NFP vs main's eager precompute

main reintroduced `_precompute_all_nfps` over `enumerate_nfp_jobs` before
generation 1.

| | NFPs built | min time |
|---|---|---|
| lazy (this branch) | 16 | 1.022 s |
| eager (main) | 24 | 2.612 s |

**Eager is 2.56× slower and builds 50% more NFPs.**

Caveat, stated because it cuts against the conclusion: this framing favours
lazy. The 16 requested NFPs are modelled as all equally needed, whereas a real
nest may touch fewer — which would help lazy further. Against that, eager
*cannot* exploit dead-ring pruning: it commits to the whole grid before any
placement has happened, so it builds NFPs over rings the run later proves
unoccupiable. The two effects leave eager ~2.5× behind here. The direction is
the finding; the factor is not portable.

**Decision: keep lazy. Do not adopt the eager precompute.**

---

## 3. Convex Minkowski — main is faster, and this corrects an earlier claim

Earlier in this work the linear edge merge was described as *"algorithmically
better"* than main's numpy cloud, with the wall-clock *"genuinely unproven"*.
It is now measured, and the claim was wrong for this workload.

Crossover by piece vertex count (repeated pairs, min-of-many):

| n = m | ours | main | ours/main | winner |
|---|---|---|---|---|
| **3** | 139.7 µs | 105.1 µs | **1.33×** | **main** |
| 4 | 141.8 µs | 114.5 µs | 1.24× | main |
| 6 | 149.2 µs | 132.3 µs | 1.13× | main |
| 8 | 155.4 µs | 154.3 µs | 1.01× | main |
| 12 | 165.7 µs | 222.0 µs | 0.75× | ours |
| 16 | 178.3 µs | 291.1 µs | 0.61× | ours |
| 24 | 200.3 µs | 479.9 µs | 0.42× | ours |
| 32 | 224.5 µs | 745.0 µs | 0.30× | ours |
| 48 | 270.1 µs | 1568.8 µs | 0.17× | ours |

Crossover at **n ≈ 8**. Geometry identical throughout (0 area mismatches) —
both compute the convex hull of the pairwise vertex sums, so only cost differs.

**The case that matters is n = 3.** `decompose_if_needed` yields Delaunay
triangles, so every piece has 3 vertices. The O(n+m) advantage of the linear
merge needs rings big enough to amortise Python loop overhead; at n = 3 the
numpy array is a fully-vectorised 3×3 and simply wins.

### How much is that worth

Real n70 Spacer against Bottle Top, using the `timings` hook `minkowski_sum`
already exposes:

```
Spacer 442 pieces, Bottle Top 72 pieces, 31824 convex pairs

  decompose_ms          105.4
  transform_ms           38.9
  convex_sum_ms        1080.8      <- 40.6% of the two hot phases
  union_ms             1582.1
```

`minkowski_sum` is 97.5% of nest wall time on this corpus, and `convex_sum_ms`
is ~38.5% of `minkowski_sum`. So a 1.33× on the convex sum is worth roughly
**9% of total nest time** — real, but not a step change.

**Decision: do not port it.** The reasons, in order of weight:

1. ~9%, not a transformation.
2. It would delete the reference implementation
   (`_minkowski_sum_convex_reference`) and the per-pair validity gates, and the
   whole `[GA PERF]` timing breakdown (`convex_pair_checks_ms`,
   `convex_pair_validity_ms`, `convex_merge_ms`, `convex_fallback_ms`) is
   instrumented around this branch's 3-phase structure.
3. The win is entirely an artefact of n = 3. Any future change producing
   higher-vertex pieces — including main's own `_merge_convex_parts`, which
   takes the Spacer from 48 pieces at mean 3.0 vertices to 18 pieces at mean
   4.7 — pushes the workload toward the crossover and eventually past it.

Recorded so it can be revisited if decomposition ever stops producing
triangles.

---

## Also measured: main's convex merge, in isolation

`make-faster.md:371-377` had already rejected `_merge_convex_parts` at
23.97 s → 44.58 s. Confirmed here on the same corpus, and the mechanism is now
clear:

| | pieces | mean vertices | pairs vs a 17-piece part |
|---|---|---|---|
| as decomposed | 48 | 3.0 | 816 |
| after main's merge | 18 | 4.7 | 306 |

The merge does what it claims — 2.7× fewer pairs. It loses because the union
and convex-hull checks *inside* the merge cost more than the pair reduction
saves. That is unaffected by which convex sum is used, so the existing
reversal stands and is now understood rather than merely recorded.

Note the two main changes pull in opposite directions: the merge raises
vertices per piece, which is exactly the regime where their own numpy cloud
loses. At mean 4.7 it is still on main's side of the crossover, so they do not
outright conflict — but they are not independent either.

---

## Summary of decisions

| decision | outcome |
|---|---|
| rotation thread pool | **keep** — 1.80× at width 8 on 4 CPUs; main's serial claim not reproduced |
| lazy vs eager NFP | **keep lazy** — eager 2.56× slower, 50% more NFPs |
| linear vs numpy convex sum | **keep the linear merge** — main is 1.33× faster at n=3 but worth only ~9% of nest time, and costs the reference implementation and the timing instrumentation |
| `_merge_convex_parts` | **do not take** — existing reversal confirmed and now explained |

No baseline re-record needed: none of this changes the nesting code.
