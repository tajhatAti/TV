"""সেটআপ ফ্লো (ওয়েব থেকে কনফিগ, env variable ছাড়াই) — টেস্ট স্যুট।"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import main as botmod
from conftest import FakeTelethon, build_init_data, post

TOKEN = "123456789:AA" + "x" * 32
HASH = "a" * 32
URL = "https://my-app.example.com"


def form(**over):
    payload = {
        "bot_token": TOKEN,
        "api_id": "1234567",
        "api_hash": HASH,
        "webapp_url": URL,
        "owner_id": "1000001",
        "auto_revoke_others": 0,
    }
    payload.update(over)
    return payload


def unconfigured_app(fake_api, tmp_path, **cfg_over):
    """env ছাড়া, কনফিগ ফাইল খালি — নতুন ডিপ্লয়ের মতো।"""
    cfg = botmod.Config(**cfg_over)
    runtime = botmod.Runtime(
        config_path=tmp_path / "data" / "config.json",
        api_factory=lambda token: fake_api,
    )
    store = botmod.Store(cfg, client_factory=FakeTelethon())
    app = botmod.make_app(cfg, api=fake_api, store=store, runtime=runtime)
    return app, runtime


# --------------------------------------------------------------------------- #
# ১. env ছাড়া সার্ভার: ক্র্যাশ নয়, সেটআপ ফর্ম
# --------------------------------------------------------------------------- #
async def test_unconfigured_server_serves_setup_page_not_crash(fake_api, tmp_path):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get("/")
        body = await resp.text()
        assert resp.status == 200
        assert "সেটআপ" in body and "/api/setup/save" in body, "লিংক খুললে সেটআপ ফর্ম আসবে"

        health = await client.get("/health")
        assert health.status == 200, "env ছাড়াও হেলথ ঠিক থাকবে"

        status = await (await client.get("/api/setup/status")).json()
        assert status["configured"] is False
        assert status["open_access"] is True, "কোনো PIN নেই"
        # প্রক্সি হোস্ট (127.0.0.1) থেকে ভুয়া লিংক বানানো হয় না — খালি রাখা হয়
        assert status["detected_url"] == ""
        resp = await client.get(
            "/api/setup/status", headers={"Origin": "https://myapp.example.com"}
        )
        assert (await resp.json())["detected_url"] == "https://myapp.example.com"

        meta = await (await client.get("/api/config")).json()
        assert meta["configured"] is False
        assert meta["demo"] is False
    finally:
        await client.close()


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/api/request-otp", {}),
        ("/api/verify-otp", {"otp": "12345"}),
        ("/api/verify-password", {"password": "x"}),
        ("/api/reset", {}),
        ("/api/revoke-others", {}),
    ],
)
async def test_bot_apis_are_blocked_until_configured(fake_api, tmp_path, path, payload):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, path, payload)
        assert status == 503, data
        assert "setup" in data["detail"] or "সেটআপ" in data["detail"]
    finally:
        await client.close()


def test_main_never_raises_without_env(monkeypatch, tmp_path, capsys):
    """main() env ছাড়াও শুরু হবে (আগের মতো SystemExit নয়)।"""
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = botmod.Config.from_sources({}, {})
    assert cfg.is_configured is False
    warnings: list = []
    cfg = botmod.Config.from_sources({}, {"PORT": "abc"}, strict=False, warnings=warnings)
    assert cfg.port == 10000 and warnings, "ভুল মান বাদ দিয়ে ডিফল্টে ফিরবে, ক্র্যাশ নয়"


def test_config_roundtrip_through_file(tmp_path):
    """সেভ করা ফাইল থেকে আবার পড়লে একই কনফিগ পাওয়া উচিত।"""
    path = tmp_path / "config.json"
    cfg = botmod.Config.from_sources({}, form(), strict=True)
    data = cfg.to_file_data()
    botmod.save_config_file(data, path)

    loaded = botmod.load_config_file(path)
    again = botmod.Config.from_sources({}, loaded, strict=True)
    assert again.bot_token == cfg.bot_token
    assert again.api_id == cfg.api_id
    assert again.webapp_url == URL
    assert again.owner_id == 1000001
    assert "SETUP_PIN" not in loaded
    if os.name == "posix":  # ফাইল শুধু মালিক পড়তে পারে
        assert oct(path.stat().st_mode)[-3:] == "600"

    # env সবসময় ফাইলের উপরে
    overridden = botmod.Config.from_sources({"OWNER_ID": "999"}, loaded, strict=True)
    assert overridden.owner_id == 999


def test_load_config_file_handles_garbage(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not-json", encoding="utf-8")
    assert botmod.load_config_file(path) == {}
    assert botmod.load_config_file(tmp_path / "missing.json") == {}


def test_startup_config_reads_saved_file_without_env(tmp_path):
    """বুটের সময় env ছাড়াও সেভ করা কনফিগ পড়া যায় (আসল বাগ ছিল)।"""
    path = tmp_path / "config.json"
    botmod.save_config_file(
        {
            "BOT_TOKEN": TOKEN, "API_ID": 1234567, "API_HASH": HASH,
            "WEBAPP_URL": URL, "OWNER_ID": 1000001,
            "REVOKE_WINDOW": 900, "AUTO_REVOKE_OTHERS": 0,
        },
        path,
    )
    cfg, runtime, warnings = botmod.startup_config({}, path)
    assert cfg.is_configured is True, "সেভ করা টোকেন বুটেই কাজে লাগবে"
    assert cfg.bot_token == TOKEN and cfg.webapp_url == URL and cfg.owner_id == 1000001
    assert not warnings

    # env থাকলে env-ই জেতে
    cfg2, _, _ = botmod.startup_config({"OWNER_ID": "555"}, path)
    assert cfg2.owner_id == 555 and cfg2.bot_token == TOKEN

    # ফাইল না থাকলে: ক্র্যাশ নয়, খালি কনফিগ + নতুন PIN
    empty_cfg, empty_rt, empty_warn = botmod.startup_config({}, tmp_path / "nope" / "config.json")
    assert empty_cfg.is_configured is False and empty_rt.config_path and not empty_warn

    # ফাইলে ভুল মান থাকলেও সার্ভার চলবে (warning সহ)
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps({"BOT_TOKEN": "খারাপ", "API_ID": "abc"}), encoding="utf-8")
    bad_cfg, _, bad_warn = botmod.startup_config({}, bad_path)
    assert bad_cfg.is_configured is False and len(bad_warn) >= 2


# --------------------------------------------------------------------------- #
# ২. সেটআপ সেভ
# --------------------------------------------------------------------------- #
async def test_setup_save_needs_no_pin_at_all(fake_api, tmp_path):
    """কোনো PIN/পাসওয়ার্ড নেই — ফর্ম খুলে সেভ চাপলেই বট চালু।"""
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/save", form())   # কোনো pin ফিল্ড নেই
        assert status == 200 and data["status"] == "saved", data
        assert runtime.config_path.exists()
        saved = json.loads(runtime.config_path.read_text(encoding="utf-8"))
        assert "SETUP_PIN" not in saved and not any("PIN" in k for k in saved)
        assert botmod.app_config(app).is_configured is True
    finally:
        await client.close()


async def test_setup_save_never_blocked_by_origin_header(fake_api, tmp_path):
    """Origin মিলুক বা না মিলুক — সেভ কখনো আটকাবে না (আগে এটাই মালিককে আটকাত)।"""
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        for headers in (
            {"Origin": "https://evil.example.com"},                     # অন্য সাইট
            {"Referer": "https://evil.example.com/x"},
            {},                                                          # curl-এর মতো
            {"Origin": "https://codenestauth.onrender.com"},             # প্রক্সি করা হোস্ট
        ):
            status, data = await post(client, "/api/setup/save", form(), headers=headers)
            assert status == 200 and data["status"] == "saved", (headers, data)
        assert runtime.config_path.exists()
    finally:
        await client.close()


async def test_setup_save_works_when_proxy_rewrites_host(fake_api, tmp_path):
    """CodeNest-এর মতো প্রক্সি: Host=127.0.0.1, ব্রাউজার Origin=পাবলিক https — সেভ হবে।"""
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {
            "Host": "127.0.0.1:11002",                    # প্রক্সি যা পাঠায়
            "Origin": "https://codenestauth.onrender.com",  # ব্রাউজার যা পাঠায়
            "Referer": "https://codenestauth.onrender.com/live/u3-verify-ec07a8/",
        }
        prefixed = "https://codenestauth.onrender.com/live/u3-verify-ec07a8/"
        status, data = await post(client, "/api/setup/save", form(webapp_url=prefixed), headers=headers)
        assert status == 200 and data["status"] == "saved", data
        assert botmod.app_config(app).webapp_url == prefixed.rstrip("/")
        assert data["polling"] is True and data["menu_button"] is True
        assert fake_api.params_of("setChatMenuButton")[-1]["menu_button"]["web_app"]["url"] == prefixed.rstrip("/")

        # OWNER_ID বের করার বাটনও এখানে কাজ করবে
        fake_api.last_update = [
            {"update_id": 1, "message": {"chat": {"id": 1000001}, "from": {"id": 1000001, "username": "me"}, "text": "/start"}}
        ]
        status, data = await post(client, "/api/setup/detect-owner", {}, headers=headers)
        assert status == 200 and data["found"] is True and data["owner_id"] == 1000001

        status, data = await post(client, "/api/setup/menu-button", {}, headers=headers)
        assert status == 200 and data["menu_button"] is True
    finally:
        await client.close()


def test_detected_url_falls_back_to_browser_origin():
    """প্রক্সি আসল হোস্ট না দিলে ব্রাউজারের Origin থেকে পাবলিক লিংক বের করা হয়।"""

    class FakeRequest:
        def __init__(self, headers):
            self.headers = headers
            self.scheme = "http"

    headers = {
        "Host": "127.0.0.1:11002",
        "Origin": "https://codenestauth.onrender.com",
        "X-Forwarded-Prefix": "/live/u3-verify-ec07a8",
    }
    assert (
        botmod.request_origin(FakeRequest(headers))
        == "https://codenestauth.onrender.com/live/u3-verify-ec07a8"
    )
    # প্রক্সি হেডার থাকলে সেটাই আগে (আগের আচরণ অপরিবর্তিত)
    assert (
        botmod.request_origin(
            FakeRequest({"Host": "127.0.0.1:11002", "X-Forwarded-Host": "public.example", "X-Forwarded-Proto": "https"})
        )
        == "https://public.example"
    )
    # কিছুই না থাকলে খালি (ভুল লিংক সেভ হবে না)
    assert botmod.request_origin(FakeRequest({"Host": "127.0.0.1:11002"})) == ""


@pytest.mark.parametrize(
    "host,private",
    [
        ("127.0.0.1:11002", True),
        ("localhost", True),
        ("[::1]:8000", True),
        ("10.0.0.5", True),
        ("192.168.1.9", True),
        ("172.17.0.2", True),
        ("codenestauth.onrender.com", False),
        ("myapp.example.com", False),
    ],
)
def test_is_private_host(host, private):
    assert botmod.is_private_host(host) is private


async def test_setup_save_is_rate_limited(fake_api, tmp_path):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        codes = []
        for _ in range(35):
            status, _ = await post(client, "/api/setup/save", form())
            codes.append(status)
        assert 429 in codes, f"অনেকবার চাপলে থামা উচিত: {codes}"
    finally:
        await client.close()


async def test_setup_save_rejects_bad_token_format(fake_api, tmp_path):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/save", form(bot_token="ভুল"))
        assert status == 400 and "BOT_TOKEN" in data["detail"]
        status, data = await post(client, "/api/setup/save", form(api_hash="zz"))
        assert status == 400 and "API_HASH" in data["detail"]
        status, data = await post(client, "/api/setup/save", form(api_id=""))
        assert status == 400
    finally:
        await client.close()


async def test_setup_save_reports_invalid_token_from_telegram(fake_api, tmp_path):
    fake_api.fail_methods["getMe"] = "Unauthorized"
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/save", form())
        assert status == 400 and "টোকেন" in data["detail"]
        assert not runtime.config_path.exists(), "যাচাই ফেল হলে সেভ হবে না"
        assert botmod.app_config(app).is_configured is False
    finally:
        await client.close()


async def test_setup_save_reports_network_failure(fake_api, tmp_path):
    fake_api.network_error = True
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/save", form())
        assert status == 502 and "কানেক্ট" in data["detail"]
    finally:
        await client.close()


async def test_setup_save_starts_bot_without_restart(fake_api, tmp_path):
    """সেভ করার সাথে সাথেই বট চালু + মেনু বাটন + মিনি অ্যাপ চালু (রিস্টার্ট লাগে না)।"""
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/save", form())
        assert status == 200 and data["status"] == "saved", data
        assert data["bot"]["username"] == "my_test_bot"
        assert data["menu_button"] is True
        assert data["polling"] is True
        assert data["warnings"] == []

        # কনফিগ হট-সোয়াপ হয়েছে
        assert botmod.app_config(app).bot_token == TOKEN
        assert botmod.app_config(app).owner_id == 1000001
        assert app[botmod.STORE_KEY].cfg.owner_id == 1000001

        # ফাইলে সেভ হয়েছে (PIN সহ)
        saved = json.loads(runtime.config_path.read_text(encoding="utf-8"))
        assert saved["BOT_TOKEN"] == TOKEN

        # মেনু বাটন Telegram-এ গেছে
        menu = fake_api.params_of("setChatMenuButton")
        assert menu and menu[-1]["menu_button"]["web_app"]["url"] == URL
        assert any("poll_updates" in t.get_coro().__qualname__ for t in botmod.running_tasks(app))

        # এখন লিংক খুললে মিনি অ্যাপ আসে (আর সেটআপ নয়), আর sেশন ফ্লো কাজ করে
        body = await (await client.get("/")).text()
        assert "Session Generator" in body and "সেটআপ" not in body.split("<h2>")[1][:40]

        await app[botmod.STORE_KEY].set_phone("1000001", "+15550000001")
        init_data = build_init_data(bot_token=TOKEN)   # সেটআপের পরে নতুন টোকেনই সঠিক
        status, otp = await post(client, "/api/request-otp", {"initData": init_data})
        assert status == 200 and otp["status"] == "otp_sent", otp
    finally:
        await client.close()


async def test_setup_save_warns_without_owner_id(fake_api, tmp_path):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/save", form(owner_id=""))
        assert status == 200
        assert any("OWNER_ID" in w for w in data["warnings"]), data["warnings"]
        assert botmod.app_config(app).owner_id == 0
    finally:
        await client.close()


async def test_setup_save_fills_webapp_url_from_request(fake_api, tmp_path):
    """https proxy হেডার থাকলে (Render/CodeNest) লিংক নিজে থেকেই ধরা পড়ে।"""
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "my-app.example.com"}
        status, data = await post(client, "/api/setup/save", form(webapp_url=""), headers=headers)
        assert status == 200, data
        assert botmod.app_config(app).webapp_url == "https://my-app.example.com"
        assert data["menu_button"] is True
        assert data["warnings"] == []
    finally:
        await client.close()


async def test_setup_save_without_https_url_warns_and_still_saves(fake_api, tmp_path):
    """http লিংক (লোকাল টেস্ট) অটো-নেওয়া হয় না, কিন্তু সেভ হয় + সতর্কবার্তা।"""
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/save", form(webapp_url=""))
        assert status == 200, data
        assert botmod.app_config(app).webapp_url == ""
        assert any("https" in w for w in data["warnings"]), data["warnings"]
        assert data["menu_button"] is False
        assert botmod.app_config(app).is_configured is True
        assert json.loads(runtime.config_path.read_text(encoding="utf-8"))["BOT_TOKEN"] == TOKEN
    finally:
        await client.close()


async def test_setup_save_keeps_existing_values_when_fields_empty(fake_api, tmp_path):
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await post(client, "/api/setup/save", form())
        # দ্বিতীয়বার শুধু AUTO_REVOKE বদলানো — টোকেন/হ্যাশ খালি
        status, data = await post(
            client, "/api/setup/save", form(bot_token="", api_hash="", api_id="", auto_revoke_others=1)
        )
        assert status == 200, data
        assert botmod.app_config(app).bot_token == TOKEN
        assert botmod.app_config(app).auto_revoke_others is True
        saved = json.loads(runtime.config_path.read_text(encoding="utf-8"))
        assert saved["AUTO_REVOKE_OTHERS"] == 1
    finally:
        await client.close()


async def test_saved_config_survives_restart(fake_api, tmp_path):
    """রিডিপ্লয়/রিস্টার্টের পর সেভ করা সেটআপ থেকেই বট চালু হবে।"""
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await post(client, "/api/setup/save", form())
    finally:
        await client.close()

    # "নতুন প্রসেস" — একই ফাইল থেকে কনফিগ পড়া
    data = botmod.load_config_file(runtime.config_path)
    cfg = botmod.Config.from_sources({}, data, strict=False)
    assert cfg.is_configured is True
    assert cfg.bot_token == TOKEN and cfg.webapp_url == URL and cfg.owner_id == 1000001

    store = botmod.Store(cfg, client_factory=FakeTelethon())
    app2 = botmod.make_app(cfg, api=fake_api, store=store, runtime=runtime)
    client2 = TestClient(TestServer(app2))
    await client2.start_server()
    try:
        status = await (await client2.get("/api/setup/status")).json()
        assert status["configured"] is True and status["polling"] is True
    finally:
        await client2.close()


async def test_token_can_be_changed_any_time_without_any_password(fake_api, tmp_path):
    """পরে টোকেন বদলাতে চাইলে শুধু নতুন টোকেন দিলেই হবে (কোনো PIN নেই)।"""
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await post(client, "/api/setup/save", form()))[0] == 200
        other = "999999999:BB" + "y" * 32
        status, data = await post(client, "/api/setup/save", form(bot_token=other))
        assert status == 200, data
        assert botmod.app_config(app).bot_token == other
        saved = json.loads(runtime.config_path.read_text(encoding="utf-8"))
        assert saved["BOT_TOKEN"] == other and "SETUP_PIN" not in saved
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# ৩. OWNER_ID বের করা ও মেনু বাটন
# --------------------------------------------------------------------------- #
async def test_detect_owner_reads_last_update(fake_api, tmp_path):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    fake_api.last_update = [
        {
            "update_id": 5,
            "message": {
                "chat": {"id": 1000001},
                "from": {"id": 1000001, "username": "me", "first_name": "Me"},
                "text": "/start",
            },
        }
    ]
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/detect-owner", {"bot_token": TOKEN})
        assert status == 200 and data["found"] is True
        assert data["owner_id"] == 1000001 and data["username"] == "me"

        fake_api.last_update = []
        status, data = await post(client, "/api/setup/detect-owner", {"bot_token": TOKEN})
        assert status == 200 and data["found"] is False and "hint" in data
    finally:
        await client.close()


async def test_detect_owner_needs_token_only(fake_api, tmp_path):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/detect-owner", {"bot_token": "খারাপ"})
        assert status == 400 and "BOT_TOKEN" in data["detail"]
    finally:
        await client.close()


async def test_detect_owner_restarts_polling(fake_api, tmp_path):
    app, runtime = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        await post(client, "/api/setup/save", form())
        assert any("poll_updates" in t.get_coro().__qualname__ for t in botmod.running_tasks(app))
        fake_api.last_update = []
        await post(client, "/api/setup/detect-owner", {})
        tasks = botmod.running_tasks(app)
        assert any("poll_updates" in t.get_coro().__qualname__ for t in tasks), "পোলিং আবার চালু হবে"
    finally:
        await client.close()


async def test_menu_button_endpoint(fake_api, tmp_path):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        status, data = await post(client, "/api/setup/menu-button", {})
        assert status == 409, "সেটআপের আগে মেনু বাটন বসানো যাবে না"

        await post(client, "/api/setup/save", form())
        status, data = await post(client, "/api/setup/menu-button", {})
        assert status == 200 and data["menu_button"] is True
    finally:
        await client.close()


# --------------------------------------------------------------------------- #
# ৪. সেটআপ পেজের ফ্রন্টএন্ড
# --------------------------------------------------------------------------- #
def test_setup_page_has_all_fields_and_hits_only_existing_routes(tmp_path):
    page = botmod.SETUP_PAGE
    for field in ("bot_token", "api_id", "api_hash", "webapp_url", "owner_id", "auto_revoke"):
        assert f'id="{field}"' in page, f"{field} ঘর নেই"
    paths = set(__import__("re").findall(r'"(/api/setup/[a-z\-]+)"', page))
    assert paths == {
        "/api/setup/save",
        "/api/setup/status",
        "/api/setup/detect-owner",
        "/api/setup/menu-button",
    }, paths


def _script(page: str) -> str:
    import re

    return re.search(r"<script>(.*?)</script>", page, re.S).group(1)


def test_setup_page_uses_relative_api_paths():
    """হোস্টিং অ্যাপকে পাথ প্রিফিক্সের নিচে চালালেও (যেমন /live/xxxx/) কল ঠিক জায়গায় যাবে।"""
    script = _script(botmod.SETUP_PAGE)
    assert "const BASE" in script and "apiUrl" in script
    assert 'fetch("/' not in script, "রুট থেকে fetch করলে প্রিফিক্স হোস্টিংয়ে Not Found আসে"
    for call in ("apiUrl(path)", 'apiUrl("/api/setup/status")'):
        assert call in script
    assert "publicUrl()" in script, "লিংক ঘর নিজে থেকেই ভরে দেওয়া উচিত"


def test_mini_app_page_uses_relative_api_paths():
    script = _script(botmod.PAGE)
    assert "const BASE" in script and "apiUrl" in script
    assert 'fetch("/' not in script, "মিনি অ্যাপও প্রিফিক্স-সেফ হতে হবে"
    assert 'fetch(apiUrl("/api/config"))' in script
    assert "fetch(apiUrl(path)" in script


def test_base_detection_logic_in_node(tmp_path):
    """BASE হিসাবটা আসল প্রিফিক্স উদাহরণে যাচাই করি (node দিয়ে)।"""
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        import pytest as _pytest

        _pytest.skip("node ইনস্টল করা নেই")
    script = _script(botmod.SETUP_PAGE)
    base_fn = script[script.index("const BASE"):script.index("function publicUrl")]
    harness = f'''
const cases = [
  ["/", "/"],
  ["/live/u3-verify-ec07a8/", "/live/u3-verify-ec07a8/"],
  ["/live/u3-verify-ec07a8", "/live/u3-verify-ec07a8/"],
  ["/live/abc/index.html", "/live/abc/"],
];
const fails = [];
for (const [pathname, want] of cases) {{
  const window = {{ location: {{ pathname, origin: "https://host.example" }} }};
  {base_fn}
  const got = apiUrl("api/setup/save");
  const wantUrl = want + "api/setup/save";
  if (got !== wantUrl) fails.push(pathname + " → " + got + " (চাই " + wantUrl + ")");
}}
if (fails.length) {{ console.log(fails.join("\\n")); process.exit(1); }}
console.log("ok");
'''
    path = tmp_path / "base.js"
    path.write_text(harness, encoding="utf-8")
    result = subprocess.run([node, str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


async def test_request_origin_honours_forwarded_prefix(fake_api, tmp_path):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        resp = await client.get(
            "/api/setup/status",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "codenestauth.onrender.com",
                "X-Forwarded-Prefix": "/live/u3-verify-ec07a8",
            },
        )
        detected = (await resp.json())["detected_url"]
        assert detected == "https://codenestauth.onrender.com/live/u3-verify-ec07a8", detected
    finally:
        await client.close()


async def test_setup_save_keeps_prefixed_public_url(fake_api, tmp_path):
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        headers = {
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "codenestauth.onrender.com",
            "X-Forwarded-Prefix": "/live/u3-verify-ec07a8",
        }
        status, data = await post(
            client,
            "/api/setup/save",
            form(webapp_url="https://codenestauth.onrender.com/live/u3-verify-ec07a8/"),
            headers=headers,
        )
        assert status == 200, data
        assert (
            botmod.app_config(app).webapp_url
            == "https://codenestauth.onrender.com/live/u3-verify-ec07a8"
        )
        menu = fake_api.params_of("setChatMenuButton")[-1]["menu_button"]["web_app"]["url"]
        assert menu.endswith("/live/u3-verify-ec07a8"), menu

        # প্ল্যাটফর্মের Origin হেডার থাকলেও সেভ আটকাবে না
        status, data = await post(
            client,
            "/api/setup/save",
            form(),
            headers={**headers, "Origin": "https://codenestauth.onrender.com"},
        )
        assert status == 200, data

        # অন্য Origin হলেও এখন আর আটকায় না (শুধু লগ হয়)
        status, data = await post(
            client,
            "/api/setup/save",
            form(),
            headers={**headers, "Origin": "https://evil.example.com"},
        )
        assert status == 200 and data["status"] == "saved"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/", "/"),
        ("/health", "/health"),
        ("/api/setup/save", "/api/setup/save"),
        ("/live/u3-verify-ec07a8/", "/"),
        ("/live/u3-verify-ec07a8", None),
        ("/live/u3-verify-ec07a8/health", "/health"),
        ("/live/u3-verify-ec07a8/setup", "/setup"),
        ("/live/u3-verify-ec07a8/api/setup/save", "/api/setup/save"),
        ("/live/a/b/c/api/request-otp", "/api/request-otp"),
        ("/favicon.ico", None),
        ("/kemono", None),
    ],
)
def test_rewrite_prefixed_path(path, expected):
    assert botmod.rewrite_prefixed_path(path) == expected


async def test_prefix_tolerance_both_proxy_styles(fake_api, tmp_path):
    """প্ল্যাটফর্ম প্রিফিক্স না কাটলেও (বা কাটলেও) সব রুট ঠিকঠাক কাজ করবে।"""
    app, _ = unconfigured_app(fake_api, tmp_path)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        base = "/live/u3-verify-ec07a8"
        # ১) প্রিফিক্সসহ (proxy না কাটলে)
        resp = await client.get(base + "/")
        assert resp.status == 200 and "সেটআপ" in await resp.text()
        resp = await client.get(base + "/health")
        assert resp.status == 200 and (await resp.json())["status"] == "ok"
        resp = await client.get(base + "/api/setup/status")
        assert resp.status == 200 and (await resp.json())["open_access"] is True
        resp = await client.get(base + "/setup")
        assert resp.status == 200 and "সেটআপ" in await resp.text()
        status, data = await post(client, base + "/api/setup/save", form())
        assert status == 200 and data["status"] == "saved", data

        # ২) প্রিফিক্স ছাড়া (প্ল্যাটফর্ম নিজেই কেটে দিলে)
        assert (await client.get("/")).status == 200
        assert (await client.get("/health")).status == 200
        assert (await client.get("/api/setup/status")).status == 200

        # ৩) অজানা পাথ আসলেই 404
        assert (await client.get("/favicon.ico")).status == 404
        assert (await client.get("/kemono")).status == 404
    finally:
        await client.close()


def test_setup_page_has_no_pin_field():
    page = botmod.SETUP_PAGE
    assert 'id="pin"' not in page and "SETUP PIN" not in page, "PIN সিস্টেম থাকা যাবে না"


def test_setup_page_ids_and_handlers_exist():
    import re

    page = botmod.SETUP_PAGE
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    used = set(re.findall(r'\$\("([a-zA-Z0-9_\-]+)"\)', script))
    defined = set(re.findall(r'id="([a-zA-Z0-9_\-]+)"', page))
    assert not (used - defined), f"HTML-এ নেই: {sorted(used - defined)}"
    handlers = set(re.findall(r'onclick="([a-zA-Z_]+)\(', page))
    funcs = set(re.findall(r'function ([a-zA-Z_]+)\(', script))
    assert handlers <= funcs, f"JS-এ নেই: {sorted(handlers - funcs)}"


def test_main_file_is_the_entrypoint():
    root = Path(__file__).resolve().parents[1]
    assert (root / "main.py").exists(), "এন্ট্রি ফাইল main.py হতে হবে"
    assert not (root / "bot.py").exists(), "পুরনো bot.py আর দরকার নেই"
    source = (root / "main.py").read_text(encoding="utf-8")
    assert "SystemExit" not in source, "env না পেলে আর ক্র্যাশ করা যাবে না"
