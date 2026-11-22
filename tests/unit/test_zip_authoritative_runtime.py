"""Read-only static guards for the ZIP-authoritative runtime manifest."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
import unittest
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = PROJECT_ROOT / "uv_gpt"
ARCHIVE = PROJECT_ROOT / "uv_gpt_v1.2.7.zip"
EXPECTED_ARCHIVE_SIZE = 673341
EXPECTED_ARCHIVE_SHA256 = (
    "B6623CB41FEA888E59C3C2E0104DB794B25B476CBB96BD7DF4626FD9E6919211"
)
EXPECTED_RUNTIME_FILES = 39


def _sha256(data):
    return hashlib.sha256(data).hexdigest().upper()


def _source(name):
    return (RUNTIME_ROOT / name).read_text(encoding="utf-8")


def _node_source(name, node):
    return ast.get_source_segment(_source(name), node)


def _class_source(name, class_name):
    tree = ast.parse(_source(name), filename=name)
    node = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    return _node_source(name, node)


class ZipAuthoritativeRuntimeTests(unittest.TestCase):
    def test_runtime_manifest_and_bytes_match_archive_without_mutation(self):
        archive_bytes = ARCHIVE.read_bytes()
        self.assertEqual(ARCHIVE.stat().st_size, EXPECTED_ARCHIVE_SIZE)
        self.assertEqual(_sha256(archive_bytes), EXPECTED_ARCHIVE_SHA256)

        with zipfile.ZipFile(ARCHIVE) as archive:
            file_infos = sorted(
                (info for info in archive.infolist() if not info.is_dir()),
                key=lambda info: info.filename,
            )
            archive_names = [info.filename for info in file_infos]
            self.assertEqual(len(archive_names), EXPECTED_RUNTIME_FILES)
            self.assertTrue(all(name.startswith("uv_gpt/") for name in archive_names))
            self.assertTrue(
                all(name.endswith(".py") or name == "uv_gpt/uv_gpt_pack_native.dll" for name in archive_names)
            )

            repo_files = sorted(
                path for path in RUNTIME_ROOT.iterdir()
                if path.is_file() and (path.suffix == ".py" or path.name == "uv_gpt_pack_native.dll")
            )
            repo_names = [f"uv_gpt/{path.name}" for path in repo_files]
            self.assertEqual(repo_names, archive_names)
            self.assertEqual(len(list(RUNTIME_ROOT.rglob("*.pyc"))), 0)
            self.assertEqual(
                [
                    path for path in RUNTIME_ROOT.rglob("*")
                    if path.is_file() and path.suffix != ".py" and path.name != "uv_gpt_pack_native.dll"
                ],
                [],
            )

            for info in file_infos:
                relative = Path(*info.filename.split("/"))
                repo_bytes = (PROJECT_ROOT / relative).read_bytes()
                zip_bytes = archive.read(info.filename)
                self.assertEqual(repo_bytes, zip_bytes, info.filename)
                self.assertEqual(_sha256(repo_bytes), _sha256(zip_bytes), info.filename)

        self.assertEqual(_sha256(ARCHIVE.read_bytes()), EXPECTED_ARCHIVE_SHA256)

    def test_init_module_manifest_and_relative_imports_are_closed(self):
        files = {path.stem for path in RUNTIME_ROOT.glob("*.py")}
        init_tree = ast.parse(_source("__init__.py"), filename="__init__.py")
        module_names = None
        for node in init_tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if any(isinstance(target, ast.Name) and target.id == "_MODULE_NAMES" for target in node.targets):
                module_names = tuple(ast.literal_eval(node.value))
                break
        self.assertIsNotNone(module_names)
        self.assertTrue(set(module_names).issubset(files))
        self.assertIn("pack_geometry", module_names)

        package_exports = set()
        for node in init_tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                package_exports.add(node.name)
            elif isinstance(node, ast.Assign):
                package_exports.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )

        missing = []
        for path in sorted(RUNTIME_ROOT.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom) or node.level != 1:
                    continue
                if node.module:
                    target = node.module.split(".")[0]
                    if target not in files:
                        missing.append(f"{path.name}:{target}")
                else:
                    for alias in node.names:
                        if alias.name != "*" and alias.name not in files and alias.name not in package_exports:
                            missing.append(f"{path.name}:{alias.name}")
        self.assertEqual(missing, [])

    def test_user_facing_routes_are_v2_and_do_not_call_obsolete_primary_paths(self):
        ui = _source("ui.py")
        self.assertIn('row.operator("uv_gpt.align_similar_pro_fast", text="Fast")', ui)
        self.assertIn('row.operator("uv_gpt.align_similar_pro_snap", text="Pro")', ui)
        self.assertIn('row.operator("uv_gpt.pack_selected")', ui)
        self.assertIn('row.operator("uv_gpt.pack_whole_mesh")', ui)
        self.assertNotIn("uv_gpt.pack_symmetry", ui)
        self.assertNotIn("uv_gpt.center_selected", ui)
        self.assertNotIn('row.operator("uv_gpt.align_to_selected", text="Fast")', ui)

        fast = _class_source("stack_tools.py", "UVGPT_OT_align_similar_pro_fast")
        pro = _class_source("stack_tools.py", "UVGPT_OT_align_similar_pro_snap")
        self.assertIn("_fast_v2_start_job(context)", fast)
        self.assertIn("_pro_exact_v2_start_job(context)", pro)
        self.assertNotIn("_pro_create_session(", fast)
        self.assertNotIn("_pro_create_session(", pro)

        for class_name, mode in (
            ("UVGPT_OT_pack_selected", '"selected"'),
            ("UVGPT_OT_pack_symmetry", '"symmetry"'),
            ("UVGPT_OT_pack_whole_mesh", '"whole"'),
        ):
            operator = _class_source("pack_tools.py", class_name)
            self.assertIn("_pack_v2_start_job(context, ", operator)
            self.assertIn(mode, operator)
            self.assertNotIn("_pack(context", operator)

    def test_progress_active_uv_and_external_worker_contracts_are_present(self):
        stack = _source("stack_tools.py")
        pack = _source("pack_tools.py")
        overlay = _source("overlay.py")
        uv_utils = _source("uv_utils.py")
        progress_format = "{percent:.0f}%  •  {elapsed:.1f}s  •  {done}/{total}"

        self.assertIn("fast_v2_worker.py", stack)
        self.assertIn("pro_exact_v2_worker.py", stack)
        self.assertIn("_pro_exact_v2_apply_pending_updates", stack)
        self.assertIn("skipped", stack.lower())
        self.assertIn("done_hold_until", stack)
        self.assertIn("pack_v2_worker.py", pack)
        self.assertIn("pack_geometry", pack)
        self.assertIn("subprocess.Popen", stack)
        self.assertIn("subprocess.Popen", pack)
        self.assertIn(progress_format, stack)
        self.assertIn(progress_format, pack)

        self.assertIn("FAST_PROGRESS_LABELS", overlay)
        self.assertIn("def set_fast_progress", overlay)
        self.assertIn("def clear_fast_progress", overlay)
        self.assertIn("obj.data.uv_layers.active", uv_utils)
        self.assertIn("shape_method=\"CONCAVE\"", uv_utils)

        self.assertIn("uv_map_name", stack)
        self.assertIn("uv_map_name", pack)
        self.assertIn('getattr(active_uv, "name", None) != captured_name', stack)
        self.assertIn('getattr(active_uv, "name", None) == job.get("uv_map_name")', pack)

    def test_keep_stack_exact_numeric_threshold_remains_a_z3_risk(self):
        pack = _source("pack_v2_core.py")
        self.assertIn("_STACK_DIRECT_TOLERANCE = 2.0e-5", pack)
        # 1e-7 constants in pack_v2_core are margin/layout clearances, not a
        # proven Keep Stack Exact tolerance; the stack gate stays a Z3 risk.
        self.assertNotIn("_STACK_DIRECT_TOLERANCE = 1.0e-7", pack)


if __name__ == "__main__":
    unittest.main()
