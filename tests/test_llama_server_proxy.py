from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from pavlusha_agent.core import AgentError, ProcessCleanupError
from pavlusha_agent.llama_server_proxy import AttachedLlamaServer, LlamaServerControl
from pavlusha_agent.adapters import ChatProvider


class Manager:
    def __init__(self):
        self.events = []

    @contextmanager
    def released_worker(self, selected):
        self.events.append(('stop', selected))
        try:
            yield
        except ProcessCleanupError:
            self.events.append(('blocked', selected))
            raise
        self.events.append(('start', selected))


class ProxyTests(unittest.TestCase):
    def test_child_release_uses_owner_and_restores_before_return(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager()
            path = Path(directory) / 'control.sock'
            with LlamaServerControl(manager, path):
                child = AttachedLlamaServer('http://localhost:8899/v1', path)
                provider = ChatProvider('http://localhost:8899/v1', 'model', '', 5, 0.1, 100,
                                        llama_server=child, backend='llama-cpp')
                with provider.released_worker():
                    self.assertEqual(manager.events, [('stop', 'model')])
                self.assertEqual(manager.events, [('stop', 'model'), ('start', 'model')])
            self.assertFalse(path.exists())

    def test_failed_child_cleanup_does_not_restart_server(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = Manager()
            path = Path(directory) / 'control.sock'
            with LlamaServerControl(manager, path):
                child = AttachedLlamaServer('http://localhost/v1', path)
                with self.assertRaises(ProcessCleanupError):
                    with child.released_worker('model'):
                        raise ProcessCleanupError('cleanup failed')
            self.assertEqual(manager.events, [('stop', 'model'), ('blocked', 'model')])

    def test_failed_release_is_reported_to_child(self):
        class FailingManager:
            @contextmanager
            def released_worker(self, selected):
                raise AgentError('cannot save')
                yield
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'control.sock'
            with LlamaServerControl(FailingManager(), path):
                with self.assertRaisesRegex(AgentError, 'cannot save'):
                    with AttachedLlamaServer('http://localhost/v1', path).released_worker('model'):
                        self.fail('must not execute')
