"""Structural checks on PlacementOptimizer's perf-counter plumbing.

Found by being bitten. A counter was added to the per-future absorption
allowlist but not to the `_perf_stats` initialiser. The absorption does
`self._perf_stats[key] += ...`, so that raised KeyError inside the per-rotation
future loop -- and because `quiet=True` routes `self.log` to nothing, every
exception there is swallowed with no output at all. The run then reported
**zero placed parts and zero NFP computations** while looking otherwise
healthy, and the harness's own self-checks could only say "the phase accumulator
is not reporting", which is a symptom three steps removed from the cause.

The harness did catch it, which is the argument for the harness. But it could
not name the bug. These tests can, because they read the source and compare the
two lists -- a property of the code, not of a run.

The invariant: **any key that is incremented on `self._perf_stats` must exist in
the `_perf_stats` initialiser.** Checked from both directions, and structurally
rather than by line range, so moving code does not silently narrow the check.

Also pins that `_holes_touched` -- documented as measurement-only -- is
genuinely unreachable when there is no probe. That being true in a comment is
not enough: it is a geometric test, and if it ran in production its cost would
be charged to the feature it was built to measure.
"""
import ast
import os
import re

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STRATEGY = os.path.join(
    _REPO, "freecad", "nestingworkbench", "Tools", "Nesting", "algorithms",
    "nesting_strategy.py")
COORDINATOR = os.path.join(
    _REPO, "freecad", "nestingworkbench", "Tools", "Nesting", "ga_coordinator.py")
ENGINE = os.path.join(
    _REPO, "freecad", "nestingworkbench", "Tools", "Nesting", "algorithms",
    "minkowski_engine.py")

_TREE = ast.parse(open(STRATEGY).read())


def _is_stats_attr(node, attr="_perf_stats"):
    return (isinstance(node, ast.Attribute)
            and node.attr == attr
            and isinstance(node.value, ast.Name)
            and node.value.id == "self")


def _schema_keys(attr="_perf_stats", tree=None):
    """Keys present in a `self.<attr> = {...}` initialiser."""
    keys = set()
    for node in ast.walk(tree if tree is not None else _TREE):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            if any(_is_stats_attr(t, attr) for t in node.targets):
                for key in node.value.keys:
                    if isinstance(key, ast.Constant) and isinstance(key.value, str):
                        keys.add(key.value)
    return keys


def _incremented_keys():
    """Every key the code adds to, on `self._perf_stats`.

    Covers both spellings: `self._perf_stats['k'] += v` and the loop form
    `for k in (...): self._perf_stats[k] += v`, which is what a new counter
    normally gets added to.
    """
    literal_keys = set()
    loop_keys = set()

    for node in ast.walk(_TREE):
        if isinstance(node, ast.AugAssign) and _is_stats_attr(node.target):
            target = node.target
            if isinstance(target, ast.Subscript):
                key = target.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    literal_keys.add(key.value)

        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            loop_vars = {t.id for t in ast.walk(node.target)
                         if isinstance(t, ast.Name)}
            body = ast.Module(body=node.body, type_ignores=[])
            for sub in ast.walk(body):
                if not (isinstance(sub, ast.AugAssign)
                        and isinstance(sub.target, ast.Subscript)
                        and _is_stats_attr(sub.target.value)):
                    continue
                # The subscript must be the loop variable, i.e. `_perf_stats[key]`
                # and not `_perf_stats['literal']`.
                key_node = sub.target.slice
                if isinstance(key_node, ast.Name) and key_node.id in loop_vars:
                    for element in node.iter.elts:
                        if (isinstance(element, ast.Constant)
                                and isinstance(element.value, str)):
                            loop_keys.add(element.value)
    return literal_keys, loop_keys


class TestCounterPlumbing:
    def test_extractors_find_something(self):
        """Guard the guard. An extractor that silently matches nothing would
        make every other test in this file pass vacuously -- which is how the
        first draft of it managed to report 0 allowlist keys and still look
        green."""
        literals, looped = _incremented_keys()
        assert len(_schema_keys()) > 20, "the _perf_stats initialiser was not found"
        assert len(looped) > 5, f"no loop-based absorption found: {looped}"

    def test_every_incremented_counter_exists_in_the_schema(self):
        """The KeyError bug, in the loop form a new counter is added in."""
        schema = _schema_keys()
        _, looped = _incremented_keys()
        missing = sorted(looped - schema)
        assert not missing, (
            f"counters incremented with `+=` but absent from the `_perf_stats` "
            f"initialiser, so each raises KeyError inside the per-rotation "
            f"future loop: {missing}\n"
            "With quiet=True the exception is swallowed with no output, every "
            "rotation evaluation dies, and the run reports zero placed parts "
            "and zero NFP computations while looking healthy.")

    def test_every_literal_counter_exists_in_the_schema(self):
        """The same invariant for the `self._perf_stats['k'] += v` form."""
        schema = _schema_keys()
        literals, _ = _incremented_keys()
        missing = sorted(literals - schema)
        assert not missing, (
            f"counters incremented directly but absent from the initialiser: "
            f"{missing}")

    def test_collision_sub_timers_are_in_the_schema(self):
        """The three sub-timers added to decompose the collision stage, and the
        three call counts beside them. Named explicitly because they are what a
        future reader looks for when asking where collision time goes."""
        schema = _schema_keys()
        for key in ("collision_intersection_ms", "collision_intersects_ms",
                    "collision_overlay_ms", "collision_hole_probe_ms",
                    "exact_collision_checks", "collision_intersects_true",
                    "collision_intersects_false",
                    "collision_intersects_calls", "collision_overlay_calls",
                    "collision_hole_probe_calls"):
            assert key in schema, f"{key} missing from the _perf_stats schema"

    def test_hole_probe_is_gated_on_the_probe_argument(self):
        """`_holes_touched` must be unreachable without a probe.

        Walks the same tree the call sites were found in. The first draft
        re-parsed the file inside the ancestor walk, so the node it was asked
        about belonged to a different tree, the parent map never matched, and
        the walk returned nothing -- which would have made this test fail for
        every call site, or pass for none, depending on how it was written.
        """
        parents = {}
        for parent in ast.walk(_TREE):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        sites = 0
        for node in ast.walk(_TREE):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "_holes_touched"):
                continue
            sites += 1
            assert _guarded_by_probe(node, parents), (
                "_holes_touched is reachable without a probe or hole_geoms "
                "guard, so the measurement-only probe runs in production")

        assert sites >= 2, (
            f"expected both collision branches to call _holes_touched, found "
            f"{sites}; the check is vacuous if the call sites move")


def _guarded_by_probe(node, parents):
    """True if any enclosing `if` tests `probe` or `hole_geoms` for None.

    Either is sound: `hole_geoms` is itself only populated under
    `if probe is not None`, and the guard is a short-circuit `and`, so the call
    cannot be reached unless one of them holds.
    """
    current = node
    while current in parents:
        current = parents[current]
        if not isinstance(current, ast.If):
            continue
        test = ast.unparse(current.test)
        if "probe is not None" in test or "hole_geoms is not None" in test:
            return True
    return False


# ---------------------------------------------------------------------------
# The same trap, one module over.
# ---------------------------------------------------------------------------
_COORD_TREE = ast.parse(open(COORDINATOR).read())
_ENGINE_TREE = ast.parse(open(ENGINE).read())


def _ga_allowlist(tree):
    """Every key accumulated into `_ga_perf` by any route.

    Both the explicit form (`self._ga_perf['k'] += stats.get('k_ms', 0)/1000`,
    which converts) and the loop form (`for k in (...): self._ga_perf[k] += ...`,
    which adds raw). This is what the schema check wants: everything touched.
    """
    keys = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.AugAssign)
                and isinstance(node.target, ast.Subscript)
                and _is_stats_attr(node.target.value, "_ga_perf")):
            key = node.target.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                keys.add(key.value)
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            loop_vars = {t.id for t in ast.walk(node.target)
                         if isinstance(t, ast.Name)}
            body = ast.Module(body=node.body, type_ignores=[])
            for sub in ast.walk(body):
                if not (isinstance(sub, ast.AugAssign)
                        and isinstance(sub.target, ast.Subscript)
                        and _is_stats_attr(sub.target.value, "_ga_perf")):
                    continue
                key_node = sub.target.slice
                if isinstance(key_node, ast.Name) and key_node.id in loop_vars:
                    for element in node.iter.elts:
                        if (isinstance(element, ast.Constant)
                                and isinstance(element.value, str)):
                            keys.add(element.value)
    return keys


def _ga_loop_allowlist(tree):
    """Only the loop-form keys -- the ones added raw, with no unit conversion.

    The loop is `self._ga_perf[key] += stats.get(key, 0)`, so a millisecond key
    placed in it lands in a seconds dict unscaled. An explicit line can convert,
    so it is not this function's business.
    """
    keys = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.For)
                and isinstance(node.iter, (ast.Tuple, ast.List))):
            continue
        loop_vars = {t.id for t in ast.walk(node.target) if isinstance(t, ast.Name)}
        body = ast.Module(body=node.body, type_ignores=[])
        for sub in ast.walk(body):
            if not (isinstance(sub, ast.AugAssign)
                    and isinstance(sub.target, ast.Subscript)
                    and _is_stats_attr(sub.target.value, "_ga_perf")):
                continue
            key_node = sub.target.slice
            if isinstance(key_node, ast.Name) and key_node.id in loop_vars:
                for element in node.iter.elts:
                    if (isinstance(element, ast.Constant)
                            and isinstance(element.value, str)):
                        keys.add(element.value)
    return keys


def _perf_stats_literals(tree, attr="_perf_stats"):
    """Key sets of every `self.<attr> = {...}` initialiser, as separate sets.

    Returned as a list rather than a union on purpose: the hazard is a key
    missing from one of them, and a union cannot express that.
    """
    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)
                and any(_is_stats_attr(t, attr) for t in node.targets)):
            out.append({key.value for key in node.value.keys
                        if isinstance(key, ast.Constant)
                        and isinstance(key.value, str)})
    return out


def _ms_to_s_pairs(tree):
    """The literal `(ms_key, s_key)` pairs of an explicit unit-conversion loop.

    Matches the shape

        for _ms_key, _s_key in (('a_ms', 'a_s'), ...):
            self._ga_perf[_s_key] = self._ga_perf.get(...) + stats.get(_ms_key, 0)/1000

    The pair is resolved out of the iterable's own tuples, not out of the body.
    An earlier version walked the body for the subscript, which only ever found
    the *variable* names -- so it reported the pairs ('_ms_key', '_s_key') and
    asserted that a variable existed in the schema, passing while checking
    nothing.

    The body is still consulted, to tie the tuple to a real conversion: a loop
    that iterates the pairs without writing `_s_key` into `_ga_perf` is not one
    of these and must not be read as one.
    """
    pairs = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.For)
                and isinstance(node.iter, (ast.Tuple, ast.List))
                and isinstance(node.target, ast.Tuple)):
            continue
        names = [t.id for t in node.target.elts if isinstance(t, ast.Name)]
        if len(names) != 2:
            continue
        # Does the body assign through the second variable into `_ga_perf`?
        writes_second = any(
            isinstance(sub, ast.Assign) and sub.targets
            and isinstance(sub.targets[0], ast.Subscript)
            and _is_stats_attr(sub.targets[0].value, "_ga_perf")
            and isinstance(sub.targets[0].slice, ast.Name)
            and sub.targets[0].slice.id == names[1]
            for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])))
        if not writes_second:
            continue
        for element in node.iter.elts:
            if (isinstance(element, (ast.Tuple, ast.List))
                    and len(element.elts) == 2
                    and all(isinstance(e, ast.Constant)
                            and isinstance(e.value, str) for e in element.elts)):
                pairs.add((element.elts[0].value, element.elts[1].value))
    return pairs


def _ga_schema():
    return _schema_keys("_ga_perf", _COORD_TREE)


class TestGaCounterPlumbing:
    """`_ga_perf` has the same allowlist-versus-initialiser hazard.

    It bit once already, in `nesting_strategy`: a key in the absorption list but
    not in the initialiser raises KeyError inside the per-rotation future loop,
    and `quiet=True` routes the log nowhere, so every rotation evaluation dies
    silently and the run reports zero placed parts. `_record_nest_perf` copies an
    explicit allowlist into `_ga_perf`, so the same mistake is available here.
    """

    def test_extractors_find_something(self):
        assert len(_ga_schema()) > 40, "the _ga_perf initialiser was not found"
        assert len(_ga_allowlist(_COORD_TREE)) > 40, "no _ga_perf absorption found"

    def test_every_absorbed_counter_exists_in_the_schema(self):
        missing = sorted(_ga_allowlist(_COORD_TREE) - _ga_schema())
        assert not missing, (
            f"counters accumulated into _ga_perf but absent from its initialiser, "
            f"so each raises KeyError: {missing}\n"
            "With quiet=True the exception is swallowed with no output and the "
            "run places nothing while reporting no error.")

    def test_overlay_rejection_split_is_recorded(self):
        """The split that decides whether a predicate could replace the overlay.

        `collision_grazing_pairs` conflates an exact touch with a
        positive-but-negligible sliver. Both reaching `_ga_perf` is what lets
        RESULTS-collision.md's open question be answered on a GA run rather than
        only on a single-nest probe.
        """
        schema = _ga_schema()
        for key in ("collision_overlay_area_zero",
                    "collision_overlay_area_sub_tol",
                    "collision_overlay_area_over_tol",
                    "collision_overlay_area_total",
                    "collision_intersects_calls", "collision_overlay_calls"):
            assert key in schema, f"{key} missing from the _ga_perf initialiser"
            assert key in _ga_allowlist(_COORD_TREE), (
                f"{key} is initialised but never accumulated, so it reads zero")

    def test_timings_are_converted_to_seconds(self):
        """The `_ga_perf` timings are seconds; the stats they absorb are ms.

        The conversion has to be explicit, because the allowlist loop adds raw.
        The first version of the collision sub-timers was absorbed, and read
        22184.8 next to a collision total of 89.6 -- a factor of a thousand, in
        the same dict, with nothing to say so.
        """
        schema = _ga_schema()
        raw = _ga_loop_allowlist(_COORD_TREE)
        # The conversion loop itself, not just the keys it produces. Asserting
        # only that the `_s` keys are in the schema passes vacuously: deleting a
        # line from the loop leaves every schema entry intact and every number
        # reading zero, which is what happened to the first version of these.
        converted = _ms_to_s_pairs(_COORD_TREE)
        assert converted, "the ms->s conversion loop was not found"
        for source, target in sorted(converted):
            assert target in schema, (
                f"{target} is produced by the conversion loop but absent from "
                f"the _ga_perf initialiser, so the `get` defaults it to 0 and "
                f"the figure never appears")
            assert source not in raw, (
                f"{source} is both converted explicitly and absorbed raw, so "
                f"it is counted twice at two different scales")
            assert source.endswith('_ms'), (
                f"{source} is not named like a millisecond value, and the "
                f"conversion divides it by 1000 on the assumption that it is")
        for key in ("collision_intersects_s", "collision_overlay_s",
                    "collision_hole_probe_s", "collision_intersection_s",
                    "candidate_generation_s", "candidate_incremental_s"):
            assert key in schema, f"{key} missing from the _ga_perf initialiser"
        for source, target in (("nfp_candidate_generation_ms",
                                "candidate_generation_s"),
                               ("nfp_candidate_incremental_ms",
                                "candidate_incremental_s")):
            assert (source, target) in converted, (
                f"the conversion loop no longer maps {source} -> {target}, so "
                f"{target} reads 0 with no error")
            assert key not in raw, (
                f"{key} is in the loop allowlist, which adds raw, so it would "
                f"land in milliseconds beside seconds")
        for key in ("collision_intersects_ms", "collision_overlay_ms",
                    "nfp_candidate_generation_ms",
                    "nfp_candidate_incremental_ms"):
            assert key not in raw, (
                f"{key} is a millisecond key in the raw allowlist")
            assert key not in schema, (
                f"{key} is a millisecond key in a seconds dict")

    def test_candidate_generation_is_measured_end_to_end(self):
        """The NFP-to-candidates stage has a GA-level figure.

        It is the stage between "the NFP exists" and "candidate points exist",
        and until now it had none: the engine computed the timing and only
        logged it, so `nfp_compute` was being read as the whole NFP cost when it
        covers only the miss path that builds the NFP.

        The engine prefixes its own keys with `nfp_` when the nester reports
        them, so both the counts and the converted timings have to be present at
        the GA level or the number is computed and then dropped.
        """
        schema = _ga_schema()
        for key in ("candidate_generation_s", "candidate_incremental_s",
                    "nfp_candidate_generation_calls",
                    "nfp_candidate_incremental_calls",
                    "nfp_candidate_points_returned"):
            assert key in schema, f"{key} missing from the _ga_perf initialiser"
        for key in ("nfp_candidate_generation_calls",
                    "nfp_candidate_incremental_calls",
                    "nfp_candidate_points_returned"):
            assert key in _ga_allowlist(_COORD_TREE), (
                f"{key} is initialised but never absorbed, so it reads zero")

    def test_engine_declares_every_counter_it_accumulates(self):
        """The engine's own `_perf_stats` has the keys it increments.

        Its two initialisers -- the constructor and the reset -- are separate
        literals, so a key added to one and not the other is reported by the
        nester and then dropped by the reset halfway through a run. That is the
        same shape of hazard as the GA allowlist one module over, and silent in
        the same way.
        """
        literals = _perf_stats_literals(_ENGINE_TREE)
        assert len(literals) == 2, (
            f"expected the engine to have 2 _perf_stats initialisers "
            f"(constructor and reset), found {len(literals)}")
        # Compared per literal, not merged. Merging finds nothing wrong, because
        # a key present in either one satisfies the merged set -- which is the
        # defect: the reset wipes the counters, so a key present only in the
        # constructor reads non-zero until the first reset and zero forever
        # after, and nothing raises.
        base = literals[0]
        for index, keys in enumerate(literals[1:], start=1):
            missing = sorted(base - keys)
            assert not missing, (
                f"_perf_stats initialiser #{index} is missing {missing}, which "
                f"initialiser #0 has. The reset discards them, so those "
                f"counters read non-zero until the first reset and zero after, "
                f"with no error.")
        for key in ("candidate_generation_ms", "candidate_generation_calls",
                    "candidate_incremental_ms", "candidate_incremental_calls",
                    "candidate_points_returned"):
            assert key in base, (
                f"the engine accumulates {key} but no initialiser declares it, "
                f"so the first reset silently discards it")


# ===========================================================================
# The two performance dials must be reachable from the panel, not just the env.
# ===========================================================================
PANEL = os.path.join(_REPO, "freecad", "nestingworkbench", "Tools", "Nesting",
                     "ui_nesting.py")
CONTROLLER = os.path.join(_REPO, "freecad", "nestingworkbench", "Tools",
                          "Nesting", "nesting_controller.py")
_PANEL_SRC = open(PANEL).read()
_CTRL_SRC = open(CONTROLLER).read()


def _flat(text):
    """Collapse runs of whitespace, so an assertion can match a wrapped line.

    Matching source text literally is brittle in a way that produces false
    negatives: `rotation_workers` is assigned across two lines for line length,
    and the first version of this test could not see it. It also produces false
    positives, because a substring survives reformatting that changes what it
    refers to. Whitespace is the only difference that does not matter here.
    """
    return re.sub(r"\s+", " ", text)


class TestPerformanceDialsAreReachable:
    """step_size and the rotation pool width must be settable without an env var.

    Both were measured to be the largest levers in a run -- the pool width 22%
    of wall clock, step_size up to 54% -- and both were reachable only through
    `NESTING_ROTATION_WORKERS` / `NESTING_STEP_SIZE`. A user launching FreeCAD
    from a desktop icon cannot set those, so as shipped the two biggest knobs in
    the workbench were unusable by the person using it.

    Structural rather than behavioural, because the panel needs Qt. The GUI probe
    `probe_ui_performance_dials.py` is the behavioural check: it builds the
    panel, moves both fields, and asserts they reach the nester and survive a
    preferences round trip.
    """

    def test_panel_declares_both_fields(self):
        for widget, default in (("minkowski_step_size_input", "5.0"),
                                ("minkowski_rotation_workers_input", "0")):
            flat = _flat(_PANEL_SRC)
            assert widget in _PANEL_SRC, f"{widget} is not declared in the panel"
            assert f"self.{widget}.setValue({default})" in flat, (
                f"{widget} default should be {default} so leaving the panel "
                f"alone reproduces the previous behaviour")

    def test_fields_are_in_the_minkowski_group(self):
        """They must land in the Minkowski group, not the Physics one.

        step_size already existed on the Physics panel, where it does nothing for
        Minkowski -- that is the gap that started this. A field that exists but
        is wired to the other algorithm looks correct and is inert.
        """
        assert "minkowski_form_layout.addRow(perf_form_layout)" \
            in _flat(_PANEL_SRC), (
            "the performance fields are not added to the Minkowski form layout")
        flat = _flat(_PANEL_SRC)
        group = flat.index("minkowski_form_layout.addRow(perf_form_layout)")
        physics = flat.index("physics_form_layout = ")
        assert group < physics, "the fields are added after the Physics layout"

    def test_controller_passes_both_to_the_nester(self):
        """Scoped to the Minkowski `else:` branch, located by AST.

        Three attempts at this check failed in three different ways, all from
        slicing the source as text:

          - `"algo_kwargs['step_size']" in source` passed while the Minkowski
            branch was stripped out, because the Physics branch still contains
            that exact line. The one algorithm where the field does nothing
            satisfied the check for the one where it must.
          - anchoring on the first `else:` in the file picked up an unrelated
            `else` in an earlier method.
          - anchoring on the first `else:` after the Physics `if` picked up the
            Physics branch's own `if random_checkbox ... else ...`, so the slice
            covered the wrong code.

        The dispatch is a real `if`, so the tree is the reliable way to name the
        branch. Anything that needs to distinguish two occurrences of the same
        construct has stopped being a text search.
        """
        tree = ast.parse(_CTRL_SRC)
        branch = None
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_src = ast.unparse(node.test)
            if test_src.replace(" ", "") == "algorithm=='Physics'":
                assert node.orelse, "the Physics branch has no else; the Minkowski "\
                                    "branch is gone and the dials cannot work"
                branch = ast.unparse(ast.Module(body=node.orelse, type_ignores=[]))
                break
        assert branch is not None, (
            "could not find the `if algorithm == 'Physics'` dispatch in the "
            "controller")
        flat = _flat(branch)

        for key, widget in (("step_size", "minkowski_step_size_input"),
                            ("rotation_workers",
                             "minkowski_rotation_workers_input")):
            assert f"algo_kwargs['{key}'] = self.ui.{widget}.value()" in flat, (
                f"the Minkowski branch does not pass {key} from {widget}, so "
                f"moving the field in the UI would do nothing")

        # The whole original defect in one assertion: the Minkowski step size
        # must not be sourced from a Physics widget anywhere in that branch.
        assert "physics_step_size_input" not in flat, (
            "the Minkowski branch reads the Physics step-size field, so a field "
            "that looks right is inert -- that is how the original gap survived")
    def test_both_survive_a_preferences_round_trip(self):
        for key, setter in (("MinkowskiStepSize", "SetFloat"),
                            ("MinkowskiRotationWorkers", "SetInt")):
            assert f'prefs.{setter}("{key}"' in _CTRL_SRC, (
                f"{key} is never written to preferences, so it resets every "
                f"session")
            assert f'prefs.GetFloat("{key}"' in _PANEL_SRC or \
                   f'prefs.GetInt("{key}"' in _PANEL_SRC, (
                f"{key} is never read back, so a saved value would not "
                f"reappear")

    def test_env_still_works_for_benchmarking(self):
        """The harness pins the pool width via the env; that must keep working.

        The GA coordinator copies `algo_kwargs` into the nest call, but the
        benchmark harness never builds one -- it calls `nest()` directly. So the
        env is the only channel a benchmark has, and taking it away to add a UI
        field would have broken every recorded baseline's reproducibility.
        """
        strategy = open(STRATEGY).read()
        for env in ("NESTING_ROTATION_WORKERS", "NESTING_STEP_SIZE"):
            assert env in strategy, f"{env} is no longer read"

    def test_resolver_precedence_is_explicit_and_distinguishes_auto(self):
        """kwargs beat the env; 0 means auto, and is not the same as absent.

        0 has to survive the trip as 0 rather than being dropped, because "the
        user chose Auto" and "nobody said anything" both land on the stdlib
        default but only one of them is a decision -- and a future default
        change must be able to tell them apart.
        """
        src = open(STRATEGY).read()
        step = src[src.index("def _step_size"):src.index("def _rotation_worker_limit")]
        assert "kwargs.get(\"step_size\")" in step
        assert "os.environ.get('NESTING_STEP_SIZE'" in step
        workers = src[src.index("def _rotation_worker_limit"):
                      src.index("class CandidateGeometryCache")]
        assert "kwargs.get('rotation_workers')" in workers
        assert "os.environ.get('NESTING_ROTATION_WORKERS'" in workers
        # an explicit non-positive value must fall through, not be honoured
        assert "int(explicit) > 0" in workers, (
            "a 0 from the UI must mean auto, not a zero-width pool")

    def test_auto_means_one_thread_per_core_not_core_count_plus_four(self):
        """Auto is `cpu_count()`. The stdlib default it replaces is not.

        `ThreadPoolExecutor()` with no argument sizes itself
        `min(32, cpu_count() + 4)` -- twice the core count on a small machine.
        Returning None to get that default is what this replaced, and it measured
        no faster than a single worker while a pool sized to the core count was
        meaningfully faster. So "auto" has to name a concrete width, and it has
        to be the core count.

        Executed rather than pattern-matched, because the four ways this can be
        wrong -- return None, return cpu_count()+4, return cpu_count() unguarded,
        or invert the precedence -- are all one line of source each, and a text
        check cannot tell them apart from the surrounding prose.
        """
        strategy = os.path.join(_REPO, "freecad", "nestingworkbench", "Tools",
                                "Nesting", "algorithms", "nesting_strategy.py")
        tree = ast.parse(open(strategy).read())
        fn = next(n for n in tree.body
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_rotation_worker_limit")
        os.environ.pop("NESTING_ROTATION_WORKERS", None)
        ns = {"os": os}
        exec(compile(ast.unparse(fn), "<fn>", "exec"), ns)
        resolve = ns["_rotation_worker_limit"]

        cores = os.cpu_count() or 1
        auto = resolve({})
        assert auto == cores, (
            f"auto resolved to {auto!r}, expected the core count {cores}. The "
            f"stdlib default it replaced, cpu_count() + 4, is "
            f"{min(32, cores + 4)} -- measured no faster than one worker")
        assert resolve({}) != min(32, cores + 4) or cores == min(32, cores + 4), (
            "auto must not be the stdlib default")

        for kwargs in ({}, {"rotation_workers": 0}, {"rotation_workers": -3},
                       {"rotation_workers": "nonsense"}, {"rotation_workers": None}):
            value = resolve(kwargs)
            assert isinstance(value, int) and value > 0, (
                f"_rotation_worker_limit({kwargs!r}) returned {value!r}. A "
                f"non-positive width silently disables parallelism, and "
                f"max_workers rejects it outright")
        assert resolve({"rotation_workers": 3}) == 3, (
            "an explicit width must still win over the core count")

        # The degenerate case has to be exercised, not left to whichever machine
        # the suite happens to run on. `os.cpu_count()` returns None on platforms
        # that cannot answer, and an unguarded `return os.cpu_count()` would
        # pass on any box with cores -- which is every box this is developed on,
        # and so never catches the bug it exists to catch.
        for bogus in (None, 0, -1):
            ns["os"] = type("_os", (), {"cpu_count": staticmethod(lambda b=bogus: b),
                                        "environ": {}})
            value = resolve({})
            assert isinstance(value, int) and value >= 1, (
                f"with os.cpu_count() returning {bogus!r} the resolver gave "
                f"{value!r}. A width below 1 disables parallelism silently and "
                f"max_workers raises on it")

    def test_the_reported_width_says_whether_it_was_chosen(self):
        """A report naming only the width cannot say chosen from default.

        Two runs with identical packing can differ in wall clock by more than a
        fifth on this alone, so a bare number in the output that might be a
        decision or might be a default is not enough to attribute a result.
        """
        strategy = open(STRATEGY).read()
        assert "'rotation_workers_auto': 0," in strategy, (
            "rotation_workers_auto is never initialised, so the += would raise "
            "KeyError inside the rotation future loop, where quiet=True swallows "
            "it and the run places nothing while reporting no error")
        assert "self._perf_stats['rotation_workers_auto'] = int(was_auto)" in strategy
        assert "def _rotation_workers_explicit(kwargs=None):" in strategy, (
            "once the width is resolved, a chosen 4 and an inferred 4 are the "
            "same int, so the distinction needs its own function")
        assert "self.rotation_workers_explicit = _rotation_workers_explicit(kwargs)" \
            in strategy, (
            "the chosen-or-inferred distinction has to survive the nester, or "
            "the flag is always zero by the time it is recorded")
        captured = strategy.index("was_auto = not self.rotation_workers_explicit")
        fallback = strategy.index("worker_limit = _rotation_worker_limit()")
        assert captured < fallback, (
            "the auto flag must be captured before the fallback, or it is always "
            "zero and the counter is decoration")
