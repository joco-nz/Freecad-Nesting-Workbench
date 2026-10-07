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
| [NEST-001](#nest-001) | Nesting direction is recorded from the Minkowski dial whatever algorithm ran | resolved | high | persistence |
| [NEST-002](#nest-002) | Per-part rotation override is read but never written | resolved | medium | persistence |
| [NEST-003](#nest-003) | Workbench and benchmark disagree on the simplification default | open | medium | benchmark |
| [NEST-004](#nest-004) | Synthetic heavy corpus is calibrated to a piece count production no longer produces | open | medium | benchmark |
| [NEST-005](#nest-005) | Simplification destroys the rotational symmetry of part outlines | limitation | low | geometry |
| [NEST-006](#nest-006) | Deflection tooltip states unmeasured ranges | open | low | docs |
| [NEST-007](#nest-007) | Nesting converts analytic source geometry to B-splines to centre it | resolved | medium | geometry |
| [NEST-008](#nest-008) | Hole-nesting detection costs 148 ms per part on real geometry | open | medium | performance |
| [NEST-009](#nest-009) | a replayed Boundary dressup clips against the SOURCE job's stock | resolved | high | CAM replay |
| [NEST-010](#nest-010) | the flattened parts could not be removed: the job's clones were links | resolved | medium | CAM replay |
| [NEST-011](#nest-011) | two toolbar buttons had the same icon | resolved | low | UI |
| [NEST-012](#nest-012) | the replay took 43 seconds | resolved | medium | performance |
| [NEST-013](#nest-013) | FreeCAD's Profile is superlinear in its number of base targets | open | medium | performance |
| [NEST-014](#nest-014) | a compensated profile of a face at the stock top collapses to a line | resolved | low (was high) | test fixture |
| [NEST-015](#nest-015) | hole-nesting reordering left every dressup unlisted | resolved | high | CAM replay |
| [NEST-016](#nest-016) | position ordering put each spacer's boundary before its own holes | resolved | medium | CAM replay |
| [NEST-017](#nest-017) | the per-stage timing table was missing the largest cost, and 60% of the run | resolved | medium | diagnostics |
| [NEST-018](#nest-018) | Tasks panel progress, and cancellation did not exist | resolved | medium | UI |
| [NEST-019](#nest-019) | the Tasks panel never appeared, and cancelling never worked | open | medium | UI |
| [NEST-020](#nest-020) | the default 5 mm endmill was present for the whole replay | resolved | medium | CAM replay |
| [NEST-021](#nest-021) | the Draft `ReferenceError` flood | resolved | low | diagnostics |
| [NEST-022](#nest-022) | Logging is 230 ad-hoc call sites and 165 exception handlers that record nothing | open | medium | diagnostics |
| [NEST-023](#nest-023) | "Stop At Sheets" was a dead control: read by nobody, never reached the engine | resolved | high | UI wiring |
| [NEST-024](#nest-024) | the replay did not carry a Profile's StartPoint onto the nested copy | resolved | medium | CAM replay |
| [NEST-025](#nest-025) | `rotation_params` is threaded through six call sites and read by nobody | open | low | dead code |
| [NEST-026](#nest-026) | `Array.Centre` and `Tags.Positions` are points on the part, carried verbatim | open | medium (unmeasured) | CAM replay |
| [NEST-027](#nest-027) | a sub-element name that resolves to a different face is invisible | resolved | medium | CAM replay |
| [NEST-028](#nest-028) | centring does nothing for a part that arrives pre-positioned | open | medium (unmeasured) | geometry |
| [NEST-029](#nest-029) | the committed nested fixture predates NEST-007, so the gate cannot see that fix | open | medium (unmeasured) | test fixture |
| [NEST-030](#nest-030) | nesting `spacing` and the CAM tool diameter are unrelated controls | resolved | low | CAM replay |

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

`status: resolved` · `severity: high` · `area: persistence`

Set the Physics direction dial, run the Physics algorithm, and the layout saved
a direction that is not the one that produced the nest.

**The entry below recorded this as one defect. It was three**, and the one written
down was the smallest. What the investigation found is in *Resolved* at the foot
of this entry; the original text is kept as it was written.

Three places that should agree do not:

| role | location | dial used |
|---|---|---|
| direction a Physics run actually uses | `nesting_controller._prepare_algo_kwargs` | `physics_direction_dial` |
| direction written onto the layout | `nesting_controller._collect_ui_params` | `minkowski_direction_dial`, unconditionally |
| direction restored on reopen | `nesting_controller._load_params_from_layout` | `minkowski_direction_dial`, unconditionally |

The write is `nesting_job.NestingJob._apply_properties` (`PROP_NESTING_DIRECTION`),
populated from the unconditional read.

The asymmetry behind it: the Minkowski dial is over-persisted — it is written
even for Physics runs — and the Physics dial is under-persisted, with no
`PROP_PHYSICS_DIRECTION` constant and no entry in `save_settings`, so its
position is never recorded anywhere at all. Reopening a Physics layout leaves
the Physics dial at whatever it currently holds.

Long-standing, and not introduced by the direction-button work. It was less
visible while both dials defaulted to the same value, because the wrong dial
usually held the right answer by coincidence.

**Fix direction.** Record the value that was actually used — branch on the
algorithm at the read, the way `use_random_direction` already does — and give the
Physics dial its own persisted property so a Physics layout round-trips. Both
halves are needed: fixing only the read still leaves the Physics setting
unrecoverable, and fixing only the storage still writes the wrong value.

### Resolved

**Three defects, not one.** Characterised against a real document before changing
anything, with the two dials at deliberately different readings (`Minkowski 90`,
`Physics 270`) — a single dial lets a write that always takes the Minkowski one
pass by coincidence.

| # | defect | evidence |
|---|---|---|
| a | **`Algorithm` was read on reload and written by nothing.** | `getattr(layout_group, "Algorithm", "Minkowski")` was the *only* occurrence of that string in `freecad/` or `tests/`. A Physics layout reopened as Minkowski, showing the wrong algorithm's entire settings section. This is the root of the direction confusion, and a bigger bug than the direction. |
| b | **The algorithm restore was gated on an unrelated property.** | It sat inside `if steps > 0:`, where `steps = getattr(layout_group, PROP_GLOBAL_ROTATION_STEPS, 0)`. A layout without a rotation-step property never restored the algorithm at all. |
| c | **Two independent paths to the direction bug.** | `_collect_ui_params` filled the params key from the Minkowski dial, *and* `save_settings` persisted preferences straight from `self.ui.minkowski_direction_dial`, bypassing `settings` entirely. Fixing only the first would have left the preferences wrong — a half-fix the fix direction below did not mention. |

Plus two smaller ones: the layout recorded **no random flag**, so a random run
recorded a dial reading that was never consulted; and the restore set the Minkowski
dial from `NestingDirection` **before** the algorithm was known.

**Measured, before:** a Physics run with `Minkowski=90 Physics=270` recorded
`NestingDirection=90`, with no `Algorithm` and no `RandomDirection` on the layout
at all.

**What changed.**

* `PROP_ALGORITHM` and `PROP_STRING` added to `constants.py`; `PROP_PHYSICS_DIRECTION`
  added for the preferences, where both dials are remembered separately —
  matching the `PhysicsRandomDirection` / `PhysicsRotationSteps` pair that already
  worked that way, and the dial was the one omission.
* `_apply_properties` records `Algorithm`, branches `NestingDirection` on it, and
  records `RandomDirection`.
* `_collect_ui_params` branches `nesting_direction` on the algorithm — the same
  shape `use_random_direction` already used — and carries both dial readings so
  `save_settings` has one source instead of two that can disagree.
* `_load_params_from_layout` restores the algorithm **first** and **outside** the
  rotation-steps gate, then hands the direction to a new
  `_load_direction_from_layout`, which restores that algorithm's dial from its own
  property and re-applies the random flag, so a random run comes back with the dial
  greyed out rather than enabled and consulted.
* `load_persisted_settings` restores the Physics dial, and its
  `_set_direction_control_enabled` call now names the active algorithm's dial
  instead of falling back to the Minkowski one. That function's docstring claimed
  "the one whose checkbox is connected without arguments", which had stopped being
  true when the Physics checkbox began passing its own.

**After:** `Algorithm='Physics'`, `NestingDirection=270`, `RandomDirection=False`,
and a new session restores both dials at 90 and 270.

Old layouts still open. With no `Algorithm` the fallback is `DEFAULT_ALGORITHM`,
which is the panel's own default and what those files were in fact run with; with
no `RandomDirection` the dial is left as the session had it. A layout records
**one** direction, because it records one algorithm and that says which dial the
number came from — the other dial is not that layout's business.

**Coverage.** `tests/freecad_harness/test_layout_persistence.py` (37 checks, gated)
for the write half on a real document, including a save/reopen round trip.
`tests/freecad_harness/probe_layout_restore.py` (28 checks, GUI) for the reload
half: the panel cannot be built under `freecadcmd`, so that tier is the only one
that can see it. `tests/test_layout_persistence/` guards the read/write *pairing*
structurally, under plain CPython.

Injection-checked: reverting the branch in `_collect_ui_params` fails two probe
checks, both traceable to that one line.

---

## NEST-002

**Per-part rotation override is read but never written**

`status: resolved` · `severity: medium` · `area: persistence`

`nesting_controller._load_shapes_from_layout` reads a per-part override:

```python
steps_map[label] = getattr(master, "PartRotationSteps", 0)
```

No `addProperty("PartRotationSteps", ...)` existed anywhere in the codebase, so
the attribute was never present and the read always yielded `0`. A user who sets
a rotation override for one part type in the shape table got a nest that honoured
it, and a reopened layout that silently reverted to the global value.

A control that appears wired and is not is worse than one that is absent, which
is what the shape table's "Override" column amounted to today.

**Fix direction.** Either write the property in the same place the quantity and
`FillSheet` metadata are written (`shape_preparer._create_master_container`), or
stop reading it. Writing it is the better outcome since the control already
exists.

### Resolved

**Two missing writes, not one.** `PartRotationOverride` — read on the line above —
had the identical defect and was not in this entry.

| property | read at | written at, before |
|---|---|---|
| `Quantity` | `_load_shapes_from_layout` | `shape_preparer.py` ✓ |
| `UpDirection` | same | ✓ |
| `FillSheet` | same | ✓ |
| `PartRotationOverride` | same | **nowhere** |
| `PartRotationSteps` | same | **nowhere** |

`PartRotationOverride` was worse than merely missing: it defaulted to `[]` on the
read side — a **list** — and was handed straight to
`add_part_row(override_rotation=...)`, where a list is falsy by accident rather
than by being off. It is now an `App::PropertyBool`, written and read as one.

**The read half was correct all along** and simply had nothing to read:
`_load_shapes_from_layout` already split the pair properly (`steps` to the spinbox,
`override` to the checkbox) and `add_part_row` already accepted both. No change was
needed there beyond the default's type.

**The nest already honoured the override**, which is why this was only ever a
reopen bug: `part_params['rotation_steps']` is the *resolved* count
(`rot_val if override else global_rot`), and `shape_preparer` feeds it to the
nester.

**Which is also what fixes what to store.** The resolved value cannot be persisted
as the override: an override of 8 and a global of 8 are the same number, so writing
it would reload the checkbox wrong. The raw pair now travels beside it as
`rotation_steps_override` / `rotation_override`, inside the `part_params` dict
whose four-key contract was already documented on `shape_preparer.prepare_parts`.

**After:** a part overridden to 12 records `PartRotationSteps=12,
PartRotationOverride=True`; an un-overridden part holding 4 in its spinbox records
`4, False`. Both survive a save/reopen.

**Coverage.** `test_layout_persistence.py` asserts the values and types on a real
document; `probe_layout_restore.py` drives the real panel through
`controller.load_layout()` and asserts the shape table's two columns came back.
Injection-checked: writing the *resolved* count instead of the raw one fails two
checks, one of which exists only to keep the fixture able to tell them apart.

### The guard, which is the real point

`tests/test_layout_persistence/` checks that **every property either reload path
reads is one that gets written**, for the master container and for the layout
group. It found both halves of NEST-002 and NEST-001's `Algorithm` at once, and it
is the general answer rather than four more one-off assertions.

Two things about it are worth recording, because both happened:

* **Its first version was silently vacuous.** `NestingJob._apply_properties` writes
  property names as *constants* — `self._set_prop(target_layout, PROP_LENGTH,
  PROP_SHEET_WIDTH, ...)` — so a helper recognising only string literals matched
  *nothing* in the write half. `_set_prop_names` returned an empty set, every
  written property looked unwritten, and the layout guard passed for the wrong
  reason. Fixed by resolving `constants.py`, with
  `test_the_constant_map_is_not_empty` added so a future change to that file fails
  loudly rather than quietly narrowing the guard's scope.
* **The matching GUI probe had the same class of fault, and worse.** `check` was
  declared `check(condition, message, detail)` while every call site was written in
  `probe_unit_panel.py`'s `check(label, condition, detail)` order — so `condition`
  received the *label string*, a non-empty string is truthy, and **all 28 checks
  passed unconditionally**. Caught only by reverting the fix and watching the probe
  stay green. It now declares `label` first and tests `condition is True`, so a
  non-boolean cannot slip through the same way.

---

## NEST-025 — `rotation_params` is threaded through six call sites and read by nobody

`status: open` · `severity: low` · `area: dead code`

Found while fixing NEST-002, and **deliberately not fixed there**: it is a signature
change to a method with nine call sites, two of them in gated tests, and bundling
that into a persistence fix would make the fix harder to review and harder to
revert.

`NestingController._collect_job_parameters` builds a fourth dict alongside
`quantities`:

```python
rotation_params[label] = (rot_val, override)
```

and returns it. It is passed down through `nesting_controller` (four sites) into
`GACoordinator.run(..., rotation_params, algo_kwargs, ...)`
(`ga_coordinator.py:502`), where it appears **once, in the signature, and is
referenced nowhere in the body**. The run reads `rotation_steps` from `ui_params`
instead.

It is the fourth instance of this shape, after NEST-023's dead "Stop At Sheets"
control, and it predates them: the per-part override reached the nester by a
different route (`part_params['rotation_steps']`) and `rotation_params` was left
behind when that route was added.

It is now redundant as well as dead: `part_params` carries
`rotation_steps_override` / `rotation_override`, which is the same pair.

**Fix direction.** Delete the parameter from `GACoordinator.run` and its four
call sites, plus the two harness callers (`bench_ga.py`, `test_ga_loop.py`) and the
two probes. Or, if it is deliberately kept as a seam for per-part rotation work,
give it a docstring saying what it is for — a parameter nobody reads reads as a bug
to the next person either way.

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

`status: resolved` · `severity: medium` · `area: geometry`

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

### Resolved — both halves, in two commits

**Done: the nester's centring** (`43003e1`), and **done: the four `cam_manager`
call sites**. Both were the same substitution and the same argument; the second
was the more consequential of the two, because `cam_manager`'s output *is* the
geometry the CAM job offsets its toolpath from.

`transformGeometry` → `transformShape`, once per call site, behind a shared
`freecad_helpers.bake_rigid(shape, placement)`. `_is_rigid` moved out of
`shape_preparer` to `freecad_helpers.is_rigid_matrix` so both modules share one
implementation rather than two copies of a predicate that has to be right.

**The four `cam_manager` matrices are rigid by construction, not by luck.** Each
comes from `Placement.toMatrix()`, and a `Placement` is a rotation and a
translation with no scale to express. So there is no hand-composed matrix to
guard here as there was in `_center_3d_shape` -- the guard moved *into*
`bake_rigid`, which accepts a `Matrix` as readily as a `Placement` and checks
whichever it is given. That is the version worth having: the check fires where
the type system stops guaranteeing anything.

**`cam_manager` does not have NEST-028.** Line 99 zeroes the shape's placement
before transforming, so the container and child placements are applied exactly
once. Worth recording because it is the difference between that code being a
drop-in for this fix and being a trap.

**Verified through the real method, not the helper.** `CAMManager._create_job_for_sheet`
on a sheet holding a plate with a hole, rotated 33° in its container, produces
`CAM_Parts_Sheet_1` with surface types `['Cylinder', 'Plane']`. Reverting a
single one of the four call sites to `transformGeometry` flips that to
`['BSplineSurface']` and fails the check -- so the assertion is on the shape CAM
actually receives, and it fails when the fix is undone. The compounds are built
before the GUI-dependent job creation, which is what makes this inspectable
headless (`freecadcmd` cannot import `PathGui`; the collection does not need it).

**`transformShape` modifies its input in place and returns the same object.**
Measured, because the first version of `bake_rigid`'s docstring claimed the
opposite -- written from an assumption about what `copy=True` means rather than
from a measurement of it. `copy=True` does **not** mean "leave the original
alone": the input's bounding box moves by the full transform (measured 15.553 mm
for a 29° rotation plus a 12/-7/3 mm offset) and its `Placement` becomes the
applied one. `transformGeometry` *does* leave the original alone. So the
docstring now states the measured contract, **pass a copy the caller owns**, and
the test pins it -- including that the same object comes back. No product bug
results, because every call site already hands over a disposable `.copy()`;
there is deliberately no internal copy in `bake_rigid`, as that would be a
second copy of every part in every nest for nothing.

Gate: 12 freecadcmd suites, 1012 checks, 0 failures, plus 543 pytest.
`test_shape_preparer_rigid.py` covers 38 of them, 12 new in this half.

**The open question above is answered — face ordering is preserved.** Measured on
the committed fixture's own `PartDesign` bodies, through the real
`prepare_parts`, comparing each master shape against its source body face by face
**by index**:

| body | faces | source types | master types | worst face-area change by index |
|---|---:|---|---|---:|
| `SimpleSpacer` | 42 → 42 | `Cylinder`, `Plane` | `Cylinder`, `Plane` | 2.8e-14 |
| `BottomStrap` | 12 → 12 | `Cylinder`, `Plane` | `Cylinder`, `Plane` | **0.000** |
| `TopStrap` | 11 → 11 | `Cylinder`, `Plane` | `Cylinder`, `Plane` | 1.4e-14 |

Volumes are now identical to source (`SimpleSpacer` 62097.5172 → 62097.5172;
before, 62097.5172 → **62090.9305**). Face *N* is the same face by index, not
merely the same count -- which is the property `cam_replay` and `cam_manager`
depend on, and the reason NEST-027's face-agreement check can be exact rather than
tolerance-based.

**Same motion, measured rather than assumed.** Against `transformGeometry` on the
same matrix: bounding boxes agree to **0.000** (identity placement) and 7.1e-15
(rotated), volume to 2.7e-12, and the result survives `_handle_new_master`'s
placement reset identically. Timing on a cylinder: **19x** faster, 1.5 ms against
28 ms per call.

**Why not a bare `Placement`, which was the obvious first attempt.** A rigid motion
needs no scale and `Placement` cannot carry any, so it looks like the right tool --
and it is not, here. `_handle_new_master` resets the master's placement to
`(0, 0, 0)` immediately afterwards, discarding a centring left in the placement.
Measured: a `Placement`-assigned centring moves 5.000 mm when that reset runs, and
the baked one does not move at all. The transform has to be baked into the
geometry.

**The guard, and why it exists at all.** `transformShape` *requires* a rigid matrix
and applies a non-rigid one as though exact -- where `transformGeometry` would have
re-fitted and so tolerated anything, which is precisely why it was wrong.
Replacing it made the assumption load-bearing, so `_is_rigid` checks it and
`_center_3d_shape` refuses a non-rigid matrix rather than distorting a part. It
uses `hasScale()` (measured 0 for rigid, 3 for a uniform scale, **-1 for a
shear**) plus `determinant()` to catch a reflection, which preserves lengths and
so reports no scale. **`Matrix.isOrthogonal()` is deliberately not used**: it
returns `0.0` for a rigid, a scaled *and* a sheared matrix alike in FreeCAD 26.3,
and its name invites exactly that reliance.

**Coverage.** `tests/freecad_harness/test_shape_preparer_rigid.py`, 26 checks,
gated and wired into `run.sh`. Injection-checked both ways: reverting to
`transformGeometry` fails 2 checks, and weakening `_is_rigid` to a
determinant-only test fails the shear check -- which is the case a
determinant-only guard provably cannot see, since that shear's determinant is
exactly 1.0.

**A latent defect found on the way, deliberately not fixed here.** With a
**non-identity placement** on the master object, `_center_3d_shape` is a **no-op**:
the matrix is applied to the local geometry and then the object's own placement is
applied on top, cancelling it, so input and output bounding boxes are identical. A
part that arrives pre-positioned is never centred.

True of `transformGeometry` and `transformShape` equally, so it is pre-existing and
orthogonal to this change, and fixing it would **move parts** -- a behaviour
change, not a fidelity one. Filed as NEST-028 rather than bundled here.

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

`status: resolved` · `severity: high` · `area: CAM replay`

**RESOLVED — by not replaying it.** `Path.Dressup.Boundary` is now in
`DRESSUP_UNSUPPORTED`: dropped from the replay, the rest of the step's stack kept,
and the user told once per dressup. Not patched, because there is no patch that is
right for all the cases; the reasoning is at the foot of this entry and the
measurement that rules out the alternatives is above.

Found by extending `test_replay_dressups.py` to a three-deep stack
(LeadInOut -> Dogbone -> Boundary), the shape FreeCAD's own `dressuptest.FCStd`
uses.

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

**When exactly does the clipping degenerate? Now measured, not inferred.** Found
while building NEST-024's unsplit-step check, which needs a Boundary that
actually cuts so there is a working toolpath to reason about.

`PathBoundary` clips with `edge.common(shape)`, and it produces an **empty path**
whenever the contour is not strictly inside the clip solid's Z band. One
operation, one contour cut at Z 0, five clip solids of identical XY extent
(700x400) differing only in Z:

| clip solid Z band | ZMax | cuts after Boundary | cuts before |
|---|---:|---:|---:|
| -2 .. 4 — straddles the contour | 4.00 | **5** | 5 |
| 0 .. 6 — top face above the contour | 6.00 | **4** | 5 |
| **-6 .. 0 — top face AT the contour** | 0.00 | **0** | 5 |
| -6 .. -2 — band entirely below it | -2.00 | **0** | 5 |
| 0.5 .. 6.5 — band entirely above it | 6.50 | **1** | 5 |

The count varies across the working rows because `Inside=True` clips against a
700x400 box and the part sits at X -30..30, so part of the contour falls outside
in XY. That is the XY story and is not what this table is about. The Z story is
the two zero rows.

Three things this changes:

* **The rule is "strictly inside the Z band", not "not coincident".** Fix 2 above
  said the `CreateFromBase` stock avoids the degeneracy "because a
  `CreateFromBase` stock spans z -3..1 with the cut strictly inside". That is the
  working condition, and it is now a stated rule rather than one observation.
* **Fix 1 is dead on arrival, not merely worse.** `DRESSUP_JOB_LINKS = {}` is
  where the fix goes, and repointing `Stock` at the replay stock will *always*
  produce an empty path: that stock spans `-thickness .. 0` and the contour is cut
  at Z 0, so its top face is the contour's Z by construction.
* **The constraint on any fix is checkable in one assertion**: the boundary
  solid's `ZMax` must be strictly greater than the contour's Z. That belongs
  beside whatever the fix does — it is the same kind of check
  `compare_stock_frames` already makes about the stock frame.

One trap this cost while measuring it, worth not repeating: a Boundary built
against the **replay sheet stock** cuts nothing, so a check written against it
"passes" while testing nothing at all. NEST-024's unsplit-step check had to use a
solid spanning Z through the contour instead.

Worth noting alongside NEST-024: this is the same failure shape one dressup over.
A value carried verbatim that names the wrong thing, with nothing to say so.

### Measured, and what it changed

Characterised against real documents before changing anything, on a fixture
built from plain boxes so the nested positions are known exactly
(`tests/freecad_harness/test_replay_boundary.py`). Three of the entry's claims
did not survive measurement.

**The defect is worse than recorded.** The entry says 6 of 8 split copies
produced "a 4-command path with no cutting motion". With a boundary fitted to
the part — the realistic case, and what `CreateFromBase` gives — the replay
produces **no cutting motion at all**, and still reports `ok=True` with clean
verification:

    source Boundary, one part                11 cutting moves
    replayed, whole, 8 matching parts          0
    the unclipped ceiling                    88

On the dressup fixture's *sheet-fitted* boundary the same defect reads 23 cutting
moves against an 88 ceiling. So the figure depends entirely on what the boundary
is fitted to, and the part-fitted case is the empty one.

**The Z rule is one predicate, not three.** The entry records the contour must be
"strictly inside the clip solid's Z band", which reads as a two-sided condition.
It is one-sided. Sweeping the boundary's band with sheet XY held fixed, in the
real replay, where the contour sits at Z 0 and the replay stock's top face is Z 0
by construction:

| boundary Z band | ZMax | cutting moves |
|---|---:|---:|
| −2.0 … 0.0 | 0.0 | **0** |
| −1.0 … 0.0 | 0.0 | **0** |
| −6.0 … 0.0 | 0.0 | **0** |
| −2.0 … 0.0005 | 0.0005 | 93 |
| 0.0 … 1.0 | 1.0 | 93 |
| −0.001 … 0.001 | 0.001 | 93 |

**`ZMax` must be strictly greater than the contour's Z. `ZMin` is irrelevant** —
a contour lying exactly in the boundary's *bottom* face is fine. This is what
makes Fix 1 dead by construction rather than merely worse: the replay stock's top
face is Z 0 and the contour is at Z 0, so repointing `Stock` at it always
produces an empty path. The entry's rule was directionally right and would have
led to an implementation that checked both sides.

**Why a part-fitted boundary fails at all**, which the entry leaves as "barely
larger than the part": tool compensation. A 5 mm endmill on `Side=Outside` puts
the tool centre 2.5 mm beyond the nominal edge, so the path for a 120 × 50 part
spans **125 × 55**. `CreateFromBase` fits 122 × 102, which is larger than the
part and smaller than the path:

| boundary half-width in X (Y fixed at 26) | cutting moves |
|---:|---:|
| 60.0 / 61.0 / 62.0 | 0 |
| 62.5 | 8 |
| 63.0 … 70.0 | 8 |
| 62.5 in X **and** 27.5 in Y | 11 (full) |

So a Boundary must exceed the part by **twice the tool radius**, and FreeCAD's
own default does not. This is measured in the source job, with no replay
involved, which is why it survived being read as a replay problem.

**`UNSPLITTABLE_DRESSUPS` is a band-aid over this defect.** Its docstring gives
the reason for leaving a Boundary whole: splitting "multiplies a known-wrong
reference and turns 'clipped slightly wrong' into 'cut nothing'". That is this
defect, so the guard could not be evaluated without it. With the stock carried
correctly per copy, measured on the same fixture:

| boundary | stock as-is | stock carried per copy |
|---|---|---|
| part-level 200 × 120 | 0 of 8, total 0 | **8 of 8, total 93** |
| sheet-level, containing the part | 2 of 8, total 23 | **8 of 8, total 93** |

**The residual in carried counts is upstream, and the carry is not implicated.**
Per copy the carried figures read 6–7 where the source gives 8, which looked like
an unfaithful carry. It is not. The fixture's grid angles are
`(column*23 + row*11) % 90`, so one of the eight copies sits at **angle 0** — a
pure translation, the case where a correct carry and a wrong carry cannot differ:

    source           10 cutting moves entering the Boundary, 6 leaving
    angle-0 copy     10 entering, 6 leaving      <- the carry is faithful
    rotated copies   10 or 11 entering, 6 or 7 leaving

The path *below* the Boundary already differs on a rotated copy, so LeadInOut and
Dogbone recompute differently on a rotated part. That is a separate pre-existing
replay fidelity issue, and it is why the carried totals are 53 rather than 64.

**Two things nobody had looked at.**

*The committed fixture exercises none of this.* `replay-fixture-CAM-Nested.FCStd`
has 7 recipe steps and **zero** Boundary dressups. The replay is tuned against a
fixture that never reaches the defect.

*`verify_replay` cannot see it.* It walks `result.operations` — 40 base
operations — while `result.dressups` is 56, so the Boundary chains are in
neither. With all 8 Boundary entries producing **0 cutting moves** it returned
`ok=True, failures=0`; after the stock was carried, 8 of 8 and 93 moves, it
returned `ok=True, failures=0`. An identical verdict for "clips everything away"
and "clips correctly". `test_replay_dressups.py` asserts `has_cutting_motion` per
list entry, but that is a test, not the product's own check, which is why this
went unnoticed.

**And a leak found on the way.** Each Boundary's constructor calls
`PathStock.CreateFromBase`, which `apply_properties` then overwrites; nothing
removes the object. Measured on two sheets with one Boundary step: **3 orphaned
stocks unsplit, 17 split.** FreeCAD's `onDelete` would clean this up for a
dressup that is deleted, but nothing deletes these.

**Cost of replicating** (two sheets, one Boundary step, 8 copies each): wall clock
4 s → 5 s, document objects 2166 → 2306, dressups 70 → 112. Carrying a stock
across documents measures **1.91 ms** per copy, so ~30 ms, and the copy keeps
its `IsBoundary` flag and survives the source document being closed — which
matters because a replay already requires the source job to be open, since the
recipe is read from it.

**The rule the fix implements.** The Boundary sits in a *part's* operation stack,
so whatever solid its `Stock` names is that part's boundary, and replicating
operations to each matching nested part is what the workbench is for. The stock
therefore moves with the part, by the same rigid motion that moved the geometry
— `source_to_clone_placement`, which NEST-024 already uses for `StartPoint`.

This was first written as "everything expressed in the source geometry's frame
rides the geometry", which over-claims: it is justified for `StartPoint` because
the user types that coordinate while looking at the part, and it was *not*
established for the Boundary — which is why the original plan then wrongly claimed
no classification was needed. The argument that actually holds is the one above:
the tool's own data model settles it, not a geometric guess about whether the
boundary is sheet-fitted or part-fitted.

**Current state: reported, not fixed.** `unmapped_job_links` finds any job-local
link the replay carried across without remapping, and the sheet outcome carries
a warning naming the replayed dressup, the property and the object.
`test_replay_dressups.py` asserts the warning is emitted, which is the
mitigation — a Boundary dressup is **not safe to post from a replayed job**
until this is resolved, and the assertion is what stops the mitigation being
quietly deleted. `DRESSUP_JOB_LINKS` is the empty map that would hold the fix, and
`UNSPLITTABLE_DRESSUPS` is the band-aid that would go with it.

### Resolved — by dropping it

**Why not by fixing it.** Carrying the stock per copy works mechanically: measured
8 of 8 copies cutting where 0 of 8 cut against the source job's. What it does not
settle is **which region the user meant**, and the readings disagree:

| what `Stock` names | reading | correct per-copy answer |
|---|---|---|
| a region fitted to the part | "don't cut outside this" | move it with the part |
| the job's own stock | "don't run off the material" | leave it on the sheet — moving it happens to give the same answer only because a stock fitted to one part contains that part |
| `Inside=False` | "don't cut here" | replicated per part it excludes that part's own cuts; measured 1 cutting move per copy from a region that already clipped the source |

A source job machines one part, so nothing in the document distinguishes these.
Classifying by comparing the stock's footprint against the job's stock and
against the part was considered and rejected: it is a guess, and a wrong guess
yields a plausible toolpath that cuts the wrong thing — the failure this workbench
exists to prevent.

**Why dropping is an improvement, not a retreat.** The clipping is what was
silently wrong. Carried verbatim it produced **0 cutting moves where the
unclipped figure is 88**, with the sheet reporting `ok=True`. A job that cuts
nothing and says nothing is worse than a job that cuts everything and says why.
The replayed job is an ordinary FreeCAD job the user owns, so a Boundary fitted
to the sheet can be added and checked there.

**What changed.**

* `Path.Dressup.Boundary` moved from `DRESSUP_BUILDERS` to
  `DRESSUP_UNSUPPORTED`, with the reason above as the user-facing text.
* `UNSPLITTABLE_DRESSUPS` is now **empty**. Its only entry was Boundary, and its
  stated reason — splitting "multiplies a known-wrong reference" — was this defect.
  Neither arrangement worked: split gave 0 of 8 cutting, whole gave 2 of 8. The
  guard was holding up a broken behaviour rather than protecting a good one.
* The report is deduplicated per source dressup. It fired **once per copy**, so a
  split step over 8 parts printed the same warning 8 times; repeating a warning
  does not make it more likely to be read.
* The wording no longer says the operation "was replayed bare", which is untrue
  when the Boundary sat over LeadInOut and Dogbone and those *were* built.
* The constructor's `CreateFromBase` stock leak is **gone as a side effect**,
  since no Boundary is constructed. That leak was measured at 3 orphans unsplit
  and 17 split, on two sheets with one Boundary step.
* `DRESSUP_VIEWPROVIDERS` lost its Boundary entry — never built, so never needs a
  view. `TestDressupTablesAgree` caught the omission.

**`built_any` removed, and the first claim about it was wrong.** The guard that
wrote an entry only when a dressup had been built looked like a latent bug: a
step whose only dressup was dropped would build an operation that never reached
`Operations.Group`. Restoring the guard left the new test **green**, because
`order_operations` walks every base operation and substitutes
`entry_of.get(id(op), op)` — falling back to the bare operation — so the list is
rebuilt from `result.operations` regardless. The guard is harmless on the normal
path. The narrow exposure that is real is the **cancel path**, which skips
ordering, so the replay's own `set_operation_order` is final there. Untested: the
harness does not cancel mid-sheet.

**Coverage.** `tests/freecad_harness/test_replay_boundary.py` — 29 checks, gated
and wired into `run.sh`. It asserts classification (in `DRESSUP_UNSUPPORTED`, not
in `DRESSUP_BUILDERS`, not in `UNSPLITTABLE_DRESSUPS`), that no Boundary object is
created, that the report appears once per dressup and says what is missing, that
LeadInOut and Dogbone below it survive, that **no copy has less motion than the
source's unclipped stack**, and that a Boundary over a bare Profile leaves a
listed, cutting operation.

Injection-checked: putting Boundary back in `DRESSUP_BUILDERS` fails 11 of its
checks and reproduces the original symptom — "the whole step produced no cutting
motion".

Three of that file's own failures were my bugs, worth recording because two are
the same trap the repo has hit before: it filters chains by label prefix, because
`chain_kinds` returns proxy *class* names and DogboneII's is `Proxy`; it matches
targets by `nested_label_of`, because an operation's `Base` points at a flattened
part named `CAMPart_57`, not at `nested_BarePlate_9`; and its fixture creates
three Profiles, so `"Profile001" if index else ""` collides.

**`test_replay_dressups.py` and `test_replay_startpoint.py` both asserted the old
contract** and were updated. The startpoint file's unsplit-step check needed more
than a mechanical edit: `UNSPLITTABLE_DRESSUPS` is empty, so it now puts Boundary
back for the duration of that one replay and restores it in a `finally`. That is
deliberate — `split_is_safe` and the stranded-start-point report are the only code
that says "I could not carry this", and deleting the check because nothing
reaches it today would leave them untested at the moment someone is most likely
to need them.

**Also worth knowing, found while setting the test up.** Boundary clips with
`edge.common(shape)`, and an edge commoned with a planar *face* in 3D returns
nothing: 0 cutting moves against a 400x200 face, 11 against a 400x200 box. The
property is documented as "Solid object", so a face is user error, but it fails
by producing an empty path rather than an error. And `Inside=True` against the
constructor's own `CreateFromBase` stock clips the contour away entirely — not
because the stock is barely larger than the part, as recorded above, but because
it is barely larger than the part *plus twice the tool radius*, which the
compensation in the toolpath adds. Measured in the table further up.

## NEST-026 — `Array.Centre` and `Tags.Positions` are points on the part, carried verbatim

`status: open` · `severity: medium (unmeasured)` · `area: CAM replay`

Raised while fixing NEST-009, by asking the question NEST-009's fix implies:
*which other values mean a place on the part?* `capture_properties` takes every
`App::Property*` verbatim, and the only part-relative transformation in the
replay is `GEOMETRY_FRAME_POINTS`, which is applied to **operations only** — no
dressup property is transformed at all.

| property | type | FreeCAD's own description |
|---|---|---|
| `Path.Dressup.Array.Centre` | `App::PropertyVector` | "The centre of rotation in polar pattern" |
| `Path.Dressup.Tags.Positions` | `App::PropertyVectorList` | "Locations of inserted holding tags" |

Both are coordinates picked on the part, and both are copied to copies that have
been moved and rotated. `Array` is the same failure shape as NEST-009's
`Boundary.Stock` one level along.

**Neither is measured.** Neither appears in the committed fixture, which is the
same blind spot Boundary had — 7 recipe steps, no Boundary dressups, and by the
same token no Array or Tags. So this is a list of candidates, not a list of
defects, and it should not be read as a claim that either is broken.

`Tags` is worth checking first: it is the one of the two the harness already
touches, so it is the cheaper of the pair to exercise. Note what
`UNSPLITTABLE_DRESSUPS` says about it — "LeadInOut, Dogbone and Tags all split
correctly and are verified to" — which is a claim about *replication*, not about
whether the tag lands on the part. Those are different claims and only the first
is tested.

**Deliberately not bundled with NEST-009.** A verified fix and an unverified
third change in one commit are both harder to review and harder to revert, and
nothing about NEST-009's fix depends on this being settled.

**Fix direction.** The same one NEST-009 establishes — a value the user picks on
the part moves with the part, by the rigid motion that moved the geometry. The
mechanism exists (`source_to_clone_placement`, and the per-unit `remap` seam in
`apply_properties`); what is needed first is a measurement showing the defect is
real, and a fixture that reaches it.

---

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

`status: resolved` · `severity: high (as reported) → low (in fact)` · `area: test fixture`

**RESOLVED, and the diagnosis below was wrong.** Not a geometry defect and not a
Z-frame defect: **the fixture's source job and its layout nested two different
solids.** The evidence is under *Resolved*; the original report is kept as it was
written, because the reason it went wrong is the useful part.

Found by the per-part split, and **not caused by it**. The split turned an
invisible defect into a visible one, which is how it was found.

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
root-causing the projection, which needs a fixture whose top face is *not* on the
stock top, to tell "Z frame" apart from "face on the stock top".

### Resolved — the fixture, not the replay

**`_make_bracket` cut a hole; `_build_layout` rebuilt the part without one.** So
the source job profiled a 7-face holed bracket while the layout nested a 6-face
holeless one, and the sub-element `Face3` — the plate top on the source — resolved
on the nested part to a **side face**:

| | faces | volume | `Face3` is | wires | normal |
|---|---:|---:|---|---:|---|
| source job bracket | 7 | 5830.4 | plate top, area 971.7 | 2 | (0, 0, 1) |
| nested part | 6 | 6000.0 | **side face**, area 240.0 | 1 | (0, −1, 0) |

The replay profiled that side face as though it were the outline, and produced a
14-command path on a straight line. Measured, same job, only the layout's solid
differing:

| nested solid | commands | uncovered | path bounds |
|---|---:|---:|---|
| different (as committed) | 14 | 1 | (20.0, 25.0, 60.0, **25.0**) |
| same holed solid | 17 | 0 | (17.5, 25.0, 62.5, 55.0) |

**The Z frame is measured and not involved.** Varying it alone changes nothing:
lifting the part 2 mm clear of the stock top, setting explicit
`StartDepth`/`FinalDepth`, turning compensation off, and profiling the bottom
face instead all produce a correct outline. The original note that the
"force-the-source-into-the-same-Z-frame probe did not complete" was pointing away
from the cause entirely.

**The replay behaved correctly and could not have done otherwise.**
`check_subnames_against_clones` reports `missing=set(), detail={'Face3': 'ok'}` —
the name resolves, to the wrong face. It detects a sub-element that is *absent*,
not one that has moved. The replay's stated assumption is in `OperationRecipe`:

> *"a nested copy keeps identical topology and therefore identical sub-element
> numbering"*

which the fixture violated.

**What changed.** The geometry is now one definition, `_bracket_shape(thickness)`,
used by `_make_bracket` and by every `_build_layout` caller. `_build_layout` takes
the shape as a **required argument with no default**, so the two cannot drift
silently again -- all three call sites had reproduced the mismatch independently,
and none of them had any reason to notice.

`KNOWN_DEGENERATE` and its by-name exception are **deleted**. The coverage check is
unexcepted, which is what its own comment said it wanted and could not then claim.
The hole is kept in the fixture deliberately: it is what makes `Face3` the top on
one solid and a side face on the other, so the fixture still has teeth.

**Coverage.** 236 checks, up from 230, `0 failure(s)`, gated. Three new checks
assert the nested part is the same solid the source job profiles -- volume, face
count and edge count, because it is the *topology* that moves `Face3`, not the
volume alone.

**Injection-checked.** Making the layout nest a different solid fails **3** checks:
the fixture guard names the mismatch, and the coverage check fires on both sheets
with nothing excepted.

One injection attempt is worth recording because it correctly *failed* to bite:
removing the hole from **both** solids left the two consistent, and a consistent
fixture is harmless. That is the difference between testing the bug and testing
the absence of it, and it is why the injection has to introduce the
*inconsistency* rather than any change at all.

**The real defect this exposed is not fixed here, and is tracked as NEST-027.** A
sub-element name that resolves to a *different* face is invisible to every check
in the replay, and the consequence is a job that cuts the wrong feature -- the
NEST-015 shape. Its reach outside a fixture is unmeasured.


## NEST-028 — centring silently does nothing for a part that arrives pre-positioned

`status: open` · `severity: medium (unmeasured)` · `area: geometry`

Found while fixing NEST-007, and deliberately not fixed there: it would **move
parts**, which is a behaviour change, not the fidelity fix that was in hand.

`ShapePreparer._center_3d_shape` builds a matrix of *translate by −centroid* and
multiplies it by the master object's own `Placement`. It then applies that matrix
to a shape **which already carries that placement**:

    combined = translate(-centroid) * Placement
    result   = shape.transformShape(combined)     # shape.Placement IS `Placement`

The placement is therefore applied twice — once inside `combined`, once factored
out of the shape — and the two cancel. Measured with a placement of `(5, 7, 11)`
and yaw 30°:

    input bounding box  (-15.722, 13.428, 20.000)..(6.598, 32.088, 50.000)
    output bounding box (-15.722, 13.428, 20.000)..(6.598, 32.088, 50.000)

Identical. The part does not move, so it is never centred. With an **identity**
placement — which is what the committed fixture and the harness fixtures all have
— the multiply is skipped, `combined` is a pure translation, and the function works
correctly. That is why it has gone unnoticed.

**Not fixed, because the right answer is a judgement and not an obvious one.**
Dropping the `Placement` multiply would make the centring work, but the existing
multiply may be *deliberate* for parts that carry a placement: it may be there to
bake a rotation into the geometry so the master shape starts axis-aligned for the
2D nester. Nothing in the function or its docstring says which. The first step is
therefore to find out whether anything depends on the current behaviour, and that
is a question about the nester rather than about this function.

**Reach is unmeasured.** It needs establishing whether real parts arrive with a
non-identity placement — anything positioned in the tree by the user rather than
created from a file would. If they do not, this is latent; if they do, every such
part is nested off-centre.

**Fix direction.** Determine whether the `Placement` multiply is load-bearing. If
not, drop it and the centring works. If it is, the function needs to separate the
two intents — bake the rotation, translate the geometry — rather than composing
them into a single matrix that then gets applied twice.

---

## NEST-027 — a sub-element name that resolves to a different face is invisible

`status: resolved` · `severity: medium` · `area: CAM replay`

> **Severity was recorded as "unknown (unmeasured)" and is now `medium`.** The
> mechanism is a job that looks right and cuts the wrong feature, which is the
> NEST-015 shape, but no real job was ever shown doing it -- see "Reach" below,
> which is measured now and came out fixture-only. Medium on the strength of the
> consequence if it does happen, not on a demonstrated occurrence.

Found while resolving NEST-014, which turned out to be a fixture bug. **The
fixture bug is fixed; this is the thing it was accidentally uncovering, and it is
the larger of the two.**

An operation's `Base` names sub-elements by name — `Face3` — and the replay
resolves them against each nested copy. `check_subnames_against_clones` reports
`missing=set(), detail={'Face3': 'ok'}` when the nested part has *fewer* faces
than the source, because `Face3` still exists. Measured on NEST-014's mismatch:

| | `Face3` resolves to | wires | normal |
|---|---|---:|---|
| source job (holed bracket) | the plate top, area 971.7 | 2 | (0, 0, 1) |
| nested part (holeless box) | a side face, area 240.0 | 1 | (0, −1, 0) |

The replay then profiles the side face as though it were the outline. Nothing
downstream sees it: the sub-element check says `ok`, and the only symptom was a
coverage warning about a path that collapsed to a line.

**This is the NEST-015 shape** — a job that looks right and cuts the wrong feature
— and the replay states the assumption it rests on, in `OperationRecipe`:

> *"a nested copy keeps identical topology and therefore identical sub-element
> numbering"*

**Reach is unmeasured, and that is the thing to establish first.** Known to
happen when the nested geometry differs from the CAM source. Not established:
whether real nesting can produce that, or whether it is confined to fixtures. The
committed 98-operation fixture passes 98 of 98, so if it happens in practice it
happens quietly. Also unmeasured: how much legitimate variation there is between
a source part and its nested copies — geometry that *should* differ, a part edited
after nesting, B-rep re-tessellation — because that number sets the tolerance any
check can carry.

### Resolved

`check_subnames_against_clones` now compares each captured sub-element's
**geometry** on the source against the same name on every nested copy, and
leaves out the copies where it means something else. The comparison is on
area, wire and edge counts for a face; length and closedness for an edge. Every
one of those is placement-independent, which is the whole requirement: a centre
of mass, a vertex position or a face normal are all measured from a frame that
differs between the two shapes.

**Reach, measured: the committed fixture is not affected, and that is the
answer to the question this entry said to settle first.** Its 7 base operations
select **56 distinct sub-names and every one is an `Edge`** -- zero `Face` names
-- so it cannot exercise a face check at all. Its edge lengths agree with the
source to a relative **3.2e-13** even in the stale fixture of
[NEST-029](#nest-029), which is why the length tolerance can be loose and the
area tolerance cannot. It passes 98 of 98 and still passes with this change.

**Legitimate variation, measured, on geometry from current code.** Across all
three part types of the fixture, every face and every edge, at four nest angles:

| | worst difference | share of the tolerance |
|---|---:|---:|
| face area | **4.263e-14** mm2 | **4.3e-05** of 1e-9 |
| edge length | **0.000e+00** mm | 0 of 1e-6 |

Roughly 23000x of headroom on area. The tolerances are relative, so the test does
not depend on the part's size -- 1e-9 is the same tolerance on an 886 mm2 face as
on an 8 mm2 one.

**The plane normal in the original fix direction is not usable, and was dropped
rather than quietly included.** Measured **90 degrees** and **180 degrees**
disagreements between faces that are provably the same face: areas equal to
1.4e-14, every count equal, and *every* `Placement` rotation identity, so the
relative rotation is provably zero and a rigid translation cannot change a
normal. The cause is the surface's **parameterisation origin being rotated** --
`BottomStrap` Face11's u at centre-of-mass is 0.000 in the source and
**4.712 rad** in the copy; `TopStrap` Face11 is pi apart. `normalAt(u, v)`
depends on where the parameter lands, which is arbitrary, so it is not an
invariant. Worth recording because the error is not subtle and both directions
of the obvious placement-based frame give the same wrong answer.

**The surface type name is not used either, and this one was a judgement call.**
It is the most direct evidence available -- `Cylinder` against `Plane` -- and it
reports **0 mismatches** on current code, which is exactly what makes it
tempting. But on the [NEST-029](#nest-029) fixture it reports **every** face and
**every** edge different, because they are all `BSplineSurface` and
`BSplineCurve`. That is a true statement about those copies and a useless one to
act on, so the numeric measures are compared instead.

**What the check catches, measured.** NEST-014's geometry, a holed plate against
a holeless box, both names resolving:

| | area | wires | edges |
|---|---:|---:|---:|
| `Face2` | 200.0000 vs 125.0000 | 1 vs 1 | 4 vs 4 |
| `Face3` | 886.9027 vs 200.0000 | 2 vs 1 | 5 vs 4 |
| `Face5` | 886.9027 vs 1000.0000 | 2 vs 1 | 5 vs 4 |

`Face2` is the one that matters and the one that is easy to leave untested: same
wire count, same edge count, **area the only discriminator**. That is precisely
the failure NEST-014 describes -- a side face profiled as though it were the
outline -- and it is invisible to counts alone.

**One copy is left out, not the whole step.** A Profile selecting `Face3` across
three copies, one of them a holeless impostor, builds **2** operations and
reports:

> `Operation 'Profile' selects Face3, which addresses a different feature on
> part_Bracket_2 than it does on the source. Not replayed there, because it would
> cut a different feature than intended.`

One bad part in a 23-part nest should not cost the other 22 their recipe.

**The message names the part the user knows, not the replay's own number.** The
replay's geometry is `CAMPart_57`; the part they placed is `part_Bracket_2`,
reached through `SourceObject`. A new `display_label_of` does that, preferring
`SourceObject`, then `nested_label_of`, then the Model entry's own label. The
per-name roll-up in `subname_detail` deliberately keeps **counts only** -- one
name is checked against different source geometry for different operations, so a
label there could name the wrong copy.

**Injection-verified, six ways.** Removing the source geometry, reverting to
existence-only, zeroing the face area, zeroing the edge length, reporting without
dropping the copy, and setting the area tolerance to 0: each fails the suite, and
the last one is the useful one -- it shows the 4.263e-14 drift is real, so the
1e-9 tolerance is not a round number picked for looks.

**Two of those injections found real holes in the tests rather than in the
product,** which is the reason they were run. Zeroing the face area left the file
**green**: `Face3` differed in wire count too and carried the check on its own,
so nothing was testing area. And choosing the length-only edge case *through*
`subelement_signature` meant that zeroing the length emptied the candidate list
and the failure read "no edge differs in length" -- true, and silent about the
defect. Both fixtures are now chosen from the geometry directly.

**`OperationRecipe`'s docstring no longer asserts the assumption as a fact.** It
said a nested copy "keeps identical topology and therefore identical
sub-element numbering". That "therefore" is a claim about geometry, not about
names, and it is now checked rather than relied on.

Tests: 63 checks in the new gated `test_replay_subnames.py`, and 11 new pytest
cases. Gate: 13 freecadcmd suites, 1240 checks, 0 failures; 554 pytest.

---

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

---

## NEST-024 — the replay did not carry a Profile's StartPoint onto the nested copy

`status: resolved` · `severity: medium` · `area: CAM replay`

A user sets `StartPoint` on a Profile in the CAM workbench, before nesting runs —
that is the premise `cam_replay` exists on (module docstring). The nesting
workbench then moves the part. The point that says *where on the contour the feed
starts* does not move with it.

### The break, in four links

| link | state |
|---|---|
| `Path/Op/Gui/PathShape.py:113-125` — `StartPoint`, `App::PropertyVectorDistance`, absolute | exists on every operation (`Path/Op/Base.py:412`, via the `opFeatures` default at `:625-633`) |
| `Path/Op/Area.py:282-283` and `:378-379` — `pathParams["start"] = obj.StartPoint` | honoured exactly |
| `cam_replay.capture_properties` (`:863`) | captures it verbatim — it is an `App::Property*` and not in `NON_REPLAYABLE_PROPERTIES` (`:799`, which is only `Base`, `Path`, `Proxy`, `ExpressionEngine`, `Label`, `Label2`, `Visibility`) |
| `cam_replay.apply_properties` (`:2583-2593`) | assigns it verbatim; the only `remap` built is `{"ToolController": …}` (`:2733-2736`) |

Meanwhile the geometry it names **is** moved: `flatten_container` (`:1240`) applies
`combined_placement(container, part)` (`:1178`, position *and* rotation), then a Z
normalisation, then `translate_to_sheet_local` (`:446`) shifts by the sheet origin.

Structurally this is NEST-009 — a link carried across a job boundary without
remapping — except that `Stock` at least got a warning, and this gets nothing.

### Measured

`tests/freecad_harness/test_replay_startpoint.py`, 27 checks. Source plate at
`(35, -12)` yaw 15, top face Z 0, start point a vertex of the top face's outer
wire. Two nested copies, so both a pure translation and a rotation are covered.

| | copy 1 (container `(140, 110)` yaw 0, child yaw 12) | copy 2 (container `(430, 260)` yaw 37) |
|---|---:|---:|
| source `StartPoint` | `(0.8458, -0.4461, 0)` | same |
| distance from its **own** contour | 0.000000 mm | 0.000000 mm |
| where it should land | `(106.497, 123.326, …)` | `(394.005, 257.918, …)` |
| distance from **that** contour | 0.000000 mm | 0.000000 mm |
| where it actually lands | `(0.8458, -0.4461, 0)` | `(0.8458, -0.4461, 0)` |
| distance from **that** contour | **141.963 mm** | **470.453 mm** |
| error | **162.7319 mm** | **470.4529 mm** |

`UseStartPoint` survives correctly — it is a bool, and a bool needs no frame. Only
the point is lost.

### Two symptoms, and only one is reliably visible

**The rapid is always wrong.** The stale coordinate goes into the toolpath
verbatim, ahead of the real positioning move. Stock measures X 0..700, Y 0..400,
so `Y -0.446` is **off the sheet**:
```
replayed copy 1                  after the transform is applied
  G0 [ Z:5 ]                       G0 [ Z:5 ]
  G0 [ X:0.846 Y:-0.446 ]  <- off  G0 [ X:106.497 Y:123.326 ]
  G0 [ X:114.814 Y:84.200 ]        G0 [ Z:3 ]
  G0 [ X:114.814 Y:84.2 Z:3 ]      G1 [ X:106.497 Y:123.326 … ]
  G1 [ X:114.814 Y:84.2 … ]        G1 [ … ]
  …                                …
                                 12 commands
```

The tool is told to rapid somewhere with no material under it, then told to rapid
again to where it was always going. 13 commands become 12.

**The contour start vertex is wrong only sometimes.** The stale point selects the
*nearest point on the wire*, so on a symmetric outline it can hit the intended
vertex by accident:

```
copy 1  began (114.814, 84.200), should be (106.497, 123.326)   40.0000 mm
copy 2  began (394.005, 257.918), should be (394.005, 257.918)    0.0000 mm  <- coincidence
```

Copy 2 is a rectangle, and a rectangle's four corners are interchangeable under
its own symmetry. **This is why the harness asserts on the property and reports
the toolpath**: a test keyed on the toolpath would pass on copy 2 for the wrong
reason, and would be right by luck on any symmetric part.

### Nothing warns

`verify_replay` on the same run: 2 operations checked, 0 failures, 2 warnings,
`ok=True`. Both warnings are the stock Z frame. Neither mentions the start point,
and the replay's own warning list is empty.

### The fix, and what is already established

The rigid motion from the source job's geometry frame to a nested copy's is

    clone.Shape.Placement * inverse(source_entry.Shape.Placement)

because `flatten_container` folds the container placement, the part's own
placement, the Z normalisation and the sheet-origin shift into the flattened
object's placement, and a nested copy is the same underlying geometry rigidly
moved (module docstring constraints 5 and 6).

**`Shape.Placement`, not `Placement`.** `PathJob.Create` puts a
`draftobjects.clone.Clone` in the Model, and Draft's Clone restores
`obj.Placement` *after* assigning a Shape that already carries the original's
placement (`draftobjects/clone.py:122-148`), so the two are not guaranteed to
agree.

Measured, not assumed:

* every factor of that transform is non-identity in the harness fixture, so it
  cannot be right by accident — a transform missing the source-side inverse fails
  six checks;
* applying it to the replayed operation puts the start point **0.000000 mm** from
  the part and makes the toolpath begin exactly where the source's did, with the
  off-sheet rapid gone and one command fewer.

**Measured on an uncompensated profile, and the choice matters.** With
`UseComp = False` the source operation's first cutting move *is* the StartPoint,
so "the transform is right" is an exact statement. With `UseComp = True` the cut
runs on the offset wire and the first cutting move is the nearest point on the
**offset**, not the StartPoint:

    UseComp=False   first cutting move (0.8458, -0.4461)  == StartPoint
    UseComp=True    first cutting move (-1.5690, -1.0931)  1.91 mm off it

1.91 mm is the default 5 mm endmill's radius projected onto the corner. The
*selection* is identical either way — both arms replay with the same defect and
the same ~470 mm error — but a fix verified against a compensated arm has to
compare against the offset contour, not the raw one.

An earlier draft of this entry also said "NEST-014 also lives on that arm: a
compensated profile of a face exactly on the stock top collapses to a line."
**That was wrong**, and investigating it is how NEST-014 was resolved: the
collapse had nothing to do with compensation or with the stock top. It was the
flatten fixture nesting a different solid from the one the source job profiled,
so the replay profiled a side face instead of the plate top. See NEST-014.

### Resolved

`status: resolved` · fixed in `cam_replay.py`: `GEOMETRY_FRAME_POINTS`,
`GEOMETRY_FRAME_POINT_FLAGS`, `geometry_frame_point_in_use`,
`source_to_clone_placement`, `carried_geometry_frame_points`,
`unmapped_geometry_frame_points`, `point_outside_targets`, plus pass one of
`replay_recipe` and one warning in `verify_replay`. Gated by
`tests/freecad_harness/test_replay_startpoint.py`, 58 checks.

Each of the six decisions above, and how it was settled:

**1. Where the transform comes from.** `base_value` now holds
`(clone, subs, geometry)` triples instead of `(clone, subs)` pairs, so each split
copy knows the one source object it came from.

That change broke the whole-step path, which assigned `base_value` straight to
`Base` and then failed with

    Could not set Base on 'Profile_replay': Expects sequence of items of type
    DocObj, (DocObj,SubName), or (DocObj, (SubName,...))

The whole unit rebuilds its `Base` as pairs. Caught by the new harness's unsplit
check; without it this would have shipped as "no Boundary step produces any
operations".

**2. `remap` is per unit now.** The tool controller is still hoisted and shared
across a step's copies -- it does not depend on which part a copy targets. The
point remap is added per unit into a copy of that dict.

**3. No single transform -> reported, not guessed.** `unmapped_geometry_frame_points`
names the points a step reads; when no unit resolves to one source object the
replay warns, once per step:

    Operation 'Profile' reads StartPoint, but it was left whole over 3 part(s),
    so no single transform applies and was left in the source job's frame.
    Check the toolpath before running it. See issues.md NEST-024.

The value is left verbatim, as NEST-009 leaves `Stock`. `verify_replay` says so
independently, so the report does not depend on anyone reading it.

**4. Gated on CAM's own flag, read not inferred.** `geometry_frame_point_in_use`
asks `UseStartPoint` where the operation has it and `SortingMode` where it does
not, because the flags are not uniform. Measured by creating one operation of
each kind:

    op           StartPoint  UseStartPoint  EndPoint  UseEndPoint  SortingMode
    Profile      yes         False           --        --            Automatic
    PocketShape  yes         False           --        --            Automatic
    Slot         yes         False           --        --            --
    Waterline    yes         False           --        --            --
    Surface      yes         False           --        --            --
    Engrave      yes         -- (absent)     yes      False         Automatic
    Drilling     yes         -- (absent)     yes      False         Automatic
    Helix        yes         -- (absent)     yes      False         Automatic
    Adaptive     -- (absent) --              --       --            --

**Without the gate this would be 97 false alarms** on the committed fixture, which
is 98 operations of which exactly one has `UseStartPoint` True.

`Helix` carries `StartPoint`/`EndPoint` too and they are *not* part-frame values
-- they place the helix in machine coordinates, and a Helix belongs to the tool,
not to a part. **No per-operation exception is needed**, because a Helix names no
part geometry, so no unit ever resolves it to a source object and the transform is
never formed. `Surface` is left to the same rule rather than special-cased.

**5. Z: XY rides the rigid motion, Z does not.** I first implemented a plain rigid
motion on the whole vector, on the grounds that a start point is a point in the
geometry's frame so the whole vector should move. **That was wrong, and the
committed fixture said so.**

A `StartPoint`'s Z is a *derived* value, not an authored coordinate:
`CommandSetStartPoint` writes `obj.StartPoint.z = obj.ClearanceHeight.Value`
(`Path/Op/Gui/Base.py:1716-1719`). On the fixture, `Profile005` has
`StartPoint.z = 5.000` and `ClearanceHeight = 5.000` -- the same number. So
riding Z moved a derived value into a frame it was never authored in:

    Z ridden rigidly   StartPoint.z 5.000 -> 6.000, Z levels [0, 3, 5, **6**]
    Z from ClearanceHeight             StartPoint.z = 5.000, Z levels [0, 3, 5]

The 6.000 is a millimetre above the operation's own clearance height and it
**reaches the toolpath**. Z is emitted whenever it exceeds the operation's
heights -- on that same operation, z of 0, 3 and 5 leave the levels at `[0, 3, 5]`
while z of 6 and z of 40 each add one.

And Z has no say in where the cut begins, which is the only thing the point is
for: same operation, same XY, z swept 0 -> 40, the first cutting move is
`(42.6, 166.408)` every time. `Path.fromShapes` uses the XY to pick the point on
the wire.

So the Z is taken from the target operation's own `ClearanceHeight` -- the rule
the GUI itself writes, so a job set up in the CAM workbench round-trips unchanged
-- and the transform's Z is the fallback for an operation with no
`ClearanceHeight`. The source/replay Z-frame disagreement is reported by
`compare_stock_frames`, which is where it belongs rather than patched here.

**6. The verification check is a bounding box, not a distance.** Measured on the
committed fixture's 48 parts (1567 edges, 613 faces):

    Shape.distToShape   5.59 ms per part on the contour, 9.62 ms off it
                        -> 548-943 ms across the fixture's 98 operations
    bounding box        0.31 ms per part -> 31 ms across the same 98

The precise version costs **5-9% of a 10.5 s replay, on every replay**, to answer
a question a box answers exactly as well: on that fixture a carried point is
inside 48 of 48 boxes and a stale one inside 0 of 48. Reuses
`COVERAGE_MARGIN_MM = 2.0` rather than inventing a threshold, for the same reason
`uncovered_targets` uses it. It warns rather than fails -- the cut still happens,
from the wrong vertex, and only the user can say whether that matters for their
part.

### Effect on the committed fixture, measured

`Profile005` is the one operation there with a real user-set start point, and it
**was** being carried wrongly. Before and after, same document, same replay:

| | copy 1 | copy 2 |
|---|---|---|
| `StartPoint` before | `(164.408, 209.573, 5.0)` | `(164.408, 209.573, 5.0)` |
| `StartPoint` after | `(44.449, 166.408, 5.0)` | `(555.551, 52.592, 5.0)` |
| first cutting move before | `(164.408, 183.9)` | `(386.116, 184.722)` |
| first cutting move after | `(42.6, 166.408)` | `(557.4, 52.592)` |
| path bounds max-Y before | **209.573** | **209.573** |
| path bounds max-Y after | 186.369 | 186.368 |

Both copies previously received **the same coordinate**, 511 mm from one of them,
and both toolpaths extended to Y 209.573 -- the stale source coordinate leaking
into the G-code, the off-material rapid this entry predicted. Neither copy's part
reaches Y 210.

The order test's 432 checks did not move. That is worth stating rather than
leaving as luck: nothing it asserts depends on where a contour begins, and it is
`test_replay_startpoint` that now covers this.

### Open: Drilling and Engrave

`Path/Op/CircularHoleBase.py:249-252` uses `StartPoint`/`EndPoint` as absolute XY
seeds for the hole TSP sort. Both are now in `GEOMETRY_FRAME_POINTS` and gated on
`SortingMode == "Automatic"`, which is how they are read -- so **the exposure is
closed by the same change**, and a Drilling operation with a user-set seed now gets
one moved onto each nested copy.

**The effect on the emitted hole order is still not established.** The fixture used
to look for it found only one of its nine holes (`CircularHoleBase` warns "Hole
diameter may be inaccurate due to tessellation on face" and drills one), so there
was no order to compare. A fixture that detects all its holes is still needed
before anyone claims this helped or hurt. Engrave carries an `EndPoint` too, gated
on `UseEndPoint`, unmeasured for the same reason.

### Reproducing

    FREECAD=/home/james/freecad_env/usr/bin/freecadcmd
    $FREECAD tests/freecad_harness/test_replay_startpoint.py

    # and confirm the assertions bite, all three ways
    # A: remove the carrying from replay_recipe
    #    -> 58 checks, 30 failures, exit 1
    # B: make the Z rule `if False` so Z rides the rigid motion
    #    -> the ClearanceHeight assertion fires on both copies, exit 1
    # C: START_POINT_IS_COPIED_VERBATIM = True in the harness
    #    -> asserts the pre-fix behaviour instead, exit 0

---

## NEST-029 — the committed nested fixture predates NEST-007, so the gate cannot see that fix

`status: resolved` · `severity: medium (measured)` · `area: test fixture`

Found while measuring [NEST-027](#nest-027)'s prerequisite, and filed separately
on purpose: rebuilding the fixture shifts numbers in four gated suites, and doing
that inside the same change as the NEST-027 check would make the two impossible
to review apart.

`replay-fixture-CAM-Nested.FCStd` is a committed `.FCStd`, so its nested parts
were built **once, by whatever code existed then**, and every replay suite reads
them rather than re-nesting. Measured: all **48** of its `part_*` objects are
`BSplineSurface`, while the source job's `Model-*` entries are still
`Cylinder`/`Plane`.

| | source | nested part |
|---|---|---|
| faces, edge and vertex counts | — | **identical** |
| max \|Δface area\| | — | **6.1054e-01** (BottomStrap), **3.8615e-01** (TopStrap), **3.1082e+01** (SimpleSpacer) mm² |
| max relative \|Δedge length\| | — | 2.1e-14, 2.4e-14, **3.2e-13** |
| surface type mismatches | — | **every face** |
| curve type mismatches | — | **every edge** |

So **NEST-007's fix is invisible to the gate.** The 0.2684% face-area error that
commit `43003e1` removed is still sitting in the fixture, and a regression of that
fix would leave all four replay suites green. Two of them compare nested geometry
against a source and would have caught it; neither does.

**This is not cosmetic, and it is the reason the NEST-027 tolerances are not the
same for area and length.** A face check with a tolerance loose enough to accept
this fixture would have to allow ~5e-3 relative -- which is far looser than the
0.77 relative difference it exists to catch, so it would be useless. Edge length
was measured to be insensitive to the same re-fitting, which is why its
tolerance can be loose. NEST-027 therefore **does not** accommodate the stale
fixture, and nothing trips over that today only because the fixture selects no
face names.

**Fixed: the fixture is re-nested with current code and committed.** Input is
`replay-fixture-CAM.FCStd` (the file that has the CAM on the source bodies, so its
structure matches #3's); `replay-fixture.FCStd` alone has 3 source objects and no
job, so it cannot drive a replay on its own. Measured before and after:

| | committed | re-nested |
|---|---|---|
| nested part face surfaces | **613 `BSplineSurface`**, 0 analytic | **310 `Plane` + 303 `Cylinder`** |
| overlapping material | none | none (see below) |
| sheets | 1 | 1 |
| parts | 48 | 48 |
| hole nestings | 15 | 14 |

So NEST-007's centring fix is now *in* the fixture, and a regression of `43003e1`
would stop being invisible. **It is not a reproduction of the original nest** -- see
[NEST-031](#nest-031), and the commit message says so explicitly. It is a fresh
nest from the same source document with the same recorded parameters.

The tempting cheaper move, a check that fails when a nested part has non-analytic
surfaces, does not avoid this: it fails against the fixture as committed, so the
fixture still had to be rebuilt.

**A second, independent piece of the same evidence.** Measured on real geometry
via `flatten_sheet` -- not on reconstructed outlines -- all **15** of the
fixture's hole nestings have a part sitting **0.0000 mm** from the wall of the
opening it occupies. Nesting the same three part types through current code puts
nested parts **2.0000 mm** off that wall at the same `PartSpacing = 4.0`, and
the user reports correct stand-off nesting in the GUI. So the fixture's hole
nestings were produced by code that no longer exists, exactly like its B-splines.

That second figure is **not** claimed as exact: it comes from a `dist == 0` that
cannot distinguish *touching* from *overlapping*. What is established is the
direction -- the fixture is flush where current code is not -- which is all the
staleness argument needs. See NEST-030's "Not covered" section.

**Also measured:** whether a stale fixture is a general hazard here. It was
committed once and read by four gated suites; nothing checked that a fixture's
geometry matched what current code produces. The answer is yes, twice over -- the
B-splines and the flush hole nestings above.

### What re-nesting measured, and two things it caught

**Reproducibility: no, confirmed.** Two runs of the identical job, no seed set:

| | run 1 | run 2 |
|---|---|---|
| sheets | 2 | 2 |
| hole nestings | **1** | **5** |
| tightest gap | 3.653626 mm | 3.585688 mm |

**0 of 48** nested containers had identical `Placement` in any pair of runs. This is
NEST-031's prediction, reached independently.

**A real overlap, and it was mine to introduce.** The first re-nests produced
`part_SimpleSpacer_1` sharing **2031.32 mm^3** of actual solid with
`part_BottomStrap_2`, at 0.000000 mm. The cause is **not** the nester:
`BottomStrap` arrives at `base=(-42, 0, 0)`, and
`NestingController._prepare_source_parts` (`nesting_controller.py:323`) zeroes every
source placement before nesting and restores it after. A re-nest that skips that
step overlaps material. Zeroing placements -- as the panel does -- removed the
overlap from every subsequent run. **The coordinator does not require zeroed
placements; it happened to matter.** Recorded because the headless route to a nest
is now known, and it has a step in it that is easy to miss.

**A second measurement error, caught.** Measuring `part_*` objects reported those
same two solids as 11.016441 mm apart with zero shared volume -- reassuring, and
wrong. `part_*` carries **local** geometry inside its `nested_*` container; the
placed solids are what `flatten_sheet` hands the replay. Every geometry claim above
was re-measured on the placed parts. This is the third time this trap has produced a
confident wrong answer, and the reason `volume2.py` exists as a separate probe.

**`search_direction` is not `None`.** The first re-nests passed
`search_direction=None`, which is what the *random* checkbox produces
(`nesting_controller.py:1296`). `NestingDirection=90` with that box off converts the
dial to a bearing vector (`:1298-1300`). Correcting it took hole nestings from
1 and 5 to **14**, against the committed 15.

### Pinned numbers vs properties: measured, and the suites held

The four gated suites that read the fixture, run against a re-nest with the
parameters corrected:

| suite | on committed | on re-nest | failures |
|---|---|---|---|
| `test_replay_order` | 432 checks | **431** | 0 |
| `test_replay_dressups` | 181 | 181 | 0 |
| `test_replay_startpoint` | 59 | 59 | 0 |
| `test_tool_clearance` | 25 | 25 | 0 |

**All four pass unmodified.** `test_replay_order` yields one *fewer* check: it
iterates per hole nesting somewhere, and the re-nest has 14 rather than 15. Every
assertion that exists still holds.

So the answer to the question this was blocking: **the suites assert invariants that
survive a re-draw, not a single random draw's coordinates.** Rewriting them into
property assertions was considered and is *not* recommended -- it would be churn
without a measured gain, and the evidence says little would be lost.

For contrast, a re-nest with the wrong `search_direction` produced **2 failures in
`test_replay_order` and 3 in `test_tool_clearance`**. The suites do catch a bad nest.

### The fixture now has a generator (Block 4)

`tests/Test_Files/replay-fixture-CAM-Nested.FCStd` arrived in one commit with
nothing producing it, so every change to it since has been manual and this entry
had to describe a rebuild in prose. `tests/freecad_harness/make_nested_fixture.py`
is that missing generator, pinned to a seed:

    freecadcmd tests/freecad_harness/make_nested_fixture.py            # verify
    NESTING_WRITE_FIXTURE=1 freecadcmd tests/freecad_harness/make_nested_fixture.py

**Verification is the default and writes nothing.** The output is a committed
binary four gated suites depend on, so writing it takes an explicit variable.

**The script verifies its own seed rather than asserting it**: it nests twice and
compares placement digests, and reports `NOT REPRODUCIBLE` if they disagree. At
seed 1234 both runs give `66b1eefbec50651e`, 48 parts.

**The seed is not arbitrary, and the first choice was wrong.** Seed 20251007 nests
reproducibly but spreads the 48 parts over **2 sheets**, and
`test_replay_order.py` asserts one sheet -- 2 failures there, plus 1 in
`test_replay_startpoint.py` in consequence. Measured at the recorded settings:

| seed | sheets | hole nestings |
|---|---|---|
| 1 | **1** | 14 |
| **1234** | **1** | 14 |
| 20251007 | 2 | 14 |
| 7, 42, 99999 | 2 | 14 |

A one-sheet pack is reachable at the settings the layout records; 20251007 was
simply not it. The reason is recorded at the `SEED` constant.

Seed 1234 gives **the same check counts as the fixture it replaces** -- 431 / 181 /
59 / 25 across `test_replay_order`, `test_replay_dressups`,
`test_replay_startpoint` and `test_tool_clearance`, all 0 failures -- so nothing was
re-baselined.

### The generator's parameters are validated, not assumed

Every property the layout records reads **identically** on the original fixture and
on the regenerated one:

| property | original | regenerated |
|---|---|---|
| `Algorithm` | `'Minkowski'` | `'Minkowski'` |
| `RandomDirection` | `False` | `False` |
| `Generations` | `4` | `4` |
| `PopulationSize` | `10` | `10` |
| `GlobalRotationSteps` | `4` | `4` |
| `Simplification` | `0.3` | `0.3` |
| `DeflectionAngle` | `20.0` | `20.0` |
| `NestingDirection` | `90` | `90` |
| `AddLabels` / `ShowBounds` / `LabelSize` | `False` / `True` / `10.0` | same |
| sheet + `PartSpacing` | 600 x 300 x 2 mm, 4 mm | same |

So the generator reads real values off the original rather than guessing them. The
user independently confirmed Minkowski, which is what the file says.

**Correction to commit `e3378a7`.** That message claimed this fixture predates
NEST-001 and therefore records no `Algorithm` property, and that whether the
original ran Minkowski or Physics "cannot be read from the file". **It does record
it, and it reads `'Minkowski'`.** The claim came from an earlier property dump,
read before this one, and it was wrong in both halves -- the property exists, and it
was readable.

One generator setting remains unverified, and it is the only one not on the layout:
`compactness_weight = 0.0`. That is the panel's own default
(`ui_nesting.py:1337`, `prefs.GetFloat("GACompactnessWeight", 0.0)`), so a run that
never touched that dial produces exactly this value -- but nothing records it, so
this rests on the default rather than on evidence from the file.

### A measurement error worth recording

The seed sweep first reported that **no** seed gave a one-sheet pack, and would
have had that written up as a finding about the nester. It was my own bug: the
count used `Name.startswith("Sheet_")`, which matches `Sheet_Boundary_1` as well
as `Sheet_1`, so every sheet count was doubled. Re-measured with
`get_sheet_groups`, seeds 1 and 1234 both give one sheet. The doubled count also
made three different generation settings look like they had produced different
fingerprints when they had produced identical ones.

### How the fixture was rebuilt, so the next person does not have to rediscover it

Headless, through `GACoordinator` directly (the route `test_ga_loop.py` uses), not
the panel. Three things that cost time and are worth writing down:

* **`GACoordinator` must be imported after a document is open.** Importing it into
  a bare `freecadcmd` segfaults 3/3 with no output at all; with a document open
  first it is fine 2/2. The import sits below `openDocument` deliberately.
* **`freecadcmd` takes no positional argument** -- it treats one as a document to
  open and segfaults before the script runs. The run tag comes from the
  environment.
* **`deflection` is linear millimetres**: the panel converts with
  `deflection_mm = deflection_angle / 200.0` (`:1058`). Passing the angle straight
  through, or 0.0, makes every part fail with
  `ValueError: Unsupported object '<name>' or no valid 2D geometry found`.

Parameters taken from what the layout records: sheet 600x300x2 mm, `PartSpacing`
4 mm, `DeflectionAngle` 20 deg, `Generations` 4, `PopulationSize` 10,
`GlobalRotationSteps` 4, `Simplification` 0.3, `NestingDirection` 90, quantities
BottomStrap 23 / TopStrap 23 / SimpleSpacer 2.

### Gate-scope correction

Only **four** gated suites read the fixture -- `test_replay_order`,
`test_replay_dressups`, `test_replay_startpoint`, `test_tool_clearance` -- which is
697 of 1293 checks. `test_replay_flatten` and `test_replay_boundary` build their
geometry with `FreeCAD.newDocument` and open no file, so they are unaffected by
fixture staleness; an earlier estimate here put the figure at ~70% by counting
them. `test_replay_identity` also reads the fixture but is **not** in the gate.

### Fixture inputs are now committed

`.gitignore` previously excluded `tests/Test_Files/*` with a single re-admission,
so `replay-fixture.FCStd` -- which gated `test_replay_subnames.py` (63 checks)
opens -- was not in the repository, and a fresh clone would have failed that suite
rather than skipping it. All three fixture inputs are now tracked.

---

## NEST-030 — nesting `spacing` and the CAM tool diameter are unrelated controls

`status: resolved` · `severity: low` · `area: CAM replay`

Carried in `plan.md:305` as the highest-consequence open item and never filed.
Found by starting here because every other open item was either a known dead end
or somebody else's judgement call, and this one had a destructive failure mode:
a flat cutter of radius `r` sweeps `r` beyond the outline it follows, so cut
paths around two parts `g` apart overlap whenever `g < 2r` -- the tool diameter.

**Verified by reading the code, before measuring anything:**

* `shape_processor.py:289` buffers each outline by `spacing / 2.0`. That is the
  whole of `spacing`: nothing else in the nester uses it.
* There is **no reference to a tool anywhere under `Tools/Nesting/`** -- no
  diameter, no endmill, no cutter. The nester never learns what is cutting.
* The replay copies the user's **real** tool into the new job (NEST-020), so a
  wide tool now faithfully reaches a nest laid out for a narrow one.
* Nothing in `cam_replay` related `spacing` to that tool, and the
  `part_spacing_input` field had **no tooltip at all**.

**Measured: the reference nest is healthy, and the risk is user-reachable rather
than default-reachable.** On the committed fixture -- `PartSpacing = 4.0`, a
1.2 mm plasma kerf, 48 parts, 1128 pairs:

| | measured |
|---|---:|
| tightest gap between part outlines | **3.6316 mm** |
| as a fraction of `spacing` | **0.908** |
| pairs sitting at exactly `spacing` | 11 of 1128 |
| pairs closer than the tool | **0** |
| clearance on the tightest pair | **2.43 mm** |

The 0.908 is outline simplification and discretisation, not a different rule.
So the minimum gap does track `spacing`, and the mechanism is exactly what
`buffer(spacing / 2)` per outline says it should be. Severity **low**: at the
12.5 mm default a 5 mm tool has 7.5 mm of headroom, and the hazard needs the user
to tighten `spacing` below their tool diameter.

### Resolved -- a tooltip and a warning, and nothing else

**Not** tool-aware nesting. Changing the nester to buffer by
`max(spacing, tool diameter)` would alter every layout for every user to defend
against a mistake the user can avoid once told, and it needs a new control the
user did not ask for. The change here changes no layout and blocks nothing.

* **`part_spacing_input` gets a tooltip** saying it is clearance between part
  *outlines*, that it has no knowledge of the tool, and that it should be at
  least the widest tool's diameter. **This first shipped broken** -- see the
  regression note below -- and is correct only after `.widget()` was put in front
  of the call.
* **`check_tool_clearance` warns** when the tightest pair of outlines is closer
  together than the job's **widest** tool. It reuses the sheet's existing
  `FootprintCache`, so it slices nothing extra, and it runs in `replay_sheet`
  after `replay_recipe`, which is what puts the *user's* tool on the job --
  judging it earlier reads the 5 mm endmill `PathJob.Create` brings with it, and
  reported the reference nest as a warning against a tool nobody will use.

Judged on the widest tool rather than the one the first operation happens to use:
a narrower tool in use today does not make the layout safe for the wider one
already sitting in the job. Deliberately a warning and not a refusal, because
the fix is one control the user can see.

**Cost: 1.8-2.0 ms** for a 48-part sheet on a warm cache, measured. The
bounding-box prune is what makes it affordable -- the full pairwise sweep is
29.6 us per pair, so 1128 pairs is ~33 ms -- and it is skipped entirely for pairs
whose boxes cannot beat the best found so far.

### Three measurement errors, recorded so they are not repeated

All three produced confident wrong answers before being caught, and the first
would have been filed as a serious defect in the nester.

1. **The `part_*` objects carry local geometry.** They sit inside `nested_*`
   containers with `Placement = identity`, and the container holds the position.
   Measured: `part_BottomStrap_1`, `_2` and `_3` all have shape bbox
   X -12..10, Y -39.3..41.1 while their containers sit at (557.3, 288.0),
   (42.7, 288.0) and (303.3, 12.0). Measuring them as placed reported **1036
   overlapping pairs** and a 31057 mm2 intersection between two spacers --
   parts appearing to occupy the same space. The objects that carry world
   placement are the **flattened** parts, via `flatten_sheet`. This is a third
   instance of the trap the harness README records twice already (proxy class
   names that are not labels; `Base` pointing at `CAMPart_N`), and the loudest,
   because it fails spectacularly rather than quietly.
2. **A positive Shapely buffer GROWS a polygon and SHRINKS its holes.** I had
   it backwards, and it inverted the whole hole-nesting analysis: the outer
   part's buffered hole is `H eroded by d`, so a nested part with a full
   `spacing` of stand-off has its buffered outline exactly *touching* the
   buffered hole. Reconstructing the hole and eroding it a second time made a
   correct placement measure 0.0000 mm -- and the user, nesting in the GUI,
   saw correct stand-off. **The fix needs no reconstruction at all**: the
   buffered hole ring is already in `shape.polygon.interiors`.
3. **`nest()` deep-copies the parts unless `simulate=True`,** so placements land
   on copies and not on the caller's objects. The returned sheets hold them, at
   `sheet.parts[j].shape`.

A fourth is worth recording because the self-check caught it: the world
transform is the **product** `nester_placement * master.Placement`, because
`datatypes/sheet.py:249` puts the nesting placement on the container and leaves
the part's own placement alone. Composing it by hand from the wrong factor put
parts at Y -38.31 on a 300 mm sheet.

### Not covered, stated rather than implied

**Part-to-hole-wall clearance.** A kerf can land on a neighbouring part through
a hole wall as well as through a part boundary, and this check does not look.
Not because it was judged unimportant: the measurement is not established. A
nested part *does* stand off the hole wall -- the user confirmed that in the GUI
against `replay-fixture-CAM.FCStd`, and a headless nest of the same parts puts
nested parts 2.0000 mm off it at `spacing = 4.0` -- but that 2.0 comes from a
`dist == 0` that cannot separate *touching* from *overlapping*, so the figure is
not confirmed and no claim is made about it.

### Regression: the tooltip broke the panel, and the gate could not see it

Found by the user running the workbench, not by the gate. `setToolTip` was called
on the `LengthField` rather than on the widget inside it:

    self.part_spacing_input.setToolTip(...)           # AttributeError, every click
    self.part_spacing_input.widget().setToolTip(...)  # correct

`AttributeError: 'LengthField' object has no attribute 'setToolTip'`, raised in
`_build_length_fields`, so **`NestingPanel()` stopped constructing entirely** and
the workbench's main entry point failed on every invocation. Lines 748 and 752
get this right on `label_height_input` and `label_size_input`; the new call was
the only one of its kind in the file.

**The gate could not have caught it, and that is the finding.** Nothing gated
builds the panel:

* `test_panel_teardown.py` mentions `NestingPanel` but allocates it with
  `NestingPanel.__new__(NestingPanel)`, skipping `__init__` on purpose, because it
  tests `reject()`/`dispose()` and does not need a built panel.
* `freecadcmd` **cannot** build the panel at all -- `LengthField` makes
  `Gui::QuantitySpinBox` through `FreeCADGui.UiLoader()`, and `freecadcmd` has no
  `UiLoader`. Measured: `AttributeError: module 'FreeCADGui' has no attribute
  'UiLoader'`.
* `test_tooltip_assets.py`, added later for the Candidate Step diagram, checks
  the panel wiring by reading `inspect.getsource`. Source text that says
  `tooltip_with_image` while the code around it raises is still source text that
  says `tooltip_with_image`.

So a one-line typo could stop the workbench opening and the gate stay green. That
is the same failure as having no gate.

**Fixed by gating panel construction.** `test_panel_construction.py` builds the
real `NestingPanel()` and is the first gated check to do so. It has to run on the
**`freecad` GUI binary**, not `freecadcmd`, for the `UiLoader` reason above; no
Xvfb is needed, FreeCAD 26.3 starts a live GUI on no display at all. Costs
**5.2 s**, against 1.0 s for a typical `freecadcmd` suite. `run.sh` now requires
`GUI_FREECAD` with the same usage-error contract as `FREECADCMD` -- deliberately
not optional, because a gate that quietly skips a check is the same as no gate.

Beyond "it does not raise", it asserts the tooltips are readable **off the widget
the panel owns**, so a wrapper used by mistake cannot pass by leaving the string
somewhere unreachable, and it states the trap outright: `LengthField` has no
`setToolTip`, while the plain spin boxes from `make_double_spinbox` do take it
directly. The correct call differs between two adjacent kinds of field and
nothing in the type says so.

Injection-verified: reintroducing the original call reproduces
`AttributeError: 'LengthField' object has no attribute 'setToolTip'` and fails
the gate with status 1.

### Injection-verified

Seven ways, and two found real holes in the tests:

| injection | caught by |
|---|---|
| never warn | 3 checks |
| warn unconditionally | 4 checks |
| narrowest tool instead of widest | 2 pytest tests |
| never prune | correctly *passes* -- the prune is an optimisation, so removing it must not change the answer |
| prune against a loose fixed bound | correctly passes -- see below |
| `_bounds_gap` overestimates | 3 pytest tests |
| `_bounds_gap` not clamped at zero | 2 pytest tests |

The fifth injection exposed that the prune test **could not fail**, and why: with
axis-aligned boxes the bounding-box gap *equals* the true distance, so any
uniform perturbation leaves the ordering unchanged. The prune is safe by
construction -- a footprint lies inside its own box, so the box gap is a true
lower bound -- and no injection of that shape can break it. The property worth
testing is therefore that precondition directly, over 400 rotated and
overlapping shapes, which is what now catches injections six and seven.

Gate: 14 freecadcmd suites, 1265 checks, 0 failures; 571 pytest. 25 of those
checks are the new gated `test_tool_clearance.py`, plus 16 new pytest cases.

---

## NEST-031 — `random_seed` is plumbed and printed, but three drawing sites never receive it

`status: resolved` · `severity: medium (measured)` · `area: nesting / GA`

Found while scoping [NEST-029](#nest-029): rebuilding the committed fixture needs to
know whether a nest can be reproduced at all.

**Measured, and the first analysis of this was wrong in two places.** Both are
corrected below rather than quietly replaced -- the errors are the useful part.

| condition (job identical, seed 777, all 48 placements fingerprinted) | result |
|---|---|
| Minkowski, seeded, **serial** (`NESTING_ROTATION_WORKERS=0`) | **REPRODUCIBLE** -- `7fc5dac5` three times, three separate processes |
| Minkowski, seeded, 4-worker pool (the default) | NOT reproducible -- `775b7e36` / `0c47947e` / `cecad2ac` |
| Minkowski, seeded, serial, seed **778** | different fingerprint `a5900f95`, so the seed does drive the outcome |
| Physics, seeded | NOT reproducible -- `08c956cb` / `5f156dad` |

Global `random.*` calls during a **seeded** run, measured by wrapping the
module-level functions:

| algorithm | calls |
|---|---|
| Minkowski | **none** |
| Physics | `uniform` **1682**, `randrange` **464**, `choice` **57** |

### Correction 1: Minkowski's seed works, and `rng or random` is not a defect

An earlier version of this entry listed `nesting_strategy.py:338`
(`self.rng = rng or random`) as "silently discarding the seed". **It does not.**
The fallback only fires when `rng` is `None`, and `ga_coordinator.py:1191` always
sets `current_kwargs['rng'] = self.rng`. The measurement agrees: a seeded Minkowski
run makes **zero** global `random.*` calls, and is bit-identical across three
processes with the pool off. The comment on that line is accurate as written.

### Correction 2: "serial" was never tested, because `rotation_workers=0` is not serial

`_rotation_worker_limit({'rotation_workers': 0})` returns **4**, measured. Line 108
requires `int(explicit) > 0`, so `0` is not "explicitly serial", it is "unset" and
falls through to the core count. Only the environment variable
`NESTING_ROTATION_WORKERS=0` resolves to 0 -- and its own docstring says so ("0 is
meaningful and explicit: no pool at all").

So every earlier reading of "still diverges with the pool off" was the **pool**, not
the rng. The two conditions have to be set independently:

    NESTING_ROTATION_WORKERS=0   -> serial, and with a seed, reproducible
    (unset, 4 workers)           -> not reproducible even with a perfect seed

### Resolved: Physics now draws from the seeded rng

Seven sites drew from the global module. All now draw from `self.rng`, set once in
`BaseNester.__init__` as `kwargs.get("rng") or random` -- the same expression
`nesting_strategy.Nester` uses, so a direct `nest()` caller with no rng keeps the
old behaviour instead of raising.

| site | draw |
|---|---|
| `physics_nester.py:24` | spawn angle |
| `physics_nester.py:31,32` | spawn target x, y |
| `physics_nester.py:45` | randomised physics direction, when the box is ticked |
| `base_nester.py:128` | anneal `initial_side` |
| `base_nester.py:156` | anneal shake angle |
| `base_nester.py:176` | anneal rotation jitter |

`PhysicsNester.__init__` needed no change: it forwards `**kwargs` to
`super().__init__`, so the rng arrives with everything else.

Measured after, seed 777, three separate processes: **`e1a16cea750773eefc3feefb`
three times**, 48 placed each. Seeds 778 and 779 give `2058823360e93d4692105557`
and `f6717fa51edf776d4a0f5e3d`. Global `random.*` calls during a seeded Physics
run: **none**, against 1682 + 464 + 57 before.

The `rng=None` fallback still works: a `nest()` call with no rng places 48 parts
and does not raise.

### Injection-verified, and two injections that escaped

`test_rotation_determinism.py` grew Physics sections, 24 checks. All seven sites
were individually injected back to the global module, and **the first pass found
two sites the suite could not see**:

* **`initial_side` (`base_nester.py:128`) passed with status 0.** It is computed
  unconditionally but only *consumed* when `anneal_random_shake_direction` is
  false; with it on, `rand_angle` replaces it (`:158-160`). The suite had only the
  random-shake configuration. Added section 4b, and the injection is caught.
* **`physics_direction=None` (`physics_nester.py:45`) passed with status 0.** The
  suite set a fixed direction, so that branch never ran. Added section 4c, and the
  injection is caught.

Both were real gaps in the test rather than in the fix, and both are the reason the
suite now runs **three** Physics configurations rather than one: the seven draw
sites are not all on one code path. An injection matrix that only tests the
configuration that happens to be convenient proves much less than it looks like.

Full gate verified too: reverting `BaseNester` to ignore the rng gives exit 1 and
4 failures in the suite; restored gives exit 0.

### What the seed does reach

`ga_coordinator.py:535-539` reads `algo_kwargs['random_seed']`, falling back to
`random.randrange(2**32)`, and builds a private instance:

    self.rng = random.Random(seed)
    FreeCAD.Console.PrintMessage(f"GA random seed: {seed}\n")

That instance is threaded down through `layout_manager.py:118`,
`nesting_strategy.py:338`, `minkowski_engine.py:251` and all of `genetic_utils.py`.
Mutation (`:1374-1375`), crossover, tournament selection, tie-breaking (`:805`) and
the part shuffle (`:1391`) all draw from it. **The GA layer is genuinely seeded.**

### What it misses

Three sites use the bare global `random` module instead:

| site | draws |
|---|---|
| `physics_nester.py:24,31,32,45` | `angle`, `target_x`, `target_y`, `angle_rad` |
| `base_nester.py:128,156,176` | `initial_side`, `rand_angle`, rotation `jitter` |
| `nesting_strategy.py:338` | `self.rng = rng or random` -- **silently discards the seed** when `rng` is falsy |

**`random.seed(` appears nowhere in the repository** -- not in `freecad/`, not in
`tests/`. The global module runs off OS entropy on every process, seeded by nothing.

`physics_nester` is not dead code: `nesting_logic.py:285` constructs
`PhysicsNester` on the live path.

### The plumbing is inert

`nesting_controller.py:1323` reads `algo_kwargs['random_seed'] =
ui_params.get('random_seed')`, and **there is no `random_seed` anywhere in
`ui_nesting.py`** -- the user cannot set one. So it is `None`, and
`ga_coordinator.py:537` draws the seed itself from the unseeded global module. The
seed is a random draw from an unseeded source.

### The Minkowski non-determinism is thread order, not the rng -- filed as NEST-032

With a working seed and the pool off, Minkowski is reproducible. With the pool on
it is not, and the rng cannot fix it. `nesting_strategy.py:521` folds results in
`as_completed` order, and `_absorb_rotation_result` picks the winner with a strict
`<` against the incumbent. When two rotations score **identically**, which one wins
is decided by which thread finishes first.

Tie-broken `score_gravity` calls measured over one seeded serial run: **30** in run
1 and **29** in run 2 -- same seed, same inputs (input-parts fingerprint identical),
different number of ties reached, because earlier placements had already diverged.
The seeded tie-break in `minkowski_engine.py:616` (`tied[rng.randrange(len(tied))]`)
is correct and reproducible; it is being fed a different candidate set each time.

So the two defects are independent: **NEST-031 is Physics's unseeded draws, NEST-032
is the rotation pool's completion order.** Fixing either alone leaves runs
irreproducible.

### Consequence, stated no more strongly than measured

A user who reads `GA random seed: 3812746` off the console, notes it, and re-runs the
same job **does not get the same nest.** The GA decisions would replay; the physics
jitter and the `rng or random` fallback would not.

**Unmeasured:** whether `Physics` and the default algorithm both take the affected
paths, and whether `rotation_workers` threading perturbs draw order even once the
rng is threaded through. Neither is established, so the size of the divergence
between two same-seed runs is not known. A claim like "two runs differ by 3 parts"
would be invented; what is measured is only that three sites cannot see the seed.

### `NESTING_RANDOM_SEED` -- the channel the panel cannot provide

**The defect this closes:** `nesting_controller.py:1323` reads
`algo_kwargs['random_seed'] = ui_params.get('random_seed')`, and `random_seed`
appears **nowhere** in `ui_nesting.py` -- measured, 0 occurrences. So from the GUI
that value is always `None`, the seed was always drawn from the unseeded global
module, and the console printed it as though it were something the user could note
and reuse. **It was not.** Every existing consumer of a seed is a harness passing
the kwarg directly: `bench_ga.py:243`, `probe_target_sheets.py:224`,
`probe_gui_session.py:214`.

An **environment variable**, not a panel control, matching the three that already
exist for this purpose (`NESTING_ROTATION_WORKERS`, `NESTING_STEP_SIZE`,
`NESTING_FILL_DEAD_HOLES`). Precedence in `GACoordinator._resolve_seed`:
`algo_kwargs['random_seed']` -> `NESTING_RANDOM_SEED` -> `random.randrange(2**32)`.
The kwarg still wins, so **the harnesses are unaffected**.

An unparseable value **warns and falls through**. The other env overrides are
silent on a bad value, but here the entire point of setting it is reproducibility:
a user who exports `NESTING_RANDOM_SEED` with a typo and gets a silent fresh draw
would believe the run was pinned. Measured -- the warning reads:

    NESTING_RANDOM_SEED is not an integer ('banana'); ignoring it and drawing a
    fresh seed. A run started with a typo here is NOT reproducible.

The console line now names its source, so a report can say whether a number was
chosen or drawn -- the same idea `bench_ga.py` already applies to worker width:

    GA random seed: 4242 (from NESTING_RANDOM_SEED)
    GA random seed: 99935349 (random (not pinned))

Measured end to end, `NESTING_RANDOM_SEED=4242`, **no `random_seed` kwarg
anywhere** -- which is the situation the GUI is in:

| run | fingerprint |
|---|---|
| 3 separate processes | `5f8f5e616a0dc58e` three times, 48 containers |
| `NESTING_RANDOM_SEED=4243` | `6cb089c11308db76`, different |
| no env var, 2 processes | `bfeffad3...` / `60a5d978...`, and seeds `99935349` / `3854624357` |

**Deliberately not persisted on the layout.** A layout recording seed 777 but
nested elsewhere is not a promise the code can keep, and recording it would be
worse than not offering it.

**And the `NESTING_ROTATION_WORKERS=0` caveat is gone.** An earlier draft of this
entry said a pinned seed also needed the pool pinned. That was true while the fold
order decided the winner; NEST-032 removed it. Measured now: widths 0, 4 and 8
all give `973d67bc5f1d6b44b0f5d0bb` at seed 777, so the width is a pure speed
knob and the env var is sufficient on its own.

---

## NEST-032 — a seeded nest depended on the rotation pool width

`status: resolved` · `severity: medium (measured)` · `area: nesting / Minkowski`

Split out of [NEST-031](#nest-031) during its measurement, because it is a
different mechanism and fixing one does not fix the other. **Independent**: with a
seed set and the pool off, Minkowski is bit-identical across three processes
(`7fc5dac5`); with the pool on and the same seed, three processes give
`775b7e36`, `0c47947e`, `cecad2ac`.

### The mechanism

`score_gravity` breaks a metric tie by drawing from an rng
(`minkowski_engine.py:615-617`):

    tied = np.flatnonzero(scores == metric)
    if rng is not None and len(tied) > 1:
        best_idx = int(tied[rng.randrange(len(tied))])

Every rotation's evaluation was handed the **same** `random.Random` instance, and
under the pool those draws happen **concurrently from several worker threads**.
Which tie-index a given rotation received therefore depended on thread
interleaving. Measured with a shim recording the thread of each draw: **29
distinct drawing threads** for one seeded pooled run, against **1** with
`NESTING_ROTATION_WORKERS=0`.

### Correction: the fold order was blamed first, and it was not the cause

This entry originally named `as_completed` fold order (`nesting_strategy.py:521`)
as the mechanism -- results folded in completion order, with `_absorb_rotation_result`
picking the winner by a strict `<`, so an exactly-tied metric went to whichever
rotation finished first.

That is a real dependency, and **fixing it did not work**: three processes gave
`3955f9f7` / `51fcb53b` / `fc13a933` *after* the fold order was changed to
submission order. Reverting that change and minting a per-rotation rng instead
gives width independence on its own.

So the fold-order fix was **dropped rather than committed**. Reintroducing it into
the committed code changes nothing measurable -- `ee223895` at both widths -- and
that null result is recorded in the gated suite's docstring as the measurement
that removed it. A plausible-sounding mechanism that survived no test is worth
naming precisely because it was wrong.

### Measured

* Tie-broken `score_gravity` calls in one seeded **serial** run: **30** in run 1,
  **29** in run 2. Same seed, and the input-parts fingerprint was identical
  (`d4d18a0ba89294c6` both runs), so the divergence is not in the inputs.
* First divergence in a same-seed serial pair: row **15 of 48** --
  `BottomStrap_23` at angle 90.0, y identical at 69.0073, x **132.7101** vs
  **112.6643**. Same angle, same row, different column: a tie resolved differently.
* `rotation_workers=0` passed as a kwarg does **not** select serial. Measured
  `_rotation_worker_limit({'rotation_workers': 0})` -> **4**, because line 108
  requires `int(explicit) > 0`. Only `NESTING_ROTATION_WORKERS=0` reaches 0. The
  docstring at :91 already says "0 is meaningful and explicit: no pool at all",
  which makes the kwarg path the surprising one.
* Default width is `os.cpu_count()`, 4 here, so **this box runs pooled by default**
  and the serial path is reachable only through the environment variable.

### Fixed

One rng per rotation, minted on the calling thread in `angles` order, and threaded
through `_evaluate_rotation_tracked` -> `_evaluate_rotation` -> `score_gravity` as
a parameter:

    rotation_rngs = [random.Random(self.rng.getrandbits(64)) for _ in angles]

The random tie-break is **kept deliberately** -- always taking `tied[0]` would bias
placement toward whichever candidate happens to be first -- while each rotation's
draw now depends only on its own index. Cost is one `getrandbits` per rotation per
part placed.

Measured after the fix, seed 777, 48 parts placed each time:

| pool width | fingerprint |
|---|---|
| 0 (serial) | `973d67bc5f1d6b44b0f5d0bb` |
| 4 (this box's default) | `973d67bc5f1d6b44b0f5d0bb` |
| 8 | `973d67bc5f1d6b44b0f5d0bb` |
| 16 | `973d67bc5f1d6b44b0f5d0bb` |

Three processes at width 4 agree with each other. Seeds 778 and 779 give
`d88ae03e...` and `a869beb7...`, so the seed still drives the outcome.

**Packing quality is unchanged**, which was worth checking rather than assuming:
48 placed either way, efficiency **40.9427%** both, delta **+0.000000 pp**, and 48
of 48 placements identical. So this is a reproducibility fix, not a quality one.

### Injection-verified

`test_rotation_determinism.py`, gated, 12 checks: four widths must agree, two seeds
must differ, and 48 parts must be placed (so the fingerprint is of a real layout).

| injection | caught |
|---|---|
| one shared rng across all rotations | **yes** -- `da8e2bdf` vs `8df33f69` |
| every rotation sharing `rotation_rngs[0]` | **yes** -- `ee223895` vs `d6e64a8f` |
| frozen seed replacing `self.rng.getrandbits(64)` | **yes** -- caught by the seed check, since 777 and 778 then agree |
| reintroducing the arrival-order fold the fix omits | **no effect** -- `ee223895` at both widths, which is why it was dropped |

Gate exit verified too: reverting the fix gives `harness: GATE FAILED` and exit 1;
restored gives exit 0.

**Still true, and now the only reproducibility caveat left:** `rotation_workers=0`
passed as a *kwarg* resolves to `os.cpu_count()`, measured. Only the environment
variable reaches 0. That is a separate trap, asserted by the suite.
