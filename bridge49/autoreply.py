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
from . import outreach_texts
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
#:
#: На этом же и стоит молчание в ответ на прямой отказ: движок возвращает
#: голый `pause`, текста нет, отправлять нечего. Поэтому здесь нельзя
#: подставлять запасной текст на пустой ответ — «понял, спасибо» после «нет»
#: и просили убрать. Карточки эта ветка тоже не заводит намеренно: отказ
#: менеджеру не нужен, диалог просто уходит в `awaiting`.
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
#:
#: ⚠️ Членство в этом списке — необходимое условие карточки, но с 06.08 уже не
#: достаточное: решает `manager_card_reason` ниже. Список остался, чтобы
#: опечатка в имени вердикта по-прежнему ломалась, а не тихо молчала.
HANDOFF_DECISIONS = frozenset({"reply_and_handoff", "handoff",
                               "manager_handoff", "knowledge_gap",
                               "hold_for_review"})

#: Вердикты «движок не понял, о чём разговор».
#:
#: `knowledge_gap` — не хватило утверждённых фактов, движок честно об этом
#: сказал. `hold_for_review` — сорвался контракт с моделью: ответа нет вовсе,
#: и что там было, знает только человек. Оба — законный повод звать менеджера
#: и оба обязаны его звать: это единственный способ узнать о непредсказуемом
#: сценарии.
UNCLEAR_DECISIONS = frozenset({"knowledge_gap", "hold_for_review"})

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

    # Сортировать ISO-строки как строки нельзя: исходящие приезжают со
    # смещением +03:00, входящие с +00:00, и «14:00+03» оказывается позже
    # «12:00+00», хотя это один и тот же момент. Переписка выстраивалась не в
    # том порядке — пока только в перенесённых тредах, но модель читает её как
    # ход разговора.
    rows.sort(key=lambda item: _sort_key(item[0]))
    return [item for _stamp, item in rows if item["text"].strip()]


def _sort_key(stamp: str):
    """Момент времени. Неразобранное уходит в конец, а не в случайное место."""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return (1, datetime.max.replace(tzinfo=timezone.utc))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (0, moment)


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
    # Разведывательные чтения (метаданные канала, проверка личек) сюда не
    # годятся: они ничего человеку не отправляли, а гейт «мы начали разговор»
    # именно про отправленное. Иначе разведка молча открывает ворота.
    return store.one(
        "SELECT 1 FROM tasks WHERE contact_id = ? AND state = 'done' "
        "  AND action IN ('send_private_dm', 'send_channel_dm', "
        "                 'send_public_chat_message', 'reply_private_dm', "
        "                 'reply_channel_dm') LIMIT 1",
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


def outreach_sector_of_thread(store: Store, thread: dict) -> str:
    """Сфера, о которой мы заговорили с этим человеком первыми.

    Движок узнаёт сферу только из слов собеседника, и это правильно почти
    всегда. Но у полос «чаты» и «личка каналов» текст первого касания сам
    называет сферу: он написан про подбор и привоз авто и ни про что другое.
    Человек, ответивший на такое сообщение «Согласны», уже сказал, о чём речь,
    — просто не повторил вслух того, что написали ему мы.

    Без этого выходило нелепо: 06.08 владелец канала про авто из Кореи
    согласился на бесплатный тест, а в ответ получил вопрос, в какой он сфере.

    Возвращаем сферу только когда первое касание восстанавливается из ника
    посимвольно (`outreach_texts.sector_of_first_touch`). Ручная отправка,
    чужой текст, письмо из другой полосы — всё это не совпадёт, и тогда сфера
    остаётся неизвестной, как и была. Догадок здесь быть не должно: выдача
    доступа в чужую тестовую группу отзывается только руками.

    Личку людей (`send_private_dm`) сюда намеренно не берём: там письма
    написаны под конкретного человека из лидов и сферы у них разные.
    """
    contact_id = str(thread.get("contact_id") or "")
    if not contact_id:
        return ""
    row = store.one(
        "SELECT c.username, t.params FROM tasks t "
        "  JOIN contacts c ON c.id = t.contact_id "
        " WHERE t.contact_id = ? AND t.state = 'done' "
        "   AND t.action IN ('send_channel_dm', 'send_public_chat_message') "
        " ORDER BY t.dispatched_at LIMIT 1",
        (contact_id,),
    )
    if row is None:
        return ""
    try:
        params = json.loads(str(row["params"] or "{}"))
    except ValueError:
        return ""
    if not isinstance(params, dict):
        return ""
    return outreach_texts.sector_of_first_touch(
        str(row["username"] or ""), str(params.get("text") or "")
    )


def account_role_for(store: Store, inbound: dict) -> str:
    """Роль, которой позволено отвечать на поверхности этого входящего.

    Берём ту, которой политика разрешает тут говорить. Раньше бралась «первая,
    которая вообще отображается в канал», и это было неверно дважды. Во-первых,
    роли лежат в `set` (`accounts._hydrate`), а порядок обхода множества строк
    зависит от случайной затравки хеша — то есть от процесса. У аккаунта 862 с
    ролями `chat_sender` и `dm_sender` канал согласия выпадал монеткой: шесть
    прогонов подряд дали 4×`public_chat` и 2×`private_dm` для разговора,
    который весь целиком был личкой. Во-вторых, даже детерминированный выбор
    «первой попавшейся» лгал бы: канал уезжает в учёт выданных доступов на
    стороне StartBot.

    Следующая версия требовала, чтобы канал роли в точности совпадал с
    поверхностью, — и это отрезало выдачу почти всем. `channel_sender`, которому
    ответили в личку, совпадения не давал; `chat_sender` не давал его никогда,
    потому что его единственная законная поверхность ответа — как раз личка.
    Теперь совпадение проверяется по праву отвечать (`ROLE_SURFACES`), а канал
    берётся из самой поверхности — где идёт разговор, видно и без гаданий.

    Если отвечать этой роли тут нельзя, возвращаем пустую строку. Тогда
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
    # Сначала роль, для которой эта поверхность — родная: у аккаунта с
    # `chat_sender` и `dm_sender` личку обязан забирать второй, а не первый по
    # алфавиту. Только если такой нет — любая, которой тут отвечать позволено.
    for role in candidates:
        if direct_invite.source_channel_for_role(role) == surface:
            return role
    for role in candidates:
        if direct_invite.reply_channel(role, surface):
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

    if branch_context is None:
        candidate = outreach_sector_of_thread(store, thread)
        if candidate:
            branch_context = branch.context_for_sector(candidate)

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
        # Словарь для сопоставления сферы. Отдельный ключ, а не обогащение
        # каталога выше: тот одновременно и список разрешённых для выдачи
        # сфер, и enum обёртки, и его изменение поменяло бы поведение шести
        # боевых сфер.
        "sector_matching_catalog": branch.matching_catalog(),
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


def demo_route_applies(
    decision: dict,
    *,
    branch_config: "direct_invite.BranchConfig | None" = None,
) -> bool:
    """Положена ли этому ходу ссылка на демо-бота без всякого согласия.

    Условие одно: человек назвал свою сферу, и готовой тестовой группы под неё
    нет. Тогда ссылка на общий демо-бот — единственное, что мы можем дать
    сразу, и просить её незачем: она бессрочная, общая и уходит один раз
    (`record_demo_invite` держит по одной на контакт).

    Почему это решается здесь, а не промптом. Правило туда добавлено, и на
    словах модель его принимает, но на живом ходу @Anrri21 не взяла три раза
    подряд: перевешивало соседнее «free_test_access только когда человек
    просит». Уговаривать текстом дальше — значит держать выдачу ссылки на
    усмотрение модели там, где решение полностью механическое.

    Само письмо всё равно собирает `record_demo_invite`, и все его отказы
    остаются в силе: молчаливый ход, чужой канал, уже выданная персональная
    ссылка, уже собранное демо. Здесь только повод, а не право.
    """
    сфера = str(decision.get("client_sector_text") or "").strip()
    if not сфера:
        # Сферу мог подтвердить и текущий ход через collected_fields_update:
        # `client_sector_text` появился позже и на старых решениях пуст.
        собрано = decision.get("collected_fields_update")
        if isinstance(собрано, dict):
            сфера = str(собрано.get("sector") or "").strip()
    if not сфера:
        return False
    # Сфера с готовой группой идёт своим путём — там настоящая выдача, и она
    # спрашивает согласия. Демо ей не положено.
    выбранная = str(decision.get("matched_direct_invite_sector_id") or "").strip()
    if выбранная:
        return False
    branch = branch_config or direct_invite.BranchConfig.from_env()
    return branch.demo_route_ready()


def manager_card_reason(verdict: str, decision: dict) -> str:
    """Зачем этому разговору живой человек. Пустая строка — незачем.

    До 06.08 карточку заводил сам вердикт: любой `reply_and_handoff` звал
    менеджера. Но вердикт говорит «этот ход требует человека», а не «какого
    именно» — и в него одинаково попадали просьба о договоре и согласие на
    бесплатный тест. Второе человеку не нужно: тест выдаёт автоматика.

    Поводов ровно три, и все три — про то, что автоматике тут делать нечего:

    * человек прямо попросил живого — менеджера, созвон, счёт, договор,
      индивидуальные условия. Это `handoff_kind=manager_action`;
    * движок не понял разговор (`UNCLEAR_DECISIONS`);
    * сработал детерминированный префильтр до модели — сегодня это резкий
      отказ или жалоба. Отличить его можно по отсутствию `handoff_kind`:
      префильтр этот ключ не кладёт вовсе, а движок обязан вернуть одно из
      трёх известных значений, иначе ход валится в `technical_failure`.
      Владелец о жалобах не говорил, но терять их нельзя: лишняя карточка
      стоит минуты внимания, потерянная жалоба — отношений с человеком.
    """
    if verdict in UNCLEAR_DECISIONS:
        return "движок не понял разговор"
    if verdict not in HANDOFF_DECISIONS:
        return ""
    kind = str(decision.get("handoff_kind") or "").strip().lower()
    if kind == direct_invite.MANAGER_HANDOFF_KIND:
        return "человек просит живого"
    if not kind and decision.get("handoff_required"):
        return "резкий отказ или жалоба"
    return ""


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
    kind = str(decision.get("handoff_kind") or "")
    if kind == direct_invite.MANAGER_HANDOFF_KIND:
        marks.append("обещание менеджера")
    elif kind == direct_invite.CONSENT_HANDOFF_KIND:
        # Согласие на тест раньше помечалось «обещанием менеджера» — теперь
        # это неправда: его закрывает автоматика. Пометку не убираем совсем,
        # но называем честно: перечитать такой ответ стоит, потому что в нём
        # выдаётся доступ.
        marks.append("выдача доступа")
    elif kind not in ("", "none"):
        marks.append(f"неизвестный вид handoff: {kind}")
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
        # Демо-бот: сфера подтверждена, готовой тестовой группы под неё нет.
        # Непустая означает, что письмо со ссылкой на демо-бота уже собрано и
        # заменило собой ответ движка — и что карточку заводить не нужно.
        "demo": "",
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
    conflict = ""
    if direct_invite.consent_from_decision(decision) or demo_route_applies(
        decision, branch_config=branch_config
    ):
        branch = branch_config or direct_invite.BranchConfig.from_env()
        # Считаем до выдачи: гейт специализации отказывает молча внутри
        # `record_consent`, и снаружи его отказ неотличим от «сфера не подошла».
        # Разница принципиальная: он срабатывает как раз тогда, когда у сферы
        # готовая группа есть, просто не та, что выбрала модель.
        conflict = direct_invite.specialization_conflict(
            branch, decision=decision, inbound=inbound)
        # Согласия может и не быть: сюда же входит ход, где человек просто
        # назвал свою сферу, а готовой группы под неё нет. Записывать согласие,
        # которого он не давал, нельзя — заявка на выдачу означала бы, что он
        # просил доступ. Ему полагается только общая ссылка ниже.
        recorded = direct_invite.record_consent(
            store,
            config=branch,
            thread=thread,
            inbound=inbound,
            account_role=account_role_for(store, inbound),
            sector_id=direct_invite.sector_from_decision(decision),
        ) if direct_invite.consent_from_decision(decision) else None
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
        elif not conflict and reply_text and (
            verdict not in SILENT_DECISIONS or verdict == "ignore"
        ):
            # Согласие есть, а готовой тестовой группы под эту сферу нет —
            # либо сферу вообще не опознали. Человек идёт в общий демо-бот и
            # там сам решает, нужны ли ему примеры под своё направление и
            # менеджер. Карточку в этой ветке не заводим намеренно: разговор
            # продолжается внутри бота.
            #
            # Письмо уезжает тем же одним сообщением, что и ответ движка, по
            # тем же соображениям, что и ссылка StartBot: отдельная задача
            # второго письма не даёт, её снимает `supersede` ответа.
            #
            # Но именно ДОПИСЫВАЕТСЯ, а не заменяет. Замена стоила ровно того,
            # ради чего ветка и писалась: на ходу, где человек задал четыре
            # предметных вопроса про покрытие, площадки и цену, его ответ
            # вытеснялся бы общим письмом про демо-бота. Сфера без готовой
            # группы не повод не отвечать — повод не обещать тест.
            #
            # Условие выше не формальность: войти сюда на ходу, где движок
            # выбрал молчание, значило бы заговорить там, где решено было
            # промолчать. А раз карточку эта ветка не заводит, человек остался
            # бы вообще без продолжения.
            demo = direct_invite.record_demo_invite(
                store,
                config=branch_config or direct_invite.BranchConfig.from_env(),
                thread=thread,
                inbound=inbound,
                account_role=account_role_for(store, inbound),
                canonical_sector_id=(
                    direct_invite.canonical_sector_from_decision(decision)
                ),
                actor=actor,
            )
            if demo is not None:
                reply_text = reply_text.rstrip() + "\n\n" + str(demo["text"])
                result["demo"] = str(demo["id"])

    if not result["invite"] and not result["demo"]:
        why = manager_card_reason(verdict, decision) or conflict
        if not why and direct_invite.consent_from_decision(decision):
            # Согласие было, а автоматика не выдала ничего. Различить надо два
            # случая: у человека уже есть ссылка — тогда беспокоить менеджера
            # незачем; или выдать не удалось, и тогда без человека нельзя:
            # он согласился и не получил ни ссылки, ни менеджера.
            why = direct_invite.consent_left_unserved(
                branch_config or direct_invite.BranchConfig.from_env(),
                store=store,
                thread=thread,
                account_role=account_role_for(store, inbound),
                surface=str(inbound.get("surface") or ""),
            )
        if why:
            # Заметку движка и повод карточки складываем: первая объясняет
            # разговор, второй — почему разговор здесь оказался. Менеджеру,
            # который видит только карточку, нужны обе половины.
            note = "; ".join(filter(None, [
                str(decision.get("knowledge_gap")
                    or decision.get("reason") or ""),
                why,
            ]))
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
                # Отвечаем на то сообщение, по которому собран текст, а не на
                # последнее в треде: пока модель думала, могло прийти новое.
                inbound_id=int(inbound["id"]),
            )
        except Exception:
            # Ссылка уже выпущена, но письма с ней не будет. Возвращаем заявку
            # в очередь, иначе она осталась бы «выпущенной» без доставки —
            # человек согласился, ссылка существует, и никто её не везёт.
            if result.get("invite_inline"):
                direct_invite.release_inline(
                    store, str(result["invite_inline"]),
                    "ответ не поставлен в очередь", actor=actor)
            if result.get("demo"):
                direct_invite.cancel_demo_invite(
                    store, str(result["demo"]),
                    "ответ не поставлен в очередь", actor=actor)
            raise
        result["task"] = queued["task"]
        result["sent_text"] = reply_text
        result["review_reason"] = reason
        if result.get("invite_inline"):
            direct_invite.attach_delivery(
                store, str(result["invite_inline"]), str(queued["task"]),
                actor=actor)
        if result.get("demo"):
            direct_invite.attach_demo_delivery(
                store, str(result["demo"]), str(queued["task"]), actor=actor)
    else:
        # Письма не будет вовсе.
        if result.get("invite_inline"):
            # Ссылку вернём отдельному проходу.
            direct_invite.release_inline(
                store, str(result["invite_inline"]),
                "ответ не отправляется на этом ходу", actor=actor)
        if result.get("demo"):
            # А демо снимаем совсем: терять нечего, ссылка общая, и следующий
            # ход соберёт письмо заново.
            direct_invite.cancel_demo_invite(
                store, str(result["demo"]),
                "ответ не отправляется на этом ходу", actor=actor)
            result["demo"] = ""

    store.log(actor, f"autoreply.{verdict}", thread["id"],
              f"задача={result['task'] or '—'} карточка={result['handoff'] or '—'} "
              f"тест={result['invite'] or '—'} демо={result['demo'] or '—'}")
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

    handled = queued = failed = invited = skipped = demoed = deferred = 0
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
        # Атомарности у разбора нет, и сделать её здесь нельзя: `handle`
        # коммитит по дороге (постановка ответа, заявка на ссылку), а коммит в
        # SQLite уничтожает точку сохранения. Обернуть проход savepoint'ом
        # значит получить видимость гарантии вместо гарантии — «no such
        # savepoint» на первом же сбое.
        #
        # Наблюдавшийся симптом закрыт иначе: при провале карточка
        # переписывается на `autoreply_failed`, поэтому она больше не остаётся
        # с причиной штатного решения, которого не случилось. Настоящая
        # атомарность потребовала бы убрать промежуточные коммиты из всей
        # цепочки — это отдельная работа, и она стоит дороже, чем даёт.
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
            if result.get("demo"):
                demoed += 1
        except replies.ReplyPending as exc:
            # Предыдущее письмо со ссылкой ещё лежит планом. Снимать его
            # нельзя, а вопрос человека терять незачем: оставляем входящее
            # неразобранным и вернёмся к нему следующим тиком, через двадцать
            # секунд. Ни карточки, ни отметки о неудаче — ничего не сломалось.
            #
            # От вечного цикла страхует предел давности в `skip_reason`: если
            # письмо почему-то так и не уедет, входящее в конце концов уйдёт
            # человеку штатной карточкой, а не этой веткой.
            deferred += 1
            store.log(actor, "autoreply.deferred", str(inbound["id"]),
                      str(exc)[:200])
            store.commit()
            continue
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
              f"пропущено={skipped} отложено={deferred} тестов={invited} "
              f"демо={demoed}")
    store.commit()
    return {"enabled": True, "handled": handled, "queued": queued,
            "failed": failed, "skipped": skipped, "deferred": deferred,
            "invited": invited, "demo": demoed}
