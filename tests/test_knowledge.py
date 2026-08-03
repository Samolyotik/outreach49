"""Поиск по базе знаний.

Перенесено из прежнего контура (`outreach/knowledge.py`, релиз a55d259) без
изменений в самом алгоритме. Отличие одно: список файлов базы приходит
параметром, а не читается из `campaigns.knowledge_base_scope` — такой колонки
у нас нет, и ради неё пришлось бы заводить чужую схему прямо в тесте.
"""
import tempfile
import unittest
from pathlib import Path

from bridge49.knowledge import (
    answer_card_sources_for_query,
    answer_pack_sources_for_query,
    expand_query_with_ontology,
    retrieve_knowledge_chunks,
)


class KnowledgeRetrievalTests(unittest.TestCase):
    def test_extensionless_scope_resolves_markdown_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_root = Path(temp_dir)
            (kb_root / "faq.md").write_text(
                "# FAQ\n\n## Сайт\n\nОфициальный сайт TG RADAR: https://tgradar.ru/.",
                encoding="utf-8",
            )

            chunks = retrieve_knowledge_chunks(
                ["faq"],
                query="какой сайт",
                kb_root=str(kb_root),
            )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].source, "faq.md")
        self.assertIn("https://tgradar.ru", chunks[0].text)

    def test_repository_prefixed_scope_resolves_inside_kb_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parent = Path(temp_dir)
            kb_root = parent / "knowledge_base"
            kb_root.mkdir()
            (kb_root / "faq.md").write_text(
                "# FAQ\n\n## Сайт\n\nОфициальный сайт TG RADAR: https://tgradar.ru/.",
                encoding="utf-8",
            )

            chunks = retrieve_knowledge_chunks(
                ["knowledge_base/faq.md"],
                query="какой сайт",
                kb_root=str(kb_root),
            )

        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].source, "faq.md")

    def test_answer_pack_routing_uses_manifest_terms(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            kb_root = Path(temp_dir)
            (kb_root / "answer_packs").mkdir()
            (kb_root / "answer_packs" / "pricing_pack.md").write_text(
                "# Pricing\n\n## Что отвечать\n\nПубличные тарифы GO / PLUS / PRO.",
                encoding="utf-8",
            )
            (kb_root / "pricing_policy.md").write_text(
                "# Pricing Policy\n\n## Сколько стоит\n\nЦена зависит от объема.",
                encoding="utf-8",
            )
            (kb_root / "kb_manifest.json").write_text(
                """
                {
                  "sources": {
                    "answer_packs/pricing_pack.md": {
                      "topic": ["pricing"],
                      "intent": ["pricing_question"],
                      "audience": "customer_llm",
                      "risk": "medium",
                      "source_url": "repo:test",
                      "updated_at": "2026-07-09",
                      "priority": 99,
                      "tags": ["цена", "стоимость", "тариф"]
                    },
                    "pricing_policy.md": {
                      "topic": ["pricing"],
                      "intent": ["pricing_question"],
                      "audience": "customer_llm",
                      "risk": "medium",
                      "source_url": "repo:test",
                      "updated_at": "2026-07-09",
                      "priority": 80,
                      "tags": ["цена"]
                    }
                  },
                  "answer_packs": {
                    "pricing_pack": {
                      "source": "answer_packs/pricing_pack.md",
                      "terms": ["цена", "стоимость", "тариф"],
                      "source_hints": ["pricing_policy.md"]
                    }
                  }
                }
                """,
                encoding="utf-8",
            )

            sources = answer_pack_sources_for_query(
                kb_root=kb_root,
                query="Сколько стоит и какие тарифы?",
            )
            chunks = retrieve_knowledge_chunks(
                ["answer_packs/pricing_pack.md", "pricing_policy.md"],
                query="Сколько стоит и какие тарифы?",
                kb_root=str(kb_root),
                limit=2,
            )

        self.assertEqual(sources, ["answer_packs/pricing_pack.md"])
        chunks_by_source = {chunk.source: chunk for chunk in chunks}
        self.assertIn("answer_packs/pricing_pack.md", chunks_by_source)
        self.assertEqual(
            chunks_by_source["answer_packs/pricing_pack.md"].metadata["topic"],
            ["pricing"],
        )

    def test_ontology_expands_aliases_and_routes_answer_cards(self):
        expanded = expand_query_with_ontology("Дайте прайс по тарифам", "knowledge_base")
        card_sources = answer_card_sources_for_query(
            kb_root="knowledge_base",
            query="Дайте прайс по тарифам",
        )

        self.assertIn("FACT_PRICING_PUBLIC", expanded)
        self.assertIn("answer_cards/pricing.md", card_sources)


if __name__ == "__main__":
    unittest.main()
