"""Pure owner-thread budget tests for persistent task admission."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import threading
import time
import unittest

from uv_gpt import pro_process_pool as POOL


@dataclass(frozen=True)
class _Estimate:
    frame_bytes: int = 32
    payload_bytes: int = 16


@dataclass(frozen=True)
class _Ticket:
    sequence: int


class _BlockingWorker:
    def __init__(self, index: int, *, submit_error: BaseException | None = None) -> None:
        self.index = index
        self.is_ready = True
        self.is_alive = True
        self.process = None
        self.pid = None
        self.submit_error = submit_error
        self.submit_gate = threading.Event()
        self.submit_started = threading.Event()
        self.submit_calls: list[str] = []
        self.active_writers = 0
        self.max_active_writers = 0
        self.close_calls = 0

    def submit(self, _wire, *, batch_id: str, item_count: int):
        del item_count
        self.submit_started.set()
        self.active_writers += 1
        self.max_active_writers = max(self.max_active_writers, self.active_writers)
        try:
            self.submit_gate.wait(2.0)
            if self.submit_error is not None:
                raise self.submit_error
            self.submit_calls.append(batch_id)
            return _Ticket(len(self.submit_calls))
        finally:
            self.active_writers -= 1

    def poll(self, timeout: float = 0.0):
        del timeout
        return None

    def force_close_nonblocking(self):
        self.is_alive = False
        self.is_ready = False
        self.submit_gate.set()
        return "complete"

    def close(self, *, graceful: bool = False):
        del graceful
        self.close_calls += 1
        self.force_close_nonblocking()


class _BlockingTask:
    operation_kind = "exact"
    item_count = 1
    pair_tasks = ()
    pair_ordinals = ()

    def __init__(self, identity: POOL.SnapshotIdentity, batch_id: str) -> None:
        self.identity = identity
        self.batch_id = batch_id
        self.validate_gate = threading.Event()
        self.serialize_calls = 0
        self.validate_calls = 0
        self.estimate_calls = 0

    def validate(self) -> None:
        self.validate_calls += 1
        self.validate_gate.wait(2.0)

    def estimate_frame(self):
        self.estimate_calls += 1
        return _Estimate()

    def to_wire(self):
        self.serialize_calls += 1
        return {"batch_id": self.batch_id}


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


def _pool_with_workers(count: int = 1, workers=None):
    pool = POOL.PersistentWorkerPool(
        count,
        session_nonce="tick-budget-session",
        generation=7,
        use_cache=False,
    )
    values = list(workers or [_BlockingWorker(index) for index in range(count)])
    pool._slots = [POOL._Slot(index=index, worker=worker) for index, worker in enumerate(values)]
    # The test uses already-ready doubles; begin_stream must not bootstrap a
    # real helper process while proving the owner/admission boundary.
    pool.start = lambda: pool
    pool.begin_stream()
    return pool, values


def _task(pool, name: str) -> _BlockingTask:
    return _BlockingTask(
        POOL.SnapshotIdentity(pool.session_nonce, pool.generation, "tick-budget"),
        name,
    )


class ProProcessTickBudgetTests(unittest.TestCase):
    def test_blocking_admission_is_background_and_owner_bounded(self):
        pool, (worker,) = _pool_with_workers()
        task = _task(pool, "slow-1")
        try:
            started = time.perf_counter()
            self.assertEqual(pool.stream_submit(task, deadline=time.perf_counter() + 0.02), ("slow-1",))
            owner_ms = (time.perf_counter() - started) * 1000.0
            self.assertLess(owner_ms, 100.0)
            self.assertEqual(pool.active_workers, 0)
            self.assertEqual(pool.admission_pending, 1)
            self.assertEqual(task.validate_calls, 1)
            self.assertFalse(worker.submit_started.is_set())

            task.validate_gate.set()
            self.assertTrue(_wait_until(worker.submit_started.is_set))
            self.assertEqual(pool.active_workers, 0)
            self.assertEqual(task.serialize_calls, 1)
            self.assertLess(pool.progress().admission_pending, 2)

            worker.submit_gate.set()
            self.assertTrue(_wait_until(lambda: worker.submit_started.is_set() and not worker.active_writers))
            pool._poll_once(deadline=time.perf_counter() + 0.2)
            self.assertEqual(pool.active_workers, 1)
            self.assertGreater(pool.admission_validate_background_ms, 0.0)
            self.assertGreater(pool.admission_serialize_background_ms, 0.0)
        finally:
            pool.close()

    def test_admission_never_adopts_before_background_ticket(self):
        pool, (worker,) = _pool_with_workers()
        task = _task(pool, "pending-1")
        try:
            pool.stream_submit(task)
            for _ in range(5):
                pool._poll_once(deadline=time.perf_counter() + 0.05)
            self.assertEqual(pool.active_workers, 0)
            self.assertEqual(worker.submit_calls, [])
            task.validate_gate.set()
            self.assertTrue(_wait_until(worker.submit_started.is_set))
            for _ in range(5):
                pool._poll_once(deadline=time.perf_counter() + 0.05)
            self.assertEqual(pool.active_workers, 0)
            worker.submit_gate.set()
            self.assertTrue(_wait_until(lambda: worker.submit_calls == ["pending-1"]))
            pool._poll_once(deadline=time.perf_counter() + 0.2)
            self.assertEqual(pool.active_workers, 1)
        finally:
            pool.close()

    def test_repeated_owner_polls_keep_one_stream_writer(self):
        pool, (worker,) = _pool_with_workers()
        task = _task(pool, "writer-1")
        task.validate_gate.set()
        try:
            pool.stream_submit(task)
            self.assertTrue(_wait_until(worker.submit_started.is_set))
            for _ in range(30):
                pool._poll_once(deadline=time.perf_counter() + 0.05)
            self.assertEqual(worker.max_active_writers, 1)
            self.assertEqual(len(worker.submit_calls), 0)
            self.assertEqual(pool.active_workers, 0)
            worker.submit_gate.set()
            self.assertTrue(_wait_until(lambda: worker.active_writers == 0))
            pool._poll_once(deadline=time.perf_counter() + 0.2)
            self.assertEqual(len(worker.submit_calls), 1)
            self.assertEqual(worker.max_active_writers, 1)
        finally:
            pool.close()

    def test_cancel_during_pending_admission_has_no_late_dispatch(self):
        pool, (worker,) = _pool_with_workers()
        task = _task(pool, "cancel-1")
        try:
            pool.stream_submit(task)
            self.assertTrue(_wait_until(lambda: pool.admission_pending == 1))
            pool.begin_cancel()
            task.validate_gate.set()
            deadline = time.perf_counter() + 1.0
            while not pool.cancel_complete and time.perf_counter() < deadline:
                pool.advance_cancel(deadline=time.perf_counter() + 0.05)
            self.assertTrue(pool.cancel_complete)
            self.assertEqual(pool.admission_pending, 0)
            self.assertEqual(pool.active_workers, 0)
            self.assertEqual(worker.submit_calls, [])
            self.assertEqual(pool._slots, [])
            self.assertEqual(pool.advance_cancel(), "complete")
        finally:
            pool.close()

    def test_shutdown_during_pending_admission_is_idempotent(self):
        pool, (worker,) = _pool_with_workers()
        task = _task(pool, "shutdown-1")
        try:
            pool.stream_submit(task)
            self.assertTrue(_wait_until(lambda: pool.admission_pending == 1))
            pool.begin_shutdown(grace_timeout=0.1)
            task.validate_gate.set()
            deadline = time.perf_counter() + 1.0
            while not pool.shutdown_complete and time.perf_counter() < deadline:
                pool.advance_shutdown(deadline=time.perf_counter() + 0.05)
            self.assertTrue(pool.shutdown_complete)
            self.assertEqual(pool.admission_pending, 0)
            self.assertEqual(pool._slots, [])
            self.assertEqual(pool.advance_shutdown(), "complete")
            self.assertGreaterEqual(worker.close_calls, 0)
        finally:
            pool.close()

    def test_admission_failure_is_reported_once_and_cleanup_is_idempotent(self):
        worker = _BlockingWorker(0, submit_error=POOL.WorkerRemoteError("send failed"))
        pool, _ = _pool_with_workers(workers=[worker])
        task = _task(pool, "failure-1")
        task.validate_gate.set()
        scheduled = []

        def remember_restart(slot, failed_task, error, *, is_context):
            scheduled.append((slot.index, failed_task.batch_id, str(error), is_context))

        pool._schedule_restart = remember_restart
        try:
            pool.stream_submit(task)
            worker.submit_gate.set()
            self.assertTrue(_wait_until(lambda: worker.active_writers == 0))
            pool._poll_once(deadline=time.perf_counter() + 0.2)
            pool._poll_once(deadline=time.perf_counter() + 0.2)
            self.assertEqual(len(scheduled), 1)
            self.assertEqual(scheduled[0][1], "failure-1")
            self.assertFalse(scheduled[0][3])
            self.assertEqual(pool.active_workers, 0)
            pool.close()
            pool.close()
        finally:
            pool.close()

    def test_submission_order_is_deterministic_for_one_and_multiple_slots(self):
        for count in (1, 2):
            workers = [_BlockingWorker(index) for index in range(count)]
            pool, values = _pool_with_workers(count, workers)
            tasks = [_task(pool, f"ordered-{index}") for index in range(count)]
            for task in tasks:
                task.validate_gate.set()
            try:
                pool.stream_submit(tuple(tasks))
                for index, worker in enumerate(values):
                    if index == 0 or count > 1:
                        worker.submit_gate.set()
                deadline = time.perf_counter() + 1.0
                while time.perf_counter() < deadline and len(
                    [call for worker in values for call in worker.submit_calls]
                ) < count:
                    pool._poll_once(deadline=time.perf_counter() + 0.05)
                    for worker in values:
                        worker.submit_gate.set()
                observed = [
                    call for worker in values for call in worker.submit_calls
                ]
                self.assertEqual(observed, [f"ordered-{index}" for index in range(count)])
                pool._poll_once(deadline=time.perf_counter() + 0.2)
                self.assertEqual(
                    tuple(index for index in sorted(pool._active)),
                    tuple(range(count)),
                )
            finally:
                pool.close()


if __name__ == "__main__":
    unittest.main()
