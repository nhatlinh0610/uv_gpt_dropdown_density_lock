"""Atomic apply and progress failures must not corrupt a Blender selection."""
import ast
import math
from pathlib import Path
import pickle
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'uv_gpt'))
import pack_v2_worker as worker


def function(name, namespace):
    tree = ast.parse((ROOT / 'uv_gpt/pack_tools.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROOT / 'uv_gpt/pack_tools.py'), 'exec'), namespace)
    return namespace[name]


class Seq(list):
    def ensure_lookup_table(self): pass
    def index_update(self): pass


class UV:
    def __init__(self, x=0., y=0.): self.uv = (x,y)


class Loop:
    def __init__(self): self.value = UV()
    def __getitem__(self, _): return self.value


class ApplyStabilityTests(unittest.TestCase):
    def apply(self, writes, scope=True, unchanged=True):
        loops = [Loop(), Loop()]
        bm = SimpleNamespace(faces=Seq([SimpleNamespace(loops=loops)]),
            edges=Seq(), verts=Seq(), loops=SimpleNamespace(layers=SimpleNamespace(uv={'UVMap': 'layer'})))
        namespace = {'bpy': SimpleNamespace(data=SimpleNamespace(objects={'test':SimpleNamespace(data=None)})),
            'bmesh': SimpleNamespace(from_edit_mesh=lambda _:bm,
                update_edit_mesh=lambda *a,**k:None), 'math': math,
            '_pack_v2_snapshot_unchanged':lambda *_:unchanged}
        job = {'object_name':'test','uv_map_name':'UVMap','result':{'writes':writes},
            'allowed_write_keys':frozenset({(0,0),(0,1)})}
        if not scope:job.pop('allowed_write_keys')
        result=function('_pack_v2_apply_result',namespace)(job)
        return result, [l.value.uv for l in loops]

    def test_invalid_second_write_has_zero_mutation(self):
        state,uvs=self.apply([(0,0,1.,1.),(1,0,2.,2.)])
        self.assertEqual(state,'invalid');self.assertEqual(uvs,[(0.,0.),(0.,0.)])

    def test_nan_duplicate_and_out_of_scope_rejected(self):
        for bad in [(0,1,float('nan'),1.),(0,0,2.,2.),(0,2,1.,1.)]:
            state,uvs=self.apply([(0,0,1.,1.),bad])
            self.assertEqual(state,'invalid');self.assertEqual(uvs,[(0.,0.),(0.,0.)])

    def test_valid_result_is_applied(self):
        state,uvs=self.apply([(0,0,1.,1.),(0,1,2.,2.)])
        self.assertEqual(state,'applied');self.assertEqual(uvs,[(1.,1.),(2.,2.)])

    def test_absent_write_scope_and_bool_index_fail_closed(self):
        for writes,scope in [([(0,0,1.,1.)],False),([(False,0,1.,1.)],True)]:
            state,uvs=self.apply(writes,scope)
            self.assertEqual(state,'invalid');self.assertEqual(uvs,[(0.,0.),(0.,0.)])

    def test_transient_uv_rna_address_does_not_invalidate_source(self):
        obj=SimpleNamespace(type='MESH',as_pointer=lambda:1,data=SimpleNamespace(as_pointer=lambda:2,
            uv_layers={'UVMap':SimpleNamespace(as_pointer=lambda:4)}))
        valid=function('_pack_v2_source_valid',{'bpy':SimpleNamespace(data=SimpleNamespace(objects={'test':obj}))})
        self.assertTrue(valid({'object_name':'test','object_pointer':1,'data_pointer':2,'uv_map_name':'UVMap','uv_pointer':3}))

    def test_changed_source_uv_is_discarded_before_writes(self):
        state,uvs=self.apply([(0,0,1.,1.),(0,1,2.,2.)],unchanged=False)
        self.assertEqual(state,'changed');self.assertEqual(uvs,[(0.,0.),(0.,0.)])

    def test_source_mesh_replacement_is_invalid(self):
        obj=SimpleNamespace(type='MESH',as_pointer=lambda:1,data=SimpleNamespace(as_pointer=lambda:3))
        namespace={'bpy':SimpleNamespace(data=SimpleNamespace(objects={'test':obj}))}
        valid=function('_pack_v2_source_valid',namespace)
        self.assertFalse(valid({'object_name':'test','object_pointer':1,'data_pointer':2}))
        self.assertFalse(valid({'object_name':'missing'}))

    def test_progress_file_sharing_failure_is_advisory(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)/'progress.json'
            p.write_text('{"percent":1}')
            with patch.object(worker.os,'replace',side_effect=PermissionError('sharing')),patch.object(worker.time,'sleep'):
                self.assertFalse(worker._atomic_json(p,{'percent':2}))
            self.assertEqual(p.read_text(),'{"percent":1}')
            self.assertTrue(worker._atomic_json(p,{'percent':3}))


class PackLaunchRecoveryTests(unittest.TestCase):
    def launch(self, codes, result_attempt=None, cancel_attempt=None, cancelled=False):
        calls=[]
        with tempfile.TemporaryDirectory() as directory:
            job={'input_path':str(Path(directory)/'input.pkl'),
                 'output_path':str(Path(directory)/'result.pkl'),
                 'progress_path':str(Path(directory)/'progress.json'),
                 'snapshot':{},'blender_binary':'fixture','blender_version':(5,2,2),
                 'cancelled':cancelled}
            def popen(*args, **kwargs):
                index=len(calls);calls.append(args[0])
                def wait():
                    if index==result_attempt:
                        with Path(job['output_path']).open('wb') as handle:
                            pickle.dump({'ok':True,'result':{'schema':'pack-v2-result-v1','writes':[]}},handle)
                    if index==cancel_attempt:job['cancelled']=True
                    return codes[index]
                return SimpleNamespace(pid=index+1,wait=wait)
            def publish(target,process):
                if target.get('cancelled'):return False
                target.update(process=process,state='worker');return True
            package=ModuleType('_pack_launch_test')
            package.stack_tools=SimpleNamespace(_v2_publish_process=publish)
            namespace={'__package__':package.__name__,'__file__':str(ROOT/'uv_gpt/pack_tools.py'),
                'Path':Path,'pickle':pickle,
                'subprocess':SimpleNamespace(Popen=popen,DEVNULL=-1),
                'pro_process_runtime':SimpleNamespace(resolve_bundled_python=lambda **_:Path('python')),
                '_pack_v2_worker_environment':lambda:{},'_pack_v2_creation_flags':lambda:0}
            with patch.dict(sys.modules,{package.__name__:package}):
                function('_pack_v2_launch_thread',namespace)(job)
        return job,calls

    def test_native_crash_restarts_once_then_accepts_complete_result(self):
        job,calls=self.launch([3221225477,0],result_attempt=1)
        self.assertEqual(len(calls),2);self.assertEqual(job['worker_retries'],1)
        self.assertEqual(job['state'],'ready_apply');self.assertEqual(job['worker_returncode'],0)

    def test_repeated_native_crash_is_terminal_with_exit_diagnostic(self):
        job,calls=self.launch([-1073741819,3221225477])
        self.assertEqual(len(calls),2);self.assertEqual(job['state'],'failed')
        self.assertIn('3221225477',job['error'])

    def test_python_failure_is_not_retried(self):
        job,calls=self.launch([1])
        self.assertEqual(len(calls),1);self.assertEqual(job['state'],'failed')
        self.assertNotIn('worker_retries',job)

    def test_native_exit_with_output_is_not_retried_or_applied(self):
        job,calls=self.launch([3221225477],result_attempt=0)
        self.assertEqual(len(calls),1);self.assertEqual(job['state'],'failed')
        self.assertNotIn('result',job)

    def test_cancellation_during_native_exit_prevents_restart(self):
        job,calls=self.launch([3221225477],cancel_attempt=0)
        self.assertEqual(len(calls),1);self.assertNotIn('worker_retries',job)
        self.assertNotIn('result',job)

    def test_cancelled_job_never_launches(self):
        job,calls=self.launch([],cancelled=True)
        self.assertEqual(calls,[]);self.assertNotIn('result',job)


if __name__ == '__main__': unittest.main()
