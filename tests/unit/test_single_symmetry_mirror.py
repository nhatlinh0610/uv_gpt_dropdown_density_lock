from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import sys
import types
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = PROJECT_ROOT / "uv_gpt" / "symmetry_pair.py"


class FakeVector:
    def __init__(self, value):
        if isinstance(value, FakeVector):
            self.x = float(value.x)
            self.y = float(value.y)
        else:
            self.x = float(value[0])
            self.y = float(value[1])

    def __iter__(self):
        return iter((self.x, self.y))

    def __add__(self, other):
        return FakeVector((self.x + other.x, self.y + other.y))

    def __sub__(self, other):
        return FakeVector((self.x - other.x, self.y - other.y))

    def __truediv__(self, scalar):
        return FakeVector((self.x / scalar, self.y / scalar))

    def __mul__(self, scalar):
        return FakeVector((self.x * scalar, self.y * scalar))

    __rmul__ = __mul__

    @property
    def length_squared(self):
        return self.x * self.x + self.y * self.y

    @property
    def length(self):
        return math.sqrt(self.length_squared)

    def dot(self, other):
        return self.x * other.x + self.y * other.y

    def negate(self):
        self.x = -self.x
        self.y = -self.y


class FakeOperator:
    def report(self, level, message):
        self.reported = (level, message)


class FakeUVData:
    def __init__(self, point):
        self.uv = FakeVector(point)


class FakeLoop:
    def __init__(self, point):
        self.data = FakeUVData(point)

    def __getitem__(self, _layer):
        return self.data


class FakeFace:
    def __init__(self, points):
        self.loops = [FakeLoop(point) for point in points]


def _load_symmetry_module():
    """Load the Blender module with small isolated import stubs."""
    module_name = "uv_gpt.symmetry_pair_single_mirror_test"
    names = (
        "bpy",
        "bmesh",
        "mathutils",
        "uv_gpt",
        "uv_gpt.island_tools",
        "uv_gpt.uv_utils",
    )
    originals = {name: sys.modules.get(name) for name in names}

    bpy = types.ModuleType("bpy")
    bpy.types = types.SimpleNamespace(Operator=FakeOperator)
    bmesh = types.ModuleType("bmesh")
    updates = []
    bmesh.update_edit_mesh = lambda *args, **kwargs: updates.append((args, kwargs))
    mathutils = types.ModuleType("mathutils")
    mathutils.Vector = FakeVector
    package = types.ModuleType("uv_gpt")
    package.__path__ = [str(PROJECT_ROOT / "uv_gpt")]
    island_tools = types.ModuleType("uv_gpt.island_tools")
    uv_utils = types.ModuleType("uv_gpt.uv_utils")
    sys.modules.update(
        {
            "bpy": bpy,
            "bmesh": bmesh,
            "mathutils": mathutils,
            "uv_gpt": package,
            "uv_gpt.island_tools": island_tools,
            "uv_gpt.uv_utils": uv_utils,
        }
    )
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    for name, original in originals.items():
        if original is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = original
    sys.modules.pop(module_name, None)
    module._test_updates = updates
    return module


SYMMETRY = _load_symmetry_module()


def _region(points):
    return [FakeFace(points)]


def _points(region):
    return [
        (loop[None].uv.x, loop[None].uv.y)
        for face in region
        for loop in face.loops
    ]


def _distances(points):
    return sorted(
        math.hypot(x1 - x2, y1 - y2)
        for index, (x1, y1) in enumerate(points)
        for x2, y2 in points[index + 1 :]
    )


class SingleSymmetryMirrorTests(unittest.TestCase):
    def test_u_and_v_reflection_preserve_scale_and_are_involutions(self):
        original = [(0.17, 0.23), (0.41, 0.31), (0.29, 0.68), (0.12, 0.55)]
        original_distances = _distances(original)
        for axis, expected in (
            ("U", [(0.83, 0.23), (0.59, 0.31), (0.71, 0.68), (0.88, 0.55)]),
            ("V", [(0.17, 0.77), (0.41, 0.69), (0.29, 0.32), (0.12, 0.45)]),
        ):
            region = _region(original)
            SYMMETRY._mirror_single_region(region, None, axis, 0.5)
            for actual, wanted in zip(_points(region), expected):
                self.assertAlmostEqual(actual[0], wanted[0], places=15)
                self.assertAlmostEqual(actual[1], wanted[1], places=15)
            for actual, wanted in zip(_distances(_points(region)), original_distances):
                self.assertAlmostEqual(actual, wanted, places=14)
            SYMMETRY._mirror_single_region(region, None, axis, 0.5)
            for actual, wanted in zip(_points(region), original):
                self.assertAlmostEqual(actual[0], wanted[0], places=15)
                self.assertAlmostEqual(actual[1], wanted[1], places=15)

    def test_operator_routes_one_region_to_reflection_without_center_helper(self):
        original = [(0.17, 0.23), (0.41, 0.31), (0.29, 0.68)]
        region = _region(original)
        settings = types.SimpleNamespace(symmetry_axis="U_HALF")
        context = types.SimpleNamespace()
        obj = types.SimpleNamespace(data=object())
        bm = object()
        layer = object()
        calls = {"ensure": 0}
        SYMMETRY._selected_regions = lambda *_args: [region]
        SYMMETRY.uv_utils.get_active_mesh_object = lambda _context: obj
        SYMMETRY.island_tools.get_active_bmesh = lambda _context: bm
        SYMMETRY.island_tools.get_active_uv_layer = lambda _bm, _obj: layer
        SYMMETRY.uv_utils.get_settings = lambda _context: settings

        def ensure(_context):
            calls["ensure"] += 1

        SYMMETRY.uv_utils.ensure_destructive_ready = ensure
        SYMMETRY._center_and_align_single_region = lambda *_args: (_ for _ in ()).throw(
            AssertionError("single-region center helper must remain dormant")
        )
        operator = SYMMETRY.UVGPT_OT_symmetry_auto_mirror()
        self.assertEqual(operator.execute(context), {"FINISHED"})
        self.assertEqual(calls["ensure"], 1)
        for actual, wanted in zip(
            _points(region),
            [(0.83, 0.23), (0.59, 0.31), (0.71, 0.68)],
        ):
            self.assertAlmostEqual(actual[0], wanted[0], places=15)
            self.assertAlmostEqual(actual[1], wanted[1], places=15)
        self.assertEqual(len(SYMMETRY._test_updates), 1)

    def test_invalid_single_axis_cancels_before_any_mutation(self):
        original = [(0.17, 0.23), (0.41, 0.31), (0.29, 0.68)]
        region = _region(original)
        settings = types.SimpleNamespace(symmetry_axis="INVALID")
        context = types.SimpleNamespace()
        obj = types.SimpleNamespace(data=object())
        bm = object()
        layer = object()
        ensured = []
        SYMMETRY._selected_regions = lambda *_args: [region]
        SYMMETRY.uv_utils.get_active_mesh_object = lambda _context: obj
        SYMMETRY.island_tools.get_active_bmesh = lambda _context: bm
        SYMMETRY.island_tools.get_active_uv_layer = lambda _bm, _obj: layer
        SYMMETRY.uv_utils.get_settings = lambda _context: settings
        SYMMETRY.uv_utils.ensure_destructive_ready = lambda _context: ensured.append(True)
        operator = SYMMETRY.UVGPT_OT_symmetry_auto_mirror()
        self.assertEqual(operator.execute(context), {"CANCELLED"})
        self.assertEqual(_points(region), original)
        self.assertEqual(ensured, [])


if __name__ == "__main__":
    unittest.main()
