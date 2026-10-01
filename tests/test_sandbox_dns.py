"""Resolver mount regressions and explicitly opt-in real network checks."""
import http.server
import os
from pathlib import Path
import shlex
import tempfile
import threading
import unittest
from unittest.mock import patch

from pavlusha_agent.sandbox import build_bwrap_command, run_shell


class ResolverConstructionTests(unittest.TestCase):
    def test_only_resolved_file_is_mounted_readonly_for_shared_network(self):
        with patch('pavlusha_agent.sandbox.Path') as path:
            path.return_value.resolve.return_value=Path('/run/systemd/resolve/stub-resolv.conf')
            with patch('pathlib.Path.is_file',return_value=True):
                args=build_bwrap_command(Path('/tmp/work'),'true',network=True)
            self.assertIn('--share-net',args)
            self.assertIn('--unshare-all',args)
            target='/run/systemd/resolve/stub-resolv.conf'
            i=args.index(target)
            self.assertEqual(args[i-1:i+2],['--ro-bind',target,target])
            self.assertNotIn('/run',args)
            self.assertNotIn('PYTHONPATH',args)
            path.return_value.resolve.assert_called_once_with(strict=True)

    def test_network_off_does_not_inspect_or_mount_resolver_target(self):
        with patch('pavlusha_agent.sandbox.Path') as path:
            args=build_bwrap_command(Path('/tmp/work'),'true',network=False)
            path.assert_not_called()
        self.assertNotIn('--share-net',args)
        self.assertIn('--unshare-all',args)
        self.assertNotIn('/run',args)

    def test_regular_resolver_needs_no_extra_mount(self):
        resolver=Path('/etc/resolv.conf')
        with patch('pavlusha_agent.sandbox.Path',return_value=resolver), \
             patch('pathlib.Path.resolve',return_value=resolver), patch('pathlib.Path.is_file',return_value=True):
            args=build_bwrap_command(Path('/tmp/work'),'true',network=True)
        self.assertNotIn('/etc/resolv.conf',args) # Already included by read-only /etc.

    def test_missing_target_fails_explicitly_and_is_re_resolved_per_command(self):
        with patch('pavlusha_agent.sandbox.Path') as path:
            path.return_value.resolve.side_effect=FileNotFoundError('resolver disappeared')
            with self.assertRaises(FileNotFoundError):
                build_bwrap_command(Path('/tmp/work'),'true',network=True)
            path.return_value.resolve.side_effect=None
            path.return_value.resolve.return_value=Path('/run/new-resolver')
            with patch('pathlib.Path.is_file',return_value=True):
                args=build_bwrap_command(Path('/tmp/work'),'true',network=True)
            self.assertIn('/run/new-resolver',args)


@unittest.skipUnless(os.environ.get('PAVLUSHA_LIVE_NETWORK')=='1',
                     'environment-dependent: set PAVLUSHA_LIVE_NETWORK=1 with bwrap/network permission')
class LiveSandboxNetworkTests(unittest.TestCase):
    def test_getaddrinfo_and_urllib_without_workdir_modifications(self):
        code="""import os, socket, sys, urllib.request
assert 'sitecustomize' not in sys.modules
assert 'PYTHONPATH' not in os.environ
assert socket.getaddrinfo('example.com', 443)
with urllib.request.urlopen('https://example.com', timeout=15) as r:
    assert r.status == 200
    assert r.read(100)
print('dns-and-urllib-ok')
"""
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp)
            result=run_shell(work,'python3 -S -c '+shlex.quote(code),network=True,timeout=25,output_limit=2000)
            self.assertEqual(result.exit_code,0,result.stderr)
            self.assertIn('dns-and-urllib-ok',result.stdout)
            self.assertEqual(list(work.iterdir()),[])

    def test_network_off_cannot_connect_to_host_loopback_and_etc_stays_readonly(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
            def log_message(self,*args):
                pass
        server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            code=f"import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:{server.server_port}', timeout=2).read())"
            with tempfile.TemporaryDirectory() as tmp:
                for network in (True,False):
                    result=run_shell(Path(tmp),'python3 -S -c '+shlex.quote(code),network=network,timeout=5,output_limit=2000)
                    self.assertEqual(result.exit_code==0,network,result.stderr)
                result=run_shell(Path(tmp),'test ! -w /etc/resolv.conf && test ! -e /run/systemd/private',
                                 network=True,timeout=5,output_limit=2000)
                self.assertEqual(result.exit_code,0,result.stderr)
        finally:
            server.shutdown(); server.server_close(); thread.join()
