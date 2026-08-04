"""Манифест прокси действительно читается.

При переносе у `ProxyBinding` потерялся декоратор `@dataclass`. Класс остался
классом с аннотациями и без конструктора, поэтому любое чтение манифеста
падало на `ProxyBinding() takes no arguments`.

Видно этого не было: единственный вызывающий — обёртка модели — ловит любое
исключение и молча уходит на прямой маршрут. То есть манифест был настроен,
пин проверялся, а запросы к модели всё это время шли с сервера напрямую, минуя
SOCKS5-прокси аккаунтов. Ровно то, что докстринг обёртки обещает не делать.
"""
from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bridge49 import proxy_bindings  # noqa: E402


class ProxyBindingTests(unittest.TestCase):
    def test_binding_is_constructible(self):
        binding = proxy_bindings.ProxyBinding(
            account_id="801", host="10.0.0.1", port=1080,
            username_env="U", password_env="P")
        self.assertEqual(binding.endpoint, ("10.0.0.1", 1080))

    def test_binding_is_a_frozen_dataclass(self):
        """Не украшение: без декоратора конструктора нет вовсе."""
        self.assertTrue(dataclasses.is_dataclass(proxy_bindings.ProxyBinding))
        binding = proxy_bindings.ProxyBinding(
            account_id="801", host="10.0.0.1", port=1080,
            username_env="U", password_env="P")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            binding.host = "10.0.0.2"  # type: ignore[misc]

    def test_manifest_loads_from_file(self):
        payload = {
            "version": 1,
            "require_unique_endpoints": True,
            "bindings": [{
                "account_id": "801", "host": "10.0.0.1", "port": 1080,
                "username_env": "PROXY_801_USER",
                "password_env": "PROXY_801_PASS",
            }],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            payload["sha256"] = proxy_bindings.canonical_manifest_sha256(payload)
            path.write_text(json.dumps(payload), encoding="utf-8")
            manifest = proxy_bindings.load_proxy_binding_manifest(str(path))
            self.assertEqual(len(manifest.bindings), 1)
            self.assertEqual(manifest.bindings[0].account_id, "801")


if __name__ == "__main__":
    unittest.main()
