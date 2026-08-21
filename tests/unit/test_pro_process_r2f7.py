"""Pure/fake MC4-R2F7 contract tests.

These tests deliberately stay on immutable payloads and small lifecycle
doubles.  They never launch Blender, a helper process, or read the fixture.
"""

from __future__ import annotations

import importlib
from pathlib import Path
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
R2F5 = importlib.import_module("tests.unit.test_pro_process_r2f5")
SNAPSHOT = importlib.import_module("tests.unit.test_pro_process_snapshot_guard")

STACK = R2F5.STACK_TOOLS
PAYLOAD = R2F5.PAYLOAD
WORKER = R2F5.WORKER


class _GraphData:
    def __init__(self, key="island", digest="content", token="graph"):
        self.graph_key = key
        self.content_digest = digest
        self.token = token
        self.calls = 0

    def to_topology_graph(self, _module):
        self.calls += 1
        return types.SimpleNamespace(token=self.token)


def _task(*, generation=1, context="context"):
    identity = PAYLOAD.SnapshotIdentity("r2f7-session", generation, "snapshot")
    return types.SimpleNamespace(identity=identity, context_digest=context)


class ResidentGraphCacheTests(unittest.TestCase):
    def test_complete_graph_is_materialized_once_per_worker_key_and_generation(self):
        state = WORKER._WorkerState(
            session_nonce="r2f7-session",
            generation=1,
            ready=True,
        )
        task = _task()
        graph_data = _GraphData()
        metrics = {"builds": 0.0, "hits": 0.0, "compute_ms": 0.0}

        first = WORKER._cached_graph(state, task, graph_data, metrics=metrics)
        second = WORKER._cached_graph(state, task, graph_data, metrics=metrics)
        equivalent_wire = _GraphData()
        third = WORKER._cached_graph(state, task, equivalent_wire, metrics=metrics)

        self.assertIs(first, second)
        self.assertIs(first, third)
        self.assertEqual(graph_data.calls, 1)
        self.assertEqual(equivalent_wire.calls, 0)
        self.assertEqual(metrics["builds"], 1.0)
        self.assertEqual(metrics["hits"], 2.0)

        next_generation = _task(generation=2)
        generation_graph = _GraphData()
        WORKER._cached_graph(state, next_generation, generation_graph, metrics=metrics)
        self.assertEqual(generation_graph.calls, 1)

        next_context = _task(context="context-2")
        context_graph = _GraphData()
        WORKER._cached_graph(state, next_context, context_graph, metrics=metrics)
        self.assertEqual(context_graph.calls, 1)

    def test_rejected_or_incomplete_materialization_never_enters_complete_cache(self):
        state = WORKER._WorkerState(
            session_nonce="r2f7-session",
            generation=1,
            ready=True,
        )
        task = _task()
        graph_data = _GraphData()
        with mock.patch.object(
            WORKER._topology,
            "_validate_graph",
            side_effect=ValueError("synthetic incomplete graph"),
        ):
            with self.assertRaises(ValueError):
                WORKER._cached_graph(state, task, graph_data, validate=True)
        self.assertEqual(graph_data.calls, 1)

        # A later complete conversion must run again; the rejected object was
        # not retained as if it were a valid immutable topology graph.
        complete = WORKER._cached_graph(state, task, graph_data)
        self.assertEqual(graph_data.calls, 2)
        self.assertEqual(complete.token, "graph")

    def test_resident_wire_round_trip_exposes_topology_cache_metrics(self):
        identity = PAYLOAD.SnapshotIdentity("r2f7-session", 1, "snapshot")
        result = PAYLOAD.ResidentExactBatchResult(
            identity=identity,
            context_digest="context",
            batch_id="resident-r2f7",
            payload_digest="payload",
            pair_results=(),
            complete=True,
            topology_cache_builds=3,
            topology_cache_hits=17,
            topology_compute_ms=4.5,
        )
        restored = PAYLOAD.ResidentExactBatchResult.from_wire(result.to_wire())
        self.assertEqual(restored.topology_cache_builds, 3)
        self.assertEqual(restored.topology_cache_hits, 17)
        self.assertEqual(restored.topology_compute_ms, 4.5)
        self.assertEqual(result.result_digest(), restored.result_digest())


class FinalizationGraceTests(unittest.TestCase):
    def test_terminal_pipeline_gets_multislice_validation_grace_and_done_chain(self):
        progress = R2F5._Progress(
            stage="done",
            pairs_total=1,
            merged_pairs=1,
            complete=True,
        )
        session = R2F5._session()
        session._process_pipeline = R2F5._DonePipeline(progress)
        session._process_snapshot_guard = R2F5._SliceGuard(slices=4)
        session._process_pool = None
        session.state = "process_pipeline"
        seen = []
        for _ in range(12):
            seen.append(session.state)
            if session.state == "process_pipeline":
                session._advance_process_pipeline()
            elif session.state == "process_shutdown":
                session._advance_process_shutdown()
            elif session.state == "finish":
                session._finalize(apply=True)
            else:
                break
            if session.done:
                seen.append(session.state)
                break
        compressed = []
        for state in seen:
            if not compressed or compressed[-1] != state:
                compressed.append(state)
        self.assertEqual(
            compressed,
            ["process_pipeline", "process_shutdown", "finish", "done"],
        )
        self.assertTrue(session.report["process_finalization_grace_no_dispatch"])
        self.assertGreaterEqual(session.report["process_finalization_grace_rounds"], 1)
        self.assertEqual(session.report["process_validation_status"], "valid")
        self.assertTrue(session.report["process_result_digest"])

    def test_incomplete_semantic_work_at_deadline_is_fail_closed_and_zero_write(self):
        progress = R2F5._Progress(
            stage="exact_wait",
            pairs_total=2,
            merged_pairs=1,
            complete=False,
            active_workers=1,
            queue_depth=1,
        )
        pipeline = R2F5._DonePipeline(progress, canonical_ordinals=(0, 1))
        pipeline.stage = "exact_wait"
        pipeline.has_consumable_result = False
        session = R2F5._session()
        session._process_pipeline = pipeline
        session._staged_writes = [("target", "loop", (1.0, 2.0))]
        session.report["aligned_exact"] = 1
        session.state = "process_pipeline"
        session._request_timeout("r2f7-incomplete")
        session._finalize(apply=True)
        self.assertTrue(session.cancelled)
        self.assertEqual(session._staged_writes, [])
        self.assertEqual(session.report["exact_loop_writes"], 0)
        self.assertFalse(session.report["process_finalization_grace_no_dispatch"])

    def test_grace_expiry_does_not_dispatch_and_clears_staged_writes(self):
        progress = R2F5._Progress(
            stage="done", pairs_total=1, merged_pairs=1, complete=True
        )
        pipeline = R2F5._DonePipeline(progress)
        session = R2F5._session()
        session._process_pipeline = pipeline
        session._process_snapshot_guard = R2F5._SliceGuard(slices=999)
        session._staged_writes = [("target", "loop", (1.0, 2.0))]
        session.state = "process_pipeline"
        session._advance_process_pipeline()
        self.assertTrue(session._process_finalization_grace_active)
        advance_calls = getattr(pipeline, "advance_calls", 0)
        session._process_finalization_grace_deadline = time.perf_counter() - 1.0
        session._advance_process_pipeline()
        self.assertEqual(getattr(pipeline, "advance_calls", 0), advance_calls)
        self.assertEqual(session._staged_writes, [])
        self.assertTrue(session.cancelled or session.done)


class SnapshotBoundedPrimitiveTests(unittest.TestCase):
    def _complete(self, builder):
        while not builder.done:
            builder.advance(operation_budget=1)
        return builder.result

    def test_incremental_snapshot_finalization_is_resumable_and_digest_stable(self):
        context, obj, bm, layer, islands = SNAPSHOT._fixture(24)
        first = SNAPSHOT._build(context, obj, bm, layer, islands, budget=3)
        first_digest = first.result.identity.snapshot_digest
        self.assertLess(first.max_primitive_ms, 250.0)
        self.assertIn(("islands", "finalize"), first.phase_transitions)
        self.assertIn(("finalize", "done"), first.phase_transitions)

        context2, obj2, bm2, layer2, islands2 = SNAPSHOT._fixture(24)
        second = SNAPSHOT._build(context2, obj2, bm2, layer2, islands2, budget=3)
        self.assertEqual(first_digest, second.result.identity.snapshot_digest)
        self.assertEqual(
            first.result.material.mesh,
            second.result.material.mesh,
        )

    def test_blocking_finalize_primitive_is_observable_and_not_hidden(self):
        context, obj, bm, layer, islands = SNAPSHOT._fixture(4)
        builder = SNAPSHOT.ADAPTER.IncrementalSnapshotBuilder(
            context,
            obj,
            bm,
            layer,
            islands,
            session_nonce="r2f7-slow",
            generation=4,
            options=SNAPSHOT._options(),
        )
        while builder._phase != "finalize":
            builder.advance(operation_budget=3)
        original = builder._advance_finalize_one
        slow_once = [True]

        def deliberately_slow_finalize():
            if slow_once[0]:
                slow_once[0] = False
                time.sleep(0.26)
            original()

        with mock.patch.object(builder, "_advance_finalize_one", deliberately_slow_finalize):
            self._complete(builder)
        self.assertGreaterEqual(builder.max_primitive_ms, 250.0)
        self.assertEqual(builder.max_primitive.get("kind"), "finalize")


class PipelineDiagnosticTests(unittest.TestCase):
    def test_pipeline_subphase_metric_is_recorded_without_dispatching_new_work(self):
        progress = R2F5._Progress(
            stage="done", pairs_total=1, merged_pairs=1, complete=True
        )
        session = R2F5._session()
        session._process_pipeline = R2F5._DonePipeline(progress)
        session._process_pool = None
        session._process_snapshot_guard = R2F5._SliceGuard(slices=2)
        session.state = "process_pipeline"
        session._advance_process_pipeline()
        self.assertIn("pipeline_advance", session.report["process_pipeline_subphase_ms"])
        self.assertEqual(
            session.report["process_pipeline_max_subphase"], "pipeline_advance"
        )
        self.assertTrue(session.report["process_finalization_grace_no_dispatch"])


if __name__ == "__main__":
    unittest.main()
