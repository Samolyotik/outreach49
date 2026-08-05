"""Контракт квалификации кандидатов на личное сообщение.

Проверяется не качество решений модели — его проверяет живой прогон, — а то,
что мягкая рамка v3 не сползёт обратно к строгой. Сползти она может тихо: не
через промпт, а через ответ, где отказ обоснован свободным намерением вроде
«просто не подходит». Поэтому отказ обязан назвать причину из закрытого
списка, а проходной вердикт не имеет права на причину отказа.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import contact_fit  # noqa: E402


def review(**overrides) -> dict:
    base = {
        "row_id": "1",
        "decision": "qualified",
        "fit_score": 80,
        "confidence": "medium",
        "intent": "ad_or_traffic_buyer",
        "need_summary": "покупает рекламу для своего магазина",
        "fit_reason": "есть бюджет на привлечение",
        "outreach_angle": "показать спрос по нише",
        "risks": [],
    }
    base.update(overrides)
    return base


class ClosedRejectListTests(unittest.TestCase):
    def test_reject_must_name_a_reason(self):
        """Отказ «просто так» — это возврат к v2, где резали всё подряд."""
        payload = {"reviews": [review(decision="reject", fit_score=10,
                                      intent="business_need_other")]}
        with self.assertRaises(contact_fit.FitError):
            contact_fit.validate(payload, ["1"])

    def test_reject_with_a_listed_reason_passes(self):
        payload = {"reviews": [review(decision="reject", fit_score=5,
                                      intent="spam")]}
        out = contact_fit.validate(payload, ["1"])
        self.assertEqual(out[0]["intent"], "spam")

    def test_pass_cannot_borrow_a_reject_reason(self):
        payload = {"reviews": [review(decision="maybe", fit_score=50,
                                      intent="competitor")]}
        with self.assertRaises(contact_fit.FitError):
            contact_fit.validate(payload, ["1"])

    def test_reasons_and_fits_do_not_overlap(self):
        self.assertFalse(set(contact_fit.REJECT_INTENTS)
                         & set(contact_fit.FIT_INTENTS))
        self.assertEqual(set(contact_fit.VALID_INTENTS),
                         set(contact_fit.REJECT_INTENTS)
                         | set(contact_fit.FIT_INTENTS))


class ScaleTests(unittest.TestCase):
    def test_decision_and_score_must_agree(self):
        for decision, score in (("qualified", 50), ("maybe", 90),
                                ("reject", 60)):
            with self.subTest(decision=decision, score=score):
                intent = ("spam" if decision == "reject"
                          else "lead_generation_need")
                payload = {"reviews": [review(decision=decision,
                                              fit_score=score, intent=intent)]}
                with self.assertRaises(contact_fit.FitError):
                    contact_fit.validate(payload, ["1"])

    def test_missing_rows_are_refused(self):
        payload = {"reviews": [review()]}
        with self.assertRaises(contact_fit.FitError):
            contact_fit.validate(payload, ["1", "2"])


class BoilerplateTests(unittest.TestCase):
    def test_repeated_blocks_are_counted_across_the_pool(self):
        """Одна строка шаблон не выдаёт — выдаёт его вся выборка сразу."""
        block = ("Анализируете воронки продаж и ключевые идеи, напишите о "
                 "вашем опыте, рассмотрим кандидатов уровня middle")
        rows = [
            {"btm_id": 1, "сообщение": f"Требуется таргетолог. {block}"},
            {"btm_id": 2, "сообщение": f"Нужен копирайтер. {block}"},
            {"btm_id": 3, "сообщение": f"Ищу дизайнера. {block}"},
            {"btm_id": 4, "сообщение": "Кто может настроить рекламу на ВБ?"},
        ]
        repeats = contact_fit.template_repeats(rows)
        self.assertEqual(repeats["1"], 3)
        self.assertEqual(repeats["4"], 1)

    def test_homoglyphs_are_visible(self):
        spam = "Бaзы даʜных и лиды кpиптa, Хoлoдкa Peги Дeпы"
        clean = "Базы данных и лиды крипта, холодка реги депы"
        self.assertGreater(contact_fit.homoglyph_words(spam), 0)
        self.assertEqual(contact_fit.homoglyph_words(clean), 0)

    def test_the_model_sees_the_counts_but_not_the_author(self):
        rows = [{"btm_id": 7, "сообщение": "Кто настроит рекламу?",
                 "источник": "WB чат", "категория": "HOT",
                 "имя": "Дмитрий", "username": "doma_sme"}]
        shown = contact_fit.prompt_rows(rows, {"7": 4})
        self.assertEqual(shown[0]["template_repeats"], 4)
        rendered = str(shown)
        self.assertNotIn("Дмитрий", rendered)
        self.assertNotIn("doma_sme", rendered)


class PromptTests(unittest.TestCase):
    def test_the_prompt_states_the_wide_frame(self):
        body = contact_fit.build_prompt(
            [{"btm_id": 1, "сообщение": "Кто настроит рекламу?"}], "канон")
        # Рамка ICP и закрытый список — единственное, что отличает v3 от v2;
        # если они выпали из промпта, версия врёт о себе.
        self.assertIn("кому нужен", body)
        self.assertIn("traffic_trade", body)
        self.assertIn("собственный продукт", body)


if __name__ == "__main__":
    unittest.main()
