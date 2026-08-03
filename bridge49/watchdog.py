"""Сторож контура: замечает молчание раньше, чем его заметит человек.

Тишина здесь неотличима от нормальной работы. Поллер, который умер, выглядит
ровно как поллер, которому нечего забрать; отвалившийся мост — как отсутствие
входящих. Поэтому проверки построены не на «есть ли ошибки», а на «когда в
последний раз что-то заведомо происходило».

Сторож ничего не чинит и никуда не пишет, кроме журнала событий и файла
состояния: право останавливать и запускать остаётся за человеком.
"""
from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import alerts
from .config import Settings
from .radar import RadarBridge
from .store import Store, dumps, now

CRITICAL = "critical"
HIGH = "high"
WARNING = "warning"

#: Порядок важности — от него зависит и сортировка вывода, и код возврата.
SEVERITY_ORDER = {CRITICAL: 0, HIGH: 1, WARNING: 2}

#: Поллер ходит раз в 15 секунд. Три минуты тишины — это уже не разброс
#: расписания, а остановка.
POLL_SILENCE_SEC = 180
#: Входящее, которое мост показывает, а мы не забрали дольше этого времени.
INBOUND_LAG_SEC = 300
#: Карточка менеджеру, до которой сутки никто не дошёл.
HANDOFF_STALE_HOURS = 24
#: Задача, застрявшая в очереди Radar без исхода.
QUEUED_STALE_HOURS = 6


@dataclass
class Finding:
    """Одно наблюдение сторожа."""

    check: str
    severity: str
    detail: str


@dataclass
class Report:
    checked_at: str
    findings: list[Finding] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.findings

    @property
    def worst(self) -> str | None:
        if not self.findings:
            return None
        return min(
            (f.severity for f in self.findings), key=lambda s: SEVERITY_ORDER.get(s, 9)
        )

    def as_dict(self) -> dict:
        return {
            "checked_at": self.checked_at,
            "ok": self.ok,
            "worst": self.worst,
            "facts": self.facts,
            "findings": [
                {"check": f.check, "severity": f.severity, "detail": f.detail}
                for f in sorted(
                    self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9)
                )
            ],
        }


def _age_seconds(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


def _last_event_at(store: Store, kind: str) -> str | None:
    row = store.one(
        "SELECT max(at) AS at FROM events WHERE kind = ?", (kind,)
    )
    return row["at"] if row and row["at"] else None


def check_poller(store: Store, report: Report) -> None:
    """Ходит ли поллер. Пустой ответ — тоже работа, важен сам факт прогона."""
    last = _last_event_at(store, "poll.inbound")
    report.facts["last_poll_at"] = last
    if last is None:
        report.findings.append(
            Finding("поллер", CRITICAL, "ни одного прогона poll в журнале")
        )
        return
    age = _age_seconds(last)
    if age is not None and age > POLL_SILENCE_SEC:
        report.findings.append(
            Finding(
                "поллер", CRITICAL,
                f"последний прогон {int(age // 60)} мин назад "
                f"(порог {POLL_SILENCE_SEC // 60} мин) — таймер стоит?",
            )
        )


def check_handoffs(store: Store, report: Report) -> None:
    """Карточки, до которых никто не дошёл. Ответ клиента протухает быстро."""
    edge = (
        datetime.now(timezone.utc) - timedelta(hours=HANDOFF_STALE_HOURS)
    ).isoformat()
    row = store.one(
        "SELECT count(*) AS n FROM handoffs WHERE status = 'new' AND created_at < ?",
        (edge,),
    )
    stale = int(row["n"]) if row else 0
    report.facts["stale_handoffs"] = stale
    if stale:
        report.findings.append(
            Finding(
                "ответы клиентов", WARNING,
                f"{stale} карточек висят дольше {HANDOFF_STALE_HOURS} ч",
            )
        )


def check_stuck_tasks(store: Store, report: Report) -> None:
    """Задачи, ушедшие в Radar и не получившие исхода."""
    edge = (
        datetime.now(timezone.utc) - timedelta(hours=QUEUED_STALE_HOURS)
    ).isoformat()
    row = store.one(
        "SELECT count(*) AS n FROM tasks WHERE state = 'queued' "
        "AND COALESCE(dispatched_at, attempted_at) < ?",
        (edge,),
    )
    stuck = int(row["n"]) if row else 0
    report.facts["stuck_tasks"] = stuck
    if stuck:
        report.findings.append(
            Finding(
                "очередь задач", WARNING,
                f"{stuck} задач в queued дольше {QUEUED_STALE_HOURS} ч без исхода",
            )
        )


def check_fleet(store: Store, settings: Settings, report: Report) -> None:
    """Боевой режим включён, а работать некому — тихий простой рассылки."""
    row = store.one(
        "SELECT count(*) AS n FROM accounts WHERE paused = 0 AND enabled = 1"
    )
    usable = int(row["n"]) if row else 0
    report.facts["accounts_usable"] = usable
    report.facts["armed"] = settings.armed
    if settings.armed and usable == 0:
        report.findings.append(
            Finding(
                "аккаунты", HIGH,
                "боевой режим включён, но ни одного доступного аккаунта",
            )
        )


async def check_bridge(store: Store, settings: Settings, report: Report) -> None:
    """Отвечает ли Radar и не отстаём ли мы от фида входящих."""
    if settings.dsn is None:
        report.findings.append(
            Finding("мост", HIGH, "нет реквизитов подключения к Radar")
        )
        return
    try:
        async with RadarBridge(settings.dsn) as bridge:
            health = await bridge.health()
    except Exception as exc:  # noqa: BLE001 — важен сам факт недоступности
        report.findings.append(
            Finding("мост", CRITICAL, f"Radar недоступен: {type(exc).__name__}: {exc}")
        )
        return

    report.facts["max_inbound_id"] = health.get("max_inbound_id")
    cursor = int(store.get_state("inbound_cursor", "0") or 0)
    report.facts["inbound_cursor"] = cursor
    behind = int(health.get("max_inbound_id") or 0) - cursor
    report.facts["inbound_behind"] = behind
    if behind > 0:
        # Отставание само по себе нормально: между появлением входящего и
        # прогоном поллера всегда есть зазор. Тревожно, только если поллер при
        # этом давно не ходил, — тогда зазор не закроется сам.
        age = _age_seconds(_last_event_at(store, "poll.inbound"))
        if age is None or age > INBOUND_LAG_SEC:
            report.findings.append(
                Finding(
                    "входящие", HIGH,
                    f"{behind} входящих в Radar не забраны, "
                    f"последний poll {int((age or 0) // 60)} мин назад",
                )
            )


def fingerprint(report: Report) -> str:
    """Отпечаток состояния: уровень плюс состав проблем.

    По одному лишь уровню сравнивать мало: новая критичная проблема поверх
    старой критичной не изменила бы его и осталась бы незамеченной. По полному
    тексту — наоборот, слишком дробно: возраст в минутах меняется каждый
    прогон, и сторож слал бы одно и то же каждые две минуты.
    """
    if not report.findings:
        return "ok"
    checks = ",".join(sorted({f.check for f in report.findings}))
    return f"{report.worst}:{checks}"


def compose_message(report: Report, *, host: str) -> str:
    """Текст для админ-форума."""
    if report.ok:
        return f"✅ outreach49 ({host}): всё восстановилось"
    head = f"🔴 outreach49 ({host}): {report.worst}"
    lines = [head, ""]
    for finding in sorted(
        report.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9)
    ):
        lines.append(f"[{finding.severity}] {finding.check}: {finding.detail}")
    facts = report.facts
    lines.append("")
    lines.append(
        f"последний poll: {facts.get('last_poll_at') or '—'}; "
        f"аккаунтов доступно: {facts.get('accounts_usable')}; "
        f"боевой режим: {'да' if facts.get('armed') else 'нет'}"
    )
    return "\n".join(lines)


async def deliver(report: Report, store: Store, *, actor: str) -> str | None:
    """Отправить тревогу в админку. Возвращает описание исхода или None."""
    target = alerts.TelegramTarget.from_file()
    if target is None or not target.enabled:
        return None
    message = compose_message(report, host=socket.gethostname())
    try:
        # Отправка блокирующая: уводим её из петли событий, чтобы сторож
        # оставался обычным async-кодом.
        message_id = await asyncio.to_thread(alerts.send, target, message)
    except alerts.AlertError as exc:
        # Молчать нельзя, но и падать незачем: сторож, не сумевший рассказать
        # о проблеме, не должен сам становиться второй проблемой.
        store.log(actor, "watchdog.alert_failed", target.describe(), str(exc)[:300])
        store.commit()
        return f"не доставлено: {exc}"
    store.log(actor, "watchdog.alert_sent", target.describe(), str(message_id))
    store.commit()
    return f"отправлено в {target.describe()}"


async def run(
    store: Store, settings: Settings, *, actor: str = "watchdog",
    with_bridge: bool = True, notify: bool = True,
) -> Report:
    """Прогнать все проверки, сохранить результат и сообщить об изменениях."""
    report = Report(checked_at=now())
    check_poller(store, report)
    check_handoffs(store, report)
    check_stuck_tasks(store, report)
    check_fleet(store, settings, report)
    if with_bridge:
        await check_bridge(store, settings, report)

    payload = report.as_dict()
    path = settings.home / "var" / "watchdog.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(payload), encoding="utf-8")

    # Пишем и сообщаем только о смене состояния, иначе события утонут в шуме:
    # сторож ходит часто, а рассказывать ему обычно нечего.
    previous = store.get_state("watchdog_state", "")
    current = fingerprint(report)
    if current == previous:
        return report

    first_run = not previous
    store.set_state("watchdog_state", current)
    store.log(
        actor, "watchdog", current,
        "; ".join(f"{f.check}: {f.detail}" for f in report.findings) or "всё ровно",
    )
    store.commit()

    # Первый прогон после установки состояния не имеет: сообщать «всё ровно»
    # там, где ничего не менялось, — лишний шум в общем форуме.
    if notify and not (first_run and report.ok):
        report.facts["alert"] = await deliver(report, store, actor=actor)
    return report
