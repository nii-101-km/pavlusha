"""Attempt Project State integration solely through existing event interfaces."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pavlusha_agent.basic_host import BasicHost
from pavlusha_agent.core import AgentError, ShellResult
from pavlusha_agent.extensions import Contribution, Event, EventHost, Tool, Outcome
from pavlusha_agent.runtime import Runtime
from pavlusha_agent.model_backend import ProviderContextOverflow
from pavlusha_modules.project_state import ProjectState
from pavlusha_modules.shell import Shell
from tests.test_events import Provider, Probe, reply


def initialize():
    return reply({'action':'project_init','design':[], 'work':[
        {'objective':'Implement','status':'ACTIVE'}, {'objective':'Verify','status':'PLANNED'}]})


def done():
    return reply({'action':'project_update','changes':[
        {'op':'update_work','id':'W001','status':'DONE','evidence':['FILE: result.txt']},
        {'op':'update_work','id':'W002','status':'DONE','evidence':['SHELL: tests passed']} ]})


def review(handoff='Continue by inspecting result.txt.'):
    return reply({'action':'project_review_complete','handoff':handoff})


def shell():return reply({'action':'shell','command':'verify'})
def finish():return reply({'action':'finish','summary':'done'})


class StateFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.work=self.root/'work';self.work.mkdir()
        self.effects=[]
    def host(self):
        def run(*a,**kw):
            self.effects.append('shell')
            return ShellResult('verify',False,0,False,'OK','',0)
        return Shell(self.work,shell_runner=run)
    def runtime(self,replies,*,module=None,**options):
        self.module=module or ProjectState(review_every=0)
        return Runtime(Provider(replies),task='task',session_dir=self.root/'session',
            extensions=[self.host(),self.module],max_steps=15,**options)
    def state_file(self):return self.root/'session/modules/project_state/state.json'


class ProjectStateModuleTests(StateFixture, unittest.TestCase):
    def test_initialization_excludes_shell_then_releases_other_tools(self):
        runtime=self.runtime([shell(),initialize(),shell(),done(),finish()])
        self.assertEqual(runtime.run(),'done')
        self.assertEqual(self.effects,['shell'])
        self.assertNotIn('shell',str(runtime.provider.requests[0]['response_format']))
        self.assertIn('shell',str(runtime.provider.requests[2]['response_format']))
        self.assertEqual(self.module.data['operation_count'],1)
    def test_normal_work_updates_are_persisted_without_refreshing_frozen_prefix(self):
        runtime=self.runtime([initialize(),done(),finish()]);runtime.run()
        initial=runtime.provider.requests[0]['messages']
        before=runtime.provider.requests[1]['messages']
        after=runtime.provider.requests[2]['messages']
        prefix=lambda messages:next(m['content'] for m in messages if str(m['content']).startswith('EXTENSION project_state/state:'))
        self.assertEqual(prefix(initial),prefix(before))
        self.assertEqual(prefix(before),prefix(after))
        self.assertIn('project_init',prefix(initial))
        self.assertIn('"status": "accepted"',str(before))
        self.assertIn('DONE',str(after))
        envelope=json.loads(self.state_file().read_text())
        self.assertEqual(envelope['payload']['project']['work'][0]['status'],'DONE')
    def test_periodic_review_is_exclusive_until_review_completion(self):
        module=ProjectState(review_every=1)
        runtime=self.runtime([initialize(),shell(),shell(),done(),review(),finish()],module=module)
        runtime.run()
        self.assertEqual(self.effects,['shell'])
        for i in (2,3,4):
            schema=str(runtime.provider.requests[i]['response_format'])
            self.assertNotIn("'const': 'shell'",schema)
            self.assertIn('project_review_complete',schema)
        self.assertIn('shell',str(runtime.provider.requests[5]['response_format']))
        self.assertEqual(module.data['project']['last_review_operation'],1)
        self.assertEqual(module.data['checkpoint']['handoff'],'Continue by inspecting result.txt.')
        prefix=lambda request:next(m['content'] for m in request['messages'] if str(m['content']).startswith('EXTENSION project_state/state:'))
        self.assertEqual(prefix(runtime.provider.requests[0]),prefix(runtime.provider.requests[-1]))
    def test_combined_review_commits_changes_and_handoff_before_shell_resumes(self):
        combined = reply({'action':'project_review_complete', 'handoff':'Fix the failed tests.',
            'changes':[{'op':'update_work','id':'W001','status':'PLANNED'},
                       {'op':'update_work','id':'W002','status':'ACTIVE',
                        'evidence':['SHELL: tests failed']} ]})
        runtime = self.runtime([initialize(), shell(), combined, shell(), review(), done(), finish()],
                               module=ProjectState(review_every=1, combined_review=True))
        self.assertEqual(runtime.run(), 'done')
        schema = runtime.provider.requests[2]['response_format']['json_schema']['schema']
        self.assertEqual(schema['properties']['action']['const'], 'project_review_complete')
        self.assertIn('changes', schema['properties'])
        self.assertIn("'const': 'shell'", str(runtime.provider.requests[3]['response_format']))
        self.assertEqual(self.effects, ['shell', 'shell'])
        self.assertEqual(self.module.data['project']['work'][1]['evidence'], ['SHELL: tests passed'])

    def test_invalid_combined_review_does_not_save_partial_changes_or_release_review(self):
        invalid = reply({'action':'project_review_complete', 'handoff':'Continue.',
                         'changes':[{'op':'update_work','id':'UNKNOWN','status':'DONE'}]})
        runtime = self.runtime([initialize(), shell(), invalid, review(), done(), finish()],
                               module=ProjectState(review_every=1, combined_review=True))
        self.assertEqual(runtime.run(), 'done')
        self.assertIn('project_state_rejected', str(runtime.provider.requests[3]['messages']))
        self.assertNotIn("'const': 'shell'", str(runtime.provider.requests[3]['response_format']))
        self.assertEqual(self.effects, ['shell'])

    def test_history_reset_requires_handoff_then_supplies_it_in_new_prefix(self):
        runtime=self.runtime([initialize(),shell(),review('NEXT: verify saved result.'),done(),finish()],history_high=2)
        runtime.run()
        self.assertEqual(runtime.session.state['context_epoch'],1)
        self.assertIn('project_review_complete',str(runtime.provider.requests[2]['response_format']))
        messages=runtime.provider.requests[3]['messages']
        self.assertIn('NEXT: verify saved result.',str(messages))
        self.assertNotIn('WORKER REASONING',str(messages))
        self.assertEqual(self.effects,['shell'])
    def test_uninitialized_module_survives_process_restart_without_inventing_state(self):
        runtime=self.runtime([shell()]);runtime.max_steps=1
        with self.assertRaises(AgentError):runtime.run()
        resumed=self.runtime([initialize(),done(),finish()]);resumed.run()
        self.assertTrue(resumed.session.resumed)
        self.assertEqual(self.effects,[])
    def test_working_state_restores_on_restart_and_completed_shell_is_not_replayed(self):
        first=self.runtime([initialize(),shell()]);first.max_steps=2
        with self.assertRaises(AgentError):first.run()
        second=self.runtime([done(),finish()]);second.run()
        self.assertEqual(self.effects,['shell'])
        self.assertEqual(self.module.data['operation_count'],0)
        self.assertEqual(second.session.state['context_epoch'],1)
        self.assertEqual(self.module.data['last_processed_turn'],4)
    def test_restart_restores_checkpoint_instead_of_pending_review(self):
        first=self.runtime([initialize(),shell()],module=ProjectState(review_every=1));first.max_steps=2
        with self.assertRaises(AgentError):first.run()
        second=self.runtime([done(),finish()],module=ProjectState(review_every=1));second.run()
        self.assertIn("'const': 'shell'",str(second.provider.requests[0]['response_format']))
        self.assertEqual(second.session.state['context_epoch'],1)
        self.assertEqual(self.effects,['shell'])
    def test_repeated_turn_notification_does_not_double_count_operations(self):
        runtime=self.runtime([initialize(),shell(),done(),finish()]);runtime.run()
        records=[json.loads(l) for l in (self.root/'session/events.jsonl').read_text().splitlines()]
        turn=next(e for e in records if e['kind']=='turn_completed' and (e['action']or{}).get('action')=='shell')
        before=copy.deepcopy(self.module.data)
        self.module.on_event(Event('turn_completed',runtime.session.state['session_id'],turn['turn_id'],0,{'turn':turn}))
        self.assertEqual(self.module.data,before)
    def test_bad_mutation_is_feedback_and_does_not_change_working_state(self):
        bad=reply({'action':'project_update','changes':[{'op':'update_work','id':'UNKNOWN','status':'DONE'}]})
        runtime=self.runtime([initialize(),bad,done(),finish()]);runtime.run()
        self.assertIn('project_state_rejected',str(runtime.provider.requests[2]['messages']))
        self.assertEqual(self.module.data['project']['revision'],2)
    def test_oversized_handoff_does_not_unlock_review(self):
        runtime=self.runtime([initialize(),shell(),review('я'*1100),done(),review(),finish()],
                             module=ProjectState(review_every=1))
        runtime.run()
        self.assertIn('project_state_rejected',str(runtime.provider.requests[3]['messages']))
        self.assertNotIn("'const': 'shell'",str(runtime.provider.requests[3]['response_format']))
    def test_corrupt_module_storage_uses_valid_generation(self):
        runtime=self.runtime([initialize()]);runtime.max_steps=1
        with self.assertRaises(AgentError):runtime.run()
        body=json.loads(self.state_file().read_text());body['payload']['operation_count']=999
        self.state_file().write_text(json.dumps(body))
        resumed=self.runtime([done(),finish()])
        self.assertEqual(resumed.run(),'done')
        self.assertEqual(self.module.data['project']['work'][0]['status'],'DONE')
    def test_overflow_restores_checkpoint_then_continues(self):
        runtime=self.runtime([initialize(),shell(),ProviderContextOverflow('full'),done(),finish()])
        runtime.run()
        self.assertEqual(runtime.session.state['context_epoch'],1)
        self.assertEqual(self.module.data['operation_count'],0)
        self.assertEqual(self.effects,['shell'])
        self.assertIn('inspect current work files',str(runtime.provider.requests[3]['messages']).lower())
    def test_repeated_overflow_same_checkpoint_stops(self):
        runtime=self.runtime([initialize(),shell(),ProviderContextOverflow('full'),shell(),
                              ProviderContextOverflow('full again')])
        with self.assertRaisesRegex(AgentError,'already recovered from this checkpoint'):
            runtime.run()
        self.assertEqual(runtime.session.state['context_epoch'],1)
    def test_recovery_will_not_run_with_old_context_if_another_module_vetoes_reset(self):
        first=self.runtime([initialize(),shell()]);first.max_steps=2
        with self.assertRaises(AgentError):first.run()
        blocker=Probe(hook=lambda e: Contribution(reset_ready=False) if e.kind=='before_reset' else None)
        second=Runtime(Provider([done()]),task='task',session_dir=self.root/'session',
                       extensions=[self.host(),ProjectState(review_every=0),blocker])
        with self.assertRaisesRegex(AgentError,'requires a completed context reset'):
            second.run()
        self.assertEqual(len(second.provider.requests),0)
    def test_review_can_request_reset_before_history_threshold(self):
        module=ProjectState(review_every=0)
        runtime=self.runtime([initialize(),shell(),review(),done(),finish()],module=module,history_high=2)
        runtime.run()
        entries=[json.loads(line) for line in (self.root/'session/events.jsonl').read_text().splitlines()]
        reasons=[entry['event']['data']['reason'] for entry in entries
                 if entry.get('kind')=='lifecycle' and entry['event']['kind']=='before_reset']
        self.assertEqual(reasons,['history_high','module_requested'])
    def test_unrelated_core_files_unchanged_by_recovery(self):
        root=Path(__file__).resolve().parents[1]
        before=json.loads((root/'tests/fixtures/state-integration-core-before.json').read_text())
        for name,expected in before.items():
            # Authorized interface, model lifecycle and release-version changes are tested separately.
            if name in ('pavlusha_agent/runtime.py','pavlusha_agent/extensions.py',
                        'pavlusha_agent/basic_host.py','pavlusha_agent/llama_server.py',
                        'pavlusha_agent/session.py', 'pavlusha_agent/__init__.py', 'pavlusha_agent/config.py',
                        'pavlusha_agent/cli.py','pavlusha_agent/adapters/__init__.py'):
                continue
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),expected,name)


class InterfaceGapTests(StateFixture, unittest.TestCase):
    """Passing reproductions of missing capabilities, not acceptance of weakened policy."""
    def test_module_owned_finish_rejects_unfinished_work_without_blocking_shell(self):
        runtime=self.runtime([initialize(),finish(),shell(),done(),finish()]);runtime.run()
        self.assertEqual(self.effects,['shell'])
        self.assertIn('unfinished_work',str(runtime.provider.requests[2]['messages']))
        self.assertEqual(runtime.session.state['status'],'finished')
        self.assertTrue(all(w['status']=='DONE' for w in self.module.data['project']['work']))
    def test_gap_redefining_finish_to_add_validation_conflicts_with_host(self):
        class FinishPolicy:
            name='finish_policy'
            def on_event(self,event):
                if event.kind=='session_started':
                    return Contribution(tools=(Tool('finish','State-aware finish',
                        {'type':'object','properties':{},'required':[],'additionalProperties':False},
                        lambda args, context:Outcome({'error':'unfinished_work'})),))
        runtime=Runtime(Provider([]),task='task',session_dir=self.root/'session',
                        extensions=[BasicHost(self.work),FinishPolicy()])
        with self.assertRaisesRegex(AgentError,'function name conflict'):runtime.run()
    def test_service_information_does_not_claim_state_is_absent(self):
        runtime=self.runtime([initialize(),done(),finish()]);runtime.run()
        prompt=runtime.provider.requests[0]['messages']
        self.assertFalse(any('Project State, Project Map and optional toolkits are absent' in m['content'] for m in prompt))
        self.assertTrue(any('MODULE project_state:' in m['content'] for m in prompt))


if __name__=='__main__':unittest.main()
