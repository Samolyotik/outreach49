"""Командная строка bridge49.

Все команды безопасны по умолчанию. Единственная, которая пишет в Radar, —
`dispatch`, и она требует одновременно файл ARMED и флаг `--confirm`.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from . import accounts as accounts_mod
from . import (alerts, autoreply, catalog, config, direct_invite, dispatcher,
               entities, planner, pollers, replies, report, research, watchdog)
from .radar import RadarBridge
from .store import Store, loads, now

ACTOR_DEFAULT = "cli"


# ---------------------------------------------------------------------------
# вспомогательное
# ---------------------------------------------------------------------------

def _settings(args, *, need_dsn: bool = False) -> config.Settings:
    return config.load(getattr(args, "home", None), need_dsn=need_dsn)


def _store(settings: config.Settings) -> Store:
    return Store(settings.db_path)


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# ---------------------------------------------------------------------------
# status / doctor
# ---------------------------------------------------------------------------

def cmd_status(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        counts = {
            row["state"]: row["n"]
            for row in store.query(
                "SELECT state, count(*) AS n FROM tasks GROUP BY state"
            )
        }
        contacts = {
            row["status"]: row["n"]
            for row in store.query(
                "SELECT status, count(*) AS n FROM contacts "
                "WHERE opted_out = 0 GROUP BY status"
            )
        }
        accounts = store.one(
            "SELECT count(*) AS total, "
            "       sum(CASE WHEN paused = 0 AND enabled = 1 THEN 1 ELSE 0 END) AS ready "
            "FROM accounts"
        )
        handoffs = store.one(
            "SELECT count(*) AS n FROM handoffs WHERE status = 'new'"
        )

        print(report.section("bridge49"))
        print(report.kv([
            ("каталог", settings.home),
            ("база", settings.db_path),
            ("боевой режим", report.good("ВКЛЮЧЁН") if settings.armed
             else "выключен (dispatch только показывает предпросмотр)"),
            ("аккаунтов", f"{accounts['ready'] or 0} готовых из {accounts['total']}"),
            ("контактов", sum(contacts.values())),
            ("задач", sum(counts.values())),
            ("ждут менеджера", handoffs["n"]),
        ]))

        if counts:
            print(report.section("задачи"))
            print(report.table(
                [{"состояние": k, "штук": v} for k, v in sorted(counts.items())]
            ))
        if contacts:
            print(report.section("контакты"))
            print(report.table(
                [{"статус": k, "штук": v} for k, v in sorted(contacts.items())]
            ))

        campaigns = store.query(
            "SELECT c.id, c.name, c.action, c.mode, c.status, "
            "  (SELECT count(*) FROM tasks t WHERE t.campaign_id = c.id) AS tasks "
            "FROM campaigns c ORDER BY c.created_at DESC LIMIT 10"
        )
        if campaigns:
            print(report.section("кампании"))
            print(report.table([dict(row) for row in campaigns]))
    return 0


def cmd_doctor(args) -> int:
    """Проверить, что всё на месте. Ни одной записи, только чтение."""
    problems = 0
    print(report.section("окружение"))
    try:
        settings = _settings(args)
        print(report.kv([
            ("BRIDGE49_HOME", settings.home),
            ("база", f"{settings.db_path} "
                     f"({'есть' if settings.db_path.exists() else 'будет создана'})"),
            ("боевой режим", "ВКЛЮЧЁН" if settings.armed else "выключен"),
        ]))
    except Exception as exc:  # noqa: BLE001
        print(report.bad(f"конфигурация: {exc}"))
        return 1

    # Темп показываем всегда: «что сейчас разрешено» должно быть видно одной
    # командой, а не вычитываться из limits.json и кода по частям.
    limits = settings.limits
    days = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

    def _days(weekdays) -> str:
        return ", ".join(days[d] for d in weekdays) or "—"

    print(report.section("темп: рассылка"))
    print(report.kv([
        ("в сутки на аккаунт", limits.per_account_daily_visible),
        ("пауза между отправками",
         f"{limits.per_account_visible_interval_sec} с "
         f"(+ до {limits.per_account_visible_jitter_sec} с разброса)"),
        ("между аккаунтами",
         f"{limits.global_visible_interval_min_sec}–"
         f"{limits.global_visible_interval_max_sec} с"),
        ("за один прогон", limits.dispatch_batch),
        ("окно", f"{limits.send_window_start_hour}:00–"
                 f"{limits.send_window_end_hour}:00 {settings.timezone}"),
        ("дни", _days(limits.send_weekdays)),
    ]))
    # Три класса темпа считаются раздельно, и увидеть их надо тоже раздельно:
    # во время поэтапного запуска разведки именно её числа и меняют.
    print(report.section("темп: ответы"))
    print(report.kv([
        ("в сутки на аккаунт", limits.reply_per_account_daily),
        ("пауза между ответами", f"{limits.reply_per_account_interval_sec} с"),
        ("между аккаунтами",
         f"{limits.reply_global_interval_min_sec}–"
         f"{limits.reply_global_interval_max_sec} с"),
        ("окно", f"{limits.reply_window_start_hour}:00–"
                 f"{limits.reply_window_end_hour}:00 {settings.timezone}"),
        ("дни", _days(limits.reply_weekdays)),
    ]))
    print(report.section("темп: разведка"))
    print(report.kv([
        ("в сутки на аккаунт", limits.read_per_account_daily),
        ("пауза между чтениями",
         f"{limits.read_per_account_interval_sec} с "
         f"(+ до {limits.read_per_account_interval_jitter_sec} с разброса)"),
        ("между аккаунтами",
         f"{limits.read_global_interval_min_sec}–"
         f"{limits.read_global_interval_max_sec} с"),
        ("окно", f"{limits.read_window_start_hour}:00–"
                 f"{limits.read_window_end_hour}:00 {settings.timezone}"),
        ("дни", _days(limits.read_weekdays)),
    ]))
    for note in settings.limits_notes:
        print(report.warn(f"limits.json зажат полом → {note}"))

    print(report.section("реквизиты моста"))
    try:
        settings = _settings(args, need_dsn=True)
        print(report.good(f"ок: {settings.dsn.describe()}"))
    except config.ConfigError as exc:
        print(report.bad(str(exc)))
        return 1

    print(report.section("связь с Radar"))
    try:
        health = asyncio.run(_health(settings))
        print(report.kv(sorted(health.items())))
        print(report.good("мост отвечает"))
    except Exception as exc:  # noqa: BLE001
        print(report.bad(f"не удалось: {exc}"))
        problems += 1

    print(report.section("реестр аккаунтов"))
    with _store(settings) as store:
        rows = accounts_mod.all_accounts(store)
        if not rows:
            print(report.warn(
                "реестр пуст — выполните: bridge49 accounts --sync accounts.json"
            ))
            problems += 1
        else:
            by_role: dict[str, int] = {}
            for account in rows:
                by_role[account["role"]] = by_role.get(account["role"], 0) + 1
            print(report.kv(sorted(by_role.items())))
            stale = [a["id"] for a in rows if not a["enabled"]]
            if stale:
                print(report.warn(f"выключены в Radar: {stale}"))
    return 1 if problems else 0


async def _health(settings: config.Settings) -> dict:
    async with RadarBridge(settings.dsn) as bridge:
        return await bridge.health()


# ---------------------------------------------------------------------------
# аккаунты и каталог действий
# ---------------------------------------------------------------------------

def cmd_accounts(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        if args.sync:
            path = Path(args.sync)
            if not path.is_absolute():
                candidate = settings.home / path
                path = candidate if candidate.exists() else path
            result = accounts_mod.sync(
                store, accounts_mod.load_snapshot(path), actor=args.actor,
                pause_new=bool(args.pause_new),
            )
            print(report.kv(sorted(result.items())))
            if result.get("paused_new"):
                print(report.warn(
                    "новые аккаунты приняты в карантине: "
                    + ", ".join(str(i) for i in result["paused_new"])
                    + ". Снимать паузу поимённо: accounts --resume ID"
                ))
            return 0
        if args.pause is not None:
            accounts_mod.pause(store, args.pause, True, actor=args.actor)
            print(f"аккаунт {args.pause} на паузе")
            return 0
        if args.resume is not None:
            accounts_mod.pause(store, args.resume, False, actor=args.actor)
            print(f"аккаунт {args.resume} снова в работе")
            return 0
        if args.resume_one:
            step = accounts_mod.resume_one(
                store, args.resume_one, actor=args.actor)
            if step is None:
                print(f"на паузе не осталось ни одного {args.resume_one}")
                return 0
            print(report.good(
                f"аккаунт {step['id']} введён в работу; "
                f"на паузе осталось {step['left']}"
            ))
            return 0

        rows = accounts_mod.all_accounts(store)
        if args.role:
            rows = [a for a in rows if a["role"] == args.role]
        if not rows:
            print("реестр пуст — bridge49 accounts --sync accounts.json")
            return 0
        print(report.table([
            {
                "id": a["id"],
                "метка": a["label"],
                "программа": a["program_code"],
                "роль": a["role"],
                "действий": len(a["allowed_actions"]),
                "вкл": a["enabled"],
                "входящие": a["publish_inbound"],
                "пауза": a["paused"],
                "состояние": a["runtime_state"],
            }
            for a in rows
        ]))
    return 0


def cmd_actions(args) -> int:
    rows = []
    for name, action in sorted(catalog.ACTIONS.items()):
        if args.role and args.role not in action.roles:
            continue
        rows.append({
            "действие": name,
            "риск": action.risk,
            "роли": sorted(action.roles) if len(action.roles) < 6 else "все",
            "текст": action.needs_text,
            "что делает": action.summary,
        })
    print(report.table(rows))
    print(
        "\nриск: read — снаружи не видно; soft — прочтение/«печатает»; "
        "\n      visible — вступление и сообщения в публичные чаты; "
        "\n      mature_dm — личное сообщение."
    )
    return 0


# ---------------------------------------------------------------------------
# контакты / шаблоны / кампании
# ---------------------------------------------------------------------------

def cmd_contacts(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        if args.import_csv:
            result = entities.import_csv(store, args.import_csv, actor=args.actor)
            print(report.kv([(k, v) for k, v in result.items() if k != "errors"]))
            for line in result["errors"]:
                print(report.warn(line))
            return 0
        if args.add:
            contact = entities.add_contact(
                store, username=args.add, display_name=args.name,
                company=args.company, segment=args.segment,
                kind=args.kind, peer_kind=args.peer_kind,
                tags=args.tag or (), actor=args.actor,
            )
            print(f"контакт {contact['id']}: @{contact['username']}")
            return 0
        if args.opt_out:
            entities.opt_out(store, args.opt_out, args.reason or "по просьбе",
                             actor=args.actor)
            print("отмечен отказ, будущие задачи сняты")
            return 0

        sql = "SELECT * FROM contacts WHERE 1=1"
        params: list = []
        if args.segment:
            sql += " AND segment = ?"
            params.append(args.segment)
        if args.status:
            sql += " AND status = ?"
            params.append(args.status)
        sql += " ORDER BY created_at LIMIT ?"
        params.append(args.limit)
        print(report.table([
            {
                "id": row["id"],
                "username": row["username"],
                "имя": row["display_name"],
                "сегмент": row["segment"],
                "статус": row["status"],
                "отказ": bool(row["opted_out"]),
            }
            for row in store.query(sql, params)
        ]))
    return 0


def cmd_templates(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        if args.add:
            body = args.body
            if body == "-":
                body = sys.stdin.read()
            elif body and Path(body).exists():
                body = Path(body).read_text(encoding="utf-8")
            template = entities.add_template(
                store, args.add, body, note=args.note, template_id=args.id,
                actor=args.actor,
            )
            fields = sorted(entities.placeholders(template["body"]))
            print(f"шаблон {template['id']}: {template['name']}")
            if fields:
                print(f"плейсхолдеры: {', '.join(fields)}")
            return 0
        if args.show:
            row = store.one("SELECT * FROM templates WHERE id = ?", (args.show,))
            if row is None:
                print(report.bad("нет такого шаблона"))
                return 1
            print(report.kv([("id", row["id"]), ("название", row["name"])]))
            print("\n" + row["body"])
            return 0
        print(report.table([
            {
                "id": row["id"],
                "название": row["name"],
                "плейсхолдеры": sorted(entities.placeholders(row["body"])),
                "длина": len(row["body"]),
            }
            for row in store.query("SELECT * FROM templates ORDER BY created_at")
        ]))
    return 0


def cmd_campaigns(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        if args.add:
            campaign = entities.add_campaign(
                store, name=args.add, action=args.action,
                template_id=args.template, segment=args.segment, mode=args.mode,
                daily_cap=args.daily_cap,
                per_account_daily_cap=args.per_account_cap,
                ttl_hours=args.ttl_hours, note=args.note, campaign_id=args.id,
                params=json.loads(args.params) if args.params else None,
                allow_repeat_contacts=bool(args.allow_repeat),
                roles=args.role or (),
                accounts=args.account or (),
                actor=args.actor,
            )
            print(f"кампания {campaign['id']}: {campaign['name']}")
            if args.allow_repeat:
                print(report.warn(
                    "кампания будет писать и тем, кому уже писали раньше"
                ))
            print("дальше: bridge49 plan " + campaign["id"])
            return 0
        if args.status_of:
            entities.set_campaign_status(
                store, args.status_of, args.set_status, actor=args.actor
            )
            print(f"кампания {args.status_of} → {args.set_status}")
            return 0
        print(report.table([
            {
                "id": row["id"],
                "название": row["name"],
                "действие": row["action"],
                "сегмент": row["segment"],
                "режим": row["mode"],
                "статус": row["status"],
                "в сутки": row["daily_cap"],
                "задач": store.one(
                    "SELECT count(*) AS n FROM tasks WHERE campaign_id = ?",
                    (row["id"],),
                )["n"],
            }
            for row in store.query("SELECT * FROM campaigns ORDER BY created_at")
        ]))
    return 0


# ---------------------------------------------------------------------------
# план / очередь / выпуск
# ---------------------------------------------------------------------------

def cmd_plan(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        try:
            result = planner.plan(
                store, args.campaign, limits=settings.limits,
                timezone_name=settings.timezone, limit=args.limit,
                actor=args.actor, dry_run=not args.apply,
            )
        except planner.PlanError as exc:
            print(report.bad(str(exc)))
            return 1

        if result["tasks"]:
            print(report.table([
                {
                    "когда": report.local_time(t["scheduled_at"], settings.timezone),
                    "аккаунт": f"{t['account_id']} {t['account_label']}",
                    "кому": t["contact"],
                    "действие": t["action"],
                    "риск": t["risk"],
                    "текст": (t["params"].get("text") or "")[:60],
                }
                for t in result["tasks"]
            ]))
        print()
        print(report.kv([
            ("запланировано", result["planned"]),
            ("пропущено", len(result["skipped"])),
            ("аккаунтов в пуле", result["pool"]),
            ("режим", "предпросмотр (--apply чтобы сохранить)"
             if result["dry_run"] else "сохранено"),
        ]))
        for item in result["skipped"][:20]:
            print(report.warn(f"{item['contact']}: {item['why']}"))
        if len(result["skipped"]) > 20:
            print(f"…и ещё {len(result['skipped']) - 20}")
    return 0


def cmd_queue(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        sql = (
            "SELECT t.*, c.username AS target, a.label AS account_label "
            "FROM tasks t "
            "LEFT JOIN contacts c ON c.id = t.contact_id "
            "LEFT JOIN accounts a ON a.id = t.account_id WHERE 1=1"
        )
        params: list = []
        if args.campaign:
            sql += " AND t.campaign_id = ?"
            params.append(args.campaign)
        if args.state:
            sql += " AND t.state = ?"
            params.append(args.state)
        sql += " ORDER BY t.scheduled_at LIMIT ?"
        params.append(args.limit)

        rows = [dict(row) for row in store.query(sql, params)]
        print(report.table([
            {
                "задача": row["id"],
                "когда": report.local_time(row["scheduled_at"], settings.timezone),
                "аккаунт": f"{row['account_id']} {row['account_label'] or ''}".strip(),
                "кому": row["target"] or row["contact_id"],
                "действие": row["action"],
                "режим": row["mode"],
                "состояние": row["state"],
                "итог": row["outcome"] or row["error_code"] or "",
            }
            for row in rows
        ]))
        print(f"\nвсего показано: {len(rows)}")
    return 0


def cmd_sources(args) -> int:
    """Что выяснила разведка: результаты чтений метаданных одной таблицей."""
    settings = _settings(args)
    with _store(settings) as store:
        records = research.rows(
            store, campaign_id=args.campaign, state=args.state,
            limit=args.limit,
        )
        if args.verdict:
            records = [r for r in records if r["verdict"] == args.verdict]

        if args.csv:
            count = research.export(records, args.csv)
            print(report.good(f"выгружено строк: {count} → {args.csv}"))
            print(report.warn(
                "Колонки username / tg_id / kind понимает `contacts --import`: "
                "отфильтруйте файл и заведите из него сегмент."
            ))
            return 0

        columns = research.COLUMNS if args.full else research.NARROW
        print(report.table(
            [{research.HEADERS[c]: r.get(c) for c in columns} for r in records]
        ))
        print()
        print(report.kv(research.summary(records)))
    return 0


def cmd_dispatch(args) -> int:
    settings = _settings(args, need_dsn=args.confirm)
    with _store(settings) as store:
        if args.unblock:
            count = dispatcher.unblock(store, args.unblock, actor=args.actor)
            print(f"возвращено в план: {count}")
            return 0
        try:
            if args.read and args.outreach:
                print(report.warn("--read и --outreach вместе не имеют смысла"))
                return 2
            cadence = None
            if args.read:
                cadence = dispatcher.CADENCE_READ
            elif args.outreach:
                cadence = dispatcher.CADENCE_OUTREACH
            result = asyncio.run(dispatcher.dispatch(
                store, settings, campaign_id=args.campaign, limit=args.limit,
                cadence=cadence,
                confirm=args.confirm, actor=args.actor,
            ))
        except dispatcher.DispatchBusy as exc:
            print(report.warn(str(exc)))
            return 0

        if result.get("dry_run"):
            if result["tasks"]:
                print(report.table(result["tasks"]))
            print()
            print(report.kv([
                ("готово к выпуску", result["would_dispatch"]),
                ("заблокировано", len(result["blocked"])),
                ("боевой режим", "включён" if result["armed"] else "ВЫКЛЮЧЕН"),
            ]))
            for item in result["blocked"][:20]:
                print(report.warn(f"{item['task']}: {item['why']}"))
            if not result["armed"]:
                print(report.warn(
                    "\nНичего не отправлено. Чтобы выпускать по-настоящему:\n"
                    "  bridge49 arm on\n"
                    "  bridge49 dispatch --confirm"
                ))
            elif not result["confirmed"]:
                print(report.warn("\nНичего не отправлено. Добавьте --confirm."))
            return 0

        print(report.good(f"выпущено команд: {result['dispatched']}"))
        for item in result.get("blocked", [])[:20]:
            print(report.warn(f"{item['task']}: {item['why']}"))
        for item in result.get("deferred", []):
            print(report.warn(
                f"{item['task']}: отложено, повторим тем же UUID — {item['why']}"
            ))
        for item in result.get("failed", []):
            print(report.bad(f"{item['task']}: {item['why']}"))
    return 0


def cmd_autoreply(args) -> int:
    """Разобрать входящие движком, поставить ответы и сразу их выпустить."""
    settings = _settings(args, need_dsn=(args.state == "run" and not args.hold))

    if args.state in ("on", "off"):
        settings.autoreply_file.parent.mkdir(parents=True, exist_ok=True)
        if args.state == "on":
            settings.autoreply_file.touch()
            print(report.good("автоответы включены"))
            print(report.warn(
                "Машина будет сама сочинять ответы людям. Она придержит то, в "
                "чём не уверена, но отправленное всё равно стоит перечитывать: "
                "bridge49 autoreply review"
            ))
        else:
            settings.autoreply_file.unlink(missing_ok=True)
            print("автоответы выключены")
        return 0

    with _store(settings) as store:
        if args.state == "review":
            rows = store.query(
                "SELECT t.id, t.account_id, t.state, t.review_reason, "
                "       t.dispatched_at, c.username "
                "  FROM tasks t LEFT JOIN contacts c ON c.id = t.contact_id "
                " WHERE t.campaign_id = ? AND t.review_reason IS NOT NULL "
                " ORDER BY t.id DESC LIMIT ?",
                (replies.AUTO_CAMPAIGN_ID, args.limit),
            )
            if not rows:
                print("помеченных ответов нет")
                return 0
            print(report.table([
                {"задача": r["id"], "аккаунт": r["account_id"],
                 "кому": r["username"] or "—", "состояние": r["state"],
                 "почему помечено": r["review_reason"]}
                for r in rows
            ]))
            return 0

        if not settings.autoreply_enabled:
            print(report.warn(
                "автоответы выключены — включить: bridge49 autoreply on"
            ))
            return 1
        result = autoreply.run(store, settings, limit=args.limit,
                               actor=args.actor)

        # Ответ, пролежавший в очереди до следующего разбора, — уже не ответ.
        # Поэтому сразу после разбора выпускаем созревшее, но только по своей
        # кампании: рассылка остаётся на ручном управлении, и автоответы её
        # очередь не трогают.
        sent = 0
        if not args.hold and settings.armed:
            try:
                released = asyncio.run(dispatcher.dispatch(
                    store, settings,
                    campaign_id=replies.AUTO_CAMPAIGN_ID,
                    confirm=True, actor=args.actor,
                ))
                sent = int(released.get("dispatched") or 0)
            except dispatcher.DispatchBusy as exc:
                print(report.warn(str(exc)))
            except dispatcher.DispatchBlocked as exc:
                print(report.warn(f"выпуск придержан: {exc}"))
        result["sent"] = sent

    print(report.kv(sorted(result.items())))
    if result.get("queued") and not settings.armed:
        print(report.warn(
            "боевой режим выключен — ответы поставлены, но не уйдут"
        ))
    if args.hold and result.get("queued"):
        print(report.warn(
            "выпуск отложен по --hold: ответы ждут `bridge49 dispatch --confirm`"
        ))
    return 0


def cmd_invites(args) -> int:
    """Автовыдача бесплатного теста: что настроено и что уже выдано."""
    # Реквизиты моста нужны только там, где мы сами выпускаем доставку.
    settings = _settings(
        args, need_dsn=(args.what == "process" and not args.hold)
    )
    branch = direct_invite.BranchConfig.from_env()

    with _store(settings) as store:
        if args.what == "process":
            result = direct_invite.process_requests(
                store, settings, config=branch, limit=args.limit,
                actor=args.actor,
            )

            # Свою кампанию выпускаем здесь же, и это не удобство, а
            # необходимость. Доставка ссылки — действие `reply_private_dm`,
            # то есть класс ответов: таймер рассылки её не берёт (он сужен до
            # `--outreach`), а прогон автоответов выпускает только свою
            # кампанию. Без этой строки выпущенная ссылка легла бы в очередь
            # навсегда — человек согласился, ссылка выпущена, и не уходит.
            sent = 0
            if not args.hold and settings.armed:
                try:
                    released = asyncio.run(dispatcher.dispatch(
                        store, settings,
                        campaign_id=direct_invite.INVITE_CAMPAIGN_ID,
                        confirm=True, actor=args.actor,
                    ))
                    sent = int(released.get("dispatched") or 0)
                except dispatcher.DispatchBusy as exc:
                    print(report.warn(str(exc)))
                except dispatcher.DispatchBlocked as exc:
                    print(report.warn(f"выпуск придержан: {exc}"))
            result["отправлено"] = sent

            result.update(direct_invite.reconcile_deliveries(
                store, actor=args.actor))
            print(report.kv(sorted(result.items())))
            if not branch.enabled:
                print(report.warn(
                    "ветка выключена — согласия копятся, ссылки не выдаются"
                ))
            if result.get("выпущено") and not settings.armed:
                print(report.warn(
                    "боевой режим выключен — ссылки выпущены, но не уйдут"
                ))
            return 0

        print(report.kv(sorted(branch.public_status().items())))
        print()
        rows = direct_invite.status_rows(store, limit=args.limit)
        if not rows:
            print("заявок пока нет")
            return 0
        print(report.table([
            {
                "заявка": row["request_id"],
                "кому": row["username"] or row["contact_id"],
                "сфера": row["sector_name"],
                "состояние": row["status"],
                "попыток": row["attempt_count"],
                "ссылка выдана": (row["link_delivered_at"] or "")[:16] or "—",
                "почему нет": (row["last_error"] or "")[:40] or "",
            }
            for row in rows
        ]))
        return 0


def cmd_arm(args) -> int:
    settings = _settings(args)
    message = dispatcher.arm(settings, args.state == "on", actor=args.actor)
    print(report.good(message) if args.state == "on" else message)
    if args.state == "on":
        print(report.warn(
            "Напоминание: команды mode=immediate у этих аккаунтов исполняются "
            "почти сразу.\nДля кампаний по умолчанию используйте mode=lottery."
        ))
    return 0


# ---------------------------------------------------------------------------
# чтение из Radar
# ---------------------------------------------------------------------------

def cmd_poll(args) -> int:
    settings = _settings(args, need_dsn=True)
    with _store(settings) as store:
        if args.what in ("results", "all"):
            result = asyncio.run(pollers.poll_results(store, settings, actor=args.actor))
            print(report.section("результаты команд"))
            print(report.kv(sorted(result.items())))
        if args.what in ("inbound", "all"):
            result = asyncio.run(
                pollers.poll_inbound(store, settings, limit=args.limit,
                                     actor=args.actor)
            )
            print(report.section("входящие"))
            print(report.kv(sorted(result.items())))
    return 0


def cmd_watchdog(args) -> int:
    """Проверить, что контур жив. Ненулевой код — есть на что смотреть."""
    if args.test_alert:
        target = alerts.TelegramTarget.from_file()
        if target is None:
            print(report.bad(
                "доставка не настроена: нет файла с реквизитами "
                f"({alerts.DEFAULT_ALERTS_FILE} или BRIDGE49_ALERTS)"
            ))
            return 1
        try:
            message_id = alerts.send(
                target, "🔧 outreach49: проверка канала тревог, реагировать не нужно"
            )
        except alerts.AlertError as exc:
            print(report.bad(f"не доставлено ({target.describe()}): {exc}"))
            return 1
        print(report.good(f"отправлено в {target.describe()}, id={message_id}"))
        return 0

    settings = _settings(args, need_dsn=not args.offline)
    with _store(settings) as store:
        result = asyncio.run(
            watchdog.run(store, settings, actor=args.actor,
                         with_bridge=not args.offline,
                         notify=not args.no_notify)
        )
    if args.json:
        _print_json(result.as_dict())
    else:
        print(report.section("сторож"))
        print(report.kv(sorted(result.facts.items())))
        if result.ok:
            print(report.good("всё ровно"))
        else:
            for finding in sorted(
                result.findings,
                key=lambda f: watchdog.SEVERITY_ORDER.get(f.severity, 9),
            ):
                line = f"[{finding.severity}] {finding.check}: {finding.detail}"
                print(
                    report.bad(line)
                    if finding.severity in (watchdog.CRITICAL, watchdog.HIGH)
                    else report.warn(line)
                )
    # Предупреждения не поднимают тревогу systemd: сутки нетронутых карточек —
    # повод посмотреть, а не повод считать сервис упавшим.
    return 1 if result.worst in (watchdog.CRITICAL, watchdog.HIGH) else 0


def cmd_reply(args) -> int:
    """Поставить ответ в диалог. Отправит его всё равно только dispatch."""
    settings = _settings(args)
    with _store(settings) as store:
        try:
            result = replies.queue_reply(
                store,
                text=args.text,
                thread_id=args.thread,
                account_id=args.account,
                peer=args.peer,
                mode=args.mode,
                actor=args.actor,
            )
        except replies.ReplyError as exc:
            print(report.bad(str(exc)))
            return 1
    print(report.kv(sorted(result.items())))
    if not settings.armed:
        print(report.warn(
            "боевой режим выключен — ответ поставлен в очередь, но не уйдёт. "
            "Включить: bridge49 arm on"
        ))
    else:
        print("отправит ближайший dispatch")
    return 0


def cmd_send(args) -> int:
    """Точечная отправка конкретному получателю — вне кампаний и сегментов."""
    settings = _settings(args)
    with _store(settings) as store:
        try:
            result = replies.queue_send(
                store, account_id=args.account, text=args.text,
                username=args.peer, tg_id=args.tg_id,
                channel_tg_id=args.channel_tg_id,
                monoforum_tg_id=args.monoforum_tg_id,
                kind=args.kind, mode=args.mode, actor=args.actor,
            )
        except replies.ReplyError as exc:
            print(report.bad(str(exc)))
            return 1
    print(report.kv(sorted(result.items())))
    if not settings.armed:
        print(report.warn("боевой режим выключен — поставлено в очередь, но не уйдёт"))
    return 0


def cmd_threads(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        sql = (
            "SELECT t.*, c.username AS target, a.label AS account_label, "
            "  (SELECT count(*) FROM inbound i WHERE i.account_id = t.account_id "
            "     AND i.peer_key = t.peer_key) AS incoming "
            "FROM threads t "
            "LEFT JOIN contacts c ON c.id = t.contact_id "
            "LEFT JOIN accounts a ON a.id = t.account_id WHERE 1=1"
        )
        params: list = []
        if args.state:
            sql += " AND t.state = ?"
            params.append(args.state)
        sql += " ORDER BY COALESCE(t.last_inbound_at, t.last_outbound_at) DESC LIMIT ?"
        params.append(args.limit)
        print(report.table([
            {
                "диалог": row["id"],
                "аккаунт": f"{row['account_id']} {row['account_label'] or ''}".strip(),
                "собеседник": row["target"] or row["peer_key"],
                "канал": row["surface"],
                "состояние": row["state"],
                "последнее входящее": report.local_time(row["last_inbound_at"], settings.timezone),
                "сообщений": row["incoming"],
            }
            for row in store.query(sql, params)
        ]))
    return 0


def cmd_thread(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        thread = store.one("SELECT * FROM threads WHERE id = ?", (args.thread,))
        if thread is None:
            print(report.bad("нет такого диалога"))
            return 1
        contact = store.one(
            "SELECT * FROM contacts WHERE id = ?", (thread["contact_id"],)
        )
        print(report.kv([
            ("диалог", thread["id"]),
            ("собеседник", (contact["username"] if contact else None)
             or thread["peer_key"]),
            ("имя", contact["display_name"] if contact else None),
            ("аккаунт", thread["account_id"] or "— (переписка до перехода)"),
            ("канал", thread["surface"]),
            ("состояние", thread["state"]),
        ]))

        # Единая лента: перенесённая история, наши исходящие и входящие.
        timeline: list[dict] = []
        for row in store.query(
            "SELECT * FROM history WHERE thread_id = ?", (thread["id"],)
        ):
            timeline.append({
                "at": row["sent_at"] or row["created_at"],
                "кто": "они" if row["direction"] == "inbound" else "мы",
                "источник": "перенос",
                "текст": row["text"] or "",
            })
        if thread["contact_id"]:
            for row in store.query(
                "SELECT * FROM tasks WHERE contact_id = ? ORDER BY created_at",
                (thread["contact_id"],),
            ):
                timeline.append({
                    "at": row["dispatched_at"] or row["scheduled_at"],
                    "кто": "мы",
                    "источник": f"{row['action']} / {row['state']}",
                    "текст": loads(row["params"], {}).get("text") or "",
                })
        for row in store.query(
            "SELECT * FROM inbound WHERE account_id = ? AND peer_key = ? ORDER BY id",
            (thread["account_id"], thread["peer_key"]),
        ):
            timeline.append({
                "at": row["sent_at"] or row["created_at"],
                "кто": "они",
                "источник": "входящее",
                "текст": row["text"] or "",
            })

        timeline.sort(key=lambda item: item["at"] or "")
        print(report.section(f"переписка ({len(timeline)})"))
        print(report.table([
            {
                "когда": report.local_time(item["at"], settings.timezone),
                "кто": item["кто"],
                "источник": item["источник"],
                "текст": item["текст"][:120],
            }
            for item in timeline[-args.limit:]
        ]))
    return 0


def cmd_handoffs(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        if args.take:
            pollers.take_handoff(store, args.take, args.owner or "manager",
                                 actor=args.actor)
            print("взято в работу")
            return 0
        if args.close:
            pollers.close_handoff(store, args.close, args.note or "", actor=args.actor)
            print("закрыто")
            return 0
        rows = store.query(
            "SELECT h.*, t.peer_key, t.account_id, c.username AS target "
            "FROM handoffs h JOIN threads t ON t.id = h.thread_id "
            "LEFT JOIN contacts c ON c.id = t.contact_id "
            "WHERE h.status != 'closed' ORDER BY h.created_at"
        )
        print(report.table([
            {
                "id": row["id"],
                "собеседник": row["target"] or row["peer_key"],
                "аккаунт": row["account_id"],
                "причина": row["reason"],
                "статус": row["status"],
                "владелец": row["owner"],
                "текст": (row["note"] or "")[:60],
            }
            for row in rows
        ]))
    return 0


def cmd_inbox(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        print(report.table([
            {
                "id": row["id"],
                "когда": report.local_time(row["sent_at"] or row["created_at"], settings.timezone),
                "аккаунт": row["account_id"],
                "от": row["peer_username"] or row["peer_key"],
                "канал": row["surface"],
                "текст": (row["text"] or "")[:80],
            }
            for row in store.query(
                "SELECT * FROM inbound ORDER BY id DESC LIMIT ?", (args.limit,)
            )
        ]))
    return 0


def cmd_events(args) -> int:
    settings = _settings(args)
    with _store(settings) as store:
        print(report.table([
            {
                "когда": report.local_time(row["at"], settings.timezone),
                "кто": row["actor"],
                "что": row["kind"],
                "объект": row["subject"],
                "детали": row["detail"],
            }
            for row in store.query(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (args.limit,)
            )
        ]))
    return 0


def cmd_serve(args) -> int:
    from .web import serve

    settings = _settings(args)
    serve(settings, host=args.host, port=args.port)
    return 0


def cmd_demo(args) -> int:
    from .demo import seed

    settings = _settings(args)
    with _store(settings) as store:
        result = seed(store, actor=args.actor)
        print(report.kv(sorted(result.items())))
        print("\nДальше:")
        print("  bridge49 plan " + result["campaign_id"])
        print("  bridge49 plan " + result["campaign_id"] + " --apply")
        print("  bridge49 dispatch          # предпросмотр, ничего не уйдёт")
    return 0


# ---------------------------------------------------------------------------
# разбор аргументов
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bridge49",
        description="Управление рассылкой через 49 TGR-аккаунтов Radar.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ничего не уходит в Telegram, пока нет файла ARMED и флага --confirm.\n"
            "Начните с: bridge49 doctor"
        ),
    )
    parser.add_argument("--home", help="каталог установки (умолчание /opt/bridge49)")
    parser.add_argument("--actor", default=ACTOR_DEFAULT, help="кто выполняет")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="общая сводка").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="проверить конфиг и связь").set_defaults(
        func=cmd_doctor
    )

    p = sub.add_parser("watchdog", help="жив ли контур (для таймера)")
    p.add_argument("--json", action="store_true", help="машинный вывод")
    p.add_argument("--offline", action="store_true",
                   help="без обращения к Radar — только локальные проверки")
    p.add_argument("--no-notify", action="store_true",
                   help="не писать в админку, только посмотреть")
    p.add_argument("--test-alert", action="store_true",
                   help="отправить проверочное сообщение и выйти")
    p.set_defaults(func=cmd_watchdog)

    p = sub.add_parser("accounts", help="реестр аккаунтов")
    p.add_argument("--sync", metavar="FILE", help="влить снимок из JSON")
    p.add_argument("--pause-new", action="store_true",
                   help="новые аккаунты принять в карантине: в реестре есть, "
                        "работы не получают, пауза снимается поимённо")
    p.add_argument("--role", help="фильтр по роли")
    p.add_argument("--pause", type=int, metavar="ID")
    p.add_argument("--resume", type=int, metavar="ID")
    p.add_argument("--resume-one", dest="resume_one", metavar="ROLE",
                   choices=sorted(catalog.ROLES),
                   help="ввести в работу один приостановленный аккаунт роли "
                        "(ступень постепенного ввода)")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("actions", help="какие действия вообще бывают")
    p.add_argument("--role", choices=catalog.ROLES)
    p.set_defaults(func=cmd_actions)

    p = sub.add_parser("contacts", help="кого достаём")
    p.add_argument("--add", metavar="USERNAME")
    p.add_argument("--name")
    p.add_argument("--company")
    p.add_argument("--segment", default="default")
    # Цель разведки — не человек, а чат или канал. Раньше это умел только
    # импорт CSV, и завести одну цель руками было нечем.
    p.add_argument("--kind", default="user", choices=sorted(entities.CONTACT_KINDS))
    p.add_argument("--peer-kind", dest="peer_kind",
                   choices=sorted(catalog.PEER_KINDS))
    p.add_argument("--tag", action="append")
    p.add_argument("--import", dest="import_csv", metavar="CSV")
    p.add_argument("--opt-out", metavar="CONTACT_ID")
    p.add_argument("--reason")
    p.add_argument("--status")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_contacts)

    p = sub.add_parser("templates", help="тексты")
    p.add_argument("--add", metavar="NAME")
    p.add_argument("--body", help="текст, путь к файлу или '-' для stdin")
    p.add_argument("--id")
    p.add_argument("--note")
    p.add_argument("--show", metavar="TEMPLATE_ID")
    p.set_defaults(func=cmd_templates)

    p = sub.add_parser("campaigns", help="кампании")
    p.add_argument("--add", metavar="NAME")
    p.add_argument("--action", default="send_private_dm", choices=sorted(catalog.ACTIONS))
    p.add_argument("--template")
    p.add_argument("--segment", default="default")
    p.add_argument("--mode", default="lottery", choices=("lottery", "immediate"))
    p.add_argument("--daily-cap", type=int, default=50)
    p.add_argument("--per-account-cap", type=int, default=12)
    p.add_argument("--ttl-hours", type=int, default=48)
    p.add_argument("--params", help="доп. params одной JSON-строкой")
    p.add_argument("--allow-repeat", action="store_true",
                   help="писать и тем, кого уже касались (догоняющая волна)")
    p.add_argument("--role", action="append", choices=sorted(catalog.ROLES),
                   help="поручать только этой роли (можно повторять)")
    p.add_argument("--account", action="append", type=int, metavar="ID",
                   help="закрепить кампанию за конкретными аккаунтами "
                        "(можно повторять)")
    p.add_argument("--note")
    p.add_argument("--id")
    p.add_argument("--status-of", metavar="CAMPAIGN_ID")
    p.add_argument("--set-status", default="active",
                   choices=entities.CAMPAIGN_STATUSES)
    p.set_defaults(func=cmd_campaigns)

    p = sub.add_parser("plan", help="разложить кампанию по аккаунтам и времени")
    p.add_argument("campaign")
    p.add_argument("--apply", action="store_true", help="сохранить план")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_plan)

    p = sub.add_parser("queue", help="очередь задач")
    p.add_argument("--campaign")
    p.add_argument("--state")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_queue)

    p = sub.add_parser("sources", help="что выяснила разведка о чатах и каналах")
    p.add_argument("--campaign")
    p.add_argument("--state")
    p.add_argument("--verdict", help="оставить только один вердикт")
    p.add_argument("--csv", metavar="PATH", help="выгрузить в файл вместо экрана")
    p.add_argument("--full", action="store_true", help="все колонки на экран")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_sources)

    p = sub.add_parser("dispatch", help="выпустить созревшие задачи в Radar")
    p.add_argument("--campaign")
    p.add_argument("--read", action="store_true",
                   help="только чтения метаданных, рассылку не трогать")
    # Без фильтра пачка достаётся тому, кто просрочен дольше. Каталог разведки
    # просрочен на часы и исчисляется тысячами задач, поэтому смешанный прогон
    # выгребал бы только его, а письмо человеку не уходило бы никогда — не
    # из-за запрета, а потому что до него не доходила очередь.
    p.add_argument("--outreach", action="store_true",
                   help="только рассылка, разведку не трогать")
    p.add_argument("--limit", type=int)
    p.add_argument("--confirm", action="store_true",
                   help="реально поставить команды (нужен ещё и режим arm on)")
    p.add_argument("--unblock", action="append", metavar="TASK_ID",
                   help="вернуть заблокированную задачу в план")
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("arm", help="включить/выключить боевой режим")
    p.add_argument("state", choices=("on", "off"))
    p.set_defaults(func=cmd_arm)

    p = sub.add_parser("autoreply", help="автоответы на входящие")
    p.add_argument("state", nargs="?", default="run",
                   choices=("run", "on", "off", "review"),
                   help="run — разобрать входящие; review — что помечено "
                        "на перечитывание")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--hold", action="store_true",
                   help="только поставить в очередь, не выпускать")
    p.set_defaults(func=cmd_autoreply)

    p = sub.add_parser("invites", help="автовыдача бесплатного теста")
    p.add_argument("what", nargs="?", default="status",
                   choices=("status", "process"),
                   help="status — что настроено и в каком состоянии заявки; "
                        "process — выпустить ссылки по накопившимся согласиям")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--hold", action="store_true",
                   help="только выпустить ссылки, доставку не отправлять")
    p.set_defaults(func=cmd_invites)

    p = sub.add_parser("poll", help="забрать результаты и входящие")
    p.add_argument("what", nargs="?", default="all",
                   choices=("all", "results", "inbound"))
    p.add_argument("--limit", type=int, default=500)
    p.set_defaults(func=cmd_poll)

    p = sub.add_parser("send", help="точечно отправить сообщение получателю")
    p.add_argument("--account", type=int, required=True, help="с какого аккаунта")
    p.add_argument("--peer", help="username получателя или канала")
    p.add_argument("--tg-id", type=int, dest="tg_id", help="или числовой id")
    p.add_argument("--kind", default="user", choices=("user", "channel_dm"))
    p.add_argument("--channel-tg-id", type=int, dest="channel_tg_id")
    p.add_argument("--monoforum-tg-id", type=int, dest="monoforum_tg_id")
    p.add_argument("--text", required=True)
    # immediate — единственный работающий режим: lottery ждёт розыгрыша,
    # которого в этом бизнесе не настроено, и команда зависает в new.
    p.add_argument("--mode", default="immediate", choices=("lottery", "immediate"))
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("reply", help="ответить в диалог")
    p.add_argument("--thread", help="id диалога (см. threads)")
    p.add_argument("--account", type=int, help="или пара: аккаунт …")
    p.add_argument("--peer", help="… и собеседник (@username или id:123)")
    p.add_argument("--text", required=True, help="текст ответа")
    p.add_argument("--mode", default="immediate", choices=("lottery", "immediate"))
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("threads", help="диалоги")
    p.add_argument("--state")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_threads)

    p = sub.add_parser("thread", help="один диалог целиком")
    p.add_argument("thread")
    p.add_argument("--limit", type=int, default=50,
                   help="сколько последних сообщений показать")
    p.set_defaults(func=cmd_thread)

    p = sub.add_parser("handoffs", help="что ждёт менеджера")
    p.add_argument("--take", metavar="HANDOFF_ID")
    p.add_argument("--owner")
    p.add_argument("--close", metavar="HANDOFF_ID")
    p.add_argument("--note")
    p.set_defaults(func=cmd_handoffs)

    p = sub.add_parser("inbox", help="последние входящие")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("events", help="журнал действий")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("serve", help="веб-панель только для чтения")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8649)
    p.set_defaults(func=cmd_serve)

    sub.add_parser("demo", help="засеять демо-данные").set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except config.ConfigError as exc:
        print(report.bad(str(exc)))
        return 2
    except KeyboardInterrupt:
        print("\nпрервано")
        return 130


if __name__ == "__main__":
    sys.exit(main())
