# Feature Context — Density Reference Selection

- Slug: `density-reference-selection`
- Status: `active`
- Last reviewed: `2026-10-07`
- Verification state: `partially verified`
- Primary code/test anchors (project-root-relative):
  - `uv_gpt/texel_density.py::_face_uv_selection_state`
  - `uv_gpt/texel_density.py::_selected_reference_face`
  - `uv_gpt/texel_density.py::square_selected_face_to_px_cm`
  - `tests/unit/test_density_reference_selection.py::DensityReferenceSelectionTests`
- Related capsules: `pack-center-selection-scope`

## 1. User Outcome / Current Contract

Square Face and Grid use exactly one selected quad reference. Sync ON reads the
current mesh selection mode; Sync OFF reads independent UV selection. Empty or
multiple reference faces cancel without changing UVs.

## 2. Current Execution Map

Context sync/mode → selected reference face → validate quad → square UV or seed
whole-mesh quad grid → apply target density.

## 3. Decision Direction

Read modern BMFace/BMLoop UV flags when present, with legacy BMLoopUV flags as
compatibility. Stale active-face/history is not proof of independent UV selection.

## 4. Invariants / Safety

- Resolve reference selection before writes.
- One selected face cannot be replaced by an unselected stale active face.
- Keep inactive UV maps and hidden faces unchanged.
- Grid intentionally operates on visible quads across the whole mesh; nonquads
  and hidden faces are skipped rather than assigned fabricated quad UVs.

## 5. Active Work

- Change type: `none` after acceptance.

## 6. Improvement / Optimization Opportunities

- No broader density algorithm change in this repair.

## 7. Verification / Known Limits

- Seven focused selector regressions cover modern/legacy flags and reference scope.
- MCP1234 Accessories: stale face100 with selected quad0 writes exactly four
  selected loops; Sync ON/OFF and no-selection cases pass.
- Density tests cover selected/whole, both units and1024/2048/4096/custom texture;
  whole-mesh relative error is below5.45e-6 in this fixture.
- Blender versions other than5.2.2 and general Undo/redo remain unverified.

## 8. Recent Updates

- 2026-10-07: context-aware reference selection removes stale active-face writes.
