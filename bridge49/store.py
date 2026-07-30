"""Локальное состояние bridge49 — один SQLite-файл.

Схема сознательно плоская: девять таблиц, никаких триггеров, никаких
контрактных фенсов. Всё, что должно быть неизменяемым, неизменяемо на стороне
Radar; здесь мы держим только план работ и зеркало того, что мост вернул.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
-- Аккаунты Radar, через которые мы работаем. Снимок, обновляется sync-accounts.
CREATE TABLE IF NOT EXISTS accounts (
  id               INTEGER PRIMARY KEY,          -- Radar Account.id (801..852)
  label            TEXT    NOT NULL,
  program_code     TEXT,
  role             TEXT    NOT NULL,             -- outreach-роль: dm_sender и т.п.
  allowed_actions  TEXT    NOT NULL DEFAULT '[]',
  enabled          INTEGER NOT NULL DEFAULT 0,
  publish_inbound  INTEGER NOT NULL DEFAULT 0,
  allow_immediate  INTEGER NOT NULL DEFAULT 0,
  runtime_state    TEXT,
  last_heartbeat_at TEXT,
  paused           INTEGER NOT NULL DEFAULT 0,   -- наша локальная пауза
  note             TEXT,
  synced_at        TEXT    NOT NULL
);

-- Кого мы хотим достать.
CREATE TABLE IF NOT EXISTS contacts (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL DEFAULT 'user',    -- user | channel | chat
  username      TEXT,
  tg_id         INTEGER,
  peer_kind     TEXT,                            -- для chat_id-селектора
  display_name  TEXT,
  company       TEXT,
  segment       TEXT NOT NULL DEFAULT 'default',
  tags          TEXT NOT NULL DEFAULT '[]',
  status        TEXT NOT NULL DEFAULT 'new',     -- new|contacted|replied|handoff|closed
  opted_out     INTEGER NOT NULL DEFAULT 0,
  opt_out_reason TEXT,
  vars          TEXT NOT NULL DEFAULT '{}',      -- подстановки для шаблона
  note          TEXT,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_segment ON contacts(segment, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_username
  ON contacts(lower(username)) WHERE username IS NOT NULL;

-- Тексты. Плейсхолдеры вида {name}.
CREATE TABLE IF NOT EXISTS templates (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  body       TEXT NOT NULL,
  note       TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Кампания = действие + текст + сегмент + темп.
CREATE TABLE IF NOT EXISTS campaigns (
  id             TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  action         TEXT NOT NULL,
  template_id    TEXT REFERENCES templates(id),
  segment        TEXT NOT NULL DEFAULT 'default',
  mode           TEXT NOT NULL DEFAULT 'lottery',   -- lottery | immediate
  status         TEXT NOT NULL DEFAULT 'draft',     -- draft|active|paused|done
  daily_cap      INTEGER NOT NULL DEFAULT 50,
  per_account_daily_cap INTEGER NOT NULL DEFAULT 12,
  params         TEXT NOT NULL DEFAULT '{}',        -- статические доп. params
  ttl_hours      INTEGER NOT NULL DEFAULT 48,       -- request.expires_at
  note           TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);

-- Единица работы: один контакт × одна кампания × один аккаунт.
CREATE TABLE IF NOT EXISTS tasks (
  id             TEXT PRIMARY KEY,
  campaign_id    TEXT NOT NULL REFERENCES campaigns(id),
  contact_id     TEXT NOT NULL REFERENCES contacts(id),
  account_id     INTEGER NOT NULL REFERENCES accounts(id),
  action         TEXT NOT NULL,
  params         TEXT NOT NULL DEFAULT '{}',
  mode           TEXT NOT NULL DEFAULT 'lottery',
  scheduled_at   TEXT NOT NULL,
  expires_at     TEXT,
  state          TEXT NOT NULL DEFAULT 'planned',
      -- planned -> queued -> done | failed | skipped | cancelled | blocked
  request_id     TEXT,                              -- UUID, который ушёл в Radar
  command_id     INTEGER,                           -- system_notification.id
  outcome        TEXT,
  error_code     TEXT,
  error_message  TEXT,
  result         TEXT,
  dispatched_at  TEXT,
  finished_at    TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_tasks_command ON tasks(command_id);
CREATE INDEX IF NOT EXISTS idx_tasks_account_day ON tasks(account_id, dispatched_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_campaign_contact
  ON tasks(campaign_id, contact_id);

-- Зеркало входящего фида Radar.
CREATE TABLE IF NOT EXISTS inbound (
  id             INTEGER PRIMARY KEY,             -- system_notification.id
  account_id     INTEGER NOT NULL,
  surface        TEXT NOT NULL,                   -- private_dm | channel_dm
  peer_key       TEXT NOT NULL,
  peer_username  TEXT,
  peer_tg_id     INTEGER,
  sender_tg_id   INTEGER,
  tg_message_id  INTEGER,
  text           TEXT,
  sent_at        TEXT,
  raw            TEXT NOT NULL,
  contact_id     TEXT REFERENCES contacts(id),
  handled        INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_inbound_peer ON inbound(account_id, peer_key, id);
CREATE INDEX IF NOT EXISTS idx_inbound_unhandled ON inbound(handled, id);

-- Диалог = связка аккаунт ↔ собеседник. Растёт из tasks и inbound.
CREATE TABLE IF NOT EXISTS threads (
  id              TEXT PRIMARY KEY,
  account_id      INTEGER NOT NULL,
  peer_key        TEXT NOT NULL,
  contact_id      TEXT REFERENCES contacts(id),
  campaign_id     TEXT REFERENCES campaigns(id),
  surface         TEXT NOT NULL DEFAULT 'private_dm',
  state           TEXT NOT NULL DEFAULT 'open',   -- open|awaiting|handoff|closed
  last_outbound_at TEXT,
  last_inbound_at  TEXT,
  owner           TEXT,
  summary         TEXT,
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_threads_peer ON threads(account_id, peer_key);

-- Что менеджеру надо взять руками.
CREATE TABLE IF NOT EXISTS handoffs (
  id          TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL REFERENCES threads(id),
  reason      TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'new',        -- new|taken|closed
  owner       TEXT,
  note        TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_handoffs_status ON handoffs(status, created_at);

-- Курсоры чтения из Radar и прочие мелочи.
CREATE TABLE IF NOT EXISTS state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Простой журнал: кто что сделал.
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  at         TEXT NOT NULL,
  actor      TEXT NOT NULL,
  kind       TEXT NOT NULL,
  subject    TEXT,
  detail     TEXT,
  UNIQUE(at, kind, subject, detail) ON CONFLICT IGNORE
);
CREATE INDEX IF NOT EXISTS idx_events_at ON events(at DESC);
"""


def now() -> str:
    """UTC ISO-8601 с секундной точностью — единый формат времени в базе."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Store:
    """Тонкая обёртка над sqlite3. Без ORM — запросов немного и они простые."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)
        self.set_state("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    # -- базовые операции ---------------------------------------------------

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)))

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.conn.execute(sql, tuple(params)).fetchone()
        return rows

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def commit(self) -> None:
        self.conn.commit()

    # -- state --------------------------------------------------------------

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM state WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # -- журнал -------------------------------------------------------------

    def log(self, actor: str, kind: str, subject: str = "", detail: str = "") -> None:
        self.execute(
            "INSERT INTO events(at, actor, kind, subject, detail) "
            "VALUES(?, ?, ?, ?, ?)",
            (now(), actor, kind, subject, detail),
        )


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default if default is not None else {}
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default if default is not None else {}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> Iterator[dict]:
    for row in rows:
        yield dict(row)
