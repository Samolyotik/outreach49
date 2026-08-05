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


if __name__ == "__main__":
    unittest.main()
