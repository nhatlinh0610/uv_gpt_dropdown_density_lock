# Feature Context — Mirror Symmetry

- Slug: `symmetry-center-pair`
- Status: `active`
- Last reviewed: `2026-10-07`
- Verification state: `partially verified`
- Primary code/test anchors (project-root-relative):
  - `uv_gpt/symmetry_pair.py::UVGPT_OT_symmetry_auto_mirror`
  - `uv_gpt/symmetry_pair.py::_mirror_single_region`
  - `uv_gpt/symmetry_pair.py::_apply_from_reference`
  - `uv_gpt/island_tools.py::get_selected_uv_regions_for_context`
- Related capsules: `pack-center-selection-scope`

This Mirror Symmetry operator is separate from Pack V2 `uv_gpt.pack_symmetry`.
Pack Symmetry owns selected-island density/pair/layout constraints; this capsule
owns the one-region/two-region Mirror Symmetry UI action.

## 1. User Outcome / Current Contract

One operator has selection-count behavior:

- **1 region**: U applies `u'=1-u`; V applies `v'=1-v` to every selected-region
  loop. Scale and the orthogonal coordinate stay unchanged. No PCA centering.
- **2 regions**: resolve target from active/last-selected face, mirror anchor
  direction through selected U/V axis, rotate target to that direction, match
  RMS radius with uniform scale, then translate target center to the reflected
  anchor center.
- **0 or >2 regions / ambiguous target**: cancel and report `INFO`, no red
  selection error popup.

## 2. Current Execution Map

Selected connected region(s) → validate U/V and target → destructive UV-map
preparation → single reflection or pair similarity transform → edit-mesh update.

## 3. Decision Direction

- User's one-island mirror request supersedes the earlier Center Auto Align route.
- The old private center helper is compatibility-only; Center Selected owns
  normal centering. Pair behavior keeps its previous anchor/target transform.

## 4. Invariants / Safety

- Single mode never scales.
- Pair mode uses uniform scale only; no non-uniform stretch.
- Pair mode does not reflect all UV coordinates, so target winding is preserved.
- Selection regions are topology-connected and resolved before destructive-ready
  UV map switching to avoid re-reading a valid one-region selection as empty.
- Invalid axis cancels before destructive setup; unrelated UV maps stay exact.

## 5. Active Work

- Change type: `none` after acceptance.

## 6. Improvement / Optimization Opportunities

- No additional shape deformation; internal self-symmetrization is not this action.

## 7. Verification / Known Limits

- `tests/unit/test_symmetry_hotfix_static.py` covers the ZIP region/axis contract.
- `tests/unit/test_unified_uv_workflow.py` covers single/pair UI and source route.
- `tests/unit/test_single_symmetry_mirror.py` checks U/V coordinates, involution,
  edge-length preservation, operator routing and invalid-axis zero mutation.
- MCP1234: single U/V with Sync ON/OFF, second UV map and duplicate-before
  operations pass on Accessories; max reflection error in Blender `2.98e-8`.
- Pair checks prove mirrored centers/similarity transforms, not exact shape
  correspondence. General Undo/redo remains unverified.

## 8. Recent Updates

- 2026-10-07: single-region route now mirrors across chosen tile half-axis;
  panel and notifications describe this behavior.
