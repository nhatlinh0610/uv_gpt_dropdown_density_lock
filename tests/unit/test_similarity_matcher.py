"""Synthetic correctness and backend-parity tests for similarity_matcher."""

import importlib.util
import math
from pathlib import Path
import unittest
from dataclasses import replace


MODULE_PATH = Path(__file__).resolve().parents[2] / "uv_gpt" / "similarity_matcher.py"
SPEC = importlib.util.spec_from_file_location("uv_gpt_similarity_matcher_test_module", MODULE_PATH)
MATCHER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MATCHER)


def segments(points):
    points = tuple(points)
    return tuple(zip(points, points[1:] + points[:1]))


def transformed(points, angle=0.37, scale=1.7, offset=(4.0, -2.0), reflection=False):
    cosine = math.cos(angle)
    sine = math.sin(angle)
    result = []
    for x, y in points:
        if reflection:
            x = -x
        result.append(
            (
                offset[0] + scale * (x * cosine - y * sine),
                offset[1] + scale * (x * sine + y * cosine),
            )
        )
    return tuple(result)


def densify(points):
    points = tuple(points)
    result = []
    for start, end in zip(points, points[1:] + points[:1]):
        result.append(start)
        result.append(((start[0] + end[0]) * 0.5, (start[1] + end[1]) * 0.5))
    return tuple(result)


def descriptor(points_or_segments, topology=None):
    first = points_or_segments[0] if points_or_segments else ()
    is_segment_sequence = (
        len(first) == 2
        and isinstance(first[0], (tuple, list))
        and len(first[0]) == 2
        and isinstance(first[1], (tuple, list))
        and len(first[1]) == 2
    )
    if is_segment_sequence:
        raw_segments = points_or_segments
    else:
        raw_segments = segments(points_or_segments)
    return MATCHER.build_descriptor(raw_segments, topology=topology)


ASYMMETRIC = (
    (0.0, 0.0),
    (2.0, 0.0),
    (2.8, 1.0),
    (1.4, 2.2),
    (0.0, 1.4),
)


class SimilarityMatcherTests(unittest.TestCase):
    def test_translation_rotation_and_uniform_scale(self):
        reference = descriptor(ASYMMETRIC, {"face_count": 5, "edge_count": 5, "vertex_count": 5})
        candidate = descriptor(
            transformed(ASYMMETRIC),
            {"face_count": 5, "edge_count": 5, "vertex_count": 5},
        )
        result = MATCHER.match_descriptors(reference, candidate, tolerance=1.0e-7, use_numpy=False)
        self.assertTrue(result.accepted, result)
        self.assertLess(result.score, 1.0e-8)
        self.assertIsNotNone(result.transform)

    def test_outer_sse_bound_uses_every_allowed_fit(self):
        reference = descriptor(ASYMMETRIC)
        candidate = descriptor(transformed(ASYMMETRIC, reflection=True))
        fits = MATCHER._fit_loop_candidates(
            reference.outer_loops[0],
            candidate.outer_loops[0],
            allow_flipping=True,
            use_numpy=False,
        )
        point_count = min(
            MATCHER.MAX_SAMPLE_COUNT,
            max(
                reference.outer_loops[0].sample_count,
                candidate.outer_loops[0].sample_count,
                MATCHER.MIN_SAMPLE_COUNT,
            ),
        )
        expected = min(fit.rms * fit.rms * point_count for fit in fits)
        result = MATCHER.match_descriptors(
            reference,
            candidate,
            allow_flipping=True,
            tolerance=1.0e-7,
            use_numpy=False,
        )
        self.assertTrue(result.accepted, result)
        self.assertEqual(result.first_outer_point_count, point_count)
        self.assertAlmostEqual(result.first_outer_min_sse, expected, places=14)

    def test_outer_sse_bound_skips_unequal_boundary_multiplicity(self):
        reference = descriptor(ASYMMETRIC)
        candidate = descriptor(densify(ASYMMETRIC))
        result = MATCHER.match_descriptors(
            reference, candidate, tolerance=1.0e-7, use_numpy=False
        )
        self.assertTrue(result.accepted, result)
        self.assertIsNone(result.first_outer_min_sse)
        self.assertEqual(result.first_outer_point_count, 0)

    def test_outer_sse_bound_is_strictly_above_exact_budget_for_small_shape_error(self):
        perturbed = list(ASYMMETRIC)
        perturbed[2] = (perturbed[2][0] + 1.0e-4, perturbed[2][1])
        reference = descriptor(ASYMMETRIC)
        candidate = descriptor(tuple(perturbed))
        result = MATCHER.match_descriptors(
            reference, candidate, tolerance=0.01, use_numpy=False
        )
        self.assertTrue(result.accepted, result)
        self.assertIsNotNone(result.first_outer_min_sse)
        self.assertGreater(
            result.first_outer_min_sse,
            len(ASYMMETRIC) * (1.0e-6 ** 2),
        )

    def test_scale_off_rejects_uniformly_scaled_shape(self):
        reference = descriptor(ASYMMETRIC)
        candidate = descriptor(transformed(ASYMMETRIC, scale=1.7))
        result = MATCHER.match_descriptors(
            reference,
            candidate,
            match_scale=False,
            tolerance=0.01,
            use_numpy=False,
        )
        self.assertFalse(result.accepted)
        self.assertGreater(result.score, 0.01)

    def test_reflection_respects_allow_flipping(self):
        reference = descriptor(ASYMMETRIC)
        candidate = descriptor(transformed(ASYMMETRIC, reflection=True))
        denied = MATCHER.match_descriptors(reference, candidate, allow_flipping=False, use_numpy=False)
        allowed = MATCHER.match_descriptors(
            reference,
            candidate,
            allow_flipping=True,
            tolerance=1.0e-7,
            use_numpy=False,
        )
        self.assertFalse(denied.accepted)
        self.assertGreater(denied.score, 0.01)
        self.assertTrue(allowed.accepted, allowed)
        self.assertTrue(allowed.transform.reflected)

    def test_cyclic_start_and_reverse_winding(self):
        reference = descriptor(ASYMMETRIC)
        shifted_reversed = tuple(reversed(ASYMMETRIC[2:] + ASYMMETRIC[:2]))
        candidate = descriptor(shifted_reversed)
        result = MATCHER.match_descriptors(reference, candidate, tolerance=1.0e-7, use_numpy=False)
        self.assertTrue(result.accepted, result)
        self.assertLess(result.score, 1.0e-8)

        # The graph extractor canonicalizes a segment set, so explicitly pass
        # a reversed immutable loop to the numeric fit to exercise the reverse
        # correspondence branch itself.
        ref_loop = reference.outer_loops[0]
        reversed_points = tuple(reversed(ref_loop.points))
        reversed_loop = replace(
            ref_loop,
            points=reversed_points,
            samples=MATCHER.resample_polyline(
                reversed_points,
                ref_loop.sample_count,
                closed=True,
                canonicalize=False,
            ),
            signed_area=-ref_loop.signed_area,
            winding=-ref_loop.winding,
        )
        fit = MATCHER.fit_loop(ref_loop, reversed_loop, use_numpy=False)
        self.assertLess(fit.score, 1.0e-8)
        self.assertTrue(fit.reversed)

    def test_unequal_raw_sampling_resamples_by_arclength(self):
        source = tuple(
            (math.cos(2.0 * math.pi * index / 40.0), 0.65 * math.sin(2.0 * math.pi * index / 40.0))
            for index in range(40)
        )
        reference = descriptor(source)
        candidate = descriptor(densify(source))
        self.assertNotEqual(reference.outer_loops[0].sample_count, candidate.outer_loops[0].sample_count)
        result = MATCHER.match_descriptors(reference, candidate, tolerance=1.0e-7, use_numpy=False)
        self.assertTrue(result.accepted, result)
        self.assertLess(result.score, 1.0e-8)

    def test_outer_and_one_hole_are_compared_separately(self):
        outer = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
        hole = ((3.0, 3.0), (5.0, 3.0), (5.0, 5.0), (3.0, 5.0))
        reference = descriptor(segments(outer) + segments(hole), {"face_count": 8})
        moved = transformed(outer, angle=0.3, scale=2.0, offset=(20.0, 5.0))
        moved_hole = transformed(hole, angle=0.3, scale=2.0, offset=(20.0, 5.0))
        candidate = descriptor(segments(moved) + segments(moved_hole), {"face_count": 8})
        self.assertEqual(len(reference.outer_loops), 1)
        self.assertEqual(len(reference.hole_loops), 1)
        result = MATCHER.match_descriptors(reference, candidate, tolerance=1.0e-7, use_numpy=False)
        self.assertTrue(result.accepted, result)
        self.assertLess(result.hole_rms, 1.0e-8)

        missing_hole = descriptor(segments(outer), {"face_count": 4})
        hole_mismatch = MATCHER.match_descriptors(
            reference,
            missing_hole,
            tolerance=0.1,
            use_numpy=False,
        )
        self.assertFalse(hole_mismatch.accepted)
        self.assertIn(hole_mismatch.reason, {"boundary_signature", "hole_count"})

    def test_outer_and_multiple_holes_use_deterministic_assignment(self):
        outer = ((0.0, 0.0), (12.0, 0.0), (12.0, 12.0), (0.0, 12.0))
        hole_a = ((2.0, 2.0), (4.0, 2.0), (4.0, 4.0), (2.0, 4.0))
        hole_b = ((7.0, 6.0), (10.0, 6.0), (10.0, 9.0), (7.0, 9.0))
        reference = descriptor(segments(outer) + segments(hole_a) + segments(hole_b), {"face_count": 12})
        moved = transformed(outer, angle=-0.2, scale=0.75, offset=(-5.0, 8.0))
        moved_a = transformed(hole_a, angle=-0.2, scale=0.75, offset=(-5.0, 8.0))
        moved_b = transformed(hole_b, angle=-0.2, scale=0.75, offset=(-5.0, 8.0))
        candidate = descriptor(segments(moved) + segments(moved_b) + segments(moved_a), {"face_count": 12})
        self.assertEqual(len(reference.hole_loops), 2)
        result = MATCHER.match_descriptors(reference, candidate, tolerance=1.0e-7, use_numpy=False)
        self.assertTrue(result.accepted, result)
        self.assertLess(result.hole_rms, 1.0e-8)

    def test_topology_mismatch_is_penalized_and_rejected_at_default_tolerance(self):
        reference = descriptor(ASYMMETRIC, {"face_count": 5, "edge_count": 5, "vertex_count": 5})
        candidate = descriptor(ASYMMETRIC, {"face_count": 6, "edge_count": 6, "vertex_count": 6})
        result = MATCHER.match_descriptors(reference, candidate, tolerance=0.01, use_numpy=False)
        self.assertFalse(result.accepted)
        self.assertFalse(result.topology_gate.strict)
        self.assertEqual(result.topology_gate.reason, "tolerant_topology")
        self.assertGreaterEqual(result.topology_penalty, 0.05)
        tolerant = MATCHER.match_descriptors(
            reference,
            candidate,
            tolerance=0.1,
            use_numpy=False,
            allow_tolerant_topology=True,
        )
        self.assertTrue(tolerant.accepted)
        self.assertFalse(tolerant.topology_gate.strict)

    def test_open_nonmanifold_and_degenerate_boundaries_reject(self):
        reference = descriptor(ASYMMETRIC)
        open_descriptor = descriptor((((0.0, 0.0), (1.0, 0.0)), ((1.0, 0.0), (1.0, 1.0))))
        non_manifold = descriptor(
            (((0.0, 0.0), (1.0, 0.0)), ((1.0, 0.0), (2.0, 0.0)), ((1.0, 0.0), (1.0, 1.0)))
        )
        degenerate = descriptor((((0.0, 0.0), (0.0, 0.0)),))
        self.assertFalse(MATCHER.match_descriptors(reference, open_descriptor, use_numpy=False).accepted)
        self.assertFalse(MATCHER.match_descriptors(reference, non_manifold, use_numpy=False).accepted)
        self.assertFalse(MATCHER.match_descriptors(reference, degenerate, use_numpy=False).accepted)
        self.assertTrue(any(loop.status == "ambiguous" for loop in non_manifold.loops))
        self.assertTrue(any(loop.status == "degenerate" for loop in degenerate.loops))

    def test_deterministic_tie_break(self):
        square = ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0))
        reference = descriptor(square)
        candidate = descriptor(transformed(square, angle=0.2, scale=1.4, offset=(8.0, -3.0)))
        first = MATCHER.match_descriptors(reference, candidate, use_numpy=False, tolerance=1.0e-7)
        second = MATCHER.match_descriptors(reference, candidate, use_numpy=False, tolerance=1.0e-7)
        self.assertEqual(first.accepted, second.accepted)
        self.assertEqual(first.reason, second.reason)
        self.assertAlmostEqual(first.score, second.score, places=14)
        self.assertEqual(first.transform, second.transform)

    def test_numpy_and_python_backend_parity(self):
        if not MATCHER.numpy_available():
            self.skipTest("NumPy is not available in this Python runtime")
        outer = ((0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0))
        hole_a = ((2.0, 2.0), (4.0, 2.0), (4.0, 4.0), (2.0, 4.0))
        hole_b = ((6.0, 6.0), (9.0, 6.0), (9.0, 8.0), (6.0, 8.0))
        ellipse = tuple(
            (math.cos(2.0 * math.pi * index / 40.0), 0.65 * math.sin(2.0 * math.pi * index / 40.0))
            for index in range(40)
        )
        cases = (
            (
                "transform",
                descriptor(ASYMMETRIC, {"face_count": 5, "edge_count": 5, "vertex_count": 5}),
                descriptor(
                    transformed(ASYMMETRIC, angle=-0.41, scale=1.35, offset=(3.0, -4.0)),
                    {"face_count": 5, "edge_count": 5, "vertex_count": 5},
                ),
                {"tolerance": 1.0e-7},
                True,
            ),
            (
                "reflection",
                descriptor(ASYMMETRIC, {"face_count": 5}),
                descriptor(transformed(ASYMMETRIC, reflection=True), {"face_count": 5}),
                {"allow_flipping": True, "tolerance": 1.0e-7},
                True,
            ),
            (
                "scale_off_reject",
                descriptor(ASYMMETRIC),
                descriptor(transformed(ASYMMETRIC, scale=1.7)),
                {"match_scale": False, "tolerance": 0.01},
                False,
            ),
            (
                "cyclic_reverse",
                descriptor(ASYMMETRIC),
                descriptor(tuple(reversed(ASYMMETRIC[2:] + ASYMMETRIC[:2]))),
                {"tolerance": 1.0e-7},
                True,
            ),
            (
                "unequal_sampling",
                descriptor(ellipse),
                descriptor(densify(ellipse)),
                {"tolerance": 1.0e-7},
                True,
            ),
            (
                "one_hole",
                descriptor(segments(outer) + segments(hole_a), {"face_count": 8}),
                descriptor(
                    segments(transformed(outer, angle=0.3, scale=1.4, offset=(2.0, -3.0)))
                    + segments(transformed(hole_a, angle=0.3, scale=1.4, offset=(2.0, -3.0))),
                    {"face_count": 8},
                ),
                {"tolerance": 1.0e-7},
                True,
            ),
            (
                "multiple_holes",
                descriptor(segments(outer) + segments(hole_a) + segments(hole_b), {"face_count": 12}),
                descriptor(
                    segments(transformed(outer, angle=-0.2, scale=0.75, offset=(-5.0, 8.0)))
                    + segments(transformed(hole_b, angle=-0.2, scale=0.75, offset=(-5.0, 8.0)))
                    + segments(transformed(hole_a, angle=-0.2, scale=0.75, offset=(-5.0, 8.0))),
                    {"face_count": 12},
                ),
                {"tolerance": 1.0e-7},
                True,
            ),
            (
                "topology_reject",
                descriptor(ASYMMETRIC, {"face_count": 5, "edge_count": 5, "vertex_count": 5}),
                descriptor(ASYMMETRIC, {"face_count": 6, "edge_count": 6, "vertex_count": 6}),
                {"tolerance": 0.01, "allow_tolerant_topology": False},
                False,
            ),
        )

        for name, reference, candidate, options, expected_acceptance in cases:
            with self.subTest(case=name):
                python_result = MATCHER.match_descriptors(
                    reference, candidate, use_numpy=False, **options
                )
                numpy_result = MATCHER.match_descriptors(
                    reference, candidate, use_numpy=True, **options
                )
                self.assertEqual(python_result.accepted, numpy_result.accepted)
                self.assertEqual(python_result.accepted, expected_acceptance)
                self.assertEqual(python_result.reason, numpy_result.reason)
                self.assertAlmostEqual(
                    python_result.score,
                    numpy_result.score,
                    delta=1.0e-10,
                    msg=name,
                )
                self.assertAlmostEqual(
                    python_result.topology_penalty,
                    numpy_result.topology_penalty,
                    delta=1.0e-12,
                    msg=name,
                )
                if python_result.transform is None or numpy_result.transform is None:
                    self.assertIsNone(python_result.transform)
                    self.assertIsNone(numpy_result.transform)
                    continue
                self.assertAlmostEqual(
                    python_result.transform.angle,
                    numpy_result.transform.angle,
                    delta=1.0e-10,
                    msg=name,
                )
                self.assertAlmostEqual(
                    python_result.transform.scale,
                    numpy_result.transform.scale,
                    delta=1.0e-10,
                    msg=name,
                )
                self.assertEqual(python_result.transform.reflected, numpy_result.transform.reflected)
                self.assertEqual(python_result.transform.reversed, numpy_result.transform.reversed)
                self.assertEqual(python_result.transform.cyclic_shift, numpy_result.transform.cyclic_shift)

    def test_per_run_descriptor_cache_and_diagnostics(self):
        diagnostics = MATCHER.MatcherDiagnostics()
        cache = MATCHER.DescriptorCache(diagnostics)
        reference = cache.get_or_build((1, 2), ("uv", 0), lambda: descriptor(ASYMMETRIC))
        same = cache.get_or_build((1, 2), ("uv", 0), lambda: self.fail("cache miss"))
        self.assertIs(reference, same)
        self.assertEqual(len(cache), 1)
        self.assertEqual(diagnostics.descriptor_builds, 1)
        self.assertEqual(diagnostics.cache_hits, 1)
        result = MATCHER.match_descriptors(reference, reference, diagnostics=diagnostics, use_numpy=False)
        self.assertTrue(result.accepted)
        self.assertEqual(result.diagnostics.candidates_seen, 1)
        self.assertEqual(result.diagnostics.full_fits, 1)

    def test_public_diagnostics_reset_and_snapshot_are_cache_free(self):
        MATCHER.reset_diagnostics()
        reference = descriptor(ASYMMETRIC)
        cache = MATCHER.DescriptorCache()
        cache.get_or_build((7,), ("uv", 1), lambda: reference)
        cache.get_or_build((7,), ("uv", 1), lambda: self.fail("unexpected public cache miss"))
        result = MATCHER.match_descriptors(reference, reference, tolerance=1.0e-7, use_numpy=False)
        self.assertTrue(result.accepted)
        snapshot = MATCHER.get_diagnostics()
        self.assertEqual(snapshot.descriptor_builds, 1)
        self.assertEqual(snapshot.cache_hits, 1)
        self.assertEqual(snapshot.candidates_seen, 1)
        self.assertEqual(snapshot.coarse_candidates, 1)
        self.assertEqual(snapshot.topology_candidates, 1)
        self.assertEqual(snapshot.full_fits, 1)
        reset = MATCHER.reset_diagnostics()
        self.assertEqual(reset.descriptor_builds, 0)
        self.assertEqual(reset.cache_hits, 0)
        self.assertEqual(MATCHER.get_diagnostics().full_fits, 0)

    def test_resampling_has_bounded_count_without_duplicate_closed_endpoint(self):
        points = ((0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0))
        samples = MATCHER.resample_polyline(points, 128, closed=True)
        self.assertEqual(len(samples), 128)
        self.assertNotEqual(samples[0], samples[-1])

    def test_cheap_signature_is_rotation_scale_invariant_and_topology_staged(self):
        reference = MATCHER.build_cheap_signature(
            segments(ASYMMETRIC),
            topology={"face_count": 5, "edge_count": 5, "vertex_count": 5},
        )
        candidate = MATCHER.build_cheap_signature(
            segments(transformed(ASYMMETRIC, angle=-0.41, scale=1.35)),
            topology={"face_count": 6, "edge_count": 6, "vertex_count": 6},
        )
        self.assertEqual(reference.raw_boundary_signature, candidate.raw_boundary_signature)
        self.assertTrue(MATCHER.cheap_boundary_gate(reference, candidate).passed)
        topology = MATCHER.cheap_topology_gate(reference, candidate)
        self.assertTrue(topology.passed)
        self.assertFalse(topology.strict)
        self.assertEqual(topology.reason, "tolerant_topology")

        unequal = MATCHER.build_cheap_signature(segments(densify(ASYMMETRIC)))
        self.assertFalse(MATCHER.cheap_boundary_gate(reference, unequal).passed)

    def test_cheap_signature_cache_has_separate_diagnostics(self):
        diagnostics = MATCHER.MatcherDiagnostics()
        cache = MATCHER.DescriptorCache(diagnostics)
        first = cache.get_or_build_cheap(
            (1, 2),
            ("uv", 0),
            lambda: MATCHER.build_cheap_signature(segments(ASYMMETRIC)),
        )
        second = cache.get_or_build_cheap(
            (1, 2),
            ("uv", 0),
            lambda: self.fail("unexpected cheap signature cache miss"),
        )
        self.assertEqual(first, second)
        self.assertEqual(diagnostics.cheap_signatures, 1)
        self.assertEqual(diagnostics.cheap_cache_hits, 1)
        self.assertEqual(diagnostics.descriptor_builds, 0)


if __name__ == "__main__":
    unittest.main()
