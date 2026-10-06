# AI Error Log — uv GPT

File này lưu regression product đã được xác nhận để task sau có memory retest.
`OPEN`/`BLOCKED` phải được xử lý trước release; các mục `RESOLVED` bên dưới là
hai regression thật đã đóng trong v1.2.6. Không ghi validation thành công, lỗi
thao tác người dùng hoặc lỗi tooling không phải product regression.

## Project

- Project name: `uv GPT`
- Project type: Blender add-on Python package
- Main runtime/host: Blender `>= 3.6.0`
- Version reviewed: `1.2.6` (ZIP SHA `4006c09f75cb3653b58a26f3903fc05bb20135376b80767e4a287158b08222e3`)
- Last reviewed: `2026-10-07`

## Regression Checklist

### Historical — Pro Bottom package delivery completed

- Reproduction: Blender MCP, Bottom/UVMap, select all 549 UV islands, Pro.
- Baseline: `boundary_component_branch_or_open`; 0 targets applied.
- Causes: mesh-edge incidence hid internal UV cuts; reversed face winding kept
  the wrong outgoing edge. Live testing also exposed Windows progress replacement
  denial and partial apply when edit-mesh handles crossed the undo-push boundary.
- Source fix: `pro_exact_v2_core.py`, `pro_exact_v2_worker.py`, and
  `stack_tools.py::_pro_exact_v2_apply_pending_updates`.
- Retest: five tests in `test_pro_exact_uv_cuts.py` pass; MCP Blender 5.2.1
  applied 470/470 targets, 79,945 loops with error 0.0; original UVs unchanged.
- Package delivery completed via Quick Reinstall; the current Accessories audit also verifies source/ZIP/installed parity. General Undo/redo remains unverified.
- Evidence: `tests/blender/bottom_pro_mcp_evidence.md`.

- [ ] Review all `OPEN` entries before release/package/build handoff.
- [ ] Check the owning `.context` capsule and any reusable fix knowledge before
  debugging a similar issue.
- [ ] Confirm no known open crash/error remains.

## Entries

No open errors.

### Z2 alignment note — Pro long-run closure remains unverified

- Status: `PENDING_Z3`
- Type: `Blender | external process | verification`
- Scope: Pro Exact V2 external worker, progressive updates and terminal cleanup.
- Current evidence: the authoritative ZIP contains external worker progress,
  skipped/failed reporting, stall/timeout lifecycle and done-hold paths.
- Limitation: Z2 intentionally does not launch Blender or addon workers, so it
  cannot prove that a real 60–100 second Pro run closes correctly on a real
  asset. This is not marked `RESOLVED`; Z3 must run the live timing/cleanup
  gate or record a genuine regression if the issue reproduces.

### 2026-08-21 — Symmetry rotated a positioned target

- Status: `RESOLVED`
- Type: `Blender | UI`
- Affected area/file: `uv_gpt/symmetry_pair.py`, Symmetry block in `uv_gpt/ui.py`
- Environment: Blender 5.0.0 background smoke; locked `cc.blend`, object
  `body pussy -4-2 base chon A big tit done`, active UV `UVMap.002`.
- Symptom: a target that was already correctly oriented became diagonal after
  Symmetry.
- Root cause: the executable route used principal-axis/rotation/scale matching.
- Fix: use anchor/target bounding-box centers and one constant target
  translation; hide the legacy rotation/scale controls from the Symmetry UI.
- Retest: `tests/blender/symmetry_real_mesh_repair.py` — U/V, history→active
  fallback, invalid-selection zero-write and lifecycle passed; fixture SHA was
  unchanged.
- Manual retest checklist:
  - [ ] Confirm visually in the interactive Blender 5.2 UV Editor.
- Notes: this historical position-only repair was later superseded by the user-approved
  `Symmetry / Center Align` contract: 1 region centers/aligns without scale; 2 regions
  intentionally match mirrored rotation + uniform scale. Keep this entry only as regression history.

### 2026-08-21 — Pack/Center rejected stale UV Sync state

- Status: `RESOLVED`
- Type: `Blender | selection state`
- Affected area/file: `uv_gpt/island_tools.py`, `uv_gpt/pack_tools.py`,
  `uv_gpt/transform_tools.py`
- Environment: Blender 5.0.0 background smoke with
  `use_uv_select_sync=True` and `bm.uv_select_sync_valid=False`.
- Symptom: Pack Selected and Center Selected were unavailable until the user
  performed Unwrap.
- Root cause: selected-scope validation treated Blender's stale sync-valid bit
  as a hard failure without a supported edit-mesh refresh.
- Fix: one pre-write BMesh UV sync refresh with exact selection/history restore;
  no Unwrap, Pack, Select All, or UV-coordinate mutation.
- Retest (historical contract): `tests/blender/pack_center_real_mesh_repair.py` — legacy Pack LOCK/IGNORE,
  Center, invalid-sync Pack/Center, rollback, Whole Mesh and lifecycle passed;
  `136104` coordinate pairs were exact during refresh and the scope was
  `5544` selected / `39824` complement.
- Manual retest checklist:
  - [ ] Open the fixture in Blender 5.2 and run selected-only Pack/Center
    without Unwrap.
- Notes: this entry records the superseded pre-V2 refresh-gate contract. Current
  Pack V2 hides the old LOCK/IGNORE control, keeps unselected UVs untouched, and
  uses unselected actual geometry as static blockers. The retained refresh helper
  is compatibility code, not the canonical Sync ON validity gate.

## Entry Template

### YYYY-MM-DD — Short Issue Title

- Status: `OPEN | BLOCKED | RESOLVED`
- Type: `crash | build | test | UI | Blender | app | packaging | other`
- Affected area/file:
- Environment:
- Symptom or exact error:
- Reproduction steps:
  1.
  2.
  3.
- Expected result:
- Actual result:
- Likely root cause:
- Next attempted fix:
- Retest command:
- Manual retest checklist:
  - [ ]
  - [ ]
- Raw log path:
- Notes:
