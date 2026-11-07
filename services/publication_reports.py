"""One evidence-based projection for live cards, exports and rolling statistics."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
import hashlib
from io import StringIO

from sqlalchemy import case, distinct, func
from db.models import Account, PublicationAttempt, PublicationCheckJob, QuantityAction, Task, TaskRuntime, TaskTrafficBucket
from services.publication_links import comment_link


def iso(value):
    return value.isoformat() if value else None


def _row(row):
    observations = row.observers or []
    return {key: getattr(row, key) for key in (
        "id", "account_id", "video_url", "aweme_id", "kind", "parent_id", "text", "text_hash",
        "status", "wire_count", "comment_id", "error_code", "visibility",
    )} | {key: iso(getattr(row, key)) for key in (
        "started_at", "dispatched_at", "finished_at", "checked_at", "visible_at",
    )} | {"source": "http_journal" if row.origin == "http" else "saved_receipt",
          "wire_count": row.wire_count if row.origin == "http" else None,
          "author_visible": any(item.get("role") == "author" and item.get("found") for item in observations),
          "independent_absences": len({item.get("account_id") for item in observations
                                       if item.get("role") != "author" and item.get("complete_absence")}),
          "link": comment_link(row.aweme_id, row.comment_id, row.video_url),
          "parent_link": comment_link(row.aweme_id, row.parent_id, row.video_url)}


def publication_rows(db, task):
    rows = [_row(row) for row in db.query(PublicationAttempt).filter(
        PublicationAttempt.task_id == task.id).order_by(PublicationAttempt.started_at, PublicationAttempt.id)]
    known = {(row["kind"], row["comment_id"]) for row in rows if row["comment_id"]}
    public_ids = set()
    # Existing receipts remain accessible without inventing historical wire attempts.
    for url, result in ((task.settings or {}).get("url_results") or {}).items():
        if not isinstance(result, dict):
            continue
        engagement = result.get("engagement") or {}
        main = engagement.get("main") or {}
        for kind, items in (("main", main.get("variants_published") or ([main] if main.get("comment_id") else [])),
                            ("reply", (engagement.get("replies") or {}).get("items") or [])):
            for item in items:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("reply_id") or item.get("reply_comment_id") or item.get("comment_id") or "")
                visible = item.get("public_confirmed") is True or item.get("visibility_status") == "public_confirmed"
                if visible:
                    public_ids.add(cid)
                if not cid.isdigit() or (kind, cid) in known:
                    continue
                known.add((kind, cid))
                visible = item.get("public_confirmed") is True or item.get("visibility_status") == "public_confirmed"
                accepted = visible or bool(item.get("network_confirmed") or item.get("success") or item.get("posted"))
                text = str(item.get("text") or "")
                rows.append(dict(id="receipt:" + cid, account_id=item.get("account_id"), video_url=url,
                    aweme_id=result.get("aweme_id"), kind=kind, parent_id=item.get("parent_comment_id"),
                    text=text, text_hash=hashlib.sha256(text.encode()).hexdigest(),
                    status="accepted" if accepted else "unknown", wire_count=None, comment_id=cid,
                    error_code=None, visibility="visible" if visible else "unchecked", source="saved_receipt",
                    started_at=None, dispatched_at=None, finished_at=None, checked_at=None, visible_at=None,
                    link=comment_link(result.get("aweme_id"), cid, url),
                    parent_link=comment_link(result.get("aweme_id"), item.get("parent_comment_id"), url)))
    # Project canonical evidence in linear time, without rescanning it per row.
    for row in rows:
        if row["source"] == "http_journal" and row["comment_id"] in public_ids and row["checked_at"] is None:
            row["visibility"] = "visible"
    return rows


def _latest_receipts(rows):
    latest = {}
    for row in rows:
        cid = row["comment_id"]
        if not cid:
            continue
        stamp = row.get("checked_at") or row.get("visible_at") or row.get("finished_at") or row.get("started_at") or ""
        previous = latest.get(cid)
        order = (stamp, row["id"])
        if previous is None or order > previous[0]:
            latest[cid] = (order, row)
    return [row for _, row in latest.values()]


def summarize(rows):
    dispatched = sum(row["wire_count"] or 0 for row in rows)
    receipts = _latest_receipts(rows)
    accepted = {row["comment_id"] for row in rows if row["status"] == "accepted" and row["comment_id"]}
    visible = {row["comment_id"] for row in receipts if row["visibility"] == "visible"}
    wire = {row["comment_id"] for row in rows if row["source"] == "http_journal" and row["comment_id"]}
    historical = {row["comment_id"] for row in rows if row["source"] != "http_journal" and row["comment_id"]}
    return dict(dispatched=dispatched, accepted=len(accepted), visible=len(visible),
        author_only=sum(row["visibility"] == "author_only" for row in receipts),
        not_found=sum(row["visibility"] == "not_found" for row in receipts),
        existing_accepted=len(accepted & historical), existing_visible=len(visible & historical),
        ever_visible=len({row["comment_id"] for row in rows if (row["visible_at"] or row["visibility"] == "visible") and row["comment_id"]}),
        sending=sum(row["status"] == "sending" for row in rows),
        uncertain=sum(row["status"] == "unknown" for row in rows),
        pending_checks=sum(row["status"] == "accepted" and row["visibility"] in {"unchecked", "inconclusive"} for row in receipts),
        failed=sum(row["status"] == "failed" for row in rows), not_sent=sum(row["status"] == "not_sent" for row in rows),
        preparing=sum(row["status"] == "preparing" for row in rows),
        historical_without_attempt_count=sum(row["wire_count"] is None for row in rows),
        accepted_percent=round(100 * len(accepted & wire) / dispatched, 2) if dispatched else None,
        visible_percent=round(100 * len(visible & wire) / dispatched, 2) if dispatched else None)


def ranked_publication_rows(query):
    p = PublicationAttempt
    cid_key = func.coalesce(p.comment_id, p.id)
    return query.with_entities(p.comment_id, p.status, p.visibility, p.origin, p.wire_count, p.visible_at,
        func.row_number().over(partition_by=cid_key, order_by=(
            func.coalesce(p.checked_at, p.visible_at, p.finished_at, p.started_at).desc(), p.id.desc())).label("rank"),
        func.max(case((p.origin == "http", 1), else_=0)).over(partition_by=cid_key).label("http"),
        func.max(case((p.origin != "http", 1), else_=0)).over(partition_by=cid_key).label("historical"),
    ).subquery()


def _sql_summary(query):
    ranked = ranked_publication_rows(query)
    c = ranked.c
    latest = (c.rank == 1) & c.comment_id.isnot(None)
    visible = latest & (c.visibility == "visible")
    accepted = c.status == "accepted"

    def count(condition):
        return func.coalesce(func.sum(case((condition, 1), else_=0)), 0)

    def unique(condition):
        return func.count(distinct(case((condition, c.comment_id))))

    fields = dict(
        dispatched=func.coalesce(func.sum(case((c.origin == "http", c.wire_count), else_=0)), 0),
        accepted=unique(accepted),
        visible=count(visible),
        author_only=count(latest & (c.visibility == "author_only")),
        not_found=count(latest & (c.visibility == "not_found")),
        sending=count(c.status == "sending"),
        uncertain=count(c.status == "unknown"),
        pending_checks=count(latest & accepted & c.visibility.in_(["unchecked", "inconclusive"])),
        failed=count(c.status == "failed"),
        not_sent=count(c.status == "not_sent"),
        preparing=count(c.status == "preparing"),
        historical_without_attempt_count=count(c.origin != "http"),
        wire_accepted=unique(accepted & (c.http == 1)),
        wire_visible=count(visible & (c.http == 1)),
        existing_accepted=unique(accepted & (c.historical == 1)),
        existing_visible=count(visible & (c.historical == 1)),
        ever_visible=unique(c.visible_at.isnot(None) | (c.visibility == "visible")),
    )
    result = dict(query.session.query(*(value.label(key) for key, value in fields.items())).one()._mapping)
    wire_accepted, wire_visible = result.pop("wire_accepted"), result.pop("wire_visible")
    sent = result["dispatched"]
    result["accepted_percent"] = round(100 * wire_accepted / sent, 2) if sent else None
    result["visible_percent"] = round(100 * wire_visible / sent, 2) if sent else None
    return result


def quantity_goal_count(db, task):
    plan = (task.settings or {}).get("publication_plan")
    if not plan:
        return None
    counts = defaultdict(lambda: defaultdict(int))
    for url, kind, count in db.query(QuantityAction.video_url, QuantityAction.kind, func.count(QuantityAction.id)
            ).filter(QuantityAction.task_id == task.id, QuantityAction.state == "visible").group_by(
                QuantityAction.video_url, QuantityAction.kind):
        counts[url][kind] = count
    return sum(v["main"] >= plan["main_count"] and v["reply"] >= plan["main_count"] * plan["replies_per_main"]
               for v in counts.values())


def activity_snapshot(db, task, *, limit=12, include_texts=False):
    from services.publication_journal import _safe_error
    from services.publication_runtime import load_publication_runtime, order_runtime, runtime_payload
    execution = load_publication_runtime(db)
    if (task.settings or {}).get("publication_plan"):
        execution = order_runtime(task.settings, execution)
    live_interval = execution.live_interval
    error_reason = _safe_error(task.error_reason)
    if (task.settings or {}).get("publication_plan") and not include_texts:
        query = db.query(PublicationAttempt).filter(PublicationAttempt.task_id == task.id)
        runtime, job = db.get(TaskRuntime, task.id), db.get(PublicationCheckJob, task.id)
        traffic = db.query(func.coalesce(func.sum(TaskTrafficBucket.received_bytes), 0),
            func.coalesce(func.sum(TaskTrafficBucket.sent_bytes), 0), func.coalesce(func.sum(TaskTrafficBucket.requests), 0)
        ).filter(TaskTrafficBucket.task_id == task.id).one()
        plan = (task.settings or {})["publication_plan"]
        success = quantity_goal_count(db, task)
        errors = dict(query.filter(PublicationAttempt.error_code.isnot(None)).with_entities(
            PublicationAttempt.error_code, func.count(PublicationAttempt.id)).group_by(PublicationAttempt.error_code).all())
        latest = query.order_by(func.coalesce(PublicationAttempt.checked_at, PublicationAttempt.finished_at,
            PublicationAttempt.dispatched_at, PublicationAttempt.started_at).desc(), PublicationAttempt.id).limit(limit).all()
        return dict(task_id=task.id, status=task.status, progress=task.progress, videos_total=len(task.video_urls or []),
            live_interval=live_interval,
            execution=runtime_payload(execution),
            error_reason=error_reason,
            videos_success=success, generated_at=datetime.now(timezone.utc).isoformat(),
            phase=runtime.phase if runtime else "preparing", updated_at=iso(runtime.updated_at) if runtime else None,
            summary=_sql_summary(query), main=_sql_summary(query.filter(PublicationAttempt.kind == "main")),
            replies=_sql_summary(query.filter(PublicationAttempt.kind == "reply")), texts=[], errors=errors,
            latest=[_row(row) for row in latest], plan=plan,
            pending_rechecks=query.filter(PublicationAttempt.next_check_at.isnot(None), PublicationAttempt.visibility != "visible")
                .with_entities(func.count(distinct(PublicationAttempt.comment_id))).scalar(),
            traffic=dict(received_bytes=int(traffic[0]), sent_bytes=int(traffic[1]), requests=int(traffic[2]),
                         measurement="browser_transfer_estimate"),
            recheck=dict(status=job.status, result=job.result) if job else None)
    rows = publication_rows(db, task)
    groups = defaultdict(list)
    for row in rows:
        groups[(row["kind"], row["text_hash"])].append(row)
    runtime = db.get(TaskRuntime, task.id)
    job = db.get(PublicationCheckJob, task.id)
    traffic = db.query(func.coalesce(func.sum(TaskTrafficBucket.received_bytes), 0),
        func.coalesce(func.sum(TaskTrafficBucket.sent_bytes), 0), func.coalesce(func.sum(TaskTrafficBucket.requests), 0)
    ).filter(TaskTrafficBucket.task_id == task.id).one()
    result = dict(task_id=task.id, status=task.status, progress=task.progress, live_interval=live_interval,
        error_reason=error_reason,
        videos_total=len(task.video_urls or []), generated_at=datetime.now(timezone.utc).isoformat(),
        phase=runtime.phase if runtime else "preparing", updated_at=iso(runtime.updated_at) if runtime else None,
        summary=summarize(rows), main=summarize([r for r in rows if r["kind"] == "main"]),
        replies=summarize([r for r in rows if r["kind"] == "reply"]),
        texts=[dict(kind=key[0], text=items[0]["text"], **summarize(items)) for key, items in groups.items()],
        errors=dict(Counter(r["error_code"] for r in rows if r["error_code"])),
        latest=sorted(rows, key=lambda r: r["finished_at"] or r["dispatched_at"] or r["started_at"] or "", reverse=True)[:limit],
        traffic=dict(received_bytes=int(traffic[0]), sent_bytes=int(traffic[1]), requests=int(traffic[2]),
                     measurement="browser_transfer_estimate"),
        recheck=dict(status=job.status, result=job.result) if job else None)
    from services.user_facing_errors import build_task_execution_summary
    existing = build_task_execution_summary(dict(id=task.id, status=task.status, settings=task.settings,
        kind=task.kind, video_urls=task.video_urls, progress=task.progress, error_reason=task.error_reason))
    result["videos_success"] = (existing.get("videos") or {}).get("success", 0)
    goal_count = quantity_goal_count(db, task)
    if goal_count is not None:
        result["videos_success"] = goal_count
        result["plan"] = task.settings["publication_plan"]
        result["execution"] = runtime_payload(execution)
    result["pending_rechecks"] = db.query(PublicationAttempt).filter(
        PublicationAttempt.task_id == task.id, PublicationAttempt.next_check_at.isnot(None),
        PublicationAttempt.visibility != "visible").with_entities(func.count(distinct(PublicationAttempt.comment_id))).scalar()
    return result


def export_report(db, task, format="txt"):
    rows = publication_rows(db, task)
    snapshot = activity_snapshot(db, task, include_texts=True)
    if format == "csv":
        output = StringIO()
        fields = ["kind", "text", "account_id", "parent_id", "comment_id", "status", "visibility", "source",
                  "wire_count", "started_at", "checked_at", "visible_at", "author_visible",
                  "independent_absences", "link", "parent_link"]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            # Spreadsheet programs must treat exported user text as data.
            writer.writerow({k: ("'" + v if isinstance(v, str) and (
                v.lstrip().startswith(("=", "+", "-", "@")) or v.startswith(("\t", "\r", "\n"))) else v)
                             for k, v in row.items() if k in fields})
        return output.getvalue()
    if format == "links":
        lines = [f"Подтверждённые публикации · заказ #{task.id}", ""]
        for row in _latest_receipts(rows):
            if row["visibility"] == "visible":
                lines.extend([f"{'Ответ' if row['kind'] == 'reply' else 'Комментарий'} {row['comment_id']}",
                              f"Родитель: {row['parent_id']}" if row["parent_id"] else "", row["text"], row["link"],
                              f"Ссылка на родителя: {row['parent_link']}" if row.get("parent_link") else "", ""])
        return "\n".join(lines)
    lines = [f"Заказ #{task.id}", f"Состояние: {task.status}", f"Обработано видео: {task.progress}/{len(task.video_urls or [])}",
             f"Цель достигнута на видео: {snapshot['videos_success']}", "",
             "Отправки — фактические HTTP-запросы публикации. CID и публичная видимость учитываются отдельно.",
             "У исторических записей количество отправок может быть неизвестно.", ""]
    for kind, name in (("main", "Главные комментарии"), ("reply", "Ответы")):
        lines.append(name)
        for text in snapshot["texts"]:
            if text["kind"] != kind:
                continue
            rate = f"{text['visible_percent']:.2f}%" if text["visible_percent"] is not None else "нет данных"
            lines.extend([text["text"], f"Видно / отправок: {text['visible']} / {text['dispatched']} · {rate}",
                          f"Принято с CID: {text['accepted']} · ожидают проверки: {text['pending_checks']}",
                          f"Только автору: {text['author_only']} · не найдены наблюдателями: {text['not_found']}",
                          f"Из предыдущих заказов: принято {text['existing_accepted']}, подтверждено {text['existing_visible']}",
                          f"Неопределённые отправки: {text['uncertain']} · ошибок: {text['failed']}", ""])
    lines.extend(["Публикации", ""])
    for row in rows:
        lines.extend([f"{row['kind']} · аккаунт {row['account_id']} · {row['status']} · {row['visibility']}",
                      row["text"], f"CID: {row['comment_id'] or 'не получен'} · родитель: {row['parent_id'] or '—'}",
                      row["link"], f"Ссылка на родителя: {row['parent_link']}" if row.get("parent_link") else "",
                      f"Проверено: {row['checked_at'] or 'проверка не выполнена'}", ""])
    return "\n".join(lines)


def rolling_statistics(db, user_id, *, hours=24):
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = [_row(row) for row in db.query(PublicationAttempt).join(Task).filter(
        Task.user_id == user_id, PublicationAttempt.origin == "http", PublicationAttempt.started_at >= since)]
    accounts = defaultdict(list)
    for row in rows:
        accounts[row["account_id"]].append(row)
    countries = dict(db.query(Account.id, Account.country_code).filter(Account.id.in_(list(accounts)))) if accounts else {}
    geographies = defaultdict(list)
    for account_id, items in accounts.items():
        geographies[str(countries.get(account_id) or "unknown").upper()].extend(items)
    return dict(hours=hours, since=since.isoformat(), summary=summarize(rows),
                accounts=[dict(account_id=key, country_code=countries.get(key), **summarize(value)) for key, value in accounts.items()],
                geographies=[dict(country_code=key, **summarize(value)) for key, value in geographies.items()])
