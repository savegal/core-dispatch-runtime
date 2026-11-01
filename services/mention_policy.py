"""Explicit per-order mention policy, independent of browser and bot runtimes."""
from contextvars import ContextVar
import re


MENTION_MODE_KEY = "mention_mode"
PLAIN_TEXT = "plain"
ATTEMPT_MENTIONS = "attempt"
MAX_MENTIONS_PER_COMMENT = 3
_MENTION_MODE = ContextVar("publication_mention_mode", default=PLAIN_TEXT)
_ORDER_MENTION_IDENTITIES = ContextVar("order_mention_identities", default=None)
_MENTION_RE = re.compile(r"(?<![\w@.])@([A-Za-z0-9_.]+)")


def find_mentions(text):
    """Find complete handles; email addresses, URLs and truncated names are text."""
    found = []
    for match in _MENTION_RE.finditer(text or ""):
        prefix = re.split(r"\s", text[:match.start()])[-1]
        if "://" in prefix or prefix.startswith("www."):
            continue
        username = match.group(1).rstrip(".")
        if 2 <= len(username) <= 24:
            found.append((match.start(), match.start() + 1 + len(username), username))
    return found


def normalize_mention_mode(value):
    if value is None:
        return PLAIN_TEXT
    if value not in (PLAIN_TEXT, ATTEMPT_MENTIONS):
        raise ValueError('mention_mode must be "plain" or "attempt"')
    return value


def current_mention_mode():
    return _MENTION_MODE.get()


def set_task_mention_mode(settings):
    value = settings.get(MENTION_MODE_KEY) if isinstance(settings, dict) else None
    mode = normalize_mention_mode(value)
    return _MENTION_MODE.set(mode), _ORDER_MENTION_IDENTITIES.set({})


def reset_task_mention_mode(token):
    if token is not None:
        mode_token, identities_token = token
        _ORDER_MENTION_IDENTITIES.reset(identities_token)
        _MENTION_MODE.reset(mode_token)


def order_mention_identities():
    return _ORDER_MENTION_IDENTITIES.get()
