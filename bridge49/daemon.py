"""Постоянные процессы контура. Пока один — приём входящих.

## Зачем вообще постоянный процесс

Приём сегодня живёт таймером раз в 15 секунд, то есть 5760 запусков в сутки.
Каждый поднимает интерпретатор, прогоняет схему базы и открывает **два**
независимых пула asyncpg с TLS до PgBouncer — по одному на результаты и на
входящие. Около 11 500 подключений в сутки ради работы, которая занимает доли
секунды. Постоянный процесс сводит это к одному пулу и одному соединению с
базой, и заодно снимает потолок частоты: шаг можно опустить ниже 15 секунд,
не увеличивая цену пропорционально.

## Почему зеркало здесь, а не отдельно

Зеркало пишет те же таблицы, что и приём (`threads`, `forum_posts`, курсоры).
Держать его отдельным процессом — значит завести двух писателей одной зоны и
разбираться с их взаимной блокировкой. Здесь оно подзадача со своим шагом:
приём каждые несколько секунд, зеркало — раз в минуту.

## Чего постоянный процесс лишается и что приходится вернуть руками

Три свойства systemd отдавал бесплатно, и их легко потерять молча:

*Взаимоисключение.* Таймер не запускает второй прогон, пока идёт первый.
Демон этого не даёт: ручной `bridge49 poll` рядом с ним — это два писателя.
Отсюда замок на зону, который берут и демон, и те же команды CLI.

*Свежесть настроек.* Разовый прогон читает конфиг заново каждый раз. Демон,
прочитавший его на старте, перестал бы замечать выключенные рубильники — это
потеря предохранителя, а не удобства. Поэтому настройки перечитываются на
каждой итерации.

*Отличие «упал» от «завис».* Упавший oneshot виден systemd. Зависший демон
выглядит здоровым. Поэтому он оставляет отметку живости в базе, а смотрит на
неё сторож — отдельный процесс, который сам может доложить о своей смерти.

## Что перестаёт происходить само

Схема базы больше не прогоняется при каждом запуске: демон делает это один раз
при старте. Выкладка с новой колонкой обязана применить схему до рестарта.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from .config import Settings, load as load_settings
from .store import Store, dumps, now

# Мост и опросы тянут за собой asyncpg, а зеркало — сеть. Всё это нужно только
# боевым подстановкам ниже, поэтому импортируется внутри них: цикл, замок зоны
# и отметка живости обязаны быть проверяемы там, где драйвера базы нет вовсе.

#: Имя зоны. Замок и отметка живости именуются по нему, чтобы у каждой зоны
#: они были свои и не пересекались.
ZONE_INGEST = "ingest"

#: Шаг приёма. Пятнадцать секунд достались от таймера, где шаг упирался в цену
#: запуска; здесь этой цены нет, и пять секунд ближе к живому разговору.
DEFAULT_STEP_SEC = 5.0

#: Шаг зеркала. Оно догоняет пачками и от секунд ничего не выигрывает.
DEFAULT_FORUM_STEP_SEC = 60.0

#: После скольких подряд неудачных итераций считаем, что дело не в помехе.
#: Шаг растёт, чтобы не молотить в недоступную базу и не забивать журнал.
BACKOFF_AFTER = 3
BACKOFF_MAX_SEC = 60.0


class ZoneBusy(RuntimeError):
    """Зону уже держит другой процесс этой же установки."""


@contextmanager
def zone_lock(settings: Settings, zone: str, *, wait_sec: float = 0.0):
    """Один процесс на зону в пределах установки.

    Замок именно файловый и именно в `var` дома: он должен разделять процессы
    одной установки и НЕ разделять разные (стенд и бой обязаны работать
    одновременно и независимо). Оборотная сторона честная — на двух машинах
    над одной базой он не спасает, поэтому такой схемы у нас и нет.
    """
    path = Path(settings.home) / "var" / f"{zone}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    deadline = time.monotonic() + max(0.0, float(wait_sec))
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ZoneBusy(
                        f"зону {zone} уже держит другой процесс ({path})"
                    ) from exc
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def beat_key(zone: str) -> str:
    return f"daemon:{zone}:beat"


def heartbeat(store: Store, zone: str, payload: dict) -> None:
    """Отметка живости. Смотрит на неё сторож, а не сам процесс."""
    store.set_state(beat_key(zone), dumps({
        "at": now(),
        "host": socket.gethostname(),
        "pid": os.getpid(),
        **payload,
    }))


@dataclass
class Tally:
    """Счётчики итераций. В журнал уходит сводка, а не каждый круг."""

    iterations: int = 0
    results: int = 0
    inbound: int = 0
    mirrored: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str = ""
    started_monotonic: float = field(default_factory=time.monotonic)

    def as_dict(self) -> dict:
        return {
            "итераций": self.iterations,
            "результатов": self.results,
            "входящих": self.inbound,
            "в зеркало": self.mirrored,
            "сбоев": self.failures,
            "последняя ошибка": self.last_error,
            "секунд работы": round(time.monotonic() - self.started_monotonic, 1),
        }


async def _default_connect(settings: Settings):
    """Одно соединение на всю жизнь процесса — ради него всё и затевалось."""
    from .radar import RadarBridge

    bridge = RadarBridge(settings.dsn)
    await bridge.connect()
    return bridge


async def _default_results(store, settings, bridge) -> dict:
    from . import pollers

    return await pollers.poll_results(
        store, settings, actor="daemon:ingest", bridge=bridge)


async def _default_inbound(store, settings, bridge) -> dict:
    from . import pollers

    return await pollers.poll_inbound(
        store, settings, actor="daemon:ingest", bridge=bridge)


def _default_mirror(store, settings) -> dict:
    """Зеркало включается переменными окружения, как и раньше.

    В стенде их нет, и это ровно то, что нужно: копия контура не должна
    заливать боевую группу второй копией той же переписки.
    """
    from . import forum as forum_module

    if not forum_module.enabled():
        return {}
    return forum_module.run(store, limit=30, actor="daemon:ingest")


async def run_ingest(
    settings: Settings,
    *,
    step: float = DEFAULT_STEP_SEC,
    forum_step: float = DEFAULT_FORUM_STEP_SEC,
    max_iterations: int | None = None,
    stop: asyncio.Event | None = None,
    connect: Callable[[Settings], Awaitable[object]] = _default_connect,
    poll_results: Callable[..., Awaitable[dict]] = _default_results,
    poll_inbound: Callable[..., Awaitable[dict]] = _default_inbound,
    mirror: Callable[..., dict] = _default_mirror,
    reload_settings: Callable[[], Settings] | None = None,
) -> Tally:
    """Цикл приёма. Возвращает сводку, когда его попросили остановиться."""
    stop = stop or asyncio.Event()
    reload_settings = reload_settings or (lambda: load_settings(settings.home))
    tally = Tally()
    store = Store(settings.db_path)
    bridge: object | None = None
    next_mirror = 0.0

    try:
        while not stop.is_set():
            if max_iterations is not None and tally.iterations >= max_iterations:
                break
            tally.iterations += 1

            # Настройки — заново на каждом круге. Иначе выключенный рубильник
            # начнёт действовать только после рестарта, а это уже не рубильник.
            try:
                settings = reload_settings()
            except Exception as exc:  # конфиг могли править прямо сейчас
                tally.last_error = f"конфиг: {exc}"

            try:
                if bridge is None:
                    bridge = await connect(settings)

                result = await poll_results(store, settings, bridge)
                tally.results += int(result.get("updated") or 0)

                incoming = await poll_inbound(store, settings, bridge)
                tally.inbound += int(incoming.get("stored") or 0)

                if time.monotonic() >= next_mirror:
                    next_mirror = time.monotonic() + forum_step
                    mirrored = mirror(store, settings)
                    tally.mirrored += int(mirrored.get("sent") or 0)

                tally.consecutive_failures = 0
            except Exception as exc:
                tally.failures += 1
                tally.consecutive_failures += 1
                tally.last_error = f"{type(exc).__name__}: {exc}"
                store.log("daemon:ingest", "daemon.error", ZONE_INGEST,
                          tally.last_error)
                # Соединение могло умереть вместе с базой на той стороне:
                # держаться за мёртвый пул нет смысла, следующий круг поднимет
                # новый.
                if bridge is not None:
                    try:
                        await bridge.close()
                    except Exception:
                        pass
                    bridge = None

            heartbeat(store, ZONE_INGEST, {
                "iterations": tally.iterations,
                "failures": tally.failures,
                "last_error": tally.last_error,
                "step": step,
            })
            # Коммит здесь обязателен и неочевиден: `set_state` и `log` только
            # выполняют запрос, а разовому прогону коммит делал кто-то другой
            # по дороге к выходу. У постоянного процесса этого «кого-то» нет,
            # и незакоммиченная отметка живости не существует для сторожа —
            # то есть работающий процесс выглядел бы мёртвым.
            store.commit()

            delay = step
            if tally.consecutive_failures >= BACKOFF_AFTER:
                delay = min(BACKOFF_MAX_SEC,
                            step * (2 ** (tally.consecutive_failures - BACKOFF_AFTER + 1)))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
    finally:
        if bridge is not None:
            try:
                await bridge.close()
            except Exception:
                pass
        store.close()
    return tally


def install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM от systemd — это «доработай круг», а не «умри немедленно».

    Резкая смерть посреди записи входящих не теряет данные (курсор двигается
    после коммита), но оставляет за собой лишнюю работу на следующий запуск.
    Дешевле дать циклу закончить итерацию.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # не все платформы это умеют
            pass
