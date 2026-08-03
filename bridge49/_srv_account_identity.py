from __future__ import annotations

import sqlite3
from typing import Dict, Optional


GENDERS = {"male", "female", "neutral"}


def normalize_gender(value: object) -> str:
    clean = str(value or "").strip().lower()
    return clean if clean in GENDERS else "neutral"


def sender_display_name(first_name: str, last_name: str, fallback: str = "") -> str:
    parts = [part.strip() for part in (first_name, last_name) if part and part.strip()]
    if parts:
        return " ".join(parts)
    return fallback.strip()


def gender_instruction(gender: str) -> str:
    normalized = normalize_gender(gender)
    if normalized == "male":
        return "Если нужна самореференция на русском, используй мужские формы: передал, готов, рад."
    if normalized == "female":
        return "Если нужна самореференция на русском, используй женские формы: передала, готова, рада."
    return "Если нужна самореференция на русском, избегай гендерных форм: лучше передам, подключу коллегу, коллега уже подключается."


def handoff_confirmation_intro(gender: str) -> str:
    normalized = normalize_gender(gender)
    if normalized == "female":
        return "Готово, я уже передала заявку менеджеру."
    if normalized == "male":
        return "Готово, я уже передал заявку менеджеру."
    return "Готово, заявку уже передали менеджеру."


def sender_account_identity(
    conn: sqlite3.Connection,
    sender_account_id: str,
) -> Dict[str, str]:
    row = conn.execute(
        """
        SELECT id, account_label, manager_name, role,
               telegram_first_name, telegram_last_name, gender
        FROM sender_accounts
        WHERE id = ?
        """,
        (sender_account_id,),
    ).fetchone()
    if row is None:
        return {
            "id": sender_account_id,
            "account_label": "",
            "role": "",
            "first_name": "",
            "last_name": "",
            "display_name": "",
            "gender": "neutral",
            "gender_instruction": gender_instruction("neutral"),
        }

    first_name = str(row["telegram_first_name"] or "").strip()
    last_name = str(row["telegram_last_name"] or "").strip()
    fallback = str(row["manager_name"] or row["account_label"] or "").strip()
    gender = normalize_gender(row["gender"])
    return {
        "id": str(row["id"]),
        "account_label": str(row["account_label"] or ""),
        "role": str(row["role"] or ""),
        "first_name": first_name,
        "last_name": last_name,
        "display_name": sender_display_name(first_name, last_name, fallback),
        "gender": gender,
        "gender_instruction": gender_instruction(gender),
    }


def sender_account_gender(conn: sqlite3.Connection, sender_account_id: Optional[str]) -> str:
    if not sender_account_id:
        return "neutral"
    row = conn.execute(
        "SELECT gender FROM sender_accounts WHERE id = ?",
        (sender_account_id,),
    ).fetchone()
    if row is None:
        return "neutral"
    return normalize_gender(row["gender"])
