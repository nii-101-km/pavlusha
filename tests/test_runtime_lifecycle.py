"""Targeted lifecycle regressions; provider replies are scripted, never live model calls."""
import copy
import io
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from pavlusha_agent.cli import build_parser, main
from pavlusha_agent.core import AgentError, ProviderTurn, ShellResult
from pavlusha_agent.project_state import validate_project_action, HANDOFF_MAX_BYTES
from pavlusha_agent.runtime import run_agent, PromptBudget
from pavlusha_agent.state_store import StateStore
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done


def measured_turn(action, *, reasoning='', tokens=1):
    reply = turn(action, reasoning=reasoning)
    reply.prompt_tokens = tokens
    return reply


def review(text='Next: verify the file.'):
    return turn({'action': 'project_review_complete', 'handoff': text})


def run_script(root, replies, *, extra=(), shell_hook=None, eof=None, task='task'):
    seen = []
    replies = iter(replies)
    def worker(provider, messages, **kwargs):
        seen.append(copy.deepcopy(messages))
        try:
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            return reply
        except StopIteration:
            if eof:
                eof()
            raise
    def shell(workdir, command, **kwargs):
        if shell_hook:
            shell_hook(workdir, command)
        return ShellResult(command, False, 0, False, 'OK', '', 0.01)
    args = build_parser().parse_args(['--no-interactive', '--no-live', '--no-network', '--project-map', 'off',
        '--workdir', str(root/'work'), '--state-dir', str(root/'state'),
        '--model', 'scripted', '--worker-context-budget', '40000', '--max-tokens', '1024',
        '--project-review-every', '0', '--max-steps', '50', *extra, task])
    with patch('pavlusha_agent.runtime.shutil.which', return_value='/fake/bwrap'), \
         patch('pavlusha_agent.provider.ChatProvider.worker_completion', worker), \
         patch('pavlusha_agent.runtime.run_shell', shell), \
         redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        error = None
        try:
            result = run_agent(args)
        except AgentError as exc:
            result, error = None, str(exc)
    return seen, result, error


class LifecycleTests(unittest.TestCase):
    def test_periodic_project_update_recovers_malformed_envelope(self):
        update = {'action': 'project_update', 'changes': [
            {'op': 'add_design', 'decision': 'Use the existing implementation', 'rationale': 'Inspected files'}
        ]}
        malformed = ProviderTurn(content=json.dumps(update)[:-1], reasoning_content='', finish_reason='stop')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen, result, error = run_script(root, [
                init_turn(), turn({'action': 'shell', 'command': 'inspect'}),
                malformed, malformed, turn(update), done('inspect'),
                turn({'action': 'finish', 'summary': 'done'}),
            ], extra=['--project-review-every', '1'])
            self.assertEqual(result, 0, error)
            self.assertIn('{"action":"project_update","changes":[...]}', seen[2][0]['content'])
            for request in seen[2:5]:
                self.assertTrue(any(m['content'].startswith('PERIODIC PROJECT STATE REVIEW') for m in request))
            for request in seen[3:5]:
                feedback = [m['content'] for m in request if m['content'].startswith('INVALID ACTION:')]
                self.assertTrue(feedback)
                self.assertTrue(all('malformed JSON action' in message for message in feedback))
                self.assertNotIn("expected 'shell' or 'finish'", str(feedback))
            state = StateStore(root/'state', 'task').load()
            self.assertEqual(len(state['project_state']['design']), 1)
            self.assertEqual(state['project_state']['last_review_operation'], 1)
            logs = [json.loads(line) for line in (root/'state/state.log').read_text().splitlines()]
            self.assertEqual(sum(x['kind'] == 'periodic_review_acknowledged' for x in logs), 1)
            self.assertFalse((root/'state/history_archive.jsonl').exists())

    def test_three_malformed_project_updates_keep_invalid_action_limit(self):
        malformed = ProviderTurn(content='{"action":"project_update","changes":[{"op":"add_design","decision":"x","rationale":"y"}]',
                                 reasoning_content='', finish_reason='stop')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen, result, error = run_script(root, [
                init_turn(), turn({'action': 'shell', 'command': 'inspect'}),
                malformed, malformed, malformed,
            ], extra=['--project-review-every', '1'])
            self.assertIsNone(result)
            self.assertEqual(error, 'model returned invalid actions three times in a row')
            state = StateStore(root/'state', 'task').load()
            self.assertEqual(state['project_state']['design'], [])
            self.assertEqual(state['project_state']['last_review_operation'], 0)
            self.assertEqual(state['counters']['operation'], 1)

    def test_three_semantically_rejected_updates_stop_before_next_action(self):
        rejected = turn({'action': 'project_update', 'changes': [
            {'op': 'update_work', 'id': 'W999', 'status': 'DONE', 'evidence': ['FILE: result.txt']}
        ]})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen, result, error = run_script(root, [
                init_turn(), rejected, rejected, rejected,
                turn({'action': 'shell', 'command': 'must not run'}),
            ])
            self.assertEqual(len(seen), 4)
            self.assertIsNone(result)
            self.assertEqual(error, 'Project State update was rejected three times in a row')
            state = StateStore(root/'state', 'task').load()
            self.assertEqual(state['operations'], [])
            self.assertEqual([w['status'] for w in state['project_state']['work']], ['ACTIVE', 'PLANNED'])

    def test_malformed_and_semantically_rejected_actions_share_one_limit(self):
        rejected = turn({'action': 'project_update', 'changes': [
            {'op': 'update_work', 'id': 'W999', 'reason': 'unknown work item'}
        ]})
        malformed = ProviderTurn(content='{"action":', reasoning_content='', finish_reason='stop')
        for failures, expected in (
            ([rejected, malformed, rejected], 'Project State update was rejected three times in a row'),
            ([rejected, rejected, malformed], 'model returned invalid actions three times in a row'),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                seen, result, error = run_script(Path(tmp), [init_turn(), *failures])
                self.assertEqual(len(seen), 4)
                self.assertIsNone(result)
                self.assertEqual(error, expected)

    def test_accepted_shell_or_state_change_resets_rejected_action_limit(self):
        rejected = turn({'action': 'project_update', 'changes': [
            {'op': 'update_work', 'id': 'W999', 'reason': 'unknown work item'}
        ]})
        accepted = [turn({'action': 'shell', 'command': 'verify'}),
                    turn({'action': 'project_update', 'changes': [
                        {'op': 'add_design', 'decision': 'Use current files', 'rationale': 'Inspected'}]})]
        for action in accepted:
            with self.subTest(action=action.content), tempfile.TemporaryDirectory() as tmp:
                seen, result, error = run_script(Path(tmp), [
                    init_turn(), rejected, rejected, action, rejected, rejected,
                    done('verified'), turn({'action': 'finish', 'summary': 'done'}),
                ])
                self.assertEqual(result, 0, error)
                self.assertEqual(len(seen), 8)

    def test_repeated_initialization_rejections_reach_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen, result, error = run_script(Path(tmp), [init_turn() for _ in range(4)])
            self.assertEqual(len(seen), 4)
            self.assertIsNone(result)
            self.assertEqual(error, 'Project State initialization was rejected three times in a row')

    def test_rejected_checkpoints_reach_limit_without_archiving_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch('pavlusha_agent.state_store.StateStore.complete_project_review',
                       side_effect=AgentError('candidate rejected')):
                seen, result, error = run_script(root, [init_turn(), review(), review(), review()],
                                                 extra=['--history-high', '1'])
            self.assertEqual(len(seen), 4)
            self.assertIsNone(result)
            self.assertEqual(error, 'Project State review was rejected three times in a row')
            state = StateStore(root/'state', 'task').load()
            self.assertEqual(state['recovery_checkpoint']['generation'], 1)
            self.assertFalse((root/'state/history_archive.jsonl').exists())

    def test_shell_duration_survives_runtime_result_admission_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, result, error = run_script(root, [init_turn(),
                turn({'action': 'shell', 'command': 'verify'}), done('verified'),
                turn({'action': 'finish', 'summary': 'done'})])
            self.assertEqual(result, 0, error)
            state = StateStore(root/'state', 'task').load()
            self.assertEqual(state['operations'][0]['duration_seconds'], .01)

    def test_count_context_first_and_simultaneous_use_one_checkpoint(self):
        for count, reasoning, expected in [(3, 'SMALL_OLD_REASONING', 'recent history'),
                                           (99, 'x'*27000, 'provider-reported prompt usage'),
                                           (3, 'x'*27000, 'recent history')]:
            with self.subTest(count=count, large=len(reasoning)>100), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                seen, result, error = run_script(root, [
                    init_turn(), done('already verified'),
                    measured_turn({'action':'shell', 'command':'verify'}, reasoning=reasoning, tokens=35000 if len(reasoning)>100 else 1),
                    review('Continue with final verification.'),
                    turn({'action':'finish', 'summary':'done'}),
                ], extra=['--history-high', str(count)])
                self.assertEqual(result, 0, error)
                self.assertNotIn('PROJECT CHECKPOINT REQUIRED', str(seen[2][1:]))
                self.assertIn(expected, str(seen[3]))
                self.assertIn('CHECKPOINT HANDOFF', str(seen[4]))
                self.assertNotIn(reasoning, str(seen[4]))
                logs = [json.loads(x) for x in (root/'state/state.log').read_text().splitlines()]
                self.assertEqual(sum(x['kind']=='project_review_completed' for x in logs), 1)
                archive = [json.loads(x) for x in (root/'state/history_archive.jsonl').read_text().splitlines()]
                self.assertEqual({x['step'] for x in archive}, {1,2,3,4})

    def test_handoff_replaced_and_periodic_review_does_not_replace_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen, result, error = run_script(root, [
                init_turn(), done('verified'), review('FIRST_HANDOFF'),
                turn({'action':'shell','command':'verify'}),
                turn({'action':'project_review_skip'}), review('LATEST_HANDOFF'),
                turn({'action':'finish','summary':'done'}),
            ], extra=['--history-high','2','--project-review-every','1'])
            self.assertEqual(result, 0, error)
            self.assertIn('PERIODIC PROJECT STATE REVIEW', str(seen[4]))
            self.assertIn('FIRST_HANDOFF', str(seen[5]))
            self.assertIn('LATEST_HANDOFF', str(seen[6]))
            self.assertNotIn('FIRST_HANDOFF', str(seen[6]))
            state = StateStore(root/'state', 'task').load()
            self.assertEqual(state['recovery_checkpoint']['handoff'], 'LATEST_HANDOFF')
            self.assertNotIn('handoff', state['project_state'])
            self.assertEqual([x['status'] for x in state['project_state']['work']], ['DONE','DONE'])
            self.assertEqual(sum(m['content'].startswith('CHECKPOINT HANDOFF') for m in seen[6]), 1)

    def test_unlimited_and_positive_step_boundary(self):
        script = [init_turn(), done('verified'), review(),
                  turn({'action':'shell','command':'verify'}),
                  turn({'action':'finish','summary':'done'})]
        for limit in ('2', '-1'):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as tmp:
                seen, result, error = run_script(Path(tmp), script, extra=['--max-steps',limit,'--history-high','2'])
                if limit=='2':
                    self.assertEqual(len(seen), 2)
                    self.assertIn('maximum of 2 steps',error)
                else:
                    self.assertEqual(result, 0, error)
                    self.assertEqual(len(seen), 5)
                    self.assertIn('CHECKPOINT HANDOFF', str(seen[3]))

    def test_large_new_entry_is_admitted_when_provider_usage_is_low(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen, result, error = run_script(root, [init_turn(reasoning='x'*50000),
                done('verified'), turn({'action':'finish', 'summary':'done'})])
            self.assertEqual(result, 0, error)
            self.assertEqual(len(seen), 3)
            self.assertFalse((root/'state/history_archive.jsonl').exists())

    def test_context_checkpoint_remains_active_across_state_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen,result,error=run_script(Path(tmp),[
                init_turn(), measured_turn({'action':'shell','command':'verify'},reasoning='x'*27000, tokens=35000),
                done('verify'), review(), turn({'action':'finish','summary':'done'})],
                extra=['--history-high','99'])
            self.assertEqual(result,0,error)
            self.assertIn('PROJECT CHECKPOINT REQUIRED',str(seen[2][1:]))
            self.assertIn('PROJECT CHECKPOINT REQUIRED',str(seen[3][1:]))
            self.assertNotIn('x'*100,str(seen[4]))

    @unittest.skipUnless(importlib.util.find_spec("tree_sitter") and importlib.util.find_spec("tree_sitter_python"), "requires tree-sitter")
    def test_crash_restart_rebuilds_map_and_keeps_committed_state_and_handoff(self):
        for abrupt in (False, True):
            with self.subTest(abrupt=abrupt), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                def child():
                    def shell(work,command):
                        (work/(command+'.py')).write_text(f'def {command}(): return 42\n')
                        if command=='uncommitted' and abrupt:
                            os._exit(17)  # File exists, but this OP was never committed.
                    _,_,error=run_script(root,[init_turn(),
                        turn({'action':'shell','command':'committed'}), review('Verify committed.py next.'),
                        turn({'action':'shell','command':'uncommitted'})],
                        extra=['--history-high','2','--max-steps','4','--project-map','on'], shell_hook=shell)
                    if not error or 'maximum of 4 steps' not in error:
                        os._exit(18)
                process=multiprocessing.get_context('fork').Process(target=child)
                process.start(); process.join(15)
                if process.is_alive():
                    process.kill(); process.join()
                    self.fail('child did not terminate')
                self.assertEqual(process.exitcode,17 if abrupt else 0)
                before=StateStore(root/'state','task').load()
                self.assertEqual(before['recovery_checkpoint']['handoff'],'Verify committed.py next.')
                self.assertEqual(before['counters']['operation'],1 if abrupt else 2)
                def verify_files(work,command):
                    self.assertEqual(command,'verify persisted files')
                    self.assertIn('return 42',(work/'committed.py').read_text())
                    self.assertIn('return 42',(work/'uncommitted.py').read_text())
                seen,result,error=run_script(root,[
                    turn({'action':'shell','command':'verify persisted files'}), done('verify persisted files'),
                    turn({'action':'finish','summary':'recovered'})],
                    extra=['--project-map','on'],shell_hook=verify_files)
                self.assertEqual(result,0,error)
                self.assertIn('Verify committed.py next.',str(seen[0]))
                self.assertIn('uncommitted.py',str(seen[0]))
                self.assertIn('committed.py',str(seen[0]))
                self.assertTrue((root/'work/uncommitted.py').exists())
                logs=[json.loads(x) for x in (root/'state/state.log').read_text().splitlines()]
                self.assertEqual(sum(x['kind']=='project_initialized' for x in logs),1)
                self.assertEqual(StateStore(root/'state','task').load()['run']['status'],'finished')
                # Explicit reset still means fresh controller state, not erased /work.
                fresh=StateStore(root/'state','task',reset=True).load()
                self.assertFalse(fresh['project_state']['initialized'])
                self.assertNotIn('checkpoint_handoff',fresh)
                self.assertTrue((root/'work/committed.py').exists())

    def test_handoff_validation_and_atomic_publication_failure(self):
        for bad in (None, 123, 'я'*(HANDOFF_MAX_BYTES//2+1)):
            with self.subTest(bad_type=type(bad)), self.assertRaises(AgentError):
                validate_project_action({'action':'project_review_complete','handoff':bad})
        with tempfile.TemporaryDirectory() as tmp:
            store=StateStore(Path(tmp),'task')
            store.initialize_project(json.loads(init_turn().content),step=1)
            store.complete_project_review(step=2,handoff='old')
            before=store.load()
            with patch('pavlusha_agent.state_store.os.replace',side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    store.complete_project_review(step=3,handoff='new')
            self.assertEqual(store.load(),before)
            store.complete_project_review(step=4)  # Omission clears the previous handoff.
            self.assertEqual(store.load()['recovery_checkpoint']['handoff'],'')

    def test_restart_after_publication_before_history_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch('pavlusha_agent.runtime._archive_history',side_effect=OSError('archive interrupted')):
                with self.assertRaises(OSError):
                    run_script(root,[init_turn(),done('verified'),review('COMMITTED_HANDOFF')],
                               extra=['--history-high','2'])
            state=StateStore(root/'state','task').load()
            self.assertEqual(state['recovery_checkpoint']['handoff'],'COMMITTED_HANDOFF')
            seen,result,error=run_script(root,[turn({'action':'finish','summary':'recovered'})])
            self.assertEqual(result,0,error)
            self.assertIn('COMMITTED_HANDOFF',str(seen[0]))
            self.assertFalse((root/'state/history_archive.jsonl').exists())

    def test_cli_validation(self):
        for option,value in [('--max-steps','0'),('--max-steps','-2'),
                             ('--history-context-high','nan'),('--history-context-high','inf'),
                             ('--history-context-high','0'),('--history-context-high','1')]:
            with self.subTest(option=option,value=value), patch('sys.argv',['agent.py',option,value,'task']), \
                 patch('pavlusha_agent.cli.run_agent') as run, redirect_stderr(io.StringIO()):
                self.assertEqual(main(),2)
                run.assert_not_called()
        for limit in ('-1','1'):
            with patch('sys.argv',['agent.py','--max-steps',limit,'task']), \
                 patch('pavlusha_agent.cli.run_agent',return_value=0) as run:
                self.assertEqual(main(),0)
                self.assertEqual(run.call_args.args[0].max_steps,int(limit))

    def test_prompt_budget_uses_actual_capacity_and_provider_usage(self):
        budget=PromptBudget(20000,0.9)
        self.assertEqual(budget.high, 18000)
        budget.observe(16000)
        self.assertFalse(budget.needs_checkpoint())
        budget.observe(18000)
        self.assertTrue(budget.needs_checkpoint())
        budget.reset()
        self.assertIsNone(budget.measured)
        self.assertFalse(budget.needs_checkpoint())
