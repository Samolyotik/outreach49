"""LLM-квалификация кандидатов на личное сообщение. Базу не трогает.

Второй слой отбора после `export_b140_candidates.py`. Правила отвечают «до
человека дотянемся и он писал по-русски»; здесь модель отвечает «ему вообще
есть о чём с нами говорить». Контракт — `bridge49/contact_fit.py`.

    OUTREACH_LLM_COMMAND=... python3 scripts/qualify_b140_candidates.py \\
        --in var/dm_candidates.json --limit 150 --batch 20 \\
        --out var/dm_qualified.json

Батчами, потому что решение выносится по каждому сообщению отдельно, а один
запрос на 150 строк модель разбирает хуже, чем восемь по двадцать. Батч, чей
ответ не сошёлся с контрактом, целиком уходит в отказ и не портит остальные.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge49 import contact_fit  # noqa: E402

#: Что модель считает каноном о продукте. Те же файлы, что у прежнего контура.
KNOWLEDGE_FILES = (
    "knowledge_base/product_overview.md",
    "knowledge_base/offer.md",
    "knowledge_base/service_scenarios.md",
    "knowledge_base/allowed_claims.md",
    "knowledge_base/forbidden_claims.md",
)


def knowledge_text() -> str:
    parts = []
    for name in KNOWLEDGE_FILES:
        path = ROOT / name
        if path.exists():
            parts.append(f"### {name}\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def ask(prompt: str, *, timeout: int) -> dict:
    command = os.environ.get("OUTREACH_LLM_COMMAND", "").strip()
    if not command:
        raise RuntimeError("не задан OUTREACH_LLM_COMMAND")
    payload = {
        # Своя ветка обёртки: presales-инструкции классификации только мешают.
        "prompt_mode": "plain",
        "task": "contact_fit_review",
        "prompt_version": contact_fit.PROMPT_VERSION,
        "reasoning_effort": "medium",
        "prompt": prompt,
    }
    completed = subprocess.run(
        command.split(), input=json.dumps(payload, ensure_ascii=False),
        text=True, capture_output=True, timeout=timeout, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"модель вернула код {completed.returncode}")
    raw = completed.stdout.strip()
    try:
        answer = json.loads(raw)
    except ValueError as exc:
        raise RuntimeError(f"ответ не JSON: {raw[:200]}") from exc
    # Обёртка может вернуть как сам объект, так и конверт вокруг него.
    for key in ("reviews",):
        if key in answer:
            return answer
    for key in ("result", "output", "data", "content"):
        inner = answer.get(key)
        if isinstance(inner, dict) and "reviews" in inner:
            return inner
        if isinstance(inner, str):
            try:
                parsed = json.loads(inner)
            except ValueError:
                continue
            if isinstance(parsed, dict) and "reviews" in parsed:
                return parsed
    raise RuntimeError("в ответе модели нет reviews")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--limit", type=int, default=150)
    parser.add_argument("--batch", type=int, default=20)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--out")
    args = parser.parse_args()

    payload = json.loads(Path(args.source).read_text(encoding="utf-8"))
    rows = (payload.get("кандидаты") or [])[:args.limit]
    if not rows:
        print("нечего квалифицировать")
        return 1

    knowledge = knowledge_text()
    by_id = {str(row.get("btm_id")): row for row in rows}
    # Считается по всей выборке, а не по батчу: шаблонный постинг видно только
    # тогда, когда рядом лежат остальные копии того же блока.
    repeats = contact_fit.template_repeats(rows)
    verdicts: list[dict] = []
    broken = 0

    for start in range(0, len(rows), args.batch):
        chunk = rows[start:start + args.batch]
        ids = [str(row.get("btm_id")) for row in chunk]
        try:
            answer = ask(contact_fit.build_prompt(chunk, knowledge, repeats),
                         timeout=args.timeout)
            verdicts.extend(contact_fit.validate(answer, ids))
        except (RuntimeError, contact_fit.FitError) as exc:
            broken += len(chunk)
            print(f"  батч {start // args.batch + 1}: отказ — {exc}")
            continue
        print(f"  батч {start // args.batch + 1}: разобрано {len(chunk)}")

    merged = []
    for verdict in verdicts:
        row = by_id.get(verdict["row_id"])
        if row is None:
            continue
        merged.append({**row, **verdict})
    merged.sort(key=lambda item: (-item["fit_score"], item["row_id"]))

    counts: dict[str, int] = {}
    intents: dict[str, int] = {}
    for item in merged:
        counts[item["decision"]] = counts.get(item["decision"], 0) + 1
        intents[item["intent"]] = intents.get(item["intent"], 0) + 1

    print(f"\nразобрано: {len(merged)} из {len(rows)}"
          + (f", батчей не сошлось на {broken} строк" if broken else ""))
    print("\nрешения:")
    for name in contact_fit.VALID_DECISIONS:
        if counts.get(name):
            print(f"  {name:10} {counts[name]}")
    print("\nнамерения:")
    for name, count in sorted(intents.items(), key=lambda kv: -kv[1]):
        print(f"  {name:38} {count}")

    result = {
        "версия промпта": contact_fit.PROMPT_VERSION,
        "разобрано": len(merged),
        "решения": counts,
        "намерения": intents,
        "кандидаты": [item for item in merged if item["decision"] == "qualified"],
        "на ручной просмотр": [item for item in merged
                               if item["decision"] == "maybe"],
    }
    print(f"\nqualified: {len(result['кандидаты'])}, "
          f"maybe: {len(result['на ручной просмотр'])}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"записано: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
