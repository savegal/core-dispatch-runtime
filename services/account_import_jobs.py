"""Resumable import batches. Raw cookies live only in the private expiring Redis payload."""
import asyncio
from datetime import timedelta
import json
import logging
from uuid import uuid4

from sqlalchemy import or_, select, update
from db.models import AccountImportJob
from services.publication_journal import utcnow
from services.redis_service import get_redis_client

logger = logging.getLogger(__name__)
PAYLOAD_TTL = 3600


def payload_key(job_id):
    return f"account-import:{job_id}:payload"


async def create_import_job(db, records):
    job_id = uuid4().hex
    client = get_redis_client()
    await asyncio.to_thread(client.set, payload_key(job_id), json.dumps(records, ensure_ascii=False), ex=PAYLOAD_TTL)
    job = AccountImportJob(id=job_id, total=len(records), status="pending", processed=0,
                           counters={}, results=[], updated_at=utcnow())
    db.add(job)
    try:
        await db.commit()
    except BaseException:
        await asyncio.to_thread(client.delete, payload_key(job_id))
        raise
    return job


def import_progress(job, *, include_results=False):
    return dict(job_id=job.id, status=job.status, total=job.total, processed=job.processed,
                **(job.counters or {}), error_code=job.error_code,
                results=(job.results or []) if include_results else (job.results or [])[-5:])


async def process_import_job(session_factory):
    from api.accounts import BulkImportRequest, import_account_records
    now, token = utcnow(), uuid4().hex
    async with session_factory() as db:
        job = (await db.execute(select(AccountImportJob).where(or_(AccountImportJob.status == "pending",
            (AccountImportJob.status == "running") & (AccountImportJob.lease_until < now)))
            .order_by(AccountImportJob.updated_at).limit(1))).scalar_one_or_none()
        if job is None:
            return False
        job_id = job.id
        claimed = await db.execute(update(AccountImportJob).where(AccountImportJob.id == job_id,
            or_(AccountImportJob.status == "pending", AccountImportJob.lease_until < now))
            .values(status="running", lease_token=token, lease_until=now + timedelta(minutes=5), updated_at=now))
        await db.commit()
        if not claimed.rowcount:
            return False
        try:
            client = get_redis_client()
            raw = await asyncio.to_thread(client.get, payload_key(job_id))
            if raw is None:
                raise ValueError("payload_expired")
            records = json.loads(raw)
            cookie_index = {}
            while True:
                # Fence the lease and hold its row lock until this batch commits.
                # A worker that lost ownership cannot write accounts or checkpoints.
                owned = await db.execute(update(AccountImportJob).where(
                    AccountImportJob.id == job_id, AccountImportJob.lease_token == token)
                    .values(lease_until=utcnow() + timedelta(minutes=5)))
                if not owned.rowcount:
                    await db.rollback()
                    return False
                await db.refresh(job)
                if job.lease_token != token:
                    return False
                start = job.processed
                if start >= job.total:
                    job.status, job.lease_token, job.lease_until = "done", None, None
                    await db.commit()
                    await asyncio.to_thread(client.delete, payload_key(job_id))
                    return True
                # Account writes and the checkpoint share one transaction.
                result = await import_account_records(BulkImportRequest(accounts=records[start:start + 25]), db,
                                                      commit=False, strict=True, cookie_index=cookie_index)
                counters = dict(job.counters or {})
                for key in ("added", "updated", "skipped", "duplicates_by_cookies", "assigned_proxies", "without_proxies"):
                    counters[key] = int(counters.get(key) or 0) + int(getattr(result, key))
                job.counters = counters
                job.results = [*(job.results or []), *[{**row, "index": start + row["index"]} for row in result.results]]
                job.processed = min(job.total, start + 25)
                job.lease_until, job.updated_at = utcnow() + timedelta(minutes=5), utcnow()
                await db.commit()
                await asyncio.to_thread(client.expire, payload_key(job_id), PAYLOAD_TTL)
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            await db.rollback()
            await db.execute(update(AccountImportJob).where(AccountImportJob.id == job_id,
                AccountImportJob.lease_token == token).values(status="pending", lease_token=None, lease_until=None))
            await db.commit()
            raise
        except Exception as exc:
            await db.rollback()
            await db.execute(update(AccountImportJob).where(AccountImportJob.id == job_id,
                AccountImportJob.lease_token == token).values(status="error",
                    error_code="payload_expired" if str(exc) == "payload_expired" else type(exc).__name__,
                    lease_token=None, lease_until=None, updated_at=utcnow()))
            await db.commit()
            logger.warning("Account import job %s failed (%s)", job_id, type(exc).__name__)
            return False


async def import_loop():
    from db.database import AsyncSessionLocal
    while True:
        try:
            await process_import_job(AsyncSessionLocal)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Account import tick failed", exc_info=True)
        await asyncio.sleep(1)
