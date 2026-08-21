"""Focused PERF-P09 checks for resumable Pro shape matching."""

import math
from pathlib import Path
import sys
import time
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from uv_gpt import pro_shape_state, similarity_matcher


def _segments(points):
    points = tuple(points)
    return tuple(zip(points, points[1:] + points[:1]))


def _transform(points, *, scale=1.7, reflection=False):
    angle = 0.37
    cosine = math.cos(angle)
    sine = math.sin(angle)
    result = []
    for x, y in points:
        if reflection:
            x = -x
        result.append(
            (
                4.0 + scale * (x * cosine - y * sine),
                -2.0 + scale * (x * sine + y * cosine),
            )
        )
    return tuple(result)


ASYMMETRIC = (
    (0.0, 0.0),
    (2.0, 0.0),
    (2.8, 1.0),
    (1.4, 2.2),
    (0.0, 1.4),
)


def _descriptor(points):
    return similarity_matcher.build_descriptor(
        _segments(points),
        topology={"face_count": 5, "edge_count": 5, "vertex_count": 5},
    )


class ProShapeStateTests(unittest.TestCase):
    def _oracle(self, reference, candidate, **options):
        return similarity_matcher.match_descriptors(
            reference,
            candidate,
            diagnostics=similarity_matcher.MatcherDiagnostics(),
            count_candidate=False,
            use_numpy=False,
            **options,
        )

    def _state_result(self, reference, candidate, **options):
        state = pro_shape_state.ProShapeMatchState(
            reference=reference,
            candidate=candidate,
            use_numpy=False,
            **options,
        )
        return state, state.run_to_completion(operation_budget=1)

    def test_equivalence_preserves_scale_and_flipping_controls(self):
        reference = _descriptor(ASYMMETRIC)
        candidate = _descriptor(
            _transform(ASYMMETRIC, scale=1.7, reflection=True)
        )
        for match_scale in (False, True):
            for allow_flipping in (False, True):
                with self.subTest(
                    match_scale=match_scale,
                    allow_flipping=allow_flipping,
                ):
                    options = {
                        "match_scale": match_scale,
                        "allow_flipping": allow_flipping,
                        "tolerance": 0.01,
                    }
                    expected = self._oracle(reference, candidate, **options)
                    state, actual = self._state_result(
                        reference,
                        candidate,
                        **options,
                    )
                    self.assertEqual(actual.accepted, expected.accepted)
                    self.assertEqual(actual.reason, expected.reason)
                    self.assertAlmostEqual(actual.score, expected.score, places=12)
                    self.assertEqual(actual.transform, expected.transform)
                    self.assertGreater(state.shape_slices, 1)
                    self.assertEqual(state.phase, "done")

    def test_pause_resume_honors_operation_cap_and_deadline(self):
        state = pro_shape_state.ProShapeMatchState(
            reference=_descriptor(ASYMMETRIC),
            candidate=_descriptor(ASYMMETRIC),
            use_numpy=False,
        )
        result, operations = state.advance(
            operation_budget=16,
            deadline=time.perf_counter() - 1.0,
        )
        self.assertIsNone(result)
        self.assertEqual(operations, 0)
        while not state.done:
            result, operations = state.advance(operation_budget=2)
            self.assertLessEqual(operations, 2)
        self.assertTrue(result.accepted)
        self.assertGreater(state.shape_slices, 1)
        self.assertGreater(state.shape_primitive_operations, state.shape_slices)

    def test_descriptor_builders_are_called_once_and_partial_state_has_no_result(self):
        calls = {"reference": 0, "candidate": 0}

        def reference_builder():
            calls["reference"] += 1
            return _descriptor(ASYMMETRIC)

        def candidate_builder():
            calls["candidate"] += 1
            return _descriptor(ASYMMETRIC)

        state = pro_shape_state.ProShapeMatchState(
            reference_builder=reference_builder,
            candidate_builder=candidate_builder,
            use_numpy=False,
        )
        result, _operations = state.advance(operation_budget=1)
        self.assertIsNone(result)
        self.assertEqual(calls, {"reference": 1, "candidate": 0})
        self.assertIsNone(state.result)
        state.run_to_completion(operation_budget=1)
        self.assertEqual(calls, {"reference": 1, "candidate": 1})
        self.assertIsNotNone(state.result)

    def test_cancel_discards_partial_state_and_never_returns_match(self):
        state = pro_shape_state.ProShapeMatchState(
            reference_builder=lambda: _descriptor(ASYMMETRIC),
            candidate_builder=lambda: _descriptor(ASYMMETRIC),
            use_numpy=False,
        )
        state.advance(operation_budget=1)
        state.cancel()
        self.assertTrue(state.done)
        self.assertTrue(state.cancelled)
        self.assertIsNone(state.result)
        self.assertIsNone(state.reference)
        self.assertIsNone(state.candidate)
        result, operations = state.advance(operation_budget=8)
        self.assertIsNone(result)
        self.assertEqual(operations, 0)

    def test_unavoidable_call_metric_is_compact(self):
        state = pro_shape_state.ProShapeMatchState(
            reference=_descriptor(ASYMMETRIC),
            candidate=_descriptor(ASYMMETRIC),
            use_numpy=False,
        )
        result = state.run_to_completion(operation_budget=4)
        self.assertTrue(result.accepted)
        self.assertGreaterEqual(state.max_shape_call_ms, 0.0)
        self.assertLessEqual(len(state.over_25ms_call_samples), 8)
        self.assertTrue(state.phase_transitions)


if __name__ == "__main__":
    unittest.main()
