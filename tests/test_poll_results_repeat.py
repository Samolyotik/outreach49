"""Опрос результатов не должен разбирать одну неудачу заново каждые 15 секунд.

06.08 в боевой базе нашлись восемь задач, каждая с 1952 одинаковыми записями
`error.classified` за ночь: 15 620 строк из 15 920 за сутки дали эти восемь.

Задача с исходом `outcome_unknown` остаётся в опросе бессрочно — и это верно:
Radar умеет позже заменить такой исход настоящим. Неверным был разбор ошибки:
он срабатывал на каждом круге и писал один и тот же вердикт заново. Поэтому
чинится не выборка, а идемпотентность разбора.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49 import pollers  # noqa: E402
from bridge49.config import Limits, Settings  # noqa: E402
from bridge49.store import Store, now  # noqa: E402


class FakeBridge:
    """Мост, который на каждый опрос отвечает одним и тем же — как настоящий."""

    def __init__(self, records):
        self.records = records
        self.calls = 0

    async def results(self, command_ids):
        self.calls += 1
        return [r for r in self.records if int(r["id"]) in set(command_ids)]


def failed_record(command_id: int, code: str, message: str) -> dict:
    return {
        "id": command_id,
        "status": "failed",
        "updated_at": now(),
        "last_error": message,
        "details": {"result": {
            "outcome": "outcome_unknown",
            "error": {"code": code, "message": message},
        }},
    }


class RepeatedClassificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "var").mkdir()
        self.store = Store(self.home / "var" / "b.sqlite")
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(832,'a','chat_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES('c1','user','x',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('cold','х','send_public_chat_message',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, command_id, dispatched_at, "
            "created_at, updated_at) VALUES('t1','cold','c1',832,"
            "'send_public_chat_message','{}','immediate',?,'queued',555,?,?,?)",
            (now(), now(), now(), now()))
        self.store.commit()
        self.settings = Settings(
            home=self.home, db_path=self.home / "var" / "b.sqlite",
            dsn=None, limits=Limits(), timezone="Europe/Moscow",
        )
        self.bridge = FakeBridge([failed_record(555, "ForbiddenError", "403")])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def poll(self) -> dict:
        return asyncio.run(pollers.poll_results(
            self.store, self.settings, bridge=self.bridge))

    def classified(self) -> int:
        row = self.store.one(
            "SELECT count(*) n FROM events WHERE kind = 'error.classified'")
        return int(row["n"])

    def test_first_poll_classifies_the_failure(self):
        self.poll()
        self.assertEqual(self.classified(), 1)

    def test_second_poll_does_not_classify_it_again(self):
        """Ровно тот случай, что дал 1952 записи на задачу."""
        self.poll()
        for _ in range(5):
            self.poll()
        self.assertEqual(self.classified(), 1)

    def test_the_task_stays_in_the_poll_set_on_purpose(self):
        """Это не чинится удалением задачи из опроса.

        `outcome_unknown` переспрашивается бессрочно намеренно: Radar умеет
        позже заменить его настоящим исходом (см. `test_bridge49`,
        `test_outcome_unknown_is_rechecked_for_radar_recovery`). Значит
        идемпотентным обязан быть сам разбор, а не выборка.
        """
        self.poll()
        self.assertEqual(self.poll()["checked"], 1, "опрос продолжается")

    def test_a_changed_verdict_is_classified_anew(self):
        """Если Radar передумал, новый разбор нужен — глушим повтор, не смену."""
        self.poll()
        self.bridge.records = [failed_record(555, "PeerFloodError", "flood")]
        self.poll()
        self.assertEqual(self.classified(), 2)


if __name__ == "__main__":
    unittest.main()
