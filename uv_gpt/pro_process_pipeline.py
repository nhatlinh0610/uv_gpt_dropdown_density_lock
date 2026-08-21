"""Deterministic two-stage process pipeline for the MC3B live adapter.

The controller is deliberately Blender-free.  It owns no mesh or UV object;
the caller supplies immutable shape batches and a bounded graph/exact builder.
Workers may finish in any order, but the only consumable observations exposed
by this module are complete results merged by canonical pair ordinal.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import time
from typing import Any, Callable, Iterable, Optional, Sequence

try:
    from .pro_process_payload import (
        PayloadValidationError,
        pair_result_digest_wire,
        stable_digest,
    )
    from .pro_process_pool import (
        PersistentWorkerPool,
        PoolResult,
        PoolStreamBusyError,
    )
except ImportError:  # direct-file imports used by the bundled worker tests
    from pro_process_payload import (  # type: ignore[no-redef]
        PayloadValidationError,
        pair_result_digest_wire,
        stable_digest,
    )
    from pro_process_pool import (  # type: ignore[no-redef]
        PersistentWorkerPool,
        PoolResult,
        PoolStreamBusyError,
    )

try:
    from .pro_group_first import (
        DirectExactJob,
        GroupFirstFrontier,
        GroupFirstPlan,
        GroupPairRequest,
        GroupPairResult,
    )
except ImportError:  # direct-file imports used by focused pure tests
    from pro_group_first import (  # type: ignore[no-redef]
        DirectExactJob,
        GroupFirstFrontier,
        GroupFirstPlan,
        GroupPairRequest,
        GroupPairResult,
    )


class PipelineError(RuntimeError):
    """The staged operation cannot expose a complete canonical result."""


class PipelineCancelled(PipelineError):
    """The operation was explicitly cancelled or invalidated."""


def _semantic_result_wire(value: Any) -> Any:
    """Exclude observational timing from canonical exact-result digests."""

    to_wire = getattr(value, "to_wire", None)
    if not callable(to_wire):
        return repr(value)
    wire = to_wire()
    if isinstance(wire, tuple) and wire and wire[0] == "pair-result":
        return pair_result_digest_wire(value)
    return wire


@dataclass(frozen=True)
class PipelinePairOutcome:
    """One canonical pair after both stages have been validated."""

    pair_ordinal: int
    shape_result: Any = None
    exact_result: Any = None
    pruned: bool = False
    prune_reason: str = ""

    @property
    def shape_accepted(self) -> bool:
        return bool(not self.pruned and getattr(self.shape_result, "accepted", False))

    @property
    def exact_accepted(self) -> bool:
        return bool(self.exact_result is not None and getattr(self.exact_result, "accepted", False))


@dataclass(frozen=True)
class PipelineProgress:
    stage: str
    pairs_total: int
    shape_completed: int
    shape_accepted: int
    shape_rejected: int
    exact_total: int
    exact_completed: int
    exact_accepted: int
    merged_pairs: int
    active_workers: int
    worker_count: int
    queue_depth: int
    retry_count: int
    elapsed_ms: float
    worker_pids: tuple[int, ...] = ()
    worker_distribution: tuple[tuple[int, int], ...] = ()
    startup_timings_ms: tuple[float, ...] = ()
    failure: str = ""
    cancelled: bool = False
    complete: bool = False
    shape_submitted: int = 0
    shape_batches_submitted: int = 0
    exact_submitted: int = 0
    exact_batches_submitted: int = 0
    pruned_pairs: int = 0
    exact_started_before_shape_terminal: bool = False
    last_progress_kind: str = ""
    retry_total: int = 0
    max_retry_per_batch: int = 0
    retried_batch_count: int = 0
    retry_failure_reason: str = ""
    retry_batches: tuple[tuple[str, int], ...] = ()
    graph_tasks_submitted: int = 0
    graph_tasks_completed: int = 0
    graph_items_total: int = 0
    graph_items_completed: int = 0
    exact_first_shape_completed: int = 0
    exact_first_shape_total: int = 0
    exact_first_timestamp_ms: Optional[float] = None
    graph_cache_hits: int = 0
    poll_calls: int = 0
    no_progress_loops: int = 0
    event_epoch: int = 0
    graph_event_epoch: int = 0
    graph_waiter_registrations: int = 0
    graph_waiter_dedup: int = 0
    resident_exact_batches_submitted: int = 0
    resident_exact_batches_completed: int = 0
    resident_graph_cache_builds: int = 0
    resident_graph_cache_hits: int = 0
    resident_graph_compute_ms: float = 0.0
    resident_topology_cache_builds: int = 0
    resident_topology_cache_hits: int = 0
    resident_topology_compute_ms: float = 0.0
    resident_exact_compute_ms: float = 0.0
    fused_batches_submitted: int = 0
    fused_batches_completed: int = 0
    fused_graph_cache_builds: int = 0
    fused_graph_cache_hits: int = 0
    fused_graph_compute_ms: float = 0.0
    fused_exact_compute_ms: float = 0.0
    fused_shape_compute_ms: float = 0.0
    fused_shape_cache_hits: int = 0
    fused_lower_bound_checked: int = 0
    fused_lower_bound_rejected: int = 0
    fused_lower_bound_skipped: int = 0
    fused_lower_bound_graph_pairs_avoided: int = 0
    fused_lower_bound_min_ratio: float = 0.0
    fused_lower_bound_max_ratio: float = 0.0
    fused_frame_bytes: int = 0
    fused_frame_total_bytes: int = 0
    frame_bytes_max: tuple[tuple[str, int], ...] = ()
    frame_bytes_total: tuple[tuple[str, int], ...] = ()
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
    restart_pending: int = 0
    restart_states: tuple[tuple[int, str, str], ...] = ()


@dataclass(frozen=True)
class PipelineResult:
    complete: bool
    cancelled: bool
    generation_invalidated: bool
    outcomes: tuple[PipelinePairOutcome, ...]
    result_digest: str
    progress: PipelineProgress
    failure: str = ""


@dataclass(frozen=True)
class FrontierDecision:
    """Main-thread ownership decision after one canonical frontier outcome."""

    release_next: bool = True
    prune_remaining: bool = False
    reason: str = ""


@dataclass
class _FusedPendingAdmission:
    """Validated fused tasks waiting for bounded stream admission.

    The builder may finish after the current modal deadline.  Keeping the
    immutable task tuple and a submit cursor lets the next tick admit the
    exact same tasks without rebuilding or duplicating batch identities.
    Ordinals in the unsubmitted tail intentionally remain unreserved until
    their task is accepted by the pool.
    """

    admitted_ordinals: tuple[int, ...]
    tasks: tuple[Any, ...]
    next_task_index: int = 0


def _ordinal(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PayloadValidationError("pipeline pair ordinal must be a non-negative integer")
    return value


class ProcessPipeline:
    """Run shape, build exact payloads, and canonical-merge without blocking.

    ``exact_builder`` is called only after a complete shape stage.  It may
    return ``None`` while the main thread is doing bounded graph snapshot work;
    a later ``advance`` retries it.  Once it returns a sequence, that exact
    stage is submitted to the same pool and therefore reuses the same PIDs.
    """

    STAGES = (
        "idle", "shape_dispatch", "shape_wait", "graph_snapshot",
        "exact_dispatch", "exact_wait", "exact_merge", "done", "cancelled", "failed",
    )

    def __init__(
        self,
        pool: PersistentWorkerPool,
        shape_batches: Sequence[Any],
        *,
        canonical_ordinals: Optional[Iterable[int]] = None,
        exact_builder: Optional[Callable[[tuple[Any, ...]], Optional[Sequence[Any]]]] = None,
        graph_result_callback: Optional[Callable[[Any, Any], Any]] = None,
        merge_callback: Optional[Callable[[PipelinePairOutcome], Any]] = None,
        merge_limit: Optional[int] = None,
    ) -> None:
        self.pool = pool
        self.shape_batches = tuple(shape_batches)
        discovered = [ordinal for batch in self.shape_batches for ordinal in getattr(batch, "pair_ordinals", ())]
        source = tuple(canonical_ordinals) if canonical_ordinals is not None else tuple(sorted(discovered))
        self.canonical_ordinals = tuple(_ordinal(item) for item in source)
        if len(set(self.canonical_ordinals)) != len(self.canonical_ordinals):
            raise PayloadValidationError("pipeline canonical ordinals are duplicated")
        if set(discovered) != set(self.canonical_ordinals):
            raise PayloadValidationError("shape batches do not cover canonical ordinals")
        if tuple(sorted(self.canonical_ordinals)) != self.canonical_ordinals:
            raise PayloadValidationError("canonical ordinals must be sorted")
        self.exact_builder = exact_builder
        self.graph_result_callback = graph_result_callback
        self.merge_callback = merge_callback
        self.merge_limit = merge_limit
        if merge_limit is not None and (isinstance(merge_limit, bool) or merge_limit <= 0):
            raise ValueError("merge_limit must be positive when provided")

        self.stage = "idle"
        self._started_at = 0.0
        self._shape_results: dict[int, Any] = {}
        self._exact_results: dict[int, Any] = {}
        self._exact_tasks: tuple[Any, ...] = ()
        self._exact_ordinals: tuple[int, ...] = ()
        self._outcomes: list[PipelinePairOutcome] = []
        self._shape_accepted = 0
        self._shape_rejected = 0
        self._exact_accepted = 0
        self._merged_index = 0
        self._failure = ""
        self._cancelled = False
        self._generation_invalidated = False
        self._started = False

    @property
    def is_terminal(self) -> bool:
        return self.stage in {"done", "cancelled", "failed"}

    @property
    def has_consumable_result(self) -> bool:
        return self.stage == "done" and len(self._outcomes) == len(self.canonical_ordinals)

    @property
    def shape_results(self) -> tuple[Any, ...]:
        return tuple(self._shape_results[item] for item in self.canonical_ordinals if item in self._shape_results)

    @property
    def exact_results(self) -> tuple[Any, ...]:
        return tuple(self._exact_results[item] for item in self._exact_ordinals if item in self._exact_results)

    @property
    def outcomes(self) -> tuple[PipelinePairOutcome, ...]:
        return tuple(self._outcomes)

    @property
    def exact_tasks(self) -> tuple[Any, ...]:
        return self._exact_tasks

    def start(self) -> PipelineProgress:
        if self._started:
            return self.progress()
        self._started = True
        self._started_at = time.perf_counter()
        self.stage = "shape_dispatch"
        try:
            self.pool.begin(self.shape_batches)
        except Exception as exc:
            self._fail(f"shape dispatch failed: {exc}")
            return self.progress()
        self.stage = "shape_wait"
        return self.progress()

    @staticmethod
    def _result_pairs(result: PoolResult) -> tuple[Any, ...]:
        if not result.complete:
            raise PipelineError(result.failure or "worker stage did not complete")
        ordered = tuple(sorted(result.results, key=lambda item: _ordinal(getattr(item, "pair_ordinal"))))
        if len({item.pair_ordinal for item in ordered}) != len(ordered):
            raise PipelineError("worker stage returned duplicate result ordinal")
        return ordered

    def _accept_shape_stage(self, result: PoolResult) -> None:
        pairs = self._result_pairs(result)
        if tuple(item.pair_ordinal for item in pairs) != self.canonical_ordinals:
            raise PipelineError("shape stage result coverage is not canonical")
        self._shape_results = {item.pair_ordinal: item for item in pairs}
        self._shape_accepted = sum(int(bool(getattr(item, "accepted", False))) for item in pairs)
        self._shape_rejected = len(pairs) - self._shape_accepted
        self.stage = "graph_snapshot"

    def _try_dispatch_exact(self, deadline: Optional[float] = None) -> None:
        if self.exact_builder is None:
            self._exact_tasks = ()
        else:
            built = self.exact_builder(self.shape_results)
            if built is None:
                return
            self._exact_tasks = tuple(built)
        exact_ordinals = [ordinal for task in self._exact_tasks for ordinal in getattr(task, "pair_ordinals", ())]
        expected = tuple(sorted(
            ordinal for ordinal, result in self._shape_results.items()
            if bool(getattr(result, "accepted", False))
        ))
        if tuple(sorted(exact_ordinals)) != expected or len(set(exact_ordinals)) != len(exact_ordinals):
            raise PipelineError("exact stage does not cover shape-accepted ordinals exactly")
        self._exact_ordinals = expected
        self.stage = "exact_dispatch"
        self.pool.begin(self._exact_tasks)
        if self.pool.is_terminal:
            self._accept_exact_stage(self.pool.final_result())
        else:
            self.stage = "exact_wait"

    def _accept_exact_stage(self, result: PoolResult) -> None:
        pairs = self._result_pairs(result)
        if tuple(item.pair_ordinal for item in pairs) != self._exact_ordinals:
            raise PipelineError("exact stage result coverage is not canonical")
        self._exact_results = {item.pair_ordinal: item for item in pairs}
        self._exact_accepted = sum(int(bool(getattr(item, "accepted", False))) for item in pairs)
        self.stage = "exact_merge"

    def _merge_prefix(self) -> None:
        limit = self.merge_limit if self.merge_limit is not None else len(self.canonical_ordinals)
        end = min(len(self.canonical_ordinals), self._merged_index + limit)
        while self._merged_index < end:
            ordinal = self.canonical_ordinals[self._merged_index]
            shape_result = self._shape_results[ordinal]
            exact_result = self._exact_results.get(ordinal)
            if bool(getattr(shape_result, "accepted", False)) and exact_result is None:
                raise PipelineError(f"missing exact result for accepted shape ordinal {ordinal}")
            outcome = PipelinePairOutcome(ordinal, shape_result, exact_result)
            if self.merge_callback is not None:
                self.merge_callback(outcome)
            self._outcomes.append(outcome)
            self._merged_index += 1
        if self._merged_index == len(self.canonical_ordinals):
            self.stage = "done"

    def _fail(self, message: str) -> None:
        self._failure = str(message)[:1024]
        self._shape_results.clear()
        self._exact_results.clear()
        self._outcomes.clear()
        self._merged_index = 0
        self.stage = "failed"
        if self._started and not self.pool.is_terminal:
            try:
                self.pool.cancel(timeout=0.5)
            except Exception:
                pass

    def advance(self, timeout: float = 0.0, *, merge_limit: Optional[int] = None) -> PipelineProgress:
        if not self._started:
            self.start()
        if self.is_terminal:
            return self.progress()
        try:
            if self.stage == "shape_wait":
                self.pool.poll(timeout)
                if self.pool.is_terminal:
                    self._accept_shape_stage(self.pool.final_result())
            if self.stage == "graph_snapshot":
                self._try_dispatch_exact()
            if self.stage == "exact_wait":
                self.pool.poll(timeout)
                if self.pool.is_terminal:
                    self._accept_exact_stage(self.pool.final_result())
            if self.stage == "exact_merge":
                old_limit = self.merge_limit
                if merge_limit is not None:
                    self.merge_limit = merge_limit
                try:
                    self._merge_prefix()
                finally:
                    self.merge_limit = old_limit
        except Exception as exc:
            self._fail(str(exc))
        return self.progress()

    def drain_merged(self) -> tuple[PipelinePairOutcome, ...]:
        """Return only outcomes already merged in canonical order."""
        return self.outcomes

    def cancel(
        self,
        *,
        timeout: float = 1.0,
        nonblocking: bool = False,
    ) -> Optional[PipelineResult]:
        self._cancelled = True
        self._shape_results.clear()
        self._exact_results.clear()
        self._outcomes.clear()
        self._merged_index = 0
        try:
            if self._started:
                if nonblocking:
                    self.pool.begin_cancel()
                    self.stage = "cancelling"
                    if not bool(getattr(self.pool, "cancel_complete", False)):
                        return None
                else:
                    self.pool.cancel(timeout=timeout)
        finally:
            if not nonblocking or bool(getattr(self.pool, "cancel_complete", True)):
                self.stage = "cancelled"
        return self.final_result()

    def advance_cancel(self, *, deadline: Optional[float] = None) -> Optional[PipelineResult]:
        if self.stage != "cancelling":
            return self.final_result() if self.is_terminal else None
        self.pool.advance_cancel(deadline=deadline)
        if bool(getattr(self.pool, "cancel_complete", False)):
            self.stage = "cancelled"
            return self.final_result()
        return None

    def invalidate_generation(self, generation: int) -> PipelineResult:
        self._generation_invalidated = True
        self._cancelled = True
        self._shape_results.clear()
        self._exact_results.clear()
        self._outcomes.clear()
        self._merged_index = 0
        try:
            if self._started:
                self.pool.invalidate_generation(generation)
        finally:
            self.stage = "cancelled"
        return self.final_result()

    def close(self) -> None:
        try:
            self.pool.close()
        finally:
            if not self.is_terminal:
                self.stage = "cancelled"
                self._cancelled = True

    def progress(self) -> PipelineProgress:
        pool_progress = self.pool.progress() if self._started else None
        elapsed = 0.0 if not self._started_at else (time.perf_counter() - self._started_at) * 1000.0
        if pool_progress is not None:
            active = pool_progress.active_workers
            queue_depth = self.pool.queue_depth
            retry_count = pool_progress.retry_count
        else:
            active = queue_depth = retry_count = 0
        return PipelineProgress(
            stage=self.stage,
            pairs_total=len(self.canonical_ordinals),
            shape_completed=len(self._shape_results),
            shape_accepted=self._shape_accepted,
            shape_rejected=self._shape_rejected,
            exact_total=len(self._exact_ordinals),
            exact_completed=len(self._exact_results),
            exact_accepted=self._exact_accepted,
            merged_pairs=self._merged_index,
            active_workers=active,
            worker_count=self.pool.worker_count,
            queue_depth=queue_depth,
            retry_count=retry_count,
            retry_total=int(getattr(pool_progress, "retry_total", retry_count)),
            max_retry_per_batch=int(getattr(pool_progress, "max_retry_per_batch", 0)),
            retried_batch_count=int(getattr(pool_progress, "retried_batch_count", 0)),
            retry_failure_reason=str(getattr(pool_progress, "retry_failure_reason", "") or ""),
            retry_batches=tuple(getattr(pool_progress, "retry_batches", ())),
            nearest_attempted=int(getattr(pool_progress, "nearest_attempted", 0)),
            nearest_accepted=int(getattr(pool_progress, "nearest_accepted", 0)),
            nearest_fallback=int(getattr(pool_progress, "nearest_fallback", 0)),
            nearest_max_seed_distance=float(getattr(pool_progress, "nearest_max_seed_distance", 0.0)),
            nearest_mean_seed_distance=float(getattr(pool_progress, "nearest_mean_seed_distance", 0.0)),
            nearest_ambiguity_count=int(getattr(pool_progress, "nearest_ambiguity_count", 0)),
            nearest_tie_count=int(getattr(pool_progress, "nearest_tie_count", 0)),
            nearest_compute_ms=float(getattr(pool_progress, "nearest_compute_ms", 0.0)),
            nearest_distance_evaluations=int(
                getattr(pool_progress, "nearest_distance_evaluations", 0)
            ),
            nearest_assignment_nodes=int(
                getattr(pool_progress, "nearest_assignment_nodes", 0)
            ),
            nearest_assignment_cap=int(
                getattr(pool_progress, "nearest_assignment_cap", 0)
            ),
            nearest_fallback_reasons=tuple(
                getattr(pool_progress, "nearest_fallback_reasons", ())
            ),
            nearest_distance_lookups=int(
                getattr(pool_progress, "nearest_distance_lookups", 0)
            ),
            nearest_distance_cache_hits=int(
                getattr(pool_progress, "nearest_distance_cache_hits", 0)
            ),
            nearest_distance_cache_misses=int(
                getattr(pool_progress, "nearest_distance_cache_misses", 0)
            ),
            nearest_operations_used=int(
                getattr(pool_progress, "nearest_operations_used", 0)
            ),
            graph_rejected_before_nearest=int(
                getattr(pool_progress, "graph_rejected_before_nearest", 0)
            ),
            nearest_seed_missing=int(
                getattr(pool_progress, "nearest_seed_missing", 0)
            ),
            nearest_fast_miss=int(
                getattr(pool_progress, "nearest_fast_miss", 0)
            ),
            exact_fallback_calls=int(
                getattr(pool_progress, "exact_fallback_calls", 0)
            ),
            exact_primary_calls=int(
                getattr(pool_progress, "exact_primary_calls", 0)
            ),
            restart_pending=int(getattr(pool_progress, "restart_pending", 0)),
            restart_states=tuple(getattr(pool_progress, "restart_states", ())),
            elapsed_ms=elapsed,
            worker_pids=self.pool.worker_pids,
            worker_distribution=self.pool.worker_task_distribution,
            startup_timings_ms=self.pool.startup_timings_ms,
            failure=self._failure,
            cancelled=self._cancelled,
            complete=self.stage == "done",
        )

    def final_result(self) -> PipelineResult:
        complete = self.has_consumable_result
        if complete:
            digest = stable_digest(tuple(
                (item.pair_ordinal,
                 getattr(item.shape_result, "to_wire", lambda: repr(item.shape_result))(),
                 None if item.exact_result is None else _semantic_result_wire(item.exact_result))
                for item in self._outcomes
            ))
            outcomes = tuple(self._outcomes)
        else:
            digest = ""
            outcomes = ()
        return PipelineResult(
            complete=complete,
            cancelled=self._cancelled,
            generation_invalidated=self._generation_invalidated,
            outcomes=outcomes,
            result_digest=digest,
            progress=self.progress(),
            failure=self._failure,
        )


class FrontierProcessPipeline:
    """Bounded canonical frontier with interleaved shape and exact work.

    The planner supplies all immutable pair ordinals, but only the first
    unresolved candidate for each ownership domain is admitted.  A shape batch
    can therefore complete while later domains remain undispatched.  Accepted
    shape results are converted to exact tasks immediately; canonical merge is
    still the only place that can release the next candidate or prune a domain.
    """

    STAGES = (
        "idle", "shape_dispatch", "shape_wait", "graph_snapshot",
        "exact_dispatch", "exact_wait", "exact_merge", "done", "cancelled", "failed",
    )

    def __init__(
        self,
        pool: PersistentWorkerPool,
        canonical_ordinals: Iterable[int],
        *,
        domain_for_ordinal: Callable[[int], Any],
        shape_builder: Callable[[tuple[int, ...]], Any],
        exact_builder: Optional[Callable[[Any], Any]] = None,
        graph_result_callback: Optional[Callable[[Any, Any], Any]] = None,
        graph_task_admitted_callback: Optional[Callable[[Any], Any]] = None,
        merge_callback: Optional[Callable[[PipelinePairOutcome], Any]] = None,
        batch_size: int = 64,
        merge_limit: Optional[int] = None,
    ) -> None:
        source = tuple(_ordinal(item) for item in canonical_ordinals)
        if not source or tuple(sorted(source)) != source or len(set(source)) != len(source):
            raise PayloadValidationError("frontier canonical ordinals must be sorted and unique")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("frontier batch_size must be positive")
        if merge_limit is not None and (isinstance(merge_limit, bool) or merge_limit <= 0):
            raise ValueError("merge_limit must be positive when provided")
        self.pool = pool
        self.canonical_ordinals = source
        self._domain_for_ordinal = domain_for_ordinal
        self.shape_builder = shape_builder
        self.exact_builder = exact_builder
        self.graph_result_callback = graph_result_callback
        self.graph_task_admitted_callback = graph_task_admitted_callback
        self.merge_callback = merge_callback
        self.batch_size = batch_size
        self.merge_limit = merge_limit

        self._domain_by_ordinal: dict[int, Any] = {}
        self._domain_queues: dict[Any, deque[int]] = {}
        for ordinal in source:
            domain = domain_for_ordinal(ordinal)
            try:
                hash(domain)
            except TypeError as exc:
                raise PayloadValidationError("frontier domain must be hashable") from exc
            self._domain_by_ordinal[ordinal] = domain
            self._domain_queues.setdefault(domain, deque()).append(ordinal)

        self.stage = "idle"
        self._started = False
        self._started_at = 0.0
        self._shape_results: dict[int, Any] = {}
        self._exact_results: dict[int, Any] = {}
        self._no_exact: set[int] = set()
        self._exact_pending: dict[int, Any] = {}
        self._graph_waiting: dict[int, Any] = {}
        self._graph_waiter_counts: dict[int, int] = {}
        self._exact_wait_graph_epoch: dict[int, int] = {}
        self._task_meta: dict[str, tuple[str, tuple[int, ...]]] = {}
        self._reserved_domains: set[Any] = set()
        self._closed_domains: set[Any] = set()
        self._pruned: dict[int, str] = {}
        self._outcomes: list[PipelinePairOutcome] = []
        self._merged_index = 0
        self._shape_submitted = 0
        self._shape_batches_submitted = 0
        self._shape_completed = 0
        self._shape_accepted = 0
        self._shape_rejected = 0
        self._exact_total = 0
        self._exact_submitted = 0
        self._exact_batches_submitted = 0
        self._exact_completed = 0
        self._exact_accepted = 0
        self._pruned_pairs = 0
        self._retry_count = 0
        self._failure = ""
        self._cancelled = False
        self._generation_invalidated = False
        self._last_progress_kind = ""
        self._exact_started_before_shape_terminal = False
        self._exact_first_shape_completed = 0
        self._exact_first_shape_total = len(source)
        self._exact_first_timestamp_ms: Optional[float] = None
        self._graph_tasks_submitted = 0
        self._graph_tasks_completed = 0
        self._graph_items_total = 0
        self._graph_items_completed = 0
        self._poll_calls = 0
        self._no_progress_loops = 0
        self._event_epoch = 0
        self._graph_event_epoch = 0
        self._graph_waiter_registrations = 0
        self._graph_waiter_dedup = 0
        self._resident_exact_batches_submitted = 0
        self._resident_exact_batches_completed = 0
        self._resident_graph_cache_builds = 0
        self._resident_graph_cache_hits = 0
        self._resident_graph_compute_ms = 0.0
        self._resident_topology_cache_builds = 0
        self._resident_topology_cache_hits = 0
        self._resident_topology_compute_ms = 0.0
        self._resident_exact_compute_ms = 0.0
        self._fused_batches_submitted = 0
        self._fused_batches_completed = 0
        self._fused_graph_cache_builds = 0
        self._fused_graph_cache_hits = 0
        self._fused_graph_compute_ms = 0.0
        self._fused_exact_compute_ms = 0.0
        self._fused_shape_compute_ms = 0.0
        self._fused_shape_cache_hits = 0
        self._fused_lower_bound_checked = 0
        self._fused_lower_bound_rejected = 0
        self._fused_lower_bound_skipped = 0
        self._fused_lower_bound_graph_pairs_avoided = 0
        self._fused_lower_bound_min_ratio = 0.0
        self._fused_lower_bound_max_ratio = 0.0
        self._fused_frame_bytes = 0
        self._fused_task_by_ordinal: dict[int, Any] = {}
        self._pending_fused_admission: Optional[_FusedPendingAdmission] = None
        self._stream_finished = False
        # ``PersistentWorkerPool.poll_stream`` may return more than one
        # completion in one non-blocking poll.  A modal deadline can expire
        # while the first completion is being validated/converted.  Keep the
        # unconsumed tail here: the pool has already retired those slots, so
        # dropping the tail would create a permanently missing canonical
        # ordinal and could make a soft timeout expose an unsafe prefix.
        self._completion_buffer: deque[Any] = deque()

    @property
    def is_terminal(self) -> bool:
        return self.stage in {"done", "cancelled", "failed"}

    @property
    def has_consumable_result(self) -> bool:
        return self.stage == "done" and len(self._outcomes) == len(self.canonical_ordinals)

    @property
    def shape_results(self) -> tuple[Any, ...]:
        return tuple(
            self._shape_results[item]
            for item in self.canonical_ordinals
            if item in self._shape_results
        )

    @property
    def exact_results(self) -> tuple[Any, ...]:
        return tuple(
            self._exact_results[item]
            for item in self.canonical_ordinals
            if item in self._exact_results
        )

    @property
    def outcomes(self) -> tuple[PipelinePairOutcome, ...]:
        return tuple(self._outcomes)

    @staticmethod
    def _task_ordinals(task: Any) -> tuple[int, ...]:
        values = tuple(_ordinal(item) for item in getattr(task, "pair_ordinals", ()))
        if not values:
            raise PipelineError("frontier task has no pair ordinals")
        return values

    @staticmethod
    def _task_kind(task: Any) -> str:
        return str(getattr(task, "operation_kind", "exact"))

    @staticmethod
    def _normalise_decision(value: Any) -> FrontierDecision:
        if isinstance(value, FrontierDecision):
            return value
        if value is False:
            return FrontierDecision(False, True, "callback_closed_domain")
        if value is True or value is None:
            return FrontierDecision()
        raise PipelineError("frontier merge callback returned an invalid decision")

    def start(self, *, deadline: Optional[float] = None) -> PipelineProgress:
        if self._started:
            return self.progress()
        self._started = True
        self._started_at = time.perf_counter()
        self.stage = "shape_dispatch"
        try:
            self.pool.begin_stream()
            self._fill_frontier(deadline)
            self._update_stage()
        except Exception as exc:
            self._fail(f"frontier start failed: {exc}")
        return self.progress()

    def _fill_frontier(self, deadline: Optional[float] = None) -> None:
        """Admit at most capacity batches, one candidate per domain each."""

        while not self.is_terminal and self.pool.stream_capacity > 0:
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            capacity = int(self.pool.stream_capacity)
            selected: list[int] = []
            selected_domains: set[Any] = set()
            for ordinal in self.canonical_ordinals:
                domain = self._domain_by_ordinal[ordinal]
                queue = self._domain_queues[domain]
                if domain in self._closed_domains or domain in self._reserved_domains:
                    continue
                if domain in selected_domains or not queue or queue[0] != ordinal:
                    continue
                selected.append(ordinal)
                selected_domains.add(domain)
                if len(selected) >= self.batch_size:
                    break
            if not selected:
                break
            try:
                task = self.shape_builder(tuple(selected))
                if tuple(sorted(self._task_ordinals(task))) != tuple(sorted(selected)):
                    raise PipelineError("shape builder changed frontier coverage")
                self._stream_submit(task, deadline)
            except PoolStreamBusyError:
                break
            for ordinal in selected:
                self._reserved_domains.add(self._domain_by_ordinal[ordinal])
            self._task_meta[str(task.batch_id)] = ("shape", tuple(selected))
            self._shape_submitted += len(selected)
            self._shape_batches_submitted += 1
            self._event_epoch += 1
            self._last_progress_kind = "shape_dispatch"

    def _stream_submit(self, task: Any, deadline: Optional[float] = None) -> Any:
        """Submit one task while preserving the shared modal deadline."""

        if deadline is None:
            return self.pool.stream_submit(task)
        if isinstance(self.pool, PersistentWorkerPool):
            return self.pool.stream_submit(task, deadline=deadline)
        # Existing focused fake pools intentionally predate the keyword.
        return self.pool.stream_submit(task)

    def _handle_completion(self, completion: Any) -> None:
        batch_id = str(getattr(completion, "batch_id", ""))
        meta = self._task_meta.pop(batch_id, None)
        if meta is None:
            raise PipelineError("frontier completion has an unknown task")
        stage, expected_ordinals = meta
        result = completion.result
        self._event_epoch += 1
        if stage == "graph":
            if len(expected_ordinals) != 1:
                raise PipelineError("graph completion must belong to one frontier ordinal")
            if self.graph_result_callback is not None:
                self.graph_result_callback(completion.task, result)
            ordinal = expected_ordinals[0]
            shape_result = self._graph_waiting.get(ordinal)
            if shape_result is None:
                raise PipelineError("graph completion has no waiting shape result")
            self._graph_event_epoch += 1
            remaining = self._graph_waiter_counts.get(ordinal, 0) - 1
            if remaining <= 0:
                self._graph_waiter_counts.pop(ordinal, None)
                self._graph_waiting.pop(ordinal, None)
                self._exact_pending[ordinal] = shape_result
            else:
                self._graph_waiter_counts[ordinal] = remaining
            self._graph_tasks_completed += 1
            self._graph_items_completed += int(
                getattr(completion.task, "item_count", len(getattr(result, "graph_results", ())))
            )
            self._last_progress_kind = "graph_complete"
            return
        pair_results = tuple(sorted(
            getattr(result, "pair_results", ()),
            key=lambda item: _ordinal(getattr(item, "pair_ordinal")),
        ))
        actual_ordinals = tuple(item.pair_ordinal for item in pair_results)
        if actual_ordinals != tuple(sorted(expected_ordinals)):
            raise PipelineError("frontier completion coverage mismatch")
        if stage == "shape":
            for pair_result in pair_results:
                ordinal = pair_result.pair_ordinal
                self._shape_results[ordinal] = pair_result
                self._shape_completed += 1
                if bool(getattr(pair_result, "accepted", False)):
                    self._shape_accepted += 1
                    self._exact_total += 1
                    self._exact_pending[ordinal] = pair_result
                else:
                    self._shape_rejected += 1
                self._last_progress_kind = "shape_complete"
        elif stage == "exact":
            if getattr(completion.task, "operation_kind", "") == "resident_exact":
                self._resident_exact_batches_completed += 1
                self._resident_graph_cache_builds += int(
                    getattr(result, "graph_cache_builds", 0)
                )
                self._resident_graph_cache_hits += int(
                    getattr(result, "graph_cache_hits", 0)
                )
                self._resident_graph_compute_ms += float(
                    getattr(result, "graph_compute_ms", 0.0)
                )
                self._resident_topology_cache_builds += int(
                    getattr(result, "topology_cache_builds", 0)
                )
                self._resident_topology_cache_hits += int(
                    getattr(result, "topology_cache_hits", 0)
                )
                self._resident_topology_compute_ms += float(
                    getattr(result, "topology_compute_ms", 0.0)
                )
                self._resident_exact_compute_ms += float(
                    getattr(result, "exact_compute_ms", 0.0)
                )
            for pair_result in pair_results:
                ordinal = pair_result.pair_ordinal
                self._exact_results[ordinal] = pair_result
                self._exact_completed += 1
                self._exact_accepted += int(bool(getattr(pair_result, "accepted", False)))
                self._last_progress_kind = "exact_complete"
        else:
            raise PipelineError("frontier completion has an invalid stage")

    def _try_dispatch_exact(self, deadline: Optional[float] = None) -> None:
        if self.exact_builder is None:
            for ordinal in tuple(sorted(self._exact_pending)):
                self._no_exact.add(ordinal)
                self._exact_pending.pop(ordinal, None)
            return
        for ordinal in tuple(sorted(self._exact_pending)):
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            if self._exact_wait_graph_epoch.get(ordinal) == self._graph_event_epoch:
                self._graph_waiter_dedup += 1
                continue
            shape_result = self._exact_pending[ordinal]
            built = self.exact_builder(shape_result)
            if built is None:
                self._exact_wait_graph_epoch[ordinal] = self._graph_event_epoch
                self.stage = "graph_snapshot"
                return
            self._exact_wait_graph_epoch.pop(ordinal, None)
            self._exact_pending.pop(ordinal, None)
            if built is False:
                self._no_exact.add(ordinal)
                self._event_epoch += 1
                self._last_progress_kind = "shape_terminal"
                continue
            tasks = (built,)
            if isinstance(built, (tuple, list)):
                tasks = tuple(built)
            if not tasks:
                raise PipelineError("exact builder returned no task")
            task_kind = self._task_kind(tasks[0])
            if any(self._task_kind(task) != task_kind for task in tasks):
                raise PipelineError("frontier builder returned mixed task kinds")
            for task in tasks:
                if deadline is not None and time.perf_counter() >= float(deadline):
                    self._exact_pending[ordinal] = shape_result
                    return
                if task_kind == "graph":
                    task_ordinals = (ordinal,)
                else:
                    task_ordinals = self._task_ordinals(task)
                    if any(item not in self._exact_pending and item != ordinal for item in task_ordinals):
                        raise PipelineError("exact builder returned an unknown frontier ordinal")
                    if len(task_ordinals) != 1 or task_ordinals[0] != ordinal:
                        raise PipelineError("frontier exact builder must return the requested ordinal")
                try:
                    self._stream_submit(task, deadline)
                except PoolStreamBusyError:
                    self._exact_pending[ordinal] = shape_result
                    return
                if task_kind == "graph":
                    if self.graph_task_admitted_callback is not None:
                        self.graph_task_admitted_callback(task)
                    self._task_meta[str(task.batch_id)] = ("graph", (ordinal,))
                    if ordinal in self._graph_waiting:
                        self._graph_waiter_dedup += 1
                    else:
                        self._graph_waiting[ordinal] = shape_result
                        self._graph_waiter_registrations += 1
                    self._graph_waiter_counts[ordinal] = (
                        self._graph_waiter_counts.get(ordinal, 0) + 1
                    )
                    self._graph_tasks_submitted += 1
                    self._graph_items_total += int(getattr(task, "item_count", 0))
                    self._event_epoch += 1
                    self._graph_event_epoch += 1
                    self._last_progress_kind = "graph_dispatch"
                else:
                    self._task_meta[str(task.batch_id)] = ("exact", task_ordinals)
                    self._exact_submitted += len(task_ordinals)
                    self._exact_batches_submitted += 1
                    if task_kind == "resident_exact":
                        self._resident_exact_batches_submitted += 1
                    self._event_epoch += 1
                    if self._exact_first_timestamp_ms is None:
                        self._exact_first_shape_completed = self._shape_completed
                        self._exact_first_timestamp_ms = (
                            time.perf_counter() - self._started_at
                        ) * 1000.0
                    self._exact_started_before_shape_terminal |= (
                        self._shape_completed < len(self.canonical_ordinals)
                    )
                    self._last_progress_kind = "exact_dispatch"
            self._exact_pending.pop(ordinal, None)
            break

    def _prune_domain(self, domain: Any, reason: str) -> None:
        queue = self._domain_queues[domain]
        for ordinal in tuple(queue):
            self._pruned.setdefault(ordinal, str(reason))
        queue.clear()
        self._closed_domains.add(domain)

    def _finish_domain(self, ordinal: int, decision: FrontierDecision) -> None:
        domain = self._domain_by_ordinal[ordinal]
        queue = self._domain_queues[domain]
        if queue and queue[0] == ordinal:
            queue.popleft()
        self._reserved_domains.discard(domain)
        if decision.prune_remaining or not decision.release_next:
            self._prune_domain(domain, decision.reason or "ownership_pruned")
        elif not queue:
            self._closed_domains.add(domain)

    def _merge_prefix(self, limit: Optional[int] = None, deadline: Optional[float] = None) -> None:
        effective_limit = limit if limit is not None else self.merge_limit
        end = len(self.canonical_ordinals)
        if effective_limit is not None:
            end = min(end, self._merged_index + effective_limit)
        while self._merged_index < end:
            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            ordinal = self.canonical_ordinals[self._merged_index]
            if ordinal in self._pruned:
                outcome = PipelinePairOutcome(
                    pair_ordinal=ordinal,
                    pruned=True,
                    prune_reason=self._pruned[ordinal],
                )
            else:
                shape_result = self._shape_results.get(ordinal)
                if shape_result is None:
                    return
                exact_result = self._exact_results.get(ordinal)
                if bool(getattr(shape_result, "accepted", False)) and ordinal not in self._no_exact and exact_result is None:
                    return
                outcome = PipelinePairOutcome(ordinal, shape_result, exact_result)
            decision = FrontierDecision()
            if self.merge_callback is not None:
                decision = self._normalise_decision(self.merge_callback(outcome))
            self._outcomes.append(outcome)
            self._merged_index += 1
            self._pruned_pairs += int(outcome.pruned)
            self._finish_domain(ordinal, decision)
            if hasattr(self, "_fused_task_by_ordinal"):
                self._fused_task_by_ordinal.pop(ordinal, None)
            self._event_epoch += 1
            self._last_progress_kind = "canonical_merge"
        self._fill_frontier(deadline)

    def _maybe_finish(self, deadline: Optional[float] = None) -> None:
        if self._merged_index != len(self.canonical_ordinals):
            return
        if self._task_meta or self._exact_pending or self._graph_waiting:
            return
        if any(self._domain_queues[domain] for domain in self._domain_queues):
            return
        if not self._stream_finished:
            if isinstance(self.pool, PersistentWorkerPool):
                self.pool.stream_finish(deadline=deadline)
            else:
                self.pool.stream_finish()
            self._stream_finished = True
        if not self.pool.is_terminal:
            return
        self.stage = "done"

    def _update_stage(self) -> None:
        if self.is_terminal:
            return
        if self._exact_pending:
            self.stage = "graph_snapshot"
        elif any(stage == "graph" for stage, _items in self._task_meta.values()):
            self.stage = "graph_snapshot"
        elif any(stage == "exact" for stage, _items in self._task_meta.values()):
            self.stage = "exact_wait"
        elif any(stage == "shape" for stage, _items in self._task_meta.values()):
            self.stage = "shape_wait"
        elif self._merged_index < len(self.canonical_ordinals):
            self.stage = "shape_dispatch"
        else:
            self.stage = "exact_merge"

    def _fail(self, message: Any) -> None:
        self._failure = str(message)[:1024]
        self._shape_results.clear()
        self._exact_results.clear()
        self._exact_pending.clear()
        self._graph_waiting.clear()
        self._graph_waiter_counts.clear()
        self._exact_wait_graph_epoch.clear()
        self._task_meta.clear()
        self._completion_buffer.clear()
        self._fused_task_by_ordinal.clear()
        self._pending_fused_admission = None
        self._outcomes.clear()
        self._merged_index = 0
        self.stage = "failed"
        if self._started and not self.pool.is_terminal:
            try:
                self.pool.cancel(timeout=1.0)
            except Exception:
                pass

    def advance(
        self,
        timeout: float = 0.0,
        *,
        merge_limit: Optional[int] = None,
        deadline: Optional[float] = None,
    ) -> PipelineProgress:
        if not self._started:
            self.start(deadline=deadline)
        if self.is_terminal:
            return self.progress()
        try:
            start_event_epoch = self._event_epoch
            start_merged_index = self._merged_index
            completions: tuple[Any, ...]
            if self._completion_buffer:
                # Drain already-retired results before polling for new work;
                # this preserves completion order across modal ticks without
                # allowing a second result tuple to be lost.
                completions = tuple(self._completion_buffer)
                self._completion_buffer.clear()
            elif deadline is None or time.perf_counter() < float(deadline):
                try:
                    self._poll_calls += 1
                    completions = self.pool.poll_stream(timeout, deadline=deadline)
                except TypeError:
                    # Existing focused fake pools predate the optional deadline.
                    self._poll_calls += 1
                    completions = self.pool.poll_stream(timeout)
            else:
                completions = ()
            for index, completion in enumerate(completions):
                if deadline is not None and time.perf_counter() >= float(deadline):
                    self._completion_buffer.extend(completions[index:])
                    break
                self._handle_completion(completion)
            pool_failure = str(getattr(self.pool, "_failure", "") or "")
            if pool_failure:
                raise PipelineError(pool_failure)
            self._try_dispatch_exact(deadline)
            self._merge_prefix(merge_limit, deadline)
            self._try_dispatch_exact(deadline)
            self._fill_frontier(deadline)
            self._merge_prefix(merge_limit, deadline)
            self._maybe_finish(deadline)
            self._update_stage()
            self._assert_no_active_stall(
                start_event_epoch,
                start_merged_index,
                deadline,
            )
            if (
                self._event_epoch == start_event_epoch
                and self._merged_index == start_merged_index
                and self._pending_fused_admission is None
            ):
                self._no_progress_loops += 1
        except Exception as exc:
            self._fail(str(exc))
        return self.progress()

    def _assert_no_active_stall(
        self,
        _start_event_epoch: int,
        _start_merged_index: int,
        _deadline: Optional[float],
    ) -> None:
        """Compatibility hook overridden by the fused frontier."""

        return None

    def drain_merged(self) -> tuple[PipelinePairOutcome, ...]:
        return self.outcomes

    def cancel(
        self,
        *,
        timeout: float = 1.0,
        nonblocking: bool = False,
    ) -> Optional[PipelineResult]:
        self._cancelled = True
        self._shape_results.clear()
        self._exact_results.clear()
        self._exact_pending.clear()
        self._graph_waiting.clear()
        self._graph_waiter_counts.clear()
        self._exact_wait_graph_epoch.clear()
        self._task_meta.clear()
        self._completion_buffer.clear()
        self._fused_task_by_ordinal.clear()
        self._pending_fused_admission = None
        self._outcomes.clear()
        self._merged_index = 0
        try:
            if self._started:
                if nonblocking:
                    self.pool.begin_cancel()
                    self.stage = "cancelling"
                    if not bool(getattr(self.pool, "cancel_complete", False)):
                        return None
                else:
                    self.pool.cancel(timeout=timeout)
        finally:
            if not nonblocking or bool(getattr(self.pool, "cancel_complete", True)):
                self.stage = "cancelled"
        return self.final_result()

    def advance_cancel(self, *, deadline: Optional[float] = None) -> Optional[PipelineResult]:
        if self.stage != "cancelling":
            return self.final_result() if self.is_terminal else None
        self.pool.advance_cancel(deadline=deadline)
        if bool(getattr(self.pool, "cancel_complete", False)):
            self.stage = "cancelled"
            return self.final_result()
        return None

    def invalidate_generation(self, generation: int) -> PipelineResult:
        self._generation_invalidated = True
        self._cancelled = True
        self._shape_results.clear()
        self._exact_results.clear()
        self._exact_pending.clear()
        self._graph_waiting.clear()
        self._graph_waiter_counts.clear()
        self._exact_wait_graph_epoch.clear()
        self._task_meta.clear()
        self._completion_buffer.clear()
        self._fused_task_by_ordinal.clear()
        self._pending_fused_admission = None
        self._outcomes.clear()
        self._merged_index = 0
        try:
            if self._started:
                self.pool.invalidate_generation(generation)
        finally:
            self.stage = "cancelled"
        return self.final_result()

    def close(self) -> None:
        try:
            self.pool.close()
        finally:
            self._completion_buffer.clear()
            self._fused_task_by_ordinal.clear()
            self._pending_fused_admission = None
            if not self.is_terminal:
                self.stage = "cancelled"
                self._cancelled = True

    def progress(self) -> PipelineProgress:
        pool_progress = self.pool.progress() if self._started else None
        elapsed = 0.0 if not self._started_at else (time.perf_counter() - self._started_at) * 1000.0
        if pool_progress is not None:
            active = pool_progress.active_workers
            queue_value = getattr(self.pool, "stream_queue_depth", None)
            if queue_value is None:
                queue_value = self.pool.queue_depth
            queue_depth = int(queue_value)
            retry_count = pool_progress.retry_count
            pids = pool_progress and tuple(getattr(self.pool, "worker_pids", ()))
            distribution = getattr(self.pool, "worker_task_distribution", ())
            startup = getattr(self.pool, "startup_timings_ms", ())
            frame_bytes_max = tuple(getattr(pool_progress, "frame_bytes_max", ()))
            frame_bytes_total = tuple(getattr(pool_progress, "frame_bytes_total", ()))
        else:
            active = queue_depth = retry_count = 0
            pids = distribution = startup = ()
            frame_bytes_max = frame_bytes_total = ()
        return PipelineProgress(
            stage=self.stage,
            pairs_total=len(self.canonical_ordinals),
            shape_completed=self._shape_completed,
            shape_accepted=self._shape_accepted,
            shape_rejected=self._shape_rejected,
            exact_total=self._exact_total,
            exact_completed=self._exact_completed,
            exact_accepted=self._exact_accepted,
            merged_pairs=self._merged_index,
            active_workers=active,
            worker_count=self.pool.worker_count,
            queue_depth=queue_depth,
            retry_count=retry_count,
            retry_total=int(getattr(pool_progress, "retry_total", retry_count)),
            max_retry_per_batch=int(getattr(pool_progress, "max_retry_per_batch", 0)),
            retried_batch_count=int(getattr(pool_progress, "retried_batch_count", 0)),
            retry_failure_reason=str(getattr(pool_progress, "retry_failure_reason", "") or ""),
            retry_batches=tuple(getattr(pool_progress, "retry_batches", ())),
            nearest_attempted=int(getattr(pool_progress, "nearest_attempted", 0)),
            nearest_accepted=int(getattr(pool_progress, "nearest_accepted", 0)),
            nearest_fallback=int(getattr(pool_progress, "nearest_fallback", 0)),
            nearest_max_seed_distance=float(getattr(pool_progress, "nearest_max_seed_distance", 0.0)),
            nearest_mean_seed_distance=float(getattr(pool_progress, "nearest_mean_seed_distance", 0.0)),
            nearest_ambiguity_count=int(getattr(pool_progress, "nearest_ambiguity_count", 0)),
            nearest_tie_count=int(getattr(pool_progress, "nearest_tie_count", 0)),
            nearest_compute_ms=float(getattr(pool_progress, "nearest_compute_ms", 0.0)),
            nearest_distance_evaluations=int(
                getattr(pool_progress, "nearest_distance_evaluations", 0)
            ),
            nearest_assignment_nodes=int(
                getattr(pool_progress, "nearest_assignment_nodes", 0)
            ),
            nearest_assignment_cap=int(
                getattr(pool_progress, "nearest_assignment_cap", 0)
            ),
            nearest_fallback_reasons=tuple(
                getattr(pool_progress, "nearest_fallback_reasons", ())
            ),
            nearest_distance_lookups=int(
                getattr(pool_progress, "nearest_distance_lookups", 0)
            ),
            nearest_distance_cache_hits=int(
                getattr(pool_progress, "nearest_distance_cache_hits", 0)
            ),
            nearest_distance_cache_misses=int(
                getattr(pool_progress, "nearest_distance_cache_misses", 0)
            ),
            nearest_operations_used=int(
                getattr(pool_progress, "nearest_operations_used", 0)
            ),
            restart_pending=int(getattr(pool_progress, "restart_pending", 0)),
            restart_states=tuple(getattr(pool_progress, "restart_states", ())),
            elapsed_ms=elapsed,
            worker_pids=pids,
            worker_distribution=distribution,
            startup_timings_ms=startup,
            failure=self._failure,
            cancelled=self._cancelled,
            complete=self.stage == "done",
            shape_submitted=self._shape_submitted,
            shape_batches_submitted=self._shape_batches_submitted,
            exact_submitted=self._exact_submitted,
            exact_batches_submitted=self._exact_batches_submitted,
            pruned_pairs=self._pruned_pairs,
            graph_tasks_submitted=self._graph_tasks_submitted,
            graph_tasks_completed=self._graph_tasks_completed,
            graph_items_total=self._graph_items_total,
            graph_items_completed=self._graph_items_completed,
            graph_cache_hits=int(getattr(pool_progress, "graph_cache_hits", 0)),
            exact_first_shape_completed=self._exact_first_shape_completed,
            exact_first_shape_total=self._exact_first_shape_total,
            exact_first_timestamp_ms=self._exact_first_timestamp_ms,
            exact_started_before_shape_terminal=self._exact_started_before_shape_terminal,
            last_progress_kind=self._last_progress_kind,
            poll_calls=self._poll_calls,
            no_progress_loops=self._no_progress_loops,
            event_epoch=self._event_epoch,
            graph_event_epoch=self._graph_event_epoch,
            graph_waiter_registrations=self._graph_waiter_registrations,
            graph_waiter_dedup=self._graph_waiter_dedup,
            resident_exact_batches_submitted=self._resident_exact_batches_submitted,
            resident_exact_batches_completed=self._resident_exact_batches_completed,
            resident_graph_cache_builds=self._resident_graph_cache_builds,
            resident_graph_cache_hits=self._resident_graph_cache_hits,
            resident_graph_compute_ms=self._resident_graph_compute_ms,
            resident_exact_compute_ms=self._resident_exact_compute_ms,
            fused_batches_submitted=self._fused_batches_submitted,
            fused_batches_completed=self._fused_batches_completed,
            fused_graph_cache_builds=self._fused_graph_cache_builds,
            fused_graph_cache_hits=self._fused_graph_cache_hits,
            fused_graph_compute_ms=self._fused_graph_compute_ms,
            fused_exact_compute_ms=self._fused_exact_compute_ms,
            fused_shape_compute_ms=self._fused_shape_compute_ms,
            fused_shape_cache_hits=self._fused_shape_cache_hits,
            fused_lower_bound_checked=self._fused_lower_bound_checked,
            fused_lower_bound_rejected=self._fused_lower_bound_rejected,
            fused_lower_bound_skipped=self._fused_lower_bound_skipped,
            fused_lower_bound_graph_pairs_avoided=self._fused_lower_bound_graph_pairs_avoided,
            fused_lower_bound_min_ratio=self._fused_lower_bound_min_ratio,
            fused_lower_bound_max_ratio=self._fused_lower_bound_max_ratio,
            fused_frame_bytes=max(
                int(self._fused_frame_bytes),
                dict(frame_bytes_max).get("fused", 0),
            ),
            fused_frame_total_bytes=int(dict(frame_bytes_total).get("fused", 0)),
            frame_bytes_max=frame_bytes_max,
            frame_bytes_total=frame_bytes_total,
        )

    def final_result(self) -> PipelineResult:
        complete = self.has_consumable_result
        if complete:
            digest_values = []
            for item in self._outcomes:
                if item.pruned:
                    digest_values.append(("pruned", item.pair_ordinal, item.prune_reason))
                else:
                    digest_values.append((
                        item.pair_ordinal,
                        getattr(item.shape_result, "to_wire", lambda: repr(item.shape_result))(),
                        None if item.exact_result is None else _semantic_result_wire(item.exact_result),
                    ))
            digest = stable_digest(tuple(digest_values))
            outcomes = tuple(self._outcomes)
        else:
            digest = ""
            outcomes = ()
        return PipelineResult(
            complete=complete,
            cancelled=self._cancelled,
            generation_invalidated=self._generation_invalidated,
            outcomes=outcomes,
            result_digest=digest,
            progress=self.progress(),
            failure=self._failure,
        )


class FusedProcessPipeline(FrontierProcessPipeline):
    """Canonical frontier for one worker-resident fused master batch.

    The base frontier remains the compatibility path for MC3B/C12.  This
    subclass admits lowest-ordinal candidates grouped by one master, sends a
    single fused task, and records both stage outcomes from one completion.
    No graph waiter or second IPC round-trip exists in this path.
    """

    STAGES = (
        "idle", "fused_dispatch", "fused_wait", "fused_merge",
        "done", "cancelled", "failed",
    )

    def __init__(
        self,
        pool: PersistentWorkerPool,
        canonical_ordinals: Iterable[int],
        *,
        domain_for_ordinal: Callable[[int], Any],
        master_for_ordinal: Callable[[int], Any],
        fused_builder: Callable[[tuple[int, ...]], Any],
        merge_callback: Optional[Callable[[PipelinePairOutcome], Any]] = None,
        batch_size: int = 16,
        merge_limit: Optional[int] = None,
    ) -> None:
        self.master_for_ordinal = master_for_ordinal
        self.fused_builder = fused_builder
        super().__init__(
            pool,
            canonical_ordinals,
            domain_for_ordinal=domain_for_ordinal,
            shape_builder=fused_builder,
            exact_builder=None,
            merge_callback=merge_callback,
            batch_size=batch_size,
            merge_limit=merge_limit,
        )

    def _record_fused_submission(self, task: Any) -> None:
        """Record one successfully admitted fused task exactly once."""

        task_ordinals = self._task_ordinals(task)
        self._task_meta[str(task.batch_id)] = ("fused", task_ordinals)
        for ordinal in task_ordinals:
            self._fused_task_by_ordinal[ordinal] = task
            self._reserved_domains.add(self._domain_by_ordinal[ordinal])
        self._fused_batches_submitted += 1
        self._exact_submitted += len(task_ordinals)
        self._exact_batches_submitted += 1
        self._event_epoch += 1
        self._last_progress_kind = "fused_dispatch"
        if not self._exact_started_before_shape_terminal:
            self._exact_started_before_shape_terminal = True
            self._exact_first_shape_completed = self._shape_completed
            self._exact_first_timestamp_ms = (
                time.perf_counter() - self._started_at
            ) * 1000.0

    def _fill_frontier(self, deadline: Optional[float] = None) -> None:
        """Admit lowest-order candidates in master-affine bounded batches."""

        while not self.is_terminal and self.pool.stream_capacity > 0:
            pending = self._pending_fused_admission
            if pending is not None:
                while (
                    pending.next_task_index < len(pending.tasks)
                    and self.pool.stream_capacity > 0
                ):
                    if deadline is not None and time.perf_counter() >= float(deadline):
                        return
                    remaining = pending.tasks[pending.next_task_index:]
                    # Submit the whole currently fitting immutable tail to the
                    # real pool in one admission.  This lets the pool compute
                    # one deterministic LPT affinity plan for the bounded
                    # window instead of assigning every task while the queue
                    # is still only one item long.  Focused fake pools keep
                    # the older one-task contract.
                    if (
                        isinstance(self.pool, PersistentWorkerPool)
                        and len(remaining) > 1
                        and len(remaining) <= int(self.pool.stream_capacity)
                    ):
                        try:
                            self._stream_submit(remaining, deadline)
                        except PoolStreamBusyError:
                            return
                        for task in remaining:
                            self._record_fused_submission(task)
                        pending.next_task_index = len(pending.tasks)
                        continue
                    task = remaining[0]
                    try:
                        self._stream_submit(task, deadline)
                    except PoolStreamBusyError:
                        return
                    self._record_fused_submission(task)
                    # Advance the cursor immediately after successful pool
                    # admission so a resumed tick cannot duplicate this task.
                    pending.next_task_index += 1
                if pending.next_task_index < len(pending.tasks):
                    # Capacity or the modal deadline stopped admission.  The
                    # pending object is the concrete dependency for the next
                    # tick; do not build another prefix.
                    return
                self._pending_fused_admission = None
                if self.pool.stream_capacity <= 0:
                    return

            if deadline is not None and time.perf_counter() >= float(deadline):
                return
            # Build a bounded frontier window from every currently visible
            # ownership domain, then group it by master.  The old path chose
            # only the master of the first canonical ordinal; with merge_limit
            # one that reduced a production batch32 request to mostly one-pair
            # tasks.  Canonical merge still remains ordinal-only, so dispatch
            # of later masters is speculative and cannot change ownership.
            groups: dict[str, tuple[Any, list[int]]] = {}
            for ordinal in self.canonical_ordinals:
                domain = self._domain_by_ordinal[ordinal]
                queue = self._domain_queues[domain]
                if (
                    domain not in self._closed_domains
                    and domain not in self._reserved_domains
                    and queue
                    and queue[0] == ordinal
                ):
                    master = self.master_for_ordinal(ordinal)
                    marker = repr(master)
                    entry = groups.get(marker)
                    if entry is None:
                        entry = (master, [])
                        groups[marker] = entry
                    entry[1].append(ordinal)
            if not groups:
                return
            capacity = int(self.pool.stream_capacity)
            selected: list[int] = []
            selected_groups = 0
            for _marker, (_master, values) in groups.items():
                if selected_groups >= capacity:
                    break
                # A normal master group is kept together up to the configured
                # production cap.  Only an oversized master is split by the
                # builder; a small group is never padded with another master.
                selected.extend(values[: self.batch_size])
                selected_groups += 1
            if not selected:
                return
            admitted = tuple(selected)
            tasks = ()
            while admitted:
                built = self.fused_builder(admitted)
                raw_tasks = (
                    (built,)
                    if not isinstance(built, (tuple, list))
                    else tuple(built)
                )
                tasks = tuple(sorted(
                    raw_tasks,
                    key=lambda task: (
                        min(self._task_ordinals(task)),
                        str(getattr(task, "batch_id", "")),
                    ),
                ))
                if not tasks:
                    raise PipelineError("fused builder returned no task")
                flattened = tuple(
                    item
                    for task in tasks
                    for item in self._task_ordinals(task)
                )
                if (
                    len(flattened) != len(set(flattened))
                    or tuple(sorted(flattened)) != tuple(sorted(admitted))
                ):
                    raise PipelineError("fused builder changed frontier coverage")
                if len(tasks) <= capacity:
                    break
                # A builder can cross the modal deadline while returning a
                # valid split.  Preserve this immutable tuple instead of
                # rebuilding it on the next tick.  The submit cursor will
                # admit only the bounded prefix that fits now.
                if deadline is not None and time.perf_counter() >= float(deadline):
                    break
                if len(admitted) == 1:
                    raise PipelineError(
                        "fused frontier task split exceeds capacity: "
                        f"capacity={capacity} ordinal={admitted[0]} tasks={len(tasks)}"
                    )
                # A frame-aware builder may split a master group.  Shrink the
                # canonical prefix deterministically until the complete task
                # set fits the current 2*N admission window.
                admitted = admitted[: max(1, len(admitted) // 2)]
            if not admitted:
                raise PipelineError("fused frontier produced an empty admission")
            self._pending_fused_admission = _FusedPendingAdmission(
                admitted_ordinals=tuple(admitted),
                tasks=tuple(tasks),
            )
            # Submit through the pending path.  If the builder consumed the
            # deadline, the immutable admission remains for the next tick.

    def _handle_completion(self, completion: Any) -> None:
        batch_id = str(getattr(completion, "batch_id", ""))
        meta = self._task_meta.pop(batch_id, None)
        if meta is None or meta[0] != "fused":
            raise PipelineError("fused completion has an unknown task")
        expected = meta[1]
        result = completion.result
        outcomes = tuple(sorted(
            getattr(result, "outcomes", ()),
            key=lambda item: _ordinal(getattr(item, "pair_ordinal")),
        ))
        if tuple(item.pair_ordinal for item in outcomes) != tuple(sorted(expected)):
            raise PipelineError("fused completion coverage mismatch")
        self._event_epoch += 1
        self._fused_batches_completed += 1
        self._fused_graph_cache_builds += int(getattr(result, "graph_cache_builds", 0))
        self._fused_graph_cache_hits += int(getattr(result, "graph_cache_hits", 0))
        self._fused_graph_compute_ms += float(getattr(result, "graph_compute_ms", 0.0))
        self._fused_exact_compute_ms += float(getattr(result, "exact_compute_ms", 0.0))
        self._fused_shape_compute_ms += float(getattr(result, "shape_compute_ms", 0.0))
        self._fused_shape_cache_hits += int(getattr(result, "shape_cache_hits", 0))
        self._fused_lower_bound_checked = getattr(self, "_fused_lower_bound_checked", 0) + int(
            getattr(result, "lower_bound_checked", 0)
        )
        self._fused_lower_bound_rejected = getattr(self, "_fused_lower_bound_rejected", 0) + int(
            getattr(result, "lower_bound_rejected", 0)
        )
        self._fused_lower_bound_skipped = getattr(self, "_fused_lower_bound_skipped", 0) + int(
            getattr(result, "lower_bound_skipped", 0)
        )
        self._fused_lower_bound_graph_pairs_avoided = getattr(
            self, "_fused_lower_bound_graph_pairs_avoided", 0
        ) + int(
            getattr(result, "lower_bound_graph_pairs_avoided", 0)
        )
        result_min_ratio = float(getattr(result, "lower_bound_min_ratio", 0.0))
        result_max_ratio = float(getattr(result, "lower_bound_max_ratio", 0.0))
        if result_min_ratio > 0.0:
            self._fused_lower_bound_min_ratio = (
                result_min_ratio
                if getattr(self, "_fused_lower_bound_min_ratio", 0.0) <= 0.0
                else min(self._fused_lower_bound_min_ratio, result_min_ratio)
            )
        self._fused_lower_bound_max_ratio = max(
            getattr(self, "_fused_lower_bound_max_ratio", 0.0), result_max_ratio
        )
        for item in outcomes:
            ordinal = item.pair_ordinal
            self._shape_results[ordinal] = item.shape_result
            self._shape_completed += 1
            if bool(getattr(item.shape_result, "accepted", False)):
                self._shape_accepted += 1
                self._exact_total += 1
            else:
                self._shape_rejected += 1
            if item.exact_result is not None:
                self._exact_results[ordinal] = item.exact_result
                self._exact_completed += 1
                self._exact_accepted += int(bool(item.exact_result.accepted))
            self._last_progress_kind = "fused_complete"
        self.stage = "fused_merge"

    def fused_task_for_ordinal(self, ordinal: int) -> Any:
        """Return the admitted immutable task that produced one fused pair."""

        return self._fused_task_by_ordinal.get(int(ordinal))

    def _assert_no_active_stall(
        self,
        start_event_epoch: int,
        start_merged_index: int,
        deadline: Optional[float],
    ) -> None:
        """Fail fast when no worker/dependency can make the frontier advance."""

        if self.is_terminal:
            return
        if self._event_epoch != start_event_epoch or self._merged_index != start_merged_index:
            return
        if self._merged_index >= len(self.canonical_ordinals):
            return
        if int(self.pool.stream_capacity) <= 0:
            return
        if (
            self._task_meta
            or self._completion_buffer
            or self._pending_fused_admission is not None
        ):
            return
        ordinal = self.canonical_ordinals[self._merged_index]
        if ordinal in self._pruned:
            return
        shape_result = self._shape_results.get(ordinal)
        if shape_result is not None:
            if not bool(getattr(shape_result, "accepted", False)):
                return
            if ordinal in self._no_exact or ordinal in self._exact_results:
                return
        if ordinal in self._fused_task_by_ordinal:
            return
        raise PipelineError(
            "fused frontier made no progress: "
            f"capacity={self.pool.stream_capacity} "
            f"merged={self._merged_index}/{len(self.canonical_ordinals)} "
            f"reserved_domains={len(self._reserved_domains)} "
            f"open_domains={len(self._domain_queues) - len(self._closed_domains)}"
        )

    def _update_stage(self) -> None:
        if self.is_terminal:
            return
        if self._task_meta:
            self.stage = "fused_wait"
        elif self._merged_index < len(self.canonical_ordinals):
            self.stage = "fused_dispatch"
        else:
            self.stage = "fused_merge"


@dataclass(frozen=True)
class GroupFirstPairOutcome:
    """One completed direct exact job from the group-first route."""

    job: DirectExactJob
    exact_result: Any
    task: Any = None

    @property
    def pair_ordinal(self) -> int:
        return int(self.job.job_ordinal)

    @property
    def accepted(self) -> bool:
        return bool(getattr(self.exact_result, "accepted", False))


@dataclass(frozen=True)
class GroupFirstPipelineProgress:
    """Bounded, canonical progress for the two-phase group-first route."""

    stage: str
    pairs_total: int
    shape_submitted: int
    shape_completed: int
    shape_accepted: int
    shape_rejected: int
    exact_total: int
    exact_submitted: int
    exact_completed: int
    exact_accepted: int
    merged_pairs: int
    active_workers: int
    worker_count: int
    queue_depth: int
    retry_count: int
    elapsed_ms: float
    worker_pids: tuple[int, ...] = ()
    worker_distribution: tuple[tuple[int, int], ...] = ()
    startup_timings_ms: tuple[float, ...] = ()
    failure: str = ""
    cancelled: bool = False
    complete: bool = False
    # Group-first emits no ownership-pruned candidate outcomes; retain the
    # compatibility counter consumed by stack_tools' shared progress bridge.
    pruned_pairs: int = 0
    shape_batches_submitted: int = 0
    shape_batches_completed: int = 0
    exact_batches_submitted: int = 0
    grouping_comparisons_planned: int = 0
    grouping_comparisons_completed: int = 0
    grouping_groups: int = 0
    direct_exact_jobs_planned: int = 0
    direct_exact_jobs_completed: int = 0
    direct_exact_jobs_failed: int = 0
    exact_job_bound: int = 0
    last_progress_kind: str = ""
    retry_total: int = 0
    max_retry_per_batch: int = 0
    retried_batch_count: int = 0
    retry_failure_reason: str = ""
    retry_batches: tuple[tuple[str, int], ...] = ()
    poll_calls: int = 0
    no_progress_loops: int = 0
    event_epoch: int = 0
    frame_bytes_max: tuple[tuple[str, int], ...] = ()
    frame_bytes_total: tuple[tuple[str, int], ...] = ()
    fused_batches_submitted: int = 0
    fused_batches_completed: int = 0
    resident_exact_batches_submitted: int = 0
    resident_exact_batches_completed: int = 0
    resident_graph_cache_builds: int = 0
    resident_graph_cache_hits: int = 0
    resident_graph_compute_ms: float = 0.0
    resident_topology_cache_builds: int = 0
    resident_topology_cache_hits: int = 0
    resident_topology_compute_ms: float = 0.0
    resident_exact_compute_ms: float = 0.0
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

    # Compatibility fields consumed by stack_tools' existing report bridge.
    exact_started_before_shape_terminal: bool = False
    exact_first_shape_completed: int = 0
    exact_first_shape_total: int = 0
    exact_first_timestamp_ms: Optional[float] = None
    process_graph_worker_submitted: int = 0
    process_graph_worker_completed: int = 0
    process_graph_worker_operations: int = 0
    process_graph_worker_cache_hits: int = 0
    graph_tasks_submitted: int = 0
    graph_tasks_completed: int = 0
    graph_items_completed: int = 0
    graph_cache_hits: int = 0
    graph_event_epoch: int = 0
    graph_waiter_registrations: int = 0
    graph_waiter_dedup: int = 0
    resident_exact_frame_bytes: int = 0
    fused_frame_bytes: int = 0
    fused_frame_total_bytes: int = 0
    fused_graph_cache_builds: int = 0
    fused_graph_cache_hits: int = 0
    fused_graph_compute_ms: float = 0.0
    fused_exact_compute_ms: float = 0.0
    fused_shape_compute_ms: float = 0.0
    fused_shape_cache_hits: int = 0
    fused_lower_bound_checked: int = 0
    fused_lower_bound_rejected: int = 0
    fused_lower_bound_skipped: int = 0
    fused_lower_bound_graph_pairs_avoided: int = 0
    fused_lower_bound_min_ratio: float = 0.0
    fused_lower_bound_max_ratio: float = 0.0
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
    restart_pending: int = 0
    restart_states: tuple[tuple[int, str, str], ...] = ()


@dataclass(frozen=True)
class GroupFirstPipelineResult:
    complete: bool
    cancelled: bool
    generation_invalidated: bool
    plan: Optional[GroupFirstPlan]
    outcomes: tuple[GroupFirstPairOutcome, ...]
    result_digest: str
    progress: GroupFirstPipelineProgress
    failure: str = ""


class GroupFirstProcessPipeline:
    """Run Normal-equivalent grouping, then one resident exact job per member.

    The class is deliberately independent of Blender.  ``shape_task_builder``
    receives immutable :class:`GroupPairRequest` records and returns one or
    more validated shape tasks.  Once the frontier is complete,
    ``exact_task_builder`` receives the frozen direct-job tuple and returns
    resident exact tasks.  The same persistent stream and worker PIDs are used
    for both phases; only canonical ordinal merge is consumable.
    """

    STAGES = (
        "idle", "group_shape_dispatch", "group_shape_wait",
        "group_exact_dispatch", "group_exact_wait", "group_exact_merge",
        "done", "cancelled", "failed",
    )

    def __init__(
        self,
        pool: PersistentWorkerPool,
        frontier: GroupFirstFrontier,
        *,
        shape_task_builder: Callable[[tuple[GroupPairRequest, ...]], Any],
        exact_task_builder: Callable[[tuple[DirectExactJob, ...]], Any],
        merge_callback: Optional[Callable[[GroupFirstPairOutcome], Any]] = None,
        batch_size: int = 32,
    ) -> None:
        if not isinstance(frontier, GroupFirstFrontier):
            raise PayloadValidationError("group-first pipeline requires a frontier")
        if not callable(shape_task_builder) or not callable(exact_task_builder):
            raise TypeError("group-first task builders must be callable")
        if isinstance(batch_size, bool) or int(batch_size) <= 0:
            raise ValueError("group-first batch_size must be positive")
        self.pool = pool
        self.frontier = frontier
        self.shape_task_builder = shape_task_builder
        self.exact_task_builder = exact_task_builder
        self.merge_callback = merge_callback
        self.batch_size = int(batch_size)
        self.stage = "idle"
        self._started = False
        self._started_at = 0.0
        self._failure = ""
        self._cancelled = False
        self._generation_invalidated = False
        self._task_meta: dict[str, tuple[str, tuple[int, ...], Any]] = {}
        # Keep completions that could not be consumed within the current
        # bounded advance.  This is part of the resumable group-first state,
        # rather than an incidental attribute created only after polling.
        self._completion_buffer: deque[Any] = deque()
        self._completed_task_by_ordinal: dict[int, Any] = {}
        self._request_by_ordinal: dict[int, GroupPairRequest] = {}
        self._submitted_shape: set[int] = set()
        self._completed_shape: set[int] = set()
        self._shape_results: dict[int, GroupPairResult] = {}
        self._shape_accepted = 0
        self._shape_rejected = 0
        self._shape_batches_submitted = 0
        self._shape_batches_completed = 0
        self._shape_pairs_submitted = 0
        self._plan: Optional[GroupFirstPlan] = None
        self._jobs: tuple[DirectExactJob, ...] = ()
        self._job_by_ordinal: dict[int, DirectExactJob] = {}
        self._exact_cursor = 0
        self._submitted_exact: set[int] = set()
        self._completed_exact: dict[int, Any] = {}
        self._outcomes: list[GroupFirstPairOutcome] = []
        self._exact_accepted = 0
        self._exact_failed = 0
        self._exact_batches_submitted = 0
        self._exact_batches_completed = 0
        self._exact_pairs_submitted = 0
        self._merged_index = 0
        self._resident_topology_cache_builds = 0
        self._resident_topology_cache_hits = 0
        self._resident_topology_compute_ms = 0.0
        self._event_epoch = 0
        self._poll_calls = 0
        self._no_progress_loops = 0
        self._last_progress_kind = ""
        self._first_exact_timestamp_ms: Optional[float] = None
        self._first_exact_shape_completed = 0

    @property
    def is_terminal(self) -> bool:
        return self.stage in {"done", "cancelled", "failed"}

    @property
    def has_consumable_result(self) -> bool:
        return bool(
            self.stage == "done"
            and self._plan is not None
            and len(self._outcomes) == len(self._jobs)
        )

    @property
    def canonical_ordinals(self) -> tuple[int, ...]:
        if self._jobs:
            return tuple(job.job_ordinal for job in self._jobs)
        return tuple(sorted(self._request_by_ordinal))

    @property
    def plan(self) -> Optional[GroupFirstPlan]:
        return self._plan

    @property
    def shape_results(self) -> tuple[GroupPairResult, ...]:
        return tuple(self._shape_results[key] for key in sorted(self._shape_results))

    @property
    def exact_results(self) -> tuple[Any, ...]:
        return tuple(self._completed_exact[key] for key in sorted(self._completed_exact))

    @property
    def outcomes(self) -> tuple[GroupFirstPairOutcome, ...]:
        return tuple(self._outcomes)

    @staticmethod
    def _tasks(value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if callable(getattr(value, "validate", None)):
            return (value,)
        if isinstance(value, (tuple, list)):
            return tuple(value)
        return (value,)

    @staticmethod
    def _task_ordinals(task: Any) -> tuple[int, ...]:
        values = getattr(task, "pair_ordinals", ())
        return tuple(_ordinal(value) for value in values)

    def _deadline_expired(self, deadline: Optional[float]) -> bool:
        return deadline is not None and time.perf_counter() >= float(deadline)

    def _pool_capacity(self) -> int:
        try:
            return max(0, int(getattr(self.pool, "stream_capacity")))
        except (AttributeError, TypeError, ValueError):
            return 0

    def _submit_tasks(
        self,
        phase: str,
        tasks: tuple[Any, ...],
        *,
        deadline: Optional[float],
    ) -> bool:
        if not tasks:
            raise PipelineError("group-first %s builder returned no task" % phase)
        if len(tasks) > self._pool_capacity():
            raise PoolStreamBusyError("group-first %s admission exceeds capacity" % phase)
        for task in tasks:
            validate = getattr(task, "validate", None)
            if not callable(validate):
                raise PipelineError("group-first %s task has no validator" % phase)
            ordinals = self._task_ordinals(task)
            if not ordinals or len(set(ordinals)) != len(ordinals):
                raise PipelineError("group-first %s task has invalid coverage" % phase)
            batch_id = str(getattr(task, "batch_id", ""))
            if not batch_id or batch_id in self._task_meta:
                raise PipelineError("group-first task id is duplicated")
        try:
            try:
                self.pool.stream_submit(tasks, deadline=deadline)
            except TypeError:
                self.pool.stream_submit(tasks)
        except PoolStreamBusyError:
            return False
        for task in tasks:
            ordinals = self._task_ordinals(task)
            self._task_meta[str(task.batch_id)] = (phase, ordinals, task)
            if phase == "shape":
                self._submitted_shape.update(ordinals)
                self._shape_pairs_submitted += len(ordinals)
                self._shape_batches_submitted += 1
            else:
                self._submitted_exact.update(ordinals)
                self._exact_pairs_submitted += len(ordinals)
                self._exact_batches_submitted += 1
        self._event_epoch += 1
        self._last_progress_kind = "%s_dispatch" % phase
        return True

    def _admit_shape(self, deadline: Optional[float]) -> None:
        while not self.is_terminal and self._pool_capacity() > 0:
            if self._deadline_expired(deadline):
                return
            pending = tuple(
                request
                for request in self.frontier.pending_requests()
                if request.pair_ordinal not in self._submitted_shape
            )
            for request in self.frontier.all_requests():
                self._request_by_ordinal[request.pair_ordinal] = request
            if not pending:
                return
            capacity = self._pool_capacity()
            take_count = min(len(pending), max(1, capacity * self.batch_size))
            while take_count:
                selected = pending[:take_count]
                tasks = self._tasks(self.shape_task_builder(tuple(selected)))
                if tasks and len(tasks) <= capacity:
                    flattened = tuple(
                        ordinal for task in tasks for ordinal in self._task_ordinals(task)
                    )
                    if tuple(sorted(flattened)) != tuple(
                        sorted(request.pair_ordinal for request in selected)
                    ) or len(flattened) != len(set(flattened)):
                        raise PipelineError("group-first shape builder changed request coverage")
                    if self._submit_tasks("shape", tasks, deadline=deadline):
                        break
                if take_count == 1:
                    raise PipelineError("one group-first shape request cannot be admitted")
                take_count = max(1, take_count // 2)
            else:
                return
            if not tasks:
                raise PipelineError("group-first shape builder returned no task")
            if self._pool_capacity() <= 0:
                return

    def _handle_shape_completion(self, completion: Any, meta: tuple[str, tuple[int, ...], Any]) -> None:
        _phase, expected, task = meta
        result = getattr(completion, "result", None)
        if result is None or not bool(getattr(result, "complete", False)):
            raise PipelineError("group-first shape completion is incomplete")
        pairs = tuple(sorted(getattr(result, "pair_results", ()), key=lambda item: _ordinal(getattr(item, "pair_ordinal"))))
        if tuple(getattr(item, "pair_ordinal") for item in pairs) != tuple(sorted(expected)):
            raise PipelineError("group-first shape completion coverage mismatch")
        for item in pairs:
            ordinal = _ordinal(item.pair_ordinal)
            request = self._request_by_ordinal.get(ordinal)
            if request is None:
                pair_tasks = getattr(task, "pair_tasks", ())
                pair = next((value for value in pair_tasks if value.pair_ordinal == ordinal), None)
                if pair is None:
                    raise PipelineError("group-first shape result references unknown request")
                request = GroupPairRequest(
                    pair_ordinal=ordinal,
                    representative_key=pair.master_key,
                    candidate_key=pair.member_key,
                    bucket_key=(),
                )
                self._request_by_ordinal[ordinal] = request
            group_result = (
                item
                if isinstance(item, GroupPairResult)
                else GroupPairResult.from_match_result(request, item)
            )
            self.frontier.consume(group_result)
            self._completed_task_by_ordinal[ordinal] = task
            self._shape_results[ordinal] = group_result
            self._completed_shape.add(ordinal)
            self._shape_accepted += int(bool(group_result.accepted))
            self._shape_rejected += int(not bool(group_result.accepted))
        self._event_epoch += 1
        self._last_progress_kind = "shape_complete"
        self._shape_batches_completed += 1

    def _start_exact(self) -> None:
        if self._plan is not None:
            return
        if not self.frontier.complete:
            return
        self._plan = self.frontier.finalize()
        self._jobs = tuple(self._plan.direct_exact_jobs)
        self._job_by_ordinal = {job.job_ordinal: job for job in self._jobs}
        self.stage = "group_exact_dispatch"
        self._event_epoch += 1
        self._last_progress_kind = "group_plan_complete"

    def _admit_exact(self, deadline: Optional[float]) -> None:
        if self._plan is None:
            return
        while not self.is_terminal and self._pool_capacity() > 0:
            if self._deadline_expired(deadline):
                return
            remaining = self._jobs[self._exact_cursor:]
            if not remaining:
                return
            capacity = self._pool_capacity()
            take_count = min(len(remaining), max(1, capacity * self.batch_size))
            while take_count:
                selected = tuple(remaining[:take_count])
                tasks = self._tasks(self.exact_task_builder(selected))
                if tasks and len(tasks) <= capacity:
                    flattened = tuple(
                        ordinal for task in tasks for ordinal in self._task_ordinals(task)
                    )
                    if tuple(sorted(flattened)) != tuple(
                        sorted(job.job_ordinal for job in selected)
                    ) or len(flattened) != len(set(flattened)):
                        raise PipelineError("group-first exact builder changed job coverage")
                    if self._submit_tasks("exact", tasks, deadline=deadline):
                        self._exact_cursor += sum(len(self._task_ordinals(task)) for task in tasks)
                        break
                if take_count == 1:
                    raise PipelineError("one group-first exact job cannot be admitted")
                take_count = max(1, take_count // 2)
            else:
                return
            if not tasks:
                raise PipelineError("group-first exact builder returned no task")
            if self._pool_capacity() <= 0:
                return

    def _handle_exact_completion(self, completion: Any, meta: tuple[str, tuple[int, ...], Any]) -> None:
        _phase, expected, task = meta
        result = getattr(completion, "result", None)
        if result is None or not bool(getattr(result, "complete", False)):
            raise PipelineError("group-first exact completion is incomplete")
        pairs = tuple(sorted(getattr(result, "pair_results", ()), key=lambda item: _ordinal(getattr(item, "pair_ordinal"))))
        if tuple(getattr(item, "pair_ordinal") for item in pairs) != tuple(sorted(expected)):
            raise PipelineError("group-first exact completion coverage mismatch")
        for item in pairs:
            ordinal = _ordinal(item.pair_ordinal)
            job = self._job_by_ordinal.get(ordinal)
            if job is None:
                raise PipelineError("group-first exact result references unknown job")
            self._completed_exact[ordinal] = item
            self._completed_task_by_ordinal[ordinal] = task
            self._exact_accepted += int(bool(getattr(item, "accepted", False)))
            self._exact_failed += int(not bool(getattr(item, "accepted", False)))
        self._event_epoch += 1
        self._last_progress_kind = "exact_complete"
        self._exact_batches_completed += 1
        if self._first_exact_timestamp_ms is None:
            self._first_exact_timestamp_ms = (time.perf_counter() - self._started_at) * 1000.0
            self._first_exact_shape_completed = len(self._completed_shape)

    def _merge_prefix(
        self,
        deadline: Optional[float],
        limit: Optional[int] = None,
    ) -> None:
        end = len(self._jobs)
        if limit is not None:
            end = min(end, self._merged_index + max(1, int(limit)))
        while self._merged_index < end:
            if self._deadline_expired(deadline):
                return
            ordinal = self._jobs[self._merged_index].job_ordinal
            exact_result = self._completed_exact.get(ordinal)
            if exact_result is None:
                return
            outcome = GroupFirstPairOutcome(
                job=self._jobs[self._merged_index],
                exact_result=exact_result,
                task=self._task_for_ordinal(ordinal),
            )
            if self.merge_callback is not None:
                self.merge_callback(outcome)
            self._outcomes.append(outcome)
            self._merged_index += 1
            self._event_epoch += 1
            self._last_progress_kind = "canonical_merge"

    def _task_for_ordinal(self, ordinal: int) -> Any:
        completed = self._completed_task_by_ordinal.get(int(ordinal))
        if completed is not None:
            return completed
        for _phase, ordinals, task in self._task_meta.values():
            if ordinal in ordinals:
                return task
        # Completed tasks are removed from _task_meta; retain the task on the
        # outcome path through the pool's completion buffer when available.
        return None

    def _finish_stream_if_ready(self, deadline: Optional[float]) -> None:
        if self._plan is None or self._exact_cursor < len(self._jobs):
            return
        if self._task_meta:
            return
        if self._completion_buffer:
            return
        if not bool(getattr(self.pool, "_stream_closed", False)):
            try:
                self.pool.stream_finish(deadline=deadline)
            except TypeError:
                self.pool.stream_finish()
        if not getattr(self.pool, "is_terminal", False):
            return
        if self._merged_index == len(self._jobs):
            self.stage = "done"

    def _fail(self, message: Any) -> None:
        self._failure = str(message)[:1024]
        self._shape_results.clear()
        self._completed_exact.clear()
        self._completed_task_by_ordinal.clear()
        self._outcomes.clear()
        self._task_meta.clear()
        self._completion_buffer.clear()
        self._merged_index = 0
        self.stage = "failed"
        if self._started and not getattr(self.pool, "is_terminal", False):
            try:
                self.pool.cancel(timeout=1.0)
            except Exception:
                pass

    def start(self, *, deadline: Optional[float] = None) -> GroupFirstPipelineProgress:
        if self._started:
            return self.progress()
        self._started = True
        self._started_at = time.perf_counter()
        try:
            self.pool.begin_stream()
            self.stage = "group_shape_dispatch"
            self._admit_shape(deadline)
            if self.frontier.complete:
                self._start_exact()
        except Exception as exc:
            self._fail("group-first start failed: %s" % exc)
        return self.progress()

    def advance(
        self,
        timeout: float = 0.0,
        *,
        merge_limit: Optional[int] = None,
        deadline: Optional[float] = None,
    ) -> GroupFirstPipelineProgress:
        if not self._started:
            self.start(deadline=deadline)
        if self.is_terminal:
            return self.progress()
        before_epoch = self._event_epoch
        before_merged = self._merged_index
        try:
            self._poll_calls += 1
            if self._completion_buffer:
                completions = tuple(self._completion_buffer)
                self._completion_buffer.clear()
            elif deadline is None or not self._deadline_expired(deadline):
                try:
                    completions = self.pool.poll_stream(timeout, deadline=deadline)
                except TypeError:
                    completions = self.pool.poll_stream(timeout)
            else:
                completions = ()
            completion_tuple = tuple(completions)
            completion_limit = None if merge_limit is None else max(1, int(merge_limit))
            for index, completion in enumerate(completion_tuple):
                if completion_limit is not None and index >= completion_limit:
                    self._completion_buffer.extend(completion_tuple[index:])
                    break
                if deadline is not None and self._deadline_expired(deadline):
                    self._completion_buffer.extend(completion_tuple[index:])
                    break
                batch_id = str(getattr(completion, "batch_id", ""))
                meta = self._task_meta.pop(batch_id, None)
                if meta is None:
                    raise PipelineError("group-first completion has unknown task")
                if meta[0] == "shape":
                    self._handle_shape_completion(completion, meta)
                else:
                    self._handle_exact_completion(completion, meta)
            pool_failure = str(getattr(self.pool, "_failure", "") or "")
            if pool_failure:
                raise PipelineError(pool_failure)
            if self._plan is None:
                self._admit_shape(deadline)
                self._start_exact()
            if self._plan is not None:
                self._admit_exact(deadline)
                self._merge_prefix(deadline, merge_limit)
                self._finish_stream_if_ready(deadline)
                if self._plan is not None and self._merged_index < len(self._jobs):
                    self.stage = "group_exact_wait" if self._task_meta else "group_exact_dispatch"
            if (
                self._event_epoch == before_epoch
                and self._merged_index == before_merged
                and not self._task_meta
                and not self.is_terminal
            ):
                self._no_progress_loops += 1
        except Exception as exc:
            self._fail(exc)
        return self.progress()

    def cancel(
        self,
        *,
        timeout: float = 1.0,
        nonblocking: bool = False,
    ) -> Optional[GroupFirstPipelineResult]:
        self._cancelled = True
        self._shape_results.clear()
        self._completed_exact.clear()
        self._completed_task_by_ordinal.clear()
        self._outcomes.clear()
        self._task_meta.clear()
        self._completion_buffer.clear()
        self._merged_index = 0
        try:
            if self._started:
                if nonblocking:
                    self.pool.begin_cancel()
                    self.stage = "cancelling"
                    if not bool(getattr(self.pool, "cancel_complete", False)):
                        return None
                else:
                    self.pool.cancel(timeout=timeout)
        finally:
            if not nonblocking or bool(getattr(self.pool, "cancel_complete", True)):
                self.stage = "cancelled"
        return self.final_result()

    def advance_cancel(
        self,
        *,
        deadline: Optional[float] = None,
    ) -> Optional[GroupFirstPipelineResult]:
        if self.stage != "cancelling":
            return self.final_result() if self.is_terminal else None
        self.pool.advance_cancel(deadline=deadline)
        if bool(getattr(self.pool, "cancel_complete", False)):
            self.stage = "cancelled"
            return self.final_result()
        return None

    def invalidate_generation(self, generation: int) -> GroupFirstPipelineResult:
        self._generation_invalidated = True
        self._cancelled = True
        self._shape_results.clear()
        self._completed_exact.clear()
        self._outcomes.clear()
        self._task_meta.clear()
        self._completion_buffer.clear()
        self._merged_index = 0
        try:
            if self._started:
                self.pool.invalidate_generation(generation)
        finally:
            self.stage = "cancelled"
        return self.final_result()

    def close(self) -> None:
        try:
            self.pool.close()
        finally:
            self._task_meta.clear()
            self._completion_buffer.clear()
            self._shape_results.clear()
            self._completed_exact.clear()
            self._completed_task_by_ordinal.clear()
            self._outcomes.clear()
            if not self.is_terminal:
                self._cancelled = True
                self.stage = "cancelled"

    def progress(self) -> GroupFirstPipelineProgress:
        pool_progress = self.pool.progress() if self._started else None
        elapsed = 0.0 if not self._started else (time.perf_counter() - self._started_at) * 1000.0
        if pool_progress is None:
            active = queue = retries = 0
            worker_count = int(getattr(self.pool, "worker_count", 0))
            pids = distribution = startup = ()
            frame_max = frame_total = ()
        else:
            active = int(getattr(pool_progress, "active_workers", 0))
            queue = int(getattr(self.pool, "stream_queue_depth", getattr(self.pool, "queue_depth", 0)))
            queue = max(queue, len(self._completion_buffer))
            retries = int(getattr(pool_progress, "retry_count", 0))
            worker_count = int(getattr(self.pool, "worker_count", 0))
            pids = tuple(getattr(self.pool, "worker_pids", ()))
            distribution = tuple(getattr(self.pool, "worker_task_distribution", ()))
            startup = tuple(getattr(self.pool, "startup_timings_ms", ()))
            frame_max = tuple(getattr(pool_progress, "frame_bytes_max", ()))
            frame_total = tuple(getattr(pool_progress, "frame_bytes_total", ()))
        plan = self._plan
        return GroupFirstPipelineProgress(
            stage=self.stage,
            pairs_total=(
                len(self._jobs) if plan is not None else int(self.frontier.comparisons_planned)
            ),
            shape_submitted=self._shape_pairs_submitted,
            shape_completed=len(self._completed_shape),
            shape_accepted=self._shape_accepted,
            shape_rejected=self._shape_rejected,
            exact_total=len(self._jobs),
            exact_submitted=self._exact_pairs_submitted,
            exact_completed=len(self._completed_exact),
            exact_accepted=self._exact_accepted,
            merged_pairs=self._merged_index,
            active_workers=active,
            worker_count=worker_count,
            queue_depth=queue,
            retry_count=retries,
            elapsed_ms=elapsed,
            worker_pids=pids,
            worker_distribution=distribution,
            startup_timings_ms=startup,
            failure=self._failure,
            cancelled=self._cancelled,
            complete=self.stage == "done",
            grouping_comparisons_planned=self.frontier.comparisons_planned,
            grouping_comparisons_completed=self.frontier.comparisons_completed,
            grouping_groups=len(self.frontier.groups),
            direct_exact_jobs_planned=len(self._jobs),
            direct_exact_jobs_completed=len(self._outcomes),
            direct_exact_jobs_failed=self._exact_failed,
            exact_job_bound=(plan.exact_job_bound if plan is not None else 0),
            shape_batches_submitted=self._shape_batches_submitted,
            shape_batches_completed=self._shape_batches_completed,
            exact_batches_submitted=self._exact_batches_submitted,
            last_progress_kind=self._last_progress_kind,
            retry_total=int(getattr(pool_progress, "retry_total", retries)) if pool_progress else 0,
            max_retry_per_batch=int(getattr(pool_progress, "max_retry_per_batch", 0)) if pool_progress else 0,
            retried_batch_count=int(getattr(pool_progress, "retried_batch_count", 0)) if pool_progress else 0,
            retry_failure_reason=str(getattr(pool_progress, "retry_failure_reason", "") or "") if pool_progress else "",
            retry_batches=tuple(getattr(pool_progress, "retry_batches", ())) if pool_progress else (),
            poll_calls=self._poll_calls,
            no_progress_loops=self._no_progress_loops,
            event_epoch=self._event_epoch,
            frame_bytes_max=frame_max,
            frame_bytes_total=frame_total,
            fused_batches_submitted=(
                self._shape_batches_submitted + self._exact_batches_submitted
            ),
            fused_batches_completed=(
                self._shape_batches_completed + self._exact_batches_completed
            ),
            resident_exact_batches_submitted=self._exact_batches_submitted,
            resident_exact_batches_completed=self._exact_batches_completed,
            nearest_attempted=int(
                getattr(pool_progress, "nearest_attempted", 0)
            ) if pool_progress else 0,
            nearest_accepted=int(
                getattr(pool_progress, "nearest_accepted", 0)
            ) if pool_progress else 0,
            nearest_fallback=int(
                getattr(pool_progress, "nearest_fallback", 0)
            ) if pool_progress else 0,
            nearest_max_seed_distance=float(
                getattr(pool_progress, "nearest_max_seed_distance", 0.0)
            ) if pool_progress else 0.0,
            nearest_mean_seed_distance=float(
                getattr(pool_progress, "nearest_mean_seed_distance", 0.0)
            ) if pool_progress else 0.0,
            nearest_ambiguity_count=int(
                getattr(pool_progress, "nearest_ambiguity_count", 0)
            ) if pool_progress else 0,
            nearest_tie_count=int(
                getattr(pool_progress, "nearest_tie_count", 0)
            ) if pool_progress else 0,
            nearest_compute_ms=float(
                getattr(pool_progress, "nearest_compute_ms", 0.0)
            ) if pool_progress else 0.0,
            nearest_distance_evaluations=int(
                getattr(pool_progress, "nearest_distance_evaluations", 0)
            ) if pool_progress else 0,
            nearest_assignment_nodes=int(
                getattr(pool_progress, "nearest_assignment_nodes", 0)
            ) if pool_progress else 0,
            nearest_assignment_cap=int(
                getattr(pool_progress, "nearest_assignment_cap", 0)
            ) if pool_progress else 0,
            nearest_fallback_reasons=tuple(
                getattr(pool_progress, "nearest_fallback_reasons", ())
            ) if pool_progress else (),
            nearest_distance_lookups=int(
                getattr(pool_progress, "nearest_distance_lookups", 0)
            ) if pool_progress else 0,
            nearest_distance_cache_hits=int(
                getattr(pool_progress, "nearest_distance_cache_hits", 0)
            ) if pool_progress else 0,
            nearest_distance_cache_misses=int(
                getattr(pool_progress, "nearest_distance_cache_misses", 0)
            ) if pool_progress else 0,
            nearest_operations_used=int(
                getattr(pool_progress, "nearest_operations_used", 0)
            ) if pool_progress else 0,
            graph_rejected_before_nearest=int(
                getattr(pool_progress, "graph_rejected_before_nearest", 0)
            ) if pool_progress else 0,
            nearest_seed_missing=int(
                getattr(pool_progress, "nearest_seed_missing", 0)
            ) if pool_progress else 0,
            nearest_fast_miss=int(
                getattr(pool_progress, "nearest_fast_miss", 0)
            ) if pool_progress else 0,
            exact_fallback_calls=int(
                getattr(pool_progress, "exact_fallback_calls", 0)
            ) if pool_progress else 0,
            exact_primary_calls=int(
                getattr(pool_progress, "exact_primary_calls", 0)
            ) if pool_progress else 0,
            exact_started_before_shape_terminal=bool(self._first_exact_timestamp_ms is not None),
            exact_first_shape_completed=self._first_exact_shape_completed,
            exact_first_shape_total=len(self._completed_shape),
            exact_first_timestamp_ms=self._first_exact_timestamp_ms,
            resident_graph_cache_builds=int(getattr(pool_progress, "resident_graph_cache_builds", 0)) if pool_progress else 0,
            resident_graph_cache_hits=int(getattr(pool_progress, "resident_graph_cache_hits", 0)) if pool_progress else 0,
            resident_graph_compute_ms=float(getattr(pool_progress, "resident_graph_compute_ms", 0.0)) if pool_progress else 0.0,
            resident_topology_cache_builds=int(getattr(pool_progress, "resident_topology_cache_builds", 0)) if pool_progress else 0,
            resident_topology_cache_hits=int(getattr(pool_progress, "resident_topology_cache_hits", 0)) if pool_progress else 0,
            resident_topology_compute_ms=float(getattr(pool_progress, "resident_topology_compute_ms", 0.0)) if pool_progress else 0.0,
            resident_exact_compute_ms=float(getattr(pool_progress, "resident_exact_compute_ms", 0.0)) if pool_progress else 0.0,
            restart_pending=int(getattr(pool_progress, "restart_pending", 0)) if pool_progress else 0,
            restart_states=tuple(getattr(pool_progress, "restart_states", ())) if pool_progress else (),
        )

    def final_result(self) -> GroupFirstPipelineResult:
        if self.has_consumable_result:
            digest = stable_digest(tuple(
                (
                    outcome.job.job_ordinal,
                    outcome.job.master_key,
                    outcome.job.member_key,
                    _semantic_result_wire(outcome.exact_result),
                )
                for outcome in self._outcomes
            ))
            outcomes = tuple(self._outcomes)
        else:
            digest = ""
            outcomes = ()
        return GroupFirstPipelineResult(
            complete=self.has_consumable_result,
            cancelled=self._cancelled,
            generation_invalidated=self._generation_invalidated,
            plan=self._plan,
            outcomes=outcomes,
            result_digest=digest,
            progress=self.progress(),
            failure=self._failure,
        )


__all__ = [
    "PipelineError", "PipelineCancelled", "PipelinePairOutcome", "PipelineProgress",
    "PipelineResult", "FrontierDecision", "FrontierProcessPipeline", "FusedProcessPipeline",
    "ProcessPipeline", "GroupFirstPairOutcome", "GroupFirstPipelineProgress",
    "GroupFirstPipelineResult", "GroupFirstProcessPipeline",
]
