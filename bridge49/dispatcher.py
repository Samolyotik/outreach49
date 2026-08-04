"""Диспетчер: единственное место, где bridge49 пишет в Radar.

Порядок операций выбран так, чтобы обрыв связи в любой точке не привёл к
повторной отправке:

1. UUID запроса пишется в локальную базу **до** обращения к Radar, причём
   атомарно — `WHERE request_id IS NULL`;
2. затем вызывается enqueue;
3. затем сохраняется полученный command_id.

Если процесс умер между 1 и 3, повторный прогон вызывает enqueue с тем же
UUID и тем же каноническим request. Контракт моста гарантирует, что такой
повтор идемпотентен и вернёт уже существующий command_id, а не создаст вторую
отправку. Новый UUID после таймаута — единственное, чего делать нельзя.

Весь прогон дополнительно защищён файловой блокировкой: два одновременных
`dispatch` (например, таймер и ручной запуск) иначе выдали бы одной задаче
два разных UUID, а два UUID — это два `dedup_key` и две реальные отправки.
"""
from __future__ import annotations

import asyncio
import fcntl
import random
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import accounts as accounts_mod
from . import catalog, replies, report
from .config import Settings
from .radar import (
    BridgeError,
    BridgeRejected,
    BridgeUnknown,
    QueueFull,
    RadarBridge,
    build_request,
    new_request_id,
)
from .store import Store, loads, now

#: Действия, которые собеседник видит. По ним считается дневной лимит.
VISIBLE_ACTIONS = tuple(
    sorted(name for name, a in catalog.ACTIONS.items() if a.visible)
)

#: Чтение метаданных, которое реально идёт в Telegram, — в стабильном порядке
#: для подстановки в SQL. Состав задаёт каталог.
READ_ACTIONS = tuple(sorted(catalog.READ_ACTIONS))


class DispatchBlocked(RuntimeError):
    """Задачу нельзя выпускать прямо сейчас. Задача остаётся запланированной."""


class DispatchTooEarly(DispatchBlocked):
    """Ещё не истекла пауза флота — это «пока рано», а не «нельзя».

    Отдельный тип нужен, чтобы боевой цикл выждал паузу и всё-таки выпустил
    задачу. Если бы такая задача просто блокировалась, за прогон уходило бы
    ровно одно сообщение, и пауза между аккаунтами превратилась бы в потолок
    пропускной способности.
    """

    def __init__(self, message: str, *, wait_seconds: int) -> None:
        super().__init__(message)
        self.wait_seconds = int(wait_seconds)


#: Дольше этого внутри одного прогона не ждём: пауза флота исчисляется
#: секундами, и большее значение означает не темп, а перекос в настройках.
MAX_INLINE_WAIT_SEC = 60


class DispatchBusy(RuntimeError):
    """Другой процесс уже выпускает задачи."""


#: Сколько ждать чужой прогон, прежде чем сдаться. Замок один на все классы, а
#: таймеры у них разные: разведка ходит раз в минуту и держит его секунд
#: двадцать, рассылка — раз в две минуты. Без ожидания рассылка регулярно
#: заставала замок занятым и пропускала прогон целиком, отставая от собственных
#: сроков; с ожиданием она просто дожидается своей очереди.
#:
#: Разделить замок по классам было бы неверно: прогон без фильтра берёт все
#: классы разом, и тогда он ничего не знал бы про чужие частные замки — два
#: процесса выпустили бы одну задачу дважды.
LOCK_WAIT_SEC = 45.0


@contextmanager
def exclusive(settings: Settings, *, wait_sec: float = 0.0):
    """Один выпускающий процесс на установку."""
    path = Path(settings.home) / "var" / "dispatch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    deadline = time.monotonic() + max(0.0, float(wait_sec))
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise DispatchBusy(
                        f"другой dispatch уже работает (блокировка {path})"
                    ) from exc
                time.sleep(0.5)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def due_tasks(store: Store, *, campaign_id: str | None = None,
              cadence: str | None = None, limit: int = 50) -> list[dict]:
    """Задачи, которым пора: время пришло."""
    sql = (
        "SELECT t.*, c.status AS campaign_status, c.name AS campaign_name, "
        "c.allow_repeat_contacts AS campaign_allow_repeat "
        "FROM tasks t JOIN campaigns c ON c.id = t.campaign_id "
        "WHERE t.state = 'planned' AND t.scheduled_at <= ? "
    )
    params: list = [now()]
    if campaign_id:
        sql += "AND t.campaign_id = ? "
        params.append(campaign_id)
    if cadence:
        actions = _cadence_actions(cadence)
        sql += f"AND t.action IN ({','.join('?' * len(actions))}) "
        params.extend(actions)
    sql += "ORDER BY t.scheduled_at, t.id LIMIT ?"
    params.append(int(limit))

    rows = [dict(row) for row in store.query(sql, params)]
    for row in rows:
        row["params"] = loads(row.get("params"), {})
    return rows


def orphaned(store: Store, *, campaign_id: str | None = None,
             cadence: str | None = None) -> list[dict]:
    """Задачи с UUID, но без command_id — прошлый прогон оборвался.

    Кампанию джойним обязательно: UUID пишется ДО обращения к Radar, поэтому
    оборванная задача могла ещё не уехать вовсе. Без статуса кампании такая
    задача прошла бы гейт «только active» и выпустилась из поставленной на
    паузу кампании.
    """
    sql = (
        "SELECT t.*, c.status AS campaign_status, c.name AS campaign_name, "
        "c.allow_repeat_contacts AS campaign_allow_repeat "
        "FROM tasks t JOIN campaigns c ON c.id = t.campaign_id "
        "WHERE t.state = 'planned' AND t.request_id IS NOT NULL "
    )
    params: list = []
    if campaign_id:
        sql += "AND t.campaign_id = ? "
        params.append(campaign_id)
    if cadence:
        actions = _cadence_actions(cadence)
        sql += f"AND t.action IN ({','.join('?' * len(actions))}) "
        params.extend(actions)
    sql += "ORDER BY t.scheduled_at"

    out = []
    for row in store.query(sql, params):
        item = dict(row)
        item["params"] = loads(item.get("params"), {})
        out.append(item)
    return out


#: Класс темпа. Рассылку мы затеваем сами, ответ ждёт живой человек — это
#: разные занятия, и меряются они разными числами. Ответ узнаётся по действию:
#: `reply_private_dm` — единственное, которое адресуется тому, кто написал нам
#: сам, и потому не является первым касанием.
CADENCE_OUTREACH = "outreach"
CADENCE_REPLY = "reply"
#: Третий класс: чтение метаданных. Собеседнику не видно ничего, но у Telegram
#: свой счётчик на resolve, и он не смотрит на то, видно нам или нет.
CADENCE_READ = "read"


def cadence_of(task: dict) -> str:
    """К какому классу темпа относится задача."""
    action = task.get("action")
    if action in replies.REPLY_ACTIONS:
        return CADENCE_REPLY
    if action in READ_ACTIONS:
        return CADENCE_READ
    return CADENCE_OUTREACH


def _cadence_actions(cadence: str) -> tuple[str, ...]:
    """Какие действия считаются в бюджете этого класса.

    Бюджеты раздельные: сорок автоответов за день не должны обнулить дневную
    норму рассылки, рассылка — лишить людей ответов, а разведка каталога — ни
    того, ни другого.
    """
    if cadence == CADENCE_REPLY:
        return tuple(sorted(replies.REPLY_ACTIONS))
    if cadence == CADENCE_READ:
        return READ_ACTIONS
    return tuple(a for a in VISIBLE_ACTIONS if a not in replies.REPLY_ACTIONS)


def sent_today(
    store: Store, account_id: int, cadence: str = CADENCE_OUTREACH
) -> int:
    """Сколько действий этого класса аккаунт уже израсходовал за сегодня.

    Считаются видимые и чтения; `soft` (прочитано, «печатает») не считается
    нигде — это не отдельный поход в Telegram, а довесок к действию, рядом с
    которым он и стоит.

    Отказ моста расходует дневной бюджет наравне с удачной отправкой: со
    стороны Telegram это была попытка, и подставлять вместо неё следующую
    было бы ровно тем ускорением, от которого мы защищаемся. Поэтому берётся
    `attempted_at`, когда `dispatched_at` пуст, а состояние не фильтруется:
    задача без обеих отметок до моста не дошла и в счёт не идёт сама собой.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    actions = _cadence_actions(cadence)
    placeholders = ",".join("?" * len(actions))
    row = store.one(
        "SELECT count(*) AS n FROM tasks "
        f"WHERE account_id = ? AND action IN ({placeholders}) "
        "  AND substr(COALESCE(dispatched_at, attempted_at), 1, 10) = ?",
        (int(account_id), *actions, today),
    )
    return int(row["n"])


def last_attempt_at(
    store: Store, account_id: int, cadence: str = CADENCE_OUTREACH
) -> datetime | None:
    """Когда аккаунт в последний раз обращался к мосту за действием класса.

    Читаем отметку попытки, а не `scheduled_at`: пол меряется от факта
    обращения. Задача, пролежавшая в плане сутки, не даёт аккаунту права
    отправить две подряд, а неудачная попытка не даёт права попробовать
    снова немедленно.

    Классы смотрят на разное, и это намеренно. Рассылка меряет паузу от
    прошлой **рассылки**: ответ человеку не повод откладывать кампанию на
    полчаса. Ответ меряет от **любого** видимого действия: тут пол короткий,
    и нужен он ровно затем, чтобы аккаунт не выпустил два сообщения одной
    секундой — а с точки зрения Telegram аккаунт один, чем бы мы его ни
    занимали. Чтение меряет только от чтения: сообщение и resolve упираются
    в разные лимиты, и заставлять разведку ждать отправку не за что.
    """
    actions = VISIBLE_ACTIONS if cadence == CADENCE_REPLY else _cadence_actions(cadence)
    placeholders = ",".join("?" * len(actions))
    row = store.one(
        "SELECT max(COALESCE(dispatched_at, attempted_at)) AS last FROM tasks "
        f"WHERE account_id = ? AND action IN ({placeholders}) "
        "  AND COALESCE(dispatched_at, attempted_at) IS NOT NULL",
        (int(account_id), *actions),
    )
    if not row or not row["last"]:
        return None
    try:
        return datetime.fromisoformat(str(row["last"]))
    except ValueError:
        return None


#: Ключ в `state`, хранящий момент, раньше которого не выпускает НИКТО.
GLOBAL_NEXT_KEY = "global_next_visible_at"
GLOBAL_NEXT_REPLY_KEY = "global_next_reply_at"
GLOBAL_NEXT_READ_KEY = "global_next_read_at"


def _account_read_key(account_id: int) -> str:
    return f"next_read_at:{int(account_id)}"


def account_next_read_at(store: Store, account_id: int) -> datetime | None:
    """Когда этому аккаунту снова можно читать.

    Момент разыгрывается на выпуске и запоминается, а не выводится из времени
    последней попытки. Разница видна на долге: аккаунт стоял на паузе, задачи
    просрочены — и правило «не чаще, чем раз в N секунд» выпустило бы их ровным
    шагом по нижней границе. Запомненный момент с разыгранной паузой держит
    ритм неровным всегда, а не только когда очередь пуста.
    """
    raw = store.get_state(_account_read_key(account_id), "") or ""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _plan_account_read_pause(
    settings: Settings, account_id: int, rng: random.Random | None = None
) -> str:
    """Разыграть следующую паузу аккаунта: `interval` + `0..jitter`."""
    limits = settings.limits
    low = max(0, int(limits.read_per_account_interval_sec))
    span = max(0, int(limits.read_per_account_interval_jitter_sec))
    delay = low + (rng or random).randint(0, span)
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def global_next_at(
    store: Store, cadence: str = CADENCE_OUTREACH
) -> datetime | None:
    """Момент, раньше которого ни один аккаунт не выпускает действие класса."""
    raw = store.get_state(_global_key(cadence), "") or ""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _global_key(cadence: str) -> str:
    """Своя пауза флота на класс: очередь ответов не стоит за рассылкой."""
    if cadence == CADENCE_REPLY:
        return GLOBAL_NEXT_REPLY_KEY
    if cadence == CADENCE_READ:
        return GLOBAL_NEXT_READ_KEY
    return GLOBAL_NEXT_KEY


def _plan_global_pause(
    settings: Settings,
    rng: random.Random | None = None,
    cadence: str = CADENCE_OUTREACH,
) -> str:
    """Отложить следующий выпуск флота на случайную паузу из диапазона."""
    limits = settings.limits
    if cadence == CADENCE_REPLY:
        low = max(0, int(limits.reply_global_interval_min_sec))
        high = max(low, int(limits.reply_global_interval_max_sec))
    elif cadence == CADENCE_READ:
        low = max(0, int(limits.read_global_interval_min_sec))
        high = max(low, int(limits.read_global_interval_max_sec))
    else:
        low = max(0, int(limits.global_visible_interval_min_sec))
        high = max(low, int(limits.global_visible_interval_max_sec))
    delay = (rng or random).randint(low, high)
    moment = datetime.now(timezone.utc) + timedelta(seconds=delay)
    return moment.isoformat()


def contact_touch(store: Store, contact_id: str) -> dict | None:
    """Писали ли мы этому контакту раньше — в любой кампании и с любого аккаунта."""
    row = store.one(
        "SELECT contact_id, first_sent_at, last_sent_at, sent_count, "
        "       last_account_id, last_campaign_id "
        "FROM contact_touches WHERE contact_id = ?",
        (str(contact_id),),
    )
    return dict(row) if row else None


def record_contact_touch(store: Store, task: dict) -> None:
    """Отметить касание контакта. Повторный вызов только наращивает счётчик."""
    stamp = now()
    store.execute(
        "INSERT INTO contact_touches(contact_id, first_sent_at, last_sent_at, "
        "  sent_count, last_account_id, last_campaign_id, last_task_id) "
        "VALUES(?,?,?,1,?,?,?) "
        "ON CONFLICT(contact_id) DO UPDATE SET "
        "  last_sent_at = excluded.last_sent_at, "
        "  sent_count = sent_count + 1, "
        "  last_account_id = excluded.last_account_id, "
        "  last_campaign_id = excluded.last_campaign_id, "
        "  last_task_id = excluded.last_task_id",
        (str(task["contact_id"]), stamp, stamp, int(task["account_id"]),
         task.get("campaign_id"), task.get("id")),
    )


def campaign_allows_repeat(task: dict) -> bool:
    """Разрешила ли кампания писать тем, кого уже касались.

    Повтор — это осознанное решение (догоняющая волна, другой оффер), поэтому
    он включается явным флагом кампании, а не выводится из того, что задача
    почему-то оказалась в очереди.
    """
    return bool(task.get("campaign_allow_repeat"))


def inside_send_window(
    settings: Settings,
    moment: datetime | None = None,
    cadence: str = CADENCE_OUTREACH,
) -> bool:
    """Идёт ли сейчас окно этого класса в рабочей таймзоне.

    У каждого класса оно своё, и разница не косметическая. Ответ идёт
    круглосуточно: он про то, когда живой человек мог бы ответить, а
    написавший в субботу вечером ждёт ответа, а не «мы вам в понедельник».
    Разведка — 06:00–23:00 без выходных: собеседник её не видит, но аккаунт,
    перебирающий имена в четыре утра, виден Telegram, а выходных у поиска
    нет. Рассылка — самое узкое окно, рабочие дни и часы.
    """
    limits = settings.limits
    if cadence == CADENCE_REPLY:
        start = limits.reply_window_start_hour
        end = limits.reply_window_end_hour
        weekdays = limits.reply_weekdays
    elif cadence == CADENCE_READ:
        start = limits.read_window_start_hour
        end = limits.read_window_end_hour
        weekdays = limits.read_weekdays
    else:
        start = limits.send_window_start_hour
        end = limits.send_window_end_hour
        weekdays = limits.send_weekdays
    if not start and not end:
        return True
    moment = moment or datetime.now(timezone.utc)
    try:
        local = moment.astimezone(ZoneInfo(settings.timezone))
    except Exception:  # noqa: BLE001 — нет tzdata: не мешаем работе
        return True
    if local.weekday() not in weekdays:
        return False
    return start <= local.hour < end


@dataclass(frozen=True)
class Pace:
    """Числа одного класса темпа и слова, которыми о нём говорят в отказе."""

    daily_cap: int
    interval_sec: int
    global_low: int
    global_high: int
    #: «выпустил 13 ответов за сегодня»
    noun: str
    #: «аккаунт отправлял меньше 60 с назад»
    verb: str
    #: «сейчас вне окна разведки»
    window_noun: str
    window_start: int
    window_end: int


def _pace(settings: Settings, cadence: str) -> Pace:
    """Собрать темп класса. Одно место, где числа сходятся со словами."""
    limits = settings.limits
    if cadence == CADENCE_REPLY:
        return Pace(
            daily_cap=int(limits.reply_per_account_daily),
            interval_sec=int(limits.reply_per_account_interval_sec or 0),
            global_low=int(limits.reply_global_interval_min_sec),
            global_high=int(limits.reply_global_interval_max_sec),
            noun="ответов", verb="отвечал", window_noun="ответов",
            window_start=int(limits.reply_window_start_hour),
            window_end=int(limits.reply_window_end_hour),
        )
    if cadence == CADENCE_READ:
        return Pace(
            daily_cap=int(limits.read_per_account_daily),
            interval_sec=int(limits.read_per_account_interval_sec or 0),
            global_low=int(limits.read_global_interval_min_sec),
            global_high=int(limits.read_global_interval_max_sec),
            noun="чтений метаданных", verb="читал", window_noun="разведки",
            window_start=int(limits.read_window_start_hour),
            window_end=int(limits.read_window_end_hour),
        )
    return Pace(
        daily_cap=int(limits.per_account_daily_visible),
        interval_sec=int(limits.per_account_visible_interval_sec or 0),
        global_low=int(limits.global_visible_interval_min_sec),
        global_high=int(limits.global_visible_interval_max_sec),
        noun="видимых действий", verb="отправлял", window_noun="отправки",
        window_start=int(limits.send_window_start_hour),
        window_end=int(limits.send_window_end_hour),
    )


def preflight(
    store: Store,
    task: dict,
    settings: Settings,
    *,
    spent: dict[int, int] | None = None,
    recent: dict[int, datetime] | None = None,
    touched: set[str] | None = None,
    enforce_global_pause: bool = True,
) -> catalog.Action:
    """Все проверки, которые дешевле сделать до обращения к базе.

    ``spent``, ``recent``, ``global_recent`` и ``touched`` нужны только
    предпросмотру: там ничего не пишется в базу, и без накопительных счётчиков
    лимит, паузы и дедуп выглядели бы замороженными — предпросмотр показал бы
    весь батч как готовый к выпуску. В боевом цикле их не передают: там после
    каждой попытки коммитятся отметки, и запрос к базе сам по себе актуален.
    """
    if task.get("campaign_status") not in (None, "active"):
        raise DispatchBlocked(
            f"кампания в статусе {task['campaign_status']}, нужен active"
        )

    account_id = int(task["account_id"])
    account = accounts_mod.get(store, account_id)
    if account is None:
        raise DispatchBlocked(f"нет аккаунта {account_id} в реестре")

    ok, why = accounts_mod.usable(account, task["action"])
    if not ok:
        raise DispatchBlocked(why)

    contact = store.one(
        "SELECT opted_out FROM contacts WHERE id = ?", (task["contact_id"],)
    )
    if contact and contact["opted_out"]:
        raise DispatchBlocked("контакт отказался от коммуникации")

    action = catalog.validate(
        task["action"], task["params"], roles=account["roles"],
        allowed_actions=account["allowed_actions"] or None,
    )

    if task["mode"] == "immediate" and action.risk in catalog.IMMEDIATE_GATED_RISKS:
        if not account["allow_immediate"]:
            raise DispatchBlocked(
                "mode=immediate для видимого действия требует "
                "allow_immediate_visible_actions=true у аккаунта в Radar"
            )

    cadence = cadence_of(task)
    pace = _pace(settings, cadence)

    # Окно проверяется ещё раз здесь, а не только при планировании: задача
    # могла пролежать в очереди до ночи из-за паузы или сбоя.
    if (action.visible or cadence == CADENCE_READ) and not inside_send_window(
        settings, cadence=cadence
    ):
        raise DispatchBlocked(
            f"сейчас вне окна {pace.window_noun} "
            f"({pace.window_start}:00–{pace.window_end}:00 {settings.timezone})"
        )

    if action.visible:
        # Одному человеку — одно первое касание, даже если кампаний несколько.
        # Уникальность в tasks держит только пару (кампания, контакт), поэтому
        # второй сегмент, пересекающийся с первым, без этой проверки написал бы
        # тем же людям повторно и, скорее всего, с другого аккаунта.
        #
        # Ответ под эту защиту не подпадает: она про первое касание, а человек,
        # которому мы отвечаем, написал нам сам и ждёт.
        if action.name not in replies.REPLY_ACTIONS and not campaign_allows_repeat(task):
            contact_id = str(task["contact_id"])
            touch = contact_touch(store, contact_id)
            if touch is None and touched is not None and contact_id in touched:
                touch = {"last_sent_at": "в этом же батче", "last_account_id": "—"}
            if touch is not None:
                raise DispatchBlocked(
                    f"контакту уже писали ({touch['last_sent_at']}, аккаунт "
                    f"{touch['last_account_id']}); чтобы это было намеренно, "
                    "поставьте кампании allow_repeat_contacts"
                )

    # Дальше — темп. Он касается и видимых действий, и чтения: у первых цена
    # ошибки — человек, получивший залп, у второго — FLOOD_WAIT на resolve.
    # Не касается только `soft`: оно едет прицепом к своему действию.
    if action.visible or cadence == CADENCE_READ:
        # Пауза поперёк флота: аккаунтов много, и без неё их отправки
        # складываются в залп, даже когда каждый по отдельности выдержал свой
        # интервал. Предпросмотр её не применяет — он отвечает на вопрос «что
        # уйдёт за прогон», а уйдёт всё, просто с паузами между отправками.
        blocked_until = (global_next_at(store, cadence)
                         if enforce_global_pause else None)
        if blocked_until is not None:
            wait = (blocked_until - datetime.now(timezone.utc)).total_seconds()
            if wait > 0:
                raise DispatchTooEarly(
                    f"по флоту только что {pace.verb} — ждём ещё {int(wait) + 1} с "
                    f"(пауза {pace.global_low}–{pace.global_high} с)",
                    wait_seconds=int(wait) + 1,
                )

        already = sent_today(store, account_id, cadence)
        already += int((spent or {}).get(account_id, 0))
        if already >= pace.daily_cap:
            raise DispatchBlocked(
                f"аккаунт уже выпустил {already} {pace.noun} за сегодня "
                f"(лимит {pace.daily_cap})"
            )

        # У чтения пауза разыграна заранее и запомнена по аккаунту. Проверка
        # ниже (от последней попытки) осталась как нижняя граница: она держит
        # и тогда, когда запомненного момента ещё нет — первый прогон после
        # обновления, чужая правка базы.
        if cadence == CADENCE_READ:
            allowed = account_next_read_at(store, account_id)
            if allowed is not None:
                wait = int((allowed - datetime.now(timezone.utc)).total_seconds())
                if wait > 0:
                    raise DispatchBlocked(
                        f"аккаунт читал недавно — следующее чтение через {wait} с"
                    )

        # Пауза между отправками проверяется здесь, а не только при
        # планировании. Планировщик раскладывает по слотам, но в базу задачи
        # попадают и мимо него: повторный plan, вторая кампания на тот же
        # сегмент, импорт, правка руками. Пол должен держать в любом из этих
        # случаев, иначе весь пакет уедет одной секундой.
        if pace.interval_sec > 0:
            last = last_attempt_at(store, account_id, cadence)
            simulated = (recent or {}).get(account_id)
            if simulated is not None and (last is None or simulated > last):
                last = simulated
            if last is not None:
                moment = datetime.now(timezone.utc)
                wait = int(
                    (last + timedelta(seconds=pace.interval_sec) - moment)
                    .total_seconds()
                )
                if wait > 0:
                    raise DispatchBlocked(
                        f"аккаунт {pace.verb} меньше {pace.interval_sec} с назад — "
                        f"ждём ещё {wait} с"
                    )

    return action


def _claim(store: Store, task: dict) -> str:
    """Закрепить за задачей UUID. Если её уже забрали — вернуть чужой.

    Атомарность здесь важнее краткости: без ``WHERE request_id IS NULL`` два
    процесса выдали бы одной задаче два UUID, а это две отправки.
    """
    existing = task.get("request_id")
    if existing:
        return str(existing)

    request_id = new_request_id()
    cursor = store.execute(
        "UPDATE tasks SET request_id = ?, updated_at = ? "
        "WHERE id = ? AND request_id IS NULL",
        (request_id, now(), task["id"]),
    )
    store.commit()
    if cursor.rowcount:
        return request_id

    row = store.one("SELECT request_id FROM tasks WHERE id = ?", (task["id"],))
    return str(row["request_id"]) if row and row["request_id"] else request_id


async def dispatch(
    store: Store,
    settings: Settings,
    *,
    campaign_id: str | None = None,
    cadence: str | None = None,
    limit: int | None = None,
    confirm: bool = False,
    actor: str = "cli",
) -> dict:
    """Выпустить созревшие задачи в Radar.

    Без `confirm` и без файла ARMED ничего не отправляется — возвращается
    предпросмотр, ровно тот же список, который ушёл бы в боевом режиме.

    ``cadence`` сужает выпуск до одного класса. Это нужно таймеру разведки:
    без сужения он выпускал бы заодно всё созревшее в рассылочных кампаниях,
    а рассылка здесь по решению владельца остаётся на ручном управлении.
    """
    limit = int(limit or settings.limits.dispatch_batch)
    armed = settings.armed
    if confirm and armed:
        with exclusive(settings, wait_sec=LOCK_WAIT_SEC):
            return await _dispatch_armed(
                store, settings, campaign_id=campaign_id, cadence=cadence,
                limit=limit, actor=actor,
            )
    return _preview(store, settings, campaign_id=campaign_id, cadence=cadence,
                    limit=limit, armed=armed, confirmed=confirm)


def _mark_attempt(
    store: Store, task: dict, settings: Settings, *, visible: bool
) -> None:
    """Отметить обращение к мосту и сдвинуть паузу флота.

    Коммитим сразу: если процесс упадёт следующей строкой, отметка о попытке
    и пауза должны пережить падение — иначе перезапуск выпустит следующую
    задачу немедленно.
    """
    store.execute(
        "UPDATE tasks SET attempted_at = ?, updated_at = ? WHERE id = ?",
        (now(), now(), task["id"]),
    )
    cadence = cadence_of(task)
    if visible or cadence == CADENCE_READ:
        store.set_state(_global_key(cadence),
                        _plan_global_pause(settings, cadence=cadence))
    if cadence == CADENCE_READ:
        account_id = int(task["account_id"])
        store.set_state(_account_read_key(account_id),
                        _plan_account_read_pause(settings, account_id))
    store.commit()


def _queue(store: Store, campaign_id: str | None, limit: int,
           cadence: str | None = None) -> list[dict]:
    pending = orphaned(store, campaign_id=campaign_id, cadence=cadence)
    fresh = due_tasks(store, campaign_id=campaign_id, cadence=cadence,
                      limit=limit)
    seen = {task["id"] for task in pending}
    # Оборванные с прошлого раза идут первыми: их надо доиграть тем же UUID.
    return (pending + [t for t in fresh if t["id"] not in seen])[:limit]


def _preview(
    store: Store, settings: Settings, *, campaign_id: str | None, limit: int,
    armed: bool, confirmed: bool, cadence: str | None = None,
) -> dict:
    spent: dict[int, int] = {}
    recent: dict[int, datetime] = {}
    touched: set[str] = set()
    ready: list[dict] = []
    blocked: list[dict] = []
    for task in _queue(store, campaign_id, limit, cadence):
        try:
            action = preflight(
                store, task, settings, spent=spent, recent=recent,
                touched=touched, enforce_global_pause=False,
            )
        except DispatchBlocked as exc:
            blocked.append({"task": task["id"], "why": str(exc)})
            continue
        if action.visible or cadence_of(task) == CADENCE_READ:
            account_id = int(task["account_id"])
            spent[account_id] = spent.get(account_id, 0) + 1
            # Весь батч ушёл бы одним прогоном, то есть «сейчас».
            recent[account_id] = datetime.now(timezone.utc)
            if action.visible:
                touched.add(str(task["contact_id"]))
        ready.append(task)

    return {
        "armed": armed,
        "confirmed": confirmed,
        "would_dispatch": len(ready),
        "blocked": blocked,
        "tasks": [_preview_row(store, t, settings.timezone) for t in ready],
        "dispatched": 0,
        "dry_run": True,
    }


async def _dispatch_armed(
    store: Store, settings: Settings, *, campaign_id: str | None, limit: int,
    actor: str, cadence: str | None = None,
) -> dict:
    queue = _queue(store, campaign_id, limit, cadence)
    sent = 0
    blocked: list[dict] = []
    failed: list[dict] = []
    deferred: list[dict] = []

    async with RadarBridge(settings.dsn) as bridge:
        for task in queue:
            # Проверяем непосредственно перед каждой отправкой, а не заранее
            # для всего батча: иначе дневной лимит внутри прогона заморожен.
            try:
                action = preflight(store, task, settings)
            except DispatchTooEarly as exc:
                # Пауза флота — это «пока рано». Ждём её и пробуем ещё раз:
                # пропустить задачу означало бы выпускать по одной за прогон.
                if exc.wait_seconds > MAX_INLINE_WAIT_SEC:
                    deferred.append({"task": task["id"], "why": str(exc)})
                    break
                await asyncio.sleep(exc.wait_seconds)
                try:
                    action = preflight(store, task, settings)
                except DispatchBlocked as retry_exc:
                    blocked.append({"task": task["id"], "why": str(retry_exc)})
                    continue
            except DispatchBlocked as exc:
                blocked.append({"task": task["id"], "why": str(exc)})
                continue

            request_id = _claim(store, task)
            expires = (
                datetime.fromisoformat(task["expires_at"])
                if task.get("expires_at") else None
            )
            request = build_request(
                # Именно проводное имя: `reply_channel_dm` — наше, для темпа и
                # бюджета, а Radar такого действия не знает.
                action=action.wire_name,
                params=task["params"],
                mode=task["mode"],
                request_id=request_id,
                expires_at=expires,
                external_job_id=task["campaign_id"],
                external_conversation_id=task["contact_id"],
            )

            try:
                command_id = await bridge.enqueue(int(task["account_id"]), request)
            except QueueFull as exc:
                # До вставки дело не дошло: команда не создана, темп не
                # израсходован. Остальной батч ждать смысла нет.
                deferred.append({"task": task["id"], "why": f"очередь моста полна: {exc}"})
                _note(store, task["id"], str(exc))
                break
            except BridgeRejected as exc:
                # Детерминированный отказ: повтор даст тот же результат.
                # Попытка всё равно засчитана — иначе следующая задача этого
                # аккаунта уехала бы немедленно, превратив череду отказов в
                # ускорение ровно там, где что-то уже пошло не так.
                _mark_attempt(store, task, settings, visible=action.visible)
                failed.append({"task": task["id"], "why": str(exc)})
                store.execute(
                    "UPDATE tasks SET state='blocked', error_code='bridge_rejected', "
                    "error_message=?, updated_at=? WHERE id=?",
                    (str(exc)[:500], now(), task["id"]),
                )
                store.commit()
                continue
            except (BridgeUnknown, BridgeError) as exc:
                # Могло уехать, а могло и нет. Оставляем задачу запланированной
                # вместе с её UUID — следующий прогон переиграет ТЕМ ЖЕ UUID,
                # и мост вернёт прежний command_id, если команда всё-таки есть.
                # Касание отмечаем: раз исход неизвестен, считаем, что дошло.
                _mark_attempt(store, task, settings, visible=action.visible)
                if action.visible:
                    record_contact_touch(store, task)
                deferred.append({"task": task["id"], "why": str(exc)})
                _note(store, task["id"], str(exc))
                continue

            _mark_attempt(store, task, settings, visible=action.visible)
            store.execute(
                "UPDATE tasks SET state='queued', command_id=?, error_message=NULL, "
                "dispatched_at=?, updated_at=? WHERE id=?",
                (int(command_id), now(), now(), task["id"]),
            )
            store.execute(
                "UPDATE contacts SET status = CASE WHEN status = 'new' "
                "THEN 'contacted' ELSE status END, updated_at = ? WHERE id = ?",
                (now(), task["contact_id"]),
            )
            if action.visible:
                record_contact_touch(store, task)
            store.commit()
            sent += 1

    store.log(
        actor, "dispatch", campaign_id or "*",
        f"sent={sent} blocked={len(blocked)} deferred={len(deferred)} "
        f"failed={len(failed)}",
    )
    store.commit()
    return {
        "armed": True,
        "confirmed": True,
        "dispatched": sent,
        "blocked": blocked,
        "deferred": deferred,
        "failed": failed,
        "dry_run": False,
    }


def _note(store: Store, task_id: str, message: str) -> None:
    store.execute(
        "UPDATE tasks SET error_message = ?, updated_at = ? WHERE id = ?",
        (message[:500], now(), task_id),
    )
    store.commit()


def unblock(store: Store, task_ids: list[str], *, actor: str = "cli") -> int:
    """Вернуть заблокированные задачи в план — после разбора причины."""
    changed = 0
    for task_id in task_ids:
        cursor = store.execute(
            "UPDATE tasks SET state='planned', error_code=NULL, error_message=NULL, "
            "updated_at=? WHERE id=? AND state='blocked'",
            (now(), task_id),
        )
        changed += cursor.rowcount
    store.log(actor, "tasks.unblock", "", f"unblocked={changed}")
    store.commit()
    return changed


def _preview_row(store: Store, task: dict, tz_name: str = "Europe/Moscow") -> dict:
    contact = store.one(
        "SELECT username, display_name FROM contacts WHERE id = ?",
        (task["contact_id"],),
    )
    account = store.one(
        "SELECT label FROM accounts WHERE id = ?", (int(task["account_id"]),)
    )
    text = str(task["params"].get("text") or "")
    return {
        "task": task["id"],
        "when": report.local_time(task["scheduled_at"], tz_name),
        "account": f"{task['account_id']} {account['label'] if account else ''}".strip(),
        "target": (contact["username"] if contact else None) or task["contact_id"],
        "action": task["action"],
        "mode": task["mode"],
        "risk": catalog.ACTIONS[task["action"]].risk,
        "preview": (text[:70] + "…") if len(text) > 70 else text,
    }


def arm(settings: Settings, on: bool, *, actor: str = "cli") -> str:
    """Включить или выключить боевой режим."""
    path = settings.armed_file
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"armed by {actor} at {now()}\n"
            "Пока этот файл существует, dispatch --confirm реально ставит\n"
            "команды в Radar. Удалите файл, чтобы вернуться к предпросмотру.\n",
            encoding="utf-8",
        )
        return f"боевой режим ВКЛЮЧЁН: {path}"
    if path.exists():
        path.unlink()
    return f"боевой режим выключен (файла {path} нет)"
