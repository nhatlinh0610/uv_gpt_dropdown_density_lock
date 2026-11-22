# Blender harness map

The runtime authority for every package smoke is
`uv_gpt_v1.2.6.zip` (SHA-256
`70A0BBAAAA05B83E423C2573F58B9FD13530899D4092CAFB3620FE3E315473EA`, `672,110` bytes,
`39` runtime files: `38` Python files + `uv_gpt_pack_native.dll`). No harness may
rebuild or modify that ZIP.

## Canonical Z2/Z3 coverage

- `tests/unit/test_zip_authoritative_runtime.py` and the focused unit/static
  tests assert the 39-file ZIP manifest, byte parity, V2 routes, worker entrypoints,
  progress format, active-UV guard and selection/blocker contracts without
  starting Blender.
- Current live evidence reports:
  - `tests/blender/genesis_mcp9876_cpp_pack_v2_report.md` + `tests/blender/genesis_live_cpp_pack_v2_before_after.png` (Blender `5.3.0` MCP `9876`, C++20 32-Thread Native DLL Pack V2 on `GEO_Genesis_Outfit_Upper.001`).
  - `tests/blender/accessories_mcp_report.md` (Blender `5.2.2 LTS` MCP `1234`, `GEO_Accessories_Combined`).
  - `tests/blender/bottom_pro_mcp_evidence.md` (Blender `5.2.1 LTS`, `Bottom` Pro Exact V2).
- The Z3 live harness exercises the user-facing operators
  `uv_gpt.align_similar_pro_fast`, `uv_gpt.align_similar_pro_snap`,
  `uv_gpt.pack_selected`, `uv_gpt.pack_whole_mesh` (plus backend compatibility
  operator `uv_gpt.pack_symmetry`) from the extracted authoritative package. It must
  record nonblocking/progressive behavior, skipped/failed/done-hold reporting,
  exact active-UV selection and the `<= 1e-7` Keep Stack Exact oracle.

## Historical compatibility harnesses

The existing `match_01*`, `match_02*`, `match_04*`, `align_similar_selected*`,
`align_similar_pro*`, `pack_*hotfix*` and `profile_pro_*` scripts retain legacy
operator/backend seams because those classes and modules remain in the ZIP for
`.blend`/fixture compatibility. Their calls to `align_to_selected`,
`_pro_create_session`, legacy Pack settings or the refresh helper are not
canonical V2 acceptance claims. They are preserved as historical diagnostics;
Z3 must not report their results as proof of the current Fast/Pro/Pack V2
workflow.

When a historical runner checks a package manifest, it must still expect the
current 39-file package (`38` Python files + `uv_gpt_pack_native.dll`) and must
never restore deleted fixtures or benchmark artifacts. New live acceptance
evidence belongs in a V2-specific harness.
