"""Actual SSE detector/retry path and existing terminal intervention machinery."""
import json
import os
import pty
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pavlusha_agent.core import AgentError
from pavlusha_agent.state_store import StateStore
from tests import test_reasoning_loop as loops
from tests.test_reasoning_window import init_turn
from tests.test_checkpoint_snapshots import done


class HumanEscalationTests(unittest.TestCase):
    def run_case(self, replies, lines=('Use a concrete next action.\n',), *, ceiling=1,
                 read_hook=None, seed=True, restart=False):
        reads=[]
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            master,slave=pty.openpty()
            inputs=iter(lines)
            def read(session):
                self.assertTrue(session.paused)
                reads.append(StateStore(root/'state','task').load())
                if read_hook: read_hook(root)
                return next(inputs)
            try:
                with os.fdopen(slave,'r') as terminal, patch('sys.stdin',terminal), \
                     patch('pavlusha_agent.interactive.InteractiveSession._readline',read), \
                     patch('pavlusha_agent.provider.ChatProvider.resolve_model',return_value='model'):
                    case=loops.RuntimeRecoveryTests().run_case(root,replies,seed=seed,
                        extra=['--interactive','--max-reasoning-loop-recoveries',str(ceiling),
                               *([] if seed else ['--reset-state'])])
            finally:
                os.close(master)
            case['reads']=reads
            case['history_archived']=(root/'state/history_archive.jsonl').exists()
            case['keep']=(root/'work/keep.txt').read_text() if (root/'work/keep.txt').exists() else None
            if restart:
                case['restarted']=StateStore(root/'state','task',cold_restart=True).load()
            return case

    def loop(self):
        return loops.script({'action':'shell','command':'NEVER_EXECUTE'},loops.QWEN_TRACE.read_text())

    def test_exact_ceiling_then_existing_intervention_then_finish(self):
        case=self.run_case([self.loop()]*4+[loops.script(loops.SHELL),loops.script()],ceiling=3,
                           lines=('Read the rendered page instead of guessing.\n',))
        self.assertEqual(case['result'],0,case['error'])
        self.assertEqual(case['commands'],['verify'])
        signals=[e for e in case['events'] if e['kind']=='reasoning_loop']
        self.assertEqual([e['recovery_attempt'] for e in signals],[0,1,2,3])
        self.assertTrue(signals[-1]['exhausted'])
        self.assertTrue(all(e['word_count']==880 for e in signals))
        self.assertEqual(len(case['reads']),1)
        self.assertEqual(case['reads'][0],case['before'])
        self.assertIn('NEED USER',case['output'])
        self.assertIn('after 3 attempts',case['output'])
        messages=case['requests'][4]['messages']
        self.assertIn('USER MESSAGE AT SAFE BOUNDARY:\nRead the rendered page',str(messages).replace('\\n','\n'))
        self.assertNotIn('NEVER_EXECUTE',str(messages))
        self.assertNotIn("I'm noticing",str(messages))
        self.assertNotIn('REASONING RECOVERY',str(messages))
        self.assertEqual(case['state']['task'],case['before']['task'])
        self.assertEqual(case['state']['project_state'],case['before']['project_state'])
        self.assertEqual(case['state']['run']['status'],'finished')
        self.assertEqual(case['keep'],'persistent work')
        self.assertFalse(case['history_archived'])

    def test_reset_allows_second_exhaustion_without_an_accepted_action(self):
        case=self.run_case([self.loop()]*4+[loops.script()],
                           lines=('Try a different approach.\n','Use the existing evidence.\n'))
        self.assertEqual(case['result'],0,case['error'])
        self.assertEqual(len(case['reads']),2)
        signals=[e for e in case['events'] if e['kind']=='reasoning_loop']
        self.assertEqual([e['recovery_attempt'] for e in signals],[0,1,0,1])
        events=[e for e in case['events'] if e['kind']=='human_escalation']
        self.assertEqual([e['resumed'] for e in events],[False,True,False,True])
        self.assertTrue(all(e['recovery_attempts']==1 for e in events))
        self.assertIn('Use the existing evidence',str(case['requests'][-1]['messages']))

    def test_blank_input_keeps_waiting_without_retry_or_reset(self):
        case=self.run_case([self.loop()]*2+[loops.script()],lines=('\n','   \n','Inspect the file.\n'))
        self.assertEqual(case['result'],0,case['error'])
        self.assertEqual(len(case['reads']),3)
        self.assertEqual(len(case['requests']),3)
        self.assertEqual(case['reads'],[case['before']]*3)
        self.assertIn('empty input keeps waiting',case['output'])
        events=[e for e in case['events'] if e['kind']=='human_escalation']
        self.assertEqual([e['resumed'] for e in events],[False,True])

    def test_quit_and_eof_do_not_reset_or_mark_finished(self):
        for line in ('/quit\n',''):
            with self.subTest(line=line):
                case=self.run_case([self.loop()]*2,lines=(line,))
                self.assertEqual(case['result'],0,case['error'])
                self.assertEqual(case['state'],case['before'])
                self.assertNotEqual(case['state']['run']['status'],'finished')
                events=[e for e in case['events'] if e['kind']=='human_escalation']
                self.assertEqual([e['resumed'] for e in events],[False])

    def test_noninteractive_exhaustion_is_bounded_failure(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch('pavlusha_agent.interactive.InteractiveSession.boundary',side_effect=AssertionError('must not wait')):
            case=loops.RuntimeRecoveryTests().run_case(Path(tmp),[self.loop()]*2,
                extra=['--max-reasoning-loop-recoveries','1'])
            self.assertIn('Human escalation is unavailable in non-interactive mode',case['error'])
            self.assertEqual(len(case['requests']),2)
            self.assertEqual(case['state'],case['before'])

    def test_quit_at_escalation_keeps_existing_cold_restart_source(self):
        case=self.run_case([self.loop()]*2,lines=('/quit\n',),restart=True)
        self.assertEqual(case['result'],0,case['error'])
        self.assertEqual(case['restarted'],case['before'])
        self.assertEqual(case['restarted']['recovery_checkpoint'],case['before']['recovery_checkpoint'])
        self.assertEqual(case['keep'],'persistent work')
        self.assertNotIn('user_message',str(case['restarted']))

    def test_integrity_failure_at_escalation_never_waits(self):
        for validator in ('validate_checkpoint','validate_persisted_project_state'):
            with self.subTest(validator=validator), patch('pavlusha_agent.runtime.'+validator,
                                                        side_effect=AgentError('integrity failure')):
                case=self.run_case([self.loop()]*2,lines=())
                self.assertIn('integrity failure',case['error'])
                self.assertEqual(case['reads'],[])
                self.assertNotIn('NEED USER',case['output'])
                self.assertFalse(any(e['kind']=='human_escalation' for e in case['events']))

    def test_provider_failure_does_not_escalate(self):
        with patch('pavlusha_agent.provider.ChatProvider.worker_completion',side_effect=AgentError('network failed')):
            case=self.run_case([],lines=())
        self.assertIn('network failed',case['error'])
        self.assertEqual(case['reads'],[])
        self.assertNotIn('NEED USER',case['output'])

    def test_escalation_before_project_init_uses_existing_init_gate(self):
        # Fresh State is valid but has no committed generation until project_init.
        init=json.loads(init_turn().content)
        update=json.loads(done('verified').content)
        case=self.run_case([self.loop()]*2+[loops.script(init),loops.script(update),loops.script()],seed=False)
        self.assertEqual(case['result'],0,case['error'])
        schema=case['requests'][2]['response_format']['json_schema']['schema']
        self.assertIn('project_init',str(schema))
        self.assertNotIn('"const": "shell"',json.dumps(schema))


if __name__=='__main__':
    unittest.main()
