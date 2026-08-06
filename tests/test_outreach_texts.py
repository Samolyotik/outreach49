"""Тексты первых касаний в чаты и в личку каналов.

Проверяется не красота формулировок, а границы: что текст не превращается в
рекламу, не повторяется у соседей и собирается одинаково при повторном
прогоне. 04.08 семь писем ушли байт в байт, потому что текст был один на всех
— здесь это ловится счётом, а не глазами.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import outreach_texts as texts  # noqa: E402


class ChatMessageTests(unittest.TestCase):
    def test_it_asks_rather_than_sells(self):
        """В чужом публичном чате реклама — повод для бана, вопрос — нет."""
        for seed in ("a", "b", "c", "автосалон"):
            with self.subTest(seed):
                body = texts.chat_message(seed)
                self.assertEqual(texts.validate(body, kind="chat"), [])
                self.assertIn("?", body)

    def test_every_combination_is_clean(self):
        seen = set()
        for index in range(400):
            body = texts.chat_message(f"чат{index}")
            self.assertEqual(texts.validate(body, kind="chat"), [], body)
            seen.add(body)
        self.assertGreater(len(seen), 300, "разнообразия меньше ожидаемого")


class ChannelDmTests(unittest.TestCase):
    def test_it_greets_and_offers(self):
        for seed in ("x", "y", "autochat"):
            with self.subTest(seed):
                body = texts.channel_dm(seed)
                self.assertEqual(texts.validate(body, kind="channel"), [])
                self.assertTrue(body.startswith("Здравствуйте"))

    def test_every_combination_is_clean(self):
        seen = set()
        for index in range(400):
            body = texts.channel_dm(f"канал{index}")
            self.assertEqual(texts.validate(body, kind="channel"), [], body)
            seen.add(body)
        self.assertGreater(len(seen), 300)


class StabilityTests(unittest.TestCase):
    def test_the_same_target_gets_the_same_text(self):
        """Повторный прогон плана не должен переписывать письма заново."""
        for seed in ("autochat", "prigon24"):
            with self.subTest(seed):
                self.assertEqual(texts.chat_message(seed),
                                 texts.chat_message(seed))
                self.assertEqual(texts.channel_dm(seed),
                                 texts.channel_dm(seed))

    def test_neighbours_do_not_collide(self):
        """Соседи по очереди — разные аккаунты в один час. Одинаковый текст у
        них читается как рассылка вернее любого другого признака."""
        batch = [texts.chat_message(f"acc{i}") for i in range(20)]
        self.assertGreater(len(set(batch)), 17)


class VarietyTests(unittest.TestCase):
    """Сколько вообще бывает текстов — и все ли они годные.

    Число комбинаций это не украшение отчёта, а прямая защита: на плане 07.08
    сто восемьдесят восемь писем в каналы дали девяносто четыре разных текста,
    один и тот же ушёл семь раз, а трижды один аккаунт отправил одинаковое
    дважды за день. Именно так рассылка и опознаётся снаружи.
    """

    ДНЕВНАЯ_НОРМА = 300

    def test_there_are_thousands_of_them(self):
        for kind in ("chat", "channel"):
            with self.subTest(kind):
                self.assertGreaterEqual(texts.combinations(kind), 2000)

    def test_every_single_combination_is_clean(self):
        """Перебор целиком, а не выборка.

        Куски пишет человек, и ошибиться можно в одном из полусотни. Дешевле
        перебрать все пять тысяч здесь, чем поймать кривую фразу в чужом чате.
        """
        for kind, build in (("chat", texts.chat_message),
                            ("channel", texts.channel_dm)):
            собранные = {build(f"{kind}-{index}")
                         for index in range(texts.combinations(kind) * 8)}
            for body in собранные:
                self.assertEqual(texts.validate(body, kind=kind), [], body)

    def test_a_daily_batch_almost_never_repeats(self):
        """Дневная норма не должна упираться в потолок комбинаций."""
        for kind, build in (("chat", texts.chat_message),
                            ("channel", texts.channel_dm)):
            with self.subTest(kind):
                партия = [build(f"цель{index}")
                          for index in range(self.ДНЕВНАЯ_НОРМА)]
                повторов = len(партия) - len(set(партия))
                self.assertLess(повторов, len(партия) * 0.12, kind)


class LegacyRecognitionTests(unittest.TestCase):
    """Письма прошлого поколения обязаны узнаваться и после смены наборов.

    На точном совпадении текста держится определение сферы без вопросов
    человеку. Перестанет узнаваться — тот, кому уже написали, при ответе
    получит демо-бота вместо готового бесплатного теста, и заметить это по
    логам почти невозможно.

    Пары ниже — настоящие, из отправленных 04–06.08.
    """

    ОТПРАВЛЕННЫЕ = (
        ("armavir_auto23",
         "Здравствуйте. Мы отслеживаем в Telegram запросы по подбору и "
         "привозу авто. По вашему направлению они появляются регулярно. "
         "Можем бесплатно показать, как это выглядит."),
        ("askjcars",
         "Здравствуйте. Отслеживаем в Telegram, где люди спрашивают про "
         "подбор и пригон авто. По вашему направлению они появляются "
         "регулярно. Если интересно, покажем бесплатно."),
        ("auto_eu1",
         "Посоветуйте, кто пригоняет авто под заказ?\n\nВажно понимать "
         "состояние машины до оплаты и полную сумму до получения. "
         "Кого посоветуете?"),
        ("auto_bazar_warszawa",
         "Ищу, кто занимается подбором и доставкой авто из-за границы.\n\n"
         "Хочу понять варианты по подбору, проверке перед покупкой и итоговой "
         "стоимости. Кого посоветуете?"),
    )

    def test_already_sent_letters_still_resolve_to_the_sector(self):
        for username, body in self.ОТПРАВЛЕННЫЕ:
            with self.subTest(username):
                self.assertEqual(
                    texts.sector_of_first_touch(username, body),
                    texts.SECTOR_ID)

    def test_new_letters_resolve_too(self):
        for seed in ("carland_auction", "k1motors"):
            with self.subTest(seed):
                for build in (texts.channel_dm, texts.chat_message):
                    self.assertEqual(
                        texts.sector_of_first_touch(seed, build(seed)),
                        texts.SECTOR_ID)

    def test_someone_elses_text_is_not_claimed(self):
        self.assertEqual(
            texts.sector_of_first_touch(
                "armavir_auto23", "Привет! Продаю машину, интересует?"), "")


class ToneTests(unittest.TestCase):
    """Расширение не должно было менять ни смысла, ни окраски.

    Проверяется не стиль, а то, что в наборы не заехало: оценок чужой работы,
    выдуманных подробностей о себе и тем, на которые нам нечего ответить.
    """

    #: «лучше» сюда не входит: в «к кому лучше обратиться?» это не оценка
    #: чужой работы, а обычный оборот, и он стоит в наборах с самого начала.
    ЧУЖЕРОДНОЕ = (
        "действительно", "честн", "гарант", "ответственност",
        "опыт", "бюджет обсужда", "впервые", "не принципиально",
        "обещ", "дешев", "выгодн", "надёжн", "проверенн подрядч",
    )

    def test_no_stray_colouring_crept_into_the_phrases(self):
        наборы = (texts._CHAT_OPENINGS + texts._CHAT_NEEDS
                  + texts._CHAT_CLOSERS + texts._DM_GREETING
                  + texts._DM_WHAT + texts._DM_WHY + texts._DM_OFFER)
        for фраза in наборы:
            low = фраза.lower()
            for слово in self.ЧУЖЕРОДНОЕ:
                self.assertNotIn(слово, low, f"{слово!r} в {фраза!r}")

    def test_the_first_generation_phrases_are_still_there_untouched(self):
        """Старые формулировки не переписаны, а дополнены."""
        self.assertEqual(texts._CHAT_OPENINGS[:6], texts._V1_CHAT_OPENINGS)
        self.assertEqual(texts._CHAT_NEEDS[:5], texts._V1_CHAT_NEEDS)
        self.assertEqual(texts._CHAT_CLOSERS[:5], texts._V1_CHAT_CLOSERS)
        self.assertEqual(texts._DM_OFFER[:5], texts._V1_DM_OFFER)


class ValidationTests(unittest.TestCase):
    def test_brand_name_is_refused(self):
        problems = texts.validate("Здравствуйте. ТГ РАДАР покажет спрос.",
                                  kind="channel")
        self.assertIn("названо имя сервиса", problems)

    def test_link_is_refused(self):
        problems = texts.validate("Кто возит авто? Пишите t.me/somebody",
                                  kind="chat")
        self.assertIn("есть ссылка или упоминание", problems)

    def test_chat_without_a_question_is_refused(self):
        problems = texts.validate("Ищу перевозчика.", kind="chat")
        self.assertTrue(any("вопрос" in item for item in problems))

    def test_channel_letter_must_greet(self):
        problems = texts.validate("Мы собираем запросы. Покажем бесплатно.",
                                  kind="channel")
        self.assertIn("письмо владельцу без приветствия", problems)

    def test_empty_is_refused(self):
        self.assertEqual(texts.validate("   ", kind="chat"), ["пустой текст"])


if __name__ == "__main__":
    unittest.main()
