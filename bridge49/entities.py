"""Контакты, шаблоны, кампании — три простые сущности с CRUD.

Никакого версионирования, апрувов и статусов согласования: если текст лежит в
шаблоне, он считается согласованным. Всё остальное решает человек до того, как
нажать `dispatch`.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any, Iterable

from . import catalog
from .store import Store, dumps, loads, new_id, now

# ---------------------------------------------------------------------------
# Контакты
# ---------------------------------------------------------------------------

CONTACT_KINDS = ("user", "channel", "chat")
CONTACT_STATUSES = ("new", "contacted", "replied", "handoff", "closed")

_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def normalize_username(value: str | None) -> str | None:
    if not value:
        return None
    username = str(value).strip().lstrip("@")
    if username.startswith("https://t.me/"):
        username = username[len("https://t.me/"):]
    elif username.startswith("t.me/"):
        username = username[len("t.me/"):]
    username = username.strip("/")
    return username or None


def add_contact(
    store: Store,
    *,
    username: str | None = None,
    tg_id: int | None = None,
    kind: str = "user",
    peer_kind: str | None = None,
    display_name: str | None = None,
    company: str | None = None,
    segment: str = "default",
    tags: Iterable[str] = (),
    vars: dict | None = None,
    note: str | None = None,
    actor: str = "cli",
) -> dict:
    """Добавить контакт. Повторный username обновляет существующего."""
    if kind not in CONTACT_KINDS:
        raise ValueError(f"kind должен быть одним из {CONTACT_KINDS}")
    username = normalize_username(username)
    if not username and tg_id is None:
        raise ValueError("нужен username или tg_id")
    if username and not _USERNAME_RE.match(username):
        raise ValueError(f"непохоже на telegram username: {username!r}")

    # Дедуплицируем и по username, и по tg_id: контакт без username иначе
    # заводился бы заново при каждом импорте, а планировщик разложил бы дубли
    # по разным аккаунтам — человек получил бы два письма от двух незнакомцев.
    existing = None
    if username:
        existing = store.one(
            "SELECT * FROM contacts WHERE lower(username) = lower(?)", (username,)
        )
    if existing is None and tg_id is not None:
        existing = store.one(
            "SELECT * FROM contacts WHERE tg_id = ?", (int(tg_id),)
        )

    payload = (
        kind, username, tg_id, peer_kind, display_name, company, segment,
        dumps(sorted(set(tags))), dumps(vars or {}), note, now(),
    )

    if existing:
        store.execute(
            "UPDATE contacts SET kind=?, username=?, tg_id=?, peer_kind=?, "
            "display_name=?, company=?, segment=?, tags=?, vars=?, note=?, "
            "updated_at=? WHERE id=?",
            payload + (existing["id"],),
        )
        contact_id = existing["id"]
    else:
        contact_id = new_id("c")
        store.execute(
            "INSERT INTO contacts(id, kind, username, tg_id, peer_kind, "
            "display_name, company, segment, tags, vars, note, updated_at, "
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (contact_id,) + payload + (now(),),
        )
    store.commit()
    return dict(store.one("SELECT * FROM contacts WHERE id = ?", (contact_id,)))


def _flatten(value) -> str:
    """Значение ячейки CSV → строка. Хвост строки приходит списком."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item).strip() for item in value if item).strip()
    return (str(value) if value is not None else "").strip()


def _find_existing(store: Store, row: dict) -> object | None:
    """Есть ли уже такой контакт — по username либо по tg_id."""
    username = normalize_username(row.get("username"))
    if username:
        return store.one(
            "SELECT id FROM contacts WHERE lower(username) = lower(?)", (username,)
        )
    if row.get("tg_id"):
        try:
            return store.one(
                "SELECT id FROM contacts WHERE tg_id = ?", (int(row["tg_id"]),)
            )
        except (TypeError, ValueError):
            return None
    return None


def import_csv(store: Store, path: Path | str, *, actor: str = "cli") -> dict:
    """Импорт CSV. Колонки: username, tg_id, name, company, segment, tags, ...

    Любые лишние колонки уезжают в `vars` и становятся доступны шаблону.
    """
    known = {
        "username", "tg_id", "kind", "peer_kind", "name", "display_name",
        "company", "segment", "tags", "note",
    }
    added = updated = failed = 0
    errors: list[str] = []

    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        # restkey обязателен: без него лишние поля строки (незакавыченная или
        # хвостовая запятая) попадают под ключ None списком, и нормализация
        # падает с AttributeError посреди импорта.
        reader = csv.DictReader(handle, restkey="_extra")
        for line_no, raw_row in enumerate(reader, start=2):
            row = {
                (key or "").strip(): _flatten(value)
                for key, value in raw_row.items()
            }
            row.pop("_extra", None)
            extra = {k: v for k, v in row.items() if k and k not in known and v}
            before = _find_existing(store, row)
            try:
                add_contact(
                    store,
                    username=row.get("username") or None,
                    tg_id=int(row["tg_id"]) if row.get("tg_id") else None,
                    kind=row.get("kind") or "user",
                    peer_kind=row.get("peer_kind") or None,
                    display_name=row.get("display_name") or row.get("name") or None,
                    company=row.get("company") or None,
                    segment=row.get("segment") or "default",
                    tags=[t for t in (row.get("tags") or "").split("|") if t],
                    vars=extra,
                    note=row.get("note") or None,
                    actor=actor,
                )
            except (ValueError, TypeError) as exc:
                failed += 1
                if len(errors) < 10:
                    errors.append(f"строка {line_no}: {exc}")
                continue
            if before:
                updated += 1
            else:
                added += 1

    store.log(actor, "contacts.import", str(path),
              f"added={added} updated={updated} failed={failed}")
    store.commit()
    return {"added": added, "updated": updated, "failed": failed, "errors": errors}


def opt_out(store: Store, contact_id: str, reason: str, *, actor: str = "cli") -> None:
    """Пометить отказ. Планировщик такие контакты больше не берёт никогда."""
    store.execute(
        "UPDATE contacts SET opted_out = 1, opt_out_reason = ?, status = 'closed', "
        "updated_at = ? WHERE id = ?",
        (reason, now(), contact_id),
    )
    store.execute(
        "UPDATE tasks SET state = 'cancelled', updated_at = ? "
        "WHERE contact_id = ? AND state = 'planned'",
        (now(), contact_id),
    )
    store.log(actor, "contacts.opt_out", contact_id, reason)
    store.commit()


# ---------------------------------------------------------------------------
# Шаблоны
# ---------------------------------------------------------------------------

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def add_template(
    store: Store, name: str, body: str, *, note: str | None = None,
    template_id: str | None = None, actor: str = "cli",
) -> dict:
    if not body.strip():
        raise ValueError("шаблон не может быть пустым")
    template_id = template_id or new_id("t")
    store.execute(
        "INSERT INTO templates(id, name, body, note, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, body=excluded.body, "
        "note=excluded.note, updated_at=excluded.updated_at",
        (template_id, name, body, note, now(), now()),
    )
    store.log(actor, "templates.upsert", template_id, name)
    store.commit()
    return dict(store.one("SELECT * FROM templates WHERE id = ?", (template_id,)))


def placeholders(body: str) -> set[str]:
    return set(_PLACEHOLDER_RE.findall(body))


def render(body: str, contact: dict) -> str:
    """Подставить значения контакта. Незаполненный плейсхолдер — ошибка.

    Лучше упасть на планировании, чем отправить человеку «Здравствуйте, {name}».
    """
    values: dict[str, Any] = dict(loads(contact.get("vars"), {}))
    values.setdefault("name", contact.get("display_name") or "")
    values.setdefault("username", contact.get("username") or "")
    values.setdefault("company", contact.get("company") or "")

    missing = sorted(
        key for key in placeholders(body) if not str(values.get(key, "")).strip()
    )
    if missing:
        raise ValueError(
            f"нет значений для плейсхолдеров {missing} "
            f"(контакт {contact.get('username') or contact.get('id')})"
        )
    return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]), body)


# ---------------------------------------------------------------------------
# Кампании
# ---------------------------------------------------------------------------

CAMPAIGN_STATUSES = ("draft", "active", "paused", "done")


def add_campaign(
    store: Store,
    *,
    name: str,
    action: str,
    template_id: str | None = None,
    segment: str = "default",
    mode: str = "lottery",
    daily_cap: int = 50,
    per_account_daily_cap: int = 12,
    params: dict | None = None,
    ttl_hours: int = 48,
    note: str | None = None,
    campaign_id: str | None = None,
    actor: str = "cli",
) -> dict:
    spec = catalog.ACTIONS.get(action)
    if spec is None:
        raise ValueError(f"неизвестное действие: {action}")
    if mode not in ("lottery", "immediate"):
        raise ValueError("mode должен быть lottery или immediate")
    if spec.needs_text and not template_id:
        raise ValueError(f"{action} требует текст — укажите шаблон")
    if template_id and not store.one(
        "SELECT id FROM templates WHERE id = ?", (template_id,)
    ):
        raise ValueError(f"нет шаблона {template_id}")

    campaign_id = campaign_id or new_id("cmp")
    store.execute(
        "INSERT INTO campaigns(id, name, action, template_id, segment, mode, "
        "daily_cap, per_account_daily_cap, params, ttl_hours, note, "
        "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET name=excluded.name, action=excluded.action, "
        "template_id=excluded.template_id, segment=excluded.segment, "
        "mode=excluded.mode, daily_cap=excluded.daily_cap, "
        "per_account_daily_cap=excluded.per_account_daily_cap, "
        "params=excluded.params, ttl_hours=excluded.ttl_hours, note=excluded.note, "
        "updated_at=excluded.updated_at",
        (campaign_id, name, action, template_id, segment, mode, int(daily_cap),
         int(per_account_daily_cap), dumps(params or {}), int(ttl_hours), note,
         now(), now()),
    )
    store.log(actor, "campaigns.upsert", campaign_id, f"{name} / {action}")
    store.commit()
    return dict(store.one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)))


def set_campaign_status(
    store: Store, campaign_id: str, status: str, *, actor: str = "cli"
) -> None:
    if status not in CAMPAIGN_STATUSES:
        raise ValueError(f"status должен быть одним из {CAMPAIGN_STATUSES}")
    store.execute(
        "UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ?",
        (status, now(), campaign_id),
    )
    store.log(actor, "campaigns.status", campaign_id, status)
    store.commit()


def get_campaign(store: Store, campaign_id: str) -> dict | None:
    row = store.one("SELECT * FROM campaigns WHERE id = ?", (campaign_id,))
    if row is None:
        return None
    campaign = dict(row)
    campaign["params"] = loads(campaign.get("params"), {})
    return campaign
