"""File-path worker entrypoint for Pack V2."""
from __future__ import annotations
import json, os, pickle, sys, time, traceback
from pathlib import Path
_HERE=Path(__file__).resolve().parent
if str(_HERE) not in sys.path: sys.path.insert(0,str(_HERE))
import pack_v2_core

def _atomic_json(path,value):
    tmp=path.with_suffix(path.suffix+'.tmp')
    tmp.write_text(json.dumps(value,separators=(',',':')),encoding='utf-8')
    for attempt in range(4):
        try:
            os.replace(tmp,path)
            return True
        except PermissionError:
            if attempt < 3: time.sleep(0.005 * (attempt + 1))
    try: tmp.unlink(missing_ok=True)
    except OSError: pass
    return False

def main(argv):
    if len(argv)!=4: return 2
    input_path,output_path,progress_path=map(Path,argv[1:])
    try:
        with input_path.open('rb') as h: snapshot=pickle.load(h)
        started=time.perf_counter()
        last_percent=[0.0]
        def progress(value):
            value=dict(value); value['worker_elapsed']=time.perf_counter()-started
            last_percent[0]=max(last_percent[0],float(value.get('percent',0.0)))
            value['percent']=last_percent[0]
            try: _atomic_json(progress_path,value)
            except OSError: pass
        result=pack_v2_core.solve_pack(snapshot,progress)
        payload={'ok':True,'result':result}
    except BaseException as exc:
        payload={'ok':False,'error':f'{type(exc).__name__}: {exc}','traceback':traceback.format_exc(limit=12)}
    tmp=output_path.with_suffix(output_path.suffix+'.tmp')
    with tmp.open('wb') as h: pickle.dump(payload,h,protocol=5)
    os.replace(tmp,output_path)
    return 0 if payload.get('ok') else 1
if __name__=='__main__': raise SystemExit(main(sys.argv))
