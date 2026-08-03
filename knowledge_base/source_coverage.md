# Source Coverage

Дата обновления: 2026-07-09.

Назначение: карта источников, из которых собрана customer-facing база знаний TG RADAR Outreach. Этот файл не предназначен для автоматической отправки клиенту; он нужен для аудита покрытия KB.

## Использованные разделы TG RADAR OS

- `01_Company/company_overview.md` и `01_Company/positioning.md`: бренд, позиционирование, каналы, публичные факты, юридический контур, отличие от рекламы.
- `03_Product/product_catalog.md` и `03_Product/service_packages.md`: продуктовая механика, карточка сигнала, тарифная логика, лимиты тарифов, ограничения по гарантиям.
- `05_Marketing/messaging.md` и сайтовые messaging-материалы: формулировки оффера, язык "без рекламы", "живой спрос", "сигналы", "демо по нише".
- `06_Website/production_repo/radarWeb`: текущие production-страницы сайта: главная, услуги, тарифы, кейсы, отзывы, примеры сигналов, сферы, площадки Telegram/VK/Max, отраслевые страницы, партнерская программа, юридические страницы.
- `06_Website/site_pages_overview.md`: карта production-страниц и список основных публичных маршрутов.
- `08_Sales/offers.md`: основной продаваемый оффер, что входит, формат продажи, аргументация стоимости и ограничения.
- `10_Finance_Admin/company_requisites.md` и публичная оферта: только публичный юридический контур; реквизиты, счета и документы в автоматических ответах передаются менеджеру.

## Использованные страницы сайта

- `https://tgradar.ru/` - общее позиционирование, proof points, площадки.
- `https://tgradar.ru/uslugi` - сценарии услуг.
- `https://tgradar.ru/price` - тарифы GO / PLUS / PRO и лимиты.
- `https://tgradar.ru/cases` - публичные кейсы.
- `https://tgradar.ru/otzyvy` - отзывы и социальное доказательство.
- `https://tgradar.ru/primery-signalov` - обезличенные примеры сигналов.
- `https://tgradar.ru/sfery` и отраслевые страницы: ВЭД / Китай, банкротство, визы, медицина, недвижимость, 1С, CRM, туризм.
- `https://tgradar.ru/tg`, `https://tgradar.ru/vk`, `https://tgradar.ru/max` - страницы площадок.
- `https://tgradar.ru/partners` - партнерская программа.
- `https://tgradar.ru/blog`, `https://tgradar.ru/lidy-bez-reklamy`, `https://tgradar.ru/b2c-biznes` - блог и контентные подборки; используются как дополнительные материалы, не как источник гарантий или коммерческих условий.
- `https://tgradar.ru/oferta`, `https://tgradar.ru/privacy`, `https://tgradar.ru/consent`, `https://tgradar.ru/cookies` - юридические страницы; используются только для ссылок и общих фактов, не для автоматического юридического консультирования.

## Во что перенесено

- `company_profile.md`: официальный сайт, бренд, юрконтур, площадки, услуги, сферы, отзывы, демо, ограничения.
- `product_overview.md`: продуктовая механика, сигнал, что получает клиент, что входит, чем сервис не является.
- `service_scenarios.md`: B2B, B2C, отдел продаж, репутация, конкуренты, спрос, рынок, тренды, инфоповоды.
- `pricing_policy.md`: публичные тарифы, лимиты, общие элементы, что требует менеджера.
- `case_studies.md`: публичные кейсы и ограничения.
- `reviews.md`: публичные отзывы и ограничения.
- `examples/signal_examples.md`: публичные обезличенные примеры сигналов.
- `partner_program.md`: публичная партнерская программа и handoff-границы.
- `blog_content.md`: блог, контентные подборки и правила безопасного использования статей в presales.
- `sector_notes/*`: отраслевые запросы, применимость, ограничения и sensitive-темы.
- `allowed_claims.md` / `forbidden_claims.md`: что можно и нельзя утверждать автоматически.
- `answer_packs/*`: короткие route-пакеты для pricing, cases, reviews, signal examples, services, sector fit, partner program и legal-sensitive вопросов.
- `answer_cards/*`: короткие карточки ответа для частых presales-вопросов. Они задают безопасную форму ответа и связывают вопрос с детальными proof-источниками; отдельно покрыты guarantees, outreach surfaces и contact source / consent-вопросы.
- `fact_registry.md`: компактный реестр проверенных фактов, публичных ссылок, тарифных ориентиров, proof-ограничений и handoff-границ.
- `reply_few_shots.md`: эталонные короткие ответы для стиля и структуры; не используется как источник новых фактов.
- `kb_ontology.json`: алиасы, синонимы и topic mapping, чтобы retrieval понимал разные формулировки одного вопроса.
- `kb_manifest.json` и `kb_manifest_presales_extensions.json`: metadata-слой для retrieval, routing, answer packs/cards, audit, freshness-проверок и eval.
- `data/kb_eval_questions.json`: smoke-набор вопросов, по которым проверяется, что retrieval достает нужные источники.
- `scripts/debug_kb_retrieval.py`: read-only отладчик маршрута, выбранных cards/packs, чанков, `usage_type` и prompt summary для одного вопроса.
- `scripts/build_kb_eval_candidates.py`: read-only генератор кандидатов для eval-набора из `knowledge_gaps` и недавних входящих сигналов.
- `presales_synthetic_testing.md`: runbook для offline и real-LLM stress suite, правил интерпретации source coverage, fallback и safety false positives.

## Что сознательно не переносится в клиентский LLM-контекст

- Внутренние планы, TODO, проектные решения, доступы, деплой-инструкции, SSH, dashboard/admin-инфраструктура, личные заметки и операционные runbooks TG RADAR OS.
- Черновые или устаревшие цифры из презентаций, если они конфликтуют с сайтом.
- Внутренние пометки о лидах / месяц по тарифам, если они не опубликованы на сайте.
- Медицинские, юридические, визовые, таможенные, финансовые и платежные консультации: такие вопросы только через менеджера / специалиста.

## Правило дальнейшего обновления

Если на сайте или в TG RADAR OS появляется новый публичный факт, кейс, тариф, отзыв, сценарий услуги или отраслевая страница, его нужно переносить в соответствующий customer-facing KB-файл, обновлять `fact_registry.md`, `kb_manifest.json` или `kb_manifest_presales_extensions.json`, при необходимости добавлять / править `answer_packs/*`, `answer_cards/*` и `kb_ontology.json`, расширять `data/kb_eval_questions.json` и добавлять файл в campaign `knowledge_base_scope`. Внутреннюю операционку переносить нельзя, если она не предназначена для клиента.

После изменения базы знаний запускать:

```bash
python3 scripts/sync_tgradar_kb.py --dry-run --check-db-scope
python3 scripts/debug_kb_retrieval.py --question "Сколько стоит TG RADAR?"
python3 scripts/synthetic_presales_stress.py --output-dir runtime/presales_stress --fail-on-issues
python3 scripts/synthetic_presales_stress.py --use-external-llm --output-dir runtime/presales_stress_llm --fail-on-issues
python3 scripts/build_kb_eval_candidates.py --limit 20 --output runtime/kb_eval_candidates.json
python3 -m unittest tests.test_kb_eval_questions
```

`build_kb_eval_candidates.py` не обновляет `data/kb_eval_questions.json` автоматически: кандидаты нужно просмотреть вручную, оставить только стабильные пользовательские вопросы и добавить ожидаемые источники осознанно.
