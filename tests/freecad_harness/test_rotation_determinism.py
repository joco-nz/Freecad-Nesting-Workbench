"""Does a seeded nest depend on the rotation pool width?

Written for [NEST-032], where a seeded nest depended on the rotation pool width.

**One cause, not two.** The first analysis blamed `as_completed` fold order --
`find_best_placement` folded in completion order and `_absorb_rotation_result`
picks the winner with a strict `<`, so an exactly-tied metric went to whichever
rotation finished first. That is real, and fixing it was tried first. It did not
work: three processes gave `3955f9f7` / `51fcb53b` / `fc13a933` *after* that
change. Reverting it and minting a per-rotation rng instead gives width
independence on its own, so the fold-order fix was **dropped rather than
committed** -- see the injection list below, where reintroducing it changes
nothing.

**The actual cause.** `score_gravity` breaks a metric tie with `rng.randrange`
(`minkowski_engine.py:617`), and every rotation's evaluation was handed the
*same* `random.Random`. Under the pool that draw happens concurrently from
several worker threads, so which tie-index each rotation received depended on
interleaving. Measured with a shim recording the thread of each draw: **29
distinct drawing threads** for one seeded pooled run, against **1** with
`NESTING_ROTATION_WORKERS=0`.

The fix keeps the random tie-break -- always taking `tied[0]` would bias
placement toward whichever candidate is first -- while making each rotation's
draw depend only on its own index.

**What this asserts**, same seed 777, on `replay-fixture-CAM.FCStd` with three
part types at quantities 23/23/2:

* pool widths 0, 4, 8 and 16 all produce the **same** 48-placement fingerprint.
  Width independence is the whole claim; a single width proving reproducibility
  would not, because a fixed pool can be accidentally repeatable.
* two different seeds produce **different** fingerprints -- without this, a
  frozen rng would pass every assertion above.
* 48 parts are placed, so the fingerprint is of a real layout and not of a
  partially-failed run.

Injection-verified, five ways. Reintroducing the arrival-order fold the fix
deliberately does *not* include: **no effect** (`ee223895` at both widths), which
is the measurement that removed it. One shared rng across all rotations: caught,
`da8e2bdf` vs `8df33f69`. Every rotation sharing `rotation_rngs[0]`: caught.
A frozen seed replacing `self.rng.getrandbits(64)`: caught by the seed check,
since seeds 777 and 778 then agree -- the assertion that makes the rest
meaningful.

Reads the placement arrays `nest()` returns, so nothing is measured off a saved
document.

Gated via `run.sh`. Writes `.last_status_rotationdet`; 0 pass, 1 fail.
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import FreeCAD

_STATUS_FILE = os.path.join(_HERE, ".last_status_rotationdet")

_failures = []
_checks = [0]

# Widths deliberately span "no pool", the default (this box has 4 cores), and
# two over-subscribed values. 16 exceeds the core count on purpose: if any
# ordering dependence survived, oversubscription is the likeliest to expose it.
WIDTHS = (0, 4, 8, 16)
SEED_A = 777
SEED_B = 778
EXPECTED_PLACED = 48


def emit(message=""):
    try:
        FreeCAD.Console.PrintMessage(str(message) + "\n")
    except Exception:
        pass


def check(condition, detail):
    _checks[0] += 1
    if not condition:
        _failures.append(detail)
        emit("  FAIL: %s" % detail)
    return bool(condition)


def fingerprint(seed, workers):
    """Nest once and return a digest of every placed part's (id, angle, x, y).

    `NESTING_ROTATION_WORKERS` rather than the `rotation_workers` kwarg, because
    `_rotation_worker_limit` treats an explicit 0 as *unset* and falls back to
    `os.cpu_count()` -- measured: `{'rotation_workers': 0}` resolves to 4. Only the
    environment variable reaches 0. Asserted in `check_the_zero_kwarg_trap`.
    """
    import hashlib
    import random

    from freecad.nestingworkbench.datatypes.shape import Shape
    from freecad.nestingworkbench.Tools.Nesting import nesting_logic
    from freecad.nestingworkbench.Tools.Nesting.shape_preparer import ShapePreparer

    previous = os.environ.get("NESTING_ROTATION_WORKERS")
    os.environ["NESTING_ROTATION_WORKERS"] = str(workers)
    try:
        doc = FreeCAD.openDocument(
            os.path.abspath(os.path.join(_REPO, "tests", "Test_Files",
                                         "replay-fixture-CAM.FCStd")))
        try:
            sources = {o.Label: o for o in doc.Objects
                       if o.TypeId == "PartDesign::Body"
                       and o.Label in ("BottomStrap", "TopStrap", "SimpleSpacer")}
            if len(sources) != 3:
                return None, 0

            # NestingController._prepare_source_parts does this before a real
            # run. Skipping it produced 2031 mm3 of genuinely shared solid in
            # NEST-029's re-nest work, because BottomStrap arrives pre-positioned
            # at base=(-42, 0, 0).
            for obj in sources.values():
                obj.Placement = FreeCAD.Placement()
            doc.recompute()

            preparer = ShapePreparer(doc, {})
            target = doc.addObject("App::DocumentObjectGroup", "Layout_target")
            # Per-label dicts, not bare counts: `prepare_parts` reads
            # `part_params.get('up_direction')` (shape_preparer.py:130), so an int
            # raises AttributeError there.
            quantities = {
                label: {"quantity": count, "rotation_steps": 4,
                        "up_direction": "Z+", "fill_sheet": False}
                for label, count in (("BottomStrap", 23), ("TopStrap", 23),
                                     ("SimpleSpacer", 2))
            }
            ui_params = {
                "sheet_width": 600.0, "sheet_height": 300.0,
                "sheet_thickness": 2.0, "spacing": 4.0, "deflection": 0.1,
                "simplification": 0.3, "rotation_steps": 4, "add_labels": False,
                "font_path": "", "show_bounds": False, "label_height": 25.0,
                "label_size": 10.0, "verbose": False,
                "performance_logging": False, "algorithm": "Minkowski",
                "compactness_weight": 0.0, "generations": 2,
                "population_size": 2, "random_seed": seed,
                "deflection_angle": 20.0, "nesting_direction": 90,
                "use_random_direction": False,
            }
            algo_kwargs = {"verbose": False, "performance_logging": False,
                           "spacing": 4.0, "search_direction": (0.0, 1.0)}
            if workers == 0:
                # Belt and braces: the kwarg path cannot express "no pool" (see
                # the docstring above), so it is only set when it agrees with the
                # environment rather than pretending to be the mechanism.
                algo_kwargs["rotation_workers"] = 1

            Shape.clear_nfp_cache()
            Shape.clear_caches()
            parts = preparer.prepare_parts(ui_params, quantities, sources,
                                           target, None)
            sheets = nesting_logic.nest(parts, 600.0, 300.0, 4, False,
                                       "Minkowski", None,
                                       rng=random.Random(seed),
                                       **algo_kwargs)[0]
            rows = sorted(
                (str(getattr(p.shape, "id", "?")), round(p.angle, 4),
                 round(p.x, 4), round(p.y, 4))
                for sheet in sheets for p in sheet.parts)
            blob = "|".join("%s:%s:%s:%s" % r for r in rows)
            return hashlib.sha256(blob.encode()).hexdigest()[:16], len(rows)
        finally:
            FreeCAD.closeDocument(doc.Name)
    finally:
        if previous is None:
            os.environ.pop("NESTING_ROTATION_WORKERS", None)
        else:
            os.environ["NESTING_ROTATION_WORKERS"] = previous


def check_the_zero_kwarg_trap():
    """The reason this suite uses the environment variable, asserted."""
    emit("")
    emit("-- 1. rotation_workers=0 does not mean serial --")
    from freecad.nestingworkbench.Tools.Nesting.algorithms.nesting_strategy import (
        _rotation_worker_limit)
    resolved = _rotation_worker_limit({"rotation_workers": 0})
    emit("  _rotation_worker_limit({'rotation_workers': 0}) -> %s"
         % resolved)
    emit("  os.cpu_count() -> %s" % os.cpu_count())
    check(resolved != 0,
          "rotation_workers=0 now resolves to 0; if the kwarg path learned to "
          "express serial, this suite should switch to it and say so")


def check_width_independence():
    emit("")
    emit("-- 2. the same seed nests identically at every pool width --")
    prints = {}
    for width in WIDTHS:
        digest, placed = fingerprint(SEED_A, width)
        prints[width] = digest
        emit("  width %-3d -> %s (%d placed)" % (width, digest, placed))
        if not check(digest is not None,
                     "the nest produced no fingerprint at width %d" % width):
            return None
        check(placed == EXPECTED_PLACED,
              "width %d placed %d part(s), expected %d -- a partial run would "
              "make the fingerprint comparison meaningless"
              % (width, placed, EXPECTED_PLACED))

    unique = set(prints.values())
    if not check(len(unique) == 1,
                 "the same seed gave %d different layouts across widths %s: %s. "
                 "The pool width must not affect the result."
                 % (len(unique), list(WIDTHS), prints)):
        return None
    emit("  all widths agree on %s" % unique.pop())
    return prints


def check_the_seed_still_matters(prints):
    emit("")
    emit("-- 3. a different seed gives a different nest --")
    digest_a, placed = fingerprint(SEED_A, WIDTHS[0])
    digest_b, _ = fingerprint(SEED_B, WIDTHS[0])
    emit("  seed %d -> %s" % (SEED_A, digest_a))
    emit("  seed %d -> %s" % (SEED_B, digest_b))
    check(digest_a != digest_b,
          "seeds %d and %d produced the same layout (%s). If the rng is not "
          "reaching the nester at all, every other assertion in this file "
          "passes trivially." % (SEED_A, SEED_B, digest_a))
    check(digest_a == prints.get(WIDTHS[0]),
          "re-running seed %d at width %d gave %s, not %s -- the suite itself "
          "is not repeatable" % (SEED_A, WIDTHS[0], digest_a,
                                 prints.get(WIDTHS[0])))


def run():
    check_the_zero_kwarg_trap()
    prints = check_width_independence()
    if prints:
        check_the_seed_still_matters(prints)


try:
    run()
    emit("")
    emit("rotation determinism: %d checks, %d failure(s)"
         % (_checks[0], len(_failures)))
    status = 1 if _failures else 0
except Exception:
    traceback.print_exc()
    emit("rotation determinism: CRASHED")
    status = 1

with open(_STATUS_FILE, "w") as fh:
    fh.write(str(status))