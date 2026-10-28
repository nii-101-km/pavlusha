"""Map integration with real event runtime and deterministic provider."""
from pathlib import Path
import tempfile
import unittest

from pavlusha_agent.core import AgentError
from pavlusha_agent.model_backend import ProviderContextOverflow
from pavlusha_agent.runtime import Runtime
from pavlusha_modules.project_map import ProjectMap
from tests.test_project_state_module import StateFixture, initialize, shell, done, review, finish


class ProjectMapModuleTests(StateFixture, unittest.TestCase):
    def composed(self, replies, **options):
        runtime = self.runtime(replies, **options)
        # Build a fresh runtime host with the same State/Shell and an independent Map.
        self.map = ProjectMap(self.work)
        return Runtime(runtime.provider, task='task', session_dir=self.root/'session',
            extensions=[self.host(), self.module, self.map], max_steps=15, **options)

    def maps(self, runtime):
        return [[m['content'] for m in req['messages']
                 if m['content'].startswith('EXTENSION project_map/navigation:')]
                for req in runtime.provider.requests]

    def test_snapshot_frozen_then_refreshed_after_review_and_reset(self):
        source = self.work/'old.py'
        source.write_text('def old(): pass\n')
        runtime = self.composed([initialize(), shell(), review(), done(), finish()], history_high=2)
        original = runtime.provider.worker_completion
        def completion(*args, **kwargs):
            if len(runtime.provider.requests) == 2:
                source.unlink()
                (self.work/'new.py').write_text('def new(): pass\n')
            return original(*args, **kwargs)
        runtime.provider.worker_completion = completion
        runtime.run()
        maps = self.maps(runtime)
        self.assertIn('old.py', str(maps[0]))
        self.assertIn('old.py', str(maps[1]))
        self.assertEqual(maps[1], maps[2])  # review retains the frozen prefix
        self.assertIn('new.py', str(maps[3]))
        self.assertNotIn('old.py', str(maps[3]))
        self.assertTrue((self.root/'session/modules/project_map/map.json').exists())

    def test_restart_and_overflow_use_current_files(self):
        source = self.work/'a.py'
        source.write_text('def before(): pass\n')
        first = self.composed([initialize(), shell()])
        first.max_steps = 2
        with self.assertRaises(AgentError): first.run()
        source.write_text('def after(): pass\n')
        second = self.composed([shell(), ProviderContextOverflow('full'), done(), finish()])
        second.run()
        maps = self.maps(second)
        self.assertTrue(all('def after()' in str(m) for m in maps))
        self.assertEqual(self.effects, ['shell', 'shell'])

    def test_ordinary_turns_keep_snapshot_until_epoch_changes(self):
        from pavlusha_agent.extensions import Event
        source = self.work/'a.py'
        source.write_text('def before(): pass\n')
        module = ProjectMap(self.work)
        event = lambda kind, epoch=0: Event(kind, 's', 1, epoch,
            {'session_dir': str(self.root/'session')})
        first = module.on_event(event('session_started'))
        source.write_text('def after(): pass\n')
        self.assertIsNone(module.on_event(event('turn_completed')))
        self.assertIsNone(module.on_event(event('prepare_turn')))
        self.assertIn('def before()', first.prefix['navigation'])
        refreshed = module.on_event(event('prepare_turn', 1))
        self.assertIn('def after()', refreshed.prefix['navigation'])
