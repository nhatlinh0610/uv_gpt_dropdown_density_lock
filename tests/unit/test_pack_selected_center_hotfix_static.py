"""Static contracts for ZIP-authoritative selection and Pack V2 routes.

These checks deliberately inspect source and AST only. Blender integration,
external worker execution, and exact numeric stack verification belong to Z3.
"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "uv_gpt"


def _source(name):
    return (SOURCE_ROOT / name).read_text(encoding="utf-8")


def _tree(name):
    return ast.parse(_source(name), filename=name)


def _function(tree, name):
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    )


def _class(tree, name):
    return next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _node_source(name, node):
    source = _source(name)
    return ast.get_source_segment(source, node)


class PackSelectedCenterHotfixStaticTests(unittest.TestCase):
    def test_changed_modules_compile_as_ast(self):
        for name in (
            "island_tools.py",
            "uv_utils.py",
            "pack_tools.py",
            "pack_v2_core.py",
            "transform_tools.py",
        ):
            compile(_source(name), name, "exec")

    def test_uv_sync_on_uses_mesh_selection_without_stale_validity_gate(self):
        source = _source("island_tools.py")
        tree = _tree("island_tools.py")
        for name in (
            "_context_uv_select_sync",
            "_mesh_select_mode",
            "_face_mesh_selected_for_uv_sync",
            "_face_uv_selected_for_context",
            "validate_uv_selection_scope",
            "get_selected_uv_islands_for_context",
        ):
            _function(tree, name)

        predicate = _node_source(
            "island_tools.py",
            _function(tree, "_face_mesh_selected_for_uv_sync"),
        )
        validation = _node_source(
            "island_tools.py",
            _function(tree, "validate_uv_selection_scope"),
        )
        self.assertIn("_mesh_select_mode(context)", predicate)
        self.assertIn('getattr(edge, "select", False)', predicate)
        self.assertIn('getattr(vert, "select", False)', predicate)
        self.assertIn("if _context_uv_select_sync(context):", validation)
        self.assertIn("return", validation)
        self.assertIn("def refresh_uv_selection_scope", source)
        self.assertIn("uv_select_sync_from_mesh", source)
        self.assertNotIn("uv_select_sync_valid", validation)

    def test_legacy_refresh_helper_remains_nondestructive_compatibility(self):
        source = _source("island_tools.py")
        refresh = _node_source(
            "island_tools.py",
            _function(_tree("island_tools.py"), "refresh_uv_selection_scope"),
        )
        for marker in (
            "uv_select_sync_from_mesh",
            "_snapshot_uv_selection_state",
            "_restore_uv_selection_state",
            "bmesh.update_edit_mesh",
            "destructive=False",
            "uv_select_sync_valid",
        ):
            self.assertIn(marker, refresh)
        for forbidden in (
            "unwrap",
            "smart_project",
            "pack_islands",
            "select_all",
            "set_all_uv_selection",
        ):
            self.assertNotIn(forbidden, refresh)
        self.assertIn("without changing UVs", refresh)
        self.assertIn("refresh_uv_selection_scope", source)

    def test_selected_pack_capture_preserves_context_and_active_uv_identity(self):
        source = _source("pack_tools.py")
        capture = _node_source(
            "pack_tools.py",
            _function(_tree("pack_tools.py"), "_pack_v2_capture_snapshot"),
        )
        self.assertIn("validate_uv_selection_scope", capture)
        self.assertIn("world_area_by_face", capture)
        self.assertIn("selected", capture)
        self.assertIn("scale_to_fit", capture)
        self.assertIn("_pack_v2_snapshot_unchanged", source)
        self.assertIn("uv_map_name", source)
        self.assertIn("_pack_v2_source_context_matches", source)

    def test_pack_v2_operators_route_to_external_worker(self):
        source = _source("pack_tools.py")
        tree = _tree("pack_tools.py")
        expected = {
            "UVGPT_OT_pack_selected": '"selected"',
            "UVGPT_OT_pack_symmetry": '"symmetry"',
            "UVGPT_OT_pack_whole_mesh": '"whole"',
        }
        for class_name, mode in expected.items():
            operator = _node_source("pack_tools.py", _class(tree, class_name))
            self.assertIn("_pack_v2_start_job(context, ", operator)
            self.assertIn(mode, operator)
            self.assertNotIn("_pack(context", operator)
        self.assertIn("pack_v2_worker.py", source)
        self.assertIn("subprocess.Popen", source)
        self.assertIn("threading.Thread", source)
        self.assertIn("_pack_v2_background_timer", source)

    def test_pack_v2_uses_actual_geometry_and_static_blockers(self):
        pack = _source("pack_tools.py")
        core = _source("pack_v2_core.py")
        geometry = _source("pack_geometry.py")
        self.assertIn("_scope_static_islands", pack)
        self.assertIn("static_islands", pack)
        self.assertIn("world_area_by_face", pack)
        self.assertIn("boundary_loops_from_segments", core)
        self.assertIn("shape_sets_conflict", core)
        self.assertIn("static_shapes", core)
        self.assertIn("pair_similar_records", core)
        self.assertIn("concave", core.lower())
        self.assertIn("polygons_conflict", geometry)

    def test_center_selected_uses_zip_selection_helper_before_destructive_boundary(self):
        source = _source("transform_tools.py")
        tree = _tree("transform_tools.py")
        selected_source = _node_source(
            "transform_tools.py",
            _function(tree, "_selected_islands"),
        )
        self.assertIn("get_selected_uv_islands_for_context", selected_source)
        self.assertLess(
            selected_source.index("get_selected_uv_islands_for_context"),
            selected_source.index("ensure_destructive_ready"),
        )
        self.assertNotIn("refresh_uv_selection_scope", selected_source)
        self.assertNotIn("get_selected_uv_islands(bm, uv_layer)", selected_source)

    def test_active_uv_helpers_and_generic_legacy_helpers_remain_available(self):
        island_source = _source("island_tools.py")
        uv_source = _source("uv_utils.py")
        self.assertIn("def get_selected_uv_islands(bm, uv_layer):", island_source)
        self.assertIn("def select_islands(bm, uv_layer, islands):", uv_source)
        self.assertIn("def select_uv_islands(context, bm, uv_layer, islands):", uv_source)
        self.assertIn("def set_active_uv_map(context, name):", uv_source)
        self.assertIn("obj.data.uv_layers.active", uv_source)


if __name__ == "__main__":
    unittest.main()
