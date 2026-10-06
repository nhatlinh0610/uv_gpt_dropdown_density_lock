"""Selection-scope tests for texel-density reference-face resolution."""

from pathlib import Path
import importlib.util
import sys
import types
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "uv_gpt" / "texel_density.py"
MISSING = object()


class FakeUVData:
    def __init__(self, *, select=MISSING, select_edge=MISSING):
        if select is not MISSING:
            self.select = select
        if select_edge is not MISSING:
            self.select_edge = select_edge


class FakeLoop:
    def __init__(
        self,
        *,
        uv_data=None,
        uv_select_vert=MISSING,
        uv_select_edge=MISSING,
    ):
        if uv_select_vert is not MISSING:
            self.uv_select_vert = uv_select_vert
        if uv_select_edge is not MISSING:
            self.uv_select_edge = uv_select_edge
        self._uv_data = uv_data or FakeUVData()

    def __getitem__(self, _uv_layer):
        return self._uv_data


class FakeElement:
    def __init__(self, selected=False):
        self.select = selected


class FakeFace:
    def __init__(
        self,
        name,
        *,
        select=False,
        hide=False,
        uv_select=MISSING,
        loops=None,
        edges=None,
        verts=None,
    ):
        self.name = name
        self.select = select
        self.hide = hide
        if uv_select is not MISSING:
            self.uv_select = uv_select
        self.loops = list(loops or [FakeLoop()])
        self.edges = list(edges or [])
        self.verts = list(verts or [])


class FakeFaceCollection(list):
    active = None


class FakeBMesh:
    def __init__(self, faces, *, active=None):
        self.faces = FakeFaceCollection(faces)
        self.faces.active = active


def _load_density_module():
    """Load texel_density with the smallest Blender API surface needed here."""

    module_name = "uv_gpt.texel_density_density_selection_test"
    if module_name in sys.modules:
        return sys.modules[module_name]

    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(Operator=type("Operator", (), {}))
    bmesh = types.ModuleType("bmesh")
    bmesh.types = types.SimpleNamespace(BMFace=FakeFace)
    bmesh.update_edit_mesh = lambda *_args, **_kwargs: None
    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = lambda value: value

    package = types.ModuleType("uv_gpt")
    package.__path__ = [str(PROJECT_ROOT / "uv_gpt")]
    island_tools = types.ModuleType("uv_gpt.island_tools")
    overlay = types.ModuleType("uv_gpt.overlay")
    uv_utils = types.ModuleType("uv_gpt.uv_utils")
    bpy.utils = types.SimpleNamespace(
        register_class=lambda _cls: None,
        unregister_class=lambda _cls: None,
    )

    originals = {
        name: sys.modules.get(name)
        for name in (
            "bpy",
            "bmesh",
            "mathutils",
            "uv_gpt",
            "uv_gpt.island_tools",
            "uv_gpt.overlay",
            "uv_gpt.uv_utils",
        )
    }
    sys.modules.update(
        {
            "bpy": bpy,
            "bmesh": bmesh,
            "mathutils": mathutils,
            "uv_gpt": package,
            "uv_gpt.island_tools": island_tools,
            "uv_gpt.overlay": overlay,
            "uv_gpt.uv_utils": uv_utils,
        }
    )
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
    return module


DENSITY = _load_density_module()


def _context(*, sync=False, mesh_select_mode=(False, False, True)):
    return types.SimpleNamespace(
        scene=types.SimpleNamespace(
            tool_settings=types.SimpleNamespace(
                use_uv_select_sync=sync,
                mesh_select_mode=mesh_select_mode,
            )
        )
    )


class DensityReferenceSelectionTests(unittest.TestCase):
    def test_modern_single_uv_face_wins_over_stale_active_mesh_face(self):
        selected = FakeFace(
            "uv-selected",
            select=False,
            uv_select=False,
            loops=[FakeLoop(uv_select_vert=True)],
        )
        stale_active = FakeFace("stale-active", select=True, uv_select=False)
        bm = FakeBMesh([selected, stale_active], active=stale_active)

        result = DENSITY._selected_reference_face(
            bm, "UVMap", context=_context(sync=False)
        )

        self.assertIs(result, selected)

    def test_modern_multiple_uv_faces_cancel(self):
        first = FakeFace("first", uv_select=True)
        second = FakeFace("second", uv_select=False, loops=[FakeLoop(uv_select_edge=True)])
        bm = FakeBMesh([first, second], active=first)

        with self.assertRaisesRegex(RuntimeError, "Select only one quad face"):
            DENSITY._selected_reference_face(
                bm, "UVMap", context=_context(sync=False)
            )

    def test_modern_empty_uv_selection_does_not_use_stale_active_face(self):
        stale_active = FakeFace(
            "stale-active",
            select=True,
            uv_select=False,
            loops=[FakeLoop(uv_select_vert=False, uv_select_edge=False)],
        )
        bm = FakeBMesh([stale_active], active=stale_active)

        with self.assertRaisesRegex(RuntimeError, "Select one quad face"):
            DENSITY._selected_reference_face(
                bm, "UVMap", context=_context(sync=False)
            )

    def test_legacy_face_selection_fallback_without_uv_selection_api(self):
        legacy = FakeFace("legacy", select=True, loops=[FakeLoop()])
        bm = FakeBMesh([legacy])

        result = DENSITY._selected_reference_face(bm, "UVMap")

        self.assertIs(result, legacy)

    def test_legacy_uv_loop_selection_is_used_without_modern_flags(self):
        selected = FakeFace(
            "legacy-uv-selected",
            select=False,
            loops=[FakeLoop(uv_data=FakeUVData(select=True))],
        )
        stale = FakeFace(
            "legacy-uv-unselected",
            select=True,
            loops=[FakeLoop(uv_data=FakeUVData(select=False))],
        )
        bm = FakeBMesh([selected, stale], active=stale)

        result = DENSITY._selected_reference_face(bm, "UVMap")

        self.assertIs(result, selected)

    def test_sync_on_face_mode_uses_mesh_selection_when_uv_flags_are_stale(self):
        mesh_selected = FakeFace(
            "mesh-selected",
            select=True,
            uv_select=False,
            loops=[FakeLoop(uv_select_vert=False, uv_select_edge=False)],
        )
        mesh_unselected = FakeFace(
            "mesh-unselected",
            select=False,
            uv_select=True,
        )
        bm = FakeBMesh([mesh_selected, mesh_unselected], active=mesh_unselected)

        result = DENSITY._selected_reference_face(
            bm,
            "UVMap",
            context=_context(sync=True, mesh_select_mode=(False, False, True)),
        )

        self.assertIs(result, mesh_selected)

    def test_sync_on_edge_mode_requires_complete_selected_boundary(self):
        selected_edges = [FakeElement(True), FakeElement(True)]
        partial_edges = [FakeElement(True), FakeElement(False)]
        selected = FakeFace("selected", edges=selected_edges, uv_select=False)
        partial = FakeFace("partial", edges=partial_edges, uv_select=True)
        bm = FakeBMesh([selected, partial])

        result = DENSITY._selected_reference_face(
            bm,
            "UVMap",
            context=_context(sync=True, mesh_select_mode=(False, True, False)),
        )

        self.assertIs(result, selected)


if __name__ == "__main__":
    unittest.main()
