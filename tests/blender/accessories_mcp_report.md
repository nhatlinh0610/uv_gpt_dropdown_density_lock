# GEO_Accessories_Combined — MCP1234 audit, 2026-10-07

Final source, canonical ZIP and installed runtime are byte-identical. Acceptance
is limited to the concrete cases below, rather than general Blender-version,
Undo/redo, artistic-quality or optimal-packing certification.

## Runtime and preservation

- Blender 5.2.2 LTS, build `d13f752e3b9c`, interactive TCP MCP port1234.
- Exact source: `GEO_Accessories_Combined`,9577 vertices,8378 faces.
- UVMap and UVMap.001;149 visible UV islands,8362 visible faces,16 hidden.
- Tests ran on disposable copied mesh data. Both source UV maps, pins, vertex
  coordinates and topology are unchanged; original selection/history, active
  object/map, Edit Mode and addon/tool settings are restored.
- Temporary test object removed. The production blend was not saved.
- Raw inputs/results, fixture, historical failed candidates and final gates:
  `.test_runtime/accessories_20261007/`.

## Defects reproduced and repaired

| Feature | Observed defect | Repair |
|---|---|---|
| Single Symmetry U/V | Calls center/PCA align, so choosing U does not reflect UV | Reflect selected-region loop coordinates across U=.5/V=.5, preserving scale |
| Square Face Sync OFF | Quad0 selected, stale active100; four unrelated loops changed | Context-aware modern/legacy UV flags; no stale active/history fallback |
| Paste Sync OFF | One selection reports149 centers; empty selection still runs | Context-aware selected-island scope; empty selection cancels |
| Worker admission | Fast admitted while Pack active | Shared Fast/Pro/Pack guard |
| Pack Symmetry geometry | Symmetric centers but corresponding mirror error0.0306700131 | Complete bijective topology mapping and per-loop reflection |
| Pack Symmetry performance | Pair worker82.262s; full probe cancelled after150s | Bounded AABB candidate search plus final polygon validation |
| Keep Stack Exact | Pack restores47 of88 known Pro followers;41 stacks lost | UV-labelled correspondence handles symmetric graph automorphisms; all88 restored |
| Pack Margin | Adjacent movable islands receive half requested gap | Full movable-envelope clearance and float32-safe spacing |
| UV boundary accuracy | Sampling drops a protruding boundary point; cardinal layout misses margin by0.00047116 | Keep every boundary point and include every written UV corner in placement bounds |
| Keep Current Scale | Already packed source can be rejected by coarse layout search | Verify/reuse an existing valid layout at scale1 before relocation search |
| Worker lifecycle | Removed/replaced source waits; later invalid write can follow earlier mutation | Durable source identity, staged complete write validation and terminal cleanup |
| Pro progressive apply | Source UV edit can be noticed after an earlier batch | Validate whole expected source before every progressive batch |
| Progress | Windows sharing error aborts work; refinement percent regresses | Advisory publication with bounded retries and monotone progress |
| Native Pack process | One observed Windows0xC0000005 exit before output | Restart once only for result-free native exit; repeat/Python failure/cancel stays terminal |

## Feature matrix

| Group | Final cases | Result |
|---|---|---|
| Center/transform | Center, Mirror X,90/180 rotation, empty selection, Sync ON/OFF, both maps | PASS numeric/protected-UV checks |
| Density read/apply | Selected/whole, target, px/cm and px/unit,1024/2048/4096/custom | PASS; whole relative error<5.45e-6 |
| Square/Grid | One quad, stale active, empty selection, Sync ON/OFF; visible quad grid | PASS;8292 quads in131 components,70 nonquads and16 hidden skipped |
| Paste | Native clipboard plus selected-only center restore; empty selection | PASS; one selected island reports1 center |
| UV maps | Actual active map, duplicate to Bake_Optimized, duplicate-before-density/mirror | PASS; original/inactive maps unchanged |
| Overlays | Number/area/density flags, refresh, hide selected TD | PASS149 label data; visual ergonomics not certified |
| Single Symmetry | U/V, Sync ON/OFF, second map and duplicate-before | PASS8 cases; max Blender reflection error2.98e-8 |
| Pair Symmetry | Anchor/target U/V, Sync ON/OFF | PASS mirrored centers and similarity transform; preserves target winding |
| Fast/Pro | All149 islands, write scope, counts, application and cleanup | PASS; details below |
| Pack | Selected/whole, normal/cardinal rotation, scale1, exact stack, symmetry U/V | PASS live plus independent geometry checks |
| Worker lifecycle | Nine mutual-admission checks; map/mode wait-return, edit, replaced/deleted source | PASS; changed source cannot overwrite user edits |
| Invalid worker writes | Fast/Pro/Pack invalid later coordinate | PASS; zero partial mutation |
| Compatibility presets | Add/remove/defaults/target/apply; collection restored | PASS; hidden compatibility operators |
| Quick Reinstall | Disable/install/enable, register and save preferences | PASS38-file source/ZIP/installed byte parity |

## Worker results

Times measure the worker's computation, excluding owner-thread apply/polling.

| Action | Islands | Geometry/accounting | Worker time | Result |
|---|---:|---|---:|---|
| Pro Exact | 149 | 88 followers | 1.078 s | PASS |
| Fast | 149 | 75 followers | 3.802 s | PASS |
| Pack Whole + Keep Stack | 149 | 88 stack followers; scale 1.041862 | 3.744 s | PASS |
| Pack Whole Cardinal | 149 | 0 stack followers; scale 0.887729 | 0.667 s | PASS |
| Pack Selected Keep Current Scale | 149 | 0 stack followers; scale 1.000000 | 0.231 s | PASS |
| Pack Selected: 2 +147 static | 2 | 0 stack followers; scale 0.664286 | 16.503 s | PASS |
| Pack Symmetry U | 149 | 0 stack followers; scale 0.406108; 58 pairs +33 singles | 2.853 s | PASS |
| Pack Symmetry V | 149 | 0 stack followers; scale 0.161567; 58 pairs +33 singles | 3.012 s | PASS |
| Pack Symmetry + Keep Stack | 149 | 88 stack followers; scale 0.128250 | 1.961 s | PASS |
| Pack Symmetry U: 2 +147 static | 2 | 0 stack followers; scale 0.314921; 1 pairs +0 singles | 1.083 s | PASS |
| Pack Symmetry V: 2 +147 static | 2 | 0 stack followers; scale 0.039127; 1 pairs +0 singles | 1.233 s | PASS |

Pro accounts for all149 islands as88 aligned followers,32 masters and29
unmatched topology singletons. All88 candidate mappings succeed; zero fallback
failures. Equal face count does not imply identical UV topology:24-face singleton
1250 has54 edges/31 UV graph vertices/1 boundary; matched24-face groups have
56/32/2 or58/35/1. Stacking unrelated corner fans would fabricate correspondence.
Fast's tolerance/scale/flipping controls govern approximate Fast matching;
they do not change Pro Exact's topology acceptance.

## Independent geometry acceptance and limits

The independent oracle checks selected/static writes, reconstructed UV boundary
geometry, full margin, tile bounds and corresponding reflection. Pro-derived
master/follower relations validate intentional stack overlap through the common
master, including graph automorphisms. All88 original follower relations remain
exact after both stack-preserving Pack cases.

All nine final geometry cases pass: no unrelated movable overlap, no movable
margin violation and no selected island outside the tile. The two selected-pair
U/V cases preserve all147 static islands. Collision checks use complete boundary
loops; decimated silhouettes are not used for acceptance.

Full U/V layouts contain58 proven reflected pairs and33 rigid singles; mirror
residual is at most2.23e-16 in worker coordinates. Singles without a compatible
partner are positioned on the axis; Pack does not reshape them into self-symmetry.
For a paired mesh with unequal world areas, one common mirror scale cannot also
force equal texel density on both sides.

Selected-only packing leaves43 inherited static out-of-tile islands and24
static-static margin contacts unchanged. These are outside the movable scope.
Keep Current Scale preserves scale; a layout that cannot meet tile, blocker and
margin constraints must report failure without writing UVs.

The raw log retains earlier failed candidates, including the one native worker
exit. The narrowed recovery path passed six regression scenarios and a live test
with an injected first native exit followed by a real worker: exactly2 launches,
1 retry, valid result applied and temp directory removed. The native crash's
underlying OS/runtime cause has not been established.

## Release gates

- 80/80 focused tests passed from the extracted final ZIP; zero skips.
- All38 runtime Python files compile; ZIP CRC and source/package/install parity pass.
- Canonical `uv_gpt_v1.2.6.zip`: 367342 bytes.
- SHA256: `4006c09f75cb3653b58a26f3903fc05bb20135376b80767e4a287158b08222e3`.
- Quick Reinstall enabled the addon in the port1234 Blender instance.
- Four feature capsules pass strict context validation. Historical full repository
  tests are not globally green; unrelated old failures were not silently fixed.
- General Undo/redo, every possible setting combination, other objects and Blender
  versions beyond the named5.2.2 runtime remain unverified in this audit.
