# CAM Replay — applying a user's CAM setup to nested geometry

## The idea

A user sets up CAM on their source parts in the CAM workbench: Profile and Drilling
operations, LeadInOut dressups, per-feature settings. The nesting workbench then
lays those parts out on a sheet. What is missing is the step that carries the
user's CAM setup across to the nested copies.

The workbench already creates a CAM job from a template, but the operations are
the template's, not the user's. This feature replays the user's actual recipe.

The name "replay" is deliberate: the classification of which features belong in
which operation has already been done, by the user, in the CAM workbench. This
feature does not classify anything. It reads a recipe and applies it.

## Status

**Steps 1-7 done. Step 8 in progress — the fixture is being built.**

The feature is complete and wired into the menu, but has only ever run
headless against synthetic geometry small enough to reason about. Step 8 closes
that, and the manual GUI pass follows it.

`Tools/Cam/cam_replay.py` holds two halves:

- the **recipe reader** (step 1) — no geometry, no document objects, so its
  tests run under plain pytest against stand-ins;
- the **flattener** (step 2) — creates document objects, so it is covered by
  both tiers: pure logic in pytest, real geometry in `freecadcmd`;
- the **job builder** (step 3) — creates a real CAM job, so the interesting
  parts can only be checked under `freecadcmd`;
- the **replay** (step 4) — recreates the user's operations and dressups.

- the **ordering** (step 5) — a partial order over the replayed operations.

- the **verification** (step 6) — decides whether a bad run is visible.

- 421 pytest tests passing (35 new for step 7).
- `tests/freecad_harness/test_replay_flatten.py`: 187 checks, 0 failures.
  The full pipeline runs end to end over a two-sheet layout: two jobs, each with
  its own stock at the origin, both verified.

Every part in every one of those checks is a `Part::Feature` built from
primitives, and none of them has more than three parts of a type. That is what
step 8 exists to change.
  A healthy two-part-type replay verifies clean: 2 operations, both with
  cutting motion, 2 warnings carried through from the stock frame comparison.
  The end-to-end replay is now exercised on a real two-part-type job: a Profile
  on the top face with a LeadInOut dressup, plus a Drilling op, replayed into a
  nest of 2 brackets and 1 spacer. The replayed Profile cuts 289.11 mm against
  a 288.87 mm expectation (2 × the source's 144.43 mm), so the toolpath is
  geometrically the same cut, just moved.
- Step 2 verified on a real cylinder: surfaces stay analytic (`Cylinder` in,
  `Cylinder` out, no `BSplineSurface`), edge count and every per-edge length
  unchanged, placement is `container * child` in that order, Z normalised to
  `-thickness..0`, and the offset idempotent.

Two findings from step 7, both real bugs the harness caught:

- **Nothing checked that the parts were on the sheet.** A sheet whose parts
  were never moved to local coordinates kept their world X offsets, left the
  stock, and every other check still passed — the coverage check asked whether
  the toolpath reached its targets, and it did; the targets were simply in the
  wrong place. `parts_outside_stock` now gates on it, with a failure message
  that names the likely cause. A part off the sheet is a scrapped part and
  possibly a crashed tool, so this is a failure, not a warning.

- **The `_UNVERIFIED` label was only applied to verification failures.** An
  operation that could not be replayed at all — its sub-element selection did
  not survive the move — left a job that cuts *less* than the source, and it
  was left unlabelled. The labelling now covers any failure, not just a
  verification one.

One finding from step 6, and it is the kind that would have shipped:

- **Canned cycles are cutting, and a list of `G1/G2/G3` missed them.** A
  Drilling operation emits `G81 [ F:0 R:1 X:.. Y:.. Z:-6 ]` — a canned cycle
  that removes material. The first version of the emptiness check tested
  membership of a `G1/G2/G3` tuple, so the verification reported *"produced no
  cutting motion"* for a perfectly good drilling operation. The rule is now
  derived instead: a G-code that is not `G0` cuts. The tuple is kept as
  documentation and a test asserts it cannot drift from the rule.

Two findings from step 5:

- **`get_2d_profile_from_obj` returns a polygon *centred on the shape*, not in
  world coordinates.** Measured: a shape spanning X 100..120 yields a profile
  spanning -10..10. Using it for the containment test would have reported a
  part sitting well outside a hole as being inside it — a confident wrong
  answer. `part_footprint` slices the shape instead and keeps world
  coordinates, so both polygons are in the same frame.

- **The nester can place parts in holes but records nothing about it.** The
  inner-fit-polygon rings are in the production candidate path
  (`minkowski_engine.get_incremental_candidates`), so hole nesting happens, but
  no relationship is stored on the part. The nesting is therefore recovered
  from the finished layout, by testing whether a part's footprint is contained
  in another part's interior ring.

One finding from step 3:

- **The default stock is padded around the model.** A fresh job's stock is a
  `StockFromBase` fitted to the geometry, which measured Z **-7.0 .. 1.0** for a
  6 mm part — a millimetre of margin above and below. The replay stock is built
  as the sheet exactly, **-6.0 .. 0.0**. So a user who checked their depths
  against their own job was looking at a different Z frame from the one their
  parts will be cut in. This is what the step 3 warning reports. It is a
  warning rather than an error because depths are re-derived from the new stock,
  so the cut is correct either way; only the displayed numbers differ.

Two findings from step 2 that are not in the original constraint list:

- **`App::PropertyLink` into an `App::Part` is out of scope.** A top-level
  feature linking to something inside a container made FreeCAD warn on every
  recompute (`CAMPart_12 links are out of scope. Out of scope links to:
  part_Bracket_1`). Switched to `App::PropertyXLink`, which is the type that
  means cross-scope. Pinned by a test, and confirmed to bite by injecting the
  regression.
- **A part tilted off Z cannot have its top at Z=0.** True by construction and
  left that way: forcing it would shear the geometry rather than place it. The
  nester only rotates about Z, so it does not arise.

The depth question is resolved — see Open items.

Every design decision below was verified against the real FreeCAD 26.3 CAM
module through `freecad_env/usr/bin/freecadcmd`, not inferred from reading the
source. Probes live in `/tmp/opencode/probe_*.py`; they are scratch and are not
part of the deliverable.

An end-to-end replay was proven to work: a user-built job with Profile +
Drilling + LeadInOut was read, and all three ops were recreated against three
rotated nested copies, each covering the full nested envelope.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | New command, standalone. No edits to `cam_manager.py` or `command_create_cam_job.py`. | User's requirement — isolate from the original author to avoid conflicts. Only `freecad_helpers` is imported, which is a shared utility. |
| 2 | User selects the source Job, then triggers the command. | The read side becomes "locate the job the user picked" rather than assuming a template. |
| 3 | Build a new Job per sheet. Stock from the layout's `SheetWidth`/`SheetHeight`/`SheetThickness`. | Stock data already lives on the layout group (`cam_manager.py:64-70`). |
| 4 | Z0 = top of stock. | Matches the existing convention: stock bottom at `-thickness`, top at `0` (`cam_manager.py:210-212`); part bottoms at `-thickness` (`cam_manager.py:103-106`). |
| 5 | Tabs are out of scope. | Handled by the user via dressup or profile settings. |
| 6 | Flatten with `Placement`, never `transformGeometry`. | Preserves analytic geometry and topology. See "Constraints". |
| 7 | Key on `App::PropertyLink`, never on labels. | Label lookups over-match. See "Constraints". |
| 8 | Replay in two passes: base ops, then dressups. | Mandatory. See "Constraints". |
| 9 | Z frame mismatch is a **warning**, not an error. | User wants to observe whether it occurs before deciding how to cope with it. |
| 10 | Log via `FreeCAD.Console.*`; `ReportView` only for an optional summary. | `Console` already routes to the Report view in the GUI and is testable headless. `FreeCADGui.ReportView` is absent under `freecadcmd`. |

## What the probes established

Read side:

- `job.Operations.Group` lists the recipe in processing order.
- A dressup's `Base` is a single `App::PropertyLink`; a real operation's `Base`
  is a `LinkSubList` (a Python list). This property *type* difference is the
  discriminator — do not try to subscript a dressup's `Base`, it raises.
- Property capture works: every `App::Property*` on an op is readable and
  writable, excluding `Base`, `Path`, `ExpressionEngine`, `Proxy` and labels.
- `Path.Dressup.Utils.baseOp()` exists for walking a dressup down to its op.

Write side, proven end to end:

```
copy0 Face1 surface: Plane          <- Placement; transformGeometry gives BSplineSurface
stock: 600 x 400 x 3, top at Z=0
Profile  -> Path.Op.Profile         <- proxy module supplies Create()
Drilling -> Path.Op.Drilling

DressupLeadInOut_replay  ncmds=51  Xrange=(-22.5, 180.69)  OK
Profile_replay           ncmds=50  Xrange=(-22.5, 180.69)  OK
Drilling_replay          ncmds=11  Xrange=(0.13, 160.04)  OK
```

Payoff of merging (why one op can cover many parts): 8 separate hole operations
produced 8 plunges; 1 Drilling operation over all 8 holes produced 13 commands
reaching all 4 parts.

## Constraints

These are not preferences. Each one is a behaviour of the CAM module that will
break the implementation if ignored.

1. **Two-pass replay is mandatory.** `Operations.Group` was observed as
   `['DressupLeadInOut', 'Profile', 'Drilling']` — the dressup sits *before* its
   base op. A single linear pass raises `KeyError` when the dressup cannot find
   its base.

2. **Recreate operations generically via the proxy's module.**
   `type(op.Proxy)` is the proxy *class* and has no `Create` method. Its module
   does: `importlib.import_module(type(op.Proxy).__module__).Create(...)`. This
   is what keeps the replay generic across Profile/Drilling/Pocket instead of
   hardcoding each.

3. **`Placement`, not `transformGeometry`.** Measured: `transformGeometry`
   converts a `Cylinder` face to a `BSplineSurface` and shifts face area
   314.1593 → 315.0023 (0.27% error). The lead-in arc maths runs against real
   geometry, so a splined cylinder means the toolpath follows an approximation.
   `Placement` preserves the analytic surface and keeps the placement off the
   topology — which the replay also depends on, since `Base` stores sub-element
   names.

4. **Container placements are ignored.** An operation whose `Base` points at a
   child inside an `App::Part` produced a toolpath at *local* coordinates
   (`[-22.5, 22.5]` instead of `~[118, 160]`). Nested geometry must be flattened
   to top-level objects before it can be referenced.

5. **`PathJob.Create` clones its base.** `job.Model.Group` became
   `['Clone', 'Clone001']`, and `isSame(original.Shape)` was `False`. Operations
   point at the clones. Objects added *later* via `job.Model.addObject()` are
   **not** cloned — which is the behaviour the write path wants.

6. **Label lookups over-match; duplicate labels are not possible.**
   `a.Label = "Bracket"; b.Label = "Bracket"` yields `['Bracket',
   'Bracket001']` — FreeCAD uniquifies, even with `DuplicateLabels=0`. So the
   duplicate-label risk is *not* real. The real hazard is that
   `findObjects(Label="nested_Bracket_1")` returns `_1`, `_10` **and** `_11`:
   it is a prefix match. This matters directly, since the 122-part corpus is
   exactly where prefix collisions appear.

7. **A silently empty operation is the dangerous failure mode.** A mis-selected
   op computed 3 commands and no cutting motion, with no error raised. Replay
   inherits that silently. Every replayed op must be asserted non-empty.

8. **`LeadInOut.Create()` cannot be used headless.** It touches `ViewProvider`
   and raises `AttributeError` under `freecadcmd`. Construct via
   `ObjectDressup(obj, base_op)` plus `job.Proxy.addOperation()` instead —
   confirmed working headless.

9. **A link into an `App::Part` from outside it is out of scope.** FreeCAD
   warns on every recompute. Use `App::PropertyXLink` for the `SourceObject`
   and `SourceContainer` links, not `App::PropertyLink`.

10. **Harness output needs `FreeCAD.Console`, not `print()`.** Once a document
    exists, `FreeCAD.Console` captures plain `print()`, so check output
    vanishes while the status file is still written correctly. The first
    version of `test_replay_flatten.py` reported nothing at all while failing
    silently.

11. **A cross-job tool controller link is accepted silently.** An operation in
    job B takes job A's `ToolController` with no error, and the toolpath is
    correct. But the new job then depends on the source job surviving. The
    replay copies it with `Path.Tool.Controller.copyTC` instead, cached so ten
    operations sharing one endmill produce one copy.

12. **A sub-element name that no longer resolves must abort its operation, not
    be dropped.** Source sub-names are passed through verbatim, which is sound
    because a `Placement` leaves topology untouched and a nested copy has
    identical topology. But nothing *guarantees* the names still mean what they
    meant, and a valid-looking name addressing the wrong feature produces
    plausible G-code cutting the wrong thing. Every name is resolved against
    each target first; a failure aborts that operation and says so.

13. **Part-type identity is recovered from the nested label, and that is a soft
    spot.** `clones_for_source` parses `nested_<type>_<n>` and compares `<type>`
    against the source part's label. It works because the nester derives both
    from the same source label. It breaks if the two are not derived from the
    same label — which is what happened in the harness when an earlier check
    had already taken the name `Bracket`, so FreeCAD uniquified the source to
    `Bracket001` while the nested labels still said `Bracket`. A robust fix is
    a real `App::PropertyLink` from the master container to the source object,
    which would mean touching `shape_preparer.py`; not done, since this feature
    is deliberately standalone. Until then, a mismatch falls back to *every*
    clone, which over-cuts visibly rather than under-cutting silently.

14. **The sheet spans `0..width`, not `-width/2..+width/2`.** The nesting sheet
    is `Polygon([(0,0), (width,0), (width,height), (0,height)])` and
    `Sheet.is_placement_valid` requires full containment, so parts are always
    inside that rectangle. The replay stock must therefore be built from the
    origin, not centred. An early version of the harness placed a part on the
    origin and reported it as a stock bug; the harness was wrong.

## Open items

- ~~**Depth properties.**~~ **Resolved.** Depths are *derived*, not authored.
  Setting `StartDepth`/`FinalDepth` on a Profile and recomputing leaves them
  unchanged — the op recomputes them from the stock box and the geometry on
  every pass. With a part spanning Z 0..3 and stock spanning Z -3..0, the op
  resolved `Start=3.0 / Final=0.0` and cut at Z 3.0 → 0.0.

  Two consequences, both good:

  - Capturing and replaying the depth properties is harmless, because the new op
    will derive its own from the new job's stock. This is why step 1 does not
    need to treat depth specially.
  - The Z frame is resolved by CAM from the stock box, not from the source
    part's authored Z. The Z0 = top of stock convention is therefore enforced
    by the stock we build in step 3, which is the single place it has to be
    right. The warning specified for step 6 remains worth having, but it is a
    check on our own stock construction rather than on the user's part.
- **Lead-in/out overshoot.** For `Arc`, `Line`, `Perpendicular` and `Tangent` on
  a rectangle, measured overshoot beyond the tool-radius offset was zero.
  `ExtendIn`/`ExtendOut` and the `ZFollow` styles plausibly do extend further.
  Needs a per-style sweep before it is relied on. Not currently a dependency —
  spacing is handled separately.
- **Tool radius vs spacing.** Independent of replay and still open:
  `spacing` is buffered by `spacing/2` (`shape_processor.py:289`) with no
  knowledge of the tool. A 5 mm tool at 4 mm spacing produced merged cut paths
  with no clean gap. Faithful replay does not help if the layout permits
  collision. This was scoped earlier as a separate concern ("Phase 1") and was
  not folded into this feature.

## Implementation steps

Not started. Ordered so each step is independently verifiable.

1. ~~`Tools/Cam/cam_replay.py` — recipe reader.~~ **Done.** Classify ops vs
   dressups by property type; capture the property dict; resolve the
   dressup→base dependency.
2. ~~Geometry flattener.~~ **Done.** Walk `nested_*` containers via
   `get_nested_containers`, emit top-level `Part::Feature` with `Placement`
   only, normalise Z so the bottom sits at `-thickness`. Returns a
   `FlattenedPart` carrying the Z shift, so a report can say which geometry
   arrived somewhere unexpected.
3. ~~Job builder.~~ **Done.** New job per sheet, stock box from the layout
   properties, Z0 = top of stock. Returns a `ReplayJob` carrying the job's
   **clones** — the list an operation must target — alongside the inputs, so
   the two cannot be confused.
4. ~~Op replay.~~ **Done.** Two passes, tool controller **copied** into the new
   job with `copyTC`, `Base` re-pointed at the matching copies, every
   sub-element name resolved against the target before assignment.
5. ~~Ordering.~~ **Done.** The user's order is reproduced exactly. The one
   exception is hole nesting: a part placed inside another's hole is held by
   the ring around it, so the outer part's hole must be cut after everything
   inside it. Implemented as a stable topological sort that only moves what
   physically has to move, and leaves `Operations.Group` untouched when no
   nesting occurred.
6. ~~Verification and reporting.~~ **Done.** An operation with **no cutting
   motion is a failure** and stops the sheet; an operation that reaches only
   some of its targets is a **warning**, because that check compares bounding
   boxes and is approximate. Both severities decided by the user.
7. ~~`commands/command_replay_cam.py`.~~ **Done.** Select a CAM job, the
   layout is auto-detected (warning if several are present), a thin options
   dialog, every sheet replayed one job per sheet. Wired into the Nesting menu
   and toolbar as `Nesting_ReplayCAMSetup`, beside `Nesting_CreateCAMJob` and
   sharing no code with it beyond `freecad_helpers`.
8. **Mid-complexity fixture + headless replay test.** In progress — the fixture
   is being built. See "The replay fixture" below for the full spec.

## The replay fixture

A committed `.FCStd` that exercises the replay at a size and a realism the
synthetic in-script fixtures do not reach.

### Why, specifically

Four gaps, each of which the current fixtures cannot reach:

1. **Scale.** `find_hole_nestings` measured **543 ms on 40 parts** (40 x
   `part_footprint` at 15.1 ms each, plus an O(n^2) containment loop over the
   parts that have holes), and it has never run above 3 parts. At n70's 122
   parts that is roughly 2 s *per sheet*, unmeasured.
2. **Part labels at quantity > 10.** `clones_for_source` recovers the part type
   by splitting `nested_<type>_<n>` on `_` and rejoining the middle. A type
   containing an underscore is verified. The interaction with FreeCAD's label
   uniquification at quantity 10+ is **not** — and that is precisely where
   `findObjects(Label=...)` over-matched, and precisely where n70 lives. This
   is the soft spot already recorded as constraint 13.
3. **Non-circular internal features.** Every internal feature in the current
   harness is a circle. A slot or rectangular pocket is a different ring count,
   a different sub-element set, and a different containment result.
4. **`PartDesign::Body` geometry.** Every synthetic part so far is a
   `Part::Feature` built from primitives. A Body is sketch-based, has different
   topology, and carries an `App::Origin` the nester has to skip
   (`SKIP_TYPEIDS` exists for that).

### Decisions

| Question | Answer |
|---|---|
| Committed `.FCStd` or generated at test time? | **Committed `.FCStd`.** Fast, deterministic, decoupled from nester changes, and openable in a GUI — which is the one untested part of the feature. |
| Primitives or sketch-based geometry? | **Sketch-based** (`PartDesign::Body`). Closer to what actually gets nested. |
| How many parts? | **~40.** Enough to cross the 10-per-type boundary; nowhere near n70. |

Rejected: hand-building the layout, because it drifts from what a real run
emits. Re-running the nester on every test invocation, because it couples the
replay test to nester regressions — confusing when the two features are
independent. Generating once and committing gives authenticity without either
cost.

Note `tests/Test_Files/` currently has **no tracked files at all**: both the
n70 `.FCStd` and its 5.3 MB `.dxf` are gitignored, so nothing in the repo is
openable in a GUI today.

### Validating the fixture before the test is written

`tests/freecad_harness/validate_replay_fixture.py` checks a candidate file and
reports what it contains, without saving anything:

    freecadcmd tests/freecad_harness/validate_replay_fixture.py <path.FCStd>

It prints its assumptions about the starting point **before** any checking, so
a misunderstanding is visible immediately rather than surfacing later as a
confusing structural failure. Structure checks are followed by a dry run of the
whole pipeline, reporting per-sheet outcome, operation count, sub-element
resolution, containment and wall clock.

Verified against both cases: the n70 file (correctly reported as *source
geometry only*), and a purpose-built valid fixture, which it passes with
**0 failures**. A diagnostic that can only fail is not a diagnostic.

Two things it caught while being written, both in code written minutes earlier
— which is the argument for having it:

  * a throwaway fixture generator placed parts at x=20 with a half-width of 30,
    so they started at x=-10. The dry run's containment check reported them
    outside the stock, and the replay's own containment check agreed. The
    generator was wrong, not the replay;
  * the validator's own containment assertion was hardcoded to X 0..300 and
    reported four parts "outside the stock" that were plainly inside a 400 mm
    sheet. It now measures against the actual stock object.

### What the fixture must contain

**The CAM job references the SOURCE parts, not the nested copies.** An
operation's `Base` points at the job's Model clone, which wraps the original
body. The layout is entirely separate, and the job can be set up on the
starting parts alone.

That is worth writing down because its absence caused a real misunderstanding
during design: the fixture was briefly specified as though the job needed to
know about the nest, which made the whole deliverable look much larger than it
is. It does not.

The *only* coupling between the two is a label convention: `clones_for_source`
unwraps the source clone, reads the original's `Label`, and matches it against
`nested_<label>_<n>`.

Labels matter, then, and getting them wrong produces a fixture that looks right
and exercises nothing.

**The layout side** (what a nesting run produces):

    Layout_001                    App::DocumentObjectGroup
      SheetWidth / SheetHeight / SheetThickness   (App::PropertyLength)
      Sheet_1                     App::DocumentObjectGroup
        Sheet_Boundary_1          Part::Feature, a plane, placed at the sheet origin
        Shapes_1                  App::DocumentObjectGroup
          nested_<Type>_1         App::Part, Placement = nest position + Z rotation
            part_<Type>_1         Part::Feature, geometry centred, own Placement
      Sheet_2 ...                 as above

`Sheet_Boundary_N` is **required**, not optional. `sheet_origin_for` reads the
sheet's world origin from it, and without it the parts are never moved to local
coordinates — which is the failure that produced the "parts outside the stock"
check in step 6.

**The CAM side** (the recipe to replay): a Job referencing the source bodies,
with at minimum one Profile on an external boundary, one operation for an
internal feature, and one LeadInOut dressup.

### Required content

**The source body labels must match the `<type>` in the nested container
labels.** A body labelled `Bracket` should produce `nested_Bracket_1`. The
nester derives both from the same source label, so a nest produced by the
workbench satisfies this automatically; it only needs checking when a layout
is assembled by hand.

It is stated as a requirement because the failure is the worst one in this
feature: if the labels drift, `clones_for_source` matches nothing and falls
back to *every* clone, so the operation cuts the wrong feature on parts it was
never set up for — and nothing raises. The validator's identity cross-check
fails on exactly this case, which is one of the main reasons it exists.

- **10–12 copies of one part type.** This is the point of the fixture: it
  crosses the label-uniquification boundary.
- **A part type whose label contains an underscore** (e.g. `L_Shape`), to
  combine the underscore case with quantity 10+.
- **At least one part with a non-circular internal feature** — a slot or a
  rectangular pocket. Closes gap 3.
- **At least one part with a fillet or chamfer.** Closes gap 4 partly, and
  changes edge counts, so it tests sub-element resolution against something
  that is not a primitive.
- **At least one part with a hole large enough to nest a smaller part inside**,
  so hole-nesting ordering is exercised by the fixture rather than only by the
  synthetic check.
- **Two sheets**, so the one-job-per-sheet path is covered at scale. The current
  synthetic test does this with 2 parts per sheet; the fixture should do it
  with a realistic mix.
- **Parts at distinct rotations.** The nester only rotates about Z, so the
  rotations should be spread rather than all zero.

### What the test will assert

Once the fixture exists:

- every replayed operation produces cutting motion;
- every sub-element name resolves on every nested part;
- every part lies within its sheet's stock;
- each sheet produced its own job, at its own origin, with the stock sized from
  the layout properties;
- the replayed toolpaths cover the nested envelope — compared by path length
  against the source, not merely by non-emptiness;
- hole nesting is detected, and the operation order changed accordingly;
- **a wall-clock budget for the replay stage**, so `find_hole_nestings` at
  scale is measured rather than assumed. The budget itself is TBD, pending the
  first real number from the fixture;
- nothing in the replay depends on nester behaviour, so a nester regression
  cannot fail this test.

### Test cycle cost, measured

The alternative to a committed layout is generating one inside the test. That
was measured rather than assumed:

| Step | Time |
|---|---|
| Nesting 42 parts, single sheet, headless | **2.568 s** |
| Replaying 16 parts (open job, flatten, build, replay, order, verify) | **0.646 s** |

So generating the layout inside the test costs roughly 2.6 s per run — a
factor of about 3 on the replay portion, not the order of magnitude it might
have been.

Committing the layout wins anyway, because its one real weakness is guarded:
label drift is caught by the validator's identity cross-check, and by the
replay's own behaviour (a mismatched source part falls back to every clone,
which the cross-check reports). Paying 2.6 s per run to keep a guard that
already exists elsewhere is not a good trade.

A committed layout does carry a second, smaller risk: if the nester's output
*convention* changes, the committed layout keeps the old shape and the replay
test keeps passing against it. That risk is contained, because the nester's own
tests are what would catch a convention change, and the replay depends on only
six label conventions.

### Sequencing

1. Fixture built and committed.
2. Headless test written and run — shakes out scale and topology.
3. **Only then** the manual GUI pass, which will then be testing only the GUI
   layer rather than also turning up structural problems.


## Tests

- **Tier 1 (pytest)** — recipe classification, property-capture exclusions,
  ordering pass, a regression test for the label prefix over-match, and
  flattening's pure logic. 249 tests.
- **Tier 2 (`freecadcmd`)** — `test_replay_flatten.py` covers what a stand-in
  cannot: that geometry survives flattening, topology is intact, placement
  order is honoured, Z normalisation is exact and idempotent, and the link
  types are in scope. Still to come: the full replay against
  `tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd`, asserting op
  count matches source, every path non-empty, and every path spans the nested
  envelope.

  It also covers step 3: that the job really clones its base, that the stock is
  the sheet exactly, that parts land on it, and that the Z frame difference is
  reported rather than fatal.

  `test_replay_flatten.py` is not yet wired into
  `tests/freecad_harness/run.sh`; it writes `.last_status_replay` and should be
  added when the replay harness grows.

- **Tier 3 (fixture)** — the committed `.FCStd` described under "The replay
  fixture". This is the only tier that uses real sketch-based geometry, a
  realistic part count, or committed binary input, and the only one whose
  result is openable by hand in a GUI. Nothing in tiers 1 and 2 can reach
  those, which is the whole reason it exists.

## Change log

- Step 8 opened. A mid-complexity committed fixture was agreed over the
  synthetic-only alternative, after measuring that `find_hole_nestings` takes
  543 ms on 40 parts and has never been run above 3. The scale question was
  the one that decided it.
- The fixture design went A (commit source + layout + job) vs B (commit source
  + job, generate the layout in the test) and back to A. B was argued for on the
  grounds that a job must somehow know about the nest; it does not, which was
  clarified and written down. A then won on measurement — B costs 2.6 s per
  run — and on the fact that A's real weakness, label drift, is already caught
  by the validator's identity cross-check.

- Branch created from `Faster-NFP-Calc-Investigate` at `a124552`.
- Step 1 implemented and verified against a live job. The reader is pure logic
  over caller-supplied shapes, so its 34 tests need no FreeCAD — the same tier-1
  approach as `test_freecad_helpers`.
- While building step 1, the depth question was settled: depths are recomputed
  by the op from the stock box, not authored. The Z frame is therefore enforced
  in step 3 (the stock) rather than carried from the user's part, which narrows
  what the step 6 warning has to check.
- Step 2 implemented. Two test bugs found and fixed before the checks were
  trusted: the multiplication-order check passed vacuously with an identity
  child (and could not distinguish a Y-rotation of a point already on the Y
  axis), and one test case used a 90-degree Y rotation on the child, which tips
  a part onto its side — not something the nester produces, and it made a
  correct placement look like a Z-normalisation failure. Both cases were wrong,
  not the module.
- Step 4 implemented. Two more harness bugs, and one real code bug:
  the identity matching compared the *source job clone's* label
  (`Model-Bracket`) against `nested_Bracket_1` instead of unwrapping to the
  original first, so nothing matched and every operation silently fell back to
  targeting every clone — the Drilling op cut 11 commands across all three
  parts instead of 7 on the one spacer. Also: the replay harness shared a
  document with the flatten checks, so FreeCAD uniquified a label underneath
  it; and the path-length faithfulness check counted G0 rapids, measuring the
  distance between parts rather than what was cut (343.73 mm for a 288.87 mm
  cut).
- Step 3 implemented. The harness caught a third bad test case of the same kind:
  a part was placed on the sheet origin, which the nesting side never does, and
  the "part is not inside the sheet" check flagged it. Three test bugs across two
  steps, all of them the check being wrong rather than the code — worth noting
  because the checks that catch bad test data are the ones doing their job.
- Original framing was "nest from a CAM setup, then apply the ops" (a
  classification problem). The user reframed it: the classification is already
  done by hand in the CAM workbench. Scope reduced to replay only, which
  removed the geometric classifier entirely.
- Duplicate-label risk raised and then **retracted** — probe showed FreeCAD
  uniquifies labels. The real identity hazard is label prefix over-matching.
- The user's initial four-scenario taxonomy (holes merged into one op, etc.)
  remains sound but is no longer something this feature computes. It is
  inherited from whatever the user set up on the source job.
