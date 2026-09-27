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
for w in 1 4 8; do
  NEST_BENCH_ROTATION_WORKERS=$w $FREECAD tests/freecad_harness/nest_benchmark.py
done
```

That comparison is still outstanding — `make-faster.md` lists "test four
rotation workers versus eight" as not yet done, and it is what would settle
main's claim that serial evaluation is 2.4–3.3× faster.

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
| 1 | `run.sh` | synthetic, 450×350, 18 parts | ~10 s | packing regressions, instrumentation drift |
| 2 | `run_heavy.sh` | heavy synthetic, 1200×600, 122 parts | ~3.5 min | throughput and algorithmic regressions |
| 3 | ad hoc | n70 (gitignored) | ~25 min | geometry-specific behaviour |

`run.sh` is the fast contract and runs on every commit. `run_heavy.sh` costs
minutes, so it is separate and opt-in. Both are committed baselines.

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
