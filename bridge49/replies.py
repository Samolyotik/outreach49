"""Ответ в существующий диалог.

Режим по умолчанию — ``immediate``. Это не «побыстрее», а единственный
работающий путь: ``lottery`` ждёт розыгрыша события ``outreach_command``,
которого нет ни у одного аккаунта, и такая команда остаётся в ``new``
навсегда. Прежний контур все свои отправки делал только ``immediate``.
Темп это не ослабляет — окно, дневной лимит и паузы проверяются у нас, до
обращения к Radar.

Рассылка и ответ — разные вещи, хотя обе доходят до Telegram одинаково.
Рассылка идёт по сегменту и планируется заранее; ответ адресуется одному
человеку, который уже написал нам и ждёт. Поэтому ответ не проходит через
кампанию с сегментом, а ставится точечно — но темп, окно и ARMED соблюдает
наравне со всем остальным.

Адресат берётся из самого входящего (`inbound_notification_id`), а не из
username: username — алиас, он меняется, а уведомление указывает ровно на тот
диалог, в котором нам написали.
"""
from __future__ import annotations

from typing import Any

from .store import Store, dumps, new_id, now

#: Действия, которыми мы отвечаем. Для них не действует защита от повторного
#: касания: она про первое касание, а ответ — продолжение начатого разговора.
REPLY_ACTIONS = frozenset({"reply_private_dm"})

#: Служебная кампания для ручных ответов. Задача не может существовать без
#: кампании, а заводить сегмент ради одного адресата бессмысленно.
REPLY_CAMPAIGN_ID = "manual_replies"
REPLY_CAMPAIGN_NAME = "Ручные ответы"


class ReplyError(RuntimeError):
    """Ответ поставить нельзя, и причина требует решения человека."""


#: Автоответы идут своей кампанией, а не вместе с ручными. Кампания — это
#: место, где живут дневные лимиты, и мешать их нельзя: наплыв входящих не
#: должен съедать бюджет рассылки, а рассылка — глушить ответы людям.
AUTO_CAMPAIGN_ID = "autoreplies"
AUTO_CAMPAIGN_NAME = "Автоответы"


def ensure_reply_campaign(
    store: Store,
    campaign_id: str = REPLY_CAMPAIGN_ID,
    name: str = REPLY_CAMPAIGN_NAME,
    note: str = "служебная: ручные ответы на входящие",
) -> str:
    """Создать служебную кампанию, если её ещё нет."""
    row = store.one("SELECT id FROM campaigns WHERE id = ?", (campaign_id,))
    if row is None:
        store.execute(
            "INSERT INTO campaigns(id, name, action, template_id, segment, mode, "
            "status, daily_cap, per_account_daily_cap, params, "
            "allow_repeat_contacts, ttl_hours, note, created_at, updated_at) "
            "VALUES(?,?,'reply_private_dm',NULL,'','lottery','active',"
            "999,99,'{}',1,48,?,?,?)",
            (campaign_id, name, note, now(), now()),
        )
    return campaign_id


def find_thread(
    store: Store, *, thread_id: str | None = None,
    account_id: int | None = None, peer: str | None = None,
) -> dict:
    """Найти диалог по id или по паре «аккаунт + собеседник»."""
    if thread_id:
        row = store.one("SELECT * FROM threads WHERE id = ?", (thread_id,))
        if row is None:
            raise ReplyError(f"нет диалога {thread_id}")
        return dict(row)
    if account_id is None or not peer:
        raise ReplyError("укажите диалог: id треда либо --account и --peer")
    key = peer.strip()
    if not key.startswith("@") and not key.startswith("id:"):
        key = f"@{key}"
    row = store.one(
        "SELECT * FROM threads WHERE account_id = ? AND lower(peer_key) = lower(?)",
        (int(account_id), key),
    )
    if row is None:
        raise ReplyError(f"нет диалога с {key} у аккаунта {account_id}")
    return dict(row)


def last_inbound(store: Store, thread: dict) -> dict:
    """Последнее входящее в диалоге — на него и отвечаем."""
    row = store.one(
        "SELECT * FROM inbound WHERE account_id = ? AND peer_key = ? "
        "ORDER BY id DESC LIMIT 1",
        (int(thread["account_id"]), thread["peer_key"]),
    )
    if row is None:
        raise ReplyError(
            "в этом диалоге нет входящих — отвечать не на что. "
            "Первое касание делается кампанией, а не ответом."
        )
    return dict(row)


def ensure_contact(store: Store, thread: dict, inbound: dict) -> str:
    """Вернуть контакт диалога, заведя его, если собеседник пришёл сам."""
    if thread.get("contact_id"):
        return str(thread["contact_id"])

    from . import entities

    username = inbound.get("peer_username")
    tg_id = inbound.get("peer_tg_id")
    if not username and not tg_id:
        raise ReplyError("у собеседника нет ни username, ни tg_id")
    contact = entities.add_contact(
        store,
        username=username,
        tg_id=int(tg_id) if tg_id else None,
        segment="inbound",
        display_name=username or (f"id:{tg_id}" if tg_id else None),
        note="заведён автоматически при ответе на входящее",
        actor="reply",
    )
    store.execute(
        "UPDATE threads SET contact_id = ?, updated_at = ? WHERE id = ?",
        (contact["id"], now(), thread["id"]),
    )
    return str(contact["id"])


#: Точечная отправка по адресату. Роли разные: monoforum канала пишет только
#: channel_sender, личное сообщение — только dm_sender. Это политика Radar, а
#: не наша: он проверит её заново перед исполнением.
SEND_ACTIONS = {"channel_dm": "send_channel_dm", "user": "send_private_dm"}

#: Служебная кампания для точечных отправок вне сегментов.
SEND_CAMPAIGN_ID = "manual_sends"
SEND_CAMPAIGN_NAME = "Точечные отправки"


def ensure_send_campaign(store: Store, action: str) -> str:
    """Служебная кампания под точечные отправки конкретным получателям."""
    campaign_id = f"{SEND_CAMPAIGN_ID}_{action}"
    if store.one("SELECT id FROM campaigns WHERE id = ?", (campaign_id,)) is None:
        store.execute(
            "INSERT INTO campaigns(id, name, action, template_id, segment, mode, "
            "status, daily_cap, per_account_daily_cap, params, "
            "allow_repeat_contacts, ttl_hours, note, created_at, updated_at) "
            "VALUES(?,?,?,NULL,'','lottery','active',999,99,'{}',1,48,?,?,?)",
            (campaign_id, f"{SEND_CAMPAIGN_NAME}: {action}", action,
             "служебная: точечные отправки", now(), now()),
        )
    return campaign_id


def queue_send(
    store: Store,
    *,
    account_id: int,
    text: str,
    username: str | None = None,
    tg_id: int | None = None,
    channel_tg_id: int | None = None,
    monoforum_tg_id: int | None = None,
    kind: str = "user",
    contact_id: str | None = None,
    mode: str = "immediate",
    idempotency: str | None = None,
    actor: str = "cli",
) -> dict[str, Any]:
    """Поставить одно сообщение конкретному получателю с конкретного аккаунта.

    Нужна там, где кампания не подходит: у каждого получателя свой текст —
    например персональная ссылка на тест. Разворачивать ради этого кампанию с
    сегментом на одного человека было бы притворством.
    """
    from . import entities

    message = (text or "").strip()
    if not message:
        raise ReplyError("пустой текст")
    if kind not in SEND_ACTIONS:
        raise ReplyError(f"неизвестный тип получателя: {kind}")
    action = SEND_ACTIONS[kind]
    if kind == "channel_dm" and not username:
        raise ReplyError("для monoforum канала нужен username канала")
    if kind == "user" and not (username or tg_id):
        raise ReplyError("нужен username или tg_id получателя")

    if contact_id is None:
        contact = entities.add_contact(
            store,
            username=username,
            tg_id=int(tg_id) if tg_id else None,
            kind="channel" if kind == "channel_dm" else "user",
            segment="manual_send",
            display_name=username or (f"id:{tg_id}" if tg_id else None),
            note="заведён при точечной отправке",
            actor=actor,
        )
        contact_id = str(contact["id"])

    campaign_id = ensure_send_campaign(store, action)

    # Один и тот же ключ не ставит вторую задачу: пакетную выдачу ссылок
    # можно перезапускать, не рискуя отправить человеку два сообщения.
    marker = idempotency or f"{account_id}:{username or tg_id}"
    if store.one(
        "SELECT id FROM tasks WHERE campaign_id = ? AND contact_id = ? "
        "AND state IN ('planned', 'queued', 'done')",
        (campaign_id, contact_id),
    ) is not None:
        raise ReplyError(f"этому получателю уже поставлена отправка ({marker})")

    params: dict[str, Any] = {"text": message}
    if username:
        params["username"] = str(username).lstrip("@")
    if kind == "user" and tg_id:
        params["target_user_tg_id"] = int(tg_id)
    if kind == "channel_dm":
        if channel_tg_id:
            params["target_channel_tg_id"] = int(channel_tg_id)
        if monoforum_tg_id:
            params["target_monoforum_tg_id"] = int(monoforum_tg_id)

    task_id = new_id("task")
    store.execute(
        "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
        "params, mode, scheduled_at, expires_at, state, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,NULL,'planned',?,?)",
        (task_id, campaign_id, contact_id, int(account_id), action,
         dumps(params), mode, now(), now(), now()),
    )
    store.log(actor, "send.queue", task_id, f"acc={account_id} {marker}")
    store.commit()
    return {
        "task": task_id, "account_id": int(account_id), "action": action,
        "peer": username or f"id:{tg_id}", "contact_id": contact_id, "mode": mode,
    }


def queue_reply(
    store: Store,
    *,
    text: str,
    thread_id: str | None = None,
    account_id: int | None = None,
    peer: str | None = None,
    mode: str = "immediate",
    actor: str = "cli",
    campaign_id: str | None = None,
    review_reason: str | None = None,
    scheduled_at: str | None = None,
) -> dict[str, Any]:
    """Поставить ответ в очередь. Ничего никуда не отправляет.

    Отправку по-прежнему делает только `dispatch`, и только при ARMED: ответ
    проходит ровно те же ворота темпа, что и всё остальное.

    ``campaign_id`` разводит ручные ответы и автоответы по разным кампаниям:
    лимиты живут на кампании, и смешивать их нельзя. ``review_reason`` метит
    ответ, который движок выдал неуверенно, — отправить его можно, но человек
    должен перечитать. ``scheduled_at`` откладывает выпуск: автоответ уходит
    не мгновенно, а спустя паузу на чтение.
    """
    message = (text or "").strip()
    if not message:
        raise ReplyError("пустой текст ответа")
    if mode not in ("lottery", "immediate"):
        raise ReplyError("mode должен быть lottery или immediate")

    thread = find_thread(
        store, thread_id=thread_id, account_id=account_id, peer=peer
    )
    inbound = last_inbound(store, thread)
    contact_id = ensure_contact(store, thread, inbound)
    campaign_id = ensure_reply_campaign(store) if campaign_id is None else campaign_id

    pending = store.one(
        "SELECT id FROM tasks WHERE campaign_id = ? AND contact_id = ? "
        "AND state IN ('planned', 'queued')",
        (campaign_id, contact_id),
    )
    if pending is not None:
        raise ReplyError(
            f"этому собеседнику уже поставлен ответ ({pending['id']}); "
            "дождитесь отправки или снимите задачу"
        )

    task_id = new_id("task")
    store.execute(
        "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
        "params, mode, scheduled_at, expires_at, state, review_reason, "
        "created_at, updated_at) "
        "VALUES(?,?,?,?,'reply_private_dm',?,?,?,NULL,'planned',?,?,?)",
        (task_id, campaign_id, contact_id, int(thread["account_id"]),
         dumps({
             "inbound_notification_id": int(inbound["id"]),
             "text": message,
         }),
         mode, scheduled_at or now(), review_reason or None, now(), now()),
    )
    store.log(actor, "reply.queue", task_id,
              f"acc={thread['account_id']} peer={thread['peer_key']}")
    store.commit()
    return {
        "task": task_id,
        "thread": thread["id"],
        "account_id": int(thread["account_id"]),
        "peer": thread["peer_key"],
        "inbound_id": int(inbound["id"]),
        "mode": mode,
        "review_reason": review_reason or "",
    }
