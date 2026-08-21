"""Focused MC4-R2F1-C2 contracts for verified-nearest and UV-area masters.

The fast matcher is intentionally exercised through a pure contract adapter.
The production writer may place the helper in ``pro_verified_nearest`` or add
it to the pure topology module; either way, the assertions below require a
structured ``CorrespondenceResult`` and a complete bijection.  A missing
production entry point is reported as an explicit skipped API mismatch so the
test file can land before the implementation packet is integrated.

No Blender objects, fixtures, workers, or approximate writes are used here.
The existing pure ``CorrespondenceSearch``/``find_correspondence`` path is the
fallback oracle for accepted-result and UV-copy comparisons.
"""

from __future__ import annotations

import hashlib
import importlib
from itertools import combinations
import math
from types import SimpleNamespace
import unittest


TOPOLOGY = importlib.import_module("uv_gpt.topology_correspondence")
GROUP_FIRST = importlib.import_module("uv_gpt.pro_group_first")


_VERIFIED_MODULE_NAMES = (
    "uv_gpt.pro_verified_nearest",
    "uv_gpt.topology_correspondence",
)
_VERIFIED_ENTRY_NAMES = (
    "find_verified_nearest",
    "verified_nearest_correspondence",
    "try_verified_nearest",
)
_FALLBACK_ENTRY_NAMES = (
    "find_verified_nearest_or_fallback",
    "verified_nearest_or_fallback",
    "find_correspondence_with_verified_nearest",
)


def _load_verified_modules():
    modules = []
    errors = []
    for name in _VERIFIED_MODULE_NAMES:
        try:
            module = importlib.import_module(name)
        except (ImportError, ModuleNotFoundError) as exc:
            errors.append("%s: %s" % (name, exc))
            continue
        if module not in modules:
            modules.append(module)
    return tuple(modules), tuple(errors)


VERIFIED_MODULES, VERIFIED_IMPORT_ERRORS = _load_verified_modules()


def _resolve_verified_entry():
    for module in VERIFIED_MODULES:
        for name in _VERIFIED_ENTRY_NAMES:
            value = getattr(module, name, None)
            if callable(value):
                return module, name, value
    return None, None, None


def _resolve_fallback_entry():
    for module in VERIFIED_MODULES:
        for name in _FALLBACK_ENTRY_NAMES:
            value = getattr(module, name, None)
            if callable(value):
                return module, name, value
    return None, None, None


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


def _loop_mapping(result):
    mapping = _field(result, "loop_mapping", default=None)
    if mapping is None:
        raise AssertionError(
            "verified-nearest result API mismatch: expected loop_mapping"
        )
    return tuple(tuple(pair) for pair in mapping)


def _call_verified(testcase, master, candidate, transform, *, allow_flipping=False, tolerance=1.0e-6):
    module, name, function = _resolve_verified_entry()
    if function is None:
        imported = "; ".join(VERIFIED_IMPORT_ERRORS) or "modules imported but no entry point"
        testcase.skipTest(
            "verified-nearest API mismatch: expected one of %s in %s; %s"
            % (
                ", ".join(_VERIFIED_ENTRY_NAMES),
                ", ".join(_VERIFIED_MODULE_NAMES),
                imported,
            )
        )
    try:
        result = function(
            master,
            candidate,
            transform,
            allow_flipping=bool(allow_flipping),
            tolerance=float(tolerance),
        )
    except TypeError as exc:
        testcase.fail(
            "verified-nearest API mismatch in %s.%s: expected "
            "(master, candidate, transform, *, allow_flipping, tolerance); %s"
            % (module.__name__, name, exc)
        )
    return result


def _call_fallback_entry(
    testcase,
    master,
    candidate,
    transform,
    *,
    allow_flipping=False,
    tolerance=1.0e-6,
    max_search=17,
):
    module, name, function = _resolve_fallback_entry()
    if function is None:
        testcase.skipTest(
            "fallback-orchestration API mismatch: expected one of %s; "
            "no verified-nearest fallback entry point is present"
            % ", ".join(_FALLBACK_ENTRY_NAMES)
        )
    try:
        return function(
            master,
            candidate,
            transform,
            allow_flipping=bool(allow_flipping),
            tolerance=float(tolerance),
            max_search=int(max_search),
        )
    except TypeError as exc:
        testcase.fail(
            "fallback-orchestration API mismatch in %s.%s: expected "
            "(master, candidate, transform, *, allow_flipping, tolerance, "
            "max_search); %s"
            % (module.__name__, name, exc)
        )


def _assert_full_bijection(testcase, result, master, candidate):
    testcase.assertIsNotNone(result, "verified-nearest must return a result object")
    testcase.assertTrue(bool(_field(result, "accepted", default=False)), result)
    mapping = _loop_mapping(result)
    master_keys = {loop.key for loop in master.loops}
    candidate_keys = {loop.key for loop in candidate.loops}
    testcase.assertEqual({pair[0] for pair in mapping}, candidate_keys)
    testcase.assertEqual({pair[1] for pair in mapping}, master_keys)
    testcase.assertEqual(len(mapping), len(candidate_keys))
    testcase.assertEqual(len({pair[0] for pair in mapping}), len(mapping))
    testcase.assertEqual(len({pair[1] for pair in mapping}), len(mapping))
    testcase.assertIsNotNone(
        _field(result, "transform", default=None),
        "accepted verified-nearest result must retain the transform used for apply",
    )


def _assert_rejected(testcase, result, reason_token):
    testcase.assertIsNotNone(result, "fast miss must be a structured result")
    testcase.assertFalse(bool(_field(result, "accepted", default=False)), result)
    testcase.assertEqual(_loop_mapping(result), ())
    reason = str(
        _field(
            result,
            "fallback_reason",
            default=_field(result, "reason", default=""),
        )
    )
    testcase.assertIn(reason_token, reason)


def _result_digest(result):
    mapping = tuple(sorted(_loop_mapping(result), key=lambda pair: (pair[0], pair[1])))
    payload = (
        bool(_field(result, "accepted", default=False)),
        mapping,
        bool(_field(result, "reflected", default=False)),
        bool(_field(result, "reversed", default=False)),
        int(_field(result, "cyclic_shift", default=0) or 0),
        str(_field(result, "reason", default="")),
    )
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


def _copy_uvs(master, candidate, result):
    master_by_key = {loop.key: loop.uv for loop in master.loops}
    return tuple(
        (candidate_key, tuple(master_by_key[master_key]))
        for candidate_key, master_key in sorted(
            _loop_mapping(result), key=lambda pair: pair[0]
        )
    )


def _polygon_graph(
    points,
    *,
    order=None,
    face_key=0,
    seam_indices=(),
    vertex_signatures=None,
):
    """Build one immutable polygon graph with explicit loop incidence."""

    points = tuple(tuple(float(value) for value in point) for point in points)
    count = len(points)
    if order is None:
        order = tuple(range(count))
    order = tuple(order)
    seam_indices = set(seam_indices)
    loop_keys = tuple((face_key, index) for index in range(count))
    loops = tuple(
        TOPOLOGY.LoopRecord(
            key=loop_keys[index],
            face_key=face_key,
            edge_key=index,
            vertex_key=order[index],
            next_key=loop_keys[(index + 1) % count],
            prev_key=loop_keys[(index - 1) % count],
            uv=points[order[index]],
            boundary=True,
            seam=index in seam_indices,
            signature=()
            if vertex_signatures is None
            else tuple(vertex_signatures[order[index]]),
        )
        for index in range(count)
    )
    edges = tuple(
        TOPOLOGY.EdgeRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            face_keys=(face_key,),
            boundary=True,
        )
        for index in range(count)
    )
    vertices = tuple(
        TOPOLOGY.VertexRecord(
            key=vertex_key,
            loop_keys=(loop_keys[index],),
            boundary=True,
            signature=()
            if vertex_signatures is None
            else tuple(vertex_signatures[vertex_key]),
        )
        for index, vertex_key in enumerate(order)
    )
    return TOPOLOGY.make_graph(
        faces=(TOPOLOGY.FaceRecord(face_key, loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(
            TOPOLOGY.BoundaryComponentRecord(
                key=("outer", face_key),
                loop_keys=loop_keys,
                role="outer",
            ),
        ),
    )


def _annulus_graph(transform=None, seam_keys=()):
    """Build four quads around a hole, retaining two boundary components."""

    outer = ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0))
    inner = ((1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0))
    points = outer + inner
    if transform is not None:
        points = tuple(transform(point) for point in points)
    face_vertices = (
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    )
    seam_keys = set(seam_keys)
    loops = []
    faces = []
    edge_loops = {}
    vertex_loops = {index: [] for index in range(8)}
    edge_keys = {}
    for face_index, vertices_for_face in enumerate(face_vertices):
        cycle = tuple((face_index, local) for local in range(4))
        faces.append(TOPOLOGY.FaceRecord(face_index, cycle))
        for local, vertex_key in enumerate(vertices_for_face):
            edge_vertices = tuple(
                sorted((vertex_key, vertices_for_face[(local + 1) % 4]))
            )
            edge_key = edge_keys.setdefault(edge_vertices, len(edge_keys))
            loop_key = (face_index, local)
            boundary = edge_vertices in {
                (0, 1),
                (1, 2),
                (2, 3),
                (0, 3),
                (4, 5),
                (5, 6),
                (6, 7),
                (4, 7),
            }
            loops.append(
                TOPOLOGY.LoopRecord(
                    key=loop_key,
                    face_key=face_index,
                    edge_key=edge_key,
                    vertex_key=vertex_key,
                    next_key=(face_index, (local + 1) % 4),
                    prev_key=(face_index, (local - 1) % 4),
                    uv=points[vertex_key],
                    boundary=boundary,
                    seam=loop_key in seam_keys,
                )
            )
            edge_loops.setdefault(edge_key, []).append(loop_key)
            vertex_loops[vertex_key].append(loop_key)
    edges = []
    for edge_vertices, edge_key in sorted(edge_keys.items(), key=lambda item: item[1]):
        loops_for_edge = tuple(edge_loops[edge_key])
        faces_for_edge = tuple(sorted({key[0] for key in loops_for_edge}))
        edges.append(
            TOPOLOGY.EdgeRecord(
                edge_key,
                loops_for_edge,
                faces_for_edge,
                boundary=len(faces_for_edge) == 1,
            )
        )
    vertices = tuple(
        TOPOLOGY.VertexRecord(
            vertex_key,
            tuple(vertex_loops[vertex_key]),
            boundary=True,
        )
        for vertex_key in range(8)
    )
    outer_loops = tuple(
        key
        for edge_key in (
            edge_keys[(0, 1)],
            edge_keys[(1, 2)],
            edge_keys[(2, 3)],
            edge_keys[(0, 3)],
        )
        for key in edge_loops[edge_key]
    )
    inner_loops = tuple(
        key
        for edge_key in (
            edge_keys[(4, 5)],
            edge_keys[(5, 6)],
            edge_keys[(6, 7)],
            edge_keys[(4, 7)],
        )
        for key in edge_loops[edge_key]
    )
    return TOPOLOGY.make_graph(
        faces=faces,
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(
            TOPOLOGY.BoundaryComponentRecord("outer", outer_loops, "outer"),
            TOPOLOGY.BoundaryComponentRecord("hole", inner_loops, "hole", "outer"),
        ),
    )


def _grid_graph(seam_keys=(), transform=None):
    """Build a 2x2 patch with one interior vertex and seam-labelled loops."""

    points = {
        0: (0.0, 0.0),
        1: (2.0, 0.0),
        2: (4.0, 0.0),
        3: (0.0, 2.0),
        4: (2.0, 2.0),
        5: (4.0, 2.0),
        6: (0.0, 4.0),
        7: (2.0, 4.0),
        8: (4.0, 4.0),
    }
    if transform is not None:
        points = {key: transform(value) for key, value in points.items()}
    face_vertices = (
        (0, 1, 4, 3),
        (1, 2, 5, 4),
        (3, 4, 7, 6),
        (4, 5, 8, 7),
    )
    seam_keys = set(seam_keys)
    loops = []
    faces = []
    edge_map = {}
    edge_loops = {}
    vertex_loops = {key: [] for key in points}
    for face_index, vertices_for_face in enumerate(face_vertices):
        cycle = tuple((face_index, local) for local in range(4))
        faces.append(TOPOLOGY.FaceRecord(face_index, cycle))
        for local, vertex_key in enumerate(vertices_for_face):
            edge_vertices = tuple(
                sorted((vertex_key, vertices_for_face[(local + 1) % 4]))
            )
            edge_key = edge_map.setdefault(edge_vertices, len(edge_map))
            loop_key = (face_index, local)
            incident_face_count = sum(
                edge_vertices[0] in face and edge_vertices[1] in face
                for face in face_vertices
            )
            boundary = incident_face_count == 1
            loops.append(
                TOPOLOGY.LoopRecord(
                    key=loop_key,
                    face_key=face_index,
                    edge_key=edge_key,
                    vertex_key=vertex_key,
                    next_key=(face_index, (local + 1) % 4),
                    prev_key=(face_index, (local - 1) % 4),
                    uv=tuple(points[vertex_key]),
                    boundary=boundary,
                    seam=loop_key in seam_keys,
                )
            )
            edge_loops.setdefault(edge_key, []).append(loop_key)
            vertex_loops[vertex_key].append(loop_key)
    edges = tuple(
        TOPOLOGY.EdgeRecord(
            edge_key,
            tuple(edge_loops[edge_key]),
            tuple(sorted({key[0] for key in edge_loops[edge_key]})),
            boundary=len({key[0] for key in edge_loops[edge_key]}) == 1,
        )
        for _edge_vertices, edge_key in sorted(edge_map.items(), key=lambda item: item[1])
    )
    vertices = tuple(
        TOPOLOGY.VertexRecord(
            vertex_key,
            tuple(vertex_loops[vertex_key]),
            boundary=any(
                edge.boundary
                for edge in edges
                if any(loop.edge_key == edge.key for loop in loops if loop.key in vertex_loops[vertex_key])
            ),
        )
        for vertex_key in sorted(vertex_loops)
    )
    boundary_loops = tuple(loop.key for loop in loops if loop.boundary)
    return TOPOLOGY.make_graph(
        faces=faces,
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(
            TOPOLOGY.BoundaryComponentRecord("outer", boundary_loops, "outer"),
        ),
    )


def _identity_transform():
    return TOPOLOGY.SimilarityTransform2D(
        angle=0.0,
        scale=1.0,
        reflected=False,
        source_center=(0.0, 0.0),
        target_center=(0.0, 0.0),
    )


def _reflection_transform(center=(2.0, 2.0)):
    return TOPOLOGY.SimilarityTransform2D(
        angle=0.0,
        scale=1.0,
        reflected=True,
        source_center=tuple(center),
        target_center=tuple(center),
    )


def _reflect_x(points, center=(2.0, 2.0)):
    return tuple((2.0 * center[0] - x, y) for x, y in points)


def _area_record(key, uv_area, density):
    return SimpleNamespace(
        key=(key,),
        bucket_key=("same-shape",),
        uv_area=uv_area,
        density=density,
    )


def _shape_results_for(keys):
    keys = tuple(sorted(tuple(key) for key in keys))
    return tuple(
        SimpleNamespace(
            pair_ordinal=ordinal,
            representative_key=master,
            candidate_key=member,
            accepted=True,
            score=0.0,
            reason="accepted",
            transform=_identity_transform(),
        )
        for ordinal, (master, member) in enumerate(
            combinations(keys, 2)
        )
    )


def _area_master_key(plan):
    groups = _field(plan, "groups", "group_records", default=()) or ()
    if len(groups) != 1:
        raise AssertionError("area-master fixture must produce one group")
    return _field(
        groups[0],
        "uv_area_master_key",
        "area_master_key",
        "master_key",
        "density_master_key",
    )


class VerifiedNearestContractTests(unittest.TestCase):
    def test_high_symmetry_domain_fails_closed_at_fixed_work_ceiling(self):
        module = importlib.import_module("uv_gpt.pro_verified_nearest")
        runner = module._Runner(
            None,
            None,
            module.VerifiedNearestOptions(nearest_max_nodes=100000),
            _identity_transform(),
        )
        runner.domains = {
            (0, 0): tuple((1, index) for index in range(5000)),
            (0, 1): tuple((1, index) for index in range(5000)),
        }
        reason = runner._search()
        self.assertEqual(reason, "nearest_operation_cap")
        self.assertEqual(runner.assignment_nodes, 0)
        self.assertEqual(runner.assignment_cap, module.NEAREST_OPERATION_CAP)

    def test_simple_polygon_fast_acceptance_is_a_full_bijection(self):
        points = ((0.0, 0.0), (3.0, 0.0), (4.0, 1.0), (2.0, 3.0), (-1.0, 2.0))
        master = _polygon_graph(points)
        candidate = _polygon_graph(points)
        result = _call_verified(self, master, candidate, _identity_transform())
        repeat = _call_verified(self, master, candidate, _identity_transform())
        _assert_full_bijection(self, result, master, candidate)
        self.assertEqual(_result_digest(result), _result_digest(repeat))

    def test_seed_distance_diagnostics_are_euclidean_not_squared(self):
        points = ((0.0, 0.0), (3.0, 0.0), (4.0, 1.0), (2.0, 3.0), (-1.0, 2.0))
        signatures = {index: ("vertex", index) for index in range(len(points))}
        master = _polygon_graph(points, vertex_signatures=signatures)
        candidate = _polygon_graph(points, vertex_signatures=signatures)
        translated_seed = TOPOLOGY.SimilarityTransform2D(
            angle=0.0,
            scale=1.0,
            reflected=False,
            source_center=(0.0, 0.0),
            target_center=(2.0, 0.0),
        )
        result = _call_verified(self, master, candidate, translated_seed)
        _assert_full_bijection(self, result, master, candidate)
        diagnostics = _field(result, "nearest_diagnostics", default=None)
        self.assertIsNotNone(diagnostics)
        self.assertAlmostEqual(float(diagnostics.max_seed_distance), 2.0)
        self.assertAlmostEqual(float(diagnostics.mean_seed_distance), 2.0)
        self.assertEqual(
            int(diagnostics.distance_cache_misses),
            int(diagnostics.distance_evaluations),
        )
        self.assertEqual(
            int(diagnostics.distance_cache_hits)
            + int(diagnostics.distance_cache_misses),
            int(diagnostics.distance_lookups),
        )
        self.assertLessEqual(
            int(diagnostics.operations_used),
            int(diagnostics.assignment_cap),
        )

    def test_cyclic_shift_and_reversed_order_are_topology_restricted(self):
        points = ((0.0, 0.0), (4.0, 0.0), (5.0, 2.0), (2.0, 4.0), (-1.0, 2.0))
        signatures = {index: ("vertex", index) for index in range(len(points))}
        shifted = _polygon_graph(
            tuple(points[(index + 2) % len(points)] for index in range(len(points)))
        )
        shifted_result = _call_verified(self, _polygon_graph(points), shifted, _identity_transform())
        _assert_full_bijection(self, shifted_result, _polygon_graph(points), shifted)

        reversed_graph = _polygon_graph(
            points,
            order=tuple(reversed(range(len(points)))),
            vertex_signatures=signatures,
        )
        master = _polygon_graph(points, vertex_signatures=signatures)
        reversed_result = _call_verified(
            self,
            master,
            reversed_graph,
            _identity_transform(),
            allow_flipping=True,
        )
        _assert_full_bijection(self, reversed_result, master, reversed_graph)
        self.assertTrue(bool(_field(reversed_result, "reversed", default=False)))

    def test_reflection_is_allowed_or_rejected_only_by_the_explicit_gate(self):
        points = ((0.0, 0.0), (4.0, 0.0), (5.0, 2.0), (2.0, 4.0), (-1.0, 2.0))
        master = _polygon_graph(points)
        candidate = _polygon_graph(_reflect_x(points))
        transform = _reflection_transform()
        rejected = _call_verified(
            self,
            master,
            candidate,
            transform,
            allow_flipping=False,
        )
        _assert_rejected(self, rejected, "reflection")
        accepted = _call_verified(
            self,
            master,
            candidate,
            transform,
            allow_flipping=True,
        )
        _assert_full_bijection(self, accepted, master, candidate)
        self.assertTrue(bool(_field(accepted, "reflected", default=False)))

    def test_hole_roles_and_seam_labels_are_not_weakened(self):
        seam_keys = ((0, 0), (2, 1), (3, 2), (1, 3))
        master = _annulus_graph(seam_keys=seam_keys)
        candidate = _annulus_graph(seam_keys=seam_keys)
        accepted = _call_verified(self, master, candidate, _identity_transform())
        _assert_full_bijection(self, accepted, master, candidate)
        self.assertEqual(
            {component.role for component in master.boundaries},
            {"outer", "hole"},
        )

        weakened = _annulus_graph(seam_keys=tuple(seam_keys[:-1]))
        rejected = _call_verified(self, master, weakened, _identity_transform())
        _assert_rejected(self, rejected, "topology")

    def test_interior_vertex_loops_remain_distinct_and_seam_sensitive(self):
        seam_keys = ((0, 1), (1, 3), (2, 2), (3, 0))
        master = _grid_graph(seam_keys=seam_keys)
        candidate = _grid_graph(seam_keys=seam_keys)
        result = _call_verified(self, master, candidate, _identity_transform())
        _assert_full_bijection(self, result, master, candidate)
        center_loops = {loop.key for loop in master.loops if loop.vertex_key == 4}
        mapped_center = {
            master_key
            for candidate_key, master_key in _loop_mapping(result)
            if candidate_key in center_loops
        }
        self.assertEqual(len(center_loops), 4)
        self.assertEqual(len(mapped_center), 4)

        weakened = _grid_graph(seam_keys=tuple(seam_keys[:-1]))
        rejected = _call_verified(self, master, weakened, _identity_transform())
        _assert_rejected(self, rejected, "topology")

    def test_symmetric_ties_are_repeatable_and_duplicate_targets_are_rejected(self):
        square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        master = _polygon_graph(square)
        candidate = _polygon_graph(square)
        first = _call_verified(self, master, candidate, _identity_transform())
        second = _call_verified(self, master, candidate, _identity_transform())
        _assert_full_bijection(self, first, master, candidate)
        _assert_full_bijection(self, second, master, candidate)
        self.assertEqual(_result_digest(first), _result_digest(second))

        duplicate_candidate = _polygon_graph(
            ((0.0, 0.0), (0.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        )
        duplicate = _call_verified(
            self,
            master,
            duplicate_candidate,
            _identity_transform(),
        )
        _assert_rejected(self, duplicate, "duplicate")

    def test_missing_transform_is_a_non_applicable_fast_miss(self):
        graph = _polygon_graph(((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)))
        result = _call_verified(self, graph, graph, None)
        _assert_rejected(self, result, "missing_transform")
        fallback = TOPOLOGY.find_correspondence(
            graph,
            graph,
            tolerance=1.0e-6,
            max_search=100000,
        )
        self.assertTrue(
            fallback.accepted,
            "a fast-only miss must remain recoverable by the exact fallback",
        )

    def test_topology_mismatch_is_a_non_applicable_fast_miss(self):
        master = _polygon_graph(((0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0)))
        candidate = _polygon_graph(((0.0, 0.0), (2.0, 0.0), (1.0, 1.0)))
        result = _call_verified(self, master, candidate, _identity_transform())
        _assert_rejected(self, result, "topology")

    def test_numeric_tolerance_accepts_exact_boundary_equality(self):
        tolerance = 1.0e-6
        master_points = ((0.0, 0.0), (3.0, 0.0), (0.0, 2.0))
        candidate_points = ((tolerance, 0.0), (3.0, 0.0), (0.0, 2.0))
        master = _polygon_graph(master_points)
        candidate = _polygon_graph(candidate_points)
        equal = _call_verified(
            self,
            master,
            candidate,
            _identity_transform(),
            tolerance=tolerance,
        )
        _assert_full_bijection(self, equal, master, candidate)
        over_candidate = _polygon_graph(
            ((3.0 * tolerance, 0.0), (3.0, 0.0), (0.0, 2.0))
        )
        below = _call_verified(
            self,
            master,
            over_candidate,
            _identity_transform(),
            tolerance=math.nextafter(tolerance, 0.0),
        )
        _assert_rejected(self, below, "tolerance")

    def test_fast_acceptance_matches_exact_fallback_and_uv_copy(self):
        points = ((0.0, 0.0), (4.0, 0.0), (5.0, 2.0), (2.0, 4.0), (-1.0, 2.0))
        master = _polygon_graph(points)
        candidate = _polygon_graph(points)
        fast = _call_verified(self, master, candidate, _identity_transform())
        fallback = TOPOLOGY.find_correspondence(
            master,
            candidate,
            allow_flipping=False,
            tolerance=1.0e-6,
            max_search=100000,
        )
        _assert_full_bijection(self, fast, master, candidate)
        self.assertTrue(fallback.accepted, fallback)
        self.assertEqual(fallback.diagnostics.refinement_max_rounds, 8)
        self.assertEqual(_loop_mapping(fast), tuple(fallback.loop_mapping))
        self.assertEqual(_copy_uvs(master, candidate, fast), _copy_uvs(master, candidate, fallback))

    def test_fast_miss_invokes_unchanged_exact_fallback_once(self):
        graph = _polygon_graph(((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)))
        module, _name, _function = _resolve_fallback_entry()
        if module is None:
            self.skipTest(
                "fallback-orchestration API mismatch: no pure wrapper found; "
                "expected one exact CorrespondenceSearch call on a fast miss"
            )
        original = getattr(TOPOLOGY, "CorrespondenceSearch", None)
        if original is None:
            self.fail("existing CorrespondenceSearch API is missing")
        calls = []

        class CountingSearch(original):
            def __init__(self, *args, **kwargs):
                calls.append(kwargs.copy())
                super().__init__(*args, **kwargs)

        setattr(TOPOLOGY, "CorrespondenceSearch", CountingSearch)
        try:
            result = _call_fallback_entry(
                self,
                graph,
                graph,
                None,
                max_search=100000,
            )
        finally:
            setattr(TOPOLOGY, "CorrespondenceSearch", original)
        _assert_full_bijection(self, result, graph, graph)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].get("max_search"), 100000)


class UvAreaMasterContractTests(unittest.TestCase):
    def _build_plan(self, records):
        keys = [record.key for record in records]
        return GROUP_FIRST.build_group_first_plan(
            records,
            _shape_results_for(keys),
            similarity_tolerance=float("inf"),
        )

    def test_larger_uv_area_wins_even_when_density_is_lower(self):
        records = (
            _area_record(0, 1.0, 100.0),
            _area_record(1, 9.0, 1.0),
            _area_record(2, float("nan"), 1000.0),
            _area_record(3, -1.0, 999.0),
        )
        plan = self._build_plan(records)
        self.assertEqual(_area_master_key(plan), (1,))
        jobs = _field(plan, "exact_jobs", "direct_exact_jobs", default=())
        self.assertEqual({job.master_key for job in jobs}, {(1,)})
        self.assertEqual({job.member_key for job in jobs}, {(0,), (2,), (3,)})

    def test_uv_area_master_tie_and_digests_are_input_order_independent(self):
        records = (
            _area_record(9, 5.0, 100.0),
            _area_record(2, 5.0, 1.0),
            _area_record(4, 2.0, 50.0),
        )
        forward = self._build_plan(records)
        reverse = self._build_plan(tuple(reversed(records)))
        self.assertEqual(_area_master_key(forward), (2,))
        self.assertEqual(_area_master_key(reverse), (2,))
        self.assertEqual(forward.membership_digest, reverse.membership_digest)
        self.assertEqual(forward.exact_jobs_digest, reverse.exact_jobs_digest)

    def test_area_master_direct_jobs_are_one_to_one_and_cover_every_member(self):
        records = (
            _area_record(0, 2.0, 1.0),
            _area_record(1, 8.0, 2.0),
            _area_record(2, 7.0, 300.0),
            _area_record(3, 3.0, 400.0),
        )
        plan = self._build_plan(records)
        jobs = tuple(_field(plan, "exact_jobs", "direct_exact_jobs", default=()))
        self.assertEqual(len(jobs), 3)
        self.assertEqual({job.master_key for job in jobs}, {(1,)})
        members = tuple(job.member_key for job in jobs)
        self.assertEqual(len(members), len(set(members)))
        self.assertEqual(set(members), {(0,), (2,), (3,)})
        self.assertNotIn((1,), set(members))


if __name__ == "__main__":
    unittest.main()
