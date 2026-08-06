"""Письмо про демо-бота дописывается к ответу, а не съедает его.

06.08 движок на четыре предметных вопроса (покрытие по Канаде, площадки,
свежие запросы, цена) ответил по существу и закончил вопросом «запустить
бесплатный тест для химчистки?». Химчистки нет ни в шести сферах выдачи, ни в
словаре из четырнадцати: готовой тестовой группы под неё не существует, звать
туда некуда, а ссылку на демо-бота — единственное, что мы правда можем дать
сразу, — человек получил бы только следующим ходом.

Правка двусоставная, и здесь проверяется её вторая половина. Первая живёт в
промпте: сфера без готовой группы больше не ждёт согласия. Вторая — тут:
раньше письмо про демо ЗАМЕНЯЛО текст движка, и включить первую половину без
второй значило бы менять ответ на четыре вопроса общим письмом про бота.

Отдельным файлом, а не дописыванием в `test_autoreply`: тот сейчас правит
соседняя сессия.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import autoreply, direct_invite, entities, replies  # noqa: E402
from bridge49.store import Store, new_id, now  # noqa: E402

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

#: Выдача включена ровно для одной сферы — авто из-за границы.
ВЕТКА = {
    "schema_version": 1, "enabled": True,
    "active_sector_ids": ["auto_import_dealers"],
    "validity_days": 7, "max_attempts": 5,
    "sector_profiles": {"auto_import_dealers": {
        "outreach_sector_id": "auto_import_dealers",
        "sector_id": "cars_abroad",
        "sector_name": "Авто из-за границы",
        "test_group_profile_id": "cars_abroad_test_group"}},
}

#: Словарь распознавания. Химчистки в нём нет намеренно — как и в боевом.
СЛОВАРЬ = {
    "schema_version": 2,
    "demo_bot_link": "https://t.me/tg_radar_robot?start=outreach",
    "sectors": [
        {"canonical_sector_id": "auto_import_dealers",
         "sector_name": "Авто из-за границы", "status": "ready"},
        {"canonical_sector_id": "crm_1c",
         "sector_name": "CRM, 1С и автоматизация продаж", "status": "manual"},
    ],
}

ОТВЕТ = (
    "По Канаде фиксированный набор источников заранее не обещается. "
    "Стоимость начинается от 29 000 ₽ в месяц."
)


class ПисьмоДописывается(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "branch.json").write_text(json.dumps(ВЕТКА), encoding="utf-8")
        (tmp / "catalog.json").write_text(
            json.dumps(СЛОВАРЬ, ensure_ascii=False), encoding="utf-8")
        self.branch = direct_invite.BranchConfig.from_path(
            tmp / "branch.json").with_sector_catalog(tmp / "catalog.json")

        self.store = Store(tmp / "b.sqlite")
        accounts_mod.sync(self.store, SNAPSHOT)
        contact = entities.add_contact(
            self.store, username="someone", segment="inbound", actor="test")
        self.thread_id = new_id("thread")
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface,"
            " state, created_at, updated_at) "
            "VALUES(?,821,'@someone',?,'private_dm','open',?,?)",
            (self.thread_id, contact["id"], now(), now()))
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, sent_at, raw, created_at) "
            "VALUES(5001,821,'private_dm','@someone','someone',?,?,'{}',?)",
            ("Какие площадки доступны для Канады и сколько стоит?",
             now(), now()))
        self.store.commit()
        self.inbound = dict(
            self.store.one("SELECT * FROM inbound WHERE id = 5001"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def согласие(self, **extra):
        """Решение движка: сфера подтверждена, готовой группы под неё нет."""
        payload = {
            "decision": "reply_and_handoff",
            "reply_text": ОТВЕТ,
            "intent": "faq_question",
            "confidence": 0.95,
            "risk_level": "low",
            "validation_warnings": [],
            "collected_fields_update": {"sector": "химчистка диванов"},
            "knowledge_gap": "",
            "reason": "",
            "handoff_required": True,
            "handoff_kind": "free_test_access",
            "matched_direct_invite_sector_id": "",
            "client_sector_text": "химчистка диванов",
            "canonical_sector_id": "",
            "sector_confidence": "none",
        }
        payload.update(extra)
        return payload

    def письма(self):
        return [json.loads(row["params"])["text"]
                for row in self.store.query(
                    "SELECT params FROM tasks WHERE campaign_id = ?",
                    (replies.AUTO_CAMPAIGN_ID,))]

    def test_ответ_движка_остаётся_целым(self):
        итог = autoreply.apply(self.store, self.inbound, self.согласие(),
                               actor="test", branch_config=self.branch)
        self.assertTrue(итог["demo"], "демо-письмо обязано собраться")
        письма = self.письма()
        self.assertEqual(len(письма), 1, f"письмо должно быть одно: {письма}")
        self.assertIn(ОТВЕТ, письма[0], "ответ на вопросы человека потерян")
        self.assertIn("t.me/tg_radar_robot", письма[0], "ссылки нет")

    def test_ссылка_идёт_после_ответа(self):
        """Порядок не косметика: сначала по делу, потом ссылка."""
        autoreply.apply(self.store, self.inbound, self.согласие(),
                        actor="test", branch_config=self.branch)
        письмо = self.письма()[0]
        self.assertLess(письмо.index(ОТВЕТ), письмо.index("t.me/tg_radar_robot"))

    def test_карточку_менеджеру_не_заводим(self):
        """Незнакомая сфера — не повод звать человека: разговор идёт в боте."""
        итог = autoreply.apply(self.store, self.inbound, self.согласие(),
                               actor="test", branch_config=self.branch)
        self.assertEqual(итог["handoff"], "")
        self.assertEqual(self.store.query("SELECT id FROM handoffs"), [])

    def test_на_молчаливом_ходу_письма_нет(self):
        """Ветка не должна заговаривать там, где движок решил промолчать."""
        итог = autoreply.apply(
            self.store, self.inbound,
            self.согласие(decision="hold_for_review", reply_text=""),
            actor="test", branch_config=self.branch)
        self.assertEqual(итог["demo"], "")
        self.assertEqual(self.письма(), [])


class ПунктХодаПодДемо(unittest.TestCase):
    """Демо-ссылке не нужен пункт `action_required`, а настоящей выдаче нужен.

    Пункт со статусом `action_required` держит одно: нельзя пометить запуск
    теста как `answered`, пока доступа ещё нет. У демо-маршрута выдавать
    нечего — ссылка общая и бессрочная, — а требование пункта означало бы,
    что человек обязан о ней попросить. Просить он не должен, в этом вся суть.
    """

    from bridge49 import presales_v2 as _pv2
    from bridge49.truth_pack import load_customer_truth_pack as _pack

    PACK = _pack()
    ХОД = "Занимаемся химчисткой диванов. Сколько стоит?"

    def ответ(self, **overrides):
        raw = {name: {"confidence": 0.9, "coverage_complete": True,
                      "collected_fields_update": {"sector": "химчистка диванов"},
                      "turn_items": []}.get(name, "")
               for name in self._pv2.PRESALES_V2_REQUIRED_FIELDS}
        raw.update({
            "action": "reply_and_handoff",
            # Не факт-интент: `pricing_question` со статусом `answered`
            # требует ещё и source_id, а проверяется здесь не это.
            "intent": "neutral",
            "reply_text": "Стоимость от 29 000 ₽ в месяц. Показать, как ищет?",
            "risk_level": "low",
            "next_state": "FAQ automation",
            "handoff_kind": "free_test_access",
            "handoff_reason": "демо-бот",
            "client_sector_text": "химчисткой диванов",
            "turn_items": [{
                "item_id": "q1", "topic": "general", "user_item": "цена",
                "user_evidence": "Сколько стоит?", "status": "answered",
                "answer_summary": "назвал цену",
                "reply_evidence": "Стоимость от 29 000 ₽ в месяц.",
                "source_ids": [],
            }],
        })
        raw.update(overrides)
        return raw

    def разобрать(self, raw):
        return self._pv2.normalize_presales_v2_result(
            raw, pack=self.PACK, required_topics=("general",),
            inbound_text=self.ХОД,
            allowed_direct_invite_sector_ids=["auto_import_dealers"],
        )

    def test_демо_проходит_без_отдельного_пункта(self):
        итог = self.разобрать(self.ответ())
        self.assertFalse(итог.technical_failure, итог.reason)
        self.assertEqual(итог.decision, "reply_and_handoff")
        self.assertEqual(итог.matched_direct_invite_sector_id, "")

    def test_настоящая_выдача_пункт_по_прежнему_требует(self):
        итог = self.разобрать(self.ответ(
            matched_direct_invite_sector_id="auto_import_dealers"))
        self.assertTrue(итог.technical_failure)
        self.assertEqual(итог.reason, "presales_v2_free_test_without_action_item")


if __name__ == "__main__":
    unittest.main()
