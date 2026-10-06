"""Focused Pack V2 symmetry geometry and bounded-runtime regressions."""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "uv_gpt"))

import pack_v2_core as core  # noqa: E402
import pro_exact_v2_core as exact  # noqa: E402


def _snapshot(axis="U", preserve_stacks=False, with_blocker=False):
    faces = [
        [
            0,
            True,
            False,
            [[0, 0, 0.10, 0.10], [1, 1, 0.22, 0.10], [2, 2, 0.10, 0.24]],
        ],
        [
            1,
            True,
            False,
            [[3, 3, 0.34, 0.58], [4, 4, 0.46, 0.58], [5, 5, 0.34, 0.72]],
        ],
    ]
    world = {"0": 1.0, "1": 1.0}
    if with_blocker:
        faces.append(
            [
                2,
                False,
                False,
                [[6, 6, 0.46, 0.20], [7, 7, 0.54, 0.20], [8, 8, 0.54, 0.48], [9, 9, 0.46, 0.48]],
            ]
        )
        world["2"] = 1.0
    return {
        "schema": core.SCHEMA,
        "faces": faces,
        "world_area_by_face": world,
        "options": {
            "mode": "symmetry",
            "margin": 0.003,
            "rotation_mode": "NONE",
            "preserve_stacks": bool(preserve_stacks),
            "scale_to_fit": True,
            "axis_kind": axis,
        },
    }


def _stacked_snapshot():
    faces = []
    for face_index in range(2):
        vertex = face_index * 3
        edge = face_index * 3
        faces.append(
            [
                face_index,
                True,
                False,
                [
                    [vertex, edge, 0.10, 0.10],
                    [vertex + 1, edge + 1, 0.20, 0.10],
                    [vertex + 2, edge + 2, 0.10, 0.20],
                ],
            ]
        )
    snapshot = _snapshot()
    snapshot["faces"] = faces
    snapshot["world_area_by_face"] = {"0": 1.0, "1": 1.0}
    snapshot["options"] = dict(snapshot["options"], preserve_stacks=True)
    return snapshot


def _automorphic_stacked_snapshot():
    """Two coincident quads whose follower loop cycle starts at another corner."""
    return {
        "schema": core.SCHEMA,
        "faces": [
            [
                0,
                True,
                False,
                [
                    [0, 0, 0.10, 0.10],
                    [1, 1, 0.30, 0.10],
                    [2, 2, 0.30, 0.30],
                    [3, 3, 0.10, 0.30],
                ],
            ],
            [
                1,
                True,
                False,
                [
                    [4, 4, 0.30, 0.10],
                    [5, 5, 0.30, 0.30],
                    [6, 6, 0.10, 0.30],
                    [7, 7, 0.10, 0.10],
                ],
            ],
        ],
        "world_area_by_face": {"0": 1.0, "1": 1.0},
        "options": {
            "mode": "whole",
            "margin": 0.003,
            "rotation_mode": "NONE",
            "preserve_stacks": True,
            "scale_to_fit": True,
            "axis_kind": "U",
        },
    }


def _already_packed_snapshot():
    """Small valid source layout for the Keep Current Scale reuse path."""
    return {
        "schema": core.SCHEMA,
        "faces": [
            [
                0,
                True,
                False,
                [
                    [0, 0, 0.10, 0.10],
                    [1, 1, 0.22, 0.10],
                    [2, 2, 0.10, 0.24],
                ],
            ],
            [
                1,
                True,
                False,
                [
                    [3, 3, 0.34, 0.58],
                    [4, 4, 0.46, 0.58],
                    [5, 5, 0.34, 0.72],
                ],
            ],
        ],
        "world_area_by_face": {"0": 1.0, "1": 1.0},
        "options": {
            "mode": "selected",
            "margin": 0.003,
            "rotation_mode": "NONE",
            "preserve_stacks": False,
            "scale_to_fit": False,
            "axis_kind": "U",
        },
    }


def _impossible_source_snapshot():
    """Two coincident large islands must not be accepted as a source layout."""
    return {
        "schema": core.SCHEMA,
        "faces": [
            [
                0,
                True,
                False,
                [
                    [0, 0, 0.05, 0.05],
                    [1, 1, 0.95, 0.05],
                    [2, 2, 0.95, 0.95],
                    [3, 3, 0.05, 0.95],
                ],
            ],
            [
                1,
                True,
                False,
                [
                    [4, 4, 0.05, 0.05],
                    [5, 5, 0.95, 0.05],
                    [6, 6, 0.95, 0.95],
                    [7, 7, 0.05, 0.95],
                ],
            ],
        ],
        "world_area_by_face": {"0": 1.0, "1": 1.0},
        "options": {
            "mode": "selected",
            "margin": 0.05,
            "rotation_mode": "NONE",
            "preserve_stacks": False,
            "scale_to_fit": False,
            "axis_kind": "U",
        },
    }


def _nested_concave_snapshot():
    """A valid source layout whose concave AABBs overlap in a cavity."""
    return {
        "schema": core.SCHEMA,
        "faces": [
            [
                0,
                True,
                False,
                [
                    [0, 0, 0.10, 0.10],
                    [1, 1, 0.70, 0.10],
                    [2, 2, 0.70, 0.30],
                    [3, 3, 0.30, 0.30],
                    [4, 4, 0.30, 0.70],
                    [5, 5, 0.10, 0.70],
                ],
            ],
            [
                1,
                True,
                False,
                [
                    [6, 6, 0.50, 0.50],
                    [7, 7, 0.65, 0.50],
                    [8, 8, 0.65, 0.65],
                    [9, 9, 0.50, 0.65],
                ],
            ],
        ],
        "world_area_by_face": {"0": 1.0, "1": 1.0},
        "options": {
            "mode": "selected",
            "margin": 0.003,
            "rotation_mode": "NONE",
            "preserve_stacks": False,
            "scale_to_fit": False,
            "axis_kind": "U",
        },
    }


def _exact_by_key(snapshot):
    exact_snapshot = dict(snapshot)
    exact_snapshot["schema"] = exact.SCHEMA
    return {
        item.face_key: item
        for item in exact.build_exact_islands(exact_snapshot, selected_only=False)
    }


def _output_uvs(result):
    return {(int(face), int(loop)): (float(u), float(v)) for face, loop, u, v in result["writes"]}


def _output_shapes(snapshot, result):
    writes = _output_uvs(result)
    shapes = []
    for island in core._parse_islands(snapshot):
        loop_uvs = {key: writes.get(key, uv) for key, uv in island.loop_uvs.items()}
        shapes.append((island, core._shape_from_loop_uvs(island, loop_uvs)))
    return shapes


class PackV2GeometryStabilityTests(unittest.TestCase):
    def test_final_collision_boundary_preserves_every_vertex(self):
        points=tuple((0.5+0.3*math.cos(i*math.tau/120),
                      0.5+0.3*math.sin(i*math.tau/120)) for i in range(120))
        shapes=core.boundary_loops_from_segments(tuple(zip(points,points[1:]+points[:1])))
        self.assertEqual(len(shapes),1)
        self.assertEqual(len(shapes[0]),120)
        self.assertEqual(set(shapes[0]),set(points))

    def test_placement_bounds_include_all_written_uv_corners(self):
        shape=(((0.,0.),(1.,0.),(0.,1.)),)
        values={(0,0):(0.,0.),(0,1):(1.,0.),(0,2):(0.,1.),(1,0):(-.05,1.1)}
        local,_,width,height=core._normalize_local(values,shape)
        self.assertEqual((width,height),(1.05,1.1))
        self.assertTrue(all(0<=u<=width and 0<=v<=height for u,v in local.values()))

    def test_maxrect_reserves_full_margin_between_rectangles_and_tile_edges(self):
        margin = 0.08
        units = [
            core.PackUnit(
                key=(index,),
                representative=None,
                width=0.30,
                height=0.25,
                area=1.0,
            )
            for index in range(2)
        ]
        placements = core._maxrect_layout(units, (), 1.0, margin)
        self.assertIsNotNone(placements)
        left = placements[(0,)]
        right = placements[(1,)]
        boxes = [
            (left[0], left[1], units[0].width, units[0].height),
            (right[0], right[1], units[1].width, units[1].height),
        ]
        for x, y, width, height in boxes:
            self.assertGreaterEqual(x, margin - 1.0e-9)
            self.assertGreaterEqual(y, margin - 1.0e-9)
            self.assertLessEqual(x + width, 1.0 - margin + 1.0e-9)
            self.assertLessEqual(y + height, 1.0 - margin + 1.0e-9)
        horizontal_gap = max(
            boxes[1][0] - (boxes[0][0] + boxes[0][2]),
            boxes[0][0] - (boxes[1][0] + boxes[1][2]),
        )
        vertical_gap = max(
            boxes[1][1] - (boxes[0][1] + boxes[0][3]),
            boxes[0][1] - (boxes[1][1] + boxes[1][3]),
        )
        self.assertGreaterEqual(max(horizontal_gap, vertical_gap), margin - 1.0e-9)

    def test_keep_current_scale_reuses_verified_source_layout_at_scale_one(self):
        snapshot = _already_packed_snapshot()
        original = {
            key: uv
            for island in core._parse_islands(snapshot)
            for key, uv in island.loop_uvs.items()
        }
        started = time.perf_counter()
        result = core.solve_pack(snapshot)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["scale"], 1.0)
        self.assertEqual(result["movable_count"], 2)
        self.assertEqual(len(result["writes"]), len(original))
        self.assertEqual(_output_uvs(result), original)

        index = core.SpatialIndex(44)
        for _island, shape in _output_shapes(snapshot, result):
            self.assertTrue(core._shape_fit_tile(shape, snapshot["options"]["margin"]))
            self.assertFalse(
                any(
                    core.shape_sets_conflict(shape, other, snapshot["options"]["margin"])
                    for other in index.query(shape, margin=snapshot["options"]["margin"])
                )
            )
            index.insert(shape)

    def test_keep_current_scale_rejects_genuinely_colliding_source(self):
        with self.assertRaises(RuntimeError):
            core.solve_pack(_impossible_source_snapshot())

    def test_keep_current_scale_reuses_valid_concave_cavity_before_aabb_search(self):
        snapshot = _nested_concave_snapshot()
        original = {
            key: uv
            for island in core._parse_islands(snapshot)
            for key, uv in island.loop_uvs.items()
        }
        result = core.solve_pack(snapshot)
        self.assertEqual(result["scale"], 1.0)
        self.assertEqual(_output_uvs(result), original)
        shapes = _output_shapes(snapshot, result)
        self.assertFalse(
            core.shape_sets_conflict(shapes[0][1], shapes[1][1], snapshot["options"]["margin"])
        )

    def test_keep_current_scale_rejects_changed_preparation_orientation(self):
        snapshot = _already_packed_snapshot()
        snapshot["options"] = dict(snapshot["options"], rotation_mode="ROT_180")
        islands = list(core._parse_islands(snapshot))
        units = core._build_pack_units(snapshot, islands, islands, False)
        for unit in units:
            core._prepare_unit(unit, rotation_mode="ROT_180", density_scale=1.0)
        self.assertIsNone(core._reuse_source_layout(units, (), snapshot["options"]["margin"]))
        original = {
            key: uv
            for island in islands
            for key, uv in island.loop_uvs.items()
        }
        result = core.solve_pack(snapshot)
        self.assertEqual(result["scale"], 1.0)
        self.assertNotEqual(_output_uvs(result), original)

    def test_exact_pair_is_reflected_on_requested_axis_and_blocker_is_untouched(self):
        for axis in ("U", "V"):
            snapshot = _snapshot(axis=axis, with_blocker=True)
            result = core.solve_pack(snapshot)
            self.assertEqual(result["pair_count"], 1)
            self.assertEqual(result["single_count"], 0)
            writes = _output_uvs(result)
            exact_by = _exact_by_key(snapshot)
            islands = list(core._parse_islands(snapshot))
            selected = [island for island in islands if island.selected]
            mapping = exact.find_deterministic_exact_mapping(
                exact_by[selected[0].face_key].graph,
                exact_by[selected[1].face_key].graph,
            )
            residual = 0.0
            for right_key, left_key in mapping:
                left = writes[left_key]
                right = writes[right_key]
                if axis == "U":
                    residual = max(residual, abs(left[0] + right[0] - 1.0), abs(left[1] - right[1]))
                else:
                    residual = max(residual, abs(left[0] - right[0]), abs(left[1] + right[1] - 1.0))
            self.assertLessEqual(residual, 1.0e-7)
            blocker_keys = {
                key
                for island in islands
                if not island.selected
                for key in island.loop_uvs
            }
            self.assertFalse(blocker_keys.intersection(writes))
            output_shapes = _output_shapes(snapshot, result)
            for index, (left_island, left_shape) in enumerate(output_shapes):
                for right_island, right_shape in output_shapes[index + 1 :]:
                    self.assertFalse(
                        core.shape_sets_conflict(left_shape, right_shape, margin=snapshot["options"]["margin"]),
                        (axis, left_island.face_key, right_island.face_key),
                    )

    def test_single_island_keeps_rigid_shape(self):
        snapshot = _snapshot(axis="U")
        snapshot["faces"] = [snapshot["faces"][0]]
        snapshot["world_area_by_face"] = {"0": 1.0}
        result = core.solve_pack(snapshot)
        self.assertEqual(result["pair_count"], 0)
        self.assertEqual(result["single_count"], 1)
        original = core._parse_islands(snapshot)[0].loop_uvs
        writes = _output_uvs(result)
        original_edge = math.hypot(original[(0, 0)][0] - original[(0, 1)][0], original[(0, 0)][1] - original[(0, 1)][1])
        output_edge = math.hypot(writes[(0, 0)][0] - writes[(0, 1)][0], writes[(0, 0)][1] - writes[(0, 1)][1])
        original_other = math.hypot(original[(0, 0)][0] - original[(0, 2)][0], original[(0, 0)][1] - original[(0, 2)][1])
        output_other = math.hypot(writes[(0, 0)][0] - writes[(0, 2)][0], writes[(0, 0)][1] - writes[(0, 2)][1])
        self.assertAlmostEqual(output_edge / output_other, original_edge / original_other, places=12)

    def test_keep_stack_exact_restores_followers_after_symmetry_layout(self):
        snapshot = _stacked_snapshot()
        result = core.solve_pack(snapshot)
        self.assertEqual(result["stack_followers"], 1)
        self.assertEqual(len(result["writes"]), 6)
        writes = _output_uvs(result)
        for loop in range(3):
            self.assertEqual(writes[(0, loop)], writes[(1, loop)])

    def test_stack_correspondence_uses_uv_labels_for_symmetric_topology(self):
        snapshot = _automorphic_stacked_snapshot()
        islands = list(core._parse_islands(snapshot))
        exact_islands = exact.build_exact_islands(
            dict(snapshot, schema=exact.SCHEMA), selected_only=False
        )
        deterministic = exact.find_deterministic_exact_mapping(
            exact_islands[0].graph,
            exact_islands[1].graph,
        )
        deterministic_residual = max(
            math.hypot(
                islands[1].loop_uvs[candidate][0] - islands[0].loop_uvs[master][0],
                islands[1].loop_uvs[candidate][1] - islands[0].loop_uvs[master][1],
            )
            for candidate, master in deterministic
        )
        self.assertGreater(deterministic_residual, core._STACK_DIRECT_TOLERANCE)
        units = core._build_pack_units(snapshot, islands, islands, True)
        self.assertEqual(len(units), 1)
        self.assertEqual(len(units[0].follower_mappings), 1)
        follower, mapping = units[0].follower_mappings[0]
        for candidate, master in mapping:
            self.assertLessEqual(
                math.hypot(
                    follower.loop_uvs[candidate][0] - islands[0].loop_uvs[master][0],
                    follower.loop_uvs[candidate][1] - islands[0].loop_uvs[master][1],
                ),
                core._STACK_DIRECT_TOLERANCE,
            )

    def test_actual_pro_keep_stack_fixture_restores_all_eighty_eight_followers(self):
        path = ROOT / ".test_runtime" / "accessories_20261007" / "post_pack_whole_mesh_keep_stack_snapshot.json"
        if not path.exists():
            self.skipTest("live whole-mesh Keep Stack snapshot is unavailable")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        islands = list(core._parse_islands(snapshot))
        movable = [island for island in islands if island.selected]
        started = time.perf_counter()
        units = core._build_pack_units(snapshot, islands, movable, True)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 10.0)
        self.assertEqual(len(units), 61)
        self.assertEqual(sum(len(unit.follower_mappings) for unit in units), 88)
        for unit in units:
            representative = unit.representative
            for follower, mapping in unit.follower_mappings:
                self.assertIsNotNone(
                    core._validated_stack_mapping(mapping, representative, follower)
                )

    def test_full_accessories_snapshot_is_bounded_and_reflected(self):
        path = ROOT / ".test_runtime" / "accessories_20261007" / "pack_symmetry_U_baseline_snapshot.json"
        if not path.exists():
            self.skipTest("live accessories snapshot is unavailable")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        started = time.perf_counter()
        result = core.solve_pack(snapshot)
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 60.0)
        self.assertEqual(result["movable_count"], 149)
        self.assertEqual(result["pair_count"], 58)
        self.assertEqual(result["single_count"], 33)
        self.assertGreater(result["scale"], 0.0)
        writes = _output_uvs(result)
        exact_by = _exact_by_key(snapshot)
        islands = [island for island in core._parse_islands(snapshot) if island.selected]
        density = core._density_scales_for_symmetry(islands)
        units, _, _ = core._sym_units(islands, "U", density, exact_by)
        residual = 0.0
        for unit in units:
            if unit["kind"] != "pair":
                continue
            left, right = unit["members"]
            mapping = exact.find_deterministic_exact_mapping(
                exact_by[left[0].face_key].graph,
                exact_by[right[0].face_key].graph,
            )
            for right_key, left_key in mapping:
                lu, lv = writes[left_key]
                ru, rv = writes[right_key]
                residual = max(residual, abs(lu + ru - 1.0), abs(lv - rv))
        self.assertLessEqual(residual, 1.0e-7)


if __name__ == "__main__":
    unittest.main()
