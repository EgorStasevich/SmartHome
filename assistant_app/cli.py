"""CLI entrypoints and argument routing."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import Config
from .daemon import AssistantDaemon
from .errors import AssistantError
from .runtime import AssistantRuntime, execute_local_command
from .stores import OwnerStore


async def run_legacy_mode(args: argparse.Namespace) -> int:
    config = Config.from_runtime(args.base_url, args.token, args.aliases)
    runtime = AssistantRuntime(config)

    try:
        states = await runtime.refresh_states()
        print(
            f"Подключение к Home Assistant успешно: {config.base_url} "
            f"(сущностей: {len(states)})"
        )

        if args.command:
            output = await execute_local_command(" ".join(args.command), runtime)
            if output and output != "__EXIT__":
                print(output)
            return 0

        print("Интерактивный режим. Напиши 'помощь' для команд.")
        while True:
            try:
                user_input = input("ha> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return 0

            if not user_input:
                continue

            try:
                output = await execute_local_command(user_input, runtime)
            except AssistantError as exc:
                print(f"Ошибка: {exc}")
                continue

            if output == "__EXIT__":
                return 0
            if output:
                print(output)
    finally:
        await runtime.close()


async def run_daemon_mode(args: argparse.Namespace) -> int:
    config = Config.from_runtime(args.base_url, args.token, args.aliases)
    if not config.telegram_bot_token:
        raise AssistantError(
            "TELEGRAM_BOT_TOKEN не задан в .env. Добавь токен и повтори запуск daemon."
        )

    runtime = AssistantRuntime(config)
    daemon = AssistantDaemon(runtime)

    try:
        await daemon.run_forever()
    finally:
        await daemon.close()
        await runtime.close()

    return 0


def run_owner_reset(args: argparse.Namespace) -> int:
    load_dotenv()
    owner_file = Path(args.owner_file or os.getenv("TELEGRAM_OWNER_FILE", "bot_owner.json"))
    store = OwnerStore(owner_file)
    deleted = store.reset()
    if deleted:
        print(f"Владелец бота сброшен: {owner_file}")
    else:
        print(f"Файл владельца не найден: {owner_file}")
    return 0


def build_legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Домашний ассистент для управления Home Assistant.",
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="Одноразовая команда. Если не указано, запускается интерактивный режим.",
    )
    parser.add_argument(
        "--base-url",
        help="Адрес Home Assistant (например, http://192.168.2.36:8123).",
    )
    parser.add_argument(
        "--token",
        help="Long-Lived Access Token Home Assistant.",
    )
    parser.add_argument(
        "--aliases",
        help="Путь к JSON-файлу с алиасами устройств.",
    )
    return parser


def build_daemon_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assistant.py daemon",
        description="Запуск фонового daemon: мониторинг + Telegram управление.",
    )
    parser.add_argument(
        "--base-url",
        help="Адрес Home Assistant (например, http://192.168.2.36:8123).",
    )
    parser.add_argument("--token", help="Long-Lived Access Token Home Assistant.")
    parser.add_argument("--aliases", help="Путь к JSON-файлу с алиасами устройств.")
    return parser


def build_owner_reset_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="assistant.py owner reset",
        description="Локальный сброс владельца Telegram бота.",
    )
    parser.add_argument(
        "--owner-file",
        help="Путь к файлу владельца бота (по умолчанию TELEGRAM_OWNER_FILE или bot_owner.json).",
    )
    return parser


def main() -> None:
    argv = sys.argv[1:]

    try:
        if argv and argv[0] == "daemon":
            daemon_parser = build_daemon_parser()
            args = daemon_parser.parse_args(argv[1:])
            code = asyncio.run(run_daemon_mode(args))
        elif len(argv) >= 2 and argv[0] == "owner" and argv[1] == "reset":
            reset_parser = build_owner_reset_parser()
            args = reset_parser.parse_args(argv[2:])
            code = run_owner_reset(args)
        else:
            legacy_parser = build_legacy_parser()
            args = legacy_parser.parse_args(argv)
            code = asyncio.run(run_legacy_mode(args))
    except AssistantError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        code = 1
    except KeyboardInterrupt:
        code = 130

    sys.exit(code)
