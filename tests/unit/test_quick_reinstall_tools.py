"""Focused tests for the uv GPT Quick Reinstall contract."""

from contextlib import redirect_stdout
import importlib.util
from io import StringIO
from pathlib import Path
import sys
import tempfile
import types
import unittest
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "uv_gpt" / "quick_reinstall_tools.py"
INIT_PATH = PROJECT_ROOT / "uv_gpt" / "__init__.py"
UI_PATH = PROJECT_ROOT / "uv_gpt" / "ui.py"


class FakeOperator:
    def __init__(self):
        self.reports = []

    def report(self, levels, message):
        self.reports.append((set(levels), message))


class FakePanel:
    pass


def load_module(fake_bpy):
    module_name = "uv_gpt_quick_reinstall_test_module"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    original_bpy = sys.modules.get("bpy")
    sys.modules["bpy"] = fake_bpy
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if original_bpy is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = original_bpy
    return module


def load_ui(fake_bpy, quick_module):
    package = types.ModuleType("uv_gpt")
    package.__path__ = [str(PROJECT_ROOT / "uv_gpt")]
    package.ADDON_VERSION = "1.2.6"
    package_modules = {
        "uv_gpt": package,
        "uv_gpt.quick_reinstall_tools": quick_module,
        "uv_gpt.tdensity_presets": types.SimpleNamespace(),
        "uv_gpt.texel_density": types.SimpleNamespace(),
    }
    original_modules = {
        name: sys.modules.get(name) for name in package_modules
    }
    sys.modules.update(package_modules)

    module_name = "uv_gpt.ui_test_module"
    spec = importlib.util.spec_from_file_location(module_name, UI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    original_bpy = sys.modules.get("bpy")
    sys.modules["bpy"] = fake_bpy
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if original_bpy is None:
            sys.modules.pop("bpy", None)
        else:
            sys.modules["bpy"] = original_bpy
        sys.modules.pop(module_name, None)
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


def make_bpy():
    bpy = types.SimpleNamespace()
    bpy.types = types.SimpleNamespace(
        Operator=FakeOperator,
        Panel=FakePanel,
        UIList=type("FakeUIList", (), {}),
    )
    bpy.context = types.SimpleNamespace(
        mode="OBJECT",
        preferences=types.SimpleNamespace(addons={}),
    )
    bpy.utils = types.SimpleNamespace(
        user_resource=lambda *_args, **_kwargs: "",
        refresh_script_paths=lambda: None,
        register_class=lambda _cls: None,
        unregister_class=lambda _cls: None,
    )
    bpy.ops = types.SimpleNamespace(
        preferences=types.SimpleNamespace(
            addon_disable=lambda **_kwargs: {"FINISHED"},
            addon_install=lambda **_kwargs: {"FINISHED"},
            addon_enable=lambda **_kwargs: {"FINISHED"},
        ),
        wm=types.SimpleNamespace(save_userpref=lambda: {"FINISHED"}),
    )
    bpy.app = types.SimpleNamespace(
        timers=types.SimpleNamespace(register=lambda *_args, **_kwargs: None),
    )
    return bpy


def make_context(
    *,
    area_type="IMAGE_EDITOR",
    ui_mode="UV",
    edit_object_type="MESH",
    edit_object_mode="EDIT",
):
    edit_object = None
    if edit_object_type is not None:
        edit_object = types.SimpleNamespace(
            type=edit_object_type,
            mode=edit_object_mode,
        )
    return types.SimpleNamespace(
        area=types.SimpleNamespace(type=area_type),
        space_data=types.SimpleNamespace(ui_mode=ui_mode),
        edit_object=edit_object,
        object=types.SimpleNamespace(type="MESH", mode="OBJECT"),
        mode="EDIT_MESH" if edit_object_mode == "EDIT" else "OBJECT",
    )


def write_zip(path, entries):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


class QuickReinstallTests(unittest.TestCase):
    def setUp(self):
        self.bpy = make_bpy()
        self.module = load_module(self.bpy)
        self.ui = load_ui(self.bpy, self.module)
        self.module._PENDING_REINSTALL = False
        for name in tuple(sys.modules):
            if name == "uv_gpt" or name.startswith("uv_gpt."):
                sys.modules.pop(name, None)

    def tearDown(self):
        self.module._PENDING_REINSTALL = False
        for name in tuple(sys.modules):
            if name == "uv_gpt" or name.startswith("uv_gpt."):
                sys.modules.pop(name, None)
        sys.modules.pop("uv_gpt_quick_reinstall_test_module", None)

    def test_shared_panel_polls_allow_fallback_edit_context(self):
        operator = self.module.UVGPT_OT_quick_reinstall_from_canonical_zip
        main_panel = self.ui.UVGPT_PT_image_editor
        quick_panel = self.ui.UVGPT_PT_quick_reinstall

        for ui_mode in ("VIEW", None):
            with self.subTest(ui_mode=ui_mode):
                context = make_context(ui_mode=ui_mode)
                self.assertTrue(main_panel.poll(context))
                self.assertTrue(quick_panel.poll(context))
                self.assertTrue(operator.poll(context))

    def test_panel_and_operator_polls_reject_unsafe_fallback_contexts(self):
        operator = self.module.UVGPT_OT_quick_reinstall_from_canonical_zip
        main_panel = self.ui.UVGPT_PT_image_editor
        quick_panel = self.ui.UVGPT_PT_quick_reinstall
        invalid_contexts = {
            "object_mode": make_context(ui_mode="VIEW", edit_object_type=None),
            "wrong_area": make_context(ui_mode="VIEW", area_type="VIEW_3D"),
            "non_mesh_edit": make_context(ui_mode=None, edit_object_type="CURVE"),
        }

        for name, context in invalid_contexts.items():
            with self.subTest(name=name):
                self.assertFalse(main_panel.poll(context))
                self.assertFalse(quick_panel.poll(context))
                self.assertFalse(operator.poll(context))

    def test_uv_mode_panel_contract_is_preserved_but_operator_stays_safe(self):
        operator = self.module.UVGPT_OT_quick_reinstall_from_canonical_zip
        main_panel = self.ui.UVGPT_PT_image_editor
        quick_panel = self.ui.UVGPT_PT_quick_reinstall
        context = make_context(ui_mode="UV", edit_object_type=None)

        self.assertTrue(main_panel.poll(context))
        self.assertTrue(quick_panel.poll(context))
        self.assertFalse(operator.poll(context))

    def test_zip_preflight_accepts_valid_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "valid.zip"
            write_zip(path, {"uv_gpt/__init__.py": "bl_info = {}"})
            self.assertIsNone(self.module._validate_canonical_zip(path))

    def test_zip_preflight_rejects_missing_bad_and_missing_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "was not found"):
                self.module._validate_canonical_zip(root / "missing.zip")

            bad = root / "bad.zip"
            bad.write_bytes(b"this is not a zip archive")
            with self.assertRaisesRegex(RuntimeError, "not readable"):
                self.module._validate_canonical_zip(bad)

            missing_entry = root / "missing-entry.zip"
            write_zip(missing_entry, {"other/__init__.py": ""})
            with self.assertRaisesRegex(RuntimeError, "uv_gpt/__init__.py"):
                self.module._validate_canonical_zip(missing_entry)

            wrong_suffix = root / "package.bin"
            wrong_suffix.write_bytes(b"not a zip")
            with self.assertRaisesRegex(RuntimeError, "requires a ZIP"):
                self.module._validate_canonical_zip(wrong_suffix)

    def test_installed_path_guard_accepts_only_blender_user_addons(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_addons = root / "scripts" / "addons"
            installed_package = user_addons / "uv_gpt"
            checkout_package = root / "checkout" / "uv_gpt"
            self.bpy.utils.user_resource = (
                lambda *_args, **_kwargs: str(user_addons)
            )
            package = types.SimpleNamespace(
                __file__=str(installed_package / "__init__.py")
            )
            sys.modules["uv_gpt"] = package
            self.assertTrue(
                self.module._installed_package_is_in_blender_user_addons()
            )

            package.__file__ = str(checkout_package / "__init__.py")
            self.assertFalse(
                self.module._installed_package_is_in_blender_user_addons()
            )

            package.__file__ = str(installed_package / "__init__.py")
            self.bpy.utils.user_resource = (
                lambda *_args, **_kwargs: str(root / "custom-scripts" / "addons")
            )
            self.assertFalse(
                self.module._installed_package_is_in_blender_user_addons()
            )

    def test_operator_schedules_timer_and_blocks_pending_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zip_path = root / "valid.zip"
            write_zip(zip_path, {"uv_gpt/__init__.py": ""})
            user_addons = root / "scripts" / "addons"
            self.module.CANONICAL_ZIP_PATH = str(zip_path)
            self.bpy.utils.user_resource = (
                lambda *_args, **_kwargs: str(user_addons)
            )
            sys.modules["uv_gpt"] = types.SimpleNamespace(
                __file__=str(user_addons / "uv_gpt" / "__init__.py")
            )
            timers = []
            self.bpy.app.timers.register = lambda callback, **kwargs: timers.append(
                (callback, kwargs)
            )

            first = self.module.UVGPT_OT_quick_reinstall_from_canonical_zip()
            self.assertEqual(first.execute(types.SimpleNamespace()), {"FINISHED"})
            self.assertTrue(self.module._PENDING_REINSTALL)
            self.assertEqual(len(timers), 1)
            self.assertEqual(timers[0][1], {"first_interval": 0.25})

            second = self.module.UVGPT_OT_quick_reinstall_from_canonical_zip()
            self.assertEqual(second.execute(types.SimpleNamespace()), {"CANCELLED"})
            self.assertEqual(len(timers), 1)
            self.assertIn("already scheduled", second.reports[0][1])

    def test_schedule_failure_clears_pending_and_reports_cancelled(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            zip_path = root / "valid.zip"
            write_zip(zip_path, {"uv_gpt/__init__.py": ""})
            user_addons = root / "scripts" / "addons"
            self.module.CANONICAL_ZIP_PATH = str(zip_path)
            self.bpy.utils.user_resource = (
                lambda *_args, **_kwargs: str(user_addons)
            )
            sys.modules["uv_gpt"] = types.SimpleNamespace(
                __file__=str(user_addons / "uv_gpt" / "__init__.py")
            )

            def fail_register(*_args, **_kwargs):
                raise RuntimeError("timer unavailable")

            self.bpy.app.timers.register = fail_register
            operator = self.module.UVGPT_OT_quick_reinstall_from_canonical_zip()
            self.assertEqual(operator.execute(types.SimpleNamespace()), {"CANCELLED"})
            self.assertFalse(self.module._PENDING_REINSTALL)
            self.assertIn("Could not schedule", operator.reports[0][1])

    def test_timer_sequence_clears_modules_and_never_removes_addon(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            zip_path = Path(directory) / "valid.zip"
            write_zip(zip_path, {"uv_gpt/__init__.py": ""})
            sys.modules["uv_gpt"] = types.SimpleNamespace(__file__="user/uv_gpt/__init__.py")
            sys.modules["uv_gpt.ui"] = types.SimpleNamespace()
            sys.modules["uv_gpt.ui.nested"] = types.SimpleNamespace()
            self.bpy.context.preferences.addons = {"uv_gpt": object()}

            self.bpy.ops.preferences.addon_disable = lambda **_kwargs: (
                events.append("disable") or {"FINISHED"}
            )

            def install(**kwargs):
                events.append("install")
                self.assertEqual(kwargs, {"filepath": str(zip_path), "overwrite": True})
                sys.modules["uv_gpt.stale_after_install"] = types.SimpleNamespace()
                return {"FINISHED"}

            self.bpy.ops.preferences.addon_install = install
            self.bpy.ops.preferences.addon_enable = lambda **_kwargs: (
                events.append("enable") or {"FINISHED"}
            )
            self.bpy.ops.wm.save_userpref = lambda: (
                events.append("save") or {"FINISHED"}
            )
            self.bpy.utils.refresh_script_paths = lambda: events.append("refresh")

            original_forget = self.module._forget_loaded_addon_modules

            def forget():
                events.append("clear")
                original_forget()

            self.module._forget_loaded_addon_modules = forget
            original_invalidate = self.module.importlib.invalidate_caches

            def invalidate():
                events.append("invalidate")
                original_invalidate()

            self.module.importlib.invalidate_caches = invalidate
            try:
                self.assertIsNone(self.module._run_quick_reinstall(str(zip_path)))
            finally:
                self.module.importlib.invalidate_caches = original_invalidate

            self.assertEqual(
                events,
                [
                    "disable",
                    "clear",
                    "invalidate",
                    "install",
                    "refresh",
                    "invalidate",
                    "clear",
                    "enable",
                    "save",
                ],
            )
            self.assertFalse(self.module._PENDING_REINSTALL)
            self.assertNotIn("uv_gpt", sys.modules)
            self.assertNotIn("uv_gpt.stale_after_install", sys.modules)
            source = MODULE_PATH.read_text(encoding="utf-8")
            self.assertNotIn("addon_remove", source)

    def test_failed_preferences_step_does_not_print_success(self):
        events = []
        self.bpy.context.preferences.addons = {"uv_gpt": object()}
        self.bpy.ops.preferences.addon_disable = lambda **_kwargs: (
            events.append("disable") or {"FINISHED"}
        )
        self.bpy.ops.preferences.addon_install = lambda **_kwargs: (
            events.append("install") or {"CANCELLED"}
        )
        output = StringIO()
        with redirect_stdout(output):
            self.module._run_quick_reinstall("missing-after-schedule.zip")
        self.assertIn("Quick Reinstall failed", output.getvalue())
        self.assertNotIn("Quick Reinstall finished", output.getvalue())
        self.assertEqual(events, ["disable", "install"])
        self.assertFalse(self.module._PENDING_REINSTALL)

    def test_source_compiles_and_registration_wiring_is_present(self):
        for path in (PROJECT_ROOT / "uv_gpt").glob("*.py"):
            compile(path.read_text(encoding="utf-8"), str(path), "exec")

        init_source = INIT_PATH.read_text(encoding="utf-8")
        ui_source = UI_PATH.read_text(encoding="utf-8")
        quick_source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('"quick_reinstall_tools"', init_source)
        self.assertIn("UVGPT_PT_quick_reinstall", ui_source)
        self.assertIn('bl_space_type = "IMAGE_EDITOR"', ui_source)
        self.assertIn('bl_region_type = "UI"', ui_source)
        self.assertIn('bl_category = "uv GPT"', ui_source)
        self.assertIn('bl_options = {"DEFAULT_CLOSED"}', ui_source)
        self.assertIn(
            "def _is_uv_gpt_image_editor_context(context):",
            ui_source,
        )
        self.assertEqual(
            ui_source.count("return _is_uv_gpt_image_editor_context(context)"),
            2,
        )
        self.assertIn('Target: {quick_reinstall_tools.ADDON_MODULE}', ui_source)
        self.assertIn('Location: Blender User Add-ons', ui_source)
        self.assertIn('"uv_gpt.quick_reinstall_from_canonical_zip"', ui_source)
        self.assertIn('"uv_gpt.quick_reinstall_from_canonical_zip"', quick_source)
        self.assertIn("UVGPT_PT_quick_reinstall", ui_source)
        self.assertIn(
            "return _is_uv_editor_edit_context(context)",
            quick_source,
        )
        self.assertNotIn('getattr(space, "ui_mode", None)', quick_source)
        self.assertNotIn('context.mode == "OBJECT"', quick_source)
        self.assertNotIn('getattr(context, "mode", None) == "OBJECT"', ui_source)
        self.assertNotIn("addon_remove", quick_source)


if __name__ == "__main__":
    unittest.main()
