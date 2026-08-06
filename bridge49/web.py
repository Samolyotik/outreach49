"""Веб-панель. Только чтение: ни одна страница ничего не меняет.

Стандартная библиотека, один файл, никаких шаблонизаторов. Слушает localhost —
наружу выставлять через SSH-туннель, а не открытием порта.
"""
from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .store import Store, loads

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bridge49 — {title}</title>
<style>
 :root {{ color-scheme: light dark; --line:#8883; --muted:#8888; }}
 body {{ font: 14px/1.5 -apple-system, "Segoe UI", Roboto, sans-serif;
        margin: 0; padding: 0 1.5rem 3rem; max-width: 1200px; }}
 header {{ display:flex; gap:1.2rem; align-items:baseline; flex-wrap:wrap;
           padding: 1.2rem 0; border-bottom:1px solid var(--line); }}
 h1 {{ font-size: 1.1rem; margin:0; letter-spacing:.02em; }}
 nav a {{ margin-right:1rem; text-decoration:none; border-bottom:1px solid var(--line); }}
 nav a.on {{ font-weight:600; border-bottom-color:currentColor; }}
 table {{ border-collapse: collapse; width:100%; margin: 1rem 0; }}
 th, td {{ text-align:left; padding:.45rem .6rem; border-bottom:1px solid var(--line);
           vertical-align: top; }}
 th {{ font-weight:600; font-size:.82rem; text-transform:uppercase;
       letter-spacing:.04em; color:var(--muted); }}
 td.num {{ text-align:right; font-variant-numeric: tabular-nums; }}
 .cards {{ display:flex; gap:1rem; flex-wrap:wrap; margin:1.2rem 0; }}
 .card {{ border:1px solid var(--line); border-radius:10px; padding:.8rem 1rem;
          min-width:9rem; }}
 .card b {{ display:block; font-size:1.6rem; font-weight:600;
            font-variant-numeric: tabular-nums; }}
 .card span {{ color:var(--muted); font-size:.82rem; }}
 .pill {{ display:inline-block; padding:.1rem .5rem; border-radius:99px;
          border:1px solid var(--line); font-size:.78rem; }}
 .armed {{ background:#c0392b; color:#fff; border-color:#c0392b; }}
 .safe {{ color:var(--muted); }}
 .msg {{ white-space:pre-wrap; max-width:52ch; }}
 footer {{ color:var(--muted); font-size:.8rem; margin-top:2rem; }}
</style></head><body>
<header>
  <h1>bridge49</h1>
  <nav>{nav}</nav>
  <span class="pill {armed_class}">{armed_text}</span>
</header>
{body}
<footer>Панель только читает. Любые изменения — через CLI на сервере.</footer>
</body></html>"""

TABS = (
    ("/", "Сводка"),
    ("/queue", "Очередь"),
    ("/threads", "Диалоги"),
    ("/inbox", "Входящие"),
    ("/accounts", "Аккаунты"),
    ("/events", "Журнал"),
)


def _nav(current: str) -> str:
    return "".join(
        f'<a href="{path}" class="{"on" if path == current else ""}">{title}</a>'
        for path, title in TABS
    )


def _table(rows, columns=None, numeric=()) -> str:
    rows = [dict(row) for row in rows]
    if not rows:
        return '<p class="safe">(пусто)</p>'
    columns = list(columns or rows[0].keys())
    head = "".join(f"<th>{html.escape(str(c))}</th>" for c in columns)
    body = []
    for row in rows:
        cells = []
        for column in columns:
            value = row.get(column)
            text = "—" if value is None else str(value)
            css = ' class="num"' if column in numeric else ""
            if column in ("текст", "сообщение"):
                css = ' class="msg"'
            cells.append(f"<td{css}>{html.escape(text)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _cards(pairs) -> str:
    return '<div class="cards">' + "".join(
        f'<div class="card"><b>{html.escape(str(v))}</b>'
        f'<span>{html.escape(str(k))}</span></div>'
        for k, v in pairs
    ) + "</div>"


def render(store: Store, settings: Settings, path: str, query: dict) -> str:
    limit = int((query.get("limit") or ["100"])[0])

    if path == "/queue":
        rows = store.query(
            "SELECT t.id, t.scheduled_at, t.account_id, a.label, c.username, "
            "       t.action, t.mode, t.state, t.outcome, t.error_code "
            "FROM tasks t LEFT JOIN accounts a ON a.id = t.account_id "
            "LEFT JOIN contacts c ON c.id = t.contact_id "
            "ORDER BY t.scheduled_at DESC LIMIT ?", (limit,)
        )
        body = _table([
            {
                "когда": (r["scheduled_at"] or "").replace("T", " ")[:16],
                "аккаунт": f"{r['account_id']} {r['label'] or ''}".strip(),
                "кому": r["username"] or "—",
                "действие": r["action"],
                "режим": r["mode"],
                "состояние": r["state"],
                "итог": r["outcome"] or r["error_code"] or "—",
            } for r in rows
        ])
        title = "Очередь"

    elif path == "/threads":
        rows = store.query(
            "SELECT t.*, c.username, c.display_name, a.label, "
            "  (SELECT count(*) FROM history h WHERE h.thread_id = t.id) AS hist "
            "FROM threads t "
            "LEFT JOIN contacts c ON c.id = t.contact_id "
            "LEFT JOIN accounts a ON a.id = t.account_id "
            "ORDER BY COALESCE(t.last_inbound_at, t.last_outbound_at) DESC LIMIT ?",
            (limit,)
        )
        head = "<tr><th>собеседник</th><th>аккаунт</th><th>состояние</th>" \
               "<th>последний ответ</th><th>сообщений</th></tr>"
        cells = []
        for r in rows:
            who = html.escape(str(r["username"] or r["peer_key"]))
            name = html.escape(str(r["display_name"] or ""))
            account = "— (до перехода)" if not r["account_id"] else \
                f"{r['account_id']} {r['label'] or ''}".strip()
            cells.append(
                f'<tr><td><a href="/thread?id={html.escape(r["id"])}">{who}</a>'
                f'{" · " + name if name else ""}</td>'
                f'<td>{html.escape(account)}</td>'
                f'<td>{html.escape(str(r["state"]))}</td>'
                f'<td>{html.escape((r["last_inbound_at"] or "—")[:16].replace("T", " "))}</td>'
                f'<td class="num">{r["hist"]}</td></tr>'
            )
        body = (f"<table><thead>{head}</thead><tbody>{''.join(cells)}</tbody></table>"
                if cells else '<p class="safe">(пусто)</p>')
        title = "Диалоги"

    elif path == "/thread":
        thread_id = (query.get("id") or [""])[0]
        thread = store.one("SELECT * FROM threads WHERE id = ?", (thread_id,))
        if thread is None:
            body = '<p class="safe">Диалог не найден.</p>'
            title = "Диалог"
        else:
            contact = store.one(
                "SELECT * FROM contacts WHERE id = ?", (thread["contact_id"],)
            )
            timeline = []
            for r in store.query(
                "SELECT * FROM history WHERE thread_id = ?", (thread_id,)
            ):
                timeline.append((r["sent_at"] or r["created_at"],
                                 r["direction"], "перенос", r["text"] or ""))
            if thread["contact_id"]:
                for r in store.query(
                    "SELECT * FROM tasks WHERE contact_id = ?",
                    (thread["contact_id"],)
                ):
                    timeline.append((
                        r["dispatched_at"] or r["scheduled_at"], "outbound",
                        f"{r['action']} / {r['state']}",
                        loads(r["params"], {}).get("text") or "",
                    ))
            for r in store.query(
                "SELECT * FROM inbound WHERE account_id = ? AND peer_key = ?",
                (thread["account_id"], thread["peer_key"])
            ):
                timeline.append((r["sent_at"] or r["created_at"], "inbound",
                                 "входящее", r["text"] or ""))
            timeline.sort(key=lambda item: item[0] or "")

            who = html.escape(str(
                (contact["username"] if contact else None) or thread["peer_key"]
            ))
            body = (
                f"<h2>{who}</h2>"
                + _cards([
                    ("состояние", thread["state"]),
                    ("канал", thread["surface"]),
                    ("аккаунт", thread["account_id"] or "до перехода"),
                    ("сообщений", len(timeline)),
                ])
                + _table([
                    {
                        "когда": (at or "")[:16].replace("T", " "),
                        "кто": "они" if direction == "inbound" else "мы",
                        "источник": origin,
                        "текст": text,
                    }
                    for at, direction, origin, text in timeline[-300:]
                ])
            )
            title = "Диалог"

    elif path == "/inbox":
        rows = store.query(
            "SELECT * FROM inbound ORDER BY id DESC LIMIT ?", (limit,)
        )
        body = _table([
            {
                "когда": (r["sent_at"] or r["created_at"] or "")[:16].replace("T", " "),
                "аккаунт": r["account_id"],
                "от": r["peer_username"] or r["peer_key"],
                "канал": r["surface"],
                "текст": r["text"] or "",
            } for r in rows
        ])
        title = "Входящие"

    elif path == "/accounts":
        rows = store.query("SELECT * FROM accounts ORDER BY id")
        body = _table([
            {
                "id": r["id"],
                "метка": r["label"],
                "программа": r["program_code"],
                "роль": r["role"],
                "действий": len(loads(r["allowed_actions"], [])),
                "включён": "да" if r["enabled"] else "нет",
                "входящие": "да" if r["publish_inbound"] else "нет",
                "пауза": "да" if r["paused"] else "нет",
                "молчит": (str(r["silenced_at"]).replace("T", " ")[:16]
                           if r["silenced_at"] else "нет"),
                "почему молчит": r["silenced_reason"] or "—",
            } for r in rows
        ], numeric=("id", "действий"))
        title = "Аккаунты"

    elif path == "/events":
        rows = store.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        body = _table([
            {
                "когда": r["at"].replace("T", " ")[:19],
                "кто": r["actor"],
                "что": r["kind"],
                "объект": r["subject"] or "—",
                "детали": r["detail"] or "—",
            } for r in rows
        ])
        title = "Журнал"

    else:
        tasks = {
            r["state"]: r["n"] for r in store.query(
                "SELECT state, count(*) AS n FROM tasks GROUP BY state"
            )
        }
        accounts = store.one(
            "SELECT count(*) AS total, "
            "sum(CASE WHEN enabled = 1 AND paused = 0 "
            "         AND silenced_at IS NULL THEN 1 ELSE 0 END) AS ready "
            "FROM accounts"
        )
        handoffs = store.one(
            "SELECT count(*) AS n FROM handoffs WHERE status = 'new'"
        )
        contacts = store.one(
            "SELECT count(*) AS n FROM contacts WHERE opted_out = 0"
        )
        body = _cards([
            ("аккаунтов готово", f"{accounts['ready'] or 0}/{accounts['total']}"),
            ("контактов", contacts["n"]),
            ("в плане", tasks.get("planned", 0)),
            ("в работе", tasks.get("queued", 0)),
            ("выполнено", tasks.get("done", 0)),
            ("ждут менеджера", handoffs["n"]),
        ])
        campaigns = store.query(
            "SELECT c.*, (SELECT count(*) FROM tasks t WHERE t.campaign_id = c.id) "
            "AS n FROM campaigns c ORDER BY c.created_at DESC"
        )
        body += "<h2>Кампании</h2>" + _table([
            {
                "название": r["name"],
                "действие": r["action"],
                "сегмент": r["segment"],
                "режим": r["mode"],
                "статус": r["status"],
                "задач": r["n"],
            } for r in campaigns
        ], numeric=("задач",))
        title = "Сводка"

    armed = settings.armed
    return PAGE.format(
        title=html.escape(title),
        nav=_nav(path),
        body=body,
        armed_class="armed" if armed else "safe",
        armed_text="боевой режим включён" if armed else "предпросмотр",
    )


def serve(settings: Settings, *, host: str = "127.0.0.1", port: int = 8649) -> None:
    class Handler(BaseHTTPRequestHandler):
        server_version = "bridge49"

        def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
            parsed = urlparse(self.path)
            if parsed.path == "/health":
                return self._send(200, "application/json",
                                  json.dumps({"ok": True}).encode())
            try:
                with Store(settings.db_path) as store:
                    page = render(store, settings, parsed.path,
                                  parse_qs(parsed.query))
            except Exception as exc:  # noqa: BLE001
                return self._send(500, "text/plain; charset=utf-8",
                                  f"ошибка: {exc}".encode())
            self._send(200, "text/html; charset=utf-8", page.encode())

        def _send(self, code: int, ctype: str, payload: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            pass  # не засоряем вывод

    print(f"bridge49: http://{host}:{port}  (Ctrl+C чтобы остановить)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
