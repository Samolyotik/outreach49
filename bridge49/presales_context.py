"""Контекст разговора для presales-модели.

Взято с релиза a55d259 — шесть функций из ``presales.py`` (2732 строки), до
которых дотягивается сборка контекста. Ни одна из них не обращается к базе:
на вход идут обычные словари, а не строки чужих таблиц, поэтому переносятся
дословно.

Остальное из ``presales.py`` не переносим: там маршрутизация по базе знаний
поверх ``campaigns.knowledge_base_scope``, работа с публичными чатами и старые
входы из ``conversation.py`` — всё на схеме, которой у нас нет.

Аннотации ``sqlite3.Row`` заменены на ``Mapping``: прежний контур и сам
передавал сюда обычные словари, глуша несоответствие через ``type: ignore``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional

from .policy import MessageClassification



CHAT_SENDER_PRIVATE_ENTRY_MODE = "chat_sender_private_after_public_chat"


INBOUND_REFERRAL_CAMPAIGN_ID = "campaign_inbound_private_messages"


def presales_entry_mode(
    conversation: Mapping[str, Any],
    recipient: Mapping[str, Any],
    sender_account: Dict[str, str],
) -> str:
    if not is_inbound_discovery_conversation(conversation, recipient):
        return "post_first_touch"
    if str(sender_account.get("role") or "") == "chat_sender":
        return CHAT_SENDER_PRIVATE_ENTRY_MODE
    return "inbound_private_without_first_touch"


def is_inbound_discovery_conversation(
    conversation: Mapping[str, Any],
    recipient: Mapping[str, Any],
) -> bool:
    return (
        str(conversation["campaign_id"] or "") == INBOUND_REFERRAL_CAMPAIGN_ID
        and str(recipient["recipient_type"] or "") == "user"
    )


def load_presales_context(conversation: Mapping[str, Any]) -> Dict[str, str]:
    if "presales_context" not in conversation.keys():
        return {}
    try:
        raw = json.loads(str(conversation["presales_context"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): str(value)
        for key, value in raw.items()
        if str(key).strip() and str(value).strip()
    }


def should_offer_manager(
    auto_reply_count: int,
    manager_nudge_after_replies: int,
) -> bool:
    return manager_nudge_after_replies > 0 and auto_reply_count >= manager_nudge_after_replies


def non_silent_boundary_reply(intent: str) -> str:
    if intent == "non_russian":
        return "Я отвечаю на русском. Напишите вопрос по-русски, и я постараюсь помочь по ТГ РАДАР."
    if intent == "spam":
        return "Похоже, это рекламное сообщение. Если у вас есть вопрос по ТГ РАДАР, напишите его, и я отвечу."
    return "Не совсем понял сообщение. Напишите вопрос чуть подробнее, и я постараюсь помочь."


def build_llm_context(
    conversation: Mapping[str, Any],
    recipient: Mapping[str, Any],
    sender_account: Dict[str, str],
    classification: MessageClassification,
    history: List[Dict[str, str]],
    auto_reply_count: int,
    max_auto_replies: int,
    manager_nudge_after_replies: int,
    entry_mode: Optional[str] = None,
    recent_public_chat_outreach: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, object]:
    inbound_discovery = is_inbound_discovery_conversation(conversation, recipient)
    resolved_entry_mode = entry_mode or presales_entry_mode(
        conversation,
        recipient,
        sender_account,
    )
    has_prior_outbound = any(item["direction"] == "outbound" for item in history)
    context: Dict[str, object] = {
        "conversation_state": conversation["state"],
        "campaign_id": conversation["campaign_id"],
        "entry_mode": resolved_entry_mode,
        "has_prior_outbound": has_prior_outbound,
        "discovery_context": load_presales_context(conversation),
        "sender_account": sender_account,
        "recipient": {
            "id": recipient["id"],
            "type": recipient["recipient_type"],
            "segment": recipient["segment"],
            "company": recipient["company"] or "",
            "role": recipient["role"] or "",
            "source": recipient["source"] or "",
            "consent_status": recipient["consent_status"] or "",
            "consent_source": recipient["consent_source"] or "",
            "consent_scope": recipient["consent_scope"] or "",
            "consent_date": recipient["consent_date"] or "",
        },
        "classification": {
            "intent": classification.intent,
            "reason": classification.reason,
            "confidence": classification.confidence,
        },
        "message_history": history[-8:],
        "auto_reply_count": auto_reply_count,
        "max_auto_replies_soft_threshold": max_auto_replies,
        "manager_nudge_after_replies": manager_nudge_after_replies,
        "should_offer_manager_soft_handoff": should_offer_manager(
            auto_reply_count,
            manager_nudge_after_replies,
        ),
        "autonomous_presales_version": "v2",
        "goal": (
            "secure consent for free system access / product demo with a manager, "
            "while staying grounded in approved knowledge"
        ),
        "handoff_rules": [
            "answer general pricing logic if approved knowledge covers it",
            "answer demo or free-access mechanics if approved knowledge covers it",
            "do not collect a full technical brief before the CTA",
            "ask at most one value-discovery question before offering free system access",
            "handoff when the person agrees to free system access or product demo with a manager",
            "handoff for explicit live person, call or meeting request",
            "handoff for contract, payment, invoice, custom price or discount",
            "handoff for access issuance, start test, pilot launch or demo booking action",
            "handoff for requests that need a legal, medical, financial or personal-data answer",
            "a reply that only says a vacancy/ad is current and redirects to another contact is not product interest and must not create a handoff",
            "when asked what happens next, explain that the manager arranges free system access / a test period; do not replace this CTA with an offer to send two or three examples",
            "after handoff, keep answering additional low-risk questions in the same chat while the manager handles access and next steps",
            "when active_handoff=true, a new low-risk question must be answered with auto_reply; never repeat the handoff confirmation unless the person explicitly asks again for a call, manager, access, contract, payment or another manager-only action",
            "when post_handoff_answer_retry=true, correct the prior routing mistake: answer the current low-risk question directly from approved facts, do not return manager_handoff and do not repeat that the request is already recorded",
            "a previous soft refusal, hard refusal or opt_out is not permanent after a new user-initiated inbound: if the current inbound clearly reopens the dialog, asks a follow-up question, agrees to continue or requests the next step, classify the current intent and continue; a repeated refusal or repeated opt_out stays paused with no reply; prior opt_out continues to block every proactive message until that inbound re-consent",
            "an already-created handoff is monotonic and must never be cancelled or downgraded because of later inbound messages; the manager reviews the complete dialog manually",
            "do not handoff only because a sensitive sector is mentioned; answer general product mechanics and limits",
            "do not handoff only because the person asks not to violate the law; acknowledge the limit and explain low-risk mechanics",
            "when should_offer_manager_soft_handoff=true, keep answering and make the single integrated next question a soft offer to connect a colleague; never append a second nudge or CTA",
            "do not stop answering only because auto_reply_count reached the soft threshold",
        ],
    }
    if inbound_discovery:
        context["inbound_discovery_rules"] = [
            "this person started a private DM and there was no proactive first-touch",
            "do not assume why the person wrote or that they already want ТГ РАДАР",
            "for a greeting or a vague referral intro, ask one short neutral question about the reason for contact",
            "if the message already contains a substantive question, answer it directly instead of forcing sector discovery",
            "sector is optional: never pause, ignore, handoff or create a knowledge gap only because sector is missing",
            "a sector outside the canonical top-sector list is valid user context and must not block the dialog",
            "if the person declines to name a sector, continue with universal product logic without naming a sector",
            "when a new sector is stated, use it as user-provided context without inventing sector-specific facts or volumes",
            "continue leading toward a free test or product demo when it is relevant",
            "write explicit fields into collected_fields_update only when supported by the conversation: sector, sector_status, inbound_need, referral_source",
        ]
    if resolved_entry_mode == CHAT_SENDER_PRIVATE_ENTRY_MODE:
        context["recent_public_chat_outreach"] = list(recent_public_chat_outreach or [])
        context["public_chat_context_policy"] = (
            "Recent public messages belong to this sender account, but they do not prove which "
            "message or chat led this person to write. Treat them only as weak background until "
            "the person explicitly identifies the request, sector, service or chat."
        )
        context["chat_sender_inbound_rules"] = [
            "the person may be responding as a supplier or specialist to a public chat seed that looked like a request for a service",
            "never continue pretending that we are negotiating a purchase or that a confirmed order exists",
            "when the person clearly responds to the public request, thank them and transparently clarify that there is no confirmed order before pivoting",
            "before saying there is no confirmed order, use a natural soft honesty marker such as «сразу скажу честно» or «сразу честно уточню, чтобы не вводить в заблуждение»; never lead with a blunt bare «у нас нет заказа»",
            "connect the stated service or sector to ТГ РАДАР only through approved universal product facts: public demand signals, AI noise filtering and delivery to the team",
            "keep the first pivot short: one value explanation, an offer to see a free test or demo, and at most one question",
            "if the message is only a greeting or the reason is unclear, ask whether they are writing about the chat message and what task or sector they mean",
            "when quoted_public_chat_request_likely=true, the current text is a copy of our own public seed and is context, not the person's own unknown question; acknowledge the source, clarify that there is no confirmed order, and pivot to ТГ РАДАР without asking what the copied request means",
            "a supplier who only offers their own services and asks for a call has not expressed interest in ТГ РАДАР; do not create a handoff until they explicitly accept a product test, demo, access or manager contact after a transparent product explanation",
            "do not claim that a specific recent public message caused the inbound unless the person says so",
            "if the person is not interested, return pause_conversation without arguing, another pitch or a follow-up",
            "if the person agrees to a free test, demo, manager contact or access, return manager_handoff",
            "when supported by the message, store sector, inbound_need, referral_source=public_chat_response and signal_type=supplier_response_to_chat_seed",
        ]
    return context
