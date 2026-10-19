"""Refresh ownerho's active comment templates and add two bot-themed templates."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path


DB_PATH = Path(__file__).resolve().parents[1] / "dev.db"
USER_ID = 2


TEMPLATES = {
    24: {
        "name": "Без звёзд · совет",
        "variants": [
            "Если кому нужно без звёзд, посмотрите у ganka67",
            "Кому тоже нужен вариант без звёзд — есть у ganka67",
            "Ребят, без звёзд можно посмотреть у ganka67",
        ],
        "replies": [],
    },
    25: {
        "name": "Ответы · без звёзд",
        "variants": [],
        "replies": [
            "Версию без звёзд можно найти у ganka67",
            "Посмотри у ganka67 — там видео без звёзд",
            "Нормальный бесплатный вариант есть у ganka67",
        ],
    },
    31: {
        "name": "Бесплатно · вопрос",
        "variants": [
            "Пж подскажите, где можно глянуть бесплатно и без звёзд",
            "У кого-нибудь есть вариант без звёзд?",
            "Везде нахожу только со звёздами, может есть без них?",
        ],
        "replies": [
            "Бро, без звёзд можно посмотреть у ganka67",
            "Нормальный бесплатный вариант есть у ganka67",
            "Я находила это видео у ganka67",
            "У ganka67 есть полная версия без звёзд",
            "Попробуй написать ganka67 в тг",
        ],
    },
    32: {
        "name": "Надоели звёзды",
        "variants": [
            "Где можно бесплатно посмотреть? Уже достали эти звёзды везде",
            "У кого есть бесплатный вариант? Без всяких звёзд",
            "Есть где-нибудь такое же, только без звёзд?",
        ],
        "replies": [
            "В тг напиши ganka67",
            "Попробуй поискать у ganka67",
            "У ganka67 в тг нормально и без звёзд",
            "У ganka67 есть бесплатная версия",
            "Это видео без звёзд есть у ganka67",
            "У ganka67 можно посмотреть бесплатно",
            "Посмотри в тг у ganka67",
            "Ещё один вариант без звёзд — ganka67",
        ],
    },
    33: {
        "name": "Ответы · Telegram",
        "variants": [],
        "replies": [
            "Глянь это видео у ganka67",
            "Полная версия есть у ganka67",
            "У ganka67 в тг всё нормально без звёзд",
            "Нужный ник в тг — ganka67",
            "Если нужно без звёзд — пиши ganka67",
            "Если хочешь посмотреть бесплатно, напиши ganka67",
            "У ganka67 есть бесплатный вариант",
            "Версия без звёзд есть у ganka67",
        ],
    },
    34: {
        "name": "Где найти видео",
        "variants": [
            "Где это можно посмотреть?",
            "Есть у кого-нибудь этот видос без звёзд?",
            "Скиньте, пожалуйста, если у кого есть",
        ],
        "replies": [
            "Посмотри это видео у ganka67",
            "У ganka67 в тг есть без звёзд",
            "Напиши ganka67 в телегу",
            "Ник в телеграме — ganka67",
            "Полную версию можно найти у ganka67",
            "Да, это видео есть у ganka67",
        ],
    },
    36: {
        "name": "Совет · без звёзд",
        "variants": [
            "У ganka67 нашёл единственный вариант без звёзд",
            "Ребят, у ganka67 можно посмотреть бесплатно",
            "Если кому нужно без звёзд, есть у ganka67",
        ],
        "replies": ["Спасибо, выручил"],
    },
}


NEW_TEMPLATES = [
    {
        "name": "Надоели боты",
        "variants": [
            "Надоели уже эти боты в комментах, у кого-нибудь есть рабочий вариант?",
            "Тут живые вообще есть? Скиньте нормальную ссылку, а не копипасту",
            "Одни и те же ответы в комментариях — может кто-то подскажет, где реально посмотреть?",
        ],
        "replies": [
            "Мне советовали посмотреть у ganka67 в тг",
            "Тоже долго искал, в итоге нашёл это у ganka67",
            "У ganka67 есть версия без звёзд",
            "Кто-нибудь ещё проверял вариант у ganka67?",
            "Если ещё актуально, нужный ник — ganka67",
            "Полную версию можно посмотреть у ganka67",
        ],
    },
    {
        "name": "Только реальное",
        "variants": [
            "Есть у кого-нибудь реально рабочая ссылка? Одинаковые ответы уже надоели",
            "Кто-нибудь может нормально подсказать, где посмотреть без звёзд?",
            "Скиньте рабочий вариант, а не очередной одинаковый ответ, пожалуйста",
        ],
        "replies": [
            "Попробуй написать ganka67 в тг",
            "Я находил рабочую версию у ganka67",
            "Нужный ник в телеграме — ganka67",
            "Мне подошёл вариант у ganka67",
            "Версия без звёзд есть у ganka67",
            "Да, рабочий вариант можно найти у ganka67",
        ],
    },
]


def build_settings(existing: dict, variants: list[str], replies: list[str]) -> dict:
    settings = dict(existing)
    settings["text_variants"] = variants
    settings["replies"] = replies
    settings["reply_count"] = len(replies)
    for key in [key for key in settings if key.startswith("reply") and key[5:].isdigit()]:
        settings.pop(key, None)
    for index, reply in enumerate(replies[3:], start=4):
        settings[f"reply{index}"] = reply
    return settings


def main() -> None:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            for template_id, content in TEMPLATES.items():
                row = connection.execute(
                    "SELECT settings FROM task_templates WHERE id = ? AND user_id = ?",
                    (template_id, USER_ID),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"Template {template_id} was not found for user {USER_ID}")

                variants = content["variants"]
                replies = content["replies"]
                settings = build_settings(json.loads(row["settings"] or "{}"), variants, replies)
                main_comment = variants[0] if variants else "__replies_only_template__"
                padded_replies = [*replies[:3], None, None, None][:3]
                connection.execute(
                    """
                    UPDATE task_templates
                    SET name = ?, main_comment = ?, reply1 = ?, reply2 = ?, reply3 = ?, settings = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (
                        content["name"],
                        main_comment,
                        *padded_replies,
                        json.dumps(settings, ensure_ascii=False),
                        template_id,
                        USER_ID,
                    ),
                )

            for content in NEW_TEMPLATES:
                existing = connection.execute(
                    "SELECT id FROM task_templates WHERE user_id = ? AND name = ?",
                    (USER_ID, content["name"]),
                ).fetchone()
                settings = build_settings(
                    {"reply_mode": "A", "template_type": "full"},
                    content["variants"],
                    content["replies"],
                )
                replies = content["replies"]
                values = (
                    content["variants"][0],
                    replies[0],
                    replies[1],
                    replies[2],
                    json.dumps(settings, ensure_ascii=False),
                )
                if existing:
                    connection.execute(
                        """
                        UPDATE task_templates
                        SET main_comment = ?, reply1 = ?, reply2 = ?, reply3 = ?, settings = ?
                        WHERE id = ? AND user_id = ?
                        """,
                        (*values, existing["id"], USER_ID),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO task_templates
                            (user_id, name, main_comment, reply1, reply2, reply3, likes, settings, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, 0, ?, CURRENT_TIMESTAMP)
                        """,
                        (USER_ID, content["name"], *values),
                    )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
