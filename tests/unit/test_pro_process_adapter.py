"""MC3A pure adapter and snapshot-boundary tests."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


TOPOLOGY = _load("topology_correspondence", ROOT / "uv_gpt" / "topology_correspondence.py")
PAYLOAD = _load("pro_process_payload", ROOT / "uv_gpt" / "pro_process_payload.py")
ADAPTER = _load("pro_process_adapter", ROOT / "uv_gpt" / "pro_process_adapter.py")


def _triangle_graph(face_key=0, offset=(0.0, 0.0)):
    points = (
        (0.0 + offset[0], 0.0 + offset[1]),
        (1.0 + offset[0], 0.0 + offset[1]),
        (0.0 + offset[0], 1.0 + offset[1]),
    )
    loop_keys = tuple((face_key, index) for index in range(3))
    loops = tuple(
        TOPOLOGY.LoopRecord(
            key=loop_keys[index],
            face_key=face_key,
            edge_key=index,
            vertex_key=index,
            next_key=loop_keys[(index + 1) % 3],
            prev_key=loop_keys[(index - 1) % 3],
            uv=points[index],
            boundary=True,
        )
        for index in range(3)
    )
    return TOPOLOGY.make_graph(
        faces=(TOPOLOGY.FaceRecord(face_key, loop_keys),),
        edges=tuple(
            TOPOLOGY.EdgeRecord(index, (loop_keys[index],), (face_key,), True)
            for index in range(3)
        ),
        vertices=tuple(
            TOPOLOGY.VertexRecord(index, (loop_keys[index],), True)
            for index in range(3)
        ),
        loops=loops,
        boundaries=(TOPOLOGY.BoundaryComponentRecord("outer", loop_keys),),
    )


class _UV:
    def __init__(self, point):
        self.uv = types.SimpleNamespace(x=float(point[0]), y=float(point[1]))
        self.select = True
        self.select_edge = False


class _Vertex:
    def __init__(self, index):
        self.index = index
        self.select = False
        self.link_edges = []


class _Edge:
    def __init__(self, index):
        self.index = index
        self.link_faces = []
        self.select = False
        self.seam = False


class _Loop:
    def __init__(self, face, index, point, vertex, edge):
        self.face = face
        self.index = index
        self.vert = vertex
        self.edge = edge
        self._uv = _UV(point)

    def __getitem__(self, _layer):
        return self._uv


class _Face:
    def __init__(self, index, points):
        self.index = index
        self.select = True
        self.loops = []
        vertices = [_Vertex(index * 3 + offset) for offset in range(len(points))]
        edges = [_Edge(index * 3 + offset) for offset in range(len(points))]
        for vertex, edge in zip(vertices, edges):
            loop = _Loop(self, len(self.loops), points[len(self.loops)], vertex, edge)
            self.loops.append(loop)
            edge.link_faces.append(self)
            vertex.link_edges.append(edge)
        for offset, loop in enumerate(self.loops):
            loop.next = self.loops[(offset + 1) % len(self.loops)]
            loop.prev = self.loops[(offset - 1) % len(self.loops)]


class _FaceList(list):
    pass


class _BM:
    def __init__(self):
        self.faces = _FaceList(
            [_Face(0, ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)))]
        )
        self.faces.active = self.faces[0]


class AdapterTests(unittest.TestCase):
    def test_graph_pair_adapter_is_wire_only_and_round_trips(self):
        identity = PAYLOAD.SnapshotIdentity("adapter-session", 3, "snapshot")
        options = ADAPTER.make_exact_options(
            allow_flipping=True,
            match_scale=True,
            tolerance=1.0e-6,
            max_search=1024,
        )
        task = ADAPTER.make_single_pair_batch(
            identity,
            pair_ordinal=7,
            master_key=(0,),
            member_key=(1,),
            master_graph=_triangle_graph(0),
            member_graph=_triangle_graph(1),
            options=options,
        )
        task.validate()
        wire_round_trip = PAYLOAD.BatchTask.from_wire(task.to_wire())
        self.assertEqual(task, wire_round_trip)
        master_data = task.graph_map[task.pair_tasks[0].master_graph.graph_key]
        self.assertEqual(
            master_data.content_digest,
            ADAPTER.graph_data_from_topology(_triangle_graph(0), (0,)).content_digest,
        )
        self.assertNotIn("bpy", (ROOT / "uv_gpt" / "pro_process_adapter.py").read_text().split())

    def test_pair_result_conversion_preserves_mapping_and_rejection(self):
        identity = PAYLOAD.SnapshotIdentity("adapter-session", 3, "snapshot")
        options = ADAPTER.make_exact_options(
            allow_flipping=False,
            match_scale=True,
            tolerance=1.0e-6,
            max_search=1024,
        )
        task = ADAPTER.make_single_pair_batch(
            identity,
            pair_ordinal=0,
            master_key=(0,),
            member_key=(1,),
            master_graph=_triangle_graph(0),
            member_graph=_triangle_graph(1),
            options=options,
        )
        correspondence = TOPOLOGY.find_correspondence(
            _triangle_graph(0),
            _triangle_graph(1),
            max_search=1024,
        )
        pair_result = PAYLOAD.PairResult.from_correspondence(
            task.pair_tasks[0], correspondence
        )
        converted = ADAPTER.pair_result_to_correspondence(
            pair_result,
            topology_module=TOPOLOGY,
            task=task,
        )
        self.assertTrue(converted.accepted)
        self.assertEqual(tuple(converted.loop_mapping), tuple(correspondence.loop_mapping))
        rejected = PAYLOAD.PairResult(
            pair_ordinal=0,
            master_key=(0,),
            member_key=(1,),
            master_graph_digest=task.pair_tasks[0].master_graph.content_digest,
            member_graph_digest=task.pair_tasks[0].member_graph.content_digest,
            accepted=False,
            reason="topology_mismatch",
        )
        rejected_converted = ADAPTER.pair_result_to_correspondence(
            rejected,
            topology_module=TOPOLOGY,
            task=task,
        )
        self.assertFalse(rejected_converted.accepted)
        self.assertEqual(rejected_converted.loop_mapping, ())

    def test_snapshot_digest_covers_uv_selection_active_and_options(self):
        bm = _BM()
        obj = types.SimpleNamespace(
            name="Object",
            mode="EDIT",
            data=types.SimpleNamespace(
                name="Mesh",
                uv_layers=types.SimpleNamespace(
                    active=types.SimpleNamespace(name="UVMap.001")
                ),
            ),
        )
        context = types.SimpleNamespace(selected_objects=(obj,))
        layer = object()
        options = ADAPTER.make_exact_options(
            allow_flipping=False,
            match_scale=True,
            tolerance=1.0e-6,
            max_search=1024,
        )
        capture = ADAPTER.capture_snapshot(
            context,
            obj,
            bm,
            layer,
            [tuple(bm.faces[0].loops)],
            session_nonce="snapshot-session",
            generation=0,
            options=options,
        )
        changed_options = ADAPTER.make_exact_options(
            allow_flipping=True,
            match_scale=True,
            tolerance=1.0e-6,
            max_search=1024,
        )
        changed = ADAPTER.capture_snapshot(
            context,
            obj,
            bm,
            layer,
            [tuple(bm.faces[0].loops)],
            session_nonce="snapshot-session",
            generation=0,
            options=changed_options,
        )
        self.assertNotEqual(capture.identity.snapshot_digest, changed.identity.snapshot_digest)
        bm.faces[0].loops[0][layer].uv.x += 0.125
        uv_changed = ADAPTER.capture_snapshot(
            context,
            obj,
            bm,
            layer,
            [tuple(bm.faces[0].loops)],
            session_nonce="snapshot-session",
            generation=0,
            options=options,
        )
        self.assertNotEqual(capture.identity.snapshot_digest, uv_changed.identity.snapshot_digest)
        bm.faces[0].loops[0][layer].select = not bm.faces[0].loops[0][layer].select
        selection_changed = ADAPTER.capture_snapshot(
            context,
            obj,
            bm,
            layer,
            [tuple(bm.faces[0].loops)],
            session_nonce="snapshot-session",
            generation=0,
            options=options,
        )
        self.assertNotEqual(capture.identity.snapshot_digest, selection_changed.identity.snapshot_digest)

    def test_adapter_module_has_no_blender_import_or_mutable_payload_reference(self):
        source = (ROOT / "uv_gpt" / "pro_process_adapter.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imports = [
            node.names[0].name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
        ]
        self.assertNotIn("bpy", imports)
        self.assertNotIn("bmesh", imports)
        self.assertNotIn("multiprocessing", imports)


if __name__ == "__main__":
    unittest.main()
