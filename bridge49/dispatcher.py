"""Диспетчер: единственное место, где bridge49 пишет в Radar.

Порядок операций выбран так, чтобы обрыв связи в любой точке не привёл к
повторной отправке:

1. UUID запроса пишется в локальную базу **до** обращения к Radar;
2. затем вызывается enqueue;
3. затем сохраняется полученный command_id.

Если процесс умер между 1 и 3, повторный прогон вызывает enqueue с тем же
UUID и тем же каноническим request. Контракт моста гарантирует, что такой
повтор идемпотентен и вернёт уже существующий command_id, а не создаст вторую
отправку. Новый UUID после таймаута — единственное, чего делать нельзя.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import accounts as accounts_mod
from . import catalog, report
from .config import Settings
from .radar import BridgeError, QueueFull, RadarBridge, build_request, new_request_id
from .store import Store, dumps, loads, now


class NotArmed(RuntimeError):
    """Боевой режим выключен — так и задумано."""


class DispatchBlocked(RuntimeError):
    """Задачу нельзя выпускать прямо сейчас."""


def due_tasks(store: Store, *, campaign_id: str | None = None,
              limit: int = 50) -> list[dict]:
    """Задачи, которым пора: время пришло, кампания активна."""
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


def orphaned(store: Store) -> list[dict]:
    """Задачи с UUID, но без command_id — прошлый прогон оборвался."""
    rows = store.query(
        "SELECT * FROM tasks WHERE state = 'planned' AND request_id IS NOT NULL "
        "ORDER BY scheduled_at"
    )
    out = []
    for row in rows:
        item = dict(row)
        item["params"] = loads(item.get("params"), {})
        out.append(item)
    return out


def preflight(store: Store, task: dict, settings: Settings) -> None:
    """Все проверки, которые дешевле сделать до обращения к базе."""
    if task.get("campaign_status") not in (None, "active"):
        raise DispatchBlocked(
            f"кампания в статусе {task['campaign_status']}, нужен active"
        )

    account = accounts_mod.get(store, int(task["account_id"]))
    if account is None:
        raise DispatchBlocked(f"нет аккаунта {task['account_id']} в реестре")

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

    # Второй пояс поверх темпа Radar: свой дневной лимит на аккаунт.
    if action.visible:
        today = datetime.now(timezone.utc).date().isoformat()
        sent = store.one(
            "SELECT count(*) AS n FROM tasks "
            "WHERE account_id = ? AND substr(dispatched_at, 1, 10) = ? "
            "  AND state NOT IN ('cancelled', 'blocked')",
            (int(task["account_id"]), today),
        )
        if int(sent["n"]) >= settings.limits.per_account_daily_visible:
            raise DispatchBlocked(
                f"аккаунт уже выпустил {sent['n']} видимых действий за сегодня "
                f"(лимит {settings.limits.per_account_daily_visible})"
            )


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
    pending = orphaned(store)
    fresh = due_tasks(store, campaign_id=campaign_id, limit=limit)
    # Оборванные с прошлого раза идут первыми: их надо доиграть тем же UUID.
    queue = pending + [t for t in fresh if t["id"] not in {p["id"] for p in pending}]
    queue = queue[:limit]

    preview: list[dict] = []
    blocked: list[dict] = []
    for task in queue:
        try:
            preflight(store, task, settings)
        except DispatchBlocked as exc:
            blocked.append({"task": task["id"], "why": str(exc)})
            continue
        preview.append(task)

    armed = settings.armed
    if not (confirm and armed):
        return {
            "armed": armed,
            "confirmed": confirm,
            "would_dispatch": len(preview),
            "blocked": blocked,
            "tasks": [_preview_row(store, t, settings.timezone) for t in preview],
            "dispatched": 0,
            "dry_run": True,
        }

    sent, failed = 0, []
    async with RadarBridge(settings.dsn) as bridge:
        for task in preview:
            request_id = task.get("request_id") or new_request_id()
            if not task.get("request_id"):
                # Шаг 1: фиксируем UUID до любого обращения к Radar.
                store.execute(
                    "UPDATE tasks SET request_id = ?, updated_at = ? WHERE id = ?",
                    (request_id, now(), task["id"]),
                )
                store.commit()

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
                # Шаг 2: сам enqueue. Повтор с тем же UUID идемпотентен.
                command_id = await bridge.enqueue(int(task["account_id"]), request)
            except QueueFull as exc:
                failed.append({"task": task["id"], "why": f"очередь моста полна: {exc}"})
                break
            except BridgeError as exc:
                failed.append({"task": task["id"], "why": str(exc)})
                store.execute(
                    "UPDATE tasks SET state='blocked', error_message=?, "
                    "updated_at=? WHERE id=?",
                    (str(exc)[:500], now(), task["id"]),
                )
                store.commit()
                continue

            # Шаг 3: запоминаем, что вернул мост.
            store.execute(
                "UPDATE tasks SET state='queued', command_id=?, dispatched_at=?, "
                "updated_at=? WHERE id=?",
                (int(command_id), now(), now(), task["id"]),
            )
            store.execute(
                "UPDATE contacts SET status = CASE WHEN status = 'new' "
                "THEN 'contacted' ELSE status END, updated_at = ? WHERE id = ?",
                (now(), task["contact_id"]),
            )
            store.commit()
            sent += 1

    store.log(actor, "dispatch", campaign_id or "*",
              f"sent={sent} blocked={len(blocked)} failed={len(failed)}")
    store.commit()
    return {
        "armed": True,
        "confirmed": True,
        "dispatched": sent,
        "blocked": blocked,
        "failed": failed,
        "dry_run": False,
    }


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
