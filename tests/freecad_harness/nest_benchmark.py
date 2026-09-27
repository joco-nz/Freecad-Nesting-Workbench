#!/usr/bin/env freecadcmd
"""Tier 2/3 nesting benchmark harness.

Runs a real nest under FreeCAD's own interpreter and records the result as
JSON, so a change to the nesting code can be gated against a recorded "before"
rather than against an impression.

Why a plain script and not a pytest module: `freecadcmd` ships its own Python
without pytest, and the workbench's document-level code (profile extraction,
master creation, packing) needs a real FreeCAD. The pure-geometry tier lives in
tests/test_minkowski_utils/ and runs under plain CPython with FreeCAD stubs.

Configuration is by environment variable, not command-line flag -- see the
Configuration section for why flags are not usable from freecadcmd.

Usage
-----
    # Run and print
    freecadcmd tests/freecad_harness/nest_benchmark.py

    # Compare against the committed baseline (the per-block gate)
    NEST_BENCH_BASELINE=tests/freecad_harness/baseline/synthetic_v1.json \
        freecadcmd tests/freecad_harness/nest_benchmark.py

    # Record a new baseline
    NEST_BENCH_OUT=/tmp/bench.json \
        freecadcmd tests/freecad_harness/nest_benchmark.py

    # A real FreeCAD document instead of the built-in corpus
    NEST_BENCH_CORPUS=tests/Test_Files/n70-....FCStd NEST_BENCH_SHEET=700x500 \
        freecadcmd tests/freecad_harness/nest_benchmark.py

The exit status is also written to tests/freecad_harness/.last_status, because
freecadcmd does not reliably propagate a script's exit code.

Gates
-----
Hard (non-zero exit means reject the change):
  * sheets must not increase
  * unplaced must not increase
  * placed must not decrease
  * density must not decrease

NEST_BENCH_ALLOW_FEWER_PLACED=1 relaxes the last two. It exists for one case:
block 2.1 ports main's hole-edge sweeps, which shrink the inner-fit polygon for
non-convex holes. That is the correctness fix working -- fewer in-hole
placements are offered because the previous ones overlapped the hole wall --
so "fewer placed" is the expected outcome there, and the flag makes that
expectation explicit rather than something a reviewer has to infer.

Deliberately NOT gated: wall-clock time and the `*_ms` perf counters. They are
recorded and printed, but a timing regression is a judgement call about
whether a geometry change is worth the time, not something a threshold can
settle. Gate on correctness and density; read the timings.

Work-counting perf counters ARE reproducible and are reported for review when
they change -- see Config.apply_rotation_worker_limit for why the thread pool
has to be pinned off to make that true.
"""
import json
import os
import sys
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_common as hc  # noqa: E402  (needs the sys.path above)

SCHEMA_VERSION = 1
DEFAULT_SEED = 20260925
STATUS_FILE = os.path.join(_HERE, ".last_status")


def emit(message=""):
    """Prints a report line in a way that survives FreeCAD's console redirect.

    Under freecadcmd, FreeCAD.Console captures plain print() once a document
    exists, so report output silently disappears. Routing through
    FreeCAD.Console.PrintMessage keeps it visible; falling back to print covers
    being run some other way.
    """
    try:
        import FreeCAD

        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
# Everything is configured by environment variable rather than command-line
# flag, because freecadcmd parses the whole command line with its own option
# parser before the script runs, and its `--pass` escape hatch silently drops
# any forwarded token that starts with '-'.
#
# Measured on FreeCAD 26.3.0:
#     freecadcmd s.py --pass a b c            -> argv ['a','b','c']   works
#     freecadcmd s.py --pass --alpha --beta   -> script never runs
#     freecadcmd s.py --pass --alpha v --beta w -> script never runs
#
# So a flag-based interface is not reliably reachable from freecadcmd at all.
# This is the same class of collision described in
# ga_coordinator.worker_mp_context: inside FreeCAD the launcher claims your
# command-line flags for itself.
#
# The payoff is that the common cases need no arguments at all.
class Config:
    def __init__(self, environ=None):
        env = os.environ if environ is None else environ
        self.corpus = env.get("NEST_BENCH_CORPUS", "synthetic")
        self.out = env.get("NEST_BENCH_OUT", "")
        self.baseline = env.get("NEST_BENCH_BASELINE", "")
        self.allow_fewer_placed = bool(env.get("NEST_BENCH_ALLOW_FEWER_PLACED", ""))
        self.seed = int(env.get("NEST_BENCH_SEED", DEFAULT_SEED))
        # Sized to fill one sheet at ~56% density with the built-in corpus:
        # dense enough that a placement regression shows up in the numbers, while
        # still landing on a single deterministic layout.
        self.sheet = env.get("NEST_BENCH_SHEET", "450x350")
        self.spacing = float(env.get("NEST_BENCH_SPACING", "5.0"))
        self.deflection = float(env.get("NEST_BENCH_DEFLECTION", "0.05"))
        self.simplification = float(env.get("NEST_BENCH_SIMPLIFICATION", "0.1"))
        self.rotation_steps = int(env.get("NEST_BENCH_ROTATION_STEPS", "4"))
        self.rotation_workers = int(env.get("NEST_BENCH_ROTATION_WORKERS", "1"))
        self.quantity = int(env.get("NEST_BENCH_QUANTITY", "3"))
        self.draw = bool(env.get("NEST_BENCH_DRAW", ""))
        # Reps run in one process, interleaved with nothing else, each with
        # cold caches. Timings become a minimum over them; counts and results
        # must be identical, and a difference is a hard failure rather than
        # something to average away.
        self.reps = max(1, int(env.get("NEST_BENCH_REPS", "5")))

    def apply_rotation_worker_limit(self):
        """Pins the rotation thread-pool width for this process.

        Read at call time by nesting_strategy._rotation_worker_limit(), so
        setting it before the nest is enough.

        Why the default is 1: with the pool enabled the work counters are not
        reproducible. Rotation evaluation draws from one shared
        `random.Random`, and concurrent threads consume it in scheduling order,
        so tie-breaks vary run to run. Measured over three identical runs,
        28 of 51 perf counters differed (bbox_checks 51639/49894/47487,
        collision_intersects_true 1856/2387/2431, nfp_cache_misses 92/94/99).
        At width 1 only the eleven `*_ms` timing counters still move and all
        40 count counters are stable.

        The nesting *result* is identical either way on the built-in corpus --
        1 sheet, 18 placed, density 0.563184 every time -- so this is a
        measurement-fidelity problem rather than a correctness one. It does
        mean a baseline recorded with the pool on cannot be compared against a
        run with it off, which is why the width is recorded in the JSON.
        """
        os.environ["NESTING_ROTATION_WORKERS"] = str(max(1, self.rotation_workers))

    def as_dict(self):
        return {
            "seed": self.seed,
            "sheet": self.sheet,
            "spacing": self.spacing,
            "deflection": self.deflection,
            "simplification": self.simplification,
            "rotation_steps": self.rotation_steps,
            "rotation_workers": self.rotation_workers,
            "quantity": self.quantity,
            "draw": self.draw,
            "reps": self.reps,
        }


# --------------------------------------------------------------------------
# Corpora
# --------------------------------------------------------------------------
# FreeCAD scaffolding rather than parts: an Origin and its axes/planes/vertex
# are the children of every PartDesign::Body, and treating them as parts would
# nest noise.
_SKIP_TYPEIDS = {
    "App::Origin",
    "App::Line",
    "App::Plane",
    "App::Point",
    "App::Part",
    "PartDesign::Line",
    "PartDesign::Plane",
    "PartDesign::Point",
    "PartDesign::CoordinateSystem",
}


def build_synthetic_corpus(doc, seed, quantity):
    """Builds a deterministic part set that exercises the hard geometry.

    Deliberately mixed so the benchmark is sensitive to more than one path:
      * three rectangles     -- convex, fast path, no decomposition
      * an L-shape           -- concave, forces triangulation
      * a triangle           -- convex but non-rectangular
      * a plate with a hole  -- the inner-fit path, which is exactly what
                               block 2.1 changes

    Seeded, so identical on every machine. That matters: the n70 .FCStd corpus
    is gitignored and machine-local, so a baseline keyed to it could not be
    reproduced by anyone else.
    """
    import random

    import FreeCAD
    import Part

    rng = random.Random(seed)
    parts = []

    def add(name, shape):
        obj = doc.addObject("Part::Feature", name)
        obj.Shape = shape
        parts.append(obj)
        return obj

    for i in range(3):
        add(
            f"Rect{i}",
            Part.makeBox(rng.randrange(40, 90), rng.randrange(30, 70), 10),
        )

    add(
        "LShape",
        Part.makeBox(80, 80, 10).cut(
            Part.makeBox(50, 50, 10, FreeCAD.Vector(30, 30, 0))
        ),
    )

    add(
        "Triangle",
        Part.makePolygon(
            [
                FreeCAD.Vector(0, 0, 0),
                FreeCAD.Vector(70, 0, 0),
                FreeCAD.Vector(0, 60, 0),
                FreeCAD.Vector(0, 0, 0),
            ]
        ).extrude(FreeCAD.Vector(0, 0, 10)),
    )

    add(
        "HoledPlate",
        Part.makeBox(100, 100, 10).cut(
            Part.makeCylinder(22, 20, FreeCAD.Vector(50, 50, -5))
        ),
    )

    return parts, {p.Label: quantity for p in parts}


def discover_doc_parts(doc):
    """Finds candidate part objects in an opened document.

    Keeps only top-level candidates: an object already inside a candidate
    (a Body's Origin, or a Sketch inside a Body) is skipped.
    """
    candidates = [
        o
        for o in doc.Objects
        if o.TypeId not in _SKIP_TYPEIDS
        and hasattr(o, "Shape")
        and o.Shape
        and not o.Shape.isNull()
    ]
    tops = []
    for o in candidates:
        parented = False
        for other in candidates:
            if other.Name == o.Name:
                continue
            group = getattr(other, "Group", None)
            if group and any(getattr(c, "Name", None) == o.Name for c in group):
                parented = True
                break
        if not parented:
            tops.append(o)
    return tops


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------
def run_nest(doc, parts, quantities, cfg):
    """Prepares and nests, returning (result, perf, dead_ring, wall_seconds)."""
    import random as _random

    import FreeCAD
    from freecad.nestingworkbench.Tools.Nesting import nesting_logic
    from freecad.nestingworkbench.Tools.Nesting.algorithms import minkowski_utils
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer
    from freecad.nestingworkbench.datatypes.shape import Shape

    # Every rep must be a cold run. These caches are class-level, so within one
    # process rep 1 computed every NFP and reps 2..N were pure cache hits --
    # which moved 24 work counts and made every rep after the first a different
    # measurement. It was found by the count-stability gate below, not by
    # inspection, and it is the reason the gate exists.
    #
    # clear_nfp_cache() is called explicitly because Shape.clear_caches()
    # deliberately leaves the NFP cache alone ("expensive and benefits from
    # persistence"). That default is right for the workbench and wrong for a
    # benchmark, which must measure a cold cache every time.
    Shape.clear_nfp_cache()
    Shape.clear_caches()

    layout_group = doc.addObject("App::DocumentObjectGroup", "Layout_bench")
    parts_group = doc.addObject("App::DocumentObjectGroup", "PartsToPlace")
    shared_group = doc.addObject("App::DocumentObjectGroup", "MasterShapes")

    ui_settings = {
        "spacing": cfg.spacing,
        "deflection": cfg.deflection,
        "simplification": cfg.simplification,
        "rotation_steps": cfg.rotation_steps,
        "add_labels": False,
        "font_path": "",
        "verbose": False,
    }
    full_quantities = {
        label: {
            "quantity": qty,
            "rotation_steps": cfg.rotation_steps,
            "up_direction": "Z+",
            "fill_sheet": False,
        }
        for label, qty in quantities.items()
    }

    preparer = ShapePreparer(
        doc,
        {},
        create_doc_objects=cfg.draw,
        master_pool={},
        shared_master_group=None if cfg.draw else shared_group,
    )
    shapes = preparer.prepare_parts(
        ui_settings, full_quantities, {p.Label: p for p in parts}, layout_group, parts_group
    )

    # prepare_parts publishes the dead-ring profiles via
    # ShapePreparer._publish_dead_ring_profiles, and they must stay published
    # for the nest itself: the pruning happens inside decompose_if_needed,
    # which is first reached while the NFP cache is cold during the run. An
    # earlier version of this harness cleared them here, which silently
    # measured the optimisation switched OFF and produced a dead_ring of {} in
    # the baseline -- a badly misleading reference for a branch whose whole
    # point is that optimisation.
    #
    # get_dead_ring_stats() is the public check: it returns {} when no part
    # set has been published. Asserted after the nest in main(), so this stays
    # a comment rather than a second code path.
    minkowski_utils.reset_dead_ring_stats()

    width, height = (float(v) for v in cfg.sheet.lower().split("x"))
    perf = {}
    started = time.perf_counter()
    sheets, unplaced, _sim_steps, _elapsed = nesting_logic.nest(
        shapes,
        width,
        height,
        rotation_steps=cfg.rotation_steps,
        algorithm="Minkowski",
        rng=_random.Random(cfg.seed),
        quiet=True,
        perf_stats_callback=perf.update,
    )
    wall = time.perf_counter() - started

    placed_total = 0
    used_area = 0.0
    by_label = {}
    for sheet in sheets:
        for part in sheet.parts:
            placed_total += 1
            label = getattr(part.shape, "master_label", None) or part.shape.id
            by_label[label] = by_label.get(label, 0) + 1
            poly = part.shape.polygon
            if poly is not None and not poly.is_empty:
                used_area += poly.area

    sheet_area = width * height
    result = {
        "sheets": len(sheets),
        "placed": placed_total,
        "unplaced": len(unplaced),
        "sheet_width": width,
        "sheet_height": height,
        "sheet_area": sheet_area,
        "used_area": round(used_area, 4),
        "density": round((used_area / (sheet_area * len(sheets))) if sheets else 0.0, 6),
        "prepared_shapes": len(shapes),
        "placed_by_label": dict(sorted(by_label.items())),
    }
    return result, perf, minkowski_utils.get_dead_ring_stats(), wall


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def validate_perf(perf):
    """Self-checks on the instrumentation, run on every harness invocation.

    Guards the guard. The phase accumulator was added after a NameError inside
    `_compute_nfp_uncached` was swallowed by that function's broad `except`:
    every NFP became `{'error': ...}`, every rotation was skipped, and the run
    quietly produced 18 single-part sheets instead of 1. Nothing crashed and the
    summary line looked plausible.

    These invariants turn that class of silent degradation into a hard failure:

      * the phase keys must be present at all, or the accumulator is broken;
      * `total_ms` must cover the phases it is the sum of, catching a
        mis-ordered or partially-written accumulator;
      * `work_convex_pairs` must be non-zero on any run that did real work;
      * `nfp_errors` must be zero. Any NFP error is a swallowed exception.

    Returns a list of failure strings, empty when the run is sound.
    """
    failures = []

    phase_total = perf.get("nfp_phase_total_ms")
    if phase_total is None:
        failures.append("no nfp_phase_total_ms: the phase accumulator is not "
                        "reporting, so phase attribution is unavailable")
        return failures

    for required in ("nfp_phase_convex_sum_ms", "nfp_phase_union_ms",
                     "nfp_work_convex_pairs", "nfp_nfp_errors"):
        if required not in perf:
            failures.append(f"missing perf key {required}")

    # total_ms is the whole NFP, so it must not be less than the sum of two
    # phases that sit inside it.
    inner = perf.get("nfp_phase_convex_sum_ms", 0.0) + perf.get("nfp_phase_union_ms", 0.0)
    if phase_total + 1e-9 < inner:
        failures.append(
            f"phase_total_ms {phase_total:.3f} < convex_sum+union {inner:.3f}: "
            f"the accumulator is not summing a partition")

    pairs = perf.get("nfp_work_convex_pairs", 0)
    if pairs <= 0:
        failures.append(f"nfp_work_convex_pairs is {pairs}: no convex sums ran, "
                        f"so this is not a comparable run")

    errors = perf.get("nfp_nfp_errors", 0)
    if errors:
        failures.append(
            f"{errors} NFP(s) errored. Each is an exception swallowed by "
            f"_compute_nfp_uncached's broad except, and each one silently "
            f"removes a placement region. This run is not comparable.")

    worst_total = perf.get("nfp_phase_total_ms_worst", 0.0)
    if worst_total > phase_total + 1e-9:
        failures.append("worst single NFP exceeds the total: accumulator is "
                        "double counting")

    return failures


def _is_timing(key):
    """True for wall-clock counters, never comparable run to run.

    Matches both `*_ms` and `*_ms_worst` (the phase accumulator's worst-single-
    NFP figures). Getting this wrong is not cosmetic: a `_ms_worst` key was
    classified as a work count and reported as a "reproducible count that
    changed", which is exactly the noise it is not.
    """
    return key.endswith("_ms") or "_ms_" in key


def build_record(cfg, corpus_desc, result, perf, dead_ring, wall):
    return {
        "schema": SCHEMA_VERSION,
        "corpus": corpus_desc,
        "config": cfg.as_dict(),
        "result": result,
        "perf": {k: perf[k] for k in sorted(perf)},
        "dead_ring": dead_ring,
        "wall_seconds": round(wall, 4),
    }


def print_record(record):
    r = record["result"]
    counts = [k for k in record["perf"] if not _is_timing(k)]
    timings = [k for k in record["perf"] if _is_timing(k)]
    emit("")
    emit("=== nest result ===")
    emit(f"  corpus            {record['corpus']['kind']}")
    emit(f"  sheets            {r['sheets']}")
    emit(f"  placed            {r['placed']}")
    emit(f"  unplaced          {r['unplaced']}")
    emit(f"  density           {r['density']:.4f}")
    emit(f"  prepared shapes   {r['prepared_shapes']}")
    emit(f"  wall              {record['wall_seconds']:.3f}s")
    emit(f"  perf counters     {len(record['perf'])} "
         f"({len(counts)} reproducible counts, {len(timings)} timings)")
    if record["dead_ring"]:
        emit(f"  dead rings        {record['dead_ring']}")
    noise = record.get("noise")
    if noise:
        emit(f"  reps              {noise['reps']} in one process, "
             f"rot width {noise['rotation_workers']}")
        emit(f"  wall spread       {noise['wall_seconds_spread_pct']:.1f}%")
        emit(f"  work counts       "
             f"{'exact' if noise['work_counts_exact'] else 'NOT EXACT: ' + str(noise['work_counts_unstable'])}")
        emit(f"  timings varying   {noise['timings_vary']} of the `*_ms` keys")


def compare(current, baseline, allow_fewer_placed):
    """Prints a diff; returns True when every gate passes."""
    c, b = current["result"], baseline["result"]
    emit("")
    emit("=== comparison vs baseline ===")
    failures = []

    def check(label, was, now, gate):
        delta = now - was
        if not gate(delta):
            verdict = "FAIL"
            failures.append(label)
        else:
            verdict = "ok"
        emit(f"  {label:16s} {was!s:>10s} -> {now!s:>10s}  ({delta:+g})  {verdict}")

    check("sheets", b["sheets"], c["sheets"], lambda d: d <= 0)
    check("unplaced", b["unplaced"], c["unplaced"], lambda d: d <= 0)
    check("placed", b["placed"], c["placed"],
          (lambda d: True) if allow_fewer_placed else (lambda d: d >= 0))
    check("density", b["density"], c["density"],
          (lambda d: True) if allow_fewer_placed else (lambda d: d >= -1e-9))

    changed = []
    for key in sorted(set(baseline["perf"]) & set(current["perf"])):
        if _is_timing(key):
            continue
        was, now = baseline["perf"][key], current["perf"][key]
        if isinstance(was, (int, float)) and isinstance(now, (int, float)) and was != now:
            changed.append((key, was, now))

    emit("")
    emit("  --- timings, not gated ---")
    emit(f"  wall_seconds      {baseline['wall_seconds']:.3f} -> {current['wall_seconds']:.3f}")
    if changed:
        emit("")
        emit("  --- work counts that changed (reproducible; review these) ---")
        for key, was, now in changed:
            pct = f"  ({100.0 * (now - was) / abs(was):+.1f}%)" if was else ""
            emit(f"  {key:32s} {was!s:>12s} -> {now!s:>12s}{pct}")
    else:
        emit("  no reproducible work counts changed")

    emit("")
    if failures:
        emit(f"  GATE FAILED: {', '.join(failures)}")
        return False
    emit("  GATE PASSED")
    return True


def main():
    import FreeCAD

    cfg = Config()
    cfg.apply_rotation_worker_limit()

    # Reps run in one process, interleaved with nothing else in between, and
    # each clears the class-level caches so every rep is a cold run. That is the
    # point: a baseline recorded from one sample is a claim about a machine
    # state that does not repeat.
    reps = []
    corpus_desc = None
    for _rep in range(cfg.reps):
        doc = FreeCAD.newDocument("bench")
        if cfg.corpus == "synthetic":
            parts, quantities = build_synthetic_corpus(doc, cfg.seed, cfg.quantity)
            corpus_desc = {"kind": "synthetic", "seed": cfg.seed,
                           "labels": sorted(quantities)}
        else:
            if not os.path.exists(cfg.corpus):
                emit(f"ERROR: corpus not found: {cfg.corpus}")
                return 2
            source = FreeCAD.openDocument(cfg.corpus)
            parts = discover_doc_parts(source)
            quantities = {p.Label: 1 for p in parts}
            corpus_desc = {"kind": "fcstd", "path": cfg.corpus,
                           "labels": sorted(quantities)}
        if not parts:
            emit("ERROR: corpus produced no parts")
            return 2
        result, perf, dead_ring, wall = run_nest(doc, parts, quantities, cfg)
        reps.append({"result": result, "perf": perf,
                     "dead_ring": dead_ring, "wall": wall})

    result_fields = list(reps[0]["result"].keys())
    perf_list = [r["perf"] for r in reps]
    results = [r["result"] for r in reps]
    unstable_counts, unstable_timings, unstable_results = hc.classify_stability(
        perf_list, results)

    # A result field that moves is a packing change, which the gate reports
    # anyway. Counts that move at width 1 mean the run is not comparable.
    if unstable_results:
        emit("")
        emit("ERROR: the packing result differed between reps: "
             f"{unstable_results}. Not a comparable run.")
        return 3

    walls = [r["wall"] for r in reps]
    if unstable_counts and cfg.rotation_workers == 1:
        emit("")
        emit("ERROR: these work counts are not reproducible at rotation width 1, "
             f"so this run is not comparable: {unstable_counts}")
        emit("  A count that should be exact has drifted. Do not average it --")
        emit("  find out why.")
        return 3
    if unstable_counts:
        emit("")
        emit(f"NOTE: {len(unstable_counts)} work counts moved because rotation "
             f"width is {cfg.rotation_workers}, where one shared random.Random is "
             f"consumed by concurrent threads and tie-breaks are not stable.")
        emit("  Recording the first value; treat these as indicative only.")

    # Timings: minimum of N is the only defensible estimator. Counts are never
    # averaged -- see hc.collapse.
    perf = hc.collapse(perf_list)

    result = reps[0]["result"]
    dead_ring = reps[0]["dead_ring"]
    record = build_record(cfg, corpus_desc, result, perf, dead_ring, min(walls))
    record["noise"] = hc.noise_block(cfg.reps, cfg.rotation_workers,
                                     unstable_counts, unstable_timings, walls)
    print_record(record)

    problems = validate_perf(perf)
    if problems:
        emit("")
        emit("ERROR: the run's own instrumentation is not self-consistent, so "
             "this is not a comparable run:")
        for problem in problems:
            emit(f"  - {problem}")
        return 3

    if not dead_ring:
        # A run with the optimisation silently off is not a usable reference.
        # Fail loudly rather than record a baseline that would let a later
        # regression in dead-ring pruning pass unnoticed.
        emit("")
        emit("ERROR: dead-ring pruning produced no counters, so it was not "
             "active for this run. Refusing to record or compare a baseline "
             "that does not exercise the optimisation.")
        return 3

    if cfg.out:
        os.makedirs(os.path.dirname(os.path.abspath(cfg.out)), exist_ok=True)
        with open(cfg.out, "w") as handle:
            json.dump(record, handle, indent=2, sort_keys=True)
        emit(f"\nwrote {cfg.out}")

    if cfg.baseline:
        with open(cfg.baseline) as handle:
            baseline = json.load(handle)
        if baseline.get("schema") != SCHEMA_VERSION:
            emit(f"ERROR: baseline schema {baseline.get('schema')} != {SCHEMA_VERSION}")
            return 2
        if baseline.get("corpus", {}).get("kind") != corpus_desc["kind"]:
            emit("ERROR: baseline corpus kind does not match this run")
            return 2
        if baseline.get("config", {}).get("rotation_workers") != cfg.rotation_workers:
            emit("ERROR: baseline was recorded at a different rotation worker "
                 f"width ({baseline['config'].get('rotation_workers')} vs "
                 f"{cfg.rotation_workers}); its work counts are not comparable")
            return 2
        return 0 if compare(record, baseline, cfg.allow_fewer_placed) else 1

    return 0


if __name__ in ("__main__", "nest_benchmark"):
    try:
        _status = main()
    except Exception:
        traceback.print_exc()
        _status = 3
    # freecadcmd does not reliably propagate a script's exit code, so the
    # status is also written where CI can read it.
    try:
        with open(STATUS_FILE, "w") as _handle:
            _handle.write(str(_status))
    except OSError:
        pass
    sys.exit(_status)
