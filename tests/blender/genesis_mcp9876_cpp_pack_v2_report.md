# Báo Cáo Kiểm Thử Live MCP 9876 — Pack V2 C++20 32-Thread Native DLL (`2026-11-19`)

## 1. Môi trường & Fixture Thực Tế
- **Blender**: `5.3.0` (Microsoft Store MSIX, kết nối trực tiếp qua MCP TCP port `9876`).
- **File Blend đang mở**: `E:\OneDrive\Super Nigger\human\Genesis-save.blend` (**giữ nguyên trên RAM + Undo stack, tuyệt đối không lưu đè ra ổ đĩa**).
- **Object / Mesh**: `GEO_Genesis_Outfit_Upper.001` (`EDIT_MESH`, `17,032` faces, `94` connected UV islands -> `50` `ANY`-locked PackUnits, gồm `39` exact stack followers và `5` locked 2D-overlap followers).
- **Thiết lập Pack**:
  - `margin = 0.003`
  - `pack_lock_overlapping = "ANY"`
  - `pack_preserve_stacks = True`
  - `rotation_mode = "CARDINAL"` và `"FREE"` (`Any (Best Fit)`)

## 2. Kiến Trúc Lõi C++20 Đa Luồng (`uv_gpt/uv_gpt_pack_native.dll`)
- **Mã nguồn & Build script**:
  - `native/pack_native.cpp` (`3,081` dòng C++20)
  - `native/build_native.ps1` (MinGW-w64 `g++ -O3 -std=c++20 -mpopcnt -mbmi2 -mstackrealign -shared -static -static-libgcc -static-libstdc++ -Wl,-Bstatic -l:libstdc++.a -l:libwinpthread.a -Wl,-Bdynamic`)
- **Độc lập Runtime (`objdump -p`)**:
  - Chỉ import `KERNEL32.dll` và `api-ms-win-crt-*.dll` (Windows UCRT có sẵn trên mọi máy Windows 10/11).
  - `0` phụ thuộc vào `libstdc++-6.dll`, `libwinpthread-1.dll`, hay `libgcc_s_seh-1.dll`.
- **Thuật toán chính**:
  1. **Zero-allocation Exact Polygon & Margin Oracle (`variant_conflicts`)**: Culling nhanh theo Edge-AABB trước khi tính khoảng cách đoạn thẳng - đoạn thẳng và điểm - đa giác (bao gồm lỗ thủng & đảo lồng nhau).
  2. **L2-Resident Direct-Mapped `WorkerWorkspace` (`~295 KB/thread`)**: Loại bỏ hoàn toàn cấp phát động `21.3 MB/iteration` trên heap, cho phép 32 luồng quét hàng nghìn cấu hình/giây không nghẽn bộ nhớ.
  3. **Trích xuất khoang lõm ngang + dọc (`extract_bay_holes`) & Trượt thung lũng chữ V (`compact_dir`)**: Tự động phát hiện và điền đầy lòng hốc chữ U (horseshoe) và 4 khoang lõm của đảo khung chữ X / chữ Y.
  4. **Chiến lược thứ tự khung xương (`major_skeleton`) & `SweepMode::ENV_X` + `compare_all_variants`**: Xếp các đảo khung lớn trước, xoay thử 24 góc (`15°` step + `min_area_rect_angle`) sau khi nén trượt để chọn góc chiếm ít bao hình chữ nhật nhất.
  5. **Nén trọng lực 2 lượt (`squeeze_and_scale_up`) & LNS 32 luồng (`parallel_lns_polish`)**: Xen kẽ các vòng thu hẹp biên (`Span-Shrink`, `ratio = 1.0`) và nâng tỉ lệ mục tiêu (`Target-Scale Climb`) với mốc thăm dò đầu tiên `probe0 = 1.0770`.
  6. **Hậu kiểm 100% bằng Python (`_solve_general_native` trong `uv_gpt/pack_v2_core.py`)**: Mọi kết quả từ DLL đều được Python kiểm tra lại bằng `SpatialIndex`, `_variant_conflicts` và `_shape_fit_tile` trước khi ghi UV; tự động fallback sang Python V3 nếu thiếu DLL.

## 3. Thay Đổi Giao Diện (`uv_gpt/ui.py` & `uv_gpt/pack_tools.py`)
- Lược bỏ 2 nút **`Pack Symmetry [U][V]`** và **`Center Selected`** khỏi bảng `Pack` trên N-Panel theo yêu cầu tinh gọn giao diện (vẫn giữ nguyên các class operator ở backend để tương thích ngược).
- Dòng báo cáo trạng thái dưới nút Pack hiển thị rõ độ phủ (`coverage_pct`) và nhắc nhở khi ô `Keep Current Scale` đang bật (`Keep Current Scale is ON (1.0x) - uncheck to maximize fill`).

## 4. Bảng Kết Quả Đo Đạc Trên `GEO_Genesis_Outfit_Upper.001`

| Engine / Chế độ | Scale | True Face Raster Coverage (`1024x1024`) | Thời gian chạy (`Wall Time`) | Exact O(N²) Margin Conflicts | Out-of-Bounds (`OOB`) | Max Rigid Similarity Error |
|---|---|---|---|---|---|---|
| **Baseline gốc** (`Keep Current Scale = ON`) | `1.0000x` | `62.26% – 63.31%` | — | `0` | `0` | `0.0` |
| **Blender 5.3.0 Native C++** (`CARDINAL`) | `1.0037x` | `62.87%` | `17.45s` | `0` | `0` | — |
| **Blender 5.3.0 Native C++** (`ANY / FREE`) | `0.9463x` | `55.84%` | `16.05s` | `0` | `0` | — |
| **`uv_gpt` Python V3 (Fallback)** | `1.0598x` | `70.63%` | `15.2s – 24.0s` | `0` | `0` | `<= 1.8e-7` |
| **`uv_gpt` C++20 32-Thread DLL (`CARDINAL`)** | **`1.0842x – 1.0885x`** | **`73.70% – 74.26%`** | **`4.23s – 4.40s`** | **`0`** | **`0`** | **`1.4e-7`** |
| **`uv_gpt` C++20 32-Thread DLL (`ANY / FREE`)** | **`1.0877x – 1.0944x`** | **`74.63% – 75.14%`** | **`4.34s – 4.52s`** | **`0`** | **`0`** | **`1.8e-7`** |

- **Kiểm tra `Ctrl+Z` Undo / `Ctrl+Shift+Z` Redo trên Blender 5.3 MCP 9876**:
  - `max_undo_diff = 0.0` (khôi phục chính xác tuyệt đối 100% tọa độ UV gốc).
  - `max_redo_diff = 0.0` (áp lại chính xác 100% tọa độ UV đã pack).
- **Ảnh đối chiếu BEFORE / AFTER**: `tests/blender/genesis_live_cpp_pack_v2_before_after.png`.
