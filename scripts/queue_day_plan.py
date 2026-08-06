"""Положить дневной план в очередь. Второй шаг после `plan_tomorrow.py`.

План — файл, который можно прочитать и оспорить. Здесь он превращается в
задачи, которые потом выпустит диспетчер: с обычным преflight, окном отправки,
полом между сообщениями и боевым режимом. Ничего не отправляет.

    # предпросмотр
    python3 scripts/queue_day_plan.py --plan var/plan_20260805.json

    # записать
    python3 scripts/queue_day_plan.py --plan var/plan_20260805.json --apply

## Долг обновляется, а не создаётся заново

Строки вида `долг` — это задачи, которые уже лежат в базе; в плане у них свой
`задача` и новый слот. Создать их второй раз значит отправить человеку два
одинаковых сообщения, поэтому для них делается UPDATE времени, и только пока
задача ещё `planned`. Взятую диспетчером задачу не двигаем: команда уже могла
уйти в Radar.

## Повторный запуск

Идемпотентность держится на паре «кампания + контакт»: если задача этому
адресату в этой кампании уже есть и не отменена, строка пропускается. Значит
прогон можно повторить после обрыва, не считая руками, где он остановился.

## Чего скрипт не делает

Не выбирает адресатов, не пишет тексты и не трогает темп. Всё это уже решено в
плане; здесь только запись.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge49 import config, outreach_texts  # noqa: E402
from bridge49.store import Store, dumps, new_id, now  # noqa: E402

#: Куда кладём новые касания. Кампания одна на оба действия — так уже устроено
#: в базе: 24 отправки в личку каналов и 24 в чаты лежат под этим же именем.
CAMPAIGN_ID = "topup_channels_chats"
CAMPAIGN_NAME = "Каналы и чаты: первое касание"

#: Вид строки плана → что проверять в тексте.
TEXT_KIND = {"channel": "channel", "chat": "chat"}


def ensure_campaign(store: Store) -> str:
    row = store.one("SELECT id FROM campaigns WHERE id = ?", (CAMPAIGN_ID,))
    if row is None:
        store.execute(
            "INSERT INTO campaigns(id, name, action, template_id, segment, mode, "
            "status, daily_cap, per_account_daily_cap, params, "
            "allow_repeat_contacts, ttl_hours, note, created_at, updated_at) "
            "VALUES(?,?,'send_channel_dm',NULL,'','immediate','active',"
            "999,99,'{}',0,48,?,?,?)",
            (CAMPAIGN_ID, CAMPAIGN_NAME, "первые касания из разведки",
             now(), now()),
        )
    return CAMPAIGN_ID


def existing_task(store: Store, campaign_id: str, contact_id: str) -> dict | None:
    """Есть ли у этого адресата задача в этой кампании — в любом состоянии.

    ⚠️ Состояние здесь не фильтруется намеренно. Раньше отсюда исключались
    `cancelled`, и проверка расходилась с базой: уникальность
    `(кампания, контакт)` в `idx_tasks_campaign_contact` отменённые считает.
    Достаточно было одной отменённой задачи, чтобы весь прогон упал на
    `IntegrityError` посреди списка — и ровно так он и упал на плане 07.08,
    споткнувшись об одну строку из трёхсот восьми.

    Отменённую задачу мы не воскрешаем и вторую не заводим: адресат считается
    занятым, и вернуть его в работу — отдельное осознанное действие человека.
    """
    row = store.one(
        "SELECT id, state FROM tasks WHERE campaign_id = ? AND contact_id = ? "
        " ORDER BY CASE state WHEN 'cancelled' THEN 1 ELSE 0 END LIMIT 1",
        (campaign_id, contact_id),
    )
    return dict(row) if row else None


def load(plan: dict, store: Store, *, apply: bool,
         limit: int | None = None) -> dict:
    rows = plan.get("отправки") or []
    if limit:
        rows = rows[:limit]

    campaign_id = ensure_campaign(store) if apply else CAMPAIGN_ID
    queued: list[dict] = []
    moved: list[dict] = []
    skipped: list[dict] = []
    refused: list[dict] = []

    for row in rows:
        kind = str(row.get("вид") or "")
        slot = str(row.get("слот_utc") or "")

        if kind == "долг":
            task_id = str(row.get("задача") or "")
            current = store.one(
                "SELECT id, state FROM tasks WHERE id = ?", (task_id,))
            if current is None:
                refused.append({"кому": row.get("кому"),
                                "почему": f"задачи {task_id} больше нет"})
                continue
            if str(current["state"]) != "planned":
                # Задачу уже взял диспетчер: команда могла уйти в Radar, и
                # двигать её время поздно.
                skipped.append({"кому": row.get("кому"), "задача": task_id,
                                "состояние": current["state"]})
                continue
            if apply:
                store.execute(
                    "UPDATE tasks SET scheduled_at = ?, updated_at = ? "
                    " WHERE id = ? AND state = 'planned'",
                    (slot, now(), task_id))
            moved.append({"кому": row.get("кому"), "задача": task_id,
                          "когда": row.get("слот"),
                          "аккаунт": row.get("аккаунт")})
            continue

        if kind not in TEXT_KIND:
            refused.append({"кому": row.get("кому"),
                            "почему": f"неизвестный вид «{kind}»"})
            continue

        text = str(row.get("текст") or "")
        problems = outreach_texts.validate(text, kind=TEXT_KIND[kind])
        if problems:
            # Проверка повторяется здесь намеренно: план мог быть собран
            # другой версией сборщика или поправлен руками.
            refused.append({"кому": row.get("кому"),
                            "почему": "; ".join(problems)})
            continue

        contact_id = str(row.get("contact_id") or "")
        if not contact_id:
            refused.append({"кому": row.get("кому"), "почему": "нет contact_id"})
            continue
        already = existing_task(store, campaign_id, contact_id)
        if already is not None:
            skipped.append({"кому": row.get("кому"), "задача": already["id"],
                            "состояние": already["state"]})
            continue

        task_id = new_id("task")
        if apply:
            store.execute(
                "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
                "action, params, mode, scheduled_at, expires_at, state, "
                "created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,'immediate',?,NULL,'planned',?,?)",
                (task_id, campaign_id, contact_id, int(row["аккаунт"]),
                 str(row["действие"]),
                 dumps({"username": str(row["кому"]).lstrip("@"), "text": text}),
                 slot, now(), now()))
        queued.append({"задача": task_id, "кому": row.get("кому"),
                       "аккаунт": row.get("аккаунт"), "когда": row.get("слот"),
                       "что": row.get("действие"), "текст": text})

    if apply:
        store.log("queue_day_plan", "plan.queued", str(plan.get("дата") or ""),
                  f"поставлено={len(queued)} перенесено={len(moved)} "
                  f"пропущено={len(skipped)} отказ={len(refused)}")
        store.commit()
    return {"поставлено": queued, "перенесено": moved,
            "пропущено": skipped, "отказано": refused}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    settings = config.load()
    with Store(settings.db_path) as store:
        result = load(plan, store, apply=bool(args.apply), limit=args.limit)

    for item in result["поставлено"][:6]:
        print(f"  {item['когда']}  акк {item['аккаунт']:<4} → {item['кому']}"
              f"  ({item['что']})")
        print(f"      {item['текст'].splitlines()[0][:96]}")
    if len(result["поставлено"]) > 6:
        print(f"  … и ещё {len(result['поставлено']) - 6}")

    for item in result["перенесено"]:
        print(f"  перенос: задача {item['задача']} акк {item['аккаунт']} "
              f"→ {item['когда']}")
    for item in result["пропущено"]:
        print(f"  пропущено: {item['кому']} — уже есть задача "
              f"{item['задача']} ({item['состояние']})")
    for item in result["отказано"]:
        print(f"  ОТКАЗ: {item['кому']} — {item['почему']}")

    print(f"\nпоставлено: {len(result['поставлено'])}, "
          f"перенесено: {len(result['перенесено'])}, "
          f"пропущено: {len(result['пропущено'])}, "
          f"отказано: {len(result['отказано'])}")
    if not args.apply:
        print("Это предпросмотр. Ничего не записано — добавьте --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
