# Master Merge Plan — Pulling value from main@eac1e30 into Faster-NFP-Calc-Investigate

**Goal:** Selectively integrate provably-beneficial, non-overlapping, and correctness-fixing capabilities from the upstream `main` (commit `eac1e30`, "Release 9-26") into this working branch, in a way that can be verified after each block and reverted if needed.

**Branch:** `Faster-NFP-Calc-Investigate` (tip `cc57e25`, 14 unpushed commits). Common ancestor with `main`: `5439c2f`.

**How this plan works:** Each numbered block is a self-contained hunk that can be applied independently with `git cherry-pick -n` or manual copy. After each block, a verification step (Tier 1→3, see §7) must pass before the next block is applied. Every block carries a `Ported-from:` trailer so a future full merge can locate the exact upstream origin. No block depends on a later one — any block can be dropped with `git revert <sha>`.

## 0. Pre-conditions & enablement

### 0.1 Install a characterisation harness
**Source:** `inmternal-overlap-issue` branch (13 test files, including `test_nfp_cpp_python_equivalence.py`, `test_inner_nfp.py`, `test_outer_nfp_holes.py`, `test_nfp_concave.py`, `test_centroid_alignment.py`, performance tests, conftest fixtures).

**Action:** Harvest the test suite from that branch, adapt any absolute paths to this repo's layout, and make them runnable on the current code. Commit these as `0.2-tests`.

**Why:** You cannot safely land ~15 geometry behavioural changes without a baseline. The other branch provides exactly the right test corpus (NFP correctness, IFP, concavity, centroid alignment, performance baselines). Without this, every later block's "does it improve speed/correctness?" is unanswerable.

**Commit after:** Tier 1–2 tests pass against the baseline (step 7.2).

### 0.2 Record the baseline
**Files:** none (numbers live in a `bench/` directory or a wiki; out of scope for the merge plan).

**Action:** Run the corpus (`n70-intercooler-spacer-bottle-nesting.FCStd` or equivalent) with a fixed seed, capture:
- `[GA PERF]` counters
- `[LAYOUT PERF]` counters
- Dead-ring stats (`polygons_pruned`, `rings_seen`, `rings_dropped`, `parts_from_misses`)
- Pack result: sheets, parts/placed, density, `hole_pct`
- Any `is_valid == False` or `is_empty == True` polygon findings.

**Why:** Every later block's "neutral-or-better" gate in §7 compares against these numbers.

**Commit after:** Baseline file is generated and its path is noted in `0.2-tests`.

---

## 1. Correctness & crash fixes (author's bugs you're behind on)

These are the author's *own* fixes from `eac1e30` that address bugs still live on this branch. They have zero overlap with your perf work, and they are strictly improvements.

### 1.1 `_MainThreadRelay` FIFO + `_draining` re-entry guard
**File(s):** `freecad/nestingworkbench/Tools/Nesting/nesting_logic.py` (lines 12–34).

**From main:** The relay now has an explicit `deque` + `_draining` flag; `updateGui()` re-entering the drain continues the queue instead of nesting one callable inside another, which previously caused a stack overflow and FreeCAD death on a warm re-nest.

**Verification:**
- Tier 1: Run the NFP correctness tests (0.2) — no invalid polygons, no crashes.
- Tier 2: Same corpus, re-nest twice without a full document reset — no `TypeError`, no one-part-per-sheet.
- Tier 3: Diff perf dump against 0.3 baseline — neutral or better.

**Why now:** This is a genuine crash fix; it does not touch any of your performance machinery. It is also a prerequisite for any later block that touches `trial_callback` or the GA loop, because the old code will silently segfault/crash under the same conditions main just fixed.

### 1.2 `_retire_worker()` — destroy running QThread from main thread
**File(s):** `freecad/nestingworkbench/Tools/Nesting/nesting_controller.py` (new method near `_on_nesting_finished`/`_on_nesting_error`).

**From main:** `worker.wait()`, then `worker.coordinator = None; coordinator.worker = None; coordinator.draw_callback = None`. Docstring: *"Destroying a QThread from a foreign thread (or while still running) is a hard crash, which is why a second run in the same panel could take FreeCAD down."*

**Verification:**
- Tier 1: Run the harness, run nesting, close panel, re-run nesting (same session) — no crash.
- Tier 2: Same as Tier 1, but also toggle simulate nesting on/off — still no crash.
- Tier 3: Same perf comparison as 1.1.

**Why now:** You lack this entirely. Your `_on_nesting_finished` just sets `self._worker = None`. A second nesting in the same session will raise or crash FreeCAD. Main's fix is minimal and well-scoped.

### 1.3 `compute_and_cache_nfp` None-master guard + per-hole failure isolation
**File(s):** `freecad/nestingworkbench/Tools/Nesting/algorithms/minkowski_engine.py` (lines 77–87, 135–142).

**From main:**
- Guard: `if shape_A.original_polygon is None or part_to_place.original_polygon is None: log and return {} without caching`, so a transient failure cannot disable a pair for the whole session.
- Per-hole: `try/except` around `calculate_inner_fit_polygon` + one-shot warning (`_warn_hole_fit_failed`), so one bad hole cannot poison the whole pair's NFP.

**Verification:**
- Tier 1: Correctness tests from 0.2 all pass.
- Tier 2: Simulate a missing `original_polygon` (patch `shape.py` to set it `None` temporarily, run a nesting, restore) — the pair is retried next rotation, not cached as empty.
- Tier 3: Perf diff neutral.

**Why now:** The author's notebook and commit text both flag this: *"Do NOT cache this. Shape.nfp_cache persists across runs by design, so a cached failure would disable this pair for the whole session."* Your code has no guard, so a single bad part permanently cripples that part-type's NFP cache.

### 1.4 Fix re-nest crash on a saved layout (#19) — original_polygon set on reload
**File(s):** `freecad/nestingworkbench/Tools/Nesting/shape_preparer.py` (`_create_temp_from_reloading`, lines 344–355) + `freecad/nestingworkbench/Tools/Nesting/nesting_controller.py` (`_load_shapes_from_layout`, `_load_params_from_layout`).

**From main:** On the reload path, set `original_polygon`, `spacing`, `deflection`, `simplification` on the reloaded wrapper (your code sets only `.polygon`). Also mirror the source master's rotation instead of `Placement(..., Rotation)`, and make `temp_bound` visible.

**Verification:**
- Tier 1: Correctness tests pass.
- Tier 2: Reload a saved layout, re-nest — no `TypeError`, no one-part-per-sheet, master outline matches.
- Tier 3: Perf diff neutral (this is a bugfix, not a perf change).

**Why now:** You are missing this fix. Your `_create_temp_from_reloading` leaves `original_polygon = None`, which is exactly what the author's comment identifies as the #19 cause: *"Leaving this at None makes compute_and_cache_nfp raise on `mA.centroid` and cache an error for every pair key."*

---

## 2. Geometric correctness upgrades (your branch should take these even though they change geometry)

These improve the math of the IFP and polygon handling. They do change geometry, but in the right direction (less permissive IFP for non-convex holes, fewer false-positive overlaps). Re-baseline after block 2.2.

### 2.1 IFP hole-edge sweeps `e ⊕ −P` subtraction
**File(s):** `freecad/nestingworkbench/Tools/Nesting/algorithms/minkowski_utils.py` — new function `_merge_convex_parts` is **not** taken (see block 3.1). The IFP rewrite in `calculate_inner_fit_polygon`.

**From main:** After computing per-piece vertex erosions, also compute *hole-edge sweeps* `e ⊕ −P` (the convex hull of every hole edge + the negated piece) and subtract `union_all(sweeps)` from the result. Main's comment: *"Vertex erosion is exact only for a convex hole: also exclude every position where a hole edge touches or crosses this piece."* Your IFP is per-piece erosion only, which for a non-convex hole is over-permissive — a candidate centroid can be accepted whose part actually crosses the hole boundary.

**What I verified:** Your engine already handles the `MultiPolygon` return from `calculate_inner_fit_polygon` (it iterates `ifp.geoms`), so the geometry plumbing accepts main's new return type cleanly.

**Verification:**
- Tier 1: Correctness tests pass.
- Tier 2: Same corpus, verify that no candidate is accepted that crosses a hole boundary (visual check or add a test). The IFP should be *smaller* (more restrictive) for non-convex holes.
- Tier 3: Perf diff may be slightly worse (more ops per IFP) — gate is *neutral or better*; if worse, document the correctness win.

**Why now:** Your IFP is a known correctness gap. Main's fix is the right direction. Do NOT take `_merge_convex_parts` (block 3.1) — your notebook measured it as a ~1.9× regression.

### 2.2 `discretize_wires_to_polygon` dedup + `make_valid`; `_require_area`/`EdgeOnProfileError`; `inv_rotation = rotation.inverted()`
**File(s):** `freecad/nestingworkbench/Tools/Nesting/algorithms/shape_processor.py`.

**From main:** Rewrote the wire→polygon path with:
- `RING_DEDUP_TOLERANCE = 1e-6` + `shapely.remove_repeated_points` on the closing point.
- `make_valid` on the assembled polygon, largest‑polygon selection if split.
- `_require_area` + `EdgeOnProfileError` raised when `poly.is_empty or poly.area < 1e-6`, with a clear message *"it is flat and viewed edge-on. Choose the up direction along its normal."*
- `inv_rotation = rotation.inverted()` replaces a 7‑branch `if up_direction == …` chain.

**What I verified:** Your `shape_preparer.py` calls `discretize_wires_to_polygon` with the saved boundary wires exactly as main does (same call signature, no re‑simplification). Your code already guards against `None` returns. Your engine already guards `MultiPolygon` IFP returns. So the plumbing is compatible.

**CRITICAL:** This rewrite changes the actual polygon objects (vertex counts, winding, validity). It **re-bases all recorded performance numbers**. After this block, re-run the baseline (0.3) and accept the new numbers before proceeding to any perf gate.

**Verification:**
- Tier 1: Correctness tests from 0.2 pass (no invalid polygons, IFP containment).
- Tier 2: Same corpus; re-baseline pack result (sheets, parts, density). The polygon set is different but topologically valid.
- Tier 3: Perf diff against *new* baseline. Do not compare to 0.3's numbers.

**Why now:** This is a strict improvement over OURS' wire handling (dedup eliminates zero‑length edges that cause GEOS "non-noded intersection"/"side location conflict" in the exact mask; `make_valid` fixes winding). It is also a prerequisite for any later block that touches `shape_preparer` or `shape_processor` geometry.

### 2.3 Fill `as_fill` — required Quantity as normal parts, then fill copies
**File(s):** `freecad/nestingworkbench/Tools/Nesting/shape_preparer.py` — `_spawn_factory` and the fill loop in `prepare_parts`.

**From main:** Their `make_instance(as_fill=...)` creates the full `Quantity` as `as_fill=False` instances first, then adds `as_fill=True` instances for the sheet fill. Their comment: *"The quantity is a requirement even when Fill is on… (Marking the required copies as fill parts queued them behind every other part and dropped the ones that no longer fit, so 20 requested circles nested as 12.)"* Your code marks every instance `fill_sheet`; their split prevents that.

**Verification:**
- Tier 1: Correctness tests pass.
- Tier 2: Corpus: verify that a part requested with `quantity=20` and `fill_sheet=True` places all 20 (or as many as fit *before* the fill pass), not silently 12.
- Tier 3: Perf diff against new baseline (this is a correctness/behaviour fix; may change sheet count).

**Why now:** You are on the buggy side. Main's split is the fix.

### 2.4 `_handle_new_master` tuple branch up-direction
**Correction:** Already verified your code uses `'Z+'`, not `part_params[0]`. No action needed. (Drop this from the plan.)

---

## 3. Decisions that must be consciously chosen (not auto-merged)

These are where main and this branch have genuinely opposite choices. The plan makes them explicit blocks so you can decide per your own data.

### 3.1 `_merge_convex_parts` (Hertel–Mehlhorn greedy convex merging) — **DO NOT TAKE**
**File(s):** `freecad/nestingworkbench/Tools/Nesting/algorithms/minkowski_utils.py`.

**The choice:** Main's `_merge_convex_parts` greedily merges convex pieces whose union is still convex (Hertel–Mehlhorn). Your notebook measured: Spacer 192 → 68 pieces, wall time **23.97s → 44.58s** (`make-faster.md:371-377`). The greedy merge *increased* wall time because repeated union + convex-hull checks cost ~39–41 seconds of decomposition. `make-faster.md:2116` says *do not revisit*.

**Your alternative:** Dead-ring pruning (`_prune_dead_rings`) gets 192 → 98 pieces and is faster. Your convex-sum phase (`_minkowski_sum_convex_linear`) is O(n+m) pure-Python edge merge + batched `from_ragged_array` build, with per-pair validity/emptiness/area gates and a reference fallback — all of which main drops.

**Decision:** Explicitly **do not take** this. Leave the dead-ring block as-is. If anyone later wants to re-experiment with convex merging, it can be a separate PR against a re‑based branch, not part of this series.

### 3.2 Lazy vs eager NFP precompute
**The choice:** Main eagerly fills `Shape.nfp_cache` with every NFP the run can request, before generation 1 starts, via `_precompute_all_nfps(enumerate_nfp_jobs(parts))` on a 2-thread pool. You made NFPs lazy on demand inside `find_best_placement`.

**Why it matters:** Main's eager precompute **drains your in-flight NFP dedup and transformed-piece caches** before the run even starts. Their `enumerate_nfp_jobs` dedupes by *instance* label while the engine keys by *type* label — a latent mismatch that only works while those coincide.

**Decision:** This series **does not** port main's `_precompute_all_nfps` or the parallel worker path (blocks 4.1–4.2 deliberately skip it). If you want to measure eager vs lazy, add a dedicated benchmark block (see §7, Phase 5) after the geometry blocks are stable, using the characterisation harness from 0.2.

### 3.3 Rotation parallelism: ThreadPoolExecutor vs serial
**The choice:** You keep a per-placement `ThreadPoolExecutor` + `NESTING_ROTATION_WORKERS` knob; main deleted it, claiming *"serial evaluation is 2.4–3.3x faster"* (their benchmark). Your own `make-faster.md:2114` lists *"test four rotation workers versus eight"* as still outstanding.

**Decision:** Do not adopt main's serial loop in this series. If you want to test the claim, add a dedicated block after the geometry stabilises (see §7, Phase 5) that runs the 4-vs-8 experiment on the same corpus, with both paths instrumented. Keep your executor functional for now.

### 3.4 `type_label` in cache keys vs instance labels
**The choice:** Main keys NFP/candidate caches by part *type* (`Shape.type_label`, a new property) rather than instance label. You key by instance label.

**Decision:** Port `Shape.type_label` + the three key-site switches (`_nfp_cache_key`, `candidate_cache_key`) as block 3.1 (below). This is strictly more NFP-sharing (covers parts with no `source_freecad_object`) and costs nothing on your side — the only edit is replacing two inline key constructions. Do NOT switch the dead-ring probe keys (`convex_pair_probe_keys`) — those are keyed on `master_poly1.wkb + angles + piece indices`, not labels, and are immune to this change.

---

## 4. Latent defects in this branch that the merge exposes

These are bugs your branch has that main's commit path brings into sharper relief. They are not "main's fault" but they make a naïve merge uncomfortable.

### 4.1 Document-level `MasterShapes` group orphaning on commit/re-nest
**File(s):** Your `_get_shared_master_group()` creates a document-level group `"MasterShapes"` not under any layout. `nesting_job.commit()` (identical on both sides) finds masters via `next(c for c in self.temp_layout.Group if c.Label.startswith("MasterShapes"))`. Since yours is not a child of the layout group, on commit the old master row is neither replaced nor deleted, and the new masters are orphaned outside any layout.

**Why it matters:** Main's `hide_all_master_shapes(doc)` only enumerates `Layout*` groups, so your pooled doc-level group is never hidden → the doubled, misaligned master outline main fixed in this release.

**Fix (own branch, not main's):** Either (a) make the shared group a child of the current layout group when `create_doc_objects=True`, or (b) on commit, explicitly delete the old master row and re-parent the new ones. This is a your-branch fix, not a main-port.

### 4.2 `trial_callback` arity 4 vs 5
**File(s):** `nesting_logic.py` lambda (`4` args) vs `nesting_strategy.PlacementOptimizer` call (`5` args including `sheet`).

**Why it matters:** Mixing your `nesting_logic` with main's `nesting_strategy` raises `TypeError` inside the per-rotation `except` → placements silently stop happening. No crash, no obvious log.

**Fix:** Either adopt main's 5-arg `trial_callback` (and update `find_best_placement` accordingly) **or** keep your 4-arg and keep main's `nesting_strategy` out. This series adopts main's `nesting_logic` wholesale (including the relay FIFO and 5-arg callback) in block 5.1.

### 4.3 Your `_handle_new_master` tuple branch uses quantity as up-direction
Already verified your code uses `'Z+'`. No issue.

---

## 5. Landing order — one commit per block

Recommended order (nothing depends on a later block; any block is droppable):

1. **0.1** — Harvest test suite from `inmternal-overlap-issue`, adapt, commit as `0.2-tests`.
2. **0.2** — Record baseline numbers (corpus + seed + perf dump); commit as `0.3-baseline`.
3. **1.1** — `_MainThreadRelay` FIFO + `_draining` guard (`nesting_logic.py`).
4. **1.2** — `_retire_worker()` (`nesting_controller.py`).
5. **1.3** — `compute_and_cache_nfp` None-master guard + per-hole failure isolation (`minkowski_engine.py`).
6. **2.1** — IFP `e ⊕ −P` hole-edge sweeps (`minkowski_utils.py`).
7. **2.2** — `discretize_wires_to_polygon` dedup+`make_valid`; `_require_area`/`EdgeOnProfileError`; `inv_rotation` (`shape_processor.py`). **Re-baseline numbers after this.**
8. **2.3** — Fill `as_fill` split (`shape_preparer.py`).
9. **3.1** — Port `Shape.type_label` + three key-site switches (`shape.py` + `minkowski_engine.py` + `nesting_strategy.py`). **Do NOT** take `_merge_convex_parts` or the numpy `minkowski_sum_convex`.
10. **3.2** — `is_known_infeasible` + `count_skipped_rotations` + `rotations_skipped` stat, wired into **your** `find_best_placement`, preserving your `_cand_cache` early-return and your `ThreadPoolExecutor`.
11. **4.1** — Fix doc-level `MasterShapes` group orphaning (your branch).
12. **4.2** — Adopt main's 5-arg `trial_callback` + `_MainThreadRelay` rewrite (blocks 1.1 + 5.1 already landed, so this is just the callback arity change in `nesting_logic.py`).
13. **5.1** — Port main's `nesting_logic.py` wholesale (relay + 5-arg trial_callback + `_find_master_container_for_part` → `getattr(part, 'master_container', None)`). This is the "take main's nesting_logic" block — everything else in it (the `_MainThreadRelay`, `nest()`, `fill_existing_sheets()`, `_hide_sim_outlines`, `_teardown_sim`) is main-only, your version is superseded.
14. **5.2** — Port main's `ga_coordinator.py` structures that are compatible after 3.1–3.2: `enumerate_nfp_jobs` as a *counter only*, `ga_snapshot.SnapshotShape` (for the parallel path, if you later decide to add it), `ga_worker.py` init/ping/nest (if you add the parallel path). **Do not** port `_precompute_all_nfps`, the ProcessPoolExecutor path, or the parallel generation loop — those contradict your lazy-NFP spine.

**After block 5.2:** You have a functional integration. Run the full Tier 1–3 suite. If all green, you can either (a) push this as a PR/merge candidate, or (b) continue with the optional blocks below.

**Optional post-5.2 (not required for the plan, but the door is open):**
- 5.3 — Add a dedicated benchmark block (Phase 5) that compares your lazy NFP + dead-ring pruning against main's eager precompute + serial rotation, on the same corpus, to finally settle the "which is faster" question.
- 5.4 — If you want main's GA presets, compactness default `1.0`, and the `simulate_nesting`/`add_labels` default flips, those are product decisions; they go in a separate series because they invalidate baselines.

---

## 6. What this plan deliberately does NOT include

| Category | Reason |
|---|---|
| UI restructure, i18n, About dialog, ManualNester Qt rework, Silhouette/Exporter DXF fixes | Orthogonal, zero overlap with your perf work, zero speed value here. These belong in a real merge or an upstream PR later. |
| `_precompute_all_nfps`, `ga_snapshot.py`, `ga_worker.py`, ProcessPoolExecutor path | Contradicts your lazy-NFP spine; needs `member_idx`; unusable until 2.1 lands and the parallel path is a deliberate opt-in. |
| `_merge_convex_parts` (greedy convex merge) | Your notebook measured 23.97s → 44.58s regression; explicitly **not** taken. |
| Main's numpy `minkowski_sum_convex` | Asymptotically different; your O(n+m) edge merge is algorithmically better; wall-clock is genuinely unproven. Benchmark separately (see 5.3). |
| Deleting your rotation `ThreadPoolExecutor` + `NESTING_ROTATION_WORKERS` | Your own notes list the 4-vs-8 experiment as outstanding; run it separately if you want to contest main's 2.4–3.3× claim. |
| GA presets, compactness default `1.0`, `simulate_nesting`/`add_labels` default flips | Product decisions; invalidate baselines; keep out of this series. |
| Any file outside `freecad/nestingworkbench/Tools/Nesting/` or `freecad/nestingworkbench/datatypes/` | Outside scope. |

---

## 7. Per-block verification protocol

After each block is applied, run these three tiers. Only if **all three pass** does the next block get applied.

### Tier 1 — Pure geometry, no FreeCAD (unit tests from the harness in 0.1)
- Run every test file from the harvested suite.
- Assert: no `is_valid == False`, no `is_empty == True` polygons, IFP containment holds, convex-piece counts within expected ranges.
- Pass requirement: **100% pass** (0 failures).

### Tier 2 — Geometry invariants, with FreeCAD (fixed corpus + fixed seed)
- Run the same corpus (`n70-intercooler-spacer-bottle-nesting.FCStd` or equivalent) with the exact same seed.
- Capture:
  - Pack result: number of sheets, total placed parts, pack density, `hole_pct`.
  - Any `TypeError`, `ValueError`, or crash during nesting.
  - Whether the result is identical or *better* (more parts placed, fewer sheets) than the pre-block baseline.
- Pass requirement: **no crashes**; pack result is **neutral or better** (same or more parts, same or fewer sheets).

### Tier 3 — Perf diff against the latest baseline
- Diff the full `[GA PERF]` + `[LAYOUT PERF]` dump against the baseline recorded in 0.3.
- Pass requirement: **neutral or better** (no counter increases, no new warnings). If a block intentionally changes a counter (e.g. 2.3 Fill changes sheet count), the doc must note the expected delta and the other counters must be neutral or better.

**Gate decision:** Only proceed to the next block if Tier 1–3 all pass. If any tier fails, revert the block (`git revert <sha>`), document the failure reason, and either (a) skip that capability, or (b) iterate a smaller sub-block until green.

**Commit message template (after each block):**
```
Ported-from: main@eac1e30 (<function/file>)
Capability: <one-line description>
Before: <brief before-numbers or "see baseline">
After: <brief after-numbers or "see new baseline">
Independently revertable: yes
Tier: 1=pass 2=pass 3=neutral-or-better
```

---

## 8. Next step

1. Create a new integration branch off current HEAD:
   ```bash
   git checkout -b integrate-main-2026.9 cc57e25
   ```
2. Apply 0.1 (harvest test suite from `inmternal-overlap-issue`), adapt paths, commit as `0.2-tests`.
3. Run Tier 1; fix any failures.
4. Proceed block by block per the order in §5, running Tiers 1–3 after each.

**Do not skip 0.1.** The characterisation harness is your safety net. Without it, you are merging blind.

---
*This plan was generated from a comparative analysis of the working branch vs. main@eac1e30, with load-bearing claims verified against the on-disk git state. Blocks marked "DO NOT TAKE" are explicitly excluded; all others are individually revertable.*