import bpy
from bpy.props import (
    BoolProperty,
    CollectionProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import PropertyGroup


def _uv_map_items(self, context):
    obj = getattr(context, "object", None)
    if not obj or obj.type != "MESH" or not obj.data.uv_layers:
        return [("NONE", "No UV Map", "")]
    return [(layer.name, layer.name, "") for layer in obj.data.uv_layers]


def _update_active_uv_map(self, context):
    obj = getattr(context, "object", None)
    name = getattr(self, "active_uv_map", "")
    if not obj or obj.type != "MESH" or not name or name == "NONE":
        return
    for index, layer in enumerate(obj.data.uv_layers):
        if layer.name == name:
            obj.data.uv_layers.active_index = index
            if obj.mode == "EDIT":
                try:
                    import bmesh

                    bm = bmesh.from_edit_mesh(obj.data)
                    bm_layer = bm.loops.layers.uv.get(name)
                    if bm_layer is not None:
                        bm.loops.layers.uv.active = bm_layer
                except Exception:
                    pass
            break


def _update_overlay_labels(self, context):
    try:
        from . import overlay

        overlay.refresh_island_labels(context)
    except Exception:
        pass


class UVGPT_TDensityPreset(PropertyGroup):
    name: StringProperty(name="Preset Name", default="Medium")
    px_cm: FloatProperty(name="px/cm", default=40.0, min=0.0001, precision=3)
    color: FloatVectorProperty(
        name="Preset Color",
        subtype="COLOR",
        size=4,
        min=0.0,
        max=1.0,
        default=(0.25, 0.65, 1.0, 1.0),
    )


class UVGPT_Settings(PropertyGroup):
    active_uv_map: EnumProperty(
        name="UV Map",
        description="Active UV map used by uv GPT tools",
        items=_uv_map_items,
        update=_update_active_uv_map,
    )
    duplicate_before_operations: BoolProperty(
        name="Duplicate before operations",
        description="Copy the active UV map to Bake_Optimized before destructive UV edits",
        default=False,
    )

    margin: FloatProperty(name="Margin", default=0.003, min=0.0, precision=4)
    rotation_mode: EnumProperty(
        name="Rotation Mode",
        items=(
            ("NONE", "None", ""),
            ("ROT_90", "90 deg", ""),
            ("ROT_180", "180 deg", ""),
            ("CARDINAL", "Cardinal 90 deg Steps", ""),
        ),
        default="NONE",
    )
    pack_selected_lock_density: BoolProperty(
        name="Lock Density",
        description="Keep selected UV islands at their current scale when using Pack Selected",
        default=True,
    )
    pack_selected_unselected_mode: EnumProperty(
        name="Unselected UVs",
        description="Choose how Pack Selected treats UV islands outside the current selection",
        items=(
            (
                "LOCK_UNSELECTED",
                "Lock Unselected UVs",
                "Treat unselected islands as fixed blockers and do not overlap them",
            ),
            (
                "IGNORE_UNSELECTED",
                "Ignore Unselected UVs",
                "Pack only the selected islands and allow overlap with unselected islands",
            ),
        ),
        default="LOCK_UNSELECTED",
    )
    pack_preserve_stacks: BoolProperty(
        name="Keep Stack Exact",
        description="Pack one representative from exact-overlapped selected stacks, then restore stack followers exactly",
        default=True,
    )
    pack_selected_only_move_selected: BoolProperty(
        name="Deprecated Only Move Selected",
        description="Legacy setting kept for older Blender sessions",
        default=True,
        options={"HIDDEN"},
    )

    texture_size_mode: EnumProperty(
        name="Texture Size",
        items=(
            ("1024", "1024", ""),
            ("2048", "2048", ""),
            ("4096", "4096", ""),
            ("CUSTOM", "Custom", ""),
        ),
        default="2048",
    )
    custom_texture_size: IntProperty(name="Custom", default=2048, min=1)
    display_unit: EnumProperty(
        name="Display Unit",
        items=(
            ("PX_CM", "px/cm", ""),
            ("PX_UNIT", "px/unit", ""),
            ("BOTH", "both", ""),
        ),
        default="BOTH",
    )
    input_unit: EnumProperty(
        name="Input Unit",
        items=(
            ("PX_CM", "px/cm", ""),
            ("PX_UNIT", "px/unit", ""),
        ),
        default="PX_CM",
    )
    target_value: FloatProperty(name="Target Value", default=40.0, min=0.0001, precision=3)
    presets: CollectionProperty(type=UVGPT_TDensityPreset)
    active_preset_index: IntProperty(name="Preset", default=0, min=0)
    default_presets_loaded: BoolProperty(default=False, options={"HIDDEN"})

    mirror_pivot: EnumProperty(
        name="Mirror Pivot",
        items=(
            ("SELECTION", "Selection Center", ""),
            ("TILE_U", "Tile Center U=0.5", ""),
        ),
        default="SELECTION",
    )

    symmetry_axis: EnumProperty(
        name="Symmetry Axis",
        items=(
            ("U_HALF", "U = 0.5", ""),
            ("V_HALF", "V = 0.5", ""),
            ("CUSTOM_U", "Custom U", ""),
            ("CUSTOM_V", "Custom V", ""),
        ),
        default="U_HALF",
    )
    custom_axis_value: FloatProperty(name="Custom Axis Value", default=0.5, precision=4)
    match_rotation: BoolProperty(
        name="Keep Parallel Rotation",
        description="Rotate the target only enough to stay parallel to the reference island",
        default=True,
    )
    match_scale: BoolProperty(name="Match Scale", default=True)
    preserve_target_shape: BoolProperty(name="Preserve Target Shape", default=True)
    keep_inside_tile: BoolProperty(name="Keep Inside 0-1 UV Tile", default=False)

    similarity_mode: EnumProperty(
        name="Similarity Mode",
        items=(
            ("FACE_COUNT", "Face Count Only", ""),
            ("BOUNDS", "Face Count + Bounds Ratio", ""),
            ("AREA", "Face Count + Bounds Ratio + Area", ""),
        ),
        default="BOUNDS",
    )
    stack_match_scale: BoolProperty(name="Match Scale", default=True)
    stack_allow_flipping: BoolProperty(name="Allow Flipping", default=False)
    stack_similarity_tolerance: FloatProperty(
        name="Similarity Tolerance",
        default=0.01,
        min=0.0,
        precision=4,
    )

    show_island_numbers: BoolProperty(
        name="Show Island Numbers",
        default=False,
        update=_update_overlay_labels,
    )
    show_area_percent: BoolProperty(
        name="Show Area %",
        default=False,
        update=_update_overlay_labels,
    )
    show_texel_density: BoolProperty(
        name="Show Texel Density",
        default=False,
        update=_update_overlay_labels,
    )

    ui_show_uv_map: BoolProperty(name="UV Map", default=False)
    ui_show_pack: BoolProperty(name="Pack", default=True)
    ui_show_texel_density: BoolProperty(name="Density", default=False)
    ui_show_td_presets: BoolProperty(
        name="Texel Density Presets",
        default=False,
        options={"HIDDEN"},
    )
    ui_show_transform: BoolProperty(name="Transform", default=False, options={"HIDDEN"})
    ui_show_symmetry: BoolProperty(name="Symmetry", default=False)
    ui_show_stack: BoolProperty(name="Stack", default=False)
    ui_show_overlay: BoolProperty(name="Overlay", default=False)


classes = (
    UVGPT_TDensityPreset,
    UVGPT_Settings,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.uv_gpt_settings = PointerProperty(type=UVGPT_Settings)


def unregister():
    if hasattr(bpy.types.Scene, "uv_gpt_settings"):
        del bpy.types.Scene.uv_gpt_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
