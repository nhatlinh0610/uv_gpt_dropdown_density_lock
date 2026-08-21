"""Read-only short session probe for P07 integration diagnosis."""

import importlib.util
from pathlib import Path
import sys

import bpy


PROJECT_ROOT = Path(sys.argv[sys.argv.index("--project-root") + 1]).resolve()
path = PROJECT_ROOT / "tests" / "blender" / "profile_pro_modal.py"
spec = importlib.util.spec_from_file_location("p07_session_probe_modal", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)

uv_gpt = module.COMMON.import_addon()
uv_gpt.register()
try:
    import uv_gpt.island_tools as island_tools
    import uv_gpt.stack_tools as stack_tools
    import uv_gpt.uv_utils as uv_utils

    obj = bpy.data.objects.get("PROExact")
    bm, uv_layer, _by_key, _baseline_uv, _baseline_selection = module._prepare_case(
        obj,
        island_tools,
        uv_utils,
    )
    operator, session = module._new_modal_operator(
        stack_tools,
        {"detail_mappings": False},
    )
    for index in range(40):
        result = operator.modal(bpy.context, module._Event("TIMER"))
        print(
            "P07_SESSION_PROBE tick=%d result=%s state=%s done=%s all_none=%s selected=%s records=%s pairs=%s enum_ops=%s enum_slices=%s error=%s"
            % (
                index + 1,
                sorted(str(value) for value in result),
                session.state,
                session.done,
                session.all_islands is None,
                session.report.get("selected_count"),
                session.report.get("planner_record_count"),
                session.report.get("candidate_pairs_processed"),
                session.report.get("enum_primitive_ops"),
                session.report.get("enum_slices"),
                session.error,
            )
        )
        if session.done:
            break
finally:
    uv_gpt.unregister()
