"""Резервная копия: проверяем не факт создания файла, а его пригодность.

Копия, которую никто не открывал, ничего не гарантирует. Поэтому тесты бьют
в три места: копия открывается и содержит те же строки; неудача не оставляет
за собой обрезанный файл; ротация не съедает свежие копии вместе со старыми.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "backup_db", ROOT / "scripts" / "backup_db.py")
backup_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_db)


def seed(path: Path, *, tasks: int = 3) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE tasks(id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE contacts(id TEXT PRIMARY KEY)")
        conn.executemany(
            "INSERT INTO tasks(id) VALUES(?)",
            [(f"t{i}",) for i in range(tasks)])
        conn.commit()
    finally:
        conn.close()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "bridge49.sqlite"
        self.out = self.root / "backups"
        seed(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_copy_opens_and_holds_the_same_rows(self):
        report = backup_db.make_backup(self.db, self.out, keep=7)
        copy = Path(str(report["копия"]))
        self.assertTrue(copy.exists())
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            self.assertEqual(
                conn.execute("SELECT count(*) FROM tasks").fetchone()[0], 3)
            self.assertEqual(
                conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conn.close()

    def test_wal_content_reaches_the_copy(self):
        """Незачекпойнченная запись должна попасть в копию.

        Ровно то, чего не даёт обычный `cp`: свежие строки живут в WAL, и
        копия файла базы без него окажется вчерашней.
        """
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("INSERT INTO tasks(id) VALUES('свежая')")
            conn.commit()
        finally:
            conn.close()
        report = backup_db.make_backup(self.db, self.out, keep=7)
        conn = sqlite3.connect(f"file:{report['копия']}?mode=ro", uri=True)
        try:
            found = conn.execute(
                "SELECT count(*) FROM tasks WHERE id='свежая'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(found, 1)

    def test_missing_database_is_a_loud_failure(self):
        with self.assertRaises(SystemExit):
            backup_db.make_backup(self.root / "нет.sqlite", self.out, keep=7)

    def test_failure_leaves_no_partial_file(self):
        """Мусор размером с базу не должен копиться после сбоя."""
        broken = self.root / "broken.sqlite"
        broken.write_bytes(b"not a database at all" * 100)
        with self.assertRaises(Exception):
            backup_db.make_backup(broken, self.out, keep=7)
        leftovers = list(self.out.glob(".*partial")) if self.out.exists() else []
        self.assertEqual(leftovers, [])

    def test_rotation_keeps_the_freshest(self):
        made = []
        for _ in range(5):
            # Имя копии несёт секунды, поэтому в тесте кладём файлы сами:
            # пять вызовов подряд уложились бы в одну секунду.
            report = backup_db.make_backup(self.db, self.out, keep=99)
            path = Path(str(report["копия"]))
            made.append(path)
            renamed = path.with_name(
                f"{backup_db.PREFIX}2026080{len(made)}T000000Z{backup_db.SUFFIX}")
            path.rename(renamed)
            made[-1] = renamed

        backup_db._rotate(self.out, keep=2)
        left = sorted(p.name for p in self.out.glob(f"{backup_db.PREFIX}*"))
        self.assertEqual(left, [made[-2].name, made[-1].name])

    def test_backup_is_not_world_readable(self):
        report = backup_db.make_backup(self.db, self.out, keep=7)
        mode = Path(str(report["копия"])).stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_keep_zero_is_refused(self):
        with self.assertRaises(SystemExit):
            backup_db.main(["--db", str(self.db), "--output-dir",
                            str(self.out), "--keep", "0"])


if __name__ == "__main__":
    unittest.main()
