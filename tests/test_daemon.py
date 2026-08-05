"""Постоянный процесс приёма: проверяем то, что теряется при демонизации.

Сам факт «крутится в цикле» проверять незачем. Ценность здесь в трёх местах,
где постоянный процесс ведёт себя иначе, чем разовый прогон, и где ошибка
будет молчаливой: настройки должны перечитываться, сбой не должен убивать
цикл, а вторая копия в том же доме не должна подниматься вовсе.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge49 import config, daemon  # noqa: E402
from bridge49.store import Store, loads  # noqa: E402


def make_settings(home: Path):
    """Настоящие настройки поверх временного дома.

    Мост не нужен: соединение в цикле подменяется, а без `need_dsn` загрузчик
    его не требует.
    """
    (home / "var").mkdir(parents=True, exist_ok=True)
    return config.load(home)


class IngestLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.settings = make_settings(self.home)
        Store(self.settings.db_path).close()

    def tearDown(self):
        self.tmp.cleanup()

    async def run_loop(self, **kw):
        defaults = dict(
            step=0.0,
            forum_step=0.0,
            max_iterations=kw.pop("iterations", 2),
            reload_settings=lambda: self.settings,
            connect=lambda settings: _ok(_FakeBridge()),
        )
        defaults.update(kw)
        return await daemon.run_ingest(self.settings, **defaults)

    async def test_loop_counts_what_it_took(self):
        async def results(store, settings, bridge):
            return {"updated": 2}

        async def inbound(store, settings, bridge):
            return {"stored": 3}

        tally = await self.run_loop(
            poll_results=results, poll_inbound=inbound,
            mirror=lambda store, settings: {"sent": 1})
        self.assertEqual(tally.iterations, 2)
        self.assertEqual(tally.results, 4)
        self.assertEqual(tally.inbound, 6)
        self.assertEqual(tally.failures, 0)

    async def test_failure_does_not_kill_the_loop(self):
        """Разовый прогон падает и уходит. Постоянный обязан пережить сбой.

        Иначе одна недоступность базы на секунду означает, что приём стоит до
        тех пор, пока кто-то это не заметит.
        """
        calls = {"n": 0}

        async def flaky(store, settings, bridge):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("мост отвалился")
            return {"updated": 1}

        tally = await self.run_loop(
            poll_results=flaky,
            poll_inbound=lambda *a: _ok({"stored": 0}),
            mirror=lambda store, settings: {})
        self.assertEqual(tally.iterations, 2)
        self.assertEqual(tally.failures, 1)
        self.assertIn("мост отвалился", tally.last_error)
        self.assertEqual(tally.results, 1, "второй круг должен был отработать")

    async def test_settings_are_read_again_every_round(self):
        """Рубильник, прочитанный на старте, перестаёт быть рубильником."""
        seen = {"n": 0}

        def reload():
            seen["n"] += 1
            return self.settings

        await self.run_loop(
            iterations=3,
            reload_settings=reload,
            poll_results=lambda *a: _ok({}),
            poll_inbound=lambda *a: _ok({}),
            mirror=lambda store, settings: {})
        self.assertEqual(seen["n"], 3)

    async def test_mirror_runs_on_its_own_slower_step(self):
        """Зеркало не обязано ходить так же часто, как приём."""
        mirrored = {"n": 0}

        def mirror(store, settings):
            mirrored["n"] += 1
            return {"sent": 0}

        await self.run_loop(
            iterations=3, forum_step=3600.0,
            poll_results=lambda *a: _ok({}),
            poll_inbound=lambda *a: _ok({}),
            mirror=mirror)
        self.assertEqual(mirrored["n"], 1, "второй круг зеркало трогать не должен")

    async def test_heartbeat_is_written_for_the_watchdog(self):
        """Зависший процесс выглядит здоровым — отличает его только отметка."""
        await self.run_loop(
            iterations=1,
            poll_results=lambda *a: _ok({}),
            poll_inbound=lambda *a: _ok({}),
            mirror=lambda store, settings: {})
        store = Store(self.settings.db_path)
        try:
            beat = loads(store.get_state(daemon.beat_key(daemon.ZONE_INGEST)), {})
        finally:
            store.close()
        self.assertEqual(beat.get("iterations"), 1)
        self.assertTrue(beat.get("at"))
        self.assertTrue(beat.get("pid"))

    async def test_heartbeat_survives_a_failed_round(self):
        """Иначе сбой выглядит как смерть, и сторож поднимет не ту тревогу."""
        async def broken(store, settings, bridge):
            raise RuntimeError("база недоступна")

        await self.run_loop(
            iterations=1, poll_results=broken,
            poll_inbound=lambda *a: _ok({}),
            mirror=lambda store, settings: {})
        store = Store(self.settings.db_path)
        try:
            beat = loads(store.get_state(daemon.beat_key(daemon.ZONE_INGEST)), {})
        finally:
            store.close()
        self.assertEqual(beat.get("failures"), 1)
        self.assertIn("база недоступна", beat.get("last_error", ""))

    async def test_stop_ends_the_loop_between_rounds(self):
        """SIGTERM — это «доработай круг», а не «умри на середине»."""
        stop = asyncio.Event()

        async def results(store, settings, bridge):
            stop.set()
            return {"updated": 1}

        tally = await self.run_loop(
            iterations=None, max_iterations=None, stop=stop,
            poll_results=results,
            poll_inbound=lambda *a: _ok({"stored": 0}),
            mirror=lambda store, settings: {})
        self.assertEqual(tally.iterations, 1)
        self.assertEqual(tally.results, 1)


class ZoneLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.settings = make_settings(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def test_second_holder_in_the_same_home_is_refused(self):
        with daemon.zone_lock(self.settings, daemon.ZONE_INGEST):
            with self.assertRaises(daemon.ZoneBusy):
                with daemon.zone_lock(self.settings, daemon.ZONE_INGEST):
                    pass

    def test_a_different_home_is_not_blocked(self):
        """Стенд и бой обязаны работать одновременно и не мешать друг другу."""
        other = Path(tempfile.mkdtemp())
        try:
            with daemon.zone_lock(self.settings, daemon.ZONE_INGEST):
                with daemon.zone_lock(make_settings(other), daemon.ZONE_INGEST):
                    pass
        finally:
            import shutil
            shutil.rmtree(other, ignore_errors=True)

    def test_lock_is_released_after_a_crash_inside(self):
        with self.assertRaises(ValueError):
            with daemon.zone_lock(self.settings, daemon.ZONE_INGEST):
                raise ValueError("падение внутри зоны")
        with daemon.zone_lock(self.settings, daemon.ZONE_INGEST):
            pass


async def _ok(payload):
    return payload


class _FakeBridge:
    """Соединение, которое ничего не соединяет. Закрывается молча."""

    async def close(self) -> None:
        return None


if __name__ == "__main__":
    unittest.main()
