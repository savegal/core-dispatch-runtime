"""Deadline-safe reads from the browser driver, never publication operations."""
import asyncio


_pending_reads: set[asyncio.Future] = set()


async def await_browser_read(operation, *, timeout=None):
    """Keep the driver waiter alive until a read completes or its page closes.

    Cancelling Patchright's channel coroutine abandons its protocol future;
    closing the page later then produces an unhandled TargetClosedError. Only
    the caller's wait is cancelled here. The underlying read is drained, with
    a strong reference until completion. Pass a factory to avoid constructing
    a read before this coroutine starts. Do not use this for POSTs or publish
    clicks: those must retain their existing cancellation/submission policy.
    """
    task = asyncio.ensure_future(operation() if callable(operation) else operation)
    _pending_reads.add(task)

    def completed(done):
        _pending_reads.discard(done)
        if not done.cancelled():
            done.exception()

    task.add_done_callback(completed)
    protected = asyncio.shield(task)
    return await protected if timeout is None else await asyncio.wait_for(protected, timeout=timeout)
