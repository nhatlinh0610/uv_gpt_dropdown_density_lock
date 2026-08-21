"""PERF-P04 wrapper for the existing full-selection Pro harness."""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "profile_pro_full_selection.py"
SPEC = importlib.util.spec_from_file_location("pro_full_selection_p04_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load full-selection harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)
BASE.PACKET_ID = "PERF-P04-FULL"
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / "pro_04_full_selection_1024.json"),
    )
).resolve()


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P04 full-selection failed: %s" % exc)
        raise SystemExit(1)
