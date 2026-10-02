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

## NEST-009 — a replayed Boundary dressup clips against the SOURCE job's stock

Found by extending `test_replay_dressups.py` to a three-deep stack
(LeadInOut -> Dogbone -> Boundary), the shape FreeCAD's own `dressuptest.FCStd`
uses. It is a defect, not a limitation, and it is **not fixed** — the two
obvious fixes are both wrong, which is why it is written down rather than
patched.

**What is wrong.** Boundary's `Stock` is an `App::PropertyLink` to a solid, and
`capture_properties` takes it verbatim. The replayed Boundary therefore clips
against the *source job's* stock: fitted to the source part, in the source Z
frame, at the source part's position. Measured:

    source Boundary:       11 cuts, Stock -> the user's own stock solid
    replayed Boundary:     11 cuts, Stock -> still the source job's object

A cross-job link resolves, so nothing raises and the job looks fine.

**Why it is easy to miss.** On the first sheet the parts are at the origin and
the source stock is fitted to a part that is also near the origin, so the two
nearly coincide. The replay is wrong and looks right. On any later sheet the
parts have been moved to sheet-local coordinates and the boundary is still
where the first sheet's part was.

**Fix 1, tried and rejected: repoint `Stock` at the replay job's own stock.**
This looks right — the sheet *is* the boundary, and decision 3 already makes
the job's stock the sheet. It is worse. The sheet stock spans `-thickness .. 0`
and the contour is cut at z 0, so the cut edge lies exactly on the stock's top
face and `PathBoundary`'s `edge.common(shape)` degenerates. Measured on the
three-deep step: **30 cutting moves before, 0 after.** The Boundary goes from
cutting to cutting nothing.

**Fix 2, not tried: copy the user's stock into the sheet's frame.** `doc.
copyObject` plus a translation by the sheet origin. Preserves the user's
boundary, which is the point of a replay, and avoids the degeneracy because a
`CreateFromBase` stock spans z -3..1 with the cut strictly inside. Costs a copy
per sheet and needs the sheet origin, which the replay already has.

**Current state: reported, not fixed.** `unmapped_job_links` finds any job-local
link the replay carried across without remapping, and the sheet outcome carries
a warning naming the replayed dressup, the property and the object.
`test_replay_dressups.py` asserts the warning is emitted, which is the
mitigation — a Boundary dressup is **not safe to post from a replayed job**
until this is resolved, and the assertion is what stops the mitigation being
quietly deleted. `DRESSUP_JOB_LINKS` is the empty map that would hold the fix.

**Also worth knowing, found while setting the test up.** Boundary clips with
`edge.common(shape)`, and an edge commoned with a planar *face* in 3D returns
nothing: 0 cutting moves against a 400x200 face, 11 against a 400x200 box. The
property is documented as "Solid object", so a face is user error, but it fails
by producing an empty path rather than an error. And `Inside=True` against the
constructor's own `CreateFromBase` stock clips the contour away entirely,
because that stock is barely larger than the part.

## NEST-010 — the flattened parts could not be removed: the job's clones were links

**RESOLVED.** The link was the whole problem, and it was removable.

Raised as "the parts group can go once copies are made into the Model folder".
Tried, measured, and reverted.

**A `draftobjects.clone.Clone` is a link, not a copy.** Its `Objects`
property points back at the object it was cloned from, and the clone
re-evaluates from it. `PathJob.Create` replaces the geometry passed to it with
these clones, so the replayed job's `Model` holds 48 objects that each depend
on a flattened part still being in the document.

Removing the flattened parts and their group leaves the document tidier and the
job wrong. Measured on the tracked fixture, 48 parts removed:

    Profile_replay        402 cmd  184 cuts
    Profile001_replay       0 cmd    0 cuts   <- the job now cuts less
    Profile002_replay     532 cmd  283 cuts
    Profile003..006_replay   0 cmd    0 cuts

Five of seven operations lost their toolpath. The clones still hold a cached
shape, so two operations happen to read it and the rest cannot resolve their
sub-element selections against it:

    Sub-element Edge100 resolved on 2 of 48

So the verification did its job: the sheet was labelled `_UNVERIFIED` rather
than passing with a silently shorter toolpath.

**Kept as-is, for now:** the flattened parts stay, in a group of their own. That
group is the tidying actually available — 48 objects in one place rather than
loose at the document root.

**If they are ever to go**, the change is to make the clones independent: bake
each clone's shape into a plain `Part::Feature` before handing it to the job, so
the job's geometry does not reference anything the replay created and then
discarded. That is a change to how the job's `Model` is built, not a cleanup,
and it has a cost — baked geometry does not follow the source part if the user
edits it, which is the thing a Clone is for.

**How it was actually resolved.** The baking was never necessary, because the
Clone was not buying anything. CAM reads a shape and resolves sub-element names
against it; it does not care that the shape arrived by reference.
`adopt_flattened_parts_as_model` promotes the flattened parts into the Model and
drops the Clones, so there is no link to keep alive and no group to hold the
parts for as long as the job exists.

Measured on the tracked fixture, 48 parts:

    Used the 48 flattened part(s) directly as the job's geometry.
    Model now: 48 entries, type Part::Feature, linking out to []
    Verified 7 operation(s), all with cutting motion.  0 failures, 0 warnings
    Document root: 4 objects.  713 -> 712, the empty staging group gone too.

And the claim the fix rests on, against a file on disk rather than a live
session — save, close, reopen:

    after reload:    7/7 operations with cutting motion
    layout deleted:  7/7 operations with cutting motion, 263 objects

The layout is genuinely not load-bearing any more. The job is a snapshot of the
nest as it was when the command ran, and it always was — what changed is that
the snapshot now lives in the job instead of in a sibling group that had to be
kept for the job's sake.

Two things it cost, both measured rather than assumed:

* `clones_for_source` read `NestedLabel` via `clone.Objects[0]`, a Clone-only
  path. Given a plain `Part::Feature` it matched nothing, returned every entry,
  and 5 of 7 operations stopped cutting — the same failure mode as the original
  NEST-010, reached from the other direction. Fixed by `nested_label_of`, which
  reads the label off the entry itself and falls back to the link. Pinned by
  `TestNestedLabelOf` and `TestModelIdentitySurvivesTheSwap`, and visible in the
  fixture validator, which now reports `nested types: TopStrap` where it
  reported `(none)`.
* `ReplayJob` no longer needs `source_parts`. The two names described one set of
  objects and are now one list, so the second was removed rather than kept as a
  way to be wrong twice.

**Two dead ends recorded so they are not retried.** Asking whether a part is
still alive does not work either way: `part.Document` still answers for a
removed object, so a `source_parts` trim on that basis kept all 48 dead
references; and `part.Name` raises `Cannot access attribute 'Name' of deleted
object`. Only asking the document for its live object names works.

## NEST-011 — two toolbar buttons had the same icon

`command_create_cam_job.py` and `command_replay_cam.py` both declared
`Nesting_CNC_Icon.svg`, and the two commands are adjacent in both the menu and
the toolbar (`init_gui.py:52-53`). Six distinct icons for seven commands.

The obvious fix — give it a distinct picture — turns out to have a house style
to match, and that style is stricter than it looks. The six existing icons are
**1448 rectangles and nothing else**: one `<path>` per colour, every shape an
axis-aligned `M x y h w v h h -w z` on a 48x48 grid. No curves, no strokes, no
gradients, no transforms, no `<g>`. So it is a fully specified format and can be
generated rather than hand-placed.

**Fixed** with `Resources/icons/Nesting_Replay_Icon.svg` — a replay ring around a
2x2 nest, one part of which is the source. 230 rectangles, 7 colours, 3549 bytes,
in the same format as the file it sits beside.

Three measurements shaped it, and none of them were what I would have guessed:

* **The stroke weight is the house style, not a preference.** The existing icons
  outline at 2 units. A first pass at 4 units read as a blob next to them.
  Related: FreeCAD renders a 48-unit grid at `width="64"` down to the toolbar,
  so at a 16px toolbar one unit is a third of a device pixel and a 2-unit
  stroke is already one device pixel. Nothing thinner can survive.
* **Silhouette decides legibility, not detail.** Rendered at 16/24/32/48: the
  fist and the stacked boards survive, the CNC icon does not. So the first
  candidates -- one part, arrow, three copies -- were dropped or simplified
  rather than refined. Two designs were cut entirely: a fan of dots, and a
  thick diagonal "cut path" that read as glare.
* **The palette came from CAM, as asked.** Tango, from the 125 icons in
  `~/dev/FreeCAD/src/Mod/CAM/Gui/Resources/icons`: `#fff110`/`#cf7008` for the
  material, `#8ae234`/`#73d216`/`#4e9a06` for a cut result, `#2e3436` for
  structure, `#ffffff` for the highlight. Every one is present in at least two
  of those 125 -- `#8f5902` in two, `#8ae234` in 37. A first pass used an
  invented `#8f5005`, which appears in no CAM icon and was replaced.

Eight candidates were generated and previewed at six sizes before one was
chosen; the generator is not in the repo, since the icons are assets and
regenerating them is not something the build does.

**Verified** by round trip: each SVG is parsed back with the same parser used on
the six existing icons, re-rendered, and compared cell by cell -- 0 differing
cells, and only `M`/`h`/`v`/`z` commands present. That check earned its keep
immediately by reporting all eight as MISMATCH when the fault was in the
validator.

**Verified in the GUI** by manual run, in both the enabled and disabled states.
That was the part no check here could reach: FreeCAD's Qt renderer antialiases,
so the real toolbar looks softer than the nearest-neighbour previews, and a
disabled command desaturates the icon, which is worth knowing about for a
palette carrying a saturated green and yellow. Both read correctly.

## NEST-012 — the replay takes 43 seconds, and 42 of them are the ordering step

Raised as "the command has no progress feedback while it works". Investigating
that found a much better answer.

`validate_replay_fixture` has timed `replay_layout` since early on: **43 s** for
one sheet of 48 parts with 7 operations. Because it loops over sheets, a
three-sheet nest is over two minutes of frozen UI.

**98% of it is one stage.** Measured with the `Progress` seam added for this
investigation (below), and independently by wrapping the suspect calls:

    Reading the source CAM setup               1 event     0.01s
    Flattening nested parts                   50 events     0.00s
    Building the replay job                    1 event     0.41s
    Replaying the recipe                      15 events     0.01s
    Ordering and tidying the tool table        1 event    43.00s
    Verifying the result                       2 events     0.05s

And within that stage, by wrapping `find_hole_nestings`, `order_operations` and
`verify_replay` and subtracting the named calls from the span:

    find_hole_nestings                                 7.44s
    order_operations                                  11.65s
    doc.recompute() between them                      23.33s
                                                    ----------
                                                     42.42s of 43.50s

**Nothing that costs time changes anything a user would notice.** The reorder
does not move a toolpath: reordering `Operations.Group` cannot alter what any
operation cuts, and the 23.33 s recompute exists only because those two calls
touched the document. So the ordering stage buys correctness that is already
there and pays for it with a full recompute of every operation's toolpath.

**Two measurement mistakes worth recording, because both produced confident
wrong answers.**

* Attributing elapsed time by *per-stage* timestamp rather than by the previous
  event gave the first event of each stage the whole elapsed run. That put
  43 of the 44 seconds on "Verifying the result", and verification costs
  **0.05 s**. Measured directly: `parts_outside_stock` 0.01 s, `.Path` reads
  0.00 s, `uncovered_targets` 0.03 s.
* The guess before either measurement -- that the cost was toolpath generation
  or flattening -- was also wrong. Both are under half a second.

`Document.recompute` is read-only on the `App.Document` type and cannot be
wrapped, so the recompute's cost is obtained by subtraction rather than
directly. `App.Document` is immutable; an instance attribute cannot be set.

**Not yet fixed.** A progress bar built on this seam would report six stages in
half a second and then sit on one stage for 43, which is reporting a symptom.
The work worth doing is in `find_hole_nestings` and in not recomputing when
`order_operations` changed nothing.

## NEST-012 — the replay took 43 seconds

Raised as "the command has no progress feedback while it works". Two thirds of
the time turned out to be doing the same work twice.

**Measured, on the committed fixture, `replay_layout` = 43.5 s.** The
instrument was the `Progress` seam: per-stage elapsed time attributed to the
previous event rather than to the last event of that stage. Keying it on the
stage name instead gave the first event of each stage the whole elapsed run,
which put 43 of the 44 seconds on verification -- and verification costs
**0.05 s**. The prior guess, that the cost was toolpath generation, was also
wrong. Both mistakes are recorded because both produced a confident answer.

**Where it actually went:**

    find_hole_nestings                            7.50s
    order_operations                             11.71s
    doc.recompute()  (the one that matters)       24.05s
    everything else, including five recomputes     0.06s
                                                ---------
                                                 43.50s

**The five "incidental" recomputes were not worth removing.** `adopt` twice,
`create_replay_job` twice, and the staging group, measured 0.003 s to 0.025 s
each -- 55 ms together. They are already effectively a single recompute, because
at those points almost nothing is touched yet. The proposal to collapse them
into one at the end was correct in principle and worth nothing in practice.

**`order_operations` was re-slicing shapes it had already been given.**
`operation_touches_hole` calls `part_footprint(geometry)` on every invocation,
and `order_operations` calls it inside a per-nesting comprehension. Measured:
**21 calls over 3 distinct shapes.** `part_footprint` slices with OCC and
discretises the wires -- 543 ms on the first call, ~150 ms after. That
duplication was 11.40 s of the 11.71 s. Fixed with `FootprintCache`, scoped to
one sheet's run: a module-level cache would go stale, since a shape can be
edited while its object `Name` stays the same and `id()` is reused after
collection.

**`find_hole_nestings` sliced all 48 parts to test pairs.** A footprint is
sliced out of its own shape, so it lies within that shape's extent; an inner
part inside an outer part's hole must therefore have an overlapping box. Every
pair whose boxes are disjoint can be dropped without slicing. Measured on the
fixture: **2256 ordered pairs reduced to 34, and 48 slices reduced to 21**,
with all 15 real nestings kept. On a nest with no hole nesting, where no two
parts' boxes touch, the cost is zero slices.

**Result: 43.5 s -> 27.5 s**, same 15 nestings, same 7 operations, same
verification, 0 failures. `21 sliced, 68 reused`.

**What remains is not waste.** The 24 s `doc.recompute()` executes seven
`Path::FeaturePython` operations -- seven `Profile*_replay` under seven
`DressupLeadInOut*_replay` -- over 23 nested parts each, which is 161 real
toolpath computations. Those happen exactly once: the earlier recomputes all
precede `replay_recipe`, so no operation exists yet when they run.

Note that touching the seven `Operations.Group` entries and recomputing costs
**0.05 s**, which looks like proof that no toolpath work happens. It is not:
those entries are the outermost *dressups*, and touching one does not propagate
to the operation beneath it. Both the profiles and the dressups are touched, and
the profiles are where the 24 s goes.

## NEST-013 — FreeCAD's Profile is superlinear in its number of base targets

Raised as "the remaining 24 s of toolpath computation might be the replay making
FreeCAD's work harder". It is not, and there is a 15x win available that costs
something the user has to weigh.

**What the 24 s is.** Seven `Profile*_replay` under seven
`DressupLeadInOut*_replay`, 161 real toolpath computations. Measured one at a
time, per operation, touching the *base* of each dressup rather than the group
entry -- `Operations.Group` holds the outermost dressup and touching that does
not execute the operation under it, which is what made two earlier probes
report 0.05 s and conclude wrongly that no toolpath work happens at all.

**It is FreeCAD's, not the replay's.** The source job's own `Profile001` --
the user's operation, untouched, with the user's own settings -- pointed at 23
targets costs **8.51 s**. The replayed one costs **8.89 s**. Switching
`UseComp` off changes nothing (8.59 s), so 3D projection is not the driver, and
rotating a part 45 degrees costs the same as leaving it upright (0.02 s), so the
nester's placements are not the driver. `HandleMultipleFeatures` is already
`Individually` on both jobs, so batch mode is not the driver.

**The cost per part rises with the number of parts**, which is the finding:

    Profile001, 8 sub-elements, per-target cost

      1 target    0.018s        5 targets   0.083s
      2 targets   0.036s       23 targets   0.370s

Roughly quadratic. The replay inherits it exactly; there is no replay-specific
waste left in this path.

**The available win: one operation per part.** Measured, three passes each,
forcing a recompute every time:

    A. one Profile, 23 targets      8.85 / 8.68 / 8.55 s
    B. 23 Profiles, 1 target each   0.11 create + 0.47 / 0.46 / 0.47 s

**8.7 s to 0.58 s, a 15x win**, and stable across passes. An earlier version of
this probe reported 18.3x by putting operation creation and the first recompute
in one bucket and comparing it against a later forced recompute; the two are
apart now and 15x is the honest figure.

**What it costs, measured.** First measurement put it at 577 commands against
621, +7.6%, and guessed it probably meant more table time. That guess was wrong,
and the 7.6% was measured without dressups; with them it is +12.7%.

Measured on the real fixture by concatenating the paths as the post processor
would, including the move from one operation's end to the next:

    A. one operation, 23 targets        347 cmd  68 rapid ( 1120 mm)  253 cut (3760.5 mm)

    B. split into 23, recipe order      391 cmd  68 rapid ( 3643 mm)  253 cut (3767.0 mm)
       B. split, sorted by position     391 cmd  68 rapid ( 2043 mm)  253 cut (3767.0 mm)
       B. split, reversed               391 cmd  68 rapid ( 3655 mm)  253 cut (3767.0 mm)

    added over one operation:
       recipe order    rapids +2524 mm    cutting +6.5 mm  (+0.17%)
       nest order      rapids  +924 mm    cutting +6.5 mm  (+0.17%)

**Cutting is unchanged: +6.5 mm on 3760 mm, +0.17%.** No extra material work, and
no extra torch-on time. The reason is that LeadInOut already inserts one
lead-in per *target*, not per operation -- measured 230 cut moves on the bare
Profile and 253 with the dressup, across 23 targets, so exactly one per part in
either arrangement. Splitting does not add a 24th.

**The rapid count is identical at 68.** Only the distance changes, and only
because one operation links between adjacent parts more tightly than 23
operations with boundaries between them. 0.9 to 2.5 m of extra torch-off travel
against 3.8 m of cutting.

**Operation order matters more than anything else here**: sorting the split
operations by position on the sheet cuts the penalty from +2524 mm to +924 mm, a
2.7x difference. That is free and it is the nest's whole premise. It has to
compose with the existing hole-nesting constraint rather than replace it -- hole
nesting is a hard ordering requirement, position is a preference, so position
belongs as the tie-break inside the topological sort `order_operations` already
performs.

**Two measurement mistakes recorded, because both gave alarming answers that
were not real.** Summing the split operation's path *and* its dressup's path
counted every move twice, since the dressup's path already contains the base
operation's -- that produced "+98% cutting, torch-on time nearly doubles", which
is not what happens. And measuring rapids per operation rather than on the
concatenated sequence counted a rapid from the origin for every operation, which
the post processor does not do. The figures above are from the concatenated
sequence.

**Not implemented. It is a product decision, not a performance fix.** The
replay's contract is that the user's operation is reproduced, one for one;
splitting it changes the job the user is handed. That should be the user's
choice, offered as an option, rather than done silently.

## NEST-014 — a compensated profile of a face at the stock top collapses to a line

Found by the per-part split, and **not caused by it**. Recorded because the
split turned an invisible defect into a visible one, which is how it was found.

The harness's synthetic bracket has its top face at exactly Z 0, which is the
replay stock's top surface, and the replay re-derives a through-cut to Z -6
against the sheet. That combination collapses the operation's XY extent:

    path bounds    (20.0, 25.0, 60.0, 25.0)
    target bounds  (20.0, 27.5, 60.0, 52.5)

**It was there before the split.** One operation covered both brackets, so its
bounds were (20.0, 16.0, 165.0, 40.1) -- an aggregate that happened to overlap
each target, so the bounding-box coverage check passed. The split gives each
operation one target, which removed the aggregate that was hiding it. The check
itself is unchanged and as strict as it was; the harness now declares this one
known-degenerate operation by name and reports it rather than failing on it.

Not root-caused. The evidence gathered:

* The source operation produces a correct 17-command outline. The replayed one
  produces 14 commands on a line.
* `FinalDepth` differs by design: 0 in the source job's own stock (-7..1), -6
  against the sheet (-6..0). That difference is documented and warned about.
* The committed fixture does **not** degenerate: 98 of 98 operations have
  cutting motion, sensible path lengths, and pass coverage.

So it is specific to this synthetic shape rather than to compensated profiles in
general. The force-the-source-into-the-same-Z-frame probe did not complete --
`StockFromBase.Height` recomputed rather than holding -- so the Z frame is
suspected but not established.

**Worth finding before it reaches a real part.** A flat-line profile over a real
plate would cut a straight gouge where the outline should be. Fixing it means
root-causing the projection, which needs a fixture whose top face is *not* on
the stock top, to tell "Z frame" apart from "face on the stock top".

## NEST-015 — hole-nesting reordering left every dressup unlisted

**Pre-existing. Found while starting the position-ordering work (Q3), which is
blocked until it is fixed.**

`order_operations` is handed `ReplayResult.operations`, which are the **base**
operations, and it wrote that list straight into `job.Operations.Group`. The
group is supposed to hold the **outermost dressup** of each step -- the rule is
stated three times in this module and in the fixture validator, and violating it
puts every contour in the job twice.

Measured on the committed fixture after the per-part split:

    Operations.Group: 98 entries
      dressups in the group: 0
      bare operations:     98
    in the whole document: 105 operations, 105 dressups

So all 105 dressups were built, linked and **never listed**. They would not have
been posted, which means the user's LeadInOut radii silently vanished from the
toolpath -- while every structural check still passed, because the checks were
looking at the base operations that *were* listed.

**Why nothing caught it.** The dressup harness's fixture has no hole nesting, and
`order_operations` returns early before the write when there is none. The real
fixture has 15 nestings, so it writes. The two fixtures disagree about the one
path that matters, and the one that exercises it had no coverage.

**Fixed** with `ReplayResult.entry_of`, mapping `id(base operation)` to the
outermost dressup wrapping it, filled in pass two where the stack is known.
`order_operations` takes it and translates before writing.

Two things that fell out of the same fix:

* **Verification was checking the wrong objects.** `verify_replay` iterates
  `result.operations`, so "98 of 98 operations with cutting motion" was a
  statement about the *base* operations. The dressed entries -- the ones that
  get posted -- were never verified. That claim is still true, but it was not
  the claim it appeared to be.
* The reordering note listed every label twice. At 98 operations that is several
  thousand characters of `then` in the Report view. It now says how many
  operations moved and under which rule.

**Why it was missed for so long**: the invariant "a dressed operation's list
entry is its dressup" was verified in three places, all of which exercised a
path that never writes. The bug lived in the one path that does.

## NEST-016 — position ordering put each spacer's boundary before its own holes

**Found by measuring the ordering on the committed fixture, not by reading the
code. Both fixes below were wrong on the first attempt and the measurement is
what caught them.**

Position ordering (Q3) originally made a part's internal step order a *tie-break
preference*. Two things that must be separate had been conflated:

* which part to visit next — a free choice, made by position;
* the order of one part's own steps — not a free choice at all, and the user's.

With within-part order left to the tie-break, hole nesting held a spacer's two
hole steps back until the nested parts were finished, but nothing held its outer
boundary back with them. It went first. Measured on the fixture:

    CAMPart_644  source indices [96, 92, 94]
    CAMPart_645  source indices [97, 93, 95]

The spacers carry `Profile004`, `Profile005` (both hole-cutting) then
`Profile006` (not), so the source order is holes-then-boundary — the user's rule.
The replay produced the reverse for both.

**This is physical, not cosmetic.** A spacer is held in the sheet by its
boundary, and the parts nested in its hole fall out the moment that boundary is
cut. Every operation after that point is machining loose parts. The user's third
rule — all of a part's internal work before its boundary — is not a separate
rule to enforce; it *falls out* of the nesting constraint once within-part order
is chained in source order.

Fixed by chaining each part's steps in source order as a hard constraint. It
also made contiguity exact: 50 stretches → 48, one per part.

### What the measurement was, and what it cost to trust it

Three of the four order measurements came back wrong before they came back
right, and each wrong one was a **vacuous pass**, not a wrong answer:

| what went wrong | what it reported |
|---|---|
| walked `.Base` from the entry to find its part | 0 parts; contiguity "perfect" |
| built the map from `entry_of.items()`, whose keys are `id()` ints | 0 parts; nesting "0 violations" |
| then wrapped those int keys in `id()` a second time | 0 parts; within-part order "0 of 0" broken" |

`0 of 0` is not a result. Three separate probes had reported a clean run while
checking nothing at all. `test_replay_order.py` now refuses to proceed if the
part maps do not fully resolve, and says so in a comment, because a check that
cannot see anything and a check that passes look identical from the outside.

The fourth measurement — nesting — reported **10 violations that were not
there**: it called `operation_touches_hole` on the dressup entries, and that
function returns `True` for anything whose `Base` is not a list of
`(geometry, subs)`, which a dressup is not. Every step of every outer part read
as a hole step.

### Measured result on the committed fixture

| | recipe order | after |
|---|---|---|
| rapid travel | 26 410 mm | **9 397 mm** |
| cutting travel | 14 030 mm | 14 030 mm |
| stretches per part | 98 | **48** (0 parts split) |
| within-part order broken | — | **0 of 48** |
| nesting violations | — | **0 of 15** |

`HorizRapid = 0.0 mm/s` on the fixture's tool controller, so machine time cannot
be derived and distances are the honest measure.

## The gate had a hole, and it is now closed

NEST-015 was reachable because the only fixture with hole nesting was the one
**nothing asserted against**:

* `test_replay_flatten.py` and `test_replay_dressups.py` both build their own
  geometry, and neither nests a part in a hole, so `order_operations` returned
  early before its write in **every gated run**;
* `replay-fixture-CAM-Nested.FCStd` — 15 nestings, the fixture that reaches the
  write — was read only by `validate_replay_fixture.py`, which is a diagnostic
  and is not gated.

`test_replay_order.py` closes it: 322 checks against the committed fixture, end
to end through the real FreeCAM modules, asserting that the list holds entries
and not bare operations, that each step's copy lands on exactly one part, that a
part's steps keep their source order, that hole nesting holds, and that parts
are contiguous. Both fixes were proved to bite by reverting each in turn.

### Two tests that were passing for the wrong reason

* `test_replay_dressups.py` compared `Operations.Group[i]` against recipe step
  `i` by index, under a comment asserting the list was step-major. Position
  ordering made that false *on purpose*, and it reported 11 chain-shape failures
  against a correct job. It now compares `(chain, target part)` pairs as a
  multiset — which also catches a copy that landed on the wrong part, something
  the previous index-wise comparison could not.
* `test_replay_flatten.py`'s "no nestings means no reordering" check passed
  because the plate and the plug are **concentric**: both are the same distance
  from the origin, so the chain falls back to source order. It would have passed
  with position ordering deleted outright. A second check now moves a part and
  asserts the order actually flips.

## NEST-017 — the per-stage timing table was missing the largest cost, and 60% of the run

**Found while adding per-stage timings to the command's report. The timing table
that had been finding the expensive stages was itself wrong by more than half
the run.**

Two defects, one of which made the other invisible.

**The final recompute was in no stage at all.** `replay_sheet` called
`doc.recompute()` between the ordering stage and the verification stage, so it
fell between the brackets. It is the single largest item in the pipeline —
measured at 24.0s of a 43.5s run at the time — and it appeared in no row. A
timing table that silently omits the biggest cost is worse than none, because
it points at the wrong thing. It is now its own stage, "Recomputing the
toolpaths", and costs 2.65s.

**The validator timed from progress events rather than from stage boundaries.**
Events land at arbitrary points *within* a stage, so nothing brackets the work
before a stage's first event or after its last. Measured on the committed
fixture, that method summed to **4.30s of a 10.54s run** — a 6.2s hole, and it
fell on the two largest stages:

| stage | event-delta (wrong) | bracketed (right) |
|---|---|---|
| Ordering and tidying the tool table | ~1.0s | **3.8s** |
| Replaying the recipe | ~1.4s | **3.6s** |
| Recomputing the toolpaths | *absent* | **2.7s** |
| SUM | 4.30s | **10.7s** |
| wall clock | 10.54s | 10.7s |

Undercounting by 60% while reading as a plausible table is the same failure
class as the rest of this file: an instrument that reports a confident wrong
answer. The 6.2s was not "some overhead" — it was the two stages anyone would
have been told to optimise.

Fixed by making `Progress` time itself. It already has exactly one boundary per
stage, so the timings come from there rather than from a second set of brackets
kept in step by hand. `clock` is injectable, which is what lets the pytest tier
assert on the numbers without sleeping.

### Why the timings live on the outcome

`Progress` is built inside `replay_layout`, one per sheet, so the caller could
not see them. `SheetOutcome.timings` carries them out, which means the command's
report, the fixture validator and any future caller read the same numbers from
the same place rather than each reconstructing them.

The stages account for `10.73s` against a `10.74s` wall clock, so nothing is
running outside a stage. The command prints the gap explicitly when it exists
rather than letting the table quietly stop short.

### What the table now says

    where the time went:
    Ordering and tidying the tool table     3.77s  35.1%
    Replaying the recipe                    3.65s  34.0%
    Recomputing the toolpaths               2.65s  24.7%
    Building the replay job                 0.48s   4.5%
    Flattening nested parts                 0.13s   1.2%
    Verifying the result                    0.05s   0.5%
    Reading the source CAM setup            0.00s   0.0%
    accounted for                          10.73s
    wall clock                             10.74s

Ordering and replaying are two thirds of the run, so that is where the next
effort belongs. Recomputing at 24.7% is FreeCAD evaluating 98 toolpaths, which
is the floor for this design.

The timings are printed on every run and are not behind a preference. Every
figure that got this feature from 43.5s to 10.7s was measured this way, and a
timing nobody can see is a timing nobody re-measures after a change makes it
worse.
