# Trạng thái hiện tại — uv GPT

## Release v1.2.7 (Min-Square-Span Rotation & Valid-Layout Non-Regression) — 2026-11-23

- **Rollback Baseline**: `v1.2.6` checkpointed at git commit `825ad2ee829c7f8eb3148ef7a8169e426dc1c61c`, tag `v1.2.6-good-baseline` (pushed to `origin/main`).
- **Root Causes Investigated (`chibi_female_nurse-save.blend`, MCP `9876`)**:
  - `GEO_Nurse_Dress.003` (35 islands / 26 PackUnits / 9 stack followers; largest island `346` faces, `max_dim = 0.606`) packs to `~75%–76%`.
  - `GEO_Nurse_Dress.001` (32 islands / 32 PackUnits / 0 stack followers) has one giant un-cut `1,120`-face boomerang island (`38.8%` of total 3D area, `solidity = 0.417`) whose optimal rotation (`172.25°`, `w = 0.9157, h = 0.9371`) caps uniform scale at `1.06077x` (hard theoretical max `64.95%`).
  - `_unit_center_and_angles` and `_prepare_unit` only tried `min_area_rect_angle` (which chose `45°` diagonal = `1.231` span!) + `15°` grid, missing off-grid `min_square_span_angle` (`172.25°`).
  - `_solve_general_native` discarded `_reuse_source_layout` and could return `scale < 1.0` when the 0.45s `1.0x` probe missed on tight layouts (`GEO_Nurse_Dress.003` dropped `75.94% -> 72.56%`).
- **Fixes in `v1.2.7`**:
  - Added `min_square_span_angle(points)` (`uv_gpt/pack_geometry.py`) and integrated `_min_square_span_candidates` into both native (`_unit_center_and_angles`) and Python fallback (`_prepare_unit`) `FREE` rotation pools (`uv_gpt/pack_v2_core.py`).
  - Preserved `source_layout` in `_solve_general_native` when native fails, fails verification, or returns `scale < 1.0 - 1e-6`.
  - Bumped add-on version to `1.2.7` (`uv_gpt/__init__.py`, `uv_gpt/quick_reinstall_tools.py`).
- **Canonical ZIP `uv_gpt_v1.2.7.zip`**: **`673341` bytes**, SHA256 `B6623CB41FEA888E59C3C2E0104DB794B25B476CBB96BD7DF4626FD9E6919211`, `39` runtime files (`38` Python files + `uv_gpt_pack_native.dll`).

## Genesis MCP9876 C++20 32-Thread Native DLL release — 2026-11-19

- Exact fixture: `GEO_Genesis_Outfit_Upper.001` (`E:\OneDrive\Super Nigger\human\Genesis-save.blend`, kept unsaved on disk), Blender `5.3.0`, MCP port `9876`, `17,032` faces, `94` connected UV islands -> `50` `ANY`-locked PackUnits (`39` exact stack followers, `5` locked overlap followers).
- Built self-contained C++20 32-thread native packing engine (`native/pack_native.cpp`, `native/build_native.ps1` -> `uv_gpt/uv_gpt_pack_native.dll`, `903,365` bytes, static MinGW-w64 compile importing only `KERNEL32.dll` + Windows UCRT `api-ms-win-crt-*.dll`).
- Integrated via `ctypes` in `uv_gpt/pack_v2_core.py` (`_load_native_pack_lib`, `_unit_center_and_angles`, `_solve_general_native`) with 100% Python exact polygon collision & tile-fit verification (`_variant_conflicts` & `_shape_fit_tile`) and automatic fallback to Python V3 (`_solve_general`).
- Optimized `_coincident_uv_labelled_mapping` (`5.032s -> 0.062s`) and skipped redundant 24-variant Python preparation when native DLL is loaded (`0.58s -> 0.05s`).
- Removed `Pack Symmetry [U][V]` and `Center Selected` from the `Pack` panel UI (`uv_gpt/ui.py`) per user request while keeping backend operators registered for compatibility.
- Live E2E results on `GEO_Genesis_Outfit_Upper.001` (`margin = 0.003`, `Lock Overlapping = ANY`):
  - `CARDINAL`: scale `1.0842x – 1.0885x`, true face raster coverage **`73.70% – 74.26%`** in **`4.23s – 4.40s`**, `0` O(N²) margin conflicts, `0` OOB.
  - `FREE` (`Any (Best Fit)`): scale `1.0877x – 1.0944x`, true face raster coverage **`74.63% – 75.14%` (`~75%`)** in **`4.34s – 4.52s`**, `0` O(N²) margin conflicts, `0` OOB, hollow U-horseshoe & 4 X-frame concave bays nested, `Ctrl+Z` Undo / `Ctrl+Shift+Z` Redo verified (`max_undo_diff = 0.0`, `max_redo_diff = 0.0`).
- Canonical ZIP `uv_gpt_v1.2.6.zip`: **`672110` bytes**, SHA256 `70a0bbaaaa05b83e423c2573f58b9fd13530899d4092cafb3620fe3e315473ea`, `39` runtime files (`38` Python files + `uv_gpt_pack_native.dll`), `39/39` byte/SHA-256 parity across repo `uv_gpt/`, ZIP, Store Blender `5.2`, and Store Blender `5.3`.
- Evidence report: `tests/blender/genesis_mcp9876_cpp_pack_v2_report.md` and `tests/blender/genesis_live_cpp_pack_v2_before_after.png`.

## Accessories MCP1234 release — 2026-10-07

- Exact fixture: GEO_Accessories_Combined, Blender5.2.2 LTS/d13f752e3b9c,
  MCP port1234,149 visible islands/8378 faces.
- Single U/V symmetry now reflects coordinates; Pack Symmetry reflects full
  corresponding UV geometry. Keep Stack Exact preserves all88 Pro followers.
- Fixed Square/Paste Sync OFF scope, full Pack margin, scale1 valid-layout reuse,
  full UV boundary/corner bounds, mutual job admission, full-source progressive
  guard and terminal source cleanup.
- All tested selected-only writes preserve inactive maps, hidden faces and static UV.
- 80 extracted-ZIP regressions pass with zero skips; strict feature context pass.
- Canonical ZIP 367342 bytes, SHA256 `4006c09f75cb3653b58a26f3903fc05bb20135376b80767e4a287158b08222e3`.
- Quick Reinstall completed;38/38 source/ZIP/installed Python files match.
- Original source UVs/pins/coordinates/topology and selection/settings restored;
  temporary object removed; production blend not saved.
- Report: `tests/blender/accessories_mcp_report.md`; raw/retry/native recovery
  evidence: `.test_runtime/accessories_20261007/`.
- Limits: pair Center/Symmetry is a similarity transform, unmatched Pack singles
  are rigid, exact mirror with unequal world areas does not imply equal density;
  no general Undo/redo, globally optimal packing or multi-version certification.
- Older snapshots below are historical and their ZIP hashes/verification limits
  do not describe this current packaged runtime.

## Quick Reinstall delivery — 2026-09-15

- User explicitly authorized ZIP packaging and Quick Reinstall, superseding the earlier ZIP freeze for this delivery.
- Rebuilt canonical `uv_gpt_v1.2.6.zip`: 356159 bytes; SHA256 `d486d1e3accd7307332171320e7cbd10fd0992a0dda80a61140edafd9a3c2af5`.
- 38 Python files compiled; ZIP structure/CRC/source parity passed; six focused tests passed from the extracted ZIP.
- Quick Reinstall disabled/replaced/enabled uv_gpt and saved preferences in Blender 5.2.1. Installed 38/38 Python files match ZIP, corrected apply function loaded, Pro registered.
- Previous ZIP backed up under `.test_runtime/zip_backups/uv_gpt_v1.2.6_before_pro_fix_20260915_035408.zip`.
- Rapid test build; prior Bottom live evidence applies. General Undo/redo and full release gates remain unverified. Historical source-only/immutable statements below describe earlier checkpoints.

## Pro Bottom MCP fix — 2026-09-15 (unreleased source)

- User resumed work and authorized testing `Bottom` through Blender MCP, then fixing Pro.
- `Bottom` / `UVMap`: 29,440 faces, 549 islands; Blender 5.2.1 LTS build `9e2066aef7ef`.
- Installed baseline failed with `boundary_component_branch_or_open`, applied 0 targets.
- Fixed UV-cut graph construction and reversed-winding edge incidence; added singleton/rejection accounting.
- Fixed Windows progress-file sharing failures and reacquired BMesh after the undo-push tick.
- BLENDER PASS on a disposable copy: 470/470 targets applied, 68 masters, 11 unmatched topology singletons; 79,945 written loops, maximum result-to-Blender coordinate error 0.0; completion 33.4 seconds in this run.
- Original Bottom UVs unchanged; temporary object removed, original active object/selection/sync restored, worker exited 0 and job directory removed. No blend save or installed-addon modification.
- STATIC PASS: six focused regressions. Undo/redo, other fixtures and a release package remain unverified.
- Runtime source now intentionally differs from immutable `uv_gpt_v1.2.6.zip` in `pro_exact_v2_core.py`, `pro_exact_v2_worker.py`, `stack_tools.py`. Earlier ZIP parity below is historical.
- Evidence: `tests/blender/bottom_pro_mcp_evidence.md`; local raw evidence under `.test_runtime/bottom_pro_diagnostic/`.

## PO handoff — 2026-09-15

- Mode: `WAIT_FOR_USER`; successor PO receives context only and must wait for the next user instruction.
- Source chat: `01a01e22-ff1f-7ab1-97d0-f7cc41a0e585` (UV GPT Two Pro Modes Recovery PO).
- ZIP SHA256 rechecked today: `ED2C740C47D7848D98AC2BA5FE4C626A4BFB33DDF6F815D0F8A2D867343F3658`; ZIP remains authoritative and immutable for this synchronization stream.
- Historical Z3 evidence: 129 Python files compiled; focused suite 37 tests with 9 skips; three pure V2 worker smokes passed. Full suite was not green (336 tests, 17 failures, 33 errors); feature-context validator reported 36 errors. Do not interpret the older pending/pass summaries below as full acceptance.
- Blender live, global exact-stack error <= 1e-7, and the reported Pro 60–100 second disappearance remain unverified. The required portable Blender path was missing at the previous audit; no new runtime discovery or live test was performed for this handoff.
- No active implementation node, writer, child or ownership lock is transferred. Preserve the dirty worktree and intentional benchmark deletions. Do not rerun completed synchronization or start remediation until the user resumes work.

- Last reviewed: `2026-11-19`
- Add-on: `uv GPT`
- Version: `1.2.6`
- Blender declared minimum: `>= 3.6.0`
- Blender 5.2 / 5.3 live verification: PASS (`MCP 1234` & `MCP 9876`)
- UI: UV Editor → Sidebar → `uv GPT`
- Source runtime: `uv_gpt/` (+ `native/pack_native.cpp`, `native/build_native.ps1`)
- Release artifact: `uv_gpt_v1.2.6.zip`
- Artifact size: `672110` bytes
- Artifact SHA256: `70A0BBAAAA05B83E423C2573F58B9FD13530899D4092CAFB3620FE3E315473EA`
- Runtime manifest: exactly 39 runtime files below `uv_gpt/` (`38` Python files + `uv_gpt_pack_native.dll`)
- Z1 parity: repo runtime `39/39` exact relative-path and byte parity with ZIP, Store 5.2, and Store 5.3

## Runtime authority

For runtime package contents and behavior, the ZIP above is authoritative.
Tests, docs, dev scripts, config and benchmark harnesses remain repository
artifacts and must describe/test this runtime after alignment. A dirty runtime
file is evidence of drift, not protection from ZIP reconciliation.

## User-facing contract

### Pack V2

- `Pack Selected`, `Pack Whole Mesh` and `Pack Symmetry` launch external Pack V2
  jobs and return without blocking Blender.
- Selected mode moves only selected movable islands, keeps unselected UVs exact,
  and uses unselected actual geometry as static blockers.
- Whole Mesh mode treats all islands as movable.
- Symmetry mode pairs compatible islands, unifies density, applies U/V symmetry
  constraints and uses exact polygon collision/layout checks.
- Actual concave/disjoint/hole boundary geometry is part of the collision route;
  bounding boxes are not the final geometry oracle.
- `Keep Current Scale` controls selected scale-to-fit behavior.
- `Keep Stack Exact` preserves topology-mapped stack followers by exact final
  master-copy semantics.

### Stack Fast / Pro

- Panel exposes `uv_gpt.align_similar_pro_fast` as `Fast` and
  `uv_gpt.align_similar_pro_snap` as `Pro`.
- Fast uses `fast_v2_worker.py` and `fast_v2_core.py` in an external process,
  with nonblocking Blender execution and guarded atomic apply.
- Pro uses `pro_exact_v2_worker.py` and `pro_exact_v2_core.py`; correspondence
  is complete exact topology/loop-to-loop mapping, heavy search is external,
  and proven target results apply progressively.
- Pro reports skipped/unproven targets and worker failures honestly and holds
  terminal progress briefly before cleanup.
- Legacy operator/backend classes remain only for compatibility and historical
  harnesses; they are not the user-facing Fast/Pro panel route.

### Active UV map and selection

- Actual `obj.data.uv_layers.active` is authoritative for destructive work.
- Jobs snapshot `uv_map_name` and wait for the correct object, Edit Mode and
  active UV map before applying results.
- UV Select Sync ON maps mesh face/edge/vertex selection to selected routes and
  does not require stale `uv_select_sync_valid` or the legacy refresh helper as
  a validity gate; that helper remains in the ZIP for compatibility.
- Sync OFF uses independent UV selection flags.

### Overlay

Progress text is:

```text
{percent:.0f}%  •  {elapsed:.1f}s  •  {done}/{total}
```

Fast/Pro/Pack progress is timer-driven and uses a terminal done-hold so the
progress label does not disappear before the user can see completion.

### Keep Stack Exact numeric status

The ZIP proves exact topology correspondence and final exact coordinate copy,
but source inspection does not prove a literal numeric tolerance `<= 1e-7`.
The current evidence shows internal values such as topology default `1e-6`,
Pack direct gate `2e-5` and force-search tolerance. This remains an explicit Z3
numeric acceptance-risk; it is not documented as verified.

## Runtime ownership map

| Module | Responsibility |
|---|---|
| `uv_gpt/__init__.py` | Registration/reload order, including `pack_geometry`; cleanup/rollback. |
| `uv_gpt/stack_tools.py` | User-facing Fast V2/Pro Exact V2 operators, external launchers, timers, guarded apply and overlay progress. |
| `uv_gpt/fast_v2_core.py` / `fast_v2_worker.py` | Pure Fast V2 solve and external worker entrypoint. |
| `uv_gpt/pro_exact_v2_core.py` / `pro_exact_v2_worker.py` | Pure exact topology solve, progressive result events and worker entrypoint. |
| `uv_gpt/pack_tools.py` | Pack V2 snapshot, external worker lifecycle, object/UV guard and user-facing Pack operators. |
| `uv_gpt/pack_v2_core.py` / `pack_v2_worker.py` | Pure Pack V2 modes, geometry/blocker/symmetry solving and worker entrypoint. |
| `uv_gpt/pack_geometry.py` | Pairing and polygon/segment collision helpers. |
| `uv_gpt/topology_correspondence.py` | Exact immutable topology graph and loop correspondence. |
| `uv_gpt/island_tools.py` | Active BMesh/UV layer, UV island discovery, Sync ON mesh selection and topology regions. |
| `uv_gpt/uv_utils.py` | Active UV map safety, state snapshots, destructive preparation and Blender pack compatibility. |
| `uv_gpt/overlay.py` | Fast/Pro/Pack labels, drawing, progress lifecycle, timers and cleanup. |
| `uv_gpt/properties.py` / `uv_gpt/ui.py` | Settings, active UV map control and user-facing panel routes. |

## Verification status

- Z0: ZIP safety, full manifest, AST/import architecture audit — pass.
- Z1: exact runtime path/SHA parity `38/38` — pass; ZIP SHA unchanged.
- Z2: repository artifact alignment and focused pure/static checks — pass;
  Blender/external-worker execution intentionally not run.
- Z3: full suite, Blender 5.2 live smoke, external process timing, visual/Undo
  checks and exact-stack numeric oracle — pending.

## Manual/Z3 gates

- Install/reload the ZIP in Blender 5.2.
- Run Fast and Pro on real selected islands; verify nonblocking behavior,
  progressive exact apply, skipped/failed reporting and cleanup.
- Run Pack Selected/Whole/Symmetry with concave shapes and unselected blockers.
- Switch between multiple UV maps and verify no unexpected UV Map 1 jump.
- Verify UV Select Sync ON, active/history target resolution and Undo.
- Capture an exact-stack numeric oracle and decide the `<= 1e-7` acceptance risk.

No live Blender pass is claimed from Z2.
