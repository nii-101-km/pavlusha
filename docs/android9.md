# Android emulator module

The optional `pavlusha_modules.android9:create_extension` module runs a
session-owned, headless Android emulator and provides ADB-based observations and
input. It does not discover or control physical Android devices. The default
AVD uses the Android 9 TV x86 image; other configured system images have not
been verified by this project.

Configure the module in the assembly, for example:

```json
{"factory":"pavlusha_modules.android9:create_extension",
 "settings":{"env_dir":".android-env","avd":"tv28",
             "allow_network":true,"allow_kvm":true}}
```

Both permissions default to false. Shared network access grants the emulator
and ADB the host network, not just loopback; KVM grants access to `/dev/kvm`.
`env_dir` must be inside the work directory and contain the SDK under `sdk/`
and AVD files under `avd/`. Install the official Android command-line tools
there first. The module checks this configured SDK; it does not search the host
for other SDKs. KVM must be available to the controller and the ports used by
the emulator and its private ADB server must be free.

1. Call `android_check` to inspect components, permissions, the selected image,
   AVD and KVM. `ready_to_start` is a preliminary check, not a boot guarantee.
2. If needed, call `android_prepare` explicitly: it requires network permission,
   accepts SDK licenses, uses the existing `sdkmanager` to install packages,
   and creates the named AVD if absent. It does not install system packages.
3. Call `android_start` and poll `android_status` until `boot_completed` is true.
   `android_logs` reads emulator logs; `android_stop` stops only owned processes.
4. Use `android_view` and `android_hierarchy` for fresh state. `android_tap`,
   `android_tap_element`, `android_text` and `android_key` send ADB input and
   return a new screenshot or an explicit capture error. Element matching
   requires exactly one match; text input supports a limited ASCII subset.

`android_adb` targets only the session's configured emulator transport. Read-only
`get-state`, `get-serialno`, `shell getprop` and `logcat -d` are available;
other emulator-targeted `shell`, `install`, or `logcat` commands require the
explicit `allow_device_changes: true` module setting. That setting grants broad
control over data and apps in the selected AVD, so enable it only for an AVD
whose contents may be changed. Server and transport management commands are
rejected. Do not use this module to access physical or user-owned devices;
connecting a device does not authorize modifying it.

The module does not manage Gradle projects or build APKs. `android_build_check`
only checks whether `java`, `javac` and `jlink` are executable in the shell
sandbox; supply `java_home` for a specific JDK. It does not verify compatibility
or install tools. Use existing shell and project tools for builds, under their
own permissions. Missing tools, failed commands and timeouts must be inspected
in the returned results.

AVD locks are never removed automatically. A matching visible process is useful
evidence, but no visible owner does not prove a lock is stale. The module does
not manage signing keys, publish artifacts, or provide host-window mouse control.
Emulator state and an image snapshot do not prove audio or physical input.

Run focused tests from the repository root:

```sh
python -m unittest tests.test_android9
```
