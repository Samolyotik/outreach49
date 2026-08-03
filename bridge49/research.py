"""Разведка: что вернули чтения метаданных.

Результат read-действия приезжает в `tasks.result` цельным JSON — ровно тем,
что отдал Radar. Для хранения это правильно, для человека бесполезно: чтобы
ответить «у каких каналов есть бесплатная личка», пришлось бы разбирать json
руками по одному. Здесь три разных формы ответа сводятся к одной строке.

Формы ровно три, и они не пересекаются:

* `check_channel_dm_metadata` / `resolve_channel_dm` — плоский ответ про
  monoforum: `availability`, `channel_tg_id`, `monoforum_tg_id`;
* `check_public_chat_metadata` — `decision` + `structurally_writable` + `chat`;
* `search_public_chat` / `get_supergroup` — только `chat`.

Ключи строки английские намеренно: выгрузку должно быть можно отфильтровать и
скормить обратно в `contacts --import`, а тот читает `username`, `tg_id`,
`kind`, `peer_kind`. Русский остаётся в заголовках таблицы на экране.
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable

from .store import Store, loads

#: Действия, чей результат эта таблица умеет читать.
READ_ACTIONS = (
    "check_channel_dm_metadata",
    "check_public_chat_metadata",
    "get_supergroup",
    "resolve_channel_dm",
    "search_public_chat",
)

#: Состояния, в которых у задачи уже есть что показать. Запланированные в
#: отчёт не идут: «вердикт: planned» шесть тысяч раз — это не отчёт, а план,
#: и смотреть на него надо командой `queue`.
FINISHED = ("done", "failed", "skipped", "blocked")

#: Порядок колонок в выгрузке и на экране.
COLUMNS = (
    "username", "title", "kind", "verdict", "participants", "tg_id",
    "monoforum_tg_id", "paid_stars", "action", "state", "error", "checked_at",
)

#: Как те же колонки называются на экране.
HEADERS = {
    "username": "цель",
    "title": "название",
    "kind": "тип",
    "verdict": "вердикт",
    "participants": "людей",
    "tg_id": "tg_id",
    "monoforum_tg_id": "monoforum",
    "paid_stars": "звёзд",
    "action": "действие",
    "state": "состояние",
    "error": "ошибка",
    "checked_at": "когда",
}

#: Колонки, без которых на экране обходимся: в терминале места мало, а в файле
#: пусть будет всё.
NARROW = ("username", "title", "kind", "verdict", "participants", "error")


def _chat_kind(chat: dict) -> str | None:
    """Человеческое имя типа: Telegram различает их флагами, а не классом."""
    if not chat:
        return None
    if chat.get("bot"):
        return "бот"
    if chat.get("monoforum"):
        return "monoforum"
    if chat.get("megagroup"):
        return "супергруппа"
    if chat.get("broadcast"):
        return "канал"
    entity = str(chat.get("type") or "")
    if entity == "User":
        return "человек"
    if entity == "Chat":
        return "группа"
    return entity or None


def _verdict(action: str, state: str, result: dict, error: str | None) -> str:
    """Одно слово о том, что это чтение выяснило."""
    if state != "done":
        return "отказ" if state in ("failed", "blocked") else state
    availability = result.get("availability")
    if availability:
        return "есть личка" if availability == "available" else str(availability)
    decision = result.get("decision")
    if decision:
        return {"eligible": "можно писать", "restricted": "закрыт"}.get(
            str(decision), str(decision)
        )
    if result.get("chat"):
        return "найден"
    return "пусто" if not error else "отказ"


def _payload(result: dict) -> dict:
    """Достать возврат самого действия из конверта Radar.

    Мост кладёт результат в `details.result`, а тот — конверт: `outcome`,
    `error`, `completed_at`, корреляционные id и `data`, внутри которого и
    лежит то, что вернуло действие. Разбор верхнего уровня давал «пусто» на
    живом ответе — поймано первой же настоящей проверкой 03.08.2026.

    Голый ответ без конверта тоже принимаем: так его отдаёт сам примитив
    Radar, и на нём написаны тесты по ту сторону моста.
    """
    data = result.get("data")
    return data if isinstance(data, dict) else result


def row(task: dict) -> dict:
    """Свести одну задачу-чтение к плоской строке."""
    result = task.get("result")
    if isinstance(result, str):
        result = loads(result, {})
    result = _payload(result if isinstance(result, dict) else {})
    chat = result.get("chat")
    chat = chat if isinstance(chat, dict) else {}

    error = task.get("error_code") or task.get("error_message") or None
    monoforum = (
        result.get("monoforum_tg_id")
        or chat.get("linked_monoforum_id")
        or None
    )
    return {
        "username": (
            chat.get("username")
            or result.get("public_username")
            or task.get("target")
            or task.get("contact_id")
        ),
        "title": chat.get("title"),
        "kind": _chat_kind(chat),
        "verdict": _verdict(
            str(task.get("action") or ""), str(task.get("state") or ""),
            result, error,
        ),
        "participants": chat.get("participants_count"),
        "tg_id": chat.get("tg_id") or result.get("channel_tg_id"),
        "monoforum_tg_id": monoforum,
        "paid_stars": (
            result.get("paid_message_stars")
            if result.get("paid_message_stars") is not None
            else chat.get("paid_message_stars")
        ),
        "action": task.get("action"),
        "state": task.get("state"),
        "error": error,
        "checked_at": task.get("finished_at") or task.get("dispatched_at"),
    }


def rows(
    store: Store, *, campaign_id: str | None = None, state: str | None = None,
    limit: int = 500,
) -> list[dict]:
    """Все чтения, которые уже что-то выяснили, свежие сверху."""
    placeholders = ",".join("?" * len(READ_ACTIONS))
    sql = (
        "SELECT t.*, c.username AS target FROM tasks t "
        "LEFT JOIN contacts c ON c.id = t.contact_id "
        f"WHERE t.action IN ({placeholders}) "
    )
    params: list[Any] = list(READ_ACTIONS)
    if campaign_id:
        sql += "AND t.campaign_id = ? "
        params.append(campaign_id)
    if state:
        sql += "AND t.state = ? "
        params.append(state)
    else:
        sql += f"AND t.state IN ({','.join('?' * len(FINISHED))}) "
        params.extend(FINISHED)
    sql += "ORDER BY COALESCE(t.finished_at, t.dispatched_at) DESC, t.id LIMIT ?"
    params.append(int(limit))

    return [row(dict(record)) for record in store.query(sql, params)]


def export(records: Iterable[dict], path: Path | str) -> int:
    """Выгрузить строки в CSV. Возвращает число записанных."""
    records = list(records)
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for record in records:
            writer.writerow({key: record.get(key) for key in COLUMNS})
    return len(records)


def summary(records: Iterable[dict]) -> list[tuple[str, int]]:
    """Сколько чего выяснили — по вердиктам, от частого к редкому."""
    counts: dict[str, int] = {}
    for record in records:
        key = str(record.get("verdict") or "—")
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))
