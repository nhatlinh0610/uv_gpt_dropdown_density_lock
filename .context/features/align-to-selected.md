# Feature Context — Stack Fast V2 / Pro Exact V2

- Slug: `align-to-selected`
- Status: `active`
- Last reviewed: `2026-10-07`
- Verification state: `partially verified`
- Primary code/test anchors (project-root-relative):
  - `uv_gpt/stack_tools.py::UVGPT_OT_align_similar_pro_fast`
  - `uv_gpt/stack_tools.py::_fast_v2_start_job`
  - `uv_gpt/stack_tools.py::UVGPT_OT_align_similar_pro_snap`
  - `uv_gpt/stack_tools.py::_pro_exact_v2_start_job`
  - `uv_gpt/fast_v2_core.py::solve_fast`
  - `uv_gpt/pro_exact_v2_core.py::solve_exact`
  - `uv_gpt/pro_exact_v2_worker.py::main`
  - `uv_gpt/topology_correspondence.py::find_correspondence`
  - `uv_gpt/overlay.py::set_fast_progress`
  - `uv_gpt/ui.py::draw_uv_gpt_panel`
  - `tests/unit/test_pro_exact_uv_cuts.py::ProExactUVCutsTests`
- Related capsules: `none`

## 1. User Outcome / Current Contract

Panel Stack chỉ hiển thị hai action:

- `Fast`: `uv_gpt.align_similar_pro_fast`; capture primitive snapshot, chạy
  matching ở external Fast V2 worker và apply atomically khi source object,
  Edit Mode, topology và actual active UV map còn hợp lệ.
- `Pro`: `uv_gpt.align_similar_pro_snap`; heavy correspondence chạy trong
  external Pro Exact V2 worker. Chỉ complete topology/loop-to-loop mapping mới
  được coi là exact; result event được stream và apply progressive.

Pro phải báo rõ target skipped/unproven, worker failure và terminal counts.
Progress hiển thị percent, elapsed và done/total; done-hold giữ overlay tồn tại
đủ lâu để completion không biến mất.

Legacy Pro/Fast compatibility classes còn trong ZIP để giữ `.blend`/harness cũ,
nhưng không phải user-facing panel route và không được dùng làm canonical docs.

## 2. Current Execution Map

Panel Pro → snapshot → external worker → UV-cut graph → full correspondence
→ progressive events → guarded Blender timer apply → held completion.

## 3. Decision Direction

- Split graph edges and corner fans at UV cuts, including slits within one island.
- Reverse outgoing-edge incidence with face winding before exact verification.
- Report topology singletons as unmatched; matched masters are not skips.
- Retry Windows progress replacement briefly; a missed progress tick is advisory.

## 4. Invariants / Safety

- Fast/Pro operator `execute`/`invoke` user-facing khởi chạy V2 detached route;
  không gọi `_ProAlignSession` làm primary implementation.
- Pro không dùng nearest-vertex many-to-one làm exact proof.
- Correspondence là one-to-one topology loop mapping.
- Master selection dùng actual UV area theo ZIP route.
- Candidate không chứng minh được topology bị skip, không snap bừa sang hình khác.
- Apply chờ đúng source object + Edit Mode + captured active UV map.
- Source thay đổi trong lúc worker chạy làm result chờ hoặc discard, không ghi
  nhầm UV.
- Before every Pro batch, compare whole captured UV/topology with expected state
  including earlier accepted batches; edits on unrelated source faces also discard.
- Validate complete write/event coverage before mutation. Fast/Pro/Pack share
  job admission and launch/cancel locking; deleted/replaced source terminates.

## 5. Active Work

- Change type: `none` after current package acceptance.
- Undo/redo and other Blender versions remain unverified.

## 6. Improvement / Optimization Opportunities

- No broader optimization in this fix; keep exact mapping and explicit rejection.

## 7. Verification / Known Limits

- `tests/unit/test_pro_snap.py` giữ pure planner coverage.
- `tests/unit/test_zip_authoritative_runtime.py` kiểm tra manifest/hash, UI IDs,
  V2 worker routes, progress format và active UV guards.
- `tests/unit/test_pro_two_modes_ui.py` giữ legacy lifecycle checks khi Blender UI
  modules khả dụng; nếu không có `blf`, test được skip rõ ràng thay vì báo lỗi
  product.
- `tests/blender/accessories_mcp_report.md`: MCP1234 Blender5.2.2 live Fast/Pro
  scope, map/mode wait-return, source edit/deletion/replacement and cleanup.
- `tests/unit/test_stack_v2_stability.py`: staged atomic writes, admission,
  launch/cancel races and progressive global expected-source validation.

### Numeric risk

Accessories fixture accounts for all149 islands: 88 followers,32 masters and29
unmatched topology singletons; all88 targets apply with error0.0 in Blender.
Fast matches75 approximate followers. This is fixture-specific evidence;
it does not certify global topology coverage or general Undo/redo.

New evidence, 2026-09-15: six focused regressions in
`tests/unit/test_pro_exact_uv_cuts.py` pass. MCP on Blender 5.2.1 LTS,
Bottom/UVMap disposable copy: 549 islands, 470 targets applied, 68 masters,
11 topology singletons; 79,945 written loops have result-to-Blender error 0.0.
Original UVs unchanged, worker exited and temp directory removed. This proves
this Pro fixture, not global Pack/Stack tolerance or general Undo/redo.
See `tests/blender/bottom_pro_mcp_evidence.md`.

## 8. Recent Updates

- Update 1 — 2026-09-15: fixed UV-cut graph and reversed face winding;
  singleton reporting, Windows progress sharing recovery, and fresh BMesh
  acquisition on the tick after undo push; later included in packaged delivery.
- Update 2 — 2026-10-07: full source guard before each batch, shared worker
  admission, source identity/cleanup and context-aware Paste selection.
