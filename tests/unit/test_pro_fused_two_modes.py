"""Focused pure tests for the correspondence mode-aware fused route.

These tests exercise only immutable shape/adapter records and the pure worker
seams.  They never launch Blender, a helper, a process pool or touch a fixture.
"""

from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest import mock

from uv_gpt import pro_process_adapter as adapter
from uv_gpt import pro_process_payload as payload
from uv_gpt import pro_process_shape as shape
from uv_gpt import pro_process_worker as worker
from uv_gpt import similarity_matcher as matcher
from uv_gpt import topology_correspondence as topology


FAST = payload.CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY
EXACT = payload.CORRESPONDENCE_MODE_EXACT_ONLY
HYBRID = payload.CORRESPONDENCE_MODE_HYBRID
POINTS = ((0.0, 0.0), (2.0, 0.0), (0.0, 1.0))


def _segments(points):
    points = tuple(points)
    return tuple(zip(points, points[1:] + points[:1]))


def _polygon_graph(points, *, face_key=0):
    points = tuple(points)
    count = len(points)
    loop_keys = tuple((face_key, index) for index in range(count))
    loops = tuple(
        topology.LoopRecord(
            key=key,
            face_key=face_key,
            edge_key=index,
            vertex_key=index,
            next_key=loop_keys[(index + 1) % count],
            prev_key=loop_keys[(index - 1) % count],
            uv=points[index],
            boundary=True,
        )
        for index, key in enumerate(loop_keys)
    )
    edges = tuple(
        topology.EdgeRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            face_keys=(face_key,),
            boundary=True,
        )
        for index in range(count)
    )
    vertices = tuple(
        topology.VertexRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            boundary=True,
        )
        for index in range(count)
    )
    return topology.make_graph(
        faces=(topology.FaceRecord(face_key, loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(topology.BoundaryComponentRecord("outer", loop_keys, "outer"),),
    )


def _correspondence_result(*, accepted, reason="", source_face=0, target_face=0):
    mapping = (
        ((source_face, 0), (target_face, 0)),
        ((source_face, 1), (target_face, 1)),
        ((source_face, 2), (target_face, 2)),
    )
    transform = topology.SimilarityTransform2D(
        angle=0.0,
        scale=1.0,
        reflected=False,
        source_center=(0.0, 0.0),
        target_center=(0.0, 0.0),
    )
    return topology.CorrespondenceResult(
        accepted=bool(accepted),
        loop_mapping=mapping if accepted else (),
        score=0.0 if accepted else float("inf"),
        residual=0.0 if accepted else float("inf"),
        reason=reason,
        transform=transform if accepted else None,
    )


class FusedFixture:
    def __init__(self, mode=HYBRID, *, worker_local=False):
        self.mode = mode
        self.payload_module = worker._payload if worker_local else payload
        self.shape_module = worker if worker_local else shape
        self.pair_class = (
            worker.FusedBatchTask.__init__.__globals__["FusedPairRef"]
            if worker_local
            else shape.FusedPairRef
        )
        self.task_class = worker.FusedBatchTask if worker_local else shape.FusedBatchTask
        self.identity = self.payload_module.SnapshotIdentity(
            "fused-focused", 2, "snapshot"
        )
        self.options = self.payload_module.ExactOptions(
            allow_flipping=False,
            match_scale=True,
            tolerance=1.0e-8,
            max_search=100000,
        )
        master = self.shape_module.ShapeDescriptor.from_similarity(
            matcher.build_descriptor(_segments(POINTS))
        )
        member = self.shape_module.ShapeDescriptor.from_similarity(
            matcher.build_descriptor(_segments(POINTS))
        )
        self.master_descriptor = master
        self.member_descriptor = member
        self.pair = self.pair_class(
            pair_ordinal=0,
            master_key=(0,),
            member_key=(1,),
            master_descriptor_digest=master.descriptor_digest,
            member_descriptor_digest=member.descriptor_digest,
            master_loop_keys=tuple((0, index) for index in range(3)),
            member_loop_keys=tuple((1, index) for index in range(3)),
            exact_options=self.options,
            correspondence_mode=mode,
        )
        self.task = self.task_class(
            identity=self.identity,
            context_digest="context",
            fused_digest="fused",
            batch_id="fused-batch",
            pair_tasks=(self.pair,),
        )

    def worker_state(self):
        state = worker._WorkerState(
            session_nonce=self.identity.session_nonce,
            generation=self.identity.generation,
            ready=True,
        )
        state.graph_context_payload = SimpleNamespace(identity=self.identity)
        state.graph_context_material = object()
        state.graph_context_digest = self.task.context_digest
        state.fused_context_digest = self.task.fused_digest
        state.fused_shape_options = self.shape_module.ShapeOptions(
            match_scale=True,
            allow_flipping=False,
            tolerance=0.01,
            allow_tolerant_topology=True,
            use_numpy=False,
        )
        state.fused_descriptors = {
            self.master_descriptor.descriptor_digest: self.master_descriptor,
            self.member_descriptor.descriptor_digest: self.member_descriptor,
        }
        return state

    def graph_batch(self, *, accepted=True):
        graphs = {
            (0,): _polygon_graph(POINTS, face_key=0),
            (1,): _polygon_graph(POINTS, face_key=1),
        }
        graph_data_cls = worker.GraphBuildEntry.__init__.__globals__["GraphData"]
        entry_cls = worker.GraphBuildEntry

        def _build(_state, graph_task):
            entries = []
            for item in graph_task.graph_items:
                key = tuple(item.island_key)
                if accepted:
                    data = graph_data_cls.from_topology(graphs[key], "graph-%s" % key[0])
                    entries.append(
                        entry_cls(
                            island_key=key,
                            material_digest=item.material_digest,
                            accepted=True,
                            graph=data,
                        )
                    )
                else:
                    entries.append(
                        entry_cls(
                            island_key=key,
                            material_digest=item.material_digest,
                            accepted=False,
                            reason="graph_rejected",
                        )
                    )
            return SimpleNamespace(
                graph_results=tuple(entries),
                cache_hits=0,
                compute_ms=0.0,
            )

        return _build


class FusedWireAndCacheTests(unittest.TestCase):
    def test_all_modes_round_trip_and_separate_task_cache_identity(self):
        tasks = [FusedFixture(mode).task for mode in (FAST, EXACT, HYBRID)]
        self.assertEqual(
            [shape.FusedPairRef.from_wire(task.pair_tasks[0].to_wire()).correspondence_mode for task in tasks],
            [FAST, EXACT, HYBRID],
        )
        self.assertEqual(
            len({task.payload_digest() for task in tasks}),
            3,
        )
        self.assertEqual(
            len({task.cache_key() for task in tasks}),
            3,
        )
        malformed = tasks[0].pair_tasks[0].to_wire()[:-1] + (None,)
        with self.assertRaisesRegex(
            payload.PayloadValidationError,
            "missing correspondence_mode",
        ):
            shape.FusedPairRef.from_wire(malformed)

    def test_legacy_pair_wire_is_hybrid_only_and_adapter_rejects_fast(self):
        fixture = FusedFixture(HYBRID)
        pair = fixture.pair
        old_wire = (
            "fused-pair",
            pair.pair_ordinal,
            pair.master_key,
            pair.member_key,
            pair.master_descriptor_digest,
            pair.member_descriptor_digest,
            pair.master_loop_keys,
            pair.member_loop_keys,
            pair.exact_options.to_wire(),
            None,
        )
        legacy = shape.FusedPairRef.from_wire(old_wire)
        self.assertEqual(legacy.correspondence_mode, HYBRID)
        self.assertTrue(legacy.legacy_mode_less)
        with self.assertRaisesRegex(payload.PayloadValidationError, "legacy fused pair wire"):
            shape.FusedPairRef.from_wire(old_wire, requested_mode=FAST)

        old_task = shape.FusedBatchTask(
            identity=fixture.identity,
            context_digest=fixture.task.context_digest,
            fused_digest=fixture.task.fused_digest,
            batch_id=fixture.task.batch_id,
            pair_tasks=(legacy,),
        )
        with self.assertRaisesRegex(payload.PayloadValidationError, "legacy fused pair wire"):
            shape.FusedBatchTask.from_wire(old_task.to_wire(), requested_mode=FAST)

        old_spec = (
            0,
            (0,),
            (1,),
            pair.master_descriptor_digest,
            pair.member_descriptor_digest,
            pair.master_loop_keys,
            pair.member_loop_keys,
            pair.exact_options,
            None,
        )
        task = adapter.make_fused_batch(
            fixture.identity,
            "context",
            "fused",
            (old_spec,),
            batch_id="legacy-hybrid",
        )
        self.assertEqual(task.correspondence_mode, HYBRID)
        with self.assertRaisesRegex(adapter.ProcessAdapterError, "non-HYBRID"):
            adapter.make_fused_batch(
                fixture.identity,
                "context",
                "fused",
                (old_spec,),
                batch_id="legacy-fast",
                correspondence_mode=FAST,
            )

        current_fast = old_spec + (FAST,)
        fast_task = adapter.make_fused_batch(
            fixture.identity,
            "context",
            "fused",
            (current_fast,),
            batch_id="current-fast",
            correspondence_mode=FAST,
        )
        self.assertEqual(fast_task.correspondence_mode, FAST)
        with self.assertRaisesRegex(adapter.ProcessAdapterError, "conflicts"):
            adapter.make_fused_batch(
                fixture.identity,
                "context",
                "fused",
                (current_fast,),
                batch_id="wrong-mode",
                correspondence_mode=EXACT,
            )

    def test_result_digest_and_validation_are_mode_separated(self):
        results = []
        for mode in (FAST, EXACT, HYBRID):
            fixture = FusedFixture(mode)
            shape_result = shape.ShapePairResult.from_prefilter(
                fixture.pair.to_shape_pair(shape.ShapeOptions()),
                shape.ShapePrefilterData(
                    reason="shape_rejected",
                    coarse_gate=shape.ShapeGateData(False, True, reason="coarse"),
                    topology_gate=shape.ShapeGateData(False, True, reason="topology"),
                ),
            )
            result = shape.FusedBatchResult(
                identity=fixture.identity,
                context_digest=fixture.task.context_digest,
                fused_digest=fixture.task.fused_digest,
                batch_id=fixture.task.batch_id,
                payload_digest=fixture.task.payload_digest(),
                outcomes=(
                    shape.FusedPairOutcome(
                        pair_ordinal=0,
                        shape_result=shape_result,
                        terminal_reason="shape_rejected",
                        correspondence_mode=mode,
                    ),
                ),
            )
            result.validate_against(fixture.task)
            results.append(result)
        self.assertEqual(len({result.result_digest() for result in results}), 3)

        fixture = FusedFixture(FAST)
        shape_result = shape.ShapePairResult.from_prefilter(
            fixture.pair.to_shape_pair(shape.ShapeOptions()),
            shape.ShapePrefilterData(
                reason="shape_rejected",
                coarse_gate=shape.ShapeGateData(False, True, reason="coarse"),
                topology_gate=shape.ShapeGateData(False, True, reason="topology"),
            ),
        )
        cross_mode = shape.FusedBatchResult(
            identity=fixture.identity,
            context_digest=fixture.task.context_digest,
            fused_digest=fixture.task.fused_digest,
            batch_id=fixture.task.batch_id,
            payload_digest=fixture.task.payload_digest(),
            outcomes=(
                shape.FusedPairOutcome(
                    pair_ordinal=0,
                    shape_result=shape_result,
                    terminal_reason="shape_rejected",
                    correspondence_mode=EXACT,
                ),
            ),
        )
        with self.assertRaisesRegex(payload.PayloadValidationError, "outcome correspondence mode"):
            cross_mode.validate_against(fixture.task)


class FusedWorkerModeTests(unittest.TestCase):
    def test_mode_dispatch_never_crosses_solver_boundaries(self):
        options = payload.ExactOptions()
        accepted = _correspondence_result(accepted=True)
        rejected = _correspondence_result(accepted=False, reason="fallback_required")
        with mock.patch.object(
            worker._verified_nearest,
            "find_verified_nearest",
            return_value=accepted,
        ) as nearest, mock.patch.object(
            worker._topology,
            "find_correspondence",
            side_effect=AssertionError("FAST called exact"),
        ):
            result, fast = worker._correspondence_for_mode(
                object(), object(), options, FAST
            )
        self.assertIs(result, accepted)
        self.assertIs(fast, accepted)
        nearest.assert_called_once()

        with mock.patch.object(
            worker._verified_nearest,
            "find_verified_nearest",
            return_value=rejected,
        ) as nearest, mock.patch.object(
            worker._topology,
            "find_correspondence",
            side_effect=AssertionError("FAST miss called exact"),
        ):
            result, fast = worker._correspondence_for_mode(
                object(), object(), options, FAST
            )
        self.assertIs(result, rejected)
        self.assertIs(fast, rejected)
        nearest.assert_called_once()

        with mock.patch.object(
            worker._verified_nearest,
            "find_verified_nearest",
            side_effect=AssertionError("EXACT called nearest"),
        ), mock.patch.object(
            worker._topology,
            "find_correspondence",
            return_value=accepted,
        ) as exact:
            result, fast = worker._correspondence_for_mode(
                object(), object(), options, EXACT
            )
        self.assertIs(result, accepted)
        self.assertIsNone(fast)
        exact.assert_called_once()

        with mock.patch.object(
            worker._verified_nearest,
            "find_verified_nearest",
            return_value=rejected,
        ) as nearest, mock.patch.object(
            worker._topology,
            "find_correspondence",
            return_value=accepted,
        ) as exact:
            result, fast = worker._correspondence_for_mode(
                object(), object(), options, HYBRID
            )
        self.assertIs(result, accepted)
        self.assertIs(fast, rejected)
        nearest.assert_called_once()
        exact.assert_called_once()

    def test_fused_worker_fast_miss_and_exact_have_honest_diagnostics(self):
        for mode, nearest_result, exact_result, expected_nearest, expected_fallback, expected_primary in (
            (FAST, _correspondence_result(accepted=False, reason="fallback_required"), None, 1, 0, 0),
            (
                EXACT,
                None,
                _correspondence_result(accepted=True, source_face=1, target_face=0),
                0,
                0,
                1,
            ),
        ):
            fixture = FusedFixture(mode, worker_local=True)
            dispatch_result = nearest_result or exact_result
            with mock.patch.object(
                worker,
                "_compute_graph_batch",
                side_effect=fixture.graph_batch(accepted=True),
            ), mock.patch.object(
                worker,
                "_correspondence_for_mode",
                return_value=(dispatch_result, nearest_result),
            ) as dispatch, mock.patch.object(
                worker,
                "_verified_exact_correspondence",
                side_effect=AssertionError("fused dispatch bypassed mode selector"),
            ):
                result = worker._compute_fused_batch(fixture.worker_state(), fixture.task)
            outcome = result.outcomes[0]
            self.assertEqual(outcome.correspondence_mode, mode)
            self.assertEqual(outcome.exact_result.correspondence_mode, mode)
            self.assertEqual(dispatch.call_args.args[3], mode)
            metrics = dict(outcome.exact_result.diagnostics)
            self.assertEqual(metrics.get("nearest_attempted", 0), expected_nearest)
            self.assertEqual(metrics.get("exact_fallback_calls", 0), expected_fallback)
            self.assertEqual(metrics.get("exact_primary_calls", 0), expected_primary)
            result.validate_against(fixture.task)

    def test_fused_graph_rejection_preserves_mode_and_never_calls_solver(self):
        for mode in (FAST, EXACT, HYBRID):
            fixture = FusedFixture(mode, worker_local=True)
            with mock.patch.object(
                worker,
                "_compute_graph_batch",
                side_effect=fixture.graph_batch(accepted=False),
            ), mock.patch.object(
                worker,
                "_correspondence_for_mode",
                side_effect=AssertionError("graph rejection invoked correspondence"),
            ):
                result = worker._compute_fused_batch(fixture.worker_state(), fixture.task)
            outcome = result.outcomes[0]
            self.assertEqual(outcome.correspondence_mode, mode)
            self.assertEqual(outcome.exact_result.correspondence_mode, mode)
            metrics = dict(outcome.exact_result.diagnostics)
            self.assertEqual(metrics.get("graph_rejected_before_nearest"), 1)
            self.assertEqual(metrics.get("exact_primary_calls"), 0)
            result.validate_against(fixture.task)


if __name__ == "__main__":
    unittest.main()
