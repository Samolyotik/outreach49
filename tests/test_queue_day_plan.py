"""Постановка дневного плана в очередь.

Главное здесь — не «задачи создались», а две границы. Долг обязан
обновляться, а не создаваться заново: продублировать его значит отправить
человеку два одинаковых сообщения. И повторный запуск после обрыва не должен
считать заново то, что уже поставлено.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49.store import Store, now  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "queue_day_plan", ROOT / "scripts" / "queue_day_plan.py")
queue_day_plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(queue_day_plan)

CHAT_TEXT = ("Кто возит авто из-за границы под ключ?\n\n"
             "Нужна проверка перед покупкой. Кого посоветуете?")
DM_TEXT = ("Здравствуйте. Мы собираем в Telegram релевантные запросы по "
           "привозу авто. Можем бесплатно показать, как это выглядит.")


class QueueDayPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        for account_id in (803, 862):
            self.store.execute(
                "INSERT INTO accounts(id, label, role, roles, enabled, synced_at)"
                " VALUES(?,?,'chat_sender','[\"chat_sender\"]',1,?)",
                (account_id, f"acc{account_id}", now()))
        for contact_id in ("c1", "c2"):
            self.store.execute(
                "INSERT INTO contacts(id, kind, username, segment, created_at,"
                " updated_at) VALUES(?,'channel',?,'recon',?,?)",
                (contact_id, contact_id, now(), now()))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('pending_replies','долг','send_private_dm',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, created_at, updated_at) "
            "VALUES('task_debt','pending_replies','c2',862,'send_private_dm',"
            "'{}','immediate','2026-08-06T07:00:00+00:00','planned',?,?)",
            (now(), now()))
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def plan(self, **overrides) -> dict:
        base = {
            "дата": "2026-08-05",
            "отправки": [
                {"вид": "chat", "действие": "send_public_chat_message",
                 "кому": "c1", "contact_id": "c1", "аккаунт": 803,
                 "слот": "11:20", "слот_utc": "2026-08-05T08:20:00+00:00",
                 "текст": CHAT_TEXT},
                {"вид": "долг", "задача": "task_debt", "кому": "кто-то",
                 "аккаунт": 862, "слот": "19:37",
                 "слот_utc": "2026-08-05T16:37:00+00:00"},
            ],
        }
        base.update(overrides)
        return base

    def queued_count(self) -> int:
        return self.store.one(
            "SELECT COUNT(*) AS n FROM tasks WHERE campaign_id = ?",
            (queue_day_plan.CAMPAIGN_ID,))["n"]

    # -- новые касания -----------------------------------------------------

    def test_preview_writes_nothing(self):
        result = queue_day_plan.load(self.plan(), self.store, apply=False)
        self.assertEqual(len(result["поставлено"]), 1)
        self.assertEqual(self.queued_count(), 0, "предпросмотр записал в базу")

    def test_apply_creates_the_task_with_text_and_slot(self):
        queue_day_plan.load(self.plan(), self.store, apply=True)
        row = self.store.one(
            "SELECT action, params, scheduled_at, account_id, state FROM tasks "
            " WHERE campaign_id = ?", (queue_day_plan.CAMPAIGN_ID,))
        self.assertEqual(row["action"], "send_public_chat_message")
        self.assertEqual(row["state"], "planned")
        self.assertEqual(row["scheduled_at"], "2026-08-05T08:20:00+00:00")
        params = json.loads(row["params"])
        self.assertEqual(params["username"], "c1")
        self.assertEqual(params["text"], CHAT_TEXT)

    def test_second_run_adds_nothing(self):
        queue_day_plan.load(self.plan(), self.store, apply=True)
        again = queue_day_plan.load(self.plan(), self.store, apply=True)
        self.assertEqual(self.queued_count(), 1)
        self.assertEqual(len(again["поставлено"]), 0)
        self.assertEqual(len(again["пропущено"]), 1)

    # -- долг ---------------------------------------------------------------

    def test_debt_is_moved_not_duplicated(self):
        """Дубль долга — это два одинаковых письма одному человеку."""
        queue_day_plan.load(self.plan(), self.store, apply=True)
        rows = self.store.query(
            "SELECT id, scheduled_at FROM tasks WHERE campaign_id='pending_replies'")
        self.assertEqual(len(rows), 1, "долг продублирован")
        self.assertEqual(rows[0]["scheduled_at"], "2026-08-05T16:37:00+00:00")

    def test_a_dispatched_debt_task_is_left_alone(self):
        """Задачу уже взял диспетчер — команда могла уйти в Radar."""
        self.store.execute(
            "UPDATE tasks SET state='queued', request_id='uuid' "
            " WHERE id='task_debt'")
        self.store.commit()
        result = queue_day_plan.load(self.plan(), self.store, apply=True)
        self.assertEqual(len(result["перенесено"]), 0)
        self.assertEqual(len(result["пропущено"]), 1)
        row = self.store.one("SELECT scheduled_at FROM tasks WHERE id='task_debt'")
        self.assertEqual(row["scheduled_at"], "2026-08-06T07:00:00+00:00")

    def test_a_vanished_debt_task_is_refused_loudly(self):
        self.store.execute("DELETE FROM tasks WHERE id='task_debt'")
        self.store.commit()
        result = queue_day_plan.load(self.plan(), self.store, apply=True)
        self.assertEqual(len(result["отказано"]), 1)
        self.assertIn("больше нет", result["отказано"][0]["почему"])

    # -- текст --------------------------------------------------------------

    def test_a_bad_text_is_refused_and_not_queued(self):
        """План мог быть собран другой версией сборщика или поправлен руками."""
        plan = self.plan()
        plan["отправки"][0]["текст"] = "Попробуйте ТГ РАДАР: https://t.me/x"
        result = queue_day_plan.load(plan, self.store, apply=True)
        self.assertEqual(len(result["поставлено"]), 0)
        self.assertEqual(len(result["отказано"]), 1)
        self.assertEqual(self.queued_count(), 0)

    def test_a_row_without_a_contact_is_refused(self):
        plan = self.plan()
        plan["отправки"][0]["contact_id"] = ""
        result = queue_day_plan.load(plan, self.store, apply=True)
        self.assertEqual(len(result["отказано"]), 1)
        self.assertEqual(self.queued_count(), 0)


if __name__ == "__main__":
    unittest.main()
