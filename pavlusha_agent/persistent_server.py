"""Opt-in reuse of a detached local llama-server, without a supervising daemon."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import tempfile
import time

from .core import AgentError
from .llama_server import ManagedLlamaServer


def state_root():
    configured = os.environ.get('PAVLUSHA_SERVER_STATE_DIR')
    root = Path(configured).expanduser() if configured else Path.home() / '.local/state/pavlusha/servers'
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    stat = root.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise AgentError('server state directory must be owned by the current user and private')
    return root


def state_key(url):
    return hashlib.sha256(url.rstrip('/').encode()).hexdigest()[:32]


@contextmanager
def server_lock(root, key):
    path = root / (key + '.lock')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentError('this persistent server is already in use') from None
        yield


def _proc_identity(pid):
    try:
        proc = Path('/proc') / str(pid)
        stat = (proc / 'stat').read_text()
        fields = stat.rsplit(')', 1)[1].split()
        if fields[0] == 'Z' or proc.stat().st_uid != os.getuid():
            return None
        return {'start': fields[19], 'command_sha256': hashlib.sha256((proc / 'cmdline').read_bytes()).hexdigest(),
                'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError, ValueError):
        return None


def _matches(record):
    return _proc_identity(record['pid']) == record['identity']


def _assert_identity_readable_or_gone(pid):
    """Do not discard ownership state if an existing process cannot be inspected."""
    if _proc_identity(pid) is not None:
        return
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise AgentError('cannot inspect recorded server process; state retained') from exc
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return
    except (OSError, IndexError):
        pass
    raise AgentError('cannot inspect recorded server process; state retained')


def _read_record(path):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if (type(record) is not dict or record.get('version') != 1 or type(record.get('pid')) is not int
                or record['pid'] < 2 or type(record.get('identity')) is not dict
                or type(record.get('base_url')) is not str or type(record.get('fingerprint')) is not str
                or type(record.get('model')) is not str
                or (record.get('kv_directory') is not None and type(record['kv_directory']) is not str)):
            raise ValueError('invalid record')
        return record
    except (OSError, ValueError, TypeError) as exc:
        raise AgentError('cannot read persistent server state: ' + str(exc)) from exc


def _signal_record(record, timeout=15):
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise AgentError('safe persistent server stopping requires Linux pidfd support')
    try:
        fd = os.pidfd_open(record['pid'])
    except ProcessLookupError:
        return
    try:
        if not _matches(record):
            raise AgentError('server process identity changed; refusing to stop it')
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not _matches(record):
                return
            time.sleep(.05)
        raise AgentError('server did not stop after SIGTERM; state was retained')
    finally:
        os.close(fd)


class PersistentDirectory:
    def __init__(self, path): self.name = str(path)
    def cleanup(self): pass  # Explicitly owned until stop_server.py is called.


class AdoptedProcess:
    def __init__(self, record): self.record, self.pid = record, record['pid']
    def poll(self): return None if _matches(self.record) else 0


class PersistentLlamaServer(ManagedLlamaServer):
    def __init__(self, config, base_url, log_path, timeout, model, root, key, api_key=''):
        super().__init__(config, base_url, log_path, timeout, api_key=api_key)
        self.model, self.root, self.key = model, root, key
        self.record_path = root / (key + '.json')
        self.record = None
        self.fingerprint = hashlib.sha256(json.dumps({'argv':self.argv, 'preserve_kv':self.preserve_kv,
            'kv_storage':self.kv_storage}, sort_keys=True).encode()).hexdigest()

    def prepare_kv_directory(self):
        if not self.preserve_kv:
            return None
        if self._kv_directory is None:
            base = '/dev/shm' if self.kv_storage == 'ram' else str(self.root)
            if self.kv_storage == 'ram' and self._filesystem_type(base) != 'tmpfs':
                raise AgentError('RAM KV storage requires /dev/shm on tmpfs; no disk fallback')
            self._kv_directory = PersistentDirectory(tempfile.mkdtemp(prefix='pavlusha-persistent-kv-', dir=base))
        return self._kv_directory.name

    def _write_record(self):
        identity = _proc_identity(self.process.pid)
        if identity is None:
            raise AgentError('cannot confirm server process identity')
        self.record = {'version':1, 'base_url':self.base_url, 'pid':self.process.pid, 'identity':identity,
                       'fingerprint':self.fingerprint, 'model':self.model,
                       'kv_directory': self._kv_directory.name if self._kv_directory else None}
        path = self.record_path
        fd, tmp = tempfile.mkstemp(prefix=path.name + '.', dir=self.root)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                json.dump(self.record, handle, ensure_ascii=False)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(tmp, path)
            directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try: os.fsync(directory)
            finally: os.close(directory)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def start(self):
        super().start()
        try:
            self._confirm_model(self.model)
            self._write_record()
        except BaseException:
            super().stop()
            raise

    def stop(self):
        process = self.process
        if process is None: return
        if isinstance(process, AdoptedProcess):
            _signal_record(process.record, timeout=min(15, self.timeout))
            self.process = None
        else:
            super().stop()

    def attach_or_start(self):
        if self.record_path.exists():
            record = _read_record(self.record_path)
            if record['base_url'] != self.base_url:
                raise AgentError('persistent server state address mismatch')
            if _matches(record):
                if record['fingerprint'] != self.fingerprint or record['model'] != self.model:
                    raise AgentError('running persistent server does not match adapter configuration')
                kv = record.get('kv_directory')
                if self.preserve_kv:
                    target = Path(kv) if kv else None
                    expected_parent = Path('/dev/shm') if self.kv_storage == 'ram' else self.root
                    if (target is None or target.parent != expected_parent
                            or not target.name.startswith('pavlusha-persistent-kv-')
                            or target.is_symlink() or not target.is_dir() or target.stat().st_uid != os.getuid()):
                        raise AgentError('persistent server KV directory is unavailable or unsafe')
                elif kv is not None:
                    raise AgentError('unexpected persistent server KV directory')
                self._kv_directory = PersistentDirectory(kv) if kv else None
                self.record, self.process = record, AdoptedProcess(record)
                if not self._ready(): raise AgentError('persistent server is not ready')
                self._confirm_model(self.model)
                return 'reused'
            # Stale records never signal a different process; their private KV is no longer in use.
            stop_record(self.root, self.record_path)
        try:
            with socket.create_connection((self.host, self.port), timeout=1):
                occupied = True
        except ConnectionRefusedError:
            occupied = False
        except OSError as exc:
            raise AgentError('cannot check server endpoint: ' + str(exc)) from exc
        if occupied:
            if not self._ready(): raise AgentError('endpoint is occupied by an incompatible service')
            self._confirm_model(self.model)
            raise AgentError('existing server is not registered by Pavlusha; adapter settings cannot be verified')
        self.start()
        return 'started'

    def __exit__(self, *exc):
        # Deliberately leave the registered server and KV directory alive.
        self.process = None
        self._kv_directory = None


def stop_record(root, path, timeout=15):
    record = _read_record(path)
    if _matches(record):
        _signal_record(record, timeout=timeout)
    else:
        _assert_identity_readable_or_gone(record['pid'])
    path.unlink(missing_ok=True)
    kv = record.get('kv_directory')
    if kv:
        target = Path(kv)
        allowed_parent = Path('/dev/shm') if target.parent == Path('/dev/shm') else root
        if (target.parent == allowed_parent and target.name.startswith('pavlusha-persistent-kv-')
                and not target.is_symlink() and target.is_dir() and target.stat().st_uid == os.getuid()):
            shutil.rmtree(target)
    return record
