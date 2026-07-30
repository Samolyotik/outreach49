"""Демо-данные: сквозной прогон без единого обращения к Telegram.

Создаются две кампании:

* **demo-dryrun** — действие ``command_dry_run``. Оно вообще не трогает
  Telegram: Radar разбирает конверт, возвращает имена параметров и закрывает
  команду. Её можно реально выпустить и посмотреть весь путь целиком.
* **demo-dm** — обычная личная рассылка, оставлена в статусе ``draft``.
  Её план видно, но диспетчер её не возьмёт, пока статус не переведут в
  ``active`` руками.

Контакты намеренно с несуществующими username: если кто-то всё же попробует
отправить, Telegram просто не найдёт адресата.
"""
from __future__ import annotations

from . import entities
from .store import Store

DEMO_TEMPLATE = (
    "Здравствуйте, {name}!\n\n"
    "Меня зовут Дмитрий, я из TG RADAR. Мы помогаем находить заявки "
    "в Telegram-чатах по вашей тематике — без ручного мониторинга.\n\n"
    "Если интересно, расскажу за пару минут, как это выглядит. "
    "Если нет — просто напишите «не надо», больше не побеспокою."
)

DEMO_CONTACTS = [
    ("demo_lead_alpha", "Алексей", "ООО Пример"),
    ("demo_lead_bravo", "Марина", "Пример-Строй"),
    ("demo_lead_delta", "Сергей", "Пример-Авто"),
    ("demo_lead_echo", "Ольга", "Пример-Юр"),
    ("demo_lead_foxtrot", "Игорь", "Пример-Недвижимость"),
]


def seed(store: Store, *, actor: str = "demo") -> dict:
    template = entities.add_template(
        store, "Демо: первое касание", DEMO_TEMPLATE,
        note="пример текста, замените своим", template_id="t_demo_first",
        actor=actor,
    )

    added = 0
    for username, name, company in DEMO_CONTACTS:
        entities.add_contact(
            store, username=username, display_name=name, company=company,
            segment="demo", tags=["demo"], actor=actor,
        )
        added += 1

    dryrun = entities.add_campaign(
        store, name="Демо: сухой прогон", action="command_dry_run",
        segment="demo", mode="lottery", daily_cap=10, per_account_daily_cap=5,
        campaign_id="cmp_demo_dryrun", actor=actor,
        note="безопасно выпускать: обращений к Telegram нет вообще",
    )
    entities.set_campaign_status(store, dryrun["id"], "active", actor=actor)

    entities.add_campaign(
        store, name="Демо: личные сообщения", action="send_private_dm",
        template_id=template["id"], segment="demo", mode="lottery",
        daily_cap=20, per_account_daily_cap=6, campaign_id="cmp_demo_dm",
        actor=actor,
        note="оставлена в draft — диспетчер такую не возьмёт",
    )

    return {
        "template_id": template["id"],
        "contacts": added,
        "campaign_id": dryrun["id"],
        "draft_campaign_id": "cmp_demo_dm",
        "segment": "demo",
    }
