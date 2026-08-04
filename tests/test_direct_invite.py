"""Автовыдача бесплатного теста.

Главное, что здесь проверяется, — не «ссылка выдалась», а границы: кому её
выдавать нельзя и что происходит, когда сервис недоступен. Цена ошибки
несимметрична. Не выдать ссылку — значит разговор пойдёт через менеджера, как
и шёл до сих пор. Выдать лишнюю — значит открыть посторонему человеку доступ,
который потом надо отзывать руками.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import direct_invite  # noqa: E402
from bridge49.store import Store, now  # noqa: E402

CONFIG = {
    "schema_version": 1,
    "enabled": True,
    "active_sector_ids": ["auto_import_dealers"],
    "validity_days": 7,
    "max_attempts": 5,
    "sector_profiles": {
        "auto_import_dealers": {
            "outreach_sector_id": "auto_import_dealers",
            "sector_id": "cars_abroad",
            "sector_name": "Авто из-за границы",
            "test_group_profile_id": "cars_abroad_test_group",
        }
    },
}

LINK = "https://t.me/tgradar_start_bot?start=opaque12"


def write_config(directory: Path, **overrides) -> Path:
    payload = dict(CONFIG)
    payload.update(overrides)
    path = directory / "branch.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class BranchConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_active_sector_resolves_by_both_names(self):
        branch = direct_invite.BranchConfig.from_path(write_config(self.dir))
        for name in ("auto_import_dealers", "cars_abroad"):
            with self.subTest(name=name):
                self.assertEqual(
                    branch.route_for(name).sector_name, "Авто из-за границы"
                )

    def test_unknown_sector_is_inactive(self):
        branch = direct_invite.BranchConfig.from_path(write_config(self.dir))
        with self.assertRaises(direct_invite.BranchInactive):
            branch.route_for("логистика")
        self.assertIsNone(branch.context_for_sector("логистика"))

    def test_sector_without_allowlist_entry_is_inactive(self):
        """Профиль есть, но сфера не включена — выдачи нет.

        Разные вещи: «мы знаем, как выдать» и «мы решили выдавать». Список
        включённых сфер — это второе, и он же единственный переключатель.
        """
        branch = direct_invite.BranchConfig.from_path(
            write_config(self.dir, active_sector_ids=[], enabled=False)
        )
        with self.assertRaises(direct_invite.BranchInactive):
            branch.route_for("cars_abroad")

    def test_enabled_without_allowlist_is_rejected(self):
        with self.assertRaises(direct_invite.DirectInviteError):
            direct_invite.BranchConfig.from_path(
                write_config(self.dir, active_sector_ids=[])
            )

    def test_broken_config_disables_branch_instead_of_raising(self):
        """Кривой конфиг — это «выключено», а не падение разбора входящих."""
        path = self.dir / "broken.json"
        path.write_text("{не json", encoding="utf-8")
        import os

        os.environ[direct_invite.BRANCH_CONFIG_ENV] = str(path)
        try:
            branch = direct_invite.BranchConfig.from_env()
        finally:
            os.environ.pop(direct_invite.BRANCH_CONFIG_ENV, None)
        self.assertFalse(branch.enabled)
        self.assertEqual(branch.active_sector_catalog(), [])

    def test_catalog_exposes_exactly_the_allowlist(self):
        branch = direct_invite.BranchConfig.from_path(write_config(self.dir))
        self.assertEqual(
            branch.active_sector_catalog(),
            [{
                "outreach_sector_id": "auto_import_dealers",
                "sector_id": "cars_abroad",
                "sector_name": "Авто из-за границы",
            }],
        )


class DecisionReadingTests(unittest.TestCase):
    def test_consent_only_for_free_test_access(self):
        self.assertTrue(direct_invite.consent_from_decision(
            {"handoff_kind": "free_test_access"}))
        for kind in ("manager_action", "none", "", None):
            with self.subTest(kind=kind):
                self.assertFalse(
                    direct_invite.consent_from_decision({"handoff_kind": kind})
                )

    def test_sector_is_taken_only_from_the_explicit_field(self):
        """Догадываться по свободному тексту нельзя — цена ошибки высока."""
        self.assertEqual(
            direct_invite.sector_from_decision(
                {"matched_direct_invite_sector_id": "auto_import_dealers",
                 "reply_text": "давайте другую сферу"}),
            "auto_import_dealers",
        )
        self.assertEqual(
            direct_invite.sector_from_decision(
                {"reply_text": "авто из-за границы, конечно"}),
            "",
        )

    def test_request_id_is_stable_for_the_same_turn(self):
        first = direct_invite.request_id_for("th_1", "5001")
        self.assertEqual(first, direct_invite.request_id_for("th_1", "5001"))
        self.assertNotEqual(first, direct_invite.request_id_for("th_1", "5002"))


class ConsentRecordingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "b.sqlite")
        self.branch = direct_invite.BranchConfig.from_path(write_config(self.dir))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, created_at, "
            "updated_at) VALUES('c1','user','someone','default',?,?)",
            (now(), now()),
        )
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "created_at, updated_at) "
            "VALUES('th1',821,'@someone','c1','private_dm',?,?)",
            (now(), now()),
        )
        self.store.commit()
        self.thread = dict(self.store.one("SELECT * FROM threads WHERE id='th1'"))
        self.inbound = {"id": "5001", "account_id": 821}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, **overrides):
        kwargs = {
            "config": self.branch,
            "thread": self.thread,
            "inbound": self.inbound,
            "account_role": "dm_sender",
            "sector_id": "auto_import_dealers",
        }
        kwargs.update(overrides)
        result = direct_invite.record_consent(self.store, **kwargs)
        self.store.commit()
        return result

    def test_records_consent_for_allowed_sector(self):
        row = self.record()
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], direct_invite.STATUS_AGREED)
        self.assertEqual(row["source_channel"], "private_dm")
        self.assertEqual(row["sector_name"], "Авто из-за границы")

    def test_foreign_sector_is_refused(self):
        self.assertIsNone(self.record(sector_id="логистика"))
        self.assertEqual(
            self.store.one("SELECT COUNT(*) AS n FROM direct_invites")["n"], 0
        )

    def test_disabled_branch_records_nothing(self):
        branch = direct_invite.BranchConfig.disabled()
        self.assertIsNone(self.record(config=branch))

    def test_role_without_channel_is_refused(self):
        """Роль, не участвующая в переписке, не даёт канала согласия."""
        self.assertIsNone(self.record(account_role="source_reader"))

    def test_repeated_turn_does_not_create_a_second_request(self):
        first = self.record()
        second = self.record()
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(
            self.store.one("SELECT COUNT(*) AS n FROM direct_invites")["n"], 1
        )

    def test_second_link_is_not_issued_to_the_same_contact(self):
        """Доступ уже открыт. Вторая ссылка не помогает, а перебивает первую."""
        self.record()
        again = direct_invite.record_consent(
            self.store, config=self.branch, thread=self.thread,
            inbound={"id": "5002", "account_id": 821},
            account_role="dm_sender", sector_id="auto_import_dealers",
        )
        self.store.commit()
        self.assertIsNone(again)


class ProcessingTests(unittest.TestCase):
    """Выпуск ссылки. Сеть подменена — проверяем состояния, а не транспорт."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "b.sqlite")
        self.branch = direct_invite.BranchConfig.from_path(write_config(self.dir))
        self.store.execute(
            "INSERT INTO accounts(id, label, role, roles, enabled, synced_at) "
            "VALUES(821,'acc','dm_sender','[\"dm_sender\"]',1,?)", (now(),))
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, created_at, "
            "updated_at) VALUES('c1','user','someone','default',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "last_outbound_at, created_at, updated_at) "
            "VALUES('th1',821,'@someone','c1','private_dm',?,?,?)",
            (now(), now(), now()))
        self.store.execute(
            "INSERT INTO inbound(id, account_id, surface, peer_key, "
            "peer_username, text, sent_at, raw, contact_id, created_at) "
            "VALUES(5001,821,'private_dm','@someone','someone',?,?,'{}','c1',?)",
            ("давайте тест", now(), now()))
        self.store.commit()
        thread = dict(self.store.one("SELECT * FROM threads WHERE id='th1'"))
        direct_invite.record_consent(
            self.store, config=self.branch, thread=thread,
            inbound={"id": "5001", "account_id": 821},
            account_role="dm_sender", sector_id="auto_import_dealers")
        self.store.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_link_is_issued_and_queued_for_delivery(self):
        client = FakeClient()
        result = direct_invite.process_requests(
            self.store, None, config=self.branch, client=client, limit=5)
        self.assertEqual(result["выпущено"], 1)
        row = dict(self.store.one("SELECT * FROM direct_invites"))
        self.assertEqual(row["status"], direct_invite.STATUS_CREATED)
        self.assertTrue(row["task_id"])
        task = dict(self.store.one(
            "SELECT * FROM tasks WHERE id = ?", (row["task_id"],)))
        self.assertIn(LINK, json.loads(task["params"])["text"])
        self.assertEqual(task["campaign_id"], direct_invite.INVITE_CAMPAIGN_ID)

    def test_service_failure_keeps_the_consent_and_retries_later(self):
        """Отказ сервиса не теряет согласие: заявка ждёт следующей попытки."""
        client = FakeClient(fail=True)
        result = direct_invite.process_requests(
            self.store, None, config=self.branch, client=client, limit=5)
        self.assertEqual(result["ошибок"], 1)
        row = dict(self.store.one("SELECT * FROM direct_invites"))
        self.assertEqual(row["status"], direct_invite.STATUS_AGREED)
        self.assertEqual(row["attempt_count"], 1)
        self.assertTrue(row["next_attempt_at"])
        self.assertIn("туннель закрыт", row["last_error"])

    def test_attempts_are_exhausted_into_a_terminal_state(self):
        client = FakeClient(fail=True)
        for _ in range(self.branch.max_attempts):
            self.store.execute(
                "UPDATE direct_invites SET next_attempt_at = NULL")
            self.store.commit()
            direct_invite.process_requests(
                self.store, None, config=self.branch, client=client, limit=5)
        row = dict(self.store.one("SELECT * FROM direct_invites"))
        self.assertEqual(row["status"], direct_invite.STATUS_CREATE_FAILED)

    def test_disabled_branch_issues_nothing(self):
        result = direct_invite.process_requests(
            self.store, None, config=direct_invite.BranchConfig.disabled(),
            client=FakeClient(), limit=5)
        self.assertEqual(result["выпущено"], 0)
        row = dict(self.store.one("SELECT * FROM direct_invites"))
        self.assertEqual(row["status"], direct_invite.STATUS_AGREED)

    def test_delivered_task_marks_the_invite_delivered(self):
        direct_invite.process_requests(
            self.store, None, config=self.branch, client=FakeClient(), limit=5)
        row = dict(self.store.one("SELECT * FROM direct_invites"))
        self.store.execute(
            "UPDATE tasks SET state='done', finished_at=? WHERE id=?",
            (now(), row["task_id"]))
        self.store.commit()
        self.assertEqual(
            direct_invite.reconcile_deliveries(self.store)["доставлено"], 1)
        row = dict(self.store.one("SELECT * FROM direct_invites"))
        self.assertEqual(row["status"], direct_invite.STATUS_DELIVERED)
        self.assertTrue(row["link_delivered_at"])


class MessageRenderingTests(unittest.TestCase):
    def test_message_warns_about_one_time_link(self):
        text = direct_invite.render_invite_message("Авто из-за границы", LINK)
        self.assertIn(LINK, text)
        self.assertIn("Авто из-за границы", text)
        # Без этих двух предупреждений человек открывает ссылку не с того
        # аккаунта, и тест достаётся не ему.
        self.assertIn("одноразовая", text)
        self.assertIn("первым Telegram-аккаунтом", text)

    def test_invalid_link_is_refused(self):
        for bad in ("", "https://example.com/x", "https://t.me/bot"):
            with self.subTest(link=bad):
                with self.assertRaises(direct_invite.DirectInviteError):
                    direct_invite.render_invite_message("Сфера", bad)


class StartBotConfigTests(unittest.TestCase):
    def test_non_loopback_plain_http_is_refused(self):
        config = direct_invite.StartBotConfig(
            api_base_url="http://example.com", service_token="x" * 40)
        with self.assertRaises(direct_invite.DirectInviteError):
            config.validate()

    def test_short_token_is_refused(self):
        config = direct_invite.StartBotConfig(
            api_base_url="http://127.0.0.1:18097", service_token="short")
        with self.assertRaises(direct_invite.DirectInviteError):
            config.validate()

    def test_loopback_with_full_token_passes(self):
        direct_invite.StartBotConfig(
            api_base_url="http://127.0.0.1:18097",
            service_token="x" * 64,
        ).validate()


class FakeClient:
    """Подмена транспорта. Сеть в тестах не трогаем."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict] = []

    def create_direct_invite(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise direct_invite.DirectInviteError("сеть недоступна: туннель закрыт")
        profile = kwargs["profile"]
        return direct_invite.CreatedInvite(
            invite_id="fti_outreach_test",
            deep_link=LINK,
            expires_at=datetime.now(timezone.utc).isoformat(),
            replayed=False,
            ready_message=direct_invite.render_invite_message(
                profile.sector_name, LINK),
        )


if __name__ == "__main__":
    unittest.main()
