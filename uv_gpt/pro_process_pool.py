"""Persistent multiworker pool for pure Pro correspondence batches.

This module owns external helper processes only.  It has no Blender adapter and
never applies UV values.  Main-thread integration and ownership decisions are a
later packet concern.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import ctypes
import math
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Optional

try:
    from .pro_process_payload import (
        BatchResult,
        BatchTask,
        CompleteResultCache,
        GraphBuildTask,
        GraphContextLoadResult,
        GraphContextLoadTask,
        GraphContextPayload,
        PayloadValidationError,
        PairResult,
        SnapshotIdentity,
        estimate_batch_frame,
        pair_result_digest_wire,
        stable_digest,
    )
    from .pro_process_runtime import (
        ForeignFrameError,
        HelperUnavailableError,
        PersistentSingleWorker,
        ProcessRuntimeError,
        WorkerCrashedError,
        WorkerEOFError,
        WorkerProtocolError,
        WorkerRemoteError,
        WorkerTimeoutError,
    )
except ImportError:  # direct-file test loading without package initialization
    from pro_process_payload import (  # type: ignore[no-redef]
        BatchResult,
        BatchTask,
        CompleteResultCache,
        GraphBuildTask,
        GraphContextLoadResult,
        GraphContextLoadTask,
        GraphContextPayload,
        PayloadValidationError,
        PairResult,
        SnapshotIdentity,
        estimate_batch_frame,
        pair_result_digest_wire,
        stable_digest,
    )
    from pro_process_runtime import (  # type: ignore[no-redef]
        ForeignFrameError,
        HelperUnavailableError,
        PersistentSingleWorker,
        ProcessRuntimeError,
        WorkerCrashedError,
        WorkerEOFError,
        WorkerProtocolError,
        WorkerRemoteError,
        WorkerTimeoutError,
    )


class PoolError(RuntimeError):
    """Base class for pool lifecycle and result errors."""


class PoolHelperUnavailableError(PoolError):
    """The requested external worker set could not be started."""


class PoolProtocolError(PoolError):
    """A worker returned a stale, foreign or malformed result."""


class PoolStreamBusyError(PoolError):
    """A streaming operation has no bounded admission slot available."""


@dataclass(frozen=True)
class JobObjectCapability:
    requested: bool
    available: bool
    kill_on_close: bool
    assigned_count: int
    reason: str


class _WindowsJobObject:
    """Small optional Job Object wrapper with an exact-process fallback."""

    def __init__(self) -> None:
        self.handle: Any = None
        self.kernel32: Any = None
        self.assigned_count = 0
        self.kill_on_close = False
        self.reason = "not_attempted"

    @property
    def requested(self) -> bool:
        return os.name == "nt"

    def prepare(self) -> None:
        if os.name != "nt":
            self.reason = "non_windows_exact_handle_fallback"
            return
        try:
            self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self.kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            self.kernel32.CreateJobObjectW.restype = ctypes.c_void_p
            self.kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self.kernel32.AssignProcessToJobObject.restype = ctypes.c_int
            self.kernel32.SetInformationJobObject.argtypes = [
                ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32
            ]
            self.kernel32.SetInformationJobObject.restype = ctypes.c_int
            self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            self.kernel32.CloseHandle.restype = ctypes.c_int
            self.handle = self.kernel32.CreateJobObjectW(None, None)
            if not self.handle:
                self.reason = f"CreateJobObjectW failed:{ctypes.get_last_error()}"
                self.handle = None
                return
            self.reason = "job_created_pending_limit"
        except Exception as exc:
            self.reason = f"job_api_unavailable:{type(exc).__name__}"
            self.handle = None

    def assign(self, process: Any) -> bool:
        if self.handle is None or self.kernel32 is None:
            return False
        process_handle = getattr(process, "_handle", None)
        if process_handle is None:
            self.reason = "popen_handle_unavailable"
            return False
        try:
            ok = bool(self.kernel32.AssignProcessToJobObject(self.handle, ctypes.c_void_p(int(process_handle))))
        except Exception as exc:
            self.reason = f"assign_exception:{type(exc).__name__}"
            return False
        if not ok:
            self.reason = f"AssignProcessToJobObject failed:{ctypes.get_last_error()}"
            return False
        self.assigned_count += 1
        return True

    def enable_kill_on_close(self) -> bool:
        if self.handle is None or self.kernel32 is None:
            return False

        class _BasicLimit(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_ulonglong), ("WriteOperationCount", ctypes.c_ulonglong),
                        ("OtherOperationCount", ctypes.c_ulonglong), ("ReadTransferCount", ctypes.c_ulonglong),
                        ("WriteTransferCount", ctypes.c_ulonglong), ("OtherTransferCount", ctypes.c_ulonglong)]

        class _ExtendedLimit(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters)]

        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            ok = bool(
                self.kernel32.SetInformationJobObject(
                    self.handle,
                    9,  # JobObjectExtendedLimitInformation
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
            )
        except Exception as exc:
            self.reason = f"job_limit_exception:{type(exc).__name__}"
            return False
        if not ok:
            self.reason = f"SetInformationJobObject failed:{ctypes.get_last_error()}"
            return False
        self.kill_on_close = True
        self.reason = "job_object_kill_on_close"
        return True

    def close(self) -> None:
        if self.handle is not None and self.kernel32 is not None:
            try:
                self.kernel32.CloseHandle(self.handle)
            except Exception:
                pass
        self.handle = None


@dataclass(frozen=True)
class PoolProgress:
    pairs_total: int
    pairs_done: int
    exact_count: int
    batches_total: int
    batches_done: int
    active_workers: int
    retry_count: int
    elapsed_ms: float
    cancelled: bool = False
    failed: bool = False
    retry_total: int = 0
    max_retry_per_batch: int = 0
    retried_batch_count: int = 0
    retry_failure_reason: str = ""
    retry_batches: tuple[tuple[str, int], ...] = ()
    graph_tasks_submitted: int = 0
    graph_tasks_completed: int = 0
    graph_items_total: int = 0
    graph_items_completed: int = 0
    graph_cache_hits: int = 0
    resident_topology_cache_builds: int = 0
    resident_topology_cache_hits: int = 0
    resident_topology_compute_ms: float = 0.0
    graph_context_ready: bool = False
    context_load_submitted: int = 0
    context_load_acked: int = 0
    context_load_frame_bytes: int = 0
    context_load_payload_bytes: int = 0
    context_load_ms: float = 0.0
    frame_bytes_max: tuple[tuple[str, int], ...] = ()
    frame_bytes_total: tuple[tuple[str, int], ...] = ()
    restart_pending: int = 0
    restart_states: tuple[tuple[int, str, str], ...] = ()
    nearest_attempted: int = 0
    nearest_accepted: int = 0
    nearest_fallback: int = 0
    nearest_max_seed_distance: float = 0.0
    nearest_mean_seed_distance: float = 0.0
    nearest_ambiguity_count: int = 0
    nearest_tie_count: int = 0
    nearest_compute_ms: float = 0.0
    nearest_distance_evaluations: int = 0
    nearest_assignment_nodes: int = 0
    nearest_assignment_cap: int = 0
    nearest_fallback_reasons: tuple[tuple[int, int], ...] = ()
    nearest_distance_lookups: int = 0
    nearest_distance_cache_hits: int = 0
    nearest_distance_cache_misses: int = 0
    nearest_operations_used: int = 0
    graph_rejected_before_nearest: int = 0
    nearest_seed_missing: int = 0
    nearest_fast_miss: int = 0
    exact_fallback_calls: int = 0
    exact_primary_calls: int = 0
    shutdown_state: str = "idle"
    shutdown_rounds: int = 0
    shutdown_force_used: bool = False
    shutdown_complete: bool = False
    # Startup/context telemetry is deliberately split by ownership.  The
    # owner values measure only the modal caller's scheduling/adoption work;
    # background values include the exact helper bootstrap or context write.
    worker_start_owner_ms: float = 0.0
    worker_start_background_ms: float = 0.0
    startup_pending: int = 0
    startup_states: tuple[tuple[int, str], ...] = ()
    context_load_pending: int = 0
    context_serialize_owner_ms: float = 0.0
    context_serialize_background_ms: float = 0.0
    context_write_background_ms: float = 0.0
    # Task admission is separate from worker execution.  These fields make
    # the modal owner/background boundary observable without exposing a
    # speculative task as active before its graph/context and stream write
    # have completed.
    admission_pending: int = 0
    admission_states: tuple[tuple[int, str, str], ...] = ()
    admission_owner_ms: float = 0.0
    admission_validate_background_ms: float = 0.0
    admission_serialize_background_ms: float = 0.0
    admission_write_background_ms: float = 0.0


@dataclass(frozen=True)
class PoolResult:
    complete: bool
    cancelled: bool
    generation_invalidated: bool
    results: tuple[Any, ...]
    result_digest: str
    progress: PoolProgress
    failure: str = ""
    worker_distribution: tuple[tuple[int, int], ...] = ()
    worker_pids: tuple[int, ...] = ()


@dataclass(frozen=True)
class StreamCompletion:
    """One fully validated typed task result from a streaming operation."""

    task: Any
    result: Any
    worker_index: int
    batch_id: str
    sequence: int


@dataclass
class _Slot:
    index: int
    worker: PersistentSingleWorker
    active: Optional["_ActiveBatch"] = None
    last_master: Optional[str] = None
    completed_batches: int = 0
    completed_pairs: int = 0


@dataclass
class _ActiveBatch:
    task: Any
    ticket: Any
    started_at: float


@dataclass(frozen=True)
class _ReadyBatch:
    task: Any
    avoid_slot: Optional[int] = None


@dataclass
class _PendingRestart:
    slot_index: int
    replacement: PersistentSingleWorker
    task: Any
    is_context: bool
    retry_key: str
    started_at: float
    state: dict[str, Any]
    cancel_event: threading.Event
    thread: threading.Thread


@dataclass
class _PendingStartup:
    """One exact worker bootstrap owned outside the modal caller."""

    slot_index: int
    worker: Any
    started_at: float
    state: dict[str, Any]
    cancel_event: threading.Event
    thread: threading.Thread


@dataclass
class _PendingContextSend:
    """One context estimate/encode/write operation owned by a worker slot."""

    slot_index: int
    task: GraphContextLoadTask
    started_at: float
    state: dict[str, Any]
    cancel_event: threading.Event
    thread: threading.Thread


@dataclass
class _PendingAdmission:
    """One task validation/encode/write owned by a worker slot.

    The owner thread only creates and later adopts this record.  All
    potentially expensive task validation, frame estimation, wire encoding,
    and the worker stream write happen in ``thread`` while this record owns
    the slot.  This is deliberately analogous to ``_PendingContextSend`` but
    retains the logical task so a completed admission can become active in a
    deterministic owner-side step.
    """

    slot_index: int
    task: Any
    started_at: float
    state: dict[str, Any]
    cancel_event: threading.Event
    thread: threading.Thread


@dataclass
class _PendingWorkerCleanup:
    """Owned detached-worker cleanup running outside the modal caller."""

    key: str
    worker: Any
    state: dict[str, Any]
    thread: threading.Thread


def _fused_task_master_key(task: Any) -> str:
    """Return the stable cache-affinity key for one fused task."""

    value = getattr(task, "master_key", None)
    if value is not None:
        return repr(value)
    pairs = tuple(getattr(task, "pair_tasks", ()))
    if pairs:
        return repr(getattr(pairs[0], "master_key", ""))
    return ""


def _fused_pair_cost(pair: Any) -> int:
    """Deterministic integer work estimate used only for worker binning.

    This is deliberately a stable scheduling heuristic, never a correctness
    rule. It charges loop material and exact-search allowance while making a
    cheap prefiltered result inexpensive.
    """

    prefilter = getattr(pair, "prefilter", None)
    if prefilter is not None:
        return 1
    master = tuple(getattr(pair, "master_loop_keys", ()))
    member = tuple(getattr(pair, "member_loop_keys", ()))
    options = getattr(pair, "exact_options", None)
    search = int(getattr(options, "max_search", 1024) or 1)
    search = min(max(search, 1), 1024)
    return 8 + 2 * (len(master) + len(member)) + 16 + 2 * min(
        len(master), len(member)
    ) + int(math.ceil(search / 32.0))


def fused_task_predicted_cost(task: Any) -> int:
    """Return the stable predicted cost for a master-affine fused task."""

    pairs = tuple(getattr(task, "pair_tasks", ()))
    if not pairs:
        return 1
    master_loops = len(tuple(getattr(pairs[0], "master_loop_keys", ())))
    unique_islands = {
        repr(value)
        for pair in pairs
        for value in (
            getattr(pair, "master_key", None),
            getattr(pair, "member_key", None),
        )
    }
    return max(
        1,
        sum(_fused_pair_cost(pair) for pair in pairs)
        + 8 * len(unique_islands)
        + 64 * master_loops,
    )


def plan_fused_affinity(
    tasks: Any,
    worker_count: int,
) -> tuple[tuple[str, int], ...]:
    """Assign fused tasks to deterministic LPT worker bins.

    The result is advisory metadata for dispatch. Canonical merge and retry
    identity remain independent of this assignment. Stable ties use worker
    index, master key, first ordinal and batch id so repeated runs produce
    the same plan.
    """

    if (
        isinstance(worker_count, bool)
        or not isinstance(worker_count, int)
        or not 1 <= worker_count <= 8
    ):
        raise ValueError("worker_count must be between 1 and 8")
    items = tuple(tasks)
    bins = [0] * worker_count
    ordered = sorted(
        items,
        key=lambda task: (
            -fused_task_predicted_cost(task),
            _fused_task_master_key(task),
            tuple(getattr(task, "pair_ordinals", ()))[:1] or (10**18,),
            str(getattr(task, "batch_id", "")),
        ),
    )
    assignments: dict[str, int] = {}
    for task in ordered:
        index = min(range(worker_count), key=lambda value: (bins[value], value))
        batch_id = str(getattr(task, "batch_id", ""))
        if batch_id:
            assignments[batch_id] = index
        bins[index] += fused_task_predicted_cost(task)
    return tuple(sorted(assignments.items()))


class PersistentWorkerPool:
    """Dynamic scheduler over N persistent single-worker runtimes."""

    def __init__(
        self,
        worker_count: int,
        *,
        worker_script: object = None,
        blender_binary: object = None,
        blender_root: object = None,
        blender_version: object = None,
        python_executable: object = None,
        session_nonce: Optional[str] = None,
        generation: int = 0,
        handshake_timeout: float = 5.0,
        io_timeout: float = 5.0,
        use_cache: bool = True,
    ) -> None:
        if isinstance(worker_count, bool) or not isinstance(worker_count, int) or not 1 <= worker_count <= 8:
            raise ValueError("worker_count must be between 1 and 8")
        self.worker_count = worker_count
        self.worker_script = (
            Path(worker_script) if worker_script is not None else Path(__file__).with_name("pro_process_worker.py")
        ).expanduser().resolve(strict=False)
        self.blender_binary = blender_binary
        self.blender_root = blender_root
        self.blender_version = blender_version
        self.python_executable = python_executable
        self.session_nonce = session_nonce or secrets.token_hex(16)
        if not isinstance(self.session_nonce, str) or not self.session_nonce:
            raise ValueError("session_nonce must be non-empty text")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        self.generation = generation
        self.handshake_timeout = handshake_timeout
        self.io_timeout = io_timeout
        self.use_cache = bool(use_cache)

        self._slots: list[_Slot] = []
        self._job = _WindowsJobObject()
        self._startup_job_prepared = False
        self._job_capability = JobObjectCapability(False, False, False, 0, "not_started")
        self._startup_timings: list[float] = []
        self._pending_startups: dict[int, _PendingStartup] = {}
        self._worker_start_owner_ms = 0.0
        self._worker_start_background_ms = 0.0
        self._shutdown_timings: list[float] = []
        self._shutdown_state = "idle"
        self._shutdown_started_at = 0.0
        self._shutdown_grace_deadline = 0.0
        self._shutdown_rounds = 0
        self._shutdown_force_used = False
        self._shutdown_complete = False
        self._shutdown_slot_started: dict[int, float] = {}
        # Cancellation has its own resumable state.  Modal ESC/context
        # invalidation must be able to discard semantic state immediately
        # without waiting for a worker CANCEL_ACK or process.wait().
        self._cancel_state = "idle"
        self._cancel_started_at = 0.0
        self._cancel_rounds = 0
        self._cancel_complete = False
        self._ready_batches: deque[_ReadyBatch] = deque()
        self._undispatched: deque[Any] = deque()
        self._active: dict[int, _ActiveBatch] = {}
        self._pending_admissions: dict[int, _PendingAdmission] = {}
        self._pending_restarts: dict[int, _PendingRestart] = {}
        self._pending_cleanups: dict[str, _PendingWorkerCleanup] = {}
        self._tasks: dict[str, Any] = {}
        self._result_buffer: dict[int, Any] = {}
        self._retry_counts: dict[str, int] = {}
        self._retry_failure_reason = ""
        self._worker_distribution: dict[int, int] = {}
        self._worker_operation_distribution: dict[int, dict[str, int]] = {}
        self._pairs_total = 0
        self._pairs_done = 0
        self._exact_count = 0
        self._batches_done = 0
        self._run_started_at = 0.0
        self._terminal = False
        self._cancelled = False
        self._generation_invalidated = False
        self._failure = ""
        self._cache = CompleteResultCache()
        self._cache_hits = 0
        self._stream_mode = False
        self._stream_closed = False
        self._stream_completions: list[StreamCompletion] = []
        self._graph_tasks_submitted = 0
        self._graph_tasks_completed = 0
        self._graph_items_total = 0
        self._graph_items_completed = 0
        self._graph_cache_hits = 0
        self._resident_topology_cache_builds = 0
        self._resident_topology_cache_hits = 0
        self._resident_topology_compute_ms = 0.0
        self._graph_context_payload: Optional[GraphContextPayload] = None
        self._context_active: dict[int, _ActiveBatch] = {}
        self._pending_context_sends: dict[int, _PendingContextSend] = {}
        self._context_loaded: set[int] = set()
        self._context_replay_pending: set[int] = set()
        self._context_retry_counts: dict[str, int] = {}
        self._context_load_submitted = 0
        self._context_load_acked = 0
        self._context_load_frame_bytes = 0
        self._context_load_payload_bytes = 0
        self._context_load_ms = 0.0
        self._context_load_frame_max_bytes = 0
        self._context_serialize_owner_ms = 0.0
        self._context_serialize_background_ms = 0.0
        self._context_write_background_ms = 0.0
        self._admission_owner_ms = 0.0
        self._admission_validate_background_ms = 0.0
        self._admission_serialize_background_ms = 0.0
        self._admission_write_background_ms = 0.0
        self._frame_bytes_max: dict[str, int] = {}
        self._frame_bytes_total: dict[str, int] = {}
        self._task_frame_estimates: dict[str, Any] = {}
        self._preferred_workers: dict[str, int] = {}
        self._dispatch_counts: dict[int, int] = {}
        self._nearest_attempted = 0
        self._nearest_accepted = 0
        self._nearest_fallback = 0
        self._nearest_max_seed_distance = 0.0
        self._nearest_mean_seed_sum = 0.0
        self._nearest_mean_sample_count = 0
        self._nearest_ambiguity_count = 0
        self._nearest_tie_count = 0
        self._nearest_compute_ms = 0.0
        self._nearest_distance_evaluations = 0
        self._nearest_assignment_nodes = 0
        self._nearest_assignment_cap = 0
        self._nearest_fallback_reasons: dict[int, int] = {}
        self._nearest_distance_lookups = 0
        self._nearest_distance_cache_hits = 0
        self._nearest_distance_cache_misses = 0
        self._nearest_operations_used = 0
        self._graph_rejected_before_nearest = 0
        self._nearest_seed_missing = 0
        self._nearest_fast_miss = 0
        self._exact_fallback_calls = 0
        self._exact_primary_calls = 0

    @property
    def workers(self) -> tuple[PersistentSingleWorker, ...]:
        return tuple(slot.worker for slot in self._slots)

    @property
    def worker_processes(self) -> tuple[PersistentSingleWorker, ...]:
        return self.workers

    @property
    def worker_pids(self) -> tuple[int, ...]:
        return tuple(worker.pid for worker in self.workers if worker.pid is not None and worker.is_alive)

    @property
    def startup_timings_ms(self) -> tuple[float, ...]:
        return tuple(self._startup_timings)

    @property
    def worker_start_owner_ms(self) -> float:
        return float(self._worker_start_owner_ms)

    @property
    def worker_start_background_ms(self) -> float:
        return float(self._worker_start_background_ms)

    @property
    def startup_pending(self) -> int:
        self._reap_pending_startups()
        return len(self._pending_startups)

    @property
    def startup_states(self) -> tuple[tuple[int, str], ...]:
        self._reap_pending_startups()
        values = []
        for index, pending in sorted(self._pending_startups.items()):
            state = "bootstrap_pending" if pending.thread.is_alive() else "bootstrap_ready"
            if pending.state.get("error") is not None:
                state = "bootstrap_failed"
            values.append((int(index), state))
        return tuple(values)

    @property
    def shutdown_timings_ms(self) -> tuple[float, ...]:
        return tuple(self._shutdown_timings)

    @property
    def shutdown_state(self) -> str:
        return str(self._shutdown_state)

    @property
    def shutdown_rounds(self) -> int:
        return int(self._shutdown_rounds)

    @property
    def shutdown_force_used(self) -> bool:
        return bool(self._shutdown_force_used)

    @property
    def shutdown_complete(self) -> bool:
        return bool(self._shutdown_complete)

    @property
    def cancel_state(self) -> str:
        return str(self._cancel_state)

    @property
    def cancel_rounds(self) -> int:
        return int(self._cancel_rounds)

    @property
    def cancel_complete(self) -> bool:
        return bool(self._cancel_complete)

    @property
    def job_object_capability(self) -> JobObjectCapability:
        return self._job_capability

    @property
    def retry_count(self) -> int:
        return self.retry_total

    @property
    def retry_total(self) -> int:
        return sum(self._retry_counts.values())

    @property
    def max_retry_per_batch(self) -> int:
        return max(self._retry_counts.values(), default=0)

    @property
    def retried_batch_count(self) -> int:
        return sum(1 for value in self._retry_counts.values() if value > 0)

    @property
    def retry_failure_reason(self) -> str:
        return str(self._retry_failure_reason or "")

    @property
    def retry_batches(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted((str(key), int(value)) for key, value in self._retry_counts.items()))

    @property
    def is_terminal(self) -> bool:
        return self._terminal

    @property
    def active_workers(self) -> int:
        return len(self._active)

    @property
    def restart_pending(self) -> int:
        return len(self._pending_restarts)

    @property
    def restart_states(self) -> tuple[tuple[int, str, str], ...]:
        values = []
        for index, pending in sorted(self._pending_restarts.items()):
            state = "handshake_pending" if pending.thread.is_alive() else "restart_ready"
            values.append((int(index), state, str(getattr(pending.task, "batch_id", ""))))
        return tuple(values)

    @property
    def cleanup_pending(self) -> int:
        self._reap_worker_cleanups()
        return sum(
            1
            for item in self._pending_cleanups.values()
            if item.thread.is_alive() or not bool(item.state.get("done"))
        )

    @property
    def graph_context_payload(self) -> Optional[GraphContextPayload]:
        return self._graph_context_payload

    @property
    def graph_context_digest(self) -> str:
        return "" if self._graph_context_payload is None else self._graph_context_payload.context_digest

    @property
    def graph_context_ready(self) -> bool:
        return bool(
            self._graph_context_payload is not None
            and len(self._context_loaded) == len(self._slots)
            and not self._context_active
            and not self._pending_context_sends
        )

    @property
    def graph_context_loaded_workers(self) -> tuple[int, ...]:
        return tuple(sorted(self._context_loaded))

    @property
    def context_load_inflight(self) -> int:
        return len(self._context_active)

    @property
    def context_load_pending(self) -> int:
        return len(self._pending_context_sends)

    @property
    def context_serialize_owner_ms(self) -> float:
        return float(self._context_serialize_owner_ms)

    @property
    def context_serialize_background_ms(self) -> float:
        return float(self._context_serialize_background_ms)

    @property
    def context_write_background_ms(self) -> float:
        return float(self._context_write_background_ms)

    @property
    def admission_pending(self) -> int:
        return len(self._pending_admissions)

    @property
    def admission_states(self) -> tuple[tuple[int, str, str], ...]:
        values = []
        for index, pending in sorted(self._pending_admissions.items()):
            state = "admission_pending" if pending.thread.is_alive() else "admission_ready"
            if pending.state.get("error") is not None:
                state = "admission_failed"
            values.append((int(index), state, str(getattr(pending.task, "batch_id", ""))))
        return tuple(values)

    @property
    def admission_owner_ms(self) -> float:
        return float(self._admission_owner_ms)

    @property
    def admission_validate_background_ms(self) -> float:
        return float(self._admission_validate_background_ms)

    @property
    def admission_serialize_background_ms(self) -> float:
        return float(self._admission_serialize_background_ms)

    @property
    def admission_write_background_ms(self) -> float:
        return float(self._admission_write_background_ms)

    @property
    def context_load_submitted(self) -> int:
        return int(self._context_load_submitted)

    @property
    def context_load_acked(self) -> int:
        return int(self._context_load_acked)

    @property
    def context_load_frame_bytes(self) -> int:
        return int(self._context_load_frame_bytes)

    @property
    def context_load_payload_bytes(self) -> int:
        return int(self._context_load_payload_bytes)

    @property
    def context_load_frame_max_bytes(self) -> int:
        return int(self._context_load_frame_max_bytes)

    @property
    def frame_bytes_max_by_operation(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted((str(key), int(value)) for key, value in self._frame_bytes_max.items()))

    @property
    def frame_bytes_total_by_operation(self) -> tuple[tuple[str, int], ...]:
        return tuple(sorted((str(key), int(value)) for key, value in self._frame_bytes_total.items()))

    @property
    def context_load_ms(self) -> float:
        return float(self._context_load_ms)

    @property
    def queue_depth(self) -> int:
        """Ready, in-flight and not-yet-admitted batches.

        The scheduler deliberately admits no more than ``2 * N`` batches, so
        this value is also the live bound that the modal pipeline can report.
        """
        return len(self._ready_batches) + len(self._active) + len(self._pending_admissions)

    @property
    def stream_queue_depth(self) -> int:
        """All admitted streaming batches, including not-yet-dispatched work."""

        return (
            len(self._ready_batches)
            + len(self._active)
            + len(self._pending_admissions)
            + len(self._undispatched)
        )

    @property
    def stream_capacity(self) -> int:
        """Number of additional batches admitted by the 2*N frontier bound."""

        return max(0, 2 * self.worker_count - self.stream_queue_depth)

    @property
    def worker_task_distribution(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted(self._worker_distribution.items()))

    @property
    def worker_operation_distribution(self) -> tuple[tuple[int, tuple[tuple[str, int], ...]], ...]:
        return tuple(
            (int(index), tuple(sorted((str(kind), int(count)) for kind, count in values.items())))
            for index, values in sorted(self._worker_operation_distribution.items())
        )

    @property
    def cache_hits(self) -> int:
        return int(self._cache_hits)

    @property
    def nearest_mean_seed_distance(self) -> float:
        if self._nearest_mean_sample_count <= 0:
            return 0.0
        return float(self._nearest_mean_seed_sum) / float(self._nearest_mean_sample_count)

    def _reset_nearest_metrics(self) -> None:
        self._nearest_attempted = 0
        self._nearest_accepted = 0
        self._nearest_fallback = 0
        self._nearest_max_seed_distance = 0.0
        self._nearest_mean_seed_sum = 0.0
        self._nearest_mean_sample_count = 0
        self._nearest_ambiguity_count = 0
        self._nearest_tie_count = 0
        self._nearest_compute_ms = 0.0
        self._nearest_distance_evaluations = 0
        self._nearest_assignment_nodes = 0
        self._nearest_assignment_cap = 0
        self._nearest_fallback_reasons.clear()
        self._nearest_distance_lookups = 0
        self._nearest_distance_cache_hits = 0
        self._nearest_distance_cache_misses = 0
        self._nearest_operations_used = 0
        self._graph_rejected_before_nearest = 0
        self._nearest_seed_missing = 0
        self._nearest_fast_miss = 0
        self._exact_fallback_calls = 0
        self._exact_primary_calls = 0

    def _record_nearest_metrics(self, item: Any) -> None:
        """Aggregate deterministic nearest evidence from one pair outcome."""

        raw = getattr(item, "diagnostics", ())
        try:
            values = {str(name): value for name, value in raw}
        except (TypeError, ValueError):
            return
        self._graph_rejected_before_nearest += int(
            values.get("graph_rejected_before_nearest", 0) or 0
        )
        self._nearest_seed_missing += int(
            values.get("nearest_seed_missing", 0) or 0
        )
        self._nearest_fast_miss += int(
            values.get(
                "nearest_fast_miss",
                values.get("nearest_fallback", 0),
            )
            or 0
        )
        self._exact_fallback_calls += int(
            values.get("exact_fallback_calls", 0) or 0
        )
        self._exact_primary_calls += int(
            values.get("exact_primary_calls", 0) or 0
        )
        if not int(values.get("nearest_attempted", 0) or 0):
            return
        self._nearest_attempted += 1
        accepted = int(bool(values.get("nearest_accepted", 0)))
        self._nearest_accepted += accepted
        self._nearest_fallback += int(
            bool(values.get("nearest_fallback", not accepted))
        )
        try:
            maximum = float(values.get("nearest_max_seed_distance", 0.0))
            mean = float(values.get("nearest_mean_seed_distance", 0.0))
            compute_ms = float(values.get("nearest_compute_ms", 0.0))
        except (TypeError, ValueError):
            return
        if math.isfinite(maximum):
            self._nearest_max_seed_distance = max(self._nearest_max_seed_distance, maximum)
        if math.isfinite(mean):
            self._nearest_mean_seed_sum += mean
            self._nearest_mean_sample_count += 1
        if math.isfinite(compute_ms):
            self._nearest_compute_ms += compute_ms
        try:
            self._nearest_ambiguity_count += int(values.get("nearest_ambiguity_count", 0) or 0)
            self._nearest_tie_count += int(values.get("nearest_tie_count", 0) or 0)
            self._nearest_distance_evaluations += int(
                values.get("nearest_distance_evaluations", 0) or 0
            )
            self._nearest_assignment_nodes += int(
                values.get("nearest_assignment_nodes", 0) or 0
            )
            self._nearest_assignment_cap = max(
                self._nearest_assignment_cap,
                int(values.get("nearest_assignment_cap", 0) or 0),
            )
            self._nearest_distance_lookups += int(
                values.get("nearest_distance_lookups", 0) or 0
            )
            self._nearest_distance_cache_hits += int(
                values.get("nearest_distance_cache_hits", 0) or 0
            )
            self._nearest_distance_cache_misses += int(
                values.get("nearest_distance_cache_misses", 0) or 0
            )
            self._nearest_operations_used += int(
                values.get("nearest_operations_used", 0) or 0
            )
            if not accepted:
                code = int(values.get("nearest_fallback_reason_code", 0) or 0)
                self._nearest_fallback_reasons[code] = (
                    self._nearest_fallback_reasons.get(code, 0) + 1
                )
        except (TypeError, ValueError):
            pass

    def _make_worker(self) -> PersistentSingleWorker:
        return PersistentSingleWorker(
            worker_script=self.worker_script,
            blender_binary=self.blender_binary,
            blender_root=self.blender_root,
            blender_version=self.blender_version,
            python_executable=self.python_executable,
            session_nonce=self.session_nonce,
            generation=self.generation,
            handshake_timeout=self.handshake_timeout,
            io_timeout=self.io_timeout,
        )

    def _refresh_job_capability(self) -> None:
        self._job_capability = JobObjectCapability(
            requested=self._job.requested,
            available=self._job.handle is not None and self._job.kill_on_close,
            kill_on_close=self._job.kill_on_close,
            assigned_count=self._job.assigned_count,
            reason=self._job.reason,
        )

    def _assign_job_if_possible(self, worker: PersistentSingleWorker) -> None:
        if self._job.handle is None:
            return
        if not self._job.assign(worker.process):
            self._job_capability = JobObjectCapability(
                requested=self._job.requested,
                available=False,
                kill_on_close=False,
                assigned_count=self._job.assigned_count,
                reason=self._job.reason,
            )
            return

    def _prepare_startup_job(self) -> None:
        """Prepare the process containment boundary exactly once per startup."""

        if self._startup_job_prepared:
            return
        self._job = _WindowsJobObject()
        self._job.prepare()
        self._startup_job_prepared = True
        self._startup_timings = []

    def _finalize_startup_job(self) -> None:
        if len(self._slots) < self.worker_count:
            return
        if self._job.handle is not None and self._job.assigned_count == self.worker_count:
            if not self._job.enable_kill_on_close():
                self._job.close()
        elif self._job.handle is not None:
            self._job.close()
        self._refresh_job_capability()

    @staticmethod
    def _owned_worker_still_exists(worker: Any) -> bool:
        return bool(
            getattr(worker, "is_alive", False)
            or getattr(worker, "process", None) is not None
        )

    def _reap_pending_startups(self) -> None:
        """Drop only cancelled bootstrap records whose exact thread is done."""

        for index, pending in tuple(self._pending_startups.items()):
            if pending.thread.is_alive() or not bool(pending.state.get("done")):
                continue
            # A normal completed bootstrap must remain visible until the
            # owner adopts it (including its error/READY state).  Only a
            # cancellation path may reap a completed worker that was never
            # admitted as a slot.
            if not pending.cancel_event.is_set():
                continue
            if self._owned_worker_still_exists(pending.worker):
                continue
            self._pending_startups.pop(index, None)

    def _start_worker_in_background(self, index: int, worker: Any) -> None:
        cancel_event = threading.Event()
        state: dict[str, Any] = {
            "done": False,
            "error": None,
            "background_ms": 0.0,
        }
        started_at = time.perf_counter()

        def bootstrap() -> None:
            started = time.perf_counter()
            try:
                if cancel_event.is_set():
                    return
                worker.start()
            except BaseException as exc:  # noqa: BLE001 - transfer to owner
                state["error"] = exc
            finally:
                state["background_ms"] = (time.perf_counter() - started) * 1000.0
                state["done"] = True
                if cancel_event.is_set():
                    force_close = getattr(worker, "force_close_nonblocking", None)
                    try:
                        if callable(force_close):
                            force_close()
                        else:
                            worker.close(graceful=False)
                    except Exception:
                        pass

        thread = threading.Thread(
            target=bootstrap,
            name=f"uv-gpt-startup-{int(index)}",
            daemon=True,
        )
        self._pending_startups[index] = _PendingStartup(
            slot_index=int(index),
            worker=worker,
            started_at=started_at,
            state=state,
            cancel_event=cancel_event,
            thread=thread,
        )
        thread.start()

    def _advance_pending_startups(self, *, deadline: Optional[float] = None) -> bool:
        """Adopt one completed bootstrap without waiting for its thread."""

        self._reap_pending_startups()
        for index, pending in tuple(sorted(self._pending_startups.items())):
            if deadline is not None and time.perf_counter() >= float(deadline):
                return False
            if pending.thread.is_alive() or not bool(pending.state.get("done")):
                return False
            self._pending_startups.pop(index, None)
            error = pending.state.get("error")
            if error is not None:
                self._detach_worker_for_cleanup(index, pending.worker)
                raise PoolHelperUnavailableError(
                    f"worker {index} bootstrap failed: {error}"
                ) from error
            if pending.cancel_event.is_set():
                force_close = getattr(pending.worker, "force_close_nonblocking", None)
                try:
                    if callable(force_close):
                        force_close()
                    else:
                        pending.worker.close(graceful=False)
                except Exception:
                    pass
                return False
            if not bool(getattr(pending.worker, "is_ready", False)):
                self._detach_worker_for_cleanup(index, pending.worker)
                raise PoolHelperUnavailableError(
                    f"worker {index} bootstrap did not become ready"
                )
            self._worker_start_background_ms += float(
                pending.state.get("background_ms", 0.0) or 0.0
            )
            self._startup_timings.append(
                (time.perf_counter() - pending.started_at) * 1000.0
            )
            self._assign_job_if_possible(pending.worker)
            self._slots.append(_Slot(index=int(index), worker=pending.worker))
            self._refresh_job_capability()
            self._finalize_startup_job()
            return True
        return False

    def _abort_pending_startups_nonblocking(self) -> None:
        """Signal every pending bootstrap and terminate only its exact worker."""

        for pending in tuple(self._pending_startups.values()):
            pending.cancel_event.set()
            force_close = getattr(pending.worker, "force_close_nonblocking", None)
            try:
                if callable(force_close):
                    force_close()
                else:
                    pending.worker.close(graceful=False)
            except Exception:
                pass

    def _abort_pending_startups(self) -> None:
        """Bounded compatibility cleanup for pending bootstrap operations."""

        self._abort_pending_startups_nonblocking()
        for index, pending in tuple(self._pending_startups.items()):
            pending.thread.join(timeout=0.05)
            if pending.thread.is_alive():
                continue
            force_close = getattr(pending.worker, "force_close_nonblocking", None)
            try:
                if callable(force_close):
                    force_close()
                else:
                    pending.worker.close(graceful=False)
            except Exception:
                pass
            self._pending_startups.pop(index, None)

    def start(self) -> "PersistentWorkerPool":
        if self._shutdown_complete:
            self._shutdown_state = "idle"
            self._shutdown_complete = False
            self._shutdown_started_at = 0.0
            self._shutdown_grace_deadline = 0.0
            self._shutdown_rounds = 0
            self._shutdown_force_used = False
            self._shutdown_slot_started.clear()
            self._shutdown_timings.clear()
        if self._slots and all(slot.worker.is_ready for slot in self._slots):
            return self
        if self._slots:
            self.close()
        while not self.start_incremental():
            # Synchronous compatibility is allowed to wait, but it still
            # drives the same nonblocking owner state machine.  The modal
            # caller never enters this loop.
            pending = tuple(self._pending_startups.values())
            if pending:
                pending[0].thread.join(timeout=0.002)
            else:
                time.sleep(0.001)
        return self

    def start_incremental(self, *, deadline: Optional[float] = None) -> bool:
        """Start at most one owned helper and report whether the pool is ready.

        Modal callers only schedule/adopt one exact bootstrap per advance.
        ``PersistentSingleWorker.start`` (including Popen and HELLO/READY)
        runs in the pending daemon operation, never in this owner method.
        """
        owner_started = time.perf_counter()
        try:
            if self._slots and len(self._slots) >= self.worker_count and all(
                slot.worker.is_ready for slot in self._slots
            ):
                return True
            self._prepare_startup_job()
            if self._pending_startups:
                self._advance_pending_startups(deadline=deadline)
                if len(self._slots) >= self.worker_count and all(
                    slot.worker.is_ready for slot in self._slots
                ):
                    return True
                # Do not launch a second bootstrap in the same owner advance
                # that adopted the prior slot.
                return False
            if deadline is not None and time.perf_counter() >= float(deadline):
                return False
            index = len(self._slots)
            worker = self._make_worker()
            self._start_worker_in_background(index, worker)
            return False
        except (ProcessRuntimeError, OSError, ValueError) as exc:
            self._close_workers(force=True)
            raise PoolHelperUnavailableError(
                f"could not start {self.worker_count} workers: {exc}"
            ) from exc
        finally:
            self._worker_start_owner_ms += (
                time.perf_counter() - owner_started
            ) * 1000.0

    def _poll_context_once(self, *, deadline: Optional[float] = None) -> None:
        """Consume bounded context ACKs without waiting for a full stage."""

        self._advance_pending_context_sends(deadline=deadline)
        if self._terminal:
            return
        for slot in tuple(self._slots):
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            active = self._context_active.get(slot.index)
            if active is None:
                continue
            # Avoid entering PersistentSingleWorker._send after an owned
            # helper has already exited; that legacy path performs a blocking
            # wait/reader join before raising.  The retry lifecycle below is
            # resumable and owns the detached cleanup.
            if not bool(getattr(slot.worker, "is_alive", True)):
                self._handle_context_failure(
                    slot,
                    active,
                    WorkerCrashedError("owned helper is not alive"),
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            try:
                message = slot.worker.poll(timeout=0.0)
            except Exception as exc:
                self._handle_context_failure(slot, active, exc, deadline=deadline)
                if self._terminal:
                    return
                continue
            if message is None:
                continue
            try:
                raw = slot.worker.consume_ticket_message(active.ticket)
            except Exception as exc:
                self._handle_context_failure(slot, active, exc, deadline=deadline)
                if self._terminal:
                    return
                continue
            if message.message_type.name == "ERROR":
                self._handle_context_failure(
                    slot,
                    active,
                    WorkerRemoteError(str(raw.payload)[:512]),
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            if message.message_type.name != "RESULT":
                self._handle_context_failure(
                    slot,
                    active,
                    PoolProtocolError("unexpected graph context response"),
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            try:
                result = GraphContextLoadResult.from_wire(raw.payload)
                result.validate_against(active.task)
            except Exception as exc:
                self._handle_context_failure(
                    slot,
                    active,
                    PoolProtocolError(f"invalid graph context ACK: {exc}"),
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            self._context_active.pop(slot.index, None)
            self._context_loaded.add(slot.index)
            self._context_load_acked += 1
            self._context_load_ms += float(result.load_ms)

    def load_graph_context(
        self,
        context: GraphContextPayload,
        *,
        deadline: Optional[float] = None,
    ) -> bool:
        """Incrementally load one immutable graph context into every worker.

        At most one worker receives the context per call.  The caller can call
        this from a modal tick and use ``graph_context_ready`` as the admission
        barrier before starting the typed shape/graph stream.
        """

        if not isinstance(context, GraphContextPayload):
            raise PoolProtocolError("graph context payload has an invalid type")
        if (
            context.identity.session_nonce != self.session_nonce
            or context.identity.generation != self.generation
        ):
            raise PoolProtocolError("graph context identity does not match pool")
        if (
            self._stream_mode
            or self._active
            or self._pending_admissions
            or self._ready_batches
            or self._undispatched
        ):
            if self._graph_context_payload is None or self.graph_context_digest != context.context_digest:
                raise PoolError("cannot replace graph context during an active stream")
        elif self._graph_context_payload is None or self.graph_context_digest != context.context_digest:
            if self._pending_context_sends:
                raise PoolError("cannot replace graph context while a context send is pending")
            # The actual protocol write performs the authoritative pickle,
            # digest and MAX_FRAME_BYTES guard.  Do not preflight-pickle the
            # immutable 12 MB context on every startup path.
            self._graph_context_payload = context
            self._context_active.clear()
            self._context_loaded.clear()
            self._context_retry_counts.clear()
            self._context_load_submitted = 0
            self._context_load_acked = 0
            self._context_load_frame_bytes = 0
            self._context_load_payload_bytes = 0
            self._context_load_ms = 0.0
            self._context_load_frame_max_bytes = 0
        # Same digest is the identity boundary; retain the resident copy.

        if self._cancelled:
            return False
        if not self._slots or not all(slot.worker.is_ready for slot in self._slots):
            return False
        self._poll_context_once(deadline=deadline)
        if self._terminal:
            return False
        if self.graph_context_ready:
            return True
        for slot in self._slots:
            if (
                slot.index in self._context_loaded
                or slot.index in self._context_active
                or slot.index in self._pending_context_sends
                or slot.index in self._pending_admissions
            ):
                continue
            self._start_context_on_slot(slot, deadline=deadline)
            break
        return self.graph_context_ready

    def _close_workers(self, *, force: bool, timeout: float = 2.0) -> None:
        self._abort_pending_admissions()
        self._abort_pending_context_sends()
        self._abort_pending_startups()
        # A pending background writer/bootstrap may still own the runtime
        # stream after the bounded compatibility joins above.  Keep its exact
        # worker on the detached cleanup path; a direct ``close`` here would
        # race the writer's encode/write/flush operation.
        deferred_workers: list[tuple[int, Any]] = []
        for pending in self._pending_admissions.values():
            if pending.thread.is_alive():
                slot = next(
                    (item for item in self._slots if item.index == pending.slot_index),
                    None,
                )
                if slot is not None:
                    deferred_workers.append((int(pending.slot_index), slot.worker))
        for pending in self._pending_context_sends.values():
            if pending.thread.is_alive():
                slot = next(
                    (item for item in self._slots if item.index == pending.slot_index),
                    None,
                )
                if slot is not None:
                    deferred_workers.append((int(pending.slot_index), slot.worker))
        for pending in self._pending_startups.values():
            if pending.thread.is_alive():
                deferred_workers.append((int(pending.slot_index), pending.worker))
        self._abort_pending_restarts()
        # ``_abort_pending_restarts`` intentionally clears its records before
        # waiting, so capture replacements before that call in future callers;
        # current restart cleanup is already detached by its failure path.
        deferred_worker_ids = {id(worker) for _index, worker in deferred_workers}
        for index, worker in deferred_workers:
            self._detach_worker_for_cleanup(index, worker)
        self._pending_admissions.clear()
        self._active.clear()
        self._context_active.clear()
        self._context_loaded.clear()
        self._context_replay_pending.clear()
        for slot in self._slots:
            started = time.perf_counter()
            try:
                if id(slot.worker) in deferred_worker_ids:
                    # The detached cleanup owns this exact handle.  Do not
                    # call close a second time from the pool owner.
                    pass
                elif force:
                    slot.worker.close(graceful=False)
                else:
                    slot.worker.shutdown(timeout=max(0.05, float(timeout)))
            except Exception:
                if id(slot.worker) not in deferred_worker_ids:
                    try:
                        slot.worker.close(graceful=False)
                    except Exception:
                        pass
            self._shutdown_timings.append((time.perf_counter() - started) * 1000.0)
            slot.active = None
        self._slots = []
        self._job.close()
        self._startup_job_prepared = False
        self._graph_context_payload = None
        self._context_retry_counts.clear()
        self._shutdown_state = "complete"
        self._shutdown_complete = True
        self._shutdown_force_used = bool(self._shutdown_force_used or force)

    def begin_shutdown(self, *, grace_timeout: float = 2.0) -> str:
        """Start owned-worker graceful shutdown without waiting.

        The semantic pool may already be terminal while its helper processes
        are still alive.  This method only emits one SHUTDOWN frame per slot;
        completion is advanced by :meth:`advance_shutdown` on later modal
        ticks.
        """

        if self._shutdown_complete:
            return self._shutdown_state
        if self._shutdown_state == "idle":
            self._shutdown_state = "begin"
            self._shutdown_started_at = time.perf_counter()
            self._shutdown_grace_deadline = (
                self._shutdown_started_at + max(0.01, float(grace_timeout))
            )
        self._abort_pending_startups_nonblocking()
        self._abort_pending_admissions_nonblocking()
        self._abort_pending_context_sends_nonblocking()
        for slot in tuple(self._slots):
            if (
                slot.index in self._pending_context_sends
                or slot.index in self._pending_admissions
            ):
                # The background context writer exclusively owns this slot;
                # a concurrent graceful SHUTDOWN frame would violate the
                # single-stream ordering guarantee.
                continue
            self._shutdown_slot_started.setdefault(slot.index, time.perf_counter())
            begin = getattr(slot.worker, "begin_shutdown", None)
            if not callable(begin):
                self._shutdown_force_used = True
                continue
            try:
                begin()
            except Exception:
                self._shutdown_force_used = True
        self._shutdown_state = "wait"
        return self._shutdown_state

    def _finish_shutdown_slot(self, slot: _Slot) -> None:
        started = self._shutdown_slot_started.pop(slot.index, None)
        if started is not None:
            self._shutdown_timings.append((time.perf_counter() - started) * 1000.0)
        slot.active = None

    def advance_shutdown(
        self,
        *,
        deadline: Optional[float] = None,
        grace_deadline: Optional[float] = None,
    ) -> str:
        """Poll owned worker shutdown once per slot under a caller deadline."""

        if self._shutdown_complete:
            return "complete"
        self._reap_worker_cleanups()
        self.begin_shutdown()
        self._shutdown_rounds += 1
        if grace_deadline is not None:
            self._shutdown_grace_deadline = min(
                self._shutdown_grace_deadline or float(grace_deadline),
                float(grace_deadline),
            )
        now = time.perf_counter()
        force = bool(
            self._shutdown_grace_deadline
            and now >= self._shutdown_grace_deadline
        )
        self._abort_pending_startups_nonblocking()
        self._abort_pending_admissions_nonblocking()
        self._abort_pending_context_sends_nonblocking()
        for slot in tuple(self._slots):
            if deadline is not None and time.perf_counter() >= float(deadline):
                break
            if (
                slot.index in self._pending_context_sends
                or slot.index in self._pending_admissions
            ):
                continue
            worker = slot.worker
            try:
                if force:
                    self._shutdown_force_used = True
                    advance = getattr(worker, "force_close_nonblocking", None)
                    if callable(advance):
                        state = advance()
                    else:
                        worker.close(graceful=False)
                        state = "complete"
                else:
                    advance = getattr(worker, "advance_shutdown", None)
                    if callable(advance):
                        state = advance()
                    else:
                        # Test doubles and old runtime shims do not expose
                        # the resumable API.  They are never allowed to make
                        # this path wait; force their exact owned handle.
                        self._shutdown_force_used = True
                        force_close = getattr(worker, "force_close_nonblocking", None)
                        if callable(force_close):
                            state = force_close()
                        else:
                            worker.close(graceful=False)
                            state = "complete"
            except Exception as exc:
                self._shutdown_force_used = True
                self._retry_failure_reason = str(exc)[:1024]
                force_close = getattr(worker, "force_close_nonblocking", None)
                try:
                    state = force_close() if callable(force_close) else "complete"
                except Exception:
                    state = "complete"
            if str(state) == "complete":
                self._finish_shutdown_slot(slot)
        # PersistentSingleWorker removes its exact identity when the process
        # is observed exited.  Keep pending slots until then; this avoids
        # falsely reporting zero orphans after a terminate.  Test doubles
        # without a ``process`` attribute are complete when ``is_alive`` is
        # false, so the same rule remains easy to exercise without a process.
        remaining = [
            slot
            for slot in self._slots
            if getattr(slot.worker, "is_alive", False)
            or getattr(slot.worker, "process", None) is not None
        ]
        cleanup_remaining = any(
            cleanup.thread.is_alive() or not bool(cleanup.state.get("done"))
            for cleanup in self._pending_cleanups.values()
        )
        pending_startup = any(
            pending.thread.is_alive()
            or self._owned_worker_still_exists(pending.worker)
            for pending in self._pending_startups.values()
        )
        pending_context = any(
            pending.thread.is_alive()
            for pending in self._pending_context_sends.values()
        )
        pending_admission = any(
            pending.thread.is_alive()
            for pending in self._pending_admissions.values()
        )
        if (
            not remaining
            and not cleanup_remaining
            and not pending_startup
            and not pending_context
            and not pending_admission
        ):
            for slot in tuple(self._slots):
                self._finish_shutdown_slot(slot)
            self._slots = []
            self._pending_startups.clear()
            self._pending_context_sends.clear()
            self._pending_admissions.clear()
            self._job.close()
            self._startup_job_prepared = False
            self._graph_context_payload = None
            self._context_retry_counts.clear()
            self._shutdown_state = "complete"
            self._shutdown_complete = True
            return "complete"
        if force:
            self._shutdown_state = "force"
        else:
            self._shutdown_state = "wait"
        return self._shutdown_state

    def _abort_pending_restarts(self) -> None:
        """Cancel background handshakes and close any replacement they own."""

        pending_values = tuple(self._pending_restarts.values())
        self._pending_restarts.clear()
        for pending in pending_values:
            pending.cancel_event.set()
            if pending.thread.is_alive():
                # The bootstrap thread is daemonized and also closes its
                # replacement after observing this event.  A short join keeps
                # normal cleanup deterministic without blocking a modal tick
                # on a slow handshake.
                pending.thread.join(timeout=0.05)
            if not pending.thread.is_alive():
                try:
                    pending.replacement.close(graceful=False)
                except Exception:
                    pass

    def _reap_worker_cleanups(self) -> None:
        """Drop only detached cleanup records whose thread has finished."""

        for key, cleanup in tuple(self._pending_cleanups.items()):
            if cleanup.thread.is_alive() or not bool(cleanup.state.get("done")):
                continue
            self._pending_cleanups.pop(key, None)

    def _detach_worker_for_cleanup(self, slot_index: int, worker: Any) -> None:
        """Detach one exact owned worker without waiting on its cleanup."""

        self._reap_worker_cleanups()
        key = "slot-%d-worker-%x" % (int(slot_index), id(worker))
        if key in self._pending_cleanups:
            return
        state: dict[str, Any] = {"done": False, "error": None}

        def cleanup() -> None:
            try:
                force_close = getattr(worker, "force_close_nonblocking", None)
                if callable(force_close):
                    # The runtime implementation is nonblocking; running it
                    # on this owned cleanup thread also protects the modal
                    # seam from older shims whose stream close is slow.
                    while True:
                        result = force_close()
                        if str(result) == "complete":
                            break
                        if not bool(
                            getattr(worker, "is_alive", False)
                        ) and getattr(worker, "process", None) is None:
                            break
                        time.sleep(0.01)
                else:
                    worker.close(graceful=False)
            except BaseException as exc:  # noqa: BLE001 - retained for owner report
                state["error"] = exc
            finally:
                state["done"] = True

        thread = threading.Thread(
            target=cleanup,
            name="uv-gpt-owned-cleanup-%d" % int(slot_index),
            daemon=True,
        )
        self._pending_cleanups[key] = _PendingWorkerCleanup(
            key=key,
            worker=worker,
            state=state,
            thread=thread,
        )
        thread.start()

    def _schedule_restart(
        self,
        slot: _Slot,
        task: Any,
        error: BaseException,
        *,
        is_context: bool,
    ) -> None:
        """Start one restart handshake asynchronously and retain its task.

        The immutable logical/context task is kept in ``_PendingRestart``;
        no batch is rebuilt and no modal caller waits for READY.  The next
        ``_poll_once`` advances the state when the bootstrap thread finishes.
        """

        retry_key = str(getattr(task, "batch_id", ""))
        retry_count = self._retry_counts.get(retry_key, 0)
        if retry_count >= 1:
            self._retry_failure_reason = (
                f"{('context=' if is_context else 'batch=')}{retry_key} "
                f"retry_attempt={retry_count + 1}: {error}"
            )[:1024]
            self._fail_operation(
                f"worker {slot.index} repeated failure for {retry_key}: {error}"
            )
            return
        self._retry_failure_reason = (
            f"{('context=' if is_context else 'batch=')}{retry_key} "
            f"retry_attempt={retry_count + 1}: {error}"
        )[:1024]
        self._context_active.pop(slot.index, None)
        self._context_loaded.discard(slot.index)
        slot.active = None
        # The failed worker is still owned, but its force/stream cleanup can
        # include waits in legacy runtime shims.  Detach it and let the owner
        # cleanup registry finish on a daemon thread; the immutable retry task
        # remains in this pool until the replacement handshake adopts it.
        self._detach_worker_for_cleanup(slot.index, slot.worker)
        try:
            replacement = self._make_worker()
        except Exception as exc:
            self._fail_operation(f"failed to create replacement worker: {exc}")
            return
        cancel_event = threading.Event()
        state: dict[str, Any] = {"done": False, "error": None}
        started_at = time.perf_counter()

        def bootstrap() -> None:
            try:
                if cancel_event.is_set():
                    return
                replacement.start()
            except BaseException as exc:  # noqa: BLE001 - transfer to owner thread
                state["error"] = exc
            finally:
                state["done"] = True
                if cancel_event.is_set():
                    try:
                        replacement.close(graceful=False)
                    except Exception:
                        pass

        thread = threading.Thread(
            target=bootstrap,
            name=f"uv-gpt-restart-{slot.index}",
            daemon=True,
        )
        pending = _PendingRestart(
            slot_index=slot.index,
            replacement=replacement,
            task=task,
            is_context=is_context,
            retry_key=retry_key,
            started_at=started_at,
            state=state,
            cancel_event=cancel_event,
            thread=thread,
        )
        self._pending_restarts[slot.index] = pending
        thread.start()

    def _advance_pending_restarts(self, *, deadline: Optional[float] = None) -> None:
        """Adopt completed handshakes without waiting inside this tick."""

        del deadline  # readiness is observed; dispatch remains deadline-aware
        self._reap_worker_cleanups()
        for index, pending in tuple(sorted(self._pending_restarts.items())):
            if not bool(pending.state.get("done")) or pending.thread.is_alive():
                continue
            self._pending_restarts.pop(index, None)
            error = pending.state.get("error")
            if error is not None:
                self._detach_worker_for_cleanup(index, pending.replacement)
                self._fail_operation(
                    f"worker {index} restart handshake failed: {error}"
                )
                return
            if not pending.replacement.is_ready:
                self._detach_worker_for_cleanup(index, pending.replacement)
                self._fail_operation(f"worker {index} restart did not become ready")
                return
            slot = next((item for item in self._slots if item.index == index), None)
            if slot is None:
                self._detach_worker_for_cleanup(index, pending.replacement)
                continue
            slot.worker = pending.replacement
            slot.active = None
            self._startup_timings.append(
                (time.perf_counter() - pending.started_at) * 1000.0
            )
            self._assign_job_if_possible(slot.worker)
            self._retry_counts[pending.retry_key] = 1
            if pending.is_context:
                self._context_replay_pending.add(index)
            else:
                # Retain the exact same task object, batch id and ordinals.
                self._ready_batches.appendleft(
                    _ReadyBatch(pending.task, avoid_slot=index)
                )

    def _advance_context_replays(self, *, deadline: Optional[float] = None) -> None:
        """Submit at most one retained context replay on a later advance."""

        for index in tuple(sorted(self._context_replay_pending)):
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            slot = next((item for item in self._slots if item.index == index), None)
            if (
                slot is None
                or index in self._pending_restarts
                or index in self._pending_context_sends
                or index in self._pending_admissions
            ):
                continue
            self._context_replay_pending.discard(index)
            self._start_context_on_slot(slot, deadline=deadline)
            return

    def _restart_slot(self, slot: _Slot) -> None:
        self._context_active.pop(slot.index, None)
        self._context_loaded.discard(slot.index)
        self._detach_worker_for_cleanup(slot.index, slot.worker)
        replacement = self._make_worker()
        try:
            replacement.start()
            self._assign_job_if_possible(replacement)
        except Exception as exc:
            self._detach_worker_for_cleanup(slot.index, replacement)
            raise PoolHelperUnavailableError("failed to restart crashed worker") from exc
        slot.worker = replacement
        slot.active = None

    def _context_batch_id(self, slot: _Slot) -> str:
        digest = self.graph_context_digest[:24] or "none"
        return "__graph-context-%s-%d" % (digest, int(slot.index))

    def _advance_pending_context_sends(self, *, deadline: Optional[float] = None) -> None:
        """Adopt one completed context encode/write without waiting on it."""

        for index, pending in tuple(sorted(self._pending_context_sends.items())):
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            if pending.thread.is_alive() or not bool(pending.state.get("done")):
                return
            self._pending_context_sends.pop(index, None)
            slot = next((item for item in self._slots if item.index == index), None)
            if pending.cancel_event.is_set() or self._cancelled:
                if slot is not None:
                    force_close_worker = getattr(
                        slot.worker, "force_close_nonblocking", None
                    )
                    try:
                        if callable(force_close_worker):
                            force_close_worker()
                        else:
                            slot.worker.close(graceful=False)
                    except Exception:
                        pass
                continue
            if slot is None:
                continue
            error = pending.state.get("error")
            if error is not None:
                active = _ActiveBatch(
                    pending.task,
                    pending.state.get("ticket"),
                    pending.started_at,
                )
                self._handle_context_failure(slot, active, error, deadline=deadline)
                return
            estimate = pending.state.get("estimate")
            ticket = pending.state.get("ticket")
            if estimate is None or ticket is None:
                self._handle_context_failure(
                    slot,
                    _ActiveBatch(pending.task, ticket, pending.started_at),
                    PoolProtocolError("context send completed without estimate/ticket"),
                    deadline=deadline,
                )
                return
            self._context_serialize_background_ms += float(
                pending.state.get("serialize_ms", 0.0) or 0.0
            )
            self._context_write_background_ms += float(
                pending.state.get("write_ms", 0.0) or 0.0
            )
            self._record_frame_estimate(pending.task, estimate)
            active = _ActiveBatch(pending.task, ticket, time.perf_counter())
            self._context_active[index] = active
            self._context_load_submitted += 1
            self._context_load_frame_bytes += int(estimate.frame_bytes)
            self._context_load_frame_max_bytes = max(
                self._context_load_frame_max_bytes,
                int(estimate.frame_bytes),
            )
            self._context_load_payload_bytes += int(estimate.payload_bytes)

    def _abort_pending_context_sends_nonblocking(self) -> None:
        """Cancel context encode/write operations and close only their slot."""

        for index, pending in tuple(self._pending_context_sends.items()):
            pending.cancel_event.set()
            slot = next((item for item in self._slots if item.index == index), None)
            if slot is None:
                continue
            force_close = getattr(slot.worker, "force_close_nonblocking", None)
            try:
                if callable(force_close):
                    force_close()
                else:
                    slot.worker.close(graceful=False)
            except Exception:
                pass

    def _abort_pending_context_sends(self) -> None:
        """Bounded compatibility cleanup for context sends."""

        self._abort_pending_context_sends_nonblocking()
        for index, pending in tuple(self._pending_context_sends.items()):
            pending.thread.join(timeout=0.05)
            if pending.thread.is_alive():
                continue
            self._pending_context_sends.pop(index, None)

    def _start_context_on_slot(
        self,
        slot: _Slot,
        *,
        deadline: Optional[float] = None,
    ) -> bool:
        """Submit one context load control task to an idle exact worker."""

        context = self._graph_context_payload
        if context is None:
            self._fail_operation("graph context was not registered before graph dispatch")
            return False
        if (
            slot.index in self._context_loaded
            or slot.index in self._context_active
            or slot.index in self._pending_context_sends
            or slot.index in self._pending_admissions
        ):
            return True
        if deadline is not None and time.perf_counter() >= float(deadline):
            return False
        owner_started = time.perf_counter()
        batch_id = self._context_batch_id(slot)
        task = GraphContextLoadTask(
            identity=context.identity,
            batch_id=batch_id,
            context=context,
        )
        self._context_serialize_owner_ms += (
            time.perf_counter() - owner_started
        ) * 1000.0
        if not bool(getattr(slot.worker, "is_alive", True)):
            active = _ActiveBatch(task, None, time.perf_counter())
            self._handle_context_failure(
                slot,
                active,
                WorkerCrashedError("owned helper is not alive"),
                deadline=deadline,
            )
            return False

        cancel_event = threading.Event()
        state: dict[str, Any] = {
            "done": False,
            "error": None,
            "estimate": None,
            "ticket": None,
            "serialize_ms": 0.0,
            "write_ms": 0.0,
        }

        def send_context() -> None:
            try:
                if cancel_event.is_set():
                    return
                started = time.perf_counter()
                estimate = task.estimate_frame()
                wire = task.to_wire()
                state["serialize_ms"] = (time.perf_counter() - started) * 1000.0
                state["estimate"] = estimate
                if cancel_event.is_set():
                    return
                write_started = time.perf_counter()
                state["ticket"] = slot.worker.submit(
                    wire,
                    batch_id=batch_id,
                    item_count=0,
                )
                state["write_ms"] = (time.perf_counter() - write_started) * 1000.0
            except BaseException as exc:  # noqa: BLE001 - transfer to owner
                state["error"] = exc
            finally:
                state["done"] = True
                if cancel_event.is_set():
                    force_close = getattr(slot.worker, "force_close_nonblocking", None)
                    try:
                        if callable(force_close):
                            force_close()
                        else:
                            slot.worker.close(graceful=False)
                    except Exception:
                        pass

        thread = threading.Thread(
            target=send_context,
            name=f"uv-gpt-context-write-{int(slot.index)}",
            daemon=True,
        )
        self._pending_context_sends[slot.index] = _PendingContextSend(
            slot_index=int(slot.index),
            task=task,
            started_at=time.perf_counter(),
            state=state,
            cancel_event=cancel_event,
            thread=thread,
        )
        thread.start()
        return True

    def _handle_context_failure(
        self,
        slot: _Slot,
        active: _ActiveBatch,
        error: BaseException,
        *,
        deadline: Optional[float] = None,
    ) -> None:
        del deadline  # a healthy retry remains pending across modal advances
        self._context_active.pop(slot.index, None)
        batch_id = str(active.task.batch_id)
        retry_count = self._retry_counts.get(batch_id, 0)
        if not self._retryable(error):
            self._retry_failure_reason = (
                f"context={batch_id} retry_attempt={retry_count + 1}: {error}"
            )[:1024]
            self._fail_operation(
                f"worker {slot.index} graph context failure: {error}"
            )
            return
        self._schedule_restart(slot, active.task, error, is_context=True)

    def _fill_ready(self, deadline: Optional[float] = None) -> None:
        # Bound the combined ready + in-flight set. At most N batches can be
        # in flight (one per worker), leaving at most N more queued when all
        # workers are busy.
        limit = max(
            0,
            2 * self.worker_count
            - len(self._active)
            - len(self._pending_admissions),
        )
        while len(self._ready_batches) < limit and self._undispatched:
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            self._ready_batches.append(_ReadyBatch(self._undispatched.popleft()))
        self._recompute_fused_affinity()

    def _recompute_fused_affinity(self) -> None:
        """Recompute advisory LPT ownership for the bounded ready window."""

        tasks = [item.task for item in self._ready_batches]
        tasks.extend(self._undispatched)
        fused = [
            task for task in tasks
            if str(getattr(task, "operation_kind", "")) == "fused"
        ]
        if not fused:
            return
        self._preferred_workers.update(
            plan_fused_affinity(fused, self.worker_count)
        )

    @staticmethod
    def _task_master_keys(task: Any) -> set[str]:
        if str(getattr(task, "operation_kind", "")) == "fused":
            value = _fused_task_master_key(task)
            return {value} if value else set()
        keys: set[str] = set()
        for pair in getattr(task, "pair_tasks", ()):
            graph = getattr(pair, "master_graph", None)
            if graph is not None:
                value = getattr(graph, "graph_key", None)
            else:
                value = getattr(pair, "master_key", None)
            if value is not None:
                keys.add(str(value))
        for value in getattr(task, "island_keys", ()):
            keys.add(str(value))
        return keys

    @classmethod
    def _task_master_key(cls, task: Any) -> Optional[str]:
        if str(getattr(task, "operation_kind", "")) == "fused":
            value = _fused_task_master_key(task)
            return value or None
        keys = cls._task_master_keys(task)
        return sorted(keys)[0] if keys else None

    @staticmethod
    def _estimate_task_frame(task: Any) -> Any:
        estimator = getattr(task, "estimate_frame", None)
        if callable(estimator):
            return estimator()
        return estimate_batch_frame(task)

    @staticmethod
    def _task_operation(task: Any) -> str:
        operation = str(getattr(task, "operation_kind", "") or "")
        if operation:
            return operation
        if isinstance(task, GraphContextLoadTask):
            return "graph_context"
        return "exact"

    def _record_frame_estimate(self, task: Any, estimate: Any) -> None:
        """Record one admitted message estimate, never a speculative build."""

        operation = self._task_operation(task)
        frame_bytes = int(getattr(estimate, "frame_bytes", 0))
        self._frame_bytes_total[operation] = (
            self._frame_bytes_total.get(operation, 0) + frame_bytes
        )
        self._frame_bytes_max[operation] = max(
            self._frame_bytes_max.get(operation, 0),
            frame_bytes,
        )

    @staticmethod
    def _task_debug_delay(task: Any) -> int:
        value = getattr(task, "debug_delay_ms", 0)
        return int(value)

    def _start_admission_on_slot(
        self,
        slot: _Slot,
        task: Any,
        *,
        deadline: Optional[float] = None,
    ) -> bool:
        """Schedule one task admission without doing protocol work on-owner.

        Validation, frame estimation, wire construction, and ``submit`` all
        ultimately perform arbitrary-size pickle/stream work.  The pending
        record owns the slot until that background operation completes, so a
        later owner advance cannot start a second writer on the same stream.
        """

        index = int(slot.index)
        if (
            index in self._pending_admissions
            or index in self._active
            or index in self._context_active
            or index in self._pending_context_sends
            or index in self._pending_restarts
        ):
            return False
        if deadline is not None and time.perf_counter() >= float(deadline):
            return False
        if not bool(getattr(slot.worker, "is_alive", True)):
            self._handle_failure(
                slot,
                _ActiveBatch(task, None, time.perf_counter()),
                WorkerCrashedError("owned helper is not alive"),
                deadline=deadline,
            )
            return False

        cancel_event = threading.Event()
        state: dict[str, Any] = {
            "done": False,
            "error": None,
            "estimate": None,
            "ticket": None,
            "validate_ms": 0.0,
            "serialize_ms": 0.0,
            "write_ms": 0.0,
        }
        started_at = time.perf_counter()

        def admit() -> None:
            try:
                if cancel_event.is_set():
                    return
                validator = getattr(task, "validate", None)
                if not callable(validator):
                    raise PoolProtocolError("stream task has no validator")
                started = time.perf_counter()
                validator()
                state["validate_ms"] = (time.perf_counter() - started) * 1000.0
                if cancel_event.is_set():
                    return

                started = time.perf_counter()
                estimate = self._estimate_task_frame(task)
                wire = task.to_wire()
                state["serialize_ms"] = (time.perf_counter() - started) * 1000.0
                state["estimate"] = estimate
                if cancel_event.is_set():
                    return
                if not bool(getattr(slot.worker, "is_alive", True)):
                    raise WorkerCrashedError("owned helper is not alive")

                started = time.perf_counter()
                state["ticket"] = slot.worker.submit(
                    wire,
                    batch_id=str(getattr(task, "batch_id", "")),
                    item_count=int(
                        getattr(task, "item_count", len(getattr(task, "pair_tasks", ())))
                    ),
                )
                state["write_ms"] = (time.perf_counter() - started) * 1000.0
            except BaseException as exc:  # noqa: BLE001 - transfer to owner
                state["error"] = exc
            finally:
                state["done"] = True
                if cancel_event.is_set():
                    force_close = getattr(slot.worker, "force_close_nonblocking", None)
                    try:
                        if callable(force_close):
                            force_close()
                        else:
                            slot.worker.close(graceful=False)
                    except Exception:
                        pass

        owner_started = time.perf_counter()
        thread = threading.Thread(
            target=admit,
            name=f"uv-gpt-admission-{index}",
            daemon=True,
        )
        self._pending_admissions[index] = _PendingAdmission(
            slot_index=index,
            task=task,
            started_at=started_at,
            state=state,
            cancel_event=cancel_event,
            thread=thread,
        )
        try:
            thread.start()
        except BaseException:
            self._pending_admissions.pop(index, None)
            cancel_event.set()
            raise
        finally:
            self._admission_owner_ms += (time.perf_counter() - owner_started) * 1000.0
        return True

    def _advance_pending_admissions(self, *, deadline: Optional[float] = None) -> None:
        """Adopt completed admissions in slot order without waiting."""

        for index, pending in tuple(sorted(self._pending_admissions.items())):
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            if pending.thread.is_alive() or not bool(pending.state.get("done")):
                continue
            self._pending_admissions.pop(index, None)
            slot = next((item for item in self._slots if item.index == index), None)
            if pending.cancel_event.is_set() or self._cancelled or self._terminal:
                if slot is not None:
                    force_close = getattr(slot.worker, "force_close_nonblocking", None)
                    try:
                        if callable(force_close):
                            force_close()
                        else:
                            slot.worker.close(graceful=False)
                    except Exception:
                        pass
                continue
            if slot is None:
                continue
            error = pending.state.get("error")
            if error is not None:
                self._handle_failure(
                    slot,
                    _ActiveBatch(pending.task, pending.state.get("ticket"), pending.started_at),
                    error,
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            estimate = pending.state.get("estimate")
            ticket = pending.state.get("ticket")
            if estimate is None or ticket is None:
                self._handle_failure(
                    slot,
                    _ActiveBatch(pending.task, ticket, pending.started_at),
                    PoolProtocolError("task admission completed without estimate/ticket"),
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            self._admission_validate_background_ms += float(
                pending.state.get("validate_ms", 0.0) or 0.0
            )
            self._admission_serialize_background_ms += float(
                pending.state.get("serialize_ms", 0.0) or 0.0
            )
            self._admission_write_background_ms += float(
                pending.state.get("write_ms", 0.0) or 0.0
            )
            self._task_frame_estimates.pop(str(pending.task.batch_id), None)
            self._record_frame_estimate(pending.task, estimate)
            active = _ActiveBatch(pending.task, ticket, time.perf_counter())
            slot.active = active
            self._active[index] = active
            slot.last_master = self._task_master_key(pending.task)
            self._dispatch_counts[index] = self._dispatch_counts.get(index, 0) + 1

    def _abort_pending_admissions_nonblocking(self) -> None:
        """Cancel task admission and close only the stream it owns."""

        for index, pending in tuple(self._pending_admissions.items()):
            pending.cancel_event.set()
            slot = next((item for item in self._slots if item.index == index), None)
            if slot is None:
                continue
            force_close = getattr(slot.worker, "force_close_nonblocking", None)
            try:
                if callable(force_close):
                    force_close()
                else:
                    slot.worker.close(graceful=False)
            except Exception:
                pass

    def _abort_pending_admissions(self) -> None:
        """Bounded compatibility cleanup for task admission operations."""

        self._abort_pending_admissions_nonblocking()
        for index, pending in tuple(self._pending_admissions.items()):
            pending.thread.join(timeout=0.05)
            if not pending.thread.is_alive():
                self._pending_admissions.pop(index, None)

    def _choose_slot(self, item: _ReadyBatch) -> Optional[_Slot]:
        idle = [
            slot for slot in self._slots
            if slot.index not in self._active
            and slot.index not in self._pending_admissions
            and slot.index not in self._context_active
            and slot.index not in self._pending_context_sends
            and slot.index not in self._pending_restarts
            and slot.worker.is_ready
        ]
        if not idle:
            return None
        if item.avoid_slot is not None:
            alternate = [slot for slot in idle if slot.index != item.avoid_slot]
            if alternate:
                idle = alternate
        operation = self._task_operation(item.task)
        preferred = self._preferred_workers.get(str(getattr(item.task, "batch_id", "")))
        if operation == "fused" and preferred is not None:
            preferred_slots = [slot for slot in idle if slot.index == preferred]
            if preferred_slots:
                return preferred_slots[0]
        # Non-fused work is deliberately dispatched by least assignment count;
        # persistent master affinity otherwise lets the first ready worker
        # monopolize a long shape frontier.
        return min(
            idle,
            key=lambda slot: (
                self._dispatch_counts.get(slot.index, 0),
                slot.completed_batches,
                slot.index,
            ),
        )

    def _dispatch(self, deadline: Optional[float] = None) -> None:
        while self._ready_batches:
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            item = self._ready_batches[0]
            slot = self._choose_slot(item)
            if slot is None:
                return
            requires_context = (
                getattr(item.task, "operation_kind", "") in {"graph", "resident_exact", "fused"}
                and bool(getattr(item.task, "context_digest", ""))
            )
            if requires_context:
                if (
                    self._graph_context_payload is None
                    or self.graph_context_digest != str(item.task.context_digest)
                ):
                    self._fail_operation(
                        "graph task context is not loaded for the current generation"
                    )
                    return
                if slot.index not in self._context_loaded:
                    self._start_context_on_slot(slot, deadline=deadline)
                    return
            self._ready_batches.popleft()
            if not bool(getattr(slot.worker, "is_alive", True)):
                self._handle_failure(
                    slot,
                    _ActiveBatch(item.task, None, time.perf_counter()),
                    WorkerCrashedError("owned helper is not alive"),
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            try:
                scheduled = self._start_admission_on_slot(
                    slot,
                    item.task,
                    deadline=deadline,
                )
            except Exception as exc:
                self._handle_failure(
                    slot,
                    _ActiveBatch(item.task, None, time.perf_counter()),
                    exc,
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            if not scheduled:
                self._ready_batches.appendleft(item)
                return
            # The background admission owns the stream until the next owner
            # advance adopts its ticket.  Returning here makes the owner
            # work per dispatch slice explicit and prevents a tight loop from
            # repeatedly inspecting the same not-yet-active frontier.
            return

    def begin(self, batches: tuple[Any, ...] | list[Any]) -> PoolProgress:
        if (
            self._active
            or self._context_active
            or self._pending_admissions
            or self._ready_batches
            or self._undispatched
        ):
            raise PoolError("pool already has an active operation")
        tasks = tuple(batches)
        self._stream_mode = False
        self._stream_closed = False
        self._stream_completions.clear()
        self._tasks = {}
        self._frame_bytes_max = {}
        self._frame_bytes_total = {}
        self._task_frame_estimates = {}
        self._admission_owner_ms = 0.0
        self._admission_validate_background_ms = 0.0
        self._admission_serialize_background_ms = 0.0
        self._admission_write_background_ms = 0.0
        self._preferred_workers.clear()
        all_ordinals: list[int] = []
        for task in tasks:
            task.validate()
            # Reject oversized frames before any worker sees the operation.
            estimate = self._estimate_task_frame(task)
            if task.identity.session_nonce != self.session_nonce or task.identity.generation != self.generation:
                raise PoolProtocolError("batch identity does not match pool generation/session")
            if task.batch_id in self._tasks:
                raise PoolProtocolError("duplicate batch id")
            self._tasks[task.batch_id] = task
            self._task_frame_estimates[str(task.batch_id)] = estimate
            all_ordinals.extend(task.pair_ordinals)
        if len(set(all_ordinals)) != len(all_ordinals):
            raise PoolProtocolError("duplicate pair ordinal across batches")
        self.start()
        self._ready_batches.clear()
        self._undispatched.clear()
        self._active.clear()
        self._result_buffer.clear()
        self._retry_counts.clear()
        self._retry_failure_reason = ""
        self._worker_distribution.clear()
        self._worker_operation_distribution.clear()
        self._dispatch_counts.clear()
        self._reset_nearest_metrics()
        self._cache_hits = 0
        self._pairs_total = len(all_ordinals)
        self._pairs_done = 0
        self._exact_count = 0
        self._batches_done = 0
        self._run_started_at = time.perf_counter()
        self._terminal = False
        self._cancelled = False
        self._generation_invalidated = False
        self._failure = ""
        self._graph_tasks_submitted = 0
        self._graph_tasks_completed = 0
        self._graph_items_total = 0
        self._graph_items_completed = 0
        self._graph_cache_hits = 0
        self._resident_topology_cache_builds = 0
        self._resident_topology_cache_hits = 0
        self._resident_topology_compute_ms = 0.0

        for task in sorted(
            tasks,
            key=lambda item: (
                tuple(getattr(item, "pair_ordinals", ()))[:1] or (10**18,),
                str(getattr(item, "batch_id", "")),
            ),
        ):
            cache_key = getattr(task, "cache_key", lambda: None)()
            cached = self._cache.get(cache_key) if self.use_cache and cache_key is not None and self._task_debug_delay(task) == 0 else None
            if cached is not None:
                try:
                    cached.validate_against(task)
                    self._task_frame_estimates.pop(str(task.batch_id), None)
                    self._record_result(task, cached, worker_index=-1)
                    self._cache_hits += 1
                    continue
                except PayloadValidationError:
                    pass
            self._undispatched.append(task)
        self._fill_ready()
        self._dispatch()
        self._finish_if_idle()
        return self.progress()

    def begin_stream(self) -> PoolProgress:
        """Start an incremental typed operation with bounded admission.

        Unlike ``begin``, a streaming caller admits only a small frontier at a
        time.  The pool therefore cannot retain the complete planner set in its
        queues.  Shape and exact tasks may share these persistent worker slots;
        the caller owns the canonical dependency and merge decisions.
        """

        if (
            self._active
            or self._context_active
            or self._pending_admissions
            or self._ready_batches
            or self._undispatched
        ):
            raise PoolError("pool already has an active operation")
        self.start()
        self._stream_mode = True
        self._stream_closed = False
        self._tasks = {}
        self._result_buffer.clear()
        self._stream_completions.clear()
        self._retry_counts.clear()
        self._retry_failure_reason = ""
        self._worker_distribution.clear()
        self._worker_operation_distribution.clear()
        self._dispatch_counts.clear()
        self._reset_nearest_metrics()
        self._frame_bytes_max = {}
        self._frame_bytes_total = {}
        self._task_frame_estimates = {}
        self._admission_owner_ms = 0.0
        self._admission_validate_background_ms = 0.0
        self._admission_serialize_background_ms = 0.0
        self._admission_write_background_ms = 0.0
        self._preferred_workers.clear()
        self._pairs_total = 0
        self._pairs_done = 0
        self._exact_count = 0
        self._batches_done = 0
        self._run_started_at = time.perf_counter()
        self._terminal = False
        self._cancelled = False
        self._generation_invalidated = False
        self._failure = ""
        self._graph_tasks_submitted = 0
        self._graph_tasks_completed = 0
        self._graph_items_total = 0
        self._graph_items_completed = 0
        self._graph_cache_hits = 0
        self._resident_topology_cache_builds = 0
        self._resident_topology_cache_hits = 0
        self._resident_topology_compute_ms = 0.0
        return self.progress()

    def stream_submit(
        self,
        tasks: Any,
        *,
        deadline: Optional[float] = None,
    ) -> tuple[str, ...]:
        """Admit typed shape/exact tasks while preserving the 2*N bound."""

        if not self._stream_mode or self._stream_closed:
            raise PoolError("stream operation is not accepting tasks")
        if self._terminal:
            raise PoolError(self._failure or "stream operation is terminal")
        if isinstance(tasks, (list, tuple)):
            items = tuple(tasks)
        else:
            items = (tasks,)
        if not items:
            return ()
        if len(items) > self.stream_capacity:
            raise PoolStreamBusyError(
                "stream admission exceeds bounded capacity: "
                f"requested={len(items)} capacity={self.stream_capacity}"
            )
        batch_ids: list[str] = []
        for task in items:
            if not callable(getattr(task, "validate", None)):
                raise PoolProtocolError("stream task has no validator")
            identity = getattr(task, "identity", None)
            if identity is None or identity.session_nonce != self.session_nonce or identity.generation != self.generation:
                raise PoolProtocolError("stream task identity does not match pool generation/session")
            batch_id = str(getattr(task, "batch_id", ""))
            if not batch_id or batch_id in self._tasks:
                raise PoolProtocolError("duplicate or empty stream batch id")
            self._tasks[batch_id] = task
            batch_ids.append(batch_id)
            self._undispatched.append(task)
            if getattr(task, "operation_kind", "exact") == "graph":
                self._graph_tasks_submitted += 1
                self._graph_items_total += int(getattr(task, "item_count", 0))
            else:
                self._pairs_total += len(getattr(task, "pair_tasks", ()))
        self._fill_ready(deadline)
        self._dispatch(deadline)
        return tuple(batch_ids)

    def stream_finish(self, *, deadline: Optional[float] = None) -> PoolProgress:
        """Declare that no more tasks will be admitted to the stream."""

        if not self._stream_mode:
            raise PoolError("stream operation has not started")
        self._stream_closed = True
        self._fill_ready(deadline)
        self._dispatch(deadline)
        self._finish_if_idle()
        return self.progress()

    def poll_stream(
        self,
        timeout: float = 0.0,
        *,
        deadline: Optional[float] = None,
    ) -> tuple[StreamCompletion, ...]:
        """Poll typed completions without waiting for the whole stream."""

        if not self._stream_mode:
            raise PoolError("stream operation has not started")
        wait_deadline = time.perf_counter() + max(0.0, float(timeout))
        while True:
            if deadline is not None and time.perf_counter() >= float(deadline):
                return ()
            self._poll_once(deadline=deadline)
            if self._stream_completions:
                completed = tuple(self._stream_completions)
                self._stream_completions.clear()
                return completed
            if self._terminal or timeout <= 0 or time.perf_counter() >= wait_deadline:
                return ()
            sleep_deadline = wait_deadline if deadline is None else min(wait_deadline, float(deadline))
            time.sleep(min(0.002, max(0.0, sleep_deadline - time.perf_counter())))

    def _record_result(
        self,
        task: Any,
        result: Any,
        *,
        worker_index: int,
        sequence: int = 0,
    ) -> None:
        result.validate_against(task)
        operation = str(getattr(task, "operation_kind", "exact"))
        is_graph = operation == "graph"
        is_fused = operation == "fused"
        result_items = (
            tuple(getattr(result, "outcomes", ()))
            if is_fused
            else tuple(getattr(result, "pair_results", ()))
        )
        if operation == "resident_exact":
            self._resident_topology_cache_builds += int(
                getattr(result, "topology_cache_builds", 0)
            )
            self._resident_topology_cache_hits += int(
                getattr(result, "topology_cache_hits", 0)
            )
            self._resident_topology_compute_ms += float(
                getattr(result, "topology_compute_ms", 0.0)
            )
        for item in result_items:
            self._record_nearest_metrics(
                getattr(item, "exact_result", None) if is_fused else item
            )
        if self._stream_mode:
            self._stream_completions.append(
                StreamCompletion(
                    task=task,
                    result=result,
                    worker_index=worker_index,
                    batch_id=str(task.batch_id),
                    sequence=int(sequence),
                )
            )
            if is_graph:
                self._graph_tasks_completed += 1
                self._graph_items_completed += int(getattr(task, "item_count", 0))
                self._graph_cache_hits += int(getattr(result, "cache_hits", 0))
            else:
                self._pairs_done += len(result_items)
                if is_fused:
                    self._exact_count += sum(
                        int(bool(getattr(item.exact_result, "accepted", False)))
                        for item in result_items
                        if getattr(item, "exact_result", None) is not None
                    )
                else:
                    self._exact_count += sum(
                        int(bool(getattr(pair_result, "accepted", False)))
                        for pair_result in result_items
                    )
            self._batches_done += 1
            if worker_index >= 0:
                completed_items = int(getattr(task, "item_count", 0)) if is_graph else len(result_items)
                self._worker_distribution[worker_index] = self._worker_distribution.get(worker_index, 0) + completed_items
                operation = str(getattr(task, "operation_kind", "exact"))
                operation_values = self._worker_operation_distribution.setdefault(worker_index, {})
                operation_values[operation] = operation_values.get(operation, 0) + completed_items
                slot = self._slots[worker_index]
                slot.completed_batches += 1
                slot.completed_pairs += completed_items
            if (not is_graph) and self.use_cache and self._task_debug_delay(task) == 0:
                self._cache.put(task, result)
            return
        if is_graph:
            self._graph_tasks_completed += 1
            self._graph_items_completed += int(getattr(task, "item_count", 0))
            self._graph_cache_hits += int(getattr(result, "cache_hits", 0))
            self._batches_done += 1
            return
        for pair_result in result_items:
            if pair_result.pair_ordinal in self._result_buffer:
                raise PoolProtocolError("duplicate result ordinal")
            self._result_buffer[pair_result.pair_ordinal] = pair_result
            self._pairs_done += 1
            self._exact_count += int(
                bool(
                    pair_result.exact_result.accepted
                    if is_fused and getattr(pair_result, "exact_result", None) is not None
                    else getattr(pair_result, "accepted", False)
                )
            )
        self._batches_done += 1
        if worker_index >= 0:
            self._worker_distribution[worker_index] = self._worker_distribution.get(worker_index, 0) + len(result_items)
            operation = str(getattr(task, "operation_kind", "exact"))
            operation_values = self._worker_operation_distribution.setdefault(worker_index, {})
            operation_values[operation] = operation_values.get(operation, 0) + len(result_items)
            slot = self._slots[worker_index]
            slot.completed_batches += 1
            slot.completed_pairs += len(result_items)
        if self.use_cache and self._task_debug_delay(task) == 0:
            self._cache.put(task, result)

    @staticmethod
    def _retryable(error: BaseException) -> bool:
        return isinstance(error, (WorkerCrashedError, WorkerEOFError, WorkerTimeoutError, WorkerRemoteError))

    def _handle_failure(
        self,
        slot: _Slot,
        active: _ActiveBatch,
        error: BaseException,
        *,
        deadline: Optional[float] = None,
    ) -> None:
        del deadline  # retry lifecycle is explicitly resumable across advances
        self._active.pop(slot.index, None)
        slot.active = None
        task = active.task
        if not self._retryable(error):
            self._retry_failure_reason = str(error)[:1024]
            self._fail_operation(f"worker {slot.index} protocol failure: {error}")
            return
        self._schedule_restart(slot, task, error, is_context=False)

    def _fail_operation(self, message: str) -> None:
        self._failure = str(message)[:1024]
        self._terminal = True
        self._ready_batches.clear()
        self._undispatched.clear()
        self._task_frame_estimates.clear()
        self._result_buffer.clear()
        self._stream_completions.clear()
        self._pairs_done = 0
        self._exact_count = 0
        self._close_workers(force=True)

    def _finish_if_idle(self) -> None:
        if self._terminal:
            return
        self._reap_worker_cleanups()
        if self._stream_mode and not self._stream_closed:
            return
        if (
            not self._active
            and not self._context_active
            and not self._pending_admissions
            and not self._pending_startups
            and not self._pending_context_sends
            and not self._pending_restarts
            and not self._pending_cleanups
            and not self._context_replay_pending
            and not self._ready_batches
            and not self._undispatched
        ):
            self._terminal = True

    def _poll_once(self, *, deadline: Optional[float] = None) -> None:
        if self._terminal:
            return
        self._advance_pending_restarts(deadline=deadline)
        if self._terminal:
            return
        self._poll_context_once(deadline=deadline)
        if self._terminal:
            return
        self._advance_context_replays(deadline=deadline)
        if self._terminal:
            return
        self._advance_pending_admissions(deadline=deadline)
        if self._terminal:
            return
        for slot in tuple(self._slots):
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            active = self._active.get(slot.index)
            if active is None:
                continue
            if not bool(getattr(slot.worker, "is_alive", True)):
                self._handle_failure(
                    slot,
                    active,
                    WorkerCrashedError("owned helper is not alive"),
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            try:
                message = slot.worker.poll(timeout=0.0)
            except Exception as exc:
                self._handle_failure(slot, active, exc, deadline=deadline)
                if self._terminal:
                    return
                continue
            if message is None:
                continue
            try:
                raw = slot.worker.consume_ticket_message(active.ticket)
            except Exception as exc:
                self._handle_failure(slot, active, exc, deadline=deadline)
                if self._terminal:
                    return
                continue
            if message.message_type.name == "ERROR":
                self._handle_failure(
                    slot,
                    active,
                    WorkerRemoteError(str(raw.payload)[:512]),
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            if message.message_type.name != "RESULT":
                self._handle_failure(
                    slot,
                    active,
                    PoolProtocolError("unexpected worker result message"),
                    deadline=deadline,
                )
                if self._terminal:
                    return
                continue
            try:
                decoder = getattr(active.task, "result_from_wire", None)
                result = decoder(raw.payload) if callable(decoder) else BatchResult.from_wire(raw.payload)
                self._record_result(
                    active.task,
                    result,
                    worker_index=slot.index,
                    sequence=active.ticket.sequence,
                )
            except Exception as exc:
                self._fail_operation(f"invalid batch result: {exc}")
                return
            self._active.pop(slot.index, None)
            slot.active = None
        self._fill_ready(deadline)
        self._dispatch(deadline)
        self._finish_if_idle()

    def poll(self, timeout: float = 0.0) -> PoolProgress:
        if not self._slots:
            raise PoolError("pool is not started")
        deadline = time.perf_counter() + max(0.0, float(timeout))
        while True:
            self._poll_once()
            if self._terminal or timeout <= 0 or time.perf_counter() >= deadline:
                return self.progress()
            time.sleep(min(0.002, max(0.0, deadline - time.perf_counter())))

    def drain(self, *, timeout: Optional[float] = None) -> PoolResult:
        if not self._slots:
            raise PoolError("pool is not started")
        deadline = None if timeout is None else time.perf_counter() + float(timeout)
        while not self._terminal:
            if deadline is not None and time.perf_counter() >= deadline:
                self._fail_operation("pool drain timeout")
                break
            wait = 0.05 if deadline is None else min(0.05, max(0.001, deadline - time.perf_counter()))
            self.poll(wait)
        return self.final_result()

    def run(self, batches: tuple[BatchTask, ...] | list[BatchTask], *, timeout: Optional[float] = None) -> PoolResult:
        self.begin(batches)
        return self.drain(timeout=timeout)

    def progress(self) -> PoolProgress:
        elapsed = 0.0 if not self._run_started_at else (time.perf_counter() - self._run_started_at) * 1000.0
        return PoolProgress(
            pairs_total=self._pairs_total,
            pairs_done=self._pairs_done,
            exact_count=self._exact_count,
            batches_total=len(self._tasks),
            batches_done=self._batches_done,
            active_workers=len(self._active),
            retry_count=self.retry_count,
            elapsed_ms=elapsed,
            cancelled=self._cancelled,
            failed=bool(self._failure),
            retry_total=self.retry_total,
            max_retry_per_batch=self.max_retry_per_batch,
            retried_batch_count=self.retried_batch_count,
            retry_failure_reason=self.retry_failure_reason,
            retry_batches=self.retry_batches,
            graph_tasks_submitted=self._graph_tasks_submitted,
            graph_tasks_completed=self._graph_tasks_completed,
            graph_items_total=self._graph_items_total,
            graph_items_completed=self._graph_items_completed,
            graph_cache_hits=self._graph_cache_hits,
            resident_topology_cache_builds=self._resident_topology_cache_builds,
            resident_topology_cache_hits=self._resident_topology_cache_hits,
            resident_topology_compute_ms=self._resident_topology_compute_ms,
            graph_context_ready=self.graph_context_ready,
            context_load_submitted=self._context_load_submitted,
            context_load_acked=self._context_load_acked,
            context_load_frame_bytes=self._context_load_frame_bytes,
            context_load_payload_bytes=self._context_load_payload_bytes,
            context_load_ms=self._context_load_ms,
            frame_bytes_max=self.frame_bytes_max_by_operation,
            frame_bytes_total=self.frame_bytes_total_by_operation,
            restart_pending=self.restart_pending,
            restart_states=self.restart_states,
            nearest_attempted=int(self._nearest_attempted),
            nearest_accepted=int(self._nearest_accepted),
            nearest_fallback=int(self._nearest_fallback),
            nearest_max_seed_distance=float(self._nearest_max_seed_distance),
            nearest_mean_seed_distance=float(self.nearest_mean_seed_distance),
            nearest_ambiguity_count=int(self._nearest_ambiguity_count),
            nearest_tie_count=int(self._nearest_tie_count),
            nearest_compute_ms=float(self._nearest_compute_ms),
            nearest_distance_evaluations=int(self._nearest_distance_evaluations),
            nearest_assignment_nodes=int(self._nearest_assignment_nodes),
            nearest_assignment_cap=int(self._nearest_assignment_cap),
            nearest_fallback_reasons=tuple(
                sorted(
                    (int(code), int(count))
                    for code, count in self._nearest_fallback_reasons.items()
                )
            ),
            nearest_distance_lookups=int(self._nearest_distance_lookups),
            nearest_distance_cache_hits=int(self._nearest_distance_cache_hits),
            nearest_distance_cache_misses=int(self._nearest_distance_cache_misses),
            nearest_operations_used=int(self._nearest_operations_used),
            graph_rejected_before_nearest=int(
                self._graph_rejected_before_nearest
            ),
            nearest_seed_missing=int(self._nearest_seed_missing),
            nearest_fast_miss=int(self._nearest_fast_miss),
            exact_fallback_calls=int(self._exact_fallback_calls),
            exact_primary_calls=int(self._exact_primary_calls),
            shutdown_state=self.shutdown_state,
            shutdown_rounds=self.shutdown_rounds,
            shutdown_force_used=self.shutdown_force_used,
            shutdown_complete=self.shutdown_complete,
            worker_start_owner_ms=self.worker_start_owner_ms,
            worker_start_background_ms=self.worker_start_background_ms,
            startup_pending=self.startup_pending,
            startup_states=self.startup_states,
            context_load_pending=self.context_load_pending,
            context_serialize_owner_ms=self.context_serialize_owner_ms,
            context_serialize_background_ms=self.context_serialize_background_ms,
            context_write_background_ms=self.context_write_background_ms,
            admission_pending=self.admission_pending,
            admission_states=self.admission_states,
            admission_owner_ms=self.admission_owner_ms,
            admission_validate_background_ms=self.admission_validate_background_ms,
            admission_serialize_background_ms=self.admission_serialize_background_ms,
            admission_write_background_ms=self.admission_write_background_ms,
        )

    def final_result(self) -> PoolResult:
        if not self._terminal:
            raise PoolError("pool operation is not terminal")
        complete = not self._failure and not self._cancelled and not self._generation_invalidated and self._pairs_done == self._pairs_total
        ordered = tuple(self._result_buffer[key] for key in sorted(self._result_buffer)) if complete else ()
        digest = stable_digest(
            tuple(pair_result_digest_wire(item) for item in ordered)
        ) if complete else ""
        return PoolResult(
            complete=complete,
            cancelled=self._cancelled,
            generation_invalidated=self._generation_invalidated,
            results=ordered,
            result_digest=digest,
            progress=self.progress(),
            failure=self._failure,
            worker_distribution=self.worker_task_distribution,
            worker_pids=tuple(sorted(self.worker_pids)),
        )

    def _abort_pending_restarts_nonblocking(self) -> None:
        """Signal owned restart handshakes without joining their threads."""

        self._abort_pending_startups_nonblocking()
        self._abort_pending_context_sends_nonblocking()
        self._abort_pending_admissions_nonblocking()
        for pending in tuple(self._pending_restarts.values()):
            pending.cancel_event.set()
            force_close = getattr(pending.replacement, "force_close_nonblocking", None)
            if callable(force_close):
                try:
                    force_close()
                except Exception:
                    pass

    def begin_cancel(self) -> str:
        """Begin owned cancellation without waiting for ACKs or processes.

        This is the modal cancellation counterpart to ``begin_shutdown``.
        It clears all semantic queues immediately, requests termination only
        through exact owned worker handles, and retains the slots until later
        ``advance_cancel`` calls observe exit.  The blocking ``cancel`` API
        below remains available to unregister/legacy callers.
        """

        if self._cancel_complete:
            return self._cancel_state
        if self._cancel_state == "idle":
            self._cancel_state = "begin"
            self._cancel_started_at = time.perf_counter()
            self._cancel_rounds = 0
        self._cancelled = True
        self._ready_batches.clear()
        self._undispatched.clear()
        self._active.clear()
        self._context_active.clear()
        self._context_loaded.clear()
        self._context_replay_pending.clear()
        self._result_buffer.clear()
        self._stream_completions.clear()
        self._pairs_done = 0
        self._exact_count = 0
        self._abort_pending_restarts_nonblocking()
        self._cancel_state = "force"
        for slot in tuple(self._slots):
            force_close = getattr(slot.worker, "force_close_nonblocking", None)
            if callable(force_close):
                try:
                    force_close()
                except Exception:
                    pass
        return self._cancel_state

    def advance_cancel(self, *, deadline: Optional[float] = None) -> str:
        """Advance exact-owned cancellation in a bounded, nonblocking slice."""

        if self._cancel_complete:
            return self._cancel_state
        self.begin_cancel()
        self._cancel_rounds += 1
        for pending_id, pending in tuple(self._pending_startups.items()):
            pending.cancel_event.set()
            if deadline is not None and time.perf_counter() >= float(deadline):
                break
            force_close = getattr(pending.worker, "force_close_nonblocking", None)
            if callable(force_close):
                try:
                    force_close()
                except Exception:
                    pass
            if (
                not pending.thread.is_alive()
                and not self._owned_worker_still_exists(pending.worker)
            ):
                self._pending_startups.pop(pending_id, None)
        for pending_id, pending in tuple(self._pending_context_sends.items()):
            pending.cancel_event.set()
            if deadline is not None and time.perf_counter() >= float(deadline):
                break
            slot = next(
                (item for item in self._slots if item.index == pending_id),
                None,
            )
            force_close = (
                None
                if slot is None
                else getattr(slot.worker, "force_close_nonblocking", None)
            )
            if callable(force_close):
                try:
                    force_close()
                except Exception:
                    pass
            if not pending.thread.is_alive():
                self._pending_context_sends.pop(pending_id, None)
        for pending_id, pending in tuple(self._pending_admissions.items()):
            pending.cancel_event.set()
            if deadline is not None and time.perf_counter() >= float(deadline):
                break
            slot = next(
                (item for item in self._slots if item.index == pending_id),
                None,
            )
            force_close = (
                None
                if slot is None
                else getattr(slot.worker, "force_close_nonblocking", None)
            )
            if callable(force_close):
                try:
                    force_close()
                except Exception:
                    pass
            if not pending.thread.is_alive():
                self._pending_admissions.pop(pending_id, None)
        for pending_id, pending in tuple(self._pending_restarts.items()):
            pending.cancel_event.set()
            if deadline is not None and time.perf_counter() >= float(deadline):
                break
            force_close = getattr(pending.replacement, "force_close_nonblocking", None)
            if callable(force_close):
                try:
                    force_close()
                except Exception:
                    pass
            if not pending.thread.is_alive():
                self._pending_restarts.pop(pending_id, None)
        for slot in tuple(self._slots):
            if deadline is not None and time.perf_counter() >= float(deadline):
                break
            force_close = getattr(slot.worker, "force_close_nonblocking", None)
            if callable(force_close):
                try:
                    force_close()
                except Exception:
                    pass
        pending_restarts = [
            pending
            for pending in self._pending_restarts.values()
            if pending.thread.is_alive()
            or getattr(pending.replacement, "is_alive", False)
            or getattr(pending.replacement, "process", None) is not None
        ]
        pending_startups = [
            pending
            for pending in self._pending_startups.values()
            if pending.thread.is_alive() or self._owned_worker_still_exists(pending.worker)
        ]
        pending_context = [
            pending
            for pending in self._pending_context_sends.values()
            if pending.thread.is_alive()
        ]
        pending_admission = [
            pending
            for pending in self._pending_admissions.values()
            if pending.thread.is_alive()
        ]
        remaining = [
            slot
            for slot in self._slots
            if getattr(slot.worker, "is_alive", False)
            or getattr(slot.worker, "process", None) is not None
        ]
        if (
            not pending_restarts
            and not pending_startups
            and not pending_context
            and not pending_admission
            and not remaining
        ):
            self._pending_restarts.clear()
            self._pending_startups.clear()
            self._pending_context_sends.clear()
            self._pending_admissions.clear()
            self._slots = []
            self._job.close()
            self._startup_job_prepared = False
            self._graph_context_payload = None
            self._context_retry_counts.clear()
            self._cancel_state = "complete"
            self._cancel_complete = True
            self._terminal = True
            self._shutdown_state = "cancelled"
            self._shutdown_complete = True
            self._shutdown_force_used = True
            return self._cancel_state
        return self._cancel_state

    def cancel(self, *, timeout: float = 1.0) -> PoolResult:
        """Blocking compatibility cancellation for unregister/legacy callers."""

        self.begin_cancel()
        deadline = time.perf_counter() + max(0.05, float(timeout))
        while not self._cancel_complete and time.perf_counter() < deadline:
            self.advance_cancel(deadline=time.perf_counter() + 0.01)
        if not self._cancel_complete:
            self._close_workers(force=True)
            self._cancel_state = "complete"
            self._cancel_complete = True
            self._terminal = True
        return self.final_result()

    def invalidate_generation(self, generation: int) -> PoolResult:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= self.generation:
            raise ValueError("generation must increase")
        result = self.cancel()
        self._generation_invalidated = True
        self.generation = generation
        self._cache.clear()
        self._terminal = True
        return PoolResult(
            complete=False,
            cancelled=True,
            generation_invalidated=True,
            results=(),
            result_digest="",
            progress=self.progress(),
            failure=result.failure,
            worker_distribution=result.worker_distribution,
            worker_pids=(),
        )

    def shutdown(self, *, timeout: float = 2.0) -> None:
        if not self._shutdown_complete:
            self.begin_shutdown(grace_timeout=timeout)
            deadline = time.perf_counter() + max(0.01, float(timeout))
            while not self._shutdown_complete:
                self.advance_shutdown(
                    deadline=time.perf_counter() + 0.01,
                    grace_deadline=deadline,
                )
                if time.perf_counter() >= deadline and not self._shutdown_complete:
                    self._shutdown_force_used = True
                    self.advance_shutdown(deadline=time.perf_counter() + 0.01)
                    if not self._shutdown_complete:
                        self._close_workers(force=True)
        self._ready_batches.clear()
        self._undispatched.clear()
        self._stream_completions.clear()
        self._stream_mode = False
        self._stream_closed = False
        self._terminal = True

    def close(self) -> None:
        self._close_workers(force=True)
        self._ready_batches.clear()
        self._undispatched.clear()
        self._stream_completions.clear()
        self._stream_mode = False
        self._stream_closed = False
        self._terminal = True

    def __enter__(self) -> "PersistentWorkerPool":
        self.start()
        return self

    def __exit__(self, _exc_type: object, _exc_value: object, _traceback: object) -> None:
        self.close()


__all__ = [
    "JobObjectCapability",
    "PoolProgress",
    "PoolResult",
    "PoolError",
    "PoolHelperUnavailableError",
    "PoolProtocolError",
    "PoolStreamBusyError",
    "PersistentWorkerPool",
    "StreamCompletion",
]
