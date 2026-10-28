"""Session-owned bounded background commands inside the normal work sandbox."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import uuid

from pavlusha_runtime.errors import AgentError, ProcessCleanupError
from pavlusha_runtime.sandbox import build_bwrap_command


def validate_process_action(action):
    kind = action['action']
    fields = {'process_start': {'command', 'network', 'lifetime_seconds'},
              'process_status': {'process_id'}, 'process_logs': {'process_id', 'limit'},
              'process_stop': {'process_id'}}[kind]
    if set(action) - fields - {'action'}:
        raise AgentError('unknown process action field')
    data = {k: v for k, v in action.items() if k != 'action'}
    key = 'command' if kind == 'process_start' else 'process_id'
    if not isinstance(data.get(key), str) or not data[key].strip() or len(data[key]) > 16000:
        raise AgentError(key + ' must be a nonblank bounded string')
    if kind == 'process_start':
        data.setdefault('network', False)
        if type(data['network']) is not bool:
            raise AgentError('network must be boolean')
        data.setdefault('lifetime_seconds', 3600)
        if type(data['lifetime_seconds']) is not int or not 1 <= data['lifetime_seconds'] <= 86400:
            raise AgentError('lifetime_seconds must be 1..86400')
    if kind == 'process_logs':
        data.setdefault('limit', 4000)
        if type(data['limit']) is not int or not 1 <= data['limit'] <= 16000:
            raise AgentError('limit must be 1..16000')
    return kind, data


class ProcessManager:
    """No arbitrary PID controls or adoption of commands from previous sessions."""
    def __init__(self, workdir, *, allow_network=False):
        self.workdir = workdir
        self.allow_network = allow_network
        self.entries = {}
        self.closed = False
        self.cleanup_error = None
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.monitor = threading.Thread(target=self._watch, daemon=True)
        self.monitor.start()

    def _drain(self, entry):
        try:
            while True:
                chunk = os.read(entry['process'].stdout.fileno(), 4096)
                if not chunk:
                    break
                with self.lock:
                    entry['bytes_seen'] += len(chunk)
                    entry['tail'] = (entry['tail'] + chunk)[-65536:]
        finally:
            entry['process'].stdout.close()

    def _stop(self, entry, reason):
        process = entry['process']
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise ProcessCleanupError('background process cleanup unconfirmed') from exc
        entry['reason'] = reason

    def _watch(self):
        while not self.wake.wait(.2):
            with self.lock:
                for entry in self.entries.values():
                    if entry['process'].poll() is None and time.monotonic() >= entry['deadline']:
                        try:
                            self._stop(entry, 'lifetime_expired')
                        except Exception as exc:
                            self.cleanup_error = exc
                            self.wake.set()

    def call(self, kind, data, *, output_limit=8000):
        with self.lock:
            if self.closed:
                return {'error': 'process_manager_closed'}
            if self.cleanup_error is not None:
                raise ProcessCleanupError('background cleanup failed: ' + str(self.cleanup_error))
            if kind == 'process_start':
                if data['network'] and not self.allow_network:
                    return {'error': 'process_network_not_granted',
                            'hint': 'Shared host network requires allow_network:true in the startup toolkit manifest.'}
                if sum(e['process'].poll() is None for e in self.entries.values()) >= 4:
                    return {'error': 'process_limit', 'limit': 4}
                if len(self.entries) >= 64:
                    return {'error': 'process_session_limit', 'limit': 64}
                command = build_bwrap_command(self.workdir, data['command'], network=data['network'])
                try:
                    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                except OSError as exc:
                    return {'error': 'process_spawn_failed', 'detail': str(exc)[:600]}
                identity = 'P' + uuid.uuid4().hex[:12]
                entry = {'process': process, 'tail': b'', 'bytes_seen': 0,
                         'deadline': time.monotonic() + data['lifetime_seconds'],
                         'network': data['network'], 'reason': None}
                entry['reader'] = threading.Thread(target=self._drain, args=(entry,), daemon=True)
                self.entries[identity] = entry
                entry['reader'].start()
                # A returned ID means spawned, not ready. Status/logs and a real
                # HTTP readiness check determine whether the service is usable.
            else:
                identity = data['process_id']
                entry = self.entries.get(identity)
                if entry is None:
                    return {'error': 'unknown_process', 'process_id': identity,
                            'hint': 'IDs are valid only in the current controller session.'}
                if kind == 'process_stop':
                    self._stop(entry, 'stopped')
            process = entry['process']
            payload = {'process_id': identity, 'state': 'running' if process.poll() is None else 'exited',
                       'exit_code': process.returncode, 'network': 'host' if entry['network'] else 'isolated',
                       'reason': entry['reason']}
            if kind == 'process_logs':
                limit = min(data['limit'], max(1, (output_limit - 1000) // 6))
                tail = entry['tail'].decode('utf-8', errors='replace')
                payload.update(output=tail[-limit:], truncated=entry['bytes_seen'] > len(entry['tail']) or len(tail) > limit)
            return payload

    def close(self):
        self.wake.set()
        with self.lock:
            if self.closed and not self.entries:
                return
            self.closed = True
            entries = list(self.entries.values())
            errors = []
            for entry in entries:
                try:
                    self._stop(entry, 'session_closed')
                except Exception as exc:
                    errors.append(exc)
        self.monitor.join(timeout=2)
        if self.monitor.is_alive():
            errors.append(ProcessCleanupError('process monitor cleanup unconfirmed'))
        for entry in entries:
            entry['reader'].join(timeout=2)
            if entry['reader'].is_alive():
                errors.append(ProcessCleanupError('process log reader cleanup unconfirmed'))
        if errors:
            raise errors[0]
        with self.lock:
            self.entries.clear()
