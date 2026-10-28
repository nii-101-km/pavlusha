import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pavlusha_agent.core import AgentError
from pavlusha_agent.session import Session
from pavlusha_modules.project_state import ProjectState
from tests.test_project_state_module import StateFixture, initialize, shell, done, finish, review


class RecoveryTests(StateFixture, unittest.TestCase):
    def test_torn_final_record_is_removed_without_losing_committed_turns(self):
        for tail in (b'{"kind":"lifecycle","event":', b'{"text":"\xd0'):
            with self.subTest(tail=tail):
                directory=self.root/('torn-'+tail.hex())
                session=Session(directory,'task')
                turn={'turn_id':1,'context_epoch':0,'action':None,'result':{},
                      'status':'completed','stop':False}
                session.complete(turn,[])
                journal=directory/'events.jsonl'
                committed=journal.read_bytes()
                with journal.open('ab') as output:output.write(tail)
                restarted=Session(directory,'task')
                self.assertEqual(journal.read_bytes(),committed)
                self.assertEqual(restarted.completed_turns(),[turn])
                restarted.journal('new_record')
                self.assertEqual(json.loads(journal.read_bytes().splitlines()[-1])['kind'],'new_record')

    def test_complete_corrupt_record_is_not_discarded(self):
        session=Session(self.root/'corrupt','task')
        journal=session.directory/'events.jsonl'
        journal.write_bytes(b'{broken}\n')
        with self.assertRaisesRegex(AgentError,'cannot recover event journal'):
            Session(session.directory,'task')
        self.assertEqual(journal.read_bytes(),b'{broken}\n')

    def test_valid_final_record_without_newline_is_separated_from_next_append(self):
        session=Session(self.root/'newline','task')
        journal=session.directory/'events.jsonl'
        journal.write_bytes(b'{"kind":"request"}')
        restarted=Session(session.directory,'task')
        restarted.journal('next')
        self.assertEqual([json.loads(line)['kind'] for line in journal.read_bytes().splitlines()],
                         ['request','next'])

    def interrupted_run(self, replies):
        runtime=self.runtime(replies)
        runtime.max_steps=len(replies)
        with self.assertRaises(AgentError): runtime.run()
        return runtime

    def test_event_committed_before_delivery_is_replayed(self):
        class Failing(ProjectState):
            def on_event(self,event):
                if event.kind=='turn_completed' and event.turn_id==2:
                    raise RuntimeError('crash before delivery')
                return super().on_event(event)
        first=self.runtime([initialize(),shell()],module=Failing(review_every=0))
        with self.assertRaisesRegex(AgentError,'crash before delivery'):first.run()
        self.assertEqual(self.effects,['shell'])
        self.assertEqual(json.loads(self.state_file().read_text())['payload']['last_processed_turn'],1)
        second=self.runtime([done(),finish()]);self.assertEqual(second.run(),'done')
        self.assertEqual(self.effects,['shell'])
        self.assertEqual(self.module.data['operation_count'],1)
        self.assertEqual(self.module.data['last_processed_turn'],4)

    def test_uncommitted_journal_entry_is_removed_before_retrying_a_turn(self):
        session=Session(self.root/'technical','task')
        turn={'turn_id':1,'context_epoch':0,'action':None,'result':{},'status':'completed','stop':False}
        with patch.object(session,'save',side_effect=OSError('power loss')):
            with self.assertRaises(OSError):session.complete(turn,[])
        self.assertIn('turn_completed',(session.directory/'events.jsonl').read_text())
        restarted=Session(session.directory,'task')
        self.assertEqual(restarted.state['turn_id'],0)
        self.assertNotIn('turn_completed',(session.directory/'events.jsonl').read_text())
        restarted.complete(turn,[])
        self.assertEqual([t['turn_id'] for t in restarted.completed_turns(0)],[1])

    def test_older_generation_replays_state_actions_and_does_not_repeat_command(self):
        self.interrupted_run([initialize(),shell(),done()])
        self.state_file().write_text('{broken')
        directory=self.state_file().parent
        for generation in directory.glob('generation-*.json'):
            if int(generation.stem.split('-')[1])>1:generation.unlink()
        resumed=self.runtime([finish()]);self.assertEqual(resumed.run(),'done')
        self.assertEqual(self.effects,['shell'])
        self.assertTrue(all(item['status']=='DONE' for item in self.module.data['project']['work']))
        self.assertEqual(self.module.data['operation_count'],1)

    def test_missing_journal_turn_fails_instead_of_inventing_state(self):
        self.interrupted_run([initialize(),shell()])
        self.state_file().write_text('{broken')
        directory=self.state_file().parent
        for generation in directory.glob('generation-*.json'):
            if int(generation.stem.split('-')[1])>1:generation.unlink()
        journal=self.root/'session/events.jsonl'
        lines=journal.read_text().splitlines()
        journal.write_text('\n'.join(line for line in lines if not (
            (item:=json.loads(line)).get('kind')=='turn_completed' and item.get('turn_id')==2))+'\n')
        resumed=self.runtime([finish()])
        with self.assertRaisesRegex(AgentError,'missing committed turns'):resumed.run()
        self.assertEqual(self.effects,['shell'])

    def test_no_valid_generation_fails(self):
        self.interrupted_run([initialize()])
        self.state_file().write_text('invalid')
        for generation in self.state_file().parent.glob('generation-*.json'):
            generation.write_text('invalid')
        with self.assertRaisesRegex(AgentError,'no valid Project State generation'):
            self.runtime([finish()]).run()

    def test_replayed_periodic_review_remains_required_after_recovery_reset(self):
        first=self.runtime([initialize(),shell()],module=ProjectState(review_every=1))
        first.max_steps=2
        with self.assertRaises(AgentError):first.run()
        self.state_file().write_text('invalid')
        for generation in self.state_file().parent.glob('generation-*.json'):
            if int(generation.stem.split('-')[1])>1:generation.unlink()
        second=self.runtime([done(),review(),finish()],module=ProjectState(review_every=1))
        self.assertEqual(second.run(),'done')
        self.assertEqual(second.session.state['context_epoch'],1)
        self.assertEqual(self.effects,['shell'])
