# Master Merge Plan — pulling value from main@eac1e30 into Faster-NFP-Workbench

**Branch:** `integrate-main-2026.9`, branched from `cc57e25` ("optimisation lab notebook").
**Upstream:** `main` = `eac1e30` "Release 9-26" (2026.9.0), 53 files, +5081/−1767.
**Merge base:** `5439c2f`.

**Reconciled** against what was actually built. Status markers throughout:
`DONE` · `DECIDED` (settled by measurement) · `PENDING` · `STRUCK` (was wrong).

---

## Status

| block | what | state |
|---|---|---|
| 0.1 | characterisation harness | **DONE** — then superseded, see below |
| 0.2 | recorded baseline | **DONE** — committed, with a known weakness (§7) |
| — | `ViewObject` guards (unplanned, found by the harness) | **DONE** |
| — | real characterisation tests replacing the approximations | **DONE** |
| — | Tier 2/3 freecadcmd harness + committed baseline | **DONE** |
| 4.1 | `MasterShapes` orphaning on commit | **DONE** |
| 4.2 | pooled master group leaked on abandoned runs | **DONE** |
| 5.3 | benchmarks: rotation width, lazy-vs-eager, convex sum | **DONE** |
| — | GA-loop integration test (found a real leak in 4.2) | **DONE** |
| — | GUI-session viability probe | **DONE** — worker path is testable |
| 1.1–1.4, 2.1–2.3, 3.1, 3.2 | the author's blocks | **PENDING** |
| 3.1, 3.2, 3.3 | merge-vs-keep decisions | **DECIDED** by 5.3 |
| 3.4 → now 3.1 | `type_label` cache keys | **PENDING** |

Nine commits on the branch. All suites green.

### Two corrections to the original plan

- **§4.2 as originally written was not a live defect.** It described the
  `trial_callback` 4-vs-5 arity mismatch. Verified: our `nesting_logic` builds a
  4-arg lambda and our `nesting_strategy` calls it with 4 args at both sites
  (`nesting_strategy.py:455`, `:619`). The two agree. The mismatch only
  materialises if our `nesting_logic` is mixed with **main's**
  `nesting_strategy`, which this integration deliberately does not do.
  **STRUCK.** The block numbered 4.2 is now a different, real defect (§4.2 below).
- **§6 claimed our linear convex merge was "algorithmically better".** Measured
  in 5.3: at n = 3 — which is what `decompose_if_needed` actually yields, Delaunay
  triangles — **main's numpy is 1.33× faster**. The claim was wrong for this
  workload. Corrected below, with the reason we still keep ours.

---

## 0. Pre-conditions & enablement

### 0.1 Characterisation harness — **DONE, then superseded**

As planned, 13 tests were harvested from `inmternal-overlap-issue`. Those turned
out to be worthless as a gate: they exercised Shapely *approximations written
inside the test files* (`piece.buffer(-0.01)` standing in for a Minkowski
difference) behind `if result is not None:` guards, so they would have passed
whether or not the workbench's NFP code was correct.

Replaced with 54 tests that call the real functions and pin measured output
(`tests/test_minkowski_utils/`). The lesson is recorded because it generalises:
**a characterisation test that cannot fail is worse than none, because it is
counted as coverage.**

### 0.2 Recorded baseline — **DONE**

Committed at `tests/freecad_harness/baseline/synthetic_v1.json`: 1 sheet,
18 placed, 0 unplaced, density 0.563184, 51 perf counters.

Weakness, from 5.3: it stores a **single run**, and `make-faster.md` records
24s→31s→37s drift across sessions on untouchable work — a **20% noise floor**.
A future comparison could read pure drift as a regression. Should be re-recorded
as min-of-N. 5.3's own sweep avoids this by interleaving widths in one session
and reporting the minimum; the committed baseline does not.

---

## 1. Correctness & crash fixes (the author's bugs we're behind on) — **PENDING**

Small and well-understood, but each is a fork of a bugfix he will likely also
fix upstream. See §9 for the "send a PR instead" question.

| block | change | file |
|---|---|---|
| **1.1** | `_MainThreadRelay` FIFO + `_draining` re-entry guard (stack-overflow crash on warm re-nest) | `nesting_logic.py` |
| **1.2** | `_retire_worker()` — destroying a running `QThread` from a foreign thread takes FreeCAD down | `nesting_controller.py` |
| **1.3** | `compute_and_cache_nfp` None-master guard (do not cache a failure) + per-hole failure isolation | `minkowski_engine.py` |
| **1.4** | `#19`: set `original_polygon`/`spacing`/`deflection`/`simplification` on the reload path | `shape_preparer.py`, `nesting_controller.py` |

1.1 and 1.2 are now **test-first viable** — see §8. 1.4 is the one with a live
bug on our branch: `_create_temp_from_reloading` sets only `.polygon`, leaving
`original_polygon` None, which is exactly what poisons every NFP pair key.

---

## 2. Geometric correctness upgrades — **PENDING**

These change geometry, in the right direction. Re-baseline after 2.2.

### 2.1 IFP hole-edge sweeps `e ⊕ −P`

**Measured in 5.3** (this branch vs main's algorithm, same helpers, same corpus):

| case | ours | main | |
|---|---|---|---|
| convex hole / sq | 36.0 | 36.0 | identical |
| convex hole / tri | 16.0 | 16.0 | identical |
| convex hole / sq@45 | 26.7452 | 26.7452 | identical |
| **non-convex L / tri** | **8.0** | **4.0** | main tighter |
| **non-convex L / sq@45** | **12.7452** | **11.7452** | main tighter |

So the port is provably safe for convex holes (the sweeps remove nothing there),
and for non-convex holes the IFP gets **smaller** — the correctness fix working.
A factor-of-two difference on the triangle case is a real hole-wall overlap being
admitted, not rounding.

**GATE CHANGE:** the IFP shrinking means fewer in-hole placements are offered, so
this block's Tier 2 gate must read **"same or fewer parts placed"**. Run it with
`NEST_BENCH_ALLOW_FEWER_PLACED=1`. The flag exists for exactly this case.

`tests/test_minkowski_utils/test_ifp_convexity.py` already encodes the gate: every
convex-case area must be unchanged afterwards, and no non-convex area may grow.

### 2.2 `discretize_wires_to_polygon` dedup + `make_valid`; `EdgeOnProfileError`; `inv_rotation`

Strict improvement: dedup removes the near-zero-length edges that cause GEOS
"non-noded intersection" / "side location conflict" in the exact mask, and
`make_valid` fixes winding. Our `shape_preparer` calls it identically to main,
so the plumbing is compatible.

**This re-bases every recorded number.** Re-record the baseline afterwards and
judge all later blocks against the new one.

### 2.3 Fill `as_fill` — required `Quantity` as normal parts, then fill copies

We are on the buggy side: we mark every instance `fill_sheet`, so required copies
get queued behind fill parts and dropped ("20 requested circles nested as 12").

### 2.4 `_handle_new_master` tuple up-direction — **STRUCK**

Not a bug. Our code already uses `'Z+'` (`shape_preparer.py:426`). An agent
report claimed otherwise; verified twice, including by reverting the fix and
re-running.

---

## 3. Decisions — **DECIDED by 5.3** except 3.1-as-port

Full data in `tests/freecad_harness/RESULTS-5.3.md`.

### 3.1 `_merge_convex_parts` (greedy convex merging) — **DO NOT TAKE**

`make-faster.md:371` had already rejected it at 23.97s → 44.58s. 5.3 confirmed and
*explained* it: the merge works (48 pieces at mean 3.0 vertices → 18 at mean 4.7,
2.7× fewer pairs) but the union and convex-hull checks inside it cost more than
the pair reduction saves. Unaffected by which convex sum is used.

### 3.2 Lazy vs eager NFP — **KEEP LAZY**

main's `_precompute_all_nfps` builds 24 NFPs where a nest asks for 16, at **2.56×**
the time. It also cannot use dead-ring pruning — it commits to the whole grid
before any placement happens. Framed explicitly as favourable to lazy; the
direction is the finding, not the factor.

### 3.3 Rotation parallelism — **KEEP THE POOL**

main's *"serial evaluation is 2.4–3.3× faster"* is **not reproduced**. n70 corpus,
interleaved one session: 18.140s (w1) → 12.014 (w2) → 10.574 (w4) → 10.068 (w8),
i.e. **1.80× faster at width 8**. Packing result identical at every width.

Two caveats: this box has 4 CPUs and the curve saturates; and the branch's own
notes warn the work is GEOS-bound with brief GIL release, so oversubscription
should hurt more on a many-core machine. Revalidate before treating 4–8 as the
default.

**Consequence now open:** rotation evaluation draws from one shared
`random.Random` across threads, which makes 28 of 51 perf counters
non-reproducible. Small fix, and it blocks honest throughput measurement now
that we know the pool is worth 1.8×. See §9.

### 3.4 `type_label` cache keys — **PENDING** (an author's change)

main keys NFP/candidate caches by part *type*; we key by instance. Strictly more
sharing, and we have no `type_label` at all. Cost on our side is replacing two
inline key constructions with their `_nfp_cache_key` / `candidate_cache_key`
helpers. Prerequisite for main's rotation-skip (`is_known_infeasible`).

Do **not** convert the dead-ring `convex_pair_probe_keys` — keyed on
`master_poly1.wkb` + angles + piece indices, not labels, and immune to this.

---

## 4. Latent defects in this branch — **DONE**

### 4.1 `MasterShapes` orphaning on commit — **DONE** (`7ffd3c8`)

Our headless mode parents pooled masters in a document-level group, so
`delete_layout` cannot reach them. `commit()` only ever looked for a
`MasterShapes` child of the temp layout, so on commit it did nothing:

| | pre-fix | post-fix |
|---|---|---|
| target `MasterShapes` | `['master_STALE']` | `['master_A','master_B']` |
| unreachable from target | **6 objects** | 0 |

Fixed in four places: a shared-group fallback in `create_layout` (the `Layout`
had no handle on its own masters), a `shared_master_group` property, an explicit
`from_ga_result` parameter plus a restructured `_promote_masters` in
`nesting_job`, and the pass-through in `ga_coordinator._finalize`.

The stale row must be deleted **before** adopting the shared masters, or the
newly-adopted group is itself found and deleted. That ordering bug was hit on the
first attempt.

`tests/freecad_harness/test_master_promotion.py`, 16 checks over headless and
simulate, verified to fail without the fix.

### 4.2 The pooled group leaked on abandoned runs — **DONE** (`3598cc4`)

A cancelled or failed run left the group populated and **visible**, with 6 master
objects orphaned, because nothing else could reach it. Added
`dispose_shared_master_group()` and a `_job_committed` gate in `run()`'s
`finally`.

The gate is deliberately *not* unconditional: the success path returns a job the
controller commits later, and in worker mode `_dispatch_finalize` may not have
run yet.

### 4.3 The `_job_committed` gate itself was wrong — **DONE** (`317c2dc`)

Found by the GA-loop test, in my own 4.2 work. `_dispatch_finalize` runs
unconditionally — the `if best_layout is not None and not cancel_callback()` guard
wraps only the *fill phase* — and `_finalize` returns `None` when there is no
best layout. So the flag was set even when no job came back, a cancelled run
claimed masters nothing would promote, and 9 objects leaked. Now keyed on
`if job is not None`. The test fails again when the fix is reverted.

Also noted, not fixed: on cancel `_dispatch_finalize` is still called with a
`None` best layout. Harmless synchronously; in worker mode it marshals a
pointless payload and blocks on `_draw_event.wait()`.

### 4.4 `trial_callback` arity — **STRUCK**. Not a live defect; see the
corrections at the top.

---

## 5. What was built, in order

| # | commit | what |
|---|---|---|
| 1 | `5429c78` | harvested test harness (later superseded) |
| 2 | `c2d61ba` | `ViewObject` guards — unblocked headless nesting entirely |
| 3 | `88abaea` | real characterisation tests; removed the approximations |
| 4 | `fd8c3c2` | Tier 2/3 freecadcmd harness + committed baseline |
| 5 | `7ffd3c8` | 4.1 master promotion on commit |
| 6 | `3598cc4` | 4.2 dispose the pooled group on abandoned runs |
| 7 | `1c964fe` | 5.3 benchmarks: three either/or decisions settled |
| 8 | `317c2dc` | GA-loop integration test; fixed the 4.3 gate |
| 9 | `1d552ad` | GUI-session viability probe |

### Unplanned work these enabled

Three blocks were not in the original plan and none were optional:

- **`ViewObject` guards** — `hasattr(obj, "ViewObject")` is not a sufficient
  test; under freecadcmd the attribute exists but is `None`. A headless run raised
  on every master container and **silently produced zero parts**.
  `shape_object.py`/`label_object.py` were worse: `obj.ViewObject.Proxy = 0`
  completely unguarded, a hard crash rather than a degradation.
- **GA-loop integration test** — nothing constructed `GACoordinator` or
  `NestingController` at all. Also found `FreeCADGui.updateGui()` called
  unconditionally in `run()` (importable under freecadcmd, but with no such
  attribute), which killed the loop on its first redraw. Fixed via
  `freecad_helpers.refresh_gui()`. main has the same unguarded pattern.
- **GUI-session viability probe** — see §8.

---

## 6. What this plan deliberately does NOT include

| category | reason |
|---|---|
| UI restructure, i18n, About dialog, ManualNester Qt rework, Silhouette/Exporter DXF fixes | Orthogonal, zero overlap, zero speed value. Belong in a real merge or an upstream PR later. |
| `_precompute_all_nfps`, `ga_snapshot.py`, `ga_worker.py`, ProcessPoolExecutor path | Contradicts the lazy-NFP spine; 5.3 measured eager at 2.56× the cost. Needs `member_idx`; unusable until 1.4 lands. |
| `_merge_convex_parts` | 23.97s → 44.58s, mechanism now understood. |
| Main's numpy `minkowski_sum_convex` | **5.3 measured it 1.33× faster at n = 3** — the case the workload always hits. Still not taking it, but for measured reasons: ~9% of nest time, and it would delete the reference implementation and the `[GA PERF]` `convex_*` instrumentation. Revisit if decomposition stops producing triangles. |
| Deleting the rotation `ThreadPoolExecutor` | 5.3 measured the pool at 1.80× on 4 CPUs. Keep. |
| GA presets, compactness default `1.0`, `simulate_nesting`/`add_labels` flips | Product decisions; invalidate baselines; separate series. |
| Any file outside `Tools/Nesting/` and `datatypes/` | Out of scope. |

---

## 7. Verification, as it actually works

Three tiers, three mechanisms. Run everything with `tests/freecad_harness/run.sh`,
which reports 0 pass / 1 gate failed / 2 usage error and fails if any suite does.

| tier | what | how |
|---|---|---|
| 1 | pure geometry, no FreeCAD | `python3 -m pytest tests/` — **70 tests**. `conftest.py` installs inert FreeCAD stubs only when genuinely absent. |
| 2 | master lifecycle | `test_master_promotion.py` — **26 checks** |
| 2 | GA loop + commit | `test_ga_loop.py` — **21 checks** |
| 3 | corpus packing + perf | `nest_benchmark.py` vs the committed baseline |

Plus two opt-in suites: `probe_gui_session.py` (12 checks, needs the `freecad`
binary) and the 5.3 benchmarks.

**Gates.** `sheets`, `unplaced`, `placed`, `density` must not regress.
`NEST_BENCH_ALLOW_FEWER_PLACED=1` relaxes the last two — for **2.1 only**.

**Never gated:** wall-clock and the eleven `*_ms` counters. A timing regression
is a judgement call, not a threshold. Work counts *are* reproducible at rotation
width 1 and are printed with a percentage when they change.

Two standing traps:

- **`[PERF]` counters are sums of per-worker times, not wall clock**
  (`make-faster.md`, "Timing interpretation"). Only explicit nesting/generation
  totals are wall time.
- **20% noise floor across sessions.** Compare with interleaved A/B in one
  session and the minimum, never two runs on different days.

### Environment findings worth keeping

- `freecadcmd` **eats command-line flags**: `--pass a b c` arrives intact,
  `--pass --alpha --beta` never runs the script. Hence env-var configuration.
- `FreeCAD.Console` swallows plain `print` once a document exists. Hence `emit()`
  in every harness script.
- `freecadcmd` runs a script with `__name__` set to the module basename and does
  not reliably propagate its exit code. Hence the `__name__ in (...)` guards and
  the `.last_status*` files.
- `FreeCAD.GuiUp` is an **int**, not a bool. `if FreeCAD.GuiUp:` is right;
  `is True` is a false negative.
- FreeCAD **de-duplicates Labels as well as Names**, so a re-nest gets
  `MasterShapes001`. Lookups must match by prefix. main hit this too and fixed it
  the same way. Pre-existing in our `create_layout` exact-match, harmless because
  nothing outside `layout_manager` reads the attribute and `commit` matches by
  prefix. Left alone deliberately, to keep 4.1 to one concern.
- Orphan detection must use **reachability from the target layout**;
  `getParentGroup()` reports `None` for children of an `App::Part`, which
  produced three false positives on the first attempt.

---

## 8. Worker-path viability — **RESOLVED, and better than expected**

1.1 and 1.2 were blocked on whether a QThread and the Qt event loop are testable.
`freecadcmd` has no GUI, so signals posted across threads are never delivered.
Xvfb is installed, so a virtual display was the obvious route.

**No Xvfb is needed.** FreeCAD 26.3's `freecad` binary starts with a live GUI on
no display at all. Verified, 12 checks:

| property | result |
|---|---|
| `FreeCAD.GuiUp` | 1 |
| `FreeCADGui.updateGui` | present |
| QApplication / main thread | exists, and this *is* `app.thread()` |
| QThread | genuinely runs off the main thread |
| queued signal across threads | delivers — the `_MainThreadRelay` mechanism |
| `ViewObject` | live; `Visibility = False` takes effect |
| `NestingWorker` + `GACoordinator` | runs on a thread, marshals a draw payload |

Consequences: **1.1 and 1.2 can be landed test-first**, and the visual half of
4.2 becomes assertable.

**The trap:** signals need the event loop pumped *after* the worker finishes, not
only while it runs. A test that waits on `isRunning()` then asserts without
draining gets a false negative. The first draft of the probe made exactly that
mistake.

**Still needed** before 1.1/1.2 are landed under test: a faithful reproduction of
`NestingController._handle_draw_request` (~50 lines), or drive the controller
itself. `create_population` must actually populate
`coordinator._pending_layouts`, or `_run_generation` receives `None`. Tractable.

---

## 9. Outstanding work that is NOT the author's

Ordered by how much it blocks.

1. **Faithful `_handle_draw_request` reproduction.** Gates 1.1 and 1.2. §8.
2. **Shared-rng across rotation threads.** One `random.Random` consumed by
   concurrent rotation evaluation makes 28 of 51 counters non-reproducible.
   Now matters because 5.3 showed the pool is worth 1.8× and we cannot measure
   it cleanly. Small; per-worker seeded rng, as main's coordinator already does
   per GA member.
3. **Re-record the baseline as min-of-N.** §0.2. Currently a single run against
   a documented 20% noise floor.
4. **Candidate-geometry-cache disabled control, seed `649084969`**
   (`make-faster.md:52-58`). Required before the cache can go default-on, and
   the cache has zero test coverage despite being the one opt-in feature with a
   measured win (87.44s → 10.22s, 75.2% hit rate).
5. **`collision_intersection`** — 350.87s aggregate, the largest remaining cost.
   The notebook's own rule applies: a counting counter and a measurement on the
   fixture *before* any code.
6. **`.gitignore` regression, still uncommitted.** The working-tree file deletes
  the ignores for `minkowski_cpp/bin/`, `build/`, `*.so`, `opencode.json` and the
  n70 fixture. Restore it.
7. **23 unpushed commits, no PR** (14 pre-existing plus the 9 here). And the
   unanswered question: send the author a PR carrying §1 and §2 so the fixes
   land once and this branch carries only perf work?
8. **`nfp-cpp` (15) and `inmternal-overlap-issue` (18)** both fork from `5439c2f`
   and contain neither our work nor each other's. `nfp-cpp` is a Clipper2 C++
   NFP engine — a third competing answer to a problem now solved twice.
