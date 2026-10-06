"""Regression coverage for Bottom's UV cuts and mirrored face winding."""
import sys
import unittest
import tempfile
import ast
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "uv_gpt"))
import pro_exact_v2_core as core
import pro_exact_v2_worker as worker


def grid_faces(offset=0, reverse=False, slit=False, asymmetric=False):
    # Four quads remain connected around an internal slit from the lower edge.
    cycles = [(0, 1, 4, 3), (1, 2, 5, 4), (3, 4, 7, 6), (4, 5, 8, 7)]
    if asymmetric:
        cycles[-1:] = [(4, 5, 8), (4, 8, 7)]
    edges = {}
    faces = []
    for index, cycle in enumerate(cycles):
        if reverse:
            cycle = tuple(reversed(cycle))
        loops = []
        for j, vertex in enumerate(cycle):
            edge = tuple(sorted((vertex, cycle[(j + 1) % len(cycle)])))
            edge_id = edges.setdefault(edge, len(edges)) + offset * 20
            x, y = vertex % 3, vertex // 3
            if slit and index == 1 and vertex == 1:
                x += 0.25
            loops.append((vertex + offset * 20, edge_id, float(x + offset * 4), float(y)))
        faces.append((offset * 10 + index, True, False, tuple(loops)))
    return faces


class ProExactUVCutsTests(unittest.TestCase):
    def test_apply_acquires_bmesh_only_on_tick_after_undo_push(self):
        source = Path(__file__).resolve().parents[2] / "uv_gpt" / "stack_tools.py"
        node = next(n for n in ast.parse(source.read_text(encoding="utf-8")).body
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_pro_exact_v2_apply_pending_updates")
        calls = []
        def forbidden_mesh_access(_mesh):
            raise AssertionError("BMesh acquired in the undo-push tick")
        namespace = {
            "Path": Path,
            "_fast_v2_source_context_matches": lambda *_: True,
            "bpy": SimpleNamespace(context=None,
                data=SimpleNamespace(objects={"test": SimpleNamespace(data=None)}),
                ops=SimpleNamespace(ed=SimpleNamespace(undo_push=lambda **_: calls.append("undo")))),
            "bmesh": SimpleNamespace(from_edit_mesh=forbidden_mesh_access),
        }
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "00000001.pkl").write_bytes(b"")
            job = {"object_name": "test", "updates_dir": directory, "undo_pushed": False}
            self.assertEqual(namespace[node.name](job), "wait")
            self.assertTrue(job["undo_pushed"])
            self.assertEqual(calls, ["undo"])

    def test_progress_sharing_violation_does_not_abort_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "progress.json"
            path.write_text('{"percent":1}', encoding="utf-8")
            with patch.object(worker.os, "replace", side_effect=PermissionError("sharing")), \
                    patch.object(worker.time, "sleep"):
                self.assertFalse(worker._atomic_json(path, {"percent": 2}))
            self.assertEqual(path.read_text(encoding="utf-8"), '{"percent":1}')
            self.assertTrue(worker._atomic_json(path, {"percent": 100}))
            self.assertEqual(path.read_text(encoding="utf-8"), '{"percent":100}')

    def test_internal_slit_builds_closed_uv_graph(self):
        snapshot = {"schema": core.SCHEMA, "faces": grid_faces(slit=True)}
        islands = core.build_exact_islands(snapshot)
        self.assertEqual(len(islands), 1)
        graph = islands[0].graph
        self.assertEqual(len(graph.edges), 13)  # Twelve mesh edges + split cut.
        self.assertEqual(len(graph.vertices), 10)
        self.assertEqual(len(graph.boundaries), 1)
        core.tc._validate_graph(graph)

    def test_reversed_winding_with_slit_copies_every_target_corner(self):
        snapshot = {"schema": core.SCHEMA, "faces": (
            grid_faces(slit=True, asymmetric=True)
            + grid_faces(1, reverse=True, slit=True, asymmetric=True))}
        islands = core.build_exact_islands(snapshot)
        master, target = islands
        self.assertIsNone(core._find_oriented_exact_mapping(master.graph, target.graph))
        mapping = core.find_deterministic_exact_mapping(master.graph, target.graph)
        self.assertIsNotNone(mapping)
        self.assertEqual({a for a, _ in mapping}, set(dict(target.loop_uvs)))
        self.assertEqual({b for _, b in mapping}, set(dict(master.loop_uvs)))
        reversed_target = core.tc._validate_graph(core._reverse_graph_winding(target.graph))
        self.assertTrue(any(core.tc._verify_full_mapping(
            graph, core.tc._validate_graph(master.graph), dict(mapping)) is not None
            for graph in (core.tc._validate_graph(target.graph), reversed_target)))
        result = core.solve_exact(snapshot)
        self.assertEqual(result["aligned_count"], 1)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(len(result["writes"]), 18)
        expected = dict(master.loop_uvs)
        written = {(f, j): (u, v) for f, j, u, v in result["writes"]}
        self.assertEqual(written, {a: expected[b] for a, b in mapping})

    def test_singleton_topology_is_reported(self):
        faces = grid_faces() + [(100, True, False, (
            (100, 100, 4., 0.), (101, 101, 5., 0.), (102, 102, 4., 1.)))]
        result = core.solve_exact({"schema": core.SCHEMA, "faces": faces})
        self.assertEqual(result["aligned_count"], 0)
        self.assertEqual(result["skipped_count"], 2)
        self.assertEqual(result["master_count"], 0)
        self.assertEqual({item["reason"] for item in result["unmatched_islands"]},
                         {"no_topology_peer"})
        self.assertEqual(result["writes"], ())

    def test_unproven_group_does_not_count_an_unmatched_master_as_success(self):
        snapshot = {"schema": core.SCHEMA, "faces": grid_faces() + grid_faces(1)}
        with patch.object(core, "find_deterministic_exact_mapping", return_value=None), \
                patch.object(core.tc, "find_correspondence", return_value=SimpleNamespace(
                    accepted=False, reason="search_budget_exhausted")):
            result = core.solve_exact(snapshot)
        self.assertEqual(result["aligned_count"], 0)
        self.assertEqual(result["master_count"], 0)
        self.assertEqual(result["skipped_count"], 2)
        self.assertIn("search_budget_exhausted", [i["reason"] for i in result["unmatched_islands"]])


if __name__ == "__main__":
    unittest.main()
