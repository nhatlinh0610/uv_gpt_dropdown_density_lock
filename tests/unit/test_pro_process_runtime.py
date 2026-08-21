"""Lifecycle tests for the single persistent external proof worker."""

from __future__ import annotations

import importlib.util
import ctypes
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL_PATH = ROOT / "uv_gpt" / "pro_process_protocol.py"
WORKER_PATH = ROOT / "uv_gpt" / "pro_process_worker.py"
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


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROTOCOL = _load_module("pro_process_protocol", PROTOCOL_PATH)
RUNTIME = _load_module("pro_process_runtime_test_module", ROOT / "uv_gpt" / "pro_process_runtime.py")


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


def _wait_pid_gone(pid: int | None, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.02)
    return not _pid_alive(pid)


def _payload(reverse: bool = False):
    items = [("zeta", [3, 4]), ("alpha", [1, 2]), ("middle", [2, 2])]
    if reverse:
        items.reverse()
    return {"operation": "sum_squares", "offset": 0.5, "items": items}


class ProProcessRuntimeTests(unittest.TestCase):
    def test_resolver_returns_exact_portable_python(self):
        resolved = RUNTIME.resolve_bundled_python(
            blender_binary=BLENDER_PATH,
            blender_version="5.0.0",
        )
        self.assertEqual(resolved, PYTHON_PATH.resolve())
        self.assertEqual(RUNTIME.probe_python_version(resolved).split(".")[:2], ["3", "11"])
        info = RUNTIME.bundled_python_info(
            blender_binary=BLENDER_PATH,
            blender_version=(5, 0, 0),
        )
        self.assertEqual(info.executable, PYTHON_PATH.resolve())
        self.assertEqual(info.version_dir, "5.0")

    def test_missing_helper_is_explicit_and_has_no_fallback(self):
        with tempfile.TemporaryDirectory(prefix="uv_gpt_mc1_missing_") as temp:
            missing = Path(temp) / "missing_worker.py"
            runtime = RUNTIME.PersistentSingleWorker(worker_script=missing)
            with self.assertRaises(RUNTIME.HelperUnavailableError):
                runtime.start()
            self.assertIsNone(runtime.process)
            runtime.close()

    def test_worker_side_static_boundary_avoids_package_initialization(self):
        source = WORKER_PATH.read_text(encoding="utf-8").lower()
        self.assertNotIn("import bpy", source)
        self.assertNotIn("import bmesh", source)
        self.assertNotIn("uv_gpt", source)
        self.assertNotIn("__init__", source)
        runtime_source = (ROOT / "uv_gpt" / "pro_process_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("threadpoolexecutor", runtime_source.lower())
        self.assertNotIn("processpoolexecutor", runtime_source.lower())

    def test_real_bundled_worker_handles_multiple_tasks_on_one_pid(self):
        runtime = RUNTIME.PersistentSingleWorker(
            worker_script=WORKER_PATH,
            blender_binary=BLENDER_PATH,
            blender_version="5.0",
            handshake_timeout=5.0,
            io_timeout=5.0,
        )
        try:
            ready = runtime.start()
            self.assertEqual(ready.message_type, PROTOCOL.MessageType.READY)
            self.assertEqual(runtime.command[0], str(PYTHON_PATH.resolve()))
            ready_payload = ready.payload
            self.assertIsInstance(ready_payload, dict)
            self.assertEqual(ready_payload["thread_caps"], {name: "1" for name in RUNTIME.THREAD_CAP_NAMES})
            pid = runtime.pid
            first = runtime.submit(_payload(), batch_id="proof-a")
            second = runtime.submit(_payload(reverse=True), batch_id="proof-b")
            self.assertEqual(runtime.pid, pid)

            # Awaiting the second first proves that completion order is not the
            # caller's input order and that the first result is buffered safely.
            result_second = runtime.await_result(second, timeout=5.0)
            result_first = runtime.await_result(first, timeout=5.0)
            self.assertEqual(result_first.message_type, PROTOCOL.MessageType.RESULT)
            self.assertEqual(result_first.payload, result_second.payload)
            self.assertEqual(
                result_first.payload["items"],
                (("alpha", 5.5), ("middle", 8.5), ("zeta", 25.5)),
            )

            shutdown = runtime.shutdown(timeout=3.0)
            self.assertIsNotNone(shutdown)
            self.assertEqual(shutdown.message_type, PROTOCOL.MessageType.SHUTDOWN_ACK)
            self.assertFalse(runtime.is_alive)
            self.assertTrue(_wait_pid_gone(pid))
        finally:
            runtime.close(graceful=False)

    def test_current_python_fake_proof_and_cancel_ack(self):
        runtime = RUNTIME.PersistentSingleWorker(
            worker_script=WORKER_PATH,
            python_executable=sys.executable,
            handshake_timeout=5.0,
            io_timeout=5.0,
        )
        try:
            runtime.start()
            ticket = runtime.submit(_payload(), batch_id="cancel-me")
            ack = runtime.cancel(batch_id=ticket.batch_id, sequence=ticket.sequence, timeout=5.0)
            self.assertIsNotNone(ack)
            self.assertEqual(ack.message_type, PROTOCOL.MessageType.CANCEL_ACK)
            self.assertEqual(runtime.pending_count, 0)
            pid = runtime.pid
            runtime.shutdown(timeout=3.0)
            self.assertTrue(_wait_pid_gone(pid))
        finally:
            runtime.close(graceful=False)

    def test_crash_eof_is_reported_and_exact_handle_cleanup_is_idempotent(self):
        runtime = RUNTIME.PersistentSingleWorker(
            worker_script=WORKER_PATH,
            python_executable=sys.executable,
            handshake_timeout=5.0,
        )
        runtime.start()
        process = runtime.process
        pid = runtime.pid
        self.assertIsNotNone(process)
        process.terminate()
        process.wait(timeout=3.0)
        with self.assertRaises((RUNTIME.WorkerCrashedError, RUNTIME.WorkerEOFError)):
            runtime.poll(timeout=1.0)
        runtime.close(graceful=False)
        runtime.close(graceful=False)
        self.assertTrue(_wait_pid_gone(pid))
        self.assertIsNone(runtime.process)

    def test_stale_generation_is_discarded_before_result_acceptance(self):
        runtime = RUNTIME.PersistentSingleWorker(session_nonce="local", generation=4)
        stale = PROTOCOL.Envelope(
            PROTOCOL.MessageType.RESULT,
            session_nonce="local",
            generation=3,
            batch_id="old",
            sequence=1,
            item_count=1,
            payload={"old": True},
        )
        self.assertIsNone(runtime._accept_message(stale))
        foreign = PROTOCOL.Envelope(
            PROTOCOL.MessageType.RESULT,
            session_nonce="foreign",
            generation=4,
            batch_id="foreign",
            sequence=1,
            item_count=1,
            payload={"foreign": True},
        )
        with self.assertRaises(RUNTIME.ForeignFrameError):
            runtime._accept_message(foreign)


if __name__ == "__main__":
    unittest.main()
