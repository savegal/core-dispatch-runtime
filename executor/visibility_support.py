"""Account identity evidence shared by both HTTP visibility modes."""
import hashlib
from urllib.parse import unquote


def identity_is_known(keys):
    return any(family in {"uid", "user_id", "username"} and bool(value) for family, value in keys)


def identities_independent(candidate, references):
    """Different session tokens alone do not prove different TikTok users."""
    if not identity_is_known(candidate):
        return False
    for reference in references:
        if not identity_is_known(reference):
            return False
        if candidate & reference:
            return False
        common = {family for family, _ in candidate} & {family for family, _ in reference} & {"uid", "user_id", "username"}
        if not common:
            return False
    return True


def identity_rejection_reason(candidate, references):
    if any(candidate & reference for reference in references):
        return "observer_identity_not_independent"
    if not identity_is_known(candidate) or any(not identity_is_known(reference) for reference in references):
        return "observer_identity_unavailable"
    if not identities_independent(candidate, references):
        return "observer_identity_not_comparable"
    return None


def account_identity_keys(account, username=None, *, user_id=None):
    """Compare actual users/sessions without retaining cookie values in reports."""
    from services.cookie_validator import get_cookies_for_account, parse_cookies_to_dict
    username = str(username or "").strip().lstrip("@").casefold()
    result = {("username", username)} if username else set()
    account_id = getattr(account, "id", None)
    if account_id is not None:
        result.add(("account_id", str(account_id)))
    user_id = str(user_id or getattr(account, "_cached_tiktok_uid", None) or "").strip()
    if user_id:
        result.add(("user_id", hashlib.sha256(user_id.encode()).hexdigest()))
    cookies = parse_cookies_to_dict(get_cookies_for_account(account))
    for name, family in (("uid_tt", "uid"), ("uid_tt_ss", "uid"),
                         ("sessionid", "session"), ("sessionid_ss", "session"), ("sid_tt", "session")):
        if cookies.get(name):
            result.add((family, hashlib.sha256(cookies[name].encode()).hexdigest()))
    sid_guard = unquote(str(cookies.get("sid_guard") or "")).split("|", 1)[0]
    if len(sid_guard) == 32 and all(char in "0123456789abcdefABCDEF" for char in sid_guard):
        result.add(("session", hashlib.sha256(sid_guard.lower().encode()).hexdigest()))
    return result
