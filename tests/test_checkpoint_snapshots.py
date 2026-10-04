"""Active runtime integration: frozen checkpoints, chronological retention, live evidence."""
import copy
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import ProviderTurn, ShellResult
from pavlusha_agent.runtime import run_agent
from pavlusha_agent.state_store import StateStore
from tests.test_reasoning_window import init_turn, turn, material_evidence


def done(command):
    return turn({'action': 'project_update', 'changes': [
        {'op': 'update_work', 'id': name, 'status': 'DONE', 'evidence': material_evidence(command)}
        for name in ('W001', 'W002')
    ]})


def layer(messages, prefix):
    return next(m for m in messages if m['content'].startswith(prefix))


class CheckpointSnapshotTests(unittest.TestCase):
    def run_case(self, turns, *, extra=(), edit=None, setup=None):
        seen = []
        self.archived_before_request = []
        self.request_formats = []
        self.project_before_request = []
        replies = iter(turns)
        def worker(provider, messages, **kwargs):
            seen.append(copy.deepcopy(messages))
            self.request_formats.append(copy.deepcopy(kwargs['response_format']))
            self.project_before_request.append(StateStore(root/'state', 'task').get_project_state())
            archive = root/"state/history_archive.jsonl"
            self.archived_before_request.append(archive.read_text() if archive.exists() else "")
            return next(replies)
        def shell(workdir, command, **kwargs):
            if edit:
                edit(workdir, command)
            return ShellResult(command, False, 0, False, 'OK', '', 0.01)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); work = root/'work'; work.mkdir()
            if setup:
                setup(work)
            args = build_parser().parse_args([
                '--workdir', str(work), '--state-dir', str(root/'state'),
                '--worker-context-budget', '30000', '--model', 'fake',
                '--project-review-every', '0', '--max-steps', str(len(turns)), *extra, 'task'])
            with patch('pavlusha_agent.runtime.shutil.which', return_value='/fake/bwrap'), \
                 patch('pavlusha_agent.provider.ChatProvider.worker_completion', worker), \
                 patch('pavlusha_agent.provider.ChatProvider.stateless_completion', side_effect=AssertionError('no manager')), \
                 patch('pavlusha_agent.runtime.run_shell', side_effect=shell), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(run_agent(args), 0)
            archive = root/'state/history_archive.jsonl'
            archived = [json.loads(l) for l in archive.read_text().splitlines()] if archive.exists() else []
            self.assertFalse((root/'state/reasoning_archive.jsonl').exists())
            state = StateStore(root/'state', 'task').load()
        return seen, archived, state

    @unittest.skipUnless(importlib.util.find_spec('tree_sitter') and importlib.util.find_spec('tree_sitter_python'), 'requires tree-sitter')
    def test_high_freezes_serialized_map_state_until_accepted_completion(self):
        def setup(work):
            (work/'module.py').write_text('def before(): pass\n')
        def edit(work, command):
            if command == 'edit':
                (work/'module.py').write_text('def after(): pass\n')
        def update(decision):
            return turn({'action': 'project_update', 'changes': [
                {'op': 'add_design', 'decision': decision, 'rationale': 'observed evidence'},
            ]})
        seen, archived, state = self.run_case([
            init_turn(reasoning='OLD_PLAN'),
            turn({'action': 'shell', 'command': 'edit'}, reasoning='OLD_ACTION'),
            update('ORDINARY_CHANGE'),
            update('HIGH_CHANGE_ONE'),
            update('HIGH_CHANGE_TWO'),
            turn({'action': 'project_review_complete', 'handoff': 'NEXT_ACTION'}, reasoning='REVIEW_ACTION'),
            turn({'action': 'shell', 'command': 'after reset'}),
            done('after reset'),
            turn({'action': 'finish', 'summary': 'done'}),
        ], extra=['--project-map', 'on', '--history-high', '3'], setup=setup, edit=edit)

        def snapshots(request):
            return json.dumps([layer(request, 'PROJECT MAP'), layer(request, 'PROJECT STATE')],
                              ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        baseline = snapshots(seen[2])  # last ordinary request before HIGH
        for index in range(3, 6):
            with self.subTest(step=index + 1):
                self.assertEqual(snapshots(seen[index]), baseline)
                self.assertEqual(seen[index][:4], seen[2][:4])
                self.assertIn('OLD_PLAN', str(seen[index]))
                self.assertIn('ORDINARY_CHANGE', str(seen[index][4:]))
                self.assertNotIn('ORDINARY_CHANGE', layer(seen[index], 'PROJECT STATE')['content'])
                self.assertFalse(self.archived_before_request[index])
                self.assertTrue(seen[index][-1]['content'].startswith('CURRENT RUNTIME PHASE: HIGH CHECKPOINT'))
                schema = self.request_formats[index]['json_schema']['schema']
                self.assertEqual({b['properties']['action']['const'] for b in schema['anyOf']},
                                 {'project_update', 'project_review_complete'})
        for index, decision in ((4, 'HIGH_CHANGE_ONE'), (5, 'HIGH_CHANGE_TWO')):
            self.assertIn(decision, str(seen[index][4:]))
            self.assertIn(decision, str(self.project_before_request[index]['design']))
            self.assertNotIn(decision, layer(seen[index], 'PROJECT STATE')['content'])
        refreshed = seen[6]
        self.assertNotEqual(snapshots(refreshed), baseline)
        self.assertIn('after', layer(refreshed, 'PROJECT MAP')['content'])
        self.assertNotIn('def before', layer(refreshed, 'PROJECT MAP')['content'])
        for decision in ('ORDINARY_CHANGE', 'HIGH_CHANGE_ONE', 'HIGH_CHANGE_TWO'):
            self.assertIn(decision, layer(refreshed, 'PROJECT STATE')['content'])
        self.assertIn('NEXT_ACTION', layer(refreshed, 'CHECKPOINT HANDOFF')['content'])
        self.assertNotIn('OLD_PLAN', str(refreshed))
        self.assertNotIn('OLD_ACTION', str(refreshed))
        self.assertNotIn('REVIEW_ACTION', str(refreshed))
        self.assertNotIn('PROJECT STATE UPDATED:', str(refreshed))
        self.assertNotIn('CURRENT RUNTIME PHASE: HIGH CHECKPOINT', str(refreshed))
        self.assertEqual(len(refreshed), 6)  # system/task/map/state/handoff/reset notice
        self.assertTrue(self.archived_before_request[6])
        self.assertEqual({record['step'] for record in archived}, set(range(1, 7)))
        schema = self.request_formats[6]['json_schema']['schema']
        self.assertEqual({b['properties']['action']['const'] for b in schema['anyOf']},
                         {'shell', 'finish', 'project_update'})
        self.assertEqual(state['recovery_checkpoint']['step'], 6)
        self.assertEqual(state['counters']['operation'], 2)

    def test_voluntary_checkpoint_button_is_rejected_below_high(self):
        seen, archived, state = self.run_case([
            init_turn(), turn({'action': 'shell', 'command': 'verify'}), done('verify'),
            turn({'action': 'project_review_complete'}),
            turn({'action': 'shell', 'command': 'after rejected review'}),
            turn({'action': 'finish', 'summary': 'done'}),
        ])
        baseline = layer(seen[1], 'PROJECT STATE')
        for req in seen[2:]:
            self.assertEqual(baseline, layer(req, 'PROJECT STATE'))
        self.assertIn('PROJECT REVIEW REJECTED', str(seen[4]))
        self.assertFalse(archived)
        self.assertFalse(any(e.get('kind') == 'project_review_completed' for e in state.get('logs', [])))
        self.assertTrue(state['project_state']['initialized'])

    def test_overflow_keeps_whole_delta_until_checkpoint_then_resets_all(self):
        seen, archived, _ = self.run_case([
            init_turn(reasoning='PLAN_OLD'),
            turn({'action': 'shell', 'command': 'first'}, reasoning='THINK_OLD'),
            turn({'action': 'shell', 'command': 'second'}, reasoning='THINK_SECOND'),
            turn({'action': 'shell', 'command': 'blocked'}, reasoning='BLOCKED_THINK'),
            done('second'), turn({'action':'project_review_complete'}, reasoning='REVIEW_THINK'),
            turn({'action':'shell','command':'after reset'}, reasoning='NEW_DELTA'),
            turn({'action':'finish','summary':'done'}),
        ], extra=['--history-high','3','--project-map','on'])
        # Crossing HIGH does not cut history, and checkpoint work may grow beyond HIGH.
        for i in (3,4,5):
            self.assertIn('PLAN_OLD',str(seen[i]))
            self.assertIn('THINK_OLD',str(seen[i]))
            self.assertIn('THINK_SECOND',str(seen[i]))
            self.assertFalse(self.archived_before_request[i])
        self.assertIn('PROJECT CHECKPOINT REQUIRED',str(seen[3]))
        self.assertEqual({x['step'] for x in archived}, set(range(1,7)))
        self.assertEqual([x['kind'] for x in archived if x['step']==2], ['reasoning','message','shell_result'])
        self.assertEqual(len(seen[6]),5)  # baseline + one intentional-reset notice
        self.assertIn('CONTEXT NOTICE',seen[6][-1]['content'])
        self.assertNotIn('THINK_OLD',str(seen[6]))
        self.assertNotIn('REVIEW_THINK',str(seen[6]))
        self.assertIn('NEW_DELTA',str(seen[7]))
        self.assertEqual(seen[6][:4],seen[7][:4])

    def test_empty_and_invalid_turn_reasoning_is_retained_in_its_own_step(self):
        seen, _, _ = self.run_case([
            init_turn(), ProviderTurn('', 'EMPTY_REASON', 'length'),
            ProviderTurn('bad json', 'INVALID_REASON', 'stop'),
            turn({'action':'shell','command':'verify'}), done('verify'),
            turn({'action':'finish','summary':'done'}),
        ])
        text=[m['content'] for m in seen[3]]
        i=next(i for i,t in enumerate(text) if t.endswith('EMPTY_REASON'))
        self.assertIn('no final action', text[i+1])
        self.assertTrue(text[i+2].endswith('INVALID_REASON'))
        self.assertEqual(text[i+3], 'bad json')
        self.assertIn('INVALID ACTION', text[i+4])

    def test_overflow_review_does_not_revalidate_worker_selected_evidence(self):
        def edit(work, command):
            if command == 'create':
                (work/'evidence.txt').write_text('verified')
            elif command == 'remove':
                (work/'evidence.txt').unlink()
        seen, archived, _ = self.run_case([
            init_turn(), turn({'action':'shell','command':'create'}, reasoning='BEFORE_REVIEW'),
            turn({'action':'project_update','changes':[
                {'op':'update_work','id':'W001','evidence':['FILE: evidence.txt']}]}),
            turn({'action':'shell','command':'remove'}),
            turn({'action':'project_review_complete'}),
            turn({'action':'shell','command':'verify'}), done('verify'),
            turn({'action':'finish','summary':'done'}),
        ], edit=edit, extra=["--history-high","4"])
        self.assertNotIn('PROJECT REVIEW REJECTED', str(seen[4]))
        self.assertIn('BEFORE_REVIEW', str(seen[4]))
        self.assertEqual(len(seen[5]), 4)  # fresh baseline plus reset notice
        self.assertFalse(self.archived_before_request[4])
        self.assertNotIn('BEFORE_REVIEW', str(seen[5]))
        self.assertEqual({x['step'] for x in archived}, set(range(1,6)))

    @unittest.skipUnless(importlib.util.find_spec('tree_sitter') and importlib.util.find_spec('tree_sitter_python'), 'requires tree-sitter')
    def test_file_changes_stay_out_of_frozen_map_until_checkpoint(self):
        def setup(work):
            (work/'old.py').write_text('def old(): pass\n')
            (work/'edit.py').write_text('def before(): pass\n')
        def edit(work,command):
            if command=='create': (work/'new.py').write_text('def created(): pass\n')
            elif command=='edit': (work/'edit.py').write_text('def after(): pass\n')
            elif command=='delete': (work/'old.py').unlink()
        seen, _, _ = self.run_case([
            init_turn(), turn({'action':'shell','command':'create'}),
            turn({'action':'shell','command':'edit'}),
            turn({'action':'project_update','changes':[
                {'op':'update_work','id':'W001','evidence':['FILE: edit.py']}]}),
            turn({'action':'shell','command':'delete'}),
            turn({'action':'shell','command':'blocked'}),
            turn({'action':'project_review_complete'}),
            turn({'action':'shell','command':'verify'}), done('verify'),
            turn({'action':'finish','summary':'done'}),
        ], extra=['--project-map','on','--project-review-every','3','--history-high','5'],setup=setup,edit=edit)
        first=layer(seen[0],'PROJECT MAP')
        for req in seen[1:7]: self.assertEqual(first, layer(req,'PROJECT MAP'))
        self.assertIn('PROJECT STATE UPDATED', str(seen[4]))
        fresh=layer(seen[7],'PROJECT MAP')
        self.assertNotEqual(first,fresh)
        self.assertIn('created',fresh['content']); self.assertIn('after',fresh['content'])
        self.assertNotIn('old.py',fresh['content']); self.assertNotIn('before',fresh['content'])
        self.assertIn('PROJECT CHECKPOINT REQUIRED',str(seen[5]))
        self.assertIn('PROJECT CHECKPOINT REQUIRED',str(seen[6]))
        self.assertEqual(fresh,layer(seen[7],'PROJECT MAP'))
        for req in seen[7:]:
            self.assertEqual(req[:4],seen[7][:4])
            self.assertTrue(req[2]['content'].startswith('PROJECT MAP'))
            self.assertTrue(req[3]['content'].startswith('PROJECT STATE'))
            self.assertFalse(any('RECENT RAW REASONING' in m['content'] for m in req))

if __name__=='__main__':unittest.main()
