bl_info = {
    "name": "uv GPT",
    "author": "OpenAI Codex",
    "version": (1, 2, 6),
    "blender": (3, 6, 0),
    "location": "UV Editor > Sidebar > uv GPT",
    "description": "uv GPT: UV-only tools for optimizing already-unwrapped bake layouts.",
    "category": "UV",
}

ADDON_VERSION = ".".join(str(part) for part in bl_info["version"])

import importlib
import sys

try:
    import bpy
except ModuleNotFoundError:
    # Keep pure helper modules importable for unit tests and non-Blender
    # tooling.  Blender execution still follows the existing registration
    # path below unchanged.
    bpy = None


_MODULE_NAMES = (
    "pro_process_protocol",
    "pro_process_payload",
    "pro_process_shape",
    "pro_verified_nearest",
    "pro_similarity_seed",
    "pro_process_worker",
    "pro_process_runtime",
    "pro_process_pool",
    "pro_group_first",
    "pro_process_pipeline",
    "pro_process_adapter",
    "properties",
    "uv_utils",
    "island_tools",
    "overlay",
    "pack_tools",
    "texel_density",
    "tdensity_presets",
    "transform_tools",
    "symmetry_pair",
    "stack_tools",
    "quick_reinstall_tools",
    "ui",
)


def _load_modules():
    loaded = []
    for name in _MODULE_NAMES:
        full_name = f"{__name__}.{name}"
        if full_name in sys.modules:
            if name == "stack_tools":
                old_module = sys.modules[full_name]
                cleanup = getattr(old_module, "unregister", None)
                if callable(cleanup):
                    try:
                        cleanup()
                    except Exception:
                        # Reload must not leave an owned helper alive merely
                        # because an old Blender class is already stale.
                        pass
            module = importlib.reload(sys.modules[full_name])
        else:
            module = importlib.import_module(full_name)
        loaded.append(module)
    return loaded


_MODULES = _load_modules() if bpy is not None else ()
_REGISTERED = False
_REGISTERED_MODULES = ()


def _clear_stale_registration():
    if hasattr(bpy.types.Scene, "uv_gpt_settings"):
        try:
            del bpy.types.Scene.uv_gpt_settings
        except Exception:
            pass

    for module in reversed(_MODULES):
        for cls in reversed(getattr(module, "classes", ())):
            registered_cls = getattr(bpy.types, cls.__name__, None)
            if registered_cls is None:
                continue
            try:
                bpy.utils.unregister_class(registered_cls)
            except Exception:
                pass


def register():
    global _REGISTERED, _REGISTERED_MODULES
    if bpy is None:
        raise RuntimeError("uv_gpt.register() requires Blender")
    if _REGISTERED:
        return
    _clear_stale_registration()
    registered = []
    try:
        for module in _MODULES:
            register_fn = getattr(module, "register", None)
            if register_fn:
                register_fn()
                registered.append(module)
    except Exception:
        for module in reversed(registered):
            unregister_fn = getattr(module, "unregister", None)
            if unregister_fn:
                unregister_fn()
        _REGISTERED = False
        _REGISTERED_MODULES = ()
        raise
    _REGISTERED_MODULES = tuple(registered)
    _REGISTERED = True


def unregister():
    global _REGISTERED, _REGISTERED_MODULES
    # The Pro session owns its helper pool.  Close that pool before other
    # modules are unloaded or their Blender classes are unregistered.
    stack_module = next(
        (module for module in _MODULES if module.__name__.endswith(".stack_tools")),
        None,
    )
    active_session = getattr(stack_module, "_ACTIVE_PRO_SESSION", None)
    if not _REGISTERED and (active_session is None or active_session.done):
        return
    modules = _REGISTERED_MODULES or _MODULES
    if stack_module is not None:
        unregister_fn = getattr(stack_module, "unregister", None)
        if unregister_fn and (stack_module in modules or active_session is not None):
            unregister_fn()
    for module in reversed(modules):
        if module is stack_module:
            continue
        unregister_fn = getattr(module, "unregister", None)
        if unregister_fn:
            unregister_fn()
    _REGISTERED = False
    _REGISTERED_MODULES = ()


if __name__ == "__main__":
    register()
