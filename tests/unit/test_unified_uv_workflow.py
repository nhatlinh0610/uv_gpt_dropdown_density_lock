"""Static contracts for the ZIP-authoritative V2 workflow."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
UV = ROOT / "uv_gpt"


def source(name):
    return (UV / name).read_text(encoding="utf-8")


def function_source(name, function_name):
    text = source(name)
    tree = ast.parse(text, filename=name)
    node = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    )
    return ast.get_source_segment(text, node)


def class_source(name, class_name):
    text = source(name)
    tree = ast.parse(text, filename=name)
    node = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return ast.get_source_segment(text, node)


class UnifiedWorkflowTests(unittest.TestCase):
    def test_stack_ui_exposes_only_fast_and_pro(self):
        ui = source("ui.py")
        stack = ui[ui.index('"ui_show_stack"'):ui.index('"ui_show_symmetry"')]
        self.assertIn('row.operator("uv_gpt.align_similar_pro_fast", text="Fast")', stack)
        self.assertIn('row.operator("uv_gpt.align_similar_pro_snap", text="Pro")', stack)
        self.assertNotIn('row.operator("uv_gpt.align_to_selected", text="Fast")', stack)
        self.assertNotIn("Pro Fast", stack)
        self.assertNotIn("Pro Exact", stack)
        self.assertNotIn("Align Similar", stack)

    def test_pro_route_is_external_exact_v2(self):
        stack = source("stack_tools.py")
        pro = class_source("stack_tools.py", "UVGPT_OT_align_similar_pro_snap")
        self.assertIn('bl_idname = "uv_gpt.align_similar_pro_snap"', stack)
        self.assertIn("_pro_exact_v2_start_job(context)", pro)
        self.assertNotIn("_pro_create_session(", pro)
        self.assertIn("pro_exact_v2_worker.py", stack)
        self.assertIn("_pro_exact_v2_apply_pending_updates", stack)
        self.assertIn("topology_correspondence", source("pro_exact_v2_core.py"))
        self.assertIn("done_hold_until", stack)

    def test_symmetry_has_single_mirror_and_pair_similarity_transform(self):
        symmetry = source("symmetry_pair.py")
        ui = source("ui.py")
        self.assertIn("_center_and_align_single_region", symmetry)
        self.assertIn("_mirror_single_region", symmetry)
        single = function_source("symmetry_pair.py", "_mirror_single_region")
        self.assertIn("_mirror_point", single)
        self.assertIn("_region_loops(region)", single)
        self.assertNotIn("_apply_similarity_transform", single)
        pair = function_source("symmetry_pair.py", "_apply_from_reference")
        self.assertIn("rms_radius", pair)
        self.assertIn("_mirror_direction", pair)
        self.assertIn("_apply_similarity_transform", pair)
        execute = function_source("symmetry_pair.py", "execute")
        self.assertIn("selection_count == 1", execute)
        self.assertIn("selection_count == 2", execute)
        self.assertIn("_mirror_single_region", execute)
        self.assertNotIn("_center_and_align_single_region(\n                    regions[0]", execute)
        self.assertIn('self.report({"INFO"}', execute)
        symmetry_ui = ui[ui.index('"ui_show_symmetry"'):ui.index('"ui_show_overlay"')]
        self.assertIn("1 island: Mirror across U=0.5 or V=0.5 (no scale)", symmetry_ui)
        self.assertIn("Mirror Symmetry", symmetry_ui)

    def test_pack_selected_preserves_unselected_and_uses_static_blockers(self):
        props = source("properties.py")
        ui = source("ui.py")
        pack_tools = source("pack_tools.py")
        capture = function_source("pack_tools.py", "_pack_v2_capture_snapshot")
        operator = class_source("pack_tools.py", "UVGPT_OT_pack_selected")
        self.assertIn("pack_selected_keep_current_scale_v2", props)
        self.assertIn("default=False", props[props.index("pack_selected_keep_current_scale_v2"):])
        legacy_start = props.index("pack_selected_unselected_mode")
        legacy_end = props.index("pack_preserve_stacks", legacy_start)
        self.assertIn('options={"HIDDEN"}', props[legacy_start:legacy_end])
        self.assertNotIn("pack_selected_unselected_mode", ui)
        self.assertIn("pack_selected_keep_current_scale_v2", ui)
        self.assertIn('_pack_v2_start_job(context, "selected")', operator)
        self.assertIn("world_area_by_face", capture)
        self.assertIn('"scale_to_fit"', capture)
        self.assertIn("static_islands", pack_tools)
        self.assertIn("shape_method=\"CONCAVE\"", pack_tools)
        self.assertIn("unselected", pack_tools.lower())
        self.assertIn("pack_v2_worker.py", pack_tools)

    def test_pack_symmetry_is_selected_only_density_unified_and_fill_scaled(self):
        pack_tools = source("pack_tools.py")
        core = source("pack_v2_core.py")
        ui = source("ui.py")
        self.assertIn("class UVGPT_OT_pack_symmetry", pack_tools)
        self.assertIn('bl_idname = "uv_gpt.pack_symmetry"', pack_tools)
        operator = class_source("pack_tools.py", "UVGPT_OT_pack_symmetry")
        self.assertIn('_pack_v2_start_job(context, "symmetry")', operator)
        self.assertIn("pair_similar_records", core)
        self.assertIn("static_shapes", core)
        self.assertIn("shape_sets_conflict", core)
        self.assertIn("symmetry", core)
        self.assertIn('row.operator("uv_gpt.pack_symmetry", text="Pack Symmetry")', ui)
        self.assertIn('prop_enum(settings, "symmetry_axis", "U_HALF"', ui)
        self.assertIn('prop_enum(settings, "symmetry_axis", "V_HALF"', ui)

    def test_user_facing_pack_errors_are_info_not_red_error(self):
        pack = source("pack_tools.py")
        symmetry = source("symmetry_pair.py")
        self.assertIn('self.report({"INFO"}', symmetry)
        pack_sym = function_source("pack_tools.py", "_pack_symmetry")
        self.assertNotIn("{'ERROR'}", pack_sym)


if __name__ == "__main__":
    unittest.main()
