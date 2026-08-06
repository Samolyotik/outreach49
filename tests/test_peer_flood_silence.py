"""PeerFlood: аккаунт перестаёт писать первым, но продолжает отвечать.

Проверяется ровно то, что легко сломать незаметно.

Опознание построено на догадке, и это не оговорка, а устройство: слова
`PeerFlood` в ответе моста нет — Radar переводит его в `mature_dm_failed`,
который в остальное время означает наш собственный промах. Поэтому образцы
ниже — не выдуманные, а списанные с боевых команд 05.08.2026 (`76605` для
MA#833 и соседние). Если однажды Radar начнёт отвечать иначе, эти тесты
покажут это первыми.

Вторая половина — про границу немоты. Она односторонняя: не писать первым, но
отвечать. Стоит перепутать сторону, и мы либо продолжим рассылку с аккаунта,
который Telegram уже считает спамером, либо бросим без ответа людей, которые
сами написали. Обе ошибки тихие.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49 import accounts as accounts_mod  # noqa: E402
from bridge49 import alerts, errors  # noqa: E402
from bridge49.store import Store, now  # noqa: E402

#: Детали команды 76605 в том виде, в каком их отдаёт view моста. Лишнее
#: выброшено, ключи и значения — как в базе.
REAL_FAILED_COMMAND = {
    "kind": "direct_username_message",
    "action_attempts": 3,
    "avoid_account_ids": [833],
    "target_account_id": 833,
    "result": {
        "outcome": "failed",
        "error": {
            "code": "mature_dm_failed",
            "message": "Telegram send rejected; action retry cap reached",
            "retryable": False,
        },
    },
}


class RecognitionTests(unittest.TestCase):
    def test_real_production_failure_is_recognised(self):
        found = errors.flooded_accounts(
            account_id=833,
            code="mature_dm_failed",
            message="Telegram send rejected; action retry cap reached",
            details=REAL_FAILED_COMMAND,
        )
        self.assertIn(833, found)

    def test_avoid_list_alone_is_enough(self):
        """Команда удалась чужим аккаунтом, но первый всё равно получил отказ.

        Так выглядят `77013` и `77270`: исход `done`, а в деталях остался тот,
        кого Radar обошёл. Смотреть только на неудачи значит пропустить их.
        """
        found = errors.flooded_accounts(
            account_id=847, code="", message="",
            details={"avoid_account_ids": [812, 839]},
        )
        self.assertEqual(set(found), {812, 839})

    def test_first_rejection_is_enough_without_a_final_outcome(self):
        """Команда ещё в работе, исхода нет — а отказ уже был.

        Radar дописывает обойдённый аккаунт в детали сразу после первого
        отказа Telegram и возвращает команду в очередь ещё дважды, каждый раз
        переждав час кулдауна. Ждать исхода значит дать аккаунту постучаться
        ещё дважды — 05.08 MA#851 так ходил с 19:04 до полуночи.
        """
        in_flight = {"avoid_account_ids": [851], "action_attempts": 1}
        self.assertIn(851, errors.flooded_accounts(
            account_id=None, code="", message="", details=in_flight))

    def test_explicit_codes_are_recognised(self):
        for code in ("peer_flood", "peer_flood_fence_active"):
            found = errors.flooded_accounts(
                account_id=804, code=code, message="", details={})
            self.assertIn(804, found, code)

    def test_ordinary_failures_do_not_silence_anyone(self):
        """Самая частая ошибка контура — и она не про аккаунт.

        `channel_dm_disabled` приезжает сотнями в день: у канала закрыта личка.
        Замолчать по ней значило бы выключить весь флот за сутки.
        """
        for code, message in (
            ("channel_dm_disabled", "The public channel does not accept ..."),
            ("paid_messages_required", "requires payment"),
            ("UsernameNotOccupiedError", "The username is not in use"),
            ("ValueError", 'No user has "region02_ufa" as username'),
            ("not_sent", ""),
            ("mature_dm_failed", "что-то другое, не про повторы"),
            ("ResponderAmbiguousSendOutcome", "unknown outcome"),
        ):
            found = errors.flooded_accounts(
                account_id=804, code=code, message=message, details={})
            self.assertEqual(found, {}, code)

    def test_garbage_in_the_avoid_list_is_survived(self):
        """Детали приезжают из чужой системы; цикл по ним не должен падать."""
        found = errors.flooded_accounts(
            account_id=None, code="", message="",
            details={"avoid_account_ids": ["нет", None, 812, {"a": 1}]},
        )
        self.assertEqual(set(found), {812})

    def test_absurdly_long_avoid_list_is_bounded(self):
        found = errors.flooded_accounts(
            account_id=None, code="", message="",
            details={"avoid_account_ids": list(range(1, 10_000))},
        )
        self.assertLessEqual(len(found), errors._AVOID_MAX)


class SilenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "var").mkdir()
        self.store = Store(self.home / "var" / "b.sqlite")
        # Роли настоящие: в боевом реестре аккаунт умеет одно-два семейства
        # отправок, и `reply_channel_dm` для `dm_sender` недоступен сам по себе.
        # Подставить сюда «все роли» значило бы проверять несуществующий флот.
        for account_id, role in ((833, "dm_sender"), (847, "channel_sender")):
            self.store.execute(
                "INSERT INTO accounts(id, label, role, roles, allowed_actions, "
                "enabled, publish_inbound, allow_immediate, runtime_state, "
                "synced_at) VALUES(?,?,?,?,?,1,1,1,'running',?)",
                (account_id, f"acc{account_id}", role,
                 f'["{role}"]', '[]', now()),
            )
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def account(self, account_id: int = 833) -> dict:
        found = accounts_mod.get(self.store, account_id)
        assert found is not None
        return found

    def test_flood_silences_the_account(self):
        fresh = errors.silence_flooded(
            self.store, task_id="task_c0ebb3f6ebad", account_id=833,
            code="mature_dm_failed",
            message="Telegram send rejected; action retry cap reached",
            details=REAL_FAILED_COMMAND,
        )
        self.assertEqual([item["id"] for item in fresh], [833])
        self.assertIsNotNone(accounts_mod.silenced(self.account()))

    def test_silence_needs_no_switch_file(self):
        """Защита от бана не должна ждать, пока кто-то создаст файл.

        Остальные решения модуля стоят за рубильником `ERROR_ACTIONS` — он про
        первую неделю наблюдения. Немота живёт по своим правилам.
        """
        self.assertFalse(errors.switch_enabled(self.home))
        errors.silence_flooded(
            self.store, task_id="t", account_id=833, code="peer_flood",
            message="", details={},
        )
        self.assertIsNotNone(accounts_mod.silenced(self.account()))

    def test_second_flood_does_not_rewrite_the_first_reason(self):
        errors.silence_flooded(
            self.store, task_id="t1", account_id=833, code="peer_flood",
            message="", details={})
        first = self.account()["silenced_reason"]
        fresh = errors.silence_flooded(
            self.store, task_id="t2", account_id=833,
            code="mature_dm_failed",
            message="Telegram send rejected; action retry cap reached",
            details={})
        self.assertEqual(fresh, [], "о повторе уведомлять незачем")
        self.assertEqual(self.account()["silenced_reason"], first)

    def test_unknown_account_is_left_alone(self):
        """`avoid_account_ids` ведёт Radar, и там может стоять не наш аккаунт."""
        fresh = errors.silence_flooded(
            self.store, task_id="t", account_id=None, code="", message="",
            details={"avoid_account_ids": [999999]})
        self.assertEqual(fresh, [])

    def test_silenced_account_still_answers_incoming(self):
        """Сердцевина требования: не пишет первым, но отвечает."""
        for account_id, action in ((833, "reply_private_dm"),
                                   (847, "reply_channel_dm")):
            accounts_mod.silence(self.store, account_id, reason="PeerFlood")
            ok, why = accounts_mod.usable(self.account(account_id), action)
            self.assertTrue(ok, f"MA#{account_id} {action}: {why}")

    def test_silenced_account_starts_nothing_itself(self):
        """И блокирует именно немота, а не роль или что-то ещё.

        Проверяется парой: то же самое действие тем же аккаунтом до немоты
        разрешено. Без второй половины тест прошёл бы и на аккаунте, которому
        действие не положено по роли, — и ничего бы не доказывал.
        """
        cases = (
            (833, "send_private_dm"),
            (847, "send_channel_dm"),
            (847, "check_channel_dm_metadata"),
        )
        for account_id, action in cases:
            ok, why = accounts_mod.usable(self.account(account_id), action)
            self.assertTrue(ok, f"до немоты MA#{account_id} {action}: {why}")
        for account_id, _ in cases:
            accounts_mod.silence(self.store, account_id, reason="PeerFlood")
        for account_id, action in cases:
            ok, why = accounts_mod.usable(self.account(account_id), action)
            self.assertFalse(ok, f"MA#{account_id} {action}")
            self.assertIn("молчит", why)

    def test_echo_actions_still_work_on_a_silenced_account(self):
        """`doctor` должен уметь проверить придержанный аккаунт.

        Эти два действия не выходят за пределы Radar — закрывать их значит
        ослепить диагностику ровно там, где смотреть нужнее всего.
        """
        accounts_mod.silence(self.store, 833, reason="PeerFlood")
        for action in ("command_dry_run", "gateway_capabilities"):
            ok, why = accounts_mod.usable(self.account(), action)
            self.assertTrue(ok, f"{action}: {why}")

    def test_silenced_account_is_not_offered_to_the_planner(self):
        """Планировщик выбирает из `candidates` — молчащего там быть не должно."""
        before = {a["id"] for a in accounts_mod.candidates(
            self.store, "send_private_dm")}
        self.assertIn(833, before)
        accounts_mod.silence(self.store, 833, reason="PeerFlood")
        after = {a["id"] for a in accounts_mod.candidates(
            self.store, "send_private_dm")}
        self.assertNotIn(833, after)
        # Соседа немота одного аккаунта не касается: флот продолжает работать.
        self.assertIn(847, {a["id"] for a in accounts_mod.candidates(
            self.store, "send_channel_dm")})

    def test_silence_has_no_deadline(self):
        """Время немоту не снимает — только человек.

        Проверяется тем, что в реестре нет никакого «до», а снятие требует
        отдельного вызова: если однажды появится срок, этот тест придётся
        осознанно переписать, а не тихо пройти.
        """
        accounts_mod.silence(self.store, 833, reason="PeerFlood")
        columns = {row["name"] for row in self.store.conn.execute(
            "PRAGMA table_info(accounts)")}
        self.assertNotIn("silenced_until", columns)
        self.assertIsNotNone(accounts_mod.silenced(self.account()))

    def test_release_returns_the_account_to_work(self):
        accounts_mod.silence(self.store, 833, reason="PeerFlood")
        self.assertTrue(accounts_mod.release(self.store, 833, actor="andrey"))
        self.assertIsNone(accounts_mod.silenced(self.account()))
        ok, _ = accounts_mod.usable(self.account(), "send_private_dm")
        self.assertTrue(ok)

    def test_release_of_a_working_account_is_a_no_op(self):
        self.assertFalse(accounts_mod.release(self.store, 833))

    def test_account_sync_does_not_resurrect_a_silenced_account(self):
        """Снимок из Radar не знает про немоту и не должен её стирать."""
        accounts_mod.silence(self.store, 833, reason="PeerFlood")
        accounts_mod.sync(self.store, [{
            "id": 833, "label": "acc833", "runtime_state": "running",
            "outreach": {"roles": ["dm_sender"], "enabled": True,
                         "allowed_actions": [], "publish_inbound": True,
                         "allow_immediate_visible_actions": True},
        }])
        self.assertIsNotNone(accounts_mod.silenced(self.account()))

    def test_ramp_does_not_release_a_silenced_account(self):
        """Ступенчатый ввод снимает паузу, а не немоту.

        Разные состояния в одной колонке слились бы именно здесь: очередной
        шаг разгона вернул бы в рассылку аккаунт, который Telegram придержал.
        """
        accounts_mod.silence(self.store, 833, reason="PeerFlood")
        accounts_mod.pause(self.store, 833, True)
        accounts_mod.resume_one(self.store, "dm_sender")
        self.assertIsNotNone(accounts_mod.silenced(self.account()))
        ok, _ = accounts_mod.usable(self.account(), "send_private_dm")
        self.assertFalse(ok)

    def test_the_event_log_keeps_both_ends(self):
        accounts_mod.silence(self.store, 833, reason="PeerFlood", actor="poll")
        accounts_mod.release(self.store, 833, actor="andrey")
        kinds = [row["kind"] for row in self.store.query(
            "SELECT kind FROM events ORDER BY id")]
        self.assertIn("accounts.silence", kinds)
        self.assertIn("accounts.release", kinds)


class AlertTests(unittest.TestCase):
    def test_message_names_the_account_and_the_way_back(self):
        text = alerts.compose_silence_message(
            [{"id": 833, "label": "acc2015_138122772",
              "why": "Radar обошёл этот аккаунт после отказа Telegram",
              "task_id": "task_c0ebb3f6ebad"}],
            host="bots1",
        )
        self.assertIn("MA#833", text)
        self.assertIn("acc2015_138122772", text)
        self.assertIn("accounts --release 833", text)
        self.assertIn("Ответы на входящие продолжаются", text)

    def test_message_fits_telegram(self):
        """Двадцать аккаунтов сразу — и сообщение не должно быть обрезано.

        Обрезанное сообщение теряет как раз хвост: там, где написано, как
        вернуть аккаунты в работу.
        """
        text = alerts.compose_silence_message(
            [{"id": 800 + i, "label": f"acc2015_{i:09d}",
              "why": "Telegram отклонил отправку предельное число раз подряд",
              "task_id": f"task_{i:012x}"} for i in range(20)],
            host="bots1",
        )
        self.assertLessEqual(len(text), alerts.MAX_MESSAGE_LEN)


if __name__ == "__main__":
    unittest.main()
