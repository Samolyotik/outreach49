"""Проверки первого касания.

Генерацию проверять нечем: текст пишет модель. А вот ворота на выходе — наши,
и они единственное, что стоит между «модель написала» и «человек прочитал».
Каждое правило здесь появилось не из общих соображений: у прежнего контура они
выведены из живой переписки, и нарушение любого видно адресату сразу.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import first_touch  # noqa: E402

GOOD = (
    "Здравствуйте! Увидели ваше сообщение про поиск клиентов на кузовной "
    "ремонт. У нас есть сервис, который находит в мессенджерах и социальных "
    "сетях сообщения людей, которым нужен кузовной ремонт, и собирает их в "
    "одном месте. Если хотите, можем бесплатно показать, как он работает. "
    "Интересно?"
)


class GoodTextTests(unittest.TestCase):
    def test_reference_text_passes(self):
        self.assertEqual(first_touch.validate_text(GOOD), [])

    def test_shape_matches_the_contract(self):
        self.assertTrue(GOOD.startswith("Здравствуйте!"))
        self.assertTrue(GOOD.rstrip().endswith("?"))
        self.assertEqual(GOOD.count("?"), 1)
        self.assertLessEqual(len(GOOD), first_touch.MAX_LENGTH)


class MirrorTests(unittest.TestCase):
    """Промпт обязан предупреждать о зеркале.

    Проверить сам текст на зеркало нельзя: оборот «которым нужны такие
    события» законен, когда человек эти события и продаёт. Отличает их только
    смысл исходного сообщения, а его на выходе уже нет. Значит, единственное
    место, где дефект ловится, — инструкция модели, и она не должна тихо
    вернуться к прежней формулировке.
    """

    def setUp(self):
        self.prompt = first_touch.build_prompt([{
            "row_id": "1",
            "primary_signal": {"category_code": "HOT",
                               "message_text": "Ищу авитолога",
                               "source_title": "чат", "published_at": ""},
            "signals": [],
        }])

    def test_prompt_asks_for_what_the_addressee_sells(self):
        self.assertIn("продаёт САМ АДРЕСАТ", self.prompt)

    def test_prompt_names_the_back_reference_trap(self):
        self.assertIn("которым нужны такие услуги", self.prompt)
        self.assertIn("указывает назад", self.prompt)

    def test_prompt_gives_a_fallback_when_the_niche_is_unknown(self):
        self.assertIn("которым нужны ваши товары и услуги", self.prompt)


class RejectionTests(unittest.TestCase):
    """Каждое правило — с объяснением, что увидит адресат при нарушении."""

    def check(self, text: str, expect: str):
        problems = first_touch.validate_text(text)
        self.assertTrue(
            any(expect in p for p in problems),
            f"ожидали «{expect}», получили {problems}",
        )

    def test_brand_name_is_refused(self):
        # Человек о нас не спрашивал: имя бренда в первом письме читается как
        # реклама, а не как разговор.
        self.check(GOOD.replace("сервис", "сервис ТГ РАДАР"), "имя бренда")

    def test_first_person_singular_is_refused(self):
        # Пишет команда. «Могу показать» обещает личного собеседника, которого
        # за аккаунтом нет.
        self.check(GOOD.replace("можем бесплатно показать", "могу бесплатно показать"),
                   "первое лицо")

    def test_link_or_username_is_refused(self):
        self.check(GOOD[:-10] + " пишите @manager?", "ссылка или username")

    def test_only_telegram_scope_is_refused(self):
        # Сказать «только Telegram» — неправда про охват, и неправда в первом
        # же письме дороже любой конверсии.
        narrow = GOOD.replace("в мессенджерах и социальных сетях", "в Telegram-чатах")
        problems = first_touch.validate_text(narrow)
        self.assertTrue(any("мессенджеры" in p for p in problems), problems)
        self.assertTrue(any("социальные сети" in p for p in problems), problems)

    def test_open_sources_wording_is_refused(self):
        # «Открытые источники» превращают письмо в отчёт о слежке.
        self.check(GOOD.replace("в мессенджерах", "в открытых мессенджерах"),
                   "«открытые»")

    def test_second_question_mark_is_refused(self):
        self.check(GOOD.replace("Увидели ваше сообщение", "Верно ли, что вы ищете?"),
                   "вопросительных знаков")

    def test_text_must_end_with_a_question(self):
        self.check(GOOD.replace("Интересно?", "Интересно? Ждём ответа."),
                   "не заканчивается вопросом")

    def test_long_dash_and_emoji_are_refused(self):
        self.check(GOOD.replace("сервис,", "сервис —"), "длинное тире")
        self.check(GOOD.replace("Интересно?", "Интересно 🙂?"), "эмодзи")

    def test_missing_greeting_is_refused(self):
        self.check(GOOD.replace("Здравствуйте! ", ""), "приветствия")

    def test_too_long_is_refused(self):
        self.check(GOOD.replace("Интересно?", "и " * 200 + "Интересно?"),
                   "длиннее")

    def test_empty_is_refused(self):
        self.assertEqual(first_touch.validate_text("  "), ["пустой текст"])


class AcceptanceTests(unittest.TestCase):
    """Мало пройти текстовые ворота: модель сама сообщает свою уверенность."""

    def draft(self, **over):
        base = {
            "row_id": "r1", "ok": True, "final_text": GOOD,
            "need_reference": "ищет клиентов на кузовной ремонт",
            "confidence": 0.9, "risk_level": "low", "reason": "по сообщению",
        }
        base.update(over)
        return base

    def test_good_draft_is_accepted(self):
        ok, problems = first_touch.accept(self.draft())
        self.assertTrue(ok, problems)

    def test_low_confidence_is_held(self):
        ok, problems = first_touch.accept(self.draft(confidence=0.7))
        self.assertFalse(ok)
        self.assertTrue(any("уверенность" in p for p in problems), problems)

    def test_non_low_risk_is_held(self):
        ok, problems = first_touch.accept(self.draft(risk_level="medium"))
        self.assertFalse(ok)

    def test_model_saying_not_ok_is_held(self):
        ok, _ = first_touch.accept(self.draft(ok=False))
        self.assertFalse(ok)

    def test_missing_need_reference_is_held(self):
        ok, problems = first_touch.accept(self.draft(need_reference=""))
        self.assertFalse(ok)
        self.assertTrue(any("потребности" in p for p in problems), problems)

    def test_bad_text_is_held_even_when_model_is_confident(self):
        ok, _ = first_touch.accept(
            self.draft(final_text=GOOD.replace("сервис", "сервис ТГ РАДАР")))
        self.assertFalse(ok)


class PromptTests(unittest.TestCase):
    def test_prompt_carries_the_person_message(self):
        contacts = [{
            "row_id": "r1",
            "primary_signal": {
                "category_code": "HOT",
                "message_text": "Ищу кто поможет с заявками на окна",
                "source_title": "Чат предпринимателей",
                "published_at": "2026-08-01T10:00:00+00:00",
            },
            "signals": [{}],
        }]
        prompt = first_touch.build_prompt(contacts)
        self.assertIn("Ищу кто поможет с заявками на окна", prompt)
        self.assertIn("Чат предпринимателей", prompt)
        self.assertIn("OUTPUT_SCHEMA", prompt)
        # Ключевые запреты обязаны доехать до модели, а не только до валидатора.
        self.assertIn("не продаем трафик", prompt)
        self.assertIn("во множественном числе", prompt)

    def test_payload_parses_with_and_without_code_fence(self):
        raw = '{"drafts":[{"row_id":"r1","ok":true,"final_text":"t",' \
              '"need_reference":"n","confidence":0.9,"risk_level":"low",' \
              '"reason":"r"}]}'
        for wrapped in (raw, "```json\n" + raw + "\n```"):
            with self.subTest(wrapped=wrapped[:12]):
                drafts = first_touch.parse_payload(wrapped)
                self.assertEqual(list(drafts), ["r1"])


if __name__ == "__main__":
    unittest.main()
