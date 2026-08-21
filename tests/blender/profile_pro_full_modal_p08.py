"""PERF-P08 bounded full-577 modal evidence harness."""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_full_modal_p07.py"
SPEC = importlib.util.spec_from_file_location("pro_full_modal_p08_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load full modal harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)

BASE.PACKET_ID = "PERF-P08-FULL-MODAL"
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_08_full_modal.json"),
    )
).resolve()
BASE.MAX_SESSION_SECONDS = 80.0
BASE.MAX_SESSION_TICKS = 30000


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P08 full-modal failed: %s" % exc)
        raise SystemExit(1)
