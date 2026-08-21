# Feature Context — Symmetry Position-Only

- Slug: `symmetry-position-only`
- Status: `active`
- Last reviewed: `2026-08-21`
- Verification state: `verified`
- Primary code/test anchors (project-root-relative, mỗi dòng một `path::symbol`):
  - `uv_gpt/symmetry_pair.py::UVGPT_OT_symmetry_auto_mirror`
  - `uv_gpt/symmetry_pair.py::_resolve_selected_pair`
  - `uv_gpt/symmetry_pair.py::_apply_from_reference`
  - `uv_gpt/symmetry_pair.py::_bounds_center`
  - `tests/blender/symmetry_real_mesh_repair.py::_run_axis_case`
  - `tests/unit/test_symmetry_hotfix_static.py::test_symmetry_transforms_all_target_region_loops`
- Related capsules: `pack-center-selection-scope`

## 1. User Outcome / Current Contract

`Mirror Target Position` mirrors the position of the second selected topology
region across `U=0.5` or `V=0.5` without rotating, scaling, or reshaping it.
The first region is the anchor. A valid selection-history target wins; active
face is the fallback. Exactly two topology regions are required.

Success means:

- Anchor and every loop outside target are byte/equality unchanged in the live
  BMesh oracle.
- Target UVs receive one common translation delta derived from anchor and target
  bounding-box centers; pairwise layout is preserved up to Blender float32
  storage rounding (`1.1920928955078125e-07` in the locked fixture run).
- Invalid/ambiguous selection cancels before any UV write.
- Symmetry UI exposes only U/V and the one position action. Legacy rotation/scale
  properties remain hidden for old-file compatibility only.

Non-goals / current limits:

- No principal-axis matching, rotation, scale matching, Keep Parallel, or tile
  clamping is part of this executable route.
- Blender 5.2 interactive panel layout, Undo, and manual visual orientation are
  not covered by the background Blender 5.0 harness.

## 2. Current Execution Map

`Symmetry panel` → `UVGPT_OT_symmetry_auto_mirror` → validate exactly two
regions → resolve target by valid history then active face → compute anchor
bounding-box center → reflect center on selected half-tile axis → translate
all target loops by one delta → `bmesh.update_edit_mesh`.

Ownership:

- `ui.py::draw_uv_gpt_panel` owns the reduced U/V/action surface.
- `symmetry_pair.py::_resolve_selected_pair` owns anchor/target precedence.
- `symmetry_pair.py::_apply_from_reference` owns the position-only transform.
- `island_tools.py::get_selected_uv_faces_for_symmetry` owns fail-closed scope.

## 3. Decision Direction

Use the region UV bounding-box center, not a principal axis or a fitted angle.
Reflect only that center across the selected `U=0.5` or `V=0.5` line. Translate
the target using `desired_target_center - current_target_center`; do not derive
or apply any orientation/scale correction. Validate before the destructive
boundary and validate again after duplication when that option is enabled.

## 4. Invariants / Safety

- Exactly two regions are required; zero, one, or more than two cancels with
  zero-write.
- Target resolution is selection history when valid, active face otherwise.
- Anchor, outside loops, UV map identity, selection flags, and active/history
  state remain unchanged.
- The executable Symmetry path contains no rotation/scale/principal-axis step.
- The live harness uses the locked object and `UVMap.002` (11,343 faces,
  45,368 loops, 17 UV islands) from a disposable in-memory copy only.

## 5. Active Work

- Change type: `none`

## 6. Improvement / Optimization Opportunities

| Candidate | Evidence or bottleneck | Metric | Trigger to act |
|---|---|---|---|
| None observed | Position-only contract is live-verified | n/a | Only after a new real-asset regression |

## 7. Verification / Known Limits

Automated:

- `tests/unit/test_symmetry_hotfix_static.py` — `17/17` focused tests pass.
- Blender 5.0 exact-fixture source smoke — verified for U/V, history→active
  fallback, invalid selection, invalid UV Sync cancellation, and registration.
- U/V target transform — verified; selected target has 1,386 faces and the
  maximum measured translation/pairwise deviation is `1.1920928955078125e-07`.
- Fixture SHA before/after — verified unchanged at the authoritative SHA.
- Package smoke — see release evidence in `CURRENT_STATE.md` after packaging.

Manual:

- [ ] In Blender 5.2, visually confirm an already oriented target stays oriented.
- [ ] Confirm Undo and the panel’s U/V-only surface in the interactive UV Editor.

Known limits / Not Implemented:

- Float32 UV storage can make Python readback of a translated pair differ by
  one ULP; the algorithm still applies one shared mathematical delta.
- Forward-version fixture warning and ambient Blender shutdown warnings are
  runtime noise; the fixture is never saved.

Regression reference:

- `AI_ERROR_LOG.md::2026-08-21 — Symmetry rotated a positioned target`

Last evidence:

- `2026-08-21` — Blender 5.0 exact fixture, source import, two axes, zero-write
  cancellation cases, and register lifecycle passed.

## 8. Recent Updates — Max 2

### 2026-08-21 — Position-only repair

- Removed executable rotation/scale/principal-axis behavior and reduced UI to
  U/V plus `Mirror Target Position`.
- Added bbox-center reflection and one-delta target translation; focused static
  and exact-fixture smoke evidence is recorded above.

## Maintenance

Capsule dưới 150 dòng, source anchors relative và registry trong
`.context/INDEX.md` đồng bộ.
