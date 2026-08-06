"""Словарь сфер обязан доехать до обоих юнитов, которые его читают.

Переменной `OUTREACH_SECTOR_CATALOG` не было ни в одном файле, и это выглядело
как «фича просто выключена». На деле выключено было больше, чем кажется:
`demo_bot_link` приезжает из того же словаря, а `canonical_sector_id` в схеме
обёртки — это enum, собранный по словарю. Пустой словарь превращает enum в одну
пустую строку, то есть модель физически не может назвать сферу. Человек с
подтверждённым направлением получает «менеджер свяжется с вами» и ни одной
ссылки — так 06.08 ушёл @secivn.

Юнитов, читающих ветку, ровно два: разбор входящих и выпуск. Подключить только
первый — значит оставить `invites status` слепым: расхождения словаря и
allowlist считаются только там, где словарь подцеплен, и это единственный
детектор рассинхрона.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import direct_invite  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE = ROOT / "deployment" / "sector.env"
CATALOG = ROOT / "deployment" / "sector-catalog.json"

VARIABLE = "OUTREACH_SECTOR_CATALOG"

#: Оба юнита, которые зовут `BranchConfig.from_env()`.
UNITS = (
    ROOT / "systemd" / "outreach49-autoreply.service",
    ROOT / "systemd" / "outreach49-invites.service",
)


def unit_lines(path: Path) -> list[str]:
    # Копия хелперов из `test_systemd_llm_command`, а не импорт: соседний файл
    # виден только под `unittest discover -s tests`, а по имени модуля
    # (`python -m unittest tests.test_...`) импорт падает. Три строки дешевле
    # теста, который не запускается половиной способов.
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def environment_files(path: Path) -> list[str]:
    return [
        line.split("=", 1)[1].lstrip("-")
        for line in unit_lines(path)
        if line.startswith("EnvironmentFile=")
    ]


def inline_environment(path: Path) -> list[str]:
    return [
        line.split("=", 1)[1].strip('"')
        for line in unit_lines(path)
        if line.startswith("Environment=")
    ]


def assignment(path: Path, variable: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{variable}="):
            return stripped.split("=", 1)[1].strip().strip('"')
    return ""


class SectorCatalogWiringTests(unittest.TestCase):
    def test_both_readers_load_the_override_file(self):
        """Один из двух — это контур, где разбор видит словарь, а выпуск нет."""
        for unit in UNITS:
            with self.subTest(unit=unit.name):
                self.assertIn(
                    "/opt/outreach49/deployment/sector.env",
                    environment_files(unit),
                    f"{unit.name} не подключает словарь сфер",
                )

    def test_the_catalog_is_not_set_with_an_inline_line(self):
        """`Environment=` молча проигрывает любому `EnvironmentFile=`."""
        for unit in UNITS:
            with self.subTest(unit=unit.name):
                for line in inline_environment(unit):
                    self.assertFalse(
                        line.startswith(f"{VARIABLE}="),
                        f"{VARIABLE} задан строкой Environment= в {unit.name}",
                    )

    def test_the_override_file_points_at_our_own_catalog(self):
        value = assignment(OVERRIDE, VARIABLE)
        self.assertTrue(value, f"файл не задаёт {VARIABLE}")
        self.assertEqual(value, "/opt/outreach49/deployment/sector-catalog.json")
        self.assertNotIn("/opt/tgradar-outreach/", value)

    def test_the_catalog_ships_with_the_repository(self):
        """Файл не в репозитории — значит на чистом клоне ветка мертва."""
        self.assertTrue(CATALOG.is_file(),
                        "deployment/sector-catalog.json не найден")

    def test_the_catalog_loads_the_way_the_worker_loads_it(self):
        """Проверять надо загрузчиком, а не глазами по JSON.

        Своя проверка полей повторяет `load_sector_catalog` неточно и потому
        пропускает ровно то, что он ловит: неизвестный статус строки, дубль
        сферы, кривые разграничители. Ошибка в данных прошла бы зелёной, а на
        боевой машине погасила бы маршрут целиком — `with_sector_catalog`
        глотает ошибку чтения и оставляет словарь пустым.
        """
        rows, demo_link = direct_invite.load_sector_catalog(CATALOG)
        self.assertTrue(rows, "словарь пуст")
        self.assertTrue(
            any(row.status == direct_invite.SECTOR_STATUS_READY
                for row in rows.values()),
            "в словаре нет ни одной сферы с готовой тестовой группой",
        )
        self.assertTrue(demo_link.startswith("https://t.me/"))
        # Загрузчик пустую ссылку пропускает, а `render_demo_message` на ней
        # падает — то есть маршрут молча закрылся бы на каждом ходу. Имя бота
        # обязано быть, иначе ссылка ведёт в никуда.
        handle = demo_link.split("?", 1)[0].rsplit("/", 1)[-1].strip()
        self.assertTrue(handle, f"в ссылке нет имени бота: {demo_link!r}")

    def test_the_demo_letter_can_actually_be_built_from_this_link(self):
        """Последняя проверка — собрать письмо тем же кодом, что и в бою."""
        _, demo_link = direct_invite.load_sector_catalog(CATALOG)
        text = direct_invite.render_demo_message(demo_link, seed="c1")
        self.assertIn(demo_link, text)


if __name__ == "__main__":
    unittest.main()
