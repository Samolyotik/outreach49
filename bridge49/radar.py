"""Клиент моста Radar.

Ровно пять поверхностей, больше у моста ничего нет:

* ``enqueue_responder_outreach_command``            — поставить команду;
* ``enqueue_responder_outreach_command_attachment`` — то же с одним файлом;
* ``responder_outreach_command_results``            — статус и результат;
* ``responder_outreach_inbound_feed``               — входящие;
* ``responder_outreach_artifacts``                  — скачанные байты.

Писать в ``system_notification`` напрямую нельзя и не нужно: функция сама
выставляет все колонки, выводит business_id из аккаунта и проверяет роль.
"""
from __future__ import annotations

import asyncio
import json
import random
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

import asyncpg

from .config import RadarDsn

#: PostgreSQL отдаёт это, когда параллельный enqueue держит advisory lock.
SQLSTATE_LOCK_NOT_AVAILABLE = "55P03"
#: А это — когда активная очередь заполнена или упёрлись в retained cap.
SQLSTATE_LIMIT = "54000"

ENQUEUE_SQL = """
SELECT public.enqueue_responder_outreach_command(
    p_account_id  => $1::bigint,
    p_request     => $2::jsonb,
    p_available_at=> $3::timestamptz
) AS command_id
"""

ENQUEUE_ATTACHMENT_SQL = """
SELECT *
FROM public.enqueue_responder_outreach_command_attachment(
    $1::bigint, $2::jsonb, $3::bytea, $4::text, $5::text, $6::text,
    $7::timestamptz
)
"""

RESULTS_SQL = """
SELECT id, account_id, status, attempts, last_error, available_at,
       created_at, updated_at, details
FROM public.responder_outreach_command_results
WHERE id = ANY($1::bigint[])
ORDER BY id
"""

RESULTS_SINCE_SQL = """
SELECT id, account_id, status, attempts, last_error, available_at,
       created_at, updated_at, details
FROM public.responder_outreach_command_results
WHERE id > $1
ORDER BY id
LIMIT $2
"""

INBOUND_SQL = """
SELECT id, account_id, dedup_key, created_at, details
FROM public.responder_outreach_inbound_feed
WHERE id > $1
ORDER BY id
LIMIT $2
"""

ARTIFACT_SQL = """
SELECT id, command_notification_id, account_id, kind, filename, mime_type,
       sha256, size_bytes, created_at
FROM public.responder_outreach_artifacts
WHERE command_notification_id = ANY($1::bigint[])
ORDER BY id
"""

ARTIFACT_BYTES_SQL = """
SELECT filename, mime_type, sha256, size_bytes, content
FROM public.responder_outreach_artifacts
WHERE id = $1
"""


class BridgeError(RuntimeError):
    """Мост отказал."""


class QueueFull(BridgeError):
    """Активная очередь заполнена — нужен drain, а не новые UUID."""


def new_request_id() -> str:
    return str(uuid.uuid4())


def build_request(
    *,
    action: str,
    params: dict[str, Any] | None = None,
    mode: str = "lottery",
    request_id: str | None = None,
    expires_at: datetime | None = None,
    external_job_id: str | None = None,
    external_conversation_id: str | None = None,
) -> dict[str, Any]:
    """Собрать envelope `request`. Неизвестные поля контракт запрещает."""
    if mode not in ("lottery", "immediate"):
        raise ValueError("mode должен быть lottery или immediate")
    request: dict[str, Any] = {
        "request_id": request_id or new_request_id(),
        "action": action,
        "mode": mode,
        "params": dict(params or {}),
    }
    if expires_at is not None:
        if expires_at.tzinfo is None:
            raise ValueError("expires_at обязан быть с таймзоной")
        request["expires_at"] = expires_at.isoformat()
    if external_job_id:
        request["external_job_id"] = str(external_job_id)[:512]
    if external_conversation_id:
        request["external_conversation_id"] = str(external_conversation_id)[:512]
    return request


def _ssl_context(dsn: RadarDsn) -> ssl.SSLContext | bool:
    if dsn.sslmode != "verify-full":
        return False
    context = ssl.create_default_context(cafile=dsn.ssl_root_cert)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


class RadarBridge:
    """Пул соединений к Radar. Открывается по требованию, закрывается явно."""

    def __init__(self, dsn: RadarDsn, *, timeout: float = 20.0):
        self.dsn = dsn
        self.timeout = timeout
        self._pool: asyncpg.Pool | None = None

    async def __aenter__(self) -> "RadarBridge":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._pool is not None:
            return
        self._pool = await asyncpg.create_pool(
            host=self.dsn.host,
            port=self.dsn.port,
            database=self.dsn.database,
            user=self.dsn.user,
            password=self.dsn.password,
            ssl=_ssl_context(self.dsn),
            min_size=1,
            max_size=max(1, min(4, self.dsn.pool_max_size)),
            # PgBouncer в transaction-pooling режиме: prepared statements
            # переиспользовать нельзя.
            statement_cache_size=0,
            command_timeout=self.timeout,
            server_settings={"application_name": "bridge49"},
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise BridgeError("пул не открыт — вызовите connect()")
        return self._pool

    # -- запись -------------------------------------------------------------

    async def enqueue(
        self,
        account_id: int,
        request: dict[str, Any],
        available_at: datetime | None = None,
        *,
        attempts: int = 4,
    ) -> int:
        """Поставить одну команду. Возвращает `system_notification.id`.

        Повтор с тем же `request_id` и тем же каноническим request идемпотентен
        и вернёт прежний id — именно так следует переигрывать сомнительный
        таймаут, а не новым UUID.
        """
        available_at = available_at or datetime.now(timezone.utc)
        payload = json.dumps(request, ensure_ascii=False)
        last_error: Exception | None = None

        for attempt in range(attempts):
            try:
                async with self.pool.acquire() as conn:
                    row = await conn.fetchrow(
                        ENQUEUE_SQL, account_id, payload, available_at
                    )
                return int(row["command_id"])
            except asyncpg.PostgresError as exc:
                code = getattr(exc, "sqlstate", None)
                if code == SQLSTATE_LIMIT:
                    raise QueueFull(str(exc)) from exc
                if code != SQLSTATE_LOCK_NOT_AVAILABLE:
                    raise BridgeError(f"enqueue отклонён: {exc}") from exc
                last_error = exc
                # Узкий advisory lock: конкуренция короткая, ждём с джиттером.
                await asyncio.sleep(0.2 * (2**attempt) + random.random() * 0.3)

        raise BridgeError(f"enqueue не прошёл за {attempts} попыток: {last_error}")

    async def enqueue_with_attachment(
        self,
        account_id: int,
        request: dict[str, Any],
        content: bytes,
        *,
        kind: str,
        filename: str,
        mime_type: str,
        available_at: datetime | None = None,
    ) -> tuple[int, int]:
        """Команда v2 с ровно одним вложением. Возвращает (command_id, artifact_id).

        Функция сама считает SHA-256, строит manifest и пишет обе строки одной
        транзакцией. Требует account-local `outbound_attachments_enabled=true`.
        """
        available_at = available_at or datetime.now(timezone.utc)
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                ENQUEUE_ATTACHMENT_SQL,
                account_id,
                json.dumps(request, ensure_ascii=False),
                content,
                kind,
                filename,
                mime_type,
                available_at,
            )
        return int(row["command_id"]), int(row["attachment_id"])

    # -- чтение -------------------------------------------------------------

    async def results(self, command_ids: Sequence[int]) -> list[dict]:
        if not command_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(RESULTS_SQL, [int(i) for i in command_ids])
        return [_normalize(row) for row in rows]

    async def results_since(self, after_id: int, limit: int = 500) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(RESULTS_SINCE_SQL, int(after_id), int(limit))
        return [_normalize(row) for row in rows]

    async def inbound(self, after_id: int, limit: int = 500) -> list[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(INBOUND_SQL, int(after_id), int(limit))
        return [_normalize(row) for row in rows]

    async def artifacts(self, command_ids: Sequence[int]) -> list[dict]:
        if not command_ids:
            return []
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(ARTIFACT_SQL, [int(i) for i in command_ids])
        return [_normalize(row) for row in rows]

    async def artifact_bytes(self, artifact_id: int) -> dict | None:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(ARTIFACT_BYTES_SQL, int(artifact_id))
        return dict(row) if row else None

    async def health(self) -> dict:
        """Проверка живости. Только SELECT, ничего не создаёт."""
        async with self.pool.acquire() as conn:
            version = await conn.fetchval("SELECT version()")
            commands = await conn.fetchval(
                "SELECT count(*) FROM public.responder_outreach_command_results"
            )
            active = await conn.fetchval(
                "SELECT count(*) FROM public.responder_outreach_command_results "
                "WHERE status IN ('new', 'processing')"
            )
            feed = await conn.fetchval(
                "SELECT count(*) FROM public.responder_outreach_inbound_feed"
            )
            max_command = await conn.fetchval(
                "SELECT coalesce(max(id), 0) "
                "FROM public.responder_outreach_command_results"
            )
            max_inbound = await conn.fetchval(
                "SELECT coalesce(max(id), 0) "
                "FROM public.responder_outreach_inbound_feed"
            )
        return {
            "dsn": self.dsn.describe(),
            "server": str(version).split(" on ")[0],
            "commands_visible": int(commands),
            "commands_active": int(active),
            "inbound_visible": int(feed),
            "max_command_id": int(max_command),
            "max_inbound_id": int(max_inbound),
        }


def _normalize(row: asyncpg.Record) -> dict:
    """asyncpg отдаёт jsonb строкой — разворачиваем, время → ISO."""
    out = dict(row)
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif key in ("details", "result") and isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except ValueError:
                pass
    return out


def default_expiry(hours: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=max(1, int(hours)))
