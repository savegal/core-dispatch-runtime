"""Keep ownership of irreversible work until it reaches a terminal state."""
import asyncio


async def await_owned(operation):
    """Defer even repeated caller cancellation until an owned task is done.

    Cancelling a to_thread task cannot stop its thread. Never await it without
    shielding during cleanup: a second cancel would abandon the live worker.
    Consume its result/error before propagating the caller's cancellation.
    """
    cancellation = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            cancellation = cancellation or exc
        except BaseException:
            break
    try:
        result = operation.result()
    except BaseException:
        if cancellation is not None:
            raise cancellation from None
        raise
    if cancellation is not None:
        raise cancellation
    return result
