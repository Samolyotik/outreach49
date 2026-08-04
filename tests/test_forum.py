"""Зеркало переписки в рабочую группу.

Проверяется не транспорт (это Bot API), а то, что карточка несёт всё нужное и
что ветка у собеседника заводится один раз. Общая лента на всех читается как
поток, а не как переписка: чтобы понять, что ответил конкретный человек,
пришлось бы вылавливать его сообщения среди чужих.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import forum  # noqa: E402
from bridge49.store import Store, now  # noqa: E402


class CardTests(unittest.TestCase):
    def test_outgoing_card_names_surface_and_peer(self):
        card = forum.outgoing_card({
            "action": "send_public_chat_message", "params":
                json.dumps({"text": "Кто занимается привозом авто?"}),
            "campaign_id": "probe", "account_id": 803, "state": "done",
            "outcome": "succeeded", "username": "autochat", "tg_id": None,
            "contact_id": "c1",
        })
        self.assertIn("МЫ НАПИСАЛИ", card)
        self.assertIn("публичный чат", card)
        self.assertIn("@autochat", card)
        self.assertIn("Кто занимается привозом авто?", card)

    def test_failed_send_is_marked(self):
        """Неудача должна бросаться в глаза, а не прятаться в общем потоке."""
        card = forum.outgoing_card({
            "action": "send_private_dm", "params": "{}", "campaign_id": "c",
            "account_id": 804, "state": "failed", "outcome": "rejected",
            "username": "x", "tg_id": None, "contact_id": "c1",
        })
        self.assertIn("НЕ ДОШЛО", card)
        self.assertIn("rejected", card)

    def test_inbound_card_shows_who_answered(self):
        card = forum.inbound_card({
            "surface": "private_dm", "peer_key": "@vadim",
            "peer_username": "vadim", "account_id": 861, "text": "Спасибо",
        })
        self.assertIn("НАМ ОТВЕТИЛИ", card)
        self.assertIn("@vadim", card)
        self.assertIn("Спасибо", card)

    def test_unknown_outcome_is_not_called_a_failure(self):
        """Оба однозначных заголовка тут врут: один зовёт писать повторно и
        рискует дублем, другой прячет пропажу."""
        card = forum.outgoing_card({
            "action": "send_private_dm", "params": "{}", "campaign_id": "c",
            "account_id": 832, "state": "failed", "outcome": "outcome_unknown",
            "username": "contextlid", "tg_id": None, "contact_id": "c1",
        })
        self.assertIn("НЕЯСНО", card)
        self.assertNotIn("НЕ ДОШЛО", card)
        self.assertNotIn("МЫ НАПИСАЛИ", card)

    def test_handoff_card_is_a_separate_type(self):
        """От человека тут ждут действия — это должно читаться сразу."""
        card = forum.handoff_card({
            "reason": "free_test_access_failed", "note": "не удалось выпустить",
            "account_id": 861, "peer_key": "@vadim", "contact_id": "c1",
            "username": "vadim",
        })
        self.assertIn("НУЖЕН ЧЕЛОВЕК", card)
        self.assertIn("free_test_access_failed", card)
        self.assertIn("@vadim", card)


class TopicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, created_at, "
            "updated_at) VALUES('c1','user','someone','default',?,?)",
            (now(), now()))
        self.store.commit()
        self.calls: list[tuple[str, dict]] = []
        self._real = forum._call
        forum._call = self.fake_call
        os.environ[forum.CHAT_ID_ENV] = "-100123"
        os.environ[forum.TOPIC_PREFIX_ENV] = "TGRadar"

    def tearDown(self):
        forum._call = self._real
        self.store.close()
        self.tmp.cleanup()

    def fake_call(self, method, payload, **kw):
        self.calls.append((method, dict(payload)))
        if method == "createForumTopic":
            return {"message_thread_id": 777}
        return {"message_id": 1}

    def add_reply(self, contact_id: str = "c1"):
        """Ветка заводится только после ответа — значит он нужен в тесте."""
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, text, raw, "
            "contact_id, created_at) VALUES(9001,821,'private_dm','@someone',"
            "'Спасибо','{}',?,?)", (contact_id, now()))
        self.store.commit()

    def test_topic_is_not_created_before_a_reply(self):
        """Иначе группа превращается в список рассылки: пишем десяткам,
        отвечают единицы, и настоящие разговоры тонут среди пустых веток."""
        self.assertIsNone(forum.ensure_topic(self.store, "c1"))
        self.assertEqual([c for c in self.calls if c[0] == "createForumTopic"], [])

    def test_topic_appears_once_the_person_answered(self):
        self.add_reply()
        self.assertEqual(forum.ensure_topic(self.store, "c1"), 777)

    def test_topic_is_created_once_and_remembered(self):
        self.add_reply()
        first = forum.ensure_topic(self.store, "c1")
        second = forum.ensure_topic(self.store, "c1")
        self.assertEqual(first, 777)
        self.assertEqual(second, 777)
        created = [c for c in self.calls if c[0] == "createForumTopic"]
        self.assertEqual(len(created), 1, "ветка заведена дважды")
        stored = self.store.one("SELECT forum_thread_id FROM contacts WHERE id='c1'")
        self.assertEqual(stored["forum_thread_id"], 777)

    def test_topic_name_identifies_the_person(self):
        self.add_reply()
        forum.ensure_topic(self.store, "c1")
        name = [c for c in self.calls if c[0] == "createForumTopic"][0][1]["name"]
        self.assertIn("@someone", name)
        self.assertIn("TGRadar", name)

    def test_no_contact_has_no_topic(self):
        self.assertIsNone(forum.ensure_topic(self.store, None))
        self.assertIsNone(forum.ensure_topic(self.store, "нет такого"))

    def test_nothing_is_written_to_general(self):
        """General — это лента, где переписка с разными людьми смешана в один
        поток. Читать её нельзя, значит и писать туда незачем."""
        with self.assertRaises(forum.NoTopic):
            forum.send("текст", thread_id=None)
        with self.assertRaises(forum.NoTopic):
            forum.send("текст", thread_id=forum.GENERAL_THREAD_ID)
        self.assertEqual([c for c in self.calls if c[0] == "sendMessage"], [])

    def test_message_goes_to_the_topic(self):
        self.add_reply()
        forum.send("текст", thread_id=forum.ensure_topic(self.store, "c1"))
        sent = [c for c in self.calls if c[0] == "sendMessage"][0][1]
        self.assertEqual(sent["message_thread_id"], 777)

    def test_topic_failure_does_not_lose_the_message(self):
        """Группа может не быть форумом. Письмо всё равно должно уехать."""
        def boom(method, payload, **kw):
            if method == "createForumTopic":
                raise forum.ForumError("the chat is not a forum")
            return {"message_id": 1}
        forum._call = boom
        self.add_reply()
        self.assertIsNone(forum.ensure_topic(self.store, "c1"))


class InFlightSendTests(unittest.TestCase):
    """Отправка попадает в зеркало один раз — и только с настоящим исходом.

    Курсор двигается вперёд после каждой карточки, поэтому карточка, выданная
    по догадке, остаётся в группе навсегда: следующий проход эту строку уже не
    увидит. Значит гадать нельзя вовсе.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, created_at, "
            "updated_at) VALUES('c1','user','webdevfound','default',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, text, raw, "
            "contact_id, created_at) VALUES(9001,812,'private_dm','@webdevfound',"
            "'Здравствуйте','{}','c1',?)", (now(),))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('autoreplies','автоответы','reply_private_dm',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO accounts(id, label, role, synced_at) "
            "VALUES(812,'tgr-812','dm_sender',?)", (now(),))
        # Ответ нужен только чтобы у собеседника появилась ветка. Сам он уже
        # отзеркалён — иначе его карточка мешалась бы с проверяемыми.
        self.store.set_state(forum.CURSOR_INBOUND, "9001")
        self.store.commit()
        self.cards: list[str] = []
        self._real = forum._call
        forum._call = self.fake_call
        os.environ[forum.CHAT_ID_ENV] = "-100123"
        os.environ[forum.BOT_TOKEN_ENV] = "t"
        os.environ[forum.ENABLED_ENV] = "1"

    def tearDown(self):
        forum._call = self._real
        self.store.close()
        self.tmp.cleanup()

    def fake_call(self, method, payload, **kw):
        if method == "createForumTopic":
            return {"message_thread_id": 777}
        if method == "sendMessage":
            self.cards.append(payload["text"])
        return {"message_id": 1}

    def add_task(self, task_id, state, outcome, dispatched, finished):
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, outcome, dispatched_at, "
            "finished_at, created_at, updated_at) VALUES(?,'autoreplies','c1',812,"
            "'reply_private_dm',?,'immediate',?,?,?,?,?,?,?)",
            (task_id, json.dumps({"text": "Здравствуйте! В какой сфере?"}),
             dispatched, state, outcome, dispatched, finished, dispatched,
             dispatched))
        self.store.commit()

    def test_task_in_flight_produces_no_card(self):
        self.add_task("t1", "queued", None, "2026-08-04T15:32:27Z", None)
        forum.run(self.store)
        self.assertEqual(self.cards, [], "карточка выдана по догадке")

    def test_card_appears_once_the_outcome_is_real(self):
        self.add_task("t1", "queued", None, "2026-08-04T15:32:27Z", None)
        forum.run(self.store)
        self.store.execute(
            "UPDATE tasks SET state='done', outcome='succeeded', "
            "finished_at='2026-08-04T15:37:08Z' WHERE id='t1'")
        self.store.commit()
        forum.run(self.store)
        self.assertEqual(len(self.cards), 1)
        self.assertIn("МЫ НАПИСАЛИ", self.cards[0])
        self.assertNotIn("НЕ ДОШЛО", self.cards[0])

    def test_each_send_is_mirrored_exactly_once(self):
        self.add_task("t1", "done", "succeeded", "2026-08-04T15:32:27Z",
                      "2026-08-04T15:37:08Z")
        forum.run(self.store)
        forum.run(self.store)
        self.assertEqual(len(self.cards), 1)

    def test_legacy_cursor_does_not_replay_what_was_mirrored(self):
        """Старый ключ хранил момент выпуска. Прочитать его как момент
        завершения — значит выдать все прежние карточки заново."""
        self.add_task("t1", "done", "succeeded", "2026-08-04T15:32:27Z",
                      "2026-08-04T15:37:08Z")
        self.store.set_state(forum.CURSOR_SENT_LEGACY, "2026-08-04T15:32:27Z")
        self.store.commit()
        forum.run(self.store)
        self.assertEqual(self.cards, [], "старая отправка уехала второй раз")

    def test_adopted_cursor_still_lets_an_in_flight_send_through(self):
        """Перенос не должен проглотить то, что было в полёте при обновлении."""
        self.add_task("t1", "done", "succeeded", "2026-08-04T15:32:27Z",
                      "2026-08-04T15:37:08Z")
        self.add_task("t2", "queued", None, "2026-08-04T15:33:00Z", None)
        self.store.set_state(forum.CURSOR_SENT_LEGACY, "2026-08-04T15:33:00Z")
        self.store.commit()
        forum.run(self.store)
        self.assertEqual(self.cards, [])
        self.store.execute(
            "UPDATE tasks SET state='done', outcome='succeeded', "
            "finished_at='2026-08-04T15:41:00Z' WHERE id='t2'")
        self.store.commit()
        forum.run(self.store)
        self.assertEqual(len(self.cards), 1)
        self.assertIn("МЫ НАПИСАЛИ", self.cards[0])


if __name__ == "__main__":
    unittest.main()
