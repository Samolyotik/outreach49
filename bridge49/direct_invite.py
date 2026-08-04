"""Автоматическая выдача ссылки на бесплатный тест.

Когда человек соглашается на тест и его сфера — в точном списке разрешённых,
ссылку он получает сам, без менеджера. Это перенос ветки прежнего контура
(`startbot_direct_invite.py` + `startbot_invites.py`), которая при переносе
`conversation.py` не приехала: приехал только запрет модели писать ссылку, а
механизм, который эту ссылку выдаёт, остался там.

Из-за этого любое согласие уходило в ручной путь. Причём именно та сфера, ради
которой ветку и заводили («Авто из-за границы»), — единственная включённая в
боевом конфиге.

## Почему модель не может выдать ссылку сама

Ссылки непрозрачные и одноразовые (`?start=<opaque>`), закрепляются за первым
открывшим аккаунтом и живут семь дней. Их выпускает внешний сервис StartBot по
HTTP. Придумать такую нельзя, и промпт прямо запрещает модели писать её в
`reply_text` — а `presales_v2.contains_internal_startbot_name` караулит нарушение
и рубит всё решение целиком. Модель делает свою часть: возвращает
`handoff_kind=free_test_access` и точный `matched_direct_invite_sector_id`.

## Разделение труда

Согласие записывается синхронно, в той же транзакции, что и решение движка, —
иначе обрыв между «человек согласился» и «мы это запомнили» терял бы согласие.
А выпуск ссылки идёт отдельным проходом: это сетевой вызов на секунды, и
держать на нём разбор входящих нельзя.

Доставка — обычная задача в очереди ответов. Никаких особых путей отправки: та
же кампания, тот же диспетчер, те же паузы и то же окно.

## Fail-closed

Нет конфига, выключена ветка, не совпала сфера, недоступен сервис — ссылка не
выдаётся, и разговор идёт ручным путём с карточкой менеджеру. Ровно как у них:
автоматика умеет только добавлять, отнять она ничего не может.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import replies
from .store import Store, new_id, now

#: Где лежит описание разрешённых сфер. Имя переменной то же, что в прежнем
#: контуре, — файл переиспользуется как есть, без переписывания.
BRANCH_CONFIG_ENV = "OUTREACH_STARTBOT_DIRECT_INVITE_CONFIG"
API_BASE_URL_ENV = "OUTREACH_STARTBOT_API_BASE_URL"
API_TOKEN_ENV = "OUTREACH_STARTBOT_API_SERVICE_TOKEN"
API_TIMEOUT_ENV = "OUTREACH_STARTBOT_TIMEOUT_SECONDS"

#: Служебная кампания доставки ссылок. Отдельная от автоответов: у неё другой
#: смысл и другой разбор в отчётах, а лимиты живут на кампании.
INVITE_CAMPAIGN_ID = "direct_invites"
INVITE_CAMPAIGN_NAME = "Автовыдача бесплатного теста"

#: Тот самый вердикт движка, ради которого всё и затевалось.
CONSENT_HANDOFF_KIND = "free_test_access"

#: Состояния заявки. Имена сохранены от прежнего контура, чтобы историю двух
#: баз можно было сравнивать глазами без словаря переводов.
STATUS_AGREED = "test_agreed"
STATUS_CREATED = "invite_created_delivery_pending"
STATUS_DELIVERED = "link_delivered"
STATUS_CREATE_FAILED = "invite_creation_failed"
STATUS_CANCELLED = "cancelled"

#: Роль отправителя → канал, из которого пришло согласие. Сервис различает их,
#: и подставлять чужой канал нельзя: он попадёт в учёт выданных доступов.
ROLE_TO_CHANNEL = {
    "channel_sender": "channel_dm",
    "dm_sender": "private_dm",
    "chat_sender": "public_chat",
}

#: Пауза перед повторной попыткой после сетевого отказа. Растёт линейно: сервис
#: за туннелем, и частые повторы при обрыве туннеля ничего не чинят.
RETRY_BACKOFF_MINUTES = 15


class DirectInviteError(RuntimeError):
    """Ветку нельзя выполнить, и это требует внимания человека."""


class BranchInactive(DirectInviteError):
    """Для этой сферы автоматическая выдача не включена. Это не ошибка."""


# ---------------------------------------------------------------------------
# конфигурация
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SectorProfile:
    outreach_sector_id: str
    sector_id: str
    sector_name: str
    test_group_profile_id: str


@dataclass(frozen=True)
class BranchConfig:
    """Точный список сфер, которым доступ выдаётся автоматически."""

    enabled: bool
    active_sector_ids: frozenset[str]
    sector_profiles: dict[str, SectorProfile]
    validity_days: int = 7
    max_attempts: int = 5
    source_path: str = ""

    @classmethod
    def disabled(cls, source_path: str = "") -> "BranchConfig":
        return cls(False, frozenset(), {}, source_path=source_path)

    @classmethod
    def from_env(cls) -> "BranchConfig":
        path = os.getenv(BRANCH_CONFIG_ENV, "").strip()
        if not path:
            return cls.disabled()
        try:
            return cls.from_path(path)
        except (OSError, ValueError, DirectInviteError):
            # Кривой или недоступный конфиг — это «выключено», а не падение
            # разбора входящих. Разговор пойдёт ручным путём.
            return cls.disabled(path)

    @classmethod
    def from_path(cls, path: str | Path) -> "BranchConfig":
        normalized = Path(path).expanduser().resolve()
        raw = json.loads(normalized.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise DirectInviteError("конфиг должен быть JSON-объектом")
        if int(raw.get("schema_version") or 0) != 1:
            raise DirectInviteError("неподдерживаемая schema_version конфига")
        if not isinstance(raw.get("enabled"), bool):
            raise DirectInviteError("enabled должен быть булевым")

        profiles: dict[str, SectorProfile] = {}
        for key, value in (raw.get("sector_profiles") or {}).items():
            if not isinstance(value, dict):
                raise DirectInviteError(f"профиль сферы {key} должен быть объектом")
            outreach_sector_id = str(value.get("outreach_sector_id") or key).strip()
            sector_id = str(value.get("sector_id") or "").strip()
            sector_name = str(value.get("sector_name") or "").strip()
            profile_id = str(value.get("test_group_profile_id") or "").strip()
            if (key != outreach_sector_id or not sector_id or not sector_name
                    or not profile_id):
                raise DirectInviteError(f"неполный профиль сферы {key}")
            profiles[outreach_sector_id] = SectorProfile(
                outreach_sector_id=outreach_sector_id,
                sector_id=sector_id,
                sector_name=sector_name,
                test_group_profile_id=profile_id,
            )

        raw_active = raw.get("active_sector_ids") or []
        if not isinstance(raw_active, list) or any(
            not isinstance(item, str) for item in raw_active
        ):
            raise DirectInviteError("active_sector_ids должен быть списком строк")
        active = frozenset(item.strip() for item in raw_active if item.strip())
        unknown = active - set(profiles)
        if unknown:
            raise DirectInviteError(
                "у включённых сфер нет профиля: " + ", ".join(sorted(unknown))
            )

        validity_days = int(raw.get("validity_days") or 7)
        max_attempts = int(raw.get("max_attempts") or 5)
        if not 1 <= validity_days <= 30:
            raise DirectInviteError("validity_days вне 1..30")
        if not 1 <= max_attempts <= 10:
            raise DirectInviteError("max_attempts вне 1..10")
        enabled = bool(raw["enabled"])
        if enabled and not active:
            # Включённая ветка без списка сфер выдавала бы доступ всем подряд.
            raise DirectInviteError("включённая ветка требует списка сфер")
        return cls(
            enabled=enabled,
            active_sector_ids=active,
            sector_profiles=profiles,
            validity_days=validity_days,
            max_attempts=max_attempts,
            source_path=str(normalized),
        )

    def resolve_route_sector_id(self, sector_id: str) -> str:
        """Внутреннее имя сферы. Принимаем и наше, и имя стороны StartBot."""
        normalized = str(sector_id or "").strip()
        if normalized in self.sector_profiles:
            return normalized
        matches = [
            route_id
            for route_id, profile in self.sector_profiles.items()
            if profile.sector_id == normalized
        ]
        return matches[0] if len(matches) == 1 else normalized

    def route_for(self, sector_id: str) -> SectorProfile:
        route_id = self.resolve_route_sector_id(sector_id)
        profile = self.sector_profiles.get(route_id)
        if profile is None:
            raise BranchInactive("у сферы нет профиля автовыдачи")
        if not self.enabled or route_id not in self.active_sector_ids:
            raise BranchInactive("автовыдача для этой сферы не включена")
        return profile

    def active_sector_catalog(self) -> list[dict[str, str]]:
        """Точный список, который видит движок. Пустой — значит выдачи нет."""
        return [
            {
                "outreach_sector_id": profile.outreach_sector_id,
                "sector_id": profile.sector_id,
                "sector_name": profile.sector_name,
            }
            for route_id in sorted(self.active_sector_ids)
            if (profile := self.sector_profiles.get(route_id)) is not None
        ]

    def context_for_sector(self, sector_id: str) -> dict[str, str] | None:
        """Маршрут для ровно одной включённой сферы. Иначе None."""
        try:
            profile = self.route_for(sector_id)
        except BranchInactive:
            return None
        return {
            "branch": "automatic",
            "outreach_sector_id": profile.outreach_sector_id,
            "sector_id": profile.sector_id,
            "sector_name": profile.sector_name,
            "test_group_profile_id": profile.test_group_profile_id,
        }

    def public_status(self) -> dict[str, object]:
        return {
            "включена": self.enabled,
            "сферы с автовыдачей": sorted(self.active_sector_ids),
            "все описанные сферы": sorted(self.sector_profiles),
            "ссылка живёт, дней": self.validity_days,
            "попыток выпуска": self.max_attempts,
            "конфиг": self.source_path or "—",
        }


# ---------------------------------------------------------------------------
# клиент StartBot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartBotConfig:
    api_base_url: str
    service_token: str
    timeout_seconds: int = 20

    @classmethod
    def from_env(cls) -> "StartBotConfig":
        return cls(
            api_base_url=os.getenv(API_BASE_URL_ENV, "").strip(),
            service_token=os.getenv(API_TOKEN_ENV, "").strip(),
            timeout_seconds=int(os.getenv(API_TIMEOUT_ENV, "20") or 20),
        )

    def validate(self) -> None:
        # Требование прежнего контура и оно же здравый смысл: сервис живёт за
        # ssh-туннелем на loopback, наружу этот токен ходить не должен.
        if not self.api_base_url.startswith(("http://127.0.0.1:", "https://")):
            raise DirectInviteError(
                "StartBot API допустим только по loopback HTTP или HTTPS"
            )
        if len(self.service_token) < 32:
            raise DirectInviteError("токен StartBot отсутствует или слишком короткий")
        if self.timeout_seconds <= 0:
            raise DirectInviteError("таймаут StartBot должен быть положительным")


@dataclass(frozen=True)
class CreatedInvite:
    invite_id: str
    deep_link: str
    expires_at: str
    replayed: bool
    ready_message: str


def _request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any | None = None,
    timeout: int = 20,
) -> dict[str, Any]:
    body = None
    request_headers = dict(headers or {})
    if json_body is not None:
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url, data=body, headers=request_headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise DirectInviteError(f"HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise DirectInviteError(f"сеть недоступна: {exc}") from exc
    if not payload:
        return {}
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise DirectInviteError(f"невалидный JSON: {payload[:300]}") from exc


class StartBotClient:
    """Транспорт и только транспорт. Кому выдавать — решает ветка выше."""

    def __init__(self, config: StartBotConfig) -> None:
        config.validate()
        self.config = config

    def create_direct_invite(
        self,
        *,
        request_id: str,
        source_channel: str,
        source_conversation_id: str,
        consent_recorded_at: datetime,
        profile: SectorProfile,
        display_name: str | None = None,
        validity_days: int = 7,
        at: datetime | None = None,
        technical_test: bool = False,
    ) -> CreatedInvite:
        if not str(request_id).strip():
            raise DirectInviteError("request_id обязателен: он и есть идемпотентность")
        if source_channel not in ("channel_dm", "private_dm", "public_chat"):
            raise DirectInviteError(f"неизвестный канал {source_channel}")
        if not 1 <= int(validity_days) <= 30:
            raise DirectInviteError("validity_days вне 1..30")
        current = at or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise DirectInviteError("время должно быть с часовым поясом")

        # invite_id детерминирован от request_id: повтор после обрыва вернёт ту
        # же ссылку, а не выпустит человеку вторую.
        digest = hashlib.sha256(str(request_id).encode("utf-8")).hexdigest()[:28]
        invite_id = f"fti_outreach_{digest}"
        payload = {
            "invite_id": invite_id,
            "source_system": "tg_radar_outreach",
            # Сторона StartBot ведёт учёт выданных доступов, и проверочный
            # выпуск не должен попадать в него как настоящий. Флаг метит
            # заявку технической — так же, как это делал прежний контур.
            "source_entity_type": (
                "technical_integration_test" if technical_test
                else "direct_free_test_invite"
            ),
            "source_entity_id": f"direct-free-test:{request_id}"[:128],
            "source_conversation_id": str(source_conversation_id)[:128],
            "source_channel": source_channel,
            "consent_recorded_at": consent_recorded_at.isoformat(),
            "sector_id": profile.sector_id,
            "sector_name": profile.sector_name,
            "display_name": display_name,
            "assigned_manager_id": None,
            "test_group_profile_id": profile.test_group_profile_id,
            "expires_at": (
                current + timedelta(days=int(validity_days))
            ).isoformat(),
            "metadata": {
                "signal_type": "codex_smoke" if technical_test
                else "direct_free_test"
            },
        }
        response = _request_json(
            "POST",
            f"{self.config.api_base_url.rstrip('/')}/v1/invites",
            headers={"Authorization": f"Bearer {self.config.service_token}"},
            json_body=payload,
            timeout=self.config.timeout_seconds,
        )
        deep_link = str(response.get("deep_link") or "")
        if not deep_link.startswith("https://t.me/") or "?start=" not in deep_link:
            raise DirectInviteError("StartBot вернул недопустимую ссылку")
        return CreatedInvite(
            invite_id=str(response.get("invite_id") or invite_id),
            deep_link=deep_link,
            expires_at=str(response.get("expires_at") or payload["expires_at"]),
            replayed=bool(response.get("replayed", False)),
            ready_message=render_invite_message(profile.sector_name, deep_link),
        )


def render_invite_message(sector_name: str, deep_link: str) -> str:
    """Текст письма со ссылкой. Перенесён дословно из прежнего контура.

    Дословно — потому что он выверен: предупреждает про одноразовость и про
    привязку к первому открывшему аккаунту, а без этого человек открывает
    ссылку не с того аккаунта и тест достаётся не ему.
    """
    normalized = str(sector_name or "").strip()
    if not normalized:
        raise DirectInviteError("нужно название сферы")
    if not str(deep_link or "").startswith("https://t.me/") or "?start=" not in deep_link:
        raise DirectInviteError("нужна корректная ссылка StartBot")
    return (
        f"Отлично! Для вас открыт бесплатный тест ТГ РАДАР по направлению "
        f"«{normalized}».\n\n"
        f"Запустить бесплатный тест: {deep_link}\n\n"
        "Ссылка одноразовая и закрепится за первым Telegram-аккаунтом, который "
        "откроет бота. Если Telegram предложит выбрать аккаунт, выберите тот, "
        "на котором хотите проходить тест.\n\n"
        "Внутри бота вы сможете выбрать удобное время начала и получить доступ "
        "к живой тестовой группе. Если понадобится помощь, там же можно задать "
        "вопрос менеджеру или запросить видеосозвон.\n\n"
        "А здесь можете продолжать задавать вопросы — я тоже постараюсь помочь "
        "по продукту и формату теста."
    )


# ---------------------------------------------------------------------------
# согласие
# ---------------------------------------------------------------------------


def source_channel_for_role(role: str) -> str:
    return ROLE_TO_CHANNEL.get(str(role or ""), "")


def request_id_for(thread_id: str, inbound_id: str) -> str:
    """Идентификатор заявки. Один и тот же ход даёт один и тот же id.

    Идемпотентность держится именно здесь: повторный разбор того же входящего
    не заведёт вторую заявку и не выпустит человеку вторую ссылку.
    """
    digest = hashlib.sha256(
        f"{thread_id}|{inbound_id}".encode("utf-8")
    ).hexdigest()[:24]
    return f"dfi_{digest}"


def sector_from_decision(decision: Mapping[str, Any]) -> str:
    """Сфера, которую движок сам сопоставил с разрешённым списком.

    Берём только явное поле. Догадываться по свободному тексту нельзя: цена
    ошибки — выданный доступ не тому, и отозвать его молча уже не выйдет.
    """
    return str(decision.get("matched_direct_invite_sector_id") or "").strip()


def consent_from_decision(decision: Mapping[str, Any]) -> bool:
    """Согласие ли это на бесплатный тест."""
    kind = str(decision.get("handoff_kind") or "").strip().lower()
    return kind == CONSENT_HANDOFF_KIND


def record_consent(
    store: Store,
    *,
    config: BranchConfig,
    thread: Mapping[str, Any],
    inbound: Mapping[str, Any],
    account_role: str,
    sector_id: str,
    consent_source: str = "presales_v2",
    at: str | None = None,
) -> dict[str, Any] | None:
    """Записать согласие. None — автоматическая ветка не подходит.

    Ничего не отправляет и в сеть не ходит: только фиксирует, что человек
    согласился и его сфера разрешена. Выпуск идёт отдельным проходом.
    """
    if not config.enabled:
        return None
    profile: SectorProfile
    try:
        profile = config.route_for(sector_id)
    except BranchInactive:
        return None

    channel = source_channel_for_role(account_role)
    if channel not in ("channel_dm", "private_dm", "public_chat"):
        return None

    request_id = request_id_for(str(thread["id"]), str(inbound["id"]))
    existing = store.one(
        "SELECT * FROM direct_invites WHERE request_id = ?", (request_id,)
    )
    if existing is not None:
        return dict(existing)

    # Второй раз одному собеседнику ссылку не выдаём. Доступ уже открыт, и
    # вторая ссылка не помогает, а перебивает первую.
    already = store.one(
        "SELECT id FROM direct_invites WHERE contact_id = ? "
        "AND status IN (?, ?, ?)",
        (thread["contact_id"], STATUS_AGREED, STATUS_CREATED, STATUS_DELIVERED),
    )
    if already is not None:
        return None

    stamp = at or now()
    invite_id = new_id("dinv")
    store.execute(
        "INSERT INTO direct_invites(id, request_id, thread_id, contact_id, "
        "account_id, inbound_id, source_channel, outreach_sector_id, sector_id, "
        "sector_name, test_group_profile_id, consent_recorded_at, consent_source, "
        "status, attempt_count, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (
            invite_id, request_id, str(thread["id"]), str(thread["contact_id"]),
            int(inbound["account_id"]), str(inbound["id"]), channel,
            profile.outreach_sector_id, profile.sector_id, profile.sector_name,
            profile.test_group_profile_id, stamp, consent_source,
            STATUS_AGREED, stamp, stamp,
        ),
    )
    store.log("autoreply", "invite.consent", request_id,
              f"сфера={profile.sector_name} канал={channel}")
    return dict(store.one("SELECT * FROM direct_invites WHERE id = ?", (invite_id,)))


# ---------------------------------------------------------------------------
# выпуск и доставка
# ---------------------------------------------------------------------------


def ensure_invite_campaign(store: Store) -> str:
    """Служебная кампания доставки. Заводится напрямую, как и остальные.

    `add_campaign` требует шаблон для всякого действия с текстом — правило для
    рассылки, где текст один на сегмент. Здесь текст свой у каждого письма:
    в нём персональная ссылка.
    """
    row = store.one("SELECT id FROM campaigns WHERE id = ?", (INVITE_CAMPAIGN_ID,))
    if row is not None:
        return INVITE_CAMPAIGN_ID
    store.execute(
        "INSERT INTO campaigns(id, name, action, template_id, segment, mode, "
        "status, daily_cap, per_account_daily_cap, params, "
        "allow_repeat_contacts, roles, accounts, ttl_hours, note, created_at, "
        "updated_at) VALUES(?,?,?,NULL,'','immediate','active',999,99,'{}',1,"
        "'[]','[]',72,?,?,?)",
        (INVITE_CAMPAIGN_ID, INVITE_CAMPAIGN_NAME, "reply_private_dm",
         "служебная: автовыдача бесплатного теста", now(), now()),
    )
    return INVITE_CAMPAIGN_ID


def pending_requests(store: Store, *, limit: int = 10,
                     at: str | None = None) -> list[dict]:
    """Согласия, которым пора выпускать ссылку."""
    stamp = at or now()
    rows = store.query(
        "SELECT * FROM direct_invites WHERE status = ? "
        "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
        "ORDER BY created_at, id LIMIT ?",
        (STATUS_AGREED, stamp, int(limit)),
    )
    return [dict(row) for row in rows]


def process_requests(
    store: Store,
    settings,
    *,
    config: BranchConfig | None = None,
    client: StartBotClient | None = None,
    limit: int = 10,
    actor: str = "invites",
) -> dict[str, Any]:
    """Выпустить ссылки по накопившимся согласиям и поставить их в очередь.

    Отдельным проходом, а не из разбора входящих: обращение к StartBot идёт
    через туннель и занимает секунды, а разбор на это время встал бы.
    """
    branch = config if config is not None else BranchConfig.from_env()
    if not branch.enabled:
        return {"состояние": "выключено", "выпущено": 0, "ошибок": 0,
                "разобрано": 0}

    rows = pending_requests(store, limit=limit)
    if not rows:
        return {"состояние": "пусто", "выпущено": 0, "ошибок": 0, "разобрано": 0}

    if client is None:
        try:
            client = StartBotClient(StartBotConfig.from_env())
        except DirectInviteError as exc:
            # Нет реквизитов или сервис недоступен — это не повод терять
            # согласия. Они лежат и ждут; человек тем временем не остаётся без
            # ответа, потому что ответ движка ушёл своим путём.
            store.log(actor, "invite.client_unavailable", "", str(exc)[:200])
            store.commit()
            return {"состояние": "сервис недоступен", "выпущено": 0,
                    "ошибок": 0, "разобрано": 0, "почему": str(exc)[:200]}

    created = failed = 0
    for row in rows:
        request_id = str(row["request_id"])
        try:
            profile = branch.route_for(str(row["outreach_sector_id"]))
            if (profile.sector_name != str(row["sector_name"])
                    or profile.test_group_profile_id
                    != str(row["test_group_profile_id"])):
                # Конфиг переписали между согласием и выпуском. Выдавать доступ
                # по разъехавшемуся профилю нельзя — это другой тест.
                raise DirectInviteError("профиль сферы разъехался с конфигом")

            contact = store.one(
                "SELECT username, display_name FROM contacts WHERE id = ?",
                (row["contact_id"],),
            )
            display_name = None
            if contact is not None:
                display_name = (contact["display_name"] or contact["username"]
                                or None)

            invite = client.create_direct_invite(
                request_id=request_id,
                source_channel=str(row["source_channel"]),
                source_conversation_id=str(row["thread_id"]),
                consent_recorded_at=_parse(str(row["consent_recorded_at"])),
                profile=profile,
                display_name=str(display_name) if display_name else None,
                validity_days=branch.validity_days,
            )
        except DirectInviteError as exc:
            failed += 1
            attempts = int(row["attempt_count"] or 0) + 1
            exhausted = attempts >= branch.max_attempts
            store.execute(
                "UPDATE direct_invites SET attempt_count = ?, next_attempt_at = ?, "
                "last_error = ?, status = ?, updated_at = ? WHERE id = ?",
                (
                    attempts,
                    None if exhausted else _later(RETRY_BACKOFF_MINUTES * attempts),
                    str(exc)[:300],
                    STATUS_CREATE_FAILED if exhausted else STATUS_AGREED,
                    now(),
                    row["id"],
                ),
            )
            store.log(actor, "invite.create_failed", request_id,
                      f"попытка {attempts}: {str(exc)[:180]}")
            if exhausted:
                # Карточку заводим только здесь, на исчерпании: пока попытки
                # остались, звать человека рано — ссылка ещё может уйти сама.
                fallback_to_manager(store, row, str(exc), actor=actor)
            store.commit()
            continue

        # Ссылка на руках. Ставим доставку обычной задачей — дальше её выпустит
        # диспетчер по своим правилам темпа, окна и боевого режима.
        try:
            queued = replies.queue_reply(
                store,
                text=invite.ready_message,
                thread_id=str(row["thread_id"]),
                campaign_id=ensure_invite_campaign(store),
                actor=actor,
            )
            task_id = str(queued["task"])
        except replies.ReplyError as exc:
            failed += 1
            store.execute(
                "UPDATE direct_invites SET invite_id = ?, invite_expires_at = ?, "
                "last_error = ?, updated_at = ? WHERE id = ?",
                (invite.invite_id, invite.expires_at,
                 f"ссылка выпущена, доставка не поставлена: {str(exc)[:220]}",
                 now(), row["id"]),
            )
            store.log(actor, "invite.queue_failed", request_id, str(exc)[:200])
            store.commit()
            continue

        store.execute(
            "UPDATE direct_invites SET invite_id = ?, invite_expires_at = ?, "
            "task_id = ?, status = ?, attempt_count = attempt_count + 1, "
            "next_attempt_at = NULL, last_error = NULL, updated_at = ? "
            "WHERE id = ?",
            (invite.invite_id, invite.expires_at, task_id, STATUS_CREATED,
             now(), row["id"]),
        )
        store.log(actor, "invite.created", request_id,
                  f"задача={task_id} повтор={invite.replayed}")
        store.commit()
        created += 1

    return {"состояние": "работа", "разобрано": len(rows), "выпущено": created,
            "ошибок": failed}


def fallback_to_manager(store: Store, row: Mapping[str, Any], why: str,
                        *, actor: str = "invites") -> str:
    """Исчерпали попытки — зовём человека.

    Без этого автоматика была бы хуже ручного пути, а не лучше: записав
    согласие, она перестаёт заводить карточку, и если ссылку выпустить не
    удалось, собеседник остаётся и без ссылки, и без менеджера. Тихо, потому что
    формально «всё по плану».

    Карточка — тот же механизм, что и у обычного handoff: одна активная на
    диалог, дальше её разбирает человек.
    """
    existing = store.one(
        "SELECT id FROM handoffs WHERE thread_id = ? AND status IN ('new','taken')",
        (row["thread_id"],),
    )
    if existing is not None:
        return str(existing["id"])
    handoff_id = new_id("handoff")
    store.execute(
        "INSERT INTO handoffs(id, thread_id, reason, status, note, "
        "created_at, updated_at) VALUES(?,?,?,'new',?,?,?)",
        (handoff_id, row["thread_id"], "free_test_access_failed",
         f"автовыдача не удалась: {why}"[:300], now(), now()),
    )
    store.execute(
        "UPDATE threads SET state = 'handoff', updated_at = ? WHERE id = ?",
        (now(), row["thread_id"]),
    )
    store.log(actor, "invite.fallback_manager", str(row["request_id"]),
              why[:200])
    return handoff_id


def reconcile_deliveries(store: Store, *, actor: str = "invites") -> dict[str, int]:
    """Отметить доставленным то, что диспетчер уже отправил.

    Своего состояния у доставки нет: она обычная задача, и правду о ней знает
    очередь. Здесь мы только переносим её в заявку, чтобы отчёт читался без
    джойнов и чтобы повторная выдача видела уже доставленное.
    """
    delivered = 0
    rows = store.query(
        "SELECT d.id, d.request_id, t.state, t.finished_at "
        "FROM direct_invites d JOIN tasks t ON t.id = d.task_id "
        "WHERE d.status = ?",
        (STATUS_CREATED,),
    )
    for row in rows:
        if str(row["state"]) != "done":
            continue
        store.execute(
            "UPDATE direct_invites SET status = ?, link_delivered_at = ?, "
            "updated_at = ? WHERE id = ?",
            (STATUS_DELIVERED, row["finished_at"] or now(), now(), row["id"]),
        )
        store.log(actor, "invite.delivered", str(row["request_id"]), "")
        delivered += 1
    if delivered:
        store.commit()
    return {"доставлено": delivered}


def status_rows(store: Store, *, limit: int = 50) -> list[dict]:
    rows = store.query(
        "SELECT d.*, c.username FROM direct_invites d "
        "LEFT JOIN contacts c ON c.id = d.contact_id "
        "ORDER BY d.created_at DESC LIMIT ?",
        (int(limit),),
    )
    return [dict(row) for row in rows]


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _later(minutes: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(minutes=int(minutes))
    ).isoformat(timespec="seconds")
