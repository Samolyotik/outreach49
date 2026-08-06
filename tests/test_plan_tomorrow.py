"""Раскладка дневного плана: границы, а не красота расписания.

Времена проверяет глаз — их видно в сводке. Здесь проверяется то, чего не
видно: что личка не добавляется сверху к уже занятому аккаунту, что режим
одной лички не переставляет чужие задачи и что письмо без текста не
превращается в пустую отправку.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49.store import Store, now  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "plan_tomorrow", ROOT / "scripts" / "plan_tomorrow.py")
plan_tomorrow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan_tomorrow)

DATE = "2026-08-05"
TEXT = ("Здравствуйте! Увидели ваше сообщение в чате. У нас есть сервис, "
        "который находит в мессенджерах и социальных сетях сообщения людей. "
        "Можем бесплатно показать. Интересно?")


class PlanDmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "b.sqlite"
        store = Store(self.path)
        # 804 — чистый dm_sender; 862 — и чат, и личка, как на проде.
        store.execute(
            "INSERT INTO accounts(id, label, role, roles, enabled, paused, "
            "runtime_state, synced_at) VALUES(804,'a804','dm_sender',"
            "'[\"dm_sender\"]',1,0,'running',?)", (now(),))
        store.execute(
            "INSERT INTO accounts(id, label, role, roles, enabled, paused, "
            "runtime_state, synced_at) VALUES(862,'a862','chat_sender',"
            "'[\"chat_sender\",\"dm_sender\"]',1,0,'running',?)", (now(),))
        for index in range(6):
            store.execute(
                "INSERT INTO contacts(id, kind, username, created_at, "
                "updated_at) VALUES(?,'user',?,?,?)",
                (f"c{index}", f"someone{index}", now(), now()))
        store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('topup_channels_chats','добор','send_channel_dm',?,?)",
            (now(), now()))
        # У 862 день уже занят: пять сообщений в чаты. Контакты разные —
        # на паре «кампания + контакт» стоит UNIQUE, и это тоже часть защиты
        # от второго письма одному человеку.
        for index in range(5):
            store.execute(
                "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
                "action, params, mode, scheduled_at, state, created_at, "
                "updated_at) VALUES(?,'topup_channels_chats',?,862,"
                "'send_public_chat_message','{}','immediate',?,'planned',?,?)",
                (f"task_busy{index}", f"c{index}",
                 f"{DATE}T09:0{index}:00+00:00", now(), now()))
        # А это ответ: человек написал сам, и в норму исходящих он не входит.
        # Действие у него обычное — догон из старой очереди отвечает обычной
        # отправкой, потому что входящего уведомления Radar уже нет.
        store.execute(
            "INSERT INTO campaigns(id, name, action, created_at, updated_at) "
            "VALUES('pending_replies','догон','send_private_dm',?,?)",
            (now(), now()))
        store.execute(
            "INSERT INTO tasks(id, campaign_id, contact_id, account_id, "
            "action, params, mode, scheduled_at, state, created_at, "
            "updated_at) VALUES('task_reply','pending_replies','c5',804,"
            "'send_private_dm','{}','immediate',?,'planned',?,?)",
            (f"{DATE}T07:03:00+00:00", now(), now()))
        store.commit()
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def candidates(self, count: int, *, with_text: bool = True) -> list[dict]:
        return [{"username": f"user{i}", "row_id": str(i),
                 "категория": "HOT", "сообщение": "нужна реклама",
                 "текст": TEXT if with_text else ""} for i in range(count)]

    def build(self, pool, **kw):
        return plan_tomorrow.build(
            self.path, date=DATE, per_account=kw.pop("per_account", 5),
            from_hour=10, to_hour=21, dm_pool=pool, only_dm=True, **kw)

    def test_busy_account_gets_no_dm(self):
        """862 уже отдал свои пять в чаты — личка сверху его не догружает."""
        plan = self.build(self.candidates(10))
        accounts = {row["аккаунт"] for row in plan["отправки"]}
        self.assertNotIn(862, accounts)
        self.assertEqual(accounts, {804})
        self.assertEqual(plan["всего"], 5)

    def test_sent_messages_still_count_against_the_norm(self):
        """Прогон в середине дня не должен выдавать вторую норму.

        Задача, которая уже ушла, перестаёт быть `planned`. Если считать
        только план, вычерпанный аккаунт выглядит пустым — и получает ещё
        пять писем сверх тех, что уже отправил.
        """
        store = Store(self.path)
        try:
            # 862 свои пять уже отправил: задачи не planned, отметка сегодня.
            store.execute(
                "UPDATE tasks SET state='done', dispatched_at=? "
                " WHERE account_id=862", (f"{DATE}T09:30:00+00:00",))
            store.commit()
        finally:
            store.close()
        plan = self.build(self.candidates(10))
        self.assertNotIn(862, {r["аккаунт"] for r in plan["отправки"]})

    def test_cancelled_tasks_free_the_slot_again(self):
        """Снятая задача место занимать не должна."""
        store = Store(self.path)
        try:
            store.execute("UPDATE tasks SET state='cancelled' "
                          " WHERE account_id=862")
            store.commit()
        finally:
            store.close()
        plan = self.build(self.candidates(10))
        self.assertIn(862, {r["аккаунт"] for r in plan["отправки"]})

    def test_busy_account_joins_only_when_free_ones_run_out(self):
        """Занятый аккаунт подключается лишь тогда, когда своих не хватает."""
        store = Store(self.path)
        try:
            # У 862 остаётся три сообщения в чаты, то есть место на два.
            store.execute("DELETE FROM tasks WHERE id IN "
                          "('task_busy3','task_busy4')")
            store.commit()
        finally:
            store.close()

        # Пул на пять: свободный 804 закрывает его целиком, 862 не трогаем.
        enough = self.build(self.candidates(5))
        self.assertEqual({r["аккаунт"] for r in enough["отправки"]}, {804})

        # Пул на десять: одного аккаунта мало, и 862 добирает ровно столько,
        # сколько у него осталось до потолка, — два письма, а не пять.
        short = self.build(self.candidates(10))
        counts: dict[int, int] = {}
        for row in short["отправки"]:
            counts[row["аккаунт"]] = counts.get(row["аккаунт"], 0) + 1
        self.assertEqual(counts, {804: 5, 862: 2})

    def test_reply_does_not_eat_the_outreach_quota(self):
        """У 804 висит догон-ответ обычной отправкой, и это не касание.

        Отличить его от исходящего можно только по кампании: действие у него
        такое же, как у холодного письма.
        """
        load = plan_tomorrow.build.__globals__["outreach_load"]
        store = Store(self.path)
        try:
            counts = load(store.conn, DATE)
        finally:
            store.close()
        self.assertEqual(counts.get(862), 5)
        self.assertIsNone(counts.get(804))

    def test_only_dm_leaves_existing_tasks_alone(self):
        """Долг в режиме одной лички не пересчитывается: он уже в очереди."""
        plan = self.build(self.candidates(3))
        self.assertTrue(all(row["вид"] == "лс" for row in plan["отправки"]))
        self.assertFalse(any(row.get("задача") for row in plan["отправки"]))

    def test_candidate_without_text_is_not_planned(self):
        plan = self.build(self.candidates(4, with_text=False))
        self.assertEqual(plan["всего"], 0)
        self.assertEqual(plan["остаток"].get("без текста"), 4)

    def test_slots_carry_text_and_row_id(self):
        plan = self.build(self.candidates(2))
        row = plan["отправки"][0]
        self.assertEqual(row["действие"], "send_private_dm")
        self.assertTrue(row["текст"].startswith("Здравствуйте!"))
        self.assertTrue(row["row_id"])
        self.assertIn("слот_utc", row)

    def test_slots_keep_the_gap_inside_one_account(self):
        plan = self.build(self.candidates(5))
        minutes = sorted(int(r["слот"][:2]) * 60 + int(r["слот"][3:])
                         for r in plan["отправки"])
        gaps = [b - a for a, b in zip(minutes, minutes[1:])]
        self.assertTrue(all(gap >= plan_tomorrow.MIN_ACCOUNT_GAP_MIN - 1
                            for gap in gaps), gaps)
        self.assertGreater(len(set(gaps)), 1, "равные интервалы — это сетка")

    def test_own_quota_lowers_only_the_named_account(self):
        """Аккаунт со своей нормой берёт меньше, остальные — как обычно.

        Норма нужна поимённо: аккаунт, сменивший занятие вчера, и аккаунт,
        который год пишет в каналы, для Telegram выглядят по-разному.
        """
        plan = self.build(self.candidates(10), quotas={804: 2})
        сколько: dict[int, int] = {}
        for row in plan["отправки"]:
            сколько[row["аккаунт"]] = сколько.get(row["аккаунт"], 0) + 1
        # 862 занят чатами и личку не берёт, поэтому весь план — на 804.
        self.assertEqual(сколько, {804: 2})
        self.assertEqual(plan["своя норма"], {"804": 2})

    def test_own_quota_counts_the_debt_already_queued(self):
        """Долг занимает место в норме, а не добавляется сверх неё.

        У 804 уже лежит один догон. При норме 2 он должен получить лишь одно
        новое касание, иначе день выйдет на три сообщения вместо двух.
        """
        plan = plan_tomorrow.build(
            self.path, date=DATE, per_account=5, from_hour=10, to_hour=21,
            dm_pool=self.candidates(10), quotas={804: 2})
        свои = [r for r in plan["отправки"] if r["аккаунт"] == 804]
        долг = [r for r in свои if r["вид"] == "долг"]
        self.assertEqual(len(долг), 1)
        self.assertEqual(len(свои), 2)


if __name__ == "__main__":
    unittest.main()
