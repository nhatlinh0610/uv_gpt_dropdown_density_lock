"""PERF-P09 bounded full-577 modal evidence harness."""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_full_modal_p08.py"
SPEC = importlib.util.spec_from_file_location("pro_full_modal_p09_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load full modal harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)

BASE.BASE.PACKET_ID = "PERF-P09-FULL-MODAL"
BASE.BASE.RESULT_PATH = Path(
    BASE.BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_09_full_modal.json"),
    )
).resolve()
BASE.BASE.MAX_SESSION_SECONDS = 90.0
BASE.BASE.MAX_SESSION_TICKS = 30000


if __name__ == "__main__":
    try:
        BASE.BASE.main()
    except Exception as exc:
        print("PERF-P09 full-modal failed: %s" % exc)
        raise SystemExit(1)
