import math

import bpy
import bmesh

from . import island_tools, uv_utils


STACK_KEY_PRECISION = 5
PACK_EPSILON = 1e-8


def _island_face_key(island):
    return tuple(sorted({loop.face.index for loop in island}))


def _pre_rotate_islands(islands, uv_layer, mode):
    if mode == "NONE":
        return
    for island in islands:
        center = island_tools.get_island_center(island, uv_layer)
        if mode == "ROT_90":
            uv_utils.rotate_island(island, uv_layer, center, math.radians(90.0))
        elif mode == "ROT_180":
            uv_utils.rotate_island(island, uv_layer, center, math.radians(180.0))
        elif mode == "CARDINAL":
            min_u, max_u, min_v, max_v = island_tools.get_island_bounds(island, uv_layer)
            if (max_v - min_v) > (max_u - min_u):
                uv_utils.rotate_island(island, uv_layer, center, math.radians(90.0))


def _islands_inside_tile(islands, uv_layer):
    epsilon = 1e-6
    for island in islands:
        for loop in island:
            uv = loop[uv_layer].uv
            if uv.x < -epsilon or uv.x > 1.0 + epsilon:
                return False
            if uv.y < -epsilon or uv.y > 1.0 + epsilon:
                return False
    return True


def _island_signature(island, uv_layer):
    bounds = tuple(
        round(value, STACK_KEY_PRECISION)
        for value in island_tools.get_island_bounds(island, uv_layer)
    )
    area = round(island_tools.get_island_area(island, uv_layer), STACK_KEY_PRECISION)
    uv_points = sorted(
        {
            (
                round(loop[uv_layer].uv.x, STACK_KEY_PRECISION),
                round(loop[uv_layer].uv.y, STACK_KEY_PRECISION),
            )
            for loop in island
        }
    )
    return (bounds, area, tuple(uv_points))


def _island_loop_records(island, uv_layer):
    records = []
    for face in island_tools.island_faces(island):
        for local_index, loop in enumerate(face.loops):
            records.append((face.index, local_index))
    return records


def _uv_snapshot(islands, uv_layer):
    snapshot = {}
    for face in {loop.face for island in islands for loop in island}:
        for local_index, loop in enumerate(face.loops):
            snapshot[(face.index, local_index)] = loop[uv_layer].uv.copy()
    return snapshot


def _restore_uv_snapshot(bm, uv_layer, snapshot):
    for (face_index, local_index), uv in snapshot.items():
        loop = _loop_from_record(bm, face_index, local_index)
        if loop is not None:
            loop[uv_layer].uv = uv.copy()


def _uv_snapshot_matches(bm, uv_layer, snapshot):
    for (face_index, local_index), expected in snapshot.items():
        loop = _loop_from_record(bm, face_index, local_index)
        if loop is None:
            return False
        actual = loop[uv_layer].uv
        if actual.x != expected.x or actual.y != expected.y:
            return False
    return True


def _loop_from_record(bm, face_index, local_index):
    if face_index >= len(bm.faces):
        return None
    face = bm.faces[face_index]
    face_loops = list(face.loops)
    if local_index >= len(face_loops):
        return None
    return face_loops[local_index]


def _loops_from_records(bm, records):
    loops = []
    for face_index, local_index in records:
        loop = _loop_from_record(bm, face_index, local_index)
        if loop is not None:
            loops.append(loop)
    return loops


def _records_bounds(bm, uv_layer, records):
    loops = _loops_from_records(bm, records)
    return island_tools.get_island_bounds(loops, uv_layer)


def _scope_static_islands(all_islands, scope_islands):
    scope_keys = {_island_face_key(island) for island in scope_islands}
    return [
        island
        for island in all_islands
        if _island_face_key(island) not in scope_keys
    ]


def _rect_intersects(a, b):
    return not (
        a["max_u"] <= b["min_u"] + PACK_EPSILON
        or a["min_u"] >= b["max_u"] - PACK_EPSILON
        or a["max_v"] <= b["min_v"] + PACK_EPSILON
        or a["min_v"] >= b["max_v"] - PACK_EPSILON
    )


def _inflate_rect(rect, margin):
    return {
        "min_u": rect["min_u"] - margin,
        "max_u": rect["max_u"] + margin,
        "min_v": rect["min_v"] - margin,
        "max_v": rect["max_v"] + margin,
    }


def _rect_from_bounds(bounds):
    min_u, max_u, min_v, max_v = bounds
    return {
        "min_u": min_u,
        "max_u": max_u,
        "min_v": min_v,
        "max_v": max_v,
    }


def _clip_rect_to_tile(rect):
    clipped = {
        "min_u": max(rect["min_u"], 0.0),
        "max_u": min(rect["max_u"], 1.0),
        "min_v": max(rect["min_v"], 0.0),
        "max_v": min(rect["max_v"], 1.0),
    }
    if clipped["max_u"] <= clipped["min_u"] or clipped["max_v"] <= clipped["min_v"]:
        return None
    return clipped


def _static_blockers(static_islands, uv_layer, margin):
    blockers = []
    for island in static_islands:
        rect = _clip_rect_to_tile(_rect_from_bounds(island_tools.get_island_bounds(island, uv_layer)))
        if rect is not None:
            blockers.append(_inflate_rect(rect, margin))
    return blockers


def _find_free_position(width, height, blockers, margin):
    xs = {margin}
    ys = {margin}
    for blocker in blockers:
        xs.add(max(margin, blocker["max_u"]))
        ys.add(max(margin, blocker["max_v"]))

    max_u = 1.0 - margin
    max_v = 1.0 - margin
    for y in sorted(ys):
        if y + height > max_v + PACK_EPSILON:
            continue
        for x in sorted(xs):
            if x + width > max_u + PACK_EPSILON:
                continue
            rect = {
                "min_u": x,
                "max_u": x + width,
                "min_v": y,
                "max_v": y + height,
            }
            if not any(_rect_intersects(rect, blocker) for blocker in blockers):
                return x, y
    return None


def _layout_with_static_islands(rectangles, static_islands, uv_layer, scale, margin):
    blockers = _static_blockers(static_islands, uv_layer, margin)
    placements = {}
    ordered = sorted(rectangles, key=lambda item: item["height"], reverse=True)
    for item in ordered:
        width = max(item["width"] * scale, 1e-9)
        height = max(item["height"] * scale, 1e-9)
        position = _find_free_position(width, height, blockers, margin)
        if position is None:
            return None
        x, y = position
        placements[item["index"]] = (x, y)
        blockers.append(
            _inflate_rect(
                {
                    "min_u": x,
                    "max_u": x + width,
                    "min_v": y,
                    "max_v": y + height,
                },
                margin,
            )
        )
    return placements


def _pack_islands_around_static(bm, uv_layer, islands, static_islands, margin, scale_to_fit=True):
    rectangles = []
    for index, island in enumerate(islands):
        min_u, max_u, min_v, max_v = island_tools.get_island_bounds(island, uv_layer)
        rectangles.append(
            {
                "index": index,
                "min_u": min_u,
                "min_v": min_v,
                "width": max(max_u - min_u, 1e-6),
                "height": max(max_v - min_v, 1e-6),
            }
        )

    if scale_to_fit:
        high = 1.0
        while _layout_with_static_islands(rectangles, static_islands, uv_layer, high, margin) and high < 1024.0:
            high *= 2.0
        low = 0.0
        best_placements = None
        for _ in range(36):
            mid = (low + high) * 0.5
            placements = _layout_with_static_islands(rectangles, static_islands, uv_layer, mid, margin)
            if placements is None:
                high = mid
            else:
                low = mid
                best_placements = placements
        scale = low
        placements = best_placements
    else:
        scale = 1.0
        placements = _layout_with_static_islands(rectangles, static_islands, uv_layer, scale, margin)

    if placements is None:
        return False

    rect_by_index = {item["index"]: item for item in rectangles}
    for index, island in enumerate(islands):
        item = rect_by_index[index]
        target_u, target_v = placements[index]
        for loop in island:
            uv = loop[uv_layer].uv
            uv.x = target_u + (uv.x - item["min_u"]) * scale
            uv.y = target_v + (uv.y - item["min_v"]) * scale
    return True


def _stack_preserve_plan(islands, uv_layer):
    buckets = {}
    for island in islands:
        buckets.setdefault(_island_signature(island, uv_layer), []).append(island)

    pack_islands = []
    pack_records = []
    stack_groups = []
    for group in buckets.values():
        representative = group[0]
        representative_records = _island_loop_records(representative, uv_layer)
        pack_islands.append(representative)
        pack_records.append(representative_records)
        if len(group) > 1:
            stack_groups.append(
                {
                    "representative": representative_records,
                    "source_bounds": island_tools.get_island_bounds(representative, uv_layer),
                    "followers": [
                        _island_loop_records(island, uv_layer) for island in group[1:]
                    ],
                }
            )
    return pack_islands, pack_records, stack_groups


def _expand_with_stacked_islands(selected_islands, all_islands, uv_layer):
    selected_signatures = {
        _island_signature(island, uv_layer) for island in selected_islands
    }
    return [
        island
        for island in all_islands
        if _island_signature(island, uv_layer) in selected_signatures
    ]


def _restore_stacked_groups(bm, uv_layer, stack_groups):
    restored = 0
    for group in stack_groups:
        source_min_u, source_max_u, source_min_v, source_max_v = group["source_bounds"]
        target_min_u, target_max_u, target_min_v, target_max_v = _records_bounds(
            bm,
            uv_layer,
            group["representative"],
        )
        source_width = max(source_max_u - source_min_u, 1e-8)
        source_height = max(source_max_v - source_min_v, 1e-8)
        target_width = target_max_u - target_min_u
        target_height = target_max_v - target_min_v
        scale_u = target_width / source_width
        scale_v = target_height / source_height

        for follower_records in group["followers"]:
            moved = False
            for face_index, local_index in follower_records:
                loop = _loop_from_record(bm, face_index, local_index)
                if loop is None:
                    continue
                uv = loop[uv_layer].uv
                uv.x = target_min_u + (uv.x - source_min_u) * scale_u
                uv.y = target_min_v + (uv.y - source_min_v) * scale_v
                moved = True
            if moved:
                restored += 1
    return restored


def _pack(context, operator, selected_only):
    settings = uv_utils.get_settings(context)
    unselected_mode = getattr(
        settings,
        "pack_selected_unselected_mode",
        "LOCK_UNSELECTED",
    )
    lock_unselected = selected_only and unselected_mode == "LOCK_UNSELECTED"
    obj = uv_utils.get_active_mesh_object(context)
    if selected_only:
        validation_bm = island_tools.get_active_bmesh(context)
        validation_uv_layer = island_tools.get_active_uv_layer(validation_bm, obj)
        island_tools.validate_uv_selection_scope(
            context,
            validation_bm,
            validation_uv_layer,
            refresh_invalid_sync=True,
        )
    uv_utils.ensure_destructive_ready(context)
    bm = island_tools.get_active_bmesh(context)
    uv_layer = island_tools.get_active_uv_layer(bm, obj)
    all_islands = island_tools.get_uv_islands(bm, uv_layer, selected_only=False)
    if selected_only:
        islands = island_tools.get_selected_uv_islands_for_context(
            context,
            bm,
            uv_layer,
        )
    else:
        islands = all_islands
    if not islands:
        operator.report({"ERROR"}, "No UV islands found to pack.")
        return {"CANCELLED"}
    original_island_count = len(islands)

    selection = uv_utils.store_uv_selection_state(bm, uv_layer)
    uv_snapshot = _uv_snapshot(all_islands, uv_layer)
    unselected_snapshot = _uv_snapshot(
        _scope_static_islands(all_islands, islands),
        uv_layer,
    )

    try:
        _pre_rotate_islands(islands, uv_layer, settings.rotation_mode)
        static_islands = _scope_static_islands(all_islands, islands) if lock_unselected else []
        if settings.pack_preserve_stacks:
            pack_islands, pack_records, stack_groups = _stack_preserve_plan(
                islands,
                uv_layer,
            )
        else:
            pack_islands = islands
            pack_records = [_island_loop_records(island, uv_layer) for island in pack_islands]
            stack_groups = []
        lock_density = selected_only and settings.pack_selected_lock_density
        scale_to_fit = not lock_density
        used_fallback = False
        used_static_packer = False
        used_stack_safe_packer = bool(stack_groups)
        overflowed_tile = False
        if selected_only:
            # Selected-only packing is deliberately data-scoped.  Native
            # bpy.ops.uv.pack_islands reads Blender's current UV-editor
            # selection context and can repack/eject the complement even after
            # select_uv_islands() appears to have isolated the requested UVs.
            # Both routes below receive explicit island records and therefore
            # cannot mutate an unselected loop by selection-context leakage.
            if lock_unselected and static_islands:
                used_static_packer = True
                fits = _pack_islands_around_static(
                    bm,
                    uv_layer,
                    pack_islands,
                    static_islands,
                    settings.margin,
                    scale_to_fit=scale_to_fit,
                )
                if not fits:
                    _restore_uv_snapshot(bm, uv_layer, uv_snapshot)
                    operator.report(
                        {"WARNING"},
                        "No free 0-1 space for selected UVs without overlapping locked unselected islands.",
                    )
                    return {"CANCELLED"}
                used_fallback = True
            else:
                # IGNORE_UNSELECTED intentionally permits overlap with the
                # complement, while still transforming selected loops only.
                # This also covers LOCK_UNSELECTED when there is no static
                # complement (all UV islands are selected).
                fits = uv_utils.basic_pack_islands(
                    bm,
                    uv_layer,
                    pack_islands,
                    settings.margin,
                    scale_to_fit=scale_to_fit,
                )
                overflowed_tile = not fits
                used_fallback = True
        else:
            # Pack Whole Mesh retains the established native route and its
            # explicit all-island selection.  It is intentionally outside the
            # selected-only boundary above.
            uv_utils.select_uv_islands(context, bm, uv_layer, pack_islands)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
            try:
                uv_utils.run_uv_pack(
                    context,
                    settings.margin,
                    rotate=settings.rotation_mode == "CARDINAL",
                    scale=scale_to_fit,
                )
            except Exception:
                bm = island_tools.get_active_bmesh(context)
                uv_layer = island_tools.get_active_uv_layer(bm, obj)
                pack_islands = [
                    loops
                    for loops in (
                        _loops_from_records(bm, records) for records in pack_records
                    )
                    if loops
                ]
                fits = uv_utils.basic_pack_islands(
                    bm,
                    uv_layer,
                    pack_islands,
                    settings.margin,
                    scale_to_fit=scale_to_fit,
                )
                overflowed_tile = not fits
                used_fallback = True

        bm = island_tools.get_active_bmesh(context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        bm.faces.ensure_lookup_table()
        bm.faces.index_update()
        preserved_stacks = _restore_stacked_groups(bm, uv_layer, stack_groups)
        if lock_density and not overflowed_tile:
            overflowed_tile = not _islands_inside_tile(islands, uv_layer)

        if selected_only and not _uv_snapshot_matches(bm, uv_layer, unselected_snapshot):
            _restore_uv_snapshot(bm, uv_layer, uv_snapshot)
            operator.report(
                {"ERROR"},
                "Pack Selected changed unselected UVs; the operation was rolled back.",
            )
            return {"CANCELLED"}
    except Exception as exc:
        bm = island_tools.get_active_bmesh(context)
        uv_layer = island_tools.get_active_uv_layer(bm, obj)
        _restore_uv_snapshot(bm, uv_layer, uv_snapshot)
        operator.report({"ERROR"}, f"Pack failed and was rolled back: {exc}")
        return {"CANCELLED"}
    finally:
        try:
            bm = island_tools.get_active_bmesh(context)
            uv_layer = island_tools.get_active_uv_layer(bm, obj)
            uv_utils.restore_uv_selection_state(bm, uv_layer, selection)
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        except Exception as exc:
            operator.report({"ERROR"}, f"Could not restore UV selection state: {exc}")

    suffix = ""
    if used_static_packer:
        suffix = " avoiding locked unselected islands"
    elif used_stack_safe_packer:
        suffix = " using stack-safe packer"
    elif used_fallback:
        suffix = " using internal packer"
    density_note = " with density locked" if lock_density else ""
    stack_note = f" and kept {preserved_stacks} stacked island(s)" if preserved_stacks else ""
    if selected_only and lock_unselected:
        scope_note = " with unselected islands locked"
    elif selected_only and unselected_mode == "IGNORE_UNSELECTED":
        scope_note = " while ignoring unselected islands"
    else:
        scope_note = ""
    if overflowed_tile:
        operator.report(
            {"WARNING"},
            "Density locked: packed islands extend outside the 0-1 tile.",
        )
    operator.report(
        {"INFO"},
        f"Packed {original_island_count} UV island(s){density_note}{stack_note}{scope_note}{suffix}.",
    )
    return {"FINISHED"}


class UVGPT_OT_pack_selected(bpy.types.Operator):
    bl_idname = "uv_gpt.pack_selected"
    bl_label = "Pack Selected"
    bl_description = "Pack selected UV islands into the 0-1 tile"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            return _pack(context, self, selected_only=True)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


class UVGPT_OT_pack_whole_mesh(bpy.types.Operator):
    bl_idname = "uv_gpt.pack_whole_mesh"
    bl_label = "Pack Whole Mesh"
    bl_description = "Pack all UV islands into the 0-1 tile"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        try:
            return _pack(context, self, selected_only=False)
        except RuntimeError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}


classes = (
    UVGPT_OT_pack_selected,
    UVGPT_OT_pack_whole_mesh,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
