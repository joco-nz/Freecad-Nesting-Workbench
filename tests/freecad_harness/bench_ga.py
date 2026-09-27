#!/usr/bin/env freecadcmd
"""GA-level benchmark: exposes GACoordinator's perf counters, which until now
nothing outside a log line could read.

Why this exists
---------------
`nest_benchmark.py` calls `nesting_logic.nest()` directly, so it never
constructs a `GACoordinator` and therefore never sees `_ga_perf` (57 counters)
or `_layout_perf` (17 phase timers). Those carry the only dataset that describes
the GA *loop* rather than a single nest: population and generation effects,
breeding, early stop, survivor cleanup, and the layout-management phase
breakdown. Including the interior-ring population metrics
(`mask_hole_rings`, `mask_subthreshold_hole_rings`, `mask_hole_sensitive_pairs`,
`mask_hole_exploiting_placements`) that the notebook uses for `hole_pct` and
`subthreshold_pct`.

It runs the coordinator synchronously -- `draw_callback=None`, `worker=None` --
which is the same path single-threaded execution takes, so this is real
measurement rather than a mock. The worker/thread path is a separate concern;
see probe_gui_session.py.

Self-validating
---------------
`validate_ga_perf` runs on every invocation. The instrumentation here is
hand-maintained (the absorbed-key list in `_record_nest_perf` is an explicit
allowlist), so it can drift silently. These checks make a drifted or dishonest
run fail rather than record a baseline nobody should trust.

Usage
-----
    $FREECAD tests/freecad_harness/bench_ga.py
    NEST_BENCH_GA_OUT=... $FREECAD tests/freecad_harness/bench_ga.py
    NEST_BENCH_GA_BASELINE=... $FREECAD tests/freecad_harness/bench_ga.py

Env: NEST_BENCH_GA_POPULATION, _GENERATIONS, _SHEET, _SEED, _QUANTITY, _REPS,
_CORPUS, _SPACING, _DEFLACTION, _SIMPLIFICATION, _ROTATION_STEPS, _OUT,
_BASELINE. Config is by environment variable because freecadcmd parses the
command line itself and silently drops any forwarded token starting with '-'.
"""
import json
import os
import random
import statistics
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_common as hc  # noqa: E402

STATUS_FILE = os.path.join(_HERE, ".last_status_bench_ga")
SCHEMA_VERSION = 1


def _cache_setting():
    """Tri-state, unset means follow the workbench default. See nest_benchmark.

    bench_ga drives the coordinator directly rather than through
    nesting_logic.nest, so it has to state the flag itself -- and stating it is
    the point, because a value it does not record is a value the baseline compare
    cannot check for drift.
    """
    from freecad.nestingworkbench.constants import CANDIDATE_GEOMETRY_CACHE_DEFAULT

    spec = os.environ.get("NEST_BENCH_CANDIDATE_GEOMETRY_CACHE", "").strip()
    if spec in ("", "default"):
        return CANDIDATE_GEOMETRY_CACHE_DEFAULT
    return spec not in ("0", "no", "false", "off")


def cfg():
    return {
        "population": int(os.environ.get("NEST_BENCH_GA_POPULATION", "2")),
        "generations": int(os.environ.get("NEST_BENCH_GA_GENERATIONS", "2")),
        "sheet": os.environ.get("NEST_BENCH_GA_SHEET", os.environ.get("NEST_BENCH_SHEET", "300x220")),
        "seed": int(os.environ.get("NEST_BENCH_SEED", "20260925")),
        "quantity": int(os.environ.get("NEST_BENCH_QUANTITY", "2")),
        "reps": int(os.environ.get("NEST_BENCH_REPS", "3")),
        "corpus": os.environ.get("NEST_BENCH_CORPUS", "synthetic"),
        "spacing": float(os.environ.get("NEST_BENCH_SPACING", "5.0")),
        "deflection": float(os.environ.get("NEST_BENCH_DEFLECTION", "0.05")),
        "simplification": float(os.environ.get("NEST_BENCH_SIMPLIFICATION", "0.1")),
        "rotation_steps": int(os.environ.get("NEST_BENCH_ROTATION_STEPS", "4")),
        "rotation_workers": int(os.environ.get("NEST_BENCH_ROTATION_WORKERS", "1")),
        # Recorded rather than inherited, so a baseline cannot silently
        # change meaning when the product default moves.
        "candidate_geometry_cache": _cache_setting(),
    }


def one_run(c):
    """One GA run, synchronously. Returns result, _ga_perf, _layout_perf, wall."""
    import FreeCAD

    from freecad.nestingworkbench.Tools.Nesting.ga_coordinator import GACoordinator
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.datatypes.shape import Shape

    # Class-level and process-wide; without this a second rep is all cache hits.
    Shape.clear_nfp_cache()
    Shape.clear_caches()

    doc = FreeCAD.newDocument(f"ga_{time.time()}")
    if c["corpus"] == "synthetic":
        parts, quantities = hc.build_synthetic_corpus(doc, c["seed"], c["quantity"])
        corpus = {"kind": "synthetic", "labels": sorted(quantities)}
    else:
        parts, quantities = hc.load_corpus(c["corpus"], c["quantity"])
        corpus = {"kind": "fcstd", "path": c["corpus"], "labels": sorted(quantities)}

    target = doc.addObject("App::DocumentObjectGroup", "Layout_target")
    width, height = (float(v) for v in c["sheet"].lower().split("x"))

    ui_params = {
        "sheet_width": width, "sheet_height": height,
        "spacing": c["spacing"], "deflection": c["deflection"],
        "simplification": c["simplification"],
        "rotation_steps": c["rotation_steps"],
        "add_labels": False, "font_path": "", "show_bounds": False,
        "label_height": 25.0, "label_size": 10.0, "verbose": False,
        "performance_logging": True, "algorithm": "Minkowski",
        "compactness_weight": 0.0, "generations": c["generations"],
        "population_size": c["population"], "random_seed": c["seed"],
        "sheet_thickness": 3.0, "deflection_angle": 30.0,
        "nesting_direction": 0, "use_random_direction": False,
    }
    full_quantities = {
        label: {"quantity": q, "rotation_steps": c["rotation_steps"],
                "up_direction": "Z+", "fill_sheet": False}
        for label, q in quantities.items()
    }
    algo_kwargs = {
        "generations": c["generations"], "population_size": c["population"],
        "verbose": False, "performance_logging": True,
        "spacing": c["spacing"], "search_direction": (0, -1),
        "random_seed": c["seed"],
        "cancel_callback": lambda: False,
        "candidate_geometry_cache": c["candidate_geometry_cache"],
        # performance_logging is what populates _ga_perf, so it must stay on.
        # The per-NFP [PERF] lines it also produces go through log_callback, so
        # a sink keeps the counters without burying the report: a single GA run
        # emits thousands of them.
        "log_callback": lambda msg, level=None: None,
    }

    coordinator = GACoordinator(
        doc=doc, shape_preparer=ShapePreparer(doc, {}),
        ui_callbacks={}, draw_callback=None, worker=None,
    )
    started = time.perf_counter()
    job = coordinator.run(target, ui_params, full_quantities,
                          {p.Label: p for p in parts}, {}, algo_kwargs,
                          False, viz_manager=None)
    wall = time.perf_counter() - started

    ga_perf = dict(coordinator._ga_perf or {})
    layout_perf = dict(coordinator._layout_perf or {})

    sheets, placed, unplaced = [], 0, 0
    if job is not None:
        sheets = job.sheets
        placed = sum(len(s.parts) for s in sheets)
        unplaced = len(getattr(job, "unplaced", []) or [])

    used_area = 0.0
    for sheet in sheets:
        for part in sheet.parts:
            poly = part.shape.polygon
            if poly is not None and not poly.is_empty:
                used_area += poly.area

    # What the GA prints as "efficiency" is exactly this ratio, so there is one
    # number, not two. An earlier version read job.efficiency, which does not
    # exist, and reported 0.0 while the run was fine at 44.8%.
    efficiency = round(used_area / (width * height * len(sheets)), 6) if sheets else 0.0

    result = {
        "sheets": len(sheets),
        "placed": placed,
        "unplaced": unplaced,
        "efficiency": efficiency,
        "used_area": round(used_area, 4),
        "density": efficiency,
    }
    return result, ga_perf, layout_perf, wall, corpus


def validate_ga_perf(ga_perf, layout_perf, c):
    """Self-checks on the GA instrumentation. Returns failure strings.

    `ga_perf` and `layout_perf` are the per-rep sample dicts from `summarise`:
    each key maps to a list of one value per rep. Only the first is examined --
    these are invariants that must hold on every rep, and the report flags
    rep-to-rep variation separately.

    The absorbed-key list in `_record_nest_perf` is an explicit allowlist, so it
    can drift without any error. These invariants catch a run whose counters say
    something impossible.
    """
    failures = []
    if not ga_perf:
        return ["_ga_perf is empty: the GA recorded nothing"]

    def one(key, default=0):
        samples = ga_perf.get(key)
        if not samples:
            return default
        return samples[0]

    evaluated = one("layout_evaluations", 0)
    if evaluated < c["population"]:
        failures.append(
            f"layout_evaluations {evaluated} < population {c['population']}: "
            f"not every member of the first generation was evaluated")

    if c["generations"] > 1:
        bred = one("offspring_layouts", 0) + one("immigrant_layouts", 0)
        if bred <= 0:
            failures.append(
                f"generations={c['generations']} but nothing was bred "
                f"(offspring=0 immigrants=0): the generation loop did not run")

    errors = one("nfp_errors", 0)
    if errors:
        failures.append(
            f"{errors} NFP(s) errored during the run. Each is an exception "
            f"swallowed by _compute_nfp_uncached's broad except that silently "
            f"removes a placement region. Not a comparable run.")

    pairs = one("nfp_convex_pairs", 0)
    if pairs <= 0:
        failures.append(
            f"nfp_convex_pairs is {pairs}: no convex sums ran, so this is not a "
            f"comparable run")

    # nfp_total_s should cover the phases it contains, at both levels.
    total_s = one("nfp_total_s", 0.0)
    inner = one("nfp_convex_sum_s", 0.0) + one("nfp_union_s", 0.0)
    if total_s + 1e-9 < inner:
        failures.append(
            f"nfp_total_s {total_s:.3f} < convex_sum+union {inner:.3f}: the "
            f"phase accumulator is not summing a partition")

    if layout_perf:
        for required in ("lm_create_s", "lm_prepare_parts_s", "lm_cleanup_s"):
            if required not in layout_perf:
                failures.append(f"missing _layout_perf key {required}")

    return failures


def summarise(c, runs):
    ga = {}
    for run in runs:
        for key, value in run["ga_perf"].items():
            ga.setdefault(key, []).append(value)
    layout = {}
    for run in runs:
        for key, value in run["layout_perf"].items():
            layout.setdefault(key, []).append(value)
    return ga, layout


def report(c, runs):
    hc.emit("=== GA benchmark ===")
    hc.emit(f"  population   {c['population']}")
    hc.emit(f"  generations  {c['generations']}")
    hc.emit(f"  reps         {c['reps']}")
    hc.emit(f"  sheet        {c['sheet']}")
    hc.emit(f"  corpus       {c['corpus']}")
    hc.emit(f"  rot workers  {c['rotation_workers']}  (pinned: counts are exact only "
            f"at width 1)")
    hc.emit(f"  cand cache   {'on' if c['candidate_geometry_cache'] else 'off'}")
    walls = [r["wall"] for r in runs]
    best = runs[0]["result"]
    hc.emit("")
    hc.emit("  --- result ---")
    for key in ("sheets", "placed", "unplaced", "efficiency", "density"):
        hc.emit(f"  {key:16s} {best[key]}")
    hc.emit(f"  {'wall min':16s} {min(walls):.3f}s   median {statistics.median(walls):.3f}s")
    spread = (max(walls) - min(walls)) / min(walls) * 100 if min(walls) else 0.0
    hc.emit(f"  {'wall spread':16s} {spread:.1f}%   over {c['reps']} reps in one process")

    ga, layout = summarise(c, runs)

    def show(title, data, only=None, limit=18):
        hc.emit("")
        hc.emit(f"  --- {title} ---")
        rows = sorted(data.items())
        if only:
            rows = [(k, v) for k, v in rows if any(s in k for s in only)]
        shown = 0
        for key, vals in rows:
            if not hc.is_timing_key(key):
                total = vals[0] if len(set(vals)) == 1 else sum(vals) / len(vals)
                stable = "" if len(set(vals)) == 1 else "  (varies across reps)"
                hc.emit(f"  {key:34s} {total!s:>12s}{stable}")
            else:
                hc.emit(f"  {key:34s} {min(vals):>12.3f}  (min)")
            shown += 1
            if shown >= limit:
                hc.emit(f"  ... {len(rows) - limit} more")
                break

    show("GA counters: NFP phases (s)", ga,
         only=("nfp_total_s", "nfp_convex_sum_s", "nfp_union_s", "nfp_decompose_s",
               "nfp_transform_s", "nfp_holes_ifp_s", "nfp_discretize_s"))
    show("GA counters: work", ga,
         only=("nfp_convex_pairs", "nfp_pieces_", "nfp_errors",
               "mask_hole_rings", "mask_subthreshold_hole_rings",
               "mask_hole_sensitive_pairs", "mask_hole_exploiting_placements",
               "layout_evaluations", "offspring", "immigrant",
               "rotation_evaluations", "successful_rotations",
               "candidate_points", "valid_candidate_points"))
    show("GA counters: loop (s)", ga,
         only=("generation_s", "nesting_s", "nfp_compute_s", "layout_management_s",
               "placement_wall_s", "rotation_wall_s"))
    show("layout-management phases (s)", layout)
    return ga, layout


def compare(record, baseline, allow_fewer):
    cur, base = record["result"], baseline["result"]
    hc.emit("")
    hc.emit("=== comparison vs baseline ===")
    failures = []

    def check(label, was, now, gate):
        delta = now - was
        verdict = "ok"
        if not gate(delta):
            verdict = "FAIL"
            failures.append(label)
        hc.emit(f"  {label:14s} {was!s:>10s} -> {now!s:>10s}  ({delta:+g})  {verdict}")

    check("sheets", base["sheets"], cur["sheets"], lambda d: d <= 0)
    check("unplaced", base["unplaced"], cur["unplaced"], lambda d: d <= 0)
    check("placed", base["placed"], cur["placed"],
          (lambda d: True) if allow_fewer else (lambda d: d >= 0))
    check("density", base["density"], cur["density"],
          (lambda d: True) if allow_fewer else (lambda d: d >= -1e-9))
    check("efficiency", base["efficiency"], cur["efficiency"],
          (lambda d: True) if allow_fewer else (lambda d: d >= -1e-9))

    changed = []
    cg, bg = record["ga_perf"], baseline["ga_perf"]
    for key in sorted(set(bg) & set(cg)):
        if hc.is_timing_key(key):
            continue
        if bg[key] != cg[key]:
            changed.append((key, bg[key], cg[key]))
    if changed:
        hc.emit("")
        hc.emit("  --- GA work counts that changed (review these) ---")
        for key, was, now in changed:
            pct = f"  ({100.0 * (now - was) / abs(was):+.1f}%)" if was else ""
            hc.emit(f"  {key:36s} {was!s:>12s} -> {now!s:>12s}{pct}")
    else:
        hc.emit("  no reproducible GA work counts changed")

    hc.emit("")
    if failures:
        hc.emit(f"  GATE FAILED: {', '.join(failures)}")
        return False
    hc.emit("  GATE PASSED")
    return True


def main():
    c = cfg()
    # Pinned so the work counts are gate-able. The tie-break draw in
    # score_gravity happens inside worker threads against one shared
    # random.Random, so the order the threads consume it in is scheduling
    # dependent above width 1.
    #
    # What that actually costs is narrower than first assumed, and measured here
    # rather than inferred. On this config every counter is identical at widths
    # 1/2/4/8. The nest harness does move some: at width 8 versus 1,
    # nfp_cache_hits/misses, candidate_points and exact_collision_checks all
    # differ, while nfp_work_convex_pairs and the packing result do not. So
    # width > 1 is measurable but not outcome-affecting; width 1 is simply the
    # only setting where the counts are exact. The noise block below reports
    # this per run, so the pin is self-enforcing rather than a comment.
    os.environ["NESTING_ROTATION_WORKERS"] = str(max(1, c["rotation_workers"]))
    runs = []
    for rep in range(c["reps"]):
        result, ga_perf, layout_perf, wall, corpus = one_run(c)
        runs.append({"result": result, "ga_perf": ga_perf,
                     "layout_perf": layout_perf, "wall": wall, "corpus": corpus})
        hc.emit(f"  rep {rep}: sheets={result['sheets']} placed={result['placed']} "
                f"efficiency={result['efficiency']}% wall={wall:.3f}s")

    ga, layout = report(c, runs)
    problems = validate_ga_perf(ga, layout, c)
    if problems:
        hc.emit("")
        hc.emit("ERROR: the GA instrumentation is not self-consistent, so this is "
                "not a comparable run:")
        for problem in problems:
            hc.emit(f"  - {problem}")
        return 3

    # Count stability, the same check the nest harness does. A work count that
    # moves at width 1 is not noise, it is a bug, and this file previously stored
    # the *mean* of such a count -- which is not a count, and which averages a
    # real regression into a plausible-looking number. Found while fixing the
    # nest harness, where the same class of drift turned out to be an uncleared
    # NFP cache making reps 2..N pure hits.
    unstable_counts, unstable_timings, unstable_results = hc.classify_stability(
        [r["ga_perf"] for r in runs], [r["result"] for r in runs])

    if unstable_results:
        hc.emit("")
        hc.emit("ERROR: the GA result differed between reps: "
                f"{unstable_results}. Not a comparable run.")
        return 3
    if unstable_counts and c["rotation_workers"] == 1:
        hc.emit("")
        hc.emit("ERROR: these GA work counts are not reproducible at rotation "
                f"width 1, so this run is not comparable: {unstable_counts}")
        hc.emit("  A count that should be exact has drifted. Do not average it --")
        hc.emit("  find out why.")
        return 3
    if unstable_counts:
        hc.emit("")
        hc.emit(f"NOTE: {len(unstable_counts)} GA work counts moved at rotation "
                f"width {c['rotation_workers']}. Above width 1 the tie-break draw in "
                f"score_gravity runs in worker threads against one shared "
                f"random.Random, so the consumption order is scheduling dependent.")
        hc.emit("  Recording the first value; treat these as indicative only.")

    walls = [r["wall"] for r in runs]
    record = {
        "schema": SCHEMA_VERSION,
        "corpus": runs[0]["corpus"],
        "config": c,
        "result": runs[0]["result"],
        "ga_perf": hc.collapse([r["ga_perf"] for r in runs]),
        "layout_perf": hc.collapse([r["layout_perf"] for r in runs]),
        "wall_seconds": round(min(walls), 4),
        "noise": hc.noise_block(c["reps"], c["rotation_workers"],
                                unstable_counts, unstable_timings, walls),
    }

    out = os.environ.get("NEST_BENCH_GA_OUT")
    if out:
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        hc.emit(f"\nwrote {out}")

    baseline_path = os.environ.get("NEST_BENCH_GA_BASELINE")
    if baseline_path:
        with open(baseline_path) as handle:
            baseline = json.load(handle)
        if baseline.get("schema") != SCHEMA_VERSION:
            hc.emit(f"ERROR: baseline schema {baseline.get('schema')} != {SCHEMA_VERSION}")
            return 2
        for key in ("population", "generations", "sheet", "seed", "quantity",
                    "rotation_workers", "candidate_geometry_cache"):
            if baseline["config"].get(key) != c[key]:
                hc.emit(f"ERROR: baseline {key}={baseline['config'].get(key)} "
                        f"but this run used {c[key]}")
                return 2
        return 0 if compare(record, baseline, bool(
            os.environ.get("NEST_BENCH_ALLOW_FEWER_PLACED"))) else 1

    return 0


if __name__ in ("__main__", "bench_ga"):
    try:
        _status = main()
    except Exception:
        traceback.print_exc()
        _status = 3
    try:
        with open(STATUS_FILE, "w") as _handle:
            _handle.write(str(_status))
    except OSError:
        pass
    sys.exit(_status)
