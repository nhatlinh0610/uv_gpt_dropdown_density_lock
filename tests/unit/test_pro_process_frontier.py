"""MC4-C1 bounded canonical frontier tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
import unittest

from uv_gpt.pro_process_pipeline import (
    FrontierDecision,
    FrontierProcessPipeline,
)
from uv_gpt.pro_process_pool import PoolProgress
from test_align_similar_selected import _load_stack_tools_for_pure_helpers


STACK_TOOLS = _load_stack_tools_for_pure_helpers()


@dataclass(frozen=True)
class _Identity:
    session_nonce: str = "frontier-test"
    generation: int = 0


@dataclass(frozen=True)
class _PairResult:
    pair_ordinal: int
    accepted: bool

    def to_wire(self):
        return (self.pair_ordinal, self.accepted)


@dataclass(frozen=True)
class _Result:
    pair_results: tuple[_PairResult, ...]


@dataclass(frozen=True)
class _Task:
    batch_id: str
    pair_ordinals: tuple[int, ...]
    stage: str
    identity: _Identity = _Identity()

    @property
    def pair_tasks(self):
        return self.pair_ordinals

    def validate(self):
        if not self.pair_ordinals:
            raise ValueError("empty test task")

    def estimate_frame(self):
        return type("Estimate", (), {"frame_bytes": 128})()


@dataclass(frozen=True)
class _Completion:
    task: _Task
    result: _Result
    worker_index: int
    batch_id: str
    sequence: int = 1


class _FakeStreamPool:
    def __init__(self, worker_count=2, *, reverse=False, shape_accept=None, exact_accept=None):
        self.worker_count = worker_count
        self.reverse = bool(reverse)
        self.shape_accept = dict(shape_accept or {})
        self.exact_accept = dict(exact_accept or {})
        self._pending = []
        self._terminal = False
        self._closed = False
        self._submitted = []
        self.max_queue = 0
        self._pairs_done = 0
        self._batches_done = 0
        self._distribution = {}

    @property
    def is_terminal(self):
        return self._terminal

    @property
    def stream_queue_depth(self):
        return len(self._pending)

    @property
    def stream_capacity(self):
        return max(0, 2 * self.worker_count - len(self._pending))

    @property
    def worker_pids(self):
        return tuple(range(1000, 1000 + self.worker_count))

    @property
    def worker_task_distribution(self):
        return tuple(sorted(self._distribution.items()))

    @property
    def startup_timings_ms(self):
        return tuple(0.1 for _ in range(self.worker_count))

    def begin_stream(self):
        self._pending.clear()
        self._submitted.clear()
        self._terminal = False
        self._closed = False
        return self.progress()

    def stream_submit(self, task):
        if len(self._pending) >= 2 * self.worker_count:
            raise RuntimeError("fake bounded queue overflow")
        self._pending.append(task)
        self._submitted.append(task)
        self.max_queue = max(self.max_queue, len(self._pending))
        return (task.batch_id,)

    def stream_finish(self):
        self._closed = True
        if not self._pending:
            self._terminal = True
        return self.progress()

    def poll_stream(self, _timeout=0.0):
        if not self._pending:
            if self._closed:
                self._terminal = True
            return ()
        index = -1 if self.reverse else 0
        task = self._pending.pop(index)
        accepted = self.shape_accept if task.stage == "shape" else self.exact_accept
        result = _Result(tuple(
            _PairResult(ordinal, bool(accepted.get(ordinal, True)))
            for ordinal in task.pair_ordinals
        ))
        self._pairs_done += len(result.pair_results)
        self._batches_done += 1
        self._distribution[0] = self._distribution.get(0, 0) + len(result.pair_results)
        return (_Completion(task, result, 0, task.batch_id),)

    def progress(self):
        return PoolProgress(
            pairs_total=0,
            pairs_done=self._pairs_done,
            exact_count=0,
            batches_total=len(self._submitted),
            batches_done=self._batches_done,
            active_workers=bool(self._pending),
            retry_count=0,
            elapsed_ms=0.0,
        )

    def cancel(self, timeout=1.0):
        self._pending.clear()
        self._terminal = True
        self._closed = True

    def invalidate_generation(self, _generation):
        self.cancel()

    def close(self):
        self.cancel()


def _run_frontier(reverse=False):
    # Two candidates per domain.  Domain 0 and 2 accept their first candidate;
    # domain 1 rejects once, then accepts its released next candidate.
    ordinals = tuple(range(6))
    pool = _FakeStreamPool(
        worker_count=2,
        reverse=reverse,
        exact_accept={0: True, 2: False, 3: True, 4: True},
    )
    tasks = []

    def shape_builder(values):
        task = _Task("shape-%02d" % values[0], tuple(values), "shape")
        tasks.append(task)
        return task

    def exact_builder(shape_result):
        return _Task(
            "exact-%02d" % shape_result.pair_ordinal,
            (shape_result.pair_ordinal,),
            "exact",
        )

    def merge(outcome):
        if outcome.pruned:
            return FrontierDecision(False, False, "pruned")
        if outcome.exact_result is not None and outcome.exact_result.accepted:
            return FrontierDecision(False, True, "owned")
        return FrontierDecision(True, False, "try_next")

    pipeline = FrontierProcessPipeline(
        pool,
        ordinals,
        domain_for_ordinal=lambda ordinal: ordinal // 2,
        shape_builder=shape_builder,
        exact_builder=exact_builder,
        merge_callback=merge,
        batch_size=2,
        merge_limit=1,
    )
    pipeline.start()
    for _ in range(200):
        pipeline.advance(0.0, merge_limit=1)
        if pipeline.is_terminal:
            break
    return pipeline, pool


class FrontierTests(unittest.TestCase):
    def test_frontier_bound_and_exact_overlap_before_shape_terminal(self):
        pipeline, pool = _run_frontier()
        self.assertTrue(pipeline.is_terminal)
        self.assertEqual(pipeline.stage, "done")
        self.assertTrue(pipeline.final_result().complete)
        self.assertLessEqual(pool.max_queue, 4)
        self.assertTrue(pipeline.progress().exact_started_before_shape_terminal)
        self.assertGreater(pipeline.progress().exact_submitted, 0)

    def test_out_of_order_completion_has_canonical_digest_and_pruning(self):
        forward, _pool = _run_frontier(reverse=False)
        reverse, _pool = _run_frontier(reverse=True)
        self.assertEqual(forward.final_result().result_digest, reverse.final_result().result_digest)
        self.assertEqual(
            tuple(item.pair_ordinal for item in forward.outcomes),
            tuple(range(6)),
        )
        self.assertEqual(
            tuple(item.pair_ordinal for item in reverse.outcomes),
            tuple(range(6)),
        )
        self.assertEqual(
            tuple(item.pair_ordinal for item in forward.outcomes if item.pruned),
            (1, 5),
        )

    def test_cancel_and_foreign_completion_expose_no_outcomes(self):
        pipeline, pool = _run_frontier()
        result = pipeline.cancel()
        self.assertFalse(result.complete)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.outcomes, ())
        self.assertTrue(pool.is_terminal)

        class ForeignPool(_FakeStreamPool):
            def poll_stream(self, _timeout=0.0):
                return (_Completion(_Task("foreign", (99,), "shape"), _Result((_PairResult(99, True),)), 0, "foreign"),)

        foreign = ForeignPool(worker_count=1)
        bad = FrontierProcessPipeline(
            foreign,
            (0,),
            domain_for_ordinal=lambda _ordinal: 0,
            shape_builder=lambda values: _Task("shape-bad", tuple(values), "shape"),
        )
        bad.start()
        bad.advance()
        self.assertEqual(bad.stage, "failed")
        self.assertEqual(bad.final_result().outcomes, ())

    def test_deadline_preserves_unconsumed_completion_tuple_tail(self):
        pool = _FakeStreamPool(worker_count=2)
        pipeline = FrontierProcessPipeline(
            pool,
            (0, 1),
            domain_for_ordinal=lambda ordinal: ordinal,
            shape_builder=lambda values: _Task(
                "shape-%02d" % values[0], tuple(values), "shape"
            ),
            exact_builder=lambda _shape: False,
            merge_callback=lambda _outcome: FrontierDecision(
                True, False, "shape_terminal"
            ),
            batch_size=1,
            merge_limit=1,
        )
        pipeline.start()
        first = pool.poll_stream()[0]
        second = pool.poll_stream()[0]
        pipeline._completion_buffer.extend((first, second))

        # Simulate a modal tick that has no remaining budget.  Both results
        # must survive for the next tick; neither may disappear merely because
        # the pool retired both worker slots in one poll cycle.
        pipeline.advance(deadline=time.perf_counter())
        self.assertEqual(len(pipeline._completion_buffer), 2)
        for _ in range(20):
            pipeline.advance()
            if pipeline.is_terminal:
                break
        self.assertTrue(pipeline.final_result().complete)
        self.assertEqual(
            tuple(item.pair_ordinal for item in pipeline.outcomes), (0, 1)
        )

    def test_progress_guard_uses_deadline_and_stateful_stall_detection(self):
        source = Path(STACK_TOOLS.__file__).read_text(encoding="utf-8")
        self.assertNotIn("guard > 100000", source)
        self.assertIn("monotonic_deadline", source)
        session = object.__new__(STACK_TOOLS._ProAlignSession)
        session.done = False
        session.started = time.perf_counter()
        session.time_budget_ms = 1000.0
        session._process_io_timeout = 0.001
        session._progress_stall_grace_ms = 1.0
        session.active_elapsed_ms = 0.0
        session.report = {}
        session._process_pipeline = None
        session._inflight = None
        session._progress_marker = lambda: ("stuck",)
        session._fail = lambda exc: (
            setattr(session, "done", True),
            session.report.__setitem__("error", str(exc)),
        )
        session._request_timeout = lambda stage: (
            setattr(session, "done", True),
            session.report.__setitem__("timeout_stage", stage),
        )
        session.step = lambda **_kwargs: None
        STACK_TOOLS._ProAlignSession.run_to_completion(session)
        self.assertIn("error", session.report)
        self.assertIn("stalled", session.report["error"])


if __name__ == "__main__":
    unittest.main()
