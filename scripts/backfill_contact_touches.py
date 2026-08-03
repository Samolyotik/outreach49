#!/usr/bin/env python3
"""Перенести прошлые касания в `contact_touches`.

Защита от повторного касания смотрит только в `contact_touches`. История,
приехавшая из прежней системы, живёт в `threads`/`history` и в статусах
контактов — то есть для этой защиты её как будто нет. Без переноса первый же
`plan` возьмёт в работу всех, кому уже писал прежний контур, и люди получат
второе «первое касание».

Источники берутся по убыванию точности:

1. исходящие в `history` — есть точные даты и аккаунт;
2. `threads.last_outbound_at` — дата есть, состава переписки нет;
3. статус контакта (`contacted`/`replied`/`handoff`/`closed`) — доказывает
   сам факт, дату приходится брать из `updated_at`.

    python3 scripts/backfill_contact_touches.py
    python3 scripts/backfill_contact_touches.py --apply
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import config  # noqa: E402
from bridge49.store import Store  # noqa: E402

#: Эти статусы контакт получает только после исходящего сообщения.
TOUCHED_STATUSES = ("contacted", "replied", "handoff", "closed")


def collect(store: Store) -> tuple[dict[str, dict], Counter]:
    """Собрать касания по контактам. Возвращает карту и статистику источников."""
    touches: dict[str, dict] = {}
    sources: Counter = Counter()

    rows = store.query(
        "SELECT t.contact_id AS contact_id, "
        "       min(h.sent_at) AS first_at, max(h.sent_at) AS last_at, "
        "       count(*) AS cnt "
        "FROM threads t "
        "JOIN history h ON h.thread_id = t.id AND h.direction = 'outbound' "
        "WHERE t.contact_id IS NOT NULL "
        "GROUP BY t.contact_id"
    )
    for row in rows:
        touches[row["contact_id"]] = {
            "first_at": row["first_at"],
            "last_at": row["last_at"],
            "count": int(row["cnt"]),
            "account_id": None,
            "source": "история",
        }
        sources["история"] += 1

    # Аккаунт берём из треда с самым поздним исходящим: именно он писал последним.
    for row in store.query(
        "SELECT t.contact_id AS contact_id, t.account_id AS account_id "
        "FROM threads t "
        "JOIN history h ON h.thread_id = t.id AND h.direction = 'outbound' "
        "WHERE t.contact_id IS NOT NULL "
        "ORDER BY h.sent_at DESC"
    ):
        entry = touches.get(row["contact_id"])
        if entry is not None and entry["account_id"] is None:
            entry["account_id"] = row["account_id"]

    for row in store.query(
        "SELECT contact_id, account_id, last_outbound_at FROM threads "
        "WHERE contact_id IS NOT NULL AND last_outbound_at IS NOT NULL"
    ):
        if row["contact_id"] in touches:
            continue
        touches[row["contact_id"]] = {
            "first_at": row["last_outbound_at"],
            "last_at": row["last_outbound_at"],
            "count": 1,
            "account_id": row["account_id"],
            "source": "тред",
        }
        sources["тред"] += 1

    placeholders = ",".join("?" * len(TOUCHED_STATUSES))
    for row in store.query(
        f"SELECT id, updated_at, created_at FROM contacts "
        f"WHERE status IN ({placeholders})",
        TOUCHED_STATUSES,
    ):
        if row["id"] in touches:
            continue
        stamp = row["updated_at"] or row["created_at"]
        touches[row["id"]] = {
            "first_at": stamp,
            "last_at": stamp,
            "count": 1,
            "account_id": None,
            "source": "статус",
        }
        sources["статус"] += 1

    return touches, sources


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--home", default=None)
    args = parser.parse_args()

    settings = config.load(args.home)
    with Store(settings.db_path) as store:
        existing = {
            row["contact_id"]
            for row in store.query("SELECT contact_id FROM contact_touches")
        }
        touches, sources = collect(store)
        fresh = {k: v for k, v in touches.items() if k not in existing}

        print(f"контактов с касанием: {len(touches)}")
        for name, count in sources.most_common():
            print(f"   {name}: {count}")
        print(f"уже отмечено: {len(existing)}")
        print(f"к записи: {len(fresh)}")

        total = store.one("SELECT count(*) AS n FROM contacts WHERE opted_out = 0")
        print(
            f"\nпосле переноса свободных контактов останется: "
            f"{int(total['n']) - len(touches)} из {int(total['n'])}"
        )

        if not args.apply:
            print("\nсухой прогон; для записи добавьте --apply")
            return 0
        if not fresh:
            print("писать нечего")
            return 0

        for contact_id, entry in fresh.items():
            store.execute(
                "INSERT INTO contact_touches(contact_id, first_sent_at, "
                "  last_sent_at, sent_count, last_account_id, last_campaign_id, "
                "  last_task_id) VALUES(?,?,?,?,?,NULL,NULL) "
                "ON CONFLICT(contact_id) DO NOTHING",
                (contact_id, entry["first_at"], entry["last_at"],
                 entry["count"], entry["account_id"]),
            )
        store.log("backfill", "contact_touches.backfill", "",
                  f"записано {len(fresh)}")
        store.commit()
        written = store.one("SELECT count(*) AS n FROM contact_touches")
        print(f"записано {len(fresh)}; всего в contact_touches: {written['n']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
