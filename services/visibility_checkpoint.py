"""Run durable checkpoints without sharing an ORM session across threads."""
import asyncio
import threading

from sqlalchemy import inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

_TASK_LOCKS = [threading.RLock() for _ in range(64)]


async def run_visibility_checkpoint(db, task, checkpoint, *args):
    from db.models import Task

    if not isinstance(db, Session) or not isinstance(task, Task) or not inspect(task).persistent:
        return checkpoint(db, task, *args)
    task_id = inspect(task).identity[0]
    bind = db.get_bind(mapper=Task).engine

    def persist():
        # The worker owns this session and all its ORM objects. The event loop
        # awaits durability before continuing with another publication.
        with _TASK_LOCKS[task_id % len(_TASK_LOCKS)], Session(bind=bind, expire_on_commit=False) as checkpoint_db:
            if bind.dialect.name == "sqlite":
                # SQLite ignores FOR UPDATE. Acquire its writer lock before
                # reading settings so checkpoints from other processes merge.
                checkpoint_db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            row = checkpoint_db.query(Task).filter(Task.id == task_id).with_for_update().one()
            checkpoint(checkpoint_db, row, *args)
            return row.settings

    pending = asyncio.create_task(asyncio.to_thread(persist))
    try:
        merged = await asyncio.shield(pending)
    except asyncio.CancelledError:
        # Cancellation cannot interrupt a thread's commit. Finish owning this
        # receipt before publication cleanup runs and propagates cancellation.
        await pending
        raise
    set_committed_value(task, "settings", merged)
