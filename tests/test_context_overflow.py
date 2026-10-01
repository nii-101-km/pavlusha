"""Measured LM Studio overflow transport and strict cold-recovery regressions."""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from pavlusha_agent.core import AgentError
from pavlusha_agent.provider import ChatProvider, ProviderContextOverflow, provider_error
from pavlusha_agent.state_store import StateStore
from tests.test_runtime_lifecycle import run_script, review
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done

ENGINE = {'error': {'code': 400, 'message': 'request (180052 tokens) exceeds the available context size (117248 tokens), try increasing it',
                    'type': 'exceed_context_size_error', 'n_prompt_tokens': 180052, 'n_ctx': 117248}}
WRAPPER = 'Engine protocol predict request returned 400: ' + json.dumps(ENGINE)
HTTP_BODY = json.dumps({'error': WRAPPER})
SSE_BODY = 'event: error\ndata: ' + json.dumps({'error': {'message': WRAPPER}, 'message': WRAPPER}) + '\n\n'

class Response(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()

class OverflowTests(unittest.TestCase):
    def test_observed_http_and_sse_raise_typed_overflow(self):
        provider = ChatProvider('http://local/v1', 'model', '', 1, 0, 8000)
        error = urllib.error.HTTPError('url', 400, 'bad', {}, io.BytesIO(HTTP_BODY.encode()))
        with patch('urllib.request.urlopen', side_effect=error), self.assertRaises(ProviderContextOverflow):
            provider.worker_completion([])
        with patch('urllib.request.urlopen', return_value=Response(SSE_BODY.encode())), self.assertRaises(ProviderContextOverflow):
            provider.worker_completion([], on_delta=lambda *x: None)

    def test_narrow_classifier_and_unrelated_sse(self):
        for body, status in [('{"error":"invalid parameters"}',400), (HTTP_BODY,500),
                             ('{"error":{"type":"exceed_context_size_error"}}',400),
                             ('context length exceeded',400), ('{}',400)]:
            self.assertNotIsInstance(provider_error(body, status=status), ProviderContextOverflow)
        provider = ChatProvider('http://local/v1', 'model', '', 1, 0, 8000)
        with patch('urllib.request.urlopen', return_value=Response(b'data: {"error":{"message":"bad parameter"}}\n\n')):
            with self.assertRaises(AgentError) as caught:
                provider.worker_completion([], on_delta=lambda *x: None)
            self.assertNotIsInstance(caught.exception, ProviderContextOverflow)

    def test_overflow_restores_checkpoint_handoff_ledger_and_current_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def shell(work, command):
                (work / (command + '.py')).write_text('def current_world(): return 42\n')
            update = turn({'action':'project_update','changes':[
                {'op':'add_design','decision':'UNCOMMITTED_DESIGN', 'rationale':'temporary'}]}, reasoning='UNCOMMITTED_REASONING')
            replies = [init_turn(), done('verified'), review('COMMITTED_HANDOFF'),
                       turn({'action':'shell','command':'after_checkpoint'}), update,
                       ProviderContextOverflow('observed overflow'),
                       turn({'action':'shell','command':'verify_current'}),
                       turn({'action':'finish','summary':'recovered'})]
            seen, result, error = run_script(root, replies, extra=['--history-high','2','--project-map','on'], shell_hook=shell)
            self.assertEqual(result, 0, error)
            recovered = str(seen[6])
            self.assertIn('COMMITTED_HANDOFF', recovered)
            self.assertIn('after_checkpoint.py', recovered)
            self.assertIn('current_world', recovered)
            self.assertNotIn('UNCOMMITTED_REASONING', recovered)
            self.assertNotIn('UNCOMMITTED_DESIGN', recovered)
            self.assertTrue((root/'work/after_checkpoint.py').exists())
            state = StateStore(root/'state','task').load()
            self.assertEqual(state['counters']['operation'], 2)
            self.assertEqual([x['id'] for x in state['operations']], ['OP0001','OP0002'])
            self.assertFalse(state['project_state']['design'])
            events = [json.loads(l) for l in (root/'state/experiment.jsonl').read_text().splitlines()]
            self.assertEqual(sum(e['kind']=='context_overflow_recovery' for e in events), 1)
            archive = [json.loads(l) for l in (root/'state/history_archive.jsonl').read_text().splitlines()]
            self.assertEqual({x['step'] for x in archive}, {1,2,3})

    def test_repeated_checkpoint_overflow_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seen, result, error = run_script(root, [init_turn(), ProviderContextOverflow('first'), ProviderContextOverflow('again')])
            self.assertIsNone(result)
            self.assertEqual(len(seen), 3)
            self.assertIn('recovery exhausted', error)
            self.assertFalse((root/'state/history_archive.jsonl').exists())

    def test_missing_checkpoint_fails_strictly(self):
        with tempfile.TemporaryDirectory() as tmp:
            seen, result, error = run_script(Path(tmp), [ProviderContextOverflow('startup overflow')])
            self.assertIsNone(result)
            self.assertEqual(len(seen), 1)
            self.assertIn('no committed recovery generation', error)

    def test_other_errors_never_recover(self):
        for error in [AgentError('provider HTTP 400: invalid parameter'), AgentError('provider error: TimeoutError'),
                      AgentError('provider error: URLError connection refused')]:
            with self.subTest(error=str(error)), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                seen, result, actual = run_script(root, [init_turn(), error])
                self.assertEqual(actual, str(error))
                events = [json.loads(l) for l in (root/'state/experiment.jsonl').read_text().splitlines()]
                self.assertFalse(any(e['kind']=='context_overflow_recovery' for e in events))
