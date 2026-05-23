import pytest


@pytest.mark.unit
def test_prune_stale_task_queue_entries_removes_missing_and_non_pending(monkeypatch):
    from services import task_service

    class FakeRedis:
        def lrange(self, *_args, **_kwargs):
            return ["1", "2", "3", "bad", "2"]

    class FakeQuery:
        def filter(self, *_args, **_kwargs):
            return self

        def all(self):
            return [(1, "pending"), (2, "done")]

    class FakeDb:
        def query(self, *_args, **_kwargs):
            return FakeQuery()

    removed_ids = []

    def fake_remove_tasks_from_queue(task_ids):
        removed_ids.extend(task_ids)
        return len(task_ids)

    monkeypatch.setattr(task_service, "get_redis", lambda: FakeRedis())
    monkeypatch.setattr(task_service, "remove_tasks_from_queue", fake_remove_tasks_from_queue)

    removed = task_service.prune_stale_task_queue_entries(FakeDb())

    assert removed == 2
    assert removed_ids == [2, 3]

