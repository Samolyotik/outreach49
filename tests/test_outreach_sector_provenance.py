"""Сферу первого касания знаем мы, а не только собеседник.

В полосах «чаты» и «личка каналов» текст первого касания сам называет сферу:
он написан про подбор и привоз авто и ни про что другое. Значит «Согласны» в
ответ на такое сообщение — это согласие по известной сфере, и переспрашивать
человека незачем.

Проверки ниже стоят по обе стороны от этого удобства. Одна следит, что сфера
подставляется там, где заслужена. Остальные — что она не подставляется больше
нигде: доступ в чужую тестовую группу отзывается только руками, и цена ошибки
тут выше цены лишнего вопроса.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49 import autoreply, outreach_texts  # noqa: E402
from bridge49.store import Store, now  # noqa: E402


class SectorOfFirstTouchTests(unittest.TestCase):
    """Узнавание заготовки: посимвольно и только по своему нику."""

    def test_our_channel_letter_is_recognised(self):
        self.assertEqual(
            outreach_texts.sector_of_first_touch(
                "auto_from_ko_rea",
                outreach_texts.channel_dm("auto_from_ko_rea")),
            outreach_texts.SECTOR_ID)

    def test_our_chat_question_is_recognised(self):
        self.assertEqual(
            outreach_texts.sector_of_first_touch(
                "bestcarskz", outreach_texts.chat_message("bestcarskz")),
            outreach_texts.SECTOR_ID)

    def test_at_sign_does_not_break_the_match(self):
        self.assertEqual(
            outreach_texts.sector_of_first_touch(
                "@bestcarskz", outreach_texts.channel_dm("bestcarskz")),
            outreach_texts.SECTOR_ID)

    def test_someone_elses_variant_is_not_ours(self):
        """Вариант выбирается хешем ника, и чужой вариант — не наше письмо."""
        self.assertEqual(
            outreach_texts.sector_of_first_touch(
                "bestcarskz", outreach_texts.channel_dm("auto_from_ko_rea")),
            "")

    def test_case_of_the_username_matters(self):
        """`Auto_...` и `auto_...` дают разные тексты — сверяем как есть."""
        text = outreach_texts.channel_dm("auto_from_ko_rea")
        self.assertEqual(
            outreach_texts.sector_of_first_touch("Auto_from_ko_rea", text), "")

    def test_a_handwritten_letter_about_cars_is_not_enough(self):
        """Похожие слова мог написать человек — этого мало."""
        self.assertEqual(
            outreach_texts.sector_of_first_touch(
                "bestcarskz",
                "Здравствуйте! Возим авто из-за границы, интересно?"),
            "")

    def test_empty_input_is_not_a_sector(self):
        self.assertEqual(outreach_texts.sector_of_first_touch("", "текст"), "")
        self.assertEqual(outreach_texts.sector_of_first_touch("acc", ""), "")


class SectorOfThreadTests(unittest.TestCase):
    """То же самое, но по нашей же базе."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(814,'a','channel_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES('c1','channel','auto_from_ko_rea',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('topup','долив','send_channel_dm',?,?)", (now(), now()))
        self.store.commit()
        self.thread = {"id": "th1", "contact_id": "c1"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def touch(self, text: str, *, action: str = "send_channel_dm",
              state: str = "done", task_id: str = "t1",
              campaign: str = "topup") -> None:
        if not self.store.one("SELECT 1 FROM campaigns WHERE id=?", (campaign,)):
            self.store.execute(
                "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
                "VALUES(?,?,?,?,?)", (campaign, campaign, action, now(), now()))
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, dispatched_at, created_at, "
            "updated_at) VALUES(?,?,'c1',814,?,?,'immediate',?,?,?,?,?)",
            (task_id, campaign, action,
             json.dumps({"text": text}, ensure_ascii=False),
             now(), state, now(), now(), now()))
        self.store.commit()

    def test_our_first_touch_gives_the_sector(self):
        self.touch(outreach_texts.channel_dm("auto_from_ko_rea"))
        self.assertEqual(
            autoreply.outreach_sector_of_thread(self.store, self.thread),
            outreach_texts.SECTOR_ID)

    def test_nothing_sent_means_nothing_known(self):
        self.assertEqual(
            autoreply.outreach_sector_of_thread(self.store, self.thread), "")

    def test_a_letter_that_never_left_does_not_count(self):
        """Поставленная, но не отправленная задача — не разговор."""
        self.touch(outreach_texts.channel_dm("auto_from_ko_rea"),
                   state="planned")
        self.assertEqual(
            autoreply.outreach_sector_of_thread(self.store, self.thread), "")

    def test_a_personal_letter_says_nothing_about_the_sector(self):
        """Личка людей пишется под каждого отдельно, сферы там разные."""
        self.touch(outreach_texts.channel_dm("auto_from_ko_rea"),
                   action="send_private_dm")
        self.assertEqual(
            autoreply.outreach_sector_of_thread(self.store, self.thread), "")

    def test_a_handwritten_send_is_not_a_lane(self):
        self.touch("Здравствуйте, пишу по вашему объявлению.")
        self.assertEqual(
            autoreply.outreach_sector_of_thread(self.store, self.thread), "")

    def test_the_earliest_touch_decides(self):
        """Первым словом был наш шаблон, дальше могло быть что угодно."""
        self.touch(outreach_texts.channel_dm("auto_from_ko_rea"), task_id="t1")
        self.touch("Ответ руками", task_id="t2", campaign="manual_sends")
        self.assertEqual(
            autoreply.outreach_sector_of_thread(self.store, self.thread),
            outreach_texts.SECTOR_ID)

    def test_a_thread_without_a_contact_is_not_guessed(self):
        self.assertEqual(
            autoreply.outreach_sector_of_thread(
                self.store, {"id": "th2", "contact_id": None}), "")

    def test_broken_params_do_not_raise(self):
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, dispatched_at, created_at, "
            "updated_at) VALUES('t9','topup','c1',814,'send_channel_dm',"
            "'не json','immediate',?,'done',?,?,?)", (now(), now(), now(), now()))
        self.store.commit()
        self.assertEqual(
            autoreply.outreach_sector_of_thread(self.store, self.thread), "")


if __name__ == "__main__":
    unittest.main()
