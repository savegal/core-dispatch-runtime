"""Shared exact-ID traversal rules for fresh comment and reply lists."""
from executor.browser_reads import await_browser_read
from executor.http.comment_discovery import comment_read_batch
from executor.http.execution_policy import valid_comment_id
from executor.http.page_runtime import page_document_key, target_page_matches


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


def reports_unseen_comments(payload, seen_count):
    total = payload.get("total")
    return type(total) is int and total > seen_count


_PAGE_MAIN_IDS_JS = """async ({wanted, video}) => {
    const wantedIds = new Set(wanted);
    const found = new Set();
    const add = id => { if (typeof id === 'string' && wantedIds.has(id)) found.add(id); };
    const inspect = (row, media) => {
        if (!row || typeof row !== 'object') return;
        const target = row.aweme_id ?? row.awemeId ?? row.item_id ?? row.itemId ?? media;
        if (String(target ?? '') !== video || typeof row.text !== 'string') return;
        if (String(row.reply_id ?? row.replyId ?? '0') !== '0' ||
            String(row.reply_to_reply_id ?? '0') !== '0') return;
        add(row.cid ?? row.comment_id ?? row.commentId);
    };
    for (const key of ['__UNIVERSAL_DATA_FOR_REHYDRATION__', 'SIGI_STATE']) {
        const text = document.getElementById(key)?.textContent;
        if (!text || text.length > 2000000) continue;
        let stack;
        try { stack = [[JSON.parse(text), null, false]]; } catch (_) { continue; }
        for (let count = 0; stack.length && count < 10000; count++) {
            const [node, media, isCommentList] = stack.pop();
            if (!node || typeof node !== 'object') continue;
            if (Array.isArray(node)) {
                for (const row of node.slice(0, 250)) stack.push([row, media, isCommentList]);
                continue;
            }
            const scope = node.aweme_id ?? node.awemeId ?? node.itemStruct?.id ?? media;
            if (isCommentList) inspect(node, scope);
            for (const [key, value] of Object.entries(node)) {
                if (value && typeof value === 'object') {
                    stack.push([value, scope, ['comments', 'commentList', 'comment_list'].includes(key)]);
                }
            }
        }
    }
    const scan = () => {
        for (const node of Array.from(document.querySelectorAll('[data-e2e="comment-level-1"]')).slice(0, 250)) {
            if (!node.getClientRects().length || getComputedStyle(node).visibility !== 'visible') continue;
            if (node.closest('[data-e2e="comment-level-2"], [data-e2e="reply-item"]')) continue;
            const row = node.closest('[data-e2e="comment-item"], [data-comment-id], [data-cid], [id^="comment-"]');
            if (!row) continue;
            for (const name of ['data-comment-id', 'data-cid']) add(row.getAttribute(name));
            if (row.id.startsWith('comment-')) add(row.id.slice(8));
            const link = row.querySelector('a[data-e2e="comment-permalink"][href]');
            if (link) {
                const href = new URL(link.href, location.href);
                if (href.origin === location.origin && href.pathname === location.pathname) add(href.searchParams.get('cid'));
            }
            const fiberKey = Object.keys(row).find(key => key.startsWith('__reactFiber$'));
            for (let fiber = fiberKey && row[fiberKey], depth = 0; fiber && depth < 4; fiber = fiber.return, depth++) {
                inspect(fiber.memoizedProps?.comment, null);
            }
        }
    };
    scan();
    const list = document.querySelector('[data-e2e="comment-list"], [data-e2e="comments-list"]');
    if (found.size < wantedIds.size && list && list.scrollHeight > list.clientHeight) {
        list.scrollBy(0, Math.min(list.clientHeight, 500));
        await new Promise(resolve => setTimeout(resolve, 200));
        scan();
    }
    return Array.from(found);
}"""


async def read_main_ids_from_page(page, aweme_id, comment_ids):
    """Positive CID evidence only, on the current target document."""
    wanted = {comment_id_key(cid) for cid in comment_ids if valid_comment_id(cid)}
    if not wanted or not target_page_matches(page, aweme_id):
        return set()
    document = page_document_key(page)
    try:
        found = await await_browser_read(lambda: page.evaluate(_PAGE_MAIN_IDS_JS,
            {"wanted": list(wanted), "video": str(aweme_id)}, isolated_context=False), timeout=1.5)
    except Exception:
        return set()
    if document != page_document_key(page) or not isinstance(found, list):
        return set()
    return wanted.intersection(cid for cid in found if isinstance(cid, str))


async def scan_comment_ids(page, aweme_id, comment_ids, *, read_page, parent_comment_id=None,
                           max_pages=20, meta_attr="_comment_list_http_meta"):
    wanted = {comment_id_key(cid) for cid in comment_ids if valid_comment_id(cid)}
    found, seen, cursors, pages = set(), set(), set(), set()
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
                seen.update(page_ids)
                more = _more(payload.get("has_more"))
                if more is False:
                    if reports_unseen_comments(payload, len(seen)):
                        reason = "comment_list_total_mismatch"
                    else:
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
