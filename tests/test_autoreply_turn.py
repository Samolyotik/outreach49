"""Молчаливые решения и карточка менеджеру при повторе.

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

from bridge49 import autoreply  # noqa: E402
from bridge49.store import Store, now  # noqa: E402


def stamp(shift_seconds: float) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=shift_seconds)).isoformat()


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
        first = autoreply.open_handoff(self.store, self.thread, "reply_and_handoff",
                                       "исходная заметка")
        autoreply.open_handoff(self.store, self.thread, "", "")
        row = dict(self.store.one("SELECT * FROM handoffs WHERE id=?", (first,)))
        self.assertEqual(row["reason"], "reply_and_handoff")
        self.assertEqual(row["note"], "исходная заметка")


if __name__ == "__main__":
    unittest.main()
