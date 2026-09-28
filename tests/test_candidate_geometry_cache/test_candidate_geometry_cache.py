"""Characterisation tests for CandidateGeometryCache and its key algebra.

This cache is the one opt-in feature in the workbench with a measured win and
no test coverage at all. RESULTS-9.4.md measures it at 0.863x wall on the n70
corpus with a 92.2% hit rate, layout-identical across six runs, and +0 MiB peak
RSS.

Why the key algebra specifically
--------------------------------
The cache is a plain `dict` with no eviction and no size cap, so the only thing
standing between "a 13.7% win" and "silently wrong collision results" is the
key. A key collision -- two different candidate geometries resolving to the
same entry -- would hand back the wrong polygon, and collision results would
change with no error anywhere. The measured layout-neutrality is evidence against
that happening on the n70 corpus; it is not proof that it cannot.

So what is pinned here is the contract the cache actually has:

  * the key is (prefix, float(x), float(y)), and the prefix pins the part
    identity, the three discretisation parameters and the angle -- so two
    candidates at different grid positions can never share an entry, and two
    evaluations of the same part/angle/position always do;
  * `angle % 360.0` is deliberate, so 0 and 360 (and -90 and 270) are the same
    rotation and must share;
  * the discretisation parameters are in the key because a differently
    discretised part has genuinely different translated geometry;
  * source identity is `id()`, which is unique among *live* objects. That is
    what makes collisions impossible in a run -- the document holds references
    to every part for its lifetime. It also means the cache cannot outlive the
    run, which is consistent with it being constructed per run.

These run under plain CPython: `tests/conftest.py` installs inert FreeCAD
stubs, and `nesting_strategy` uses no FreeCAD API at import time.
"""
import pytest
from shapely.geometry import Polygon

from freecad.nestingworkbench.Tools.Nesting.algorithms import nesting_strategy as ns


class FakeDocument:
    def __init__(self, name):
        self.Name = name


class FakeSource:
    def __init__(self, name, document_name):
        self.Name = name
        self.Document = FakeDocument(document_name)


class FakePart:
    """The attributes `_candidate_geometry_key_prefix` reads, and nothing else.

    Deliberately a bare object rather than a Shape: the key function reaches for
    `source_freecad_object`, `spacing`, `deflection` and `simplification`, and a
    test that constructs a real Shape would be testing FreeCAD's attribute
    storage instead of the key logic.
    """

    def __init__(self, source=None, spacing=5.0, deflection=0.05,
                 simplification=0.1):
        self.source_freecad_object = source
        self.spacing = spacing
        self.deflection = deflection
        self.simplification = simplification


def square(size=10.0, x=0.0):
    return Polygon([(x, 0.0), (x + size, 0.0), (x + size, size), (x, size)])


class TestCacheSemantics:
    def test_absent_key_returns_none(self):
        cache = ns.CandidateGeometryCache()
        assert cache.get(("anything", 1.0, 2.0)) is None

    def test_put_then_get_returns_the_same_object(self):
        """Identity, not a copy: the caller mutates neither."""
        cache = ns.CandidateGeometryCache()
        poly = square()
        cache.put(("k", 0.0, 0.0), poly)
        assert cache.get(("k", 0.0, 0.0)) is poly

    def test_len_tracks_entries(self):
        cache = ns.CandidateGeometryCache()
        for i in range(5):
            cache.put(("k", float(i), 0.0), square())
        assert len(cache) == 5

    def test_clear_releases_everything(self):
        """run_nest relies on the cache being per-run; a stale one is a wrong
        layout, because the keys embed `id()` of a possibly-recycled address."""
        cache = ns.CandidateGeometryCache()
        cache.put(("k", 0.0, 0.0), square())
        cache.clear()
        assert len(cache) == 0
        assert cache.get(("k", 0.0, 0.0)) is None

    def test_same_key_twice_keeps_one_entry(self):
        cache = ns.CandidateGeometryCache()
        first, second = square(size=10.0), square(size=99.0)
        cache.put(("k", 0.0, 0.0), first)
        cache.put(("k", 0.0, 0.0), second)
        assert len(cache) == 1
        # Last write wins. The key is the contract, not the geometry -- which is
        # exactly why a key collision would be a silent wrong answer.
        assert cache.get(("k", 0.0, 0.0)) is second

    def test_distinct_positions_do_not_share(self):
        cache = ns.CandidateGeometryCache()
        polys = {}
        for i in range(4):
            key = ns._candidate_geometry_key(("p",), float(i * 10), 0.0)
            polys[key] = square(x=float(i * 10))
            cache.put(key, polys[key])
        assert len(cache) == 4
        for key, poly in polys.items():
            assert cache.get(key) is poly


class TestDefaultOn:
    """The cache is default-on, and five places have to agree about that.

    The constant lives in `freecad.nestingworkbench.constants` and is read by the
    UI checkbox, the preference read-back, the controller's two fallbacks, the
    coordinator's gate and the benchmark harness. Those are six consumers of one
    fact, and the failure mode is quiet: the workbench ships default-on while a
    committed baseline still describes the off path, and the gate stays green
    while measuring something nobody runs.
    """

    def test_constant_is_true(self):
        from freecad.nestingworkbench import constants

        assert constants.CANDIDATE_GEOMETRY_CACHE_DEFAULT is True

    def test_reachable_by_star_import(self):
        """ui_nesting and nesting_controller use `from ...constants import *`."""
        namespace = {}
        exec("from freecad.nestingworkbench.constants import *", namespace)
        assert namespace["CANDIDATE_GEOMETRY_CACHE_DEFAULT"] is True

    def test_ui_default_reads_the_constant(self):
        """The checkbox must not hardcode its own default."""
        from freecad.nestingworkbench import constants

        source = _read(
            "freecad/nestingworkbench/Tools/Nesting/ui_nesting.py")
        assert '"candidate_geometry_cache": CANDIDATE_GEOMETRY_CACHE_DEFAULT' in source
        assert '"candidate_geometry_cache": False' not in source
        # and the preference read-back, which is what a returning user actually
        # gets rather than the _DEFAULTS entry
        assert ('prefs.GetBool("CandidateGeometryCache",\n'
                '                               CANDIDATE_GEOMETRY_CACHE_DEFAULT)'
                in source)
        assert 'prefs.GetBool("CandidateGeometryCache", False)' not in source
        assert constants.CANDIDATE_GEOMETRY_CACHE_DEFAULT is True

    def test_no_stray_hardcoded_fallbacks(self):
        """Greps the three call sites for a literal False next to the flag.

        A source-level check rather than a behavioural one because the call
        sites are `dict.get(key, False)` fallbacks inside a large function --
        importing the module to observe them would need a full FreeCAD session
        and would still not prove the *other* five agree.
        """
        offenders = []
        for relative in (
            "freecad/nestingworkbench/Tools/Nesting/ui_nesting.py",
            "freecad/nestingworkbench/Tools/Nesting/nesting_controller.py",
            "freecad/nestingworkbench/Tools/Nesting/ga_coordinator.py",
        ):
            for number, line in enumerate(_read(relative).splitlines(), 1):
                stripped = line.strip()
                if "candidate_geometry_cache" not in stripped:
                    continue
                if "False" in stripped and "CANDIDATE_GEOMETRY_CACHE_DEFAULT" not in stripped:
                    offenders.append(f"{relative}:{number}: {stripped}")
        assert not offenders, "hardcoded cache default remains:\n" + "\n".join(offenders)

    def test_harness_follows_the_product_default(self):
        """Unset must mean "whatever the workbench does", not a harness default.

        The harness measures the product. Pinning its own default is how the two
        drift apart silently.
        """
        from freecad.nestingworkbench import constants

        assert _config({}).candidate_geometry_cache is \
            constants.CANDIDATE_GEOMETRY_CACHE_DEFAULT
        assert _config({"NEST_BENCH_CANDIDATE_GEOMETRY_CACHE": ""}).candidate_geometry_cache is \
            constants.CANDIDATE_GEOMETRY_CACHE_DEFAULT
        assert _config({"NEST_BENCH_CANDIDATE_GEOMETRY_CACHE": "default"}
                       ).candidate_geometry_cache is \
            constants.CANDIDATE_GEOMETRY_CACHE_DEFAULT

    @pytest.mark.parametrize("spec", ["0", "no", "false", "off"])
    def test_harness_control_arm_is_explicitly_off(self, spec):
        """The A/B driver needs a way to turn it off, which is how 9.4 was
        measured, so the override must not be lost when the default flips."""
        assert _config({"NEST_BENCH_CANDIDATE_GEOMETRY_CACHE": spec}
                       ).candidate_geometry_cache is False

    @pytest.mark.parametrize("spec", ["1", "yes", "true", "on"])
    def test_harness_explicit_on(self, spec):
        assert _config({"NEST_BENCH_CANDIDATE_GEOMETRY_CACHE": spec}
                       ).candidate_geometry_cache is True


def _read(relative):
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, os.path.pardir, relative)
    with open(os.path.normpath(path)) as handle:
        return handle.read()


def _config(environ):
    """The harness Config, loaded by path so its __name__ guard cannot fire."""
    import importlib.util
    import os

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.path.pardir, "freecad_harness", "nest_benchmark.py")
    spec = importlib.util.spec_from_file_location(
        "nb_for_cache_default", os.path.normpath(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Config(environ)


class TestKeyPrefix:
    def test_same_everything_gives_the_same_prefix(self):
        """The reuse the 92.2% hit rate depends on."""
        part = FakePart(source=FakeSource("Spacer", "doc"))
        assert (ns.PlacementOptimizer._candidate_geometry_key_prefix(part, 90.0)
                == ns.PlacementOptimizer._candidate_geometry_key_prefix(part, 90.0))

    def test_angle_is_taken_modulo_360(self):
        """0 and 360 are the same rotation; -90 and 270 are the same rotation."""
        part = FakePart(source=FakeSource("Spacer", "doc"))
        prefix = ns.PlacementOptimizer._candidate_geometry_key_prefix
        assert prefix(part, 0.0) == prefix(part, 360.0)
        assert prefix(part, 0.0) == prefix(part, 720.0)
        assert prefix(part, -90.0) == prefix(part, 270.0)

    def test_different_angles_differ(self):
        part = FakePart(source=FakeSource("Spacer", "doc"))
        prefix = ns.PlacementOptimizer._candidate_geometry_key_prefix
        assert prefix(part, 0.0) != prefix(part, 90.0)
        assert prefix(part, 90.0) != prefix(part, 180.0)

    def test_different_source_objects_differ(self):
        """Identity-keyed, so two live parts can never share an entry."""
        one = FakePart(source=FakeSource("Spacer", "doc"))
        two = FakePart(source=FakeSource("Spacer", "doc"))
        prefix = ns.PlacementOptimizer._candidate_geometry_key_prefix
        # Same document name and same source name -- only identity separates
        # them. This is the case a name-based key would get wrong.
        assert prefix(one, 0.0) != prefix(two, 0.0)

    def test_same_name_in_a_different_document_differ(self):
        one = FakePart(source=FakeSource("Spacer", "docA"))
        two = FakePart(source=FakeSource("Spacer", "docB"))
        prefix = ns.PlacementOptimizer._candidate_geometry_key_prefix
        assert prefix(one, 0.0) != prefix(two, 0.0)

    def test_source_less_parts_fall_back_to_part_identity(self):
        one = FakePart(source=None)
        two = FakePart(source=None)
        prefix = ns.PlacementOptimizer._candidate_geometry_key_prefix
        assert prefix(one, 0.0) != prefix(two, 0.0)

    @pytest.mark.parametrize("field,value", [
        ("spacing", 7.5),
        ("deflection", 0.2),
        ("simplification", 0.5),
    ])
    def test_discretisation_parameters_are_part_of_the_key(self, field, value):
        """A differently discretised part has different translated geometry,
        so reusing an entry across it would be wrong.

        The parameter is varied on ONE part object, mutating it in place. A
        first draft built a second FakePart with the same values, which passed
        even with the parameters deleted from the key entirely -- two distinct
        objects have distinct `id(source)`, so identity separated the prefixes
        and the test passed for the wrong reason. Holding identity constant is
        what makes this test mean anything.
        """
        part = FakePart(source=FakeSource("Spacer", "doc"))
        prefix = ns.PlacementOptimizer._candidate_geometry_key_prefix
        before = prefix(part, 0.0)
        setattr(part, field, value)
        assert prefix(part, 0.0) != before

    def test_default_parameters_do_not_collide(self):
        """Guards against a prefix that forgets a parameter entirely.

        As above: one object, mutated between readings, so only the parameter
        can be doing the separating.
        """
        part = FakePart(source=FakeSource("Spacer", "doc"))
        prefix = ns.PlacementOptimizer._candidate_geometry_key_prefix
        keys = [prefix(part, 0.0)]
        for field, value in (("spacing", 7.5), ("deflection", 0.2),
                             ("simplification", 0.5)):
            original = getattr(part, field)
            setattr(part, field, value)
            keys.append(prefix(part, 0.0))
            setattr(part, field, original)
        assert len(set(keys)) == 4


class TestFullKey:
    def test_position_completes_the_key(self):
        prefix = ("identity", 5.0, 0.05, 0.1, 90.0)
        a = ns._candidate_geometry_key(prefix, 10.0, 20.0)
        b = ns._candidate_geometry_key(prefix, 20.0, 10.0)
        assert a != b, "x and y must not be interchangeable"

    def test_positions_are_coerced_to_float(self):
        """Grid positions are floats, so an int coordinate must hit the same
        entry rather than silently missing and costing a geometry rebuild."""
        prefix = ("identity", 5.0, 0.05, 0.1, 90.0)
        assert (ns._candidate_geometry_key(prefix, 10, 20)
                == ns._candidate_geometry_key(prefix, 10.0, 20.0))

    def test_prefix_is_carried_whole(self):
        """Every prefix component has to reach the full key, or a reused entry
        crosses a part, an angle or a discretisation."""
        base = ("identity", 5.0, 0.05, 0.1, 90.0)
        keys = {
            ns._candidate_geometry_key(base, 0.0, 0.0),
            ns._candidate_geometry_key(("other", 5.0, 0.05, 0.1, 90.0), 0.0, 0.0),
            ns._candidate_geometry_key(("identity", 7.5, 0.05, 0.1, 90.0), 0.0, 0.0),
            ns._candidate_geometry_key(("identity", 5.0, 0.2, 0.1, 90.0), 0.0, 0.0),
            ns._candidate_geometry_key(("identity", 5.0, 0.05, 0.5, 90.0), 0.0, 0.0),
            ns._candidate_geometry_key(("identity", 5.0, 0.05, 0.1, 180.0), 0.0, 0.0),
            ns._candidate_geometry_key(base, 0.0, 5.0),
        }
        assert len(keys) == 7

    def test_repeated_evaluation_reuses_one_entry(self):
        """The end-to-end property the hit rate measures: same part, same angle,
        same position, evaluated twice, is one cache entry and one polygon."""
        part = FakePart(source=FakeSource("Spacer", "doc"))
        cache = ns.CandidateGeometryCache()
        prefix = ns.PlacementOptimizer._candidate_geometry_key_prefix(part, 45.0)
        built = []

        for _evaluation in range(3):
            for x, y in ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0)):
                key = ns._candidate_geometry_key(prefix, x, y)
                candidate = cache.get(key)
                if candidate is None:
                    candidate = square(x=x)
                    cache.put(key, candidate)
                    built.append(key)

        assert len(cache) == 3
        assert len(built) == 3, "the second and third evaluations built nothing"
