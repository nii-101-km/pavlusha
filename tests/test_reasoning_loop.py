"""Lexical detector, real SSE reader, and runtime recovery regressions."""
import copy
import io
import json
from pathlib import Path
import random
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from pavlusha_agent.cli import build_parser, main
from pavlusha_agent.core import AgentError
from pavlusha_agent.provider import ChatProvider, WorkerStreamInterrupted
from pavlusha_agent.reasoning_loop import ReasoningLoopDetector, RECOVERY_MESSAGE
from pavlusha_agent.runtime import run_agent
from pavlusha_agent.state_store import StateStore
from tests.test_reasoning_window import init_turn
from tests.test_checkpoint_snapshots import done

# Synthetic distinct-word cycle, deliberately unrelated to the GLM-OCR text.
LOOP = (' '.join(f'cycleword{i}' for i in range(240))+' ')*8
FINISH = {'action':'finish','summary':'done'}
SHELL = {'action':'shell','command':'verify','network':False}
FIXTURE = Path(__file__).parent/'fixtures/glm_ocr_reasoning.json'
QWEN_EXCERPT = Path(__file__).parent/'fixtures/qwen_lab5_reasoning_excerpt.txt'
QWEN_TRACE = Path(__file__).parent/'fixtures/qwen_lab5_step10_reasoning.txt'


def detect(text, chunk_size):
    detector=ReasoningLoopDetector()
    signals=[]
    for i in range(0,len(text),chunk_size):
        signals.extend(detector.feed(text[i:i+chunk_size]))
    signals.extend(detector.finish())
    return detector,signals


class DetectorTests(unittest.TestCase):
    def test_real_qwen_lab5_interrupted_generation_is_detected(self):
        # Full terminal-captured STEP 10, including its unique preamble and final
        # truncated sentence. No repetition was added to this fixture.
        text = QWEN_TRACE.read_text()
        self.assertEqual(len(re.findall(r'\w+', text)), 3953)
        detector, signals = detect(text,97)
        signal = detector.confirmation
        self.assertIsNotNone(signal)
        self.assertEqual(signal.word_count, 880)
        self.assertEqual(signal.window_start, 640)
        self.assertEqual(signal.matched_window_start, 120)
        self.assertEqual(signal.similarity, 1.0)
        self.assertEqual(signal.consecutive_matches, 2)
        scores = {s.word_count: s.similarity for s in signals}
        self.assertAlmostEqual(scores[720], 0.3522727272727273)
        self.assertAlmostEqual(scores[760], 0.45588235294117646)
        self.assertAlmostEqual(scores[800], 0.6391752577319587)
        self.assertEqual(scores[840], 1.0)
        for chunk_size in (1,7,511,len(text)):
            self.assertEqual(detect(text,chunk_size)[0].confirmation,signal)

    def test_qwen_lab5_quoted_excerpt_replay_and_minimum_lag(self):
        # Actual user-quoted text; the repetition count is reconstructed, not a
        # claim that an unavailable full streamed generation was captured.
        excerpt = QWEN_EXCERPT.read_text()
        self.assertEqual(len(re.findall(r'\w+', excerpt)), 62)
        short, signals = detect(excerpt * 11, 97)
        self.assertIsNone(short.confirmation)
        self.assertEqual(short.word_count, 682)
        self.assertTrue(all(s.matched_window_start is None for s in signals))
        one, signals = detect(excerpt * 12, 97)
        self.assertIsNone(one.confirmation)
        self.assertEqual(signals[-1].window_start, 480)
        self.assertEqual(signals[-1].matched_window_start, 0)
        self.assertEqual(signals[-1].similarity, 1.0)
        self.assertEqual(signals[-1].consecutive_matches, 1)
        decisions = [detect(excerpt * 13, n)[0].confirmation for n in (1, 7, 97, 511, 100000)]
        self.assertTrue(all(d == decisions[0] for d in decisions))
        self.assertEqual(decisions[0].word_count, 760)
        self.assertEqual(decisions[0].window_start, 520)
        self.assertEqual(decisions[0].matched_window_start, 0)
        self.assertEqual(decisions[0].similarity, 1.0)
        self.assertEqual(decisions[0].consecutive_matches, 2)

    def test_long_real_reasoning_and_a_single_lab5_excerpt_are_not_a_cycle(self):
        # Real OCR/application reasoning reuses page, model, code and condition
        # vocabulary extensively while progressing through distinct decisions.
        text = json.loads(FIXTURE.read_text())['turns']['38']
        self.assertGreater(len(re.findall(r'\w+', text)), 7500)
        self.assertIsNone(detect(text + '\n' + QWEN_EXCERPT.read_text(),97)[0].confirmation)

    def test_repeated_cycle_requires_two_lagged_matches(self):
        detector,signals=detect(LOOP,17)
        self.assertIsNotNone(detector.confirmation)
        first_match=next(s for s in signals if s.consecutive_matches)
        self.assertFalse(first_match.confirmed)
        self.assertEqual(detector.confirmation.consecutive_matches,2)
        self.assertGreaterEqual(detector.confirmation.repetition_distance,480)
        self.assertEqual(detector.confirmation.word_count,760)
        self.assertEqual(len(signals),(760-240)//40+1)

    def test_long_nonrepetitive_and_common_terms_are_not_loops(self):
        rng=random.Random(42)
        texts=[' '.join(f'item{i}' for i in range(10000)),
               ' '.join(rng.choice(['state','file','test','worker','project','next','inspect','run',
                                    'result','model','action','check','need','then','now','with'])
                        for _ in range(10000))]
        for text in texts:
            with self.subTest(chars=len(text)):
                self.assertIsNone(detect(text,101)[0].confirmation)

    def test_chunk_boundaries_case_and_unicode(self):
        text=((' '.join(f'Тест_{i}' for i in range(240))+' ')*6).upper()
        decisions=[detect(text,size)[0].confirmation for size in (1,2,7,40,511,len(text))]
        self.assertIsNotNone(decisions[0])
        self.assertTrue(all(x==decisions[0] for x in decisions))
        self.assertEqual(decisions[0],detect(text.lower(),23)[0].confirmation)

    def test_pending_word_is_not_counted_or_retokenized(self):
        d=ReasoningLoopDetector()
        for _ in range(1000):
            self.assertEqual(d.feed('a'),[])
        self.assertEqual(d.word_count,0)
        d.feed(' ')
        self.assertEqual(d.word_count,1)
        self.assertEqual(d._words[0],'a'*1000)

    def test_known_attractor_and_all_other_real_turns_streamed(self):
        turns=json.loads(FIXTURE.read_text())['turns']
        self.assertEqual(len(turns),80)
        detected={}
        for step,text in turns.items():
            detector,_=detect(text,83)
            if detector.confirmation:
                detected[step]=detector.confirmation
        self.assertEqual(set(detected),{'37'})
        known=detected['37']
        self.assertEqual(known.word_count,1160)
        self.assertEqual(known.window_start,920)
        self.assertEqual(known.matched_window_start,400)
        self.assertLess(known.word_count,len(re.findall(r'\w+',turns['37']))//2)
        for size in (1,7,127,1024,len(turns['37'])):
            self.assertEqual(detect(turns['37'],size)[0].confirmation,known)


class TrackingResponse(io.BytesIO):
    def __init__(self,data):
        super().__init__(data)
        self.lines_read=0
    def __next__(self):
        line=super().__next__()
        self.lines_read+=1
        return line


def script(action=FINISH, reasoning='NORMAL_REASONING', *, early_content='', early_tools=None, prompt_tokens=200):
    return dict(action=action,reasoning=reasoning,early_content=early_content,early_tools=early_tools,prompt_tokens=prompt_tokens)


def wire_response(item,stream):
    content=json.dumps(item['action']) if isinstance(item['action'],dict) else item['action']
    usage={'prompt_tokens':item.get('prompt_tokens',200),'completion_tokens':100,'completion_tokens_details':{'reasoning_tokens':50}}
    if not stream:
        return TrackingResponse(json.dumps({'choices':[{'message':{'content':content,
            'reasoning_content':item['reasoning']},'finish_reason':'stop'}],'usage':usage}).encode())
    deltas=[]
    if item['early_content'] or item['early_tools']:
        deltas.append({'content':item['early_content'],'tool_calls':item['early_tools'] or []})
    deltas.extend({'reasoning_content':item['reasoning'][i:i+97]}
                  for i in range(0,len(item['reasoning']),97))
    deltas.append({'content':content})
    chunks=[{'choices':[{'delta':d,'finish_reason':None}]} for d in deltas]
    chunks.append({'choices':[{'delta':{},'finish_reason':'stop'}],'usage':usage})
    data=''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n'
    return TrackingResponse(data.encode())


class ProviderInterruptionTests(unittest.TestCase):
    def test_real_sse_reader_closes_before_returning_partial_diagnostics(self):
        response=wire_response(script(reasoning=LOOP,early_content='{"action":"shell"',
            early_tools=[{'id':'partial-tool'}]),True)
        expected_lines=response.getvalue().count(b'\n')
        detector=ReasoningLoopDetector()
        def observer(kind,text):
            if kind=='reasoning' and any(s.confirmed for s in detector.feed(text)):
                return False
        provider=ChatProvider('http://test/v1','model','',5,0,1024)
        with patch('urllib.request.urlopen',return_value=response):
            with self.assertRaises(WorkerStreamInterrupted) as caught:
                provider.worker_completion([{'role':'user','content':'task'}],on_delta=observer)
        self.assertTrue(response.closed)
        self.assertLess(response.lines_read,expected_lines//2)
        self.assertEqual(caught.exception.turn.content,'{"action":"shell"')
        self.assertEqual(caught.exception.turn.tool_calls,[{'id':'partial-tool'}])
        self.assertTrue(LOOP.startswith(caught.exception.turn.reasoning_content))
        self.assertIsNone(caught.exception.turn.reasoning_tokens)

    def test_observer_none_preserves_full_response_and_usage(self):
        item=script(reasoning=LOOP)
        response=wire_response(item,True)
        provider=ChatProvider('http://test/v1','model','',5,0,1024)
        detector=ReasoningLoopDetector()
        def observer(kind,text):
            if kind=='reasoning':
                detector.feed(text)
        with patch('urllib.request.urlopen',return_value=response):
            result=provider.worker_completion([{'role':'user','content':'task'}],on_delta=observer)
        self.assertTrue(response.closed)
        self.assertEqual(result.reasoning_content,LOOP)
        self.assertEqual(json.loads(result.content),FINISH)
        self.assertEqual(result.reasoning_tokens,50)
        self.assertIsNotNone(detector.confirmation)

    def test_network_failure_does_not_become_loop_recovery(self):
        response=wire_response(script(),True)
        class Broken(TrackingResponse):
            def __next__(self):
                raise OSError('lost stream')
        response=Broken(response.getvalue())
        provider=ChatProvider('http://test/v1','model','',5,0,1024)
        with patch('urllib.request.urlopen',return_value=response), self.assertRaisesRegex(AgentError,'lost stream'):
            provider.worker_completion([],on_delta=lambda *args:None)
        self.assertTrue(response.closed)


class RuntimeRecoveryTests(unittest.TestCase):
    def seed(self,root):
        (root/'work').mkdir()
        (root/'work/keep.txt').write_text('persistent work')
        store=StateStore(root/'state','task')
        store.initialize_project(json.loads(init_turn().content),step=1)
        store.update_project(json.loads(done('verified').content)['changes'],step=2)
        store.complete_project_review(step=3,handoff='SEED_HANDOFF')
        return store

    def run_case(self,root,replies,*,mode='recover',extra=(),seed=True):
        store=self.seed(root) if seed else StateStore(root/'state','task')
        before=store.load()
        requests=[]; responses=[]; commands=[]; maps=[]; output=io.StringIO()
        replies=iter(replies)
        class Map:
            def __init__(self,*args,**kwargs): pass
            def refresh(self):
                maps.append('refresh')
            def message(self): return {'role':'user','content':'PROJECT MAP frozen sentinel'}
        def send(request,**kwargs):
            if responses:
                self.assertTrue(responses[-1].closed,'previous stream still open at retry')
            payload=json.loads(request.data); requests.append(payload)
            result=wire_response(next(replies),payload['stream']); responses.append(result)
            return result
        def shell(work,command,**kwargs):
            from pavlusha_agent.core import ShellResult
            commands.append(command)
            return ShellResult(command,False,0,False,'verified','',0.01)
        args=build_parser().parse_args(['--no-interactive', '--no-live', '--no-network', '--project-map', 'off', '--model','model','--workdir',str(root/'work'),
            '--state-dir',str(root/'state'),'--worker-context-budget','100000',
            '--history-high','99','--project-review-every','0','--max-steps','50',
            '--project-map','on',*(['--reasoning-loop-recovery',mode] if mode is not None else []),
            *extra,'task'])
        with patch('pavlusha_agent.runtime.shutil.which',return_value='/fake/bwrap'), \
             patch('pavlusha_agent.runtime.ProjectMap',Map), \
             patch('urllib.request.urlopen',side_effect=send), \
             patch('pavlusha_agent.runtime.run_shell',side_effect=shell), \
             redirect_stderr(output),redirect_stdout(output):
            error=None
            try:
                result=run_agent(args)
            except AgentError as exc:
                result=None; error=str(exc)
        events=[json.loads(line) for line in (root/'state/experiment.jsonl').read_text().splitlines()]
        return dict(requests=requests,responses=responses,commands=commands,maps=maps,
                    before=before,state=store.load(),events=events,result=result,error=error,output=output.getvalue())

    def test_off_and_observe_preserve_actions_history_and_handoff(self):
        for mode in ('off','observe'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                case=self.run_case(Path(tmp),[script(SHELL,LOOP),script()],mode=mode)
                self.assertEqual(case['result'],0,case['error'])
                self.assertEqual(case['commands'],['verify'])
                self.assertIn(LOOP,str(case['requests'][1]['messages']).replace('\\n','\n'))
                loops=[e for e in case['events'] if e['kind']=='reasoning_loop']
                self.assertEqual(len(loops),int(mode=='observe'))
                if loops: self.assertFalse(loops[0]['interrupted'])
                self.assertEqual(case['requests'][0]['stream'],mode=='observe')
                self.assertEqual(case['maps'],['refresh'])
                self.assertEqual(case['state']['checkpoint_handoff'],'SEED_HANDOFF')
                self.assertNotIn('REASONING RECOVERY',str(case['requests']))

    def test_real_qwen_trace_replay_off_observe_and_recover(self):
        reasoning = QWEN_TRACE.read_text()
        for mode in ('off', 'observe', 'recover'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                replies = ([script(SHELL, reasoning), script()] if mode != 'recover'
                           else [script({'action':'shell','command':'NEVER_EXECUTE'},reasoning),
                                 script(SHELL),script()])
                case = self.run_case(Path(tmp), replies, mode=mode)
                self.assertEqual(case['result'], 0, case['error'])
                self.assertEqual(case['commands'], ['verify'])
                loops = [e for e in case['events'] if e['kind'] == 'reasoning_loop']
                self.assertEqual(len(loops), int(mode != 'off'))
                if loops:
                    self.assertEqual(loops[0]['word_count'], 880)
                    self.assertEqual(loops[0]['similarity'], 1.0)
                    self.assertEqual(loops[0]['interrupted'], mode == 'recover')
                self.assertEqual(case['before']['project_state'], case['state']['project_state'])

    def test_default_recovers_real_qwen_trace_without_explicit_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = self.run_case(Path(tmp),
                [script({'action':'shell','command':'NEVER_EXECUTE'}, QWEN_TRACE.read_text()),
                 script(SHELL), script()], mode=None)
            self.assertEqual(case['result'], 0, case['error'])
            self.assertEqual(case['commands'], ['verify'])
            loops = [e for e in case['events'] if e['kind'] == 'reasoning_loop']
            self.assertEqual(len(loops), 1)
            self.assertEqual(loops[0]['word_count'], 880)
            self.assertEqual(loops[0]['similarity'], 1.0)
            self.assertTrue(loops[0]['interrupted'])
            self.assertEqual(case['before']['project_state'], case['state']['project_state'])

    def test_explicit_off_does_not_create_detector(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch('pavlusha_agent.runtime.ReasoningLoopDetector') as detector:
            case = self.run_case(Path(tmp), [script(SHELL, QWEN_TRACE.read_text()), script()], mode='off')
            self.assertEqual(case['result'], 0, case['error'])
            self.assertEqual(case['commands'], ['verify'])
            detector.assert_not_called()

    def test_recover_keeps_preloop_context_and_resets_on_accepted_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            case=self.run_case(root,[script(SHELL,'VALID_PRIOR_REASONING'),
                script({'action':'shell','command':'NEVER_EXECUTE'},LOOP,
                       early_content=json.dumps({'action':'shell','command':'NEVER_EXECUTE'})),
                script(SHELL),script(reasoning=LOOP),script()],
                extra=['--max-reasoning-loop-recoveries','1','--live'])
            self.assertEqual(case['result'],0,case['error'])
            self.assertEqual(case['commands'],['verify','verify'])
            self.assertEqual(case['maps'],['refresh'])
            requests=case['requests']
            for before,after in ((1,2),(3,4)):
                self.assertEqual(requests[after]['messages'],requests[before]['messages']+[RECOVERY_MESSAGE])
                self.assertNotIn('cycleword',str(requests[after]['messages']))
                self.assertIn('VALID_PRIOR_REASONING',str(requests[after]['messages']))
            self.assertNotIn('REASONING RECOVERY',str(requests[3]['messages']))
            loops=[e for e in case['events'] if e['kind']=='reasoning_loop']
            self.assertEqual([e['recovery_attempt'] for e in loops],[0,0])
            self.assertTrue(all(e['interrupted'] for e in loops))
            self.assertIn('NEVER_EXECUTE',loops[0]['interrupted_content'])
            self.assertTrue(all(LOOP.startswith(e['interrupted_reasoning']) for e in loops))
            self.assertIn('REASONING LOOP',case['output'])
            self.assertIn('repetition suspected',case['output'])
            self.assertEqual(case['before']['project_state'],case['state']['project_state'])
            self.assertEqual(case['state']['checkpoint_handoff'],'SEED_HANDOFF')
            self.assertFalse((root/'state/history_archive.jsonl').exists())

    def test_exhaustion_preserves_state_files_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            case=self.run_case(root,[script(reasoning=LOOP)]*4,extra=['--max-steps','-1'])
            self.assertIn('3 consecutive recovery attempts',case['error'])
            self.assertEqual(case['state'],case['before'])
            self.assertEqual(case['commands'],[])
            self.assertEqual(case['maps'],['refresh'])
            self.assertEqual((root/'work/keep.txt').read_text(),'persistent work')
            self.assertFalse((root/'state/history_archive.jsonl').exists())
            loops=[e for e in case['events'] if e['kind']=='reasoning_loop']
            self.assertEqual([e['recovery_attempt'] for e in loops],[0,1,2,3])
            self.assertTrue(loops[-1]['exhausted'])
            base=case['requests'][0]['messages']
            for request in case['requests'][1:]:
                self.assertEqual(request['messages'],base+[RECOVERY_MESSAGE])
            restarted=self.run_case(root,[script()],seed=False)
            self.assertEqual(restarted['result'],0,restarted['error'])
            self.assertIn('SEED_HANDOFF',str(restarted['requests'][0]))
            self.assertNotIn('REASONING RECOVERY',str(restarted['requests'][0]))

    def test_invalid_and_rejected_actions_do_not_reset_episode(self):
        for action in ('bad JSON',{'action':'project_review_complete'},
                       {'action':'project_update','changes':[{'op':'update_work','id':'W999','status':'DONE','evidence':['FILE: keep.txt']}]}):
            with self.subTest(action=action),tempfile.TemporaryDirectory() as tmp:
                case=self.run_case(Path(tmp),[script(reasoning=LOOP),script(action),script(reasoning=LOOP)],
                                   extra=['--max-reasoning-loop-recoveries','1'])
                self.assertIn('1 consecutive recovery attempts',case['error'])
                self.assertEqual(case['state'],case['before'])

    def test_high_context_high_and_periodic_reviews_keep_their_own_lifecycle(self):
        for extra,reasoning,action,refreshes in [
            # Map refreshes at startup and accepted completion, never HIGH entry.
            (['--history-high','1'],'normal',{'action':'project_review_complete','handoff':'NEW_HANDOFF'},2),
            (['--history-context-high','0.2'],'x'*14000,{'action':'project_review_complete','handoff':'NEW_HANDOFF'},2),
            (['--project-review-every','1'],'normal',{'action':'project_review_skip'},1)]:
            with self.subTest(extra=extra),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                case=self.run_case(root,[script(SHELL,reasoning,prompt_tokens=20000 if '--history-context-high' in extra else 200),script(reasoning=LOOP),script(action),script()],extra=extra)
                self.assertEqual(case['result'],0,case['error'])
                self.assertEqual(len(case['maps']),refreshes)
                self.assertEqual(case['requests'][2]['messages'],case['requests'][1]['messages']+[RECOVERY_MESSAGE])
                self.assertNotIn('cycleword',str(case['requests'][3]['messages']))
                periodic=action['action']=='project_review_skip'
                self.assertEqual(case['state']['checkpoint_handoff'],'SEED_HANDOFF' if periodic else 'NEW_HANDOFF')
                if periodic:
                    self.assertIn('PERIODIC PROJECT STATE REVIEW',case['requests'][1]['messages'][-1]['content'])
                    self.assertFalse((root/'state/history_archive.jsonl').exists())
                else:
                    archive=(root/'state/history_archive.jsonl').read_text()
                    self.assertNotIn('cycleword',archive)
                    self.assertNotIn('REASONING RECOVERY',archive)
                    self.assertIn('NEW_HANDOFF',str(case['requests'][3]['messages']))

    def test_long_or_empty_action_without_repetition_does_not_trigger_loop_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            # This completed no-action turn follows the pre-existing bounded empty-response
            # continuation path. Length and absence of action never drive the new detector.
            reasoning=' '.join(f'unique{i}' for i in range(4000))
            case=self.run_case(Path(tmp),[script('',reasoning),script()])
            self.assertEqual(case['result'],0,case['error'])
            self.assertFalse(any(e['kind']=='reasoning_loop' for e in case['events']))
            self.assertIn('previous worker attempt produced no final action',str(case['requests'][1]))
            self.assertNotIn('REASONING RECOVERY',str(case['requests'][1]))
            self.assertIn(reasoning,str(case['requests'][1]))

    def test_off_live_and_observe_prompts_match_off_nonlive(self):
        outcomes=[]
        for mode,extra in [('off',[]),('off',['--live']),('observe',['--live'])]:
            with tempfile.TemporaryDirectory() as tmp:
                case=self.run_case(Path(tmp),[script(SHELL,LOOP),script()],mode=mode,extra=extra)
                self.assertEqual(case['result'],0,case['error'])
                outcomes.append(case)
        for case in outcomes[1:]:
            self.assertEqual([r['messages'] for r in outcomes[0]['requests']],
                             [r['messages'] for r in case['requests']])
        self.assertNotIn('REASONING LOOP',outcomes[1]['output'])
        self.assertIn('REASONING LOOP',outcomes[2]['output'])

    def test_cli_defaults_and_validation(self):
        args=build_parser().parse_args(['task'])
        self.assertEqual(args.reasoning_loop_recovery,'recover')
        for interactive in ([], ['--interactive']):
            self.assertEqual(build_parser().parse_args([*interactive,'task']).reasoning_loop_recovery,'recover')
            for mode in ('off', 'observe', 'recover'):
                self.assertEqual(build_parser().parse_args(
                    [*interactive,'--reasoning-loop-recovery',mode,'task']).reasoning_loop_recovery,mode)
        self.assertEqual(args.max_reasoning_loop_recoveries,3)
        for limit in ('0','-1'):
            with patch('sys.argv',['agent.py','--max-reasoning-loop-recoveries',limit,'task']), \
                 patch('pavlusha_agent.cli.run_agent') as run,redirect_stderr(io.StringIO()):
                self.assertEqual(main(),2)
                run.assert_not_called()


class HTTPStreamingRecoveryTests(unittest.TestCase):
    def test_actual_http_stream_closed_and_retried_before_action_admission(self):
        import http.server
        import threading
        import urllib.request
        requests=[]; responses=[]; retried=threading.Event(); first_done=threading.Event()
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args): pass
            def do_POST(self):
                payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                index=len(requests); requests.append(payload)
                if index:
                    retried.set()
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Connection','close')
                self.end_headers()
                self.close_connection=True
                def send(delta):
                    self.wfile.write(('data: '+json.dumps({'choices':[{'delta':delta}]})+'\n\n').encode())
                    self.wfile.flush()
                try:
                    if index==0:
                        # Even a valid JSON action already received must never be executed
                        # when this generation is subsequently interrupted for repetition.
                        send({'content':json.dumps(SHELL)})
                        for i in range(0,len(LOOP),97):
                            send({'reasoning_content':LOOP[i:i+97]})
                        if not retried.wait(5):
                            return
                        first_done.set()
                    else:
                        send({'reasoning_content':'Normal continuation.'})
                        send({'content':json.dumps(FINISH)})
                    self.wfile.write(b'data: [DONE]\n\n')
                    self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError):
                    pass
        try:
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        except PermissionError as exc:
            self.skipTest(f'environment does not permit loopback HTTP server: {exc}')
        server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        original_urlopen=urllib.request.urlopen
        def send(request,**kwargs):
            if responses:
                self.assertTrue(responses[-1].closed,'actual HTTP response not closed before retry')
                self.assertFalse(first_done.is_set(),'retry waited for completion instead of interrupting')
            result=original_urlopen(request,**kwargs)
            responses.append(result)
            return result
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                RuntimeRecoveryTests().seed(root)
                args=build_parser().parse_args(['--no-interactive', '--no-live', '--no-network', '--project-map', 'off', '--base-url',f'http://127.0.0.1:{server.server_port}/v1',
                    '--model','local-sse-fixture','--workdir',str(root/'work'),'--state-dir',str(root/'state'),
                    '--worker-context-budget','100000','--max-steps','1','--project-review-every','0',
                    '--reasoning-loop-recovery','recover','task'])
                with patch('pavlusha_agent.runtime.shutil.which',return_value='/fake/bwrap'), \
                     patch('urllib.request.urlopen',side_effect=send), \
                     patch('pavlusha_agent.runtime.run_shell',side_effect=AssertionError('interrupted action executed')), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(run_agent(args),0)
                self.assertEqual(len(requests),2)
                self.assertTrue(all(r['stream'] for r in requests))
                self.assertEqual(requests[1]['messages'],requests[0]['messages']+[RECOVERY_MESSAGE])
                self.assertTrue(all(r.closed for r in responses))
                events=[json.loads(x) for x in (root/'state/experiment.jsonl').read_text().splitlines()]
                loop=next(e for e in events if e['kind']=='reasoning_loop')
                self.assertTrue(loop['interrupted'])
                self.assertEqual(loop['interrupted_content'],json.dumps(SHELL))
                self.assertTrue(LOOP.startswith(loop['interrupted_reasoning']))
                self.assertLess(len(loop['interrupted_reasoning']),len(LOOP))
        finally:
            retried.set()
            server.shutdown(); server.server_close(); thread.join(5)
