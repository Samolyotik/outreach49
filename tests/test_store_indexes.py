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

    def close(self, task_id: str) -> None:
        self.store.execute("UPDATE tasks SET state='done' WHERE id=?", (task_id,))
        self.store.commit()

    def test_second_reply_in_a_private_dm_is_allowed(self):
        """Ответить можно снова — после того, как предыдущий ушёл."""
        self.add_task("t1", "reply_private_dm")
        self.close("t1")
        self.add_task("t2", "reply_private_dm")

    def test_second_reply_in_a_channel_dm_is_allowed(self):
        """Тот же разговор, другая поверхность — и то же право ответить.

        Пока `reply_channel_dm` оставался под правилом про рассылку, машина
        отвечала человеку в канале ровно один раз за всю жизнь диалога, а
        дальше заводила карточку. Снаружи это выглядело не как отказ, а как
        молчание.

        Правил тут два, и они не спорят. Правило про рассылку ответов не
        касается вовсе; отдельный рубеж не даёт держать ДВА незакрытых ответа
        одновременно. Закрытый предыдущий места не занимает — иначе разговор
        обрывался бы на первой же реплике.
        """
        self.add_task("t1", "reply_channel_dm")
        self.close("t1")
        self.add_task("t2", "reply_channel_dm")

    def test_two_open_replies_are_still_refused(self):
        """Граница между двумя правилами: незакрытый ответ должен быть один."""
        self.add_task("t1", "reply_channel_dm")
        with self.assertRaises(sqlite3.IntegrityError):
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

    def test_index_of_the_wrong_shape_is_replaced(self):
        """Условие верное, а сам индекс — нет.

        Сравнивать одно лишь условие значит повторить починенную ошибку в
        другом месте: неуникальный индекс или индекс по другим колонкам
        прошёл бы за актуальный и остался бы навсегда.
        """
        Store(self.path).close()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP INDEX idx_tasks_campaign_contact")
            conn.execute(
                "CREATE INDEX idx_tasks_campaign_contact ON tasks(contact_id) "
                " WHERE action NOT IN ('reply_private_dm', 'reply_channel_dm')")
            conn.commit()
        finally:
            conn.close()
        Store(self.path).close()
        sql = self.index_sql()
        self.assertIn("UNIQUE", sql)
        self.assertIn("campaign_id, contact_id", sql)

    def test_missing_index_is_created_without_a_drop(self):
        """Кто-то уже снёс индекс — пересоздание не должно падать на DROP."""
        Store(self.path).close()
        conn = sqlite3.connect(self.path)
        try:
            conn.execute("DROP INDEX idx_tasks_campaign_contact")
            conn.commit()
        finally:
            conn.close()
        Store(self.path).close()
        self.assertIn("reply_channel_dm", self.index_sql())

    def test_stale_reader_does_not_migrate_twice(self):
        """Тот, кто решил мигрировать по устаревшему чтению, не должен падать.

        Естественную гонку поймать нельзя — окно между DROP и CREATE
        микроскопическое, и в прогоне на двенадцать одновременных открытий она
        не проявилась ни на прежней версии, ни на этой. Но она не про удачу, а
        про устройство: раньше решение принималось по чтению ВНЕ замка, и в
        худшем исходе база оставалась без индекса и с дублем — после чего
        падало каждое следующее открытие.

        Поэтому проверяется само свойство: решение перечитывается уже под
        эксклюзивной записью. Здесь первое чтение подделано под устаревшее —
        так выглядит процесс, который посмотрел на базу до чужой миграции.
        """
        Store(self.path).close()
        wanted = self.index_sql()

        store = Store.__new__(Store)
        store.path = self.path
        store.conn = sqlite3.connect(self.path, timeout=30)
        store.conn.row_factory = sqlite3.Row
        real = store._tasks_index_is_current
        calls = {"n": 0}

        def stale_first_time():
            calls["n"] += 1
            return False if calls["n"] == 1 else real()

        store._tasks_index_is_current = stale_first_time
        try:
            store._ensure_indexes()   # не должно ни упасть, ни пересоздать
            self.assertFalse(store.conn.in_transaction)
        finally:
            store.close()

        self.assertGreaterEqual(calls["n"], 2, "перечитывания под замком не было")
        self.assertEqual(self.index_sql(), wanted)

    def test_migration_leaves_no_transaction_open(self):
        """Незакрытая транзакция заперла бы писателей на busy_timeout."""
        Store(self.path).close()
        self.replace_index("action <> 'reply_private_dm'")
        store = Store(self.path)
        try:
            self.assertFalse(store.conn.in_transaction)
        finally:
            store.close()


class ReplyActionsAgreementTests(unittest.TestCase):
    """Канон «что считается ответом» записан в четырёх местах.

    Индекс исключает ответы из правила про рассылку, диспетчер снимает с них
    защиту от повторного касания, планировщик не считает их исходящими. Все
    четыре списка обязаны совпадать: разъедутся — и добавленный третий вид
    ответа молча попадёт под правило, из которого его выводили.
    """

    def test_index_condition_matches_the_canonical_set(self):
        from bridge49 import replies, store as store_mod

        for action in replies.REPLY_ACTIONS:
            self.assertIn(f"'{action}'", store_mod._TASKS_UNIQUE_WHERE,
                          f"{action} считается ответом, но индекс его не знает")
        listed = store_mod._TASKS_UNIQUE_WHERE.count("'") // 2
        self.assertEqual(listed, len(replies.REPLY_ACTIONS),
                         "в индексе перечислено не столько действий, "
                         "сколько считается ответами")

    def test_planner_copy_matches_too(self):
        import importlib.util

        from bridge49 import replies

        spec = importlib.util.spec_from_file_location(
            "plan_tomorrow", ROOT / "scripts" / "plan_tomorrow.py")
        planner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(planner)
        self.assertEqual(set(planner.REPLY_ACTIONS), set(replies.REPLY_ACTIONS))


if __name__ == "__main__":
    unittest.main()
