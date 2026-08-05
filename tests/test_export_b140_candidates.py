"""Отбор кандидатов на личное сообщение: кого нельзя брать вовсе.

Правила здесь дешёвые и грубые, и это их достоинство: они отвечают только на
вопрос «до этого адресата мы вообще дотянемся». Смысловую пригодность решает
второй слой, `contact_fit`, и подменять одно другим нельзя — иначе в очередь
попадает то, что и отправить-то невозможно.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "export_b140_candidates", ROOT / "scripts" / "export_b140_candidates.py")
export = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export)


def row(**overrides) -> dict:
    base = {
        "author_username": "someseller",
        "author_native_id": "8822718124",
        "author_name": "Максим",
        "author_config": None,
        "author_banned": False,
        "message_text": "Кто может настроить рекламу на ВБ?",
        "category": "HOT",
        "match_score": 0.9,
        "match_source": "keyword",
        "source_title": "WB чат",
        "permalink": "",
        "published_at": "2026-08-04",
        "btm_id": 1,
    }
    base.update(overrides)
    return base


class ChannelAuthorTests(unittest.TestCase):
    """Каналу в личку не напишешь, а в выборке он неотличим от человека."""

    def test_negative_id_is_a_channel(self):
        self.assertTrue(export.is_channel(row(author_native_id="-1001362408356")))
        self.assertTrue(export.is_channel(row(author_native_id="-1002156754580")))

    def test_positive_id_is_a_person(self):
        self.assertFalse(export.is_channel(row(author_native_id="8822718124")))
        self.assertFalse(export.is_channel(row(author_native_id="118516838")))

    def test_missing_id_is_not_treated_as_a_channel(self):
        """Пустой идентификатор — не повод выбрасывать живого человека."""
        self.assertFalse(export.is_channel(row(author_native_id=None)))
        self.assertFalse(export.is_channel(row(author_native_id="")))

    def test_channel_is_refused_by_name(self):
        out = export.select([row(author_username="jobfortarget",
                                 author_native_id="-1001362408356")],
                            contacted=set(), limit=10)
        self.assertEqual(out["кандидаты"], [])
        self.assertEqual(out["отсеяно"].get("channel_author"), 1)

    def test_person_still_passes(self):
        out = export.select([row()], contacted=set(), limit=10)
        self.assertEqual(len(out["кандидаты"]), 1)
        self.assertEqual(out["кандидаты"][0]["username"], "someseller")


class OtherRulesStillHoldTests(unittest.TestCase):
    def test_bot_is_refused(self):
        out = export.select([row(author_username="helper_bot")],
                            contacted=set(), limit=10)
        self.assertEqual(out["отсеяно"].get("bot_author"), 1)

    def test_already_contacted_is_refused(self):
        out = export.select([row()], contacted={"someseller"}, limit=10)
        self.assertEqual(out["отсеяно"].get("already_contacted_private_dm"), 1)

    def test_banned_author_is_refused(self):
        out = export.select([row(author_banned=True)], contacted=set(), limit=10)
        self.assertEqual(out["отсеяно"].get("business_author_banned"), 1)

    def test_non_russian_is_refused(self):
        out = export.select([row(message_text="Hi, who can help with ads?")],
                            contacted=set(), limit=10)
        self.assertEqual(out["отсеяно"].get("non_russian_message"), 1)

    def test_one_best_message_per_person(self):
        rows = [row(btm_id=1, category="COLD", match_score=0.9),
                row(btm_id=2, category="HOT", match_score=0.5)]
        out = export.select(rows, contacted=set(), limit=10)
        self.assertEqual(len(out["кандидаты"]), 1)
        self.assertEqual(out["кандидаты"][0]["категория"], "HOT")


if __name__ == "__main__":
    unittest.main()
