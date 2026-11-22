# uv GPT v1.2.6

`uv GPT` là Blender add-on cho workflow UV đã unwrap: Pack V2, texel density,
Stack Fast/Pro, symmetry, overlay và quản lý active UV map.

Bản sửa ngày 2026-10-07 được kiểm tra qua MCP port 1234 trên bản sao của
`GEO_Accessories_Combined`, Blender 5.2.2 LTS. Symmetry một island phản chiếu
UV theo U/V; Pack Symmetry dùng correspondence để phản chiếu cả hình; Pack
giữ đủ 88 follower sau Pro. Square/Paste nhận đúng selection khi Sync OFF,
và worker bảo vệ dữ liệu khi source thay đổi. Xem bảng kiểm tra tại
`tests/blender/accessories_mcp_report.md` và `CURRENT_STATE.md`.

Artifact hiện tại:

```text
uv_gpt_v1.2.6.zip
size: 672110 bytes
SHA256: 70a0bbaaaa05b83e423c2573f58b9fd13530899d4092cafb3620fe3e315473ea
runtime files: 39 files under uv_gpt/ (38 Python files + uv_gpt_pack_native.dll)
```

## Yêu cầu và cài đặt

- Blender `3.6.0` trở lên theo `bl_info`; đã live test các case trong báo cáo
  trên Blender 5.2.2 LTS và Blender 5.3.0.
- Mesh có UV map và đang ở `Edit Mode`.
- Artifact cài trực tiếp: `uv_gpt_v1.2.6.zip`.

Cài bằng `Edit → Preferences → Add-ons → Install...`, chọn ZIP, bật `uv GPT`,
sau đó mở `UV Editor → N Sidebar → uv GPT`.

## Workflow hiện tại

| Nhóm | ZIP-authoritative behavior |
|---|---|
| `Pack` | Hai nút user-facing trong bảng `Pack` là `Pack Selected` và `Pack Whole Mesh` (đã lược bỏ `Pack Symmetry [U][V]` và `Center Selected` khỏi bảng UI theo yêu cầu gọn giao diện; các operator backend vẫn giữ tương thích). Cả hai đi qua Pack V2 external worker sử dụng lõi **C++20 32-thread Native DLL (`uv_gpt_pack_native.dll`)** kết hợp kiểm tra va chạm exact O(N²) bằng Python (tự động fallback sang Python V3 nếu không nạp được DLL). Selected chỉ emit writes cho selected movable islands, giữ unselected UV nguyên vẹn nhưng dùng geometry unselected làm blocker. Whole Mesh cho phép toàn bộ island di chuyển. |
| `Stack Fast` | Operator `uv_gpt.align_similar_pro_fast`; snapshot nhỏ được xử lý bởi `fast_v2_worker.py`/`fast_v2_core.py` ngoài Blender, operator nonblocking, apply atomically khi đúng source context và active UV map. |
| `Stack Pro` | Operator `uv_gpt.align_similar_pro_snap`; correspondence topology/loop-to-loop chạy ngoài Blender qua `pro_exact_v2_worker.py`/`pro_exact_v2_core.py`, target exact được apply progressive. Skipped, failed và completion đều được báo. |
| `Lock Overlapping` | Property `pack_lock_overlapping` (`Any Overlapping` mặc định / `Exact Stack Only` / `Off`): `Any` giữ cứng cả exact stack lẫn mọi nhóm đảo đè chồng 2D (đè một phần, lồng decal, hình lõm) thành một khối cứng; `Exact` khóa stack trùng topo qua `Keep Stack Exact` (`pack_preserve_stacks`); `Off` tách rời toàn bộ. |
| `Rotation Mode` | Mặc định `Cardinal (Best 90°)` thử `0°/90°/180°/270°` theo từng island để lồng khe sát nhất; `Any (Best Fit)` (`FREE`) thử thêm góc min-area bounding box và `45°/135°` để nén tối đa. |
| `Overlay` | Progress dùng format `percent • elapsed • done/total`; Fast/Pro/Pack giữ label trong background polling và có completion hold trước cleanup. |
| `UV Map` | Actual `obj.data.uv_layers.active` là nguồn sự thật. Job snapshot giữ `uv_map_name` và không apply khi object/mode/active UV map không khớp. |
| `UV Select Sync` | Khi Sync ON, mesh face/edge/vertex selection là selection source; route không dùng stale `uv_select_sync_valid` hoặc refresh helper cũ làm validity gate. Khi Sync OFF, independent UV selection flags được kiểm tra. |

## Pack V2

### Pack Selected

`Pack Selected` chạy external Pack V2 với contract:

1. Selected islands là movable islands.
2. Unselected UVs không bị ghi.
3. Unselected actual geometry được giữ làm static blocker để layout không
   overlap vào vùng đang tồn tại.
4. `Keep Current Scale = OFF` cho phép scale-to-fit tile 0–1.
5. `Keep Current Scale = ON` giữ scale hiện tại và reuse layout gốc nếu đã hợp
   lệ; nếu cần sắp xếp lại, solver dùng dual-bitmask độ phân giải cao (lồng
   vào lỗ rỗng/khoang lõm, multi-order + squeaky-wheel promotion) để tìm layout
   hợp lệ ở `scale = 1.0`. Nếu vẫn không tồn tại layout thỏa tile, blocker và
   margin ở `scale = 1.0`, job báo failure trước khi ghi UV.
6. `Keep Stack Exact` khôi phục follower UV bằng exact topology mapping.

Pack V2 dùng actual boundary geometry, gồm concave/disjoint/hole shape khi
kiểm tra collision. Không dùng bounding-box-only behavior làm nguồn cuối.

### Pack Whole Mesh

`Pack Whole Mesh` gửi mode `whole` tới `pack_v2_worker.py`; tất cả island được
coi là movable, vẫn qua snapshot/context guard và atomic apply.

### Lõi C++20 32 luồng (`uv_gpt_pack_native.dll`)

- Nguồn: `native/pack_native.cpp`; build: `native/build_native.ps1` (MinGW-w64
  static, chỉ import `KERNEL32.dll` + Windows UCRT).
- `pack_v2_core._solve_general_native` gọi DLL qua `ctypes`, sau đó Python kiểm
  tra lại 100% collision/margin/tile-fit trước khi trả layout; DLL thiếu hoặc
  kết quả không hợp lệ → tự fallback về solver Python V3.
- Kết quả đo trên `GEO_Genesis_Outfit_Upper.001` (Blender 5.3, MCP 9876,
  margin 0.003): `FREE` ~74.6–75.1% coverage trong ~4.3–4.5s, `CARDINAL`
  ~73.7–74.3%, 0 conflict. Chi tiết: `tests/blender/genesis_mcp9876_cpp_pack_v2_report.md`.
- Kết quả có dao động nhỏ giữa các lần chạy (đa luồng, có time budget).

### Pack Symmetry (backend only — đã bỏ nút khỏi UI)

Từ 2026-11-19, nút `Pack Symmetry [U][V]` và `Center Selected` không còn hiển
thị trên bảng `Pack`. Operator `uv_gpt.pack_symmetry` vẫn được đăng ký để tương
thích (gọi qua script/F3). Khi gọi, operator gửi mode `symmetry` tới Pack V2:

- tính density scale từ selected/world area; cặp phản chiếu dùng một scale
  chung để giữ hình UV mirror exact. Khi world area hai bên khác nhau, không
  thể đồng thời giữ UV mirror exact và ép texel density hai bên bằng nhau;
- pair island tương thích bằng shape/topology/fingerprint;
- đặt pair đối xứng qua `U_HALF` hoặc `V_HALF`;
- xử lý single island nằm trên axis;
- dùng exact polygon collision và static blockers;
- chỉ apply vào selected movable islands.

## Stack Fast / Pro

### Fast V2

Fast V2 route:

```text
UVGPT_OT_align_similar_pro_fast
  -> _fast_v2_capture_snapshot
  -> _fast_v2_start_job
  -> fast_v2_worker.py
  -> fast_v2_core.solve_fast
  -> _fast_v2_background_timer
```

Blender chỉ capture immutable primitive snapshot, khởi chạy external process và
poll result/progress bằng timer. Nếu source object, Edit Mode, topology, UV data
hoặc actual active UV map thay đổi, result không được apply nhầm.

### Pro Exact V2

Pro route:

```text
UVGPT_OT_align_similar_pro_snap
  -> _pro_exact_v2_start_job
  -> pro_exact_v2_worker.py
  -> pro_exact_v2_core.solve_exact
  -> topology_correspondence
  -> _pro_exact_v2_apply_pending_updates
```

Pro chỉ coi correspondence complete topology/loop mapping là exact result.
Heavy search nằm ngoài Blender. Proven targets được stream qua update files và
apply theo bounded timer ticks; target không chứng minh được correspondence
được ghi là skipped/unproven, worker failure được báo là failed, và completion
giữ progress trước khi cleanup.

> Các legacy operator/backend class còn được ZIP giữ để compatibility với file
> `.blend` và harness cũ, nhưng không phải Fast/Pro user-facing panel route.

## Mirror Symmetry

Đây là operator riêng `uv_gpt.symmetry_auto_mirror`, khác với Pack Symmetry:

- 1 region: U phản chiếu `u' = 1 - u`, V phản chiếu `v' = 1 - v`; giữ nguyên
  scale và tọa độ còn lại. Nút `Center Selected` vẫn dùng để căn giữa;
- 2 regions: anchor + target, mirror vị trí target qua axis và match rotation /
  uniform scale theo contract của ZIP;
- selection history/active face được dùng để resolve target;
- selection ambiguity hủy an toàn và report `INFO`, không có popup đỏ sai.

## Active UV map và selection safety

`uv_utils.ensure_destructive_ready` đọc actual active UV layer, không ép
dropdown setting sang một map khác. `set_active_uv_map` đồng bộ data/BMesh layer
khi người dùng thực sự yêu cầu đổi map. Job apply luôn kiểm tra `uv_map_name` đã
capture.

Khi UV Select Sync ON, mesh selection là authoritative. Selected-only routes
không gọi `refresh_uv_selection_scope` và không dựa vào
`uv_select_sync_valid` làm validity gate; helper cũ vẫn tồn tại trong ZIP để
giữ compatibility. Đây là điểm khác với stale repo behavior trước Z1.

## Overlay

Progress center text có format chính xác:

```text
{percent:.0f}%  •  {elapsed:.1f}s  •  {done}/{total}
```

Overlay giữ Fast label, Pro/Pack labels, elapsed/done/total và done-hold trước
khi timer/draw handler được cleanup.

## Reload khi thay build

Sau khi cài ZIP mới, nên disable/enable add-on hoặc restart Blender. Nếu đang
phát triển source trực tiếp có thể dùng `F3 → Reload Scripts`, nhưng restart là
cách chắc nhất để tránh module Python cũ còn trong RAM.

## Verification status

- Bản cập nhật Pack V2 (2026-11-19):
  - Nâng cấp Pack V2 với `_DualBitmaskTile` (giữ hàng chuyển tiếp cấu trúc để không
    bỏ sót khe/nhánh trung gian), collision check không cấp phát thừa, sắp xếp lồng
    lỗ rỗng/hình lõm, portfolio multi-order + squeaky-wheel promotion, tách nhóm góc
    gọn cho `FREE` và nhận diện chồng lấn dải mỏng / đỉnh chạm biên trên đa giác
    nhiều đỉnh trong `pack_geometry.py`.
  - Sửa chọn UV Vertex đơn lẻ khi `UV Select Sync = OFF` trong `island_tools.py`,
    sửa lỗi trùng prefix `RuntimeError:` và sửa `Undo`/`Redo` sau Pack V2 khi vào
    Edit Mode chưa có baseline undo step (`uv GPT Pre-Pack`) trong `pack_tools.py`.
  - Đã kiểm tra: `109/109` unit tests (11 module `tests.unit.*`), extracted-ZIP
    `py_compile` (`38/38`) + `pack_v2_worker.py` smoke + `test_pack_v2_overlap_and_nesting`,
    và Blender `5.3.0` headless E2E (`accessories_fixture.blend` 149 đảo: Keep Scale
    `1.000x` + Undo/Redo `max_err=0.0`, Whole `FREE` `1.001x`, Symmetry `0.693x`, Sync OFF
    1-UV-vertex + `Lock Overlapping = ANY`, disable/enable lifecycle). Blender `5.2` mới
    đối chiếu SHA-256 `38/38` file cài đặt (chưa chạy live process `5.2`).
- Z0–Z2 lịch sử: xem `tests/blender/accessories_mcp_report.md` và `CURRENT_STATE.md`.

Không claim live Blender pass trong môi trường không có Blender executable.
Trước khi dùng trên asset quan trọng, test trên bản sao `.blend` các route Pack
Selected/Whole/Symmetry, Fast, Pro, active UV map, UV Sync ON và Undo.
