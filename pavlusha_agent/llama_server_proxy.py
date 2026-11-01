"""Allow a child controller to request release through the server's owner."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import socketserver
import threading

from .core import AgentError, ProcessCleanupError


def _send(stream, value):
    stream.write((json.dumps(value) + '\n').encode())
    stream.flush()


def _read(stream):
    line = stream.readline(65537)
    if not line or len(line) > 65536:
        raise AgentError('server control connection closed or invalid')
    value = json.loads(line)
    if type(value) is not dict:
        raise AgentError('invalid server control response')
    return value


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        manager = self.server.manager
        try:
            request = _read(self.rfile)
            if request.get('action') != 'release' or type(request.get('model')) is not str:
                raise AgentError('invalid server control request')
            with self.server.release_lock:
                with manager.released_worker(request['model']):
                    _send(self.wfile, {'status': 'released'})
                    try:
                        response = _read(self.rfile)
                    except Exception as exc:
                        raise ProcessCleanupError('child connection lost; Worker restore blocked') from exc
                    if response.get('action') != 'restore':
                        raise ProcessCleanupError('child cleanup unconfirmed; Worker restore blocked')
                _send(self.wfile, {'status': 'restored'})
        except Exception as exc:
            try:
                _send(self.wfile, {'status': 'error', 'message': str(exc)})
            except OSError:
                pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class LlamaServerControl:
    """Owner-side control endpoint; place it in an owner-only directory."""
    def __init__(self, manager, path):
        self.manager = manager
        self.path = Path(path)
        self.server = None
        self.thread = None

    def __enter__(self):
        self.server = _Server(str(self.path), _Handler)
        os.chmod(self.path, 0o600)
        self.server.manager = self.manager
        self.server.release_lock = threading.Lock()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()
            self.path.unlink(missing_ok=True)


class AttachedLlamaServer:
    """Child-side wrapper with the same release interface as the owner."""
    def __init__(self, base_url, control_path, timeout=300):
        self.base_url = base_url.rstrip('/')
        self.control_path = str(control_path)
        self.timeout = timeout

    @contextmanager
    def released_worker(self, selected, *, restore_guard=None):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(self.timeout)
            sock.connect(self.control_path)
            with sock.makefile('rwb') as stream:
                _send(stream, {'action': 'release', 'model': selected})
                response = _read(stream)
                if response.get('status') != 'released':
                    raise AgentError(response.get('message', 'Worker release failed'))
                try:
                    yield
                finally:
                    import sys
                    error = sys.exc_info()[1]
                    if isinstance(error, ProcessCleanupError):
                        _send(stream, {'action': 'abort'})
                    else:
                        try:
                            if restore_guard is not None:
                                restore_guard()
                        except BaseException:
                            _send(stream, {'action': 'abort'})
                            raise
                        _send(stream, {'action': 'restore'})
                        response = _read(stream)
                        if response.get('status') != 'restored':
                            raise AgentError(response.get('message', 'Worker restore failed'))
