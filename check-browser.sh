#!/bin/sh
# Check the private GUI browser using the same environment as run-pavlusha.sh.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
venv_dir=${PAVLUSHA_VENV:-${VIRTUAL_ENV:-"$project_dir/.venv"}}
python_bin=$venv_dir/bin/python
errors=0

printf '%s\n' 'Проверка браузера Павлуши'
if [ ! -x "$python_bin" ]; then
    printf '%s\n' "ОШИБКА: не найдено Python-окружение: $venv_dir" \
        "Создайте .venv по README или задайте PAVLUSHA_VENV." >&2
    errors=1
else
    printf '%s\n' "Python: $python_bin"
    if ! "$python_bin" -c 'import Xlib; from PIL import Image' >/dev/null 2>&1; then
        printf '%s\n' "ОШИБКА: отсутствует python-xlib или Pillow." \
            "Установите зависимости: \"$python_bin\" -m pip install -r \"$project_dir/requirements.txt\"" >&2
        errors=1
    fi
fi
for program in bwrap Xvfb; do
    if ! command -v "$program" >/dev/null 2>&1; then
        printf '%s\n' "ОШИБКА: не найден $program." >&2
        errors=1
    fi
done
for program in /usr/bin/epiphany /usr/bin/dbus-run-session /usr/bin/dbus-daemon /usr/bin/gsettings; do
    if [ ! -x "$program" ]; then
        printf '%s\n' "ОШИБКА: не найден $program." >&2
        errors=1
    fi
done
if [ "$errors" -ne 0 ]; then
    printf '%s\n' 'Для Debian/Ubuntu: sudo apt-get install bubblewrap xvfb epiphany-browser dbus-daemon libglib2.0-bin' >&2
    printf '%s\n' 'Подробности: docs/browser-setup.md' >&2
    exit 1
fi

printf '%s\n' 'Компоненты найдены. Открываю тестовую страницу в изолированном браузере…'
# No model, external network, user files, or user browser are used.
PYTHONPATH="$project_dir${PYTHONPATH:+:$PYTHONPATH}" "$python_bin" - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from pavlusha_agent.extensions import Event
from pavlusha_modules.gui import Gui

try:
    with TemporaryDirectory(prefix='pavlusha-browser-check-') as temp:
        work = Path(temp) / 'work'
        work.mkdir()
        (work / 'index.html').write_text('<!doctype html><title>Pavlusha test</title><h1>Browser check OK</h1>', encoding='utf-8')
        gui = Gui(work, allow_network=False)
        try:
            contribution = gui.on_event(Event('session_started', 'browser_check', 0, 0))
            tools = {tool.name: tool for tool in contribution.tools}
            command = ('python3 -u -m http.server 8000 --bind 127.0.0.1 > /work/browser-check.log 2>&1 & '
                       'exec pavlusha-browser http://127.0.0.1:8000/')
            started = tools['gui_start'].invoke({'command': command, 'network': False, 'delay': 8}, None)
            if started.result.get('state') != 'alive':
                raise RuntimeError('Не удалось запустить браузер: ' + str(started.result))
            observed = tools['view_gui'].invoke({'delay': 2}, None)
            if observed.result.get('state') != 'alive' or not observed.observations:
                raise RuntimeError('Не удалось получить снимок браузера: ' + str(observed.result))
            log = work / 'browser-check.log'
            log_text = log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''
            if '"GET / HTTP/1.1" 200' not in log_text:
                raise RuntimeError('Браузер не загрузил тестовую страницу. Журнал сервера: ' + log_text[-1500:])
        finally:
            gui.on_event(Event('session_stopped', 'browser_check', 0, 0))
    print('ГОТОВО: браузер открыл локальную страницу, снимок получен; внешняя сеть не использовалась.')
    print('Поддержка изображений моделью проверяется отдельно; см. docs/browser-setup.md.')
except Exception as exc:
    print('ОШИБКА: проверка браузера не прошла:', exc, file=__import__('sys').stderr)
    print('Проверьте доступность bubblewrap и виртуального экрана; см. docs/browser-setup.md.', file=__import__('sys').stderr)
    raise SystemExit(1)
PY
