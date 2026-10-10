"""Ordered, all-or-nothing startup for repeatable custom-function modules."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema

from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError
from pavlusha_agent.functions import FunctionRegistry
from pavlusha_agent.state_store import StateStore
from pavlusha_agent.worker_contract import worker_response_format
from tests.test_checkpoint_snapshots import done
from tests.test_functions import call
from tests.test_reasoning_window import init_turn, turn
from tests.test_runtime_lifecycle import run_script, review


class ModuleFunctionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.first = self.root / 'first.py'
        self.second = self.root / 'second.py'
        self.first.write_text('def add(a: int, b: int) -> int:\n    return a + b\n')
        self.second.write_text('def double(value: int) -> dict:\n'
                               '    if value < 0: raise ValueError("negative value")\n'
                               '    return {"doubled": value * 2}\n')
        self.extra = ['--functions', str(self.first), '--functions', str(self.second)]

    def test_cli_zero_one_two_module_paths(self):
        parser = build_parser()
        self.assertIsNone(parser.parse_args([]).functions)
        self.assertEqual(parser.parse_args(self.extra[:2]).functions, [str(self.first)])
        self.assertEqual(parser.parse_args(self.extra).functions, [str(self.first), str(self.second)])
        for paths in ([self.first], [str(self.first)]):
            self.assertEqual(FunctionRegistry(paths).call(call('add', a=2, b=3), 1000)['result'], 5)

    def test_both_functions_schema_and_launch_order(self):
        marker = self.root / 'imports'
        for path, label in ((self.first, 'first'), (self.second, 'second')):
            path.write_text(f'with open({str(marker)!r}, "a") as log: log.write({label!r})\n' + path.read_text())
        for paths, order, expected_log in (([self.first, self.second], ['add', 'double'], 'firstsecond'),
                                          ([self.second, self.first], ['double', 'add'], 'secondfirst')):
            marker.write_text('')
            registry = FunctionRegistry(paths)
            self.assertEqual(marker.read_text(), expected_log)
            self.assertEqual(list(registry.functions), order)
            self.assertEqual([d['name'] for d in registry.descriptions], order)
            self.assertEqual([d['name'] for d in json.loads(registry.prompt().split('\n')[-1])], order)
            schema = worker_response_format(initialized=True, functions=registry.descriptions)['json_schema']['schema']
            names = [a['properties']['name']['const'] for a in schema['anyOf']
                     if a['properties']['action']['const'] == 'call_function']
            self.assertEqual(names, order)
            for action in (call('add', a=2, b=3), call('double', value=5)):
                jsonschema.validate(action, schema)
            self.assertEqual(registry.call(call('add', a=2, b=3), 1000)['result'], 5)
            self.assertEqual(registry.call(call('double', value=5), 1000)['result'], {'doubled': 10})

    def test_duplicate_names_fail_in_both_orders_before_worker(self):
        self.second.write_text('def add(a: int, b: int) -> int:\n    return a - b\n')
        for paths in ([self.first, self.second], [self.second, self.first]):
            before = set(sys.modules)
            with self.assertRaises(AgentError) as caught:
                FunctionRegistry(paths)
            message = str(caught.exception)
            self.assertIn("Duplicate function name 'add'", message)
            for path in paths:
                self.assertIn(str(path), message)
            self.assertEqual({n for n in set(sys.modules) - before if n.startswith('_pavlusha_user_functions_')}, set())
            with patch('pavlusha_agent.runtime.ChatProvider') as provider:
                _, result, error = run_script(self.root, [], extra=[x for p in paths for x in ('--functions', str(p))])
            self.assertIsNone(result)
            self.assertIn("Duplicate function name 'add'", error)
            provider.assert_not_called()
            self.assertFalse((self.root / 'state').exists())

    def test_invalid_first_or_second_prevents_entire_startup(self):
        invalid = self.root / 'invalid.py'
        invalid.write_text('def invalid(value: tuple[int]) -> dict:\n    return {}\n')
        for paths in ([invalid, self.first], [self.first, invalid]):
            before = set(sys.modules)
            with patch('pavlusha_agent.runtime.ChatProvider') as provider:
                _, result, error = run_script(self.root, [], extra=[x for p in paths for x in ('--functions', str(p))])
            self.assertIsNone(result)
            self.assertIn(str(invalid), error)
            self.assertIn('unsupported annotation', error)
            provider.assert_not_called()
            self.assertFalse((self.root / 'state').exists())
            self.assertEqual({n for n in set(sys.modules) - before if n.startswith('_pavlusha_user_functions_')}, set())

    def test_two_modules_results_errors_shell_and_finish_share_existing_flow(self):
        with patch('pavlusha_agent.provider.ChatProvider.released_worker') as release:
            seen, result, error = run_script(self.root, [init_turn(), turn(call('add', a=2, b=3)),
                turn(call('double', value=5)), turn(call('double', value=-1)),
                turn(call('add', a=True, b=3)), turn({'action': 'finish', 'summary': 'premature'}),
                turn({'action': 'shell', 'command': 'verify'}), done('verified'),
                turn({'action': 'finish', 'summary': 'done'})], extra=self.extra)
        self.assertEqual(result, 0, error)
        self.assertIn('"name": "add"', str(seen[0]))
        self.assertIn('"name": "double"', str(seen[0]))
        self.assertIn('"result": 5', str(seen[2]))
        self.assertIn('"doubled": 10', str(seen[3]))
        self.assertIn('function_exception', str(seen[4]))
        self.assertIn('invalid_arguments', str(seen[5]))
        self.assertIn('FINISH REJECTED', str(seen[6]))
        self.assertIn('SHELL RESULT (OP0005)', str(seen[7]))
        state = StateStore(self.root / 'state', 'task').load()
        self.assertEqual([o['command'] for o in state['operations']],
                         ['call_function add', 'call_function double', 'call_function double', 'call_function add', 'verify'])
        self.assertEqual(state['run']['status'], 'finished')
        release.assert_not_called()

    def test_checkpoint_and_restart_reload_configuration_without_replay(self):
        marker = self.root / 'calls'
        for path, name in ((self.first, 'add'), (self.second, 'double')):
            source = path.read_text()
            source = source.replace('    return', f'    with open({str(marker)!r}, "a") as log: log.write({name!r})\n    return')
            path.write_text(source)
        seen, _, error = run_script(self.root, [init_turn(), turn(call('add', a=2, b=3)),
            turn(call('double', value=5)), review('Both calls observed; finish verification.')],
            extra=[*self.extra, '--history-high', '3', '--max-steps', '4'])
        self.assertIn('maximum of 4 steps', error)
        self.assertEqual(marker.read_text(), 'adddouble')
        self.assertIn('PROJECT CHECKPOINT REQUIRED', str(seen[3]))
        archive = (self.root / 'state/history_archive.jsonl').read_text()
        self.assertIn('FUNCTION RESULT (OP0001)', archive)
        self.assertIn('FUNCTION RESULT (OP0002)', archive)
        seen, result, error = run_script(self.root, [done('verified'), turn({'action': 'finish', 'summary': 'done'})],
                                         extra=self.extra)
        self.assertEqual(result, 0, error)
        self.assertIn('Both calls observed', str(seen[0]))
        self.assertIn('"name": "add"', str(seen[0][0]))
        self.assertIn('"name": "double"', str(seen[0][0]))
        self.assertNotIn('FUNCTION RESULT', str(seen[0][1:]))
        self.assertEqual(marker.read_text(), 'adddouble')
        self.assertNotIn('functions', StateStore(self.root / 'state', 'task').load())


if __name__ == '__main__':
    unittest.main()
