from __future__ import annotations

import os
from typing import Dict, List
from urllib.parse import quote

from .proxy_bindings import load_proxy_binding_manifest


def load_codex_proxy_routes() -> List[Dict[str, str]]:
    """Resolve an ordered SOCKS5 failover pool without exposing its secrets."""

    if os.environ.get("CODEX_LLM_PROXY_FAILOVER_ENABLED") != "1":
        return [{"route_id": "direct", "proxy_url": ""}]
    manifest_path = os.environ.get("CODEX_LLM_PROXY_BINDINGS_MANIFEST", "").strip()
    manifest_sha256 = os.environ.get("CODEX_LLM_PROXY_BINDINGS_SHA256", "").strip()
    ordered_ids = [
        item.strip()
        for item in os.environ.get("CODEX_LLM_PROXY_ACCOUNT_ORDER", "").split(",")
        if item.strip()
    ]
    if not manifest_path or not manifest_sha256:
        raise ValueError("codex_proxy_manifest_not_pinned")
    if not ordered_ids or len(ordered_ids) > 5 or len(set(ordered_ids)) != len(ordered_ids):
        raise ValueError("codex_proxy_order_must_contain_one_to_five_unique_accounts")
    manifest = load_proxy_binding_manifest(
        manifest_path,
        expected_sha256=manifest_sha256,
    )
    by_account = {binding.account_id: binding for binding in manifest.bindings}
    routes: List[Dict[str, str]] = []
    for account_id in ordered_ids:
        binding = by_account.get(account_id)
        if binding is None:
            raise ValueError("codex_proxy_order_contains_unknown_account")
        username = os.environ.get(binding.username_env, "")
        password = os.environ.get(binding.password_env, "")
        if not username or not password:
            raise ValueError("codex_proxy_secret_missing")
        proxy_url = (
            "socks5h://"
            f"{quote(username, safe='')}:{quote(password, safe='')}@"
            f"{binding.host}:{binding.port}"
        )
        routes.append({"route_id": account_id, "proxy_url": proxy_url})
    return routes


def codex_attempt_environment(proxy_url: str) -> Dict[str, str]:
    environment = dict(os.environ)
    proxy_names = (
        "ALL_PROXY",
        "all_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
    )
    for name in proxy_names:
        environment.pop(name, None)
    if proxy_url:
        for name in proxy_names:
            environment[name] = proxy_url
    return environment
