import bpy

from . import quick_reinstall_tools, tdensity_presets, texel_density
from . import ADDON_VERSION


def _settings(context):
    return context.scene.uv_gpt_settings


def _active_preset(settings):
    return tdensity_presets.active_preset(settings)


def _section(layout, settings, prop_name, title):
    box = layout.box()
    is_open = getattr(settings, prop_name)
    icon = "TRIA_DOWN" if is_open else "TRIA_RIGHT"
    row = box.row(align=True)
    row.prop(settings, prop_name, text=title, icon=icon, emboss=False)
    return box if is_open else None


class UVGPT_UL_tdensity_presets(bpy.types.UIList):
    bl_idname = "UVGPT_UL_tdensity_presets"

    def draw_item(
        self,
        context,
        layout,
        _data,
        item,
        _icon,
        _active_data,
        _active_propname,
        _index,
    ):
        settings = context.scene.uv_gpt_settings
        row = layout.row(align=True)
        row.prop(item, "name", text="", emboss=False)
        if settings.input_unit == "PX_UNIT":
            value = texel_density.px_cm_to_px_unit(context, item.px_cm)
            row.label(text=f"{value:.1f} px/unit")
        else:
            row.label(text=f"{item.px_cm:.2f} px/cm")


def draw_uv_gpt_panel(layout, context):
    settings = _settings(context)

    layout.label(text=f"uv GPT v{ADDON_VERSION}")
    obj = getattr(context, "edit_object", None) or getattr(context, "object", None)
    if not obj or obj.type != "MESH":
        layout.label(text="Select a mesh object")
        return
    if obj.mode != "EDIT":
        layout.label(text="Enter Edit Mode to edit UVs")
        return

    box = _section(layout, settings, "ui_show_pack", "Pack")
    if box:
        box.prop(settings, "margin")
        box.prop(settings, "rotation_mode")
        box.prop(settings, "pack_selected_lock_density")
        box.prop(settings, "pack_selected_unselected_mode")
        box.prop(settings, "pack_preserve_stacks")
        row = box.row(align=True)
        row.operator("uv_gpt.pack_selected")
        row.operator("uv_gpt.pack_whole_mesh")
        box.operator("uv_gpt.center_selected")

    box = _section(layout, settings, "ui_show_texel_density", "Density")
    if box:
        row = box.row(align=True)
        row.prop(settings, "texture_size_mode")
        if settings.texture_size_mode == "CUSTOM":
            row.prop(settings, "custom_texture_size", text="")
        box.prop(settings, "display_unit")
        box.prop(settings, "input_unit")
        box.prop(settings, "target_value")
        row = box.row(align=True)
        row.operator("uv_gpt.square_selected_face_td", text="Square Face")
        row.operator("uv_gpt.square_face_apply_td_whole_mesh", text="Grid Mesh")
        row = box.row(align=True)
        row.operator("uv_gpt.show_selected_td", text="Show Selected")
        row.operator("uv_gpt.show_all_td", text="Show All")
        box.operator("uv_gpt.set_target_from_selected", text="Target From Selected")
        row = box.row(align=True)
        row.operator("uv_gpt.apply_td_selected", text="Apply Selected")
        row.operator("uv_gpt.apply_td_whole_mesh", text="Apply Whole Mesh")

    box = _section(layout, settings, "ui_show_stack", "Stack")
    if box:
        box.prop(settings, "stack_match_scale")
        box.prop(settings, "stack_allow_flipping")
        box.prop(settings, "stack_similarity_tolerance")
        row = box.row(align=True)
        row.operator("uv_gpt.paste_keep_position")
        row.operator("uv_gpt.align_to_selected", text="Align Similar")
        row = box.row(align=True)
        row.operator("uv_gpt.align_similar_pro_fast", text="Pro Fast")
        row.operator("uv_gpt.align_similar_pro_exact", text="Pro Exact")

    box = _section(layout, settings, "ui_show_symmetry", "Symmetry")
    if box:
        box.label(text="Select anchor first, then Shift-select target")
        row = box.row(align=True)
        row.label(text="Axis")
        row.prop_enum(settings, "symmetry_axis", "U_HALF", text="U")
        row.prop_enum(settings, "symmetry_axis", "V_HALF", text="V")
        if settings.symmetry_axis not in {"U_HALF", "V_HALF"}:
            box.label(text="Choose U or V before running symmetry", icon="ERROR")
        box.operator("uv_gpt.symmetry_auto_mirror", text="Mirror Target Position")

    box = _section(layout, settings, "ui_show_overlay", "Overlay")
    if box:
        box.prop(settings, "show_island_numbers")
        box.prop(settings, "show_area_percent")
        box.prop(settings, "show_texel_density")
        box.operator("uv_gpt.refresh_overlay")

    box = _section(layout, settings, "ui_show_uv_map", "UV Map")
    if box:
        box.prop(settings, "active_uv_map")
        box.operator("uv_gpt.duplicate_to_bake_optimized")
        box.prop(settings, "duplicate_before_operations")


def _is_uv_gpt_image_editor_context(context):
    area = getattr(context, "area", None)
    if getattr(area, "type", None) != "IMAGE_EDITOR":
        return False

    space = getattr(context, "space_data", None)
    if getattr(space, "ui_mode", None) == "UV":
        return True

    obj = getattr(context, "edit_object", None) or getattr(context, "object", None)
    return bool(
        obj
        and getattr(obj, "type", None) == "MESH"
        and getattr(obj, "mode", None) == "EDIT"
    )


class UVGPT_PT_view3d(bpy.types.Panel):
    bl_label = "uv GPT"
    bl_idname = "UVGPT_PT_view3d"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "uv GPT"

    @classmethod
    def poll(cls, _context):
        return False

    def draw(self, context):
        draw_uv_gpt_panel(self.layout, context)


class UVGPT_PT_image_editor(bpy.types.Panel):
    bl_label = "uv GPT"
    bl_idname = "UVGPT_PT_image_editor"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "uv GPT"

    @classmethod
    def poll(cls, context):
        return _is_uv_gpt_image_editor_context(context)

    def draw(self, context):
        draw_uv_gpt_panel(self.layout, context)


class UVGPT_PT_quick_reinstall(bpy.types.Panel):
    bl_label = "Quick Reinstall"
    bl_idname = "UVGPT_PT_quick_reinstall"
    bl_space_type = "IMAGE_EDITOR"
    bl_region_type = "UI"
    bl_category = "uv GPT"
    bl_options = {"DEFAULT_CLOSED"}

    @classmethod
    def poll(cls, context):
        return _is_uv_gpt_image_editor_context(context)

    def draw(self, context):
        del context
        layout = self.layout
        layout.label(text=f"Target: {quick_reinstall_tools.ADDON_MODULE}")
        layout.label(text="Location: Blender User Add-ons")
        layout.label(
            text=f"ZIP: {quick_reinstall_tools._canonical_zip_file()}"
        )
        layout.operator(
            "uv_gpt.quick_reinstall_from_canonical_zip",
            text="Quick Reinstall Add-on",
        )


classes = (
    UVGPT_UL_tdensity_presets,
    UVGPT_PT_view3d,
    UVGPT_PT_image_editor,
    UVGPT_PT_quick_reinstall,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
