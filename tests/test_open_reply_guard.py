"""Нижний рубеж: у собеседника не может быть двух незакрытых ответов.

Прикладная проверка в `queue_reply` неатомарна — между «посмотрел, нет ли уже
поставленного» и «вставил» помещается чужая вставка. Сегодня туда некому
влезть: в кампанию ответов пишет один oneshot-юнит. Завтра разбор входящих
станет постоянным процессом с параллельностью, и окно откроется.

Поэтому проверяется не поведение одного вызова, а свойство базы: второй
незакрытый ответ не вставляется в принципе, кто бы его ни вставлял.
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49 import replies  # noqa: E402
from bridge49.store import Store, now  # noqa: E402


class OpenReplyIndexTests(unittest.TestCase):
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

    def add(self, task_id: str, *, action: str = "reply_private_dm",
            state: str = "planned", campaign: str = "autoreplies",
            contact: str = "c1") -> None:
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, created_at, updated_at) "
            "VALUES(?,?,?,804,?,'{}','immediate',?,?,?,?)",
            (task_id, campaign, contact, action, now(), state, now(), now()))
        self.store.commit()

    def test_second_open_reply_is_refused(self):
        self.add("t1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add("t2")

    def test_refusal_covers_the_channel_surface_too(self):
        self.add("t1", action="reply_channel_dm")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add("t2", action="reply_channel_dm")

    def test_queued_reply_also_holds_the_slot(self):
        """Ушедший в мост, но не подтверждённый — тоже незакрытый."""
        self.add("t1", state="queued")
        with self.assertRaises(sqlite3.IntegrityError):
            self.add("t2")

    def test_sent_reply_frees_the_slot(self):
        """Иначе второй ответ в разговоре стал бы невозможен вовсе."""
        self.add("t1", state="done")
        self.add("t2")

    def test_cancelled_reply_frees_the_slot(self):
        """Так работает замена ждущего ответа: снять и поставить новый."""
        self.add("t1")
        self.store.execute("UPDATE tasks SET state='cancelled' WHERE id='t1'")
        self.store.commit()
        self.add("t2")

    def test_first_touch_is_not_affected(self):
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('cold','холодная','send_private_dm',?,?)", (now(), now()))
        self.store.commit()
        self.add("t1", action="send_private_dm", campaign="cold")

    def test_another_contact_is_not_affected(self):
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES('c2','user','other',?,?)", (now(), now()))
        self.store.commit()
        self.add("t1")
        self.add("t2", contact="c2")

    def test_reply_actions_match_the_canonical_set(self):
        from bridge49 import store as store_mod

        for action in replies.REPLY_ACTIONS:
            self.assertIn(f"'{action}'", store_mod._REPLY_ACTIONS_SQL)
        self.assertEqual(store_mod._REPLY_ACTIONS_SQL.count("'") // 2,
                         len(replies.REPLY_ACTIONS))


class RefusedIndexTests(unittest.TestCase):
    """База с уже существующим дублем не должна валить весь контур."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "b.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_existing_duplicate_does_not_break_opening(self):
        """Рубеж не завёлся — это плохо, но не повод останавливать всё.

        Какой из двух ответов лишний, знает человек, а не миграция. Молча
        чинить данные нельзя, а падать при открытии базы — тем более: без
        индекса контур ровно там же, где был вчера.
        """
        store = Store(self.path)
        store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(804,'a','channel_sender',1,?)", (now(),))
        store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES('c1','user','x',?,?)", (now(), now()))
        store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('autoreplies','а','reply_private_dm',?,?)", (now(), now()))
        store.commit()
        # Сносим рубеж и заводим дубль, который он бы не пропустил.
        store.conn.execute("DROP INDEX idx_tasks_open_reply")
        for task_id in ("t1", "t2"):
            store.execute(
                "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
                "action, params, mode, scheduled_at, state, created_at, "
                "updated_at) VALUES(?,'autoreplies','c1',804,"
                "'reply_private_dm','{}','immediate',?,'planned',?,?)",
                (task_id, now(), now(), now()))
        store.commit()
        store.close()

        reopened = Store(self.path)          # не должно упасть
        try:
            row = reopened.one(
                "SELECT count(*) n FROM events WHERE kind = ?",
                ("index.open_reply_refused",))
            self.assertEqual(row["n"], 1, "отказ должен быть записан")
            self.assertFalse(reopened.conn.in_transaction)
        finally:
            reopened.close()



class DemoRouteGateTests(unittest.TestCase):
    """Мастер-гейт обязан выключать и демо-маршрут, а не только StartBot."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "t_di", ROOT / "tests" / "test_direct_invite.py")
        self.helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helpers)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def branch(self, *, enabled: bool):
        from bridge49 import direct_invite
        path = self.helpers.write_config(self.dir, enabled=enabled)
        return direct_invite.BranchConfig.from_path(path).with_sector_catalog(
            self.helpers.write_catalog(self.dir))

    def test_disabled_branch_does_not_send_demo(self):
        """Флаг выключал только StartBot, а демо продолжало уходить.

        Мастер-гейт — единственная ручка «автоматика ничего не делает сама».
        Хуже того, непрочитавшийся конфиг тоже даёт выключенную ветку, но
        словарь сфер к ней всё равно подвешивался: неудачная правка общего
        файла не останавливала маршрут, а молча оставляла его работать.
        """
        self.assertFalse(self.branch(enabled=False).demo_route_ready())

    def test_enabled_branch_sends_demo(self):
        self.assertTrue(self.branch(enabled=True).demo_route_ready())

    def test_a_config_that_failed_to_load_sends_nothing(self):
        from bridge49 import direct_invite
        broken = self.dir / "broken.json"
        broken.write_text("{ не json", encoding="utf-8")
        branch = direct_invite.BranchConfig.disabled(str(broken))
        self.assertFalse(
            branch.with_sector_catalog(
                self.helpers.write_catalog(self.dir)).demo_route_ready())


if __name__ == "__main__":
    unittest.main()
