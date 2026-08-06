"""Решение по входящему сообщению.

Перенесено дословно с релиза a55d259. Единственная правка — имена модулей:
``customer_truth_pack`` → ``truth_pack``, а шесть функций сборки контекста
переехали из ``presales`` в ``presales_context``.

Функция ``decide_inbound_reply`` ничего не пишет и не читает из базы: на вход
идёт словарь, на выход решение. Всё, что происходит с этим решением дальше —
очередь, темп, отправка — наше и живёт снаружи.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Optional, Union

from .policy import MessageClassification, classify_inbound
from .presales_context import (
    CHAT_SENDER_PRIVATE_ENTRY_MODE,
    INBOUND_REFERRAL_CAMPAIGN_ID,
    build_llm_context,
    non_silent_boundary_reply,
)
from .truth_pack import CustomerTruthPack, load_customer_truth_pack
from .presales_v2 import (
    PRESALES_V2_MAX_PRIMARY_ATTEMPTS,
    PresalesV2ExternalResult,
    build_presales_v2_prompt,
    call_presales_v2_llm,
    normalize_presales_v2_result,
    presales_v2_repair_instruction,
    required_topics_for_turn,
    sha256_json,
    technical_failure_result,
)


DECISION_VERSION = 1
DECISION_MODE = "mvp_provider_neutral_inbound_decision"
INBOUND_REPLY_ROLES = frozenset(
    {"channel_sender", "chat_sender", "dm_sender"}
)
NO_SEND_DECISIONS = frozenset(
    {
        "opt_out",
        "ignore",
        "manager_handoff",
        "knowledge_gap",
        "hold_for_review",
        "await_newer_inbound",
    }
)


class MvpInboundDecisionError(ValueError):
    pass


LLMCaller = Callable[
    ..., Union[PresalesV2ExternalResult, Mapping[str, object]]
]


def _required(value: object, label: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or "\x00" in normalized:
        raise MvpInboundDecisionError(f"{label} is required")
    return normalized


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): str(item)
        for key, item in value.items()
        if str(key).strip() and str(item).strip()
    }


def _normalized_history(
    value: object,
    *,
    current_text: str,
    received_at: str,
) -> list[dict[str, str]]:
    if value is None:
        source: Sequence[object] = ()
    elif isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        source = value
    else:
        raise MvpInboundDecisionError("history must be a sequence")

    history: list[dict[str, str]] = []
    for index, raw in enumerate(source, start=1):
        if not isinstance(raw, Mapping):
            raise MvpInboundDecisionError(
                f"history row {index} must be an object"
            )
        direction = str(raw.get("direction") or "").strip()
        text = str(raw.get("text") or "").strip()
        if direction not in {"inbound", "outbound"} or not text:
            continue
        history.append(
            {
                "direction": direction,
                "text": text,
                "intent": str(raw.get("intent") or "").strip(),
                "created_at": str(raw.get("created_at") or "").strip(),
            }
        )
    if (
        not history
        or history[-1]["direction"] != "inbound"
        or history[-1]["text"] != current_text
    ):
        history.append(
            {
                "direction": "inbound",
                "text": current_text,
                "intent": "",
                "created_at": received_at,
            }
        )
    return history


def inbound_decision_key(context: Mapping[str, object]) -> str:
    text = _required(context.get("text"), "text")
    return _sha256(
        {
            "version": DECISION_VERSION,
            "provider_id": _required(
                context.get("provider_id"), "provider_id"
            ),
            "handoff_id": str(context.get("handoff_id") or "").strip(),
            "inbound_id": _required(
                context.get("inbound_id"), "inbound_id"
            ),
            "account_id": _required(
                context.get("account_id"), "account_id"
            ),
            "role": _required(context.get("role"), "role"),
            "peer_key": _required(context.get("peer_key"), "peer_key"),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    )


def _decision_payload(
    context: Mapping[str, object],
    *,
    classification: MessageClassification,
    decision: str,
    reply_text: str = "",
    confidence: Optional[float] = None,
    risk_level: str = "",
    handoff_required: bool = False,
    handoff_reason: str = "",
    reason: str = "",
    engine: str,
    technical_failure: bool = False,
    prompt_sha256: str = "",
    truth_pack_sha256: str = "",
    collected_fields_update: Optional[Mapping[str, str]] = None,
    validation_warnings: Optional[Sequence[str]] = None,
) -> dict[str, object]:
    final_text = str(reply_text or "").strip()
    send_allowed = bool(
        final_text
        and decision not in NO_SEND_DECISIONS
        and not technical_failure
    )
    return {
        "version": DECISION_VERSION,
        "mode": DECISION_MODE,
        "decision_key": inbound_decision_key(context),
        "provider_id": str(context["provider_id"]),
        "handoff_id": str(context.get("handoff_id") or ""),
        "inbound_id": str(context["inbound_id"]),
        "account_id": str(context["account_id"]),
        "role": str(context["role"]),
        "decision": decision,
        "send_allowed": send_allowed,
        "reply_text": final_text,
        "intent": classification.intent,
        "semantic_intent": classification.intent,
        "confidence": (
            classification.confidence
            if confidence is None
            else max(0.0, min(1.0, float(confidence)))
        ),
        "risk_level": risk_level or classification.risk_level,
        "handoff_required": bool(handoff_required),
        "handoff_reason": str(handoff_reason or ""),
        "reason": str(reason or classification.reason),
        "engine": engine,
        "technical_failure": bool(technical_failure),
        "prompt_sha256": str(prompt_sha256 or ""),
        "truth_pack_sha256": str(truth_pack_sha256 or ""),
        "collected_fields_update": dict(collected_fields_update or {}),
        "validation_warnings": list(validation_warnings or ()),
    }


def _presales_context(
    context: Mapping[str, object],
    *,
    classification: MessageClassification,
    history: list[dict[str, str]],
) -> dict[str, object]:
    role = str(context["role"])
    has_prior_outbound = any(
        item["direction"] == "outbound" for item in history
    )
    campaign_id = str(context.get("campaign_id") or "").strip()
    if not campaign_id:
        campaign_id = (
            "mvp_bridge49_existing_dialog"
            if has_prior_outbound
            else INBOUND_REFERRAL_CAMPAIGN_ID
        )
    entry_mode = str(context.get("entry_mode") or "").strip()
    if not entry_mode:
        if role == "chat_sender":
            entry_mode = CHAT_SENDER_PRIVATE_ENTRY_MODE
        elif has_prior_outbound:
            entry_mode = "post_first_touch"
        else:
            entry_mode = "inbound_private_without_first_touch"

    discovery_context = _string_mapping(context.get("discovery_context"))
    conversation = {
        "id": str(
            context.get("conversation_id")
            or (
                f"{context['provider_id']}:{context['account_id']}:"
                f"{context['peer_key']}"
            )
        ),
        "state": str(context.get("conversation_state") or "FAQ automation"),
        "campaign_id": campaign_id,
        "handoff_status": (
            "pending" if bool(context.get("semantic_handoff_active")) else ""
        ),
        "presales_context": json.dumps(
            discovery_context,
            ensure_ascii=False,
            sort_keys=True,
        ),
    }
    recipient_input = (
        dict(context.get("recipient") or {})
        if isinstance(context.get("recipient"), Mapping)
        else {}
    )
    recipient = {
        "id": str(
            recipient_input.get("id")
            or f"{context['provider_id']}:{context['peer_key']}"
        ),
        "recipient_type": "user",
        "segment": str(recipient_input.get("segment") or "bridge49_inbound"),
        "company": str(recipient_input.get("company") or ""),
        "role": str(recipient_input.get("role") or ""),
        "source": str(recipient_input.get("source") or context["provider_id"]),
        "consent_status": str(
            recipient_input.get("consent_status") or "inbound"
        ),
        "consent_source": str(
            recipient_input.get("consent_source") or "telegram_inbound"
        ),
        "consent_scope": str(
            recipient_input.get("consent_scope")
            or "reply_in_same_private_chat"
        ),
        "consent_date": str(recipient_input.get("consent_date") or ""),
    }
    sender_account = {
        "id": str(context["account_id"]),
        "role": role,
        "provider_id": str(context["provider_id"]),
        "label": str(context.get("account_label") or ""),
    }
    recent_public = context.get("recent_public_chat_outreach")
    if not isinstance(recent_public, list):
        recent_public = []
    llm_context = build_llm_context(
        conversation=conversation,  # type: ignore[arg-type]
        recipient=recipient,  # type: ignore[arg-type]
        sender_account=sender_account,
        classification=classification,
        history=history,
        auto_reply_count=max(0, int(context.get("auto_reply_count") or 0)),
        max_auto_replies=max(
            1, int(context.get("max_auto_replies") or 100)
        ),
        manager_nudge_after_replies=max(
            0, int(context.get("manager_nudge_after_replies") or 80)
        ),
        entry_mode=entry_mode,
        recent_public_chat_outreach=recent_public,
    )
    llm_context["message_history"] = history
    llm_context["conversation_id"] = conversation["id"]
    llm_context["active_handoff"] = bool(
        context.get("semantic_handoff_active")
    )
    llm_context["presales_engine"] = "v2_full_context"
    llm_context["free_test_sector_known"] = bool(
        discovery_context.get("sector")
    )
    llm_context["automatic_free_test_sector_catalog"] = list(
        context.get("direct_invite_sector_catalog") or []
    )
    llm_context["sector_matching_catalog"] = list(
        context.get("sector_matching_catalog") or []
    )
    # Промпт спрашивает у контекста, работает ли автовыдача для этого
    # собеседника (`free_test_access_branch.branch=automatic`). Ключ обязан
    # существовать: без него условие в промпте не выполнится никогда, и модель
    # всегда выберет ручной путь — молча, без всякого признака поломки.
    branch = context.get("free_test_access_branch")
    llm_context["free_test_access_branch"] = (
        dict(branch) if isinstance(branch, Mapping) and branch
        else {"branch": "manager"}
    )
    if entry_mode == CHAT_SENDER_PRIVATE_ENTRY_MODE:
        llm_context["quoted_public_chat_request_likely"] = bool(
            context.get("quoted_public_chat_request_likely")
        )
    return llm_context


def decide_inbound_reply(
    context: Mapping[str, object],
    *,
    command: Optional[str] = None,
    # Верхний из трёх пределов: он убивает саму обёртку. Держать его ниже
    # прокси-маршрута бессмысленно — маршруту тогда не дадут доработать. При
    # 300 с на маршрут здесь нужно больше 300; 380 даёт один полный маршрут и
    # запас на накладные обёртки.
    timeout_seconds: float = 380,
    truth_pack: Optional[CustomerTruthPack] = None,
    llm_caller: LLMCaller = call_presales_v2_llm,
) -> dict[str, object]:
    """Return one provider-neutral reactive decision without touching a DB.

    The function intentionally reuses the existing local safety classifier and
    presales-v2 prompt/normalizer. Provider ingress, persistence and delivery
    remain outside this seam.
    """

    if not isinstance(context, Mapping):
        raise MvpInboundDecisionError("context must be an object")
    provider_id = _required(context.get("provider_id"), "provider_id")
    inbound_id = _required(context.get("inbound_id"), "inbound_id")
    account_id = _required(context.get("account_id"), "account_id")
    role = _required(context.get("role"), "role")
    peer_key = _required(context.get("peer_key"), "peer_key")
    text = _required(context.get("text"), "text")
    if role not in INBOUND_REPLY_ROLES:
        raise MvpInboundDecisionError(
            f"role {role} cannot handle private inbound replies"
        )
    normalized_context = {
        **dict(context),
        "provider_id": provider_id,
        "inbound_id": inbound_id,
        "account_id": account_id,
        "role": role,
        "peer_key": peer_key,
        "text": text,
    }
    classification = classify_inbound(text)
    if classification.intent == "opt_out":
        return _decision_payload(
            normalized_context,
            classification=classification,
            decision="opt_out",
            engine="deterministic_policy",
        )
    if classification.intent == "spam":
        return _decision_payload(
            normalized_context,
            classification=classification,
            decision="ignore",
            engine="deterministic_policy",
        )
    if classification.intent == "hard_negative":
        return _decision_payload(
            normalized_context,
            classification=classification,
            decision="manager_handoff",
            handoff_required=True,
            handoff_reason=classification.reason,
            engine="deterministic_policy",
        )
    if classification.intent == "meaningless":
        return _decision_payload(
            normalized_context,
            classification=classification,
            decision="auto_reply",
            reply_text=non_silent_boundary_reply(classification.intent),
            engine="deterministic_policy",
        )

    received_at = str(context.get("received_at") or "").strip()
    history = _normalized_history(
        context.get("history"),
        current_text=text,
        received_at=received_at,
    )
    pack = truth_pack or load_customer_truth_pack()
    llm_context = _presales_context(
        normalized_context,
        classification=classification,
        history=history,
    )
    required_topics = required_topics_for_turn(text)
    payload = build_presales_v2_prompt(
        inbound_text=text,
        context=llm_context,
        pack=pack,
        required_topics=required_topics,
        reasoning_effort=str(context.get("reasoning_effort") or "high"),
    )
    prompt_sha256 = sha256_json(payload)
    direct_invite_context = context.get("direct_invite_context")
    if not isinstance(direct_invite_context, Mapping):
        direct_invite_context = {}
    sector_catalog = context.get("direct_invite_sector_catalog")
    if not isinstance(sector_catalog, list):
        sector_catalog = []
    normalized = None
    attempts = 0
    for attempt in range(1, PRESALES_V2_MAX_PRIMARY_ATTEMPTS + 1):
        attempts = attempt
        active_payload = dict(payload)
        active_payload["runtime_attempt"] = attempt
        if attempt > 1 and normalized is not None:
            active_payload["repair_instruction"] = (
                presales_v2_repair_instruction(normalized.reason)
            )
        try:
            external = llm_caller(
                active_payload,
                command=command,
                timeout_seconds=timeout_seconds,
                supersession_check=None,
            )
        except Exception as exc:
            normalized = technical_failure_result(
                f"mvp_inbound_llm_error:{exc.__class__.__name__}"
            )
            continue
        if isinstance(external, Mapping):
            raw = dict(external)
            external_reason = ""
        else:
            raw = external.raw
            external_reason = external.reason
        if raw is None:
            normalized = technical_failure_result(
                external_reason or "mvp_inbound_llm_no_result"
            )
        else:
            normalized = normalize_presales_v2_result(
                raw,
                pack=pack,
                required_topics=required_topics,
                inbound_text=text,
                allowed_direct_invite_sector_ids=[
                    str(item.get("outreach_sector_id") or "")
                    for item in sector_catalog
                    if isinstance(item, Mapping)
                    and str(item.get("outreach_sector_id") or "").strip()
                ],
                required_direct_invite_sector_id=str(
                    direct_invite_context.get("outreach_sector_id") or ""
                ),
                confirmed_sector_available=bool(
                    llm_context.get("free_test_sector_known")
                ),
                known_canonical_sector_ids=[
                    str(item.get("canonical_sector_id") or "")
                    for item in (llm_context.get("sector_matching_catalog") or [])
                    if isinstance(item, Mapping)
                ],
            )
        if not normalized.technical_failure:
            break
    if normalized is None:
        normalized = technical_failure_result("mvp_inbound_llm_no_result")

    if normalized.technical_failure:
        semantic_classification = MessageClassification(
            intent=classification.intent,
            risk_level=classification.risk_level,
            confidence=classification.confidence,
            handoff_required=False,
            automation_paused=True,
            reason=normalized.reason,
        )
        return {
            **_decision_payload(
                normalized_context,
                classification=semantic_classification,
                decision="hold_for_review",
                reason=normalized.reason,
                engine="presales_v2",
                technical_failure=True,
                prompt_sha256=prompt_sha256,
                truth_pack_sha256=pack.sha256,
            ),
            "primary_llm_attempts": attempts,
        }

    semantic_classification = MessageClassification(
        intent=normalized.intent,
        risk_level=normalized.risk_level,
        confidence=normalized.confidence,
        handoff_required=normalized.handoff_required,
        automation_paused=normalized.decision
        in {
            "manager_handoff",
            "pause_conversation",
            "opt_out",
            "hold_for_review",
        },
        reason=normalized.reason,
    )
    return {
        **_decision_payload(
            normalized_context,
            classification=semantic_classification,
            decision=normalized.decision,
            reply_text=normalized.reply_text,
            confidence=normalized.confidence,
            risk_level=normalized.risk_level,
            handoff_required=normalized.handoff_required,
            handoff_reason=normalized.handoff_reason,
            reason=normalized.reason,
            engine="presales_v2",
            prompt_sha256=prompt_sha256,
            truth_pack_sha256=pack.sha256,
            collected_fields_update=normalized.collected_fields_update,
            validation_warnings=normalized.validation_warnings,
        ),
        "primary_llm_attempts": attempts,
        "handoff_kind": normalized.handoff_kind,
        # Без этого ключа автовыдача мертва целиком: `sector_from_decision`
        # читает именно его, пустое значение даёт `BranchInactive`, согласие
        # не записывается — и человек, которому уже пообещали ссылку, не
        # получает её никогда. Модель сферу возвращает и возвращала: 04.08 на
        # ходе @cargo316k_1688 в сыром ответе стояло `logistics_ved_china`,
        # а до `record_consent` доезжала пустая строка.
        "matched_direct_invite_sector_id": (
            normalized.matched_direct_invite_sector_id
        ),
        # Сопоставление сферы со словарём. Гейт демо-маршрута читает два
        # последних ключа; без них он не включается вовсе.
        "client_sector_text": normalized.client_sector_text,
        "canonical_sector_id": normalized.canonical_sector_id,
        "sector_confidence": normalized.sector_confidence,
        "knowledge_gap": normalized.knowledge_gap,
        "next_state": normalized.next_state,
    }
