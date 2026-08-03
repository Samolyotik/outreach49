"""Развязка темпа: ответы и рассылка считаются раздельно.

Проверяем то, ради чего развязка затевалась, и то, что при этом не должно
было развязаться: Telegram видит один аккаунт, поэтому общий пол между любыми
видимыми действиями остаётся.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import config, dispatcher, entities  # noqa: E402
from bridge49.config import Limits, Settings  # noqa: E402
from bridge49.store import Store, new_id, now  # noqa: E402

SNAPSHOT = [
    {
        "id": 821, "label": "dm-one", "program_code": "TGR1",
        "runtime_state": "running",
        "outreach": {
            "enabled": True, "roles": ["dm_sender"], "publish_inbound": True,
            "allow_immediate_visible_actions": True,
            "allowed_actions": ["send_private_dm", "reply_private_dm"],
        },
    },
]


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.store = Store(tmp / "b.sqlite")
        accounts_mod.sync(self.store, SNAPSHOT)
        self.settings = Settings(
            home=tmp, db_path=tmp / "b.sqlite", dsn=None, limits=Limits(),
            timezone="Europe/Moscow",
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_task(self, action: str, *, when: datetime | None = None) -> str:
        """Отметить уже совершённую попытку — так набирается дневной расход."""
        contact = entities.add_contact(
            self.store, username=f"u{new_id('c')[:6]}", actor="test",
        )
        task_id = new_id("task")
        stamp = (when or datetime.now(timezone.utc)).isoformat()
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, segment, mode, status, "
            "daily_cap, per_account_daily_cap, params, ttl_hours, created_at, "
            "updated_at) VALUES(?,?,?,'','immediate','active',99,99,'{}',48,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (f"c_{action}", action, action, now(), now()),
        )
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, dispatched_at, created_at, "
            "updated_at) VALUES(?,?,?,821,?,'{}','immediate',?,'done',?,?,?)",
            (task_id, f"c_{action}", contact["id"], action, stamp, stamp,
             now(), now()),
        )
        self.store.commit()
        return task_id

    # -- бюджеты ------------------------------------------------------------

    def test_replies_do_not_spend_the_outreach_budget(self):
        for _ in range(5):
            self.add_task("reply_private_dm")

        self.assertEqual(
            dispatcher.visible_sent_today(self.store, 821,
                                          dispatcher.CADENCE_OUTREACH), 0)
        self.assertEqual(
            dispatcher.visible_sent_today(self.store, 821,
                                          dispatcher.CADENCE_REPLY), 5)

    def test_outreach_does_not_spend_the_reply_budget(self):
        for _ in range(3):
            self.add_task("send_private_dm")

        self.assertEqual(
            dispatcher.visible_sent_today(self.store, 821,
                                          dispatcher.CADENCE_REPLY), 0)
        self.assertEqual(
            dispatcher.visible_sent_today(self.store, 821,
                                          dispatcher.CADENCE_OUTREACH), 3)

    # -- паузы --------------------------------------------------------------

    def test_a_reply_does_not_delay_the_campaign(self):
        """Рассылка меряет паузу от прошлой рассылки, а не от ответа."""
        self.add_task("reply_private_dm")

        self.assertIsNone(
            dispatcher.last_visible_attempt_at(self.store, 821,
                                               dispatcher.CADENCE_OUTREACH))

    def test_a_campaign_send_still_holds_back_the_next_reply(self):
        """А вот ответ смотрит на любое действие: аккаунт-то один."""
        self.add_task("send_private_dm")

        last = dispatcher.last_visible_attempt_at(self.store, 821,
                                                  dispatcher.CADENCE_REPLY)
        self.assertIsNotNone(last)

    def test_fleet_pauses_are_kept_apart(self):
        self.store.set_state(
            dispatcher.GLOBAL_NEXT_KEY,
            (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        )

        self.assertIsNotNone(
            dispatcher.global_next_visible_at(self.store,
                                              dispatcher.CADENCE_OUTREACH))
        self.assertIsNone(
            dispatcher.global_next_visible_at(self.store,
                                              dispatcher.CADENCE_REPLY))

    # -- класс задачи -------------------------------------------------------

    def test_cadence_is_derived_from_the_action(self):
        self.assertEqual(dispatcher.cadence_of({"action": "reply_private_dm"}),
                         dispatcher.CADENCE_REPLY)
        self.assertEqual(dispatcher.cadence_of({"action": "send_private_dm"}),
                         dispatcher.CADENCE_OUTREACH)
        self.assertEqual(dispatcher.cadence_of({"action": "send_channel_dm"}),
                         dispatcher.CADENCE_OUTREACH)

    # -- пол ----------------------------------------------------------------

    def test_reply_limits_have_their_own_floor(self):
        limits = Limits()
        limits.reply_per_account_daily = 10_000
        limits.reply_per_account_interval_sec = 0

        notes = config.clamp(limits)

        self.assertEqual(limits.reply_per_account_daily,
                         config.HARD_MAX_REPLY_DAILY)
        self.assertEqual(limits.reply_per_account_interval_sec,
                         config.HARD_MIN_REPLY_INTERVAL_SEC)
        self.assertEqual(len(notes), 2, notes)

    def test_replies_run_round_the_clock(self):
        """Перенос поведения прежнего контура: require_send_window_for_auto_reply
        стоял в False и в коде, и в боевом конфиге."""
        limits = Limits()
        config.clamp(limits)
        settings = Settings(
            home=Path("/tmp"), db_path=Path("/tmp/x"), dsn=None, limits=limits,
            timezone="Europe/Moscow",
        )
        # Ночь воскресенья — худший случай и для часа, и для дня недели.
        night = datetime(2026, 8, 2, 0, 30, tzinfo=timezone.utc)

        self.assertTrue(dispatcher.inside_send_window(
            settings, night, cadence=dispatcher.CADENCE_REPLY))
        self.assertFalse(dispatcher.inside_send_window(
            settings, night, cadence=dispatcher.CADENCE_OUTREACH))


if __name__ == "__main__":
    unittest.main()
