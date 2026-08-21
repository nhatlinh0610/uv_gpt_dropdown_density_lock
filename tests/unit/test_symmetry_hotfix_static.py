"""Static contract checks for the S1 symmetry hotfix."""

from __future__ import annotations

import ast
import pathlib
import unittest


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
SYMMETRY_SOURCE = PROJECT_ROOT / "uv_gpt" / "symmetry_pair.py"
ISLAND_SOURCE = PROJECT_ROOT / "uv_gpt" / "island_tools.py"
UI_SOURCE = PROJECT_ROOT / "uv_gpt" / "ui.py"


class SymmetryHotfixStaticTests(unittest.TestCase):
    def test_reference_workflow_is_not_in_production_symmetry_registration(self):
        source = SYMMETRY_SOURCE.read_text(encoding="utf-8")
        ui = UI_SOURCE.read_text(encoding="utf-8")

        for stale_id in (
            "symmetry_set_reference",
            "symmetry_snap_selected",
            "symmetry_clear_reference",
        ):
            self.assertNotIn(stale_id, source)
            self.assertNotIn(stale_id, ui)
        self.assertNotIn("REFERENCE_CACHE", source)

    def test_symmetry_registration_contains_only_pair_operator(self):
        tree = ast.parse(SYMMETRY_SOURCE.read_text(encoding="utf-8"))
        classes_assignment = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "classes" for target in node.targets)
        )
        self.assertIsInstance(classes_assignment.value, ast.Tuple)
        registered_names = [
            item.id
            for item in classes_assignment.value.elts
            if isinstance(item, ast.Name)
        ]
        self.assertEqual(registered_names, ["UVGPT_OT_symmetry_auto_mirror"])

    def test_axis_route_is_limited_to_u_or_v_and_ui_hides_legacy_custom_values(self):
        source = SYMMETRY_SOURCE.read_text(encoding="utf-8")
        ui = UI_SOURCE.read_text(encoding="utf-8")
        symmetry_start = ui.index('"ui_show_symmetry"')
        overlay_start = ui.index('"ui_show_overlay"')
        symmetry_ui = ui[symmetry_start:overlay_start]

        self.assertNotIn('"CUSTOM_U"', source)
        self.assertNotIn('"CUSTOM_V"', source)
        self.assertIn('prop_enum(settings, "symmetry_axis", "U_HALF"', symmetry_ui)
        self.assertIn('prop_enum(settings, "symmetry_axis", "V_HALF"', symmetry_ui)
        self.assertIn('Mirror Target Position', symmetry_ui)
        self.assertNotIn('prop(settings, "custom_axis_value")', symmetry_ui)
        self.assertNotIn('prop(settings, "keep_inside_tile")', symmetry_ui)
        self.assertNotIn("match_rotation", symmetry_ui)
        self.assertNotIn("match_scale", symmetry_ui)

    def test_operator_keeps_blender_undo(self):
        source = SYMMETRY_SOURCE.read_text(encoding="utf-8")
        self.assertIn('bl_options = {"REGISTER", "UNDO"}', source)

    def test_symmetry_uses_context_regions_not_legacy_island_predicate(self):
        source = SYMMETRY_SOURCE.read_text(encoding="utf-8")

        self.assertIn("get_selected_uv_regions_for_context", source)
        self.assertNotIn("get_selected_uv_islands(bm, uv_layer)", source)
        self.assertNotIn("get_active_uv_island", source)
        self.assertIn("exactly two connected UV regions", source)

    def test_region_helper_is_topology_connected_and_never_spatially_merged(self):
        source = ISLAND_SOURCE.read_text(encoding="utf-8")

        self.assertIn("get_selected_uv_faces_for_symmetry", source)
        self.assertIn("get_selected_uv_regions_for_context", source)
        self.assertIn("edge.link_faces", source)
        self.assertIn("never merged by spatial", source)
        self.assertIn("uv_select_sync_valid", source)

    def test_active_history_target_precedes_stale_bmesh_active_fallback(self):
        island_source = ISLAND_SOURCE.read_text(encoding="utf-8")
        symmetry_source = SYMMETRY_SOURCE.read_text(encoding="utf-8")

        self.assertIn("def resolve_selected_region_target", island_source)
        self.assertIn("_selection_history_active_element(bm)", island_source)
        self.assertIn("if len(history_indices) == 1", island_source)
        self.assertIn("if len(active_indices) == 1", island_source)
        self.assertIn("resolve_selected_region_target", symmetry_source)
        self.assertNotIn("_active_face_candidates", symmetry_source)
        self.assertNotIn("different selected regions", symmetry_source)

    def test_invalid_target_cancels_without_writing(self):
        source = SYMMETRY_SOURCE.read_text(encoding="utf-8")
        self.assertIn("target_index is None", source)
        self.assertIn("without writing UVs", source)

    def test_symmetry_transforms_all_target_region_loops(self):
        source = SYMMETRY_SOURCE.read_text(encoding="utf-8")

        self.assertIn("def _region_loops", source)
        self.assertIn("def _bounds_center", source)
        self.assertIn("get_island_bounds", source)
        self.assertIn("target_loops = _region_loops(target_region)", source)
        self.assertIn("desired_target_center", source)
        self.assertIn("delta = desired_target_center - target_center", source)
        self.assertIn("uv_utils.translate_island(target_loops", source)
        for forbidden in (
            "get_island_main_axis_farthest_points",
            "rotate_island",
            "scale_island",
            "match_rotation",
            "match_scale",
            "Keep Parallel",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
