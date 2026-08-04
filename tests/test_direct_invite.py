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


class ProductionConfigTests(unittest.TestCase):
    """Боевой конфиг проверяется как код, а не глазами.

    Ошибиться тут — значит увести человека в чужую тестовую группу: сторона
    StartBot принимает `sector_id` и `test_group_profile_id` свободной строкой,
    без списка допустимых значений, поэтому опечатка не отвергается, а
    исполняется.
    """

    PATH = Path(__file__).resolve().parents[1] / (
        "deployment/startbot-direct-invite.production.json")

    #: Шесть сфер, подтверждённых стороной StartBot 04.08.
    EXPECTED = {
        "auto_import_dealers": ("cars_abroad", "cars_abroad_test_group"),
        "real_estate_investment": (
            "real_estate_investment", "real_estate_investment_test_group"),
        "logistics_ved_china": (
            "logistics_ved_china", "logistics_ved_china_test_group"),
        "bankruptcy_debt_relief": (
            "bankruptcy_debt_relief", "bankruptcy_debt_relief_test_group"),
        "legal_services_business_private": (
            "legal_services_business_private",
            "legal_services_business_private_test_group"),
        "tourism_visas_relocation": (
            "tourism_visas_relocation", "tourism_visas_relocation_test_group"),
    }

    def setUp(self):
        self.branch = direct_invite.BranchConfig.from_path(self.PATH)

    def test_every_route_points_where_startbot_expects(self):
        for route_id, (sector_id, profile_id) in self.EXPECTED.items():
            with self.subTest(route_id):
                profile = self.branch.route_for(route_id)
                self.assertEqual(profile.sector_id, sector_id)
                self.assertEqual(profile.test_group_profile_id, profile_id)

    def test_all_six_are_switched_on(self):
        self.assertTrue(self.branch.enabled)
        self.assertEqual(set(self.branch.active_sector_ids), set(self.EXPECTED))

    def test_each_sector_has_its_own_test_group(self):
        """Общая группа у двух сфер = чужие люди в одном тесте."""
        groups = [p.test_group_profile_id
                  for p in self.branch.sector_profiles.values()]
        self.assertEqual(len(groups), len(set(groups)), "группа переиспользована")

    def test_startbot_names_are_unambiguous(self):
        """`resolve_route_sector_id` возвращает маршрут только при ровно одном
        совпадении. Дубль `sector_id` тихо увёл бы разбор в менеджерскую ветку."""
        ids = [p.sector_id for p in self.branch.sector_profiles.values()]
        self.assertEqual(len(ids), len(set(ids)))
        for route_id, (sector_id, _) in self.EXPECTED.items():
            with self.subTest(route_id):
                self.assertEqual(
                    self.branch.resolve_route_sector_id(sector_id), route_id)

    def test_unknown_sector_stays_closed(self):
        for name in ("marketing", "medicine", "cars_abroad_test_group", ""):
            with self.subTest(name), self.assertRaises(direct_invite.BranchInactive):
                self.branch.route_for(name)
        self.assertIsNone(self.branch.context_for_sector("medicine"))

    def test_catalog_shown_to_the_engine_matches_the_file(self):
        catalog = self.branch.active_sector_catalog()
        self.assertEqual(
            {item["outreach_sector_id"] for item in catalog}, set(self.EXPECTED))
        for item in catalog:
            with self.subTest(item["outreach_sector_id"]):
                self.assertTrue(item["sector_name"].strip())

    def test_message_survives_every_sector_name(self):
        """Название сферы уходит в письмо человеку дословно. Длинное имя со
        скобками не должно ломать ни одну из формулировок."""
        for route_id in self.EXPECTED:
            profile = self.branch.route_for(route_id)
            for seed in ("a", "b", "c", "d", "e", "f", "g", "h"):
                with self.subTest(route_id, seed=seed):
                    text = direct_invite.render_invite_message(
                        profile.sector_name, LINK, seed=seed)
                    self.assertIn(profile.sector_name, text)
                    self.assertIn(LINK, text)


class LegalVersusBankruptcyTests(unittest.TestCase):
    """Банкротство и общие юруслуги ведут в две разные тестовые группы.

    Сценарии заданы стороной StartBot 04.08. Выбор сферы делает модель, поэтому
    здесь проверяется не её суждение, а что решение доезжает до нужной группы и
    что явное противоречие останавливает выдачу. Само суждение — отдельный
    прогон по живой модели, он в наборе не живёт.
    """

    PATH = ProductionConfigTests.PATH
    BANKRUPTCY = "bankruptcy_debt_relief"
    LEGAL = "legal_services_business_private"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.store = Store(self.dir / "b.sqlite")
        self.branch = direct_invite.BranchConfig.from_path(self.PATH)
        self.store.execute(
            "INSERT INTO contacts(id, kind, username, segment, created_at, "
            "updated_at) VALUES('c1','user','someone','default',?,?)",
            (now(), now()))
        self.store.execute(
            "INSERT INTO threads(id, account_id, peer_key, contact_id, surface, "
            "created_at, updated_at) "
            "VALUES('th1',821,'@someone','c1','private_dm',?,?)", (now(), now()))
        self.store.commit()
        self.thread = dict(self.store.one("SELECT * FROM threads WHERE id='th1'"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, text: str, sector_id: str, inbound_id: str = "5001"):
        row = direct_invite.record_consent(
            self.store, config=self.branch, thread=self.thread,
            inbound={"id": inbound_id, "account_id": 821, "text": text},
            account_role="dm_sender", sector_id=sector_id)
        self.store.commit()
        return row

    def test_lawyer_for_bankruptcy_goes_to_the_bankruptcy_group(self):
        row = self.record("Нужен юрист по банкротству", self.BANKRUPTCY)
        self.assertEqual(row["test_group_profile_id"],
                         "bankruptcy_debt_relief_test_group")

    def test_debt_relief_goes_to_the_bankruptcy_group(self):
        row = self.record("Помощь со списанием долгов", self.BANKRUPTCY)
        self.assertEqual(row["test_group_profile_id"],
                         "bankruptcy_debt_relief_test_group")

    def test_contract_review_goes_to_the_legal_group(self):
        row = self.record("Нужна проверка договора юристом", self.LEGAL)
        self.assertEqual(row["test_group_profile_id"],
                         "legal_services_business_private_test_group")

    def test_named_specialization_is_not_swallowed_by_the_general_sector(self):
        """Второй слой: человек назвал банкротство, а сфера выбрана общая.

        Выдать тут ссылку — значит увести человека в чужую тестовую группу, и
        отозвать доступ потом придётся руками. Поэтому разговор идёт менеджеру.
        """
        for text in ("Нужен юрист по банкротству",
                     "Юрист по списанию долгов",
                     "Юридическое сопровождение процедуры банкротства",
                     "Помогите списать долги"):
            with self.subTest(text):
                self.assertIsNone(self.record(text, self.LEGAL))
        self.assertEqual(
            self.store.one("SELECT COUNT(*) AS n FROM direct_invites")["n"], 0)

    def test_plain_legal_requests_are_not_blocked(self):
        """Гейт умеет только запрещать и без явных слов молчит."""
        for n, text in enumerate((
                "Нужен юрист для проверки договора",
                "Ищу юридическое сопровождение бизнеса",
                "Нужна консультация по судебному спору")):
            with self.subTest(text):
                self.assertEqual(
                    direct_invite.contradicts_named_specialization(text, self.LEGAL),
                    "")
        row = self.record("Ищу юридическое сопровождение бизнеса", self.LEGAL)
        self.assertIsNotNone(row)

    def test_guard_never_touches_other_sectors(self):
        """Слово «банкротство» в чужой сфере — не повод её запрещать."""
        self.assertEqual(
            direct_invite.contradicts_named_specialization(
                "Возим авто, был случай банкротства поставщика",
                "auto_import_dealers"),
            "")

    def test_mixed_interest_issues_nothing_until_the_person_chooses(self):
        """Смешанный запрос: модель обязана вернуть пустую сферу и спросить.

        Пустая сфера сюда доезжает как «маршрута нет» — ни заявки, ни группы.
        """
        self.assertIsNone(self.record(
            "Интересны и общие юруслуги, и банкротство", ""))
        self.assertEqual(
            self.store.one("SELECT COUNT(*) AS n FROM direct_invites")["n"], 0)

    def test_after_the_person_chooses_only_that_group_is_issued(self):
        """Уточнили — выдаём ровно выбранное, и ровно один раз."""
        row = self.record("Давайте банкротство", self.BANKRUPTCY, inbound_id="5002")
        self.assertEqual(row["test_group_profile_id"],
                         "bankruptcy_debt_relief_test_group")
        # Вторая ссылка тому же человеку не выдаётся даже под другую сферу.
        self.assertIsNone(self.record("А ещё договоры", self.LEGAL,
                                      inbound_id="5003"))
        groups = [r["test_group_profile_id"] for r in self.store.query(
            "SELECT test_group_profile_id FROM direct_invites")]
        self.assertEqual(groups, ["bankruptcy_debt_relief_test_group"])


class KnowledgeSplitTests(unittest.TestCase):
    """Заметки базы знаний разделены и каждая несёт границу с соседней."""

    KB = Path(__file__).resolve().parents[1] / "knowledge_base"

    def test_the_combined_note_is_gone(self):
        self.assertFalse((self.KB / "sector_notes/legal_bankruptcy.md").exists())

    def test_both_notes_exist_and_carry_the_boundary(self):
        for name in ("bankruptcy_debt_relief", "legal_services_business_private"):
            with self.subTest(name):
                text = (self.KB / f"sector_notes/{name}.md").read_text(
                    encoding="utf-8")
                self.assertIn("Граница", text)
                # Правило приоритета обязано ехать с любой из двух заметок:
                # какая из них найдётся по запросу, заранее неизвестно.
                self.assertIn("специализаци", text)
                self.assertIn("уточняющий вопрос", text)

    def test_manifest_describes_both_notes_with_distinct_tags(self):
        manifest = json.loads((self.KB / "kb_manifest.json").read_text(
            encoding="utf-8"))
        sources = manifest["sources"]
        self.assertNotIn("sector_notes/legal_bankruptcy.md", sources)
        bankruptcy = sources["sector_notes/bankruptcy_debt_relief.md"]["tags"]
        legal = sources["sector_notes/legal_services_business_private.md"]["tags"]
        self.assertIn("банкротство", bankruptcy)
        self.assertNotIn("банкротство", legal)
        self.assertTrue(set(bankruptcy) - set(legal))

    def test_no_reference_points_at_the_removed_note(self):
        for name in ("customer_truth_sources_v2.json",
                     "customer_truth_runtime_v2.json", "kb_manifest.json"):
            with self.subTest(name):
                self.assertNotIn("legal_bankruptcy",
                                 (self.KB / name).read_text(encoding="utf-8"))


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

    def test_exhausted_attempts_call_a_human(self):
        """Иначе автоматика хуже ручного пути: ни ссылки, ни менеджера.

        Записав согласие, ветка перестаёт заводить карточку. Если после этого
        ссылку выпустить не удалось, единственный, кто ещё может спасти
        разговор, — человек. И узнать он должен сам, а не из отчёта.
        """
        client = FakeClient(fail=True)
        for _ in range(self.branch.max_attempts):
            self.store.execute(
                "UPDATE direct_invites SET next_attempt_at = NULL")
            self.store.commit()
            direct_invite.process_requests(
                self.store, None, config=self.branch, client=client, limit=5)
        card = self.store.one(
            "SELECT * FROM handoffs WHERE thread_id = 'th1' AND status = 'new'")
        self.assertIsNotNone(card)
        self.assertEqual(card["reason"], "free_test_access_failed")

    def test_human_is_not_called_while_attempts_remain(self):
        """Пока попытки есть, звать человека рано — ссылка ещё может уйти."""
        direct_invite.process_requests(
            self.store, None, config=self.branch,
            client=FakeClient(fail=True), limit=5)
        self.assertIsNone(self.store.one("SELECT * FROM handoffs"))

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

    def test_every_combination_keeps_the_facts(self):
        """Формулировки разные, факты одни.

        Перебираем все сочетания абзацев, а не выборку: вариант, потерявший
        предупреждение об одноразовости, встретится не в тестах, а у человека,
        который откроет ссылку не с того аккаунта.
        """
        from itertools import product

        combos = product(
            direct_invite._OPENINGS, direct_invite._LINK_LINES,
            direct_invite._ONE_TIME, direct_invite._INSIDE,
            direct_invite._CLOSINGS,
        )
        checked = 0
        for opening, link_line, one_time, inside, closing in combos:
            text = "\n\n".join((
                opening.format(sector="Авто из-за границы"),
                link_line.format(link=LINK), one_time, inside, closing,
            ))
            checked += 1
            self.assertIn(LINK, text)
            self.assertIn("Авто из-за границы", text)
            for group in direct_invite._ONE_TIME_MARKERS:
                self.assertTrue(
                    any(marker in one_time.lower() for marker in group),
                    f"потеряно предупреждение {group}: {one_time[:60]}",
                )
            # Стиль ответов: без длинного тире и эмодзи.
            self.assertNotIn("—", text)
        self.assertEqual(checked, 4 * 4 * 4 * 3 * 3)

    def test_different_recipients_get_different_text(self):
        """Иначе одинаковый текст с нескольких аккаунтов выдаёт рассылку."""
        import re

        links = [
            f"https://t.me/tgradar_start_bot?start=opaque{n:04d}"
            for n in range(60)
        ]
        skeletons = {
            re.sub(r"https://t\.me/\S+", "<ссылка>",
                   direct_invite.render_invite_message("Авто из-за границы", link))
            for link in links
        }
        # 576 сочетаний на 60 получателей: совпадения возможны, но текст не
        # должен быть один на всех.
        self.assertGreater(len(skeletons), 20, "разнообразия почти нет")

    def test_same_recipient_gets_stable_text(self):
        """Повторный выпуск не должен переписывать уже собранное письмо."""
        first = direct_invite.render_invite_message("Авто из-за границы", LINK)
        second = direct_invite.render_invite_message("Авто из-за границы", LINK)
        self.assertEqual(first, second)

    def test_seed_overrides_the_link(self):
        by_link = direct_invite.render_invite_message("Сфера", LINK)
        by_seed = direct_invite.render_invite_message("Сфера", LINK, seed="другое")
        self.assertIn(LINK, by_seed)
        self.assertNotEqual(by_link, by_seed)

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
