"""Диспетчер: единственное место, где bridge49 пишет в Radar.

Порядок операций выбран так, чтобы обрыв связи в любой точке не привёл к
повторной отправке:

1. UUID запроса пишется в локальную базу **до** обращения к Radar, причём
   атомарно — `WHERE request_id IS NULL`;
2. затем вызывается enqueue;
3. затем сохраняется полученный command_id.

Если процесс умер между 1 и 3, повторный прогон вызывает enqueue с тем же
UUID и тем же каноническим request. Контракт моста гарантирует, что такой
повтор идемпотентен и вернёт уже существующий command_id, а не создаст вторую
отправку. Новый UUID после таймаута — единственное, чего делать нельзя.

Весь прогон дополнительно защищён файловой блокировкой: два одновременных
`dispatch` (например, таймер и ручной запуск) иначе выдали бы одной задаче
два разных UUID, а два UUID — это два `dedup_key` и две реальные отправки.
"""
from __future__ import annotations

import fcntl
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import accounts as accounts_mod
from . import catalog, report
from .config import Settings
from .radar import (
    BridgeError,
    BridgeRejected,
    BridgeUnknown,
    QueueFull,
    RadarBridge,
    build_request,
    new_request_id,
)
from .store import Store, loads, now

#: Действия, которые собеседник видит. По ним считается дневной лимит.
VISIBLE_ACTIONS = tuple(
    sorted(name for name, a in catalog.ACTIONS.items() if a.visible)
)


class DispatchBlocked(RuntimeError):
    """Задачу нельзя выпускать прямо сейчас. Задача остаётся запланированной."""


class DispatchBusy(RuntimeError):
    """Другой процесс уже выпускает задачи."""


@contextmanager
def exclusive(settings: Settings):
    """Один выпускающий процесс на установку."""
    path = Path(settings.home) / "var" / "dispatch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise DispatchBusy(
                f"другой dispatch уже работает (блокировка {path})"
            ) from exc
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def due_tasks(store: Store, *, campaign_id: str | None = None,
              limit: int = 50) -> list[dict]:
    """Задачи, которым пора: время пришло."""
    sql = (
        "SELECT t.*, c.status AS campaign_status, c.name AS campaign_name "
        "FROM tasks t JOIN campaigns c ON c.id = t.campaign_id "
        "WHERE t.state = 'planned' AND t.scheduled_at <= ? "
    )
    params: list = [now()]
    if campaign_id:
        sql += "AND t.campaign_id = ? "
        params.append(campaign_id)
    sql += "ORDER BY t.scheduled_at, t.id LIMIT ?"
    params.append(int(limit))

    rows = [dict(row) for row in store.query(sql, params)]
    for row in rows:
        row["params"] = loads(row.get("params"), {})
    return rows


def orphaned(store: Store, *, campaign_id: str | None = None) -> list[dict]:
    """Задачи с UUID, но без command_id — прошлый прогон оборвался.

    Кампанию джойним обязательно: UUID пишется ДО обращения к Radar, поэтому
    оборванная задача могла ещё не уехать вовсе. Без статуса кампании такая
    задача прошла бы гейт «только active» и выпустилась из поставленной на
    паузу кампании.
    """
    sql = (
        "SELECT t.*, c.status AS campaign_status, c.name AS campaign_name "
        "FROM tasks t JOIN campaigns c ON c.id = t.campaign_id "
        "WHERE t.state = 'planned' AND t.request_id IS NOT NULL "
    )
    params: list = []
    if campaign_id:
        sql += "AND t.campaign_id = ? "
        params.append(campaign_id)
    sql += "ORDER BY t.scheduled_at"

    out = []
    for row in store.query(sql, params):
        item = dict(row)
        item["params"] = loads(item.get("params"), {})
        out.append(item)
    return out


def visible_sent_today(store: Store, account_id: int) -> int:
    """Сколько видимых действий аккаунт уже выпустил за сегодня.

    Считаем только видимые: read и soft собеседник не наблюдает, и лимит на
    них не распространяется — значит и бюджет они тратить не должны.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    placeholders = ",".join("?" * len(VISIBLE_ACTIONS))
    row = store.one(
        "SELECT count(*) AS n FROM tasks "
        f"WHERE account_id = ? AND action IN ({placeholders}) "
        "  AND substr(dispatched_at, 1, 10) = ? "
        "  AND state NOT IN ('cancelled', 'blocked')",
        (int(account_id), *VISIBLE_ACTIONS, today),
    )
    return int(row["n"])


def last_visible_dispatch_at(store: Store, account_id: int) -> datetime | None:
    """Когда аккаунт в последний раз реально выпускал видимое действие.

    Читаем `dispatched_at`, а не `scheduled_at`: пол меряется от факта
    отправки. Задача, пролежавшая в плане сутки, не даёт аккаунту права
    отправить две подряд.
    """
    placeholders = ",".join("?" * len(VISIBLE_ACTIONS))
    row = store.one(
        "SELECT max(dispatched_at) AS last FROM tasks "
        f"WHERE account_id = ? AND action IN ({placeholders}) "
        "  AND dispatched_at IS NOT NULL "
        "  AND state NOT IN ('cancelled', 'blocked')",
        (int(account_id), *VISIBLE_ACTIONS),
    )
    if not row or not row["last"]:
        return None
    try:
        return datetime.fromisoformat(str(row["last"]))
    except ValueError:
        return None


def inside_send_window(settings: Settings, moment: datetime | None = None) -> bool:
    """Идёт ли сейчас окно отправки в рабочей таймзоне."""
    limits = settings.limits
    if not limits.send_window_start_hour and not limits.send_window_end_hour:
        return True
    moment = moment or datetime.now(timezone.utc)
    try:
        local = moment.astimezone(ZoneInfo(settings.timezone))
    except Exception:  # noqa: BLE001 — нет tzdata: не мешаем работе
        return True
    if local.weekday() not in limits.send_weekdays:
        return False
    return limits.send_window_start_hour <= local.hour < limits.send_window_end_hour


def preflight(
    store: Store,
    task: dict,
    settings: Settings,
    *,
    spent: dict[int, int] | None = None,
    recent: dict[int, datetime] | None = None,
) -> catalog.Action:
    """Все проверки, которые дешевле сделать до обращения к базе.

    ``spent`` и ``recent`` нужны только предпросмотру: там ничего не пишется в
    базу, и без накопительных счётчиков лимит и пауза выглядели бы
    замороженными — предпросмотр показал бы весь батч как готовый к выпуску.
    В боевом цикле их не передают: там после каждой отправки коммитится
    `dispatched_at`, и запрос к базе сам по себе актуален.
    """
    if task.get("campaign_status") not in (None, "active"):
        raise DispatchBlocked(
            f"кампания в статусе {task['campaign_status']}, нужен active"
        )

    account_id = int(task["account_id"])
    account = accounts_mod.get(store, account_id)
    if account is None:
        raise DispatchBlocked(f"нет аккаунта {account_id} в реестре")

    ok, why = accounts_mod.usable(account, task["action"])
    if not ok:
        raise DispatchBlocked(why)

    contact = store.one(
        "SELECT opted_out FROM contacts WHERE id = ?", (task["contact_id"],)
    )
    if contact and contact["opted_out"]:
        raise DispatchBlocked("контакт отказался от коммуникации")

    action = catalog.validate(
        task["action"], task["params"], roles=account["roles"],
        allowed_actions=account["allowed_actions"] or None,
    )

    if task["mode"] == "immediate" and action.risk in catalog.IMMEDIATE_GATED_RISKS:
        if not account["allow_immediate"]:
            raise DispatchBlocked(
                "mode=immediate для видимого действия требует "
                "allow_immediate_visible_actions=true у аккаунта в Radar"
            )

    if action.visible:
        # Окно проверяется ещё раз здесь, а не только при планировании:
        # задача могла пролежать в очереди до ночи из-за паузы или сбоя.
        if not inside_send_window(settings):
            raise DispatchBlocked(
                "сейчас вне окна отправки "
                f"({settings.limits.send_window_start_hour}:00–"
                f"{settings.limits.send_window_end_hour}:00 {settings.timezone})"
            )
        already = visible_sent_today(store, account_id)
        already += int((spent or {}).get(account_id, 0))
        if already >= settings.limits.per_account_daily_visible:
            raise DispatchBlocked(
                f"аккаунт уже выпустил {already} видимых действий за сегодня "
                f"(лимит {settings.limits.per_account_daily_visible})"
            )

        # Пауза между отправками проверяется здесь, а не только при
        # планировании. Планировщик раскладывает по слотам, но в базу задачи
        # попадают и мимо него: повторный plan, вторая кампания на тот же
        # сегмент, импорт, правка руками. Пол должен держать в любом из этих
        # случаев, иначе весь пакет уедет одной секундой.
        interval = int(settings.limits.per_account_visible_interval_sec or 0)
        if interval > 0:
            last = last_visible_dispatch_at(store, account_id)
            simulated = (recent or {}).get(account_id)
            if simulated is not None and (last is None or simulated > last):
                last = simulated
            if last is not None:
                moment = datetime.now(timezone.utc)
                wait = int((last + timedelta(seconds=interval) - moment).total_seconds())
                if wait > 0:
                    raise DispatchBlocked(
                        f"аккаунт отправлял меньше {interval} с назад — "
                        f"ждём ещё {wait} с"
                    )

    return action


def _claim(store: Store, task: dict) -> str:
    """Закрепить за задачей UUID. Если её уже забрали — вернуть чужой.

    Атомарность здесь важнее краткости: без ``WHERE request_id IS NULL`` два
    процесса выдали бы одной задаче два UUID, а это две отправки.
    """
    existing = task.get("request_id")
    if existing:
        return str(existing)

    request_id = new_request_id()
    cursor = store.execute(
        "UPDATE tasks SET request_id = ?, updated_at = ? "
        "WHERE id = ? AND request_id IS NULL",
        (request_id, now(), task["id"]),
    )
    store.commit()
    if cursor.rowcount:
        return request_id

    row = store.one("SELECT request_id FROM tasks WHERE id = ?", (task["id"],))
    return str(row["request_id"]) if row and row["request_id"] else request_id


async def dispatch(
    store: Store,
    settings: Settings,
    *,
    campaign_id: str | None = None,
    limit: int | None = None,
    confirm: bool = False,
    actor: str = "cli",
) -> dict:
    """Выпустить созревшие задачи в Radar.

    Без `confirm` и без файла ARMED ничего не отправляется — возвращается
    предпросмотр, ровно тот же список, который ушёл бы в боевом режиме.
    """
    limit = int(limit or settings.limits.dispatch_batch)
    armed = settings.armed
    if confirm and armed:
        with exclusive(settings):
            return await _dispatch_armed(
                store, settings, campaign_id=campaign_id, limit=limit, actor=actor
            )
    return _preview(store, settings, campaign_id=campaign_id, limit=limit,
                    armed=armed, confirmed=confirm)


def _queue(store: Store, campaign_id: str | None, limit: int) -> list[dict]:
    pending = orphaned(store, campaign_id=campaign_id)
    fresh = due_tasks(store, campaign_id=campaign_id, limit=limit)
    seen = {task["id"] for task in pending}
    # Оборванные с прошлого раза идут первыми: их надо доиграть тем же UUID.
    return (pending + [t for t in fresh if t["id"] not in seen])[:limit]


def _preview(
    store: Store, settings: Settings, *, campaign_id: str | None, limit: int,
    armed: bool, confirmed: bool,
) -> dict:
    spent: dict[int, int] = {}
    recent: dict[int, datetime] = {}
    ready: list[dict] = []
    blocked: list[dict] = []
    for task in _queue(store, campaign_id, limit):
        try:
            action = preflight(store, task, settings, spent=spent, recent=recent)
        except DispatchBlocked as exc:
            blocked.append({"task": task["id"], "why": str(exc)})
            continue
        if action.visible:
            account_id = int(task["account_id"])
            spent[account_id] = spent.get(account_id, 0) + 1
            # Весь батч ушёл бы одним прогоном, то есть «сейчас».
            recent[account_id] = datetime.now(timezone.utc)
        ready.append(task)

    return {
        "armed": armed,
        "confirmed": confirmed,
        "would_dispatch": len(ready),
        "blocked": blocked,
        "tasks": [_preview_row(store, t, settings.timezone) for t in ready],
        "dispatched": 0,
        "dry_run": True,
    }


async def _dispatch_armed(
    store: Store, settings: Settings, *, campaign_id: str | None, limit: int,
    actor: str,
) -> dict:
    queue = _queue(store, campaign_id, limit)
    sent = 0
    blocked: list[dict] = []
    failed: list[dict] = []
    deferred: list[dict] = []

    async with RadarBridge(settings.dsn) as bridge:
        for task in queue:
            # Проверяем непосредственно перед каждой отправкой, а не заранее
            # для всего батча: иначе дневной лимит внутри прогона заморожен.
            try:
                preflight(store, task, settings)
            except DispatchBlocked as exc:
                blocked.append({"task": task["id"], "why": str(exc)})
                continue

            request_id = _claim(store, task)
            expires = (
                datetime.fromisoformat(task["expires_at"])
                if task.get("expires_at") else None
            )
            request = build_request(
                action=task["action"],
                params=task["params"],
                mode=task["mode"],
                request_id=request_id,
                expires_at=expires,
                external_job_id=task["campaign_id"],
                external_conversation_id=task["contact_id"],
            )

            try:
                command_id = await bridge.enqueue(int(task["account_id"]), request)
            except QueueFull as exc:
                # До вставки дело не дошло; остальной батч ждать смысла нет.
                deferred.append({"task": task["id"], "why": f"очередь моста полна: {exc}"})
                _note(store, task["id"], str(exc))
                break
            except BridgeRejected as exc:
                # Детерминированный отказ: повтор даст тот же результат.
                failed.append({"task": task["id"], "why": str(exc)})
                store.execute(
                    "UPDATE tasks SET state='blocked', error_code='bridge_rejected', "
                    "error_message=?, updated_at=? WHERE id=?",
                    (str(exc)[:500], now(), task["id"]),
                )
                store.commit()
                continue
            except (BridgeUnknown, BridgeError) as exc:
                # Могло уехать, а могло и нет. Оставляем задачу запланированной
                # вместе с её UUID — следующий прогон переиграет ТЕМ ЖЕ UUID,
                # и мост вернёт прежний command_id, если команда всё-таки есть.
                deferred.append({"task": task["id"], "why": str(exc)})
                _note(store, task["id"], str(exc))
                continue

            store.execute(
                "UPDATE tasks SET state='queued', command_id=?, error_message=NULL, "
                "dispatched_at=?, updated_at=? WHERE id=?",
                (int(command_id), now(), now(), task["id"]),
            )
            store.execute(
                "UPDATE contacts SET status = CASE WHEN status = 'new' "
                "THEN 'contacted' ELSE status END, updated_at = ? WHERE id = ?",
                (now(), task["contact_id"]),
            )
            store.commit()
            sent += 1

    store.log(
        actor, "dispatch", campaign_id or "*",
        f"sent={sent} blocked={len(blocked)} deferred={len(deferred)} "
        f"failed={len(failed)}",
    )
    store.commit()
    return {
        "armed": True,
        "confirmed": True,
        "dispatched": sent,
        "blocked": blocked,
        "deferred": deferred,
        "failed": failed,
        "dry_run": False,
    }


def _note(store: Store, task_id: str, message: str) -> None:
    store.execute(
        "UPDATE tasks SET error_message = ?, updated_at = ? WHERE id = ?",
        (message[:500], now(), task_id),
    )
    store.commit()


def unblock(store: Store, task_ids: list[str], *, actor: str = "cli") -> int:
    """Вернуть заблокированные задачи в план — после разбора причины."""
    changed = 0
    for task_id in task_ids:
        cursor = store.execute(
            "UPDATE tasks SET state='planned', error_code=NULL, error_message=NULL, "
            "updated_at=? WHERE id=? AND state='blocked'",
            (now(), task_id),
        )
        changed += cursor.rowcount
    store.log(actor, "tasks.unblock", "", f"unblocked={changed}")
    store.commit()
    return changed


def _preview_row(store: Store, task: dict, tz_name: str = "Europe/Moscow") -> dict:
    contact = store.one(
        "SELECT username, display_name FROM contacts WHERE id = ?",
        (task["contact_id"],),
    )
    account = store.one(
        "SELECT label FROM accounts WHERE id = ?", (int(task["account_id"]),)
    )
    text = str(task["params"].get("text") or "")
    return {
        "task": task["id"],
        "when": report.local_time(task["scheduled_at"], tz_name),
        "account": f"{task['account_id']} {account['label'] if account else ''}".strip(),
        "target": (contact["username"] if contact else None) or task["contact_id"],
        "action": task["action"],
        "mode": task["mode"],
        "risk": catalog.ACTIONS[task["action"]].risk,
        "preview": (text[:70] + "…") if len(text) > 70 else text,
    }


def arm(settings: Settings, on: bool, *, actor: str = "cli") -> str:
    """Включить или выключить боевой режим."""
    path = settings.armed_file
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"armed by {actor} at {now()}\n"
            "Пока этот файл существует, dispatch --confirm реально ставит\n"
            "команды в Radar. Удалите файл, чтобы вернуться к предпросмотру.\n",
            encoding="utf-8",
        )
        return f"боевой режим ВКЛЮЧЁН: {path}"
    if path.exists():
        path.unlink()
    return f"боевой режим выключен (файла {path} нет)"
