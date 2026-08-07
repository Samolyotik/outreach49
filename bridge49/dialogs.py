"""Сброс диалога: вернуть переписку в исходное состояние для повторного теста.

Автоответы проверяются только вживую — с настоящего аккаунта, настоящим
входящим, через настоящий мост. Но второй раз тот же сценарий с того же
аккаунта не проигрывается: движок помнит разговор, персональная ссылка уже
выдана, демо-письмо уже ушло, а согласие второй раз не обслуживается.
Этот модуль возвращает пару «аккаунт × собеседник» в то состояние, в каком
она была до теста.

## Отметка, а не удаление

Переписка, которую читает модель, собирается из трёх таблиц
(`autoreply.conversation_history`): перенесённая история, входящие и наши
отправленные задачи. Удалить их нельзя, и не из осторожности, а по существу:

* `tasks` — это ещё и учёт темпа. Дневной лимит аккаунта считается по строкам
  с сегодняшним `dispatched_at` (`dispatcher.sent_today`). Удаление истории
  теста вернуло бы аккаунту уже израсходованный бюджет — то есть сброс,
  сделанный ради теста, разрешил бы флоту лишние отправки живым людям.
* `history` — перенос из контура коллеги, повторить его нечем: исходная база
  живёт на его стороне.

Поэтому сброс ставит на диалоге отметку `reset_at`, а сборка переписки просто
не смотрит раньше неё. Ничего не разрушено, `dialog undo` снимает отметку и
возвращает разговор целиком.

## Что остаётся видно и почему

По умолчанию модель продолжает видеть **первое касание** — наше письмо, с
которого разговор начался. Без него следующий тест читается неправильно:
входящее приходит будто от постороннего, перечень услуг собеседника выглядит
рекламой, и вердикт уходит в `ignore/spam`. `--full` убирает и его.

Отметка не касается двух проверок нарочно:

* `we_started_it` — иначе после сброса тестовый собеседник становится
  «посторонним», и автоответ выключается совсем;
* `outreach_sector_of_thread` — сфера восстанавливается из текста первого
  касания, и прятать её значит спрашивать у человека то, что мы и так знаем.

## Команда из самого диалога

Тест идёт с телефона, и переключаться на консоль ради сброса неудобно.
Поэтому тот же сброс вызывается сообщением в диалоге — но только от
собеседника из белого списка `var/TEST_PEERS`. Без списка команда не работает
вовсе: токен, набранный живым лидом, не должен ничего стирать.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import alerts, direct_invite, outreach_texts
from .store import Store, dumps, new_id, now

#: Что считается нашим первым касанием. Ответы сюда не входят: разговор
#: начинает письмо, а не реплика в нём.
SEND_ACTIONS = ("send_private_dm", "send_channel_dm", "send_public_chat_message")

#: Значения `threads.reset_scope`.
SCOPE_KEEP_FIRST_TOUCH = "keep_first_touch"
SCOPE_FULL = "full"

#: Токен управляющей команды в диалоге.
#:
#: Нарочно нечитаемый и не начинающийся со слэша: слэш-команды человек набирает
#: случайно, а это сочетание — нет. Живой лид его не напишет, а если напишет,
#: он всё равно не в белом списке.
RESET_COMMAND = "##reset49"

#: Служебная кампания для посева. Задача не может существовать без кампании,
#: а заводить сегмент ради тестового контакта незачем.
SEED_CAMPAIGN_ID = "test_seed"
SEED_CAMPAIGN_NAME = "Посев первого касания (тесты)"


class DialogError(RuntimeError):
    """Сброс сделать нельзя, и причина требует решения человека."""


@dataclass(frozen=True)
class ResetCommand:
    """Разобранная команда из диалога."""

    full: bool


def parse_reset_command(text: str) -> ResetCommand | None:
    """Управляющая команда или обычное сообщение.

    Разбор нарочно строгий: команда — это всё сообщение целиком, а не слово
    внутри него. Иначе цитата нашей же переписки или пересланное сообщение
    сработали бы как команда.
    """
    body = str(text or "").strip().lower()
    if body == RESET_COMMAND:
        return ResetCommand(full=False)
    if body == f"{RESET_COMMAND} full":
        return ResetCommand(full=True)
    return None


def peer_allowed(inbound: dict, settings) -> bool:
    """Разрешено ли этому собеседнику управлять сбросом.

    Сверяем все три опознавателя, которые приезжают во входящем: ключ пары,
    ник и числовой id. Ник меняется, id — нет, и наоборот: у собеседника из
    личек каналов ника может не быть вовсе.
    """
    allowed = settings.test_peers
    if not allowed:
        return False
    candidates = {
        _peer_token(inbound.get("peer_key")),
        _peer_token(inbound.get("peer_username")),
        _peer_token(inbound.get("peer_tg_id")),
        _peer_token(inbound.get("sender_tg_id")),
    }
    return bool(candidates & set(allowed))


def _peer_token(value: Any) -> str:
    """Привести опознаватель к тому же виду, в каком лежит белый список."""
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("id:"):
        return raw
    if raw.lstrip("-").isdigit():
        return f"id:{raw}"
    return f"@{raw.lstrip('@')}"


# ---------------------------------------------------------------------------
# первое касание
# ---------------------------------------------------------------------------


def first_touch(store: Store, thread: dict) -> dict[str, str] | None:
    """Наше письмо, с которого начался разговор.

    Берём самую раннюю отправленную задачу с непустым текстом. Пустые
    пропускаем, а не отбрасываем поиск: разведывательные действия
    (`check_channel_dm_metadata` и родня) тоже лежат в `tasks`, текста в них
    нет, и первой по времени может оказаться как раз такая.
    """
    contact_id = str(thread.get("contact_id") or "")
    if not contact_id:
        return None
    marks = ",".join("?" * len(SEND_ACTIONS))
    for row in store.query(
        "SELECT params, dispatched_at FROM tasks "
        " WHERE contact_id = ? AND state = 'done' "
        f"   AND action IN ({marks}) "
        " ORDER BY dispatched_at",
        (contact_id, *SEND_ACTIONS),
    ):
        try:
            text = str(json.loads(row["params"] or "{}").get("text") or "")
        except (TypeError, ValueError):
            continue
        if text.strip():
            return {"text": text, "sent_at": str(row["dispatched_at"] or "")}
    return None


def seed_first_touch(
    store: Store,
    thread: dict,
    *,
    days_ago: int = 3,
    actor: str = "cli",
    at: str | None = None,
) -> dict[str, Any]:
    """Посадить в диалог наше первое письмо, которого не было.

    Нужно там, где тест идёт с аккаунта, которому мы никогда не писали: без
    первого касания автоответ считает собеседника посторонним и молчит, а
    сфера остаётся неизвестной, потому что она восстанавливается из текста
    письма.

    Текст берётся тем же генератором, что и боевой (`outreach_texts`), с тем
    же зерном — ником контакта. Поэтому `sector_of_first_touch` опознаёт его
    посимвольно, как настоящее, и сфера приезжает движку сама.

    Два решения, которые здесь важнее кода:

    * `dispatched_at` ставится **задним числом**. Дневной лимит аккаунта
      считается по сегодняшним отправкам, и письмо «сегодня» съело бы слот
      живой рассылки. Заодно это правдоподобно: человек отвечает на письмо
      трёхдневной давности.
    * `contact_touches` не пишем. Это леджер **настоящих** отправок, защита от
      второго холодного касания; подделывать его ради теста нельзя. Значит
      тестовый контакт по-прежнему может попасть в план рассылки — держите его
      в своём сегменте.
    """
    contact = store.one(
        "SELECT * FROM contacts WHERE id = ?", (thread.get("contact_id"),))
    if contact is None:
        raise DialogError("у диалога нет контакта — посеять письмо некуда")
    seed = str(contact["username"] or "").strip().lstrip("@")
    if not seed:
        raise DialogError("у контакта нет ника: текст письма собрать не из чего")

    surface = str(thread.get("surface") or "private_dm")
    if surface == "public_chat":
        text = outreach_texts.chat_message(seed)
        action = "send_public_chat_message"
    elif surface == "channel_dm":
        text = outreach_texts.channel_dm(seed)
        action = "send_channel_dm"
    else:
        # В личке людей письма пишутся под конкретного человека, и сферу из
        # них не выводят (`outreach_sector_of_thread` их не смотрит). Текст
        # берём тот же — он нужен здесь как «мы начали разговор», а не как
        # источник сферы.
        text = outreach_texts.channel_dm(seed)
        action = "send_private_dm"

    stamp = at or now()
    moment = _parse(stamp) or datetime.now(timezone.utc)
    dispatched = (moment - timedelta(days=max(0, days_ago))).isoformat(
        timespec="seconds")

    _ensure_seed_campaign(store)
    existing = store.one(
        "SELECT id FROM tasks WHERE campaign_id = ? AND contact_id = ?",
        (SEED_CAMPAIGN_ID, str(contact["id"])),
    )
    params = dumps({"text": text, "seeded_for_test": True})
    if existing is not None:
        # Повторный посев — не вторая строка, а обновление той же. Частичный
        # уникальный индекс `idx_tasks_campaign_contact` вторую и не пустит.
        task_id = str(existing["id"])
        store.execute(
            "UPDATE tasks SET action = ?, params = ?, dispatched_at = ?, "
            "  finished_at = ?, updated_at = ? WHERE id = ?",
            (action, params, dispatched, dispatched, stamp, task_id),
        )
    else:
        task_id = new_id("task")
        store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "  params, mode, scheduled_at, state, outcome, dispatched_at, "
            "  finished_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,'immediate',?,'done','seeded',?,?,?,?)",
            (task_id, SEED_CAMPAIGN_ID, str(contact["id"]),
             int(thread["account_id"]), action, params, dispatched,
             dispatched, dispatched, stamp, stamp),
        )
    store.execute(
        "UPDATE threads SET last_outbound_at = COALESCE(last_outbound_at, ?), "
        "  updated_at = ? WHERE id = ?",
        (dispatched, stamp, str(thread["id"])),
    )
    store.log(actor, "dialog.seed", str(thread["id"]),
              f"{action}, {days_ago} дн. назад, задача {task_id}")
    store.commit()
    return {"task": task_id, "action": action, "text": text,
            "dispatched_at": dispatched,
            "sector": outreach_texts.sector_of_first_touch(
                str(contact["username"] or ""), text)}


def _ensure_seed_campaign(store: Store) -> None:
    """Служебная кампания посева. Ничего не рассылает: задачи в ней сразу
    `done`, диспетчер их не видит."""
    if store.one("SELECT id FROM campaigns WHERE id = ?", (SEED_CAMPAIGN_ID,)):
        return
    store.execute(
        "INSERT INTO campaigns(id, name, action, template_id, segment, mode, "
        "status, daily_cap, per_account_daily_cap, params, "
        "allow_repeat_contacts, ttl_hours, note, created_at, updated_at) "
        "VALUES(?,?,'send_channel_dm',NULL,'','immediate','paused',"
        "0,0,'{}',1,48,?,?,?)",
        (SEED_CAMPAIGN_ID, SEED_CAMPAIGN_NAME,
         "служебная: посев первого касания для тестов", now(), now()),
    )


# ---------------------------------------------------------------------------
# сброс
# ---------------------------------------------------------------------------


def plan_reset(store: Store, thread: dict) -> dict[str, Any]:
    """Что сброс изменит. Ничего не пишет — годится для `--dry-run`."""
    contact_id = str(thread.get("contact_id") or "")
    unhandled = store.one(
        "SELECT count(*) AS n FROM inbound "
        " WHERE account_id = ? AND peer_key = ? AND handled = 0",
        (int(thread["account_id"]), str(thread["peer_key"])),
    )
    planned: list[str] = []
    in_flight: list[str] = []
    if contact_id:
        for row in store.query(
            "SELECT id, state, request_id FROM tasks "
            " WHERE contact_id = ? AND state IN ('planned','queued')",
            (contact_id,),
        ):
            # Снимаем только то, что ещё никуда не уехало. У `queued` уже есть
            # закреплённый запрос в Radar: местная отмена его не отзовёт, а
            # только соврёт про исход. Такие показываем человеку и оставляем.
            if str(row["state"]) == "planned" and not row["request_id"]:
                planned.append(str(row["id"]))
            else:
                in_flight.append(str(row["id"]))
    handoffs = [str(row["id"]) for row in store.query(
        "SELECT id FROM handoffs WHERE thread_id = ? AND status IN ('new','taken')",
        (str(thread["id"]),),
    )]
    invites = [str(row["id"]) for row in store.query(
        "SELECT id FROM direct_invites WHERE contact_id = ? AND status IN (?,?,?)",
        (contact_id, direct_invite.STATUS_AGREED, direct_invite.STATUS_CREATED,
         direct_invite.STATUS_DELIVERED),
    )] if contact_id else []
    demos = [str(row["id"]) for row in store.query(
        "SELECT id FROM demo_invites WHERE contact_id = ? AND status != ?",
        (contact_id, direct_invite.DEMO_STATUS_CANCELLED),
    )] if contact_id else []
    contact = store.one(
        "SELECT opted_out FROM contacts WHERE id = ?", (contact_id,)
    ) if contact_id else None
    return {
        "thread": str(thread["id"]),
        "unhandled_inbound": int(unhandled["n"]) if unhandled else 0,
        "cancel_tasks": planned,
        "in_flight_tasks": in_flight,
        "close_handoffs": handoffs,
        "cancel_invites": invites,
        "cancel_demos": demos,
        "opted_out": bool(contact["opted_out"]) if contact else False,
    }


def reset_thread(
    store: Store,
    thread: dict,
    *,
    full: bool = False,
    actor: str = "cli",
    at: str | None = None,
) -> dict[str, Any]:
    """Вернуть диалог в состояние «человек только что ответил на письмо».

    Возвращает то же, что и `plan_reset`, — то есть отчёт о сделанном.
    """
    plan = plan_reset(store, thread)
    stamp = at or now()
    scope = SCOPE_FULL if full else SCOPE_KEEP_FIRST_TOUCH

    store.execute(
        "UPDATE threads SET reset_at = ?, reset_scope = ?, presales_context = NULL, "
        "  summary = NULL, state = 'open', updated_at = ? WHERE id = ?",
        (stamp, scope, stamp, str(thread["id"])),
    )
    # Входящие не удаляем: отметка их и так спрячет от модели. Но неразобранные
    # надо закрыть, иначе следующий проход ответит на реплику прошлого теста.
    store.execute(
        "UPDATE inbound SET handled = 1 "
        " WHERE account_id = ? AND peer_key = ? AND handled = 0",
        (int(thread["account_id"]), str(thread["peer_key"])),
    )
    for task_id in plan["cancel_tasks"]:
        store.execute(
            "UPDATE tasks SET state = 'cancelled', error_message = ?, "
            "  updated_at = ? WHERE id = ?",
            ("снято сбросом диалога", stamp, task_id),
        )
    contact_id = str(thread.get("contact_id") or "")
    if contact_id:
        store.execute(
            "UPDATE contacts SET opted_out = 0, opt_out_reason = NULL, "
            "  status = 'contacted', updated_at = ? WHERE id = ?",
            (stamp, contact_id),
        )
    for invite_id in plan["cancel_invites"]:
        # Локальный статус, а не отзыв ссылки: выпущенная ссылка StartBot
        # живёт на его стороне, и снять её нечем. Это и есть цена теста —
        # каждый прогон по «готовой» сфере сжигает одну настоящую ссылку.
        store.execute(
            "UPDATE direct_invites SET status = ?, last_error = ?, updated_at = ? "
            " WHERE id = ?",
            (direct_invite.STATUS_CANCELLED, "сброс диалога", stamp, invite_id),
        )
    for demo_id in plan["cancel_demos"]:
        direct_invite.cancel_demo_invite(
            store, demo_id, "сброс диалога", actor=actor)
    for handoff_id in plan["close_handoffs"]:
        store.execute(
            "UPDATE handoffs SET status = 'closed', note = ?, updated_at = ? "
            " WHERE id = ?",
            ("закрыта сбросом диалога", stamp, handoff_id),
        )

    store.log(actor, "dialog.reset", str(thread["id"]),
              f"объём={scope} входящих={plan['unhandled_inbound']} "
              f"снято={len(plan['cancel_tasks'])} "
              f"в полёте={len(plan['in_flight_tasks'])} "
              f"карточек={len(plan['close_handoffs'])} "
              f"ссылок={len(plan['cancel_invites'])} "
              f"демо={len(plan['cancel_demos'])}")
    store.commit()
    return {**plan, "reset_at": stamp, "scope": scope}


def undo_reset(store: Store, thread: dict, *, actor: str = "cli") -> bool:
    """Снять отметку: разговор возвращается целиком."""
    if not thread.get("reset_at"):
        return False
    store.execute(
        "UPDATE threads SET reset_at = NULL, reset_scope = NULL, updated_at = ? "
        " WHERE id = ?",
        (now(), str(thread["id"])),
    )
    store.log(actor, "dialog.reset.undo", str(thread["id"]),
              f"была отметка {thread['reset_at']}")
    store.commit()
    return True


def announce_reset(
    summary: dict[str, Any], *, inbound: dict, store: Store,
    actor: str = "autoreply",
) -> bool:
    """Сказать в топик тревог, что диалог сброшен командой из ЛС.

    Не в сам диалог: подтверждение «сброшено» стало бы первой репликой
    следующего теста. И best-effort: доставка тревог настроена не везде
    (локально её нет вовсе), а тест из-за молчащего оповещения падать не
    должен — факт сброса и так лежит в журнале.
    """
    try:
        target = alerts.TelegramTarget.from_file()
        if target is None or not target.enabled:
            return False
        alerts.send(target, "\n".join([
            "🧪 Диалог сброшен командой из ЛС",
            f"собеседник: {inbound.get('peer_key')} "
            f"(аккаунт {inbound.get('account_id')})",
            f"объём: {summary.get('scope')}",
            f"снято: ответов {len(summary.get('cancel_tasks') or [])}, "
            f"карточек {len(summary.get('close_handoffs') or [])}, "
            f"ссылок {len(summary.get('cancel_invites') or [])}, "
            f"демо {len(summary.get('cancel_demos') or [])}",
        ]))
        return True
    except Exception as exc:  # noqa: BLE001 — оповещение не главнее сброса
        store.log(actor, "dialog.reset.announce_failed", summary.get("thread", ""),
                  f"{exc.__class__.__name__}: {exc}"[:200])
        store.commit()
        return False


def _parse(stamp: str | None) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
