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
        # Комбинаций 150; на 400 сидах должна набраться заметная их часть.
        self.assertGreater(len(seen), 100, "разнообразия меньше ожидаемого")


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
        self.assertGreater(len(seen), 80)


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
