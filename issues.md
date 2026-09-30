# Issues

General problem tracker for the FreeCAD Nesting Workbench. Anything worth
remembering, comparing against, or referring to later goes here — not just
things being worked on right now.

This is deliberately *not* a task list. `CHANGELOG.md` records what changed,
`master-merge.md` and `make-faster.md` record investigations, and this file
records the things that are still true and still need deciding.

## How to use

- **One issue per problem**, with a stable ID. Refer to issues by ID in commit
  messages, code comments and pull requests so the link survives.
- **IDs are never reused and never renumbered.** An ID that stops appearing
  because it was closed still means the same thing forever; the status changes,
  the ID does not.
- **Next ID is the highest one issued plus one.** Gaps are fine and expected —
  do not backfill them.
- **Record the measurement, not the conclusion.** Every entry below carries
  file/line references or numbers, because a plausible-sounding summary of why
  something is wrong is worth much less than the evidence when someone picks it
  up in six months.

### ID scheme

`NEST-###`, zero-padded to three digits. The prefix keeps it greppable and stops
it colliding with GitHub's own `#123` references when both appear in the same
sentence.

### Status vocabulary

| status | meaning |
|---|---|
| `open` | confirmed, not being worked on |
| `investigating` | being measured or designed right now |
| `fixed` | resolved; keep the entry, set the date and the commit |
| `wontfix` | deliberately not being done; keep the reasoning, it is the useful part |
| `limitation` | known and accepted, not a defect. Something that is true and fine |

Severity is impact on a user running a normal job: `high` produces a wrong or
silently-wrong result, `medium` loses a control or a setting, `low` is
inconvenience or documentation.

### Line references

File:line references below were checked against the working tree when the entry
was written, and they do go stale as code moves. When picking one up, treat it
as a hint to the right function rather than as the exact place — prefer naming
the symbol when you edit the entry.

## Summary

| ID | title | status | severity | area |
|---|---|---|---|---|
| [NEST-001](#nest-001) | Nesting direction is recorded from the Minkowski dial whatever algorithm ran | open | high | persistence |
| [NEST-002](#nest-002) | Per-part rotation override is read but never written | open | medium | persistence |
| [NEST-003](#nest-003) | Workbench and benchmark disagree on the simplification default | open | medium | benchmark |
| [NEST-004](#nest-004) | Synthetic heavy corpus is calibrated to a piece count production no longer produces | open | medium | benchmark |
| [NEST-005](#nest-005) | Simplification destroys the rotational symmetry of part outlines | limitation | low | geometry |
| [NEST-006](#nest-006) | Deflection tooltip states unmeasured ranges | open | low | docs |
| [NEST-007](#nest-007) | Nesting converts analytic source geometry to B-splines to centre it | open | medium | geometry |
| [NEST-008](#nest-008) | Hole-nesting detection costs 148 ms per part on real geometry | open | medium | performance |

---

## NEST-001

**Nesting direction is recorded from the Minkowski dial whatever algorithm ran**

`status: open` · `severity: high` · `area: persistence`

Set the Physics direction dial, run the Physics algorithm, and the layout saves
a direction that is not the one that produced the nest.

Three places that should agree do not:

| role | location | dial used |
|---|---|---|
| direction a Physics run actually uses | `nesting_controller.py:919` | `physics_direction_dial` |
| direction written onto the layout | `nesting_controller.py:805` | `minkowski_direction_dial` |
| direction restored on reopen | `nesting_controller.py:263` | `minkowski_direction_dial` |

The write is `nesting_job.py:231` (`PROP_NESTING_DIRECTION`), which is populated
from the unconditional read at `:805`.

The asymmetry behind it: the Minkowski dial is over-persisted — it is written
even for Physics runs — and the Physics dial is under-persisted, with no
`PROP_PHYSICS_DIRECTION` constant and no entry in `save_settings`, so its
position is never recorded anywhere at all. Reopening a Physics layout leaves
the Physics dial at whatever it currently holds.

Long-standing, and not introduced by the direction-button work. It was less
visible while both dials defaulted to the same value, because the wrong dial
usually held the right answer by coincidence.

**Fix direction.** Record the value that was actually used — branch on the
algorithm at `:805` the way `:807` already does for `use_random_direction` —
and give the Physics dial its own persisted property so a Physics layout
round-trips. Both halves are needed: fixing only the read still leaves the
Physics setting unrecoverable, and fixing only the storage still writes the
wrong value.

---

## NEST-002

**Per-part rotation override is read but never written**

`status: open` · `severity: medium` · `area: persistence`

`nesting_controller.py:323` reads a per-part override:

```python
steps_map[label] = getattr(master, "PartRotationSteps", 0)
```

No `addProperty("PartRotationSteps", ...)` exists anywhere in the codebase, so
the attribute is never present and the read always yields `0`. A user who sets
a rotation override for one part type in the shape table gets a nest that
honours it, and a reopened layout that silently reverts to the global value.

A control that appears wired and is not is worse than one that is absent, which
is what the shape table's "Override" column amounts to today.

**Fix direction.** Either write the property in the same place the quantity and
`FillSheet` metadata are written (`shape_preparer.py:421-437`), or stop reading
it. Writing it is the better outcome since the control already exists.

---

## NEST-003

**Workbench and benchmark disagree on the simplification default**

`status: open` · `severity: medium` · `area: benchmark`

The two halves of the project run at different geometry:

| | value | location |
|---|---|---|
| workbench default | `1.0` | `ui_nesting.py:109`, prefs default at `:738` |
| benchmark harness default | `0.1` | `nest_benchmark.py:181`, documented at `tests/freecad_harness/README.md:67` |

So every committed baseline describes a different set of polygons from the ones
a user actually gets. That is the same class of problem the harness already
guards against for the candidate-geometry cache, where
`nest_benchmark.py:191-199` deliberately treats "unset" as "follow the
workbench default" for exactly this reason. The simplification dial does not
get that treatment.

**Fix direction.** Give the harness the same tri-state treatment, defaulting to
the workbench value, and re-record the committed baselines. Expect the
baselines to move: at 0.1 the n70 corpus runs ~32% slower than at 1.0, so any
gate recorded against the old setting is currently measuring something users do
not run.

---

## NEST-004

**Synthetic heavy corpus is calibrated to a piece count production no longer produces**

`status: open` · `severity: medium` · `area: benchmark`

`harness_common.py:116` sizes the synthetic heavy corpus against the real
Spacer's 229 convex pieces, and `harness_common.py:159` picks a 6×6 hole grid
measured at 234 pieces to land within 8% of that.

Measured at the production default of `simplification=1.0`, the real Spacer
decomposes to **54** pieces, not 229. So the corpus benchmarks a workload
roughly 4× heavier than the geometry production actually feeds the NFP, and
every "this is expensive" conclusion drawn from it is measured against the wrong
baseline.

**Fix direction.** Re-measure the real corpus at the production default and
resize the grid, or state the multiplier on the corpus so nobody reads its
timings as production timings. Note that convex-piece count is a proxy: the
Spacer drops 1681 → 54 pieces (a 970× reduction) while wall clock drops only
about 1.3× above `simplification=0.1`, because the NFP is not as pair-dominated
as the count suggests. The proxy has misled this repo before —
`harness_common.py:100-124` records an earlier version that got the same dial
wrong.

---

## NEST-005

**Simplification destroys the rotational symmetry of part outlines**

`status: limitation` · `severity: low` · `area: geometry`

Recorded so the investigation is not repeated. Rotational-equivalence
optimisation — dropping candidate rotations that produce an already-tried shape
— is not feasible on real geometry, for a reason that is not about dead
internal holes.

On the n70 corpus, the two bottle parts are exactly 2-fold symmetric as
hole-free outlines, and lose that entirely at `simplification=0.1` and above:

| simplification | vertices | detected order | relative asymmetry |
|---|---|---|---|
| 0.0 | 264 | 2 | 0.0 |
| 0.1 | 36 | none | 5.2e-04 |
| 1.0 (default) | 12 | none | 1.5e-03 |

`simplify()` is Douglas-Peucker, which picks vertices greedily from the start of
a ring, so opposite sides of a part end up with different vertex sets. The
Spacer is asymmetric at every tolerance, as expected.

So any dedup would have to detect on a higher-fidelity polygon and apply the
reduction to the coarse one, resting on a measured 5.2e-04 relative residual
rather than on exact equality. Combined with a detection pass that costs time
on every run and returns "no symmetry" for every part in both corpora, the
measured value was zero.

Related: dead internal holes are a genuine but separate problem. They would need
the rotation *pivot* to be named explicitly rather than re-derived from
`.centroid`, because the whole nesting frame is translation-invariant — moving
the stored polygon does not move the point `rotate(..., origin='centroid')`
turns about.

---

## NEST-006

**Deflection tooltip states unmeasured ranges**

`status: open` · `severity: low` · `area: docs`

`ui_nesting.py:94-107` tells the user "10° is good for most parts. Use 5° for
precision, 30°+ for speed", with no measurement behind it. The Simplify tooltip
immediately below it now carries a measured wall-clock/yield table from a
122-part nest, so the two controls read as though they were equally grounded
when only one is.

Not urgent, and deliberately left alone rather than given invented numbers.

**Fix direction.** Sweep `NEST_BENCH_DEFLECTION` the way the simplification
sweep was done and put the real curve in, or soften the claims to match what is
actually known.


---

## NEST-007

**Nesting converts analytic source geometry to B-splines to centre it**

`status: open` · `severity: medium` · `area: geometry`

Every 3D part that goes into a nest arrives at the layout as a B-spline solid
even when the user modelled it from planes and cylinders. Measured on the
replay fixture, whose source bodies are `PartDesign::Body` objects built from
sketches:

| object | surface types |
|---|---|
| source body `SimpleSpacer` | `Cylinder`, `Plane` |
| `master_shape_SimpleSpacer` | `BSplineSurface` |
| `part_SimpleSpacer_1` (nested) | `BSplineSurface` |

The conversion happens in `ShapePreparer._center_3d_shape`, which centres the
shape for the nester and does it with `transformGeometry`. That is the one
Part API that re-fits geometry rather than moving it, so a rigid translation
comes back as a spline approximation. Measured on the same part:

    transformGeometry   119 ms  ->  ['BSplineSurface']
    transformShape       11 ms  ->  ['Cylinder', 'Plane']

Ten times the cost, and the analytic surfaces are gone. `transformShape` with
a rigid matrix is the operation that was wanted.

This is not confined to the nester. `cam_manager` uses `transformGeometry` in
four places, at `:100`, `:107`, `:117` and `:122`, for exactly the same reason
-- placing geometry for CAM. So the two halves of the workbench share the
mistake independently.

Why it matters rather than being cosmetic:

* **CAM cuts from the approximation.** A profile op offsets the toolpath from
  the real surface. Against a splined cylinder that is an approximation of
  what the user drew. Measured on a cylinder before and after: face area
  314.1593 -> 315.0023, a 0.27% shift from a placement that should have been
  exact.
* **It is 1.8x slower to section.** `shape.slice()` on the nested geometry
  measured 287 ms against 158 ms on the analytic source, which is what makes
  [NEST-008](#nest-008) expensive.
* **It is the same trap the CAM replay already avoids.** `cam_replay` applies a
  `Placement` for precisely this reason, and says so in its module docstring.
  The nester and `cam_manager` predate it and do not.

Not demonstrated to produce a wrong part in any real job, hence medium rather
than high. The 0.27% figure is the honest measure of the error and the user
should weigh it against their own tolerances -- on the 1.2 mm kerf plasma
workload in the replay fixture it is nothing, and on a precision mill it is
not.

**Fix direction.** `transformShape(matrix, copy=True)` in
`_center_3d_shape`, and in the four `cam_manager` call sites. Both are
localised. It needs checking that `transformShape` does not reorder or
re-index faces on a rigid motion, because `cam_replay` and `cam_manager`
both depend on stable sub-element names -- `transformShape` is documented to
preserve topology for a rigid motion where `transformGeometry` may not, which
is the reason to prefer it, but it should be confirmed against the n70 corpus
rather than assumed.

---

## NEST-008

**Hole-nesting detection costs 148 ms per part on real geometry**

`status: open` · `severity: medium` · `area: performance`

`cam_replay.find_hole_nestings` builds a 2D footprint of every part by
cross-sectioning it, then tests every part against every other part's interior
rings. Measured on the replay fixture, 48 parts, one sheet:

| stage | cost |
|---|---|
| `part_footprint`, per part | 148 ms |
| `part_footprint`, 48 parts | 7.1 s |
| `find_hole_nestings`, 48 parts | 7.0 s |
| projected to n70's 122 parts | **~36 s** |

That projection is the problem. The replay is a one-time command, so 7 s for
one sheet is survivable, but the cost falls entirely on the hole-nesting check
and grows linearly with part count.

**The cost is `slice()`, not discretisation.** Six parts measured:

    slice          1722 ms
    discretize       4 ms

And the discretisation tolerance is irrelevant to it -- 1695 ms at deflection
1.0 against 1754 ms at 0.05, because the section is the cost. So loosening
tolerance, which is the obvious thing to reach for, buys nothing.

Part of the cost is [NEST-007](#nest-007): the nester's B-spline conversion
makes slicing 1.8x slower. But slicing is expensive on this geometry either
way -- 158 ms on the analytic source -- so that is a contributing cause, not
the whole of it.

**Recorded dead end: the bounding-box pre-filter does not work.** The obvious
optimisation is to skip parts that cannot be nesting, since part B can only sit
inside a hole of part A if B's bounding box fits within A's. On the fixture
that filter keeps **48 of 48** parts, because the 23 `BottomStrap` and 23
`TopStrap` copies are long and thin and overlap each other in extent. The
filter costs 596 ms for 2304 pairs and eliminates nothing. Worth writing down
so the idea is not retried on strap-like parts.

**Fix direction.** Footprint the *master* rather than each copy. Every copy of
a part type is the same shape at a different placement, so the fixture's three
types need three sections rather than 48 -- roughly a 16x cut, and it stays
inside `cam_replay` with no change to the nester. The copies' positions are
already known from their container placements, so containment is unaffected.
Worth measuring before committing to it: the master shapes are B-splined too,
so each section is still ~287 ms and the estimate is 3 x 287 ms, not 48 x 287.

Not recorded as a limitation, because a 16x reduction looks available and has
simply not been built.
