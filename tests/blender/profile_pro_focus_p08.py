"""PERF-P08 wrapper for the current-fixture focused Pro regression."""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_focus_p07.py"
SPEC = importlib.util.spec_from_file_location("pro_focus_p08_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Pro focus harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)

BASE.BASE.PACKET_ID = "PERF-P08-FOCUS"
BASE.BASE.RESULT_PATH = Path(
    BASE.BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_08_focus.json"),
    )
).resolve()


if __name__ == "__main__":
    try:
        BASE.BASE.main()
    except Exception as exc:
        print("PERF-P08 focus failed: %s" % exc)
        raise SystemExit(1)
