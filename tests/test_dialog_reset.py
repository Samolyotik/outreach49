"""Сброс диалога: что он прячет, что снимает и чего не трогает.

Проверка идёт по гейтам, а не по таблицам: смысл сброса не в том, что строки
поменяли статус, а в том, что следующий тест начинается с чистого листа —
модель не помнит разговора, ссылка выдаётся заново, отказ не блокирует
отправку.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import autoreply, dialogs, direct_invite, entities  # noqa: E402
from bridge49 import outreach_texts, replies  # noqa: E402
from bridge49.config import Settings, Limits  # noqa: E402
from bridge49.store import Store, dumps, new_id, now  # noqa: E402

SNAPSHOT = [
    {
        "id": 821, "label": "dm-one", "program_code": "TGR1",
        "runtime_state": "running",
        "outreach": {
            "enabled": True, "roles": ["dm_sender"], "publish_inbound": True,
            "allow_immediate_visible_actions": True,
            "allowed_actions": ["reply_private_dm", "send_private_dm"],
        },
    },
]


def ago(hours: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(hours=hours)
    return moment.isoformat(timespec="seconds")


class DialogResetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = Store(self.home / "b.sqlite")
        accounts_mod.sync(self.store, SNAPSHOT)
        contact = entities.add_contact(
            self.store, username="tester", segment="inbound", actor="test")
        self.contact_id = contact["id"]
        self.thread_id = new_id("thread")
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, last_outbound_at, created_at, updated_at) "
            "VALUES(?,821,'@tester',?,'private_dm','open',?,?,?)",
            (self.thread_id, self.contact_id, ago(72), ago(72), now()),
        )
        # Первое касание — наше письмо трёхдневной давности.
        self.first_touch_text = "Здравствуйте. Это наше первое письмо."
        self.add_task("send_private_dm", self.first_touch_text, ago(72),
                      campaign_id="cold")
        # Разговор после него: его реплика, наш автоответ, его вторая реплика.
        self.add_inbound(5001, "Расскажите подробнее", ago(2))
        self.add_task("reply_private_dm", "Рассказываю подробнее",
                      ago(1.5), campaign_id=replies.AUTO_CAMPAIGN_ID)
        self.add_inbound(5002, "А сколько стоит?", ago(1))
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # -- вспомогательное -----------------------------------------------------

    def add_task(self, action, text, stamp, *, campaign_id="cold",
                 state="done", request_id=None):
        replies.ensure_reply_campaign(self.store, campaign_id, campaign_id, "")
        task_id = new_id("task")
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "  params, mode, scheduled_at, state, request_id, dispatched_at, "
            "  created_at, updated_at) "
            "VALUES(?,?,?,821,?,?,'immediate',?,?,?,?,?,?)",
            (task_id, campaign_id, self.contact_id, action,
             dumps({"text": text}), stamp, state, request_id,
             stamp if state == "done" else None, stamp, stamp),
        )
        self.store.commit()
        return task_id

    def add_inbound(self, ident, text, stamp, *, handled=1):
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, peer_tg_id, text, sent_at, raw, contact_id, "
            "handled, created_at) "
            "VALUES(?,821,'private_dm','@tester','tester',7001,?,?,'{}',?,?,?)",
            (ident, text, stamp, self.contact_id, handled, stamp),
        )
        self.store.commit()

    def thread(self):
        return dict(self.store.one(
            "SELECT * FROM threads WHERE id = ?", (self.thread_id,)))

    def texts(self):
        return [item["text"]
                for item in autoreply.conversation_history(
                    self.store, self.thread())]

    def settings(self, peers: str | None = None):
        if peers is not None:
            (self.home / "var").mkdir(parents=True, exist_ok=True)
            (self.home / "var" / "TEST_PEERS").write_text(
                peers, encoding="utf-8")
        return Settings(home=self.home, db_path=self.home / "b.sqlite",
                        dsn=None, limits=Limits())

    # -- переписка -----------------------------------------------------------

    def test_reset_keeps_first_touch_only(self):
        """После сброса модель видит наше письмо и больше ничего."""
        self.assertEqual(len(self.texts()), 4)
        dialogs.reset_thread(self.store, self.thread(), actor="test")
        self.assertEqual(self.texts(), [self.first_touch_text])

    def test_full_reset_hides_first_touch_too(self):
        dialogs.reset_thread(self.store, self.thread(), full=True, actor="test")
        self.assertEqual(self.texts(), [])

    def test_new_message_after_reset_is_visible(self):
        """Сброс прячет прошлое, а не всё подряд."""
        dialogs.reset_thread(self.store, self.thread(), actor="test")
        self.add_inbound(5003, "Здравствуйте!", now())
        self.assertEqual(self.texts(), [self.first_touch_text, "Здравствуйте!"])

    def test_message_in_the_same_second_survives(self):
        """Граница включающая: время у нас с точностью до секунды.

        Строгая граница прятала бы первое сообщение теста, если оно попало в
        секунду сброса, — и модель получала бы ход без его же текста.
        """
        stamp = now()
        dialogs.reset_thread(self.store, self.thread(), actor="test", at=stamp)
        self.add_inbound(5009, "первое сообщение теста", stamp)
        self.assertIn("первое сообщение теста", self.texts())

    def test_control_command_never_shows_in_history(self):
        self.add_inbound(5010, "##reset49", now())
        dialogs.reset_thread(self.store, self.thread(), actor="test")
        self.assertNotIn("##reset49", self.texts())

    def test_undo_returns_everything(self):
        dialogs.reset_thread(self.store, self.thread(), actor="test")
        self.assertTrue(dialogs.undo_reset(self.store, self.thread(),
                                           actor="test"))
        self.assertEqual(len(self.texts()), 4)

    def test_auto_reply_count_starts_over(self):
        self.assertEqual(autoreply.auto_reply_count(self.store, self.thread()), 1)
        dialogs.reset_thread(self.store, self.thread(), actor="test")
        self.assertEqual(autoreply.auto_reply_count(self.store, self.thread()), 0)

    def test_peer_is_still_ours_after_reset(self):
        """Гейт «мы начали разговор» сброс не трогает.

        Иначе тестовый собеседник стал бы посторонним, и автоответ выключился
        бы совсем — то есть сброс ломал бы ровно то, ради чего он сделан.
        """
        dialogs.reset_thread(self.store, self.thread(), full=True, actor="test")
        self.assertTrue(autoreply.we_started_it(self.store, self.thread()))

    # -- гейты повторной выдачи ----------------------------------------------

    def test_invites_and_demo_unblocked(self):
        self.store.execute(
            "INSERT INTO direct_invites(id, request_id, thread_id, contact_id, "
            "  account_id, inbound_id, source_channel, outreach_sector_id, "
            "  sector_id, sector_name, test_group_profile_id, "
            "  consent_recorded_at, consent_source, status, created_at, "
            "  updated_at) "
            "VALUES('dinv_1','req_1',?,?,821,'5001','private_dm','s','s','S',"
            "'p',?,'engine',?,?,?)",
            (self.thread_id, self.contact_id, now(),
             direct_invite.STATUS_DELIVERED, now(), now()),
        )
        self.store.execute(
            "INSERT INTO demo_invites(id, contact_id, thread_id, account_id, "
            "  inbound_id, source_channel, canonical_sector_id, sector_status, "
            "  status, created_at, updated_at) "
            "VALUES('demo_1',?,?,821,'5001','private_dm','other','unknown',?,?,?)",
            (self.contact_id, self.thread_id, direct_invite.DEMO_STATUS_DELIVERED,
             now(), now()),
        )
        self.store.commit()
        self.assertTrue(direct_invite.demo_invite_blocked_by_personal_link(
            self.store, self.contact_id))

        dialogs.reset_thread(self.store, self.thread(), actor="test")

        self.assertFalse(direct_invite.demo_invite_blocked_by_personal_link(
            self.store, self.contact_id))
        demo = self.store.one("SELECT status FROM demo_invites WHERE id='demo_1'")
        self.assertEqual(demo["status"], direct_invite.DEMO_STATUS_CANCELLED)

    def test_opt_out_and_handoff_cleared(self):
        entities.opt_out(self.store, self.contact_id, "не пишите", actor="test") \
            if hasattr(entities, "opt_out") else self.store.execute(
                "UPDATE contacts SET opted_out = 1, status = 'closed' WHERE id = ?",
                (self.contact_id,))
        handoff_id = autoreply.open_handoff(
            self.store, self.thread(), "knowledge_gap", "не знаю ответа")
        self.store.commit()

        dialogs.reset_thread(self.store, self.thread(), actor="test")

        contact = self.store.one(
            "SELECT opted_out, status FROM contacts WHERE id = ?",
            (self.contact_id,))
        self.assertEqual(contact["opted_out"], 0)
        self.assertEqual(contact["status"], "contacted")
        card = self.store.one("SELECT status FROM handoffs WHERE id = ?",
                              (handoff_id,))
        self.assertEqual(card["status"], "closed")
        self.assertEqual(self.thread()["state"], "open")

    def test_pending_reply_cancelled_but_dispatched_left_alone(self):
        """Снимаем только то, что ещё никуда не уехало."""
        planned = self.add_task("reply_private_dm", "черновик", now(),
                                campaign_id=replies.AUTO_CAMPAIGN_ID,
                                state="planned")
        flying = self.add_task("reply_private_dm", "уже в Radar", now(),
                               campaign_id="manual_replies", state="queued",
                               request_id="uuid-1")

        summary = dialogs.reset_thread(self.store, self.thread(), actor="test")

        self.assertEqual(summary["cancel_tasks"], [planned])
        self.assertEqual(summary["in_flight_tasks"], [flying])
        states = {row["id"]: row["state"] for row in self.store.query(
            "SELECT id, state FROM tasks WHERE id IN (?,?)", (planned, flying))}
        self.assertEqual(states[planned], "cancelled")
        self.assertEqual(states[flying], "queued")

    def test_unhandled_inbound_closed(self):
        self.add_inbound(5004, "ещё вопрос", now(), handled=0)
        dialogs.reset_thread(self.store, self.thread(), actor="test")
        row = self.store.one("SELECT handled FROM inbound WHERE id = 5004")
        self.assertEqual(row["handled"], 1)

    def test_presales_context_forgotten(self):
        self.store.execute(
            "UPDATE threads SET presales_context = ? WHERE id = ?",
            (dumps({"sector_id": "auto_import_dealers"}), self.thread_id))
        self.store.commit()
        dialogs.reset_thread(self.store, self.thread(), actor="test")
        self.assertEqual(autoreply.discovery_context(self.thread()), {})

    def test_dry_run_changes_nothing(self):
        plan = dialogs.plan_reset(self.store, self.thread())
        self.assertEqual(plan["thread"], self.thread_id)
        self.assertIsNone(self.thread()["reset_at"])
        self.assertEqual(len(self.texts()), 4)

    # -- команда из диалога --------------------------------------------------

    def test_command_parsing_is_whole_message_only(self):
        self.assertIsNotNone(dialogs.parse_reset_command("##reset49"))
        self.assertIsNotNone(dialogs.parse_reset_command("  ##RESET49  "))
        self.assertTrue(dialogs.parse_reset_command("##reset49 full").full)
        self.assertIsNone(dialogs.parse_reset_command(
            "он прислал ##reset49 в цитате"))
        self.assertIsNone(dialogs.parse_reset_command("сброс"))

    def test_control_reset_runs_for_listed_peer(self):
        self.add_inbound(5005, "##reset49", now(), handled=0)
        inbound = dict(self.store.one("SELECT * FROM inbound WHERE id = 5005"))
        done = autoreply.control_reset(
            self.store, inbound, self.thread(), self.settings("@tester"),
            actor="test")
        self.assertTrue(done)
        self.assertIsNotNone(self.thread()["reset_at"])

    def test_control_reset_refused_for_stranger(self):
        """Живой лид, набравший токен, ничего не стирает."""
        self.add_inbound(5006, "##reset49", now(), handled=0)
        inbound = dict(self.store.one("SELECT * FROM inbound WHERE id = 5006"))
        done = autoreply.control_reset(
            self.store, inbound, self.thread(), self.settings("@someone_else"),
            actor="test")
        self.assertFalse(done)
        self.assertIsNone(self.thread()["reset_at"])

    def test_control_reset_refused_without_list(self):
        self.add_inbound(5007, "##reset49", now(), handled=0)
        inbound = dict(self.store.one("SELECT * FROM inbound WHERE id = 5007"))
        self.assertFalse(autoreply.control_reset(
            self.store, inbound, self.thread(), self.settings(), actor="test"))

    def test_peer_matches_by_numeric_id(self):
        self.add_inbound(5008, "##reset49", now(), handled=0)
        inbound = dict(self.store.one("SELECT * FROM inbound WHERE id = 5008"))
        self.assertTrue(dialogs.peer_allowed(inbound, self.settings("id:7001")))

    def test_command_does_not_wait_for_quiet_turn(self):
        """Ход с командой закрыт сразу, без сорока пяти секунд тишины."""
        row = {"text": "##reset49", "sent_at": now(), "contact_id": self.contact_id}
        self.assertTrue(autoreply._turn_is_closed(self.store, row, row, now()))

    # -- посев ---------------------------------------------------------------

    def test_seed_makes_us_the_starter_and_recovers_sector(self):
        self.store.execute(
            "DELETE FROM tasks WHERE contact_id = ?", (self.contact_id,))
        self.store.execute(
            "UPDATE threads SET last_outbound_at = NULL WHERE id = ?",
            (self.thread_id,))
        self.store.commit()
        self.assertFalse(autoreply.we_started_it(self.store, self.thread()))

        seeded = dialogs.seed_first_touch(
            self.store, self.thread(), days_ago=3, actor="test")

        self.assertTrue(autoreply.we_started_it(self.store, self.thread()))
        self.assertEqual(seeded["text"], outreach_texts.channel_dm("tester"))
        # Сфера из личного письма не выводится — как и в бою.
        self.assertEqual(
            autoreply.outreach_sector_of_thread(self.store, self.thread()), "")

    def test_seed_does_not_spend_todays_budget(self):
        seeded = dialogs.seed_first_touch(
            self.store, self.thread(), days_ago=3, actor="test")
        self.assertLess(seeded["dispatched_at"][:10], now()[:10])

    def test_second_seed_updates_the_same_row(self):
        first = dialogs.seed_first_touch(self.store, self.thread(), actor="test")
        second = dialogs.seed_first_touch(self.store, self.thread(), actor="test")
        self.assertEqual(first["task"], second["task"])
        rows = self.store.query(
            "SELECT id FROM tasks WHERE campaign_id = ?",
            (dialogs.SEED_CAMPAIGN_ID,))
        self.assertEqual(len(rows), 1)

    def test_seed_in_channel_thread_carries_sector(self):
        self.store.execute(
            "UPDATE threads SET surface = 'channel_dm' WHERE id = ?",
            (self.thread_id,))
        self.store.execute(
            "DELETE FROM tasks WHERE contact_id = ?", (self.contact_id,))
        self.store.commit()
        seeded = dialogs.seed_first_touch(self.store, self.thread(), actor="test")
        self.assertEqual(seeded["sector"], outreach_texts.SECTOR_ID)
        self.assertEqual(
            autoreply.outreach_sector_of_thread(self.store, self.thread()),
            outreach_texts.SECTOR_ID)


if __name__ == "__main__":
    unittest.main()
