"""Focused pure tests for the Pro planner face-refinement prefilter."""

import unittest

from uv_gpt import pro_candidate_planner as planner


_GRAPH_A = (
    (0, 5),
    (0, 6),
    (1, 4),
    (1, 5),
    (1, 6),
    (2, 3),
    (2, 4),
    (3, 4),
)
_GRAPH_B = (
    (0, 3),
    (0, 5),
    (0, 6),
    (1, 4),
    (1, 6),
    (2, 3),
    (2, 5),
    (3, 4),
)


def _adjacency(edges, count=7):
    values = [set() for _ in range(count)]
    for left, right in edges:
        values[left].add(right)
        values[right].add(left)
    return tuple(tuple(sorted(value)) for value in values)


def _face_refinement(edges, count=7, **overrides):
    values = {
        "face_labels": ("face",) * count,
        "adjacency": _adjacency(edges, count),
        "edge_labels": ("edge",) * len(edges),
        "vertex_labels": ("vertex",) * count,
        "loop_count": len(edges) * 2,
    }
    values.update(overrides)
    return planner.canonical_face_color_refinement(**values)


class ProTopologyRefinementTests(unittest.TestCase):
    def test_cycle_rotation_and_reversal_are_canonical(self):
        first = planner.canonical_cycle_signature(("e0", "v0", "e1", "v1"))
        rotated = planner.canonical_cycle_signature(("e1", "v1", "e0", "v0"))
        reversed_cycle = planner.canonical_cycle_signature(("v1", "e1", "v0", "e0"))
        self.assertEqual(first, rotated)
        self.assertEqual(first, reversed_cycle)

    def test_face_relabel_and_input_order_do_not_change_fingerprint(self):
        first = _face_refinement(_GRAPH_A)
        permutation = (3, 0, 6, 2, 5, 1, 4)
        old_to_new = {old: new for new, old in enumerate(permutation)}
        relabeled_edges = tuple(
            (old_to_new[left], old_to_new[right]) for left, right in reversed(_GRAPH_A)
        )
        relabeled = _face_refinement(relabeled_edges)
        self.assertEqual(first.fingerprint, relabeled.fingerprint)
        self.assertEqual(first.fingerprint, _face_refinement(_GRAPH_A).fingerprint)

    def test_two_round_collision_is_split_by_convergence(self):
        two_a = _face_refinement(_GRAPH_A, max_rounds=2)
        two_b = _face_refinement(_GRAPH_B, max_rounds=2)
        self.assertEqual(two_a.fingerprint, two_b.fingerprint)
        full_a = _face_refinement(_GRAPH_A)
        full_b = _face_refinement(_GRAPH_B)
        self.assertNotEqual(full_a.fingerprint, full_b.fingerprint)
        self.assertTrue(full_a.stable)
        self.assertTrue(full_b.stable)
        self.assertGreater(full_a.rounds, 2)

    def test_digest_is_stable_and_contains_no_adapter_ids(self):
        first = _face_refinement(_GRAPH_A)
        second = _face_refinement(_GRAPH_A)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.fingerprint[0], "pro-face-wl-v5")
        self.assertNotIn("9999", repr(first.fingerprint))

    def test_bound_records_truncation_for_a_long_refinement(self):
        edges = tuple((index, index + 1) for index in range(129))
        result = _face_refinement(edges, count=130)
        self.assertEqual(
            result.max_rounds,
            planner.DEFAULT_FACE_REFINEMENT_MAX_ROUNDS,
        )
        self.assertEqual(result.rounds, result.max_rounds)
        self.assertFalse(result.stable)
        self.assertTrue(result.truncated)
        self.assertGreaterEqual(result.elapsed_ms, 0.0)

    def test_isomorphic_exact_candidates_remain_in_one_strict_bucket(self):
        first = _face_refinement(_GRAPH_A).fingerprint
        permutation = (3, 0, 6, 2, 5, 1, 4)
        old_to_new = {old: new for new, old in enumerate(permutation)}
        relabeled_edges = tuple(
            (old_to_new[left], old_to_new[right]) for left, right in _GRAPH_A
        )
        second = _face_refinement(relabeled_edges).fingerprint
        records = (
            planner.IslandRecord((0,), first, (0.1, 0.2), 2.0, ("x",)),
            planner.IslandRecord((1,), second, (0.1, 0.2), 1.0, ("x",)),
        )
        pairs, diagnostics = planner.plan_candidates(
            records,
            planner.PlannerConfig(
                per_member_k=4,
                global_pair_budget=20,
                per_bucket_pair_budget=20,
                index_dimensions=2,
                fallback_probe_limit=4,
                fallback_candidate_limit=4,
                batch_size=4,
            ),
        ).materialize()
        self.assertEqual(len(pairs), 2)
        self.assertEqual(diagnostics.candidate_pairs, 2)

    def test_converged_prefilter_only_removes_pairs_and_keeps_order(self):
        coarse_a = _face_refinement(_GRAPH_A, max_rounds=2).fingerprint
        coarse_b = _face_refinement(_GRAPH_B, max_rounds=2).fingerprint
        full_a = _face_refinement(_GRAPH_A).fingerprint
        full_b = _face_refinement(_GRAPH_B).fingerprint
        records_coarse = (
            planner.IslandRecord((0,), coarse_a, (0.1, 0.2), 2.0, ("x",)),
            planner.IslandRecord((1,), coarse_b, (0.1, 0.2), 1.0, ("x",)),
        )
        records_full = (
            planner.IslandRecord((0,), full_a, (0.1, 0.2), 2.0, ("x",)),
            planner.IslandRecord((1,), full_b, (0.1, 0.2), 1.0, ("x",)),
        )
        config = planner.PlannerConfig(
            per_member_k=4,
            global_pair_budget=20,
            per_bucket_pair_budget=20,
            index_dimensions=2,
            fallback_probe_limit=4,
            fallback_candidate_limit=4,
            batch_size=4,
        )
        coarse_pairs, coarse_diag = planner.plan_candidates(
            records_coarse, config
        ).materialize()
        full_pairs, full_diag = planner.plan_candidates(records_full, config).materialize()
        self.assertLessEqual(full_diag.candidate_pairs, coarse_diag.candidate_pairs)
        self.assertEqual(
            [(pair.member_key, pair.master_key) for pair in full_pairs],
            sorted((pair.member_key, pair.master_key) for pair in full_pairs),
        )
        self.assertEqual(full_diag.candidate_pairs, 0)


if __name__ == "__main__":
    unittest.main()
