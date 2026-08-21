"""PERF-P05 wrapper for focused/dedicated Pro interval profiles."""

from pathlib import Path
import importlib.util
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE = PROJECT_ROOT / "tests" / "blender" / "align_similar_pro.py"
SPEC = importlib.util.spec_from_file_location("pro_focus_p05_base", SOURCE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Unable to load Pro focus harness")
BASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BASE
SPEC.loader.exec_module(BASE)


def _yield_every():
    value = BASE._arg_value("--yield-every", "64")
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 64


YIELD_EVERY = _yield_every()
BASE.PACKET_ID = "PERF-P05-Y%s" % YIELD_EVERY
BASE.COOPERATIVE_YIELD_EVERY = YIELD_EVERY
BASE.RESULT_PATH = Path(
    BASE._arg_value(
        "--result",
        str(PROJECT_ROOT / "benchmarks" / ("pro_05_focus_y%s.json" % YIELD_EVERY)),
    )
).resolve()


if __name__ == "__main__":
    try:
        BASE.main()
    except Exception as exc:
        print("PERF-P05 focus failed: %s" % exc)
        raise SystemExit(1)
