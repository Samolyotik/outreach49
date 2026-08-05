"""Квалификация кандидата на личное сообщение.

Перенос LLM-проверки из прежнего контура
(`outreach/tgradar_contact_pipeline.py`, `build_llm_prompt` и рядом). Правила
отбора уже перенесены в `scripts/export_b140_candidates.py`; здесь второй слой,
который они не заменяют.

Зачем он нужен. Правила отвечают на вопрос «до этого человека мы дотянемся и
он писал по-русски». Они не отвечают на вопрос «ему вообще есть смысл писать».
В выборке хватает спам-рассылок, серых схем, бирж трафика и наших же
конкурентов — написать такому не касание, а холостой ход.

## Почему v3 мягче v2

Первый прогон 05.08 разобрал 60 кандидатов и оставил 2. Разбор показал, что
планка стояла не там: v2 требовала, чтобы САМО сообщение показывало боль
лидогенерации. Но входной пул — это уже лиды бизнеса 140, то есть сообщения,
которые наш собственный продакшн признал релевантными ТГ РАДАР. Второй слой,
переспрашивающий первый и почти всегда отвечающий «нет», не фильтр, а глушилка.

Поэтому рамка перевёрнута. Наш клиент — не тот, кто пожаловался на лидоген, а
любой, кто продаёт и кому нужны заявки: владелец, селлер, мастер, агентство.
Человек, который покупает рекламу или нанимает таргетолога, уже платит за
привлечение — он ближе к нам, чем тот, кто рассуждает о лидах вообще.

Отказ теперь — закрытый список причин (спам, серое, торговля трафиком,
конкурент, соискатель, небизнес, язык, пусто). Всё, что не попало в список,
получает как минимум `maybe`. Это осознанный сдвиг в сторону полноты: цена
лишнего вежливого сообщения ниже цены пропущенного живого клиента.

Что сохранено из переноса:

* решение выносится ПО ТЕКСТУ СООБЩЕНИЯ, а не по автору. Имя и username не
  передаются: человек оценивается по тому, что написал, а не как подписан;
* при нехватке данных модель обязана выбрать `maybe` — не `qualified` и,
  в отличие от v2, не `reject`;
* `outreach_angle` — не готовый текст письма, а короткий смысл будущего
  обращения. Текст пишет `first_touch`, и у него свой контракт;
* согласованность decision и fit_score проверяется на нашей стороне: модель,
  поставившая `qualified` с оценкой 50, отвергается целиком.
"""
from __future__ import annotations

import collections
import json
import re
from typing import Any, Mapping, Sequence

PROMPT_VERSION = "tgradar_contact_fit_v3_recall_first"

QUALIFIED_MIN_SCORE = 70
MAYBE_MIN_SCORE = 40

VALID_DECISIONS = ("maybe", "qualified", "reject")
VALID_CONFIDENCE = ("high", "low", "medium")

#: Причины, по которым человеку писать не надо. Список закрытый: если ни одна
#: не подходит, отказа быть не может. Именно это отличает v3 от v2, где отказ
#: был свободным решением модели.
REJECT_INTENTS = (
    "competitor",
    "grey_or_prohibited",
    "job_seeker",
    "non_russian",
    "personal_or_nonbusiness",
    "spam",
    "traffic_trade",
    "unclear",
)

#: За что человек проходит. По разбивке видно, чем именно живёт пул: сейчас это
#: в основном покупатели рекламы и наниматели подрядчиков, а не жалобы на лиды.
FIT_INTENTS = (
    "ad_or_traffic_buyer",
    "business_need_other",
    "contractor_or_tool_search",
    "lead_generation_need",
    "market_demand_research",
    "marketing_or_sales_problem",
    "reputation_or_competitor_monitoring",
    "service_seller_needs_clients",
)

VALID_INTENTS = tuple(sorted(FIT_INTENTS + REJECT_INTENTS))

#: Рамка целевого клиента. Держится отдельной константой, потому что это и есть
#: предмет спора между версиями промпта, и менять её надо осознанно.
ICP = """
Наш потенциальный клиент — любой, кто продаёт товар или услугу и кому нужен
поток клиентов. Рамка широкая, и сужать её нельзя:

* владельцы бизнеса, ИП, самозанятые, селлеры маркетплейсов, локальные услуги;
* фрилансеры, мастера и специалисты, которые ищут заказы;
* агентства, студии, продюсерские центры, онлайн-школы;
* те, кто покупает рекламу, трафик или маркетинговые услуги для своего
  продукта: у них уже есть бюджет на привлечение, и мы прямая альтернатива;
* те, кто нанимает таргетолога, директолога, авитолога, SMM или закупщика
  рекламы под свой проект: это то же самое, только другими словами;
* те, кто спрашивает про боты и сервисы, присылающие заявки из чатов,
  сравнивает их или уже ими пользуется: у них наш конкурент уже был;
* те, кто жалуется на дорогую рекламу, слабые лиды, отсутствие заявок;
* те, кто спрашивает «где искать клиентов», «куда рекламировать свои услуги».

Точно понимать нишу человека не обязательно. Достаточно, что по сообщению
видно делового человека и есть честный повод написать.
""".strip()

#: Каждая причина отказа названа так, как её потом видно в разбивке намерений.
REJECT_RULES = """
* `spam` — шаблонная рассылка с контактами и ссылками, продажа баз данных,
  «холодка / реги / депы», подмена букв латиницей внутри русских слов.
* `grey_or_prohibited` — серые и запрещённые схемы: регистрация ООО и ИП с
  поиском «поставщиков по людям», карты и счета, дропы, обмен USDT и тезера,
  гемблинг, адалт, нутра.
* `traffic_trade` — трафик и аудитория как товар: покупка и продажа каналов,
  биржи ПДП, ЦПМ и МЦА, взаимный пиар, накрутка подписчиков и просмотров,
  «куплю ОП», заливы УБТ, арбитраж как основной бизнес.
* `competitor` — сам продаёт рассылки по чатам, парсинг, «лиды из Telegram»,
  мониторинг чатов. Это провайдер, а не покупатель.
* `job_seeker` — соискатель: резюме, «ищу работу», отклик на вакансию.
* `personal_or_nonbusiness` — личное, бытовое, техподдержка, автопостинг,
  вопрос по интерфейсу площадки без коммерческой задачи.
* `non_russian` — украиноязычное или полностью англоязычное сообщение.
* `unclear` — по тексту нельзя понять вообще ничего.
""".strip()

#: Две оговорки, на которых модель ошибается чаще всего.
REJECT_CAVEATS = """
Конкурент — только тот, кто продаёт именно поиск клиентов из чатов или
рассылки. Юрист, бухгалтер, разработчик ботов, дизайнер, рекламирующие свои
услуги, — не конкуренты, а бизнесы, которым нужны клиенты: это `maybe` с
намерением `service_seller_needs_clients`, а не отказ.

Селлер, покупающий рекламу для своего магазина или товара, — это `qualified`,
а не `traffic_trade`. Разница в том, есть ли у автора собственный продукт.
""".strip()


def clean(value: object) -> str:
    return str(value or "").strip()


_WORD_RE = re.compile(r"[^а-яёa-z0-9 ]")
_LINK_RE = re.compile(r"https?://\S+|@[\w]+")
_MIXED_RE = re.compile(
    r"[А-Яа-яЁё]*[A-Za-z][А-Яа-яЁё]+|[А-Яа-яЁё]+[A-Za-z]+")


def _shingles(text: str, size: int = 5) -> set[str]:
    body = _LINK_RE.sub(" ", text.lower())
    words = _WORD_RE.sub(" ", body).split()
    return {" ".join(words[i:i + size])
            for i in range(max(0, len(words) - size + 1))}


def template_repeats(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """В скольких сообщениях пула встречается тот же пятисловный фрагмент.

    Спам-постинг и фермы фейковых вакансий переклеивают одни и те же блоки по
    десяткам чатов. Одна строка этого не покажет, а пул показывает: считать
    можно только зная всю выборку, поэтому счёт живёт здесь, а не в промпте.
    """
    document_frequency: collections.Counter[str] = collections.Counter()
    per_row: list[tuple[str, set[str]]] = []
    for row in rows:
        key = str(row.get("btm_id") or row.get("row_id"))
        grams = _shingles(clean(row.get("сообщение")))
        per_row.append((key, grams))
        document_frequency.update(grams)
    return {key: max((document_frequency[gram] for gram in grams), default=1)
            for key, grams in per_row}


def homoglyph_words(text: str) -> int:
    """Слова, где латиница подмешана в кириллицу, — маркер обхода фильтров."""
    return sum(1 for word in _MIXED_RE.findall(text)
               if re.search(r"[А-Яа-яЁё]", word)
               and re.search(r"[A-Za-z]", word))


def prompt_rows(rows: Sequence[Mapping[str, Any]],
                repeats: Mapping[str, int] | None = None) -> list[dict]:
    """Что видит модель. Только сообщение и его обстановка, без автора.

    Имя и username намеренно не передаются: решение должно опираться на то,
    что человек написал, а не на то, как он подписан.
    """
    out = []
    for row in rows:
        row_id = str(row.get("btm_id") or row.get("row_id"))
        text = clean(row.get("сообщение"))
        out.append({
            "row_id": row_id,
            "source_category": clean(row.get("категория")),
            "message_text": text[:5000],
            "source_title": clean(row.get("источник"))[:500],
            "template_repeats": (repeats or {}).get(row_id, 1),
            "homoglyph_words": homoglyph_words(text),
        })
    return out


def output_schema() -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reviews"],
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "row_id", "decision", "fit_score", "confidence",
                        "intent", "need_summary", "fit_reason",
                        "outreach_angle", "risks",
                    ],
                    "properties": {
                        "row_id": {"type": "string"},
                        "decision": {"type": "string",
                                     "enum": list(VALID_DECISIONS)},
                        "fit_score": {"type": "integer", "minimum": 0,
                                      "maximum": 100},
                        "confidence": {"type": "string",
                                       "enum": list(VALID_CONFIDENCE)},
                        "intent": {"type": "string", "enum": list(VALID_INTENTS)},
                        "need_summary": {"type": "string", "maxLength": 500},
                        "fit_reason": {"type": "string", "maxLength": 500},
                        "outreach_angle": {"type": "string", "maxLength": 400},
                        "risks": {"type": "array", "items": {"type": "string"},
                                  "maxItems": 8},
                    },
                },
            }
        },
    }


def build_prompt(rows: Sequence[Mapping[str, Any]], knowledge: str,
                 repeats: Mapping[str, int] | None = None) -> str:
    return "\n".join([
        "Ты выполняешь LLM-проверку сигналов для процесса "
        "«Подготовить контакты из ТГ РАДАР».",
        "Это классификация данных, а не задача кодового агента. Не запускай "
        "команды, инструменты, веб и не используй внешние знания.",
        "Для каждого row_id независимо оцени, стоит ли писать этому человеку "
        "личное сообщение.",
        "",
        "КОГО МЫ ИЩЕМ:",
        ICP,
        "",
        "РЕШЕНИЯ:",
        "qualified: по сообщению видно, что автор связан с бизнесом, услугами "
        "или продажами, и есть о чём написать — нужны клиенты, покупает "
        "рекламу, ищет подрядчика или инструмент, жалуется на привлечение.",
        "maybe: деловой контекст правдоподобен, но слабый — очень короткое "
        "сообщение, роль неясна, тема соседняя, или это реклама собственных "
        "услуг без видимой потребности.",
        "reject: только по закрытому списку причин ниже. Во всех остальных "
        "сомнительных случаях ставь maybe, а не reject.",
        "",
        "ЕДИНСТВЕННЫЕ ПРИЧИНЫ ДЛЯ ОТКАЗА (они же значения intent):",
        REJECT_RULES,
        "",
        REJECT_CAVEATS,
        "",
        f"Шкала согласована с decision: qualified={QUALIFIED_MIN_SCORE}-100, "
        f"maybe={MAYBE_MIN_SCORE}-{QUALIFIED_MIN_SCORE - 1}, "
        f"reject=0-{MAYBE_MIN_SCORE - 1}.",
        "intent у отказа — одна из причин выше; у qualified и maybe — одно из: "
        + ", ".join(FIT_INTENTS) + ".",
        "Языковой гейт: рассматриваем только сообщения, основной связный текст "
        "которых написан по-русски. Отдельные латинские термины — Telegram, "
        "CRM, SEO, SMM, ROAS — допустимы.",
        "template_repeats — в скольких сообщениях выборки встречается тот же "
        "пятисловный фрагмент; 3 и больше означает шаблонный постинг. "
        "homoglyph_words — сколько слов с латиницей внутри кириллицы.",
        "Название чата — дополнительный контекст: оно не переопределяет смысл "
        "самого сообщения.",
        "need_summary кратко фиксирует потребность автора только из текста. "
        "outreach_angle — не готовое сообщение, а короткий безопасный смысл "
        "будущего обращения.",
        "Не придумывай должность, компанию или отрасль, которых в тексте нет: "
        "при нехватке контекста это maybe.",
        "Верни один JSON-объект по схеме; reviews должен содержать каждый "
        "входной row_id ровно один раз.",
        "",
        "СХЕМА ОТВЕТА:",
        json.dumps(output_schema(), ensure_ascii=False, indent=2),
        "",
        "КАНОНИЧЕСКАЯ ИНФОРМАЦИЯ О ПРОДУКТЕ:",
        knowledge,
        "",
        "MESSAGES:",
        json.dumps(prompt_rows(rows, repeats), ensure_ascii=False, indent=2),
    ])


class FitError(ValueError):
    """Ответ модели не соответствует контракту."""


def validate(payload: Mapping[str, Any],
             expected_ids: Sequence[str]) -> list[dict]:
    """Разобрать ответ модели. Расхождение с контрактом — отказ целиком.

    Пропускать «почти правильный» ответ нельзя: на другом конце живой человек,
    которому мы напишем, и цена ошибки несимметрична.
    """
    reviews = payload.get("reviews")
    if not isinstance(reviews, list):
        raise FitError("в ответе нет массива reviews")

    seen: set[str] = set()
    out: list[dict] = []
    for raw in reviews:
        if not isinstance(raw, Mapping):
            raise FitError("элемент reviews не объект")
        row_id = clean(raw.get("row_id"))
        decision = clean(raw.get("decision"))
        confidence = clean(raw.get("confidence"))
        intent = clean(raw.get("intent"))
        try:
            score = int(raw.get("fit_score"))
        except (TypeError, ValueError):
            raise FitError(f"fit_score не число у {row_id}") from None

        if not row_id or row_id in seen:
            raise FitError(f"пустой или повторный row_id: {row_id!r}")
        if decision not in VALID_DECISIONS:
            raise FitError(f"неизвестное decision у {row_id}")
        if confidence not in VALID_CONFIDENCE:
            raise FitError(f"неизвестное confidence у {row_id}")
        if intent not in VALID_INTENTS:
            raise FitError(f"неизвестное intent у {row_id}")
        if not 0 <= score <= 100:
            raise FitError(f"fit_score вне 0..100 у {row_id}")
        # Оценка и решение обязаны сходиться. Модель, поставившая qualified с
        # оценкой 50, противоречит сама себе, и верить ей нельзя ни в чём.
        if decision == "qualified" and score < QUALIFIED_MIN_SCORE:
            raise FitError(f"qualified ниже порога у {row_id}")
        if decision == "maybe" and not (
                MAYBE_MIN_SCORE <= score < QUALIFIED_MIN_SCORE):
            raise FitError(f"maybe вне своей полосы у {row_id}")
        if decision == "reject" and score >= MAYBE_MIN_SCORE:
            raise FitError(f"reject выше своей полосы у {row_id}")
        # Отказ обязан назвать причину из закрытого списка, а проходной вердикт
        # не может ссылаться на причину отказа: иначе «мягкая» рамка тихо
        # возвращается к строгой через свободный intent.
        if decision == "reject" and intent not in REJECT_INTENTS:
            raise FitError(f"отказ без причины из списка у {row_id}")
        if decision != "reject" and intent in REJECT_INTENTS:
            raise FitError(f"{decision} с причиной отказа у {row_id}")

        seen.add(row_id)
        out.append({
            "row_id": row_id,
            "decision": decision,
            "fit_score": score,
            "confidence": confidence,
            "intent": intent,
            "need_summary": clean(raw.get("need_summary")),
            "fit_reason": clean(raw.get("fit_reason")),
            "outreach_angle": clean(raw.get("outreach_angle")),
            "risks": [clean(item) for item in (raw.get("risks") or [])],
        })

    missing = set(str(item) for item in expected_ids) - seen
    if missing:
        raise FitError(f"модель пропустила {len(missing)} строк")
    return out
