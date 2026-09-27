# Making NFP Precalculation Faster

## Current status — 2026-09-27

This document is a **chronological optimization lab notebook**. Earlier sections
record what was believed and measured at the time. Later findings supersede
several earlier recommendations, so the opening sections should not be read as a
current plan.

### Current bottleneck

The original bottleneck was eager **NFP** precalculation. That is no longer the
current bottleneck. NFP cost has since been cut by roughly 4x on the 122-part
Spacer/Bottle workload by the dead-ring pruning described below, taking
`nfp_compute` from 114.58 s in the control to 28-43 s depending on how loaded
the machine was that day.

For the 122-part Spacer/Bottle workload, the current leading repeated cost is
the GA candidate-evaluation stage reported by `[GA PERF TOTAL]`:

- `collision_intersection` — the largest aggregate worker cost, 350.87 s in the
  most recent run. This is the same stage that produced the DE-9IM reversal, so
  treat any new idea here as suspect until the fixture says otherwise.
- `candidate_geometry` — still meaningful at 25.36 s, but reduced substantially
  by the candidate-geometry cache when enabled.
- `nfp_compute` — no longer a lead cost.
- layout management is **closed** as a target (see "Master-shape pooling").

`collision_intersection` is now clearly the thing to attack next, and nothing has
been tried there since the DE-9IM gate was reverted. ~90% of the overlays that
survive to the area test are sub-micron slivers whose interiors genuinely touch,
so the cost is structural under current area-tolerance semantics and is
irreducible without revisiting those semantics.

### Validated and retained

Everything in the previous list, plus:

- **Dead-ring pruning** — interior rings that no part in the set can occupy are
  dropped from the NFP decomposition input before the cache key. Enabled by
  default; `NESTING_FILL_DEAD_HOLES=0` restores the old behaviour. Spacer ×
  Spacer went from 192 × 192 convex pieces (36,864 Minkowski sums) to 98 × 98
  (9,604), and the NFP is never smaller, so no placement can be lost. See
  "Pruning rings that no part can occupy".
- Collision-mask interior-ring instrumentation: `hole_rings`, `hole_vertices`,
  `exterior_vertices`, `hole_pct`, `subthreshold_rings`, `subthreshold_pct`,
  `hole_sensitive_pairs`, `hole_exploiting_placements`.
- `NESTING_ROTATION_WORKERS` knob and the `rotation_workers` report field, so
  the pool width is measurable rather than assumed.

### Experimental and opt-in

The **Candidate Geometry Cache** remains a run-scoped, opt-in Minkowski option,
**disabled by default**. The first enabled 122-part run showed a 75.2% hit rate,
`candidate_geometry` reduced from 87.44 s to 10.22 s aggregate, 1.82 s cache
overhead, and peak memory of about 1.9 GB versus about 1.8 GB when disabled. The
packing result remained 40.4% efficiency, 2 sheets, and 122 placed parts. A
cache-disabled control with the same seed (`649084969`) is still required before
making the cache the default.

### Rejected or deferred

- Greedy convex-piece merging — performance regression.
- `unary_union` variants (`union_all`, NumPy input) — no measurable gain.
- Coordinate redundancy cleanup — no useful redundancy found.
- Convex-pair result caching — zero key reuse in the measured workload.
- Native edge-merge, per-pair native buffer, and batched native buffer paths —
  isolated prototypes only; the real FreeCAD workload showed no meaningful
  end-to-end gain, so native integration is deferred.
- A **convex-polygon fast path** for the NFP decomposition. This sounded
  obviously right and is dead: `minkowski_utils` gates it on
  `not polygon.interiors`, but the Spacer's *exterior* is genuinely concave
  (exterior area 205,465 against a convex hull of 227,028), so the gate never
  opens on the part that matters.

### Timing interpretation

Most `[PERF]` and `[GA PERF TOTAL]` stage values (for example
`candidate_wall`, `rotation_wall`, `candidate_geometry`, and
`collision_intersection`) are **sums of per-worker times**, not wall-clock
durations. Only explicit `nesting`/generation totals represent wall time. The
current report also exposes `max_concurrent_rotations`; the earlier
`worker_samples` value was removed because it did not represent CPU utilization.

**And do not compare wall clock across sessions.** See "The machine moves under
the measurement": per-generation time on this box drifted 24 s → 31 s → 37 s
across three consecutive treatment runs, on work that is pure NFP-cache lookup
and cannot be touched by any change under test. A 20% swing is the noise floor.
Prefer machine-independent evidence — piece counts, structural invariants,
`freecadcmd` e2e measurements taken back to back with their control.

### Next action

1. `collision_intersection` (350.87 s aggregate) is the only remaining large
   cost. As with DE-9IM, the bar for a new idea there is a counting counter,
   measured on the fixture, before any code.
2. Run one cache-disabled control with seed `649084969` and Performance Logging.
3. If the paired result confirms the wall-time advantage and memory is
   acceptable, enable the candidate-geometry cache by default.
4. The 4-vs-8 rotation worker test. **Not yet run**, and now harder than it
   looks: `find_best_placement` takes the best with a strict `<`, so on a metric
   tie the first future to finish wins and completion order is
   scheduling-dependent. The result is not provably width-neutral, and the
   timing drift above would swamp a real 2x. It needs a pinned seed and
   interleaved A/B in one session, not two runs on different days.

### Reversals worth remembering

All are recorded in full below. They are listed here because the shape of each
is the kind of thing that recurs if the reasoning is not written down:

- The DE-9IM zero-area gate was correct code that lost money, because a counter
  was misread: `grazing_pairs` means "area within tolerance", **not** "the
  interiors touch". An area tolerance of 1e-7 is equally satisfied by an
  overlap of area 1e-8, and on the fixture 148,124 of 152,431 grazing pairs are
  sub-tolerance slivers with genuinely overlapping interiors.
- A harness stub bug once made an optimization look 2.7x *slower* than its
  baseline. The stub nested a `Vector` inside a `Vector`, which raised inside a
  swallowed `except`, which produced silently empty layouts. The run still
  "passed". A benchmark that cannot fail loudly is not a benchmark.
- The dead-ring optimization was framed wrongly **twice** before it was framed
  right. It was not "reduce collision-mask vertices" and it was not "add a
  convex fast path". It was "prune holes too small to hold the smallest part,
  as early as possible" — and that targets the NFP decomposition, not the mask.

### Instrumentation that lied, three times

Every one of these shipped a *number* that was confidently wrong. In each case
the code being measured was fine; the measurement was not.

1. `set_dead_ring_profiles` zeroed the counters once per **layout**, but all the
   decomposition work happens in generation 1. The report therefore always
   described the last, empty layout and printed `polys=0 rings=0 dropped=0`.
2. With the reset moved to run level, the four counters still lost writes,
   because `d[k] += v` is a read followed by a store and eight rotation workers
   execute the NFP path concurrently. This one was caught only because the
   numbers were *impossible*: `dropped=1048 > seen=1042`, and dropped is a
   subset of seen by construction.
3. My first harness for #2 could not detect the bug it was written for. A plain
   unlocked increment loop across 8 threads lost **zero** of 96,000 updates — a
   dict read and store are nanoseconds apart against a 5 ms switch interval. The
   negative control had to force the window open with `time.sleep(0)` before it
   reproduced the inversion.

---

The rest of this document is the historical record, in the order the work was
performed.

## Historical opening: NFP precalculation

At the start of this investigation, the bottleneck was believed to be **NFP**
precalculation (No-Fit Polygon), not NFB. The implementation eagerly calculated a
large Cartesian product of part types and rotations before nesting started.

## Findings from the first phase-timing run

The supplied run used three part types, eight rotations, and produced NFPs with the following complexity:

| Part pair | Convex pieces | Convex pairs | Typical total time | Convex-sum time |
|---|---:|---:|---:|---:|
| Spacer → Spacer | 192 × 192 | 36,864 | 92–97 s | 90–95 s |
| Spacer → Bottle Top | 192 × 26 | 4,992 | 11–14 s | 10.9–13.1 s |
| Spacer → Bottle Bottom | 192 × 22 | 4,224 | 10–12 s | 9.9–11.2 s |
| Bottle Top → Spacer | 26 × 192 | 4,992 | 12–13 s | 11.7–12.8 s |
| Bottle Top → Bottle Top | 26 × 26 | 676 | 1.5–2.1 s | 1.4–2.0 s |
| Bottle Bottom → Spacer | 22 × 192 | 4,224 | 10–11 s | 10.1–10.7 s |
| Bottle Bottom → Bottle Top/Bottom | 22 × 26 / 22 × 22 | 572 / 484 | 1.2–1.7 s | 1.1–1.6 s |

The important conclusion is that `convex_sum` is the overwhelming bottleneck:

- For the 192 × 192 case, `convex_sum` takes approximately 90–95 seconds out of 92–97 seconds.
- For the 192 × 26 and 26 × 192 cases, it takes approximately 11–13 seconds out of 11–14 seconds.
- `union` is usually only about 20–230 ms for the smaller pairs and 1.5–1.8 seconds for the 192 × 192 pair.
- `decompose` is generally below a few milliseconds after the first cache population, with occasional first-use values around 250 ms.
- `prepare`, `transform`, `assemble`, and `discretize` are not meaningful optimization targets at this scale.
- `holes_ifp` is usually small, although it reaches roughly 100–215 ms for Spacer → Bottle Top/Bottom. It is not the primary problem.

The run also shows that nesting itself completed in only 1.47 seconds with 48 cache hits and no cache misses. Almost all elapsed time was therefore spent in the eager precomputation phase. This makes unused-job elimination just as important as making each NFP faster.

### Revised priority from the evidence

1. **Stop calculating NFP jobs that are not needed.**
2. **Replace or substantially improve the convex Minkowski-sum loop.**
3. **Only then optimize decomposition, unions, or threading.**

The timing data does not justify prioritizing `unary_union`, discretization, or Python startup/compiler changes.

### Demand-driven calculation implemented

The GA coordinator now uses the existing lazy cache path instead of eagerly
precomputing every part-pair and rotation combination. NFPs are calculated by
the placement engine only when a candidate evaluation requests a missing cache
entry, then retained in `Shape.nfp_cache` for later placements and runs.

This removes the up-front Cartesian-product cost. It does not yet reduce the
cost of an individual NFP miss; the next target remains the `convex_sum` phase.
The next retest should record:

- How many NFP cache misses occur.
- Which part-pair/angle keys are actually requested.
- Total nesting time compared with the previous eager run.
- Whether concurrent placement evaluation produces duplicate calculations for
  the same missing key.

### Results from the lazy-run retest

The lazy change worked as intended:

- The multi-minute up-front precompute disappeared.
- Total nesting time was **58.92 seconds**.
- There were **44 NFP cache misses** and **4 cache hits**.
- The summed NFP compute time was **455.047 seconds**, but this is the sum
  across concurrent rotation evaluations; wall-clock nesting time was 58.92
  seconds.
- Packing efficiency remained **47.3%**, matching the previous run.
- The slowest placements were `Bottle Bottom_1` at 26.52 seconds and
  `Bottle Top_1` at 25.64 seconds.

The lazy path avoided the especially expensive `Spacer → Spacer` jobs from
the earlier run. That is the main reason the runtime fell from several
minutes to under one minute. It also confirms that the cache and placement
behavior remain functionally consistent for this test.

However, this particular nest still requested all eight rotation angles for
the expensive mixed pairs:

- `Spacer → Bottle Top`: 8 NFPs × approximately 24–25 seconds each.
- `Spacer → Bottle Bottom`: 8 NFPs × approximately 21–22 seconds each.

The phase breakdown remains consistent:

- `convex_sum` accounts for almost all of every NFP miss.
- `union`, decomposition, and hole/IFP processing are secondary.

The next optimization should therefore target the convex Minkowski
calculation, not further cache administration. Lazy calculation should remain
in place because it removes entire pair/angle groups when they are not needed.

### Linear convex Minkowski implementation ready for manual testing

The current Python implementation now uses a linear edge-vector merge for
convex polygon Minkowski sums. The previous `MultiPoint` cross-product and
`convex_hull` implementation remains as a fallback for unexpected or invalid
inputs.

Focused runtime checks against the reference implementation produced:

| Convex vertices | Linear implementation | Reference implementation |
|---|---:|---:|
| 26 × 26 | 0.3 ms | 13.9 ms |
| 192 × 26 | 0.6 ms | 95.9 ms |
| 192 × 192 | 0.8 ms | 638.2 ms |

The checks also confirmed matching polygon areas to approximately `1e-10` and
matching symmetric differences for representative convex polygons. These are
standalone geometry checks rather than a complete FreeCAD nest, so the manual
test should verify the full NFP output, placement validity, packing efficiency,
and runtime.

### Results from the linear-sum retest

The full FreeCAD nest completed successfully with no visible nesting problem:

- Total time: **25.48 seconds**.
- Previous lazy implementation: **58.92 seconds**.
- Wall-clock improvement: approximately **2.31× faster**, or **56.8% less
  time**.
- Packing efficiency: **47.3%**, unchanged.
- Sheets and placed parts: **1 sheet, 5 parts**, unchanged.
- NFP cache: **45 misses**, **3 hits**.
- Summed NFP compute time: **189.712 seconds** across concurrent rotation
  evaluations.

Representative NFP timings improved as follows:

| NFP pair | Previous convex-sum range | New convex-sum range |
|---|---:|---:|
| Spacer → Bottle Top, 192 × 26 | 24.0–24.6 s | 9.1–9.7 s |
| Spacer → Bottle Bottom, 192 × 22 | 20.3–21.3 s | 8.0–8.4 s |
| Bottle Top → Bottle Top, 26 × 26 | 1.25–3.49 s | 1.25–1.40 s |
| Bottle Bottom → Bottle Bottom, 22 × 22 | 1.07–2.53 s | 0.83–1.02 s |

The linear algorithm therefore produced a clear end-to-end improvement while
preserving the observed packing result. It also confirms that the remaining
runtime is still dominated by `convex_sum`.

One additional issue is now visible: the same cache key
`('Bottle Top', 'Bottle Bottom', 90.0)` appears twice during concurrent
evaluation. This indicates duplicate in-flight NFP calculations. The next
target should be an in-flight cache/future mechanism so that concurrent
threads share one calculation for a missing key instead of repeating it.

### In-flight NFP deduplication implemented

NFP calculation now maintains a shared in-flight `Future` per cache key. The
first thread requesting a missing key becomes the owner and performs the
geometry calculation. Other threads join that Future and receive the same
result instead of recalculating the NFP.

The owner stores successful and geometry-error results in the normal NFP cache,
resolves the Future, and removes the in-flight entry in all cases. Unexpected
exceptions are propagated to waiting callers and do not leave a permanently
stuck key.

### Results from the in-flight deduplication retest

The same-part manual test completed successfully:

- Total time: **23.83 seconds**, down from **25.48 seconds**.
- Improvement from in-flight deduplication: approximately **1.65 seconds**
  or **6.5%** for this run.
- Packing efficiency remained **47.3%**.
- Same result: **1 sheet, 5 parts**.
- NFP cache: **43 misses**, **5 hits**.
- Summed NFP compute time: **178.938 seconds**, down from **189.712
  seconds**.

The log contains explicit join events, including:

```text
NFP in-flight JOIN key=('Bottle Top', 'Bottle Bottom', 0.0)
NFP in-flight JOIN key=('Bottle Top', 'Bottle Bottom', 180.0)
NFP in-flight JOIN key=('Bottle Top', 'Bottle Bottom', 315.0)
```

There is only one phase-timing entry for each joined key, confirming that
duplicate concurrent calculations were eliminated. The change preserved the
nesting result and produced a measurable but workload-dependent improvement.

### Decomposition statistics from the latest retest

The added PERF fields identify where the Spacer's 192-piece decomposition
comes from:

| Part | Source vertices | Delaunay triangles | Clipped pieces | Retained convex pieces |
|---|---:|---:|---:|---:|
| Spacer | 42 | 289 | 10 | 192 |
| Bottle Top | 12 | 30 | 0 | 26 |
| Bottle Bottom | 12 | 26 | 0 | 22 |

The important conclusion is that clipping is not multiplying the Spacer
decomposition. Only 10 clipped pieces were generated; the 192 retained
pieces are primarily the in-boundary Delaunay triangles. The 42 source
vertices nevertheless produce 289 unconstrained Delaunay triangles, of which
192 contribute usable convex pieces.

The latest run also confirms the remaining cost distribution:

- Spacer × Bottle Top: 4,992 convex pairs and about 9.1–9.4 seconds in
  `convex_sum` per rotation.
- Spacer × Bottle Bottom: 4,224 convex pairs and about 7.8–8.2 seconds in
  `convex_sum` per rotation.
- Bottle Top × Bottle Top: 676 pairs and about 1.1–1.5 seconds.
- Bottle Bottom × Bottle Bottom: 484 pairs and about 0.75–1.0 seconds.
- Total wall time: **23.97 seconds**, with the same **47.3%** packing
  efficiency and **1 sheet, 5 placed parts** result.

This makes reducing the number of convex pieces the next worthwhile
algorithmic target, but it should be approached conservatively. Replacing the
triangulation with an arbitrary under-cover decomposition could miss
collisions. The likely experiment is an opt-in adjacent-convex-piece merge
pass that only accepts a merged polygon when it remains convex and the union
area is preserved. The merge must be validated against the current
decomposition for coverage, NFP validity, and nesting output. If it reduces
the 192-piece Spacer substantially, it can remove work quadratically from
Spacer × Spacer and linearly from Spacer × Bottle Top/Bottom. If it does not,
the next fallback is to optimize the convex-piece loop and GEOS union rather
than change decomposition semantics.

### Greedy convex-piece merging rejected

The greedy Shapely merge experiment was reverted after the retest. Although it
reduced the Spacer from 192 to 68 pieces, it increased total wall time from
23.97 seconds to 44.58 seconds because repeated union and convex-hull checks
added approximately 39–41 seconds to decomposition. The `merged` PERF field
remains available and reports zero for the restored baseline.

Decomposition now uses in-flight coordination: concurrent requests for the
same polygon share one decomposition Future, analogous to NFP in-flight
deduplication. This avoids duplicate decomposition work without changing the
geometry or adding an expensive merge pass.

### Decomposition in-flight deduplication retest

The restored baseline and decomposition coordination performed as expected:

- Total wall time: **24.04 seconds**, effectively unchanged from the previous
  **23.97-second** baseline (within normal run-to-run variation).
- Packing efficiency: **47.3%**.
- Result: **one sheet, five placed parts**.
- NFP cache: **44 misses and 4 hits**, matching the earlier baseline.
- Spacer decomposition: **192 pieces**, with **4,992** pairs against Bottle
  Top and **4,224** pairs against Bottle Bottom.
- PERF output reports `merged=0x0`, confirming the expensive greedy merge is
  disabled.
- Spacer → Bottle Top `decompose` time is approximately **37–116 ms**, not
  tens of seconds.

This confirms that decomposition in-flight coordination is safe but provides
little wall-time improvement for this workload because decomposition was
already cached quickly. The next optimization should not spend more effort on
decomposition synchronization.

### Rotated convex-piece caching implemented

Rotated and reflected convex-piece lists are now cached by source geometry,
angle, reflection state, and transform origin. This avoids repeating Shapely
rotation/scaling work when the same shape transform is used by multiple NFP
calculations or IFP checks. The cache is cleared with the per-run
decomposition cache, so it cannot retain geometry from an earlier nesting
run.

This is expected to be a low-risk, modest optimization because the current
profiles show transformation taking hundreds of milliseconds while
`convex_sum` takes several seconds. The manual retest should confirm that
NFP geometry and packing output remain unchanged and show whether the
transformation phase decreases.

### Rotated convex-piece caching retest

The cache reduced repeated transformation work as expected:

- Total wall time: **23.44 seconds**, versus **24.04 seconds** immediately
  before this change; approximately **0.60 seconds faster (2.5%)**.
- Packing efficiency remained **47.3%**.
- Result remained **one sheet, five placed parts**.
- NFP cache behavior remained **44 misses and 4 hits**.
- Spacer → Bottle Top transform timings were generally reduced from roughly
  140–300 ms to roughly 70–270 ms, with later repeated transforms often near
  zero.
- Bottle Top/Bottle Top and Bottle Top/Bottle Bottom repeated transforms were
  commonly **0.1–2 ms** after cache reuse.

The optimization is worthwhile and safe, but—as expected—`convex_sum` still
dominates the runtime. The next target should therefore investigate reuse of
identical convex-piece Minkowski sums or reducing the number of pair
operations without repeating the expensive decomposition-merge regression.

### Convex-pair reuse instrumentation added

The next experiment is instrumentation-only. Each convex-piece Minkowski
request now records a stable logical key containing both source geometries,
angles, reflection states, and piece indices. PERF output reports per-NFP
requests, unique keys, and repeats. The nesting summary also reports totals
across the run:

```text
Convex pair probe: <requests> requests / <unique> unique / <repeats> repeats
```

No pair-result cache has been added yet. The manual run will determine whether
the same convex-piece pair is recalculated often enough to justify caching.

### Convex-sum phase instrumentation added

The next manual run will break down `convex_sum` into:

- `convex_prepare`: ring extraction, winding normalization, and edge-vector preparation;
- `convex_merge`: linear edge-vector merge;
- `convex_polygon`: construction of the resulting Shapely Polygon;
- `convex_fallbacks` and fallback time.

This is instrumentation-only; the linear Minkowski algorithm and output
geometry are unchanged.

The next run also separates polygon construction from post-construction
checks using `convex_validity`, `convex_empty`, `convex_area`, and reports the
total `convex_points` generated. No geometry behavior is changed.

### Convex-sum phase breakdown result

The instrumentation shows that the linear edge merge itself is not the
bottleneck:

- `convex_prepare`: approximately **1.9–2.5 seconds** for each
  Spacer × Bottle Top NFP and **1.9–2.1 seconds** for Spacer × Bottle Bottom.
- `convex_merge`: approximately **21–26 ms** for the 4,224–4,992 pair NFPs.
- `convex_polygon`: approximately **0.7–1.6 seconds** for those NFPs.
- `convex_fallbacks`: **0** across the run.
- Total wall time: **24.50 seconds**, with unchanged **47.3%** efficiency and
  one sheet/five parts.

The current implementation repeats `_convex_ring_vertices()` and edge-vector
construction for every pair, even though each transformed convex piece is
paired repeatedly. The next implementation should prepare each transformed
piece once before entering the nested pair loop, then pass the prepared
vertices/edges to the linear merge function. This directly targets the
measured `convex_prepare` cost without changing decomposition or geometry.
Polygon construction remains a secondary follow-up target.

### Prepared convex-ring optimization implemented

Convex pieces are now normalized and converted to edge vectors once per
transformed piece before the nested Minkowski pair loop. The pair loop reuses
those prepared rings, while retaining the same linear edge merge, validity
checks, and reference fallback behavior. The existing PERF fields remain
comparable; `convex_prepare` now measures one preparation per piece rather
than repeating preparation for every pair.

### Prepared convex-ring optimization retest

The optimization produced a substantial improvement:

- Total wall time: **13.60 seconds**, down from **24.50 seconds**;
  approximately **10.90 seconds faster (44.5%)**.
- Packing efficiency remained **47.3%**.
- Result remained **one sheet, five placed parts**.
- Convex-pair requests remained **87,584**, with **87,584 unique** and zero
  repeats.
- Spacer × Bottle Top `convex_sum` fell from approximately **7.9–10.1
  seconds** per NFP to approximately **4.7–5.1 seconds**.
- Spacer × Bottle Top `convex_prepare` fell from approximately **2.0–2.5
  seconds** per NFP to approximately **36–101 ms**.
- Spacer × Bottle Bottom `convex_prepare` is now approximately **56–98 ms**.
- `convex_merge` remains small at approximately **21–31 ms**.
- `convex_polygon` is now the largest measured sub-phase, generally about
  **0.6–1.1 seconds** for the large NFPs.
- `convex_fallbacks` remained **zero**.

This validates that preparing each transformed convex ring once was the right
optimization. The next investigation should target Shapely Polygon
construction in the pair loop, while preserving the current prepared-ring
algorithm as the new baseline.

### Polygon post-construction instrumentation retest

The follow-up manual run completed successfully with unchanged nesting output:

- Total wall time: **13.59 seconds**, matching the 13.60-second prepared-ring
  baseline.
- Packing efficiency remained **47.3%**.
- Result remained **one sheet, five placed parts**.
- NFP cache: **43 misses** and **5 hits**.
- Convex-pair requests: **87,584**, all unique, with **0 repeats**.
- No linear-sum fallbacks occurred.

The large NFP timings were representative:

| NFP pair | Convex sum | Polygon construction | Merge |
|---|---:|---:|---:|
| Spacer × Bottle Top (4,992 pairs) | 4.05–5.05 s | 0.90–1.14 s | 26.8–28.5 ms |
| Spacer × Bottle Bottom (4,224 pairs) | 3.63–4.18 s | 0.61–0.68 s | 22.2–23.0 ms |

`convex_validity`, `convex_empty`, and `convex_area` all reported **0.0 ms**
throughout the run. The measured cost is therefore associated with creating
the Shapely polygons, not with the post-construction property checks. Polygon
construction is significant—roughly 15–25% of `convex_sum` for the large
NFPs—but it is not the sole dominant cost.

The next investigation should benchmark an alternative result-construction
path, such as reducing redundant collinear output vertices before creating the
Shapely Polygon or constructing from a lower-allocation coordinate
representation. This should remain instrumentation-first and must compare
polygon area/symmetric difference and full nesting output before being enabled.

### Result-ring redundancy retest

The manual run completed successfully with unchanged nesting behavior:

- Total wall time: **14.34 seconds**. This is slightly above the 13.59-second
  baseline and is consistent with normal run-to-run variation plus the
  measurement pass; the ring analysis itself accounts for only about **0.4
  seconds** across the large NFPs.
- Packing efficiency remained **47.3%**.
- Result remained **one sheet, five placed parts**.
- NFP cache: **44 misses** and **4 hits**.
- Convex-pair requests: **87,584**, all unique.
- No linear-sum fallbacks occurred.

The generated rings contain no useful redundancy:

- `convex_duplicates`: **0** for every NFP;
- `convex_collinear`: **0** for every NFP;
- `convex_max_points`: only **6–7** points per convex result;
- `convex_points`: approximately **29,977** for Spacer × Bottle Top and
  **25,366** for Spacer × Bottle Bottom.

The measurement pass found no duplicate or removable collinear vertices.
Generated convex results contained only **6–7 points** each, while Polygon
construction took approximately **0.6–1.2 seconds** for the large NFPs.
Coordinate cleanup is therefore not a useful optimization for this workload.
The measurement-only instrumentation was removed after this run.

The next investigation should move away from coordinate cleanup and instead
isolate the Shapely construction boundary itself, or evaluate a native
implementation that can retain coordinates in a lower-allocation form until
the later union stage. Any such change must preserve the current prepared-ring
algorithm and be validated against polygon equivalence and the full manual
nest.

### Next investigation plan: Shapely construction boundary

The next experiment should remain instrumentation-first and compare the
current `Polygon(result)` path with one alternate construction path in a
focused benchmark. The benchmark should use representative prepared convex
rings from the real Spacer and Bottle geometries and measure:

1. Coordinate generation time;
2. Shapely Polygon construction time;
3. Result area, validity, and symmetric difference;
4. Total time for a representative NFP;
5. Any change in Python allocation or conversion overhead.

The first candidate should be a direct lower-allocation coordinate path using
the Shapely API already available in the FreeCAD environment. NumPy should
not be introduced unless the benchmark shows that conversion overhead is
lower than the current list-of-tuples path. No production behavior should
change during the benchmark.

If no alternate Python construction path materially improves the result, the
next step should be a small native convex-sum prototype that returns boundary
coordinates, followed by one Shapely construction per pair. That prototype
must be benchmarked separately before considering integration.

### Shapely construction benchmark: initial result

The selected workspace interpreter has Shapely **2.1.2** and NumPy available.
An isolated benchmark compared constructing 5,000 six-point convex polygons
from Python lists with constructing them from already-materialized
`float64` NumPy arrays:

| Input representation | Typical time |
|---|---:|
| List of coordinate tuples | **161–168 ms** |
| Existing `float64` NumPy array | **118–123 ms** |

The array path was approximately **25–28% faster** in this synthetic
construction-only benchmark, with matching area and validity. Converting the
existing list to an array immediately before each construction reduced the
benefit substantially, so the result does not justify adding a conversion
around the current `result` list.

This is evidence that a NumPy-backed result buffer may be worthwhile, but it
is not yet evidence that changing the production loop will improve FreeCAD:
the current edge merge creates a Python list incrementally, and the FreeCAD
embedded Shapely/NumPy versions must be confirmed. The next focused benchmark
should reproduce the complete prepared-ring merge while comparing:

1. the current list append plus `Polygon(list)` path;
2. a preallocated NumPy result buffer plus `Polygon(array[:count])`;
3. geometry equivalence and total pair-loop time.

Only if the complete benchmark remains faster should the NumPy path be added
behind a narrowly scoped implementation change and tested manually in
FreeCAD. If NumPy allocation offsets the construction gain, the native
coordinate-buffer prototype remains the better next direction.

### Complete merge/construction benchmark result

The focused benchmark reproduced the prepared linear edge merge and Polygon
construction over 100,000 representative convex pairs. The geometry results
matched exactly for the comparison sample:

- Equal area: **18.97**;
- Both results valid;
- Symmetric difference area: **0.0**.

The timings were:

| Implementation | Total time |
|---|---:|
| Existing prepared Python tuples plus `Polygon(list)` | **4,034 ms** |
| NumPy result buffer plus `Polygon(array)` | **6,557 ms** |

The NumPy path was approximately **62% slower** when the complete merge loop
was included. The earlier Polygon-only improvement does not survive the cost
of managing a NumPy array from a Python-controlled edge merge. The alternate
path should therefore **not** be integrated into production.

The current prepared Python implementation remains the correct baseline. The
next investigation should be a small native coordinate-buffer prototype that
performs the edge merge outside the Python loop and returns coordinates in a
format Shapely can consume. It should first be benchmarked independently
against the existing implementation; no build-system or production dependency
change is justified until that prototype demonstrates a meaningful end-to-end
gain.

### Native edge-merge prototype: initial result

An isolated C++17 prototype was built with `-O3`. It implements the same
prepared linear edge merge as the production Python code and returns the
resulting boundary coordinates. For 100,000 representative convex pairs:

| Operation | Time |
|---|---:|
| Python edge merge only | **approximately 585 ms** |
| Native C++ edge merge only | **approximately 8 ms** |

The native result matched the Python result for the checked sample:

- **10** boundary points;
- area **22.375**;
- identical coordinates within floating-point representation.

This is a large speedup for the merge loop itself, but it is not yet an
end-to-end NFP improvement. The production logs show that `convex_merge`
already takes only approximately **22–29 ms per large NFP**, while Polygon
construction takes approximately **0.6–1.2 seconds**. Moving only the edge
merge into native code would therefore save little wall time after the
Python/native boundary and coordinate-copy costs are included.

The prototype should not be integrated as an edge-merge-only extension. The
next native experiment must measure a complete path that minimizes boundary
crossings and includes Shapely construction. A viable design would either
return a buffer that Shapely can consume without another per-point Python
conversion, or move more of the pair-result construction into native code.
Until that complete path is benchmarked, the production implementation remains
unchanged.

### Native coordinate-buffer prototype: complete-path result

The next prototype used a small C-compatible shared library rather than the
removed ignored `minkowski_cpp` build. It accepts prepared vertex/edge buffers,
performs the edge merge natively, returns one contiguous `float64` coordinate
buffer, and lets Python construct the Shapely Polygon from that buffer.

For a six-point by five-point representative pair, including the native call,
buffer wrapping, memory release, and Shapely Polygon construction:

| Implementation | 100,000 pairs |
|---|---:|
| Current Python merge plus `Polygon(list)` | **5,985 ms** |
| Native buffer plus `Polygon(array)` | **4,908 ms** |

The native buffer path was approximately **18% faster** for this small-ring
case. Geometry matched:

- area **22.375**;
- both results valid;
- symmetric difference area **0.0**;
- 10 exterior points.

For a larger **192-point by 26-point** prepared-ring pair, including the same
complete path over 10,000 pairs:

| Implementation | 10,000 pairs |
|---|---:|
| Current Python merge plus `Polygon(list)` | **5,670 ms** |
| Native buffer plus `Polygon(array)` | **547 ms** |

That is approximately a **10.4× speedup** in the complete pair path, with
matching area (**326.29914070073556**), validity, point count (**218**), and
zero symmetric difference.

This is promising evidence for the native direction, but it is still an
isolated prototype using synthetic convex rings and a `ctypes` boundary. It
has not been tested against FreeCAD’s embedded Python or the real decomposed
Spacer pieces. The next step is to benchmark the buffer API against exported
prepared rings from the real workload, then decide whether a narrow native
module integration is justified. The removed ignored `minkowski_cpp` tree is
not part of this work and should not be treated as an implementation base.

### Native buffer benchmark with real pair-count shape

The next test used **4,992 convex pairs**, matching the measured Spacer ×
Bottle Top pair count. Each prepared convex ring had 3–9 vertices, matching
the small convex pieces produced by the decomposition rather than using one
artificial 192-vertex ring.

Results:

| Implementation | 4,992 complete pairs |
|---|---:|
| Current Python merge plus Polygon construction | **298.8 ms** |
| Native buffer plus per-pair `ctypes` call and Polygon construction | **335.8 ms** |

The native path was **0.89× as fast** as Python in this representative
pair-size benchmark. Geometry was exactly equivalent:

- maximum area delta: **0.0**;
- maximum symmetric difference area: **0.0**;
- zero invalid Python results;
- zero invalid native results.

This shows that the earlier 10.4× result was specific to unusually large
individual rings, not to the actual decomposed-pair workload. For the real
workload, one native call and allocation per convex pair costs more than the
Python merge loop saves.

The native buffer path should not be integrated in its current per-pair form.
The next and final native prototype worth testing is a batched API that
accepts all prepared pair buffers in one call and returns one packed output
buffer plus offsets. That would amortize `ctypes` calls and allocations while
leaving Shapely Polygon construction in Python. If batching does not beat the
current implementation, native integration should be deferred.

### Batched native buffer prototype: initial result

The batched prototype accepts concatenated prepared vertices and edges plus
offset arrays for all pairs, performs every edge merge in one native call, and
returns one packed coordinate buffer with output offsets. Python then creates
the individual Shapely Polygons from those slices.

For the representative 4,992-pair, 3–9-vertex workload:

- Batched native merge plus all Polygon construction: **152.7 ms**;
- Previous Python merge plus all Polygon construction: **298.8 ms**;
- Approximate speedup: **1.96×**;
- Invalid native results: **0**;
- First result: **5 exterior points**.

This result is materially better than the per-pair native buffer path because
it amortizes the foreign-function call and allocation overhead. The benchmark
has not yet compared every pair's area and symmetric difference, so it is
still a prototype result rather than production evidence. The next validation
must compare all 4,992 outputs against the Python baseline, then test the
batched API with the actual transformed/decomposed rings in FreeCAD's
embedded Python. Only after those checks should the API be considered for a
narrow optional integration.

### Batched native geometry-equivalence validation

The batched output was compared against the Python baseline for all **4,992**
representative pairs:

- maximum area delta: **0.0**;
- maximum symmetric difference area: **0.0**;
- invalid native results: **0**.

The batched prototype therefore passes the isolated geometry-equivalence
check. It has not yet been loaded by FreeCAD's embedded interpreter, and its
input packing still needs to be connected to the real transformed convex
pieces. The next step is an embedded-FreeCAD compatibility test using the
actual NFP preparation path; production integration remains deferred until
that test succeeds.

### Batched native FreeCAD compatibility validation

The batched prototype was loaded successfully inside the supplied FreeCAD
AppImage:

- FreeCAD **26.3.0**;
- embedded Python **3.11.14**;
- NumPy **2.4.6**;
- Shapely **2.1.2**.

It was then run against the actual
`n70-intercooler-spacer-bottle-nesting.FCStd` document using the existing
profile extraction and decomposition functions. The current extraction
settings produced **229 Spacer pieces** and **36 Bottle Top pieces**, or
**8,244 convex pairs**.

The complete pair benchmark was:

| Implementation | 8,244 real FreeCAD pairs |
|---|---:|
| Current Python merge plus Polygon construction | **278.1 ms** |
| Batched native merge plus Polygon construction | **275.8 ms** |

Geometry was exactly equivalent:

- maximum area delta: **0.0**;
- maximum symmetric difference area: **0.0**;
- invalid native results: **0**.

The batched native path is compatible with the FreeCAD embedded runtime and
geometrically correct, but its measured improvement on the real decomposed
workload is only about **0.8%**, within normal benchmark noise. Input packing,
output copying, and Polygon construction dominate once the actual convex
pieces are small. The batched prototype should not be integrated into
production based on this result.

The native investigation is complete for this API shape. A meaningful native
gain would require moving a larger portion of the Shapely/GEOS-facing work
across the boundary, not merely replacing the already-small edge merge.

### Convex-sum residual instrumentation added

The next manual run adds timers around the complete convex-piece pair loop:

- `convex_pair_loop`: total time spent iterating and dispatching all pairs;
- `convex_pair_checks`: time spent evaluating validity, emptiness, and area;
- `convex_pair_append`: time spent appending results to the Minkowski-part
  list.

These values are reported alongside the existing merge, Polygon-construction,
and fallback timings. They will show whether the unexplained remainder of
`convex_sum` is Python loop/dispatch overhead or another post-construction
operation. No geometry or fallback behavior is changed.

### Per-check instrumentation added

The next manual run splits `convex_pair_checks` into:

- `convex_pair_validity`;
- `convex_pair_empty`;
- `convex_pair_area`.

It also reports outcome counts as:

```text
convex_pair_outcomes=<valid>/<non_empty>/<positive_area>
```

The acceptance logic remains unchanged and still falls back to the reference
Minkowski implementation if any result fails the checks. This run is intended
to identify which property access is expensive and confirm whether all
generated convex results pass consistently.

### Convex-sum residual instrumentation result

The manual run completed with the expected nesting result:

- Wall time: **13.62 seconds**;
- packing efficiency: **47.3%**;
- one sheet and five placed parts;
- **44** NFP misses and **4** hits;
- **87,584** convex-pair requests, all unique;
- zero fallbacks.

The new measurements identify the dominant residual cost. For the large
Spacer × Bottle Top NFPs:

| Measurement | Typical time |
|---|---:|
| `convex_sum` | 4.3–4.9 s |
| `convex_pair_loop` | 4.2–4.8 s |
| `convex_pair_checks` | 3.3–3.8 s |
| `convex_polygon` | 0.87–1.06 s |
| `convex_merge` | 26–27 ms |
| `convex_pair_append` | 1.7–1.8 ms |

The pair checks therefore account for roughly **75–80% of the pair-loop
time** and are a substantially better target than the merge loop or native
edge-buffer prototype. The earlier `convex_validity`, `convex_empty`, and
`convex_area` fields showed zero because they were not timing the production
pair-loop property accesses individually; the new aggregate timer confirms
that those Shapely property evaluations are expensive in aggregate.

The next investigation should split `convex_pair_checks` into separate
`is_valid`, `is_empty`, and `area` timings and count the results of each
check. If all prepared linear sums are consistently valid, non-empty, and
positive—as the zero-fallback run suggests—we can benchmark removing or
deferring those per-pair checks while retaining a final NFP-level validation
and the existing fallback path for construction errors.

### Per-property convex-pair check result

The follow-up manual run completed with unchanged nesting output:

- Wall time: **13.68 seconds**;
- packing efficiency: **47.3%**;
- one sheet and five placed parts;
- **45** NFP misses and **3** hits;
- aggregate NFP compute time: **97.5 seconds**;
- zero convex-sum fallbacks.

Every measured convex result passed every acceptance check. The large workloads
reported, for example:

```text
convex_pair_outcomes=4992/4992/4992
convex_pair_outcomes=4224/4224/4224
```

This was consistent across all logged NFPs, including the smaller
`676/676/676` and `572/572/572` workloads. No invalid, empty, or zero-area
result was observed.

The individual timings identify `is_valid` as the clear target. For the
4,992-pair Spacer × Bottle Top NFPs, typical values were:

| Check | Typical time | Share of measured checks |
|---|---:|---:|
| `is_valid` | 2.2–3.0 s | approximately 70–75% |
| `is_empty` | 0.5–0.7 s | approximately 14–18% |
| `area > 0` | 0.35–0.47 s | approximately 9–12% |

The corresponding 4,224-pair Spacer × Bottle Bottom NFPs showed the same
ordering. This confirms that the previous aggregate `convex_pair_checks`
measurement was primarily Shapely validity evaluation, not Python dispatch,
edge merging, or result appending.

The next experiment should therefore use a narrowly scoped fast path that
does not evaluate `is_valid`, `is_empty`, or `area` for every convex pair.
It must retain a final assembled-NFP validity/geometry check and fall back to
the current checked/reference path if that final check fails. The experiment
must compare both the final NFP geometry and the manual nesting result before
any behavior change is enabled by default.

### Fast convex-pair check experiment prepared

The experiment is now wired into the NFP path:

- the initial linear-sum pass skips the three per-pair property checks;
- all constructed pair results are still collected;
- the assembled final NFP is checked for non-empty, valid, positive-area
  geometry;
- if that final check fails, the same NFP is recomputed using the original
  per-pair checks and reference fallback behavior.

The checked behavior remains available through the
`validate_convex_pairs=True` argument to `minkowski_sum`; the NFP engine
invokes the experimental fast path with it disabled. No other callers'
behavior was changed.

In the next manual run, zero per-pair check timings are expected. Any
`Fast convex-pair path rejected final NFP` warning indicates that checked
recomputation was required.

### Fast convex-pair check experiment result

The manual run completed successfully with the fast path enabled:

- total wall time: **8.26 seconds**;
- previous checked-path run: **13.68 seconds**;
- wall-time reduction: approximately **39.6%**;
- aggregate NFP compute: **52.65 seconds**, versus **97.5 seconds**;
- packing efficiency: **47.3%**;
- one sheet and five placed parts;
- **44** NFP misses and **4** hits;
- **87,584** convex-pair requests, all unique;
- zero final-NFP rejection warnings;
- zero convex-sum fallbacks.

For the largest 4,992-pair Spacer × Bottle Top NFPs, the fast path reduced
the typical phase totals from approximately 4.9–5.9 seconds to
2.75–3.33 seconds. The per-pair check timings were all zero as expected:

```text
convex_pair_checks=0.0
convex_pair_validity=0.0
convex_pair_empty=0.0
convex_pair_area=0.0
convex_pair_outcomes=0/0/0
```

The visible nesting result matched the checked-path run exactly in the
reported output: 47.3% efficiency, one sheet, and five placed parts. No
fallback was needed, so the final-NFP guard did not alter the result.

The fast path is therefore a meaningful optimization for this workload and is
**validated and retained**. The later bulk-construction work supplied the
further automated geometry-equivalence evidence (16 NFP-level cases with
symmetric difference 0.0, matching probe counters, and an engine-level nest
with identical placement output). The per-pair checked mode remains available
through `validate_convex_pairs=True` for diagnostics and regression
comparisons.

### Source-placement save/normalize/restore result

The source-placement workflow was manually tested with verbose logging after
the earlier geometry-normalization attempt was reverted. The run confirmed:

- all three selected source parts were processed;
- the nesting completed without errors;
- the original placement-aware workflow produced aligned displayed parts and
  boundaries;
- the final result was **47.3%** packing efficiency, one sheet, and five
  placed parts;
- total wall time was **8.73 seconds**;
- NFP cache activity was **45 misses and 3 hits**;
- all **87,584** convex-pair requests remained unique;
- no convex-sum fallbacks or fast-path rejection warnings occurred.

The source objects' original placements were restored after preparation and
nesting. The originals remained hidden so they do not visually overlap the
generated nested layout. Their visibility can be restored by the user later,
with their original positions and rotations preserved.

The placement correction is therefore accepted as a controller-level
save/identity-normalize/copy/restore workflow rather than a change to the
master geometry transform logic.

### Larger-nest scaling run

A larger manual run was completed with:

- sheet size: **1200 × 600 mm**;
- **2 Spacer** parts;
- **60 Bottle Bottom** parts;
- **60 Bottle Top** parts;
- **8 generations**;
- population size **4**;
- eight rotation steps and the other performance-run settings unchanged.

The run completed successfully:

- **122 parts placed**;
- **2 sheets**;
- final packing efficiency: **40.4%**;
- total wall time: **476.93 seconds** (approximately **7.95 minutes**);
- early stopping after no improvement for five generations.

The log shows that this workload exercises the previously avoided
Spacer × Spacer case. Each of its eight rotation NFPs used **192 × 192 =
36,864** convex pairs and took approximately **16.5–21.7 seconds**. The
eight Spacer × Spacer NFPs therefore account for roughly **171 seconds of
aggregate NFP time**, with the largest individual NFP phase values around
21.7 seconds. This is the dominant one-time geometry cost for the two-spacer
workload.

The smaller mixed and bottle-only NFPs are much cheaper with the current fast
path:

- Spacer × Bottle Bottom: approximately **2.8–3.0 seconds** per requested
  NFP;
- Bottle Top × Spacer: approximately **0.4–0.5 seconds**;
- Bottle Bottom × Spacer: approximately **0.36–0.38 seconds**;
- Bottle Bottom × Bottle Top: approximately **0.06–0.07 seconds**.

The important scaling result is that total wall time is not explained by NFP
generation alone. The initial NFP-heavy phase runs from approximately
09:39:49 to 09:43:04, while the GA continues until 09:49:27. Thus roughly
three minutes of the **7.95-minute** run are spent evaluating the repeated
GA population/generation candidates after the main NFP set has been
calculated. The five-part benchmark does not expose this cost because it used
one generation and one population.

This changes the next optimization priority:

1. instrument per-generation and per-population candidate evaluation time;
2. split candidate time into NFP lookup, collision/validity evaluation,
   scoring, and layout-copy overhead;
3. measure NFP cache hit/miss counts per generation;
4. only then decide whether more NFP geometry optimization or candidate-result
   reuse has the larger payoff.

The current run confirms that the fast convex-pair path remains correct at
larger scale, with no reported convex fallbacks or fast-path rejection
warnings. Because the final result is **40.4%** across two sheets, it should
not be compared directly with the five-part **47.3%** result; the workloads
and sheet geometry differ.

### Convex-pair reuse probe result

The manual run found no pair reuse:

- Total wall time: **23.50 seconds**, versus **23.44 seconds** with the
  transformed-piece cache; the approximately **0.06-second** difference is
  normal run-to-run variation.
- Convex-pair requests: **87,584**.
- Unique logical pair keys: **87,584**.
- Repeated requests: **0**.
- Packing efficiency remained **47.3%**, with **one sheet and five parts**.

Every NFP's `pair_probe` also reported zero repeats, for example
`4992/4992/0` and `4224/4224/0`. A pair-result cache would therefore not help
this workload and would add key-generation, locking, and memory overhead. The
probe should remain instrumentation only; the next optimization should focus
on reducing the cost of each unique linear convex Minkowski sum or reducing
the number of unique pairs through a cheaper geometry-preserving method.

### Convex-piece merge retest: regression

The merge pass did reduce the decomposition size substantially:

| Part | Previous pieces | Merged-run pieces | Previous pairs | Merged-run pairs |
|---|---:|---:|---:|---:|
| Spacer | 192 | 68 | 192 × 26 = 4,992 | 68 × 8 = 544 |
| Bottle Top | 26 | 8 | 26 × 26 = 676 | 8 × 8 = 64 |
| Bottle Bottom | 22 | 7 | 22 × 22 = 484 | 7 × 7 = 49 |

However, the implementation is a net performance regression. The run took
**44.58 seconds**, compared with **23.97 seconds** before merging. The
Spacer → Bottle Top NFPs spent approximately **39–41 seconds in
`decompose`** each, while their `convex_sum` phase fell to only about
0.6–0.9 seconds. This means the quadratic Shapely union/convexity search in
the merge pass costs far more than the convex sums it removes. The concurrent
requests also make this especially expensive because multiple first-time
decomposition calls can perform the costly merge work before the cache is
populated.

Packing behavior was preserved in this run (**47.3%**, one sheet, five parts),
but performance correctness was not. The current greedy merge implementation
should therefore be removed or disabled before further testing. A viable
replacement would need to avoid pairwise geometry unions during every
decomposition, for example by using triangulation adjacency and a bounded,
single-pass merge strategy, or by reducing convex-piece complexity inside the
Minkowski loop instead.

## Compiled options: short answer (historical)

> **Superseded.** This section records the native-code exploration that followed
> the initial convex-Minkowski work. That investigation is now complete: the
> native edge-merge, per-pair buffer, and batched buffer prototypes were all
> benchmarked, and none produced a meaningful end-to-end gain on the real
> FreeCAD workload. See the native prototype sections below and the Current
> status at the top of this document.

- **Recompiling Python itself:** probably little benefit.
- **Using a C/C++ extension for the custom Minkowski code:** potentially significant benefit.
- **Optimizing Shapely/GEOS calls:** useful only if the current workload is mostly inside GEOS; otherwise rewriting the surrounding Python algorithm matters more.
- **Using NumPy/Numba:** useful for numeric array work, but not directly for Shapely geometry operations.

The current implementation is a hybrid:

```text
Python orchestration
  -> Shapely geometry objects
      -> GEOS C/C++ operations
  -> NumPy discretisation
```

Some parts are already compiled, but the custom Minkowski implementation still creates many intermediate Python objects.

## What is already compiled

These operations are generally implemented in native code:

- Shapely polygon intersection and union.
- Shapely triangulation.
- Shapely validity and buffering.
- GEOS convex hull.
- NumPy array operations.
- FreeCAD/Part geometry operations.

Therefore, simply replacing Python with C++ will not accelerate every operation. For example, wrapping this call in C++ would not make much difference:

```python
unary_union(minkowski_parts)
```

The expensive union is already performed by GEOS.

## Best candidate for a faster Python implementation

The strongest target is the custom convex Minkowski sum:


```python
def minkowski_sum_convex(poly1, poly2):
    sum_vertices = []
    for p1 in poly1.exterior.coords:
        for p2 in poly2.exterior.coords:
            sum_vertices.append((p1[0] + p2[0], p1[1] + p2[1]))
    return MultiPoint(sum_vertices).convex_hull
```

This currently involves:

1. Python iteration over every vertex pair.
2. Creation of many Python tuples.
3. Construction of a Shapely `MultiPoint`.
4. A GEOS convex hull.

Before introducing C++ or another compiled module, implement the algorithmic improvement in Python if practical:

- Use the standard edge-angle merge algorithm for convex polygon Minkowski sums.
- Avoid generating every pairwise vertex sum.
- Avoid calling `MultiPoint(...).convex_hull` for every convex-piece pair.
- Keep the result as a polygon boundary and construct Shapely geometry only where required.

The existing code performs a convex sum once for every pair of decomposed pieces. The log shows that this is not a minor Python overhead: tens of thousands of convex-piece operations are being performed for one NFP. A linear convex Minkowski implementation is therefore the highest-value per-NFP optimization.

A C++ implementation could later:

- Represent polygons as contiguous numeric arrays.
- Use the linear-time edge-merging algorithm for convex polygon Minkowski sums.
- Avoid generating every pairwise vertex sum.
- Return only the resulting boundary coordinates.

The algorithmic change could reduce the operation from approximately:

```text
O(V1 × V2) plus convex hull
```

to approximately:

```text
O(V1 + V2)
```

This is more valuable than merely compiling the existing nested loops, because it changes the algorithm as well as the implementation language. A compiled implementation should be considered only after measuring the pure-Python algorithmic version.

## What a useful native module should look like

A useful extension API would operate on plain numeric arrays:

```python
result = minkowski_sum_convex(
    polygon_a_coords,
    polygon_b_coords,
    reflect_b=True,
)
```

The extension should return an `Nx2` NumPy array, after which Python can construct the Shapely polygon only once.

A poor extension API would repeatedly convert between Shapely objects and native representations:

```python
native_minkowski(shapely_polygon_a, shapely_polygon_b)
```

If the extension converts back and forth between Python, Shapely, and GEOS representations repeatedly, much of the benefit may be lost.

## Cython, pybind11, or Rust?

### Cython

A good fit if the implementation stays close to the current Python code and uses typed memoryviews.

Advantages:

- Relatively straightforward integration.
- Good for numeric loops.
- Can release the GIL.
- Easier than a full C++ geometry library.

Disadvantages:

- A compiled module is still required for every supported FreeCAD/Python combination.

### pybind11 C++

A good fit for a more substantial geometry implementation.

Advantages:

- Clean NumPy array interface.
- Easy to release the GIL around independent calculations.
- Suitable for a linear Minkowski implementation.
- Easy to extend later with native convex decomposition or polygon cleanup.

Disadvantages:

- ABI and packaging complexity.
- Must build against the Python version embedded in FreeCAD.

### Rust/PyO3

Technically viable, but probably not the best first choice. The project already depends on FreeCAD, Shapely, GEOS, and NumPy, so Rust would add another build and distribution layer without an obvious advantage over C++.

## Python compiler options

### PGO/LTO Python build

A profile-guided and link-time optimized CPython build may improve Python bytecode execution modestly, often by a low-single-digit to low-teens percentage for Python-heavy workloads.

However:

- FreeCAD embeds its own Python.
- The workbench runs inside that interpreter.
- Replacing or rebuilding FreeCAD's Python is difficult and fragile.
- Most polygon operations are already in GEOS C/C++.

This is not worth pursuing unless profiling shows that a large percentage of runtime is spent in Python bytecode rather than Shapely/GEOS.

### PyPy

Probably not suitable:

- FreeCAD integration is CPython-oriented.
- Shapely, NumPy, and FreeCAD extension compatibility would be concerns.
- The workbench runs inside FreeCAD's embedded Python environment.

### Python 3.13 free-threaded/no-GIL builds

This is unlikely to be an immediate solution:

- FreeCAD must support the interpreter.
- Native extensions must be compatible.
- Shapely, NumPy, and FreeCAD bindings all need compatible builds.
- The expensive work is largely native GEOS work and Python coordination.

A free-threaded build might help some Python-side parallelism, but it would be a large compatibility project rather than a targeted optimization.

### Numba

Numba will not compile code that manipulates Shapely objects, GEOS geometries, or Python dictionaries. It could help with:

- Coordinate transformations.
- Rotation matrices.
- Candidate point generation.
- Numeric scoring.
- Deduplication and grid calculations.

Those portions are already partly vectorized with NumPy, so the expected gain is probably smaller than improving the Minkowski algorithm.

## Compiled versus algorithmically better

Compiling the current implementation literally may provide less benefit than expected.

Moving this unchanged to C++:

```text
for every convex part A:
    for every convex part B:
        generate every vertex pair
        calculate convex hull
```

would reduce Python overhead, but would still perform the same excessive amount of work.

A better sequence is:

1. Avoid precomputing NFPs that are never used.
2. Cache convex decompositions and rotated convex pieces.
3. Replace pairwise vertex generation with a linear convex Minkowski algorithm.
4. Only then move that numeric implementation to Cython or C++.

## Likely optimization split after the timing run

| Area | Likely value |
|---|---:|
| Lazy/on-demand NFP generation | Very high |
| Better convex Minkowski algorithm | Extremely high |
| Reusing decomposition and rotated pieces | Low/medium for this workload |
| In-flight cache deduplication | Low/medium for this workload |
| Cython/C++ implementation of improved numeric Minkowski code | High, but second-stage |
| More Python threads | Uncertain |
| Rebuilding CPython | Low |
| Numba on current Shapely code | Low/medium |
| Free-threaded Python | High effort, uncertain benefit |

## How to decide whether a native module is justified

Measure these categories separately:

```text
Python setup and loops
Shapely/GEOS operations
NumPy/discretisation
```

Add timings around:

- `decompose_if_needed()`
- convex-part rotation
- `minkowski_sum_convex()`
- `unary_union()`
- hole/IFP calculation
- discretisation

The supplied run confirms that `minkowski_sum_convex()` and its surrounding convex-piece loop are a large fraction of runtime. However, the first response should be an algorithmic implementation in Python, not a native rewrite. The current implementation calls `MultiPoint(...).convex_hull` up to 36,864 times for a single Spacer → Spacer NFP.

If `unary_union()` dominates, rewriting that part in C++ will not help much. The options are instead:

- Reduce the number of pieces.
- Reduce polygon complexity.
- Avoid unnecessary unions.
- Use a different geometric representation.
- Improve the decomposition strategy.

The supplied run shows that `unary_union()` does not dominate. Decomposition caching is already effective after the first call, so it is not the first optimization target for this workload.

## Recommended implementation order (historical)

> **Superseded.** The steps below were the plan before the linear Minkowski
> implementation, in-flight deduplication, prepared rings, fast convex-pair
> path, bulk polygon construction, and bounding-box/sheet-difference
> optimizations were implemented and validated. The current next steps are in
> the Current status section at the top.

1. **Make precalculation lazy or demand-driven.** The run precomputed many expensive NFPs even though only five parts were ultimately nested. Confirm the exact requested-key set after switching to lazy generation.
2. **Implement and test a linear convex Minkowski sum in Python.** This directly targets the 90–95 second phase.
3. **Benchmark the new implementation against representative pairs**, especially 192 × 192, 192 × 26, and 22 × 22.
4. **Retain the phase timings and add geometry-equivalence tests** comparing area, bounds, validity, and placement outcomes against the current implementation.
5. Cache rotated convex pieces if profiling shows the transformation phase grows after the convex-sum improvement.
6. Add in-flight calculation deduplication if lazy generation causes concurrent duplicate requests.
7. Benchmark worker counts only after the amount of per-NFP work has been reduced.

Do not start by increasing the thread count. The expensive operations are GEOS geometry operations and large temporary allocations, so more threads may make the total time worse.

### Expected payback

The largest likely immediate saving is from demand-driven generation. In the supplied run, the actual nesting phase needed only cached entries and completed in 1.47 seconds, while the precomputation phase ran for several minutes. Avoiding unused pair/rotation combinations can therefore remove entire 10–95 second jobs.

For NFPs that are genuinely needed, the next largest saving is the convex-sum algorithm. The 192 × 192 case has 36,864 convex-piece pairs, so reducing each pair from “all vertex-pair sums plus a GEOS convex hull” to a linear convex-polygon merge should have a much larger effect than optimizing any surrounding phase.

## Practical recommendation (historical)

> **Superseded.** This was the pre-native-prototype recommendation. The native
> prototype was subsequently built and rejected on the real workload, and the
> algorithmic Python optimizations were validated and retained. See the Current
> status section at the top for the present plan.

Do not start by rebuilding Python. First implement a small native prototype for:

```text
convex polygon + reflected convex polygon
    -> resulting convex Minkowski boundary
```

Use NumPy arrays and a released-GIL C++ function, then benchmark it against the current `MultiPoint(...).convex_hull` implementation using representative real parts.

If the prototype is substantially faster and preserves geometry, integrate it behind a fallback:

```python
try:
    from ._fast_minkowski import minkowski_sum_convex_fast
except ImportError:
    minkowski_sum_convex_fast = None
```

This keeps source installations and unsupported FreeCAD/Python platforms functional while allowing packaged builds to use the accelerated implementation.

## GA candidate-evaluation instrumentation

The large 122-part run showed a substantial interval after the main NFP-heavy
phase, so instrumentation was added to separate repeated GA work from one-time
NFP generation. With verbose logging enabled, the GA now reports per-generation
timings and a final aggregate line containing:

- generation and layout-evaluation counts;
- total layout nesting time;
- NFP computation time and NFP cache hits/misses;
- exact candidate-validity/collision-check time;
- candidate scoring time;
- layout-management time for cloning, deletion, and next-generation creation;
- offspring and immigrant layout counts;
- rotation, candidate, and valid-candidate counts.

The nesting path reports these values through a callback without changing the
existing `nest()` return shape. This keeps the instrumentation diagnostic-only
and avoids affecting normal layout behavior.

### First instrumented large-run retest

The retest completed with the same 122-part workload and produced:

- 494.84 seconds total;
- 40.4% efficiency;
- 2 sheets and 122 placed parts;
- the same fast-path result: no convex-pair validation or fallback work.

The detailed NFP records confirm that Spacer × Spacer remains dominant:
approximately 17.4–22.3 seconds per rotation, with `convex_polygon` usually
around 14.6–18.3 seconds of each NFP. The eight Spacer × Spacer rotations
therefore account for roughly 170 seconds.

The log did not contain the expected `[GA PERF]` or `[GA PERF TOTAL]` records,
so it does not yet separate GA candidate evaluation from layout-management
time. The final aggregate record is now emitted whenever the GA probe is
active, rather than being suppressed by the verbose flag. A further run is
required to obtain the candidate-evaluation breakdown.

### GA breakdown retest

The subsequent retest produced the first complete aggregate breakdown:

```text
generations=527.97s
layouts=19
nesting=497.59s
nfp_compute=229.25s
validity=1375.20s
score=0.22s
layout_management=76.91s
rotations=3385
candidates=453297
valid_candidates=435545
nfp_hits=26034
nfp_misses=82
```

The run completed with 122 parts placed on two sheets at 40.4% efficiency.
The 19 layout evaluations correspond to the initial population and generated
offspring/immigrants over the generations that ran before early stopping.

The main actionable result is that candidate validity/collision checking is
now the leading repeated-evaluation cost. The `validity` value is the sum of
timings from parallel rotation workers, so it is CPU-time-like aggregate work,
not wall-clock time. It still clearly outweighs candidate scoring, which took
only 0.22 seconds in aggregate. Layout management was also measurable at
76.91 seconds, while NFP computation accounted for 229.25 seconds of the
nesting work.

This shifts the next optimization target from score calculation to
`PlacementOptimizer._exact_candidate_mask()`. The likely high-value
investigation is reducing the number of candidate-by-polygon Shapely
operations, while preserving the exact collision check required after NFP
candidate generation. The existing bounds mask should remain the first
filter; subsequent work should measure the contribution of candidate
deduplication, bounding-box rejection, and polygon intersection checks before
changing behavior.

## Candidate-validity stage instrumentation

The next probe separates exact candidate validation into:

- candidates surviving the sheet-bounds mask;
- candidates entering exact sheet-boundary checks;
- candidates rejected by `candidate.difference(bin_polygon)`;
- candidates entering existing-part collision checks;
- candidates rejected by polygon intersection;
- total existing-polygon intersection checks, including short-circuiting;
- aggregate time for candidate translation, sheet difference, and collision
  intersection operations.

These values are emitted in `[GA PERF TOTAL]` as
`candidate_geometry`, `sheet_difference`, `collision_intersection`, and the
corresponding candidate/check counters. The geometry behavior is unchanged;
the probe preserves the existing early exit on the first detected collision.

### Candidate-validity breakdown retest

The next 122-part run completed in 515.90 seconds with 40.4% efficiency:

```text
generations=544.37s
layouts=19
nesting=515.90s
nfp_compute=230.71s
validity=1405.20s
score=0.24s
candidate_geometry=73.97s
sheet_difference=142.03s
collision_intersection=1168.00s
layout_management=77.83s
rotations=3390
candidates=455643
valid_candidates=437884
bounds_survivors=455643
sheet_candidates=455463
sheet_rejections=0
collision_candidates=455463
collision_rejections=17759
polygon_checks=20161861
nfp_hits=26300
nfp_misses=84
```

This identifies existing-part collision testing as the clear next bottleneck:
`collision_intersection` consumed 1,168 seconds of aggregate worker time,
compared with 142 seconds for sheet-boundary differences and 74 seconds for
candidate polygon translation. There were approximately 20.2 million
existing-polygon intersection checks. The sheet-boundary check rejected no
candidates; 17,759 candidates were rejected by existing-part collisions.

The timings are summed across parallel rotation workers, so they are not
wall-clock durations. They nevertheless establish the optimization order:
reduce the number or cost of candidate-versus-existing-polygon checks before
changing candidate scoring or NFP generation. A spatial prefilter based on
prepared polygon bounds, followed by exact intersection only for bounding-box
overlaps, is the next experiment to measure. It must preserve the existing
area-tolerance semantics and collision short-circuit behavior.

## Bounding-box collision prefilter

Implemented the next experiment in `_exact_candidate_mask()`. Each translated
candidate now compares its bounds with each existing polygon before invoking
Shapely `intersection()`. Non-overlapping bounding boxes skip the exact
intersection; overlapping boxes retain the existing area-tolerance test and
early collision exit. The `[GA PERF TOTAL]` output now includes
`bbox_rejections`, allowing the reduction in exact checks to be measured.

### Bounding-box prefilter retest

The large 122-part retest completed with the same result:

- **219.04 seconds** nesting time;
- **40.4% efficiency**;
- **2 sheets** and **122 placed parts**.

Compared with the immediately preceding run without the prefilter:

| Metric | Before | After |
|---|---:|---:|
| Nesting time | 515.90 s | 219.04 s |
| Aggregate validity | 1405.20 s | 394.19 s |
| Collision intersection time | 1168.00 s | 152.07 s |
| Exact polygon checks | 20,161,861 | 555,217 |
| Bounding-box skips | not applicable | 19,546,376 |
| Collision rejections | 17,759 | 18,613 |

The prefilter removed approximately **97.2%** of exact polygon checks and
reduced wall-clock nesting time by approximately **57.5%**, while preserving
the packing result. NFP computation remained similar at approximately
234 seconds, so it is now comparable to—and slightly larger than—the
remaining GA candidate-evaluation cost. This optimization is therefore
validated and should be retained.

## Performance Logging UI control

Added a `Performance Logging` checkbox directly to the right of
`Verbose Logging`. Its preference is persisted as `PerformanceLogging`.
Performance and timing records—including NFP `[PERF]` output, per-part
`[TIMING]` output, convex-pair probes, candidate-validation timing, and
`[GA PERF]` summaries—are now gated by this new checkbox. `Verbose Logging`
continues to control detailed algorithm diagnostics independently.

The first manual UI test exposed and corrected a wiring defect where the
performance flag reached `Nester` but not its `PlacementOptimizer`, causing
rotation worker failures before nesting could start. The flag is now passed
explicitly to both objects.

## Sheet-difference elimination

The bounding-box prefilter still constructed a translated Shapely candidate,
ran `candidate.difference(bin_polygon)`, and measured its area for **every**
bounds survivor. In the 122-part workload that cost 142.03 seconds of
aggregate worker time even though it rejected **zero** candidates.

`_exact_candidate_mask()` now skips two redundant geometry steps without
changing acceptance:

1. The GEOS sheet-difference check only runs for candidates whose bounding
   box actually crosses the sheet boundary. A polygon is always contained in
   its own bbox, so a candidate whose bbox lies wholly inside the sheet
   rectangle provably has an empty difference; the bounds pre-filter already
   guarantees the inside case. Candidate bboxes are computed as exact
   arithmetic on the known rotated extents — `points + rmin/rmin/maxx/maxy` —
   with no Shapely geometry or GEOS call.
2. Translated candidate geometry is constructed **lazily**, only when it is
   needed: candidates whose bbox crosses the boundary or whose bbox overlaps
   an existing part's bbox. The numpy bbox-overlap screen is still fully
   vectorized (chunked over rows), and the existing-part iteration order,
   the collision short-circuit, `area_tolerance`, and bbox-rejection
   accounting are preserved.

A new `sheet_boundary_candidates` probe counts only the candidates that
actually entered the difference path.

### Sheet-difference elimination retest

The 122-part manual run completed successfully with the same result:

- **190.37 seconds** nesting time (previous prefilter run: 219.04 seconds;
  not strictly comparable across runs because of GA randomness and early
  stopping — this run fired "no improvement for 5 generations").
- **40.4% efficiency**, **2 sheets**, **122 placed parts** — unchanged.
- `sheet_difference`: **142.03 s → 0.00 s** (aggregate worker time).
- `sheet_rejections`: **0**.
- `sheet_boundary`: **0** — the boundary probe confirms that in this
  workload no candidate bbox crosses the sheet boundary, so the
  difference path genuinely never needed to run.
- `bbox_rejections`: **19,750,635** — the same ~19.5M scale as the prior
  prefilter run, confirming the vectorized overlap screen is still doing
  the same filtering work.
- `polygon_checks`: **558,029** (prior run: 555,217), `collision_rejections`:
  **18,837** (prior: 18,613) — the exact-collision path is unchanged.
- `candidate_geometry`: **95.76 s**, `collision_intersection`: **203.44 s**
  aggregate. Both drifted slightly from the prior run's 73.97 s / 152.07 s;
  candidate geometry is still constructed for nearly every candidate because
  the sheet is densely packed (122 parts), and the totals are summed across
  parallel rotation workers. The definitive saving is the sheet-difference
  work, which is now zero.

This optimization is validated and should be retained. The remaining
repeated-evaluation costs are `collision_intersection` (203.44 s aggregate)
and `candidate_geometry` (95.76 s), plus `nfp_compute` (224.78 s) which is
dominated by the Spacer × Spacer convex-polygon construction.

### Bulk convex-pair polygon construction

The remaining NFP cost is the Spacer × Spacer `convex_polygon` phase. For each
pair of convex pieces the code constructed one Shapely `Polygon()` object per
pair (36,864 pairs for the Spacer × Spacer NFP). Under the GA's concurrent
rotation workers those per-pair calls serialize on the GIL: 8 concurrent
Spacer × Spacer NFPs took **27.07 s wall** with ~20.8 s per worker spent in
`convex_polygon_create`.

Change (in `minkowski_utils.py`):

- The pair merge now produces raw open rings (`_minkowski_merge_ring`, pure
  Python arithmetic, no Shapely geometry per pair) for every convex pair.
- All rings are then built in **one** `shapely.from_ragged_array(
  GeometryType.POLYGON, coords, offsets=(ring_idx, poly_idx))` call, which
  releases the GIL, instead of one `Polygon()` per pair.
- `validate_convex_pairs` semantics are preserved: the checked path gathers
  the per-pair validity/empty/area counters over the bulk-built polygons, and
  any ring that cannot form a polygon (merge exception or fewer than 3
  vertices) is routed to `_minkowski_sum_convex_reference` exactly as before.
- `pair_keys`, `convex_pair_loop_ms`, `convex_pair_append_ms`,
  `convex_fallbacks`, `convex_fallback_ms` and the probe totals are kept
  intact; `minkowski_parts` is still a Python list, and union order does not
  affect the final NFP.

Validation (FreeCAD 26.3.0 embedded runtime, real Spacer/Bottle pieces):

- **NFP-level equivalence** (`bench_equiv.py`): 16 cases — every pair family
  (Spacer × Spacer at 0°/17°/−17°/201.5°, Spacer × Bottle Top/Bottom, Bottle
  Top × Bottle Bottom, Bottle Top × Bottle Top) × `validate_convex_pairs`
  on/off — all produce **symdiff 0.0** vs the legacy per-pair loop, with
  identical pair counts and fallback counts.
- **Production-path concurrency** (`bench_concurrent.py`, 8 threads):
  - before: wall **27.07 s**, `convex_polygon_create` ≈ 20,800 ms/thread;
  - after: wall **10.90 s** (**2.48×**), `convex_polygon_create` ≈ 200–820
    ms/thread (a ~97% reduction).
  - single-thread: 4.72 s → 3.11 s.
- **Engine-level `nest()`** (deterministic seed 7777, fixed direction, 1
  Spacer + 1 Bottle Top + 1 Bottle Bottom): old vs new produce the **same
  2 sheets, 3/3 placed, identical sheet areas (110,563.6 / 5,040.0) and equal
  probe counters** (candidates 24, polygon_checks 13, collision_rejections 0)
  while `nfp_compute` drops 33.33 s → 21.29 s single-threaded.

The definitive win is the Spacer × Spacer NFP, whose per-worker construction
cost was the dominant `convex_polygon` line. Bulk `from_ragged_array`
construction keeps the exact same accept/reject semantics while eliminating
the GIL serialization.

The residual Spacer × Spacer union was investigated next. The earlier
52,441-input estimate was stale: the current real workload has
**192 × 192 = 36,864** convex-pair polygons, as confirmed by both the PERF
record and the isolated benchmark.

### Unary-union diagnostics and isolated benchmark

The latest 122-part Performance Logging run reported:

- `nesting=163.93 s`;
- `nfp_compute=79.16 s` aggregate worker time;
- `validity=313.48 s` aggregate worker time;
- `collision_intersection=191.33 s` aggregate worker time;
- `candidate_geometry=89.03 s` aggregate worker time;
- `layout_management=72.75 s`;
- unchanged result of **40.4% efficiency**, two sheets, and 122 placed parts.

The eight Spacer × Spacer union records ranged from approximately **1.94 s to
2.60 s** each and summed to **18.62 s**. That sum is aggregate time from
parallel rotation workers, not additive wall time.

Temporary Performance Logging diagnostics were added around the union. They
report, outside the timed union interval:

- input polygon and coordinate counts;
- invalid and empty input counts;
- summed input area and aggregate input bounds;
- result type, validity, area, bounds, polygon/ring counts, and coordinates;
- result/input area ratio;
- WKB size and a short SHA-256 digest.

A representative production-path 0° NFP reported:

```text
union=1767.6ms union_diag=69.0ms
union_inputs=36864p/258004xy invalid=0/empty=0
input_area=114064675
union_result=Polygon 1p/1r/188xy valid=True
area=838893.478 ratio=0.00735454
```

Diagnostic collection cost approximately **69 ms** in that case and is only
enabled when NFP timings are requested by Performance Logging. It is charged
to `union_diag_ms`, not `union_ms`, and does not change production geometry.

The isolated benchmark used the real Spacer fixture, the saved manual-run
settings (spacing 4.0 mm, deflection 0.1 mm, simplification 1.0), all eight
45-degree rotations, FreeCAD 26.3.0, Shapely 2.1.2, and NumPy 2.4.6. Each
rotation used 36,864 real convex-pair polygons.

| Union method | Samples | Mean | Range |
|---|---:|---:|---:|
| Current `ops.unary_union(list)` | 16 | **1,634.4 ms** | 1,502.5–1,741.5 ms |
| `shapely.union_all(list)` | 16 | **1,633.9 ms** | 1,496.5–1,753.2 ms |
| `union_all(numpy_object_array)` | 16 | **1,624.8 ms** | 1,496.0–1,787.4 ms |

All three methods produced geometrically equivalent results, with symmetric
difference area **0.0** for every rotation. `union_all` and the NumPy object
array provided no meaningful improvement over the current call; the small
mean differences were within normal benchmark variation.

Across the eight rotations:

- every input set contained **36,864 polygons**;
- input sets contained approximately **258,000 coordinates**;
- no input polygon was invalid or empty;
- every result was one valid Polygon with one ring;
- results contained only **98–189 coordinates**;
- the result/input summed-area ratio was approximately **0.00735–0.00808**.

The convex-pair polygons therefore overlap heavily: the final union retains
less than approximately 0.81% of their summed area.

Concurrency was measured separately using all eight real angle workloads:

| Concurrent unions | Wall time | Aggregate worker time | Effective concurrency |
|---:|---:|---:|---:|
| 4 | **5.270 s** | **17.925 s** | **3.40×** |
| 8 | **4.730 s** | **35.037 s** | **7.41×**, with CPU oversubscription |

The machine exposes four CPUs. The four-worker aggregate result,
**17.925 s**, closely matches the live run's **18.617 s** union aggregate.
This demonstrates that the GEOS union is **not serialized under the GIL**.
Eight workers on four CPUs increase each worker's elapsed time substantially
while producing only a modest improvement in overall wall time.

### Unary-union optimization conclusion

Further low-risk optimization of `unary_union()` has limited value for this
workload:

- the eight Spacer × Spacer unions cost approximately **5.3 s of practical
  wall time**, not the 18–20 s suggested by their aggregate sum;
- the complete nesting phase took approximately **164 s**;
- eliminating the union completely would therefore save at most about
  **3.2%** of nesting wall time;
- making it twice as fast would save about **1.6%**;
- `union_all`, NumPy input, and the current GEOS call are effectively
  equivalent in both performance and geometry;
- increasing concurrency beyond the available CPU count mostly adds contention.

A fundamentally different NFP algorithm could reduce the 36,864 highly
overlapping inputs, but that would be a major geometry and regression-risk
project rather than a small union optimization. The current union path should
therefore be treated as adequately optimized and no longer a priority.

Optimization should move to the repeated candidate-evaluation work—especially
`collision_intersection`, overall candidate validity, and
`candidate_geometry`—with the understanding that these PERF values are summed
across parallel workers and must not be interpreted as wall-clock durations.

### Candidate-key reuse measurement correction

The subsequent 122-part Performance Logging run completed with the expected
result: **40.4% efficiency**, **2 sheets**, and **122 placed parts**. It
reported:

```text
nesting=157.43s
candidate_wall=313.31s aggregate
candidate_geometry=90.21s aggregate
collision_intersection=189.02s aggregate
candidate_geometries=444223
bbox_checks=20226461
bbox_overlap_pairs=551212
exact_collision_checks=550329
candidate_points=450244
candidate_geometry_unique=151632
candidate_geometry_repeats=298612
```

The bbox prefilter is already highly selective: approximately 95.7% of bbox
comparisons were rejected, and the exact-check count is almost the same as the
number of surviving overlap pairs. The initial key-reuse counters were
misleading, however: they observed every candidate point rather than only rows
that actually constructed a translated polygon.

The diagnostic was therefore refined to count only the rows selected by the
lazy geometry path. It also now uses a GA-run-scoped key tracker, allowing
reuse to be measured across layouts rather than only within one optimizer. The
new fields are:

```text
geometry_key_observations
geometry_unique
geometry_repeats
```

These counters are measurement-only and do not cache polygons, alter candidate
ordering, change collision short-circuiting, or modify area-tolerance
semantics. At the time of this run, a retest was required before implementing a
candidate-geometry cache; that retest has since been completed and the
run-scoped cache for translated candidate polygons only is now implemented.
Collision results are still never cached because they depend on the current
sheet layout.

### Run-scoped candidate-geometry cache implemented

The cache experiment is now implemented as an opt-in Minkowski option. The
`Candidate Geometry Cache` checkbox is persisted with the other panel settings
and is disabled by default, so existing users retain the current behavior until
the option is explicitly enabled.

When enabled, the GA coordinator creates one cache for the entire run and shares
it across all generations, layouts, and the deferred fill phase. The cache key
contains:

- run-local source-object identity, including document and object identity;
- spacing;
- deflection;
- simplification;
- exact normalized rotation angle;
- candidate centroid coordinates.

Only the translated candidate polygon is cached. Sheet-boundary checks,
existing-part intersections, collision short-circuiting, area-tolerance
comparison, candidate scoring, and placement ordering are still performed for
every candidate exactly as before. The cache and diagnostic key tracker are
explicitly cleared in the GA run's `finally` block and are not retained between
nesting jobs.

Performance Logging now reports:

```text
candidate_geometries
geometry_key_observations
geometry_unique
geometry_repeats
cache_hits
cache_misses
cache_s
cache_entries
```

`candidate_geometries` counts actual translations. With the cache enabled it
should be substantially lower than `geometry_key_observations`; the difference
represents reused translated polygons. `cache_misses` should equal the number
of geometries actually built, while `cache_hits` should account for the
observed reuse. Collision counts and the final packing result must remain
unchanged.

### Candidate-geometry cache enabled retest

The first cache-enabled 122-part run used seed `649084969`, Performance Logging,
eight rotation steps, and the existing GA settings. It completed with the
expected **40.4% efficiency, 2 sheets, and 122 placed parts**.

These measurements were taken with the initial cache key and lifecycle, before
the final key/lifecycle cleanup described below. The cleanup does not change
candidate acceptance or packing behavior, but the benchmark is labelled
separately from the current hardened implementation so the two are not
conflated.

The aggregate counters were internally consistent:

```text
nesting=126.59s
candidate_wall=188.36s
candidate_geometry=10.22s
collision_intersection=142.40s
rotation_wall=318.21s
candidate_geometries=97797
geometry_key_observations=394550
geometry_unique=97797
geometry_repeats=296753
cache_hits=296753
cache_misses=97797
cache_ms=1.82
cache_entries=97797
```

The pre-cleanup build printed the cache timing with the label `cache_ms` even
though the value is in seconds. The current implementation reports the same
value as `cache_s`, so the unit label is now accurate.

Thus:

```text
cache_hits + cache_misses = geometry_key_observations
candidate_geometries = cache_misses
geometry_unique = cache_entries = cache_misses
geometry_repeats = cache_hits
```

The cache hit rate was approximately **75.2%**, and only 24.8% of candidate
geometry rows required a translated polygon construction. Peak FreeCAD memory
was approximately **1.9 GB**, compared with approximately 1.8 GB in the
cache-disabled run. The measured aggregate candidate-geometry work fell from
87.44 seconds in the corrected baseline to 10.22 seconds, while cache lookup
and insertion overhead was only 1.82 seconds.

For reference, the immediately preceding cache-disabled 122-part run (seed
`217202820`) recorded the same **40.4% efficiency, 2 sheets, 122 placed parts**
with `nesting=160.29s`, `candidate_wall=314.05s`, `candidate_geometry=89.76s`,
`collision_intersection=188.17s`, `candidate_geometries=452267`,
`geometry_repeats=357466`, and `cache_hits=0` / `cache_misses=0`. Because that
run used a different seed, it is a directional reference only and not a paired
A/B control.

The enabled run used a different seed from the corrected `1843576684` baseline,
so the improvement is strong but not yet a strict paired wall-time comparison.
The remaining control should disable the cache and use seed `649084969` with
all other settings unchanged. The final cache key cleanup also uses the exact
normalized angle and a run-local source identity, and explicitly clears the
cache and diagnostic tracker after each GA run.

### Candidate-geometry cache hardening (current implementation)

The candidate-geometry cache was subsequently hardened before being considered
for production use:

- The cache key uses a run-local source-object identity (document name, object
  name, and `id(source)`), the geometry-processing settings, the **exact**
  normalized rotation angle, and the candidate centroid coordinates. Angle
  rounding was removed so that near-but-not-equal angles cannot collide.
- One shared key-construction helper is used by both the diagnostic tracker and
  the production cache, so they cannot diverge.
- The key tracker and cache both implement `clear()`, and the GA coordinator
  clears them in a `finally` block so the cache is released on success,
  cancellation, and error.
- The diagnostic key tracker is created **only when Performance Logging is
  enabled**; the production cache is independent of Performance Logging and
  still operates when logging is off.
- Cache lookup/insertion timing is collected only when Performance Logging is
  enabled, and is reported as `cache_s` in seconds.
- Cache creation now happens **after** GA setup completes, so a setup failure
  cannot leave retained candidate geometry on the coordinator.
- The Shapely-dependent strategy import is kept lazy so importing the panel
  still works when the optional nesting dependency is missing.
- The misleading `worker_samples` counter was removed and replaced with
  `max_concurrent_rotations`.

Validation performed for the hardened implementation:

- `python -m py_compile` passed for `nesting_strategy.py`, `ga_coordinator.py`,
  `ui_nesting.py`, and `nesting_controller.py`;
- `git diff --check` passed;
- focused key/cache checks passed: different exact angles produce different keys,
  same-named source objects with different identities produce different keys,
  tracker reuse counting works, and both `clear()` methods release state.

### Cache-enabled/disabled decision status

The cache is **worth keeping** based on the enabled run:

- 75.2% hit rate with no change to the packing result;
- `candidate_geometry` reduced from 87.44 s to 10.22 s aggregate;
- only 1.82 s aggregate cache overhead;
- peak memory increase of roughly 100 MB (about 1.8 GB to 1.9 GB).

Before enabling the cache by default, run one cache-disabled control with seed
`649084969` and otherwise identical settings. If the disabled control is
around 145–155 s while the enabled run remains around 126–130 s, the
wall-time advantage is confirmed and the cache should become the default. If
the disabled control is also close to 130 s, repeat once to separate noise from
a real gain. Regardless of the result, the cache should remain opt-in in this
commit.

### Final recommendation state

- **Retain** all validated optimizations listed in Current status.
- **Test** the headless GA layout change manually; it is the single largest
  remaining win (expected 20-36%) and is harness-validated but unproven in
  FreeCAD.
- **Keep** the candidate-geometry cache as an opt-in option for now; promote it
  to default only after the same-seed control confirms the gain.
- **Then** run the cache-disabled control with seed `649084969`.
- **Then** implement the collision `intersects` prefilter.
- **Then** test four rotation workers versus eight as a separate, independent
  experiment.
- **Do not** revisit `unary_union`, greedy convex-piece merging, convex-pair
  result caching, or the native buffer prototypes for this workload; each was
  measured and shown to be neutral or a regression.

---

## Layout-management sub-phase instrumentation

### Why

`layout_management` has been stable at roughly 76–77 s across every 122-part run
(cache enabled, cache disabled, prefilter, baseline), which makes it the largest
untargeted wall-time cost in the GA:

| Run | `layout_management` |
|---|---:|
| Baseline `1843576684` | 77.18 s |
| Prefilter run | 76.91 s |
| Cache disabled | 77.19 s |
| Cache enabled `649084969` | 76.31 s |

Against a cache-enabled GA generation wall of 189.59 s, that is roughly one third
of the run. The arithmetic is attractive: halving it would save about 38 s, which
is 20% of total GA wall time on its own. But the 76 s was a single undivided
number, so the first step is to measure where it actually goes rather than guess.

The flatness across runs is itself informative. It does not scale with the number
of candidate evaluations, which the candidate-geometry cache reduced by 75%, so it
is a structural cost: layout/parts cloning, FreeCAD document object creation and
deletion, chromosome mutation, and placement rebinding. It also plausibly
explains the observed 37–50% CPU utilization on four cores, because most of this
work is serial main-thread FreeCAD work while the rotation pool sits idle.

### What was added

Measurement-only instrumentation, gated on Performance Logging exactly like the
existing stages. The measurement dict is created in `GACoordinator.run()` only
when `performance_logging` is true and is passed by reference into
`LayoutManager`, `ShapePreparer`, and `recursive_delete`. When it is `None` every
hook is a single attribute test and a `return`, so production runs pay no cost.

**Timers** (all cumulative for the run, in seconds):

| Key | Covers |
|---|---|
| `lm_population_s` | initial `create_ga_population` (outside `generation_s`) |
| `lm_next_generation_s` | whole `_build_next_generation` |
| `lm_create_s` | `create_layout` total |
| `lm_group_s` | layout + `PartsToPlace` group `addObject` |
| `lm_prepare_parts_s` | `ShapePreparer.prepare_parts` total |
| `lm_master_prepare_s` | master shape prep and processed-cache lookup |
| `lm_part_instances_s` | per-part FreeCAD instance creation |
| `lm_ordering_s` | chromosome ordering and rotation application |
| `lm_delete_s` | `delete_layout` / `recursive_delete` |
| `lm_gene_ops_s` | selection, crossover, mutation, immigrant randomization |
| `lm_cleanup_s` | final discard of non-winning layouts |
| `lm_post_nest_rebind_s` | per-part placement rebind after `nest()` returns |
| `lm_efficiency_s` | `calculate_efficiency` |
| `lm_fill_s` | `fill_existing_sheets` on the winner |
| `lm_finalize_s` | `_finalize` plus its main-thread dispatch wait |
| `lm_doc_recompute_s` | main-thread `doc.recompute()` |

**Counters**: `lm_populations_created`, `lm_layouts_created`,
`lm_layouts_deleted`, `lm_parts_created`, `lm_masters_processed`,
`lm_master_group_objects_created`, `lm_group_objects_created`,
`lm_master_containers_created`, `lm_master_part_features_created`,
`lm_master_boundary_features_created`, `lm_part_features_created`,
`lm_part_boundary_features_created`, `doc_objects_deleted`.

The document-object counters are the important ones, because they distinguish
"shallow Python work" from "real FreeCAD object churn". `lm_part_features_created`
plus `lm_part_boundary_features_created` give the number of transient part and
boundary objects minted per layout, and `doc_objects_deleted` is counted inside
`recursive_delete` at each successful `doc.removeObject` call.

### Output

Per generation, appended to the existing `[GA PERF]` line as deltas:

```
lm_create=19.21s lm_delete=0.42s lm_gene_ops=0.00s lm_nextgen=19.63s lm_rebind=1.10s lm_eff=0.01s
```

Once per run, appended to the `[GA PERF TOTAL]` line as `[LAYOUT PERF]`:

```
[LAYOUT PERF] population=19.20s next_generation=57.10s create=76.30s
  (group=0.02s prepare=75.90s masters=0.10s instances=75.80s ordering=0.40s)
  delete=0.30s gene_ops=0.00s cleanup=0.00s post_nest_rebind=4.40s
  efficiency=0.04s fill=0.00s layouts_created=16 layouts_deleted=12
  parts_created=1952 part_features=1952 part_boundary_features=1952
  master_containers=4 master_part_features=4 master_boundary_features=4
  master_groups=16 group_objects=32 doc_objects_deleted=4156
```

(The values above are illustrative placeholders, not measurements.)

Because `doc.recompute()` and the main-thread part of `_finalize` run *after* the
totals line is printed, they are reported on a separate trailing line:

```
[GA PERF FINALIZE] finalize_dispatch=0.85s doc_recompute=0.40s
```

### Interpreting the sub-timers

The timers are deliberately disjoint so they sum rather than double-count:

- `lm_create_s` = `lm_group_s` + `lm_prepare_parts_s` + `lm_ordering_s` (+ overhead)
- `lm_prepare_parts_s` = `lm_master_prepare_s` + `lm_part_instances_s` (+ overhead)
- `lm_next_generation_s` >= `lm_create_s` + `lm_delete_s` + `lm_gene_ops_s`
  (the outgoing layouts are deleted before the gene bookkeeping, so gene ops and
  delete never overlap)
- `layout_management_s` (existing) >= `lm_next_generation_s` per generation,
  because the coordinator measures the whole main-thread round trip

`lm_gene_ops_s` is expected to be negligible; if it is not, the chromosome
representation is the problem rather than FreeCAD.

### Validation performed

- `python -m py_compile` passed for `ga_coordinator.py`, `layout_manager.py`,
  `shape_preparer.py`, `nesting_controller.py`, and `freecad_helpers.py`;
- `git diff --check` passed;
- AST check: all 28 `lm_*` keys are declared in
  `_LAYOUT_PERF_TIMERS` / `_LAYOUT_PERF_COUNTERS`, none missing, none orphaned,
  and every key written by `layout_manager.py`, `shape_preparer.py`, and
  `freecad_helpers.py` is declared;
- harness tests with FreeCAD stubbed out confirmed:
  - `recursive_delete` counts 4 removals with `perf_stats` and creates no state
    without it, and an already-deleted child is not double-counted;
  - `LayoutManager.create_layout` is a no-op with `perf_stats=None` and records
    correct group/parts/ordering counters with a dict;
  - chromosome ordering and `layout.genes` output are byte-identical to before
    the change, including the fill-part tail and the `p3, 45.0` rotation case;
  - `delete_layout` records the delete timer and counter;
  - `_build_next_generation` keeps `lm_next_generation_s >= create + delete +
    gene_ops` and produces the same population size and layout counts as before
    (population 4, elite_count 2, immigrant_ratio 0.25 to 2 offspring + 1
    immigrant, 3 created, 3 deleted);
  - the coordinator formatters no-op cleanly when `_layout_perf is None`, and the
    `doc_recompute` value is correctly absent from the totals line.

### Status

Instrumentation is in place but **no measurement run has been done yet**. The next
step is a single 122-part run with Performance Logging on and the candidate-geometry
cache set to its current value, then read the `[LAYOUT PERF]` block to decide
between the two candidate fixes:

- if `lm_part_instances_s` dominates, the transient document object churn is the
  cost, and the experiment is to nest GA layouts from Shapely data only and create
  FreeCAD objects just for the winning layout;
- if `lm_delete_s` dominates, the cost is removal churn rather than creation, and
  the experiment is to defer or batch deletion of discarded layouts;
- if neither dominates, the remaining cost is in `lm_post_nest_rebind_s` or
  `lm_master_prepare_s`, and the target moves accordingly.

Do not start any of those changes before the measurement run.

---

## First instrumented run — and what it revealed

Run: seed `1658300858`, Performance Logging on, candidate-geometry cache on.
Result 40.4% efficiency, 2 sheets, 122 placed.

### The breakdown answered the question immediately

GA wall from log timestamps (06:30:23 → 06:34:22) was **239 s**.

| Timer | s | % of GA wall |
|---|---:|---:|
| `lm_population_s` | 20.61 | 8.6% |
| `lm_next_generation_s` | 78.96 | 33.0% |
| `lm_create_s` | **94.44** | **39.5%** |
| ├ `lm_group_s` | 0.01 | ~0% |
| ├ `lm_prepare_parts_s` | 94.21 | 39.4% |
| │ ├ `lm_master_prepare_s` | 9.59 | 4.0% |
| │ └ `lm_part_instances_s` | **84.61** | **35.4%** |
| └ `lm_ordering_s` | 0.22 | 0.1% |
| `lm_delete_s` | 4.82 | 2.0% |
| `lm_gene_ops_s` | 0.12 | ~0% |
| `lm_cleanup_s` | 0.00 | — |
| `lm_post_nest_rebind_s` | 0.11 | ~0% |
| `lm_efficiency_s` | 0.38 | 0.2% |

The sub-timers were internally consistent, which validated the instrumentation:

- `prepare 94.21 = masters 9.59 + instances 84.61`
- `create 94.44 = group 0.01 + prepare 94.21 + ordering 0.22`
- `nextgen 78.96 ≈ 73.83 create + 4.82 delete + 0.12 gene_ops`

**89.6% of layout management was `lm_part_instances_s`.** Deletion was 6.1%. Gene
ops, rebind, efficiency and ordering were rounding error: the GA bookkeeping is
effectively free and the cost is FreeCAD document churn.

Object accounting closed exactly:

```
19 layouts x 256 objects = 4864 created
15 layouts x 256 objects = 3840 deleted
4 layouts x 256 objects  = 1024 remaining
```

256 = 2 groups + 1 MasterShapes + 3 masters x 3 + 122 parts x 2 (part +
boundary). At ~19.6 ms per FreeCAD object, 94.2 s was spent creating 4807
transient objects, the overwhelming majority never displayed.

### The code proves it, not just the timing

Two findings settled the design:

1. `nesting_logic.py:152` — `parts_to_process = parts if simulate else
   copy.deepcopy(parts)`. In non-simulate mode the nester already works on
   deep-copied Shapes, and `Shape.__deepcopy__` sets `fc_object = None`
   (`shape.py:107`). **The nester already operates on Shapes with no FreeCAD
   object behind them.**
2. `shape.fc_object` is read in exactly one place, `Sheet._draw_single_part`
   (`sheet.py:187`), which already guards `if not shape_obj: return`. It is
   consumed only at commit, by the winner.

So the 244 objects per GA layout existed solely to be re-parented at `_finalize`.

### Candidate-geometry cache, same run

79.7% hit rate (367,390 hits / 93,329 misses / 460,719 observations, all
invariants holding), `candidate_geometry` 10.38 s against ~87 s uncached, 2.14 s
overhead. Still a different seed from `649084969`, so the paired A/B for
promoting the cache to default is still outstanding.

---

## Headless GA layouts

The decided fix: **GA layouts are built from Shapely geometry alone, and FreeCAD
objects are created only for the winning layout at commit.**

### Why the change is small and safe

- Master FreeCAD objects are still created per layout. They are only 9.59 s of
  the 94.44 s, and keeping them means the commit path
  (`NestingJob.commit` moves the `MasterShapes` group and reads
  `Quantity`/`UpDirection`/`FillSheet`/`SourceCentroid` off the containers) needs
  no changes, and materialization has a geometry source already in hand with
  nothing recomputed. `_handle_new_master` already short-circuits reprojection on
  a processed-cache hit, so the 9.59 s is object churn, not geometry.
- This removes 4636 of 4807 objects per run (96.4%).

### Implementation

- `ShapePreparer(..., create_doc_objects=True)`. When False, `make_instance`
  skips the two `create_part_feature` calls and leaves `shape.fc_object = None`.
- `ShapePreparer` also records `_last_master_objects` (master label → master
  `Part::Feature`) during `prepare_parts`; `LayoutManager.create_layout` copies it
  onto the `Layout` as `layout.master_objects`.
- `LayoutManager(..., create_doc_objects=True)` passes the flag through.
- `LayoutManager.materialize_layout_objects(layout, ui_params)` creates the
  part/boundary objects for a headless layout. Idempotent (skips shapes that
  already have an `fc_object`), walks both `layout.parts` and each sheet's placed
  parts so mid-nest fill spawns are covered, and skips rather than raises if a
  master label cannot be resolved.
- `GACoordinator._headless_layouts = not is_simulating`. Simulate mode draws every
  layout as it nests, so it keeps per-layout objects.
- `GACoordinator._finalize` materializes the winner before `sheet.draw`, gated on
  Performance Logging for the log line, with a new `lm_materialize_s` timer.

`Sheet._draw_final_part` re-parents both the part feature and its boundary out of
`parts_group` into the `nested_*` container, so the materialization handoff reuses
the existing, proven path and `NestingJob.cleanup()` cannot delete the results.

### Expected effect

| Scenario | Saving | New wall | Δ |
|---|---:|---:|---:|
| Halve `lm_create_s` | 47 s | 192 s | -19.7% |
| Remove `lm_create_s`, add ~4.5 s to materialize | 85 s | 154 s | **-36%** |

It also removes ~95 s of main-thread serial time, which should raise the effective
rotation concurrency above the 2.58x currently measured on 4 cores.

---

## Bugs found by the same run

### 1. Early-stop leaked the final population

`lm_cleanup_s` was `0.00s` and the arithmetic (19 created − 15 deleted = 4
remaining) confirmed it. The early-stop `break` fired *before* the `else:` cleanup
branch, so the losing 3 layouts of the final population — 768 FreeCAD objects —
were never deleted, and they persisted into the commit path.

Six generations had run; the break skipped the per-generation perf print, which is
why there was no `generation=6` line.

**Fix:** the final cleanup moved out of the generation loop to just after it, so
all four exits (normal completion, cancel, interrupt, early stop) reach it. It
deletes every layout that is not `best_layout`; `delete_layout` is re-entry safe,
so layouts already discarded by `_build_next_generation` are skipped.

### 2. `generation_s` dropped the final generation

Because the break sat before the accumulation, the final generation's wall time
appeared in `nesting_s` and `layouts` but never in `generation_s`.
`generations=200.73s` was really ~216 s.

**Fix:** the early-stop decision is still made *before* building the next
generation (a converged run must not pay to breed a population it will discard),
but it is acted on *after* the generation is accounted for and reported.

A regression was caught here during validation: the first version of the fix let
the build run before the break, so the early-stop generation bred a population it
immediately threw away — an extra ~16 s of pure waste. Caught by the harness and
corrected to `if gen < generations - 1 and not early_stop`.

### 3. `validity` duplicated `candidate_wall`

`validity=206.95s` exactly equalled `candidate_wall=206.95s`. Not a bug:
`nesting_strategy.py:445` and `:448` assign the *same* expression
`(t_validity - t_nfp) * 1000` to `_t_validity_ms` and
`_t_candidate_evaluation_ms`. One is the stage name, the other the wall name for
the same window. Earlier analysis had been reading `validity` as if it were a
separable sub-stage.

**Fix:** relabelled to `validity_stage` with a comment noting it is the post-NFP
window and decomposes into `candidate_geometry + sheet_difference +
collision`. No behavioural change.

---

## Validation for steps 1, 2 and 4

- `python -m py_compile` passed for `ga_coordinator.py`, `layout_manager.py`,
  `shape_preparer.py`, `nesting_controller.py`, `freecad_helpers.py`;
- `git diff --check` passed;
- AST check: all 30 `lm_*`/`doc_objects_deleted` keys declared, none missing, and
  every key written by the three downstream modules is declared;
- `fc_object` reader audit: `sheet.py:187` is the only consumer and already
  tolerates None;
- draw-path audit: all four `sheet.draw` call sites are either simulate-gated
  (never headless) or run after materialization, and `materialize` is confirmed to
  precede `draw` inside `_finalize`;
- headless harness: 0 features created while producing the full parts list;
  `layout.genes` and part ordering byte-identical between headless and full modes;
  materialization creates the right counts, is idempotent, is a no-op when the
  manager builds its own objects, and skips an unresolvable master without raising;
- loop harness: normal completion (3 generation lines, 9 layouts deleted), early
  stop (6 generation lines including the final one, 18 deleted = 5 builds + 3
  post-loop), cancel at the top of generation 2 (6 deleted), and interrupt
  (6 deleted) all clean up and account correctly.

### Status

Steps 1, 2 and 4 are implemented and harness-validated but **not yet run in
FreeCAD**. A manual test is needed to confirm the packing result is unchanged and
to read the new `lm_part_instances_s` / `lm_materialize_s` values.

Still outstanding:

- the collision `intersects` prefilter (step 3), ~15-25% of the collision
  aggregate;
- the cache-disabled control with seed `649084969`;
- the 4-vs-8 worker test, now lower priority since the serial bottleneck is
  being removed.




## Fixture benchmark — the numbers every later claim is read against

Two runs of the 122-part Spacer/Bottle fixture, on the same 4-CPU machine. Every
speed claim below is stated against one of these, or is explicitly labelled as
unmeasured.

| | run A | run B |
|---|---|---|
| commit | `34bbe67` (gate present) | `3e14eb7` (gate reverted, masters pooled) |
| GA seed | `617902902` | `2332608348` |
| Packing efficiency | 40.4% | 40.4% |
| Sheets | 2 | 2 |
| Parts placed | 122 | 122 |
| NFP start → `GA Complete` (wall) | 153 s | 150 s |
| `nesting` (wall) | 133.95 s | 130.16 s |
| `generations` (wall) | 142.81 s | 131.91 s |
| `collision_intersection` (aggregate) | 162.92 s | 143.09 s |
| `nfp_compute` (aggregate) | 96.15 s | 86.61 s |
| `layout_management` (wall) | 7.21 s | 0.55 s |
| `lm_master_prepare_s` (wall) | **9.79 s** | **2.08 s** |
| `master_containers` created | 57 | 3 |
| `master_groups` created | 19 | 1 |
| `doc_objects_deleted` | 216 | 36 |

The two runs use different random seeds, so **cross-run wall-clock differences
are not attributable to code**. That is why the layout-management numbers
matter: they are same-measurement, structural counts (`master_containers=3`,
`masters_pooled=54`) rather than timings, and those are exactly predictable.

The invariant that the packing result is unchanged is checked every run, and the
collision-counter invariants are checked too:

```
intersects_true + intersects_false == exact_collision_checks
intersects_true - grazing_pairs    == collision_rejections
```

Run B: `374921 + 175076 == 549997` and `175076 - 156610 == 18466`, both exact.
The second identity is structural, not luck: the candidate loop breaks at the
first real overlap, so each rejected candidate consumes exactly one
`overlaps=True` pair.

## The DE-9IM zero-area gate, and why it was reverted

Committed as `34bbe67`, reverted as `988208d`. This is the most instructive
result in the document, because the code was correct, the tests were thorough,
and it still lost money.

### The idea

With the `intersects` prefilter in place, most remaining overlays were spent
computing an area of exactly zero. `grazing_pairs` counted the pairs where
`intersects` returned True and the overlay came back within tolerance. The idea
was to settle those with a DE-9IM pattern instead of running the overlay:

```python
# matches exactly when the two interiors are disjoint
_ZERO_AREA_PATTERN = "FF*******"
```

Interiors disjoint implies the intersection is at most a set of lines and
points, so its area is exactly `0.0` and `area > tolerance` is necessarily
False. A match can therefore only ever short-circuit to `overlaps = False`,
which is the verdict the overlay reaches anyway. Candidate ordering,
short-circuiting and tolerance semantics are all unchanged.

### A real bug found on the way

The first pattern tried was `"FF*FF****"`. That constrains the *boundary*
cells as well as the interior one, which additionally requires the boundaries to
be disjoint — i.e. FULLY disjoint rather than touching. It therefore never
matches a touching pair, and the gate silently degraded into a no-op that still
paid for a `relate` call on every single pair.

The matrix is ordered `[I*I, I*B, I*E, B*I, B*B, B*E, E*I, E*B, E*E]`, so only
the **first** cell is the interior/interior one. Constrain that cell alone. A
no-op gate is decision-preserving, which is precisely why a mask-comparison
test could not catch this; the analytic exact-touch construction in
`test_intersects_equiv.py` could.

### Why it lost

Run A measured `zero_area_gated=4,307` against `grazing_pairs=152,431`:

| | value |
|---|---:|
| `grazing_pairs` | 152,431 |
| `zero_area_gated` | 4,307 (2.8%) |
| `intersects_true` (relate calls paid) | 171,356 |

The gate pays one `relate` on every `intersects_true` pair to skip 4,307
overlays — 39.8 `relate` calls per overlay skipped. And the overlay it skips is
the cheap kind: a grazing overlay resolves fast (fitted cost 73.6 µs), whereas
the 645 µs cost-model figure belongs to the *real-overlap* overlay, which the
gate never touches. At those fitted costs:

```
spent:  171,356 x 41.7us = +7.15 s aggregate
saved:    4,307 x 73.6us = -0.32 s aggregate
net:                    ~ +6.8 s aggregate   (~2.5 s wall at 2.7x parallelism)
```

The arithmetic is lopsided enough that no A/B run was needed to decide. Only a
counting counter was needed, and the gate needed a counting counter because the
premise was untested.

### The actual mistake

The premise was wrong, and it is worth being precise about how, because the same
error class appears in the `96.8%` figure earlier in this document.

`grazing_pairs` was read as "the interiors touch, so the overlay is computing an
area that is exactly zero". It does not mean that. It means `area <= 1e-7`. And
a tolerance of 1e-7 is equally satisfied by an overlap of area 1e-8.

On the fixture, **148,124 of 152,431 grazing pairs have genuinely overlapping
interiors** — sub-micron slivers along the contact. The DE-9IM gate correctly
refuses them, and there was nothing there to skip.

Run B, on a different seed, reproduces the population shape almost exactly:

| | run A | run B |
|---|---:|---:|
| disjoint (`intersects` False) | 67.9% | 68.2% |
| grazing (area within tolerance) | 28.9% | 28.5% |
| real overlap | 3.1% | 3.36% |

The grazing share is stable at 28–29% across seeds, so the sliver reading was
not a one-run artifact. A DE-9IM gate on this population has nowhere to go.

### What was kept

`collision_grazing_pairs` survives; `collision_zero_area_gated` does not. The
grazing counter is what explains the failure, and without it the next reader of
the log has no way to tell the grazing set apart from a set of exact contacts.
Its comment now says what the number means and, more usefully, what it does
not:

> A grazing pair only means the overlay reported `area <= tolerance`, and a
> tolerance of 1e-7 is equally satisfied by an overlap of area 1e-8. On the
> 122-part fixture the grazing set is overwhelmingly sub-tolerance slivers with
> genuinely overlapping interiors — 148,124 of 152,431, measured — not exact
> contacts.

`collision_zero_area_gated` is not re-added. It can only be non-zero under a
gate, and the gate is gone.

### The general lesson

This was the second time synthetic benchmarks endorsed an idea that real data
killed. The first measurement said 1.2x on complex bbox-overlapping pairs,
against 2.7x on simple far-apart pairs — and the second was irrelevant anyway,
because the gate replaces the *overlay*, so the criterion is
`c_relate < 0.902 x c_graze_overlay`, not `c_relate < c_intersects`. Getting
the replacement right and still losing says the population was wrong, not the
model. When a counter is available that counts the population the idea claims to
serve, count it *before* building the thing.

## Master-shape pooling

`346932d`, with follow-ups `985e7f9` and `3e14eb7`. This one paid.

### The problem

`lm_master_prepare_s` was **9.79 s of a 10.02 s create phase** — about 90% of all
distinct layout-management time, and the largest non-nesting item left.

The cause is in `ShapePreparer._handle_new_master`. Even on a
`processed_shape_cache` *hit*, it re-ran:

- `master_obj.Shape.copy()`
- `_center_3d_shape`
- `create_part_feature` — a new `Part::Feature`
- `_create_boundary_object`

A 19-layout run therefore built **57 masters where 3 suffice** — Spacer, Bottle
Top and Bottle Bottom, each rebuilt 19 times at ~172 ms apiece. The copy is the
cost; these parts have 192 faces. And a master depends only on its source shape
and the geometry settings, never on the layout, so every layout in a run wanted
the identical set.

### The change

A run-scoped pool on `LayoutManager`, keyed on the existing `cache_key`
(`Name, spacing, deflection, simplification, up_direction`) — which already
encodes exactly the inputs a master depends on. A pool hit reuses the objects
and skips the create path.

Headless only, and the group ownership is the part that actually needed care.
Pooled masters are parented to a **document-level** `MasterShapes` group, not to
any layout's group, because `delete_layout` recursively deletes a layout group
and everything under it. Masters parented there would be destroyed by the
teardown of whichever layout was discarded first, leaving every later layout
holding dangling references. Simulate mode is untouched and still builds
per-layout masters under its own group, because a simulate layout draws while it
nests and must own them.

### Measured result

Run B, against run A:

| | before | after |
|---|---:|---:|
| `lm_master_prepare_s` | 9.79 s | **2.08 s** |
| `master_containers` | 57 | **3** |
| `master_part_features` | 57 | **3** |
| `master_groups` | 19 | **1** |
| `doc_objects_deleted` | 216 | **36** |
| `masters_pooled` | — | 54 |

`masters_pooled=54` is exactly `19 layouts x 3 types - 3 first-builds`. The
counter is self-consistent, so this is a structural result rather than a timing
that happened to look good.

**7.71 s of layout management recovered**, and 180 fewer document objects
created and destroyed. The packing result was unchanged: 40.4%, 2 sheets, 122
placed.

### Two bugs the change introduced, and how they were caught

**Simulate mode silently took the headless path.** The pool was written and read
under `master_pool is not None`. `LayoutManager` creates the pool dict
unconditionally, so in simulate mode layouts 2..N found their predecessors'
masters and reused them, instead of building and owning their own. That is a
straight violation of the simulate-mode contract: sharing masters across layouts
means one layout's teardown can remove objects another is still drawing from. It
also made the two modes no longer comparable at all. Now gated on
`not create_doc_objects`, which is the actual precondition.

Caught by a harness that drives the **real** `ShapePreparer` rather than a fake.
The existing headless harness fakes the preparer, so it could only ever have
observed the `LayoutManager` half of the contract.

**The geometry wrapper was shared instead of copied.** The pool returns the
master object *and* its wrapper; sharing the wrapper puts all 122 part polygons
on the same objects across all layouts, which narrows an invariant the
`processed_shape_cache` path never had — it deep-copies the wrapper per layout.

I measured whether this actually leaks before paying for the fix, and **it does
not**: rotating `Spacer_1` in layout 0 to 37 degrees leaves layout 1's
`Spacer_1` at angle 0 with an unchanged centroid, and moving layout 1 by
`(25, -13)` leaves layout 0 alone. `Shape.set_rotation()` and `move()`/`move_to()`
rebind `shape.polygon` from `shape.original_polygon` rather than mutating in
place, so the shared objects are only ever read.

So the copy in `3e14eb7` is **not** a bug fix. It restores per-layout isolation
that used to exist, for the cost of one small 2D deepcopy — the saving here is
entirely the avoided `Shape.copy()` on a 192-face solid, so paying microseconds
to not weaken an invariant is the right trade. The commit message says exactly
that, so nobody later reads it as a fix for an observed bug.

### Interpreting the layout timers — a double-count

`[LAYOUT PERF]` reports `create=2.31s` alongside `population=2.14s` and
`next_generation=0.51s`. Those three do not add up: `population + next_generation
= 2.65 s > create`. They cannot.

`create` is the total across the run. `population` and `next_generation` are
*blocks that contain* `create` calls — 15 of the 19 layouts are built inside
`_build_next_generation`. The true distinct layout cost is therefore
`create + delete + gene_ops + cleanup + rebind + efficiency`, about **2.65 s**,
down from about 10.9 s. Do not sum the create-family timers.

## A measurement trap: a stub bug that inverted a result

Worth its own section, because it nearly produced a wrong decision and it is
the failure mode most likely to recur.

The pinned-seed headless harness (`/tmp/opencode/test_pinned_seed.py`) exists to
make two runs comparable. Its first A/B said the pooling change was **2.7x
slower** — 9.2 s versus 26.1 s. That is the opposite of the truth, and the
harness was lying for three separate reasons at once:

1. The `FreeCAD.Vector` stand-in did not flatten its single-argument form.
   `Shape.__deepcopy__` rebuilds vectors with `FreeCAD.Vector(v)`, so `v` is a
   3-tuple — which nested a vector *inside* a vector.
2. `source_centroid.negative()` then raised `TypeError`.
3. That raise landed in the **swallowed** `except` in `ShapePreparer.prepare_parts`,
   which logs and continues.

Result: the *baseline* (no pooling) produced **silently empty layouts**, and
nesting almost nothing. The run still exited 0. The harness printed `PASS`.

The tell, once I looked for it, was obvious: the "slow" arm reported
`candidate_geometries_built` roughly *double* the other arm's while doing a
fraction of the work. More candidates, less time, is not a performance
difference; it is a different code path.

### What actually went wrong in process

- The A/B script copied variant files into the tree, and one `git checkout HEAD --`
   during an unrelated step silently reverted the variant under test. Some
   intermediate "results" were therefore not comparable at all. The fix was to
   park variants in `/tmp/opencode/ab/` and have the runner copy them in and
   restore from a single `trap`, so no shell step can strand a mixed tree.
- Area was initially used as the leak discriminator in the aliasing probe. Area
  is useless for that: a rigid rotation preserves area. Angle and centroid are
  what change. The first version of that probe would have reported "no leak" for
  the wrong reason and been equally unconvincing.
- The harness reports `collision_rejections=0` on its synthetic geometry, which
  makes the grazing/overlap split **degenerate** — every `intersects_true` pair
  is grazing, so the second invariant holds trivially and proves nothing. The
  harness now says so explicitly rather than reporting a vacuous pass.

### The rules that came out of it

- A stub whose failure mode is a swallowed exception is worse than no stub: it
  converts a crash into a wrong number. Every stub class must be able to raise
  loudly on an unexpected call.
- A benchmark that cannot fail is not a benchmark. Before trusting a speed
  number, check that both arms did the same *work* — candidate counts, part
  counts, placed parts — not just that both finished.
- The pinned seed fixes the search but **not** the per-run counters. Rotations
  are evaluated concurrently and the winner is chosen with a strict `<` against
  whichever result completed first, so a metric tie is broken by thread
  scheduling. Outcome (sheets, efficiency, placed) is reproducible; counters move
  by ~10% run to run. Take precise figures from the fixture, not the harness.

## Collision-mask hole instrumentation

Two commits, `a200822` and `9101640`, added counters answering a question the
existing report could not: **how much of the collision mask's boundary is holes,
and how much of that is even reachable by a nestable part?**

The mask is built by buffering each placed part by `spacing/2` and unioning. The
exterior of the union is the material boundary; the interiors are the holes
between parts. Before this work none of that was visible.

Reported now, per run:

- `hole_rings` / `hole_vertices` / `exterior_vertices` / `hole_pct`
- `subthreshold_rings` (as a numerator over the total) and `subthreshold_pct`
- `hole_sensitive_pairs` — pair checks where at least one part was hole-sensitive
- `hole_exploiting_placements` — placements that actually landed in a hole

On the fixture: `hole_pct=52.1%`. **More than half the mask's boundary is
interior rings.** And `subthreshold_pct=91.5%` — 509,710 of 513,905 rings are
too small to hold the smallest part in the set. That is the number that
eventually justified the pruning work.

Two things about these counters that are easy to get wrong:

- `hole_rings=505957` is a count of **re-observations**, not unique rings. Ring
  geometry is memoised per `Shape` and re-counted once per existing part per
  mask call. Never read it as a population.
- The `subthreshold_rings` denominator changed during this work. An earlier
  version divided by something that made the percentage meaningless. The
  denominator is now the total ring count, and the fraction is auditable — which
  is what let the 91.5% stand up as independent corroboration of the 95.6% the
  pruning counters report through a completely different code path.

The instrumentation was deliberately kept separate from the optimization that
followed, and is gated on Performance Logging like everything else in the
report. Its own cost was cut ~40x by measuring only a subsample; the
sub-threshold count in particular used to walk every ring of every part on every
call.

## Rotation worker width knob

`5610438` added `NESTING_ROTATION_WORKERS` and the `rotation_workers` report
field, with no change to the default.

`ThreadPoolExecutor()` with no argument defaults to
`min(32, os.cpu_count() + 4)`, which is 8 on this 4-CPU box — twice the core
count. The candidate-geometry work is GEOS-bound and releases the GIL, so
oversubscription is not automatically harmful, but neither is it automatically
helpful: it trades throughput for cache pressure and contention on the shared
NFP cache. The effective width is measurable rather than assumable, so it is now
a knob.

**The 4-vs-8 comparison has not been run.** Two reasons it is not the quick
check it looks like:

1. It is **not provably result-neutral**. `find_best_placement` takes the best
   with a strict `<` against whichever result completed first, so on a metric tie
   the winner is decided by thread scheduling, and completion order depends on
   pool width.
2. The timing drift documented below means two runs on different days cannot
   resolve a difference of the size expected.

It needs a pinned seed and both arms interleaved in one session.

## Pruning rings that no part can occupy

The largest NFP win in this project, and the one that took the longest to frame
correctly. Three commits: `1e87e5f` implement, `0ea47ed` fix the counters,
`5b23dfe` enable by default.

### Three wrong framings before the right one

**Wrong 1: "reduce collision-mask vertices."** The 91.5% sub-threshold
measurement seemed to point straight at the mask. But the mask never passes
through `decompose_if_needed`; it is not decomposable at all. Dead rings there
cost nothing to draw. The optimization would have been invisible.

**Wrong 2: concluding it was not worth doing at all.** Overcorrecting, and wrong
for a different reason: the mask is not the expensive thing, but the *holes* are
what force the NFP decomposition to produce convex pieces.

**Wrong 3: "add a convex fast path to the decomposition."** If a part's polygon
had no holes it could be triangulated directly with no convex decomposition at
all. The gate at `minkowski_utils.py:103` tests `not polygon.interiors`. But the
Spacer's *exterior* is genuinely concave — exterior area 205,465 against a
convex hull of 227,028 — so the gate never opens on the part that matters.
Confirmed by direct probe, not by reading the code.

**Right: "prune holes too small to hold the smallest part, as early as
possible."** That targets the NFP decomposition input, and the mechanism is
piece count: the decomposition must cover material *around* every ring, so each
ring costs convex pieces, and the NFP pays one pairwise Minkowski sum per piece
pair. Rings are the multiplier.

### The fixture spacing was not what I assumed

The first reproduction attempt failed because I assumed the fixture spacing was
12.5 mm. At that value `buffer(6.25)` erases every hole before the NFP engine
ever sees one, so the whole effect is invisible and the counters read zero.

It is approximately **4.0 mm**. Proof, and the numbers reproduce exactly:
`spacing=4.0` gives Spacer `parts=192 → 98` (i.e. 192² = 36,864 sums → 98² =
9,604) and leaves Bottle Bottom at 22, unchanged. That the Spacer moves and
Bottle Bottom does not is itself the confirmation that the mechanism is
hole-driven.

### Why pruning is safe, and not merely measured

`_ring_is_live` mirrors the NFP engine's own filter **exactly**, including its
strictness:

```python
if mw < width and mh < height and ma < area:
```

The engine only ever builds an inner-fit island under that test, so a ring that
fails it has no island, its area is pure forbidden over-cover, and the
decomposition can skip straight over it. A part that *exactly* fills a ring is
classified dead here — the aggressive reading — and deliberately so, because the
engine would refuse to build an island for it anyway. `<=` would prune strictly
less at no gain in reachable positions.

Two structural arguments hold independently of any measurement:

1. **Pruning only ever shrinks the NFP.** A smaller forbidden region means the
   set of positions the NFP declares legal can only grow. It cannot cost a
   placement.
2. **The collision mask is not in this code path at all.** It keeps every ring
   and remains the exact verifier. `hole_exploiting_placements` is untouched.

### What it also repaired

A live defect that predates the optimization. Replacing a clipped piece with its
`convex_hull` in `_decompose_uncached` over-covers rings, but the unconstrained
Delaunay can also lose coverage outright. A Minkowski sum of two rigid bodies
has area invariant under relative rotation, so:

| pair, angle | unpruned | pruned |
|---|---|---|
| Bottle Top × Bottle Top, 0° | 15094.8 | 15094.8 |
| Bottle Top × Bottle Top, 90° | **10947.6** | 15094.8 |
| Bottle Top × Bottle Top, 135° | **11977.6** | 15094.8 |
| Bottle Top × Bottle Top, 270° | **10974.1** | 15094.8 |
| Bottle Top × Bottle Top, 315° | **12772.4** | 15094.8 |

15094.8 is correct at every angle. The unpruned figures at 90°/135°/270°/315°
are lost coverage — the exact failure the code comment in `_decompose_uncached`
says must never happen. With the rings pruned the area is 15094.8 at all eight
angles. The optimization is not only faster, it is more correct.

### Two things it is not

**Not a strict superset.** 3 of 48 profile-pair cases get *smaller* by at most
0.0393% of area (28–94 mm² of roughly 232,000), all Spacer × Bottle Top. This is
not a defect — a smaller forbidden region is strictly more permissive — but it
should not be described as "never smaller" without the number attached. The
`freecadcmd` e2e asserts 0 violations against a 0.1% shrink limit and reports
the worst case explicitly.

**Not proven to preserve the global optimum.** It makes a strictly larger
position set available at equal fitness, so it can never do worse, but it also
cannot be claimed to find layouts the unpruned code could not.

### The A/B

Four fixture runs, three different GA seeds, one control:

| run | seed | pruning | efficiency | sheets | placed | nesting | `nfp_compute` |
|---|---|---|---|---|---|---|---|
| control | 3409674212 | off | 40.4% | 2 | 122 | 213.20 s | 114.58 s |
| T-1 | 3561566808 | on | 40.4% | 2 | 122 | 201.67 s | 28.17 s |
| T-2 | 2140966527 | on | 40.4% | 2 | 122 | 250.12 s | 38.63 s |
| T-3 | 926421131 | on | 40.4% | 2 | 122 | 294.52 s | 43.28 s |

Both structural invariants held exactly on every run:

```
intersects_false + intersects_true == exact_collision_checks
intersects_true - grazing_pairs    == collision_rejections
```

## The machine moves under the measurement

The `nesting` column above goes 201 s → 250 s → 295 s. That is **not** the
optimization regressing. Per-generation times, on work that is pure NFP-cache
lookup (`hits=1 misses=0` from generation 2 onward) and cannot be touched by
anything under test:

| run | gen 2 | gen 3 | gen 4 | gen 5 | gen 6 |
|---|---|---|---|---|---|
| T-1 | 24.96 s | 24.25 s | 27.55 s | 24.51 s | 23.97 s |
| T-2 | 31.04 s | 30.57 s | 31.32 s | 31.37 s | 30.34 s |
| T-3 | 36.82 s | 38.20 s | 38.17 s | 37.69 s | 36.83 s |

Monotone ~+20% then ~+20% again across three consecutive sessions, including on
the generations that are pure cache hits. That is the box — thermal, other load,
whatever — and it puts a **20% floor on the noise** of any single-run fixture
comparison.

Consequences, applied honestly:

- The `nfp_compute` ratio is 4.07× against the control when the machine was
  fast, and 2.6× when it was slow. Both are true. The real ratio is not
  recoverable from wall clock this week, and picking the flattering one would be
  dishonest.
- The 4× claim that goes in the commit message rests on **machine-independent**
  evidence: `parts=98x98` against the control's `192x192`, and the
  `freecadcmd` e2e, which is measured back to back with its own control in the
  same process and reported 4.06x–4.82x across three sessions.
- Future A/B work on this box wants interleaved arms and a pinned seed, not two
  runs on different days.

### A side effect worth naming

The unexplained `collision_intersection` increase seen in T-1 (250.89 s → 261.81 s
against a control that nested nothing differently) was never explained. A +0.5%
change in `exact_collision_checks` cannot mechanically produce +4.3% there. The
honest position is that it is unresolved, and that with the drift documented
above it is not currently distinguishable from machine noise. It should not be
cited as evidence of a regression, and equally it should not be waved away.

## Validation harnesses

All under `/tmp/opencode/`. The first three need no `PYTHONPATH`; the rest do.

| harness | what it pins |
|---|---|
| `test_intersects_equiv.py` | `intersects` prefilter is decision-preserving; the analytic square-hole exact-touch construction that a fakes-only harness cannot reach |
| `test_mask_equiv.py` | real `_exact_candidate_mask` vs a pure-overlay control, 180 trials / 8,345 pairs; the deterministic exact-touch check repurposed from "gate fires" to "area test settles it" |
| `test_counter_plumbing.py` | 9 counters traverse all 4 hops: probe init → probe increment → generic copy → aggregation tuple → `_ga_perf` → print. Fails if any hop is reverted. |
| `test_layout_perf.py` | layout sub-phase timers, disjoint and correctly attributed |
| `test_nextgen_perf.py` | `next_generation >= create + delete + gene_ops` |
| `test_headless.py` | headless creates zero features but a full parts list; genes byte-identical to simulate; materialization counts, idempotence, no-op when the manager builds its own; **pooling: 3 built, 9 reused over 4 layouts, masters survive teardown** |
| `test_ga_loop.py` | cleanup on every exit path; `generation_s` covers the final generation |
| `test_master_pool_real.py` | the **real** `ShapePreparer`: 1 `Shape.copy()` per master type across 4 layouts, masters in the shared group and not a layout group, pool intact after 3 teardowns, a post-teardown layout still fully pooled, pooled layouts get their own wrapper copy, and simulate mode still builds 3-per-layout with an empty pool |
| `test_pinned_seed.py` | fixed-seed headless GA; prints `[GA PERF TOTAL]` and checks both collision invariants. Cannot measure the pooling win (see below). |
| `probe_holes.py` | measures hole population and the sub-threshold fraction through the real mask path |
| `test_profile_deepcopy.py` | master pooling does not leak a mutated wrapper between layouts |
| `test_worker_limit.py` | `NESTING_ROTATION_WORKERS` parsing and the `rotation_workers` report field |
| `test_dead_rings.py` | 9 hops on the pruning contract: live ring survives, strict `<` boundary preserved, hole-free polygon is the same object, caller polygon never mutated, invalid prune falls back, lifecycle, speed, counter **ownership**, report shape |
| `test_dead_ring_race.py` | the counters are exact under 8 concurrent threads, **and** a negative control proving the test can fail |

Plus `verify_dead_rings_e2e.py`, which must run under `freecadcmd`, not
`python3`, because it needs real OCC.

### Two harnesses that could not fail, and how they were fixed

Both are recorded because the failure mode is the same one that produced the
DE-9IM reversal and the master-pooling reversal.

**`test_pinned_seed.py` stubs the thing it is trying to measure.** It stubs
`create_part_feature` and `Shape.copy` — precisely the work pooling removes. It
can validate correctness, and it earned its keep by catching the simulate-mode
bug, but the speed claim had to come from the fixture. A harness that stubs the
thing you are measuring cannot measure it.

**The first counter-race harness lost nothing.** Written to prove the
single-threaded-untestable claim that `d[k] += v` races, it ran 8 threads ×
4,000 iterations of a bare unlocked increment and reported *zero* lost updates
out of 96,000. A dict read and a store are nanoseconds apart; CPython's switch
interval is 5 ms, so the window essentially never opens. Adding a comment as a
"yield point" did not help, because a comment is not a yield point. The fix was
`time.sleep(0)` between the read and the store, which really does yield the GIL.
With that in place the negative control reproduces the fixture's exact symptom —
`dropped=9237 > seen=8853`, against the fixture's `dropped=1048 > seen=1042`.

The rule, stated generally: **for any claim about concurrency, the test must
include a negative control that is expected to fail, and the test must be run
once with the fix disabled to confirm it does.**

## Status

Fixture-verified, harness-validated commits on `Faster-NFP-Calc-Investigate`:

- `988208d` revert the DE-9IM zero-area gate
- `47383fa` keep `collision_grazing_pairs`, with the sliver finding recorded
- `346932d` master-shape pooling, headless only
- `985e7f9` gate pooling on `create_doc_objects` (simulate-mode fix)
- `3e14eb7` copy the pooled geometry wrapper per layout
- `a200822` collision-mask interior-ring instrumentation
- `9101640` sub-threshold measurement, 40x cheaper instrumentation
- `5610438` auditable `hole_pct` plus the rotation-worker width knob
- `1e87e5f` prune rings no nestable part can occupy
- `0ea47ed` run-level, lock-protected dead-ring counters
- `5b23dfe` enable the pruning by default

Packing result unchanged across all of them: **40.4% efficiency, 2 sheets, 122
placed**, and the winning layout still materializes its masters and 122 part
objects. Real-engine e2e: 0 shrink violations, worst 0.0393% against a 0.1%
limit, ~4x faster NFP.

Layout management and NFP compute are both closed as targets.
`collision_intersection` (350.87 s aggregate) is what remains, and it sits in the
stage that has already produced one reversal — so the bar for a new idea there is
a counting counter, measured on the fixture, before any code.

Still outstanding:

- the cache-disabled control with seed `649084969`;
- the 4-vs-8 rotation worker test, which needs a pinned seed and interleaved arms
  in one session given both the scheduling non-determinism and the timing drift
  documented above.

