"""Shared helpers for the freecadcmd harness scripts.

Deliberately NOT named like a script. Under freecadcmd a script runs with
`__name__` set to its own basename, so every harness uses the guard
`if __name__ in ("__main__", "<its own name>")`. Importing one harness from
another therefore re-executes it -- which is how an early version of the GA-loop
debug script ended up running the whole suite on import. A module that never
matches any script's name cannot be re-executed by accident.

Import with the harness directory on sys.path:

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import harness_common
"""
import math
import os
import random
import types

# Inert FreeCAD stand-ins, so this module can be imported under plain CPython
# too. Harmless when the real modules are present.
for _name in ("FreeCAD", "Part", "FreeCADGui", "Draft"):
    if _name not in os.environ.get("_HARNESS_NO_STUB", "") and _name not in __import__("sys").modules:
        try:
            __import__(_name)
        except ImportError:
            _mod = types.ModuleType(_name)
            _mod.__getattr__ = lambda attr: None
            __import__("sys").modules[_name] = _mod

# FreeCAD scaffolding rather than parts: an Origin and its axes/planes/vertex
# are the children of every PartDesign::Body, and treating them as parts would
# nest noise.
SKIP_TYPEIDS = {
    "App::Origin", "App::Line", "App::Plane", "App::Point", "App::Part",
    "PartDesign::Line", "PartDesign::Plane", "PartDesign::Point",
    "PartDesign::CoordinateSystem",
}


def emit(message=""):
    """Prints a report line in a way that survives FreeCAD's console redirect.

    Under freecadcmd, FreeCAD.Console captures plain print() once a document
    exists, so report output silently disappears.
    """
    try:
        import FreeCAD

        if FreeCAD is not None and hasattr(FreeCAD, "Console"):
            FreeCAD.Console.PrintMessage(str(message) + "\n")
            return
    except Exception:
        pass
    print(message)


def build_synthetic_corpus(doc, seed, quantity):
    """The built-in deterministic corpus, shared by every harness.

    Seeded, so identical on every machine. That matters: the n70 .FCStd corpus
    is gitignored and machine-local, so a baseline keyed to it could not be
    reproduced by anyone else.

    Deliberately mixed so a run is sensitive to more than one code path:
      * three rectangles     -- convex, fast path, no decomposition
      * an L-shape           -- concave, forces triangulation
      * a triangle           -- convex but non-rectangular
      * a plate with a hole  -- the inner-fit path, which block 2.1 changes
    """
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
        add(f"Rect{i}", Part.makeBox(rng.randrange(40, 90), rng.randrange(30, 70), 10))
    add("LShape", Part.makeBox(80, 80, 10).cut(
        Part.makeBox(50, 50, 10, FreeCAD.Vector(30, 30, 0))))
    add("Triangle", Part.makePolygon([
        FreeCAD.Vector(0, 0, 0), FreeCAD.Vector(70, 0, 0),
        FreeCAD.Vector(0, 60, 0), FreeCAD.Vector(0, 0, 0),
    ]).extrude(FreeCAD.Vector(0, 0, 10)))
    add("HoledPlate", Part.makeBox(100, 100, 10).cut(
        Part.makeCylinder(22, 20, FreeCAD.Vector(50, 50, -5))))

    return parts, {p.Label: quantity for p in parts}


def discover_doc_parts(doc):
    """Top-level candidate part objects in an opened document.

    An object already inside a candidate -- a Body's Origin, a Sketch inside a
    Body -- is skipped.
    """
    candidates = [
        o for o in doc.Objects
        if o.TypeId not in SKIP_TYPEIDS
        and hasattr(o, "Shape") and o.Shape and not o.Shape.isNull()
    ]
    tops = []
    for o in candidates:
        parented = any(
            other.Name != o.Name
            and getattr(other, "Group", None)
            and any(getattr(c, "Name", None) == o.Name for c in other.Group)
            for other in candidates
        )
        if not parented:
            tops.append(o)
    return tops


def load_corpus(path, quantity=None):
    """Opens a .FCStd and returns (parts, quantities) from it."""
    import FreeCAD

    doc = FreeCAD.openDocument(path)
    parts = discover_doc_parts(doc)
    q = quantity if quantity is not None else 1
    return parts, {p.Label: q for p in parts}


def ring(cx, cy, r, n, phase=0.0):
    return [(cx + r * math.cos(2 * math.pi * i / n + phase),
             cy + r * math.sin(2 * math.pi * i / n + phase)) for i in range(n)]


def is_timing_key(key):
    """True for wall-clock counters, which are never comparable run to run.

    Matches `*_ms`, `*_ms_worst` (the phase accumulator's worst-single-NFP
    figures) and `*_s` / `*_seconds`. Misclassifying any of those is not
    cosmetic: a timing reported as a "reproducible work count that changed" is
    exactly the noise the split exists to exclude, and it trains the reader to
    ignore the report.
    """
    return (key.endswith("_ms")
            or "_ms_" in key
            or key.endswith("_s")
            or key.endswith("_seconds"))


def classify_stability(perfs, results):
    """Splits sampled fields into those that held and those that moved.

    Two tiers, and the distinction is the whole point:

      * work counts and result fields are exact at rotation width 1, so a change
        between reps means something is wrong, not that the box was busy
      * timings are never exact; a minimum over reps is the only defensible
        estimator, and the spread is worth recording so the baseline says how
        noisy it is instead of asserting one sample

    `perfs` is a list of per-rep perf dicts, `results` a list of per-rep result
    dicts. Returns (unstable_counts, unstable_timings, unstable_results).
    """
    unstable_counts, unstable_timings = [], []
    for key in perfs[0]:
        values = [p.get(key) for p in perfs]
        if len(set(values)) == 1:
            continue
        (unstable_timings if is_timing_key(key) else unstable_counts).append(key)

    def fingerprint(value):
        # placed_by_label is a dict, which is unhashable; compare structurally.
        return repr(value) if isinstance(value, (dict, list)) else value

    unstable_results = [
        k for k in results[0]
        if len({fingerprint(r.get(k)) for r in results}) > 1
    ]
    return unstable_counts, unstable_timings, unstable_results


def collapse(perfs):
    """Folds per-rep perf dicts into one: minimum for timings, one value for counts.

    Deliberately never averages a work count. A mean of a count is not a count,
    and averaging a count that *should* be deterministic is exactly how a real
    regression gets smoothed into a plausible-looking number.
    """
    out = {}
    for key in perfs[0]:
        values = [p.get(key) for p in perfs]
        out[key] = min(values) if is_timing_key(key) else values[0]
    return out


def noise_block(reps, rotation_workers, unstable_counts, unstable_timings, walls):
    """The self-describing part of a baseline: what is exact, and what is not.

    A reader of a baseline otherwise sees a hundred counters with no indication
    that some are exact and the rest are noise, and cannot tell which is safe to
    gate on.
    """
    spread = (max(walls) - min(walls)) / min(walls) if min(walls) else 0.0
    return {
        "reps": reps,
        "rotation_workers": rotation_workers,
        "wall_seconds_spread_pct": round(100.0 * spread, 2),
        "work_counts_exact": not unstable_counts,
        "work_counts_unstable": sorted(unstable_counts),
        "timings_vary": len(unstable_timings),
        "note": (
            "Work counts and result fields are exact at rotation width 1 and "
            "safe to gate. Above width 1 the tie-break draw in score_gravity "
            "runs in worker threads against one shared random.Random, so some "
            "counts move; measured on the synthetic corpus, cache hits/misses, "
            "candidate_points and exact_collision_checks move while "
            "nfp_work_convex_pairs and the packing result do not. Timings are a "
            "minimum over `reps` runs in one process and are not comparable "
            "against a stored number."),
    }
