# Эксплуатация

## Установка

```bash
# 1. Код
mkdir -p /opt/bridge49
# скопировать содержимое репозитория в /opt/bridge49

# 2. Виртуальное окружение
python3 -m venv /opt/bridge49/venv
/opt/bridge49/venv/bin/pip install -r /opt/bridge49/requirements.txt

# 3. Реестр аккаунтов
cd /opt/bridge49 && bin/bridge49 accounts --sync accounts.json

# 4. Проверка
bin/bridge49 doctor
```

Требуется доступ на чтение к `/var/lib/tgradar-outreach/secrets/tgr_bridge.env` —
это тот же файл реквизитов, что использует `tgradar-outreach`. Свои реквизиты
bridge49 не заводит и копий секрета не делает.

Переопределить путь можно переменной `BRIDGE49_SECRET`, корень установки —
`BRIDGE49_HOME`, таймзону — `BRIDGE49_TZ`.

---

## Обновление снимка аккаунтов

Снимок делает оператор Radar, потому что нашей роли таблица `account`
недоступна:

```bash
# на машине с доступом к Radar read-only
python scripts/snapshot_accounts.py > accounts.json
scp accounts.json root@сервер:/opt/bridge49/accounts.json

# на сервере
cd /opt/bridge49 && bin/bridge49 accounts --sync accounts.json
```

Снимок нужен для планирования (кому какое действие можно поручить), но не
является источником прав: Radar проверяет роль и allowlist заново перед каждым
исполнением. Устаревший снимок приведёт к отказу конкретной команды, а не к
её несанкционированному выполнению.

---

## Регулярные задачи

Примеры в `systemd/`. Разумный минимум:

| Как часто | Команда | Зачем |
|---|---|---|
| каждые 5 минут | `dispatch --confirm` | выпускать созревшие задачи |
| каждые 2 минуты | `poll` | подтягивать результаты и входящие |
| раз в сутки | `accounts --sync accounts.json` | освежать реестр |

```bash
systemctl enable --now bridge49-poll.timer
systemctl enable --now bridge49-dispatch.timer
```

⚠️ Таймер `dispatch` имеет смысл включать только после `arm on`. Без файла
ARMED он будет просто печатать предпросмотр в журнал.

---

## Резервные копии

Всё состояние — один файл:

```bash
sqlite3 /opt/bridge49/var/bridge49.sqlite ".backup /var/backups/bridge49-$(date +%F).sqlite"
```

Восстановление — положить файл обратно. Ничего другого сохранять не нужно:
код лежит в git, реквизиты — в файле `tgradar-outreach`, снимок аккаунтов
пересоздаётся из Radar.

---

## Разбор проблем

### `doctor` не проходит на реквизитах

```
нет файла с реквизитами моста: /var/lib/tgradar-outreach/secrets/tgr_bridge.env
```

Файл существует, но недоступен текущему пользователю. Запускать нужно из-под
пользователя, у которого есть право чтения (обычно `root`).

### `doctor` не проходит на связи

Смотреть по порядку:

```bash
# доступен ли сам PgBouncer
nc -zv <host> 6432

# что говорит Radar
bin/bridge49 doctor
```

Частые причины: не открыт IP сервера на стороне PgBouncer, роль исчерпала
`CONNECTION LIMIT`, PgBouncer перезапускался.

### Команды ставятся, но не исполняются

Ожидаемо, если у `outreach_command` ноль лотерейных билетов — это
стоп-кран по умолчанию. Проверить можно так:

```bash
bin/bridge49 queue --state queued
bin/bridge49 poll results
```

Если команды неделями в `new` и `attempts=0`, дело именно в билетах. Их
выдаёт владелец Radar в `RESPONDER_WORKER_ACTIONS.events.outreach_command`
конкретных аккаунтов.

### Команда завершилась `skipped`

Смотреть `error_code` в `bridge49 queue`:

| Код | Что значит |
|---|---|
| `stale` | текст пролежал в очереди дольше 72 часов и потерял актуальность |
| `immediate_visible_not_allowed` | видимое действие в режиме `immediate` без разрешения аккаунта |
| `operator_cancelled` | команду снял оператор Radar |
| `request_changed` | тот же UUID переиспользован с другим телом — так нельзя |

### Задача застряла в `queued`

```bash
bin/bridge49 poll results
```

Если статус в Radar `new` — команда ждёт выпуска (см. билеты выше). Если
`processing` дольше нескольких минут — идёт исполнение либо аккаунт
переподключается. `failed/outcome_unknown` означает ручной разбор: повторять
такое действие автоматикой нельзя.

---

## Что делать НЕ надо

* **Не редактировать `Account.config` в Radar.** Нашей роли это и не доступно,
  а обход через другие креды сломает границу доверия.
* **Не перезапускать TGR-воркеры и не стартовать аккаунты.** Массовые
  переподключения — прямой риск для аккаунтов.
* **Не переигрывать команду новым UUID.** `dispatch` сам повторит тем же.
* **Не открывать порт веб-панели наружу.** Она слушает `127.0.0.1`; для
  доступа использовать SSH-туннель.

---

## Удаление

```bash
systemctl disable --now bridge49-poll.timer bridge49-dispatch.timer
rm -rf /opt/bridge49
```

На Radar это не влияет никак: bridge49 не владеет ничем на его стороне, кроме
уже поставленных команд, которые доиграются или протухнут сами.
