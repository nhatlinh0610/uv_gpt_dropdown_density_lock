"""Pure MC2 payload, partition, validation and serialization tests."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import math
from pathlib import Path
import sys
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


TOPOLOGY = _load("topology_correspondence", ROOT / "uv_gpt" / "topology_correspondence.py")
PAYLOAD = _load("pro_process_payload", ROOT / "uv_gpt" / "pro_process_payload.py")


def _polygon_graph(points, *, face_key=0, order=None):
    count = len(points)
    order = tuple(range(count)) if order is None else tuple(order)
    loops = []
    edges = []
    vertices = []
    face_loop_keys = tuple((face_key, index) for index in range(count))
    for index in range(count):
        vertex_index = order[index]
        loops.append(
            TOPOLOGY.LoopRecord(
                key=(face_key, index),
                face_key=face_key,
                edge_key=index,
                vertex_key=vertex_index,
                next_key=(face_key, (index + 1) % count),
                prev_key=(face_key, (index - 1) % count),
                uv=tuple(points[vertex_index]),
                boundary=True,
            )
        )
        edges.append(
            TOPOLOGY.EdgeRecord(index, ((face_key, index),), (face_key,), boundary=True)
        )
        vertices.append(
            TOPOLOGY.VertexRecord(vertex_index, ((face_key, index),), boundary=True)
        )
    return TOPOLOGY.make_graph(
        faces=(TOPOLOGY.FaceRecord(face_key, face_loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(TOPOLOGY.BoundaryComponentRecord("outer", face_loop_keys, "outer"),),
    )


def _task_pair(identity, master, member, ordinal=0, *, allow_flipping=False):
    master_data = PAYLOAD.GraphData.from_topology(master, "master")
    member_data = PAYLOAD.GraphData.from_topology(member, f"member-{ordinal}")
    pair = PAYLOAD.PairTask(
        pair_ordinal=ordinal,
        master_key="master",
        member_key=f"member-{ordinal}",
        master_graph=PAYLOAD.GraphRef(master_data.graph_key, master_data.content_digest),
        member_graph=PAYLOAD.GraphRef(member_data.graph_key, member_data.content_digest),
        options=PAYLOAD.ExactOptions(allow_flipping=allow_flipping, tolerance=1.0e-8),
    )
    return pair, (master_data, member_data)


class ProProcessPayloadTests(unittest.TestCase):
    def setUp(self):
        self.identity = PAYLOAD.SnapshotIdentity("mc2-session", 4, "snapshot-digest")
        self.master = _polygon_graph(((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)))
        self.member = _polygon_graph(((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)))
        self.pair, graph_values = _task_pair(self.identity, self.master, self.member)
        self.graphs = graph_values

    def test_digest_is_independent_of_mapping_insertion_order(self):
        left = PAYLOAD.stable_digest({"alpha": (1, 2), "beta": {"x": 3, "y": 4}})
        right = PAYLOAD.stable_digest({"beta": {"y": 4, "x": 3}, "alpha": (1, 2)})
        self.assertEqual(left, right)
        task_a = PAYLOAD.BatchTask(self.identity, "batch-1", (self.pair,), self.graphs)
        task_b = PAYLOAD.BatchTask(self.identity, "batch-1", (self.pair,), tuple(reversed(self.graphs)))
        self.assertEqual(task_a.payload_digest(), task_b.payload_digest())

    def test_partition_groups_master_affinity_and_preserves_all_ordinals(self):
        pairs = []
        graphs = []
        for ordinal in range(5):
            pair, values = _task_pair(self.identity, self.master, self.member, ordinal)
            pairs.append(pair)
            graphs.extend(values)
        batches = PAYLOAD.partition_batches(self.identity, pairs, graphs, batch_size=2)
        self.assertEqual(tuple(item.pair_ordinal for batch in batches for item in batch.pair_tasks), tuple(range(5)))
        self.assertEqual([len(batch.pair_tasks) for batch in batches], [2, 2, 1])
        self.assertEqual(len({item.batch_id for item in batches}), len(batches))
        for batch in batches:
            batch.validate()

    def test_interleaved_master_runs_keep_canonical_order_without_cross_affinity(self):
        identity = PAYLOAD.SnapshotIdentity("mc2-interleaved", 4, "snapshot-digest")
        master_a = PAYLOAD.GraphData.from_topology(self.master, "master-a")
        master_b = PAYLOAD.GraphData.from_topology(self.master, "master-b")
        members = [
            PAYLOAD.GraphData.from_topology(self.member, f"interleaved-member-{index}")
            for index in range(4)
        ]
        pairs = []
        for ordinal, member in enumerate(members):
            master = master_a if ordinal % 2 == 0 else master_b
            pairs.append(
                PAYLOAD.PairTask(
                    ordinal,
                    master.graph_key,
                    member.graph_key,
                    PAYLOAD.GraphRef(master.graph_key, master.content_digest),
                    PAYLOAD.GraphRef(member.graph_key, member.content_digest),
                )
            )
        batches = PAYLOAD.partition_batches(
            identity,
            pairs,
            (master_a, master_b, *members),
            batch_size=2,
        )
        self.assertEqual(
            tuple(item.pair_ordinal for batch in batches for item in batch.pair_tasks),
            (0, 1, 2, 3),
        )
        self.assertEqual([len(batch.pair_tasks) for batch in batches], [1, 1, 1, 1])
        self.assertTrue(all(len({pair.master_graph.graph_key for pair in batch.pair_tasks}) == 1 for batch in batches))

    def test_serialization_estimate_and_frame_cap(self):
        task = PAYLOAD.BatchTask(self.identity, "batch-1", (self.pair,), self.graphs)
        estimate = PAYLOAD.estimate_batch_frame(task)
        self.assertEqual(estimate.protocol, 5)
        self.assertGreater(estimate.payload_bytes, 0)
        self.assertGreater(estimate.frame_bytes, estimate.payload_bytes)
        with mock.patch.object(PAYLOAD, "MAX_FRAME_BYTES", 100):
            with self.assertRaises(PAYLOAD.FrameSizeError):
                PAYLOAD.estimate_batch_frame(task)

    def test_actual_correspondence_result_validates_full_bijection(self):
        task = PAYLOAD.BatchTask(self.identity, "batch-1", (self.pair,), self.graphs)
        result = TOPOLOGY.find_correspondence(
            self.master,
            self.member,
            tolerance=1.0e-8,
            allow_flipping=False,
        )
        pair_result = PAYLOAD.PairResult.from_correspondence(self.pair, result)
        batch_result = PAYLOAD.BatchResult(
            self.identity,
            task.batch_id,
            task.payload_digest(),
            (pair_result,),
        )
        batch_result.validate_against(task)
        self.assertTrue(pair_result.accepted)
        self.assertEqual(len(pair_result.loop_mapping), 4)
        self.assertTrue(math.isfinite(pair_result.score))

        malformed = replace(pair_result, loop_mapping=pair_result.loop_mapping[:-1])
        with self.assertRaises(PAYLOAD.PayloadValidationError):
            replace(batch_result, pair_results=(malformed,)).validate_against(task)

        rejected_with_mapping = replace(pair_result, accepted=False, loop_mapping=pair_result.loop_mapping, score=None, residual=None, transform=None)
        with self.assertRaises(PAYLOAD.PayloadValidationError):
            replace(batch_result, pair_results=(rejected_with_mapping,)).validate_against(task)

        malformed_wire = list(pair_result.to_wire())
        malformed_wire[7] = ((pair_result.loop_mapping[0][0],),)
        with self.assertRaises(PAYLOAD.PayloadValidationError):
            PAYLOAD.PairResult.from_wire(tuple(malformed_wire))

    def test_loop_refinement_diagnostics_roundtrip_and_semantic_digest(self):
        task = PAYLOAD.BatchTask(self.identity, "batch-1", (self.pair,), self.graphs)
        exact = TOPOLOGY.find_correspondence(
            self.master,
            self.member,
            tolerance=1.0e-8,
        )
        pair_result = PAYLOAD.PairResult.from_correspondence(self.pair, exact)
        names = dict(pair_result.diagnostics)
        self.assertGreaterEqual(names["refinement_rounds"], 1)
        self.assertGreaterEqual(names["refinement_max_rounds"], names["refinement_rounds"])
        self.assertIn("refinement_elapsed_us", names)
        decoded = PAYLOAD.PairResult.from_wire(pair_result.to_wire())
        self.assertEqual(decoded, pair_result)
        delayed = replace(
            pair_result,
            diagnostics=tuple(
                (name, value + 100000 if name == "refinement_elapsed_us" else value)
                for name, value in pair_result.diagnostics
            ),
        )
        first = PAYLOAD.BatchResult(
            self.identity,
            task.batch_id,
            task.payload_digest(),
            (pair_result,),
        )
        second = PAYLOAD.BatchResult(
            self.identity,
            task.batch_id,
            task.payload_digest(),
            (delayed,),
        )
        self.assertEqual(first.result_digest(), second.result_digest())

    def test_rejected_result_is_complete_but_has_empty_mapping(self):
        task = PAYLOAD.BatchTask(self.identity, "batch-1", (self.pair,), self.graphs)
        rejected = PAYLOAD.PairResult(
            pair_ordinal=0,
            master_key="master",
            member_key="member-0",
            master_graph_digest=self.pair.master_graph.content_digest,
            member_graph_digest=self.pair.member_graph.content_digest,
            accepted=False,
            reason="not_equivalent",
        )
        batch = PAYLOAD.BatchResult(self.identity, task.batch_id, task.payload_digest(), (rejected,))
        batch.validate_against(task)
        self.assertFalse(batch.pair_results[0].accepted)

    def test_cache_accepts_only_complete_validated_result(self):
        task = PAYLOAD.BatchTask(self.identity, "batch-1", (self.pair,), self.graphs)
        exact = TOPOLOGY.find_correspondence(self.master, self.member, tolerance=1.0e-8)
        pair_result = PAYLOAD.PairResult.from_correspondence(self.pair, exact)
        result = PAYLOAD.BatchResult(self.identity, task.batch_id, task.payload_digest(), (pair_result,))
        cache = PAYLOAD.CompleteResultCache()
        cache.put(task, result)
        self.assertEqual(cache.get(task.cache_key()), result)
        with self.assertRaises(PAYLOAD.PayloadValidationError):
            cache.put(task, replace(result, complete=False))
        cache.clear_generation(self.identity.generation)
        self.assertEqual(len(cache), 1)


if __name__ == "__main__":
    unittest.main()
