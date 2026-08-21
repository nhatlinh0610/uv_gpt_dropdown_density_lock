import bpy
import bmesh
import blf
import time


TD_OVERLAY_LABELS = []
ISLAND_OVERLAY_LABELS = []
_DRAW_HANDLERS = []
_TIMER_RUNNING = False
_EVENT_WATCHER_RUNNING = False
_NAV_WATCHER_RUNNING = False
_NAV_HIDDEN = False
_NAV_HIDE_UNTIL = 0.0
_NAV_LAST_VIEW_STATES = None
_NAV_STABLE_SINCE = 0.0
LIVE_REFRESH_INTERVAL = 0.75
NAV_WATCH_INTERVAL = 0.06
NAV_UNHIDE_DELAY = 0.18
MAX_LIVE_REFRESH_LABELS = 64
MAX_OUTLINED_LABELS = 96


def tag_redraw_image_editors():
    try:
        windows = bpy.context.window_manager.windows
    except Exception:
        windows = []
    for window in windows:
        screen = getattr(window, "screen", None)
        if not screen:
            continue
        for area in screen.areas:
            if area.type == "IMAGE_EDITOR":
                area.tag_redraw()


def _overlay_active():
    return bool(TD_OVERLAY_LABELS or ISLAND_OVERLAY_LABELS)


def _context_object(context):
    return getattr(context, "edit_object", None) or getattr(context, "object", None)


def _label_object(context, label):
    obj_name = label.get("object_name")
    if obj_name:
        obj = bpy.data.objects.get(obj_name)
        if obj and obj.type == "MESH":
            return obj
    obj = _context_object(context)
    return obj if obj and obj.type == "MESH" else None


def _object_active_uv_name(obj):
    if not obj or obj.type != "MESH" or not obj.data.uv_layers.active:
        return ""
    return obj.data.uv_layers.active.name


def _active_uv_name(context):
    return _object_active_uv_name(_context_object(context))


def _label_visible(context, label):
    obj = _label_object(context, label)
    if not obj or obj.type != "MESH":
        return False
    if obj.mode != "EDIT":
        return False
    if label.get("object_name") and label.get("object_name") != obj.name:
        return False
    return True


def _draw_text(font_id, x, y, text, size=13, dense=False):
    try:
        blf.size(font_id, size)
    except TypeError:
        blf.size(font_id, size, 72)
    width, height = blf.dimensions(font_id, text)
    x -= width * 0.5
    y -= height * 0.5

    blf.color(font_id, 0.0, 0.0, 0.0, 0.92)
    offsets = ((1, -1),) if dense else ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, 1))
    for dx, dy in offsets:
        blf.position(font_id, x + dx, y + dy, 0)
        blf.draw(font_id, text)

    blf.color(font_id, 1.0, 0.92, 0.15, 1.0)
    blf.position(font_id, x, y, 0)
    blf.draw(font_id, text)


def _set_nav_hidden(value, duration=0.0):
    global _NAV_HIDDEN, _NAV_HIDE_UNTIL
    _NAV_HIDE_UNTIL = time.monotonic() + duration if value and duration else 0.0
    if _NAV_HIDDEN == value:
        return
    _NAV_HIDDEN = value
    tag_redraw_image_editors()


def _nav_hidden():
    if _NAV_HIDDEN and _NAV_HIDE_UNTIL and time.monotonic() > _NAV_HIDE_UNTIL:
        _set_nav_hidden(False)
    return _NAV_HIDDEN


def _image_editor_view_states():
    states = []
    for region in _image_editor_window_regions():
        view2d = getattr(region, "view2d", None)
        if not view2d:
            continue
        min_u, min_v = view2d.region_to_view(0, 0)
        max_u, max_v = view2d.region_to_view(region.width, region.height)
        states.append(
            (
                region.as_pointer(),
                round(min_u, 5),
                round(min_v, 5),
                round(max_u, 5),
                round(max_v, 5),
            )
        )
    return tuple(states)


def _navigation_watch_timer():
    global _NAV_WATCHER_RUNNING, _NAV_LAST_VIEW_STATES, _NAV_STABLE_SINCE
    if not _NAV_WATCHER_RUNNING:
        return None
    if not (TD_OVERLAY_LABELS or ISLAND_OVERLAY_LABELS):
        stop_navigation_watcher()
        return None

    states = _image_editor_view_states()
    if _NAV_LAST_VIEW_STATES and states and states != _NAV_LAST_VIEW_STATES:
        clear_labels_for_navigation()
        return None

    _NAV_LAST_VIEW_STATES = states
    return NAV_WATCH_INTERVAL


def ensure_navigation_watcher():
    global _NAV_WATCHER_RUNNING, _NAV_LAST_VIEW_STATES, _NAV_STABLE_SINCE
    if _NAV_WATCHER_RUNNING:
        return
    _NAV_WATCHER_RUNNING = True
    _NAV_LAST_VIEW_STATES = _image_editor_view_states()
    _NAV_STABLE_SINCE = time.monotonic()
    try:
        bpy.app.timers.register(_navigation_watch_timer, first_interval=NAV_WATCH_INTERVAL)
    except ValueError:
        pass


def stop_navigation_watcher():
    global _NAV_WATCHER_RUNNING, _NAV_LAST_VIEW_STATES, _NAV_STABLE_SINCE
    _NAV_WATCHER_RUNNING = False
    _NAV_LAST_VIEW_STATES = None
    _NAV_STABLE_SINCE = 0.0
    _set_nav_hidden(False)


def clear_labels_for_navigation():
    TD_OVERLAY_LABELS.clear()
    ISLAND_OVERLAY_LABELS.clear()
    remove_draw_handler()
    stop_overlay_timer()
    stop_event_watcher()
    stop_navigation_watcher()
    tag_redraw_image_editors()


def _label_loops(context, label):
    face_indices = label.get("face_indices")
    if not face_indices:
        return None, None, None
    obj = _label_object(context, label)
    if not obj or obj.type != "MESH" or obj.mode != "EDIT":
        return None, None, None

    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    uv_name = label.get("uv_map_name")
    uv_layer = bm.loops.layers.uv.get(uv_name) if uv_name else None
    if uv_layer is None:
        active_name = _object_active_uv_name(obj)
        uv_layer = bm.loops.layers.uv.get(active_name) if active_name else None
    if uv_layer is None:
        uv_layer = bm.loops.layers.uv.verify()
    loops = []
    for face_index in face_indices:
        if 0 <= face_index < len(bm.faces):
            loops.extend(bm.faces[face_index].loops)
    return obj, uv_layer, loops


def _dynamic_label_state(_context, label):
    return label.get("center", (0.0, 0.0)), label.get("text", "")


def _uv_layer_for_label(bm, obj, label):
    uv_name = label.get("uv_map_name")
    uv_layer = bm.loops.layers.uv.get(uv_name) if uv_name else None
    if uv_layer is None:
        active_name = _object_active_uv_name(obj)
        uv_layer = bm.loops.layers.uv.get(active_name) if active_name else None
    if uv_layer is None:
        uv_layer = bm.loops.layers.uv.verify()
    return uv_layer


def _loops_from_label(bm, label):
    face_indices = label.get("face_indices")
    if not face_indices:
        return []
    loops = []
    for face_index in face_indices:
        if 0 <= face_index < len(bm.faces):
            loops.extend(bm.faces[face_index].loops)
    return loops


def _update_dynamic_label(context, obj, bm, label, area_cache):
    from . import island_tools, texel_density, uv_utils

    center = label.get("center", (0.0, 0.0))
    text = label.get("text", "")
    uv_layer = _uv_layer_for_label(bm, obj, label)
    loops = _loops_from_label(bm, label)
    if not loops:
        label["center"] = center
        label["text"] = text
        return

    center_vec = uv_utils.get_loops_center(loops, uv_layer)
    center = (center_vec.x, center_vec.y)

    if label.get("td_dynamic"):
        px_cm = texel_density.calculate_island_px_cm(context, obj, loops, uv_layer)
        label["center"] = center
        label["text"] = texel_density.format_td_label(context, px_cm)
        return

    if label.get("island_dynamic"):
        settings = context.scene.uv_gpt_settings
        parts = []
        island_index = label.get("island_index")
        if settings.show_island_numbers and island_index is not None:
            parts.append(f"#{island_index:02d}")
        if settings.show_area_percent:
            cache_key = (obj.name, label.get("uv_map_name", ""))
            total_area = area_cache.get(cache_key)
            if total_area is None:
                all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
                total_area = sum(
                    island_tools.get_island_area(island, uv_layer)
                    for island in all_islands
                )
                area_cache[cache_key] = total_area
            area = island_tools.get_island_area(loops, uv_layer)
            percent = 0.0 if total_area <= 0.0 else (area / total_area) * 100.0
            parts.append(f"{percent:.1f}%")
        if settings.show_texel_density:
            px_cm = texel_density.calculate_island_px_cm(context, obj, loops, uv_layer)
            parts.append(texel_density.format_td_label(context, px_cm))
        text = " | ".join(parts)

    label["center"] = center
    label["text"] = text


def _refresh_dynamic_labels():
    labels = TD_OVERLAY_LABELS + ISLAND_OVERLAY_LABELS
    if not labels:
        return
    context = bpy.context
    grouped_labels = {}
    for label in labels:
        obj = _label_object(context, label)
        if not obj or obj.type != "MESH" or obj.mode != "EDIT":
            continue
        grouped_labels.setdefault(obj, []).append(label)

    area_cache = {}
    for obj, obj_labels in grouped_labels.items():
        try:
            bm = bmesh.from_edit_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            bm.faces.index_update()
        except Exception:
            continue
        for label in obj_labels:
            try:
                _update_dynamic_label(context, obj, bm, label, area_cache)
            except Exception:
                continue
    tag_redraw_image_editors()


def _overlay_timer():
    if not _TIMER_RUNNING:
        return None
    if not _live_refresh_enabled():
        stop_overlay_timer()
        return None
    _refresh_dynamic_labels()
    if TD_OVERLAY_LABELS or ISLAND_OVERLAY_LABELS:
        return LIVE_REFRESH_INTERVAL
    return None


def _live_label_count():
    return len(TD_OVERLAY_LABELS) + len(ISLAND_OVERLAY_LABELS)


def _live_refresh_enabled():
    count = _live_label_count()
    return 0 < count <= MAX_LIVE_REFRESH_LABELS


def ensure_overlay_timer():
    global _TIMER_RUNNING
    if not _live_refresh_enabled():
        stop_overlay_timer()
        return
    if _TIMER_RUNNING:
        return
    _TIMER_RUNNING = True
    try:
        bpy.app.timers.register(_overlay_timer, first_interval=LIVE_REFRESH_INTERVAL)
    except ValueError:
        pass


def stop_overlay_timer():
    global _TIMER_RUNNING
    _TIMER_RUNNING = False


def sync_overlay_timer():
    if _live_refresh_enabled():
        ensure_overlay_timer()
    else:
        stop_overlay_timer()


def ensure_event_watcher(_context=None):
    if _EVENT_WATCHER_RUNNING:
        return
    try:
        bpy.ops.uv_gpt.overlay_event_watcher()
    except Exception:
        pass


def stop_event_watcher():
    global _EVENT_WATCHER_RUNNING
    _EVENT_WATCHER_RUNNING = False
    _set_nav_hidden(False)


def _draw_callback(region=None):
    try:
        if _nav_hidden():
            return
        context = bpy.context
        context_region = getattr(context, "region", None)
        if context_region and getattr(context_region, "view2d", None):
            region = context_region
        elif region is None:
            region = context_region
        if not region:
            return
        view2d = getattr(region, "view2d", None)
        if not view2d:
            return
        labels = TD_OVERLAY_LABELS + ISLAND_OVERLAY_LABELS
        dense = len(labels) > MAX_OUTLINED_LABELS
        text_size = 11 if dense else 13
        for label in labels:
            if not _label_visible(context, label):
                continue
            center, text = _dynamic_label_state(context, label)
            if not text:
                continue
            x, y = view2d.view_to_region(center[0], center[1], clip=True)
            if x is None or y is None:
                continue
            _draw_text(0, x, y, text, size=text_size, dense=dense)
    except Exception:
        return


def _image_editor_window_regions():
    regions = []
    try:
        windows = bpy.context.window_manager.windows
    except Exception:
        windows = []
    for window in windows:
        screen = getattr(window, "screen", None)
        if not screen:
            continue
        for area in screen.areas:
            if area.type != "IMAGE_EDITOR":
                continue
            for region in area.regions:
                if region.type == "WINDOW":
                    regions.append(region)
                    break
    return regions


def ensure_draw_handler():
    if _DRAW_HANDLERS:
        return
    regions = _image_editor_window_regions() or [None]
    for region in regions:
        _DRAW_HANDLERS.append(
            bpy.types.SpaceImageEditor.draw_handler_add(
                _draw_callback, (region,), "WINDOW", "POST_PIXEL"
            )
        )


def remove_draw_handler():
    while _DRAW_HANDLERS:
        handler = _DRAW_HANDLERS.pop()
        try:
            bpy.types.SpaceImageEditor.draw_handler_remove(handler, "WINDOW")
        except Exception:
            pass


def reset_draw_handler():
    remove_draw_handler()
    ensure_draw_handler()


def set_td_labels(labels):
    TD_OVERLAY_LABELS[:] = labels
    if labels:
        reset_draw_handler()
        ensure_navigation_watcher()
        ensure_event_watcher()
    sync_overlay_timer()
    tag_redraw_image_editors()


def clear_td_labels():
    TD_OVERLAY_LABELS.clear()
    if not ISLAND_OVERLAY_LABELS:
        remove_draw_handler()
        stop_navigation_watcher()
        stop_event_watcher()
    sync_overlay_timer()
    tag_redraw_image_editors()


def td_overlay_active():
    return bool(TD_OVERLAY_LABELS)


def set_island_labels(labels):
    ISLAND_OVERLAY_LABELS[:] = labels
    if labels:
        reset_draw_handler()
        ensure_navigation_watcher()
        ensure_event_watcher()
    sync_overlay_timer()
    tag_redraw_image_editors()


def clear_island_labels():
    ISLAND_OVERLAY_LABELS.clear()
    if not TD_OVERLAY_LABELS:
        remove_draw_handler()
        stop_navigation_watcher()
        stop_event_watcher()
    sync_overlay_timer()
    tag_redraw_image_editors()


def refresh_island_labels(context):
    labels = _build_island_labels(context)
    set_island_labels(labels)
    return labels


def _build_island_labels(context):
    from . import island_tools, texel_density

    settings = context.scene.uv_gpt_settings
    obj = _context_object(context)
    if not obj or obj.type != "MESH" or obj.mode != "EDIT":
        return []
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    total_area = sum(island_tools.get_island_area(island, uv_layer) for island in islands)
    labels = []
    for index, island in enumerate(islands, start=1):
        parts = []
        if settings.show_island_numbers:
            parts.append(f"#{index:02d}")
        if settings.show_area_percent:
            area = island_tools.get_island_area(island, uv_layer)
            percent = 0.0 if total_area <= 0.0 else (area / total_area) * 100.0
            parts.append(f"{percent:.1f}%")
        if settings.show_texel_density:
            px_cm = texel_density.calculate_island_px_cm(context, obj, island, uv_layer)
            parts.append(texel_density.format_td_label(context, px_cm))
        if not parts:
            continue
        center = island_tools.get_island_center(island, uv_layer)
        labels.append(
            {
                "object_name": obj.name,
                "uv_map_name": obj.data.uv_layers.active.name if obj.data.uv_layers.active else "",
                "face_indices": [face.index for face in island_tools.island_faces(island)],
                "center": (center.x, center.y),
                "text": " | ".join(parts),
                "island_dynamic": True,
                "island_index": index,
            }
        )
    return labels


class UVGPT_OT_overlay_event_watcher(bpy.types.Operator):
    bl_idname = "uv_gpt.overlay_event_watcher"
    bl_label = "uv GPT Overlay Event Watcher"
    bl_description = "Temporarily hides UV overlay labels while navigating the UV Editor"

    def execute(self, context):
        global _EVENT_WATCHER_RUNNING
        if _EVENT_WATCHER_RUNNING:
            return {"FINISHED"}
        _EVENT_WATCHER_RUNNING = True
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, _context, event):
        if not _EVENT_WATCHER_RUNNING or not _overlay_active():
            stop_event_watcher()
            return {"CANCELLED"}

        if event.type == "MIDDLEMOUSE":
            if event.value == "PRESS":
                _set_nav_hidden(True, duration=2.0)
            elif event.value == "RELEASE":
                _set_nav_hidden(False)
        elif event.type in {"MOUSEMOVE", "INBETWEEN_MOUSEMOVE"} and _NAV_HIDDEN:
            _set_nav_hidden(True, duration=0.35)
        elif event.type in {"TRACKPADPAN", "TRACKPADZOOM", "NDOF_MOTION"}:
            _set_nav_hidden(True, duration=0.35)

        return {"PASS_THROUGH"}


class UVGPT_OT_refresh_overlay(bpy.types.Operator):
    bl_idname = "uv_gpt.refresh_overlay"
    bl_label = "Refresh Overlay"
    bl_description = "Refresh island overlay labels in the UV Editor"
    bl_options = {"REGISTER"}

    def execute(self, context):
        try:
            labels = refresh_island_labels(context)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Refreshed {len(labels)} overlay label(s).")
        return {"FINISHED"}


classes = (
    UVGPT_OT_overlay_event_watcher,
    UVGPT_OT_refresh_overlay,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    clear_td_labels()
    clear_island_labels()
    stop_navigation_watcher()
    stop_event_watcher()
    stop_overlay_timer()
    remove_draw_handler()
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
