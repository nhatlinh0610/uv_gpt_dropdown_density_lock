"""Read-only PERF-P07 probe for the resumable island state machine."""

import sys
import time

import bmesh
import bpy


PROJECT_ROOT = sys.argv[sys.argv.index("--project-root") + 1]
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import uv_gpt.stack_tools as stack_tools


obj = bpy.data.objects.get("PROExact")
if obj is None:
    raise RuntimeError("PROExact missing")
bm = bmesh.new()
bm.from_mesh(obj.data)
uv_layer = bm.loops.layers.uv.get("UVMap.001")
state = stack_tools._ProIslandEnumerationState(bm, uv_layer)
for index in range(20):
    result, operations = state.advance(
        operation_budget=1024,
        deadline=time.perf_counter() + 0.01,
    )
    print(
        "P07_ENUM_PROBE slice=%d phase=%s ops=%d total=%d done=%s islands=%s"
        % (
            index + 1,
            state.phase,
            operations,
            state.enum_primitive_operations,
            state.done,
            len(result) if result is not None else -1,
        )
    )
    if state.done:
        break
bm.free()
