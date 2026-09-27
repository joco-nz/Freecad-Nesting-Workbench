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
