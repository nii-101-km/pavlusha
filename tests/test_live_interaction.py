"""Safe live key, pause, input and continuation boundaries."""
import io
import json
import os
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.basic_host import BasicHost
from pavlusha_agent.core import ProviderTurn, ShellResult
from pavlusha_agent.extensions import Contribution, Outcome
from pavlusha_agent.model_backend import WorkerStreamInterrupted
from pavlusha_agent.runtime import Runtime
from pavlusha_modules.interactive import Interactive
from tests.test_events import Probe, Provider, reply, tool


class FakeUI:
    def __init__(self, answers, inspect=None):
        self.answers=iter(answers); self.inspect=inspect; self.inputs=[]; self.closed=False
    def start(self, post): self.post=post
    def read_message(self, prompt, *, require_message=False):
        if self.inspect:self.inspect()
        self.inputs.append((prompt,require_message))
        return next(self.answers)
    def close(self):self.closed=True


class LiveInteractionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.calls=[]
    def make(self,provider,ui,extra=(),shell=None,**options):
        def runner(root,command,**kwargs):
            self.calls.append(command)
            if shell:shell()
            return ShellResult(command,False,0,False,'done','',.01)
        self.ui=ui
        self.runtime=Runtime(provider,task='task',session_dir=self.root/'session',
            extensions=[BasicHost(self.root,shell_runner=runner),Interactive(),*extra],
            interaction_ui=ui,**options)
        return self.runtime
    def test_normal_pause_finishes_turn_then_takes_input_without_closing_modules(self):
        probe=Probe();ui=FakeUI(['Change direction'])
        class Normal(Provider):
            def worker_completion(inner,messages,**options):
                result=super(Normal,inner).worker_completion(messages,**options)
                if len(inner.requests)==1:
                    ui.post('ctrl_z')
                    self.assertIsNone(options['on_delta']('reasoning','still working'))
                return result
        provider=Normal([reply({'action':'shell','command':'one'}),reply({'action':'finish','summary':'done'})])
        runtime=self.make(provider,ui,[probe]);ui.inspect=lambda:self.assertEqual(self.calls,['one'])
        self.assertEqual(runtime.run(),'done')
        self.assertEqual(self.calls,['one']);self.assertTrue(ui.closed)
        self.assertIn('Change direction',str(provider.requests[1]['messages']))
        self.assertEqual([e.kind for e in probe.events].count('session_started'),1)
        self.assertEqual([e.kind for e in probe.events].count('session_stopped'),1)
        self.assertTrue(any(e.kind=='paused' for e in probe.events))
    def test_urgent_pause_discards_partial_generation_and_never_executes_action(self):
        ui=FakeUI(['Use different approach']);probe=Probe()
        class Urgent(Provider):
            def worker_completion(inner,messages,**options):
                result=super(Urgent,inner).worker_completion(messages,**options)
                if len(inner.requests)==1:
                    ui.post('ctrl_backslash')
                    self.assertFalse(options['on_delta']('reasoning','PRIVATE PARTIAL'))
                    raise WorkerStreamInterrupted(result)
                return result
        provider=Urgent([reply({'action':'shell','command':'old'}),reply({'action':'finish','summary':'done'})])
        runtime=self.make(provider,ui,[probe]);self.assertEqual(runtime.run(),'done')
        self.assertEqual(self.calls,[])
        second=str(provider.requests[1]['messages'])
        self.assertIn('Use different approach',second)
        self.assertNotIn('PRIVATE PARTIAL',second);self.assertNotIn('"command": "old"',second)
        turn=next(e.data['turn'] for e in probe.events if e.kind=='turn_completed')
        self.assertEqual(turn['status'],'generation_interrupted')
        self.assertIsNone(turn['response']);self.assertIsNone(turn['action'])
    def test_urgent_key_after_response_before_dispatch_discards_action(self):
        from jsonschema import Draft202012Validator
        ui=FakeUI(['new guidance']);probe=Probe()
        provider=Provider([reply({'action':'shell','command':'must-not-run'}),reply({'action':'finish','summary':'done'})])
        runtime=self.make(provider,ui,[probe]);original=Draft202012Validator.validate;fired=[]
        def inject(validator,instance,*args,**kwargs):
            if instance.get('action')=='shell' and not fired:
                fired.append(True);ui.post('ctrl_backslash')
            return original(validator,instance,*args,**kwargs)
        with patch.object(Draft202012Validator,'validate',inject):
            self.assertEqual(runtime.run(),'done')
        self.assertEqual(self.calls,[])
        self.assertIn('new guidance',str(provider.requests[1]['messages']))
    def test_both_modes_wait_until_executing_tool_and_result_are_saved(self):
        for key in ('ctrl_z','ctrl_backslash'):
            with self.subTest(key=key),tempfile.TemporaryDirectory() as tmp:
                self.root=Path(tmp);self.calls=[];ui=FakeUI(['next'])
                provider=Provider([reply({'action':'shell','command':'long'}),reply({'action':'finish','summary':'done'})])
                runtime=self.make(provider,ui,shell=lambda:ui.post(key))
                def inspect():
                    state=json.loads((self.root/'session/session.json').read_text())
                    self.assertIsNone(state['pending'])
                    self.assertEqual(state['turn_id'],1)
                    self.assertIn('done',str(state['history']))
                    self.assertEqual(self.calls,['long'])
                ui.inspect=inspect
                self.assertEqual(runtime.run(),'done')
                self.assertEqual(self.calls,['long'])
    def test_key_event_is_delivered_during_exclusive_review(self):
        ui=FakeUI(['note during review']);seen=[]
        def hook(event):
            seen.append(event.kind)
            if event.kind=='session_started':
                return Contribution(tools=(tool('review'),),exclusive=True)
            if event.kind=='turn_completed':return Contribution()
        provider=Provider([reply({'action':'review'}),reply({'action':'finish','summary':'done'})])
        def key(messages,options):
            if len(provider.requests)==1:ui.post('ctrl_z')
        provider.hook=key
        runtime=self.make(provider,ui,[Probe('review_module',hook)])
        self.assertEqual(runtime.run(),'done')
        self.assertIn('key_pressed',seen);self.assertIn('paused',seen)
        self.assertIn('note during review',str(provider.requests[1]['messages']))
    def test_model_ask_user_uses_same_process_when_terminal_available(self):
        ui=FakeUI(['blue']);provider=Provider([reply({'action':'ask_user','question':'Which color?'}),
                                              reply({'action':'finish','summary':'done'})])
        runtime=self.make(provider,ui);self.assertEqual(runtime.run(),'done')
        self.assertEqual(ui.inputs,[('Which color?',True)])
        self.assertIn('blue',str(provider.requests[1]['messages']))
        self.assertEqual(runtime.session.state['status'],'finished')
    def test_empty_reply_resumes_and_eof_stops_without_finish(self):
        for answer in ('',None):
            with self.subTest(answer=answer),tempfile.TemporaryDirectory() as tmp:
                self.root=Path(tmp);ui=FakeUI([answer]);probe=Probe()
                provider=Provider([reply({'action':'shell','command':'one'}),reply({'action':'finish','summary':'done'})])
                provider.hook=lambda messages,options: ui.post('ctrl_z') if len(provider.requests)==1 else None
                runtime=self.make(provider,ui,[probe]);result=runtime.run()
                if answer is None:
                    self.assertEqual(result,'Session ended by user.')
                    self.assertEqual(runtime.session.state['status'],'stopped')
                    self.assertEqual(len(provider.requests),1)
                else:
                    self.assertEqual(result,'done')
                    self.assertNotIn('USER REPLY:',str(provider.requests[1]['messages']))
    def test_next_sse_line_checks_keys_even_without_reasoning_or_content(self):
        from pavlusha_agent.adapters import ChatProvider
        from unittest.mock import patch
        ui=FakeUI(['stop old request']);probe=Probe();calls=[]
        frames=[{'choices':[{'delta':{'tool_calls':[{'index':0,'id':'id'}]}}]},
                {'choices':[{'delta':{'content':'{"action":"shell","command":"old"}'},'finish_reason':'stop'}]}]
        data=(''.join('data: '+json.dumps(f)+'\n\n' for f in frames)+'data: [DONE]\n\n').encode()
        class InputStream(io.BytesIO):
            def __iter__(self):
                while line := self.readline():
                    if line.startswith(b'data:') and not calls:
                        calls.append(1);ui.post('ctrl_backslash')
                    yield line
        real=ChatProvider('http://invalid.local/v1','test','',5,.1,100,backend='chat-completions')
        second={'choices':[{'delta':{'content':'{"action":"finish","summary":"done"}'},'finish_reason':'stop'}]}
        output=('data: '+json.dumps(second)+'\n\ndata: [DONE]\n\n').encode()
        runtime=self.make(real,ui,[probe])
        with patch('urllib.request.urlopen',side_effect=[InputStream(data),io.BytesIO(output)]):
            self.assertEqual(runtime.run(),'done')
        self.assertEqual(self.calls,[])
        self.assertEqual(next(e.data['turn'] for e in probe.events if e.kind=='turn_completed')['status'],
                         'generation_interrupted')


class TerminalAdapterTests(unittest.TestCase):
    def run_message_input(self, steps, *, require_message=False):
        master,slave=pty.openpty()
        self.addCleanup(os.close,master);self.addCleanup(os.close,slave)
        code='''import json,sys,termios\nfrom pavlusha_agent.terminal_input import TerminalInput\nfd=sys.stdin.fileno();before=termios.tcgetattr(fd)\nu=TerminalInput();u.start(lambda key:None)\nvalue=u.read_message("Type reply",require_message=REQUIRE)\nu.close();print("\\nVALUE_JSON:"+json.dumps(value),flush=True)\nprint("RESTORED:"+str(termios.tcgetattr(fd)==before),flush=True)\n'''.replace('REQUIRE',repr(require_message))
        process=subprocess.Popen([sys.executable,'-u','-c',code],stdin=slave,stdout=slave,stderr=slave,
                                 cwd=Path(__file__).resolve().parents[1],close_fds=True)
        self.addCleanup(lambda:process.kill() if process.poll() is None else None)
        data=b''
        def until(marker):
            nonlocal data
            deadline=time.monotonic()+8
            while marker not in data and time.monotonic()<deadline:
                if select.select([master],[],[],.2)[0]:
                    try:data+=os.read(master,4096)
                    except OSError:break
            self.assertIn(marker,data)
        until(b'> ')
        for payload,marker in steps:
            os.write(master,payload)
            if marker: until(marker)
        until(b'RESTORED:True')
        self.assertEqual(process.wait(timeout=5),0)
        return json.loads(data.split(b'VALUE_JSON:',1)[1].splitlines()[0])

    def test_bracketed_paste_is_one_message(self):
        message='Первая строка\n\ndef example():\n    return 42\n'
        result=self.run_message_input([(b'\x1b[200~'+message.encode()+b'\x1b[201~', b'return 42'),
                                       (b'\r',None)])
        self.assertEqual(result,message)

    def test_manual_multiline_preserves_blank_lines(self):
        result=self.run_message_input([(b'/multiline\r', 'Многострочный ввод'.encode()),
                                       ('один\n\nдва\n/send\n'.encode(),None)])
        self.assertEqual(result,'один\n\nдва')

    def test_required_message_allows_correction(self):
        result=self.run_message_input([(b'\r','Нужен ответ'.encode()),(b'abc\x7fd\r',None)],require_message=True)
        self.assertEqual(result,'abd')

    def test_real_pty_keys_input_and_terminal_restoration(self):
        master,slave=pty.openpty()
        self.addCleanup(os.close,master);self.addCleanup(os.close,slave)
        code='''import queue,sys,termios\nfrom pavlusha_agent.terminal_input import TerminalInput\nq=queue.Queue();fd=sys.stdin.fileno();before=termios.tcgetattr(fd)\nu=TerminalInput();u.start(q.put);print("READY",flush=True)\nkey=q.get(timeout=5);print("KEY:"+key,flush=True)\nvalue=u.read_message("Type reply")\nprint("VALUE:"+repr(value),flush=True);u.close()\nprint("RESTORED:"+str(termios.tcgetattr(fd)==before),flush=True)\n'''
        process=subprocess.Popen([sys.executable,'-u','-c',code],stdin=slave,stdout=slave,stderr=slave,
                                 cwd=Path(__file__).resolve().parents[1],close_fds=True)
        self.addCleanup(lambda:process.kill() if process.poll() is None else None)
        data=b''
        def until(marker):
            nonlocal data
            deadline=time.monotonic()+8
            while marker not in data and time.monotonic()<deadline:
                if select.select([master],[],[],.2)[0]:
                    try:data+=os.read(master,4096)
                    except OSError:break
            self.assertIn(marker,data)
        until(b'READY');os.write(master,b'\x1a');until(b'KEY:ctrl_z');until(b'PAUSED')
        os.write(master,'Привет\n'.encode())
        until(b'RESTORED:True');self.assertIn('Привет'.encode(),data)
        self.assertEqual(process.wait(timeout=5),0)
