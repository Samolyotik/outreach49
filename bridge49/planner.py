"""Планировщик: превращает кампанию в конкретные задачи с временем и аккаунтом.

Планирование ничего не отправляет и в Radar не ходит. Это чистая функция от
локального состояния, поэтому её можно гонять сколько угодно раз и смотреть,
что получится, до того как что-то реально уедет.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import accounts as accounts_mod
from . import catalog, entities
from .config import Limits
from .store import Store, dumps, loads, new_id, now


class PlanError(RuntimeError):
    """Кампанию нельзя спланировать в текущем виде."""


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 — на машине может не быть tzdata
        return ZoneInfo("UTC")


def next_slot(
    after: datetime, limits: Limits, tz: ZoneInfo, *, paced: bool
) -> datetime:
    """Ближайший момент внутри окна отправки, не раньше `after`.

    Для непубличных `read`-действий окно не применяется: их никто не видит.
    """
    if not paced:
        return after
    local = after.astimezone(tz)
    for _ in range(14):  # максимум две недели вперёд — дальше что-то не так
        if local.weekday() in limits.send_weekdays:
            if local.hour < limits.send_window_start_hour:
                local = local.replace(
                    hour=limits.send_window_start_hour, minute=0, second=0,
                    microsecond=0,
                )
            if limits.send_window_start_hour <= local.hour < limits.send_window_end_hour:
                return local.astimezone(timezone.utc)
        local = (local + timedelta(days=1)).replace(
            hour=limits.send_window_start_hour, minute=0, second=0, microsecond=0
        )
    raise PlanError("не удалось найти окно отправки — проверьте limits.json")


def _selector_for(contact: dict, action: catalog.Action) -> dict:
    """Построить селектор цели из контакта под конкретное действие."""
    params: dict = {}
    username = contact.get("username")
    tg_id = contact.get("tg_id")

    if action.name in ("send_private_dm",):
        if username:
            params["username"] = username
        elif tg_id:
            params["target_user_tg_id"] = int(tg_id)
        else:
            raise PlanError("для личного сообщения нужен username или tg_id")
        return params

    if action.name in ("send_channel_dm", "check_channel_dm_metadata",
                       "resolve_channel_dm", "search_public_chat",
                       "source_finder_bot_send_text"):
        if not username:
            raise PlanError(f"{action.name} требует username")
        if action.name != "source_finder_bot_send_text":
            params["username"] = username
        return params

    if action.selector:
        if username:
            params["username"] = username
        elif tg_id:
            params["chat_id"] = int(tg_id)
            params["peer_kind"] = contact.get("peer_kind") or "channel"
        else:
            raise PlanError(f"{action.name} требует username или tg_id")
    return params


def plan(
    store: Store,
    campaign_id: str,
    *,
    limits: Limits,
    timezone_name: str = "Europe/Moscow",
    limit: int | None = None,
    actor: str = "cli",
    dry_run: bool = True,
) -> dict:
    """Собрать задачи для кампании.

    При `dry_run=True` ничего не пишется — возвращается тот же самый план,
    который был бы сохранён.
    """
    campaign = entities.get_campaign(store, campaign_id)
    if campaign is None:
        raise PlanError(f"нет кампании {campaign_id}")

    action = catalog.ACTIONS.get(campaign["action"])
    if action is None:
        raise PlanError(f"неизвестное действие {campaign['action']}")

    template = None
    if campaign["template_id"]:
        row = store.one(
            "SELECT * FROM templates WHERE id = ?", (campaign["template_id"],)
        )
        if row is None:
            raise PlanError(f"нет шаблона {campaign['template_id']}")
        template = dict(row)

    pool = accounts_mod.candidates(store, action.name)
    if not pool:
        raise PlanError(
            f"нет ни одного аккаунта, которому разрешено {action.name}. "
            f"Действие требует роль из {sorted(action.roles)}."
        )

    contacts = store.query(
        "SELECT c.* FROM contacts c "
        "WHERE c.opted_out = 0 AND c.segment = ? "
        "  AND NOT EXISTS (SELECT 1 FROM tasks t "
        "                  WHERE t.campaign_id = ? AND t.contact_id = c.id) "
        "ORDER BY c.created_at, c.id",
        (campaign["segment"], campaign_id),
    )
    if limit:
        contacts = contacts[: int(limit)]
    if not contacts:
        return {"planned": 0, "skipped": [], "tasks": [], "pool": len(pool)}

    tz = _tz(timezone_name)
    paced = action.visible
    per_account_cap = min(
        int(campaign["per_account_daily_cap"]) or 1,
        limits.per_account_daily_visible if paced else 10_000,
    )
    interval = timedelta(
        seconds=limits.per_account_visible_interval_sec if paced else 5
    )

    # Сколько каждый аккаунт уже отработал за сегодня — учитываем, чтобы
    # повторный plan не удваивал нагрузку.
    today = datetime.now(timezone.utc).date().isoformat()
    used: dict[int, int] = {}
    cursor: dict[int, datetime] = {}
    for row in store.query(
        "SELECT account_id, count(*) AS n, max(scheduled_at) AS last "
        "FROM tasks WHERE state IN ('planned','queued') "
        "  AND substr(scheduled_at, 1, 10) = ? GROUP BY account_id",
        (today,),
    ):
        used[int(row["account_id"])] = int(row["n"])
        if row["last"]:
            cursor[int(row["account_id"])] = datetime.fromisoformat(row["last"])

    start = datetime.now(timezone.utc) + timedelta(minutes=1)
    planned: list[dict] = []
    skipped: list[dict] = []
    daily_budget = int(campaign["daily_cap"])
    index = 0

    for contact in contacts:
        contact = dict(contact)
        if len(planned) >= daily_budget:
            skipped.append({"contact": contact["id"], "why": "исчерпан daily_cap"})
            continue

        # Выбираем наименее загруженный подходящий аккаунт.
        ranked = sorted(pool, key=lambda a: (used.get(a["id"], 0), a["id"]))
        account = next(
            (a for a in ranked if used.get(a["id"], 0) < per_account_cap), None
        )
        if account is None:
            skipped.append({
                "contact": contact["id"],
                "why": f"все аккаунты выбрали дневной лимит ({per_account_cap})",
            })
            continue

        try:
            params = dict(campaign["params"])
            params.update(_selector_for(contact, action))
            if template is not None:
                params["text"] = entities.render(template["body"], contact)
            catalog.validate(
                action.name, params, roles=account["roles"],
                allowed_actions=account["allowed_actions"] or None,
            )
        except (PlanError, ValueError, catalog.ValidationError) as exc:
            skipped.append({"contact": contact["id"], "why": str(exc)})
            continue

        base = max(start, cursor.get(account["id"], start))
        slot = next_slot(base, limits, tz, paced=paced)
        cursor[account["id"]] = slot + interval
        used[account["id"]] = used.get(account["id"], 0) + 1
        index += 1

        planned.append({
            "id": new_id("task"),
            "campaign_id": campaign_id,
            "contact_id": contact["id"],
            "contact": contact.get("username") or contact["id"],
            "account_id": account["id"],
            "account_label": account["label"],
            "action": action.name,
            "params": params,
            "mode": campaign["mode"],
            "scheduled_at": slot.isoformat(timespec="seconds"),
            "risk": action.risk,
        })

    if not dry_run and planned:
        expires_hours = int(campaign["ttl_hours"])
        for task in planned:
            scheduled = datetime.fromisoformat(task["scheduled_at"])
            expires = scheduled + timedelta(hours=expires_hours)
            store.execute(
                "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
                "action, params, mode, scheduled_at, expires_at, state, "
                "created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,'planned',?,?)",
                (task["id"], campaign_id, task["contact_id"], task["account_id"],
                 task["action"], dumps(task["params"]), task["mode"],
                 task["scheduled_at"], expires.isoformat(timespec="seconds"),
                 now(), now()),
            )
        store.log(actor, "plan", campaign_id,
                  f"planned={len(planned)} skipped={len(skipped)}")
        store.commit()

    return {
        "planned": len(planned),
        "skipped": skipped,
        "tasks": planned,
        "pool": len(pool),
        "dry_run": dry_run,
    }


def cancel(store: Store, task_ids: list[str], *, actor: str = "cli") -> int:
    """Снять запланированные задачи. Уже отправленные в Radar не трогаем."""
    changed = 0
    for task_id in task_ids:
        cursor = store.execute(
            "UPDATE tasks SET state='cancelled', updated_at=? "
            "WHERE id=? AND state='planned'",
            (now(), task_id),
        )
        changed += cursor.rowcount
    store.log(actor, "tasks.cancel", "", f"cancelled={changed}")
    store.commit()
    return changed
