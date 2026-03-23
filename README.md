# Home Assistant Assistant (RU)

Локальный ассистент для Home Assistant с двумя режимами:

- CLI (русские команды для управления домом)
- Daemon (мониторинг проблем + Telegram-бот + сценарии)

## 1. Установка

```bash
cd "/Users/egorstasevich/Documents/Home Assistant"
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Конфиг

```bash
cp .env.example .env
```

Обязательные поля в `.env`:

- `HA_BASE_URL`
- `HA_TOKEN`
- `TELEGRAM_BOT_TOKEN`

Главные рабочие параметры:

- `ALERT_POLL_SECONDS=60`
- `ALERT_DIGEST_TIME=09:00`
- `QUIET_HOURS=23:00-08:00`
- `BATTERY_WARN=30`
- `BATTERY_CRITICAL=20`
- `UNAVAILABLE_WARN_MIN=10`
- `ALERT_DEDUP_MIN=360`
- `TIMEZONE=Europe/Minsk`

## 3. CLI режим (как раньше)

Интерактивно:

```bash
python3 assistant.py
```

Разовая команда:

```bash
python3 assistant.py "включи коридорный свет"
```

Поддерживаемые команды в CLI:

- `включи <устройство>`
- `выключи <устройство>`
- `статус <устройство>`
- `установи температуру <значение> [в <climate>]`
- `сцена <название>`
- `список [домен]`

## 4. Daemon режим

Запуск:

```bash
python3 assistant.py daemon
```

Что делает daemon:

- опрашивает Home Assistant каждые `ALERT_POLL_SECONDS`
- выявляет проблемы и отправляет алерты в Telegram
- отправляет ежедневную сводку в `ALERT_DIGEST_TIME`
- поддерживает quiet-hours для некритичных алертов

## 5. Telegram доступ (owner lock)

- Только `private` чат.
- Первый пользователь, написавший боту, фиксируется как владелец (`bot_owner.json`).
- Все остальные user_id получают отказ.

Локальный сброс владельца:

```bash
python3 assistant.py owner reset
```

## 6. Telegram команды

- `/start`
- `/help`
- `/status`
- `/problems`
- `/devices [domain]`
- `/on <entity|alias>`
- `/off <entity|alias>`
- `/state <entity|alias>`
- `/sc_list`
- `/sc_show <id>`
- `/sc_new <id> <name>`
- `/sc_delete <id>`
- `/sc_add <id> on|off <entity_id>`
- `/sc_add <id> temp <climate_id> <value>`
- `/sc_add <id> delay <seconds>`
- `/sc_add <id> script <script_id>`
- `/sc_step_remove <id> <index>`
- `/sc_run <id>`

## 7. Сценарии ассистента

Сценарии хранятся отдельно в `scenarios.json` (не перезаписывают `automation.*` и `script.*` Home Assistant).

Формат шага:

- `{"type":"on","entity_id":"light.xxx"}`
- `{"type":"off","entity_id":"switch.xxx"}`
- `{"type":"temp","entity_id":"climate.xxx","value":22}`
- `{"type":"delay","seconds":30}`
- `{"type":"script","entity_id":"script.xxx"}`

## 8. Файлы состояния

Daemon создаёт/обновляет:

- `bot_owner.json`
- `scenarios.json`
- `alert_state.json`

## 9. Структура кода

Код разбит на модули (`assistant_app/`), чтобы не держать всё в одном файле:

- `assistant_app/cli.py` — маршрутизация CLI режимов
- `assistant_app/daemon.py` — Telegram polling и мониторинг
- `assistant_app/runtime.py` — runtime и локальные команды
- `assistant_app/stores.py` — хранилища и детектор проблем
- `assistant_app/clients.py` — клиенты Home Assistant / Telegram
- `assistant_app/config.py` — конфиг из `.env`
- `assistant_app/entities.py` — индексация сущностей и алиасы
- `assistant_app/constants.py`, `assistant_app/utils.py`, `assistant_app/errors.py` — общие константы/утилиты/ошибки

`assistant.py` теперь только entrypoint, поэтому запуск команд не изменился.

## 10. Деплой на NAS (24/7 через Docker Compose)

### Что добавлено в проект

- `Dockerfile`
- `docker-compose.nas.yml`
- `scripts/deploy_nas.sh`

### Первый запуск на NAS

1. Скопируй проект на NAS (или клонируй Git-репозиторий).
2. Перейди в папку проекта.
3. Создай конфиг:

```bash
cp .env.example .env
```

4. Заполни `.env` своими значениями (`HA_BASE_URL`, `HA_TOKEN`, `TELEGRAM_BOT_TOKEN`).
5. Запусти:

```bash
./scripts/deploy_nas.sh
```

### Проверка статуса и логов

```bash
docker compose -f docker-compose.nas.yml ps
docker compose -f docker-compose.nas.yml logs -f ha-assistant
```

### Обновление после правок кода

Если ты изменил код локально и запушил в репозиторий:

```bash
git pull
./scripts/deploy_nas.sh
```

Скрипт пересоберёт контейнер и перезапустит сервис без ручных шагов.

### Где хранятся данные ассистента

Состояние хранится в папке `data/` (монтируется в контейнер как `/app/data`):

- `data/bot_owner.json`
- `data/scenarios.json`
- `data/alert_state.json`
- `data/aliases.json` (если используешь алиасы)

Поэтому при обновлении контейнера данные не теряются.
