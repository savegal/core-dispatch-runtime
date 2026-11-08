import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from db.models import Account, PublicationAttempt
from services import account_assignment as assignments
from services import account_service as service
from db.migrations.add_account_assignment_migration import migrate


@pytest.fixture(autouse=True)
def isolated_scheduler(monkeypatch):
    service._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
    service._REPLY_ACCOUNT_RESERVED_UNTIL.clear()
    assignments._owned_leases.clear()
    monkeypatch.setattr(service.settings, "USE_PROXY", False)
    monkeypatch.setattr(service.settings, "PROXY_GEO_MATCHING", False)
    monkeypatch.setattr(service.settings, "MAX_ACTIONS_PER_ACCOUNT", 999999)
    monkeypatch.setattr(service, "_has_bad_proxy", lambda *a, **kw: False)
    monkeypatch.setattr(service, "get_proxy_type_filter", lambda db: None)
    monkeypatch.setattr(service, "get_proxy_prefer_mobile", lambda db: False)
    yield
    for scope, account_id in list(assignments._owned_leases):
        assignments.release_assignment(account_id, scope)
    service._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
    service._REPLY_ACCOUNT_RESERVED_UNTIL.clear()


def add_accounts(db, count):
    now = datetime.now(timezone.utc)
    accounts = [Account(label=f"fair-{i}", status="active", cookies=json.dumps(
        # Rotation is the tie-breaker between accounts of equal quality.
        {"sessionid": f"session-{i}", "ttwid": "wid"}),
        created_at=now-timedelta(days=7)) for i in range(count)]
    db.add_all(accounts)
    db.commit()
    return [a.id for a in accounts]


@pytest.mark.parametrize("selector", [service.get_free_account_for_main_comment, service.get_free_account_for_reply])
def test_account_quality_beats_an_older_assignment(db_session, selector):
    older_id, newer_id = add_accounts(db_session, 2)
    now = datetime.now(timezone.utc)
    older, newer = db_session.get(Account, older_id), db_session.get(Account, newer_id)
    older.last_assigned_at = now - timedelta(hours=2)
    older.error_count_recent = 2
    newer.last_assigned_at = now - timedelta(hours=1)
    db_session.commit()

    selected = selector(db_session)
    assert selected.id == newer_id


@pytest.mark.parametrize("scope,selector", [(service.MAIN_SCOPE, service.get_free_account_for_main_comment),
    (service.REPLY_SCOPE, service.get_free_account_for_reply)])
def test_clean_cookie_profile_wins_before_the_first_candidate_batch(db_session, scope, selector):
    ids = add_accounts(db_session, 24)
    rows = db_session.query(Account).order_by(Account.id).all()
    now = datetime.now(timezone.utc)
    for index, row in enumerate(rows):
        profile = {"sessionid": f"session-{index}", "ttwid": "wid", "tt_csrf_token": "csrf"}
        if row.id != ids[-1]:
            profile.update({"x-web-secsdk-uid": "fixture", "gd_random": "present"})
        row.cookies = json.dumps(profile)
    rows[-1].last_assigned_at = now
    db_session.commit()

    healthy = {"bucket": "healthy"}
    clean_score = service._calculate_scope_quality_score(rows[-1], healthy, scope, now)
    risky_score = service._calculate_scope_quality_score(rows[0], healthy, scope, now)
    assert clean_score - risky_score == 25
    assert selector(db_session).id == ids[-1]


@pytest.mark.parametrize("selector", [service.get_free_account_for_main_comment, service.get_free_account_for_reply])
def test_only_recent_wire_empty_responses_exclude_account(db_session, sample_task, selector):
    ids = add_accounts(db_session, 3)
    now = datetime.now(timezone.utc)
    for index, (account_id, elapsed, wires) in enumerate(zip(ids, [59, 61, 15], [1, 1, 0])):
        db_session.add(PublicationAttempt(id=f"response-{index}", task_id=sample_task.id, account_id=account_id,
            video_url=sample_task.video_urls[0], kind="main", text="fixture", text_hash="fixture",
            status="unknown", wire_count=wires, error_code="submitted_unconfirmed:empty_body",
            started_at=now - timedelta(hours=2), finished_at=now - timedelta(minutes=elapsed)))
    db_session.commit()

    assert selector(db_session).id == ids[1]
    assert selector(db_session, exclude_ids=[ids[1]]).id == ids[2]


def test_rotation_covers_pool_beyond_first_sql_batch_and_survives_restart(db_session):
    ids = add_accounts(db_session, 45)
    selected = []
    for turn in range(90):
        with Session(db_session.get_bind()) as db:
            account = service.get_free_account_for_main_comment(db)
            assert account is not None
            selected.append(account.id)
            service.release_main_account_reservation(account.id)
        # No successful posts are recorded. Fresh sessions simulate subsequent orders.
    assert set(selected[:45]) == set(ids)
    assert set(Counter(selected).values()) == {2}
    assert all(a != b for a,b in zip(selected, selected[1:]))


def test_main_and_reply_share_turns_and_database_leases(db_session):
    ids = add_accounts(db_session, 4)
    main = service.get_free_account_for_main_comment(db_session)
    service._MAIN_ACCOUNT_RESERVED_UNTIL.clear()  # A different process cannot see local leases.
    with Session(db_session.get_bind()) as other:
        reply = service.get_free_account_for_reply(other, reserve=True)
        assert reply.id != main.id
        service.release_reply_account_reservation(reply.id)
    service.release_main_account_reservation(main.id)
    next_main = service.get_free_account_for_main_comment(db_session)
    assert next_main.id not in {main.id, reply.id}
    service.release_main_account_reservation(next_main.id)


def test_parallel_workers_get_distinct_assignments(db_session):
    add_accounts(db_session, 12)
    engine = db_session.get_bind()
    def select_one(_):
        with Session(engine) as db:
            account = service.get_free_account_for_reply(db, reserve=True)
            return account.id if account else None
    with ThreadPoolExecutor(max_workers=4) as workers:
        selected = list(workers.map(select_one, range(4)))
    assert None not in selected
    assert len(set(selected)) == 4
    for account_id in selected:
        service.release_reply_account_reservation(account_id)


def test_single_account_is_reusable_after_release_but_not_while_leased(db_session):
    [account_id] = add_accounts(db_session, 1)
    account = service.get_free_account_for_main_comment(db_session)
    service._MAIN_ACCOUNT_RESERVED_UNTIL.clear()
    with Session(db_session.get_bind()) as other:
        assert service.get_free_account_for_reply(other, reserve=True) is None
    service.release_main_account_reservation(account.id)
    assert service.get_free_account_for_reply(db_session, reserve=True).id == account_id


def test_expired_lease_recovers_and_old_owner_cannot_release_new_token(db_session):
    [account_id] = add_accounts(db_session, 1)
    account = db_session.get(Account, account_id)
    assert assignments.claim_assignment(db_session, account, "main_comment")
    old_owner = assignments._owned_leases[("main_comment",account_id)]
    db_session.execute(update(Account).where(Account.id==account_id).values(assignment_lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)))
    db_session.commit()
    assert assignments.claim_assignment(db_session, account, "main_comment")
    new_owner = assignments._owned_leases[("main_comment",account_id)]
    assignments._owned_leases[("main_comment",account_id)] = old_owner
    assignments.release_assignment(account_id,"main_comment")
    db_session.expire_all()
    assert db_session.get(Account,account_id).assignment_lease_token == new_owner[1]
    assignments._owned_leases[("main_comment",account_id)] = new_owner
    assignments.release_assignment(account_id,"main_comment")


def test_invalid_empty_auth_is_not_selected(db_session):
    bad = Account(label="empty-auth",cookies='{"sessionid":"", "sid_tt":""}',status="active")
    db_session.add(bad); db_session.commit()
    assert service.get_free_account_for_main_comment(db_session) is None
    assert service.get_free_account_for_reply(db_session, reserve=True) is None


def test_assignment_migration_is_idempotent_and_preserves_data(tmp_path):
    import sqlite3
    path=tmp_path/'migration.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE accounts(id INTEGER PRIMARY KEY,label TEXT)')
        db.execute("INSERT INTO accounts VALUES(1,'original')")
    assert migrate(database_path=str(path)) is True
    assert migrate(database_path=str(path)) is False
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT label,assignment_count,last_assigned_at FROM accounts').fetchone()==('original',0,None)
