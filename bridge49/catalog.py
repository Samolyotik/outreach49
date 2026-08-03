"""Каталог действий моста Radar TGR.

Это локальное зеркало контракта `docs/OUTREACH_BRIDGE.md` §3. Оно нужно
только чтобы отсеять заведомо невалидную команду ДО обращения к базе:
источник истины остаётся на стороне Radar, который проверит всё заново.

Держим таблицу декларативной и плоской — её должно быть легко читать и
править одному человеку.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Риски. Определяют, что увидит собеседник, и как Radar будет темпировать выпуск.
# ---------------------------------------------------------------------------

RISK_READ = "read"            # ничего не видно снаружи
RISK_SOFT = "soft"            # read receipt / typing
RISK_VISIBLE = "visible"      # join / отправка в публичный чат
RISK_MATURE_DM = "mature_dm"  # личное сообщение через зрелый DM-контур

#: Риски, для которых Radar применяет рампу и потолок частоты.
PACED_RISKS = frozenset({RISK_VISIBLE, RISK_MATURE_DM})

#: Риски, которые нельзя пускать в mode=immediate без явного account-local
#: разрешения (`allow_immediate_visible_actions`).
IMMEDIATE_GATED_RISKS = PACED_RISKS | {RISK_SOFT}

ROLES = (
    "channel_sender",
    "chat_sender",
    "dm_sender",
    "private_reader",
    "source_finder",
    "source_reader",
)

#: Роли, которые разрешено совмещать на одном аккаунте.
SENDER_FAMILY = frozenset({"channel_sender", "chat_sender", "dm_sender"})

#: Find Groups bot: зафиксирован контрактом, подменить нельзя.
SOURCE_FINDER_BOT_ID = 7750789444

MAX_TEXT_UTF16 = 4096
MAX_CAPTION_UTF16 = 1024


@dataclass(frozen=True)
class Action:
    """Одно действие моста."""

    name: str
    risk: str
    roles: frozenset[str]
    summary: str
    #: Параметры, без которых команда точно будет отклонена.
    required: tuple[str, ...] = ()
    #: Разрешённые необязательные параметры.
    optional: tuple[str, ...] = ()
    #: Принимает ли действие общий селектор чата (username / chat_id+peer_kind).
    selector: bool = False
    #: Требует ли непустой текст (или caption при вложении).
    needs_text: bool = False
    #: Допускает ли флаги online/typing.
    activity_flags: bool = False

    @property
    def visible(self) -> bool:
        return self.risk in PACED_RISKS


def _a(*args, **kwargs) -> Action:
    return Action(*args, **kwargs)


#: Полный registry. Порядок — как в контракте, чтобы диффы были читаемыми.
ACTIONS: dict[str, Action] = {
    action.name: action
    for action in (
        _a("command_dry_run", RISK_READ, frozenset(ROLES),
           "Эхо имён параметров. Ни одного обращения к Telegram."),
        _a("gateway_capabilities", RISK_READ, frozenset(ROLES),
           "Вернуть registry, роль и фактический allowlist аккаунта. "
           "Обращений к Telegram нет."),
        _a("get_me", RISK_READ, frozenset(ROLES),
           "Кто этот аккаунт. Единственный read-RPC, реально идущий в Telegram."),

        _a("get_chat", RISK_READ, frozenset({"private_reader"}),
           "Метаданные чата.", selector=True),
        _a("create_private_chat", RISK_READ, frozenset({"dm_sender"}),
           "Резолв пользователя в entity cache. Сообщений не шлёт.",
           optional=("username", "user_id", "chat_id", "peer_kind")),
        _a("search_public_chat", RISK_READ,
           frozenset({"chat_sender", "source_finder", "source_reader",
                      "private_reader"}),
           "Поиск публичного чата по username.", required=("username",)),
        _a("get_supergroup", RISK_READ,
           frozenset({"chat_sender", "source_finder", "source_reader",
                      "private_reader"}),
           "Метаданные супергруппы/канала.", selector=True),
        _a("get_supergroup_full_info", RISK_READ, frozenset({"private_reader"}),
           "Полная информация о супергруппе/канале.", selector=True),

        _a("check_channel_dm_metadata", RISK_READ,
           frozenset({"channel_sender", "source_reader"}),
           "Есть ли у канала бесплатный monoforum для Channel DM.",
           required=("username",)),
        _a("resolve_channel_dm", RISK_READ,
           frozenset({"channel_sender", "source_reader"}),
           "То же самое, второе имя из лексикона внешнего пайплайна.",
           required=("username",)),
        _a("send_channel_dm", RISK_MATURE_DM, frozenset({"channel_sender"}),
           "Написать в monoforum публичного канала.",
           required=("username",),
           optional=("text", "target_channel_tg_id", "target_monoforum_tg_id",
                     "online", "typing", "attachments"),
           needs_text=True, activity_flags=True),
        _a("sync_channel_dm_replies", RISK_READ, frozenset({"channel_sender"}),
           "Догнать пропущенные ответы в известных monoforum.",
           optional=("limit_per_dialog",)),
        _a("sync_private_dm_replies", RISK_READ,
           frozenset({"channel_sender", "dm_sender", "chat_sender"}),
           "Догнать пропущенные ответы в личных диалогах.",
           optional=("limit_per_dialog",)),

        # chat_sender добавлен 03.08.2026 решением владельца: у трёх аккаунтов
        # остались личные диалоги, которые они же и вели, а `reply_private_dm`
        # для них недоступен — он берёт адресата из входящего уведомления
        # Radar, а таких уведомлений у этих аккаунтов нет и не появится.
        # Право писать в личку первым это всё-таки расширяет, поэтому Radar
        # проверяет свой allowlist независимо и остаётся последним словом.
        _a("send_private_dm", RISK_MATURE_DM,
           frozenset({"dm_sender", "chat_sender"}),
           "Личное сообщение пользователю.",
           optional=("username", "target_user_tg_id", "text", "online",
                     "typing", "attachments"),
           needs_text=True, activity_flags=True),
        _a("reply_private_dm", RISK_MATURE_DM,
           frozenset({"channel_sender", "dm_sender", "chat_sender"}),
           "Ответ на входящее ЛС. Адресат берётся из самого входящего.",
           required=("inbound_notification_id",),
           optional=("text", "online", "typing", "attachments"),
           needs_text=True, activity_flags=True),

        _a("mark_messages_read", RISK_SOFT,
           frozenset({"channel_sender", "dm_sender", "chat_sender"}),
           "Отметить сообщения прочитанными.", selector=True,
           optional=("message_id", "message_ids", "force_read")),
        _a("view_messages", RISK_SOFT,
           frozenset({"channel_sender", "dm_sender", "chat_sender"}),
           "То же самое, второе имя.", selector=True,
           optional=("message_id", "message_ids", "force_read")),
        _a("send_typing", RISK_SOFT,
           frozenset({"channel_sender", "dm_sender", "chat_sender"}),
           "Показать «печатает».", selector=True, optional=("action_type",)),
        _a("send_chat_action", RISK_SOFT,
           frozenset({"channel_sender", "dm_sender", "chat_sender"}),
           "То же самое, второе имя.", selector=True, optional=("action_type",)),

        _a("check_public_chat_metadata", RISK_READ, frozenset({"source_reader"}),
           "Метаданные публичного чата.", selector=True),
        _a("inspect_public_chat_target", RISK_READ, frozenset({"chat_sender"}),
           "Снимок членства этого аккаунта в чате.", selector=True),
        _a("refresh_public_chat_membership", RISK_READ, frozenset({"chat_sender"}),
           "То же самое, второе имя.", selector=True),
        _a("confirm_public_chat_membership", RISK_READ, frozenset({"chat_sender"}),
           "То же самое, третье имя.", selector=True),
        _a("check_deferred_public_chat_membership", RISK_READ,
           frozenset({"chat_sender"}),
           "То же самое, четвёртое имя.", selector=True),
        _a("join_public_chat", RISK_VISIBLE, frozenset({"chat_sender"}),
           "Вступить в публичную супергруппу.", selector=True),
        _a("send_public_chat_message", RISK_VISIBLE, frozenset({"chat_sender"}),
           "Сообщение в публичную супергруппу. Radar сам проверит членство "
           "и при необходимости сделает один bounded auto-join.",
           selector=True,
           optional=("text", "text_sha256", "online", "typing", "attachments"),
           needs_text=True, activity_flags=True),
        _a("verify_public_chat_message", RISK_READ, frozenset({"chat_sender"}),
           "Проверить, что сообщение на месте.", selector=True,
           required=("message_id",)),

        _a("get_chat_history", RISK_READ,
           frozenset({"source_finder", "private_reader"}),
           "Ограниченная история чата.", selector=True,
           optional=("limit", "from_message_id", "offset", "only_local")),
        _a("collect_private_club_contacts", RISK_READ,
           frozenset({"private_reader"}),
           "Уникальные не-бот отправители чата (без текста сообщений).",
           selector=True, optional=("limit",)),

        _a("source_finder_bot_send_text", RISK_VISIBLE,
           frozenset({"source_finder"}),
           "Написать Find Groups bot.", required=("text",),
           optional=("bot_id",)),
        _a("source_finder_bot_callback", RISK_VISIBLE,
           frozenset({"source_finder"}),
           "Нажать inline-кнопку в ответе бота.",
           required=("message_id", "data"), optional=("bot_id",)),

        _a("download_message_media", RISK_READ,
           frozenset({"source_finder", "private_reader"}),
           "Скачать вложение сообщения в artifact-таблицу.",
           selector=True, required=("message_id",)),
    )
}

#: Действия, которые вообще не обращаются к Telegram.
NO_TELEGRAM_ACTIONS = frozenset({"command_dry_run", "gateway_capabilities"})

#: Чтение, которое реально идёт в Telegram. У него нет риска для собеседника,
#: но есть собственный лимит на стороне Telegram: resolve имени считается
#: отдельно от отправки и не прощает залпа. Эхо (`command_dry_run`,
#: `gateway_capabilities`) сюда не входит — оно не выходит за пределы Radar.
READ_ACTIONS = frozenset(
    name for name, action in ACTIONS.items()
    if action.risk == RISK_READ and name not in NO_TELEGRAM_ACTIONS
)

#: Селекторные ключи, любой из которых открывает общий селектор.
SELECTOR_KEYS = ("username", "chat_id", "supergroup_id", "user_id")

PEER_KINDS = frozenset({"channel", "chat", "user"})

CHAT_ACTION_TYPES = frozenset({
    "typing", "contact", "game", "location", "photo", "record-audio",
    "record-round", "record-video", "audio", "round", "video", "document",
    "cancel",
})


def actions_for_role(role: str) -> list[str]:
    """Все действия, доступные роли, в стабильном порядке."""
    return sorted(name for name, action in ACTIONS.items() if role in action.roles)


def utf16_len(text: str) -> int:
    """Длина в UTF-16 code units — именно так её считает Telegram."""
    return len(text.encode("utf-16-le")) // 2


class ValidationError(ValueError):
    """Команда точно будет отклонена Radar — не тратим на неё enqueue."""


def validate(
    action_name: str,
    params: dict,
    *,
    roles: frozenset[str] | set[str],
    allowed_actions: frozenset[str] | set[str] | None = None,
    has_attachment: bool = False,
) -> Action:
    """Проверить команду локально. Бросает :class:`ValidationError`.

    Это не замена серверным проверкам, а способ не засорять очередь заведомым
    мусором и получать понятную ошибку в терминале, а не в JSON-результате
    через десять минут.
    """
    action = ACTIONS.get(action_name)
    if action is None:
        raise ValidationError(f"неизвестное действие: {action_name}")

    if not action.roles & set(roles):
        raise ValidationError(
            f"{action_name} требует одну из ролей "
            f"{sorted(action.roles)}, у аккаунта {sorted(roles)}"
        )

    if allowed_actions is not None and action_name not in allowed_actions:
        raise ValidationError(
            f"{action_name} отсутствует в allowed_actions аккаунта"
        )

    known = set(action.required) | set(action.optional)
    if action.selector:
        known |= set(SELECTOR_KEYS) | {"peer_kind"}

    unknown = sorted(set(params) - known)
    if unknown:
        raise ValidationError(
            f"{action_name}: неизвестные параметры {unknown}; "
            f"допустимы {sorted(known)}"
        )

    missing = [key for key in action.required if params.get(key) in (None, "")]
    if missing:
        raise ValidationError(f"{action_name}: не хватает параметров {missing}")

    if action.selector and not any(params.get(key) for key in SELECTOR_KEYS):
        raise ValidationError(
            f"{action_name}: нужен селектор — один из {list(SELECTOR_KEYS)}"
        )

    peer_kind = params.get("peer_kind")
    if peer_kind is not None and peer_kind not in PEER_KINDS:
        raise ValidationError(
            f"peer_kind должен быть одним из {sorted(PEER_KINDS)}"
        )

    if action.needs_text:
        text = params.get("text")
        if has_attachment:
            # С вложением text становится подписью: допустима пустая строка.
            if text is not None:
                if not isinstance(text, str):
                    raise ValidationError("text должен быть строкой")
                if text and not text.strip():
                    raise ValidationError("подпись не может быть из одних пробелов")
                if utf16_len(text) > MAX_CAPTION_UTF16:
                    raise ValidationError(
                        f"подпись длиннее {MAX_CAPTION_UTF16} UTF-16 code units"
                    )
        else:
            if not isinstance(text, str) or not text.strip():
                raise ValidationError(f"{action_name}: нужен непустой text")
            if utf16_len(text) > MAX_TEXT_UTF16:
                raise ValidationError(
                    f"text длиннее {MAX_TEXT_UTF16} UTF-16 code units "
                    f"(сейчас {utf16_len(text)})"
                )

    for flag in ("online", "typing"):
        if flag in params:
            if not action.activity_flags:
                raise ValidationError(
                    f"{action_name} не принимает флаг {flag} "
                    "(даже со значением false)"
                )
            if not isinstance(params[flag], bool):
                raise ValidationError(f"{flag} должен быть JSON boolean")

    action_type = params.get("action_type")
    if action_type is not None and action_type not in CHAT_ACTION_TYPES:
        raise ValidationError(
            f"action_type должен быть одним из {sorted(CHAT_ACTION_TYPES)}"
        )

    if action_name.startswith("source_finder_bot_"):
        bot_id = params.get("bot_id")
        if bot_id is not None and int(bot_id) != SOURCE_FINDER_BOT_ID:
            raise ValidationError(
                f"bot_id зафиксирован контрактом: {SOURCE_FINDER_BOT_ID}"
            )

    limit = params.get("limit_per_dialog", params.get("limit"))
    if limit is not None and not (1 <= int(limit) <= 500):
        raise ValidationError("limit должен быть в диапазоне 1..500")

    return action
