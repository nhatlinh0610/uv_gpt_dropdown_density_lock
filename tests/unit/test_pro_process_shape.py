"""Pure MC3B descriptor/shape wire tests."""

from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest

from uv_gpt import similarity_matcher
from uv_gpt.pro_process_payload import (
    ExactOptions,
    FrameSizeError,
    SnapshotIdentity,
    stable_digest,
)
from uv_gpt import pro_process_shape as shape


ROOT = Path(__file__).resolve().parents[2]


def _descriptor(points, face_key):
    segments = tuple((points[index], points[(index + 1) % len(points)]) for index in range(len(points)))
    return similarity_matcher.build_descriptor(
        segments,
        face_key=(face_key,),
        topology={
            "face_count": 1,
            "edge_count": len(points),
            "vertex_count": len(points),
        },
    )


class ShapeSchemaTests(unittest.TestCase):
    def setUp(self):
        self.square = _descriptor(
            ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
            0,
        )
        self.triangle = _descriptor(
            ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)),
            1,
        )
        self.identity = SnapshotIdentity("shape-test", 2, "snapshot-digest")

    def test_descriptor_wire_round_trip_is_primitive_and_stable(self):
        descriptor = shape.ShapeDescriptor.from_similarity(self.square)
        wire = descriptor.to_wire()
        restored = shape.ShapeDescriptor.from_wire(wire)
        self.assertEqual(descriptor, restored)
        self.assertEqual(descriptor.descriptor_digest, restored.descriptor_digest)
        self.assertEqual(stable_digest(wire), stable_digest(dict(reversed(tuple(wire.items())))))
        self.assertNotIn("bpy", repr(wire).lower())

    def test_shape_batch_round_trip_and_order(self):
        task = shape.make_shape_batch(
            self.identity,
            (
                (3, (0,), (3,), self.square, self.square),
                (1, (0,), (1,), self.square, self.triangle),
                (2, (0,), (2,), self.square, self.square),
            ),
            shape.ShapeOptions(tolerance=0.1),
            batch_id="shape-order",
        )
        self.assertEqual(task.pair_ordinals, (1, 2, 3))
        self.assertEqual(task, shape.ShapeBatchTask.from_wire(task.to_wire()))
        estimate = task.estimate_frame()
        self.assertGreater(estimate.payload_bytes, 0)
        self.assertGreater(estimate.frame_bytes, estimate.payload_bytes)
        self.assertEqual(estimate.protocol, 5)

    def test_fused_batch_round_trip_decodes_worker_wire(self):
        master = shape.ShapeDescriptor.from_similarity(self.square)
        member = shape.ShapeDescriptor.from_similarity(self.square)
        task = shape.FusedBatchTask(
            identity=self.identity,
            context_digest="context-digest",
            fused_digest="fused-digest",
            batch_id="fused-wire",
            pair_tasks=(
                shape.FusedPairRef(
                    pair_ordinal=0,
                    master_key=(0,),
                    member_key=(1,),
                    master_descriptor_digest=master.descriptor_digest,
                    member_descriptor_digest=member.descriptor_digest,
                    master_loop_keys=((0, 0), (0, 1), (0, 2), (0, 3)),
                    member_loop_keys=((1, 0), (1, 1), (1, 2), (1, 3)),
                    exact_options=ExactOptions(),
                ),
            ),
        )
        self.assertEqual(task, shape.FusedBatchTask.from_wire(task.to_wire()))

    def test_rejected_infinite_match_metrics_are_not_serialized(self):
        task = shape.make_shape_batch(
            self.identity,
            ((0, (0,), (1,), self.square, self.triangle),),
            shape.ShapeOptions(tolerance=0.1, allow_tolerant_topology=False),
            batch_id="shape-reject",
        )
        direct = similarity_matcher.match_descriptors(
            self.square,
            self.triangle,
            tolerance=0.1,
            allow_tolerant_topology=False,
            count_candidate=False,
        )
        result = shape.ShapePairResult.from_similarity(task.pair_tasks[0], direct)
        self.assertFalse(result.accepted)
        self.assertTrue(result.score is None or math.isfinite(result.score))
        self.assertTrue(result.outer_rms is None or math.isfinite(result.outer_rms))
        result.validate(task.pair_tasks[0], task.descriptor_map)
        self.assertEqual(result, shape.ShapePairResult.from_wire(result.to_wire()))

    def test_wire_match_result_matches_direct_oracle(self):
        task = shape.make_shape_batch(
            self.identity,
            ((0, (0,), (1,), self.square, self.square),),
            shape.ShapeOptions(tolerance=0.1),
            batch_id="shape-accepted",
        )
        direct = similarity_matcher.match_descriptors(
            self.square,
            self.square,
            tolerance=0.1,
            count_candidate=False,
        )
        encoded = shape.ShapePairResult.from_similarity(task.pair_tasks[0], direct)
        decoded = shape.ShapePairResult.from_wire(encoded.to_wire())
        round_trip = decoded.to_similarity(similarity_matcher)
        self.assertEqual(round_trip.accepted, direct.accepted)
        self.assertAlmostEqual(round_trip.score, direct.score)
        self.assertEqual(round_trip.reason, direct.reason)
        self.assertEqual(round_trip.transform, direct.transform)

    def test_frame_limit_is_checked_before_dispatch(self):
        task = shape.make_shape_batch(
            self.identity,
            ((0, (0,), (1,), self.square, self.square),),
            shape.ShapeOptions(),
            batch_id="shape-frame",
        )
        old_limit = shape.MAX_FRAME_BYTES
        try:
            shape.MAX_FRAME_BYTES = 1
            with self.assertRaises(FrameSizeError):
                shape.estimate_shape_frame(task)
        finally:
            shape.MAX_FRAME_BYTES = old_limit

    def test_worker_module_has_no_blender_boundary(self):
        source = (ROOT / "uv_gpt" / "pro_process_worker.py").read_text(encoding="utf-8").lower()
        self.assertNotIn("import bpy", source)
        self.assertNotIn("import bmesh", source)
        self.assertNotIn("threadpoolexecutor", source)
        self.assertNotIn("processpoolexecutor", source)


if __name__ == "__main__":
    unittest.main()
