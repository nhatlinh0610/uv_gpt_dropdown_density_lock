# Bản đồ context — uv GPT v1.2.6

## Mục đích

Thư mục này là bản đồ kỹ thuật tối thiểu cho add-on Blender `uv GPT` v1.2.6.
Nó giúp task sau tìm đúng source anchor và trạng thái validation; nó không thay
thế README, source code hoặc một feature specification chưa có evidence.

## Phạm vi hiện tại

- Runtime target: Blender `>= 3.6.0`.
- Package: `uv_gpt/`.
- Artifact hiện tại: `uv_gpt_v1.2.6.zip` (artifact phân phối duy nhất của workspace).
- UI owner: `uv_gpt/ui.py::draw_uv_gpt_panel`.
- Registration owner: `uv_gpt/__init__.py::register` và các `register()` của
  module.
- State/error handoff: `CURRENT_STATE.md`, `AI_ERROR_LOG.md`.

## Bản đồ workflow và source anchors

| Workflow hiện tại | Source anchors chính | Trạng thái context |
|---|---|---|
| Pack UV | `uv_gpt/pack_tools.py::UVGPT_OT_pack_selected`, `uv_gpt/pack_tools.py::UVGPT_OT_pack_whole_mesh`, `uv_gpt/island_tools.py::refresh_uv_selection_scope` | Selected-only scope và stale UV Sync có capsule `pack-center-selection-scope`. |
| Density | `uv_gpt/texel_density.py::UVGPT_OT_apply_td_selected`, `uv_gpt/texel_density.py::UVGPT_OT_apply_td_whole_mesh` | Chỉ lập bản đồ; chưa tạo capsule. |
| Stack | `uv_gpt/stack_tools.py::UVGPT_OT_align_to_selected`, `uv_gpt/stack_tools.py::UVGPT_OT_paste_keep_position`, `uv_gpt/similarity_matcher.py::match_descriptors`, `uv_gpt/match_scheduler.py::schedule_numeric_batch` | Align To Selected dùng lazy ordered boundary/hole-aware similarity và AUTO numeric scheduler; selected target cố định, candidate unselected được căn; Blender 5.0 exact fixture và packaged 1.2.6 smoke đã live-verified. MATCH-01 speed target remains unmet and documented. |
| Symmetry | `uv_gpt/symmetry_pair.py::UVGPT_OT_symmetry_auto_mirror`, `uv_gpt/symmetry_pair.py::_apply_from_reference`, `uv_gpt/ui.py::draw_uv_gpt_panel` | Position-only U/V contract có capsule `symmetry-position-only`. |
| Overlay | `uv_gpt/overlay.py::UVGPT_OT_refresh_overlay`, `uv_gpt/texel_density.py::UVGPT_OT_show_selected_td` | Chỉ lập bản đồ; chưa tạo capsule. |
| UV Map/safety | `uv_gpt/properties.py::UVGPT_Settings`, `uv_gpt/uv_utils.py::UVGPT_OT_duplicate_to_bake_optimized` | Chỉ lập bản đồ; chưa tạo capsule. |

`features/` chỉ chứa capsule cho workflow đã thay đổi behavior, invariant/safety,
ownership hoặc verification contract. Hiện Stack có capsule `align-to-selected`;
không tạo spec riêng cho các module, nút hoặc operator chưa được thay đổi.

## Quy tắc cập nhật context

1. Đọc `CURRENT_STATE.md`, entry liên quan trong `AI_ERROR_LOG.md` và source
   anchor trước khi sửa behavior.
2. Nếu behavior/invariant/validation thay đổi, tạo hoặc cập nhật đúng capsule
   sở hữu workflow; giữ `INDEX.md` và registry đồng bộ.
3. Nếu chỉ sửa typo/format hoặc scaffold không làm thay đổi behavior, ghi
   `Context impact: none` trong handoff và không tạo capsule.
4. Ghi rõ `verified`, `not verified` hoặc `manual verification required`; không
   biến static inspection thành Blender live test.

## Feature Registry

Feature registry liệt kê các workflow có capsule canonical; các hàng bên trên
vẫn là bản đồ source cross-feature.

| Feature | Capsule | Primary code/test anchors | Status |
|---|---|---|---|
| Align To Selected | `features/align-to-selected.md` | `uv_gpt/stack_tools.py::UVGPT_OT_align_to_selected`, `uv_gpt/similarity_matcher.py::match_descriptors` | active |
| Symmetry Position-Only | `features/symmetry-position-only.md` | `uv_gpt/symmetry_pair.py::UVGPT_OT_symmetry_auto_mirror`, `uv_gpt/symmetry_pair.py::_apply_from_reference` | active |
| Pack/Center Selected Scope | `features/pack-center-selection-scope.md` | `uv_gpt/island_tools.py::refresh_uv_selection_scope`, `uv_gpt/pack_tools.py::_pack`, `uv_gpt/transform_tools.py::_selected_islands` | active |
<!-- FEATURE_REGISTRY -->

## Ranh giới memory

- `README.md`: cài đặt và hướng dẫn người dùng.
- `CURRENT_STATE.md`: snapshot cross-feature, artifact và validation.
- `.context/features/*.md`: canonical truth của workflow khi thực sự cần.
- `AI_ERROR_LOG.md`: issue/regression đang mở và bước retest.
