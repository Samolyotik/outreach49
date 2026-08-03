"""Тесты bridge49. Стандартный unittest — никаких зависимостей.

Запуск:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import random
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import (alerts, catalog, config, dispatcher, entities, planner,  # noqa: E402
                      pollers, radar, replies, watchdog)
from bridge49.config import Limits, Settings  # noqa: E402
from bridge49.store import Store, now  # noqa: E402


def _load_script(name: str):
    """Скрипты лежат вне пакета — подгружаем по пути."""
    path = Path(__file__).resolve().parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


legacy_import = _load_script("import_from_tgradar_outreach")

SNAPSHOT = [
    {
        "id": 821, "label": "dm-one", "program_code": "TGR1",
        "runtime_state": "running",
        "outreach": {
            "enabled": True, "roles": ["dm_sender"], "publish_inbound": True,
            "allow_immediate_visible_actions": True,
            "allowed_actions": [
                "command_dry_run", "gateway_capabilities", "get_me",
                "create_private_chat", "send_private_dm", "reply_private_dm",
                "sync_private_dm_replies", "mark_messages_read", "send_typing",
            ],
        },
    },
    {
        "id": 803, "label": "chat-one", "program_code": "TGR8",
        "runtime_state": "running",
        "outreach": {
            "enabled": True, "roles": ["chat_sender"], "publish_inbound": True,
            "allow_immediate_visible_actions": False,
            "allowed_actions": [
                "command_dry_run", "get_me", "search_public_chat",
                "join_public_chat", "send_public_chat_message",
            ],
        },
    },
]


def make_env(tmp: Path) -> tuple[Store, Settings]:
    store = Store(tmp / "b.sqlite")
    accounts_mod.sync(store, SNAPSHOT)
    limits = Limits()
    # Паузу флота тесты выключают: диспетчер её честно высыпает, и с боевыми
    # 10–20 секундами прогон растянулся бы на минуты. Её собственное поведение
    # проверяется отдельно, в FleetPaceTests.
    limits.global_visible_interval_min_sec = 0
    limits.global_visible_interval_max_sec = 0
    settings = Settings(
        home=tmp, db_path=tmp / "b.sqlite", dsn=None, limits=limits,
        timezone="Europe/Moscow",
    )
    return store, settings


class CatalogTests(unittest.TestCase):
    def test_role_gate(self):
        # monoforum канала пишет только channel_sender — это по-прежнему так.
        with self.assertRaises(catalog.ValidationError):
            catalog.validate(
                "send_channel_dm", {"username": "x", "text": "привет"},
                roles={"dm_sender"},
            )
        action = catalog.validate(
            "send_channel_dm", {"username": "x", "text": "привет"},
            roles={"channel_sender"},
        )
        self.assertEqual(action.risk, catalog.RISK_MATURE_DM)

    def test_chat_sender_may_write_private_dm(self):
        # Расширено 03.08.2026: у трёх chat_sender остались личные диалоги,
        # а reply_private_dm им недоступен — нет входящего уведомления Radar.
        action = catalog.validate(
            "send_private_dm", {"username": "x", "text": "привет"},
            roles={"chat_sender"},
        )
        self.assertEqual(action.risk, catalog.RISK_MATURE_DM)

    def test_allowlist_narrows_role(self):
        with self.assertRaises(catalog.ValidationError):
            catalog.validate(
                "send_private_dm", {"username": "x", "text": "привет"},
                roles={"dm_sender"}, allowed_actions={"get_me"},
            )

    def test_unknown_param_rejected(self):
        with self.assertRaises(catalog.ValidationError):
            catalog.validate(
                "get_me", {"username": "x"}, roles={"dm_sender"},
            )

    def test_activity_flags_only_where_allowed(self):
        with self.assertRaises(catalog.ValidationError):
            catalog.validate(
                "get_me", {"online": False}, roles={"dm_sender"},
            )
        catalog.validate(
            "send_private_dm", {"username": "x", "text": "т", "online": True},
            roles={"dm_sender"},
        )
        with self.assertRaises(catalog.ValidationError):
            catalog.validate(
                "send_private_dm",
                {"username": "x", "text": "т", "online": "true"},
                roles={"dm_sender"},
            )

    def test_text_length_measured_in_utf16(self):
        # Эмодзи вне BMP занимают две UTF-16 единицы — как и считает Telegram.
        self.assertEqual(catalog.utf16_len("🙂"), 2)
        with self.assertRaises(catalog.ValidationError):
            catalog.validate(
                "send_private_dm", {"username": "x", "text": "🙂" * 2049},
                roles={"dm_sender"},
            )

    def test_caption_rules_with_attachment(self):
        catalog.validate(
            "send_private_dm", {"username": "x", "text": ""},
            roles={"dm_sender"}, has_attachment=True,
        )
        with self.assertRaises(catalog.ValidationError):
            catalog.validate(
                "send_private_dm", {"username": "x", "text": ""},
                roles={"dm_sender"},
            )

    def test_source_finder_bot_is_pinned(self):
        with self.assertRaises(catalog.ValidationError):
            catalog.validate(
                "source_finder_bot_send_text", {"text": "hi", "bot_id": 123},
                roles={"source_finder"},
            )

    def test_no_telegram_actions_are_read(self):
        for name in catalog.NO_TELEGRAM_ACTIONS:
            self.assertEqual(catalog.ACTIONS[name].risk, catalog.RISK_READ)


class TemplateTests(unittest.TestCase):
    def test_missing_placeholder_is_an_error(self):
        contact = {"display_name": "", "username": "u", "vars": "{}"}
        with self.assertRaises(ValueError):
            entities.render("Здравствуйте, {name}!", contact)

    def test_render_uses_contact_vars(self):
        contact = {"display_name": "Иван", "username": "u",
                   "vars": '{"city": "Тверь"}'}
        self.assertEqual(
            entities.render("{name} из {city}", contact), "Иван из Тверь"
        )

    def test_username_normalization(self):
        for raw in ("@user_one", "https://t.me/user_one", "t.me/user_one",
                    " user_one "):
            self.assertEqual(entities.normalize_username(raw), "user_one")


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_visible_action_lands_inside_send_window(self):
        entities.add_template(self.store, "t", "Здравствуйте, {name}!",
                              template_id="t1")
        for i in range(3):
            entities.add_contact(self.store, username=f"lead_{i}",
                                 display_name=f"Имя{i}", segment="s")
        entities.add_campaign(
            self.store, name="c", action="send_private_dm", template_id="t1",
            segment="s", campaign_id="c1",
        )
        result = planner.plan(
            self.store, "c1", limits=self.settings.limits,
            timezone_name="Europe/Moscow", dry_run=True,
        )
        self.assertEqual(result["planned"], 3)
        from zoneinfo import ZoneInfo

        for task in result["tasks"]:
            local = datetime.fromisoformat(task["scheduled_at"]).astimezone(
                ZoneInfo("Europe/Moscow")
            )
            self.assertIn(local.weekday(), self.settings.limits.send_weekdays)
            self.assertGreaterEqual(
                local.hour, self.settings.limits.send_window_start_hour
            )
            self.assertLess(local.hour, self.settings.limits.send_window_end_hour)

    def test_read_action_ignores_window(self):
        entities.add_contact(self.store, username="lead_x", segment="s")
        entities.add_campaign(
            self.store, name="c", action="command_dry_run", segment="s",
            campaign_id="c2",
        )
        result = planner.plan(
            self.store, "c2", limits=self.settings.limits, dry_run=True
        )
        planned = datetime.fromisoformat(result["tasks"][0]["scheduled_at"])
        self.assertLess(planned - datetime.now(timezone.utc), timedelta(minutes=5))

    def test_replan_does_not_duplicate(self):
        entities.add_contact(self.store, username="lead_y", segment="s")
        entities.add_campaign(
            self.store, name="c", action="command_dry_run", segment="s",
            campaign_id="c3",
        )
        planner.plan(self.store, "c3", limits=self.settings.limits, dry_run=False)
        again = planner.plan(
            self.store, "c3", limits=self.settings.limits, dry_run=True
        )
        self.assertEqual(again["planned"], 0)

    def test_opted_out_contact_is_never_planned(self):
        contact = entities.add_contact(self.store, username="lead_z", segment="s")
        entities.opt_out(self.store, contact["id"], "просил не писать")
        entities.add_campaign(
            self.store, name="c", action="command_dry_run", segment="s",
            campaign_id="c4",
        )
        result = planner.plan(
            self.store, "c4", limits=self.settings.limits, dry_run=True
        )
        self.assertEqual(result["planned"], 0)

    def test_no_capable_account_is_a_clear_error(self):
        entities.add_contact(self.store, username="lead_w", segment="s")
        entities.add_campaign(
            self.store, name="c", action="collect_private_club_contacts",
            segment="s", campaign_id="c5",
        )
        with self.assertRaises(planner.PlanError) as ctx:
            planner.plan(self.store, "c5", limits=self.settings.limits,
                         dry_run=True)
        self.assertIn("private_reader", str(ctx.exception))


class DispatchGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        entities.add_contact(self.store, username="lead_a", segment="s")
        entities.add_campaign(
            self.store, name="c", action="command_dry_run", segment="s",
            campaign_id="cx",
        )
        entities.set_campaign_status(self.store, "cx", "active")
        planner.plan(self.store, "cx", limits=self.settings.limits, dry_run=False)
        self.store.execute("UPDATE tasks SET scheduled_at = '2000-01-01T00:00:00+00:00'")
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_without_armed_file_nothing_is_sent(self):
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=True)
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(result["would_dispatch"], 1)

    def test_armed_without_confirm_is_still_a_preview(self):
        dispatcher.arm(self.settings, True)
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["dispatched"], 0)

    def test_draft_campaign_is_blocked(self):
        entities.set_campaign_status(self.store, "cx", "draft")
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertEqual(result["would_dispatch"], 0)
        self.assertEqual(len(result["blocked"]), 1)
        self.assertIn("draft", result["blocked"][0]["why"])

    def test_paused_account_is_blocked(self):
        accounts_mod.pause(self.store, 821, True)
        accounts_mod.pause(self.store, 803, True)
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertEqual(result["would_dispatch"], 0)

    def test_orphaned_task_still_obeys_campaign_status(self):
        # UUID пишется до обращения к Radar, поэтому оборванная задача могла
        # ещё не уехать вовсе — пауза кампании обязана её удержать.
        self.store.execute(
            "UPDATE tasks SET request_id = 'уже-выдан' WHERE campaign_id = 'cx'"
        )
        self.store.commit()
        entities.set_campaign_status(self.store, "cx", "paused")
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertEqual(result["would_dispatch"], 0)
        self.assertEqual(len(result["blocked"]), 1)
        self.assertIn("paused", result["blocked"][0]["why"])

    def test_orphaned_task_is_replayed_with_the_same_uuid(self):
        self.store.execute(
            "UPDATE tasks SET request_id = 'уже-выдан' WHERE campaign_id = 'cx'"
        )
        self.store.commit()
        pending = dispatcher.orphaned(self.store)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["request_id"], "уже-выдан")
        self.assertEqual(pending[0]["campaign_status"], "active")

    def test_immediate_visible_requires_account_flag(self):
        # 803 не имеет allow_immediate_visible_actions.
        self.store.execute(
            "UPDATE tasks SET mode='immediate', action='send_public_chat_message', "
            "account_id=803, params='{\"username\": \"grp\", \"text\": \"т\"}'"
        )
        self.store.commit()
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertEqual(result["would_dispatch"], 0)
        self.assertIn("allow_immediate", result["blocked"][0]["why"])


class FakeBridge:
    """Заглушка моста: отдаёт заранее заданные строки, никуда не ходит."""

    def __init__(self, rows):
        self.rows = rows

    def __call__(self, dsn, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def inbound(self, after_id, limit):
        return [r for r in self.rows if int(r["id"]) > int(after_id)][:limit]


class FakeResultBridge(FakeBridge):
    async def results(self, command_ids):
        wanted = {int(command_id) for command_id in command_ids}
        return [r for r in self.rows if int(r["id"]) in wanted]


class ResultPollTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        entities.add_campaign(
            self.store, name="results", action="command_dry_run",
            segment="results", campaign_id="results",
        )
        entities.add_contact(self.store, username="result_lead",
                             segment="results")
        planner.plan(
            self.store, "results", limits=self.settings.limits, dry_run=False,
        )
        self.store.execute(
            "UPDATE tasks SET state='queued', command_id=1001, "
            "dispatched_at='2026-08-01T06:21:48+00:00'"
        )
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _poll(self, rows):
        original = pollers.RadarBridge
        pollers.RadarBridge = FakeResultBridge(rows)
        try:
            return asyncio.run(pollers.poll_results(self.store, self.settings))
        finally:
            pollers.RadarBridge = original

    def _task(self):
        return dict(self.store.one("SELECT * FROM tasks"))

    @staticmethod
    def _record(*, status="done", result=None, updated_at="2026-08-01T06:22:20+00:00"):
        details = {} if result is None else {"result": result}
        return {
            "id": 1001,
            "status": status,
            "last_error": None,
            "updated_at": updated_at,
            "details": details,
        }

    def test_terminal_status_without_result_stays_observable(self):
        result = self._poll([self._record()])

        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["still_running"], 1)
        self.assertEqual(self._task()["state"], "queued")

    def test_later_authoritative_result_completes_the_same_task(self):
        self._poll([self._record()])
        result = self._poll([
            self._record(
                result={"outcome": "succeeded", "data": {}},
                updated_at="2026-08-01T06:22:24+00:00",
            )
        ])

        self.assertEqual(result["updated"], 1)
        self.assertEqual(self._task()["state"], "done")
        self.assertEqual(self._task()["outcome"], "succeeded")

    def test_old_false_failed_row_is_reconciled(self):
        self.store.execute(
            "UPDATE tasks SET state='failed', outcome=NULL, result='{}'"
        )
        self.store.commit()

        result = self._poll([
            self._record(result={"outcome": "succeeded", "data": {}})
        ])

        self.assertEqual(result["checked"], 1)
        self.assertEqual(self._task()["state"], "done")
        self.assertEqual(self._task()["outcome"], "succeeded")

    def test_outcome_unknown_is_rechecked_for_radar_recovery(self):
        self._poll([
            self._record(
                status="failed",
                result={
                    "outcome": "outcome_unknown",
                    "error": {"code": "stale_after_effect_marker"},
                },
            )
        ])
        self.assertEqual(self._task()["state"], "failed")
        self.assertEqual(self._task()["outcome"], "outcome_unknown")

        self._poll([
            self._record(result={"outcome": "succeeded", "data": {}})
        ])

        self.assertEqual(self._task()["state"], "done")
        self.assertEqual(self._task()["outcome"], "succeeded")


def inbound_record(notification_id: int, *, job: str, conversation: str) -> dict:
    return {
        "id": notification_id,
        "account_id": 821,
        "created_at": "2026-07-30T12:00:00+00:00",
        "details": {
            "schema": "tgr.outreach.inbound",
            "version": 1,
            "surface": "private_dm",
            "peer": {"type": "user", "tg_id": 555, "username": "someone"},
            "message": {
                "tg_message_id": 9, "sender_tg_id": 555,
                "date": "2026-07-30T12:00:00+00:00", "text": "интересно",
            },
            "correlation": {
                "external_job_id": job,
                "external_conversation_id": conversation,
            },
        },
    }


class InboundPollTests(unittest.TestCase):
    """Фид виден целиком по бизнесу, включая команды чужого продюсера."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _poll(self, rows):
        original = pollers.RadarBridge
        pollers.RadarBridge = FakeBridge(rows)
        try:
            return asyncio.run(pollers.poll_inbound(self.store, self.settings))
        finally:
            pollers.RadarBridge = original

    def test_foreign_correlation_ids_do_not_break_the_feed(self):
        result = self._poll([
            inbound_record(1001, job="их-кампания", conversation="их-лид")
        ])
        self.assertEqual(result["stored"], 1)
        self.assertEqual(result["link_failed"], 0)
        self.assertEqual(result["cursor"], 1001)
        thread = self.store.one("SELECT * FROM threads")
        self.assertIsNone(thread["campaign_id"])
        self.assertIsNone(thread["contact_id"])

    def test_our_own_correlation_ids_are_linked(self):
        contact = entities.add_contact(self.store, username="someone",
                                       segment="s")
        entities.add_campaign(self.store, name="c", action="command_dry_run",
                              segment="s", campaign_id="cmp_ours")
        result = self._poll([
            inbound_record(1002, job="cmp_ours", conversation=contact["id"])
        ])
        self.assertEqual(result["stored"], 1)
        thread = self.store.one("SELECT * FROM threads")
        self.assertEqual(thread["campaign_id"], "cmp_ours")
        self.assertEqual(thread["contact_id"], contact["id"])
        self.assertEqual(thread["state"], "handoff")
        self.assertEqual(
            self.store.one("SELECT status FROM contacts WHERE id = ?",
                           (contact["id"],))["status"],
            "replied",
        )

    def test_cursor_prevents_reprocessing(self):
        rows = [inbound_record(1003, job="x", conversation="y")]
        self.assertEqual(self._poll(rows)["stored"], 1)
        self.assertEqual(self._poll(rows)["fetched"], 0)

    def test_one_handoff_per_thread(self):
        self._poll([
            inbound_record(1004, job="x", conversation="y"),
            inbound_record(1005, job="x", conversation="y"),
        ])
        count = self.store.one("SELECT count(*) AS n FROM handoffs")["n"]
        self.assertEqual(count, 1)


class FakeEnqueueBridge:
    """Заглушка записи: считает вызовы и умеет падать заданной ошибкой."""

    def __init__(self, error=None):
        self.error = error
        self.calls = []
        self.next_id = 1000

    def __call__(self, dsn, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def enqueue(self, account_id, request, available_at=None, **kwargs):
        self.calls.append((account_id, request))
        if self.error is not None:
            raise self.error
        self.next_id += 1
        return self.next_id


def run_dispatch(store, settings, bridge, **kwargs):
    original = dispatcher.RadarBridge
    dispatcher.RadarBridge = bridge
    try:
        return asyncio.run(dispatcher.dispatch(store, settings, **kwargs))
    finally:
        dispatcher.RadarBridge = original


class DailyCapTests(unittest.TestCase):
    """Лимит обязан держать и ВНУТРИ одного прогона, а не только между ними."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        self.settings.limits.per_account_daily_visible = 3
        self.settings.limits.dispatch_batch = 25
        # Здесь проверяется именно суточный лимит, поэтому паузу между
        # отправками выключаем — она живёт в PaceFloorTests.
        self.settings.limits.per_account_visible_interval_sec = 0
        # Окно отправки не должно мешать тесту.
        self.settings.limits.send_window_start_hour = 0
        self.settings.limits.send_window_end_hour = 24
        self.settings.limits.send_weekdays = (0, 1, 2, 3, 4, 5, 6)
        entities.add_template(self.store, "t", "Привет, {name}!", template_id="t1")
        entities.add_campaign(
            self.store, name="c", action="send_private_dm", template_id="t1",
            segment="s", campaign_id="cap", per_account_daily_cap=99,
            daily_cap=99,
        )
        entities.set_campaign_status(self.store, "cap", "active")
        for i in range(10):
            entities.add_contact(self.store, username=f"lead_cap{i}",
                                 display_name=f"Имя{i}", segment="s")
        planner.plan(self.store, "cap", limits=self.settings.limits,
                     dry_run=False)
        # Все задачи созрели и все на единственном dm_sender.
        self.store.execute(
            "UPDATE tasks SET scheduled_at = '2000-01-01T00:00:00+00:00', "
            "account_id = 821"
        )
        self.store.commit()
        dispatcher.arm(self.settings, True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_cap_holds_within_a_single_run(self):
        bridge = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(result["dispatched"], 3)
        self.assertEqual(len(bridge.calls), 3)
        self.assertTrue(
            any("лимит 3" in b["why"] for b in result["blocked"]),
            result["blocked"],
        )

    def test_preview_shows_the_same_number(self):
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertEqual(result["would_dispatch"], 3)

    def test_read_actions_do_not_spend_the_visible_budget(self):
        self.store.execute(
            "UPDATE tasks SET action='command_dry_run', params='{}', "
            "dispatched_at=strftime('%Y-%m-%dT%H:%M:%S+00:00','now'), state='done'"
        )
        self.store.commit()
        self.assertEqual(dispatcher.visible_sent_today(self.store, 821), 0)


class SendWindowAtDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        entities.add_template(self.store, "t", "Привет!", template_id="t1")
        entities.add_campaign(
            self.store, name="c", action="send_private_dm", template_id="t1",
            segment="s", campaign_id="win",
        )
        entities.set_campaign_status(self.store, "win", "active")
        entities.add_contact(self.store, username="lead_win", segment="s")
        planner.plan(self.store, "win", limits=self.settings.limits, dry_run=False)
        self.store.execute(
            "UPDATE tasks SET scheduled_at = '2000-01-01T00:00:00+00:00', "
            "account_id = 821"
        )
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_outside_the_window_a_visible_task_is_held(self):
        # Окно, в которое текущий момент заведомо не попадает.
        self.settings.limits.send_weekdays = ()
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertEqual(result["would_dispatch"], 0)
        self.assertIn("вне окна", result["blocked"][0]["why"])

    def test_inside_the_window_it_goes(self):
        self.settings.limits.send_window_start_hour = 0
        self.settings.limits.send_window_end_hour = 24
        self.settings.limits.send_weekdays = (0, 1, 2, 3, 4, 5, 6)
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertEqual(result["would_dispatch"], 1)


class EnqueueFailureTests(unittest.TestCase):
    """Неизвестный исход нельзя закрывать: команда могла уже уехать."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        entities.add_campaign(
            self.store, name="c", action="command_dry_run", segment="s",
            campaign_id="err",
        )
        entities.set_campaign_status(self.store, "err", "active")
        entities.add_contact(self.store, username="lead_err", segment="s")
        planner.plan(self.store, "err", limits=self.settings.limits, dry_run=False)
        self.store.execute(
            "UPDATE tasks SET scheduled_at = '2000-01-01T00:00:00+00:00'"
        )
        self.store.commit()
        dispatcher.arm(self.settings, True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _task(self):
        return dict(self.store.one("SELECT * FROM tasks"))

    def test_unknown_outcome_keeps_the_task_planned_with_its_uuid(self):
        bridge = FakeEnqueueBridge(radar.BridgeUnknown("связь оборвалась"))
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(len(result["deferred"]), 1)
        task = self._task()
        self.assertEqual(task["state"], "planned")
        self.assertIsNotNone(task["request_id"])

    def test_the_same_uuid_is_replayed_next_run(self):
        first = FakeEnqueueBridge(radar.BridgeUnknown("связь оборвалась"))
        run_dispatch(self.store, self.settings, first, confirm=True)
        uuid_used = first.calls[0][1]["request_id"]

        second = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, second, confirm=True)
        self.assertEqual(result["dispatched"], 1)
        self.assertEqual(second.calls[0][1]["request_id"], uuid_used)

    def test_deterministic_rejection_is_terminal(self):
        bridge = FakeEnqueueBridge(radar.BridgeRejected("роль не позволяет"))
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(self._task()["state"], "blocked")

    def test_blocked_can_be_returned_to_the_plan(self):
        run_dispatch(self.store, self.settings,
                     FakeEnqueueBridge(radar.BridgeRejected("нет")), confirm=True)
        self.assertEqual(dispatcher.unblock(self.store, [self._task()["id"]]), 1)
        self.assertEqual(self._task()["state"], "planned")

    def test_claim_is_atomic(self):
        task = self._task()
        first = dispatcher._claim(self.store, task)
        # Второй процесс видит ту же строку в старом снимке, без request_id.
        stale = dict(task)
        stale["request_id"] = None
        second = dispatcher._claim(self.store, stale)
        self.assertEqual(first, second)


class ReplanAccountingTests(unittest.TestCase):
    """Повторный plan не должен выдавать аккаунту лимит заново."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        entities.add_template(self.store, "t", "Привет!", template_id="t1")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _campaign(self, name, cap=2):
        entities.add_campaign(
            self.store, name=name, action="send_private_dm", template_id="t1",
            segment="s", campaign_id=name, per_account_daily_cap=cap,
            daily_cap=99,
        )
        return name

    def test_second_campaign_does_not_reset_the_daily_load(self):
        for i in range(8):
            entities.add_contact(self.store, username=f"lead_rp{i}", segment="s")
        planner.plan(self.store, self._campaign("one"),
                     limits=self.settings.limits, dry_run=False)
        planner.plan(self.store, self._campaign("two"),
                     limits=self.settings.limits, dry_run=False)

        rows = self.store.query(
            "SELECT account_id, substr(scheduled_at, 1, 10) AS day, count(*) AS n "
            "FROM tasks GROUP BY account_id, day"
        )
        for row in rows:
            self.assertLessEqual(
                row["n"], 2,
                f"аккаунт {row['account_id']} получил {row['n']} задач на {row['day']}",
            )

    def test_slots_never_collide(self):
        for i in range(12):
            entities.add_contact(self.store, username=f"lead_col{i}", segment="s")
        planner.plan(self.store, self._campaign("a", cap=3),
                     limits=self.settings.limits, dry_run=False)
        planner.plan(self.store, self._campaign("b", cap=3),
                     limits=self.settings.limits, dry_run=False)
        rows = self.store.query(
            "SELECT account_id, scheduled_at, count(*) AS n FROM tasks "
            "GROUP BY account_id, scheduled_at HAVING n > 1"
        )
        self.assertEqual([dict(r) for r in rows], [])

    def test_completed_tasks_still_count(self):
        for i in range(4):
            entities.add_contact(self.store, username=f"lead_done{i}", segment="s")
        planner.plan(self.store, self._campaign("x", cap=2),
                     limits=self.settings.limits, dry_run=False)
        self.store.execute("UPDATE tasks SET state = 'done'")
        self.store.commit()
        planner.plan(self.store, self._campaign("y", cap=2),
                     limits=self.settings.limits, dry_run=False)
        rows = self.store.query(
            "SELECT account_id, substr(scheduled_at, 1, 10) AS day, count(*) AS n "
            "FROM tasks GROUP BY account_id, day"
        )
        for row in rows:
            self.assertLessEqual(row["n"], 2)


class CsvImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _write(self, text: str) -> Path:
        path = Path(self.tmp.name) / "leads.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def test_extra_columns_do_not_crash_the_import(self):
        path = self._write(
            "username,name,company\n"
            "lead_ok,Иван,ООО Ромашка\n"
            "lead_comma,Пётр,ООО Ромашка, Тверь\n"   # лишняя запятая
            "lead_tail,Анна,ООО Астра,\n"            # хвостовая запятая
        )
        result = entities.import_csv(self.store, path)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["added"], 3)

    def test_contacts_without_username_dedupe_by_tg_id(self):
        path = self._write("tg_id,name\n555,Иван\n555,Иван ещё раз\n")
        result = entities.import_csv(self.store, path)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(
            self.store.one("SELECT count(*) AS n FROM contacts")["n"], 1
        )


class AccountRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_candidates_respect_role(self):
        self.assertEqual(
            [a["id"] for a in accounts_mod.candidates(self.store, "send_private_dm")],
            [821],
        )
        self.assertEqual(
            [a["id"] for a in
             accounts_mod.candidates(self.store, "send_public_chat_message")],
            [803],
        )

    def test_disabled_account_is_not_a_candidate(self):
        self.store.execute("UPDATE accounts SET enabled = 0 WHERE id = 821")
        self.store.commit()
        self.assertEqual(
            accounts_mod.candidates(self.store, "send_private_dm"), []
        )

    def test_sync_is_idempotent(self):
        first = accounts_mod.sync(self.store, SNAPSHOT)
        self.assertEqual(first["added"], 0)
        self.assertEqual(first["updated"], 2)


class AccountIntakeTests(unittest.TestCase):
    """Приём чужих аккаунтов: приезжают в карантине, работать не начинают."""

    NEWCOMER = {
        "id": 861, "label": "moved-one", "program_code": "TGR1",
        "runtime_state": "running",
        "outreach": {
            "enabled": True, "roles": ["dm_sender"], "publish_inbound": True,
            "allow_immediate_visible_actions": True,
            "allowed_actions": ["send_private_dm", "reply_private_dm", "get_me"],
        },
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_new_accounts_arrive_paused(self):
        result = accounts_mod.sync(
            self.store, SNAPSHOT + [self.NEWCOMER], pause_new=True
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["paused_new"], [861])
        self.assertTrue(accounts_mod.get(self.store, 861)["paused"])

    def test_quarantined_account_gets_no_work(self):
        accounts_mod.sync(self.store, SNAPSHOT + [self.NEWCOMER], pause_new=True)
        ok, why = accounts_mod.usable(
            accounts_mod.get(self.store, 861), "send_private_dm"
        )
        self.assertFalse(ok)
        self.assertIn("паузе", why)
        self.assertEqual(
            [a["id"] for a in
             accounts_mod.candidates(self.store, "send_private_dm")],
            [821],
        )

    def test_already_working_accounts_are_not_touched(self):
        accounts_mod.sync(self.store, SNAPSHOT)
        result = accounts_mod.sync(
            self.store, SNAPSHOT + [self.NEWCOMER], pause_new=True
        )
        # Пауза касается только новичка: 821 работал и продолжает работать.
        self.assertEqual(result["paused_new"], [861])
        self.assertFalse(accounts_mod.get(self.store, 821)["paused"])

    def test_pause_survives_a_repeated_sync(self):
        accounts_mod.sync(self.store, SNAPSHOT + [self.NEWCOMER], pause_new=True)
        accounts_mod.sync(self.store, SNAPSHOT + [self.NEWCOMER])
        self.assertTrue(
            accounts_mod.get(self.store, 861)["paused"],
            "повторный sync снял карантин — так нельзя",
        )


class LegacyImportTests(unittest.TestCase):
    """Импорт истории: по аккаунтам и без очереди работы."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        self.src_path = Path(self.tmp.name) / "src.sqlite"
        self._build_source()
        self.src = sqlite3.connect(self.src_path)
        self.src.row_factory = sqlite3.Row

    def tearDown(self):
        self.src.close()
        self.store.close()
        self.tmp.cleanup()

    def _build_source(self):
        conn = sqlite3.connect(self.src_path)
        conn.executescript(
            """
            CREATE TABLE recipients (
              id TEXT PRIMARY KEY, telegram_username TEXT,
              telegram_channel_username TEXT, telegram_user_id TEXT,
              channel_chat_id TEXT, name TEXT, company TEXT, segment TEXT,
              notes TEXT, opt_out_status INTEGER DEFAULT 0,
              last_contacted_at TEXT, last_replied_at TEXT, created_at TEXT
            );
            CREATE TABLE opt_outs (recipient_id TEXT);
            CREATE TABLE conversations (
              id TEXT PRIMARY KEY, recipient_id TEXT, sender_account_id TEXT,
              campaign_id TEXT, state TEXT, last_message_at TEXT,
              last_inbound_at TEXT, last_outbound_at TEXT,
              handoff_status TEXT DEFAULT 'none', manager_owner TEXT,
              summary TEXT, created_at TEXT
            );
            CREATE TABLE messages (
              id TEXT PRIMARY KEY, conversation_id TEXT, direction TEXT,
              sender_type TEXT, telegram_message_id TEXT, text TEXT,
              sent_at TEXT, created_at TEXT
            );
            CREATE TABLE handoff_tasks (
              id TEXT PRIMARY KEY, conversation_id TEXT, status TEXT,
              reason TEXT, manager_owner TEXT, summary TEXT, created_at TEXT
            );
            """
        )
        # Один человек, с которым говорили ДВА разных аккаунта.
        conn.execute(
            "INSERT INTO recipients(id, telegram_username, name, segment, "
            "created_at) VALUES('r1', 'lead_moved', 'Иван', 'seg', ?)",
            ("2026-07-01T10:00:00+00:00",),
        )
        for cid, sender in (("cv1", "dm_sender_002"), ("cv2", "channel_sender_001")):
            conn.execute(
                "INSERT INTO conversations(id, recipient_id, sender_account_id, "
                "campaign_id, state, last_message_at, handoff_status, created_at) "
                "VALUES(?,?,?,NULL,'Replied',?, 'pending', ?)",
                (cid, "r1", sender, "2026-07-20T10:00:00+00:00",
                 "2026-07-01T10:00:00+00:00"),
            )
        conn.execute(
            "INSERT INTO messages(id, conversation_id, direction, sender_type, "
            "telegram_message_id, text, sent_at, created_at) "
            "VALUES('m1','cv1','inbound','recipient','7788','привет',?,?)",
            ("2026-07-20T10:00:00+00:00", "2026-07-20T10:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO handoff_tasks(id, conversation_id, status, reason, "
            "created_at) VALUES('ht1','cv1','new','ответил', ?)",
            ("2026-07-20T10:01:00+00:00",),
        )
        conn.commit()
        conn.close()

    def _run(self, **kwargs):
        contacts = legacy_import.import_contacts(self.src, self.store)
        threads = legacy_import.import_threads(
            self.src, self.store, contacts["mapping"], {}, **kwargs
        )
        history = legacy_import.import_history(
            self.src, self.store, threads["mapping"]
        )
        handoffs = legacy_import.import_handoffs(
            self.src, self.store, threads["mapping"],
            archive=kwargs.get("archive", False),
        )
        return threads, history, handoffs

    def test_dialogs_land_on_their_own_accounts(self):
        self._run(accounts={"dm_sender_002": 861, "channel_sender_001": 862})
        owners = {
            int(row["account_id"])
            for row in self.store.query("SELECT account_id FROM threads")
        }
        self.assertEqual(owners, {861, 862})

    def test_two_accounts_talking_to_one_person_are_not_merged(self):
        threads, _, _ = self._run(
            accounts={"dm_sender_002": 861, "channel_sender_001": 862}
        )
        # У человека один, но переписки две — их вели разные аккаунты.
        self.assertEqual(threads["imported"], 2)
        self.assertEqual(threads["merged_conversations"], 0)

    def test_unmapped_sender_is_reported_not_hidden(self):
        threads, _, _ = self._run(accounts={"dm_sender_002": 861})
        self.assertEqual(threads["unmapped_senders"], {"channel_sender_001": 1})

    def test_archive_leaves_no_work_queue(self):
        self._run(accounts={"dm_sender_002": 861}, archive=True)
        pending = self.store.one(
            "SELECT count(*) AS n FROM handoffs WHERE status IN ('new','taken')"
        )["n"]
        self.assertEqual(pending, 0, "архив создал очередь менеджеру")
        states = {
            row["state"]
            for row in self.store.query("SELECT DISTINCT state FROM threads")
        }
        self.assertEqual(states, {"closed"})

    def test_without_archive_the_queue_appears(self):
        # Обратная проверка: флаг действительно что-то меняет.
        self._run(accounts={"dm_sender_002": 861})
        pending = self.store.one(
            "SELECT count(*) AS n FROM handoffs WHERE status IN ('new','taken')"
        )["n"]
        self.assertEqual(pending, 1)

    def test_telegram_message_id_survives_for_a_later_radar_backfill(self):
        self._run(accounts={"dm_sender_002": 861}, archive=True)
        row = self.store.one("SELECT source_ref FROM history WHERE id = 'h_imp_m1'")
        self.assertEqual(row["source_ref"], "7788")


class PaceFloorTests(unittest.TestCase):
    """Пауза между отправками держится на ВЫПУСКЕ, а не только в плане.

    Планировщик раскладывает задачи по слотам, но в базу они попадают и мимо
    него: повторный plan, вторая кампания, импорт, правка руками. Инцидент
    01.08 на соседнем контуре выглядел ровно так — пакет ушёл одной секундой.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        self.settings.limits.per_account_daily_visible = 12
        self.settings.limits.per_account_visible_interval_sec = 900
        self.settings.limits.send_window_start_hour = 0
        self.settings.limits.send_window_end_hour = 24
        self.settings.limits.send_weekdays = (0, 1, 2, 3, 4, 5, 6)
        entities.add_template(self.store, "t", "Привет!", template_id="t1")
        entities.add_campaign(
            self.store, name="c", action="send_private_dm", template_id="t1",
            segment="s", campaign_id="pace", per_account_daily_cap=99,
            daily_cap=99,
        )
        entities.set_campaign_status(self.store, "pace", "active")
        for i in range(4):
            entities.add_contact(self.store, username=f"lead_pace{i}", segment="s")
        planner.plan(self.store, "pace", limits=self.settings.limits, dry_run=False)
        # Все задачи созрели и все на одном аккаунте — как если бы их вставили
        # мимо планировщика.
        self.store.execute(
            "UPDATE tasks SET scheduled_at = '2000-01-01T00:00:00+00:00', "
            "account_id = 821"
        )
        self.store.commit()
        dispatcher.arm(self.settings, True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_only_one_goes_out_per_run(self):
        bridge = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(result["dispatched"], 1)
        self.assertEqual(len(bridge.calls), 1)
        self.assertTrue(
            any("ждём ещё" in b["why"] for b in result["blocked"]),
            result["blocked"],
        )

    def test_next_run_is_still_held(self):
        run_dispatch(self.store, self.settings, FakeEnqueueBridge(), confirm=True)
        second = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, second, confirm=True)
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(second.calls, [])

    def test_preview_shows_the_same_single_task(self):
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertEqual(result["would_dispatch"], 1)

    def test_pause_is_measured_per_account(self):
        run_dispatch(self.store, self.settings, FakeEnqueueBridge(), confirm=True)
        self.assertIsNotNone(dispatcher.last_visible_attempt_at(self.store, 821))
        self.assertIsNone(dispatcher.last_visible_attempt_at(self.store, 803))

    def test_zero_interval_disables_the_pause(self):
        self.settings.limits.per_account_visible_interval_sec = 0
        result = run_dispatch(self.store, self.settings, FakeEnqueueBridge(),
                              confirm=True)
        self.assertEqual(result["dispatched"], 4)


class JitterTests(unittest.TestCase):
    """Слоты не должны ложиться на ровную сетку — и не должны падать под пол."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        self.limits = self.settings.limits
        self.limits.per_account_visible_interval_sec = 900
        self.limits.per_account_visible_jitter_sec = 420
        self.limits.send_window_start_hour = 0
        self.limits.send_window_end_hour = 24
        self.limits.send_weekdays = (0, 1, 2, 3, 4, 5, 6)
        entities.add_template(self.store, "t", "Привет!", template_id="t1")
        entities.add_campaign(
            self.store, name="c", action="send_private_dm", template_id="t1",
            segment="s", campaign_id="jit", per_account_daily_cap=12,
            daily_cap=99,
        )
        for i in range(10):
            entities.add_contact(self.store, username=f"lead_jit{i}", segment="s")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _gaps(self) -> list[int]:
        rows = self.store.query(
            "SELECT scheduled_at FROM tasks WHERE account_id = 821 "
            "ORDER BY scheduled_at"
        )
        moments = [datetime.fromisoformat(r["scheduled_at"]) for r in rows]
        return [
            int((b - a).total_seconds()) for a, b in zip(moments, moments[1:])
        ]

    def test_gaps_never_fall_below_the_dispatch_floor(self):
        planner.plan(self.store, "jit", limits=self.limits, dry_run=False,
                     rng=random.Random(1))
        gaps = self._gaps()
        self.assertTrue(gaps)
        for gap in gaps:
            self.assertGreaterEqual(
                gap, self.limits.per_account_visible_interval_sec,
                f"слот провалился под пол: {gap} с",
            )

    def test_gaps_are_not_a_fixed_grid(self):
        planner.plan(self.store, "jit", limits=self.limits, dry_run=False,
                     rng=random.Random(1))
        self.assertGreater(len(set(self._gaps())), 1, "интервалы одинаковые")

    def test_jitter_never_exceeds_its_span(self):
        planner.plan(self.store, "jit", limits=self.limits, dry_run=False,
                     rng=random.Random(7))
        ceiling = (self.limits.per_account_visible_interval_sec
                   + self.limits.per_account_visible_jitter_sec)
        for gap in self._gaps():
            self.assertLessEqual(gap, ceiling, f"разброс вышел за границу: {gap} с")

    def test_zero_jitter_keeps_the_old_behaviour(self):
        self.limits.per_account_visible_jitter_sec = 0
        planner.plan(self.store, "jit", limits=self.limits, dry_run=False,
                     rng=random.Random(1))
        self.assertEqual(
            set(self._gaps()), {self.limits.per_account_visible_interval_sec}
        )


class LimitsFloorTests(unittest.TestCase):
    """`limits.json` может ужесточать темп, но не смягчать его.

    Значения взяты те самые, что нашлись на соседнем контуре 01.08 — с ними
    рассылка ушла залпом.
    """

    LOOSE = {
        "per_account_daily_visible": 10000,
        "per_account_visible_interval_sec": 0,
        "dispatch_batch": 5000,
        "send_window_start_hour": 0,
        "send_window_end_hour": 24,
        "send_weekdays": [0, 1, 2, 3, 4, 5, 6],
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "var").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _load(self, raw: dict):
        (self.home / "var" / "limits.json").write_text(
            json.dumps(raw), encoding="utf-8"
        )
        return config.load(self.home)

    def test_loose_file_is_clamped_to_the_floor(self):
        settings = self._load(self.LOOSE)
        limits = settings.limits
        self.assertEqual(limits.per_account_daily_visible,
                         config.HARD_MAX_DAILY_VISIBLE)
        self.assertEqual(limits.per_account_visible_interval_sec,
                         config.HARD_MIN_INTERVAL_SEC)
        self.assertEqual(limits.dispatch_batch, config.HARD_MAX_DISPATCH_BATCH)
        self.assertEqual(limits.send_window_start_hour,
                         config.HARD_WINDOW_START_HOUR)
        self.assertEqual(limits.send_window_end_hour, config.HARD_WINDOW_END_HOUR)

    def test_clamping_is_reported_not_silent(self):
        settings = self._load(self.LOOSE)
        self.assertTrue(settings.limits_notes)
        joined = " ".join(settings.limits_notes)
        self.assertIn("per_account_visible_interval_sec", joined)

    def test_stricter_values_pass_through_untouched(self):
        settings = self._load({
            "per_account_daily_visible": 4,
            "per_account_visible_interval_sec": 3600,
            "dispatch_batch": 5,
            "send_window_start_hour": 11,
            "send_window_end_hour": 18,
            "send_weekdays": [0, 1, 2],
        })
        self.assertEqual(settings.limits.per_account_daily_visible, 4)
        self.assertEqual(settings.limits.per_account_visible_interval_sec, 3600)
        self.assertEqual(settings.limits.dispatch_batch, 5)
        self.assertEqual(settings.limits.send_window_start_hour, 11)
        self.assertEqual(settings.limits.send_weekdays, (0, 1, 2))
        self.assertEqual(settings.limits_notes, [])

    def test_defaults_without_a_file_are_the_shipped_ones(self):
        settings = config.load(self.home)
        self.assertEqual(settings.limits.per_account_daily_visible, 12)
        self.assertEqual(settings.limits.per_account_visible_interval_sec, 900)
        self.assertEqual(settings.limits.dispatch_batch, 25)

    def test_default_home_is_our_installation(self):
        # Форк унаследовал /opt/bridge49, и команда, запущенная без
        # BRIDGE49_HOME, открывала базу чужого контура — с записью в неё.
        self.assertEqual(str(config.DEFAULT_HOME), "/opt/outreach49")


def _seed_campaign(store, settings, *, contacts: int, campaign_id: str,
                   segment: str, allow_repeat: bool = False,
                   account_id: int = 821) -> None:
    """Кампания с созревшими задачами на одном аккаунте."""
    if not store.one("SELECT id FROM templates WHERE id = 't1'"):
        entities.add_template(store, "t", "Привет, {name}!", template_id="t1")
    entities.add_campaign(
        store, name=campaign_id, action="send_private_dm", template_id="t1",
        segment=segment, campaign_id=campaign_id, per_account_daily_cap=99,
        daily_cap=99, allow_repeat_contacts=allow_repeat,
    )
    entities.set_campaign_status(store, campaign_id, "active")
    for i in range(contacts):
        entities.add_contact(store, username=f"{segment}_lead{i}",
                             display_name=f"Имя{i}", segment=segment)
    planner.plan(store, campaign_id, limits=settings.limits, dry_run=False)
    store.execute(
        "UPDATE tasks SET scheduled_at = '2000-01-01T00:00:00+00:00', "
        "account_id = ? WHERE campaign_id = ?", (account_id, campaign_id),
    )
    store.commit()


class FleetPaceTests(unittest.TestCase):
    """Пауза между аккаунтами: отправки не должны складываться в залп."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        limits = self.settings.limits
        limits.global_visible_interval_min_sec = 10
        limits.global_visible_interval_max_sec = 20
        limits.per_account_visible_interval_sec = 0
        limits.send_window_start_hour = 0
        limits.send_window_end_hour = 24
        limits.send_weekdays = (0, 1, 2, 3, 4, 5, 6)
        _seed_campaign(self.store, self.settings, contacts=3,
                       campaign_id="fleet", segment="fl")
        dispatcher.arm(self.settings, True)
        self.slept: list[float] = []

        async def fake_sleep(seconds):
            # Моделируем течение времени: без этого пауза так и висела бы в
            # будущем, и проверялось бы не ожидание, а его отсутствие.
            self.slept.append(seconds)
            past = datetime.now(timezone.utc) - timedelta(seconds=1)
            self.store.set_state(dispatcher.GLOBAL_NEXT_KEY, past.isoformat())
            self.store.commit()

        self._real_sleep = dispatcher.asyncio.sleep
        dispatcher.asyncio.sleep = fake_sleep

    def tearDown(self):
        dispatcher.asyncio.sleep = self._real_sleep
        self.store.close()
        self.tmp.cleanup()

    def test_batch_goes_out_but_waits_between_sends(self):
        bridge = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        # Батч не режется: пауза выдерживается ожиданием, а не пропуском.
        self.assertEqual(result["dispatched"], 3)
        self.assertEqual(len(self.slept), 2)
        for waited in self.slept:
            self.assertGreaterEqual(waited, 1)
            self.assertLessEqual(waited, 21)

    def test_pause_survives_the_run(self):
        run_dispatch(self.store, self.settings, FakeEnqueueBridge(), confirm=True)
        self.assertIsNotNone(dispatcher.global_next_visible_at(self.store))

    def test_absurd_pause_defers_instead_of_sleeping(self):
        self.settings.limits.global_visible_interval_min_sec = 3600
        self.settings.limits.global_visible_interval_max_sec = 3600
        result = run_dispatch(self.store, self.settings, FakeEnqueueBridge(),
                              confirm=True)
        self.assertEqual(result["dispatched"], 1)
        self.assertTrue(result["deferred"], result)
        self.assertEqual(self.slept, [])

    def test_preview_does_not_apply_the_fleet_pause(self):
        # Пауза висит в будущем, но предпросмотр отвечает на вопрос «что уйдёт
        # за прогон», а уйдёт весь батч — просто с ожиданием между отправками.
        future = datetime.now(timezone.utc) + timedelta(seconds=30)
        self.store.set_state(dispatcher.GLOBAL_NEXT_KEY, future.isoformat())
        self.store.commit()
        result = asyncio.run(
            dispatcher.dispatch(self.store, self.settings, confirm=False)
        )
        self.assertEqual(result["would_dispatch"], 3)


class ContactTouchTests(unittest.TestCase):
    """Одному человеку — одно первое касание, сколько бы кампаний ни было."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        limits = self.settings.limits
        limits.per_account_visible_interval_sec = 0
        limits.send_window_start_hour = 0
        limits.send_window_end_hour = 24
        limits.send_weekdays = (0, 1, 2, 3, 4, 5, 6)
        _seed_campaign(self.store, self.settings, contacts=2,
                       campaign_id="first", segment="tw")
        dispatcher.arm(self.settings, True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _second_campaign(self, allow_repeat: bool = False) -> None:
        entities.add_campaign(
            self.store, name="second", action="send_private_dm",
            template_id="t1", segment="tw", campaign_id="second",
            per_account_daily_cap=99, daily_cap=99,
            allow_repeat_contacts=allow_repeat,
        )
        entities.set_campaign_status(self.store, "second", "active")
        planner.plan(self.store, "second", limits=self.settings.limits,
                     dry_run=False)
        self.store.execute(
            "UPDATE tasks SET scheduled_at = '2000-01-01T00:00:00+00:00', "
            "account_id = 821 WHERE campaign_id = 'second'"
        )
        self.store.commit()

    def test_touch_is_recorded_on_send(self):
        run_dispatch(self.store, self.settings, FakeEnqueueBridge(), confirm=True)
        touched = self.store.query("SELECT contact_id FROM contact_touches")
        self.assertEqual(len(touched), 2)

    def test_second_campaign_does_not_plan_touched_contacts(self):
        run_dispatch(self.store, self.settings, FakeEnqueueBridge(), confirm=True)
        self._second_campaign()
        planned = self.store.query(
            "SELECT id FROM tasks WHERE campaign_id = 'second'"
        )
        self.assertEqual(planned, [])

    def test_repeat_is_blocked_even_if_a_task_appears_anyway(self):
        run_dispatch(self.store, self.settings, FakeEnqueueBridge(), confirm=True)
        # Задача мимо планировщика: правка руками, импорт, повторный plan.
        contact = self.store.one("SELECT contact_id FROM contact_touches")
        self._second_campaign(allow_repeat=True)
        self.store.execute(
            "UPDATE campaigns SET allow_repeat_contacts = 0 WHERE id = 'second'"
        )
        self.store.commit()
        bridge = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(bridge.calls, [])
        self.assertTrue(
            any("уже писали" in b["why"] for b in result["blocked"]),
            result["blocked"],
        )
        self.assertTrue(contact)

    def test_explicit_allow_repeat_lets_the_second_wave_through(self):
        run_dispatch(self.store, self.settings, FakeEnqueueBridge(), confirm=True)
        self._second_campaign(allow_repeat=True)
        bridge = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(result["dispatched"], 2)


class FailedAttemptTests(unittest.TestCase):
    """Отказ моста расходует темп: иначе череда ошибок разгоняет отправку."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        limits = self.settings.limits
        limits.per_account_visible_interval_sec = 900
        limits.send_window_start_hour = 0
        limits.send_window_end_hour = 24
        limits.send_weekdays = (0, 1, 2, 3, 4, 5, 6)
        _seed_campaign(self.store, self.settings, contacts=3,
                       campaign_id="fail", segment="fa")
        dispatcher.arm(self.settings, True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_rejected_attempt_holds_the_account(self):
        bridge = FakeEnqueueBridge(error=radar.BridgeRejected("нельзя"))
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        # Отказ пришёл один раз, дальше аккаунт держит пауза.
        self.assertEqual(len(bridge.calls), 1)
        self.assertEqual(len(result["failed"]), 1)
        self.assertTrue(
            any("меньше 900" in b["why"] for b in result["blocked"]),
            result["blocked"],
        )

    def test_attempt_counts_towards_the_daily_budget(self):
        bridge = FakeEnqueueBridge(error=radar.BridgeRejected("нельзя"))
        run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(dispatcher.visible_sent_today(self.store, 821), 1)

    def test_unknown_outcome_also_records_the_touch(self):
        bridge = FakeEnqueueBridge(error=radar.BridgeUnknown("таймаут"))
        run_dispatch(self.store, self.settings, bridge, confirm=True)
        # Исход неизвестен: считаем, что дошло, и повторно не пишем.
        self.assertEqual(
            len(self.store.query("SELECT contact_id FROM contact_touches")), 1
        )

    def test_rejected_attempt_does_not_record_a_touch(self):
        bridge = FakeEnqueueBridge(error=radar.BridgeRejected("нельзя"))
        run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(self.store.query("SELECT contact_id FROM contact_touches"), [])


class WatchdogTests(unittest.TestCase):
    """Тишина контура неотличима от нормы — сторож смотрит на давность."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run(self):
        return asyncio.run(
            watchdog.run(self.store, self.settings, with_bridge=False)
        )

    def _poll_logged(self, *, minutes_ago: int) -> None:
        moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        self.store.execute(
            "INSERT INTO events(at, actor, kind, subject, detail) "
            "VALUES(?, 'timer', 'poll.inbound', '', '')",
            (moment.isoformat(),),
        )
        self.store.commit()

    def test_silent_poller_is_critical(self):
        self._poll_logged(minutes_ago=30)
        result = self._run()
        self.assertFalse(result.ok)
        self.assertEqual(result.worst, watchdog.CRITICAL)
        self.assertTrue(any(f.check == "поллер" for f in result.findings))

    def test_fresh_poll_is_quiet(self):
        self._poll_logged(minutes_ago=0)
        result = self._run()
        self.assertTrue(result.ok, result.as_dict())

    def test_never_polled_is_critical(self):
        result = self._run()
        self.assertEqual(result.worst, watchdog.CRITICAL)

    def test_armed_without_accounts_is_high(self):
        self._poll_logged(minutes_ago=0)
        self.store.execute("UPDATE accounts SET paused = 1")
        self.store.commit()
        dispatcher.arm(self.settings, True)
        result = self._run()
        self.assertEqual(result.worst, watchdog.HIGH)
        self.assertTrue(any(f.check == "аккаунты" for f in result.findings))

    def test_stale_handoff_is_only_a_warning(self):
        self._poll_logged(minutes_ago=0)
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, surface, created_at, "
            "updated_at) VALUES('th1', 821, '@x', 'private_dm', ?, ?)",
            (old, old),
        )
        self.store.execute(
            "INSERT INTO handoffs(id, thread_id, reason, status, created_at, "
            "updated_at) VALUES('h1', 'th1', 'ответ', 'new', ?, ?)",
            (old, old),
        )
        self.store.commit()
        result = self._run()
        self.assertEqual(result.worst, watchdog.WARNING)

    def test_state_file_is_written(self):
        self._poll_logged(minutes_ago=0)
        self._run()
        payload = json.loads(
            (self.settings.home / "var" / "watchdog.json").read_text("utf-8")
        )
        self.assertTrue(payload["ok"])
        self.assertIn("last_poll_at", payload["facts"])

    def test_only_state_changes_are_logged(self):
        self._poll_logged(minutes_ago=0)
        self._run()
        self._run()
        events = self.store.query(
            "SELECT id FROM events WHERE kind = 'watchdog'"
        )
        self.assertEqual(len(events), 1)

    def test_fingerprint_notices_a_new_problem_at_the_same_level(self):
        first = watchdog.Report(checked_at="t")
        first.findings.append(watchdog.Finding("поллер", watchdog.CRITICAL, "x"))
        second = watchdog.Report(checked_at="t")
        second.findings.append(watchdog.Finding("поллер", watchdog.CRITICAL, "x"))
        second.findings.append(watchdog.Finding("мост", watchdog.CRITICAL, "y"))
        self.assertNotEqual(
            watchdog.fingerprint(first), watchdog.fingerprint(second)
        )

    def test_fingerprint_ignores_changing_detail(self):
        first = watchdog.Report(checked_at="t")
        first.findings.append(
            watchdog.Finding("поллер", watchdog.CRITICAL, "5 мин назад")
        )
        second = watchdog.Report(checked_at="t")
        second.findings.append(
            watchdog.Finding("поллер", watchdog.CRITICAL, "7 мин назад")
        )
        self.assertEqual(watchdog.fingerprint(first), watchdog.fingerprint(second))


class AlertDeliveryTests(unittest.TestCase):
    """Тревога должна доехать до админки — и не уронить сторожа, если нет."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        self.env = Path(self.tmp.name) / "alerts.env"
        self.env.write_text(
            'OUTREACH_OPS_TELEGRAM_ENABLED="1"\n'
            'OUTREACH_OPS_TELEGRAM_BOT_TOKEN="123:ABC"\n'
            'OUTREACH_OPS_TELEGRAM_CHAT_ID="-1003374720972"\n'
            'OUTREACH_OPS_TELEGRAM_THREAD_ID="69282"\n',
            encoding="utf-8",
        )
        self.sent: list[str] = []
        self._real_send = alerts.send
        self._real_from_file = alerts.TelegramTarget.from_file
        alerts.TelegramTarget.from_file = classmethod(
            lambda cls, path=None: self._real_from_file(self.env)
        )

    def tearDown(self):
        alerts.send = self._real_send
        alerts.TelegramTarget.from_file = self._real_from_file
        self.store.close()
        self.tmp.cleanup()

    def _capture(self):
        def fake_send(target, text):
            self.sent.append(text)
            return 4242
        alerts.send = fake_send

    def _run(self):
        return asyncio.run(
            watchdog.run(self.store, self.settings, with_bridge=False)
        )

    def test_target_is_read_from_the_env_file(self):
        target = alerts.TelegramTarget.from_file()
        self.assertEqual(target.chat_id, "-1003374720972")
        self.assertEqual(target.thread_id, "69282")
        self.assertTrue(target.enabled)
        # Токен не должен утекать в описание для логов.
        self.assertNotIn("123:ABC", target.describe())

    def test_problem_is_delivered(self):
        self._capture()
        self._run()
        self.assertEqual(len(self.sent), 1)
        self.assertIn("поллер", self.sent[0])

    def test_repeated_state_is_not_delivered_twice(self):
        self._capture()
        self._run()
        self._run()
        self.assertEqual(len(self.sent), 1)

    def test_recovery_is_delivered(self):
        self._capture()
        self._run()
        self.store.execute(
            "INSERT INTO events(at, actor, kind, subject, detail) "
            "VALUES(?, 'timer', 'poll.inbound', '', '')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.store.commit()
        self._run()
        self.assertEqual(len(self.sent), 2)
        self.assertIn("восстановилось", self.sent[1])

    def test_first_quiet_run_stays_silent(self):
        self._capture()
        self.store.execute(
            "INSERT INTO events(at, actor, kind, subject, detail) "
            "VALUES(?, 'timer', 'poll.inbound', '', '')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        self.store.commit()
        self._run()
        self.assertEqual(self.sent, [])

    def test_delivery_failure_does_not_break_the_watchdog(self):
        def failing_send(target, text):
            raise alerts.AlertError("HTTP 400: chat not found")
        alerts.send = failing_send
        result = self._run()
        self.assertFalse(result.ok)
        logged = self.store.query(
            "SELECT detail FROM events WHERE kind = 'watchdog.alert_failed'"
        )
        self.assertEqual(len(logged), 1)
        self.assertIn("chat not found", logged[0]["detail"])

    def test_missing_config_means_no_delivery(self):
        alerts.TelegramTarget.from_file = classmethod(lambda cls, path=None: None)
        self._capture()
        result = self._run()
        self.assertFalse(result.ok)
        self.assertEqual(self.sent, [])


class SendTests(unittest.TestCase):
    """Точечная отправка: у каждого получателя свой текст, кампании нет."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        limits = self.settings.limits
        limits.per_account_visible_interval_sec = 0
        limits.send_window_start_hour = 0
        limits.send_window_end_hour = 24
        limits.send_weekdays = (0, 1, 2, 3, 4, 5, 6)
        dispatcher.arm(self.settings, True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_private_send_carries_its_own_text(self):
        result = replies.queue_send(
            self.store, account_id=821, text="персональная ссылка", username="lead"
        )
        task = self.store.one(
            "SELECT action, params FROM tasks WHERE id = ?", (result["task"],)
        )
        self.assertEqual(task["action"], "send_private_dm")
        self.assertEqual(json.loads(task["params"])["text"], "персональная ссылка")

    def test_channel_send_keeps_both_route_ids(self):
        result = replies.queue_send(
            self.store, account_id=821, text="ссылка", username="autoimport27",
            kind="channel_dm", channel_tg_id=-1001763001372,
            monoforum_tg_id=-2071763001372,
        )
        params = json.loads(self.store.one(
            "SELECT params FROM tasks WHERE id = ?", (result["task"],)
        )["params"])
        self.assertEqual(params["target_channel_tg_id"], -1001763001372)
        self.assertEqual(params["target_monoforum_tg_id"], -2071763001372)

    def test_repeat_does_not_queue_a_second_message(self):
        replies.queue_send(self.store, account_id=821, text="раз", username="lead")
        with self.assertRaises(replies.ReplyError):
            replies.queue_send(self.store, account_id=821, text="два", username="lead")

    def test_channel_send_needs_a_username(self):
        with self.assertRaises(replies.ReplyError):
            replies.queue_send(
                self.store, account_id=821, text="т", kind="channel_dm",
                monoforum_tg_id=-2071763001372,
            )

    def test_default_mode_is_immediate(self):
        # lottery ждёт розыгрыша события outreach_command, которого нет ни у
        # одного аккаунта: такая команда остаётся в Radar со статусом new
        # навсегда. Прежний контур все отправки делал только immediate.
        result = replies.queue_send(
            self.store, account_id=821, text="ссылка", username="lead"
        )
        self.assertEqual(result["mode"], "immediate")
        task = self.store.one(
            "SELECT mode FROM tasks WHERE id = ?", (result["task"],)
        )
        self.assertEqual(task["mode"], "immediate")

    def test_send_goes_through_the_usual_gates(self):
        self.settings.limits.send_weekdays = ()
        replies.queue_send(self.store, account_id=821, text="ссылка", username="lead")
        bridge = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(bridge.calls, [])


class ReplyTests(unittest.TestCase):
    """Ответ адресуется входящему, а не username, и слушается тех же ворот."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store, self.settings = make_env(Path(self.tmp.name))
        limits = self.settings.limits
        limits.per_account_visible_interval_sec = 0
        limits.send_window_start_hour = 0
        limits.send_window_end_hour = 24
        limits.send_weekdays = (0, 1, 2, 3, 4, 5, 6)
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, surface, state, "
            "created_at, updated_at) "
            "VALUES('th1', 821, '@lead', 'private_dm', 'handoff', ?, ?)",
            (now(), now()),
        )
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, peer_username, "
            "peer_tg_id, text, sent_at, raw, created_at) "
            "VALUES(9001, 821, 'private_dm', '@lead', 'lead', 777, 'Здравствуйте!', "
            "?, '{}', ?)",
            (now(), now()),
        )
        self.store.commit()
        dispatcher.arm(self.settings, True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_reply_targets_the_inbound_notification(self):
        result = replies.queue_reply(
            self.store, text="Расскажу подробнее", thread_id="th1"
        )
        task = self.store.one(
            "SELECT action, params, account_id FROM tasks WHERE id = ?",
            (result["task"],),
        )
        self.assertEqual(task["action"], "reply_private_dm")
        self.assertEqual(int(task["account_id"]), 821)
        params = json.loads(task["params"])
        # Адресат берётся из входящего: username — алиас, он меняется.
        self.assertEqual(params["inbound_notification_id"], 9001)
        self.assertEqual(params["text"], "Расскажу подробнее")

    def test_contact_is_created_for_a_stranger(self):
        replies.queue_reply(self.store, text="Привет", account_id=821, peer="lead")
        thread = self.store.one("SELECT contact_id FROM threads WHERE id = 'th1'")
        self.assertIsNotNone(thread["contact_id"])
        contact = self.store.one(
            "SELECT username, segment FROM contacts WHERE id = ?",
            (thread["contact_id"],),
        )
        self.assertEqual(contact["username"], "lead")
        self.assertEqual(contact["segment"], "inbound")

    def test_second_reply_while_the_first_waits_is_refused(self):
        replies.queue_reply(self.store, text="раз", thread_id="th1")
        with self.assertRaises(replies.ReplyError):
            replies.queue_reply(self.store, text="два", thread_id="th1")

    def test_thread_without_inbound_cannot_be_answered(self):
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, surface, created_at, "
            "updated_at) VALUES('th2', 821, '@silent', 'private_dm', ?, ?)",
            (now(), now()),
        )
        self.store.commit()
        with self.assertRaises(replies.ReplyError):
            replies.queue_reply(self.store, text="ау", thread_id="th2")

    def test_reply_is_not_blocked_by_the_touch_guard(self):
        # Человеку уже писали — для первого касания это стоп, для ответа нет.
        replies.queue_reply(self.store, text="ответ", thread_id="th1")
        contact = self.store.one("SELECT contact_id FROM threads WHERE id='th1'")
        self.store.execute(
            "INSERT INTO contact_touches(contact_id, first_sent_at, last_sent_at, "
            "sent_count) VALUES(?,?,?,1)",
            (contact["contact_id"], now(), now()),
        )
        self.store.commit()
        bridge = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(result["dispatched"], 1, result)

    def test_reply_still_obeys_the_send_window(self):
        self.settings.limits.send_weekdays = ()
        replies.queue_reply(self.store, text="ответ", thread_id="th1")
        bridge = FakeEnqueueBridge()
        result = run_dispatch(self.store, self.settings, bridge, confirm=True)
        self.assertEqual(result["dispatched"], 0)
        self.assertEqual(bridge.calls, [])


if __name__ == "__main__":
    unittest.main()
