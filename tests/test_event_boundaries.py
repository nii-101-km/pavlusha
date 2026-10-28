"""Failure and persistence boundaries of the experimental event interfaces."""
import json
from pathlib import Path
import tempfile
import unittest

from pavlusha_agent.basic_host import BasicHost
from pavlusha_agent.core import AgentError, ShellResult
from pavlusha_agent.extensions import Contribution, Outcome, Tool
from pavlusha_agent.runtime import Runtime
from pavlusha_agent.session import Session
from tests.test_events import Probe, Provider, reply, tool


class EventBoundaryTests(unittest.TestCase):
    def test_observer_failure_after_finish_keeps_session_terminal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def fail(event):
                if event.kind=='turn_completed':raise RuntimeError('observer failed')
            runtime=Runtime(Provider([reply({'action':'finish','summary':'done'})]),task='task',
                session_dir=root/'session',extensions=[BasicHost(root),Probe(hook=fail)])
            with self.assertRaisesRegex(AgentError,'observer failed'):runtime.run()
            state=json.loads((root/'session/session.json').read_text())
            self.assertEqual(state['status'],'finished')
            with self.assertRaisesRegex(AgentError,'session already finished'):
                Session(root/'session','task')

    def test_result_is_durable_before_completed_turn_reaches_module(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);seen=[]
            def inspect(event):
                if event.kind=='turn_completed':
                    state=json.loads((root/'session/session.json').read_text())
                    seen.append((state['turn_id'],state['pending']))
            runtime=Runtime(Provider([reply({'action':'finish','summary':'done'})]),task='task',
                session_dir=root/'session',extensions=[BasicHost(root),Probe(hook=inspect)])
            runtime.run()
            self.assertEqual(seen,[(1,None)])
    def test_observer_failure_does_not_reexecute_completed_effect_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);calls=[]
            def shell(*a,**kw):calls.append(1);return ShellResult('effect',False,0,False,'OK','',0)
            def fail(event):
                if event.kind=='turn_completed':raise RuntimeError('observer failed')
            runtime=Runtime(Provider([reply({'action':'shell','command':'effect'})]),task='task',
                session_dir=root/'session',extensions=[BasicHost(root,shell_runner=shell),Probe(hook=fail)])
            with self.assertRaisesRegex(AgentError,'observer failed'):runtime.run()
            second=Runtime(Provider([reply({'action':'finish','summary':'done'})]),task='task',
                session_dir=root/'session',extensions=[BasicHost(root,shell_runner=shell)])
            second.run();self.assertEqual(calls,[1])
    def test_reset_hook_failure_preserves_history_and_stops_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def fail(event):
                if event.kind=='before_reset':raise RuntimeError('could not save module')
            observer=Probe(hook=fail)
            host=BasicHost(root,shell_runner=lambda *a,**kw:ShellResult('one',False,0,False,'OK','',0))
            runtime=Runtime(Provider([reply({'action':'shell','command':'one'})]),task='task',
                session_dir=root/'session',extensions=[host,observer],history_high=1)
            with self.assertRaisesRegex(AgentError,'could not save'):runtime.run()
            state=json.loads((root/'session/session.json').read_text())
            self.assertEqual(len(state['history']),1)
            self.assertEqual(state['context_epoch'],0)
            self.assertEqual(observer.events[-1].kind,'session_stopped')
    def test_task_mismatch_cannot_reuse_existing_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            Session(tmp,'original')
            with self.assertRaisesRegex(AgentError,'task/version mismatch'):Session(tmp,'different')
    def test_shutdown_of_one_module_does_not_suppress_other_shutdowns(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            def fail(event):
                if event.kind=='session_stopped':raise RuntimeError('close failed')
            healthy=Probe('healthy');bad=Probe('bad',fail)
            runtime=Runtime(Provider([reply({'action':'finish','summary':'done'})]),task='task',
                session_dir=root/'session',extensions=[BasicHost(root),healthy,bad])
            with self.assertRaisesRegex(AgentError,'close failed'):runtime.run()
            self.assertEqual(healthy.events[-1].kind,'session_stopped')
            self.assertEqual([m.name for m in runtime.host.started],['bad'])
    def test_network_permission_denial_is_observed_without_shell_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);calls=[];observer=Probe()
            host=BasicHost(root,shell_runner=lambda *a,**kw:calls.append(1))
            runtime=Runtime(Provider([reply({'action':'shell','command':'wget example','network':True}),
                reply({'action':'finish','summary':'done'})]),task='task',session_dir=root/'session',
                extensions=[host,observer])
            runtime.run()
            turn=next(e.data['turn'] for e in observer.events if e.kind=='turn_completed')
            self.assertEqual(turn['result']['error'],'network_not_granted')
            self.assertEqual(calls,[])

    def test_failed_stop_journal_cannot_skip_module_cleanup(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);observer=Probe()
            runtime=Runtime(Provider([reply({'action':'finish','summary':'done'})]),task='task',
                session_dir=root/'session',extensions=[BasicHost(root),observer])
            original=runtime.session.journal
            def journal(kind, **data):
                if kind=='lifecycle' and data['event']['kind']=='session_stopped':
                    raise OSError('journal full')
                return original(kind,**data)
            with patch.object(runtime.session,'journal',journal):
                with self.assertRaisesRegex(AgentError,'stop persistence failed'):runtime.run()
            self.assertEqual(observer.events[-1].kind,'session_stopped')

    def test_live_stream_is_diagnostic_and_completed_event_still_occurs_once(self):
        class Streaming(Provider):
            def worker_completion(self,messages,**options):
                options['on_delta']('reasoning','Working')
                options['on_delta']('content','complete')
                return super().worker_completion(messages,**options)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);observer=Probe()
            runtime=Runtime(Streaming([reply({'action':'finish','summary':'done'})]),task='task',
                session_dir=root/'session',extensions=[BasicHost(root),observer],live=True)
            runtime.run()
            deltas=[json.loads(line) for line in (root/'session/generation.jsonl').read_text().splitlines()]
            self.assertEqual([d['text'] for d in deltas],['Working','complete'])
            self.assertEqual(len([e for e in observer.events if e.kind=='turn_completed']),1)

    def test_same_runtime_cannot_emit_a_second_session_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);observer=Probe()
            runtime=Runtime(Provider([reply({'action':'finish','summary':'done'})]),task='task',
                session_dir=root/'session',extensions=[BasicHost(root),observer])
            runtime.run()
            with self.assertRaisesRegex(AgentError,'already ran'):runtime.run()
            self.assertEqual(len([e for e in observer.events if e.kind=='session_started']),1)


if __name__=='__main__':unittest.main()
