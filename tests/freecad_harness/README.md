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

Re-record with `NEST_BENCH_OUT=...` after an intentional change. Keep the
schema version in step with the reader; the harness refuses a mismatch.

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
