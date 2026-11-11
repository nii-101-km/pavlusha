"""System instructions follow the same request snapshot as callable actions."""
import unittest
from pavlusha_agent.core import AgentError
from pavlusha_agent.extensions import Contribution, Event, EventHost, Outcome, Tool


def spec(name):
    return Tool(name, 'Description of ' + name,
                {'type': 'object', 'properties': {}, 'additionalProperties': False},
                lambda args, context: Outcome({}))


class Module:
    def __init__(self, name, contribution):
        self.name, self.contribution = name, contribution
    def on_event(self, event):
        if event.kind == 'session_started':
            return self.contribution


def start(host):
    host.emit(Event('session_started', 'session', 0, 0))


class SystemInstructionsTests(unittest.TestCase):
    def test_tool_and_its_instruction_share_snapshot_and_exclusive_filter(self):
        normal = Module('normal', Contribution(tools=(spec('work'),), system_instructions='Work policy'))
        review = Module('review', Contribution(tools=(spec('review'),), exclusive=True,
                                                system_instructions='Review policy'))
        host = EventHost([normal, review]); start(host)
        view = host.snapshot()
        system = '\n'.join(view.system_instructions)
        self.assertIn('Review policy', system)
        self.assertIn('ACTION review:', system)
        self.assertIn('Work policy', system)
        self.assertIn('ACTION work:', system)
        self.assertEqual([t.name for t in view.tools], ['review'])
        host.contributions['review'] = Contribution()
        self.assertEqual([t.name for t in view.tools], ['review'])
        self.assertIn('ACTION work:', '\n'.join(host.snapshot().system_instructions))

    def test_missing_or_invalid_module_contribution_fails_with_owner(self):
        for value in (None, Contribution(system_instructions=None),
                      Contribution(tools=(Tool('work', '', {'type': 'object', 'properties': {},
                          'additionalProperties': False}, lambda a, c: Outcome({})),))):
            with self.subTest(value=value):
                host = EventHost([Module('broken', value)])
                with self.assertRaisesRegex(AgentError, 'broken.*session_started'):
                    start(host)

    def test_empty_explicit_contribution_is_valid(self):
        host = EventHost([Module('observer', Contribution())]); start(host)
        self.assertEqual(host.snapshot().system_instructions, ())


if __name__ == '__main__':
    unittest.main()
