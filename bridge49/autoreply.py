"""Что делать с решением движка.

Движок (`inbound_decision.decide_inbound_reply`) отвечает на вопрос «что
сказать», но ничего не знает ни про нашу базу, ни про очередь, ни про темп.
Этот модуль — вторая половина: он собирает движку контекст из наших таблиц и
раскладывает его решение по нашим действиям.

Прежний контур делал то же самое в `conversation.py`, но там это было
переплетено с чужой схемой (`conversations`, `recipients`, `send_queue`),
поэтому переносить его целиком смысла не было — перенесены сами решения.

Разделение труда важно держать в голове: движок решает, *что* ответить, и
насколько он в этом уверен; кому и когда физически можно писать — решает наш
preflight в `dispatcher`. Ни одно решение движка не отправляет сообщение само.

## Три состояния уверенности

Движок различает их сам, и мы доверяем его пометкам:

* уверен → ответ уходит молча;
* не уверен, но ответ есть → ответ уходит **и** метится на перечитывание;
* жёстко не уверен (`knowledge_gap`) → человеку уходит честное «зафиксировал
  вопрос для команды», а нам заводится карточка;
* сорван контракт или нет связи с моделью (`hold_for_review`) → человеку не
  уходит ничего, карточка заводится нам.
"""
from __future__ import annotations

import json
from typing import Any

from . import accounts as accounts_mod
from . import replies
from .inbound_decision import decide_inbound_reply
from .presales_context import non_silent_boundary_reply
from .store import Store, dumps, new_id, now

#: Как мы представляемся движку. Он писался провайдер-нейтральным, чтобы один
#: и тот же разбор работал поверх разных транспортов.
PROVIDER_ID = "outreach49"

#: Роли, которым вообще можно отвечать в личку. Движок проверяет это и сам, но
#: лучше отсеять раньше, чем тратить запрос к модели.
REPLY_ROLES = frozenset({"channel_sender", "chat_sender", "dm_sender"})

#: Решения, после которых человеку ничего не отправляется.
SILENT_DECISIONS = frozenset({"opt_out", "ignore", "pause_conversation",
                              "hold_for_review"})

#: Решения, после которых менеджеру заводится карточка.
HANDOFF_DECISIONS = frozenset({"manager_handoff", "knowledge_gap",
                               "hold_for_review"})

#: Ответ на жёсткую нехватку знаний. Текст перенесён дословно из прежнего
#: контура: он обещает ровно то, что мы действительно делаем — заводим
#: карточку менеджеру, — и не выдумывает фактов.
KNOWLEDGE_GAP_REPLY = (
    "Не хочу давать неточную информацию. Я зафиксировал этот вопрос для команды, "
    "и коллега вернется с ответом. А пока могу помочь по тому, как ТГ РАДАР ищет "
    "сигналы спроса, или показать бесплатный тест системы. Хотите посмотреть?"
)


class AutoReplyError(RuntimeError):
    """Автоответ построить нельзя, и это требует внимания человека."""


def thread_for(store: Store, inbound: dict) -> dict | None:
    return store.one(
        "SELECT * FROM threads WHERE account_id = ? AND peer_key = ?",
        (int(inbound["account_id"]), inbound["peer_key"]),
    )


def conversation_history(store: Store, thread: dict) -> list[dict[str, str]]:
    """Переписка диалога в том виде, в каком её ждёт движок.

    Собирается из трёх мест, потому что в трёх местах и лежит: перенесённая
    история (`history`), входящие (`inbound`) и наши отправленные ответы
    (`tasks`). Порядок — по времени; движок сам допишет текущее входящее, если
    его в хвосте не окажется.
    """
    rows: list[tuple[str, dict[str, str]]] = []

    for row in store.query(
        "SELECT direction, text, sent_at, created_at FROM history "
        "WHERE thread_id = ? ORDER BY sent_at, id",
        (thread["id"],),
    ):
        stamp = str(row["sent_at"] or row["created_at"] or "")
        rows.append((stamp, {
            "direction": row["direction"],
            "text": str(row["text"] or ""),
            "created_at": stamp,
        }))

    for row in store.query(
        "SELECT text, sent_at, created_at FROM inbound "
        "WHERE account_id = ? AND peer_key = ? ORDER BY id",
        (int(thread["account_id"]), thread["peer_key"]),
    ):
        stamp = str(row["sent_at"] or row["created_at"] or "")
        rows.append((stamp, {
            "direction": "inbound",
            "text": str(row["text"] or ""),
            "created_at": stamp,
        }))

    for row in store.query(
        "SELECT params, dispatched_at FROM tasks "
        "WHERE account_id = ? AND state = 'done' AND contact_id = ? "
        "ORDER BY dispatched_at",
        (int(thread["account_id"]), thread["contact_id"]),
    ):
        try:
            text = str(json.loads(row["params"] or "{}").get("text") or "")
        except (TypeError, ValueError):
            continue
        if not text:
            continue
        stamp = str(row["dispatched_at"] or "")
        rows.append((stamp, {
            "direction": "outbound",
            "text": text,
            "created_at": stamp,
        }))

    rows.sort(key=lambda item: item[0])
    return [item for _stamp, item in rows if item["text"].strip()]


def auto_reply_count(store: Store, thread: dict) -> int:
    """Сколько автоответов уже ушло в этом диалоге.

    Движку это нужно, чтобы вовремя предложить подключить живого коллегу, а не
    отвечать бесконечно.
    """
    row = store.one(
        "SELECT COUNT(*) AS n FROM tasks "
        "WHERE campaign_id = ? AND contact_id = ? AND state = 'done'",
        (replies.AUTO_CAMPAIGN_ID, thread["contact_id"]),
    )
    return int(row["n"]) if row else 0


def discovery_context(thread: dict) -> dict[str, str]:
    """Что модель уже выяснила о собеседнике в прошлые ходы."""
    try:
        raw = json.loads(str(thread["presales_context"] or "{}"))
    except (TypeError, ValueError, KeyError, IndexError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()
            if str(k).strip() and str(v).strip()}


def build_context(store: Store, inbound: dict, thread: dict) -> dict[str, Any]:
    """Собрать движку контекст из наших таблиц."""
    account = accounts_mod.get(store, int(inbound["account_id"]))
    if account is None:
        raise AutoReplyError(f"нет аккаунта {inbound['account_id']} в реестре")
    role = str(account["role"] or "")
    if role not in REPLY_ROLES:
        raise AutoReplyError(f"роль {role} не отвечает в личке")

    history = conversation_history(store, thread)
    handoff = store.one(
        "SELECT id FROM handoffs WHERE thread_id = ? AND status IN ('new','taken')",
        (thread["id"],),
    )
    return {
        "provider_id": PROVIDER_ID,
        "inbound_id": str(inbound["id"]),
        "account_id": str(inbound["account_id"]),
        "account_label": str(account["label"] or ""),
        "role": role,
        "peer_key": str(inbound["peer_key"]),
        "text": str(inbound["text"] or ""),
        "received_at": str(inbound["sent_at"] or inbound["created_at"] or ""),
        "conversation_id": str(thread["id"]),
        "campaign_id": str(thread["campaign_id"] or ""),
        "history": history,
        "auto_reply_count": auto_reply_count(store, thread),
        "discovery_context": discovery_context(thread),
        "semantic_handoff_active": handoff is not None,
    }


def remember_discovery(store: Store, thread: dict, update: dict) -> None:
    """Дописать в диалог то, что модель узнала на этом ходу."""
    if not update:
        return
    merged = discovery_context(thread)
    merged.update({str(k): str(v) for k, v in update.items()
                   if str(k).strip() and str(v).strip()})
    store.execute(
        "UPDATE threads SET presales_context = ?, updated_at = ? WHERE id = ?",
        (dumps(merged), now(), thread["id"]),
    )


def open_handoff(store: Store, thread: dict, reason: str, note: str = "") -> str:
    """Завести карточку менеджеру, если по диалогу её ещё нет."""
    existing = store.one(
        "SELECT id FROM handoffs WHERE thread_id = ? AND status IN ('new','taken')",
        (thread["id"],),
    )
    if existing is not None:
        return str(existing["id"])
    handoff_id = new_id("handoff")
    store.execute(
        "INSERT INTO handoffs(id, thread_id, reason, status, note, "
        "created_at, updated_at) VALUES(?,?,?,'new',?,?,?)",
        (handoff_id, thread["id"], reason, note or None, now(), now()),
    )
    store.execute(
        "UPDATE threads SET state = 'handoff', updated_at = ? WHERE id = ?",
        (now(), thread["id"]),
    )
    return handoff_id


def review_mark(decision: dict) -> str:
    """Почему этот ответ стоит перечитать глазами. Пусто — движок был уверен.

    Мы не выдумываем свой порог уверенности поверх движка: он уже отбраковал
    всё, в чём сомневается по существу. Здесь отмечается то, что он выдал с
    оговорками — предупреждения проверки и повышенный риск темы.
    """
    marks = []
    warnings = decision.get("validation_warnings") or []
    if warnings:
        marks.append("предупреждения: " + ", ".join(str(w) for w in warnings))
    if str(decision.get("risk_level") or "") == "high":
        marks.append("рискованная тема")
    if decision.get("handoff_kind") not in (None, "", "none"):
        marks.append(f"обещание менеджера: {decision['handoff_kind']}")
    return "; ".join(marks)


def apply(
    store: Store,
    inbound: dict,
    decision: dict,
    *,
    scheduled_at: str | None = None,
    actor: str = "autoreply",
) -> dict[str, Any]:
    """Разложить решение движка по нашим действиям.

    Возвращает, что именно сделано, — вызывающий сам решает, логировать это
    или показать человеку. Отправку по-прежнему делает только `dispatch`.
    """
    thread = thread_for(store, inbound)
    if thread is None:
        raise AutoReplyError("нет диалога для входящего")

    verdict = str(decision.get("decision") or "")
    reply_text = str(decision.get("reply_text") or "").strip()
    result: dict[str, Any] = {
        "decision": verdict,
        "task": "",
        "handoff": "",
        "sent_text": "",
        "review_reason": "",
    }

    remember_discovery(store, thread, decision.get("collected_fields_update") or {})

    if verdict == "opt_out":
        # Единственный случай, когда мы молчим полностью: человек попросил не
        # писать. Ответ «принято» был бы ещё одним сообщением после запрета.
        store.execute(
            "UPDATE contacts SET opted_out = 1 WHERE id = ?",
            (thread["contact_id"],),
        )
        store.execute(
            "UPDATE threads SET state = 'closed', updated_at = ? WHERE id = ?",
            (now(), thread["id"]),
        )
        store.log(actor, "autoreply.opt_out", thread["id"], "контакт закрыт")
        store.commit()
        return result

    if verdict == "ignore":
        # Рекламному спаму не отвечаем вовсе. На остальное непонятное (не по-
        # русски, бессмыслица) отвечаем короткой вежливой рамкой: молчание в
        # личной переписке читается как «нас забанили», а не как фильтр.
        intent = str(decision.get("intent") or "")
        if intent == "spam":
            store.log(actor, "autoreply.spam", thread["id"], "молча пропущено")
            store.commit()
            return result
        reply_text = non_silent_boundary_reply(intent)

    if verdict == "knowledge_gap":
        reply_text = KNOWLEDGE_GAP_REPLY

    if verdict in HANDOFF_DECISIONS:
        note = str(decision.get("knowledge_gap") or decision.get("reason") or "")
        result["handoff"] = open_handoff(store, thread, verdict, note)

    if verdict == "pause_conversation":
        store.execute(
            "UPDATE threads SET state = 'awaiting', updated_at = ? WHERE id = ?",
            (now(), thread["id"]),
        )

    should_send = reply_text and (
        verdict not in SILENT_DECISIONS or verdict == "ignore"
    )
    if should_send:
        reason = review_mark(decision)
        if verdict == "knowledge_gap":
            reason = "; ".join(filter(None, [reason, "нехватка знаний"]))
        queued = replies.queue_reply(
            store,
            text=reply_text,
            thread_id=thread["id"],
            campaign_id=replies.ensure_reply_campaign(
                store,
                replies.AUTO_CAMPAIGN_ID,
                replies.AUTO_CAMPAIGN_NAME,
                "служебная: автоответы на входящие",
            ),
            review_reason=reason or None,
            scheduled_at=scheduled_at,
            actor=actor,
        )
        result["task"] = queued["task"]
        result["sent_text"] = reply_text
        result["review_reason"] = reason

    store.log(actor, f"autoreply.{verdict}", thread["id"],
              f"задача={result['task'] or '—'} карточка={result['handoff'] or '—'}")
    store.commit()
    return result


def handle(
    store: Store,
    inbound: dict,
    *,
    command: str | None = None,
    scheduled_at: str | None = None,
    actor: str = "autoreply",
    llm_caller=None,
) -> dict[str, Any]:
    """Полный ход: собрать контекст, спросить движок, разложить решение."""
    thread = thread_for(store, inbound)
    if thread is None:
        raise AutoReplyError("нет диалога для входящего")
    context = build_context(store, inbound, thread)
    kwargs: dict[str, Any] = {"command": command}
    if llm_caller is not None:
        kwargs["llm_caller"] = llm_caller
    decision = decide_inbound_reply(context, **kwargs)
    applied = apply(store, inbound, decision, scheduled_at=scheduled_at, actor=actor)
    return {**applied, "engine": decision}
