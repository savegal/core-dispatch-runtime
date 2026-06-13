from types import SimpleNamespace

import pytest

from executor import tiktok_client_async
from executor.tiktok_client_async import TikTokClientError
from services import account_service
from services import cookie_persistence_service


def test_get_cookies_for_account_reads_relative_cookie_file(monkeypatch, tmp_path):
    cookies_dir = tmp_path / "cookies"
    cookies_dir.mkdir()
    cookie_file = cookies_dir / "account.json"
    cookie_file.write_text('{"sessionid":"abc"}', encoding="utf-8")
    monkeypatch.setattr(
        tiktok_client_async,
        "settings",
        SimpleNamespace(COOKIES_DIR=str(cookies_dir)),
    )

    account = SimpleNamespace(id=1, cookies="", cookie_file="account.json")

    assert tiktok_client_async._get_cookies_for_account(account) == '{"sessionid":"abc"}'


def test_get_cookies_for_account_reads_absolute_cookie_file_inside_cookie_dir(monkeypatch, tmp_path):
    cookies_dir = tmp_path / "cookies"
    cookies_dir.mkdir()
    cookie_file = cookies_dir / "account.json"
    cookie_file.write_text('{"sessionid":"abc"}', encoding="utf-8")
    monkeypatch.setattr(
        tiktok_client_async,
        "settings",
        SimpleNamespace(COOKIES_DIR=str(cookies_dir)),
    )

    account = SimpleNamespace(id=1, cookies="", cookie_file=str(cookie_file))

    assert tiktok_client_async._get_cookies_for_account(account) == '{"sessionid":"abc"}'


def test_get_cookies_for_account_rejects_absolute_cookie_file_outside_cookie_dir(monkeypatch, tmp_path):
    cookies_dir = tmp_path / "cookies"
    cookies_dir.mkdir()
    outside_file = tmp_path / "outside.json"
    outside_file.write_text('{"sessionid":"leaked"}', encoding="utf-8")
    monkeypatch.setattr(
        tiktok_client_async,
        "settings",
        SimpleNamespace(COOKIES_DIR=str(cookies_dir)),
    )

    account = SimpleNamespace(id=1, cookies="", cookie_file=str(outside_file))

    with pytest.raises(TikTokClientError):
        tiktok_client_async._get_cookies_for_account(account)


def test_save_cookies_to_file_writes_relative_path(monkeypatch, tmp_path):
    cookies_dir = tmp_path / "cookies"
    monkeypatch.setattr(
        cookie_persistence_service,
        "settings",
        SimpleNamespace(COOKIES_DIR=str(cookies_dir)),
    )

    saved = cookie_persistence_service.save_cookies_to_file(
        "account.json",
        [{"name": "sessionid", "value": "abc"}],
    )

    assert saved is True
    assert (cookies_dir / "account.json").exists()


def test_save_cookies_to_file_rejects_absolute_path(monkeypatch, tmp_path):
    cookies_dir = tmp_path / "cookies"
    outside_file = tmp_path / "outside.json"
    monkeypatch.setattr(
        cookie_persistence_service,
        "settings",
        SimpleNamespace(COOKIES_DIR=str(cookies_dir)),
    )

    saved = cookie_persistence_service.save_cookies_to_file(
        str(outside_file),
        [{"name": "sessionid", "value": "abc"}],
    )

    assert saved is False
    assert not outside_file.exists()


def test_account_service_reads_relative_cookie_file_from_cookie_dir(monkeypatch, tmp_path):
    cookies_dir = tmp_path / "cookies"
    cookies_dir.mkdir()
    cookie_file = cookies_dir / "account.json"
    cookie_file.write_text('{"sessionid":"abc"}', encoding="utf-8")
    monkeypatch.setattr(
        account_service,
        "settings",
        SimpleNamespace(COOKIES_DIR=str(cookies_dir)),
    )

    account = SimpleNamespace(id=1, cookies="", cookie_file="account.json")

    assert account_service._read_account_cookie_source(account) == '{"sessionid":"abc"}'


def test_account_service_reads_absolute_cookie_file_inside_cookie_dir(monkeypatch, tmp_path):
    cookies_dir = tmp_path / "cookies"
    cookies_dir.mkdir()
    cookie_file = cookies_dir / "account.json"
    cookie_file.write_text('{"sessionid":"abc"}', encoding="utf-8")
    monkeypatch.setattr(
        account_service,
        "settings",
        SimpleNamespace(COOKIES_DIR=str(cookies_dir)),
    )

    account = SimpleNamespace(id=1, cookies="", cookie_file=str(cookie_file))

    assert account_service._read_account_cookie_source(account) == '{"sessionid":"abc"}'


def test_account_service_rejects_absolute_cookie_file(monkeypatch, tmp_path):
    cookies_dir = tmp_path / "cookies"
    cookies_dir.mkdir()
    outside_file = tmp_path / "outside.json"
    outside_file.write_text('{"sessionid":"leaked"}', encoding="utf-8")
    monkeypatch.setattr(
        account_service,
        "settings",
        SimpleNamespace(COOKIES_DIR=str(cookies_dir)),
    )

    account = SimpleNamespace(id=1, cookies="", cookie_file=str(outside_file))

    assert account_service._read_account_cookie_source(account) == ""
