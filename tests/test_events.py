"""Exercise events through the real bare turn loop, not through hook mocks alone."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent.basic_host import BasicHost
from pavlusha_agent.config import SYSTEM_PROMPT
from pavlusha_agent.core import AgentError, ProviderTurn, ShellResult
from pavlusha_agent.extensions import Contribution, Event, EventHost, Outcome, Tool, InvocationContext
from pavlusha_agent.model_backend import ProviderContextOverflow
from pavlusha_agent.runtime import Runtime
from pavlusha_agent.session import Session


def reply(action, *, tokens=10, finish='stop', reasoning='Observed evidence'):
    return ProviderTurn(json.dumps(action), reasoning, finish, prompt_tokens=tokens)


def tool(name='probe', invoke=None):
    return Tool(name, 'A test capability', {'type':'object','properties':{},'required':[],
        'additionalProperties':False}, lambda args, context: invoke(args) if invoke else Outcome({'ok':True}))


class Probe:
    def __init__(self, name='probe', hook=None):
        self.name, self.events, self.hook = name, [], hook
    def on_event(self, event):
        self.events.append(copy.deepcopy(event))
        return self.hook(event) if self.hook else None


class Provider:
    def __init__(self, replies, hook=None):
        self.replies, self.requests, self.hook = iter(replies), [], hook
    def worker_completion(self, messages, **options):
        self.requests.append(copy.deepcopy({'messages':messages, **options}))
        if self.hook: self.hook(messages, options)
        item = next(self.replies)
        if isinstance(item, BaseException): raise item
        return item


class RuntimeEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root/'work'
        self.work.mkdir()
        self.calls = []
    def host(self):
        def shell(workdir, command, **options):
            self.calls.append((command,options))
            return ShellResult(command,options['network'],0,False,'VERIFIED','',.01)
        return BasicHost(self.work, shell_runner=shell)
    def runtime(self, replies, modules=(), **options):
        provider = replies if isinstance(replies,Provider) else Provider(replies)
        return Runtime(provider, task='TASK', session_dir=self.root/'session',
            extensions=[self.host(), *modules], max_steps=10, **options)
    def test_order_full_turn_and_unchanged_system_prompt(self):
        probe = Probe()
        runtime = self.runtime([reply({'action':'shell','command':'verify'}),
            reply({'action':'finish','summary':'done'})], [probe])
        self.assertEqual(runtime.run(),'done')
        self.assertEqual([e.kind for e in probe.events], [
            'session_started','prepare_turn','turn_completed','prepare_turn','turn_completed','session_stopped'])
        turn = probe.events[2].data['turn']
        self.assertEqual(turn['response']['reasoning_content'],'Observed evidence')
        self.assertEqual(turn['action'], {'action':'shell','command':'verify'})
        self.assertEqual(turn['result']['stdout'],'VERIFIED')
        self.assertEqual(turn['status'],'completed')
        self.assertEqual(turn['request']['messages'][0],{'role':'system','content':SYSTEM_PROMPT})
        self.assertEqual(probe.events[-1].data['reason'],'finished')
        self.assertEqual(probe.events[-2].data['turn']['stop'],True)
    def test_initial_and_post_reset_prepare_have_correct_flags(self):
        probe=Probe()
        runtime=self.runtime([reply({'action':'shell','command':'one'}),
            reply({'action':'shell','command':'two'}),reply({'action':'finish','summary':'done'})],
            [probe], history_high=2)
        runtime.run()
        prepares=[e for e in probe.events if e.kind=='prepare_turn']
        self.assertEqual([e.data['first_in_context'] for e in prepares],[True,False,True])
        self.assertEqual([e.context_epoch for e in prepares],[0,0,1])
        reset=next(e for e in probe.events if e.kind=='before_reset')
        self.assertEqual(len(reset.data['history']),2)
        self.assertEqual(reset.data['reason'],'history_high')
        final=runtime.provider.requests[-1]['messages']
        self.assertNotIn('VERIFIED',str(final))
        self.assertIn('CONTEXT RESET',str(final))
        self.assertEqual(len(self.calls),2)
    def test_service_prefix_remains_stable_across_turns_and_after_reset(self):
        probe = Probe()
        runtime = self.runtime([reply({'action':'shell','command':'one'}),
            reply({'action':'shell','command':'two'}),
            reply({'action':'shell','command':'three'}),
            reply({'action':'finish','summary':'done'})], [probe], history_high=2)
        runtime.run()
        services = [request['messages'][2]['content'] for request in runtime.provider.requests]
        self.assertEqual(services[0], services[1])
        self.assertEqual(services[2], services[3])
        self.assertNotEqual(services[1], services[2])
        self.assertIn('CONTEXT RESET', services[2])
        self.assertEqual([e.data['next_turn_id'] for e in probe.events if e.kind == 'prepare_turn'],
                         [1, 2, 3, 4])
    def test_module_rebuilds_prefix_before_first_post_reset_request(self):
        def hook(event):
            if event.kind=='prepare_turn':
                return Contribution(prefix={'snapshot':f'epoch={event.context_epoch}'},
                                    message='CURRENT MODULE NOTICE')
        runtime=self.runtime([reply({'action':'shell','command':'one'}),
            reply({'action':'finish','summary':'done'})],[Probe(hook=hook)],history_high=1)
        runtime.run()
        messages=runtime.provider.requests[-1]['messages']
        self.assertIn('epoch=1',str(messages));self.assertNotIn('epoch=0',str(messages))
        self.assertEqual(messages[-1]['content'],'EXTENSION probe:\nCURRENT MODULE NOTICE')
    def test_reset_can_be_deferred_without_discarding_history(self):
        def hook(event):
            if event.kind=='before_reset':return Contribution(reset_ready=False,message='PRESERVE HISTORY')
        runtime=self.runtime([reply({'action':'shell','command':'one'}),
            reply({'action':'finish','summary':'done'})],[Probe(hook=hook)],history_high=1)
        runtime.run()
        self.assertEqual(runtime.session.state['context_epoch'],0)
        self.assertIn('VERIFIED',str(runtime.provider.requests[-1]))
    def test_exclusive_module_limits_tools_but_preserves_other_context(self):
        review_done=[]
        def check(args):review_done.append(True);return Outcome({'review':'accepted'})
        def hook(event):
            if event.kind=='session_started':
                return Contribution(prefix={'state':'REVIEW SNAPSHOT'},message='REVIEW ONLY',
                                    tools=(tool('review',check),),exclusive=True)
            if event.kind=='turn_completed':return Contribution()
        review=Probe('state',hook)
        other=Probe('map',lambda e:Contribution(prefix={'map':'HIDDEN MAP'},message='HIDDEN INSTRUCTION')
                    if e.kind=='session_started' else None)
        runtime=self.runtime([reply({'action':'shell','command':'blocked'}),
            reply({'action':'review'}),reply({'action':'shell','command':'allowed'}),
            reply({'action':'finish','summary':'done'})], [review,other])
        # Keep exclusive mode until review ACTUALLY succeeds; rejection alone must not release it.
        old_hook=review.hook
        review.hook=lambda e: (None if e.kind=='turn_completed' and e.data['turn']['status']!='completed' else old_hook(e))
        runtime.run()
        first=runtime.provider.requests[0]
        self.assertIn('HIDDEN MAP',str(first));self.assertIn('HIDDEN INSTRUCTION',str(first))
        self.assertNotIn('"const": "shell"',json.dumps(first['response_format']))
        self.assertEqual([x[0] for x in self.calls],['allowed'])
        self.assertEqual(review_done,[True])
        self.assertIn('HIDDEN MAP',str(runtime.provider.requests[2]))
    def test_truncated_and_invalid_calls_emit_complete_turns_without_execution(self):
        probe=Probe()
        runtime=self.runtime([reply({'action':'shell','command':'blocked'},finish='length'),
            reply({'action':'shell','command':True}), reply({'action':'finish','summary':'done'})],[probe])
        runtime.run()
        self.assertEqual(self.calls,[])
        turns=[e.data['turn'] for e in probe.events if e.kind=='turn_completed']
        self.assertEqual([t['status'] for t in turns],['rejected','rejected','completed'])
        self.assertEqual([t['turn_id'] for t in turns],[1,2,3])
    def test_runtime_uses_request_snapshot_even_if_host_changes_while_model_runs(self):
        invoked=[]
        extension=Probe('dynamic',lambda e:Contribution(tools=(tool('old',lambda a:invoked.append('old') or Outcome({})),))
                        if e.kind=='session_started' else None)
        provider=Provider([reply({'action':'old'}),reply({'action':'finish','summary':'done'})])
        runtime=self.runtime(provider,[extension])
        def mutate(messages,options):
            runtime.host.contributions['dynamic']=Contribution(tools=(tool('new'),))
        provider.hook=mutate
        runtime.run()
        self.assertEqual(invoked,['old'])
    def test_output_limit_still_notifies_modules_that_action_executed(self):
        probe=Probe()
        big=Probe('big',lambda e:Contribution(tools=(tool('big',lambda a:Outcome({'text':'x'*2000})),))
                  if e.kind=='session_started' else None)
        runtime=self.runtime([reply({'action':'big'}),reply({'action':'finish','summary':'done'})],
                             [probe,big],output_limit=1000)
        runtime.run()
        first=next(e for e in probe.events if e.kind=='turn_completed').data['turn']
        self.assertTrue(first['result']['output_withheld'])
        self.assertTrue(first['result']['effects_may_have_occurred'])
        self.assertNotIn('x'*2000,str(runtime.provider.requests[-1]['messages']))
    def test_tool_exception_is_a_full_factual_turn(self):
        def fail(a):raise RuntimeError('tool failed')
        module=Probe('bad',lambda e:Contribution(tools=(tool('bad',fail),)) if e.kind=='session_started' else None)
        runtime=self.runtime([reply({'action':'bad'}),reply({'action':'finish','summary':'done'})],[module])
        runtime.run()
        first=next(e for e in module.events if e.kind=='turn_completed').data['turn']
        self.assertEqual(first['status'],'execution_error')
        self.assertTrue(first['result']['effects_may_have_occurred'])
    def test_provider_failure_notifies_completed_attempt_and_cleanup(self):
        probe=Probe();runtime=self.runtime([RuntimeError('provider failed')],[probe])
        with self.assertRaisesRegex(RuntimeError,'provider failed'):runtime.run()
        self.assertEqual([e.kind for e in probe.events],['session_started','prepare_turn','turn_completed','session_stopped'])
        completed=probe.events[2].data['turn']
        self.assertIsNone(completed['response']);self.assertIsNone(completed['action'])
        self.assertEqual(completed['status'],'provider_error')
    def test_max_steps_also_stops_all_modules(self):
        probe=Probe();runtime=self.runtime([reply({'action':'shell','command':'once'})],[probe])
        runtime.max_steps=1
        with self.assertRaisesRegex(AgentError,'maximum'):runtime.run()
        self.assertEqual(probe.events[-1].kind,'session_stopped')
    def test_overflow_emits_failed_turn_then_reset_then_first_prepare(self):
        probe=Probe()
        runtime=self.runtime([reply({'action':'shell','command':'once'}),ProviderContextOverflow('overflow'),
            reply({'action':'finish','summary':'done'})],[probe])
        runtime.run()
        self.assertEqual([x[0] for x in self.calls],['once'])
        completed=[e.data['turn'] for e in probe.events if e.kind=='turn_completed']
        self.assertEqual([t['status'] for t in completed],['completed','provider_error','completed'])
        self.assertEqual(runtime.session.state['context_epoch'],1)
        prepares=[e for e in probe.events if e.kind=='prepare_turn']
        self.assertTrue(prepares[-1].data['first_in_context'])
    def test_overflow_without_removable_history_stops_without_retry_loop(self):
        runtime=self.runtime([ProviderContextOverflow('overflow')])
        with self.assertRaisesRegex(AgentError,'no removable progress'):runtime.run()
        self.assertEqual(len(runtime.provider.requests),1)
    def test_reset_veto_on_overflow_keeps_history_and_stops(self):
        probe=Probe(hook=lambda e:Contribution(reset_ready=False) if e.kind=='before_reset' else None)
        runtime=self.runtime([reply({'action':'shell','command':'once'}),ProviderContextOverflow('overflow')],[probe])
        with self.assertRaisesRegex(AgentError,'deferred reset'):runtime.run()
        self.assertEqual(runtime.session.state['context_epoch'],0)
        self.assertEqual(len(runtime.session.state['history']),2)
    def test_context_usage_triggers_reset_independently_of_turn_counter(self):
        probe=Probe()
        runtime=self.runtime([reply({'action':'shell','command':'once'},tokens=90),
            reply({'action':'finish','summary':'done'})],[probe],context_capacity=100)
        runtime.run()
        self.assertEqual(next(e for e in probe.events if e.kind=='before_reset').data['reason'],'context_high')
    def test_restart_restores_technical_history_and_does_not_replay_action(self):
        first=self.runtime([reply({'action':'shell','command':'once'})]);first.max_steps=1
        with self.assertRaises(AgentError):first.run()
        probe=Probe();second=self.runtime([reply({'action':'finish','summary':'done'})],[probe])
        second.run()
        self.assertTrue(probe.events[0].data['resumed'])
        self.assertEqual(probe.events[0].data['last_turn_id'],1)
        self.assertFalse(probe.events[1].data['first_in_context'])
        self.assertEqual([x[0] for x in self.calls],['once'])
        self.assertIn('VERIFIED',str(second.provider.requests[0]))
    def test_interrupted_action_reports_turn_and_refuses_uncertain_replay(self):
        def cancel(a):raise KeyboardInterrupt()
        module=Probe('cancel',lambda e:Contribution(tools=(tool('cancel',cancel),)) if e.kind=='session_started' else None)
        runtime=self.runtime([reply({'action':'cancel'})],[module])
        with self.assertRaises(KeyboardInterrupt):runtime.run()
        self.assertEqual(module.events[-2].data['turn']['status'],'interrupted')
        self.assertEqual(module.events[-1].data['reason'],'cancelled')
        with self.assertRaisesRegex(AgentError,'uncertain outcome'):
            self.runtime([reply({'action':'finish','summary':'done'})])
    def test_no_project_modules_or_old_registries_in_active_runtime(self):
        root=Path(__file__).resolve().parents[1]
        for name in ('action_registry.py','functions.py','project_state.py','project_map.py','checkpoint.py','state_store.py'):
            self.assertFalse((root/'pavlusha_agent'/name).exists(),name)
        baseline=json.loads((root/'tests/fixtures/original-hashes.json').read_text())
        self.assertEqual(hashlib.sha256((root/'pavlusha_agent/config.py').read_bytes()).hexdigest(),
                         baseline['pavlusha_agent/config.py'])


class EventHostTests(unittest.TestCase):
    def event(self,kind='session_started'):return Event(kind,'session',0,0,{'nested':{'value':1}})
    def test_duplicate_extension_names_fail_before_startup(self):
        with self.assertRaisesRegex(AgentError,'duplicate'):EventHost([Probe(),Probe()])
    def test_duplicate_functions_fail_even_when_one_owner_is_exclusive(self):
        modules=[Probe(name,lambda e:Contribution(tools=(tool(),),exclusive=True)) for name in ('one','two')]
        host=EventHost(modules);host.emit(self.event())
        with self.assertRaisesRegex(AgentError,'function name conflict'):host.snapshot()
    def test_two_exclusive_owners_fail_explicitly(self):
        host=EventHost([Probe(n,lambda e:Contribution(exclusive=True)) for n in ('one','two')]);host.emit(self.event())
        with self.assertRaisesRegex(AgentError,'exclusive mode conflict'):host.snapshot()
    def test_events_are_copied_for_each_module(self):
        first=Probe('first',lambda e:e.data['nested'].update(value=999))
        second=Probe('second');event=self.event();host=EventHost([first,second]);host.emit(event)
        self.assertEqual(second.events[0].data['nested']['value'],1)
        self.assertEqual(event.data['nested']['value'],1)
    def test_contribution_schemas_are_copied_without_copying_handler_identity(self):
        called=[];spec=tool(invoke=lambda a:called.append(1) or Outcome({}))
        contribution=Contribution(prefix={'key':'original'},tools=(spec,))
        host=EventHost([Probe(hook=lambda e:contribution)]);host.emit(self.event());view=host.snapshot()
        contribution.prefix['key']='mutated';spec.arguments['required']=['new_arg']
        view.invoke({'action':'probe'}, InvocationContext('session', 1, 0, {}))
        self.assertEqual(called,[1]);self.assertIn('original',str(view.prefix))
    def test_failed_startup_cleanup_runs_reverse_including_partial_owner(self):
        calls=[]
        def hook(name):
            def handle(e):
                calls.append((name,e.kind))
                if name=='two' and e.kind=='session_started':raise RuntimeError('partial failure')
            return handle
        one,two,three=[Probe(n,hook(n)) for n in ('one','two','three')]
        host=EventHost([one,two,three])
        with self.assertRaises(RuntimeError):host.emit(self.event())
        host.stop(self.event('session_stopped'))
        self.assertEqual(calls,[('one','session_started'),('two','session_started'),
                                ('two','session_stopped'),('one','session_stopped')])
    def test_cleanup_failure_attempts_every_owner_and_preserves_failed_owner_for_retry(self):
        attempts=[];failed=[True]
        def hook(e):
            if e.kind=='session_stopped':
                attempts.append('two')
                if failed.pop():raise RuntimeError('close failed')
        host=EventHost([Probe('one'),Probe('two',hook)]);host.emit(self.event())
        with self.assertRaisesRegex(AgentError,'cleanup failed'):host.stop(self.event('session_stopped'))
        self.assertEqual([m.name for m in host.started],['two'])
        failed.append(False);host.stop(self.event('session_stopped'));self.assertEqual(host.started,[])
        self.assertEqual(attempts,['two','two'])
    def test_completed_turn_broadcast_attempts_all_modules_even_when_one_fails(self):
        def bad(e):
            if e.kind=='turn_completed':raise RuntimeError('observer failed')
        first,second=Probe('first',bad),Probe('second');host=EventHost([first,second]);host.emit(self.event())
        with self.assertRaisesRegex(AgentError,'observer failed'):host.emit(self.event('turn_completed'))
        self.assertEqual(second.events[-1].kind,'turn_completed')
    def test_zero_available_functions_fails_instead_of_silent_model_request(self):
        host=EventHost([Probe()]);host.emit(self.event())
        with self.assertRaisesRegex(AgentError,'no callable'):host.snapshot().response_format()
    def test_stop_after_startup_failure_is_emitted_by_runtime(self):
        def bad(e):
            if e.kind=='session_started':raise RuntimeError('startup failed')
        probe=Probe(hook=bad)
        with tempfile.TemporaryDirectory() as tmp:
            runtime=Runtime(Provider([]),task='task',session_dir=tmp,extensions=[probe])
            with self.assertRaisesRegex(RuntimeError,'startup failed'):runtime.run()
        self.assertEqual([e.kind for e in probe.events],['session_started','session_stopped'])


if __name__=='__main__':unittest.main()
