# Pavlusha 0.10.0

Changes since the published 0.9.1 release:

- Added an optional, session-owned Android 9 emulator module with SDK, AVD,
  KVM and JDK checks; bounded ADB actions; screenshots; UI hierarchy and
  unambiguous element matching. Emulator process, ADB visibility and Android
  boot completion are reported separately.
- Added opt-in KVM passthrough to the shared sandbox and owned background-process
  implementation. Android network, KVM and device-changing ADB operations each
  require explicit module settings. Network sharing is the host network.
- Added Android setup and limitations documentation, local artifact and signing
  key ignore rules, and restored the README table of model configurations.

The module is headless and targets its own emulator. Physical Android devices,
other Android versions, host-window mouse input and audible output are not
covered by the verified scenario. `android_prepare` explicitly accepts SDK
licenses and uses existing command-line tools; Gradle builds remain separate.
See [Android module](android9.md) for configuration and safety details.

Verification: the isolated commit snapshot passed 301 automated tests, with 13
skipped. A subsequent live dogfood checked denial of ungranted installation and
ADB server control, then booted Android 9, installed a test APK with explicit
permission and verified a UI action. Gitleaks found no leaks in the Android
commit. These results do not validate unrelated local uncommitted changes.
