#!/usr/bin/env python3
"""Проверенная резервная копия боевой базы.

Вся работа контура — один файл: курсоры входящих, дедуп касаний, статусы
заявок, история переписки. До сих пор его никто не копировал, то есть потеря
машины означала потерю всего, что контур знает о людях.

## Почему не `cp`

База живая, и в момент копирования в ней идёт запись. Обычное копирование даст
файл, который откроется, но окажется обрезанным на середине транзакции, — и
узнаем мы об этом ровно тогда, когда он понадобится. Поэтому копия снимается
штатным механизмом SQLite (`Connection.backup`): он согласован по определению,
не блокирует писателя и корректно забирает содержимое WAL.

## Почему копия сразу проверяется

Резервная копия, которую никто не открывал, — это не копия, а надежда. После
снятия файл открывается заново, прогоняется `integrity_check` и считаются
строки в опорных таблицах. Не сошлось — копия удаляется и команда падает с
ненулевым кодом, чтобы таймер отметился неудачей, а не тишиной.

## Почему скрипт ничего не импортирует из bridge49

Он обязан работать в самый неудачный момент — когда пакет сломан незавершённым
деплоем, а база ещё цела. Единственная зависимость — стандартная библиотека.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

#: Таблицы, по которым сверяется правдоподобность копии. Не полный список — их
#: задача поймать обрезанный файл, а не доказать эквивалентность.
WITNESS_TABLES = ("tasks", "contacts", "threads", "inbound", "state")

#: Сколько копий держим. Меньше пяти опасно: неудачный день может закрыть собой
#: все свежие копии, и откатываться будет некуда.
DEFAULT_KEEP = 7

PREFIX = "bridge49-"
SUFFIX = ".sqlite"


def _open_ro(path: Path) -> sqlite3.Connection:
    """Открыть только на чтение — читатель не должен становиться писателем."""
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)


def _witness_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in WITNESS_TABLES:
        try:
            row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
        except sqlite3.Error:
            # Таблицы может не быть на ранней схеме — это не повод падать.
            continue
        counts[table] = int(row[0])
    return counts


def _fsync_dir(path: Path) -> None:
    """Довести до диска саму запись в каталоге, а не только содержимое файла.

    Без этого после отключения питания файл может существовать под временным
    именем или не существовать вовсе, хотя os.replace уже вернул успех.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def make_backup(db: Path, output_dir: Path, *, keep: int) -> dict[str, object]:
    if not db.exists():
        raise SystemExit(f"базы нет: {db}")
    output_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = output_dir / f"{PREFIX}{stamp}{SUFFIX}"
    # Временное имя — в том же каталоге, иначе os.replace станет копированием
    # через границу файловой системы и перестанет быть атомарным.
    partial = output_dir / f".{PREFIX}{stamp}{SUFFIX}.partial"

    started = time.monotonic()
    # Незавершённая копия не должна пережить эту функцию ни при каком исходе:
    # иначе первый же сбой оставит в каталоге мусор размером с базу, и он
    # будет накапливаться молча — таймер отметится неудачей, а место кончится
    # без единой строчки про причину.
    try:
        source = _open_ro(db)
        try:
            expected = _witness_counts(source)
            target = sqlite3.connect(partial)
            try:
                source.backup(target)
                target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                target.commit()
            finally:
                target.close()
        finally:
            source.close()

        # Проверяем то, что реально легло на диск, а не то, что мы только что
        # держали в руках: копия открывается заново, отдельным соединением.
        check = _open_ro(partial)
        try:
            verdict = check.execute("PRAGMA integrity_check").fetchone()[0]
            if verdict != "ok":
                raise SystemExit(f"копия не прошла integrity_check: {verdict}")
            actual = _witness_counts(check)
        finally:
            check.close()

        # База живая, за время копирования строк могло прибавиться. Убыль же
        # означает, что мы сняли не то, — это отказ.
        for table, before in expected.items():
            after = actual.get(table)
            if after is None or after < before:
                raise SystemExit(
                    f"копия неполная: в {table} было {before}, стало {after}"
                )

        os.chmod(partial, 0o600)
        fd = os.open(partial, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(partial, final)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    _fsync_dir(output_dir)

    removed = _rotate(output_dir, keep=keep)
    return {
        "копия": str(final),
        "размер, МБ": round(final.stat().st_size / 1048576, 1),
        "секунд": round(time.monotonic() - started, 1),
        "строк": ", ".join(f"{k}={v}" for k, v in sorted(actual.items())),
        "удалено старых": removed,
    }


def _rotate(output_dir: Path, *, keep: int) -> int:
    """Оставить `keep` самых свежих копий. Имена сортируются как время."""
    existing = sorted(
        p for p in output_dir.glob(f"{PREFIX}*{SUFFIX}") if p.is_file()
    )
    doomed = existing[:-keep] if keep > 0 else []
    for path in doomed:
        path.unlink(missing_ok=True)
    if doomed:
        _fsync_dir(output_dir)
    return len(doomed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--db",
        default=os.environ.get("BRIDGE49_DB", "/opt/outreach49/var/bridge49.sqlite"),
        help="какую базу копируем",
    )
    parser.add_argument(
        "--output-dir",
        default="/var/backups/outreach49",
        help="куда складывать копии",
    )
    parser.add_argument(
        "--keep", type=int, default=DEFAULT_KEEP, help="сколько копий держать"
    )
    args = parser.parse_args(argv)

    if args.keep < 1:
        raise SystemExit("--keep меньше одной копии — это не резервирование")

    report = make_backup(
        Path(args.db).expanduser(),
        Path(args.output_dir).expanduser(),
        keep=args.keep,
    )
    width = max(len(k) for k in report)
    for key, value in report.items():
        print(f"{key.ljust(width)}  {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
