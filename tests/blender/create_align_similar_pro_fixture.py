"""Create a disposable synthetic PRO-02B Blender fixture.

This script writes only the dedicated synthetic file requested by PRO-02B; it
never opens or writes the locked cc.blend fixture.
"""

from __future__ import annotations

import math
from pathlib import Path
import sys


sys.dont_write_bytecode = True

import bpy


def _arg_value(name, default):
    if "--" not in sys.argv:
        return default
    args = sys.argv[sys.argv.index("--") + 1 :]
    try:
        index = args.index(name)
    except ValueError:
        return default
    return args[index + 1] if index + 1 < len(args) else default


OUTPUT = Path(
    _arg_value(
        "--output",
        str(Path(__file__).resolve().parents[2] / "benchmarks" / "pro_02b_dedicated_fixture.blend"),
    )
).resolve()


def _add_object(name, patches):
    vertices = []
    faces = []
    face_uvs = []
    for patch in patches:
        offset = len(vertices)
        vertices.extend(tuple(vertex) for vertex in patch["vertices"])
        for face, uv_face in zip(patch["faces"], patch["uv_faces"]):
            faces.append(tuple(offset + index for index in face))
            face_uvs.append(tuple(tuple(point) for point in uv_face))
    mesh = bpy.data.meshes.new(name + "Mesh")
    mesh.from_pydata(vertices, [], faces)
    mesh.update()
    uv_layer = mesh.uv_layers.new(name="UVMap.001")
    for polygon, uv_face in zip(mesh.polygons, face_uvs):
        for loop_index, point in zip(polygon.loop_indices, uv_face):
            uv_layer.data[loop_index].uv = point
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.collection.objects.link(obj)
    return obj


def _quad_patch(world_origin, world_size, uv_origin=(0.0, 0.0), uv_mode="normal"):
    x, y = world_origin
    size = float(world_size)
    u, v = uv_origin
    vertices = (
        (x, y, 0.0),
        (x + size, y, 0.0),
        (x + size, y + size, 0.0),
        (x, y + size, 0.0),
    )
    if uv_mode == "cyclic":
        uv_face = ((u + 1.0, v), (u + 1.0, v + 1.0), (u, v + 1.0), (u, v))
    elif uv_mode == "reflection":
        uv_face = ((u, v), (u - 1.0, v), (u - 1.0, v + 1.0), (u, v + 1.0))
    else:
        uv_face = ((u, v), (u + 1.0, v), (u + 1.0, v + 1.0), (u, v + 1.0))
    return {
        "vertices": vertices,
        "faces": ((0, 1, 2, 3),),
        "uv_faces": (uv_face,),
    }


def _annulus_patch(world_origin, world_size, uv_origin=(0.0, 0.0)):
    x, y = world_origin
    size = float(world_size)
    u, v = uv_origin
    outer = ((0.0, 0.0), (size, 0.0), (size, size), (0.0, size))
    inner_size = size * 0.45
    inset = size * 0.275
    inner = (
        (inset, inset),
        (inset + inner_size, inset),
        (inset + inner_size, inset + inner_size),
        (inset, inset + inner_size),
    )
    world = tuple((x + px, y + py, 0.0) for px, py in outer + inner)
    outer_uv = ((u, v), (u + 1.0, v), (u + 1.0, v + 1.0), (u, v + 1.0))
    inner_uv = (
        (u + 0.3, v + 0.3),
        (u + 0.7, v + 0.3),
        (u + 0.7, v + 0.7),
        (u + 0.3, v + 0.7),
    )
    faces = (
        (0, 1, 5, 4),
        (1, 2, 6, 5),
        (2, 3, 7, 6),
        (3, 0, 4, 7),
    )
    uv_faces = tuple(
        (
            outer_uv[index],
            outer_uv[(index + 1) % 4],
            inner_uv[(index + 1) % 4],
            inner_uv[index],
        )
        for index in range(4)
    )
    return {"vertices": world, "faces": faces, "uv_faces": uv_faces}


def _grid_patch(world_origin, world_size, uv_origin=(0.0, 0.0)):
    x, y = world_origin
    size = float(world_size) / 2.0
    u, v = uv_origin
    vertices = []
    for row in range(3):
        for column in range(3):
            vertices.append((x + column * size, y + row * size, 0.0))
    faces = []
    uv_faces = []
    for row in range(2):
        for column in range(2):
            a = row * 3 + column
            faces.append((a, a + 1, a + 4, a + 3))
            uv_faces.append(
                (
                    (u + column, v + row),
                    (u + column + 1.0, v + row),
                    (u + column + 1.0, v + row + 1.0),
                    (u + column, v + row + 1.0),
                )
            )
    return {"vertices": vertices, "faces": tuple(faces), "uv_faces": tuple(uv_faces)}


def _seam_patch(world_origin, world_size, uv_origin=(0.0, 0.0)):
    """Two fan sheets joined by a bridge, with a split interior vertex."""

    x, y = world_origin
    size = float(world_size)
    u, v = uv_origin
    vertices = (
        (x + 0.0 * size, y + 0.0 * size, 0.0),
        (x - 1.0 * size, y - 1.0 * size, 0.0),
        (x + 1.0 * size, y - 1.0 * size, 0.0),
        (x + 1.0 * size, y + 1.0 * size, 0.0),
        (x - 1.0 * size, y + 1.0 * size, 0.0),
        (x + 3.0 * size, y - 1.0 * size, 0.0),
        (x + 5.0 * size, y - 1.0 * size, 0.0),
        (x + 5.0 * size, y + 1.0 * size, 0.0),
        (x + 3.0 * size, y + 1.0 * size, 0.0),
    )
    faces = (
        (0, 1, 2),
        (0, 2, 3),
        (0, 3, 4),
        (0, 4, 1),
        (2, 3, 7, 6),
        (0, 5, 6),
        (0, 6, 7),
        (0, 7, 8),
        (0, 8, 5),
    )
    face_uvs = (
        (
            (u + 0.0, v + 0.0),
            (u - 1.0, v - 1.0),
            (u + 1.0, v - 1.0),
        ),
        (
            (u + 0.0, v + 0.0),
            (u + 1.0, v - 1.0),
            (u + 1.0, v + 1.0),
        ),
        (
            (u + 0.0, v + 0.0),
            (u + 1.0, v + 1.0),
            (u - 1.0, v + 1.0),
        ),
        (
            (u + 0.0, v + 0.0),
            (u - 1.0, v + 1.0),
            (u - 1.0, v - 1.0),
        ),
        (
            (u + 1.0, v - 1.0),
            (u + 1.0, v + 1.0),
            (u + 5.0, v + 1.0),
            (u + 5.0, v - 1.0),
        ),
        (
            (u + 4.0, v + 0.0),
            (u + 3.0, v - 1.0),
            (u + 5.0, v - 1.0),
        ),
        (
            (u + 4.0, v + 0.0),
            (u + 5.0, v - 1.0),
            (u + 5.0, v + 1.0),
        ),
        (
            (u + 4.0, v + 0.0),
            (u + 5.0, v + 1.0),
            (u + 3.0, v + 1.0),
        ),
        (
            (u + 4.0, v + 0.0),
            (u + 3.0, v + 1.0),
            (u + 3.0, v - 1.0),
        ),
    )
    return {
        "vertices": vertices,
        "faces": faces,
        "uv_faces": face_uvs,
    }


def build():
    bpy.ops.object.mode_set(mode="OBJECT") if bpy.context.object and bpy.context.object.mode != "OBJECT" else None
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for mesh in list(bpy.data.meshes):
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)

    _add_object(
        "PROExact",
        (
            _quad_patch((0.0, 0.0), 1.0, (0.0, 0.0)),
            _quad_patch((3.0, 0.0), 2.0, (3.0, 0.0)),
            _quad_patch((6.0, 0.0), 1.5, (6.0, 0.0), "cyclic"),
            _quad_patch((9.0, 0.0), 1.25, (9.0, 0.0), "reflection"),
            _quad_patch((12.0, 0.0), 1.0, (12.0, 0.0)),
        ),
    )
    _add_object(
        "PROHole",
        (
            _annulus_patch((0.0, 0.0), 4.0, (0.0, 0.0)),
            _annulus_patch((7.0, 0.0), 5.0, (3.0, 0.0)),
        ),
    )
    _add_object(
        "PROInterior",
        (
            _grid_patch((0.0, 0.0), 4.0, (0.0, 0.0)),
            _grid_patch((7.0, 0.0), 5.0, (3.0, 0.0)),
        ),
    )
    _add_object(
        "PROSeam",
        (
            _seam_patch((0.0, 0.0), 1.0, (0.0, 0.0)),
            _seam_patch((5.0, 0.0), 2.0, (4.0, 0.0)),
        ),
    )
    _add_object(
        "PRONonIso",
        (
            _quad_patch((0.0, 0.0), 1.0, (0.0, 0.0)),
            {
                "vertices": ((3.0, 0.0, 0.0), (4.0, 0.0, 0.0), (4.0, 1.0, 0.0), (3.0, 1.0, 0.0)),
                "faces": ((0, 1, 2), (0, 2, 3)),
                "uv_faces": (
                    ((3.0, 0.0), (4.0, 0.0), (4.0, 1.0)),
                    ((3.0, 0.0), (4.0, 1.0), (3.0, 1.0)),
                ),
            },
        ),
    )
    bpy.ops.wm.save_as_mainfile(filepath=str(OUTPUT))
    print("Created dedicated PRO-02B fixture: %s" % OUTPUT)


if __name__ == "__main__":
    build()
