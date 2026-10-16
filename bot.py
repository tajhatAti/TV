"""Session String Generator — শুধু নিজের Telegram অ্যাকাউন্টের জন্য।

কীভাবে কাজ করে
---------------
1. ইউজার বটের সাথে প্রাইভেট চ্যাটে `/start` দেয়।
2. বট একটি reply-keyboard বাটন পাঠায়: "📱 আমার নম্বর শেয়ার করুন" (request_contact)।
3. ইউজার **নিজের** কন্টাক্ট শেয়ার করে (অন্য কারো কন্টাক্ট দিলে বট তা গ্রহণ করে না)।
4. বট মিনি অ্যাপের (web_app) লিংক পাঠায়। মিনি অ্যাপ `Telegram.WebApp.initData` দিয়ে
   নিজেকে authenticate করে — অর্থাৎ কোন ইউজার কথা বলছে সেটা server নিজে যাচাই করে,
   client যা পাঠায় তা বিশ্বাস করে না।
5. মিনি অ্যাপে ইউজার নিজের OTP (লগইন কোড) দেয়, 2FA থাকলে পাসওয়ার্ড দেয়।
6. লগইন সফল হলে session string তৈরি হয় এবং **শুধু সেই ইউজারকেই** দেখানো হয়
   (নিজের মিনি অ্যাপ স্ক্রিনে + নিজের বট-চ্যাটে, যেখানে এক ট্যাপে মুছে ফেলার বাটন আছে)।
   কোনো তৃতীয় পক্ষ, চ্যানেল বা ডেটাবেজে সেশন পাঠানো হয় না।

নিরাপত্তার নীতি
---------------
* সব ক্রেডেনশিয়াল environment variable থেকে আসে — কোডে কিছু হার্ডকোড করা নেই।
* `initData` HMAC-SHA256 দিয়ে যাচাই করা হয় + `auth_date` freshness চেক করা হয়।
* pending login প্রতিটি Telegram user id-র সাথে বাঁধা; body-র `phone` কখনো বিশ্বাস করা হয় না।
* OTP রিকোয়েস্টে per-user rate limit + cooldown আছে।
* `log_out()` কখনো কল করা হয় না (করলে নতুন session string সাথে সাথে বাতিল হয়ে যায়) —
  শুধু `disconnect()` করা হয়।
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qsl

from aiohttp import ClientSession, ClientTimeout, web
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeEmptyError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    PhoneNumberUnoccupiedError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

log = logging.getLogger("sessionbot")

# --------------------------------------------------------------------------- #
# AppKeys (aiohttp-র typed storage key)
# --------------------------------------------------------------------------- #
CFG_KEY = web.AppKey("cfg", "Config")
API_KEY = web.AppKey("api", "Any")
STORE_KEY = web.AppKey("store", "Store")
HTTP_KEY = web.AppKey("http", ClientSession)
TASKS_KEY = web.AppKey("tasks", list)

PHONE_RE = re.compile(r"^\+\d{7,15}$")
DEMO_UID = "0"              # ডেমো মোডে ব্যবহৃত ভুয়া user id
DEMO_PHONE = "+15550000001"
DEMO_CODE = "12345"         # ডেমো মোডের টেস্ট OTP
DEMO_PASSWORD = "demo1234"  # ডেমো মোডের টেস্ট 2FA পাসওয়ার্ড


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class ConfigError(RuntimeError):
    """env variable ভুল/অনুপস্থিত।"""


def _env(env: Mapping[str, str], name: str, *, required: bool = True, default: str = "") -> str:
    raw = (env.get(name) or "").strip()
    if not raw:
        if required:
            raise ConfigError(
                f"{name} সেট করা নেই। এনভায়রনমেন্ট ভেরিয়েবল হিসেবে {name} দিন "
                f"(দেখুন .env.example / README.md)।"
            )
        return default
    return raw


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} অবশ্যই একটি সংখ্যা হবে, পেয়েছি: {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} ধনাত্মক সংখ্যা হতে হবে, পেয়েছি: {value}")
    return value


@dataclass(frozen=True)
class Config:
    bot_token: str
    api_id: int
    api_hash: str
    port: int = 10000
    webapp_url: str = ""
    pending_ttl: int = 900          # pending login কত সেকেন্ড বাঁচবে
    phone_ttl: int = 86400          # শেয়ার করা নম্বর মেমরিতে কত সেকেন্ড থাকবে
    initdata_max_age: int = 3600    # initData কত পুরনো হলে reject
    otp_window: int = 900           # rate-limit window (সেকেন্ড)
    otp_limit: int = 3              # window-তে সর্বোচ্চ কতবার কোড চাওয়া যাবে
    otp_cooldown: int = 30          # দুইবার কোড চাওয়ার মাঝে ন্যূনতম বিরতি
    demo: bool = False

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        env = os.environ if env is None else env
        demo = (env.get("DEMO") or "").strip() in {"1", "true", "yes", "on"}
        if demo:
            log.warning("⚠️  DEMO মোডে চলছে — কোনো আসল Telegram API কল হবে না।")
        webapp_url = _env(env, "WEBAPP_URL", required=False).rstrip("/")
        if webapp_url and not webapp_url.startswith("https://"):
            raise ConfigError(
                "WEBAPP_URL অবশ্যই https:// দিয়ে শুরু হতে হবে (Telegram Mini App-এর জন্য বাধ্যতামূলক)। "
                f"পেয়েছি: {webapp_url!r}"
            )
        cfg = cls(
            bot_token=_env(env, "BOT_TOKEN", required=not demo),
            api_id=_env_int(env, "API_ID", 0) if demo else _env_int(env, "API_ID", 0),
            api_hash=_env(env, "API_HASH", required=False),
            port=_env_int(env, "PORT", 10000),
            webapp_url=webapp_url,
            pending_ttl=_env_int(env, "PENDING_TTL", 900),
            phone_ttl=_env_int(env, "PHONE_TTL", 86400),
            initdata_max_age=_env_int(env, "INITDATA_MAX_AGE", 3600),
            otp_window=_env_int(env, "OTP_WINDOW", 900),
            otp_limit=_env_int(env, "OTP_LIMIT", 3),
            otp_cooldown=_env_int(env, "OTP_COOLDOWN", 30),
            demo=demo,
        )
        if not demo:
            if not cfg.api_id or not cfg.api_hash:
                raise ConfigError("API_ID / API_HASH সেট করা নেই (my.telegram.org থেকে নিন)।")
            if api_hash_looks_invalid(cfg.api_hash):
                raise ConfigError("API_HASH ভুল ফরম্যাটে আছে (৩২ অক্ষরের hex হওয়া উচিত)।")
        return cfg


def api_hash_looks_invalid(api_hash: str) -> bool:
    return not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash or "")


# --------------------------------------------------------------------------- #
# initData যাচাই
# --------------------------------------------------------------------------- #
class InitDataError(RuntimeError):
    """Mini App initData অবৈধ/পুরনো।"""


def verify_init_data(
    init_data: str,
    bot_token: str,
    max_age: int = 3600,
    *,
    now: Optional[float] = None,
) -> dict:
    """Telegram WebApp `initData` যাচাই করে user dict ফেরত দেয়।

    Telegram-এর অফিসিয়াল নিয়ম: hash = HMAC_SHA256(HMAC_SHA256("WebAppData", bot_token), data_check_string)
    """
    if not init_data or not init_data.strip():
        raise InitDataError("initData পাওয়া যায়নি — মিনি অ্যাপটি Telegram থেকে খুলুন।")
    if not bot_token:
        raise InitDataError("সার্ভারে BOT_TOKEN সেট করা নেই।")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=False))
    received = pairs.pop("hash", None)
    if not received:
        raise InitDataError("initData-তে hash নেই — অনুরোধ বাতিল করা হয়েছে।")
    pairs.pop("signature", None)

    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise InitDataError("initData যাচাই ব্যর্থ (hash মেলেনি) — মিনি অ্যাপটি Telegram থেকে খুলুন।")

    now = time.time() if now is None else now
    try:
        auth_date = int(pairs.get("auth_date", "0") or 0)
    except ValueError:
        raise InitDataError("initData-তে auth_date অবৈধ।") from None
    if auth_date <= 0:
        raise InitDataError("initData-তে auth_date নেই।")
    if auth_date > now + 300:
        raise InitDataError("initData-এর সময় ভবিষ্যতের — বাতিল করা হয়েছে।")
    if now - auth_date > max_age:
        raise InitDataError("এই মিনি অ্যাপ সেশন পুরনো হয়ে গেছে — বট মেনু থেকে আবার খুলুন।")

    raw_user = pairs.get("user")
    if not raw_user:
        raise InitDataError("initData-তে user তথ্য নেই।")
    try:
        user = json.loads(raw_user)
    except json.JSONDecodeError:
        raise InitDataError("initData-তে user তথ্য পড়া যায়নি।") from None
    if not isinstance(user, dict) or not user.get("id"):
        raise InitDataError("initData-তে user id নেই।")
    return user


def normalize_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    phone = "+" + digits
    return phone if PHONE_RE.fullmatch(phone) else None


# --------------------------------------------------------------------------- #
# Telegram Bot API wrapper
# --------------------------------------------------------------------------- #
class BotAPIError(RuntimeError):
    def __init__(self, method: str, description: str, error_code: int = 0, parameters: Optional[dict] = None):
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.error_code = error_code
        self.parameters = parameters or {}


class BotAPI:
    """শুধু এই বটের নিজের token দিয়ে Telegram Bot API কল করার ছোট wrapper।"""

    def __init__(self, token: str, session: ClientSession):
        self.token = token
        self.session = session

    async def call(self, method: str, **params) -> Any:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        async with self.session.post(url, json=params) as resp:
            data = await resp.json(content_type=None)
        if not data.get("ok"):
            raise BotAPIError(
                method,
                data.get("description", "unknown error"),
                int(data.get("error_code") or 0),
                data.get("parameters") or {},
            )
        return data.get("result")


# --------------------------------------------------------------------------- #
# Store: নম্বর, pending login, rate limit
# --------------------------------------------------------------------------- #
@dataclass
class PhoneRecord:
    phone: str
    shared_at: float


@dataclass
class Pending:
    uid: str
    phone: str
    phone_code_hash: str
    client: Any
    created: float
    stage: str = "otp"          # "otp" | "2fa"


class DemoClient:
    """ডেমো মোডের নকল client — কোনো নেটওয়ার্ক বা আসল Telegram কল নেই।

    OTP: `12345`, 2FA পাসওয়ার্ড: `demo1234` — শুধু UI ফ্লো দেখার জন্য।
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.number = DemoClient._count = getattr(DemoClient, "_count", 0) + 1
        self.authorized = False
        self.disconnected = False
        self.logged_out = False
        self.session = SimpleNamespace(
            save=lambda: f"DEMO-SESSION-{self.number}-{secrets.token_hex(12)}"
        )

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True

    async def log_out(self) -> None:  # আসল কোডে কখনো ডাকা হয় না
        self.logged_out = True

    async def send_code_request(self, phone: str) -> Any:
        return SimpleNamespace(phone_code_hash=f"DEMO-HASH-{self.number}")

    async def sign_in(self, phone: Any = None, code: Any = None, phone_code_hash: Any = None, password: Any = None) -> Any:
        if password is not None:
            if password != DEMO_PASSWORD:
                raise PasswordHashInvalidError(request=None)
            self.authorized = True
            return await self.get_me()
        if code != DEMO_CODE:
            raise PhoneCodeInvalidError(request=None)
        self.authorized = True
        raise SessionPasswordNeededError(request=None)  # ডেমোতে 2FA ধাপও দেখানো হয়

    async def get_me(self) -> Any:
        if not self.authorized:
            raise RuntimeError("ডেমো ক্লায়েন্ট এখনো লগইন করেনি")
        return SimpleNamespace(
            id=int(DEMO_UID), username="demo_user", first_name="Demo", phone=DEMO_PHONE
        )


def default_client_factory(cfg: Config) -> Callable[[], Any]:
    if cfg.demo:
        return lambda: DemoClient(cfg)

    def factory() -> Any:
        return TelegramClient(
            StringSession(),
            cfg.api_id,
            cfg.api_hash,
            device_model="MySessionBot",
            system_version="1.0",
            app_version="1.0",
        )

    return factory


async def safe_disconnect(client: Any) -> None:
    """client বন্ধ করি — কখনো log_out করি না, কারণ তাতে session string বাতিল হয়ে যায়।"""
    if client is None:
        return
    try:
        await client.disconnect()
    except Exception as exc:  # pragma: no cover - best effort
        log.debug("disconnect ব্যর্থ: %s", exc)


class Store:
    """সব state মেমরিতে — কোনো সেশন/নম্বর ডিস্কে লেখা হয় না।"""

    def __init__(
        self,
        cfg: Config,
        *,
        clock: Callable[[], float] = time.time,
        client_factory: Optional[Callable[[], Any]] = None,
    ):
        self.cfg = cfg
        self.clock = clock
        self.client_factory = client_factory or default_client_factory(cfg)
        self.phones: dict[str, PhoneRecord] = {}
        self.pending: dict[str, Pending] = {}
        self.otp_hits: dict[str, deque] = {}

    # ---- নম্বর ---- #
    async def set_phone(self, uid: str, phone: str) -> None:
        """নম্বর সংরক্ষণ; আগের pending login থাকলে সেটিও বাতিল করা হয়।"""
        await self.reset(uid)
        self.phones[uid] = PhoneRecord(phone=phone, shared_at=self.clock())

    def get_phone(self, uid: str) -> Optional[str]:
        rec = self.phones.get(uid)
        if not rec:
            return None
        if self.clock() - rec.shared_at > self.cfg.phone_ttl:
            self.phones.pop(uid, None)
            return None
        return rec.phone

    # ---- pending login ---- #
    def put_pending(self, uid: str, phone: str, phone_code_hash: str, client: Any) -> Pending:
        pending = Pending(
            uid=uid,
            phone=phone,
            phone_code_hash=phone_code_hash,
            client=client,
            created=self.clock(),
        )
        self.pending[uid] = pending
        return pending

    def get_pending(self, uid: str) -> Optional[Pending]:
        pending = self.pending.get(uid)
        if not pending:
            return None
        if self.clock() - pending.created > self.cfg.pending_ttl:
            self.pending.pop(uid, None)
            return None
        return pending

    async def reset(self, uid: str) -> bool:
        pending = self.pending.pop(uid, None)
        if pending:
            await safe_disconnect(pending.client)
            return True
        return False

    # ---- rate limit ---- #
    def check_otp_rate(self, uid: str) -> tuple[bool, str]:
        if self.cfg.demo:
            return True, ""  # ডেমো মোডে কোনো Telegram কল হয় না, তাই limit দরকার নেই
        now = self.clock()
        hits = self.otp_hits.setdefault(uid, deque())
        while hits and now - hits[0] > self.cfg.otp_window:
            hits.popleft()
        if len(hits) >= self.cfg.otp_limit:
            wait = int(self.cfg.otp_window - (now - hits[0])) + 1
            return False, (
                f"অনেকবার কোড চাওয়া হয়েছে। প্রায় {max(1, wait // 60 + 1)} মিনিট পর আবার চেষ্টা করুন।"
            )
        if hits and now - hits[-1] < self.cfg.otp_cooldown:
            wait = int(self.cfg.otp_cooldown - (now - hits[-1])) + 1
            return False, f"একটু ধৈর্য ধরুন — আবার কোড চাওয়ার আগে {wait} সেকেন্ড অপেক্ষা করুন।"
        hits.append(now)
        return True, ""

    # ---- housekeeping ---- #
    async def sweep(self) -> int:
        """মেয়াদোত্তীর্ণ pending login বন্ধ করে দেয় ও পুরনো নম্বর মুছে ফেলে।"""
        now = self.clock()
        removed = 0
        for uid in list(self.pending):
            pending = self.pending[uid]
            if now - pending.created > self.cfg.pending_ttl:
                self.pending.pop(uid, None)
                await safe_disconnect(pending.client)
                removed += 1
        for uid in list(self.phones):
            if now - self.phones[uid].shared_at > self.cfg.phone_ttl:
                self.phones.pop(uid, None)
        return removed

    async def close(self) -> None:
        for uid in list(self.pending):
            pending = self.pending.pop(uid)
            await safe_disconnect(pending.client)


# --------------------------------------------------------------------------- #
# Telegram update handling (long polling)
# --------------------------------------------------------------------------- #
WELCOME = (
    "👋 <b>আপনার নিজের সেশন জেনারেটর</b>\n\n"
    "নিচের <b>📱 আমার নম্বর শেয়ার করুন</b> বাটনে ট্যাপ করলে বট আপনার নম্বর নেবে "
    "এবং মিনি অ্যাপের লিংক দেবে।\n\n"
    "🔒 এখানে শুধু <b>আপনার নিজের</b> অ্যাকাউন্টের সেশন বানানো যায়। "
    "OTP বা 2FA পাসওয়ার্ড কারো সাথে শেয়ার করবেন না।"
)
CONTACT_OK = (
    "✅ নম্বর সংরক্ষণ করা হয়েছে: <code>{phone}</code>\n\n"
    "এবার নিচের বাটনে ট্যাপ করে মিনি অ্যাপ খুলুন এবং আপনার Telegram-এ আসা ওটিপি দিন।"
)
CONTACT_BAD = (
    "⚠️ এই নম্বরটি নেওয়া গেল না।\n\n"
    "অনুগ্রহ করে <b>নিজের</b> নম্বর শেয়ার করুন — অন্য কারো কন্টাক্ট শেয়ার করলে "
    "বট সেটি গ্রহণ করে না।"
)
HELP = (
    "🔐 <b>কীভাবে ব্যবহার করবেন</b>\n\n"
    "1. /start দিয়ে <b>📱 আমার নম্বর শেয়ার করুন</b> বাটনে ট্যাপ করুন।\n"
    "2. বট যে মিনি অ্যাপের বাটন দেবে সেটি খুলুন।\n"
    "3. আপনার Telegram-এ আসা ওটিপি লিখুন (2FA থাকলে পাসওয়ার্ডও)।\n"
    "4. স্ক্রিনে session string আসবে — কপি করে নিরাপদে রাখুন।\n\n"
    "⚠️ Session string = আপনার অ্যাকাউন্টের চাবি। কাউকে দেবেন না, "
    "কোনো চ্যানেল/গ্রুপে পোস্ট করবেন না।\n\n"
    "প্রয়োজনে /reset দিয়ে চলমান লগইন বাতিল করতে পারেন।"
)
RESET_DONE = "🧹 চলমান লগইন বাতিল করা হয়েছে। আবার /start দিয়ে শুরু করুন।"


def _webapp_url(app: web.Application) -> str:
    return app[CFG_KEY].webapp_url


async def handle_update(app: web.Application, update: dict) -> None:
    api = app[API_KEY]
    store: Store = app[STORE_KEY]
    cfg = app[CFG_KEY]

    if "callback_query" in update:
        await _handle_callback(app, update["callback_query"])
        return

    message = update.get("message")
    if not message:
        return
    chat_id = message["chat"]["id"]
    sender = message.get("from") or {}
    uid = str(sender.get("id") or "")
    text = (message.get("text") or "").strip()

    # --- কন্টাক্ট শেয়ার --- #
    if "contact" in message:
        if not uid or uid != str(chat_id):
            # গ্রুপ/চ্যানেলে কন্টাক্ট এলেই এখানে পড়ে — ব্যক্তিগত চ্যাটে করতে বলি।
            await api.call(
                "sendMessage",
                chat_id=chat_id,
                text="🔒 অনুগ্রহ করে বটের <b>প্রাইভেট চ্যাটে</b> নিজের নম্বর শেয়ার করুন।",
                parse_mode="HTML",
            )
            return
        contact = message["contact"]
        if str(contact.get("user_id") or "") != uid:
            await api.call("sendMessage", chat_id=chat_id, text=CONTACT_BAD, parse_mode="HTML")
            return
        phone = normalize_phone(contact.get("phone_number", ""))
        if not phone:
            await api.call("sendMessage", chat_id=chat_id, text=CONTACT_BAD, parse_mode="HTML")
            return
        await store.set_phone(uid, phone)
        await api.call(
            "sendMessage",
            chat_id=chat_id,
            text=CONTACT_OK.format(phone=html.escape(phone)),
            parse_mode="HTML",
            reply_markup={"remove_keyboard": True},
        )
        if cfg.webapp_url:
            await api.call(
                "sendMessage",
                chat_id=chat_id,
                text="👇 মিনি অ্যাপ খুলে সেশন তৈরি করুন:",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🚀 মিনি অ্যাপ খুলুন", "web_app": {"url": cfg.webapp_url}}]
                    ]
                },
            )
        else:
            await api.call(
                "sendMessage",
                chat_id=chat_id,
                text="⚠️ সার্ভারে WEBAPP_URL সেট করা নেই, তাই বাটন দেখানো যাচ্ছে না।",
            )
        return

    # --- কমান্ড --- #
    if text.startswith("/start"):
        await api.call(
            "sendMessage",
            chat_id=chat_id,
            text=WELCOME,
            parse_mode="HTML",
            reply_markup={
                "keyboard": [[{"text": "📱 আমার নম্বর শেয়ার করুন", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            },
        )
        return

    if text.startswith("/reset"):
        await store.reset(uid)
        await api.call("sendMessage", chat_id=chat_id, text=RESET_DONE, parse_mode="HTML")
        return

    if text.startswith("/help") or text.startswith("/session"):
        await api.call("sendMessage", chat_id=chat_id, text=HELP, parse_mode="HTML")
        return

    await api.call(
        "sendMessage",
        chat_id=chat_id,
        text="ℹ️ /start দিয়ে শুরু করুন অথবা /help দেখুন।",
        parse_mode="HTML",
    )


async def _handle_callback(app: web.Application, cq: dict) -> None:
    api = app[API_KEY]
    data = cq.get("data") or ""
    message = cq.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    sender = str((cq.get("from") or {}).get("id") or "")

    if data == "del:session" and chat_id is not None and sender == str(chat_id):
        try:
            await api.call("deleteMessage", chat_id=chat_id, message_id=message["message_id"])
            await api.call(
                "answerCallbackQuery", callback_query_id=cq["id"], text="✅ সেশনের মেসেজ মুছে ফেলা হয়েছে"
            )
        except BotAPIError as exc:
            await api.call(
                "answerCallbackQuery",
                callback_query_id=cq["id"],
                text=f"⚠️ মুছতে পারিনি: {exc.description}",
                show_alert=True,
            )
        return
    if cq.get("id"):
        await api.call("answerCallbackQuery", callback_query_id=cq["id"])


async def poll_updates(app: web.Application) -> None:
    api = app[API_KEY]
    offset = 0
    try:
        await api.call("deleteWebhook", drop_pending_updates=False)
    except BotAPIError as exc:
        log.warning("deleteWebhook ব্যর্থ: %s", exc)
    while True:
        try:
            updates = await api.call(
                "getUpdates",
                offset=offset,
                timeout=25,
                allowed_updates=["message", "callback_query"],
            )
            for update in updates or []:
                offset = update["update_id"] + 1
                try:
                    await handle_update(app, update)
                except Exception as exc:  # noqa: BLE001 - একটি update ব্যর্থ হলে বাকিগুলো চলবে
                    log.exception("update handle করতে সমস্যা: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("getUpdates ব্যর্থ: %s", exc)
            await asyncio.sleep(3)


async def sweeper(app: web.Application) -> None:
    store: Store = app[STORE_KEY]
    while True:
        try:
            await asyncio.sleep(30)
            removed = await store.sweep()
            if removed:
                log.info("%d টি মেয়াদোত্তীর্ণ pending login বন্ধ করা হয়েছে", removed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("sweeper সমস্যা: %s", exc)


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
def _authed_uid(request: web.Request, body: dict) -> str:
    cfg: Config = request.app[CFG_KEY]
    init_data = (body or {}).get("initData") or ""
    if not init_data:
        if cfg.demo:
            return DEMO_UID
        raise InitDataError("initData পাওয়া যায়নি — মিনি অ্যাপটি টেলিগ্রামের ভেতর থেকে খুলুন।")
    user = verify_init_data(init_data, cfg.bot_token, cfg.initdata_max_age)
    return str(user["id"])


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _clean(text: str, limit: int = 300) -> str:
    """ইউজারের কাছে দেখানোর মতো নিরাপদ এক লাইনের বার্তা।"""
    flat = re.sub(r"\s+", " ", str(text)).strip()
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _detail(text: str, status: int = 400) -> web.Response:
    return web.json_response({"detail": text}, status=status)


async def handle_page(request: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html", charset="utf-8")


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_config(request: web.Request) -> web.Response:
    """মিনি অ্যাপের জন্য পাবলিক মেটাডেটা — কোনো গোপন তথ্য এখানে নেই।"""
    cfg: Config = request.app[CFG_KEY]
    payload: dict[str, Any] = {
        "demo": cfg.demo,
        "webapp_configured": bool(cfg.webapp_url),
    }
    if cfg.demo:  # শুধু ডেমো মোডে দেখানো হয়; প্রোডাকশনে কিছুই ফাঁস হয় না
        payload["demo_code"] = DEMO_CODE
        payload["demo_password"] = DEMO_PASSWORD
    return web.json_response(payload)


async def handle_request_otp(request: web.Request) -> web.Response:
    """mini app খোলার সাথে সাথেই কল হয়: নম্বর আছে কি না দেখে OTP পাঠায়।"""
    cfg: Config = request.app[CFG_KEY]
    store: Store = request.app[STORE_KEY]
    body = await _json_body(request)
    try:
        uid = _authed_uid(request, body)
    except InitDataError as exc:
        return _detail(str(exc), 403)

    phone = store.get_phone(uid)
    if not phone and cfg.demo:
        phone = DEMO_PHONE
    if not phone:
        return _detail(
            "আগে বটের প্রাইভেট চ্যাটে \"📱 আমার নম্বর শেয়ার করুন\" বাটনে ট্যাপ করুন, "
            "তারপর এই মিনি অ্যাপটি খুলুন।",
            409,
        )

    allowed, message = store.check_otp_rate(uid)
    if not allowed:
        return _detail(message, 429)

    await store.reset(uid)  # আগের অসম্পূর্ণ লগইন থাকলে বাতিল
    client: Any = None
    try:
        client = store.client_factory()
        await client.connect()
        code = await client.send_code_request(phone)
        phone_code_hash = getattr(code, "phone_code_hash", None) or code["phone_code_hash"]
    except FloodWaitError as exc:
        await safe_disconnect(client)
        return _detail(
            f"Telegram কিছুক্ষণের জন্য আটকে দিয়েছে — {exc.seconds} সেকেন্ড পর আবার চেষ্টা করুন।", 429
        )
    except (PhoneNumberInvalidError, PhoneNumberUnoccupiedError):
        await safe_disconnect(client)
        return _detail("এই নম্বরটি Telegram-এ নিবন্ধিত নয় বা ফরম্যাট ভুল।", 400)
    except Exception as exc:  # noqa: BLE001
        await safe_disconnect(client)
        log.exception("send_code_request ব্যর্থ")
        return _detail(f"কোড পাঠানো যায়নি: {_clean(exc)}", 400)

    store.put_pending(uid, phone, phone_code_hash, client)
    log.info("OTP পাঠানো হলো (uid=%s, phone=%s)", uid, phone)
    return web.json_response(
        {
            "status": "otp_sent",
            "phone": phone,
            "stage": "otp",
            "demo": cfg.demo,
        }
    )


async def handle_verify_otp(request: web.Request) -> web.Response:
    store: Store = request.app[STORE_KEY]
    body = await _json_body(request)
    try:
        uid = _authed_uid(request, body)
    except InitDataError as exc:
        return _detail(str(exc), 403)

    otp = re.sub(r"\D", "", str(body.get("otp") or ""))
    if not otp:
        return _detail("ওটিপি খালি রাখা যাবে না।", 400)

    pending = store.get_pending(uid)
    if not pending:
        return _detail("লগইন সেশন পাওয়া যায়নি বা মেয়াদ শেষ — আবার ওটিপি নিন।", 409)

    client = pending.client
    try:
        await client.sign_in(phone=pending.phone, code=otp, phone_code_hash=pending.phone_code_hash)
    except SessionPasswordNeededError:
        pending.stage = "2fa"
        return web.json_response({"status": "2fa_required"})
    except (PhoneCodeInvalidError, PhoneCodeEmptyError):
        return _detail("ওটিপি ভুল হয়েছে — আবার দেখে লিখুন।", 400)
    except PhoneCodeExpiredError:
        pending.stage = "expired"
        await store.reset(uid)
        return _detail("ওটিপির মেয়াদ শেষ — আবার কোড পাঠান।", 400)
    except FloodWaitError as exc:
        return _detail(f"অনেকবার চেষ্টা — {exc.seconds} সেকেন্ড পর আবার দিন।", 429)
    except Exception as exc:  # noqa: BLE001
        log.exception("sign_in ব্যর্থ")
        return _detail(f"লগইন করা যায়নি: {_clean(exc)}", 400)

    return await _finalize(request, pending)


async def handle_verify_password(request: web.Request) -> web.Response:
    store: Store = request.app[STORE_KEY]
    body = await _json_body(request)
    try:
        uid = _authed_uid(request, body)
    except InitDataError as exc:
        return _detail(str(exc), 403)

    password = str(body.get("password") or "")
    if not password:
        return _detail("পাসওয়ার্ড খালি রাখা যাবে না।", 400)

    pending = store.get_pending(uid)
    if not pending:
        return _detail("লগইন সেশন পাওয়া যায়নি বা মেয়াদ শেষ — আবার শুরু করুন।", 409)

    try:
        await pending.client.sign_in(password=password)
    except PasswordHashInvalidError:
        return _detail("2FA পাসওয়ার্ড ভুল হয়েছে।", 400)
    except FloodWaitError as exc:
        return _detail(f"অনেকবার চেষ্টা — {exc.seconds} সেকেন্ড পর আবার দিন।", 429)
    except Exception as exc:  # noqa: BLE001
        log.exception("2FA sign_in ব্যর্থ")
        return _detail(f"লগইন করা যায়নি: {_clean(exc)}", 400)

    return await _finalize(request, pending)


async def handle_reset(request: web.Request) -> web.Response:
    store: Store = request.app[STORE_KEY]
    body = await _json_body(request)
    try:
        uid = _authed_uid(request, body)
    except InitDataError as exc:
        return _detail(str(exc), 403)
    await store.reset(uid)
    return web.json_response({"status": "reset"})


async def _finalize(request: web.Request, pending: Pending) -> web.Response:
    """session string বানিয়ে শুধু মালিককেই দেয়, তারপর client disconnect করে।"""
    cfg: Config = request.app[CFG_KEY]
    api: BotAPI = request.app[API_KEY]
    store: Store = request.app[STORE_KEY]
    client = pending.client
    uid = pending.uid

    account: dict = {}
    try:
        me = await client.get_me()
        account = {
            "id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
            "first_name": getattr(me, "first_name", None),
            "phone": getattr(me, "phone", None) or pending.phone,
        }
    except Exception as exc:  # noqa: BLE001 - get_me ব্যর্থ হলেও session string কাজ করে
        log.warning("get_me ব্যর্থ: %s", exc)
        account = {"phone": pending.phone}

    session_string = client.session.save()
    await store.reset(uid)

    who = account.get("username") or account.get("first_name") or str(account.get("id") or "")
    text = (
        "✅ <b>আপনার Session String তৈরি হয়েছে</b>\n"
        f"👤 অ্যাকাউন্ট: <b>{html.escape(str(who))}</b>\n"
        f"📱 নম্বর: <code>{html.escape(str(account.get('phone') or pending.phone))}</code>\n\n"
        "🔑 <b>Session String</b> (ট্যাপ করলেই কপি হবে):\n"
        f"<code>{html.escape(session_string)}</code>\n\n"
        "⚠️ এই স্ট্রিং আপনার অ্যাকাউন্টের পূর্ণ নিয়ন্ত্রণ দেয়। কাউকে দেবেন না, "
        "স্ক্রিনশট শেয়ার করবেন না। কপি করে নিরাপদ জায়গায় রাখুন "
        "(<code>.env</code> বা পাসওয়ার্ড ম্যানেজার), তারপর নিচের বাটনে ট্যাপ করে "
        "এই মেসেজটি মুছে ফেলুন।"
    )
    if not cfg.demo:
        try:
            await api.call(
                "sendMessage",
                chat_id=int(uid) if re.fullmatch(r"-?\d+", uid) else uid,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "🗑 সেশনের মেসেজ মুছে ফেলুন", "callback_data": "del:session"}]
                    ]
                },
            )
        except Exception as exc:  # noqa: BLE001 - bot message fail হলেও mini app session পাবে
            log.warning("session DM পাঠানো যায়নি (uid=%s): %s", uid, exc)

    await safe_disconnect(client)
    log.info("session string তৈরি হয়েছে (uid=%s)", uid)

    return web.json_response(
        {
            "status": "success",
            "session": session_string,
            "account": account,
            "demo": cfg.demo,
        }
    )


# --------------------------------------------------------------------------- #
# Mini App UI
# --------------------------------------------------------------------------- #
PAGE = r"""<!doctype html>
<html lang="bn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Session Generator</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  :root{--bg:#17212b;--card:#1f2b38;--line:#2b3a4a;--accent:#2f8fd8;--ok:#33d17a;--err:#ff6b81;--muted:#8fa1b3}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;background:var(--bg);color:#fff;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans Bengali",sans-serif}
  .card{width:100%;max-width:400px;background:var(--card);border-radius:16px;padding:20px;box-shadow:0 12px 34px rgba(0,0,0,.45)}
  h2{margin:0 0 4px;font-size:19px;text-align:center}
  .sub{text-align:center;color:var(--muted);font-size:12px;margin-bottom:14px}
  .banner{font-size:12px;line-height:1.6;background:#2a1f22;border:1px solid #4a2b32;color:#ffd7dd;
          padding:9px 11px;border-radius:9px;margin-bottom:12px}
  .banner.demo{background:#1f2a1f;border-color:#2f4a33;color:#d6ffe0}
  label{display:block;font-size:13px;color:var(--muted);margin-bottom:6px}
  input,button,textarea{width:100%;padding:12px;border-radius:9px;border:none;font-size:14px;font-family:inherit}
  input{background:var(--bg);color:#fff;border:1px solid var(--line);margin-bottom:10px}
  button{background:var(--accent);color:#fff;font-weight:600;cursor:pointer;margin-bottom:8px}
  button:disabled{opacity:.55;cursor:not-allowed}
  button.ghost{background:transparent;border:1px solid var(--line);color:#cfe0ef;font-weight:500}
  textarea{background:#0f171f;color:#8ef1b6;border:1px solid var(--line);font-family:ui-monospace,Menlo,Consolas,monospace;
           font-size:11px;word-break:break-all;height:110px;resize:vertical}
  .hidden{display:none!important}
  #msg{margin-top:10px;font-size:13px;text-align:center;line-height:1.6;white-space:pre-line}
  .err{color:var(--err)} .ok{color:var(--ok)} .muted{color:var(--muted);font-size:12px;text-align:center}
  .spinner{width:22px;height:22px;border:3px solid var(--line);border-top-color:var(--accent);border-radius:50%;
           margin:6px auto 10px;animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .steps{display:flex;gap:6px;justify-content:center;margin-bottom:14px}
  .steps i{width:26px;height:4px;border-radius:2px;background:var(--line);display:block}
  .steps i.on{background:var(--accent)} .steps i.done{background:var(--ok)}
</style>
</head>
<body>
<div class="card">
  <h2>🔐 Session Generator</h2>
  <div class="sub">শুধু আপনার নিজের অ্যাকাউন্টের জন্য</div>

  <div class="steps"><i id="p1"></i><i id="p2"></i><i id="p3"></i></div>

  <div id="banner" class="banner">
    ⚠️ এই মিনি অ্যাপে আপনার দেওয়া OTP/পাসওয়ার্ড শুধু আপনার নিজের session string বানাতে ব্যবহৃত হয়
    এবং কোথাও পাঠানো হয় না। কারো সাথে শেয়ার করবেন না।
  </div>
  <div id="demo-banner" class="banner demo hidden"></div>

  <div id="step-loading">
    <div class="spinner"></div>
    <div class="muted" id="load-msg">আপনার নম্বর যাচাই করে ওটিপি পাঠানো হচ্ছে…</div>
  </div>

  <div id="step-otp" class="hidden">
    <label id="otp-label">ওটিপি (OTP) কোড দিন</label>
    <input id="otp" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="12345">
    <button id="btn-otp" onclick="verifyOTP()">✅ ওটিপি কনফার্ম করুন</button>
    <button class="ghost" id="btn-resend" onclick="resend()">🔄 নতুন কোড পাঠান</button>
  </div>

  <div id="step-2fa" class="hidden">
    <label>2FA (Two-Step Verification) পাসওয়ার্ড দিন</label>
    <input id="password" type="password" placeholder="পাসওয়ার্ড">
    <button id="btn-2fa" onclick="verifyPassword()">🔓 পাসওয়ার্ড দিয়ে লগইন করুন</button>
  </div>

  <div id="step-done" class="hidden">
    <div id="done-msg" class="ok" style="text-align:center;font-size:13px;margin-bottom:10px;line-height:1.7"></div>
    <label>আপনার Session String</label>
    <textarea id="session" readonly onclick="this.select()"></textarea>
    <button id="btn-copy" onclick="copySession()">📋 কপি করুন</button>
    <div class="muted" style="line-height:1.7">
      কপি করে নিরাপদ জায়গায় রাখুন (<code>.env</code> বা পাসওয়ার্ড ম্যানেজার)।<br>
      একই স্ট্রিং আপনার বট-চ্যাটেও পাঠানো হয়েছে — ওখান থেকে এক ট্যাপে মুছে ফেলতে পারবেন।
    </div>
  </div>

  <div id="msg"></div>
</div>

<script>
const tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
if (tg) { try { tg.ready(); tg.expand(); } catch (e) {} }

const $ = (id) => document.getElementById(id);
const appMeta = { demo: false };

function say(text, cls) {
  const el = $("msg");
  el.className = cls || "";
  el.textContent = text || "";
}
function steps(n) {
  ["p1","p2","p3"].forEach((id, i) => {
    const el = $(id);
    el.className = i + 1 < n ? "done" : (i + 1 === n ? "on" : "");
  });
}
function show(step) {
  ["step-loading","step-otp","step-2fa","step-done"].forEach((id) => $(id).classList.add("hidden"));
  $(step).classList.remove("hidden");
}
function busy(btn, on, idleText, busyText) {
  const b = $(btn);
  b.disabled = on;
  b.textContent = on ? busyText : idleText;
}
async function api(path, body) {
  const payload = Object.assign({ initData: tg ? tg.initData : "" }, body || {});
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  return { ok: res.ok, status: res.status, data: data };
}
function demoBanner(meta) {
  const b = $("demo-banner");
  if (!meta.demo) { b.classList.add("hidden"); return; }
  const bits = [];
  if (meta.demo_code) bits.push("ওটিপি: " + meta.demo_code);
  if (meta.demo_password) bits.push("2FA পাসওয়ার্ড: " + meta.demo_password);
  b.textContent = "🧪 ডেমো মোড — কোনো আসল Telegram লগইন হচ্ছে না।" +
                  (bits.length ? " টেস্ট করতে: " + bits.join(" | ") : "");
  b.classList.remove("hidden");
}

async function bootstrap() {
  let meta = { demo: false };
  try {
    const res = await fetch("/api/config");
    if (res.ok) meta = await res.json();
  } catch (e) {}
  Object.assign(appMeta, meta);
  demoBanner(appMeta);

  if (tg && tg.initData) { requestOtp(); return; }
  if (meta.demo) { requestOtp(); return; }

  show("step-loading");
  steps(1);
  say("🔒 এই মিনি অ্যাপ শুধু Telegram-এর ভেতর থেকে Telegram বটের মেনু/বাটন দিয়ে খুললে কাজ করে।", "err");
  $("load-msg").textContent = "";
}

async function requestOtp() {
  show("step-loading"); steps(1);
  $("load-msg").textContent = "নম্বর যাচাই করে ওটিপি পাঠানো হচ্ছে…";
  const r = await api("/api/request-otp", {});
  if (!r.ok) {
    $("load-msg").textContent = "";
    say(r.data.detail || "অজানা ত্রুটি — আবার চেষ্টা করুন।", "err");
    return false;
  }
  Object.assign(appMeta, r.data);
  demoBanner(appMeta);
  $("otp-label").textContent = "নম্বর (" + r.data.phone + ") এ পাঠানো ওটিপি দিন";
  $("otp").value = "";
  show("step-otp"); steps(2); say("");
  return true;
}

async function resend() {
  busy("btn-resend", true, "🔄 নতুন কোড পাঠান", "পাঠানো হচ্ছে…");
  const ok = await requestOtp();
  busy("btn-resend", false, "🔄 নতুন কোড পাঠান", "পাঠানো হচ্ছে…");
  if (ok) say("নতুন কোড পাঠানো হয়েছে।", "ok");
}

async function verifyOTP() {
  const otp = $("otp").value.trim();
  if (!otp) { say("ওটিপি লিখুন।", "err"); return; }
  busy("btn-otp", true, "✅ ওটিপি কনফার্ম করুন", "যাচাই হচ্ছে…");
  say("");
  const r = await api("/api/verify-otp", { otp: otp });
  busy("btn-otp", false, "✅ ওটিপি কনফার্ম করুন", "যাচাই হচ্ছে…");
  if (r.ok && r.data.status === "2fa_required") {
    $("otp").value = "";
    show("step-2fa"); steps(3);
    say("টু-স্টেপ ভেরিফিকেশন চালু আছে — পাসওয়ার্ড দিন।", "");
    return;
  }
  if (r.ok && r.data.status === "success") { finish(r.data); return; }
  say(r.data.detail || "ওটিপি যাচাই করা যায়নি।", "err");
}

async function verifyPassword() {
  const pwd = $("password").value;
  if (!pwd) { say("পাসওয়ার্ড লিখুন।", "err"); return; }
  busy("btn-2fa", true, "🔓 পাসওয়ার্ড দিয়ে লগইন করুন", "যাচাই হচ্ছে…");
  say("");
  const r = await api("/api/verify-password", { password: pwd });
  busy("btn-2fa", false, "🔓 পাসওয়ার্ড দিয়ে লগইন করুন", "যাচাই হচ্ছে…");
  if (r.ok && r.data.status === "success") { finish(r.data); return; }
  say(r.data.detail || "পাসওয়ার্ড যাচাই করা যায়নি।", "err");
}

function finish(data) {
  $("session").value = data.session || "";
  $("otp").value = "";
  $("password").value = "";
  const acc = data.account || {};
  const who = acc.username ? "@" + acc.username : (acc.first_name || acc.phone || "");
  $("done-msg").textContent = "🎉 সফল!" + (who ? " (" + who + ")" : "") +
                              "\nনিচের session string কপি করে রাখুন।";
  show("step-done"); steps(3);
  ["p1","p2","p3"].forEach((id) => $(id).className = "done");
  say(data.demo ? "🧪 ডেমো মোড — এটি একটি নকল session string।"
                : "বট-চ্যাটে এই স্ট্রিংটি পাঠিয়ে দেওয়া হয়েছে — ওখান থেকে মুছে ফেলতে পারবেন।", "ok");
}

async function copySession() {
  const ta = $("session");
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(ta.value);
    } else {
      ta.select(); document.execCommand("copy");
    }
    say("✅ কপি হয়েছে — নিরাপদ জায়গায় রাখুন।", "ok");
  } catch (e) {
    ta.select(); say("কপি করতে না পারলে টেক্সটটি সিলেক্ট করে ম্যানুয়ালি কপি করুন।", "err");
  }
}

window.addEventListener("load", bootstrap);
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
async def setup_menu_button(app: web.Application) -> None:
    """বটের চ্যাট মেনু-বাটনে মিনি অ্যাপ বসাই (ব্যাকগ্রাউন্ডে — স্টার্টআপ আটকে থাকে না)।"""
    cfg: Config = app[CFG_KEY]
    if cfg.demo:
        log.info("ডেমো মোড — বটের মেনু বাটন সেট করার দরকার নেই।")
        return
    if not cfg.webapp_url:
        log.warning("WEBAPP_URL সেট করা নেই — মিনি অ্যাপ খোলার বাটন দেখানো হবে না।")
        return
    for attempt in range(5):
        try:
            await app[API_KEY].call(
                "setChatMenuButton",
                menu_button={
                    "type": "web_app",
                    "text": "🔐 Session Generator",
                    "web_app": {"url": cfg.webapp_url},
                },
            )
            log.info("মেনু বাটন সেট হয়েছে: %s", cfg.webapp_url)
            return
        except Exception as exc:  # noqa: BLE001 - নেটওয়ার্ক সমস্যায় আবার চেষ্টা
            log.warning("মেনু বাটন সেট করা যায়নি (চেষ্টা %d/5): %s", attempt + 1, exc)
            await asyncio.sleep(min(30, 2 ** attempt))


async def _on_startup(app: web.Application) -> None:
    cfg: Config = app[CFG_KEY]
    http = ClientSession(timeout=ClientTimeout(total=40))
    app[HTTP_KEY] = http
    if app.get(API_KEY) is None:
        app[API_KEY] = BotAPI(cfg.bot_token, http)
    if app.get(STORE_KEY) is None:
        app[STORE_KEY] = Store(cfg)

    if cfg.demo:
        log.warning("🧪 DEMO মোড চালু — শুধু UI টেস্টের জন্য, প্রোডাকশনে ব্যবহার করবেন না।")

    tasks = [asyncio.create_task(sweeper(app)), asyncio.create_task(setup_menu_button(app))]
    if not cfg.demo:
        tasks.append(asyncio.create_task(poll_updates(app)))
    app[TASKS_KEY] = tasks
    log.info("সার্ভার প্রস্তুত (demo=%s, tasks=%d)", cfg.demo, len(tasks))


async def _on_cleanup(app: web.Application) -> None:
    for task in app.get(TASKS_KEY, []):
        task.cancel()
    for task in app.get(TASKS_KEY, []):
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    store = app.get(STORE_KEY)
    if store is not None:
        await store.close()
    http = app.get(HTTP_KEY)
    if http is not None:
        await http.close()


def make_app(
    cfg: Optional[Config] = None,
    *,
    api: Any = None,
    store: Optional[Store] = None,
    http: Optional[ClientSession] = None,
) -> web.Application:
    """aiohttp অ্যাপ বানায়। টেস্টে `api`/`store` inject করা যায়।"""
    if cfg is None:
        cfg = Config.from_env()
    app = web.Application()
    app[CFG_KEY] = cfg
    if api is not None:
        app[API_KEY] = api
    if store is not None:
        app[STORE_KEY] = store
    if http is not None:
        app[HTTP_KEY] = http

    app.router.add_get("/", handle_page)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/config", handle_config)
    app.router.add_post("/api/request-otp", handle_request_otp)
    app.router.add_post("/api/verify-otp", handle_verify_otp)
    app.router.add_post("/api/verify-password", handle_verify_password)
    app.router.add_post("/api/reset", handle_reset)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        raise SystemExit(f"❌ কনফিগারেশন সমস্যা: {exc}")
    log.info("সার্ভার চালু হচ্ছে — port=%s demo=%s", cfg.port, cfg.demo)
    web.run_app(make_app(cfg), host="0.0.0.0", port=cfg.port, print=None)


if __name__ == "__main__":
    main()
