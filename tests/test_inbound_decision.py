"""Движок решения по входящему.

Проверяем не качество ответов модели, а то, что перенос состоятелен: шов
``decide_inbound_reply`` работает без базы и без сети, локальные предохранители
срабатывают до обращения к модели, а её ответ доезжает до решения.
"""
import unittest

from bridge49.inbound_decision import decide_inbound_reply
from bridge49.presales_v2 import PresalesV2ExternalResult


def context(text: str, **overrides):
    base = {
        "provider_id": "bridge49",
        "inbound_id": "42",
        "account_id": "793",
        "role": "dm_sender",
        "peer_key": "@someone",
        "text": text,
    }
    base.update(overrides)
    return base


def answering_llm(reply_text: str, action: str = "reply", *, turn_items=None):
    """Подставная модель: отдаёт готовый ответ в контракте presales v2.

    ``turn_items`` обязательны: контракт требует, чтобы модель перечислила
    разобранные пункты и подтвердила каждый дословной цитатой — из вопроса
    (``user_evidence``) и из собственного ответа (``reply_evidence``). Без
    этого ответ не выпускается, и заглушка обязана вести себя так же, иначе
    тест проверял бы не тот путь.
    """

    def caller(payload, **kwargs):
        return PresalesV2ExternalResult(
            raw={
                "action": action,
                "intent": "pricing_question",
                "reply_text": reply_text,
                "confidence": 0.8,
                "risk_level": "low",
                "next_state": "FAQ automation",
                "handoff_reason": "",
                "handoff_kind": "none",
                "matched_direct_invite_sector_id": "",
                "knowledge_gap": "",
                "collected_fields_update": {},
                "coverage_complete": True,
                "turn_items": turn_items or [],
                "reason": "",
            },
            reason="",
        )

    return caller


class InboundDecisionTests(unittest.TestCase):
    def test_opt_out_is_caught_before_the_model(self):
        """Отказ разбирается локально — модель не зовём вовсе."""

        def explode(*args, **kwargs):
            raise AssertionError("модель не должна вызываться при отказе")

        decision = decide_inbound_reply(
            context("не пишите мне больше, отпишите"),
            llm_caller=explode,
        )

        self.assertEqual(decision["decision"], "opt_out")
        self.assertEqual(decision["reply_text"], "")

    def test_model_answer_becomes_a_decision(self):
        # Ответ про цену обязан заканчиваться одним вопросом к бесплатному
        # тесту — иначе движок его придержит (см. тест ниже).
        reply = "Тарифы GO, PLUS и PRO. Хотите посмотреть на бесплатном тесте?"
        decision = decide_inbound_reply(
            context("Сколько стоит и какие тарифы?"),
            llm_caller=answering_llm(
                reply,
                turn_items=[
                    {
                        "item_id": "1",
                        "topic": "pricing",
                        "user_item": "спрашивает про стоимость и тарифы",
                        "user_evidence": "Сколько стоит",
                        "status": "answered",
                        "answer_summary": "назвал публичные тарифы",
                        "reply_evidence": "Тарифы GO, PLUS и PRO",
                        "source_ids": ["v1:answer_cards/pricing.md"],
                    }
                ],
            ),
        )

        self.assertEqual(decision["decision"], "auto_reply")
        self.assertIn("Тарифы", decision["reply_text"])

    def test_answer_citing_a_missing_source_is_held_back(self):
        """Ссылаться можно только на реально существующий файл базы знаний."""
        decision = decide_inbound_reply(
            context("Сколько стоит?"),
            llm_caller=answering_llm(
                "Стоит 1000 рублей.",
                turn_items=[
                    {
                        "item_id": "1",
                        "topic": "pricing",
                        "user_item": "спрашивает цену",
                        "user_evidence": "Сколько стоит",
                        "status": "answered",
                        "answer_summary": "назвал цену",
                        "reply_evidence": "Стоит 1000 рублей",
                        "source_ids": ["v1:такого_файла_нет.md"],
                    }
                ],
            ),
        )

        self.assertEqual(decision["decision"], "hold_for_review")
        self.assertEqual(decision["reason"], "presales_v2_unknown_source_id")
        self.assertEqual(decision["reply_text"], "")

    def test_answer_without_evidence_is_held_back(self):
        """Ответ без разбора пунктов не выпускается — это защита контракта."""
        decision = decide_inbound_reply(
            context("Сколько стоит и какие тарифы?"),
            llm_caller=answering_llm("Тарифы GO, PLUS и PRO."),
        )

        self.assertEqual(decision["decision"], "hold_for_review")
        self.assertEqual(decision["reason"], "presales_v2_no_turn_items")
        self.assertEqual(decision["reply_text"], "")

    def test_technical_failure_does_not_invent_an_answer(self):
        """Если модель недоступна, решение не должно превращаться в отправку."""

        def broken(*args, **kwargs):
            raise RuntimeError("нет связи")

        decision = decide_inbound_reply(
            context("Расскажите про демо"),
            llm_caller=broken,
        )

        self.assertNotEqual(decision["decision"], "auto_reply")
        self.assertEqual(decision["reply_text"], "")
        self.assertTrue(decision["technical_failure"])

    def test_role_that_cannot_write_privately_is_rejected(self):
        with self.assertRaises(ValueError):
            decide_inbound_reply(
                context("привет", role="source_reader"),
                llm_caller=answering_llm("не должно дойти"),
            )


if __name__ == "__main__":
    unittest.main()


class DirectInviteSeamTests(unittest.TestCase):
    """Сфера, сопоставленная моделью, обязана доехать до решения.

    Ключ читает `direct_invite.sector_from_decision`; пустое значение даёт
    `BranchInactive`, согласие не записывается, ссылка не выпускается — и всё
    это молча, потому что разговор просто уходит менеджеру. Так автовыдача
    была мертва с самого переноса: 04.08 модель на ходе @cargo316k_1688
    вернула `logistics_ved_china`, а до записи согласия доехала пустая строка.
    """

    def caller(self, sector: str, handoff_kind: str = "free_test_access"):
        def make(payload, **kwargs):
            return PresalesV2ExternalResult(
                raw={
                    "action": "reply_and_handoff",
                    "intent": "demo_question",
                    "reply_text": "Принято, ссылка придёт отдельно.",
                    "confidence": 0.9,
                    "risk_level": "low",
                    "next_state": "free_test_access_pending",
                    "handoff_reason": "человек согласился на тест",
                    "handoff_kind": handoff_kind,
                    "matched_direct_invite_sector_id": sector,
                    "knowledge_gap": "",
                    "collected_fields_update": {"sector": "карго из Китая"},
                    "coverage_complete": True,
                    "turn_items": [{
                        "item_id": "1",
                        "topic": "free_test",
                        "user_item": "просит показать бесплатный тест",
                        "user_evidence": "покажите тест",
                        "status": "action_required",
                        "answer_summary": "заявка принята, ссылка придёт",
                        "reply_evidence": "ссылка придёт отдельно",
                        "source_ids": ["v1:free_test.md"],
                    }],
                    "reason": "",
                },
                reason="",
            )
        return make

    def decide(self, sector: str, **extra):
        return decide_inbound_reply(
            context("Мы возим карго из Китая, покажите тест",
                    direct_invite_sector_catalog=[{
                        "outreach_sector_id": "logistics_ved_china",
                        "sector_id": "logistics_ved_china",
                        "sector_name": "ВЭД, Китай, логистика",
                    }],
                    **extra),
            llm_caller=self.caller(sector),
        )

    def test_matched_sector_reaches_the_decision(self):
        decision = self.decide("logistics_ved_china")
        self.assertEqual(decision["handoff_kind"], "free_test_access")
        self.assertEqual(decision["matched_direct_invite_sector_id"],
                         "logistics_ved_china")

    def test_an_unmatched_sector_stays_empty_but_present(self):
        """Отсутствие ключа неотличимо от «сфера не сопоставлена», и разница
        видна только тем, что человек не получает обещанную ссылку."""
        from bridge49 import direct_invite
        decision = self.decide("")
        self.assertIn("matched_direct_invite_sector_id", decision)
        self.assertEqual(decision["matched_direct_invite_sector_id"], "")
        self.assertEqual(direct_invite.sector_from_decision(decision), "")

    def test_the_branch_would_actually_fire(self):
        """Сквозная проверка шва: то, что читает автовыдача."""
        from bridge49 import direct_invite
        decision = self.decide("logistics_ved_china")
        self.assertTrue(direct_invite.consent_from_decision(decision))
        self.assertEqual(direct_invite.sector_from_decision(decision),
                         "logistics_ved_china")
