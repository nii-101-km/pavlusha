import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pavlusha_agent.core import AgentError, ProcessCleanupError
from pavlusha_agent.extensions import Event
from pavlusha_modules.orchestration import Orchestration


class OrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.work = root / 'work'
        self.work.mkdir()
        self.state = root / 'state'
        self.context = {'workdir': str(self.work), 'base_url': 'http://localhost/v1',
                        'api_timeout': 5, 'kv_storage_dir': str(root),
                        'server_control_path': str(root/'socket'), 'backend': 'llama-cpp',
                        'modules_path': str(root/'modules.json'), 'model': 'm',
                        'temperature': 0.1, 'max_tokens': 100, 'worker_context_budget': 1000,
                        'reasoning_effort': 'low', 'max_steps': 20, 'history_high': 100,
                        'history_context_high': 0.8, 'output_limit': 4000, 'command_timeout': 20,
                        'review_every': 10, 'live_format': 'human', 'network': False}
        self.module = Orchestration(self.context)
        self.module.on_event(Event('session_started', 's', 0, 0, {'session_dir': str(self.state)}))

    def test_delegate_copies_and_collects_child_output(self):
        (self.work/'source.txt').write_text('input')
        def execute(command, check):
            child_work = Path(command[command.index('--workdir') + 1])
            child_state = Path(command[command.index('--session-dir') + 1])
            (child_work.parent/'exit.json').write_text(json.dumps({'exit_code': 0}))
            self.assertEqual((child_work/'source.txt').read_text(), 'input')
            (child_work/'answer.txt').write_text('done')
            child_state.mkdir()
            (child_state/'session.json').write_text(json.dumps({'status': 'finished'}))
            (child_state/'events.jsonl').write_text(json.dumps({
                'kind': 'turn_completed', 'stop': True, 'result': {'summary': 'done'}}) + '\n')
            return SimpleNamespace(returncode=0)
        with patch('pavlusha_modules.orchestration.shutil.which', return_value='/usr/bin/terminal'), \
             patch.dict('os.environ', {'DISPLAY': ':1'}), \
             patch('pavlusha_modules.orchestration.SlotSnapshot') as snapshot, \
             patch('pavlusha_modules.orchestration.subprocess.run', side_effect=execute):
            result = self.module.delegate({'task': 'test', 'inputs': ['.']}, None).result
        self.assertEqual((result['status'], result['summary'], result['kv_status']), ('finished', 'done', 'restored'))
        snapshot.return_value.save.assert_called_once()
        snapshot.return_value.restore.assert_called_once()
        self.module.collect({'child_id': result['child_id'], 'paths': ['answer.txt']}, None)
        self.assertEqual((self.work/'answer.txt').read_text(), 'done')
        with self.assertRaises(AgentError):
            self.module.collect({'child_id': result['child_id'], 'paths': ['answer.txt']}, None)

    def test_invalid_child_and_escaping_paths_are_rejected(self):
        with self.assertRaises(AgentError):
            self.module.collect({'child_id': '../x', 'paths': ['x']}, None)

    def test_unconfirmed_child_exit_does_not_restore_parent_slot(self):
        with patch('pavlusha_modules.orchestration.shutil.which', return_value='/usr/bin/terminal'), \
             patch.dict('os.environ', {'DISPLAY': ':1'}), \
             patch('pavlusha_modules.orchestration.SlotSnapshot') as snapshot, \
             patch('pavlusha_modules.orchestration.subprocess.run', return_value=SimpleNamespace(returncode=0)):
            with self.assertRaises(ProcessCleanupError):
                self.module.delegate({'task': 'test', 'inputs': []}, None)
            snapshot.return_value.restore.assert_not_called()
