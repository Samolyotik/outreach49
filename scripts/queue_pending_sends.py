"""Положить сообщения из плана в очередь. Второй шаг после `plan_pending_sends`.

План — файл, который можно прочитать и оспорить. Этот скрипт превращает
выбранные строки плана в задачи, которые диспетчер потом выпустит. Разделение
намеренное: сорок шесть писем живым людям, пролежавших от суток до трёх
недель, стоят того, чтобы между «посчитали» и «поставили» был явный шаг.

Ничего не отправляет: задача попадает в состояние `planned`, а выпускает её
`dispatch` — с обычным преflight, окном отправки и полом между сообщениями.

    # что будет поставлено
    python3 scripts/queue_pending_sends.py --plan var/plan_pending.json

    # одно конкретное, по идентификатору из очереди прежнего контура
    python3 scripts/queue_pending_sends.py --plan var/plan_pending.json \\
        --only queue_2e6281feea0f --apply

Кампании заводятся по действию: у кампании одно действие, а в плане их два.
Обе создаются активными и без сегмента — адресаты берутся из плана поимённо,
планировщик к ним не применяется.

Повторный запуск не создаёт дублей: уникальность `(кампания, контакт)` держит
индекс, и уже поставленное сообщение пропускается с отметкой.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import config, entities  # noqa: E402
from bridge49.store import Store, dumps, new_id, now  # noqa: E402

#: Кампания на каждое действие. Имя говорит, откуда это взялось.
CAMPAIGNS = {
    "send_private_dm": ("pending_replies", "Догон: ответы из очереди 03.08"),
    "send_channel_dm": ("pending_invites", "Догон: приглашения из очереди 03.08"),
}


def _ensure_campaign(store: Store, campaign_id: str, name: str,
                     action: str) -> None:
    """Служебная кампания под догон. Пишется напрямую, и вот почему.

    `add_campaign` требует шаблон для всякого действия с текстом — правильное
    правило для рассылки, где текст один на сегмент. Здесь текст свой у каждого
    сообщения: это выверенные ответы конкретным людям, а не рассылка. Тем же
    способом заведены служебные кампании ручных ответов и автоответов.
    """
    row = store.one("SELECT id FROM campaigns WHERE id = ?", (campaign_id,))
    if row is not None:
        store.execute(
            "UPDATE campaigns SET status = 'active', updated_at = ? WHERE id = ?",
            (now(), campaign_id),
        )
        return
    store.execute(
        "INSERT INTO campaigns(id, name, action, template_id, segment, mode, "
        "status, daily_cap, per_account_daily_cap, params, "
        "allow_repeat_contacts, roles, accounts, ttl_hours, note, created_at, "
        "updated_at) VALUES(?,?,?,NULL,'','immediate','active',999,99,'{}',1,"
        "'[]','[]',72,?,?,?)",
        (campaign_id, name, action,
         "догон очереди прежнего контура, план от 04.08.2026", now(), now()),
    )


def _contact_for(store: Store, message: dict) -> str:
    """Контакт под адресата. Уже известный — переиспользуется."""
    params = message["params"]
    username = params.get("username")
    tg_id = params.get("target_user_tg_id")
    kind = "channel" if message["действие"] == "send_channel_dm" else "user"
    contact = entities.add_contact(
        store,
        username=str(username) if username else None,
        tg_id=int(tg_id) if tg_id else None,
        kind=kind,
        segment="pending_backlog",
        note=f"очередь прежнего контура, {message['источник']}",
        actor="queue_pending",
    )
    return str(contact["id"])


def load(
    plan: dict, store: Store, *, apply: bool, only: set[str] | None,
    limit: int | None,
) -> dict:
    messages = sorted(plan["сообщения"], key=lambda m: m["когда"])
    if only:
        messages = [m for m in messages if m["источник"] in only]
    if limit:
        messages = messages[: int(limit)]

    queued: list[dict] = []
    already: list[dict] = []

    for message in messages:
        action = message["действие"]
        campaign_id, campaign_name = CAMPAIGNS[action]
        if apply:
            _ensure_campaign(store, campaign_id, campaign_name, action)
            contact_id = _contact_for(store, message)
            existing = store.one(
                "SELECT id, state FROM tasks WHERE campaign_id = ? AND contact_id = ?",
                (campaign_id, contact_id),
            )
            if existing is not None:
                already.append({**message, "задача": existing["id"],
                                "состояние": existing["state"]})
                continue
            scheduled = datetime.fromisoformat(message["когда"])
            expires = scheduled + timedelta(hours=72)
            task_id = new_id("task")
            store.execute(
                "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
                "action, params, mode, scheduled_at, expires_at, state, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,'planned',?,?)",
                (task_id, campaign_id, contact_id, int(message["аккаунт"]),
                 action, dumps(message["params"]), "immediate",
                 scheduled.isoformat(timespec="seconds"),
                 expires.isoformat(timespec="seconds"), now(), now()),
            )
            queued.append({**message, "задача": task_id})
        else:
            queued.append(message)

    if apply:
        store.log("queue_pending", "pending.queue", "",
                  f"queued={len(queued)} already={len(already)}")
        store.commit()

    return {"поставлено": queued, "уже было": already}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--only", action="append", metavar="SOURCE_ID",
                        help="только эти строки плана (можно повторять)")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    settings = config.load()
    with Store(settings.db_path) as store:
        result = load(
            plan, store, apply=bool(args.apply),
            only=set(args.only) if args.only else None,
            limit=args.limit,
        )

    for message in result["поставлено"]:
        print(f"{message['когда'][5:16]}  акк {message['аккаунт']} → "
              f"{message['кому']}  ({message['что']})")
        print(f"    {message['текст'][:100]}")
    for message in result["уже было"]:
        print(f"уже в очереди: {message['кому']} — задача {message['задача']} "
              f"({message['состояние']})")

    print(f"\nпоставлено: {len(result['поставлено'])}, "
          f"уже было: {len(result['уже было'])}")
    if not args.apply:
        print("Это предпросмотр. Ничего не записано — добавьте --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
