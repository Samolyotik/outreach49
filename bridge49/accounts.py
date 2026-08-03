"""Реестр наших 49 TGR-аккаунтов.

Мост не даёт читать таблицу `account` — у выделенной роли есть только функции
и три view. Поэтому инвентарь приходит снимком: JSON, который делает оператор
Radar (`scripts/snapshot_accounts.py` в этом же репозитории) и кладёт рядом.

Снимок — не источник истины про права. Даже если он устарел, Radar заново
проверит роль и allowlist перед каждым действием и отклонит лишнее.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from . import catalog
from .store import Store, dumps, loads, now


def load_snapshot(path: Path | str) -> list[dict]:
    """Прочитать снимок аккаунтов. Принимает и наш формат, и сырой дамп."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "accounts" in data:
        data = data["accounts"]
    if not isinstance(data, list):
        raise ValueError("снимок должен быть списком аккаунтов")
    return data


def sync(
    store: Store, rows: Iterable[dict], *, actor: str = "cli",
    pause_new: bool = False,
) -> dict:
    """Влить снимок в локальный реестр. Локальную паузу и заметку не трогаем.

    ``pause_new`` ставит на паузу только что появившиеся аккаунты — уже
    работающих это не касается. Так принимают чужие аккаунты: они приезжают в
    реестр, видны в отчётах, но ни одна задача им не достанется, пока паузу не
    снимут поимённо. Карантин обязан быть свойством самого приёма, иначе он
    превращается в шаг, который забывают сделать.
    """
    seen: set[int] = set()
    added = updated = skipped = 0
    fresh: list[int] = []

    for raw in rows:
        outreach = raw.get("outreach") or raw.get("RESPONDER_OUTREACH") or {}
        roles = outreach.get("roles") or ([outreach["role"]] if outreach.get("role") else [])
        if not roles:
            skipped += 1
            continue

        account_id = int(raw["id"])
        seen.add(account_id)
        existing = store.one("SELECT id FROM accounts WHERE id = ?", (account_id,))

        fields = (
            str(raw.get("label") or f"account-{account_id}"),
            raw.get("program_code"),
            str(roles[0]),
            dumps(sorted(outreach.get("allowed_actions") or [])),
            int(bool(outreach.get("enabled"))),
            int(bool(outreach.get("publish_inbound"))),
            int(bool(outreach.get("allow_immediate_visible_actions"))),
            raw.get("runtime_state"),
            raw.get("last_heartbeat_at"),
            now(),
            account_id,
        )

        if existing:
            store.execute(
                "UPDATE accounts SET label=?, program_code=?, role=?, "
                "allowed_actions=?, enabled=?, publish_inbound=?, "
                "allow_immediate=?, runtime_state=?, last_heartbeat_at=?, "
                "synced_at=? WHERE id=?",
                fields,
            )
            updated += 1
        else:
            store.execute(
                "INSERT INTO accounts(label, program_code, role, allowed_actions, "
                "enabled, publish_inbound, allow_immediate, runtime_state, "
                "last_heartbeat_at, synced_at, id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                fields,
            )
            added += 1
            fresh.append(account_id)

    if pause_new and fresh:
        store.execute(
            "UPDATE accounts SET paused = 1 WHERE id IN "
            f"({','.join('?' * len(fresh))})",
            fresh,
        )
        store.log(actor, "accounts.quarantine", "",
                  f"paused_new={','.join(str(i) for i in fresh)}")

    stale = [
        int(row["id"])
        for row in store.query("SELECT id FROM accounts")
        if int(row["id"]) not in seen
    ]
    store.log(
        actor, "accounts.sync", "",
        f"added={added} updated={updated} skipped={skipped} stale={len(stale)}",
    )
    store.commit()
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "stale": stale,
        "total": len(seen),
        "paused_new": fresh if pause_new else [],
    }


def all_accounts(store: Store) -> list[dict]:
    rows = store.query("SELECT * FROM accounts ORDER BY id")
    return [_hydrate(row) for row in rows]


def get(store: Store, account_id: int) -> dict | None:
    row = store.one("SELECT * FROM accounts WHERE id = ?", (int(account_id),))
    return _hydrate(row) if row else None


def _hydrate(row: Any) -> dict:
    account = dict(row)
    account["allowed_actions"] = set(loads(account.get("allowed_actions"), []))
    account["roles"] = {account["role"]}
    account["enabled"] = bool(account["enabled"])
    account["publish_inbound"] = bool(account["publish_inbound"])
    account["allow_immediate"] = bool(account["allow_immediate"])
    account["paused"] = bool(account["paused"])
    return account


def usable(account: dict, action: str) -> tuple[bool, str]:
    """Можно ли поручить этому аккаунту это действие прямо сейчас."""
    if account["paused"]:
        return False, "аккаунт на локальной паузе"
    if not account["enabled"]:
        return False, "RESPONDER_OUTREACH.enabled=false в Radar"
    if account.get("runtime_state") and account["runtime_state"] != "running":
        return False, f"runtime_state={account['runtime_state']}"
    try:
        catalog.validate(
            action, _probe_params(action), roles=account["roles"],
            allowed_actions=account["allowed_actions"] or None,
        )
    except catalog.ValidationError as exc:
        message = str(exc)
        # Нехватка параметров здесь не важна: мы проверяем только допуск.
        if "не хватает параметров" in message or "нужен селектор" in message \
                or "нужен непустой text" in message:
            return True, ""
        return False, message
    return True, ""


def _probe_params(action: str) -> dict:
    """Минимальные параметры, чтобы validate дошёл до проверки роли."""
    spec = catalog.ACTIONS.get(action)
    if spec is None:
        return {}
    params: dict[str, Any] = {}
    if spec.selector:
        params["username"] = "probe"
    for key in spec.required:
        params.setdefault(key, "probe" if key != "message_id" else 1)
    if spec.needs_text:
        params["text"] = "probe"
    return params


def candidates(store: Store, action: str) -> list[dict]:
    """Аккаунты, которым можно поручить действие, в порядке загруженности."""
    result = []
    for account in all_accounts(store):
        ok, _ = usable(account, action)
        if ok:
            result.append(account)
    return result


def resume_one(store: Store, role: str, *, actor: str = "cli") -> dict | None:
    """Снять паузу ровно с одного аккаунта роли. Возвращает его или `None`.

    Ступень постепенного ввода. Вводить флот залпом нельзя — если что-то не
    так с разбором ответа или с самими целями, узнаешь об этом сразу на всех.
    Вводить руками по одному тоже плохо: ступень тогда равна тому, как скоро
    у человека дойдут руки, а это не темп, а случайность.

    Берётся аккаунт с наименьшим id — не потому, что он чем-то лучше, а чтобы
    порядок ввода был воспроизводим и по журналу читалось, кто следующий.
    """
    row = store.one(
        "SELECT id FROM accounts WHERE role = ? AND paused = 1 AND enabled = 1 "
        "ORDER BY id LIMIT 1",
        (str(role),),
    )
    if row is None:
        return None
    account_id = int(row["id"])
    store.execute("UPDATE accounts SET paused = 0 WHERE id = ?", (account_id,))
    left = store.one(
        "SELECT count(*) AS n FROM accounts WHERE role = ? AND paused = 1",
        (str(role),),
    )
    store.log(actor, "accounts.ramp", str(account_id),
              f"role={role} осталось={left['n']}")
    store.commit()
    return {"id": account_id, "left": int(left["n"])}


def pause(store: Store, account_id: int, paused: bool, *, actor: str = "cli") -> None:
    store.execute(
        "UPDATE accounts SET paused = ? WHERE id = ?",
        (int(bool(paused)), int(account_id)),
    )
    store.log(
        actor, "accounts.pause" if paused else "accounts.resume", str(account_id)
    )
    store.commit()
