# AI Error Log — uv GPT

File này lưu regression product đã được xác nhận để task sau có memory retest.
`OPEN`/`BLOCKED` phải được xử lý trước release; các mục `RESOLVED` bên dưới là
hai regression thật đã đóng trong v1.2.6. Không ghi validation thành công, lỗi
thao tác người dùng hoặc lỗi tooling không phải product regression.

## Project

- Project name: `uv GPT`
- Project type: Blender add-on Python package
- Main runtime/host: Blender `>= 3.6.0`
- Version reviewed: `1.2.6`
- Last reviewed: `2026-08-21`

## Regression Checklist

- [ ] Review all `OPEN` entries before release/package/build handoff.
- [ ] Check the owning `.context` capsule and any reusable fix knowledge before
  debugging a similar issue.
- [ ] Confirm no known open crash/error remains.

## Entries

No open errors.

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
- Notes: legacy properties remain only for old-file compatibility and are not
  read by the executable Symmetry transform.

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
- Retest: `tests/blender/pack_center_real_mesh_repair.py` — Pack LOCK/IGNORE,
  Center, invalid-sync Pack/Center, rollback, Whole Mesh and lifecycle passed;
  `136104` coordinate pairs were exact during refresh and the scope was
  `5544` selected / `39824` complement.
- Manual retest checklist:
  - [ ] Open the fixture in Blender 5.2 and run selected-only Pack/Center
    without Unwrap.
- Notes: if refresh cannot prove a reliable scope, the action still cancels
  without writing UVs.

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
