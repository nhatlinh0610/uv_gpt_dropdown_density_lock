"""Pure MC4-R2F1 integration oracles.

These tests deliberately stop at immutable graphs, fake workers, and the
existing pool state machine.  They never start Blender or an external helper.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from uv_gpt import pro_process_payload as payload
from uv_gpt import pro_process_pool as pool_module
from uv_gpt import pro_process_worker as worker_module
from uv_gpt import topology_correspondence as topology


def _triangle_graph(offset=(0.0, 0.0)):
    points = tuple(
        (float(x) + float(offset[0]), float(y) + float(offset[1]))
        for x, y in ((0.0, 0.0), (2.0, 0.0), (0.0, 1.0))
    )
    loop_keys = tuple((0, index) for index in range(3))
    loops = tuple(
        topology.LoopRecord(
            key=loop_keys[index],
            face_key=0,
            edge_key=index,
            vertex_key=index,
            next_key=loop_keys[(index + 1) % 3],
            prev_key=loop_keys[(index - 1) % 3],
            uv=points[index],
            boundary=True,
        )
        for index in range(3)
    )
    edges = tuple(
        topology.EdgeRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            face_keys=(0,),
            boundary=True,
        )
        for index in range(3)
    )
    vertices = tuple(
        topology.VertexRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            boundary=True,
        )
        for index in range(3)
    )
    return topology.make_graph(
        faces=(topology.FaceRecord(0, loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(topology.BoundaryComponentRecord("outer", loop_keys, "outer"),),
    )


class _FakeWorker:
    def __init__(self, *, ready=True, gate=None):
        self.is_ready = bool(ready)
        self.pid = None
        self.process = None
        self.closed = False
        self.started = threading.Event()
        self._gate = gate

    @property
    def is_alive(self):
        return not self.closed

    def start(self):
        self.started.set()
        if self._gate is not None:
            self._gate.wait(1.0)
        if self.closed:
            return
        self.is_ready = True

    def close(self, **_kwargs):
        self.closed = True
        self.is_ready = False


class _FakeShutdownWorker:
    """Pure owned-worker double for resumable graceful teardown tests."""

    def __init__(self, *, pending_rounds=2):
        self.is_ready = True
        self._alive = True
        self.process = object()
        self.pending_rounds = int(pending_rounds)
        self.begin_calls = 0
        self.advance_calls = 0
        self.force_calls = 0
        self.shutdown_requested = False

    @property
    def is_alive(self):
        return self._alive

    def begin_shutdown(self):
        self.begin_calls += 1
        self.shutdown_requested = True
        return False

    def advance_shutdown(self):
        self.advance_calls += 1
        if self.advance_calls > self.pending_rounds:
            self._alive = False
            self.process = None
            return "complete"
        return "acknowledged"

    def force_close_nonblocking(self):
        self.force_calls += 1
        self._alive = False
        self.process = None
        return "complete"

    def close(self, **_kwargs):
        self._alive = False
        self.process = None
        self.is_ready = False


class VerifiedNearestWorkerTests(unittest.TestCase):
    def test_fast_accept_does_not_construct_or_call_exact_search(self):
        graph = _triangle_graph()
        options = payload.ExactOptions()
        seed = topology.SimilarityTransform2D(
            angle=0.0,
            scale=1.0,
            reflected=False,
            source_center=(0.0, 0.0),
            target_center=(0.0, 0.0),
        )

        class UnexpectedSearch:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("fast accepted path instantiated CorrespondenceSearch")

        with mock.patch.object(
            worker_module._topology,
            "CorrespondenceSearch",
            UnexpectedSearch,
        ), mock.patch.object(
            worker_module._topology,
            "find_correspondence",
            side_effect=AssertionError("fast accepted path invoked exact fallback"),
        ):
            result, nearest = worker_module._verified_exact_correspondence(
                graph,
                graph,
                options,
                seed_transform=seed,
            )
        self.assertTrue(result.accepted)
        self.assertIs(result, nearest)
        self.assertEqual(len(result.loop_mapping), 3)

    def test_fast_miss_invokes_unchanged_exact_fallback_once(self):
        graph = _triangle_graph()
        options = payload.ExactOptions()
        original = worker_module._topology.find_correspondence
        calls = []

        def counted_fallback(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        with mock.patch.object(
            worker_module._topology,
            "find_correspondence",
            side_effect=counted_fallback,
        ):
            result, nearest = worker_module._verified_exact_correspondence(
                graph,
                graph,
                options,
                seed_transform=None,
            )
        self.assertTrue(result.accepted)
        self.assertFalse(nearest.accepted)
        self.assertEqual(nearest.reason, "fallback_required")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["max_search"], options.max_search)

        master_data = payload.GraphData.from_topology(graph, "master")
        member_data = payload.GraphData.from_topology(graph, "member")
        pair = payload.PairTask(
            pair_ordinal=0,
            master_key=(0,),
            member_key=(1,),
            master_graph=payload.GraphRef(
                master_data.graph_key,
                master_data.content_digest,
            ),
            member_graph=payload.GraphRef(
                member_data.graph_key,
                member_data.content_digest,
            ),
            options=options,
        )
        pair_result = payload.PairResult.from_correspondence(
            pair,
            result,
            nearest_result=nearest,
        )
        metrics = dict(pair_result.diagnostics)
        self.assertEqual(metrics["nearest_attempted"], 0)
        self.assertEqual(metrics["nearest_accepted"], 0)
        self.assertEqual(metrics["nearest_fallback"], 0)

    def test_worker_passes_fixed_nearest_operation_cap(self):
        graph = _triangle_graph()
        options = payload.ExactOptions()
        seed = topology.SimilarityTransform2D(
            angle=0.0,
            scale=1.0,
            reflected=False,
            source_center=(0.0, 0.0),
            target_center=(0.0, 0.0),
        )
        original = worker_module._verified_nearest.find_verified_nearest
        calls = []

        def capture(*args, **kwargs):
            calls.append(dict(kwargs))
            return original(*args, **kwargs)

        with mock.patch.object(
            worker_module._verified_nearest,
            "find_verified_nearest",
            side_effect=capture,
        ):
            result, _nearest = worker_module._verified_exact_correspondence(
                graph,
                graph,
                options,
                seed_transform=seed,
            )
        self.assertTrue(result.accepted)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0]["nearest_max_nodes"],
            worker_module.VERIFIED_NEAREST_MAX_NODES,
        )
        self.assertEqual(worker_module.VERIFIED_NEAREST_MAX_NODES, 4096)


class DeferredRetryAndFairnessTests(unittest.TestCase):
    def _make_pool_with_slot(self, replacement):
        pool = pool_module.PersistentWorkerPool(4)
        old = _FakeWorker()
        slot = pool_module._Slot(index=0, worker=old)
        pool._slots = [slot]
        pool._make_worker = lambda: replacement
        return pool, slot, old

    def test_restart_handshake_is_deferred_and_requeues_same_task(self):
        gate = threading.Event()
        replacement = _FakeWorker(ready=False, gate=gate)
        pool, slot, old = self._make_pool_with_slot(replacement)
        task = SimpleNamespace(
            batch_id="shape-1603",
            pair_ordinals=(1603,),
            operation_kind="shape",
        )
        active = pool_module._ActiveBatch(task, object(), time.perf_counter())

        started = time.perf_counter()
        pool._schedule_restart(
            slot,
            task,
            pool_module.WorkerCrashedError("synthetic final batch death"),
            is_context=False,
        )
        self.assertTrue(replacement.started.wait(0.25))
        pool._advance_pending_restarts(deadline=time.perf_counter() + 0.001)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.assertLess(elapsed_ms, 50.0)
        self.assertEqual(pool.restart_pending, 1)
        self.assertFalse(old.is_alive)

        gate.set()
        pending = pool._pending_restarts[0]
        pending.thread.join(0.5)
        pool._advance_pending_restarts(deadline=time.perf_counter() + 0.001)
        self.assertEqual(pool.restart_pending, 0)
        self.assertEqual(pool.retry_batches, (("shape-1603", 1),))
        self.assertEqual(len(pool._ready_batches), 1)
        self.assertIs(pool._ready_batches[0].task, task)
        self.assertEqual(pool._ready_batches[0].task.pair_ordinals, (1603,))
        pool.close()

    def test_context_replay_waits_for_a_later_bounded_advance(self):
        pool = pool_module.PersistentWorkerPool(1)
        slot = pool_module._Slot(index=0, worker=_FakeWorker())
        pool._slots = [slot]
        pool._context_replay_pending.add(0)

        with mock.patch.object(pool, "_start_context_on_slot", return_value=True) as submit:
            expired = time.perf_counter() - 0.001
            pool._advance_context_replays(deadline=expired)
            self.assertEqual(pool._context_replay_pending, {0})
            submit.assert_not_called()

            pool._advance_context_replays(deadline=time.perf_counter() + 0.050)
            self.assertEqual(pool._context_replay_pending, set())
            submit.assert_called_once_with(slot, deadline=mock.ANY)
        pool.close()

    def test_repeated_failure_cancels_and_clears_staged_state(self):
        replacement = _FakeWorker(ready=False)
        pool, slot, _old = self._make_pool_with_slot(replacement)
        task = SimpleNamespace(
            batch_id="shape-final",
            pair_ordinals=(1603,),
            operation_kind="shape",
        )
        pool._retry_counts[task.batch_id] = 1
        pool._active[slot.index] = pool_module._ActiveBatch(
            task,
            object(),
            time.perf_counter(),
        )
        slot.active = pool._active[slot.index]
        pool._handle_failure(
            slot,
            slot.active,
            pool_module.WorkerCrashedError("synthetic repeated death"),
        )
        self.assertTrue(pool.is_terminal)
        self.assertTrue(pool._failure)
        self.assertEqual(len(pool._ready_batches), 0)
        self.assertEqual(len(pool._undispatched), 0)
        self.assertEqual(pool._result_buffer, {})
        self.assertEqual(pool._pairs_done, 0)

    def test_shape_dispatch_uses_all_workers_for_synthetic_final_batch(self):
        pool = pool_module.PersistentWorkerPool(4)
        pool._slots = [
            pool_module._Slot(index=index, worker=_FakeWorker())
            for index in range(4)
        ]
        counts = [0, 0, 0, 0]
        for ordinal in range(1604):
            task = SimpleNamespace(
                batch_id=f"shape-{ordinal:08d}",
                operation_kind="shape",
            )
            selected = pool._choose_slot(pool_module._ReadyBatch(task))
            self.assertIsNotNone(selected)
            counts[selected.index] += 1
            pool._dispatch_counts[selected.index] = (
                pool._dispatch_counts.get(selected.index, 0) + 1
            )
        self.assertEqual(sum(counts), 1604)
        self.assertEqual(counts, [401, 401, 401, 401])
        self.assertLessEqual(max(counts), int(1604 * 0.60))

    def test_graceful_shutdown_is_resumable_and_bounded(self):
        pool = pool_module.PersistentWorkerPool(4)
        workers = [_FakeShutdownWorker(pending_rounds=index + 1) for index in range(4)]
        pool._slots = [
            pool_module._Slot(index=index, worker=worker)
            for index, worker in enumerate(workers)
        ]
        pool._terminal = True
        pool.begin_shutdown(grace_timeout=1.0)
        self.assertEqual([worker.begin_calls for worker in workers], [1, 1, 1, 1])
        elapsed_samples = []
        for _ in range(8):
            started = time.perf_counter()
            pool.advance_shutdown(deadline=time.perf_counter() + 0.002)
            elapsed_samples.append((time.perf_counter() - started) * 1000.0)
            if pool.shutdown_complete:
                break
        self.assertTrue(pool.shutdown_complete)
        self.assertEqual(pool.shutdown_state, "complete")
        self.assertEqual(pool.shutdown_force_used, False)
        self.assertEqual(pool.worker_pids, ())
        self.assertLessEqual(max(elapsed_samples), 250.0)
        self.assertGreaterEqual(pool.shutdown_rounds, 1)

    def test_grace_expiry_forces_only_owned_workers(self):
        pool = pool_module.PersistentWorkerPool(2)
        workers = [_FakeShutdownWorker(pending_rounds=100) for _ in range(2)]
        pool._slots = [
            pool_module._Slot(index=index, worker=worker)
            for index, worker in enumerate(workers)
        ]
        pool._terminal = True
        pool.begin_shutdown(grace_timeout=1.0)
        state = pool.advance_shutdown(
            deadline=time.perf_counter() + 0.002,
            grace_deadline=time.perf_counter() - 0.001,
        )
        self.assertEqual(state, "complete")
        self.assertTrue(pool.shutdown_complete)
        self.assertTrue(pool.shutdown_force_used)
        self.assertEqual([worker.force_calls for worker in workers], [1, 1])

    def test_modal_cancel_is_resumable_and_does_not_wait_for_ack(self):
        pool = pool_module.PersistentWorkerPool(4)
        workers = [_FakeShutdownWorker(pending_rounds=100) for _ in range(4)]
        pool._slots = [
            pool_module._Slot(index=index, worker=worker)
            for index, worker in enumerate(workers)
        ]

        started = time.perf_counter()
        self.assertEqual(pool.begin_cancel(), "force")
        begin_ms = (time.perf_counter() - started) * 1000.0
        self.assertLessEqual(begin_ms, 250.0)
        self.assertFalse(pool.cancel_complete)
        self.assertEqual(pool._active, {})

        elapsed = []
        for _ in range(4):
            tick = time.perf_counter()
            state = pool.advance_cancel(deadline=time.perf_counter() + 0.01)
            elapsed.append((time.perf_counter() - tick) * 1000.0)
            if state == "complete":
                break
        self.assertTrue(pool.cancel_complete)
        self.assertEqual(pool.cancel_state, "complete")
        self.assertEqual(pool.worker_pids, ())
        self.assertLessEqual(max(elapsed), 250.0)
        self.assertTrue(all(worker.force_calls >= 1 for worker in workers))


if __name__ == "__main__":
    unittest.main()
