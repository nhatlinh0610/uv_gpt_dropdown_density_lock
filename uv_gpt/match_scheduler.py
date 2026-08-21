"""Pure-numeric scheduling policy and bounded backend prototypes.

This module is intentionally independent from Blender.  Callers are expected
to extract immutable numeric descriptors on the Blender main thread and pass
those descriptors here.  No executor is retained between calls; a parallel
execution owns one short-lived executor and always shuts it down before
returning.

The policy is deliberately conservative:

* ``AUTO`` uses the effective full-fit count when one is available.  This is
  important for the MATCH-02 ``cc.blend`` case: 576 candidates were inspected
  but only one candidate reached the expensive full fit, so AUTO remains
  single-threaded.
* Pure-Python work defaults to single-threaded execution because Python loops
  are GIL-bound.  GIL-threading can be explicitly enabled for a benchmark, but
  the policy does not imply a speedup.
* Thread/process worker counts are bounded by
  ``min(8, logical_cpu_count, batch_size, policy.max_workers)``.
* The process path is a benchmark/prototype helper.  It accepts only
  immutable, pickleable numeric payloads and never touches Blender data.
  NumPy's own internal threading is not inspected or controlled; the API
  makes no oversubscription claim.

The public dataclasses are frozen so a decision/result can be safely passed to
diagnostic code without exposing scheduler state.  ``CancellationToken`` is
the deliberate exception: cancellation is mutable state owned by the caller.
"""

from concurrent.futures import FIRST_COMPLETED, Future, ProcessPoolExecutor, ThreadPoolExecutor, wait
from dataclasses import dataclass, fields, is_dataclass
import math
import numbers
import os
import pickle
import threading
import time
from typing import Any, Callable, Dict, Generic, Iterable, List, Optional, Sequence, Tuple, TypeVar


BACKEND_AUTO = "auto"
BACKEND_SINGLE = "single"
BACKEND_THREAD = "thread"
BACKEND_PROCESS = "process"
VALID_BACKENDS = frozenset(
    (BACKEND_AUTO, BACKEND_SINGLE, BACKEND_THREAD, BACKEND_PROCESS)
)

MAX_WORKERS = 8
DEFAULT_THREAD_MIN_BATCH_SIZE = 32
DEFAULT_PROCESS_MIN_BATCH_SIZE = 128
TIE_EPSILON = 1.0e-12

T = TypeVar("T")
R = TypeVar("R")


class ProcessContractError(TypeError):
    """Raised when the process prototype cannot honor its serialization contract."""


def _validate_non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("%s must be a non-negative integer" % name)
    if value < 0:
        raise ValueError("%s must be a non-negative integer" % name)
    return int(value)


def _validate_positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("%s must be a positive integer" % name)
    return int(value)


def _validate_backend(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("backend must be one of %s" % sorted(VALID_BACKENDS))
    normalized = value.strip().lower()
    if normalized not in VALID_BACKENDS:
        raise ValueError("backend must be one of %s" % sorted(VALID_BACKENDS))
    return normalized


def _validate_immutable(value: Any, *, path: str = "payload", depth: int = 0) -> None:
    """Validate the immutable scalar/tuple/frozen-dataclass payload contract.

    A descriptor may contain small immutable metadata strings and ``None`` in
    addition to numeric values.  Mutable containers, arbitrary objects, sets,
    and mappings are rejected because they make process serialization and
    deterministic ordering dependent on caller-owned state.
    """

    if depth > 100:
        raise TypeError("%s is too deeply nested" % path)
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, numbers.Real):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("%s contains a non-finite number" % path)
        return
    if isinstance(value, tuple):
        for index, item in enumerate(value):
            _validate_immutable(item, path="%s[%d]" % (path, index), depth=depth + 1)
        return
    if is_dataclass(value):
        params = getattr(type(value), "__dataclass_params__", None)
        if params is None or not getattr(params, "frozen", False):
            raise TypeError("%s must use a frozen dataclass" % path)
        for field in fields(value):
            _validate_immutable(
                getattr(value, field.name),
                path="%s.%s" % (path, field.name),
                depth=depth + 1,
            )
        return
    raise TypeError(
        "%s must contain only numbers, strings, None, tuples, or frozen dataclasses; got %s"
        % (path, type(value).__name__)
    )


def validate_numeric_payload(value: Any, *, path: str = "payload") -> None:
    """Validate one immutable numeric descriptor or result.

    The function returns ``None`` on success and raises ``TypeError`` or
    ``ValueError`` on contract violations.  Use :func:`is_valid_numeric_payload`
    when a boolean result is more convenient.
    """

    _validate_immutable(value, path=path)


def is_valid_numeric_payload(value: Any) -> bool:
    """Return whether ``value`` satisfies the immutable numeric payload contract."""

    try:
        validate_numeric_payload(value)
    except (TypeError, ValueError):
        return False
    return True


def _pickle_size(value: Any, *, path: str) -> int:
    try:
        return len(pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL))
    except Exception as exc:
        raise ProcessContractError(
            "%s is not pickleable: %s: %s" % (path, type(exc).__name__, exc)
        ) from exc


@dataclass(frozen=True)
class NumericTask(Generic[T]):
    """Immutable payload with a canonical input position and optional tie key."""

    index: int
    payload: T
    key: Any = None

    def __post_init__(self) -> None:
        _validate_non_negative_int(self.index, "index")
        validate_numeric_payload(self.payload, path="payload")
        if self.key is not None:
            validate_numeric_payload(self.key, path="key")


NumericWorkItem = NumericTask


@dataclass(frozen=True)
class SchedulerPolicy:
    """Immutable adaptive policy inputs and safety gates."""

    backend: str = BACKEND_AUTO
    thread_min_batch_size: int = DEFAULT_THREAD_MIN_BATCH_SIZE
    process_min_batch_size: int = DEFAULT_PROCESS_MIN_BATCH_SIZE
    max_workers: int = MAX_WORKERS
    logical_cpu_count: Optional[int] = None
    allow_gil_threads: bool = False
    # NumPy is eligible after the batch threshold, but the decision/result
    # explicitly carries "not_claimed" for internal NumPy/BLAS oversubscription.
    # Callers can disable the outer thread pool when their benchmark requires it.
    allow_numpy_threads: bool = True
    allow_process_benchmark: bool = False
    auto_process_benchmark: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend", _validate_backend(self.backend))
        object.__setattr__(
            self,
            "thread_min_batch_size",
            _validate_positive_int(self.thread_min_batch_size, "thread_min_batch_size"),
        )
        object.__setattr__(
            self,
            "process_min_batch_size",
            _validate_positive_int(self.process_min_batch_size, "process_min_batch_size"),
        )
        object.__setattr__(self, "max_workers", _validate_positive_int(self.max_workers, "max_workers"))
        if self.logical_cpu_count is not None:
            object.__setattr__(
                self,
                "logical_cpu_count",
                _validate_positive_int(self.logical_cpu_count, "logical_cpu_count"),
            )
        for name in (
            "allow_gil_threads",
            "allow_numpy_threads",
            "allow_process_benchmark",
            "auto_process_benchmark",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ValueError("%s must be a bool" % name)


DEFAULT_POLICY = SchedulerPolicy()


@dataclass(frozen=True)
class SchedulerRequest:
    """Immutable facts supplied to backend policy selection."""

    backend: str = BACKEND_AUTO
    batch_size: int = 0
    full_fit_count: Optional[int] = None
    pure_python: bool = True
    numpy_enabled: bool = False
    independent: bool = True
    payload_serializable: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend", _validate_backend(self.backend))
        object.__setattr__(self, "batch_size", _validate_non_negative_int(self.batch_size, "batch_size"))
        if self.full_fit_count is not None:
            count = _validate_non_negative_int(self.full_fit_count, "full_fit_count")
            if count > self.batch_size:
                raise ValueError("full_fit_count cannot exceed batch_size")
            object.__setattr__(self, "full_fit_count", count)
        for name in ("pure_python", "numpy_enabled", "independent", "payload_serializable"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError("%s must be a bool" % name)


@dataclass(frozen=True)
class SchedulerDecision:
    """Immutable backend choice with enough context to explain the choice."""

    requested_backend: str
    backend: str
    batch_size: int
    effective_batch_size: int
    full_fit_count: Optional[int]
    independent: bool
    pure_python: bool
    numpy_enabled: bool
    payload_serializable: bool
    logical_cpu_count: int
    worker_count: int
    thread_min_batch_size: int
    process_min_batch_size: int
    benchmark_only: bool
    reason: str
    numpy_oversubscription: str

    @property
    def is_parallel(self) -> bool:
        return self.backend in (BACKEND_THREAD, BACKEND_PROCESS)

    @property
    def chosen_backend(self) -> str:
        """Readable alias for consumers that distinguish requested/chosen state."""

        return self.backend

    @property
    def workers(self) -> int:
        """Readable alias for the bounded worker count."""

        return self.worker_count

    @property
    def effective_batch(self) -> int:
        return self.effective_batch_size

    @property
    def uses_threads(self) -> bool:
        return self.backend == BACKEND_THREAD

    @property
    def uses_processes(self) -> bool:
        return self.backend == BACKEND_PROCESS


def logical_cpu_count(override: Optional[int] = None) -> int:
    """Return a positive logical CPU count without assuming a host topology."""

    if override is not None:
        return _validate_positive_int(override, "logical_cpu_count")
    return max(1, int(os.cpu_count() or 1))


def worker_count_for(
    batch_size: int,
    *,
    logical_cpus: Optional[int] = None,
    max_workers: int = MAX_WORKERS,
) -> int:
    """Return the conservative parallel worker cap for a non-empty batch."""

    batch_size = _validate_non_negative_int(batch_size, "batch_size")
    max_workers = _validate_positive_int(max_workers, "max_workers")
    if batch_size == 0:
        return 0
    return min(MAX_WORKERS, max_workers, logical_cpu_count(logical_cpus), batch_size)


def choose_backend(
    request: Optional[SchedulerRequest] = None,
    *,
    policy: Optional[SchedulerPolicy] = None,
    backend: Optional[str] = None,
    batch_size: Optional[int] = None,
    full_fit_count: Optional[int] = None,
    pure_python: Optional[bool] = None,
    numpy_enabled: Optional[bool] = None,
    independent: Optional[bool] = None,
    payload_serializable: Optional[bool] = None,
) -> SchedulerDecision:
    """Choose a backend from immutable numeric workload facts.

    A request object is preferred for callers that want an auditable contract;
    keyword facts are provided as a small convenience for probes and tests.
    ``full_fit_count`` is the effective workload when present, so cheap
    candidates that never reach the expensive numeric fit do not trigger a
    parallel backend.
    """

    policy = policy or DEFAULT_POLICY
    if request is not None and any(
        value is not None
        for value in (
            backend,
            batch_size,
            full_fit_count,
            pure_python,
            numpy_enabled,
            independent,
            payload_serializable,
        )
    ):
        raise ValueError("pass either request or workload keyword facts, not both")
    if request is None:
        request = SchedulerRequest(
            backend=policy.backend if backend is None else backend,
            batch_size=0 if batch_size is None else batch_size,
            full_fit_count=full_fit_count,
            pure_python=True if pure_python is None else pure_python,
            numpy_enabled=False if numpy_enabled is None else numpy_enabled,
            independent=True if independent is None else independent,
            payload_serializable=(
                False if payload_serializable is None else payload_serializable
            ),
        )
    cpu_count = logical_cpu_count(policy.logical_cpu_count)
    effective_batch = (
        request.batch_size if request.full_fit_count is None else request.full_fit_count
    )
    parallel_workers = worker_count_for(
        request.batch_size,
        logical_cpus=cpu_count,
        max_workers=policy.max_workers,
    )
    single_workers = 0 if request.batch_size == 0 else 1
    numpy_note = "not_applicable"
    if request.numpy_enabled:
        numpy_note = "not_claimed"

    def decision(chosen: str, reason: str, *, benchmark_only: bool = False) -> SchedulerDecision:
        return SchedulerDecision(
            requested_backend=request.backend,
            backend=chosen,
            batch_size=request.batch_size,
            effective_batch_size=effective_batch,
            full_fit_count=request.full_fit_count,
            independent=request.independent,
            pure_python=request.pure_python,
            numpy_enabled=request.numpy_enabled,
            payload_serializable=request.payload_serializable,
            logical_cpu_count=cpu_count,
            worker_count=(parallel_workers if chosen in (BACKEND_THREAD, BACKEND_PROCESS) else single_workers),
            thread_min_batch_size=policy.thread_min_batch_size,
            process_min_batch_size=policy.process_min_batch_size,
            benchmark_only=benchmark_only,
            reason=reason,
            numpy_oversubscription=numpy_note,
        )

    if request.batch_size == 0:
        return decision(BACKEND_SINGLE, "empty_batch")
    if request.backend == BACKEND_SINGLE:
        return decision(BACKEND_SINGLE, "explicit_single")
    if not request.independent:
        return decision(BACKEND_SINGLE, "non_independent_batch")

    def thread_eligible() -> Tuple[bool, str]:
        if effective_batch < policy.thread_min_batch_size:
            return False, "effective_batch_below_thread_threshold"
        if request.pure_python and not policy.allow_gil_threads:
            return False, "pure_python_gil_default_single"
        if request.numpy_enabled and not policy.allow_numpy_threads:
            return False, "numpy_threading_disabled"
        return True, ""

    def process_eligible() -> Tuple[bool, str]:
        if not policy.allow_process_benchmark:
            return False, "process_benchmark_disabled"
        if effective_batch < policy.process_min_batch_size:
            return False, "effective_batch_below_process_threshold"
        if not request.payload_serializable:
            return False, "process_payload_not_serializable"
        return True, ""

    if request.backend == BACKEND_PROCESS:
        eligible, reason = process_eligible()
        if eligible:
            return decision(BACKEND_PROCESS, "explicit_process_benchmark", benchmark_only=True)
        return decision(BACKEND_SINGLE, reason)

    if request.backend == BACKEND_THREAD:
        eligible, reason = thread_eligible()
        if eligible:
            return decision(BACKEND_THREAD, "explicit_thread")
        return decision(BACKEND_SINGLE, reason)

    # AUTO never enters the process prototype unless a benchmark caller opts
    # into that behavior explicitly in the immutable policy.
    if policy.auto_process_benchmark:
        eligible, _reason = process_eligible()
        if eligible:
            return decision(BACKEND_PROCESS, "auto_process_benchmark", benchmark_only=True)
    eligible, reason = thread_eligible()
    if eligible:
        return decision(BACKEND_THREAD, "auto_thread_threshold_met")
    return decision(BACKEND_SINGLE, reason)


decide_backend = choose_backend
select_backend = choose_backend


class CancellationToken:
    """Thread-safe cancellation and generation state owned by the caller."""

    def __init__(self, generation: int = 0) -> None:
        self._generation = _validate_non_negative_int(generation, "generation")
        self._cancelled = threading.Event()
        self._lock = threading.Lock()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self) -> None:
        """Mark the current execution cancelled; this operation is idempotent."""

        self._cancelled.set()

    def advance(self, generation: int) -> None:
        """Move to a newer generation and reopen the token for that generation."""

        generation = _validate_non_negative_int(generation, "generation")
        with self._lock:
            if generation < self._generation:
                raise ValueError("generation cannot move backwards")
            self._generation = generation
            # Older executions become stale because their generation no longer
            # matches; the new generation starts uncancelled.
            self._cancelled.clear()

    def is_stale(self, expected_generation: Optional[int]) -> bool:
        return expected_generation is not None and self.generation != expected_generation


def _generation_is_stale(
    token: Optional[CancellationToken],
    generation: Optional[int],
    current_generation: Optional[Any],
) -> bool:
    if token is not None and token.is_stale(generation):
        return True
    if generation is None or current_generation is None:
        return False
    current = current_generation() if callable(current_generation) else current_generation
    return current != generation


def _stop_reason(
    token: Optional[CancellationToken],
    generation: Optional[int],
    current_generation: Optional[Any],
) -> Optional[str]:
    if token is not None and token.cancelled:
        return "cancelled"
    if _generation_is_stale(token, generation, current_generation):
        return "stale_generation"
    return None


@dataclass(frozen=True)
class TaskResult(Generic[R]):
    """One result slot, retained even when cancellation leaves it incomplete."""

    index: int
    key: Any
    status: str
    value: Optional[R] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class SchedulerDiagnostics:
    """Immutable timing and lifecycle evidence for one batch execution."""

    backend: str
    requested_backend: str
    input_count: int
    submitted_count: int
    completed_count: int
    failed_count: int
    cancelled_count: int
    stale_count: int
    worker_count: int
    elapsed_seconds: float
    execution_seconds: float
    serialization_seconds: float
    executor_created: bool
    executor_shutdown: bool
    ordering_preserved: bool
    cancellation_requested: bool
    stale_generation: bool
    generation: Optional[int]
    process_benchmark_only: bool
    numpy_oversubscription_claimed: bool
    first_error: Optional[str] = None


@dataclass(frozen=True)
class SchedulerResult(Generic[R]):
    """Immutable ordered task records plus diagnostics.

    ``results`` is always ordered by input position, regardless of completion
    order.  ``values`` is the ordered tuple of successfully completed values;
    use ``results`` when cancellation/partial completion needs to be visible.
    """

    results: Tuple[TaskResult[R], ...]
    decision: SchedulerDecision
    diagnostics: SchedulerDiagnostics

    @property
    def entries(self) -> Tuple[TaskResult[R], ...]:
        return self.results

    @property
    def values(self) -> Tuple[R, ...]:
        return tuple(entry.value for entry in self.results if entry.status == "completed")

    @property
    def ordered_values(self) -> Tuple[R, ...]:
        """Alias for ``values`` for callers that emphasize ordering."""

        return self.values

    @property
    def completed(self) -> bool:
        return all(entry.status == "completed" for entry in self.results)


@dataclass(frozen=True)
class ProcessSerializationReport:
    """Evidence that process inputs satisfy the immutable pickle contract."""

    item_count: int
    payload_bytes: int
    worker_bytes: int
    pickle_protocol: int
    valid: bool = True


def _normalize_tasks(items: Iterable[Any]) -> Tuple[NumericTask[Any], ...]:
    normalized: List[NumericTask[Any]] = []
    for position, item in enumerate(tuple(items)):
        if isinstance(item, NumericTask):
            key = position if item.key is None else item.key
            normalized.append(NumericTask(position, item.payload, key))
        else:
            normalized.append(NumericTask(position, item, position))
    return tuple(normalized)


def validate_process_contract(
    items: Iterable[Any],
    worker: Callable[[Any], Any],
) -> ProcessSerializationReport:
    """Validate and measure process payload/worker serialization.

    The iterable is materialized only for validation; the caller may pass the
    same tuple/list to :func:`run_process_benchmark` afterward.  No Blender
    object can pass the immutable payload validator, even if it happens to be
    technically pickleable.
    """

    if not callable(worker):
        raise ProcessContractError("worker must be callable")
    try:
        tasks = _normalize_tasks(items)
    except (TypeError, ValueError) as exc:
        raise ProcessContractError("process payload validation failed: %s" % exc) from exc
    worker_bytes = _pickle_size(worker, path="worker")
    payload_bytes = sum(
        _pickle_size(task.payload, path="payload[%d]" % task.index) for task in tasks
    )
    return ProcessSerializationReport(
        item_count=len(tasks),
        payload_bytes=payload_bytes,
        worker_bytes=worker_bytes,
        pickle_protocol=pickle.HIGHEST_PROTOCOL,
    )


def is_process_serializable(payload: Any, worker: Optional[Callable[[Any], Any]] = None) -> bool:
    """Return whether a payload, and optionally a worker, meet process rules."""

    try:
        validate_numeric_payload(payload)
        _pickle_size(payload, path="payload")
        if worker is not None:
            if not callable(worker):
                return False
            _pickle_size(worker, path="worker")
    except (TypeError, ValueError, ProcessContractError):
        return False
    return True


def _stable_sort_key(value: Any) -> Tuple[Any, ...]:
    """Build a comparable key for the allowed immutable tie-key values."""

    if value is None:
        return (0,)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, numbers.Real):
        return (2, float(value))
    if isinstance(value, str):
        return (3, value)
    if isinstance(value, tuple):
        return (4, tuple(_stable_sort_key(item) for item in value))
    if is_dataclass(value):
        return (
            5,
            type(value).__module__,
            type(value).__qualname__,
            tuple(_stable_sort_key(getattr(value, field.name)) for field in fields(value)),
        )
    # This path is defensive; NumericTask validates keys before they reach it.
    return (6, type(value).__module__, type(value).__qualname__, repr(value))


@dataclass(frozen=True)
class BestSelection(Generic[R]):
    """Deterministic winning value and the exact tie-break fields used."""

    index: int
    key: Any
    score: float
    value: R


def select_best(
    values: Sequence[R],
    *,
    score_key: Callable[[R], float] = lambda value: float(value),
    tie_key: Optional[Callable[[R], Any]] = None,
    tie_epsilon: float = TIE_EPSILON,
) -> Optional[BestSelection[R]]:
    """Select the lowest score with deterministic key-then-input-order ties."""

    if tie_epsilon < 0 or not math.isfinite(float(tie_epsilon)):
        raise ValueError("tie_epsilon must be finite and non-negative")
    best: Optional[BestSelection[R]] = None
    best_sort_key: Optional[Tuple[Any, ...]] = None
    for index, value in enumerate(values):
        score = float(score_key(value))
        if not math.isfinite(score):
            continue
        key = index if tie_key is None else tie_key(value)
        validate_numeric_payload(key, path="tie_key")
        candidate_key = _stable_sort_key(key)
        if best is None:
            best = BestSelection(index, key, score, value)
            best_sort_key = candidate_key
            continue
        assert best_sort_key is not None
        if score < best.score - tie_epsilon:
            best = BestSelection(index, key, score, value)
            best_sort_key = candidate_key
        elif abs(score - best.score) <= tie_epsilon and (
            candidate_key < best_sort_key
            or (candidate_key == best_sort_key and index < best.index)
        ):
            best = BestSelection(index, key, score, value)
            best_sort_key = candidate_key
    return best


deterministic_best = select_best
select_best_result = select_best


def _safe_error(exc: BaseException) -> str:
    return "%s: %s" % (type(exc).__name__, exc)


def _shutdown_executor(executor: Any) -> bool:
    try:
        executor.shutdown(wait=True, cancel_futures=True)
    except TypeError:  # pragma: no cover - Python < 3.9 compatibility.
        executor.shutdown(wait=True)
    return True


def _build_result(
    tasks: Tuple[NumericTask[Any], ...],
    slots: List[Optional[TaskResult[Any]]],
    decision: SchedulerDecision,
    *,
    submitted_count: int,
    started_at: float,
    execution_started_at: float,
    serialization_seconds: float,
    executor_created: bool,
    executor_shutdown: bool,
    cancellation_requested: bool,
    stale_generation: bool,
    generation: Optional[int],
    first_error: Optional[str],
) -> SchedulerResult[Any]:
    if stale_generation:
        # A stale generation must never leak a completed value to the caller:
        # the caller may otherwise apply a result from an older UV snapshot.
        for position, task in enumerate(tasks):
            slots[position] = TaskResult(
                task.index,
                task.key,
                "stale",
                error="stale_generation",
            )
    else:
        for position, task in enumerate(tasks):
            if slots[position] is None:
                slots[position] = TaskResult(task.index, task.key, "cancelled", error="cancelled")
    results = tuple(slot for slot in slots if slot is not None)
    # The slots are filled for every task above; the assertion also documents
    # the ordering invariant for type checkers and future maintainers.
    if len(results) != len(tasks):
        raise RuntimeError("scheduler result slots are incomplete")
    completed = sum(1 for item in results if item.status == "completed")
    failed = sum(1 for item in results if item.status == "failed")
    cancelled = sum(1 for item in results if item.status == "cancelled")
    stale = sum(1 for item in results if item.status == "stale")
    elapsed = time.perf_counter() - started_at
    execution_seconds = max(0.0, time.perf_counter() - execution_started_at)
    ordering = tuple(item.index for item in results) == tuple(task.index for task in tasks)
    diagnostics = SchedulerDiagnostics(
        backend=decision.backend,
        requested_backend=decision.requested_backend,
        input_count=len(tasks),
        submitted_count=submitted_count,
        completed_count=completed,
        failed_count=failed,
        cancelled_count=cancelled,
        stale_count=stale,
        worker_count=decision.worker_count,
        elapsed_seconds=elapsed,
        execution_seconds=execution_seconds,
        serialization_seconds=serialization_seconds,
        executor_created=executor_created,
        executor_shutdown=executor_shutdown,
        ordering_preserved=ordering,
        cancellation_requested=cancellation_requested,
        stale_generation=stale_generation,
        generation=generation,
        process_benchmark_only=decision.benchmark_only,
        numpy_oversubscription_claimed=False,
        first_error=first_error,
    )
    return SchedulerResult(results=results, decision=decision, diagnostics=diagnostics)


def _schedule_tasks(
    tasks: Tuple[NumericTask[Any], ...],
    worker: Callable[[Any], Any],
    *,
    policy: SchedulerPolicy,
    backend: Optional[str],
    full_fit_count: Optional[int],
    pure_python: bool,
    numpy_enabled: bool,
    independent: bool,
    token: Optional[CancellationToken],
    generation: Optional[int],
    current_generation: Optional[Any],
    validate_results: bool,
    raise_on_error: bool,
) -> SchedulerResult[Any]:
    started_at = time.perf_counter()
    requested_backend = _validate_backend(policy.backend if backend is None else backend)
    serialization_started = time.perf_counter()
    requires_process_contract = (
        requested_backend == BACKEND_PROCESS and policy.allow_process_benchmark
    ) or (requested_backend == BACKEND_AUTO and policy.auto_process_benchmark)
    payload_serializable = False
    first_exception: Optional[BaseException] = None
    if requires_process_contract:
        try:
            validate_process_contract(tasks, worker)
            payload_serializable = True
        except ProcessContractError as exc:
            first_exception = exc
    serialization_seconds = time.perf_counter() - serialization_started
    request = SchedulerRequest(
        backend=requested_backend,
        batch_size=len(tasks),
        full_fit_count=full_fit_count,
        pure_python=pure_python,
        numpy_enabled=numpy_enabled,
        independent=independent,
        payload_serializable=payload_serializable,
    )
    decision = choose_backend(request, policy=policy)
    slots: List[Optional[TaskResult[Any]]] = [None] * len(tasks)
    submitted_count = 0
    executor_created = False
    executor_shutdown = False
    execution_started_at = time.perf_counter()
    cancellation_requested = False
    stale_generation = False
    first_error = _safe_error(first_exception) if first_exception is not None else None

    def mark_stop(position: int, reason: str) -> None:
        nonlocal cancellation_requested, stale_generation
        if reason == "stale_generation":
            stale_generation = True
        else:
            cancellation_requested = True
        status = "stale" if reason == "stale_generation" else "cancelled"
        for index in range(position, len(tasks)):
            if slots[index] is None:
                task = tasks[index]
                slots[index] = TaskResult(task.index, task.key, status, error=reason)

    def store_value(task: NumericTask[Any], value: Any) -> None:
        nonlocal first_error
        try:
            if validate_results:
                validate_numeric_payload(value, path="result[%d]" % task.index)
        except (TypeError, ValueError) as exc:
            message = _safe_error(exc)
            slots[task.index] = TaskResult(task.index, task.key, "failed", error=message)
            if first_error is None:
                first_error = message
            return
        slots[task.index] = TaskResult(task.index, task.key, "completed", value=value)

    def store_failure(task: NumericTask[Any], exc: BaseException) -> None:
        nonlocal first_error
        message = _safe_error(exc)
        slots[task.index] = TaskResult(task.index, task.key, "failed", error=message)
        if first_error is None:
            first_error = message

    if decision.backend == BACKEND_SINGLE:
        submitted_count = len(tasks)
        for position, task in enumerate(tasks):
            reason = _stop_reason(token, generation, current_generation)
            if reason is not None:
                mark_stop(position, reason)
                break
            try:
                value = worker(task.payload)
            except Exception as exc:  # preserve diagnostics for worker failures.
                store_failure(task, exc)
                continue
            if _stop_reason(token, generation, current_generation) is not None:
                reason = _stop_reason(token, generation, current_generation) or "cancelled"
                mark_stop(position, reason)
                break
            store_value(task, value)
    elif decision.backend in (BACKEND_THREAD, BACKEND_PROCESS):
        executor_type = ThreadPoolExecutor if decision.backend == BACKEND_THREAD else ProcessPoolExecutor
        executor = executor_type(max_workers=decision.worker_count)
        executor_created = True
        pending: Dict[Future[Any], NumericTask[Any]] = {}
        task_iterator = iter(tasks)
        try:
            def submit_available() -> None:
                nonlocal submitted_count
                # Keep the queue bounded so cancellation does not enqueue an
                # unbounded stale generation.  Input ordering is retained by
                # the task index, not by completion order.
                limit = max(1, decision.worker_count * 2)
                while len(pending) < limit:
                    reason = _stop_reason(token, generation, current_generation)
                    if reason is not None:
                        mark_stop(0, reason)
                        return
                    try:
                        task = next(task_iterator)
                    except StopIteration:
                        return
                    pending[executor.submit(worker, task.payload)] = task
                    submitted_count += 1

            submit_available()
            while pending:
                done, _not_done = wait(tuple(pending), return_when=FIRST_COMPLETED)
                done_items = sorted(
                    ((pending[future], future) for future in done),
                    key=lambda item: item[0].index,
                )
                for task, future in done_items:
                    pending.pop(future, None)
                    reason = _stop_reason(token, generation, current_generation)
                    if reason is not None:
                        mark_stop(0, reason)
                        continue
                    try:
                        value = future.result()
                    except Exception as exc:
                        store_failure(task, exc)
                    else:
                        store_value(task, value)
                if _stop_reason(token, generation, current_generation) is not None:
                    reason = _stop_reason(token, generation, current_generation) or "cancelled"
                    mark_stop(0, reason)
                    for future in pending:
                        future.cancel()
                    pending.clear()
                    break
                submit_available()
        finally:
            for future in pending:
                future.cancel()
            pending.clear()
            executor_shutdown = _shutdown_executor(executor)
    else:  # pragma: no cover - choose_backend constrains this branch.
        raise RuntimeError("unsupported scheduler backend: %s" % decision.backend)

    if _generation_is_stale(token, generation, current_generation):
        stale_generation = True
    if token is not None and token.cancelled:
        cancellation_requested = True
    result = _build_result(
        tasks,
        slots,
        decision,
        submitted_count=submitted_count,
        started_at=started_at,
        execution_started_at=execution_started_at,
        serialization_seconds=serialization_seconds,
        executor_created=executor_created,
        executor_shutdown=executor_shutdown,
        cancellation_requested=cancellation_requested,
        stale_generation=stale_generation,
        generation=generation,
        first_error=first_error,
    )
    if raise_on_error and first_exception is not None:
        raise first_exception
    if raise_on_error:
        for entry in result.results:
            if entry.status == "failed":
                raise RuntimeError(entry.error or "numeric worker failed")
    return result


def schedule_numeric_batch(
    items: Iterable[Any],
    worker: Callable[[Any], R],
    *,
    policy: Optional[SchedulerPolicy] = None,
    backend: Optional[str] = None,
    full_fit_count: Optional[int] = None,
    pure_python: bool = True,
    numpy_enabled: bool = False,
    independent: bool = True,
    token: Optional[CancellationToken] = None,
    generation: Optional[int] = None,
    current_generation: Optional[Any] = None,
    validate_results: bool = True,
    raise_on_error: bool = False,
) -> SchedulerResult[R]:
    """Execute an immutable numeric batch using the selected safe policy path."""

    if not callable(worker):
        raise TypeError("worker must be callable")
    if generation is not None:
        _validate_non_negative_int(generation, "generation")
    tasks = _normalize_tasks(items)
    return _schedule_tasks(
        tasks,
        worker,
        policy=policy or DEFAULT_POLICY,
        backend=backend,
        full_fit_count=full_fit_count,
        pure_python=pure_python,
        numpy_enabled=numpy_enabled,
        independent=independent,
        token=token,
        generation=generation,
        current_generation=current_generation,
        validate_results=validate_results,
        raise_on_error=raise_on_error,
    )


run_numeric_batch = schedule_numeric_batch
schedule_batch = schedule_numeric_batch


def run_process_benchmark(
    items: Iterable[Any],
    worker: Callable[[Any], R],
    *,
    policy: Optional[SchedulerPolicy] = None,
    full_fit_count: Optional[int] = None,
    token: Optional[CancellationToken] = None,
    generation: Optional[int] = None,
    current_generation: Optional[Any] = None,
    validate_results: bool = True,
    raise_on_error: bool = False,
    force_process: bool = True,
) -> SchedulerResult[R]:
    """Run the explicitly opt-in process prototype and prove cleanup.

    ``force_process=True`` is useful for small benchmark probes where the
    adaptive process threshold would otherwise correctly fall back to single.
    Production/operator code should leave the default AUTO path untouched and
    should not call this helper unless its Blender-data isolation and main
    thread apply boundary have been separately demonstrated.
    """

    tasks = _normalize_tasks(items)
    validate_process_contract(tasks, worker)
    base_policy = policy or DEFAULT_POLICY
    process_policy = SchedulerPolicy(
        backend=BACKEND_PROCESS,
        thread_min_batch_size=base_policy.thread_min_batch_size,
        process_min_batch_size=(1 if force_process else base_policy.process_min_batch_size),
        max_workers=base_policy.max_workers,
        logical_cpu_count=base_policy.logical_cpu_count,
        allow_gil_threads=base_policy.allow_gil_threads,
        allow_numpy_threads=base_policy.allow_numpy_threads,
        allow_process_benchmark=True,
        auto_process_benchmark=False,
    )
    return _schedule_tasks(
        tasks,
        worker,
        policy=process_policy,
        backend=BACKEND_PROCESS,
        full_fit_count=full_fit_count,
        pure_python=True,
        numpy_enabled=False,
        independent=True,
        token=token,
        generation=generation,
        current_generation=current_generation,
        validate_results=validate_results,
        raise_on_error=raise_on_error,
    )


run_process_prototype = run_process_benchmark


__all__ = [
    "BACKEND_AUTO",
    "BACKEND_PROCESS",
    "BACKEND_SINGLE",
    "BACKEND_THREAD",
    "BestSelection",
    "CancellationToken",
    "DEFAULT_POLICY",
    "DEFAULT_PROCESS_MIN_BATCH_SIZE",
    "DEFAULT_THREAD_MIN_BATCH_SIZE",
    "MAX_WORKERS",
    "NumericTask",
    "NumericWorkItem",
    "ProcessContractError",
    "ProcessSerializationReport",
    "SchedulerDecision",
    "SchedulerDiagnostics",
    "SchedulerPolicy",
    "SchedulerRequest",
    "SchedulerResult",
    "TaskResult",
    "TIE_EPSILON",
    "choose_backend",
    "decide_backend",
    "deterministic_best",
    "is_process_serializable",
    "is_valid_numeric_payload",
    "logical_cpu_count",
    "run_numeric_batch",
    "run_process_benchmark",
    "run_process_prototype",
    "schedule_batch",
    "schedule_numeric_batch",
    "select_backend",
    "select_best",
    "select_best_result",
    "validate_numeric_payload",
    "validate_process_contract",
    "worker_count_for",
]
