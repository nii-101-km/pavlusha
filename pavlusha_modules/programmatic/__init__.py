"""Bounded Python programs with explicit access to opted-in module tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shlex
import socket
import subprocess
import tempfile
import time

from pavlusha_agent.core import AgentError, ProcessCleanupError
from pavlusha_agent.extensions import Contribution, Outcome, Tool
from pavlusha_agent.process_supervision import stop_owned_process
from pavlusha_runtime.sandbox import build_bwrap_command

CLIENT = '''import json, socket

def call(name, **arguments):
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError("invalid tool name")
    data = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False, allow_nan=False).encode() + b"\\n"
    if len(data) > 65536:
        raise ValueError("tool request too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect("/opt/pavlusha/programmatic/rpc.sock")
        sock.sendall(data)
        response = b""
        while b"\\n" not in response:
            chunk = sock.recv(65536)
            if not chunk: raise RuntimeError("tool connection closed")
            response += chunk
            if len(response) > 131072: raise RuntimeError("tool response too large")
    result = json.loads(response.split(b"\\n", 1)[0])
    if "error" in result: raise RuntimeError(result["error"])
    return result["result"]
'''


class Programmatic:
    name = 'programmatic'

    def __init__(self, workdir, allowed, timeout, max_calls, output_limit):
        self.workdir = Path(workdir).resolve()
        self.allowed = frozenset(allowed)
        self.timeout = timeout
        self.max_calls = max_calls
        self.output_limit = output_limit

    def on_event(self, event):
        if event.kind == 'session_started':
            schema = {'type': 'object', 'properties': {'code': {'type': 'string', 'minLength': 1, 'maxLength': 32000}},
                      'required': ['code'], 'additionalProperties': False}
            names = ', '.join(sorted(self.allowed)) or '(none)'
            return Contribution(system_instructions=(
                'execute_program runs bounded Python in /work. Import call from pavlusha_tools and use '
                'call("tool_name", argument=value). Only permitted programmatic tools can be called: '
                + names + '. Tool outcomes may have side effects. Print a concise result.'),
                tools=(Tool('execute_program', 'Run a Python program with explicitly permitted tool calls.',
                            schema, self.execute, accepts_tool_calls=True),))

    def execute(self, args, context):
        if context.call_tool is None:
            raise AgentError('programmatic kernel interface unavailable')
        with tempfile.TemporaryDirectory(prefix='pavlusha-program-') as tmp:
            root = Path(tmp)
            (root / 'pavlusha_tools.py').write_text(CLIENT, encoding='utf-8')
            (root / 'program.py').write_text(args['code'], encoding='utf-8')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(root / 'rpc.sock'))
                server.listen(1)
                server.setblocking(False)
                command = 'exec python3 -u ' + shlex.quote('/opt/pavlusha/programmatic/program.py')
                argv = build_bwrap_command(self.workdir, command, network=False)
                index = argv.index('/bin/bash')
                argv[index:index] = ['--dir', '/opt/pavlusha/programmatic', '--ro-bind', str(root), '/opt/pavlusha/programmatic']
                try:
                    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                except OSError as exc:
                    raise AgentError('cannot start program: ' + str(exc)) from exc
                output = bytearray()
                calls = 0
                deadline = time.monotonic() + self.timeout
                timed_out = False
                limited = False
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(server, selectors.EVENT_READ, 'socket')
                        selector.register(process.stdout, selectors.EVENT_READ, 'output')
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                timed_out = True
                                break
                            if process.poll() is not None and not selector.get_map().get(process.stdout.fileno()):
                                break
                            for key, _ in selector.select(min(remaining, .1)):
                                if key.data == 'output':
                                    chunk = os.read(process.stdout.fileno(), 65536)
                                    if not chunk:
                                        selector.unregister(process.stdout)
                                    else:
                                        output.extend(chunk)
                                        if len(output) > self.output_limit:
                                            limited = True
                                            break
                                else:
                                    conn, _ = server.accept()
                                    with conn:
                                        conn.settimeout(min(1.0, max(.01, deadline-time.monotonic())))
                                        request = b''
                                        try:
                                            while b'\n' not in request and len(request) <= 65536:
                                                part = conn.recv(65536)
                                                if not part: break
                                                request += part
                                            if b'\n' not in request or len(request) > 65536:
                                                raise ValueError('invalid request')
                                            msg = json.loads(request.split(b'\n', 1)[0])
                                            if (type(msg) is not dict or set(msg) != {'name', 'arguments'}
                                                    or type(msg['name']) is not str or type(msg['arguments']) is not dict
                                                    or 'action' in msg['arguments']):
                                                raise ValueError('invalid request')
                                            if msg['name'] not in self.allowed:
                                                raise ValueError('tool unavailable')
                                            calls += 1
                                            if calls > self.max_calls:
                                                raise ValueError('tool call limit exceeded')
                                            result = context.call_tool({'action': msg['name'], **msg['arguments']})
                                            answer = {'result': result}
                                        except ProcessCleanupError:
                                            raise
                                        except Exception as exc:
                                            answer = {'error': str(exc)[:1000] + '; effects may have occurred'}
                                        try:
                                            payload = json.dumps(answer, ensure_ascii=False, allow_nan=False).encode() + b'\n'
                                            if len(payload) > 131072:
                                                payload = b'{"error":"response too large; effects may have occurred"}\n'
                                            conn.sendall(payload)
                                        except (OSError, ValueError):
                                            pass
                            if limited:
                                break
                    if timed_out or limited:
                        stop_owned_process(process)
                    else:
                        process.wait()
                except BaseException:
                    stop_owned_process(process)
                    raise
                finally:
                    process.stdout.close()
                return Outcome({'status': 'timeout' if timed_out else 'output_limit' if limited else
                                'completed' if process.returncode == 0 else 'failed',
                                'exit_code': process.returncode, 'tool_calls': calls,
                                'output': output[:self.output_limit].decode('utf-8', 'replace'),
                                'effects_may_have_occurred': bool(calls)})


def create_extension(*, settings, context):
    if set(settings) - {'allowed_tools', 'timeout_seconds', 'max_calls', 'output_limit'}:
        raise ValueError('unknown programmatic setting')
    allowed = settings.get('allowed_tools', [])
    timeout = settings.get('timeout_seconds', 60)
    max_calls = settings.get('max_calls', 20)
    output_limit = settings.get('output_limit', 8000)
    if (type(allowed) is not list or any(type(n) is not str or not n.isidentifier() for n in allowed)
            or len(set(allowed)) != len(allowed) or type(timeout) is not int or not 1 <= timeout <= 300
            or type(max_calls) is not int or not 1 <= max_calls <= 100
            or type(output_limit) is not int or not 1 <= output_limit <= 16000):
        raise ValueError('invalid programmatic settings')
    return Programmatic(context['workdir'], allowed, timeout, max_calls, output_limit)
