"""Perplexity Agent API adapter for the existing model backend interface."""
import json
import urllib.error
import urllib.request
import uuid
from jsonschema import Draft202012Validator

from ..core import AgentError, ProviderTurn
from ..model_backend import ProviderUnsupported, WorkerStreamInterrupted
from ._chat import ChatProvider


def _schema_for_perplexity(value):
    if isinstance(value, list):
        return [_schema_for_perplexity(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _schema_for_perplexity(item) for key, item in value.items() if key != 'const'}
    if 'const' in value:
        constant = value['const']
        types = {str: 'string', bool: 'boolean', int: 'integer', float: 'number', type(None): 'null'}
        if type(constant) not in types:
            raise ProviderUnsupported('Perplexity adapter does not support structured constants')
        result['type'] = types[type(constant)]
        result['enum'] = [constant]
    return result


def _response_schema(schema):
    branches = schema.get('anyOf')
    if not branches:
        return _schema_for_perplexity(schema)
    properties = {}
    actions = []
    action_rules = []
    for branch in branches:
        fields = branch.get('properties', {})
        action = fields.get('action', {}).get('const')
        if not isinstance(action, str):
            raise ProviderUnsupported('unsupported action schema for Perplexity')
        actions.append(action)
        required = [name for name in branch.get('required', []) if name != 'action']
        allowed = [name for name in fields if name != 'action']
        arrays = [name for name in required if fields[name].get('type') == 'array']
        rule = (f"{action}: required fields: {', '.join(required) or 'none'}; "
                f"allowed fields: {', '.join(allowed) or 'none'}")
        if arrays:
            rule += f"; array fields {', '.join(arrays)} use [] when empty, never null"
        action_rules.append(rule)
        for name, definition in fields.items():
            if name == 'action':
                continue
            candidate = _schema_for_perplexity(definition)
            if name in properties and properties[name] != candidate:
                raise ProviderUnsupported('conflicting action field schemas for Perplexity: ' + name)
            properties[name] = candidate
    properties['action'] = {'type': 'string', 'enum': actions,
                            'description': 'Choose exactly one action and include only its allowed fields. '
                                           'Required fields must have valid non-null values. ' +
                                           '; '.join(action_rules)}
    # The kernel validates the returned object against the original per-action
    # schema, including required fields, before invoking any action.
    return {'type': 'object', 'properties': properties, 'required': ['action'],
            'additionalProperties': False}


class PerplexityAgentAdapter(ChatProvider):
    backend_name = 'perplexity-agent'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # One key per adapter/session, never derived from the task or credentials.
        self.prompt_cache_key = 'pavlusha-' + uuid.uuid4().hex

    def _read_stream(self, response, on_delta, should_cancel):
        fragments = []
        lines = []
        completed = None

        def event(data):
            nonlocal completed
            if data == '[DONE]':
                return
            item = json.loads(data)
            if not isinstance(item, dict):
                raise ValueError('invalid stream event')
            kind = item.get('type')
            if kind == 'response.output_text.delta':
                delta = item.get('delta')
                if not isinstance(delta, str):
                    raise ValueError('invalid output delta')
                fragments.append(delta)
                if on_delta and on_delta('content', delta) is False:
                    raise WorkerStreamInterrupted(ProviderTurn(''.join(fragments), '', None))
            elif kind == 'response.completed':
                completed = item.get('response')
            elif kind in ('response.failed', 'response.incomplete', 'error'):
                response_info = item.get('response') if isinstance(item.get('response'), dict) else {}
                error_info = item.get('error') or response_info.get('error') or {}
                incomplete = response_info.get('incomplete_details') or item.get('incomplete_details') or {}
                details = []
                for value in (kind, response_info.get('status'),
                              error_info.get('code') if isinstance(error_info, dict) else None,
                              incomplete.get('reason') if isinstance(incomplete, dict) else None):
                    if isinstance(value, str) and len(value) <= 80 and all(c.isascii() and (c.isalnum() or c in '._-') for c in value):
                        details.append(value)
                raise AgentError('Perplexity stream did not complete successfully' +
                                 (': ' + ', '.join(details) if details else ''))

        for raw in response:
            if should_cancel and should_cancel():
                raise WorkerStreamInterrupted(ProviderTurn(''.join(fragments), '', None))
            line = raw.decode('utf-8', 'strict').rstrip('\r\n')
            if not line:
                if lines:
                    event('\n'.join(lines)); lines = []
                continue
            if line.startswith('data:'):
                lines.append(line[5:].lstrip(' '))
        if lines:
            event('\n'.join(lines))
        if not isinstance(completed, dict):
            raise AgentError('Perplexity stream ended before completion; response discarded')
        return completed

    def worker_completion(self, messages, *, on_delta=None, should_cancel=None,
                          response_format=None, reasoning_effort=None, **options):
        if options:
            raise AgentError('unsupported Perplexity request options: ' + ', '.join(sorted(options)))
        if should_cancel and should_cancel():
            raise WorkerStreamInterrupted(ProviderTurn('', '', None))
        if not self.api_key:
            raise AgentError('Perplexity API key is missing')
        instructions = []
        items = []
        for message in messages:
            role, content = message.get('role'), message.get('content')
            if role not in ('system', 'user', 'assistant'):
                raise ProviderUnsupported('unsupported message role')
            if isinstance(content, list) and role == 'user':
                parts = []
                for part in content:
                    if isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str):
                        parts.append({'type': 'input_text', 'text': part['text']})
                    elif isinstance(part, dict) and part.get('type') == 'image_url' and isinstance(part.get('image_url'), dict):
                        url = part['image_url'].get('url')
                        if not isinstance(url, str) or not url.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,')):
                            raise ProviderUnsupported('only embedded PNG or JPEG observations are supported')
                        parts.append({'type': 'input_image', 'image_url': url})
                    else:
                        raise ProviderUnsupported('unsupported observation content')
                content = parts
            elif not isinstance(content, str):
                raise ProviderUnsupported('unsupported message content')
            if role == 'system':
                if items:
                    raise AgentError('system instructions must precede conversation messages')
                instructions.append(content)
            else:
                items.append({'type': 'message', 'role': role, 'content': content})
        streaming = on_delta is not None or should_cancel is not None
        payload = {'model': self.resolve_model(), 'instructions': '\n\n'.join(instructions),
                   'input': items, 'max_output_tokens': self.max_tokens, 'stream': streaming,
                   'prompt_cache_key': self.prompt_cache_key}
        if response_format is not None:
            fmt = response_format.get('json_schema', {})
            if response_format.get('type') != 'json_schema' or not isinstance(fmt.get('schema'), dict):
                raise ProviderUnsupported('Perplexity adapter requires a JSON schema response format')
            payload['response_format'] = {'type': 'json_schema', 'json_schema':
                                          {'name': 'workeraction', 'schema': _response_schema(fmt['schema'])}}
        if reasoning_effort:
            payload['reasoning'] = {'effort': reasoning_effort}
        request = urllib.request.Request(self.base_url + '/agent',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers=self._headers(), method='POST')
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = self._read_stream(response, on_delta, should_cancel) if streaming else json.load(response)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise AgentError(f'Perplexity HTTP error {status}') from None
        except (OSError, ValueError) as exc:
            raise AgentError(f'Perplexity request failed: {type(exc).__name__}') from None
        if not isinstance(body, dict) or body.get('status') != 'completed' or body.get('error'):
            raise AgentError('Perplexity response did not complete successfully')
        texts = []
        for item in body.get('output', []):
            if item.get('type') == 'message' and item.get('role') == 'assistant' and item.get('status') == 'completed':
                texts.extend(part['text'] for part in item.get('content', [])
                             if part.get('type') == 'output_text' and isinstance(part.get('text'), str))
        content = '\n'.join(texts)
        if not content.strip():
            raise AgentError('Perplexity returned no completed assistant text')
        # Some providers materialize every optional union field as null. Remove
        # only null fields belonging to other actions; preserve everything else
        # for the kernel's authoritative validation.
        if response_format is not None:
            original = response_format['json_schema']['schema']
            branches = original.get('anyOf', [])
            try:
                action = json.loads(content)
            except (TypeError, ValueError):
                action = None
            if isinstance(action, dict):
                matching = [b for b in branches if b.get('properties', {}).get('action', {}).get('const') == action.get('action')]
                if len(matching) == 1:
                    branch = matching[0]
                    allowed = branch['properties']
                    required = set(branch.get('required', []))
                    union_fields = {k for branch in branches for k in branch.get('properties', {})}
                    action = {k: v for k, v in action.items()
                              if v is not None or k in required or k not in union_fields
                              or (k in allowed and Draft202012Validator(allowed[k]).is_valid(None))}
                    content = json.dumps(action, ensure_ascii=False)
        usage = body.get('usage') if isinstance(body.get('usage'), dict) else {}
        def count(name):
            value = usage.get(name)
            return value if type(value) is int and value >= 0 else None
        input_details = usage.get('input_tokens_details') if isinstance(usage.get('input_tokens_details'), dict) else {}
        output_details = usage.get('output_tokens_details') if isinstance(usage.get('output_tokens_details'), dict) else {}
        cached = input_details.get('cached_tokens')
        created = input_details.get('cache_creation_input_tokens')
        read = input_details.get('cache_read_input_tokens')
        reasoning = output_details.get('reasoning_tokens')
        cost = usage.get('cost') if isinstance(usage.get('cost'), dict) else {}
        total_cost = cost.get('total_cost')
        self.last_usage = {'input_tokens': count('input_tokens'), 'output_tokens': count('output_tokens'),
                           'cached_tokens': cached if type(cached) is int and cached >= 0 else None,
                           'cache_creation_input_tokens': created if type(created) is int and created >= 0 else None,
                           'cache_read_input_tokens': read if type(read) is int and read >= 0 else None,
                           'reasoning_tokens': reasoning if type(reasoning) is int and reasoning >= 0 else None,
                           'total_cost': total_cost if type(total_cost) in (int, float) and total_cost >= 0 else None,
                           'currency': cost.get('currency') if isinstance(cost.get('currency'), str) else None}
        turn = ProviderTurn(content, '', 'stop', self.last_usage['input_tokens'], self.last_usage['output_tokens'],
                            self.last_usage['reasoning_tokens'])
        if should_cancel and should_cancel():
            raise WorkerStreamInterrupted(turn)
        return turn

    def stateless_completion(self, messages, **options):
        return self.worker_completion(messages, **options)
