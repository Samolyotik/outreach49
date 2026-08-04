"""Слой решения: как вердикт движка превращается в наши действия.

Движок здесь подставной — его собственное поведение проверяется в
`test_inbound_decision`. Тут важно другое: что мы делаем с каждым из его
вердиктов и, главное, чего не делаем.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
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
            "peer_username, text, sent_at, raw, created_at) "
            "VALUES(5001,821,'private_dm','@someone','someone',?,?,'{}',?)",
            ("Сколько стоит?", now(), now()),
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
            "peer_username, text, sent_at, raw, created_at) "
            "VALUES(?,821,'private_dm',?,?,?,?,'{}',?)",
            (ident, peer, peer.lstrip("@"), text, now(), now()),
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


class LlmBoundaryTests(unittest.TestCase):
    """Граница с моделью — внешняя команда: JSON на stdin, JSON на stdout.

    Модель здесь поддельная, но путь настоящий: подпроцесс, разбор ответа,
    проверки контракта, постановка в очередь.
    """

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
        thread_id = new_id("thread")
        self.store.execute(
            # last_outbound_at обязателен: без него диалог считается чужим и
            # проход отдаст его менеджеру, не дойдя до модели.
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, last_outbound_at, created_at, updated_at) "
            "VALUES(?,821,'@someone',?,'private_dm','open',?,?,?)",
            (thread_id, contact["id"], now(), now(), now()),
        )
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, sent_at, raw, created_at) "
            "VALUES(7001,821,'private_dm','@someone','someone',?,?,'{}',?)",
            ("Сколько стоит?", now(), now()),
        )
        # Разговор начали мы — иначе сработают ворота на посторонних и до
        # модели дело не дойдёт. Сами ворота проверяются в StrangerGateTests.
        self.store.execute(
            "INSERT INTO history(id, thread_id, direction, text, sent_at, "
            "created_at) VALUES(?,?,'outbound','Здравствуйте!',?,?)",
            (new_id("hist"), thread_id, now(), now()),
        )
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fake_model(self, payload: str) -> str:
        """Скрипт-заглушка в роли OUTREACH_LLM_COMMAND."""
        path = Path(self.tmp.name) / "model.py"
        path.write_text(
            "import sys, json\n"
            "sys.stdin.read()\n"
            f"sys.stdout.write({payload!r})\n",
            encoding="utf-8",
        )
        return f"{sys.executable} {path}"

    def test_a_good_answer_travels_all_the_way_to_the_queue(self):
        answer = json.dumps({
            "action": "reply",
            "intent": "pricing_question",
            "reply_text": "Тарифы от 29 000 ₽. Показать бесплатный тест?",
            "confidence": 0.9, "risk_level": "low",
            "next_state": "FAQ automation", "handoff_reason": "",
            "handoff_kind": "none", "matched_direct_invite_sector_id": "",
            "knowledge_gap": "", "collected_fields_update": {},
            "coverage_complete": True, "reason": "",
            "turn_items": [{
                "item_id": "1", "topic": "pricing", "user_item": "цена",
                "user_evidence": "Сколько стоит", "status": "answered",
                "answer_summary": "назвал тарифы",
                "reply_evidence": "Тарифы от 29 000 ₽",
                "source_ids": ["v1:answer_cards/pricing.md"],
            }],
        }, ensure_ascii=False)

        result = autoreply.run(self.store, self.settings,
                               command=self.fake_model(answer))

        self.assertEqual(result["queued"], 1, result)
        task = self.store.one("SELECT * FROM tasks WHERE campaign_id = ?",
                              (replies.AUTO_CAMPAIGN_ID,))
        self.assertIn("29 000", json.loads(task["params"])["text"])

    def test_a_broken_model_answer_sends_nothing(self):
        """Мусор вместо JSON не должен превращаться в сообщение человеку."""
        result = autoreply.run(self.store, self.settings,
                               command=self.fake_model("это не json"))

        self.assertEqual(result["queued"], 0, result)
        self.assertIsNone(
            self.store.one("SELECT * FROM tasks WHERE campaign_id = ?",
                           (replies.AUTO_CAMPAIGN_ID,))
        )
        card = self.store.one("SELECT reason FROM handoffs WHERE status = 'new'")
        self.assertIsNotNone(card, "должна остаться карточка менеджеру")


class StrangerGateTests(unittest.TestCase):
    """Отвечаем только там, где первое слово было нашим."""

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

        contact = entities.add_contact(self.store, username="stranger",
                                       segment="inbound", actor="test")
        self.contact_id = contact["id"]
        self.thread_id = new_id("thread")
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, created_at, updated_at) "
            "VALUES(?,821,'@stranger',?,'private_dm','open',?,?)",
            (self.thread_id, self.contact_id, now(), now()),
        )
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, sent_at, raw, created_at) "
            "VALUES(8001,821,'private_dm','@stranger','stranger',?,?,'{}',?)",
            ("سلام خوبی؟", now(), now()),
        )
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def exploding_model(self):
        def caller(*args, **kwargs):
            raise AssertionError("модель не должна вызываться для постороннего")
        return caller

    def test_a_stranger_gets_a_card_and_no_model_call(self):
        result = autoreply.run(self.store, self.settings,
                               llm_caller=self.exploding_model())

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["queued"], 0)
        card = self.store.one("SELECT reason FROM handoffs WHERE status = 'new'")
        self.assertEqual(card["reason"], "входящее от постороннего")

    def test_prior_history_makes_it_ours(self):
        self.store.execute(
            "INSERT INTO history(id, thread_id, direction, text, sent_at, "
            "created_at) VALUES(?,?,'outbound','Здравствуйте!',?,?)",
            (new_id("hist"), self.thread_id, now(), now()),
        )
        self.store.commit()
        thread = dict(self.store.one("SELECT * FROM threads WHERE id = ?",
                                     (self.thread_id,)))

        self.assertTrue(autoreply.we_started_it(self.store, thread))

    def test_the_gate_can_be_opened_deliberately(self):
        """С открытыми воротами входящее идёт обычным путём — через движок.

        На этом тексте видно, что предохранителей два и они независимы: ворота
        пропустили постороннего, но движок сам подавил сообщение на чужом языке
        (`intent=spam`, `inbound_non_russian_suppressed`) и отвечать не стал.
        Карточки при этом тоже нет — в отличие от закрытых ворот, где менеджер
        узнал бы о письме.
        """
        self.settings.autoreply_strangers_file.touch()

        result = autoreply.run(self.store, self.settings,
                               llm_caller=self.exploding_model())

        self.assertEqual(result["skipped"], 0, "ворота должны были открыться")
        self.assertEqual(result["queued"], 0, "движок подавляет чужой язык сам")
        self.assertIsNone(
            self.store.one("SELECT id FROM handoffs WHERE status = 'new'")
        )


class ArabicScriptGateTests(unittest.TestCase):
    """Собеседник, записанный арабским письмом, машине не достаётся.

    Подавление чужого языка в движке смотрит на текст сообщения. С арабским
    ником, но русским текстом входящее раньше доезжало до модели и получало
    ответ — эта дыра здесь и закрывается.
    """

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
        # Ворота на посторонних не должны маскировать проверку письма.
        self.settings.autoreply_strangers_file.touch()

    def make(self, *, username: str, display_name: str | None = None,
             text: str = "Здравствуйте, а сколько стоит?") -> dict:
        contact = entities.add_contact(
            self.store, username=username, display_name=display_name,
            segment="inbound", actor="test",
        )
        thread_id = new_id("thread")
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, last_outbound_at, created_at, updated_at) "
            "VALUES(?,821,?,?,'private_dm','open',?,?,?)",
            (thread_id, f"@{username}", contact["id"], now(), now(), now()),
        )
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, sent_at, raw, created_at) "
            "VALUES(9600,821,'private_dm',?,?,?,?,'{}',?)",
            (f"@{username}", username, text, now(), now()),
        )
        self.store.commit()
        return dict(self.store.one("SELECT * FROM inbound WHERE id = 9600"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def exploding_model(self):
        def caller(*args, **kwargs):
            raise AssertionError("модель звали для арабского собеседника")
        return caller

    def test_arabic_username_is_skipped_before_the_model(self):
        self.make(username="ahmadian3324", display_name="احمدیان")

        result = autoreply.run(self.store, self.settings,
                               llm_caller=self.exploding_model())

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["queued"], 0)
        card = self.store.one("SELECT reason FROM handoffs WHERE status = 'new'")
        self.assertEqual(card["reason"], "собеседник записан арабским письмом")

    def test_arabic_in_the_peer_field_is_enough(self):
        inbound = self.make(username="someone")
        self.store.execute(
            "UPDATE inbound SET peer_username = ? WHERE id = 9600", ("سلام",))
        self.store.commit()
        thread = autoreply.thread_for(
            self.store, dict(self.store.one("SELECT * FROM inbound WHERE id = 9600")))

        self.assertTrue(autoreply.arabic_script_peer(
            self.store,
            dict(self.store.one("SELECT * FROM inbound WHERE id = 9600")),
            thread,
        ))

    def test_a_latin_nickname_still_reaches_the_model(self):
        """Правило про письмо, а не про происхождение: ali_khan проходит."""
        self.make(username="ali_khan", display_name="Ali Khan")
        called = []

        def caller(*args, **kwargs):
            called.append(True)
            raise RuntimeError("модель недоступна")

        autoreply.run(self.store, self.settings, llm_caller=caller)

        self.assertTrue(called, "латинский ник не должен отсекаться")

    def test_the_rule_cannot_be_bypassed_by_calling_handle(self):
        """Ворота на посторонних живут в проходе, а это правило — в самом ходе."""
        inbound = self.make(username="ahmadian3324", display_name="احمدیان")

        with self.assertRaises(autoreply.AutoReplyError):
            autoreply.handle(self.store, inbound,
                             llm_caller=self.exploding_model())


class RepeatedRepliesTests(unittest.TestCase):
    """Одному человеку можно отвечать много раз.

    Уникальность (кампания, контакт) — правило про рассылку: второй заход на
    сегмент не должен слать человеку второе «первое касание». К ответам оно
    неприменимо: разговор продолжается, и 03.08 сплошной индекс уронил второй
    ответ с IntegrityError.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.store = Store(tmp / "b.sqlite")
        accounts_mod.sync(self.store, SNAPSHOT)
        contact = entities.add_contact(self.store, username="someone",
                                       segment="inbound", actor="test")
        self.contact_id = contact["id"]
        self.thread_id = new_id("thread")
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, created_at, updated_at) "
            "VALUES(?,821,'@someone',?,'private_dm','open',?,?)",
            (self.thread_id, self.contact_id, now(), now()),
        )
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_inbound(self, ident: int, text: str):
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, sent_at, raw, created_at) "
            "VALUES(?,821,'private_dm','@someone','someone',?,?,'{}',?)",
            (ident, text, now(), now()),
        )
        self.store.commit()
        return dict(self.store.one("SELECT * FROM inbound WHERE id = ?", (ident,)))

    def test_a_second_reply_to_the_same_person_is_allowed(self):
        first = self.add_inbound(1, "Сколько стоит?")
        autoreply.apply(self.store, first,
                        verdict("auto_reply", reply_text="Тарифы от 29 000 ₽."))
        self.store.execute(
            "UPDATE tasks SET state = 'done', dispatched_at = ? "
            "WHERE campaign_id = ?", (now(), replies.AUTO_CAMPAIGN_ID))
        self.store.commit()

        second = self.add_inbound(2, "Как вас зовут?")
        autoreply.apply(self.store, second,
                        verdict("auto_reply", reply_text="Меня зовут Юрий."))

        tasks = self.store.query(
            "SELECT id FROM tasks WHERE campaign_id = ?",
            (replies.AUTO_CAMPAIGN_ID,))
        self.assertEqual(len(tasks), 2, "второй ответ обязан ставиться")

    def test_first_touch_uniqueness_still_holds_for_outreach(self):
        """Защита рассылки от повторного касания должна остаться."""
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, segment, mode, status, "
            "daily_cap, per_account_daily_cap, params, ttl_hours, created_at, "
            "updated_at) VALUES('c1','c1','send_private_dm','','immediate',"
            "'active',9,9,'{}',48,?,?)", (now(), now()))
        def add_outreach_task():
            self.store.execute(
                "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
                "action, params, mode, scheduled_at, state, created_at, "
                "updated_at) VALUES(?,'c1',?,821,'send_private_dm','{}',"
                "'immediate',?,'planned',?,?)",
                (new_id("task"), self.contact_id, now(), now(), now()),
            )

        add_outreach_task()
        self.store.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            add_outreach_task()


class HandoffVocabularyTests(unittest.TestCase):
    """Обещание менеджера обязано доходить до менеджера.

    04.08 машина пять раз написала человеку «передаю менеджеру», и ни одной
    карточки заведено не было: в списке стояло имя `manager_handoff`, которого
    движок не выдаёт, а настоящий вердикт зовётся `reply_and_handoff`. Условие
    не совпадало, и несовпадение выглядело как штатная работа.

    Опаснее всего то, что с включением автоответов поллер перестал заводить
    карточки сам: этот список стал единственным путём к человеку.
    """

    def test_handoff_decisions_are_real_engine_verdicts(self):
        unknown = autoreply.HANDOFF_DECISIONS - autoreply.ENGINE_DECISIONS
        self.assertEqual(
            unknown, set(),
            f"вердикт, которого движок не выдаёт: {unknown}. Такое имя не "
            f"сработает никогда и не будет заметно.",
        )

    def test_semantic_handoff_verdicts_are_covered(self):
        """Оба handoff-вердикта словаря обязаны звать человека."""
        for verdict in ("reply_and_handoff", "handoff"):
            with self.subTest(verdict=verdict):
                self.assertIn(verdict, autoreply.HANDOFF_DECISIONS)


class StaleInboundGateTests(unittest.TestCase):
    """Машина не отвечает на то, что успело состариться.

    Автоответ по устройству отвечает так, будто человек написал только что:
    `reply_moment` прямо подтягивает просроченный момент к «сейчас». Для живого
    фида это верно — поллер ходит раз в пятнадцать секунд. Но на аккаунтах
    лежит перенесённая переписка прежних владельцев и очередь недоотвеченного
    за три недели, и любой путь, которым старое сообщение попало бы в `inbound`
    — сбой поллера, повторная публикация из журнала респондера, откат курсора,
    восстановление базы из резервной копии, — обернулся бы бодрым ответом на
    письмо трёхнедельной давности.

    Поэтому у машины есть граница давности. Она не отменяет ответ, а передаёт
    решение человеку: уместен ли ещё ответ, видно только ему.
    """

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

        contact = entities.add_contact(self.store, username="vadim",
                                       segment="inbound", actor="test")
        self.contact_id = contact["id"]
        self.thread_id = new_id("thread")
        # Диалог наш: первое слово было нашим, значит ворота на посторонних
        # открыты и сработать может только гейт давности.
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, last_outbound_at, created_at, updated_at) "
            "VALUES(?,821,'@vadim',?,'private_dm','open',?,?,?)",
            (self.thread_id, self.contact_id, now(), now(), now()),
        )
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_inbound(self, sent_at, *, row_id=9101, text="Добрый день"):
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, sent_at, raw, contact_id, created_at) "
            "VALUES(?,821,'private_dm','@vadim','vadim',?,?,'{}',?,?)",
            (row_id, text, sent_at, self.contact_id, now()),
        )
        self.store.commit()
        return dict(self.store.one("SELECT * FROM inbound WHERE id = ?", (row_id,)))

    def exploding_model(self):
        def caller(*args, **kwargs):
            raise AssertionError("модель не должна вызываться для старого письма")
        return caller

    def test_age_is_counted_from_the_telegram_stamp(self):
        """Давность разговора, а не давность нашей копии.

        `created_at` у записи всегда «сейчас» — она завелась при опросе. Если
        считать по ней, гейт не сработает никогда, ровно в том случае, для
        которого он и написан: старое сообщение, только что попавшее в базу.
        """
        moment = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
        inbound = self.add_inbound("2026-07-20T09:00:00+00:00")

        age = autoreply.inbound_age_hours(inbound, at=moment)

        self.assertAlmostEqual(age, 15 * 24 + 3, places=1)

    def test_a_three_week_old_letter_gets_a_card_and_no_model_call(self):
        self.add_inbound("2026-07-14T08:00:00+00:00")

        result = autoreply.run(self.store, self.settings,
                               llm_caller=self.exploding_model())

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["queued"], 0)
        card = self.store.one("SELECT reason FROM handoffs WHERE status = 'new'")
        self.assertIn("пролежало", card["reason"])
        self.assertIn("предел 24 ч", card["reason"])

    def test_a_fresh_message_still_reaches_the_model(self):
        """Гейт не должен задевать живой фид — там счёт идёт на секунды."""
        self.add_inbound(datetime.now(timezone.utc).isoformat())

        seen = []

        def caller(*args, **kwargs):
            seen.append(True)
            raise RuntimeError("до модели дошло, дальше не важно")

        result = autoreply.run(self.store, self.settings, llm_caller=caller)

        self.assertEqual(result["skipped"], 0, "свежее письмо не должно отсекаться")
        self.assertTrue(seen, "модель должна была быть вызвана")

    def test_an_overnight_message_is_still_answered(self):
        """Написали ночью, разбираем утром — это нормальная переписка.

        Сутки взяты именно поэтому: ночной наплыв должен доезжать до модели,
        отсекается только то, что уже точно не разговор.
        """
        recent = datetime.now(timezone.utc) - timedelta(hours=9)
        inbound = self.add_inbound(recent.isoformat())
        thread = dict(self.store.one("SELECT * FROM threads WHERE id = ?",
                                     (self.thread_id,)))

        self.assertEqual(
            autoreply.skip_reason(self.store, inbound, thread, self.settings), ""
        )

    def test_the_limit_is_configurable_and_capped(self):
        from bridge49.config import HARD_MAX_INBOUND_AGE_HOURS, Limits, clamp

        limits = Limits(reply_max_inbound_age_hours=1000)
        notes = clamp(limits)

        self.assertEqual(limits.reply_max_inbound_age_hours,
                         HARD_MAX_INBOUND_AGE_HOURS)
        self.assertTrue(any("reply_max_inbound_age_hours" in n for n in notes))

    def test_zero_means_the_machine_answers_nothing(self):
        """Осмысленный аварийный режим: выключить машину, не выключая фид."""
        from bridge49.config import Limits, Settings

        settings = Settings(
            home=self.settings.home, db_path=self.settings.db_path, dsn=None,
            limits=Limits(reply_max_inbound_age_hours=0),
            timezone="Europe/Moscow",
        )
        self.add_inbound(datetime.now(timezone.utc).isoformat())

        result = autoreply.run(self.store, settings,
                               llm_caller=self.exploding_model())

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["queued"], 0)

    def test_an_unreadable_stamp_is_treated_as_infinitely_old(self):
        """Предохранитель отказывает в сторону человека, а не модели.

        Даты быть не может: продюсер конверта в Radar пишет её безусловно и
        падает на дате без часового пояса. Значит пустое время — сломанный
        конверт, и решать по нему должен человек.
        """
        inbound = self.add_inbound("не дата")

        self.assertEqual(autoreply.inbound_age_hours(inbound), float("inf"))

    def test_a_missing_stamp_goes_to_a_human(self):
        self.add_inbound(None, row_id=9102)

        result = autoreply.run(self.store, self.settings,
                               llm_caller=self.exploding_model())

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["queued"], 0)
        card = self.store.one("SELECT reason FROM handoffs WHERE status = 'new'")
        self.assertEqual(card["reason"], "у входящего нет времени отправки")
