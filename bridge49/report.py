"""Вывод в терминал: таблицы и сводки. Без внешних зависимостей."""
from __future__ import annotations

import shutil
from typing import Any, Iterable, Sequence


def table(rows: Sequence[dict], columns: Sequence[str] | None = None,
          *, max_width: int | None = None) -> str:
    """Простая выровненная таблица. Длинные значения обрезаются по ширине окна."""
    rows = [dict(row) for row in rows]
    if not rows:
        return "(пусто)"
    columns = list(columns or rows[0].keys())
    width = max_width or shutil.get_terminal_size((140, 40)).columns

    cells = [[_fmt(row.get(col)) for col in columns] for row in rows]
    sizes = [
        max(len(str(col)), *(len(cell[i]) for cell in cells))
        for i, col in enumerate(columns)
    ]

    # Если не влезаем — ужимаем самую широкую колонку, пока не влезем.
    while sum(sizes) + 3 * (len(sizes) - 1) > width and max(sizes) > 8:
        sizes[sizes.index(max(sizes))] -= 1

    def line(values: Sequence[str]) -> str:
        return "   ".join(
            _clip(value, size).ljust(size) for value, size in zip(values, sizes)
        )

    out = [line([str(c) for c in columns]), line(["─" * s for s in sizes])]
    out.extend(line(cell) for cell in cells)
    return "\n".join(out)


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(v) for v in sorted(value)) or "—"
    # Переводы строк ломают выравнивание — схлопываем в один видимый маркер.
    return " ⏎ ".join(str(value).splitlines()) or "—"


def local_time(value: str | None, tz_name: str = "Europe/Moscow") -> str:
    """ISO-время из базы (UTC) → «ДД.ММ ЧЧ:ММ» в рабочей таймзоне."""
    if not value:
        return "—"
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return str(value)[:16].replace("T", " ")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        moment = moment.astimezone(ZoneInfo(tz_name))
    except Exception:  # noqa: BLE001 — нет tzdata: покажем как есть
        pass
    return moment.strftime("%d.%m %H:%M")


def _clip(value: str, size: int) -> str:
    return value if len(value) <= size else value[: max(1, size - 1)] + "…"


def section(title: str) -> str:
    return f"\n\033[1m{title}\033[0m" if _tty() else f"\n{title}\n{'=' * len(title)}"


def warn(text: str) -> str:
    return f"\033[33m{text}\033[0m" if _tty() else f"! {text}"


def bad(text: str) -> str:
    return f"\033[31m{text}\033[0m" if _tty() else f"!! {text}"


def good(text: str) -> str:
    return f"\033[32m{text}\033[0m" if _tty() else text


def _tty() -> bool:
    import sys

    return sys.stdout.isatty()


def kv(pairs: Iterable[tuple[str, Any]]) -> str:
    pairs = list(pairs)
    if not pairs:
        return "(пусто)"
    width = max(len(str(key)) for key, _ in pairs)
    return "\n".join(f"{str(key).ljust(width)}  {_fmt(value)}" for key, value in pairs)
