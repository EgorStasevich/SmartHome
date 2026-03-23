"""Text constants and static domain rules."""

HELP_TEXT = """
Команды:
  помощь
      Показать подсказку.

  включи <устройство>
  выключи <устройство>
      Примеры:
      включи light.kitchen
      выключи люстра в гостиной

  статус <устройство>
      Пример:
      статус sensor.living_room_temperature

  установи температуру <значение> [в <климат-устройство>]
      Примеры:
      установи температуру 22 в climate.living_room
      установи температуру 21.5

  сцена <название сцены>
  активируй <название сцены>
      Пример:
      сцена вечер

  список [домен]
      Примеры:
      список
      список light

  обнови
      Перечитать список устройств из Home Assistant.

  выход
      Завершить работу.
""".strip()


TELEGRAM_HELP_TEXT = """
Домашний ассистент: команды

Мониторинг:
- /status
- /problems
- /devices [domain]

Управление:
- /on <entity|alias>
- /off <entity|alias>
- /state <entity|alias>

Сценарии:
- /sc_list
- /sc_show <id>
- /sc_new <id> <name>
- /sc_delete <id>
- /sc_add <id> on|off <entity_id>
- /sc_add <id> temp <climate_id> <value>
- /sc_add <id> delay <seconds>
- /sc_add <id> script <script_id>
- /sc_step_remove <id> <index>
- /sc_run <id>

Сервис:
- /help
""".strip()


DEFAULT_DENYLIST_PATTERNS = [
    r"audio_output",
    r"bssid",
    r"connection_type",
    r"geocoded_location",
    r"last_update_trigger",
    r"sim_1",
    r"sim_2",
    r"ssid",
    r"storage",
    r"cleaning_progress",
    r"cruising_history",
    r"drying_progress",
    r"low_water_warning",
    r"mapping_time",
    r"stream_status",
    r"task_type",
]


WORKING_UNAVAILABLE_DOMAINS = {
    "light",
    "switch",
    "climate",
    "sensor",
    "binary_sensor",
    "camera",
    "media_player",
}
