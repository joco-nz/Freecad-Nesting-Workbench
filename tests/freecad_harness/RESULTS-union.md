# The `union` phase — where the work actually is, and what is (not) removable

`union` is `unary_union(minkowski_parts)` at the end of `minkowski_sum`. It reads
as **71% of NFP time** on both the n70 workload and the heavy synthetic corpus,
and it is not on `make-faster.md`'s optimisation list — which targets
`convex_sum` (22–25%) with "replace pairwise vertex generation with a linear
convex Minkowski algorithm".

So: measure before code, per the notebook's own rule.

## The counters

Added to `minkowski_utils` and accumulated by the engine's existing phase
machinery: `union_calls`, `union_inputs`, `union_inputs_max`, `union_outputs`,
`union_result_vertices`, and a power-of-two input-size histogram
(`union_input_pow2_N`).

`union_ms` on its own cannot be acted on — it says 21 seconds went here, not
whether that is one enormous call or a million small ones, which have completely
different fixes.

Measured on the heavy corpus (min of 3, `work counts exact`):

| | |
|---|---:|
| union calls | 120 |
| union inputs (total) | 445 616 |
| union inputs (max, one call) | **54 756** |
| union outputs (total) | 120 |
| union result vertices (total) | 6 337 |
| `union_ms` | 21.0 s of 29.5 s NFP |
| per input | 47.2 µs |

Input-size distribution:

```
      1..        1     80 calls
    128..      255     32 calls
  32768..    65535      8 calls
```

**8 calls out of 120 hold 98.4% of the inputs** (438 048 of 445 616). They are
the HeavyPlate against itself: 234 × 234 = 54 756, at 8 rotation angles. The
other 112 calls are 1-to-255 inputs and are free.

`union_inputs` equals `convex_pairs` exactly, and `union_outputs` equals
`union_calls` — every union returns exactly one polygon, the NFP. Both are now
asserted on every harness run.

## The worst call, in detail

```
inputs                 54,756
input vertices        345,594
distinct WKB          54,750    (0.0% exact duplicates)
distinct envelopes    26,729    (51.2% share one envelope)
sum of input areas  241,048,453
result vertices           58
vertex retention        0.02%
area retention          0.37%
```

**99.98% of the input geometry does not appear in the answer.** That is the
finding that looked most promising and turned out to be the least actionable.

## What was tried, and what it cost

**Exact-duplicate removal.** 6 duplicates in 54 756. Worthless.

**Envelope-containment filter.** Sound in principle — an input whose envelope is
covered by another's cannot contribute anything new — but with 51% of inputs
sharing an envelope, each `STRtree.query` returns thousands of candidates. On
the worst call that is O(n²), roughly 3 billion comparisons. Not viable, and
measuring that was cheaper than attempting it.

**Calibration, which overturned the hypothesis.** The assumption was that heavy
overlap was the cost. It is not:

| 54 756 boxes | time | per input |
|---|---:|---:|
| disjoint, grid layout | 8 266.95 ms | 150.98 µs |
| fully stacked on one spot | 978.26 ms | 17.87 µs |

**Disjoint input is 8.4× *slower* than fully overlapping input.** GEOS's cost
here is driven by the size of the *output* — a union of 54 756 disjoint boxes
must build a 54 756-component MultiPolygon — not by merge difficulty. Our real
call produces a 58-vertex output, which is the cheap end.

At 47.2 µs per input against 17.9 µs for trivial boxes, and with the real
polygons averaging 6.3 vertices rather than 4, **the union is running at roughly
GEOS's normal rate. It is not pathological.**

## Why the 54 756 pair results are not waste

The input count is `parts_a × parts_b` — every convex piece of A against every
convex piece of B. That is not an implementation artefact:

> If `A = ∪ᵢ Aᵢ` and `B = ∪ⱼ Bⱼ` and `p = a + b` with `a ∈ Aᵢ` and `b ∈ Bⱼ`,
> then `p ∈ Aᵢ ⊕ Bⱼ`. Hence `A ⊕ B = ∪ᵢⱼ (Aᵢ ⊕ Bⱼ)`.

Minkowski sum distributes over union, so the union of all pairwise sums **is**
the exact NFP. There is no smaller correct set of inputs at this decomposition
granularity.

## So where the opportunity actually is: upstream

`convex_pairs` is `n × m`, and it is quadratic in the decomposition. For the
HeavyPlate that is 234 × 234. But a plate with 36 *rectangular* holes does not
need 234 convex pieces — a minimal convex partition is roughly 4 frame pieces
plus one ring per hole, so ~40. At 40 × 40 the pair count would be **1 600
instead of 54 756, a 34× reduction**, and the union would fall with it.

`decompose_if_needed` triangulates — its own docstring says "into convex parts
(triangles)" — so its output scales with the discretised outline's vertex count
rather than with the number of genuine features. Measured: **234 pieces for 36
holes** (heavy), **253 for 13** (n70 Spacer). Roughly 6.7 and 19 pieces per hole.

That makes the decomposition granularity the lever, not the union. The union is
the largest *line item*; the decomposition is the *cause* of the line item.

Two caveats before anyone acts on this:

- A triangulation and a minimal convex partition have the same area and the same
  union, so this would not change the nesting. But it is a real geometric change
  to code that every NFP depends on.
- Per `probe_union_determinism.py`, the union is **deterministic given the same
  input** (identical WKB across calls, and under reordering) but **not invariant
  to which redundant inputs you include** — removing 6 duplicates moved the area
  by 3.5e-10 mm² on an 880 000 mm² polygon, i.e. double-precision epsilon. So
  any input-reduction change perturbs the NFP at the last bit, which can flip a
  candidate tie and change the packing. The A/B for such a change must gate on
  the packing result, not only on wall clock.

## Summary

| question | answer |
|---|---|
| where is the work? | 8 calls of 54 756 inputs; 98.4% of all inputs |
| is it reducible at the union? | no — dedup finds 6, containment filter is O(n²) |
| is it slow per input? | no — 47 µs against GEOS's ~18 µs floor for trivial boxes |
| are the inputs necessary? | yes, at this granularity — Minkowski distributes over union |
| where is the opportunity? | the decomposition: 234 pieces for 36 holes, quadratic in the union |

---

# Second edition — the lever was here all along, and main already had it

Everything in the first edition stands. The union is not reducible *at the
union*: 6 exact duplicates in 54 756, the containment filter is O(n²), GEOS runs
at its normal rate, and the 54 756 inputs are mathematically required at that
granularity. The last row of that summary named the real lever — the
decomposition, 234 pieces for 36 holes, quadratic in the union.

## What was actually wrong: this branch, not main

The decomposition is a Delaunay triangulation of the polygon's **vertex set**, so
piece count is O(vertices) rather than O(features). The heavy plate has 4
exterior vertices and 36 holes of 4, giving 148 vertices and 234 pieces, against
a convex partition of about 40 for a rectangle with 36 rectangular holes — a
constant 7.1–7.7× over-partition, at every grid size measured. Since
`minkowski_sum` evaluates every ordered pair, and union and pair-merge cost are
both flat per pair (46.8–50.2 µs measured from 19k to 73k pairs), that 4.3× is a
~19× cheaper NFP.

**`main@eac1e30` already merges.** `_merge_convex_parts` at
`minkowski_utils.py:20`, called at line 121 inside the decomposition, with a
docstring stating the same argument: *"piece count is quadratic in run time …
Merging is union-preserving, so the covering guarantee `decompose_if_needed`
depends on is unchanged."*

This branch had none of it. The earlier attempt at this work was `c43b0a1` — the
same idea, O(n³) because it restarted its whole scan after every merge, 13 774
`unary_union` calls for 234 pieces, reverted per `make-faster.md` and leaving
only the `merged_pieces` plumbing dead at zero. main later shipped a working
version of the same operation.

So the 0.626× below is **not a gain over main. It is the recovery of a
regression this branch was carrying**, measured against its own pre-port state
and never against upstream. The first edition of this file was framed as
"irreducible at the union, lever is upstream" — which was right, and the reason
it is right is that upstream had already pulled that lever.

## The port

`main`'s `_merge_convex_parts` verbatim, with its call site, keeping this
branch's dead-ring pruning, in-flight decomposition coordination and
`decomposition_stats`. Those three are branch capabilities main does not have;
the merge is main's.

A branch-local rewrite was written first, and dropped. Both were run on the real
n70 parts, on identical input:

| polygon | holes | raw | main pc | main ms | local pc | local ms | symdiff |
|---|---:|---:|---:|---:|---:|---:|---:|
| Spacer | 13 | 442 | 194 | 541.2 | 193 | 48.4 | 0.0 |
| Bottle Top | 3 | 72 | 25 | 17.4 | 27 | 7.8 | 0.0 |
| Bottle Bottom | 2 | 67 | 22 | 14.6 | 24 | 7.2 | 0.0 |
| Sketch | 0 | 107 | 18 | 25.9 | 19 | 11.6 | 0.0 |

The local version was an adjacency-restricted pass (shared boundary edge only,
O(E) with union-find) and it is 2–11× cheaper to compute. It was dropped
anyway, for two reasons the measurement supplied:

- **The piece counts are a wash.** Within ±2, and not uniformly in the local
  version's favour — coarser on the Spacer, *finer* on both bottle parts. So the
  NFP work, which is quadratic in the count, is unaffected.
- **The cost difference is not worth a fork.** `decompose_if_needed` memoises on
  WKT, so the merge runs once per distinct polygon per process. On the n70 GA
  configuration that is ~0.5 s of a 128 s run. Carrying a divergent copy of a
  function main already ships is a fork repaid on every rebase.

Converging also turned out to be *functionally* better, not merely tidier. The
local version had a `min_pieces=32` threshold; main's has none, so it also
coarsens small concave parts. On the GA synthetic corpus that is the difference
between **3 332 and 164** convex pairs — 20× — for the same packing.

One apparent difference turned out not to be one. Main's pass guards with
`or u.interiors`, which looks stricter than the local version's. It is
unreachable: the union of two convex sets is always simply connected, so
`u.interiors` can never be non-empty for inputs this pass merges. Removing the
guard leaves the suite green, which is how that was established rather than
assumed.

## Result on this branch

Heavy corpus, one nest:

| | before the port | after | |
|---|---:|---:|---|
| `nfp_work_convex_pairs` | 445 616 | 19 256 | −95.7% |
| `nfp_work_convex_pairs_max` | 54 756 | 2 209 | −96.0% |
| `nfp_work_convex_result_points` | 2 634 854 | 258 076 | −90.2% |
| `nfp_work_parts_a_max` | 234 | 47 | −79.9% |
| `nfp_work_merged_pieces_a` | 0 | 7 480 | the dead counter, now reporting |
| `nfp_phase_union_ms` | 21 172 | 1 060 | −95.0% |
| `nfp_phase_total_ms` | 29 992 | 3 040 | −89.9% |
| **wall** | **59.4 s** | **37.2 s** | **0.626×** |
| sheets / placed / density | 1 / 122 / 0.660024 | **identical** | |

n70's Spacer goes 253 → 74 pieces.

## On the n70 GA configuration, none of this shows

A GA run of the notebook's configuration (1200×600, 2 Spacer + 60 + 60,
8 generations, population 4) measures **the same on main and on this branch**,
and the reason is in the `[GA PERF TOTAL]` line:

```
nfp_compute=6.04s        of  generations=132.59s     ->  4.5%
collision_intersection=171.13s   within  candidate_wall=250.03s
exact_collision_checks=573233   nfp_misses=82
```

NFP construction is 4.5% of that run. The expensive Spacer×Spacer NFPs are
computed **once** — 82 misses — and then hit 26 468 times across 3 338 rotations
and 19 layouts. NFP cost is front-loaded in a single cold nest and amortised in a
GA run, and the 0.626× is a single-cold-nest number. Both arms already merge, so
both land on the same total.

The `*_s` figures there are sums of per-worker times, not wall shares
(`collision_intersection=171.13s` against a 132.59s run gives it away), so read
the ordering rather than the ratios. `nfp_misses=82` against
`exact_collision_checks=573233` are plain counts and make the same point
structurally.

## On the NFP being unchanged

Minkowski sum distributes over union, so `∪ᵢⱼ(Aᵢ⊕Bⱼ)` is the same region for
*any* convex partition of A and of B. Both partitions here are exact: piece areas
sum to the union area to the last decimal, nothing overlaps, no piece is
non-convex, none strays outside the source polygon, and `_minkowski_merge_ring`
is exact to ~1e-13 for 3 to 64 vertices. Symmetric difference between the
partitions built by the two implementations is exactly 0.

Bit-identity of the two NFP *polygons* is not true, and an earlier draft of this
file asserted it was. Areas agree exactly (120 × 108 = 12 960, six decimals) but
the boundaries carry mirror-image slivers of ~39 mm² on 12 960 — 0.6% — with 19
boundary vertices against 12. Floating-point noise from unioning many small
triangles against fewer merged ones, in both directions.

Two measurement traps produced a false alarm first, both now pinned in the
tests:

- **`equals()` is unusable on NFPs.** GEOS keeps different collinear boundary
  points depending on the input, so geometrically identical NFPs can differ in
  vertex count and `equals()` reports `False`. Compare `symmetric_difference` or
  area.
- **Rotating each convex piece about its own centroid is not the production
  transform.** `_transform_convex_parts` rotates about one common origin. Per
  piece, they are displaced independently, so the union is a scattered set
  rather than a rotated copy — and the error is larger for a few large pieces
  than for many small ones, which made a correct merge look like it changed the
  NFP by 9%.

## Summary, second edition

| question | answer |
|---|---|
| is the union reducible at the union? | no — unchanged from the first edition |
| was this branch behind main on the decomposition? | **yes** — main shipped the merge; the branch had a reverted O(n³) attempt |
| does the decomposition merge work? | yes: 445 616 → 19 256 pairs, 59.4 s → 37.2 s, packing identical |
| is that a gain over main? | **no** — it is a regression recovered, and main already had it |
| why does it not show on the n70 GA config? | NFP is 4.5% of that run; the cost is amortised over 19 layouts |
| where is the time on the config that matters? | the candidate stage, and collision within it |
