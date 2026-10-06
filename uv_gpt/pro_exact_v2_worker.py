"""File-path worker entrypoint for Pro Exact V2."""
from __future__ import annotations
import json
import os
import pickle
from pathlib import Path
import sys
import time
import traceback

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
import pro_exact_v2_core


def _atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
    # Windows can briefly deny replacement while Blender reads progress.json.
    # Progress is advisory: a missed tick must not abort proven UV matches.
    for attempt in range(4):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:
            if attempt < 3:
                time.sleep(0.005 * (attempt + 1))
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def main(argv):
    if len(argv) != 5:
        return 2
    input_path, output_path, progress_path, updates_dir = map(Path, argv[1:])
    try:
        with input_path.open("rb") as handle:
            snapshot = pickle.load(handle)
        started = time.perf_counter()
        updates_dir.mkdir(parents=True, exist_ok=True)
        sequence = [0]
        def progress(value):
            value = dict(value)
            value["worker_elapsed"] = time.perf_counter() - started
            _atomic_json(progress_path, value)
        def match(value):
            sequence[0] += 1
            event = dict(value)
            event["sequence"] = sequence[0]
            final_path = updates_dir / ("%08d.pkl" % sequence[0])
            temp_path = updates_dir / (".%08d.pkl.tmp" % sequence[0])
            with temp_path.open("wb") as handle:
                pickle.dump(event, handle, protocol=5)
            os.replace(temp_path, final_path)
        result = pro_exact_v2_core.solve_exact(snapshot, progress, match_cb=match)
        payload = {"ok": True, "result": result}
    except BaseException as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=12),
        }
    with output_path.open("wb") as handle:
        pickle.dump(payload, handle, protocol=5)
    return 0 if payload.get("ok") else 1

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
