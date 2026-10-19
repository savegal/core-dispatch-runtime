"""Audit every account locally; optionally validate browser-auth sessions over HTTP.

No posts or browser launches. Inconclusive results never invalidate an account.
Only --apply-valid writes: confirmed-valid active/error sessions are refreshed.
"""
import argparse
import asyncio
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config.settings import get_settings
from db.models import Account
from services.account_service import MAIN_SCOPE, _read_account_session_quality, _read_runtime_state
from services.cookie_validator import validate_account_http


async def audit(args):
    logging.disable(logging.CRITICAL)
    engine = create_engine(get_settings().DB_URL)
    now = datetime.now(timezone.utc)
    with Session(engine) as db:
        accounts = db.query(Account).order_by(Account.last_validation_attempt_at.asc().nulls_first(), Account.id).all()
        db.expunge_all()
    rows = []
    eligible = []
    for account in accounts:
        quality = _read_account_session_quality(account, MAIN_SCOPE, now)
        runtime = _read_runtime_state(account, now)
        row = {"account_id": account.id, "pool": "shared" if account.owner_user_id is None else "personal", "status": account.status,
               "session_bucket": quality["bucket"], "session_reason": quality["quality_reason"], "has_browser_auth": bool(quality.get("has_browser_auth")),
               "runtime_bucket": runtime["bucket"], "runtime_category": runtime["category"], "http_status": "not_checked"}
        rows.append(row)
        if row["has_browser_auth"] and account.status in ("active", "error", "reauth_needed"):
            eligible.append((account, row))
        elif not row["has_browser_auth"]:
            row["http_status"] = "missing_browser_auth"
    output = {"started_at": now.isoformat(), "total": len(accounts), "shared": sum(a.owner_user_id is None for a in accounts),
              "statuses": dict(Counter(a.status for a in accounts)), "http_candidates": len(eligible), "rows": rows}
    dest = Path(args.output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    def save():
        output["http_summary"] = dict(Counter(r["http_status"] for r in rows))
        dest.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    save()
    print(json.dumps({k:v for k,v in output.items() if k != "rows"}), flush=True)
    if args.http:
        semaphore = asyncio.Semaphore(args.concurrency)
        completed = 0
        async def check(account, row):
            nonlocal completed
            async with semaphore:
                try:
                    result = await asyncio.wait_for(validate_account_http(account), timeout=25)
                except Exception as exc:
                    result = {"status": "inconclusive", "error": type(exc).__name__, "definitive": False}
                row["http_status"] = str(result.get("status") or "inconclusive")
                row["checked_at"] = datetime.now(timezone.utc).isoformat()
                # Errors can contain URLs; keep only a fixed diagnostic category.
                error = str(result.get("error") or "").lower()
                row["http_reason"] = ("valid" if result.get("valid") else "access_or_rate_limit" if any(x in error for x in ("403", "429", "challenge")) else "auth_rejected" if result.get("definitive") else "not_confirmed")
                if args.apply_valid and result.get("valid") and account.status in ("active", "error"):
                    with Session(engine) as db:
                        current = db.get(Account, account.id)
                        state = _read_runtime_state(current)
                        # Preserve active cooldowns and all explicit reauthentication states.
                        if current.status in ("active", "error") and not state["active_cooldown"]:
                            stamp = datetime.now(timezone.utc)
                            current.last_validation_attempt_at = stamp
                            current.last_validation_ok = stamp
                            current.last_checked_at = stamp
                            current.validation_fail_count = 0
                            current.status = "active"
                            current.error_count_recent = 0
                            current.last_error = None
                            db.commit()
                            row["applied_valid"] = True
                completed += 1
                if completed % 25 == 0 or completed == len(eligible):
                    save()
                    print(json.dumps({"checked":completed,"of":len(eligible),"summary":output["http_summary"]}), flush=True)
        await asyncio.gather(*(check(a,r) for a,r in eligible))
    output["finished_at"] = datetime.now(timezone.utc).isoformat()
    save()
    engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--http", action="store_true")
    parser.add_argument("--apply-valid", action="store_true")
    parser.add_argument("--concurrency", type=int, default=5, choices=range(1,11))
    parser.add_argument("--output", default=str(ROOT / "scratch/account-pool-validation.json"))
    asyncio.run(audit(parser.parse_args()))
