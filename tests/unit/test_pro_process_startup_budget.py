"""Pure budget tests for the group-first pool startup state machine.

These tests use only owned doubles.  They never launch Blender, a bundled
helper, a process pool, or a worker subprocess.  The fake clock deliberately
advances by the historical 338 ms startup spike inside the background worker
to prove that the modal owner observes only scheduling/adoption work.
"""

from __future__ import annotations

import ast
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from uv_gpt import pro_process_pool as pool_module


ROOT = Path(__file__).resolve().parents[2]


class _FakeClock:
    def __init__(self):
        self.ms = 0.0

    def advance(self, milliseconds):
        self.ms += float(milliseconds)


class _BootstrapWorker:
    def __init__(self, clock, *, gate=None):
        self.clock = clock
        self.gate = gate
        self.start_thread = None
        self.is_ready = False
        self.closed = False
        self.process = object()
        self.force_close_calls = 0

    @property
    def is_alive(self):
        return not self.closed

    @property
    def pid(self):
        return None

    @property
    def owned_process(self):
        return None

    def start(self):
        self.start_thread = threading.current_thread()
        if self.gate is not None:
            self.gate.wait(1.0)
        self.clock.advance(338.3764)
        if self.closed:
            return
        self.is_ready = True

    def force_close_nonblocking(self):
        self.force_close_calls += 1
        self.closed = True
        self.is_ready = False
        self.process = None
        return "complete"

    def close(self, **_kwargs):
        self.closed = True
        self.is_ready = False
        self.process = None


class _ContextWorker(_BootstrapWorker):
    def __init__(self, clock, *, submit_gate=None):
        super().__init__(clock)
        self.is_ready = True
        self.submit_gate = submit_gate
        self.submit_thread = None
        self.submit_calls = 0

    def submit(self, _wire, **_kwargs):
        if self.submit_gate is not None:
            self.submit_gate.wait(1.0)
        if self.closed:
            raise RuntimeError("owned worker was cancelled before context write")
        self.submit_thread = threading.current_thread()
        self.submit_calls += 1
        self.clock.advance(338.3764)
        return SimpleNamespace(sequence=self.submit_calls)


class _ContextTask:
    def __init__(self, *, identity, batch_id, context):
        self.identity = identity
        self.batch_id = batch_id
        self.context = context
        self.operation_kind = "graph_context"

    def estimate_frame(self):
        return SimpleNamespace(frame_bytes=64, payload_bytes=32)

    def to_wire(self):
        return {"operation": "snapshot_graph_context_load"}


class StartupBudgetTests(unittest.TestCase):
    def _make_pool(self, worker_count=4):
        return pool_module.PersistentWorkerPool(worker_count, use_cache=False)

    def test_owner_never_runs_bootstrap_and_adopts_deterministic_slots(self):
        clock = _FakeClock()
        created = []
        pool = self._make_pool(4)

        def make_worker():
            worker = _BootstrapWorker(clock)
            created.append(worker)
            return worker

        pool._make_worker = make_worker
        owner = threading.current_thread()
        owner_samples = []
        for index in range(4):
            started = time.perf_counter()
            ready = pool.start_incremental(deadline=time.perf_counter() + 0.050)
            owner_samples.append((time.perf_counter() - started) * 1000.0)
            self.assertFalse(ready)
            pending = pool._pending_startups[index]
            pending.thread.join(0.5)
            started = time.perf_counter()
            ready = pool.start_incremental(deadline=time.perf_counter() + 0.050)
            owner_samples.append((time.perf_counter() - started) * 1000.0)
            if index < 3:
                self.assertFalse(ready)
            else:
                self.assertTrue(ready)

        self.assertEqual([slot.index for slot in pool._slots], [0, 1, 2, 3])
        self.assertEqual(pool.worker_count, 4)
        self.assertTrue(all(worker.start_thread is not owner for worker in created))
        self.assertGreaterEqual(clock.ms, 4.0 * 338.3764)
        self.assertLess(max(owner_samples), 50.0)
        self.assertEqual(pool.startup_pending, 0)
        self.assertGreater(pool.worker_start_background_ms, 0.0)
        self.assertLess(pool.worker_start_owner_ms, 50.0)
        pool.close()

    def test_expired_deadline_admits_no_bootstrap(self):
        pool = self._make_pool(4)
        created = []
        pool._make_worker = lambda: created.append(_BootstrapWorker(_FakeClock())) or created[-1]
        self.assertFalse(pool.start_incremental(deadline=time.perf_counter() - 0.001))
        self.assertEqual(created, [])
        self.assertEqual(pool.startup_pending, 0)
        pool.close()

    def test_synchronous_start_compatibility_drives_incremental_state_machine(self):
        clock = _FakeClock()
        pool = self._make_pool(4)
        pool._make_worker = lambda: _BootstrapWorker(clock)
        pool.start()
        self.assertEqual([slot.index for slot in pool._slots], [0, 1, 2, 3])
        self.assertTrue(all(slot.worker.is_ready for slot in pool._slots))
        self.assertGreaterEqual(clock.ms, 4.0 * 338.3764)
        pool.close()

    def test_context_estimate_and_write_are_background_and_ack_is_dispatch_barrier(self):
        clock = _FakeClock()
        worker = _ContextWorker(clock)
        pool = self._make_pool(1)
        pool._slots = [pool_module._Slot(index=0, worker=worker)]
        identity = SimpleNamespace(session_nonce="budget", generation=0)
        pool._graph_context_payload = SimpleNamespace(
            identity=identity,
            context_digest="context-budget",
        )
        slot = pool._slots[0]
        with mock.patch.object(pool_module, "GraphContextLoadTask", _ContextTask):
            started = time.perf_counter()
            self.assertTrue(
                pool._start_context_on_slot(
                    slot,
                    deadline=time.perf_counter() + 0.050,
                )
            )
            owner_ms = (time.perf_counter() - started) * 1000.0
            self.assertLess(owner_ms, 50.0)
            self.assertEqual(pool.context_load_pending, 1)
            self.assertIsNone(
                pool._choose_slot(pool_module._ReadyBatch(SimpleNamespace()))
            )
            pending = pool._pending_context_sends[0]
            pending.thread.join(0.5)
            pool._advance_pending_context_sends(deadline=time.perf_counter() + 0.050)

        self.assertIsNot(worker.submit_thread, threading.current_thread())
        self.assertEqual(pool.context_load_pending, 0)
        self.assertEqual(pool.context_load_submitted, 1)
        self.assertFalse(pool.graph_context_ready)
        self.assertIsNone(
            pool._choose_slot(pool_module._ReadyBatch(SimpleNamespace()))
        )

        # Only a validated ACK releases the slot for dispatch.
        pool._context_active.clear()
        pool._context_loaded.add(0)
        self.assertIs(pool._choose_slot(pool_module._ReadyBatch(SimpleNamespace())), slot)
        self.assertGreater(pool.context_serialize_background_ms, 0.0)
        self.assertGreater(pool.context_write_background_ms, 0.0)
        pool.close()

    def test_cancel_pending_bootstrap_and_context_is_idempotent_and_orphan_free(self):
        clock = _FakeClock()
        gate = threading.Event()
        worker = _BootstrapWorker(clock, gate=gate)
        pool = self._make_pool(1)
        pool._make_worker = lambda: worker
        self.assertFalse(pool.start_incremental(deadline=time.perf_counter() + 0.050))
        self.assertEqual(pool.begin_cancel(), "force")
        gate.set()
        pending = next(iter(pool._pending_startups.values()))
        pending.thread.join(0.5)
        self.assertEqual(pool.advance_cancel(deadline=time.perf_counter() + 0.050), "complete")
        self.assertEqual(pool.advance_cancel(deadline=time.perf_counter() + 0.050), "complete")
        self.assertEqual(pool._pending_startups, {})
        self.assertEqual(pool._slots, [])
        self.assertTrue(worker.force_close_calls >= 1)

    def test_cancel_pending_context_send_has_no_late_dispatch(self):
        clock = _FakeClock()
        submit_gate = threading.Event()
        worker = _ContextWorker(clock, submit_gate=submit_gate)
        pool = self._make_pool(1)
        pool._slots = [pool_module._Slot(index=0, worker=worker)]
        identity = SimpleNamespace(session_nonce="cancel-context", generation=0)
        pool._graph_context_payload = SimpleNamespace(
            identity=identity,
            context_digest="cancel-context-digest",
        )
        with mock.patch.object(pool_module, "GraphContextLoadTask", _ContextTask):
            self.assertTrue(
                pool._start_context_on_slot(
                    pool._slots[0],
                    deadline=time.perf_counter() + 0.050,
                )
            )
            self.assertEqual(pool.begin_cancel(), "force")
            pending = next(iter(pool._pending_context_sends.values()))
            submit_gate.set()
            pending.thread.join(0.5)
        self.assertEqual(pool.advance_cancel(deadline=time.perf_counter() + 0.050), "complete")
        self.assertEqual(pool._pending_context_sends, {})
        self.assertEqual(worker.submit_calls, 0)
        self.assertEqual(pool._slots, [])
        self.assertEqual(pool.advance_cancel(deadline=time.perf_counter() + 0.050), "complete")

    def test_startup_pipeline_deadline_is_explicit_and_context_barrier_is_pending_aware(self):
        source = (ROOT / "uv_gpt" / "stack_tools.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        startup = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_advance_process_startup"
        )
        calls = [
            node
            for node in ast.walk(startup)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "start"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "_process_pipeline"
        ]
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [keyword.arg for keyword in calls[0].keywords],
            ["deadline"],
        )
        self.assertEqual(
            ast.unparse(calls[0].keywords[0].value),
            "deadline",
        )


if __name__ == "__main__":
    unittest.main()
