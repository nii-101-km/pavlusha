"""Manifest validation and State/Map composition through the real runtime."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from pavlusha_agent.core import AgentError
from pavlusha_agent.extensions import EventHost
from pavlusha_agent.runtime import Runtime
from pavlusha_modules.loader import load_modules
from tests.test_events import Provider
from tests.test_project_state_module import StateFixture, initialize, shell, review, done, finish
from project_state_agent import module_context, main
from pavlusha_agent.cli import build_parser


class ModuleLoaderTests(StateFixture, unittest.TestCase):
    def test_failed_module_loading_cleans_prepared_server_directory(self):
        directory = self.root/'kv'
        directory.mkdir()
        server = unittest.mock.MagicMock()
        server.prepare_kv_directory.return_value = str(directory)
        config = self.root/'assembly.json'
        config.write_text(json.dumps({'modules': [], 'runtime': {'model': 'worker',
                           'llama_server_config': 'server.json', 'interactive': False}}))
        argv = ['project_state_agent.py', '--workdir', str(self.work),
                '--session-dir', str(self.root/'session'), '--config', str(config), 'task']
        with patch.object(sys, 'argv', argv), patch('project_state_agent.ManagedLlamaServer', return_value=server), \
             patch('project_state_agent.load_modules', side_effect=AgentError('bad module')) as load, \
             patch('sys.stderr'):
            self.assertEqual(main(), 2)
        self.assertEqual(load.call_args.kwargs['context']['kv_storage_dir'], str(directory))
        server.start.assert_not_called()
        server.__exit__.assert_called_once_with(None, None, None)

    def test_launch_settings_are_exposed_to_modules(self):
        parser = build_parser()
        parser.add_argument('--review-every', type=int, default=10)
        parser.add_argument('--modules', type=Path, default=self.root/'modules.json')
        parser.add_argument('--interactive', default=None)
        parser.add_argument('--live-format', default='human')
        args = parser.parse_args(['--workdir', str(self.work), '--session-dir', str(self.root/'session'),
                                  '--model', 'worker', '--base-url', 'http://localhost:9000/v1',
                                  '--temperature', '0.3', '--history-high', '37'])
        context = module_context(args, self.work, self.root/'session')
        self.assertEqual(context['base_url'], 'http://localhost:9000/v1')
        self.assertEqual(context['model'], 'worker')
        self.assertEqual(context['temperature'], 0.3)
        self.assertEqual(context['history_high'], 37)
        self.assertEqual(context['session_dir'], str(self.root/'session'))
        self.assertIsNone(context['llama_server_config'])
        self.assertIsNone(context['kv_storage_dir'])
        self.assertEqual(context['modules_path'], str((self.root/'modules.json').resolve()))
        self.assertEqual(module_context(args, self.work, self.root/'session', '/dev/shm/kv')['kv_storage_dir'],
                         '/dev/shm/kv')

    def load(self, value):
        path = self.root/'modules.json'
        path.write_text(json.dumps(value))
        return load_modules(path, context={'workdir':str(self.work)})

    def test_invalid_manifest_rejected_before_import(self):
        values = [{}, {'modules':{}}, {'modules':[{'factory':'bad'}]},
                  {'modules':[{'factory':'a:b','settings':[]}]},
                  {'modules':[{'factory':'a:b','extra':1}]}]
        for value in values:
            with self.subTest(value=value), patch('importlib.import_module') as importer:
                with self.assertRaises(AgentError): self.load(value)
                importer.assert_not_called()

    def test_import_failure_bad_settings_and_duplicate_names_are_explicit(self):
        for entries in [
            [{'factory':'missing_pavlusha_module:create_extension'}],
            [{'factory':'pavlusha_modules.project_map:create_extension','settings':{'unknown':1}}],
            [{'factory':'pavlusha_modules.project_state:create_extension'}]*2,
        ]:
            with self.subTest(entries=entries), self.assertRaises(AgentError):
                self.load({'modules':entries})

    def test_default_manifest_constructs_handlers_without_starting_resources(self):
        path = Path(__file__).resolve().parents[1]/'examples/qwen.native.config.json'
        modules = load_modules(path, context={'workdir':str(self.work), 'review_every':3})
        self.assertEqual([m.name for m in modules], ['shell','project_state','project_map',
            'images','drawio','processes','gui','browser','expert','office','gpu_shell',
            'reasoning_code','loop_detection','interactive'])
        self.assertEqual(modules[1].review_every,3)
        self.assertIsNone(modules[1].data)
        self.assertIsNone(modules[2].map)
        EventHost(modules)

    def test_loaded_modules_review_reset_and_restart(self):
        manifest = {'modules':[
            {'factory':'pavlusha_modules.project_state:create_extension', 'settings':{'review_every':0}},
            {'factory':'pavlusha_modules.project_map:create_extension'}]}
        (self.work/'a.py').write_text('def alpha(): pass\n')
        modules = self.load(manifest)
        provider = Provider([initialize(),shell(),review(),done(),finish()])
        runtime = Runtime(provider,task='task',session_dir=self.root/'session',
            extensions=[self.host(),*modules],history_high=2,max_steps=4)
        with self.assertRaisesRegex(AgentError, 'maximum'):
            runtime.run()
        self.assertEqual(runtime.session.state['context_epoch'],1)
        self.assertIn('EXTENSION project_map/navigation:', str(provider.requests[2]['messages']))
        self.assertIn('def alpha()',str(provider.requests[3]['messages']))
        # Fresh instances recover their own storage through session_started.
        (self.work/'a.py').write_text('def beta(): pass\n')
        resumed_provider = Provider([done(),finish()])
        resumed = Runtime(resumed_provider,task='task',session_dir=self.root/'session',
            extensions=[self.host(),*self.load(manifest)],max_steps=10)
        self.assertEqual(resumed.run(),'done')
        self.assertIn('def beta()',str(resumed_provider.requests[0]['messages']))
        self.assertEqual(self.effects,['shell'])
