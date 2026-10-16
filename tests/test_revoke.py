"""৬ক. "অন্য ডিভাইস লগ আউট" (revoke) + OWNER_ID allowlist টেস্ট।"""

from __future__ import annotations

import pytest
from aiohttp.test_utils import TestClient, TestServer

import bot as botmod
from conftest import BOT_TOKEN, USER_A, USER_B, FakeClient, build_init_data, post

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def message_update(text=None, uid=None, chat_id=None):
    uid = uid or USER_A["id"]
    return {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "chat": {"id": chat_id if chat_id is not None else uid, "type": "private"},
            "from": {"id": uid},
            "text": text,
        },
    }


async def _make_session(web, store, user=None):
    """OTP ফ্লো চালিয়ে একটি সেশন তৈরি করে (revoke window চালু থাকে)।"""
    user = user or USER_A
    await store.set_phone(str(user["id"]), "+15550000001")
    await post(web, "/api/request-otp", {"initData": build_init_data(user=user)})
    return await post(web, "/api/verify-otp", {"initData": build_init_data(user=user), "otp": "12345"})


# --------------------------------------------------------------------------- #
# revoke: মূল আচরণ
# --------------------------------------------------------------------------- #
async def test_revoke_others_logs_out_phone_but_keeps_new_session(web, store, telethon):
    status, data = await _make_session(web, store)
    assert status == 200 and data["session"]
    before = telethon.last.session.save()

    status, data = await post(web, "/api/revoke-others", {"initData": build_init_data()})
    assert status == 200 and data["status"] == "ok"
    assert data["count"] == 2, data
    assert data["session_kept"] is True
    assert telethon.last.revoked_hashes == [2, 3], "current সেশন বাদে বাকিগুলোই বন্ধ হওয়া উচিত"
    assert telethon.last.session.save() == before, "session string অটুট থাকতে হবে"
    assert telethon.last.logged_out is False, "log_out() ডাকলে string-টাই মরে যেত"
    assert telethon.last.disconnected is True
    assert store.completed.get(str(USER_A["id"])) is None


async def test_revoke_others_requires_auth(web):
    for payload in ({}, {"initData": "hash=bad&auth_date=1&user=%7B%22id%22%3A1%7D"}):
        status, _ = await post(web, "/api/revoke-others", payload)
        assert status == 403


async def test_revoke_others_without_session_returns_409(web):
    status, data = await post(web, "/api/revoke-others", {"initData": build_init_data()})
    assert status == 409 and data["detail"]


async def test_revoke_others_is_bound_to_telegram_user(web, store, telethon):
    await _make_session(web, store, user=USER_A)
    status, data = await post(web, "/api/revoke-others", {"initData": build_init_data(user=USER_B)})
    assert status == 409, data
    assert telethon.last.revoked_hashes == [], "অন্য ইউজারের হয়ে লগ আউট করা যাবে না"


async def test_revoke_others_is_one_shot(web, store, telethon):
    await _make_session(web, store)
    assert (await post(web, "/api/revoke-others", {"initData": build_init_data()}))[0] == 200
    status, _ = await post(web, "/api/revoke-others", {"initData": build_init_data()})
    assert status == 409, "দ্বিতীয়বার client আর হাতে নেই"


async def test_revoke_handles_empty_authorization_list(cfg, fake_api, telethon):
    store = botmod.Store(cfg, client_factory=telethon)
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    app = botmod.make_app(cfg, api=fake_api, store=store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await post(client, "/api/request-otp", {"initData": build_init_data()})
        await post(client, "/api/verify-otp", {"initData": build_init_data(), "otp": "12345"})
        telethon.authorizations = None       # Telegram কিছু না দিলে
        status, data = await post(client, "/api/revoke-others", {"initData": build_init_data()})
        assert status == 200 and data["count"] == 0
    finally:
        await client.close()


async def test_revoke_others_expires_after_window(cfg, telethon):
    clock = {"t": 0.0}
    short = botmod.Config(**{**cfg.__dict__, "revoke_window": 60})
    store = botmod.Store(short, clock=lambda: clock["t"], client_factory=telethon)
    client = telethon()
    store.put_pending("1", "+15550000001", "H", client)
    store.complete("1", store.pending["1"])
    assert await store.get_completed("1") is not None
    clock["t"] += 61
    assert await store.get_completed("1") is None
    assert client.disconnected is True, "window শেষে client বন্ধ হয়ে যেতে হবে"
    assert client.logged_out is False


async def test_sweep_closes_stale_completed_clients(cfg, telethon):
    clock = {"t": 0.0}
    store = botmod.Store(cfg, clock=lambda: clock["t"], client_factory=telethon)
    client = telethon()
    store.put_pending("1", "+15550000001", "H", client)
    store.complete("1", store.pending["1"])
    clock["t"] += cfg.revoke_window + 1
    assert await store.sweep() == 1
    assert client.disconnected is True


async def test_new_phone_share_cancels_previous_pending(cfg, telethon, fake_api, store):
    """নতুন নম্বর শেয়ার করলে আগের অসম্পূর্ণ client বন্ধ হয়।"""
    app = botmod.make_app(cfg, api=fake_api, store=store)
    first = telethon()
    store.put_pending(str(USER_A["id"]), "+15550000001", "H", first)
    await store.set_phone(str(USER_A["id"]), "+15550000002")
    assert first.disconnected is True
    assert store.pending == {}


# --------------------------------------------------------------------------- #
# AUTO_REVOKE_OTHERS
# --------------------------------------------------------------------------- #
async def test_auto_revoke_others_env(cfg, fake_api, telethon):
    """AUTO_REVOKE_OTHERS=1 হলে সেশন তৈরির সঙ্গেই ফোন লগ আউট হয়ে যায়।"""
    auto = botmod.Config(**{**cfg.__dict__, "auto_revoke_others": True})
    store = botmod.Store(auto, client_factory=telethon)
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    app = botmod.make_app(auto, api=fake_api, store=store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await post(client, "/api/request-otp", {"initData": build_init_data()})
        status, data = await post(client, "/api/verify-otp", {"initData": build_init_data(), "otp": "12345"})
        assert status == 200 and data["auto_revoked"] is True
        assert len(data["revoked"]) == 2
        assert telethon.last.revoked_hashes == [2, 3]
        assert telethon.last.disconnected is True
        assert data["session"], "auto revoke হলেও session string পাওয়া যাবে"
        assert store.completed.get(str(USER_A["id"])) is None
    finally:
        await client.close()


async def test_auto_revoke_failure_still_returns_session(cfg, fake_api, telethon):
    """revoke ব্যর্থ হলেও ইউজার session string পাবে (শুধু সতর্কবার্তা)।"""
    auto = botmod.Config(**{**cfg.__dict__, "auto_revoke_others": True})
    store = botmod.Store(auto, client_factory=telethon)
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    app = botmod.make_app(auto, api=fake_api, store=store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await post(client, "/api/request-otp", {"initData": build_init_data()})

        original = FakeClient.__call__

        async def boom(request):  # GetAuthorizations/ResetAuthorization-এ exception
            raise RuntimeError("network down")

        FakeClient.__call__ = boom  # type: ignore[assignment]
        try:
            status, data = await post(
                client, "/api/verify-otp", {"initData": build_init_data(), "otp": "12345"}
            )
        finally:
            FakeClient.__call__ = original  # type: ignore[assignment]
        assert status == 200
        assert data["session"], "revoke ব্যর্থ হলেও session string দিতে হবে"
        assert data["auto_revoked"] is True and data["revoke_error"] is True
    finally:
        await client.close()


def test_env_flag_parsing():
    cfg = botmod.Config.from_env({
        "DEMO": "1", "AUTO_REVOKE_OTHERS": "true", "OWNER_ID": "123", "REVOKE_WINDOW": "60",
    })
    assert cfg.auto_revoke_others is True and cfg.owner_id == 123 and cfg.revoke_window == 60
    assert botmod.Config.from_env({"DEMO": "1", "AUTO_REVOKE_OTHERS": "0"}).auto_revoke_others is False
    with pytest.raises(botmod.ConfigError, match="AUTO_REVOKE_OTHERS"):
        botmod.Config.from_env({"DEMO": "1", "AUTO_REVOKE_OTHERS": "maybe"})


# --------------------------------------------------------------------------- #
# OWNER_ID allowlist
# --------------------------------------------------------------------------- #
async def test_owner_allowlist_blocks_other_users(cfg, fake_api, telethon):
    owned = botmod.Config(**{**cfg.__dict__, "owner_id": int(USER_A["id"])})
    store = botmod.Store(owned, client_factory=telethon)
    app = botmod.make_app(owned, api=fake_api, store=store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await store.set_phone(str(USER_A["id"]), "+15550000001")
        status, _ = await post(client, "/api/request-otp", {"initData": build_init_data(user=USER_A)})
        assert status == 200, "মালিকের অনুরোধ কাজ করবে"

        for path in ("/api/request-otp", "/api/verify-otp", "/api/verify-password", "/api/revoke-others", "/api/reset"):
            status, data = await post(client, path, {"initData": build_init_data(user=USER_B)})
            assert status == 403 and "ব্যক্তিগত" in data["detail"], (path, data)
    finally:
        await client.close()


async def test_owner_allowlist_ignores_other_users_in_chat(cfg, fake_api, store, telethon):
    owned = botmod.Config(**{**cfg.__dict__, "owner_id": int(USER_A["id"])})
    app = botmod.make_app(owned, api=fake_api, store=store)
    await botmod.handle_update(app, message_update("/start", uid=USER_B["id"]))
    texts = [p["text"] for p in fake_api.params_of("sendMessage")]
    assert texts and "ব্যক্তিগত" in texts[-1]
    assert store.get_phone(str(USER_B["id"])) is None


async def test_owner_can_use_bot_normally(cfg, fake_api, store, telethon):
    owned = botmod.Config(**{**cfg.__dict__, "owner_id": int(USER_A["id"])})
    app = botmod.make_app(owned, api=fake_api, store=store)
    await botmod.handle_update(app, message_update("/start", uid=USER_A["id"]))
    params = fake_api.params_of("sendMessage")[-1]
    assert params["chat_id"] == USER_A["id"]
    assert params["reply_markup"]["keyboard"][0][0]["request_contact"] is True


# --------------------------------------------------------------------------- #
# ব্যাকডোর-মুক্তি: সোর্স যাচাই
# --------------------------------------------------------------------------- #
def test_source_has_no_keyword_admin_backdoor():
    """কোডে keyword/পাসওয়ার্ড-ভিত্তিক গোপন admin backdoor থাকা যাবে না।"""
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    for bad in ("admin24", "ADMIN24", "backdoor", "secret_admin", "master_key"):
        assert bad not in source, f"সন্দেহজনক admin backdoor প্যাটার্ন: {bad}"
    assert "OWNER_ID" in source, "অ্যাক্সেস নিয়ন্ত্রণ OWNER_ID allowlist দিয়েই হতে হবে"
    assert "owner_id" in source


# --------------------------------------------------------------------------- #
# ডেমো মোডে revoke
# --------------------------------------------------------------------------- #
async def test_demo_mode_revoke_others_end_to_end():
    cfg = botmod.Config.from_env({"DEMO": "1", "WEBAPP_URL": "https://demo.test/app/"})
    store = botmod.Store(cfg)
    app = botmod.make_app(cfg, store=store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await post(client, "/api/request-otp", {})
        status, data = await post(client, "/api/verify-otp", {"otp": botmod.DEMO_CODE})
        assert status == 200 and data["status"] == "2fa_required"
        status, data = await post(client, "/api/verify-password", {"password": botmod.DEMO_PASSWORD})
        assert status == 200 and data["session"].startswith("DEMO-SESSION-")

        status, data = await post(client, "/api/revoke-others", {})
        assert status == 200 and data["count"] == 2
        assert data["session_kept"] is True
    finally:
        await client.close()


def test_demo_client_revoke_never_touches_current_session():
    cfg = botmod.Config.from_env({"DEMO": "1"})
    demo = botmod.DemoClient(cfg)
    demo.authorized = True
    import asyncio

    revoked = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        botmod.revoke_other_sessions(demo)
    )
    assert sorted(demo.revoked_hashes) == [2, 3]
    assert len(revoked) == 2
    assert demo.logged_out is False
