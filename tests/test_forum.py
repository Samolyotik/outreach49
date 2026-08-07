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
        self.assertIsNone(forum.ensure_topic(self.store, "c1", chat_id="-100123"))
        self.assertEqual([c for c in self.calls if c[0] == "createForumTopic"], [])

    def test_topic_appears_once_the_person_answered(self):
        self.add_reply()
        self.assertEqual(forum.ensure_topic(self.store, "c1", chat_id="-100123"), 777)

    def test_topic_is_created_once_and_remembered(self):
        self.add_reply()
        first = forum.ensure_topic(self.store, "c1", chat_id="-100123")
        second = forum.ensure_topic(self.store, "c1", chat_id="-100123")
        self.assertEqual(first, 777)
        self.assertEqual(second, 777)
        created = [c for c in self.calls if c[0] == "createForumTopic"]
        self.assertEqual(len(created), 1, "ветка заведена дважды")
        stored = self.store.one("SELECT forum_thread_id FROM contacts WHERE id='c1'")
        self.assertEqual(stored["forum_thread_id"], 777)

    def test_topic_name_identifies_the_person(self):
        self.add_reply()
        forum.ensure_topic(self.store, "c1", chat_id="-100123")
        name = [c for c in self.calls if c[0] == "createForumTopic"][0][1]["name"]
        self.assertIn("@someone", name)
        self.assertIn("TGRadar", name)

    def test_no_contact_has_no_topic(self):
        self.assertIsNone(forum.ensure_topic(self.store, None, chat_id="-100123"))
        self.assertIsNone(forum.ensure_topic(self.store, "нет такого", chat_id="-100123"))

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
        forum.send("текст", thread_id=forum.ensure_topic(self.store, "c1", chat_id="-100123"))
        sent = [c for c in self.calls if c[0] == "sendMessage"][0][1]
        self.assertEqual(sent["message_thread_id"], 777)

    def test_topic_failure_stops_the_stream_instead_of_losing_the_message(self):
        """Отказ завести ветку обязан быть слышен наверху.

        Раньше здесь возвращался None: событие уходило как «нет ветки»,
        курсор двигался, и переписка исчезала — в General писать запрещено,
        и уехать ей было некуда. Проверка отличает два непохожих случая:
        «человек ещё не отвечал» — пропуск, «Telegram не дал завести ветку» —
        остановка потока.
        """
        def boom(method, payload, **kw):
            if method == "createForumTopic":
                raise forum.ForumError("not enough rights to create a topic")
            return {"message_id": 1}
        forum._call = boom
        self.add_reply()
        with self.assertRaises(forum.ForumError):
            forum.ensure_topic(self.store, "c1", chat_id="-100123")
        # А без ответа собеседника — по-прежнему тихий пропуск.
        self.store.execute("DELETE FROM inbound")
        self.store.commit()
        self.assertIsNone(
            forum.ensure_topic(self.store, "c1", chat_id="-100123"))


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


class RoutingTests(unittest.TestCase):
    """Заявки и переписка живут в разных группах.

    Пока они шли вместе, группа перестала читаться: за двое суток 229
    сообщений, из них 153 зеркала разговоров. Заявка — единственное, где от
    человека ждут действия, и она обязана быть одна в своей группе.
    """

    MANAGER = "-1004444189462"
    DIALOGS = "-1004447957104"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(804,'acc804','dm_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, created_at, "
            "updated_at) VALUES('c1','user','someone','default',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, raw, contact_id, created_at) "
            "VALUES(9001,804,'private_dm','@someone','someone','Да','{}','c1',?)",
            (now(),))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('c','кампания','send_private_dm',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, outcome, finished_at, "
            "created_at, updated_at) VALUES('t1','c','c1',804,'send_private_dm',"
            "'{\"text\":\"Здравствуйте!\"}','immediate',?,'done','succeeded',?,?,?)",
            (now(), now(), now(), now()))
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, "
            "created_at, updated_at) VALUES('th1',804,'@someone','c1',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO handoffs(id, thread_id, reason, note, status, "
            "created_at, updated_at) "
            "VALUES('h1','th1','нужен человек','сфера','new',?,?)",
            (now(), now()))
        self.store.commit()

        self.calls: list[tuple[str, dict]] = []
        self._real = forum._call
        forum._call = self.fake_call
        self._env = {k: os.environ.get(k) for k in
                     (forum.CHAT_ID_ENV, forum.DIALOG_CHAT_ID_ENV,
                      forum.ENABLED_ENV, forum.BOT_TOKEN_ENV)}
        os.environ[forum.CHAT_ID_ENV] = self.MANAGER
        os.environ[forum.DIALOG_CHAT_ID_ENV] = self.DIALOGS
        os.environ[forum.ENABLED_ENV] = "1"
        os.environ[forum.BOT_TOKEN_ENV] = "тест"

    def tearDown(self):
        forum._call = self._real
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.store.close()
        self.tmp.cleanup()

    def fake_call(self, method, payload, **kw):
        self.calls.append((method, dict(payload)))
        if method == "createForumTopic":
            # У каждой группы свои номера веток: если код перепутает колонки,
            # тест это увидит.
            return {"message_thread_id": 555 if payload["chat_id"] == self.DIALOGS
                    else 777}
        return {"message_id": 100 + len(self.calls)}

    def sent(self) -> list[dict]:
        return [payload for method, payload in self.calls
                if method == "sendMessage"]

    def test_dialogs_and_handoffs_go_to_different_groups(self):
        forum.run(self.store)
        by_chat: dict[str, list[str]] = {}
        for payload in self.sent():
            by_chat.setdefault(payload["chat_id"], []).append(payload["text"])
        self.assertIn(self.DIALOGS, by_chat)
        self.assertIn(self.MANAGER, by_chat)
        self.assertTrue(all("НУЖЕН ЧЕЛОВЕК" not in t for t in by_chat[self.DIALOGS]))
        self.assertTrue(all("НУЖЕН ЧЕЛОВЕК" in t for t in by_chat[self.MANAGER]),
                        "в группу заявок уехало что-то кроме заявки")
        self.assertTrue(any("МЫ НАПИСАЛИ" in t for t in by_chat[self.DIALOGS]))
        self.assertTrue(any("НАМ ОТВЕТИЛИ" in t for t in by_chat[self.DIALOGS]))

    def test_each_group_keeps_its_own_topic(self):
        forum.run(self.store)
        row = self.store.one(
            "SELECT forum_thread_id, dialog_thread_id FROM contacts WHERE id='c1'")
        self.assertEqual(row["forum_thread_id"], 777)
        self.assertEqual(row["dialog_thread_id"], 555)
        for payload in self.sent():
            expected = 555 if payload["chat_id"] == self.DIALOGS else 777
            self.assertEqual(payload["message_thread_id"], expected,
                             "ветка одной группы подставлена в другую")

    def test_without_the_dialog_group_everything_stays_as_before(self):
        os.environ.pop(forum.DIALOG_CHAT_ID_ENV)
        forum.run(self.store)
        self.assertEqual({p["chat_id"] for p in self.sent()}, {self.MANAGER})

    def test_posts_are_remembered_for_later_cleanup(self):
        forum.run(self.store)
        rows = list(self.store.query(
            "SELECT chat_id, message_id, kind FROM forum_posts ORDER BY message_id"))
        self.assertEqual(len(rows), len(self.sent()))
        kinds = {r["kind"] for r in rows}
        self.assertEqual(kinds, {"outgoing", "inbound", "handoff"})
        handoff = [r for r in rows if r["kind"] == "handoff"][0]
        self.assertEqual(handoff["chat_id"], self.MANAGER)
        for r in rows:
            if r["kind"] != "handoff":
                self.assertEqual(r["chat_id"], self.DIALOGS)

    def test_cursor_holds_when_the_dialog_group_refuses_topics(self):
        """Права ещё не выданы — переписка должна подождать, а не пропасть."""
        def refuse(method, payload, **kw):
            self.calls.append((method, dict(payload)))
            if method == "createForumTopic" and payload["chat_id"] == self.DIALOGS:
                raise forum.ForumError("not enough rights to create a topic")
            if method == "createForumTopic":
                return {"message_thread_id": 777}
            return {"message_id": 1}
        forum._call = refuse
        result = forum.run(self.store)
        self.assertEqual(result["ошибок"], 2, "оба потока должны были встать")
        self.assertEqual(self.store.get_state(forum.CURSOR_SENT, ""), "")
        self.assertEqual(self.store.get_state(forum.CURSOR_INBOUND, "0") or "0", "0")
        # Заявка при этом уехала: её группа работает.
        self.assertTrue(any("НУЖЕН ЧЕЛОВЕК" in p["text"] for p in self.sent()))


class HandoffHistoryTests(unittest.TestCase):
    """Карточка обязана нести разговор, который к ней привёл.

    05.08 переписку из группы заявок вычистили, чтобы та читалась, — и
    карточки остались без контекста: «назвал сферу», а какую и в ответ на
    что, по карточке не восстановить. Разговор поэтому идёт внутрь карточки,
    а не отдельными сообщениями рядом.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(804,'a','dm_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES('c1','user','someone',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('c','к','send_private_dm',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, outcome, finished_at, created_at, "
            "updated_at) VALUES('t1','c','c1',804,'send_private_dm',?,'immediate',"
            "'2026-08-05T07:00:00+00:00','done','succeeded',"
            "'2026-08-05T07:00:00+00:00',?,?)",
            ('{"text":"Здравствуйте! Увидели ваше сообщение про рекламу."}',
             now(), now()))
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, peer_username, "
            "text, raw, contact_id, created_at) VALUES(9001,804,'private_dm',"
            "'@someone','someone','Да, интересно','{}','c1',"
            "'2026-08-05T07:30:00+00:00')")
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, peer_username, "
            "text, raw, contact_id, created_at) VALUES(9002,804,'private_dm',"
            "'@someone','someone','Медицина','{}','c1',"
            "'2026-08-05T08:00:00+00:00')")
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def card(self):
        row = {"reason": "reply_and_handoff", "note": "назвал сферу",
               "account_id": 804, "peer_key": "@someone", "contact_id": "c1",
               "username": "someone"}
        return forum.handoff_card(row, forum.conversation(self.store, "c1"))

    def test_conversation_is_ordered_and_attributed(self):
        talk = forum.conversation(self.store, "c1")
        self.assertEqual([t["кто"] for t in talk], ["мы", "он", "он"])
        self.assertEqual([t["текст"] for t in talk],
                         ["Здравствуйте! Увидели ваше сообщение про рекламу.",
                          "Да, интересно", "Медицина"])

    def test_card_carries_the_conversation(self):
        card = self.card()
        self.assertIn("НУЖЕН ЧЕЛОВЕК", card)
        self.assertIn("── о чём говорили ──", card)
        self.assertIn("Да, интересно", card)
        self.assertIn("Медицина", card)

    def test_card_without_history_is_still_valid(self):
        """Разговора может не быть вовсе — карточка от этого не ломается."""
        card = forum.handoff_card(
            {"reason": "r", "note": "n", "account_id": 1, "peer_key": "@x",
             "contact_id": "c9", "username": None}, [])
        self.assertIn("НУЖЕН ЧЕЛОВЕК", card)
        self.assertNotIn("── о чём говорили ──", card)

    def test_conversation_is_capped(self):
        for index in range(40):
            self.store.execute(
                "INSERT INTO inbound(id, account_id, surface, peer_key, text, "
                "raw, contact_id, created_at) VALUES(?,804,'private_dm','@someone',"
                "?, '{}','c1',?)",
                (10000 + index, "реплика %d" % index,
                 "2026-08-05T09:%02d:00+00:00" % index))
        self.store.commit()
        talk = forum.conversation(self.store, "c1")
        self.assertEqual(len(talk), forum.HISTORY_LINES)
        # Берём последние, а не первые: менеджеру важен конец разговора.
        self.assertIn("реплика 39", [t["текст"] for t in talk])
        self.assertNotIn("реплика 0", [t["текст"] for t in talk])

    def test_long_replies_are_trimmed(self):
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, text, raw, "
            "contact_id, created_at) VALUES(9999,804,'private_dm','@someone',?,"
            "'{}','c1','2026-08-05T10:00:00+00:00')", ("я" * 900,))
        self.store.commit()
        talk = forum.conversation(self.store, "c1")
        self.assertTrue(all(len(t["текст"]) <= forum.HISTORY_CHARS for t in talk))

    def test_card_fits_a_telegram_message(self):
        for index in range(12):
            self.store.execute(
                "INSERT INTO inbound(id, account_id, surface, peer_key, text, "
                "raw, contact_id, created_at) VALUES(?,804,'private_dm','@someone',"
                "?, '{}','c1',?)",
                (20000 + index, "щ" * 900, "2026-08-05T11:%02d:00+00:00" % index))
        self.store.commit()
        self.assertLessEqual(len(self.card()), 4000)

    def test_no_contact_means_no_history(self):
        self.assertEqual(forum.conversation(self.store, None), [])
        self.assertEqual(forum.conversation(self.store, "нет такого"), [])

    def test_chain_is_capped_at_ten(self):
        """Договорённая граница: цепочка до десяти сообщений, не длиннее."""
        self.assertEqual(forum.HISTORY_LINES, 10)


if __name__ == "__main__":
    unittest.main()
