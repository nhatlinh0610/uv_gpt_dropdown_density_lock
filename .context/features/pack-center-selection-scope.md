# Feature Context — Pack V2 Selection / Geometry / Symmetry

- Slug: `pack-center-selection-scope`
- Status: `active`
- Last reviewed: `2026-10-07`
- Verification state: `partially verified`
- Primary code/test anchors (project-root-relative):
  - `uv_gpt/pack_tools.py::UVGPT_OT_pack_selected`
  - `uv_gpt/pack_tools.py::UVGPT_OT_pack_whole_mesh`
  - `uv_gpt/pack_tools.py::UVGPT_OT_pack_symmetry`
  - `uv_gpt/pack_tools.py::_pack_v2_capture_snapshot`
  - `uv_gpt/pack_tools.py::_pack_v2_start_job`
  - `uv_gpt/pack_v2_core.py::solve_pack`
  - `uv_gpt/pack_v2_worker.py::main`
  - `uv_gpt/pack_geometry.py::polygons_conflict`
  - `uv_gpt/island_tools.py::get_selected_uv_islands_for_context`
  - `uv_gpt/uv_utils.py::ensure_destructive_ready`
- Related capsules: `align-to-selected`

## 1. User Outcome / Current Contract

### Pack Selected

- Selected islands là movable; unselected UV loops không được ghi.
- Unselected actual geometry được đưa vào blocker set để selected layout không
  overlap vùng đang tồn tại.
- `Keep Current Scale = OFF` cho phép scale-to-fit tile 0–1.
- `Keep Current Scale = ON` giữ scale hiện tại; layout không thỏa tile, blocker
  và margin phải báo failure trước khi ghi. Layout gốc hợp lệ được reuse scale1.
- `Keep Stack Exact` dùng topology correspondence và final exact master-copy.
- Snapshot/context guard discards writes nếu source UV/topology thay đổi.

### Pack Whole Mesh

- Mode `whole` cho phép toàn bộ island movable.
- Worker/core, progress và apply guard dùng cùng Pack V2 lifecycle.

### Pack Symmetry

- Mode `symmetry` dùng actual UV/world geometry để tính density scale; một cặp
  dùng scale chung để giữ UV mirror exact, kể cả khi world area hai bên khác nhau.
- Chỉ full bijective topology mapping mới cho phép pair; UV của target được
  phản chiếu theo từng corresponding loop quanh `U_HALF` hoặc `V_HALF`.
- Single được đặt trên axis bằng rigid transform, không tự đối xứng hóa shape.
- Concave/disjoint/hole polygon collision là geometry oracle cuối, không phải
  bounding-box-only.
- Selected-only writes vẫn giữ unselected UV exact; blocker geometry vẫn được
  dùng khi tìm layout.

### Selection and active UV

- Sync ON dùng mesh face/edge/vertex selection, không dựa vào stale
  `uv_select_sync_valid` hoặc `refresh_uv_selection_scope` làm validity gate;
  helper vẫn được ZIP giữ cho compatibility.
- Sync OFF kiểm tra independent UV selection flags.
- Active UV map lấy từ actual object data/BMesh layer; dropdown không được ép map
  khác vào destructive path.

## 2. Current Execution Map

Operator → primitive snapshot → external worker → complete result → source guard
→ validate all writes → atomic owner-thread apply → process/temp cleanup.

## 3. Decision Direction

- Pair geometry cần correspondence; PCA hoặc cân đối bounding-box không đủ.
- Coincident stacks cần UV-labelled topology mapping, tránh chọn sai automorphism.
- AABB dùng để loại candidate nhanh; polygon geometry kiểm tra layout cuối.
- MaxRects phải chừa đủ margin, không dùng envelope chỉ tạo half-margin gap.
- Collision dùng toàn bộ boundary points; bounds bao gồm mọi UV corner được ghi,
  tránh bỏ sót điểm nhô ra do sampling hoặc đổi thứ tự loop sau rotation.

## 4. Invariants / Safety

- V2 worker chạy ngoài Blender; UI operator không chờ solver.
- Source object, mode, topology counts và `uv_map_name` được snapshot/guard.
- Context object/mode/map không đúng làm job wait; xóa/thay source làm terminal
  cleanup. Không dùng địa chỉ MeshUVLoopLayer RNA làm identity vì nó có thể đổi.
- Validate toàn bộ write scope, finite coordinates, bounds và duplicate keys
  trước khi ghi bất kỳ loop nào; compare captured UV/topology để discard source edit.
- Fast/Pro/Pack chỉ được có một job; launch/cancel dùng cùng lock.
- Native Windows exit `0xC0000005` trước result được restart tối đa một lần;
  cancellation, Python errors hoặc output đã tồn tại không được retry.

## 5. Active Work

- Change type: `none` after the current accepted package; host evidence is in
  `tests/blender/accessories_mcp_report.md`.

## 6. Improvement / Optimization Opportunities

- Symmetry candidate search is bounded; optimize only with exact geometry checks.

## 7. Verification / Known Limits

- `tests/unit/test_pack_selected_center_hotfix_static.py` kiểm tra ZIP selection
  predicates, V2 modes, worker route, active UV guard và geometry/blocker symbols.
- `tests/unit/test_zip_authoritative_runtime.py` kiểm tra 38-file manifest/hash
  và user-facing Pack operator routes.
- `tests/unit/test_pack_v2_geometry_stability.py`: reflection, rotation, blocker,
  margin, actual149-island performance and all88 Pro-stack followers.
- `tests/unit/test_pack_v2_apply_stability.py`: atomic validation, source identity,
  advisory progress sharing and bounded native recovery/cancellation.
- MCP1234 Blender5.2.2 tests use a duplicate of `GEO_Accessories_Combined`;
  inactive maps, hidden/static UV and source object remain protected.
- General Undo/redo, other hosts and globally optimal packing remain unverified.

## 8. Recent Updates

- 2026-10-07: exact mirrored pair UVs, all88 known stack followers, bounded
  symmetry layout, full margin, complete UV boundary/corner bounds, scale1
  valid-layout reuse and owner-thread lifecycle validation.
