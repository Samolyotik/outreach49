"""Планировщик: превращает кампанию в конкретные задачи с временем и аккаунтом.

Планирование ничего не отправляет и в Radar не ходит. Это чистая функция от
локального состояния, поэтому её можно гонять сколько угодно раз и смотреть,
что получится, до того как что-то реально уедет.
"""
from __future__ import annotations

import random
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


def _jitter(
    rng: random.Random, limits: Limits, paced: bool, reading: bool = False
) -> timedelta:
    """Случайная добавка к слоту — всегда вверх, никогда вниз.

    Вниз нельзя: диспетчер проверяет минимальную паузу на выпуске, и слот,
    провалившийся под неё, был бы заблокирован и переигран. Вверх — просто
    убирает ровную сетку 15:00 / 15:15 / 15:30.

    У чтения разброс свой: прежний контур задавал паузу диапазоном (240–360 с),
    а не одним числом, и ровный шаг сам по себе выглядит машиной.
    """
    if reading:
        span = int(getattr(limits, "read_per_account_interval_jitter_sec", 0) or 0)
    elif paced:
        span = int(getattr(limits, "per_account_visible_jitter_sec", 0) or 0)
    else:
        return timedelta(0)
    if span <= 0:
        return timedelta(0)
    return timedelta(seconds=rng.randint(0, span))


def _place(
    account_id: int,
    *,
    start: datetime,
    cursor: dict[int, datetime],
    used: dict[tuple[int, str], int],
    cap: int,
    limits: Limits,
    tz: ZoneInfo,
    paced: bool,
    rng: random.Random,
    reading: bool = False,
) -> tuple[datetime, str] | tuple[None, None]:
    """Найти ближайший слот аккаунта, не превышающий дневной лимит.

    Лимит считается по дню самого слота, а не по «сегодня»: план, собранный
    вечером, кладёт задачи на завтра, и вчерашний счётчик к ним отношения не
    имеет. Если день уже забит — переходим к следующему.
    """
    base = (max(start, cursor.get(account_id, start))
            + _jitter(rng, limits, paced, reading))
    slot = next_slot(base, limits, tz, paced=paced, reading=reading)
    for _ in range(30):  # месяц вперёд — дальше искать бессмысленно
        day = slot.date().isoformat()
        if used.get((account_id, day), 0) < cap:
            return slot, day
        tomorrow = (slot + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Разброс нужен и здесь: иначе все аккаунты, перенесённые на
        # следующий день, стартуют ровно в начало окна одной секундой.
        slot = next_slot(
            next_slot(tomorrow, limits, tz, paced=paced, reading=reading)
            + _jitter(rng, limits, paced, reading),
            limits, tz, paced=paced, reading=reading,
        )
    return None, None


def window_of(
    limits: Limits, *, paced: bool, reading: bool = False
) -> tuple[int, int, tuple[int, ...]] | None:
    """Окно класса: (начало, конец, дни недели). `None` — окна нет.

    У рассылки оно узкое — рабочие часы и дни. У разведки шире и без выходных
    (06:00–23:00, как в `work_calendar` прежнего контура): читателя никто не
    видит, но аккаунт, перебирающий имена в четыре утра, на человека не похож.
    """
    if paced:
        return (limits.send_window_start_hour, limits.send_window_end_hour,
                tuple(limits.send_weekdays))
    if reading:
        return (limits.read_window_start_hour, limits.read_window_end_hour,
                tuple(limits.read_weekdays))
    return None


def next_slot(
    after: datetime, limits: Limits, tz: ZoneInfo, *, paced: bool,
    reading: bool = False,
) -> datetime:
    """Ближайший момент внутри окна класса, не раньше `after`."""
    window = window_of(limits, paced=paced, reading=reading)
    if window is None:
        return after
    start, end, weekdays = window
    local = after.astimezone(tz)
    for _ in range(14):  # максимум две недели вперёд — дальше что-то не так
        if local.weekday() in weekdays:
            if local.hour < start:
                local = local.replace(
                    hour=start, minute=0, second=0, microsecond=0,
                )
            if start <= local.hour < end:
                return local.astimezone(timezone.utc)
        local = (local + timedelta(days=1)).replace(
            hour=start, minute=0, second=0, microsecond=0
        )
    raise PlanError("не удалось найти окно — проверьте limits.json")


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
    rng: random.Random | None = None,
) -> dict:
    """Собрать задачи для кампании.

    При `dry_run=True` ничего не пишется — возвращается тот же самый план,
    который был бы сохранён.

    ``rng`` нужен только тестам: с фиксированным зерном разброс слотов
    воспроизводится.
    """
    rng = rng or random.Random()
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

    # Кампания вправе сузить пул по роли. Допуск и уместность — разные вещи:
    # `check_channel_dm_metadata` по контракту разрешён и отправителям каналов,
    # но разведку каталога ведут читатели, иначе тридцать аккаунтов вместо
    # тринадцати расходуют на неё свой лимит resolve.
    wanted = set(campaign.get("roles") or ())
    if wanted:
        pool = [a for a in pool if a["roles"] & wanted]
        if not pool:
            raise PlanError(
                f"кампания сужена до ролей {sorted(wanted)}, но среди аккаунтов "
                f"с допуском на {action.name} таких нет"
            )

    # Видимое действие получает только тот, кому мы ещё не писали: `tasks`
    # хранит уникальность лишь внутри кампании, и второй сегмент, пересекающийся
    # с первым, иначе принесёт человеку второе «первое касание». Догоняющая
    # волна включает повтор явно — ключом allow_repeat_contacts в кампании.
    repeat_allowed = bool(
        not action.visible or campaign.get("allow_repeat_contacts")
    )
    sql = (
        "SELECT c.* FROM contacts c "
        "WHERE c.opted_out = 0 AND c.segment = ? "
        "  AND NOT EXISTS (SELECT 1 FROM tasks t "
        "                  WHERE t.campaign_id = ? AND t.contact_id = c.id) "
    )
    if not repeat_allowed:
        sql += (
            "  AND NOT EXISTS (SELECT 1 FROM contact_touches ct "
            "                  WHERE ct.contact_id = c.id) "
        )
    contacts = store.query(
        sql + "ORDER BY c.created_at, c.id", (campaign["segment"], campaign_id)
    )
    if limit:
        contacts = contacts[: int(limit)]
    if not contacts:
        return {"planned": 0, "skipped": [], "tasks": [], "pool": len(pool)}

    tz = _tz(timezone_name)
    paced = action.visible
    # Чтение метаданных не видно собеседнику, но упирается в лимит resolve на
    # стороне Telegram — свой потолок и свой шаг у него есть. Раскладываем по
    # тем же числам, что проверит диспетчер: план, который заведомо не пройдёт
    # преflight, — это не план, а очередь отказов.
    reading = action.name in catalog.READ_ACTIONS
    per_account_cap = min(
        int(campaign["per_account_daily_cap"]) or 1,
        limits.per_account_daily_visible if paced
        else limits.read_per_account_daily if reading
        else 10_000,
    )
    interval = timedelta(seconds=(
        limits.per_account_visible_interval_sec if paced
        else limits.read_per_account_interval_sec if reading
        else 5
    ))

    # Нагрузка учитывается по дню запланированного слота и по ВСЕМ незакрытым
    # состояниям. Считать «за сегодня» и только planned/queued нельзя: после
    # poll задачи уходят в done и исчезли бы из счётчика, а повторный plan
    # выдал бы аккаунту полный лимит заново, да ещё и на те же самые минуты.
    used: dict[tuple[int, str], int] = {}
    cursor: dict[int, datetime] = {}
    for row in store.query(
        "SELECT account_id, substr(scheduled_at, 1, 10) AS day, count(*) AS n "
        "FROM tasks WHERE state NOT IN ('cancelled', 'blocked') "
        "GROUP BY account_id, day"
    ):
        used[(int(row["account_id"]), str(row["day"]))] = int(row["n"])
    for row in store.query(
        "SELECT account_id, max(scheduled_at) AS last FROM tasks "
        "WHERE state NOT IN ('cancelled', 'blocked') GROUP BY account_id"
    ):
        if row["last"]:
            cursor[int(row["account_id"])] = (
                datetime.fromisoformat(row["last"]) + interval
            )

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

        # Берём аккаунт, который освободится раньше остальных и у которого в
        # этот день ещё осталось место.
        account: dict | None = None
        slot: datetime | None = None
        day: str | None = None
        for candidate in sorted(
            pool, key=lambda a: (cursor.get(a["id"], start), a["id"])
        ):
            slot, day = _place(
                candidate["id"], start=start, cursor=cursor, used=used,
                cap=per_account_cap, limits=limits, tz=tz, paced=paced, rng=rng,
                reading=reading,
            )
            if slot is not None:
                account = candidate
                break
        if account is None or slot is None or day is None:
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

        cursor[account["id"]] = slot + interval
        used[(account["id"], day)] = used.get((account["id"], day), 0) + 1
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
