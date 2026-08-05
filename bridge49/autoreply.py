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

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from . import accounts as accounts_mod
from . import direct_invite
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

#: Чем подменяем пустой текст. Голосовое и файл приезжают без текста, и разбор
#: на них падал исключением: движок требует непустое сообщение. Человек при
#: этом не получал ничего — 05.08 так пропал собеседник, который накануне писал
#: «Направьте информацию, изучим». Подмена перенесена из прежнего контура: она
#: даёт движку понять, что сообщение было, и не выдумывает его содержания.
ATTACHMENT_PLACEHOLDER = "[Вложение без текста]"

#: Сколько собеседник должен помолчать, чтобы ход считался законченным.
TURN_QUIET = 45.0

#: Предел ожидания. Пишущий без пауз обязан получить ответ.
TURN_MAX_WAIT = 300.0

#: Решения, после которых человеку ничего не отправляется.
#:
#: `pause_conversation` отсюда убран, и это не послабление. Нормализатор
#: схлопывает в это имя два разных действия движка: `reply_and_pause`, где
#: текст обязателен и непуст, и голый `pause`, где текста нет вовсе. Держать их
#: вместе в списке молчаливых значило выбрасывать написанный ответ: за 05.08
#: так пропало 13 текстов из 14, от 24 до 335 знаков, при уверенности модели
#: 0.98–0.99. Промпт при этом прямо требует обратного — «обычный вежливый отказ
#: не оставляй без ответа».
#:
#: Голый `pause` после этого ничего не отправит сам: текста у него нет, а
#: нормализатор роняет ответ, если текст пришёл с действием, которое отвечать
#: не должно. То есть развилка держится не на этом списке, а на самом движке.
SILENT_DECISIONS = frozenset({"opt_out", "ignore", "hold_for_review"})

#: Решения, после которых менеджеру заводится карточка.
#:
#: Имена обязаны совпадать со словарём движка, а он такой:
#: reply | reply_and_pause | reply_and_handoff | handoff | ignore | opt_out |
#: pause | knowledge_gap (плюс hold_for_review при срыве контракта).
#:
#: Здесь стоял `manager_handoff` — имя, которого движок не выдаёт никогда. Из-за
#: этого пять раз подряд машина написала человеку «передаю менеджеру», и ни одной
#: карточки заведено не было: условие не совпадало, а несовпадение выглядело как
#: штатная работа. Ошибка тем опаснее, что с включением автоответов поллер
#: перестал заводить карточки сам — этот список стал единственным путём к
#: человеку.
#:
#: `manager_handoff` оставлен как страховка на случай, если такое имя появится.
HANDOFF_DECISIONS = frozenset({"reply_and_handoff", "handoff",
                               "manager_handoff", "knowledge_gap",
                               "hold_for_review"})

#: Весь словарь вердиктов движка. Нужен не для работы, а для проверки: список
#: выше обязан быть его подмножеством, иначе опечатка снова превратится в тихий
#: отказ вместо поломки.
ENGINE_DECISIONS = frozenset({
    "reply", "reply_and_pause", "reply_and_handoff", "handoff", "ignore",
    "opt_out", "pause", "knowledge_gap", "hold_for_review",
    # Имена, в которые наш слой переводит часть вердиктов движка.
    "auto_reply", "pause_conversation", "manager_handoff",
})

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
    """Диалог по входящему. Возвращаем словарь, а не строку курсора: у
    ``sqlite3.Row`` нет ``get``, а необязательные поля читаются именно так."""
    row = store.one(
        "SELECT * FROM threads WHERE account_id = ? AND peer_key = ?",
        (int(inbound["account_id"]), inbound["peer_key"]),
    )
    return dict(row) if row is not None else None


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


def we_started_it(store: Store, thread: dict) -> bool:
    """Начинали ли мы этот разговор.

    На аккаунтах остались собеседники прежних владельцев: они пишут не нам и
    не про нас. Машине отвечать им нечего — она либо ответит по-русски тому,
    кто спрашивал про аренду на фарси, либо начнёт продавать ТГ РАДАР
    человеку, который нас не искал. И то и другое — переписка с посторонними
    от нашего имени.

    Поэтому автоответ работает только там, где первое слово было нашим:
    перенесённая история, наша отправленная задача или отметка исходящего в
    диалоге. Остальное уходит менеджеру карточкой — ровно как было до
    появления автоответов, то есть хуже не становится.

    Ослабить это можно файлом ``var/AUTOREPLY_STRANGERS`` — тогда движок
    возьмётся и за пришедших самостоятельно.
    """
    if thread.get("last_outbound_at"):
        return True
    if store.one(
        "SELECT 1 FROM history WHERE thread_id = ? AND direction = 'outbound' LIMIT 1",
        (thread["id"],),
    ):
        return True
    return store.one(
        "SELECT 1 FROM tasks WHERE contact_id = ? AND state = 'done' LIMIT 1",
        (thread["contact_id"],),
    ) is not None


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


def account_role_for(store: Store, inbound: dict) -> str:
    """Роль аккаунта, по которой определяется канал согласия.

    Берём ту роль, чей канал совпадает с поверхностью этого входящего. Раньше
    бралась «первая, которая вообще отображается в канал», и это было неверно
    дважды. Во-первых, роли лежат в `set` (`accounts._hydrate`), а порядок
    обхода множества строк зависит от случайной затравки хеша — то есть от
    процесса. У аккаунта 862 с ролями `chat_sender` и `dm_sender` канал
    согласия выпадал монеткой: шесть прогонов подряд дали 4×`public_chat` и
    2×`private_dm` для разговора, который весь целиком был личкой. Во-вторых,
    даже детерминированный выбор «первой попавшейся» лгал бы: канал уезжает в
    учёт выданных доступов на стороне StartBot.

    Если подходящей роли у аккаунта нет, возвращаем пустую строку. Тогда
    `record_consent` откажет и разговор уйдёт менеджеру — приписать согласию
    канал, которого не было, хуже, чем не выдать доступ автоматически.
    """
    account = accounts_mod.get(store, int(inbound["account_id"]))
    if account is None:
        return ""
    roles = account.get("roles") or []
    if isinstance(roles, str):
        try:
            roles = json.loads(roles)
        except (TypeError, ValueError):
            roles = []
    candidates = sorted(str(r) for r in roles) or [str(account.get("role") or "")]
    surface = str(inbound.get("surface") or "").strip()
    for role in candidates:
        if direct_invite.source_channel_for_role(role) == surface:
            return role
    return ""


def build_context(
    store: Store, inbound: dict, thread: dict,
    *, branch_config: "direct_invite.BranchConfig | None" = None,
) -> dict[str, Any]:
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

    # Автовыдача бесплатного теста. Движок обязан знать две вещи, и они разные.
    #
    # Каталог — исчерпывающий список сфер, которым доступ выдаётся без
    # менеджера. По нему движок сам сопоставляет сферу, подтверждённую
    # человеком в переписке.
    #
    # Ветка — сфера, известная нам заранее, из маршрута: откуда человек к нам
    # пришёл. Промпт принимает любое из двух («branch=automatic ЛИБО сфера
    # совпала с элементом каталога»), но путать их нельзя. Свободный текст из
    # разговора сюда не годится: сфера там записана словами человека, а не
    # идентификатором, и совпадения не будет никогда — ключ молча вырождается
    # в «менеджер». Поэтому читаем только нормализованные имена, как читал их
    # прежний контур.
    branch = branch_config or direct_invite.BranchConfig.from_env()
    known = discovery_context(thread)
    branch_context = None
    for key in ("direct_invite_sector_id", "sector_id"):
        candidate = str(known.get(key) or "").strip()
        if candidate:
            branch_context = branch.context_for_sector(candidate)
            if branch_context is not None:
                break

    return {
        "provider_id": PROVIDER_ID,
        "inbound_id": str(inbound["id"]),
        "account_id": str(inbound["account_id"]),
        "account_label": str(account["label"] or ""),
        "role": role,
        "peer_key": str(inbound["peer_key"]),
        "text": str(inbound["text"] or "") or ATTACHMENT_PLACEHOLDER,
        "received_at": str(inbound["sent_at"] or inbound["created_at"] or ""),
        "conversation_id": str(thread["id"]),
        "campaign_id": str(thread["campaign_id"] or ""),
        "history": history,
        "auto_reply_count": auto_reply_count(store, thread),
        "discovery_context": discovery_context(thread),
        "semantic_handoff_active": handoff is not None,
        "direct_invite_sector_catalog": branch.active_sector_catalog(),
        "direct_invite_context": branch_context or {},
        "free_test_access_branch": branch_context or {"branch": "manager"},
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
    """Завести карточку менеджеру или освежить уже открытую.

    Вторая карточка по одному диалогу не нужна — но и молча возвращать первую
    нельзя. Раньше повтор не менял в ней ничего, и получалось два дефекта
    сразу. Собеседник, которому третьи сутки обещают менеджера, не создавал
    никакого нового сигнала: его карточка от позавчера так и лежала с
    нетронутой отметкой времени, неотличимая от вчерашних. А содержимое
    отставало от разговора — карточка говорила «сфера подтверждена, запустить
    тест», когда машина уже написала человеку, что схема ему не подходит.

    Поэтому повтор освежает карточку: новая причина, новая заметка, новое
    время. Форум-постер видит изменение и выносит её заново.
    """
    existing = store.one(
        "SELECT id, reason, note FROM handoffs "
        " WHERE thread_id = ? AND status IN ('new','taken')",
        (thread["id"],),
    )
    if existing is not None:
        store.execute(
            "UPDATE handoffs SET reason = ?, note = ?, updated_at = ? "
            " WHERE id = ?",
            (reason or existing["reason"], note or existing["note"],
             now(), existing["id"]),
        )
        store.commit()
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
    branch_config: "direct_invite.BranchConfig | None" = None,
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
        # Заявка на автовыдачу. Непустая означает, что человек получит ссылку
        # сам и менеджер ему не нужен.
        "invite": "",
        # Ссылка выпущена прямо здесь и уедет этим же письмом. Пусто — значит
        # письмо обещает её отдельно, а выпуск подберёт свой проход.
        "invite_inline": "",
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

    # Развилка автовыдачи. У них она стоит ровно здесь же — перед тем, как
    # звать менеджера: `if direct_invite is not None: ... else: create_handoff`.
    # Согласие на тест по разрешённой сфере закрывается само, и карточка тогда
    # не нужна — она означала бы, что человека всё-таки ждёт менеджер.
    #
    # Если ветка не подошла (выключена, сфера чужая, канал не тот), всё идёт
    # прежним путём. Автоматика умеет только добавлять.
    if direct_invite.consent_from_decision(decision):
        recorded = direct_invite.record_consent(
            store,
            config=branch_config or direct_invite.BranchConfig.from_env(),
            thread=thread,
            inbound=inbound,
            account_role=account_role_for(store, inbound),
            sector_id=direct_invite.sector_from_decision(decision),
        )
        if recorded is not None:
            result["invite"] = str(recorded["request_id"])
            # Ссылку выпускаем прямо здесь, чтобы она уехала этим же письмом.
            # Не вышло — `issued` пустой, и всё идёт прежним путём: ответ
            # движка обещает ссылку отдельно, а выпуск подберёт свой проход.
            issued = direct_invite.issue_inline(
                store, str(recorded["request_id"]),
                config=branch_config or direct_invite.BranchConfig.from_env(),
                actor=actor,
            )
            if issued:
                reply_text = str(issued["text"])
                result["invite_inline"] = issued["invite_row_id"]
            # Сферу, которую движок сопоставил с каталогом, запоминаем в
            # нормализованном виде. Со следующего хода она приходит уже как
            # известная из маршрута, и движку не приходится сопоставлять её
            # заново по свободному тексту — а значит, и ошибаться заново.
            remember_discovery(
                store, thread,
                {"direct_invite_sector_id": str(recorded["outreach_sector_id"])},
            )

    if verdict in HANDOFF_DECISIONS and not result["invite"]:
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
        try:
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
                # Человек мог дописать, пока наш ответ ждёт паузы на чтение.
                # Тогда его надо пересобрать, а не звать менеджера.
                supersede=True,
            )
        except Exception:
            # Ссылка уже выпущена, но письма с ней не будет. Возвращаем заявку
            # в очередь, иначе она осталась бы «выпущенной» без доставки —
            # человек согласился, ссылка существует, и никто её не везёт.
            if result.get("invite_inline"):
                direct_invite.release_inline(
                    store, str(result["invite_inline"]),
                    "ответ не поставлен в очередь", actor=actor)
            raise
        result["task"] = queued["task"]
        result["sent_text"] = reply_text
        result["review_reason"] = reason
        if result.get("invite_inline"):
            direct_invite.attach_delivery(
                store, str(result["invite_inline"]), str(queued["task"]),
                actor=actor)
    elif result.get("invite_inline"):
        # Письма не будет вовсе — вернём ссылку отдельному проходу.
        direct_invite.release_inline(
            store, str(result["invite_inline"]),
            "ответ не отправляется на этом ходу", actor=actor)

    store.log(actor, f"autoreply.{verdict}", thread["id"],
              f"задача={result['task'] or '—'} карточка={result['handoff'] or '—'} "
              f"тест={result['invite'] or '—'}")
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
    branch_config: "direct_invite.BranchConfig | None" = None,
) -> dict[str, Any]:
    """Полный ход: собрать контекст, спросить движок, разложить решение.

    Ворота на посторонних проверяет проход `run`: они зависят от настроек, а
    сюда настройки не приходят. А вот арабское письмо проверяется прямо здесь
    и безусловно — правило не должно обходиться тем, что кто-то позвал `handle`
    напрямую, мимо прохода.
    """
    thread = thread_for(store, inbound)
    if thread is None:
        raise AutoReplyError("нет диалога для входящего")
    if arabic_script_peer(store, inbound, thread):
        raise AutoReplyError("собеседник записан арабским письмом")
    context = build_context(store, inbound, thread,
                            branch_config=branch_config)
    kwargs: dict[str, Any] = {"command": command}
    if llm_caller is not None:
        kwargs["llm_caller"] = llm_caller
    decision = decide_inbound_reply(context, **kwargs)
    applied = apply(store, inbound, decision, scheduled_at=scheduled_at,
                    actor=actor, branch_config=branch_config)
    return {**applied, "engine": decision}


#: Арабское письмо во всех блоках Unicode, включая персидские буквы и формы
#: представления. Персидский и арабский пользуются одной графикой, поэтому
#: правило накрывает и то и другое.
ARABIC_SCRIPT = re.compile(
    r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]"
)


def arabic_script_peer(store: Store, inbound: dict, thread: dict) -> bool:
    """Записан ли собеседник арабским письмом.

    Подавление чужого языка в движке смотрит на **текст** сообщения, а этого
    мало: с арабским ником, но русским текстом входящее доезжает до модели и
    получает ответ. На наших номерах такие собеседники остались от прежних
    владельцев, и разговаривать с ними машине незачем.

    Признак это косвенный: человек с арабским именем может писать по-русски и
    быть настоящим клиентом. Поэтому не молчим в пустоту, а заводим карточку —
    решает человек, а не автомат.
    """
    parts = [inbound.get("peer_username"), inbound.get("peer_key")]
    contact = store.one(
        "SELECT username, display_name FROM contacts WHERE id = ?",
        (thread.get("contact_id"),),
    )
    if contact is not None:
        parts += [contact["username"], contact["display_name"]]
    return any(ARABIC_SCRIPT.search(str(part or "")) for part in parts)


def inbound_age_hours(inbound: dict, *, at: datetime | None = None) -> float:
    """Сколько часов прошло с момента, когда человек это написал.

    Считается от `sent_at` — времени по Telegram, а не от того, когда запись
    завелась у нас. Разница принципиальная: `created_at` у старого сообщения,
    только что попавшего в базу, будет «сейчас», и гейт давности по нему не
    сработал бы ровно в том случае, для которого написан.

    Нечитаемой даты быть не должно: продюсер конверта в Radar пишет дату
    безусловно и падает на дате без часового пояса, то есть сообщение без даты
    не публикуется вовсе. Поэтому пустое или нечитаемое время — признак
    сломанного конверта, и считается оно бесконечно старым, а не свежим:
    предохранитель обязан отказывать в сторону человека.
    """
    raw = str(inbound.get("sent_at") or "")
    try:
        sent = datetime.fromisoformat(raw)
    except ValueError:
        return float("inf")
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    moment = at or datetime.now(timezone.utc)
    return max(0.0, (moment - sent).total_seconds() / 3600.0)


def skip_reason(store: Store, inbound: dict, thread: dict, settings) -> str:
    """Почему с этим входящим машина не работает. Пусто — работает."""
    if arabic_script_peer(store, inbound, thread):
        return "собеседник записан арабским письмом"
    if not (settings.autoreply_strangers or we_started_it(store, thread)):
        return "входящее от постороннего"
    limit = int(settings.limits.reply_max_inbound_age_hours)
    age = inbound_age_hours(inbound)
    if age > limit:
        # Живой фид сюда не попадает: поллер ходит раз в пятнадцать секунд.
        # Сработавший гейт означает, что в очереди оказалась старая переписка —
        # и это не редкость, а штатный путь: Radar при старте аккаунта сам
        # добирает из истории ответы, пришедшие пока аккаунт стоял
        # (`sync_outreach_private_dm_history`), и публикует их с подлинной
        # старой датой. Разбирать такое должен человек: уместен ли ещё ответ,
        # видно только ему.
        if age == float("inf"):
            return "у входящего нет времени отправки"
        return f"входящее пролежало {age:.0f} ч (предел {limit} ч)"
    return ""


def reply_moment(inbound: dict, settings) -> str:
    """Когда выпускать ответ: спустя паузу на чтение от самого входящего.

    Это не темп, а правдоподобие. Ответ через полсекунды выдаёт автомат
    вернее любого текста, поэтому пауза отсчитывается от момента, когда
    человек написал, а не от того, когда до него дошли руки у нас.

    Разброс детерминированный — от идентификатора входящего. Повторный
    прогон даст тот же момент, а не сдвинет отправку ещё на полминуты.
    """
    limits = settings.limits
    raw = str(inbound.get("sent_at") or inbound.get("created_at") or "")
    try:
        base = datetime.fromisoformat(raw)
    except ValueError:
        base = datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    low = max(0, int(limits.reply_delay_after_inbound_min_sec))
    high = max(low, int(limits.reply_delay_after_inbound_max_sec))
    span = high - low
    digest = hashlib.sha256(str(inbound["id"]).encode("utf-8")).hexdigest()
    delay = low + (int(digest[:8], 16) % (span + 1) if span else 0)
    moment = base + timedelta(seconds=delay)
    # Входящее могло пролежать: назад во времени задачу ставить бессмысленно.
    return max(moment, datetime.now(timezone.utc)).isoformat()


def pending(store: Store, limit: int = 20, *, at: str | None = None) -> list[dict]:
    """Входящие, которые ещё не разобраны, по одному ходу на собеседника.

    Схлопывание внутри прогона было всегда: несколько сообщений подряд — это
    один ход, и три ответа на него выглядели бы хуже, чем один на всё сразу.
    Обогнанные помечаем разобранными, их текст всё равно уедет в модель как
    история.

    Но работало это только внутри тика. Человек, дописавший мысль через десять
    секунд, попадал уже в следующий прогон — и получал второе сообщение:

        11:54:21  он  И как это работает?
        11:54:32  он  Мне писать люди будут по поводу таро?
        11:55:38  мы  ТГ РАДАР — ИИ-сервис…
        11:56:54  мы  Не совсем: ТГ РАДАР сначала находит…

    Заменить ждущий ответ на пересобранный (`supersede`) нельзя: к следующему
    тику диспетчер уже закрепил за ним запрос. За всю историю базы замена не
    сработала ни разу — то есть чинить надо не её.

    Поэтому ход считается законченным, когда собеседник помолчал ``TURN_QUIET``.
    Цена — эти секунды, и она мала: медиана ответа сейчас 5–7 минут.

    Окно действует только в середине разговора, где мы этому человеку уже
    отвечали: дубли рождаются там, а ждать пришлось бы каждого нового
    собеседника. И оно не вечно — через ``TURN_MAX_WAIT`` ход закрывается
    принудительно, иначе пишущий без пауз не дождался бы ответа никогда.
    """
    stamp = at or now()
    rows = [dict(r) for r in store.query(
        "SELECT * FROM inbound WHERE handled = 0 ORDER BY id", ()
    )]
    newest: dict[tuple[int, str], dict] = {}
    oldest: dict[tuple[int, str], dict] = {}
    superseded: list[int] = []
    for row in rows:
        key = (int(row["account_id"]), str(row["peer_key"]))
        if key in newest:
            superseded.append(int(newest[key]["id"]))
        else:
            oldest[key] = row
        newest[key] = row
    if superseded:
        marks = ",".join("?" * len(superseded))
        store.execute(
            f"UPDATE inbound SET handled = 1 WHERE id IN ({marks})", superseded
        )
        store.commit()

    return [row for key, row in newest.items()
            if _turn_is_closed(store, row, oldest[key], stamp)][:limit]


def _turn_is_closed(store: Store, newest: dict, oldest: dict, stamp: str) -> bool:
    """Закончил ли собеседник мысль."""
    quiet = _age_seconds(newest.get("sent_at") or newest.get("created_at"), stamp)
    if quiet is None or quiet >= TURN_QUIET:
        # Неразобранная отметка не должна задерживать ответ.
        return True
    waited = _age_seconds(oldest.get("sent_at") or oldest.get("created_at"), stamp)
    if waited is not None and waited >= TURN_MAX_WAIT:
        return True
    return not _conversation_started(store, newest)


def _age_seconds(stamp: str | None, at: str) -> float | None:
    try:
        then = datetime.fromisoformat(str(stamp))
        moment = datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (moment - then).total_seconds()


def _conversation_started(store: Store, row: dict) -> bool:
    """Писали ли мы этому человеку — то есть идёт ли уже разговор.

    Считается любое наше отправленное сообщение, а не только ответ. Сначала
    здесь стояли одни ответы, и проверка на боевых данных показала, чего это
    стоит: `@deliverymag`, один из трёх подтверждённых дублей 05.08, окном не
    закрывался. Ему уходило холодное письмо, а не ответ, — разговор с нашей
    стороны начался, серия сообщений пришла на него же.
    """
    contact_id = row.get("contact_id")
    if not contact_id:
        return False
    return store.one(
        "SELECT 1 FROM tasks WHERE contact_id = ? AND state = 'done' LIMIT 1",
        (contact_id,),
    ) is not None


def run(
    store: Store,
    settings,
    *,
    limit: int = 20,
    command: str | None = None,
    actor: str = "autoreply",
    llm_caller=None,
) -> dict[str, Any]:
    """Разобрать накопившиеся входящие. Ничего не отправляет — только ставит.

    Вызывается отдельным проходом, а не из поллера: обращение к модели идёт
    десятки секунд, и фид входящих на это время встал бы.
    """
    if not settings.autoreply_enabled:
        return {"enabled": False, "handled": 0, "queued": 0, "failed": 0}

    # Один раз на проход: конфиг читается с диска, и дёргать его на каждое
    # входящее незачем.
    branch_config = direct_invite.BranchConfig.from_env()

    handled = queued = failed = invited = skipped = 0
    for inbound in pending(store, limit):
        thread = thread_for(store, inbound)
        reason = skip_reason(store, inbound, thread, settings) if thread else ""
        if reason:
            # Модель даже не зовём: отвечать тут нечего, а вызов стоит денег и
            # минуты. Менеджер увидит карточку, как и до автоответов.
            open_handoff(store, thread, reason,
                         str(inbound.get("text") or "")[:300])
            store.execute("UPDATE inbound SET handled = 1 WHERE id = ?",
                          (int(inbound["id"]),))
            store.commit()
            skipped += 1
            handled += 1
            continue
        try:
            result = handle(
                store, inbound,
                command=command,
                scheduled_at=reply_moment(inbound, settings),
                actor=actor,
                llm_caller=llm_caller,
                branch_config=branch_config,
            )
            if result["task"]:
                queued += 1
            if result.get("invite"):
                invited += 1
        except Exception as exc:  # noqa: BLE001 — разбор одного не рушит проход
            failed += 1
            store.log(actor, "autoreply.failed", str(inbound["id"]),
                      f"{exc.__class__.__name__}: {exc}"[:300])
            thread = thread_for(store, inbound)
            if thread is not None:
                open_handoff(store, thread, "autoreply_failed",
                             f"{exc.__class__.__name__}: {exc}"[:300])
        # Помечаем разобранным в любом случае, включая неудачу: иначе одно
        # неразбираемое сообщение крутилось бы в голове очереди вечно и не
        # давало бы разобрать остальные. Про неудачу знает журнал и карточка.
        store.execute("UPDATE inbound SET handled = 1 WHERE id = ?",
                      (int(inbound["id"]),))
        handled += 1
        store.commit()

    store.log(actor, "autoreply.run", "",
              f"разобрано={handled} поставлено={queued} ошибок={failed} "
              f"пропущено={skipped} тестов={invited}")
    store.commit()
    return {"enabled": True, "handled": handled, "queued": queued,
            "failed": failed, "skipped": skipped, "invited": invited}
