"""Форма ответа, показанная модели, и форма, которую требует валидатор.

Это два разных списка полей в одном файле, и разъехаться они могут молча.
Валидатор сверяет множества в обе стороны: лишнее поле — отказ, недостающее —
тоже. Отказ приходит как `technical_failure`, решение `hold_for_review`, а оно
молчит по замыслу. То есть цена расхождения — не исключение в журнале, а живой
человек, которому никто не ответил, и карточка менеджеру вместо разговора.

Ровно так и вышло в ночь на 06.08, на первом же входящем после переезда. В
`PRESALES_V2_REQUIRED_FIELDS` добавили три поля сопоставления сферы, в прозе
промпта их описали подробно, а в `output_schema` — ту самую форму, которую
модель копирует, — добавить забыли. Модель вернула четырнадцать полей из
семнадцати, ход развалился целиком, и человек, только что написавший
«Согласны», не получил ничего.

Проверка тут дешёвая и тупая нарочно: сравнить два множества. Дороже неё
только следующее такое же поле.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import presales_v2  # noqa: E402
from bridge49.truth_pack import load_customer_truth_pack  # noqa: E402


def schema_shown_to_model() -> dict:
    payload = presales_v2.build_presales_v2_prompt(
        inbound_text="Согласны",
        context={},
        pack=load_customer_truth_pack(),
        required_topics=[],
        reasoning_effort="medium",
    )
    return payload["output_schema"]


class SchemaParityTests(unittest.TestCase):
    def test_model_is_shown_every_field_the_validator_demands(self):
        shown = set(schema_shown_to_model())
        missing = presales_v2.PRESALES_V2_REQUIRED_FIELDS - shown
        self.assertEqual(
            missing, set(),
            "модель не увидит эти поля в форме ответа и не вернёт их, "
            "а валидатор развалит ход целиком и промолчит",
        )

    def test_the_validator_knows_every_field_the_model_is_asked_for(self):
        """Обратная сторона: лишнее поле в схеме — тоже отказ.

        `presales_v2_schema_unknown_fields` роняет ход так же насмерть, как и
        недостающее, только причина в журнале другая.
        """
        shown = set(schema_shown_to_model())
        extra = shown - presales_v2.PRESALES_V2_REQUIRED_FIELDS
        self.assertEqual(
            extra, set(),
            "модель вернёт поле, которого валидатор не ждёт, и ход упадёт",
        )

    def test_the_three_sector_fields_are_named_explicitly(self):
        """Именно на них расхождение и случилось — пусть падает адресно."""
        shown = set(schema_shown_to_model())
        for field in ("client_sector_text", "canonical_sector_id",
                      "sector_confidence"):
            self.assertIn(field, shown)


class RepairInstructionTests(unittest.TestCase):
    """Второй заход обязан знать, чего именно не хватило в первом.

    Поля остаются обязательными — это осознанное решение контракта. Значит
    единственный способ спасти ход, когда модель забыла ключ, — попросить
    заново и назвать пропажу поимённо. Общей фразы «верни полный контракт»
    ей не хватает: свой ответ она уже считает полным.
    """

    REASON = ("presales_v2_schema_missing_fields:"
              "canonical_sector_id,client_sector_text,sector_confidence")

    def test_the_missing_keys_are_named(self):
        text = presales_v2.presales_v2_repair_instruction(self.REASON)
        for field in ("canonical_sector_id", "client_sector_text",
                      "sector_confidence"):
            self.assertIn(field, text)

    def test_the_empty_string_is_offered_as_a_way_out(self):
        """Иначе модель снова пропустит ключ, которому нечего сказать."""
        text = presales_v2.presales_v2_repair_instruction(self.REASON)
        self.assertIn("пустую строку", text)

    def test_other_reasons_keep_their_own_wording(self):
        text = presales_v2.presales_v2_repair_instruction(
            "presales_v2_free_test_requires_confirmed_sector")
        self.assertIn("Сфера человека ещё не подтверждена", text)
        self.assertNotIn("не было ключей", text)

    def test_an_unknown_reason_still_gets_the_general_part(self):
        text = presales_v2.presales_v2_repair_instruction("что-то своё")
        self.assertIn("hard-валидацию", text)


if __name__ == "__main__":
    unittest.main()
