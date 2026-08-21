"""Pure MC4-R2F3 contracts for candidate-to-master seed algebra.

The seed implementation is deliberately exercised through finite 2-D tuples
only.  No Blender host, NumPy, fixture, worker, or external process is part
of this oracle.  The small adapter accepts the naming variants already used
by the Pro transform code while keeping the algebraic direction explicit:

* a seed maps ``source``/candidate coordinates to ``target``/master
  coordinates;
* composition arguments are ``source_to_mid`` followed by ``mid_to_target``;
* re-rooting receives candidate-to-representative and
  master-to-representative seeds and returns candidate-to-master.

The production module may be written in parallel.  Missing exports are
reported as focused skips (and the import test records a hard failure), so a
partial API cannot be mistaken for a complete contract.
"""

from __future__ import annotations

import importlib
import inspect
import math
from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


try:
    SEED = importlib.import_module("uv_gpt.pro_similarity_seed")
    SEED_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - exercised when the API is absent
    SEED = None
    SEED_IMPORT_ERROR = exc


try:
    GROUP_FIRST = importlib.import_module("uv_gpt.pro_group_first")
    GROUP_FIRST_IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - exercised only by partial installs
    GROUP_FIRST = None
    GROUP_FIRST_IMPORT_ERROR = exc


POINTS = (
    (-2.25, 0.75),
    (-0.4, 2.6),
    (1.35, -1.1),
    (3.2, 1.9),
)


def _resolve(module, *names):
    if module is None:
        return None
    for name in names:
        value = getattr(module, name, None)
        if value is not None:
            return value
    return None


def _field(value, *names, default=None):
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value[name]
        return default
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _call_supported(callable_, positional=(), **kwargs):
    """Call a contract entry point without passing unknown optional names."""

    try:
        signature = inspect.signature(callable_)
    except (TypeError, ValueError):
        return callable_(*positional, **kwargs)
    parameters = signature.parameters
    if any(parameter.kind == parameter.VAR_KEYWORD for parameter in parameters.values()):
        return callable_(*positional, **kwargs)
    accepted = {
        name: value
        for name, value in kwargs.items()
        if name in parameters
        and parameters[name].kind
        in (parameters[name].POSITIONAL_OR_KEYWORD, parameters[name].KEYWORD_ONLY)
    }
    return callable_(*positional, **accepted)


def _call_pair(callable_, first, second, first_names, second_names):
    """Call a binary contract using semantic keyword names when available."""

    try:
        signature = inspect.signature(callable_)
    except (TypeError, ValueError):
        return callable_(first, second)
    parameters = signature.parameters
    first_name = next((name for name in first_names if name in parameters), None)
    second_name = next((name for name in second_names if name in parameters), None)
    if first_name is not None and second_name is not None:
        return callable_(**{first_name: first, second_name: second})
    return callable_(first, second)


def _seed_factory():
    return _resolve(
        SEED,
        "make_seed_transform",
        "make_similarity_seed",
        "make_seed",
        "SeedTransform",
        "SimilaritySeed",
        "SimilarityTransformSeed",
        "SimilarityTransform2D",
        "SimilarityTransform",
    )


def _make_seed(
    *,
    angle=0.0,
    scale=1.0,
    reflected=False,
    source_center=(0.0, 0.0),
    target_center=(0.0, 0.0),
):
    factory = _seed_factory()
    if factory is None or not callable(factory):
        canonical = _resolve(SEED, "canonical_seed", "canonical_transform")
        if callable(canonical):
            return canonical(
                (angle, scale, reflected, source_center, target_center)
            )
        raise unittest.SkipTest(
            "pro_similarity_seed has no recognized seed factory/class export"
        )
    kwargs = {
        "angle": angle,
        "scale": scale,
        "reflected": reflected,
        "reflection": reflected,
        "source_center": source_center,
        "target_center": target_center,
        "candidate_center": source_center,
        "reference_center": target_center,
    }
    try:
        return _call_supported(factory, **kwargs)
    except TypeError as first_error:
        try:
            return factory(angle, scale, reflected, source_center, target_center)
        except TypeError:
            raise first_error


def _identity_seed():
    value = _resolve(
        SEED,
        "IDENTITY_SEED",
        "IDENTITY_TRANSFORM",
        "IDENTITY",
    )
    if value is not None and not callable(value):
        return value
    factory = _resolve(
        SEED,
        "identity_seed",
        "identity_transform",
        "make_identity_seed",
        "make_identity_transform",
    )
    if callable(factory):
        return _call_supported(factory)
    return _make_seed()


def _apply(seed, point):
    for name in ("apply", "map_point", "transform_point"):
        method = getattr(seed, name, None)
        if callable(method):
            result = method(point)
            return (float(result[0]), float(result[1]))
    function = _resolve(
        SEED,
        "apply_seed",
        "apply_transform",
        "apply_similarity",
    )
    if callable(function):
        result = _call_pair(
            function,
            seed,
            point,
            ("seed", "transform", "similarity"),
            ("point", "value", "xy"),
        )
        return (float(result[0]), float(result[1]))
    raise unittest.SkipTest("seed API has no apply/map_point operation")


def _inverse(seed):
    for name in ("inverse", "invert"):
        method = getattr(seed, name, None)
        if callable(method):
            return method()
    function = _resolve(
        SEED,
        "inverse_seed",
        "invert_seed",
        "inverse_transform",
        "invert_transform",
    )
    if callable(function):
        return _call_supported(function, positional=(seed,))
    raise unittest.SkipTest("seed API has no inverse/invert operation")


def _compose(source_to_mid, mid_to_target):
    """Compose source→mid, then mid→target."""

    method = getattr(source_to_mid, "then", None)
    if callable(method):
        return method(mid_to_target)
    method = getattr(mid_to_target, "compose", None)
    if callable(method):
        return method(source_to_mid)
    method = getattr(source_to_mid, "compose_after", None)
    if callable(method):
        return method(mid_to_target)
    method = getattr(source_to_mid, "compose", None)
    if callable(method):
        return method(mid_to_target)
    function = _resolve(
        SEED,
        "compose_seed",
        "compose_seeds",
        "compose_transforms",
        "compose_similarity",
        "compose",
    )
    if callable(function):
        if function is getattr(SEED, "compose_seeds", None):
            return _call_pair(
                function,
                mid_to_target,
                source_to_mid,
                ("outer", "after", "mid_to_target", "second"),
                ("inner", "before", "source_to_mid", "first"),
            )
        return _call_pair(
            function,
            source_to_mid,
            mid_to_target,
            ("source_to_mid", "first", "inner", "before", "left"),
            ("mid_to_target", "second", "outer", "after", "right"),
        )
    raise unittest.SkipTest("seed API has no composition operation")


def _reroot(candidate_to_representative, master_to_representative):
    """Return candidate→master from two transforms sharing a representative."""

    method = getattr(candidate_to_representative, "reroot", None)
    if callable(method):
        return method(master_to_representative)
    function = _resolve(
        SEED,
        "reroot_seed",
        "re_root_seed",
        "reroot_transform",
        "re_root_transform",
        "rebase_seed",
        "reroot_member_to_master",
        "derive_candidate_to_master",
        "candidate_to_master_via_representative",
    )
    if callable(function):
        if function is getattr(SEED, "reroot_member_to_master", None):
            return _call_pair(
                function,
                master_to_representative,
                candidate_to_representative,
                ("master_to_representative", "master_to_rep", "master_seed", "master", "first"),
                ("member_to_representative", "member_to_rep", "candidate_to_representative", "candidate_seed", "candidate", "second"),
            )
        return _call_pair(
            function,
            candidate_to_representative,
            master_to_representative,
            (
                "candidate_to_representative",
                "candidate_to_rep",
                "candidate_seed",
                "candidate",
                "first",
            ),
            (
                "master_to_representative",
                "master_to_rep",
                "master_seed",
                "master",
                "second",
            ),
        )
    raise unittest.SkipTest("seed API has no candidate/master re-root operation")


def _serialize(seed):
    for name in ("to_wire", "to_tuple", "serialize", "canonical"):
        method = getattr(seed, name, None)
        if callable(method):
            return method()
    function = _resolve(
        SEED,
        "seed_to_wire",
        "serialize_seed",
        "serialize_transform",
        "canonical_seed",
        "canonicalize_seed",
    )
    if callable(function):
        return _call_supported(function, positional=(seed,))
    raise unittest.SkipTest("seed API has no deterministic serialization operation")


def _canonical(seed):
    for name in ("canonical", "canonical_wire", "to_canonical"):
        method = getattr(seed, name, None)
        if callable(method):
            return method()
    function = _resolve(
        SEED,
        "canonical_seed",
        "canonicalize_seed",
        "canonical_transform",
        "canonical",
    )
    if callable(function):
        return _call_supported(function, positional=(seed,))
    return _serialize(seed)


def _from_wire(wire):
    for name in (
        "seed_from_wire",
        "deserialize_seed",
        "deserialize_transform",
        "from_wire",
    ):
        function = _resolve(SEED, name)
        if callable(function):
            return _call_supported(function, positional=(wire,))
    for name in ("SeedTransform", "SimilaritySeed", "SimilarityTransformSeed", "SimilarityTransform2D", "SimilarityTransform"):
        factory = _resolve(SEED, name)
        for method_name in ("from_wire", "from_tuple", "deserialize"):
            method = getattr(factory, method_name, None)
            if callable(method):
                return _call_supported(method, positional=(wire,))
    canonical = _resolve(SEED, "canonical_seed", "canonical_transform")
    if callable(canonical):
        return canonical(wire)
    raise unittest.SkipTest("seed API has no wire deserializer")


def _coerce_seed(value):
    if value is None:
        return None
    if callable(getattr(value, "apply", None)):
        return value
    for name in (
        "coerce_seed",
        "normalize_seed",
        "coerce_seed_transform",
        "normalize_seed_transform",
        "canonical_seed",
    ):
        function = _resolve(SEED, name)
        if callable(function):
            return _call_supported(function, positional=(value,))
    if isinstance(value, (tuple, list)) and len(value) == 5:
        try:
            return _from_wire(value)
        except unittest.SkipTest:
            return _make_seed(
                angle=value[0],
                scale=value[1],
                reflected=value[2],
                source_center=value[3],
                target_center=value[4],
            )
    angle = _field(value, "angle")
    scale = _field(value, "scale")
    reflected = _field(value, "reflected", "reflection")
    source_center = _field(value, "source_center", "candidate_center")
    target_center = _field(value, "target_center", "reference_center")
    if None not in (angle, scale, reflected, source_center, target_center):
        return _make_seed(
            angle=angle,
            scale=scale,
            reflected=reflected,
            source_center=source_center,
            target_center=target_center,
        )
    raise unittest.SkipTest("plan seed cannot be coerced to the pure seed API")


def _assert_point(testcase, actual, expected, *, places=10):
    testcase.assertEqual(len(actual), 2)
    testcase.assertAlmostEqual(float(actual[0]), float(expected[0]), places=places)
    testcase.assertAlmostEqual(float(actual[1]), float(expected[1]), places=places)


def _group_first_factory(*names):
    return _resolve(GROUP_FIRST, *names)


def _plan_island(key, *, uv_area):
    factory = _group_first_factory("IslandRecord", "GroupIsland", "IslandInput")
    if factory is None or not callable(factory):
        raise unittest.SkipTest("group-first plan has no primitive island record")
    payload = {
        "key": tuple(key),
        "island_key": tuple(key),
        "bucket_key": ("seed-contract",),
        "strict_bucket_key": ("seed-contract",),
        "density": 1.0,
        "uv_area": uv_area,
        "uv_size": uv_area,
        "ordinal": int(key[0]),
    }
    try:
        return _call_supported(factory, **payload)
    except TypeError as first_error:
        try:
            return factory(tuple(key), 1.0, ("seed-contract",), int(key[0]), "", (), uv_area, uv_area)
        except TypeError:
            raise first_error


def _plan_pair_result(request, transform):
    factory = _group_first_factory("GroupPairResult", "ShapePairResult", "GroupMatchResult")
    if factory is None or not callable(factory):
        raise unittest.SkipTest("group-first plan has no primitive pair-result record")
    return _call_supported(
        factory,
        pair_ordinal=request.pair_ordinal,
        ordinal=request.pair_ordinal,
        representative_key=request.representative_key,
        master_key=request.representative_key,
        candidate_key=request.candidate_key,
        member_key=request.candidate_key,
        accepted=True,
        score=0.0,
        reason="accepted",
        transform=transform,
    )


def _plan_with_shape_seeds(testcase, *, master_is_representative):
    if GROUP_FIRST is None:
        raise unittest.SkipTest(
            "uv_gpt.pro_group_first is not available: %s" % GROUP_FIRST_IMPORT_ERROR
        )
    frontier_type = _group_first_factory("GroupFirstFrontier", "GroupFirstPlanner", "GroupFirstState")
    if frontier_type is None or not callable(frontier_type):
        raise unittest.SkipTest("group-first plan has no recognized frontier")

    representative_key = (0,)
    master_key = representative_key if master_is_representative else (1,)
    member_key = (2,)
    records = (
        _plan_island(representative_key, uv_area=9.0 if master_is_representative else 1.0),
        _plan_island((1,), uv_area=1.0 if master_is_representative else 9.0),
        _plan_island(member_key, uv_area=3.0),
    )
    first_member_to_representative = _make_seed(
        angle=-0.21,
        scale=1.35,
        reflected=True,
        source_center=(2.0, -1.0),
        target_center=(-3.0, 4.0),
    )
    member_to_representative = _make_seed(
        angle=0.43,
        scale=0.72,
        reflected=False,
        source_center=(-1.0, 3.5),
        target_center=(-3.0, 4.0),
    )
    master_to_representative = (
        _make_seed(source_center=(-3.0, 4.0), target_center=(-3.0, 4.0))
        if master_is_representative
        else first_member_to_representative
    )
    frontier = _call_supported(
        frontier_type,
        positional=(records,),
        similarity_tolerance=float("inf"),
    )
    transforms = {
        (1,): first_member_to_representative,
        member_key: member_to_representative,
    }
    pending = tuple(frontier.pending_requests())
    while pending:
        for request in pending:
            transform = transforms.get(tuple(request.candidate_key))
            if transform is None:
                testcase.fail("unexpected plan request without a synthetic shape seed")
            frontier.consume(_plan_pair_result(request, transform))
        pending = tuple(frontier.pending_requests())
    finalize = getattr(frontier, "finalize", None)
    if not callable(finalize):
        finalize = getattr(frontier, "finish", None)
    if not callable(finalize):
        raise unittest.SkipTest("group-first plan has no finalize method")
    plan = finalize()
    jobs = tuple(getattr(plan, "direct_exact_jobs", getattr(plan, "exact_jobs", ())))
    return (
        plan,
        jobs,
        representative_key,
        master_key,
        member_key,
        master_to_representative,
        first_member_to_representative,
        member_to_representative,
    )


class SeedAlgebraContractTests(unittest.TestCase):
    def test_module_imports_as_a_pure_python_module(self):
        if SEED_IMPORT_ERROR is not None:
            self.fail("pro_similarity_seed import/API mismatch: %r" % (SEED_IMPORT_ERROR,))
        self.assertIsNotNone(SEED)

    def test_identity_is_pointwise_identity(self):
        identity = _identity_seed()
        for point in POINTS:
            with self.subTest(point=point):
                _assert_point(self, _apply(identity, point), point)

    def test_inverse_roundtrip_is_pointwise_bijective(self):
        seed = _make_seed(
            angle=0.61,
            scale=1.7,
            reflected=True,
            source_center=(1.25, -2.5),
            target_center=(-3.0, 4.75),
        )
        inverse = _inverse(seed)
        for point in POINTS:
            with self.subTest(point=point):
                _assert_point(self, _apply(inverse, _apply(seed, point)), point)
                _assert_point(self, _apply(seed, _apply(inverse, point)), point)

    def test_translation_preserves_point_delta(self):
        seed = _make_seed(
            angle=0.0,
            scale=1.0,
            reflected=False,
            source_center=(0.0, 0.0),
            target_center=(3.5, -4.25),
        )
        for point in POINTS:
            with self.subTest(point=point):
                _assert_point(
                    self,
                    _apply(seed, point),
                    (point[0] + 3.5, point[1] - 4.25),
                )

    def test_scale_and_rotation_use_source_and_target_centres(self):
        seed = _make_seed(
            angle=math.pi / 2.0,
            scale=2.0,
            reflected=False,
            source_center=(1.0, 2.0),
            target_center=(-2.0, 3.0),
        )
        _assert_point(self, _apply(seed, (2.0, 2.0)), (-2.0, 5.0))

    def test_composition_covers_all_four_reflection_combinations(self):
        for first_reflected in (False, True):
            for second_reflected in (False, True):
                with self.subTest(
                    first_reflected=first_reflected,
                    second_reflected=second_reflected,
                ):
                    source_to_mid = _make_seed(
                        angle=0.37,
                        scale=1.4,
                        reflected=first_reflected,
                        source_center=(-1.0, 2.0),
                        target_center=(3.0, -4.0),
                    )
                    mid_to_target = _make_seed(
                        angle=-0.52,
                        scale=0.65,
                        reflected=second_reflected,
                        source_center=(3.0, -4.0),
                        target_center=(5.5, 1.25),
                    )
                    composed = _compose(source_to_mid, mid_to_target)
                    for point in POINTS:
                        _assert_point(
                            self,
                            _apply(composed, point),
                            _apply(mid_to_target, _apply(source_to_mid, point)),
                        )
                    reflected = _field(composed, "reflected", "reflection")
                    if reflected is not None:
                        self.assertEqual(
                            bool(reflected),
                            first_reflected ^ second_reflected,
                        )

    def test_candidate_and_master_seeds_re_root_to_same_representative_points(self):
        candidate_to_representative = _make_seed(
            angle=0.31,
            scale=1.2,
            reflected=True,
            source_center=(-1.5, 0.75),
            target_center=(4.0, -2.0),
        )
        master_to_representative = _make_seed(
            angle=-0.44,
            scale=0.8,
            reflected=False,
            source_center=(2.25, 3.0),
            target_center=(4.0, -2.0),
        )
        candidate_to_master = _reroot(
            candidate_to_representative,
            master_to_representative,
        )
        representative_to_master = _inverse(master_to_representative)
        for point in POINTS:
            with self.subTest(point=point):
                _assert_point(
                    self,
                    _apply(candidate_to_master, point),
                    _apply(
                        representative_to_master,
                        _apply(candidate_to_representative, point),
                    ),
                )

    def test_representative_as_member_uses_identity_candidate_seed(self):
        representative_to_representative = _make_seed(
            source_center=(4.0, -2.0),
            target_center=(4.0, -2.0),
        )
        master_to_representative = _make_seed(
            angle=0.22,
            scale=1.15,
            reflected=True,
            source_center=(-2.0, 1.0),
            target_center=(4.0, -2.0),
        )
        representative_to_master = _reroot(
            representative_to_representative,
            master_to_representative,
        )
        expected = _inverse(master_to_representative)
        for point in POINTS:
            with self.subTest(point=point):
                _assert_point(
                    self,
                    _apply(representative_to_master, point),
                    _apply(expected, point),
                )

    def test_master_as_representative_uses_identity_master_seed(self):
        candidate_to_representative = _make_seed(
            angle=-0.19,
            scale=0.9,
            reflected=True,
            source_center=(1.0, -3.0),
            target_center=(4.0, -2.0),
        )
        master_to_representative = _make_seed(
            source_center=(4.0, -2.0),
            target_center=(4.0, -2.0),
        )
        candidate_to_master = _reroot(
            candidate_to_representative,
            master_to_representative,
        )
        for point in POINTS:
            with self.subTest(point=point):
                _assert_point(
                    self,
                    _apply(candidate_to_master, point),
                    _apply(candidate_to_representative, point),
                )

    def test_invalid_zero_and_nonfinite_inputs_are_rejected(self):
        invalid_specs = (
            {"scale": 0.0},
            {"scale": -1.0},
            {"scale": float("nan")},
            {"scale": float("inf")},
            {"angle": float("nan")},
            {"angle": float("inf")},
            {"source_center": (float("nan"), 0.0)},
            {"target_center": (0.0, float("inf"))},
            {"reflected": 1},
        )
        for spec in invalid_specs:
            with self.subTest(spec=spec):
                try:
                    result = _make_seed(**spec)
                except (TypeError, ValueError):
                    # Raising a validation error is an accepted API spelling;
                    # the shipped pure module uses ``None`` instead.
                    continue
                self.assertIsNone(result)

    def test_serialization_and_canonical_output_are_deterministic(self):
        seed = _make_seed(
            angle=0.375,
            scale=1.25,
            reflected=True,
            source_center=(1.5, -2.0),
            target_center=(-3.5, 4.25),
        )
        wire = _serialize(seed)
        self.assertEqual(wire, _serialize(seed))
        self.assertEqual(_canonical(seed), _canonical(seed))
        self.assertEqual(
            wire,
            (0.375, 1.25, True, (1.5, -2.0), (-3.5, 4.25)),
        )

    def test_wire_roundtrip_preserves_algebraic_points(self):
        seed = _make_seed(
            angle=-0.28,
            scale=0.875,
            reflected=False,
            source_center=(-2.0, 1.25),
            target_center=(3.5, -4.0),
        )
        restored = _from_wire(_serialize(seed))
        self.assertEqual(_serialize(restored), _serialize(seed))
        for point in POINTS:
            with self.subTest(point=point):
                _assert_point(self, _apply(restored, point), _apply(seed, point))

    def test_plan_re_root_populates_direct_jobs_for_nonrepresentative_master(self):
        (
            _plan,
            jobs,
            representative_key,
            master_key,
            member_key,
            master_to_representative,
            _first_member_to_representative,
            member_to_representative,
        ) = _plan_with_shape_seeds(self, master_is_representative=False)
        by_member = {tuple(job.member_key): job for job in jobs}
        self.assertIn(representative_key, by_member)
        self.assertIn(member_key, by_member)
        self.assertNotIn(master_key, by_member)

        representative_to_master = _coerce_seed(by_member[representative_key].seed_transform)
        member_to_master = _coerce_seed(by_member[member_key].seed_transform)
        self.assertIsNotNone(
            representative_to_master,
            "representative-as-member must receive inverse master→representative seed",
        )
        self.assertIsNotNone(
            member_to_master,
            "non-representative member must receive re-rooted candidate→master seed",
        )
        for point in POINTS:
            with self.subTest(point=point, member="representative"):
                _assert_point(
                    self,
                    _apply(
                        master_to_representative,
                        _apply(representative_to_master, point),
                    ),
                    point,
                )
            with self.subTest(point=point, member="candidate"):
                _assert_point(
                    self,
                    _apply(
                        master_to_representative,
                        _apply(member_to_master, point),
                    ),
                    _apply(member_to_representative, point),
                )

    def test_plan_re_root_keeps_direct_seed_when_master_is_representative(self):
        (
            _plan,
            jobs,
            representative_key,
            master_key,
            member_key,
            master_to_representative,
            first_member_to_representative,
            member_to_representative,
        ) = _plan_with_shape_seeds(self, master_is_representative=True)
        self.assertEqual(master_key, representative_key)
        by_member = {tuple(job.member_key): job for job in jobs}
        self.assertNotIn(master_key, by_member)
        for key, expected in (
            ((1,), first_member_to_representative),
            (member_key, member_to_representative),
        ):
            seed = _coerce_seed(by_member[key].seed_transform)
            self.assertIsNotNone(seed)
            for point in POINTS:
                with self.subTest(member=key, point=point):
                    _assert_point(self, _apply(seed, point), _apply(expected, point))
        for point in POINTS:
            with self.subTest(point=point):
                _assert_point(self, _apply(master_to_representative, point), point)


if __name__ == "__main__":
    unittest.main()
