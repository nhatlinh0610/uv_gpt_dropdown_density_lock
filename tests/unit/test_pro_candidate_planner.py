"""Focused unit coverage for the bounded Align Similar Pro planner."""

from dataclasses import FrozenInstanceError
import importlib
import random
import unittest


PLANNER = importlib.import_module("uv_gpt.pro_candidate_planner")
TOPOLOGY = importlib.import_module("uv_gpt.topology_correspondence")


def _record(
    key,
    fingerprint="quad",
    descriptor=(0.10, 0.20),
    density=1.0,
    cheap=("cheap",),
):
    return PLANNER.IslandRecord(
        face_key=(key,) if not isinstance(key, tuple) else key,
        strict_topology_fingerprint=fingerprint,
        normalized_boundary_descriptor=descriptor,
        density=density,
        cheap_signature=cheap,
    )


def _config(**overrides):
    values = dict(
        per_member_k=8,
        global_pair_budget=10000,
        per_bucket_pair_budget=10000,
        descriptor_bin_width=0.05,
        index_dimensions=2,
        fallback_probe_limit=16,
        fallback_candidate_limit=8,
        batch_size=7,
    )
    values.update(overrides)
    return PLANNER.PlannerConfig(**values)


def _polygon_graph(face_key, vertex_start, side_count):
    loop_keys = tuple((face_key, index) for index in range(side_count))
    vertices = tuple(
        TOPOLOGY.VertexRecord(
            key=vertex_start + index,
            loop_keys=(loop_keys[index],),
            boundary=True,
            signature=(),
        )
        for index in range(side_count)
    )
    edges = tuple(
        TOPOLOGY.EdgeRecord(
            key=vertex_start + index,
            loop_keys=(loop_keys[index],),
            face_keys=(face_key,),
            boundary=True,
            non_manifold=False,
            signature=(),
        )
        for index in range(side_count)
    )
    loops = tuple(
        TOPOLOGY.LoopRecord(
            key=loop_keys[index],
            face_key=face_key,
            edge_key=vertex_start + index,
            vertex_key=vertex_start + index,
            next_key=loop_keys[(index + 1) % side_count],
            prev_key=loop_keys[(index - 1) % side_count],
            uv=(float(index), float(index % 2)),
            boundary=True,
            seam=False,
            signature=(),
        )
        for index in range(side_count)
    )
    return TOPOLOGY.make_graph(
        faces=(TOPOLOGY.FaceRecord(face_key, loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
    )


class ProCandidatePlannerTests(unittest.TestCase):
    def test_import_is_pure_and_records_are_frozen(self):
        self.assertNotIn("bpy", PLANNER.__dict__)
        record = _record(1)
        with self.assertRaises(FrozenInstanceError):
            record.density = 2.0
        self.assertIsInstance(record.face_key, tuple)
        self.assertIsInstance(record.normalized_boundary_descriptor, tuple)

    def test_record_rejects_arbitrary_runtime_object_payload(self):
        with self.assertRaises(TypeError):
            _record(1, cheap=object())

    def test_canonical_graph_signature_ignores_adapter_keys_but_not_shape(self):
        first = PLANNER.canonical_graph_color_signature(_polygon_graph(0, 0, 3))
        remapped = PLANNER.canonical_graph_color_signature(_polygon_graph(99, 400, 3))
        square = PLANNER.canonical_graph_color_signature(_polygon_graph(0, 0, 4))
        self.assertEqual(first, remapped)
        self.assertNotEqual(first, square)

    def test_output_is_deterministic_under_shuffled_input(self):
        records = [
            _record(10, density=2.0),
            _record(2, density=2.0),
            _record(7, density=1.0),
            _record(15, density=0.5),
        ]
        first_plan = PLANNER.plan_candidates(records, _config(per_member_k=2))
        first_pairs, first_diag = first_plan.materialize()
        shuffled = list(records)
        random.Random(713).shuffle(shuffled)
        second_plan = PLANNER.plan_candidates(shuffled, _config(per_member_k=2))
        second_pairs, second_diag = second_plan.materialize()
        self.assertEqual(
            [(item.member_key, item.master_key, item.tier) for item in first_pairs],
            [(item.member_key, item.master_key, item.tier) for item in second_pairs],
        )
        self.assertEqual(first_diag.member_statuses, second_diag.member_statuses)
        self.assertEqual(first_diag.candidate_pairs, second_diag.candidate_pairs)

    def test_density_descending_and_face_key_tie_order(self):
        records = [
            _record(9, density=3.0),
            _record(2, density=3.0),
            _record(4, density=2.0),
            _record(12, density=1.0),
        ]
        pairs, _diagnostics = PLANNER.plan_candidates(
            records, _config(per_member_k=3)
        ).materialize()
        for member_key in ((4,), (12,)):
            masters = [
                pair.master_key
                for pair in pairs
                if pair.member_key == member_key
            ]
            self.assertEqual(masters, [(2,), (9,), (12,)] if member_key == (4,) else [(2,), (9,), (4,)])
        self.assertEqual(
            [pair.master_key for pair in pairs if pair.member_key == (12,)],
            [(2,), (9,), (4,)],
        )

    def test_strict_topology_mismatch_never_pairs(self):
        records = [
            _record(1, fingerprint=("quad", "color-a")),
            _record(2, fingerprint=("quad", "color-b")),
        ]
        pairs, diagnostics = PLANNER.plan_candidates(
            records, _config(per_member_k=4)
        ).materialize()
        self.assertEqual(pairs, ())
        self.assertEqual(diagnostics.topology_buckets, 2)
        self.assertEqual(diagnostics.candidate_pairs, 0)

    def test_neighbor_bin_boundary_is_overlap_safe(self):
        records = [
            _record(1, descriptor=(0.049, 0.10), density=2.0),
            _record(2, descriptor=(0.051, 0.10), density=1.0),
        ]
        pairs, _diagnostics = PLANNER.plan_candidates(
            records,
            _config(per_member_k=1, descriptor_bin_width=0.05),
        ).materialize()
        self.assertEqual(
            {(pair.member_key, pair.master_key) for pair in pairs},
            {((1,), (2,)), ((2,), (1,))},
        )
        self.assertTrue(all(pair.tier == "neighbor_bin" for pair in pairs))

    def test_fallback_tier_fills_a_bounded_gap(self):
        records = [
            _record(1, descriptor=(0.00, 0.00), density=2.0),
            _record(2, descriptor=(0.50, 0.00), density=1.0),
        ]
        pairs, diagnostics = PLANNER.plan_candidates(
            records,
            _config(
                per_member_k=1,
                fallback_probe_limit=4,
                fallback_candidate_limit=1,
            ),
        ).materialize()
        self.assertEqual(len(pairs), 2)
        self.assertTrue(all(pair.tier == "canonical_fallback" for pair in pairs))
        self.assertEqual(diagnostics.candidate_pairs, 2)

    def test_per_member_k_is_enforced_and_reported(self):
        records = [_record(index, density=100.0 - index) for index in range(20)]
        pairs, diagnostics = PLANNER.plan_candidates(
            records, _config(per_member_k=3)
        ).materialize()
        self.assertLessEqual(len(pairs), len(records) * 3)
        self.assertEqual(len(diagnostics.truncated_members), len(records))
        reasons = dict(diagnostics.reason_counts)
        self.assertEqual(reasons[PLANNER.REASON_PER_MEMBER_K], len(records))
        self.assertNotIn(PLANNER.REASON_CANDIDATE_INDEX_LIMITED, reasons)

    def test_per_bucket_budget_is_enforced_with_explicit_reason(self):
        records = [_record(index) for index in range(6)]
        pairs, diagnostics = PLANNER.plan_candidates(
            records,
            _config(per_member_k=4, per_bucket_pair_budget=3),
        ).materialize()
        self.assertEqual(len(pairs), 3)
        self.assertIn("quad", diagnostics.truncated_buckets)
        self.assertIn(PLANNER.REASON_BUCKET_PAIR_BUDGET, dict(diagnostics.reason_counts))

    def test_global_budget_is_enforced_with_explicit_reason(self):
        records = [_record(index) for index in range(10)]
        pairs, diagnostics = PLANNER.plan_candidates(
            records,
            _config(per_member_k=4, global_pair_budget=5),
        ).materialize()
        self.assertEqual(len(pairs), 5)
        self.assertIn(PLANNER.REASON_GLOBAL_PAIR_BUDGET, dict(diagnostics.reason_counts))
        self.assertLessEqual(diagnostics.candidate_pairs, 5)

    def test_pair_stream_has_no_transitive_component_assignment(self):
        records = [_record(index) for index in range(5)]
        plan = PLANNER.plan_candidates(records, _config(per_member_k=1))
        pairs = list(plan.iter_pairs())
        self.assertTrue(all(pair.member_key != pair.master_key for pair in pairs))
        self.assertFalse(hasattr(plan, "components"))
        self.assertFalse(hasattr(plan, "_pairs"))

    def test_synthetic_worst_bucket_is_linear_in_k_not_quadratic_pairs(self):
        records = [
            _record(index, descriptor=(0.125, 0.25), density=1000.0 - index)
            for index in range(600)
        ]
        plan = PLANNER.plan_candidates(
            records,
            _config(per_member_k=4, global_pair_budget=10000, per_bucket_pair_budget=10000),
        )
        pairs, diagnostics = plan.materialize()
        self.assertLessEqual(len(pairs), 600 * 4)
        self.assertEqual(diagnostics.max_bucket, 600)
        self.assertEqual(diagnostics.theoretical_all_pairs, 600 * 599 // 2)
        self.assertGreater(diagnostics.avoided_all_pairs, 0)
        self.assertGreater(diagnostics.estimated_bytes, 0)

    def test_streaming_batches_never_exceed_requested_batch_size(self):
        records = [_record(index) for index in range(25)]
        plan = PLANNER.plan_candidates(records, _config(per_member_k=2))
        batches = list(plan.iter_batches(batch_size=5))
        self.assertTrue(batches)
        self.assertTrue(all(0 < len(batch) <= 5 for batch in batches))
        self.assertEqual(sum(len(batch) for batch in batches), plan.diagnostics.candidate_pairs)

    def test_invalid_density_is_unresolved_not_a_master(self):
        records = [
            _record(1, density=0.0),
            _record(2, density=1.0),
            _record(3, density=0.5),
        ]
        pairs, diagnostics = PLANNER.plan_candidates(records, _config()).materialize()
        self.assertEqual(
            {(pair.member_key, pair.master_key) for pair in pairs},
            {((2,), (3,)), ((3,), (2,))},
        )
        self.assertEqual(diagnostics.unresolved_members, 1)
        self.assertIn(
            PLANNER.REASON_INVALID_MEMBER_DENSITY,
            dict(diagnostics.reason_counts),
        )


if __name__ == "__main__":
    unittest.main()
