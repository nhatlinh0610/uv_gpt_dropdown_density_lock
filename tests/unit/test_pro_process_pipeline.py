"""MC3B stage-transition, PID reuse and canonical merge tests."""

from __future__ import annotations

from pathlib import Path
import sys
import time
from types import SimpleNamespace
import unittest

from uv_gpt import similarity_matcher, topology_correspondence
from uv_gpt.pro_process_adapter import make_exact_options, make_single_pair_batch
from uv_gpt.pro_process_payload import ExactOptions, SnapshotIdentity
from uv_gpt.pro_process_pipeline import FusedProcessPipeline, ProcessPipeline
from uv_gpt.pro_process_pool import PersistentWorkerPool
from uv_gpt.pro_process_shape import (
    FusedBatchResult,
    FusedBatchTask,
    FusedPairOutcome,
    FusedPairRef,
    ShapeGateData,
    ShapeOptions,
    ShapePairResult,
    ShapePrefilterData,
    make_shape_batch,
)


ROOT = Path(__file__).resolve().parents[2]


def _triangle_descriptor(face_key):
    points = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
    segments = tuple((points[index], points[(index + 1) % 3]) for index in range(3))
    return similarity_matcher.build_descriptor(
        segments,
        face_key=(face_key,),
        topology={"face_count": 1, "edge_count": 3, "vertex_count": 3},
    )


def _triangle_graph(face_key):
    points = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))
    loop_keys = tuple((face_key, index) for index in range(3))
    loops = tuple(
        topology_correspondence.LoopRecord(
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
    return topology_correspondence.make_graph(
        faces=(topology_correspondence.FaceRecord(face_key, loop_keys),),
        edges=tuple(
            topology_correspondence.EdgeRecord(index, (loop_keys[index],), (face_key,), True)
            for index in range(3)
        ),
        vertices=tuple(
            topology_correspondence.VertexRecord(index, (loop_keys[index],), True)
            for index in range(3)
        ),
        loops=loops,
        boundaries=(topology_correspondence.BoundaryComponentRecord("outer", loop_keys),),
    )


class PipelineTests(unittest.TestCase):
    def test_fused_terminal_coverage_differs_from_exact_attempt_coverage(self):
        """A prefiltered fused pair is terminal without an exact attempt.

        ``exact_submitted`` counts compact fused pair refs dispatched to the
        worker, while ``exact_total`` counts only shape-accepted pairs that
        actually enter correspondence.  Keeping this distinction explicit
        prevents a rejected prefilter from being reported as missing exact
        coverage after the fused batch has completed.
        """

        identity = SnapshotIdentity("fused-accounting", 0, "snapshot")
        descriptor = _triangle_descriptor(0)
        prefilter = ShapePrefilterData(
            reason="prefilter_rejected",
            coarse_gate=ShapeGateData(False, True, reason="coarse"),
            topology_gate=ShapeGateData(False, True, reason="topology"),
        )
        shape_task = make_shape_batch(
            identity,
            ((0, (0,), (1,), descriptor, descriptor, prefilter),),
            ShapeOptions(),
            batch_id="shape-accounting",
        )
        shape_pair = shape_task.pair_tasks[0]
        shape_result = ShapePairResult.from_prefilter(shape_pair, prefilter)
        fused_ref = FusedPairRef(
            pair_ordinal=0,
            master_key=shape_pair.master_key,
            member_key=shape_pair.member_key,
            master_descriptor_digest=shape_pair.master_descriptor_digest,
            member_descriptor_digest=shape_pair.member_descriptor_digest,
            master_loop_keys=(),
            member_loop_keys=(),
            exact_options=ExactOptions(),
        )
        fused_task = FusedBatchTask(
            identity=identity,
            context_digest="context",
            fused_digest="fused",
            batch_id="fused-accounting",
            pair_tasks=(fused_ref,),
        )
        fused_result = FusedBatchResult(
            identity=identity,
            context_digest=fused_task.context_digest,
            fused_digest=fused_task.fused_digest,
            batch_id=fused_task.batch_id,
            payload_digest="payload",
            outcomes=(FusedPairOutcome(
                pair_ordinal=0,
                shape_result=shape_result,
                exact_result=None,
                terminal_reason="shape_prefilter_rejected",
            ),),
        )

        pipeline = object.__new__(FusedProcessPipeline)
        pipeline._task_meta = {
            fused_task.batch_id: ("fused", (0,)),
        }
        pipeline._shape_results = {}
        pipeline._exact_results = {}
        pipeline._event_epoch = 0
        pipeline._fused_batches_completed = 0
        pipeline._fused_graph_cache_builds = 0
        pipeline._fused_graph_cache_hits = 0
        pipeline._fused_graph_compute_ms = 0.0
        pipeline._fused_exact_compute_ms = 0.0
        pipeline._fused_shape_compute_ms = 0.0
        pipeline._fused_shape_cache_hits = 0
        pipeline._shape_completed = 0
        pipeline._shape_accepted = 0
        pipeline._shape_rejected = 0
        pipeline._exact_total = 0
        pipeline._exact_completed = 0
        pipeline._exact_accepted = 0
        pipeline._last_progress_kind = ""
        pipeline.stage = "fused_wait"

        pipeline._handle_completion(SimpleNamespace(
            batch_id=fused_task.batch_id,
            result=fused_result,
        ))

        terminal_pairs = pipeline._shape_completed
        exact_attempts = pipeline._exact_total
        self.assertEqual(terminal_pairs, 1)
        self.assertEqual(exact_attempts, 0)
        self.assertNotEqual(terminal_pairs, exact_attempts)
        self.assertEqual(pipeline._fused_batches_completed, 1)

    def _make_operation(self, worker_count, *, delay=0):
        nonce = "pipeline-%d-%d" % (worker_count, delay)
        identity = SnapshotIdentity(nonce, 0, "pipeline-snapshot")
        descriptor = _triangle_descriptor(0)
        shape_batches = tuple(
            make_shape_batch(
                identity,
                ((ordinal, (0,), (ordinal + 1,), descriptor, descriptor),),
                ShapeOptions(tolerance=0.1),
                batch_id="shape-%02d" % ordinal,
                debug_delay_ms=delay,
            )
            for ordinal in range(4)
        )
        exact_options = make_exact_options(
            allow_flipping=False,
            match_scale=True,
            tolerance=1.0e-6,
            max_search=1024,
        )
        exact_batches = tuple(
            make_single_pair_batch(
                identity,
                pair_ordinal=ordinal,
                master_key=(0,),
                member_key=(ordinal + 1,),
                master_graph=_triangle_graph(0),
                member_graph=_triangle_graph(ordinal + 1),
                options=exact_options,
            )
            for ordinal in range(4)
        )
        pool = PersistentWorkerPool(
            worker_count,
            python_executable=sys.executable,
            session_nonce=nonce,
            use_cache=False,
        )
        merged = []
        stage_pids = []

        def exact_builder(_shape_results):
            stage_pids.append(tuple(pool.worker_pids))
            return exact_batches

        pipeline = ProcessPipeline(
            pool,
            shape_batches,
            exact_builder=exact_builder,
            merge_callback=merged.append,
            merge_limit=1,
        )
        return pipeline, pool, merged, stage_pids

    def _run(self, worker_count, *, delay=0):
        pipeline, pool, merged, stage_pids = self._make_operation(worker_count, delay=delay)
        try:
            pipeline.start()
            initial_pids = tuple(pool.worker_pids)
            max_queue = 0
            for _ in range(1000):
                progress = pipeline.advance(0.01)
                max_queue = max(max_queue, progress.queue_depth)
                if pipeline.is_terminal:
                    break
            self.assertEqual(pipeline.stage, "done", pipeline.progress())
            result = pipeline.final_result()
            self.assertTrue(result.complete)
            self.assertEqual(len(merged), 4)
            self.assertEqual(tuple(item.pair_ordinal for item in merged), (0, 1, 2, 3))
            self.assertLessEqual(max_queue, 2 * worker_count)
            self.assertTrue(stage_pids)
            self.assertEqual(set(stage_pids[0]), set(initial_pids))
            return result.result_digest, initial_pids, pipeline.progress()
        finally:
            pipeline.close()

    def test_pipeline_is_deterministic_and_reuses_pids_across_counts(self):
        digests = []
        for count in (1, 2, 4):
            digest, pids, progress = self._run(count)
            digests.append(digest)
            self.assertEqual(len(pids), count)
            self.assertEqual(len(set(pids)), count)
            if count > 1:
                self.assertGreaterEqual(len(progress.worker_distribution), 2)
        self.assertEqual(len(set(digests)), 1)
        repeated, _pids, _progress = self._run(4, delay=8)
        self.assertEqual(repeated, digests[-1])

    def test_cancel_discards_all_stages_and_owned_workers(self):
        pipeline, pool, _merged, _stage_pids = self._make_operation(2, delay=250)
        pipeline.start()
        self.assertFalse(pipeline.is_terminal)
        result = pipeline.cancel(timeout=1.0)
        self.assertFalse(result.complete)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.outcomes, ())
        self.assertEqual(pool.worker_pids, ())
        pipeline.cancel(timeout=1.0)
        pipeline.close()

    def test_exact_stage_failure_exposes_no_consumable_result(self):
        pipeline, pool, _merged, _stage_pids = self._make_operation(1)
        pipeline.exact_builder = lambda _shape_results: (object(),)
        try:
            pipeline.start()
            for _ in range(1000):
                pipeline.advance(0.01)
                if pipeline.is_terminal:
                    break
            self.assertEqual(pipeline.stage, "failed")
            self.assertFalse(pipeline.final_result().complete)
            self.assertEqual(pipeline.final_result().outcomes, ())
        finally:
            pipeline.close()


if __name__ == "__main__":
    unittest.main()
