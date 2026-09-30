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

**Steps 1-5 done. Steps 6-7 not started.**

`Tools/Cam/cam_replay.py` holds two halves:

- the **recipe reader** (step 1) — no geometry, no document objects, so its
  tests run under plain pytest against stand-ins;
- the **flattener** (step 2) — creates document objects, so it is covered by
  both tiers: pure logic in pytest, real geometry in `freecadcmd`;
- the **job builder** (step 3) — creates a real CAM job, so the interesting
  parts can only be checked under `freecadcmd`;
- the **replay** (step 4) — recreates the user's operations and dressups.

- the **ordering** (step 5) — a partial order over the replayed operations.

- 331 pytest tests passing (18 new for step 5).
- `tests/freecad_harness/test_replay_flatten.py`: 137 checks, 0 failures.
  The end-to-end replay is now exercised on a real two-part-type job: a Profile
  on the top face with a LeadInOut dressup, plus a Drilling op, replayed into a
  nest of 2 brackets and 1 spacer. The replayed Profile cuts 289.11 mm against
  a 288.87 mm expectation (2 × the source's 144.43 mm), so the toolpath is
  geometrically the same cut, just moved.
- Step 2 verified on a real cylinder: surfaces stay analytic (`Cylinder` in,
  `Cylinder` out, no `BSplineSurface`), edge count and every per-edge length
  unchanged, placement is `container * child` in that order, Z normalised to
  `-thickness..0`, and the offset idempotent.

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
6. Verification and reporting. Non-empty assertion per op; Z frame check as a
   **warning**.
7. `commands/command_replay_cam.py` — command, dialog, selection validation.

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

## Change log

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
