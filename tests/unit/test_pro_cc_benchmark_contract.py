"""Pure contract tests for the cc benchmark wrapper oracle."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WRAPPER_PATH = PROJECT_ROOT / "tests" / "blender" / "profile_pro_process_cc_benchmark.py"


def _load_wrapper():
    saved_argv = sys.argv[:]
    sys.argv = [
        str(WRAPPER_PATH),
        "--",
        "--mode",
        "VERIFIED_NEAREST_ONLY",
        "--worker-count",
        "1",
    ]
    try:
        spec = importlib.util.spec_from_file_location(
            "t2r4e_cc_benchmark_contract", WRAPPER_PATH
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("unable to load benchmark wrapper")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.argv = saved_argv


WRAPPER = _load_wrapper()


class ProCcBenchmarkContractTests(unittest.TestCase):
    def setUp(self):
        WRAPPER.MODE = "VERIFIED_NEAREST_ONLY"
        WRAPPER.LOOP_KEYS_BY_KEY = {}

    def test_mapping_reuses_master_loops_across_members_but_is_bijective_per_member(self):
        master = (99,)
        member_a = (1,)
        member_b = (2,)
        WRAPPER.LOOP_KEYS_BY_KEY = {
            master: ((100,), (101,)),
            member_a: ((10,), (11,)),
            member_b: ((20,), (21,)),
        }
        result = {
            "groups": [
                {
                    "master_key": list(master),
                    "member_keys": [list(member_a), list(member_b)],
                    "mapping_pairs": [
                        [[[10], [100]], [[11], [101]]],
                        [[[20], [100]], [[21], [101]]],
                    ],
                }
            ]
        }

        contract = WRAPPER._mapping_contract(result)

        self.assertTrue(contract["passed"], contract["issues"])
        self.assertEqual(contract["groups"][0]["mapping_pair_count"], 4)
        self.assertTrue(all(row["bijective"] for row in contract["groups"][0]["member_rows"]))

    def test_fast_accounting_ignores_seeded_plan_count_and_preserves_phase_counts(self):
        result = {
            "correspondence_mode": "VERIFIED_NEAREST_ONLY",
            "mode": "VERIFIED_NEAREST_ONLY",
            "worker_mode": WRAPPER.WORKER_MODE,
            "process_fused": True,
            "process_group_first": True,
            "process_nearest_accounting_valid": True,
            "process_thread_caps": {"OMP_NUM_THREADS": "1"},
            "direct_exact_jobs_completed": 4,
            "direct_exact_jobs_planned": 4,
            "process_graph_rejected_before_nearest": 1,
            "process_nearest_seed_missing": 0,
            "process_nearest_attempted": 3,
            "process_nearest_accepted": 2,
            "process_nearest_fast_miss": 1,
            "process_nearest_seeded_jobs_planned": 4,
            "process_nearest_seedless_jobs_planned": 0,
            "process_nearest_fallback": 1,
            "process_nearest_fallback_exact_calls": 0,
            "process_exact_fallback_calls": 0,
            "process_exact_primary_calls": 0,
            "process_exact_pairs_submitted": 3,
            "process_exact_pairs_completed": 3,
            "process_exact_accepted": 2,
        }

        contract = WRAPPER._mode_contract(result, requested_mode="VERIFIED_NEAREST_ONLY")

        self.assertTrue(contract["passed"], contract["issues"])
        self.assertEqual(
            contract["pair_phase_counters"]["direct_resident_group_pairs_completed"],
            3,
        )
        self.assertIn("not CorrespondenceSearch", contract["pair_phase_counter_semantics"])

    def test_fast_worker_branch_is_source_proven_zero_exact_constructor(self):
        worker_source = (
            PROJECT_ROOT / "uv_gpt" / "pro_process_worker.py"
        ).read_text(encoding="utf-8")
        start = worker_source.index(
            "def _correspondence_for_mode("
        )
        fast_start = worker_source.index(
            "if mode == CORRESPONDENCE_MODE_VERIFIED_NEAREST_ONLY:", start
        )
        hybrid_start = worker_source.index(
            "if mode == CORRESPONDENCE_MODE_HYBRID:", fast_start
        )
        fast_branch = worker_source[fast_start:hybrid_start]

        self.assertIn("_verified_nearest.find_verified_nearest", fast_branch)
        self.assertNotIn("_topology.find_correspondence", fast_branch)

    def test_exact_allows_seeded_plan_but_requires_zero_nearest_calls(self):
        result = {
            "correspondence_mode": "EXACT_ONLY",
            "mode": "EXACT_ONLY",
            "worker_mode": WRAPPER.WORKER_MODE,
            "process_fused": True,
            "process_group_first": True,
            "process_nearest_accounting_valid": True,
            "process_thread_caps": {"OMP_NUM_THREADS": "1"},
            "direct_exact_jobs_completed": 4,
            "direct_exact_jobs_planned": 4,
            "process_graph_rejected_before_nearest": 1,
            "process_nearest_seed_missing": 0,
            "process_nearest_attempted": 0,
            "process_nearest_accepted": 0,
            "process_nearest_fast_miss": 0,
            "process_nearest_seeded_jobs_planned": 4,
            "process_nearest_seedless_jobs_planned": 0,
            "process_nearest_fallback": 0,
            "process_nearest_fallback_exact_calls": 0,
            "process_exact_fallback_calls": 0,
            "process_exact_primary_calls": 3,
            "process_exact_pairs_submitted": 3,
            "process_exact_pairs_completed": 3,
            "process_exact_accepted": 3,
        }

        contract = WRAPPER._mode_contract(result, requested_mode="EXACT_ONLY")

        self.assertTrue(contract["passed"], contract["issues"])

    def test_persisted_summary_passes_corrected_contract_but_keeps_tick_failure(self):
        payload = {
            "mode": "VERIFIED_NEAREST_ONLY",
            "worker_mode": WRAPPER.WORKER_MODE,
            "process_thread_caps": {"OMP_NUM_THREADS": "1"},
            "contract": {
                "mode": {
                    "actual_mode": "VERIFIED_NEAREST_ONLY",
                    "worker_mode": WRAPPER.WORKER_MODE,
                    "counters": {
                        "direct_exact_jobs_completed": 4,
                        "direct_exact_jobs_planned": 4,
                        "process_graph_rejected_before_nearest": 1,
                        "process_nearest_seed_missing": 0,
                        "process_nearest_attempted": 3,
                        "process_nearest_accepted": 2,
                        "process_nearest_fast_miss": 1,
                        "process_nearest_seeded_jobs_planned": 4,
                        "process_nearest_fallback_exact_calls": 0,
                        "process_exact_fallback_calls": 0,
                        "process_exact_primary_calls": 0,
                        "process_exact_pairs_submitted": 3,
                        "process_exact_pairs_completed": 3,
                        "process_exact_accepted": 2,
                    },
                },
                "mapping": {
                    "groups": [
                        {
                            "master_key": [99],
                            "member_count": 2,
                            "mapping_pair_count": 4,
                            "expected_mapping_pair_count": 4,
                        }
                    ]
                },
                "uv_area": {
                    "passed": True,
                    "issues": [],
                    "reported_master_count": 1,
                    "rows": [
                        {"master_key": [99], "master_area": 5.0, "larger_members": []}
                    ],
                },
            },
            "tick_metrics": {
                "max_tick_ms": 473.6104,
                "max_startup_tick_ms": 188.4032,
                "max_tick_stage": "process_group_exact_dispatch",
            },
        }

        audit = WRAPPER._audit_persisted_summary(payload)

        self.assertTrue(audit["mode"]["passed"], audit["mode"]["issues"])
        self.assertTrue(audit["mapping"]["passed"], audit["mapping"]["issues"])
        self.assertTrue(audit["uv_area"]["passed"], audit["uv_area"]["issues"])
        self.assertFalse(audit["tick"]["within_limit"])
        self.assertEqual(
            audit["raw_phase_counters"]["process_exact_pairs_completed"], 3
        )


if __name__ == "__main__":
    unittest.main()
