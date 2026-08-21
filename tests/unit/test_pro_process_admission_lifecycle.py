"""Pure admission/stream lifecycle contracts for T2R4H."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import pickle
from types import SimpleNamespace
import threading
import time
import unittest

from uv_gpt import pro_process_pool as POOL
from uv_gpt import pro_process_runtime as RUNTIME
from uv_gpt.pro_group_first import GroupFirstFrontier, IslandRecord


@dataclass(frozen=True)
class _Estimate:
    frame_bytes: int = 64
    payload_bytes: int = 32


@dataclass(frozen=True)
class _Ticket:
    sequence: int


class _FakeStream:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class _FakeProcess:
    def __init__(self) -> None:
        self.pid = 9401
        self.returncode = None
        self.stdin = _FakeStream()
        self.stdout = _FakeStream()
        self.terminate_calls = 0
        self.kill_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.returncode = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class _AdmissionWorker:
    def __init__(self) -> None:
        self.is_ready = True
        self.is_alive = True
        self.process = None
        self.pid = None
        self.submit_started = threading.Event()
        self.submit_gate = threading.Event()
        self.close_requested = False
        self.active_writers = 0
        self.max_active_writers = 0
        self.submit_calls: list[str] = []
        self.close_calls = 0

    def submit(self, _wire, *, batch_id: str, item_count: int):
        del item_count
        self.submit_started.set()
        self.active_writers += 1
        self.max_active_writers = max(self.max_active_writers, self.active_writers)
        try:
            self.submit_gate.wait(2.0)
            if self.close_requested:
                raise RuntimeError("close requested")
            self.submit_calls.append(batch_id)
            return _Ticket(len(self.submit_calls))
        finally:
            self.active_writers -= 1

    def poll(self, timeout=0.0):
        del timeout
        return None

    def force_close_nonblocking(self):
        self.close_requested = True
        self.is_alive = False
        self.is_ready = False
        self.submit_gate.set()
        return "complete"

    def close(self, *, graceful=False):
        del graceful
        self.close_calls += 1
        self.force_close_nonblocking()


class _StageTask:
    operation_kind = "exact"
    item_count = 1
    pair_tasks = ()
    pair_ordinals = ()

    def __init__(self, identity: POOL.SnapshotIdentity, batch_id: str, stage: str) -> None:
        self.identity = identity
        self.batch_id = batch_id
        self.stage = stage
        self.gate = threading.Event()
        self.reached = threading.Event()

    def _stage(self, name: str) -> None:
        if self.stage != name:
            return
        self.reached.set()
        self.gate.wait(2.0)

    def validate(self) -> None:
        self._stage("validate")

    def estimate_frame(self):
        self._stage("estimate")
        return _Estimate()

    def to_wire(self):
        self._stage("to_wire")
        return {"batch_id": self.batch_id}


def _pool_with_worker():
    pool = POOL.PersistentWorkerPool(
        1,
        session_nonce="admission-lifecycle-session",
        generation=3,
        use_cache=False,
    )
    worker = _AdmissionWorker()
    pool._slots = [POOL._Slot(index=0, worker=worker)]
    pool.start = lambda: pool
    pool.begin_stream()
    return pool, worker


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.001)
    return bool(predicate())


class RuntimeStreamLifecycleTests(unittest.TestCase):
    def test_force_close_defers_until_blocked_writer_releases(self):
        runtime = RUNTIME.PersistentSingleWorker(session_nonce="runtime-close", generation=1)
        process = _FakeProcess()
        runtime._process = process
        runtime._identity = SimpleNamespace(process=process, pid=process.pid)
        runtime._ready_message = object()
        entered = threading.Event()
        release = threading.Event()
        original_write = RUNTIME.write_message

        def blocking_write(*_args, **_kwargs):
            entered.set()
            if not release.wait(2.0):
                raise RuntimeError("writer test timed out")

        RUNTIME.write_message = blocking_write
        errors: list[BaseException] = []

        def submit() -> None:
            try:
                runtime.submit({"items": (1,)}, batch_id="blocked", item_count=1)
            except BaseException as exc:  # noqa: BLE001 - assert transfer to owner
                errors.append(exc)

        thread = threading.Thread(target=submit, name="test-blocked-submit")
        try:
            thread.start()
            self.assertTrue(entered.wait(1.0))
            self.assertEqual(runtime.force_close_nonblocking(), "pending")
            self.assertEqual(process.terminate_calls, 0)
            self.assertEqual(process.stdin.close_calls, 0)
            release.set()
            thread.join(2.0)
            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
            self.assertIsInstance(errors[0], RUNTIME.WorkerCrashedError)
            self.assertEqual(process.terminate_calls, 1)
            self.assertEqual(process.stdin.close_calls, 1)
            self.assertEqual(process.stdout.close_calls, 1)
            self.assertIsNone(runtime.process)
            self.assertEqual(runtime.force_close_nonblocking(), "complete")
            self.assertEqual(process.terminate_calls, 1)
        finally:
            release.set()
            thread.join(2.0)
            RUNTIME.write_message = original_write
            runtime.close(graceful=False)

    def test_runtime_cleanup_lock_is_shared_by_nonblocking_and_blocking_paths(self):
        runtime = RUNTIME.PersistentSingleWorker(session_nonce="runtime-idempotent", generation=1)
        process = _FakeProcess()
        runtime._process = process
        runtime._identity = SimpleNamespace(process=process, pid=process.pid)
        runtime._ready_message = object()
        self.assertEqual(runtime.force_close_nonblocking(), "complete")
        runtime.close(graceful=False)
        runtime.close(graceful=False)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.stdin.close_calls, 1)
        self.assertEqual(process.stdout.close_calls, 1)
        self.assertIsNone(runtime.process)


class PoolAdmissionLifecycleTests(unittest.TestCase):
    def test_cancel_at_each_admission_stage_has_no_late_ticket_or_dispatch(self):
        for stage in ("validate", "estimate", "to_wire", "submit"):
            pool, worker = _pool_with_worker()
            task = _StageTask(
                POOL.SnapshotIdentity(pool.session_nonce, pool.generation, "stage"),
                "stage-%s" % stage,
                stage,
            )
            try:
                pool.stream_submit(task)
                if stage == "submit":
                    self.assertTrue(worker.submit_started.wait(1.0), stage)
                else:
                    self.assertTrue(task.reached.wait(1.0), stage)
                pool.begin_cancel()
                task.gate.set()
                deadline = time.perf_counter() + 1.0
                while not pool.cancel_complete and time.perf_counter() < deadline:
                    pool.advance_cancel(deadline=time.perf_counter() + 0.05)
                self.assertTrue(pool.cancel_complete, stage)
                self.assertEqual(worker.submit_calls, [], stage)
                self.assertEqual(pool.active_workers, 0, stage)
                self.assertEqual(pool.admission_pending, 0, stage)
                self.assertEqual(pool._slots, [], stage)
            finally:
                task.gate.set()
                pool.close()

    def test_close_detaches_a_writer_owned_slot_instead_of_direct_close(self):
        pool, worker = _pool_with_worker()
        task = _StageTask(
            POOL.SnapshotIdentity(pool.session_nonce, pool.generation, "detach"),
            "detach-1",
            "submit",
        )
        # Make the worker remain pending after force-close so the pool's
        # bounded compatibility close must use its detached cleanup thread.
        original_force = worker.force_close_nonblocking

        def pending_force_close():
            worker.close_requested = True
            worker.is_alive = True
            worker.is_ready = True
            return "pending" if worker.active_writers else original_force()

        worker.force_close_nonblocking = pending_force_close
        try:
            pool.stream_submit(task)
            self.assertTrue(worker.submit_started.wait(1.0))
            pool.close()
            self.assertEqual(worker.close_calls, 0)
            self.assertEqual(worker.max_active_writers, 1)
            task.gate.set()
            worker.submit_gate.set()
            self.assertTrue(_wait_until(lambda: worker.active_writers == 0))
            self.assertTrue(
                _wait_until(
                    lambda: not pool._pending_cleanups
                    or all(item.state.get("done") for item in pool._pending_cleanups.values())
                )
            )
        finally:
            task.gate.set()
            worker.submit_gate.set()
            pool.close()


@dataclass(frozen=True)
class _ImmutableAdmissionPayload:
    groups: tuple[object, ...]

    def to_wire(self) -> bytes:
        return pickle.dumps(self.groups, protocol=5)


class FrontierAdmissionStressTests(unittest.TestCase):
    def test_frontier_groups_and_immutable_admission_encoding_are_stable_concurrently(self):
        frontier = GroupFirstFrontier(
            tuple(
                IslandRecord(
                    key=("island", index),
                    density=1.0 + index,
                    bucket_key=("bucket",),
                    ordinal=index,
                    uv_area=2.0 + index,
                )
                for index in range(8)
            ),
            similarity_tolerance=0.0,
        )
        payload = _ImmutableAdmissionPayload(tuple(frontier.groups))
        errors: list[BaseException] = []
        group_digests: list[str] = []
        wire_digests: list[str] = []

        def read_groups() -> None:
            try:
                for _ in range(200):
                    snapshot = frontier.groups
                    group_digests.append(
                        hashlib.sha256(pickle.dumps(snapshot, protocol=5)).hexdigest()
                    )
            except BaseException as exc:  # noqa: BLE001 - assert no race exception
                errors.append(exc)

        def encode_payload() -> None:
            try:
                for _ in range(200):
                    wire_digests.append(hashlib.sha256(payload.to_wire()).hexdigest())
            except BaseException as exc:  # noqa: BLE001 - assert no race exception
                errors.append(exc)

        readers = [
            threading.Thread(target=read_groups, name="test-frontier-groups"),
            threading.Thread(target=encode_payload, name="test-admission-encode"),
        ]
        for thread in readers:
            thread.start()
        for thread in readers:
            thread.join(2.0)
        self.assertFalse(errors)
        self.assertTrue(group_digests)
        self.assertTrue(wire_digests)
        self.assertEqual(len(set(group_digests)), 1)
        self.assertEqual(len(set(wire_digests)), 1)
        self.assertEqual(frontier.groups, payload.groups)


if __name__ == "__main__":
    unittest.main()
