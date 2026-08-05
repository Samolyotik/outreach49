"""Зеркало переписки в рабочую группу.

Radar показывает в бизнес-форуме только Channel DM: зеркало создаётся для
разговоров, а сообщение в публичный чат разговором не является, и личку он не
зеркалит вовсе (40 зеркал за всю историю, все одного вида). Значит половина
работы флота не видна никому, кроме базы.

Чинить это в Radar нельзя без выкатки и рестарта воркеров. Поэтому зеркалим
сами: bridge49 знает и что отправил, и что получил, а до Telegram дотягивается
обычным ботом. Никакого Radar в этом пути нет — ни рестартов, ни hot-reload.

Что уходит в группы:

* исходящие — в чаты, в личку и в личку каналов, с текстом и адресатом;
* входящие — ответы людей;
* карточки менеджеру, когда машина передаёт разговор человеку.

## Две группы, а не одна

Сначала всё шло в одну группу — и она перестала читаться. За двое суток туда
уехало 229 сообщений, из них 153 переписки и 55 карточек: заявка, ради которой
группа и заводилась, тонула среди зеркала разговоров. Поэтому потоки разведены:

* `OUTREACH_MANAGER_TELEGRAM_CHAT_ID` — только карточки менеджеру. Всё, что
  там появилось, требует действия человека;
* `OUTREACH_DIALOG_TELEGRAM_CHAT_ID` — вся переписка. Читают, когда нужен
  контекст, а не по каждому событию.

Если вторая группа не задана, поведение прежнее: всё в одну. Ветки у групп
свои — номер ветки одной группы в другой не значит ничего, поэтому у контакта
две колонки.

Курсор хранится у нас: каждое событие уезжает ровно один раз. Повторный проход
после обрыва ничего не задваивает, потому что курсор двигается только после
успешной отправки.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping, Sequence

from .store import Store, now

BOT_TOKEN_ENV = "OUTREACH_MANAGER_TELEGRAM_BOT_TOKEN"
CHAT_ID_ENV = "OUTREACH_MANAGER_TELEGRAM_CHAT_ID"
DIALOG_CHAT_ID_ENV = "OUTREACH_DIALOG_TELEGRAM_CHAT_ID"
THREAD_ID_ENV = "OUTREACH_MANAGER_TELEGRAM_THREAD_ID"
TOPIC_PREFIX_ENV = "OUTREACH_MANAGER_TELEGRAM_TOPIC_PREFIX"
ENABLED_ENV = "OUTREACH_MANAGER_TELEGRAM_ENABLED"

#: Где чей поток и в какой колонке лежит ветка собеседника.
MANAGER = ("manager", CHAT_ID_ENV, "forum_thread_id")
DIALOG = ("dialog", DIALOG_CHAT_ID_ENV, "dialog_thread_id")

#: Докуда уже отзеркалили. Раздельно по видам: отправки и входящие живут в
#: разных таблицах с разными ключами, и общий курсор их бы перепутал.
#:
#: Отправки считаются по `finished_at`, а не по `dispatched_at`: у выпущенной
#: задачи исхода ещё нет, и курсор по выпуску показывал бы её как неудачу —
#: см. `SENT_SQL`. Ключ поэтому новый: старый хранил момент выпуска, и читать
#: его как момент завершения нельзя.
CURSOR_SENT = "forum_cursor_done"
CURSOR_INBOUND = "forum_cursor_inbound"
CURSOR_HANDOFF = "forum_cursor_handoff"

#: Прежний курсор по выпуску. Остаётся ради переноса значения при обновлении.
CURSOR_SENT_LEGACY = "forum_cursor_sent"

#: Как называть поверхности по-человечески.
SURFACE = {
    "send_private_dm": "личка",
    "send_channel_dm": "личка канала",
    "send_public_chat_message": "публичный чат",
    "reply_private_dm": "ответ в личке",
    "reply_channel_dm": "ответ в личке канала",
}


#: Общая лента форума. Писать в неё запрещено: см. `send`.
GENERAL_THREAD_ID = 1

#: Radar потерял связь после выпуска команды: отправлено или нет — неизвестно.
OUTCOME_UNKNOWN = "outcome_unknown"

#: Зеркалим только завершённые отправки. Пока задача в полёте (`queued`), исход
#: пуст, и любая карточка о ней — догадка. Курсор при этом двигается вперёд, то
#: есть догадку уже не исправить: следующий проход её не увидит. Так ответ
#: @webdevfound 04.08 попал в группу как «НЕ ДОШЛО», хотя Radar доставил его
#: через 4 минуты — он ждал своей очереди в поаккаунтном темпе (5–6.75 мин
#: между видимыми действиями). Условие на `finished_at` закрывает и гонку, и
#: невозможность исправления: строка попадает в зеркало один раз и уже с
#: настоящим исходом.
SENT_SQL = (
    "SELECT t.id, t.account_id, t.action, t.params, t.campaign_id, "
    "       t.state, t.outcome, t.finished_at, c.username, c.tg_id, "
    "       t.contact_id "
    "  FROM tasks t LEFT JOIN contacts c ON c.id = t.contact_id "
    " WHERE t.finished_at IS NOT NULL AND t.finished_at > ? "
    "   AND t.action IN ('send_private_dm', 'send_channel_dm', "
    "                    'send_public_chat_message', 'reply_private_dm', "
    "                    'reply_channel_dm') "
    " ORDER BY t.finished_at LIMIT ?"
)


class ForumError(RuntimeError):
    """Зеркало не работает, и это стоит увидеть."""


class NoTopic(ForumError):
    """У собеседника ещё нет ветки — событие пропускаем, а не теряем связь.

    Только про «человек пока не отвечал». Отказ Telegram завести ветку — это
    не то же самое: там событие ещё можно доставить позже, и подменять одно
    другим значит терять переписку молча (см. `ensure_topic`).
    """


def destination(kind: tuple[str, str, str]) -> tuple[str, str]:
    """Куда писать поток и в какой колонке искать его ветку.

    Группа диалогов необязательна: пока её нет, всё идёт по-старому в группу
    менеджера и в её же ветки. Так включение сводится к одной переменной, а
    выключение — к её удалению.
    """
    _, chat_env, column = kind
    chat = os.environ.get(chat_env, "").strip()
    if chat:
        return chat, column
    return os.environ.get(CHAT_ID_ENV, "").strip(), MANAGER[2]


def enabled() -> bool:
    return (os.environ.get(ENABLED_ENV, "").strip() == "1"
            and bool(os.environ.get(BOT_TOKEN_ENV, "").strip())
            and bool(os.environ.get(CHAT_ID_ENV, "").strip()))


def _call(method: str, payload: Mapping[str, Any], *, timeout: int = 20) -> dict:
    token = os.environ.get(BOT_TOKEN_ENV, "").strip()
    if not token:
        raise ForumError("нет токена бота")
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ForumError(f"HTTP {exc.code}: {detail[:200]}") from exc
    except urllib.error.URLError as exc:
        raise ForumError(f"сеть недоступна: {exc}") from exc
    if not parsed.get("ok"):
        raise ForumError(str(parsed.get("description") or parsed)[:200])
    return parsed.get("result") or {}


def send(text: str, *, thread_id: int | None = None,
         chat_id: str | None = None) -> dict:
    """Одно сообщение в ветку собеседника.

    Без ветки не отправляем ничего. General — это лента, в которой переписка
    с разными людьми смешивается в один поток; читать её нельзя, а засорять
    рабочую группу тем, что никто не читает, тем более незачем.
    """
    thread = thread_id
    if thread is None:
        raw = os.environ.get(THREAD_ID_ENV, "").strip()
        thread = int(raw) if raw else None
    if not thread or int(thread) == GENERAL_THREAD_ID:
        raise NoTopic("нет ветки собеседника")
    return _call("sendMessage", {
        "chat_id": chat_id or os.environ.get(CHAT_ID_ENV, "").strip(),
        "text": text[:4000],
        "disable_web_page_preview": True,
        "message_thread_id": int(thread),
    })


def remember(store: Store, result: Mapping[str, Any], *, chat_id: str,
             thread_id: int | None, kind: str, ref: str,
             contact_id: str | None) -> None:
    """Запомнить, что и куда мы отправили.

    Без этого убрать из группы лишнее нечем: Telegram не даёт боту читать
    историю, и своё сообщение приходится опознавать пересылкой.
    """
    message_id = int((result or {}).get("message_id") or 0)
    if not message_id:
        return
    store.execute(
        "INSERT OR IGNORE INTO forum_posts(chat_id, message_id, thread_id, "
        "  kind, ref, contact_id, created_at) VALUES(?,?,?,?,?,?,?)",
        (str(chat_id), message_id, thread_id, kind, str(ref),
         contact_id, now()))


def topic_name(row: Mapping[str, Any]) -> str:
    """Как назвать ветку. Имя должно опознаваться с одного взгляда."""
    prefix = os.environ.get(TOPIC_PREFIX_ENV, "").strip()
    who = ""
    if row.get("username"):
        who = "@" + str(row["username"])
    elif row.get("display_name"):
        who = str(row["display_name"])
    elif row.get("tg_id"):
        who = "id:%s" % row["tg_id"]
    else:
        who = str(row.get("contact_id") or "?")
    name = ("%s %s" % (prefix, who)).strip() if prefix else who
    return name[:128]


def ensure_topic(store: Store, contact_id: str | None, *,
                 chat_id: str, column: str = "forum_thread_id") -> int | None:
    """Ветка собеседника в этой группе. Заводится один раз и запоминается.

    Ветка появляется только после того, как человек ответил. Заводить её на
    каждое исходящее значит превращать группу в список рассылки: за сутки мы
    пишем десяткам, отвечают единицы, и настоящие разговоры утонули бы среди
    пустых веток.

    Пока ответа нет, возвращаем None: событие пропускается как `NoTopic`, и
    курсор идёт дальше — ждать ответа, который может не прийти никогда, эта
    очередь не должна.

    А вот отказ Telegram завести ветку — совсем другое дело, и молча его
    глотать нельзя. Раньше здесь возвращался None, `send` поднимал `NoTopic`,
    курсор двигался — и переписка исчезала. Именно так она и исчезла бы при
    переезде в группу диалогов, где боту ещё не выдали право на темы.
    Поэтому теперь наверх уходит `ForumError`: поток встаёт, курсор держится,
    следующий проход повторит.
    """
    if not contact_id:
        return None
    row = store.one(
        f"SELECT id, username, display_name, tg_id, {column} AS thread "
        "FROM contacts WHERE id = ?", (contact_id,))
    if row is None:
        return None
    if row["thread"]:
        return int(row["thread"])
    if store.one("SELECT 1 FROM inbound WHERE contact_id = ? LIMIT 1",
                 (contact_id,)) is None:
        return None
    try:
        created = _call("createForumTopic", {
            "chat_id": chat_id,
            "name": topic_name(dict(row)),
        })
    except ForumError as exc:
        store.log("forum", "forum.topic_failed", str(contact_id), str(exc)[:200])
        store.commit()
        raise
    thread_id = int(created.get("message_thread_id") or 0) or None
    if thread_id:
        store.execute(f"UPDATE contacts SET {column} = ? WHERE id = ?",
                      (thread_id, contact_id))
        store.commit()
    return thread_id


#: Сколько последних реплик показывать в карточке и сколько знаков на реплику.
#:
#: Десять — граница, за которой карточка перестаёт читаться с одного экрана, а
#: пользы не прибавляет: решение менеджер принимает по концу разговора, а не по
#: его началу. Длинные реплики режутся, потому что в переписке встречаются
#: простыни на тысячу знаков, и одна такая вытеснила бы весь остальной обмен.
HISTORY_LINES = 10
HISTORY_CHARS = 220


def conversation(store: Store, contact_id: str | None, *,
                 limit: int = HISTORY_LINES) -> list[dict]:
    """Последние реплики разговора: что мы написали и что ответили.

    Собирается из двух мест, потому что живёт в двух: наши отправки — в
    `tasks`, чужие ответы — в `inbound`. Общего журнала переписки у нас нет,
    и заводить его ради показа не стоит.
    """
    if not contact_id:
        return []
    rows = list(store.query(
        "SELECT COALESCE(t.finished_at, t.dispatched_at) AS at, 'мы' AS side, "
        "       t.params AS body FROM tasks t "
        " WHERE t.contact_id = ? AND t.state = 'done' "
        "   AND t.action IN ('send_private_dm', 'send_channel_dm', "
        "                    'send_public_chat_message', 'reply_private_dm', "
        "                    'reply_channel_dm') "
        " UNION ALL "
        "SELECT i.created_at AS at, 'он' AS side, i.text AS body FROM inbound i "
        " WHERE i.contact_id = ? "
        " ORDER BY at DESC LIMIT ?",
        (contact_id, contact_id, int(limit))))
    out = []
    for row in reversed(rows):
        text = str(row["body"] or "")
        if row["side"] == "мы":
            try:
                text = str(json.loads(text or "{}").get("text") or "")
            except (TypeError, ValueError):
                text = ""
        text = " ".join(text.split())
        if not text:
            continue
        out.append({"когда": str(row["at"] or "")[:16].replace("T", " "),
                    "кто": row["side"], "текст": text[:HISTORY_CHARS]})
    return out


def handoff_card(row: Mapping[str, Any],
                 history: Sequence[Mapping[str, Any]] | None = None) -> str:
    """Карточка менеджеру: машина передаёт разговор человеку.

    Разговор идёт прямо в карточку, а не отдельными сообщениями рядом. Причина
    простая: 05.08 переписку из группы заявок убрали, чтобы та читалась, и
    карточки остались висеть без всякого контекста — «назвал сферу», а какую и
    в ответ на что, поди угадай. Мирить это разнесением по двум группам
    бессмысленно: человеку, который берёт заявку в работу, нужен разговор
    здесь и сейчас, а не в соседней группе.
    """
    lines = [
        "НУЖЕН ЧЕЛОВЕК",
        "причина: %s" % (row["reason"] or "не указана"),
        "кому: %s" % (("@" + str(row["username"])) if row["username"]
                      else str(row["peer_key"] or row["contact_id"] or "?")),
        "аккаунт: %s" % row["account_id"],
        "",
        str(row["note"] or "")[:2000],
    ]
    if history:
        lines += ["", "── о чём говорили ──"]
        lines += ["%s %s: %s" % (item["когда"], item["кто"], item["текст"])
                  for item in history]
    return "\n".join(filter(None, lines))


def _peer(row: Mapping[str, Any]) -> str:
    if row["username"]:
        return "@" + str(row["username"])
    if row["tg_id"]:
        return "id:%s" % row["tg_id"]
    return str(row["contact_id"] or "?")


def outgoing_card(row: Mapping[str, Any]) -> str:
    surface = SURFACE.get(str(row["action"]), str(row["action"]))
    text = ""
    try:
        text = str(json.loads(row["params"] or "{}").get("text") or "")
    except (TypeError, ValueError):
        pass
    outcome = str(row["outcome"] or "")
    if outcome == "succeeded":
        head = "МЫ НАПИСАЛИ · %s" % surface
    elif outcome == OUTCOME_UNKNOWN:
        # Связь с Radar оборвалась после отправки команды. Отправлено или нет —
        # неизвестно, и оба однозначных заголовка тут врут: «не дошло» позовёт
        # человека писать повторно, «мы написали» скроет пропажу.
        head = "НЕЯСНО, ДОШЛО ЛИ · %s" % surface
    else:
        head = "НЕ ДОШЛО · %s (%s)" % (surface, outcome or row["state"])
    return "\n".join(filter(None, [
        head,
        "кому: %s" % _peer(row),
        "аккаунт: %s" % row["account_id"],
        "кампания: %s" % row["campaign_id"],
        "",
        text[:2500],
    ]))


def inbound_card(row: Mapping[str, Any]) -> str:
    return "\n".join(filter(None, [
        "НАМ ОТВЕТИЛИ · %s" % (row["surface"] or "личка"),
        "от: %s" % (("@" + str(row["peer_username"])) if row["peer_username"]
                    else str(row["peer_key"])),
        "аккаунт: %s" % row["account_id"],
        "",
        str(row["text"] or "")[:2500],
    ]))


def adopt_sent_cursor(store: Store) -> str:
    """Курсор отправок, с одноразовым переносом со старого ключа.

    Старый курсор хранил момент выпуска, новый — момент завершения. Подставить
    одно вместо другого нельзя: у уже отзеркалённой строки завершение позже
    выпуска, и она уехала бы в группу второй раз. Поэтому берём самое позднее
    завершение среди строк, выпущенных до старого курсора: всё отзеркалённое
    оказывается позади, а то, что было в полёте в момент обновления, ещё
    впереди и уедет с настоящим исходом.
    """
    current = str(store.get_state(CURSOR_SENT, "") or "")
    if current:
        return current
    legacy = str(store.get_state(CURSOR_SENT_LEGACY, "") or "")
    if not legacy:
        return ""
    row = store.one(
        "SELECT MAX(finished_at) AS edge FROM tasks "
        " WHERE dispatched_at IS NOT NULL AND dispatched_at <= ? "
        "   AND finished_at IS NOT NULL",
        (legacy,),
    )
    adopted = str((row["edge"] if row else None) or legacy)
    store.set_state(CURSOR_SENT, adopted)
    store.commit()
    return adopted


def run(store: Store, *, limit: int = 30, actor: str = "forum") -> dict:
    """Отзеркалить новые события. Курсор двигается только после отправки."""
    if not enabled():
        return {"состояние": "выключено", "отправлено": 0}

    posted = failed = skipped = 0
    dialog_chat, dialog_column = destination(DIALOG)
    manager_chat, manager_column = destination(MANAGER)

    def deliver(card: str, *, chat: str, column: str, kind: str, ref: str,
                contact_id: str | None) -> None:
        thread = ensure_topic(store, contact_id, chat_id=chat, column=column)
        result = send(card, thread_id=thread, chat_id=chat)
        remember(store, result, chat_id=chat, thread_id=thread, kind=kind,
                 ref=ref, contact_id=contact_id)

    # -- исходящие -----------------------------------------------------------
    cursor = adopt_sent_cursor(store)
    rows = store.query(SENT_SQL, (cursor, int(limit)))
    for row in rows:
        try:
            deliver(outgoing_card(dict(row)), chat=dialog_chat,
                    column=dialog_column, kind="outgoing",
                    ref=str(row["id"]), contact_id=row["contact_id"])
            posted += 1
        except NoTopic:
            # Человек ещё не отвечал. Курсор всё равно двигаем: иначе одно
            # такое событие встало бы в голове очереди навсегда.
            skipped += 1
        except ForumError as exc:
            failed += 1
            store.log(actor, "forum.failed", str(row["id"]), str(exc)[:200])
            store.commit()
            break
        store.set_state(CURSOR_SENT, str(row["finished_at"]))
        store.commit()

    # -- входящие ------------------------------------------------------------
    inbound_cursor = int(store.get_state(CURSOR_INBOUND, "0") or 0)
    rows = store.query(
        "SELECT id, account_id, surface, peer_key, peer_username, text, "
        "       contact_id FROM inbound WHERE id > ? ORDER BY id LIMIT ?",
        (inbound_cursor, int(limit)),
    )
    for row in rows:
        try:
            deliver(inbound_card(dict(row)), chat=dialog_chat,
                    column=dialog_column, kind="inbound",
                    ref=str(row["id"]), contact_id=row["contact_id"])
            posted += 1
        except NoTopic:
            skipped += 1
        except ForumError as exc:
            failed += 1
            store.log(actor, "forum.failed", str(row["id"]), str(exc)[:200])
            store.commit()
            break
        store.set_state(CURSOR_INBOUND, str(row["id"]))
        store.commit()

    # -- карточки менеджеру ---------------------------------------------------
    #
    # Единственный поток группы заявок: это события, где от человека ждут
    # действия. Пока рядом ехало зеркало переписки, они в нём терялись.
    handoff_cursor = str(store.get_state(CURSOR_HANDOFF, "") or "")
    rows = store.query(
        "SELECT h.id, h.reason, h.note, h.created_at, t.account_id, "
        "       t.peer_key, t.contact_id, c.username "
        "  FROM handoffs h JOIN threads t ON t.id = h.thread_id "
        "  LEFT JOIN contacts c ON c.id = t.contact_id "
        " WHERE h.created_at > ? AND h.status = 'new' "
        " ORDER BY h.created_at LIMIT ?",
        (handoff_cursor, int(limit)),
    )
    for row in rows:
        try:
            deliver(handoff_card(dict(row),
                                 conversation(store, row["contact_id"])),
                    chat=manager_chat,
                    column=manager_column, kind="handoff",
                    ref=str(row["id"]), contact_id=row["contact_id"])
            posted += 1
        except NoTopic:
            skipped += 1
        except ForumError as exc:
            failed += 1
            store.log(actor, "forum.failed", str(row["id"]), str(exc)[:200])
            store.commit()
            break
        store.set_state(CURSOR_HANDOFF, str(row["created_at"]))
        store.commit()

    if posted or failed:
        store.log(actor, "forum.run", "",
                  "отправлено=%d пропущено=%d ошибок=%d"
                  % (posted, skipped, failed))
        store.commit()
    return {"состояние": "работа", "отправлено": posted,
            "без ветки": skipped, "ошибок": failed}
