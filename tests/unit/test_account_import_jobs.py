import asyncio
import json

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models import Account, AccountImportJob
from services import account_import_jobs as jobs


def records(count):
    return [{"label": f"import-{index}", "cookies": json.dumps([
        {"name": "sessionid", "value": f"{index:032x}", "domain": ".tiktok.com", "path": "/"}
    ])} for index in range(count)]


@pytest.mark.asyncio
async def test_import_records_and_checkpoint_commit_together(async_db_session, isolated_proxy_leases, monkeypatch):
    monkeypatch.setattr(jobs, "get_redis_client", lambda: isolated_proxy_leases)
    factory = async_sessionmaker(async_db_session.bind, expire_on_commit=False)
    job = await jobs.create_import_job(async_db_session, records(30))
    assert jobs.import_progress(job)["processed"] == 0
    assert "cookies" not in str(jobs.import_progress(job))
    assert await jobs.process_import_job(factory)
    await async_db_session.refresh(job)
    assert job.status == "done" and job.processed == 30
    assert job.counters["added"] == 30
    assert len(job.results) == 30
    assert (await async_db_session.execute(select(func.count(Account.id)))).scalar() == 30
    assert isolated_proxy_leases.get(jobs.payload_key(job.id)) is None
    assert "sessionid" not in json.dumps(jobs.import_progress(job, include_results=True))


@pytest.mark.asyncio
async def test_cancelled_import_resumes_at_committed_batch(async_db_session, isolated_proxy_leases, monkeypatch):
    import api.accounts as accounts
    monkeypatch.setattr(jobs, "get_redis_client", lambda: isolated_proxy_leases)
    factory = async_sessionmaker(async_db_session.bind, expire_on_commit=False)
    job = await jobs.create_import_job(async_db_session, records(30))
    original, calls = accounts.import_account_records, 0
    async def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError()
        return await original(*args, **kwargs)
    monkeypatch.setattr(accounts, "import_account_records", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await jobs.process_import_job(factory)
    await async_db_session.refresh(job)
    assert job.status == "pending" and job.processed == 25
    monkeypatch.setattr(accounts, "import_account_records", original)
    assert await jobs.process_import_job(factory)
    await async_db_session.refresh(job)
    assert job.status == "done" and job.processed == 30
    assert job.counters["added"] == 30 and job.counters["updated"] == 0


@pytest.mark.asyncio
async def test_missing_payload_is_an_explicit_error(async_db_session, isolated_proxy_leases, monkeypatch):
    monkeypatch.setattr(jobs, "get_redis_client", lambda: isolated_proxy_leases)
    factory = async_sessionmaker(async_db_session.bind, expire_on_commit=False)
    job = await jobs.create_import_job(async_db_session, records(1))
    isolated_proxy_leases.delete(jobs.payload_key(job.id))
    assert not await jobs.process_import_job(factory)
    await async_db_session.refresh(job)
    assert job.status == "error" and job.processed == 0
    assert not (await async_db_session.execute(select(Account))).scalars().all()
