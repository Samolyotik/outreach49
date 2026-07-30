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
from bridge49 import catalog, dispatcher, entities, planner  # noqa: E402
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
