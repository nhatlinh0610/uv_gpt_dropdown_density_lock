"""Safe one-click reinstall of the canonical uv GPT add-on ZIP."""

from pathlib import Path
import importlib
import os
import sys
import zipfile

import bpy


ADDON_MODULE = "uv_gpt"
DEFAULT_CANONICAL_ZIP_PATH = Path(
    r"E:\OneDrive\all add on blender\codex\uv_gpt_dropdown_density_lock\uv_gpt_v1.2.6.zip"
)
CANONICAL_ZIP_PATH = os.environ.get(
    "UV_GPT_ADDON_ZIP",
    str(DEFAULT_CANONICAL_ZIP_PATH),
)
_PENDING_REINSTALL = False


def _canonical_zip_file():
    return Path(CANONICAL_ZIP_PATH)


def _is_uv_editor_edit_context(context):
    area = getattr(context, "area", None)
    if getattr(area, "type", None) != "IMAGE_EDITOR":
        return False

    edit_object = getattr(context, "edit_object", None)
    return bool(
        edit_object
        and getattr(edit_object, "type", None) == "MESH"
        and getattr(edit_object, "mode", None) == "EDIT"
    )


def _installed_package_is_in_blender_user_addons():
    package = sys.modules.get(ADDON_MODULE)
    package_file = getattr(package, "__file__", None)
    try:
        user_addons = bpy.utils.user_resource(
            "SCRIPTS",
            path="addons",
            create=False,
        )
    except (OSError, TypeError, AttributeError):
        return False

    if not package_file or not user_addons:
        return False

    try:
        package_dir = Path(package_file).resolve().parent
        expected_dir = (Path(user_addons).resolve() / ADDON_MODULE).resolve()
    except (OSError, TypeError, ValueError):
        return False
    return package_dir == expected_dir


def _validate_canonical_zip(zip_path):
    if not zip_path.is_file():
        raise RuntimeError("Canonical ZIP was not found: %s" % zip_path)
    if zip_path.suffix.lower() != ".zip":
        raise RuntimeError("Quick Reinstall requires a ZIP package: %s" % zip_path)

    required_entry = ADDON_MODULE + "/__init__.py"
    try:
        with zipfile.ZipFile(str(zip_path), "r") as archive:
            if required_entry not in archive.namelist():
                raise RuntimeError(
                    "Canonical ZIP does not contain %s." % required_entry
                )
            archive.read(required_entry)
    except RuntimeError:
        raise
    except (OSError, EOFError, zipfile.BadZipFile) as error:
        raise RuntimeError("Canonical ZIP is not readable: %s" % error) from error


def _require_finished(result, action):
    if set(result) != {"FINISHED"}:
        raise RuntimeError("%s did not finish: %r" % (action, set(result)))


def _disable_if_currently_enabled():
    if bpy.context.preferences.addons.get(ADDON_MODULE) is None:
        return
    _require_finished(
        bpy.ops.preferences.addon_disable(module=ADDON_MODULE),
        "Disable installed add-on",
    )


def _forget_loaded_addon_modules():
    """Drop uv GPT modules so enable imports every file from the new ZIP."""
    prefix = ADDON_MODULE + "."
    loaded_names = [
        name
        for name in tuple(sys.modules)
        if name == ADDON_MODULE or name.startswith(prefix)
    ]
    loaded_names.sort(
        key=lambda name: (name.count("."), len(name)),
        reverse=True,
    )
    for name in loaded_names:
        sys.modules.pop(name, None)


def _run_quick_reinstall(zip_path_text):
    global _PENDING_REINSTALL
    zip_path = Path(zip_path_text)
    try:
        _disable_if_currently_enabled()
        _forget_loaded_addon_modules()
        importlib.invalidate_caches()
        _require_finished(
            bpy.ops.preferences.addon_install(
                filepath=str(zip_path),
                overwrite=True,
            ),
            "Replace installed add-on from canonical ZIP",
        )
        bpy.utils.refresh_script_paths()
        importlib.invalidate_caches()
        _forget_loaded_addon_modules()
        _require_finished(
            bpy.ops.preferences.addon_enable(module=ADDON_MODULE),
            "Enable reinstalled add-on",
        )
        _require_finished(
            bpy.ops.wm.save_userpref(),
            "Save Blender preferences",
        )
        print("[uv GPT] Quick Reinstall finished from %s" % zip_path)
    except Exception as error:
        print("[uv GPT] Quick Reinstall failed: %s" % error)
    finally:
        _PENDING_REINSTALL = False
    return None


class UVGPT_OT_quick_reinstall_from_canonical_zip(bpy.types.Operator):
    bl_idname = "uv_gpt.quick_reinstall_from_canonical_zip"
    bl_label = "Quick Reinstall Add-on"
    bl_description = (
        "Replace the installed uv GPT add-on from the canonical ZIP, "
        "enable it, and save Blender preferences"
    )
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        return _is_uv_editor_edit_context(context)

    def execute(self, _context):
        global _PENDING_REINSTALL
        if _PENDING_REINSTALL:
            self.report({"WARNING"}, "Quick Reinstall is already scheduled.")
            return {"CANCELLED"}

        zip_path = _canonical_zip_file()
        try:
            _validate_canonical_zip(zip_path)
        except RuntimeError as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}

        if not _installed_package_is_in_blender_user_addons():
            self.report(
                {"ERROR"},
                "Install this ZIP once through Blender Preferences before using Quick Reinstall.",
            )
            return {"CANCELLED"}

        _PENDING_REINSTALL = True
        try:
            bpy.app.timers.register(
                lambda: _run_quick_reinstall(str(zip_path)),
                first_interval=0.25,
            )
        except Exception as error:
            _PENDING_REINSTALL = False
            self.report({"ERROR"}, "Could not schedule Quick Reinstall: %s" % error)
            return {"CANCELLED"}

        self.report(
            {"INFO"},
            "Quick Reinstall scheduled: replace, enable, and save preferences.",
        )
        return {"FINISHED"}


classes = (UVGPT_OT_quick_reinstall_from_canonical_zip,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
