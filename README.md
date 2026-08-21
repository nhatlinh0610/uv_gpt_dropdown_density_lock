# uv GPT v1.2.6

uv GPT là Blender add-on cung cấp các công cụ UV cho layout đã unwrap, gồm
đóng gói island, texel density, stack, symmetry, overlay và quản lý UV map.

## Yêu cầu

- Blender `3.6.0` trở lên.
- Một mesh có UV map; các thao tác UV chính cần mở mesh ở Edit Mode.
- File cài đặt: `uv_gpt_v1.2.6.zip`.
- Artifact phân phối duy nhất của workspace là `uv_gpt_v1.2.6.zip`.

## Cài đặt

1. Mở Blender → `Edit` → `Preferences` → `Add-ons`.
2. Chọn `Install...`, trỏ tới `uv_gpt_v1.2.6.zip`, rồi xác nhận cài đặt.
3. Tìm `uv GPT` trong danh sách add-on và bật checkbox.
4. Lưu Preferences nếu muốn Blender giữ trạng thái bật cho các lần mở sau.

## Mở giao diện

1. Chọn một mesh và vào `Edit Mode`.
2. Mở `UV Editor` (hoặc chuyển một vùng Image Editor sang chế độ UV).
3. Nhấn `N` để mở Sidebar, chọn tab `uv GPT`.
4. Mở các nhóm `Pack`, `Density`, `Stack`, `Symmetry`, `Overlay` và `UV Map`
   bằng cách bấm tiêu đề nhóm.

Nếu không thấy panel, kiểm tra vùng hiện tại là UV Editor/Image Editor, object là
mesh và đang ở Edit Mode.

## Quy trình an toàn nên dùng

1. Lưu file `.blend` trước khi thao tác; với layout quan trọng, làm trên một
   bản sao hoặc mesh test.
2. Chọn đúng UV map và chọn các face/island cần tác động.
3. Nếu cần bản dự phòng ngay trong file, mở nhóm `UV Map` và bật
   `Duplicate before operations`; tùy chọn này dùng tên map `Bake_Optimized`.
4. Chạy một thao tác nhỏ trên mesh test trước, kiểm tra UV và undo khi cần.

Các lệnh pack, scale, mirror, stack và grid có thể thay đổi UV. Tùy chọn backup
không thay thế việc lưu file và kiểm tra trên bản sao.

## Các nhóm chức năng

| Nhóm | Chức năng hiện có |
|---|---|
| `Pack` | `Pack Selected` hoặc `Pack Whole Mesh`; chỉnh margin, rotation; có lựa chọn khóa density, xử lý UV chưa chọn và giữ stack; `Center Selected` căn island đã chọn về tâm tile. Pack/Center selected-only tự refresh một lần trạng thái UV Sync stale trước khi ghi UV, không cần Unwrap; phần complement không bị đổi. |
| `Density` | Chọn texture size, đơn vị `px/cm` hoặc `px/unit`, nhập target; xem density của selected/all, lấy target từ selection, áp dụng cho selected/whole mesh; `Square Face` và `Grid Mesh` tạo UV theo density mục tiêu. |
| `Stack` | `Paste Keep Position` và `Align To Selected`; align dùng Border Shape, với tùy chọn `Match Scale`, `Allow Flipping` và `Similarity Tolerance`. |
| `Symmetry` | Chọn anchor trước, Shift-chọn target rồi chạy `Mirror Target Position` theo trục U hoặc V tại 0.5. Anchor là vùng thứ nhất; target là vùng thứ hai theo selection history hợp lệ, fallback về active face. Operator chỉ dịch toàn bộ target bằng một delta từ tâm bounding-box phản chiếu; rotation, scale, hướng, shape và offset nội bộ của target được giữ nguyên. UI Symmetry chỉ còn U/V và một action; các property legacy rotation/scale vẫn ẩn để tương thích file cũ. |
| `Overlay` | Bật số island, phần trăm area và texel density trong UV Editor; dùng `Refresh Overlay` để cập nhật nhãn. |
| `UV Map` | Chọn UV map active và tùy chọn duplicate map hiện tại thành `Bake_Optimized` trước các thao tác có thể phá dữ liệu UV. |

### Symmetry position-only

Trong nhóm `Symmetry`, chọn đúng hai vùng topology: anchor trước, target sau.
Selection history hợp lệ quyết định target; nếu history không dùng được thì
active face là fallback. Chọn `U` hoặc `V`, rồi bấm `Mirror Target Position`.
Add-on lấy tâm bounding-box UV của anchor, phản chiếu tâm đó qua `U=0.5` hoặc
`V=0.5`, và cộng cùng một delta cho mọi loop của target. Vì vậy target không bị
xoay, scale, đổi hướng hoặc biến dạng; anchor và mọi loop ngoài target giữ
nguyên. Selection không hợp lệ hoặc không phân giải được target sẽ hủy với
zero-write.

### Pack/Center selected-only và file mở có UV Sync stale

`Pack Selected` (cả `LOCK_UNSELECTED` và `IGNORE_UNSELECTED`) và `Center
Selected` chỉ ghi lên scope UV đang chọn. Nếu file mở khiến Blender báo
`uv_select_sync_valid=False`, add-on refresh trạng thái selection một lần bằng
API edit-mesh/BMesh, giữ nguyên UV coordinates, selection, active face/history,
UV map và mode; không gọi Unwrap, Smart Project, Pack, Select All hoặc thao tác
đổi tọa độ trong bước refresh. Nếu không xác lập được scope tin cậy, thao tác
hủy an toàn và không ghi dữ liệu. `Pack Whole Mesh` vẫn là lựa chọn chủ ý để
cho phép thay đổi toàn bộ mesh.

Tên và nhóm trên đây phản ánh UI/source hiện tại. Hiệu quả thực tế còn phụ thuộc
mesh, UV selection, topology, không gian UV và tùy chọn Blender đang dùng.

Trong nhóm `Stack`, chọn một hoặc nhiều UV island làm target/reference rồi bấm
`Align To Selected`. Các island tương tự nhưng chưa chọn sẽ được tìm và căn
chồng lên target phù hợp; target đã chọn không bị di chuyển. Khi có nhiều target,
mỗi island chưa chọn dùng target có similarity score tốt nhất. `Paste Keep
Position` là thao tác riêng và không dùng dispatch của `Align To Selected`.

### Matcher và CPU policy

`Align To Selected` dùng một implementation Python độc lập của project; nó
không phải engine hay algorithm của UVPackmaster và không tuyên bố tương đương
với UVPackmaster. Matcher dựng ordered UV border loops, phân biệt outer/hole,
resample theo arclength với số mẫu giới hạn, thử cyclic/reverse winding và fit
transform similarity 2D bằng Procrustes/Kabsch-style math. Cheap raw-boundary và
topology gates chạy trước; full descriptors chỉ dựng cho candidate đã qua gate,
cache chỉ sống trong một operator execution.

Mọi thao tác `bpy`/`bmesh` extraction và UV apply đều ở main thread. `AUTO` dùng
single worker cho case fixture thực vì chỉ còn một full fit; NumPy threads chỉ
dành cho batch numeric lớn khi benchmark chứng minh có lợi. ProcessPool đã được
đánh giá nhưng không ship do overhead/lifecycle/serialization trên Windows và
Blender; pure-Python dưới GIL mặc định chạy single.

### MATCH-04 evidence

Trên `C:\Users\linhp\Downloads\cc.blend`, đọc read-only bằng Blender portable
5.0.0, fixture SHA trước/sau giữ nguyên. Fixture được lưu bởi Blender 5.2.44,
vì vậy Blender 5.0 cảnh báo forward-version khi mở file.

- MATCH-01 old matcher: median `634.806 ms`.
- MATCH-02 correct matcher: median `1005.188 ms`.
- MATCH-03 optimized correct matcher: min `642.855 ms`, median `673.303 ms`,
  p95 `720.866 ms`.
- MATCH-03 nhanh khoảng `33.0%` so với MATCH-02 nhưng chậm khoảng `6.1%` so
  với matcher cũ đơn giản trên fixture này; scheduler không phải bottleneck.
- Pruning: `576` candidates → `3` raw-compatible → `1` coarse → `1` topology →
  `1` full fit; đúng `1` candidate thay đổi, selected target immutable, max
  normalized RMS `2.7421e-06`.
- Package smoke từ ZIP đã extract: Blender `5.0.0`, NumPy `1.26.4`, version
  `1.2.6`, operator `uv_gpt.align_to_selected`, 1 warmup + 3 measured;
  min/median/p95 lần lượt `671.906/708.489/728.967 ms`. Cả selected UV và
  selection đều giữ nguyên, incompatible changes `0`, đúng 1 candidate đổi
  trong mỗi run, register/unregister sạch.
- Artifact hiện tại: `uv_gpt_v1.2.6.zip`, 30 entries, `306,757` bytes,
  SHA256 `217B60633748883B589CCEEDAC6860CEED5B55E8775C8843A07A2DC868CA3FA7`.
  ZIP chỉ chứa package `uv_gpt/`; tests, benchmarks, runtime, docs và cache
  không nằm trong artifact.

Benchmark JSON và package smoke evidence nằm trong `benchmarks/`, gồm
`match_03_fixture.json`, `match_03_synthetic.json` và `match_04_package_smoke.json`.
Kết quả chỉ chứng minh các case đo được; không gọi kết quả là “beautiful” và
không mở rộng claim sang UVPackmaster.

## Tắt, bật lại và reload

- Tắt: vào `Preferences` → `Add-ons`, tìm `uv GPT`, bỏ chọn checkbox.
- Bật lại: chọn lại checkbox đó.
- Khi thay ZIP bằng bản mới: tắt add-on, cài `uv_gpt_v1.2.6.zip` trong `Install...`,
  rồi bật lại và kiểm tra version hiển thị trong panel.
- Khi đang phát triển source và UI chưa cập nhật, có thể dùng `F3` →
  `Reload Scripts` hoặc đóng/mở lại Blender; sau đó kiểm tra lại panel và
  operator trên mesh test.

## Giới hạn cần nhớ

- Đây là bộ công cụ UV; tài liệu này không cam kết unwrap tự động hay xử lý
  texture/material.
- Exact-fixture package smoke đã được kiểm tra read-only trong Blender 5.0.0;
  UI context, Undo, multi-target/no-match và kết quả trên asset thật vẫn cần
  người dùng kiểm tra thủ công trên bản sao trước khi dùng cho asset thật.
