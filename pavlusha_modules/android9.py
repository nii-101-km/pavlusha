"""Session-owned Android 9 emulator and ADB operations in the workspace sandbox."""
import os
import shlex
import tempfile
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from pavlusha_agent.extensions import Contribution, Outcome, Observation
from pavlusha_agent.sandbox import run_shell, admit_shell_result
from .services import ProcessManager, validate_process_action, read_image
from .util import declare, TEXT, INTEGER


class Android9:
    name = 'android9'

    def __init__(self, workdir, env_dir='.android-env', avd='tv28', output_limit=24000, system_image='system-images;android-28;android-tv;x86', *, allow_network=False, allow_kvm=False, allow_device_changes=False):
        self.workdir = Path(workdir).resolve()
        self.env_dir = env_dir
        self.env_path = (self.workdir / env_dir).resolve()
        if not self.env_path.is_relative_to(self.workdir) or self.env_path == self.workdir:
            raise ValueError('env_dir must be inside the work directory')
        if not avd or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in avd):
            raise ValueError('invalid AVD name')
        if not re.fullmatch(r'system-images;android-[0-9]+;[A-Za-z0-9_-]+;[A-Za-z0-9_-]+', system_image):
            raise ValueError('invalid system image package')
        if any(type(value) is not bool for value in (allow_network, allow_kvm, allow_device_changes)):
            raise ValueError('Android permissions must be boolean')
        self.allow_network = allow_network
        self.allow_kvm = allow_kvm
        self.allow_device_changes = allow_device_changes
        self.system_image = system_image
        self.avd = avd
        self.root = '/work/' + self.env_path.relative_to(self.workdir).as_posix()
        self.output_limit = output_limit
        self.manager = None
        self.process_id = None
        self.adb_process_id = None

    def _environment(self):
        q = shlex.quote
        return 'export ' + ' '.join(f'{k}={q(v)}' for k, v in {
            'HOME': self.root + '/home', 'ANDROID_HOME': self.root + '/sdk',
            'ANDROID_SDK_ROOT': self.root + '/sdk', 'ANDROID_AVD_HOME': self.root + '/avd',
            'ANDROID_USER_HOME': self.root + '/home', 'GRADLE_USER_HOME': self.root + '/gradle-cache',
            'TMPDIR': self.root + '/tmp', 'XDG_CACHE_HOME': self.root + '/home/.cache',
            'ADB_SERVER_PORT': '5038',
        }.items()) + '; '

    def _run(self, command, *, network=True, kvm=False, timeout=60):
        if network and not self.allow_network:
            return {'error': 'android_network_not_granted', 'hint': 'Set allow_network:true in the Android module settings.',
                    'exit_code': None, 'timed_out': False, 'stdout': '', 'stderr': ''}
        if kvm and not self.allow_kvm:
            return {'error': 'android_kvm_not_granted', 'hint': 'Set allow_kvm:true in the Android module settings.',
                    'exit_code': None, 'timed_out': False, 'stdout': '', 'stderr': ''}
        try:
            result = run_shell(self.workdir, self._environment() + command,
                               network=network, kvm=kvm, timeout=timeout)
        except OSError as exc:
            return {'error': 'android_command_launch_failed', 'detail': str(exc),
                    'exit_code': None, 'timed_out': False, 'stdout': '', 'stderr': ''}
        return admit_shell_result(result, self.output_limit)

    def check(self, args):
        checks = {name: (self.env_path / rel).is_file() for name, rel in {
            'emulator': 'sdk/emulator/emulator', 'adb': 'sdk/platform-tools/adb',
            'sdkmanager': 'sdk/cmdline-tools/latest/bin/sdkmanager',
            'avdmanager': 'sdk/cmdline-tools/latest/bin/avdmanager',
            'system_image': 'sdk/' + self.system_image.replace(';', '/') + '/source.properties',
            'avd': 'avd/' + self.avd + '.ini',
        }.items()}
        config = self.env_path / 'avd' / (self.avd + '.avd') / 'config.ini'
        actual_image = None
        if config.is_file():
            try:
                for line in config.read_text(encoding='utf-8').splitlines():
                    key, sep, value = line.partition('=')
                    if sep and key.strip() == 'image.sysdir.1':
                        actual_image = value.strip().strip('/').replace('/', ';')
                        break
            except OSError:
                pass
        checks['avd_image_matches'] = actual_image == self.system_image if actual_image else False
        checks['kvm'] = Path('/dev/kvm').is_char_device() and os.access('/dev/kvm', os.R_OK | os.W_OK)
        return {'checks': checks, 'ready_to_start': self.allow_network and self.allow_kvm and all(checks[k] for k in ('emulator', 'adb', 'system_image', 'avd', 'avd_image_matches', 'kvm')), 'permissions': {'network': self.allow_network, 'kvm': self.allow_kvm}, 'avd_image': actual_image, 'missing': [k for k, v in checks.items() if not v], 'system_image': self.system_image, 'env_dir': self.env_dir,
                'kvm_scope': 'controller environment only; absence here does not establish host availability; separate shell actions must explicitly request kvm:true',
                'note': 'Use android_start to run with KVM and shared ADB network. android_prepare installs missing packages with the local sdkmanager; command-line tools must already be installed.'}

    def prepare(self, args):
        if not self.allow_network:
            return {'error': 'android_network_not_granted', 'hint': 'SDK preparation requires allow_network:true in Android settings.'}
        manager = self.env_path / 'sdk/cmdline-tools/latest/bin/sdkmanager'
        if not manager.is_file():
            return {'error': 'sdkmanager_missing', 'hint': 'Install official Android command-line tools under env_dir/sdk/cmdline-tools/latest first.'}
        q = shlex.quote
        base = q(self.root)
        sdkmanager = q(self.root + '/sdk/cmdline-tools/latest/bin/sdkmanager')
        avdmanager = q(self.root + '/sdk/cmdline-tools/latest/bin/avdmanager')
        cmd = (f'mkdir -p {base}/home {base}/tmp {base}/avd {base}/gradle-cache; '
               f'set +o pipefail; yes | {sdkmanager} --sdk_root={base}/sdk '
               "'platform-tools' 'platforms;android-28' 'platforms;android-34' "
               f"'build-tools;34.0.0' 'emulator' {q(self.system_image)}; "
               'rc=${PIPESTATUS[1]}; if [ "$rc" -ne 0 ]; then exit "$rc"; fi; '
               f'if [ ! -f {base}/avd/{q(self.avd)}.ini ]; then '
               f'echo no | {avdmanager} create avd -n {q(self.avd)} '
               f"-k {q(self.system_image)}" + (" -d tv_720p" if self.system_image == 'system-images;android-28;android-tv;x86' else "") + "; fi")
        result = self._run(cmd, timeout=1800)
        result['note'] = ('SDK preparation completed; licenses were accepted by this explicit action.'
                          if result.get('exit_code') == 0 and not result.get('timed_out')
                          else 'SDK preparation did not complete; inspect the command result.')
        return result

    def build_check(self, args):
        """Inspect the selected JDK inside the same sandbox used for builds."""
        home = args.get('java_home', '')
        if not isinstance(home, str) or '\x00' in home or (home and not home.startswith('/')):
            return {'error': 'invalid_java_home', 'hint': 'Use an absolute path visible inside the sandbox.'}
        script = '''import json, os, shutil, sys
from pathlib import Path
home = sys.argv[1]
java = Path(home) / 'bin/java' if home else shutil.which('java')
java = Path(java).resolve() if java else None
root = Path(home).resolve() if home else (java.parent.parent if java else None)
tools = {}
for name in ('java', 'javac', 'jlink'):
    path = root / 'bin' / name if root else None
    tools[name] = {'path': str(path) if path else None,
                   'available': bool(path and path.is_file() and os.access(path, os.X_OK))}
print(json.dumps({'java_home': str(root) if root else None, 'tools': tools,
                  'jdk_tools_available': all(v['available'] for v in tools.values())}))
'''
        result = self._run('python3 -c ' + shlex.quote(script) + ' ' + shlex.quote(home), network=False, timeout=15)
        if result.get('exit_code') != 0 or result.get('timed_out'):
            return {'error': 'android_build_check_failed', 'check_result': result}
        try:
            import json
            details = json.loads(result['stdout'])
        except (KeyError, ValueError):
            return {'error': 'android_build_check_invalid', 'check_result': result}
        details['note'] = ('Checks executable presence only; the selected JDK must also be compatible with the build tools. '
                           'Use java_home to check the JDK selected for Gradle; this does not affect emulator readiness.')
        return details

    def lock_owner(self):
        # A visible matching process is evidence of use; no match is not proof of staleness.
        matches = []
        for proc in Path('/proc').iterdir():
            if not proc.name.isdigit():
                continue
            try:
                argv = (proc / 'cmdline').read_bytes().split(b'\0')
                decoded = [x.decode('utf-8', 'replace') for x in argv if x]
                if any('qemu' in Path(x).name or 'emulator' in Path(x).name for x in decoded[:1]) and any(
                    decoded[i:i+2] == ['-avd', self.avd] for i in range(len(decoded))):
                    matches.append(int(proc.name))
            except (OSError, ValueError):
                continue
        return matches

    def start(self, args):
        if not self.allow_network:
            return {'error': 'android_network_not_granted', 'hint': 'ADB requires allow_network:true in Android settings.'}
        if not self.allow_kvm:
            return {'error': 'android_kvm_not_granted', 'hint': 'Emulator requires allow_kvm:true in Android settings.'}
        if self.process_id:
            state = self.manager.call('process_status', {'process_id': self.process_id})
            if state['state'] == 'running':
                return {'error': 'already_running', **state}
            if self.adb_process_id:
                self.manager.call('process_stop', {'process_id': self.adb_process_id})
                self.adb_process_id = None
            self.process_id = None
        checks = self.check({})
        if not checks['ready_to_start']:
            return {'error': 'environment_not_ready', **checks}
        lock = self.env_path / 'avd' / (self.avd + '.avd') / 'hardware-qemu.ini.lock'
        if lock.exists():
            owners = self.lock_owner()
            return {'error': 'avd_lock_present', 'path': str(lock), 'owner': 'visible_matching_process' if owners else 'unknown', 'matching_pids': owners, 'owner_note': 'Process visibility may be restricted by the sandbox; lock existence alone cannot establish whether it is stale.',
                    'hint': 'Check that no emulator owns this AVD before removing a stale lock.'}
        # Emulator and ADB ports are shared with the host network. Refuse a collision.
        port_check = self._run("python3 -c " + shlex.quote(
            "import socket,sys; sys.exit(any(socket.socket().connect_ex(('127.0.0.1',p)) == 0 for p in (5554,5555,5038)))"), timeout=10)
        if port_check.get('error'):
            return port_check
        if port_check.get('exit_code') != 0 or port_check.get('timed_out'):
            return {'error': 'emulator_port_unavailable', 'ports': [5038, 5554, 5555], 'check_result': port_check}
        adb = shlex.quote(self.root + '/sdk/platform-tools/adb')
        _, adb_data = validate_process_action({'action': 'process_start',
            'command': self._environment() + 'exec ' + adb + ' -P 5038 nodaemon server',
            'network': True, 'kvm': False, 'lifetime_seconds': 86400})
        try:
            adb_process = self.manager.call('process_start', adb_data)
        except OSError as exc:
            return {'error': 'adb_server_start_failed', 'detail': str(exc)}
        self.adb_process_id = adb_process.get('process_id')
        if not self.adb_process_id or adb_process.get('state') != 'running':
            self.adb_process_id = None
            return {'error': 'adb_server_start_failed', 'process_result': adb_process}
        emulator = shlex.quote(self.root + '/sdk/emulator/emulator')
        cmd = self._environment() + 'exec ' + emulator + ' -avd ' + shlex.quote(self.avd) + (
            ' -no-window -no-audio -no-boot-anim -no-metrics -gpu swiftshader_indirect -no-snapshot -port 5554')
        _, data = validate_process_action({'action': 'process_start', 'command': cmd,
                                           'network': True, 'kvm': True, 'lifetime_seconds': 86400})
        try:
            result = self.manager.call('process_start', data)
        except OSError as exc:
            self.manager.call('process_stop', {'process_id': self.adb_process_id})
            self.adb_process_id = None
            return {'error': 'emulator_start_failed', 'detail': str(exc)}
        self.process_id = result.get('process_id')
        if not self.process_id or result.get('state') != 'running':
            self.manager.call('process_stop', {'process_id': self.adb_process_id})
            self.adb_process_id = None
            self.process_id = None
            return {'error': 'emulator_start_failed', 'process_result': result}
        result['note'] = 'Started does not mean booted; call android_status.'
        return result

    def status(self, args):
        if not self.process_id:
            return {'state': 'not_started'}
        result = self.manager.call('process_status', {'process_id': self.process_id})
        if result['state'] == 'running':
            if not self.adb_process_id:
                return {**result, 'adb_visible': False, 'boot_completed': False, 'error': 'adb_server_not_running'}
            adb_state = self.manager.call('process_status', {'process_id': self.adb_process_id})
            if adb_state.get('state') != 'running':
                return {**result, 'adb_visible': False, 'boot_completed': False, 'error': 'adb_server_not_running', 'adb_server': adb_state}
            adb = shlex.quote(self.root + '/sdk/platform-tools/adb')
            self._run(adb + ' -P 5038 connect 127.0.0.1:5555', timeout=10)
            device = self._run(adb + ' -P 5038 -s 127.0.0.1:5555 get-state', timeout=10)
            result['adb_visible'] = device.get('exit_code') == 0 and device.get('stdout', '').strip() == 'device'
            if not result['adb_visible']:
                result['boot_completed'] = False
                result['adb_output'] = (device.get('stdout', '') + device.get('stderr', ''))[-1000:]
                return result
            probe = self._run(adb + ' -P 5038 -s 127.0.0.1:5555 shell getprop sys.boot_completed', timeout=10)
            result['adb_exit_code'] = probe['exit_code']
            result['boot_completed'] = probe['exit_code'] == 0 and probe['stdout'].strip() == '1'
            result['adb_output'] = (probe['stdout'] + probe['stderr'])[-1000:]
        return result

    def logs(self, args):
        if not self.process_id:
            return {'state': 'not_started'}
        return self.manager.call('process_logs', {'process_id': self.process_id, 'limit': 4000}, output_limit=self.output_limit)

    def stop(self, args):
        if not self.process_id:
            return {'state': 'not_started'}
        result = self.manager.call('process_stop', {'process_id': self.process_id})
        if self.adb_process_id:
            result['adb_server'] = self.manager.call('process_stop', {'process_id': self.adb_process_id})
            self.adb_process_id = None
        self.process_id = None
        return result

    def adb(self, args):
        values = args['args']
        if not isinstance(values, list) or not values or len(values) > 64 or any(not isinstance(x, str) or '\x00' in x for x in values):
            return {'error': 'invalid_adb_arguments'}
        executable = shlex.quote(self.root + '/sdk/platform-tools/adb')
        command = executable + ' -P 5038 -s 127.0.0.1:5555 wait-for-device ' + ' '.join(shlex.quote(x) for x in values)
        return self._run(command, timeout=60)

    def agent_adb(self, args):
        """Restrict the agent-facing ADB entry point to the session's emulator."""
        values = args.get('args')
        if not isinstance(values, list) or not values or len(values) > 64 or any(not isinstance(x, str) or '\x00' in x for x in values):
            return {'error': 'invalid_adb_arguments'}
        # ADB server/transport commands ignore or change the selected device.
        if values[0] not in ('get-state', 'get-serialno', 'shell', 'install', 'logcat'):
            return {'error': 'android_adb_command_not_allowed', 'hint': 'Only emulator-targeted commands are supported.'}
        diagnostic = (values in (['get-state'], ['get-serialno'], ['logcat', '-d']) or
                      (values[:2] == ['shell', 'getprop'] and
                       (len(values) == 2 or (len(values) == 3 and re.fullmatch(r'[A-Za-z0-9_.-]+', values[2])))))
        if not diagnostic and not self.allow_device_changes:
            return {'error': 'android_device_changes_not_granted',
                    'hint': 'Mutating ADB commands require allow_device_changes:true in Android settings.'}
        return self.adb(args)

    def visual(self, kind, args):
        """Run a bounded input action, then return a fresh Android screenshot."""
        if kind == 'tap':
            x, y = args.get('x'), args.get('y')
            if type(x) is not int or type(y) is not int or not (0 <= x <= 10000 and 0 <= y <= 10000):
                return Outcome({'error': 'invalid_coordinates'})
            action = self.adb({'args': ['shell', 'input', 'tap', str(x), str(y)]})
        elif kind == 'text':
            text = args.get('text')
            if not isinstance(text, str) or not text or len(text) > 1024 or any(ord(c) < 32 or ord(c) > 126 or c in '%&|;<>$`\\"\'()' for c in text):
                return Outcome({'error': 'unsupported_text', 'hint': 'Use printable ASCII without shell-special characters; spaces are supported.'})
            action = self.adb({'args': ['shell', 'input', 'text', text.replace(' ', '%s')]})
        elif kind == 'key':
            key = args.get('key')
            if key not in ('BACK', 'HOME', 'ENTER', 'TAB', 'ESCAPE', 'DPAD_UP', 'DPAD_DOWN', 'DPAD_LEFT', 'DPAD_RIGHT', 'DPAD_CENTER'):
                return Outcome({'error': 'unsupported_key'})
            action = self.adb({'args': ['shell', 'input', 'keyevent', 'KEYCODE_' + key]})
        elif kind == 'view':
            action = None
        else:
            return Outcome({'error': 'invalid_action'})
        if action is not None and (action.get('exit_code') != 0 or action.get('timed_out')):
            return Outcome({'error': 'android_input_failed', 'action_result': action})
        # A unique file in /work prevents stale screenshots and is removed even on failure.
        with tempfile.NamedTemporaryFile(prefix='.android-screen-', suffix='.png', dir=self.workdir) as tmp:
            target = '/work/' + Path(tmp.name).name
            adb = shlex.quote(self.root + '/sdk/platform-tools/adb')
            result = self._run('sleep 0.5; ' + adb + ' -P 5038 -s 127.0.0.1:5555 wait-for-device exec-out screencap -p > ' + shlex.quote(target), timeout=30)
            if result.get('exit_code') != 0 or result.get('timed_out'):
                return Outcome({'error': 'android_screenshot_failed', 'action_result': action, 'capture_result': result})
            metadata, block = read_image(target, self.workdir)
            if block is None:
                return Outcome({'error': 'android_screenshot_invalid', 'action_result': action, 'image': metadata})
            metadata.pop('path', None)
            return Outcome({'action': kind, 'action_result': action, 'image': metadata},
                           observations=(Observation(text='Android screen after ' + kind, image=block['image_url']['url']),))

    def hierarchy(self, args):
        """Dump the current hierarchy; never reuse a previous dump."""
        with tempfile.NamedTemporaryFile(prefix='.android-ui-', suffix='.xml', dir=self.workdir) as tmp:
            target = '/work/' + Path(tmp.name).name
            adb = shlex.quote(self.root + '/sdk/platform-tools/adb')
            result = self._run(adb + ' -P 5038 -s 127.0.0.1:5555 exec-out uiautomator dump /dev/tty > ' + shlex.quote(target), timeout=30)
            if result.get('exit_code') != 0 or result.get('timed_out'):
                return {'error': 'android_hierarchy_failed', 'capture_result': result}
            try:
                data = Path(tmp.name).read_text(encoding='utf-8')
                start = data.find('<?xml')
                end = data.rfind('</hierarchy>')
                if start < 0 or end < 0:
                    raise ValueError('missing XML hierarchy')
                root = ET.fromstring(data[start:end + len('</hierarchy>')])
                nodes = [dict(node.attrib) for node in root.iter('node')]
            except (ValueError, OSError, ET.ParseError) as exc:
                return {'error': 'android_hierarchy_invalid', 'detail': str(exc)}
            return {'nodes': nodes}

    def tap_element(self, args):
        criteria = {k: args[k] for k in ('text', 'resource_id', 'content_desc') if args.get(k)}
        if not criteria:
            return Outcome({'error': 'missing_element_criteria'})
        hierarchy = self.hierarchy({})
        if 'error' in hierarchy:
            return Outcome(hierarchy)
        names = {'text': 'text', 'resource_id': 'resource-id', 'content_desc': 'content-desc'}
        matches = [n for n in hierarchy['nodes'] if all(n.get(names[k]) == v for k, v in criteria.items())]
        if len(matches) != 1:
            return Outcome({'error': 'element_not_found' if not matches else 'element_ambiguous', 'matches': len(matches)})
        match = re.fullmatch(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', matches[0].get('bounds', ''))
        if not match:
            return Outcome({'error': 'element_invalid_bounds'})
        x1, y1, x2, y2 = map(int, match.groups())
        if x2 <= x1 or y2 <= y1:
            return Outcome({'error': 'element_invalid_bounds'})
        return self.visual('tap', {'x': (x1 + x2)//2, 'y': (y1 + y2)//2})

    def owned_action(self, callback):
        if not self.manager or not self.process_id or not self.adb_process_id:
            return Outcome({'error': 'android_session_not_running'})
        state = self.manager.call('process_status', {'process_id': self.process_id})
        adb_state = self.manager.call('process_status', {'process_id': self.adb_process_id})
        if state.get('state') != 'running' or adb_state.get('state') != 'running':
            return Outcome({'error': 'android_session_not_running', 'process': state, 'adb_server': adb_state})
        return callback()

    def on_event(self, event):
        if event.kind == 'session_started':
            self.manager = ProcessManager(self.workdir, allow_network=self.allow_network, allow_kvm=self.allow_kvm)
            empty = {}
            tools = (
                declare('android_check', 'Check the isolated Android 9 SDK, AVD and KVM.', empty, (), lambda a,c: Outcome(self.check(a))),
                declare('android_build_check', 'Check java, javac and jlink for an Android build in the shell sandbox. Optionally specify the selected absolute java_home.', {'java_home': TEXT}, (), lambda a,c: Outcome(self.build_check(a))),
                declare('android_prepare', 'Install pinned Android 9 packages using local sdkmanager, accept SDK licenses and create the AVD.', empty, (), lambda a,c: Outcome(self.prepare(a))),
                declare('android_start', 'Start the session-owned Android 9 emulator with KVM.', empty, (), lambda a,c: Outcome(self.start(a))),
                declare('android_status', 'Check the emulator process and Android boot completion.', empty, (), lambda a,c: Outcome(self.status(a))),
                declare('android_logs', 'Read recent emulator logs.', empty, (), lambda a,c: Outcome(self.logs(a))),
                declare('android_stop', 'Stop the emulator owned by this session.', empty, (), lambda a,c: Outcome(self.stop(a))),
                declare('android_view', 'Observe the current Android screen.', empty, (), lambda a,c: self.owned_action(lambda: self.visual('view', a))),
                declare('android_hierarchy', 'Get a fresh Android UI hierarchy.', empty, (), lambda a,c: self.owned_action(lambda: Outcome(self.hierarchy(a)))),
                declare('android_tap_element', 'Find exactly one element in a fresh hierarchy and tap it.', {'text': TEXT, 'resource_id': TEXT, 'content_desc': TEXT}, (), lambda a,c: self.owned_action(lambda: self.tap_element(a))),
                declare('android_tap', 'Tap Android screen coordinates and return a fresh screenshot.', {'x': INTEGER, 'y': INTEGER}, ('x', 'y'), lambda a,c: self.owned_action(lambda: self.visual('tap', a))),
                declare('android_text', 'Type supported ASCII text into the focused field and return a fresh screenshot.', {'text': TEXT}, ('text',), lambda a,c: self.owned_action(lambda: self.visual('text', a))),
                declare('android_key', 'Press an Android key (BACK, HOME, ENTER, TAB, ESCAPE, or DPAD_*) and return a fresh screenshot.', {'key': TEXT}, ('key',), lambda a,c: self.owned_action(lambda: self.visual('key', a))),
                declare('android_adb', 'Run supported ADB commands on the session-owned emulator. Device changes require an explicit module grant.',
                        {'args': {'type': 'array', 'items': TEXT, 'minItems': 1, 'maxItems': 64}}, ('args',), lambda a,c: self.owned_action(lambda: Outcome(self.agent_adb(a)))),
            )
            return Contribution(message='ANDROID 9: Before building an app, use android_build_check for the JDK selected for Gradle; it checks java, javac and jlink, not version compatibility. Use android_view and android_hierarchy for fresh screen state; android_tap_element matches a unique element by text, resource ID or content description. android_tap, android_text and android_key return a fresh screen image or a separate capture error. Re-check coordinates after the keyboard or layout changes. Use android_check and, when needed, android_prepare to install SDK packages and accept licenses, then android_start, android_status, android_logs, android_adb and android_stop. Start is not proof of boot; wait for boot_completed. Never delete an AVD lock without checking its owner.', tools=tools)
        if event.kind == 'session_stopped' and self.manager:
            self.manager.close()
            self.manager = None


def create_extension(*, settings, context):
    if set(settings) - {'env_dir', 'avd', 'system_image', 'allow_network', 'allow_kvm', 'allow_device_changes'}:
        raise ValueError('unknown Android9 setting')
    if any(not isinstance(settings[k], str) for k in ('env_dir', 'avd', 'system_image') if k in settings):
        raise ValueError('Android9 env_dir, avd and system_image must be strings')
    if any(type(settings.get(k, False)) is not bool for k in ('allow_network', 'allow_kvm', 'allow_device_changes')):
        raise ValueError('Android9 permission settings must be boolean')
    return Android9(context['workdir'], settings.get('env_dir', '.android-env'),
                    settings.get('avd', 'tv28'), context.get('output_limit', 24000), settings.get('system_image', 'system-images;android-28;android-tv;x86'),
                    allow_network=settings.get('allow_network', False), allow_kvm=settings.get('allow_kvm', False),
                    allow_device_changes=settings.get('allow_device_changes', False))
