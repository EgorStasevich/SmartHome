"""Small shared utility helpers."""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_float(value: str, fallback: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def parse_int(value: str, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def now_iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def format_local_timestamp(value: Optional[str], timezone: ZoneInfo) -> str:
    if not value:
        return "-"
    dt = parse_iso_datetime(value)
    if not dt:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone)
    return dt.astimezone(timezone).strftime("%Y-%m-%d %H:%M")


def atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    temp_path.replace(path)


def split_telegram_command(text: str) -> Tuple[str, List[str]]:
    try:
        chunks = shlex.split(text)
    except ValueError:
        chunks = text.split()

    if not chunks:
        return "", []

    cmd = chunks[0]
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd.lower(), chunks[1:]


def chunk_text(text: str, max_len: int = 3900) -> List[str]:
    if len(text) <= max_len:
        return [text]

    lines = text.splitlines()
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for line in lines:
        extra = len(line) + 1
        if current and current_len + extra > max_len:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += extra

    if current:
        chunks.append("\n".join(current))

    return chunks
