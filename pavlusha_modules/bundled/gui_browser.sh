#!/bin/sh
# Bound/exposed only by GuiRuntime inside its existing bubblewrap sandbox.
set -eu
if [ "${PAVLUSHA_PRIVATE_GUI:-}" != 1 ]; then
    echo 'pavlusha-browser requires gui_start private bubblewrap environment' >&2
    exit 2
fi
for executable in /usr/bin/epiphany /usr/bin/dbus-run-session /usr/bin/dbus-daemon /usr/bin/gsettings; do
    if [ ! -x "$executable" ]; then
        echo "pavlusha-browser: missing $executable; install epiphany-browser, dbus-daemon and libglib2.0-bin on the controller host. Do not search for substitute browsers." >&2
        exit 127
    fi
done
# No host bus/profile/GPU. WebKit's nested sandbox is unavailable on this host;
# the application and all its children remain in Pavlusha's outer bubblewrap.
export GSK_RENDERER=cairo
export WEBKIT_DISABLE_COMPOSITING_MODE=1
export WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1
export GIO_USE_VFS=local
export GTK_A11Y=none
export XDG_RUNTIME_DIR=/tmp/pavlusha-browser-runtime
mkdir -p "$XDG_RUNTIME_DIR"
chmod 700 "$XDG_RUNTIME_DIR"
profile=$(mktemp -d /tmp/pavlusha-browser-profile.XXXXXX)
export XDG_CONFIG_HOME="$profile/config"
export GSETTINGS_BACKEND=keyfile
# Without a window manager this first-run modal can hide behind the main window
# and swallow coordinate input. These preferences belong only to this session.
/usr/bin/gsettings set org.gnome.Epiphany ask-for-default false
exec /usr/bin/dbus-run-session -- /usr/bin/epiphany --private-instance --profile="$profile" "$@"
