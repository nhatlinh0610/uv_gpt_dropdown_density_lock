# Bottom Pro MCP evidence — 2026-09-15

Status: BLENDER PASS for this fixture; unreleased source fix.

- Blender: 5.2.1 LTS, build 9e2066aef7ef.
- Object: Bottom; active UV map: UVMap; 29,440 mesh faces.
- Scope: select all on a disposable duplicate, UV Sync ON, actual Pro operator through Blender MCP.
- Installed stack_tools.py matched workspace baseline before testing.
- Test used workspace worker path and a temporary in-memory apply function; original installed module restored after testing.
- Baseline operator failed: boundary_component_branch_or_open, 0 targets applied.
- Isolated source diagnosis: 37/549 islands failed graph construction; correcting cuts exposed 21 reversed-winding rejects.
- Correcting UV cuts and reversed winding produced 470 exact targets without fallback.
- A live run exposed WinError 5 replacing progress.json; bounded advisory-progress recovery added.
- A subsequent partial-apply run stopped at 379 targets with UV mismatches. Taking fresh BMesh handles on the tick after undo push passed the next run. This supports the scoped fix; no universal Blender API root-cause claim.

## Final live result

- Selected islands: 549 = 470 applied targets + 68 matched masters + 11 topology singletons.
- Each singleton has no other island with the same full topology bucket; no proven target remained unapplied.
- Worker matched 470; Blender applied 470; 79,945 loop writes checked.
- Maximum absolute result-to-Blender U/V error: 0.0.
- Completion: 100% / 33.4s / 470 exact / 11 skipped. Single-run elapsed evidence, not a benchmark.
- Worker exit code: 0; worker job directory removed; no active Pro job.
- Original Bottom UV coordinates unchanged; test object/mesh removed.
- Original active object shirt low, Object Mode, selection and UV Sync restored; no blend file saved.

## Validation and limits

- Six focused unit regressions passed; changed Python compiled in memory.
- Global full suite, other fixtures, Undo/redo and package/install validation NOT RUN.
- Immutable uv_gpt_v1.2.6.zip unchanged; installed addon restored to its original implementation.
- Source differs intentionally in the three runtime files below.
- Raw snapshot/result/evidence retained locally in .test_runtime/bottom_pro_diagnostic/.

## Fingerprints

- Snapshot SHA256: 12ea9a48889d647f1d057f83b802afabcd657c169ca95aae29c2b5feaae039b1
- pro_exact_v2_core.py: 56c41d384e555d9f9266e0e223a0e1eb04fbdd5a798d749af029d6a9c9960577
- pro_exact_v2_worker.py: 4aa82755ffb4188370220cd1665ffbe1f67f8a9f970075af2c7e02396f17d1dc
- stack_tools.py: 733b2295effa7306b3a9b878735097223b54b36c680ac2570aaff1a8e78be703

- Final accounting hardening was retested on the same snapshot: 470 / 68 / 11, all 79,945 writes byte-for-value unchanged from the live-tested result.
- Final core SHA256: a8c14f35f64e318d6369517bd527f206a1c64cb1f160f2b0f75b122e4c1f473f
- Feature-context validator: FAIL, 26 existing format/duplicate-registry errors remain outside this Pro fix; no full acceptance claim.

## Delivery follow-up — 2026-09-15

User authorized rebuilding the canonical ZIP and Quick Reinstall. ZIP: 356159 bytes, SHA256 d486d1e3accd7307332171320e7cbd10fd0992a0dda80a61140edafd9a3c2af5. Six extracted-ZIP regressions pass. Quick Reinstall completed; 38/38 installed Python files match ZIP and fixed apply function is loaded. Earlier source-only statements above are historical.
