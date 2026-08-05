"""Разбор неудач: что значит код и что после него делать.

Проверяется не полнота таблицы, а её края. Полнота недостижима — коды приезжают
от двух посредников, и словарь у них свой. Важно другое: незнакомое не должно
ничего останавливать, знакомое опасное — должно, а неоднозначное нельзя трогать
вовсе.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49 import errors  # noqa: E402
from bridge49.store import Store, now  # noqa: E402


class ClassifyTests(unittest.TestCase):
    def test_peer_flood_holds_the_account(self):
        v = errors.classify("peer_flood", "PeerFloodError")
        self.assertEqual(v.scope, errors.SCOPE_ACCOUNT)
        self.assertEqual(v.action, errors.ACTION_HOLD_ACCOUNT)
        self.assertGreater(v.hold_seconds, 0)

    def test_flood_wait_takes_the_delay_from_the_text(self):
        v = errors.classify("flood_wait", "A wait of 420 seconds is required")
        self.assertEqual(v.hold_seconds, 420)

    def test_flood_wait_without_a_number_still_holds(self):
        v = errors.classify("flood_wait", "flood")
        self.assertEqual(v.hold_seconds, errors.DEFAULT_HOLD_SECONDS)

    def test_absurd_delay_is_bounded(self):
        v = errors.classify("flood_wait", "wait of 999999 seconds")
        self.assertLessEqual(v.hold_seconds, 24 * 3600)

    def test_closed_channel_dm_only_skips_the_recipient(self):
        v = errors.classify("channel_dm_disabled", "does not accept messages")
        self.assertEqual(v.scope, errors.SCOPE_RECIPIENT)
        self.assertEqual(v.action, errors.ACTION_SKIP)

    def test_unknown_code_does_not_stop_anything(self):
        """Главное свойство таблицы.

        У прежнего контура незнакомый код означал карантин аккаунта — потому
        что между ними и Telegram никого не было. У нас через то же поле
        приезжают наши собственные исключения, и такой fail-closed выключил бы
        флот за наши же баги.
        """
        v = errors.classify("СовершенноНоваяОшибка", "что-то произошло")
        self.assertEqual(v.scope, errors.SCOPE_UNKNOWN)
        self.assertEqual(v.action, errors.ACTION_NOTE)
        self.assertFalse(v.acts)

    def test_our_own_value_error_is_not_an_account_problem(self):
        v = errors.classify("ValueError", "деление на ноль где-то у нас")
        self.assertNotEqual(v.scope, errors.SCOPE_ACCOUNT)
        self.assertFalse(v.acts)

    def test_failed_resolve_is_recognised_by_its_text(self):
        """`ValueError` приезжает и как наш промах, и как чужое имя."""
        v = errors.classify(
            "ValueError", 'No user has "avto_amerikan_rf" as username')
        self.assertEqual(v.scope, errors.SCOPE_RECIPIENT)
        self.assertEqual(v.action, errors.ACTION_SKIP)

    def test_ambiguous_outcome_is_never_acted_upon(self):
        """Сообщение могло уйти. Любое действие здесь — второе сообщение."""
        v = errors.classify("ResponderAmbiguousSendOutcome", "unknown outcome")
        self.assertFalse(v.acts)

    def test_ambiguous_outcome_is_not_the_same_as_an_unknown_code(self):
        """Две разные вещи, и путать их нельзя даже в отчёте.

        Незнакомый код — пробел в таблице, его надо закрыть. Неясный исход —
        закрытое решение: трогать нечего, потому что сообщение могло уйти.
        Сведи их в одну строку сводки — и пробел будет вечно выглядеть
        заполненным.
        """
        ambiguous = errors.classify("ResponderAmbiguousSendOutcome", "")
        unknown = errors.classify("ЧтоТоНовое", "")
        self.assertEqual(ambiguous.scope, errors.SCOPE_AMBIGUOUS)
        self.assertEqual(unknown.scope, errors.SCOPE_UNKNOWN)
        self.assertNotEqual(ambiguous.scope, unknown.scope)

    def test_empty_code_is_harmless(self):
        self.assertFalse(errors.classify(None, None).acts)
        self.assertFalse(errors.classify("", "").acts)


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / "var").mkdir()
        self.store = Store(self.home / "var" / "b.sqlite")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def arm(self) -> None:
        (self.home / "var" / errors.SWITCH_FILE).write_text("on")

    def test_observation_records_but_does_not_hold(self):
        """Первая неделя: решения видны, но ничего не останавливают."""
        v = errors.record(
            self.store, task_id="t1", account_id=804, code="peer_flood",
            message="PeerFloodError", home=self.home)
        self.assertEqual(v.action, errors.ACTION_HOLD_ACCOUNT)
        self.assertIsNone(errors.held_until(self.store, 804))
        row = self.store.one(
            "SELECT detail FROM events WHERE kind='error.classified'")
        self.assertIn("наблюдение", row["detail"])

    def test_with_the_switch_the_account_is_held(self):
        self.arm()
        errors.record(self.store, task_id="t1", account_id=804,
                      code="peer_flood", message="PeerFloodError",
                      home=self.home)
        hold = errors.held_until(self.store, 804)
        self.assertIsNotNone(hold)
        self.assertIn("спам", hold[1])

    def test_expired_hold_stops_counting(self):
        self.store.set_state("account_hold:804", "2020-01-01T00:00:00+00:00|старое")
        self.store.commit()
        self.assertIsNone(errors.held_until(self.store, 804))

    def test_recipient_error_never_holds_the_account(self):
        self.arm()
        errors.record(self.store, task_id="t1", account_id=804,
                      code="channel_dm_disabled", message="closed",
                      home=self.home)
        self.assertIsNone(errors.held_until(self.store, 804))

    def test_unknown_code_never_holds_the_account(self):
        self.arm()
        errors.record(self.store, task_id="t1", account_id=804,
                      code="ЧтоТоНовое", message="?", home=self.home)
        self.assertIsNone(errors.held_until(self.store, 804))

    def test_every_failure_is_written_down(self):
        for code in ("peer_flood", "channel_dm_disabled", "ЧтоТоНовое"):
            errors.record(self.store, task_id=f"t_{code}", account_id=804,
                          code=code, message="", home=self.home)
        rows = self.store.query(
            "SELECT detail FROM events WHERE kind='error.classified'")
        self.assertEqual(len(rows), 3)


class CatalogAgreementTests(unittest.TestCase):
    """Таблица должна знать те коды, которые контур уже видел вживую."""

    SEEN_IN_PRODUCTION = (
        "channel_dm_disabled", "member_cannot_send", "paid_messages_required",
        "UsernameNotOccupiedError", "ForbiddenError", "join_request_pending",
        "not_sent", "mature_dm_failed", "ResponderAmbiguousSendOutcome",
        "chat_write_forbidden", "invalid_inbound_reply_target",
        "public_username_not_channel",
    )

    def test_all_observed_codes_are_known(self):
        unknown = [code for code in self.SEEN_IN_PRODUCTION
                   if code not in errors.CATALOG]
        self.assertEqual(unknown, [], f"не разобраны: {unknown}")

    def test_only_the_account_scope_ever_holds(self):
        """Придержать флот может ровно одна причина — и она про аккаунт."""
        for code in errors.CATALOG:
            verdict = errors.classify(code, "")
            if verdict.action == errors.ACTION_HOLD_ACCOUNT:
                self.assertEqual(verdict.scope, errors.SCOPE_ACCOUNT, code)


if __name__ == "__main__":
    unittest.main()
