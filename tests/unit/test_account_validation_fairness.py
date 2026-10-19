import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pytest
from db.models import Account
from services import cookie_validator as validator
from services.auto_account_validator import _get_validation_accounts


class Response:
    def __init__(self, status, payload=None):
        self.status=status; self.payload=payload; self.headers={}; self.url='https://www.tiktok.com/passport/web/account/info/'
    async def __aenter__(self):return self
    async def __aexit__(self,*args):pass
    async def json(self,**kwargs):return self.payload
    async def text(self,**kwargs):return ''


@pytest.mark.parametrize('http,payload,expected',[
    (403,None,'inconclusive'),
    (429,None,'inconclusive'),
    (401,None,'dead'),
    (200,{'message':'error'},'inconclusive'),
    (200,{'message':'session expired'},'dead'),
    (200,{'message':'success','data':{'username':'verified-user'}},'valid'),
    (200,None,'inconclusive'),
])
async def test_validation_distinguishes_auth_rejection_from_transport_or_generic_error(monkeypatch,http,payload,expected):
    class Client:
        def __init__(self,*args,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def get(self,*args,**kwargs):return Response(http,payload)
    monkeypatch.setattr(validator.aiohttp,'ClientSession',Client)
    account=SimpleNamespace(id=1,cookies='{"sessionid":"test-session"}',proxy=None,user_agent=None)
    result=await validator.validate_account_http(account)
    assert result['status']==expected
    if expected=='inconclusive':assert result['definitive'] is False


async def test_one_validation_exception_does_not_abort_batch(monkeypatch):
    async def check(account):
        if account.id==1:raise ValueError('bad account')
        return {'valid':True,'status':'valid'}
    monkeypatch.setattr(validator,'validate_account_http',check)
    result=await validator.validate_accounts_batch([SimpleNamespace(id=i,label=str(i)) for i in (1,2)])
    assert [r['status'] for r in result]==['inconclusive','valid']


def test_validation_batches_cover_never_checked_accounts_before_rechecking(db_session):
    now=datetime.now(timezone.utc)
    accounts=[Account(label=f'validate-{i}',cookies='{"sessionid":"test"}',status='active',last_checked_at=now) for i in range(70)]
    db_session.add_all(accounts);db_session.commit()
    first=_get_validation_accounts(db_session,now)
    assert len(first)==50
    for account in first:account.last_validation_attempt_at=now
    db_session.commit()
    second=_get_validation_accounts(db_session,now+timedelta(minutes=31))
    assert {a.id for a in second[:20]}=={a.id for a in accounts[50:]}
    # Usage's last_checked_at must not hide an account from the validation queue.
    assert set(a.id for a in first+second)==set(a.id for a in accounts)


@pytest.mark.parametrize('probe_status',['inconclusive','valid'])
def test_validation_preserves_active_runtime_cooldown(db_session,monkeypatch,probe_status):
    from services import auto_account_validator as auto
    from sqlalchemy.orm import Session
    now=datetime.now(timezone.utc)
    reason='captcha_challenge:slider'
    account=Account(label='captcha-check',cookies='{"sessionid":"test"}',status='error',last_error=reason,error_count_recent=2,last_checked_at=now)
    db_session.add(account);db_session.commit();account_id=account.id
    monkeypatch.setattr(auto,'_last_validation_time',None)
    monkeypatch.setattr(auto,'_validation_running',False)
    engine=db_session.get_bind()
    monkeypatch.setattr(auto,'get_sync_db',lambda:iter([Session(engine)]))
    monkeypatch.setattr(auto,'validate_accounts_sync',lambda accounts:[{'account_id':a.id,'status':probe_status,'valid':probe_status=='valid','definitive':False} for a in accounts])
    auto.auto_validate_accounts()
    db_session.expire_all();current=db_session.get(Account,account_id)
    assert current.status=='error'
    assert current.last_error==reason
    assert current.error_count_recent==2
    assert current.last_checked_at.replace(tzinfo=timezone.utc)==now
    assert current.last_validation_attempt_at is not None


def test_validation_does_not_apply_result_to_replaced_cookies(db_session,monkeypatch):
    from services import auto_account_validator as auto
    from sqlalchemy.orm import Session
    account=Account(label='replaced-cookies',cookies='{"sessionid":"old"}',status='active')
    db_session.add(account);db_session.commit();account_id=account.id;engine=db_session.get_bind()
    monkeypatch.setattr(auto,'_last_validation_time',None)
    monkeypatch.setattr(auto,'_validation_running',False)
    monkeypatch.setattr(auto,'get_sync_db',lambda:iter([Session(engine)]))
    def validate(accounts):
        with Session(engine) as db:
            current=db.get(Account,account_id);current.cookies='{"sessionid":"new"}';db.commit()
        return [{'account_id':account_id,'status':'dead','definitive':True}]
    monkeypatch.setattr(auto,'validate_accounts_sync',validate)
    auto.auto_validate_accounts()
    db_session.expire_all();current=db_session.get(Account,account_id)
    assert current.status=='active'
    assert current.validation_fail_count==0
    assert current.cookies=='{"sessionid":"new"}'
