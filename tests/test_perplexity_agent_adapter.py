import io
import json
import unittest
from unittest.mock import patch

from pavlusha_agent.adapters import ChatProvider
from pavlusha_agent.core import AgentError
from pavlusha_agent.model_backend import ProviderUnsupported, WorkerStreamInterrupted


class PerplexityAgentAdapterTests(unittest.TestCase):
    def setUp(self):
        self.provider = ChatProvider('https://api.perplexity.ai/v1', 'openai/gpt-6.1-terra',
                                     'secret', 30, .1, 256, backend='perplexity-agent')
        self.messages = [{'role': 'system', 'content': 'rules'},
                         {'role': 'user', 'content': 'task'}]
        self.response = {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant',
            'status': 'completed', 'content': [{'type': 'output_text', 'text': '{"action":"finish"}'}]}],
            'usage': {'input_tokens': 100, 'output_tokens': 8, 'input_tokens_details': {'cached_tokens': 50,
                'cache_creation_input_tokens': 40, 'cache_read_input_tokens': 50},
                'cost': {'total_cost': 0.002, 'currency': 'USD'}}}

    def test_request_and_response(self):
        schema = {'type': 'json_schema', 'json_schema': {'name': 'action', 'schema': {'type': 'object'}}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(self.response).encode())) as request:
            result = self.provider.worker_completion(self.messages, response_format=schema, reasoning_effort='low')
        sent = request.call_args.args[0]
        body = json.loads(sent.data)
        self.assertEqual(sent.full_url, 'https://api.perplexity.ai/v1/agent')
        self.assertEqual(body['model'], 'openai/gpt-6.1-terra')
        self.assertEqual(body['instructions'], 'rules')
        self.assertEqual(body['prompt_cache_key'], self.provider.prompt_cache_key)
        self.assertEqual(body['input'], [{'type': 'message', 'role': 'user', 'content': 'task'}])
        self.assertEqual(body['response_format'], {'type': 'json_schema', 'json_schema':
                                                   {'name': 'workeraction', 'schema': schema['json_schema']['schema']}})
        self.assertEqual((result.content, result.prompt_tokens, result.completion_tokens),
                         ('{"action":"finish"}', 100, 8))
        self.assertEqual(self.provider.last_usage['cached_tokens'], 50)
        self.assertEqual(self.provider.last_usage['cache_creation_input_tokens'], 40)
        self.assertEqual(self.provider.last_usage['total_cost'], 0.002)

    def test_incomplete_response_is_not_an_action(self):
        self.response['status'] = 'incomplete'
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(self.response).encode())):
            with self.assertRaises(AgentError):
                self.provider.worker_completion(self.messages)

    def test_unsupported_observation_is_not_silently_discarded(self):
        with self.assertRaises(ProviderUnsupported):
            self.provider.worker_completion([{'role': 'user', 'content': [{'type': 'image_url'}]}])

    def test_gui_image_observation_is_translated(self):
        messages = [{'role':'user','content':[{'type':'text','text':'Screenshot'},
            {'type':'image_url','image_url':{'url':'data:image/png;base64,YQ=='}}]}]
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(self.response).encode())) as request:
            self.provider.worker_completion(messages)
        parts = json.loads(request.call_args.args[0].data)['input'][0]['content']
        self.assertEqual(parts, [{'type':'input_text','text':'Screenshot'},
                                 {'type':'input_image','image_url':'data:image/png;base64,YQ=='}])

    def test_cancelled_result_is_never_returned(self):
        stream = io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n')
        with patch('urllib.request.urlopen', return_value=stream):
            with self.assertRaises(WorkerStreamInterrupted):
                self.provider.worker_completion(self.messages, on_delta=lambda *_: False)
        self.assertTrue(stream.closed)

    def test_streaming_emits_deltas_then_returns_completed_action_and_usage(self):
        events = [ {'type': 'response.output_text.delta', 'delta': '{"action":'},
                   {'type': 'response.output_text.delta', 'delta': '"finish"}'},
                   {'type': 'response.completed', 'response': self.response} ]
        data = ''.join('event: ignored\ndata: '+json.dumps(event)+'\n\n' for event in events).encode()
        chunks = []
        with patch('urllib.request.urlopen', return_value=io.BytesIO(data)) as request:
            result = self.provider.worker_completion(self.messages, on_delta=lambda kind, part: chunks.append((kind, part)))
        self.assertTrue(json.loads(request.call_args.args[0].data)['stream'])
        self.assertEqual(''.join(part for _, part in chunks), '{"action":"finish"}')
        self.assertEqual(result.content, '{"action":"finish"}')
        self.assertEqual(result.prompt_tokens, 100)
        self.assertEqual(self.provider.last_usage['cached_tokens'], 50)

    def test_stream_eof_without_completion_is_rejected(self):
        data = b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
        with patch('urllib.request.urlopen', return_value=io.BytesIO(data)):
            with self.assertRaisesRegex(AgentError, 'before completion'):
                self.provider.worker_completion(self.messages, on_delta=lambda *_: None)

    def test_stream_server_failure_does_not_return_partial_action(self):
        data = b'data: {"type":"response.failed"}\n\n'
        with patch('urllib.request.urlopen', return_value=io.BytesIO(data)):
            with self.assertRaisesRegex(AgentError, 'did not complete'):
                self.provider.worker_completion(self.messages, on_delta=lambda *_: None)

    def test_http_error_does_not_expose_api_key(self):
        import urllib.error
        error = urllib.error.HTTPError('https://example.invalid', 401, 'secret', {}, None)
        with patch('urllib.request.urlopen', side_effect=error):
            with self.assertRaises(AgentError) as raised:
                self.provider.worker_completion(self.messages)
        self.assertNotIn('secret', str(raised.exception))

    def test_action_const_converts_to_string_enum(self):
        schema = {'type': 'json_schema', 'json_schema': {'schema':
            {'type': 'object', 'properties': {'action': {'const': 'finish'}}}}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(self.response).encode())) as request:
            self.provider.worker_completion(self.messages, response_format=schema)
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(sent['response_format']['json_schema']['schema']['properties']['action'],
                         {'type': 'string', 'enum': ['finish']})

    def test_multiple_actions_have_object_schema(self):
        branch = lambda name: {'type': 'object', 'properties': {'action': {'const': name},
            'summary': {'type': 'string'}}, 'required': ['action', 'summary']}
        schema = {'type': 'json_schema', 'json_schema': {'schema': {'anyOf': [branch('finish'), branch('message')]}}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(self.response).encode())) as request:
            self.provider.worker_completion(self.messages, response_format=schema)
        sent = json.loads(request.call_args.args[0].data)['response_format']['json_schema']['schema']
        self.assertEqual(sent['properties']['action']['enum'], ['finish', 'message'])
        self.assertEqual(sent['required'], ['action'])

    def test_cache_key_stable_in_session_and_separate_between_adapters(self):
        other = ChatProvider('https://api.perplexity.ai/v1', 'openai/gpt-6.1-terra',
                             'secret', 30, .1, 256, backend='perplexity-agent')
        self.assertNotEqual(self.provider.prompt_cache_key, other.prompt_cache_key)
        with patch('urllib.request.urlopen', side_effect=lambda *a, **kw: io.BytesIO(json.dumps(self.response).encode())) as request:
            self.provider.worker_completion(self.messages)
            self.provider.worker_completion(self.messages)
        keys = [json.loads(call.args[0].data)['prompt_cache_key'] for call in request.call_args_list]
        self.assertEqual(keys, [self.provider.prompt_cache_key] * 2)

    def test_schema_explains_fields_for_each_action(self):
        branches = [
            {'type': 'object', 'properties': {'action': {'const': 'delegate_agent'},
                'task': {'type': 'string'}, 'inputs': {'type': 'array', 'items': {'type': 'string'}}},
             'required': ['action', 'task', 'inputs']},
            {'type': 'object', 'properties': {'action': {'const': 'collect_agent'},
                'child_id': {'type': 'string'}}, 'required': ['action', 'child_id']},
        ]
        schema = {'type': 'json_schema', 'json_schema': {'schema': {'anyOf': branches}}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(self.response).encode())) as request:
            self.provider.worker_completion(self.messages, response_format=schema)
        sent = json.loads(request.call_args.args[0].data)['response_format']['json_schema']['schema']
        description = sent['properties']['action']['description']
        self.assertIn('delegate_agent: required fields: task, inputs; allowed fields: task, inputs', description)
        self.assertIn('array fields inputs use [] when empty, never null', description)
        self.assertIn('collect_agent: required fields: child_id; allowed fields: child_id', description)

    def test_only_null_fields_from_other_actions_are_removed(self):
        branches = [{'type': 'object', 'properties': {'action': {'const': 'finish'}, 'summary': {'type': 'string'}}},
                    {'type': 'object', 'properties': {'action': {'const': 'shell'}, 'command': {'type': 'string'}}}]
        self.response['output'][0]['content'][0]['text'] = '{"action":"finish","summary":"ok","command":null,"unexpected":42}'
        schema = {'type': 'json_schema', 'json_schema': {'schema': {'anyOf': branches}}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(self.response).encode())):
            result = self.provider.worker_completion(self.messages, response_format=schema)
        self.assertEqual(json.loads(result.content), {'action': 'finish', 'summary': 'ok', 'unexpected': 42})

    def test_optional_null_omitted_but_required_and_unknown_null_kept(self):
        branches = [{'type':'object','properties':{'action':{'const':'gui_start'},
            'command':{'type':'string'},'delay':{'type':'number'}},'required':['action','command']},
            {'type':'object','properties':{'action':{'const':'finish'},'summary':{'type':'string'}},
             'required':['action','summary']}]
        self.response['output'][0]['content'][0]['text'] = '{"action":"gui_start","command":null,"delay":null,"summary":null,"unknown":null}'
        schema = {'type':'json_schema','json_schema':{'schema':{'anyOf':branches}}}
        with patch('urllib.request.urlopen', return_value=io.BytesIO(json.dumps(self.response).encode())):
            result = self.provider.worker_completion(self.messages, response_format=schema)
        self.assertEqual(json.loads(result.content), {'action':'gui_start','command':None,'unknown':None})
