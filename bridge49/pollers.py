"""Чтение из Radar: результаты команд и входящие сообщения.

Обе операции — чистый SELECT. Их можно гонять сколько угодно часто и в любой
момент: состояние задач и диалогов пересобирается идемпотентно.
"""
from __future__ import annotations

from typing import Any

from .config import Settings
from .radar import RadarBridge
from .store import Store, dumps, loads, new_id, now

#: Ключи курсоров в таблице state.
INBOUND_CURSOR = "inbound_cursor"

#: Ответы, после которых диалогом должен заняться человек.
HANDOFF_SURFACES = ("private_dm", "channel_dm")


async def poll_results(
    store: Store, settings: Settings, *, actor: str = "cli"
) -> dict:
    """Подтянуть статусы всех задач, которые ждут ответа моста."""
    rows = store.query(
        "SELECT id, command_id FROM tasks "
        "WHERE command_id IS NOT NULL AND ("
        "state = 'queued' OR "
        "(state = 'failed' AND outcome = 'outcome_unknown') OR "
        "(state = 'failed' AND outcome IS NULL "
        " AND (result IS NULL OR result = '{}'))"
        ") ORDER BY command_id"
    )
    if not rows:
        return {"checked": 0, "updated": 0, "still_running": 0}

    by_command = {int(row["command_id"]): row["id"] for row in rows}
    async with RadarBridge(settings.dsn) as bridge:
        results = await bridge.results(sorted(by_command))

    updated = running = 0
    for record in results:
        task_id = by_command.get(int(record["id"]))
        if task_id is None:
            continue
        details = record.get("details") or {}
        if isinstance(details, str):
            details = loads(details, {})
        raw_result = details.get("result")
        result = raw_result if isinstance(raw_result, dict) else {}
        status = str(record.get("status") or "")

        if status in ("new", "processing"):
            running += 1
            retry = details.get("last_retry")
            if retry:
                store.execute(
                    "UPDATE tasks SET error_message=?, updated_at=? WHERE id=?",
                    (dumps(retry)[:500], now(), task_id),
                )
            continue

        outcome = str(result.get("outcome") or "")
        if status in ("done", "skipped", "failed") and not outcome:
            # Mature-DM delivery is persisted in two short DB transactions:
            # the adapter may expose ``done`` after the accepted Telegram
            # message is durable but before Radar appends details.result.  An
            # empty terminal snapshot is therefore not a failure; keep polling
            # until the authoritative result appears.  The query above also
            # recovers rows misclassified by older bridge49 versions.
            running += 1
            continue
        error = result.get("error") or {}
        state = {
            "done": "done" if outcome == "succeeded" else "failed",
            "skipped": "skipped",
            "failed": "failed",
        }.get(status, "failed")

        store.execute(
            "UPDATE tasks SET state=?, outcome=?, error_code=?, error_message=?, "
            "result=?, finished_at=?, updated_at=? WHERE id=?",
            (
                state,
                outcome or None,
                (error.get("code") if isinstance(error, dict) else None),
                (str(error.get("message"))[:500] if isinstance(error, dict)
                 and error.get("message") else record.get("last_error")),
                dumps(result)[:100_000],
                record.get("updated_at") or now(),
                now(),
                task_id,
            ),
        )
        _touch_thread_outbound(store, task_id, result)
        updated += 1

    store.log(actor, "poll.results", "",
              f"checked={len(results)} updated={updated} running={running}")
    store.commit()
    return {"checked": len(results), "updated": updated, "still_running": running}


def _touch_thread_outbound(store: Store, task_id: str, result: dict) -> None:
    """Успешная отправка открывает (или продлевает) диалог."""
    if str(result.get("outcome")) != "succeeded":
        return
    task = store.one("SELECT * FROM tasks WHERE id = ?", (task_id,))
    if task is None:
        return
    contact = store.one(
        "SELECT * FROM contacts WHERE id = ?", (task["contact_id"],)
    )
    if contact is None:
        return

    peer_key = _peer_key_for_contact(dict(contact))
    if peer_key is None:
        return
    _upsert_thread(
        store,
        account_id=int(task["account_id"]),
        peer_key=peer_key,
        contact_id=task["contact_id"],
        campaign_id=task["campaign_id"],
        surface="channel_dm" if task["action"] == "send_channel_dm" else "private_dm",
        outbound_at=now(),
    )


def _peer_key_for_contact(contact: dict) -> str | None:
    if contact.get("username"):
        return f"@{str(contact['username']).lower()}"
    if contact.get("tg_id"):
        return f"id:{int(contact['tg_id'])}"
    return None


def _upsert_thread(
    store: Store, *, account_id: int, peer_key: str,
    contact_id: str | None = None, campaign_id: str | None = None,
    surface: str = "private_dm", outbound_at: str | None = None,
    inbound_at: str | None = None,
) -> str:
    row = store.one(
        "SELECT * FROM threads WHERE account_id = ? AND peer_key = ?",
        (account_id, peer_key),
    )
    if row is None:
        thread_id = new_id("th")
        store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, campaign_id, "
            "surface, last_outbound_at, last_inbound_at, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (thread_id, account_id, peer_key, contact_id, campaign_id, surface,
             outbound_at, inbound_at, now(), now()),
        )
        return thread_id

    thread_id = row["id"]
    store.execute(
        "UPDATE threads SET contact_id = COALESCE(contact_id, ?), "
        "campaign_id = COALESCE(campaign_id, ?), "
        "last_outbound_at = COALESCE(?, last_outbound_at), "
        "last_inbound_at = COALESCE(?, last_inbound_at), "
        "state = CASE WHEN ? IS NOT NULL AND state = 'open' THEN 'awaiting' "
        "             ELSE state END, "
        "updated_at = ? WHERE id = ?",
        (contact_id, campaign_id, outbound_at, inbound_at, inbound_at,
         now(), thread_id),
    )
    return thread_id


async def poll_inbound(
    store: Store, settings: Settings, *, limit: int = 500, actor: str = "cli"
) -> dict:
    """Забрать новые входящие. Курсор — возрастающий system_notification.id."""
    cursor = int(store.get_state(INBOUND_CURSOR, "0") or 0)
    async with RadarBridge(settings.dsn) as bridge:
        rows = await bridge.inbound(cursor, limit)

    stored = replies = failures = 0
    for record in rows:
        details = record.get("details") or {}
        if isinstance(details, str):
            details = loads(details, {})
        message = details.get("message") or {}
        peer = details.get("peer") or {}
        correlation = details.get("correlation") or {}
        account_id = int(record["account_id"])
        surface = str(details.get("surface") or "private_dm")

        peer_username = peer.get("username")
        peer_key = (
            f"@{str(peer_username).lower()}" if peer_username
            else f"id:{int(peer.get('tg_id') or 0)}"
        )

        # Корреляционные ID приходят из конверта команды и могут принадлежать
        # чужому продюсеру — в фиде видны все команды бизнеса, не только наши.
        # Поэтому оба ID принимаем, только если такие записи есть у нас.
        contact_id = correlation.get("external_conversation_id")
        if contact_id and not store.one(
            "SELECT id FROM contacts WHERE id = ?", (contact_id,)
        ):
            contact_id = None
        campaign_id = correlation.get("external_job_id")
        if campaign_id and not store.one(
            "SELECT id FROM campaigns WHERE id = ?", (campaign_id,)
        ):
            campaign_id = None
        if contact_id is None and peer_username:
            match = store.one(
                "SELECT id FROM contacts WHERE lower(username) = lower(?)",
                (str(peer_username),),
            )
            contact_id = match["id"] if match else None

        store.execute(
            "INSERT OR IGNORE INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, peer_tg_id, sender_tg_id, tg_message_id, text, "
            "sent_at, raw, contact_id, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                int(record["id"]), account_id, surface, peer_key, peer_username,
                peer.get("tg_id"), message.get("sender_tg_id"),
                message.get("tg_message_id"), message.get("text"),
                message.get("date"), dumps(details)[:200_000], contact_id,
                record.get("created_at") or now(),
            ),
        )
        stored += 1

        # Само сообщение уже сохранено выше. Всё, что идёт дальше, — это
        # связывание с диалогом и контактом; если оно почему-то не удалось,
        # входящее не теряем и курсор всё равно двигаем, иначе один кривой
        # конверт остановил бы весь фид.
        try:
            thread_id = _upsert_thread(
                store, account_id=account_id, peer_key=peer_key,
                contact_id=contact_id, campaign_id=campaign_id,
                surface=surface, inbound_at=message.get("date") or now(),
            )
            if contact_id:
                store.execute(
                    "UPDATE contacts SET status = 'replied', updated_at = ? "
                    "WHERE id = ? AND status IN ('new', 'contacted')",
                    (now(), contact_id),
                )
            if surface in HANDOFF_SURFACES:
                replies += _ensure_handoff(
                    store, thread_id, message.get("text") or ""
                )
        except Exception as exc:  # noqa: BLE001 — фид важнее связывания
            failures += 1
            store.log(actor, "poll.inbound.link_failed", str(record["id"]),
                      str(exc)[:300])

        cursor = max(cursor, int(record["id"]))

    store.set_state(INBOUND_CURSOR, str(cursor))
    store.log(actor, "poll.inbound", "",
              f"fetched={len(rows)} stored={stored} handoffs={replies} "
              f"link_failed={failures} cursor={cursor}")
    store.commit()
    return {
        "fetched": len(rows), "stored": stored, "handoffs": replies,
        "link_failed": failures, "cursor": cursor,
    }


def _ensure_handoff(store: Store, thread_id: str, text: str) -> int:
    """Один активный handoff на диалог — второго ответа хватит и первого."""
    existing = store.one(
        "SELECT id FROM handoffs WHERE thread_id = ? AND status IN ('new','taken')",
        (thread_id,),
    )
    if existing:
        return 0
    store.execute(
        "INSERT INTO handoffs(id, thread_id, reason, status, note, "
        "created_at, updated_at) VALUES(?,?,?,'new',?,?,?)",
        (new_id("h"), thread_id, "получен ответ", text[:300], now(), now()),
    )
    store.execute(
        "UPDATE threads SET state = 'handoff', updated_at = ? WHERE id = ?",
        (now(), thread_id),
    )
    return 1


def take_handoff(store: Store, handoff_id: str, owner: str, *,
                 actor: str = "cli") -> None:
    store.execute(
        "UPDATE handoffs SET status='taken', owner=?, updated_at=? WHERE id=?",
        (owner, now(), handoff_id),
    )
    store.log(actor, "handoff.take", handoff_id, owner)
    store.commit()


def close_handoff(store: Store, handoff_id: str, note: str = "", *,
                  actor: str = "cli") -> None:
    row = store.one("SELECT thread_id FROM handoffs WHERE id = ?", (handoff_id,))
    store.execute(
        "UPDATE handoffs SET status='closed', note=COALESCE(?, note), updated_at=? "
        "WHERE id=?",
        (note or None, now(), handoff_id),
    )
    if row:
        store.execute(
            "UPDATE threads SET state='closed', updated_at=? WHERE id=?",
            (now(), row["thread_id"]),
        )
    store.log(actor, "handoff.close", handoff_id, note)
    store.commit()
