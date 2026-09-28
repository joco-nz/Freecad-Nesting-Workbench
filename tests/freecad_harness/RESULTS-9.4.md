# Block 9.4 — candidate-geometry cache, A/B on the real workload

The cache is the one opt-in feature in this workbench with a measured win and
**zero test coverage**. `make-faster.md` records `87.44 s → 10.22 s` at a 75.2%
hit rate, and `master-merge.md` §9.4 required a disabled-control measurement
before it could go default-on. On the n70 corpus the `candidate_geometry_ms`
counter had been reading ~10.8 s of work that the cache would largely skip.

Reproduce with:

```sh
NEST_BENCH_CORPUS=tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd \
NEST_BENCH_QUANTITIES='Spacer=2,Bottle Top=60,Bottle Bottom=60' \
NEST_BENCH_SHEET=1200x600 NEST_BENCH_ROTATION_STEPS=8 NEST_BENCH_REPS=3 \
$FREECAD tests/freecad_harness/bench_candidate_cache.py
```

Workload is the notebook's: 2 Spacer + 60 Bottle Top + 60 Bottle Bottom on
1200×600, 8 rotation steps. Matches the notebook's GA run of the same parts
(122 placed, 2 sheets, 40.4%).

## Result

| | cache off | cache on | |
|---|---:|---:|---|
| wall (min of 3) | 72.688 s | 62.715 s | **0.863×** |
| `candidate_geometry` | 10 757 ms | 886 ms | **−91.8%** |
| control spread | 0.927 s (1.3%) | 0.484 s (0.7%) | |
| hit rate | — | **92.2%** (141 301 / 11 927) | |
| peak RSS | 580 MiB | 580 MiB | **+0 MiB** |
| placed / sheets / density | 122 / 2 / 0.418433 | 122 / 2 / 0.418433 | **identical** |

The effect is **13.7%** against a **1.3%** control spread — ten times the noise,
measured interleaved so both arms saw the same machine state.

## The three questions, answered

**1. Is it layout-neutral? Yes — on real geometry, across 6 runs.** The cache
stores geometry only; collision, sheet-boundary and scoring results are
documented as always recomputed. Confirmed rather than assumed: all six runs
produced one distinct fingerprint, and the fingerprint includes
`placed_by_label`, so a change in *which* parts were placed would have shown
even if the totals matched. This mattered to check, because
`bench_rotation_workers.py` found the analogous claim for pool width was false
on this corpus.

**2. What does it save? 10 seconds of 72.7.** Note the ratio: `candidate_geometry`
falls 91.8% but total wall only 13.7%, because that phase was never most of the
run. The honest headline is the wall figure, not the phase figure. The hit rate
here is 92.2% against the notebook's 75.2% — this workload repeats candidate
geometries more, so the notebook's number should not be transferred.

**3. What does it cost in memory? Nothing measurable here.** `580 MiB` both
arms, identical to the resolution of `ru_maxrss`. Plausible rather than
surprising: 141 301 cached polygons fit under a peak that the NFP work reaches
anyway (516 144 convex pairs unioned). It is still an unbounded per-run dict
with no eviction and no size cap, so a workload with more distinct candidate
geometries would cost more — this is "no cost on this workload", not "no cost".

## Measurement notes

- **Timing interleaved, memory not.** `ru_maxrss` is a process high-water mark
  and never falls, so a reading taken in a process that has already run the
  other arm is contaminated by it. The memory phase runs one subprocess per arm
  and reports absolute peak RSS. Measuring it in-process would have
  manufactured a result in whichever direction the run order favoured.
- **Counters are exact.** `hits/misses` was `141 301 / 11 927` on all three
  reps, so the cache behaviour itself is reproducible even though wall is not.
- **The control arm is the honest denominator.** The verdict line compares the
  effect against the spread *within* the control arm, not against zero.

## What this does not say

- Not a claim about other corpora. The synthetic corpus gives a 41.7% hit rate
  and a −6.2% wall change; both are much smaller, and its control spread is
  4.2%, so it is the n70 run that settles this.
- Not a claim about GA runs. This is a single nest per rep. The notebook's
  87.44 s → 10.22 s figure came from a GA run with 19 layouts, and layout
  recycling may change the reuse pattern substantially.
- Not a memory-safety argument. One workload, one shape of reuse.

## Recommendation

`make-faster.md` §9.4's precondition is now met. On the evidence: the cache is
layout-neutral, worth 13.7% on the real workload at a 92.2% hit rate, and free
in peak RSS here. It is a defensible default-on.

Before that, though: it has **no tests at all**, and the correctness surface is
real. A key collision — two different geometries hashing to the same cache key —
would return the wrong polygon and silently change collision results. The
layout-neutrality result is evidence against that happening on this corpus, not
proof that it cannot. Characterisation tests for `CandidateGeometryCache` (a
cached polygon equals a freshly computed one; distinct geometries get distinct
keys) are the missing piece, and they are cheap.
