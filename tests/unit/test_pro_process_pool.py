"""MC2 persistent pool tests using the pure topology correspondence workload."""

from __future__ import annotations

from dataclasses import replace
import ctypes
import importlib.util
import math
import os
from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
BLENDER_PATH = (
    ROOT
    / ".test_runtime"
    / "blender-5.0.0"
    / "blender-5.0.0-windows-x64"
    / "blender.exe"
)
PYTHON_PATH = (
    ROOT
    / ".test_runtime"
    / "blender-5.0.0"
    / "blender-5.0.0-windows-x64"
    / "5.0"
    / "python"
    / "bin"
    / "python.exe"
)
WORKER_PATH = ROOT / "uv_gpt" / "pro_process_worker.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROTOCOL = _load("pro_process_protocol", ROOT / "uv_gpt" / "pro_process_protocol.py")
TOPOLOGY = _load("topology_correspondence", ROOT / "uv_gpt" / "topology_correspondence.py")
PAYLOAD = _load("pro_process_payload", ROOT / "uv_gpt" / "pro_process_payload.py")
RUNTIME = _load("pro_process_runtime", ROOT / "uv_gpt" / "pro_process_runtime.py")
POOL = _load("pro_process_pool", ROOT / "uv_gpt" / "pro_process_pool.py")


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
        edges.append(TOPOLOGY.EdgeRecord(index, ((face_key, index),), (face_key,), boundary=True))
        vertices.append(TOPOLOGY.VertexRecord(vertex_index, ((face_key, index),), boundary=True))
    return TOPOLOGY.make_graph(
        faces=(TOPOLOGY.FaceRecord(face_key, face_loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(TOPOLOGY.BoundaryComponentRecord("outer", face_loop_keys, "outer"),),
    )


def _make_batches(session_nonce: str, generation: int, *, count=8, batch_size=1, delay_ms=0):
    identity = PAYLOAD.SnapshotIdentity(session_nonce, generation, "mc2-topology-snapshot")
    points = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    master = _polygon_graph(points)
    master_data = PAYLOAD.GraphData.from_topology(master, "master")
    graph_map = {master_data.graph_key: master_data}
    pairs = []
    for ordinal in range(count):
        member = _polygon_graph(points, order=(0, 1, 2, 3))
        member_data = PAYLOAD.GraphData.from_topology(member, f"member-{ordinal}")
        graph_map[member_data.graph_key] = member_data
        pairs.append(
            PAYLOAD.PairTask(
                pair_ordinal=ordinal,
                master_key="master",
                member_key=f"member-{ordinal}",
                master_graph=PAYLOAD.GraphRef(master_data.graph_key, master_data.content_digest),
                member_graph=PAYLOAD.GraphRef(member_data.graph_key, member_data.content_digest),
                options=PAYLOAD.ExactOptions(tolerance=1.0e-8),
            )
        )
    batches = PAYLOAD.partition_batches(identity, pairs, tuple(graph_map.values()), batch_size=batch_size)
    if delay_ms:
        batches = tuple(replace(batch, debug_delay_ms=delay_ms) for batch in batches)
    return batches


def _make_special_batch(session_nonce: str, generation: int):
    identity = PAYLOAD.SnapshotIdentity(session_nonce, generation, "mc2-special-snapshot")
    points = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
    master_data = PAYLOAD.GraphData.from_topology(_polygon_graph(points), "special-master")
    same_data = PAYLOAD.GraphData.from_topology(_polygon_graph(points), "special-same")
    reverse_data = PAYLOAD.GraphData.from_topology(
        _polygon_graph(points, order=(0, 3, 2, 1)), "special-reverse"
    )
    triangle_data = PAYLOAD.GraphData.from_topology(
        _polygon_graph(((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))), "special-triangle"
    )
    graphs = (master_data, same_data, reverse_data, triangle_data)
    pairs = (
        PAYLOAD.PairTask(
            0, "master", "same",
            PAYLOAD.GraphRef(master_data.graph_key, master_data.content_digest),
            PAYLOAD.GraphRef(same_data.graph_key, same_data.content_digest),
            PAYLOAD.ExactOptions(tolerance=1.0e-8),
        ),
        PAYLOAD.PairTask(
            1, "master", "reverse",
            PAYLOAD.GraphRef(master_data.graph_key, master_data.content_digest),
            PAYLOAD.GraphRef(reverse_data.graph_key, reverse_data.content_digest),
            PAYLOAD.ExactOptions(allow_flipping=True, tolerance=1.0e-8),
        ),
        PAYLOAD.PairTask(
            2, "master", "triangle",
            PAYLOAD.GraphRef(master_data.graph_key, master_data.content_digest),
            PAYLOAD.GraphRef(triangle_data.graph_key, triangle_data.content_digest),
            PAYLOAD.ExactOptions(tolerance=1.0e-8),
        ),
    )
    batch = PAYLOAD.BatchTask(identity, "special-batch", pairs, graphs)
    batch.validate()
    return (batch,)


def _pid_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(0x00100000 | 0x00001000, 0, pid)
        if not handle:
            return False
        exit_code = ctypes.c_uint32()
        try:
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _wait_pids_gone(pids, timeout=4.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_pid_alive(pid) for pid in pids):
            return True
        time.sleep(0.02)
    return not any(_pid_alive(pid) for pid in pids)


def _new_pool(count: int, session: str, *, generation=4, use_cache=False, io_timeout=5.0):
    return POOL.PersistentWorkerPool(
        count,
        worker_script=WORKER_PATH,
        blender_binary=BLENDER_PATH,
        blender_version="5.0",
        session_nonce=session,
        generation=generation,
        handshake_timeout=8.0,
        io_timeout=io_timeout,
        use_cache=use_cache,
    )


class ProProcessPoolTests(unittest.TestCase):
    def test_actual_topology_digest_is_equal_for_one_two_four_workers(self):
        self.assertTrue(BLENDER_PATH.is_file())
        self.assertTrue(PYTHON_PATH.is_file())
        digests = []
        for count in (1, 2, 4):
            pool = _new_pool(count, f"mc2-proof-{count}")
            pids = ()
            try:
                started = pool.start()
                pids = pool.worker_pids
                self.assertEqual(len(pids), count)
                self.assertEqual({Path(worker.command[0]).resolve() for worker in pool.workers}, {PYTHON_PATH.resolve()})
                for worker in pool.workers:
                    self.assertEqual(worker.ready_payload["thread_caps"], {
                        name: "1" for name in RUNTIME.THREAD_CAP_NAMES
                    })
                batches = _make_batches(pool.session_nonce, pool.generation, count=8, batch_size=1)
                result = pool.run(batches, timeout=30.0)
                self.assertTrue(result.complete, result.failure)
                self.assertEqual(len(result.results), 8)
                self.assertEqual(tuple(item.pair_ordinal for item in result.results), tuple(range(8)))
                self.assertTrue(all(item.accepted for item in result.results))
                self.assertTrue(all(math.isfinite(item.score) for item in result.results))
                self.assertEqual(result.progress.pairs_done, 8)
                self.assertEqual(result.progress.batches_done, 8)
                self.assertEqual(result.progress.retry_count, 0)
                self.assertEqual(len(set(result.worker_pids)), count)
                self.assertEqual(len(result.worker_distribution), min(count, 8))
                self.assertTrue(all(value > 0 for _index, value in result.worker_distribution))
                frame_sizes = tuple(PAYLOAD.estimate_batch_frame(batch).frame_bytes for batch in batches)
                self.assertTrue(all(size < PAYLOAD.MAX_FRAME_BYTES for size in frame_sizes))
                self.assertGreater(max(frame_sizes), 0)
                digests.append(result.result_digest)
                self.assertIsNotNone(started)
            finally:
                pool.close()
                self.assertTrue(_wait_pids_gone(pids), f"orphan worker PIDs: {pids}")
        self.assertEqual(len(set(digests)), 1)

    def test_six_and_eight_worker_start_shutdown_smoke(self):
        for count in (6, 8):
            pool = _new_pool(count, f"mc2-smoke-{count}")
            pids = ()
            try:
                pool.start()
                pids = pool.worker_pids
                self.assertEqual(len(pids), count)
                result = pool.run(_make_batches(pool.session_nonce, pool.generation, count=1), timeout=30.0)
                self.assertTrue(result.complete, result.failure)
            finally:
                pool.shutdown()
                self.assertTrue(_wait_pids_gone(pids), f"orphan worker PIDs: {pids}")

    def test_crash_retries_once_and_succeeds(self):
        pool = _new_pool(2, "mc2-crash-success")
        pids = ()
        try:
            pool.start()
            pids = pool.worker_pids
            pool.begin(_make_batches(pool.session_nonce, pool.generation, count=4, batch_size=1, delay_ms=700))
            killed_pid = pool.worker_processes[0].pid
            pool.worker_processes[0].process.terminate()
            result = pool.drain(timeout=30.0)
            self.assertTrue(result.complete, result.failure)
            self.assertEqual(result.progress.retry_count, 1)
            self.assertEqual(result.progress.retry_total, 1)
            self.assertEqual(result.progress.max_retry_per_batch, 1)
            self.assertEqual(result.progress.retried_batch_count, 1)
            self.assertEqual(sum(count for _batch, count in result.progress.retry_batches), 1)
            self.assertEqual(len(result.results), 4)
            self.assertNotEqual(pool.worker_processes[0].pid, killed_pid)
        finally:
            pool.close()
            self.assertTrue(_wait_pids_gone(pids), f"orphan worker PIDs: {pids}")

    def test_worker_exact_operation_covers_accepted_reversed_and_rejected(self):
        pool = _new_pool(1, "mc2-special")
        pids = ()
        try:
            pool.start()
            pids = pool.worker_pids
            result = pool.run(_make_special_batch(pool.session_nonce, pool.generation), timeout=30.0)
            self.assertTrue(result.complete, result.failure)
            self.assertEqual(len(result.results), 3)
            accepted, reversed_result, rejected = result.results
            self.assertTrue(accepted.accepted)
            self.assertTrue(reversed_result.accepted)
            self.assertTrue(reversed_result.reflected)
            self.assertTrue(reversed_result.reversed)
            self.assertFalse(rejected.accepted)
            self.assertEqual(rejected.loop_mapping, ())
        finally:
            pool.close()
            self.assertTrue(_wait_pids_gone(pids), f"orphan worker PIDs: {pids}")

    def test_complete_cache_reuses_only_validated_batches(self):
        pool = _new_pool(1, "mc2-cache", use_cache=True)
        pids = ()
        try:
            pool.start()
            pids = pool.worker_pids
            batches = _make_batches(pool.session_nonce, pool.generation, count=2, batch_size=1)
            first = pool.run(batches, timeout=30.0)
            self.assertTrue(first.complete, first.failure)
            second = pool.run(batches, timeout=30.0)
            self.assertTrue(second.complete, second.failure)
            self.assertEqual(second.result_digest, first.result_digest)
            self.assertEqual(second.results, first.results)
            self.assertEqual(second.worker_distribution, ())
            self.assertEqual(pool.worker_pids, pids)
        finally:
            pool.close()
            self.assertTrue(_wait_pids_gone(pids), f"orphan worker PIDs: {pids}")

    def test_repeated_crash_has_no_final_result(self):
        pool = _new_pool(1, "mc2-crash-failure")
        pids = ()
        try:
            pool.start()
            pids = pool.worker_pids
            pool.begin(_make_batches(pool.session_nonce, pool.generation, count=1, delay_ms=800))
            first_pid = pool.worker_processes[0].pid
            pool.worker_processes[0].process.terminate()
            deadline = time.monotonic() + 10.0
            while pool.retry_count < 1 and time.monotonic() < deadline:
                pool.poll(0.1)
            self.assertEqual(pool.retry_count, 1)
            replacement_pid = pool.worker_processes[0].pid
            self.assertNotEqual(first_pid, replacement_pid)
            pool.worker_processes[0].process.terminate()
            result = pool.drain(timeout=30.0)
            self.assertFalse(result.complete)
            self.assertEqual(result.results, ())
            self.assertTrue(result.failure)
            self.assertEqual(result.progress.retry_count, 1)
            self.assertEqual(result.progress.retry_total, 1)
            self.assertEqual(result.progress.max_retry_per_batch, 1)
            self.assertEqual(result.progress.retried_batch_count, 1)
            self.assertIn("retry_attempt=2", result.progress.retry_failure_reason)
            self.assertEqual(pool.worker_pids, ())
        finally:
            pool.close()
            self.assertTrue(_wait_pids_gone(pids), f"orphan worker PIDs: {pids}")

    def test_cancel_and_generation_invalidation_expose_no_result(self):
        pool = _new_pool(2, "mc2-cancel")
        pids = ()
        try:
            pool.start()
            pids = pool.worker_pids
            pool.begin(_make_batches(pool.session_nonce, pool.generation, count=2, delay_ms=1200))
            cancelled = pool.cancel(timeout=0.4)
            self.assertFalse(cancelled.complete)
            self.assertTrue(cancelled.cancelled)
            self.assertEqual(cancelled.results, ())
            self.assertEqual(pool.worker_pids, ())
        finally:
            pool.close()
            self.assertTrue(_wait_pids_gone(pids), f"orphan worker PIDs: {pids}")

        pool = _new_pool(1, "mc2-invalidate")
        pids = ()
        try:
            pool.start()
            pids = pool.worker_pids
            pool.begin(_make_batches(pool.session_nonce, pool.generation, count=1, delay_ms=1200))
            invalidated = pool.invalidate_generation(pool.generation + 1)
            self.assertFalse(invalidated.complete)
            self.assertTrue(invalidated.cancelled)
            self.assertTrue(invalidated.generation_invalidated)
            self.assertEqual(invalidated.results, ())
            self.assertEqual(pool.worker_pids, ())
        finally:
            pool.close()
            self.assertTrue(_wait_pids_gone(pids), f"orphan worker PIDs: {pids}")

    def test_missing_helper_is_explicit_and_has_no_fallback(self):
        missing = ROOT / ".test_runtime" / "does-not-exist-python.exe"
        pool = POOL.PersistentWorkerPool(
            1,
            worker_script=WORKER_PATH,
            python_executable=missing,
            session_nonce="mc2-missing",
            generation=4,
        )
        with self.assertRaises(POOL.PoolHelperUnavailableError):
            pool.start()
        self.assertEqual(pool.worker_pids, ())

    def test_foreign_result_and_forbidden_worker_imports_are_rejected(self):
        batches = _make_batches("mc2-foreign", 4, count=1)
        task = batches[0]
        wrong_identity = PAYLOAD.SnapshotIdentity("other-session", 4, "mc2-topology-snapshot")
        exact = TOPOLOGY.find_correspondence(
            task.graph_map["master"].to_topology_graph(TOPOLOGY),
            task.graph_map["member-0"].to_topology_graph(TOPOLOGY),
            tolerance=1.0e-8,
        )
        pair_result = PAYLOAD.PairResult.from_correspondence(task.pair_tasks[0], exact)
        foreign = PAYLOAD.BatchResult(wrong_identity, task.batch_id, task.payload_digest(), (pair_result,))
        with self.assertRaises(PAYLOAD.PayloadValidationError):
            foreign.validate_against(task)

        for path in (
            ROOT / "uv_gpt" / "pro_process_worker.py",
            ROOT / "uv_gpt" / "pro_process_pool.py",
            ROOT / "uv_gpt" / "pro_process_payload.py",
        ):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("ThreadPoolExecutor", source)
            self.assertNotIn("ProcessPoolExecutor", source)
            self.assertNotIn("multiprocessing", source)
        worker_source = (ROOT / "uv_gpt" / "pro_process_worker.py").read_text(encoding="utf-8")
        self.assertNotIn("import bpy", worker_source)
        self.assertNotIn("import bmesh", worker_source)
        self.assertNotIn("BMLoop", worker_source)
        self.assertNotIn("uv_gpt.__init__", worker_source)


if __name__ == "__main__":
    unittest.main()
