"""Perplexity Agent wire contract and unchanged Worker tool lifecycle."""
import copy
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch, MagicMock

from pavlusha_agent.cli import build_parser
from pavlusha_agent.expert import Expert, EXPERT_SYSTEM
from pavlusha_agent.state_store import StateStore
from tests.test_expert import EXTRA, ask, response
from tests.test_runtime_lifecycle import run_script, review
from tests.test_reasoning_window import init_turn, turn
from tests.test_checkpoint_snapshots import done

AGENT_EXTRA=[*EXTRA,'--expert-transport','perplexity-agent','--expert-api-key-env','PERPLEXITY_API_KEY']

def agent_body():
    return {'status':'completed','error':None,'output':[
        {'type':'reasoning','summary':'HIDDEN_REASONING'},
        {'type':'message','role':'assistant','status':'completed','content':[
            {'type':'output_text','text':'ADVICE'}, {'type':'output_text','text':'Verify independently.'}]}],
        'usage':{'input_tokens':999999,'output_tokens':40,'cost':{'total_cost':0.002,'currency':'USD'}}}

class PerplexityExpertTests(unittest.TestCase):
    def expert(self,*extra):
        return Expert(build_parser().parse_args([*AGENT_EXTRA,*extra]))

    def test_documented_request_auth_explicit_input_and_usage(self):
        events=[]
        with patch.dict(os.environ,{'PERPLEXITY_API_KEY':'PERPLEXITY_SECRET','EXPERT_API_KEY':'WRONG_KEY'}), \
             patch('urllib.request.urlopen',return_value=response(agent_body())) as endpoint:
            result=self.expert('--expert-reasoning-effort','high','--expert-max-tokens','16000').ask('QUESTION','CONTEXT',telemetry=events.append)
        req=endpoint.call_args.args[0]
        self.assertEqual(req.full_url,'https://expert.invalid/v1/agent')
        self.assertEqual(req.get_method(),'POST')
        self.assertEqual(req.get_header('Authorization'),'Bearer PERPLEXITY_SECRET')
        self.assertEqual(endpoint.call_args.kwargs['timeout'],60)
        payload=json.loads(req.data)
        self.assertEqual(payload,{'model':'consultant','instructions':EXPERT_SYSTEM,
            'input':[{'type':'message','role':'user','content':'QUESTION'},
                     {'type':'message','role':'user','content':'CONTEXT'}],
            'max_output_tokens':16000,'reasoning':{'effort':'high'},'stream':False})
        self.assertEqual(result['answer'],'ADVICE\nVerify independently.')
        self.assertEqual(result['prompt_tokens'],999999)
        self.assertEqual(result['completion_tokens'],40)
        self.assertEqual([e['event'] for e in events],['started','completed'])
        self.assertEqual(events[1]['prompt_tokens'],999999)
        self.assertNotIn('answer',str(events))
        for marker in ('HIDDEN_REASONING','PERPLEXITY_SECRET','WRONG_KEY'):
            self.assertNotIn(marker,str(payload)+str(result)+str(events))
        # All unsupported output structures stay below the transport boundary.
        self.assertNotIn('output',result)
        self.assertNotIn('cost',result)

    def test_missing_or_invalid_usage_is_unknown_and_reasoning_optional(self):
        for usage in (None,{}, {'input_tokens':True,'output_tokens':-1},
                      {'input_tokens':'15','output_tokens':2.5}):
            body=agent_body()
            body['usage']=usage
            with self.subTest(usage=usage),patch('urllib.request.urlopen',return_value=response(body)) as endpoint:
                result=self.expert('--expert-base-url','https://api.perplexity.ai/v1/').ask('q','')
                self.assertIsNone(result['prompt_tokens'])
                self.assertIsNone(result['completion_tokens'])
                self.assertNotIn('reasoning',json.loads(endpoint.call_args.args[0].data))
                self.assertEqual(endpoint.call_args.args[0].full_url,'https://api.perplexity.ai/v1/agent')

    def test_only_completed_assistant_output_text_is_returned(self):
        body=agent_body()
        body['output'].extend([
            {'type':'function_call','arguments':'DO_NOT_EXECUTE'},
            {'type':'message','role':'user','status':'completed','content':[{'type':'output_text','text':'NOT_ASSISTANT'}]},
            {'type':'message','role':'assistant','status':'in_progress','content':[{'type':'output_text','text':'NOT_FINAL'}]}])
        with patch('urllib.request.urlopen',return_value=response(body)):
            self.assertEqual(self.expert().ask('q','c')['answer'],'ADVICE\nVerify independently.')

    def test_status_errors_empty_text_and_malformed_response(self):
        bodies=[None,[],{'output':[]}, {'status':'unknown'},
            {'status':'completed','output':[]}, {'status':'completed','output':None},
            {'status':'completed','output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':42}]}]},
            {'status':'completed','output':[{'type':'reasoning','text':'HIDDEN_REASONING'}]}]
        for body in bodies:
            # response(None) uses the legacy Chat fixture; it is also malformed for Agent.
            with self.subTest(body=body),patch('urllib.request.urlopen',return_value=response(body)):
                self.assertEqual(self.expert().ask('q','c')['error'],'expert_malformed_response')
        for status in ('failed','incomplete','cancelled','queued','in_progress','completed'):
            body=agent_body()
            body.update(status=status,error={'message':'PERPLEXITY_SECRET','code':'bad','type':'invalid_request'})
            with self.subTest(status=status),patch('urllib.request.urlopen',return_value=response(body)):
                result=self.expert().ask('q','c')
                self.assertEqual(result['error'],'expert_provider_failed')
                self.assertEqual(result['status'],status)
                self.assertEqual(result['completion_tokens'],40)
                self.assertNotIn('PERPLEXITY_SECRET',str(result))

    def test_timeout_http_and_bad_json_leave_worker_and_ceiling_usable(self):
        cases=[(TimeoutError('PERPLEXITY_SECRET'),'expert_timeout'),
            (urllib.error.URLError('PERPLEXITY_SECRET'),'expert_connection_error')]
        for status in (400,401,429,500,503):
            cases.append((urllib.error.HTTPError('url',status,'failed',{},io.BytesIO(b'PERPLEXITY_SECRET')),'expert_http_error'))
        for failure,expected in cases:
            with self.subTest(expected=expected),tempfile.TemporaryDirectory() as tmp,patch('urllib.request.urlopen',side_effect=failure) as endpoint:
                seen,result,error=run_script(Path(tmp),[init_turn(),done('verified'),ask(),ask(),
                    turn({'action':'shell','command':'continue'}),turn({'action':'finish','summary':'done'})],
                    extra=[*AGENT_EXTRA,'--expert-max-calls','1'])
                self.assertEqual(result,0,error)
                self.assertIn(expected,str(seen[3]))
                self.assertIn('expert_call_limit',str(seen[4]))
                self.assertIn('SHELL RESULT',str(seen[5]))
                self.assertEqual(endpoint.call_count,1)
                self.assertNotIn('PERPLEXITY_SECRET',str(seen))
        fake=MagicMock()
        fake.__enter__.return_value=io.StringIO('invalid json PERPLEXITY_SECRET')
        with patch('urllib.request.urlopen',return_value=fake):
            self.assertEqual(self.expert().ask('q','c')['error'],'expert_malformed_response')

    def test_worker_gui_state_map_usage_and_secrecy_isolation(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'PERPLEXITY_API_KEY':'PERPLEXITY_SECRET'}):
            root=Path(tmp)
            gui=MagicMock()
            gui.__enter__.return_value=gui
            gui.execute.return_value=({'state':'alive'},None)
            project_map=MagicMock()
            project_map.message.return_value={'role':'user','content':'PRIVATE_MAP'}
            snapshots=[]
            def endpoint(*args,**kwargs):
                gui.__exit__.assert_not_called()
                self.assertEqual(gui.execute.call_count,1)
                self.assertEqual(project_map.refresh.call_count,1)
                payload=json.loads(args[0].data)
                self.assertEqual(payload['instructions'],EXPERT_SYSTEM)
                self.assertEqual(payload['input'],[{'type':'message','role':'user','content':'QUESTION'},
                    {'type':'message','role':'user','content':'CONTEXT'}])
                self.assertNotIn('tools',payload)
                snapshots.append(StateStore(root/'state','task').get_project_state())
                body=agent_body()
                body['output'][1]['content'][0]['text']='ADVICE PERPLEXITY_SECRET'
                return response(body)
            def shell(work,command):
                self.assertEqual(command,'verify advice')
                gui.__exit__.assert_not_called()
                self.assertEqual(StateStore(root/'state','task').get_project_state(),snapshots[0])
                self.assertEqual(project_map.refresh.call_count,1)
            with patch('urllib.request.urlopen',side_effect=endpoint),patch('pavlusha_agent.runtime.GuiRuntime',return_value=gui), \
                 patch('pavlusha_agent.runtime.ProjectMap',return_value=project_map):
                seen,result,error=run_script(root,[init_turn(),done('verified'),
                    turn({'action':'gui_start','command':'app','network':False}),ask(),
                    turn({'action':'shell','command':'verify advice'}),turn({'action':'finish','summary':'done'})],
                    extra=[*AGENT_EXTRA,'--gui','--project-map','on'],shell_hook=shell)
            self.assertEqual(result,0,error)
            self.assertIn('EXPERT RESULT',str(seen[4]))
            self.assertIn('ADVICE [REDACTED]',str(seen[4]))
            self.assertNotIn('PROJECT CHECKPOINT REQUIRED',str(seen[5][1:]))
            self.assertEqual(gui.execute.call_count,1)
            self.assertEqual(gui.__exit__.call_count,1)
            telemetry=(root/'state/experiment.jsonl').read_text()
            self.assertIn('999999',telemetry)
            self.assertNotIn('ADVICE',telemetry)
            self.assertNotIn('CONTEXT',telemetry)
            files=''.join(p.read_text() for p in (root/'state').rglob('*') if p.is_file())
            self.assertNotIn('PERPLEXITY_SECRET',files+str(seen))
            self.assertNotIn('HIDDEN_REASONING',files+str(seen))
            self.assertNotIn('ADVICE',json.dumps(StateStore(root/'state','task').load()))

    def test_cold_recovery_does_not_replay(self):
        with tempfile.TemporaryDirectory() as tmp,patch('urllib.request.urlopen',return_value=response(agent_body())) as endpoint:
            root=Path(tmp)
            seen,result,error=run_script(root,[init_turn(),ask(),review('Continue verification.'),
                turn({'action':'shell','command':'verify'})],extra=[*AGENT_EXTRA,'--history-high','2','--max-steps','4'])
            self.assertIn('maximum of 4 steps',error)
            self.assertIn('EXPERT RESULT',(root/'state/history_archive.jsonl').read_text())
            seen,result,error=run_script(root,[done('verified'),turn({'action':'finish','summary':'done'})],extra=AGENT_EXTRA)
            self.assertEqual(result,0,error)
            self.assertEqual(endpoint.call_count,1)
            self.assertNotIn('EXPERT RESULT',str(seen[0]))

    def test_default_transport_and_disabled_behavior_unchanged(self):
        self.assertEqual(build_parser().parse_args([]).expert_transport,'chat-completions')
        with tempfile.TemporaryDirectory() as tmp,patch('urllib.request.urlopen') as endpoint:
            seen,result,error=run_script(Path(tmp),[init_turn(),done('verified'),turn({'action':'finish','summary':'done'})],
                extra=['--expert-transport','perplexity-agent'])
            self.assertEqual(result,0,error)
            self.assertNotIn('ask_expert',str(seen))
            endpoint.assert_not_called()

if __name__=='__main__':
    unittest.main()
