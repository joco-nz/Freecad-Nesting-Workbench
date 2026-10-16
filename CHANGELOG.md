# Changelog

All notable changes to Jet-Nesting will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.3] - 2026-10-13

### Fixed
- **Cross-platform deterministic nesting**: Fixed non-deterministic tie-breaking in `score_gravity()` that caused 180° rotation flips between Linux/macOS/Windows. The metric is now rounded to 10 decimal places and ties are broken by preferring the candidate closest to origin, ensuring identical results across platforms with same library versions.

### Added
- Cross-platform diagnostic tools (`tests/diagnose_console.py`, `tests/diagnose_nesting.py`, `tests/compare_nesting_results.py`, `tests/run_diagnose.sh`) for verifying nesting reproducibility across platforms.

## [Unreleased]

### Fixed
- **The harness gate no longer stops on a status file that was never missing.**
  `run.sh` waited for `.last_status_verifystate`, but `test_verify_stage.py`
  writes `.last_status_verifystage`: a one-word mismatch that was permanent, so
  the gate always quit at `DID NOT RUN -- did not write ...` and printed no
  verdict at all, hiding the results of every other suite. The two names now
  match, so the gate runs to its summary and reports what actually happened.
- **A replayed job's start point is no longer carried to the wrong place on the
  part.** `source_to_clone_placement` built the transform from the two
  placements alone, but the nesting side bakes its frame shift into the
  *geometry*: `shape_preparer._handle_new_master` resets the master's placement
  to identity after `bake_rigid` has moved the vertices, so all 54 `part_*`
  objects in the fixture carry an identity placement and the shift where
  placements cannot see it -- 0.74 to 2.09 mm depending on the part.
  Every carried `StartPoint` landed that far out. Measured on the fixture, a
  replayed cut path sat **2.094006 mm** from the rigid image of its source's, a
  pure X translation, and `Profile004`'s start point sat **2.725067 mm** from its
  own cut path where the source's sat **0.636008 mm**. The rotation is still read
  from the placements, which are correct; the translation is now measured between
  the two solids, which is the only place the baked shift is visible. The 110
  replayed paths now sit within **0.0336 mm** of their source's -- the replay was
  never wrong, only the carry was. A pair that is not the same part moved now
  returns `None` rather than a guess, and a replay reports any carried point
  whose distance to its part no longer matches its source's. See issues.md
  NEST-040.
- **A lead-in reaching another lead-in or lead-out is no longer reported as a
  clash with a cut path.** The audit has always tested lead runs against each
  other -- a lead-in is itself a cut path, and another operation's can run into
  it -- but `mark_conflict` built its sentence from the part name and the words
  `cut path`, so the tree read *"comes within 0.0000 mm of part_TopStrap_14's
  cut path"* for a clash with that part's *lead-in*. The operator was sent to a
  contour that was never involved. Reports now give lead runs a section of their
  own and the headline states the count, including when it is zero.
  On plasma or waterjet this is the worse of the two failures: reaching a cut
  path re-cuts material, while crossing another lead run bridges the arc across
  a kerf it has already opened, drops the flame and errors the controller.
  Same clearance margin and the same fix; a different sentence, because the
  decision about whether to post the job is a different one.
- **The fix no longer verifies candidates against geometry that has already
  moved.** The sheet index was built once, before any start point moved, and
  Shapely `STRtree` is immutable -- so after the first operation was fixed, every
  subsequent candidate was checked against a neighbour advertised at where it
  used to be. Measured on the fixture: moving one start point carried its lead-in
  from **20.0 mm** away from a neighbour's lead-in to **0.0 mm** from it. The
  neighbour's own search then cleared a run against a lead-in that was not there,
  and the run finished reporting a clean sheet with two lead-ins crossing on it.
  Every individual test passed, because each was honest about the index it was
  given. The index now follows the operations as they are fixed; the rebuild
  costs **0.0754 ms** over 280 entries, so the worst search pays 0.3%.
- **A fix run re-reads the finished job before reporting.** An operation that
  was moved but does not hold up is marked `CONFLICT_` rather than reported
  fixed -- the cross-operation failure above cannot be caught per operation, so
  the claim is checked at the end against a fresh scan.
- `Check Lead-ins Against Nesting`'s search now walks the first stage's real
  candidate ceiling rather than 8 in one check, so it can actually demonstrate
  that a lead-out is judged together with its lead-in.
- **Four stale measurement pins in the lead-in audit's test suite replaced with
  assertions that test something.** None was re-pinned to whatever the code
  currently prints, which asserts nothing and breaks again on the next re-nest.
  The nearest-near-miss check now asserts the clash is *tighter than the largest
  arc sag* — which is the property that catches chorded arcs, and is about the
  reader rather than about the nest. The off-sheet check now **builds** its case
  (`RadiusIn` raised until a pierce point leaves the stock, measured at 40 mm)
  instead of relying on the fixture supplying one. The idempotence check now
  asserts a **third** run moves nothing, since the tool converges in two.
- **A pierce point closer to the sheet edge than the tool radius is now a
  finding.** The edge gets the same clearance a neighbouring part's cut path
  already got, read from the same number, for the same reason: **the kerf has to
  have material on both sides of it.** A pierce 0.4 mm from the edge with a 0.6
  mm tool puts two thirds of the kerf over air — on plasma the arc strikes across
  a sliver, on waterjet the part loses its boundary. It is flagged for fixing on
  the same terms as any other conflict. Fixing this required first correcting
  the measurement it is based on; see below.
- **The sheet-edge check was measuring the wrong distance, and the zero margin
  was hiding it.** It measured a point's distance to the sheet *polygon* rather
  than its *boundary*, and Shapely reports 0 between geometries of different
  dimensions that intersect — so **every** pierce point on the material read
  "0 mm from the edge". The check appeared to work only because the margin it was
  given was `0.0` and `0 < 0` is False; that zero was a constraint imposed by the
  bug rather than a decision about pierce points. Measured against the boundary,
  the fixture's closest pierce point is 2.3105 mm. The margin is now the
  operation's own tool radius and is required rather than defaulted, and the
  report distinguishes four findings — off the sheet, within the tool radius of
  it, pierces clear then runs off, pierces clear then skims — rather than
  reporting all of them as "pierces off the edge". See NEST-038.
- **A check loop that iterates zero times now fails loudly.** Four assertions in
  the off-sheet check had silently stopped running when a re-nest left the
  fixture with nothing to iterate — they were not counted at all, so the suite
  went quieter rather than greener. A `group()` helper reports an empty group as
  a check in its own right and yields the items, so there is no shape in which a
  loop of checks can skip itself.

### Added
- **An adjustable clearance on the lead-in check.** The report-or-fix dialog
  carries a **Clearance** dial, a multiple of the tool radius, so the
  sensitivity of every check can be turned up or down rather than fixed. Both
  checks move together — a lead run reaching a neighbouring cut, and a pierce
  point reaching the sheet edge — because they are the same question asked of
  two things, and a dial that moved one and not the other would leave a report
  whose halves disagree about what "too close" means.

  **1.00 is the default, and is the clearance the check has always used**: a cut
  exactly touching its neighbour, which is the smallest clearance that is
  physically meaningful. Above it buys margin against lead-in placement and kerf
  wander, and finds more; below it finds less; at 0.00 only outright crossings
  are reported. The unit is the *radius* rather than the diameter precisely so
  that 1.00 preserves existing behaviour instead of silently doubling it.

  The value is remembered in preferences and clamped on the way in and out — a
  parameter file can be hand-edited, and a negative clearance would make every
  pierce point read as inside it. Cancelling the dialog leaves it unchanged.

  The report states the clearance it used, because the counts are a function of
  it: the same job reports 40 offending operations at 1.00 and rather more at
  2.00, and a number nobody can compare is decoration.

- `Nesting > Check Lead-ins Against Nesting` finds the lead-ins and lead-outs of
  a replayed CAM job that reach another part's cut path **or another
  operation's lead-in or lead-out**, and moves their start points until they
  clear. Menu only, not the toolbar: it acts on a replayed job, so a toolbar
  button is inert for most of a session. Offers report-only first, which writes
  nothing.
  A lead-in extends outside its own part by `RadiusIn`, and the nester placed
  that part among others; nothing in the replay accounted for where it landed.
  On the committed fixture the worst lead-in clears its neighbour's cut path by
  **0.0598 mm** -- which no intersection test reports, because nothing actually
  crosses, so the check uses a clearance margin and starts at the tool radius.
  **Only `StartPoint` is changed**, and `UseStartPoint` so that `StartPoint` is
  read at all -- with the flag off, `StartPoint` is not read. `StyleIn`,
  `AngleIn`, `RadiusIn`, `InvertIn` and the rest are the user's craft and are
  never touched; `InvertIn` in particular would mirror the lead-in to the other
  side of the contour, which is a different cut rather than a relocated one.
  `StartPoint` is the exception because it is the only property in a CAM recipe
  that is an absolute coordinate, and so the only one nesting made meaningless.
  Candidates walk outwards from the operation's own start point, alternating
  clockwise and anticlockwise, so the accepted fix is the nearest one that works
  -- on the fixture it fixed the offender on the first candidate, taking the
  lead-in from 0.0598 mm to 3.3530 mm.
  **Lead-in and lead-out are tested together**, since one start point places
  both ends; accepting a candidate on the lead-in alone would fix the reported
  conflict by creating an unreported one.
  What cannot be fixed is **marked `CONFLICT_` and left alone** -- the dressup is
  not skipped, it will still cut -- with a `LeadInConflict` property naming the
  part in the way. The fix lands on the replayed job only, so re-running the
  replay discards it.
  The search runs in the Tasks panel with a working **Cancel**. The bar counts
  candidates rather than operations, because the fastest fix is a single
  candidate and an operations-based bar would jump straight to 100% and read as a
  hang. Cancelling keeps whatever was already fixed -- each fix is checked on its
  own -- and the operations it never reached are reported as not looked at
  rather than marked, since nobody has said they conflict. See NEST-033.
- The panel's length fields follow the document's unit system. Sheet size,
  thickness, part spacing, label size and height, simplify tolerance, candidate
  step, physics step and anneal amplitudes, and the Manual Nester influence
  radius are now shown in, and accept, whatever units the document uses --
  including fractional and compound inch entry (`1/2 in`, `1' 11"`). Values
  still reach the nester as plain millimetres; nothing below the UI changed.
- Console and dialog messages that quote a dimension -- packing yield, CAM
  stock size, the sheet-thickness mismatch warning, the manual nester's physics
  log -- report it in the document's units instead of a hardcoded `mm`.
- `Helpers` and `Logging` collapse, and start collapsed, taking 167px off the
  panel's height. Their contents are diagnostic controls and were taller than
  the four sheet fields at the top of the panel. The collapse state is not
  remembered, so every open is the same shape.
  Note that a checkable `QGroupBox` alone does not do this: unchecking it only
  *disables* its children, leaving them visible and the panel just as tall.
  FreeCAD's own BIM workbench has that behaviour in its "Sun Position" group.
  Hiding the contents is what actually reclaims the space.
- Sheet size, part spacing, candidate step and rotation threads are laid out as
  two-column grids rather than single-field rows.
- The nesting direction dial steps in 15° increments, and the four cardinal
  buttons around it are gone -- all four directions are still on the 15° grid,
  so nothing was lost but three rows of height. The readout now shows the
  compass *bearing* rather than the dial reading, which differ by a quarter
  turn and a flip: it used to display 90 where the run searched 180, which was
  survivable while only the four button values were reachable and wrong at 20 of
  the 24 positions now reachable.
- The nesting direction and both "Use Random Direction" checkboxes are
  remembered between sessions. A restored random-direction run correctly leaves
  its dial disabled, so the direction cannot be set and then ignored.
- `Nesting Settings` collapses, and starts collapsed, like `Helpers` and
  `Logging`. Its dial is the tallest single control in the panel and is a
  set-and-forget choice.
- The three controls in `Nesting Settings` are laid out in two columns, with the
  dial spanning both rows on the right and the checkbox and rotation angle
  stacked to its left.


#### Replay CAM Setup

A new command, beside `Create CAM Job`. Select a CAM job and the nesting workbench
carries **your** setup across to the nested copies: your Profile and Drilling
operations, your LeadInOut and other dressups, your per-feature settings, in your
order. It classifies nothing -- which features belong in which operation was
already decided by you, in the CAM workbench, before nesting ran. Everything else
(parts, operations, order) comes from your job and the layout, so the dialog only
asks what the job cannot say. One job per sheet, every sheet replayed.

- The reader walks the job the way a user builds one. It reads through the
  dressup stack to the operation underneath -- a dressed operation is not *in*
  `Operations`, it is reached through its dressup's link -- so a job whose every
  operation is dressed up reads correctly rather than appearing empty.
- Nested parts are flattened to top-level objects first. CAM cannot see a part's
  placement while it sits inside an `App::Part`: an operation pointing at a child
  inside one produced a toolpath at the child's local coordinates, `[-22.5, 22.5]`
  where the container placement put it at `~[118, 160]`. The transform is a
  `Placement`, never `transformGeometry`.
- The new job is built with the sheet as its stock, so depths re-derive against
  the sheet rather than the source part's stock box.
- Operations are ordered so a nested part is cut before the hole holding it --
  the one ordering exception, and a physical one: the inner part is held only by
  the ring of material around it, so cutting the hole first drops it.
- The result is **verified**: an operation whose base selection resolves to
  nothing computes three commands, raises nothing, and would otherwise post
  plausible G-code that removes less material than intended. Checks are reported
  at two severities by the quality of the evidence.
- The flattened parts are the job's geometry rather than clones of it. A `Clone`
  is a link, so the flattened parts had to outlive the job -- which is why they
  could never be removed and every sheet left a 48-object group at the document
  root.
- `Replay CAM Setup` has its own icon (`Nesting_Replay_Icon.svg`), matching the
  house style: 1448 rectangles, one `<path>` per colour, no curves or strokes.

#### Other additions

- All five panel headings are now collapsible sections: `Nesting Settings`,
  `Optimizations`, `Physics Nesting Settings`, `Helpers` and `Logging`. The three
  diagnostic and set-and-forget groups start collapsed; `Optimizations` and
  `Physics Nesting Settings` start open, because they were plain group boxes
  before and making them collapsible must not also tuck them away.
  The first three follow the selected algorithm -- switching to `Physics` swaps
  `Nesting Settings` and `Optimizations` for `Physics Nesting Settings`. `Helpers`
  and `Logging` always apply, so they are never hidden.
  Collapsing **hides** contents rather than disabling them: a checkable
  `QGroupBox` only greys its children out and reclaims no space, and disabling
  them would silently change what a run uses. `probe_unit_panel.py` pins both
  halves -- children stay hidden, and the run params still see through it.
- A Tasks panel for a replay, with a working **Cancel**. Cancelling is an engine
  feature, so the engine got one; the button would otherwise have been a control
  that did nothing. The panel shows no overall percentage, because there is no
  honest one -- ordering is 35% of the run and the rest is a handful of events.
- Per-stage timings in the replay's report. The figures that took this feature
  from 43.5s to 10.6s had all come out of the fixture validator, which a user
  never runs.


### Changed

- The committed replay fixture no longer asks for a lead-out on
  `DressupLeadInOut001` (`LeadOut` false, `StyleIn` perpendicular). A replay of
  it now carries **2** lead-outs where it carried 28. The lead-in audit's test
  suite no longer hardcodes the lead-out count: it reads the number of dressups
  that ask for one off the job it is checking, and turns six more on itself, so
  the figure cannot go stale when the fixture changes and lead-out coverage does
  not depend on the file. See issues.md NEST-041.
- Display name changed from "Nesting" to **Jet-Nesting** in the workbench dropdown, menu bar, toolbar, task panel title, Addon Manager listing, and README. Internal identifiers (`NestingWorkbench` class, `Preferences/NestingWorkbench` path, command names `Nesting_*`, property groups, icon filenames) are deliberately unchanged to preserve user settings and saved document compatibility.
- A replayed source step is now cut **once per target part** rather than once over
  all of them. FreeCAD's Profile is superlinear in the number of Base entries it
  holds, so a single 23-target operation was paying for 23 targets and achieving
  one. Each part keeps the source operation's whole selection, so the user's
  per-part recipe is reproduced intact; what is multiplied is how many times,
  which is the one thing a single-part source job could not express an opinion
  about.
- Replayed operations are ordered by **where the parts sit**, nearest-neighbour
  from the sheet origin, instead of by source step. One-operation-per-part made
  the list 98 entries long, still grouped by source step, so a part's two steps
  sat 23 entries apart and the torch crossed the sheet between them. Within a
  part, source order is a hard constraint rather than a tie-break preference --
  hole nesting holds a spacer's hole steps back until the nested parts are
  finished.
- Simulate mode now shares master shapes across layouts, as headless mode already
  did. Previously the pool was written and read unconditionally, so in Simulate
  mode -- which builds every layout for real -- layouts 2..N found their
  predecessors' masters and silently took the headless path.
- A rotation-pool width of `0` now means no pool, so serial evaluation can be
  measured rather than argued about.


### Performance

- Simulate's trial repaints are coalesced onto the placement pump instead of
  repainting the whole sheet once per trial layout. Measured on the Simulate
  drawing path: **150.60s -> 127.90s**, a 22.7s / 15.1% saving, with an identical
  result (6 sheets, 18 placed, 0.35397) and every placement still drawn. The
  animations were checked by eye, since the saving is in frames a counter does
  not see.
- A replay of the committed fixture went **43.5s -> 27.5s -> 10.6s** across two
  changes: caching the footprint slices that were being recomputed for shapes
  already in hand (21 calls over 3 distinct shapes, 11.40s of an 11.71s stage),
  and the per-target-part split above. 98 operations, all 98 with cutting motion,
  0 failures. (A later measurement of the same run reads 10.7s; the box is four
  CPUs and these figures move by a few tenths between runs.)
- **The lead-in audit's suite runs its copy-only checks in parallel.** The suite
  is now a table of checks tagged `serial` (they read or mutate the one shared
  replay) or `farmed` (each reopens its own copy of the pristine replay
  template). A coordinator runs the serial chain on the shared job while
  `cpu_count() - 1` worker processes claim the farmed checks from a shared
  queue, largest first, and their results are merged back. Every check still
  runs exactly once, so the reported count is unchanged (**177**). Measured on
  the four-vCPU development box: **205 s in one process -> 82 s** in the best
  run (2.5x), with runs spread to ~115 s when the shared host is busy. Each
  worker also closes its template copies after every check instead of holding
  them all, which drops the coordinator's peak RSS from **1567 MB to 373 MB**.
  `LEADIN_WORKERS=N` sets the count; `1` runs everything in one process, which
  is what `run.sh` uses because it already fans its twenty suites across groups.

### Fixed

- **`Replay CAM` did not carry a `StartPoint` onto the nested copies.** Every other
  setting on an operation is carried verbatim, which is right for values and wrong
  for a point: `StartPoint` is an absolute coordinate saying where on the contour
  the feed begins, and the part moved while the point did not. The contour then
  began at the nearest point on the wire to a coordinate that had nothing to do
  with it -- 40 mm along an edge on one copy, and *correctly* on another by
  accident, because a rectangle's four corners are interchangeable under its own
  symmetry. The toolpath also carried a rapid to the stale coordinate, which on a
  sheet whose origin is the corner is off the stock entirely: on the committed
  fixture, two copies 511 mm apart were both sent the same coordinate and both
  toolpaths reached out to it.
  Each copy now gets the start point moved by the same rigid motion that moved its
  geometry, and the replay **warns** when a point is off every part it targets --
  so this is caught rather than posted. Only points the operation actually reads
  are moved, which on the committed fixture is one operation of 98. See
  [NEST-024](issues.md#nest-024).

- **A Boundary dressup no longer clips against the wrong stock.** A replayed
  Boundary was clipping every nested copy against a solid belonging to the source
  job -- fitted to one part, in one Z frame, at one position. Measured against a
  part-fitted boundary, that produced **no cutting motion at all** where the
  unclipped figure is 88, and the sheet still reported success.
  Boundary is now dropped from the replay with the rest of the step's dressups
  kept, and reported once per dressup. It is not silently dropped, and it is not
  silently patched: a nested sheet cannot tell whether the solid meant the part,
  the sheet, or an exclusion mask, and guessing would produce a plausible
  toolpath that cuts the wrong thing. The replayed job is an ordinary FreeCAD job
  you own, so a Boundary fitted to the sheet can be added and checked there.
  Two side effects: the unsplittable-dressup guard is now empty -- its only entry
  was Boundary, and it existed because of this defect -- and the leaked stock
  object each Boundary constructor left behind (3 per two sheets, 17 if split) is
  gone.
  See [NEST-009](issues.md#nest-009).

- **Nesting no longer turns your geometry into B-splines.** Centring a part used
  `transformGeometry`, the one Part operation that re-fits geometry rather than
  moving it. Every analytic face came back as a `BSplineSurface`, so a cylinder you
  modelled became an approximation of one -- measured a face area of 314.1593
  against 315.0023, a +0.27% shift from a transform that should have been exact,
  and CAM then offsets the toolpath from that approximation. It was also about ten
  times slower than the operation that was wanted, and it made sectioning ~1.8x
  slower, which is most of the nesting-time cost.
  Centring now uses `transformShape`. Same motion -- bounding boxes agree to 0.000
  -- but surfaces stay analytic and the per-face areas now match the source
  exactly. Face numbering is unchanged, which is what the CAM replay relies on to
  address the right feature on every nested copy. See
  [NEST-007](issues.md#nest-007).

- **A layout did not record the algorithm that produced it.** Reopening a Physics
  layout brought up Minkowski, with the wrong algorithm's whole settings section --
  and with the direction to match, because `NestingDirection` was written from the
  Minkowski dial whatever ran, so a Physics run saved a number the Physics search
  never read. A layout now records the algorithm, the direction *that* algorithm's
  dial held, and whether that direction was used at all, so a random run comes back
  with its dial greyed out rather than enabled and consulted.
  The Physics direction dial is now remembered between sessions too. It had no
  preference key at all, so its position was recorded nowhere -- while
  `PhysicsRandomDirection` and `PhysicsRotationSteps` were remembered all along.
  See [NEST-001](issues.md#nest-001).

- **A per-part rotation override was lost on reopen.** The shape table's
  "Rotations" and "Override" columns produced a correct nest and then a layout that
  silently reverted to the global value, because neither was ever written onto the
  part's master shape. Both now round-trip, as a number and a flag separately -- an
  override of 8 and a global of 8 are the same number, so the flag is what tells
  them apart. See [NEST-002](issues.md#nest-002).

- **"Stop At Sheets" did nothing at all.** The dial was read by nobody:
  `_collect_ui_params` never read the spinbox and `_prepare_algo_kwargs` never
  set `algo_kwargs['target_sheets']`, so `GACoordinator` was handed no target,
  defaulted it to 0, and every run went to completion -- silently, with no error,
  warning or log line. The engine was never wrong; only the wiring was, and every
  test covering the target passed it in directly and so bypassed the panel. Two
  lines of fix. Guarded end to end by `probe_target_sheets.py`, which drives a
  real GA run from panel-derived kwargs.
- Hole-nesting reordering replaced **every dressup** in the job with a bare
  operation. `order_operations` was handed the base operations and wrote that list
  straight into `Operations.Group`, which is supposed to hold the outermost
  dressup. All 105 dressups were built, linked and never listed, so the user's
  LeadInOut radii silently vanished from the toolpath -- while every structural
  check still passed, because the checks were looking at the base operations that
  *were* listed. Fixed with `ReplayResult.entry_of`.
- The Tasks panel never appeared, cancelling never worked, and the default
  `TC: 5mm Endmill` controller was present for the whole replay.
- The Tasks panel had no content, because the method was called `open`.
- 96 Draft `ReferenceError`s and an error popup, on every replay. Draft's
  `make_clone` defers `format_object` by one event-loop turn and guards it with
  `if not target: return`, which does not cover a *deleted* object.
- Cancel deleted a replayed operation. The flattened parts are the job's geometry
  and must stay.
- A replayed job copied four named settings rather than every setting the source
  job carried.
- The replayed job and its operations had no view provider, and the failure was
  silent -- the tree rendered flat, the Machine was lost, and the dialog was
  inventing the post.

- A document switch while the panel is open no longer leaves it resolving
  against the previous document, which could nest one document's shapes into
  another. The fields also re-render for the newly active document, and a
  change to a document's unit system is picked up before the next run even
  though FreeCAD raises no event for it.
- The Manual Nester's influence radius can now display values above the field's
  maximum when set from Ctrl+scroll, instead of showing a number that disagreed
  with the radius actually in use.
- **README: described a nonexistent "Transform Parts (Experimental)" command and a
  nonexistent SVG export format.** The README's `## Other Tools` section has been
  corrected: the working command is **Manual Nester** with three modes (Push parts
  Physics / Valid placement only / Auto-rotate to fit), export is DXF only, and
  the two missing features **Replay CAM Setup onto Nesting** and **Check Lead-ins
  Against Nesting** are now documented. **Stack Sheets** is now correctly described
  as **Stack/Unstack Sheets** (a toggle). See NEST-007 for the Manual Nester's
  existence and NEST-033 for the lead-in check.

### Known issues
- **Turning a lead-out on can detach its own lead-in**, leaving it 0.688830 mm
  off the contour it belongs to, after which the lead-in audit reports the run
  as clashing with its own operation. Found by making the test suite build its
  own lead-outs rather than read the count off the fixture (NEST-041).
  **It is not a replay defect and not a stale path** -- it reproduces on the
  main Job with one dressup touched and survives a forced recompute, and the
  replay reproduces the identical gap on every copy. The repro is narrow:
  `DressupLeadInOut` on `Model-BottomStrap`, edges `Edge23`/`Edge24`,
  `RadiusOut=0.9`, `StartAt=OutOfEdge`. Open; see issues.md NEST-042.

### Notes
- A step left **whole** rather than split -- one operation covering every part it
  targets, which happens when it carries an unsplittable dressup -- cannot have a
  `StartPoint` carried, because there is no single frame to move it out of. The
  replay now says so by name instead of leaving the point where it was. Set your
  own start point on those, or let CAM pick.
- `Population Size` deliberately still defaults to 1. Measured on the heavy
  corpus at a contested sheet size and on the n70 customer part, populations of
  1, 3, 4 and 10 all returned the same sheet count, placed count and density,
  while wall clock went 18s -> 86s and 49s -> 197s. Crossover does run across
  those populations (offspring 0 -> 2 -> 16), so this is not the degenerate
  small-population case; `create_ga_population` leaves the first layout
  unshuffled and unrotated, and it wins every generation and seed measured, so
  best-of-N converges on it. A higher default would multiply the cost of every
  first run for no measured gain. Set it as high as you like -- it now sticks.

## [1.0.1] - 2026-08-29

### Changed
- All toolbar icons converted from PNG to SVG for crisp rendering at any size.
- Icons renamed with a `Nesting_` prefix so they cannot collide with other
  addons in FreeCAD's global icon search path.
- `FreeCADGui.addIconPath()` moved out of module scope into `Initialize()`, so
  the workbench no longer affects icon lookup unless it is activated.

### Added
- Explicit `__init__.py` in the six packages that previously relied on implicit
  namespace packages.

## [1.0.0] - 2026-08-06

### Added
- 2D bin-packing nesting of 3D parts onto flat material sheets.
- Minkowski-Sum / No-Fit Polygon (NFP) placement engine with GA optimizer.
- Interactive Manual Nester tool with drag-and-drop and proximity physics repulsion.
- Direct FreeCAD CAM job creation from nested layouts.
- Multi-sheet DXF export utility.
- 2D projection silhouette generator for complex 3D geometry.
- Addon manifest `package.xml` and LGPL-2.1 license.
