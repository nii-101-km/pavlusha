"""Committed-generation publication failures and strict runtime cold-restart regression."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent.checkpoint import validate_checkpoint, _digest
from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError
from pavlusha_agent.project_state import HANDOFF_MAX_BYTES, validate_handoff
from pavlusha_agent.runtime import run_agent
from pavlusha_agent.state_store import StateStore
from tests.test_reasoning_window import init_turn, turn
from tests.test_runtime_lifecycle import run_script
from tests import test_reasoning_loop as reasoning_tests
from tests.test_reasoning_loop import script, LOOP, SHELL


class AtomicCheckpointTests(unittest.TestCase):
    def seed(self, root):
        store=StateStore(root/'state','task')
        store.initialize_project(json.loads(init_turn().content),step=1)
        store.update_project([{'op':'add_design','decision':'COMMITTED_OLD','rationale':'old finding'}],step=2)
        store.complete_project_review(step=3,handoff='HANDOFF_OLD')
        return store

    def prepare_working_changes(self, store):
        store.update_project([{'op':'add_design','decision':'CANDIDATE_NEW','rationale':'new finding'}],step=4)

    def resume(self, root):
        return StateStore(root/'state','task',cold_restart=True).load()

    def test_partial_candidate_state_or_handoff_write_does_not_publish(self):
        for boundary in ('state','handoff'):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); store=self.seed(root)
                committed=copy.deepcopy(store.load()['recovery_checkpoint'])
                self.prepare_working_changes(store)
                actual_open=open
                class PartialWriter:
                    def __init__(self,handle): self.handle=handle
                    def __enter__(self): return self
                    def __exit__(self,*args): self.handle.close()
                    def write(self,data):
                        begin=data.index('"recovery_checkpoint"')
                        field=data.index('"project_state"' if boundary=='state' else '"handoff"',begin)
                        end=field+40 if boundary=='state' else field+len('"handoff": "')+4
                        self.handle.write(data[:end])
                        self.handle.flush(); os.fsync(self.handle.fileno())
                        raise OSError('injected partial '+boundary)
                def open_candidate(path,*args,**kwargs):
                    handle=actual_open(path,*args,**kwargs)
                    return PartialWriter(handle) if Path(path).name.startswith('.state.json.tmp.') else handle
                with patch('builtins.open',side_effect=open_candidate):
                    with self.assertRaisesRegex(OSError,'injected partial'):
                        store.complete_project_review(step=5,handoff='HANDOFF_NEW')
                candidates=list((root/'state').glob('.state.json.tmp.*'))
                self.assertEqual(len(candidates),1)
                with self.assertRaises(json.JSONDecodeError): json.loads(candidates[0].read_text())
                recovered=self.resume(root)
                self.assertEqual(recovered['recovery_checkpoint'],committed)
                self.assertEqual(recovered['project_state'],committed['project_state'])
                self.assertEqual(recovered['checkpoint_handoff'],'HANDOFF_OLD')

    def test_failure_while_building_candidate_keeps_previous_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=self.seed(root)
            old=copy.deepcopy(store.load()['recovery_checkpoint'])
            self.prepare_working_changes(store)
            with patch('pavlusha_agent.state_store.complete_review',side_effect=AgentError('candidate not complete')):
                with self.assertRaises(AgentError):
                    store.complete_project_review(step=5,handoff='HANDOFF_NEW')
            self.assertEqual(self.resume(root)['recovery_checkpoint'],old)

    def test_before_and_after_atomic_replace_choose_exactly_one_generation(self):
        for after in (False,True):
            with self.subTest(after=after), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); store=self.seed(root)
                old=copy.deepcopy(store.load()['recovery_checkpoint'])
                self.prepare_working_changes(store)
                actual_replace=os.replace
                def fail(source,destination):
                    if after: actual_replace(source,destination)
                    raise OSError('at publication boundary')
                with patch('pavlusha_agent.state_store.os.replace',side_effect=fail):
                    with self.assertRaises(OSError): store.complete_project_review(step=5,handoff='HANDOFF_NEW')
                if not after:
                    complete=json.loads(next((root/'state').glob('.state.json.tmp.*')).read_text())
                    self.assertEqual(complete['recovery_checkpoint']['handoff'],'HANDOFF_NEW')
                recovered=self.resume(root)
                checkpoint=recovered['recovery_checkpoint']
                self.assertEqual(checkpoint['generation'],old['generation']+int(after))
                self.assertEqual(checkpoint['handoff'],'HANDOFF_NEW' if after else 'HANDOFF_OLD')
                self.assertEqual(recovered['project_state'],checkpoint['project_state'])
                self.assertEqual(recovered['checkpoint_handoff'],checkpoint['handoff'])
                self.assertEqual(len(checkpoint['project_state']['design']),2 if after else 1)

    def test_directory_fsync_failure_is_not_claimed_as_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=self.seed(root); self.prepare_working_changes(store)
            actual_fsync=os.fsync
            def sync(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError('directory sync failed')
                actual_fsync(fd)
            with patch('pavlusha_agent.state_store.os.fsync',side_effect=sync):
                with self.assertRaisesRegex(OSError,'directory sync failed'):
                    store.complete_project_review(step=5,handoff='HANDOFF_NEW')
            state=self.resume(root)
            self.assertEqual(state['recovery_checkpoint']['generation'],3)
            self.assertEqual(state['checkpoint_handoff'],'HANDOFF_NEW')
            validate_checkpoint(state,'task')

    def test_success_replaces_handoff_and_ignores_uncommitted_top_level_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=self.seed(root); self.prepare_working_changes(store)
            store.complete_project_review(step=5,handoff='HANDOFF_NEW')
            committed=copy.deepcopy(store.load()['recovery_checkpoint'])
            store.update_project([{'op':'add_design','decision':'UNCOMMITTED_LATER','rationale':'later work'}],step=6)
            store.record_operation({'command':'post checkpoint','stdout':'written','exit_code':0})
            live=store.load()
            live['checkpoint_handoff']='UNCOMMITTED_WRONG_HANDOFF'
            store._write_atomic(live)
            recovered=self.resume(root)
            self.assertEqual(recovered['recovery_checkpoint'],committed)
            self.assertEqual(recovered['project_state'],committed['project_state'])
            self.assertEqual(recovered['checkpoint_handoff'],'HANDOFF_NEW')
            self.assertEqual(recovered['counters']['operation'],1)
            op=StateStore(root/'state','task').record_operation({'command':'next','exit_code':0})
            self.assertEqual(op['id'],'OP0002')
            self.assertNotIn('HANDOFF_OLD',json.dumps(recovered['recovery_checkpoint']))

    def test_corrupt_candidate_ignored_but_corrupt_committed_generation_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=self.seed(root)
            expected=copy.deepcopy(store.load()['recovery_checkpoint'])
            candidate=root/'state/.state.json.tmp.other-process'
            candidate.write_text('{"recovery_checkpoint": invalid')
            self.assertEqual(self.resume(root)['recovery_checkpoint'],expected)
            # An old complete image beside a corrupt committed file does not authorize fallback.
            candidate.write_bytes(store.state_path.read_bytes())
            store.state_path.write_text('{"recovery_checkpoint":')
            with self.assertRaisesRegex(AgentError,'RECOVERY FAILED'):
                self.resume(root)

    def test_missing_commit_fails_even_with_newer_work_map_logs_or_handoff(self):
        for artifacts in ((),('candidate',),('world',),('state_without_checkpoint',)):
            with self.subTest(artifacts=artifacts),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); state_dir=root/'state'; state_dir.mkdir()
                work=root/'work'; work.mkdir(); (work/'valuable.py').write_text('def useful(): return 42\n')
                if 'candidate' in artifacts:
                    (state_dir/'.state.json.tmp.dead').write_text(json.dumps({'project_state':json.loads(init_turn().content)}))
                if 'world' in artifacts:
                    for file in ('project_map.json','state.log','experiment.jsonl','history_archive.jsonl','handoff.txt'):
                        (state_dir/file).write_text('useful evidence but no committed Project State')
                if 'state_without_checkpoint' in artifacts:
                    store=StateStore(state_dir,'task')
                    store.initialize_project(json.loads(init_turn().content),step=1)
                    raw=store.load(); del raw['recovery_checkpoint']; store._write_atomic(raw)
                args=build_parser().parse_args(['--workdir',str(work),'--state-dir',str(state_dir),
                    '--model','fake','--worker-context-budget','40000','task'])
                before={p.name:p.read_bytes() for p in state_dir.iterdir()}
                with patch('pavlusha_agent.runtime.shutil.which',return_value='/fake/bwrap'), \
                     patch('pavlusha_agent.runtime.ChatProvider') as provider, \
                     patch('pavlusha_agent.runtime.ProjectMap') as project_map:
                    with self.assertRaisesRegex(AgentError,'RECOVERY FAILED'): run_agent(args)
                    provider.assert_not_called(); project_map.assert_not_called()
                self.assertEqual((work/'valuable.py').read_text(),'def useful(): return 42\n')
                self.assertEqual({p.name:p.read_bytes() for p in state_dir.iterdir()},before)

    def test_uninitialized_state_or_handoff_alone_is_not_a_committed_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=StateStore(root/'state','task')
            raw=store.load(); raw['checkpoint_handoff']='continue useful work'; store._write_atomic(raw)
            with self.assertRaisesRegex(AgentError,'RECOVERY FAILED'):
                self.resume(root)

    def test_matching_generation_integrity_rejects_mixed_state_handoff_or_metadata(self):
        for field in ('project_state','handoff','generation','state_version','operation_count'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); store=self.seed(root); old=copy.deepcopy(store.load()['recovery_checkpoint'])
                self.prepare_working_changes(store); store.record_operation({'command':'new generation','exit_code':0})
                store.complete_project_review(step=5,handoff='HANDOFF_NEW')
                mixed=store.load(); mixed['recovery_checkpoint'][field]=old[field]
                store._write_atomic(mixed)
                with self.assertRaisesRegex(AgentError,'RECOVERY FAILED'):
                    self.resume(root)

    def test_persisted_project_state_is_structurally_validated_without_repair(self):
        def missing(state): del state['project_state']['work']
        def active(state): state['project_state']['recovery']['active']='W999'
        def status(state): state['project_state']['work'][0]['status']='UNKNOWN'
        def evidence(state): state['project_state']['work'][0]['evidence']=None
        def duplicate(state): state['project_state']['work'][1]['id']='W001'
        def counters(state): state['project_state']['counters']['work']=99
        def superseded(state): state['project_state']['work'][1]['status']='SUPERSEDED'
        def initialized(state): state['project_state']['initialized']=False
        def bad_metadata(state): state['generation']=True
        def missing_handoff(state): del state['handoff']
        for change in (missing,active,status,evidence,duplicate,counters,superseded,initialized,bad_metadata,missing_handoff):
            with self.subTest(change=change.__name__),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); store=self.seed(root); raw=store.load(); checkpoint=raw['recovery_checkpoint']
                change(checkpoint)
                checkpoint['sha256']=_digest({k:v for k,v in checkpoint.items() if k!='sha256'})
                store._write_atomic(raw)
                before=store.state_path.read_bytes()
                with self.assertRaisesRegex(AgentError,'RECOVERY FAILED'): self.resume(root)
                self.assertEqual(store.state_path.read_bytes(),before)

    def test_handoff_utf8_boundary_and_rejected_checkpoint_preserve_old_commit(self):
        for text in ('a'*HANDOFF_MAX_BYTES,'я'*(HANDOFF_MAX_BYTES//2)):
            with self.subTest(text=text[:1]),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); store=self.seed(root)
                store.complete_project_review(step=4,handoff=text)
                self.assertEqual(self.resume(root)['checkpoint_handoff'],text)
                old=store.load()
                with self.assertRaises(AgentError): store.complete_project_review(step=5,handoff=text+'a')
                self.assertEqual(store.load(),old)
        for value in ('\ud800',None,123):
            with self.subTest(value=repr(value)),self.assertRaises(AgentError): validate_handoff(value)

    def test_fresh_initial_commit_and_intentional_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            fresh=StateStore(root/'state','task',cold_restart=True)
            self.assertNotIn('recovery_checkpoint',fresh.load())
            with self.assertRaisesRegex(AgentError,'RECOVERY FAILED'): self.resume(root)
            initial=fresh.initialize_project(json.loads(init_turn().content),step=1)
            self.assertEqual(self.resume(root)['project_state'],initial)
            (root/'work').mkdir(); (root/'work/keep').write_text('saved')
            reset=StateStore(root/'state','different task',cold_restart=True,reset=True)
            self.assertFalse(reset.get_project_state()['initialized'])
            self.assertNotIn('recovery_checkpoint',reset.load())
            self.assertEqual((root/'work/keep').read_text(),'saved')

    def test_duplicate_json_fields_and_invalid_numbers_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); store=self.seed(root)
            for raw in ('{"recovery_checkpoint":{},"recovery_checkpoint":{}}','{"version":NaN}'):
                store.state_path.write_text(raw)
                with self.assertRaisesRegex(AgentError,'RECOVERY FAILED'): self.resume(root)

    def test_periodic_review_does_not_publish_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            turns=[init_turn(),turn({'action':'shell','command':'verify'}),
                   turn({'action':'project_update','changes':[{'op':'add_design','decision':'PERIODIC_FINDING','rationale':'new evidence'}]}),
                   turn({'action':'project_update','changes':[{'op':'update_work','id':name,'status':'DONE'} for name in ('W001','W002')]}),
                   turn({'action':'finish','summary':'done'})]
            _,result,error=run_script(root,turns,extra=['--project-review-every','1','--history-high','99','--max-steps','-1'])
            self.assertEqual(result,0,error)
            state=StateStore(root/'state','task').load()
            self.assertEqual(state['recovery_checkpoint']['generation'],1)
            self.assertEqual(state['recovery_checkpoint']['kind'],'initial')
            self.assertEqual(state['recovery_checkpoint']['handoff'],'')
            self.assertNotEqual(state['project_state'],state['recovery_checkpoint']['project_state'])
            recovered=self.resume(root)
            self.assertEqual(recovered['project_state'],state['recovery_checkpoint']['project_state'])
            self.assertEqual(recovered['run']['status'],'running')

    def test_reasoning_loop_recovery_does_not_publish_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            case=reasoning_tests.RuntimeRecoveryTests().run_case(Path(tmp),[script(reasoning=LOOP),script(SHELL),script()])
            self.assertEqual(case['result'],0,case['error'])
            self.assertEqual(case['state']['recovery_checkpoint'],case['before']['recovery_checkpoint'])
            self.assertEqual(case['state']['checkpoint_handoff'],'SEED_HANDOFF')


@unittest.skipUnless(importlib.util.find_spec("tree_sitter") and importlib.util.find_spec("tree_sitter_python"),
                     "requires tree-sitter for current-world Project Map process integration")
class RealProcessCheckpointRecoveryTests(unittest.TestCase):
    def launch(self,root,phase,boundary='none'):
        return subprocess.run([sys.executable,str(Path(__file__).parent/'fixtures/atomic_runtime_process.py'),
                               str(root),phase,boundary],capture_output=True,text=True,timeout=20)

    def test_fresh_second_process_restarts_after_abrupt_exit_with_newer_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            first=self.launch(root,'create')
            self.assertEqual(first.returncode,71,first.stderr)
            raw=json.loads((root/'state/state.json').read_text())
            checkpoint=copy.deepcopy(raw['recovery_checkpoint'])
            self.assertEqual(checkpoint['handoff'],'Verify persisted.py next.')
            self.assertEqual(checkpoint['kind'],'high')
            self.assertIn('UNCOMMITTED',str(raw['project_state']))
            self.assertNotIn('UNCOMMITTED',str(checkpoint['project_state']))
            second=self.launch(root,'resume')
            self.assertEqual(second.returncode,0,second.stderr)
            first_request=json.loads((root/'resumed-request.json').read_text())
            self.assertIn('Verify persisted.py next.',str(first_request))
            self.assertNotIn('UNCOMMITTED',str(first_request))
            self.assertIn('newer.py',str(first_request))
            self.assertEqual((root/'work/newer.py').read_text(),'def newer(): return 2\n')
            logs=[json.loads(x) for x in (root/'state/state.log').read_text().splitlines()]
            self.assertEqual(sum(e['kind']=='project_initialized' for e in logs),1)
            state=json.loads((root/'state/state.json').read_text())
            self.assertEqual(state['run']['status'],'finished')
            self.assertEqual(state['recovery_checkpoint'],checkpoint)

    def test_process_dies_at_publication_boundaries_and_second_process_loads_old_or_new(self):
        for boundary in ('partial_state','partial_handoff','before_replace','after_replace'):
            with self.subTest(boundary=boundary),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                first=self.launch(root,'create')
                self.assertEqual(first.returncode,71,first.stderr)
                old=json.loads((root/'state/state.json').read_text())['recovery_checkpoint']
                failing=self.launch(root,'checkpoint',boundary)
                self.assertEqual(failing.returncode,72,failing.stderr)
                current=json.loads((root/'state/state.json').read_text())['recovery_checkpoint']
                is_new=boundary=='after_replace'
                self.assertEqual(current['generation'],old['generation']+int(is_new))
                self.assertEqual(current['handoff'],'Resume candidate finding.' if is_new else old['handoff'])
                recovered=self.launch(root,'resume')
                self.assertEqual(recovered.returncode,0,recovered.stderr)
                messages=json.loads((root/'resumed-request.json').read_text())
                self.assertIn(current['handoff'],str(messages))
                self.assertEqual('CANDIDATE_COMMITTED' in str(messages),is_new)
                self.assertTrue((root/'work/newer.py').exists())
