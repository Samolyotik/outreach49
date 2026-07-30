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
DEFAULT_HOME = Path("/opt/bridge49")

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
    #: Сколько всего видимых действий выпускать за один прогон диспетчера.
    dispatch_batch: int = 25
    #: Часы окна отправки в таймзоне кампании (включительно/исключительно).
    send_window_start_hour: int = 10
    send_window_end_hour: int = 20
    #: Дни недели, когда разрешена отправка (0=понедельник).
    send_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)


@dataclass
class Settings:
    home: Path
    db_path: Path
    dsn: RadarDsn | None
    limits: Limits
    timezone: str = "Europe/Moscow"

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
    limits_path = var / "limits.json"
    if limits_path.exists():
        raw = json.loads(limits_path.read_text(encoding="utf-8"))
        for key, value in raw.items():
            if hasattr(limits, key):
                setattr(limits, key, value)
        if isinstance(limits.send_weekdays, list):
            limits.send_weekdays = tuple(limits.send_weekdays)

    dsn = RadarDsn.from_secret_file() if need_dsn else None
    return Settings(
        home=home,
        db_path=Path(os.environ.get("BRIDGE49_DB", var / "bridge49.sqlite")),
        dsn=dsn,
        limits=limits,
        timezone=os.environ.get("BRIDGE49_TZ", "Europe/Moscow"),
    )
