import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from pavlusha_agent.core import AgentError
from pavlusha_agent.persistent_server import (PersistentLlamaServer, _matches, _proc_identity, _read_record,
                                             server_lock, state_key, state_root, stop_record)

ROOT = Path(__file__).resolve().parents[1]


class PersistentServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root/'state'
        self.env = patch.dict(os.environ, {'PAVLUSHA_SERVER_STATE_DIR':str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.server = self.root/'llama-server'
        self.model_path = self.root/'model.gguf'
        self.model_path.touch()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.port = sock.getsockname()[1]
        self.url = f'http://127.0.0.1:{self.port}/v1'
        self.server.write_text('''#!/usr/bin/env python3
import http.server, json
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  body = {"status":"ok"} if self.path == "/health" else {"data":[{"id":"test-model"}]}
  self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(body).encode())
 def do_POST(self):
  self.rfile.read(int(self.headers.get("Content-Length", "0")))
  action = json.dumps({"action":"finish","summary":"done"})
  chunk = {"model":"test-model", "choices":[{"delta":{"content":action},"finish_reason":"stop"}]}
  self.send_response(200); self.send_header("Content-Type","text/event-stream"); self.end_headers()
  self.wfile.write(("data: " + json.dumps(chunk) + "\\n\\ndata: [DONE]\\n\\n").encode())
 def log_message(self, *args): pass
http.server.HTTPServer(('127.0.0.1', %d), Handler).serve_forever()
''' % self.port)
        self.server.chmod(0o700)
        self.config = {'argv':[str(self.server),'--model',str(self.model_path)],'preserve_kv':True}
        self.key = state_key(self.url)
        self.addCleanup(self.cleanup_server)

    def manager(self, model='test-model', config=None):
        root = state_root()
        return PersistentLlamaServer(config or self.config, self.url, root/'server.log', 5, model, root, self.key)

    def cleanup_server(self):
        record = self.state/(self.key+'.json')
        if record.exists():
            try: stop_record(self.state, record, timeout=3)
            except Exception: pass

    def test_start_detach_reuse_then_stop(self):
        root = state_root()
        with server_lock(root, self.key):
            first = self.manager()
            self.assertEqual(first.attach_or_start(), 'started')
            child = first.process
            pid, kv = child.pid, first.prepare_kv_directory()
            first.__exit__(None,None,None)
        os.kill(pid, 0)
        with server_lock(root, self.key):
            second = self.manager()
            self.assertEqual(second.attach_or_start(), 'reused')
            self.assertEqual(second.process.pid, pid)
            self.assertEqual(second.prepare_kv_directory(), kv)
            second.__exit__(None,None,None)
        with server_lock(root, self.key):
            stop_record(root, root/(self.key+'.json'))
        self.assertFalse(Path(kv).exists())
        self.assertFalse((root/(self.key+'.json')).exists())
        # Reap the direct child in this test process.
        child.wait(timeout=3)

    def test_incompatible_settings_do_not_replace_or_stop_server(self):
        first=self.manager()
        self.assertEqual(first.attach_or_start(), 'started')
        child=first.process
        pid=child.pid
        first.__exit__(None,None,None)
        bad=self.manager(config={'argv':[str(self.server),'--model',str(self.model_path),'--changed'], 'preserve_kv':True})
        with self.assertRaisesRegex(AgentError, 'does not match'):
            bad.attach_or_start()
        os.kill(pid,0)
        with self.assertRaisesRegex(AgentError, 'does not match'):
            self.manager(model='wrong').attach_or_start()
        stop_record(state_root(), state_root()/(self.key+'.json'))
        child.wait(timeout=3)

    def test_lock_prevents_concurrent_use_or_stop(self):
        root=state_root()
        with server_lock(root,self.key):
            with self.assertRaisesRegex(AgentError,'already in use'):
                with server_lock(root,self.key): pass

    def test_stop_cli_targets_registered_server(self):
        manager=self.manager()
        self.assertEqual(manager.attach_or_start(), 'started')
        child=manager.process
        pid=child.pid
        manager.__exit__(None,None,None)
        result=subprocess.run([sys.executable,str(ROOT/'stop_server.py')],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse((self.state/(self.key+'.json')).exists())
        child.wait(timeout=3)

    def test_stale_identity_never_signals_other_process(self):
        root=state_root(); path=root/(self.key+'.json')
        fake={'version':1,'base_url':self.url,'pid':os.getpid(),'identity':{'start':'wrong'},
              'fingerprint':'x','model':'x','kv_directory':None}
        path.write_text(json.dumps(fake))
        self.assertFalse(_matches(_read_record(path)))
        stop_record(root,path)
        self.assertFalse(path.exists())

    def test_assembly_reuses_server_between_processes(self):
        config_path = self.root/'assembly.json'
        config_path.write_text(json.dumps({'runtime':{'model':'test-model', 'base_url':self.url,
            'backend':'llama-cpp', 'api_timeout':5, 'interactive':False, 'llama_server':self.config},
            'modules':[{'factory':'pavlusha_modules.finish:create_extension'}]}))
        work=self.root/'work'; work.mkdir()
        for index in (1,2):
            result=subprocess.run([sys.executable, str(ROOT/'project_state_agent.py'), '--config', str(config_path),
                '--workdir',str(work),'--session-dir',str(self.root/f'session-{index}'),
                '--keep-server','--',f'test-{index}'], cwd=ROOT, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode,0,result.stderr)
            state=json.loads((self.root/f'session-{index}'/'session.json').read_text())
            self.assertEqual(state['status'],'finished')
            record=_read_record(state_root()/(self.key+'.json'))
            if index==1: initial=record['pid']
            else: self.assertEqual(record['pid'],initial)
        self.assertTrue(_matches(record))

    def test_default_assembly_stops_owned_server(self):
        config_path = self.root/'assembly.json'
        config_path.write_text(json.dumps({'runtime':{'model':'test-model', 'base_url':self.url,
            'backend':'llama-cpp', 'api_timeout':5, 'interactive':False, 'llama_server':self.config},
            'modules':[{'factory':'pavlusha_modules.finish:create_extension'}]}))
        work=self.root/'work'; work.mkdir()
        result=subprocess.run([sys.executable, str(ROOT/'project_state_agent.py'), '--config', str(config_path),
            '--workdir',str(work),'--session-dir',str(self.root/'session'), '--','test'],
            cwd=ROOT, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        with socket.socket() as sock:
            sock.settimeout(.3)
            self.assertNotEqual(sock.connect_ex(('127.0.0.1',self.port)),0)
        self.assertFalse(self.state.exists())

    def test_matching_external_server_is_never_registered_or_stopped(self):
        process=subprocess.Popen([str(self.server)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            manager=self.manager()
            for _ in range(100):
                if manager._ready(): break
                time.sleep(.02)
            with self.assertRaisesRegex(AgentError, 'settings cannot be verified'):
                manager.attach_or_start()
            self.assertFalse((self.state/(self.key+'.json')).exists())
            manager.__exit__(None,None,None)
            self.assertIsNone(process.poll())
        finally:
            process.terminate()
            process.wait(timeout=5)

    def test_invalid_control_configuration_does_not_start_server(self):
        config_path=self.root/'assembly.json'
        invalid={'argv':self.config['argv'], 'preserve_kv':False}
        config_path.write_text(json.dumps({'runtime':{'model':'test-model', 'base_url':self.url,
            'backend':'llama-cpp', 'api_timeout':5, 'interactive':False, 'enable_server_control':True,
            'llama_server':invalid}, 'modules':[{'factory':'pavlusha_modules.finish:create_extension'}]}))
        work=self.root/'work'; work.mkdir()
        result=subprocess.run([sys.executable,str(ROOT/'project_state_agent.py'),'--config',str(config_path),
            '--workdir',str(work),'--session-dir',str(self.root/'session'),'--keep-server','--','test'],
            cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('requires preserve_kv',result.stderr)
        self.assertFalse((self.state/(self.key+'.json')).exists())
        with socket.socket() as sock:
            sock.settimeout(.3)
            self.assertNotEqual(sock.connect_ex(('127.0.0.1',self.port)),0)

    def test_normal_termination_cleans_managed_server(self):
        import signal
        marker=self.root/'request-started'
        pidfile=self.root/'server-pid'
        self.server.write_text('''#!/usr/bin/env python3
import http.server, os, time
from pathlib import Path
Path(%r).write_text(str(os.getpid()))
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  self.send_response(200); self.end_headers(); self.wfile.write(b'{"status":"ok"}')
 def do_POST(self):
  self.rfile.read(int(self.headers.get("Content-Length", "0")))
  Path(%r).write_text("waiting")
  time.sleep(60)
 def log_message(self,*args): pass
http.server.HTTPServer(('127.0.0.1', %d),Handler).serve_forever()
''' % (str(pidfile),str(marker),self.port))
        self.server.chmod(0o700)
        config_path=self.root/'assembly.json'
        config_path.write_text(json.dumps({'runtime':{'model':'test-model','base_url':self.url,'backend':'llama-cpp',
            'api_timeout':10,'interactive':False,'llama_server':self.config},
            'modules':[{'factory':'pavlusha_modules.finish:create_extension'}]}))
        work=self.root/'work';work.mkdir()
        process=subprocess.Popen([sys.executable,str(ROOT/'project_state_agent.py'),'--config',str(config_path),
            '--workdir',str(work),'--session-dir',str(self.root/'session'),'--','test'],cwd=ROOT,
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            end=time.monotonic()+15
            while not marker.exists() and process.poll() is None and time.monotonic()<end: time.sleep(.05)
            self.assertTrue(marker.exists(), 'model request did not start')
            server_pid=int(pidfile.read_text())
            process.send_signal(signal.SIGTERM)
            self.assertEqual(process.wait(timeout=15),130)
            self.assertIsNone(_proc_identity(server_pid))
            state=json.loads((self.root/'session/session.json').read_text())
            self.assertEqual(state['status'],'cancelled')
        finally:
            if process.poll() is None:
                process.kill();process.wait(timeout=5)
