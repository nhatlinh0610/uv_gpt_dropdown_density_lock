import sys
import bpy

from . import quick_reinstall_tools, tdensity_presets, texel_density
from . import ADDON_VERSION


def _settings(context):
    return context.scene.uv_gpt_settings


def _active_preset(settings):
    return tdensity_presets.active_preset(settings)


def _section(layout, settings, prop_name, title, icon="NONE", badge=""):
    box = layout.box()
    is_open = getattr(settings, prop_name)
    tri_icon = "TRIA_DOWN" if is_open else "TRIA_RIGHT"
    row = box.row(align=True)
    row.prop(settings, prop_name, text="", icon=tri_icon, emboss=False)
    if icon and icon != "NONE":
        row.prop(settings, prop_name, text=title, icon=icon, emboss=False)
    else:
        row.prop(settings, prop_name, text=title, emboss=False)
    if badge:
        sub = row.row(align=True)
        sub.alignment = "RIGHT"
        sub.label(text=badge)
    return box if is_open else None


def _draw_split_row(container, factor=0.38):
    col = container.column(align=True)
    col.use_property_split = True
    col.use_property_decorate = False
    return col


def _draw_pack_report(box, report, obj):
    report_text = str(report.get("text") or "")
    warn_text = str(report.get("warn") or "")
    if not report_text and not warn_text:
        return
    cov = float(report.get("coverage_pct", 0.0) or 0.0)
    sc = float(report.get("scale", 0.0) or 0.0)
    mov = int(report.get("movable_count", 0) or 0)
    units = int(report.get("unit_count", 0) or 0)
    st = int(report.get("stack_followers", 0) or 0)
    ov = int(report.get("overlap_followers", 0) or 0)
    bn_span = float(report.get("bottleneck_span_pct", 0.0) or 0.0)
    bn_cnt = int(report.get("bottleneck_face_count", 0) or 0)
    bn_faces = report.get("bottleneck_faces") or ()

    card = box.box()
    if report.get("ok") is True and cov > 0.0:
        row = card.row(align=True)
        row.label(text=f"Fill {cov:.1f}%", icon="CHECKMARK")
        right = row.row(align=True)
        right.alignment = "RIGHT"
        right.label(text=f"{sc:.3f}x")
        parts = [f"{mov} isl"]
        if units > 0:
            parts.append(f"{units} u")
        if st > 0:
            parts.append(f"{st} st")
        if ov > 0:
            parts.append(f"{ov} ov")
        card.label(text="  |  ".join(parts))
    elif report_text:
        icon = "ERROR" if report.get("ok") is False else "TIME"
        card.label(text=report_text, icon=icon)

    if warn_text:
        is_bottleneck = bn_span >= 96.0 and cov < 68.0
        wbox = card.box()
        wbox.alert = is_bottleneck
        if is_bottleneck and bn_cnt > 0:
            wbox.label(text=f"1 island hits {bn_span:.0f}% tile ({bn_cnt}f)", icon="ERROR")
            wbox.label(text="Split UV seam to scale up")
        else:
            wbox.label(text=warn_text, icon="INFO")
        if (
            is_bottleneck
            and bn_faces
            and bn_cnt > 0
            and str(report.get("object_name") or "") == obj.name
        ):
            card.operator(
                "uv_gpt.select_pack_bottleneck",
                text=f"Select Bottleneck ({bn_cnt}f)",
                icon="RESTRICT_SELECT_OFF",
            )


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

    hdr = layout.row(align=True)
    hdr.label(text=f"uv GPT v{ADDON_VERSION}")
    obj = getattr(context, "edit_object", None) or getattr(context, "object", None)
    if obj and obj.type == "MESH" and obj.data.uv_layers:
        active_uv = obj.data.uv_layers.active
        if active_uv is not None:
            sub = hdr.row(align=True)
            sub.alignment = "RIGHT"
            sub.label(text=active_uv.name, icon="GROUP_UVS")
    if not obj or obj.type != "MESH":
        layout.box().label(text="Select a mesh object", icon="INFO")
        return
    if obj.mode != "EDIT":
        layout.box().label(text="Enter Edit Mode to edit UVs", icon="INFO")
        return
    if len(obj.data.uv_layers) > 1:
        uv_row = _draw_split_row(layout)
        uv_row.prop(settings, "active_uv_map", text="UV")

    pack_mod = sys.modules.get(f"{__package__}.pack_tools" if __package__ else "pack_tools")
    report = getattr(pack_mod, "_LAST_PACK_V2_REPORT", None) or {}
    cov = float(report.get("coverage_pct", 0.0) or 0.0)
    pack_badge = f"{cov:.1f}%" if (report.get("ok") is True and cov > 0.0) else ""

    box = _section(layout, settings, "ui_show_pack", "Pack", icon="PACKAGE", badge=pack_badge)
    if box:
        split_col = _draw_split_row(box)
        split_col.prop(settings, "margin")
        split_col.prop(settings, "rotation_mode")
        box.prop(settings, "pack_lock_overlapping")
        trow = box.row(align=True)
        trow.prop(settings, "pack_selected_keep_current_scale_v2", text="Keep Scale")
        if settings.pack_lock_overlapping == "EXACT":
            trow.prop(settings, "pack_preserve_stacks", text="Keep Exact")
        row = box.row(align=True)
        row.scale_y = 1.25
        row.operator("uv_gpt.pack_selected")
        row.operator("uv_gpt.pack_whole_mesh")
        _draw_pack_report(box, report, obj)

    td_badge = f"{settings.texture_size_mode}px" if settings.texture_size_mode != "CUSTOM" else "Custom"
    box = _section(layout, settings, "ui_show_texel_density", "Density", icon="TEXTURE", badge=td_badge)
    if box:
        split_col = _draw_split_row(box)
        row = split_col.row(align=True)
        row.prop(settings, "texture_size_mode")
        if settings.texture_size_mode == "CUSTOM":
            row.prop(settings, "custom_texture_size", text="")
        split_col.prop(settings, "display_unit")
        split_col.prop(settings, "input_unit")
        trow = split_col.row(align=True)
        trow.prop(settings, "target_value")
        trow.operator("uv_gpt.set_target_from_selected", text="", icon="EYEDROPPER")

        arow = box.row(align=True)
        arow.scale_y = 1.15
        arow.operator("uv_gpt.apply_td_selected", text="Apply Selected")
        arow.operator("uv_gpt.apply_td_whole_mesh", text="Apply Whole")
        box.operator("uv_gpt.show_all_td", text="Show All", icon="HIDE_OFF")

        srow = box.split(factor=0.45, align=True)
        srow.prop(settings, "small_uv_percent", text="Max %")
        srow.operator("uv_gpt.select_small_uv_by_percent", text="Select Small UV")

    st_cnt = int(report.get("stack_followers", 0) or 0)
    stack_badge = f"{st_cnt} st" if st_cnt > 0 else ""
    box = _section(layout, settings, "ui_show_stack", "Stack", icon="DUPLICATE", badge=stack_badge)
    if box:
        trow = box.row(align=True)
        trow.prop(settings, "stack_match_scale")
        trow.prop(settings, "stack_allow_flipping")
        split_col = _draw_split_row(box)
        split_col.prop(settings, "stack_similarity_tolerance")
        box.operator("uv_gpt.paste_keep_position")
        row = box.row(align=True)
        row.scale_y = 1.2
        row.operator_context = "INVOKE_DEFAULT"
        row.operator("uv_gpt.align_similar_pro_fast", text="Fast")
        row.operator("uv_gpt.align_similar_pro_snap", text="Pro")

    box = _section(layout, settings, "ui_show_symmetry", "Symmetry", icon="MOD_MIRROR")
    if box:
        row = box.row(align=True)
        row.label(text="Axis")
        row.prop_enum(settings, "symmetry_axis", "U_HALF", text="U")
        row.prop_enum(settings, "symmetry_axis", "V_HALF", text="V")
        if settings.symmetry_axis not in {"U_HALF", "V_HALF"}:
            box.label(text="Choose U or V first", icon="INFO")
        mrow = box.row(align=True)
        mrow.scale_y = 1.15
        mrow.operator("uv_gpt.symmetry_auto_mirror", text="Mirror Symmetry")
        col = box.column(align=True)
        col.scale_y = 0.85
        col.label(text="1 island: Mirror across U=0.5 or V=0.5 (no scale)")
        col.label(text="2 islands: Anchor first -> active/last")

    box = _section(layout, settings, "ui_show_overlay", "Overlay", icon="OVERLAY")
    if box:
        prow = box.row(align=True)
        prow.prop(settings, "show_island_numbers", text="#", toggle=True)
        prow.prop(settings, "show_area_percent", text="%", toggle=True)
        prow.prop(settings, "show_texel_density", text="TD", toggle=True)
        box.operator("uv_gpt.refresh_overlay", icon="FILE_REFRESH")


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
