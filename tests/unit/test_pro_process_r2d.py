"""Focused MC4-R2D proof tests.

This module is deliberately self-contained and exercises only pure matcher,
payload and worker seams.  It never starts Blender or opens a fixture.
"""

from __future__ import annotations

import inspect
import math
from types import SimpleNamespace
import unittest
from unittest import mock

from uv_gpt import pro_process_worker as WORKER
from uv_gpt import similarity_matcher as MATCHER


ASYMMETRIC = (
    (0.0, 0.0),
    (2.0, 0.0),
    (2.8, 1.0),
    (1.4, 2.2),
    (0.0, 1.4),
)


def _segments(points):
    points = tuple(points)
    return tuple(zip(points, points[1:] + points[:1]))


def _descriptor(points):
    return MATCHER.build_descriptor(_segments(points))


def _polygon_graph(points, *, face_key=0):
    """Build a minimal one-face graph using the worker's topology module."""

    topology = WORKER._topology
    points = tuple(points)
    count = len(points)
    loop_keys = tuple((face_key, index) for index in range(count))
    loops = tuple(
        topology.LoopRecord(
            key=key,
            face_key=face_key,
            edge_key=index,
            vertex_key=index,
            next_key=(face_key, (index + 1) % count),
            prev_key=(face_key, (index - 1) % count),
            uv=points[index],
            boundary=True,
            seam=False,
        )
        for index, key in enumerate(loop_keys)
    )
    edges = tuple(
        topology.EdgeRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            face_keys=(face_key,),
            boundary=True,
        )
        for index in range(count)
    )
    vertices = tuple(
        topology.VertexRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            boundary=True,
        )
        for index in range(count)
    )
    return topology.make_graph(
        faces=(topology.FaceRecord(face_key, loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(
            topology.BoundaryComponentRecord(
                key=("outer", face_key),
                loop_keys=loop_keys,
                role="outer",
            ),
        ),
    )


def _worker_task(master_points, member_points, *, exact_tolerance=1.0e-6):
    """Make a pure fused task/state pair without loading a graph context."""

    master = _descriptor(master_points)
    member = _descriptor(member_points)
    master_wire = WORKER.ShapeDescriptor.from_similarity(master)
    member_wire = WORKER.ShapeDescriptor.from_similarity(member)
    payload_globals = WORKER.FusedBatchTask.__init__.__globals__
    identity = payload_globals["SnapshotIdentity"](
        "r2d-focused", 0, "r2d-snapshot"
    )
    context_digest = "r2d-context"
    fused_digest = "r2d-fused"
    exact_options = payload_globals["ExactOptions"](
        allow_flipping=False,
        match_scale=True,
        tolerance=exact_tolerance,
        max_search=1025,
    )
    loop_count = len(ASYMMETRIC)
    pair_cls = WORKER.FusedBatchTask.__init__.__globals__["FusedPairRef"]
    pair = pair_cls(
        pair_ordinal=0,
        master_key=(0,),
        member_key=(1,),
        master_descriptor_digest=master_wire.descriptor_digest,
        member_descriptor_digest=member_wire.descriptor_digest,
        master_loop_keys=tuple((0, index) for index in range(loop_count)),
        member_loop_keys=tuple((1, index) for index in range(loop_count)),
        exact_options=exact_options,
    )
    task = WORKER.FusedBatchTask(
        identity=identity,
        context_digest=context_digest,
        fused_digest=fused_digest,
        batch_id="r2d-focused-batch",
        pair_tasks=(pair,),
    )
    state = WORKER._WorkerState(
        session_nonce=identity.session_nonce,
        generation=identity.generation,
        ready=True,
    )
    # _compute_fused_batch only needs the identity of the already admitted
    # context for this proof; graph construction is deliberately spied below.
    state.graph_context_payload = SimpleNamespace(identity=identity)
    state.graph_context_material = object()
    state.graph_context_digest = context_digest
    state.fused_context_digest = fused_digest
    state.fused_shape_options = WORKER.ShapeOptions(
        match_scale=True,
        allow_flipping=False,
        tolerance=0.01,
        allow_tolerant_topology=True,
        use_numpy=False,
    )
    state.fused_descriptors = {
        master_wire.descriptor_digest: master_wire,
        member_wire.descriptor_digest: member_wire,
    }
    return state, task


class R2DShapeBoundTests(unittest.TestCase):
    def test_bound_is_minimum_over_all_allowed_fits(self):
        reference = _descriptor(ASYMMETRIC)
        reflected = tuple((-x + 4.0, y - 2.0) for x, y in ASYMMETRIC)
        candidate = _descriptor(reflected)
        fits = MATCHER._fit_loop_candidates(
            reference.outer_loops[0],
            candidate.outer_loops[0],
            match_scale=True,
            allow_flipping=True,
            use_numpy=False,
        )
        self.assertGreater(len(fits), 1)
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
        selected_sse = result.transform.rms * result.transform.rms * point_count
        self.assertLessEqual(result.first_outer_min_sse, selected_sse + 1.0e-14)

    def test_equality_and_conservative_epsilon_never_reject(self):
        budget = 7.0e-10
        epsilon = MATCHER.outer_sse_bound_epsilon(budget, budget)
        self.assertFalse(budget > budget + epsilon)
        self.assertFalse(budget + epsilon * 0.5 > budget + epsilon)
        self.assertTrue(budget + epsilon * 2.0 > budget + epsilon)

    def test_multi_open_and_ambiguous_descriptors_skip_bound(self):
        reference = _descriptor(ASYMMETRIC)
        candidate = _descriptor(ASYMMETRIC)
        fits = MATCHER._fit_loop_candidates(
            reference.outer_loops[0],
            candidate.outer_loops[0],
            use_numpy=False,
        )
        loop = candidate.outer_loops[0]
        multi_outer = candidate.__class__(
            face_key=candidate.face_key,
            loops=candidate.loops,
            outer_loops=(loop, loop),
            hole_loops=candidate.hole_loops,
            open_loops=candidate.open_loops,
            topology=candidate.topology,
            bounds=candidate.bounds,
            center=candidate.center,
            boundary_signature=candidate.boundary_signature,
            normalized_shape_signature=candidate.normalized_shape_signature,
            raw_boundary_signature=candidate.raw_boundary_signature,
        )
        open_loop = loop.__class__(
            points=loop.points,
            closed=False,
            status="open",
            perimeter=loop.perimeter,
            signed_area=loop.signed_area,
            area=loop.area,
            winding=loop.winding,
            degenerate=loop.degenerate,
            sample_count=loop.sample_count,
            samples=loop.samples,
            role=loop.role,
            containment_depth=loop.containment_depth,
            parent_outer_index=loop.parent_outer_index,
            component_index=loop.component_index,
        )
        ambiguous_loop = open_loop.__class__(
            points=open_loop.points,
            closed=False,
            status="ambiguous",
            perimeter=open_loop.perimeter,
            signed_area=open_loop.signed_area,
            area=open_loop.area,
            winding=open_loop.winding,
            degenerate=open_loop.degenerate,
            sample_count=open_loop.sample_count,
            samples=open_loop.samples,
            role=open_loop.role,
            containment_depth=open_loop.containment_depth,
            parent_outer_index=open_loop.parent_outer_index,
            component_index=open_loop.component_index,
        )
        open_descriptor = candidate.__class__(
            face_key=candidate.face_key,
            loops=candidate.loops,
            outer_loops=candidate.outer_loops,
            hole_loops=candidate.hole_loops,
            open_loops=(open_loop,),
            topology=candidate.topology,
            bounds=candidate.bounds,
            center=candidate.center,
            boundary_signature=candidate.boundary_signature,
            normalized_shape_signature=candidate.normalized_shape_signature,
            raw_boundary_signature=candidate.raw_boundary_signature,
        )
        ambiguous_descriptor = open_descriptor.__class__(
            face_key=open_descriptor.face_key,
            loops=open_descriptor.loops,
            outer_loops=open_descriptor.outer_loops,
            hole_loops=open_descriptor.hole_loops,
            open_loops=(ambiguous_loop,),
            topology=open_descriptor.topology,
            bounds=open_descriptor.bounds,
            center=open_descriptor.center,
            boundary_signature=open_descriptor.boundary_signature,
            normalized_shape_signature=open_descriptor.normalized_shape_signature,
            raw_boundary_signature=open_descriptor.raw_boundary_signature,
        )
        self.assertEqual(
            MATCHER._first_outer_min_sse(reference, multi_outer, fits),
            (None, 0),
        )
        self.assertEqual(
            MATCHER._first_outer_min_sse(reference, open_descriptor, fits),
            (None, 0),
        )
        self.assertEqual(
            MATCHER._first_outer_min_sse(reference, ambiguous_descriptor, fits),
            (None, 0),
        )

    def test_fused_worker_unsafe_bound_is_disabled_and_exact_remains_reachable(self):
        perturbed = list(ASYMMETRIC)
        perturbed[2] = (perturbed[2][0] + 1.0e-4, perturbed[2][1])
        state, task = _worker_task(ASYMMETRIC, tuple(perturbed))
        graph_data_cls = WORKER.GraphBuildEntry.__init__.__globals__["GraphData"]
        entry_cls = WORKER.GraphBuildEntry
        graphs = {
            (0,): _polygon_graph(ASYMMETRIC, face_key=0),
            (1,): _polygon_graph(tuple(perturbed), face_key=1),
        }

        def graph_batch(_state, graph_task):
            entries = []
            for item in graph_task.graph_items:
                key = tuple(item.island_key)
                data = graph_data_cls.from_topology(graphs[key], "r2d-disabled-%s" % key[0])
                entries.append(
                    entry_cls(
                        island_key=key,
                        material_digest=item.material_digest,
                        accepted=True,
                        graph=data,
                    )
                )
            return SimpleNamespace(
                graph_results=tuple(entries),
                cache_hits=0,
                compute_ms=0.0,
            )

        with mock.patch.object(WORKER, "_compute_graph_batch", side_effect=graph_batch) as graph_mock:
            result = WORKER._compute_fused_batch(state, task)
        outcome = result.outcomes[0]
        self.assertTrue(result.complete)
        self.assertEqual(result.lower_bound_checked, 0)
        self.assertEqual(result.lower_bound_rejected, 0)
        self.assertEqual(result.lower_bound_skipped, 1)
        self.assertEqual(result.lower_bound_graph_pairs_avoided, 0)
        self.assertEqual(graph_mock.call_count, 1)
        self.assertIsNotNone(outcome.exact_result)
        self.assertNotEqual(outcome.exact_result.reason, "geometry_lower_bound_mismatch")

    def test_fused_worker_pass_path_is_deterministic_and_uses_exact(self):
        state, task = _worker_task(ASYMMETRIC, ASYMMETRIC, exact_tolerance=1.0e-6)
        graph_data_cls = WORKER.GraphBuildEntry.__init__.__globals__["GraphData"]
        entry_cls = WORKER.GraphBuildEntry
        graphs = {
            (0,): _polygon_graph(ASYMMETRIC, face_key=0),
            (1,): _polygon_graph(ASYMMETRIC, face_key=1),
        }

        def graph_batch(_state, graph_task):
            entries = []
            for item in graph_task.graph_items:
                key = tuple(item.island_key)
                data = graph_data_cls.from_topology(graphs[key], "r2d-%s" % key[0])
                entries.append(
                    entry_cls(
                        island_key=key,
                        material_digest=item.material_digest,
                        accepted=True,
                        graph=data,
                    )
                )
            return SimpleNamespace(
                graph_results=tuple(entries),
                cache_hits=0,
                compute_ms=0.0,
            )

        with mock.patch.object(WORKER, "_compute_graph_batch", side_effect=graph_batch):
            first = WORKER._compute_fused_batch(state, task)
        state2, task2 = _worker_task(ASYMMETRIC, ASYMMETRIC, exact_tolerance=1.0e-6)
        with mock.patch.object(WORKER, "_compute_graph_batch", side_effect=graph_batch):
            second = WORKER._compute_fused_batch(state2, task2)
        first_outcome = first.outcomes[0]
        second_outcome = second.outcomes[0]
        self.assertEqual(first.lower_bound_rejected, 0)
        self.assertEqual(first_outcome.terminal_reason, "accepted")
        self.assertTrue(first_outcome.exact_result.accepted)
        self.assertEqual(
            first_outcome.exact_result.loop_mapping,
            second_outcome.exact_result.loop_mapping,
        )
        self.assertEqual(first_outcome.exact_result.reason, second_outcome.exact_result.reason)

    def test_exact_rollback_keeps_eight_rounds_without_histogram_gate(self):
        topology = WORKER._topology
        self.assertEqual(topology.CorrespondenceSearch._MAX_REFINEMENT_ROUNDS, 8)
        source = inspect.getsource(topology.CorrespondenceSearch._advance_one)
        refinement_to_domains = source.split(
            'if self._phase == "build_label_buckets":', 1
        )[0]
        self.assertNotIn("_label_histogram", refinement_to_domains)
        self.assertNotIn("topology_signature_mismatch", refinement_to_domains)
        graph = _polygon_graph(ASYMMETRIC)
        result = topology.find_correspondence(
            graph,
            graph,
            tolerance=1.0e-8,
            allow_flipping=False,
        )
        self.assertTrue(result.accepted, result)
        self.assertEqual(result.diagnostics.refinement_max_rounds, 8)
        self.assertEqual(len(result.loop_mapping), len(ASYMMETRIC))


if __name__ == "__main__":
    unittest.main()
