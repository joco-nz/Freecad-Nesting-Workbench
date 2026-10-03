# Tier 2/3 nesting benchmark harness

Records a real nest as JSON and gates a change against a recorded "before".
This is the document-level counterpart to `tests/test_minkowski_utils/`, which
covers the pure-geometry tier under plain CPython.

## Why this is not a pytest module

`freecadcmd` ships its own Python **without pytest**, and the workbench's
document-level code (profile extraction, master creation, packing) needs a real
FreeCAD. So the two tiers are split:

| tier | what it covers | how it runs |
|---|---|---|
| 1 | view guards, `minkowski_utils` | `python3 -m pytest tests/` (FreeCAD stubs) |
| 2/3 | corpus packing, `[GA PERF]` counters | `freecadcmd` on this script |

## Why configuration is by environment variable

`freecadcmd` parses the whole command line with its own option parser before
the script runs, and its `--pass` escape hatch silently drops any forwarded
token that starts with `-`. Measured on FreeCAD 26.3.0:

```
freecadcmd s.py --pass a b c              -> argv ['a','b','c']   works
freecadcmd s.py --pass --alpha --beta     -> script never runs
freecadcmd s.py --pass --alpha v --beta w -> script never runs
```

So a flag-based interface is not reliably reachable at all. This is the same
class of collision described in `ga_coordinator.worker_mp_context`: inside
FreeCAD, the launcher claims your command-line flags for itself. The payoff of
env vars is that the common cases need no arguments.

## Usage

```sh
FREECAD=/home/james/freecad_env/usr/bin/freecadcmd   # or your freecadcmd

# Run and print
$FREECAD tests/freecad_harness/nest_benchmark.py

# Gate against the committed baseline  (exit 0 = pass, 1 = gate failed)
NEST_BENCH_BASELINE=tests/freecad_harness/baseline/synthetic_v1.json \
    $FREECAD tests/freecad_harness/nest_benchmark.py

# Re-record the baseline
NEST_BENCH_OUT=tests/freecad_harness/baseline/synthetic_v1.json \
    $FREECAD tests/freecad_harness/nest_benchmark.py
```

`freecadcmd` does not reliably propagate a script's exit code, so the status
is also written to `.last_status`.

### Variables

| variable | default | meaning |
|---|---|---|
| `NEST_BENCH_CORPUS` | `synthetic` | `synthetic`, or a path to a `.FCStd` |
| `NEST_BENCH_OUT` | – | write result JSON here |
| `NEST_BENCH_BASELINE` | – | compare against this JSON; sets the exit code |
| `NEST_BENCH_ALLOW_FEWER_PLACED` | – | relax the placed/density gates |
| `NEST_BENCH_SEED` | `20260925` | seeds the corpus and the placement rng |
| `NEST_BENCH_SHEET` | `450x350` | `WxH` in mm |
| `NEST_BENCH_SPACING` | `5.0` | part spacing |
| `NEST_BENCH_DEFLECTION` | `0.05` | tessellation deflection |
| `NEST_BENCH_SIMPLIFICATION` | `0.1` | polygon simplification |
| `NEST_BENCH_ROTATION_STEPS` | `4` | rotations per part |
| `NEST_BENCH_ROTATION_WORKERS` | `1` | rotation thread-pool width; see below |
| `NEST_BENCH_QUANTITY` | `3` | copies per part type (synthetic corpus) |
| `NEST_BENCH_DRAW` | – | build FreeCAD objects for the winner |

## Corpora

**`synthetic` (default, committed).** Six seeded part types chosen to exercise
the paths that matter: three rectangles (convex fast path), an L-shape
(concave, forces triangulation), a triangle, and a plate with a through hole
(the inner-fit path that block 2.1 changes). Default sheet is 450×350, which
fills one sheet at ~56% density.

The real `n70-...FCStd` corpus is **gitignored and machine-local**, so a
baseline keyed to it could not be reproduced by anyone else. That is why the
default corpus is generated rather than loaded. The FCStd path still works for
deeper checks:

```sh
NEST_BENCH_CORPUS=tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd \
NEST_BENCH_SHEET=700x500 \
    $FREECAD tests/freecad_harness/nest_benchmark.py
```

## Gates

Hard — non-zero exit means reject the change:

- `sheets` must not increase
- `unplaced` must not increase
- `placed` must not decrease
- `density` must not decrease

`NEST_BENCH_ALLOW_FEWER_PLACED=1` relaxes the last two. It exists for one
specific case: **block 2.1** ports main's hole-edge sweeps, which shrink the
inner-fit polygon for non-convex holes. That is the correctness fix working —
fewer in-hole placements are offered because the previous ones overlapped the
hole wall — so "fewer placed" is the *expected* outcome, and the flag makes
that expectation explicit rather than something a reviewer must infer.

Not gated: wall-clock and the eleven `*_ms` counters. A timing regression is a
judgement call about whether a geometry change is worth the time, not something
a threshold can settle. Gate on correctness and density; read the timings.

Reproducible **work counts** are not gated either, but are printed when they
change, with a percentage — they are the best available signal that a change
did more or less geometric work.

## Reproducibility: the rotation thread pool

`NEST_BENCH_ROTATION_WORKERS` defaults to **1**, and that default matters.

With the pool enabled, the work counters are not reproducible. Rotation
evaluation draws from one shared `random.Random`, and concurrent threads
consume it in scheduling order, so tie-breaks vary between runs. Over three
identical runs, **28 of 51** perf counters differed:

```
bbox_checks                    51639 / 49894 / 47487
collision_intersects_true       1856 /  2387 /  2431
nfp_cache_misses                  92 /    94 /    99
```

At width 1, only the eleven `*_ms` timing counters still move and all 40 count
counters are stable.

The nesting **result** is identical either way on the built-in corpus — 1
sheet, 18 placed, density `0.563184` every time — so this is a
measurement-fidelity problem, not a correctness one. But it means a baseline
recorded at one width cannot be compared against a run at another, so the
harness refuses to compare across widths and records the width in the JSON.

Raising the width is how you measure whether the thread pool is earning its
keep:

```sh
for w in 0 1 4 8; do
  NEST_BENCH_ROTATION_WORKERS=$w $FREECAD tests/freecad_harness/nest_benchmark.py
done
```

**Width 0 means no pool at all**, which is not the same as width 1: width 1
still constructs and tears down a `ThreadPoolExecutor` on every placement.
There was no way to express "serial" before it, so the serial-versus-pool
comparison this benchmark was written for could not be run.

That comparison has now been run — see **[RESULTS-rotation-width.md](RESULTS-rotation-width.md)**.
`bench_rotation_workers.py` sweeps 0 by default for this reason.

Headline: main@eac1e30's claim that "serial evaluation is 2.4–3.3× faster" does
**not** reproduce. Serial is 45% faster on the synthetic corpus (p=0.0008,
n=8, interleaved) and 1.9% *slower* on the n70 corpus (p=0.036, n=8). The sign
flips with the geometry, because it depends on how much of each rotation is
spent in GIL-released shapely calls versus Python. The default stays at
`os.cpu_count()`.

Two things that file records and this section does not:

- Work counts are unstable above width 1 — 28 of 82 moved across three
  identical width-4 runs — because the shared `self.rng` is drawn from *inside*
  `_evaluate_rotation` (`nesting_strategy.py:805`), so threads consume it in
  scheduling order. Ordering the pool's results does not fix it. The **result**
  is stable regardless. It also means a serial run is not doing identical work
  to a pooled one: on the n70 GA path the pool builds 46% more candidate
  geometry, so the two are not comparable even for timing.
- Width 1 is the worst of the four on n70 (4.68 s min, against 4.30 s serial,
  4.09 s at width 4 and 4.11 s at width 8): full pool cost, no parallelism.
  Noted, not changed.

## Dead-ring pruning must be active

The harness fails (exit 3) if `dead_ring` comes back empty, because a run with
the optimisation silently off is not a usable reference — a later regression in
dead-ring pruning would pass unnoticed.

This check earned its place: an earlier version cleared the dead-ring profiles
after `prepare_parts` published them, which measured the optimisation
switched **off** and recorded `dead_ring: {}` as the baseline. The profiles
have to stay published for the nest, because the pruning happens inside
`decompose_if_needed`, first reached while the NFP cache is cold during the run.

Current committed baseline: `rings_seen 28, rings_dropped 28, polygons_pruned
28` — with the result byte-identical to the pruning-off run, which is an
empirical confirmation of the documented invariant that pruning only ever
shrinks the NFP and so cannot cost a placement.

## GUI sessions and the worker path

`freecadcmd` has no GUI, so `FreeCAD.GuiUp` is False, `ViewObject` is None, and
a signal posted across threads is never delivered. That made the workbench's
worker/thread path untestable, which blocked two planned integration blocks:
**1.1** (`_MainThreadRelay` FIFO guard) and **1.2** (`_retire_worker`, the
second-run crash).

**No Xvfb is needed.** FreeCAD 26.3's `freecad` binary starts with a live GUI on
no display at all. Verified by `probe_gui_session.py` (run it with `freecad`,
*not* `freecadcmd`):

```sh
$FREECAD tests/freecad_harness/probe_gui_session.py
```

| property | result |
|---|---|
| `FreeCAD.GuiUp` | 1 (an int — test truthiness, not `is True`) |
| `FreeCADGui.updateGui` | present |
| QApplication / main thread | exists, and this *is* `app.thread()` |
| QThread | genuinely runs off the main thread |
| queued signal across threads | delivers — the `_MainThreadRelay` mechanism |
| `ViewObject` | live; `Visibility = False` takes effect |
| `NestingWorker` + `GACoordinator` | runs on a thread, marshals a draw payload |

Consequences:

- **1.1 and 1.2 can be landed test-first.** Both the relay mechanism and the
  QThread lifecycle are exercisable.
- **The visual half of 4.2 becomes assertable** — master outlines, labels,
  visibility — which the 4.2 notes had to leave unverified.
- `FreeCAD.GuiUp` is an `int`, so `if FreeCAD.GuiUp:` (what the workbench does)
  is correct and `is True` is a false negative.

### The trap in any worker-mode test

Signals need the event loop pumped **after** the worker finishes, not only while
it runs. A test that waits on `isRunning()` and then asserts without draining
the queue gets a false negative. `probe_gui_session.py` provides `pump()` and
`drain()` for this; the first draft of the probe made exactly that mistake and
looked like the signal never arrived.

### Still needed for a worker-mode GA test

The probe's draw handler only counts payloads. `create_population` is supposed
to run `LayoutManager.create_ga_population` and stash the result in
`coordinator._pending_layouts`; without that, `_run_generation` receives `None`
and raises — which the probe asserts happens, rather than hanging. A real
worker-mode test therefore needs a faithful reproduction of
`NestingController._handle_draw_request` (about 50 lines), or should drive the
controller itself. Tractable, and the natural next piece of work.

## Document units

The panel's length fields show and accept the **document's** unit system
(`doc.UnitSystem`, which also drives the global one), while everything
downstream of them stays in millimetres. Two files, two interpreters:

| file | covers | runs under |
|---|---|---|
| `test_document_units.py` | `units.py` — schema resolution, length/area formatting, `parse_length`, `length_mm` | `freecadcmd`, wired into `run.sh` |
| `probe_unit_panel.py` | `length_field.py` and the real `NestingPanel` | `freecad` (GUI), run by hand |

The split is forced: `freecadcmd` has no `FreeCADGui.UiLoader`, so
`Gui::QuantitySpinBox` cannot be constructed there at all
(`AttributeError: module 'FreeCADGui' has no attribute 'UiLoader'`). No Xvfb
needed — see above.

### The invariant the probe exists to hold

A `Gui::QuantitySpinBox` rounds whatever is written into it to the display
unit's precision, and what comes back out is the **rounded** number:

| set | reads back | schema |
|---|---|---|
| 600 mm | 599.948 mm | Imperial decimal |
| 12.5 mm | 12.446 mm | Imperial decimal |
| 0.5 mm | **0.0 mm** | Building US (ft-in) |
| 0.5 mm | **0.0 mm** | Imperial for Civil Eng (ft) |

The last two are the reason `length_field.py` exists. A 0.5 mm Part Spacing
becoming no gap at all is not a rounding argument, it is a wrong machine
setting, and it would be silent. So the millimetre value lives in Python, is
written into the widget for display, and is never read back —
`LengthField.mm()` returns the stored figure and only a real user edit replaces
it. The probe asserts exactly that, across all four imperial schemas.

Verified by injection, both ways round: making `refresh()` trust the widget's
read-back fails 24 checks, and making the clamp a no-op fails 6.

### Two more traps the probe demonstrates rather than assumes

1. **A bounded `QuantitySpinBox` is worse than an unbounded one.** Set a value
   above its maximum and the *text* updates correctly while the `Quantity`
   behind it goes stale — and it stays stale through the next user edit:

   ```python
   widget.setProperty("maximum", 10000.0)             # mm
   widget.setProperty("value", Quantity("600 mm"))
   widget.setProperty("value", Quantity("20000 mm"))  # text '787.40 in', value 12.7 mm
   # user types "500 in"                               # text '500 in',    value 12.7 mm
   ```

   So the widget is left unbounded (its defaults are ±DBL_MAX) and the range is
   enforced in Python. That is also why bounds apply to *typed input only*: a
   saved 20000 mm layout is the user's data and is stored verbatim.

2. **`w.setValue(Quantity(...))` silently writes `0.0`.** PySide resolves the
   overload to the wrong slot, and the widget announces the change with
   `valueChanged(0.0)`. Everything goes through `setProperty`/`property`
   instead. `setRange`, `setSingleStep`, `setDecimals` and `value()` do not
   exist on the PySide wrapper at all — its declared type is `QAbstractSpinBox`.

## Panel layout

Two structural properties of the panel are asserted in `probe_unit_panel.py`,
because neither is observable from a value and both fail silently.

### Collapsed groups must hide, never disable

`Nesting Settings`, `Helpers` and `Logging` all start collapsed, and the load-bearing property is
**reclaimed height**, not the toggle flag. An unchecked checkable `QGroupBox`
only *disables* its children; it does not hide them. Measured on two identical
four-checkbox groups:

| approach | height |
|---|---|
| `setCheckable(True)` + `setChecked(False)`, nothing else | **138 px** |
| same, plus the contents hidden | **40 px** |

The first is the house pattern — it is what FreeCAD's own BIM workbench does
for its "Sun Position" group (`Mod/BIM/ArchSite.py`, `setCheckable` and
`setChecked` with no hiding code). It looks like it works: the title is a
checkbox, the controls grey out, and the panel is exactly as tall as before.
The first version of this shipped that way and passed an `isCheckable()` test.

So the probe asserts the height, and verifies by injection that removing the
hiding fails it with `collapsed=179px expanded=179px`.

Two further properties, because either would silently change what a run uses:

- **Collapsed means hidden, never disabled.** Everything in these groups is
  read unconditionally by `_collect_ui_params`, so a collapse that disabled
  the children would quietly turn off verbose logging, labels and the font. The
  probe sets those controls *while they are invisible* and asserts the
  collected params see through the collapse.
- **`refresh_unit_display` still re-renders a `LengthField` inside a collapsed
  group** — a hidden widget is not a destroyed one.

Note that `setVisible` is the right call for the hiding and `setEnabled` is not:
a hidden widget still reports its real checked state.

### Where each control sits

Everything above asserts *state*: a checkbox reads back as checked, a
`LengthField` holds millimetres, a section is collapsed. None of it asserts
*position*, so a control parented into the wrong container passes all of it.
`probe_gui_layout_positions.py` covers that, and it needs `freecad` rather than
`freecadcmd` because it builds the panel.

Three properties:

- **Containment.** Each tracked control is located by walking its parents up to
  the enclosing `CollapsibleSection`, and the section's title is asserted:
  Nesting Settings 3, Optimizations 8, Physics 11, Helpers 8, Logging 2, panel
  level 14. A control that drifts into a neighbouring section fails here.
- **Parentage.** Every tracked control must have a non-None `parentWidget()`.
  Created but never added to a layout, a widget keeps a null parent — so it
  exists on `self`, satisfies every containment check above, and is simply not
  on screen.
- **Main layout order.** The seven items in the panel's own layout, in order:
  form, shape table, table buttons, action buttons, progress bar, status label,
  stretch. A stretch that migrated above the progress bar passes every other
  check in the file.

**Two files now know the panel's structure.** Moving a control from one section
to another fails here *and* in `probe_unit_panel`, because `EXPECTED` in the
probe is a table of which control belongs where — a second source of truth to
keep in step with the panel. That is deliberate: the two assert different things
(containment versus grid cell order, which stays in `probe_unit_panel`), and
neither can be derived from the other. But it does mean a section move is a
two-file edit, and finding that out from a failing test is the wrong way.

What this probe cannot see: a control in the right section but the wrong
*cell*, and two transposed rows — `probe_unit_panel`'s `_two_column_grid`
assertions cover that. Together they cover position. Neither covers whether
the result *looks* right, which is what a human is for.

### GA field persistence

`Generations` and `Population Size` round-trip through preferences, exercised
through `_collect_ui_params` (the path a real run takes) rather than a direct
`save_settings` call that could drift from it. The default is asserted to stay
1; see the measurement in the changelog for why it is not higher.

## The direction dial

Three properties that a QDial does not give you and that the panel had to
supply, all asserted in `probe_unit_panel.py`.

**A checkable `QGroupBox` is not a layout** — see the collapse section above,
which is the same class of mistake: the obvious Qt call looks like it works and
does not.

**A QDial reading is not a compass bearing.** The reading is rotated a quarter
turn and flipped, so reading 0 is bearing 270 (Down) and reading 90 is bearing
180 (Left). The readout shows the bearing, and names it when the bearing is one
of the four cardinals. Displaying the reading instead was survivable while the
only reachable values were the four the removed buttons snapped to, and wrong
at 20 of the 24 positions now reachable. `constants.dial_to_bearing()` owns the
conversion so the readout and the controller's `search_direction` cannot
disagree — the controller had it written out by hand twice.

**The 15-degree step has to be on both paths.** `setSingleStep` covers a mouse
click and the arrow keys; `setPageStep` covers PageUp/PageDown, whose default of
10 would skip past the grid and land somewhere that is not a step. Verified by
injection: dropping the page-step call fails
`the dial's page step matches ... pageStep=10`.

Persistence is stored as a **reading**, not a bearing, and snapped to the step
on load — a value from a build with a different step would otherwise land
between notches, showing a direction the user never chose. Verified by
injection: removing the snap fails with `got 37`.

## Population and the layout-0 finding

`Population Size` defaults to 1 and the reason is measured, not assumed. On
the heavy corpus at a contested 560x400 sheet, and on the n70 customer part at
1200x600, with generations 3:

| population | offspring | sheets | placed | efficiency | wall |
|---|---|---|---|---|---|
| 1 | 0 | 2 | 120 | 0.652988 | 18.0 s |
| 3 | 2 | 2 | 120 | 0.652988 | 31.2 s |
| 10 | 16 | 2 | 120 | 0.652988 | 86.0 s |

Identical to six decimals while wall goes up 4.8x. Crossover is genuinely
running — offspring goes 0 -> 2 -> 16 — so this is **not** the degenerate
small-population case where no genes are ever combined.

The mechanism: `LayoutManager.create_ga_population` applies the shuffle and the
random rotations to every layout **except the first** (`if layout.parts and
i > 0`). Layout 0 keeps the master input ordering. Across nine
generation/seed combinations, layout 0 had the lowest fitness every time, by
about 1%, and the per-layout sheet counts at a contested size show the others
splitting 2/3 or 3/4 while layout 0 stayed at the better count. Randomising a
good ordering only degrades it, so best-of-N converges on layout 0 and the
reported `efficiency` — `parts_area / (sheets * sheet_area)`, with
`parts_area` constant — cannot move.

Two consequences worth knowing when reading any population measurement:

- **The efficiency metric can only move if the sheet count moves.** Reading a
  flat efficiency at a sheet size every layout fits on proves nothing; the
  measurement above uses 560x400, where layouts genuinely disagree.
- **`elite_count = max(2, population_size // 5)`,** so population 10 still has
  only 2 possible parents. A larger population widens sampling, not the gene
  pool.

Both are properties of the search strategy rather than of the panel, and neither
is fixed here. Measured on one machine with `compactness_weight=0` across two
corpora, so a job whose master ordering is genuinely poor would be expected to
behave differently.

## Areas

`schemaTranslate` and `getUserPreferred` are length formatters and given an
Area quantity both return `('0.00', 1.0, '')` with no complaint. A yield report
that silently printed `0.00` would be worse than one that printed nothing, so
`units.format_area` converts through the schema's *stable* length unit,
squared. "Stable" is the operative word: `schemaTranslate` is
magnitude-autoscaling, and asked for 1 mm under the US-customary schema it
answers `thou` while for 1000 mm it answers `yd`. Neither works as a unit label
on a field that has to stay put while the number changes.

## Phase attribution and the GA level

Two levels of counters, both now exposed rather than log-only.

### NFP phase breakdown (`nest_benchmark.py`)

`minkowski_sum` produced ~25 phase timings and work counts that were formatted
into a `[PERF]` line and **discarded** — so answering "which phase dominates?"
needed a hand-written probe. `MinkowskiEngine` now accumulates them into
`_perf_stats`, so they reach `Nester.get_perf_stats()` and the harness.
Collected keys went 51 → 110 (36 phase, 22 work). A run now reports, as a
query:

```
nfp_phase_union_ms 154.0   nfp_phase_convex_sum_ms 85.2
nfp_phase_convex_pair_loop_ms 42.5   nfp_phase_convex_prepare_ms 37.4
nfp_phase_transform_ms 33.0   nfp_phase_decompose_ms 30.4   nfp_phase_total_ms 366.4
nfp_work_convex_pairs 818    nfp_work_parts_a 48
```

`nfp_errors` is counted too. A failed NFP becomes `{'error': ...}` and is
skipped, which **silently removes a placement region** — a `NameError` once did
exactly that and cost a run its packing (18 single-part sheets instead of 1)
with nothing crashing. Both harnesses refuse a run with any.

### GA level (`bench_ga.py`)

`nest_benchmark.py` calls `nesting_logic.nest()` directly, so it never sees
`_ga_perf` (57 counters) or `_layout_perf` (17 phase timers) — the only dataset
describing the GA *loop* rather than a single nest. Drives the coordinator
synchronously (`draw_callback=None`, `worker=None`), which is the path
single-threaded execution takes.

```sh
$FREECAD tests/freecad_harness/bench_ga.py
NEST_BENCH_GA_BASELINE=tests/freecad_harness/baseline/ga_pop2_gen2.json \
    $FREECAD tests/freecad_harness/bench_ga.py
```

Exposes the NFP phase sums at GA level (`nfp_union_s`, `nfp_convex_sum_s`, …),
the convex-pair work count, the interior-ring population metrics
(`mask_hole_rings`, `mask_subthreshold_hole_rings`, `mask_hole_sensitive_pairs`,
`mask_hole_exploiting_placements` — the `hole_pct` / `subthreshold_pct`
denominators), the loop counters, and the full layout-management breakdown
(`lm_layouts_created`, `lm_layouts_deleted`, `doc_objects_deleted`,
`lm_master_prepare_s`, …).

Two things to know:

- **Rotation width is pinned to 1** and recorded in the baseline. The tie-break
  draw in `score_gravity` happens inside worker threads against one shared
  `random.Random`, so above width 1 the consumption order is scheduling
  dependent. Measured on this corpus rather than assumed: on the *GA* config
  every counter is identical at widths 1/2/4/8, but on the *nest* config
  `nfp_cache_hits`, `nfp_cache_misses`, `candidate_points` and
  `exact_collision_checks` all move with width, while `nfp_work_convex_pairs`
  and the packing result do not. So width > 1 is measurable but not
  outcome-affecting — and width 1 is the only setting where the counts are
  exact enough to gate. The `noise` block reports this per run, so the pin
  self-enforces rather than relying on a comment.
- `performance_logging` must stay on to populate `_ga_perf`, but its per-NFP
  `[PERF]` lines go through `log_callback`, which is pointed at a sink. A single
  GA run otherwise emits thousands of them.

### Shared helpers

`harness_common.py` holds the corpus builder, document part discovery, `emit()`,
the timing-key classifier, and the rep statistics (`classify_stability`,
`collapse`, `noise_block`). It is deliberately **not** named like a script:
under `freecadcmd` a script runs with `__name__` set to its own basename, so
every harness uses `if __name__ in ("__main__", "<own name>")` — and importing
one harness from another re-executes it. That is not hypothetical; an early
debug script ran the whole suite on import.

The rep statistics are covered by `tests/test_harness_stats.py`, which runs in
plain CPython — `harness_common` installs inert stand-ins for the FreeCAD
modules precisely so the logic deciding *whether a run is comparable* sits in
the fast suite rather than only in an expensive freecadcmd run.

`bench_rotation_workers.py` still carries its own copy of the corpus builder.
Migrating it is a follow-up, not a blocker.

## Baseline

`baseline/synthetic_v1.json` — schema 1, synthetic corpus, seed 20260925,
450×350, rotation workers 1, 5 reps:

| | |
|---|---|
| sheets | 1 |
| placed | 18 (6 types × 3) |
| unplaced | 0 |
| density | 0.563184 |
| used area | 88701.4668 mm² |
| wall (min of 5) | ~1.10 s, spread ~7% |
| perf counters | 110 (63 exact counts, 47 timings) |

`baseline/ga_pop2_gen2.json` — GA, population 2, generations 2, 300×220, 3 reps:
2 sheets, 12 placed, efficiency 0.447987, `nfp_convex_pairs` 3332,
`nfp_errors` 0.

`baseline/heavy_v1.json` — tier 2, heavy corpus, 1200×600, 8 rotation steps,
3 reps: 1 sheet, 122 placed, density 0.660024, 69.1 s, all 63 counts exact.

### Config drift is a usage error, not a regression

A baseline only means something against the run that recorded it, so the harness
compares the workload-defining config fields and refuses with exit 2 if they
differ:

```
ERROR: baseline sheet='1200x600' but this run used '900x600'. That is a
different workload, not a regression.
```

Without that, changing a sheet size reports as `GATE FAILED: sheets, density` —
and the obvious response to a failing gate is to change the code. Exit 1 means
"the nest broke"; exit 2 means "you are not running the same thing". Checked:
corpus kind, sheet, quantity, per-label quantities, rotation steps, spacing,
deflection, simplification, seed, rotation width, and the candidate-geometry
cache flag.

Re-record with `NEST_BENCH_OUT=...` after an intentional change. Keep the
schema version in step with the reader; the harness refuses a mismatch.

## Three tiers

| tier | runner | corpus | cost | what it catches |
|---|---|---|---|---|
| 1 | `run.sh` | synthetic, 450×350, 18 parts | ~10 s | packing regressions, instrumentation drift, unit formatting |
| 2 | `run_heavy.sh` | heavy synthetic, 1200×600, 122 parts | ~3.5 min | throughput on a single cold nest |
| 2 | `run_ga.sh` | heavy synthetic, GA | ~1 min | throughput across layouts and generations |
| 3 | `run_ga.sh GA_CORPUS=n70` | n70 (gitignored), GA | ~3 min | the configuration actually run |

`run.sh` is the fast contract and runs on every commit. The rest cost minutes, so
they are separate and opt-in. Tiers 1 and 2 have committed baselines; tier 3
cannot, because the n70 corpus is a gitignored customer part.

**A single cold nest and a GA run are different regimes, and optimising one
tells you almost nothing about the other.** Measured on the n70 configuration:
NFP construction is over half of a single cold nest but **2.3%** of the GA run,
because the NFP misses are computed once and then hit tens of thousands of
times across the layouts. The collision stage is the reverse — a third of a
single nest, **49%** of the GA run. Every throughput claim should say which one
it measured.

**And "the NFP cost" is ambiguous, which cost this work a wrong conclusion.**
`nfp_compute` covers only the miss path that *builds* an NFP. Turning an NFP
that exists into the candidate points to test is a separate stage, 8.3 ms per
call over 4,396 calls, and it was timed and then only logged, so it was
invisible. It is **20.1%** of the GA run — 9× the construction cost, and 22.4%
end to end. So the NFP work is not 2% of the run; it is a fifth of it, in a
different place than assumed.

### Tier 2: the heavy synthetic corpus

`NEST_BENCH_CORPUS=heavy` builds one plate drilled with 36 large holes plus four
small part types. It exists to reproduce the *intensity* of a real job without
its geometry, because n70 is a customer part in a gitignored file that nobody
else can reproduce and CI cannot run.

It matches n70 closely enough to be worth trusting:

| | heavy | n70 |
|---|---:|---:|
| wall (min of N, cache off) | 69.1 s | 73.6 s |
| wall (min of N, cache on) | 58.9 s | 62.7 s |
| `nfp_work_convex_pairs` | 445 616 | 516 144 |
| `nfp_work_parts_a` / `_b` | 9 440 / 1 984 | 6 096 / 2 064 |
| `convex_result_points` | 2.63 M | 3.23 M |
| `candidate_geometries_built` | 172 256 | 153 228 |
| `exact_collision_checks` | 184 197 | 188 317 |
| `collision_intersection` | 18.4 s | 21.4 s |
| `candidate_geometry` | 11.8 s | 10.9 s |
| `union` share of NFP | **71.3%** | **70.7%** |
| `convex_sum` share of NFP | 22.0% | 25.1% |

The phase profile matching to within a couple of points is the part that matters:
a corpus that merely took a similar number of seconds could be spending them
somewhere else entirely.

**The dial is hole size, not hole count.** A first attempt used a 14×14 grid of
r=7 mm holes and produced **1** convex piece, against n70's 253 — 100× less
work. `decompose_if_needed` prunes interior rings that no nestable part can
occupy, and nothing 40–90 mm across fits a 14 mm hole, so all 196 rings were
dropped and the plate decomposed to a plain rectangle. Large, occupiable holes
are what make the part expensive. Verified with `probe_decomposition.py`, which
reports real piece counts through the actual `ShapePreparer` pipeline.

The dial has a cliff on the far side, which is worth knowing:

```
6x5 -> 198 pieces    6x6 -> 234    7x6 -> 270    7x7 -> 312
8x7 ->   1 piece  (collapsed: 8mm webs stop triangulating)
```

6×6 lands within 8% of the Spacer's 253. "More holes" is not monotonically
heavier.

## The n70 corpus

`tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd` is a real job:
3 part types — **Spacer** (510×438 mm, the one that dominates), **Bottle Top**,
**Bottle Bottom** — inside `PartDesign::Body` objects with Sketches, Pads and
Origins. `discover_doc_parts` correctly finds the 3 Bodies and skips the 30
scaffolding objects. The Spacer's 13 bottle-sized holes are what make it
expensive: 253 convex pieces, so a Spacer-against-Spacer NFP is 192×192 = 36 864
convex pairs. `probe_decomposition.py` reports those counts.

It is **gitignored** (added in 3507c79, "Ignore updated"), so it is the one
workload here that cannot back a committed baseline. `run_heavy.sh` covers the
same intensity with the heavy synthetic corpus; n70 stays as tier 3, run locally
against a baseline stored outside the repository:

```sh
HEAVY_CORPUS=tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd \
NEST_BENCH_QUANTITIES='Spacer=2,Bottle Top=60,Bottle Bottom=60' \
NEST_BENCH_BASELINE=~/n70_local.json tests/freecad_harness/run_heavy.sh
```

Reproduces the workload in `make-faster.md`:

```sh
NEST_BENCH_CORPUS=tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd \
NEST_BENCH_QUANTITIES='Spacer=2,Bottle Top=60,Bottle Bottom=60' \
NEST_BENCH_SHEET=1200x600 NEST_BENCH_ROTATION_STEPS=8 \
NEST_BENCH_REPS=3 $FREECADCMD tests/freecad_harness/nest_benchmark.py
```

Measured here: **122 placed, 2 sheets, 41.8% density, 72.5 s** per nest
(min of 3, spread 1.9%). The notebook's GA run of the same part set reported 122
placed, 2 sheets, 40.4% — so this is the same workload.

`NEST_BENCH_QUANTITIES` exists because the workload is not uniform. The FCStd
path used to hardcode quantity 1, which meant `NEST_BENCH_QUANTITY=2` and `=5`
produced byte-identical runs and any n70 measurement was silently measuring one
of each part. Found by measuring, not by reading. A malformed entry raises
rather than being skipped, because a dropped quantity gives a clean-looking run
of the wrong workload.

## The candidate-geometry cache

`NEST_BENCH_CANDIDATE_GEOMETRY_CACHE=1` enables the one opt-in feature with a
measured win and no test coverage. `bench_candidate_cache.py` A/B's it
interleaved and answers three questions in order of importance: is it
layout-neutral, what does it save, what does it cost in memory.

On the n70 workload: **layout-identical across 6 runs**, `candidate_geometry`
10 757 ms → 886 ms, total wall 72.688 s → 62.715 s (0.863×) against a 1.3%
control spread, 92.2% hit rate, **+0 MiB** peak RSS. Full write-up in
[RESULTS-9.4.md](RESULTS-9.4.md).

Memory is measured in a **separate process per arm**, not in-process:
`ru_maxrss` is a process high-water mark and never falls, so a reading taken
after the other arm has run is contaminated by it.

### Default on

The cache is now **on by default** in the workbench. `CANDIDATE_GEOMETRY_CACHE_DEFAULT`
in `freecad/nestingworkbench/constants.py` is the single source of truth, read by
the UI checkbox, the preference read-back, the controller's two fallbacks, the
coordinator's gate and both harnesses. Five hardcoded booleans would be five
chances to disagree about what the workbench actually does.

It lives in `constants.py` rather than beside `CandidateGeometryCache` because
`ui_nesting` needs it at module scope and must not import `nesting_strategy` —
that pulls in Shapely, and the panel has to keep importing when the optional
nesting dependency is missing.

**Existing installs keep the old behaviour.** The preference is written on save,
so anyone who has already run this workbench has `CandidateGeometryCache=False`
stored. Deliberate — a saved setting is the user's choice — but it means the new
default reaches new users and anyone who resets preferences, not everyone.

`NEST_BENCH_CANDIDATE_GEOMETRY_CACHE` is tri-state so the harness measures the
product rather than a parallel opinion of it:

| value | meaning |
|---|---|
| unset (or `default`) | follow `CANDIDATE_GEOMETRY_CACHE_DEFAULT` |
| `0` / `off` / `false` | explicitly off — the A/B control arm |
| `1` / `on` / `true` | explicitly on |

A harness that pinned its own default would let the two drift: the workbench
could ship default-on while every committed baseline still described the off
path, and the gate would stay green while measuring something nobody runs.
`test_candidate_geometry_cache.py` guards that, by source inspection of all
three call sites as well as behaviourally.

### Where the remaining time goes

Two write-ups, both measurement-first per `make-faster.md`'s own rule — and one
of them ends in a win that was measured and then **disproved**, which is the more
useful outcome.

- [RESULTS-union.md](RESULTS-union.md) — the union is 71% of NFP time, 8 calls of
  54 756 inputs hold 98.4% of the work, and 99.98% of the input geometry does
  not survive into the answer. Not removable at the union: 6 duplicates in
  54 756, and the containment filter is O(n²). The lever is the decomposition
  upstream — 234 convex pieces for 36 rectangular holes — and `main@eac1e30`
  already ships that merge, which this branch had lost along with a reverted
  O(n³) attempt at the same idea. Porting it takes the heavy corpus from
  445 616 convex pairs to 19 256 and the run from 59.4 s to 37.2 s, packing
  bit-identical — a regression recovered, **not** a gain over main. It does not
  show on a GA run of that configuration, where NFP construction is 4.5% of the
  total.
- [RESULTS-parallelism.md](RESULTS-parallelism.md) — where the n70 GA run's time
  goes, and the search for 20–30 s. The answer is mostly not an optimisation:
  the rotation pool defaults to `cpu_count() + 4` (8 threads on a 4-CPU box) and
  4 workers is **22.3% faster than 1 and 8 is no better than 1** — 41.6 s, with
  packing density bit-identical. Also the three optimisations that were measured
  and rejected: body-only overlays (2.9× on the Spacer, weighted 1.11×),
  `shapely.prepare` (no effect), and `overlaps or contains` (13.3× cheaper and
  wrong). The remaining lever is the unmeasured `step_size` density dial.
- [RESULTS-collision.md](RESULTS-collision.md) — the collision stage decomposed
  into overlay 65.6% / `intersects` 24.7% / bookkeeping 9.7% on the n70 GA run,
  where it is 49% of wall. Replacing the overlay with `overlaps or contains` is
  6–10× cheaper and would be worth ~17% — and is wrong: **89.4%** of overlays
  measure a positive area below tolerance, totalling 0.033 mm², and a predicate
  would reject them as real overlaps. That 51.8 s of necessary work is 29% of
  the run.

`probe_union_cost.py`, `probe_union_determinism.py` and
`probe_collision_overlay.py` produce those; `probe_decomposition.py` reports
convex piece counts through the real `ShapePreparer` pipeline.

Two instrumentation traps found along the way, both worth knowing about:

- **A "measurement-only" probe inside a production timer.** `_holes_touched` is
  correctly gated on `probe is not None` and never runs in production, but it sat
  inside the `collision_intersection_ms` window — so every logged run overstated
  collision cost by 8.8%. Now timed separately.
- **A new counter in the absorption allowlist but not in the schema.** The
  absorption does `self._perf_stats[key] += ...`, so a key listed there but not
  in the initialiser raises `KeyError` inside the per-rotation future loop. With
  `quiet=True` the exception is swallowed with no output, every rotation
  evaluation dies, and the run reports **zero placed parts** while looking
  healthy. `tests/test_perf_counter_plumbing/` now checks the two lists agree,
  structurally.

## Reps, and what a baseline is allowed to claim

A baseline recorded from one sample is a claim about a machine state that does
not repeat. Both benchmarks now run **N reps in one process** (`NEST_BENCH_REPS`,
default 5 for nest, 3 for GA), each with cold caches, and every baseline carries
a `noise` block saying what is exact and what is not:

```json
"noise": {
  "reps": 5, "rotation_workers": 1,
  "wall_seconds_spread_pct": 11.4,
  "work_counts_exact": true, "work_counts_unstable": [],
  "timings_vary": 37
}
```

Two tiers, and the distinction is the point:

| | estimator | gate on it? |
|---|---|---|
| result fields (`sheets`, `placed`, `unplaced`, `density`) | must be **identical** across reps, else exit 3 | yes |
| work counts (63 of 110) | must be **identical** at width 1, else exit 3 | yes, as a diff review |
| timings (47 of 110) | **minimum** of N | no |
| `wall_seconds` | **minimum** of N | no |

A work count that moves between reps is a bug, not noise, and it is a hard
failure. Averaging it would be worse than useless: the mean of a count is not a
count, and averaging a count that should be deterministic is exactly how a real
regression gets smoothed into a plausible-looking number. `hc.collapse` never
does it, and `tests/test_harness_stats.py` asserts it does not.

**Reps must be cold.** `Shape.nfp_cache` is class-level, so without an explicit
clear the first rep computes every NFP and the rest are pure cache hits. That
moved 24 counters and made every rep after the first a different measurement. It
was found by the count-stability gate, not by inspection, which is the argument
for having the gate.

### On the 20% noise floor

`make-faster.md` recorded per-generation drift of 24s → 31s → 37s across three
consecutive runs on the n70 corpus, and concluded the noise floor is ~20%. Two
caveats, because that number is heavier than it looks next to what is committed:

- The committed baseline is the **synthetic** corpus, where wall spread measures
  ~6–11% over five in-process reps. The 20% figure came from the much longer n70
  runs, not from this corpus.
- Five reps on a quiet box is a thin basis for a noise floor anyway. A
  defensible figure is a deliberate exercise — three consecutive runs under
  comparable load, recorded, not inferred — not something to fold into a test.

The heavy corpus is where drift actually bites, and it **cannot have a committed
baseline**: `tests/Test_Files/n70-*.FCStd` is gitignored, so no one else could
reproduce it. Heavy-corpus measurement therefore stays an ad-hoc activity in the
style of `bench_rotation_workers.py` — interleaved within one session, minimum
of N, never compared against a stored number.

### Which is the real noise floor?

Measured on the n70 workload, 3 back-to-back reps in one process: wall spread
**1.9%**, and all 63 work counts **exact**. Not 20%.

Both numbers can be right, because they answer different questions. In-process
reps sample a quiet box with a cold cache each time; the 24s → 31s → 37s
sequence was across conditions. So in-process reps *understate* the variation
you will see between a quiet box and a loaded one — which is exactly the
variation that matters when deciding whether a number moved. The honest reading:

- for **gating**, in-process reps at width 1 are sufficient and the discrete
  fields are exact on both corpora;
- for **claiming a speedup**, the measurement has to be interleaved with the
  thing it is compared against, in one session, minimum of N. Never a stored
  number.
