"""Что означает неудачная команда и что с ней делать.

Сейчас все неудачи одинаковы: задача помечается `failed`, и на этом всё.
А причины разной природы, и путать их дорого. «У этого человека закрыта личка»
означает пропустить его и идти дальше. «Telegram считает, что ты рассылаешь
спам» означает, что этому аккаунту пора замолчать, — и продолжать после такого
значит идти к бану.

## Почему таблица своя, а не перенесённая

У прежнего контура каталог ошибок на четыре сотни строк и восемь десятков
маркеров. Перенести его нельзя, и дело не в объёме: он написан про их
собственный TDLib-гейтвей, а первое же правило ловит строки вида «requires a
live tdlib_gateway runtime session». Между ними и Telegram не было никого,
поэтому «незнакомая ошибка» у них означала «Telegram сказал что-то новое», и
разумным ответом был карантин аккаунта.

У нас через то же поле приезжают исключения двух посредников. В боевой базе
это видно прямо: `ValueError` двенадцать раз (наш собственный промах) и
`ResponderAmbiguousSendOutcome` трижды (неоднозначность на стороне Radar).
Их fail-closed отправил бы аккаунты в карантин за наши же баги. Поэтому
незнакомый код у нас не значит ничего, кроме «посмотри»: он записывается и
попадает в сводку, но сам ничего не останавливает.

## Почему таблица маленькая

Radar уже держит защиту аккаунта сам. `FloodWaitError` он превращает в
`flood_wait`, возвращает команду в очередь и блокирует аккаунт на нужные
секунды. `PeerFloodError` — в `peer_flood`, помечает аккаунт и заводит
долговременный кулдаун, а следующие попытки отвечает `peer_flood_fence_active`.
Дублировать это нельзя: получились бы два разных мнения об одном аккаунте.

Наша задача уже: заметить и перестать кормить придержанный аккаунт задачами,
которые всё равно вернутся отказом. Плюс отличать смертельное для адресата от
временного, чтобы не долбиться в несуществующий username вечно.

## Пока только наблюдение

Решения исполняются, только если рядом лежит файл-рубильник. Без него они
записываются в журнал и видны в сводке, но ничего не останавливают: словарь
кодов может оказаться шире наблюдённого, и цена ошибки в первую неделю выше
пользы.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .store import Store, now

#: Кого касается неудача.
SCOPE_RECIPIENT = "адресат"
SCOPE_CHAT = "чат"
SCOPE_ACCOUNT = "аккаунт"
SCOPE_OURS = "наше"
SCOPE_UNKNOWN = "код незнаком"
#: Исход неизвестен: сообщение могло уйти. Отдельно от незнакомого кода —
#: это не пробел в таблице, а зафиксированный запрет что-либо предпринимать.
SCOPE_AMBIGUOUS = "исход неясен"

#: Что с ней делать.
ACTION_SKIP = "пропустить"          # адресат недоступен, идём дальше
ACTION_HOLD_ACCOUNT = "придержать"  # аккаунту пора замолчать
ACTION_NOTE = "заметить"            # ничего не делаем, но пишем

#: На сколько придерживаем аккаунт своей стороной. Число намеренно наше и не
#: выведено из кулдауна Radar: у него свой счёт и своя длительность, а два
#: мнения об одном аккаунте хуже, чем одно грубое. Наше — заведомо короче, оно
#: только не даёт кормить аккаунт задачами, пока Radar его держит.
DEFAULT_HOLD_SECONDS = 3600

#: Рубильник: без этого файла решения не исполняются.
SWITCH_FILE = "ERROR_ACTIONS"

_HOLD_KEY = "account_hold:%s"

#: `FloodWaitError` несёт секунды прямо в тексте.
_SECONDS_RE = re.compile(r"(\d+)\s*(?:sec|second|секунд)", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    code: str
    scope: str
    action: str
    why: str
    hold_seconds: int = 0

    @property
    def acts(self) -> bool:
        return self.action != ACTION_NOTE


def _v(scope: str, action: str, why: str, hold: int = 0):
    return lambda code, message: Verdict(code, scope, action, why, hold)


#: Таблица построена по кодам, которые контур видел вживую, плюс те, что Radar
#: умеет выдавать по коду (см. `outreach_queue`). Ничего сверх этого: код,
#: которого никто не наблюдал и который не выдаётся источником, — это догадка.
CATALOG: dict = {
    # -- аккаунт: единственная группа, ради которой всё и делается ----------
    "peer_flood": _v(
        SCOPE_ACCOUNT, ACTION_HOLD_ACCOUNT,
        "Telegram счёл рассылку спамом; Radar уже завёл кулдаун",
        DEFAULT_HOLD_SECONDS),
    "peer_flood_fence_active": _v(
        SCOPE_ACCOUNT, ACTION_HOLD_ACCOUNT,
        "Radar держит аккаунт после недавнего peer_flood",
        DEFAULT_HOLD_SECONDS),
    "flood_wait": _v(
        SCOPE_ACCOUNT, ACTION_HOLD_ACCOUNT,
        "Telegram попросил подождать; Radar сам вернёт команду в очередь",
        0),  # секунды берутся из текста

    # -- адресат: пропустить и идти дальше ---------------------------------
    "channel_dm_disabled": _v(
        SCOPE_RECIPIENT, ACTION_SKIP, "у канала закрыта личка"),
    "paid_messages_required": _v(
        SCOPE_RECIPIENT, ACTION_SKIP, "адресат берёт плату за сообщения"),
    "UsernameNotOccupiedError": _v(
        SCOPE_RECIPIENT, ACTION_SKIP, "такого имени в Telegram нет"),
    "public_username_not_channel": _v(
        SCOPE_RECIPIENT, ACTION_SKIP, "имя занято не каналом"),
    "join_request_pending": _v(
        SCOPE_RECIPIENT, ACTION_NOTE, "заявка на вступление ещё не одобрена"),

    # -- чат: писать сюда нельзя -------------------------------------------
    "member_cannot_send": _v(
        SCOPE_CHAT, ACTION_SKIP, "аккаунту закрыта отправка в этот чат"),
    "chat_write_forbidden": _v(
        SCOPE_CHAT, ACTION_SKIP, "Telegram запретил писать в этот чат"),
    "ForbiddenError": _v(
        SCOPE_CHAT, ACTION_SKIP, "Telegram ответил 403 на отправку"),

    # -- наше: чинить кодом, а не поведением флота --------------------------
    "invalid_inbound_reply_target": _v(
        SCOPE_OURS, ACTION_NOTE, "мы сослались не на то входящее"),
    "mature_dm_failed": _v(
        SCOPE_OURS, ACTION_NOTE, "отправка в личку не завершилась"),
    "mature_dm_not_terminal": _v(
        SCOPE_OURS, ACTION_NOTE, "исход отправки ещё не окончателен"),
    "not_sent": _v(SCOPE_OURS, ACTION_NOTE, "мост не подтвердил отправку"),

    # -- неоднозначность: трогать нельзя ------------------------------------
    #
    # Отдельная строка не ради классификации, а ради запрета. «Неизвестно, ушло
    # ли» — это не неудача: сообщение могло дойти. Любой повтор здесь означает
    # второе сообщение живому человеку.
    "ResponderAmbiguousSendOutcome": _v(
        SCOPE_AMBIGUOUS, ACTION_NOTE,
        "неизвестно, ушло ли сообщение — повторять нельзя"),
}


def classify(code: str | None, message: str | None = None) -> Verdict:
    """Вердикт по коду. Незнакомый код — это «посмотри», а не «карантин»."""
    normalized = str(code or "").strip()
    text = str(message or "")
    if not normalized:
        return Verdict("", SCOPE_UNKNOWN, ACTION_NOTE, "кода нет")

    factory = CATALOG.get(normalized)
    if factory is None:
        # Один разбор по тексту, и только он. `ValueError` приезжает и как наш
        # промах, и как неудачный resolve чужого имени — по коду они
        # неразличимы, а по смыслу это разные вещи.
        if "as username" in text or "no user has" in text.lower():
            return Verdict(normalized, SCOPE_RECIPIENT, ACTION_SKIP,
                           "имя не разрешилось в пользователя")
        return Verdict(normalized, SCOPE_UNKNOWN, ACTION_NOTE,
                       "код незнаком — посмотреть глазами")

    verdict = factory(normalized, text)
    if normalized == "flood_wait":
        found = _SECONDS_RE.search(text)
        seconds = int(found.group(1)) if found else DEFAULT_HOLD_SECONDS
        verdict = Verdict(verdict.code, verdict.scope, verdict.action,
                          verdict.why, max(60, min(24 * 3600, seconds)))
    return verdict


# ---------------------------------------------------------------------------
# удержание аккаунта
# ---------------------------------------------------------------------------


def hold_account(store: Store, account_id: int, verdict: Verdict) -> str:
    """Записать, до какого момента мы не даём этому аккаунту работу."""
    until = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, int(verdict.hold_seconds or DEFAULT_HOLD_SECONDS)))
    stamp = until.isoformat()
    store.set_state(_HOLD_KEY % int(account_id), f"{stamp}|{verdict.why}")
    return stamp


def held_until(store: Store, account_id: int) -> tuple[str, str] | None:
    """До какого момента аккаунт придержан и почему. Истёкшее не считается."""
    raw = store.get_state(_HOLD_KEY % int(account_id))
    if not raw:
        return None
    stamp, _, why = str(raw).partition("|")
    if stamp <= now():
        return None
    return stamp, why


def switch_enabled(home) -> bool:
    """Исполнять ли решения. Без файла — только наблюдение."""
    from pathlib import Path

    return (Path(home) / "var" / SWITCH_FILE).exists()


def record(
    store: Store,
    *,
    task_id: str,
    account_id: int | None,
    code: str | None,
    message: str | None,
    home,
    actor: str = "errors",
) -> Verdict:
    """Разобрать неудачу, записать вердикт и — если разрешено — исполнить.

    Запись идёт всегда, исполнение — только при рубильнике. Это разные вещи:
    наблюдение безвредно и нужно именно до того, как включать исполнение.
    """
    verdict = classify(code, message)
    acting = verdict.acts and switch_enabled(home)
    store.log(
        actor, "error.classified", task_id,
        f"{verdict.code or '—'} → {verdict.scope}/{verdict.action}"
        f"{'' if acting else ' (наблюдение)'}: {verdict.why}",
    )
    if acting and verdict.action == ACTION_HOLD_ACCOUNT and account_id:
        until = hold_account(store, int(account_id), verdict)
        store.log(actor, "account.held", str(account_id),
                  f"до {until}: {verdict.why}")
    return verdict
