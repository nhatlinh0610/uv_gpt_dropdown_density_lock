from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from uv_gpt.pro_process_pipeline import FrontierDecision, FusedProcessPipeline
from uv_gpt.pro_process_pool import plan_fused_affinity


@dataclass(frozen=True)
class _Task:
    batch_id: str
    pair_ordinals: tuple[int, ...]
    operation_kind: str = "fused"

    def validate(self):
        if not self.pair_ordinals:
            raise ValueError("empty task")

    def estimate_frame(self):
        return SimpleNamespace(frame_bytes=128 + len(self.pair_ordinals) * 16)


@dataclass(frozen=True)
class _Completion:
    task: _Task
    result: object
    worker_index: int
    batch_id: str
    sequence: int = 1


class _Pool:
    def __init__(self, worker_count=1):
        self.worker_count = worker_count
        self.pending = []
        self.closed = False
        self._terminal = False

    @property
    def is_terminal(self):
        return self._terminal

    @property
    def stream_capacity(self):
        return max(0, 2 * self.worker_count - len(self.pending))

    @property
    def stream_queue_depth(self):
        return len(self.pending)

    @property
    def queue_depth(self):
        return len(self.pending)

    @property
    def worker_pids(self):
        return tuple(range(9000, 9000 + self.worker_count))

    @property
    def worker_task_distribution(self):
        return ()

    @property
    def startup_timings_ms(self):
        return ()

    def begin_stream(self):
        self.pending.clear()
        self.closed = False
        self._terminal = False

    def stream_submit(self, task, *, deadline=None):
        del deadline
        if len(self.pending) >= 2 * self.worker_count:
            raise RuntimeError("queue overflow")
        self.pending.append(task)
        return (task.batch_id,)

    def stream_finish(self, *, deadline=None):
        del deadline
        self.closed = True
        if not self.pending:
            self._terminal = True

    def poll_stream(self, _timeout=0.0, *, deadline=None):
        del deadline
        if not self.pending:
            if self.closed:
                self._terminal = True
            return ()
        task = self.pending.pop(0)
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
        return SimpleNamespace(
            active_workers=int(bool(self.pending)),
            retry_count=0,
            retry_total=0,
            max_retry_per_batch=0,
            retried_batch_count=0,
            retry_failure_reason="",
            retry_batches=(),
        )

    def cancel(self, timeout=1.0):
        del timeout
        self.pending.clear()
        self.closed = True
        self._terminal = True

    def close(self):
        self.cancel()


class R2ATests(unittest.TestCase):
    def test_frontier_coalesces_visible_master_groups(self):
        pool = _Pool(worker_count=1)
        built = []

        def builder(ordinals):
            values = tuple(ordinals)
            built.append(values)
            groups = {}
            for ordinal in values:
                groups.setdefault(ordinal % 2, []).append(ordinal)
            return tuple(
                _Task("fused-%d" % items[0], tuple(items))
                for items in groups.values()
            )

        pipeline = FusedProcessPipeline(
            pool,
            tuple(range(8)),
            domain_for_ordinal=lambda ordinal: ordinal,
            master_for_ordinal=lambda ordinal: ordinal % 2,
            fused_builder=builder,
            merge_callback=lambda _outcome: FrontierDecision(
                False, True, "owned"
            ),
            batch_size=8,
            merge_limit=1,
        )
        pipeline.start()
        self.assertEqual(len(built), 1)
        self.assertEqual(set(built[0]), set(range(8)))
        self.assertEqual(len(pool.pending), 2)
        for _ in range(40):
            pipeline.advance()
            if pipeline.is_terminal:
                break
        self.assertTrue(pipeline.final_result().complete, pipeline.progress())
        self.assertEqual(
            tuple(item.pair_ordinal for item in pipeline.outcomes),
            tuple(range(8)),
        )

    def test_lpt_affinity_is_stable_and_uses_all_workers(self):
        options = SimpleNamespace(max_search=1024)
        tasks = []
        for ordinal, loop_count in enumerate((1, 4, 8, 16, 2, 6, 12, 3)):
            pair = SimpleNamespace(
                master_key=(ordinal % 3,),
                member_key=(ordinal + 20,),
                master_loop_keys=tuple(range(loop_count)),
                member_loop_keys=tuple(range(loop_count + 1)),
                exact_options=options,
                prefilter=None,
            )
            tasks.append(
                SimpleNamespace(
                    operation_kind="fused",
                    batch_id="task-%02d" % ordinal,
                    pair_tasks=(pair,),
                    pair_ordinals=(ordinal,),
                    master_key=pair.master_key,
                )
            )
        first = plan_fused_affinity(tasks, 4)
        second = plan_fused_affinity(tuple(reversed(tasks)), 4)
        self.assertEqual(first, second)
        self.assertEqual({worker for _batch, worker in first}, {0, 1, 2, 3})


if __name__ == "__main__":
    unittest.main()
