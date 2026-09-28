# The `collision_intersection` stage — unpicked, and the win that isn't there

`collision_intersection_ms` read 18.4 s of a 59 s run on the heavy corpus: 69% of
candidate evaluation, and the largest item after the NFP union. `make-faster.md`
§9.5 flags it as the largest remaining cost.

"18 seconds in collision" is not actionable, so the stage is now decomposed by
three new sub-timers and three call counts.

## Where the time is

Heavy corpus, one nest, 184 197 exact collision checks:

| | time | share | per call | calls |
|---|---:|---:|---:|---:|
| `intersection().area` | 12.088 s | **65.7%** | 83.4 µs | 144 981 |
| `intersects()` | 4.364 s | 23.7% | 23.7 µs | 184 197 |
| hole probe *(measurement-only)* | 1.611 s | 8.8% | 8.8 µs | 184 197 |
| pair bookkeeping, bbox scan | 0.323 s | 1.8% | | |

Two things fall out immediately.

**The hole probe is 8.8% of the figure.** `_holes_touched` is documented as
measurement-only and is correctly gated on `probe is not None`, so it never runs
in production — but it sat *inside* the `collision_intersection_ms` window, so
every logged run overstated the real cost by its own overhead. It is now timed
separately, which is what makes the total usable as a production figure. This
is the second time a "measurement-only" probe has been found inside a production
timer.

**Only 21.3% of `intersects` calls short-circuit** (39 216 of 184 197), so 144 981
pairs go on to the overlay. `make-faster.md` records 67.7% short-circuiting on the
n70 fixture; the heavy corpus overlaps far more.

## The overlay rejects everything

`collision_grazing_pairs` equals `collision_intersects_true` — **144 981 of
144 981**. Every pair that `intersects` accepts is then rejected by the area
test. Not a near miss: total.

That is coherent. The NFP already guarantees non-overlap, so candidates sit
exactly *on* the NFP boundary, `intersects` fires on a touch, and the area comes
out below tolerance every time. The stage is a verification pass that always
verifies.

It is also workload-dependent, and the counters show how. On the n70 fixture the
same figures are 98 366 true against 91 456 grazing — 93%, not 100%. And
`mask_hole_exploiting_placements` is 0 on both corpora, so no internal-fit
placement ever occurs. An IFP placement genuinely overlaps its container; that
is the case the overlay exists for, and neither corpus here exercises it.

## The win that isn't there

The obvious move is to drop the overlay. For two polygons, positive-area
intersection is exactly `overlaps or contains` in either direction — a
boundary-only touch is none of them — and those are predicates rather than
constructive overlays.

Measured on the real corpus polygons (`probe_collision_overlay.py`, 4 000 pairs
per case, min of 5):

| case | `intersection().area` | `overlaps+contains` | ratio | agree? |
|---|---:|---:|---:|---|
| touching | 117.1 µs | 11.9 µs | **9.8× cheaper** | yes |
| overlapping | 115.4 µs | 19.2 µs | **6.0× cheaper** | yes |
| disjoint | 10.3 µs | 15.8 µs | 0.65× | yes |

6–10× cheaper, and it would have been worth roughly **10 s of a 59 s run, about
17%**. All three cases agree.

**It is still wrong, and one counter says why.** New instrumentation splits the
rejected pairs by how they are rejected:

```
overlay calls                     144,981
  intersection area == 0 exactly    1,734   (1.196%)
  0 < area <= tolerance           143,247   (98.804%)
  sum of intersection areas           0.0000 mm²
```

Only 1.2% are exact boundary touches. The other 98.8% have a *positive* area
below the 1e-7 tolerance — and those areas sum to zero at the printed precision.
They are floating-point touching artifacts, not real overlaps, on parts at
~500 mm where 1e-7 mm² is about 1e-10 mm across.

So the tolerance is load-bearing: it is what separates "touching" from
"overlapping" when GEOS cannot tell them apart. `overlaps` sees the same noise
`intersects` does, so the predicate form would reject all 143 247 — which are the
*correct* placements. The swap trades a 17% wall-clock win for a worse packing,
and the reason is only visible if you measure the area distribution rather than
the pass/fail rate.

There is no cheap predicate that separates them, because the ambiguity is in the
geometry GEOS is handed, not in the API being used.

## Where that leaves it

No safe win at the call site. The 12.1 s is GEOS computing an intersection for
pairs that touch, and distinguishing that from a real overlap is the entire
purpose of the call.

The remaining levers are all upstream of the call, and none is cheap:

- **Fewer pairs reaching collision at all.** 184 197 verifications exist because
  the NFP cannot be trusted to prove non-overlap by itself. If a proof existed,
  the stage would shrink. That is a geometry argument, not a performance one.
- **Exercise the case that is untested.** `mask_hole_exploiting_placements` is 0
  everywhere measured, so the IFP path — the one where the overlay genuinely
  matters — has no measured cost at all. Before the stage is tuned, a workload
  that produces internal-fit placements would show whether the 12.1 s is
  dominated by IFP pairs or by boundary touches.
- **The probe's 8.8% is measurement, not production.** Now separated, so future
  collision figures can be quoted without it.

## Reproduce

```sh
NEST_BENCH_CORPUS=heavy \
NEST_BENCH_QUANTITIES='HeavyPlate=2,Small0=30,Small1=30,Small2=30,SmallL=30' \
NEST_BENCH_SHEET=1200x600 NEST_BENCH_ROTATION_STEPS=8 NEST_BENCH_REPS=1 \
$FREECADCMD tests/freecad_harness/nest_benchmark.py      # the counters

$FREECADCMD tests/freecad_harness/probe_collision_overlay.py   # the predicate timing
```

## Summary

| question | answer |
|---|---|
| where is the time? | overlay 65.7%, `intersects` 23.7%, measurement probe 8.8% |
| does the overlay ever accept? | no — 144 981 of 144 981 rejected |
| can a predicate replace it? | it is 6–10× cheaper and would be worth ~17%, but **no** |
| why not? | 98.8% of the rejections are positive areas below tolerance, summing to 0 mm²; the predicate would reject the correct placements |
| is the win real? | measured, and disproved. That is the useful outcome |

---

# Second edition — measured on the configuration that actually runs

Everything above was measured on a single cold nest of the heavy synthetic
corpus. The configuration that is actually run is a GA run of the n70 file —
1200×600, 2 Spacer + 60 Bottle Top + 60 Bottle Bottom, 8 generations, population
4 — and the two regimes differ enough that the conclusions had to be re-derived
on it. It is now a harness configuration (`run_ga.sh GA_CORPUS=n70`), gated
locally, because n70 is a gitignored customer part.

## The shape of that run

180.5 s wall, 122 parts placed, 2 sheets, efficiency 0.418433.

```
collision stage          88.42 s      49% of wall
  intersection().area    58.01 s      65.6% of collision    157.2 us x 368,954
  intersects()           21.80 s      24.7% of collision     29.8 us x 732,514
  bookkeeping             8.61 s       9.7% of collision
nfp_compute               4.07 s       2.3% of generations
```

**NFP construction is 2.3% of this run**, against 4.5% of the 132.59 s the same
configuration took on a loaded box. The NFP misses are computed once — 82 of
them — and then hit 26 468 times. Any NFP-stage optimisation is bounded by a
couple of percent here. That is the finding that redirected this work.

## The predicate replacement is unsafe here too, and now provably

The open question from the first edition was the split of the grazing pairs.
`grazing_pairs` conflates two very different things — an exact boundary touch and
a positive sliver under the tolerance — and only the first is one a
`overlaps or contains` predicate also rejects. Counters added, on the GA
distribution:

| overlay calls | 368,954 | |
|---|---:|---|
| area == 0 exactly | 15 311 | **4.1%** — a true touch; the predicate agrees |
| 0 < area ≤ tolerance | **329 690** | **89.4%** — floating-point noise; a predicate would **wrongly reject** these |
| area > tolerance | 23 953 | 6.5% — real overlaps; both reject, 258 mm² each |

So `overlaps or contains` would reject 329 690 pairs that the area test accepts
as valid placements. It is 6–10× cheaper and it is wrong, on the workload that
matters as well as on the synthetic one.

## The opportunity, and why the obvious fix is the wrong one

The striking number is what the noise group costs:

```
329,690 overlays that together measure at most  0.033 mm²
costing                                            51.8 s
which is                            89% of the overlay, 29% of the run
```

**29% of the n70 GA run is spent measuring intersection areas that sum to less
than a third of a square millimetre.** Those pairs are boundary touches that
GEOS reports as having a small positive intersection area; the 1e-7 tolerance
exists precisely to absorb that, and it is doing its job. So this is not a
redundancy to be optimised — it is a robustness workaround for the geometry
library, and the work is necessary to *disprove* an overlap.

That closes the question the first edition left open. The remaining levers are
all about getting fewer pairs to the collision stage, or none of them are
available at this layer:

- The bbox prefilter already runs first and rejects 17.7 M of 21.0 M checks.
  `intersects` then rejects a further 363 560 of 732 514, so 49.6% never reach
  the overlay.
- The internal-fit path is genuinely exercised here, unlike either synthetic
  corpus: `hole_pct=52.0%`, `hole_exploiting_placements=121 500`,
  `hole_sensitive_pairs=66 359`. That is the case the overlay exists for, and on
  a workload without it the 100%-grazing figure was misleading.

## Two defects in the instrumentation, found by this measurement

Both were in code added to answer this question, and both produced numbers that
looked plausible.

**The split counter was two-way, not three-way.** `collision_overlay_area_below_tol`
only tested `<= 0.0`, so it counted every overlay with *positive* area —
including the real overlaps — as sub-tolerance. It read 95.8% sub-tolerance when
the true figure is 89.4%. The difference is exactly the 23 953 real rejections,
i.e. the number that decides the question. Now split three ways: zero, sub-tolerance,
over.

**Milliseconds in a seconds dict.** The collision sub-timers were added through
`_record_nest_perf`'s allowlist loop, which does `self._ga_perf[key] += stats.get(key, 0)` —
no `/1000`. They read `collision_intersects_ms 22184.8` beside
`collision_intersection_s 89.6` in the same record. Converted explicitly now, and
`test_perf_counter_plumbing.py` checks the raw loop never carries a millisecond
key.

## Summary, second edition

| question | answer |
|---|---|
| how big is collision on the real config? | 88.4 s of 180.5 s — 49% of the run |
| how big is the NFP stage there? | 4.07 s — **2.3%**, so NFP work is bounded by a couple of percent |
| can a predicate replace the overlay? | no — **89.4%** of overlays are sub-tolerance noise it would reject |
| is there a real opportunity? | 51.8 s measures 0.033 mm² in total, but it is necessary work: disproving an overlap is the point |
| what would move it? | fewer pairs reaching collision, not a cheaper test of the pairs that do |
