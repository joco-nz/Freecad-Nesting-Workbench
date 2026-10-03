# Decomposing the Nesting panel's `_setup_ui`

Goal: the panel's build method was 653 lines and unmaintainable in practice. This
is what splitting it cost, what it bought, and the three things that went wrong on
the way — two of them bugs I introduced and shipped past a green test suite.

`_setup_ui` builds the whole panel: every widget, every layout, and every signal
connection. It was one method because that is how it grew.

## Result

| | before | after |
|---|---:|---:|
| `_setup_ui` | 653 lines | **180** |
| methods over 80 lines, excluding `_setup_ui` | 0 | 4 (`_build_physics_section` 120, `_assemble_panel_layout` 115, `load_persisted_settings` 103, `_build_direction_grid` 102) |
| raw widget constructions in `_setup_ui` | 50 | 0 |

The end state is an orchestrator: create the algorithm dropdown and connect it,
eight builder calls, `_assemble_panel_layout()`, then the signal wiring. Most of
the remaining 180 lines are the section-design comments and the wiring, which is
where they belong.

Two commits before this one did the visible part: `50eebbe` made all five panel
headings collapsible sections, and `fb061ce` routed 26 widget constructions
through shared factories in `ui_helpers.py`.

## What moved, and in what order

Six extractions, deliberately easiest-first. Every one but the last two is pure
widget creation onto `self`, where nothing is parented and the only failure mode
is a missing attribute.

| step | builder | `_setup_ui` after |
|---|---|---:|
| B1 | `_build_minkowski_ga_fields`, `_build_optimization_fields`, `_build_minkowski_perf_dials` | 539 |
| B2 | `_build_length_fields` | 477 |
| B3 | `_set_initial_section_visibility`, `_build_helper_fields`, `_build_action_buttons` | 445 |
| B4 | `_build_physics_section` | 351 |
| B5 | `_build_direction_grid` | 273 |
| B6 | `_assemble_panel_layout` | 180 |

Two placements were forced by data flow rather than taste:

- **`_build_length_fields` reassigns `self._length_fields`** rather than
  appending, so it has to run before anything appends. It is still called first.
- **`refresh_unit_display` walks `self._length_fields`**, not the widget tree, so
  a builder that creates a `LengthField` must append it there. And
  `_refresh_unit_tooltips` reaches two fields by attribute *name*
  (`simplification_input`, `minkowski_step_size_input`), so those names cannot be
  localised.

**`_build_physics_section`, `_assemble_panel_layout` and `_build_direction_grid`
run over the ~80-line guideline and that is deliberate.** Both blocks interleave widget creation with
layout population — each widget is created and immediately added to the form,
across ~23 `addRow` calls with spacers and separators between them. Splitting
either would mean returning twenty widgets or rewriting it as two passes: churn on
verified code to satisfy a line-count target. The direction grid's five comment
paragraphs are one argument and were kept whole for the same reason.

## Three guards, added before the extractions they protect

Each was verified by injection — change the thing, watch the check fail, revert —
because a guard added alongside the change it guards is a guard that has never
been shown to work.

| guard | protects | injection |
|---|---|---|
| `probe_gui_layout_positions.py` (`015ebc5`) | containment and parentage of every control | moved `sound_checkbox` Helpers → Logging |
| `case_direction_dial` cell order (`926dcf2`) | the direction grid's arrangement | moved the dial to row 1, single-row span |
| main-layout item order (`baf53fa`) | the seven items in `main_layout` | moved `addStretch` above the status label; moved `shape_table` after the table buttons |

`probe_gui_layout_positions.py` exists because *every other panel test asserts on
state, and none on position*. A control parented into the wrong container passes
all of them. That was the gap the extraction opened, so it was closed first.

## What went wrong

### 1. A documented return value that was never written

`_build_optimization_fields` has a docstring saying it returns the compactness
row's layout. The body never returned it. The caller therefore passed `None`
into the Optimizations grid and **the panel did not build at all**.

It compiled, and **all 537 unit tests passed** — none of them construct the
panel. `probe_gui_collapsible.py` caught it on the first run.

The comment above the optimisation fields now says why `addLayout` exists and why
the builder returns its layout, and the return is there.

### 2. Two layouts created in one place and consumed in another

`_assemble_panel_layout`'s first version missed `helpers_layout` and
`logging_box_layout` — created in `_setup_ui`, consumed only by the helpers and
logging forms. `NameError`, panel does not build, again caught by
`probe_gui_collapsible` rather than the unit tests.

The pattern across both: **the unit tests are blind to whether the panel exists.**
Anything that fails during construction needs a probe that builds the panel.

### 3. Two tests asserting on spelling, dressed as behaviour

Both broke on a correct refactor and had to be taught the new spelling:

- `test_fields_are_in_the_minkowski_group` walks the AST looking for
  `setLayout` to check a Minkowski field is not wired into the Physics group —
  from a real bug. Renaming to `addLayout` made it pass **vacuously**, which is
  the failure mode it exists to catch.
- `test_panel_declares_both_fields` searches for a literal `setValue(0)`. Its own
  docstring says the assertion is "on the RESOLVED default, not on a spelling",
  but the implementation was spelling-dependent.

Both now accept any spelling that sets the same value. Worth knowing before the
next refactor here: **several tests in this suite assert how the panel is written
rather than what it does.**

### And one check that could never have worked

The first version of the positional probe counted layouts per widget and asserted
at most one, to catch a double-add. It reported three false failures, and working
out why showed it was incapable:

- `QLayout.indexOf()` recurses into nested layouts, so a control in
  `form_layout` is also found by the `main_layout` nesting it.
- Qt *removes* a widget from its previous layout when it is added to a second
  one — so the count could never exceed one however the code misbehaved.

Replaced with a parentage check that can fail. A check that cannot fail is not a
check, and the observable consequence of a double-add is that the first layout
silently loses a widget — which shows up as a containment failure anyway. The
discarded attempt is recorded in the probe's docstring so nobody re-adds it.

## Smaller findings

**The A/B line estimate was wrong.** Routing 26 constructions through factories
was projected to take `_setup_ui` from 653 to ~610. It went to 643 — three lines.
Most sites were already one line joined with `;`, so `make_checkbox("Show
Bounds", checked=True)` saves nothing. What was actually bought is one spelling
for widget configuration and a module that owns the convention. The line-count
win arrived from the extractions, not the factories.

**`LengthField` is deliberately not served by `make_double_spinbox`.** It is a
wrapper that owns a spin box and exposes `.widget()`, and it is the one widget in
the panel that knows what a unit is. Routing it through a unit-unaware factory
would be the wrong layer.

**Not everything was extracted.** The signal wiring stays in `_setup_ui`, and
`load_persisted_settings` (103 lines) and `_two_column_grid` (50) were left alone.
The wiring because moving it changes when a signal can first fire relative to the
fields, which nothing tests; the other two because neither is in `_setup_ui` and
neither is what made it unreadable.

**The algorithm dropdown turned out better than planned.** The plan was to move
its creation into the assembly too, which would have moved its `connect` from
before the field builders to after — a wiring order change, on a decision that
the wiring was to stay put. Leaving creation and connection in `_setup_ui` and
moving only its `addRow` means **no wiring moved relative to anything**.

## What this leaves

`EXPECTED` in `probe_gui_layout_positions.py` is a second place that knows which
control belongs in which section, so moving a control between sections is now a
two-file edit. That is deliberate — the two probes assert different things,
containment versus grid cell order, and neither derives from the other — but it
should be discovered from this file rather than from a failing test. It is noted
in the harness README under "Panel layout".

## Reproducing

```sh
FREECAD=/path/to/freecadcmd
GUI=/path/to/freecad          # the three panel probes need this one

python3 -m pytest tests/ -q

for t in probe_unit_panel probe_gui_collapsible probe_gui_layout_positions; do
    env $GUI tests/freecad_harness/$t.py
done
for t in test_panel_teardown test_document_units test_master_promotion; do
    env $FREECAD tests/freecad_harness/$t.py
done
```

Current: 537 unit tests · `probe_unit_panel` 141/141 · `probe_gui_collapsible`
40/40 · `probe_gui_layout_positions` 101/101 · `test_panel_teardown` 25/25 ·
`test_document_units` 72/72 · `test_master_promotion` 32/32.
