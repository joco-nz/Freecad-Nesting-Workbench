# Object churn in Simulate mode: where the per-member FreeCAD objects actually are

Follows `RESULTS-rotation-width.md`, which measured the headless GA path and
found the per-member document-object churn there to be **0.17 s of a 30 s run
(0.6%)** — already solved, and not worth porting upstream's template for.

This file covers the other path. **Simulate mode is the same problem at a
different scale, and it is severe.**

## Short answer

On n70 at pop 10 × gen 4, Simulate mode spends **59.5 s of its 150.4 s
generation time in layout management — 39.6%** — and creates and deletes
**2,914 FreeCAD document objects** where the headless path manages 72. The packing
result is byte-identical between the two (6 sheets, 18 placed, 0.35397), and
the geometry counters match to within 5%, so the entire gap is object churn and
drawing, not search.

This is the shape of problem upstream describes as "builds and deletes a full
set of FreeCAD objects for every population member". At our scale it is worth
roughly 40% of generation time in layout management alone (45% counting the
one-off population build), against their claimed 3.25× (~69% reduction).

## 1. Why Simulate is different, and why that is deliberate

`GACoordinator.run` takes `is_simulating`. It sets one flag:

```python
# Simulate mode draws every layout as it nests, so it must keep them.
self._headless_layouts = not is_simulating
```

which becomes `create_doc_objects` on `LayoutManager`/`ShapePreparer`. In
headless mode that is `False` and the GA creates **no** per-part document
objects at all — `part_features=0` below. Simulate sets it `True`, and
`shape_preparer.py:598` then creates a `Part::Feature` per part per layout.

Master pooling is gated on the same flag:

```python
# Pooling is gated on create_doc_objects, NOT on master_pool being
# non-empty. LayoutManager always supplies a pool dict, so gating on the
# dict alone let simulate mode read it: layouts 2..N then reused
# layout 1's masters instead of owning their own, which is exactly
# what simulate mode must not do -- it draws as it nests, so tearing
# one down must not remove objects another is still using.
self.pool_masters = not create_doc_objects
```

Both are intentional. Simulate draws each layout as it nests, so it needs
per-layout objects, and the pooling gate exists because sharing masters across
simulated layouts **was a bug** — it broke teardown.

## 2. Method

`bench_ga.py` with `NEST_BENCH_GA_SIMULATE=1` (added in `9468475`).

Config held constant across both arms: n70 corpus
(`tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd`), sheet
700×500, quantity 6 → **18 parts**, `NEST_BENCH_GA_POPULATION=10`,
`NEST_BENCH_GA_GENERATIONS=4`, `rotation_steps=4`, rotation width pinned to 1
so the work counters are exact and not scheduling-dependent (see
`RESULTS-rotation-width.md` §6).

**Simulate needs the GUI binary.** `nesting_logic.update_sim_view` calls
`FreeCADGui.updateGui()`, which does not exist under `freecadcmd`; the run
aborts on the first placement. This fails *silently* — it reports 0 sheets and
0% efficiency rather than raising, because `quiet=True` swallows the log. My
first attempt measured exactly that and looked like a 6× speedup. Use `freecad`,
which needs no Xvfb on 26.3:

```sh
NEST_BENCH_GA_SIMULATE=1 NEST_BENCH_CORPUS=.../n70-...FCStd \
NEST_BENCH_GA_SHEET=700x500 NEST_BENCH_QUANTITY=6 \
NEST_BENCH_GA_POPULATION=10 NEST_BENCH_GA_GENERATIONS=4 \
NEST_BENCH_REPS=1 \
    freecad tests/freecad_harness/bench_ga.py     # NOT freecadcmd
```

Arms: headless 2 reps under `freecadcmd`, Simulate 1 rep under `freecad`.
Simulate is n=1 — see §6.

## 3. Time

| | headless | Simulate | ratio |
|---|---:|---:|---:|
| outer wall | 31.0 s | **173.6 s** | 5.6× |
| `generations_s` | 29.06 s | 150.39 s | 5.2× |
| `nesting_s` | 28.67 s | 82.71 s | 2.9× |
| **`layout_management_s`** | **0.17 s** | **59.52 s** | **350×** |

`layout_management_s` is 39.6% of Simulate's 150.4 s generation time, against
0.58% of headless's 29.1 s.

Per-rep, to show it is not one bad run: headless walls 31.348 / 30.941 s,
`generations` 29.25 / 28.87 s, `layout_management` 0.15 / 0.19 s.

The two components of the gap, kept separate because they are different
problems:

- **Object churn — 59.5 s.** `layout_management_s`, i.e. creating and deleting
  the per-generation layouts. **39.6% of Simulate's generation time**, against
  0.6% headless. Add the 19.8 s one-off population build and total non-nesting
  overhead is 67.7 s, or 45%.
- **Drawing — the rest.** `nesting_s` is 2.9× slower, but that is not churn:
  Simulate calls `update_sim_view` → `FreeCADGui.updateGui()` per placement.
  Attributing all 54 s of extra nesting time to object churn would be wrong.

## 4. Objects

| | headless | Simulate |
|---|---:|---:|
| **`part_features` created** | **0** | **666** |
| `part_boundary_features` | 0 | 666 |
| `master_containers` | 3 | 111 |
| `master_part_features` | 3 | 111 |
| `master_boundary_features` | 3 | 111 |
| `master_groups` | 1 | 37 |
| `group_objects` | 74 | 74 |
| **`doc_objects_deleted`** | **72** | **2,914** |
| `layouts_created` | 37 | 37 |
| `parts_created` | 666 | 666 |

The last two rows are the control: **the logical work is identical** — 37
layouts, 666 parts, both arms. What differs is that headless expresses those
666 parts as Shapely geometry and never puts them in the document, while
Simulate builds 1,332 `Part::Feature` objects for them and deletes all of them
again, plus 108 master containers and 37 master groups.

Sub-phase breakdown, Simulate:

| | headless | Simulate | ratio |
|---|---:|---:|---:|
| `create` | 1.96 s | 69.80 s | 36× |
| `prepare` | 1.85 s | 69.68 s | 38× |
| **`instances`** | **0.00 s** | **51.82 s** | — |
| `masters` | 1.84 s | 17.86 s | 9.7× |
| `delete` | 0.01 s | 12.76 s | 1276× |
| `cleanup` | 0.01 s | 3.27 s | 327× |
| `population` | 1.86 s | 19.84 s | 10.7× |

`instances` — 51.8 s, 78 ms per part feature — is `create_part_feature(...,
master_shape_obj.Shape.copy())` at `shape_preparer.py:599`, a full 3D solid
copy, 666 times. That is the single largest churn item and it is the one
Simulate cannot simply stop doing, because those objects are what it draws.

## 5. The search is not the difference

Every geometry counter matches within 5%:

| | headless | Simulate |
|---|---:|---:|
| `nfp_compute_s` | 2.12 s | 2.30 s (1.08×) |
| `validity_stage_s` | 21.87 s | 21.18 s (0.97×) |
| `collision_intersection_s` | 16.54 s | 15.76 s (0.95×) |

and the result is identical: **6 sheets, 18 placed, 0.35397 efficiency** in
both. So none of the 5.6× is a better or worse search. It is object churn plus
GUI drawing, full stop.

## 6. What could be removed, and what it would risk

**Not addressed here.** For the record, and in rough order of value:

1. **Share masters through a document-level group** — the trick headless
   already uses via `_get_shared_master_group`, and the reason
   `test_master_promotion.py` exists. That would take `master_containers` from
   111 to 3 and save most of the 17.9 s `masters` time.
   *Risk:* this is precisely the change the `pool_masters` comment records as
   having broken teardown before. Headless only gets away with it because
   `recursive_delete` of a layout does not reach a document-level group; Simulate
   currently parents masters per layout. Making that safe means re-establishing
   the invariant headless already relies on — real work, and it has a scar.

2. **Stop rebuilding per-part features per layout** — the 51.8 s `instances`
   item. Simulate needs objects to draw, so this cannot be deleted; at best the
   objects could be reused and repositioned, which reintroduces exactly the
   shared-object lifetime problem as (1), on the objects that matter most.

Realistic total: perhaps 20–25 s of the ~86 s of churn. Meaningful, not
transformative, and it goes directly at documented teardown semantics.

## 7. Caveats

- **Simulate is n=1.** Headless is 2 reps. At 39.6% versus 0.58% no plausible noise
  explains the gap, but the 45% figure is not a tight estimate, and the
  headless/Simulate ratio should not be quoted to three significant figures.
- **`nesting_s` is not purely drawing.** I attributed the difference to
  `updateGui` per placement, which is the only extra work in that path I
  verified. I did not profile it, so treat the churn/drawing split as
  indicative.
- **One corpus.** 18 parts of n70. Simulate's per-member cost scales with part
  count × population × generations, so the *share* will differ on other inputs.
- The silent-failure mode of §2 is worth flagging on its own: under
  `freecadcmd`, Simulate reports 0 sheets and 0% efficiency and exits 0. Any
  future harness that adds a GUI-only path should fail loudly instead.

## Summary

| lever | headless | Simulate | status |
|---|---|---|---|
| per-member document objects | 0 | 1,332 | **the finding** — 39.6% of generation time |
| share masters via document-level group | n/a | −17.9 s, 111→3 containers | not done; re-opens a documented teardown bug |
| reuse per-part features | n/a | −51.8 s possible | not done; Simulate needs them to draw |
| upstream's template approach | 0.6% ceiling | untested | **not needed** — measured as ~0 on the path upstream fixed |

**The upstream fix targets the headless path, where our churn is already 0.6%.
The churn upstream describes is real on this workbench, but it lives in
Simulate mode.** Anyone reading upstream's 3.25% and reaching for a template
should be told to measure Simulate instead.

## Reproducing

```sh
FREECAD=/path/to/freecadcmd
GUI=/path/to/freecad            # Simulate needs this one

COMMON="NEST_BENCH_CORPUS=tests/Test_Files/n70-intercooler-spacer-bottle-nesting.FCStd
        NEST_BENCH_GA_SHEET=700x500 NEST_BENCH_QUANTITY=6
        NEST_BENCH_GA_POPULATION=10 NEST_BENCH_GA_GENERATIONS=4
        NEST_BENCH_ROTATION_STEPS=4 NEST_BENCH_REPS=1"

env $COMMON $FREECAD tests/freecad_harness/bench_ga.py
env $COMMON NEST_BENCH_GA_SIMULATE=1 $GUI tests/freecad_harness/bench_ga.py
```
