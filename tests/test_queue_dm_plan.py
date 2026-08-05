"""Постановка личных сообщений в очередь.

Проверяется не «задачи создались», а четыре границы, каждая из которых стоит
живого письма незнакомому человеку: контакт заводится один раз и потом
переиспользуется; тому, кому уже писали, второе первое касание не уходит;
повторный прогон после обрыва не дублирует; текст, не прошедший проверку
контракта, не ставится вовсе.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49.store import Store, now  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "queue_dm_plan", ROOT / "scripts" / "queue_dm_plan.py")
queue_dm_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue_dm_plan)

GOOD_TEXT = (
    "Здравствуйте! Увидели ваше сообщение в чате про поиск подрядчика по "
    "рекламе. У нас есть сервис, который находит в мессенджерах и социальных "
    "сетях сообщения людей, которым нужны такие работы, и собирает их в одном "
    "месте. Если хотите, можем бесплатно показать, как он работает. Интересно?"
)


class QueueDmPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        for account_id in (804, 812):
            self.store.execute(
                "INSERT INTO accounts(id, label, role, roles, enabled, "
                "synced_at) VALUES(?,?,'dm_sender','[\"dm_sender\"]',1,?)",
                (account_id, f"acc{account_id}", now()))
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def plan(self, *rows) -> dict:
        return {"дата": "2026-08-05", "отправки": list(rows)}

    def row(self, **overrides) -> dict:
        base = {
            "вид": "лс",
            "действие": "send_private_dm",
            "кому": "someseller",
            "аккаунт": 804,
            "слот": "11:20",
            "слот_utc": "2026-08-05T08:20:00+00:00",
            "текст": GOOD_TEXT,
            "повод": "Кто настроит рекламу на ВБ?",
        }
        base.update(overrides)
        return base

    def queue(self, plan, *, apply=True):
        return queue_dm_plan.load(plan, self.store, apply=apply, per_account=5)

    def test_new_person_gets_a_contact_and_a_task(self):
        result = self.queue(self.plan(self.row()))
        self.assertEqual(len(result["поставлено"]), 1)
        contact = self.store.one(
            "SELECT id, segment, kind FROM contacts WHERE username = ?",
            ("someseller",))
        self.assertIsNotNone(contact)
        self.assertEqual(contact["kind"], "user")
        task = self.store.one(
            "SELECT account_id, action, params, state FROM tasks "
            " WHERE contact_id = ?", (contact["id"],))
        self.assertEqual(task["state"], "planned")
        self.assertEqual(task["action"], "send_private_dm")
        self.assertIn("someseller", task["params"])

    def test_second_run_does_not_duplicate(self):
        plan = self.plan(self.row())
        self.queue(plan)
        again = self.queue(plan)
        self.assertEqual(len(again["поставлено"]), 0)
        self.assertEqual(len(again["пропущено"]), 1)
        count = self.store.one("SELECT count(*) AS n FROM tasks")["n"]
        self.assertEqual(count, 1)

    def test_person_we_already_wrote_to_is_skipped(self):
        """Второе «первое касание» — это и есть рассылка."""
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, created_at, "
            "updated_at) VALUES('c_old','user','someseller','b140',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO contact_touches(contact_id, first_sent_at, "
            "last_sent_at, sent_count, last_account_id) "
            "VALUES('c_old',?,?,1,812)", (now(), now()))
        self.store.commit()
        result = self.queue(self.plan(self.row()))
        self.assertEqual(len(result["поставлено"]), 0)
        self.assertEqual(len(result["пропущено"]), 1)
        self.assertIn("уже писали", result["пропущено"][0]["почему"])

    def test_opted_out_is_refused(self):
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, opted_out, "
            "created_at, updated_at) "
            "VALUES('c_out','user','someseller','b140',1,?,?)",
            (now(), now()))
        self.store.commit()
        result = self.queue(self.plan(self.row()))
        self.assertEqual(len(result["отказано"]), 1)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM tasks")["n"], 0)

    def test_existing_contact_is_reused(self):
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, created_at, "
            "updated_at) VALUES('c_known','user','someseller','recon',?,?)",
            (now(), now()))
        self.store.commit()
        self.queue(self.plan(self.row()))
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM contacts")["n"], 1)
        task = self.store.one("SELECT contact_id FROM tasks")
        self.assertEqual(task["contact_id"], "c_known")

    def test_bad_text_is_refused_not_queued(self):
        for label, text in (
            ("бренд", GOOD_TEXT.replace("У нас есть сервис", "У нас есть ТГ РАДАР")),
            ("ссылка", GOOD_TEXT.replace("Интересно?", "Пишите t.me/x?")),
            ("от первого лица", GOOD_TEXT.replace("Увидели", "Увидел")),
            ("пусто", ""),
        ):
            with self.subTest(label):
                store = Store(Path(self.tmp.name) / f"{label}.sqlite")
                store.execute(
                    "INSERT INTO accounts(id, label, role, roles, enabled, "
                    "synced_at) VALUES(804,'a','dm_sender','[]',1,?)", (now(),))
                store.commit()
                result = queue_dm_plan.load(
                    self.plan(self.row(текст=text)), store,
                    apply=True, per_account=5)
                self.assertEqual(len(result["отказано"]), 1)
                self.assertEqual(
                    store.one("SELECT count(*) AS n FROM tasks")["n"], 0)
                store.close()

    def test_preview_writes_nothing(self):
        result = self.queue(self.plan(self.row()), apply=False)
        self.assertEqual(len(result["поставлено"]), 1)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM tasks")["n"], 0)
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM contacts")["n"], 0)

    def test_only_dm_rows_are_taken(self):
        other = {"вид": "chat", "действие": "send_public_chat_message",
                 "кому": "somechat", "аккаунт": 804, "слот": "12:00",
                 "слот_utc": "2026-08-05T09:00:00+00:00", "текст": "Кто возит?"}
        result = self.queue(self.plan(self.row(), other))
        self.assertEqual(len(result["поставлено"]), 1)
        self.assertEqual(result["поставлено"][0]["кому"], "someseller")


class CampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_campaign_cap_follows_what_we_actually_queue(self):
        """Потолок кампании и потолок прогона обязаны совпадать.

        Диспетчер считает по `per_account_daily_visible` и на кампанию не
        смотрит, поэтому расхождение живёт незаметно — до первого запуска
        `planner.py`, который срежет очередь по устаревшему числу.
        """
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, per_account_daily_cap, "
            "created_at, updated_at) VALUES(?,'старая','send_private_dm',2,?,?)",
            (queue_dm_plan.CAMPAIGN_ID, now(), now()))
        self.store.commit()
        queue_dm_plan.ensure_campaign(self.store, 5)
        row = self.store.one(
            "SELECT per_account_daily_cap FROM campaigns WHERE id = ?",
            (queue_dm_plan.CAMPAIGN_ID,))
        self.assertEqual(row["per_account_daily_cap"], 5)

    def test_campaign_is_created_when_missing(self):
        queue_dm_plan.ensure_campaign(self.store, 5)
        row = self.store.one("SELECT action, status, roles FROM campaigns "
                             " WHERE id = ?", (queue_dm_plan.CAMPAIGN_ID,))
        self.assertEqual(row["action"], "send_private_dm")
        self.assertEqual(row["status"], "active")
        self.assertIn("dm_sender", row["roles"])


if __name__ == "__main__":
    unittest.main()
