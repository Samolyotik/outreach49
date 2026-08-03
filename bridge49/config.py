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

    # -- ответы -------------------------------------------------------------
    #
    # У ответа другая природа, чем у рассылки, поэтому и темп другой. Рассылку
    # мы затеваем сами и вправе растянуть её как угодно; ответ ждёт живой
    # человек, который только что написал. Пауза в полчаса перед «здравствуйте»
    # — это не осторожность, а молчание в лицо.
    #
    # Поэтому счётчики разведены: наплыв входящих не съедает бюджет рассылки, а
    # рассылка не глушит ответы. Но развести до конца нельзя — Telegram видит
    # один аккаунт, а не два независимых потока, и общий пол между любыми
    # видимыми действиями остаётся.

    #: Сколько ответов на аккаунт в сутки. Отдельно от бюджета рассылки.
    reply_per_account_daily: int = 40
    #: Пол между ответами одного аккаунта, сек.
    reply_per_account_interval_sec: int = 60
    #: Пауза между ответами разных аккаунтов, сек.
    reply_global_interval_min_sec: int = 3
    reply_global_interval_max_sec: int = 8
    #: Ответы идут круглосуточно и без выходных. Это не послабление, а перенос
    #: поведения прежнего контура: там `require_send_window_for_auto_reply`
    #: стоял в False и в коде, и в боевом конфиге. Окно нужно рассылке, которую
    #: мы затеваем сами; человека, написавшего ночью, оно бы просто заставило
    #: ждать до утра без всякой пользы.
    reply_window_start_hour: int = 0
    reply_window_end_hour: int = 24
    reply_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    #: Задержка перед автоответом, считается от входящего. Не темп, а правдо-
    #: подобие: ответ через полсекунды выдаёт автомат вернее любого текста.
    reply_delay_after_inbound_min_sec: int = 10
    reply_delay_after_inbound_max_sec: int = 30

    # -- чтение метаданных --------------------------------------------------
    #
    # Разведка ничего не показывает собеседнику, и по этой причине ни один
    # темп на неё до сих пор не распространялся: ни наш — `preflight` пропускал
    # read мимо всех проверок, — ни Radar, где паcятся только `visible` и
    # `mature_dm`. Между тем `search_public_chat` и вся семья `check_*` — это
    # resolve имени в Telegram, а у него собственный лимит, никак не связанный
    # с видимостью. Исполнитель Radar берёт по одной команде за тик, то есть
    # до одной в секунду на аккаунт, и пачка созревших задач ушла бы именно с
    # такой скоростью — прямиком в FLOOD_WAIT.
    #
    # Поэтому у чтения свой класс темпа. Смотрит класс только на себя: read не
    # отнимает права у рассылки и не ждёт её — Telegram считает эти лимиты
    # порознь.
    #
    # Числа не выдуманы, а взяты у прежнего контура: профиль `standard` из
    # `configs/account_task_speeds.json`, роль `source_reader`. Он там же и
    # отработан — 17.07 пять читателей сделали 415 проверок за девять часов,
    # то есть ровно одну на 76 секунд по флоту. Своих чисел здесь быть не
    # должно: разведка ходит теми же RPC и упирается в тот же лимит.

    #: Сколько чтений метаданных на аккаунт в сутки. Их `daily_cap_per_account`.
    read_per_account_daily: int = 100
    #: Пауза между чтениями одного аккаунта: `interval` + случайное из
    #: `0..jitter`. Не фиксированный пол, а именно диапазон, и вот почему.
    #:
    #: Сотня в сутки — это одна проверка в 864 секунды. С жёстким полом в
    #: четыре минуты аккаунт отстреливал бы свою сотню за семь часов и
    #: семнадцать стоял без дела: суточная норма соблюдена, но выглядит это
    #: как рабочая смена робота, а не как человек, поглядывающий в Telegram.
    #: Диапазон 540–1200 с даёт в среднем 870 с — сотня ровно на сутки, при
    #: этом соседние паузы отличаются вдвое.
    #:
    #: Пауза разыгрывается на выпуске и запоминается по аккаунту, а не
    #: считается от последней попытки: иначе накопившийся долг (аккаунт стоял
    #: на паузе, задачи просрочены) вылился бы ровным шагом по нижней границе.
    read_per_account_interval_sec: int = 540
    read_per_account_interval_jitter_sec: int = 660
    #: Пауза между чтениями разных аккаунтов, сек. Их `global_gap`.
    read_global_interval_min_sec: int = 60
    read_global_interval_max_sec: int = 90
    #: Разведка идёт круглосуточно — решение владельца от 03.08.2026.
    #:
    #: У прежнего контура окно было (`work_calendar`, 06:00–23:00 МСК в обоих
    #: боевых конфигах), и довод за него понятен: аккаунт, перебирающий имена
    #: в четыре утра, на живого человека не похож. Довод против оказался
    #: сильнее — чтение никому не видно, ночь это треть суток, а от лимита
    #: resolve защищает не расписание, а скорость, и она остаётся прежней.
    read_window_start_hour: int = 0
    read_window_end_hour: int = 24
    read_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)


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

#: Свой пол для ответов. Он мягче рассылочного, но он есть: развязка темпа —
#: это другой предохранитель, а не его отсутствие. Сорок ответов в сутки с
#: минутным полом — это переписка занятого человека, а не бот.
HARD_MAX_REPLY_DAILY = 100
HARD_MIN_REPLY_INTERVAL_SEC = 30
HARD_MIN_REPLY_GLOBAL_INTERVAL_SEC = 2
#: Ответам ночное окно не зажимаем: круглосуточная работа — это перенесённое
#: поведение прежнего контура, а не просмотренная дыра. Пол оставлен только на
#: бессмысленные значения (перевёрнутое или вышедшее за сутки окно).
HARD_REPLY_WINDOW_START_HOUR = 0
HARD_REPLY_WINDOW_END_HOUR = 24

#: Пол для чтения. Окна у него нет — разведку никто не наблюдает, и ночной
#: запрет означал бы только то, что сутки простаивают впустую. А вот скорость
#: ограничена жёстко, и это ровно та огибающая, внутри которой прежний контур
#: держал все три своих профиля: быстрее «fast» не пускаем никого. Сто чтений
#: в сутки — их `daily_cap_per_account_max`, и это потолок, а не умолчание.
HARD_MAX_READ_DAILY = 100
HARD_MIN_READ_INTERVAL_SEC = 240
HARD_MIN_READ_GLOBAL_INTERVAL_SEC = 60
#: Ночь разведке не запрещаем — см. `read_window_*`. Пол оставлен только на
#: бессмысленные значения (перевёрнутое или вышедшее за сутки окно).
HARD_READ_WINDOW_START_HOUR = 0
HARD_READ_WINDOW_END_HOUR = 24


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

    # -- тот же пол для ответов, своими числами -----------------------------

    reply_daily = int(limits.reply_per_account_daily)
    if reply_daily > HARD_MAX_REPLY_DAILY:
        fix("reply_per_account_daily", HARD_MAX_REPLY_DAILY,
            f"пол: не больше {HARD_MAX_REPLY_DAILY} ответов в сутки на аккаунт")
    elif reply_daily < 0:
        fix("reply_per_account_daily", 0, "отрицательный лимит")

    if int(limits.reply_per_account_interval_sec) < HARD_MIN_REPLY_INTERVAL_SEC:
        fix("reply_per_account_interval_sec", HARD_MIN_REPLY_INTERVAL_SEC,
            f"пол: между ответами не меньше {HARD_MIN_REPLY_INTERVAL_SEC} с")

    if int(limits.reply_global_interval_min_sec) < HARD_MIN_REPLY_GLOBAL_INTERVAL_SEC:
        fix("reply_global_interval_min_sec", HARD_MIN_REPLY_GLOBAL_INTERVAL_SEC,
            f"пол: между ответами разных аккаунтов не меньше "
            f"{HARD_MIN_REPLY_GLOBAL_INTERVAL_SEC} с")
    if int(limits.reply_global_interval_max_sec) < limits.reply_global_interval_min_sec:
        fix("reply_global_interval_max_sec",
            int(limits.reply_global_interval_min_sec),
            "верхняя граница ниже нижней")

    if int(limits.reply_window_start_hour) < HARD_REPLY_WINDOW_START_HOUR:
        fix("reply_window_start_hour", HARD_REPLY_WINDOW_START_HOUR,
            f"пол: ответы не раньше {HARD_REPLY_WINDOW_START_HOUR}:00")
    if int(limits.reply_window_end_hour) > HARD_REPLY_WINDOW_END_HOUR:
        fix("reply_window_end_hour", HARD_REPLY_WINDOW_END_HOUR,
            f"пол: ответы не позже {HARD_REPLY_WINDOW_END_HOUR}:00")
    if limits.reply_window_end_hour <= limits.reply_window_start_hour:
        fix("reply_window_end_hour", HARD_REPLY_WINDOW_END_HOUR, "окно схлопнулось")

    reply_days = tuple(sorted({int(d) for d in limits.reply_weekdays if 0 <= int(d) <= 6}))
    if reply_days != tuple(limits.reply_weekdays):
        notes.append(f"reply_weekdays: {tuple(limits.reply_weekdays)} → {reply_days}")
        limits.reply_weekdays = reply_days

    delay_min = int(limits.reply_delay_after_inbound_min_sec)
    if delay_min < 0:
        fix("reply_delay_after_inbound_min_sec", 0, "отрицательная задержка")
    if int(limits.reply_delay_after_inbound_max_sec) < limits.reply_delay_after_inbound_min_sec:
        fix("reply_delay_after_inbound_max_sec",
            int(limits.reply_delay_after_inbound_min_sec),
            "верхняя граница ниже нижней")

    # -- тот же пол для чтения ----------------------------------------------

    read_daily = int(limits.read_per_account_daily)
    if read_daily > HARD_MAX_READ_DAILY:
        fix("read_per_account_daily", HARD_MAX_READ_DAILY,
            f"пол: не больше {HARD_MAX_READ_DAILY} чтений в сутки на аккаунт")
    elif read_daily < 0:
        fix("read_per_account_daily", 0, "отрицательный лимит")

    if int(limits.read_per_account_interval_sec) < HARD_MIN_READ_INTERVAL_SEC:
        fix("read_per_account_interval_sec", HARD_MIN_READ_INTERVAL_SEC,
            f"пол: между чтениями не меньше {HARD_MIN_READ_INTERVAL_SEC} с")

    if int(limits.read_per_account_interval_jitter_sec) < 0:
        fix("read_per_account_interval_jitter_sec", 0, "отрицательный разброс")

    if int(limits.read_global_interval_min_sec) < HARD_MIN_READ_GLOBAL_INTERVAL_SEC:
        fix("read_global_interval_min_sec", HARD_MIN_READ_GLOBAL_INTERVAL_SEC,
            f"пол: между чтениями разных аккаунтов не меньше "
            f"{HARD_MIN_READ_GLOBAL_INTERVAL_SEC} с")
    if int(limits.read_global_interval_max_sec) < limits.read_global_interval_min_sec:
        fix("read_global_interval_max_sec",
            int(limits.read_global_interval_min_sec),
            "верхняя граница ниже нижней")

    if int(limits.read_window_start_hour) < HARD_READ_WINDOW_START_HOUR:
        fix("read_window_start_hour", HARD_READ_WINDOW_START_HOUR,
            f"пол: разведка не раньше {HARD_READ_WINDOW_START_HOUR}:00")
    if int(limits.read_window_end_hour) > HARD_READ_WINDOW_END_HOUR:
        fix("read_window_end_hour", HARD_READ_WINDOW_END_HOUR,
            f"пол: разведка не позже {HARD_READ_WINDOW_END_HOUR}:00")
    if limits.read_window_end_hour <= limits.read_window_start_hour:
        fix("read_window_end_hour", HARD_READ_WINDOW_END_HOUR, "окно схлопнулось")

    read_days = tuple(sorted({int(d) for d in limits.read_weekdays if 0 <= int(d) <= 6}))
    if read_days != tuple(limits.read_weekdays):
        notes.append(f"read_weekdays: {tuple(limits.read_weekdays)} → {read_days}")
        limits.read_weekdays = read_days

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
    def autoreply_file(self) -> Path:
        """Отдельный рубильник автоответов, независимый от ARMED.

        Их два, потому что это два разных решения. ARMED разрешает флоту
        обращаться к Telegram вообще; этот — разрешает машине самой сочинять,
        что ответить человеку. Второе можно захотеть выключить, не останавливая
        первое, и наоборот.
        """
        return self.home / "var" / "AUTOREPLY"

    @property
    def autoreply_enabled(self) -> bool:
        return self.autoreply_file.exists()

    @property
    def autoreply_strangers_file(self) -> Path:
        """Разрешить машине отвечать тем, кому мы не писали первыми.

        Выключено, потому что на аккаунтах остались собеседники прежних
        владельцев: они пишут не нам. Включать имеет смысл, когда пойдут
        обращения по нашим собственным ссылкам.
        """
        return self.home / "var" / "AUTOREPLY_STRANGERS"

    @property
    def autoreply_strangers(self) -> bool:
        return self.autoreply_strangers_file.exists()

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
        # JSON знает только списки, а дни недели дальше сравниваются и
        # печатаются как кортежи. Приводим все три набора, а не один: забытый
        # набор ведёт себя правильно ровно до первой попытки его показать.
        for field_name in ("send_weekdays", "reply_weekdays", "read_weekdays"):
            value = getattr(limits, field_name)
            if isinstance(value, list):
                setattr(limits, field_name, tuple(value))
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
