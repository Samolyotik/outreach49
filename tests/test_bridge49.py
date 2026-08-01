"""Тесты bridge49. Стандартный unittest — никаких зависимостей.

Запуск:  python -m unittest discover -s tests -v
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import catalog, dispatcher, entities, planner, pollers, radar  # noqa: E402
from bridge49.config import Limits, Settings  # noqa: E402
from bridge49.store import Store  # noqa: E402

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
    settings = Settings(
        home=tmp, db_path=tmp / "b.sqlite", dsn=None, limits=Limits(),
        timezone="Europe/Moscow",
    )
    return store, settings


class CatalogTests(unittest.TestCase):
    def test_role_gate(self):
        with self.assertRaises(catalog.ValidationError):
            catalog.validate(
                "send_private_dm", {"username": "x", "text": "привет"},
                roles={"chat_sender"},
            )
        action = catalog.validate(
            "send_private_dm", {"username": "x", "text": "привет"},
            roles={"dm_sender"},
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


if __name__ == "__main__":
    unittest.main()
