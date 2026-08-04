"""Манифест прокси для Codex: чтение и проверка пина.

Взято с релиза a55d259 — две функции и два класса, до которых дотягивается
чтение манифеста. Ни одна из них не обращается к базе.

Остальное из ``proxy_bindings.py`` не переносим: там правка привязок с
аудитом в чужих таблицах. Нам нужно только прочитать готовый манифест и
убедиться, что он не менялся с момента, когда его закрепили.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple


@dataclass(frozen=True)
class ProxyBinding:
    account_id: str
    host: str
    port: int
    username_env: str
    password_env: str

    @property
    def endpoint(self) -> Tuple[str, int]:
        return self.host, self.port


@dataclass(frozen=True)
class ProxyBindingManifest:
    version: int
    bindings: Tuple[ProxyBinding, ...]
    sha256: str
    require_unique_endpoints: bool


def canonical_manifest_sha256(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_proxy_binding_manifest(
    path: str,
    *,
    expected_sha256: str = "",
) -> ProxyBindingManifest:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Proxy binding manifest must be a JSON object")
    actual_sha256 = canonical_manifest_sha256(payload)
    normalized_expected = str(expected_sha256 or "").strip().lower()
    if normalized_expected and actual_sha256 != normalized_expected:
        raise ValueError(
            "Proxy binding manifest SHA-256 mismatch: "
            f"expected {normalized_expected}, got {actual_sha256}"
        )
    if int(payload.get("version") or 0) != 1:
        raise ValueError("Proxy binding manifest version must be 1")
    raw_bindings = payload.get("bindings")
    if not isinstance(raw_bindings, list) or not raw_bindings:
        raise ValueError("Proxy binding manifest bindings must be a non-empty list")
    forbidden_keys = {"username", "password", "proxy_url", "url", "credentials"}
    bindings: List[ProxyBinding] = []
    for position, raw in enumerate(raw_bindings, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Proxy binding #{position} must be an object")
        leaked = forbidden_keys.intersection(raw)
        if leaked:
            raise ValueError(
                f"Proxy binding #{position} contains forbidden secret fields: "
                + ", ".join(sorted(leaked))
            )
        proxy_type = str(raw.get("proxy_type") or "socks5").strip().lower()
        if proxy_type != "socks5":
            raise ValueError(f"Proxy binding #{position} must use socks5")
        account_id = str(raw.get("account_id") or "").strip()
        host = str(raw.get("host") or "").strip()
        username_env = str(raw.get("username_env") or "").strip()
        password_env = str(raw.get("password_env") or "").strip()
        try:
            port = int(raw.get("port"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Proxy binding #{position} port must be an integer") from exc
        if not account_id or not host:
            raise ValueError(f"Proxy binding #{position} requires account_id and host")
        if port < 1 or port > 65535:
            raise ValueError(f"Proxy binding #{position} port is out of range")
        for env_name, label in (
            (username_env, "username_env"),
            (password_env, "password_env"),
        ):
            if not env_name or not env_name.replace("_", "A").isalnum() or not env_name[0].isalpha():
                raise ValueError(
                    f"Proxy binding #{position} {label} is not a valid environment name"
                )
        bindings.append(
            ProxyBinding(
                account_id=account_id,
                host=host,
                port=port,
                username_env=username_env,
                password_env=password_env,
            )
        )
    account_ids = [binding.account_id for binding in bindings]
    if len(set(account_ids)) != len(account_ids):
        raise ValueError("Proxy binding manifest contains duplicate account IDs")
    endpoints = [binding.endpoint for binding in bindings]
    require_unique = bool(payload.get("require_unique_endpoints", True))
    if require_unique and len(set(endpoints)) != len(endpoints):
        raise ValueError("Proxy binding manifest contains duplicate endpoints")
    return ProxyBindingManifest(
        version=1,
        bindings=tuple(bindings),
        sha256=actual_sha256,
        require_unique_endpoints=require_unique,
    )
