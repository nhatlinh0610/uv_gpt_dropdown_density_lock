# Feature Context — Pack/Center Selected Scope

- Slug: `pack-center-selection-scope`
- Status: `active`
- Last reviewed: `2026-08-21`
- Verification state: `verified`
- Primary code/test anchors (project-root-relative, mỗi dòng một `path::symbol`):
  - `uv_gpt/island_tools.py::refresh_uv_selection_scope`
  - `uv_gpt/island_tools.py::validate_uv_selection_scope`
  - `uv_gpt/island_tools.py::get_selected_uv_islands_for_context`
  - `uv_gpt/pack_tools.py::_pack`
  - `uv_gpt/transform_tools.py::_selected_islands`
  - `tests/blender/pack_center_real_mesh_repair.py::_run_invalid_sync_case`
  - `tests/unit/test_pack_selected_center_hotfix_static.py::test_invalid_sync_refresh_is_nondestructive_and_prewrite`
- Related capsules: `symmetry-position-only`

## 1. User Outcome / Current Contract

`Pack Selected` and `Center Selected` work on the visible selected UV scope even
when an opened file has `use_uv_select_sync=True` and Blender reports
`bm.uv_select_sync_valid=False`. The add-on performs one non-destructive
selection-state refresh before the destructive boundary; the user does not need
to Unwrap merely to make these actions available.

Success means:

- Refresh preserves every UV coordinate and UV map, selection flags, active face,
  selection history, active object, and Edit Mode.
- Pack `LOCK_UNSELECTED` and `IGNORE_UNSELECTED`, plus Center, affect selected
  scope only; the complement is exact in the live oracle.
- If refresh cannot establish a reliable scope, the action cancels before UV
  writes. `Pack Whole Mesh` retains intentional all-mesh behavior.

Non-goals / current limits:

- Refresh never calls Unwrap, Smart Project, Pack, Select All, seam operations,
  or coordinate-mutating preparation.
- Interactive Blender 5.2 UI/Undo/manual file-open behavior remains a manual
  verification item; source smoke runs in disposable Blender 5.0 processes.

## 2. Current Execution Map

`Pack Selected/Center Selected` → pre-write selected-scope validation with
`refresh_invalid_sync=True` → `refresh_uv_selection_scope` only if sync validity
is stale → re-read visible selected islands → `ensure_destructive_ready` →
selected-only backend/translation.

Ownership:

- `island_tools.py` owns sync validity diagnosis, snapshot/restore, and scope.
- `pack_tools.py::_pack` owns explicit selected-only Pack LOCK/IGNORE backends
  and Whole Mesh separation.
- `transform_tools.py::_selected_islands` and Center own selected translation.

## 3. Decision Direction

Use Blender’s supported BMesh UV sync boundary: snapshot selection flags and
history, call `bm.uv_select_sync_from_mesh()`, restore the exact selection state,
update the edit mesh with `destructive=False`, then require
`bm.uv_select_sync_valid is True`. The scope is re-evaluated once after refresh.
No refresh is attempted after `ensure_destructive_ready` or after a destructive
operation has begun.

## 4. Invariants / Safety

- Snapshot/restore covers all exposed mesh and UV selection flags, active face,
  and selection history; it intentionally does not write UV coordinates.
- All UV maps are read-only during refresh. The exact fixture oracle checks
  `3 × 45,368 = 136,104` coordinate pairs unchanged during both Pack and Center
  refresh runs.
- Selected scope in the locked case is 5,544 loops; complement is 39,824 loops.
- Pack rollback and exception paths keep their existing atomic state guarantees.
- Object/mode/active map are preserved; register/unregister/re-register passes.

## 5. Active Work

- Change type: `none`

## 6. Improvement / Optimization Opportunities

| Candidate | Evidence or bottleneck | Metric | Trigger to act |
|---|---|---|---|
| None observed | One pre-write refresh is sufficient on the locked fixture | one refresh/operator | Revisit only if another Blender version exposes a different stale boundary |

## 7. Verification / Known Limits

Automated:

- `tests/unit/test_pack_selected_center_hotfix_static.py` — focused static
  contract checks pass with the Symmetry suite.
- Blender 5.0 exact-fixture source smoke — Pack LOCK, Pack IGNORE, Center,
  rollback, invalid-sync Pack/Center, Whole Mesh sanity, and registration pass.
- Invalid-sync Pack/Center — refresh called once, all `136,104` coordinate pairs
  exact during refresh, selected/complement scope restored after operation.
- Fixture SHA before/after — verified unchanged at the authoritative SHA.
- Package smoke — see release evidence in `CURRENT_STATE.md` after packaging.

Manual:

- [ ] Open the fixture in Blender 5.2, enable UV Sync, and run Pack/Center
  selected without Unwrap.
- [ ] Confirm selected-only complement and Undo in the interactive UV Editor.

Known limits / Not Implemented:

- Blender’s stale-sync flag is version/runtime state; Blender 5.2 manual
  confirmation is still required for the visible UI flow.
- Ambient Blender add-on/GPU and shutdown memory warnings are not product
  failures when the controlled process exits successfully.

Regression reference:

- `AI_ERROR_LOG.md::2026-08-21 — Pack/Center rejected stale UV Sync state`

Last evidence:

- `2026-08-21` — exact fixture source smoke, `5,544/39,824` scope oracle,
  `136,104` coordinate refresh proof, rollback and Whole Mesh gates passed.

## 8. Recent Updates — Max 2

### 2026-08-21 — Stale UV Sync repair

- Added a single supported BMesh sync refresh with exact selection/history
  restoration before selected-only destructive preparation.
- Pack and Center now re-evaluate scope only after the pre-write refresh; no
  Unwrap or UV-coordinate mutation is used for normalization.

## Maintenance

Capsule dưới 150 dòng, source anchors relative và registry trong
`.context/INDEX.md` đồng bộ.
