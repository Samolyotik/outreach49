"""План отправки застрявших сообщений. Пишет файл, базу не трогает.

03.08 в очереди прежнего контура остались 46 сообщений со статусом
`review_required`: 35 автоответов людям и 11 приглашений на бесплатный тест.
Часть ссылок к тому моменту протухла, поэтому их перевыпустили — свежие лежат
отдельным файлом и склеиваются с очередью по `queue_id`.

Этот скрипт превращает два файла в один план: кто, кому, чем и когда. Плана
достаточно, чтобы его прочитать глазами и поспорить с ним, — и только потом
класть в очередь. Разделение намеренное: сорок шесть сообщений живым людям,
пролежавшие сутки, стоят одного лишнего шага.

Чего скрипт НЕ делает: не пишет в базу, не ходит в Radar, не отправляет.

    python3 scripts/plan_pending_sends.py \\
        --queue var/pending_queue_20260803.json \\
        --links var/reissued_links_20260803.json \\
        --out var/plan_pending_20260804.json

Отправитель не выбирается. Он берётся из очереди прежнего контура и меняться
не может: человек писал конкретному аккаунту, и ответ от другого — это письмо
от незнакомца, а не продолжение разговора. Поэтому дневной потолок здесь
раскладывает сообщения по дням, а не по аккаунтам: у кого накопилось восемь,
тот и будет писать три дня.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import catalog, config, direct_invite  # noqa: E402
from bridge49.store import Store  # noqa: E402

#: Сколько сообщений одному аккаунту в сутки. Меньше обычного потолка: это
#: догоняющая пачка, а не рассылка, и спешить с ней некуда.
DEFAULT_PER_ACCOUNT_DAILY = 3

#: Что каким действием отправляется.
#:
#: `reply_private_dm` здесь не годится ни одному сообщению, и это проверено:
#: он берёт адресата из входящего уведомления Radar, а у нас нет ни одного
#: входящего, которое соответствовало бы этим людям — они писали прежнему
#: контуру, в его собственный фид. Остаётся `send_private_dm`, который
#: адресуется напрямую.
ACTION_PRIVATE = "send_private_dm"
ACTION_CHANNEL = "send_channel_dm"


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


def _target(item: dict, *, allow_tg_id: bool = False) -> tuple[str, dict, str]:
    """Действие, параметры и человекочитаемый адрес одного сообщения.

    Адресуем по username и только по нему. Путь по одному `tg_id` контракт
    описывает прямо: он «использует entity cache этого же аккаунта». Кэш
    наполняется, пока аккаунт видит собеседника своими глазами, и живёт в
    сессии. Сессии переподняли заново — кэш пуст, а диалоги остались в
    прежних. Значит отправка по id не то чтобы рискованна, она просто не
    найдёт адресата, и узнаем мы об этом отказом на каждое сообщение.

    ``allow_tg_id`` оставлен для случая, когда кэш заведомо прогрет — но по
    умолчанию выключен, потому что тихий отказ хуже явного пропуска.
    """
    channel = item.get("telegram_channel_username")
    if channel:
        return ACTION_CHANNEL, {"username": str(channel)}, f"@{channel}"

    username = item.get("telegram_username")
    if username:
        return ACTION_PRIVATE, {"username": str(username)}, f"@{username}"

    user_id = item.get("telegram_user_id")
    if user_id and allow_tg_id:
        return (ACTION_PRIVATE, {"target_user_tg_id": int(user_id)},
                f"id:{int(user_id)}")
    if user_id:
        raise ValueError(
            f"только tg_id ({int(user_id)}), username нет — отправка по id "
            "упирается в пустой entity cache новой сессии"
        )

    raise ValueError("нечем адресовать: нет ни канала, ни username, ни tg_id")


#: Ссылка запуска и название сферы внутри перевыпущенного текста. Читаем их,
#: а не пересобираем: сфера и ссылка — факты чужого выпуска, и подставлять
#: вместо них свои догадки нельзя.
_DEEP_LINK_RE = re.compile(r"https://t\.me/\S+\?start=\S+")
_SECTOR_RE = re.compile(r"по направлению «([^»]+)»")


def _deep_link(text: str) -> str:
    match = _DEEP_LINK_RE.search(text or "")
    return match.group(0).rstrip(".,);") if match else ""


def _sector_name(text: str) -> str:
    match = _SECTOR_RE.search(text or "")
    return match.group(1).strip() if match else ""


def _text(item: dict, links: dict[str, dict]) -> str:
    """Текст сообщения. У приглашений он берётся из перевыпуска.

    Ссылка одноразовая и с сроком: та, что лежит в очереди, протухла ещё
    03.08. Отправить старую — значит послать человеку заведомо мёртвую
    кнопку, поэтому текст приглашения берётся только из перевыпуска.
    """
    link = links.get(item["id"])
    if item.get("kind") == "startbot_invite":
        if link is None or not link.get("text"):
            raise ValueError("приглашение без перевыпущенной ссылки")
        # Текст перевыпуска один на всех: шесть приглашений, ушедших 04.08 с
        # трёх аккаунтов, совпали байт в байт, кроме самой ссылки. Собираем
        # заново своим сборщиком — факты те же, формулировки у каждого свои.
        # Ссылку и название сферы берём из перевыпуска, а не выдумываем.
        deep_link = _deep_link(str(link["text"]))
        sector = _sector_name(str(link["text"]))
        if deep_link and sector:
            return direct_invite.render_invite_message(sector, deep_link)
        return str(link["text"])
    text = item.get("override_text")
    if not text:
        raise ValueError("нет текста")
    return str(text)


def plan(
    queue: list[dict],
    links: dict[str, dict],
    *,
    store: Store,
    settings: config.Settings,
    per_account_daily: int,
    start: datetime,
    kinds: frozenset[str] | None = None,
    allow_tg_id: bool = False,
) -> dict:
    limits = settings.limits
    tz = _tz(settings.timezone)
    interval = timedelta(seconds=int(limits.per_account_visible_interval_sec))
    account_jitter = max(0, int(limits.per_account_visible_jitter_sec or 0))
    fleet_min = max(0, int(limits.global_visible_interval_min_sec or 0))
    fleet_max = max(fleet_min, int(limits.global_visible_interval_max_sec or 0))

    # Разброс детерминированный: зерно из состава очереди, а не из часов. Один
    # и тот же вход даёт один и тот же план, иначе повторный прогон рисовал бы
    # другие времена и спорить с планом было бы не о чем.
    seed = hashlib.sha256(
        "|".join(sorted(str(i.get("id")) for i in queue)).encode("utf-8")
    ).hexdigest()
    rng = random.Random(seed)

    ready: list[dict] = []
    skipped: list[dict] = []
    used: dict[tuple[int, str], int] = {}
    cursor: dict[int, datetime] = {}
    #: Общий курсор флота. Без него у каждого аккаунта свой отсчёт от одного и
    #: того же старта, и первое письмо всех отправителей встаёт на одну и ту же
    #: секунду: три аккаунта пишут разным людям одновременно. Со стороны это
    #: один залп из одного центра, а не три человека за работой.
    fleet_next: datetime | None = None

    for item in sorted(queue, key=lambda i: str(i.get("created_at") or "")):
        if kinds is not None and item.get("kind") not in kinds:
            continue
        account_id = item.get("our_account_id")
        try:
            if not account_id:
                raise ValueError("в очереди не указан наш аккаунт")
            account = accounts_mod.get(store, int(account_id))
            if account is None:
                raise ValueError(f"аккаунта {account_id} нет в реестре")
            action, params, target = _target(item, allow_tg_id=allow_tg_id)
            ok, why = accounts_mod.usable(account, action)
            if not ok:
                raise ValueError(f"аккаунт {account_id}: {why}")
            params["text"] = _text(item, links)
            catalog.validate(
                action, params, roles=account["roles"],
                allowed_actions=account["allowed_actions"] or None,
            )
        except (ValueError, catalog.ValidationError) as exc:
            skipped.append({
                "id": item.get("id"), "kind": item.get("kind"),
                "why": str(exc),
            })
            continue

        # Слот. Потолок считается по дню самого слота: у кого накопилось
        # больше нормы, тот пишет несколько дней подряд.
        #
        # Нижних границ у слота три, и берётся самая поздняя: начало плана,
        # пауза этого аккаунта после его прошлого письма и пауза всего флота
        # после любого прошлого письма. Третья и разводит отправителей во
        # времени; окно и дневной потолок проверяются ниже уже поверх неё.
        account_id = int(account_id)
        floors = [start, cursor.get(account_id, start)]
        if fleet_next is not None:
            floors.append(fleet_next)
        slot = max(floors)
        for _ in range(30):
            local = slot.astimezone(tz)
            if (local.weekday() in limits.send_weekdays
                    and limits.send_window_start_hour <= local.hour
                    < limits.send_window_end_hour
                    and used.get((account_id, local.date().isoformat()), 0)
                    < per_account_daily):
                break
            if local.hour < limits.send_window_start_hour:
                local = local.replace(hour=limits.send_window_start_hour,
                                      minute=0, second=0, microsecond=0)
            else:
                local = (local + timedelta(days=1)).replace(
                    hour=limits.send_window_start_hour, minute=0, second=0,
                    microsecond=0)
            slot = local.astimezone(timezone.utc)
        else:
            skipped.append({
                "id": item.get("id"), "kind": item.get("kind"),
                "why": "не нашлось слота в ближайший месяц",
            })
            continue

        day = slot.astimezone(tz).date().isoformat()
        used[(account_id, day)] = used.get((account_id, day), 0) + 1
        # Разброс только вверх: диспетчер проверяет минимальную паузу на
        # выпуске, и слот, провалившийся под неё, был бы заблокирован. А ровный
        # шаг ровно в полчаса сам по себе выглядит машиной — от него и уходим.
        cursor[account_id] = (
            slot + interval + timedelta(seconds=rng.randint(0, account_jitter))
        )
        fleet_next = slot + timedelta(seconds=rng.randint(fleet_min, fleet_max))

        ready.append({
            "источник": item["id"],
            "что": ("приглашение" if item.get("kind") == "startbot_invite"
                    else "ответ"),
            "аккаунт": account_id,
            "метка": account["label"],
            "действие": action,
            "кому": target,
            "когда": slot.isoformat(timespec="seconds"),
            "день": day,
            "текст": params["text"],
            "почему лежало": item.get("review_reason"),
            "последнее входящее": item.get("last_inbound_at"),
            "params": params,
        })

    by_day: dict[str, int] = {}
    by_account: dict[str, int] = {}
    for row in ready:
        by_day[row["день"]] = by_day.get(row["день"], 0) + 1
        key = str(row["аккаунт"])
        by_account[key] = by_account.get(key, 0) + 1

    return {
        "составлен": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "потолок на аккаунт в сутки": per_account_daily,
        "всего в плане": len(ready),
        "по дням": dict(sorted(by_day.items())),
        "по аккаунтам": dict(sorted(by_account.items(), key=lambda kv: -kv[1])),
        "отброшено": skipped,
        "сообщения": ready,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", required=True, type=Path)
    parser.add_argument("--links", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--per-account-daily", type=int,
                        default=DEFAULT_PER_ACCOUNT_DAILY)
    parser.add_argument("--kind", action="append",
                        choices=("startbot_invite", "auto_reply"),
                        help="планировать только этот вид (можно повторять)")
    parser.add_argument("--allow-tg-id", action="store_true",
                        help="разрешить адресацию по tg_id — только если "
                             "entity cache аккаунта заведомо прогрет")
    args = parser.parse_args()

    payload = json.loads(args.queue.read_text(encoding="utf-8"))
    queue = payload["items"] if isinstance(payload, dict) else payload
    links: dict[str, dict] = {}
    if args.links:
        for row in json.loads(args.links.read_text(encoding="utf-8")):
            links[str(row.get("queue_id"))] = row

    settings = config.load()
    with Store(settings.db_path) as store:
        result = plan(
            queue, links, store=store, settings=settings,
            per_account_daily=int(args.per_account_daily),
            start=datetime.now(timezone.utc) + timedelta(minutes=5),
            kinds=frozenset(args.kind) if args.kind else None,
            allow_tg_id=bool(args.allow_tg_id),
        )

    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"в плане: {result['всего в плане']} из {len(queue)}")
    print(f"потолок: {result['потолок на аккаунт в сутки']} на аккаунт в сутки")
    print("по дням:", result["по дням"])
    if result["отброшено"]:
        print(f"отброшено: {len(result['отброшено'])}")
        for row in result["отброшено"]:
            print(f"  {row['id']} ({row['kind']}): {row['why']}")
    print(f"\nплан записан: {args.out}")
    print("В базу ничего не добавлено — это отдельный шаг.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
