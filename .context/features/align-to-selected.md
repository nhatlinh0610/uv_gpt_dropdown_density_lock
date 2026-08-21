# Feature Context — Align To Selected

- Slug: `align-to-selected`
- Status: `active`
- Last reviewed: `2026-08-18`
- Verification state: `partially verified`
- Primary code/test anchors (project-root-relative, mỗi dòng một `path::symbol`):
  - `uv_gpt/stack_tools.py::UVGPT_OT_align_to_selected`
  - `uv_gpt/stack_tools.py::_align_unselected_to_selected`
  - `uv_gpt/stack_tools.py::_best_align_transform`
  - `uv_gpt/similarity_matcher.py::match_descriptors`
  - `uv_gpt/similarity_matcher.py::DescriptorCache`
  - `uv_gpt/match_scheduler.py::schedule_numeric_batch`
  - `uv_gpt/ui.py::draw_uv_gpt_panel`
  - `uv_gpt/properties.py::UVGPT_Settings`
- Related capsules: `none`

## 1. User Outcome / Current Contract

Trong UV Editor, nhóm `Stack` hiển thị `Match Scale`, `Allow Flipping`,
`Similarity Tolerance`, `Paste Keep Position` và một action `Align To Selected`.
Người dùng chọn một hoặc nhiều island làm target/reference. Action Align tìm
island tương tự nhưng chưa chọn, rồi căn chúng chồng lên target phù hợp.

Success means:

- Selected target island(s) không bị transform bởi action Align.
- Mỗi candidate unselected phù hợp với Border Shape được căn đúng một lần.
- Nếu có nhiều target selected, candidate dùng target có similarity score thấp nhất
  trong tolerance.
- Operator giữ Blender Undo và destructive-ready guard hiện có.

## 2. Current Execution Map

`Stack button` → `UVGPT_OT_align_to_selected` → one main-thread island enumeration
→ selected islands là reference → `_align_unselected_to_selected` dựng cheap
boundary/topology metadata một lần → lazy full descriptor/cache chỉ cho candidate
đã qua gate → ordered cyclic/reverse similarity fit qua AUTO numeric scheduler →
`_apply_align_transform` chỉ ghi UV của candidate.

Ownership: `ui.py::draw_uv_gpt_panel` dispatches the Stack action;
`properties.py::UVGPT_Settings` owns its three settings;
`stack_tools.py::_align_unselected_to_selected` owns scope and apply direction;
`_best_align_transform` is the BMesh/numeric seam; `similarity_matcher.py` owns
loops, holes, resampling, fit, gates and diagnostics; `match_scheduler.py` owns
deterministic numeric scheduling (ProcessPool benchmark-only).

## 3. Decision Direction

Similarity dùng numeric ordered boundary descriptors. Candidate đi qua cheap raw
boundary signature trước, topology/core gate sau đó mới vào cyclic/reverse
Procrustes/Kabsch-style fit; outer và holes được chấm riêng, không trộn loop.
Open/ambiguous/degenerate boundary states và outer/hole structure mismatch bị
loại an toàn; core topology mismatch có penalty fallback trong pure matcher,
nhưng tolerance operator `0.01` giữ gate hiệu dụng strict.
`stack_match_scale` và `stack_allow_flipping` giữ nguyên semantics hiện có.
Với nhiều selected target, transform có score thấp nhất được chọn với face-key
tie-break deterministic. Descriptor cache chỉ tồn tại trong một operator
execution và không giữ BMesh references.

## 4. Invariants / Safety

- `selected_keys` loại selected islands khỏi candidate loop; selected UV chỉ đọc làm reference.
- Chỉ `_apply_align_transform(island, ...)` trên island chưa chọn.
- `ensure_destructive_ready(context)` chạy trước khi lấy và biến đổi UV; operator giữ `REGISTER`/`UNDO`.
- `Paste Keep Position` và behavior pack không bị đổi.

## 5. Active Work

- Change type: `MATCH-04 release/package handoff` (completed).
- `stack_tools.py` gọi matcher và scheduler; `__init__.py` load order không cần đổi.
  AUTO chọn single cho exact fixture vì chỉ có một full fit; không tạo persistent pool.
  Release metadata là `1.2.6`; package smoke chạy từ ZIP extract, không cài persistent.

## 6. Improvement / Optimization Opportunities

| Candidate | Evidence or bottleneck | Metric | Trigger to act |
|---|---|---|---|
| Exact 577-island fixture | MATCH-03 median `673.303 ms`, min `642.855 ms`, p95 `720.866 ms`; MATCH-01 median `634.806 ms`; MATCH-02 median `1005.188 ms` | `33.0%` faster than MATCH-02, but `6.1%` slower than MATCH-01 | MATCH-04 may optimize island enumeration/boundary extraction; do not infer scheduler win from this case |
| AUTO policy | One full fit after pruning; `AUTO → single`, worker count `1` | Scheduler match median about `2.088 ms`; no needless worker spawn | Keep process benchmark-only until a larger safe corpus proves benefit |

## 7. Verification / Known Limits

Automated:

- Pure matcher + scheduler unit suite — `verified`, 27 tests (16 matcher, 11 scheduler), including NumPy/Python parity and lifecycle checks.
- In-memory compile of changed source/harness — `verified`.
- Blender 5.0 exact fixture — `verified`: 1 warmup + 10 measured; selected UV/selection unchanged, incompatible changes `0`, max RMS `2.7421e-06`, pruning `576 → 1 → 1 → 1`.
- Synthetic backends — `verified`: 72/72 Python single, NumPy single, NumPy threads and ProcessPool prototype runs completed with acceptance/transform parity; ProcessPool benchmark-only.
- Phase accounting — `verified`: internal phases plus classified `operator_boundary_overhead`; unexplained overhead `0` in final JSON.
- Packaged artifact smoke — `verified`: extracted `uv_gpt_v1.2.6.zip` reports version
  `1.2.6`, registers/unregisters cleanly and passes exact-fixture correctness smoke;
  the current release keeps the package root and source byte parity. Package smoke timing is
  `671.906/708.489/728.967 ms` min/median/p95 over 1 warmup + 3 measured;
  NumPy `1.26.4`, RMS max `2.7421e-06`, selected/selection immutable and
  incompatible changes `0`.
- `check-feature-context.ps1 -ProjectRoot . -Strict` — verified pass.
- `check-ai-project.ps1 -ProjectRoot . -Strict` — verified pass.

Manual:

- [ ] Trong Blender UI, chọn target/candidate giống border shape; xác nhận candidate chồng lên target còn target giữ nguyên.
- [ ] Chọn nhiều target; xác nhận mỗi candidate dùng target score tốt nhất.
- [ ] Xác nhận candidate không phù hợp tolerance không bị di chuyển.
- [ ] Xác nhận Undo, `Paste Keep Position`, reload/register và lỗi empty selection.

Known limits / Not Implemented:

- Exact fixture live-test dùng in-memory deterministic selection; saved selection
  trong fixture rỗng.
- UI context, Undo, multi-target, no-match, flipping trên real fixture và toàn bộ hole/open/degenerate corpus chưa được Blender harness cover; synthetic unit suite đã cover geometry tương ứng.
- Forward-version warning của fixture (`502.44` mở bằng Blender `5.0.0`) và
  ambient Blender memory warning vẫn xuất hiện; fixture không được save.
- MATCH-03 không đạt MATCH-01 median target: bottleneck là island enumeration/boundary extraction/cheap signatures, không phải scheduling. Không claim “beautiful” từ score; geometry evidence nằm trong JSON.

Last evidence:

- `2026-08-18` — MATCH-04 version/package smoke, exact fixture read-only
  preservation, ZIP hash/structure and validators verified; package manual install
  and UI/Undo/general corpus remain partial.

## 8. Recent Updates — Max 2

### 2026-08-18 — Update 1

- Reason: MATCH-02 thay Border Shape point-cloud bằng ordered loop/hole-aware matcher.
- Code-level change: `similarity_matcher.py`; staged raw/topology gates, bounded resampling, cyclic/reverse Procrustes, reflection/scale flags, per-run cache and diagnostics.
- Automated validation: 16 matcher tests, NumPy/Python parity, exact Blender correctness case; target/selection immutable, RMS max `2.7421e-06`.
- Live verification: deterministic in-memory case verified; UI/Undo and broader corpus pending.

### 2026-08-18 — Update 2

- Reason: MATCH-03 reduced eager full-descriptor work and added safe adaptive numeric scheduling.
- Code-level change: one island enumeration, cheap raw/topology metadata, lazy two-descriptor build, deterministic AUTO scheduler, phase split and benchmark-only ProcessPool.
- Automated validation: 27 unit tests; exact Blender 1+10; synthetic 72/72 parity; fixture/source/harness hashes unchanged and no cache. Median `673.303 ms` beats MATCH-02 but remains above MATCH-01, so claims stay limited.

## Maintenance

Capsule dưới 150 dòng, source anchors relative và registry trong `.context/INDEX.md`
đồng bộ. Khi có Blender evidence mới, cập nhật canonical verification state.
