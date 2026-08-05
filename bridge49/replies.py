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

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .store import Store, dumps, new_id, now

#: Действия, которыми мы отвечаем. Для них не действует защита от повторного
#: касания: она про первое касание, а ответ — продолжение начатого разговора.
#:
#: Здесь стоят ЛОГИЧЕСКИЕ имена. `reply_channel_dm` уезжает в Radar как
#: `send_channel_dm` (см. `catalog.Action.wire`), но темп, дневной бюджет и
#: снятие защиты от повторного касания считаются по имени в задаче — поэтому
#: ответ в личку канала обязан отличаться от рассылки в неё же.
REPLY_ACTIONS = frozenset({"reply_private_dm", "reply_channel_dm"})

#: Статус заявки «ссылка выпущена, доставка ещё не подтверждена». Держим
#: строкой, а не импортом: `direct_invite` импортирует `replies`, и обратный
#: импорт замкнул бы круг.
INVITE_STATUS_CREATED = "invite_created_delivery_pending"

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


@dataclass(frozen=True)
class ReplyRoute:
    """Чем отвечать на входящее этой поверхности."""

    surface: str
    action: str
    role: str


#: Ответ выбирается по поверхности входящего, а не по одному действию на всё.
#:
#: `reply_private_dm` у Radar принимает ТОЛЬКО личку человека: он сверяет
#: `surface == private_dm` и `peer.type == user` и иначе отвечает
#: `invalid_inbound_reply_target`. Значит для личек каналов это действие не
#: подходит никогда, и задача с ним обречена ещё до выпуска. Ровно так 04.08
#: остался без ответа человек, написавший «Покажите» в monoforum
#: @armavir_auto23.
REPLY_ROUTES = {
    "private_dm": ReplyRoute("private_dm", "reply_private_dm", "dm_sender"),
    "channel_dm": ReplyRoute("channel_dm", "reply_channel_dm", "channel_sender"),
}


def reply_route(surface: Any) -> ReplyRoute:
    normalized = str(surface or "").strip()
    route = REPLY_ROUTES.get(normalized)
    if route is None:
        raise ReplyError(
            f"на поверхность {normalized or '<пусто>'} отвечать нечем"
        )
    return route


def channel_reply_params(inbound: dict) -> dict[str, Any]:
    """Куда именно писать ответ в личку канала.

    Проверки перенесены из прежнего контура (`bridge49_handoff_reply.py`,
    `_channel_target`) и намеренно въедливы: у `send_channel_dm` нет привязки
    к входящему, поэтому Radar не может, как в личке, вывести адресата из
    своего же факта и перепроверить нас. Здесь адресата задаём мы — и ошибка
    отправит сообщение в чужой канал.

    Поэтому берём цель только из фида Radar, сверяем её с тем, что сохранили у
    себя, и требуем следа прошлой команды: monoforum канала существует потому,
    что мы туда написали первыми. Без такого следа это не наш разговор.
    """
    raw = inbound.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except ValueError:
            raw = {}
    if not isinstance(raw, dict):
        raw = {}
    peer = raw.get("peer")
    message = raw.get("message")
    if (
        str(raw.get("schema") or "") != "tgr.outreach.inbound"
        or int(raw.get("version") or 0) != 1
        or str(raw.get("surface") or "") != "channel_dm"
        or not isinstance(peer, dict)
        or str(peer.get("type") or "") != "channel_dm"
        or not isinstance(message, dict)
    ):
        raise ReplyError("у входящего нет достоверного происхождения от Radar")

    username = str(peer.get("username") or "").strip().lstrip("@").casefold()
    stored_username = str(
        inbound.get("peer_username") or ""
    ).strip().lstrip("@").casefold()
    channel_tg_id = peer.get("channel_tg_id")
    monoforum_tg_id = peer.get("monoforum_tg_id")
    if (
        not username
        or username != stored_username
        or not channel_tg_id
        or not monoforum_tg_id
        or int(peer.get("tg_id") or 0) != int(monoforum_tg_id)
        or int(inbound.get("peer_tg_id") or 0) != int(monoforum_tg_id)
    ):
        raise ReplyError("цель входящего неполна или расходится с сохранённой")

    raw_account_id = raw.get("account_id")
    if raw_account_id is not None and int(raw_account_id) != int(
        inbound.get("account_id") or 0
    ):
        raise ReplyError("аккаунт входящего не совпадает с сохранённым")

    correlation = raw.get("correlation")
    if not isinstance(correlation, dict) or not correlation.get("last_command_id"):
        raise ReplyError("нет следа прошлой команды: это не наш разговор")

    return {
        "username": username,
        "target_channel_tg_id": peer_id(channel_tg_id, "target_channel_tg_id"),
        "target_monoforum_tg_id": peer_id(
            monoforum_tg_id, "target_monoforum_tg_id"),
    }

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


#: Размеченные идентификаторы Telegram, которые нам встречаются: канал идёт с
#: префиксом «-100», его monoforum — с «-207».
MARKED_PEER_PREFIXES = ("-100", "-207")
MARKED_PEER_OFFSET = 10 ** 12


def peer_id(value: Any, field: str) -> int:
    """Привести идентификатор канала к тому виду, которого ждёт Radar.

    Radar передаёт значение прямо в ``PeerChannel`` и потому требует голый
    положительный id. Telethon отдаёт размеченный, со знаком, и отправить
    такой напрямую нельзя: команда отклоняется с «must be a positive 64-bit
    integer». Именно это и случилось 03.08 на выдаче ссылок.

    Разметка снимается вычитанием триллиона, и это работает для обоих
    префиксов сразу::

        канал     -1001763001372 → 1763001372
        monoforum -2071763001372 → 1071763001372

    Связь между парой видна и в живых данных: monoforum — это тот же канал
    плюс 1 070 000 000 000. Отрезать префикс как строку заманчиво, но неверно:
    на monoforum это дало бы 1763001372 вместо 1071763001372, то есть номер
    самого канала вместо номера его monoforum.

    Отрицательное значение с чужим префиксом — не канал, а обычная группа.
    Молча превращать одно в другое нельзя, поэтому отказываемся вслух.
    """
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ReplyError(f"{field}: не число ({value!r})") from exc
    if number > 0:
        return number
    if not str(number).startswith(MARKED_PEER_PREFIXES):
        raise ReplyError(
            f"{field}: {number} не похож на канал — размеченный id канала "
            f"начинается с {' или '.join(MARKED_PEER_PREFIXES)}, "
            "а это, скорее всего, обычная группа"
        )
    normalized = -number - MARKED_PEER_OFFSET
    if normalized <= 0:
        raise ReplyError(f"{field}: после снятия разметки вышло {normalized}")
    return normalized


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
            params["target_channel_tg_id"] = peer_id(
                channel_tg_id, "target_channel_tg_id")
        if monoforum_tg_id:
            params["target_monoforum_tg_id"] = peer_id(
                monoforum_tg_id, "target_monoforum_tg_id")

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


def supersede_pending_reply(store: Store, pending: Any, *,
                            actor: str = "autoreply") -> bool:
    """Снять ещё не ушедший ответ, чтобы поставить вместо него свежий.

    Снимаем только то, чего наша сторона ещё не касалась: задача `planned` и
    без `request_id`/`command_id`. Как только команда ушла в Radar, отменять
    нечего — сообщение уже в пути, и вторая попытка дала бы дубль. Приём и
    ровно это различение перенесены из прежнего контура
    (`bridge49_handoff_reply.py`, `supersede`).

    Условие перепроверяется в самом UPDATE, а не только чтением: между
    проверкой и записью диспетчер мог забрать задачу. Ноль изменённых строк —
    значит забрал, и заменять уже нельзя.

    Письмо со ссылкой не трогаем никогда: это не черновик, а то, ради чего
    человек и писал. Пусть уходит, а на новое сообщение ответим следующим.
    """
    if str(pending["state"]) != "planned":
        return False
    if pending["request_id"] or pending["command_id"]:
        return False
    # Второе условие — не дубль первого. Ссылка помечается выпущенной ДО
    # постановки письма, и между этими двумя коммитами `task_id` ещё пуст:
    # проверка по задаче в этот момент письма не узнаёт.
    carries_invite = store.one(
        "SELECT 1 FROM direct_invites "
        " WHERE task_id = ? OR (contact_id = ? AND status = ? AND task_id IS NULL)",
        (pending["id"], pending["contact_id"], INVITE_STATUS_CREATED),
    )
    if carries_invite is not None:
        return False
    cursor = store.execute(
        "UPDATE tasks SET state = 'cancelled', updated_at = ? "
        " WHERE id = ? AND state = 'planned' "
        "   AND request_id IS NULL AND command_id IS NULL",
        (now(), pending["id"]),
    )
    if not cursor.rowcount:
        return False
    store.log(actor, "reply.superseded", str(pending["id"]),
              "собеседник дописал — ответ пересобран")
    store.commit()
    return True


def _inbound_by_id(store: Store, thread: Any, inbound_id: int) -> dict:
    """То самое входящее, по которому собран ответ.

    Если его вдруг нет в этом треде — это не повод молча ответить на чужое:
    возвращаемся к последнему, как раньше, но такой ответ хотя бы не уедет
    реплаем в другой разговор.
    """
    row = store.one(
        "SELECT * FROM inbound WHERE id = ? AND account_id = ? AND peer_key = ?",
        (int(inbound_id), int(thread["account_id"]), thread["peer_key"]),
    )
    return dict(row) if row is not None else last_inbound(store, thread)


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
    supersede: bool = False,
    inbound_id: int | None = None,
) -> dict[str, Any]:
    """Поставить ответ в очередь. Ничего никуда не отправляет.

    Отправку по-прежнему делает только `dispatch`, и только при ARMED: ответ
    проходит ровно те же ворота темпа, что и всё остальное.

    ``campaign_id`` разводит ручные ответы и автоответы по разным кампаниям:
    лимиты живут на кампании, и смешивать их нельзя. ``review_reason`` метит
    ответ, который движок выдал неуверенно, — отправить его можно, но человек
    должен перечитать. ``scheduled_at`` откладывает выпуск: автоответ уходит
    не мгновенно, а спустя паузу на чтение.

    ``supersede`` разрешает заменить ещё не ушедший ответ этому же собеседнику.
    Нужно для автоответов: человек дописывает мысль вторым и третьим
    сообщением, а наш ответ на первое лежит и ждёт паузы на чтение. Отказ в
    такой ситуации — не защита, а ложная тревога: 04.08 он трижды завёл
    менеджеру карточку о несуществующем сбое и потерял уточнение собеседника.
    У ручных ответов замены нет: там отказ информирует человека, а не машину.
    """
    message = (text or "").strip()
    if not message:
        raise ReplyError("пустой текст ответа")
    if mode not in ("lottery", "immediate"):
        raise ReplyError("mode должен быть lottery или immediate")

    thread = find_thread(
        store, thread_id=thread_id, account_id=account_id, peer=peer
    )
    # По умолчанию отвечаем на последнее входящее треда — так работает ручной
    # ответ, где собеседник и есть «последний написавший». Но автоответ строит
    # текст по конкретному сообщению, и пока он думал, могло прийти следующее:
    # тогда Radar доставлял реплай не на то, на что мы отвечали. Живой случай
    # 05.08 — ответ на 76797 уехал реплаем на 76799.
    inbound = (_inbound_by_id(store, thread, inbound_id)
               if inbound_id is not None else last_inbound(store, thread))
    contact_id = ensure_contact(store, thread, inbound)
    campaign_id = ensure_reply_campaign(store) if campaign_id is None else campaign_id

    pending = store.one(
        "SELECT id, state, request_id, command_id, contact_id FROM tasks "
        " WHERE campaign_id = ? AND contact_id = ? "
        "   AND state IN ('planned', 'queued') ORDER BY created_at DESC, id DESC",
        (campaign_id, contact_id),
    )
    if pending is not None and not (
        supersede and supersede_pending_reply(store, pending, actor=actor)
    ):
        raise ReplyError(
            f"этому собеседнику уже поставлен ответ ({pending['id']}); "
            "дождитесь отправки или снимите задачу"
        )

    # Чем отвечать — решает поверхность входящего, а не одно действие на всё.
    route = reply_route(inbound.get("surface"))
    if route.surface == "channel_dm":
        params = channel_reply_params(inbound)
        params["text"] = message
    else:
        params = {
            "inbound_notification_id": int(inbound["id"]),
            "text": message,
        }

    task_id = new_id("task")
    try:
        store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, expires_at, state, review_reason, "
            "created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,NULL,'planned',?,?,?)",
            (task_id, campaign_id, contact_id, int(thread["account_id"]),
             route.action, dumps(params),
             mode, scheduled_at or now(), review_reason or None, now(), now()),
        )
    except sqlite3.IntegrityError as exc:
        # Проверка выше неатомарна: между «посмотрел» и «вставил» другой
        # процесс мог поставить свой ответ. Пока в кампанию пишет один
        # oneshot-юнит, попасть сюда неоткуда; когда разбор входящих станет
        # постоянным процессом с параллельностью — станет откуда.
        #
        # Проигранная гонка обязана читаться так же, как замеченный дубль:
        # вызывающему нужно знать, что ответ уже поставлен, а не разбирать
        # ошибку базы. Иначе автоответ заведёт менеджеру карточку о
        # несуществующем сбое — ровно как это было 04.08 с ложным отказом.
        store.conn.rollback()
        raise ReplyError(
            "этому собеседнику уже поставлен ответ (гонка при постановке); "
            "дождитесь отправки или снимите задачу"
        ) from exc
    store.log(actor, "reply.queue", task_id,
              f"acc={thread['account_id']} peer={thread['peer_key']} "
              f"{route.action}")
    store.commit()
    return {
        "task": task_id,
        "thread": thread["id"],
        "account_id": int(thread["account_id"]),
        "peer": thread["peer_key"],
        "inbound_id": int(inbound["id"]),
        "action": route.action,
        "surface": route.surface,
        "mode": mode,
        "review_reason": review_reason or "",
    }
