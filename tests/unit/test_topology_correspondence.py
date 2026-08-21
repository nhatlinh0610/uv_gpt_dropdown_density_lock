"""Focused PRO-02A tests for pure exact loop correspondence."""

import importlib.util
import math
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "uv_gpt" / "topology_correspondence.py"
SPEC = importlib.util.spec_from_file_location("uv_gpt_topology_correspondence_test", MODULE_PATH)
TOPOLOGY = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(TOPOLOGY)


def _point_transform(point, angle=0.0, scale=1.0, translate=(0.0, 0.0), reflect=False):
    x, y = point
    if reflect:
        x = -x
    c = math.cos(angle)
    s = math.sin(angle)
    return (
        translate[0] + scale * (c * x - s * y),
        translate[1] + scale * (s * x + c * y),
    )


def _polygon_graph(
    points,
    *,
    order=None,
    seam_indices=(),
    face_key=0,
    boundary_role="outer",
    vertex_signatures=None,
):
    """One-face immutable polygon with explicit loop/edge/vertex incidence."""

    count = len(points)
    if order is None:
        order = tuple(range(count))
    loops = []
    edges = []
    vertices = []
    face_loop_keys = tuple((face_key, index) for index in range(count))
    for index in range(count):
        vertex_index = order[index]
        edge_index = index
        key = (face_key, index)
        loops.append(
            TOPOLOGY.LoopRecord(
                key=key,
                face_key=face_key,
                edge_key=edge_index,
                vertex_key=vertex_index,
                next_key=(face_key, (index + 1) % count),
                prev_key=(face_key, (index - 1) % count),
                uv=tuple(points[vertex_index]),
                boundary=True,
                seam=index in set(seam_indices),
            )
        )
    for index in range(count):
        edges.append(
            TOPOLOGY.EdgeRecord(
                key=index,
                loop_keys=((face_key, index),),
                face_keys=(face_key,),
                boundary=True,
            )
        )
    for index in range(count):
        vertices.append(
            TOPOLOGY.VertexRecord(
                key=order[index],
                loop_keys=((face_key, index),),
                boundary=True,
                signature=()
                if vertex_signatures is None
                else tuple(vertex_signatures[order[index]]),
            )
        )
    return TOPOLOGY.make_graph(
        faces=(TOPOLOGY.FaceRecord(face_key, face_loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(
            TOPOLOGY.BoundaryComponentRecord(
                key="outer-boundary",
                loop_keys=face_loop_keys,
                role=boundary_role,
            ),
        ),
    )


def _annulus_graph(transform=None):
    """Four quads around a hole, with two role/hierarchy boundary rings."""

    outer = ((0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0))
    inner = ((1.0, 1.0), (3.0, 1.0), (3.0, 3.0), (1.0, 3.0))
    points = outer + inner
    if transform is not None:
        points = tuple(transform(point) for point in points)

    # Face i traverses outer[i] -> outer[i+1] -> inner[i+1] -> inner[i].
    face_vertices = (
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    )
    loops = []
    faces = []
    edge_loops = {}
    vertex_loops = {index: [] for index in range(8)}
    edge_keys = {}
    next_edge = 0
    for face_index, vertices_for_face in enumerate(face_vertices):
        cycle = tuple((face_index, local) for local in range(4))
        faces.append(TOPOLOGY.FaceRecord(face_index, cycle))
        for local, vertex_key in enumerate(vertices_for_face):
            edge_vertices = tuple(sorted((vertex_key, vertices_for_face[(local + 1) % 4])))
            if edge_vertices not in edge_keys:
                edge_keys[edge_vertices] = next_edge
                next_edge += 1
            edge_key = edge_keys[edge_vertices]
            loop_key = (face_index, local)
            boundary = edge_vertices in {
                (0, 1),
                (1, 2),
                (2, 3),
                (0, 3),
                (4, 5),
                (5, 6),
                (6, 7),
                (4, 7),
            }
            loops.append(
                TOPOLOGY.LoopRecord(
                    key=loop_key,
                    face_key=face_index,
                    edge_key=edge_key,
                    vertex_key=vertex_key,
                    next_key=(face_index, (local + 1) % 4),
                    prev_key=(face_index, (local - 1) % 4),
                    uv=points[vertex_key],
                    boundary=boundary,
                )
            )
            edge_loops.setdefault(edge_key, []).append(loop_key)
            vertex_loops[vertex_key].append(loop_key)
    edges = []
    for edge_vertices, edge_key in sorted(edge_keys.items(), key=lambda item: item[1]):
        loops_for_edge = tuple(edge_loops[edge_key])
        faces_for_edge = tuple(sorted({key[0] for key in loops_for_edge}))
        edges.append(
            TOPOLOGY.EdgeRecord(
                edge_key,
                loops_for_edge,
                faces_for_edge,
                boundary=len(faces_for_edge) == 1,
            )
        )
    vertices = []
    for vertex_key in range(8):
        vertices.append(
            TOPOLOGY.VertexRecord(
                vertex_key,
                tuple(vertex_loops[vertex_key]),
                boundary=vertex_key in {0, 1, 2, 3, 4, 5, 6, 7},
            )
        )
    # Select actual boundary loops from each radial edge; this deliberately
    # keeps loop identity separate from the shared mesh vertices.
    outer_loops = tuple(
        key for key in edge_loops[edge_keys[(0, 1)]]
        + edge_loops[edge_keys[(1, 2)]]
        + edge_loops[edge_keys[(2, 3)]]
        + edge_loops[edge_keys[(0, 3)]]
    )
    inner_loops = tuple(
        key for key in edge_loops[edge_keys[(4, 5)]]
        + edge_loops[edge_keys[(5, 6)]]
        + edge_loops[edge_keys[(6, 7)]]
        + edge_loops[edge_keys[(4, 7)]]
    )
    return TOPOLOGY.make_graph(
        faces=faces,
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(
            TOPOLOGY.BoundaryComponentRecord("outer", outer_loops, "outer"),
            TOPOLOGY.BoundaryComponentRecord("hole", inner_loops, "hole", "outer"),
        ),
    )


class TopologyCorrespondenceTests(unittest.TestCase):
    def test_import_is_pure_and_has_no_blender_dependency(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertNotIn("import bpy", source)
        self.assertNotIn("import bmesh", source)
        probe = (
            "import importlib.util, sys; "
            "path = %r; "
            "spec = importlib.util.spec_from_file_location('pure_probe', path); "
            "module = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(module); "
            "assert 'bpy' not in sys.modules and 'bmesh' not in sys.modules"
        ) % str(MODULE_PATH)
        subprocess.run(
            [sys.executable, "-c", probe],
            check=True,
            capture_output=True,
            text=True,
        )
        package_probe = (
            "import sys; sys.path.insert(0, %r); "
            "import uv_gpt.topology_correspondence as module; "
            "assert module.LoopRecord and 'bpy' not in sys.modules"
        ) % str(ROOT)
        subprocess.run(
            [sys.executable, "-c", package_probe],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_identical_topology_returns_complete_bijection(self):
        points = ((0.0, 0.0), (3.0, 0.0), (3.5, 2.0), (1.2, 3.0), (-0.5, 1.0))
        result = TOPOLOGY.find_correspondence(
            _polygon_graph(points), _polygon_graph(points), tolerance=1.0e-8
        )
        self.assertTrue(result.accepted, result)
        self.assertEqual(len(result.loop_mapping), 5)
        self.assertEqual({pair[0] for pair in result.loop_mapping}, {(0, i) for i in range(5)})
        self.assertEqual({pair[1] for pair in result.loop_mapping}, {(0, i) for i in range(5)})
        self.assertEqual(len(result.mapping), 5)

    def test_default_search_matches_explicit_disabled_cooperative_mode(self):
        points = ((0.0, 0.0), (3.0, 0.0), (3.5, 2.0), (1.2, 3.0), (-0.5, 1.0))
        master = _polygon_graph(points)
        candidate = _polygon_graph(points)
        default = TOPOLOGY.find_correspondence(
            master,
            candidate,
            tolerance=1.0e-8,
            max_search=10000,
        )
        disabled = TOPOLOGY.find_correspondence(
            master,
            candidate,
            tolerance=1.0e-8,
            max_search=10000,
            cooperative_yield_every=0,
        )
        self.assertEqual(default.accepted, disabled.accepted)
        self.assertEqual(default.reason, disabled.reason)
        self.assertEqual(default.loop_mapping, disabled.loop_mapping)
        self.assertEqual(default.score, disabled.score)
        self.assertEqual(
            default.diagnostics.search_count,
            disabled.diagnostics.search_count,
        )
        self.assertEqual(default.diagnostics.yield_count, 0)
        self.assertEqual(disabled.diagnostics.yield_count, 0)

    def test_cooperative_yield_is_disabled_by_default_and_bounded_when_enabled(self):
        square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        graph = _polygon_graph(square)
        baseline = TOPOLOGY.find_correspondence(
            graph,
            graph,
            tolerance=1.0e-8,
            max_search=10000,
        )
        yielded = TOPOLOGY.find_correspondence(
            graph,
            graph,
            tolerance=1.0e-8,
            max_search=10000,
            cooperative_yield_every=2,
        )
        self.assertEqual(yielded.loop_mapping, baseline.loop_mapping)
        self.assertEqual(yielded.reason, baseline.reason)
        self.assertEqual(yielded.score, baseline.score)
        self.assertEqual(
            yielded.diagnostics.search_count,
            baseline.diagnostics.search_count,
        )
        self.assertEqual(
            yielded.diagnostics.yield_count,
            baseline.diagnostics.search_count // 2,
        )
        self.assertEqual(
            TOPOLOGY.find_correspondence(
                graph,
                graph,
                tolerance=1.0e-8,
                max_search=10000,
                cooperative_yield_every=-4,
            ).diagnostics.yield_count,
            0,
        )

    def test_resumable_engine_matches_synchronous_result_and_diagnostics(self):
        master = _annulus_graph()
        candidate = _annulus_graph(
            lambda point: _point_transform(
                point,
                angle=0.31,
                scale=0.75,
                translate=(2.0, 1.0),
            )
        )
        kwargs = {
            "tolerance": 1.0e-8,
            "allow_flipping": False,
            "match_scale": True,
            "max_search": 10000,
        }
        synchronous = TOPOLOGY.find_correspondence(master, candidate, **kwargs)
        engine = TOPOLOGY.CorrespondenceSearch(master, candidate, **kwargs)
        steps = 0
        while not engine.done:
            step = engine.step(operation_budget=1)
            self.assertLessEqual(step.operations, 1)
            self.assertIn(step.status, {"pending", "success", "failure"})
            steps += 1
            self.assertLess(steps, 10000)
        resumable = engine.result
        self.assertIsNotNone(resumable)
        self.assertEqual(resumable.accepted, synchronous.accepted)
        self.assertEqual(resumable.reason, synchronous.reason)
        self.assertEqual(resumable.loop_mapping, synchronous.loop_mapping)
        self.assertEqual(resumable.reflected, synchronous.reflected)
        self.assertEqual(resumable.reversed, synchronous.reversed)
        self.assertEqual(resumable.cyclic_shift, synchronous.cyclic_shift)
        self.assertEqual(resumable.score, synchronous.score)
        self.assertEqual(resumable.residual, synchronous.residual)
        self.assertEqual(resumable.diagnostics, synchronous.diagnostics)

    def test_resumable_engine_deadline_and_operation_budget_are_bounded(self):
        square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        engine = TOPOLOGY.CorrespondenceSearch(
            _polygon_graph(square),
            _polygon_graph(square),
            tolerance=1.0e-8,
            max_search=10000,
        )
        first = engine.step(operation_budget=1)
        self.assertEqual(first.operations, 1)
        self.assertEqual(first.status, "pending")
        self.assertFalse(engine.done)
        second = engine.step(deadline=__import__("time").monotonic())
        self.assertEqual(second.operations, 0)
        self.assertEqual(second.status, "pending")
        self.assertFalse(engine.done)
        while not engine.done:
            step = engine.step(operation_budget=2)
            self.assertLessEqual(step.operations, 2)
        self.assertTrue(engine.result.accepted)

    def test_resumable_engine_cancel_discards_partial_mapping(self):
        square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        engine = TOPOLOGY.CorrespondenceSearch(
            _polygon_graph(square),
            _polygon_graph(square),
            tolerance=1.0e-8,
            max_search=10000,
        )
        engine.step(operation_budget=4)
        self.assertFalse(engine.done)
        engine.cancel()
        cancelled = engine.step(operation_budget=100)
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIsNone(cancelled.result)
        self.assertIsNone(engine.result)
        self.assertEqual(engine.state, "cancelled")

    def test_graph_records_are_frozen_and_incidence_is_tuple_based(self):
        graph = _polygon_graph(((0.0, 0.0), (2.0, 0.0), (2.0, 1.0)))
        with self.assertRaises(AttributeError):
            graph.loops = ()
        self.assertIsInstance(graph.loops, tuple)
        self.assertIsInstance(graph.loops[0].key, tuple)

    def test_cyclic_boundary_shift_is_resolved_by_full_loop_residual(self):
        master_points = ((0.0, 0.0), (4.0, 0.0), (5.0, 2.0), (2.0, 4.0), (-1.0, 2.0))
        shift = 2
        candidate_points = tuple(master_points[(index + shift) % len(master_points)] for index in range(5))
        result = TOPOLOGY.find_correspondence(
            _polygon_graph(master_points), _polygon_graph(candidate_points), tolerance=1.0e-8
        )
        self.assertTrue(result.accepted, result)
        self.assertAlmostEqual(result.residual, 0.0, places=7)
        self.assertEqual(
            result.mapping,
            {(0, index): (0, (index + shift) % 5) for index in range(5)},
        )

    def test_reversed_cycle_is_allowed_and_reported(self):
        master_points = ((0.0, 0.0), (4.0, 0.0), (5.0, 2.0), (2.0, 4.0), (-1.0, 2.0))
        # Vertex signatures anchor the physical vertex identities, while the
        # candidate face cycle is reversed.  This makes the orientation choice
        # explicit instead of relying on a symmetric polygon automorphism.
        candidate = _polygon_graph(
            master_points,
            order=tuple(reversed(range(5))),
            vertex_signatures={index: ("vertex", index) for index in range(5)},
        )
        master = _polygon_graph(
            master_points,
            vertex_signatures={index: ("vertex", index) for index in range(5)},
        )
        rejected = TOPOLOGY.find_correspondence(
            master, candidate, tolerance=1.0e-8, allow_flipping=False
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "reflection_not_allowed")
        result = TOPOLOGY.find_correspondence(
            master, candidate, tolerance=1.0e-8, allow_flipping=True
        )
        self.assertTrue(result.accepted, result)
        self.assertTrue(result.reversed)
        self.assertTrue(result.reflected)
        self.assertEqual(len(result.loop_mapping), 5)

    def test_reflection_is_gated_by_allow_flipping(self):
        points = ((0.0, 0.0), (4.0, 0.0), (5.0, 2.0), (2.0, 4.0), (-1.0, 2.0))
        reflected = tuple(_point_transform(point, scale=1.7, translate=(4.0, -2.0), reflect=True) for point in points)
        rejected = TOPOLOGY.find_correspondence(
            _polygon_graph(points), _polygon_graph(reflected), tolerance=1.0e-8, allow_flipping=False
        )
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.reason, "reflection_not_allowed")
        accepted = TOPOLOGY.find_correspondence(
            _polygon_graph(points), _polygon_graph(reflected), tolerance=1.0e-8, allow_flipping=True
        )
        self.assertTrue(accepted.accepted, accepted)
        self.assertTrue(accepted.reflected)
        self.assertLess(accepted.residual, 1.0e-7)

    def test_loop_refinement_keeps_baseline_eight_round_cap(self):
        points = ((0.0, 0.0), (4.0, 0.0), (5.0, 2.0), (2.0, 4.0), (-1.0, 2.0))
        master = _polygon_graph(
            points,
            vertex_signatures={index: ("vertex", index) for index in range(5)},
        )
        candidate = _polygon_graph(
            points,
            order=tuple(reversed(range(5))),
            vertex_signatures={index: ("vertex", index) for index in range(5)},
        )
        result = TOPOLOGY.find_correspondence(
            master,
            candidate,
            tolerance=1.0e-8,
            allow_flipping=True,
        )
        self.assertTrue(result.accepted, result)
        self.assertEqual(result.diagnostics.refinement_max_rounds, 8)
        self.assertGreaterEqual(result.diagnostics.refinement_rounds, 1)
        self.assertEqual(len(result.loop_mapping), 5)

    def test_refinement_diagnostics_do_not_change_exact_search_semantics(self):
        master = _annulus_graph()
        candidate = _annulus_graph(
            lambda point: _point_transform(
                point,
                angle=0.31,
                scale=0.75,
                translate=(2.0, 1.0),
            )
        )
        result = TOPOLOGY.find_correspondence(
            master,
            candidate,
            tolerance=1.0e-8,
        )
        self.assertTrue(result.accepted, result)
        self.assertEqual(result.diagnostics.refinement_max_rounds, 8)
        self.assertGreater(result.diagnostics.search_count, 0)
        self.assertEqual(len(result.loop_mapping), 16)

    def test_hole_role_and_hierarchy_are_part_of_exact_match(self):
        transform = lambda point: _point_transform(point, angle=0.31, scale=0.75, translate=(2.0, 1.0))
        result = TOPOLOGY.find_correspondence(
            _annulus_graph(), _annulus_graph(transform), tolerance=1.0e-8
        )
        self.assertTrue(result.accepted, result)
        self.assertEqual(len(result.loop_mapping), 16)
        self.assertEqual(result.diagnostics.complete_mappings > 0, True)

    def test_interior_vertex_and_faces_propagate_through_all_loops(self):
        # A 2x2 quad patch: the center vertex and four radial edges are
        # interior while the outer ring is boundary.
        points = {
            0: (0.0, 0.0), 1: (2.0, 0.0), 2: (4.0, 0.0),
            3: (0.0, 2.0), 4: (2.0, 2.0), 5: (4.0, 2.0),
            6: (0.0, 4.0), 7: (2.0, 4.0), 8: (4.0, 4.0),
        }
        face_vertices = ((0, 1, 4, 3), (1, 2, 5, 4), (3, 4, 7, 6), (4, 5, 8, 7))
        graph = _grid_graph(points, face_vertices)
        result = TOPOLOGY.find_correspondence(graph, graph, tolerance=1.0e-8)
        self.assertTrue(result.accepted, result)
        self.assertEqual(len(result.loop_mapping), 16)
        center_loops = {loop.key for loop in graph.loops if loop.vertex_key == 4}
        self.assertEqual(len(center_loops), 4)
        self.assertEqual(len({result.mapping[key] for key in center_loops}), 4)

    def test_shared_mesh_vertex_keeps_distinct_uv_loops_and_seam_pattern(self):
        # Two faces share only mesh vertex 0.  Their loops remain distinct and
        # carry different UVs/seam flags; the correspondence cannot collapse
        # them into one vertex coordinate.
        loops = (
            TOPOLOGY.LoopRecord((0, 0), 0, 0, 0, (0, 1), (0, 2), (0.0, 0.0), True, True),
            TOPOLOGY.LoopRecord((0, 1), 0, 1, 1, (0, 2), (0, 0), (1.0, 0.0), True, False),
            TOPOLOGY.LoopRecord((0, 2), 0, 2, 2, (0, 0), (0, 1), (0.0, 1.0), True, False),
            TOPOLOGY.LoopRecord((1, 0), 1, 3, 0, (1, 1), (1, 2), (0.2, 0.2), True, True),
            TOPOLOGY.LoopRecord((1, 1), 1, 4, 3, (1, 2), (1, 0), (1.2, 0.2), True, False),
            TOPOLOGY.LoopRecord((1, 2), 1, 5, 4, (1, 0), (1, 1), (0.2, 1.2), True, False),
        )
        edges = tuple(
            TOPOLOGY.EdgeRecord(index, ((index // 3, index % 3),), (index // 3,), True)
            for index in range(6)
        )
        vertices = (
            TOPOLOGY.VertexRecord(0, ((0, 0), (1, 0)), True),
            TOPOLOGY.VertexRecord(1, ((0, 1),), True),
            TOPOLOGY.VertexRecord(2, ((0, 2),), True),
            TOPOLOGY.VertexRecord(3, ((1, 1),), True),
            TOPOLOGY.VertexRecord(4, ((1, 2),), True),
        )
        graph = TOPOLOGY.make_graph(
            faces=(TOPOLOGY.FaceRecord(0, ((0, 0), (0, 1), (0, 2))), TOPOLOGY.FaceRecord(1, ((1, 0), (1, 1), (1, 2)))),
            edges=edges,
            vertices=vertices,
            loops=loops,
            boundaries=(
                TOPOLOGY.BoundaryComponentRecord("a", ((0, 0), (0, 1), (0, 2)), "outer"),
                TOPOLOGY.BoundaryComponentRecord("b", ((1, 0), (1, 1), (1, 2)), "outer"),
            ),
        )
        result = TOPOLOGY.find_correspondence(graph, graph, tolerance=1.0e-8)
        self.assertTrue(result.accepted, result)
        self.assertEqual(result.mapping[(0, 0)], (0, 0))
        self.assertEqual(result.mapping[(1, 0)], (1, 0))
        self.assertNotEqual(result.mapping[(0, 0)], result.mapping[(1, 0)])

    def test_same_counts_but_ordered_adjacency_mismatch_rejects(self):
        points = ((0.0, 0.0), (4.0, 0.0), (5.0, 2.0), (2.0, 4.0), (-1.0, 2.0))
        master = _polygon_graph(points)
        bad_loops = list(master.loops)
        # Keep counts and all node histograms, but break the ordered cycle.
        bad_loops[1] = TOPOLOGY.LoopRecord(
            key=bad_loops[1].key,
            face_key=bad_loops[1].face_key,
            edge_key=bad_loops[1].edge_key,
            vertex_key=bad_loops[1].vertex_key,
            next_key=(0, 3),
            prev_key=(0, 0),
            uv=bad_loops[1].uv,
            boundary=bad_loops[1].boundary,
        )
        bad = TOPOLOGY.make_graph(master.faces, master.edges, master.vertices, bad_loops, master.boundaries)
        result = TOPOLOGY.find_correspondence(master, bad)
        self.assertFalse(result.accepted)
        self.assertTrue(result.reason.startswith("invalid_record_"))

    def test_degenerate_uv_geometry_is_rejected_explicitly(self):
        degenerate = _polygon_graph(((0.0, 0.0),) * 5)
        result = TOPOLOGY.find_correspondence(degenerate, degenerate)
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "degenerate_uv_geometry")

    def test_symmetric_ambiguity_is_repeatable_and_lexical(self):
        square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        first = TOPOLOGY.find_correspondence(
            _polygon_graph(square), _polygon_graph(square), tolerance=1.0e-8, max_search=10000
        )
        second = TOPOLOGY.find_correspondence(
            _polygon_graph(square), _polygon_graph(square), tolerance=1.0e-8, max_search=10000
        )
        self.assertTrue(first.accepted, first)
        self.assertEqual(first.loop_mapping, second.loop_mapping)
        self.assertEqual(
            first.loop_mapping,
            tuple(((0, index), (0, index)) for index in range(4)),
        )

    def test_branch_budget_overrun_rejects_even_if_partial_mapping_exists(self):
        square = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))
        result = TOPOLOGY.find_correspondence(
            _polygon_graph(square), _polygon_graph(square), tolerance=1.0e-8, max_search=1
        )
        self.assertFalse(result.accepted)
        self.assertEqual(result.reason, "search_budget_exceeded")
        self.assertGreaterEqual(result.diagnostics.search_count, 2)


def _grid_graph(points, face_vertices):
    loops = []
    faces = []
    edge_map = {}
    edge_loops = {}
    vertex_loops = {key: [] for key in points}
    for face_index, vertices_for_face in enumerate(face_vertices):
        cycle = tuple((face_index, local) for local in range(4))
        faces.append(TOPOLOGY.FaceRecord(face_index, cycle))
        for local, vertex_key in enumerate(vertices_for_face):
            edge_vertices = tuple(sorted((vertex_key, vertices_for_face[(local + 1) % 4])))
            edge_key = edge_map.setdefault(edge_vertices, len(edge_map))
            boundary = sum(1 for face in face_vertices if edge_vertices[0] in face and edge_vertices[1] in face) == 1
            loop_key = (face_index, local)
            loops.append(
                TOPOLOGY.LoopRecord(
                    loop_key, face_index, edge_key, vertex_key,
                    (face_index, (local + 1) % 4),
                    (face_index, (local - 1) % 4),
                    tuple(points[vertex_key]), boundary, False,
                )
            )
            edge_loops.setdefault(edge_key, []).append(loop_key)
            vertex_loops[vertex_key].append(loop_key)
    edges = []
    for edge_vertices, edge_key in sorted(edge_map.items(), key=lambda item: item[1]):
        loops_for_edge = tuple(edge_loops[edge_key])
        faces_for_edge = tuple(sorted({key[0] for key in loops_for_edge}))
        edges.append(TOPOLOGY.EdgeRecord(edge_key, loops_for_edge, faces_for_edge, len(faces_for_edge) == 1))
    vertices = []
    for vertex_key, loops_for_vertex in vertex_loops.items():
        boundary = any(
            next(edge.boundary for edge in edges if edge.key == next(loop.edge_key for loop in loops if loop.key == key))
            for key in loops_for_vertex
        )
        vertices.append(TOPOLOGY.VertexRecord(vertex_key, tuple(loops_for_vertex), boundary))
    boundary_loops = tuple(loop.key for loop in loops if loop.boundary)
    return TOPOLOGY.make_graph(
        faces, edges, vertices, loops,
        (TOPOLOGY.BoundaryComponentRecord("outer", boundary_loops, "outer"),),
    )


if __name__ == "__main__":
    unittest.main()
