# Trạng thái hiện tại — uv GPT

- Last reviewed: `2026-08-21`
- Project type: Blender add-on Python package.
- Add-on name: `uv GPT`.
- Version: `1.2.6` (từ `uv_gpt/__init__.py::bl_info`).
- Blender target: `>= 3.6.0` (metadata `bl_info["blender"] == (3, 6, 0)`).
- UI location: UV Editor → Sidebar → `uv GPT`.
- Repository: không phải Git repository.

## Artifact và source

- Source package: `uv_gpt/`.
- Distribution artifact duy nhất: `uv_gpt_v1.2.6.zip`.
- `uv_gpt_v1.2.6.zip`: `306,757` bytes, SHA256
  `217B60633748883B589CCEEDAC6860CEED5B55E8775C8843A07A2DC868CA3FA7`.
- ZIP release chứa package `uv_gpt/` với đúng 30 file Python hiện tại, byte
  parity với source; không chứa tests, benchmarks, runtime, docs hoặc cache.
- Root workspace có đúng một ZIP: `uv_gpt_v1.2.6.zip`.
- Version `1.2.6` khớp `bl_info` và package smoke kiểm tra từ bản extract của ZIP.

## Bản đồ kiến trúc hiện tại

| Module | Trách nhiệm được thấy trong source |
|---|---|
| `uv_gpt/__init__.py` | `bl_info`, version, load/reload 11 module, stale registration cleanup, `register()`/`unregister()`. |
| `uv_gpt/properties.py` | `UVGPT_Settings`, texel-density preset properties, active UV map, pack/density/stack/symmetry/overlay/UI settings và `Scene.uv_gpt_settings`. |
| `uv_gpt/ui.py` | Panel `uv GPT` trong Image Editor/UV context và các section Pack/Density/Stack/Symmetry/Overlay/UV Map. Panel View 3D hiện poll false. |
| `uv_gpt/island_tools.py` | Xác định UV island, island active/selected, bounds, center, area và face metrics; refresh stale UV Sync selection state trước selected-only Pack/Center. |
| `uv_gpt/uv_utils.py` | Lấy mesh/UV context, chọn island, transform cơ bản, copy/paste, pack helper và duplicate sang `Bake_Optimized`. |
| `uv_gpt/pack_tools.py` | Operator pack selected/whole mesh và logic margin, rotation, density lock, unselected scope, preserve stack. |
| `uv_gpt/texel_density.py` | Đo/format/chuyển đổi texel density, apply density, square face, grid whole mesh và các operator hiển thị/áp dụng. |
| `uv_gpt/tdensity_presets.py` | Preset density, preset operators và khởi tạo default preset qua Blender timer. |
| `uv_gpt/transform_tools.py` | Operator Center Selected, mirror X, rotate 90/180; Center Selected dùng selected UV scope sau pre-write refresh. |
| `uv_gpt/symmetry_pair.py` | Auto mirror đúng hai vùng topology theo anchor/history-target, phản chiếu tâm bounding-box U/V và dịch target position-only; route executable không rotation/scale. |
| `uv_gpt/stack_tools.py` | `Paste Keep Position`, `Align To Selected` và `Align Similar Pro`; Pro dùng immutable topology graph, density master và exact BMLoop correspondence, còn action cũ giữ ordered boundary/hole-aware matcher và AUTO scheduler. |
| `uv_gpt/similarity_matcher.py` | Pure-Python/NumPy ordered loop descriptor, hole/topology gates, cyclic/reverse Procrustes fit và per-run diagnostics/cache. |
| `uv_gpt/match_scheduler.py` | Deterministic single/thread numeric scheduler; ProcessPool chỉ là benchmark prototype, không ship vào operator. |
| `uv_gpt/topology_correspondence.py` | Pure immutable face/edge/vertex/loop graph, exact deterministic correspondence, cyclic/reverse/reflection handling, hole/interior propagation và bounded search. |
| `uv_gpt/overlay.py` | Nhãn overlay island/area/texel density và watcher/refresh cho UV Editor. |

## Verification state

- `E:\OneDrive\AI_Rules\scripts\check-ai-project.ps1 -ProjectRoot .`:
  **pass**, gồm `check-feature-context.ps1 -Strict` với 3 capsule.
- Python AST/compile: **pass** cho toàn bộ 30 file `uv_gpt/*.py`.
- Focused static suite: **17/17 pass** cho Symmetry và Pack/Center.
- Blender 5.0 exact-fixture source smoke: **pass**.
  - Fixture/object/map: `cc.blend`, `body pussy -4-2 base chon A big tit done`,
    `UVMap.002`; object có 11,343 faces, 45,368 loops và 17 UV islands.
  - Symmetry U/V: anchor trước, history-target rồi active fallback; target-only
    one-delta transform, invalid selection/invalid UV Sync zero-write, lifecycle.
    Maximum observed float32 readback variation/pairwise error:
    `1.1920928955078125e-07`.
  - Pack/Center: selected `5,544` loops, complement `39,824`; LOCK/IGNORE,
    Center, rollback, Whole Mesh và lifecycle pass. Invalid-sync Pack/Center
    gọi refresh đúng một lần; `136,104` UV coordinate pairs của cả 3 map exact
    trong refresh.
- Packaged smoke: **pass** từ extract của `uv_gpt_v1.2.6.zip`; package import
  path trỏ đúng extract, version `1.2.6`, Symmetry U/V và invalid-sync
  Pack/Center đều pass.
- ZIP audit: **pass**; đúng 30 entries, source byte parity, không tests/
  benchmarks/runtime/docs/cache; root có đúng một ZIP.
- Locked fixture SHA trước/sau mọi source/package smoke:
  `5CB51356284D731990D5F5CA481EDB64ACD4452B47802CAAE5EA5DB307C5D3B6`.
- Blender ghi forward-version/ambient shutdown warning trong background nhưng
  các process kiểm soát exit code `0`; không dùng interactive Blender 5.2 và
  không save fixture.
- UI, Undo, Blender 5.2 manual open-file flow và visual orientation trên asset
  thật: **manual verification required**.

## Giới hạn và handoff

- Bản đồ module và tên operator ở trên là evidence từ source hiện tại; các
  claim live trong packet này chỉ áp dụng cho Blender 5.0 background smoke trên
  fixture nêu rõ.
- Capsules canonical tại `.context/features/align-to-selected.md`,
  `.context/features/symmetry-position-only.md` và
  `.context/features/pack-center-selection-scope.md` ghi contract, invariant
  và verification scope tương ứng.
- Nếu phát hiện regression thật trong Blender, ghi issue đang mở vào
  `AI_ERROR_LOG.md` cùng bước tái kiểm tra; không dùng log cho validation thành
  công.
