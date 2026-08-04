#!/usr/bin/env python3
"""Граница с моделью для OUTREACH_LLM_COMMAND: JSON на stdin, JSON на stdout.

Перенесено с релиза a55d259 без изменений в логике — поменялись только имена
модулей, откуда берётся прокси-пул.

Вопреки прежнему докстрингу («test-only, intentionally not the production LLM
integration»), в бою на .148 работал именно этот файл: обещанной обёртки под
OpenAI API так и не появилось. Модель вызывается через `codex exec`, то есть
через залогиненную сессию Codex CLI, а не по ключу; сам ключ нигде не хранится
и в код не попадает.

Выход наружу идёт через SOCKS5-прокси телеграм-аккаунтов, по одному, с
отдельным таймаутом на попытку — сервер до OpenAI напрямую не ходит.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge49.codex_proxy import codex_attempt_environment, load_codex_proxy_routes
from bridge49.proxy_bindings import load_proxy_binding_manifest


DEFAULT_USAGE_LOG_PATH = ROOT / "runtime" / "codex_llm_usage.jsonl"
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_RETRY_BACKOFF_SECONDS = 2.0
DEFAULT_MAX_RETRY_BACKOFF_SECONDS = 30.0
REASONING_EFFORT_ORDER = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "xhigh": 3,
}


def main() -> int:
    raw_stdin = sys.stdin.read()
    try:
        payload = json.loads(raw_stdin)
    except json.JSONDecodeError:
        print_json(error_response("codex_wrapper_invalid_input_json"))
        return 0

    if (
        os.environ.get("CODEX_LLM_TEST_MODE") != "1"
        and os.environ.get("CODEX_LLM_ENABLED") != "1"
    ):
        print_json(error_response("codex_wrapper_not_enabled"))
        return 0

    prompt = build_codex_prompt(payload)
    result, run_metadata = run_codex(prompt, source_payload=payload)
    write_usage_log(payload, prompt, result, run_metadata)
    print_json(result)
    return 0


def is_plain_prompt_payload(payload: Dict[str, Any]) -> bool:
    """Задача, у которой уже есть свой промпт целиком.

    Обёртка писалась под presales, и обе её ветки клеят к запросу presales-
    инструкции: как отвечать клиенту, когда звать менеджера, что считать
    спамом. Для классификации это не просто лишнее, а вредное — модель
    получает два противоречащих задания сразу.
    """
    return str(payload.get("prompt_mode") or "").strip() == "plain"


def build_codex_prompt(payload: Dict[str, Any]) -> str:
    if is_plain_prompt_payload(payload):
        return str(payload.get("prompt") or "")
    if is_presales_v2_payload(payload):
        compact_payload = dict(payload)
        compact_payload.pop("output_schema", None)
        return "\n".join(
            [
                "Ты read-only LLM-модуль Presales v2 ТГ РАДАР, а не кодовый агент.",
                "Не запускай инструменты, не меняй файлы и не используй внешние знания.",
                "Используй только system, current_turn_text, conversation_context и truth_pack из INPUT_JSON.",
                "Разбери весь текущий ход, включая несколько вопросов, и верни ровно один JSON object по native JSON Schema.",
                "Все обязательные поля, включая action, intent, coverage_complete, turn_items и reason, должны присутствовать.",
                "user_evidence копируй дословно из current_turn_text; reply_evidence — дословно из финального reply_text.",
                "Для фактического ответа укажи только source_id из truth_pack.source_catalog.",
                "Без Markdown, пояснений и code fence.",
                "",
                "INPUT_JSON:",
                json.dumps(compact_payload, ensure_ascii=False, separators=(",", ":")),
            ]
        )
    output_schema = payload.get("output_schema")
    if not isinstance(output_schema, dict):
        output_schema = expected_contract()
    return "\n".join(
        [
            "Ты не кодовый агент в этой задаче. Ты отвечаешь как LLM-draft модуль для ТГ РАДАР.",
            "Не запускай команды, не редактируй файлы, не ищи внешнюю информацию и не используй знания вне входного JSON.",
            "Используй только system, conversation_context и knowledge_chunks из INPUT_JSON.",
            "Верни ровно один JSON object по OUTPUT_SCHEMA. Без Markdown, без пояснений, без code fence.",
            "Если знаний из knowledge_chunks недостаточно, верни ok=false, reason=knowledge_not_enough и knowledge_gap.",
            "Если нужен живой человек, договор, счет, оплата, доступ, запуск или юридический ответ, верни ok=false и handoff_required=true.",
            "Всегда верни semantic intent и decision из INPUT_JSON.output_schema; это главный semantic decision для live-пайплайна.",
            "Каждое входящее должно получить короткий безопасный ответ. Исключения — явный opt-out с прямой просьбой больше не писать, подтвержденный рекламный спам и сообщение без содержательного русского текста.",
            "Для сообщения без содержательного русского текста верни decision=ignore, intent=spam, ok=false и пустой reply_text; не проси перейти на русский.",
            "Для рекламного спама, включая NFT, airdrop, wallet, giveaway и crypto promotion, верни decision=ignore, intent=spam, ok=false и пустой reply_text.",
            "Для бессодержательного сообщения верни auto_reply с intent=meaningless и попроси уточнить вопрос. Короткое русское приветствие считается осмысленным: задай короткий уточняющий вопрос.",
            "При явном отказе от дальнейших сообщений верни decision=opt_out, intent=opt_out и ok=false.",
            "Sensitive отрасль сама по себе не handoff: если пользователь не просит медицинский, юридический или финансовый совет, ответь общей механикой поиска публичных сигналов и ограничениями.",
            "Фразы вроде 'не нарушайте закон', 'важен комплаенс' или 'без медицинских обещаний' сами по себе не handoff: подтверди ограничение и ответь только про low-risk механику продукта.",
            "Общие русские вопросы про демо или тест вроде 'можно демо?' и 'как проходит тест?' не являются handoff: ответь общей механикой и предложи следующий шаг. Нерусский запрос оставь без ответа как spam/ignore.",
            "Короткое 'да' является handoff только если предыдущий ответ явно предлагал менеджера, бесплатный доступ или демо с менеджером.",
            "Подтверждение актуальности чужой вакансии или объявления с перенаправлением к другому контакту не является заявкой в ТГ РАДАР: верни pause_conversation без handoff, если в текущем сообщении нет явного интереса к продукту, тесту, демо, доступу или менеджеру.",
            "Короткое 'да', 'ок' или 'давайте' после продуктового объяснения или CTA не является knowledge_gap: выбери manager_handoff при явном согласии на менеджера/демо/бесплатный доступ, иначе коротко продолжи по продукту.",
            "Осмысленный русский оффтоп вроде погоды, крипты, политики или шуток не является handoff: коротко отреагируй и верни к ТГ РАДАР. Подтвержденный рекламный оффтоп-спам оставь без ответа.",
            "Учитывай conversation_context.sender_account: если нужна самореференция, используй правильные русские окончания по gender; если gender=neutral, избегай гендерных форм.",
            "Если ответ можно отправить автоматически, risk_level должен быть low и handoff_required=false. Не завышай confidence: любое значение ниже 0.8 допустимо и будет отдельно проверено второй моделью.",
            "",
            "OUTPUT_SCHEMA:",
            json.dumps(output_schema, ensure_ascii=False, indent=2),
            "",
            "INPUT_JSON:",
            json.dumps(payload, ensure_ascii=False, indent=2),
        ]
    )


def run_codex(prompt: str, *, source_payload: Optional[Dict[str, Any]] = None
              ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    codex_bin = os.environ.get("CODEX_LLM_CODEX_BIN", "codex")
    timeout_seconds = parse_timeout(os.environ.get("CODEX_LLM_TIMEOUT_SECONDS"), default=180.0)
    workdir = Path(os.environ.get("CODEX_LLM_WORKDIR", str(ROOT))).resolve()
    model = resolve_model()
    # У plain-промпта нет INPUT_JSON внутри текста, поэтому исходный
    # payload передаётся отдельно, а не вылавливается обратно из промпта.
    prompt_payload = source_payload or extract_prompt_payload(prompt) or {}
    reasoning_effort = resolve_reasoning_effort(prompt_payload=prompt_payload)
    metadata: Dict[str, Any] = {
        "codex_bin": codex_bin,
        "model": model,
        "reasoning_effort": reasoning_effort,
        "workdir": str(workdir),
        "timeout_seconds": timeout_seconds,
        "proxy_attempt_timeout_seconds": timeout_seconds,
        "started_at": utc_now(),
        "duration_ms": 0,
        "returncode": None,
        "stdout_chars": 0,
        "stderr_chars": 0,
        "raw_output_chars": 0,
        "error": "",
        "proxy_failover_enabled": False,
        "proxy_attempt_count": 0,
        "proxy_route_id": "",
        "proxy_routes_considered": [],
        "retry_backoff_count": 0,
        "retry_backoff_seconds_total": 0.0,
        "rate_limit_count": 0,
        "last_error_class": "",
    }
    started = time.monotonic()

    def finish(response: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        metadata["duration_ms"] = int((time.monotonic() - started) * 1000)
        return response, metadata

    try:
        routes = load_codex_proxy_routes()
    except Exception:
        metadata["error"] = "proxy_pool_configuration"
        return finish(
            error_response_for_payload(
                prompt_payload,
                "codex_wrapper_proxy_pool_invalid",
            )
        )
    metadata["proxy_failover_enabled"] = any(route["proxy_url"] for route in routes)
    metadata["proxy_routes_considered"] = [route["route_id"] for route in routes]
    attempt_timeout_seconds = timeout_seconds
    if metadata["proxy_failover_enabled"]:
        attempt_timeout_seconds = min(
            timeout_seconds,
            parse_timeout(
                os.environ.get("CODEX_LLM_PROXY_ATTEMPT_TIMEOUT_SECONDS"),
                default=60.0,
            ),
        )
    metadata["proxy_attempt_timeout_seconds"] = attempt_timeout_seconds

    with tempfile.TemporaryDirectory(prefix="codex-llm-wrapper-") as tmp:
        output_schema_path: Optional[Path] = None
        native_schema = native_output_schema(prompt_payload)
        if native_schema is not None:
            output_schema_path = Path(tmp) / "output_schema.json"
            output_schema_path.write_text(
                json.dumps(
                    native_schema,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            metadata["native_output_schema"] = True
            metadata["output_schema_sha256"] = sha256_text(
                output_schema_path.read_text(encoding="utf-8")
            )
        else:
            metadata["native_output_schema"] = False
            metadata["output_schema_sha256"] = ""
        for attempt, route in enumerate(routes, start=1):
            output_path = Path(tmp) / f"last_message_{attempt}.json"
            cmd: List[str] = [
                codex_bin,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "-c",
                'approval_policy="never"',
                "-c",
                f'model_reasoning_effort="{reasoning_effort}"',
                "--output-last-message",
                str(output_path),
                "-C",
                str(workdir),
                "-",
                "--model",
                model,
            ]
            if output_schema_path is not None:
                cmd[cmd.index("--output-last-message"):cmd.index("--output-last-message")] = [
                    "--output-schema",
                    str(output_schema_path),
                ]
            metadata["proxy_attempt_count"] = attempt
            metadata["proxy_route_id"] = route["route_id"]
            try:
                completed = subprocess.run(
                    cmd,
                    input=prompt,
                    text=True,
                    capture_output=True,
                    timeout=attempt_timeout_seconds,
                    check=False,
                    env=codex_attempt_environment(route["proxy_url"]),
                )
            except FileNotFoundError:
                metadata["error"] = "codex_not_found"
                return finish(
                    error_response_for_payload(
                        prompt_payload,
                        "codex_wrapper_codex_not_found",
                    )
                )
            except subprocess.TimeoutExpired:
                metadata["error"] = "timeout"
                metadata["last_error_class"] = "timeout"
                apply_retry_backoff(
                    metadata,
                    attempt=attempt,
                    remaining_attempts=len(routes) - attempt,
                )
                continue

            metadata["returncode"] = completed.returncode
            metadata["stdout_chars"] = len(completed.stdout or "")
            metadata["stderr_chars"] = len(completed.stderr or "")
            if completed.returncode != 0:
                metadata["error"] = "non_zero_returncode"
                error_class = codex_failure_class(completed.stderr)
                metadata["last_error_class"] = error_class
                if error_class == "rate_limit":
                    metadata["rate_limit_count"] = (
                        int(metadata["rate_limit_count"]) + 1
                    )
                if error_class in {"rate_limit", "overloaded"}:
                    apply_retry_backoff(
                        metadata,
                        attempt=attempt,
                        remaining_attempts=len(routes) - attempt,
                    )
                continue
            if not output_path.exists():
                metadata["error"] = "missing_output"
                return finish(
                    error_response_for_payload(
                        prompt_payload,
                        "codex_wrapper_missing_output",
                    )
                )
            raw_output = output_path.read_text(encoding="utf-8").strip()
            metadata["raw_output_chars"] = len(raw_output)
            parsed = extract_json_payload(raw_output)
            if parsed is None:
                metadata["error"] = "invalid_output_json"
                return finish(
                    error_response_for_payload(
                        prompt_payload,
                        "codex_wrapper_invalid_output_json",
                    )
                )
            metadata["error"] = ""
            metadata["last_error_class"] = ""
            # Свой промпт — свой ответ. Нормализатор приводит что угодно к
            # presales-форме (`decision`, `reply_text`, `ok`), и для задачи с
            # другой схемой это не приведение, а потеря: разбор классификации
            # доезжал сюда целым и выходил пустым каркасом.
            if is_plain_prompt_payload(prompt_payload):
                return finish(parsed)
            return finish(normalize_contract_response(parsed, prompt_payload))
    return finish(
        error_response_for_payload(
            prompt_payload,
            "codex_wrapper_failed",
            detail="all_configured_proxy_routes_failed",
        )
    )


def write_usage_log(
    payload: Dict[str, Any],
    prompt: str,
    result: Dict[str, Any],
    run_metadata: Dict[str, Any],
) -> None:
    log_path_value = os.environ.get("CODEX_LLM_USAGE_LOG", str(DEFAULT_USAGE_LOG_PATH)).strip()
    if log_path_value.lower() in {"", "0", "false", "off", "no"}:
        return
    record = build_usage_record(payload, prompt, result, run_metadata)
    if os.environ.get("CODEX_LLM_LOG_FULL_PAYLOAD") == "1":
        record["debug_payload"] = payload
        record["debug_result"] = result
    path = Path(log_path_value).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        return


def build_usage_record(
    payload: Dict[str, Any],
    prompt: str,
    result: Dict[str, Any],
    run_metadata: Dict[str, Any],
) -> Dict[str, Any]:
    context = payload.get("conversation_context") if isinstance(payload, dict) else {}
    if not isinstance(context, dict):
        context = {}
    recipient = context.get("recipient") or {}
    if not isinstance(recipient, dict):
        recipient = {}
    chunks = payload.get("knowledge_chunks") if isinstance(payload, dict) else []
    if not isinstance(chunks, list):
        chunks = []
    history = context.get("message_history") or []
    if not isinstance(history, list):
        history = []
    truth_pack = payload.get("truth_pack") if isinstance(payload, dict) else {}
    if not isinstance(truth_pack, dict):
        truth_pack = {}
    facts = truth_pack.get("facts") or []
    if not isinstance(facts, list):
        facts = []

    input_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    result_json = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    input_tokens = estimate_tokens(prompt)
    output_tokens = estimate_tokens(result_json)
    reply_text = str(result.get("reply_text") or result.get("text") or "")
    knowledge_chars = sum(len(str(chunk.get("text") or "")) for chunk in chunks if isinstance(chunk, dict))

    return {
        "id": f"codex_llm_usage_{uuid.uuid4().hex[:12]}",
        "created_at": utc_now(),
        "provider": "codex_cli_wrapper",
        "contract_version": str(payload.get("contract_version") or ""),
        "runtime_attempt": int(payload.get("runtime_attempt") or 1),
        "repair_reason": trim_text(str(payload.get("repair_instruction") or ""), limit=500),
        "mode": (
            "production_reply_draft"
            if os.environ.get("CODEX_LLM_ENABLED") == "1"
            else "test"
        ),
        "estimate_method": "ceil_utf8_bytes_div_4",
        "campaign_id": str(context.get("campaign_id") or ""),
        "conversation_id": str(context.get("conversation_id") or ""),
        "recipient_id": str(recipient.get("id") or ""),
        "recipient_type": str(recipient.get("type") or ""),
        "classification_intent": str((context.get("classification") or {}).get("intent") or "")
        if isinstance(context.get("classification"), dict)
        else "",
        "auto_reply_count": int(context.get("auto_reply_count") or 0),
        "ok": bool(result.get("ok")),
        "decision": str(result.get("decision") or ""),
        "intent": str(result.get("intent") or ""),
        "reason": str(result.get("reason") or ""),
        "handoff_required": bool(result.get("handoff_required")),
        "risk_level": str(result.get("risk_level") or ""),
        "confidence": float(result.get("confidence") or 0.0),
        "duration_ms": int(run_metadata.get("duration_ms") or 0),
        "returncode": run_metadata.get("returncode"),
        "error": str(run_metadata.get("error") or ""),
        "model": str(run_metadata.get("model") or ""),
        "reasoning_effort": str(run_metadata.get("reasoning_effort") or ""),
        "codex_bin": str(run_metadata.get("codex_bin") or ""),
        "proxy_failover_enabled": bool(run_metadata.get("proxy_failover_enabled")),
        "proxy_attempt_count": int(run_metadata.get("proxy_attempt_count") or 0),
        "proxy_attempt_timeout_seconds": float(
            run_metadata.get("proxy_attempt_timeout_seconds") or 0
        ),
        "proxy_route_id": str(run_metadata.get("proxy_route_id") or ""),
        "proxy_routes_considered": list(run_metadata.get("proxy_routes_considered") or []),
        "retry_backoff_count": int(run_metadata.get("retry_backoff_count") or 0),
        "retry_backoff_seconds_total": float(
            run_metadata.get("retry_backoff_seconds_total") or 0
        ),
        "rate_limit_count": int(run_metadata.get("rate_limit_count") or 0),
        "last_error_class": str(run_metadata.get("last_error_class") or ""),
        "native_output_schema": bool(run_metadata.get("native_output_schema")),
        "output_schema_sha256": str(run_metadata.get("output_schema_sha256") or ""),
        "prompt_chars": len(prompt),
        "prompt_bytes": len(prompt.encode("utf-8")),
        "input_json_chars": len(input_json),
        "input_json_bytes": len(input_json.encode("utf-8")),
        "output_json_chars": len(result_json),
        "output_json_bytes": len(result_json.encode("utf-8")),
        "reply_text_chars": len(reply_text),
        "knowledge_chunks_count": len(chunks),
        "knowledge_chars": knowledge_chars,
        "truth_fact_count": len(facts),
        "truth_runtime_characters": int(
            truth_pack.get("runtime_characters") or 0
        ),
        "truth_pack_sha256": str(truth_pack.get("sha256") or ""),
        "truth_source_pack_sha256": str(
            truth_pack.get("source_pack_sha256") or ""
        ),
        "history_messages_count": len(history),
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_total_tokens": input_tokens + output_tokens,
        "estimated_reply_text_tokens": estimate_tokens(reply_text) if reply_text else 0,
        "prompt_sha256": sha256_text(prompt),
        "input_json_sha256": sha256_text(input_json),
        "result_json_sha256": sha256_text(result_json),
    }


def expected_contract() -> Dict[str, Any]:
    return {
        "decision": "auto_reply",
        "intent": "faq_question",
        "ok": True,
        "reply_text": "короткий ответ для Telegram",
        "confidence": 0.8,
        "risk_level": "low",
        "next_state": "FAQ automation",
        "collected_fields_update": {},
        "handoff_required": False,
        "handoff_reason": "",
        "knowledge_gap": "",
        "used_sources": ["faq.md"],
        "reason": "",
    }


def is_presales_v2_payload(payload: Dict[str, Any]) -> bool:
    return str(payload.get("contract_version") or "").strip() == "presales_v2"


def native_output_schema(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not is_presales_v2_payload(payload):
        return None
    truth_pack = payload.get("truth_pack")
    source_catalog = (
        truth_pack.get("source_catalog")
        if isinstance(truth_pack, dict)
        else []
    )
    source_ids: List[str] = []
    for item in source_catalog if isinstance(source_catalog, list) else []:
        source_id = (
            str(item.get("source_id") or "").strip()
            if isinstance(item, dict)
            else str(item or "").strip()
        )
        if source_id and source_id not in source_ids:
            source_ids.append(source_id)
    source_item_schema: Dict[str, Any] = {"type": "string", "minLength": 1}
    if source_ids:
        source_item_schema["enum"] = source_ids
    conversation_context = payload.get("conversation_context")
    sector_catalog = (
        conversation_context.get("automatic_free_test_sector_catalog")
        if isinstance(conversation_context, dict)
        else []
    )
    direct_invite_sector_ids = [""]
    for item in sector_catalog if isinstance(sector_catalog, list) else []:
        sector_id = (
            str(item.get("outreach_sector_id") or "").strip()
            if isinstance(item, dict)
            else ""
        )
        if sector_id and sector_id not in direct_invite_sector_ids:
            direct_invite_sector_ids.append(sector_id)
    string_field = {"type": "string"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [
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
        ],
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "reply",
                    "reply_and_pause",
                    "reply_and_handoff",
                    "handoff",
                    "ignore",
                    "opt_out",
                    "pause",
                    "knowledge_gap",
                ],
            },
            "intent": {
                "type": "string",
                "enum": [
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
                ],
            },
            "reply_text": string_field,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "risk_level": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
            "next_state": string_field,
            "handoff_reason": string_field,
            "handoff_kind": {
                "type": "string",
                "enum": ["none", "free_test_access", "manager_action"],
            },
            "matched_direct_invite_sector_id": {
                "type": "string",
                "enum": direct_invite_sector_ids,
            },
            "knowledge_gap": string_field,
            "collected_fields_update": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "sector",
                    "sector_status",
                    "inbound_need",
                    "referral_source",
                    "priority_service",
                    "geo",
                    "signal_type",
                ],
                "properties": {
                    name: string_field
                    for name in (
                        "sector",
                        "sector_status",
                        "inbound_need",
                        "referral_source",
                        "priority_service",
                        "geo",
                        "signal_type",
                    )
                },
            },
            "coverage_complete": {"type": "boolean"},
            "turn_items": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "item_id",
                        "topic",
                        "user_item",
                        "user_evidence",
                        "status",
                        "answer_summary",
                        "reply_evidence",
                        "source_ids",
                    ],
                    "properties": {
                        "item_id": {"type": "string", "minLength": 1},
                        "topic": {"type": "string", "minLength": 1},
                        "user_item": {"type": "string", "minLength": 1},
                        "user_evidence": {"type": "string", "minLength": 1},
                        "status": {
                            "type": "string",
                            "enum": [
                                "answered",
                                "clarification_requested",
                                "action_required",
                                "needs_manager",
                                "declined_out_of_scope",
                                "not_applicable",
                            ],
                        },
                        "answer_summary": string_field,
                        "reply_evidence": string_field,
                        "source_ids": {
                            "type": "array",
                            "items": source_item_schema,
                        },
                    },
                },
            },
            "reason": string_field,
        },
    }


def resolve_model() -> str:
    return os.environ.get("CODEX_LLM_MODEL", "").strip() or DEFAULT_CODEX_MODEL


def codex_failure_class(stderr: object) -> str:
    text = " ".join(str(stderr or "").lower().split())
    if any(
        marker in text
        for marker in (
            "429",
            "rate limit",
            "rate_limit",
            "too many requests",
            "usage limit",
        )
    ):
        return "rate_limit"
    if any(
        marker in text
        for marker in (
            "overloaded",
            "temporarily unavailable",
            "service unavailable",
            "503",
        )
    ):
        return "overloaded"
    if any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
        )
    ):
        return "timeout"
    if any(
        marker in text
        for marker in (
            "unauthorized",
            "authentication",
            "401",
            "403",
        )
    ):
        return "authentication"
    return "transport"


def apply_retry_backoff(
    metadata: Dict[str, Any],
    *,
    attempt: int,
    remaining_attempts: int,
) -> None:
    if remaining_attempts <= 0:
        return
    base = parse_non_negative_float(
        os.environ.get("CODEX_LLM_RETRY_BACKOFF_SECONDS"),
        default=DEFAULT_RETRY_BACKOFF_SECONDS,
    )
    maximum = parse_non_negative_float(
        os.environ.get("CODEX_LLM_MAX_RETRY_BACKOFF_SECONDS"),
        default=DEFAULT_MAX_RETRY_BACKOFF_SECONDS,
    )
    delay = min(maximum, base * (2 ** max(0, int(attempt) - 1)))
    if delay <= 0:
        return
    time.sleep(delay)
    metadata["retry_backoff_count"] = int(
        metadata.get("retry_backoff_count") or 0
    ) + 1
    metadata["retry_backoff_seconds_total"] = round(
        float(metadata.get("retry_backoff_seconds_total") or 0.0) + delay,
        3,
    )


def resolve_reasoning_effort(prompt_payload: Optional[Dict[str, Any]] = None) -> str:
    base = normalize_reasoning_effort(
        os.environ.get("CODEX_LLM_REASONING_EFFORT")
        or os.environ.get("CODEX_LLM_MODEL_REASONING_EFFORT")
        or DEFAULT_REASONING_EFFORT
    )
    requested_raw = requested_reasoning_effort(prompt_payload or {})
    if not requested_raw:
        return base
    requested = normalize_reasoning_effort(requested_raw)
    return max_reasoning_effort(base, requested)


def requested_reasoning_effort(payload: Dict[str, Any]) -> str:
    direct = str(payload.get("reasoning_effort") or "").strip()
    if direct:
        return direct
    runtime = payload.get("runtime") or payload.get("llm_runtime") or {}
    if isinstance(runtime, dict):
        return str(runtime.get("reasoning_effort") or "").strip()
    return ""


def normalize_reasoning_effort(value: str) -> str:
    clean = value.strip().lower().replace("-", "")
    aliases = {
        "x-high": "xhigh",
        "x_high": "xhigh",
        "extra_high": "xhigh",
        "extra-high": "xhigh",
    }
    clean = aliases.get(clean, clean)
    return clean if clean in REASONING_EFFORT_ORDER else DEFAULT_REASONING_EFFORT


def max_reasoning_effort(first: str, second: str) -> str:
    return first if REASONING_EFFORT_ORDER[first] >= REASONING_EFFORT_ORDER[second] else second


def extract_prompt_payload(prompt: str) -> Optional[Dict[str, Any]]:
    marker = "INPUT_JSON:\n"
    if marker not in prompt:
        return None
    raw = prompt.split(marker, 1)[1].strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def extract_json_payload(text: str) -> Optional[Dict[str, Any]]:
    clean = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", clean, flags=re.DOTALL)
    if fenced:
        clean = fenced.group(1).strip()
    try:
        value = json.loads(clean)
    except json.JSONDecodeError:
        candidate = extract_first_object(clean)
        if not candidate:
            return None
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    return value


def extract_first_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start : end + 1]


def normalize_response(raw: Dict[str, Any]) -> Dict[str, Any]:
    response = expected_contract()
    response.update(
        {
            "decision": str(raw.get("decision") or "").strip(),
            "intent": str(raw.get("intent") or "").strip(),
            "ok": bool(raw.get("ok")),
            "reply_text": str(raw.get("reply_text") or raw.get("text") or "").strip(),
            "confidence": clamp_float(raw.get("confidence")),
            "risk_level": normalize_risk(raw.get("risk_level")),
            "next_state": str(raw.get("next_state") or "").strip(),
            "collected_fields_update": clean_string_dict(raw.get("collected_fields_update")),
            "handoff_required": bool(raw.get("handoff_required")),
            "handoff_reason": str(raw.get("handoff_reason") or "").strip(),
            "knowledge_gap": str(raw.get("knowledge_gap") or "").strip(),
            "used_sources": clean_string_list(raw.get("used_sources")),
            "reason": str(raw.get("reason") or "").strip(),
        }
    )
    return response


def normalize_contract_response(
    raw: Dict[str, Any],
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    if is_presales_v2_payload(payload):
        return raw
    return normalize_response(raw)


def error_response_for_payload(
    payload: Dict[str, Any],
    reason: str,
    detail: str = "",
) -> Dict[str, Any]:
    if not is_presales_v2_payload(payload):
        return error_response(reason, detail)
    return {
        "action": "knowledge_gap",
        "intent": "neutral",
        "reply_text": "",
        "confidence": 0.0,
        "risk_level": "medium",
        "next_state": "",
        "handoff_reason": "",
        "handoff_kind": "none",
        "matched_direct_invite_sector_id": "",
        "knowledge_gap": detail,
        "collected_fields_update": {},
        "coverage_complete": False,
        "turn_items": [],
        "reason": reason,
    }


def error_response(reason: str, detail: str = "") -> Dict[str, Any]:
    response = expected_contract()
    response.update(
        {
            "decision": "hold_for_review",
            "intent": "neutral",
            "ok": False,
            "reply_text": "",
            "confidence": 0.0,
            "risk_level": "medium",
            "next_state": "",
            "handoff_required": False,
            "handoff_reason": "",
            "knowledge_gap": detail,
            "used_sources": [],
            "reason": reason,
        }
    )
    return response


def clean_string_dict(value: Any) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, str] = {}
    for key, item in value.items():
        clean_key = str(key).strip()
        clean_value = str(item).strip()
        if clean_key and clean_value:
            result[clean_key] = clean_value
    return result


def clean_string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        clean_item = str(item).strip()
        if clean_item and clean_item not in result:
            result.append(clean_item)
    return result


def clamp_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number < 0:
        return 0.0
    if number > 1:
        return 1.0
    return number


def normalize_risk(value: Any) -> str:
    risk = str(value or "medium").strip()
    if risk not in {"low", "medium", "high"}:
        return "medium"
    return risk


def parse_timeout(value: Optional[str], default: float) -> float:
    try:
        parsed = float(value or "")
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def parse_non_negative_float(value: Optional[str], default: float) -> float:
    try:
        parsed = float(value or "")
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def trim_text(text: str, limit: int = 600) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(math.ceil(len(text.encode("utf-8")) / 4)))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def print_json(value: Dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
