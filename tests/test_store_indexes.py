"""Правило «одно касание на контакт» и его исключения.

Проверяется не индекс сам по себе, а его последствие для человека: первое
касание должно быть одно, а ответов в разговоре — сколько угодно. Оба вида
ответа, а не один: именно на этом контур и обжёгся дважды.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49.store import Store, now  # noqa: E402


class TaskUniquenessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "b.sqlite"
        self.store = Store(self.path)
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(804,'acc804','channel_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES('c1','user','someone',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('autoreplies','автоответы','reply_private_dm',?,?)",
            (now(), now()))
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_task(self, task_id: str, action: str) -> None:
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, created_at, updated_at) "
            "VALUES(?, 'autoreplies', 'c1', 804, ?, '{}', 'immediate', ?, "
            "'planned', ?, ?)",
            (task_id, action, now(), now(), now()))
        self.store.commit()

    def test_second_reply_in_a_private_dm_is_allowed(self):
        self.add_task("t1", "reply_private_dm")
        self.add_task("t2", "reply_private_dm")

    def test_second_reply_in_a_channel_dm_is_allowed(self):
        """Тот же разговор, другая поверхность — и то же право ответить.

        Пока `reply_channel_dm` оставался под индексом, машина отвечала
        человеку в канале ровно один раз за всю жизнь диалога, а дальше
        заводила карточку. Снаружи это выглядело не как отказ, а как молчание.
        """
        self.add_task("t1", "reply_channel_dm")
        self.add_task("t2", "reply_channel_dm")

    def test_second_first_touch_is_still_refused(self):
        """Исключение для ответов не должно распускать правило про рассылку."""
        self.add_task("t1", "send_private_dm")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_task("t2", "send_private_dm")

    def test_cancelled_first_touch_still_holds_the_slot(self):
        """Известное свойство, зафиксировано намеренно.

        Снятая задача остаётся в таблице и продолжает занимать ключ. Это не
        побочный эффект: «мы уже подходили к этому человеку в этой кампании» —
        факт, который снятие задачи не отменяет.
        """
        self.add_task("t1", "send_private_dm")
        self.store.execute("UPDATE tasks SET state='cancelled' WHERE id='t1'")
        self.store.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_task("t2", "send_private_dm")


class IndexMigrationTests(unittest.TestCase):
    """Правило должно доезжать до баз, которые завели раньше правки."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "b.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def index_sql(self) -> str:
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                " AND name='idx_tasks_campaign_contact'").fetchone()
        finally:
            conn.close()
        return " ".join(str(row[0]).split()) if row and row[0] else ""

    def replace_index(self, where: str | None) -> None:
        """Подсунуть базе индекс прежнего поколения."""
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP INDEX IF EXISTS idx_tasks_campaign_contact")
            clause = f" WHERE {where}" if where else ""
            conn.execute(
                "CREATE UNIQUE INDEX idx_tasks_campaign_contact "
                f" ON tasks(campaign_id, contact_id){clause}")
            conn.commit()
        finally:
            conn.close()

    def test_fresh_database_gets_both_exclusions(self):
        Store(self.path).close()
        self.assertIn("reply_channel_dm", self.index_sql())

    def test_solid_index_is_migrated(self):
        """Самое старое поколение: уникальность без условия вовсе."""
        Store(self.path).close()
        self.replace_index(None)
        Store(self.path).close()
        self.assertIn("reply_channel_dm", self.index_sql())

    def test_previous_partial_index_is_migrated_too(self):
        """Главная развилка: условие есть, но устаревшее.

        Прежняя проверка спрашивала «частичный ли индекс» и на любое условие
        отвечала «да». Поэтому вторая правка правила молча не доезжала до
        живых баз — а именно они и работают в бою.
        """
        Store(self.path).close()
        self.replace_index("action <> 'reply_private_dm'")
        Store(self.path).close()
        self.assertIn("reply_channel_dm", self.index_sql())

    def test_migration_does_not_rewrite_an_already_current_index(self):
        Store(self.path).close()
        before = self.index_sql()
        Store(self.path).close()
        self.assertEqual(self.index_sql(), before)


if __name__ == "__main__":
    unittest.main()
