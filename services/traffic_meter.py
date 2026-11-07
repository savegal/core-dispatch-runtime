"""Batch browser transfer sizes without downloading bodies or keeping request URLs."""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import logging
import threading
from weakref import WeakSet

from sqlalchemy.orm import Session
from db.models import TaskTrafficBucket
from services.async_ownership import await_owned

logger = logging.getLogger(__name__)
_meters = WeakSet()
_meters_lock = threading.RLock()


async def flush_task_traffic(engine, task_id):
    """Checkpoint measured bytes before a quota decision on this event loop.

    Other workers expose their measurements through SQL. Never await a browser
    meter belonging to another loop/thread. Weak references retain no contexts.
    """
    loop = asyncio.get_running_loop()
    with _meters_lock:
        meters = [meter for meter in _meters if meter.engine is engine
                  and meter._loop in (None, loop)
                  and (task_id in meter._writing_task_ids or any(key[0] == task_id for key in meter.buffer))]
    if meters:
        return all(await asyncio.gather(*(meter.flush() for meter in meters)))
    return True


def persist_sizes(engine, batch):
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    with Session(engine) as db:
        for (task_id, account_key, category, hour), (received, sent, count) in batch.items():
            statement = insert(TaskTrafficBucket).values(task_id=task_id, account_key=account_key,
                category=category, hour=hour, received_bytes=received, sent_bytes=sent, requests=count)
            db.execute(statement.on_conflict_do_update(index_elements=["task_id", "account_key", "category", "hour"],
                set_={"received_bytes": TaskTrafficBucket.received_bytes + received,
                      "sent_bytes": TaskTrafficBucket.sent_bytes + sent,
                      "requests": TaskTrafficBucket.requests + count}))
        db.commit()


class TrafficMeter:
    def __init__(self, engine, account_id):
        self.engine, self.account_id = engine, account_id or 0
        self.buffer = defaultdict(lambda: [0, 0, 0])
        self.pending = set()
        self.timer = None
        self.lock = asyncio.Lock()
        self.closing = False
        self.wake = asyncio.Event()
        self._close_task = None
        self._writing_task_ids = set()
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None
        with _meters_lock:
            _meters.add(self)

    def started(self, request):
        try:
            task_id = getattr(request.frame.page, "_pool_task_id", None)
        except Exception:
            task_id = None
        if isinstance(task_id, int):
            request._traffic_task_id = task_id

    def finished(self, request):
        if not isinstance(getattr(request, "_traffic_task_id", None), int) or getattr(request, "_traffic_local", False):
            return
        task = asyncio.create_task(self.measure(request))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def measure(self, request):
        try:
            sizes = await request.sizes()
            received = max(0, int(sizes.get("responseBodySize") or 0)) + max(0, int(sizes.get("responseHeadersSize") or 0))
            sent = max(0, int(sizes.get("requestBodySize") or 0)) + max(0, int(sizes.get("requestHeadersSize") or 0))
            category = "assets" if request.resource_type in {"script", "stylesheet", "image", "media", "font"} else (
                "writes" if request.method.upper() not in {"GET", "HEAD"} else "reads")
            hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
            values = self.buffer[(request._traffic_task_id, self.account_id, category, hour)]
            values[0] += received
            values[1] += sent
            values[2] += 1
            if not self.closing and (self.timer is None or self.timer.done()):
                self.timer = asyncio.create_task(self.delayed_flush())
        except Exception:
            logger.debug("Browser transfer size unavailable", exc_info=True)

    async def delayed_flush(self):
        while self.buffer:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass
            await self.flush()
            if self.closing:
                break

    async def flush(self):
        if self._loop is None:
            self._loop = asyncio.get_running_loop()
        async with self.lock:
            batch, self.buffer = dict(self.buffer), defaultdict(lambda: [0, 0, 0])
            if not batch:
                return True
            self._writing_task_ids = {key[0] for key in batch}
            operation = asyncio.create_task(asyncio.to_thread(persist_sizes, self.engine, batch))
            try:
                await await_owned(operation)
            except asyncio.CancelledError:
                # The thread has finished before await_owned propagates cancel.
                # A committed batch must not be restored and counted twice.
                if not operation.cancelled() and operation.exception() is not None:
                    self._restore(batch)
                raise
            except Exception:
                self._restore(batch)
                logger.warning("Could not persist measured browser traffic", exc_info=True)
                return False
            finally:
                self._writing_task_ids.clear()
            return True

    def _restore(self, batch):
        for key, values in batch.items():
            for index, count in enumerate(values):
                self.buffer[key][index] += count

    async def close(self):
        self.closing = True
        self.wake.set()
        # Multiple cleanup paths may close one browser context. They share one
        # drain; caller cancellation cannot cancel its measurements or SQL write.
        if self._close_task is None or (self._close_task.done() and (
                self.buffer or self._close_task.cancelled() or self._close_task.exception() is not None)):
            self._close_task = asyncio.create_task(self._finish_close())
        await await_owned(self._close_task)

    async def _finish_close(self):
        if self.timer and not self.timer.done():
            await asyncio.gather(self.timer, return_exceptions=True)
        if self.pending:
            await asyncio.gather(*list(self.pending), return_exceptions=True)
        await self.flush()


def install_traffic_meter(context, account_id):
    from services.publication_journal import publication_context
    scope = publication_context()
    if not callable(getattr(context, "on", None)):
        return
    from db.database import sync_engine
    meter = TrafficMeter(scope[0] if scope else sync_engine, account_id)
    context._traffic_meter = meter
    context.on("request", meter.started)
    context.on("requestfinished", meter.finished)
