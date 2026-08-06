"""Локальный разбор входящего сообщения.

Перенесено с релиза a55d259. Взята только чистая часть — разбор текста:
отказ от переписки, жёсткий негатив, язык, рекламный спам, намерение.

Отброшен хвост модуля (``recipient_has_required_consent`` и далее): он
отбирает получателей для рассылки по чужим таблицам ``recipients``,
``conversations`` и ``send_queue``. К ответу на входящее это отношения не
имеет, а кого и когда можно трогать, у нас решает свой preflight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone


OPT_OUT_PATTERNS = [
    r"\bстоп\b",
    r"\bstop\b",
    r"^\s*(пожалуйста\s+)?не\s+пишите\s*[.!?]*\s*$",
    r"^\s*(пожалуйста\s+)?не\s+присылайте\s*[.!?]*\s*$",
    r"удалите",
    r"отписаться",
    r"отпишите",
    r"unsubscribe",
    r"не\s+присылайте",
    r"больше\s+не\s+надо",
    r"не\s+интересно,\s*не\s+пишите",
]

HARD_NEGATIVE_PATTERNS = [
    r"жалоб",
    r"\bэто\s+спам\b",
    r"\bваш[ае]?\s+спам\b",
    r"спамер",
    r"куда\s+жаловаться",
    r"нарушаете",
    r"незакон",
    r"роскомнадзор",
]

INBOUND_NO_REPLY_INTENTS = frozenset({"spam"})

# Only high-confidence promotional patterns belong here. Ambiguous messages stay
# eligible for the primary LLM so a legitimate Russian question about crypto,
# NFT or another off-topic subject is not silently discarded.
UNSOLICITED_SPAM_PATTERNS = [
    r"\b(?:nft|airdrop|giveaway|wallet|token|crypto|dogs)\b.{0,120}\b(?:claim|join|reward|rating|gift|bonus|win|earn|buy)\b",
    r"\b(?:claim|join|reward|rating|gift|bonus|win|earn|buy)\b.{0,120}\b(?:nft|airdrop|giveaway|wallet|token|crypto|dogs)\b",
    # Mixed-language crypto promotions are common: the asset is written in
    # Latin characters while the promotional CTA is Russian.
    r"\b(?:nft|airdrop|giveaway|wallet|token|crypto|dogs)\b.{0,160}\b(?:розыгрыш|подар|зв[её]зд|бонус|получ|участв|забер|выигр|заработ|куп)\w*",
    r"\b(?:розыгрыш|подар|зв[её]зд|бонус|получ|участв|забер|выигр|заработ|куп)\w*.{0,160}\b(?:nft|airdrop|giveaway|wallet|token|crypto|dogs)\b",
    r"\b(?:нфт|аирдроп|эйрдроп|крипт|токен|кошел[её]к|догс)\w*.{0,120}\b(?:забер|получ|участв|подар|бонус|выигр|заработ|куп)\w*",
    r"\b(?:забер|получ|участв|подар|бонус|выигр|заработ|куп)\w*.{0,120}\b(?:нфт|аирдроп|эйрдроп|крипт|токен|кошел[её]к|догс)\w*",
    r"\b(?:casino|казино|betting|ставк[аи])\b.{0,100}\b(?:bonus|бонус|win|выигр|join|переход|регистр)\w*",
    r"\b(?:фрибет\w*|freebet\w*|winline|1xbet|betboom|fonbet|pari|букмекер\w*)\b.{0,180}\b(?:регистр\w*|получ\w*|забер\w*|бонус\w*|ставк\w*|переход\w*)\b",
    r"\b(?:регистр\w*|получ\w*|забер\w*|бонус\w*|ставк\w*|переход\w*)\b.{0,180}\b(?:фрибет\w*|freebet\w*|winline|1xbet|betboom|fonbet|pari|букмекер\w*)\b",
    # Russian referral/service promotion.  Require both an unsolicited CTA and
    # a commercial/referral marker so normal product questions stay eligible
    # for the primary LLM.
    r"\b(?:попробуй|попробуйте|переходи|переходите|запускай|регистрируйся)\w*\b.{0,220}\b(?:реферал\w*|бонус\w*|зараб\w*|продаж\w*|покупател\w*|бирж\w*)\b",
    r"\b(?:реферал\w*|бонус\w*|сотн\w*\s+покупател\w*|тысяч\w*\s+(?:админ\w*|пользовател\w*))\b.{0,220}\b(?:попробуй|попробуйте|бесплатн\w*|переходи|регистрируйся)\w*\b",
    r"\b(?:попробуй|попробуйте)\w*\s+бесплатн\w*.{0,220}@[a-z0-9_]{5,32}bot\b",
]

RUSSIAN_LETTER_PATTERN = re.compile(r"[а-яё]", flags=re.IGNORECASE)
NON_RUSSIAN_CYRILLIC_PATTERN = re.compile(r"[іїєґўђћџљњќѓѕј]", flags=re.IGNORECASE)

REFERRED_CONTACT_PATTERN = re.compile(
    r"\b(?:напиши(?:те)?|свяжи(?:сь|тесь)|обрати(?:сь|тесь))\b"
    r".{0,80}?(@[a-zA-Z0-9_]{5,32})\b",
    flags=re.IGNORECASE,
)

@dataclass(frozen=True)
class MessageClassification:
    intent: str
    risk_level: str
    confidence: float
    handoff_required: bool = False
    automation_paused: bool = False
    reason: str = ""

def parse_iso(value: str) -> datetime:
    normalized = str(value).strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def has_any(patterns, text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def extract_referred_telegram_username(text: str) -> str:
    match = REFERRED_CONTACT_PATTERN.search(text or "")
    if not match:
        return ""
    # "Не пишите @user" is a prohibition, not a referral instruction.
    negative_prefix = (text or "")[max(0, match.start() - 24) : match.start()]
    if re.search(r"\bне\s*$", negative_prefix, flags=re.IGNORECASE):
        return ""
    return match.group(1)


def is_opt_out_request(normalized: str) -> bool:
    if has_any(OPT_OUT_PATTERNS, normalized):
        return True
    semantic_patterns = [
        r"(больше|никогда|впредь|в\s+дальнейшем).{0,80}\bне\b.{0,40}(пиш|присыл|отправ|связыва|беспоко|тревож|звон)",
        r"(прошу|пожалуйста).{0,40}\bне\b.{0,40}(пиш|присыл|отправ|связыва|беспоко|тревож|звон)",
        r"(не\s+надо|не\s+нужно|не\s+хочу|не\s+желаю).{0,60}(получать|общаться|сообщени|рассыл|пиш|присыл|отправ|связыва)",
        r"(прекратите|прекращайте|хватит).{0,80}(пис|присыл|отправ|коммуникац|связ|рассыл|звон)",
        r"(удалите|уберите|исключите).{0,80}(меня|мой\s+номер|мой\s+контакт|из\s+базы|из\s+рассыл|из\s+списк)",
        r"(do\s+not|don't|dont).{0,50}(contact|message|text|write|email)",
        r"(stop|unsubscribe).{0,50}(message|contact|text|email|me)",
    ]
    return has_any(semantic_patterns, normalized)


def is_hard_negative_request(normalized: str) -> bool:
    if has_any(HARD_NEGATIVE_PATTERNS, normalized):
        return True
    semantic_patterns = [
        r"(буду|хочу|планирую).{0,40}жалова",
        r"(пожалуюсь|подаю\s+жалобу|подам\s+жалобу)",
        r"(это|ваш[ае]?).{0,30}(спам|незакон)",
    ]
    return has_any(semantic_patterns, normalized)


def is_high_confidence_unsolicited_spam(normalized: str) -> bool:
    return has_any(UNSOLICITED_SPAM_PATTERNS, normalized)


def inbound_language_reason(normalized: str) -> str:
    """Classify clearly non-Russian or meaningless text for the inbound gate.

    Product names, URLs and Latin abbreviations are allowed when the message has
    a meaningful Russian-language core. Short Russian replies such as "да" and
    greetings are intentionally eligible.
    """
    letters = [char for char in normalized if char.isalpha()]
    if not letters:
        return "meaningless"
    russian_letters = RUSSIAN_LETTER_PATTERN.findall(normalized)
    if not russian_letters:
        return "non_russian"
    non_russian_cyrillic = NON_RUSSIAN_CYRILLIC_PATTERN.findall(normalized)
    if len(non_russian_cyrillic) >= 2 and len(non_russian_cyrillic) / len(letters) >= 0.08:
        return "non_russian"
    if len(russian_letters) / len(letters) < 0.30:
        return "non_russian"
    compact_letters = "".join(letters)
    if len(compact_letters) >= 4 and len(set(compact_letters)) == 1:
        return "meaningless"
    return ""


def is_affirmative_opt_out_confirmation(text: str) -> bool:
    normalized = normalize_text(text)
    compact = re.sub(r"[.!?]+$", "", normalized).strip()
    if compact in {
        "да",
        "да да",
        "верно",
        "правильно",
        "именно",
        "подтверждаю",
        "ага",
        "угу",
    }:
        return True
    return has_any(
        [
            r"^да\b.{0,60}(останов|прекрат|не\s+пиш|больше\s+не|не\s+надо)",
            r"^(верно|правильно|именно)\b.{0,60}(останов|прекрат|не\s+пиш|больше\s+не|не\s+надо)?$",
        ],
        normalized,
    )


def classify_inbound(text: str) -> MessageClassification:
    """Classify local safety cases while silently dropping confirmed spam.

    Опт-аут, подтверждённый рекламный спам и любой нерусский текст остаются без
    ответа. Жалоба тоже: она уходит менеджеру карточкой, а спорить с ней
    машине нечем. Короткую вежливую рамку получает ровно один случай —
    `meaningless`, бессмысленный русский текст.

    Прежняя формулировка обещала рамку и жалобам, и нерусским; ни того, ни
    другого не было ни дня. Нерусское входящее приходит сюда с intent `spam`, а
    `apply` на спаме молчит; жалоба уходит вердиктом `manager_handoff`, а он
    без текста. Описание разошлось с кодом, и это опаснее, чем кажется: по нему
    поведение и «чинят».
    """
    normalized = normalize_text(text)
    if is_opt_out_request(normalized):
        return MessageClassification(
            intent="opt_out",
            risk_level="high",
            confidence=0.98,
            handoff_required=False,
            automation_paused=True,
            reason="global_opt_out_phrase",
        )
    if is_hard_negative_request(normalized):
        return MessageClassification(
            intent="hard_negative",
            risk_level="high",
            confidence=0.90,
            handoff_required=True,
            automation_paused=True,
            reason="hard_negative_boundary_reply",
        )
    if is_high_confidence_unsolicited_spam(normalized):
        return MessageClassification(
            intent="spam",
            risk_level="medium",
            confidence=0.98,
            handoff_required=False,
            automation_paused=False,
            reason="inbound_spam_suppressed",
        )
    language_reason = inbound_language_reason(normalized)
    if language_reason == "non_russian":
        return MessageClassification(
            intent="spam",
            risk_level="medium",
            confidence=0.99,
            handoff_required=False,
            automation_paused=False,
            reason="inbound_non_russian_suppressed",
        )
    if language_reason:
        return MessageClassification(
            intent=language_reason,
            risk_level="low",
            confidence=0.99,
            handoff_required=False,
            automation_paused=False,
            reason=f"inbound_{language_reason}_boundary_reply",
        )
    return MessageClassification(
        intent="neutral",
        risk_level="medium",
        confidence=0.0,
        handoff_required=False,
        automation_paused=False,
        reason="llm_semantic_authority",
    )
