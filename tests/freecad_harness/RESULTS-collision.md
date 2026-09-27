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
