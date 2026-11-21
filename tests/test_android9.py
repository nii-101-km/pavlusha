import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pavlusha_modules.android9 import Android9, create_extension


class Android9Tests(unittest.TestCase):
    def test_environment_must_stay_in_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError): Android9(Path(tmp), '../outside')
            with self.assertRaises(ValueError): Android9(Path(tmp), 'sdk', '../bad')

    def test_missing_sdk_and_stale_lock_fail_without_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(Path(tmp), 'env', allow_network=True, allow_kvm=True)
            self.assertEqual(module.start({})['error'], 'environment_not_ready')
            lock = Path(tmp) / 'env/avd/tv28.avd/hardware-qemu.ini.lock'
            lock.parent.mkdir(parents=True)
            lock.touch()
            with patch.object(module, 'check', return_value={'ready_to_start': True}):
                self.assertEqual(module.start({})['error'], 'avd_lock_present')
            self.assertTrue(lock.exists())

    def test_adb_arguments_are_quoted(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(Path(tmp), 'env', allow_network=True, allow_kvm=True)
            with patch.object(module, '_run', return_value={}) as run:
                module.adb({'args': ['shell', 'echo', '$(touch /tmp/nope)']})
                self.assertIn("'$(touch /tmp/nope)'", run.call_args.args[0])
            self.assertEqual(module.adb({'args': []})['error'], 'invalid_adb_arguments')

    def test_visual_tap_returns_new_image_and_cleans_temporary_file(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(Path(tmp), 'env', allow_network=True, allow_kvm=True)
            def run(command, **kwargs):
                if 'screencap -p' in command:
                    output = command.rsplit(' > ', 1)[1].strip("'\"")
                    Image.new('RGB', (128, 72), 'red').save(Path(tmp) / Path(output).name)
                return {'exit_code': 0, 'timed_out': False}
            with patch.object(module, '_run', side_effect=run) as runner:
                outcome = module.visual('tap', {'x': 32, 'y': 40})
            self.assertEqual(outcome.result['image']['width'], 128)
            self.assertEqual(len(outcome.observations), 1)
            self.assertTrue(outcome.observations[0].image.startswith('data:image/png;base64,'))
            self.assertIn('input tap 32 40', runner.call_args_list[0].args[0])
            self.assertEqual(list(Path(tmp).glob('.android-screen-*')), [])

    def test_visual_reports_input_and_capture_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(Path(tmp), 'env', allow_network=True, allow_kvm=True)
            with patch.object(module, '_run', return_value={'exit_code': 1, 'timed_out': False}) as run:
                self.assertEqual(module.visual('tap', {'x': 1, 'y': 2}).result['error'], 'android_input_failed')
                self.assertEqual(run.call_count, 1)
                self.assertEqual(module.visual('view', {}).result['error'], 'android_screenshot_failed')
            self.assertEqual(module.visual('tap', {'x': -1, 'y': 2}).result['error'], 'invalid_coordinates')
            self.assertEqual(module.visual('text', {'text': '$(bad)'}).result['error'], 'unsupported_text')
            self.assertEqual(module.visual('key', {'key': 'POWER'}).result['error'], 'unsupported_key')
            self.assertEqual(list(Path(tmp).glob('.android-screen-*')), [])

    def test_settings_are_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError): create_extension(settings={'unexpected': True}, context={'workdir': tmp})

class AndroidEnhancementTests(unittest.TestCase):
    def test_permissions_are_explicit_and_checked_before_side_effects(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            for network, kvm in ((False, False), (False, True), (True, False)):
                module = Android9(tmp, allow_network=network, allow_kvm=kvm)
                module.manager = Mock()
                with patch('pavlusha_modules.android9.run_shell') as run:
                    result = module.start({})
                    self.assertEqual(result['error'], 'android_network_not_granted' if not network else 'android_kvm_not_granted')
                    run.assert_not_called()
                    module.manager.call.assert_not_called()
                checks = module.check({})
                self.assertEqual(checks['permissions'], {'network': network, 'kvm': kvm})
                self.assertFalse(checks['ready_to_start'])
            module = Android9(tmp)
            self.assertEqual(module.prepare({})['error'], 'android_network_not_granted')
            with patch('pavlusha_modules.android9.run_shell') as run:
                self.assertEqual(module._run('true', network=True)['error'], 'android_network_not_granted')
                run.assert_not_called()
            with self.assertRaises(ValueError):
                create_extension(settings={'allow_network': 'true'}, context={'workdir': tmp})

    def test_session_manager_receives_only_granted_permissions(self):
        from types import SimpleNamespace
        for network, kvm in ((False, False), (True, False), (False, True), (True, True)):
            with tempfile.TemporaryDirectory() as tmp:
                module = create_extension(settings={'allow_network': network, 'allow_kvm': kvm}, context={'workdir': tmp})
                with patch('pavlusha_modules.android9.ProcessManager') as manager:
                    module.on_event(SimpleNamespace(kind='session_started'))
                    manager.assert_called_once_with(module.workdir, allow_network=network, allow_kvm=kvm)

    def test_build_check_reports_missing_and_selected_jdk_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            missing = {'exit_code': 0, 'stdout': '{"jdk_tools_available": false}', 'timed_out': False}
            with patch.object(module, '_run', return_value=missing) as run:
                self.assertFalse(module.build_check({})['jdk_tools_available'])
                self.assertFalse(run.call_args.kwargs['network'])
            available = {'exit_code': 0, 'stdout': '{"jdk_tools_available": true}', 'timed_out': False}
            with patch.object(module, '_run', return_value=available) as run:
                self.assertTrue(module.build_check({'java_home': '/work/test jdk'})['jdk_tools_available'])
                self.assertIn("'/work/test jdk'", run.call_args.args[0])
            self.assertEqual(module.build_check({'java_home': 'relative'})['error'], 'invalid_java_home')

    def test_image_selection_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            default = Android9(tmp)
            self.assertEqual(default.system_image, 'system-images;android-28;android-tv;x86')
            module = create_extension(settings={'system_image': 'system-images;android-33;google_apis;x86_64'}, context={'workdir': tmp})
            self.assertIn('system_image', module.check({})['missing'])
            with self.assertRaises(ValueError): Android9(tmp, system_image='../../bad')

    def test_status_distinguishes_device_and_boot(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            module.process_id = 'owned'
            module.adb_process_id = 'adb'
            module.manager = Mock()
            module.manager.call.return_value = {'state': 'running'}
            with patch.object(module, '_run', return_value={'exit_code': 1, 'stdout': '', 'stderr': 'offline'}):
                self.assertFalse(module.status({})['adb_visible'])
            with patch.object(module, '_run', side_effect=[{'exit_code': 0}, {'exit_code': 0, 'stdout': 'device', 'stderr': ''}, {'exit_code': 0, 'stdout': '0', 'stderr': ''}]):
                state = module.status({})
                self.assertTrue(state['adb_visible'])
                self.assertFalse(state['boot_completed'])

    def test_element_matching_refuses_missing_and_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            node = {'text': 'Settings', 'bounds': '[0,0][100,40]'}
            with patch.object(module, 'hierarchy', return_value={'nodes': []}), patch.object(module, 'visual') as visual:
                self.assertEqual(module.tap_element({'text': 'Settings'}).result['error'], 'element_not_found')
                visual.assert_not_called()
            with patch.object(module, 'hierarchy', return_value={'nodes': [node, node]}), patch.object(module, 'visual') as visual:
                self.assertEqual(module.tap_element({'text': 'Settings'}).result['error'], 'element_ambiguous')
                visual.assert_not_called()
            with patch.object(module, 'hierarchy', return_value={'nodes': [node]}), patch.object(module, 'visual') as visual:
                module.tap_element({'text': 'Settings'})
                visual.assert_called_once_with('tap', {'x': 50, 'y': 20})

    def test_successful_action_and_failed_capture_are_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            with patch.object(module, '_run', side_effect=[{'exit_code': 0}, {'exit_code': 1}]):
                result = module.visual('tap', {'x': 1, 'y': 2}).result
            self.assertEqual(result['error'], 'android_screenshot_failed')
            self.assertEqual(result['action_result']['exit_code'], 0)

    def test_lock_owner_is_conservative(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            lock = Path(tmp) / '.android-env/avd/tv28.avd/hardware-qemu.ini.lock'
            lock.parent.mkdir(parents=True)
            lock.touch()
            with patch.object(module, 'check', return_value={'ready_to_start': True}), patch.object(module, 'lock_owner', return_value=[123]):
                self.assertEqual(module.start({})['owner'], 'visible_matching_process')
            with patch.object(module, 'check', return_value={'ready_to_start': True}), patch.object(module, 'lock_owner', return_value=[]):
                self.assertEqual(module.start({})['owner'], 'unknown')
            self.assertTrue(lock.exists())

    def test_avd_image_config_and_hierarchy_are_read_fresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            config = Path(tmp) / '.android-env/avd/tv28.avd/config.ini'
            config.parent.mkdir(parents=True)
            config.write_text('image.sysdir.1 = system-images/android-28/android-tv/x86/\n')
            self.assertTrue(module.check({})['checks']['avd_image_matches'])
            config.write_text('image.sysdir.1 = system-images/android-33/google_apis/x86_64/\n')
            self.assertFalse(module.check({})['checks']['avd_image_matches'])
            def capture(command, **kwargs):
                output = command.rsplit(' > ', 1)[1].strip("'\"")
                (Path(tmp) / Path(output).name).write_text('<?xml version="1.0"?><hierarchy><node text="Fresh" bounds="[0,0][2,2]"/></hierarchy>')
                return {'exit_code': 0}
            with patch.object(module, '_run', side_effect=capture):
                self.assertEqual(module.hierarchy({})['nodes'][0]['text'], 'Fresh')
            self.assertEqual(list(Path(tmp).glob('.android-ui-*')), [])

    def test_actions_reject_unowned_or_exited_processes(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            callback = Mock()
            self.assertEqual(module.owned_action(callback).result['error'], 'android_session_not_running')
            module.process_id, module.adb_process_id = 'emulator', 'adb'
            module.manager = Mock()
            module.manager.call.side_effect = [{'state': 'exited'}, {'state': 'running'}]
            self.assertEqual(module.owned_action(callback).result['error'], 'android_session_not_running')
            callback.assert_not_called()

    def test_stopped_adb_is_reported_without_spawning_a_server(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            module.process_id, module.adb_process_id = 'emulator', 'adb'
            module.manager = Mock()
            module.manager.call.side_effect = [{'state': 'running'}, {'state': 'exited'}]
            with patch.object(module, '_run') as run:
                result = module.status({})
            self.assertEqual(result['error'], 'adb_server_not_running')
            self.assertFalse(result['adb_visible'])
            run.assert_not_called()

    def test_restart_stops_owned_adb_after_emulator_exits(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            module.process_id, module.adb_process_id = 'old-emulator', 'old-adb'
            module.manager = Mock()
            module.manager.call.side_effect = [{'state': 'exited'}, {'state': 'exited'}]
            with patch.object(module, 'check', return_value={'ready_to_start': False}):
                self.assertEqual(module.start({})['error'], 'environment_not_ready')
            module.manager.call.assert_any_call('process_stop', {'process_id': 'old-adb'})
            self.assertIsNone(module.adb_process_id)
            self.assertIsNone(module.process_id)

class AndroidSafetyTests(unittest.TestCase):
    def test_adb_mutations_require_explicit_grant_and_server_controls_are_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp)
            with patch.object(module, 'adb', return_value={'exit_code': 0}) as adb:
                self.assertEqual(module.agent_adb({'args': ['shell', 'getprop', 'sys.boot_completed']})['exit_code'], 0)
                adb.assert_called_once()
                for args in (['install', '/work/app.apk'], ['shell', 'pm', 'clear', 'example.app'],
                             ['shell', 'getprop', 'x; reboot']):
                    self.assertEqual(module.agent_adb({'args': args})['error'], 'android_device_changes_not_granted')
                self.assertEqual(module.agent_adb({'args': ['connect', 'example.invalid:5555']})['error'], 'android_adb_command_not_allowed')
                self.assertEqual(module.agent_adb({'args': ['-s', 'other', 'shell', 'id']})['error'], 'android_adb_command_not_allowed')
                self.assertEqual(adb.call_count, 1)
                module.allow_device_changes = True
                self.assertEqual(module.agent_adb({'args': ['install', '/work/app.apk']})['exit_code'], 0)
                self.assertEqual(module.agent_adb({'args': ['kill-server']})['error'], 'android_adb_command_not_allowed')

    def test_prepare_does_not_claim_success_on_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True)
            manager = module.env_path / 'sdk/cmdline-tools/latest/bin/sdkmanager'
            manager.parent.mkdir(parents=True)
            manager.touch()
            with patch.object(module, '_run', return_value={'exit_code': -1, 'timed_out': True}):
                result = module.prepare({})
            self.assertIn('did not complete', result['note'])
            self.assertTrue(result['timed_out'])

    def test_launch_failure_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True)
            with patch('pavlusha_modules.android9.run_shell', side_effect=OSError('missing bubblewrap')):
                result = module._run('true')
            self.assertEqual(result['error'], 'android_command_launch_failed')
            self.assertIn('missing bubblewrap', result['detail'])

    def test_event_host_registers_android_tools_and_rejects_invalid_arguments(self):
        from jsonschema.exceptions import ValidationError
        from pavlusha_agent.extensions import Event, EventHost, InvocationContext
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp)
            host = EventHost([module])
            event = Event('session_started', 's', 0, 0, {})
            host.emit(event)
            try:
                snapshot = host.snapshot()
                context = InvocationContext('s', 1, 0, {})
                outcome = snapshot.invoke({'action': 'android_check'}, context)
                self.assertFalse(outcome.result['ready_to_start'])
                self.assertIn('adb', outcome.result['missing'])
                self.assertEqual(snapshot.invoke({'action': 'android_adb', 'args': ['get-state']}, context).result['error'], 'android_session_not_running')
                with self.assertRaises(ValidationError):
                    snapshot.invoke({'action': 'android_tap', 'x': 'bad', 'y': 1}, context)
                self.assertFalse(snapshot.invoke({'action': 'android_check'}, context).result['ready_to_start'])
            finally:
                host.emit(Event('session_stopped', 's', 1, 0, {}))

    def test_failed_emulator_spawn_is_reported_and_owned_adb_is_stopped(self):
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as tmp:
            module = Android9(tmp, allow_network=True, allow_kvm=True)
            module.manager = Mock()
            module.manager.call.side_effect = [
                {'process_id': 'adb', 'state': 'running'},
                {'process_id': 'emulator', 'state': 'exited', 'exit_code': 1},
                {'process_id': 'adb', 'state': 'exited'},
            ]
            with patch.object(module, 'check', return_value={'ready_to_start': True}), \
                 patch.object(module, '_run', return_value={'exit_code': 0, 'timed_out': False}):
                result = module.start({})
            self.assertEqual(result['error'], 'emulator_start_failed')
            self.assertIsNone(module.process_id)
            module.manager.call.assert_any_call('process_stop', {'process_id': 'adb'})
