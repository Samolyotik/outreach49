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

SCHEMA_VERSION = 9

SCHEMA = """
-- Аккаунты Radar, через которые мы работаем. Снимок, обновляется sync-accounts.
CREATE TABLE IF NOT EXISTS accounts (
  id               INTEGER PRIMARY KEY,          -- Radar Account.id (801..852)
  label            TEXT    NOT NULL,
  program_code     TEXT,
  role             TEXT    NOT NULL,             -- главная роль, для отчётов
  -- Все роли аккаунта. Radar разрешает несколько из семейства отправителей и
  -- объединяет их действия; помнить только первую значит не уметь того, что
  -- аккаунту разрешено второй.
  roles            TEXT    NOT NULL DEFAULT '[]',
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
CREATE INDEX IF NOT EXISTS idx_contacts_tg_id
  ON contacts(tg_id) WHERE tg_id IS NOT NULL;

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
  -- Разрешает писать тем, кого уже касались раньше (догоняющая волна).
  -- Отдельной колонкой, а не ключом в params: params уезжают в Radar как
  -- параметры действия и валидируются каталогом — чужой ключ там не пройдёт.
  allow_repeat_contacts INTEGER NOT NULL DEFAULT 0,
  -- Кому поручать. Пусто — любому, кому действие разрешено. Нужно там, где
  -- роль решает не только допуск: `check_channel_dm_metadata` по контракту
  -- разрешён и отправителям каналов, но разведку каталога ведут читатели.
  roles          TEXT NOT NULL DEFAULT '[]',
  -- Ещё уже: поимённый список аккаунтов. Роль отвечает на вопрос «кому это
  -- вообще можно», список — на вопрос «кто именно этим занят». Нужен, чтобы
  -- вести две разведки параллельно разными людьми, а не одну за другой.
  accounts       TEXT NOT NULL DEFAULT '[]',
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
-- Одному контакту — одно касание в кампании. Правило про рассылку: второй
-- заход на тот же сегмент не должен слать человеку второе «первое касание».
--
-- Ответы под него не подпадают, и это не послабление, а разная природа. С
-- человеком, который нам пишет, разговор продолжается: он спросил цену, потом
-- спросил про сроки, потом как нас зовут. Сплошной индекс означал бы, что
-- ответить можно ровно один раз за всю жизнь диалога — второе сообщение
-- падало бы с IntegrityError. Именно так и случилось 03.08.
--
-- От двух одновременных ответов защищает не индекс, а проверка в queue_reply:
-- она смотрит, нет ли уже поставленного и неотправленного.
--
-- Ответов у нас два вида, и исключать надо оба. Сначала исключили только
-- личку, а `reply_channel_dm` остался под индексом — то есть на поверхности
-- каналов машина отвечала человеку ровно один раз за всё время, а дальше
-- всегда заводила карточку. Снаружи это выглядит не как отказ, а как
-- молчание: в журнале три IntegrityError, в диалоге тишина.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_campaign_contact
  ON tasks(campaign_id, contact_id)
  WHERE action NOT IN ('reply_private_dm', 'reply_channel_dm');

-- Глобальная история касаний: кому мы вообще писали, независимо от кампании.
-- Без неё вторая кампания на пересекающийся сегмент шлёт человеку второе
-- «первое касание», и нередко с другого аккаунта — со стороны получателя это
-- выглядит как рассылка веером. Уникальность tasks держит только пару
-- (кампания, контакт) и поперёк кампаний не защищает.
CREATE TABLE IF NOT EXISTS contact_touches (
  contact_id       TEXT PRIMARY KEY REFERENCES contacts(id),
  first_sent_at    TEXT NOT NULL,
  last_sent_at     TEXT NOT NULL,
  sent_count       INTEGER NOT NULL DEFAULT 1,
  last_account_id  INTEGER,
  last_campaign_id TEXT,
  last_task_id     TEXT
);

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

-- Что мы уже отправили в рабочие группы. Нужно не для показа, а для уборки:
-- 05.08 выяснилось, что вычистить из группы лишнее нечем — номеров сообщений
-- мы не сохраняли, и единственным способом узнать своё сообщение оставалось
-- переслать его и прочитать. Теперь номер известен сразу.
CREATE TABLE IF NOT EXISTS forum_posts (
  chat_id     TEXT    NOT NULL,
  message_id  INTEGER NOT NULL,
  thread_id   INTEGER,
  kind        TEXT    NOT NULL,   -- outgoing | inbound | handoff
  ref         TEXT,               -- id задачи, входящего или карточки
  contact_id  TEXT REFERENCES contacts(id),
  created_at  TEXT    NOT NULL,
  PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_forum_posts_kind
  ON forum_posts(chat_id, kind, created_at);

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

-- Переписка, перенесённая из прежней системы. Наши собственные исходящие
-- живут в tasks, входящие — в inbound; сюда попадает только импорт, чтобы
-- история диалога не обрывалась на дате перехода.
CREATE TABLE IF NOT EXISTS history (
  id          TEXT PRIMARY KEY,
  thread_id   TEXT NOT NULL REFERENCES threads(id),
  direction   TEXT NOT NULL,                     -- inbound | outbound
  author      TEXT,                              -- system | manager | recipient
  text        TEXT NOT NULL DEFAULT '',
  sent_at     TEXT,
  origin      TEXT NOT NULL DEFAULT 'import',
  -- id сообщения в Telegram по данным исходной системы. Нужен, чтобы позже
  -- разложить эту историю в responder-домен Radar, не выводя связи заново.
  source_ref  TEXT,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_thread ON history(thread_id, sent_at);

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

-- Автовыдача бесплатного теста: согласие → выпуск ссылки → доставка.
--
-- Заявка живёт отдельно от задачи доставки, потому что это разные сущности с
-- разной судьбой: ссылка может быть выпущена, а доставка не поставлена, и
-- наоборот — задача может уйти в очередь, а сервис ответить повтором. Уникален
-- request_id: он выводится из (диалог, входящее), поэтому повторный разбор того
-- же хода не заведёт вторую заявку и не выдаст человеку вторую ссылку.
CREATE TABLE IF NOT EXISTS direct_invites (
  id                    TEXT PRIMARY KEY,
  request_id            TEXT NOT NULL UNIQUE,
  thread_id             TEXT NOT NULL REFERENCES threads(id),
  contact_id            TEXT NOT NULL REFERENCES contacts(id),
  account_id            INTEGER NOT NULL,
  inbound_id            TEXT NOT NULL,
  source_channel        TEXT NOT NULL,
  outreach_sector_id    TEXT NOT NULL,
  sector_id             TEXT NOT NULL,
  sector_name           TEXT NOT NULL,
  test_group_profile_id TEXT NOT NULL,
  consent_recorded_at   TEXT NOT NULL,
  consent_source        TEXT NOT NULL,
  invite_id             TEXT UNIQUE,
  invite_expires_at     TEXT,
  task_id               TEXT UNIQUE REFERENCES tasks(id),
  status                TEXT NOT NULL,
  attempt_count         INTEGER NOT NULL DEFAULT 0,
  next_attempt_at       TEXT,
  last_error            TEXT,
  link_delivered_at     TEXT,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  CHECK (source_channel IN ('channel_dm', 'private_dm', 'public_chat')),
  CHECK (status IN ('test_agreed', 'invite_created_delivery_pending',
                    'link_delivered', 'invite_creation_failed',
                    'delivery_failed', 'cancelled'))
);
CREATE INDEX IF NOT EXISTS idx_direct_invites_status
  ON direct_invites(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_direct_invites_contact
  ON direct_invites(contact_id, status);

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


#: Условие частичной уникальности задач. Держится отдельной строкой, потому
#: что живёт в двух местах сразу: в схеме для новой базы и в пересоздании
#: индекса для уже работающей. Разъедутся эти два места — и правило будет
#: разным в зависимости от того, когда базу завели.
#:
#: Набор имён обязан совпадать с ``replies.REPLY_ACTIONS`` — каноном «что у нас
#: считается ответом». Импортировать его сюда нельзя (``replies`` сам стоит на
#: ``store``), поэтому совпадение стережёт тест.
_TASKS_UNIQUE_WHERE = "action NOT IN ('reply_private_dm', 'reply_channel_dm')"

_TASKS_UNIQUE_INDEX_NAME = "idx_tasks_campaign_contact"

#: Оператор целиком, а не одно условие. Сравнивать надо всё правило: индекс с
#: верным условием, но неуникальный или по другим колонкам, — это ровно тот же
#: класс ошибки, что и починенный. Проверять одну грань правила из нескольких
#: значит однажды пропустить остальные.
_TASKS_UNIQUE_INDEX_SQL = (
    f"CREATE UNIQUE INDEX {_TASKS_UNIQUE_INDEX_NAME} "
    "ON tasks(campaign_id, contact_id) "
    f"WHERE {_TASKS_UNIQUE_WHERE}"
)


def _normalized_index_sql(sql: str | None) -> str:
    """Текст индекса, приведённый к сравнимому виду.

    SQLite хранит запрос почти как его написали: переносы строк и отступы
    остаются, а ``IF NOT EXISTS`` и хвостовая точка с запятой вырезаются.
    Схема пишет индекс в три строки, пересоздание — в одну; смысл у них один.
    """
    text = " ".join(str(sql or "").split())
    return text.replace("INDEX IF NOT EXISTS ", "INDEX ").rstrip(";")


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
        self._ensure_columns()
        self.set_state("schema_version", str(SCHEMA_VERSION))
        self.conn.commit()

    #: Колонки, добавленные после первой версии схемы. `CREATE TABLE IF NOT
    #: EXISTS` их в уже существующую базу не принесёт, поэтому доливаем руками.
    LATE_COLUMNS = (
        ("history", "source_ref", "TEXT"),
        # Момент попытки выпуска, в отличие от dispatched_at заполняется и
        # тогда, когда мост ответил отказом: попытка всё равно расходует темп.
        ("tasks", "attempted_at", "TEXT"),
        ("campaigns", "allow_repeat_contacts", "INTEGER NOT NULL DEFAULT 0"),
        # Почему автоответ стоит перечитать глазами. Пусто — движок был уверен.
        # Метка живёт на задаче, а не на диалоге: перечитывать нужно конкретное
        # отправленное сообщение, и таких в одном диалоге может быть несколько.
        ("tasks", "review_reason", "TEXT"),
        # Что модель успела узнать о собеседнике (сфера, задача, источник).
        # Копится по диалогу и уезжает в следующий запрос как discovery_context.
        ("threads", "presales_context", "TEXT"),
        # Ветка в рабочей группе, своя у каждого собеседника. Одна общая лента
        # на всех читается как поток, а не как переписка: чтобы понять, что
        # ответил конкретный человек, приходится вылавливать его сообщения
        # среди чужих.
        ("contacts", "forum_thread_id", "INTEGER"),
        # Ветка в группе диалогов. Отдельной колонкой от `forum_thread_id`:
        # групп теперь две, и номера веток в них независимы — подставить
        # ветку одной группы в другую значит писать в чужой разговор или
        # в никуда.
        ("contacts", "dialog_thread_id", "INTEGER"),
        # Кому поручать эту кампанию, если роль решает не только допуск.
        # Пусто — любой, кому действие разрешено. Отдельной колонкой, а не
        # ключом в params: params уезжают в Radar как параметры действия и
        # валидируются каталогом — чужой ключ там не пройдёт.
        ("campaigns", "roles", "TEXT NOT NULL DEFAULT '[]'"),
        # Поимённый список аккаунтов кампании. Пусто — любой из подходящих
        # по роли.
        ("campaigns", "accounts", "TEXT NOT NULL DEFAULT '[]'"),
        ("accounts", "roles", "TEXT NOT NULL DEFAULT '[]'"),
    )

    def _ensure_columns(self) -> None:
        for table, column, kind in self.LATE_COLUMNS:
            have = {
                row["name"]
                for row in self.conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in have:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {kind}"
                )
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        """Переделать индексы, которые изменились после создания базы.

        ``CREATE INDEX IF NOT EXISTS`` существующий индекс не трогает, поэтому
        превращение сплошной уникальности в частичную приходится делать руками:
        иначе в уже живущей базе останется старое правило, и второй ответ
        одному человеку так и будет падать.

        Сравнивать надо само условие, а не наличие слова ``WHERE``. Проверка
        «частичный ли индекс» отвечает «да» на любое условие, включая
        устаревшее, — и вторая правка этого же правила молча не доезжает до
        живых баз. Ровно это и случилось с ответами в каналах.
        """
        if self._tasks_index_is_current():
            return

        # Дальше — редкий путь, и он обязан быть неделимым.
        #
        # DDL в sqlite3 идёт в автокоммите: между DROP и CREATE есть окно, в
        # котором индекса нет вовсе. Базу открывает каждая команда и каждый
        # таймер — около одиннадцати открытий в минуту, причём systemd
        # намеренно склеивает таймеры в одно пробуждение. В это окно попадают
        # три исхода, и все три проверены вживую: второй DROP падает «нет
        # такого индекса»; второй CREATE падает «индекс уже есть»; и худший —
        # чужая вставка успевает пролезть без индекса, после чего CREATE
        # падает уже навсегда, а вместе с ним каждое следующее открытие базы.
        #
        # Эксклюзивная запись закрывает все три: второй процесс дождётся
        # своей очереди, перечитает состояние уже под замком и увидит, что
        # работа сделана. А откат вернёт прежний индекс, если создание нового
        # почему-то не удастся, — вместо базы, оставшейся вообще без правила.
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if not self._tasks_index_is_current():
                self.conn.execute(
                    f"DROP INDEX IF EXISTS {_TASKS_UNIQUE_INDEX_NAME}")
                # Новое условие строго уже прежнего: под индексом остаётся
                # меньше строк, чем раньше. Значит создание не может упереться
                # в уже существующие дубли и не требует предварительной чистки.
                self.conn.execute(_TASKS_UNIQUE_INDEX_SQL)
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _tasks_index_is_current(self) -> bool:
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master "
            " WHERE type = 'index' AND name = ?",
            (_TASKS_UNIQUE_INDEX_NAME,),
        ).fetchone()
        if row is None:
            return False
        return (_normalized_index_sql(row["sql"])
                == _normalized_index_sql(_TASKS_UNIQUE_INDEX_SQL))

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
