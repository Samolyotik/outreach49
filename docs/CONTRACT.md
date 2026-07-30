# Контракт моста

Шпаргалка по тому, что мост Radar принимает и возвращает. Полный текст
контракта живёт в репозитории Radar (`docs/OUTREACH_BRIDGE.md`); здесь — то,
что нужно в повседневной работе.

---

## Пять поверхностей, и больше ничего

| Что | Тип | Зачем |
|---|---|---|
| `enqueue_responder_outreach_command(bigint, jsonb, timestamptz)` | функция | поставить команду |
| `enqueue_responder_outreach_command_attachment(…, bytea, …)` | функция | то же с одним файлом |
| `responder_outreach_command_results` | view | статус и результат |
| `responder_outreach_inbound_feed` | view | входящие сообщения |
| `responder_outreach_artifacts` | view | скачанные байты |

Прямая запись в `system_notification` невозможна и не нужна: функция сама
выставляет все колонки, выводит `business_id` из аккаунта и проверяет роль.
Права на изменение `Account.config`, статусов и результатов у нашей роли нет —
и это правильно.

---

## Конверт команды

```json
{
  "schema": "tgr.outreach.command",
  "version": 1,
  "request": {
    "request_id": "UUID",
    "action": "send_private_dm",
    "mode": "lottery",
    "params": { "username": "ivan", "text": "Здравствуйте!" },
    "expires_at": "2026-08-01T12:00:00+03:00",
    "external_job_id": "cmp_…",
    "external_conversation_id": "c_…"
  }
}
```

Обязательны `request_id`, `action`, `mode`, `params`. Неизвестные поля
запрещены. `external_job_id` и `external_conversation_id` bridge49 заполняет
идентификаторами кампании и контакта — именно по ним потом сходятся входящие.

После первой попытки `request` неизменяем. **Повтор с тем же UUID и тем же
телом идемпотентен** и возвращает прежний `command_id`; повтор с другим телом
даёт `request_changed`. Это единственный правильный способ переиграть
сомнительный таймаут.

---

## Режимы выпуска

| Режим | Когда исполнится |
|---|---|
| `lottery` | когда системному действию `outreach_command` выпадет лотерейный билет. По умолчанию билетов **ноль** — очередь копится и ждёт |
| `immediate` | почти сразу, на ближайшем тике планировщика аккаунта |
| force оператора | `/tg_force_responder_action <account> outreach_command 1` в админ-боте Radar — заберёт старейшую созревшую команду любого режима |

`available_at` работает как «не раньше» во всех трёх случаях.

⚠️ У всех 49 наших аккаунтов включён `allow_immediate_visible_actions`,
поэтому `immediate` действительно означает «сейчас», в том числе для отправок.

---

## Жизненный цикл команды

```
new → processing → done | failed | skipped
         └─────────→ new     (безопасный повтор: FloodWait, read-ошибка)
```

* `done` — исполнено; смотреть `result.outcome`: `succeeded` либо `rejected`;
* `skipped` — детерминированный отказ: схема, срок, политика, цель, отмена;
* `failed` — попытки исчерпаны либо результат необратимого RPC не доказан.

`outcome_unknown` всегда сопровождается ручным разбором: **повторять такое
действие нельзя**, оно может задвоить видимый эффект.

---

## Действия, роли и риски

Риск определяет, что увидит собеседник, и как Radar темпирует выпуск:

* **read** — снаружи не видно;
* **soft** — отметка о прочтении, «печатает»;
* **visible** — вступление в чат, сообщение в публичную группу;
* **mature_dm** — личное сообщение через зрелый DM-контур.

Рампа и потолок частоты применяются только к `visible` и `mature_dm`.

| Действие | Риск | Роли | Параметры | Что делает |
|---|---|---|---|---|
| `command_dry_run` | read | все | — | Эхо имён параметров. Ни одного обращения к Telegram. |
| `gateway_capabilities` | read | все | — | Вернуть registry, роль и фактический allowlist аккаунта. Обращений к Telegram нет. |
| `get_me` | read | все | — | Кто этот аккаунт. Единственный read-RPC, реально идущий в Telegram. |
| `check_channel_dm_metadata` | read | `channel_sender`, `source_reader` | `username` | Есть ли у канала бесплатный monoforum для Channel DM. |
| `resolve_channel_dm` | read | `channel_sender`, `source_reader` | `username` | То же самое, второе имя из лексикона внешнего пайплайна. |
| `check_public_chat_metadata` | read | `source_reader` | селектор | Метаданные публичного чата. |
| `search_public_chat` | read | `chat_sender`, `private_reader`, `source_finder`, `source_reader` | `username` | Поиск публичного чата по username. |
| `get_supergroup` | read | `chat_sender`, `private_reader`, `source_finder`, `source_reader` | селектор | Метаданные супергруппы/канала. |
| `get_supergroup_full_info` | read | `private_reader` | селектор | Полная информация о супергруппе/канале. |
| `get_chat` | read | `private_reader` | селектор | Метаданные чата. |
| `get_chat_history` | read | `private_reader`, `source_finder` | селектор | Ограниченная история чата. |
| `collect_private_club_contacts` | read | `private_reader` | селектор | Уникальные не-бот отправители чата (без текста сообщений). |
| `create_private_chat` | read | `dm_sender` | — | Резолв пользователя в entity cache. Сообщений не шлёт. |
| `inspect_public_chat_target` | read | `chat_sender` | селектор | Снимок членства этого аккаунта в чате. |
| `refresh_public_chat_membership` | read | `chat_sender` | селектор | То же самое, второе имя. |
| `confirm_public_chat_membership` | read | `chat_sender` | селектор | То же самое, третье имя. |
| `check_deferred_public_chat_membership` | read | `chat_sender` | селектор | То же самое, четвёртое имя. |
| `verify_public_chat_message` | read | `chat_sender` | селектор + `message_id` | Проверить, что сообщение на месте. |
| `sync_channel_dm_replies` | read | `channel_sender` | — | Догнать пропущенные ответы в известных monoforum. |
| `sync_private_dm_replies` | read | `channel_sender`, `chat_sender`, `dm_sender` | — | Догнать пропущенные ответы в личных диалогах. |
| `download_message_media` | read | `private_reader`, `source_finder` | селектор + `message_id` | Скачать вложение сообщения в artifact-таблицу. |
| `mark_messages_read` | soft | `channel_sender`, `chat_sender`, `dm_sender` | селектор | Отметить сообщения прочитанными. |
| `view_messages` | soft | `channel_sender`, `chat_sender`, `dm_sender` | селектор | То же самое, второе имя. |
| `send_typing` | soft | `channel_sender`, `chat_sender`, `dm_sender` | селектор | Показать «печатает». |
| `send_chat_action` | soft | `channel_sender`, `chat_sender`, `dm_sender` | селектор | То же самое, второе имя. |
| `join_public_chat` | visible | `chat_sender` | селектор | Вступить в публичную супергруппу. |
| `send_public_chat_message` | visible | `chat_sender` | селектор + текст | Сообщение в публичную супергруппу. Radar сам проверит членство и при необходимости сделает один bounded auto-join. |
| `source_finder_bot_send_text` | visible | `source_finder` | `text` | Написать Find Groups bot. |
| `source_finder_bot_callback` | visible | `source_finder` | `message_id`, `data` | Нажать inline-кнопку в ответе бота. |
| `send_private_dm` | mature_dm | `dm_sender` | `username` или `target_user_tg_id` + текст | Личное сообщение пользователю. |
| `send_channel_dm` | mature_dm | `channel_sender` | `username` + текст | Написать в monoforum публичного канала. |
| `reply_private_dm` | mature_dm | `channel_sender`, `chat_sender`, `dm_sender` | `inbound_notification_id` + текст | Ответ на входящее ЛС. Адресат берётся из самого входящего. |

Таблица сгенерирована из `bridge49/catalog.py`, поэтому расходиться с кодом
не может. Актуальный вид всегда доступен командой `bridge49 actions`.

### Общий селектор

```json
{"username": "public_name"}
{"chat_id": 123456789, "peer_kind": "channel"}
```

`peer_kind` — `channel` (по умолчанию), `chat` или `user`. Для пользователя
и обычной группы его нужно указывать явно. Алиасы `supergroup_id` и `user_id`
работают наравне с `chat_id`.

---

## Ограничения текста

| | Обычное сообщение | Подпись к вложению |
|---|---|---|
| длина | 1..4096 UTF-16 code units | 0..1024 |
| пустой текст | запрещён | допустим |

UTF-16 — не то же, что символы: эмодзи вне BMP считаются за две единицы.
`bridge49` считает так же, поэтому длинный текст отсекается на планировании,
а не в результате команды.

---

## Лимиты очереди

| Ограничение | Значение |
|---|---|
| активных команд на аккаунт | 1 000 |
| активных команд на весь мост | 10 000 |
| всего сохранённых команд | 20 000 |

`SQLSTATE 55P03` — короткая конкуренция, повторяется автоматически с
джиттером. `SQLSTATE 54000` — очередь заполнена; новые UUID должны ждать,
пока она разгрузится.

---

## Входящие

Приходят отдельными строками в `responder_outreach_inbound_feed`, читаются
возрастающим курсором по `id`. В конверте — поверхность (`private_dm` или
`channel_dm`), собеседник, текст, метаданные вложений и корреляция с исходной
командой.

Байты вложений в фид не попадают: для них ставится отдельная команда
`download_message_media`.

Публикуются только те аккаунты, у которых включён `publish_inbound`. У нас
это 36 отправителей из 49; 13 `source_reader` входящих не публикуют, потому
что им и не пишут.
