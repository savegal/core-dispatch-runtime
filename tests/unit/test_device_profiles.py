from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest

from executor.browser_identity import chromium_brands, desktop_user_agent, sec_ch_ua
from executor.device_profiles import build_device_profile, device_family, install_device_profile, prepare_device_page


@pytest.mark.parametrize("ua", [None, "Windows NT 10.0 Chrome/120.0.0.0", "iPhone Mobile", "iPad Mobile",
                                "Macintosh Mobile", "Android 14 Mobile", "Android Tablet", "Macintosh", "X11 Linux"])
def test_legacy_user_agent_always_uses_windows_desktop(ua):
    account = SimpleNamespace(id=1219, user_agent=ua)
    profile = build_device_profile(account, {}, "145.0.7632.6")
    assert device_family(ua) == profile["identity"]["family"] == "windows"
    assert profile["identity"]["platform"] == profile["override"]["platform"] == "Win32"
    assert profile["options"]["user_agent"] == desktop_user_agent("145.0.7632.6")
    assert "Chrome/145.0.0.0" in profile["options"]["user_agent"]
    assert profile["options"]["has_touch"] is False
    assert profile["options"]["is_mobile"] is False
    assert profile["options"]["device_scale_factor"] == 1
    assert profile["identity"]["preset"] is None
    metadata = profile["override"]["userAgentMetadata"]
    assert metadata["platform"] == "Windows" and metadata["mobile"] is False
    assert metadata["model"] == "" and metadata["architecture"] == "x86"
    assert metadata["fullVersion"] == "145.0.7632.6"
    assert metadata["brands"] == chromium_brands("145.0.7632.6")


def test_stable_geometry_and_seeds_ignore_saved_user_agent():
    account = SimpleNamespace(id=1219, user_agent=None)
    profile = build_device_profile(account, {}, "145.0.7632.6")
    assert profile == build_device_profile(account, {}, "145.0.7632.6")
    account.user_agent = "iPhone Mobile"
    assert profile == build_device_profile(account, {}, "145.0.7632.6")
    assert profile["options"]["screen"]["height"] - profile["options"]["viewport"]["height"] == 90
    assert profile["identity"]["hardware"]["deviceMemory"] in (4, 8)
    full = build_device_profile(account, {}, "145.0.7632.6", chrome_height=0)
    assert full["options"]["screen"] == full["options"]["viewport"]
    other = build_device_profile(SimpleNamespace(id=1220, user_agent=None), {}, "145.0.7632.6")
    assert profile["identity"]["canvasSeed"] != other["identity"]["canvasSeed"]
    assert profile["identity"]["audioSeed"] != other["identity"]["audioSeed"]
    contrasting = build_device_profile(SimpleNamespace(id=1224, user_agent=None), {}, "145.0.7632.6")
    assert profile["identity"]["hardware"]["deviceMemory"] != contrasting["identity"]["hardware"]["deviceMemory"]
    assert profile["identity"]["hardware"]["webglRenderer"] != contrasting["identity"]["hardware"]["webglRenderer"]


@pytest.mark.parametrize("version,grease,number", [("143.0.1.2", "Not A(Brand", "24"),
                                                   ("145.0.7632.6", "Not:A-Brand", "99")])
def test_chromium_grease_tracks_engine_major(version, grease, number):
    brands = chromium_brands(version)
    assert len(brands) == 3
    assert {"brand": grease, "version": number} in brands
    full = chromium_brands(version, full=True)
    assert [item["brand"] for item in brands] == [item["brand"] for item in full]
    assert {"brand": grease, "version": number + ".0.0.0"} in full
    assert {"brand": "Chromium", "version": version} in full
    assert f'"{grease}";v="{number}"' in sec_ch_ua(version)


def test_invalid_engine_version_is_not_silently_replaced():
    with pytest.raises(ValueError, match="Invalid Chromium"):
        build_device_profile(SimpleNamespace(id=1), {}, "unknown")


@pytest.mark.asyncio
async def test_page_preparation_is_shared_and_uses_native_cdp():
    import asyncio
    context = SimpleNamespace(add_init_script=AsyncMock(), on=Mock(), new_cdp_session=AsyncMock())
    session = SimpleNamespace(send=AsyncMock())
    context.new_cdp_session.return_value = session
    profile = build_device_profile(SimpleNamespace(id=1, user_agent=None), {}, "145.0.7632.6")
    await install_device_profile(context, profile, False)
    assert context._account_canvas_noise is True
    page = SimpleNamespace(context=context)
    await asyncio.gather(prepare_device_page(page), prepare_device_page(page))
    context.new_cdp_session.assert_awaited_once_with(page)
    assert session.send.await_args_list == [
        call("Emulation.setUserAgentOverride", profile["override"]),
        call("Emulation.setHardwareConcurrencyOverride", {"hardwareConcurrency":profile["identity"]["hardware"]["hardwareConcurrency"]}),
    ]


@pytest.mark.asyncio
async def test_failure_to_apply_native_metadata_is_propagated():
    context = SimpleNamespace(_account_device_profile={"override": {}},
                              new_cdp_session=AsyncMock(side_effect=RuntimeError("closed")))
    with pytest.raises(RuntimeError, match="closed"):
        await prepare_device_page(SimpleNamespace(context=context))


def test_deterministic_seed_priority_chain():
    import json

    # 1. Same uid_tt across different Account.id produces identical hardware profile
    acc_uid_a = SimpleNamespace(id=101, cookies=json.dumps([{"name": "uid_tt", "value": "uid_abc1234567890"}]))
    acc_uid_b = SimpleNamespace(id=202, cookies=json.dumps([{"name": "uid_tt", "value": "uid_abc1234567890"}]))
    p_uid_a = build_device_profile(acc_uid_a, {}, "145.0.7632.6")
    p_uid_b = build_device_profile(acc_uid_b, {}, "145.0.7632.6")
    assert p_uid_a["identity"] == p_uid_b["identity"]

    # 2. Cached _cached_tiktok_uid matches uid_tt
    acc_cached_uid = SimpleNamespace(id=303, _cached_tiktok_uid="uid_abc1234567890", cookies="[]")
    p_cached_uid = build_device_profile(acc_cached_uid, {}, "145.0.7632.6")
    assert p_cached_uid["identity"] == p_uid_a["identity"]

    # 3. Stable session token (sid_tt / sessionid / 32-char prefix of sid_guard) when uid_tt is absent
    sid_token = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    acc_sid = SimpleNamespace(id=401, cookies=json.dumps([{"name": "sid_tt", "value": sid_token}]))
    acc_guard = SimpleNamespace(
        id=402,
        cookies=json.dumps([{"name": "sid_guard", "value": f"{sid_token}%7C1700000000%7C5184000%7CMon"}]),
    )
    p_sid = build_device_profile(acc_sid, {}, "145.0.7632.6")
    p_guard = build_device_profile(acc_guard, {}, "145.0.7632.6")
    assert p_sid["identity"] == p_guard["identity"]

    # 4. Non-technical label vs technical label fallback to Account.id
    acc_label_a = SimpleNamespace(id=501, label="@RealCreator_One", cookies="[]")
    acc_label_b = SimpleNamespace(id=502, label="realcreator_one", cookies="[]")
    assert build_device_profile(acc_label_a, {}, "145.0.7632.6")["identity"] == build_device_profile(acc_label_b, {}, "145.0.7632.6")["identity"]

    acc_tech_a = SimpleNamespace(id=601, label="account_601", cookies="[]")
    acc_tech_b = SimpleNamespace(id=601, label=None, cookies="[]")
    assert build_device_profile(acc_tech_a, {}, "145.0.7632.6")["identity"] == build_device_profile(acc_tech_b, {}, "145.0.7632.6")["identity"]

