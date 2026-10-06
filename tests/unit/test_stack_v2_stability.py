"""Focused pure lifecycle checks for the detached Fast/Pro V2 routes."""

from pathlib import Path
import pickle
import sys
from types import SimpleNamespace
import types
import tempfile
import unittest
from unittest.mock import patch

UNIT_ROOT = Path(__file__).resolve().parent
if str(UNIT_ROOT) not in sys.path:
    sys.path.insert(0, str(UNIT_ROOT))
# The shared pure-helper loader predates Blender's ``blf`` dependency in the
# overlay module.  Detached V2 tests only need the stack module itself.
sys.modules.setdefault("uv_gpt.overlay", types.ModuleType("uv_gpt.overlay"))
from test_align_similar_selected import _load_stack_tools_for_pure_helpers


STACK = _load_stack_tools_for_pure_helpers()


class _UVValue:
    def __init__(self, u, v):
        self.uv = SimpleNamespace(x=float(u), y=float(v))


class _Loop:
    def __init__(self, vertex, edge, u, v):
        self.vert = SimpleNamespace(index=int(vertex))
        self.edge = SimpleNamespace(index=int(edge))
        self._value = _UVValue(u, v)

    def __getitem__(self, _layer):
        return self._value


class _Face:
    def __init__(self, loops):
        self.loops = tuple(loops)


class _ElementList(list):
    def ensure_lookup_table(self):
        return None

    def index_update(self):
        return None


class _BMesh:
    def __init__(self, faces, uv_layer):
        self.faces = _ElementList(faces)
        self.edges = _ElementList(range(sum(len(face.loops) for face in faces)))
        self.verts = _ElementList(range(sum(len(face.loops) for face in faces)))
        self.loops = SimpleNamespace(
            layers=SimpleNamespace(
                uv=SimpleNamespace(get=lambda _name: uv_layer),
            ),
        )


class _UVLayers(list):
    def __init__(self, *layers):
        super().__init__(layers)
        self.active = layers[0] if layers else None


class _RNA:
    def __init__(self, pointer, name):
        self.pointer = int(pointer)
        self.name = str(name)

    def as_pointer(self):
        return self.pointer


class _Object:
    type = "MESH"

    def __init__(self, pointer=11, data_pointer=22, mode="EDIT"):
        self.name = "Source"
        self.mode = mode
        self.data = _RNA(data_pointer, "SourceMesh")
        self.data.uv_layers = _UVLayers(_RNA(33, "UVMap"))
        self.pointer = int(pointer)

    def as_pointer(self):
        return self.pointer


class _PasteVector:
    def __init__(self, x, y):
        self.x = float(x)
        self.y = float(y)

    def copy(self):
        return _PasteVector(self.x, self.y)

    def __sub__(self, other):
        return _PasteVector(self.x - other.x, self.y - other.y)

    def __iadd__(self, other):
        self.x += other.x
        self.y += other.y
        return self


class _PasteLoop:
    def __init__(self, face_index, x=1.0, y=2.0):
        self.face = SimpleNamespace(index=int(face_index))
        self._uv = SimpleNamespace(uv=_PasteVector(x, y))

    def __getitem__(self, _layer):
        return self._uv


class _PasteFace:
    def __init__(self, index, loops):
        self.index = int(index)
        self.loops = tuple(loops)


class _PasteBMesh:
    def __init__(self, faces):
        self.faces = _ElementList(faces)


class _RunningProcess:
    pid = 707

    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _fake_bpy(obj, undo_calls=None):
    if undo_calls is None:
        undo_calls = []
    return SimpleNamespace(
        data=SimpleNamespace(objects={obj.name: obj}),
        context=SimpleNamespace(edit_object=obj, object=obj, workspace=None),
        ops=SimpleNamespace(
            ed=SimpleNamespace(undo_push=lambda **_kwargs: undo_calls.append("undo")),
        ),
    )


def _identity_fields(obj):
    uv_layer = obj.data.uv_layers.active
    return {
        "object_name": obj.name,
        "data_name": obj.data.name,
        "uv_map_name": uv_layer.name,
        "_object_ref": obj,
        "_data_ref": obj.data,
        "object_pointer": obj.as_pointer(),
        "data_pointer": obj.data.as_pointer(),
    }


def _triangle_face(offset=0, u_offset=0.0):
    return _Face(
        (
            _Loop(offset + 0, offset + 0, u_offset + 0.0, 0.0),
            _Loop(offset + 1, offset + 1, u_offset + 1.0, 0.0),
            _Loop(offset + 2, offset + 2, u_offset + 0.0, 1.0),
        )
    )


class StackV2StabilityTests(unittest.TestCase):
    def _paste_fixture(self, *, sync, selected):
        uv_layer = object()
        loop = _PasteLoop(0)
        face = _PasteFace(0, (loop,))
        bm = _PasteBMesh((face,))
        obj = SimpleNamespace(data=SimpleNamespace())
        context = SimpleNamespace(
            scene=SimpleNamespace(
                tool_settings=SimpleNamespace(use_uv_select_sync=bool(sync)),
            ),
        )
        selection_calls = []
        paste_calls = []
        update_calls = []
        reports = []
        selected_island = (loop,)

        def select_for_context(received_context, received_bm, received_layer):
            selection_calls.append(
                (received_context, received_bm, received_layer),
            )
            return [selected_island] if selected else []

        island_tools = SimpleNamespace(
            get_active_bmesh=lambda _context: bm,
            get_active_uv_layer=lambda _bm, _obj: uv_layer,
            get_selected_uv_islands=lambda *_args: (_ for _ in ()).throw(
                AssertionError("legacy mesh-only selection helper used"),
            ),
            get_selected_uv_islands_for_context=select_for_context,
            get_island_center=lambda _island, _layer: _PasteVector(1.0, 2.0),
        )
        uv_utils = SimpleNamespace(
            ensure_destructive_ready=lambda _context: None,
            get_active_mesh_object=lambda _context: obj,
            run_uv_paste=lambda _context: paste_calls.append("paste"),
            get_loops_center=lambda _loops, _layer: _PasteVector(1.0, 2.0),
        )
        fake_bmesh = SimpleNamespace(
            update_edit_mesh=lambda *_args, **_kwargs: update_calls.append("update"),
        )
        operator = STACK.UVGPT_OT_paste_keep_position()
        operator.report = lambda levels, message: reports.append((levels, message))
        return (
            operator,
            context,
            island_tools,
            uv_utils,
            fake_bmesh,
            selection_calls,
            paste_calls,
            update_calls,
            reports,
        )

    def test_paste_sync_off_uses_context_subset_for_visible_mesh(self):
        fixture = self._paste_fixture(sync=False, selected=True)
        (
            operator,
            context,
            island_tools,
            uv_utils,
            fake_bmesh,
            selection_calls,
            paste_calls,
            update_calls,
            reports,
        ) = fixture
        with patch.object(STACK, "island_tools", island_tools), \
             patch.object(STACK, "uv_utils", uv_utils), \
             patch.object(STACK, "bmesh", fake_bmesh):
            result = operator.execute(context)
        self.assertEqual(result, {"FINISHED"})
        self.assertEqual(len(selection_calls), 1)
        self.assertIs(selection_calls[0][0], context)
        self.assertFalse(context.scene.tool_settings.use_uv_select_sync)
        self.assertEqual(paste_calls, ["paste"])
        self.assertEqual(update_calls, ["update"])
        self.assertIn("restored 1 island center(s)", reports[-1][1])

    def test_paste_sync_on_preserves_context_selection_and_native_paste(self):
        fixture = self._paste_fixture(sync=True, selected=True)
        (
            operator,
            context,
            island_tools,
            uv_utils,
            fake_bmesh,
            selection_calls,
            paste_calls,
            update_calls,
            reports,
        ) = fixture
        with patch.object(STACK, "island_tools", island_tools), \
             patch.object(STACK, "uv_utils", uv_utils), \
             patch.object(STACK, "bmesh", fake_bmesh):
            result = operator.execute(context)
        self.assertEqual(result, {"FINISHED"})
        self.assertTrue(selection_calls[0][0].scene.tool_settings.use_uv_select_sync)
        self.assertEqual(paste_calls, ["paste"])
        self.assertEqual(update_calls, ["update"])
        self.assertIn("restored 1 island center(s)", reports[-1][1])

    def test_paste_empty_context_selection_cancels_before_native_paste(self):
        fixture = self._paste_fixture(sync=False, selected=False)
        (
            operator,
            context,
            island_tools,
            uv_utils,
            fake_bmesh,
            selection_calls,
            paste_calls,
            update_calls,
            reports,
        ) = fixture
        with patch.object(STACK, "island_tools", island_tools), \
             patch.object(STACK, "uv_utils", uv_utils), \
             patch.object(STACK, "bmesh", fake_bmesh):
            result = operator.execute(context)
        self.assertEqual(result, {"CANCELLED"})
        self.assertEqual(len(selection_calls), 1)
        self.assertEqual(paste_calls, [])
        self.assertEqual(update_calls, [])
        self.assertIn("Select one or more target UV islands", reports[-1][1])

    def test_fast_stages_all_writes_before_any_assignment(self):
        obj = _Object()
        uv_layer = obj.data.uv_layers.active
        bm = _BMesh((_triangle_face(),), uv_layer)
        initial = [(loop[uv_layer].uv.x, loop[uv_layer].uv.y) for loop in bm.faces[0].loops]
        job = {
            **_identity_fields(obj),
            "object_name": obj.name,
            "snapshot": {"faces": ()},
            "_allowed_write_keys": {(0, 0), (0, 1), (0, 2)},
        }
        staged = STACK._v2_stage_writes(
            job,
            bm,
            uv_layer,
            ((0, 0, 0.2, 0.2), (0, 0, 0.4, 0.4)),
        )
        self.assertIsNone(staged)
        self.assertEqual(
            initial,
            [(loop[uv_layer].uv.x, loop[uv_layer].uv.y) for loop in bm.faces[0].loops],
        )

    def test_fast_apply_rejects_invalid_second_write_without_mutation(self):
        obj = _Object()
        uv_layer = obj.data.uv_layers.active
        bm = _BMesh((_triangle_face(),), uv_layer)
        snapshot_face = (
            0,
            True,
            False,
            (
                (0, 0, 0.0, 0.0),
                (1, 1, 1.0, 0.0),
                (2, 2, 0.0, 1.0),
            ),
        )
        job = {
            **_identity_fields(obj),
            "snapshot": {"faces": (snapshot_face,)},
            "edge_count": len(bm.edges),
            "vert_count": len(bm.verts),
            "result": {
                "writes": ((0, 0, 0.2, 0.2), (0, 0, 0.4, 0.4)),
            },
            "_allowed_write_keys": {(0, 0), (0, 1), (0, 2)},
        }
        undo_calls = []
        updates = []
        fake_bpy = _fake_bpy(obj, undo_calls)
        fake_bmesh = SimpleNamespace(
            from_edit_mesh=lambda _data: bm,
            update_edit_mesh=lambda *_args, **_kwargs: updates.append("update"),
        )
        with patch.object(STACK, "bpy", fake_bpy), patch.object(STACK, "bmesh", fake_bmesh):
            state = STACK._fast_v2_apply_result(job)
        self.assertEqual(state, "invalid")
        self.assertEqual(undo_calls, [])
        self.assertEqual(updates, [])
        self.assertEqual(
            [(loop[uv_layer].uv.x, loop[uv_layer].uv.y) for loop in bm.faces[0].loops],
            [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0)],
        )

    def test_fast_apply_discards_source_uv_change_after_pointer_reallocation(self):
        obj = _Object()
        uv_layer = obj.data.uv_layers.active
        bm = _BMesh((_triangle_face(),), uv_layer)
        snapshot_face = (
            0,
            True,
            False,
            (
                (0, 0, 0.0, 0.0),
                (1, 1, 1.0, 0.0),
                (2, 2, 0.0, 1.0),
            ),
        )
        job = {
            **_identity_fields(obj),
            "snapshot": {"faces": (snapshot_face,)},
            "edge_count": len(bm.edges),
            "vert_count": len(bm.verts),
            "result": {"writes": ((0, 0, 0.2, 0.2),)},
            "_allowed_write_keys": {(0, 0), (0, 1), (0, 2)},
        }
        bm.faces[0].loops[0][uv_layer].uv.x = 0.25
        undo_calls = []
        updates = []
        fake_bpy = _fake_bpy(obj, undo_calls)
        fake_bmesh = SimpleNamespace(
            from_edit_mesh=lambda _data: bm,
            update_edit_mesh=lambda *_args, **_kwargs: updates.append("update"),
        )
        with patch.object(STACK, "bpy", fake_bpy), patch.object(STACK, "bmesh", fake_bmesh):
            state = STACK._fast_v2_apply_result(job)
        self.assertEqual(state, "changed")
        self.assertEqual(undo_calls, [])
        self.assertEqual(updates, [])

    def test_pro_invalid_second_event_write_is_terminal_and_atomic(self):
        obj = _Object()
        uv_layer = obj.data.uv_layers.active
        bm = _BMesh((_triangle_face(), _triangle_face(3, 2.0)), uv_layer)
        snapshot = {
            "faces": (
                (
                    0,
                    True,
                    False,
                    ((0, 0, 0.0, 0.0), (1, 1, 1.0, 0.0), (2, 2, 0.0, 1.0)),
                ),
                (
                    1,
                    True,
                    False,
                    ((3, 3, 2.0, 0.0), (4, 4, 3.0, 0.0), (5, 5, 2.0, 1.0)),
                ),
            ),
        }
        job = {
            **_identity_fields(obj),
            "snapshot": snapshot,
            "edge_count": len(bm.edges),
            "vert_count": len(bm.verts),
            "expected_uv": {
                (0, 0): (0.0, 0.0),
                (0, 1): (1.0, 0.0),
                (0, 2): (0.0, 1.0),
                (1, 0): (2.0, 0.0),
                (1, 1): (3.0, 0.0),
                (1, 2): (2.0, 1.0),
            },
            "expected_structure": {
                (face, local): (face * 3 + local, face * 3 + local)
                for face in (0, 1)
                for local in range(3)
            },
            "_allowed_write_keys": {(1, 0), (1, 1), (1, 2)},
            "updates_dir": None,
            "undo_pushed": True,
            "next_update_sequence": 1,
        }
        initial = [
            (loop[uv_layer].uv.x, loop[uv_layer].uv.y)
            for face in bm.faces
            for loop in face.loops
        ]
        with tempfile.TemporaryDirectory() as directory:
            update_path = Path(directory) / "00000001.pkl"
            update_path.write_bytes(
                pickle.dumps(
                    {
                        "sequence": 1,
                        "target_face_key": (1,),
                        "master_face_key": (0,),
                        "writes": (
                            (1, 0, 0.1, 0.1),
                            (1, 0, 0.2, 0.2),
                            (1, 2, 0.3, 0.3),
                        ),
                    },
                    protocol=5,
                )
            )
            job["updates_dir"] = directory
            fake_bpy = _fake_bpy(obj)
            updates = []
            fake_bmesh = SimpleNamespace(
                from_edit_mesh=lambda _data: bm,
                update_edit_mesh=lambda *_args, **_kwargs: updates.append("update"),
            )
            with patch.object(STACK, "bpy", fake_bpy), patch.object(STACK, "bmesh", fake_bmesh):
                state = STACK._pro_exact_v2_apply_pending_updates(job)
        self.assertEqual(state, "invalid")
        self.assertEqual(updates, [])
        self.assertEqual(
            initial,
            [
                (loop[uv_layer].uv.x, loop[uv_layer].uv.y)
                for face in bm.faces
                for loop in face.loops
            ],
        )

    def test_pro_unrelated_source_uv_change_blocks_first_event_batch(self):
        obj = _Object()
        uv_layer = obj.data.uv_layers.active
        bm = _BMesh((_triangle_face(), _triangle_face(3, 2.0)), uv_layer)
        snapshot = {
            "faces": (
                (0, True, False, ((0, 0, 0.0, 0.0), (1, 1, 1.0, 0.0), (2, 2, 0.0, 1.0))),
                (1, True, False, ((3, 3, 2.0, 0.0), (4, 4, 3.0, 0.0), (5, 5, 2.0, 1.0))),
            ),
        }
        job = {
            **_identity_fields(obj),
            "snapshot": snapshot,
            "edge_count": len(bm.edges),
            "vert_count": len(bm.verts),
            "expected_uv": {
                (0, 0): (0.0, 0.0), (0, 1): (1.0, 0.0), (0, 2): (0.0, 1.0),
                (1, 0): (2.0, 0.0), (1, 1): (3.0, 0.0), (1, 2): (2.0, 1.0),
            },
            "expected_structure": {
                (face, local): (face * 3 + local, face * 3 + local)
                for face in (0, 1)
                for local in range(3)
            },
            "_allowed_write_keys": {(1, 0), (1, 1), (1, 2)},
            "updates_dir": None,
            "undo_pushed": True,
            "next_update_sequence": 1,
        }
        # The changed face is outside the first worker event's target/master.
        bm.faces[0].loops[0][uv_layer].uv.x = 0.25
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "00000001.pkl").write_bytes(
                pickle.dumps(
                    {
                        "sequence": 1,
                        "target_face_key": (1,),
                        "master_face_key": (0,),
                        "writes": (
                            (1, 0, 0.1, 0.1),
                            (1, 1, 0.2, 0.2),
                            (1, 2, 0.3, 0.3),
                        ),
                    },
                    protocol=5,
                )
            )
            job["updates_dir"] = directory
            fake_bpy = _fake_bpy(obj)
            updates = []
            fake_bmesh = SimpleNamespace(
                from_edit_mesh=lambda _data: bm,
                update_edit_mesh=lambda *_args, **_kwargs: updates.append("update"),
            )
            with patch.object(STACK, "bpy", fake_bpy), patch.object(STACK, "bmesh", fake_bmesh):
                state = STACK._pro_exact_v2_apply_pending_updates(job)
        self.assertEqual(state, "changed")
        self.assertEqual(updates, [])
        self.assertEqual(bm.faces[1].loops[0][uv_layer].uv.x, 2.0)

    def test_admission_guards_pack_and_fast_pro_pair(self):
        pack_module = SimpleNamespace(_ACTIVE_PACK_V2_JOB=object())
        with patch.dict(sys.modules, {"uv_gpt.pack_tools": pack_module}):
            with self.assertRaisesRegex(RuntimeError, "Pack"):
                STACK._fast_v2_start_job(None)
            with self.assertRaisesRegex(RuntimeError, "Pack"):
                STACK._pro_exact_v2_start_job(None)

        with patch.object(STACK, "_ACTIVE_PRO_EXACT_V2_JOB", object()):
            with self.assertRaisesRegex(RuntimeError, "Pro"):
                STACK._fast_v2_start_job(None)
        with patch.object(STACK, "_ACTIVE_FAST_BACKGROUND_JOB", object()):
            with self.assertRaisesRegex(RuntimeError, "Fast"):
                STACK._pro_exact_v2_start_job(None)

    def test_source_guard_distinguishes_context_wait_from_identity_failure(self):
        obj = _Object(mode="OBJECT")
        job = _identity_fields(obj)
        fake_bpy = _fake_bpy(obj)
        with patch.object(STACK, "bpy", fake_bpy):
            self.assertEqual(STACK._fast_v2_source_context_state(job, fake_bpy.context), "wait")
            obj.data = _RNA(99, "ReplacementMesh")
            obj.data.uv_layers = _UVLayers(_RNA(100, "UVMap"))
            self.assertEqual(STACK._fast_v2_source_context_state(job, fake_bpy.context), "invalid")
            fake_bpy.data.objects.clear()
            self.assertEqual(STACK._fast_v2_source_context_state(job, fake_bpy.context), "invalid")

    def test_source_guard_allows_transient_uv_pointer_change(self):
        obj = _Object(mode="EDIT")
        job = _identity_fields(obj)
        fake_bpy = _fake_bpy(obj)
        replacement = _RNA(44, "UVMap")
        obj.data.uv_layers[0] = replacement
        obj.data.uv_layers.active = replacement
        with patch.object(STACK, "bpy", fake_bpy):
            self.assertEqual(
                STACK._fast_v2_source_context_state(job, fake_bpy.context),
                "ready",
            )

    def test_fast_deleted_source_takes_terminal_cleanup_path(self):
        job = {"kind": "fast_v2", "object_name": "Source"}
        fake_bpy = SimpleNamespace(
            data=SimpleNamespace(objects={}),
            context=SimpleNamespace(edit_object=None, object=None, workspace=None),
        )
        with patch.object(STACK, "bpy", fake_bpy), \
             patch.object(STACK, "_ACTIVE_FAST_BACKGROUND_JOB", job), \
             patch.object(STACK, "_fast_v2_cancel_job") as cancel:
            result = STACK._fast_v2_background_timer()
        self.assertIsNone(result)
        cancel.assert_called_once_with(job, "source_invalid")

    def test_pro_deleted_source_takes_terminal_cleanup_path(self):
        job = {"kind": "pro_exact_v2", "object_name": "Source"}
        with patch.object(STACK, "_ACTIVE_PRO_EXACT_V2_JOB", job), \
             patch.object(STACK, "_pro_exact_v2_apply_pending_updates", return_value="invalid"), \
             patch.object(STACK, "_pro_exact_v2_finish_job") as finish:
            result = STACK._pro_exact_v2_background_timer()
        self.assertIsNone(result)
        finish.assert_called_once_with(
            job,
            terminal_state="invalid",
            terminal_error="invalid_result_or_source",
        )

    def test_cancel_records_terminal_report_and_reaps_publish_race(self):
        job = {}
        STACK._v2_cancel_external_job(job, "source_invalid")
        self.assertTrue(job["cancelled"])
        self.assertEqual(job["terminal_state"], "source_invalid")
        self.assertEqual(job["terminal_error"], "source_invalid")

        process = _RunningProcess()
        self.assertFalse(STACK._v2_publish_process({"cancelled": True}, process))
        self.assertTrue(process.terminated)


if __name__ == "__main__":
    unittest.main()
