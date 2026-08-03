"""Слой решения: как вердикт движка превращается в наши действия.

Движок здесь подставной — его собственное поведение проверяется в
`test_inbound_decision`. Тут важно другое: что мы делаем с каждым из его
вердиктов и, главное, чего не делаем.
"""
from __future__ import annotations

import json
from datetime import datetime
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import autoreply, entities, replies  # noqa: E402
from bridge49.store import Store, new_id, now  # noqa: E402

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


def verdict(decision: str, **extra):
    """Минимальное решение движка в том виде, в каком его отдаёт шов."""
    base = {
        "decision": decision,
        "reply_text": "",
        "intent": "neutral",
        "confidence": 0.8,
        "risk_level": "low",
        "validation_warnings": [],
        "collected_fields_update": {},
        "knowledge_gap": "",
        "reason": "",
        "handoff_kind": "none",
    }
    base.update(extra)
    return base


class AutoReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.store = Store(tmp / "b.sqlite")
        accounts_mod.sync(self.store, SNAPSHOT)
        contact = entities.add_contact(
            self.store, username="someone", segment="inbound", actor="test",
        )
        self.contact_id = contact["id"]
        self.thread_id = new_id("thread")
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, created_at, updated_at) "
            "VALUES(?,821,'@someone',?,'private_dm','open',?,?)",
            (self.thread_id, self.contact_id, now(), now()),
        )
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, raw, created_at) "
            "VALUES(5001,821,'private_dm','@someone','someone',?,'{}',?)",
            ("Сколько стоит?", now()),
        )
        self.store.commit()
        self.inbound = dict(
            self.store.one("SELECT * FROM inbound WHERE id = 5001")
        )

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def queued(self):
        return self.store.query(
            "SELECT * FROM tasks WHERE campaign_id = ?",
            (replies.AUTO_CAMPAIGN_ID,),
        )

    # -- что уходит человеку ------------------------------------------------

    def test_confident_answer_is_queued_without_a_review_mark(self):
        result = autoreply.apply(
            self.store, self.inbound,
            verdict("auto_reply", reply_text="Тарифы GO, PLUS и PRO."),
        )

        tasks = self.queued()
        self.assertEqual(len(tasks), 1)
        self.assertEqual(json.loads(tasks[0]["params"])["text"],
                         "Тарифы GO, PLUS и PRO.")
        self.assertIsNone(tasks[0]["review_reason"])
        self.assertEqual(result["review_reason"], "")

    def test_unsure_answer_is_still_sent_but_marked(self):
        """Первый уровень: ответ есть, уверенности нет — шлём и метим."""
        result = autoreply.apply(
            self.store, self.inbound,
            verdict(
                "auto_reply",
                reply_text="Скорее всего, подойдёт.",
                validation_warnings=["reply_evidence_missing"],
                risk_level="high",
            ),
        )

        tasks = self.queued()
        self.assertEqual(len(tasks), 1)
        self.assertIn("reply_evidence_missing", tasks[0]["review_reason"])
        self.assertIn("рискованная тема", tasks[0]["review_reason"])
        self.assertTrue(result["task"])

    def test_knowledge_gap_answers_honestly_and_raises_a_card(self):
        """Второй уровень: не знаем — говорим об этом и зовём человека."""
        result = autoreply.apply(
            self.store, self.inbound,
            verdict("knowledge_gap", knowledge_gap="просят СРО-допуск"),
        )

        tasks = self.queued()
        self.assertEqual(len(tasks), 1)
        self.assertIn("зафиксировал этот вопрос для команды",
                      json.loads(tasks[0]["params"])["text"])
        self.assertIn("нехватка знаний", tasks[0]["review_reason"])

        card = self.store.one("SELECT * FROM handoffs WHERE id = ?",
                              (result["handoff"],))
        self.assertEqual(card["reason"], "knowledge_gap")
        self.assertEqual(card["note"], "просят СРО-допуск")

    # -- чего человеку не уходит --------------------------------------------

    def test_hold_for_review_sends_nothing(self):
        """Третий уровень: контракт сорван — молчим и зовём человека."""
        result = autoreply.apply(
            self.store, self.inbound,
            verdict("hold_for_review", reason="presales_v2_no_turn_items",
                    reply_text="черновик, который нельзя выпускать"),
        )

        self.assertEqual(self.queued(), [])
        self.assertTrue(result["handoff"])
        self.assertEqual(result["sent_text"], "")

    def test_opt_out_closes_the_contact_and_stays_silent(self):
        autoreply.apply(self.store, self.inbound, verdict("opt_out"))

        self.assertEqual(self.queued(), [])
        contact = self.store.one("SELECT opted_out FROM contacts WHERE id = ?",
                                 (self.contact_id,))
        self.assertEqual(contact["opted_out"], 1)
        thread = self.store.one("SELECT state FROM threads WHERE id = ?",
                                (self.thread_id,))
        self.assertEqual(thread["state"], "closed")

    def test_spam_is_ignored_silently(self):
        autoreply.apply(self.store, self.inbound,
                        verdict("ignore", intent="spam"))

        self.assertEqual(self.queued(), [])

    def test_unclear_message_gets_a_polite_boundary_reply(self):
        """Не спам, а невнятица — молчание читалось бы как бан."""
        autoreply.apply(self.store, self.inbound,
                        verdict("ignore", intent="non_russian"))

        tasks = self.queued()
        self.assertEqual(len(tasks), 1)
        self.assertIn("по-русски", json.loads(tasks[0]["params"])["text"])

    # -- память диалога -----------------------------------------------------

    def test_what_the_model_learned_is_remembered(self):
        autoreply.apply(
            self.store, self.inbound,
            verdict("auto_reply", reply_text="Понял, вы по грузоперевозкам.",
                    collected_fields_update={"sector": "логистика"}),
        )

        thread = dict(self.store.one("SELECT * FROM threads WHERE id = ?",
                                     (self.thread_id,)))
        self.assertEqual(autoreply.discovery_context(thread),
                         {"sector": "логистика"})

    def test_history_is_assembled_in_time_order(self):
        self.store.execute(
            "INSERT INTO history(id, thread_id, direction, text, sent_at, "
            "created_at) VALUES(?,?,'outbound','Здравствуйте!',?,?)",
            (new_id("hist"), self.thread_id, "2026-07-01T10:00:00+00:00", now()),
        )
        self.store.commit()
        thread = dict(self.store.one("SELECT * FROM threads WHERE id = ?",
                                     (self.thread_id,)))

        history = autoreply.conversation_history(self.store, thread)

        self.assertEqual([item["direction"] for item in history],
                         ["outbound", "inbound"])
        self.assertEqual(history[0]["text"], "Здравствуйте!")
        self.assertEqual(history[1]["text"], "Сколько стоит?")


if __name__ == "__main__":
    unittest.main()


class AutoReplyRunTests(unittest.TestCase):
    """Проход разбора: очередь входящих, задержка, устойчивость к сбоям."""

    def setUp(self):
        from bridge49.config import Limits, Settings

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.store = Store(tmp / "b.sqlite")
        accounts_mod.sync(self.store, SNAPSHOT)
        self.settings = Settings(
            home=tmp, db_path=tmp / "b.sqlite", dsn=None, limits=Limits(),
            timezone="Europe/Moscow",
        )
        (tmp / "var").mkdir(parents=True, exist_ok=True)
        self.settings.autoreply_file.touch()

        contact = entities.add_contact(self.store, username="someone",
                                       segment="inbound", actor="test")
        self.thread_id = new_id("thread")
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, created_at, updated_at) "
            "VALUES(?,821,'@someone',?,'private_dm','open',?,?)",
            (self.thread_id, contact["id"], now(), now()),
        )
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_inbound(self, ident: int, text: str, peer: str = "@someone"):
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, raw, created_at) "
            "VALUES(?,821,'private_dm',?,?,?,'{}',?)",
            (ident, peer, peer.lstrip("@"), text, now()),
        )
        self.store.commit()

    def test_switch_off_means_nothing_happens(self):
        self.settings.autoreply_file.unlink()
        self.add_inbound(1, "привет")

        result = autoreply.run(self.store, self.settings)

        self.assertFalse(result["enabled"])
        self.assertEqual(result["handled"], 0)

    def test_only_the_newest_message_of_a_burst_is_answered(self):
        """Три сообщения подряд — один ответ, а не три."""
        self.add_inbound(1, "здравствуйте")
        self.add_inbound(2, "хочу спросить")
        self.add_inbound(3, "сколько стоит?")

        pending = autoreply.pending(self.store)

        self.assertEqual([row["id"] for row in pending], [3])
        earlier = self.store.query(
            "SELECT id, handled FROM inbound WHERE id IN (1,2) ORDER BY id")
        self.assertEqual([r["handled"] for r in earlier], [1, 1])

    def test_messages_from_different_people_are_all_answered(self):
        self.add_inbound(1, "вопрос", peer="@someone")
        self.add_inbound(2, "другой вопрос", peer="@another")

        pending = autoreply.pending(self.store)

        self.assertEqual(sorted(row["id"] for row in pending), [1, 2])

    def test_a_broken_message_does_not_block_the_queue(self):
        """Иначе одно неразбираемое входящее стояло бы в голове очереди вечно."""
        self.add_inbound(1, "вопрос без диалога", peer="@nothread")

        result = autoreply.run(self.store, self.settings)

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["handled"], 1)
        row = self.store.one("SELECT handled FROM inbound WHERE id = 1")
        self.assertEqual(row["handled"], 1)

    def test_reply_moment_is_delayed_and_stable(self):
        self.add_inbound(1, "вопрос")
        inbound = dict(self.store.one("SELECT * FROM inbound WHERE id = 1"))

        first = autoreply.reply_moment(inbound, self.settings)
        second = autoreply.reply_moment(inbound, self.settings)

        self.assertEqual(first, second)
        self.assertGreaterEqual(
            datetime.fromisoformat(first),
            datetime.fromisoformat(str(inbound["created_at"])),
        )
