"""PERF-P06 wrapper for the focused Pro exact regression."""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
SPEC = importlib.util.spec_from_file_location("pro_focus_p06_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Pro focus harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)

BASE.PACKET_ID = "PERF-P06-FOCUS"
BASE.COOPERATIVE_YIELD_EVERY = None
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_06_focus.json"),
    )
).resolve()


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P06 focus failed: %s" % exc)
        raise SystemExit(1)
