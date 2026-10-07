"""CLI-to-wire Worker effort passthrough, without local capability policy."""
import copy
import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError, ShellResult
from pavlusha_agent.runtime import run_agent
from tests import test_worker_release
from tests.test_checkpoint_snapshots import done
from tests.test_reasoning_window import init_turn, turn


def response(reply, streaming):
    if streaming:
        chunk = {'choices':[{'delta':{'content':reply.content}, 'finish_reason':'stop'}],
                 'usage':{'prompt_tokens':1, 'completion_tokens':1}}
        return io.BytesIO(('data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n').encode())
    return io.BytesIO(json.dumps({'choices':[{'message':{'content':reply.content},
                                             'finish_reason':'stop'}],
                                 'usage':{'prompt_tokens':1, 'completion_tokens':1}}).encode())


class ReasoningEffortTests(unittest.TestCase):
    def run_wire(self, *, effort=None, streaming=False, release=False, rejection=False):
        requests, commands = [], []
        lifecycle = []
        replies = [init_turn(), done('verified')]
        if release:
            replies.append(turn({'action':'shell','command':'verify after release','release_worker':True}))
        replies.append(turn({'action':'finish','summary':'Done'}))
        replies = iter(replies)
        config = {'context_length':40000, 'reasoning_budget_message':'existing model setting'}
        provider, lifecycle, _ = test_worker_release.ReleaseProviderTests().provider(config=config)
        def endpoint(request, timeout):
            payload = json.loads(request.data)
            requests.append(copy.deepcopy(payload))
            if rejection:
                raise urllib.error.HTTPError(request.full_url, 400, 'Bad Request', {},
                    io.BytesIO(b'{"error":"unsupported reasoning_effort from provider"}'))
            return response(next(replies), payload['stream'])
        def shell(workdir, command, **kwargs):
            commands.append(command)
            return ShellResult(command, False, 0, False, 'OK', '', .01)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            extra = ['--reasoning-effort', effort] if effort is not None else []
            # Exercise both transports independently of default loop recovery,
            # which requests streaming even without --live.
            extra.extend(['--reasoning-loop-recovery', 'off'])
            if streaming:
                extra.append('--live')
            args = build_parser().parse_args(['--no-interactive', '--no-live', '--no-network', '--project-map', 'off',
                '--workdir', str(root/'work'), '--state-dir', str(root/'state'),
                '--model', 'scripted', '--worker-context-budget', '40000',
                '--max-steps','4','--project-review-every','0', *extra, 'task'])
            error = None
            with patch('pavlusha_agent.runtime.shutil.which', return_value='/fake/bwrap'), \
                 patch('urllib.request.urlopen', endpoint), \
                 patch('pavlusha_agent.runtime.run_shell', shell), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                if release:
                    with patch('pavlusha_agent.runtime.ChatProvider', return_value=provider):
                        result = run_agent(args)
                else:
                    try:
                        result = run_agent(args)
                    except AgentError as exc:
                        result, error = None, str(exc)
        return result, error, requests, commands, lifecycle

    def test_omission_preserves_default_in_both_transports(self):
        self.assertIsNone(build_parser().parse_args(['task']).reasoning_effort)
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                result, error, requests, commands, _ = self.run_wire(streaming=streaming)
                self.assertEqual(result, 0, error)
                self.assertEqual(len(requests), 3)
                self.assertTrue(all('reasoning_effort' not in r for r in requests))
                self.assertEqual(commands, [])

    def test_arbitrary_cli_string_and_raw_reasoning_noop_unchanged(self):
        for text in ('provider-new-value', 'vendor:custom effort/β ', ''):
            with self.subTest(text=text):
                args = build_parser().parse_args(['--reasoning-effort',text,
                                                  '--raw-reasoning-limit','20000','task'])
                self.assertEqual(args.reasoning_effort, text)
                self.assertEqual(args.raw_reasoning_limit, 20000)

    def test_cli_effort_reaches_each_actual_request_unchanged(self):
        for streaming in (False, True):
            for text in ('vendor:custom effort/β ', ''):
                with self.subTest(streaming=streaming, text=text):
                    result, error, requests, _, _ = self.run_wire(effort=text, streaming=streaming)
                    self.assertEqual(result, 0, error)
                    self.assertEqual([r['reasoning_effort'] for r in requests], [text]*3)
                    self.assertTrue(all(r['stream'] == streaming for r in requests))

    def test_provider_rejection_stops_without_fallback(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                result, error, requests, commands, _ = self.run_wire(
                    effort='unrecognized-by-provider', streaming=streaming, rejection=True)
                self.assertIsNone(result)
                self.assertIn('provider HTTP 400', error)
                self.assertIn('unsupported reasoning_effort from provider', error)
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]['reasoning_effort'], 'unrecognized-by-provider')
                self.assertEqual(commands, [])

    def test_request_effort_survives_release_and_model_instance_change(self):
        for streaming in (False, True):
            for effort in (None, 'provider-specific-value'):
                with self.subTest(streaming=streaming, effort=effort):
                    result, error, requests, commands, lifecycle = self.run_wire(
                        effort=effort, streaming=streaming, release=True)
                    self.assertEqual(result, 0, error)
                    self.assertEqual(len(requests), 4)
                    if effort is None:
                        self.assertTrue(all('reasoning_effort' not in r for r in requests))
                    else:
                        self.assertEqual([r['reasoning_effort'] for r in requests], [effort]*4)
                    self.assertEqual(requests[0]['model'], 'worker-instance')
                    self.assertEqual(requests[-1]['model'], 'new-worker-instance')
                    self.assertEqual(commands, ['verify after release'])
                    load = next(payload for path, payload in lifecycle if path == '/load')
                    self.assertEqual(load['reasoning_budget_message'], 'existing model setting')
                    self.assertNotIn('reasoning_effort', load)  # This knob is per-request, not model load.
