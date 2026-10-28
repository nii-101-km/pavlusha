"""Controller-owned local llama-server lifecycle; never manage an attached server."""
from __future__ import annotations

from contextlib import contextmanager
import json
import sys
import math
import os
import re
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

from .core import AgentError, ProcessCleanupError


class ManagedLlamaServer:
    def __init__(self, config_path, base_url, log_path, timeout, api_key=""):
        self.base_url = base_url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base_url)
        if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1")
                or parsed.path != "/v1" or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise AgentError("Managed llama-server requires a local http://HOST:PORT/v1 endpoint")
        try:
            self.host, self.port = parsed.hostname, parsed.port or 80
        except ValueError as exc:
            raise AgentError("Invalid managed llama-server port") from exc
        if not math.isfinite(timeout) or timeout <= 0:
            raise AgentError("Managed llama-server requires a finite positive API timeout")
        self.timeout = timeout
        self.api_key = api_key
        try:
            config = json.loads(Path(config_path).read_text())
            argv = config["argv"]
            if (set(config) - {"argv", "preserve_kv", "kv_storage"} or type(config.get("preserve_kv", False)) is not bool
                    or not isinstance(argv, list) or not argv
                    or any(not isinstance(a, str) or not a or "\0" in a for a in argv)):
                raise ValueError("expected argv with nonempty string arguments and optional boolean preserve_kv")
            if config.get("kv_storage", "disk") not in ("disk", "ram"):
                raise ValueError('kv_storage must be "disk" or "ram"')
            if config.get("preserve_kv", False) and any(
                    a == "--no-slots" or a.split("=", 1)[0] == "--slot-save-path" for a in argv):
                raise ValueError("preserve_kv owns --slot-save-path and requires slots enabled")
            executable = Path(argv[0])
            if (not executable.is_absolute() or executable.name != "llama-server"
                    or not executable.is_file() or not os.access(executable, os.X_OK)):
                raise ValueError("argv[0] must be an absolute executable llama-server path")
            model_flags = [i for i, a in enumerate(argv) if a in ("-m", "--model")]
            if len(model_flags) != 1 or model_flags[0] + 1 >= len(argv):
                raise ValueError("exactly one explicit local --model/-m is required")
            model = Path(argv[model_flags[0] + 1])
            if not model.is_absolute() or not model.is_file():
                raise ValueError("model must be an existing absolute local file")
            # This adapter owns one single-model server, never a downloader/router.
            forbidden = ("--hf-", "--model-url", "-mu", "-hf", "-hfr", "-hff", "--models-",
                         "--spec-draft-hf", "-hfd", "-hfrd", "--mmproj-url", "--embd-gemma-default")
            if any(a.startswith(forbidden) for a in argv[1:]):
                raise ValueError("download and router arguments are not supported")
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise AgentError(f"Invalid managed llama-server config: {exc}") from exc
        self.argv = tuple(argv)
        self.preserve_kv = config.get("preserve_kv", False)
        self.kv_storage = config.get("kv_storage", "disk")
        self._kv_directory = None
        self.log_path = Path(log_path)
        self.process = None

    def _json(self, route, payload=None):
        root = self.base_url.removesuffix("/v1")
        headers = {"Authorization": "Bearer " + self.api_key} if self.api_key else {}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(root + route, headers=headers,
                                         data=None if payload is None else json.dumps(payload).encode())
        with urllib.request.urlopen(request, timeout=min(2.0, self.timeout) if payload is None else self.timeout) as response:
            return json.load(response)

    @staticmethod
    def _filesystem_type(path):
        """Resolve the most specific Linux mount; tmpfs may itself be bind-mounted."""
        path = Path(path).resolve()
        matches = []
        for line in Path('/proc/self/mountinfo').read_text().splitlines():
            fields, separator, filesystem = line.partition(' - ')
            if not separator:
                continue
            mountpoint = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields.split()[4])
            mountpoint = Path(mountpoint)
            if path == mountpoint or path.is_relative_to(mountpoint):
                matches.append((len(mountpoint.parts), filesystem.split()[0]))
        if not matches:
            raise AgentError(f'Cannot determine filesystem for KV storage: {path}')
        return max(matches, key=lambda item: item[0])[1]

    def _create_kv_directory(self):
        directory = '/dev/shm' if self.kv_storage == 'ram' else None
        if directory is not None:
            if not Path(directory).is_dir() or self._filesystem_type(directory) != 'tmpfs':
                raise AgentError('RAM KV storage requires /dev/shm on tmpfs; no disk fallback')
        return tempfile.TemporaryDirectory(prefix='pavlusha-kv-', dir=directory)

    def _save_kv(self):
        """Snapshot the sole idle slot; never mix clients or interrupt generation."""
        try:
            slots = self._json("/slots")
            if (not isinstance(slots, list) or len(slots) != 1
                    or not isinstance(slots[0], dict)
                    or slots[0].get("is_processing") is not False
                    or type(slots[0].get("id")) is not int or slots[0]["id"] < 0):
                raise ValueError("preserve_kv requires exactly one idle slot and exclusive server use")
            slot = slots[0]["id"]
            filename = "worker.bin"
            path = Path(self._kv_directory.name) / filename
            path.unlink(missing_ok=True)
            reply = self._json(f"/slots/{slot}?action=save", {"filename": filename})
            count, size = reply.get("n_saved"), reply.get("n_written")
            if (reply.get("id_slot") != slot or reply.get("filename") != filename
                    or type(count) is not int or count < 0 or type(size) is not int or size <= 0
                    or not path.is_file() or path.stat().st_size != size):
                raise ValueError("server did not confirm a complete KV snapshot")
            return slot, filename, count, size
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise AgentError(f"Worker KV save failed; workload was not started: {exc}") from exc

    def _restore_kv(self, snapshot):
        slot, filename, count, size = snapshot
        try:
            reply = self._json(f"/slots/{slot}?action=restore", {"filename": filename})
            if (reply.get("id_slot") != slot or reply.get("filename") != filename
                    or type(reply.get("n_restored")) is not int or reply["n_restored"] != count
                    or type(reply.get("n_read")) is not int or reply["n_read"] != size):
                raise ValueError("server restored a different KV snapshot size or token count")
            (Path(self._kv_directory.name) / filename).unlink()
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise AgentError(f"Worker KV restore failed: {exc}") from exc

    def _ready(self):
        try:
            return self._json("/health").get("status") == "ok"
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def start(self):
        if self.process is not None and self.process.poll() is None:
            if self._ready():
                return
            raise AgentError("Owned llama-server is alive but not ready; refusing a duplicate")
        try:
            with socket.create_connection((self.host, self.port), timeout=1):
                raise AgentError("Managed llama-server endpoint is already occupied; attached servers are not stopped")
        except ConnectionRefusedError:
            pass
        except OSError as exc:
            raise AgentError(f"Cannot establish that llama-server endpoint is free: {exc}") from exc
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            argv = self.argv
            if self.preserve_kv:
                if self._kv_directory is None:
                    self._kv_directory = self._create_kv_directory()
                argv += ("--slots", "--slot-save-path", self._kv_directory.name)
            with self.log_path.open("ab") as output:
                self.process = subprocess.Popen(argv, stdout=output, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, start_new_session=True)
            deadline = time.monotonic() + self.timeout
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise AgentError(f"llama-server exited during startup; see {self.log_path}")
                if self._ready():
                    # Do not accept a response after our owned process has exited.
                    if self.process.poll() is None:
                        return
                time.sleep(.1)
            raise AgentError(f"llama-server startup timed out; see {self.log_path}")
        except BaseException as exc:
            self.stop()
            if isinstance(exc, (OSError, AgentError)):
                raise AgentError(f"Managed llama-server startup failed: {exc}") from exc
            raise

    def stop(self):
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=min(10.0, self.timeout))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=min(5.0, self.timeout))
                except subprocess.TimeoutExpired as exc:
                    raise AgentError("Owned llama-server did not stop; shell must not run") from exc
        self.process = None

    def _confirm_model(self, selected):
        try:
            ids = [item.get("id") for item in self._json("/v1/models")["data"]]
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            raise AgentError(f"Cannot confirm managed llama-server model: {exc}") from exc
        if ids != [selected]:
            raise AgentError("Managed llama-server must expose exactly the selected Worker model")

    @contextmanager
    def released_worker(self, selected, *, restore_guard=None):
        if self.process is None or self.process.poll() is not None:
            raise AgentError("Worker release requires the controller-owned llama-server")
        self._confirm_model(selected)
        snapshot = self._save_kv() if getattr(self, "preserve_kv", False) else None
        entered = False
        try:
            self.stop()
            entered = True
            yield
        finally:
            if isinstance(sys.exc_info()[1], ProcessCleanupError):
                raise  # Owned work is not confirmed dead; keep Worker unloaded.
            if entered and restore_guard is not None:
                restore_guard()  # Do not reload while an external GPU operation remains unresolved.
            try:
                self.start()
                self._confirm_model(selected)
                if snapshot is not None:
                    self._restore_kv(snapshot)
            except (OSError, AgentError) as exc:
                raise AgentError(f"Worker restoration failed; runtime stopped: {exc}") from exc

    def __enter__(self):
        try:
            self.start()
        except BaseException:
            # A failed __enter__ does not receive an automatic __exit__.
            self.__exit__(*sys.exc_info())
            raise
        return self

    def __exit__(self, *exc):
        try:
            self.stop()
        finally:
            if self._kv_directory is not None:
                self._kv_directory.cleanup()
                self._kv_directory = None
