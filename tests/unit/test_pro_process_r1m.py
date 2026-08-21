"""Focused MC4-R1M liveness and frame-accounting regressions."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import unittest

import uv_gpt.pro_process_pipeline as pipeline_module
from uv_gpt.pro_process_pipeline import FrontierDecision, FusedProcessPipeline
from uv_gpt.pro_process_pool import PersistentWorkerPool, PoolProgress


@dataclass(frozen=True)
class _Task:
    batch_id: str
    pair_ordinals: tuple[int, ...]
    operation_kind: str = "fused"

    def validate(self):
        if not self.pair_ordinals:
            raise ValueError("empty task")

    def estimate_frame(self):
        return SimpleNamespace(frame_bytes=100 + 10 * len(self.pair_ordinals))


@dataclass(frozen=True)
class _Completion:
    task: _Task
    result: object
    worker_index: int
    batch_id: str
    sequence: int = 1


class _FusedPool:
    def __init__(self, worker_count=1):
        self.worker_count = worker_count
        self._pending = []
        self._closed = False
        self._terminal = False
        self.max_queue = 0

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
        return tuple(range(7000, 7000 + self.worker_count))

    @property
    def worker_task_distribution(self):
        return ((0, sum(len(task.pair_ordinals) for task in self._pending)),)

    @property
    def startup_timings_ms(self):
        return ()

    def begin_stream(self):
        self._pending.clear()
        self._closed = False
        self._terminal = False

    def stream_submit(self, task, *, deadline=None):
        del deadline
        if len(self._pending) >= 2 * self.worker_count:
            raise RuntimeError("queue overflow")
        self._pending.append(task)
        self.max_queue = max(self.max_queue, len(self._pending))
        return (task.batch_id,)

    def stream_finish(self, *, deadline=None):
        del deadline
        self._closed = True
        if not self._pending:
            self._terminal = True

    def poll_stream(self, _timeout=0.0, *, deadline=None):
        del deadline
        if not self._pending:
            if self._closed:
                self._terminal = True
            return ()
        task = self._pending.pop(0)
        outcomes = tuple(
            SimpleNamespace(
                pair_ordinal=ordinal,
                shape_result=SimpleNamespace(accepted=True),
                exact_result=SimpleNamespace(accepted=True),
            )
            for ordinal in task.pair_ordinals
        )
        result = SimpleNamespace(
            outcomes=outcomes,
            graph_cache_builds=0,
            graph_cache_hits=0,
            graph_compute_ms=0.0,
            exact_compute_ms=0.0,
            shape_compute_ms=0.0,
            shape_cache_hits=0,
        )
        return (_Completion(task, result, 0, task.batch_id),)

    def progress(self):
        return PoolProgress(
            pairs_total=0,
            pairs_done=0,
            exact_count=0,
            batches_total=len(self._pending),
            batches_done=0,
            active_workers=int(bool(self._pending)),
            retry_count=0,
            elapsed_ms=0.0,
            frame_bytes_max=(("fused", 140),),
            frame_bytes_total=(("fused", 280),),
        )

    def cancel(self, timeout=1.0):
        del timeout
        self._pending.clear()
        self._closed = True
        self._terminal = True

    def invalidate_generation(self, _generation):
        self.cancel()

    def close(self):
        self.cancel()


class _FakeClock:
    def __init__(self):
        self.value = 0.0

    def perf_counter(self):
        return self.value

    def advance(self, amount):
        self.value += float(amount)


class R1MTests(unittest.TestCase):
    def test_split_builder_shrinks_canonical_prefix_and_completes(self):
        pool = _FusedPool(worker_count=1)
        calls = []

        def builder(ordinals):
            values = tuple(ordinals)
            calls.append(values)
            chunks = tuple(
                _Task(
                    "fused-%d" % chunk[0],
                    tuple(chunk),
                )
                for chunk in (values[index:index + 2] for index in range(0, len(values), 2))
            )
            return chunks[0] if len(chunks) == 1 else chunks

        pipeline = FusedProcessPipeline(
            pool,
            tuple(range(8)),
            domain_for_ordinal=lambda ordinal: ordinal,
            master_for_ordinal=lambda _ordinal: "master",
            fused_builder=builder,
            merge_callback=lambda _outcome: FrontierDecision(
                False, True, "owned"
            ),
            batch_size=8,
            merge_limit=1,
        )
        pipeline.start()
        for _ in range(100):
            pipeline.advance()
            if pipeline.is_terminal:
                break

        self.assertTrue(pipeline.final_result().complete, pipeline.progress())
        self.assertEqual(
            tuple(item.pair_ordinal for item in pipeline.outcomes),
            tuple(range(8)),
        )
        self.assertLessEqual(pool.max_queue, 2)
        self.assertTrue(any(len(values) < 8 for values in calls))
        self.assertLess(pipeline.progress().no_progress_loops, 10)

    def test_deadline_after_build_retains_tasks_and_resumes_once(self):
        """A built split is submitted over later ticks without rebuilding."""

        pool = _FusedPool(worker_count=1)
        clock = _FakeClock()
        calls = []
        original_time = pipeline_module.time

        def builder(ordinals):
            values = tuple(ordinals)
            calls.append(values)
            # Simulate a builder that crosses the current modal deadline.
            clock.advance(2.0)
            return tuple(
                _Task("deadline-fused-%d" % ordinal, (ordinal,))
                for ordinal in values
            )

        pipeline_module.time = clock
        try:
            pipeline = FusedProcessPipeline(
                pool,
                tuple(range(4)),
                domain_for_ordinal=lambda ordinal: ordinal,
                master_for_ordinal=lambda _ordinal: "master",
                fused_builder=builder,
                merge_callback=lambda _outcome: FrontierDecision(
                    False, True, "owned"
                ),
                batch_size=4,
                merge_limit=1,
            )
            pipeline.start(deadline=1.0)
            self.assertEqual(calls, [(0, 1, 2, 3)])
            self.assertIsNotNone(pipeline._pending_fused_admission)
            self.assertEqual(pool._pending, [])
            self.assertEqual(pipeline._reserved_domains, set())

            # The next tick admits only the bounded prefix.  The immutable
            # tail remains pending and no builder call is repeated.
            pipeline.advance()
            pending = pipeline._pending_fused_admission
            self.assertIsNotNone(pending)
            self.assertEqual(pending.next_task_index, 2)
            self.assertEqual(len(pool._pending), 2)
            self.assertEqual(pipeline._reserved_domains, {0, 1})

            for _ in range(30):
                pipeline.advance()
                if pipeline.is_terminal:
                    break
            self.assertTrue(pipeline.final_result().complete, pipeline.progress())
            self.assertEqual(
                tuple(item.pair_ordinal for item in pipeline.outcomes),
                (0, 1, 2, 3),
            )
            self.assertEqual(calls, [(0, 1, 2, 3)])
            self.assertIsNone(pipeline._pending_fused_admission)
            self.assertLess(pipeline.progress().no_progress_loops, 5)
        finally:
            pipeline_module.time = original_time

    def test_pending_admission_cancel_clears_unsubmitted_tail(self):
        pool = _FusedPool(worker_count=1)
        clock = _FakeClock()
        original_time = pipeline_module.time
        pipeline_module.time = clock
        try:
            pipeline = FusedProcessPipeline(
                pool,
                tuple(range(3)),
                domain_for_ordinal=lambda ordinal: ordinal,
                master_for_ordinal=lambda _ordinal: "master",
                fused_builder=lambda ordinals: tuple(
                    _Task("cancel-fused-%d" % ordinal, (ordinal,))
                    for ordinal in ordinals
                ),
                merge_callback=lambda _outcome: FrontierDecision(
                    False, True, "owned"
                ),
                batch_size=3,
                merge_limit=1,
            )
            pipeline.start(deadline=-1.0)
            self.assertIsNone(pipeline._pending_fused_admission)
            # Build on a live tick, then force the fake clock past the
            # deadline from inside the builder.
            def late_builder(ordinals):
                clock.advance(2.0)
                return tuple(
                    _Task("cancel-late-%d" % ordinal, (ordinal,))
                    for ordinal in ordinals
                )
            pipeline.fused_builder = late_builder
            pipeline.advance(deadline=1.0)
            self.assertIsNotNone(pipeline._pending_fused_admission)
            result = pipeline.cancel(timeout=0.1)
            self.assertFalse(result.complete)
            self.assertEqual(result.outcomes, ())
            self.assertIsNone(pipeline._pending_fused_admission)
            self.assertEqual(pool._pending, [])
        finally:
            pipeline_module.time = original_time

    def test_frame_accounting_keeps_max_and_total_separate(self):
        pool = PersistentWorkerPool(
            1,
            python_executable="not-started-in-this-unit-test",
            session_nonce="r1m-frame",
        )
        task_a = _Task("a", (0,))
        task_b = _Task("b", (1, 2))
        pool._record_frame_estimate(task_a, SimpleNamespace(frame_bytes=120))
        pool._record_frame_estimate(task_b, SimpleNamespace(frame_bytes=240))
        self.assertEqual(pool.frame_bytes_max_by_operation, (("fused", 240),))
        self.assertEqual(pool.frame_bytes_total_by_operation, (("fused", 360),))


if __name__ == "__main__":
    unittest.main()
