from __future__ import annotations

import json
import re
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, ContextManager, Dict, List, Optional

from .account_identity import sender_account_identity
from .knowledge import (
    KnowledgeChunk,
    answer_card_matches_for_query,
    answer_pack_matches_for_query,
    load_kb_manifest,
    normalize_chunk_text,
    resolve_knowledge_path,
    retrieve_knowledge_chunks,
    source_metadata,
    split_markdown_chunks,
)
from .llm_assistant import (
    ExternalLLMDraft,
    ExternalLLMReview,
    clean_reply_style,
    draft_with_external_llm,
    review_with_external_llm,
)
from .policy import MessageClassification, normalize_text


PRESALES_INTENTS = {
    "greeting",
    "positive",
    "faq_question",
    "pricing_question",
    "demo_question",
    "unknown_question",
    "neutral",
}

MAX_AUTO_PRESALES_REPLIES = 100
MANAGER_NUDGE_AFTER_AUTO_PRESALES_REPLIES = 80
INTERNAL_KB_SOURCES = {
    "autonomous_presales_policy.md",
    "presales_assistant_playbook.md",
    "tone_of_voice.md",
    "forbidden_claims.md",
    "compliance_notes.md",
    "changelog.md",
}
PRESALES_INTERNAL_HINT_SOURCES = {
    "forbidden_claims.md",
    "compliance_notes.md",
}
PRESALES_ALWAYS_INCLUDE_SOURCES = (
    "product_overview.md",
    "company_profile.md",
    "site_pages.md",
    "offer.md",
    "pricing_policy.md",
    "free_test.md",
    "faq.md",
    "allowed_claims.md",
)
PRESALES_PROOF_INCLUDE_SOURCES = (
    "case_studies.md",
    "reviews.md",
    "examples/signal_examples.md",
    "faq.md",
    "company_profile.md",
    "site_pages.md",
)
LLM_TECHNICAL_FAILURE_REASONS = (
    "llm_command_error",
    "llm_command_failed",
    "llm_invalid_json",
    "llm_invalid_response_schema",
    "codex_wrapper_",
)
SECONDARY_LLM_REVIEW_PATTERNS = (
    r"\bгарант",
    r"\bдоговор",
    r"\bсч[её]т",
    r"\bоплат",
    r"\bскидк",
    r"\bюрид",
    r"персональн[а-я\s]+данн",
    r"\broi\b",
    r"\bcpl\b",
    r"\bcac\b",
)
PRESALES_ANSWER_PACK_LIMIT = 2
PRESALES_ANSWER_CARD_LIMIT = 2
PRESALES_CHUNK_LIMIT = 12
PRESALES_ANSWER_PACK_RESPONSE_HEADINGS = {
    "Что отвечать",
    "Что важно добавить",
    "Что не обещать",
    "Как объяснять кейсы",
    "Proof points",
    "Пример формулировки",
    "Как выбрать сценарий",
    "Как объяснять проверку",
    "Осторожные сферы",
    "Что можно назвать",
    "Что требует менеджера",
    "Что можно объяснить",
    "Что нельзя делать",
    "Что не входит в услугу",
}
INBOUND_REFERRAL_CAMPAIGN_ID = "campaign_inbound_private_messages"
CHAT_SENDER_PRIVATE_ENTRY_MODE = "chat_sender_private_after_public_chat"
CHAT_SENDER_INBOUND_STATE = "Chat inbound presales"
RECENT_PUBLIC_CHAT_CONTEXT_LIMIT = 5
RECENT_PUBLIC_CHAT_CONTEXT_DAYS = 45
PRIMARY_LLM_MAX_ATTEMPTS = 2
SECONDARY_LLM_MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class PresalesDraft:
    ok: bool
    text: str
    confidence: float
    risk_level: str
    source_files: List[str]
    next_state: str = "FAQ automation"
    reason: str = ""
    handoff_required: bool = False
    handoff_reason: str = ""
    handoff_kind: str = ""
    matched_direct_invite_sector_id: str = ""
    knowledge_gap: str = ""
    intent: str = ""
    decision: str = "auto_reply"
    reply_source: str = "unknown"
    collected_fields_update: Dict[str, str] | None = None
    retrieved_source_files: List[str] = field(default_factory=list)
    claimed_used_sources: List[str] = field(default_factory=list)
    validated_used_sources: List[str] = field(default_factory=list)
    invalid_used_sources: List[str] = field(default_factory=list)
    secondary_review_required: bool = False
    secondary_review_decision: str = ""
    secondary_review_confidence: float | None = None
    secondary_review_reason: str = ""
    secondary_review_reason_code: str = ""
    primary_llm_attempts: int = 0
    secondary_review_attempts: int = 0
    engine_version: str = "v1"
    fallback_from_engine: str = ""
    truth_pack_sha256: str = ""
    prompt_sha256: str = ""
    turn_items: List[Dict[str, object]] = field(default_factory=list)
    required_topics: List[str] = field(default_factory=list)
    coverage_complete: bool = False
    technical_failure: bool = False
    contract_version: str = ""
    hard_validation_passed: bool = False
    validation_warnings: List[str] = field(default_factory=list)
    repair_reason: str = ""
    truth_fact_count: int = 0
    truth_runtime_characters: int = 0
    prompt_characters: int = 0
    generation_duration_ms: int = 0


@dataclass(frozen=True)
class PresalesKnowledgeRoute:
    topic_ids: List[str]
    answer_pack_sources: List[str]
    answer_card_sources: List[str]
    source_hints: List[str]
    search_terms: str


def is_presales_candidate(classification: MessageClassification) -> bool:
    return (
        classification.intent in PRESALES_INTENTS
        and not classification.handoff_required
        and not classification.automation_paused
    )


def draft_presales_reply(
    conn: sqlite3.Connection,
    conversation: sqlite3.Row,
    recipient: sqlite3.Row,
    inbound_text: str,
    classification: MessageClassification,
    kb_root: str = "knowledge_base",
    max_auto_replies: int = MAX_AUTO_PRESALES_REPLIES,
    manager_nudge_after_replies: int = MANAGER_NUDGE_AFTER_AUTO_PRESALES_REPLIES,
    typing_indicator: Optional[Callable[[], ContextManager[None]]] = None,
    direct_invite_context: Optional[Dict[str, str]] = None,
) -> PresalesDraft:
    history = recent_messages(conn, conversation["id"], limit=10)
    auto_reply_count = count_auto_replies_for_conversation(conn, conversation["id"])
    inbound_discovery = is_inbound_discovery_conversation(conversation, recipient)
    sender_account = sender_account_identity(conn, conversation["sender_account_id"])
    entry_mode = presales_entry_mode(conversation, recipient, sender_account)
    recent_public_chat_outreach = (
        recent_public_chat_outreach_context(
            conn,
            sender_account_id=conversation["sender_account_id"],
            reference_at=(
                history[-1]["created_at"]
                if history
                else str(conversation["created_at"] or "")
            ),
        )
        if entry_mode == CHAT_SENDER_PRIVATE_ENTRY_MODE
        else []
    )

    chunks = retrieve_presales_chunks(
        conn,
        conversation=conversation,
        recipient=recipient,
        inbound_text=inbound_text,
        classification=classification,
        history=history,
        kb_root=kb_root,
    )
    context = build_llm_context(
        conversation=conversation,
        recipient=recipient,
        sender_account=sender_account,
        classification=classification,
        history=history,
        auto_reply_count=auto_reply_count,
        max_auto_replies=max_auto_replies,
        manager_nudge_after_replies=manager_nudge_after_replies,
        entry_mode=entry_mode,
        recent_public_chat_outreach=recent_public_chat_outreach,
    )
    context["active_handoff"] = conversation["handoff_status"] in {"pending", "taken"}
    if direct_invite_context:
        context["free_test_access_branch"] = dict(direct_invite_context)
        context["goal"] = (
            "secure explicit consent for a free test and let the deterministic router issue "
            "one one-time StartBot link; use a manager only for an explicit human/call request "
            "or another manager-only trigger"
        )
    if entry_mode == CHAT_SENDER_PRIVATE_ENTRY_MODE:
        context["quoted_public_chat_request_likely"] = is_likely_quoted_public_chat_request(
            inbound_text,
            history,
            recent_public_chat_outreach,
        )
    secondary_review_required = False
    secondary_review = None
    primary_llm_attempts = 0
    secondary_review_attempts = 0
    with typing_indicator() if typing_indicator is not None else nullcontext():
        external = None
        for _attempt in range(PRIMARY_LLM_MAX_ATTEMPTS):
            primary_llm_attempts += 1
            external = draft_with_external_llm(inbound_text, chunks, context=context)
            if not should_retry_external_llm(external):
                break
        if (
            external
            and (external.handoff_required or external.decision == "manager_handoff")
            and bool(context.get("active_handoff"))
            and not is_explicit_post_handoff_action_request(inbound_text)
        ):
            # The handoff itself is already monotonic.  For a later low-risk
            # question, ask the semantic authority once more for the answer
            # instead of emitting another generic handoff confirmation.
            repair_context = dict(context)
            repair_context["post_handoff_answer_retry"] = True
            repaired = draft_with_external_llm(
                inbound_text,
                chunks,
                context=repair_context,
            )
            if repaired and repaired.ok and repaired.decision in {"", "auto_reply"}:
                external = repaired
        if external and external.ok and should_request_secondary_llm_review(inbound_text, external):
            secondary_review_required = True
            for _attempt in range(SECONDARY_LLM_MAX_ATTEMPTS):
                secondary_review_attempts += 1
                secondary_review = review_with_external_llm(
                    inbound_text,
                    chunks,
                    external,
                    context=context,
                )
                if secondary_review is not None:
                    break
    retrieved_source_files = source_files(chunks)
    claimed_used_sources = dedupe_strings(list(external.used_sources)) if external else []
    validated_used_sources, invalid_used_sources = validate_claimed_used_sources(
        chunks,
        claimed_used_sources,
    )
    effective_source_files = validated_used_sources or retrieved_source_files
    trace_kwargs = {
        "retrieved_source_files": retrieved_source_files,
        "claimed_used_sources": claimed_used_sources,
        "validated_used_sources": validated_used_sources,
        "invalid_used_sources": invalid_used_sources,
        "secondary_review_required": secondary_review_required,
        "secondary_review_decision": (
            secondary_review.decision
            if secondary_review is not None
            else ("unavailable" if secondary_review_required else "")
        ),
        "secondary_review_confidence": (
            secondary_review.confidence if secondary_review is not None else None
        ),
        "secondary_review_reason": (
            secondary_review.reason
            if secondary_review is not None
            else ("secondary_llm_review_unavailable" if secondary_review_required else "")
        ),
        "secondary_review_reason_code": (
            secondary_review.reason_code if secondary_review is not None else "technical_unavailable"
        ) if secondary_review_required else "",
        "primary_llm_attempts": primary_llm_attempts,
        "secondary_review_attempts": secondary_review_attempts,
    }
    if external and external.collected_fields_update and inbound_discovery:
        store_presales_context(
            conn,
            conversation_id=conversation["id"],
            updates=external.collected_fields_update,
        )
    if inbound_discovery and llm_technically_unavailable(external):
        if is_clear_soft_negative(inbound_text):
            return PresalesDraft(
                ok=True,
                text=soft_negative_acknowledgement_text(),
                confidence=0.0,
                risk_level="low",
                source_files=effective_source_files,
                reason="soft_negative_during_llm_outage",
                intent="soft_negative",
                decision="pause_conversation",
                reply_source="deterministic_fallback",
                **trace_kwargs,
            )
        fallback_text = inbound_discovery_fallback_reply(
            inbound_text=inbound_text,
            classification=classification,
            history=history,
            chunks=chunks,
            entry_mode=entry_mode,
        )
        return PresalesDraft(
            ok=True,
            text=clean_reply_style(fallback_text),
            confidence=0.0,
            risk_level="low",
            source_files=effective_source_files,
            next_state=(
                CHAT_SENDER_INBOUND_STATE
                if entry_mode == CHAT_SENDER_PRIVATE_ENTRY_MODE
                else "Inbound discovery"
            ),
            intent=classification.intent,
            decision="auto_reply",
            reply_source="deterministic_fallback",
            **trace_kwargs,
        )
    if external and external.decision == "ignore":
        if external.intent in {"spam", "non_russian"}:
            return PresalesDraft(
                ok=False,
                text="",
                confidence=external.confidence,
                risk_level="low",
                source_files=effective_source_files,
                reason=external.reason or (
                    "inbound_non_russian_suppressed"
                    if external.intent == "non_russian"
                    else "unsolicited_promotion"
                ),
                intent="spam",
                decision="ignore",
                reply_source="llm",
                **trace_kwargs,
            )
        return PresalesDraft(
            ok=True,
            text=non_silent_boundary_reply(external.intent),
            confidence=external.confidence,
            risk_level="low",
            source_files=effective_source_files,
            reason=external.reason or "inbound_reply_suppressed",
            intent=external.intent or "meaningless",
            decision="auto_reply",
            reply_source="deterministic_non_silent_boundary",
            **trace_kwargs,
        )
    if external and external.decision == "pause_conversation":
        return PresalesDraft(
            ok=True,
            text=soft_negative_acknowledgement_text(),
            confidence=external.confidence,
            risk_level="low",
            source_files=effective_source_files,
            reason=external.reason or "soft_negative",
            intent="soft_negative",
            decision="pause_conversation",
            reply_source="deterministic_non_silent_boundary",
            **trace_kwargs,
        )
    if external and external.decision == "opt_out":
        return PresalesDraft(
            ok=True,
            text=opt_out_clarification_text(),
            confidence=external.confidence,
            risk_level="low",
            source_files=effective_source_files,
            reason="model_opt_out_without_local_explicit_opt_out",
            intent="neutral",
            decision="auto_reply",
            reply_source="deterministic_non_silent_boundary",
            **trace_kwargs,
        )
    if external and external.decision in {"hold_for_review", "knowledge_gap"}:
        return PresalesDraft(
            ok=False,
            text="",
            confidence=external.confidence,
            risk_level=external.risk_level,
            source_files=effective_source_files,
            reason=external.reason or external.decision,
            knowledge_gap=external.knowledge_gap or f"LLM requested {external.decision}: {inbound_text}",
            intent=external.intent or classification.intent,
            decision=external.decision,
            reply_source="llm",
            **trace_kwargs,
        )
    if (
        external
        and (external.handoff_required or external.decision == "manager_handoff")
        and is_redirect_only_without_product_interest(inbound_text)
    ):
        return PresalesDraft(
            ok=True,
            text=soft_negative_acknowledgement_text(),
            confidence=external.confidence,
            risk_level="low",
            source_files=effective_source_files,
            reason="redirect_only_without_tg_radar_interest",
            intent="soft_negative",
            decision="pause_conversation",
            reply_source="deterministic_handoff_guard",
            **trace_kwargs,
        )
    if (
        external
        and (external.handoff_required or external.decision == "manager_handoff")
        and is_unsolicited_supplier_call_offer_without_product_interest(
            inbound_text,
            history,
        )
    ):
        return PresalesDraft(
            ok=True,
            text=supplier_offer_boundary_reply(),
            confidence=external.confidence,
            risk_level="low",
            source_files=effective_source_files,
            reason="supplier_call_offer_without_tg_radar_interest",
            intent="soft_negative",
            decision="pause_conversation",
            reply_source="deterministic_handoff_guard",
            **trace_kwargs,
        )
    if (
        external
        and (external.handoff_required or external.decision == "manager_handoff")
        and bool(context.get("active_handoff"))
        and not is_explicit_post_handoff_action_request(inbound_text)
    ):
        return PresalesDraft(
            ok=False,
            text="",
            confidence=external.confidence,
            risk_level="low",
            source_files=effective_source_files,
            reason="duplicate_active_handoff_confirmation_suppressed",
            intent=external.intent or classification.intent,
            decision="hold_for_review",
            reply_source="deterministic_handoff_guard",
            **trace_kwargs,
        )
    if external and (external.handoff_required or external.decision == "manager_handoff"):
        return PresalesDraft(
            ok=False,
            text="",
            confidence=external.confidence,
            risk_level=external.risk_level,
            source_files=effective_source_files,
            reason=external.reason or "handoff_required",
            handoff_required=True,
            handoff_reason=external.handoff_reason or external.reason or "handoff_required",
            knowledge_gap=external.knowledge_gap,
            intent=external.intent or "manager_handoff",
            decision="manager_handoff",
            reply_source="llm",
            **trace_kwargs,
        )
    if external and external.ok and secondary_review_required:
        if secondary_review is not None and secondary_review.decision == "escalate":
            return PresalesDraft(
                ok=False,
                text="",
                confidence=secondary_review.confidence,
                risk_level="medium",
                source_files=effective_source_files,
                reason=secondary_review.reason or "secondary_llm_escalation",
                handoff_required=True,
                handoff_reason=secondary_review.reason or "secondary_llm_escalation",
                intent="manager_handoff",
                decision="manager_handoff",
                reply_source="llm",
                **trace_kwargs,
            )
        if not secondary_review_is_approved(secondary_review):
            insufficient_facts = secondary_review_has_insufficient_facts(secondary_review)
            return PresalesDraft(
                ok=False,
                text="",
                confidence=external.confidence,
                risk_level="medium",
                source_files=effective_source_files,
                reason=(
                    "knowledge_not_enough"
                    if insufficient_facts
                    else (
                        secondary_review.reason
                        if secondary_review is not None and secondary_review.reason
                        else "secondary_llm_review_unavailable"
                    )
                ),
                knowledge_gap=(
                    external.knowledge_gap
                    or f"Secondary reviewer found insufficient facts for: {inbound_text}"
                    if insufficient_facts
                    else ""
                ),
                intent=external.intent or classification.intent,
                decision="knowledge_gap" if insufficient_facts else "hold_for_review",
                reply_source="llm",
                **trace_kwargs,
            )
    if external and external.ok:
        return PresalesDraft(
            ok=True,
            text=clean_reply_style(external.text),
            confidence=external.confidence,
            risk_level=external.risk_level,
            source_files=effective_source_files,
            next_state=(
                CHAT_SENDER_INBOUND_STATE
                if entry_mode == CHAT_SENDER_PRIVATE_ENTRY_MODE
                else (
                    external.next_state
                    or ("Inbound discovery" if inbound_discovery else next_state_for(classification))
                )
            ),
            intent=external.intent or classification.intent,
            decision=external.decision or "auto_reply",
            reply_source="llm",
            collected_fields_update=external.collected_fields_update,
            **trace_kwargs,
        )
    if external:
        return PresalesDraft(
            ok=False,
            text="",
            confidence=external.confidence,
            risk_level=external.risk_level,
            source_files=effective_source_files,
            reason=external.reason or "llm_decision_blocked",
            knowledge_gap=external.knowledge_gap or f"LLM decision requires review: {inbound_text}",
            intent=external.intent or classification.intent,
            decision="hold_for_review",
            reply_source="llm",
            **trace_kwargs,
        )
    return PresalesDraft(
        ok=False,
        text="",
        confidence=0.0,
        risk_level="medium",
        source_files=effective_source_files,
        reason="llm_unavailable_after_retry",
        knowledge_gap=f"Primary LLM did not return a decision: {inbound_text}",
        intent=classification.intent,
        decision="hold_for_review",
        reply_source="llm",
        **trace_kwargs,
    )


def should_retry_external_llm(draft: Optional[ExternalLLMDraft]) -> bool:
    if draft is None:
        return True
    return any(draft.reason.startswith(prefix) for prefix in LLM_TECHNICAL_FAILURE_REASONS)


def llm_technically_unavailable(draft: Optional[ExternalLLMDraft]) -> bool:
    return draft is None or should_retry_external_llm(draft)


def should_request_secondary_llm_review(
    inbound_text: str,
    draft: ExternalLLMDraft,
) -> bool:
    if not draft.ok or draft.decision not in {"", "auto_reply"}:
        return False
    if draft.confidence < 0.8:
        return True
    combined = normalize_text(f"{inbound_text} {draft.text}")
    return any(re.search(pattern, combined, flags=re.IGNORECASE) for pattern in SECONDARY_LLM_REVIEW_PATTERNS)


def secondary_review_is_approved(review: ExternalLLMReview | None) -> bool:
    return bool(review is not None and review.decision == "approve")


def secondary_review_has_insufficient_facts(review: ExternalLLMReview | None) -> bool:
    return bool(review is not None and review.reason_code == "insufficient_facts")


def non_silent_boundary_reply(intent: str) -> str:
    if intent == "non_russian":
        return "Я отвечаю на русском. Напишите вопрос по-русски, и я постараюсь помочь по ТГ РАДАР."
    if intent == "spam":
        return "Похоже, это рекламное сообщение. Если у вас есть вопрос по ТГ РАДАР, напишите его, и я отвечу."
    return "Не совсем понял сообщение. Напишите вопрос чуть подробнее, и я постараюсь помочь."


def soft_negative_acknowledgement_text() -> str:
    return "Понял, не буду настаивать. Если позже появится вопрос по ТГ РАДАР, напишите, и я отвечу."


def supplier_offer_boundary_reply() -> str:
    return (
        "Спасибо за предложение. Скажу честно: подтвержденного заказа на эту услугу "
        "у нас сейчас нет. Я могу помочь по ТГ РАДАР и поиску сигналов спроса. "
        "Если это будет актуально, напишите, и я расскажу подробнее."
    )


def opt_out_clarification_text() -> str:
    return (
        "Правильно понимаю, что вы хотите, чтобы мы больше не писали? "
        "Если да, ответьте «стоп», и я сразу остановлю сообщения."
    )


def validate_claimed_used_sources(
    chunks: List[KnowledgeChunk],
    claimed_sources: List[str],
) -> tuple[List[str], List[str]]:
    """Validate model-reported citations only against chunks it actually saw."""

    available_headings: Dict[str, set[str]] = {}
    for chunk in chunks:
        available_headings.setdefault(chunk.source, set()).add(normalize_text(chunk.heading))
    validated: List[str] = []
    invalid: List[str] = []
    for claimed in dedupe_strings(claimed_sources):
        normalized_claim = str(claimed or "").strip().replace("\\", "/")
        while normalized_claim.startswith("./"):
            normalized_claim = normalized_claim[2:]
        if normalized_claim.startswith("knowledge_base/"):
            normalized_claim = normalized_claim[len("knowledge_base/") :]
        source, separator, heading = normalized_claim.partition("#")
        source = source.strip()
        heading_is_valid = (
            not separator
            or normalize_text(heading) in available_headings.get(source, set())
        )
        if source in available_headings and heading_is_valid:
            if source not in validated:
                validated.append(source)
        else:
            invalid.append(claimed)
    return validated, invalid


def recent_messages(conn: sqlite3.Connection, conversation_id: str, limit: int) -> List[Dict[str, str]]:
    rows = conn.execute(
        """
        SELECT direction, sender_type, intent, text, created_at
        FROM messages
        WHERE conversation_id = ?
        ORDER BY rowid DESC
        LIMIT ?
        """,
        (conversation_id, limit),
    ).fetchall()
    return [
        {
            "direction": row["direction"],
            "sender_type": row["sender_type"],
            "intent": row["intent"] or "",
            "text": row["text"],
            "created_at": row["created_at"],
        }
        for row in reversed(rows)
    ]


def count_auto_replies(history: List[Dict[str, str]]) -> int:
    return sum(
        1
        for item in history
        if item["direction"] == "outbound" and item["intent"] in {"faq", "auto_reply"}
    )


def count_auto_replies_for_conversation(conn: sqlite3.Connection, conversation_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM messages
        WHERE conversation_id = ?
          AND direction = 'outbound'
          AND intent IN ('faq', 'auto_reply')
        """,
        (conversation_id,),
    ).fetchone()
    return int(row["count"] or 0)


def should_offer_manager(
    auto_reply_count: int,
    manager_nudge_after_replies: int,
) -> bool:
    return manager_nudge_after_replies > 0 and auto_reply_count >= manager_nudge_after_replies


def retrieve_presales_chunks(
    conn: sqlite3.Connection,
    conversation: sqlite3.Row,
    recipient: sqlite3.Row,
    inbound_text: str,
    classification: MessageClassification,
    history: List[Dict[str, str]],
    kb_root: str,
) -> List[KnowledgeChunk]:
    history_text = " ".join(item["text"] for item in history[-6:])
    route = route_presales_query(
        inbound_text=inbound_text,
        classification=classification,
        kb_root=kb_root,
    )
    expanded_query = " ".join(
        [
            history_text,
            inbound_text,
            classification.intent,
            intent_keywords(classification.intent, inbound_text),
            route.search_terms,
            str(recipient["segment"] or ""),
            str(recipient["company"] or ""),
            "ТГ РАДАР сервис инструмент лиды сигналы спроса Telegram бесплатный доступ демо тест",
        ]
    )
    chunks = retrieve_knowledge_chunks(
        conn,
        campaign_id=conversation["campaign_id"],
        query=expanded_query,
        kb_root=kb_root,
        limit=8,
    )
    chunks = routed_customer_facing_chunks(
        chunks,
        route.answer_pack_sources,
        route.answer_card_sources,
    )
    if not chunks:
        chunks = retrieve_knowledge_chunks(
            conn,
            campaign_id=conversation["campaign_id"],
            query="как работает сервис что такое сигнал спроса бесплатный доступ демо тарифы ограничения",
            kb_root=kb_root,
            limit=8,
        )
        chunks = routed_customer_facing_chunks(
            chunks,
            route.answer_pack_sources,
            route.answer_card_sources,
        )
    pack_chunks = answer_pack_presales_chunks(route.answer_pack_sources, kb_root=kb_root)
    card_chunks = answer_card_presales_chunks(route.answer_card_sources, kb_root=kb_root)
    hint_chunks = source_hint_presales_chunks(route.source_hints, kb_root=kb_root)
    if pack_chunks or card_chunks or hint_chunks:
        chunks = merge_knowledge_chunks(
            pack_chunks,
            merge_knowledge_chunks(
                card_chunks,
                merge_knowledge_chunks(hint_chunks, chunks, limit=PRESALES_CHUNK_LIMIT),
                limit=PRESALES_CHUNK_LIMIT,
            ),
            limit=PRESALES_CHUNK_LIMIT,
        )
    if has_case_or_proof_terms(normalize_text(inbound_text)):
        chunks = merge_knowledge_chunks(
            proof_presales_chunks(
                conn,
                conversation["campaign_id"],
                query=inbound_text,
                kb_root=kb_root,
            ),
            chunks,
            limit=PRESALES_CHUNK_LIMIT,
        )
    if has_service_terms(normalize_text(inbound_text)):
        chunks = merge_knowledge_chunks(
            service_presales_chunks(kb_root=kb_root),
            chunks,
            limit=PRESALES_CHUNK_LIMIT,
        )
    if classification.intent == "pricing_question" or has_pricing_terms(
        normalize_text(inbound_text)
    ):
        chunks = merge_knowledge_chunks(
            pricing_presales_chunks(kb_root=kb_root),
            chunks,
            limit=PRESALES_CHUNK_LIMIT,
        )
    if has_partner_terms(normalize_text(inbound_text)):
        chunks = merge_knowledge_chunks(
            partner_presales_chunks(kb_root=kb_root),
            chunks,
            limit=PRESALES_CHUNK_LIMIT,
        )
    if has_blog_terms(normalize_text(inbound_text)):
        chunks = merge_knowledge_chunks(
            blog_presales_chunks(kb_root=kb_root),
            chunks,
            limit=PRESALES_CHUNK_LIMIT,
        )
    if has_sector_terms(normalize_text(inbound_text)):
        chunks = merge_knowledge_chunks(
            sector_presales_chunks(inbound_text, kb_root=kb_root),
            chunks,
            limit=PRESALES_CHUNK_LIMIT,
        )
    route_required_chunks = route_required_presales_chunks(
        pack_chunks=pack_chunks,
        card_chunks=card_chunks,
        hint_chunks=hint_chunks,
    )
    if has_out_of_scope_service_terms(normalize_text(inbound_text)):
        route_required_chunks = merge_knowledge_chunks(
            route_required_chunks,
            out_of_scope_service_presales_chunks(kb_root=kb_root),
            limit=PRESALES_CHUNK_LIMIT,
        )
    if route_required_chunks:
        chunks = ensure_required_presales_chunks(
            chunks,
            route_required_chunks,
            limit=PRESALES_CHUNK_LIMIT,
        )
    return merge_knowledge_chunks(
        chunks,
        always_include_presales_chunks(conn, conversation["campaign_id"], kb_root=kb_root),
    )


def route_presales_query(
    inbound_text: str,
    classification: MessageClassification,
    kb_root: str,
) -> PresalesKnowledgeRoute:
    normalized = normalize_text(inbound_text)
    topic_ids: List[str] = []
    route_terms: List[str] = [classification.intent]
    if classification.intent == "pricing_question" or has_pricing_terms(normalized):
        topic_ids.extend(["pricing", "free_test"])
    if has_guarantee_terms(normalized):
        topic_ids.extend(["guarantees", "safety"])
    if has_review_terms(normalized):
        topic_ids.extend(["proof", "reviews"])
    if has_signal_example_text_terms(normalized):
        topic_ids.extend(["proof", "signal_examples"])
    if has_case_text_terms(normalized):
        topic_ids.extend(["proof", "cases"])
    if has_service_terms(normalized):
        topic_ids.extend(["services", "service_scenarios"])
    if has_contact_source_terms(normalized):
        topic_ids.extend(["contact_source", "consent", "company"])
    if has_outreach_surface_terms(normalized):
        topic_ids.extend(["outreach_surfaces", "product"])
    if has_partner_terms(normalized):
        topic_ids.append("partner_program")
    if has_blog_terms(normalized):
        topic_ids.append("blog")
    if has_sector_terms(normalized):
        topic_ids.extend(["sector_fit", "sectors"])
    if has_legal_sensitive_terms(normalized):
        topic_ids.extend(["legal_sensitive", "handoff"])
    if has_site_or_presentation_terms(normalized):
        topic_ids.extend(["site_links", "company"])
    if has_identity_terms(normalized):
        topic_ids.extend(["company", "product"])
    if not topic_ids:
        topic_ids.extend(["product", "faq"])

    pack_query = " ".join([inbound_text, classification.intent, *topic_ids])
    pack_matches = answer_pack_matches_for_query(
        kb_root=kb_root,
        query=pack_query,
        limit=PRESALES_ANSWER_PACK_LIMIT,
    )
    answer_pack_sources: List[str] = []
    answer_card_sources: List[str] = []
    source_hints: List[str] = []
    for match in pack_matches:
        if match.source not in answer_pack_sources:
            answer_pack_sources.append(match.source)
        for hint in match.source_hints:
            if hint not in source_hints:
                source_hints.append(hint)
    card_matches = answer_card_matches_for_query(
        kb_root=kb_root,
        query=pack_query,
        limit=PRESALES_ANSWER_CARD_LIMIT,
    )
    for match in card_matches:
        if match.source not in answer_card_sources:
            answer_card_sources.append(match.source)
        for hint in match.source_hints:
            if hint not in source_hints:
                source_hints.append(hint)
    route_terms.extend(topic_ids)
    route_terms.extend(source_hints)
    route_terms.extend(answer_pack_sources)
    route_terms.extend(answer_card_sources)
    return PresalesKnowledgeRoute(
        topic_ids=dedupe_strings(topic_ids),
        answer_pack_sources=answer_pack_sources,
        answer_card_sources=answer_card_sources,
        source_hints=source_hints,
        search_terms=" ".join(dedupe_strings(route_terms)),
    )


def always_include_presales_chunks(
    conn: sqlite3.Connection,
    campaign_id: str,
    kb_root: str,
) -> List[KnowledgeChunk]:
    campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if campaign is None:
        return []
    root = Path(kb_root).resolve()
    manifest = load_kb_manifest(root)
    result: List[KnowledgeChunk] = []
    for source in PRESALES_ALWAYS_INCLUDE_SOURCES:
        path = resolve_knowledge_path(root, source)
        if path is None or not path.exists() or not path.is_file():
            continue
        source_id = str(path.relative_to(root))
        metadata = source_metadata(manifest, source_id)
        if not is_customer_facing_source(source_id, metadata):
            continue
        for heading, text in split_markdown_chunks(path.read_text(encoding="utf-8")):
            normalized = normalize_chunk_text(text, max_chars=520)
            if not normalized:
                continue
            result.append(
                KnowledgeChunk(
                    source=source_id,
                    heading=heading,
                    text=normalized,
                    score=1,
                    metadata=metadata,
                )
            )
            break
    return result


def proof_presales_chunks(
    conn: sqlite3.Connection,
    campaign_id: str,
    query: str,
    kb_root: str,
) -> List[KnowledgeChunk]:
    campaign = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
    if campaign is None:
        return []
    root = Path(kb_root).resolve()
    manifest = load_kb_manifest(root)
    result: List[KnowledgeChunk] = []
    query_terms = token_set(query)
    for source in PRESALES_PROOF_INCLUDE_SOURCES:
        path = resolve_knowledge_path(root, source)
        if path is None or not path.exists() or not path.is_file():
            continue
        source_id = str(path.relative_to(root))
        metadata = source_metadata(manifest, source_id)
        if not is_customer_facing_source(source_id, metadata):
            continue
        source_chunks = list(split_markdown_chunks(path.read_text(encoding="utf-8")))
        for heading, text in split_markdown_chunks(path.read_text(encoding="utf-8")):
            if not should_include_proof_chunk(source, heading, query_terms):
                continue
            normalized = normalize_chunk_text(text, max_chars=620)
            if not normalized:
                continue
            result.append(
                KnowledgeChunk(
                            source=source_id,
                            heading=heading,
                            text=normalized,
                            score=proof_chunk_score(source, heading, query_terms),
                            metadata=metadata,
                        )
                    )
        if source == "case_studies.md" and not any(
            chunk.source == "case_studies.md" for chunk in result
        ):
            for heading, text in source_chunks[:1]:
                normalized = normalize_chunk_text(text, max_chars=620)
                if normalized:
                    result.append(
                        KnowledgeChunk(
                            source=source_id,
                            heading=heading,
                            text=normalized,
                            score=90,
                            metadata=metadata,
                        )
                    )
    return sorted(result, key=lambda item: (-item.score, item.source, item.heading))


def answer_pack_presales_chunks(
    sources: List[str],
    kb_root: str,
) -> List[KnowledgeChunk]:
    result: List[KnowledgeChunk] = []
    for source in sources:
        selected = selected_source_chunks(
            kb_root=kb_root,
            source=source,
            headings=PRESALES_ANSWER_PACK_RESPONSE_HEADINGS,
            score=145,
            max_chars=620,
            limit=3,
        )
        combined = combine_routed_source_chunks(selected, "Answer Pack")
        if combined is not None:
            result.append(combined)
    return result


def answer_card_presales_chunks(
    sources: List[str],
    kb_root: str,
) -> List[KnowledgeChunk]:
    result: List[KnowledgeChunk] = []
    for source in sources:
        selected = selected_source_chunks(
            kb_root=kb_root,
            source=source,
            headings={
                "Must Say",
                "Must Not Say",
                "Handoff Trigger",
                "Example Reply Short",
                "Example Reply: Block Risk",
                "Example Reply: Out of Scope",
            },
            score=143,
            max_chars=680,
            limit=5,
        )
        combined = combine_routed_source_chunks(selected, "Answer Card")
        if combined is not None:
            result.append(combined)
    return result


def combine_routed_source_chunks(
    chunks: List[KnowledgeChunk],
    heading: str,
) -> KnowledgeChunk | None:
    if not chunks:
        return None
    text = "\n\n".join(f"## {chunk.heading}\n{chunk.text}" for chunk in chunks)
    return KnowledgeChunk(
        source=chunks[0].source,
        heading=heading,
        text=text,
        score=max(chunk.score for chunk in chunks),
        metadata=chunks[0].metadata,
    )


def source_hint_presales_chunks(
    sources: List[str],
    kb_root: str,
) -> List[KnowledgeChunk]:
    result: List[KnowledgeChunk] = []
    for source in sources:
        if not is_customer_facing_source(source) and source not in PRESALES_INTERNAL_HINT_SOURCES:
            continue
        result.extend(
            selected_source_chunks(
                kb_root=kb_root,
                source=source,
                headings=None,
                score=92,
                max_chars=520,
                limit=1,
            )
        )
    return result


def route_required_presales_chunks(
    pack_chunks: List[KnowledgeChunk],
    card_chunks: List[KnowledgeChunk],
    hint_chunks: List[KnowledgeChunk],
) -> List[KnowledgeChunk]:
    support_priority = {
        "fact_registry.md": 0,
        "allowed_claims.md": 0,
        "forbidden_claims.md": 0,
        "service_scenarios.md": 1,
        "partner_program.md": 1,
        "case_studies.md": 2,
        "reviews.md": 2,
        "examples/signal_examples.md": 2,
        "site_pages.md": 3,
        "product_overview.md": 4,
        "reply_few_shots.md": 7,
    }
    support_chunks = [
        chunk
        for chunk in hint_chunks
        if chunk.source
        in support_priority
    ]
    support_chunks = sorted(
        support_chunks,
        key=lambda chunk: (support_priority[chunk.source], chunk.source, chunk.heading),
    )
    return merge_knowledge_chunks(
        pack_chunks,
        merge_knowledge_chunks(
            card_chunks,
            support_chunks,
            limit=8,
        ),
        limit=10,
    )


def ensure_required_presales_chunks(
    chunks: List[KnowledgeChunk],
    required_chunks: List[KnowledgeChunk],
    limit: int,
) -> List[KnowledgeChunk]:
    required_keys = {(chunk.source, chunk.heading) for chunk in required_chunks}
    result = merge_knowledge_chunks(chunks, [], limit=limit)
    seen = {(chunk.source, chunk.heading) for chunk in result}
    for required in required_chunks:
        key = (required.source, required.heading)
        if key in seen:
            continue
        if len(result) >= limit:
            drop_index = None
            for index in range(len(result) - 1, -1, -1):
                candidate_key = (result[index].source, result[index].heading)
                if candidate_key not in required_keys:
                    drop_index = index
                    break
            if drop_index is None:
                break
            dropped = result.pop(drop_index)
            seen.discard((dropped.source, dropped.heading))
        result.append(required)
        seen.add(key)
    return result[:limit]


def should_include_proof_chunk(source: str, heading: str, query_terms: set[str]) -> bool:
    normalized_heading = normalize_text(heading)
    if source == "case_studies.md":
        if has_review_query_terms(query_terms) and not has_case_query_terms(query_terms):
            return False
        if normalized_heading in {"где посмотреть кейсы", "важное ограничение по кейсам"}:
            return True
        if "авто" in query_terms and "авто" in token_set(heading):
            return True
        if "китай" in query_terms and "china" in normalized_heading:
            return True
        return False
    if source == "faq.md":
        return normalized_heading in {
            "где посмотреть кейсы?",
            "какие кейсы есть?",
            "можно посмотреть примеры?",
            "есть ли отзывы?",
        }
    if source == "reviews.md":
        if not has_review_query_terms(query_terms):
            return False
        return normalized_heading in {
            "где посмотреть отзывы",
            "что можно говорить по отзывам",
            "публичные proof points страницы отзывов",
            "сценарии из отзывов",
            "ограничение по отзывам",
        }
    if source == "examples/signal_examples.md":
        if not has_signal_example_query_terms(query_terms):
            return False
        return normalized_heading in {
            "что показывает пример сигнала",
            "общие обезличенные примеры с сайта",
            "как отвечать на просьбу показать пример",
        }
    if source == "company_profile.md":
        return normalized_heading in {"полезные ссылки", "ограничения"}
    if source == "site_pages.md":
        return normalized_heading in {"proof points сайта", "как отвечать ссылками"}
    return False


def proof_chunk_score(source: str, heading: str, query_terms: set[str]) -> int:
    normalized_heading = normalize_text(heading)
    if source == "reviews.md" and has_review_query_terms(query_terms):
        return 140
    if source == "examples/signal_examples.md" and has_signal_example_query_terms(query_terms):
        return 138
    if source == "case_studies.md" and normalized_heading == "где посмотреть кейсы":
        return 130
    if source == "faq.md" and normalized_heading == "какие кейсы есть?":
        return 125
    if source == "faq.md" and normalized_heading == "где посмотреть кейсы?":
        return 120
    if source == "case_studies.md" and "авто" in query_terms and "авто" in token_set(heading):
        return 118
    if source == "reviews.md" and normalized_heading == "где посмотреть отзывы":
        return 124
    if source == "reviews.md" and normalized_heading == "что можно говорить по отзывам":
        return 122
    if source == "examples/signal_examples.md" and normalized_heading == "общие обезличенные примеры с сайта":
        return 123
    if source == "examples/signal_examples.md" and normalized_heading == "что показывает пример сигнала":
        return 121
    if source == "site_pages.md" and normalized_heading == "как отвечать ссылками":
        return 112
    if source == "site_pages.md" and normalized_heading == "proof points сайта":
        return 110
    return 100


def has_case_query_terms(query_terms: set[str]) -> bool:
    return bool({"кейс", "кейсы"} & query_terms)


def has_review_query_terms(query_terms: set[str]) -> bool:
    return bool({"отзыв", "отзывы", "яндекс"} & query_terms)


def has_signal_example_query_terms(query_terms: set[str]) -> bool:
    return bool({"пример", "примеры", "сигнал", "сигналы"} & query_terms) or any(
        term.startswith("покаж") for term in query_terms
    )


def token_set(text: str) -> set[str]:
    return set(re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9]{3,}", normalize_text(text)))


def customer_facing_chunks(chunks: List[KnowledgeChunk]) -> List[KnowledgeChunk]:
    return [
        chunk
        for chunk in chunks
        if is_customer_facing_source(chunk.source, chunk.metadata)
    ]


def routed_customer_facing_chunks(
    chunks: List[KnowledgeChunk],
    routed_answer_pack_sources: List[str],
    routed_answer_card_sources: List[str],
) -> List[KnowledgeChunk]:
    routed_packs = set(routed_answer_pack_sources)
    routed_cards = set(routed_answer_card_sources)
    return [
        chunk
        for chunk in customer_facing_chunks(chunks)
        if not chunk.source.startswith("answer_packs/") or chunk.source in routed_packs
        if not chunk.source.startswith("answer_cards/") or chunk.source in routed_cards
    ]


def service_presales_chunks(kb_root: str) -> List[KnowledgeChunk]:
    return selected_source_chunks(
        kb_root=kb_root,
        source="service_scenarios.md",
        headings={
            "Общая логика услуг",
            "Как выбирать сценарий в ответе",
            "Что не входит в услугу",
            "Лиды для B2B",
            "Лиды для B2C",
            "Мониторинг для отдела продаж",
        },
        score=115,
        max_chars=560,
    )


def out_of_scope_service_presales_chunks(kb_root: str) -> List[KnowledgeChunk]:
    return selected_source_chunks(
        kb_root=kb_root,
        source="service_scenarios.md",
        headings={"Что не входит в услугу"},
        score=144,
        max_chars=620,
        limit=1,
    )


def pricing_presales_chunks(kb_root: str) -> List[KnowledgeChunk]:
    chunks = selected_source_chunks(
        kb_root=kb_root,
        source="pricing_policy.md",
        headings={
            "Сколько стоит публично",
            "Публичные тарифы сайта",
            "Лимиты тарифов с сайта",
            "Общие элементы тарифов",
            "Что требует менеджера",
        },
        score=118,
        max_chars=580,
    )
    heading_priority = {
        "Сколько стоит публично": 0,
        "Лимиты тарифов с сайта": 1,
        "Общие элементы тарифов": 2,
        "Публичные тарифы сайта": 3,
        "Что требует менеджера": 4,
    }
    return sorted(
        chunks,
        key=lambda chunk: (heading_priority.get(chunk.heading, 99), chunk.heading),
    )


def partner_presales_chunks(kb_root: str) -> List[KnowledgeChunk]:
    return selected_source_chunks(
        kb_root=kb_root,
        source="partner_program.md",
        headings=None,
        score=118,
        max_chars=560,
        limit=5,
    )


def blog_presales_chunks(kb_root: str) -> List[KnowledgeChunk]:
    return selected_source_chunks(
        kb_root=kb_root,
        source="blog_content.md",
        headings=None,
        score=116,
        max_chars=560,
        limit=4,
    )


def sector_presales_chunks(inbound_text: str, kb_root: str) -> List[KnowledgeChunk]:
    normalized = normalize_text(inbound_text)
    sources: List[str] = []
    if any(marker in normalized for marker in ("медицин", "клиник", "врач")):
        sources.append("sector_notes/medicine.md")
    if any(marker in normalized for marker in ("crm", "црм", "1с", "амо", "amo")):
        sources.append("sector_notes/crm_1c.md")
    if any(marker in normalized for marker in ("банкрот", "юрист", "долг")):
        sources.append("sector_notes/legal_bankruptcy.md")
    if any(marker in normalized for marker in ("виз", "тур", "путешеств")):
        sources.append("sector_notes/tourism_visas.md")
    if has_auto_sector_terms(normalized):
        sources.append("sector_notes/auto_china.md")
    if any(marker in normalized for marker in ("китай", "вед", "логист", "тамож", "достав")):
        sources.append("sector_notes/logistics_ved.md")
    if any(marker in normalized for marker in ("недвиж", "ремонт", "квартир", "риелт")):
        sources.append("sector_notes/real_estate_repair.md")
    if any(marker in normalized for marker in ("маркет", "smm", "seo", "реклам", "агентств")):
        sources.append("sector_notes/marketing.md")
    if not sources:
        sources = [
            "site_pages.md",
            "company_profile.md",
        ]
    result: List[KnowledgeChunk] = []
    for source in sources:
        result.extend(
            selected_source_chunks(
                kb_root=kb_root,
                source=source,
                headings=None,
                score=116,
                max_chars=560,
                limit=3,
            )
        )
    return result


def selected_source_chunks(
    kb_root: str,
    source: str,
    headings: set[str] | None,
    score: int,
    max_chars: int,
    limit: int = 5,
) -> List[KnowledgeChunk]:
    root = Path(kb_root).resolve()
    path = resolve_knowledge_path(root, source)
    if path is None or not path.exists() or not path.is_file():
        return []
    manifest = load_kb_manifest(root)
    source = str(path.relative_to(root))
    result: List[KnowledgeChunk] = []
    for heading, text in split_markdown_chunks(path.read_text(encoding="utf-8")):
        if headings is not None and heading not in headings:
            continue
        normalized = normalize_chunk_text(text, max_chars=max_chars)
        if not normalized:
            continue
        result.append(
            KnowledgeChunk(
                source=source,
                heading=heading,
                text=normalized,
                score=score,
                metadata=source_metadata(manifest, source),
            )
        )
        if len(result) >= limit:
            break
    return result


def merge_knowledge_chunks(
    primary: List[KnowledgeChunk],
    required: List[KnowledgeChunk],
    limit: int = 12,
) -> List[KnowledgeChunk]:
    result: List[KnowledgeChunk] = []
    seen = set()
    for chunk in [*primary, *required]:
        key = (chunk.source, chunk.heading)
        if key in seen:
            continue
        seen.add(key)
        result.append(chunk)
        if len(result) >= limit:
            break
    return result


def intent_keywords(intent: str, inbound_text: str) -> str:
    normalized = normalize_text(inbound_text)
    if intent == "greeting":
        return "назначение что это бесплатный доступ заявки проверка спроса"
    if intent == "positive":
        return "интересно бесплатный доступ демо менеджер заявки проверка спроса"
    if intent == "pricing_question":
        return "цена стоимость тариф коммерческие условия от чего зависит"
    if intent == "demo_question":
        return "демо бесплатный доступ система менеджер подключение"
    if has_case_or_proof_terms(normalized):
        return "кейсы отзывы результаты примеры клиентов публичные кейсы сайт авто ВЭД маркетинг туризм недвижимость IT примеры сигналов"
    if "пример" in normalized:
        return "примеры сигналов кейсы бесплатный доступ демо система"
    if has_service_terms(normalized):
        return "услуги сценарии лиды B2B B2C мониторинг продажи репутация конкуренты рынок тренды инфоповоды сигналы спроса рабочий чат Telegram-CRM CRM таблица передача сигнала"
    if has_review_terms(normalized):
        return "отзывы клиенты социальное доказательство теплые сигналы сайт Яндекс"
    if has_partner_terms(normalized):
        return "партнерская программа реферальная программа партнер 10% выплаты рекомендации клиенты"
    if has_blog_terms(normalized):
        return "блог статьи почитать материалы лиды без рекламы B2B B2C Telegram бизнес сигналы спроса"
    if has_sector_terms(normalized):
        return "сферы ниши отрасли ВЭД Китай авто банкротство визы медицина недвижимость 1С CRM туризм маркетинг"
    if has_guarantee_terms(normalized):
        return "гарантии безопасность блокировка юридические последствия лиды продажи ограничения"
    if "работ" in normalized or "устро" in normalized:
        return "как работает источники ключевые слова фильтрация"
    if "подходит" in normalized or "ниш" in normalized:
        return "кому подходит ниша сфера"
    if "тест" in normalized or "демо" in normalized:
        return "бесплатный доступ демо система менеджер формат"
    return "продукт сервис инструмент механика отличие от рекламы"


def is_inbound_discovery_conversation(
    conversation: sqlite3.Row,
    recipient: sqlite3.Row,
) -> bool:
    return (
        str(conversation["campaign_id"] or "") == INBOUND_REFERRAL_CAMPAIGN_ID
        and str(recipient["recipient_type"] or "") == "user"
    )


def presales_entry_mode(
    conversation: sqlite3.Row,
    recipient: sqlite3.Row,
    sender_account: Dict[str, str],
) -> str:
    if not is_inbound_discovery_conversation(conversation, recipient):
        return "post_first_touch"
    if str(sender_account.get("role") or "") == "chat_sender":
        return CHAT_SENDER_PRIVATE_ENTRY_MODE
    return "inbound_private_without_first_touch"


def recent_public_chat_outreach_context(
    conn: sqlite3.Connection,
    sender_account_id: str,
    reference_at: str,
    *,
    limit: int = RECENT_PUBLIC_CHAT_CONTEXT_LIMIT,
    max_age_days: int = RECENT_PUBLIC_CHAT_CONTEXT_DAYS,
) -> List[Dict[str, str]]:
    if not sender_account_id or not reference_at or limit <= 0 or max_age_days <= 0:
        return []
    rows = conn.execute(
        """
        SELECT id, chat_username, chat_title, text, campaign_id, source_run_id, sent_at
        FROM public_chat_messages
        WHERE sender_account_id = ?
          AND delivery_status = 'sent'
          AND is_test = 0
          AND sent_at IS NOT NULL
          AND datetime(sent_at) <= datetime(?)
          AND datetime(sent_at) >= datetime(?, ?)
        ORDER BY datetime(sent_at) DESC, id DESC
        LIMIT ?
        """,
        (
            sender_account_id,
            reference_at,
            reference_at,
            f"-{int(max_age_days)} days",
            int(limit),
        ),
    ).fetchall()
    return [
        {
            "public_chat_message_id": str(row["id"] or ""),
            "chat_username": str(row["chat_username"] or ""),
            "chat_title": str(row["chat_title"] or ""),
            "text": str(row["text"] or ""),
            "campaign_id": str(row["campaign_id"] or ""),
            "source_run_id": str(row["source_run_id"] or ""),
            "sent_at": str(row["sent_at"] or ""),
        }
        for row in rows
    ]


def load_presales_context(conversation: sqlite3.Row) -> Dict[str, str]:
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


def store_presales_context(
    conn: sqlite3.Connection,
    conversation_id: str,
    updates: Dict[str, str],
) -> None:
    allowed_fields = {
        "sector",
        "sector_status",
        "inbound_need",
        "referral_source",
        "priority_service",
        "geo",
        "signal_type",
    }
    row = conn.execute(
        "SELECT presales_context FROM conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return
    try:
        current = json.loads(str(row["presales_context"] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    for key, value in updates.items():
        clean_key = str(key).strip()
        clean_value = str(value).strip()
        if clean_key in allowed_fields and clean_value:
            current[clean_key] = clean_value
    conn.execute(
        "UPDATE conversations SET presales_context = ? WHERE id = ?",
        (json.dumps(current, ensure_ascii=False, sort_keys=True), conversation_id),
    )


def build_llm_context(
    conversation: sqlite3.Row,
    recipient: sqlite3.Row,
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


def inbound_discovery_fallback_reply(
    inbound_text: str,
    classification: MessageClassification,
    history: List[Dict[str, str]],
    chunks: List[KnowledgeChunk],
    entry_mode: str = "inbound_private_without_first_touch",
) -> str:
    normalized = normalize_text(inbound_text)
    if entry_mode == CHAT_SENDER_PRIVATE_ENTRY_MODE:
        return chat_sender_inbound_fallback_reply(normalized, history)
    referral_intro = any(
        marker in normalized
        for marker in (
            "передали ваш контакт",
            "дали ваш контакт",
            "поделились вашим контактом",
            "посоветовали написать",
        )
    )
    short_intro = len(normalized.split()) <= 10
    if classification.intent == "greeting" or normalized.strip(" .!?") in {
        "здравствуйте",
        "добрый день",
        "добрый вечер",
        "привет",
    }:
        if referral_intro:
            return (
                "Здравствуйте. Да, слушаю. Подскажите, пожалуйста, по какому вопросу "
                "вам передали контакт?"
            )
        return "Здравствуйте. Подскажите, пожалуйста, чем могу помочь?"
    if referral_intro and short_intro:
        return (
            "Здравствуйте. Да, слушаю. Подскажите, пожалуйста, по какому вопросу "
            "вам передали контакт?"
        )
    if any(marker in normalized for marker in ("не хочу говорить", "не скажу", "без сферы")):
        return (
            "Без проблем, можем обсудить без привязки к сфере. Сервис помогает находить "
            "в Telegram сообщения, где люди уже ищут услугу, товар или подрядчика. "
            "Хотите бесплатно посмотреть, как это работает?"
        )
    specific = fallback_reply(inbound_text, classification, history, chunks)
    if specific:
        return specific
    return (
        "Да, такую задачу тоже можно проверить. Сервис находит в Telegram сообщения с уже "
        "проявленным спросом и помогает отделить их от рекламы и шума. Можем бесплатно "
        "показать систему на вашей задаче. Хотите посмотреть?"
    )


def is_clear_soft_negative(inbound_text: str) -> bool:
    normalized = normalize_text(inbound_text)
    return any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        for pattern in (
            r"\b(?:мне |нам |пока )?не ?интересно\b",
            r"\bне ?актуально\b",
            r"\b(?:нам|мне) (?:это )?не (?:нужно|надо)\b",
            r"\bоткажусь\b",
            r"\bне хочу (?:тест|демо|сервис|продолжать|обсуждать)\b",
        )
    )


def is_redirect_only_without_product_interest(inbound_text: str) -> bool:
    """Reject a handoff when the reply only redirects an unrelated vacancy/ad."""
    normalized = normalize_text(inbound_text)
    redirect_only = any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        for pattern in (
            r"\bваканси\w*.{0,40}\bактуальн\w*",
            r"\bобраща(?:йся|йтесь)\b.{0,100}(?:контакт\w*|@[a-z0-9_]{5,32})",
            r"\b(?:напиши|пишите|напишите|свяжитесь)\b.{0,100}@[a-z0-9_]{5,32}",
            r"\bконтактн\w*\s+данн\w*.{0,80}\bобъявлен\w*",
            r"\bуказанн\w*.{0,80}\b(?:объявлен\w*|контакт\w*)",
        )
    )
    if not redirect_only:
        return False
    return not _contains_tg_radar_product_interest(normalized)


def is_unsolicited_supplier_call_offer_without_product_interest(
    inbound_text: str,
    history: List[Dict[str, str]],
) -> bool:
    """Block seller-as-lead false positives before a product conversation exists."""

    current = normalize_text(inbound_text)
    inbound_history = normalize_text(
        " ".join(
            str(item.get("text") or "")
            for item in history
            if item.get("direction") == "inbound"
        )
    )
    seller_offer = any(
        re.search(pattern, inbound_history, flags=re.IGNORECASE)
        for pattern in (
            r"\bмы\s+(?:оказываем|предлагаем|делаем|занимаемся|готовы\s+оказать)\b",
            r"\b(?:оказываем|предлагаем)\w*\s+услуг\w*",
            r"\bуслуг\w*\s+(?:полного|полног[оа])\s+цикл\w*",
            r"\b(?:можем|готовы)\w*\s+(?:помочь|подключиться|выполнить)\b",
        )
    )
    call_offer = any(
        re.search(pattern, current, flags=re.IGNORECASE)
        for pattern in (
            r"\b(?:созвон|позвон|звонок)\w*",
            r"\b(?:удобно|можем|давайте)\b.{0,50}\b(?:обсудить|встретиться)\b",
        )
    )
    product_interest = _contains_tg_radar_product_interest(inbound_history)
    prior_product_reply = any(
        item.get("direction") == "outbound"
        and re.search(
            r"\b(?:(?:tg|тг)\s*radar|сервис|продукт|демо|бесплатн\w*\s+тест|доступ)\b",
            normalize_text(str(item.get("text") or "")),
            flags=re.IGNORECASE,
        )
        for item in history
    )
    return seller_offer and call_offer and not product_interest and not prior_product_reply


def _contains_tg_radar_product_interest(normalized: str) -> bool:
    return any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        for pattern in (
            r"\b(?:tg|тг)\s*radar\b",
            r"\b(?:ваш|этот)\s+(?:сервис|продукт|инструмент)\b",
            r"\b(?:демо|тест|доступ|тариф|стоимост)\w*\b",
            r"\b(?:интересн|покаж|расскаж)\w*.{0,60}\b(?:сервис|систем|продукт|инструмент|демо|тест)\w*",
            r"\bкак\s+(?:это\s+)?работает\b",
            r"\b(?:подключите|передайте)\b.{0,40}\bменеджер\w*",
        )
    )


def is_explicit_post_handoff_action_request(inbound_text: str) -> bool:
    normalized = normalize_text(inbound_text)
    return any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        for pattern in (
            r"\b(?:менеджер|человек|оператор)\w*\b",
            r"\b(?:созвон|позвон|звонок|встреч)\w*\b",
            r"\b(?:договор|сч[её]т|оплат|скидк|доступ|запуск|старт\s+тест)\w*\b",
            r"\bподтвержда\w*\b",
        )
    )


def is_likely_quoted_public_chat_request(
    inbound_text: str,
    history: List[Dict[str, str]],
    recent_public_chat_outreach: List[Dict[str, str]],
) -> bool:
    normalized = normalize_text(inbound_text)
    if len(normalized) < 40:
        return False
    copied_from_public = any(
        _texts_substantially_match(normalized, normalize_text(str(item.get("text") or "")))
        for item in recent_public_chat_outreach
    )
    if not copied_from_public:
        return False
    preceding_referral = any(
        item.get("direction") == "inbound"
        and re.search(
            r"\b(?:в\s+чате\s+увидел|увидел\w*\s+(?:ваше\s+)?сообщение|по\s+сообщению\s+в\s+чате)\b",
            normalize_text(str(item.get("text") or "")),
            flags=re.IGNORECASE,
        )
        for item in history[:-1]
    )
    return copied_from_public and preceding_referral


def _texts_substantially_match(left: str, right: str) -> bool:
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = sorted((left, right), key=len)
    return len(shorter) >= 40 and shorter in longer and len(shorter) / len(longer) >= 0.8


def chat_sender_inbound_fallback_reply(
    normalized: str,
    history: List[Dict[str, str]],
) -> str:
    compact = normalized.strip(" .!?")
    if compact in {"здравствуйте", "добрый день", "добрый вечер", "привет"}:
        return (
            "Здравствуйте. Вы по сообщению в чате? Подскажите, пожалуйста, "
            "по какой задаче пишете?"
        )
    supplier_response = any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        for pattern in (
            r"\bотклик",
            r"по вашему (?:сообщению|запросу|объявлению)",
            r"вы (?:искали|ищете|хотели)",
            r"(?:готов|можем)\w* помочь",
            r"\bмы (?:занимаемся|оказываем|делаем|привозим|доставляем)",
            r"\bя (?:маркетолог|юрист|бухгалтер|риелтор|риэлтор|консультант)",
        )
    )
    if supplier_response:
        return (
            "Спасибо, что написали. Скажу честно: подтвержденного заказа на эту услугу "
            "у нас сейчас нет. Мы знакомимся с "
            "компаниями этой сферы и показываем ТГ РАДАР: сервис находит в Telegram "
            "живые запросы на услуги и подрядчиков и помогает отделить их от шума. "
            "Можем бесплатно показать, как это работает на вашей сфере. Интересно посмотреть?"
        )
    if any(item["direction"] == "outbound" for item in history):
        return (
            "Да, можно бесплатно посмотреть систему на вашей сфере. Менеджер покажет, "
            "как находятся и фильтруются живые запросы, без обещаний по фиксированному "
            "объему. Передать ему заявку на бесплатный тест?"
        )
    return (
        "Спасибо, что написали. Вы по сообщению в чате? Подскажите, пожалуйста, "
        "какую задачу или сферу имеете в виду?"
    )


def fallback_reply(
    inbound_text: str,
    classification: MessageClassification,
    history: List[Dict[str, str]],
    chunks: List[KnowledgeChunk],
) -> str:
    """Legacy offline fixture; the live presales path must never call this helper."""
    normalized = normalize_text(inbound_text)
    question = next_question(history)
    contextual = contextual_detour_reply(normalized, question)
    if contextual:
        return clean_reply_style(contextual)

    if asks_what_happens_next(normalized):
        return (
            "Дальше менеджер свяжется с вами и согласует бесплатный доступ к системе на "
            "тестовый период. Вы сможете сами посмотреть, как сервис находит и фильтрует "
            "запросы по вашей нише. Если формат окажется полезным, уже потом обсудите с "
            "менеджером платное продолжение.\n\n"
            "Передать менеджеру заявку на бесплатный доступ?"
        )

    if looks_english(normalized):
        if has_pricing_terms(normalized):
            return (
                "ТГ РАДАР pricing depends on monitoring volume, niche, sources and the way demand "
                "signals are delivered to your team. I cannot name an individual price here, but "
                "it usually makes sense to look at the system first and discuss the paid format "
                "after that.\n\n"
                "Would you like me to connect a manager so they can show free access to the system?"
            )
        return (
            "Sure. ТГ РАДАР helps find public Telegram discussions where people already show "
            "demand: they ask for recommendations, compare options or look for a contractor. "
            "The system filters noise and passes useful signals to the team.\n\n"
            "Would you like me to connect a manager so they can show free access to the system?"
        )

    if classification.intent == "greeting":
        return (
            "Здравствуйте. Да, могу сориентировать. Сервис находит в Telegram сообщения, "
            "где люди уже ищут услугу, подрядчика или рекомендацию, фильтрует шум и "
            "передает команде понятный сигнал.\n\n"
            f"{next_question(history)}"
        )

    if "гарант" in normalized:
        return (
            "Гарантировать продажи или фиксированное количество лидов нельзя: результат зависит "
            "от ниши, источников, сезонности и скорости обработки. Корректная логика - сначала "
            "бесплатно посмотреть систему и проверить, есть ли живые сигналы спроса.\n\n"
            f"{question}"
        )

    if has_roi_terms(normalized):
        return (
            "ROI, CPL или окупаемость заранее обещать нельзя. Это зависит от ниши, спроса, "
            "скорости обработки сигналов и самого предложения. Корректнее сначала бесплатно "
            "посмотреть систему и понять, есть ли в вашей нише живые запросы.\n\n"
            f"{question}"
        )

    if has_identity_terms(normalized):
        return (
            "Я отвечаю от команды ТГ РАДАР. Мы делаем сервис, который ищет в Telegram публичные "
            "сигналы спроса: когда люди сами ищут подрядчика, рекомендацию, товар или услугу.\n\n"
            f"{question}"
        )

    if has_site_or_presentation_terms(normalized):
        return (
            "Ссылку на сайт или материалы лучше пришлет менеджер, чтобы не дать вам устаревшую "
            "или неподходящую презентацию. Здесь могу коротко объяснить механику: система ищет "
            "публичные сигналы спроса в Telegram и показывает их в рабочем формате.\n\n"
            f"{free_access_cta()}"
        )

    if has_source_terms(normalized):
        return (
            "Источники подбираются под нишу: чаты, группы, каналы, комментарии и другие публичные "
            "места, где люди обсуждают задачу или ищут рекомендации. Дальше настраиваются ключевые "
            "слова, минус-слова и фильтры, чтобы отсечь рекламу и случайный шум.\n\n"
            f"{question}"
        )

    if has_b2b_or_geo_terms(normalized):
        return (
            "Для B2B это может работать, если ваша аудитория публично обсуждает задачи, ищет "
            "подрядчиков или спрашивает рекомендации. По географии тоже зависит от того, есть ли "
            "живые локальные или отраслевые сообщества. Это как раз лучше проверять через "
            "бесплатный доступ к системе.\n\n"
            f"{question}"
        )

    if has_sector_fit_terms(normalized):
        return (
            "В этой сфере смысл есть, если люди в Telegram обсуждают покупку, подбор, подрядчиков "
            "или просят рекомендации. Система помогает найти такие сообщения и отделить их от "
            "рекламы и фонового шума.\n\n"
            f"{question}"
        )

    if has_case_or_proof_terms(normalized):
        return (
            "Кейсы можно посмотреть на https://tgradar.ru/cases. Там есть конкретные "
            "результаты по разным направлениям, а через бесплатный доступ можно проверить, "
            "какие запросы видны уже по вашей задаче.\n\n"
            f"{question}"
        )

    if "пример" in normalized:
        return (
            "Примеры можно показать, но лучше не ограничиваться разовой подборкой. Логичнее "
            "бесплатно посмотреть систему в работе: как находятся реальные сигналы, как "
            "фильтруется шум и что попадает в выдачу.\n\n"
            f"{question}"
        )

    if has_crm_terms(normalized):
        return (
            "Да, найденный запрос можно сразу передать в привычный рабочий поток команды, "
            "например в Telegram-CRM или вашу CRM. Менеджер увидит, что человек ищет, "
            "почему запрос подходит и с чего лучше начать ответ.\n\n"
            f"{question}"
        )

    if classification.intent == "pricing_question" or has_pricing_terms(normalized):
        return (
            "Стоимость ТГ РАДАР начинается от 29 000 руб. в месяц. Уже в базовом "
            "варианте есть ИИ-фильтрация запросов, подсказки для первого ответа, "
            "Telegram-CRM и ежедневная аналитика; подробнее о вариантах: "
            "https://tgradar.ru/price.\n\n"
            f"{free_access_cta()}"
        )

    if "тест" in normalized or "демо" in normalized:
        return (
            "Смысл бесплатного теста - дать посмотреть систему / демо продукта до обсуждения "
            "платного формата. Период, формат доступа и детали подключения подтверждает менеджер.\n\n"
            f"{question}"
        )

    if "работ" in normalized or "устро" in normalized or "наход" in normalized:
        return (
            "Механика такая: под нишу подбираются Telegram-источники, ключевые слова, минус-слова "
            "и сценарии спроса. Система собирает сообщения, ИИ отсеивает рекламу и шум, а в работу "
            "попадают сигналы с текстом, источником, контекстом и приоритетом.\n\n"
            f"{question}"
        )

    if "реклам" in normalized or "отлич" in normalized:
        return (
            "От рекламы отличается тем, что здесь не покупаются показы. Сервис ищет уже возникший "
            "спрос: сообщения, где человек сам спрашивает рекомендацию, ищет подрядчика или "
            "сравнивает варианты. Это отдельный канал, не замена всей рекламе.\n\n"
            f"{question}"
        )

    if "подходит" in normalized or "ниш" in normalized:
        return (
            "Лучше всего подходит нишам, где клиенты публично задают вопросы, ищут рекомендации, "
            "сравнивают подрядчиков или обсуждают покупку в сообществах. Особенно полезно, если "
            "важны скорость ответа и высокий чек.\n\n"
            f"{question}"
        )

    if classification.intent in {"positive", "faq_question"}:
        return (
            "Да, расскажу подробнее. Суть в том, что сервис ищет не просто упоминания, а сообщения "
            "с признаками спроса: человек ищет компанию, просит рекомендацию, сравнивает варианты "
            "или описывает срочную задачу. Потом такие сообщения фильтруются и передаются как "
            "рабочий поток сигналов.\n\n"
            f"{question}"
        )

    if classification.intent == "neutral":
        return (
            "Понял. Если коротко, идея в том, чтобы находить в Telegram уже возникший спрос, "
            "а не покупать показы как в рекламе.\n\n"
            f"{question}"
        )

    if chunks:
        return compose_from_chunks(chunks, question)

    return ""


def next_question(history: List[Dict[str, str]]) -> str:
    outbound_text = "\n".join(
        item["text"].lower()
        for item in history
        if item["direction"] == "outbound"
    )
    if not asked_value_discovery(outbound_text):
        return (
            "Вам это интереснее как источник потенциальных заявок или как способ проверить, "
            "есть ли живой спрос в вашей нише?"
        )
    return free_access_cta()


def asks_what_happens_next(normalized: str) -> bool:
    compact = normalized.strip(" .!?")
    if compact in {
        "что потом",
        "и что потом",
        "а что потом",
        "что дальше",
        "и что дальше",
        "а дальше что",
        "что будет дальше",
    }:
        return True
    return bool(
        re.search(
            r"(?:^|[.!?]\s*)(?:и\s+|а\s+)?"
            r"(?:что\s+потом|что\s+дальше|дальше\s+что|что\s+будет\s+дальше)$",
            compact,
        )
    )


def contextual_detour_reply(normalized: str, question: str) -> str:
    if has_profanity_or_rude_marker(normalized):
        return (
            "Понимаю эмоцию :) Давайте без пафоса: смысл в том, чтобы находить реальные "
            "сообщения людей, которые уже ищут услугу.\n\n"
            f"{question}"
        )
    if has_sarcasm_terms(normalized):
        return (
            "Понимаю скепсис. Тут без магии: источники, фильтры, ИИ-отсев шума и проверка, "
            "есть ли в Telegram реальные запросы по вашей нише.\n\n"
            f"{question}"
        )
    if has_competitor_terms(normalized):
        return (
            "Да, такие инструменты есть. Мы фокусируемся на живых сигналах спроса из Telegram "
            "и удобной передаче их в работу.\n\n"
            f"{free_access_cta()}"
        )
    if asks_for_joke(normalized):
        return (
            "С шутками аккуратно: тут я больше по спросу в Telegram :) Но идея простая: "
            "ищем не магию, а реальные сообщения людей, которые уже что-то ищут.\n\n"
            f"{question}"
        )
    if looks_offtopic(normalized):
        return (
            "Это не совсем моя тема :) Зато могу быстро сориентировать по "
            "ТГ РАДАР: как ищем сигналы спроса и как работает бесплатный доступ.\n\n"
            f"{free_access_cta()}"
        )
    return ""


def has_profanity_or_rude_marker(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "бля",
            "сука",
            "хер",
            "хрен",
            "говн",
            "дерьм",
            "фигн",
            "чушь",
            "ерунд",
        )
    )


def has_competitor_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "tgstat",
            "телеметр",
            "telemetr",
            "youscan",
            "you scan",
            "brand analytics",
            "бренд аналитик",
            "targethunter",
            "таргетхантер",
            "парсер",
            "конкурент",
        )
    )


def asks_for_joke(normalized: str) -> bool:
    return any(marker in normalized for marker in ("анекдот", "пошути", "шутк", "мем"))


def looks_offtopic(normalized: str) -> bool:
    if has_product_terms(normalized):
        return False
    return any(
        marker in normalized
        for marker in (
            "погода",
            "футбол",
            "хоккей",
            "рецепт",
            "кот",
            "кошка",
            "собака",
            "курс доллар",
            "биткоин",
            "политик",
            "новости",
            "кино",
            "сериал",
            "музык",
            "гороскоп",
        )
    )


def has_product_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "tg radar",
            "тг радар",
            "сигнал",
            "telegram",
            "телеграм",
            "лид",
            "заявк",
            "спрос",
            "демо",
            "тест",
            "доступ",
            "сервис",
            "система",
            "услуг",
            "отзыв",
            "кейс",
            "пример",
            "сфер",
            "ниш",
            "тариф",
        )
    )


def looks_english(normalized: str) -> bool:
    latin_letters = sum(1 for char in normalized if "a" <= char <= "z")
    cyrillic_letters = sum(1 for char in normalized if "а" <= char <= "я")
    return latin_letters >= 8 and latin_letters > cyrillic_letters


def has_identity_terms(normalized: str) -> bool:
    return any(marker in normalized for marker in ("кто вы", "от какой компан", "что за компан"))


def has_site_or_presentation_terms(normalized: str) -> bool:
    return any(marker in normalized for marker in ("сайт", "презентац", "материал", "страниц"))


def has_source_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "источник",
            "источники",
            "откуда",
            "чаты",
            "чат",
            "каналы",
            "канал",
            "живые",
            "мусор",
        )
    )


def has_outreach_surface_terms(normalized: str) -> bool:
    surface_markers = (
        "людям или канал",
        "каналам",
        "каналы",
        "личку",
        "личные сообщ",
        "direct messages",
        "dm канал",
        "директ",
        "куда пишете",
        "где ищете",
        "outreach",
    )
    action_markers = (
        "пишете людям",
        "пишете канал",
        "писать людям",
        "писать канал",
        "писать",
        "можно",
        "работаете",
        "ищете",
        "источник",
        "источники",
    )
    return any(marker in normalized for marker in surface_markers) and any(
        marker in normalized for marker in action_markers
    )


def has_contact_source_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "откуда у вас мой контакт",
            "откуда мой контакт",
            "почему вы мне пишете",
            "где взяли мой контакт",
            "где взяли контакт",
            "где взяли мой номер",
            "я давал согласие",
            "согласие давал",
            "почему написали",
        )
    )


def has_b2b_or_geo_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in ("b2b", "б2б", "длинный цикл", "длинным циклом", "город", "географ")
    )


def has_sector_fit_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "авто",
            "китай",
            "агентств",
            "маркетингов",
            "логист",
            "недвиж",
            "ремонт",
            "банкрот",
            "виз",
            "медицин",
            "клиник",
            "crm",
            "црм",
            "1с",
            "тур",
            "b2b",
            "b2c",
            "б2б",
            "б2ц",
        )
    )


def has_case_or_proof_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "кейс",
            "кейсы",
            "отзыв",
            "отзывы",
            "цифр",
            "пример результата",
            "результат",
            "пример",
            "примеры",
            "покаж",
            "доказатель",
            "proof",
        )
    )


def has_case_text_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "кейс",
            "кейсы",
            "цифр",
            "пример результата",
            "результат",
            "доказатель",
            "proof",
        )
    )


def has_signal_example_text_terms(normalized: str) -> bool:
    if "пример результата" in normalized:
        return False
    return any(marker in normalized for marker in ("пример", "примеры", "покаж")) and any(
        marker in normalized
        for marker in (
            "сигнал",
            "лид",
            "как выглядит",
            "сообщен",
            "пример",
            "примеры",
            "покаж",
        )
    )


def has_review_terms(normalized: str) -> bool:
    return any(marker in normalized for marker in ("отзыв", "отзывы", "яндекс", "клиенты говорят"))


def has_service_terms(normalized: str) -> bool:
    return has_out_of_scope_service_terms(normalized) or any(
        marker in normalized
        for marker in (
            "услуг",
            "сценар",
            "b2b",
            "b2c",
            "б2б",
            "б2ц",
            "репутац",
            "конкурент",
            "рынк",
            "тренд",
            "инфоповод",
            "мониторинг",
            "отдел продаж",
            "лидоген",
            "crm",
            "црм",
            "telegram-crm",
            "telegram crm",
            "телеграм-crm",
            "телеграм crm",
            "рабочий чат",
            "таблиц",
            "передавать сигнал",
            "передача сигнал",
            "куда передав",
        )
    )


def has_out_of_scope_service_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "автоматизац заказ",
            "автоматизировать заказ",
            "запустить сайт",
            "запуск сайт",
            "сайт с автоматизац",
        )
    )


def has_partner_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "партнер",
            "партнёр",
            "реферал",
            "рефераль",
            "рекомендовать",
            "комисси",
            "выплат",
        )
    )


def has_blog_terms(normalized: str) -> bool:
    return any(marker in normalized for marker in ("блог", "стать", "почитать", "материал"))


def has_sector_terms(normalized: str) -> bool:
    return has_auto_sector_terms(normalized) or any(
        marker in normalized
        for marker in (
            "сфер",
            "ниш",
            "отрасл",
            "китай",
            "вед",
            "логист",
            "банкрот",
            "виз",
            "медицин",
            "клиник",
            "недвиж",
            "1с",
            "crm",
            "црм",
            "тур",
            "маркетинг",
            "агентств",
        )
    )


def has_auto_sector_terms(normalized: str) -> bool:
    return bool(re.search(r"\bавто\b|\bавтомобил|\bмашин", normalized))


def has_legal_sensitive_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "договор",
            "счет",
            "счёт",
            "оплат",
            "персональн",
            "юрид",
            "медицинск",
            "документ",
            "compliance",
            "комплаенс",
            "доступ",
        )
    )


def has_roi_terms(normalized: str) -> bool:
    return any(marker in normalized for marker in ("roi", "окуп", "cpl", "cac", "окупаем"))


def has_guarantee_terms(normalized: str) -> bool:
    return any(
        marker in normalized
        for marker in (
            "гарант",
            "сколько лидов",
            "лидов в месяц",
            "roi",
            "окуп",
            "cpl",
            "cac",
            "точно получите",
            "блокир",
            "юридическ последств",
        )
    )


def has_crm_terms(normalized: str) -> bool:
    return any(marker in normalized for marker in ("crm", "црм", "amo", "amo crm", "amocrm"))


def has_sarcasm_terms(normalized: str) -> bool:
    return any(marker in normalized for marker in ("волшеб", "очередной лидогенератор", "магия"))


def asked_value_discovery(outbound_text: str) -> bool:
    return any(
        marker in outbound_text
        for marker in (
            "источник потенциальных заявок",
            "проверить, есть ли живой спрос",
            "проверить спрос",
            "горячие запросы",
        )
    )


def free_access_cta() -> str:
    return (
        "Могу передать менеджеру, чтобы он помог подключить бесплатный доступ к системе "
        "и показал, как это работает на вашей задаче?"
    )


def compose_from_chunks(chunks: List[KnowledgeChunk], question: str) -> str:
    customer_chunks = [
        chunk
        for chunk in chunks
        if is_customer_facing_source(chunk.source, chunk.metadata)
    ]
    if not customer_chunks:
        return (
            "Не хочу придумывать лишнее. По утвержденной информации могу сказать базово: "
            "ТГ РАДАР помогает находить в Telegram публичные сигналы спроса и проверять, "
            "есть ли живые запросы в нише.\n\n"
            f"{question}"
        )
    primary = customer_chunks[0].text
    primary = re.sub(r"\s+", " ", primary).strip()
    if len(primary) > 420:
        primary = primary[:419].rstrip() + "..."
    return f"Коротко: {primary}\n\n{question}"


def is_customer_facing_source(
    source: str,
    metadata: Dict[str, object] | None = None,
) -> bool:
    metadata = metadata or {}
    audience = str(metadata.get("audience") or "")
    usage_type = str(metadata.get("usage_type") or "")
    if audience in {"internal", "internal_llm"} or usage_type == "source_audit":
        return False
    return source not in INTERNAL_KB_SOURCES and not source.startswith("source_finder_")


def next_state_for(classification: MessageClassification) -> str:
    if classification.intent in {"greeting", "positive"}:
        return "Interested"
    return "FAQ automation"


def has_pricing_terms(normalized: str) -> bool:
    return any(
        term in normalized
        for term in (
            "цена",
            "цену",
            "цены",
            "стоимост",
            "тариф",
            "пакет",
            "прайс",
            "сколько стоит",
            "how much",
            "cost",
            "price",
            "pricing",
        )
    )


def source_files(chunks: List[KnowledgeChunk]) -> List[str]:
    result: List[str] = []
    for chunk in chunks:
        if chunk.source not in result:
            result.append(chunk.source)
    return result


def dedupe_strings(values: List[str]) -> List[str]:
    result: List[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result
