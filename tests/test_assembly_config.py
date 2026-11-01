import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import sys

from pavlusha_agent.assembly_config import load_assembly
from pavlusha_agent.core import AgentError
from pavlusha_modules.loader import load_modules
from tests.test_events import Provider, reply
from project_state_agent import main


class AssemblyConfigTests(unittest.TestCase):
    def test_settings_and_modules_share_one_config(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'assembly.json'
            path.write_text(json.dumps({'runtime': {'model': 'small', 'base_url': 'http://127.0.0.1:8999/v1',
                'worker_context_budget': 7000, 'history_context_high': 0.5, 'interactive': False},
                'modules': [{'factory': 'pavlusha_modules.token_window:create_extension'}]}))
            args = load_assembly(path, workdir=root/'work', session_dir=root/'session', task='task')
            self.assertEqual(args.worker_context_budget, 7000)
            self.assertEqual(args.history_context_high, 0.5)
            self.assertFalse(args.interactive)
            module = load_modules(path, context={'base_url': args.base_url,
                                  'worker_context_budget': args.worker_context_budget})[0]
            self.assertEqual((module.endpoint, module.retain_limit, module.single_limit),
                             ('http://127.0.0.1:8999', 1750, 2800))

    def test_invalid_config_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); path = root/'assembly.json'
            for runtime in ({}, {'model': 'm', 'typo': 1}, {'model': 'm', 'interactive': 'false'},
                            {'model': 'm', 'backend': 'invalid'}):
                with self.subTest(runtime=runtime):
                    path.write_text(json.dumps({'runtime': runtime, 'modules': []}))
                    with self.assertRaises(AgentError):
                        load_assembly(path, workdir=root/'work', session_dir=root/'session', task='task')

    def test_mismatched_window_setting_is_rejected(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); path = root/'assembly.json'
            path.write_text(json.dumps({'runtime': {'model': 'm'}, 'modules': [{
                'factory': 'pavlusha_modules.token_window:create_extension',
                'settings': {'context_tokens': 10000}}]}))
            with self.assertRaisesRegex(AgentError, 'must match'):
                load_modules(path, context={'base_url': 'http://127.0.0.1:8999/v1', 'worker_context_budget': 7000})

    def test_inline_server_and_ambiguous_server_are_validated(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); path = root/'assembly.json'
            runtime = {'model': 'm', 'llama_server': {'argv': ['server']}}
            path.write_text(json.dumps({'runtime': runtime, 'modules': []}))
            args = load_assembly(path, workdir=root/'work', session_dir=root/'session', task='task')
            self.assertEqual(args.llama_server_config, {'argv': ['server']})
            runtime['llama_server_config'] = 'other.json'
            path.write_text(json.dumps({'runtime': runtime, 'modules': []}))
            with self.assertRaisesRegex(AgentError, 'cannot be combined'):
                load_assembly(path, workdir=root/'work', session_dir=root/'session', task='task')

    def test_cli_runs_configured_module_to_completion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); path = root/'assembly.json'
            path.write_text(json.dumps({'runtime': {'model': 'm', 'interactive': False}, 'modules': [
                {'factory': 'pavlusha_modules.finish:create_extension'}]}))
            provider = Provider([reply({'action': 'finish', 'summary': 'verified'})])
            argv = ['project_state_agent.py', '--config', str(path), '--workdir', str(root/'work'),
                    '--session-dir', str(root/'session'), '--', 'task']
            with patch.object(sys, 'argv', argv), patch('project_state_agent.ChatProvider', return_value=provider), patch('sys.stdout'):
                self.assertEqual(main(), 0)
            state = json.loads((root/'session/session.json').read_text())
            self.assertEqual(state['status'], 'finished')
