"""Квалификация кандидата на личное сообщение.

Перенос LLM-проверки из прежнего контура
(`outreach/tgradar_contact_pipeline.py`, `build_llm_prompt` и рядом). Правила
отбора уже перенесены в `scripts/export_b140_candidates.py`; здесь второй слой,
который они не заменяют.

Зачем он нужен. Правила отвечают на вопрос «до этого человека мы дотянемся и
он писал по-русски». Они не отвечают на вопрос «ему вообще есть о чём с нами
говорить». Лид бизнеса 140 попадает в выборку по ключевикам и эмбеддингу, и
среди них полно людей, которые сами продают маркетинг, ищут работу или просто
упомянули слово «лиды». Написать такому — не касание, а спам.

Поэтому модель отвечает по каждому сообщению отдельно: `qualified`, `maybe`
или `reject`, с оценкой 0–100 и разбором намерения. Пороги согласованы со
шкалой: qualified 70–100, maybe 40–69, reject 0–39.

Что важно в переносе:

* решение выносится ПО ТЕКСТУ СООБЩЕНИЯ, а не по автору. Домысливать
  должность, компанию или отрасль запрещено прямым указанием в промпте;
* при нехватке данных модель обязана выбрать `maybe` или `reject`, а не
  натягивать `qualified`;
* `outreach_angle` — это не готовый текст письма, а короткий смысл будущего
  обращения. Текст пишет `first_touch`, и у него свой контракт;
* согласованность decision и fit_score проверяется на нашей стороне: модель,
  поставившая `qualified` с оценкой 50, отвергается целиком.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

PROMPT_VERSION = "tgradar_contact_fit_v2_russian_only"

QUALIFIED_MIN_SCORE = 70
MAYBE_MIN_SCORE = 40

VALID_DECISIONS = ("maybe", "qualified", "reject")
VALID_CONFIDENCE = ("high", "low", "medium")

#: Таксономия намерений перенесена целиком: по ней видно, за что именно
#: человека отвергли, и она же показывает, чем засорён пул.
VALID_INTENTS = (
    "business_need_other",
    "lead_generation_need",
    "market_demand_research",
    "marketing_or_sales_problem",
    "non_russian",
    "not_relevant",
    "personal_or_nonbusiness",
    "reputation_or_competitor_monitoring",
    "sales_monitoring_need",
    "seller_or_promotion",
    "spam",
    "unclear",
)


def clean(value: object) -> str:
    return str(value or "").strip()


def prompt_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Что видит модель. Только сообщение и его обстановка, без автора.

    Имя и username намеренно не передаются: решение должно опираться на то,
    что человек написал, а не на то, как он подписан.
    """
    out = []
    for row in rows:
        out.append({
            "row_id": str(row.get("btm_id") or row.get("row_id")),
            "source_category": clean(row.get("категория")),
            "message_text": clean(row.get("сообщение"))[:5000],
            "source_title": clean(row.get("источник"))[:500],
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


def build_prompt(rows: Sequence[Mapping[str, Any]], knowledge: str) -> str:
    return "\n".join([
        "Ты выполняешь LLM-проверку сигналов для процесса "
        "«Подготовить контакты из ТГ РАДАР».",
        "Это классификация данных, а не задача кодового агента. Не запускай "
        "команды, инструменты, веб и не используй внешние знания.",
        "Для каждого row_id независимо оцени, показывает ли САМО сообщение "
        "актуальную деловую потребность, для которой человеку потенциально "
        "подходит ТГ РАДАР.",
        "Обязательный языковой гейт: рассматривай только сообщения, основной "
        "связный текст которых написан по-русски.",
        "Полностью английские и преимущественно не-русские сообщения всегда "
        "reject с intent=non_russian, даже если бизнес-задача кажется "
        "релевантной.",
        "Допустимы отдельные общеупотребительные термины на латинице — "
        "Telegram, CRM, AI, B2B, SEO, SMM, ROAS, TikTok — если само сообщение "
        "сформулировано по-русски.",
        "ТГ РАДАР ищет публичные сигналы спроса, вопросы, рекомендации, "
        "сравнения, поиск подрядчика, товара или услуги, репутационные и "
        "конкурентные сигналы, затем отсеивает шум и передаёт контекст команде.",
        "Не считай человека целевым только потому, что он упомянул маркетинг, "
        "лиды, Telegram, продажи или сам рекламирует свои услуги.",
        "qualified: явная текущая бизнес-задача или боль, которую реально "
        "решает ТГ РАДАР, и сообщение даёт честное основание для личного "
        "обращения.",
        "maybe: применимость возможна, но бизнес-контекст или потребность "
        "неочевидны; нужен ручной просмотр.",
        "reject: личный или бытовой запрос, реклама автора, поиск работы, "
        "продажа услуг, спам, новость без потребности, нерелевантный вопрос "
        "или слишком слабый контекст.",
        f"Шкала согласована с decision: qualified={QUALIFIED_MIN_SCORE}-100, "
        f"maybe={MAYBE_MIN_SCORE}-{QUALIFIED_MIN_SCORE - 1}, "
        f"reject=0-{MAYBE_MIN_SCORE - 1}.",
        "need_summary кратко фиксирует потребность автора только из текста. "
        "outreach_angle — не готовое сообщение, а короткий безопасный смысл "
        "будущего обращения.",
        "Не придумывай должность, компанию, отрасль или намерение автора. "
        "При нехватке данных выбирай maybe или reject.",
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
        json.dumps(prompt_rows(rows), ensure_ascii=False, indent=2),
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
