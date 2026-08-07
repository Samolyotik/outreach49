"""Чем именно разбор входящих зовёт модель.

Юнит автоответов подключает окружение прежнего контура целиком — ради Codex,
прокси и общей сессии. В том окружении своя `OUTREACH_LLM_COMMAND`, и она
указывает на его обёртку.

Перебить её строкой `Environment=` нельзя: `EnvironmentFile=` побеждает
`Environment=` независимо от порядка в юните. Ровно эта попытка в юните и
стояла, и была мертва с переезда 06.08.

Стоило это дня молчания. Чужая обёртка собирает модели другую схему ответа:
в её `required` четырнадцать полей, в нашей — семнадцать. Схема уходит в
`codex exec --output-schema`, то есть жёстко, и вернуть три недостающих поля
модель физически не могла. Наша проверка требует все семнадцать и валит ход
в `technical_failure` — а он молчит по замыслу. Девять живых разговоров за
день получили карточку менеджеру вместо ответа.

Поэтому проверки ниже держат не «переменная где-то задана», а способ: только
файлом и только последним.
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "systemd" / "outreach49-autoreply.service"
OVERRIDE = ROOT / "deployment" / "llm.env"

VARIABLE = "OUTREACH_LLM_COMMAND"


def unit_lines(path: Path) -> list[str]:
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


class AutoreplyUnitTests(unittest.TestCase):
    def test_the_wrapper_is_not_set_with_an_inline_line(self):
        """Такая строка молча проигрывает любому EnvironmentFile."""
        for assignment in inline_environment(UNIT):
            self.assertFalse(
                assignment.startswith(f"{VARIABLE}="),
                f"{VARIABLE} задан строкой Environment= — её перебьёт "
                "окружение прежнего контура, и модель позовут чужой обёрткой",
            )

    def test_our_override_file_is_loaded_last(self):
        """Среди EnvironmentFile побеждает последний — значит наш и должен быть им."""
        files = environment_files(UNIT)
        self.assertTrue(files, "юнит вообще не подключает окружение")
        self.assertEqual(
            files[-1], "/opt/outreach49/deployment/llm.env",
            "наш файл обязан идти после production.env, иначе он ничего не "
            "перебивает",
        )

    def test_the_override_file_defines_the_wrapper(self):
        assignments = [
            line.strip()
            for line in OVERRIDE.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        self.assertTrue(
            any(line.startswith(f"{VARIABLE}=") for line in assignments),
            f"файл-перебивка не задаёт {VARIABLE} — тогда он бесполезен",
        )

    def test_the_wrapper_points_at_our_own_contour(self):
        """Чужая обёртка — это чужая схема ответа и молчание на весь день."""
        text = OVERRIDE.read_text(encoding="utf-8")
        line = next(
            item for item in text.splitlines()
            if item.startswith(f"{VARIABLE}=")
        )
        value = line.split("=", 1)[1].strip().strip('"')
        self.assertIn("/opt/outreach49/scripts/codex_llm_wrapper.py", value)
        self.assertNotIn("/opt/tgradar-outreach/", value)

    def test_the_wrapper_keeps_its_interpreter(self):
        """Значение из двух слов: без кавычек systemd отбросил бы скрипт."""
        line = next(
            item for item in OVERRIDE.read_text(encoding="utf-8").splitlines()
            if item.startswith(f"{VARIABLE}=")
        )
        value = line.split("=", 1)[1].strip()
        self.assertTrue(value.startswith('"') and value.endswith('"'),
                        "значение с пробелом обязано быть в кавычках")
        self.assertEqual(len(value.strip('"').split()), 2)


class ВложенностьТаймаутов(unittest.TestCase):
    """Три предела на один вызов модели, и все три обязаны согласоваться.

    Обёртка берёт `min(CODEX_LLM_TIMEOUT_SECONDS,
    CODEX_LLM_PROXY_ATTEMPT_TIMEOUT_SECONDS)`, поэтому поднять только второй —
    значит не поднять ничего: первый обрежет его обратно. А сверху обе
    накрывает `decide_inbound_reply(timeout_seconds=...)`, который убивает саму
    обёртку; будь он меньше, маршруту не дали бы доработать.

    Проверка держит именно согласованность, а не конкретные числа: менять их
    можно, разъезжаться — нет.
    """

    def значения(self) -> dict[str, float]:
        из_файла = {}
        for строка in OVERRIDE.read_text(encoding="utf-8").splitlines():
            строка = строка.strip()
            if строка.startswith("CODEX_LLM") and "=" in строка:
                имя, _, значение = строка.partition("=")
                из_файла[имя.strip()] = float(значение.strip().strip('"'))
        return из_файла

    def test_оба_предела_заданы_и_равны(self):
        значения = self.значения()
        self.assertIn("CODEX_LLM_TIMEOUT_SECONDS", значения)
        self.assertIn("CODEX_LLM_PROXY_ATTEMPT_TIMEOUT_SECONDS", значения)
        self.assertEqual(
            значения["CODEX_LLM_TIMEOUT_SECONDS"],
            значения["CODEX_LLM_PROXY_ATTEMPT_TIMEOUT_SECONDS"],
            "общий предел обрежет маршрутный: обёртка берёт из них минимум")

    def test_предел_на_стороне_python_больше_маршрутного(self):
        import sys
        sys.path.insert(0, str(ROOT))
        from bridge49 import inbound_decision, presales_v2
        маршрут = self.значения()["CODEX_LLM_PROXY_ATTEMPT_TIMEOUT_SECONDS"]
        for функция in (inbound_decision.decide_inbound_reply,
                        presales_v2.call_presales_v2_llm):
            with self.subTest(функция=функция.__name__):
                предел = функция.__kwdefaults__["timeout_seconds"]
                self.assertGreater(
                    предел, маршрут,
                    "обёртку убьют раньше, чем маршрут доработает")


if __name__ == "__main__":
    unittest.main()
