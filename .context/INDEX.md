# Bản đồ context — uv GPT v1.2.6

## Current workflows

| Workflow | Primary anchors | Capsule |
|---|---|---|
| Pack V2 Selected / Whole / Symmetry | `uv_gpt/pack_tools.py`, `uv_gpt/pack_v2_core.py`, `uv_gpt/pack_v2_worker.py`, `uv_gpt/pack_geometry.py` | pack-center-selection-scope |
| Stack Fast V2 / Pro Exact V2 | `uv_gpt/stack_tools.py`, `uv_gpt/fast_v2_core.py`, `uv_gpt/fast_v2_worker.py`, `uv_gpt/pro_exact_v2_core.py`, `uv_gpt/pro_exact_v2_worker.py`, `uv_gpt/topology_correspondence.py` | align-to-selected |
| Mirror Symmetry | `uv_gpt/symmetry_pair.py`, `uv_gpt/island_tools.py` | symmetry-center-pair |
| Density | `uv_gpt/texel_density.py`, `uv_gpt/tdensity_presets.py` | density-reference-selection |
| Overlay / progress | `uv_gpt/overlay.py`, `uv_gpt/stack_tools.py`, `uv_gpt/pack_tools.py` | mapped only |
| Active UV map / selection safety | `uv_gpt/uv_utils.py`, `uv_gpt/island_tools.py`, `uv_gpt/properties.py` | mapped only |

## Feature Registry

| Feature | Capsule | Primary owner | Status |
|---|---|---|---|
| Pack selected scope + Pack Symmetry | `features/pack-center-selection-scope.md` | pack_tools / pack_v2_core | active |
| Stack Fast / Pro | `features/align-to-selected.md` | stack_tools | active |
| Mirror Symmetry | `features/symmetry-center-pair.md` | symmetry_pair | active |
| Density reference selection | `features/density-reference-selection.md` | texel_density | active |
<!-- FEATURE_REGISTRY -->

## Maintenance rules

- `README.md` là user-facing behavior.
- `CURRENT_STATE.md` là snapshot source/artifact/verification hiện tại.
- Capsule mô tả invariant và ownership của workflow đang active.
- `tests/blender/README.md` phân biệt canonical V2 live gates với historical
  compatibility harnesses; kết quả harness cũ không được nâng thành V2 proof.
- `AI_ERROR_LOG.md` là lịch sử regression; nội dung cũ có thể mô tả behavior cũ
  và không phải contract hiện tại.
- Canonical ZIP là `uv_gpt_v1.2.6.zip`, manifest 38 runtime Python files.
  SHA/size và evidence hiện tại nằm ở đầu `CURRENT_STATE.md`; lịch sử cũ không
  thay thế việc kiểm tra source/ZIP/installed byte parity của bản mới.
