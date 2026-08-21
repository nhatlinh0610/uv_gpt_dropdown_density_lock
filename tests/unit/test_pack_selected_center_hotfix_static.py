"""Static contracts for the S2 UV selection-scope hotfix."""

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


class PackSelectedCenterHotfixStaticTests(unittest.TestCase):
    def test_changed_modules_compile_as_ast(self):
        for name in ("island_tools.py", "uv_utils.py", "pack_tools.py", "transform_tools.py"):
            compile(_source(name), name, "exec")

    def test_uv_only_scope_is_explicit_and_fail_closed(self):
        source = _source("island_tools.py")
        tree = _tree("island_tools.py")
        for name in (
            "_context_uv_select_sync",
            "_face_uv_selected_for_context",
            "validate_uv_selection_scope",
            "get_selected_uv_islands_for_context",
        ):
            _function(tree, name)
        self.assertIn("uv_select_sync_valid", source)
        self.assertIn("uv_select_edge", source)
        self.assertIn("uv_select_vert", source)
        self.assertIn("disable UV Sync", source)
        predicate = ast.get_source_segment(source, _function(tree, "_face_uv_selected_for_context"))
        self.assertNotIn("loop.edge.select", predicate)
        self.assertNotIn("loop.vert.select", predicate)

    def test_invalid_sync_refresh_is_nondestructive_and_prewrite(self):
        source = _source("island_tools.py")
        tree = _tree("island_tools.py")
        refresh = ast.get_source_segment(
            source,
            _function(tree, "refresh_uv_selection_scope"),
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

        validation = ast.get_source_segment(
            source,
            _function(tree, "validate_uv_selection_scope"),
        )
        self.assertIn("refresh_invalid_sync=False", validation)
        self.assertIn("refresh_uv_selection_scope", validation)

    def test_pack_uses_scope_snapshot_native_guard_and_finally_restore(self):
        source = _source("pack_tools.py")
        tree = _tree("pack_tools.py")
        pack = _function(tree, "_pack")
        pack_source = ast.get_source_segment(source, pack)
        for marker in (
            "validate_uv_selection_scope",
            "get_selected_uv_islands_for_context",
            "store_uv_selection_state",
            "select_uv_islands",
            "_uv_snapshot(all_islands",
            "_uv_snapshot_matches",
            "restore_uv_selection_state",
            "finally:",
        ):
            self.assertIn(marker, pack_source)
        self.assertLess(
            pack_source.index("validate_uv_selection_scope"),
            pack_source.index("ensure_destructive_ready"),
        )
        self.assertIn("refresh_invalid_sync=True", pack_source)
        self.assertEqual(pack_source.count("refresh_invalid_sync=True"), 1)
        self.assertIn("Pack Selected changed unselected UVs", pack_source)

    def test_selected_pack_uses_explicit_internal_backend_not_native(self):
        source = _source("pack_tools.py")
        tree = _tree("pack_tools.py")
        pack_source = ast.get_source_segment(source, _function(tree, "_pack"))
        selected_route = pack_source.index("if selected_only:")
        native_route = pack_source.index("uv_utils.run_uv_pack(")
        whole_mesh_route = pack_source.index("Pack Whole Mesh retains")
        self.assertLess(selected_route, native_route)
        self.assertLess(whole_mesh_route, native_route)
        self.assertIn("uv_utils.basic_pack_islands(", pack_source)
        self.assertIn("cannot mutate an unselected loop", pack_source)

    def test_center_validates_uv_scope_before_destructive_boundary(self):
        source = _source("transform_tools.py")
        tree = _tree("transform_tools.py")
        selected_source = ast.get_source_segment(
            source,
            _function(tree, "_selected_islands"),
        )
        self.assertLess(
            selected_source.index("get_selected_uv_islands_for_context"),
            selected_source.index("ensure_destructive_ready"),
        )
        self.assertIn("refresh_invalid_sync=True", selected_source)
        self.assertEqual(selected_source.count("refresh_invalid_sync=True"), 1)

    def test_center_selected_uses_uv_only_scope_without_changing_other_transforms(self):
        source = _source("transform_tools.py")
        tree = _tree("transform_tools.py")
        selected_islands = _function(tree, "_selected_islands")
        selected_source = ast.get_source_segment(source, selected_islands)
        self.assertIn("uv_only", selected_source)
        center = _function(tree, "execute")
        center_source = ast.get_source_segment(source, center)
        self.assertIn("_selected_islands(context, uv_only=True)", center_source)
        self.assertIn("get_selected_uv_islands(bm, uv_layer)", selected_source)

    def test_legacy_generic_helpers_remain_for_unrelated_callers(self):
        island_source = _source("island_tools.py")
        uv_source = _source("uv_utils.py")
        self.assertIn("def get_selected_uv_islands(bm, uv_layer):", island_source)
        self.assertIn("def select_islands(bm, uv_layer, islands):", uv_source)
        self.assertIn("def select_uv_islands(context, bm, uv_layer, islands):", uv_source)


if __name__ == "__main__":
    unittest.main()
