"""Schema/Core agreement and actual provider/runtime wire regressions (no live model)."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from jsonschema import Draft202012Validator

from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError, ShellResult, _extract_json_object
from pavlusha_agent.gui import GUI_ACTIONS, validate_gui_action
from pavlusha_agent.project_state import validate_project_action
from pavlusha_agent.runtime import run_agent
from pavlusha_agent.state_store import StateStore
from pavlusha_agent.sandbox import validate_action
from pavlusha_agent.worker_contract import worker_response_format
from tests.test_reasoning_loop import script, wire_response
from tests.test_reasoning_window import init_turn
from tests.test_checkpoint_snapshots import done


INIT = json.loads(init_turn().content)
DONE = json.loads(done('inspect').content)
SHELL = {'action': 'shell', 'command': 'inspect'}
FINISH = {'action': 'finish', 'summary': 'verified'}
CHANGES = [
    {'op': 'add_design', 'decision': 'existing interface', 'rationale': 'inspected'},
    {'op': 'supersede_design', 'id': 'D001', 'deviation': 'V001'},
    {'op': 'add_work', 'objective': 'verify', 'status': 'PLANNED', 'deliverables': ['result.txt']},
    {'op': 'update_work', 'id': 'W001', 'status': 'DONE', 'evidence': ['FILE: result.txt']},
    {'op': 'record_deviation', 'affects': ['W001'], 'original': 'before', 'actual': 'after', 'reason': 'observed'},
]


def validator(**options):
    schema = worker_response_format(**options)['json_schema']['schema']
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


class SchemaTests(unittest.TestCase):
    def test_all_active_action_families_validate_with_existing_core(self):
        ordinary = validator(initialized=True, gui_enabled=True, expert_enabled=True)
        variants = [SHELL, {**SHELL, 'network': True, 'gpu': True, 'release_worker': True, 'timeout': 9999}, FINISH]
        for action in variants:
            ordinary.validate(action)
            validate_action(action, 1800)
        for change in CHANGES:
            action = {'action': 'project_update', 'changes': [change]}
            ordinary.validate(action)
            validate_project_action(action)
        variants = [
            {'action': 'gui_start', 'command': 'app', 'network': False, 'timeout': 30, 'delay': None},
            {'action': 'view_gui'}, {'action': 'click', 'x': 0, 'y': 599},
            {'action': 'right_click', 'x': 799, 'y': 0},
            {'action': 'drag', 'x1': 0, 'y1': 0, 'x2': 799, 'y2': 599},
            {'action': 'type_text', 'text': ''}, {'action': 'gui_close'},
        ]
        self.assertEqual({x['action'] for x in variants}, GUI_ACTIONS)
        for action in variants:
            ordinary.validate(action)
            validate_gui_action(action, 1800)
        ordinary.validate({'action': 'ask_expert', 'question': 'what?', 'context': ''})
        validator(initialized=False).validate(INIT)
        validate_project_action(INIT)
        for action in ({'action': 'project_review_complete'},
                       {'action': 'project_review_complete', 'note': None, 'handoff': 'Next: inspect'}):
            validator(initialized=True, checkpoint_required=True).validate(action)
            validate_project_action(action)
        action = {'action': 'project_review_skip'}
        validator(initialized=True, periodic_review=True).validate(action)
        validate_project_action(action)

    def test_phases_and_feature_flags_never_broaden_allowed_actions(self):
        fixtures = [INIT, SHELL, FINISH, DONE, {'action': 'project_review_skip'},
                    {'action': 'project_review_complete'}, {'action': 'view_gui'},
                    {'action': 'ask_expert', 'question': 'what?', 'context': ''},
                    {'action': 'drop_context', 'ids': ['x'], 'intent': 'old'},
                    {'action': 'compact_context', 'items': [{'id': 'x', 'summary': 'old'}]}]
        phases = [
            ({'initialized': False}, {'project_init'}),
            ({'initialized': True}, {'shell', 'finish', 'project_update'}),
            ({'initialized': True, 'periodic_review': True}, {'project_update', 'project_review_skip'}),
            ({'initialized': True, 'checkpoint_required': True, 'periodic_review': True},
             {'project_update', 'project_review_complete'}),
            ({'initialized': True, 'gui_enabled': True, 'expert_enabled': True},
             {'shell', 'finish', 'project_update', 'view_gui', 'ask_expert'}),
            ({'initialized': True, 'periodic_review': True, 'gui_enabled': True, 'expert_enabled': True},
             {'project_update', 'project_review_skip'}),
            ({'initialized': True, 'checkpoint_required': True, 'gui_enabled': True, 'expert_enabled': True},
             {'project_update', 'project_review_complete'}),
        ]
        for options, allowed in phases:
            check = validator(**options)
            for action in fixtures:
                with self.subTest(options=options, action=action):
                    self.assertEqual(check.is_valid(action), action['action'] in allowed)

    def test_discriminators_required_fields_and_payloads_cannot_be_interchanged(self):
        check = validator(initialized=True, gui_enabled=True, expert_enabled=True)
        bad = [
            {}, {'action': 'unknown'}, {'action': 'shell', 'summary': 'done'},
            {'action': 'finish', 'command': 'inspect'}, {**SHELL, 'summary': 'done'},
            {**FINISH, 'gpu': True}, {'action': 'project_update', 'changes': []},
            {'action': 'project_update', 'changes': [{'op': 'add_design', 'objective': 'work'}]},
            {'action': 'project_update', 'changes': [{'op': 'update_work', 'id': 'W001'}]},
            {'action': 'gui_start', 'command': 'app', 'gpu': True},
            {'action': 'click', 'text': 'hello'}, {'action': 'type_text', 'x': 0, 'y': 0},
            {'action': 'ask_expert', 'question': 'what?'},
        ]
        for key in ('network', 'gpu', 'release_worker'):
            bad.extend({**SHELL, key: value} for value in (0, 1, 'true', None))
        bad.append({**SHELL, 'timeout': True})
        for action in bad:
            with self.subTest(action=action):
                self.assertFalse(check.is_valid(action))
        for change in CHANGES:
            branch = next(x for x in check.schema['anyOf'] if x['properties']['action']['const'] == 'project_update')
            operation = next(x for x in branch['properties']['changes']['items']['anyOf']
                             if x['properties']['op']['const'] == change['op'])
            for key in operation['required']:
                missing = {k: v for k, v in change.items() if k != key}
                self.assertFalse(check.is_valid({'action': 'project_update', 'changes': [missing]}))

    def test_generation_shape_does_not_replace_core_semantic_validation(self):
        action = {'action': 'project_review_complete', 'handoff': 'я' * 1100}
        validator(initialized=True, checkpoint_required=True).validate(action)
        with self.assertRaisesRegex(AgentError, 'UTF-8 bytes'):
            validate_project_action(action)
        action = {**INIT, 'work': [{**item, 'status': 'PLANNED'} for item in INIT['work']]}
        validator(initialized=False).validate(action)
        with self.assertRaisesRegex(AgentError, 'exactly one ACTIVE'):
            validate_project_action(action)


class RuntimeWireTests(unittest.TestCase):
    def run_case(self, replies, *, streaming=True, every=0, history_high=100,
                 max_steps=25, reasoning_by_step=None):
        requests, shell_calls = [], []
        replies = iter(replies)
        def send(request, **kwargs):
            payload = json.loads(request.data)
            requests.append(payload)
            self.assertEqual(payload['stream'], streaming)
            self.assertNotIn('tools', payload)
            self.assertNotIn('tool_choice', payload)
            self.assertEqual(payload['response_format']['type'], 'json_schema')
            self.assertTrue(payload['response_format']['json_schema']['strict'])
            response = next(replies)
            if isinstance(response, Exception):
                raise response
            action, finish_reason = response if isinstance(response, tuple) else (response, 'stop')
            reasoning = (reasoning_by_step or {}).get(len(requests), 'SEPARATE_REASONING')
            wire = wire_response(script(action=action, reasoning=reasoning), streaming)
            if finish_reason != 'stop':
                wire = io.BytesIO(wire.getvalue().replace(b'"stop"', json.dumps(finish_reason).encode()))
            return wire
        def shell(workdir, command, **kwargs):
            shell_calls.append(command)
            return ShellResult(command, False, 0, False, 'OK', '', 0.01)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = build_parser().parse_args([
                '--workdir', str(root/'work'), '--state-dir', str(root/'state'), '--model', 'scripted',
                '--worker-context-budget', '40000', '--max-tokens', '1024', '--max-steps', str(max_steps),
                '--project-review-every', str(every), '--history-high', str(history_high),
                '--reasoning-loop-recovery', 'observe' if streaming else 'off', 'test'])
            with patch('urllib.request.urlopen', side_effect=send), \
                 patch('pavlusha_agent.runtime.shutil.which', return_value='/fake/bwrap'), \
                 patch('pavlusha_agent.runtime.run_shell', side_effect=shell), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                try:
                    result, error = run_agent(args), None
                except AgentError as exc:
                    result, error = None, str(exc)
            state = StateStore(root/'state', 'test').load()
        return requests, shell_calls, state, result, error

    def test_high_after_30_steps_repeated_updates_keep_exit_instruction_at_request_tail(self):
        # historical checkpoint regression: periodic review on 12/13 and 24, then HIGH
        # on 31. Accepted updates on 31-37 do not unlock shell, even when
        # reasoning on 35-37 says to stop repeating updates and run shell.
        update = {'action': 'project_update', 'changes': [
            {'op': 'update_work', 'id': 'W001', 'status': 'ACTIVE',
             'evidence': ['OP0026: implementation inspected']},
        ]}
        rejected_update = {'action': 'project_update', 'changes': [
            {'op': 'update_work', 'id': 'W999', 'status': 'ACTIVE'},
        ]}
        replies = ([INIT] + [SHELL] * 10 + [rejected_update, update]
                   + [SHELL] * 10 + [update] + [SHELL] * 6
                   + [update] * 7 + [{'action': 'project_review_complete',
                                     'handoff': 'Next: run the pending shell check.'}]
                   + [SHELL, DONE, FINISH])
        reasoning = {step: 'The update is already recorded. Stop repeating it and run shell.'
                     for step in (35, 36, 37)}
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                requests, calls, state, result, error = self.run_case(
                    replies, streaming=streaming, every=10, history_high=30,
                    max_steps=41, reasoning_by_step=reasoning)
                self.assertEqual(result, 0, error)
                self.assertEqual(len(requests), 41)
                self.assertEqual(len(calls), 27)
                allowed = []
                for request in requests:
                    schema = request['response_format']['json_schema']['schema']
                    allowed.append({branch['properties']['action']['const']
                                    for branch in schema.get('anyOf', [schema])})
                self.assertEqual(allowed[29], {'shell', 'finish', 'project_update'})
                for step in (12, 13, 24):
                    self.assertEqual(allowed[step - 1], {'project_update', 'project_review_skip'})
                for step in range(31, 39):
                    request = requests[step - 1]
                    self.assertEqual(allowed[step - 1], {'project_update', 'project_review_complete'})
                    tail = request['messages'][-1]
                    self.assertEqual(tail['role'], 'user')
                    self.assertIn('CURRENT RUNTIME PHASE: HIGH CHECKPOINT', tail['content'])
                    self.assertIn('project_update does not complete', tail['content'])
                    self.assertIn('project_review_complete', tail['content'])
                    self.assertIn('shell/finish are unavailable', tail['content'])
                    self.assertEqual(sum('CURRENT RUNTIME PHASE: HIGH CHECKPOINT' in m['content']
                                         for m in request['messages']), 1)
                    if step > 31:
                        self.assertTrue(request['messages'][-2]['content'].startswith('PROJECT STATE UPDATED:'))
                for step in (35, 36, 37):
                    # The contradictory reasoning really went through the provider
                    # wire and entered History; it cannot override the HIGH schema.
                    self.assertTrue(any(reasoning[step] in m['content']
                                        for m in requests[step]['messages']))
                self.assertEqual(allowed[38], {'shell', 'finish', 'project_update'})
                self.assertFalse(any('CURRENT RUNTIME PHASE: HIGH CHECKPOINT' in m['content']
                                     for m in requests[38]['messages']))
                self.assertFalse(any('Stop repeating it and run shell.' in m['content']
                                     for m in requests[38]['messages']))
                self.assertEqual(state['recovery_checkpoint']['generation'], 2)
                self.assertEqual(state['recovery_checkpoint']['step'], 38)
                self.assertEqual(state['run']['status'], 'finished')

    def test_real_provider_wire_selects_same_protocol_before_during_after_periodic_review(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                requests, calls, state, result, error = self.run_case([
                    INIT, SHELL, {'action': 'project_review_skip'}, {**SHELL, 'gpu': True},
                    DONE, FINISH,
                ], streaming=streaming, every=1)
                self.assertEqual(result, 0, error)
                self.assertEqual(calls, ['inspect', 'inspect'])
                self.assertEqual(state['run']['status'], 'finished')
                kinds = []
                for request in requests:
                    schema = request['response_format']['json_schema']['schema']
                    branches = schema.get('anyOf', [schema])
                    kinds.append({x['properties']['action']['const'] for x in branches})
                self.assertEqual(kinds, [
                    {'project_init'}, {'shell', 'finish', 'project_update'},
                    {'project_update', 'project_review_skip'}, {'shell', 'finish', 'project_update'},
                    {'project_update', 'project_review_skip'}, {'shell', 'finish', 'project_update'},
                ])
                self.assertIn('SEPARATE_REASONING', str(requests[-1]['messages']))

    def test_malformed_unsupported_and_truncated_responses_retry_without_execution(self):
        for bad in ('{"action":"shell","command":"NEVER_EXECUTE"',
                    {'action': 'unsupported'}, {'action': 'shell', 'command': 123},
                    ({'action': 'shell', 'command': 'NEVER_EXECUTE'}, 'length')):
            with self.subTest(bad=bad):
                requests, calls, state, result, error = self.run_case([INIT, bad, bad, DONE, FINISH])
                self.assertEqual(result, 0, error)
                self.assertEqual(calls, [])
                self.assertEqual(state['counters']['operation'], 0)
                self.assertIn('INVALID ACTION:', str(requests[3]['messages']))
        for bad in ('{"action":"project_update","changes":[{"op":"add_design","decision":"x","rationale":"y"}]',
                    ({'action': 'shell', 'command': 'NEVER_EXECUTE'}, 'length')):
            requests, calls, state, result, error = self.run_case([INIT, bad, bad, bad])
            self.assertIsNone(result)
            self.assertEqual(error, 'model returned invalid actions three times in a row')
            self.assertEqual(calls, [])
            self.assertEqual(state['project_state']['design'], [])

    def test_provider_schema_rejection_stops_without_permissive_fallback(self):
        error = urllib.error.HTTPError('http://test', 400, 'schema rejected', {}, io.BytesIO(b'unsupported schema'))
        requests, calls, state, result, message = self.run_case([error])
        self.assertIsNone(result)
        self.assertIn('unsupported schema', message)
        self.assertEqual(len(requests), 1)
        self.assertEqual(calls, [])
        self.assertFalse(state['project_state']['initialized'])

    def test_completion_ceiling_blocks_partial_and_complete_json_in_both_transports(self):
        for streaming in (False, True):
            for content in ('{"action":"shell","command":"NEVER_EXECUTE"',
                            {'action': 'shell', 'command': 'NEVER_EXECUTE'}):
                with self.subTest(streaming=streaming, content=content):
                    truncated = (content, 'length')
                    requests, calls, state, result, error = self.run_case(
                        [INIT, truncated, truncated, truncated], streaming=streaming)
                    self.assertIsNone(result)
                    self.assertEqual(error, 'model returned invalid actions three times in a row')
                    self.assertEqual(calls, [])
                    self.assertEqual(state['counters']['operation'], 0)
                    self.assertIn('completion was truncated', str(requests[-1]['messages']))

    def test_parser_stays_fail_closed_for_truncated_nested_action(self):
        with self.assertRaisesRegex(AgentError, 'malformed JSON action'):
            _extract_json_object('{"action":"project_update","changes":[{"action":"shell","command":"NEVER"}]')


if __name__ == '__main__':
    unittest.main()
