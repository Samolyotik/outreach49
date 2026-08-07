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


class PublicLettersTests(unittest.TestCase):
    """Что мы сказали МЕСТУ — то же самое, но со стороны аккаунта.

    Проверка по контакту (выше) находит письмо только там, где письмо и ответ
    лежат на одном контакте: человек ответил в самой личке канала. Владелец
    канала, написавший со своего личного аккаунта, — другой контакт, и его
    разговор для движка начинался ниоткуда.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(814,'a','channel_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(815,'b','channel_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('topup','долив','send_channel_dm',?,?)", (now(), now()))
        self.store.commit()
        self.serial = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def letter_to(self, channel: str, text: str | None = None, *,
                  action: str = "send_channel_dm", state: str = "done",
                  account_id: int = 814) -> None:
        self.serial += 1
        contact = f"c{self.serial}"
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES(?,'channel',?,?,?)", (contact, channel, now(), now()))
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, dispatched_at, created_at, "
            "updated_at) VALUES(?,'topup',?,?,?,?,'immediate',?,?,?,?,?)",
            (f"t{self.serial}", contact, account_id, action,
             json.dumps({"text": outreach_texts.channel_dm(channel)
                                 if text is None else text},
                        ensure_ascii=False),
             now(), state, f"2026-08-07T1{self.serial}:00:00+00:00",
             now(), now()))
        self.store.commit()

    def letters(self, role: str = "channel_sender", **kwargs):
        return autoreply.our_public_letters(self.store, 814, role, **kwargs)

    def test_our_own_letters_come_back(self):
        self.letter_to("auto_from_ko_rea")
        letters = self.letters()
        self.assertEqual(len(letters), 1)
        self.assertEqual(letters[0]["kind"], "send_channel_dm")
        self.assertIn("Telegram", letters[0]["text"])

    def test_the_newest_letters_come_first(self):
        self.letter_to("auto_from_ko_rea")
        self.letter_to("bestcarskz")
        self.assertEqual(
            [item["text"] for item in self.letters()],
            [outreach_texts.channel_dm("bestcarskz"),
             outreach_texts.channel_dm("auto_from_ko_rea")])

    def test_only_a_few_are_shown(self):
        """Письма полосы почти одинаковы: десяток отличается от тройки шумом."""
        for index in range(6):
            self.letter_to(f"dealer{index}")
        self.assertEqual(len(self.letters()), autoreply.PUBLIC_LETTERS_SHOWN)

    def test_a_handwritten_send_is_not_our_letter(self):
        """Иначе движку уехал бы чужой текст как наш собственный."""
        self.letter_to("auto_from_ko_rea", "Здравствуйте, пишу по объявлению.")
        self.assertEqual(self.letters(), [])

    def test_a_letter_that_never_left_does_not_count(self):
        self.letter_to("auto_from_ko_rea", state="planned")
        self.assertEqual(self.letters(), [])

    def test_the_lane_of_the_role_decides(self):
        """Письмо каналу, засчитанное чат-аккаунту, увело бы в чужие правила."""
        self.letter_to("auto_from_ko_rea")
        self.assertEqual(self.letters(role="chat_sender"), [])

    def test_a_role_without_a_lane_gets_nothing(self):
        self.letter_to("auto_from_ko_rea")
        self.assertEqual(self.letters(role="dm_sender"), [])

    def test_another_accounts_letters_are_not_ours(self):
        self.letter_to("auto_from_ko_rea", account_id=815)
        self.assertEqual(self.letters(), [])

    def test_broken_params_do_not_raise(self):
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES('cx','channel','auto_from_ko_rea',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, dispatched_at, created_at, "
            "updated_at) VALUES('tx','topup','cx',814,'send_channel_dm',"
            "'не json','immediate',?,'done',?,?,?)",
            (now(), now(), now(), now()))
        self.store.commit()
        self.assertEqual(self.letters(), [])


class PublicEntryContextTests(unittest.TestCase):
    """Разговор, начатый нашим письмом МЕСТУ, приезжает движку с началом.

    07.08 @vodopad_anhel написал «Здравствуйте заинтересовали» аккаунту, за
    четверть часа до того написавшему его каналу, и получил в ответ «Подскажите,
    что именно вас заинтересовало?». Иначе и быть не могло: движок видел голую
    строку без единого признака, что разговор начали мы.
    """

    BRANCH = {
        "schema_version": 1, "enabled": True,
        "active_sector_ids": ["auto_import_dealers"],
        "validity_days": 7, "max_attempts": 5,
        "sector_profiles": {"auto_import_dealers": {
            "outreach_sector_id": "auto_import_dealers",
            "sector_id": "cars_abroad",
            "sector_name": "Авто из-за границы",
            "test_group_profile_id": "cars_abroad_test_group"}},
    }

    def setUp(self):
        from bridge49 import direct_invite
        from bridge49 import presales_context
        self.modes = presales_context
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        path = tmp / "branch.json"
        path.write_text(json.dumps(self.BRANCH), encoding="utf-8")
        self.branch = direct_invite.BranchConfig.from_path(path)
        self.store = Store(tmp / "b.sqlite")
        self.store.execute(
            "INSERT INTO accounts(id, label, role, enabled, synced_at) "
            "VALUES(814,'acc-814','channel_sender',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('topup','долив','send_channel_dm',?,?)", (now(), now()))
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('autoreplies','автоответы','reply_private_dm',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, created_at, "
            "updated_at) VALUES('owner','user','vodopad_anhel','inbound',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "state, campaign_id, created_at, updated_at) "
            "VALUES('th1',814,'@vodopad_anhel','owner','private_dm','open',"
            "'autoreplies',?,?)", (now(), now()))
        self.store.commit()
        self.thread = dict(self.store.one("SELECT * FROM threads WHERE id='th1'"))
        self.inbound = {
            "id": 83967, "account_id": 814, "surface": "private_dm",
            "peer_key": "@vodopad_anhel", "text": "Здравствуйте заинтересовали",
            "sent_at": now(), "created_at": now(),
        }

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def wrote_to_a_channel(self, channel: str = "auto_from_ko_rea",
                           action: str = "send_channel_dm") -> None:
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, created_at, updated_at) "
            "VALUES(?,'channel',?,?,?)", (channel, channel, now(), now()))
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, dispatched_at, created_at, "
            "updated_at) VALUES(?,'topup',?,814,?,?,'immediate',?,'done',?,?,?)",
            (f"task-{channel}", channel, action,
             json.dumps({"text": outreach_texts.channel_dm(channel)},
                        ensure_ascii=False),
             now(), now(), now(), now()))
        self.store.commit()

    def context(self) -> dict:
        return autoreply.build_context(
            self.store, self.inbound, self.thread, branch_config=self.branch)

    def test_the_engine_sees_what_we_wrote_and_to_what_lane(self):
        self.wrote_to_a_channel()
        context = self.context()
        self.assertEqual(context["entry_mode"],
                         self.modes.CHANNEL_SENDER_PRIVATE_ENTRY_MODE)
        self.assertEqual(
            [item["text"] for item in context["recent_public_chat_outreach"]],
            [outreach_texts.channel_dm("auto_from_ko_rea")])

    def test_our_letter_does_not_decide_the_sector_for_him(self):
        """Сфера письма — наша, а не его: адресат не обязан быть из неё.

        03.08 @arikhina ответила на письмо каналу «зачем нам сообщения людей,
        которым нужны запуски онлайн-школ»: письмо про авто ушло не в ту дверь.
        Открытая ветка звала бы её в чужую тестовую группу, а такой доступ
        отзывается только руками.
        """
        self.wrote_to_a_channel()
        self.assertEqual(self.context()["free_test_access_branch"],
                         {"branch": "manager"})

    def test_without_our_letter_nothing_changes(self):
        context = self.context()
        self.assertNotIn("entry_mode", context)
        self.assertNotIn("recent_public_chat_outreach", context)
        self.assertEqual(context["free_test_access_branch"],
                         {"branch": "manager"})

    def test_a_dialogue_we_started_ourselves_keeps_the_usual_path(self):
        """Там первое касание лежит на самом контакте и находится обычной проверкой."""
        self.wrote_to_a_channel()
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, dispatched_at, created_at, "
            "updated_at) VALUES('personal','topup','owner',814,'send_private_dm',"
            "'{}','immediate',?,'done',?,?,?)", (now(), now(), now(), now()))
        self.store.commit()
        context = self.context()
        self.assertNotIn("entry_mode", context)
        self.assertNotIn("recent_public_chat_outreach", context)

    def test_our_own_reply_does_not_turn_him_into_a_stranger_lane(self):
        """Со второго хода полоса и ветка обязаны остаться прежними.

        Разговор начался нашим письмом каналу, и наш же ответ этого не меняет.
        По прежней проверке «мы тут говорили» со второго хода терялись и
        правила полосы, и автоветка теста — ровно тогда, когда разговор пошёл.
        """
        self.wrote_to_a_channel()
        self.store.execute(
            "UPDATE threads SET last_outbound_at = ? WHERE id = 'th1'", (now(),))
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, dispatched_at, created_at, "
            "updated_at) VALUES('answer','autoreplies','owner',814,"
            "'reply_private_dm','{}','immediate',?,'done',?,?,?)",
            (now(), now(), now(), now()))
        self.store.commit()
        self.thread = dict(self.store.one("SELECT * FROM threads WHERE id='th1'"))
        context = self.context()
        self.assertEqual(context["entry_mode"],
                         self.modes.CHANNEL_SENDER_PRIVATE_ENTRY_MODE)
        self.assertEqual(len(context["recent_public_chat_outreach"]), 1)

    def test_an_inherited_dialogue_is_not_opened_by_this(self):
        """Собеседники прежних владельцев аккаунтов остаются посторонними."""
        self.wrote_to_a_channel()
        self.store.execute(
            "INSERT INTO history(id, thread_id, direction, text, sent_at, "
            "origin, created_at) VALUES('h1','th1','inbound','старое',?,"
            "'import',?)", (now(), now()))
        self.store.commit()
        self.assertNotIn("entry_mode", self.context())

    def test_a_chat_account_gets_the_chat_rules(self):
        """Полосы отвечают по-разному: в чате мы разворачиваем крючок покупателя."""
        self.store.execute(
            "UPDATE accounts SET role = 'chat_sender' WHERE id = 814")
        self.store.commit()
        self.wrote_to_a_channel(channel="somechat",
                                action="send_public_chat_message")
        self.assertEqual(self.context()["entry_mode"],
                         self.modes.CHAT_SENDER_PRIVATE_ENTRY_MODE)


if __name__ == "__main__":
    unittest.main()
