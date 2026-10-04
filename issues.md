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
| [NEST-009](#nest-009) | a replayed Boundary dressup clips against the SOURCE job's stock | open | high | CAM replay |
| [NEST-010](#nest-010) | the flattened parts could not be removed: the job's clones were links | resolved | medium | CAM replay |
| [NEST-011](#nest-011) | two toolbar buttons had the same icon | resolved | low | UI |
| [NEST-012](#nest-012) | the replay took 43 seconds | resolved | medium | performance |
| [NEST-013](#nest-013) | FreeCAD's Profile is superlinear in its number of base targets | open | medium | performance |
| [NEST-014](#nest-014) | a compensated profile of a face at the stock top collapses to a line | open | high | geometry |
| [NEST-015](#nest-015) | hole-nesting reordering left every dressup unlisted | resolved | high | CAM replay |
| [NEST-016](#nest-016) | position ordering put each spacer's boundary before its own holes | resolved | medium | CAM replay |
| [NEST-017](#nest-017) | the per-stage timing table was missing the largest cost, and 60% of the run | resolved | medium | diagnostics |
| [NEST-018](#nest-018) | Tasks panel progress, and cancellation did not exist | resolved | medium | UI |
| [NEST-019](#nest-019) | the Tasks panel never appeared, and cancelling never worked | open | medium | UI |
| [NEST-020](#nest-020) | the default 5 mm endmill was present for the whole replay | resolved | medium | CAM replay |
| [NEST-021](#nest-021) | the Draft `ReferenceError` flood | resolved | low | diagnostics |
| [NEST-022](#nest-022) | Logging is 230 ad-hoc call sites and 165 exception handlers that record nothing | open | medium | diagnostics |
| [NEST-023](#nest-023) | "Stop At Sheets" was a dead control: read by nobody, never reached the engine | resolved | high | UI wiring |

Statuses for NEST-009 through NEST-021 were derived from each entry's own
prose, cross-checked where a later entry supersedes an earlier one: NEST-018's
"Still to verify" is closed by the GUI verification recorded in NEST-020, and
NEST-015 is fixed by `ReplayResult.entry_of`. Two of those entries carried no
status field at all before this pass, which is why thirteen of them were
missing from this table.

**NEST-012 appears twice.** The entry marked *superseded draft* is the pre-fix
investigation; the one this table links to records the same investigation after
the fix. Both are kept because the draft holds a per-stage event-count table and
a note on why `Document.recompute` can only be timed by subtraction, neither of
which the later entry repeats. **They should be merged** -- see the task list.

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

`status: open` · `severity: high` · `area: CAM replay`

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

`status: resolved` · `severity: medium` · `area: CAM replay`

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

`status: resolved` · `severity: low` · `area: UI`

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

## NEST-012 — the replay takes 43 seconds, and 42 of them are the ordering step (superseded draft)

`status: superseded` · `severity: medium` · `area: performance`

**Superseded by the second NEST-012 below**, which records the same
investigation after the fix and is the entry to read. This one is the
pre-fix draft and is kept only because its per-stage event counts and its
note on why the recompute can only be timed by subtraction are not
repeated there. They should be merged; see the note under the Summary
table.

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

`status: resolved` · `severity: medium` · `area: performance`

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

`status: open` · `severity: medium` · `area: performance`

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

`status: open` · `severity: high` · `area: geometry`

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

`status: resolved` · `severity: high` · `area: CAM replay`

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

`status: resolved` · `severity: medium` · `area: CAM replay`

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

`status: resolved` · `severity: medium` · `area: diagnostics`

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

## NEST-018 — Tasks panel progress, and cancellation did not exist

`status: resolved` · `severity: medium` · `area: UI`

The `Progress` seam had a callback and no consumer. Worse, it had no notion of
being stopped at all: **a Cancel button would have been a control that did
nothing.** Cancelling is an engine feature and the engine did not have one.

### What the panel shows, and what it refuses to show

There is no overall percentage, because there is no honest one. From NEST-017:

    Ordering and tidying the tool table   35.1%
    Replaying the recipe                  34.0%
    Recomputing the toolpaths             24.7%

Ordering is a third of the run and reports `total == 0` — it is a single
indivisible call. A bar ticking once per operation would sit still for nearly
four seconds and then jump, which is worse than no bar because it looks broken.

So the bar is **within** the current stage and says so. A stage with no
countable work gets Qt's marquee and the words "this stage reports no progress",
which is the truth about it. The elapsed clock and "stage N of 7" are always
visible, so something genuinely increases even when the bar cannot move.

`ReplayProgressView` is pure — no Qt, no event loop — and is what the tests
cover. The widget only draws what it is told. A progress bar cannot be
exercised headless, and one that has been looked at once is indistinguishable
from one that does not work.

The replay is synchronous on the GUI thread, so the widget is not painted until
the replay finishes unless the event loop is pumped. `FreeCADGui.updateGui()` on
each event is the whole fix, and is what FreeCAD's own modules use for it.

### Cancel: keeping what was built, and two bugs on the way

A cancelled replay **keeps** its job and labels it `_UNVERIFIED`. Deleting it
would be the wrong instinct — an operator who cancels wants to see how far it
got, and that is also what makes a cancel debuggable.

The polling model: the engine takes `cancel_check()` and reads it at every point
it can stop. A widget that fell over on the way to being asked is *not* a
request to stop, so a raising check disables cancellation rather than firing it.

**Bug 1 — cancellation was 23 operations late.** The poll sat at the recipe-item
boundary in pass two. The fixture's first step lands on 23 parts, so a cancel
ran 23 operations past. Now polled per copy; each copy owns its own stack, so
breaking there leaves nothing half-built.

**Bug 2 — cancelling reached the NEST-015 failure by another road.** This is the
interesting one. Pass one builds *every* base operation before pass two dresses
them, so after a cancel `result.operations` is complete while the Operations
list holds only the stacks that were finished. `order_operations` reads that
mismatch as "repopulate" and writes the bare operations over the entries.

Measured: cancelled 5 operations in, and the list came back holding **98 bare
operations of 98**, with every dressup orphaned and unlisted — the exact
NEST-015 signature, reached by a different path. The ordering step now does not
run at all on a cancelled replay, and the harness asserts the invariant on that
path (negative control: removing the guard reproduces 94 bare of 98).

Cancellation also stops the *run*, not the sheet. Carrying on opened a panel for
the next sheet and started working on it while the user was still looking at the
Cancel button they had just pressed.

### Still to verify

The panel has not been seen in a running FreeCAD. `ReplayProgressView` is tested
and the engine paths are tested against real FreeCAD objects; the Qt half is not.

## NEST-019 — the Tasks panel never appeared, and cancelling never worked

`status: open` · `severity: medium` · `area: UI`

**All three bugs found by the user running it, from the console log. None of
them was visible to any test in this repository, because all three are Qt.**

### 1. `setRange` takes two arguments

    Replay progress callback raised TypeError: setRange expected 2 arguments, got 1.

Called with one argument it raises on the *first progress event*, the exception
propagates out of the callback, and `Progress` correctly gives up on a callback
that has already failed. So the panel died before drawing anything. The
pytest tier covers `ReplayProgressView`, which is pure; the widget is exactly
the part it cannot catch.

### 2. `cancel_check=task.cancelled` passed a bool, not a callable

    Replay cancel check raised; cancelling is disabled.

`cancelled` is a **property**, so naming it in a call evaluates it there and
hands `replay_layout` the bool it happened to be — `False`. `Progress` then
called that `False` and got `TypeError: 'bool' object is not callable`, and
because a cancel check that raises is treated as a broken UI rather than as a
request to stop, **cancelling was disabled for the entire run**. The button
would have looked plausible and done nothing.

This is the second time in this file that a name evaluated at the wrong moment
produced a silent no-op. The general lesson, cheap to state and apparently not
instinctive: a property read at the wrong time is a value, and a value where a
callable is expected fails *later*, in the consumer, where nothing connects it
to the mistake.

### 3. The task pane was not in view

`Control.showDialog` succeeded — the widget existed, which is why `cancelled`
could be read at all — but the task pane lives in the Start workbench's dock.
From the Model or CAM workbench it is off screen, which reads as "no progress".
`Control.showTaskView()` is now called first.

### What the console log also showed

**A `ReferenceError` per deleted object, from Draft, not from here.** About 96
of them:

    File ".../Draft/draftmake/make_clone.py", line 156, in <lambda>
      QtCore.QTimer.singleShot(0, lambda: gui_utils.format_object(cl, obj[0]))
    File ".../Draft/draftutils/gui_utils.py", line 557, in format_object
      if not hasattr(target, "ViewObject"):
    ReferenceError: Cannot access attribute 'ViewObject' of deleted object

Draft defers `format_object` by one event-loop turn (a workaround for
FreeCAD #27958) and guards it with `if not target: return`, which does not
cover a *deleted* object — a deleted FreeCAD object is not falsy, it raises on
attribute access. Our nesting workbench creates Draft clones and then removes
its staging clones, so by the time anything pumps the event loop those deferred
callbacks point at corpses.

Nothing pumped the event loop before this feature. `FreeCADGui.updateGui()` —
which is what makes a synchronous replay repaint at all — is now the first thing
to run them, so the noise appears during the replay instead of on the next
unrelated event.

**Not fixed here.** The honest fix is at the point the clones are deleted: pump
the event loop while they are still alive. That is in the nesting path, which is
benchmarked and out of scope for this feature. Recorded so it is not mistaken
for a replay defect, and so nobody re-derives it.

Also seen and not ours: `Could not tidy the tool table on ...: Cannot access
attribute 'Label' of deleted object` — which is the next issue, and *was* ours.

## NEST-020 — the default 5 mm endmill was present for the whole replay

`status: resolved` · `severity: medium` · `area: CAM replay`

Reported by the user: the new job still carried the `TC: 5mm Endmill` controller
and the SetupSheet's tool dialog kept triggering.

Two bugs, one hiding the other.

**`prune_unused_tool_controllers` had never completed successfully.** It did

    removed.append(getattr(controller, "Label", "?"))

*after* `_remove_with_orphans` deleted the controller. Reading `Label` off a
deleted FreeCAD object raises `ReferenceError`, and `getattr`'s default only
swallows `AttributeError`, so the exception escaped, aborted the prune
mid-flight, and the console said only "Could not tidy the tool table". This had
been firing on every run since the step was written.

**The prune ran in the wrong place anyway.** It came *after* `replay_recipe`,
which is where all 98 operations are created — so even working, it was far too
late to stop a prompt that fires while operations are being made.

Fixed by removing the controllers the job arrived with, at the moment the user's
own controller is copied in and before the first operation object exists:

    `PathJob.Create`      job has TC: 5mm Endmill
    copy_tool_controller  job has both            <- nothing built yet, harmless
    remove obsolete      job has only the user's
    create operation      1 operation, 1 controller

Deliberately not before `copy_tool_controller` runs: that would leave a window
with *no* tool, which is the same prompt from the other direction. The harness
now asserts the property directly — **never an operation in existence while two
controllers are in the table** — over all 98 built operations. Negative control:

    FAIL: with 1 operation(s) built the job held 2 tool controllers
          ['TC: 5mm Endmill', 'TC: Plasma, 40A, 1.2mm kerf001'];

The first version of that check asserted on the *first* replay event, which
passed for the wrong reason: the user's controller had not been copied in yet, so
the count was 1 by accident. It is the first event *with an operation built*
that matters, and 98 of those now agree.

`prune_unused_tool_controllers` is kept as the safety net for a custom
`job_factory` whose job was not empty to begin with.

## GUI verification

The panel and the cancel path are now verified in a real GUI (`freecad` against
`DISPLAY=:10.0`), not just headless:

    18 checks  widget construction, bar ranges, marquee, panel open/close,
               cancel flag, reading the flag after the widget is deleted
    12 checks  end-to-end: panel open, replay draws 156 stage events, cancel
               keeps 4 of 98 operations labelled _UNVERIFIED, panel closes,
               and an uncancelled run with the panel open is still ok

Both would have caught the `setRange` and property bugs. They are probes rather
than gate tests because they need a display.

## NEST-021 — the Draft `ReferenceError` flood (resolved)

`status: resolved` · `severity: low` · `area: diagnostics`

**The user reported it as an unusable error popup. It was resolved, and the
count matches their log exactly: 96 before, 0 after.**

Draft's `make_clone` defers `format_object` by one event-loop turn — a
workaround for FreeCAD #27958 — and guards it with `if not target: return`.
That does not cover a **deleted** object. A deleted FreeCAD object is not
falsy; it raises on attribute access:

    File ".../Draft/draftmake/make_clone.py", line 156, in <lambda>
      QtCore.QTimer.singleShot(0, lambda: gui_utils.format_object(cl, obj[0]))
    File ".../Draft/draftutils/gui_utils.py", line 557, in format_object
      if not hasattr(target, "ViewObject"):
    ReferenceError: Cannot access attribute 'ViewObject' of deleted object

A nest that made Draft clones and then removed its staging clones leaves one
queued callback per clone, all pointing at corpses.

**Nothing pumped the event loop before this feature.** Measured, pumping
`updateGui()` on a freshly-opened fixture produces **0** of these — they need
the replay, because that is where the deletion happens. But `updateGui()` is
also the only reason a synchronous replay repaints at all, and the reason Cancel
can be clicked. So the feature that fixed the missing progress bar is the same
one that surfaced 96 tracebacks and an error popup.

### The investigation, including two dead ends

* Assumption: we call `Draft.make_clone`. **False.** `grep` found no call in
  this repository, and instrumenting `make_clone` during a replay counted **0**
  calls. The clones come from earlier in the user's session, not from the replay.
* Assumption: the noise is proportional to what the replay deletes. **False.**
  Pumping the loop on the opened fixture without replaying produced 0 errors;
  the replay is required.
* What is actually proportional: `format_object` runs **48 times** per replay --
  once per flattened part -- and in a clean session all 48 land on live objects.
  The user's session differs by timing: its callbacks fire after the objects are
  gone.

So the count is not derivable from anything the replay does. It depends on what
the *session* left queued. That is why the fix had to be at the pump rather than
at a call site.

### The fix

The bug is Draft's; the provocation is ours. So the pump carries the guard:
`format_object` is wrapped for exactly as long as the panel is open, returning
None for a deleted object, and **undone on close** so the rest of the session
sees Draft unmodified. A permanently patched Draft would be a worse thing to hand
a user than a burst of tracebacks.

`format_object` is looked up as an attribute at call time
(`gui_utils.format_object(...)`), so patching the module attribute intercepts
the already-queued call.

Skipped calls are **counted and reported**, not hidden — `show_error` for a panel
that did not open, and a line in the report for dropped Draft callbacks. Both
went through three rounds of "the panel is just absent and nobody knows why",
because the `except` discarded the reason. A panel that is merely absent and a
panel that was never tried look identical from outside.

Verified by negative control in a real GUI:

    ReferenceErrors without the guard:  96
    ReferenceErrors with the guard:      0


---

## NEST-022

**Logging is 230 ad-hoc call sites and 165 exception handlers that record nothing**

`status: open` · `severity: medium` · `area: diagnostics`

Measured 2026-10-04 while assessing whether to adopt upstream's `nw_logger.py`.
Nothing has been built. This entry exists so the measurements survive, and so the
next person does not re-derive them.

### The two numbers

**230 `FreeCAD.Console.Print*` call sites across 25 files:**

| file | sites | file | sites |
|---|---:|---|---:|
| `manual_nester_tool.py` | 33 | `exporter.py` | 8 |
| `nesting_controller.py` | 24 | `command_create_silhouette.py` | 8 |
| `ga_coordinator.py` | 23 | `nesting_logic.py` | 7 |
| `cam_replay.py` | 20 | `ui_nesting.py` | 6 |
| `silhouette_creator.py` | 18 | `minkowski_engine.py` | 4 |
| `shape_processor.py` | 15 | `layout_manager.py` | 4 |
| `cam_manager.py` | 11 | `nesting_job.py` | 3 |
| `input_manager.py` | 10 | everything else | 36 |

**165 exception handlers record nothing at all** — no `Console` call, no
`print`, no logger, nothing. Counted by looking at the two lines after each
`except` for any recording:

| file | silent handlers | file | silent handlers |
|---|---:|---|---:|
| `cam_replay.py` | 60 | `ga_coordinator.py` | 6 |
| `replay_progress.py` | 14 | `ui_nesting.py` | 5 |
| `units.py` | 12 | `collision_resolver.py` | 5 |
| `manual_nester_tool.py` | 10 | *all others* | 53 |
| `nesting_strategy.py` | 10 | | |
| `length_field.py` | 8 | | |
| `nesting_controller.py` | 7 | | |

**The second number is the interesting one.** The 230 are untidy; the 165 are
where evidence currently disappears. Several are load-bearing races — a deleted
widget during teardown, a `removeObject` that fails because the object is already
gone. Those fire routinely and, today, leave no trace.

### The two existing panel controls are not log levels

Worth recording because the obvious first move — add a third dial — is the wrong
one. Neither existing control is verbosity:

| control | scope | what it gates |
|---|---|---|
| `verbose` | per-run panel field, **not persisted** | narration of normal operation — "Rotation eval: 4 rotations in 12ms". 25 `if verbose:` sites. |
| `performance_logging` | per-run panel field | whether **instrumentation is computed at all**, not just printed. `CandidateGeometryKeyTracker` is only constructed when it is on (`nesting_strategy.py:350`); `LayoutManager._layout_perf` stays `None` unless on. It is a *cost* switch. |

So `performance_logging` answers "is this run worth measuring?" and `verbose`
answers "do I want to watch it work?" Neither is "should this library emit at
info or debug?".

### What upstream's `nw_logger.py` is, and is not

`nw_logger.py` (228 lines, not in our tree) is a `logging`-module wrapper with
level helpers, a rotating file handler, and two preferences
(`EnableDebugLog`, `EnableCrashLog`).

Two things to know before considering it:

**Its `debug()` is for recovered failures, not narration.** All 47 `debug()`
call sites sampled are `except` handlers recording a swallowed error:

    [Sheet] recursive_delete skipped during child cleanup: {e}
    [ShapePreparer] Face creation failed, falling back to Compound wires: {e}
    [NestingPanel] update_progress widget deleted: {e}

That is the 165-handler problem, and it is the part worth having.

**About a third of it is dead code for us.** `_in_worker`,
`_buffer_worker_lines`, `drain_worker_messages`, `replay_worker_messages` and
`_WORKER_BUFFERED` exist to carry warnings out of GA *worker processes*. We have
no process pool, so adopting them as-is means shipping code with no caller.

Its `EnableDebugLog` is a persistent preference, which is the opposite lifetime
from the two panel fields — and deliberately so, since those are documented as
not persisted ("every open starts in the same shape"). Standing interest in
"what happened last Tuesday" is a standing preference; "watch this run work" is
not.

### Why nothing was done

Scoped as an option, then parked in favour of a different approach. Two
consequences worth keeping:

- **A blanket conversion of the 165 would be noise.** Many are silent *on
  purpose* — a widget-deleted race during teardown is expected, not exceptional.
  Whether a given silence loses information is a judgement per site, so this is
  not a sweep.
- **The two preferences need a UI or they are unreachable.** Upstream toggles
  them in a `command_settings.py` we do not have. Set by hand only, that is a
  control nobody can reach.

### Reproducing

    # 230 call sites
    grep -rn 'FreeCAD\.Console\.Print' freecad/ --include=*.py | grep -v __pycache__ | wc -l

    # 165 silent handlers: for each `except`, look at the next two lines for
    # Console./print(/logger, and for a bare `except ...: pass`

---

## NEST-023 — "Stop At Sheets" was a dead control (resolved)

`status: resolved` · `severity: high` · `area: UI wiring`

Reported as "the early-finish flag does not seem to be working". It was not
working at all: the dial was read by nobody, so the feature was permanently off
and every run went to completion with no error, warning, or log line.

### The break, in four links

| link | state |
|---|---|
| `ui_nesting.py:871` — the spinbox, "Stop At Sheets:", 0-100, "Off" at 0 | existed |
| `nesting_controller._collect_ui_params` | **never read it** |
| `nesting_controller._prepare_algo_kwargs` | **never set `algo_kwargs['target_sheets']`** |
| `ga_coordinator.run:530`, `_run_generation:1175` | reads `algo_kwargs['target_sheets']` |

The coordinator was handed no target, defaulted it to 0, and never stopped
early. The engine was never wrong: `_evaluate_layout:1290` returns the instant a
layout qualifies, `run:797` skips breeding a generation nothing will use, and
`run:851` breaks and records the hit as a **success, not a cancel**.

### Why it survived

Every test that covers the target passes it **directly into `ALGO_KWARGS`**,
bypassing the panel:

    tests/freecad_harness/test_ga_loop.py:329,373,396,429

Those five cases (met, not-met-by-dropping-parts, unreachable, fill-only, and a
hit being a success) all passed throughout. Testing the engine and testing the
wiring are different jobs, and only one of them was being done — so the suite was
green and comprehensive about the wrong thing.

This is the reusable finding: **a panel control can exist, hold a sensible value,
be laid out correctly, and still be read by nobody.** Nothing about the widget,
the layout probes, or the unit tests can see that.

### Scoped, and it was not systemic

Audited all 39 panel value-controls. `minkowski_target_sheets_input` was the only
one read nowhere. The three other apparent orphans (`add_parts_button`,
`font_select_button`, `remove_parts_button`) are wired via `clicked.connect`,
which is correct. So this was an isolated two-line gap, not a family.

### The fix, and its guard

Two lines: collect the spinbox in `_collect_ui_params`, pass it on in
`_prepare_algo_kwargs`'s Minkowski branch beside `generations` and
`population_size`. Not persisted, as the widget's own comment says —
`save_settings` names its preference keys explicitly, so adding the key to the
collected dict cannot leak it into prefs.

Guard is `probe_target_sheets.py` (panel → `_collect_ui_params` →
`_prepare_algo_kwargs` → a real GA run) plus the panel-side half in
`probe_unit_panel.case_ga_dials_reach_the_run`. **Injection-verified**: 13/13
with the fix, 8/13 with it reverted.

That verification earned its place. The probe's first version took a
`target_sheets` argument and assigned it into the kwargs, and with the fix
reverted the GA assertions still passed — the run was being handed the target
directly, so it could not detect the target not arriving. Injecting the value
made the check decorative. `run_ga` now takes no target at all.

### Also found: the `log_callback` / `draw_callback=None` trap

Hunting this abort cost most of the session, and the trap is worth keeping.

`_prepare_algo_kwargs` puts `log_callback=NestingPanel.log_message` — a **Qt
widget method** — into `algo_kwargs`, and `nesting_strategy` calls it from the
rotation worker threads (`nesting_strategy.py:449,1402`). Production never hits
this: `_run_generation` replaces `log_callback` with a console sink whenever
`draw_callback` is set.

Any caller passing `draw_callback=None` — which `bench_ga.py`,
`test_ga_loop.py` and `probe_target_sheets.py` all do, to run synchronously —
loses that substitution, and with `performance_logging` on, thousands of
`[TIMING]` lines reach the widget from a worker and the process aborts:

    GUI API 'FreeCADGui.updateGui' may only be used from the main thread.
    terminate called after throwing an instance of 'Py::RuntimeError'

It reads as a coordinator fault and the stack points at the coordinator. Still
latent in production because `draw_callback` is always set there, but it is a
trap for any future probe or headless caller. Pass a null or console sink, as
`bench_ga.py` already does.

### Reproducing

    /home/james/freecad_env/usr/bin/freecad \
        tests/freecad_harness/probe_target_sheets.py

    # and confirm the guard bites
    git stash push freecad/nestingworkbench/Tools/Nesting/nesting_controller.py
    # -> 8/13, exit 1; then git stash pop
