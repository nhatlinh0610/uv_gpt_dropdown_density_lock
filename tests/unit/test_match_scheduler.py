"""Focused contract tests for the pure-numeric MATCH-03 scheduler."""

import importlib.util
import operator
from dataclasses import dataclass
from pathlib import Path
import time
import unittest


MODULE_PATH = Path(__file__).resolve().parents[2] / "uv_gpt" / "match_scheduler.py"
SPEC = importlib.util.spec_from_file_location("uv_gpt_match_scheduler_test_module", MODULE_PATH)
SCHEDULER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(SCHEDULER)


def process_negate(value):
    """Top-level worker so Windows spawn can serialize the process prototype."""

    return operator.neg(value)


@dataclass(frozen=True)
class FrozenPayload:
    value: int


@dataclass
class MutablePayload:
    value: int


class MatchSchedulerTests(unittest.TestCase):
    def test_module_has_no_blender_imports(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import bpy", source)
        self.assertNotIn("import bmesh", source)

    def test_auto_uses_full_fit_count_for_real_cc_case(self):
        decision = SCHEDULER.choose_backend(
            batch_size=576,
            full_fit_count=1,
            pure_python=True,
            independent=True,
        )
        self.assertEqual(decision.backend, SCHEDULER.BACKEND_SINGLE)
        self.assertEqual(decision.effective_batch_size, 1)
        self.assertEqual(decision.worker_count, 1)
        self.assertEqual(decision.reason, "effective_batch_below_thread_threshold")

    def test_pure_python_defaults_single_even_for_large_batch(self):
        decision = SCHEDULER.choose_backend(
            batch_size=128,
            pure_python=True,
            independent=True,
        )
        self.assertEqual(decision.backend, SCHEDULER.BACKEND_SINGLE)
        self.assertEqual(decision.reason, "pure_python_gil_default_single")

        opted_in = SCHEDULER.choose_backend(
            batch_size=32,
            pure_python=True,
            policy=SCHEDULER.SchedulerPolicy(
                allow_gil_threads=True,
                thread_min_batch_size=32,
                logical_cpu_count=16,
            ),
        )
        self.assertEqual(opted_in.backend, SCHEDULER.BACKEND_THREAD)

    def test_process_is_explicit_benchmark_only_policy(self):
        disabled = SCHEDULER.choose_backend(
            backend=SCHEDULER.BACKEND_PROCESS,
            batch_size=128,
            pure_python=True,
            payload_serializable=True,
        )
        self.assertEqual(disabled.backend, SCHEDULER.BACKEND_SINGLE)
        self.assertEqual(disabled.reason, "process_benchmark_disabled")

        enabled = SCHEDULER.choose_backend(
            backend=SCHEDULER.BACKEND_PROCESS,
            batch_size=128,
            pure_python=True,
            payload_serializable=True,
            policy=SCHEDULER.SchedulerPolicy(
                allow_process_benchmark=True,
                logical_cpu_count=4,
            ),
        )
        self.assertEqual(enabled.backend, SCHEDULER.BACKEND_PROCESS)
        self.assertTrue(enabled.benchmark_only)
        self.assertEqual(enabled.worker_count, 4)

        dependent = SCHEDULER.choose_backend(
            batch_size=128,
            pure_python=False,
            independent=False,
        )
        self.assertEqual(dependent.backend, SCHEDULER.BACKEND_SINGLE)
        self.assertEqual(dependent.reason, "non_independent_batch")

    def test_thread_threshold_and_worker_cap(self):
        policy = SCHEDULER.SchedulerPolicy(
            allow_gil_threads=True,
            thread_min_batch_size=4,
            logical_cpu_count=32,
            max_workers=99,
        )
        below = SCHEDULER.choose_backend(
            batch_size=3,
            pure_python=True,
            policy=policy,
        )
        self.assertEqual(below.backend, SCHEDULER.BACKEND_SINGLE)
        self.assertEqual(below.reason, "effective_batch_below_thread_threshold")

        capped = SCHEDULER.choose_backend(
            batch_size=17,
            pure_python=True,
            policy=policy,
        )
        self.assertEqual(capped.backend, SCHEDULER.BACKEND_THREAD)
        self.assertEqual(capped.worker_count, 8)

        cpu_limited = SCHEDULER.choose_backend(
            batch_size=17,
            pure_python=True,
            policy=SCHEDULER.SchedulerPolicy(
                allow_gil_threads=True,
                thread_min_batch_size=4,
                logical_cpu_count=2,
            ),
        )
        self.assertEqual(cpu_limited.worker_count, 2)

        numpy_thread = SCHEDULER.choose_backend(
            batch_size=32,
            pure_python=False,
            numpy_enabled=True,
            policy=SCHEDULER.SchedulerPolicy(
                thread_min_batch_size=32,
                logical_cpu_count=4,
            ),
        )
        self.assertEqual(numpy_thread.backend, SCHEDULER.BACKEND_THREAD)
        self.assertEqual(numpy_thread.numpy_oversubscription, "not_claimed")

    def test_immutable_payload_validation(self):
        SCHEDULER.validate_numeric_payload((1, 2.5, (3, None, "metadata")))
        SCHEDULER.validate_numeric_payload(FrozenPayload(4))
        self.assertTrue(SCHEDULER.is_valid_numeric_payload(FrozenPayload(4)))
        self.assertFalse(SCHEDULER.is_valid_numeric_payload([1, 2]))
        self.assertFalse(SCHEDULER.is_valid_numeric_payload({"value": 1}))
        with self.assertRaises(TypeError):
            SCHEDULER.validate_numeric_payload(MutablePayload(4))

    def test_single_and_thread_results_keep_input_order(self):
        single = SCHEDULER.schedule_numeric_batch(
            (3, 1, 2),
            lambda value: value * value,
        )
        self.assertEqual(single.values, (9, 1, 4))
        self.assertEqual(tuple(item.index for item in single.results), (0, 1, 2))
        self.assertTrue(single.diagnostics.ordering_preserved)

        def delayed_square(value):
            time.sleep((3 - value) * 0.002)
            return value * value

        thread = SCHEDULER.schedule_numeric_batch(
            (0, 1, 2, 3),
            delayed_square,
            policy=SCHEDULER.SchedulerPolicy(
                allow_gil_threads=True,
                thread_min_batch_size=1,
                logical_cpu_count=4,
            ),
            backend=SCHEDULER.BACKEND_THREAD,
        )
        self.assertEqual(thread.decision.backend, SCHEDULER.BACKEND_THREAD)
        self.assertEqual(thread.values, (0, 1, 4, 9))
        self.assertEqual(tuple(item.index for item in thread.results), (0, 1, 2, 3))
        self.assertTrue(thread.diagnostics.executor_created)
        self.assertTrue(thread.diagnostics.executor_shutdown)

    def test_deterministic_tie_break_prefers_key_then_input_order(self):
        selection = SCHEDULER.select_best(
            ("first", "second", "third"),
            score_key=lambda _value: 1.0,
            tie_key=lambda value: {"first": 2, "second": 1, "third": 1}[value],
        )
        self.assertIsNotNone(selection)
        self.assertEqual(selection.value, "second")
        self.assertEqual(selection.index, 1)

        repeated = SCHEDULER.select_best(
            ("a", "b"),
            score_key=lambda _value: 1.0,
            tie_key=lambda _value: 0,
        )
        self.assertEqual(repeated.index, 0)

    def test_process_contract_and_executor_cleanup(self):
        report = SCHEDULER.validate_process_contract((3, 1, 2), process_negate)
        self.assertTrue(report.valid)
        self.assertEqual(report.item_count, 3)
        self.assertGreater(report.payload_bytes, 0)
        self.assertGreater(report.worker_bytes, 0)

        result = SCHEDULER.run_process_benchmark(
            (3, 1, 2),
            process_negate,
            policy=SCHEDULER.SchedulerPolicy(logical_cpu_count=2),
        )
        self.assertEqual(result.decision.backend, SCHEDULER.BACKEND_PROCESS)
        self.assertEqual(result.values, (-3, -1, -2))
        self.assertEqual(result.decision.worker_count, 2)
        self.assertTrue(result.decision.benchmark_only)
        self.assertTrue(result.diagnostics.executor_created)
        self.assertTrue(result.diagnostics.executor_shutdown)
        self.assertTrue(result.diagnostics.process_benchmark_only)
        self.assertFalse(result.diagnostics.numpy_oversubscription_claimed)

        with self.assertRaises(SCHEDULER.ProcessContractError):
            SCHEDULER.validate_process_contract(([1, 2],), process_negate)
        self.assertFalse(SCHEDULER.is_process_serializable((1,), lambda value: value))

    def test_cancellation_discards_current_and_remaining_generation(self):
        token = SCHEDULER.CancellationToken(generation=7)

        def cancel_on_first(value):
            if value == 0:
                token.cancel()
            return value

        result = SCHEDULER.schedule_numeric_batch(
            (0, 1, 2),
            cancel_on_first,
            token=token,
            generation=7,
            current_generation=7,
        )
        self.assertTrue(result.diagnostics.cancellation_requested)
        self.assertLess(result.diagnostics.completed_count, 3)
        self.assertTrue(all(item.status != "completed" for item in result.results))

    def test_stale_generation_discards_result_before_return(self):
        current = [11]
        token = SCHEDULER.CancellationToken(generation=11)

        def supersede_on_first(value):
            if value == 0:
                current[0] = 12
            return value

        result = SCHEDULER.schedule_numeric_batch(
            (0, 1),
            supersede_on_first,
            token=token,
            generation=11,
            current_generation=lambda: current[0],
        )
        self.assertTrue(result.diagnostics.stale_generation)
        self.assertGreater(result.diagnostics.stale_count, 0)
        self.assertTrue(all(item.status == "stale" for item in result.results))


if __name__ == "__main__":
    unittest.main()
