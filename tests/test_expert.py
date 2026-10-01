"""Expert contracts: mocked transport and scripted Worker, never paid API calls."""
import io
import json
import os
import tempfile
import unittest
import urllib.error
import http.client
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch, MagicMock
from pavlusha_agent.cli import build_parser
from pavlusha_agent.core import AgentError
from pavlusha_agent.expert import Expert, EXPERT_SYSTEM
from pavlusha_agent.state_store import StateStore
from tests.test_runtime_lifecycle import run_script, review
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done

EXTRA=['--expert','on','--expert-base-url','https://expert.invalid/v1','--expert-model','consultant','--network']
def ask():
    return turn({'action':'ask_expert','question':'QUESTION','context':'CONTEXT'})
def response(payload=None):
    fake=MagicMock()
    fake.__enter__.return_value=io.StringIO(json.dumps(payload if payload is not None else {
        'choices':[{'message':{'content':'ADVICE: independently verify.', 'reasoning_content':'HIDDEN'},'finish_reason':'stop'}],
        'usage':{'prompt_tokens':999999,'completion_tokens':20}}))
    return fake

class ExpertTests(unittest.TestCase):
    def expert(self,*extra):
        return Expert(build_parser().parse_args([*EXTRA,*extra]))

    def test_request_explicit_context_configuration_usage_and_secrecy(self):
        events=[]
        with patch.dict(os.environ,{'EXPERT_API_KEY':'SECRET_KEY'}), patch('urllib.request.urlopen',return_value=response()) as endpoint:
            result=self.expert('--expert-reasoning-effort','high').ask('QUESTION','CONTEXT',telemetry=events.append)
        req=endpoint.call_args.args[0]
        payload=json.loads(req.data)
        self.assertEqual(payload['messages'],[{'role':'system','content':EXPERT_SYSTEM},
            {'role':'user','content':'QUESTION'},{'role':'user','content':'CONTEXT'}])
        self.assertEqual(req.full_url,'https://expert.invalid/v1/chat/completions')
        self.assertEqual(req.get_header('Authorization'),'Bearer SECRET_KEY')
        self.assertEqual(payload['reasoning_effort'],'high')
        self.assertEqual(payload['max_tokens'],4096)
        self.assertNotIn('tools',payload)
        self.assertEqual(result['answer'],'ADVICE: independently verify.')
        self.assertEqual(result['prompt_tokens'],999999)
        self.assertEqual([e['event'] for e in events],['started','completed'])
        self.assertNotIn('answer',str(events))
        self.assertNotIn('SECRET_KEY',str(result)+str(events)+str(payload))
        self.assertNotIn('HIDDEN',str(result)+str(events))

    def test_mock_http_endpoint_returns_answer_to_worker(self):
        requests=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                requests.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                body=json.dumps({'choices':[{'message':{'content':'LOCAL_ENDPOINT_ADVICE'}}]}).encode()
                self.send_response(200)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            def log_message(self,*args):
                pass
        with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    seen,result,error=run_script(Path(tmp),[init_turn(),done('verified'),ask(),
                        turn({'action':'shell','command':'verify advice'}),turn({'action':'finish','summary':'done'})],
                        extra=[*EXTRA,'--expert-base-url',f'http://127.0.0.1:{server.server_port}/v1'])
                    self.assertEqual(result,0,error)
                    self.assertIn('LOCAL_ENDPOINT_ADVICE',str(seen[3]))
                    self.assertIn('SHELL RESULT',str(seen[4]))
                    self.assertEqual(len(requests),1)
                    self.assertEqual(requests[0][0],'/v1/chat/completions')
                    self.assertEqual(requests[0][1]['messages'][1:],[
                        {'role':'user','content':'QUESTION'},{'role':'user','content':'CONTEXT'}])
            finally:
                server.shutdown()
                thread.join(2)

    def test_failures_and_attempt_ceiling(self):
        failures=[(TimeoutError('SECRET_KEY'),'expert_timeout'),
            (http.client.IncompleteRead(b'SECRET_KEY'),'expert_connection_error'),
            (urllib.error.URLError('SECRET_KEY'),'expert_connection_error')]
        for status in (400,401,429,503):
            failures.append((urllib.error.HTTPError('url',status,'SECRET_KEY',{},io.BytesIO(b'context length exceeded SECRET_KEY')),'expert_http_error'))
        for failure,expected in failures:
            with self.subTest(failure=failure),patch('urllib.request.urlopen',side_effect=failure) as endpoint:
                expert=self.expert('--expert-max-calls','1')
                result=expert.ask('q','c')
                self.assertEqual(result['error'],expected)
                self.assertNotIn('SECRET_KEY',str(result))
                self.assertEqual(expert.ask('q','c')['error'],'expert_call_limit')
                self.assertEqual(endpoint.call_count,1)

    def test_malformed_response_is_bounded(self):
        for payload in ({'choices':[]},{'choices':[{'message':None}]},
                        {'choices':[{'message':{'content':42}}]},{'choices':[{'message':{'content':''}}]}):
            with self.subTest(payload=payload),patch('urllib.request.urlopen',return_value=response(payload)):
                self.assertEqual(self.expert().ask('q','c')['error'],'expert_malformed_response')

    def test_reflected_secret_redacted(self):
        with patch.dict(os.environ,{'EXPERT_API_KEY':'SECRET_KEY'}),patch('urllib.request.urlopen',return_value=response(
                {'choices':[{'message':{'content':'SECRET_KEY reflected','reasoning_content':'HIDDEN'}}]})):
            self.assertEqual(self.expert().ask('q','c')['answer'],'[REDACTED] reflected')

    def test_network_input_and_zero_ceiling_denied_without_request(self):
        for extra,q,c,expected in [([], '', 'c','invalid_expert_input'),([], 'q',None,'invalid_expert_input'),
                                  (['--expert-max-calls','0'],'q','c','expert_call_limit')]:
            with patch('urllib.request.urlopen') as endpoint:
                self.assertEqual(self.expert(*extra).ask(q,c)['error'],expected)
                endpoint.assert_not_called()
        expert=self.expert()
        expert.network=False
        with patch('urllib.request.urlopen') as endpoint:
            self.assertEqual(expert.ask('q','c')['error'],'network_not_granted')
            self.assertEqual(expert.calls,0)
            endpoint.assert_not_called()

    def test_configuration_independent_and_validated(self):
        args=build_parser().parse_args(EXTRA)
        self.assertEqual(args.base_url,os.getenv('AGENT_BASE_URL','http://127.0.0.1:1234/v1'))
        for extra in (['--expert-base-url','https://key@host/v1'],['--expert-max-calls','-1'],
                      ['--expert-max-tokens','0'],['--expert-timeout','0'],['--expert-model','']):
            with self.subTest(extra=extra),self.assertRaises(AgentError):
                self.expert(*extra)

    def test_worker_result_state_isolation_gui_lifetime_and_following_shell(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'EXPERT_API_KEY':'SECRET_KEY'}):
            root=Path(tmp)
            shell_seen=[]
            snapshots=[]
            gui=MagicMock()
            project_map=MagicMock()
            project_map.message.return_value={'role':'user','content':'PRIVATE_PROJECT_MAP'}
            gui.__enter__.return_value=gui
            gui.execute.return_value=({'state':'alive'},None)
            def endpoint(*args,**kwargs):
                gui.__exit__.assert_not_called()
                self.assertEqual(gui.execute.call_count,1)
                self.assertEqual(project_map.refresh.call_count,1)
                self.assertEqual(json.loads(args[0].data)['messages'][1:],[
                    {'role':'user','content':'QUESTION'},{'role':'user','content':'CONTEXT'}])
                snapshots.append(StateStore(root/'state','task').get_project_state())
                return response()
            def shell(work,command):
                shell_seen.append(command)
                self.assertEqual(StateStore(root/'state','task').get_project_state(),snapshots[0])
                gui.__exit__.assert_not_called()
                self.assertEqual(project_map.refresh.call_count,1)
            with patch('urllib.request.urlopen',side_effect=endpoint),patch('pavlusha_agent.runtime.GuiRuntime',return_value=gui), \
                 patch('pavlusha_agent.runtime.ProjectMap',return_value=project_map):
                seen,result,error=run_script(root,[init_turn(),done('verified'),
                    turn({'action':'gui_start','command':'app','network':False}),ask(),
                    turn({'action':'shell','command':'independently verify'}),
                    turn({'action':'finish','summary':'done'})],extra=[*EXTRA,'--gui','--project-map','on'],shell_hook=shell)
            self.assertEqual(result,0,error)
            self.assertIn('ask_expert',str(seen[0][0]))
            self.assertIn('ADVICE:',str(seen[4]))
            self.assertEqual(shell_seen,['independently verify'])
            self.assertEqual(gui.execute.call_count,1)
            self.assertEqual(gui.__exit__.call_count,1)
            self.assertEqual(project_map.refresh.call_count,1)
            self.assertNotIn('PROJECT CHECKPOINT REQUIRED',str(seen[5][1:]))
            files=''.join(p.read_text() for p in (root/'state').rglob('*') if p.is_file())
            self.assertNotIn('SECRET_KEY',files+str(seen))
            self.assertNotIn('HIDDEN',files+str(seen))
            self.assertNotIn('ADVICE:',json.dumps(StateStore(root/'state','task').load()))
            telemetry=(root/'state/experiment.jsonl').read_text()
            self.assertIn('expert_call',telemetry)
            self.assertNotIn('CONTEXT',telemetry)
            self.assertNotIn('ADVICE:',telemetry)

    def test_failure_and_ceiling_leave_worker_usable(self):
        with tempfile.TemporaryDirectory() as tmp,patch('urllib.request.urlopen',side_effect=TimeoutError) as endpoint:
            seen,result,error=run_script(Path(tmp),[init_turn(),done('verified'),ask(),ask(),
                turn({'action':'shell','command':'continue'}),turn({'action':'finish','summary':'done'})],
                extra=[*EXTRA,'--expert-max-calls','1'])
            self.assertEqual(result,0,error)
            self.assertIn('expert_timeout',str(seen[3]))
            self.assertIn('expert_call_limit',str(seen[4]))
            self.assertIn('SHELL RESULT',str(seen[5]))
            self.assertEqual(endpoint.call_count,1)

    def test_disabled_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp,patch('urllib.request.urlopen') as endpoint:
            seen,result,error=run_script(Path(tmp),[init_turn(),done('verified'),turn({'action':'finish','summary':'done'})])
            self.assertEqual(result,0,error)
            self.assertNotIn('ask_expert',str(seen))
            endpoint.assert_not_called()

    def test_disabled_invocation_and_enabled_bad_input_do_not_call_provider(self):
        for extra in ([],EXTRA):
            with tempfile.TemporaryDirectory() as tmp,patch('urllib.request.urlopen') as endpoint:
                seen,result,error=run_script(Path(tmp),[init_turn(),done('verified'),
                    turn({'action':'ask_expert','question':42,'context':'c'}),
                    turn({'action':'shell','command':'continue'}),turn({'action':'finish','summary':'done'})],extra=extra)
                self.assertEqual(result,0,error)
                self.assertIn('invalid_expert_input' if extra else 'INVALID ACTION',str(seen[3]))
                endpoint.assert_not_called()

    def test_checkpoint_gate_blocks_expert_until_review(self):
        with tempfile.TemporaryDirectory() as tmp,patch('urllib.request.urlopen',return_value=response()) as endpoint:
            seen,result,error=run_script(Path(tmp),[init_turn(),done('verified'),ask(),
                review('Continue.'),ask(),turn({'action':'finish','summary':'done'})],
                extra=[*EXTRA,'--history-high','2'])
            self.assertEqual(result,0,error)
            self.assertEqual(endpoint.call_count,1)
            self.assertIn('PROJECT CHECKPOINT REQUIRED',str(seen[3][1:]))

    def test_recovery_no_replay_existing_history_archive(self):
        with tempfile.TemporaryDirectory() as tmp,patch('urllib.request.urlopen',return_value=response()) as endpoint:
            root=Path(tmp)
            seen,result,error=run_script(root,[init_turn(),ask(),review('Continue verification.'),
                turn({'action':'shell','command':'verify'})],extra=[*EXTRA,'--history-high','2','--max-steps','4'])
            self.assertIn('maximum of 4 steps',error)
            self.assertIn('EXPERT RESULT',(root/'state/history_archive.jsonl').read_text())
            seen,result,error=run_script(root,[done('verified'),turn({'action':'finish','summary':'done'})],extra=EXTRA)
            self.assertEqual(result,0,error)
            self.assertEqual(endpoint.call_count,1)
            self.assertNotIn('EXPERT RESULT',str(seen[0]))
            self.assertIn('Continue verification.',str(seen[0]))

if __name__=='__main__':
    unittest.main()
