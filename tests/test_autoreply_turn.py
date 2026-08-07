"""Ход собеседника, молчаливые решения и карточка при повторе.

Каждый тест здесь стоит за конкретным разговором 05.08, где машина повела себя
плохо на живом человеке. Поэтому проверяется не форма, а именно то поведение,
которое тогда подвело.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49 import autoreply, replies  # noqa: E402
from bridge49.store import Store, now  # noqa: E402


def stamp(shift_seconds: float) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=shift_seconds)).isoformat()


class TurnWindowTests(unittest.TestCase):
    """«Он дописал мысль» — это один ход, а не два."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(821,'a','dm_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('autoreplies','а','reply_private_dm',?,?)", (now(), now()))
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, inbound_id: int, text: str, *, age: float,
            peer: str = "@somebody", mid_conversation: bool = True) -> None:
        """Сообщение собеседника.

        По умолчанию — середина разговора: мы ему уже отвечали. Окно тишины
        действует только там, поэтому эту предысторию тест и заводит.
        """
        contact = f"c{peer.strip('@')}"
        if mid_conversation and not self.store.one(
                "SELECT 1 FROM contacts WHERE id=?", (contact,)):
            self.store.execute(
                "INSERT INTO contacts(id, kind, username, created_at, "
                "updated_at) VALUES(?,'user',?,?,?)",
                (contact, peer.strip("@"), now(), now()))
            self.store.execute(
                "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
                "action, params, mode, scheduled_at, state, created_at, "
                "updated_at) VALUES(?,'autoreplies',?,821,'reply_private_dm',"
                "'{}','immediate',?,'done',?,?)",
                (f"t_{contact}", contact, now(), now(), now()))
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, text, "
            "raw, contact_id, sent_at, handled, created_at) "
            "VALUES(?,821,'private_dm',?,?,'{}',?,?,0,?)",
            (inbound_id, peer, text, contact if mid_conversation else None,
             stamp(-age), stamp(-age)))
        self.store.commit()

    def test_fresh_message_waits_for_the_person_to_finish(self):
        """Ровно случай 11:54:21 → 11:54:32: на первое отвечать рано."""
        self.add(1, "И как это работает?", age=5)
        self.assertEqual(autoreply.pending(self.store), [])

    def test_first_message_from_a_new_person_is_not_delayed(self):
        """Окно — про середину разговора, а не про первое обращение."""
        self.add(1, "Здравствуйте", age=2, mid_conversation=False)
        self.assertEqual(len(autoreply.pending(self.store)), 1)

    def test_quiet_turn_is_answered(self):
        self.add(1, "И как это работает?", age=autoreply.TURN_QUIET + 5)
        self.assertEqual(len(autoreply.pending(self.store)), 1)

    def test_series_collapses_into_the_last_message(self):
        self.add(1, "И как это работает?", age=autoreply.TURN_QUIET + 20)
        self.add(2, "Мне писать люди будут?", age=autoreply.TURN_QUIET + 5)
        self.assertEqual([r["id"] for r in autoreply.pending(self.store)], [2])
        older = self.store.one("SELECT handled FROM inbound WHERE id=1")
        self.assertEqual(older["handled"], 1, "обогнанное помечено разобранным")

    def test_a_person_who_never_pauses_still_gets_an_answer(self):
        """Иначе пишущий без пауз не дождался бы ответа никогда."""
        self.add(1, "первое", age=autoreply.TURN_MAX_WAIT + 60)
        self.add(2, "второе", age=1)
        self.assertEqual([r["id"] for r in autoreply.pending(self.store)], [2])

    def test_different_people_do_not_delay_each_other(self):
        self.add(1, "старое", age=autoreply.TURN_QUIET + 10, peer="@first")
        self.add(2, "свежее", age=2, peer="@second")
        self.assertEqual([r["id"] for r in autoreply.pending(self.store)], [1])

    def test_unreadable_timestamp_does_not_stall_the_answer(self):
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, text, "
            "raw, sent_at, handled, created_at) "
            "VALUES(9,821,'private_dm','@x','привет','{}','не дата',0,'не дата')")
        self.store.commit()
        self.assertEqual(len(autoreply.pending(self.store)), 1)


class SilentDecisionTests(unittest.TestCase):
    """Написанный ответ не должен пропадать из-за имени решения."""

    def test_pause_conversation_is_no_longer_silent(self):
        """13 текстов из 14 пропали 05.08 именно здесь.

        Нормализатор схлопывает в это имя два разных действия движка:
        `reply_and_pause` с обязательным текстом и голый `pause` без текста.
        Держать их вместе в молчаливых значило выбрасывать написанное.
        """
        self.assertNotIn("pause_conversation", autoreply.SILENT_DECISIONS)

    def test_contract_failure_stays_silent_by_design(self):
        """Сорванный контракт молчит намеренно, и это не тот же случай.

        Соблазн ответить заготовкой есть: 05.08 так пропал самый содержательный
        вопрос суток. Но `hold_for_review` срабатывает и при обрыве связи с
        моделью — тогда одна и та же заготовка ушла бы всем сразу. Пока эти два
        случая неотличимы по вердикту, молчание безопаснее.
        """
        self.assertIn("hold_for_review", autoreply.SILENT_DECISIONS)

    def test_refusal_stays_silent(self):
        """Прямому отказу отвечать не надо — это не регресс, а граница."""
        self.assertIn("opt_out", autoreply.SILENT_DECISIONS)
        self.assertIn("ignore", autoreply.SILENT_DECISIONS)

    def test_every_silent_decision_is_a_real_engine_verdict(self):
        for decision in autoreply.SILENT_DECISIONS:
            self.assertIn(decision, autoreply.ENGINE_DECISIONS)

    def test_contract_failure_still_reaches_a_human(self):
        self.assertIn("hold_for_review", autoreply.HANDOFF_DECISIONS)


class HandoffRefreshTests(unittest.TestCase):
    """Повторное обещание менеджера обязано создавать новый сигнал."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(821,'a','dm_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES('c1','user','x',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, created_at, updated_at) "
            "VALUES('th1',821,'@x','c1','private_dm','open',?,?)",
            (now(), now()))
        self.store.commit()
        self.thread = dict(self.store.one("SELECT * FROM threads WHERE id='th1'"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_repeat_refreshes_the_open_card(self):
        first = autoreply.open_handoff(
            self.store, self.thread, "reply_and_handoff", "сфера подтверждена")
        # Состариваем карточку: в жизни повтор приходит через сутки, а
        # отметка времени у нас с точностью до секунды.
        self.store.execute(
            "UPDATE handoffs SET updated_at='2026-08-03T14:04:03+00:00' "
            " WHERE id=?", (first,))
        self.store.commit()
        row_before = dict(self.store.one(
            "SELECT * FROM handoffs WHERE id=?", (first,)))

        second = autoreply.open_handoff(
            self.store, self.thread, "reply_and_handoff_repeated",
            "человек напомнил, ждёт третьи сутки")

        self.assertEqual(second, first, "вторая карточка по треду не нужна")
        row_after = dict(self.store.one(
            "SELECT * FROM handoffs WHERE id=?", (first,)))
        self.assertEqual(row_after["reason"], "reply_and_handoff_repeated")
        self.assertIn("напомнил", row_after["note"])
        self.assertNotEqual(row_after["updated_at"], row_before["updated_at"],
                            "без новой отметки времени форум не поднимет её")

    def test_empty_reason_does_not_erase_the_old_one(self):
        """Прежнюю заметку не теряем — но и не выдаём за объяснение нового.

        Подмена пустой заметки старой выглядела безобидно, а получалось
        враньё: 06.08 разговор упал во второй раз, и менеджер увидел причину
        от 05.08, которой в тот день не было вовсе. Поэтому старый текст
        остаётся, но помечен как прежний и со своей датой.
        """
        first = autoreply.open_handoff(self.store, self.thread, "reply_and_handoff",
                                       "исходная заметка")
        autoreply.open_handoff(self.store, self.thread, "", "")
        row = dict(self.store.one("SELECT * FROM handoffs WHERE id=?", (first,)))
        self.assertEqual(row["reason"], "reply_and_handoff")
        self.assertIn("исходная заметка", row["note"])
        self.assertIn("подробностей этого отказа не записано", row["note"])
        self.assertIn("прежняя заметка", row["note"])

    def test_the_marker_never_nests_into_itself(self):
        """Повторный отказ без причины не должен растить заметку.

        У @anrri21 06.08 ход повторился два с половиной десятка раз. Без этой
        проверки заметка выросла бы в километр «прежняя: прежняя: прежняя», а
        настоящий текст уехал бы в самый хвост.
        """
        first = autoreply.open_handoff(self.store, self.thread,
                                       "hold_for_review", "исходная заметка")
        seen = set()
        for _ in range(5):
            autoreply.open_handoff(self.store, self.thread, "hold_for_review", "")
            seen.add(dict(self.store.one(
                "SELECT note FROM handoffs WHERE id=?", (first,)))["note"])
        self.assertEqual(len(seen), 1, f"заметка растёт: {seen}")
        note = seen.pop()
        self.assertEqual(note.count("подробностей этого отказа не записано"), 1)
        self.assertIn("исходная заметка", note)

    def test_a_card_without_any_note_says_so(self):
        first = autoreply.open_handoff(self.store, self.thread, "hold_for_review")
        row = dict(self.store.one("SELECT * FROM handoffs WHERE id=?", (first,)))
        self.assertIsNone(row["note"])
        autoreply.open_handoff(self.store, self.thread, "hold_for_review")
        row = dict(self.store.one("SELECT * FROM handoffs WHERE id=?", (first,)))
        self.assertEqual(row["note"], "подробностей этого отказа не записано")




class ReplyAnchorTests(unittest.TestCase):
    """Ответ должен уехать реплаем на то, по чему он собран."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(821,'a','dm_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES('c1','user','x',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, created_at, updated_at) "
            "VALUES('th1',821,'@x','c1','private_dm','open',?,?)", (now(), now()))
        for ident, text in ((76797, "Сколько стоит?"), (76799, "И ещё вопрос")):
            self.store.execute(
                "INSERT INTO inbound(id, account_id, surface, peer_key, "
                "peer_username, text, raw, contact_id, sent_at, handled, "
                "created_at) VALUES(?,821,'private_dm','@x','x',?,'{}','c1',?,1,?)",
                (ident, text, now(), now()))
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def queued_target(self) -> int:
        row = self.store.one(
            "SELECT params FROM tasks WHERE action LIKE 'reply%' "
            " ORDER BY created_at DESC LIMIT 1")
        import json
        return int(json.loads(row["params"])["inbound_notification_id"])

    def test_reply_goes_to_the_message_it_answers(self):
        """05.08: ответ на 76797 уехал реплаем на 76799."""
        replies.queue_reply(self.store, text="Тарифы от 29 000 ₽.",
                            thread_id="th1", inbound_id=76797)
        self.assertEqual(self.queued_target(), 76797)

    def test_without_an_explicit_message_the_last_one_is_used(self):
        """Ручной ответ адресуется последнему написавшему — как и раньше."""
        replies.queue_reply(self.store, text="Отвечаю.", thread_id="th1")
        self.assertEqual(self.queued_target(), 76799)

    def test_a_foreign_message_does_not_leak_into_this_thread(self):
        """Чужой id не должен уводить ответ в другой разговор."""
        replies.queue_reply(self.store, text="Отвечаю.", thread_id="th1",
                            inbound_id=999999)
        self.assertEqual(self.queued_target(), 76799)


if __name__ == "__main__":
    unittest.main()
