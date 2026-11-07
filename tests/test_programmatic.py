import json
import shlex
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from pavlusha_agent.core import ProviderTurn
from pavlusha_agent.extensions import Contribution, Event, Outcome, Tool
from pavlusha_agent.runtime import Runtime
from pavlusha_modules.programmatic import create_extension


def reply(action):
    return ProviderTurn(json.dumps(action), '', 'stop')


class Provider:
    def __init__(self, actions): self.actions = iter(actions)
    def worker_completion(self, messages, **kwargs): return reply(next(self.actions))


class Sample:
    name = 'sample'
    def __init__(self): self.called = []
    def on_event(self, event):
        if event.kind != 'session_started': return None
        schema = {'type': 'object', 'properties': {'value': {'type': 'integer'}},
                  'required': ['value'], 'additionalProperties': False}
        def handler(args, context):
            self.called.append(args['value'])
            return Outcome({'value': args['value'] * 2})
        return Contribution(tools=(Tool('double', 'Double a number.', schema, handler, programmatic=True),
                                   Tool('hidden', 'Not available to programs.', schema, handler)))


class Done:
    name = 'done'
    def on_event(self, event):
        if event.kind == 'session_started':
            from pavlusha_agent.extensions import Control
            return Contribution(tools=(Tool('finish', 'Finish.', {'type': 'object', 'properties': {},
                'required': [], 'additionalProperties': False}, lambda a,c: Outcome({'ok': True},
                controls=(Control('stop', 'done', finished=True),))),))


class ProgrammaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('bwrap'):
            raise unittest.SkipTest('bubblewrap unavailable')
        test = subprocess.run(['bwrap', '--ro-bind', '/', '/', '--proc', '/proc', '--dev', '/dev', '--', '/bin/true'],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if test.returncode:
            raise unittest.SkipTest('bubblewrap namespaces unavailable')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'work').mkdir()
        self.sample = Sample()

    def run_program(self, code, allowed):
        module = create_extension(settings={'allowed_tools': allowed, 'timeout_seconds': 5},
                                  context={'workdir': str(self.root/'work')})
        runtime = Runtime(Provider([{'action': 'execute_program', 'code': code}, {'action': 'finish'}]),
                          task='test', session_dir=self.root/'session',
                          extensions=[self.sample, module, Done()])
        self.assertEqual(runtime.run(), 'done')
        return runtime.session.completed_turns()[0]

    def test_calls_opted_in_tool_and_journals(self):
        turn = self.run_program('from pavlusha_tools import call\nprint(call("double", value=21)["value"])', ['double'])
        self.assertEqual(turn['result']['status'], 'completed')
        self.assertEqual(turn['result']['output'].strip(), '42')
        self.assertEqual(self.sample.called, [21])
        journal = (self.root/'session/events.jsonl').read_text()
        self.assertIn('programmatic_call_started', journal)
        self.assertIn('programmatic_call_completed', journal)

    def test_saved_python_file_runs_in_new_session_with_allowlist_and_journal(self):
        from pavlusha_modules.shell import Shell
        from pavlusha_agent.basic_host import BasicHost
        work=self.root/'work'
        scenario='''# Pavlusha scenario
# Description: check project files
from pavlusha_tools import call
result = call("shell", command="printf SCENARIO_OK")
print("allowed:", result["stdout"])
try:
    call("double", value=5)
except Exception as exc:
    print("denied:", exc)
'''
        # A normal agent tool saves the reusable file in the project workspace.
        first=Runtime(Provider([{'action':'shell','command':'printf %s '+shlex.quote(scenario)+' > check_project.py'},
                                {'action':'finish','summary':'saved'}]),
                      task='save scenario',session_dir=self.root/'session-save',
                      extensions=[BasicHost(work)])
        self.assertEqual(first.run(),'saved')
        self.assertEqual((work/'check_project.py').read_text(),scenario)
        # A fresh session executes the existing file through the programmatic gateway.
        program=create_extension(settings={'allowed_tools':['shell'],'timeout_seconds':10},
                                 context={'workdir':str(work)})
        code="exec(compile(open('/work/check_project.py', encoding='utf-8').read(), '/work/check_project.py', 'exec'))"
        second=Runtime(Provider([{'action':'execute_program','code':code},{'action':'finish'}]),
                       task='reuse scenario',session_dir=self.root/'session-reuse',
                       extensions=[Shell(work),self.sample,program,Done()])
        self.assertEqual(second.run(),'done')
        turn=second.session.completed_turns()[0]
        self.assertEqual(turn['result']['status'],'completed')
        self.assertIn('allowed: SCENARIO_OK',turn['result']['output'])
        self.assertIn('denied: tool unavailable',turn['result']['output'])
        self.assertEqual(self.sample.called,[])
        entries=[json.loads(line) for line in (self.root/'session-reuse/events.jsonl').read_text().splitlines()]
        starts=[entry for entry in entries if entry.get('kind')=='programmatic_call_started']
        ends=[entry for entry in entries if entry.get('kind')=='programmatic_call_completed']
        self.assertEqual([entry['action']['action'] for entry in starts],['shell'])
        self.assertEqual([entry['action']['action'] for entry in ends],['shell'])
        self.assertTrue(any('check_project.py' in json.dumps(entry) for entry in entries))

    def test_config_and_owner_both_must_allow(self):
        code = '''from pavlusha_tools import call
for name in ("double", "hidden", "finish"):
    try: call(name, value=1)
    except Exception: print(name, "denied")
'''
        turn = self.run_program(code, ['hidden', 'finish'])
        self.assertEqual(turn['result']['status'], 'completed')
        self.assertEqual(self.sample.called, [])
        for name in ('double', 'hidden', 'finish'):
            self.assertIn(name + ' denied', turn['result']['output'])

    def test_schema_validation_and_timeout(self):
        turn = self.run_program('from pavlusha_tools import call\ncall("double", value="bad")', ['double'])
        self.assertEqual(turn['result']['status'], 'failed')
        self.assertEqual(self.sample.called, [])

    def test_program_timeout(self):
        module = create_extension(settings={'allowed_tools': [], 'timeout_seconds': 1},
                                  context={'workdir': str(self.root/'work')})
        runtime = Runtime(Provider([{'action': 'execute_program', 'code': 'import time\ntime.sleep(20)'},
                                    {'action': 'finish'}]), task='test', session_dir=self.root/'session',
                          extensions=[module, Done()])
        self.assertEqual(runtime.run(), 'done')
        self.assertEqual(runtime.session.completed_turns()[0]['result']['status'], 'timeout')

    def test_raw_socket_cannot_bypass_allowlist(self):
        code = '''import json, socket
sock=socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect("/opt/pavlusha/programmatic/rpc.sock")
sock.sendall((json.dumps({"name":"double","arguments":{"value":3}})+"\\n").encode())
print(sock.recv(4096).decode())
'''
        turn=self.run_program(code, [])
        self.assertEqual(turn['result']['status'],'completed')
        self.assertIn('tool unavailable',turn['result']['output'])
        self.assertEqual(self.sample.called,[])

    def test_limit_blocks_additional_tool_side_effects(self):
        module=create_extension(settings={'allowed_tools':['double'],'timeout_seconds':5,'max_calls':1},
                                context={'workdir':str(self.root/'work')})
        code='''from pavlusha_tools import call
print(call("double", value=1))
try: call("double", value=2)
except Exception as e: print(e)
'''
        runtime=Runtime(Provider([{'action':'execute_program','code':code},{'action':'finish'}]),
                        task='test',session_dir=self.root/'session',extensions=[self.sample,module,Done()])
        self.assertEqual(runtime.run(),'done')
        self.assertEqual(self.sample.called,[1])
        self.assertIn('tool call limit exceeded',runtime.session.completed_turns()[0]['result']['output'])

class ProgrammaticOrchestrationTests(ProgrammaticTests):
    def orchestration(self):
        from pavlusha_modules.orchestration import ArishkaDelegation
        manifest = self.root / 'child.json'
        manifest.write_text(json.dumps({'modules': []}))
        return ArishkaDelegation({'modules': str(manifest), 'base_url': 'http://127.0.0.1:1/v1', 'model': 'test'},
                                 {'workdir': str(self.root/'work')})

    def make_runtime(self, code, allowed):
        module = create_extension(settings={'allowed_tools': allowed, 'timeout_seconds': 10},
                                  context={'workdir': str(self.root/'work')})
        return Runtime(Provider([{'action': 'execute_program', 'code': code}, {'action': 'finish'}]),
                       task='test', session_dir=self.root/'session', extensions=[self.orchestration(), module, Done()])

    def test_delegate_and_collect_through_program(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        code = '''from pavlusha_tools import call
r = call("delegate_agent", task="create output", inputs=[])
print("status", r["status"], "summary", r["summary"])
call("collect_agent", child_id=r["child_id"], paths=["result.txt"])
print("collected")
'''
        runtime = self.make_runtime(code, ['delegate_agent', 'collect_agent'])
        def child(cmd, check, env):
            work = Path(cmd[cmd.index('--workdir') + 1])
            state = Path(cmd[cmd.index('--session-dir') + 1])
            (work/'result.txt').write_text('verified')
            state.mkdir()
            (state/'session.json').write_text('{"status":"finished"}')
            (state/'events.jsonl').write_text(json.dumps({'kind': 'turn_completed', 'stop': True,
                                                          'result': {'summary': 'success'}}) + '\n')
            (work.parent/'exit.json').write_text('{"exit_code":0}')
            return SimpleNamespace(returncode=0)
        with patch('pavlusha_modules.orchestration.shutil.which', return_value='gnome-terminal'), \
             patch.dict('os.environ', {'DISPLAY': ':1'}), \
             patch('pavlusha_modules.orchestration.subprocess.run', side_effect=child):
            self.assertEqual(runtime.run(), 'done')
        result = runtime.session.completed_turns()[0]['result']
        self.assertEqual(result['status'], 'completed')
        self.assertIn('status finished summary success', result['output'])
        self.assertEqual((self.root/'work/result.txt').read_text(), 'verified')

    def test_unconfirmed_child_preserves_pending_action(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        from pavlusha_agent.core import ProcessCleanupError
        runtime = self.make_runtime('from pavlusha_tools import call\ncall("delegate_agent", task="test", inputs=[])',
                                    ['delegate_agent'])
        with patch('pavlusha_modules.orchestration.shutil.which', return_value='gnome-terminal'), \
             patch.dict('os.environ', {'DISPLAY': ':1'}), \
             patch('pavlusha_modules.orchestration.subprocess.run', return_value=SimpleNamespace(returncode=0)):
            with self.assertRaises(ProcessCleanupError):
                runtime.run()
        self.assertEqual(runtime.session.state['pending']['action'], 'execute_program')
