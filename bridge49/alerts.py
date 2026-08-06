"""Доставка тревог сторожа в админ-форум Telegram.

Реквизиты берутся из env-файла в том же формате и с теми же именами, что у
прежнего контура (`OUTREACH_OPS_TELEGRAM_*`): бот и топик остаются те же, и
файл можно просто скопировать, ничего не переписывая.

Отправка идёт по HTTP из стандартной библиотеки — ради одного сообщения в
несколько минут тянуть зависимость незачем. Ошибка доставки никогда не
роняет сторожа: не сумев рассказать о проблеме, он не должен превращаться во
вторую проблему.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .config import _parse_env_file
from .store import Store

#: Куда смотреть за реквизитами, если не задано иное.
DEFAULT_ALERTS_FILE = "/etc/outreach49/alerts.env"

API_TIMEOUT_SEC = 12
#: Telegram режет сообщения на 4096 символах.
MAX_MESSAGE_LEN = 3500


class AlertError(RuntimeError):
    """Не удалось отправить. Вызывающий решает, насколько это важно."""


@dataclass(frozen=True)
class TelegramTarget:
    token: str
    chat_id: str
    thread_id: str | None = None
    enabled: bool = True

    @classmethod
    def from_file(cls, path: Path | str | None = None) -> "TelegramTarget | None":
        """Собрать адресата. ``None`` — доставка не настроена, это не ошибка."""
        path = Path(
            path or os.environ.get("BRIDGE49_ALERTS", DEFAULT_ALERTS_FILE)
        )
        if not path.exists():
            return None
        values = _parse_env_file(path)
        token = values.get("OUTREACH_OPS_TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = values.get("OUTREACH_OPS_TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            return None
        thread = values.get("OUTREACH_OPS_TELEGRAM_THREAD_ID", "").strip()
        enabled = values.get("OUTREACH_OPS_TELEGRAM_ENABLED", "1").strip()
        return cls(
            token=token,
            chat_id=chat_id,
            thread_id=thread or None,
            enabled=enabled not in ("0", "false", "False", "no"),
        )

    def describe(self) -> str:
        """Строка для логов. Токен сюда не попадает — и не должен."""
        where = f"chat {self.chat_id}"
        if self.thread_id:
            where += f", топик {self.thread_id}"
        return where


def send(target: TelegramTarget, text: str) -> int:
    """Отправить сообщение. Возвращает message_id."""
    if not target.enabled:
        raise AlertError("доставка выключена в конфиге")
    payload: dict[str, object] = {
        "chat_id": target.chat_id,
        "text": text[:MAX_MESSAGE_LEN],
        "disable_web_page_preview": True,
    }
    if target.thread_id:
        payload["message_thread_id"] = int(target.thread_id)

    request = urllib.request.Request(
        f"https://api.telegram.org/bot{target.token}/sendMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT_SEC) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Тело ответа Telegram объясняет причину точнее кода: «топик не
        # найден» и «бот не в группе» — это оба 400.
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise AlertError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise AlertError(f"{type(exc).__name__}: {exc}") from exc

    if not body.get("ok"):
        raise AlertError(str(body)[:300])
    return int(body["result"]["message_id"])


# ---------------------------------------------------------------------------
# замолчавшие аккаунты
# ---------------------------------------------------------------------------


def compose_silence_message(silenced: list[dict], *, host: str) -> str:
    """Текст про аккаунты, которые перестали писать первыми.

    Команда возврата стоит в конце и с перечислением всех номеров — чтобы её
    можно было скопировать целиком, не выписывая id из строк выше.
    """
    plural = "аккаунты" if len(silenced) > 1 else "аккаунт"
    lines = [
        f"🔇 outreach49 ({host}): Telegram отклонил отправки, "
        f"{plural} больше не пишет первым",
        "",
    ]
    for item in silenced:
        lines.append(f"MA#{item['id']} {item['label']} — {item['why']}")
        lines.append(f"    задача {item['task_id']}")
    lines.append("")
    lines.append(
        "Ответы на входящие продолжаются — молчание касается только того, что "
        "аккаунт начинает сам: рассылки и разведки."
    )
    lines.append(
        "Само оно не снимется. Вернуть в работу: bridge49 accounts --release "
        + " ".join(str(item["id"]) for item in silenced)
    )
    return "\n".join(lines)


async def announce_silence(
    store: Store, silenced: list[dict], *, actor: str = "cli",
) -> str | None:
    """Сообщить в админку. Недоставленное уведомление опрос не роняет.

    Причина, по которой здесь `try` вокруг всего: зовут отсюда опрос
    результатов — сердце контура, он ходит каждые пятнадцать секунд. Уронить
    его из-за недоступного Telegram значило бы поменять одну проблему на
    большую. Сам факт немоты записан в базу до этого вызова, и потерять его
    неудачная отправка не может.
    """
    try:
        target = TelegramTarget.from_file()
        if target is None or not target.enabled:
            return None
        message = compose_silence_message(silenced, host=socket.gethostname())
        # Отправка блокирующая: уводим её из петли событий.
        message_id = await asyncio.to_thread(send, target, message)
    except AlertError as exc:
        store.log(actor, "accounts.silence_alert_failed", "", str(exc)[:300])
        store.commit()
        return f"не доставлено: {exc}"
    except Exception as exc:  # noqa: BLE001 — см. докстроку
        store.log(actor, "accounts.silence_alert_failed", "",
                  f"{type(exc).__name__}: {exc}"[:300])
        store.commit()
        return f"не доставлено: {type(exc).__name__}"
    store.log(actor, "accounts.silence_alert_sent", target.describe(),
              str(message_id))
    store.commit()
    return f"отправлено в {target.describe()}"
