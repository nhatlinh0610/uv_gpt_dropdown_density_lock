"""Regression tests for Pack V2 Lock Overlapping, multi-angle rotation, and concave nesting."""
from __future__ import annotations

import ast
import json
import math
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[2]
UV_GPT_DIR = REPO_ROOT / "uv_gpt"
if str(UV_GPT_DIR) not in sys.path:
    sys.path.insert(0, str(UV_GPT_DIR))

import pack_geometry  # noqa: E402
import pack_v2_core  # noqa: E402


def _make_island_faces(face_id_start, vert_id_start, edge_id_start, selected, polys):
    """Build disconnected/connected quad or poly faces for synthetic Pack V2 snapshots."""
    faces = []
    f_idx = face_id_start
    v_idx = vert_id_start
    e_idx = edge_id_start
    for pts in polys:
        loops = []
        for u, v in pts:
            loops.append((v_idx, e_idx, float(u), float(v)))
            v_idx += 1
            e_idx += 1
        faces.append((f_idx, bool(selected), False, tuple(loops)))
        f_idx += 1
    return faces, f_idx, v_idx, e_idx


def _make_l_island_faces(face_id_start, vert_id_start, edge_id_start, selected, origin=(0.0, 0.0), arm=0.8, thick=0.2):
    """Build a 3-quad manifold L-shaped island sharing internal vertices and edges."""
    ox, oy = origin
    # Shared vertex grid:
    # (0, 2*t) --- (t, 2*t)? Wait: build exact 3 quads: corner [0,t]x[0,t], right [t,arm]x[0,t], up [0,t]x[t,arm]
    v00 = vert_id_start + 0
    v10 = vert_id_start + 1
    v20 = vert_id_start + 2
    v01 = vert_id_start + 3
    v11 = vert_id_start + 4
    v21 = vert_id_start + 5
    v02 = vert_id_start + 6
    v12 = vert_id_start + 7

    e_base = edge_id_start
    e00_10 = e_base + 0
    e10_11 = e_base + 1
    e11_01 = e_base + 2
    e01_00 = e_base + 3
    e10_20 = e_base + 4
    e20_21 = e_base + 5
    e21_11 = e_base + 6
    e11_12 = e_base + 7
    e12_02 = e_base + 8
    e02_01 = e_base + 9

    p00 = (ox, oy)
    p10 = (ox + thick, oy)
    p20 = (ox + arm, oy)
    p01 = (ox, oy + thick)
    p11 = (ox + thick, oy + thick)
    p21 = (ox + arm, oy + thick)
    p02 = (ox, oy + arm)
    p12 = (ox + thick, oy + arm)

    faces = [
        (
            face_id_start + 0,
            bool(selected),
            False,
            (
                (v00, e00_10, *p00),
                (v10, e10_11, *p10),
                (v11, e11_01, *p11),
                (v01, e01_00, *p01),
            ),
        ),
        (
            face_id_start + 1,
            bool(selected),
            False,
            (
                (v10, e10_20, *p10),
                (v20, e20_21, *p20),
                (v21, e21_11, *p21),
                (v11, e10_11, *p11),
            ),
        ),
        (
            face_id_start + 2,
            bool(selected),
            False,
            (
                (v01, e11_01, *p01),
                (v11, e11_12, *p11),
                (v12, e12_02, *p12),
                (v02, e02_01, *p02),
            ),
        ),
    ]
    return faces, face_id_start + 3, vert_id_start + 8, edge_id_start + 10


def _build_snapshot(faces, **options):
    opts = {
        "mode": "selected",
        "margin": 0.005,
        "rotation_mode": "CARDINAL",
        "preserve_stacks": True,
        "lock_overlapping": "ANY",
        "scale_to_fit": True,
        "axis_kind": "U",
    }
    opts.update(options)
    return {
        "schema": pack_v2_core.SCHEMA,
        "faces": tuple(faces),
        "world_area_by_face": {f[0]: 1.0 for f in faces},
        "options": opts,
    }


def _writes_map(result):
    return {(int(f), int(l)): (float(u), float(v)) for f, l, u, v in result["writes"]}


def _apply_writes_to_snapshot(snapshot, result):
    wmap = _writes_map(result)
    new_faces = []
    for fidx, sel, hide, loops in snapshot["faces"]:
        new_loops = []
        for lidx, (vidx, eidx, u, v) in enumerate(loops):
            nu, nv = wmap.get((int(fidx), int(lidx)), (float(u), float(v)))
            new_loops.append((vidx, eidx, nu, nv))
        new_faces.append((fidx, sel, hide, tuple(new_loops)))
    return dict(snapshot, faces=tuple(new_faces))


def _fit_2d_similarity(src_pts, dst_pts):
    """Estimate (scale, cos, sin, tx, ty) mapping src_pts -> dst_pts and return max residual."""
    assert len(src_pts) == len(dst_pts) and len(src_pts) >= 2
    sx0, sy0 = src_pts[0]
    dx0, dy0 = dst_pts[0]
    best_idx = max(
        range(1, len(src_pts)),
        key=lambda k: math.hypot(src_pts[k][0] - sx0, src_pts[k][1] - sy0),
    )
    vx, vy = src_pts[best_idx][0] - sx0, src_pts[best_idx][1] - sy0
    wx, wy = dst_pts[best_idx][0] - dx0, dst_pts[best_idx][1] - dy0
    denom = vx * vx + vy * vy
    assert denom > 1e-12
    a = (wx * vx + wy * vy) / denom
    b = (wy * vx - wx * vy) / denom
    max_err = 0.0
    for (sx, sy), (dx, dy) in zip(src_pts, dst_pts):
        px = dx0 + a * (sx - sx0) - b * (sy - sy0)
        py = dy0 + b * (sx - sx0) + a * (sy - sy0)
        max_err = max(max_err, math.hypot(px - dx, py - dy))
    return math.hypot(a, b), max_err


def _rect(x0, y0, x1, y1):
    return ((x0, y0), (x1, y0), (x1, y1), (x0, y1))


class PackV2OverlapAndNestingTests(unittest.TestCase):
    def test_lock_overlapping_any_locks_heterogeneous_nested_and_crossing_islands(self):
        # Shift all movable islands off-tile so translation, scaling, and rotation all run.
        faces = []
        f_id = v_id = e_id = 0
        # Host A: 1 quad
        f_a, f_id, v_id, e_id = _make_island_faces(
            f_id, v_id, e_id, True, [_rect(2.0, 2.0, 2.4, 2.2)]
        )
        # Nested B: triangle wholly inside A (heterogeneous topology vs quad A)
        f_b, f_id, v_id, e_id = _make_island_faces(
            f_id, v_id, e_id, True, [((2.06, 2.05), (2.18, 2.05), (2.12, 2.16))]
        )
        # Crossing strip C: pentagon crossing A's right edge
        f_c, f_id, v_id, e_id = _make_island_faces(
            f_id,
            v_id,
            e_id,
            True,
            [((2.32, 1.95), (2.48, 1.95), (2.50, 2.06), (2.46, 2.13), (2.32, 2.13))],
        )
        # Separate island D
        f_d, f_id, v_id, e_id = _make_island_faces(
            f_id, v_id, e_id, True, [_rect(3.0, 3.0, 3.3, 3.3)]
        )
        faces.extend(f_a + f_b + f_c + f_d)

        for rot_mode in ("NONE", "CARDINAL", "FREE"):
            snap = _build_snapshot(
                faces,
                mode="selected",
                margin=0.01,
                rotation_mode=rot_mode,
                lock_overlapping="ANY",
                preserve_stacks=True,
                scale_to_fit=True,
            )
            res = pack_v2_core.solve_pack(snap)
            self.assertEqual(res["overlap_followers"], 2, f"rot={rot_mode}")

            wmap = _writes_map(res)
            src_group = []
            dst_group = []
            for fidx, _sel, _hide, loops in f_a + f_b + f_c:
                for lidx, (_vi, _ei, u, v) in enumerate(loops):
                    src_group.append((u, v))
                    dst_group.append(wmap[(fidx, lidx)])

            est_scale, max_err = _fit_2d_similarity(src_group, dst_group)
            self.assertGreater(est_scale, 0.5)
            self.assertLessEqual(max_err, 1e-6, f"rot={rot_mode} drifted overlap group by {max_err}")

            # Verify D stays inside [margin, 1 - margin] and doesn't overlap group
            out_snap = _apply_writes_to_snapshot(snap, res)
            out_islands = list(pack_v2_core._parse_islands(out_snap))
            for isl in out_islands:
                for poly in isl.boundary_loops:
                    for u, v in poly:
                        self.assertGreaterEqual(u, 0.01 - 1e-6)
                        self.assertLessEqual(u, 0.99 + 1e-6)
                        self.assertGreaterEqual(v, 0.01 - 1e-6)
                        self.assertLessEqual(v, 0.99 + 1e-6)
            shape_d = out_islands[-1].boundary_loops
            for group_isl in out_islands[:-1]:
                self.assertFalse(
                    pack_v2_core.shape_sets_conflict(shape_d, group_isl.boundary_loops, margin=0.01 - 1e-6)
                )

        # EXACT mode should NOT merge heterogeneous overlaps A/B/C
        snap_exact = _build_snapshot(
            faces,
            mode="selected",
            margin=0.01,
            rotation_mode="CARDINAL",
            lock_overlapping="EXACT",
            preserve_stacks=True,
        )
        res_exact = pack_v2_core.solve_pack(snap_exact)
        self.assertEqual(res_exact["overlap_followers"], 0)
        out_exact = list(pack_v2_core._parse_islands(_apply_writes_to_snapshot(snap_exact, res_exact)))
        for i in range(len(out_exact)):
            for j in range(i + 1, len(out_exact)):
                self.assertFalse(
                    pack_v2_core.shape_sets_conflict(
                        out_exact[i].boundary_loops,
                        out_exact[j].boundary_loops,
                        margin=0.01 - 1e-6,
                    )
                )

    def test_lock_overlapping_exact_omitted_preserve_flag_still_works(self):
        # Coincident exact stack quads, caller omits preserve_stacks and passes lock_overlapping="EXACT".
        f0, f_id, v_id, e_id = _make_island_faces(0, 0, 0, True, [_rect(0.0, 0.0, 0.3, 0.3)])
        f1, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [_rect(0.0, 0.0, 0.3, 0.3)])
        snap = {
            "schema": pack_v2_core.SCHEMA,
            "faces": tuple(f0 + f1),
            "world_area_by_face": {0: 1.0, 1: 1.0},
            "options": {
                "mode": "selected",
                "margin": 0.01,
                "rotation_mode": "NONE",
                "lock_overlapping": "EXACT",
            },
        }
        res_exact = pack_v2_core.solve_pack(snap)
        self.assertEqual(res_exact["stack_followers"], 1)

        snap_off = dict(snap, options=dict(snap["options"], lock_overlapping="OFF"))
        res_off = pack_v2_core.solve_pack(snap_off)
        self.assertEqual(res_off["stack_followers"], 0)
        self.assertEqual(res_off["overlap_followers"], 0)

    def test_lock_overlapping_any_does_not_merge_flush_adjacent_islands(self):
        f_left, f_id, v_id, e_id = _make_island_faces(0, 0, 0, True, [_rect(0.0, 0.0, 0.4, 0.4)])
        f_right, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [_rect(0.4, 0.0, 0.8, 0.4)])
        snap = _build_snapshot(
            f_left + f_right,
            mode="whole",
            margin=0.05,
            rotation_mode="NONE",
            lock_overlapping="ANY",
        )
        res = pack_v2_core.solve_pack(snap)
        self.assertEqual(res["overlap_followers"], 0)
        out_islands = list(pack_v2_core._parse_islands(_apply_writes_to_snapshot(snap, res)))
        self.assertFalse(
            pack_v2_core.shape_sets_conflict(
                out_islands[0].boundary_loops,
                out_islands[1].boundary_loops,
                margin=0.05 - 1e-6,
            )
        )

    def test_lock_overlapping_any_preserves_exact_followers_on_group_members(self):
        f_host, f_id, v_id, e_id = _make_island_faces(0, 0, 0, True, [_rect(0.0, 0.0, 0.5, 0.3)])
        tri = ((0.1, 0.05), (0.35, 0.05), (0.22, 0.25))
        f_ov1, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [tri])
        f_ov2, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [tri])
        f_sep, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [_rect(1.0, 1.0, 1.25, 1.25)])

        snap = _build_snapshot(
            f_host + f_ov1 + f_ov2 + f_sep,
            mode="selected",
            margin=0.01,
            rotation_mode="FREE",
            lock_overlapping="ANY",
        )
        res = pack_v2_core.solve_pack(snap)
        self.assertEqual(res["stack_followers"], 1)
        self.assertEqual(res["overlap_followers"], 1)

        wmap = _writes_map(res)
        for lidx in range(3):
            p1 = wmap[(f_ov1[0][0], lidx)]
            p2 = wmap[(f_ov2[0][0], lidx)]
            self.assertLessEqual(math.hypot(p1[0] - p2[0], p1[1] - p2[1]), 1e-6)

        src_pts = [(u, v) for _vi, _ei, u, v in f_host[0][3] + f_ov1[0][3]]
        dst_pts = [wmap[(f_host[0][0], k)] for k in range(4)] + [wmap[(f_ov1[0][0], k)] for k in range(3)]
        _, err = _fit_2d_similarity(src_pts, dst_pts)
        self.assertLessEqual(err, 1e-6)

    def test_lock_overlapping_any_works_in_symmetry_and_keep_current_scale(self):
        f_id = v_id = e_id = 0
        # Pairable identical quads
        f_l, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [_rect(0.05, 0.05, 0.25, 0.25)])
        f_r, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [_rect(0.75, 0.05, 0.95, 0.25)])
        # Host quad + heterogeneous triangle decal overlapping at center-ish zone
        f_h, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [_rect(0.35, 0.40, 0.65, 0.60)])
        f_d, f_id, v_id, e_id = _make_island_faces(
            f_id, v_id, e_id, True, [((0.40, 0.44), (0.58, 0.44), (0.49, 0.56))]
        )
        all_faces = f_l + f_r + f_h + f_d

        # Symmetry mode
        snap_sym = _build_snapshot(
            all_faces,
            mode="symmetry",
            margin=0.01,
            rotation_mode="CARDINAL",
            lock_overlapping="ANY",
            axis_kind="U",
        )
        res_sym = pack_v2_core.solve_pack(snap_sym)
        self.assertEqual(res_sym["overlap_followers"], 1)
        wmap_sym = _writes_map(res_sym)
        src_grp = [(u, v) for _vi, _ei, u, v in f_h[0][3] + f_d[0][3]]
        dst_grp = [wmap_sym[(f_h[0][0], k)] for k in range(4)] + [wmap_sym[(f_d[0][0], k)] for k in range(3)]
        _, err_sym = _fit_2d_similarity(src_grp, dst_grp)
        self.assertLessEqual(err_sym, 1e-6)

        # Keep Current Scale mode (already fits in 0-1 without collisions between distinct groups)
        snap_keep = _build_snapshot(
            all_faces,
            mode="selected",
            margin=0.01,
            rotation_mode="NONE",
            lock_overlapping="ANY",
            scale_to_fit=False,
        )
        res_keep = pack_v2_core.solve_pack(snap_keep)
        self.assertAlmostEqual(res_keep["scale"], 1.0, places=6)
        self.assertEqual(res_keep["overlap_followers"], 1)
        wmap_keep = _writes_map(res_keep)
        dst_keep = [wmap_keep[(f_h[0][0], k)] for k in range(4)] + [wmap_keep[(f_d[0][0], k)] for k in range(3)]
        _, err_keep = _fit_2d_similarity(src_grp, dst_keep)
        self.assertLessEqual(err_keep, 1e-6)

    def test_concave_l_cavity_and_cardinal_wedge_interlocking(self):
        # 1) Concave L-shape (0.8 x 0.8, thickness 0.2) + 0.45 x 0.45 square sitting inside cavity -> scale > 1.15
        f_l, f_id, v_id, e_id = _make_l_island_faces(0, 0, 0, True, origin=(0.0, 0.0), arm=0.8, thick=0.2)
        f_sq, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [_rect(2.0, 2.0, 2.45, 2.45)])
        snap_l = _build_snapshot(
            f_l + f_sq,
            mode="selected",
            margin=0.01,
            rotation_mode="CARDINAL",
            lock_overlapping="ANY",
        )
        res_l = pack_v2_core.solve_pack(snap_l)
        self.assertGreater(res_l["scale"], 1.15)
        out_l = list(pack_v2_core._parse_islands(_apply_writes_to_snapshot(snap_l, res_l)))
        self.assertFalse(
            pack_v2_core.shape_sets_conflict(
                out_l[0].boundary_loops,
                out_l[1].boundary_loops,
                margin=0.01 - 1e-6,
            )
        )

        # 2) Two identical right-triangle wedges (0.8 x 0.8):
        # NONE can't flip the second wedge 180 deg, whereas CARDINAL flips 180 deg and fits tighter.
        w0 = ((0.0, 0.0), (0.8, 0.0), (0.0, 0.8))
        w1 = ((1.0, 0.0), (1.8, 0.0), (1.0, 0.8))
        f_w0, f_id, v_id, e_id = _make_island_faces(0, 0, 0, True, [w0])
        f_w1, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [w1])
        snap_none = _build_snapshot(f_w0 + f_w1, margin=0.01, rotation_mode="NONE", lock_overlapping="OFF")
        snap_card = _build_snapshot(f_w0 + f_w1, margin=0.01, rotation_mode="CARDINAL", lock_overlapping="OFF")
        res_none = pack_v2_core.solve_pack(snap_none)
        res_card = pack_v2_core.solve_pack(snap_card)
        self.assertGreater(res_card["scale"], res_none["scale"] * 1.35)
        out_wedges = list(pack_v2_core._parse_islands(_apply_writes_to_snapshot(snap_card, res_card)))
        self.assertFalse(
            pack_v2_core.shape_sets_conflict(
                out_wedges[0].boundary_loops,
                out_wedges[1].boundary_loops,
                margin=0.01 - 1e-6,
            )
        )

    def test_free_rotation_aligns_tilted_strips_tighter_than_cardinal(self):
        # Four thick 45-deg tilted rectangular bars (length 0.85, thickness 0.16).
        # At 45 deg they cannot all pack at full length into 0..1 corner slices in CARDINAL,
        # whereas FREE uses min_area_rect_angle to straighten them horizontal and stack parallel.
        c = math.cos(math.radians(45.0))
        s = math.sin(math.radians(45.0))

        def _tilted_bar(dx, dy, L=0.85, W=0.16):
            local = ((0.0, 0.0), (L, 0.0), (L, W), (0.0, W))
            return tuple((c * x - s * y + dx, s * x + c * y + dy) for x, y in local)

        faces = []
        f_id = v_id = e_id = 0
        for idx in range(4):
            f_bar, f_id, v_id, e_id = _make_island_faces(
                f_id, v_id, e_id, True, [_tilted_bar(idx * 1.5, 0.0)]
            )
            faces.extend(f_bar)

        snap_card = _build_snapshot(faces, margin=0.01, rotation_mode="CARDINAL", lock_overlapping="OFF")
        snap_free = _build_snapshot(faces, margin=0.01, rotation_mode="FREE", lock_overlapping="OFF")
        res_card = pack_v2_core.solve_pack(snap_card)
        res_free = pack_v2_core.solve_pack(snap_free)
        self.assertGreater(res_free["scale"], res_card["scale"] * 1.15)

        out_free = list(pack_v2_core._parse_islands(_apply_writes_to_snapshot(snap_free, res_free)))
        for i in range(len(out_free)):
            for j in range(i + 1, len(out_free)):
                self.assertFalse(
                    pack_v2_core.shape_sets_conflict(
                        out_free[i].boundary_loops,
                        out_free[j].boundary_loops,
                        margin=0.01 - 1e-6,
                    )
                )

    def test_pack_geometry_min_area_rect_and_strict_overlap(self):
        # Concave C-crescent overlap without edge crossing vs flush 0-gap square touching
        sq_left = ((_rect(0.0, 0.0, 0.5, 0.5)),)
        sq_flush = ((_rect(0.5, 0.0, 1.0, 0.5)),)
        sq_nested = ((_rect(0.1, 0.1, 0.4, 0.4)),)
        self.assertFalse(pack_geometry.shapes_strictly_overlap(sq_left, sq_flush))
        self.assertTrue(pack_geometry.shapes_strictly_overlap(sq_left, sq_nested))

        # Concave C-shape overlapping another strip inside C's solid wall
        c_shape = (
            ((0.0, 0.0), (0.6, 0.0), (0.6, 0.2), (0.2, 0.2), (0.2, 0.4), (0.6, 0.4), (0.6, 0.6), (0.0, 0.6)),
        )
        inside_bar = ((_rect(0.02, 0.05, 0.15, 0.55)),)
        gap_bar = ((_rect(0.25, 0.22, 0.55, 0.38)),)
        self.assertTrue(pack_geometry.shapes_strictly_overlap(c_shape, inside_bar))
        self.assertFalse(pack_geometry.shapes_strictly_overlap(c_shape, gap_bar))

        # Microscopic float32 rounding graze (2e-6) must NOT fuse separate touching islands.
        sq_graze = ((_rect(0.5 - 2.0e-6, 0.0, 1.0, 0.5)),)
        self.assertFalse(pack_geometry.shapes_strictly_overlap(sq_left, sq_graze))

    def test_pack_v2_worker_subprocess_roundtrip(self):
        f_a, f_id, v_id, e_id = _make_island_faces(0, 0, 0, True, [_rect(0.0, 0.0, 0.4, 0.3)])
        f_b, f_id, v_id, e_id = _make_island_faces(
            f_id, v_id, e_id, True, [((0.1, 0.05), (0.5, 0.05), (0.3, 0.25))]
        )
        snap = _build_snapshot(
            f_a + f_b,
            mode="selected",
            margin=0.01,
            rotation_mode="CARDINAL",
            lock_overlapping="ANY",
        )
        worker_path = UV_GPT_DIR / "pack_v2_worker.py"
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            in_pkl = tmp / "input.pkl"
            out_pkl = tmp / "output.pkl"
            prog_json = tmp / "progress.json"
            with in_pkl.open("wb") as fh:
                pickle.dump(snap, fh, protocol=5)
            proc = subprocess.run(
                [sys.executable, "-B", str(worker_path), str(in_pkl), str(out_pkl), str(prog_json)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr or proc.stdout)
            with out_pkl.open("rb") as fh:
                payload = pickle.load(fh)
            self.assertTrue(payload.get("ok"), msg=payload.get("error"))
            result = payload["result"]
            self.assertEqual(result["overlap_followers"], 1)
            self.assertGreater(result["scale"], 1.0)
            self.assertTrue(prog_json.exists())

    def test_ui_properties_and_snapshot_contracts(self):
        props_text = (UV_GPT_DIR / "properties.py").read_text(encoding="utf-8")
        ui_text = (UV_GPT_DIR / "ui.py").read_text(encoding="utf-8")
        pack_tools_text = (UV_GPT_DIR / "pack_tools.py").read_text(encoding="utf-8")

        ast.parse(props_text)
        ast.parse(ui_text)
        ast.parse(pack_tools_text)

        self.assertIn("pack_lock_overlapping: EnumProperty(", props_text)
        for token in ('"ANY"', '"EXACT"', '"OFF"', 'default="ANY"', '"CARDINAL"', '"FREE"'):
            self.assertIn(token, props_text)

        unselected_idx = props_text.index("pack_selected_unselected_mode")
        hidden_idx = props_text.index('options={"HIDDEN"}', unselected_idx)
        preserve_idx = props_text.index("pack_preserve_stacks: BoolProperty(", unselected_idx)
        self.assertLess(unselected_idx, hidden_idx)
        self.assertLess(hidden_idx, preserve_idx)

        self.assertIn('box.prop(settings, "pack_lock_overlapping")', ui_text)
        self.assertIn('settings.pack_lock_overlapping == "EXACT"', ui_text)
        self.assertIn('_LAST_PACK_V2_REPORT', ui_text)
        self.assertIn('"lock_overlapping": lock_mode', pack_tools_text)
        self.assertIn("_show_pack_v2_popup", pack_tools_text)

        # Preserve historical enum indices in properties.py for existing .blend files
        rot_section = props_text[props_text.index("rotation_mode: EnumProperty("):props_text.index("pack_selected_lock_density")]
        self.assertLess(rot_section.index('"NONE"'), rot_section.index('"ROT_90"'))
        self.assertLess(rot_section.index('"ROT_90"'), rot_section.index('"ROT_180"'))
        self.assertLess(rot_section.index('"ROT_180"'), rot_section.index('"CARDINAL"'))
        self.assertLess(rot_section.index('"CARDINAL"'), rot_section.index('"FREE"'))
        self.assertIn("if msg.startswith(prefix) else", pack_tools_text)

    def test_hollow_ring_hole_nesting_and_lock_overlapping_distinction(self):
        # Build a 4-quad manifold hollow square ring [0, 0.66]x[0, 0.66] with inner hole [0.10, 0.56]x[0.10, 0.56].
        # Place a 0.36x0.36 square inside the hole and two 0.30x0.30 squares outside.
        ox, oy, S, t = 0.0, 0.0, 0.66, 0.10
        outer = [(ox, oy), (ox + S, oy), (ox + S, oy + S), (ox, oy + S)]
        inner = [(ox + t, oy + t), (ox + S - t, oy + t), (ox + S - t, oy + S - t), (ox + t, oy + S - t)]
        vo = [0, 1, 2, 3]
        vi = [4, 5, 6, 7]
        eo = [0, 1, 2, 3]
        ei = [4, 5, 6, 7]
        em = [8, 9, 10, 11]
        ring_faces = []
        for k in range(4):
            nk = (k + 1) % 4
            ring_faces.append((
                k,
                True,
                False,
                (
                    (vo[k], eo[k], *outer[k]),
                    (vo[nk], em[nk], *outer[nk]),
                    (vi[nk], ei[k], *inner[nk]),
                    (vi[k], em[k], *inner[k]),
                ),
            ))
        f_in, f_id, v_id, e_id = _make_island_faces(4, 8, 12, True, [_rect(0.15, 0.15, 0.51, 0.51)])
        f_out1, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [_rect(2.0, 2.0, 2.30, 2.30)])
        f_out2, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [_rect(3.0, 2.0, 3.30, 2.30)])
        snap = _build_snapshot(
            ring_faces + f_in + f_out1 + f_out2,
            mode="whole",
            margin=0.01,
            rotation_mode="CARDINAL",
            lock_overlapping="ANY",
            scale_to_fit=True,
        )
        res = pack_v2_core.solve_pack(snap)
        # Island in the empty hole must NOT be falsely locked to the ring!
        self.assertEqual(res["overlap_followers"], 0)
        # Without nesting inside the ring's hole, 0.66 + 0.36 > 1.0 forces scale <= 0.90; nesting yields >= 0.96.
        self.assertGreater(res["scale"], 0.96)
        out_islands = list(pack_v2_core._parse_islands(_apply_writes_to_snapshot(snap, res)))
        for i in range(len(out_islands)):
            for j in range(i + 1, len(out_islands)):
                self.assertFalse(
                    pack_v2_core.shape_sets_conflict(
                        out_islands[i].boundary_loops,
                        out_islands[j].boundary_loops,
                        margin=0.01 - 1e-6,
                    )
                )

    def test_thin_ribbon_and_collinear_corner_overlap_locked_rigidly(self):
        # Ultra-thin overlapping ribbons (length 0.8, width 0.0002) with different face counts
        r1 = (((0.1, 0.2), (0.9, 0.2), (0.9, 0.2002), (0.1, 0.2002)),)
        r2 = (((0.2, 0.2), (0.7, 0.2), (0.7, 0.2002), (0.2, 0.2002)),)
        self.assertTrue(pack_geometry.shapes_strictly_overlap(r1, r2))

        # Collinear corner overlap without strict edge crossing
        c1 = (((0.0, 0.0), (0.4, 0.0), (0.4, 0.4), (0.0, 0.4)),)
        c2 = (((0.0, 0.0), (0.2, 0.0), (0.2, 0.2), (0.0, 0.2)),)
        self.assertTrue(pack_geometry.shapes_strictly_overlap(c1, c2))

        # Pack Selected with off-tile static blocker must NOT block tile borders
        f_m, f_id, v_id, e_id = _make_island_faces(0, 0, 0, True, [_rect(0.0, 0.0, 0.90, 0.90)])
        f_off, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, False, [_rect(1.5, 0.1, 2.2, 0.9)])
        snap_off = _build_snapshot(
            f_m + f_off,
            mode="selected",
            margin=0.01,
            rotation_mode="NONE",
            lock_overlapping="ANY",
            scale_to_fit=True,
        )
        res_off = pack_v2_core.solve_pack(snap_off)
        self.assertGreater(res_off["scale"], 1.05)

        # Impossible scale_to_fit=True when static blocker covers the whole tile raises RuntimeError cleanly
        f_full, _, _, _ = _make_island_faces(f_id, v_id, e_id, False, [_rect(0.0, 0.0, 1.0, 1.0)])
        snap_blocked = _build_snapshot(
            f_m + f_full,
            mode="selected",
            margin=0.01,
            rotation_mode="NONE",
            lock_overlapping="OFF",
            scale_to_fit=True,
        )
        with self.assertRaises(RuntimeError):
            pack_v2_core.solve_pack(snap_blocked)

    def test_dense_accessories_keep_scale_fit_and_locked_overlaps(self):
        snap_path = REPO_ROOT / ".test_runtime" / "accessories_20261007" / "post_pack_whole_cardinal_snapshot.json"
        if not snap_path.is_file():
            self.skipTest("accessories snapshot unavailable")
        base = json.loads(snap_path.read_text(encoding="utf-8"))

        # 1) Keep Current Scale (scale_to_fit=False) previously threw RuntimeError; must now pack cleanly at 1.0
        snap_keep = dict(
            base,
            options=dict(
                base["options"],
                mode="whole",
                rotation_mode="CARDINAL",
                lock_overlapping="ANY",
                preserve_stacks=True,
                scale_to_fit=False,
            ),
        )
        res_keep = pack_v2_core.solve_pack(snap_keep)
        self.assertEqual(res_keep["scale"], 1.0)
        self.assertEqual(res_keep["movable_count"], 149)

        # Verify zero collisions & tile bounds on output
        margin = float(snap_keep["options"]["margin"])
        out_islands = list(pack_v2_core._parse_islands(_apply_writes_to_snapshot(snap_keep, res_keep)))
        idx = pack_v2_core.SpatialIndex(44)
        for isl in out_islands:
            self.assertTrue(pack_v2_core._shape_fit_tile(isl.boundary_loops, margin - 1e-6))
            b = pack_v2_core.shape_bounds(isl.boundary_loops)
            for other_sh, _ob, _oe, _ in idx.query_entries(b, margin=margin):
                self.assertFalse(pack_v2_core.shape_sets_conflict(isl.boundary_loops, other_sh, margin=margin - 1e-6))
            idx.insert(isl.boundary_loops, bounds=b)

        # 2) Add heterogeneous overlapping decals onto dense islands + verify Lock Overlapping="ANY" + scale_to_fit=True
        max_f = max(f[0] for f in base["faces"]) + 1
        max_v = max(lp[0] for f in base["faces"] for lp in f[3]) + 1
        max_e = max(lp[1] for f in base["faces"] for lp in f[3]) + 1
        in_islands = list(pack_v2_core._parse_islands(snap_keep))
        # Pick 2 large islands and place overlapping triangle decals inside their interiors
        ov_faces = []
        ov_src_pts = []
        target_islands = sorted(in_islands, key=lambda x: -x.uv_area)[:2]
        for isl in target_islands:
            # Find a face centroid of isl
            first_f_idx = isl.face_key[0]
            f_rec = next(f for f in base["faces"] if f[0] == first_f_idx)
            pts = [(lp[2], lp[3]) for lp in f_rec[3]]
            cx = sum(p[0] for p in pts) / len(pts)
            cy = sum(p[1] for p in pts) / len(pts)
            decal = ((cx - 0.008, cy - 0.008), (cx + 0.008, cy - 0.008), (cx, cy + 0.010))
            f_dec, max_f, max_v, max_e = _make_island_faces(max_f, max_v, max_e, True, [decal])
            ov_faces.extend(f_dec)
            ov_src_pts.append((isl, f_dec))

        world_areas = dict(base["world_area_by_face"])
        for f in ov_faces:
            world_areas[str(f[0])] = 0.1
        dense_ov_snap = dict(
            base,
            faces=tuple(base["faces"]) + tuple(ov_faces),
            world_area_by_face=world_areas,
            options=dict(
                base["options"],
                mode="whole",
                rotation_mode="CARDINAL",
                lock_overlapping="ANY",
                preserve_stacks=True,
                scale_to_fit=True,
            ),
        )
        res_ov = pack_v2_core.solve_pack(dense_ov_snap)
        self.assertEqual(res_ov["overlap_followers"], 2)
        self.assertGreaterEqual(res_ov["scale"], 0.97)
        wmap = _writes_map(res_ov)
        for host_isl, f_dec in ov_src_pts:
            src_pts = list(host_isl.loop_uvs.values()) + [(u, v) for _vi, _ei, u, v in f_dec[0][3]]
            dst_pts = [wmap[k] for k in host_isl.loop_uvs] + [wmap[(f_dec[0][0], k)] for k in range(3)]
            _, err = _fit_2d_similarity(src_pts, dst_pts)
            self.assertLessEqual(err, 1e-6)

    def test_dual_bitmask_e_poly_retains_middle_prong_collision_avoidance(self):
        e_poly = (
            (0.0, 0.0), (0.80, 0.0), (0.80, 0.05), (0.08, 0.05),
            (0.08, 0.38), (0.80, 0.38), (0.80, 0.43), (0.08, 0.43),
            (0.08, 0.75), (0.80, 0.75), (0.80, 0.80), (0.0, 0.80),
        )
        tile = pack_v2_core._DualBitmaskTile(288, margin=0.003)
        _pw, ph, rows = tile.prepare_piece(((e_poly,),), 0.80, 0.80, 1.0)
        self.assertGreater(ph, 200)
        mid_rows = [dr for dr, _runs in rows if 105 <= dr <= 125]
        self.assertTrue(mid_rows, f"Expected middle prong rows in 105..125, got {[dr for dr, _ in rows]}")

        # Pack the E-poly as static (unselected) obstacle alongside a small movable quad
        # aligned right on the middle prong y-band, ensuring no overlap occurs.
        e_faces, nf, nv, ne = _make_island_faces(0, 0, 0, False, [e_poly])
        # Fill interior bands minimally so the E-poly stays unmovable; put movable rects
        # that must nest inside the wide bays without hitting the middle prong.
        bay_rects = [_rect(0.20 + 0.14 * i, 0.10, 0.32 + 0.14 * i, 0.30) for i in range(4)]
        bay_rects += [_rect(0.20 + 0.14 * i, 0.50, 0.32 + 0.14 * i, 0.70) for i in range(4)]
        all_faces = list(e_faces)
        for r in bay_rects:
            mf, nf, nv, ne = _make_island_faces(nf, nv, ne, True, [r])
            all_faces.extend(mf)
        snap = _build_snapshot(
            all_faces,
            mode="selected",
            margin=0.003,
            rotation_mode="NONE",
            preserve_stacks=False,
            lock_overlapping="ANY",
            scale_to_fit=False,
        )
        res = pack_v2_core.solve_pack(snap)
        wmap = _writes_map(res)
        for fid, sel, _pin, loops in all_faces:
            if not sel:
                continue
            moved_poly = tuple(wmap[(fid, k)] for k in range(len(loops)))
            self.assertFalse(
                pack_geometry.polygon_sets_conflict((e_poly,), (moved_poly,), margin=0.002),
                f"Movable rect overlapped E-poly middle prong/frame: {moved_poly}",
            )

    def test_high_vertex_count_tooth_overlap_detection_and_locking(self):
        import math

        pts = []
        n_arc = 190
        for i in range(n_arc):
            ang = 2.0 * math.pi * i / n_arc
            pts.append((0.5 + 0.3 * math.cos(ang), 0.5 + 0.3 * math.sin(ang)))
        poly_big = tuple(pts + [(0.802, 0.4995), (0.805, 0.5), (0.802, 0.5005)])
        tooth = ((0.8045, 0.4998), (0.820, 0.4998), (0.820, 0.5002), (0.8045, 0.5002))
        self.assertTrue(pack_geometry.shapes_strictly_overlap((poly_big,), (tooth,)))

        f_big, nf, nv, ne = _make_island_faces(0, 0, 0, True, [poly_big])
        f_tooth, _, _, _ = _make_island_faces(nf, nv, ne, True, [tooth])
        snap = _build_snapshot(
            f_big + f_tooth,
            mode="whole",
            margin=0.004,
            rotation_mode="CARDINAL",
            preserve_stacks=False,
            lock_overlapping="ANY",
            scale_to_fit=True,
        )
        res = pack_v2_core.solve_pack(snap)
        self.assertEqual(res["overlap_followers"], 1)
        wmap = _writes_map(res)
        src_pts = [(u, v) for _vi, _ei, u, v in f_big[0][3]] + [(u, v) for _vi, _ei, u, v in f_tooth[0][3]]
        dst_pts = [wmap[(f_big[0][0], k)] for k in range(len(poly_big))] + [
            wmap[(f_tooth[0][0], k)] for k in range(len(tooth))
        ]
        _, err = _fit_2d_similarity(src_pts, dst_pts)
        self.assertLessEqual(err, 1e-6)

    def test_dense_accessories_scrambled_free_and_cardinal_tightness(self):
        import copy
        import json
        import math
        from pathlib import Path

        snap_path = (
            Path(__file__).resolve().parents[2]
            / ".test_runtime"
            / "accessories_20261007"
            / "post_pack_whole_cardinal_snapshot.json"
        )
        with snap_path.open("r", encoding="utf-8") as fh:
            base = json.load(fh)
        islands = pack_v2_core._parse_islands(base)
        face_to_island = {}
        island_centroids = {}
        for idx, isl in enumerate(islands):
            xs = [u for u, _v in isl.loop_uvs.values()]
            ys = [v for _u, v in isl.loop_uvs.values()]
            island_centroids[idx] = (0.5 * (min(xs) + max(xs)), 0.5 * (min(ys) + max(ys)))
            for fid, _ in isl.loop_uvs:
                face_to_island[fid] = idx

        def scrambled(deg_fn, rot_mode):
            s = copy.deepcopy(base)
            new_faces = []
            for fid, _sel, pin, loops in s["faces"]:
                idx = face_to_island.get(fid)
                if idx is None:
                    new_faces.append([fid, False, pin, loops])
                    continue
                cx, cy = island_centroids[idx]
                ox = (idx % 13) * 1.5
                oy = (idx // 13) * 1.5
                rad = math.radians(deg_fn(idx))
                c, sn = math.cos(rad), math.sin(rad)
                new_loops = [
                    [vi, ei, ox + (u - cx) * c - (v - cy) * sn, oy + (u - cx) * sn + (v - cy) * c]
                    for vi, ei, u, v in loops
                ]
                new_faces.append([fid, True, pin, new_loops])
            s["faces"] = new_faces
            s["options"] = {
                "mode": "whole",
                "margin": 0.003,
                "rotation_mode": rot_mode,
                "lock_overlapping": "ANY",
                "preserve_stacks": False,
                "scale_to_fit": True,
            }
            return s

        res_card = pack_v2_core.solve_pack(scrambled(lambda i: 90.0 * (i % 4), "CARDINAL"))
        self.assertGreaterEqual(res_card["scale"], 1.0)
        res_free_37 = pack_v2_core.solve_pack(scrambled(lambda i: 37.0 + 90.0 * (i % 4), "FREE"))
        self.assertGreaterEqual(res_free_37["scale"], 1.0)
        res_card_37 = pack_v2_core.solve_pack(scrambled(lambda i: 37.0 + 90.0 * (i % 4), "CARDINAL"))
        self.assertGreater(res_free_37["scale"], res_card_37["scale"])

    def test_symmetry_cardinal_uses_combined_locked_overlap_bounds(self):
        # Host is wider than tall (0.16 x 0.08), while the locked overlap member extends
        # vertically (0.08 x 0.36) so the rigid combined group is taller than wide.
        host = _rect(0.10, 0.20, 0.26, 0.28)
        tall_ov = _rect(0.14, 0.10, 0.22, 0.46)
        f_host, nf, nv, ne = _make_island_faces(0, 0, 0, True, [host])
        f_ov, _, _, _ = _make_island_faces(nf, nv, ne, True, [tall_ov])
        snap = _build_snapshot(
            f_host + f_ov,
            mode="symmetry",
            margin=0.005,
            rotation_mode="CARDINAL",
            preserve_stacks=False,
            lock_overlapping="ANY",
            scale_to_fit=False,
            axis_kind="U",
        )
        res = pack_v2_core.solve_pack(snap)
        self.assertEqual(res["overlap_followers"], 1)
        wmap = _writes_map(res)
        dst_pts = [wmap[(f_host[0][0], k)] for k in range(4)] + [wmap[(f_ov[0][0], k)] for k in range(4)]
        src_pts = list(host) + list(tall_ov)
        _, err = _fit_2d_similarity(src_pts, dst_pts)
        self.assertLessEqual(err, 1e-6)
        xs = [p[0] for p in dst_pts]
        ys = [p[1] for p in dst_pts]
        self.assertGreater(max(xs) - min(xs), max(ys) - min(ys))

    def test_v3_hollow_horseshoe_and_frame_nesting_uniform_scaleup_and_report_hint(self):
        # Build a hollow U-horseshoe (width 0.42, open inner bay), a hollow square frame
        # (closed interior hole), and small islands that must nest inside both pockets so
        # scale exceeds non-nesting limits with zero O(N^2) conflicts and positive coverage_pct.
        faces = []
        f_id = v_id = e_id = 0
        # 1) U-horseshoe (outer 0..0.44 x 0..0.44 with deep 0.28x0.28 pocket open at top)
        u_poly = (
            (0.0, 0.0), (0.44, 0.0), (0.44, 0.44), (0.36, 0.44),
            (0.36, 0.08), (0.08, 0.08), (0.08, 0.44), (0.0, 0.44),
        )
        f_u, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [u_poly])
        faces.extend(f_u)
        # 2) Closed hollow frame [0.50, 0.94] x [0.0, 0.44] with hole [0.58, 0.86] x [0.08, 0.36]
        ox, oy, S, t = 0.50, 0.0, 0.44, 0.08
        outer = [(ox, oy), (ox + S, oy), (ox + S, oy + S), (ox, oy + S)]
        inner = [(ox + t, oy + t), (ox + S - t, oy + t), (ox + S - t, oy + S - t), (ox + t, oy + S - t)]
        for k in range(4):
            nk = (k + 1) % 4
            faces.append((
                f_id + k,
                True,
                False,
                (
                    (v_id + k, e_id + k, *outer[k]),
                    (v_id + nk, e_id + 8 + nk, *outer[nk]),
                    (v_id + 4 + nk, e_id + 4 + k, *inner[nk]),
                    (v_id + 4 + k, e_id + 8 + k, *inner[k]),
                ),
            ))
        f_id += 4
        v_id += 8
        e_id += 12
        # 3) Large solid blocks + small squares that can fit inside the frame hole / U pocket
        for rect in (
            _rect(1.2, 0.0, 1.62, 0.42),
            _rect(1.7, 0.0, 2.12, 0.42),
            _rect(1.2, 0.6, 1.40, 0.80),
            _rect(1.5, 0.6, 1.70, 0.80),
        ):
            mf, f_id, v_id, e_id = _make_island_faces(f_id, v_id, e_id, True, [rect])
            faces.extend(mf)

        snap = _build_snapshot(
            faces,
            mode="whole",
            margin=0.004,
            rotation_mode="CARDINAL",
            lock_overlapping="ANY",
            scale_to_fit=True,
        )
        res = pack_v2_core.solve_pack(snap)
        self.assertGreater(res["scale"], 1.04)
        self.assertGreater(res["coverage_pct"], 55.0)
        out_islands = list(pack_v2_core._parse_islands(_apply_writes_to_snapshot(snap, res)))
        for i in range(len(out_islands)):
            self.assertTrue(pack_v2_core._shape_fit_tile(out_islands[i].boundary_loops, 0.004 - 1e-6))
            for j in range(i + 1, len(out_islands)):
                self.assertFalse(
                    pack_v2_core.shape_sets_conflict(
                        out_islands[i].boundary_loops,
                        out_islands[j].boundary_loops,
                        margin=0.004 - 1e-6,
                    )
                )

        # 4) Verify _try_uniform_scale_up expands cleanly to border and preserves static safety gate
        f_a, f_id2, v_id2, e_id2 = _make_island_faces(0, 0, 0, True, [_rect(0.01, 0.01, 0.41, 0.41)])
        f_b, _, _, _ = _make_island_faces(f_id2, v_id2, e_id2, True, [_rect(0.43, 0.01, 0.83, 0.41)])
        unit_a = pack_v2_core.PackUnit(key=(0,), representative=list(pack_v2_core._parse_islands(_build_snapshot(f_a)))[0])
        unit_b = pack_v2_core.PackUnit(key=(1,), representative=list(pack_v2_core._parse_islands(_build_snapshot(f_b)))[0])
        pack_v2_core._prepare_unit(unit_a, rotation_mode="NONE")
        pack_v2_core._prepare_unit(unit_b, rotation_mode="NONE")
        up_scale, up_pl = pack_v2_core._try_uniform_scale_up(
            [unit_a, unit_b],
            {(0,): (0.01, 0.01), (1,): (0.43, 0.01)},
            1.0,
            0.005,
        )
        self.assertGreater(up_scale, 1.15)
        self.assertIn((0,), up_pl)

        # 5) Keep Current Scale warning contract in pack_tools.py
        pack_tools_text = (UV_GPT_DIR / "pack_tools.py").read_text(encoding="utf-8")
        self.assertIn("Keep Current Scale is ON (1.0x)", pack_tools_text)
        self.assertIn("coverage_pct", pack_tools_text)

        # 6) Native C++20 DLL availability & Pack UI cleanup contract
        self.assertIsNotNone(pack_v2_core._load_native_pack_lib())
        ui_text = (UV_GPT_DIR / "ui.py").read_text(encoding="utf-8")
        self.assertNotIn("uv_gpt.pack_symmetry", ui_text)
        self.assertNotIn("uv_gpt.center_selected", ui_text)
        self.assertIn("uv_gpt.pack_selected", ui_text)
        self.assertIn("uv_gpt.pack_whole_mesh", ui_text)

    def test_min_square_span_angle_improves_tilted_l_island_free_pack(self):
        # An L-island tilted by 7.5 deg sits halfway between 15-deg grid steps;
        # min_area_rect_angle picks ~45 deg (wide diagonal), whereas
        # min_square_span_angle recovers the -7.5 deg axis-aligned square fit.
        faces, _, _, _ = _make_l_island_faces(
            0, 0, 0, True, origin=(0.05, 0.05), arm=0.90, thick=0.14
        )
        rad = math.radians(7.5)
        c, s = math.cos(rad), math.sin(rad)
        rotated_faces = []
        for f_idx, sel, hide, loops in faces:
            r_loops = tuple(
                (
                    vi,
                    ei,
                    0.5 + c * (u - 0.5) - s * (v - 0.5),
                    0.5 + s * (u - 0.5) + c * (v - 0.5),
                )
                for vi, ei, u, v in loops
            )
            rotated_faces.append((f_idx, sel, hide, r_loops))
        snap = _build_snapshot(
            rotated_faces,
            mode="whole",
            margin=0.003,
            rotation_mode="FREE",
            lock_overlapping="EXACT",
            scale_to_fit=True,
        )
        res = pack_v2_core.solve_pack(snap)
        self.assertGreater(res["scale"], 1.04)
        out_islands = list(pack_v2_core._parse_islands(_apply_writes_to_snapshot(snap, res)))
        self.assertTrue(pack_v2_core._shape_fit_tile(out_islands[0].boundary_loops, 0.003 - 1e-6))

    def test_pack_v2_never_regresses_valid_source_layout(self):
        # Two tight non-overlapping quads inside [0.003, 0.997] must never
        # regress below scale = 1.0 when scale_to_fit=True.
        f_a, f_id, v_id, e_id = _make_island_faces(0, 0, 0, True, [_rect(0.004, 0.004, 0.495, 0.996)])
        f_b, _, _, _ = _make_island_faces(f_id, v_id, e_id, True, [_rect(0.502, 0.004, 0.996, 0.996)])
        snap = _build_snapshot(
            f_a + f_b,
            mode="whole",
            margin=0.003,
            rotation_mode="FREE",
            lock_overlapping="EXACT",
            scale_to_fit=True,
        )
        res = pack_v2_core.solve_pack(snap)
        self.assertGreaterEqual(res["scale"], 1.0 - 1.0e-6)


if __name__ == "__main__":
    unittest.main()
