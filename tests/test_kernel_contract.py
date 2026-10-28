"""Transition, observation and execution boundaries of the unified module API."""
import base64
from contextlib import contextmanager
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent.adapters import ChatProvider
from pavlusha_agent.basic_host import BasicHost
from pavlusha_agent.core import AgentError, ProcessCleanupError, ShellResult
from pavlusha_agent.extensions import (Contribution, Control, Event, EventHost, EventReply,
    ExecutionPolicy, Observation, Outcome, Tool)
from pavlusha_agent.runtime import Runtime
from tests.test_events import Provider, Probe, reply
from tests.test_project_state_module import initialize, done, finish
from pavlusha_modules.project_state import ProjectState
from pavlusha_modules.shell import Shell

PNG = 'data:image/png;base64,' + base64.b64encode(
    base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZ0AAAAASUVORK5CYII=')).decode()


def tool(invoke, *, policy=ExecutionPolicy()):
    return Tool('work', 'Do work', {'type':'object','properties':{},'required':[],
        'additionalProperties':False}, invoke, policy)


def module(spec, *, observation=None):
    return Probe('worker', lambda e: Contribution(tools=(spec,),
        observations=(observation,) if observation else ()) if e.kind == 'session_started' else None)


class KernelContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def runtime(self, provider, modules, **options):
        host = Shell(self.root/'work') if any(isinstance(m, ProjectState) for m in modules) else BasicHost(self.root/'work')
        return Runtime(provider, task='task',session_dir=self.root/'session',
            extensions=[host, *modules], **options)

    def state(self):
        return json.loads((self.root/'session/session.json').read_text())

    def test_invocation_context_is_private_current_response_without_runtime(self):
        seen = []
        def invoke(args, context):
            seen.append((context.turn_id, context.context_epoch, context.response['reasoning_content']))
            self.assertFalse(hasattr(context, 'provider'))
            context.response['reasoning_content'] = 'mutated'
            return Outcome({'ok':True})
        runtime = self.runtime(Provider([reply({'action':'work'}),finish()]), [module(tool(invoke))])
        runtime.run()
        self.assertEqual(seen, [(1,0,'Observed evidence')])
        journal = (self.root/'session/events.jsonl').read_text()
        self.assertNotIn('mutated', journal)

    def test_controls_do_not_replace_module_contribution(self):
        def hook(e):
            if e.kind == 'session_started': return Contribution(prefix={'keep':'durable prefix'})
            if e.kind == 'turn_completed' and e.turn_id == 1:
                return EventReply(controls=(Control('reset','test'),))
        runtime = self.runtime(Provider([reply({'action':'shell','command':'true'}),finish()]), [Probe(hook=hook)])
        # Avoid starting a real sandbox for this contract test.
        runtime.host.extensions[0].shell_runner = lambda *a,**kw: ShellResult('true',False,0,False,'','',0)
        runtime.run()
        self.assertEqual(self.state()['context_epoch'],1)
        self.assertIn('durable prefix', str(runtime.provider.requests[1]))

    def test_module_can_stop_without_claiming_success_or_executing_a_tool(self):
        probe = Probe(hook=lambda e: EventReply(controls=(Control('stop','loop detected'),))
                      if e.kind == 'prepare_turn' else None)
        runtime = self.runtime(Provider([]), [probe])
        self.assertEqual(runtime.run(),'loop detected')
        self.assertEqual(self.state()['status'],'stopped')
        self.assertEqual(runtime.provider.requests,[])
        self.assertEqual(probe.events[-1].data['reason'],'stopped')

    def test_pause_is_durable_and_requires_input_then_resumes_without_replaying(self):
        effects = []
        def invoke(args, context):
            effects.append(1)
            return Outcome({'question':'Which color?'}, controls=(Control('pause','Which color?'),))
        runtime = self.runtime(Provider([reply({'action':'work'})]), [module(tool(invoke))])
        self.assertEqual(runtime.run(),'Which color?')
        self.assertEqual(self.state()['status'],'paused')
        with self.assertRaisesRegex(AgentError,'requires user_input'):
            self.runtime(Provider([]), [])
        provider = Provider([finish()])
        self.assertEqual(self.runtime(provider, [], user_input='blue').run(),'done')
        self.assertIn('USER REPLY:\\nblue'.replace('\\n','\n'), str(provider.requests[0]['messages']).replace('\\n','\n'))
        self.assertEqual(effects,[1])
        self.assertNotIn('awaiting_input',self.state())

    def test_user_reply_survives_state_recovery_reset(self):
        def invoke(args, context): return Outcome({}, controls=(Control('pause','Choose'),))
        provider = Provider([initialize(),reply({'action':'work'})])
        first = self.runtime(provider,[ProjectState(review_every=0),module(tool(invoke))])
        self.assertEqual(first.run(),'Choose')
        second_provider = Provider([done(),finish()])
        second = self.runtime(second_provider,[ProjectState(review_every=0)],user_input='blue')
        second.run()
        self.assertIn('blue',str(second_provider.requests[0]['messages']))

    def test_real_sse_interrupt_closes_stream_and_does_not_execute_partial_action(self):
        chunks = [{'choices':[{'delta':{'reasoning_content':'loop'}}]},
                  {'choices':[{'delta':{'content':'{"action":"work"}'},'finish_reason':'stop'}]}]
        stream = io.BytesIO((''.join('data: '+json.dumps(c)+'\n\n' for c in chunks)+'data: [DONE]\n\n').encode())
        effects = []
        probe = Probe(hook=lambda e: EventReply(controls=(Control('interrupt','repetition'),
                                                       Control('stop','halt'),))
                      if e.kind == 'reasoning_fragment' else None)
        provider = ChatProvider('http://local.invalid/v1','test','',10,.1,100,backend='chat-completions')
        runtime = self.runtime(provider,[probe,module(tool(lambda a,c:effects.append(1) or Outcome({})))])
        with patch('urllib.request.urlopen',return_value=stream):
            self.assertEqual(runtime.run(),'halt')
        self.assertTrue(stream.closed)
        self.assertEqual(effects,[])
        turn = next(e.data['turn'] for e in probe.events if e.kind == 'turn_completed')
        self.assertEqual(turn['status'],'generation_interrupted')
        self.assertIsNone(turn['action'])
        self.assertEqual(turn['response']['reasoning_content'],'loop')

    def test_one_request_observation_visible_and_consumed_during_exclusive(self):
        def exclusive(e):
            if e.kind == 'session_started':
                return Contribution(exclusive=True, tools=(tool(lambda a,c:Outcome({})),))
            if e.kind == 'turn_completed': return Contribution()
        visual = Probe('visual',lambda e:Contribution(observations=(Observation(image=PNG),))
                       if e.kind == 'session_started' else None)
        provider = Provider([reply({'action':'work'}),reply({'action':'shell','command':'true'}),finish()])
        runtime = self.runtime(provider,[Probe('review',exclusive),visual])
        runtime.host.extensions[0].shell_runner = lambda *a,**kw: ShellResult('true',False,0,False,'','',0)
        runtime.run()
        self.assertIn(PNG,str(provider.requests[0]))
        self.assertNotIn(PNG,str(provider.requests[1]))
        self.assertNotIn(PNG,str(provider.requests[2]))

    def test_tool_observation_is_ephemeral_and_current_observation_is_replaceable(self):
        provider = Provider([reply({'action':'work'}),reply({'action':'work'}),finish()])
        runtime = self.runtime(provider,[module(tool(lambda a,c:Outcome({},observations=(Observation(image=PNG),))))])
        runtime.run()
        self.assertNotIn(PNG,str(provider.requests[0]))
        self.assertIn(PNG,str(provider.requests[1]))
        self.assertIn(PNG,str(provider.requests[2]))
        self.assertNotIn(PNG,str(self.state()['history']))

    def test_observation_until_replaced_persists_across_requests(self):
        visual = Probe('visual',lambda e:Contribution(observations=(Observation(text='current image', image=PNG,
                       lifetime='until_replaced'),)) if e.kind == 'session_started' else
                       Contribution() if e.kind == 'turn_completed' and e.turn_id == 2 else None)
        provider = Provider([reply({'action':'work'}),reply({'action':'work'}),finish()])
        self.runtime(provider,[visual,module(tool(lambda a,c:Outcome({})))]).run()
        self.assertIn(PNG,str(provider.requests[0]))
        self.assertIn(PNG,str(provider.requests[1]))
        self.assertNotIn(PNG,str(provider.requests[2]))

    def test_invalid_visual_payload_is_rejected(self):
        for image in ['file:///secret','https://example.org/a.png','data:image/png;base64,eA==']:
            with self.subTest(image=image):
                host = EventHost([Probe(hook=lambda e:Contribution(observations=(Observation(image=image),)))])
                with self.assertRaises(AgentError):
                    host.emit(Event('session_started','s',0,0))

    def test_release_saves_result_before_guard_and_restore(self):
        sequence = []
        @contextmanager
        def released_worker(*,restore_guard):
            sequence.append('release')
            try: yield
            finally:
                restore_guard()
                sequence.append('restore')
        def guard():
            self.assertEqual(self.state()['turn_id'],1)
            self.assertIsNone(self.state()['pending'])
            sequence.append('guard')
        def invoke(a,c):
            sequence.append('invoke')
            return Outcome({'ok':True})
        provider = Provider([reply({'action':'work'}),finish()])
        provider.released_worker = released_worker
        self.runtime(provider,[module(tool(invoke,policy=ExecutionPolicy(True,guard)))]).run()
        self.assertEqual(sequence,['release','invoke','guard','restore'])

    def test_failed_restore_stops_without_repeating_saved_effect(self):
        effects=[]
        @contextmanager
        def released_worker(*,restore_guard):
            yield
            raise AgentError('restore failed')
        provider = Provider([reply({'action':'work'})]);provider.released_worker=released_worker
        runtime = self.runtime(provider,[module(tool(lambda a,c:effects.append(1) or Outcome({}),
                                                     policy=ExecutionPolicy(True)))])
        with self.assertRaisesRegex(AgentError,'restore failed'):runtime.run()
        self.runtime(Provider([finish()]),[]).run()
        self.assertEqual(effects,[1])

    def test_failed_release_never_invokes_and_does_not_exit_unentered_manager(self):
        effects=[]
        @contextmanager
        def released_worker(*,restore_guard):
            raise AgentError('cannot release')
            yield
        provider = Provider([reply({'action':'work'}),finish()]);provider.released_worker=released_worker
        runtime = self.runtime(provider,[module(tool(lambda a,c:effects.append(1) or Outcome({}),
                                                     policy=ExecutionPolicy(True)))])
        self.assertEqual(runtime.run(),'done')
        self.assertEqual(effects,[])
        self.assertIn('cannot release',str(provider.requests[1]))

    def test_uncertain_process_cleanup_keeps_model_unloaded_and_pending(self):
        sequence=[]
        @contextmanager
        def released_worker(*,restore_guard):
            sequence.append('release')
            try: yield
            except ProcessCleanupError: raise
            else: sequence.append('restore')
        def invoke(a,c):raise ProcessCleanupError('uncertain')
        provider=Provider([reply({'action':'work'})]);provider.released_worker=released_worker
        runtime=self.runtime(provider,[module(tool(invoke,policy=ExecutionPolicy(True)))])
        with self.assertRaises(ProcessCleanupError):runtime.run()
        self.assertEqual(sequence,['release'])
        self.assertIsNotNone(self.state()['pending'])

    def test_request_snapshot_keeps_execution_policy_and_observation_owner(self):
        calls=[]
        @contextmanager
        def released_worker(*,restore_guard):
            calls.append('release'); yield; calls.append('restore')
        original=tool(lambda a,c:Outcome({},observations=(Observation(text='original tool image',image=PNG),)),
                      policy=ExecutionPolicy(True))
        provider=Provider([reply({'action':'work'}),finish()]);provider.released_worker=released_worker
        runtime=self.runtime(provider,[module(original)])
        def mutate(messages,options):
            runtime.host.contributions['worker']=Contribution()
        provider.hook=mutate
        runtime.run()
        self.assertEqual(calls,['release','restore'])
        self.assertIn(PNG,str(provider.requests[1]))

    def test_interrupt_can_continue_with_next_turn_without_executing_partial_response(self):
        effects=[]
        class Stream(Provider):
            def worker_completion(self,messages,**options):
                response=super().worker_completion(messages,**options)
                options['on_delta']('reasoning','repeat')
                return response  # Even if provider ignores False, Runtime must discard action.
        observer=Probe(hook=lambda e:EventReply(controls=(Control('interrupt','repeat'),))
                       if e.kind=='reasoning_fragment' and e.turn_id==1 else None)
        provider=Stream([reply({'action':'work'}),finish()])
        runtime=self.runtime(provider,[observer,module(tool(lambda a,c:effects.append(1) or Outcome({})))])
        self.assertEqual(runtime.run(),'done')
        self.assertEqual(effects,[])
        self.assertIn('generation_interrupted',str(provider.requests[1]))

    def test_oversized_outcome_cannot_pause_stop_or_publish_images(self):
        provider=Provider([reply({'action':'work'}),finish()])
        runtime=self.runtime(provider,[module(tool(lambda a,c:Outcome({'large':'x'*3000},
            controls=(Control('pause','Question'),), observations=(Observation(image=PNG),))))], output_limit=1000)
        self.assertEqual(runtime.run(),'done')
        self.assertNotIn(PNG,str(provider.requests[1]))
        self.assertNotIn('awaiting_input',self.state())
        self.assertIn('output_limit_exceeded',str(provider.requests[1]))

    def test_stop_requested_before_reset_preserves_history(self):
        probe=Probe(hook=lambda e:EventReply(controls=(Control('stop','preserve for inspection'),))
                    if e.kind=='before_reset' else None)
        runtime=self.runtime(Provider([reply({'action':'work'})]),[probe,module(tool(lambda a,c:Outcome({'evidence':'saved'})))],
                             history_high=1)
        self.assertEqual(runtime.run(),'preserve for inspection')
        self.assertEqual(self.state()['context_epoch'],0)
        self.assertIn('saved',str(self.state()['history']))

    def test_invalid_reply_cannot_leave_queued_controls(self):
        host=EventHost([Probe(hook=lambda e:EventReply(
            Contribution(observations=(Observation(image='invalid'),)),
            (Control('reset','must not leak'),)))])
        with self.assertRaises(AgentError):host.emit(Event('session_started','s',0,0))
        self.assertEqual(host.controls,[])
