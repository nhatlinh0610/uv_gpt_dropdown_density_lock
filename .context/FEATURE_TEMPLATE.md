# Feature Context — {{FEATURE_TITLE}}

> Đây là template, không phải feature đã triển khai. Chỉ copy thành
> `.context/features/<slug>.md` khi workflow thực sự được thay đổi hoặc cần
> knowledge bền vững. Không điền bằng suy đoán từ tên nút.

- Slug: `{{FEATURE_ID}}`
- Status: `scaffold | active | stable | deprecated`
- Last reviewed: `{{YYYY-MM-DD}}`
- Verification state: `not verified | partially verified | verified`
- Primary code/test anchors (project-root-relative, mỗi dòng một `path::symbol`):
  - `{{SOURCE_PATH}}::{{SYMBOL}}`
- Related capsules: `none`

## 1. User Outcome / Current Contract

{{OBSERVABLE_OUTCOME}}

Success means:

- Mô tả kết quả quan sát được, điều kiện context và input hợp lệ.

Non-goals / current limits:

- Ghi rõ giới hạn hoặc behavior chưa triển khai; không mô tả kế hoạch như
  behavior đã shipped.

## 2. Current Execution Map

`input/event` → `owner` → `core decision` → `side effect/output`

Ownership:

- `path::symbol`: một trách nhiệm cụ thể.

## 3. Decision Direction

Khi thay đổi workflow, đánh giá theo thứ tự: observable contract, owner thật,
invariant an toàn/lifecycle, evidence phân biệt các hướng, rồi focused check.

Decision rules:

- Chỉ ghi kết luận và lý do có thể tái sử dụng; không lưu chain-of-thought,
  transcript hoặc log dài.

## 4. Invariants / Safety

- Điều kiện UV/mesh/selection, undo/rollback, version compatibility hoặc
  preservation cần giữ đúng.

## 5. Active Work

- Change type: `none`
- Chỉ dùng section này cho risk/decision chưa xong cần bàn giao; không dùng
  làm nhật ký phiên.

## 6. Improvement / Optimization Opportunities

| Candidate | Evidence or bottleneck | Metric | Trigger to act |
|---|---|---|---|
| None observed | n/a | n/a | Chỉ thêm khi có evidence hoặc ngưỡng rõ ràng |

## 7. Verification / Known Limits

Automated:

- `{{COMMAND}}` — `not run`

Manual:

- [ ] Scenario quan sát được nhỏ nhất.

Known limits / Not Implemented:

- `{{LIMITS_OR_NONE}}`

Regression reference:

- `AI_ERROR_LOG.md::<entry> | none`

Last evidence:

- `{{YYYY-MM-DD}} — not verified`

## 8. Recent Updates — Max 2

Chỉ thêm outcome update có ngày khi context impact là `updated`; trước update
thứ ba phải fold durable knowledge vào sections 1–7 và xóa update cũ nhất.

## Maintenance

Giữ capsule dưới 150 dòng, source anchors relative và registry trong
`.context/INDEX.md` đồng bộ. Nếu context impact là `none`, không tạo capsule.
