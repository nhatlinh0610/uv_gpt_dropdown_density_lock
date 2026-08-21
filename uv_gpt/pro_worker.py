"""Bounded pure-Python correspondence worker for Align Similar Pro.

The worker boundary is intentionally narrow.  A caller may submit only the
immutable :class:`IslandGraph` values and scalar correspondence options.  No
Blender object, BMesh element, UV layer, context, or session is ever passed to
the executor.  The session remains responsible for polling, snapshot checks,
staging, and applying UV writes on Blender's main thread.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import time

from . import topology_correspondence


@dataclass(frozen=True)
class WorkerOutcome:
    """A completed immutable worker result or compact error."""

    token: int
    result: object = None
    error: str = None
    wall_ms: float = 0.0
    compute_ms: float = 0.0


@dataclass(frozen=True)
class _WorkerPayload:
    result: object = None
    error: str = None
    compute_ms: float = 0.0


def _run_correspondence(
    master_graph,
    candidate_graph,
    allow_flipping,
    match_scale,
    tolerance,
    max_search,
    cooperative_yield_every,
):
    """Executor entry point containing no Blender-facing code."""

    # Let the submitter return to Blender's modal loop before the pure-Python
    # search starts competing for the GIL.  This is only a scheduling yield;
    # the worker remains one-at-a-time and receives the same immutable values.
    time.sleep(0.001)
    compute_started = time.perf_counter()
    try:
        result = topology_correspondence.find_correspondence(
            master_graph,
            candidate_graph,
            allow_flipping=bool(allow_flipping),
            match_scale=bool(match_scale),
            tolerance=float(tolerance),
            max_search=int(max_search),
            cooperative_yield_every=int(cooperative_yield_every),
        )
    except BaseException as exc:  # return compact failure to the main thread
        return _WorkerPayload(
            error=str(exc) or type(exc).__name__,
            compute_ms=(time.perf_counter() - compute_started) * 1000.0,
        )
    return _WorkerPayload(
        result=result,
        compute_ms=(time.perf_counter() - compute_started) * 1000.0,
    )


class ProCorrespondenceWorker:
    """One-at-a-time correspondence executor with non-blocking polling."""

    def __init__(
        self,
        max_workers=1,
        executor_factory=None,
        cooperative_yield_every=64,
    ):
        if int(max_workers) != 1:
            raise ValueError("Align Similar Pro requires exactly one worker.")
        self.max_workers = 1
        self._executor_factory = executor_factory or ThreadPoolExecutor
        try:
            self.cooperative_yield_every = max(0, int(cooperative_yield_every))
        except (TypeError, ValueError):
            self.cooperative_yield_every = 0
        self._executor = None
        self._future = None
        self._token = None
        self._next_token = 1
        self._submitted_at = None
        self._closed = False
        self._submissions = 0
        self._completions = 0
        self._discards = 0
        self._errors = 0
        self._in_flight_peak = 0
        self._future_wall_ms = 0.0
        self._max_future_wall_ms = 0.0
        self._worker_compute_ms = 0.0
        self._max_worker_compute_ms = 0.0
        self._shutdown = False

    @property
    def in_flight(self):
        return self._future is not None

    @property
    def in_flight_token(self):
        return self._token

    def in_flight_wall_ms(self):
        if self._future is None or self._submitted_at is None:
            return 0.0
        return max(0.0, (time.perf_counter() - self._submitted_at) * 1000.0)

    def _ensure_executor(self):
        if self._executor is None:
            self._executor = self._executor_factory(
                max_workers=1,
                thread_name_prefix="uv-gpt-pro",
            )

    def submit(
        self,
        master_graph,
        candidate_graph,
        *,
        allow_flipping,
        match_scale,
        tolerance,
        max_search,
        cooperative_yield_every=None,
    ):
        """Submit one immutable graph pair and return its deterministic token."""

        if self._closed or self._shutdown:
            raise RuntimeError("Align Similar Pro worker is closed.")
        if self._future is not None:
            raise RuntimeError("Align Similar Pro worker already has an in-flight pair.")
        if not isinstance(master_graph, topology_correspondence.IslandGraph):
            raise TypeError("master_graph must be an immutable IslandGraph.")
        if not isinstance(candidate_graph, topology_correspondence.IslandGraph):
            raise TypeError("candidate_graph must be an immutable IslandGraph.")
        if cooperative_yield_every is None:
            cooperative_yield_every = self.cooperative_yield_every
        try:
            cooperative_yield_every = max(0, int(cooperative_yield_every))
        except (TypeError, ValueError):
            cooperative_yield_every = self.cooperative_yield_every

        self._ensure_executor()
        token = self._next_token
        self._next_token += 1
        self._token = token
        self._submitted_at = time.perf_counter()
        try:
            self._future = self._executor.submit(
                _run_correspondence,
                master_graph,
                candidate_graph,
                bool(allow_flipping),
                bool(match_scale),
                float(tolerance),
                int(max_search),
                cooperative_yield_every,
            )
        except Exception:
            self._future = None
            self._token = None
            self._submitted_at = None
            raise
        self._submissions += 1
        self._in_flight_peak = max(self._in_flight_peak, 1)
        return token

    def poll(self):
        """Return a completed outcome, or ``None`` without waiting."""

        future = self._future
        if future is None or not future.done():
            return None
        token = self._token
        submitted_at = self._submitted_at
        wall_ms = 0.0
        if submitted_at is not None:
            wall_ms = max(0.0, (time.perf_counter() - submitted_at) * 1000.0)
        result = None
        error = None
        compute_ms = 0.0
        try:
            # ``done()`` was true above, so this call does not block.
            payload = future.result()
            if isinstance(payload, _WorkerPayload):
                result = payload.result
                error = payload.error
                compute_ms = float(payload.compute_ms)
                if error:
                    self._errors += 1
            else:  # compatibility for a deliberately injected test executor
                result = payload
                compute_ms = wall_ms
        except BaseException as exc:  # keep session cleanup deterministic
            error = str(exc) or type(exc).__name__
            self._errors += 1
        finally:
            self._future = None
            self._token = None
            self._submitted_at = None
        self._completions += 1
        self._future_wall_ms += wall_ms
        self._max_future_wall_ms = max(self._max_future_wall_ms, wall_ms)
        self._worker_compute_ms += compute_ms
        self._max_worker_compute_ms = max(self._max_worker_compute_ms, compute_ms)
        return WorkerOutcome(
            token=token,
            result=result,
            error=error,
            wall_ms=wall_ms,
            compute_ms=compute_ms,
        )

    def discard(self):
        """Discard a pending/running result without waiting for the worker."""

        future = self._future
        if future is None:
            return False
        try:
            future.cancel()
        finally:
            self._future = None
            self._token = None
            self._submitted_at = None
            self._discards += 1
            # The executor is made unusable after a discard.  This prevents a
            # running task that ignored cancel() from ever overlapping a new
            # submission and preserves deterministic one-in-flight ordering.
            self._closed = True
        return True

    def shutdown(self):
        """Release the executor without waiting on a running pure-Python task."""

        if self._shutdown:
            return
        self.discard()
        self._closed = True
        executor = self._executor
        self._executor = None
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:  # pragma: no cover - older Python compatibility
                executor.shutdown(wait=False)
        self._shutdown = True

    def snapshot(self):
        """Return compact counters suitable for normal operator evidence."""

        return {
            "worker_submissions": self._submissions,
            "worker_completions": self._completions,
            "worker_discards": self._discards,
            "worker_errors": self._errors,
            "worker_in_flight_peak": self._in_flight_peak,
            "future_wall_ms": self._future_wall_ms,
            "max_future_wall_ms": self._max_future_wall_ms,
            "worker_compute_ms": self._worker_compute_ms,
            "max_worker_compute_ms": self._max_worker_compute_ms,
            "cooperative_yield_every": self.cooperative_yield_every,
            "worker_shutdown": self._shutdown,
        }
