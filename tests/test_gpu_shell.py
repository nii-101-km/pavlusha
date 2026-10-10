"""Opt-in NVIDIA compute nodes without changing ordinary shell isolation."""
import subprocess
import signal
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from pavlusha_agent.core import AgentError
from pavlusha_agent.sandbox import (_nvidia_compute_devices, build_bwrap_command,
                                   run_shell, validate_action)


class GpuShellTests(unittest.TestCase):
    def test_default_and_false_are_identical_and_do_not_inspect_devices(self):
        with patch('pavlusha_agent.sandbox._nvidia_compute_devices') as devices:
            ordinary=build_bwrap_command(Path('/private/work'),'true',network=False)
            explicit=build_bwrap_command(Path('/private/work'),'true',network=False,gpu=False)
            devices.assert_not_called()
        self.assertEqual(ordinary,explicit)
        self.assertNotIn('--dev-bind',ordinary)
        base={'action':'shell','command':'true'}
        self.assertEqual(validate_action(base,300),validate_action({**base,'gpu':False},300))
        self.assertNotIn('gpu',validate_action(base,300)[1])

    def test_gpu_boolean_validation_and_independent_release_and_timeout(self):
        base={'action':'shell','command':'true','timeout':1800}
        for invalid in (None, 0, 1, 'true', [], {}):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(AgentError,'shell.gpu'):
                validate_action({**base,'gpu':invalid},300)
        for release in (False, True):
            kind,data=validate_action({**base,'gpu':True,'release_worker':release},300)
            self.assertEqual(kind,'shell')
            self.assertTrue(data['gpu'])
            self.assertEqual(data.get('release_worker',False),release)
            self.assertEqual(data['timeout'],300)

    def test_only_compute_device_binds_are_added_after_private_dev(self):
        nodes=[Path(p) for p in ('/dev/nvidiactl','/dev/nvidia-uvm','/dev/nvidia0','/dev/nvidia1')]
        for network in (False, True):
            with self.subTest(network=network), \
                 patch('pavlusha_agent.sandbox._nvidia_compute_devices',return_value=nodes) as discover:
                default=build_bwrap_command(Path('/private/work'),'true',network=network)
                gpu=build_bwrap_command(Path('/private/work'),'true',network=network,gpu=True)
            discover.assert_called_once_with()
            i=default.index('--tmpfs')
            extra=[part for node in nodes for part in ('--dev-bind',str(node),str(node))]
            self.assertEqual(gpu,default[:i]+extra+default[i:])
            self.assertEqual(gpu[i-2:i],['--dev','/dev'])
            for forbidden in ('/sys','/run','/dev/dri','/dev/nvidia-modeset','/dev/nvidia-uvm-tools'):
                self.assertNotIn(forbidden,gpu)
            self.assertNotIn(('--dev-bind','/dev'),list(zip(gpu,gpu[1:])))

    def test_discovery_filters_names_and_rejects_nondevices_or_symlinks(self):
        candidates=[Path('/dev/'+name) for name in ('nvidia1','nvidia0','nvidia0-other','nvidia-modeset','nvidia-uvm-tools')]
        with patch('pathlib.Path.glob',return_value=candidates), \
             patch('pathlib.Path.is_char_device',return_value=True), \
             patch('pathlib.Path.is_symlink',return_value=False):
            self.assertEqual(_nvidia_compute_devices(),[
                Path('/dev/nvidiactl'),Path('/dev/nvidia-uvm'),Path('/dev/nvidia0'),Path('/dev/nvidia1')])
        for is_symlink, is_device in ((True,True),(False,False)):
            with self.subTest(symlink=is_symlink,device=is_device), \
                 patch('pathlib.Path.glob',return_value=[Path('/dev/nvidia0')]), \
                 patch('pathlib.Path.is_char_device',return_value=is_device), \
                 patch('pathlib.Path.is_symlink',return_value=is_symlink), \
                 self.assertRaisesRegex(OSError,'character device'):
                _nvidia_compute_devices()
        with patch('pathlib.Path.glob',return_value=[]), self.assertRaisesRegex(OSError,'no numbered'):
            _nvidia_compute_devices()

    def test_device_failure_does_not_launch_a_command(self):
        with patch('pavlusha_agent.sandbox._nvidia_compute_devices',side_effect=OSError('missing UVM')), \
             patch('pavlusha_agent.sandbox.subprocess.Popen') as popen, \
             self.assertRaisesRegex(OSError,'missing UVM'):
            run_shell(Path('/private/work'),'true',network=False,gpu=True,timeout=3)
        popen.assert_not_called()

    def test_gpu_does_not_change_launch_or_process_group_timeout_escalation(self):
        for gpu in (False,True):
            with self.subTest(gpu=gpu):
                process=Mock(pid=123,returncode=-signal.SIGKILL)
                process.communicate.side_effect=[subprocess.TimeoutExpired('cmd',1),
                    subprocess.TimeoutExpired('cmd',.5),('partial','error')]
                with patch('pavlusha_agent.sandbox.build_bwrap_command',return_value=['bwrap','cmd']) as build, \
                     patch('pavlusha_agent.sandbox.subprocess.Popen',return_value=process) as popen, \
                     patch('pavlusha_agent.sandbox.os.killpg') as killpg:
                    result=run_shell(Path('/private/work'),'cmd',network=False,gpu=gpu,timeout=1)
                build.assert_called_once_with(Path('/private/work'),'cmd',network=False,gpu=gpu)
                self.assertTrue(popen.call_args.kwargs['start_new_session'])
                self.assertEqual([call.args for call in killpg.call_args_list],[(123,signal.SIGTERM),(123,signal.SIGKILL)])
                self.assertEqual([call.kwargs for call in process.communicate.call_args_list],[{'timeout':1},{'timeout':.5},{}])
                self.assertTrue(result.timed_out)
                self.assertEqual(result.stdout,'partial')
