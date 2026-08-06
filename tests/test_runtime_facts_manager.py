"""Что о менеджере знает модель на самом деле.

Правку политики 06.08 легко сделать вхолостую: проза `knowledge_base/*.md` в
живой промпт не попадает вовсе. Боевой манифест
`customer_truth_sources_v2.json` пинит `runtime_catalog`, и в промпт уезжает
только он — курированный список фактов. Полный текст источников отсекается в
`truth_pack._load_runtime_catalog`.

Поэтому единственное место, где «выдачу доступа делает менеджер» можно
запретить модели, — сам runtime-каталог. Тест держит именно его: правка
`.md`-файлов проверку не пройдёт.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB = ROOT / "knowledge_base"
SOURCES = KB / "customer_truth_sources_v2.json"


def runtime_facts() -> dict[str, dict]:
    manifest = json.loads(SOURCES.read_text(encoding="utf-8"))
    name = str(manifest.get("runtime_catalog") or "").strip()
    if not name:
        raise AssertionError("боевой манифест обязан пинить runtime_catalog")
    raw = json.loads((KB / name).read_text(encoding="utf-8"))
    return {str(fact["fact_id"]): fact for fact in raw["facts"]}


class ManagerBoundariesTests(unittest.TestCase):
    def setUp(self):
        self.facts = runtime_facts()

    def test_the_manifest_still_pins_a_curated_catalog(self):
        """Если пин исчезнет, в промпт уедет проза — и вернётся старая политика."""
        manifest = json.loads(SOURCES.read_text(encoding="utf-8"))
        self.assertTrue(manifest.get("runtime_catalog"))

    def test_issuing_access_is_no_longer_manager_only(self):
        text = str(self.facts["MANAGER_BOUNDARIES"]["text"])
        for phrase in ("выдачи доступа", "фактического запуска теста"):
            self.assertNotIn(
                phrase, text,
                f"«{phrase}» в manager-only списке возвращает ту самую политику, "
                "из-за которой человек с подтверждённой сферой получал "
                "«менеджер свяжется» вместо ссылки",
            )

    def test_the_fact_names_the_automatic_route_instead(self):
        """Мало убрать запрет: модель должна знать, что делать вместо него."""
        text = str(self.facts["MANAGER_BOUNDARIES"]["text"])
        self.assertIn("демо-бот", text)

    def test_the_demo_fact_does_not_send_access_to_a_manager(self):
        fact = self.facts["DEMO_AND_FREE_TEST"]
        conditions = str(fact.get("conditions") or "")
        self.assertIn("автоматика", conditions)
        self.assertNotIn("или менеджером", conditions)


if __name__ == "__main__":
    unittest.main()
