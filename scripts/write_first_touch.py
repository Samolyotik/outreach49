"""Тексты первых личных сообщений. Базу не трогает, ничего не отправляет.

Третий шаг после `export_b140_candidates.py` (правила) и
`qualify_b140_candidates.py` (модель). Контракт — `bridge49/first_touch.py`,
перенесённый из прежнего контура дословно вместе с проверкой готового текста.

    python3 scripts/write_first_touch.py --in var/dm_qualified.json \\
        --batch 10 --out var/dm_texts.json

Почему проверка после модели, а не только промпт. Текст уходит живому
человеку от лица аккаунта, который ему незнаком. `first_touch.validate_text`
ловит то, что промпт запрещает, но модель всё равно иногда пишет: название
сервиса, ссылку, обещание результата, речь от первого лица единственного
числа, больше одного вопроса. Черновик, не прошедший проверку, отбрасывается
целиком — переписывать его наполовину нельзя.
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

from bridge49 import first_touch  # noqa: E402


def as_contact(item: dict) -> dict:
    """Кандидат из выборки → вход контракта первого касания."""
    return {
        "row_id": str(item.get("row_id") or item.get("btm_id")),
        "primary_signal": {
            "category_code": item.get("категория") or "",
            "message_text": item.get("сообщение") or "",
            "source_title": item.get("источник") or "",
            "published_at": item.get("написано") or "",
        },
        "signals": [],
    }


def ask(prompt: str, *, timeout: int) -> dict:
    command = os.environ.get("OUTREACH_LLM_COMMAND", "").strip()
    if not command:
        raise RuntimeError("не задан OUTREACH_LLM_COMMAND")
    payload = {
        "prompt_mode": "plain",
        "task": "first_touch_draft",
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
    if "drafts" in answer:
        return answer
    for key in ("result", "output", "data", "content"):
        inner = answer.get(key)
        if isinstance(inner, dict) and "drafts" in inner:
            return inner
        if isinstance(inner, str):
            try:
                parsed = json.loads(inner)
            except ValueError:
                continue
            if isinstance(parsed, dict) and "drafts" in parsed:
                return parsed
    raise RuntimeError(f"в ответе модели нет drafts: {raw[:200]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--batch", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=400)
    parser.add_argument("--out")
    args = parser.parse_args()

    payload = json.loads(Path(args.source).read_text(encoding="utf-8"))
    pool = payload.get("кандидаты") or []
    rows = pool[:args.limit]
    if not rows:
        print("нечего писать: qualified пуст")
        return 1

    by_id = {str(item.get("row_id") or item.get("btm_id")): item for item in rows}
    accepted: list[dict] = []
    refused: dict[str, int] = {}
    lost = 0

    for start in range(0, len(rows), args.batch):
        chunk = [as_contact(item) for item in rows[start:start + args.batch]]
        number = start // args.batch + 1
        try:
            answer = ask(first_touch.build_prompt(chunk), timeout=args.timeout)
            drafts = first_touch.parse_payload(json.dumps(answer, ensure_ascii=False))
        except (RuntimeError, ValueError) as exc:
            lost += len(chunk)
            print(f"  батч {number}: отказ — {str(exc)[:120]}")
            continue

        good = 0
        for row_id, draft in drafts.items():
            ok, problems = first_touch.accept(draft)
            if not ok:
                for problem in problems:
                    refused[problem] = refused.get(problem, 0) + 1
                continue
            source = by_id.get(str(row_id))
            if source is None:
                continue
            accepted.append({
                "username": source.get("username"),
                "категория": source.get("категория"),
                "оценка": source.get("fit_score"),
                "намерение": source.get("intent"),
                "повод": source.get("сообщение"),
                "текст": draft.get("final_text"),
                "row_id": row_id,
            })
            good += 1
        print(f"  батч {number}: принято {good} из {len(chunk)}")

    print(f"\nготовых текстов: {len(accepted)} из {len(rows)}"
          + (f", батчей не сошлось на {lost}" if lost else ""))
    if refused:
        print("\nотбраковано проверкой:")
        for reason, count in sorted(refused.items(), key=lambda kv: -kv[1]):
            print(f"  {reason:34} {count}")

    unique = len({item["текст"] for item in accepted})
    print(f"\nразных текстов: {unique} из {len(accepted)}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"готово": len(accepted), "тексты": accepted},
                       ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        print(f"записано: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
