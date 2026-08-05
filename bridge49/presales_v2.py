"""Промпт и контракт presales v2.

Перенесено дословно с релиза a55d259. Изъята одна функция —
``draft_presales_v2_reply``: она была входом из ``conversation.py`` и
единственным местом в модуле, которое читало чужие таблицы. Все девять имён,
которые модуль импортировал из ``presales``, использовались только внутри неё,
поэтому после изъятия зависимости от ``presales`` не осталось совсем.

Наш вход — ``mvp_inbound_decision.decide_inbound_reply``: он собирает контекст
сам и до базы не доходит.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import time
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Callable, ContextManager, Dict, Iterable, List, Optional

from .llm import clean_reply_style, violates_reply_guardrails
from .policy import normalize_text
from .truth_pack import (
    CustomerTruthPack,
    load_customer_truth_pack,
    validate_source_references,
)


PRESALES_V2_CONTRACT = "presales_v2"
PRESALES_V2_MAX_PRIMARY_ATTEMPTS = 2
PRESALES_V2_SUPERSESSION_POLL_SECONDS = 0.25
PRESALES_V2_TERMINATE_GRACE_SECONDS = 1.0
PRESALES_V2_AWAITING_TEST_SECTOR_STATE = "awaiting_test_sector"
PRESALES_V2_AWAITING_TEST_CONSENT_STATE = "awaiting_test_consent"
PRESALES_V2_BRAND_NAME = "ТГ РАДАР"
PRESALES_V2_CORE_DESCRIPTION = (
    "ТГ РАДАР — ИИ-сервис, который находит в мессенджерах и соцсетях людей, "
    "уже ищущих ваши товары или услуги, и передаёт их запросы вашей команде."
)
PRESALES_V2_BRAND_VARIANT_PATTERN = re.compile(
    r"(?iu)(?:\bTG(?:\s+|-)RADAR\b|\bТГ(?:\s+|-)РАДАР\b|\bТЕГРАДАР\b)"
)
PRESALES_V2_INTERNAL_STARTBOT_PATTERN = re.compile(
    r"(?iu)(?:\bstart(?:\s+|-)?bot\b|\bстарт(?:\s+|-)?бот\b)"
)
PRESALES_V2_ALLOWED_ACTIONS = {
    "reply",
    "reply_and_pause",
    "reply_and_handoff",
    "handoff",
    "ignore",
    "opt_out",
    "pause",
    "knowledge_gap",
}
PRESALES_V2_ALLOWED_ITEM_STATUSES = {
    "answered",
    "clarification_requested",
    "action_required",
    "needs_manager",
    "declined_out_of_scope",
    "not_applicable",
}
PRESALES_V2_ALLOWED_HANDOFF_KINDS = {
    "none",
    "free_test_access",
    "manager_action",
}
PRESALES_V2_ALLOWED_INTENTS = {
    "greeting",
    "positive",
    "faq_question",
    "pricing_question",
    "demo_question",
    "unknown_question",
    "neutral",
    "spam",
    "non_russian",
    "meaningless",
    "soft_negative",
    "hard_negative",
    "manager_handoff",
    "opt_out",
}
PRESALES_V2_FACTUAL_INTENTS = {
    "faq_question",
    "pricing_question",
    "demo_question",
}
PRESALES_V2_REQUIRED_FIELDS = {
    "action",
    "intent",
    "reply_text",
    "confidence",
    "risk_level",
    "next_state",
    "handoff_reason",
    "handoff_kind",
    "matched_direct_invite_sector_id",
    "knowledge_gap",
    "collected_fields_update",
    "coverage_complete",
    "turn_items",
    "reason",
}
PRESALES_V2_TURN_ITEM_FIELDS = {
    "item_id",
    "topic",
    "user_item",
    "user_evidence",
    "status",
    "answer_summary",
    "reply_evidence",
    "source_ids",
}
TOPIC_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "pricing",
        (
            r"\bтариф\w*\b",
            r"\bцен(?:а|ы|е|у|ой|ою|ам|ами|ах)\b",
            r"\bрасценк\w*\b",
            r"\bстоимост\w*\b",
            r"\bсколько(?:\s+\w+){0,3}\s+стоит\b",
            r"\bпрайс\w*\b",
            r"\bprices?\b",
            r"\bpricing\b",
        ),
    ),
    (
        "reviews",
        (
            r"\bотзыв\w*\b",
            r"\bмнени\w*\s+клиент\w*\b",
            r"\bяндекс\b",
            r"\breviews?\b",
        ),
    ),
    (
        "cases",
        (
            r"\bкейс\w*\b",
            r"\bрезультат\w*\s+клиент\w*\b",
            r"\bпример\w*\s+работ\w*\b",
            r"\broi\b",
            r"\bокупаемост\w*\b",
            r"\bcases?\b",
        ),
    ),
    (
        "signal_examples",
        (
            r"\bпример\w*\s+сигнал\w*\b",
            r"\bкак\s+выглядит\w*\s+сигнал\w*\b",
            r"\bпокаж\w*\s+сигнал\w*\b",
        ),
    ),
    (
        "demo",
        (
            r"\bдемо\w*\b",
            r"\bтест\w*\b",
            r"\bпопроб\w*\b",
            r"\bдоступ\w*\b",
        ),
    ),
    (
        "partner_program",
        (
            r"\bпартн[её]р\w*\b",
            r"\bреферал\w*\b",
        ),
    ),
    (
        "company",
        (
            r"\bкто\s+вы\b",
            r"\bваша\s+компания\b",
            r"\bо\s+компании\b",
        ),
    ),
    (
        "services",
        (
            r"\bчто\s+делаете\b",
            r"\bкакие\s+услуги\b",
            r"\bчто\s+умеет\b",
            r"\bкак\s+работает\b",
        ),
    ),
    (
        "platforms",
        (
            r"\bкак(?:ие|их)\s+(?:площадк|соцсет|социальн\w*\s+сет|мессенджер)\w*\b",
            r"\bгде\s+(?:вы\s+)?работаете\b",
            r"\bработаете\s+(?:ли\s+)?(?:в|с)\s+(?:telegram|телеграм|вк|вконтакте|max|макс)\b",
            r"\bподдерживаете\s+(?:telegram|телеграм|вк|вконтакте|max|макс)\b",
        ),
    ),
    (
        "source_scope",
        (
            r"\b(?:открыт|закрыт)\w*\s+(?:источник|чат|канал|групп|обсужден)\w*\b",
            r"\bисточник\w*\s+(?:открыт|закрыт)\w*\b",
            r"\bтолько\s+в\s+открыт\w*\b",
        ),
    ),
    (
        "implementation_promo",
        (
            r"\b(?:внедрен|настройк)\w*.{0,24}\b0\s*(?:₽|руб)",
            r"\b0\s*(?:₽|руб).{0,24}\b(?:внедрен|настройк)\w*",
            r"\b(?:акци|промо)\w*.{0,28}\b(?:внедрен|настройк)\w*",
        ),
    ),
    (
        "integrations",
        (
            r"\bapi\b",
            r"\bwebhooks?\b",
            r"\bвебхук\w*\b",
            r"\bинтеграц\w*\b",
        ),
    ),
    (
        "automation",
        (
            r"\bавтоматиз\w*\b",
            r"\bавтоматическ\w*\s+(?:общен|ответ|режим|обработк)\w*\b",
            r"\bручн\w*\s+(?:или|и)\s+автоматическ\w*\b",
        ),
    ),
    (
        "signal_classification",
        (
            r"\bклассификац\w*\s+сигнал\w*\b",
            r"\bклассифицир\w*\s+сигнал\w*\b",
            r"\bhot\s*/?\s*warm\s*/?\s*cold\s*/?\s*other\b",
            r"\bгоряч\w*,?\s+т[её]пл\w*,?\s+холодн\w*\b",
        ),
    ),
    (
        "fact_checking",
        (
            r"\bfact[\s-]?check\w*\b",
            r"\bфакт[\s-]?чекинг\w*\b",
            r"\bпровер\w*\s+(?:факт|информац)\w*\s+(?:для\s+)?сми\b",
        ),
    ),
    (
        "sensitive_advice",
        (
            r"\b(?:медицинск|юридическ|визов|таможенн|финансов)\w*\s+консультац\w*\b",
            r"\bда[её]те\s+(?:медицинск|юридическ|визов|таможенн|финансов)\w*\s+совет\w*\b",
        ),
    ),
    (
        "guarantees",
        (
            r"\bгарант\w*\b",
            r"\bсколько\s+(?:лидов|продаж|сигналов)\s+(?:будет|получим)\s+точно\b",
            r"\bокупит\w*\b",
        ),
    ),
    (
        "site_links",
        (
            r"\bгде\s+почитать\b",
            r"\bссылк\w*\b",
            r"\bсайт\w*\b",
            r"\bгде\s+посмотреть\b",
        ),
    ),
)


@dataclass(frozen=True)
class PresalesV2ExternalResult:
    raw: dict[str, object] | None
    reason: str


class PresalesV2GenerationSuperseded(RuntimeError):
    """The current draft became obsolete after a newer inbound was persisted."""

    def __init__(self, newer_event_id: str) -> None:
        super().__init__("presales_v2_generation_superseded")
        self.newer_event_id = newer_event_id


@dataclass(frozen=True)
class PresalesV2Normalized:
    ok: bool
    action: str
    decision: str
    intent: str
    reply_text: str
    confidence: float
    risk_level: str
    next_state: str
    handoff_required: bool
    handoff_reason: str
    handoff_kind: str
    matched_direct_invite_sector_id: str
    knowledge_gap: str
    collected_fields_update: Dict[str, str]
    turn_items: List[Dict[str, object]]
    used_source_ids: List[str]
    invalid_source_ids: List[str]
    coverage_complete: bool
    reason: str
    technical_failure: bool
    validation_warnings: List[str]


def build_presales_v2_prompt(
    *,
    inbound_text: str,
    context: Dict[str, object],
    pack: CustomerTruthPack,
    required_topics: List[str],
    reasoning_effort: str,
) -> dict[str, object]:
    return {
        "contract_version": PRESALES_V2_CONTRACT,
        "task": (
            "Ответить на текущий ход presales-диалога ТГ РАДАР, используя полный "
            "контекст разговора и весь компактный канонический каталог фактов."
        ),
        "reasoning_effort": reasoning_effort,
        "system": (
            "Ты единый полно-контекстный presales-ассистент, а не intent-router и "
            "не набор сценарных веток. Сначала пойми весь текущий ход и историю, "
            "затем дай один цельный ответ. Разбери ВСЕ вопросы, просьбы, возражения "
            "и ограничения текущего хода. Если пользователь задал несколько "
            "вопросов вместе, ответь на каждый; наличие сложного пункта не отменяет "
            "ответы на остальные. Не говори, что точной информации нет, если она "
            "есть хотя бы в одном факте truth pack. "
            "Во всех собственных клиентских формулировках название сервиса пиши "
            "ровно «ТГ РАДАР» кириллицей и заглавными буквами. Не используй "
            "латинские, смешанные, слитные или некапитальные варианты названия. "
            "URL, username, source_id, технические идентификаторы и дословный "
            "user_evidence не переписывай. "
            "На общий вопрос по смыслу «что это за сервис?», «что вы "
            "предлагаете?» или просьбу кратко рассказать о продукте начинай "
            "клиентский ответ с точной основной формулировки: «"
            f"{PRESALES_V2_CORE_DESCRIPTION}"
            "» Затем естественно добавь только релевантные этому диалогу "
            "пояснения. Не заменяй эту формулировку внутренними терминами "
            "«проявленный спрос» или «рыночные сигналы». "
            "Для каждого пункта укажи в turn_items статус и source_ids. "
            "Добавь user_evidence — короткую "
            "ТОЧНУЮ цитату из "
            "current_turn_text, подтверждающую этот пункт. Для каждого пункта, который "
            "должен быть виден клиенту, добавь reply_evidence — короткую ТОЧНУЮ цитату из "
            "reply_text, где этот пункт реально обработан. Нельзя считать пункт "
            "answered только во внутреннем summary. Truth pack — источник фактов, "
            "а не готовый клиентский текст: не копируй его списки и канцелярские "
            "конструкции дословно. Сначала пойми, что человек хочет решить, затем "
            "переведи релевантные факты в 2–5 живых предложений языком нормального "
            "менеджера в Telegram. Начинай с прямого ответа или понятной пользы, "
            "затем добавь не больше двух-трёх действительно важных деталей. "
            "Соблюдай запрошенную глубину: ссылка не заменяет содержательный ответ, "
            "а перечень сфер кейсов не заменяет результаты. При общем вопросе "
            "«сколько стоит?», «какая цена?» или «какие тарифы?» не пересказывай "
            "всю таблицу GO / PLUS / PRO. Скажи, что подписка начинается от "
            "29 000 ₽ в месяц, коротко назови ценность продукта и расширенных "
            "вариантов, затем дай https://tgradar.ru/price. Полную сетку цен, "
            "названия тарифов и лимиты перечисляй только если человек явно просит "
            "сравнить варианты, расписать все цены, отличия или лимиты либо "
            "спрашивает о конкретном тарифе. "
            "Когда объясняешь, что получает клиент, говори через результат: он "
            "видит, что именно ищет человек, где появился запрос, почему он важен "
            "и с чего можно начать разговор. Не перечисляй по умолчанию все поля, "
            "каналы доставки и категории HOT / WARM / COLD / OTHER; называй их "
            "только при прямом вопросе о составе сигнала, интеграциях или "
            "классификации. "
            "Если спрашивают результаты кейсов, приведи хотя бы один точный "
            "подтверждённый публичный или презентационный результат из truth pack; "
            "не называй презентационный пример опубликованным на сайте. Если кейса "
            "по сфере человека нет, скажи об этом и дай ближайший подтверждённый "
            "пример без переноса его результата на эту сферу. Клиенту внутренние "
            "source_ids не показывай. Не придумывай факты, гарантии, индивидуальные "
            "условия, ROI, фиксированные лиды или продажи. Подтверждённые "
            "кейсовые цифры и ROI из truth pack называй прямо и естественно: "
            "например, «в кейсе по авто из-за границы получили 6 сделок за "
            "месяц». Не переноси результат кейса на нового клиента и не обещай "
            "ему такую же цифру. В обычном ответе о кейсах без прямого вопроса "
            "о гарантиях или прогнозе не добавляй защитные оговорки вроде "
            "«исторический результат», «прошлый проект», «не прогноз», "
            "«не обещание» или «не гарантия». Если человек прямо спрашивает, "
            "гарантирован ли результат или какой результат будет у него, тогда "
            "коротко и ясно объясни границу. "
            "Для большинства проектов используются Telegram, ВКонтакте и Max, но "
            "конечные площадки и источники подбираются под конкретный проект. Можно "
            "подтвердить работу с открытыми и законно доступными закрытыми "
            "источниками, не обещая доступ к любому закрытому сообществу и не "
            "раскрывая внутренний список источников. "
            "Различай бессрочный демо-бот на сайте и бесплатный тест системы до "
            "трёх дней. Промо внедрения 0 ₽ не поднимай в обычном ответе о тарифах; "
            "при прямом вопросе объясни, что оно может действовать во время теста "
            "или использования демо-бота либо по согласованию, а финальное решение "
            "принимает менеджер. "
            "Общий вопрос об API, webhook, CRM-интеграциях или автоматизации "
            "закрывай сам: они доступны на некоторых тарифах или этапах. Handoff "
            "нужен для конкретной схемы, подключения или запуска. Базовая "
            "классификация сигналов — HOT, WARM, COLD, OTHER; в отдельных проектах "
            "она может расширяться. Действующего клиента нельзя сделать новым "
            "рефералом; бывшего неактивного клиента можно закрепить после проверки, "
            "если партнёр реанимировал подписку. ТГ РАДАР не оказывает sensitive-"
            "консультации. Продукт fact-checking для СМИ можно подтвердить, но его "
            "интеграцию и workflow уточняет менеджер, а окончательное редакционное "
            "решение остаётся за клиентом. Конфликтный/менеджерский пункт можно "
            "передать менеджеру, одновременно ответив на остальные известные пункты "
            "через action=reply_and_handoff. Handoff нужен для явного запроса "
            "человека/созвона, договора, счёта, оплаты, индивидуальной цены, выдачи "
            "доступа или фактического запуска. Обычные вопросы о тарифах, кейсах, "
            "отзывах, продукте, тесте и ссылках отвечай самостоятельно по truth pack. "
            "Если человеку нужен менеджер и уместно коротко подтвердить передачу, "
            "используй reply_and_handoff; чистый handoff оставляй только для случая, "
            "когда клиентский текст действительно неуместен или небезопасен. Обычный "
            "вежливый отказ не оставляй без ответа: один раз коротко прими отказ без "
            "нового предложения и используй action=reply_and_pause. Без текста "
            "остаются явный opt-out, подтверждённый рекламный спам и отдельное "
            "входящее без содержательного русского текста. "
            "required_topics — только диагностические подсказки, а не закрытый "
            "набор веток и не приказ обсуждать случайно совпавшее слово. Сам "
            "определи все смысловые пункты текущего хода. Для подтверждённого спама или "
            "opt-out опиши весь текущий ход одним turn_item и не выдумывай отдельные "
            "вопросы о продукте из рекламного текста. "
            "Классификация подтверждённого рекламного спама имеет приоритет над "
            "языком: повторяющееся бот-сообщение про монеты, airdrop, награду, "
            "уведомления, подписку или другую массовую акцию с командой либо CTA "
            "остается action=ignore, intent=spam и с пустым reply_text, даже если "
            "оно написано не по-русски. Не отвечай на такой спам просьбой перейти "
            "на русский. "
            "Отдельное нерусское сообщение без содержательного русского текста "
            "также классифицируй как action=ignore, intent=spam с пустым reply_text; "
            "не проси перейти на русский. "
            "Перед тем как впервые предлагать бесплатный тест, узнай сферу самого "
            "человека. conversation_context.free_test_sector_known=true означает, "
            "что сфера уже надёжно известна из прямого контекста или точной ветки "
            "источника: повторно её не спрашивай. Если "
            "conversation_context.free_test_sector_known=false и человек ещё "
            "не назвал сферу в текущем ходе или прямой истории, обычный "
            "содержательный ответ заверши "
            "одним коротким вопросом о сфере или направлении проекта, не вопросом "
            "о согласии на тест; верни next_state=awaiting_test_sector. Можно кратко "
            "объяснить, что сфера нужна, чтобы показать релевантный бесплатный тест. "
            "Если человек назвал сферу в текущем ходе или однозначно называл её "
            "ранее в прямой истории, сохрани её в collected_fields_update.sector "
            "и поставь sector_status=user_confirmed. Затем предложи бесплатный тест "
            "уже применительно к этой сфере и верни "
            "next_state=awaiting_test_consent. "
            "Если человек сам просит тест или уже согласился на него, но сфера всё "
            "ещё неизвестна, не создавай handoff и не обещай менеджера: сохрани "
            "смысл согласия через next_state=awaiting_test_sector, верни action=reply, "
            "handoff_kind=none и задай только один короткий вопрос о сфере. "
            "conversation_state=awaiting_test_sector означает, что согласие на тест "
            "уже получено и повторно спрашивать его нельзя. Когда человек после "
            "этого однозначно называет сферу, сразу запускай free_test_access: для "
            "точного automatic-соответствия через автоматического бота для "
            "бесплатного теста, для другой подтверждённой "
            "сферы через менеджера. Если ответ о сфере неоднозначен, задай одно "
            "уточнение и сохрани next_state=awaiting_test_sector. "
            "Если conversation_context.free_test_access_branch.branch=automatic "
            "либо явно подтверждённая сфера самого человека точно совпадает с одним "
            "элементом automatic_free_test_sector_catalog, явное согласие на "
            "бесплатный тест, демо или доступ возвращай как semantic "
            "action=reply_and_handoff, чтобы deterministic router создал одноразовую "
            "ссылку на бота для бесплатного теста. В matched_direct_invite_sector_id "
            "при этом верни exact outreach_sector_id. Внутреннее техническое "
            "название этого бота никогда не показывай клиенту и не используй в "
            "reply_text; называй его только «бот для бесплатного теста». В "
            "клиентском reply_text не обещай "
            "менеджера: подтверди, что заявка принята и одноразовая ссылка на бота "
            "для бесплатного теста придёт отдельно. Менеджера в automatic-ветке "
            "упоминай только при прямом запросе живого человека, созвона или другого "
            "manager-only действия. Если точного automatic-соответствия нет, "
            "используй обычную менеджерскую ветку. "
            "Для каждого handoff обязательно верни структурированный handoff_kind. "
            "Используй free_test_access только когда по смыслу текущего хода и "
            "прямой истории человек просит, принимает или хочет фактически запустить "
            "бесплатный тест, демо системы либо получить доступ к нему. Для просьбы "
            "о менеджере, созвоне, договоре, счёте, оплате, индивидуальной настройке "
            "или другого действия человека используй manager_action. Во всех "
            "действиях без handoff используй none. Не определяй handoff_kind поиском "
            "слов: учитывай объект согласия и весь прямой диалог. "
            "Для turn_item, который запускает free_test_access, используй "
            "status=action_required; для manager_action используй "
            "status=needs_manager. Не помечай фактический запуск теста как answered, "
            "пока deterministic router ещё не создал доступ. "
            "automatic_free_test_sector_catalog — исчерпывающий allowlist сфер, "
            "для которых при handoff_kind=free_test_access доступ можно выдать "
            "автоматически. В matched_direct_invite_sector_id верни точный "
            "outreach_sector_id из этого каталога только если сфера самого человека "
            "явно подтверждена его сообщениями в прямом диалоге либо уже дана в "
            "free_test_access_branch. Если подтверждённая сфера другая, верни пустую "
            "строку: тогда заявку обработает менеджер. Если сферы пока нет или "
            "соответствие неоднозначно, handoff запрещён до уточнения. Никогда не "
            "подбирай этот id из слабого публичного фона. "
            "Ответ короткий, естественный, по-русски, без длинного тире, эмодзи, "
            "канцелярита и рекламного пафоса. Не пересказывай без пользы только что "
            "сказанное пользователем и выбирай естественный глагол для вложения, а "
            "не формулировки вроде «прочитать анимацию». Ответ может быть "
            "структурирован, если вопросов несколько. Любой обычный action=reply "
            "сначала отвечает по существу, а затем заканчивается ровно одним "
            "естественным следующим вопросом, ведущим к персонализированному "
            "бесплатному тесту или демо системы. Если сфера неизвестна, этим "
            "единственным вопросом должен быть вопрос о сфере; если известна, "
            "задай прямой вопрос о тесте применительно к ней. Отдельный второй CTA "
            "запрещён. Не используй пустые "
            "продолжения вроде «Что-нибудь ещё?» или «Чем ещё помочь?». Если "
            "conversation_context.conversation_state=awaiting_test_consent и сфера "
            "известна, после ответа на новый информационный вопрос снова задай "
            "прямой вопрос о бесплатном тесте: не считай CTA из более раннего хода "
            "вопросом текущего reply_text. Если это старый awaiting_test_consent, "
            "но free_test_sector_known=false, сначала запроси сферу и переключись "
            "в awaiting_test_sector. Исключения должны использовать соответствующий другой "
            "action: reply_and_pause для вежливого завершения без вопроса, "
            "reply_and_handoff когда следующий шаг уже запущен, либо action без "
            "клиентского текста для opt-out и спама. Для любого action с клиентским "
            "reply_text всегда ставь risk_level=low: medium/high "
            "разрешены только когда reply_text нет. Названия topic делай короткими "
            "и стабильными; совпадение с required_topics желательно для диагностики, "
            "но смысловое покрытие важнее названия. Все дополнительные пункты "
            "текущего хода тоже перечисляй. "
            "recent_public_chat_outreach — только слабый фон уровня аккаунта: он не "
            "доказывает, из какого сообщения пришёл конкретный человек. Не называй "
            "и не записывай сектор, услугу или задачу из этого фона, пока сам человек "
            "не подтвердил их в текущем ходе или прямой истории диалога. Для "
            "неоднозначных слов вроде «этим» задай один нейтральный уточняющий вопрос. "
            "Короткое согласие наследует ровно тот объект, который был предложен в "
            "непосредственно предыдущем видимом сообщении, и не расширяет его. "
            "Сначала определи этот объект по смыслу. Если человеку прямо предложили "
            "бесплатный тест системы, продуктовый демо-показ, выдачу доступа, "
            "менеджера, созвон или фактический запуск, то «да», «давайте», "
            "«покажите», «интересно, скидывайте» и другое однозначное согласие уже "
            "являются согласием на этот следующий шаг. При известной сфере сразу "
            "используй reply_and_handoff; при неизвестной сохрани согласие состоянием "
            "awaiting_test_sector и спроси только сферу. Не спрашивай разрешение "
            "повторно. То, что тест или "
            "демо технически организует менеджер, не требует отдельной фразы "
            "«согласен на менеджера», если человек уже принял явно предложенный "
            "тест, демо или доступ. Если же ранее обещали только прислать примеры, "
            "кейс, отзыв, видео, ссылку или другой информационный материал, согласие "
            "разрешает только выдать этот материал и не создаёт handoff. При этом "
            "персонализированное предложение бесплатно показать сам инструмент, "
            "его работу или примеры, которые он находит «по вашему направлению», "
            "считай продуктовым демо-показом, а не обещанием статического файла: "
            "явное согласие на такой показ создаёт reply_and_handoff. Статическим "
            "материалом считай только прямое обещание прислать в чат конкретную "
            "ссылку, кейс, видео, отзыв или готовый пример без показа системы. Согласие "
            "после общего discovery-вопроса или вопроса об интересе также не "
            "создаёт handoff: продолжи разговор и сформулируй один точный CTA. "
            "Не реконструируй содержание, географию или условия прежнего сообщения "
            "в публичном чате из отраслевой базы или recent_public_chat_outreach: "
            "точные детали можно повторять только из прямой истории этого диалога. "
            "Не утверждай, что подтверждённого заказа нет, только потому что человек "
            "пишет аккаунту chat_sender: это допустимо, когда он сам явно отвечает "
            "на наш публичный запрос либо отсутствие заказа подтверждено прямой "
            "историей; иначе нейтрально уточни причину обращения. Когда по этим "
            "правилам нужно сообщить об отсутствии подтверждённого заказа, используй "
            "точную прозрачную формулировку «Скажу честно: подтверждённого заказа "
            "сейчас нет». Не начинай переход с сухого или резкого «у нас нет "
            "заказа». Самостоятельная "
            "прямая формулировка «вам нужна наша услуга?», «есть потребность?», "
            "«планируете покупать?» уже является явным вопросом о нашей текущей "
            "потребности: ответь на него по существу, что подтверждённого заказа или "
            "потребности сейчас нет, начав с «Скажу честно», и только затем при "
            "уместности уточняй контекст "
            "или переходи к ТГ РАДАР. Не уклоняйся от такого вопроса встречным "
            "уточнением. Самостоятельная "
            "массовая рекламная подача с ценами, гарантиями и предложением своих "
            "услуг без явной связи с нашим запросом — подтверждённый рекламный спам "
            "и остаётся без ответа; явный содержательный отклик на наш публичный "
            "запрос обрабатывай по правилам chat_sender. Язык текущего входящего "
            "проверяй до наследования согласия: отдельное нерусское сообщение без "
            "содержательного русского текста классифицируй как spam/ignore без "
            "ответа и не превращай в handoff даже после русского CTA. "
            "Truth pack — канонический, но не обязательно исчерпывающий реестр всех "
            "когда-либо существовавших кейсов. Не заявляй «кейса в этой сфере нет» "
            "или «это ближайший кейс», если источник прямо не подтверждает полноту "
            "реестра и такое сравнение. Безопасная формулировка: «из доступных "
            "подтверждённых примеров могу привести…», с явным указанием другой "
            "сферы и без переноса результата. "
            "Не расширяй ограничение контакта. Просьба не использовать контакт для "
            "других рассылок запрещает другие рассылки, но не отменяет уже "
            "запрошенный ответ, тест или демо в текущем диалоге: коротко подтверди "
            "границу видимым ответом и продолжи только согласованный текущий "
            "контекст. Полный запрет писать или требование прекратить любое общение "
            "обрабатывай как настоящий opt-out по общей политике."
        ),
        "current_turn_text": inbound_text,
        "required_topics": required_topics,
        "conversation_context": context,
        "truth_pack": pack.as_prompt_payload(),
        "output_schema": {
            "action": (
                "reply|reply_and_pause|reply_and_handoff|handoff|ignore|opt_out|"
                "pause|knowledge_gap"
            ),
            "intent": (
                "greeting|positive|faq_question|pricing_question|demo_question|"
                "unknown_question|neutral|spam|non_russian|meaningless|soft_negative|"
                "hard_negative|manager_handoff|opt_out"
            ),
            "reply_text": (
                "string; empty only when action is not reply/reply_and_pause/"
                "reply_and_handoff"
            ),
            "confidence": "number 0..1",
            "risk_level": "low|medium|high",
            "next_state": "string",
            "handoff_reason": "string",
            "handoff_kind": "none|free_test_access|manager_action",
            "matched_direct_invite_sector_id": (
                "exact outreach_sector_id from conversation_context."
                "automatic_free_test_sector_catalog or empty string"
            ),
            # Три поля сопоставления сферы. Их держит `PRESALES_V2_REQUIRED_FIELDS`,
            # и отсутствие любого из них валит весь ход в technical_failure —
            # а он молчит по замыслу. Значит список полей здесь и там обязан
            # совпадать буква в букву: инструкции в прозе выше модель читает,
            # но форму ответа копирует отсюда. Расхождение стоило нам первого
            # же живого разговора после переезда 06.08: человек согласился на
            # тест и не получил ничего.
            "client_sector_text": (
                "the person's own wording of their sector, verbatim, no "
                "paraphrase; empty string if they never named it"
            ),
            "canonical_sector_id": (
                "exact canonical_sector_id from conversation_context."
                "sector_matching_catalog or empty string"
            ),
            "sector_confidence": "exact|likely|ambiguous|none or empty string",
            "knowledge_gap": "string",
            "collected_fields_update": "object<string,string>",
            "coverage_complete": "boolean",
            "turn_items": [
                {
                    "item_id": "q1, q2, ... unique",
                    "topic": (
                        "short stable topic id; preferably reuse a matching "
                        "required_topics hint"
                    ),
                    "user_item": "short paraphrase of one user question/request",
                    "user_evidence": (
                        "short exact quote copied from current_turn_text that proves "
                        "this item exists"
                    ),
                    "status": (
                        "answered|clarification_requested|action_required|needs_manager|"
                        "declined_out_of_scope|not_applicable"
                    ),
                    "answer_summary": "what the reply did for this item",
                    "reply_evidence": (
                        "short exact quote copied from reply_text; empty only when "
                        "this action intentionally has no client-visible reply"
                    ),
                    "source_ids": [
                        "exact source_id values from truth_pack.source_catalog"
                    ],
                }
            ],
            "reason": "short internal reason",
        },
    }


def call_presales_v2_llm(
    payload: dict[str, object],
    *,
    command: Optional[str] = None,
    timeout_seconds: float = 240,
    supersession_check: Optional[Callable[[], str]] = None,
) -> PresalesV2ExternalResult:
    command_value = command or os.environ.get("OUTREACH_LLM_COMMAND")
    if not command_value:
        return PresalesV2ExternalResult(None, "presales_v2_llm_command_missing")
    newer_event_id = checked_superseding_event_id(supersession_check)
    if newer_event_id:
        raise PresalesV2GenerationSuperseded(newer_event_id)
    timeout_value = max(1.0, float(timeout_seconds))
    command_parts = shlex.split(command_value)
    try:
        process = subprocess.Popen(
            command_parts,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except OSError as exc:
        return PresalesV2ExternalResult(
            None,
            f"presales_v2_llm_command_error:{exc.__class__.__name__}",
        )
    deadline = time.monotonic() + timeout_value
    input_text: Optional[str] = json.dumps(payload, ensure_ascii=False)
    stdout = ""
    stderr = ""
    while True:
        newer_event_id = checked_superseding_event_id(supersession_check)
        if newer_event_id:
            terminate_external_process_group(process)
            process.communicate()
            close_external_process_pipes(process)
            raise PresalesV2GenerationSuperseded(newer_event_id)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate_external_process_group(process)
            process.communicate()
            close_external_process_pipes(process)
            return PresalesV2ExternalResult(
                None,
                "presales_v2_llm_command_error:TimeoutExpired",
            )
        try:
            stdout, stderr = process.communicate(
                input=input_text,
                timeout=min(PRESALES_V2_SUPERSESSION_POLL_SECONDS, remaining),
            )
            break
        except subprocess.TimeoutExpired:
            input_text = None
            continue
    close_external_process_pipes(process)
    completed = subprocess.CompletedProcess(
        command_parts,
        int(process.returncode or 0),
        stdout,
        stderr,
    )
    if completed.returncode != 0:
        return PresalesV2ExternalResult(
            None,
            f"presales_v2_llm_command_failed:{completed.returncode}",
        )
    try:
        raw = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return PresalesV2ExternalResult(None, "presales_v2_llm_invalid_json")
    if not isinstance(raw, dict):
        return PresalesV2ExternalResult(None, "presales_v2_llm_root_not_object")
    return PresalesV2ExternalResult(raw, "")


def checked_superseding_event_id(
    supersession_check: Optional[Callable[[], str]],
) -> str:
    if supersession_check is None:
        return ""
    try:
        return str(supersession_check() or "")
    except Exception:
        # Cancellation is an optimization. The durable publication fence remains
        # authoritative if a transient SQLite read cannot be completed here.
        return ""


def terminate_external_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=PRESALES_V2_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            process.kill()
        except OSError:
            return
    try:
        process.wait(timeout=PRESALES_V2_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return


def close_external_process_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def presales_v2_repair_instruction(reason: str) -> str:
    sector_instruction = ""
    if reason == "presales_v2_free_test_requires_confirmed_sector":
        sector_instruction = (
            " Сфера человека ещё не подтверждена. Не создавай handoff и не обещай "
            "менеджера. Верни action=reply, handoff_kind=none, пустой "
            "matched_direct_invite_sector_id, next_state=awaiting_test_sector и "
            "задай один короткий вопрос только о сфере; уже выраженный интерес к "
            "тесту считается сохранённым."
        )
    elif reason == "presales_v2_noncanonical_brand_spelling":
        sector_instruction = (
            " Во всём клиентском reply_text замени название сервиса на точное "
            "написание «ТГ РАДАР». Не меняй URL, username, source_id, технические "
            "идентификаторы и дословный user_evidence."
        )
    elif reason == "presales_v2_internal_free_test_bot_name_exposed":
        sector_instruction = (
            " Удали из клиентского reply_text внутреннее техническое название бота "
            "и назови его только «бот для бесплатного теста». Не меняй URL, "
            "username, source_id, технические идентификаторы и дословный user_evidence."
        )
    return (
        "Предыдущий результат не прошёл обязательную hard-валидацию. "
        f"Точная причина: {reason}. Исправь только эту причину, сохрани "
        "содержательный ответ и верни полный JSON-контракт. Используй точные "
        "цитаты current_turn_text в user_evidence, точные цитаты финального "
        "reply_text в reply_evidence и только source_id из "
        f"truth_pack.source_catalog.{sector_instruction}"
    )


def normalize_presales_v2_result(
    raw: dict[str, object],
    *,
    pack: CustomerTruthPack,
    required_topics: Iterable[str],
    inbound_text: str,
    allowed_direct_invite_sector_ids: Iterable[str] = (),
    required_direct_invite_sector_id: str = "",
    confirmed_sector_available: bool = False,
) -> PresalesV2Normalized:
    wrapper_reason = str(raw.get("reason") or "").strip()
    if wrapper_reason.startswith("codex_wrapper_"):
        return technical_failure_result(wrapper_reason)
    raw_fields = set(raw)
    missing_fields = sorted(PRESALES_V2_REQUIRED_FIELDS.difference(raw_fields))
    unknown_fields = sorted(raw_fields.difference(PRESALES_V2_REQUIRED_FIELDS))
    if missing_fields:
        return technical_failure_result(
            "presales_v2_schema_missing_fields:" + ",".join(missing_fields)
        )
    if unknown_fields:
        return technical_failure_result(
            "presales_v2_schema_unknown_fields:" + ",".join(unknown_fields)
        )
    action = str(raw.get("action") or "").strip()
    if action not in PRESALES_V2_ALLOWED_ACTIONS:
        return technical_failure_result("presales_v2_invalid_action")
    intent = str(raw.get("intent") or "").strip()
    if intent not in PRESALES_V2_ALLOWED_INTENTS:
        return technical_failure_result("presales_v2_invalid_intent")
    confidence = strict_number(raw.get("confidence"))
    if confidence is None:
        return technical_failure_result("presales_v2_invalid_confidence")
    risk_level = str(raw.get("risk_level") or "").strip()
    if risk_level not in {"low", "medium", "high"}:
        return technical_failure_result("presales_v2_invalid_risk_level")
    for field_name in (
        "reply_text",
        "next_state",
        "handoff_reason",
        "handoff_kind",
        "matched_direct_invite_sector_id",
        "knowledge_gap",
        "reason",
    ):
        if not isinstance(raw.get(field_name), str):
            return technical_failure_result(
                f"presales_v2_schema_invalid_string:{field_name}"
            )
    collected_raw = raw.get("collected_fields_update")
    if not isinstance(collected_raw, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in collected_raw.items()
    ):
        return technical_failure_result(
            "presales_v2_schema_invalid_collected_fields"
        )
    coverage_value = raw.get("coverage_complete")
    if not isinstance(coverage_value, bool):
        return technical_failure_result("presales_v2_invalid_coverage_flag")
    turn_items = normalize_turn_items(raw.get("turn_items"))
    if turn_items is None:
        return technical_failure_result("presales_v2_invalid_turn_items")
    valid_source_ids = pack.source_ids
    used_source_ids: list[str] = []
    invalid_source_ids: list[str] = []
    for item in turn_items:
        user_evidence = str(item["user_evidence"])
        if not user_evidence or user_evidence not in inbound_text:
            return technical_failure_result(
                "presales_v2_item_without_user_evidence:"
                + str(item["item_id"])
            )
        source_ids = list(item["source_ids"])
        for source_id in source_ids:
            target = (
                used_source_ids if source_id in valid_source_ids else invalid_source_ids
            )
            if source_id not in target:
                target.append(source_id)
        if (
            item["status"] == "answered"
            and intent in PRESALES_V2_FACTUAL_INTENTS
            and not source_ids
        ):
            return technical_failure_result("presales_v2_answer_without_source")
    if invalid_source_ids:
        result = technical_failure_result("presales_v2_unknown_source_id")
        return PresalesV2Normalized(
            **{
                **result.__dict__,
                "invalid_source_ids": invalid_source_ids,
            }
        )
    required = list(dict.fromkeys(str(topic) for topic in required_topics if str(topic)))
    covered_topics = {
        str(item["topic"])
        for item in turn_items
        if item["status"]
        in {
            "answered",
            "clarification_requested",
            "action_required",
            "needs_manager",
            "declined_out_of_scope",
            "not_applicable",
        }
    }
    addressed_items = [
        item
        for item in turn_items
        if item["status"]
        in {
            "answered",
            "clarification_requested",
            "action_required",
            "needs_manager",
            "declined_out_of_scope",
            "not_applicable",
        }
    ]
    missing_topics = [
        topic
        for topic in required
        if topic not in covered_topics
        and not (topic == "general" and addressed_items)
    ]
    if not coverage_value:
        return technical_failure_result("presales_v2_coverage_complete_false")
    if not turn_items:
        return technical_failure_result("presales_v2_no_turn_items")
    reply_text = clean_reply_style(str(raw.get("reply_text") or ""))
    reply_action = action in {"reply", "reply_and_pause", "reply_and_handoff"}
    if reply_action and (not reply_text or violates_reply_guardrails(reply_text)):
        return technical_failure_result("presales_v2_reply_guardrail_failed")
    if reply_action and contains_noncanonical_brand_spelling(reply_text):
        return technical_failure_result(
            "presales_v2_noncanonical_brand_spelling"
        )
    if reply_action and contains_internal_startbot_name(reply_text):
        return technical_failure_result(
            "presales_v2_internal_free_test_bot_name_exposed"
        )
    if not reply_action and reply_text:
        return technical_failure_result("presales_v2_unexpected_reply_text")
    if action == "reply" and question_mark_count(reply_text) != 1:
        return technical_failure_result(
            "presales_v2_reply_requires_one_next_step_question"
        )
    if reply_action:
        for item in turn_items:
            if item["status"] == "not_applicable":
                continue
            evidence = str(item.get("reply_evidence") or "").strip()
            final_evidence = clean_reply_style(evidence)
            if not final_evidence or final_evidence not in reply_text:
                return technical_failure_result(
                    "presales_v2_item_without_visible_reply_evidence:"
                    + str(item["item_id"])
                )
    if action == "ignore" and intent != "spam":
        return technical_failure_result("presales_v2_ignore_requires_spam")
    if action == "opt_out" and intent != "opt_out":
        return technical_failure_result("presales_v2_opt_out_intent_mismatch")
    if reply_action and risk_level != "low":
        return technical_failure_result("presales_v2_customer_reply_not_low_risk")
    if action == "reply" and any(
        item["status"] in {"action_required", "needs_manager"}
        for item in turn_items
    ):
        return technical_failure_result("presales_v2_manager_item_without_handoff")
    if intent == "soft_negative" and action != "reply_and_pause":
        return technical_failure_result(
            "presales_v2_soft_negative_requires_reply_and_pause"
        )

    decision = {
        "reply": "auto_reply",
        "reply_and_pause": "pause_conversation",
        "reply_and_handoff": "reply_and_handoff",
        "handoff": "manager_handoff",
        "ignore": "ignore",
        "opt_out": "opt_out",
        "pause": "pause_conversation",
        "knowledge_gap": "knowledge_gap",
    }[action]
    handoff_required = action in {"reply_and_handoff", "handoff"}
    collected = clean_string_dict(raw.get("collected_fields_update"))
    handoff_reason = str(raw.get("handoff_reason") or "").strip()
    handoff_kind = str(raw.get("handoff_kind") or "").strip()
    matched_direct_invite_sector_id = str(
        raw.get("matched_direct_invite_sector_id") or ""
    ).strip()
    knowledge_gap = str(raw.get("knowledge_gap") or "").strip()
    reason = str(raw.get("reason") or "").strip()
    if handoff_kind not in PRESALES_V2_ALLOWED_HANDOFF_KINDS:
        return technical_failure_result("presales_v2_invalid_handoff_kind")
    if handoff_required and handoff_kind == "none":
        return technical_failure_result("presales_v2_handoff_kind_required")
    if not handoff_required and handoff_kind != "none":
        return technical_failure_result("presales_v2_unexpected_handoff_kind")
    if handoff_kind == "free_test_access" and not any(
        item["status"] == "action_required" for item in turn_items
    ):
        return technical_failure_result(
            "presales_v2_free_test_without_action_item"
        )
    if handoff_kind == "manager_action" and not any(
        item["status"] == "needs_manager" for item in turn_items
    ):
        return technical_failure_result(
            "presales_v2_manager_handoff_without_manager_item"
        )
    if (
        matched_direct_invite_sector_id
        and handoff_kind != "free_test_access"
    ):
        return technical_failure_result(
            "presales_v2_sector_match_without_free_test_access"
        )
    allowed_sector_ids = {
        str(item or "").strip()
        for item in allowed_direct_invite_sector_ids
        if str(item or "").strip()
    }
    if (
        matched_direct_invite_sector_id
        and matched_direct_invite_sector_id not in allowed_sector_ids
    ):
        return technical_failure_result(
            "presales_v2_direct_invite_sector_not_allowlisted"
        )
    required_sector_id = str(required_direct_invite_sector_id or "").strip()
    turn_sector_confirmed = bool(str(collected.get("sector") or "").strip())
    sector_confirmed = bool(
        confirmed_sector_available
        or required_sector_id
        or turn_sector_confirmed
    )
    if handoff_kind == "free_test_access" and not sector_confirmed:
        return technical_failure_result(
            "presales_v2_free_test_requires_confirmed_sector"
        )
    if (
        handoff_kind == "free_test_access"
        and required_sector_id
        and matched_direct_invite_sector_id != required_sector_id
    ):
        return technical_failure_result(
            "presales_v2_known_direct_invite_sector_not_selected"
        )
    if (
        handoff_kind == "free_test_access"
        and matched_direct_invite_sector_id
        and re.search(r"\bменеджер\w*\b", reply_text, flags=re.IGNORECASE)
    ):
        return technical_failure_result(
            "presales_v2_automatic_invite_promised_manager"
        )
    if handoff_required and not handoff_reason:
        handoff_reason = "presales_v2_manager_action_required"
    if action == "knowledge_gap" and not knowledge_gap:
        knowledge_gap = f"Недостаточно подтверждённых фактов для: {inbound_text[:300]}"
    warnings = soft_validation_warnings(
        reply_text=reply_text,
        missing_topics=missing_topics,
        required_topics=required,
        turn_items=turn_items,
    )
    return PresalesV2Normalized(
        ok=reply_action,
        action=action,
        decision=decision,
        intent=intent,
        reply_text=reply_text,
        confidence=confidence,
        risk_level=risk_level,
        next_state=str(raw.get("next_state") or "FAQ automation").strip(),
        handoff_required=handoff_required,
        handoff_reason=handoff_reason,
        handoff_kind=handoff_kind,
        matched_direct_invite_sector_id=matched_direct_invite_sector_id,
        knowledge_gap=knowledge_gap,
        collected_fields_update=collected,
        turn_items=turn_items,
        used_source_ids=used_source_ids,
        invalid_source_ids=[],
        coverage_complete=True,
        reason=reason,
        technical_failure=False,
        validation_warnings=warnings,
    )


def contains_noncanonical_brand_spelling(text: str) -> bool:
    return any(
        match.group(0) != PRESALES_V2_BRAND_NAME
        for match in PRESALES_V2_BRAND_VARIANT_PATTERN.finditer(str(text or ""))
    )


def contains_internal_startbot_name(text: str) -> bool:
    return bool(PRESALES_V2_INTERNAL_STARTBOT_PATTERN.search(str(text or "")))


def normalize_turn_items(value: object) -> List[Dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    result: list[dict[str, object]] = []
    item_ids: set[str] = set()
    for raw in value:
        if not isinstance(raw, dict):
            return None
        if set(raw) != PRESALES_V2_TURN_ITEM_FIELDS:
            return None
        if any(
            not isinstance(raw.get(field_name), str)
            for field_name in (
                "item_id",
                "topic",
                "user_item",
                "user_evidence",
                "status",
                "answer_summary",
                "reply_evidence",
            )
        ):
            return None
        if not isinstance(raw.get("source_ids"), list) or any(
            not isinstance(source_id, str)
            for source_id in raw.get("source_ids") or []
        ):
            return None
        item_id = str(raw.get("item_id") or "").strip()
        topic = str(raw.get("topic") or "").strip()
        user_item = str(raw.get("user_item") or "").strip()
        user_evidence = str(raw.get("user_evidence") or "").strip()
        status = str(raw.get("status") or "").strip()
        answer_summary = str(raw.get("answer_summary") or "").strip()
        reply_evidence = str(raw.get("reply_evidence") or "").strip()
        source_ids = clean_string_list(raw.get("source_ids"))
        if (
            not item_id
            or item_id in item_ids
            or not topic
            or not user_item
            or not user_evidence
            or status not in PRESALES_V2_ALLOWED_ITEM_STATUSES
        ):
            return None
        if status != "not_applicable" and not answer_summary:
            return None
        item_ids.add(item_id)
        result.append(
            {
                "item_id": item_id,
                "topic": topic,
                "user_item": user_item,
                "user_evidence": user_evidence,
                "status": status,
                "answer_summary": answer_summary,
                "reply_evidence": reply_evidence,
                "source_ids": source_ids,
            }
        )
    return result


def required_topics_for_turn(text: str) -> List[str]:
    normalized = normalize_text(text)
    topics: list[str] = []
    for topic, patterns in TOPIC_PATTERNS:
        if any(re.search(pattern, normalized) for pattern in patterns):
            topics.append(topic)
    if "site_links" in topics and any(
        topic in topics
        for topic in {
            "pricing",
            "reviews",
            "cases",
            "signal_examples",
            "partner_program",
        }
    ):
        topics.remove("site_links")
    if not topics and (
        "?" in text
        or len(normalized.split()) >= 3
    ):
        topics.append("general")
    return topics


def technical_failure_result(reason: str) -> PresalesV2Normalized:
    return PresalesV2Normalized(
        ok=False,
        action="knowledge_gap",
        decision="hold_for_review",
        intent="neutral",
        reply_text="",
        confidence=0.0,
        risk_level="medium",
        next_state="",
        handoff_required=False,
        handoff_reason="",
        handoff_kind="none",
        matched_direct_invite_sector_id="",
        knowledge_gap="",
        collected_fields_update={},
        turn_items=[],
        used_source_ids=[],
        invalid_source_ids=[],
        coverage_complete=False,
        reason=reason,
        technical_failure=True,
        validation_warnings=[],
    )


def strict_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if 0.0 <= number <= 1.0 else None


def clean_string_dict(value: object) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        clean_key = str(key or "").strip()
        clean_value = str(item or "").strip()
        if clean_key and clean_value:
            result[clean_key] = clean_value
    return result


def clean_string_list(value: object) -> List[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        clean = str(item or "").strip()
        if clean and clean not in result:
            result.append(clean)
    return result


def question_mark_count(text: str) -> int:
    text_without_urls = re.sub(r"https?://\S+", "", text, flags=re.IGNORECASE)
    return len(re.findall(r"[?？]", text_without_urls))


def soft_validation_warnings(
    *,
    reply_text: str,
    missing_topics: Iterable[str],
    required_topics: Iterable[str],
    turn_items: Iterable[Dict[str, object]],
) -> List[str]:
    warnings: list[str] = []
    missing = list(missing_topics)
    if missing:
        warnings.append("topic_label_mismatch:" + ",".join(missing))
    if question_mark_count(reply_text) > 1:
        warnings.append("style_multiple_questions")
    if len(reply_text) > 1_500:
        warnings.append("style_long_reply")
    required = set(required_topics)
    item_topics = {str(item.get("topic") or "") for item in turn_items}
    if required and not required.intersection(item_topics):
        warnings.append("coverage_keyword_hints_not_reused")
    return warnings


def sha256_json(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
