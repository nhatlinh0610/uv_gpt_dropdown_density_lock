"""Create the dedicated deterministic Blender fixture for AS-02 toggle checks."""

from __future__ import annotations

import math
from pathlib import Path
import sys


import bmesh
import bpy
from mathutils import Vector


def _arg_value(name, default):
    if "--" not in sys.argv:
        return default
    args = sys.argv[sys.argv.index("--") + 1 :]
    try:
        index = args.index(name)
    except ValueError:
        return default
    return args[index + 1] if index + 1 < len(args) else default


OUTPUT_PATH = Path(
    _arg_value(
        "--output",
        str(Path(__file__).resolve().parents[2] / ".test_runtime" / "as_02_focus.blend"),
    )
).resolve()

BASE_POINTS = (
    (0.0, 0.0),
    (2.0, 0.0),
    (2.8, 1.0),
    (1.4, 2.2),
    (0.0, 1.4),
)


def transformed(points, angle=0.0, scale=1.0, offset=(0.0, 0.0), reflection=False):
    cosine = math.cos(angle)
    sine = math.sin(angle)
    result = []
    for x, y in points:
        if reflection:
            x = -x
        result.append(
            (
                offset[0] + scale * (x * cosine - y * sine),
                offset[1] + scale * (x * sine + y * cosine),
            )
        )
    return tuple(result)


def main():
    bpy.ops.object.mode_set(mode="OBJECT") if bpy.context.object and bpy.context.object.mode != "OBJECT" else None
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)

    islands = (
        BASE_POINTS,
        transformed(BASE_POINTS, angle=0.37, scale=1.7, offset=(4.0, -2.0)),
        transformed(BASE_POINTS, angle=-0.23, scale=1.0, offset=(9.0, 3.0), reflection=True),
        transformed(BASE_POINTS, angle=0.19, scale=1.0, offset=(14.0, -1.5)),
        ((20.0, 0.0), (22.0, 0.0), (21.0, 1.5)),
    )
    vertices = []
    faces = []
    for points in islands:
        start = len(vertices)
        vertices.extend((float(x), float(y), 0.0) for x, y in points)
        faces.append(tuple(range(start, len(vertices))))

    mesh = bpy.data.meshes.new("AS_02_FocusMesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv_layer = mesh.uv_layers.new(name="UVMap.001")
    for polygon, points in zip(mesh.polygons, islands):
        for loop_index, point in zip(polygon.loop_indices, points):
            uv_layer.data[loop_index].uv = Vector(point)
    mesh.uv_layers.active_index = 0

    obj = bpy.data.objects.new("AS_02_Focus", mesh)
    bpy.context.collection.objects.link(obj)
    bpy.context.view_layer.objects.active = obj
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bm = bmesh.from_edit_mesh(mesh)
    bm.faces.ensure_lookup_table()
    bm.faces.index_update()
    bm.faces.active = bm.faces[0]
    bm_uv_layer = bm.loops.layers.uv.get(uv_layer.name)
    if bm_uv_layer is None:
        raise RuntimeError("Focused fixture UV layer was not created")
    for face in bm.faces:
        selected = face.index < 4
        face.select_set(selected)
        for edge in face.edges:
            edge.select_set(selected)
        for vert in face.verts:
            vert.select_set(selected)
    bmesh.update_edit_mesh(mesh, loop_triangles=False, destructive=False)
    bpy.context.scene.tool_settings.use_uv_select_sync = True
    bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT_PATH))
    print("AS-02 focused fixture written: %s" % OUTPUT_PATH, flush=True)


if __name__ == "__main__":
    main()
