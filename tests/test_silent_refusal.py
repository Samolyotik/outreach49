"""Прямой отказ остаётся без ответа.

06.08 человек написал «На данный момент я не планирую использовать сторонние
сервисы для поиска клиентов, спасибо за ваше предложение» — и получил в ответ
«Понял, спасибо за ответ.». Это лишний ход: после «нет» сказать уже нечего, а
сообщение уходит.

Причина была не в модели, а в контракте: `soft_negative` обязан был приходить
с `reply_and_pause`, то есть с текстом. Молчание там было технически
невозможно. Здесь проверяется обратное требование и то, что оно доходит до
самого конца — до отсутствия задачи в очереди.

Заготовка ответа собирается из `PRESALES_V2_REQUIRED_FIELDS`, а не из
переписанного руками списка: набор полей проверяется на точное совпадение в
обе стороны, и захардкоженный список пришлось бы править при каждом новом
поле — молча получая `presales_v2_schema_missing_fields` вместо проверяемого.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import autoreply, entities, presales_v2, replies  # noqa: E402
from bridge49.store import Store, new_id, now  # noqa: E402
from bridge49.truth_pack import load_customer_truth_pack  # noqa: E402

PACK = load_customer_truth_pack()

ОТКАЗ = (
    "На данный момент я не планирую использовать сторонние сервисы для поиска "
    "клиентов, спасибо за ваше предложение."
)

#: Значения нестроковых полей контракта. Всё остальное — пустая строка.
НЕ_СТРОКИ: dict[str, object] = {
    "confidence": 0.95,
    "coverage_complete": True,
    "collected_fields_update": {},
    "turn_items": [],
}


def элемент(**overrides) -> dict[str, object]:
    """Пункт хода. По умолчанию — молчаливый: ни ответа, ни долга."""
    item: dict[str, object] = {
        "item_id": "q1",
        "topic": "general",
        "user_item": "отказ от предложения",
        "user_evidence": "не планирую использовать сторонние сервисы",
        "status": "not_applicable",
        "answer_summary": "отказ принят, ответ не требуется",
        "reply_evidence": "",
        "source_ids": [],
    }
    item.update(overrides)
    return item


def ответ(**overrides) -> dict[str, object]:
    """Полный контракт движка с заполненными по умолчанию полями."""
    raw: dict[str, object] = {
        name: НЕ_СТРОКИ.get(name, "")
        for name in presales_v2.PRESALES_V2_REQUIRED_FIELDS
    }
    raw.update({
        "action": "pause",
        "intent": "soft_negative",
        "risk_level": "low",
        "next_state": "paused",
        "handoff_kind": "none",
        "reason": "прямой отказ",
        "turn_items": [элемент()],
    })
    raw.update(overrides)
    return raw


def разобрать(raw: dict[str, object]):
    return presales_v2.normalize_presales_v2_result(
        raw, pack=PACK, required_topics=("general",), inbound_text=ОТКАЗ,
    )


class КонтрактОтказа(unittest.TestCase):
    def test_прямой_отказ_проходит_молча(self):
        итог = разобрать(ответ())
        self.assertFalse(итог.technical_failure, итог.reason)
        self.assertEqual(итог.decision, "pause_conversation")
        self.assertEqual(итог.reply_text, "")
        self.assertFalse(итог.handoff_required)

    def test_ответ_на_отказ_больше_не_проходит(self):
        """Ровно тот случай, что ушёл человеку 06.08."""
        итог = разобрать(ответ(
            action="reply_and_pause",
            reply_text="Понял, спасибо за ответ.",
            turn_items=[элемент(status="answered",
                                reply_evidence="Понял, спасибо за ответ.")],
        ))
        self.assertTrue(итог.technical_failure)
        self.assertEqual(итог.reason,
                         "presales_v2_soft_negative_requires_silent_pause")

    def test_молчание_с_незакрытым_вопросом_не_проходит(self):
        """Отказ вперемешку с вопросом — это вопрос, и отвечать надо.

        Дороже ошибиться в сторону карточки, чем оборвать разговор молча.
        """
        for статус in ("clarification_requested", "action_required",
                       "needs_manager", "answered"):
            with self.subTest(статус=статус):
                итог = разобрать(ответ(turn_items=[элемент(status=статус)]))
                self.assertTrue(итог.technical_failure)
                self.assertEqual(итог.reason,
                                 "presales_v2_silent_pause_owes_reply")

    def test_отказ_вне_soft_negative_не_ломает_обычный_ответ(self):
        """Проверка обязана быть узкой: обычный ответ на вопрос живёт как жил."""
        итог = разобрать(ответ(
            action="reply",
            # Не факт-интент намеренно: `faq_question` со статусом `answered`
            # требует ещё и source_id, а проверяется здесь не это.
            intent="neutral",
            reply_text="Находим запросы в чатах. Какая у вас сфера?",
            turn_items=[элемент(
                status="answered",
                reply_evidence="Находим запросы в чатах.",
            )],
        ))
        self.assertFalse(итог.technical_failure, итог.reason)
        self.assertEqual(итог.decision, "auto_reply")

    def test_повторный_заход_просит_выбросить_текст(self):
        """Общее «сохрани содержательный ответ» здесь чинит не в ту сторону.

        Без своей ветки второй заход повторял бы первый, и каждый отказ
        стоил бы менеджеру карточки на пустом месте.
        """
        for причина in ("presales_v2_soft_negative_requires_silent_pause",
                        "presales_v2_silent_pause_owes_reply"):
            with self.subTest(причина=причина):
                текст = presales_v2.presales_v2_repair_instruction(причина)
                self.assertIn("пустой reply_text", текст)
                self.assertIn("Прежний текст ответа не сохраняй", текст)


class ПромптОтказа(unittest.TestCase):
    def подсказка(self) -> str:
        payload = presales_v2.build_presales_v2_prompt(
            inbound_text=ОТКАЗ, context={}, pack=PACK,
            required_topics=("general",), reasoning_effort="high",
        )
        return "\n".join(
            str(value) for value in payload.values() if isinstance(value, str)
        )

    def test_промпт_требует_молчать(self):
        текст = self.подсказка()
        self.assertIn("Прямой отказ оставляй совсем без ответа", текст)
        self.assertIn("action=pause", текст)

    def test_промпт_не_требует_обратного(self):
        """Старая формулировка требовала ответа на отказ прямым текстом."""
        self.assertNotIn("вежливый отказ не оставляй без ответа",
                         self.подсказка())


SNAPSHOT = [
    {
        "id": 821, "label": "dm-one", "program_code": "TGR1",
        "runtime_state": "running",
        "outreach": {
            "enabled": True, "roles": ["dm_sender"], "publish_inbound": True,
            "allow_immediate_visible_actions": True,
            "allowed_actions": ["reply_private_dm", "send_private_dm"],
        },
    },
]


class МолчаниеДоОчереди(unittest.TestCase):
    """Контракт — половина дела: важно, что до очереди ничего не доходит."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "b.sqlite")
        accounts_mod.sync(self.store, SNAPSHOT)
        contact = entities.add_contact(
            self.store, username="someone", segment="inbound", actor="test")
        self.thread_id = new_id("thread")
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface,"
            " state, created_at, updated_at) "
            "VALUES(?,821,'@someone',?,'private_dm','open',?,?)",
            (self.thread_id, contact["id"], now(), now()),
        )
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, sent_at, raw, created_at) "
            "VALUES(5001,821,'private_dm','@someone','someone',?,?,'{}',?)",
            (ОТКАЗ, now(), now()),
        )
        self.store.commit()
        self.inbound = dict(
            self.store.one("SELECT * FROM inbound WHERE id = 5001"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_отказ_не_создаёт_ни_задачи_ни_карточки(self):
        итог = autoreply.apply(self.store, self.inbound, {
            "decision": "pause_conversation",
            "reply_text": "",
            "intent": "soft_negative",
            "confidence": 0.95,
            "risk_level": "low",
            "validation_warnings": [],
            "collected_fields_update": {},
            "knowledge_gap": "",
            "reason": "прямой отказ",
            "handoff_kind": "none",
        }, actor="test")

        self.assertEqual(итог["task"], "")
        self.assertEqual(итог["handoff"], "")
        self.assertEqual(итог["sent_text"], "")
        self.assertEqual(self.store.query("SELECT id FROM tasks"), [])
        self.assertEqual(self.store.query("SELECT id FROM handoffs"), [])
        state = self.store.one(
            "SELECT state FROM threads WHERE id = ?", (self.thread_id,))
        self.assertEqual(state["state"], "awaiting")

    def test_отказ_видно_в_журнале(self):
        """Молчание обязано оставлять след: иначе оно неотличимо от поломки."""
        autoreply.apply(self.store, self.inbound, {
            "decision": "pause_conversation", "reply_text": "",
            "intent": "soft_negative", "confidence": 0.95,
            "risk_level": "low", "validation_warnings": [],
            "collected_fields_update": {}, "knowledge_gap": "",
            "reason": "прямой отказ", "handoff_kind": "none",
        }, actor="test")
        kinds = [row["kind"] for row in self.store.query(
            "SELECT kind FROM events WHERE subject = ?", (self.thread_id,))]
        self.assertIn("autoreply.pause_conversation", kinds)


if __name__ == "__main__":
    unittest.main()
