"""PERF-P09 wrapper for the current-fixture focused Pro regression."""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_focus_p08.py"
SPEC = importlib.util.spec_from_file_location("pro_focus_p09_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Pro focus harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)

BASE.BASE.BASE.PACKET_ID = "PERF-P09-FOCUS"
BASE.BASE.BASE.RESULT_PATH = Path(
    BASE.BASE.BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_09_focus.json"),
    )
).resolve()


if __name__ == "__main__":
    try:
        BASE.BASE.BASE.main()
    except Exception as exc:
        print("PERF-P09 focus failed: %s" % exc)
        raise SystemExit(1)
