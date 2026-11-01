# Загрузчик модулей

`project_state_agent.py` загружает настройки запуска и модули из одного JSON.
Выберите конфигурацию и передайте только путь к ней, рабочую папку, папку
сессии и задачу:

```sh
python project_state_agent.py --config examples/liquid.cpu.config.json \
  --workdir /path/to/work --session-dir /path/to/session -- 'Задача'
```

В `runtime` находятся настройки модели, окна и сервера, а в `modules` —
состав сборки. Параметры `--modules`, `--model`, `--backend` и остальные
настройки сборки непосредственно при запуске не принимаются.

```json
{
  "runtime": {
    "backend": "llama-cpp",
    "base_url": "http://127.0.0.1:18951/v1",
    "model": "lfm3-1.7b",
    "worker_context_budget": 7000,
    "history_context_high": 0.5
  },
  "modules": [
    {"factory": "pavlusha_modules.shell:create_extension"},
    {"factory": "pavlusha_modules.finish:create_extension"},
    {"factory": "pavlusha_modules.token_window:create_extension"}
  ]
}
```

Готовые примеры: `examples/liquid.cpu.config.json` и
`examples/qwen.native.config.json`. Объект `runtime.llama_server` задаёт
параметры управляемого сервера внутри того же файла; вместо него поле
`llama_server_config` может указывать на отдельный файл, путь к которому
разрешается относительно конфига сборки. В примерах сервера указаны заполнители
путей, которые нужно заменить. Без обоих полей агент подключается к уже
запущенному серверу по `base_url`. Секреты не помещаются в конфиг: для ключа
можно задать имя переменной окружения через `api_key_env`.

`token_window` получает адрес токенизатора и размер окна из `base_url` и
`worker_context_budget`; явное значение `context_tokens`, если задано, должно
совпадать с размером окна. Его доли задаются настройками модуля
`retain_fraction` и `single_fraction`, а порог начала сброса — параметром
`history_context_high` в `runtime`.

Загрузчик не знает предметных правил модулей. Он импортирует указанную Python-фабрику
и вызывает её как `factory(settings=..., context=...)`. Фабрика возвращает объект
с `name` и `on_event`. Контекст содержит `workdir`, `review_every`, `network`, `command_timeout`,
`output_limit` и `config_dir` — каталог конфигурации. Сборка также передаёт
`session_dir`, `modules_path`, `base_url`, `backend`, `model`, `api_timeout`,
`temperature`, `max_tokens`, `worker_context_budget`, `reasoning_effort`,
`max_steps`, `history_high`, `history_context_high`, `live`, `live_format`,
`interactive` и `llama_server_config` (путь или `null`). Это параметры запуска;
наличие конфигурации сервера не означает, что модуль должен запускать сервер.
`kv_storage_dir` содержит путь к каталогу снимков управляемого сервера, если
включён `preserve_kv`, иначе `null`. В RAM-режиме каталог находится в `/dev/shm`;
его создаёт и удаляет управляющий сервером компонент. При подключении к внешнему
серверу путь неизвестен и также равен `null`.
Пути передаются абсолютными. Каждый вызов получает собственную копию.
Явная настройка State `settings.review_every` имеет приоритет над CLI; при её
отсутствии используется CLI, затем значение 10. Map настроек пока не принимает.

Весь манифест структурно проверяется до импорта. Ошибки импорта, создания
обработчика и повторяющиеся имена приводят к явной ошибке до запуска сервера модели.
Порядок записей определяет порядок модулей и их вкладов; очистка идёт в обратном
порядке через EventHost. Конфликты функций проверяются существующим EventHost.

Это загрузка доверенного Python-кода. Конструкторы/фабрики должны только создавать
обработчик: ресурсы следует выделять на `session_started` и освобождать на
`session_stopped`, включая частично неудачный startup. Загрузчик не может откатить
произвольные побочные эффекты Python-импорта или конструктора.

Поддерживается импорт установленных/доступных Python-пакетов; загрузка произвольного
`.py` по файловому пути и старые манифесты `--functions` пока не поддерживаются.
Состав модулей не записывается в техническую сессию: для возобновления следует
использовать ту же конфигурацию. Горячей замены во время сессии нет.

Загрузчик работает с общим контрактом v2 (см. `event-interface.md`).
Модуль оркестрации подключён в `examples/arishka-pavlusha.config.json`;
его настройки и ограничения описаны в [orchestration.md](orchestration.md).
Проверки: `tests/test_module_loader.py`.
