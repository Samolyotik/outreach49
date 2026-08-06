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
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import replies
from .store import Store, new_id, now

#: Где лежит описание разрешённых сфер. Имя переменной то же, что в прежнем
#: контуре, — файл переиспользуется как есть, без переписывания.
BRANCH_CONFIG_ENV = "OUTREACH_STARTBOT_DIRECT_INVITE_CONFIG"

#: Где лежит канонический словарь сфер. Отдельный файл, а не новые ключи в
#: конфиге выше, по одной причине: тот файл общий с прежним контуром, а
#: `from_env` глотает любую ошибку чтения и тихо возвращает «выключено». То
#: есть неудачная правка общего файла даёт не отказ, а молчаливую смерть
#: автовыдачи без следа в журнале. Здесь же худший случай — пустой словарь.
SECTOR_CATALOG_ENV = "OUTREACH_SECTOR_CATALOG"

#: Статусы строки словаря. На маршрут сейчас влияет только `ready`: сфера с
#: готовой тестовой группой получает прямой доступ, все остальные — демо-бота.
#: Различие `manual` и `out_of_scope` словарь помнит, но ни текст, ни маршрут
#: оно пока не меняет: человек сам запрашивает менеджера уже внутри демо-бота.
SECTOR_STATUS_READY = "ready"
SECTOR_STATUS_MANUAL = "manual"
SECTOR_STATUS_OUT_OF_SCOPE = "out_of_scope"
SECTOR_STATUSES = (
    SECTOR_STATUS_READY,
    SECTOR_STATUS_MANUAL,
    SECTOR_STATUS_OUT_OF_SCOPE,
)

#: Сферы нет в словаре — или человек её вовсе не назвал.
#:
#: Это НЕ статус строки словаря, и в `SECTOR_STATUSES` его быть не должно:
#: строка с таким статусом не прочиталась бы. Маркер живёт только в колонке
#: `demo_invites.sector_status`, чтобы в отчётах было видно разницу между
#: «сфера известна, готовой группы нет» и «сферу не опознали вовсе».
#:
#: Пустая строка вместо маркера тоже прошла бы: `NOT NULL` в SQLite её
#: допускает, а `CHECK` на этой колонке нет. Но пустота в отчёте неотличима от
#: потерянного значения, а разница здесь как раз и есть предмет наблюдения.
SECTOR_STATUS_UNKNOWN = "unknown"
API_BASE_URL_ENV = "OUTREACH_STARTBOT_API_BASE_URL"
API_TOKEN_ENV = "OUTREACH_STARTBOT_API_SERVICE_TOKEN"
API_TIMEOUT_ENV = "OUTREACH_STARTBOT_TIMEOUT_SECONDS"

#: Служебная кампания доставки ссылок. Отдельная от автоответов: у неё другой
#: смысл и другой разбор в отчётах, а лимиты живут на кампании.
INVITE_CAMPAIGN_ID = "direct_invites"
INVITE_CAMPAIGN_NAME = "Автовыдача бесплатного теста"

#: Служебная кампания демо-маршрута. Отдельная от выдачи ссылок StartBot:
#: у неё другой смысл в отчётах, а лимиты живут на кампании.
DEMO_CAMPAIGN_ID = "demo_invites"
DEMO_CAMPAIGN_NAME = "Демо-бот для сфер без готового теста"

#: Состояния демо-заявки. Своих сетевых вызовов у неё нет, поэтому и состояний
#: втрое меньше, чем у выдачи StartBot: письмо ставится в очередь сразу.
DEMO_STATUS_QUEUED = "queued"
DEMO_STATUS_DELIVERED = "delivered"
DEMO_STATUS_CANCELLED = "cancelled"

#: Тот самый вердикт движка, ради которого всё и затевалось.
CONSENT_HANDOFF_KIND = "free_test_access"

#: Человек просит живого: менеджера, созвон, счёт, договор, свои условия.
#: Единственный вид handoff, который движок вправе адресовать человеку.
MANAGER_HANDOFF_KIND = "manager_action"

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

#: Поверхности, на которых роли разрешено отвечать. Это не то же самое, что
#: `ROLE_TO_CHANNEL`: там у роли ровно один канал — тот, с которого она пишет
#: первой. Отвечают же роли шире, и правило взято из политики дословно:
#: `channel_sender` отвечает в личке канала или в личке, которую человек начал
#: сам; `chat_sender` отвечает ТОЛЬКО в такой личке и никогда публично.
#:
#: Разница стоила выдачи целиком. Канал согласия выводился из роли, и у
#: `channel_sender`, которому ответили в личку, он не совпадал с поверхностью
#: разговора — выдача молча отказывала. У `chat_sender` не совпадал никогда:
#: единственная его законная поверхность ответа — личка, а канал по таблице —
#: `public_chat`. За 01–06.08 так осталось без единой ссылки 80 человек из 104,
#: а демо-писем не выдано вообще ни одного.
ROLE_SURFACES = {
    "channel_sender": ("channel_dm", "private_dm"),
    "dm_sender": ("private_dm",),
    "chat_sender": ("private_dm", "public_chat"),
}


def reply_channel(role: str, surface: str) -> str:
    """Канал этого разговора. Пустая строка — роли тут отвечать нельзя.

    Канал берётся из поверхности входящего, а не выводится из роли: где идёт
    разговор — наблюдаемый факт, и придумывать его по роли аккаунта незачем.
    Роль отвечает на другой вопрос — вправе ли она тут говорить.

    Метка уезжает в учёт StartBot, поэтому она обязана быть правдой. Значение
    `private_dm` там уже встречается: три выдачи из десяти прошли именно с ним.
    """
    role = str(role or "")
    surface = str(surface or "").strip()
    if not surface:
        # Поверхности нет — остаётся канал самой роли. В бою она есть всегда,
        # это колонка `inbound.surface`; пустой приходит только там, где
        # входящее собрано руками.
        return ROLE_TO_CHANNEL.get(role, "")
    return surface if surface in ROLE_SURFACES.get(role, ()) else ""

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
class SectorRow:
    """Строка канонического словаря сфер.

    Маршрута она не содержит: `sector_id` и `test_group_profile_id` живут
    только в конфиге StartBot, где их и валидируют. Дублировать их здесь
    значило бы завести второй источник правды о том, в какую тестовую группу
    ведёт сфера, — а расхождение имён видно только по пяти неудачным выпускам.
    """

    canonical_sector_id: str
    sector_name: str
    status: str
    note: str = ""
    description: str = ""
    subsectors: tuple[str, ...] = ()
    synonyms: tuple[str, ...] = ()
    self_names: tuple[str, ...] = ()
    service_markers: tuple[str, ...] = ()
    boundaries: tuple[tuple[str, str], ...] = ()

    def for_prompt(self) -> dict[str, Any]:
        """Проекция для модели: без заметок и служебных полей."""
        payload: dict[str, Any] = {
            "canonical_sector_id": self.canonical_sector_id,
            "sector_name": self.sector_name,
            "free_test_group_ready": self.status == SECTOR_STATUS_READY,
        }
        if self.description:
            payload["description"] = self.description
        for key, values in (
            ("subsectors", self.subsectors),
            ("synonyms", self.synonyms),
            ("self_names", self.self_names),
            ("service_markers", self.service_markers),
        ):
            if values:
                payload[key] = list(values)
        if self.boundaries:
            payload["boundaries"] = [
                {"vs": vs, "rule": rule} for vs, rule in self.boundaries
            ]
        return payload


def _text_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(
        str(item).strip() for item in value if str(item or "").strip()
    )


def load_sector_catalog(
    path: str | Path,
) -> tuple[dict[str, SectorRow], str]:
    """Прочитать словарь сфер. Возвращает строки и ссылку на демо-бота.

    Бросает только на явно испорченном файле; вызывающий обязан это поймать и
    продолжить без словаря, потому что автовыдача шести сфер от него не
    зависит и падать вместе с ним не должна.
    """
    normalized = Path(path).expanduser().resolve()
    raw = json.loads(normalized.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise DirectInviteError("словарь сфер должен быть JSON-объектом")
    if int(raw.get("schema_version") or 0) != 2:
        raise DirectInviteError("неподдерживаемая schema_version словаря сфер")

    demo_link = str(raw.get("demo_bot_link") or "").strip()
    if demo_link and not demo_link.startswith("https://t.me/"):
        raise DirectInviteError("ссылка на демо-бота должна вести на t.me")

    rows: dict[str, SectorRow] = {}
    for item in raw.get("sectors") or []:
        if not isinstance(item, dict):
            raise DirectInviteError("строка словаря должна быть объектом")
        canonical = str(item.get("canonical_sector_id") or "").strip()
        name = str(item.get("sector_name") or "").strip()
        status = str(item.get("status") or "").strip()
        if not canonical or not name:
            raise DirectInviteError(f"неполная строка словаря: {canonical or '?'}")
        if status not in SECTOR_STATUSES:
            raise DirectInviteError(
                f"у сферы {canonical} неизвестный статус {status!r}"
            )
        if canonical in rows:
            raise DirectInviteError(f"сфера {canonical} описана дважды")
        boundaries: list[tuple[str, str]] = []
        for edge in item.get("boundaries") or []:
            if not isinstance(edge, dict):
                raise DirectInviteError(f"разграничитель сферы {canonical} кривой")
            vs = str(edge.get("vs") or "").strip()
            rule = str(edge.get("rule") or "").strip()
            if vs and rule:
                boundaries.append((vs, rule))
        rows[canonical] = SectorRow(
            canonical_sector_id=canonical,
            sector_name=name,
            status=status,
            note=str(item.get("note") or "").strip(),
            description=str(item.get("description") or "").strip(),
            subsectors=_text_tuple(item.get("subsectors")),
            synonyms=_text_tuple(item.get("synonyms")),
            self_names=_text_tuple(item.get("self_names")),
            service_markers=_text_tuple(item.get("service_markers")),
            boundaries=tuple(boundaries),
        )
    return rows, demo_link


@dataclass(frozen=True)
class BranchConfig:
    """Точный список сфер, которым доступ выдаётся автоматически."""

    enabled: bool
    active_sector_ids: frozenset[str]
    sector_profiles: dict[str, SectorProfile]
    validity_days: int = 7
    max_attempts: int = 5
    source_path: str = ""
    # Словарь сфер приезжает отдельным файлом и только добавляет: без него
    # всё поведение ровно сегодняшнее. Поля обязаны идти последними и с
    # дефолтами — `disabled()` и тесты конструируют этот класс позиционно.
    sector_rows: Mapping[str, SectorRow] = field(default_factory=dict)
    demo_bot_link: str = ""
    catalog_path: str = ""
    catalog_warnings: tuple[str, ...] = ()

    @classmethod
    def disabled(cls, source_path: str = "") -> "BranchConfig":
        return cls(False, frozenset(), {}, source_path=source_path)

    @classmethod
    def from_env(cls) -> "BranchConfig":
        path = os.getenv(BRANCH_CONFIG_ENV, "").strip()
        if not path:
            base = cls.disabled()
        else:
            try:
                base = cls.from_path(path)
            except (OSError, ValueError, DirectInviteError):
                # Кривой или недоступный конфиг — это «выключено», а не падение
                # разбора входящих. Разговор пойдёт ручным путём.
                base = cls.disabled(path)
        return base.with_sector_catalog(os.getenv(SECTOR_CATALOG_ENV, "").strip())

    def with_sector_catalog(self, path: str | Path | None) -> "BranchConfig":
        """Присоединить словарь сфер. Ошибка словаря ветку не выключает.

        Здесь принципиально другое поведение, чем у конфига маршрутов выше.
        Тот конфиг решает, кому вообще выдавать доступ, и при сомнении обязан
        молчать. Словарь же только описывает сферы: если он не прочитался,
        правильный ответ — работать как вчера и сказать об этом в статусе, а
        не отнимать доступ у шести боевых сфер.
        """
        normalized = str(path or "").strip()
        if not normalized:
            return self
        try:
            rows, demo_link = load_sector_catalog(normalized)
        except (OSError, ValueError, DirectInviteError) as exc:
            return replace(
                self, catalog_path=normalized, catalog_warnings=(str(exc),)
            )
        return replace(
            self,
            sector_rows=rows,
            demo_bot_link=demo_link,
            catalog_path=normalized,
            catalog_warnings=tuple(
                self._catalog_disagreements(rows)
            ),
        )

    def _catalog_disagreements(
        self, rows: Mapping[str, SectorRow]
    ) -> list[str]:
        """Расхождения словаря и боевого конфига — заметка, а не запрет.

        Единственный источник правды о том, какой сфере положен прямой
        доступ, — `active_sector_ids`. Словарь его не сужает: иначе модель
        по-прежнему считала бы сферу автоматической и обещала ссылку, которую
        выпуск затем молча не выдаст.
        """
        notes: list[str] = []
        ready = {
            key for key, row in rows.items()
            if row.status == SECTOR_STATUS_READY
        }
        for missing in sorted(self.active_sector_ids - set(rows)):
            notes.append(f"сфера {missing} выдаётся, но в словаре её нет")
        for stale in sorted((set(rows) & self.active_sector_ids) - ready):
            notes.append(
                f"сфера {stale} выдаётся, а в словаре помечена как "
                f"{rows[stale].status}"
            )
        for orphan in sorted(ready - self.active_sector_ids):
            notes.append(
                f"сфера {orphan} помечена готовой, но доступ по ней не выдаётся"
            )
        return notes

    def sector_status(self, canonical_sector_id: str) -> str:
        row = self.sector_rows.get(str(canonical_sector_id or "").strip())
        return row.status if row is not None else ""

    def sector_row(self, canonical_sector_id: str) -> SectorRow | None:
        return self.sector_rows.get(str(canonical_sector_id or "").strip())

    def matching_catalog(self) -> list[dict[str, Any]]:
        """Словарь для сопоставления в промпте. Не allowlist выдачи.

        Отдельно от `active_sector_catalog`: тот одновременно и текст промпта,
        и список разрешённых id, и источник enum обёртки. Обогащать его
        значило бы менять поведение шести боевых сфер.
        """
        return [
            row.for_prompt()
            for row in sorted(
                self.sector_rows.values(),
                key=lambda item: (
                    item.status != SECTOR_STATUS_READY,
                    item.canonical_sector_id,
                ),
            )
        ]

    def demo_route_ready(self) -> bool:
        """Можно ли вести человека в демо-бота.

        `enabled` здесь обязателен, и это не формальность. Мастер-гейт — это
        единственная ручка «автоматика ничего не делает сама»; без него флаг
        выключал только выдачу StartBot, а демо продолжало уходить. Хуже того,
        `from_env` вешает словарь сфер даже на конфиг, который не прочитался, —
        то есть неудачная правка общего файла не останавливала маршрут, а
        молча оставляла его работать. И CLI при этом печатал «ветка выключена
        — ссылки не выдаются», что переставало быть правдой.
        """
        return (self.enabled
                and bool(self.demo_bot_link)
                and bool(self.sector_rows))

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
            "словарь сфер": self.catalog_path or "—",
            "строк в словаре": len(self.sector_rows),
            "из них с готовым тестом": sum(
                1 for row in self.sector_rows.values()
                if row.status == SECTOR_STATUS_READY
            ),
            "демо-бот": self.demo_bot_link or "—",
            "расхождения словаря": list(self.catalog_warnings) or ["нет"],
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


#: Варианты каждого абзаца письма со ссылкой.
#:
#: Текст прежнего контура был один на всех, и это видно: семь писем, ушедших
#: 04.08 с шести разных аккаунтов, совпали байт в байт, кроме самой ссылки.
#: Одинаковый текст из нескольких аккаунтов — это ровно тот признак, по
#: которому рассылку и отличают от переписки.
#:
#: Меняются формулировки, факты — нет. В каждом варианте третьего абзаца
#: обязаны остаться оба предупреждения: ссылка срабатывает один раз и
#: закрепляется за первым открывшим её аккаунтом. Без них человек открывает
#: ссылку не с того аккаунта, и тест достаётся не ему. За этим следит проверка
#: `_ONE_TIME_MARKERS`, прогоняемая по всем сочетаниям в тестах.
_OPENINGS = (
    "Отлично! Для вас открыт бесплатный тест ТГ РАДАР по направлению «{sector}».",
    "Готово: бесплатный тест ТГ РАДАР по направлению «{sector}» для вас открыт.",
    "Открыл вам бесплатный тест ТГ РАДАР по направлению «{sector}».",
    "Бесплатный тест ТГ РАДАР по направлению «{sector}» готов.",
)
_LINK_LINES = (
    "Запустить бесплатный тест: {link}",
    "Ссылка для запуска: {link}",
    "Вот ссылка на запуск: {link}",
    "Запуск здесь: {link}",
)
_ONE_TIME = (
    "Ссылка одноразовая и закрепится за первым Telegram-аккаунтом, который "
    "откроет бота. Если Telegram предложит выбрать аккаунт, выберите тот, на "
    "котором хотите проходить тест.",
    "Ссылка сработает один раз и привяжется к тому Telegram-аккаунту, который "
    "откроет бота первым. Если Telegram спросит, каким аккаунтом войти, "
    "выберите тот, где хотите проходить тест.",
    "Учтите: ссылка одноразовая, и доступ закрепится за первым аккаунтом, "
    "который по ней зайдёт. Если Telegram предложит выбор аккаунта, укажите "
    "тот, на котором будете проходить тест.",
    "Ссылка работает один раз и закрепляется за первым открывшим её "
    "Telegram-аккаунтом. Если появится выбор аккаунта, выберите тот, на "
    "котором планируете проходить тест.",
)
_INSIDE = (
    "Внутри бота вы сможете выбрать удобное время начала и получить доступ к "
    "живой тестовой группе. Если понадобится помощь, там же можно задать "
    "вопрос менеджеру или запросить видеосозвон.",
    "В боте выберете удобное время старта и получите доступ к живой тестовой "
    "группе. Там же можно спросить менеджера или попросить видеосозвон, если "
    "понадобится.",
    "В боте можно выбрать время начала и войти в живую тестовую группу. Если "
    "нужна помощь, оттуда же пишется менеджеру или запрашивается видеосозвон.",
)
_CLOSINGS = (
    "А здесь можете продолжать задавать вопросы: я тоже постараюсь помочь по "
    "продукту и формату теста.",
    "Здесь тоже можно продолжать спрашивать, помогу по продукту и по формату "
    "теста.",
    "Вопросы по продукту и формату теста можно и дальше задавать здесь, я на "
    "связи.",
)

#: Что обязано уцелеть в любом варианте третьего абзаца.
_ONE_TIME_MARKERS = (("одноразов", "один раз", "работает один"), ("перв",))


def render_invite_message(
    sector_name: str, deep_link: str, *, seed: str | None = None
) -> str:
    """Текст письма со ссылкой, свой у каждого получателя.

    Выбор вариантов детерминированный и по умолчанию завязан на саму ссылку:
    она уникальна для человека, а значит текст у каждого свой и при этом не
    меняется между повторными вызовами. Это важно: письмо могут собрать заново
    при повторе выпуска, и оно не должно после этого выглядеть иначе.
    """
    normalized = str(sector_name or "").strip()
    if not normalized:
        raise DirectInviteError("нужно название сферы")
    if not str(deep_link or "").startswith("https://t.me/") or "?start=" not in deep_link:
        raise DirectInviteError("нужна корректная ссылка StartBot")

    digest = hashlib.sha256(str(seed or deep_link).encode("utf-8")).digest()
    blocks = (
        _OPENINGS[digest[0] % len(_OPENINGS)].format(sector=normalized),
        _LINK_LINES[digest[1] % len(_LINK_LINES)].format(link=deep_link),
        _ONE_TIME[digest[2] % len(_ONE_TIME)],
        _INSIDE[digest[3] % len(_INSIDE)],
        _CLOSINGS[digest[4] % len(_CLOSINGS)],
    )
    return "\n\n".join(blocks)


#: Текст демо-маршрута. Один смысл на все сферы — и в этом весь смысл: письмо
#: ничего не обещает про конкретное направление, поэтому его не нужно
#: переписывать под каждую новую сферу и невозможно случайно соврать.
#:
#: Текст выдачи StartBot переиспользовать нельзя, хотя соблазн есть. Он
#: утверждает три вещи, ложные для этой ссылки: что тест открыт «по
#: направлению «{sector}»», что ссылка одноразовая и закрепится за первым
#: аккаунтом, и что внутри ждёт живая тестовая группа. Здесь ссылка общая и
#: постоянная, а группы под сферу человека может не быть вовсе.
_DEMO_OPENINGS = (
    "Отлично, тогда предлагаю посмотреть, как {brand} работает на практике, "
    "в нашем демо-боте.",
    "Тогда предлагаю посмотреть {brand} в деле: у нас есть демо-бот.",
    "Хорошо, тогда проще показать. У нас для этого есть демо-бот {brand}.",
    "Отлично. Показать, как это устроено, проще всего в нашем демо-боте "
    "{brand}.",
)
_DEMO_INSIDE = (
    "В нём сервис в реальном времени находит сообщения людей с потенциальным "
    "спросом в разных сферах.",
    "Там видно, как сервис прямо сейчас находит в открытых чатах сообщения "
    "людей с потенциальным спросом по разным направлениям.",
    "Внутри он в реальном времени собирает сообщения людей, у которых виден "
    "потенциальный спрос, по нескольким направлениям сразу.",
    "В нём в реальном времени видно сами сообщения с потенциальным спросом, "
    "которые сервис находит в открытых чатах по разным сферам.",
)
_DEMO_MANAGER = (
    "Там же можно задать вопросы менеджеру и запросить примеры сообщений "
    "именно для вашей сферы.",
    "Там же есть менеджер: ему можно задать вопросы и попросить примеры "
    "сообщений под вашу сферу.",
    "Если понадобятся примеры именно по вашему направлению, их можно "
    "запросить там же у менеджера.",
    "Вопросы и примеры под вашу сферу можно запросить прямо там, у менеджера.",
)
_DEMO_LINK_LINES = (
    "Посмотреть, как это работает: [{link_text}]({link})",
    "Вот он: [{link_text}]({link})",
    "Демо-бот здесь: [{link_text}]({link})",
    "Заглянуть можно тут: [{link_text}]({link})",
)

#: Слова, которых в этом письме быть не должно: они относятся к персональной
#: ссылке StartBot и здесь были бы обещанием, которого никто не выполнит.
_DEMO_FORBIDDEN = (
    "одноразов", "один раз", "закрепится", "персональн", "тестовая группа",
)


def render_demo_message(deep_link: str, *, seed: str) -> str:
    """Письмо со ссылкой на демо-бота.

    Сферу оно не называет намеренно. Демо-бот один на всех, и назвать в нём
    направление человека значило бы пообещать готовую группу под это
    направление — ровно то, чего у сфер без тестовой группы нет.

    Сид обязателен и должен быть привязан к получателю. У выдачи StartBot
    роль сида играет сама ссылка: она у каждого своя, поэтому и текст выходит
    свой. Здесь ссылка общая и постоянная, так что сид по ссылке дал бы всем
    один и тот же текст с шести аккаунтов — заметный шаблон. При этом
    повторный вызов с тем же сидом обязан давать тот же текст: письмо могут
    собрать заново при повторе, и человек не должен увидеть два разных.
    """
    link = str(deep_link or "").strip()
    if not link.startswith("https://t.me/"):
        raise DirectInviteError("нужна ссылка на демо-бота в t.me")
    # Видимый текст — @имя бота, а адрес прячется в разметке. Проверено живой
    # отправкой: Telethon по умолчанию разбирает markdown, и ссылка уезжает
    # как MessageEntityTextUrl.
    handle = link.split("?", 1)[0].rsplit("/", 1)[-1].strip()
    if not handle:
        raise DirectInviteError("в ссылке на демо-бота нет имени бота")
    marker = str(seed or "").strip()
    if not marker:
        raise DirectInviteError("нужен сид получателя для текста демо-письма")

    digest = hashlib.sha256(marker.encode("utf-8")).digest()
    text = "\n\n".join((
        " ".join((
            _DEMO_OPENINGS[digest[0] % len(_DEMO_OPENINGS)].format(
                brand="ТГ РАДАР"),
            _DEMO_INSIDE[digest[1] % len(_DEMO_INSIDE)],
        )),
        _DEMO_MANAGER[digest[2] % len(_DEMO_MANAGER)],
        _DEMO_LINK_LINES[digest[3] % len(_DEMO_LINK_LINES)].format(
            link_text=f"@{handle}", link=link),
    ))

    lowered = text.lower()
    for word in _DEMO_FORBIDDEN:
        if word in lowered:
            raise DirectInviteError(f"в письме демо-бота лишнее слово: {word}")
    if text.count(link) != 1:
        raise DirectInviteError("ссылка в письме демо-бота должна быть одна")
    if "—" in text:
        raise DirectInviteError("длинное тире в письме демо-бота запрещено")
    return text


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


def consent_left_unserved(
    config: BranchConfig,
    *,
    store: Store,
    thread: Mapping[str, Any],
    account_role: str,
    surface: str = "",
) -> str:
    """Почему согласие осталось без выдачи. Пусто — выдавать было и не нужно.

    Зовётся только когда человек согласился на тест, а автоматика не выдала ни
    ссылки, ни демо. Различие здесь дороже, чем кажется: «не смогли» и «уже
    незачем» выглядят одинаково — оба возвращают `None`, — но первое обязано
    дойти до менеджера, а второе не должно его беспокоить.

    Умолчание здесь — «не смогли», и это принципиально. Первая версия перечисляла
    известные причины отказа и в конце возвращала пустоту, то есть всякая
    непредусмотренная причина читалась как «человек обслужен». Причин же у
    `record_demo_invite` семь, а знала функция три: согласие по сфере с готовой
    группой, которой отказал `record_consent`, проваливалось в тишину — ни
    ссылки, ни демо, ни менеджера. До правки 06.08 такой ход заводил карточку.

    «Уже незачем» ровно одно: человеку есть что открыть. Персональная ссылка
    выдана раньше либо демо-письмо уже собрано и не отменено.
    """
    contact_id = str(thread.get("contact_id") or "").strip()
    if not contact_id:
        return "у собеседника не заведён контакт"
    if demo_invite_blocked_by_personal_link(store, contact_id):
        return ""
    existing = store.one(
        "SELECT status FROM demo_invites WHERE contact_id = ?", (contact_id,)
    )
    if existing is not None and str(existing["status"]) != DEMO_STATUS_CANCELLED:
        return ""
    if not config.enabled:
        return "автовыдача выключена мастер-флагом"
    if not config.demo_route_ready():
        return "демо-маршрут недоступен: нет словаря сфер или ссылки на бота"
    # Поверхность знает только вызывающий, у которого есть входящее. Без неё
    # остаётся спросить роль — приблизительно, зато честно: это диагностика,
    # и её дело назвать причину, а не выдавать доступ.
    канал = (reply_channel(account_role, surface) if surface
             else source_channel_for_role(account_role))
    if канал not in ("channel_dm", "private_dm", "public_chat"):
        return (f"роль {account_role or '—'} не отвечает на поверхности "
                f"{surface or '—'}")
    return "согласие есть, а выдать не удалось"


def specialization_conflict(
    config: BranchConfig,
    *,
    decision: Mapping[str, Any],
    inbound: Mapping[str, Any],
) -> str:
    """Причина, по которой ход обязан достаться человеку. Пусто — конфликта нет.

    Тот же детерминированный гейт, что отбивает выдачу внутри `record_consent`,
    но видимый вызывающему. Без него отказ гейта неотличим от «сфера не подошла»
    и уводит человека в общее демо — а гейт срабатывает ровно тогда, когда
    сфера-то как раз известна и у неё есть готовая группа, просто не та, что
    выбрала модель. Демо здесь было бы шагом назад, и выбирать между двумя
    группами должен человек.
    """
    try:
        profile = config.route_for(sector_from_decision(decision))
    except BranchInactive:
        return ""
    return contradicts_named_specialization(
        inbound.get("text"), profile.outreach_sector_id
    )


#: Уверенность сопоставления, при которой демо-маршрут вообще рассматривается.
SECTOR_CONFIDENCE_EXACT = "exact"


def canonical_sector_from_decision(decision: Mapping[str, Any]) -> str:
    """Опознанная сфера. Пусто — сферу не опознали, и это нормальный случай.

    Требуем точного сопоставления, но смысл требования не тот, что раньше.
    Сфера здесь решает ровно один вопрос: не положен ли человеку настоящий
    доступ вместо демо. На этот вопрос «скорее всего» не отвечает, поэтому
    `likely` и `ambiguous` читаются как «не знаем» — и человек идёт общим
    маршрутом в демо-бота, а не к менеджеру.

    Раньше пустота отсюда закрывала демо-маршрут целиком. Это было ошибкой:
    письмо демо-бота сферу не называет (`render_demo_message`), поэтому
    «ответить невпопад» им невозможно — оно одинаково для всех.
    """
    if str(decision.get("sector_confidence") or "").strip().lower() != (
        SECTOR_CONFIDENCE_EXACT
    ):
        return ""
    return str(decision.get("canonical_sector_id") or "").strip()


#: Явно названная специализация и общая сфера, в которую её нельзя свести.
#:
#: Банкротство и общие юридические услуги — две сферы с двумя разными тестовыми
#: группами. Слова «юрист», «юридическая помощь», «юридическая компания»
#: называют профессию исполнителя, а не специализацию, поэтому «юрист по
#: банкротству» — это банкротство. Модель об этом знает из заметок базы знаний;
#: здесь стоит второй, детерминированный слой на случай, когда она всё же
#: выберет общую сферу.
#:
#: Гейт односторонний и умеет только запрещать. Сам он сферу не выбирает: без
#: явных слов не срабатывает вовсе, а сработав — уводит разговор менеджеру, а
#: не подменяет выбор модели своим. Обратной проверки нет намеренно: сферу
#: могли подтвердить ходом раньше, и текущее сообщение о ней молчит.
_SPECIALIZATION_GUARDS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(
            r"банкрот"
            r"|спис\w*\s+(?:кредитн\w+\s+)?долг"
            r"|долг\w*\s+спис",
            re.IGNORECASE,
        ),
        "legal_services_business_private",
        "bankruptcy_debt_relief",
    ),
)


def contradicts_named_specialization(text: str, route_sector_id: str) -> str:
    """Причина отказа, если человек назвал специализацию, а сфера выбрана общая.

    Пустая строка — противоречия нет.
    """
    message = str(text or "")
    if not message.strip():
        return ""
    for pattern, general_id, special_id in _SPECIALIZATION_GUARDS:
        if route_sector_id == general_id and pattern.search(message):
            return f"названа специализация {special_id}, а сфера выбрана {general_id}"
    return ""


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

    contradiction = contradicts_named_specialization(
        inbound.get("text"), profile.outreach_sector_id
    )
    if contradiction:
        store.log("autoreply", "invite.sector_contradiction",
                  str(inbound.get("id") or ""), contradiction)
        return None

    channel = reply_channel(account_role, inbound.get("surface"))
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


def demo_invite_blocked_by_personal_link(store: Store, contact_id: str) -> bool:
    """Персональная ссылка уже выдана — демо-бота поверх неё не шлём.

    Запрос по контакту, а не по треду: у человека уже есть настоящий доступ, и
    письмо про общий демо-бот выглядело бы шагом назад.
    """
    row = store.one(
        "SELECT id FROM direct_invites WHERE contact_id = ? "
        "AND status IN (?, ?, ?)",
        (str(contact_id), STATUS_AGREED, STATUS_CREATED, STATUS_DELIVERED),
    )
    return row is not None


def record_demo_invite(
    store: Store,
    *,
    config: BranchConfig,
    thread: Mapping[str, Any],
    inbound: Mapping[str, Any],
    account_role: str,
    canonical_sector_id: str,
    at: str | None = None,
    actor: str = "autoreply",
) -> dict[str, Any] | None:
    """Собрать письмо со ссылкой на демо-бота. None — маршрут не подходит.

    Задачу здесь не ставим, а возвращаем текст: письмо должно уехать тем же
    одним сообщением, что и ответ движка. Отдельная задача второго письма не
    даёт — она либо отвергается («этому собеседнику уже поставлен ответ»),
    либо снимается ответом движка, который ставится с `supersede`. Привязать
    задачу обязан вызывающий: `attach_demo_delivery` или `cancel_demo_invite`.
    """
    if not config.demo_route_ready():
        return None

    channel = reply_channel(account_role, inbound.get("surface"))
    if channel not in ("channel_dm", "private_dm", "public_chat"):
        return None

    contact_id = str(thread.get("contact_id") or "").strip()
    if not contact_id:
        # Собеседник без username и с нулевым id: контакт не заведён, и запись
        # уронила бы разбор по внешнему ключу, оставив человека без ответа.
        store.log(actor, "demo.no_contact", str(thread.get("id") or ""), "")
        return None

    # Сфера нужна только для одного решения: «а не положен ли этому человеку
    # настоящий доступ вместо демо». Само письмо про сферу не знает и знать не
    # должно — демо-бот один на всех (`render_demo_message`).
    #
    # Поэтому ненайденная сфера — не отказ, а обычный случай. Раньше здесь
    # стоял немой `return None`, и он уводил человека обратно к менеджеру: так
    # 06.08 ушёл @secivn с подтверждённым «графическим дизайном». Словарь
    # закрытый, четырнадцать строк, и всё, чего в нём нет, попадало в этот
    # возврат — вместе с теми, кто сферу просто не назвал.
    sector = str(canonical_sector_id or "").strip()
    row = config.sector_row(sector)
    if row is None:
        if sector:
            # Такого не должно быть: id приходит из enum, собранного по этому
            # же словарю. Раз случилось — словарь и разбор разъехались.
            store.log(actor, "demo.sector_not_in_catalog", contact_id, sector)
        sector, status = "", SECTOR_STATUS_UNKNOWN
    else:
        status = row.status
        if status == SECTOR_STATUS_READY:
            # Готовой сфере положен настоящий доступ, а не демо. Сюда попасть
            # можно только при расхождении словаря и конфига выдачи.
            store.log(actor, "demo.sector_is_ready", sector, "")
            return None

    if demo_invite_blocked_by_personal_link(store, contact_id):
        return None

    existing = store.one(
        "SELECT * FROM demo_invites WHERE contact_id = ?", (contact_id,)
    )
    if existing is not None and str(existing["status"]) != DEMO_STATUS_CANCELLED:
        return None

    try:
        text = render_demo_message(config.demo_bot_link, seed=contact_id)
    except DirectInviteError as exc:
        store.log(actor, "demo.text_failed", contact_id, str(exc)[:200])
        return None

    stamp = at or now()
    if existing is not None:
        # Снятое письмо разрешает ровно одну повторную сборку: строка та же,
        # потому что contact_id уникален.
        store.execute(
            "UPDATE demo_invites SET thread_id = ?, account_id = ?, "
            "inbound_id = ?, source_channel = ?, canonical_sector_id = ?, "
            "sector_status = ?, task_id = NULL, status = ?, updated_at = ? "
            "WHERE id = ?",
            (str(thread["id"]), int(inbound["account_id"]), str(inbound["id"]),
             channel, sector, status, DEMO_STATUS_QUEUED, stamp,
             existing["id"]),
        )
        demo_id = str(existing["id"])
    else:
        demo_id = new_id("demo")
        store.execute(
            "INSERT INTO demo_invites(id, contact_id, thread_id, account_id, "
            "inbound_id, source_channel, canonical_sector_id, sector_status, "
            "status, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (demo_id, contact_id, str(thread["id"]),
             int(inbound["account_id"]), str(inbound["id"]), channel, sector,
             status, DEMO_STATUS_QUEUED, stamp, stamp),
        )
    store.log(actor, "demo.composed", contact_id,
              f"сфера={sector or '—'} статус={status}")
    result = dict(store.one("SELECT * FROM demo_invites WHERE id = ?", (demo_id,)))
    result["text"] = text
    return result


def attach_demo_delivery(store: Store, demo_row_id: str, task_id: str,
                         *, actor: str = "autoreply") -> None:
    """Связать демо-письмо с задачей, которая его везёт."""
    store.execute(
        "UPDATE demo_invites SET task_id = ?, updated_at = ? WHERE id = ?",
        (str(task_id), now(), str(demo_row_id)),
    )
    store.log(actor, "demo.queued", str(demo_row_id),
              f"задача={task_id} одним сообщением")
    store.commit()


def cancel_demo_invite(store: Store, demo_row_id: str, why: str,
                       *, actor: str = "autoreply") -> None:
    """Письма не будет. Строку снимаем, чтобы следующий ход мог повторить.

    В отличие от ссылки StartBot, здесь ничего не выпущено и терять нечего:
    ссылка на демо-бота общая и постоянная.
    """
    store.execute(
        "UPDATE demo_invites SET status = ?, updated_at = ? WHERE id = ?",
        (DEMO_STATUS_CANCELLED, now(), str(demo_row_id)),
    )
    store.log(actor, "demo.cancelled", str(demo_row_id), why[:200])
    store.commit()


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


def rescue_orphans(store: Store, *, actor: str = "invites") -> int:
    """Вернуть в очередь заявки, у которых ссылка есть, а везти её некому.

    Между «пометили выпущенной» и «привязали письмо» лежат два отдельных
    коммита, и падение процесса между ними оставляет заявку в тупике: она уже
    не `test_agreed`, значит её не подберёт `pending_requests`, но и `task_id`
    у неё пуст, значит её не закроет `reconcile_deliveries`. Человек согласился,
    ссылка выпущена, и о ней никто больше не вспомнит. Сюда же попадает письмо,
    отменённое заменой, — на случай, если защита в `supersede_pending_reply`
    когда-нибудь разъедется с этим кодом.

    Возврат безопасен: выпуск идемпотентен по `request_id` и отдаст ту же
    ссылку, а не вторую.
    """
    rows = store.query(
        "SELECT d.id, d.request_id FROM direct_invites d "
        "LEFT JOIN tasks t ON t.id = d.task_id "
        " WHERE d.status = ? "
        "   AND (d.task_id IS NULL OR t.id IS NULL OR t.state = 'cancelled')",
        (STATUS_CREATED,),
    )
    for row in rows:
        store.execute(
            "UPDATE direct_invites SET status = ?, task_id = NULL, "
            "last_error = ?, updated_at = ? WHERE id = ?",
            (STATUS_AGREED, "письма не оказалось — заявка возвращена в очередь",
             now(), row["id"]),
        )
        store.log(actor, "invite.rescued", str(row["request_id"]),
                  "ссылка выпущена, письма нет")
    if rows:
        store.commit()
    return len(rows)


def mint(
    store: Store,
    row: Mapping[str, Any],
    *,
    branch: BranchConfig,
    client: "StartBotClient",
    actor: str = "invites",
) -> "CreatedInvite | None":
    """Выпустить ссылку по одному согласию. None — не вышло.

    Учёт попыток, откладывание и вызов человека на исчерпании живут здесь, в
    одном месте: выпуск зовут теперь двое — разбор входящих, чтобы отправить
    ссылку тем же сообщением, и отдельный проход, который подбирает всё, что
    у разбора не получилось. Две копии политики повторов разъехались бы.
    """
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

        return client.create_direct_invite(
            request_id=request_id,
            source_channel=str(row["source_channel"]),
            source_conversation_id=str(row["thread_id"]),
            consent_recorded_at=_parse(str(row["consent_recorded_at"])),
            profile=profile,
            display_name=str(display_name) if display_name else None,
            validity_days=branch.validity_days,
        )
    except DirectInviteError as exc:
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
        return None


def issue_inline(
    store: Store,
    request_id: str,
    *,
    config: BranchConfig | None = None,
    client: "StartBotClient | None" = None,
    actor: str = "autoreply",
) -> dict[str, Any] | None:
    """Выпустить ссылку прямо в разборе, чтобы отправить её одним сообщением.

    Иначе человек получает два: сначала «принято, ссылка придёт отдельно», и
    только через 5–7 минут саму ссылку — столько ждёт поаккаунтный темп Radar
    между двумя видимыми действиями. Одно письмо со ссылкой читается лучше.

    Возвращает `{"text", "invite_row_id"}` либо None. None — это не поломка, а
    команда идти прежним путём: ответ движка уходит как есть, а ссылку позже
    подберёт отдельный проход. Поэтому здесь не бросаем исключений: недоступный
    StartBot не должен оставлять человека вообще без ответа.

    Заявка сразу помечается выпущенной, ещё до постановки задачи: иначе её
    успел бы подхватить проход по таймеру и выпустить доставку второй раз.
    Привязать задачу обязан вызывающий — `attach_delivery` или `release`.
    """
    branch = config if config is not None else BranchConfig.from_env()
    if not branch.enabled:
        return None
    row = store.one(
        "SELECT * FROM direct_invites WHERE request_id = ? AND status = ?",
        (str(request_id), STATUS_AGREED),
    )
    if row is None:
        return None
    row = dict(row)
    if client is None:
        try:
            client = StartBotClient(StartBotConfig.from_env())
        except DirectInviteError as exc:
            store.log(actor, "invite.client_unavailable", str(request_id),
                      str(exc)[:200])
            store.commit()
            return None
    invite = mint(store, row, branch=branch, client=client, actor=actor)
    if invite is None:
        return None
    store.execute(
        "UPDATE direct_invites SET invite_id = ?, invite_expires_at = ?, "
        "status = ?, attempt_count = attempt_count + 1, next_attempt_at = NULL, "
        "last_error = NULL, updated_at = ? WHERE id = ?",
        (invite.invite_id, invite.expires_at, STATUS_CREATED, now(), row["id"]),
    )
    store.commit()
    return {"text": invite.ready_message, "invite_row_id": str(row["id"]),
            "request_id": request_id, "replayed": invite.replayed}


def attach_delivery(store: Store, invite_row_id: str, task_id: str,
                    *, actor: str = "autoreply") -> None:
    """Связать выпущенную ссылку с задачей, которая её везёт."""
    store.execute(
        "UPDATE direct_invites SET task_id = ?, updated_at = ? WHERE id = ?",
        (str(task_id), now(), str(invite_row_id)),
    )
    store.log(actor, "invite.created", str(invite_row_id),
              f"задача={task_id} одним сообщением")
    store.commit()


def release_inline(store: Store, invite_row_id: str, why: str,
                   *, actor: str = "autoreply") -> None:
    """Вернуть заявку в очередь, если отправить письмо так и не вышло.

    Ссылка уже выпущена и в StartBot существует, но никто её не везёт. Статус
    возвращаем в «согласие есть», чтобы её подобрал проход по таймеру: выпуск
    идемпотентен по `request_id` и вернёт ту же ссылку, а не вторую.
    """
    store.execute(
        "UPDATE direct_invites SET status = ?, last_error = ?, updated_at = ? "
        "WHERE id = ?",
        (STATUS_AGREED, f"письмо не поставлено: {why}"[:300], now(),
         str(invite_row_id)),
    )
    store.log(actor, "invite.inline_released", str(invite_row_id), why[:200])
    store.commit()


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

    rescued = rescue_orphans(store, actor=actor)
    rows = pending_requests(store, limit=limit)
    if not rows:
        return {"состояние": "пусто", "выпущено": 0, "ошибок": 0,
                "разобрано": 0, "спасено": rescued}

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
        invite = mint(store, row, branch=branch, client=client, actor=actor)
        if invite is None:
            failed += 1
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
            "ошибок": failed, "спасено": rescued}


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
