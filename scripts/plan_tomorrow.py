"""План отправок на день. Пишет файл, базу не трогает.

Раскладывает по аккаунтам и по времени то, что мы собираемся отправить завтра:
долг (ответы, застрявшие с прошлых дней) плюс новые касания из разведки.

Чего скрипт НЕ делает: не пишет в базу, не ходит в Radar, не отправляет. План
существует, чтобы его прочитать глазами и поспорить с ним, и только потом
класть в очередь — как и `plan_pending_sends.py`.

    python3 scripts/plan_tomorrow.py --date 2026-08-05 --per-account 5 \\
        --from-hour 9 --to-hour 21 --out var/plan_20260805.json

## Как раскладывается время

Время считается ОТ АККАУНТА, а не от общей ленты. Это не мелочь: в первой
версии джиттер применялся к шагу флота (~4 минуты), а внутри аккаунта слоты
всё равно расходились ровно на «шаг × число аккаунтов». Получилось 140–151
минута между сообщениями одного аккаунта — 124 одинаковых интервала из 136,
то есть та самая сетка, которой хотели избежать.

Поэтому каждый аккаунт раскладывается сам: его k сообщений делят окно на k
частей, слот встаёт в середину своей части и сдвигается на джиттер до 35%
части (при пяти сообщениях это ±50 минут). Плюс у аккаунта своя фаза, тоже
детерминированная, — иначе все начинали бы день одновременно.

Джиттер и фаза считаются хешем от даты, аккаунта и цели: повторный прогон
даёт тот же план, а не новую раскладку.

Общая лента получается сама. Плотность её не заботит: пол между отправками
разных аккаунтов (20–30 с) держит диспетчер, и слоты в одну минуту для него
не проблема.

## Чего скрипт не решает

Сколько именно писать и кому — решает человек. Скрипт показывает, сколько
целей вообще есть, и честно говорит, если их меньше, чем ёмкость флота.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone


from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge49 import outreach_texts  # noqa: E402

MSK = timezone(timedelta(hours=3))

#: Минимальный разрыв между двумя сообщениями одного аккаунта.
MIN_ACCOUNT_GAP_MIN = 45

#: Насколько своя у каждого аккаунта кромка окна: сдвиг первого и
#: последнего слота внутрь, 0–40 минут по хешу.
EDGE_MARGIN_MIN = 40

#: Доля своей части окна, на которую слот может сдвинуться в любую сторону.
#:
#: Больше половины — намеренно: при 0.5 слот в худшем случае лишь дотягивается
#: до края соседней части, и порядок сообщений внутри дня остаётся заранее
#: известным. За половиной части начинают перекрываться, соседи меняются
#: местами, и раскладка перестаёт читаться как расписание.
JITTER_RATIO = 0.75

#: Роль → чем она пишет и из какого пула берёт цели. Пул берётся из разведки:
#: канал или чат попадает сюда, только если проверка прошла успешно и мы туда
#: ещё не писали.
LANES = {
    "channel_sender": ("send_channel_dm", "recon_channels", "channel"),
    "chat_sender": ("send_public_chat_message", "recon_chats", "chat"),
}

#: Личка отдельно: её цели — живые люди из лидов бизнеса 140, и приходят они
#: файлом от `export_b140_candidates.py`, а не из нашей разведки.
DM_LANE = ("dm_sender", "send_private_dm", "лс")


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def ready_accounts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, label, role, roles FROM accounts "
        " WHERE enabled=1 AND paused=0 "
        "   AND lower(COALESCE(runtime_state,''))='running' ORDER BY id"
    ).fetchall()
    out = []
    for row in rows:
        try:
            roles = set(json.loads(row["roles"] or "[]"))
        except ValueError:
            roles = set()
        roles.add(str(row["role"] or ""))
        out.append({"id": int(row["id"]), "label": row["label"], "roles": roles})
    return out


def verified_targets(conn: sqlite3.Connection, campaign: str) -> list[dict]:
    """Цели, которые разведка проверила и которым мы ещё не писали."""
    rows = conn.execute(
        "SELECT c.id AS contact_id, c.username, c.display_name "
        "  FROM tasks t JOIN contacts c ON c.id = t.contact_id "
        "  LEFT JOIN contact_touches ct ON ct.contact_id = c.id "
        " WHERE t.campaign_id = ? AND t.outcome = 'succeeded' "
        "   AND ct.contact_id IS NULL AND COALESCE(c.opted_out,0) = 0 "
        "   AND c.username IS NOT NULL AND trim(c.username) <> '' "
        " GROUP BY c.id ORDER BY c.id",
        (campaign,),
    ).fetchall()
    return [dict(r) for r in rows]


def debt(conn: sqlite3.Connection) -> list[dict]:
    """Уже поставленные, но ещё не ушедшие отправки людям.

    Отправитель здесь не выбирается: человек писал конкретному аккаунту, и
    ответ от другого — это письмо от незнакомца, а не продолжение разговора.
    """
    rows = conn.execute(
        "SELECT t.id AS task_id, t.account_id, t.campaign_id, t.action, "
        "       t.contact_id, c.username, "
        "       datetime(t.scheduled_at,'+3 hours') AS byl_slot "
        "  FROM tasks t LEFT JOIN contacts c ON c.id = t.contact_id "
        " WHERE t.state = 'planned' "
        "   AND t.campaign_id NOT IN ('recon_channels','recon_chats') "
        " ORDER BY t.scheduled_at"
    ).fetchall()
    return [dict(r) for r in rows]


def jitter_minutes(seed: str, step_min: float, *,
                   ratio: float = JITTER_RATIO) -> float:
    """Детерминированный сдвиг слота в пределах ±ratio доли."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    unit = int.from_bytes(digest[:8], "big") / float(1 << 64)  # [0, 1)
    return (unit * 2 - 1) * ratio * step_min


def build(db: Path, *, date: str, per_account: int,
          from_hour: int, to_hour: int,
          dm_pool: list[dict] | None = None,
          jitter: float = JITTER_RATIO) -> dict:
    conn = connect(db)
    try:
        accounts = ready_accounts(conn)
        pools = {
            lane: verified_targets(conn, campaign)
            for lane, (_, campaign, _) in LANES.items()
        }
        outstanding = debt(conn)
    finally:
        conn.close()

    by_account: dict[int, list[dict]] = {}
    for item in outstanding:
        by_account.setdefault(int(item["account_id"]), []).append({
            "вид": "долг",
            "задача": item["task_id"],
            "кампания": item["campaign_id"],
            "действие": item["action"],
            "кому": item["username"] or item["contact_id"],
            "прежний слот": item["byl_slot"],
        })

    # Новые касания добираются до потолка, по кругу: аккаунты идут по очереди,
    # и каждый берёт по одной цели за виток. Иначе первый аккаунт вычерпал бы
    # пул целиком, а остальные остались бы без работы.
    dm_pool = list(dm_pool or [])
    left_over = {lane: 0 for lane in LANES}
    for lane, (action, _, kind) in LANES.items():
        eligible = [a for a in accounts if lane in a["roles"]]
        pool = list(pools[lane])
        cursor = 0
        for _ in range(per_account):
            for account in eligible:
                slots = by_account.setdefault(account["id"], [])
                if len(slots) >= per_account or cursor >= len(pool):
                    continue
                target = pool[cursor]
                cursor += 1
                body = (outreach_texts.chat_message(target["username"])
                        if kind == "chat"
                        else outreach_texts.channel_dm(target["username"]))
                problems = outreach_texts.validate(body, kind=kind)
                if problems:
                    # Кривой текст не ставим вовсе: письмо в чужой чат
                    # исправить после отправки нельзя.
                    left_over.setdefault("отбраковано", 0)
                    left_over["отбраковано"] += 1
                    continue
                slots.append({
                    "вид": kind,
                    "действие": action,
                    "кому": target["username"],
                    "contact_id": target["contact_id"],
                    "текст": body,
                })
        left_over[lane] = max(0, len(pool) - cursor)

    if dm_pool:
        lane, action, kind = DM_LANE
        eligible = [a for a in accounts if lane in a["roles"]]
        cursor = 0
        for _ in range(per_account):
            for account in eligible:
                slots = by_account.setdefault(account["id"], [])
                if len(slots) >= per_account or cursor >= len(dm_pool):
                    continue
                target = dm_pool[cursor]
                cursor += 1
                slots.append({
                    "вид": kind,
                    "действие": action,
                    "кому": target["username"],
                    "категория": target.get("категория"),
                    "повод": str(target.get("сообщение") or "")[:120],
                })
        left_over[lane] = max(0, len(dm_pool) - cursor)

    plan = [(acc, item) for acc, items in by_account.items() for item in items]
    total = len(plan)
    if not total:
        return {"дата": date, "всего": 0, "отправки": [], "остаток": left_over}

    start = datetime.fromisoformat(f"{date}T{from_hour:02d}:00:00").replace(tzinfo=MSK)
    finish = datetime.fromisoformat(f"{date}T{to_hour:02d}:00:00").replace(tzinfo=MSK)
    window_min = (finish - start).total_seconds() / 60

    out: list[dict] = []
    for acc in sorted(by_account):
        items = by_account[acc]
        if not items:
            continue
        share = window_min / len(items)
        # Своя фаза у каждого аккаунта: без неё все начинали бы день в один и
        # тот же момент своей первой доли.
        phase = jitter_minutes(f"{date}|фаза|{acc}", share, ratio=0.5)
        # Джиттер шире половины доли, поэтому соседние слоты перекрываются и
        # могут поменяться местами: порядок внутри дня перестаёт быть заранее
        # известным. Из-за этого слоты сортируются ПОСЛЕ сдвига, а не до.
        raw = sorted(
            (index + 0.5) * share + phase
            + jitter_minutes(f"{date}|{acc}|{item.get('кому')}|{index}", share,
                             ratio=jitter)
            for index, item in enumerate(items)
        )
        # У каждого аккаунта своя кромка окна. Без неё жёсткий клип сваливает
        # всё, что вышло за край, ровно на границу: в прогоне 05.08 семь
        # аккаунтов писали в 21:00 секунда в секунду — сговор виднее сетки.
        head = abs(jitter_minutes(f"{date}|голова|{acc}", EDGE_MARGIN_MIN,
                                  ratio=1.0))
        tail = abs(jitter_minutes(f"{date}|хвост|{acc}", EDGE_MARGIN_MIN,
                                  ratio=1.0))
        low, high = head, max(head + MIN_ACCOUNT_GAP_MIN, window_min - tail)
        minutes = [max(low, min(high, value)) for value in raw]
        # Проход вперёд разводит слипшиеся слоты, проход назад возвращает в
        # окно то, что вперёд из него вытолкнуло. Без второго прохода хвост
        # упирается в край и складывается там кучей: в прогоне 05.08 на 21:00
        # сваливалось семь сообщений сразу.
        for index in range(1, len(minutes)):
            minutes[index] = max(
                minutes[index], minutes[index - 1] + MIN_ACCOUNT_GAP_MIN)
        for index in range(len(minutes) - 2, -1, -1):
            minutes[index] = min(
                minutes[index], minutes[index + 1] - MIN_ACCOUNT_GAP_MIN)
        overflow = minutes[-1] - high if minutes else 0.0
        if overflow > 0:
            minutes = [value - overflow for value in minutes]
        minutes = [max(0.0, min(window_min, value)) for value in minutes]  # noqa: E501

        for item, minute in zip(items, minutes):
            moment = start + timedelta(minutes=minute)
            out.append({**item, "аккаунт": acc,
                        "слот": moment.strftime("%H:%M"),
                        "слот_utc": moment.astimezone(timezone.utc).isoformat()})

    out.sort(key=lambda r: r["слот"])
    return {"дата": date, "окно": f"{from_hour:02d}:00–{to_hour:02d}:00 МСК",
            "потолок на аккаунт": per_account, "всего": total,
            "остаток": left_over, "отправки": out}


def summarize(plan: dict) -> None:
    print(f"План на {plan['дата']}, окно {plan.get('окно','—')}, "
          f"потолок {plan.get('потолок на аккаунт')} на аккаунт")
    print(f"всего сообщений: {plan['всего']}\n")

    kinds: dict[str, int] = {}
    per_account: dict[int, int] = {}
    for row in plan["отправки"]:
        kinds[row["вид"]] = kinds.get(row["вид"], 0) + 1
        per_account[row["аккаунт"]] = per_account.get(row["аккаунт"], 0) + 1
    print("по видам:")
    for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
        print(f"  {kind:10} {count}")
    print(f"\nаккаунтов задействовано: {len(per_account)}")
    spread: dict[int, int] = {}
    for count in per_account.values():
        spread[count] = spread.get(count, 0) + 1
    for count in sorted(spread, reverse=True):
        print(f"  по {count} сообщени{'ю' if count == 1 else 'я'}: "
              f"{spread[count]} аккаунт(ов)")

    print("\nпо часам:")
    hours: dict[str, int] = {}
    for row in plan["отправки"]:
        hours[row["слот"][:2]] = hours.get(row["слот"][:2], 0) + 1
    for hour in sorted(hours):
        print(f"  {hour}:00  {'█' * hours[hour]} {hours[hour]}")

    left = plan.get("остаток") or {}
    if any(left.values()):
        print("\nцелей осталось в пуле на следующие дни:")
        for lane, count in left.items():
            print(f"  {lane:16} {count}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="var/bridge49.sqlite")
    parser.add_argument("--date", required=True)
    parser.add_argument("--per-account", type=int, default=5)
    parser.add_argument("--from-hour", type=int, default=10)
    parser.add_argument("--to-hour", type=int, default=21)
    parser.add_argument("--dm-candidates",
                        help="файл от export_b140_candidates.py")
    parser.add_argument("--jitter", type=float, default=JITTER_RATIO,
                        help="доля части окна, 0.75 по умолчанию")
    parser.add_argument("--out")
    args = parser.parse_args()

    dm_pool: list[dict] = []
    if args.dm_candidates:
        payload = json.loads(
            Path(args.dm_candidates).read_text(encoding="utf-8"))
        dm_pool = payload.get("кандидаты") or []

    plan = build(Path(args.db), date=args.date, per_account=args.per_account,
                 from_hour=args.from_hour, to_hour=args.to_hour,
                 dm_pool=dm_pool, jitter=args.jitter)
    summarize(plan)
    if args.out:
        Path(args.out).write_text(
            json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nплан записан: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
