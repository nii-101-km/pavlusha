"""Real CPU-only workloads: detached children must die before returning to Worker."""
import io
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from pavlusha_agent.core import ProcessCleanupError
from pavlusha_agent.process_supervision import stop_owned_process
from pavlusha_agent.sandbox import run_shell


class OwnedProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.marker = self.root/'heartbeat'

    def assert_stopped(self):
        self.assertTrue(self.marker.exists(), 'Child must have actually started')
        size = self.marker.stat().st_size
        time.sleep(.2)
        self.assertEqual(self.marker.stat().st_size, size, 'Detached child still running')

    def test_backends_veto_restore_on_unconfirmed_owned_process_cleanup(self):
        from pavlusha_agent.llama_server import ManagedLlamaServer
        from pavlusha_agent.adapters import LMStudioAdapter
        manager=object.__new__(ManagedLlamaServer)
        manager.process=Mock();manager.process.poll.return_value=None
        with patch.object(manager,'_confirm_model'),patch.object(manager,'stop'),patch.object(manager,'start') as start:
            with self.assertRaises(ProcessCleanupError):
                with manager.released_worker('model'): raise ProcessCleanupError('unconfirmed')
            start.assert_not_called()
        provider=LMStudioAdapter('http://localhost:1234/v1','instance','',1,.1,100)
        with patch.object(provider,'_loaded_worker_instances',side_effect=[[(dict(key='model'),dict(id='instance'))],[]]), \
             patch.object(provider,'_model_lifecycle_request',return_value={'instance_id':'instance'}) as request:
            with self.assertRaises(ProcessCleanupError):
                with provider.released_worker(): raise ProcessCleanupError('unconfirmed')
            self.assertEqual(request.call_count,1)

    def test_shell_cleanup_failure_is_recorded_as_interrupted_and_blocks_replay(self):
        from pavlusha_agent.runtime import Runtime
        from pavlusha_agent.basic_host import BasicHost
        from tests.test_events import Provider, Probe, reply
        observer = Probe()
        def fail(*args, **kwargs):
            raise ProcessCleanupError('unconfirmed')
        runtime = Runtime(Provider([reply({'action':'shell','command':'work'})]),
            task='task', session_dir=self.root/'session',
            extensions=[BasicHost(self.root/'work',shell_runner=fail),observer])
        with self.assertRaises(ProcessCleanupError):
            runtime.run()
        turn = next(e.data['turn'] for e in observer.events if e.kind=='turn_completed')
        self.assertEqual(turn['status'],'interrupted')
        self.assertTrue(turn['result']['effects_may_have_occurred'])
        self.assertIsNotNone(runtime.session.state['pending'])
        self.assertEqual(observer.events[-1].kind,'session_stopped')

    def shell_command(self):
        import shlex
        child = ("import os,signal,time;os.setsid();signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                 "f=open('/work/heartbeat','ab',buffering=0);"
                 "exec('while True: f.write(b\"x\"); time.sleep(.02)')")
        return 'python3 -c '+shlex.quote(child)+' & sleep 60'

    def test_real_shell_timeout_stops_detached_child(self):
        result = run_shell(self.root,self.shell_command(),network=False,timeout=1)
        self.assertTrue(result.timed_out)
        self.assert_stopped()

    def test_cancel_or_pipe_exception_stops_tree_before_propagating(self):
        popen = subprocess.Popen
        for error in [KeyboardInterrupt(), OSError('pipe failed')]:
            with self.subTest(error=type(error).__name__):
                self.marker.unlink(missing_ok=True)
                def launch(*args, **kwargs):
                    process = popen(*args, **kwargs)
                    communicate = process.communicate
                    first = True
                    def interrupted(*args, **kwargs):
                        nonlocal first
                        if first:
                            first = False
                            deadline=time.monotonic()+3
                            while not self.marker.exists() and time.monotonic()<deadline: time.sleep(.01)
                            raise error
                        return communicate(*args, **kwargs)
                    process.communicate=interrupted
                    return process
                with patch('pavlusha_agent.sandbox.subprocess.Popen', side_effect=launch):
                    with self.assertRaises(type(error)):
                        run_shell(self.root,self.shell_command(),network=False,timeout=5)
                self.assert_stopped()

    def test_permission_failure_cannot_be_admitted_as_normal_completion(self):
        process=Mock(pid=123);process.poll.return_value=None
        with patch('pavlusha_agent.process_supervision.os.killpg',side_effect=PermissionError('denied')):
            with self.assertRaises(ProcessCleanupError): stop_owned_process(process)

    def test_unconfirmed_kill_is_fatal_and_wait_is_bounded(self):
        process = Mock(pid=123)
        process.poll.return_value=None
        process.communicate.side_effect=[subprocess.TimeoutExpired('test',.5),subprocess.TimeoutExpired('test',5)]
        with patch('pavlusha_agent.process_supervision.os.killpg') as kill:
            with self.assertRaises(ProcessCleanupError): stop_owned_process(process)
        self.assertEqual([c.args for c in kill.call_args_list],[(123,signal.SIGTERM),(123,signal.SIGKILL)])


if __name__ == '__main__': unittest.main()
