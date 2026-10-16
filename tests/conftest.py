from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional
from urllib.parse import urlencode

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as botmod  # noqa: E402

BOT_TOKEN = "1112223333:TEST-TOKEN-not-a-real-credential"
USER_A = {"id": 1000001, "first_name": "A", "username": "alpha", "language_code": "bn"}
USER_B = {"id": 2000002, "first_name": "B", "username": "beta", "language_code": "bn"}


def build_init_data(
    bot_token: str = BOT_TOKEN,
    user: Optional[dict] = None,
    *,
    auth_date: Optional[int] = None,
    include_hash: bool = True,
    extra: Optional[dict] = None,
    break_hash: bool = False,
) -> str:
    """আসল Telegram-এর মতো করে initData বানায় (hash সহ)।"""
    user = user or USER_A
    pairs = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAAtest-query",
        "user": json.dumps(user, separators=(",", ":")),
    }
    pairs.update(extra or {})
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if break_hash:
        digest = ("0" if digest[0] != "0" else "1") + digest[1:]
    if include_hash:
        pairs["hash"] = digest
    return urlencode(pairs)


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeAPI:
    """Telegram Bot API-র নকল — শুধু কলগুলো রেকর্ড করে।"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail_send_message = False
        self.incoming: list[dict] = []
        self.offsets: list[int] = []
        self.last_update: list[dict] = []
        self.fail_methods: dict[str, str] = {}
        self.network_error = False
        self.get_me_result = {"id": 424242, "username": "my_test_bot", "first_name": "Test Bot"}

    async def call(self, method: str, **params: Any) -> Any:
        if method == "getUpdates":
            self.offsets.append(params.get("offset"))
            if params.get("offset") == -1:      # detect-owner একক কল
                self.calls.append((method, params))
                return self.last_update
            if self.incoming:
                batch, self.incoming = self.incoming[:], []
                return batch
            # long-poll এর মতো আচরণ — টাস্কটি cancel হওয়া পর্যন্ত অপেক্ষা করবে
            import asyncio

            await asyncio.sleep(30)
            return []
        self.calls.append((method, params))
        if method in self.fail_methods:
            raise botmod.BotAPIError(method, self.fail_methods[method], 400)
        if self.network_error:
            raise OSError("network is unreachable")
        if method == "sendMessage" and self.fail_send_message:
            raise botmod.BotAPIError(method, "chat not found", 400)
        if method == "getMe":
            return self.get_me_result
        if method in {"setChatMenuButton", "deleteWebhook"}:
            return True
        return {"message_id": len(self.calls)}

    def methods(self) -> list[str]:
        return [name for name, _ in self.calls]

    def params_of(self, method: str) -> list[dict]:
        return [p for name, p in self.calls if name == method]

    def session_messages(self) -> list[dict]:
        """session string সহ যেসব মেসেজ পাঠানো হয়েছে।"""
        out = []
        for _, params in self.calls:
            markup = params.get("reply_markup") or {}
            rows = markup.get("inline_keyboard") or []
            if any(btn.get("callback_data") == "del:session" for row in rows for btn in row):
                out.append(params)
        return out


class FakeClient:
    """Telethon TelegramClient-এর নকল (কোনো নেটওয়ার্ক কল নেই)।"""

    def __init__(self, factory: "FakeTelethon", name: str) -> None:
        self.factory = factory
        self.name = name
        self.connected = False
        self.disconnected = False
        self.logged_out = False
        self.authorized = False
        self.session = SimpleNamespace(save=lambda: f"SESSION-STRING-{name}")
        self.code_requests: list[str] = []
        self.sign_ins: list[dict] = []
        self.revoked_hashes: list[Any] = []

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def log_out(self) -> None:
        self.logged_out = True

    async def send_code_request(self, phone: str) -> Any:
        self.code_requests.append(phone)
        if self.factory.send_code_error is not None:
            raise self.factory.send_code_error
        return SimpleNamespace(phone_code_hash=f"HASH-{self.name}")

    async def sign_in(self, phone: Any = None, code: Any = None, phone_code_hash: Any = None, password: Any = None) -> Any:
        self.sign_ins.append({"phone": phone, "code": code, "hash": phone_code_hash, "password": password})
        if password is not None:
            if self.factory.password is None or password != self.factory.password:
                raise botmod.PasswordHashInvalidError(request=None)
            self.authorized = True
            return self.factory.me
        if code != self.factory.right_code:
            raise botmod.PhoneCodeInvalidError(request=None)
        if self.factory.needs_2fa:
            self.authorized = True  # Telethon-এ এই মুহূর্তে পাসওয়ার্ড ধাপে যায়
            raise botmod.SessionPasswordNeededError(request=None)
        self.authorized = True
        return self.factory.me

    async def get_me(self) -> Any:
        if not self.authorized:
            raise RuntimeError("not authorized")
        return self.factory.me

    async def __call__(self, request: Any) -> Any:
        """Telethon-এর client(request) স্টাইল — GetAuthorizations / ResetAuthorization।"""
        name = type(request).__name__
        if name == "GetAuthorizationsRequest":
            return SimpleNamespace(authorizations=list(self.factory.authorizations or []))
        if name == "ResetAuthorizationRequest":
            self.revoked_hashes.append(request.hash)
            return True
        raise RuntimeError(f"FakeClient এই request চেনে না: {name}")


class FakeTelethon:
    """client_factory — প্রতি লগইনে নতুন FakeClient দেয়।"""

    def __init__(
        self,
        *,
        right_code: str = "12345",
        password: Optional[str] = None,
        needs_2fa: bool = False,
        send_code_error: Optional[BaseException] = None,
        me: Optional[Any] = None,
    ) -> None:
        self.right_code = right_code
        self.password = password
        self.needs_2fa = needs_2fa
        self.send_code_error = send_code_error
        self.me = me or SimpleNamespace(
            id=USER_A["id"], username="alpha", first_name="A", phone="+15550000001"
        )
        self.created: list[FakeClient] = []
        self.authorizations = [
            SimpleNamespace(hash=1, current=True, device_model="This mini app", platform="Web"),
            SimpleNamespace(hash=2, current=False, device_model="Phone", platform="Android"),
            SimpleNamespace(hash=3, current=False, device_model="Old laptop", platform="Windows"),
        ]

    def __call__(self) -> FakeClient:
        client = FakeClient(self, f"c{len(self.created) + 1}")
        self.created.append(client)
        return client

    @property
    def last(self) -> FakeClient:
        assert self.created, "কোনো client তৈরি হয়নি"
        return self.created[-1]


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def cfg() -> botmod.Config:
    return botmod.Config(
        bot_token=BOT_TOKEN,
        api_id=123456,
        api_hash="a" * 32,
        port=10000,
        webapp_url="https://example.test/session/",
        pending_ttl=900,
        phone_ttl=900,
        initdata_max_age=3600,
        otp_window=900,
        otp_limit=3,
        otp_cooldown=0,
        demo=False,
    )


@pytest.fixture
def fake_api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture
def telethon() -> FakeTelethon:
    return FakeTelethon()


@pytest.fixture
def store(cfg: botmod.Config, telethon: FakeTelethon) -> botmod.Store:
    return botmod.Store(cfg, client_factory=telethon)


@pytest.fixture
def runtime(tmp_path, fake_api) -> botmod.Runtime:
    return botmod.Runtime(
        config_path=tmp_path / "config.json", pin="424242", pin_source="test",
        api_factory=lambda token: fake_api,
    )


@pytest_asyncio.fixture
async def web(cfg: botmod.Config, fake_api: FakeAPI, store: botmod.Store, runtime):
    app = botmod.make_app(cfg, api=fake_api, store=store, runtime=runtime)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client
    finally:
        await client.close()


async def post(client: TestClient, path: str, payload: dict, headers: Optional[dict] = None) -> tuple[int, dict]:
    resp = await client.post(path, json=payload, headers=headers or {})
    try:
        data = await resp.json()
    except Exception:  # noqa: BLE001
        data = {"raw": await resp.text()}
    return resp.status, data
