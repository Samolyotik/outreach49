"""Разведка: свой темп у чтения и читаемый результат.

До этих правок read-действия проходили `preflight` вообще без проверок — ни
дневного лимита, ни пола, ни паузы флота. Со стороны собеседника там и правда
ничего не видно, но `search_public_chat` — это resolve имени, у которого свой
лимит в Telegram, а исполнитель Radar берёт по команде за тик. Пачка созревших
задач ушла бы со скоростью команда в секунду на аккаунт.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import catalog, config, dispatcher, entities, planner, research  # noqa: E402
from bridge49.config import Limits, Settings  # noqa: E402
from bridge49.store import Store, new_id, now  # noqa: E402

SNAPSHOT = [
    {
        "id": 801, "label": "reader-one", "program_code": "TGR11",
        "runtime_state": "running",
        "outreach": {
            "enabled": True, "roles": ["source_reader"], "publish_inbound": False,
            "allow_immediate_visible_actions": True,
            "allowed_actions": [
                "check_channel_dm_metadata", "check_public_chat_metadata",
                "get_supergroup", "resolve_channel_dm", "search_public_chat",
            ],
        },
    },
]


#: Понедельник, 15:00 по Москве. Время заморожено не ради окна (у разведки его
#: нет), а ради дневного счётчика и пола: они меряются от «сейчас», и без
#: заморозки проверка, начатая в 23:59:59, считала бы завтрашний день.
MIDDAY = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


class ReadCadenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.store = Store(tmp / "b.sqlite")
        accounts_mod.sync(self.store, SNAPSHOT)
        limits = Limits()
        config.clamp(limits)
        self.settings = Settings(
            home=tmp, db_path=tmp / "b.sqlite", dsn=None, limits=limits,
            timezone="Europe/Moscow",
        )
        self.clock = frozen_clock(MIDDAY)
        self.clock.start()
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, segment, mode, status, "
            "daily_cap, per_account_daily_cap, params, ttl_hours, created_at, "
            "updated_at) VALUES('recon','Разведка','check_channel_dm_metadata',"
            "'sources','immediate','active',500,500,'{}',48,?,?)",
            (now(), now()),
        )
        self.store.commit()

    def tearDown(self):
        self.clock.stop()
        self.store.close()
        self.tmp.cleanup()

    def add_task(self, action: str, *, state: str = "done",
                 when: datetime | None = None, result: dict | None = None) -> dict:
        contact = entities.add_contact(
            self.store, username=f"c{new_id('c')[:6]}", kind="channel",
            segment="sources", actor="test",
        )
        task_id = new_id("task")
        stamp = (when or MIDDAY).isoformat()
        self.store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, action, "
            "params, mode, scheduled_at, state, dispatched_at, finished_at, "
            "result, created_at, updated_at) "
            "VALUES(?,'recon',?,801,?,'{}','immediate',?,?,?,?,?,?,?)",
            (task_id, contact["id"], action, stamp, state, stamp, stamp,
             json.dumps(result or {}), now(), now()),
        )
        self.store.commit()
        return {"id": task_id, "contact": contact}

    def pending(self, action: str = "check_channel_dm_metadata") -> dict:
        """Задача, которую диспетчер прямо сейчас попробует выпустить."""
        contact = entities.add_contact(
            self.store, username=f"t{new_id('c')[:6]}", kind="channel",
            segment="sources", actor="test",
        )
        return {
            "id": new_id("task"), "campaign_id": "recon",
            "contact_id": contact["id"], "account_id": 801, "action": action,
            "params": {"username": contact["username"]}, "mode": "immediate",
            "campaign_status": "active", "expires_at": None,
        }

    # -- класс --------------------------------------------------------------

    def test_reading_is_its_own_class(self):
        self.assertEqual(
            dispatcher.cadence_of({"action": "check_channel_dm_metadata"}),
            dispatcher.CADENCE_READ,
        )
        self.assertEqual(
            dispatcher.cadence_of({"action": "send_private_dm"}),
            dispatcher.CADENCE_OUTREACH,
        )

    def test_echo_is_not_counted_as_reading(self):
        """`command_dry_run` не выходит за пределы Radar — бюджет не тратит."""
        self.assertNotIn("command_dry_run", catalog.READ_ACTIONS)
        self.assertNotIn("gateway_capabilities", catalog.READ_ACTIONS)
        self.assertIn("search_public_chat", catalog.READ_ACTIONS)

    # -- бюджеты ------------------------------------------------------------

    def test_reading_does_not_spend_the_outreach_budget(self):
        for _ in range(4):
            self.add_task("check_channel_dm_metadata")

        self.assertEqual(
            dispatcher.sent_today(self.store, 801, dispatcher.CADENCE_READ), 4)
        self.assertEqual(
            dispatcher.sent_today(self.store, 801,
                                  dispatcher.CADENCE_OUTREACH), 0)

    def test_a_send_does_not_hold_back_a_read(self):
        """Отправка и resolve упираются в разные лимиты Telegram."""
        self.add_task("send_private_dm")

        self.assertIsNone(dispatcher.last_attempt_at(
            self.store, 801, dispatcher.CADENCE_READ))

    # -- пол ----------------------------------------------------------------

    def test_the_daily_cap_stops_the_sweep(self):
        limits = self.settings.limits
        limits.read_per_account_daily = 3
        for _ in range(3):
            self.add_task("check_channel_dm_metadata")

        with self.assertRaises(dispatcher.DispatchBlocked) as caught:
            dispatcher.preflight(self.store, self.pending(), self.settings)
        self.assertIn("чтений метаданных", str(caught.exception))

    def test_the_floor_between_reads_holds(self):
        """Главное, ради чего всё затевалось: пачка не уедет одной секундой."""
        self.add_task("check_channel_dm_metadata", when=MIDDAY)

        with self.assertRaises(dispatcher.DispatchBlocked) as caught:
            dispatcher.preflight(self.store, self.pending(), self.settings)
        self.assertIn("читал меньше", str(caught.exception))

    def test_an_old_read_does_not_hold_anything_back(self):
        self.add_task("check_channel_dm_metadata",
                      when=MIDDAY - timedelta(hours=2))

        action = dispatcher.preflight(self.store, self.pending(), self.settings)
        self.assertEqual(action.name, "check_channel_dm_metadata")

    def test_the_fleet_pause_is_kept_apart_from_the_others(self):
        self.store.set_state(
            dispatcher.GLOBAL_NEXT_KEY,
            (MIDDAY + timedelta(hours=1)).isoformat(),
        )

        self.assertIsNone(dispatcher.global_next_at(
            self.store, dispatcher.CADENCE_READ))
        self.assertIsNotNone(dispatcher.global_next_at(
            self.store, dispatcher.CADENCE_OUTREACH))

    def test_the_fleet_pause_applies_to_reading_too(self):
        self.store.set_state(
            dispatcher.GLOBAL_NEXT_READ_KEY,
            (MIDDAY + timedelta(seconds=40)).isoformat(),
        )

        with self.assertRaises(dispatcher.DispatchTooEarly):
            dispatcher.preflight(self.store, self.pending(), self.settings)

    def test_reading_runs_round_the_clock(self):
        """Окна у разведки нет — решение владельца от 03.08.2026.

        У прежнего контура окно было (06:00–23:00), и довод за него понятен:
        аккаунт, перебирающий имена ночью, на живого человека не похож. Довод
        против оказался сильнее — чтение никому не видно, ночь это треть
        суток, а от лимита resolve защищает скорость, а не расписание.
        """
        self.settings.limits.read_per_account_interval_sec = 0
        night = datetime(2026, 8, 2, 1, 30, tzinfo=timezone.utc)  # 04:30 МСК вс

        with frozen_clock(night):
            action = dispatcher.preflight(
                self.store, self.pending(), self.settings)

        self.assertEqual(action.risk, catalog.RISK_READ)

    def test_the_numbers_are_the_ones_the_old_contour_proved(self):
        """Профиль `standard`, роль `source_reader`, прежний контур.

        Числа сверены с `configs/account_task_speeds.json` на боевом сервере и
        с тем, как контур ходил на самом деле: 17.07 пять читателей сделали
        415 проверок за девять часов — одна на 76 секунд по флоту. Свои числа
        здесь заводить нельзя: RPC те же, лимит тот же.
        """
        limits = Limits()

        self.assertEqual(limits.read_per_account_daily, 100)
        self.assertEqual(limits.read_per_account_interval_sec, 240)
        self.assertEqual(
            limits.read_per_account_interval_sec
            + limits.read_per_account_interval_jitter_sec,
            360,
        )
        self.assertEqual(limits.read_global_interval_min_sec, 60)
        self.assertEqual(limits.read_global_interval_max_sec, 90)
        # Окно — единственное, что мы у них НЕ взяли: у них 06:00-23:00,
        # у нас круглосуточно по решению владельца от 03.08.2026.
        self.assertEqual(limits.read_window_start_hour, 0)
        self.assertEqual(limits.read_window_end_hour, 24)
        # Огибающая всех трёх их профилей: быстрее «fast» не пускаем никого.
        self.assertEqual(config.HARD_MAX_READ_DAILY, 100)
        self.assertEqual(config.HARD_MIN_READ_INTERVAL_SEC, 240)
        self.assertEqual(config.HARD_MIN_READ_GLOBAL_INTERVAL_SEC, 60)

    def test_the_floor_is_clamped_to_something_sane(self):
        limits = Limits()
        limits.read_per_account_daily = 10_000
        limits.read_per_account_interval_sec = 0

        notes = config.clamp(limits)

        self.assertEqual(limits.read_per_account_daily,
                         config.HARD_MAX_READ_DAILY)
        self.assertEqual(limits.read_per_account_interval_sec,
                         config.HARD_MIN_READ_INTERVAL_SEC)
        self.assertEqual(len(notes), 2, notes)

    # -- сужение по классу ---------------------------------------------------

    def test_the_read_filter_leaves_the_campaign_alone(self):
        """Таймер разведки не должен выпускать рассылку.

        Оба класса кладут задачи в одну таблицу, и `dispatch` без сужения
        забрал бы созревшее из рассылочной кампании заодно.
        """
        self.store.execute(
            "INSERT INTO campaigns(id, name, action, segment, mode, status, "
            "daily_cap, per_account_daily_cap, params, ttl_hours, created_at, "
            "updated_at) VALUES('blast','Рассылка','send_private_dm',"
            "'sources','lottery','active',50,12,'{}',48,?,?)",
            (now(), now()),
        )
        past = (MIDDAY - timedelta(minutes=5)).isoformat()
        for campaign, action in (("recon", "check_channel_dm_metadata"),
                                 ("blast", "send_private_dm")):
            contact = entities.add_contact(
                self.store, username=f"x{new_id('c')[:6]}", segment="sources",
                actor="test",
            )
            self.store.execute(
                "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
                "action, params, mode, scheduled_at, state, created_at, "
                "updated_at) VALUES(?,?,?,801,?,'{}','immediate',?,'planned',?,?)",
                (new_id("task"), campaign, contact["id"], action, past,
                 now(), now()),
            )
        self.store.commit()

        both = dispatcher.due_tasks(self.store)
        only_read = dispatcher.due_tasks(
            self.store, cadence=dispatcher.CADENCE_READ)

        self.assertEqual(len(both), 2)
        self.assertEqual([t["action"] for t in only_read],
                         ["check_channel_dm_metadata"])

    # -- кому поручаем -------------------------------------------------------

    def test_a_campaign_can_narrow_the_pool_to_readers(self):
        """Допуск и уместность — разные вещи.

        `check_channel_dm_metadata` контракт разрешает и отправителям каналов.
        Без сужения план 03.08 разложил разведку каталога на тридцать аккаунтов
        вместо тринадцати: отправители тратили бы свой лимит resolve на чужую
        работу.
        """
        accounts_mod.sync(self.store, [{
            "id": 802, "label": "sender-one", "runtime_state": "running",
            "outreach": {
                "enabled": True, "roles": ["channel_sender"],
                "allow_immediate_visible_actions": True,
                "allowed_actions": ["check_channel_dm_metadata"],
            },
        }])
        for index in range(4):
            entities.add_contact(
                self.store, username=f"src{index}", kind="channel",
                segment="sources", actor="test",
            )

        wide = planner.plan(self.store, "recon", limits=self.settings.limits,
                            dry_run=True)
        self.store.execute(
            "UPDATE campaigns SET roles = ? WHERE id = 'recon'",
            ('["source_reader"]',),
        )
        self.store.commit()
        narrow = planner.plan(self.store, "recon", limits=self.settings.limits,
                              dry_run=True)

        self.assertEqual(wide["pool"], 2)
        self.assertEqual(narrow["pool"], 1)
        self.assertEqual({t["account_id"] for t in narrow["tasks"]}, {801})

    def test_narrowing_to_a_role_that_cannot_do_it_is_refused(self):
        """Сужение до роли без допуска — ошибка, а не пустой план."""
        with self.assertRaises(ValueError) as caught:
            entities.add_campaign(
                self.store, name="ерунда", action="check_public_chat_metadata",
                roles=["dm_sender"], campaign_id="nope",
            )
        self.assertIn("не разрешён", str(caught.exception))

    # -- план и выпуск говорят одними числами -------------------------------

    def test_the_plan_uses_the_same_floor_the_dispatcher_checks(self):
        limits = self.settings.limits
        limits.read_per_account_interval_sec = 40
        for index in range(3):
            entities.add_contact(
                self.store, username=f"src{index}", kind="channel",
                segment="sources", actor="test",
            )

        plan = planner.plan(self.store, "recon", limits=limits, dry_run=True)

        slots = [
            datetime.fromisoformat(task["scheduled_at"])
            for task in plan["tasks"]
        ]
        self.assertEqual(len(slots), 3, plan["skipped"])
        for earlier, later in zip(slots, slots[1:]):
            self.assertGreaterEqual((later - earlier).total_seconds(), 40)


class ResultTableTests(unittest.TestCase):
    """Три формы ответа сводятся к одной строке."""

    def test_channel_dm_metadata(self):
        line = research.row({
            "action": "check_channel_dm_metadata", "state": "done",
            "target": "somechannel",
            "result": {
                "availability": "available", "public_username": "somechannel",
                "channel_tg_id": 1763001372, "monoforum_tg_id": 2833001372,
                "paid_message_stars": 0,
            },
        })

        self.assertEqual(line["verdict"], "есть личка")
        self.assertEqual(line["monoforum_tg_id"], 2833001372)
        self.assertEqual(line["tg_id"], 1763001372)

    def test_public_chat_metadata(self):
        line = research.row({
            "action": "check_public_chat_metadata", "state": "done",
            "result": {
                "decision": "restricted", "structurally_writable": False,
                "chat": {
                    "type": "Channel", "tg_id": 42, "username": "closedchat",
                    "title": "Закрытый", "megagroup": True,
                    "participants_count": 1200,
                },
            },
        })

        self.assertEqual(line["verdict"], "закрыт")
        self.assertEqual(line["kind"], "супергруппа")
        self.assertEqual(line["participants"], 1200)
        self.assertEqual(line["username"], "closedchat")

    def test_plain_lookup(self):
        line = research.row({
            "action": "get_supergroup", "state": "done",
            "result": {"chat": {"type": "Channel", "tg_id": 7,
                                "title": "Вещание", "broadcast": True}},
        })

        self.assertEqual(line["verdict"], "найден")
        self.assertEqual(line["kind"], "канал")

    def test_a_refusal_reads_as_a_refusal(self):
        line = research.row({
            "action": "search_public_chat", "state": "failed",
            "target": "gone", "error_code": "username_not_occupied",
            "result": {},
        })

        self.assertEqual(line["verdict"], "отказ")
        self.assertEqual(line["error"], "username_not_occupied")
        self.assertEqual(line["username"], "gone")

    def test_the_result_is_read_from_the_stored_string(self):
        """В базе результат лежит текстом — строка обязана его разобрать."""
        line = research.row({
            "action": "check_channel_dm_metadata", "state": "done",
            "result": json.dumps({"availability": "available",
                                  "public_username": "x"}),
        })

        self.assertEqual(line["verdict"], "есть личка")

    def test_summary_counts_verdicts(self):
        counts = research.summary([
            {"verdict": "есть личка"}, {"verdict": "есть личка"},
            {"verdict": "отказ"},
        ])

        self.assertEqual(counts, [("есть личка", 2), ("отказ", 1)])

    def test_export_keeps_columns_importable(self):
        """Выгрузку должно быть можно скормить обратно в `contacts --import`."""
        for column in ("username", "tg_id", "kind"):
            self.assertIn(column, research.COLUMNS)


class frozen_clock:
    """Подменить «сейчас» в диспетчере. Своё, чтобы не тащить зависимость.

    Без заморозки половина проверок падала бы по ночам — и падала бы верно:
    окно у разведки есть, и в 04:00 диспетчер обязан отказать.
    """

    def __init__(self, moment: datetime) -> None:
        self.moment = moment
        self.real = None

    def start(self):
        import bridge49.dispatcher as module

        self.real = module.datetime
        moment = self.moment

        class Frozen(datetime):
            @classmethod
            def now(cls, tz=None):
                return moment.astimezone(tz) if tz else moment

        module.datetime = Frozen
        return self

    def stop(self):
        import bridge49.dispatcher as module

        if self.real is not None:
            module.datetime = self.real
        self.real = None

    __enter__ = start

    def __exit__(self, *exc):
        self.stop()
        return False


if __name__ == "__main__":
    unittest.main()
