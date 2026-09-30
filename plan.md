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

**Step 1 done. Steps 2-7 not started.**

`Tools/Cam/cam_replay.py` exists: the recipe reader, and nothing else. It
touches no geometry and creates no document objects, which is what makes it
testable under plain pytest against stand-in objects.

- 34 new tests, 220 in the suite, all passing.
- Verified against a live job: a Profile with a LeadInOut dressup and a Drilling
  read back in group order, 39/36/17 properties captured, dressup attached to
  its operation despite being listed first, sub-element names preserved, and
  `resolve_source_object` unwrapping the model clone back to the original.

One probe finding folded in below: depths are derived, not authored.

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
2. Geometry flattener. Walk `nested_*` containers via `get_nested_containers`,
   emit top-level `Part::Feature` with `Placement` only, normalise Z so the
   bottom sits at `-thickness`.
3. Job builder. New job per sheet, stock box from the layout properties,
   Z0 = top of stock.
4. Op replay. Two passes, tool controller remapped into the new job, `Base`
   re-pointed at all copies.
5. Ordering. Internals before perimeters via `job.Operations.Group`.
6. Verification and reporting. Non-empty assertion per op; Z frame check as a
   **warning**.
7. `commands/command_replay_cam.py` — command, dialog, selection validation.

## Tests

- **Tier 1 (pytest)** — recipe classification, property-capture exclusions,
  ordering pass, and a regression test for the label prefix over-match.
- **Tier 2 (`freecadcmd`)** — full replay against
  `tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd`: op count
  matches source, every path non-empty, every path spans the nested envelope,
  geometry still analytic. Uses the headless dressup construction path.

## Change log

- Branch created from `Faster-NFP-Calc-Investigate` at `a124552`.
- Step 1 implemented and verified against a live job. The reader is pure logic
  over caller-supplied shapes, so its 34 tests need no FreeCAD — the same tier-1
  approach as `test_freecad_helpers`.
- While building step 1, the depth question was settled: depths are recomputed
  by the op from the stock box, not authored. The Z frame is therefore enforced
  in step 3 (the stock) rather than carried from the user's part, which narrows
  what the step 6 warning has to check.
- Original framing was "nest from a CAM setup, then apply the ops" (a
  classification problem). The user reframed it: the classification is already
  done by hand in the CAM workbench. Scope reduced to replay only, which
  removed the geometric classifier entirely.
- Duplicate-label risk raised and then **retracted** — probe showed FreeCAD
  uniquifies labels. The real identity hazard is label prefix over-matching.
- The user's initial four-scenario taxonomy (holes merged into one op, etc.)
  remains sound but is no longer something this feature computes. It is
  inherited from whatever the user set up on the source job.
