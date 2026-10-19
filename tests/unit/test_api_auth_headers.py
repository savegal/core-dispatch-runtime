"""Exercise header extraction through FastAPI, including its auth dependencies."""
import time
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from api import auth
from services.user_scope_signature import sign_user_scope


pytestmark = pytest.mark.unit


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(auth, "verify_admin_token", lambda token: token == "test-admin")
    monkeypatch.setattr(auth, "verify_executor_token", lambda token: token == "test-executor")
    monkeypatch.setattr(auth, "get_settings", lambda: SimpleNamespace(JWT_SECRET="test-scope-secret"))
    app = FastAPI()

    @app.get("/admin", dependencies=[Depends(auth.verify_admin_header)])
    def admin_route():
        return {"ok": True}

    @app.get("/executor", dependencies=[Depends(auth.verify_executor_header)])
    def executor_route():
        return {"ok": True}

    @app.get("/user")
    def user_route(user_id: int = Depends(auth.verify_user_header)):
        return {"user_id": user_id}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as test_client:
        yield test_client


@pytest.mark.parametrize("path,header,token", [
    ("/admin", "X-Admin-Token", "test-admin"),
    ("/executor", "X-Internal-Token", "test-executor"),
])
@pytest.mark.parametrize("provided", ["valid", "wrong", "missing"])
async def test_token_headers_are_extracted_and_enforced(client, path, header, token, provided):
    headers = {} if provided == "missing" else {header: token if provided == "valid" else "invalid"}
    response = await client.get(path, headers=headers)
    assert response.status_code == (200 if provided == "valid" else 403)


@pytest.mark.parametrize("missing", [None, "X-Admin-Token", "X-User-Id", "X-User-Ts", "X-User-Signature"])
async def test_user_scope_requires_all_signed_headers(client, missing):
    timestamp = int(time.time())
    headers = {
        "X-Admin-Token": "test-admin", "X-User-Id": "42", "X-User-Ts": str(timestamp),
        "X-User-Signature": sign_user_scope(user_id=42, timestamp=timestamp, secret="test-scope-secret"),
    }
    if missing:
        headers.pop(missing)
    response = await client.get("/user", headers=headers)
    assert response.status_code == (403 if missing else 200)
    if missing is None:
        assert response.json() == {"user_id": 42}


@pytest.mark.parametrize("tamper", ["user", "signature", "expired"])
async def test_user_scope_rejects_invalid_signed_headers(client, tamper):
    timestamp = int(time.time()) - (1000 if tamper == "expired" else 0)
    signature = sign_user_scope(user_id=42, timestamp=timestamp, secret="test-scope-secret")
    response = await client.get("/user", headers={
        "X-Admin-Token": "test-admin", "X-User-Id": "43" if tamper == "user" else "42",
        "X-User-Ts": str(timestamp), "X-User-Signature": "invalid" if tamper == "signature" else signature,
    })
    assert response.status_code == 403


async def test_token_cannot_be_supplied_as_query_parameter(client):
    response = await client.get("/admin", params={"x_admin_token": "test-admin"})
    assert response.status_code == 403


def test_full_application_imports_and_builds_openapi():
    # Import the entire route graph: this is where run_server.py used to crash.
    from main import app

    schema = app.openapi()
    parameters = schema["paths"]["/api/users/sync_telegram"]["post"]["parameters"]
    assert any(p["name"] == "x-admin-token" and p["in"] == "header" for p in parameters)
