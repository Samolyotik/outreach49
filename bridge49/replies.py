"""Ответ в существующий диалог.

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


def ensure_reply_campaign(store: Store) -> str:
    """Создать служебную кампанию, если её ещё нет."""
    row = store.one("SELECT id FROM campaigns WHERE id = ?", (REPLY_CAMPAIGN_ID,))
    if row is None:
        store.execute(
            "INSERT INTO campaigns(id, name, action, template_id, segment, mode, "
            "status, daily_cap, per_account_daily_cap, params, "
            "allow_repeat_contacts, ttl_hours, note, created_at, updated_at) "
            "VALUES(?,?,'reply_private_dm',NULL,'','lottery','active',"
            "999,99,'{}',1,48,?,?,?)",
            (REPLY_CAMPAIGN_ID, REPLY_CAMPAIGN_NAME,
             "служебная: ручные ответы на входящие", now(), now()),
        )
    return REPLY_CAMPAIGN_ID


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


def queue_reply(
    store: Store,
    *,
    text: str,
    thread_id: str | None = None,
    account_id: int | None = None,
    peer: str | None = None,
    mode: str = "lottery",
    actor: str = "cli",
) -> dict[str, Any]:
    """Поставить ответ в очередь. Ничего никуда не отправляет.

    Отправку по-прежнему делает только `dispatch`, и только при ARMED: ответ
    проходит ровно те же ворота темпа, что и всё остальное.
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
    campaign_id = ensure_reply_campaign(store)

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
        "params, mode, scheduled_at, expires_at, state, created_at, updated_at) "
        "VALUES(?,?,?,?,'reply_private_dm',?,?,?,NULL,'planned',?,?)",
        (task_id, campaign_id, contact_id, int(thread["account_id"]),
         dumps({
             "inbound_notification_id": int(inbound["id"]),
             "text": message,
         }),
         mode, now(), now(), now()),
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
    }
