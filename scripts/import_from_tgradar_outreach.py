"""Перенос рабочего состояния из tgradar-outreach в bridge49.

Читает SQLite прежней системы **только на чтение** и переносит то, без чего
нельзя продолжить работу: контакты, тексты, кампании, диалоги, переписку и
незакрытые задачи менеджера.

Что НЕ переносится и почему:

* ``sender_accounts``, ``device_profiles``, ``local_tdlib_*`` — это 19 её
  собственных TDLib-сессий. У нас своих сессий нет, отправляют аккаунты Radar;
* ``operation_queue`` — незавершённые операции прежнего движка. Переносить их
  нельзя: они ссылаются на её гейтвеи и её же launch authority;
* аттестации, релизные пины, lifecycle-сертификаты — охрана её сессий.

Кампании переносятся **всегда в статусе draft**, независимо от их прежнего
статуса. Импорт не должен ничего запускать: что и когда возобновлять — решает
человек.

Скрипт идемпотентен: повторный запуск обновляет те же строки, а не плодит
дубли. Идентификаторы получают префикс ``imp_`` и хвост исходного id.

    python3 scripts/import_from_tgradar_outreach.py \
        --source /var/lib/tgradar-outreach/production/runtime/outreach.sqlite \
        --target /opt/bridge49/var/bridge49.sqlite --apply
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49.store import Store, dumps  # noqa: E402

#: Аккаунт, которого у нас нет: переписка велась гейтвеями прежней системы.
LEGACY_ACCOUNT = 0


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _norm_username(value) -> str | None:
    if not value:
        return None
    username = str(value).strip().lstrip("@").strip("/")
    for prefix in ("https://t.me/", "t.me/"):
        if username.startswith(prefix):
            username = username[len(prefix):]
    return username or None


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# контакты
# ---------------------------------------------------------------------------

def import_contacts(src: sqlite3.Connection, store: Store) -> dict:
    rows = src.execute("SELECT * FROM recipients").fetchall()
    opted = {
        row["recipient_id"]
        for row in src.execute(
            "SELECT recipient_id FROM opt_outs WHERE recipient_id IS NOT NULL"
        )
    }

    mapping: dict[str, str] = {}
    added = updated = skipped = 0

    for row in rows:
        username = (
            _norm_username(row["telegram_username"])
            or _norm_username(row["telegram_channel_username"])
        )
        tg_id = _int_or_none(row["telegram_user_id"]) or _int_or_none(
            row["channel_chat_id"]
        )
        if not username and tg_id is None:
            skipped += 1
            continue

        kind = "channel" if row["telegram_channel_username"] else "user"
        contact_id = f"c_imp_{row['id']}"

        # Тот же человек мог уже появиться у нас — тогда обновляем его,
        # а не заводим второго.
        existing = store.one(
            "SELECT id FROM contacts WHERE id = ?", (contact_id,)
        )
        if existing is None and username:
            existing = store.one(
                "SELECT id FROM contacts WHERE lower(username) = lower(?)",
                (username,),
            )
        if existing is None and tg_id is not None:
            existing = store.one(
                "SELECT id FROM contacts WHERE tg_id = ?", (tg_id,)
            )

        variables = {
            key: row[key]
            for key in ("company", "role", "source", "timezone")
            if key in row.keys() and row[key]
        }
        variables["name"] = row["name"] or ""

        opted_out = bool(row["opt_out_status"]) or row["id"] in opted
        status = "closed" if opted_out else (
            "replied" if row["last_replied_at"]
            else "contacted" if row["last_contacted_at"] else "new"
        )

        fields = (
            kind, username, tg_id,
            "channel" if kind == "channel" else None,
            row["name"], row["company"], row["segment"] or "imported",
            dumps(["imported"]), status, int(opted_out),
            "перенесено из tgradar-outreach" if opted_out else None,
            dumps(variables), row["notes"], now(),
        )

        if existing:
            store.execute(
                "UPDATE contacts SET kind=?, username=?, tg_id=?, peer_kind=?, "
                "display_name=?, company=?, segment=?, tags=?, status=?, "
                "opted_out=?, opt_out_reason=?, vars=?, note=?, updated_at=? "
                "WHERE id=?",
                fields + (existing["id"],),
            )
            mapping[row["id"]] = existing["id"]
            updated += 1
        else:
            store.execute(
                "INSERT INTO contacts(kind, username, tg_id, peer_kind, "
                "display_name, company, segment, tags, status, opted_out, "
                "opt_out_reason, vars, note, updated_at, id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                fields + (contact_id, row["created_at"] if "created_at" in row.keys()
                          else now()),
            )
            mapping[row["id"]] = contact_id
            added += 1

    store.commit()
    return {"added": added, "updated": updated,
            "skipped_without_target": skipped, "mapping": mapping}


# ---------------------------------------------------------------------------
# шаблоны и кампании
# ---------------------------------------------------------------------------

def import_templates(src: sqlite3.Connection, store: Store) -> dict:
    rows = src.execute(
        "SELECT * FROM message_templates WHERE archived_at IS NULL"
    ).fetchall()
    mapping: dict[str, str] = {}
    count = 0
    for row in rows:
        body = (row["text"] or "").strip()
        if not body:
            continue
        template_id = f"t_imp_{row['id']}"
        note = f"перенесено; тип {row['type']}, риск {row['risk_level']}"
        store.execute(
            "INSERT INTO templates(id, name, body, note, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "name=excluded.name, body=excluded.body, note=excluded.note, "
            "updated_at=excluded.updated_at",
            (template_id, row["name"], body, note,
             row["created_at"] or now(), now()),
        )
        mapping[row["id"]] = template_id
        count += 1
    store.commit()
    return {"imported": count, "mapping": mapping}


def import_campaigns(
    src: sqlite3.Connection, store: Store, templates: dict[str, str]
) -> dict:
    rows = src.execute("SELECT * FROM campaigns").fetchall()
    mapping: dict[str, str] = {}
    count = 0
    for row in rows:
        campaign_id = f"cmp_imp_{row['id']}"
        template_id = templates.get(row["first_touch_template_id"])
        note = (
            f"перенесено из tgradar-outreach (там статус «{row['status']}»); "
            f"цель: {row['goal']}; оффер: {row['offer']}. "
            "Действие выставлено send_private_dm — проверьте перед запуском."
        )
        store.execute(
            "INSERT INTO campaigns(id, name, action, template_id, segment, mode, "
            "status, daily_cap, per_account_daily_cap, params, ttl_hours, note, "
            "created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,'draft',?,?,'{}',48,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "template_id=excluded.template_id, segment=excluded.segment, "
            "daily_cap=excluded.daily_cap, note=excluded.note, "
            "updated_at=excluded.updated_at",
            (campaign_id, row["name"], "send_private_dm", template_id,
             row["segment"] or "imported", "lottery",
             max(1, int(row["daily_global_cap"] or 20)), 12, note,
             row["created_at"] or now(), now()),
        )
        mapping[row["id"]] = campaign_id
        count += 1
    store.commit()
    return {"imported": count, "mapping": mapping}


# ---------------------------------------------------------------------------
# диалоги, переписка, задачи менеджера
# ---------------------------------------------------------------------------

_STATE_MAP = {
    "Queued": "open", "Sent": "awaiting", "Delivered": "awaiting",
    "Replied": "awaiting", "Handoff": "handoff", "Closed": "closed",
    "Refused": "closed", "OptedOut": "closed",
}


def import_threads(
    src: sqlite3.Connection, store: Store,
    contacts: dict[str, str], campaigns: dict[str, str],
) -> dict:
    """Схлопнуть его conversations в наши threads.

    У него диалог заводится на пару «лид × кампания», у нас — на пару
    «аккаунт × собеседник». Поэтому несколько его conversations одного и того
    же человека становятся ОДНИМ нашим диалогом: у человека он и был один.
    Состояние берём от самой свежей переписки, а карту conversation → thread
    возвращаем целиком, чтобы сообщения и задачи менеджера легли по местам.
    """
    rows = src.execute(
        "SELECT * FROM conversations ORDER BY COALESCE(last_message_at, created_at)"
    ).fetchall()
    mapping: dict[str, str] = {}
    merged = 0
    skipped = 0
    seen: set[str] = set()

    for row in rows:
        contact_id = contacts.get(row["recipient_id"])
        if contact_id is None:
            skipped += 1
            continue

        thread_id = f"th_imp_{row['recipient_id']}"
        mapping[row["id"]] = thread_id
        if thread_id in seen:
            merged += 1

        contact = store.one(
            "SELECT username, tg_id FROM contacts WHERE id = ?", (contact_id,)
        )
        peer_key = (
            f"@{str(contact['username']).lower()}" if contact and contact["username"]
            else f"id:{contact['tg_id']}" if contact and contact["tg_id"]
            else f"imp:{row['recipient_id']}"
        )
        state = _STATE_MAP.get(str(row["state"]), "open")
        if str(row["handoff_status"]) in ("pending", "taken"):
            state = "handoff"

        # Строки идут по возрастанию времени, поэтому последняя запись того же
        # человека и задаёт итоговое состояние.
        store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, campaign_id, "
            "surface, state, last_outbound_at, last_inbound_at, owner, summary, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
            "campaign_id=COALESCE(excluded.campaign_id, threads.campaign_id), "
            "last_outbound_at=COALESCE(excluded.last_outbound_at, "
            "                          threads.last_outbound_at), "
            "last_inbound_at=COALESCE(excluded.last_inbound_at, "
            "                         threads.last_inbound_at), "
            "owner=COALESCE(excluded.owner, threads.owner), "
            "summary=COALESCE(excluded.summary, threads.summary), "
            "updated_at=excluded.updated_at",
            (thread_id, LEGACY_ACCOUNT, peer_key, contact_id,
             campaigns.get(row["campaign_id"]), "private_dm", state,
             row["last_outbound_at"], row["last_inbound_at"],
             row["manager_owner"], row["summary"],
             row["created_at"] or now(), now()),
        )
        seen.add(thread_id)

    store.commit()
    return {"imported": len(seen), "merged_conversations": merged,
            "skipped_without_contact": skipped, "mapping": mapping}


def import_history(
    src: sqlite3.Connection, store: Store, threads: dict[str, str]
) -> dict:
    rows = src.execute(
        "SELECT * FROM messages ORDER BY created_at"
    ).fetchall()
    count = skipped = 0
    for row in rows:
        thread_id = threads.get(row["conversation_id"])
        if thread_id is None:
            skipped += 1
            continue
        store.execute(
            "INSERT INTO history(id, thread_id, direction, author, text, sent_at, "
            "origin, created_at) VALUES(?,?,?,?,?,?,'import',?) "
            "ON CONFLICT(id) DO UPDATE SET text=excluded.text, "
            "sent_at=excluded.sent_at",
            (f"h_imp_{row['id']}", thread_id, row["direction"],
             row["sender_type"], row["text"] or "",
             row["sent_at"] or row["created_at"], row["created_at"] or now()),
        )
        count += 1
    store.commit()
    return {"imported": count, "skipped_without_thread": skipped}


def import_handoffs(
    src: sqlite3.Connection, store: Store, threads: dict[str, str]
) -> dict:
    """Перенести задачи менеджера.

    Активная задача у нас может быть только одна на диалог, а после схлопывания
    conversations на один диалог их могло прийтись несколько. Оставляем
    активной самую свежую, остальные закрываем — иначе менеджер получил бы
    несколько карточек об одном и том же человеке.
    """
    rows = src.execute(
        "SELECT * FROM handoff_tasks ORDER BY created_at DESC"
    ).fetchall()
    count = skipped = demoted = 0
    active_taken: set[str] = set()

    for row in rows:
        thread_id = threads.get(row["conversation_id"])
        if thread_id is None:
            skipped += 1
            continue

        status = {"new": "new", "taken": "taken"}.get(str(row["status"]), "closed")
        if status in ("new", "taken"):
            if thread_id in active_taken:
                status = "closed"
                demoted += 1
            else:
                active_taken.add(thread_id)

        store.execute(
            "INSERT INTO handoffs(id, thread_id, reason, status, owner, note, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET status=excluded.status, "
            "owner=excluded.owner, note=excluded.note, updated_at=excluded.updated_at",
            (f"h_task_imp_{row['id']}", thread_id, row["reason"] or "перенос",
             status, row["manager_owner"], (row["summary"] or "")[:300],
             row["created_at"] or now(), now()),
        )
        count += 1

    store.commit()
    return {"imported": count, "skipped_without_thread": skipped,
            "closed_as_duplicate": demoted}


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        default="/var/lib/tgradar-outreach/production/runtime/outreach.sqlite",
    )
    parser.add_argument("--target", default="/opt/bridge49/var/bridge49.sqlite")
    parser.add_argument("--apply", action="store_true",
                        help="без этого флага только показать, что будет перенесено")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.exists():
        print(f"нет исходной базы: {source}")
        return 1

    src = _open_readonly(source)
    try:
        counts = {
            name: src.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in ("recipients", "message_templates", "campaigns",
                         "conversations", "messages", "handoff_tasks", "opt_outs")
        }
        print("в исходной базе:")
        for name, value in counts.items():
            print(f"  {name:<20} {value}")

        if not args.apply:
            print("\nэто предпросмотр. Для переноса добавьте --apply")
            return 0

        with Store(args.target) as store:
            print("\nпереношу…")
            contacts = import_contacts(src, store)
            print(f"  контакты:  +{contacts['added']} новых, "
                  f"{contacts['updated']} обновлено, "
                  f"{contacts['skipped_without_target']} без адресата пропущено")

            templates = import_templates(src, store)
            print(f"  шаблоны:   {templates['imported']}")

            campaigns = import_campaigns(src, store, templates["mapping"])
            print(f"  кампании:  {campaigns['imported']} (все в статусе draft)")

            threads = import_threads(
                src, store, contacts["mapping"], campaigns["mapping"]
            )
            print(f"  диалоги:   {threads['imported']} "
                  f"(схлопнуто переписок: {threads['merged_conversations']}, "
                  f"без контакта пропущено: {threads['skipped_without_contact']})")

            history = import_history(src, store, threads["mapping"])
            print(f"  переписка: {history['imported']} сообщений")

            handoffs = import_handoffs(src, store, threads["mapping"])
            print(f"  менеджеру: {handoffs['imported']} задач "
                  f"(дублей закрыто: {handoffs['closed_as_duplicate']})")

            store.log("import", "import.tgradar_outreach", str(source),
                      f"contacts={contacts['added']}+{contacts['updated']} "
                      f"templates={templates['imported']} "
                      f"campaigns={campaigns['imported']} "
                      f"threads={threads['imported']} "
                      f"history={history['imported']} "
                      f"handoffs={handoffs['imported']}")
            store.commit()

        print("\nГотово. Все перенесённые кампании в статусе draft — "
              "запускать их нужно осознанно.")
        return 0
    finally:
        src.close()


if __name__ == "__main__":
    sys.exit(main())
