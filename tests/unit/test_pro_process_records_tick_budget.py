"""Focused pure tests for the cooperative Pro planner-record builder."""

from __future__ import annotations

import hashlib
import pickle
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_align_similar_selected import STACK_TOOLS


class _UV:
    def __init__(self, x, y):
        self.x = float(x)
        self.y = float(y)

    def __sub__(self, other):
        return _UV(self.x - other.x, self.y - other.y)

    @property
    def length_squared(self):
        return self.x * self.x + self.y * self.y

    def copy(self):
        return _UV(self.x, self.y)


class _Vec:
    def __init__(self, x, y, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def __sub__(self, other):
        return _Vec(self.x - other.x, self.y - other.y, self.z - other.z)

    def cross(self, other):
        return _Vec(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        )

    @property
    def length(self):
        return (self.x * self.x + self.y * self.y + self.z * self.z) ** 0.5


class _IdentityMatrix:
    def __matmul__(self, value):
        return value


class _UVData:
    def __init__(self, uv):
        self.uv = _UV(*uv)
        self.select = False
        self.select_edge = False


class _Vertex:
    def __init__(self, index, co):
        self.index = int(index)
        self.co = co
        self.select = False


class _Edge:
    def __init__(self, index):
        self.index = int(index)
        self.link_faces = []
        self.select = False


class _Loop:
    def __init__(self, vertex, edge, uv):
        self.vert = vertex
        self.edge = edge
        self.data = _UVData(uv)
        self.face = None
        self.link_loop_next = None
        self.select = False

    def __getitem__(self, _layer):
        return self.data


class _Face:
    def __init__(self, index, points, vertex_offset, edge_offset):
        self.index = int(index)
        self.hide = False
        self.select = False
        self.loops = []
        for local_index, point in enumerate(points):
            vertex = _Vertex(
                vertex_offset + local_index,
                _Vec(point[0], point[1], 0.0),
            )
            edge = _Edge(edge_offset + local_index)
            self.loops.append(_Loop(vertex, edge, point))
        for loop in self.loops:
            loop.face = self
            loop.edge.link_faces = [self]
        for local_index, loop in enumerate(self.loops):
            loop.link_loop_next = self.loops[(local_index + 1) % len(self.loops)]


def _faces_for_island(island):
    result = []
    seen = set()
    for loop in island:
        if loop.face not in seen:
            seen.add(loop.face)
            result.append(loop.face)
    return result


def _island_area(island, uv_layer):
    total = 0.0
    for face in _faces_for_island(island):
        points = [loop[uv_layer].uv for loop in face.loops]
        if len(points) < 3:
            continue
        face_total = 0.0
        for index, point in enumerate(points):
            other = points[(index + 1) % len(points)]
            face_total += point.x * other.y - other.x * point.y
        total += abs(face_total) * 0.5
    return total


def _make_island(face_count, *, start_index=0):
    faces = []
    for offset in range(face_count):
        index = start_index + offset
        if offset % 2:
            points = (
                (float(index * 2), 0.0),
                (float(index * 2 + 1), 0.0),
                (float(index * 2 + 1), 1.0),
                (float(index * 2), 1.0),
            )
        else:
            points = (
                (float(index * 2), 0.0),
                (float(index * 2 + 1), 0.0),
                (float(index * 2), 1.0),
            )
        faces.append(
            _Face(
                index,
                points,
                vertex_offset=index * 8,
                edge_offset=index * 8,
            )
        )
    island = tuple(loop for face in faces for loop in face.loops)
    return faces, island


class ProProcessRecordsTickBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._old_get_area = getattr(
            STACK_TOOLS.island_tools, "get_island_area", None
        )
        cls._old_island_faces = getattr(
            STACK_TOOLS.island_tools, "island_faces", None
        )
        STACK_TOOLS.island_tools.get_island_area = _island_area
        STACK_TOOLS.island_tools.island_faces = _faces_for_island

    @classmethod
    def tearDownClass(cls):
        if cls._old_get_area is None:
            delattr(STACK_TOOLS.island_tools, "get_island_area")
        else:
            STACK_TOOLS.island_tools.get_island_area = cls._old_get_area
        if cls._old_island_faces is None:
            delattr(STACK_TOOLS.island_tools, "island_faces")
        else:
            STACK_TOOLS.island_tools.island_faces = cls._old_island_faces

    def _build(self, island, *, identity=("records-test",)):
        obj = SimpleNamespace(matrix_world=_IdentityMatrix())
        layer = object()
        cache = STACK_TOOLS.similarity_matcher.DescriptorCache()
        state = STACK_TOOLS._ProPlannerRecordBuildState(
            obj,
            island,
            layer,
            cache,
            identity,
            {},
            refinement_metrics={},
        )
        return obj, layer, cache, state

    def _run(self, state, budget=8):
        slices = 0
        while not state.done:
            _result, _error, operations = state.advance(operation_budget=budget)
            self.assertLessEqual(operations, budget)
            slices += 1
            self.assertLess(slices, 500000)
        self.assertIsNone(state.error)
        return state.result

    def test_421_face_island_yields_and_matches_synchronous_oracle(self):
        _faces, island = _make_island(421)
        obj, layer, _cache, state = self._build(island)
        expected, expected_signature = STACK_TOOLS._pro_planner_record_for_island(
            obj,
            island,
            layer,
            STACK_TOOLS.similarity_matcher.DescriptorCache(),
            ("oracle",),
            {},
            refinement_metrics={},
        )
        actual, actual_signature, actual_uv_area = self._run(state, budget=8)
        self.assertEqual(actual, expected)
        self.assertEqual(actual_signature, expected_signature)
        self.assertEqual(actual_uv_area, STACK_TOOLS._pro_uv_area_for_island(island, layer))
        self.assertGreater(state.slices, 100)
        self.assertGreater(state.operations, 421)

    def test_record_output_is_not_visible_until_full_island_commit(self):
        _faces, island = _make_island(32)
        obj, layer, _cache, state = self._build(island)
        _result, _error, operations = state.advance(operation_budget=1)
        self.assertEqual(operations, 1)
        self.assertFalse(state.done)
        self.assertIsNone(state.result)

        session = STACK_TOOLS._ProAlignSession(
            None,
            obj,
            object(),
            layer,
            selected_islands=[island],
            all_islands=[island],
            evidence={},
        )
        session._descriptor_cache = STACK_TOOLS.similarity_matcher.DescriptorCache()
        session._snapshot_identity = ("session-records",)
        session._tick_deadline = None
        session._record_one()
        self.assertEqual(session._record_index, 0)
        self.assertEqual(session._planner_records, [])
        self.assertIsNotNone(session._record_builder)
        session.cancel("user_cancelled")
        self.assertEqual(session._planner_records, [])
        self.assertIsNone(session._record_builder)
        self.assertEqual(session.report.get("exact_loop_writes", 0), 0)

    def test_sliced_record_does_not_call_monolithic_record_oracle(self):
        _faces, island = _make_island(12)
        _obj, _layer, _cache, state = self._build(island)
        with patch.object(
            STACK_TOOLS,
            "_pro_planner_record_for_island",
            side_effect=AssertionError("monolithic oracle called by live state"),
        ):
            result = self._run(state, budget=4)
        self.assertIsNotNone(result[0])
        self.assertGreater(state.slices, 1)

    def test_varied_boundary_records_are_byte_and_digest_deterministic(self):
        _faces, island = _make_island(9)
        obj, layer, _cache, first = self._build(island, identity=("repeat",))
        _obj2, _layer2, _cache2, second = self._build(island, identity=("repeat",))
        first_result = self._run(first, budget=5)
        second_result = self._run(second, budget=5)
        self.assertEqual(first_result, second_result)
        first_digest = hashlib.sha256(pickle.dumps(first_result, protocol=4)).hexdigest()
        second_digest = hashlib.sha256(pickle.dumps(second_result, protocol=4)).hexdigest()
        self.assertEqual(first_digest, second_digest)
        self.assertEqual(first_result[0].face_key, tuple(range(9)))
        self.assertEqual(first_result[0].strict_topology_fingerprint[0], "pro-lite-topology-v4")

    def test_cancellation_discards_partial_record_state_without_writes(self):
        _faces, island = _make_island(64)
        obj, layer, _cache, state = self._build(island)
        state.advance(operation_budget=3)
        self.assertFalse(state.done)
        state.error = RuntimeError("cancelled")
        self.assertTrue(state.done)
        self.assertIsNone(state.result)
        self.assertEqual(getattr(obj, "write_count", 0), 0)
        self.assertEqual(getattr(layer, "write_count", 0), 0)


if __name__ == "__main__":
    unittest.main()
