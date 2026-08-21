"""MC4-C2 incremental snapshot and immutable graph frontier tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PAYLOAD = _load("pro_process_payload", ROOT / "uv_gpt" / "pro_process_payload.py")
ADAPTER = _load("pro_process_adapter", ROOT / "uv_gpt" / "pro_process_adapter.py")


class _UV:
    def __init__(self, point):
        self.uv = types.SimpleNamespace(x=float(point[0]), y=float(point[1]))
        self.select = True
        self.select_edge = False


class _Vertex:
    def __init__(self, index):
        self.index = int(index)
        self.select = False
        self.link_edges = []


class _Edge:
    def __init__(self, index):
        self.index = int(index)
        self.select = False
        self.seam = False
        self.link_faces = []


class _StableIntEdge(_Edge):
    """Blender 5.0-shaped edge accessor: ``index`` is a plain int."""

    @property
    def index(self):
        return int(self._index)

    @index.setter
    def index(self, value):
        self._index = int(value)


class _Loop:
    def __init__(self, face, index, vertex, edge, point):
        self.face = face
        self.index = int(index)
        self.vert = vertex
        self.edge = edge
        self._uv = _UV(point)

    def __getitem__(self, _layer):
        return self._uv

    @property
    def link_loop_next(self):
        return self.next

    @property
    def link_loop_prev(self):
        return self.prev


class _OneReadEdge(_StableIntEdge):
    """Ensure vertex records use captured primitive keys, not live accessors."""

    def __init__(self, index):
        super().__init__(index)
        self.index_reads = 0

    @property
    def index(self):
        self.index_reads += 1
        if self.index_reads > 1:
            raise TypeError("live edge index is no longer sequence-shaped")
        return int(self._index)

    @index.setter
    def index(self, value):
        self._index = int(value)


class _Face:
    def __init__(self, index, offset=0.0):
        self.index = int(index)
        self.select = True
        self.loops = []
        points = ((offset, 0.0), (offset + 1.0, 0.0), (offset, 1.0))
        vertices = [_Vertex(index * 3 + item) for item in range(3)]
        edges = [_Edge(index * 3 + item) for item in range(3)]
        for item, point in enumerate(points):
            loop = _Loop(self, item, vertices[item], edges[item], point)
            self.loops.append(loop)
            vertices[item].link_edges.append(edges[item])
            edges[item].link_faces.append(self)


class _Faces(list):
    active = None


def _fixture(face_count=2):
    faces = _Faces(_Face(index, float(index) * 2.0) for index in range(face_count))
    faces.active = faces[0] if faces else None
    bm = types.SimpleNamespace(
        faces=faces,
        edges=[loop.edge for face in faces for loop in face.loops],
        verts=[loop.vert for face in faces for loop in face.loops],
    )
    obj = types.SimpleNamespace(
        name="SnapshotObject",
        mode="EDIT",
        data=types.SimpleNamespace(
            name="SnapshotMesh",
            uv_layers=types.SimpleNamespace(
                active=types.SimpleNamespace(name="UVMap.001")
            ),
        ),
    )
    context = types.SimpleNamespace(selected_objects=(obj,))
    layer = object()
    islands = [tuple(face.loops) for face in faces]
    return context, obj, bm, layer, islands


def _accessor_shape_fixture():
    face = _Face(0)
    edge = _OneReadEdge(0)
    vertex = _Vertex(0)
    loop = _Loop(face, 0, vertex, edge, (0.0, 0.0))
    face.loops = [loop]
    edge.link_faces = [face]
    vertex.link_edges = [edge]
    faces = _Faces([face])
    faces.active = face
    bm = types.SimpleNamespace(faces=faces, edges=[edge], verts=[vertex])
    obj = types.SimpleNamespace(
        name="SnapshotObject",
        mode="EDIT",
        data=types.SimpleNamespace(
            name="SnapshotMesh",
            uv_layers=types.SimpleNamespace(
                active=types.SimpleNamespace(name="UVMap.001")
            ),
        ),
    )
    context = types.SimpleNamespace(selected_objects=(obj,))
    return context, obj, bm, object(), [(loop,)]


def _options():
    return ADAPTER.make_exact_options(
        allow_flipping=False,
        match_scale=True,
        tolerance=1.0e-6,
        max_search=1024,
    )


def _shared_two_face_fixture():
    """Two UV islands sharing mesh edges/vertices but not UV coordinates."""

    vertices = [_Vertex(index) for index in range(4)]
    edges = [_Edge(index) for index in range(5)]
    faces = []
    specs = (
        (0, (0, 1, 2), (0, 1, 2), ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))),
        (1, (1, 3, 2), (3, 4, 1), ((10.0, 0.0), (11.0, 0.0), (10.0, 1.0))),
    )
    for face_index, vertex_ids, edge_ids, points in specs:
        face = types.SimpleNamespace(index=face_index, select=True, loops=[])
        for local_index, (vertex_id, edge_id, point) in enumerate(
            zip(vertex_ids, edge_ids, points)
        ):
            loop = _Loop(face, local_index, vertices[vertex_id], edges[edge_id], point)
            face.loops.append(loop)
            vertices[vertex_id].link_edges.append(edges[edge_id])
            if face not in edges[edge_id].link_faces:
                edges[edge_id].link_faces.append(face)
        for local_index, loop in enumerate(face.loops):
            loop.next = face.loops[(local_index + 1) % len(face.loops)]
            loop.prev = face.loops[(local_index - 1) % len(face.loops)]
        faces.append(face)
    face_list = _Faces(faces)
    face_list.active = faces[0]
    bm = types.SimpleNamespace(faces=face_list, edges=edges, verts=vertices)
    obj = types.SimpleNamespace(
        name="SharedObject",
        mode="EDIT",
        data=types.SimpleNamespace(
            name="SharedMesh",
            uv_layers=types.SimpleNamespace(
                active=types.SimpleNamespace(name="UVMap.001")
            ),
        ),
    )
    context = types.SimpleNamespace(selected_objects=(obj,))
    layer = object()
    islands = [tuple(face.loops) for face in faces]
    return context, obj, bm, layer, islands


def _build(context, obj, bm, layer, islands, budget=3):
    builder = ADAPTER.IncrementalSnapshotBuilder(
        context,
        obj,
        bm,
        layer,
        islands,
        session_nonce="snapshot-test",
        generation=4,
        options=_options(),
    )
    while not builder.done:
        builder.advance(operation_budget=budget)
    return builder


class SnapshotGuardTests(unittest.TestCase):
    def test_blender50_scalar_edge_index_is_normalized_once(self):
        context, obj, bm, layer, islands = _accessor_shape_fixture()
        builder = _build(context, obj, bm, layer, islands, budget=2)
        self.assertTrue(builder.done)
        self.assertEqual(builder.result.material.vertex_payload[0][0], 0)
        self.assertEqual(builder.result.material.vertex_payload[0][2], True)
        self.assertEqual(bm.edges[0].index_reads, 1)

    def test_initial_builder_is_incremental_and_does_not_call_legacy_capture(self):
        context, obj, bm, layer, islands = _fixture(2)
        with mock.patch.object(
            ADAPTER,
            "capture_snapshot",
            side_effect=AssertionError("legacy full capture must not run"),
        ):
            builder = ADAPTER.IncrementalSnapshotBuilder(
                context,
                obj,
                bm,
                layer,
                islands,
                session_nonce="snapshot-test",
                generation=4,
                options=_options(),
            )
            self.assertIsNone(builder.advance(operation_budget=1))
            self.assertFalse(builder.done)
            while not builder.done:
                builder.advance(operation_budget=3)
        self.assertGreater(builder.slices, 1)
        self.assertEqual(len(builder.live_loop_map), 6)
        self.assertEqual(len(builder.prewrite_snapshot), 6)
        self.assertLess(builder.max_slice_ms, 250.0)

    def test_forced_validation_rebuilds_incrementally_and_detects_uv_digest_change(self):
        context, obj, bm, layer, islands = _fixture(1)
        builder = _build(context, obj, bm, layer, islands)
        guard = ADAPTER.SnapshotGuard(
            builder.result,
            context,
            obj,
            bm,
            layer,
            islands,
            session_nonce="snapshot-test",
            generation=4,
            options=_options(),
        )
        guard.request_validation()
        status = "pending"
        while status == "pending":
            status = guard.advance_validation(operation_budget=2)
        self.assertEqual(status, "valid")
        self.assertGreater(guard.validation_slices, 1)

        bm.faces[0].loops[0]._uv.uv.x += 0.25
        guard.request_validation()
        status = "pending"
        while status == "pending":
            status = guard.advance_validation(operation_budget=2)
        self.assertEqual(status, "invalid")
        self.assertIn("digest", guard.invalid_reason)

    def test_sentinel_invalidation_is_immediate_without_full_scan(self):
        context, obj, bm, layer, islands = _fixture(1)
        builder = _build(context, obj, bm, layer, islands)
        guard = ADAPTER.SnapshotGuard(
            builder.result,
            context,
            obj,
            bm,
            layer,
            islands,
            session_nonce="snapshot-test",
            generation=4,
            options=_options(),
        )
        obj.mode = "OBJECT"
        guard.request_validation()
        self.assertEqual(guard.advance_validation(operation_budget=64), "invalid")
        self.assertEqual(guard.validation_slices, 0)

    def test_graph_builder_uses_immutable_material_and_is_slice_bounded(self):
        context, obj, bm, layer, islands = _fixture(2)
        builder = _build(context, obj, bm, layer, islands, budget=4)
        original_uv = builder.result.material.loop_by_key[(0, 0)][6]
        bm.faces[0].loops[0]._uv.uv.x = 99.0
        graph_builder = ADAPTER.SnapshotGraphBuilder(
            builder.result,
            (0,),
            builder.live_loop_map,
        )
        self.assertIsNone(graph_builder.advance(operation_budget=1))
        while not graph_builder.done:
            graph_builder.advance(operation_budget=2)
        graph_data, live_loops = graph_builder.result
        self.assertEqual(graph_data.loops[0].uv, original_uv)
        self.assertEqual(len(graph_data.loops), 3)
        self.assertEqual(len(graph_data.boundaries), 1)
        self.assertEqual(len(live_loops), 3)
        self.assertLess(graph_builder.max_slice_ms, 250.0)

    def test_graph_generation_is_complete_only(self):
        context, obj, bm, layer, islands = _fixture(1)
        builder = _build(context, obj, bm, layer, islands)
        graph_builder = ADAPTER.SnapshotGraphBuilder(
            builder.result,
            (0,),
            builder.live_loop_map,
        )
        for _ in range(4):
            self.assertIsNone(graph_builder.advance(operation_budget=1))
        self.assertFalse(graph_builder.done)
        self.assertIsNone(graph_builder.result)
        while not graph_builder.done:
            graph_builder.advance(operation_budget=8)
        self.assertIsNotNone(graph_builder.result)
        graph_builder.result[0].validate()

    def test_graph_matches_legacy_oracle_for_shared_edges_and_uv_split(self):
        context, obj, bm, layer, islands = _shared_two_face_fixture()
        builder = _build(context, obj, bm, layer, islands, budget=8)
        capture = builder.result
        graph_builder = ADAPTER.SnapshotGraphBuilder(capture, (0,), builder.live_loop_map)
        while not graph_builder.done:
            graph_builder.advance(operation_budget=8)
        snapshot_data, _live = graph_builder.result

        # The global material intentionally sees edge 1 and vertices 1/2 as
        # shared/split.  The selected-island graph must reproduce the legacy
        # per-island semantics instead.
        global_edge = capture.material.edge_by_key[1]
        self.assertFalse(global_edge[3])
        self.assertTrue(all(not item.seam for item in snapshot_data.loops))
        self.assertTrue(next(item for item in snapshot_data.edges if item.key == 1).boundary)

        from tests.unit.test_align_similar_selected import STACK_TOOLS

        legacy_graph, _legacy_loops = STACK_TOOLS._pro_graph_for_island(
            islands[0], layer
        )
        legacy_data = ADAPTER.graph_data_from_topology(legacy_graph, (0,))
        self.assertEqual(snapshot_data.to_wire(), legacy_data.to_wire())

    def test_boundary_oracle_preserves_holes_and_rejects_open_branch(self):
        def ring(prefix, side):
            points = (
                (0.0, 0.0),
                (side, 0.0),
                (side, side),
                (0.0, side),
            )
            loops = {}
            for index, point in enumerate(points):
                key = (prefix, index)
                next_key = (prefix, (index + 1) % 4)
                loops[key] = PAYLOAD.GraphLoopData(
                    key, prefix, (prefix, index), (prefix, index), next_key,
                    (prefix, (index - 1) % 4), point, True, False, ()
                )
            return loops

        loops = ring("outer", 4.0)
        hole = ring("hole", 1.0)
        loops.update(hole)
        boundary = ADAPTER._SnapshotBoundaryBuilder(loops)
        while not boundary.done:
            boundary.advance(2, None)
        self.assertEqual([item.role for item in boundary.boundaries], ["outer", "hole"])
        self.assertEqual(boundary.boundaries[1].parent_key, ("boundary", 0))

        branch = {
            ("edge", index): PAYLOAD.GraphLoopData(
                ("edge", index), "face", ("edge", index), index,
                ("end", index), ("prev", index), (0.0, 0.0), True
            )
            for index in range(3)
        }
        branch.update({
            ("end", index): PAYLOAD.GraphLoopData(
                ("end", index), "helper", ("helper", index), index + 10,
                ("end", index), ("end", index), (float(index + 1), 0.0), False
            )
            for index in range(3)
        })
        broken = ADAPTER._SnapshotBoundaryBuilder(branch)
        with self.assertRaises(ADAPTER.ProcessAdapterError) as raised:
            while not broken.done:
                broken.advance(8, None)
        self.assertEqual(str(raised.exception), "boundary_component_branch_or_open")

    def test_large_graph_has_bounded_primitive_and_deterministic_digest(self):
        context, obj, bm, layer, islands = _fixture(577)
        all_loops = tuple(loop for face in bm.faces for loop in face.loops)
        builder = _build(context, obj, bm, layer, [all_loops], budget=128)
        graph_builder = ADAPTER.SnapshotGraphBuilder(
            builder.result, tuple(range(577)), builder.live_loop_map
        )
        while not graph_builder.done:
            graph_builder.advance(operation_budget=64)
        data, _live = graph_builder.result
        data.validate()
        self.assertLess(graph_builder.max_slice_ms, 50.0)
        self.assertLess(graph_builder.max_primitive_ms, 50.0)
        self.assertEqual(graph_builder.max_primitive.get("kind"), "finalize")
        self.assertEqual(data.content_digest, data.computed_content_digest())


if __name__ == "__main__":
    unittest.main()
