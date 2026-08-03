from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


OPEN_CONVERSATION_STATES = {
    "Queued",
    "First touch sent",
    "Waiting reply",
    "Interested",
    "FAQ automation",
    "Knowledge review",
    "Qualified",
    "Manager handoff",
    "Manager takeover",
}

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

    Explicit opt-out and confirmed promotional spam are the no-reply gates.
    Complaints, non-Russian and unclear content keep their diagnostic intent,
    and the conversation layer sends a short deterministic reply.
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


def recipient_has_required_consent(recipient: sqlite3.Row) -> bool:
    return (
        recipient["consent_status"] == "active"
        and bool(recipient["consent_source"])
        and bool(recipient["consent_date"])
        and recipient["opt_out_status"] == 0
    )


def recipient_has_reachable_target(recipient: sqlite3.Row) -> bool:
    recipient_type = recipient["recipient_type"]
    if recipient_type == "user":
        return bool(recipient["telegram_user_id"] or recipient["telegram_username"])
    if recipient_type == "channel_dm":
        if not (recipient["telegram_channel_username"] or recipient["channel_chat_id"]):
            return False
        if recipient["channel_dm_available"] != 1:
            return False
        if recipient["channel_dm_status"] not in ("available", "paid"):
            return False
        paid_stars = recipient["paid_message_stars"]
        if paid_stars is not None and int(paid_stars) > 0 and not recipient["paid_dm_approved_at"]:
            return False
        return True
    return False


def recipient_past_cooldown(recipient: sqlite3.Row, now: datetime, cooldown_days: int) -> bool:
    if not recipient["last_contacted_at"]:
        return True
    last_contacted = parse_iso(recipient["last_contacted_at"])
    return last_contacted <= now - timedelta(days=cooldown_days)


def recipient_is_eligible(
    conn: sqlite3.Connection,
    recipient: sqlite3.Row,
    campaign: sqlite3.Row,
    now: datetime,
    min_priority_score: float = 0,
    cooldown_days: int = 30,
) -> bool:
    if not recipient_has_required_consent(recipient):
        return False
    if not recipient_has_reachable_target(recipient):
        return False
    if recipient["segment"] != campaign["segment"]:
        return False
    if float(recipient["priority_score"]) < min_priority_score:
        return False
    if not recipient_past_cooldown(recipient, now, cooldown_days):
        return False

    open_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM conversations
        WHERE recipient_id = ?
          AND state IN ({})
        """.format(",".join("?" for _ in OPEN_CONVERSATION_STATES)),
        (recipient["id"], *sorted(OPEN_CONVERSATION_STATES)),
    ).fetchone()["count"]
    if open_count:
        return False

    pending_count = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM send_queue
        WHERE recipient_id = ?
          AND campaign_id = ?
          AND status = 'pending'
        """,
        (recipient["id"], campaign["id"]),
    ).fetchone()["count"]
    return pending_count == 0
