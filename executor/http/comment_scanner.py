"""Shared exact-ID traversal rules for fresh comment and reply lists."""
from executor.http.comment_discovery import comment_read_batch
from executor.http.execution_policy import valid_comment_id


def comment_id_key(value):
    return str(value or "").strip()


def _more(value):
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if type(value) is str and value in {"0", "1"}:
        return value == "1"
    return None


def _cursor(value):
    if type(value) is int and value >= 0:
        return value
    if type(value) is str and value.isascii() and value.isdigit():
        return int(value)
    return None


async def scan_comment_ids(page, aweme_id, comment_ids, *, read_page, parent_comment_id=None,
                           max_pages=20, meta_attr="_comment_list_http_meta"):
    wanted = {comment_id_key(cid) for cid in comment_ids if valid_comment_id(cid)}
    found, cursors, pages = set(), set(), set()
    cursor, scanned, complete, reason = 0, 0, False, "page_limit"
    reply = parent_comment_id is not None
    if not wanted or (reply and not valid_comment_id(parent_comment_id)):
        reason = "invalid_comment_ids"
    else:
        async with comment_read_batch(page):
            for _ in range(max(1, min(int(max_pages), 100))):
                cursors.add(cursor)
                payload = await read_page(cursor)
                if payload is None:
                    reason = (getattr(page, meta_attr, {}) or {}).get("reason", "unavailable")
                    break
                rows = payload.get("comments") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    reason = "invalid_comments"
                    break
                invalid, page_ids = False, set()
                for row in rows:
                    if not isinstance(row, dict) or not valid_comment_id(row.get("cid")):
                        invalid = True
                        continue
                    cid = comment_id_key(row["cid"])
                    media = row.get("aweme_id")
                    if media is not None and comment_id_key(media) != str(aweme_id):
                        invalid = True
                        continue
                    parent = comment_id_key(row.get("reply_id") or "0")
                    if parent != "0" and not valid_comment_id(parent):
                        invalid = True
                        continue
                    if reply and parent != str(parent_comment_id):
                        invalid = True
                        continue
                    scanned += 1
                    page_ids.add(cid)
                    if (reply or parent == "0") and cid in wanted:
                        found.add(cid)
                if found == wanted:
                    reason = "all_found"
                    break
                if invalid:
                    reason = "invalid_rows_or_parent" if reply else "invalid_rows"
                    break
                fingerprint = frozenset(page_ids)
                if fingerprint and fingerprint in pages:
                    reason = "repeated_page"
                    break
                pages.add(fingerprint)
                more = _more(payload.get("has_more"))
                if more is False:
                    complete, reason = True, "exhausted"
                    break
                if more is None:
                    reason = "missing_has_more"
                    break
                next_cursor = _cursor(payload.get("cursor"))
                if next_cursor is None or next_cursor in cursors:
                    reason = "cursor_loop" if payload.get("cursor") is None or next_cursor in cursors else "invalid_cursor"
                    break
                cursor = next_cursor
    result = {"found_ids": sorted(found), "absent_ids": sorted(wanted - found) if complete else [],
              "complete": complete, "scanned": scanned, "reason": reason}
    if reply:
        result["parent_comment_id"] = str(parent_comment_id)
    return result
