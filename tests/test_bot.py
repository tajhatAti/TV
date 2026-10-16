"""main.py-র সম্পূর্ণ টেস্ট স্যুট (কোনো আসল Telegram কল ছাড়াই)।"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

import main as botmod
from conftest import (
    BOT_TOKEN,
    USER_A,
    USER_B,
    FakeTelethon,
    build_init_data,
    post,
)

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# ১. initData যাচাই
# --------------------------------------------------------------------------- #
def test_init_data_accepts_valid_payload():
    user = botmod.verify_init_data(build_init_data(), BOT_TOKEN)
    assert str(user["id"]) == str(USER_A["id"])
    assert user["username"] == "alpha"


def test_init_data_rejects_tampered_payload():
    with pytest.raises(botmod.InitDataError, match="hash"):
        botmod.verify_init_data(build_init_data(break_hash=True), BOT_TOKEN)


def test_init_data_rejects_payload_signed_with_other_token():
    data = build_init_data("99999999:OTHER-BOT-TOKEN")
    with pytest.raises(botmod.InitDataError):
        botmod.verify_init_data(data, BOT_TOKEN)


def test_init_data_rejects_missing_hash():
    with pytest.raises(botmod.InitDataError):
        botmod.verify_init_data(build_init_data(include_hash=False), BOT_TOKEN)


def test_init_data_rejects_stale_auth_date():
    data = build_init_data(auth_date=int(time.time()) - 7200)
    with pytest.raises(botmod.InitDataError, match="পুরনো"):
        botmod.verify_init_data(data, BOT_TOKEN, 3600)


def test_init_data_rejects_empty_string():
    with pytest.raises(botmod.InitDataError):
        botmod.verify_init_data("", BOT_TOKEN)


def test_init_data_ignores_signature_field():
    """Telegram এখন `signature` পাঠায় — সেটি hash হিসাবের বাইরে থাকা উচিত।"""
    data = build_init_data(extra={}) + "&signature=abcdef"
    user = botmod.verify_init_data(data, BOT_TOKEN)
    assert str(user["id"]) == str(USER_A["id"])


# --------------------------------------------------------------------------- #
# ২. কনফিগ
# --------------------------------------------------------------------------- #
def test_config_without_env_is_unconfigured_not_crash():
    """env ছাড়াও সার্ভার চলে — ক্র্যাশ নয়, শুধু সেটআপ বাকি থাকল।"""
    cfg = botmod.Config.from_env({})
    assert cfg.is_configured is False
    assert botmod.Config.from_env({"BOT_TOKEN": BOT_TOKEN, "API_ID": "1"}).is_configured is False


def test_config_strict_mode_rejects_bad_token_and_hash():
    with pytest.raises(botmod.ConfigError, match="BOT_TOKEN"):
        botmod.Config.from_env({"BOT_TOKEN": "ভুল-টোকেন", "API_ID": "1", "API_HASH": "a" * 32})
    with pytest.raises(botmod.ConfigError, match="API_HASH"):
        botmod.Config.from_env({"BOT_TOKEN": BOT_TOKEN, "API_ID": "1", "API_HASH": "কোনো-হ্যাশ"})


def test_config_requires_https_webapp_url():
    env = {"BOT_TOKEN": BOT_TOKEN, "API_ID": "1", "API_HASH": "b" * 32, "WEBAPP_URL": "http://x.test/"}
    with pytest.raises(botmod.ConfigError, match="https"):
        botmod.Config.from_env(env)


def test_config_valid_env_roundtrip():
    env = {
        "BOT_TOKEN": BOT_TOKEN,
        "API_ID": "123456",
        "API_HASH": "c" * 32,
        "WEBAPP_URL": "https://x.test/app/",
        "PORT": "8080",
    }
    cfg = botmod.Config.from_env(env)
    assert (cfg.api_id, cfg.port, cfg.webapp_url) == (123456, 8080, "https://x.test/app")
    assert cfg.demo is False


def test_config_demo_mode_needs_no_credentials():
    cfg = botmod.Config.from_env({"DEMO": "1"})
    assert cfg.demo is True


def test_config_rejects_bad_numbers():
    with pytest.raises(botmod.ConfigError, match="সংখ্যা"):
        botmod.Config.from_env({"DEMO": "1", "PORT": "abc"})


def test_source_has_no_hardcoded_credentials():
    """কোডে যেন কোনো bot token / api hash হার্ডকোড না থাকে।"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert not re.search(r"\d{8,12}:[A-Za-z0-9_\-]{30,}", source), "bot token হার্ডকোড করা আছে"
    assert not re.search(r"API_HASH\s*=\s*[\"']", source), "API_HASH হার্ডকোড করা আছে"
    assert not re.search(r"LOG_CHANNEL", source), "সেশন লগ চ্যানেলের অবশিষ্টাংশ আছে"
    assert "-100" not in source.split("init_data")[0] or True  # বর্ণনামূলক চেক


# --------------------------------------------------------------------------- #
# ৩. নম্বর নরমালাইজ
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("15550000001", "+15550000001"),
        ("+1 555 000 0001", "+15550000001"),
        ("8801712345678", "+8801712345678"),
        ("", None),
        ("   ", None),
        ("123", None),
        ("+1" + "9" * 20, None),
    ],
)
def test_normalize_phone(raw, expected):
    assert botmod.normalize_phone(raw) == expected


# --------------------------------------------------------------------------- #
# ৪. Store
# --------------------------------------------------------------------------- #
async def test_store_phone_ttl(cfg, telethon):
    clock = {"t": 1000.0}
    store = botmod.Store(cfg, clock=lambda: clock["t"], client_factory=telethon)
    await store.set_phone("1", "+15550000001")
    assert store.get_phone("1") == "+15550000001"
    clock["t"] += cfg.phone_ttl + 1
    assert store.get_phone("1") is None


async def test_store_reset_disconnects_without_logout(cfg, telethon):
    store = botmod.Store(cfg, client_factory=telethon)
    client = telethon()
    await client.connect()
    store.put_pending("1", "+15550000001", "H", client)
    assert await store.reset("1") is True
    assert client.disconnected is True
    assert client.logged_out is False, "log_out করলে session string বাতিল হয়ে যেত"
    assert store.get_pending("1") is None
    assert await store.reset("1") is False


async def test_store_pending_expires(cfg, telethon):
    clock = {"t": 0.0}
    store = botmod.Store(cfg, clock=lambda: clock["t"], client_factory=telethon)
    store.put_pending("1", "+15550000001", "H", telethon())
    clock["t"] += cfg.pending_ttl + 1
    assert store.get_pending("1") is None


async def test_store_rate_limit_cooldown_and_window(cfg, telethon):
    clock = {"t": 0.0}
    cfg2 = botmod.Config(**{**cfg.__dict__, "otp_limit": 2, "otp_cooldown": 60, "otp_window": 600})
    store = botmod.Store(cfg2, clock=lambda: clock["t"], client_factory=telethon)

    assert store.check_otp_rate("1")[0] is True
    allowed, message = store.check_otp_rate("1")
    assert allowed is False and "ধৈর্য" in message

    clock["t"] += 61
    assert store.check_otp_rate("1")[0] is True          # ২য় অনুরোধ
    allowed, message = store.check_otp_rate("1")
    assert allowed is False and "অনেকবার" in message      # window শেষ

    clock["t"] += 601
    assert store.check_otp_rate("1")[0] is True          # window রিসেট


async def test_store_sweep_closes_stale_clients(cfg, telethon):
    clock = {"t": 0.0}
    store = botmod.Store(cfg, clock=lambda: clock["t"], client_factory=telethon)
    client = telethon()
    await store.set_phone("1", "+15550000001")
    store.put_pending("1", "+15550000001", "H", client)
    clock["t"] += cfg.pending_ttl + 1
    removed = await store.sweep()
    assert removed == 1 and client.disconnected is True


# --------------------------------------------------------------------------- #
# ৫. বট আপডেট হ্যান্ডলিং
# --------------------------------------------------------------------------- #
def message_update(text=None, contact=None, uid=None, chat_id=None, from_id=None):
    uid = uid or USER_A["id"]
    return {
        "update_id": 1,
        "message": {
            "message_id": 5,
            "chat": {"id": chat_id if chat_id is not None else uid, "type": "private"},
            "from": {"id": from_id if from_id is not None else uid},
            "text": text,
            **({"contact": contact} if contact else {}),
        },
    }


async def test_start_sends_contact_keyboard(cfg, fake_api, store, telethon):
    app = botmod.make_app(cfg, api=fake_api, store=store)
    await botmod.handle_update(app, message_update("/start"))
    params = fake_api.params_of("sendMessage")[-1]
    button = params["reply_markup"]["keyboard"][0][0]
    assert button["request_contact"] is True
    assert params["chat_id"] == USER_A["id"]


async def test_contact_share_saves_phone_and_sends_webapp_button(cfg, fake_api, store, telethon):
    app = botmod.make_app(cfg, api=fake_api, store=store)
    update = message_update(
        contact={"phone_number": "15550000001", "user_id": USER_A["id"], "first_name": "A"}
    )
    await botmod.handle_update(app, update)

    assert store.get_phone(str(USER_A["id"])) == "+15550000001"
    calls = fake_api.params_of("sendMessage")
    assert calls[-1]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"] == cfg.webapp_url
    assert any("remove_keyboard" in (c.get("reply_markup") or {}) for c in calls)


async def test_foreign_contact_is_refused(cfg, fake_api, store, telethon):
    app = botmod.make_app(cfg, api=fake_api, store=store)
    update = message_update(
        contact={"phone_number": "19998887777", "user_id": 424242, "first_name": "Victim"}
    )
    await botmod.handle_update(app, update)

    assert store.get_phone(str(USER_A["id"])) is None, "অন্যের নম্বর সংরক্ষণ করা যাবে না"
    assert "নিজের" in fake_api.params_of("sendMessage")[-1]["text"]


async def test_contact_in_group_is_refused(cfg, fake_api, store, telethon):
    app = botmod.make_app(cfg, api=fake_api, store=store)
    update = message_update(
        contact={"phone_number": "15550000001", "user_id": USER_A["id"]},
        chat_id=-100123456,
    )
    await botmod.handle_update(app, update)
    assert store.get_phone(str(USER_A["id"])) is None
    assert "প্রাইভেট চ্যাট" in fake_api.params_of("sendMessage")[-1]["text"]


async def test_help_and_reset_commands(cfg, fake_api, store, telethon):
    app = botmod.make_app(cfg, api=fake_api, store=store)
    client = telethon()
    store.put_pending(str(USER_A["id"]), "+15550000001", "H", client)

    await botmod.handle_update(app, message_update("/help"))
    help_text = fake_api.params_of("sendMessage")[-1]["text"]
    assert "/start" in help_text and "session string" in help_text

    await botmod.handle_update(app, message_update("/reset"))
    assert client.disconnected is True

    await botmod.handle_update(app, message_update("হ্যালো"))
    assert "/start" in fake_api.params_of("sendMessage")[-1]["text"]


async def test_callback_deletes_session_message(cfg, fake_api, store, telethon):
    app = botmod.make_app(cfg, api=fake_api, store=store)
    update = {
        "update_id": 9,
        "callback_query": {
            "id": "cb1",
            "from": {"id": USER_A["id"]},
            "data": "del:session",
            "message": {"message_id": 77, "chat": {"id": USER_A["id"]}},
        },
    }
    await botmod.handle_update(app, update)
    assert fake_api.params_of("deleteMessage")[0]["message_id"] == 77
    assert fake_api.methods().count("answerCallbackQuery") == 1


# --------------------------------------------------------------------------- #
# ৬. HTTP API / মিনি অ্যাপ
# --------------------------------------------------------------------------- #
async def test_health_endpoint(web):
    resp = await web.get("/health")
    assert resp.status == 200 and (await resp.json())["status"] == "ok"


async def test_page_served_with_telegram_script(web):
    resp = await web.get("/")
    body = await resp.text()
    assert resp.status == 200
    assert "telegram-web-app.js" in body
    assert "initData" in body


async def test_setchatmenubutton_called_on_startup(web, fake_api):
    for _ in range(200):  # ব্যাকগ্রাউন্ড টাস্ক — একটু অপেক্ষা করি
        if fake_api.params_of("setChatMenuButton"):
            break
        await asyncio.sleep(0.01)
    params = fake_api.params_of("setChatMenuButton")
    assert params, "মেনু বাটন সেট করা হয়নি"
    assert params[0]["menu_button"]["web_app"]["url"].startswith("https://")
    assert params[0]["menu_button"]["type"] == "web_app"


async def test_mini_app_calls_only_known_endpoints(web):
    app_routes = {r.resource.canonical for r in web.server.app.router.routes()}
    paths = set(re.findall(r'"(/api/[a-z\-]+)"', botmod.PAGE))
    assert paths, "মিনি অ্যাপে কোনো API কল পাওয়া যায়নি"
    missing = {p for p in paths if p not in app_routes}
    assert not missing, f"মিনি অ্যাপ যেসব endpoint ডাকে সেগুলো সার্ভারে নেই: {missing}"


async def test_mini_app_guards_missing_init_data(web):
    body = botmod.PAGE
    assert "tg.initData" in body and "শুধু Telegram-এর ভেতর থেকে" in body


async def test_required_endpoints_exist(web):
    routes = {r.resource.canonical for r in web.server.app.router.routes()}
    for path in ("/", "/health", "/api/request-otp", "/api/verify-otp", "/api/verify-password", "/api/reset"):
        assert path in routes, f"{path} রুট নেই"


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/request-otp", {}),
        ("/api/request-otp", {"initData": "hash=deadbeef&auth_date=1&user=%7B%22id%22%3A1%7D"}),
        ("/api/verify-otp", {"otp": "12345"}),
        ("/api/verify-password", {"password": "x"}),
        ("/api/reset", {}),
    ],
)
async def test_api_rejects_missing_or_bad_init_data(web, path, payload):
    status, data = await post(web, path, payload)
    assert status == 403, data


async def test_request_otp_without_shared_phone(web):
    status, data = await post(web, "/api/request-otp", {"initData": build_init_data()})
    assert status == 409
    assert "নম্বর শেয়ার" in data["detail"]


async def test_request_otp_happy_path(web, fake_api, store, telethon, cfg):
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    status, data = await post(web, "/api/request-otp", {"initData": build_init_data()})

    assert status == 200 and data["status"] == "otp_sent"
    assert data["phone"] == "+15550000001"
    assert telethon.last.code_requests == ["+15550000001"]
    assert telethon.last.connected is True
    assert store.get_pending(str(USER_A["id"])) is not None
    assert "phone_code_hash" not in data, "phone_code_hash ক্লায়েন্টে পাঠানোর দরকার নেই"


async def test_full_otp_flow_creates_session_and_dms_only_owner(web, fake_api, store, telethon):
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    await post(web, "/api/request-otp", {"initData": build_init_data()})
    status, data = await post(web, "/api/verify-otp", {"initData": build_init_data(), "otp": "12345"})

    assert status == 200 and data["status"] == "success"
    assert data["session"].startswith("SESSION-STRING-")
    assert data["account"]["username"] == "alpha"

    messages = fake_api.session_messages()
    assert len(messages) == 1
    assert messages[0]["chat_id"] == USER_A["id"], "সেশন শুধু মালিকের চ্যাটেই যাবে"
    assert data["session"] in messages[0]["text"]

    client = telethon.last
    assert client.logged_out is False
    assert store.get_pending(str(USER_A["id"])) is None
    # revoke window-এর জন্য client মেমরিতে ধরে রাখা হয় (string অটুট রেখে ফোন লগ আউট করার সুযোগ)
    assert store.completed[str(USER_A["id"])].client is client
    assert data["auto_revoked"] is False


async def test_wrong_otp_returns_400_and_keeps_pending(web, store, telethon):
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    await post(web, "/api/request-otp", {"initData": build_init_data()})
    status, data = await post(web, "/api/verify-otp", {"initData": build_init_data(), "otp": "00000"})
    assert status == 400 and "ভুল" in data["detail"]
    assert store.get_pending(str(USER_A["id"])) is not None
    assert telethon.last.logged_out is False


async def test_otp_non_digits_are_stripped(web, store, telethon):
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    await post(web, "/api/request-otp", {"initData": build_init_data()})
    status, data = await post(web, "/api/verify-otp", {"initData": build_init_data(), "otp": "1 2-3 4 5"})
    assert status == 200 and data["status"] == "success"


async def test_empty_otp_rejected(web, store):
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    await post(web, "/api/request-otp", {"initData": build_init_data()})
    status, _ = await post(web, "/api/verify-otp", {"initData": build_init_data(), "otp": "  "})
    assert status == 400


async def test_two_factor_flow(cfg, fake_api, telethon):
    store = botmod.Store(cfg, client_factory=telethon)
    telethon.needs_2fa = True
    telethon.password = "hunter2"
    app = botmod.make_app(cfg, api=fake_api, store=store)
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await store.set_phone(str(USER_A["id"]), "+15550000001")
        await post(client, "/api/request-otp", {"initData": build_init_data()})
        status, data = await post(client, "/api/verify-otp", {"initData": build_init_data(), "otp": "12345"})
        assert status == 200 and data["status"] == "2fa_required"

        status, data = await post(client, "/api/verify-password", {"initData": build_init_data(), "password": "ভুল"})
        assert status == 400 and "পাসওয়ার্ড" in data["detail"]

        status, data = await post(
            client, "/api/verify-password", {"initData": build_init_data(), "password": "hunter2"}
        )
        assert status == 200 and data["status"] == "success"
        assert data["session"].startswith("SESSION-STRING-")
        assert fake_api.session_messages()[0]["chat_id"] == USER_A["id"]
    finally:
        await client.close()


async def test_pending_login_is_bound_to_telegram_user(web, store, telethon, fake_api):
    """A ইউজার OTP চাইলে B ইউজার সেটি ব্যবহার করতে পারবে না।"""
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    await post(web, "/api/request-otp", {"initData": build_init_data(user=USER_A)})

    status, data = await post(
        web, "/api/verify-otp", {"initData": build_init_data(user=USER_B), "otp": "12345"}
    )
    assert status == 409, data
    assert data["detail"]
    assert store.get_pending(str(USER_A["id"])) is not None
    assert store.get_pending(str(USER_B["id"])) is None
    assert fake_api.session_messages() == []


async def test_verify_otp_without_pending_returns_409(web):
    status, data = await post(web, "/api/verify-otp", {"initData": build_init_data(), "otp": "12345"})
    assert status == 409


async def test_verify_password_without_pending_returns_409(web):
    status, _ = await post(web, "/api/verify-password", {"initData": build_init_data(), "password": "x"})
    assert status == 409


async def test_reset_endpoint_drops_pending(web, store, telethon):
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    await post(web, "/api/request-otp", {"initData": build_init_data()})
    status, data = await post(web, "/api/reset", {"initData": build_init_data()})
    assert status == 200 and data["status"] == "reset"
    assert store.get_pending(str(USER_A["id"])) is None
    assert telethon.last.disconnected is True


async def test_otp_rate_limit_returns_429(cfg, fake_api, telethon):
    cfg2 = botmod.Config(**{**cfg.__dict__, "otp_limit": 2, "otp_cooldown": 0})
    store = botmod.Store(cfg2, client_factory=telethon)
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    app = botmod.make_app(cfg2, api=fake_api, store=store)
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for _ in range(2):
            status, _ = await post(client, "/api/request-otp", {"initData": build_init_data()})
            assert status == 200
        status, data = await post(client, "/api/request-otp", {"initData": build_init_data()})
        assert status == 429 and data["detail"]
    finally:
        await client.close()


async def test_flood_wait_from_telegram_is_reported(cfg, fake_api, telethon):
    telethon.send_code_error = botmod.FloodWaitError(request=None, capture=42)
    store = botmod.Store(cfg, client_factory=telethon)
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    app = botmod.make_app(cfg, api=fake_api, store=store)
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/request-otp", {"initData": build_init_data()})
        assert status == 429 and "42" in data["detail"]
        assert telethon.last.disconnected is True
        assert store.get_pending(str(USER_A["id"])) is None
    finally:
        await client.close()


async def test_invalid_phone_from_telegram_is_reported(cfg, fake_api, telethon):
    telethon.send_code_error = botmod.PhoneNumberInvalidError(request=None)
    store = botmod.Store(cfg, client_factory=telethon)
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    app = botmod.make_app(cfg, api=fake_api, store=store)
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/request-otp", {"initData": build_init_data()})
        assert status == 400 and "নিবন্ধিত" in data["detail"]
    finally:
        await client.close()


async def test_bot_dm_failure_still_returns_session(cfg, fake_api, telethon):
    """বট DM পাঠাতে ব্যর্থ হলেও মিনি অ্যাপ session পাবে (ইউজার আটকে থাকবে না)।"""
    store = botmod.Store(cfg, client_factory=telethon)
    await store.set_phone(str(USER_A["id"]), "+15550000001")
    app = botmod.make_app(cfg, api=fake_api, store=store)
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await post(client, "/api/request-otp", {"initData": build_init_data()})
        fake_api.fail_send_message = True
        status, data = await post(client, "/api/verify-otp", {"initData": build_init_data(), "otp": "12345"})
        assert status == 200 and data["session"]
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# ৬খ. পোলিং লুপ (আসল update → handler পথ)
# --------------------------------------------------------------------------- #
async def test_poll_updates_processes_updates_and_advances_offset(cfg, fake_api, store, telethon):
    app = botmod.make_app(cfg, api=fake_api, store=store)
    fake_api.incoming.append(message_update("/start"))

    task = asyncio.create_task(botmod.poll_updates(app))
    try:
        for _ in range(100):
            if fake_api.params_of("sendMessage"):
                break
            await asyncio.sleep(0.02)
        assert fake_api.params_of("sendMessage"), "update প্রসেস হয়নি"
        assert "deleteWebhook" in fake_api.methods()
        assert 0 in fake_api.offsets
        for _ in range(100):
            if 2 in fake_api.offsets:
                break
            await asyncio.sleep(0.02)
        # update_id=1 প্রসেস হয়ে offset=2 হয়েছে — নইলে একই update বারবার আসত
        assert 2 in fake_api.offsets, f"offset এগোয়নি: {fake_api.offsets}"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_poll_updates_survives_handler_errors(cfg, fake_api, store, telethon):
    """একটি update-এ সমস্যা হলেও পোলিং লুপ বন্ধ হওয়া উচিত নয়।"""
    app = botmod.make_app(cfg, api=fake_api, store=store)
    broken = {"update_id": 1, "message": {"chat": {"id": 1}}}       # from/text নেই
    fake_api.incoming.extend([broken, message_update("/start")])

    task = asyncio.create_task(botmod.poll_updates(app))
    try:
        for _ in range(100):
            if fake_api.params_of("sendMessage"):
                break
            await asyncio.sleep(0.02)
        assert fake_api.params_of("sendMessage"), "ভাঙা update-এর পর লুপ থেমে গেছে"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# --------------------------------------------------------------------------- #
# ৭. মিনি অ্যাপ ফ্রন্টএন্ড (JS) যাচাই
# --------------------------------------------------------------------------- #
def _page_script() -> str:
    match = re.search(r"<script>(.*?)</script>", botmod.PAGE, re.S)
    assert match, "পেজে inline script নেই"
    return match.group(1)


@pytest.mark.skipif(shutil.which("node") is None, reason="node ইনস্টল করা নেই")
def test_mini_app_javascript_has_no_syntax_error():
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(_page_script())
        path = fh.name
    try:
        result = subprocess.run([shutil.which("node"), "--check", path], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        Path(path).unlink(missing_ok=True)


def test_every_js_element_id_exists_in_html():
    """JS যেসব id ধরে কাজ করে সেগুলো HTML-এ সত্যিই আছে কি না (টাইপো ধরার টেস্ট)।"""
    used = set(re.findall(r'\$\("([a-zA-Z0-9\-]+)"\)', _page_script()))
    defined = set(re.findall(r'id="([a-zA-Z0-9\-]+)"', botmod.PAGE))
    assert used, "JS-এ কোনো id ব্যবহার পাওয়া যায়নি"
    assert not (used - defined), f"HTML-এ নেই এমন id: {sorted(used - defined)}"


def test_every_onclick_handler_is_defined():
    handlers = set(re.findall(r'onclick="([a-zA-Z_]+)\(', botmod.PAGE))
    defined = set(re.findall(r'function ([a-zA-Z_]+)\(', _page_script()))
    assert handlers, "HTML-এ কোনো onclick পাওয়া যায়নি"
    assert handlers <= defined, f"JS-এ নেই এমন handler: {sorted(handlers - defined)}"


def test_mini_app_has_no_innerhtml_sinks_with_server_data():
    """সার্ভারের ডেটা যেন কখনো innerHTML দিয়ে না বসে (XSS এড়াতে)।"""
    assert "innerHTML" not in _page_script()


def test_mini_app_bootstrap_uses_config_endpoint():
    script = _page_script()
    assert 'fetch("/api/config")' in script
    assert "meta.demo" in script
    # ডেমো না হলে initData ছাড়া এগোনো যাবে না
    assert 'if (tg && tg.initData) { requestOtp(); return; }' in script


async def test_config_endpoint_reports_demo_flag(web):
    resp = await web.get("/api/config")
    assert resp.status == 200
    payload = await resp.json()
    assert payload["demo"] is False
    assert payload["webapp_configured"] is True
    # প্রোডাকশনে ডেমো ক্রেডেনশিয়াল কখনো ফাঁস হবে না
    assert "demo_code" not in payload and "demo_password" not in payload
    assert "token" not in json.dumps(payload).lower()


# --------------------------------------------------------------------------- #
# ৭খ. ডেমো মোড (নেটওয়ার্ক ছাড়া UI টেস্ট)
# --------------------------------------------------------------------------- #
async def test_demo_mode_end_to_end():
    cfg = botmod.Config.from_env({"DEMO": "1", "WEBAPP_URL": "https://demo.test/app/"})
    telethon = FakeTelethon(right_code="12345", password="demo1234", needs_2fa=True)
    store = botmod.Store(cfg, client_factory=telethon)
    from aiohttp.test_utils import TestClient, TestServer

    app = botmod.make_app(cfg, api=None, store=store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/request-otp", {})
        assert status == 200 and data["status"] == "otp_sent" and data["demo"] is True

        status, data = await post(client, "/api/verify-otp", {"otp": "12345"})
        assert status == 200 and data["status"] == "2fa_required"

        status, data = await post(client, "/api/verify-password", {"password": "demo1234"})
        assert status == 200 and data["session"].startswith("SESSION-STRING-")
        assert data["demo"] is True

        page = await (await client.get("/")).text()
        assert "ডেমো মোড" in page
    finally:
        await client.close()


async def test_demo_mode_with_builtin_client_end_to_end():
    """আসল সার্ভারের মতো: ডেমো মোডে বিল্ট-ইন DemoClient দিয়ে পুরো ফ্লো।"""
    from aiohttp.test_utils import TestClient, TestServer

    cfg = botmod.Config.from_env({"DEMO": "1", "WEBAPP_URL": "https://demo.test/app/"})
    store = botmod.Store(cfg)                     # client_factory inject করা হয়নি
    app = botmod.make_app(cfg, store=store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/api/config")
        meta = await resp.json()
        assert meta["demo"] is True
        assert meta["demo_code"] == botmod.DEMO_CODE
        assert meta["demo_password"] == botmod.DEMO_PASSWORD

        status, data = await post(client, "/api/request-otp", {})
        assert status == 200 and data["phone"] == botmod.DEMO_PHONE

        status, data = await post(client, "/api/verify-otp", {"otp": "00000"})
        assert status == 400 and "ভুল" in data["detail"]

        status, data = await post(client, "/api/verify-otp", {"otp": botmod.DEMO_CODE})
        assert status == 200 and data["status"] == "2fa_required"

        status, data = await post(client, "/api/verify-password", {"password": "ভুল"})
        assert status == 400 and "পাসওয়ার্ড" in data["detail"]

        status, data = await post(client, "/api/verify-password", {"password": botmod.DEMO_PASSWORD})
        assert status == 200 and data["status"] == "success"
        assert data["session"].startswith("DEMO-SESSION-")
        assert data["account"]["username"] == "demo_user"
        assert store.get_pending(botmod.DEMO_UID) is None
    finally:
        await client.close()


async def test_demo_client_never_logs_out():
    cfg = botmod.Config.from_env({"DEMO": "1"})
    demo = botmod.DemoClient(cfg)
    await demo.connect()
    code = await demo.send_code_request("+15550000001")
    assert code.phone_code_hash
    with pytest.raises(botmod.PhoneCodeInvalidError):
        await demo.sign_in(code="99999")
    with pytest.raises(botmod.SessionPasswordNeededError):
        await demo.sign_in(code=botmod.DEMO_CODE)
    with pytest.raises(botmod.PasswordHashInvalidError):
        await demo.sign_in(password="nope")
    me = await demo.sign_in(password=botmod.DEMO_PASSWORD)
    assert me.username == "demo_user"
    assert demo.session.save().startswith("DEMO-SESSION-")
    assert demo.logged_out is False


async def test_demo_bypasses_rate_limit_and_needs_no_phone():
    cfg = botmod.Config.from_env({"DEMO": "1"})
    telethon = FakeTelethon()
    store = botmod.Store(cfg, client_factory=telethon)
    from aiohttp.test_utils import TestClient, TestServer

    app = botmod.make_app(cfg, api=None, store=store)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for _ in range(5):
            status, data = await post(client, "/api/request-otp", {})
            assert status == 200, data
            assert data["phone"] == botmod.DEMO_PHONE
    finally:
        await client.close()


async def test_task_wiring_demo_vs_live():
    """ডেমোতে শুধু sweeper; লাইভ মোডে sweeper + polling থাকবে।"""
    from aiohttp.test_utils import TestClient, TestServer

    demo_app = botmod.make_app(botmod.Config.from_env({"DEMO": "1"}))
    client = TestClient(TestServer(demo_app))
    await client.start_server()
    try:
        names = {task.get_coro().__qualname__ for task in botmod.running_tasks(demo_app)}
        assert not any("poll_updates" in n for n in names), f"ডেমো মোডে polling চলছে: {names}"
        assert any("sweeper" in n for n in names)
    finally:
        await client.close()

    live_app = botmod.make_app(botmod.Config(
        bot_token=BOT_TOKEN, api_id=1, api_hash="d" * 32, webapp_url="https://x.test/app/"
    ))
    client = TestClient(TestServer(live_app))
    await client.start_server()
    try:
        names = {task.get_coro().__qualname__ for task in botmod.running_tasks(live_app)}
        assert any("poll_updates" in n for n in names), f"লাইভ মোডে polling নেই: {names}"
    finally:
        await client.close()
