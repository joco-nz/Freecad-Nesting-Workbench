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

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STRATEGY = os.path.join(
    _REPO, "freecad", "nestingworkbench", "Tools", "Nesting", "algorithms",
    "nesting_strategy.py")

_TREE = ast.parse(open(STRATEGY).read())


def _is_stats_attr(node):
    return (isinstance(node, ast.Attribute)
            and node.attr == "_perf_stats"
            and isinstance(node.value, ast.Name)
            and node.value.id == "self")


def _schema_keys():
    """Keys present in the `self._perf_stats = {...}` initialiser."""
    keys = set()
    for node in ast.walk(_TREE):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            if any(_is_stats_attr(t) for t in node.targets):
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
