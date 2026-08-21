import math

import bmesh
from mathutils import Vector


EPSILON = 1e-6


def get_active_bmesh(context):
    obj = getattr(context, "edit_object", None) or getattr(context, "object", None)
    if not obj or obj.type != "MESH":
        raise RuntimeError("Select a mesh object.")
    if obj.mode != "EDIT":
        raise RuntimeError("uv GPT works on UVs in Edit Mode.")
    bm = bmesh.from_edit_mesh(obj.data)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()
    return bm


def get_active_uv_layer(bm, obj):
    active_name = None
    if obj.data.uv_layers and obj.data.uv_layers.active:
        active_name = obj.data.uv_layers.active.name
    uv_layer = bm.loops.layers.uv.get(active_name) if active_name else None
    if uv_layer is None:
        uv_layer = bm.loops.layers.uv.verify()
    return uv_layer


def _loop_selected(loop, uv_layer):
    luv = loop[uv_layer]
    return bool(
        getattr(luv, "select", False)
        or getattr(luv, "select_edge", False)
        or loop.face.select
        or loop.vert.select
        or loop.edge.select
    )


def _context_uv_select_sync(context):
    scene = getattr(context, "scene", None)
    tool_settings = getattr(scene, "tool_settings", None)
    return bool(getattr(tool_settings, "use_uv_select_sync", False))


def _optional_bool(owner, attribute):
    value = getattr(owner, attribute, None)
    return None if value is None else bool(value)


def _set_optional_bool(owner, attribute, value):
    if value is None:
        return
    setter = getattr(owner, f"{attribute}_set", None)
    if setter is not None:
        try:
            setter(bool(value))
            return
        except (AttributeError, TypeError, RuntimeError):
            pass
    try:
        setattr(owner, attribute, bool(value))
    except (AttributeError, TypeError, RuntimeError):
        pass


def _selection_element_key(element):
    if isinstance(element, bmesh.types.BMFace):
        return ("FACE", int(element.index))
    if isinstance(element, bmesh.types.BMEdge):
        return ("EDGE", int(element.index))
    if isinstance(element, bmesh.types.BMVert):
        return ("VERT", int(element.index))
    return None


def _selection_element_from_key(bm, key):
    if not key:
        return None
    element_type, index = key
    collection = {
        "FACE": bm.faces,
        "EDGE": bm.edges,
        "VERT": bm.verts,
    }.get(element_type)
    if collection is None or index < 0 or index >= len(collection):
        return None
    return collection[index]


def _snapshot_uv_selection_state(bm, uv_layer):
    """Capture only state the non-destructive sync refresh must preserve."""
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.edges.ensure_lookup_table()
    bm.edges.index_update()
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()

    loops = {}
    for face in bm.faces:
        for local_index, loop in enumerate(face.loops):
            luv = loop[uv_layer]
            loops[(face.index, local_index)] = (
                bool(face.select),
                bool(loop.vert.select),
                bool(loop.edge.select),
                _optional_bool(face, "uv_select"),
                _optional_bool(loop, "uv_select_vert"),
                _optional_bool(loop, "uv_select_edge"),
                _optional_bool(luv, "select"),
                _optional_bool(luv, "select_edge"),
            )

    history = getattr(bm, "select_history", None)
    history_entries = ()
    history_active = None
    if history is not None:
        history_entries = tuple(
            key
            for key in (_selection_element_key(item) for item in history)
            if key is not None
        )
        history_active = _selection_element_key(getattr(history, "active", None))

    return {
        "faces": {face.index: bool(face.select) for face in bm.faces},
        "edges": {edge.index: bool(edge.select) for edge in bm.edges},
        "verts": {vert.index: bool(vert.select) for vert in bm.verts},
        "loops": loops,
        "history": history_entries,
        "history_active": history_active,
        "active_face": _selection_element_key(getattr(bm.faces, "active", None)),
    }


def _restore_uv_selection_state(bm, uv_layer, snapshot):
    """Restore a selection snapshot without touching any UV coordinate."""
    if not snapshot:
        return

    for face in bm.faces:
        face.select_set(False)
        for loop in face.loops:
            loop.vert.select_set(False)
            loop.edge.select_set(False)
            _set_optional_bool(face, "uv_select", False)
            _set_optional_bool(loop, "uv_select_vert", False)
            _set_optional_bool(loop, "uv_select_edge", False)
            luv = loop[uv_layer]
            _set_optional_bool(luv, "select", False)
            _set_optional_bool(luv, "select_edge", False)

    for (face_index, local_index), values in snapshot["loops"].items():
        if face_index < 0 or face_index >= len(bm.faces):
            continue
        face = bm.faces[face_index]
        loops = list(face.loops)
        if local_index < 0 or local_index >= len(loops):
            continue
        loop = loops[local_index]
        (
            face_select,
            vert_select,
            edge_select,
            face_uv_select,
            loop_uv_vert_select,
            loop_uv_edge_select,
            uv_select,
            uv_edge_select,
        ) = values
        face.select_set(face_select)
        loop.vert.select_set(vert_select)
        loop.edge.select_set(edge_select)
        _set_optional_bool(face, "uv_select", face_uv_select)
        _set_optional_bool(loop, "uv_select_vert", loop_uv_vert_select)
        _set_optional_bool(loop, "uv_select_edge", loop_uv_edge_select)
        luv = loop[uv_layer]
        _set_optional_bool(luv, "select", uv_select)
        _set_optional_bool(luv, "select_edge", uv_edge_select)

    for edge in bm.edges:
        edge.select_set(bool(snapshot["edges"].get(edge.index, False)))
    for vert in bm.verts:
        vert.select_set(bool(snapshot["verts"].get(vert.index, False)))

    history = getattr(bm, "select_history", None)
    if history is not None:
        history.clear()
        for key in snapshot.get("history", ()):
            element = _selection_element_from_key(bm, key)
            if element is not None:
                history.add(element)
        active = _selection_element_from_key(bm, snapshot.get("history_active"))
        if active is not None:
            try:
                history.active = active
            except (AttributeError, TypeError, RuntimeError):
                pass

    active_face = _selection_element_from_key(bm, snapshot.get("active_face"))
    if isinstance(active_face, bmesh.types.BMFace):
        try:
            bm.faces.active = active_face
        except (AttributeError, TypeError, RuntimeError):
            pass

    # History insertion may reselect linked elements; finish with the exact
    # per-loop flags captured before the refresh.
    for (face_index, local_index), values in snapshot["loops"].items():
        if face_index < 0 or face_index >= len(bm.faces):
            continue
        face = bm.faces[face_index]
        loops = list(face.loops)
        if local_index < 0 or local_index >= len(loops):
            continue
        loop = loops[local_index]
        (
            face_select,
            _vert_select,
            _edge_select,
            face_uv_select,
            loop_uv_vert_select,
            loop_uv_edge_select,
            uv_select,
            uv_edge_select,
        ) = values
        face.select_set(face_select)
        _set_optional_bool(face, "uv_select", face_uv_select)
        _set_optional_bool(loop, "uv_select_vert", loop_uv_vert_select)
        _set_optional_bool(loop, "uv_select_edge", loop_uv_edge_select)
        luv = loop[uv_layer]
        _set_optional_bool(luv, "select", uv_select)
        _set_optional_bool(luv, "select_edge", uv_edge_select)
    for edge in bm.edges:
        edge.select_set(bool(snapshot["edges"].get(edge.index, False)))
    for vert in bm.verts:
        vert.select_set(bool(snapshot["verts"].get(vert.index, False)))


def refresh_uv_selection_scope(context, bm, uv_layer):
    """Refresh an invalid UV-sync flag without changing UV data or scope.

    Blender's supported BMesh sync method establishes the validity bit.  The
    selection flags are restored immediately afterwards so this helper only
    repairs the stale validity boundary; callers still re-evaluate selection
    through the normal context predicate.
    """
    if not _context_uv_select_sync(context):
        return False
    if getattr(bm, "uv_select_sync_valid", None) is True:
        return False

    snapshot = _snapshot_uv_selection_state(bm, uv_layer)
    obj = getattr(context, "edit_object", None) or getattr(context, "object", None)
    try:
        sync_from_mesh = getattr(bm, "uv_select_sync_from_mesh", None)
        if not callable(sync_from_mesh):
            raise RuntimeError("Blender does not expose the UV selection sync API.")
        sync_from_mesh()
        _restore_uv_selection_state(bm, uv_layer, snapshot)
        if obj is None or getattr(obj, "type", None) != "MESH":
            raise RuntimeError("The edit mesh context disappeared during refresh.")
        bmesh.update_edit_mesh(
            obj.data,
            loop_triangles=False,
            destructive=False,
        )
        if getattr(bm, "uv_select_sync_valid", None) is not True:
            raise RuntimeError("Blender did not mark UV selection sync valid.")
    except Exception as exc:
        try:
            _restore_uv_selection_state(bm, uv_layer, snapshot)
            bm.uv_select_sync_valid = False
        except (AttributeError, TypeError, RuntimeError):
            pass
        raise RuntimeError(
            "UV Select Sync could not be refreshed safely; selected-only "
            "Pack/Center was cancelled without changing UVs."
        ) from exc
    return True


def _uv_loop_edge_selected(loop, uv_layer):
    """Read an individual loop's UV-edge selection across Blender API generations."""
    value = getattr(loop, "uv_select_edge", None)
    if value is not None:
        return bool(value)
    luv = loop[uv_layer]
    return bool(getattr(luv, "select_edge", False))


def _uv_loop_vertex_selected(loop, uv_layer):
    """Read an individual loop's UV-vertex selection across Blender API generations."""
    value = getattr(loop, "uv_select_vert", None)
    if value is not None:
        return bool(value)
    luv = loop[uv_layer]
    return bool(getattr(luv, "select", False))


def _face_uv_selected_for_context(context, bm, face, uv_layer):
    """Mirror Blender's UV Editor selection predicate without mesh-island widening."""
    if face.hide:
        return False

    use_sync = _context_uv_select_sync(context)
    sync_valid = getattr(bm, "uv_select_sync_valid", None)
    if use_sync:
        # A synced BMesh that cannot represent UV selection independently is
        # rejected by validate_uv_selection_scope before this predicate runs.
        if sync_valid is not True:
            return False
        face_uv_selected = getattr(face, "uv_select", None)
        if face_uv_selected is not None and bool(face_uv_selected):
            return True
        return any(_uv_loop_edge_selected(loop, uv_layer) for loop in face.loops)

    # In UV Editor mode without sync, Blender requires the mesh face to be
    # selected and then consults the face/loop UV selection flags. Mesh edge or
    # vertex flags alone are deliberately not accepted here.
    if not face.select:
        return False
    face_uv_selected = getattr(face, "uv_select", None)
    if face_uv_selected is not None:
        return bool(face_uv_selected) or any(
            _uv_loop_edge_selected(loop, uv_layer) for loop in face.loops
        )
    return any(
        _uv_loop_vertex_selected(loop, uv_layer)
        or _uv_loop_edge_selected(loop, uv_layer)
        for loop in face.loops
    )


def validate_uv_selection_scope(
    context,
    bm,
    uv_layer,
    *,
    refresh_invalid_sync=False,
):
    """Validate UV scope, optionally repairing Blender's stale sync bit."""
    if _context_uv_select_sync(context):
        sync_valid = getattr(bm, "uv_select_sync_valid", None)
        if sync_valid is not True:
            if refresh_invalid_sync:
                refresh_uv_selection_scope(context, bm, uv_layer)
                sync_valid = getattr(bm, "uv_select_sync_valid", None)
            if sync_valid is not True:
                raise RuntimeError(
                    "UV Select Sync is not valid for this mesh; disable UV Sync "
                    "to pack or center only the selected UV island."
                )

    has_uv_face_state = any(
        getattr(face, "uv_select", None) is not None
        for face in bm.faces
    )
    has_uv_loop_state = any(
        getattr(loop, "uv_select_edge", None) is not None
        or getattr(loop, "uv_select_vert", None) is not None
        or getattr(loop[uv_layer], "select", None) is not None
        or getattr(loop[uv_layer], "select_edge", None) is not None
        for face in bm.faces
        for loop in face.loops
    )
    if not has_uv_face_state and not has_uv_loop_state:
        raise RuntimeError(
            "Blender did not expose independent UV selection state; "
            "selected-only packing is unavailable for this mesh."
        )


def get_selected_uv_islands_for_context(
    context,
    bm,
    uv_layer,
    *,
    refresh_invalid_sync=False,
):
    """Return islands selected by UV-loop state, never by mesh-only flags."""
    validate_uv_selection_scope(
        context,
        bm,
        uv_layer,
        refresh_invalid_sync=refresh_invalid_sync,
    )
    selected_faces = {
        face
        for face in bm.faces
        if _face_uv_selected_for_context(context, bm, face, uv_layer)
    }
    return [
        island
        for island in _build_all_uv_islands(bm, uv_layer)
        if any(loop.face in selected_faces for loop in island)
    ]


def _face_uv_selected_for_symmetry(context, bm, face, uv_layer):
    """Read one visible UV face without widening through mesh edge/vertex flags.

    Symmetry operates on selected *regions*, not UV islands.  In synced mode a
    valid Blender sync state makes the mesh face selection the authoritative
    scope.  Without sync, the mesh face must still be selected, but only UV
    face/loop flags are allowed to mark it selected; mesh-only edge/vertex
    flags are deliberately ignored because they are shared by neighbouring
    faces and caused the original real-mesh leakage.
    """
    if face.hide or not face.select:
        return False

    if _context_uv_select_sync(context):
        if getattr(bm, "uv_select_sync_valid", None) is not True:
            return False
        return True

    face_uv_selected = getattr(face, "uv_select", None)
    if face_uv_selected is True:
        return True

    for loop in face.loops:
        luv = loop[uv_layer]
        if bool(
            getattr(luv, "select", False)
            or getattr(luv, "select_edge", False)
            or _uv_loop_vertex_selected(loop, uv_layer)
            or _uv_loop_edge_selected(loop, uv_layer)
        ):
            return True
    return False


def get_selected_uv_faces_for_symmetry(context, bm, uv_layer):
    """Return selected UV-editor faces for the topology-region Symmetry path.

    This is intentionally separate from the legacy island predicate and from
    Pack/Center's helper.  It preserves the latter APIs while giving Symmetry
    an operation-specific, fail-closed selection contract.
    """
    try:
        validate_uv_selection_scope(context, bm, uv_layer)
    except RuntimeError as exc:
        message = str(exc)
        if "UV Select Sync" in message:
            raise RuntimeError(
                "UV Select Sync is not valid for Symmetry; disable UV Sync "
                "before selecting two visible regions."
            ) from exc
        raise RuntimeError(
            "Blender did not expose independent UV selection state for "
            "Symmetry; select the regions in the UV Editor and try again."
        ) from exc

    faces = [
        face
        for face in bm.faces
        if _face_uv_selected_for_symmetry(context, bm, face, uv_layer)
    ]
    return tuple(sorted(faces, key=lambda face: int(face.index)))


def get_selected_uv_regions_for_context(context, bm, uv_layer):
    """Return selected UV regions as topology-connected face tuples.

    UV seams intentionally do not split a region here: two selected faces
    sharing a mesh edge remain in one region even when their UV coordinates are
    disconnected.  Disconnected mesh components are never merged by spatial
    proximity, so an ambiguous selection remains visible to the caller as
    multiple regions and can be cancelled safely.
    """
    selected_faces = get_selected_uv_faces_for_symmetry(context, bm, uv_layer)
    if not selected_faces:
        return ()

    selected_set = set(selected_faces)
    adjacency = {face: set() for face in selected_faces}
    for face in selected_faces:
        for edge in face.edges:
            for linked in edge.link_faces:
                if linked is face or linked.hide or linked not in selected_set:
                    continue
                adjacency[face].add(linked)
                adjacency[linked].add(face)

    regions = []
    visited = set()
    for start in selected_faces:
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        component = []
        while stack:
            face = stack.pop()
            component.append(face)
            for linked in sorted(adjacency[face], key=lambda item: int(item.index)):
                if linked not in visited:
                    visited.add(linked)
                    stack.append(linked)
        regions.append(tuple(sorted(component, key=lambda item: int(item.index))))

    return tuple(regions)


def _selection_history_active_element(bm):
    """Return the active/last selection-history element without mutating it."""
    history = getattr(bm, "select_history", None)
    if history is None:
        return None

    active = getattr(history, "active", None)
    if active is not None:
        return active

    # A few Blender/API-shaped doubles expose the history as an iterable but
    # do not provide ``active``.  The final history entry is the equivalent
    # last-selected element in that case.
    try:
        entries = list(history)
    except (TypeError, RuntimeError):
        return None
    return entries[-1] if entries else None


def _selected_region_indices_for_element(element, region_by_face):
    """Return selected topology regions touched by one history/active element."""
    if element is None:
        return ()
    if isinstance(element, bmesh.types.BMFace):
        linked_faces = (element,)
    elif isinstance(element, (bmesh.types.BMEdge, bmesh.types.BMVert)):
        linked_faces = getattr(element, "link_faces", ())
    else:
        return ()

    return tuple(
        sorted(
            {
                region_index
                for face in linked_faces
                if face in region_by_face
                for region_index in (region_by_face[face],)
            }
        )
    )


def resolve_selected_region_target(bm, regions):
    """Resolve the target region with selection-history precedence.

    The active/last-selected history element is authoritative when it touches
    exactly one selected topology region.  ``bm.faces.active`` is consulted
    only when history has no valid, unambiguous selected-region target.  The
    return value is ``(source, region_index)`` or ``(None, None)`` when
    neither source resolves a selected region.
    """
    region_by_face = {
        face: region_index
        for region_index, region in enumerate(regions)
        for face in region
    }

    history_indices = _selected_region_indices_for_element(
        _selection_history_active_element(bm),
        region_by_face,
    )
    if len(history_indices) == 1:
        return "history", history_indices[0]

    active_indices = _selected_region_indices_for_element(
        getattr(bm.faces, "active", None),
        region_by_face,
    )
    if len(active_indices) == 1:
        return "active", active_indices[0]

    return None, None


def get_selected_uv_loops(bm, uv_layer):
    return [
        loop
        for face in bm.faces
        if not face.hide
        for loop in face.loops
        if _loop_selected(loop, uv_layer)
    ]


def _uv_close(a, b, epsilon=EPSILON):
    return abs(a.x - b.x) <= epsilon and abs(a.y - b.y) <= epsilon


def _edge_uvs_match(loop_a, loop_b, uv_layer):
    a0 = loop_a[uv_layer].uv
    a1 = loop_a.link_loop_next[uv_layer].uv
    b0 = loop_b[uv_layer].uv
    b1 = loop_b.link_loop_next[uv_layer].uv
    return (_uv_close(a0, b1) and _uv_close(a1, b0)) or (
        _uv_close(a0, b0) and _uv_close(a1, b1)
    )


def _build_all_uv_islands(bm, uv_layer):
    faces = [face for face in bm.faces if not face.hide]
    face_set = set(faces)
    adjacency = {face: set() for face in faces}
    edge_map = {}

    for face in faces:
        for loop in face.loops:
            edge_map.setdefault(loop.edge, []).append(loop)

    for loops in edge_map.values():
        count = len(loops)
        for i in range(count):
            for j in range(i + 1, count):
                loop_a = loops[i]
                loop_b = loops[j]
                if loop_a.face is loop_b.face:
                    continue
                if loop_a.face not in face_set or loop_b.face not in face_set:
                    continue
                if _edge_uvs_match(loop_a, loop_b, uv_layer):
                    adjacency[loop_a.face].add(loop_b.face)
                    adjacency[loop_b.face].add(loop_a.face)

    islands = []
    visited = set()
    for start in faces:
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        island_faces = []
        while stack:
            face = stack.pop()
            island_faces.append(face)
            for linked in adjacency[face]:
                if linked not in visited:
                    visited.add(linked)
                    stack.append(linked)
        loops = [loop for face in island_faces for loop in face.loops]
        if loops:
            islands.append(loops)
    return islands


def get_uv_islands(bm, uv_layer, selected_only=False):
    islands = _build_all_uv_islands(bm, uv_layer)
    if not selected_only:
        return islands
    return [island for island in islands if any(_loop_selected(loop, uv_layer) for loop in island)]


def get_selected_uv_islands(bm, uv_layer):
    return get_uv_islands(bm, uv_layer, selected_only=True)


def _active_face_from_history(bm):
    active_face = getattr(bm.faces, "active", None)
    if isinstance(active_face, bmesh.types.BMFace) and not active_face.hide:
        return active_face

    history = getattr(bm, "select_history", None)
    active = getattr(history, "active", None) if history else None
    if isinstance(active, bmesh.types.BMFace):
        return active
    if isinstance(active, bmesh.types.BMEdge):
        selected = [face for face in active.link_faces if face.select and not face.hide]
        if selected:
            return selected[-1]
        linked = [face for face in active.link_faces if not face.hide]
        return linked[-1] if linked else None
    if isinstance(active, bmesh.types.BMVert):
        selected = [face for face in active.link_faces if face.select and not face.hide]
        if selected:
            return selected[-1]
        linked = [face for face in active.link_faces if not face.hide]
        return linked[-1] if linked else None
    return None


def get_active_uv_island(bm, uv_layer):
    active_face = _active_face_from_history(bm)
    islands = get_selected_uv_islands(bm, uv_layer)
    if not islands:
        islands = get_uv_islands(bm, uv_layer, selected_only=False)

    if active_face is None and len(islands) == 1:
        return islands[0]
    if active_face is None:
        raise RuntimeError(
            "No active island found. Select the reference island last or make one face active."
        )

    for island in islands:
        if any(loop.face is active_face for loop in island):
            return island

    raise RuntimeError(
        "No active island found. Select the reference island last or make one face active."
    )


def get_island_bounds(island, uv_layer):
    if not island:
        return (0.0, 0.0, 0.0, 0.0)
    us = [loop[uv_layer].uv.x for loop in island]
    vs = [loop[uv_layer].uv.y for loop in island]
    return (min(us), max(us), min(vs), max(vs))


def get_island_center(island, uv_layer):
    min_u, max_u, min_v, max_v = get_island_bounds(island, uv_layer)
    return Vector(((min_u + max_u) * 0.5, (min_v + max_v) * 0.5))


def _polygon_area(points):
    if len(points) < 3:
        return 0.0
    total = 0.0
    for index, point in enumerate(points):
        nxt = points[(index + 1) % len(points)]
        total += point.x * nxt.y - nxt.x * point.y
    return abs(total) * 0.5


def get_island_area(island, uv_layer):
    faces = []
    seen = set()
    for loop in island:
        if loop.face not in seen:
            seen.add(loop.face)
            faces.append(loop.face)
    area = 0.0
    for face in faces:
        points = [loop[uv_layer].uv for loop in face.loops]
        area += _polygon_area(points)
    return area


def get_island_main_axis_farthest_points(island, uv_layer):
    points = []
    seen = set()
    for loop in island:
        uv = loop[uv_layer].uv
        key = (round(uv.x, 7), round(uv.y, 7))
        if key not in seen:
            seen.add(key)
            points.append(uv.copy())

    if len(points) < 2:
        center = get_island_center(island, uv_layer)
        return (center.copy(), center.copy(), 0.0, 0.0)

    best_a = points[0]
    best_b = points[1]
    best_len_sq = -1.0
    for i, point_a in enumerate(points):
        for point_b in points[i + 1 :]:
            length_sq = (point_b - point_a).length_squared
            if length_sq > best_len_sq:
                best_len_sq = length_sq
                best_a = point_a
                best_b = point_b

    vector = best_b - best_a
    length = math.sqrt(max(best_len_sq, 0.0))
    angle = math.atan2(vector.y, vector.x) if length > EPSILON else 0.0
    return (best_a.copy(), best_b.copy(), length, angle)


def island_face_count(island):
    return len({loop.face for loop in island})


def island_faces(island):
    seen = set()
    result = []
    for loop in island:
        if loop.face not in seen:
            seen.add(loop.face)
            result.append(loop.face)
    return result
