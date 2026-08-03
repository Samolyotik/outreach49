"""Конфигурация bridge49.

Ничего не выдумываем: реквизиты моста берём из того же файла, который уже
использует существующий tgradar-outreach. Мы его только читаем.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

#: Канонический секрет моста. Тот же файл, что читает tgradar-outreach.
DEFAULT_SECRET_PATH = Path("/var/lib/tgradar-outreach/secrets/tgr_bridge.env")

#: Корень установки. Переопределяется переменной BRIDGE49_HOME.
DEFAULT_HOME = Path("/opt/outreach49")

#: Бизнес наших TGR-аккаунтов.
DEFAULT_BUSINESS_ID = 51


class ConfigError(RuntimeError):
    """Конфигурация неполна или противоречива."""


def _parse_env_file(path: Path) -> dict[str, str]:
    """Разобрать `KEY=value` файл. Кавычки снимаем, комментарии пропускаем."""
    if not path.exists():
        raise ConfigError(
            f"нет файла с реквизитами моста: {path}\n"
            "Это тот же файл, что использует tgradar-outreach. "
            "Запускать bridge49 нужно с правами, позволяющими его прочитать."
        )
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


@dataclass(frozen=True)
class RadarDsn:
    """Куда и под кем ходить в базу Radar."""

    host: str
    port: int
    database: str
    user: str
    password: str = field(repr=False)
    sslmode: str = "disable"
    ssl_root_cert: str | None = None
    pool_max_size: int = 4
    business_id: int = DEFAULT_BUSINESS_ID

    @classmethod
    def from_secret_file(cls, path: Path | None = None) -> "RadarDsn":
        path = Path(path or os.environ.get("BRIDGE49_SECRET", DEFAULT_SECRET_PATH))
        values = _parse_env_file(path)
        required = (
            "TGR_BRIDGE_DATABASE_HOST",
            "TGR_BRIDGE_DATABASE_PORT",
            "TGR_BRIDGE_DATABASE_NAME",
            "TGR_BRIDGE_DATABASE_USER",
            "TGR_BRIDGE_DATABASE_PASSWORD",
        )
        missing = [key for key in required if not values.get(key)]
        if missing:
            raise ConfigError(f"в {path} не хватает ключей: {', '.join(missing)}")
        return cls(
            host=values["TGR_BRIDGE_DATABASE_HOST"],
            port=int(values["TGR_BRIDGE_DATABASE_PORT"]),
            database=values["TGR_BRIDGE_DATABASE_NAME"],
            user=values["TGR_BRIDGE_DATABASE_USER"],
            password=values["TGR_BRIDGE_DATABASE_PASSWORD"],
            sslmode=values.get("TGR_BRIDGE_DATABASE_SSLMODE", "disable"),
            ssl_root_cert=values.get("TGR_BRIDGE_DATABASE_SSL_ROOT_CERT") or None,
            pool_max_size=int(values.get("TGR_BRIDGE_POOL_MAX_SIZE", "4")),
            business_id=int(
                values.get("TGR_BRIDGE_BUSINESS_ID", str(DEFAULT_BUSINESS_ID))
            ),
        )

    def describe(self) -> str:
        """Строка для логов. Пароль сюда не попадает — и не должен."""
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


@dataclass
class Limits:
    """Локальные ограничители. Radar темпирует и сам, это второй пояс."""

    #: Сколько видимых действий (visible/mature_dm) на аккаунт в сутки.
    per_account_daily_visible: int = 12
    #: Минимальный интервал между видимыми действиями одного аккаунта, сек.
    per_account_visible_interval_sec: int = 900
    #: Случайная добавка к интервалу, сек: 0..N поверх минимума. Без неё
    #: отправки ложатся на ровную сетку 15:00, 15:15, 15:30 — это само по
    #: себе машинный след. Добавка всегда вверх, чтобы план не проваливался
    #: под пол, который диспетчер проверяет на выпуске.
    per_account_visible_jitter_sec: int = 420
    #: Минимальная пауза между видимыми действиями ЛЮБЫХ аккаунтов, секунды.
    #: Пол на аккаунт держит ритм внутри одной персоны, но не поперёк флота:
    #: тринадцать аккаунтов вправе выпустить сообщения одной и той же секундой,
    #: и со стороны это ровно тот залп, из-за которого всё и началось. Пауза
    #: берётся случайной в диапазоне min..max, иначе поток ляжет на ровную сетку.
    global_visible_interval_min_sec: int = 10
    global_visible_interval_max_sec: int = 20
    #: Сколько всего видимых действий выпускать за один прогон диспетчера.
    dispatch_batch: int = 25
    #: Часы окна отправки в таймзоне кампании (включительно/исключительно).
    send_window_start_hour: int = 10
    send_window_end_hour: int = 20
    #: Дни недели, когда разрешена отправка (0=понедельник).
    send_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)


#: Жёсткий пол. `limits.json` может делать темп СТРОЖЕ и только строже.
#:
#: Это не паранойя, а вывод из инцидента 01.08 на соседнем контуре: там в
#: limits.json оказались `interval_sec: 0`, окно 00–24 и батч 5000, и вся
#: рассылка ушла одним залпом. Файл правится руками и агентами, а цена
#: ошибки — аккаунты, которые греются неделями. Поэтому значения из файла
#: зажимаются здесь и об этом сообщается в `doctor`.
HARD_MAX_DAILY_VISIBLE = 40
HARD_MIN_INTERVAL_SEC = 300
HARD_MIN_GLOBAL_INTERVAL_SEC = 5
HARD_MAX_DISPATCH_BATCH = 50
HARD_WINDOW_START_HOUR = 8
HARD_WINDOW_END_HOUR = 22


def clamp(limits: Limits) -> list[str]:
    """Зажать лимиты в границы пола. Возвращает список сделанных поправок."""
    notes: list[str] = []

    def fix(field_name: str, value: int, why: str) -> None:
        notes.append(f"{field_name}: {getattr(limits, field_name)} → {value} ({why})")
        setattr(limits, field_name, value)

    daily = int(limits.per_account_daily_visible)
    if daily > HARD_MAX_DAILY_VISIBLE:
        fix("per_account_daily_visible", HARD_MAX_DAILY_VISIBLE,
            f"пол: не больше {HARD_MAX_DAILY_VISIBLE} в сутки на аккаунт")
    elif daily < 0:
        fix("per_account_daily_visible", 0, "отрицательный лимит")

    interval = int(limits.per_account_visible_interval_sec)
    if interval < HARD_MIN_INTERVAL_SEC:
        fix("per_account_visible_interval_sec", HARD_MIN_INTERVAL_SEC,
            f"пол: пауза не меньше {HARD_MIN_INTERVAL_SEC} с")

    if int(limits.per_account_visible_jitter_sec) < 0:
        fix("per_account_visible_jitter_sec", 0, "отрицательный разброс")

    global_min = int(limits.global_visible_interval_min_sec)
    if global_min < HARD_MIN_GLOBAL_INTERVAL_SEC:
        fix("global_visible_interval_min_sec", HARD_MIN_GLOBAL_INTERVAL_SEC,
            f"пол: между аккаунтами не меньше {HARD_MIN_GLOBAL_INTERVAL_SEC} с")
    if int(limits.global_visible_interval_max_sec) < limits.global_visible_interval_min_sec:
        # Схлопнутый диапазон означал бы «пауза ровно min» без разброса —
        # молча терять джиттер нельзя, поэтому поправка попадает в notes.
        fix("global_visible_interval_max_sec",
            int(limits.global_visible_interval_min_sec),
            "верхняя граница ниже нижней")

    batch = int(limits.dispatch_batch)
    if batch > HARD_MAX_DISPATCH_BATCH:
        fix("dispatch_batch", HARD_MAX_DISPATCH_BATCH,
            f"пол: не больше {HARD_MAX_DISPATCH_BATCH} за прогон")
    elif batch < 1:
        fix("dispatch_batch", 1, "пустой батч")

    if int(limits.send_window_start_hour) < HARD_WINDOW_START_HOUR:
        fix("send_window_start_hour", HARD_WINDOW_START_HOUR,
            f"пол: окно не раньше {HARD_WINDOW_START_HOUR}:00")
    if int(limits.send_window_end_hour) > HARD_WINDOW_END_HOUR:
        fix("send_window_end_hour", HARD_WINDOW_END_HOUR,
            f"пол: окно не позже {HARD_WINDOW_END_HOUR}:00")
    if limits.send_window_end_hour <= limits.send_window_start_hour:
        fix("send_window_end_hour", HARD_WINDOW_END_HOUR, "окно схлопнулось")

    days = tuple(sorted({int(d) for d in limits.send_weekdays if 0 <= int(d) <= 6}))
    if days != tuple(limits.send_weekdays):
        notes.append(f"send_weekdays: {tuple(limits.send_weekdays)} → {days}")
        limits.send_weekdays = days

    return notes


@dataclass
class Settings:
    home: Path
    db_path: Path
    dsn: RadarDsn | None
    limits: Limits
    timezone: str = "Europe/Moscow"
    #: Что пришлось зажать при чтении limits.json. Показывается в `doctor`.
    limits_notes: list[str] = field(default_factory=list)

    @property
    def armed_file(self) -> Path:
        """Наличие этого файла — единственный переключатель «боевого» режима."""
        return self.home / "var" / "ARMED"

    @property
    def armed(self) -> bool:
        return self.armed_file.exists()

    @property
    def accounts_snapshot(self) -> Path:
        return self.home / "accounts.json"

    def limits_file(self) -> Path:
        return self.home / "var" / "limits.json"


def load(home: Path | str | None = None, *, need_dsn: bool = False) -> Settings:
    """Собрать настройки. Без `need_dsn` секрет не читается вовсе."""
    home = Path(home or os.environ.get("BRIDGE49_HOME", DEFAULT_HOME))
    var = home / "var"
    var.mkdir(parents=True, exist_ok=True)

    limits = Limits()
    notes: list[str] = []
    limits_path = var / "limits.json"
    if limits_path.exists():
        raw = json.loads(limits_path.read_text(encoding="utf-8"))
        for key, value in raw.items():
            if hasattr(limits, key):
                setattr(limits, key, value)
        if isinstance(limits.send_weekdays, list):
            limits.send_weekdays = tuple(limits.send_weekdays)
        # Файл может только ужесточать темп. Всё, что мягче пола, зажимается.
        notes = clamp(limits)

    dsn = RadarDsn.from_secret_file() if need_dsn else None
    return Settings(
        home=home,
        db_path=Path(os.environ.get("BRIDGE49_DB", var / "bridge49.sqlite")),
        dsn=dsn,
        limits=limits,
        timezone=os.environ.get("BRIDGE49_TZ", "Europe/Moscow"),
        limits_notes=notes,
    )
