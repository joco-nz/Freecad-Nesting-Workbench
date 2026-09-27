#!/usr/bin/env freecadcmd
"""Block 9.4: the candidate-geometry cache, A/B'd on the real workload.

The cache is the one opt-in feature with a measured win and no test coverage.
`make-faster.md` records 87.44 s -> 10.22 s at a 75.2% hit rate, and
master-merge.md 9.4 needs a disabled-control measurement before it can go
default-on. It is off by default (`algo_kwargs['candidate_geometry_cache']`),
so on the n70 corpus the `candidate_geometry_ms` counter has been reading
~10.9 s of work that the cache would have largely skipped.

What this settles
-----------------
Three questions, in order of how much they matter:

1. **Does it change the packing?** If it does, it is a behaviour change, not a
   speed knob, and no timing number justifies shipping it. The cache stores
   *geometry only* -- collision, sheet-boundary and scoring results are
   documented as always recomputed -- so it should be layout-neutral. Measured
   rather than assumed, because `bench_rotation_workers.py` found the analogous
   claim for pool width was false on this corpus.

2. **What does it save here?** Not the notebook's number. This corpus, this box,
   measured against a control run in the same session.

3. **What does it cost in memory?** `CandidateGeometryCache` is an unbounded
   `dict` of translated Shapely polygons, scoped to a run, with no eviction and
   no size cap. That is the risk, and a win of the notebook's size would be
   worthless if it meant an unbounded resident set. Measured, not reasoned about.

Methodology
-----------
Timing: interleaved round-robin within one process, minimum of N, per
`make-faster.md`'s own warning that drift swamps the effect being measured. A
control and a treatment run back to back so both see the same machine state.

Memory: **not** interleaved, and in a separate process per arm. `ru_maxrss` is
a process high-water mark and never falls, so RSS measured inside a process
that has already run the other arm cannot be attributed to either. Comparing
sticky in-process readings would manufacture a result.

Usage
-----
    NEST_BENCH_CORPUS=... NEST_BENCH_QUANTITIES='Spacer=2,...' \
    NEST_BENCH_SHEET=1200x600 NEST_BENCH_ROTATION_STEPS=8 \
    NEST_BENCH_REPS=3 $FREECAD tests/freecad_harness/bench_candidate_cache.py

Re-running the whole script re-runs both phases; pass NEST_BENCH_CACHE_MEM=0 to
skip the subprocess phase when only the timing comparison is wanted.
"""
import json
import os
import subprocess
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
for _p in (_REPO, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_common as hc  # noqa: E402


def _load_nest_benchmark():
    """Loads nest_benchmark.py as a library, by path, under a name of its own.

    NOT `import nest_benchmark`. Under freecadcmd a script runs with __name__
    set to its own basename, so nest_benchmark's guard is
    `if __name__ in ("__main__", "nest_benchmark")` -- a plain import would
    satisfy it and run a full benchmark, with a full rep loop, from inside this
    script. Loading it under a different module name means the guard cannot
    match. This is the trap harness_common.py's own docstring describes, and it
    is the reason that module is deliberately not named like a script.
    """
    import importlib.util

    path = os.path.join(_HERE, "nest_benchmark.py")
    spec = importlib.util.spec_from_file_location("nb_harness_module", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["nb_harness_module"] = module
    spec.loader.exec_module(module)
    return module


nb = _load_nest_benchmark()

STATUS_FILE = os.path.join(_HERE, ".last_status_bench_cache")
FREECADCMD = os.environ.get(
    "FREECADCMD", "/home/james/freecad_env/usr/bin/freecadcmd")

ARMS = (False, True)          # candidate_geometry_cache off, then on
CACHE_COUNTERS = (
    "candidate_geometry_cache_hits",
    "candidate_geometry_cache_misses",
    "candidate_geometry_cache_ms",
    "candidate_geometry_cache_entries",
)


def corpus_label():
    return os.environ.get("NEST_BENCH_CORPUS", "synthetic")


def fingerprint(result):
    """The packing outcome, as a comparable tuple.

    `placed_by_label` is included so a change in *which* parts were placed is
    caught even when the totals happen to match.
    """
    by_label = result.get("placed_by_label") or {}
    return (result["sheets"], result["placed"], result["unplaced"],
            result["density"],
            tuple(sorted((k, v) for k, v in by_label.items())))


def one_run(enabled, seed):
    """One cold nest at the given cache setting."""
    import FreeCAD

    cfg = nb.Config({
        "NEST_BENCH_CORPUS": corpus_label(),
        "NEST_BENCH_QUANTITIES": os.environ.get("NEST_BENCH_QUANTITIES", ""),
        "NEST_BENCH_SHEET": os.environ.get("NEST_BENCH_SHEET", "450x350"),
        "NEST_BENCH_ROTATION_STEPS": os.environ.get("NEST_BENCH_ROTATION_STEPS", "8"),
        "NEST_BENCH_QUANTITY": os.environ.get("NEST_BENCH_QUANTITY", "3"),
        "NEST_BENCH_SEED": str(seed),
        "NEST_BENCH_SPACING": os.environ.get("NEST_BENCH_SPACING", "5.0"),
        "NEST_BENCH_DEFLECTION": os.environ.get("NEST_BENCH_DEFLECTION", "0.05"),
        "NEST_BENCH_SIMPLIFICATION": os.environ.get("NEST_BENCH_SIMPLIFICATION", "0.1"),
        "NEST_BENCH_ROTATION_WORKERS": "1",
        "NEST_BENCH_CANDIDATE_GEOMETRY_CACHE": "1" if enabled else "",
    })
    cfg.apply_rotation_worker_limit()

    doc = FreeCAD.newDocument(f"cc{int(enabled)}_{seed}")
    if cfg.corpus == "synthetic":
        parts, quantities = nb.build_synthetic_corpus(doc, cfg.seed, cfg.quantity)
    else:
        if not os.path.exists(cfg.corpus):
            raise FileNotFoundError(cfg.corpus)
        source = FreeCAD.openDocument(cfg.corpus)
        parts = nb.discover_doc_parts(source)
        quantities = {p.Label: cfg.per_label_quantities.get(p.Label, cfg.quantity)
                      for p in parts}

    result, perf, _dead_ring, wall, peak_rss = nb.run_nest(
        doc, parts, quantities, cfg)
    counters = {k: perf.get(k) for k in CACHE_COUNTERS}
    return {
        "wall": wall,
        "peak_rss": peak_rss,
        "result": result,
        "fingerprint": fingerprint(result),
        "cache": counters,
        "candidate_geometry_ms": perf.get("candidate_geometry_ms"),
        "candidate_geometries_built": perf.get("candidate_geometries_built"),
        "candidate_geometry_observations": perf.get("candidate_geometry_observations"),
    }


def timing_phase(reps):
    hc.emit("=== timing: interleaved A/B, one process ===")
    runs = {arm: [] for arm in ARMS}
    for rep in range(reps):
        for arm in ARMS:
            r = one_run(arm, 20260925)
            runs[arm].append(r)
            hits = r["cache"]["candidate_geometry_cache_hits"]
            misses = r["cache"]["candidate_geometry_cache_misses"]
            hc.emit(f"  rep {rep}  cache={'on ' if arm else 'off'}  "
                    f"wall {r['wall']:7.3f}s  placed {r['result']['placed']:>3}  "
                    f"sheets {r['result']['sheets']}  "
                    f"cand_geom {r['candidate_geometry_ms']:.0f}ms  "
                    f"hits/misses {hits}/{misses}")
    return runs


def report_timing(runs):
    hc.emit("")
    hc.emit("=== summary: min of N is the estimator, per make-faster.md ===")
    hc.emit(f"  {'cache':>6} {'min':>9} {'median':>9} {'spread':>8}  "
            f"{'cand_geom ms':>13} {'hit rate':>9}")
    stats = {}
    for arm in ARMS:
        walls = [r["wall"] for r in runs[arm]]
        cg = [r["candidate_geometry_ms"] for r in runs[arm]]
        hits = sum(r["cache"]["candidate_geometry_cache_hits"] or 0 for r in runs[arm])
        misses = sum(r["cache"]["candidate_geometry_cache_misses"] or 0 for r in runs[arm])
        rate = f"{100.0 * hits / (hits + misses):.1f}%" if (hits + misses) else "-"
        stats[arm] = {"min": min(walls), "median": sorted(walls)[len(walls) // 2],
                      "spread": max(walls) - min(walls),
                      "cand_geom": min(cg), "hits": hits, "misses": misses}
        hc.emit(f"  {'on' if arm else 'off':>6} {min(walls):>9.3f} "
                f"{stats[arm]['median']:>9.3f} {stats[arm]['spread']:>8.3f}  "
                f"{min(cg):>13.0f} {rate:>9}")

    off, on = stats[False], stats[True]
    hc.emit("")
    hc.emit(f"  candidate_geometry work: {off['cand_geom']:.0f}ms -> "
            f"{on['cand_geom']:.0f}ms "
            f"({100.0 * (on['cand_geom'] - off['cand_geom']) / off['cand_geom']:+.1f}%)")
    hc.emit(f"  total wall:              {off['min']:.3f}s -> {on['min']:.3f}s "
            f"({on['min'] / off['min']:.3f}x)")

    # Is the effect larger than the noise it was measured through?
    spread_pct = 100.0 * off["spread"] / off["min"] if off["min"] else 0.0
    delta_pct = 100.0 * (on["min"] - off["min"]) / off["min"] if off["min"] else 0.0
    hc.emit("")
    if abs(delta_pct) < spread_pct:
        hc.emit(f"  VERDICT: the {delta_pct:+.1f}% change is SMALLER than the "
                f"{spread_pct:.1f}% spread within the control arm.")
        hc.emit("  Not distinguishable from noise on this box. More reps, or a "
                "heavier corpus.")
    else:
        hc.emit(f"  VERDICT: the {delta_pct:+.1f}% change exceeds the "
                f"{spread_pct:.1f}% control spread.")

    # Question 1: is it layout-neutral?
    hc.emit("")
    all_fp = {}
    for arm in ARMS:
        for r in runs[arm]:
            all_fp.setdefault(r["fingerprint"], []).append(arm)
    hc.emit(f"=== distinct packing results across all runs: {len(all_fp)} ===")
    for fp, arms in sorted(all_fp.items(), key=lambda kv: -len(kv[1])):
        hc.emit(f"  sheets={fp[0]} placed={fp[1]} unplaced={fp[2]} "
                f"density={fp[3]:.6f}  arms {sorted('on' if a else 'off' for a in arms)}")
    if len(all_fp) == 1:
        hc.emit("  -> packing result IS identical with and without the cache, so "
                "it is a pure speed knob on this corpus")
    else:
        hc.emit("  -> packing result DIFFERS. The cache is a behaviour change, not "
                "a speed knob, and no timing result justifies shipping it as one.")
    return stats, len(all_fp) == 1


def memory_phase(reps):
    """One subprocess per arm, reporting absolute peak RSS.

    Separate processes because ru_maxrss is monotonic: a reading taken in a
    process that has already run the other arm is contaminated by it.
    """
    hc.emit("")
    hc.emit("=== memory: one process per arm, absolute peak RSS ===")
    hc.emit("  (ru_maxrss is a high-water mark and never falls, so this cannot "
            "be done in-process)")
    out = {}
    for arm in ARMS:
        samples = []
        for _rep in range(reps):
            env = dict(os.environ)
            env["NEST_BENCH_CACHE_CHILD"] = "1" if arm else "0"
            proc = subprocess.run(
                [FREECADCMD, os.path.abspath(__file__)],
                env=env, capture_output=True, text=True, cwd=_REPO)
            line = [ln for ln in proc.stdout.splitlines() if ln.startswith("RSS_ABS_KB")]
            if not line:
                hc.emit(f"  cache={'on ' if arm else 'off'}  child produced no "
                        f"RSS line; exit {proc.returncode}")
                hc.emit("  " + (proc.stdout or proc.stderr).strip()[-400:])
                return None
            samples.append(int(line[-1].split()[1]))
        out[arm] = {"min_kb": min(samples), "max_kb": max(samples)}
        hc.emit(f"  cache={'on ' if arm else 'off'}  peak RSS "
                f"min {min(samples) / 1024:.0f} MiB  max {max(samples) / 1024:.0f} MiB")
    if out[False] and out[True]:
        delta = out[True]["max_kb"] - out[False]["max_kb"]
        hc.emit(f"  delta (on - off, worst case): {delta / 1024:+.0f} MiB")
        if delta > 200:
            hc.emit("  NOTE: over 200 MiB. CandidateGeometryCache is an unbounded "
                    "per-run dict with no eviction, so this scales with the "
                    "workload and is worth a cap before default-on.")
    return out


def child():
    """Single nest in this process, printing absolute peak RSS for the parent."""
    import FreeCAD  # noqa: F401

    r = one_run(os.environ.get("NEST_BENCH_CACHE_CHILD") == "1", 20260925)
    rss = nb._peak_rss_bytes()
    hc.emit(f"RSS_ABS_KB {rss // 1024 if rss else 0}")
    return 0


def main():
    if os.environ.get("NEST_BENCH_CACHE_CHILD"):
        return child()

    reps = int(os.environ.get("NEST_BENCH_REPS", "3"))
    hc.emit("=== candidate-geometry cache A/B ===")
    hc.emit(f"  corpus       {corpus_label()}")
    hc.emit(f"  sheet        {os.environ.get('NEST_BENCH_SHEET', '450x350')}")
    hc.emit(f"  quantities   {os.environ.get('NEST_BENCH_QUANTITIES', '(uniform)')}")
    hc.emit(f"  rot steps    {os.environ.get('NEST_BENCH_ROTATION_STEPS', '8')}")
    hc.emit(f"  reps         {reps} (interleaved)")

    runs = timing_phase(reps)
    stats, layout_neutral = report_timing(runs)

    if os.environ.get("NEST_BENCH_CACHE_MEM", "1") != "0":
        memory_phase(max(2, reps - 1))

    out = os.environ.get("NEST_BENCH_CACHE_OUT")
    if out:
        payload = {
            "corpus": corpus_label(),
            "reps": reps,
            "layout_neutral": layout_neutral,
            "arms": {
                ("on" if a else "off"): {
                    "min_wall": stats[a]["min"],
                    "median_wall": stats[a]["median"],
                    "spread": stats[a]["spread"],
                    "candidate_geometry_ms": stats[a]["cand_geom"],
                    "hits": stats[a]["hits"],
                    "misses": stats[a]["misses"],
                    "fingerprint": repr(runs[a][0]["fingerprint"]),
                } for a in ARMS
            },
        }
        os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
        with open(out, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
        hc.emit(f"\nwrote {out}")
    return 0


if __name__ in ("__main__", "bench_candidate_cache"):
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
