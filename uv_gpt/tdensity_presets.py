import bpy

from . import texel_density, uv_utils


_COLOR_CYCLE = (
    (0.25, 0.65, 1.0, 1.0),
    (0.35, 0.9, 0.45, 1.0),
    (1.0, 0.68, 0.25, 1.0),
    (0.95, 0.35, 0.35, 1.0),
)

DEFAULT_PRESETS = (
    ("Low", 20.0, (0.35, 0.75, 1.0, 1.0)),
    ("Medium", 40.0, (0.45, 0.9, 0.45, 1.0)),
    ("High", 80.0, (1.0, 0.7, 0.25, 1.0)),
    ("Hero", 120.0, (1.0, 0.35, 0.35, 1.0)),
)

_TIMER_ATTEMPTS = 0


def active_preset(settings):
    if not settings.presets:
        return None
    index = min(max(settings.active_preset_index, 0), len(settings.presets) - 1)
    return settings.presets[index]


def ensure_default_presets(settings):
    if settings.default_presets_loaded:
        return 0
    existing_names = {preset.name for preset in settings.presets}
    added = 0
    for name, px_cm, color in DEFAULT_PRESETS:
        if name in existing_names:
            continue
        preset = settings.presets.add()
        preset.name = name
        preset.px_cm = px_cm
        preset.color = color
        added += 1
    settings.default_presets_loaded = True
    if settings.presets:
        settings.active_preset_index = min(settings.active_preset_index, len(settings.presets) - 1)
    return added


def initialize_default_presets_for_scenes():
    global _TIMER_ATTEMPTS
    try:
        scenes = getattr(bpy.data, "scenes", None)
        if scenes is None:
            raise RuntimeError("Scene data is not ready.")
        for scene in scenes:
            settings = getattr(scene, "uv_gpt_settings", None)
            if settings is not None:
                ensure_default_presets(settings)
    except Exception:
        _TIMER_ATTEMPTS += 1
        return 0.5 if _TIMER_ATTEMPTS < 20 else None
    return None


class UVGPT_OT_td_preset_add(bpy.types.Operator):
    bl_idname = "uv_gpt.td_preset_add"
    bl_label = "Add Preset"
    bl_description = "Add a texel density preset using the current target value"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = uv_utils.get_settings(context)
        preset = settings.presets.add()
        preset.name = f"Preset {len(settings.presets)}"
        preset.px_cm = texel_density.target_px_cm(context)
        preset.color = _COLOR_CYCLE[(len(settings.presets) - 1) % len(_COLOR_CYCLE)]
        settings.active_preset_index = len(settings.presets) - 1
        return {"FINISHED"}


class UVGPT_OT_td_preset_load_defaults(bpy.types.Operator):
    bl_idname = "uv_gpt.td_preset_load_defaults"
    bl_label = "Create Defaults"
    bl_description = "Create the Low, Medium, High, and Hero texel density presets"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = uv_utils.get_settings(context)
        settings.default_presets_loaded = False
        added = ensure_default_presets(settings)
        self.report({"INFO"}, f"Created {added} default preset(s).")
        return {"FINISHED"}


class UVGPT_OT_td_preset_remove(bpy.types.Operator):
    bl_idname = "uv_gpt.td_preset_remove"
    bl_label = "Remove Preset"
    bl_description = "Remove the active texel density preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = uv_utils.get_settings(context)
        if not settings.presets:
            self.report({"ERROR"}, "No texel density preset to remove.")
            return {"CANCELLED"}
        index = min(max(settings.active_preset_index, 0), len(settings.presets) - 1)
        settings.presets.remove(index)
        settings.active_preset_index = min(index, max(0, len(settings.presets) - 1))
        return {"FINISHED"}


class UVGPT_OT_td_preset_set_target(bpy.types.Operator):
    bl_idname = "uv_gpt.td_preset_set_target"
    bl_label = "Set Target From Preset"
    bl_description = "Set the target texel density from the active preset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = uv_utils.get_settings(context)
        ensure_default_presets(settings)
        preset = active_preset(settings)
        if not preset:
            self.report({"ERROR"}, "No active texel density preset.")
            return {"CANCELLED"}
        texel_density.set_target_from_px_cm(context, preset.px_cm)
        return {"FINISHED"}


class UVGPT_OT_td_preset_apply_selected(bpy.types.Operator):
    bl_idname = "uv_gpt.td_preset_apply_selected"
    bl_label = "Apply Preset To Selected"
    bl_description = "Scale selected UV islands to the active preset texel density"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = uv_utils.get_settings(context)
        ensure_default_presets(settings)
        preset = active_preset(settings)
        if not preset:
            self.report({"ERROR"}, "No active texel density preset.")
            return {"CANCELLED"}
        try:
            changed = texel_density.apply_px_cm_to_selection(context, preset.px_cm)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Applied preset to {changed} selected island(s).")
        return {"FINISHED"}


classes = (
    UVGPT_OT_td_preset_add,
    UVGPT_OT_td_preset_load_defaults,
    UVGPT_OT_td_preset_remove,
    UVGPT_OT_td_preset_set_target,
    UVGPT_OT_td_preset_apply_selected,
)


def register():
    global _TIMER_ATTEMPTS
    _TIMER_ATTEMPTS = 0
    for cls in classes:
        bpy.utils.register_class(cls)
    try:
        bpy.app.timers.register(initialize_default_presets_for_scenes, first_interval=0.1)
    except Exception:
        pass


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
