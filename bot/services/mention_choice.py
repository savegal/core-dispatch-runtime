"""Ask for mention consent at the final step, bound to the exact order input."""
from dataclasses import dataclass
import hashlib
import json
import math
import time
import uuid

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from services.mention_policy import find_mentions, PLAIN_TEXT, ATTEMPT_MENTIONS, MAX_MENTIONS_PER_COMMENT


CALLBACK_PREFIX = "order:mentions:"
PENDING_KEY = "pending_mention_choice"
SELECTED_KEY = "selected_mention_choice"
CHOICE_TTL_SECONDS = 3600


def choice_is_fresh(choice):
    if not isinstance(choice, dict) or not isinstance(choice.get("token"), str):
        return False
    created = choice.get("created_at")
    return (type(created) in (int, float) and math.isfinite(created)
            and 0 <= time.time() - created <= CHOICE_TTL_SECONDS)


@dataclass(frozen=True)
class MentionChoice:
    mode: str = PLAIN_TEXT
    idempotency_key: str | None = None

    @property
    def request_kwargs(self):
        return {"idempotency_key": self.idempotency_key} if self.idempotency_key else {}


async def ensure_mention_choice(message, state, texts, *, user_id, resume, scope=None):
    texts = [str(text) for text in texts if text]
    mentions = [find_mentions(text) for text in texts]
    if not any(mentions):
        return MentionChoice()
    fingerprint = hashlib.sha256(json.dumps([user_id, resume, scope, texts], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    data = await state.get_data()
    data = data if isinstance(data, dict) else {}
    selected = data.get(SELECTED_KEY) or {}
    if (choice_is_fresh(selected) and selected.get("fingerprint") == fingerprint
            and selected.get("mode") in (PLAIN_TEXT, ATTEMPT_MENTIONS)
            ):
        return MentionChoice(selected["mode"], "mentions-" + selected["token"])
    if message is None:
        return None
    token = uuid.uuid4().hex
    pending = {"token": token, "fingerprint": fingerprint, "resume": resume,
               "created_at": time.time(), "user_id": user_id,
               "message_id": message.message_id, "chat_id": message.chat.id,
               "over_limit": any(len(items) > MAX_MENTIONS_PER_COMMENT for items in mentions)}
    await state.update_data(**{PENDING_KEY: pending, SELECTED_KEY: None})
    await message.edit_text(
        "В текстах есть @username.\n\n"
        "Попробовать сделать их отметками или оставить обычным текстом?\n\n"
        "Если пользователя не удастся найти, комментарий с отметкой не отправится.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Попробовать отметить", callback_data=f"{CALLBACK_PREFIX}attempt:{token}")],
            [InlineKeyboardButton(text="Оставить как текст", callback_data=f"{CALLBACK_PREFIX}plain:{token}")],
            [InlineKeyboardButton(text="Отмена", callback_data="menu:main")],
        ]), parse_mode=None,
    )
    return None
