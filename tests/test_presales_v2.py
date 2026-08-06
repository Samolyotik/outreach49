"""Контракт ответа движка в части сопоставления сферы.

Схема ответа устроена строго: множество полей проверяется на точное
совпадение в обе стороны, а в схеме обёртки каждое свойство стоит в
``required``. Необязательных полей там не бывает, поэтому «я не знаю»
выражается пустой строкой, а не отсутствием ключа.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import presales_v2  # noqa: E402
from bridge49.truth_pack import load_customer_truth_pack  # noqa: E402

PACK = load_customer_truth_pack()

#: Ответ, который проходит нормализацию целиком. Пункт хода подтверждён
#: цитатами с обеих сторон — без этого ответ не выпускается.
GOOD_ANSWER = {
    "action": "reply",
    "intent": "pricing_question",
    "reply_text": "Тарифы от 29 000 ₽. Сколько у вас чатов?",
    "confidence": 0.8,
    "risk_level": "low",
    "next_state": "FAQ automation",
    "handoff_reason": "",
    "handoff_kind": "none",
    "matched_direct_invite_sector_id": "",
    "client_sector_text": "",
    "canonical_sector_id": "",
    "sector_confidence": "",
    "knowledge_gap": "",
    "collected_fields_update": {},
    "coverage_complete": True,
    "turn_items": [{
        "item_id": "1",
        "topic": "pricing",
        "user_item": "цена",
        "user_evidence": "Сколько стоит",
        "status": "answered",
        "answer_summary": "назвал тарифы",
        "reply_evidence": "Тарифы от 29 000 ₽",
        "source_ids": ["v1:answer_cards/pricing.md"],
    }],
    "reason": "",
}

class SectorMatchingContractTests(unittest.TestCase):
    """Три поля сопоставления сферы.

    Главное здесь — мягкость. Непонятное значение обязано превращаться в «не
    знаю», а не рушить ответ: жёсткий отказ уходит в повторную попытку, а та
    дословно подсказывает модели ожидаемое значение, и со второго раза модель
    уверенно называет сферу, в которой только что сомневалась.
    """

    CATALOG = ("auto_import_dealers", "crm_1c")

    def normalize(self, raw=None, **extra):
        payload = dict(raw if raw is not None else GOOD_ANSWER)
        payload.update(extra)
        return presales_v2.normalize_presales_v2_result(
            payload, pack=PACK, required_topics=[],
            inbound_text="Сколько стоит?",
            known_canonical_sector_ids=self.CATALOG)

    def test_the_three_fields_are_required_like_every_other(self):
        """Схема ответа не знает необязательных свойств."""
        for field in ("client_sector_text", "canonical_sector_id",
                      "sector_confidence"):
            with self.subTest(field):
                raw = dict(GOOD_ANSWER)
                raw.pop(field)
                result = self.normalize(raw)
                self.assertTrue(result.technical_failure)
                self.assertIn("schema_missing_fields", result.reason)
                self.assertIn(field, result.reason)

    def test_a_clean_match_survives(self):
        result = self.normalize(
            client_sector_text="интегрируем Битрикс",
            canonical_sector_id="crm_1c",
            sector_confidence="exact")
        self.assertFalse(result.technical_failure)
        self.assertEqual(result.canonical_sector_id, "crm_1c")
        self.assertEqual(result.sector_confidence, "exact")
        self.assertEqual(result.client_sector_text, "интегрируем Битрикс")

    def test_an_invented_sector_becomes_i_do_not_know(self):
        result = self.normalize(canonical_sector_id="китобойный промысел",
                                sector_confidence="exact")
        self.assertFalse(result.technical_failure, "ответ терять нельзя")
        self.assertEqual(result.canonical_sector_id, "")
        self.assertEqual(result.sector_confidence, "")
        self.assertTrue(any("canonical_sector_unknown" in warning
                            for warning in result.validation_warnings))

    def test_an_unknown_confidence_becomes_i_do_not_know(self):
        result = self.normalize(canonical_sector_id="crm_1c",
                                sector_confidence="почти уверен")
        self.assertFalse(result.technical_failure)
        self.assertEqual(result.sector_confidence, "")
        self.assertTrue(any("sector_confidence_unknown" in warning
                            for warning in result.validation_warnings))

    def test_certainty_about_nothing_is_not_certainty(self):
        """`exact` без сферы открыл бы демо-маршрут в пустоту."""
        result = self.normalize(canonical_sector_id="", sector_confidence="exact")
        self.assertEqual(result.sector_confidence, "")
        self.assertIn("sector_confidence_without_sector",
                      result.validation_warnings)

    def test_every_allowed_confidence_passes(self):
        for value in ("exact", "likely", "ambiguous", "none", ""):
            with self.subTest(value=value or "пусто"):
                result = self.normalize(canonical_sector_id="crm_1c",
                                        sector_confidence=value)
                self.assertFalse(result.technical_failure)
                expected = value if value != "" else ""
                self.assertEqual(result.sector_confidence, expected)

    def test_a_technical_failure_carries_empty_fields(self):
        result = presales_v2.technical_failure_result("что-то пошло не так")
        self.assertEqual(result.canonical_sector_id, "")
        self.assertEqual(result.sector_confidence, "")
        self.assertEqual(result.client_sector_text, "")


class SectorPromptRulesTests(unittest.TestCase):
    """Правила словаря в промпте. Проверяется наличие, а не формулировка."""

    def prompt(self) -> str:
        import json
        built = presales_v2.build_presales_v2_prompt(
            inbound_text="Интегрируем Битрикс",
            context={}, pack=PACK, required_topics=[],
            reasoning_effort="medium")
        return json.dumps(built, ensure_ascii=False)

    def test_the_prompt_explains_all_three_fields(self):
        text = self.prompt()
        for field in ("client_sector_text", "canonical_sector_id",
                      "sector_confidence", "sector_matching_catalog"):
            with self.subTest(field):
                self.assertIn(field, text)

    def test_the_prompt_demands_confirmation_before_a_near_miss(self):
        """Ремонт не должен молча уехать в готовую группу недвижимости."""
        text = self.prompt()
        self.assertIn("free_test_group_ready=true", text)
        self.assertIn("подтверждения человека", text)

    def test_the_prompt_protects_the_reserved_memory_keys(self):
        text = self.prompt()
        self.assertIn("direct_invite_sector_id", text)
        self.assertIn("меняет ветку следующего хода", text)

    def test_the_prompt_still_demands_the_sector_in_the_person_own_words(self):
        """Запрет на нормализованные значения не должен читаться шире.

        Две инструкции стояли рядом и спорили: одна велела сохранять сферу в
        collected_fields_update.sector, другая объявляла этот ключ
        зарезервированным. Проигрыш первой стоит хода целиком: без
        подтверждённой сферы согласие на тест отбраковывается в
        technical_failure, и человек не получает ничего.
        """
        text = self.prompt()
        self.assertIn("collected_fields_update.sector записывать нужно всегда",
                      text)
