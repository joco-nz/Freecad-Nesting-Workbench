# Collision-check defect — investigation status

**Living status document.** Its purpose is to survive compaction and model
changes: update it at the end of every step (mark steps done, record findings,
adjust the next move). **Defect is now numbered: NEST-034** in `issues.md`
(summary-table rows for NEST-031…034 added too).

Last updated: **2026-10-08** — verdict reached (see below); now running
**recommendation #2** (simplification A/B) combined with the user's
**Stop At Sheets = 1** workflow — batch 1 in flight, see Next move item 0.
Previous: next-move steps 1–5 all done; probe rewritten, self-tested
(27 checks), run twice (run 1 exposed a probe bug in L3a — double-buffering
already-buffered polygons — fixed; run 2 clean, exit 0, 69.4 s).
**Verdict: L1 = L2 = L3a = 0, L3b = 54 → classification 3, polygon-source
divergence (hypothesis e) confirmed.** NEST-034 recorded in `issues.md`.

---

## Objective

Explain and fix a genuine packing defect: two parts are placed closer than
`PartSpacing` allows and the collision check does not reject them. The active
work is **step 3a**, an instrumented nest run that classifies the failure as
one of:

1. **detected-but-wrongly-accepted** (check runs, verdict wrong),
2. **never-detected** (check never runs on the offending pair, or runs on wrong
   geometry — stale cache / screen bug), or
3. **polygon-source divergence** (mask polygons are clean; the real committed
   geometry overlaps — tessellation/simplification shortfall).

## The defect (measured, confirmed genuine)

- `PartSpacing = 3.0`; every outline is buffered by `spacing/2 = 1.5 mm` per
  part, so required centre-to-outline clearances are met iff buffered outlines
  are disjoint.
- Two parts in the committed adopted fixture sit at a **1.9979 mm gap** → their
  buffered outlines overlap **2.6008 mm²**. Shortfall vs the required 3.0 mm is
  1.0021 mm. The check should reject; it does not.
- Verified already: buffer is really applied (area +26.61%);
  `original_polygon == polygon == final_buffered_polygon`; the mask's collision
  check uses the buffered polygons; tessellation-vs-slice area discrepancy is
  0.57% — arithmetically insufficient *by area* to explain the overlap (local
  vertex-wise error is still live as hypothesis **e**).

## Hypothesis board

| id | hypothesis | state |
|----|------------|-------|
| a | check runs on unbuffered polygons | **eliminated** (check uses buffered) |
| b | offending pair skipped by the pair loop | **eliminated** |
| c | tolerance misuse (area ≤ 1e-7 accepted) | **eliminated** (overlap is 2.6 mm²) |
| d | a bypass path never reaches the mask | **eliminated** |
| e | polygon-source divergence: tessellation + simplification (0.3) puts the mask polygon ~0.5–1.0 mm inside real geometry — matches the 1.0021 mm shortfall | **open**, tested by layer 3a vs 3b |
| f | stale/poisoned `CandidateGeometryCache`: run-wide shared, key = `(prefix, float(x), float(y))` with `prefix` containing `id(source)`/`id(part)` — id reuse after GC can return another geometry | **open**, tested by layer 1 cache-vs-fresh WKB compare |
| g | mutation after check: committed polygon differs from tested candidate (`set_rotation`/`move`, gene-angle re-rotation sites) | **open**, tested by layer 2 vs layer 1 |
| h | screen/index bug in the bbox screen (impossible-arithmetic residual class) | **open**, labelled `UNEXPLAINED`/`SCREEN_MISS` in layer 1 verdicts |

## Instrumentation design (locked — three layers)

Probe file: `/tmp/opencode/step3a.py`, output `/tmp/opencode/step3a.txt`
(contract: a final `DONE rc=N` line), console `/tmp/opencode/step3a.console`.

- **Layer 1 — mask-time fresh recheck.** Wrap `PlacementOptimizer._exact_candidate_mask`
  (staticmethod; install via `staticmethod(wrapper)`) after the original
  returns. For every **accepted** candidate: build fresh `translate(rotated_poly,
  …)`, vectorized bbox screen against `sheet.parts` polygons, `intersects`
  prefilter, exact `intersection().area > tol`. Per violating pair log: part id
  + angle (thread-local via `_evaluate_rotation_tracked` wrapper), candidate
  index/x/y, existing part id, overlap area, whether the original built geometry
  for that candidate (`probe['_geometry_candidate_indices']` membership), and
  cache status (WKB compare of `cache.get(_candidate_geometry_key(prefix, x, y))`
  vs fresh → `SAME`/`MISS`/`DIVERGE`). Counters: `mask_calls`,
  `rechecked_calls`, `accepted_candidates`, `recheck_overlaps`,
  `recheck_screen_miss`, `recheck_cache_diverge`, `recheck_cache_miss`;
  details capped `MAX_DETAIL = 40`.
- **Layer 2 — commit-time pair check.** Wrap `Nester._attempt_placement_on_sheet`
  (1666): after a truthy return, verify `sheet.parts` grew by exactly 1, then
  pair `sheet.parts[-1].shape.polygon` against `[:-1]` (bbox screen + area).
  Counter `commit_overlaps`; a growth mismatch is itself counted.
- **Layer 3 — post-run dual scan** (after `GACoordinator.run` → `job.commit()` →
  `doc.recompute()`):
  - **3a (mask space):** pairwise on `job.sheets[].parts[].shape.polygon` — the
    final layout as the mask saw it. Counter `l3a_overlaps`.
  - **3b (slice space):** `resolve_layout_group` → `get_sheet_groups` →
    `flatten_sheet` → `part_footprint` buffered by `spacing/2`, pairwise +
    `tightest_part_gap` per sheet. Counter `l3b_overlaps`.

**Verdict logic:** L1 > 0 → classify per violation (`cache=DIVERGE` → hypothesis
f; `screen=OUT`/built-geometry absent → h; cache `MISS`/`SAME` with fresh
overlap → g-adjacent "accepted against identical evidence"). L2 > 0 with L1 = 0
→ mutation after check (g). L3a > 0 with L1 = L2 = 0 → check failed on final
layout. L3b > 0 with L3a = 0 → **polygon-source divergence (e)**. All clean but
gap < spacing → defect did not reproduce (see seed caveat below).

## Constraints that must not regress

- Lead-in work is frozen: `StyleIn/Out`, `AngleIn/Out`, `RadiusIn/Out`,
  `InvertIn/Out`, `ExtendIn/Out` immutable; only `StartPoint` movable with
  `UseStartPoint=True`; margin = tool radius; sheet-edge margin = tool
  diameter; report-only commands write nothing; unfixable → `CONFLICT_` prefix
  + `LeadInConflict`; fixes land only on the replayed job. (Phase 1/2/3 done:
  183 harness checks, 647 pytest green.)
- **Fixture files are never overwritten.** `replay-fixture-CAM-Nested.FCStd` is
  MODIFIED-uncommitted (the adopted 54-part fixture) — do not revert or
  regenerate it casually. Baseline `facdb4d` restores `replay-fixture-CAM.FCStd`.
  The probe opens the source document read-only and **never saves**.
- Guard against infinite loops during investigation.
- Seed via env `NESTING_RANDOM_SEED=1234` set at start of `run()`, restored in
  `finally`; must **not** set `algo_kwargs['random_seed']`.

## Findings — instrumented run 1 (2026-10-08, exit 0, 72.4 s)

Full report: `/tmp/opencode/step3a.txt` (superseded by run 2's file after the
L3a fix below; console `/tmp/opencode/step3a.console`).

- **Liveness healthy**: 2,162 mask calls, 1,998 commits (growth always +1),
  probe totals normal (132,240 candidates, 123,077 exact checks), 0 spy
  errors. Run shape: **2 sheets, 54 parts** at seed 1234/direction 45 (not the
  fixture's 1-sheet arrangement).
- **L1 = 0** over 122,267 accepted candidates / 113,104 exact rechecks →
  **hypotheses f (stale cache), h (screen bug), and decision divergence are
  eliminated on the live path**: every accepted candidate's fresh geometry is
  disjoint from everything on its sheet.
- **L2 = 0** over 1,998 commits → **hypothesis g (mutation at commit)
  eliminated**: committed polygon == tested candidate at commit time.
- **L3b = 54 pairs** with buffered footprints overlapping > tol, areas
  ≤ 10.5 mm², **tightest raw gap 2.7526 mm < spacing 3.0** (pair
  `part_BottomStrap_26 / part_TopStrap_8`) → **the defect class reproduced**
  in this run's nest.
- **L3a run-1 numbers (91 pairs, ≤742 mm²) were MY bug, not the
  workbench's**: `_l3a_scan` buffered polygons that are *already*
  spacing/2-buffered (`shape.py:90`), setting an effective 6.0 mm threshold.
  Tells: pair count 91 (raw < 6.0) vs L3b's 54 (raw < 3.0), and L3a's
  "implied raw gap 3.0000" against L3b's true 2.7526. **Fixed**: pair the
  polygons as-is; self-test + syntax re-validated; run 2 launched.

**Prediction before run 2**: L3a (corrected) should read 0 while L3b reads 54
→ per the verdict rules that is **L3b > 0 with L3a = 0 → hypothesis e
(polygon-source divergence)** — the mask's buffered polygons are pairwise
disjoint while the real tessellated footprints sit closer than spacing
allows.

## Findings — instrumented run 2 (2026-10-08, exit 0, 69.4 s, corrected L3a)

Report: `/tmp/opencode/step3a.txt` (current), console `/tmp/opencode/step3a.console`.

**Prediction confirmed exactly:**

| layer | run 1 | run 2 (L3a fixed) |
|---|---|---|
| L1 recheck_overlaps | 0 (of 122,267 accepted / 113,104 exact) | **0** (identical) |
| L2 commit_overlaps | 0 (of 1,998 commits) | **0** (identical) |
| L3a mask-space overlaps | 91 *(probe bug: double buffer)* | **0** (2 sheets, 54 parts) |
| L3b slice-space overlaps | 54 | **54**, tightest raw gap **2.7526 mm** < 3.0 |

- Same run shape both times (2 sheets, 54 parts; 2,162 mask calls,
  1,998 commits; probe totals byte-identical — deterministic under seed 1234).
- **Verdict per the locked rules: L3b > 0 with L3a = 0 → hypothesis e
  (polygon-source divergence) CONFIRMED.**
  - L1 = 0: every accepted candidate's fresh, cache-bypassed geometry is
    disjoint from its sheet → **f, h, and decision divergence eliminated**.
  - L2 = 0: committed polygon == tested candidate at commit time → **g
    eliminated**.
  - L3a = 0: the final layout's mask-space polygons are pairwise disjoint
    (tightest contact is an exactly-tangent grazing pair at raw gap = 3.0).
  - L3b = 54: the real tessellated footprints of those same 54 parts overlap
    the spacing buffer in 54 pairs (areas ≤ 10.5350 mm²), tightest raw gap
    **2.7526 mm** (`part_BottomStrap_26 / part_TopStrap_8`) → shortfall
    **0.2474 mm** below spacing 3.0 in this run's nest.
- Defect reproduced without the fixture's own arrangement (different nest,
  2 sheets vs the fixture's 1-sheet layout) → the divergence is systematic,
  not a one-off of the adopted fixture's seed.
- Seed caveat stands: adopted fixture's real seed unknown; seed 1234 is
  best-effort reproduction (it reproduced the *class* of defect anyway).
- Cosmetic note: verdict line 1 of run 2's report ("the post-run scan above
  found one") was worded for the L3a>0 branch and reads oddly when L3a=0;
  line 2 states the correct conclusion. Probe wording only, no re-run needed.

## Completed milestones

1. Phase 1 (detection), Phase 2 (lead-in fix), Phase 3 (progress/cancel) —
   complete and green.
2. Defect proven genuine (measurements above); hypotheses a–d eliminated.
3. Baseline `facdb4d` for `replay-fixture-CAM.FCStd`; pinned snapshot
   `/tmp/opencode/snap.FCStd` (md5 `3c3786d4…`).
4. Fixture provenance: `make_nested_fixture.py` constants are **stale**
   (spacing 4.0 / direction 90 / 23-23-2 = 48 parts, seed 1234) vs adopted
   fixture (54 parts, spacing 3.0, direction 45, 26/26/2). Adopted-fixture seed
   unknown → **seed-provenance caveat**: seed 1234 may not reproduce the defect;
   report that honestly if the run comes back clean.
5. Full API recon, re-verified 2026-10-08 (line numbers below).

## Next move

1. ~~**Write `/tmp/opencode/step3a.py`** from scratch~~ **[DONE 2026-10-08]**
   — rewritten from scratch; requirements below kept for the record:
   - Harness pattern: module `_LOG` + lock-protected `e()`, top-level
     `try: rc = run() except: e(traceback.format_exc())` → `DONE rc=N` →
     `_LOG.close()` → `sys.exit(rc)`.
   - Open source doc **first**, then import workbench modules
     (`GACoordinator` only after `FreeCAD.openDocument` — bare run segfaults
     3/3); `NESTING_RANDOM_SEED` set/restore around the run.
   - Install all three layers; never-raise spies (log own tracebacks; original
     exceptions propagate — rotation-eval callers swallow them at
     `nesting_strategy.py:544/565`, so a silent probe failure would be
     indistinguishable from a clean run; `mask_calls` proves liveness).
   - Flow: params (adopted fixture: spacing 3.0, direction 45, 26/26/2,
     generations 4, population 10, rotation steps 4, deflection 20/200,
     simplification 0.3, sheet 600×300×2) → `GACoordinator.run` → `commit()``
     → `doc.recompute()` → layer 3a → layer 3b → `_report()` with counters,
     probe totals, reading guide, verdict.
2. ~~Syntax-check~~ **[DONE 2026-10-08]** `ast.parse` passes; plus the
   synthetic self-test noted in the header.
3. ~~Run~~ **[DONE 2026-10-08]** run 1 (exit 0, 72.4 s) exposed the L3a
   double-buffer probe bug (see Findings run 1); L3a fixed (polygons paired
   as-is) and self-test re-validated.
4. ~~Read `/tmp/opencode/step3a.txt`, classify~~ **[DONE 2026-10-08]** run 2
   (exit 0, 69.4 s): **L1 = L2 = L3a = 0, L3b = 54 → hypothesis e
   (polygon-source divergence) confirmed**; see Findings run 2.
5. ~~Report findings + assign NEST-034~~ **[DONE 2026-10-08]** — NEST-034
   recorded in `issues.md`; findings reported to the user.

### Findings — batch 1 (2026-10-08, 5 runs, all exit 0)

**Simplification A/B (seed 1234, only `simplification` changed):**

| | sim 0.3 (runs 1-2) | sim 0.0 (batch 1) |
|---|---|---|
| L3b violating pairs | 54 | **0** |
| tightest raw gap | 2.7526 mm | **3.0000 mm** (sub-tolerance contacts only) |
| L1 / L2 / L3a | 0 / 0 / 0 | 0 / 0 / 0 |

**Verdict: recommendation #2 answered — simplification is the cause.** With
simplification off, mask space and slice space agree exactly and every pair
respects spacing; with it at 0.3, the mask lies inward by up to 0.2474 mm and
54 pairs breach. `shape_processor.py:24/98` documents simplification as a
**mm tolerance applied early** (Douglas-Peucker; the shortfall 0.2474 ≤ 0.3
fits the DP bound, and buffer() is 1-Lipschitz in Hausdorff distance so the
buffered outlines inherit the same ≤ tol per-outline error, ≤ 2×tol on a
pair). Runtime cost of sim 0.0: 235 s vs ~100 s parallel-contended (~2.3×).

**1-sheet hunt (target_sheets=1, fixture-faithful):** seeds 1, 2, 3, 4, 5,
6, 7 all returned **2 sheets, "target: not reached"** — as did seed 1234
(runs 1-2 shape) and the sim0 run. 9/9 misses, with suspiciously convergent
outcomes (6 of 8 seeds → the identical 2.7526 mm tightest gap). Root-caused
by the **user's own UI run** (see batch 2 below): three harness params and
the direction were wrong.

### Findings — batch 2: the user's real UI run (2026-10-08)

The user nested `replay-fixture-CAM.FCStd` through the UI and got the
**1-sheet result**, with the full log and parameters:

- **Seed 1892920427** (`GA random seed: 1892920427 (random (not pinned))`) —
  the previously-unrecoverable seed, now known for one real run.
- **Rotation Angle = 225°** — confirms the direction finding: the dial stores
  45 (CCW from 6 o'clock), `dial_to_bearing` (`constants.py:96`) maps it to
  bearing 225, and the probe had been feeding 45° as a compass bearing —
  **exactly the opposite diagonal** — for every run so far. Fixed in the
  probe (`angle = radians((270 - NESTING_DIRECTION) % 360)`).
- **Candidate step = 1.00 mm** (probe omitted `step_size` → fell back to
  `base_nester.py:24`'s 5.0 default; the engine discretizes candidate rings
  on this grid — `minkowski_engine.py:358/372/424` — so 5.0 is 5× coarser
  than the user's runs).
- **Compactness = 0.30** (probe passed 0.0; read from ui_params at
  `ga_coordinator.py:980/1306` — different fitness weighting).
- **Rotation threads = 4** (probe omitted → auto).
- Everything else matched: Minkowski, 600×300, spacing 3.0, curve 20,
  Simplify 0.30, gens 4, pop 10, Stop At Sheets 1, 26/26/2, rotation
  steps 4, candidate geometry cache on.
- Their run: **1 sheet, 54 placed, 83.4% efficiency, GA 11.55 s, target
  reached early** (single log line: "Target reached: all parts placed on
  1 sheet(s) (target 1) — stopped early").

Probe now defaults to these exact values; **faithful A/B launched**:
seed 1892920427, direction 225, step 1.0, workers 4, compactness 0.3,
target 1 — run A @ sim 0.3 (`step3a_faithful.txt`), run B @ sim 0.0
(`step3a_faithful_sim0.txt`).

### Findings — faithful run A (sim 0.3, seed 1892920427) [DONE]

Report `/tmp/opencode/step3a_faithful.txt`, exit 0, 29.4 s:

- **1 sheet, 54 parts, target MET** — early stop fired after the first
  layout (54 commits, 214 mask calls vs 2,162 in the 4-gen runs).
- **Tightest raw gap 1.9979 mm — the exact original defect** (shortfall
  1.0021 mm, the committed fixture's number to 4 decimals). The "unknown
  seed" caveat is now moot: faithful *parameters* reproduce the headline
  defect without needing the fixture's seed (and earlier evidence that 6 of
  8 seeds gave an identical 2.7526 suggests tight pairs are structural to
  the params, not the seed — old params → 2.7526, faithful params → 1.9979).
- L1 = 0 (61,666 accepted rechecked), L2 = 0, L3a = 0, **L3b = 56 pairs**
  → hypothesis e confirmed on the user's own configuration.
- **Bound violated: 1.0021 > 2×0.3.** Simplification alone (DP tol 0.3,
  ≤ 0.6 mm on a pair) cannot explain the committed defect — a second
  error source compounds it. This is the open question recommendation #1
  exists to answer.



**Resolved by faithful run A:** the *committed* fixture's shortfall is
**1.0021 mm** (gap 1.9979); today's old-param sim-0.3 runs max out at 0.2474
and the DP bound for tol 0.3 is 0.6 — 1.0021 does not fit. Faithful run A
(exact user params, simplification 0.3 as stored) **reproduces 1.9979
exactly**, so the fixture was NOT nested at a different simplification: a
second error source compounds DP error (deflection/tessellation, buffer join
discretization, hole nesting — `mask_hole_exploiting_placements`=26,299,
holes are simplified too). Also flag: the **UI default simplification is
1.0 mm** (`ui_nesting.py:113`, prefs default 1.0) → DP bound 2.0 mm
shortfall for users on defaults.

### Findings — faithful run B (sim 0.0, same seed/params) [DONE]

Report `/tmp/opencode/step3a_faithful_sim0.txt`, exit 0, 465.5 s:

- **L3b = 0, tightest 3.0000 mm** → defect eliminated on the faithful
  configuration too. Both A/B pairs (seed 1234 old params, seed 1892920427
  faithful params) agree: **simplification is the switch**.
- **But: 2 sheets, target NOT reached, 465 s vs A's 29 s (16×).** Exact
  geometry changes the packing: A's first layout fit 54/1 sheet only
  because the simplified mask saw space that isn't there (56 violations);
  with truthful geometry the search never found a legal 1-sheet pack in
  4 generations. **The user's 1-sheet success is, at least in part, the
  defect working** — whether a *legal* 1-sheet packing exists for this mix
  at spacing 3.0 is now an open question (7 seeds + this run = 0 legal
  1-sheet packs; the committed fixture's own 1-sheet pack breaches by
  1.0021 mm).
- Clean runtime attribution comes from the OLD-param A/B (same seed, both
  full 4 gens): 235 s @ sim0 vs ~100 s @ sim0.3 → **~2.3× per-run cost for
  exact geometry**, before accounting for early stop.

### User's second manual test + batch 3 [DONE]

Same params as the faithful run **except `simplify=0.00` and Candidate step
= 0.10 mm** (was 1.0): seed **3355455059**, **1 sheet, 54 placed, 83.8%
efficiency, target reached, GA 8.91 s**. Two things this changes:

- **A legal 1-sheet pack probably exists.** Simplification off means the
  mask geometry is truthful, so if the pack passes L3b it is a *legal*
  single sheet — answering the open question from faithful B (which failed
  to reach 1 sheet at sim 0.0 + step **1.0**). The candidate step is the
  suspected difference: 0.1 mm is a 10× finer placement grid, so faithful
  B's failure may have been the coarse step, not exact geometry.
- **The "sim 0.0 is unaffordable" worry needs revisiting**: their GA found
  the pack in 8.91 s (early stop), not the 465 s faithful B spent grinding
  4 generations at step 1.0.

Batch 3 (both @ seed 3355455059, target 1, direction 225, compactness 0.3,
workers 4):
- **C = the user's exact revision** (`step3a_c_fine_sim0.txt`): sim 0.0,
  step 0.1 → does L3b = 0 (legal) and target MET? Also the runtime with
  L1 instrumentation.
- **D = isolation twin** (`step3a_d_fine_sim03.txt`): sim **0.3**, step 0.1
  → same seed/step, only simplification differs from C. If D shows violations
  (or the 1.9979 pattern) while C is clean, simplification's effect is
  isolated from the step change; also shows whether the defect's magnitude
  depends on the step grid.



### Findings — run C (user's exact revision: sim 0.0, step 0.1) + clamp hunt [DONE]

Report `/tmp/opencode/step3a_c_fine_sim0.txt`, exit 0, 512.9 s:
**2 sheets, target NOT reached** (ran all 4 gens, 2,054 commits),
L1 = L2 = L3a = 0, **L3b = 0, tightest 3.0000** — exact geometry is clean,
but the harness did **not** reproduce the user's 1-sheet/8.91 s result
despite identical seed and parameters.

Suspects and findings:

- **UI clamp (likely explanation of the config delta):** the simplification
  field is `LengthField(mm_min=0.001, ...)` (`ui_nesting.py:850`) and
  `_clamp` (`length_field.py:229-235`) raises typed values below the bound
  → the user's typed "0.00" almost certainly **ran as 0.001 mm** (canonical
  value 0.001, displayed "0.00" at 2 decimals). Run C used a true 0.0.
  Probe passes `ui_params['simplification']` through the same path as the
  controller (`nesting_controller.py:1067`), so wiring matches; only the
  value differs.
- **Harness determinism, unproven until C2:** if the repeat run flips to
  1 sheet, the difference is run-to-run nondeterminism (4 rotation threads,
  hash ordering) and the user's 1-sheet was the same coin flip.

Legs launched:
- **C2** (`step3a_c2_repeat.txt`): byte-identical repeat of C →
  determinism check.
- **F** (`step3a_f_sim001.txt`): sim **0.001** (the clamped UI value),
  step 0.1, same seed → if this yields 1 sheet while C/C2 stay at 2, the
  clamp + search sensitivity explains the user's run.

### Findings — run F (clamp value: sim 0.001, step 0.1) [DONE]

Report `/tmp/opencode/step3a_f_sim001.txt`, exit 0, 140.2 s:
**1 sheet, target MET** (54 commits — first layout again) — reproduces the
user's UI result on their field's *actual* canonical value. L1 = L2 = L3a = 0,
**L3b = 1 pair, tightest 2.9998 mm** (shortfall 0.0002 = 0.2 µm — inside the
0.001 DP pair bound of 0.002, i.e. a tolerance-boundary graze, not a real
breach; the pack is legal for practical purposes).

Interim: user's typed "0.00" → clamped 0.001 → different search trajectory
than true 0.0 (run C: 2 sheets) → their 1-sheet result.

**C2 verdict [DONE]:** byte-identical repeat of C — 519.4 s, **2 sheets,
same 2,156/2,054/871,107 counters, L3b 0, tightest 3.0000** (only wall
time differs). **The harness is deterministic**, so every same-seed A/B in
this investigation is a controlled experiment, and the C-vs-F delta is the
0.0/0.001 configuration itself: **the user's "Simplify 0.00" ran as 0.001
(mm_min clamp) and that is exactly why their UI found the 1-sheet pack our
true-0.0 run never did.** Notably, the workaround depends on the clamp: at
true 0.0 this seed deterministically fails to reach 1 sheet.

### Findings — run D (isolation twin: sim 0.3, step 0.1) [DONE]

Report `/tmp/opencode/step3a_d_fine_sim03.txt`, exit 0, 29.3 s:
**1 sheet, target MET, L1 = L2 = L3a = 0, L3b = 58, tightest 2.7160 mm**
(shortfall 0.2840 — within the 0.6 DP bound this time). Fine step does not
cure the defect: 58 violating pairs with simplification 0.3 regardless of
grid, and the 1-sheet result again arrives only via the lying mask.
(Run C/C2 below supply the sim-0.0 half of the pair.)

### Recommendation (answer to "which of #1 or #3 next") [confirmed by batch 3; point 1 superseded by #1]

> **Superseded note (2026-10-08, after #1 ran):** point 1's "1.0021 > 0.6"
> used a *per-stage* DP bound against a pipeline with **two stacked DP
> passes** (`:98/195` then `:294`) — the correct bound is 4 × tol = 1.2 mm,
> which 1.0008 fits. No second error source exists; drawn geometry tracks
> the unsimplified profile within 0.09 mm total. See
> "Recommendation #1 — stage-wise decomposition" below for the measured
> split, and NEST-034's resolution appendix in `issues.md`.

**#1 next — scoped, then #3.** Reasons, all measured:

1. **The simplification bound is violated**: faithful A's shortfall is
   1.0021 mm but DP tol 0.3 guarantees ≤ 0.6 mm on a pair. ≥0.4 mm comes
   from somewhere else (tessellation/deflection, hole-ring handling,
   buffer join discretization, or simplify running more than once). #3's
   "oversize the buffer by the measured deviation" cannot be sized until
   that split is known — oversizing by 0.3 would NOT have prevented the
   committed defect.
2. **#3's fast option just got expensive**: disabling simplification for
   collision costs ~2.3× runtime (clean A/B) and *loses the 1-sheet
   workflow goal* (faithful B: 2 sheets in 4 gens). That makes
   buffer-oversize or verify-on-accept the live candidates, and both need
   #1's deviation distribution.
3. **#1 is cheap now**: the probe already reads both geometries in-process;
   extend it to log, per simplify call (`shape_processor.py:98`), input/
   output vertex counts + Hausdorff distance, and per violating pair
   decompose the gap error stage by stage (source tessellation → simplified
   profile → buffered mask polygon → drawn footprint).

**Batch-3 amendments (dose-response, all deterministic):**

| simplification | step | sheets | shortfall (worst pair) |
|---|---|---|---|
| 0.3 (faithful A, step 1.0) | 1.0 | 1 | **1.0021** (exceeds 0.6 bound) |
| 0.3 (run D, step 0.1) | 0.1 | 1 | 0.2840 (within bound) |
| 0.001 (run F, step 0.1) | 0.1 | 1 | **0.0002** (~legal) |
| 0.0 (runs C/C2, step 0.1) | 0.1 | 2 | 0 (clean, but no 1-sheet) |

- Shortfall tracks simplification tolerance monotonically → causal
  attribution for the *bulk* of the defect is now a dose-response curve,
  not a single A/B. The **1.0021 > 0.6 anomaly remains layout-dependent**
  (0.3 in run D stayed within bound) — #1 should decompose faithful A's
  worst pair specifically. **[resolved by #1 below: not layout-dependent,
  just a per-stage bound applied to two stacked passes]
- **Determinism proven** (C2 = C on every counter) → all same-seed pairs
  above are controlled experiments.
- **The user's practical workflow already works**: their UI settings
  (step 0.1, Simplify typed 0.00 → clamped 0.001) yield a 1-sheet pack
  that is legal to within 0.2 µm in 8.91 s — and at true 0.0 this seed
  deterministically does *not* find it. Worth noting the UI cannot turn
  simplification off (`mm_min=0.001`) and displays the clamped value as
  "0.00" — the workaround is accidental.
- Runtime worry (from faithful B's 465 s) is early-stop-dominated, not
  inherent to sim 0: F reached the target in 140 s instrumented.

### Recommendation #1 — stage-wise decomposition [DONE 2026-10-08]

Instrument `/tmp/opencode/step3b.py` (+ `step3b_selftest.py`, 14/14 green).
Re-ran the faithful-A nest; decomposition of all **56 violating pairs**
(total shortfall 6.0378 mm), telescope residual 9e-16, Kabsch 54/54 at
2.3e-13, buffer recompute vs stored artifacts at 1.5e-14, mask↔foot match
max centroid offset 1.11 mm.

**Verdict: the defect is 100% simplification — in TWO STACKED DP passes.
The "1.0021 > 0.6 bound" anomaly is resolved: the bound was applied per
stage when two DP stages compose (≤ 4×tol = 1.2 mm on a pair).**

Aggregate over 56 pairs (mm):

| rung | sum | reading |
|---|---|---|
| placement vs unsimplified profile (3−g_p0) | +6.1279 | layout, seen through the pipeline's own unsimplified profiles |
| T_dp1, early DP (`:98/195`) | −3.8443 | DP shrinks outlines → apparent gaps OPEN |
| T_dp2, third DP (`:298`) | 0.0000 | visual-only pass, no collision effect |
| T_mask, buffer + DP-on-buffered (`:294`) | −2.6602 | mask stage opens apparent gaps further |
| T_draw, mask vs drawn solid | +6.4143 | the mask-vs-reality divergence = the defect itself |

- **Drawn geometry tracks the unsimplified profile**: Σ(g_p0 − g_draw) =
  −0.0901 across all 56 pairs (worst pair 0.0013). Profiles at deflection
  0.1 are *marginally conservative* vs the solid slice — not a defect source.
- **Simplification overstates pair gaps by Σ+6.5045** (profile-stage
  +3.8443, buffer-stage +2.6602) — masks passed everything (L1=L2=L3a=0)
  while drawn reality breached by 6.0378.
- **Worst pair CAMPart_760/762 (TopStrap×TopStrap, the committed fixture's
  1.9979)**: draw 1.9979 → p0 1.9992 → p03 2.5688 (early DP **+0.5696**)
  → core 2.5688 → mask 3.0000 (buffer stage **+0.4312**) → shortfall
  1.0021 = 0.5696 + 0.4312 + 0.0013. Both stages within their own 0.6
  pair bound; stacked bound 1.2 ≥ 1.0008 ✓. **No third source.**
- Bound re-check across the dose-response: 4×tol = 1.2 (observed 1.0008),
  1.2 (observed 0.284), 0.004 (observed 0.0002), 0 (observed 0) ✓ all fit.
- T_dp2 = 0 for every pair: `:298`'s simplify of the unbuffered polygon is
  collision-irrelevant (re-simplifying an already-0.3-DP'd ring is a no-op).

**Side finding (measurement caveat + candidate defect):** the fine-deflection
profile (0.01) of **TopStrap** diverges 0.8787 mm from the coarse (0.1)
profile — deterministic, not a centering artifact (probe_bb: identical
bounds, centroid-aligned HD 0.8186, bit-exact repeats), and it *violates the
slice⊆silhouette inequality* (g_fine 3.4895 > g_draw 1.9979 − ε), so **the
fine profile is the wrong geometry**, not the coarse one. `T_true`/`T_defl`
per-pair columns on TopStrap rows are therefore contaminated (their SUM
= 3−g_p0 is registration-free and trustworthy). Also: profile vertex counts
depend on document mesh state (fresh doc 133 verts vs post-nest 260 at the
same deflection, same area). Filed as NEST-035.

**#3 sizing now grounded (measured options):**
1. **Verify-on-accept (recommended)** — L1's plumbing already rechecks every
   accepted candidate; point it at the *unsimplified* profile. Exact to
   within |draw−p0| ≈ 0.01, measured **+43% runtime**, search speed and the
   1-sheet behavior untouched (simplification stays for search).
2. **Conservative mask** — restore each built mask with
   `buffer(2×tol + ε)` (superset of the true buffered outline by the
   1-Lipschitz + DP chain bound): exact legality, zero runtime (one-time
   build), worst-case false-reject conservatism ≈ 4×tol + 2ε ≈ 1.2 mm at
   tol 0.3 — i.e. density cost in tight spots.
3. **Raw buffer oversize** — needs ≥ 4×tol empirically (1.2 mm @ 0.3;
   **4.0 mm @ the panel default 1.0** → spacing would effectively be
   7 mm): usable only if simplification is lowered in lockstep.
Baseline remains: simplification off = exact but ~2.3× runtime and the
1-sheet goal was unreachable (faithful B / runs C+C2).

### Fix implemented — #3 option 1: verify-on-accept [IMPLEMENTED + MEASURED 2026-10-08]

Ordered by the user ("implement #3 as proposed"), option 1 landed:

- **Geometry** — `shape_processor.create_single_nesting_part` builds, once
  per master (3 per run), an **unsimplified twin** of the buffered mask:
  profile re-extracted at simplification 0.0, buffered by the same
  `spacing/2`, same recentering shift → `Shape.verify_original_polygon`
  (pristine build-frame source) + `Shape.verify_polygon` (follows every
  rotation/translation; `set_rotation` re-derives from the pristine source
  exactly as `polygon` ← `original_polygon`, so a part re-placed in a later
  generation cannot double-rotate it). Mirrored through `__deepcopy__`,
  the shape_preparer instance spawn, and the ga_coordinator rebind.
- **Gate** — `_exact_candidate_mask` gained `_verify_stage` at **both**
  exits (empty-sheet gets the bounds leg too): mask-accepted rows are
  re-tested on verify geometry with the mask's *own* predicate (same
  `area_tolerance`, same bounds window). Candidate translated by the
  mask's own `points[i] − rotated_centroid` (registration proven by unit
  smoke with deliberately offset centroids); existing parts read
  `verify_polygon` in sheet frame, per-pair fallback to the mask polygon
  counted as `verify_fallback_pairs`; a part whose verify source *is* the
  mask polygon skips the stage entirely (today's behaviour, free).
  Escape hatch: `NESTING_VERIFY_ON_ACCEPT` (default **1**).
- **Probe counters** — `verify_candidates / bounds_rejections /
  screen_pairs / exact_checks / rejections / fallback_pairs / ms`,
  absorbed into `_perf_stats` with the `KeyError`-safe `.get` pattern
  (never inside a `quiet=True` `+=` on a possibly-missing key). step3a's
  `mask_wrapper` now forwards `**kwargs`.
- **Unit smoke** `/tmp/opencode/test_verify_stage.py` **17/17 green**
  under freecadcmd: both legs, touch-vs-overlap semantics (area 0
  accepted, 0.5 mm² rejected), bounds overhang, registration arithmetic
  (mask-centroid vs self-centroid placements proven to differ, manual
  rigid motion matches), probe counters.

**Three runs + two controls (all exit 0, probe-to-probe):**

| run | config | gate | placed | L3b | tightest gap | elapsed |
|---|---|---|---|---|---|---|
| G1 | faithful A (sim 0.3, step 1.0, seed 1892920427) | on | **8/54**, target not reached | **0** | 3.0000 | **347.2 s** |
| G3 | faithful A | **off** | 54/54, MET | 56 | 1.9979 | 31.4 s |
| pre-fix A | faithful A | — | 54/54, MET | 56 | 1.9979 | 29.4 s |
| G2 | run F (sim 0.001, step 0.1, seed 3355455059) | on | **54/54, MET** | **0** | 3.0000 | 129.0 s |
| pre-fix F | run F | — | 54/54, MET | 1 | 2.9998 | 140.2 s |

- **Control G3 is bit-exact to pre-fix faithful A** on every counter
  (`accepted_candidates` 61,666 = 61,666, commit 54/54, mask calls
  214 = 214) and reproduces the defect (L3b 56, tightest 1.9979) →
  the gate is the *only* behavioural delta; determinism intact. Its
  verify counters read all-zero → the gate is fully off, not silently
  degraded (`fallback_pairs` 0 in G1/G2 as well → verify geometry was
  built everywhere).
- **Defect closed wherever the run completes**: tightest drawn gap is
  exactly 3.0000 mm in both gate-on runs; the L1=L2=L3a=0 ∧ L3b>0
  signature is gone (L3b 56 → 0 and 1 → 0).
- **The user's practical workflow is fully preserved and slightly
  faster**: G2 keeps the full 54-part 1-sheet pack, closes the last
  breach (2.9998 → 3.0000), and runs 129.0 s vs pre-fix 140.2 s.
  Verify there: 88,176 rows entered, **2,906 rejected (3.3%)**,
  34.7 s CPU-summed.
- **G1 fails functionally at sim 0.3**: the gate rejects **99.3%** of
  mask-accepted rows (605,347 collision + 10,128 bounds of 619,639;
  survivors 4,164) → layouts place only 8/54 → the GA never hits early
  stop and the futile retry/fill loops dominate everything (mask calls
  214 → 2,964, candidates 67,655 → 720,930, commit attempts 54 →
  2,664) → 347 s ≈ 11× control. The earlier "+43% runtime, 1-sheet
  untouched" sizing claim held **at low simplification only**.

**Why G1 starves (mechanism, cross-checked against recommendation #1's
decomposition):** the Minkowski NFP proposes *grazing* positions in
**mask space** — pre-fix faithful A's 56 violating pairs ≈ one grazing
contact per placed part, and all of them breached (that *is* the defect;
non-grazing neighbours were legal, hence only 56). At sim 0.3 the mask is
optimistic by 0.05–1.0 mm per pair (stacked DP), so a mask-grazing
candidate has true gap ≤ 3.0 almost surely; any penetration × contact
length ≫ `area_tolerance` 1e-7 → verify rejects it. Generation and
verification therefore disagree on nearly every contact at high
simplification: the gate measures truth correctly, but the candidate set
contains (almost) no positions where truth holds. At sim 0.001 the
optimism is µm-scale and 96.7% of candidates pass → search untouched.

**Consequence — the option-2 pairing is now the follow-up** (filed as
NEST-036): to run simplification ≥ 0.3 *and* honest acceptance,
generation must carry the same margin as verification — oversize the
built mask (`buffer(2×tol + ε)` restore, option 2) or dilate the sampled
NFP by δ ≥ the measured optimism — with verify-on-accept kept as the
exactness backstop (then `verify_rejections ≈ 0` by construction and
verify cost collapses to the intersects screen). Sizing is a
density-vs-strictness policy call (worst case 1.2 mm @ 0.3; 4.0 mm @
panel default 1.0) — **user decision pending**. Until then:
`NESTING_VERIFY_ON_ACCEPT=0` restores today's behaviour bit-exactly (G3),
and the user's clamped simplification (0.001) runs exact *and* complete
under the gate.

**Validation state (2026-10-08, evening):**
- Unit smoke 17/17; pure pytest **647/647** in 13.5 s.
- Harness `tests/freecad_harness/run.sh`: nest-benchmark **GATE PASSED**
  (its sim-0.1 baseline layout is reproduced bit-identically with the
  gate ON → zero verify rejections at 0.1); replay flatten/dressups/
  order/startpoint/boundary suites all green. Two gated failures, both
  isolated by a gate-off control:
  - `test_rotation_determinism` — **caused by the gate**: 6/48 parts
    placed at sim 0.3 (NEST-036 starvation inside the repo's own gate),
    which also makes the seed-differentiation assertions pass
    trivially-false. Control `NESTING_VERIFY_ON_ACCEPT=0` → 48/48 at
    every pool width, all 34 checks + seeded-nest determinism green.
  - `test_tool_clearance` — **pre-existing, gate-independent**: the
    adopted 54-part fixture (MODIFIED-uncommitted, written 12:53, before
    this fix existed) carries NEST-034's own defect (1.9979 mm), and the
    check's `0.8 × spacing` bound detects exactly that. Control: the
    identical single failure with the gate OFF. It clears only when the
    adopted fixture is re-nested legally, which is gated on the NEST-036
    decision ("do not revert or regenerate it casually" still stands).

### Repair-push implemented — NEST-036 fix [IMPLEMENTED + MEASURED 2026-10-09]

Ordered by the user ("progress with your recommendation (repair-push) and
measure"). The verify gate stays exactly as NEST-034 defined it — same
predicate, same tolerance — but a rejection is first offered to a bounded
repair that moves the candidate to a provably legal coordinate; only when
that fails does the row reject, i.e. today's behaviour.

**Mechanism** (`nesting_strategy.py`):

- **Env gate** `NESTING_VERIFY_REPAIR`, default ON, no GUI: the debug/A-B
  audience lives at env level like `NESTING_VERIFY_ON_ACCEPT`. OFF is
  bit-identical to the reject-only gate (proven by control below).
- **Two repair legs, one bounded loop** (`_repair_candidate`,
  `_REPAIR_MAX_ATTEMPTS = 6`):
  - *Bounds*: the position window in which BOTH the verify and the mask
    bbox sit strictly on the sheet is an interval intersection of their
    (different) bounds; the candidate is clamped into it with a 0.01 mm
    edge margin (`_REPAIR_EDGE_MARGIN`). An empty window (part wider than
    the sheet at this angle) rejects.
  - *Collision*: away from the penetration sliver — direction = sliver
    centroid toward the candidate body (hole-safe), depth =
    2·area/perimeter × 1.25 + 0.01 floor, vector-summed over all
    violators (verify leg preferred per partner, mask leg otherwise).
  - Every iteration re-proves BOTH predicates (mask + verify, bounds +
    sheet-difference) at the new coordinate: a clamp re-tests collisions,
    a push re-tests bounds. Final positions are strictly on-sheet, which
    makes the sheet-difference leg structurally true at the reported
    coordinate.
- **Correctness invariants**:
  - Repaired coordinates never touch `points` — `get_incremental_candidates`
    returns the engine's shared cache array. The move leaves through the
    probe under the private key `_verify_repaired`, popped by
    `_evaluate_rotation`, applied to a `score_pts` copy used for scoring
    and best-extraction; geometry-key recording keeps the pristine
    positions (the cached mask geometry was built there).
  - Repair never trades correctness: a kept row's final coordinate passed
    everything downstream re-checks; failure falls back to rejection —
    the pre-repair decision. No probe / no mask geometry → repair skipped
    → reject-only.
- **Counters**: `verify_repairs`, `verify_bounds_repairs`,
  `verify_repair_failures`, `verify_repair_attempts`, `verify_repair_ms`
  (init 0 so a run reports 0 rather than omitting them; absorbed like the
  NEST-034 keys). Bounds rejections count only *final* failures, so the
  reject-only control's numbers stay comparable across A/B.

**The bounds leg was not optional — found by measuring validation**:

- First cut (collision repairs only) took `test_rotation_determinism`
  from 6/48 to 44/48 but still failed. An instrumented replay of the
  test's nest (`/tmp/opencode/probe_rotdet.py`) showed **28 EMPTY-SHEET
  KILLS**, every one the same shape: 4 rows in, 4 rows rejected. On a
  fresh sheet the engine's only candidates are the 4 corner-flush
  positions — exact in MASK space; the unsimplified verify bbox
  overhangs the wall by the DP error (≤ 4×0.3 mm), so the bounds leg
  rejected **every rotation of otherwise placeable parts**
  (TopStrap_20..23 failed even on a brand-new sheet → no sheet-2 append
  → 44/48). After bounds repair: `empty_kills=0`, 48 containers, whole
  test green.
- On faithful A the same leg cut `verify_bounds_rejections` 333 → 3 (the
  3 are genuinely empty-window parts at that angle).

**Measured** (probe `step3a.py`: 54 parts = 2 SimpleSpacer + 26 Bottom +
26 TopStrap, sheet 600x300, spacing 3.0, curve 20, rotation steps 4,
population 10, generations 4, target 1 sheet, 4 workers):

| Run | pre-fix (gate off) | reject-only (gate on) | gate + repair (final code) |
|---|---|---|---|
| Faithful A: sim 0.3, step 1.0, seed 1892920427 | 54/54, L3b 0, tightest **1.9979 mm (the defect)**, 29.4 s | **8/54**, 347.2 s, 605 347 of 619 639 rows rejected (97.7%) | **54/54, 1 sheet MET**, L1=L2=L3a=L3b=0, tightest 3.0000, **114.9 s** |
| Run F: sim 0.001, step 0.1, seed 3355455059 | 54/54, tightest 2.9998, 140.2 s | 54/54, 129.0 s, 3.3% rejected | **54/54**, all 0, tightest 3.0000, **122.5 s** |
| Control: `NESTING_VERIFY_REPAIR=0` on final code, faithful A | — | ≡ G1: 8/54, *every* deterministic counter identical (mask_calls 2964, verify_candidates 619 639, rejections 605 347, same tightest pair), 327.9 s vs G1's 347.2 s (timing noise; only new keys appear, reporting 0) | — |

Repair counters, faithful A: **57 899 repairs** (330 bounds + 57 569
collision), 1 617 failures (= 1 614 collision + 3 bounds rejections),
91 181 attempts (1.58/repair), 261.0 s repair CPU — a subset of
`verify_ms` 328.2 s, i.e. ≈65 s wall of the 114.9 s; NFP+GA+commit
≈33 s wall ≈ pre-fix's 29.4 s. Run F: 2 912 repairs, **0 failures**, all
one-attempt (sub-µm penetration), 9.5 s CPU.

**Honest cost**: on faithful A a *correct* layout costs 114.9 s vs the
pre-fix 29.4 s (3.9×) — the pre-fix number bought its speed with the
1.9979 mm defect. Against the reject-only gate it is 3.0× faster and
places 54 parts instead of 8. Run F is faster than its gate-only
baseline (122.5 vs 129.0 s).

**Validation (final code)**:

- Unit smoke `/tmp/opencode/test_verify_stage.py`: **50/50** — the 17
  NEST-034 checks pinned reject-only (`NESTING_VERIFY_REPAIR=0` before
  import) plus 33 repair checks: success with both predicates proven at
  the new coordinate, trapped candidate bounded rejection, off-sheet
  push rejection, coincident/degenerate rejection, empty-sheet pull-in,
  empty-window rejection, gate-off A/B identity. Two real bugs caught
  pre-measurement: a candidate mask cached across loop iterations (would
  report a clean position computed from the *old* one) and a degenerate
  push vector silently accepting the still-overlapping original.
- Pure pytest **647/647** (11.8 s).
- `test_rotation_determinism` (gate + repair ON): **34/34, status 0** —
  widths 0/4/8/16 all 48/48 at one digest (`a86e7043032993bd`), seeds
  777/778 differ, Physics sections 48/48, env-seeded GA 81.9 % / 1 sheet
  / 48 placed. (Gate-off control: 48 placed at digest
  `97c7950a0360be6f` — the test asserts self-consistency across widths
  and seed differentiation, not cross-config identity; the repaired
  layout legitimately differs.)
- Repair-OFF control ≡ G1 (diff above).
- Harness `tests/freecad_harness/run.sh` (final code): nest-benchmark
  **GATE PASSED** (sim-0.1 fingerprint reproduced → repair provably never
  fires there); replay flatten/dressups/order/startpoint/boundary,
  shape-rigidity, sub-element, tooltip, panel, persistence all green;
  `seeded-nest determinism` 34/0 (the suite this fix was for). Sole
  failure: `test_tool_clearance` 25/1 — the *same* pre-existing
  fixture-content failure documented above (1.9979 mm baked into the
  adopted fixture; gate-off control identical), so overall
  `GATE FAILED` is the known fixture verdict, not a regression.
- The bench GATE fingerprint at sim 0.1 is untouched by construction:
  zero rejections at 0.1 → repair never fires — **confirmed** by the
  GATE PASSED above.

**Not changed**: `test_tool_clearance`'s fixture-content failure (the
1.9979 mm defect baked into the adopted MODIFIED fixture before this
work existed) — re-nesting that fixture remains a pending user decision
("do not revert or regenerate it casually" still stands).

### Next move (post-verdict — localizing hypothesis e)

0. **[IN PROGRESS 2026-10-08] User input + batches 1-2.**
   The user runs
   this sheet size/mix at **population 10, generations 4, Stop At Sheets = 1**
   and reaches a single sheet. Verified against the fixture's `Layout_000`
   object (unzipped `Document.xml`): Algorithm=Minkowski, Generations=4,
   PopulationSize=10, NestingDirection=45, PartSpacing=3.0,
   GlobalRotationSteps=4, DeflectionAngle=20, Simplification=0.3, sheet
   600x300x2, RandomDirection=false, AddLabels=false — **the probe matches on
   every persisted parameter**. Not persisted: TargetSheets (deliberately
   un-persisted, README) and the seed (`ga_coordinator._resolve_seed`: the
   panel supplies none, so the GUI always draws fresh — the fixture's seed is
   unrecoverable). `target_sheets` is **early stop only**
   (`ga_coordinator.py:573/1217/1329` — "an unreachable target simply never
   fires"), so seed 1234's search (which never produced a 1-sheet layout —
   runs 1-2 finished 2 sheets 49+5) cannot be tipped to 1 sheet by the flag;
   reaching 1 sheet needs a seed whose search finds one.
   Probe edits (self-test still 27/27 green): env overrides
   `NESTING_STEP3A_LOG`, `NESTING_STEP3A_SIMPLIFICATION`,
   `NESTING_STEP3A_TARGET_SHEETS` (default 1 = user workflow), `SEED` read
   from `NESTING_RANDOM_SEED`; params line prints effective values; run-shape
   gained a `target: N sheet(s) -> MET/not reached` line; seed caveat reworded
   (fresh-draw provenance, not the stale-script history).
   **Batch 1 (4 parallel freecadcmd, distinct logs):**
   - seeds 1, 2, 3 @ sim 0.3 + target 1 → hunt for the user's 1-sheet
     outcome; a hit = Run A (user-faithful baseline),
   - seed 1234 @ **sim 0.0** + target 1 → B leg of the simplification A/B
     against runs 1-2 (same seed, target cannot fire, so trajectory differs
     only by simplification).
   Then batch 2 = hunt winner @ sim 0.0 (A/B on the 1-sheet layout).

1. **Measure the divergence per-vertex**: for the tightest violating pairs
   (start with `part_BottomStrap_26` / `part_TopStrap_8`, gap 2.7526 vs 3.0),
   compare `Shape.polygon` (mask geometry) against the flattened footprint
   edge-by-edge; attribute the 0.2474 mm shortfall to simplification
   (0.3 @ 20°), tessellation deflection, or buffering-a-simplified-outline.
   Suspect simplification first: `simplification=0.3` moves vertices, and a
   simplified *inward* edge both shrinks the outline and re-opens neighbours.
2. **Confirm attribution cheaply**: re-run the probe (69 s) with
   `simplification=0.0` — if L3b drops to 0 (or the tightest gap rises to
   ≥ 3.0), simplification is the culprit; if not, sweep deflection.
3. **Choose the fix** from the measurement: oversize the collision buffer by
   the measured max deviation (keeps speed, conservative), or exclude collision
   geometry from simplification (exact, may cost time), or check footprints
   directly (correct but likely slow — measure before choosing).
4. Fix + regression test (fixture-level: assert every pair's real gap ≥
   spacing − epsilon after a seeded nest); re-run the 647 pytest + harness.

## Key file references (verified 2026-10-08)

- `freecad/nestingworkbench/Tools/Nesting/algorithms/nesting_strategy.py`
  — `_candidate_geometry_key` 24; `CandidateGeometryCache` 269 (`get`/`put`);
  `PlacementOptimizer.__init__` 321 (cache param stored at 352);
  `find_best_placement` 452; pool submit 530 / serial 562 →
  `_evaluate_rotation_tracked` 700 (thread-local part/angle wrapper);
  `_candidate_geometry_key_prefix` 726; `score_gravity` result applied 833–837;
  `_exact_candidate_mask` def 866 (staticmethod, call site 823);
  existing polygons 934–939; bbox screen 1034–1074; exact-row loop with
  cache get/put 1117–1152; `intersects` prefilter ~1200; area test 1205–1207;
  collision reject 1290–1292; `Nester._attempt_placement_on_sheet` 1666–1676.
- `freecad/nestingworkbench/Tools/Nesting/ga_coordinator.py` — run-scoped
  `CandidateGeometryCache()` created at 753.
- `freecad/nestingworkbench/Tools/Nesting/nesting_job.py` — `from_ga_result` 15,
  `job.sheets` assigned 42, `commit()` 71.
- `freecad/nestingworkbench/datatypes/shape.py` — `original_polygon` 34/90,
  `get_final_placement` 195, `set_rotation` 224, `move` 239.
- `freecad/nestingworkbench/Tools/Cam/cam_replay.py` — `flatten_container` 1247,
  `flatten_sheet` 1527, `nested_label_of` 1892, `part_footprint` 3647,
  `tightest_part_gap` 3832, `resolve_layout_group` 4569.
- `freecad/nestingworkbench/freecad_helpers.py` — `get_layout_group` 145,
  `get_sheet_groups` 176, `get_nested_containers` 193,
  `calculate_container_centroid` 253.
- Harness patterns: `tests/freecad_harness/make_nested_fixture.py`
  (ui_params/algo_kwargs 123–143, seed try/finally 181–219, unguarded
  `sys.exit(main())` 222), `tests/freecad_harness/probe_overlay_geometry_cost.py`
  (module `_LOG` + tail try/except + `_LOG.close()`),
  `tests/freecad_harness/test_tool_clearance.py` (slice-scan pattern 229–278).
- Old broken `/tmp/opencode/step3a.py`: bugs are
  `nesting_strategy.NestingStrategy` (class is `PlacementOptimizer`),
  wrong mask signature (missing `probe`), `cam_replay.get_nested_containers`
  (lives in `freecad_helpers`), seed defined but never set.

## Harness gotchas (measured, not inferred)

- Under `freecadcmd`: import `GACoordinator` only **after** `openDocument`;
  write output to a file (stdout may go to `/dev/null`); no positional args
  beyond the script path; exit code unreliable — the `DONE rc=N` log line is
  the contract.
- Rotation-eval exceptions are swallowed (`continue`, `quiet=True`) at
  `nesting_strategy.py:544/565` — spies must log their own errors, and
  `mask_calls > 0` is what distinguishes "no violations" from "probe never ran".
- Seed 1234 is documented but produced 48-part layouts at the old params;
  exact reproduction of the 54-part defect is **not guaranteed**.
- `bound_*` objects are source bounds at origin (one per source label, drawn
  only from `shape_preparer.py:618`) — not per-part layout bounds; layer 3a's
  source is `job.sheets[].parts[].shape.polygon`.
