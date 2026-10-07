"""Is a seeded nest reproducible, on either algorithm?

Written for [NEST-032] (Minkowski) and [NEST-031] (Physics), which had two
independent causes and needed both fixed before this property held at all.

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

Neither algorithm had a gated check before, so nothing would have noticed either
regression.

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


def fingerprint(seed, workers, algo="Minkowski", physics_shake=True,
               physics_direction=(0.0, 1.0)):
    """Nest once and return a digest of every placed part's (id, angle, x, y).

    `algo="Physics"` takes no pool: the physics path is single-threaded, so the
    width is meaningless there and is not varied.

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
            if algo == "Physics":
                # Reduced anneal settings purely for runtime; the draw sites
                # under test are the spawn and anneal draws, and both are
                # exercised at these values. The panel's defaults are 25/10.
                algo_kwargs.update({"anneal_steps": 5, "anneal_rot_steps": 2,
                                    "physics_direction": physics_direction,
                                    "step_size": 5.0, "max_spawn_count": 20,
                                    "max_nesting_steps": 100,
                                    "anneal_random_shake_direction":
                                        physics_shake})
            sheets = nesting_logic.nest(parts, 600.0, 300.0, 4, False, algo,
                                       None, rng=random.Random(seed),
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


def check_physics_reproducible():
    """NEST-031. Physics drew from the unseeded global module at seven sites."""
    emit("")
    emit("-- 4. Physics: the same seed gives the same nest --")
    digests = []
    for attempt in range(3):
        digest, placed = fingerprint(SEED_A, 4, algo="Physics")
        digests.append(digest)
        emit("  attempt %d -> %s (%d placed)" % (attempt + 1, digest, placed))
        if not check(digest is not None,
                     "the Physics nest produced no fingerprint"):
            return
        check(placed == EXPECTED_PLACED,
              "Physics attempt %d placed %d part(s), expected %d"
              % (attempt + 1, placed, EXPECTED_PLACED))
    check(len(set(digests)) == 1,
          "three seeded Physics runs gave %d different layouts: %s. "
          "physics_nester and base_nester must draw from the seeded rng."
          % (len(set(digests)), digests))

    other, _ = fingerprint(SEED_B, 4, algo="Physics")
    emit("  seed %d -> %s" % (SEED_B, other))
    check(other != digests[0],
          "Physics seeds %d and %d produced the same layout (%s)"
          % (SEED_A, SEED_B, digests[0]))

    # The deterministic-shake branch, which is a *different* draw site.
    # `initial_side = self.rng.choice([1, -1])` is computed unconditionally but
    # only consumed when `anneal_random_shake_direction` is false; with it on,
    # `rand_angle` replaces it (base_nester.py:158-160). Injecting a global draw
    # at `initial_side` while shake is on is therefore NOT caught -- measured,
    # status 0 -- so this second configuration is what covers that site. Without
    # it the suite would pass with one of the seven draws still unseeded.
    emit("")
    emit("-- 4b. Physics with the deterministic shake branch --")
    fixed_shake = []
    for attempt in range(2):
        digest, placed = fingerprint(SEED_A, 4, algo="Physics",
                                     physics_shake=False)
        fixed_shake.append(digest)
        emit("  attempt %d (shake off) -> %s (%d placed)"
             % (attempt + 1, digest, placed))
    check(len(set(fixed_shake)) == 1,
          "seeded Physics with anneal_random_shake_direction=False gave %d "
          "different layouts: %s" % (len(set(fixed_shake)), fixed_shake))

    # The third Physics branch: `physics_direction=None`, which draws its own
    # angle per part. Uncovered by the two above, and an injection at that site
    # passed the suite (measured, status 0) until this was added. Three
    # configurations, because the seven draw sites are not all on one path.
    emit("")
    emit("-- 4c. Physics with a randomised physics direction --")
    randomised = []
    for attempt in range(2):
        digest, placed = fingerprint(SEED_A, 4, algo="Physics",
                                     physics_direction=None)
        randomised.append(digest)
        emit("  attempt %d (direction None) -> %s (%d placed)"
             % (attempt + 1, digest, placed))
    check(len(set(randomised)) == 1,
          "seeded Physics with physics_direction=None gave %d different "
          "layouts: %s" % (len(set(randomised)), randomised))


def check_physics_without_a_seed_still_works():
    """The `kwargs.get("rng") or random` fallback must not turn into a crash.

    A direct `nest()` caller with no rng gets the global module, as before. Making
    the seeded path mandatory would break every caller outside `GACoordinator`,
    which is not a change this fix is entitled to make.
    """
    emit("")
    emit("-- 5. Physics with no rng at all still nests --")
    from freecad.nestingworkbench.Tools.Nesting.algorithms import physics_nester
    nester = physics_nester.PhysicsNester(600.0, 300.0, 4)
    emit("  PhysicsNester(...).rng is the global module: %s"
         % (nester.rng is __import__("random")))
    check(nester.rng is not None,
          "PhysicsNester with no rng has rng=None, so the first draw would "
          "raise AttributeError")

    seeded = physics_nester.PhysicsNester(600.0, 300.0, 4, rng=__import__("random").Random(5))
    check(seeded.rng is not None and seeded.rng is not nester.rng,
          "PhysicsNester ignored an rng that was passed to it")


def run():
    check_the_zero_kwarg_trap()
    prints = check_width_independence()
    if prints:
        check_the_seed_still_matters(prints)
    check_physics_reproducible()
    check_physics_without_a_seed_still_works()


try:
    run()
    emit("")
    emit("seeded-nest determinism: %d checks, %d failure(s)"
         % (_checks[0], len(_failures)))
    status = 1 if _failures else 0
except Exception:
    traceback.print_exc()
    emit("seeded-nest determinism: CRASHED")
    status = 1

with open(_STATUS_FILE, "w") as fh:
    fh.write(str(status))