"""Trusted capability contracts and integration through the existing runtime gates."""
import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

from pavlusha_agent.core import AgentError
from pavlusha_agent.functions import FunctionRegistry
from pavlusha_agent.live import LiveConsoleRenderer
from pavlusha_agent.state_store import StateStore
from pavlusha_agent.worker_contract import worker_response_format
from tests.test_runtime_lifecycle import run_script, review
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done
from tests import test_interactive

SOURCE = '''from __future__ import annotations
from math import sqrt

def _private(x): return x

def multiply(a: int, b: int = 2) -> dict:
    """Multiply two integers."""
    return {"result": a * b}

def nested(items: list[dict[str, int | None]], flag: bool = False) -> list:
    return items

def failure() -> dict:
    raise RuntimeError("external service unavailable")

def invalid():
    return object()

def enormous() -> str:
    return "x" * 100000
'''


def call(name='multiply', **arguments):
    return {'action': 'call_function', 'name': name, 'arguments': arguments}


class FunctionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.module = self.root / 'capabilities.py'
        self.module.write_text(SOURCE)
        self.registry = FunctionRegistry(self.module)

    def test_loading_discovery_and_schema(self):
        self.assertEqual(set(self.registry.functions), {'multiply', 'nested', 'failure', 'invalid', 'enormous'})
        description = self.registry.descriptions[0]
        self.assertEqual(description['description'], 'Multiply two integers.')
        self.assertEqual(description['arguments']['required'], ['a'])
        self.assertEqual(description['arguments']['properties']['b'], {'type': 'integer', 'default': 2})
        schema = worker_response_format(initialized=True, functions=self.registry.descriptions)['json_schema']['schema']
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(call(a=3), schema)
        for phase in ({'initialized': False}, {'initialized': True, 'checkpoint_required': True},
                      {'initialized': True, 'periodic_review': True}, {'initialized': True}):
            shape = worker_response_format(**phase, functions=None if phase == {'initialized': True} else self.registry.descriptions)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(call(a=3), shape['json_schema']['schema'])

    def test_defaults_success_and_nested_annotations(self):
        self.assertEqual(self.registry.call(call(a=3), 1000)['result'], {'result': 6})
        self.assertEqual(self.registry.call(call(a=3, b=5), 1000)['result'], {'result': 15})
        self.assertEqual(self.registry.call(call('nested', items=[{'x': None}, {'y': 2}]), 1000)['result'],
                         [{'x': None}, {'y': 2}])

    def test_argument_errors_never_execute(self):
        for action in (call(), call(a=True), call(a=1.5), call(a='3'), call(a=2, unknown=3),
                       {**call(a=2), 'release_worker': True}, {**call(), 'arguments': []},
                       call('nested', items=[{'x': 'wrong'}]), {**call(), 'name': ['bad']},
                       {**call(), 'name': 'module.symbol'}):
            with self.subTest(action=action):
                self.assertEqual(self.registry.call(action, 1000)['error'], 'invalid_arguments')
        self.assertEqual(self.registry.call(call('sqrt'), 1000)['error'], 'unknown_function')
        self.assertEqual(self.registry.call(call('_private'), 1000)['error'], 'unknown_function')

    def test_exceptions_invalid_results_and_limits(self):
        for name, expected in [('failure', 'function_exception'), ('invalid', 'invalid_result'),
                               ('enormous', 'output_limit_exceeded')]:
            outcome = self.registry.call(call(name), 1000)
            self.assertEqual(outcome['error'], expected)
            self.assertLess(len(json.dumps(outcome)), 1300)
        for expression in ('float("nan")', 'float("inf")', '{1: "bad"}', '(1, 2)', '{"nested": object()}',
                           'iter([1])'):
            self.module.write_text('def bad():\n    return ' + expression + '\n')
            self.assertEqual(FunctionRegistry(self.module).call(call('bad'), 1000)['error'], 'invalid_result')
        self.module.write_text('def bad() -> int:\n    return "wrong"\n')
        self.assertEqual(FunctionRegistry(self.module).call(call('bad'), 1000)['error'], 'invalid_result')

    def test_fail_clearly_on_bad_modules_and_declarations(self):
        for source in ('def f(x): pass', 'def f(x: tuple[int]): pass',
                       'def f(*x: int): pass', 'def f(x: int, /): pass',
                       'def f(x: int = "bad"): pass', 'async def f(): pass',
                       'def f(): yield 1', 'async def f(): yield 1', 'def f(x: dict[int, str]): pass',
                       'def f(): pass\ng = f', 'raise RuntimeError("import failed")',
                       'x = 1', 'invalid syntax!'):
            self.module.write_text(source)
            with self.subTest(source=source), self.assertRaises(AgentError):
                FunctionRegistry(self.module)
        with self.assertRaises(AgentError):
            FunctionRegistry(self.root / 'missing.py')

    def test_history_ledger_following_shell_and_finish(self):
        seen, result, error = run_script(self.root, [init_turn(), turn(call(a=4)),
            turn(call('failure')), turn({'action': 'finish', 'summary': 'premature'}),
            turn({'action': 'shell', 'command': 'verify'}), done('verified'),
            turn({'action': 'finish', 'summary': 'done'})], extra=['--functions', str(self.module)])
        self.assertEqual(result, 0, error)
        self.assertIn('Multiply two integers', str(seen[0]))
        self.assertIn('FUNCTION RESULT (OP0001)', str(seen[2]))
        self.assertIn('"result": 8', str(seen[2]))
        self.assertIn('function_exception', str(seen[3]))
        self.assertIn('FINISH REJECTED', str(seen[4]))
        self.assertIn('SHELL RESULT (OP0003)', str(seen[5]))
        state = StateStore(self.root / 'state', 'task').load()
        self.assertEqual(state['operations'][0]['command'], 'call_function multiply')
        self.assertIn('"result": 8', state['operations'][0]['result_excerpt'])
        self.assertEqual(state['run']['status'], 'finished')
        self.assertNotIn('functions', state)

    def test_disabled_call_is_unavailable(self):
        seen, result, error = run_script(self.root, [init_turn(), turn(call(a=3)), done('verified'),
            turn({'action': 'finish', 'summary': 'done'})])
        self.assertEqual(result, 0, error)
        self.assertNotIn('User-enabled external capabilities', str(seen[0]))
        self.assertIn('INVALID ACTION', str(seen[2]))
        self.assertEqual(StateStore(self.root / 'state', 'task').load()['counters']['operation'], 0)

    def test_live_function_outcomes_do_not_use_shell_exit_status(self):
        output = io.StringIO()
        live = LiveConsoleRenderer(stream=output, color=False)
        live.function_result('OP0001', {'name': 'multiply', 'result': {'result': 42}})
        live.function_result('OP0002', {'name': 'failure', 'error': 'function_exception'})
        self.assertIn('OP0001 · returned', output.getvalue())
        self.assertIn('OP0002 · failed · function_exception', output.getvalue())
        self.assertNotIn('exit None', output.getvalue())

    def test_contract_errors_continue_and_periodic_gate_blocks_call(self):
        with patch('pavlusha_agent.provider.ChatProvider.released_worker') as release:
            seen, result, error = run_script(self.root, [init_turn(), turn(call('unknown')),
                turn(call(a=3)), turn({'action': 'project_review_skip'}),
                turn(call(a=True)), turn({'action': 'project_review_skip'}),
                turn(call(a=3)), done('verified'), turn({'action': 'finish', 'summary': 'done'})],
                extra=['--functions', str(self.module), '--project-review-every', '1'])
        self.assertEqual(result, 0, error)
        self.assertIn('unknown_function', str(seen[2]))
        self.assertIn('PERIODIC PROJECT STATE REVIEW:', str(seen[3]))
        self.assertIn('invalid_arguments', str(seen[5]))
        self.assertIn('"result": 6', str(seen[7]))
        self.assertEqual(StateStore(self.root / 'state', 'task').load()['counters']['operation'], 3)
        release.assert_not_called()

    def test_pause_before_dispatch_obeys_existing_held_proposal_boundary(self):
        helper = test_interactive.InteractiveTests()
        self.addCleanup(helper.doCleanups)
        result, seen, _, waiting, _, state, _ = helper.run_case([
            init_turn(), turn(call(a=3)), done('verified'), turn({'action': 'finish', 'summary': 'done'})],
            inference_pause=2, lines=('Do not call functions\n',), extra=['--functions', str(self.module)])
        self.assertEqual(result, 0)
        self.assertEqual(state['counters']['operation'], 0)
        self.assertEqual(waiting[0], (2, []))
        self.assertNotIn('FUNCTION RESULT', str(seen[2]))

    def test_checkpoint_gate_recovery_no_replay_and_launch_configuration(self):
        marker = self.root / 'calls'
        self.module.write_text(f'def counted() -> int:\n    with open({str(marker)!r}, "a") as f: f.write("call\\n")\n    return 7\n')
        extra = ['--functions', str(self.module), '--history-high', '2', '--max-steps', '5']
        seen, _, error = run_script(self.root, [init_turn(), turn(call('counted')), turn(call('counted')),
            review('Continue verification.'), turn({'action': 'shell', 'command': 'verify'})], extra=extra)
        self.assertIn('maximum of 5 steps', error)
        self.assertIn('PROJECT CHECKPOINT REQUIRED', str(seen[3]))
        self.assertEqual(marker.read_text(), 'call\n')
        self.assertIn('FUNCTION RESULT', (self.root / 'state/history_archive.jsonl').read_text())
        seen, result, error = run_script(self.root, [done('verified'), turn({'action': 'finish', 'summary': 'done'})])
        self.assertEqual(result, 0, error)
        self.assertIn('Continue verification.', str(seen[0]))
        self.assertNotIn('User-enabled external capabilities', str(seen[0]))
        self.assertEqual(marker.read_text(), 'call\n')

    def test_crash_after_side_effect_before_recording_has_no_automatic_replay(self):
        marker = self.root / 'effect'
        self.module.write_text(f'def effect() -> bool:\n    from pathlib import Path\n    Path({str(marker)!r}).write_text("effect")\n    return True\n')
        with patch.object(StateStore, 'record_operation', side_effect=OSError('crash before recording')):
            with self.assertRaises(OSError):
                run_script(self.root, [init_turn(), turn(call('effect'))], extra=['--functions', str(self.module)])
        self.assertTrue(marker.exists())
        self.assertEqual(StateStore(self.root / 'state', 'task').load()['counters']['operation'], 0)
        seen, result, error = run_script(self.root, [done('verified'), turn({'action': 'finish', 'summary': 'done'})],
                                         extra=['--functions', str(self.module)])
        self.assertEqual(result, 0, error)
        self.assertNotIn('FUNCTION RESULT', str(seen[0][1:]))

    def test_pause_during_real_function_records_before_user_input(self):
        self.module.write_text('def pause() -> dict:\n    import signal\n    signal.raise_signal(signal.SIGTSTP)\n    return {"completed": True}\n')
        helper = test_interactive.InteractiveTests()
        self.addCleanup(helper.doCleanups)
        def read_hook(root, seen, commands):
            state = StateStore(root / 'state', 'task').load()
            self.assertEqual(state['counters']['operation'], 1)
            self.assertIn('completed', state['operations'][0]['result_excerpt'])
            self.assertEqual(len(seen), 2)
        with patch('pavlusha_agent.interactive.os.write'):
            result, seen, commands, waiting, _, _, _ = helper.run_case([
                init_turn(), turn(call('pause')), done('verified'), turn({'action': 'finish', 'summary': 'done'})],
                lines=('new constraint\n',), extra=['--functions', str(self.module)], read_hook=read_hook)
        self.assertEqual(result, 0)
        self.assertEqual(waiting[0], (2, []))
        content = [m['content'] for m in seen[2]]
        self.assertLess(next(i for i, m in enumerate(content) if m.startswith('FUNCTION RESULT')),
                        next(i for i, m in enumerate(content) if m.startswith('USER MESSAGE AT')))
        self.assertEqual(commands, [])


if __name__ == '__main__':
    unittest.main()
