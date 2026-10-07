"""Default terminal launch and explicit restrictive profile through run_agent."""
import copy
import io
import json
import os
import pty
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.cli import build_parser, main
from pavlusha_agent.core import AgentError, ShellResult
from pavlusha_agent.state_store import StateStore
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done


class LaunchProfileTests(unittest.TestCase):
    def test_defaults_and_explicit_overrides(self):
        args = build_parser().parse_args(['task'])
        self.assertEqual((args.live, args.network, args.interactive, args.gui), (True,)*4)
        self.assertEqual((args.project_map, args.reasoning_loop_recovery), ('on','recover'))
        self.assertEqual((args.history_high, args.max_steps, args.history_context_high), (200,-1,.85))
        self.assertIsNone(args.functions)
        self.assertEqual(args.expert, 'off')
        restrictive = build_parser().parse_args([
            '--no-live','--no-network','--no-interactive','--no-gui','--project-map','off',
            '--reasoning-loop-recovery','off','--history-high','30','--max-steps','60','task'])
        self.assertEqual((restrictive.live, restrictive.network, restrictive.interactive, restrictive.gui), (False,)*4)
        self.assertEqual((restrictive.project_map, restrictive.reasoning_loop_recovery), ('off','off'))
        self.assertEqual((restrictive.history_high, restrictive.max_steps), (30,60))
        for flag in ('--live','--network','--interactive','--gui'):
            self.assertTrue(getattr(build_parser().parse_args([flag,'task']), flag[2:]))

    def run_profile(self, extra=(), *, missing_map=False, context_high=False):
        seen, formats, options, commands = [], [], [], []
        first = turn({'action':'shell','command':'verify','network':True})
        if context_high:
            first.prompt_tokens = 35000
        replies = [first]
        if context_high:
            replies.append(turn({'action':'project_review_complete','handoff':'Verified; finish.'}))
        replies.append(turn({'action':'finish','summary':'done'}))
        replies = iter(replies)
        def worker(provider, messages, **kwargs):
            seen.append(copy.deepcopy(messages))
            formats.append(copy.deepcopy(kwargs['response_format']))
            options.append(set(kwargs))
            return next(replies)
        def shell(workdir, command, **kwargs):
            commands.append(kwargs)
            return ShellResult(command,kwargs['network'],0,False,'verified','',.01)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); work=root/'work'; work.mkdir()
            (work/'probe.py').write_text('def probe(): return 1\n')
            store=StateStore(root/'state','task')
            store.initialize_project(json.loads(init_turn().content),step=1)
            store.update_project(json.loads(done('prepared').content)['changes'],step=2)
            store.complete_project_review(step=3,handoff='Inspect current files; finish.')
            # None of the new profile flags is supplied in the default case.
            argv=['agent.py','--workdir',str(work),'--state-dir',str(root/'state'),
                '--model','scripted','--worker-context-budget','40000',*extra,'task']
            master,slave=pty.openpty()
            try:
                with os.fdopen(slave,'r') as terminal, \
                     patch('sys.stdin',terminal), \
                     patch('sys.argv',argv), \
                     patch('pavlusha_agent.runtime.shutil.which',return_value='/fake/bwrap'), \
                     patch('pavlusha_agent.provider.ChatProvider.resolve_model',return_value='scripted'), \
                     patch('pavlusha_agent.provider.ChatProvider.worker_completion',worker), \
                     patch('pavlusha_agent.runtime.run_shell',side_effect=shell), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as output:
                    if missing_map:
                        with patch('pavlusha_agent.runtime.PythonTreeSitterIndexer',
                                   side_effect=AgentError('Project Map requires Tree-sitter')):
                            self.assertEqual(main(),0)
                    else:
                        self.assertEqual(main(),0)
                    logs=output.getvalue()
            finally:
                os.close(master)
        return seen,formats,options,commands,logs

    def test_default_runtime_effective_profile(self):
        seen,formats,options,commands,logs=self.run_profile()
        self.assertTrue(commands[0]['network'])
        ordinary=json.dumps(formats[0])
        self.assertIn('gui_start',ordinary)
        self.assertIn('wait_for_user',ordinary)
        self.assertNotIn('ask_expert',ordinary)
        self.assertNotIn('call_function',ordinary)
        self.assertIn('on_delta',options[0])
        self.assertIn('STEP',logs)
        if 'Project Map unavailable' not in logs:
            self.assertTrue(any(m['content'].startswith('PROJECT MAP') for m in seen[0]))

    def test_restrictive_runtime_profile(self):
        seen,formats,options,commands,logs=self.run_profile([
            '--no-network','--no-gui','--no-interactive','--no-live','--project-map','off',
            '--reasoning-loop-recovery','off','--no-reasoning-recovery'])
        self.assertEqual(commands,[])
        self.assertIn('network_not_granted',str(seen[1]))
        self.assertNotIn('gui_start',json.dumps(formats[0]))
        self.assertNotIn('wait_for_user',json.dumps(formats[0]))
        self.assertNotIn('on_delta',options[0])
        self.assertNotIn('STEP',logs)

    def test_missing_map_dependencies_degrade_without_failing_task(self):
        seen,_,_,commands,logs=self.run_profile(missing_map=True)
        self.assertTrue(commands)
        self.assertIn('Project Map unavailable',logs)
        self.assertFalse(any(m['content'].startswith('PROJECT MAP') for m in seen[0]))

    def test_context_high_precedes_default_200_step_trigger(self):
        _,formats,_,_,_=self.run_profile(context_high=True)
        checkpoint=json.dumps(formats[1])
        self.assertIn('project_review_complete',checkpoint)
        self.assertNotIn('"const": "shell"',checkpoint)

    def test_no_live_retains_interactive_chat_without_worker_progress(self):
        _,formats,_,_,logs=self.run_profile(['--no-live'])
        self.assertIn('wait_for_user',json.dumps(formats[0]))
        self.assertIn('PAUSE',logs)
        self.assertNotIn('STEP',logs)


if __name__ == '__main__':
    unittest.main()
