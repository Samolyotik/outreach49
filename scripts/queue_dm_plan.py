"""Положить дневной план по личке в очередь. Ничего не отправляет.

Пара к `queue_day_plan.py`, но для людей, а не для каналов и чатов. Разница
не косметическая, и потому это отдельный скрипт:

* адресата ещё нет в базе. Каналы и чаты приходят из нашей разведки и лежат в
  `contacts` заранее; человек из лидов бизнеса 140 не лежит нигде, и строку
  контакта создаём здесь;
* текст у каждого свой. В чаты и в личку каналов уходит текст, собранный из
  проверенных кусков, — там достаточно проверить вид. Личное письмо написано
  под конкретное сообщение человека, и проверка у него своя, из
  `first_touch.validate_text`;
* цена ошибки выше. В чужом чате неудачное сообщение теряется в ленте, а в
  личку оно приходит лично, от незнакомого аккаунта, и остаётся в переписке.

## Что защищает от повторного письма

Три разные вещи, и ни одна не заменяет другую:

1. `contact_touches` — писал ли этому человеку кто угодно из флота и когда.
   Диспетчер проверяет это сам перед отправкой, здесь проверка ранняя, чтобы
   не ставить задачу, которая всё равно упрётся;
2. задача в этой же кампании — второй заход того же прогона;
3. `opted_out` — человек попросил не писать.

## Повторный запуск

Идемпотентность на паре «кампания + контакт»: прогон можно повторить после
обрыва, не считая руками, где он остановился. Контакт, созданный прошлым
прогоном, находится по username и переиспользуется.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge49 import config, first_touch  # noqa: E402
from bridge49.store import Store, dumps, new_id, now  # noqa: E402

#: Кампания уже заведена и активна: 12 писем по ней ушло с прежних прогонов.
CAMPAIGN_ID = "cold_dm_tgradar"
CAMPAIGN_NAME = "Холодные письма из пула TG RADAR"

#: Откуда пришли эти люди. Сегмент нужен, чтобы их потом было видно отдельно
#: от каналов и чатов разведки.
SEGMENT = "b140"


def ensure_campaign(store: Store, per_account: int) -> None:
    row = store.one("SELECT id, per_account_daily_cap FROM campaigns "
                    " WHERE id = ?", (CAMPAIGN_ID,))
    if row is None:
        store.execute(
            "INSERT INTO campaigns(id, name, action, template_id, segment, "
            "mode, status, daily_cap, per_account_daily_cap, params, "
            "allow_repeat_contacts, roles, accounts, ttl_hours, note, "
            "created_at, updated_at) "
            "VALUES(?,?,'send_private_dm',NULL,?,'immediate','active',"
            "999,?,'{}',0,'[\"dm_sender\"]','[]',48,?,?,?)",
            (CAMPAIGN_ID, CAMPAIGN_NAME, SEGMENT, per_account,
             "первые касания по лидам бизнеса 140", now(), now()))
        return
    # Потолок кампании держим равным тому, с которым реально ставим. Диспетчер
    # его не смотрит — он считает по `per_account_daily_visible`, — но
    # `planner.py` смотрит, и расхождение однажды тихо срежет очередь.
    if int(row["per_account_daily_cap"]) != per_account:
        store.execute(
            "UPDATE campaigns SET per_account_daily_cap = ?, updated_at = ? "
            " WHERE id = ?", (per_account, now(), CAMPAIGN_ID))


def find_contact(store: Store, username: str) -> dict | None:
    row = store.one(
        "SELECT id, username, opted_out FROM contacts "
        " WHERE lower(username) = lower(?)", (username,))
    return dict(row) if row else None


def create_contact(store: Store, *, username: str, display_name: str,
                   note: str) -> str:
    contact_id = new_id("contact")
    store.execute(
        "INSERT INTO contacts(id, kind, username, tg_id, peer_kind, "
        "display_name, company, segment, tags, status, opted_out, "
        "opt_out_reason, vars, note, created_at, updated_at) "
        "VALUES(?,'user',?,NULL,NULL,?,NULL,?,'[]','new',0,NULL,'{}',?,?,?)",
        (contact_id, username.lstrip("@"), display_name, SEGMENT, note,
         now(), now()))
    return contact_id


def load(plan: dict, store: Store, *, apply: bool, per_account: int,
         limit: int | None = None) -> dict:
    rows = [r for r in (plan.get("отправки") or [])
            if str(r.get("вид") or "") == "лс"]
    if limit:
        rows = rows[:limit]

    if apply:
        ensure_campaign(store, per_account)

    queued: list[dict] = []
    skipped: list[dict] = []
    refused: list[dict] = []

    for row in rows:
        username = str(row.get("кому") or "").strip().lstrip("@")
        text = str(row.get("текст") or "").strip()
        if not username:
            refused.append({"кому": row.get("кому"), "почему": "нет username"})
            continue

        problems = first_touch.validate_text(text)
        if problems:
            # Проверка повторяется здесь намеренно: план мог быть собран
            # другой версией контракта или поправлен руками.
            refused.append({"кому": username, "почему": "; ".join(problems)})
            continue

        contact = find_contact(store, username)
        if contact is not None and int(contact["opted_out"] or 0):
            refused.append({"кому": username, "почему": "человек отписался"})
            continue

        contact_id = contact["id"] if contact else None
        if contact_id is not None:
            touch = store.one(
                "SELECT last_sent_at, last_account_id FROM contact_touches "
                " WHERE contact_id = ?", (contact_id,))
            if touch is not None:
                skipped.append({"кому": username,
                                "почему": f"уже писали {touch['last_sent_at']}"})
                continue
            already = store.one(
                "SELECT id, state FROM tasks "
                " WHERE campaign_id = ? AND contact_id = ? "
                "   AND state <> 'cancelled' LIMIT 1",
                (CAMPAIGN_ID, contact_id))
            if already is not None:
                skipped.append({"кому": username,
                                "почему": f"задача {already['id']} "
                                          f"({already['state']})"})
                continue

        if apply:
            if contact_id is None:
                contact_id = create_contact(
                    store, username=username,
                    display_name=str(row.get("имя") or ""),
                    note=str(row.get("повод") or "")[:200])
            task_id = new_id("task")
            store.execute(
                "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
                "action, params, mode, scheduled_at, expires_at, state, "
                "created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,'immediate',?,NULL,'planned',?,?)",
                (task_id, CAMPAIGN_ID, contact_id, int(row["аккаунт"]),
                 str(row["действие"]),
                 dumps({"username": username, "text": text}),
                 str(row["слот_utc"]), now(), now()))
        else:
            task_id = "—"

        queued.append({"задача": task_id, "кому": username,
                       "аккаунт": row.get("аккаунт"), "когда": row.get("слот"),
                       "текст": text, "повод": row.get("повод")})

    if apply:
        store.log("queue_dm_plan", "plan.queued", str(plan.get("дата") or ""),
                  f"поставлено={len(queued)} пропущено={len(skipped)} "
                  f"отказ={len(refused)}")
        store.commit()
    return {"поставлено": queued, "пропущено": skipped, "отказано": refused}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--per-account", type=int, default=5)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    settings = config.load()
    with Store(settings.db_path) as store:
        result = load(plan, store, apply=bool(args.apply),
                      per_account=args.per_account, limit=args.limit)

    for item in result["поставлено"][:5]:
        print(f"  {item['когда']}  акк {item['аккаунт']:<4} → @{item['кому']}")
        print(f"      {item['текст'][:110]}")
    if len(result["поставлено"]) > 5:
        print(f"  … и ещё {len(result['поставлено']) - 5}")

    for item in result["пропущено"]:
        print(f"  пропущено: @{item['кому']} — {item['почему']}")
    for item in result["отказано"]:
        print(f"  ОТКАЗ: @{item['кому']} — {item['почему']}")

    print(f"\nпоставлено: {len(result['поставлено'])}, "
          f"пропущено: {len(result['пропущено'])}, "
          f"отказано: {len(result['отказано'])}")
    if not args.apply:
        print("Это предпросмотр. Ничего не записано — добавьте --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
