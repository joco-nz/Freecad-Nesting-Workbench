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

## Baseline

`baseline/synthetic_v1.json` — schema 1, synthetic corpus, seed 20260925,
450×350, rotation workers 1:

| | |
|---|---|
| sheets | 1 |
| placed | 18 (6 types × 3) |
| unplaced | 0 |
| density | 0.563184 |
| used area | 88701.4668 mm² |
| wall | ~2.2 s |
| perf counters | 51 (40 reproducible counts, 11 timings) |

Re-record with `NEST_BENCH_OUT=...` after an intentional change. Keep the
schema version in step with the reader; the harness refuses a mismatch.
