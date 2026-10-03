# The forced `updateGui()` calls in Simulate mode: what they cost, and why they cannot simply be deleted

Follows `RESULTS-simulate-churn.md`, which measured Simulate's object churn
(39.6% of generation time) and attributed the *rest* of Simulate's 5.6× gap to
"the drawing half" without profiling it. This file profiles it.

The instrumentation is commit `29f16e9`; the regime counters and the two ceiling
arms are the work recorded here. Nothing has been changed in the drawing path.

## Short answer

**Suppressing the trial-pump alone saves 25.6 s of a 151.9 s run — 16.8% — with a
byte-identical packing result and every placement still drawn.** That is the
ceiling for the trial coalescing now scoped, and it is half what the trial
counter appears to promise (55.5 s), because roughly half the painting is
deferred to the next pump rather than eliminated.

Two of the three obvious theories were wrong, and knowing which is why:

- The pumps are **not** draining a backlog. Queue high-water is **3**. The
  painting is on the critical path, not overlapped with nesting.
- The pumps **cannot be deleted**, because `updateGui()` is also the event-loop
  drain that executes callbacks posted from the rotation worker thread. Delete
  it and 679 of the 1,325 trial callbacks silently never run at all.

## 1. Method

Same configuration as `RESULTS-simulate-churn.md` so the two are comparable:
n70 corpus, sheet 700×500, quantity 6 → 18 parts, `NEST_BENCH_GA_POPULATION=10`,
`NEST_BENCH_GA_GENERATIONS=4`, `rotation_steps=4`, rotation width pinned to 1 so
work counters stay exact, `NEST_BENCH_REPS=1`.

**Simulate needs the GUI binary.** `updateGui()` does not exist under
`freecadcmd` and the run then fails *silently* — 0 sheets, 0% efficiency, exit 0
(`RESULTS-simulate-churn.md` §2).

Three arms, all measurement-only:

| arm | `NEST_BENCH_GA_SIMULATE` | extra | what it means |
|---|---|---|---|
| **A** | 1 | — | baseline, pumps as shipped |
| **B** | 1 | `NEST_BENCH_GA_NO_PUMP=1` | ceiling with **every** pump neutralised |
| **C** | 1 | `NEST_BENCH_GA_NO_TRIAL_PUMP=1` | ceiling with only the **trial** pump neutralised |

Both switches are harness-only, default-off, and monkeypatch `FreeCADGui` in the
bench process. There is deliberately no product flag that can reach a user.

Arm C suppresses the pump by wrapping the *call site* (`draw_trial_placement`),
not the pump itself, so nested pumps are suppressed too. Each trial draw's pump
is what drains the next queued trial, so leaving them on would measure nothing.

## 2. Time

| | A baseline | B no pumps | C no trial pump |
|---|---:|---:|---:|
| **outer wall** | **151.92 s** | **97.95 s** | **126.37 s** |
| `generations_s` | 133.77 s | 79.70 s | 108.21 s |
| `nesting_s` | 81.60 s | 31.42 s | 56.43 s |
| `placement_wall_s` | 58.51 s | 27.56 s | 28.44 s |
| `rotation_wall_s` | 26.72 s | 24.98 s | 25.92 s |
| `layout_management_s` | 44.64 s | 44.10 s | 44.02 s |
| `validity_stage_s` | 21.06 s | 20.04 s | 20.90 s |
| **saving** | — | **−53.97 s (−35.5%)** | **−25.56 s (−16.8%)** |

**Result identical in all three: 6 sheets, 18 placed, efficiency 0.35397.** Every
geometry counter matches exactly (`candidate_geometries=16363`,
`exact_collision_checks=102964`, `bbox_checks=680643`, `nfp_hits=2315`), and
`layout_management` and `validity_stage` are flat across arms — so none of this
is the search, and none of it is the object churn the previous file measured.

## 3. The regime, which was the thing worth measuring

`29f16e9` timed the drawing calls but not *how they were reached*, and the two
ways are not the same cost:

- **direct** — the wrapper runs the drawing inline, so every `updateGui()`
  blocks the caller;
- **relayed** — the caller posts to `_MainThreadRelay` and returns, leaving the
  main thread a backlog.

| arm | direct | relayed | queue high-water |
|---|---:|---:|---:|
| A | 2,023 | 679 | 3 |
| C | 2,023 | 679 | 4 |

**Queue high-water 3.** The backlog hypothesis is dead: the main thread keeps up,
so the pumps are on the critical path and their cost is real wall time. This was
worth measuring because if the queue had been deep, the painting would have
overlapped nesting and coalescing would have bought far less.

It also shows `bench_ga.py` measures the **direct** regime, while the real app
runs the GA on a `NestingWorker` thread (`nesting_controller.py:65,684`) and so
takes the **relay** path. Same pump count, different scheduling. Both arms are
measured in the direct regime, so the saving transfers as *work removed* but the
wall-clock figure is optimistic for the threaded app.

## 4. The pumps cannot be deleted — they are the drain

| arm | `trial_calls` | `trial_gui_s` | `update_gui_s` |
|---|---:|---:|---:|
| A | 1,325 | 55.54 s | 19.65 s |
| B | **646** | 0.02 s | 0.00 s |
| C | **1,325** | 0.04 s | 24.35 s |

Arm B's trial count collapsed by exactly **679** — the relayed callback count.
Not a coincidence and not a search effect:

1. the trial callback fires from the rotation worker thread, so it is *posted*,
   not called inline;
2. `FreeCADGui.updateGui()` processes the Qt event queue, so it is what actually
   executes those posted callbacks;
3. neuter it and 679 callbacks are still sitting in the queue when the run
   returns. The process then exits and they are gone.

So `updateGui()` here is not only a repaint — it is the event loop. Worse, the
nesting runs *reentrantly*: each trial draw pumps, which runs the next queued
trial, which pumps again.

This is also a latent bug independent of performance. A relayed trial callback
runs whenever the main thread next pumps, which may be long after the rotation
it visualises finished — the callback draws a position the search has already
moved past.

## 5. Why the ceiling is 25.6 s and not 55.5 s

Arm C removes 55.5 s of trial pump and recovers 25.6 s of wall. The gap is in the
placement pump, which **rose** from 19.65 s to 24.35 s.

Total pump time fell 75.19 s → 24.39 s, a 50.8 s reduction, while wall fell only
25.6 s. So about half the removed pumping was not eliminated: it moved to
whichever pump happened next. Reassigning a `Part::Feature`'s Shape still marks
the scene graph dirty; the GL work is deferred, not skipped.

**Practical consequence:** setting expectations against `trial_gui_s` will
overstate any fix by roughly 2×. The measured ceiling for trial coalescing is
**−25.6 s (−16.8%)**, and a coalescing scheme that keeps some trial paints will
land below it.

## 6. What is left, and what it would cost

Not started. Ordered by value.

1. **Coalesce trial draws** (the scoped work). Keep one pending trial draw;
   replace it with the latest rather than queueing all 1,325. Must keep a pump
   to drain the slot — see §4 — so the placement pump is load-bearing and cannot
   be throttled away as part of this.
   *Ceiling:* −25.6 s measured (arm C). *Risk:* the trial animation becomes
   coarser; visual quality needs a human eye, since no test can distinguish "the
   animation reads fine" from "the animation did not change".

2. **Stop redrawing stale trials at all.** The relay delivering a trial
   visualisation after the rotation has moved on (§4) is arguably wrong
   independent of cost, but fixing it changes what the animation shows.

3. **Time-budget the placement pump.** Deliberately excluded — the constraint is
   that every placement is painted. Worth ~19.7 s if that constraint is ever
   revisited.

4. The `instances` churn (51.8 s) and master sharing are `RESULTS-simulate-churn.md`
   §6 items and are untouched here.

## 7. Caveats

- **n=1 per arm.** Three arms, three runs. `validity_stage` and
  `layout_management` are flat across arms, which is the check that matters most
  — but the wall-clock savings are single samples on a 4-CPU box, and
  `RESULTS-simulate-churn.md` already records `updateGui` timings moving several
  seconds run to run. Quote −25.6 s as "about 25 s", not to three figures.
- **The direct regime, not the app's.** See §3. The bench runs the GA
  synchronously; the app runs it on a worker thread. Work removed transfers;
  wall-clock overlap does not.
- **`trial_shape_s` and `trial_geom_s` are unchanged by design** (1.37→1.52 s,
  0.87→0.91 s). The Shape is still reassigned 1,325 times in every arm; only the
  repaint is suppressed. This is a repaint fix, not a redraw fix.
- **Arm B is not a candidate.** It drops 679 callbacks (§4) and would be a
  behaviour regression. It is a ceiling, nothing more.

## Reproducing

```sh
GUI=/home/james/freecad_env/usr/bin/freecad      # NOT freecadcmd

COMMON="NEST_BENCH_CORPUS=tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd
        NEST_BENCH_GA_SHEET=700x500 NEST_BENCH_QUANTITY=6
        NEST_BENCH_GA_POPULATION=10 NEST_BENCH_GA_GENERATIONS=4
        NEST_BENCH_ROTATION_STEPS=4 NEST_BENCH_ROTATION_WORKERS=1
        NEST_BENCH_REPS=1"

env $COMMON NEST_BENCH_GA_SIMULATE=1 $GUI tests/freecad_harness/bench_ga.py
env $COMMON NEST_BENCH_GA_SIMULATE=1 NEST_BENCH_GA_NO_PUMP=1      $GUI tests/freecad_harness/bench_ga.py
env $COMMON NEST_BENCH_GA_SIMULATE=1 NEST_BENCH_GA_NO_TRIAL_PUMP=1 $GUI tests/freecad_harness/bench_ga.py
```

Compare `wall min`, and the `simulate drawing (this rep, delta)` block, which
reports the threading regime and queue high-water alongside the timers.

## Summary

| | value |
|---|---|
| trial-pump suppression, measured ceiling | **−25.6 s of 151.9 s (−16.8%)** |
| all pumps, ceiling | −54.0 s (−35.5%), but drops 679 callbacks |
| pump cost on the critical path | **yes** — queue high-water 3, no backlog |
| `updateGui()` is also the event-loop drain | **yes** — deleting it loses 679 callbacks |
| result under every arm | 6 sheets, 18 placed, 0.35397 |