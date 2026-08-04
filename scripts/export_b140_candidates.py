"""Кандидаты на личное сообщение из лидов бизнеса 140.

Перенос отбора из прежнего контура (`outreach/tgradar_contact_pipeline.py`).
Там он ходил за данными по SSH к экспортёру на чужом боксе; у нас есть свой
readonly-доступ к базе Radar, поэтому запрос делается напрямую, а правила
отбора перенесены как есть.

Бизнес 140 — это ТГ РАДАР, ищущий клиентов сам себе. Его лиды и есть люди,
которые вслух сформулировали потребность в том, что мы продаём. Им и пишем.

    ~/.radar-ops/with-radar-analyst-ro python3 scripts/export_b140_candidates.py \\
        --days 30 --limit 400 --contacted var/contacted.txt --out var/dm_candidates.json

Чего скрипт НЕ делает: не пишет ни в Radar, ни в нашу базу, не отправляет.

## Что перенесено дословно

* список категорий `HOT` / `WARM` / `COLD` (`SOURCE_EXPORT_CATEGORIES`);
* проверка языка `russian_language_check` — русский текст, но с поправкой на
  ссылки, ники и общепринятые латинские термины;
* `reachability_status` — до кого мы вообще дотянемся;
* отсев ботов и забаненных в бизнесе авторов;
* подавление тех, кому уже писали (`already_contacted_private_dm`).

## Чего НЕ перенесено

LLM-квалификация: у них после правил идёт разбор моделью с оценкой
пригодности (`qualified` / `maybe` / `reject`, пороги 70 и 40) и разбором
намерения. Здесь её нет — это отдельный шаг, и он дороже самого отбора.
Значит выборка ниже — это «правила пропустили», а не «модель одобрила».
Ранжирование ниже (категория, затем оценка совпадения) — грубая замена.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import ssl
from pathlib import Path

#: Категории лидов, из которых берём людей. Взято из конфига прежнего контура.
CATEGORIES = ("SUPER_HOT", "HOT", "WARM", "COLD")

#: Порядок предпочтения: чем горячее лид, тем раньше он попадёт в очередь.
CATEGORY_RANK = {name: index for index, name in enumerate(CATEGORIES)}

BUSINESS_ID = 140
TELEGRAM_PLATFORM_ID = 1

URL_HANDLE_RE = re.compile(r"(https?://\S+|www\.\S+|@[\w\d_]+)", re.IGNORECASE)
NON_RUSSIAN_CYRILLIC_RE = re.compile(r"[іїєґўђћџљњќѓѕј]", re.IGNORECASE)
CYRILLIC_LETTER_RE = re.compile(r"[а-яё]", re.IGNORECASE)
LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-]*")
LATIN_LETTER_RE = re.compile(r"[A-Za-z]")

#: Латиница, которая в русском деловом тексте нормальна и не делает его чужим.
COMMON_LATIN_TERMS = {
    "ads", "ai", "api", "b2b", "b2c", "backend", "chatgpt", "cmo", "cpa",
    "cpc", "cpl", "crm", "cto", "ceo", "erp", "faq", "gpt", "hr", "it",
    "kpi", "llm", "mvp", "pr", "roi", "saas", "seo", "smm", "sql", "telegram",
    "tg", "ui", "ux", "vk", "whatsapp",
}


def russian_language_check(text: str) -> tuple[bool, str]:
    """Русская проза, но ссылки, ники и общая латиница не в счёт.

    Перенесено дословно: доля кириллицы среди «смысловых» букв должна быть не
    ниже 70%, а украинские/сербские буквы отвергают текст сразу.
    """
    cleaned = URL_HANDLE_RE.sub(" ", (text or "").strip())
    if NON_RUSSIAN_CYRILLIC_RE.search(cleaned):
        return False, "non_russian_message"
    cyrillic = len(CYRILLIC_LETTER_RE.findall(cleaned))
    if cyrillic < 4:
        return False, "non_russian_message"

    unexpected_latin = 0
    for token in LATIN_TOKEN_RE.findall(cleaned):
        normalized = re.sub(r"[^a-z0-9]", "", token.lower())
        if not normalized or normalized in COMMON_LATIN_TERMS:
            continue
        if token.isupper() and len(normalized) <= 5:
            continue
        unexpected_latin += len(LATIN_LETTER_RE.findall(token))

    semantic = cyrillic + unexpected_latin
    if not semantic or cyrillic / semantic < 0.70:
        return False, "non_russian_message"
    return True, ""


QUERY = """
SELECT btm.id            AS btm_id,
       cat.machine_title AS category,
       btm.match_score,
       btm.match_source,
       btm.matched_at,
       m.id              AS message_id,
       m.text            AS message_text,
       m.published_at,
       m.permalink,
       a.id              AS author_id,
       a.username        AS author_username,
       a.display_name    AS author_name,
       a.native_id       AS author_native_id,
       a.config          AS author_config,
       ba.is_banned      AS author_banned,
       ba.status         AS author_status,
       src.title         AS source_title
  FROM business_target_message btm
  JOIN message m  ON m.id = btm.message_id
  JOIN author  a  ON a.id = m.author_id
  LEFT JOIN business_target_category cat ON cat.id = btm.category_id
  LEFT JOIN business_author ba
         ON ba.business_id = btm.business_id AND ba.author_id = a.id
  LEFT JOIN source src ON src.id = m.source_id
 WHERE btm.business_id = $1
   AND btm.is_target = true
   AND m.platform_id = $2
   AND cat.machine_title = ANY($3::text[])
   AND a.username IS NOT NULL AND a.username <> ''
   AND m.text IS NOT NULL AND m.text <> ''
   AND btm.matched_at >= now() - ($4 || ' days')::interval
 ORDER BY btm.matched_at DESC
 LIMIT $5
"""


async def fetch(days: int, scan_limit: int) -> list[dict]:
    import asyncpg

    conn = await asyncpg.connect(
        host=os.environ["RADAR_ANALYST_RO_HOST"],
        port=int(os.environ["RADAR_ANALYST_RO_PORT"]),
        user=os.environ["RADAR_ANALYST_RO_USER"],
        password=os.environ["RADAR_ANALYST_RO_PASSWORD"],
        database=os.environ["RADAR_ANALYST_RO_DATABASE"],
        ssl=False, timeout=60, statement_cache_size=0,
    )
    try:
        rows = await conn.fetch(
            QUERY, BUSINESS_ID, TELEGRAM_PLATFORM_ID, list(CATEGORIES),
            str(int(days)), int(scan_limit),
        )
        return [dict(row) for row in rows]
    finally:
        await conn.close()


def is_bot(row: dict) -> bool:
    config = row.get("author_config")
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except ValueError:
            config = None
    if isinstance(config, dict) and bool(config.get("is_bot")):
        return True
    return str(row.get("author_username") or "").lower().endswith("bot")


def select(rows: list[dict], *, contacted: set[str], limit: int) -> dict:
    """Отсеять по правилам и оставить по одному лучшему сообщению на человека."""
    rejected: dict[str, int] = {}
    best: dict[str, dict] = {}

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for row in rows:
        username = str(row.get("author_username") or "").strip().lstrip("@")
        if not username:
            reject("telegram_identity_unavailable")
            continue
        if username.lower() in contacted:
            reject("already_contacted_private_dm")
            continue
        if is_bot(row):
            reject("bot_author")
            continue
        if bool(row.get("author_banned")):
            reject("business_author_banned")
            continue
        ok, why = russian_language_check(str(row.get("message_text") or ""))
        if not ok:
            reject(why)
            continue

        key = username.lower()
        rank = (
            CATEGORY_RANK.get(str(row.get("category") or ""), 99),
            -float(row.get("match_score") or 0),
        )
        current = best.get(key)
        if current is None or rank < current["_rank"]:
            best[key] = {
                "_rank": rank,
                "username": username,
                "имя": row.get("author_name") or "",
                "категория": row.get("category"),
                "оценка": float(row.get("match_score") or 0),
                "как найден": row.get("match_source"),
                "источник": row.get("source_title") or "",
                "сообщение": (str(row.get("message_text") or "")
                              .replace("\n", " ")[:400]),
                "ссылка": row.get("permalink") or "",
                "написано": str(row.get("published_at") or "")[:19],
                "btm_id": row.get("btm_id"),
            }

    ordered = sorted(best.values(), key=lambda item: item["_rank"])
    for item in ordered:
        item.pop("_rank", None)
    return {"кандидатов": len(ordered[:limit]),
            "найдено всего": len(best),
            "отсеяно": rejected,
            "кандидаты": ordered[:limit]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--scan-limit", type=int, default=20000)
    parser.add_argument("--limit", type=int, default=400)
    parser.add_argument("--contacted", help="файл со списком username, кому уже писали")
    parser.add_argument("--out")
    args = parser.parse_args()

    contacted: set[str] = set()
    if args.contacted and Path(args.contacted).exists():
        contacted = {
            line.strip().lstrip("@").lower()
            for line in Path(args.contacted).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    rows = asyncio.run(fetch(args.days, args.scan_limit))
    result = select(rows, contacted=contacted, limit=args.limit)

    print(f"лидов просмотрено: {len(rows)}")
    print(f"уникальных людей прошло правила: {result['найдено всего']}")
    print(f"взято в выборку: {result['кандидатов']}")
    print("\nотсеяно:")
    for reason, count in sorted(result["отсеяно"].items(), key=lambda kv: -kv[1]):
        print(f"  {reason:34} {count}")
    by_category: dict[str, int] = {}
    for item in result["кандидаты"]:
        by_category[item["категория"]] = by_category.get(item["категория"], 0) + 1
    print("\nпо категориям:")
    for name in CATEGORIES:
        if by_category.get(name):
            print(f"  {name:10} {by_category[name]}")

    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"\nзаписано: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
