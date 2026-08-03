"""Перенос каталога источников из tgradar-outreach в bridge49.

Прежний перенос (`import_from_tgradar_outreach.py`) взял рабочее состояние —
контакты, тексты, кампании, переписку. Каталог разведки он не трогал, и это
было верно тогда: задача стояла «продолжить переписку», а не «продолжить
поиск». Теперь нужен именно каталог.

Каталог живёт в `outreach_inventory_items`. Каждая строка — одна цель вида
``username:somechannel`` на одной из поверхностей (`channels` / `chats`), со
статусом. Нас интересует ровно один статус — `validation_pending`: это цели,
которые LLM уже одобрила, а проверка метаданных ещё не прошла. Их 5197 среди
каналов и 956 среди чатов, и это ровно та работа, ради которой существуют
аккаунты `source_reader`.

Что НЕ переносим и почему:

* `superseded` — прежние версии тех же ключей, история переносов;
* `eligible` / `ineligible` — уже проверенные, перепроверять незачем;
* `suppressed` — снятые с работы вручную или как уже отработанные;
* `tg_radar_dm` — это люди для личной рассылки, а не источники;
* `source_reader_channel_dm_queue` — legacy-очередь, из которой каталог и
  собран (`source_kind = legacy_channel_queue_bridge`). Брать обе значило бы
  завести одни и те же цели дважды.

Цели приезжают контактами: разведка в bridge49 — обычная кампания, а её
адресаты живут в `contacts`. Поверхность становится сегментом, чтобы кампания
по каналам не задела чаты.

    python3 scripts/import_sources_from_tgradar_outreach.py \\
        --source /var/lib/tgradar-outreach/production/runtime/outreach.sqlite \\
        --apply

Скрипт идемпотентен: повторный запуск обновляет те же строки. Без `--apply`
ничего не пишет и показывает, что бы приехало.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import entities  # noqa: E402
from bridge49.config import DEFAULT_HOME  # noqa: E402
from bridge49.store import Store  # noqa: E402

#: Единственный статус, который означает «ещё не проверено».
PENDING = "validation_pending"

#: Поверхность прежнего контура → как это называется у нас.
SURFACES = {
    "channels": ("channel", "recon_channels"),
    "chats": ("chat", "recon_chats"),
}

SELECT = """
SELECT surface, item_key, json_extract(payload, '$.original_source_run_id') AS run
FROM outreach_inventory_items
WHERE status = ? AND surface IN ('channels', 'chats') AND item_key LIKE 'username:%'
GROUP BY surface, item_key
ORDER BY surface, item_key
"""


def _open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def run(src: sqlite3.Connection, store: Store, *, apply: bool) -> dict:
    counts: dict[str, int] = {}
    added = updated = skipped = 0

    for row in src.execute(SELECT, (PENDING,)):
        kind, segment = SURFACES[row["surface"]]
        username = str(row["item_key"]).split(":", 1)[1].strip().lstrip("@")
        if not username:
            skipped += 1
            continue
        counts[segment] = counts.get(segment, 0) + 1
        if not apply:
            continue

        existing = store.one(
            "SELECT id FROM contacts WHERE lower(username) = lower(?)",
            (username,),
        )
        try:
            entities.add_contact(
                store, username=username, kind=kind, segment=segment,
                peer_kind="channel",
                note=f"каталог tgradar-outreach, прогон {row['run'] or '—'}",
                actor="import_sources",
            )
        except ValueError:
            # Имя, непохожее на telegram username, — в каталоге такое
            # встречается. Пропускаем поимённо, а не роняем весь перенос.
            skipped += 1
            counts[segment] -= 1
            continue
        if existing:
            updated += 1
        else:
            added += 1

    if apply:
        store.log("import_sources", "sources.import", "",
                  f"added={added} updated={updated} skipped={skipped}")
        store.commit()
    return {
        "по сегментам": counts,
        "заведено": added,
        "обновлено": updated,
        "пропущено": skipped,
        "применено": apply,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True, type=Path,
        help="SQLite прежней системы (открывается только на чтение)",
    )
    parser.add_argument(
        "--target", type=Path,
        default=Path(DEFAULT_HOME) / "var" / "bridge49.sqlite",
    )
    parser.add_argument("--apply", action="store_true", help="записать")
    args = parser.parse_args()

    src = _open_readonly(args.source)
    try:
        with Store(args.target) as store:
            result = run(src, store, apply=args.apply)
    finally:
        src.close()

    for key, value in result.items():
        print(f"{key}: {value}")
    if not args.apply:
        print("\nНичего не записано. Повторите с --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
