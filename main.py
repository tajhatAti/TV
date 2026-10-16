"""Session String Generator — শুধু নিজের Telegram অ্যাকাউন্টের জন্য।

প্রথমবার চালু (কোনো env variable ছাড়াই)
---------------------------------------
সার্ভার সাথে সাথেই চালু হয় — env না পেলে ক্র্যাশ করে না। পাবলিক লিংক ব্রাউজারে
খুললে **সেটআপ ফর্ম** আসে; ওখানেই BOT_TOKEN, API_ID, API_HASH (এবং চাইলে OWNER_ID)
বসিয়ে "ভেরিফাই করে সেভ" চাপলেই বট চালু হয়ে যায়। মানগুলো `data/config.json`-এ
সেভ থাকে, তাই পরেরবার/রিডিপ্লয়ে আর দিতে হয় না। সেভ করার জন্য ডিপ্লয়ের লগে
দেখানো ছোট **SETUP PIN** লাগে (পাবলিক লিংকে কেউ টোকেন বদলে বট হাইজ্যাক করা
আটকাতে)। চাইলে env দিয়েও সব কিছু দেওয়া যায় — env সবসময় ফাইলের উপরে।

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
import contextlib
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
from dataclasses import dataclass, field
from pathlib import Path
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
from telethon.tl import functions as tl_functions

log = logging.getLogger("sessionbot")

# --------------------------------------------------------------------------- #
# AppKeys (aiohttp-র typed storage key)
# --------------------------------------------------------------------------- #
CFG_KEY = web.AppKey("cfg", "Config")
API_KEY = web.AppKey("api", "Any")
STORE_KEY = web.AppKey("store", "Store")
HTTP_KEY = web.AppKey("http", ClientSession)
RUNTIME_KEY = web.AppKey("runtime", "Runtime")

SETUP_HINT = "⚙️ সেটআপ বাকি — মালিক /setup পেজ খুলে BOT_TOKEN, API ID ও API HASH বসালেই কাজ শুরু হবে।"

PHONE_RE = re.compile(r"^\+\d{7,15}$")
DEMO_UID = "0"              # ডেমো মোডে ব্যবহৃত ভুয়া user id
DEMO_PHONE = "+15550000001"
DEMO_CODE = "12345"         # ডেমো মোডের টেস্ট OTP
DEMO_PASSWORD = "demo1234"  # ডেমো মোডের টেস্ট 2FA পাসওয়ার্ড


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
class ConfigError(RuntimeError):
    """কনফিগারেশন ভুল/অসম্পূর্ণ।"""


# --------------------------------------------------------------------------- #
# কোথায় সেভ হবে (env ছাড়াও কাজ করে)
# --------------------------------------------------------------------------- #
DATA_DIR = Path(os.environ.get("DATA_DIR") or (Path(__file__).resolve().parent / "data"))
CONFIG_FILE = DATA_DIR / "config.json"


def load_config_file(path: Optional[Path] = None) -> dict:
    """ডিস্ক থেকে সেভ করা সেটআপ পড়ি (না থাকলে খালি dict)।"""
    path = path or CONFIG_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        log.warning("config ফাইল পড়া যায়নি (%s): %s", path, exc)
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("config ফাইলে ভুল JSON (%s): %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k).upper(): v for k, v in data.items()}


def save_config_file(data: dict, path: Optional[Path] = None) -> None:
    """সেটআপ ডিস্কে সেভ করি (অ্যাটমিক + শুধু মালিক পড়তে পারে)।"""
    path = path or CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def mask_secret(value: str, keep: int = 6) -> str:
    """টোকেন/হ্যাশ স্ক্রিনে দেখানোর জন্য আংশিক লুকাই।"""
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]


def _read_source(env: Mapping[str, str], data: Mapping[str, Any], name: str) -> str:
    """env আগে, তারপর সেভ করা ফাইল।"""
    raw_env = env.get(name)
    if raw_env is not None and str(raw_env).strip():
        return str(raw_env).strip()
    raw_file = data.get(name)
    return "" if raw_file is None else str(raw_file).strip()


def _source_int(
    env, data, name: str, default: int, *, strict: bool, warnings: list
) -> int:
    raw = _read_source(env, data, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        message = f"{name} অবশ্যই একটি সংখ্যা হবে, পাওয়া গেছে: {raw!r}"
        if strict:
            raise ConfigError(message)
        warnings.append(message)
        return default
    if value <= 0:
        message = f"{name} ধনাত্মক সংখ্যা হতে হবে, পাওয়া গেছে: {value}"
        if strict:
            raise ConfigError(message)
        warnings.append(message)
        return default
    return value


def _source_flag(env, data, name: str, default: bool, *, strict: bool, warnings: list) -> bool:
    raw = _read_source(env, data, name).lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    message = f"{name} অবশ্যই 0/1 (বা true/false) হবে, পাওয়া গেছে: {raw!r}"
    if strict:
        raise ConfigError(message)
    warnings.append(message)
    return default


@dataclass(frozen=True)
class Config:
    bot_token: str = ""
    api_id: int = 0
    api_hash: str = ""
    port: int = 10000
    webapp_url: str = ""
    pending_ttl: int = 900          # pending login কত সেকেন্ড বাঁচবে
    phone_ttl: int = 86400          # শেয়ার করা নম্বর মেমরিতে কত সেকেন্ড থাকবে
    initdata_max_age: int = 3600    # initData কত পুরনো হলে reject
    otp_window: int = 900           # rate-limit window (সেকেন্ড)
    otp_limit: int = 3              # window-তে সর্বোচ্চ কতবার কোড চাওয়া যাবে
    otp_cooldown: int = 30          # দুইবার কোড চাওয়ার মাঝে ন্যূনতম বিরতি
    revoke_window: int = 900        # সেশন বানানোর পর কত সেকেন্ড "অন্য ডিভাইস লগ আউট" করা যাবে
    auto_revoke_others: bool = False  # True হলে সেশন তৈরির সঙ্গেই অন্য ডিভাইস লগ আউট
    owner_id: int = 0               # 0 = যে কেউ ব্যবহার করতে পারে; নাহলে শুধু এই Telegram id
    demo: bool = False

    @property
    def is_configured(self) -> bool:
        """বট চালানোর জন্য যা যা দরকার (টোকেন + API ID + HASH) সব আছে কি না।"""
        return bool(self.bot_token and self.api_id and self.api_hash)

    @classmethod
    def from_sources(
        cls,
        env: Optional[Mapping[str, str]] = None,
        file_data: Optional[Mapping[str, Any]] = None,
        *,
        strict: bool = True,
        warnings: Optional[list] = None,
    ) -> "Config":
        """env → সেভ করা ফাইল → ডিফল্ট, এই ক্রমে মান নেয়।

        `strict=False` হলে ভুল মান বাদ দিয়ে খালি কনফিগ ফেরত দেয় (সার্ভার তবু
        চালু থাকে, যাতে সেটআপ পেজ থেকে ঠিক করা যায়)।
        """
        env = os.environ if env is None else env
        data = {str(k).upper(): v for k, v in (file_data or {}).items()}
        warns: list = warnings if warnings is not None else []

        demo = _source_flag(env, data, "DEMO", False, strict=strict, warnings=warns)
        if demo:
            log.warning("⚠️  DEMO মোডে চলছে — কোনো আসল Telegram API কল হবে না।")

        bot_token = _read_source(env, data, "BOT_TOKEN")
        if bot_token and not re.fullmatch(r"\d{5,}:[A-Za-z0-9_\-]{20,}", bot_token):
            message = "BOT_TOKEN-এর ফরম্যাট ঠিক নেই (দেখতে হবে এমন: 123456789:AA...)"
            if strict:
                raise ConfigError(message)
            warns.append(message)
            bot_token = ""

        api_hash = _read_source(env, data, "API_HASH")
        if api_hash and api_hash_looks_invalid(api_hash):
            message = "API_HASH ভুল ফরম্যাটে আছে (৩২ অক্ষরের hex হওয়া উচিত)"
            if strict:
                raise ConfigError(message)
            warns.append(message)
            api_hash = ""

        webapp_url = _read_source(env, data, "WEBAPP_URL").rstrip("/")
        if webapp_url and not webapp_url.startswith("https://"):
            message = (
                "WEBAPP_URL অবশ্যই https:// দিয়ে শুরু হতে হবে "
                f"(Telegram Mini App-এর জন্য বাধ্যতামূলক)। পাওয়া গেছে: {webapp_url!r}"
            )
            if strict:
                raise ConfigError(message)
            warns.append(message)
            webapp_url = ""

        cfg = cls(
            bot_token=bot_token,
            api_id=_source_int(env, data, "API_ID", 0, strict=strict, warnings=warns),
            api_hash=api_hash,
            port=_source_int(env, data, "PORT", 10000, strict=strict, warnings=warns),
            webapp_url=webapp_url,
            pending_ttl=_source_int(env, data, "PENDING_TTL", 900, strict=strict, warnings=warns),
            phone_ttl=_source_int(env, data, "PHONE_TTL", 86400, strict=strict, warnings=warns),
            initdata_max_age=_source_int(env, data, "INITDATA_MAX_AGE", 3600, strict=strict, warnings=warns),
            otp_window=_source_int(env, data, "OTP_WINDOW", 900, strict=strict, warnings=warns),
            otp_limit=_source_int(env, data, "OTP_LIMIT", 3, strict=strict, warnings=warns),
            otp_cooldown=_source_int(env, data, "OTP_COOLDOWN", 30, strict=strict, warnings=warns),
            revoke_window=_source_int(env, data, "REVOKE_WINDOW", 900, strict=strict, warnings=warns),
            auto_revoke_others=_source_flag(
                env, data, "AUTO_REVOKE_OTHERS", False, strict=strict, warnings=warns
            ),
            owner_id=_source_int(env, data, "OWNER_ID", 0, strict=strict, warnings=warns),
            demo=demo,
        )
        return cfg

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        """শুধু env থেকে (টেস্ট/স্ক্রিপ্টের জন্য)।"""
        return cls.from_sources(env, {}, strict=True)

    def to_file_data(self) -> dict:
        """সেভ করার মতো dict (শুধু যা সেট করা আছে)।"""
        data: dict[str, Any] = {
            "BOT_TOKEN": self.bot_token,
            "API_ID": self.api_id or "",
            "API_HASH": self.api_hash,
            "WEBAPP_URL": self.webapp_url,
            "OWNER_ID": self.owner_id or "",
            "AUTO_REVOKE_OTHERS": 1 if self.auto_revoke_others else 0,
            "REVOKE_WINDOW": self.revoke_window,
        }
        return data


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
    stage: str = "otp"          # "otp" | "2fa" | "done"
    session_string: str = ""    # শুধু মেমরিতে, revoke ধাপের জন্য দরকার হয় না
    completed_at: float = 0.0


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
        self.revoked_hashes: list[Any] = []
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

    async def __call__(self, request: Any) -> Any:
        """GetAuthorizations / ResetAuthorization — ডেমোতে নকল সেশন লিস্ট।"""
        name = type(request).__name__
        if name == "GetAuthorizationsRequest":
            return SimpleNamespace(
                authorizations=[
                    SimpleNamespace(hash=1, current=True, device_model="This mini app", platform="Web"),
                    SimpleNamespace(hash=2, current=False, device_model="Phone (demo)", platform="Android"),
                    SimpleNamespace(hash=3, current=False, device_model="Old laptop (demo)", platform="Windows"),
                ]
            )
        if name == "ResetAuthorizationRequest":
            self.revoked_hashes.append(request.hash)
            return True
        raise RuntimeError(f"ডেমো ক্লায়েন্ট এই request চেনে না: {name}")


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


async def revoke_other_sessions(client: Any) -> list[str]:
    """অন্য সব ডিভাইস/সেশন থেকে লগ আউট করি — **নিজের নতুন সেশনটি বাদ দিয়ে**।

    এটাই "ফোন থেকে অটো লগ আউট" এর আসল পদ্ধতি: `log_out()` ডাকলে নতুন session
    string-টাও বাতিল হয়ে যায়, কিন্তু `account.resetAuthorization` দিয়ে শুধু
    বাকি সেশনগুলো (ফোন, পুরনো ল্যাপটপ) বন্ধ করা যায় — string অটুট থাকে।
    """
    auths = await client(tl_functions.account.GetAuthorizationsRequest())
    revoked: list[str] = []
    for auth in getattr(auths, "authorizations", None) or []:
        if getattr(auth, "current", False):
            continue  # এটাই এইমাত্র বানানো সেশন — একে ছোঁব না
        await client(tl_functions.account.ResetAuthorizationRequest(hash=auth.hash))
        label = getattr(auth, "device_model", None) or getattr(auth, "app_name", None) or "অজানা ডিভাইস"
        platform = getattr(auth, "platform", None)
        revoked.append(f"{label} ({platform})" if platform else str(label))
    return revoked


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
        self.completed: dict[str, Pending] = {}   # revoke ধাপের জন্য অল্প সময় রাখা হয়
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

    # ---- সেশন তৈরি হওয়ার পরের ছোট window (শুধু "অন্য ডিভাইস লগ আউট" এর জন্য) ---- #
    def complete(self, uid: str, pending: Pending) -> None:
        """লগইন সফল — pending থেকে সরিয়ে সাময়িকভাবে ধরে রাখি (মেমরিতে)।"""
        self.pending.pop(uid, None)
        pending.stage = "done"
        pending.completed_at = self.clock()
        self.completed[uid] = pending

    async def get_completed(self, uid: str) -> Optional[Pending]:
        pending = self.completed.get(uid)
        if not pending:
            return None
        if self.clock() - pending.completed_at > self.cfg.revoke_window:
            self.completed.pop(uid, None)
            await safe_disconnect(pending.client)
            return None
        return pending

    async def drop_completed(self, uid: str) -> bool:
        pending = self.completed.pop(uid, None)
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
        for uid in list(self.completed):
            pending = self.completed[uid]
            if now - pending.completed_at > self.cfg.revoke_window:
                self.completed.pop(uid, None)
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
        for uid in list(self.completed):
            pending = self.completed.pop(uid)
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
    return app_config(app).webapp_url


async def handle_update(app: web.Application, update: dict) -> None:
    api = app_api(app)
    store: Store = app[STORE_KEY]
    cfg = app_config(app)

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

    # OWNER_ID সেট থাকলে বাকি সবাইকে ignore করা হয় (কোনো লুকানো keyword/ট্রিগার নয়)
    if cfg.owner_id and uid and uid != str(cfg.owner_id):
        log.info("অননুমোদিত uid=%s বট ব্যবহারের চেষ্টা করেছে — ignore", uid)
        try:
            await api.call(
                "sendMessage",
                chat_id=chat_id,
                text="🔒 এই বটটি ব্যক্তিগত — শুধু মালিক এটি ব্যবহার করতে পারেন।",
            )
        except Exception:  # noqa: BLE001
            pass
        return

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
    api = app_api(app)
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
    api = app_api(app)
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
    cfg: Config = app_config(request.app)
    init_data = (body or {}).get("initData") or ""
    if not init_data:
        if cfg.demo:
            return DEMO_UID
        raise InitDataError("initData পাওয়া যায়নি — মিনি অ্যাপটি টেলিগ্রামের ভেতর থেকে খুলুন।")
    user = verify_init_data(init_data, cfg.bot_token, cfg.initdata_max_age)
    uid = str(user["id"])
    if cfg.owner_id and uid != str(cfg.owner_id):
        log.warning("অননুমোদিত uid=%s মিনি অ্যাপ ব্যবহারের চেষ্টা করেছে", uid)
        raise InitDataError("🔒 এই বটটি ব্যক্তিগত — শুধু মালিকের নিজের ব্যবহারের জন্য।")
    return uid


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



SETUP_PAGE = r"""<!doctype html>
<html lang="bn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>সেটআপ — Session Generator</title>
<style>
  :root{--bg:#17212b;--card:#1f2b38;--line:#2b3a4a;--accent:#2f8fd8;--ok:#33d17a;--err:#ff6b81;--muted:#8fa1b3}
  *{box-sizing:border-box}
  body{margin:0;min-height:100vh;padding:16px;background:var(--bg);color:#fff;
       font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Noto Sans Bengali",sans-serif;display:grid;place-items:center}
  .card{width:100%;max-width:460px;background:var(--card);border-radius:16px;padding:20px;box-shadow:0 12px 34px rgba(0,0,0,.45)}
  h2{margin:0 0 4px;font-size:19px;text-align:center}
  .sub{text-align:center;color:var(--muted);font-size:12px;margin-bottom:14px}
  .box{font-size:12.5px;line-height:1.7;padding:10px 12px;border-radius:9px;margin-bottom:12px}
  .info{background:#1b2735;border:1px solid var(--line);color:#cfe0ef}
  .good{background:#16281d;border:1px solid #2f4a33;color:#d6ffe0}
  .bad{background:#2a1f22;border:1px solid #4a2b32;color:#ffd7dd}
  label{display:block;font-size:12.5px;color:var(--muted);margin:10px 0 5px}
  input[type=text],input[type=password],input[type=number]{width:100%;padding:11px;border-radius:9px;background:var(--bg);
       color:#fff;border:1px solid var(--line);font-size:14px;font-family:inherit}
  .row{display:flex;gap:8px;align-items:center;margin-top:10px}
  .row input[type=checkbox]{width:18px;height:18px}
  .row span{font-size:12.5px;color:#cfe0ef}
  button{width:100%;padding:12px;border-radius:9px;border:none;background:var(--accent);color:#fff;font-weight:600;
         font-size:14px;cursor:pointer;font-family:inherit;margin-top:10px}
  button:disabled{opacity:.55;cursor:not-allowed}
  button.ghost{background:transparent;border:1px solid var(--line);color:#cfe0ef;font-weight:500}
  a{color:#8ecbff}
  code{background:#0f171f;padding:1px 5px;border-radius:5px;font-size:11.5px;word-break:break-all}
  #msg{margin-top:12px;font-size:13px;line-height:1.8;white-space:pre-line}
  .err{color:var(--err)} .ok{color:var(--ok)}
</style>
</head>
<body>
<div class="card">
  <h2>⚙️ সেটআপ</h2>
  <div class="sub">একবার বসালেই সেভ হয়ে থাকবে — কোনো env variable লাগবে না</div>

  <div id="status" class="box info">লোড হচ্ছে…</div>

  <label>🔑 SETUP PIN <span style="color:#5b6b7d">(ডিপ্লয়ের লগে থাকা ৬ সংখ্যার কোড)</span></label>
  <input id="pin" type="password" inputmode="numeric" placeholder="123456" autocomplete="off">

  <label>🤖 BOT_TOKEN <span style="color:#5b6b7d">@BotFather থেকে</span></label>
  <input id="bot_token" type="password" placeholder="123456789:AA..." autocomplete="off">

  <label>🆔 API_ID <span style="color:#5b6b7d">my.telegram.org → API development tools</span></label>
  <input id="api_id" type="number" placeholder="1234567">

  <label>🔐 API_HASH</label>
  <input id="api_hash" type="password" placeholder="32 অক্ষরের hex" autocomplete="off">

  <label>🌐 মিনি অ্যাপ লিংক (WEBAPP_URL)</label>
  <input id="webapp_url" type="text" placeholder="https://your-app.example.com/">

  <label>👤 OWNER_ID <span style="color:#5b6b7d">আপনার numeric id — শুধু আপনি ব্যবহার করতে পারবেন</span></label>
  <input id="owner_id" type="number" placeholder="123456789">
  <button class="ghost" id="btn-detect" onclick="detectOwner()">🔍 বটে /start পাঠিয়ে আমার id বের করুন</button>

  <div class="row">
    <input id="auto_revoke" type="checkbox">
    <span>📵 সেশন তৈরি হওয়ার সঙ্গেই অন্য ডিভাইসগুলো থেকে অটো লগ আউট করবে</span>
  </div>

  <button id="btn-save" onclick="save()">✅ ভেরিফাই করে সেভ করুন ও বট চালু করুন</button>
  <button class="ghost" id="btn-menu" onclick="setMenu()">🔘 মেনু বাটন আবার সেট করুন</button>

  <div id="msg"></div>
</div>
<script>
const $ = (id) => document.getElementById(id);
function say(text, cls) { const el = $("msg"); el.className = cls || ""; el.textContent = text || ""; }
function busy(id, on, busyText) { const b = $(id); b.disabled = on; if (busyText) b.textContent = busyText; }
async function api(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  let data = {}; try { data = await res.json(); } catch (e) {}
  return { ok: res.ok, data: data };
}

async function loadStatus() {
  try {
    const res = await fetch("/api/setup/status");
    const st = await res.json();
    const lines = [];
    if (st.demo) {
      lines.push("🧪 <b>ডেমো মোড</b> চালু আছে (env DEMO=1) — আসল বট ছাড়াই UI টেস্ট হচ্ছে।");
      lines.push("ডেমো ওটিপি: <b>12345</b> · 2FA: <b>demo1234</b>");
    }
    if (st.configured) {
      lines.push("✅ <b>বট কনফিগার করা আছে</b>" + (st.polling ? " · পোলিং চালু" : " · পোলিং বন্ধ"));
      lines.push("টোকেন: <code>" + st.token_masked + "</code> · API_ID: <code>" + st.api_id + "</code>");
      if (st.owner_id) lines.push("OWNER_ID: <code>" + st.owner_id + "</code> (শুধু আপনি ব্যবহার করতে পারবেন)");
      else lines.push("⚠️ OWNER_ID বসানো নেই — যে কেউ আপনার বট দিয়ে সেশন বানাতে পারবে।");
    } else {
      lines.push("⚙️ এখনো কনফিগার করা হয়নি — নিচের ফর্মটি পূরণ করে সেভ করুন।");
    }
    lines.push("🌐 মিনি অ্যাপ লিংক: <code>" + (st.webapp_url || st.detected_url || "—") + "</code>");
    lines.push("🔑 PIN সোর্স: " + st.pin_source + " · খালি ঘর বাদ দিলে আগের মানই থাকবে।");
    $("status").innerHTML = lines.join("<br>");
    if (!st.webapp_url && st.detected_url) $("webapp_url").value = st.detected_url + "/";
    if (st.owner_id) $("owner_id").value = st.owner_id;
    $("auto_revoke").checked = !!st.auto_revoke_others;
    $("btn-menu").style.display = st.configured ? "block" : "none";
  } catch (e) {
    $("status").className = "box bad";
    $("status").textContent = "সার্ভার স্টেটাস পড়া যায়নি: " + e;
  }
}

async function save() {
  const payload = {
    pin: $("pin").value, bot_token: $("bot_token").value, api_id: $("api_id").value,
    api_hash: $("api_hash").value, webapp_url: $("webapp_url").value,
    owner_id: $("owner_id").value, auto_revoke_others: $("auto_revoke").checked ? 1 : 0
  };
  busy("btn-save", true, "⏳ যাচাই করা হচ্ছে…"); say("");
  const r = await api("/api/setup/save", payload);
  busy("btn-save", false, "✅ ভেরিফাই করে সেভ করুন ও বট চালু করুন");
  if (!r.ok) { say("❌ " + (r.data.detail || "সেভ করা যায়নি"), "err"); return; }
  const b = r.data.bot || {};
  let text = "🎉 সেভ হয়েছে ও বট চালু হয়েছে!\nবট: @" + (b.username || "—") + " (id " + (b.id || "—") + ")\n" +
             "মেনু বাটন: " + (r.data.menu_button ? "✅ সেট হয়েছে" : "⚠️ সেট হয়নি") + "\n" +
             "মিনি অ্যাপ লিংক: " + r.data.webapp_url;
  if ((r.data.warnings || []).length) text += "\n\n⚠️ " + r.data.warnings.join("\n⚠️ ");
  say(text, "ok");
  $("bot_token").value = ""; $("api_hash").value = ""; $("pin").value = "";
  loadStatus();
}

async function detectOwner() {
  busy("btn-detect", true, "⏳ খোঁজা হচ্ছে…");
  const r = await api("/api/setup/detect-owner", { pin: $("pin").value, bot_token: $("bot_token").value });
  busy("btn-detect", false, "🔍 বটে /start পাঠিয়ে আমার id বের করুন");
  if (!r.ok) { say("❌ " + (r.data.detail || "পাওয়া যায়নি"), "err"); return; }
  if (!r.data.found) { say("ℹ️ " + (r.data.hint || "কিছু পাওয়া যায়নি"), ""); return; }
  $("owner_id").value = r.data.owner_id;
  say("✅ আপনার id: " + r.data.owner_id + (r.data.username ? " (@" + r.data.username + ")" : "") +
      "\nOWNER_ID ঘরে বসে গেছে — এখন সেভ করুন।", "ok");
}

async function setMenu() {
  busy("btn-menu", true, "⏳ সেট করা হচ্ছে…");
  const r = await api("/api/setup/menu-button", { pin: $("pin").value });
  busy("btn-menu", false, "🔘 মেনু বাটন আবার সেট করুন");
  say(r.ok && r.data.menu_button ? "✅ মেনু বাটন সেট হয়েছে।" : "⚠️ সেট করা যায়নি (PIN/লিংক দেখুন)।",
      r.ok && r.data.menu_button ? "ok" : "err");
}

loadStatus();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# সেটআপ পেজ — env variable ছাড়াই সব কিছু ওয়েব থেকে
# --------------------------------------------------------------------------- #
def request_origin(request: web.Request) -> str:
    """পাবলিক লিংক অনুমান করি (Render/CodeNest-এর reverse proxy হেডার থেকে)।"""
    proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
    host = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
    if not host:
        host = (request.headers.get("Host") or "").split(",")[0].strip()
    if not host:
        return ""
    if proto not in {"http", "https"}:
        proto = "https" if request.scheme in {"https", ""} else request.scheme
    return f"{proto}://{host}"


def setup_state(app: web.Application) -> dict:
    cfg: Config = app_config(app)
    runtime = app.get(RUNTIME_KEY)
    return {
        "configured": cfg.is_configured,
        "demo": cfg.demo,
        "token_masked": mask_secret(cfg.bot_token),
        "token_set": bool(cfg.bot_token),
        "api_id": cfg.api_id or "",
        "api_hash_masked": mask_secret(cfg.api_hash),
        "webapp_url": cfg.webapp_url,
        "owner_id": cfg.owner_id or "",
        "auto_revoke_others": cfg.auto_revoke_others,
        "revoke_window": cfg.revoke_window,
        "pin_source": getattr(runtime, "pin_source", "generated") if runtime else "unknown",
        "polling": any("poll_updates" in t.get_coro().__qualname__ for t in running_tasks(app)),
    }


async def handle_setup_page(request: web.Request) -> web.Response:
    return web.Response(text=SETUP_PAGE, content_type="text/html", charset="utf-8")


async def handle_setup_status(request: web.Request) -> web.Response:
    payload = setup_state(request.app)
    payload["detected_url"] = request_origin(request)
    return web.json_response(payload)


async def handle_setup_save(request: web.Request) -> web.Response:
    """ফর্ম থেকে টোকেন/API ID নিয়ে যাচাই করে সেভ করি, তারপর বট সাথে সাথে চালু করি।"""
    app = request.app
    runtime = app.get(RUNTIME_KEY)
    body = await _json_body(request)
    if not pin_matches(runtime, body.get("pin")):
        return _detail(
            "SETUP PIN ভুল হয়েছে। ডিপ্লয়ের লগে (Live logs) \"SETUP PIN\" লাইনটি দেখুন।", 403
        )

    existing = app_config(app)
    detected = request_origin(request)
    auto_url = detected if detected.startswith("https://") else ""
    payload = {
        "BOT_TOKEN": str(body.get("bot_token") or "").strip() or existing.bot_token,
        "API_ID": str(body.get("api_id") or "").strip() or (existing.api_id or ""),
        "API_HASH": str(body.get("api_hash") or "").strip() or existing.api_hash,
        "WEBAPP_URL": str(body.get("webapp_url") or "").strip()
        or existing.webapp_url
        or auto_url,
        "OWNER_ID": str(body.get("owner_id") or "").strip() or (existing.owner_id or ""),
        "AUTO_REVOKE_OTHERS": 1 if body.get("auto_revoke_others") else 0,
        "REVOKE_WINDOW": existing.revoke_window,
    }
    try:
        cfg = Config.from_sources({}, payload, strict=True)
    except ConfigError as exc:
        return _detail(f"⚠️ {exc}", 400)

    if not cfg.is_configured:
        return _detail("BOT_TOKEN, API ID ও API HASH — তিনটিই দিতে হবে।", 400)
    probe = make_bot_api(app, cfg.bot_token)
    try:
        me = await probe.call("getMe")
    except BotAPIError as exc:
        return _detail(f"❌ টোকেন কাজ করছে না: {exc.description}", 400)
    except Exception as exc:  # noqa: BLE001
        return _detail(f"❌ Telegram-এ কানেক্ট করা যায়নি: {_clean(exc)}", 502)

    data = cfg.to_file_data()
    if runtime is not None:
        data["SETUP_PIN"] = runtime.pin
    try:
        save_config_file(data, runtime.config_path if runtime else None)
    except OSError as exc:
        return _detail(f"❌ সেভ করা যায়নি: {_clean(exc)}", 500)

    await apply_config(app, cfg)
    menu_ok = await set_menu_button(app, retries=1)

    warnings: list[str] = []
    if not cfg.webapp_url:
        warnings.append(
            "মিনি অ্যাপের https লিংক বসানো হয়নি — WEBAPP_URL ঘরে আপনার পাবলিক "
            "https লিংকটি (যেমন https://xxx.example.com/) বসিয়ে আবার সেভ করুন, "
            "তবেই বট থেকে মিনি অ্যাপ খোলার বাটন আসবে।"
        )
    if not cfg.owner_id:
        warnings.append(
            "OWNER_ID দেওয়া হয়নি — যে কেউ আপনার বট দিয়ে নিজের সেশন বানাতে পারবে। "
            "আপনার numeric id বসিয়ে সেভ করা ভালো।"
        )
    if not menu_ok:
        warnings.append(
            "বটের মেনু-বাটন সেট করা যায়নি (টোকেন ঠিক আছে কিনা/লিংক https কিনা দেখুন) — "
            "নিচে \"মেনু বাটন আবার সেট করুন\" চাপতে পারেন।"
        )

    log.info("সেটআপ সেভ হলো (@%s) — বট চালু", me.get("username"))
    return web.json_response(
        {
            "status": "saved",
            "bot": {
                "id": me.get("id"),
                "username": me.get("username"),
                "first_name": me.get("first_name"),
            },
            "webapp_url": cfg.webapp_url,
            "menu_button": menu_ok,
            "polling": any("poll_updates" in t.get_coro().__qualname__ for t in running_tasks(app)),
            "owner_id": cfg.owner_id,
            "warnings": warnings,
        }
    )


async def handle_setup_menu_button(request: web.Request) -> web.Response:
    """মেনু-বাটন আবার সেট করার বাটন।"""
    app = request.app
    runtime = app.get(RUNTIME_KEY)
    body = await _json_body(request)
    if not pin_matches(runtime, body.get("pin")):
        return _detail("SETUP PIN ভুল হয়েছে।", 403)
    if not app_config(app).is_configured:
        return _detail("আগে সেটআপ সেভ করুন।", 409)
    ok = await set_menu_button(app, retries=2)
    return web.json_response({"status": "ok" if ok else "failed", "menu_button": ok})


async def handle_setup_detect_owner(request: web.Request) -> web.Response:
    """বটে যিনি /start পাঠিয়েছেন তার numeric id বের করি (OWNER_ID বসানোর জন্য)।"""
    app = request.app
    runtime = app.get(RUNTIME_KEY)
    body = await _json_body(request)
    if not pin_matches(runtime, body.get("pin")):
        return _detail("SETUP PIN ভুল হয়েছে।", 403)

    token = str(body.get("bot_token") or "").strip() or app_config(app).bot_token
    if not re.fullmatch(r"\d{5,}:[A-Za-z0-9_\-]{20,}", token or ""):
        return _detail("আগে BOT_TOKEN ঘরে টোকেনটি লিখুন, তারপর আবার চাপুন।", 400)

    api = make_bot_api(app, token)
    cancelled = stop_bot_tasks(app)          # long-poll চললে getUpdates conflict করে
    await wait_tasks(cancelled)
    try:
        await api.call("deleteWebhook", drop_pending_updates=False)
        updates = await api.call(
            "getUpdates", offset=-1, limit=1, timeout=0, allowed_updates=["message"]
        )
    except BotAPIError as exc:
        return _detail(f"Telegram: {exc.description}", 400)
    except Exception as exc:  # noqa: BLE001
        return _detail(f"Telegram-এ কানেক্ট করা যায়নি: {_clean(exc)}", 502)
    finally:
        start_bot_tasks(app)
        await asyncio.sleep(0)

    sender = {}
    for update in updates or []:
        message = update.get("message") or update.get("edited_message") or {}
        sender = message.get("from") or {}
    if not sender:
        return web.json_response(
            {
                "found": False,
                "hint": "আপনার বটে /start পাঠান, তারপর এই বাটন আবার চাপুন।",
            }
        )
    return web.json_response(
        {
            "found": True,
            "owner_id": sender.get("id"),
            "username": sender.get("username"),
            "first_name": sender.get("first_name"),
        }
    )


def need_setup(request: web.Request) -> Optional[web.Response]:
    """কনফিগার না থাকলে (ডেমো নয়) API কল আটকাই — সেটআপের পথ দেখাই।"""
    cfg: Config = app_config(request.app)
    if cfg.is_configured or cfg.demo:
        return None
    return _detail(SETUP_HINT, 503)


async def handle_page(request: web.Request) -> web.Response:
    """লিংক খুললে: কনফিগার করা থাকলে মিনি অ্যাপ, নাহলে সেটআপ ফর্ম।"""
    cfg: Config = app_config(request.app)
    if not cfg.is_configured and not cfg.demo:
        return web.Response(text=SETUP_PAGE, content_type="text/html", charset="utf-8")
    return web.Response(text=PAGE, content_type="text/html", charset="utf-8")


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_config(request: web.Request) -> web.Response:
    """মিনি অ্যাপের জন্য পাবলিক মেটাডেটা — কোনো গোপন তথ্য এখানে নেই।"""
    cfg: Config = app_config(request.app)
    payload: dict[str, Any] = {
        "demo": cfg.demo,
        "configured": cfg.is_configured or cfg.demo,
        "webapp_configured": bool(cfg.webapp_url),
    }
    if cfg.demo:  # শুধু ডেমো মোডে দেখানো হয়; প্রোডাকশনে কিছুই ফাঁস হয় না
        payload["demo_code"] = DEMO_CODE
        payload["demo_password"] = DEMO_PASSWORD
    return web.json_response(payload)


async def handle_request_otp(request: web.Request) -> web.Response:
    """mini app খোলার সাথে সাথেই কল হয়: নম্বর আছে কি না দেখে OTP পাঠায়।"""
    guard = need_setup(request)
    if guard is not None:
        return guard

    cfg: Config = app_config(request.app)
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
    guard = need_setup(request)
    if guard is not None:
        return guard

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
    guard = need_setup(request)
    if guard is not None:
        return guard

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


async def handle_revoke_others(request: web.Request) -> web.Response:
    """এইমাত্র যে অ্যাকাউন্টে লগইন হলো তার **অন্য সব সেশন** বন্ধ করে (string অটুট থাকে)।"""
    guard = need_setup(request)
    if guard is not None:
        return guard

    store: Store = request.app[STORE_KEY]
    body = await _json_body(request)
    try:
        uid = _authed_uid(request, body)
    except InitDataError as exc:
        return _detail(str(exc), 403)

    pending = await store.get_completed(uid)
    if not pending:
        return _detail(
            "সেশনটি আর হাতে নেই (সময় শেষ বা নতুন করে শুরু করা হয়েছে)। "
            "আবার সেশন বানিয়ে তারপর এই বাটনটি চাপুন।",
            409,
        )

    try:
        revoked = await revoke_other_sessions(pending.client)
    except Exception as exc:  # noqa: BLE001
        log.exception("revoke_other_sessions ব্যর্থ")
        return _detail(f"লগ আউট করা যায়নি: {_clean(exc)}", 400)

    await store.drop_completed(uid)
    log.info("অন্য %d টি সেশন লগ আউট করা হলো (uid=%s)", len(revoked), uid)
    return web.json_response(
        {
            "status": "ok",
            "revoked": revoked,
            "count": len(revoked),
            "session_kept": True,
        }
    )


async def handle_reset(request: web.Request) -> web.Response:
    guard = need_setup(request)
    if guard is not None:
        return guard

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
    cfg: Config = app_config(request.app)
    api: BotAPI = app_api(request.app)
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
    pending.session_string = session_string

    revoked: Optional[list[str]] = None
    if cfg.auto_revoke_others:
        try:
            revoked = await revoke_other_sessions(client)
            log.info("অটো লগ আউট: %d টি অন্য সেশন বন্ধ (uid=%s)", len(revoked), uid)
        except Exception as exc:  # noqa: BLE001
            log.warning("অন্য ডিভাইস লগ আউট করা যায়নি (uid=%s): %s", uid, exc)
            revoked = None
        await store.reset(uid)
    else:
        # string অটুট রেখে অল্প সময় client ধরে রাখি — "অন্য ডিভাইস লগ আউট" বাটনের জন্য
        store.complete(uid, pending)

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
        "এই মেসেজটি মুছে ফেলুন।\n\n"
        "📵 ফোন/পুরনো ডিভাইস থেকে লগ আউট করতে মিনি অ্যাপে "
        "\"অন্য ডিভাইস লগ আউট\" বাটনটি ব্যবহার করুন — তাতে <b>এই স্ট্রিং নষ্ট হয় না</b>।"
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

    if not cfg.auto_revoke_others:
        log.info("session string তৈরি হয়েছে (uid=%s) — revoke window চালু", uid)
        return web.json_response(
            {
                "status": "success",
                "session": session_string,
                "account": account,
                "demo": cfg.demo,
                "auto_revoked": False,
                "revoke_window": cfg.revoke_window,
            }
        )

    log.info("session string তৈরি হয়েছে (uid=%s)", uid)
    return web.json_response(
        {
            "status": "success",
            "session": session_string,
            "account": account,
            "demo": cfg.demo,
            "auto_revoked": True,
            "revoked": revoked or [],
            "revoke_error": revoked is None,
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

    <div id="revoke-box">
      <button id="btn-revoke" class="ghost" onclick="revokeOthers()">📵 অন্য ডিভাইস থেকে লগ আউট করুন</button>
      <div class="muted" style="line-height:1.7">
        ফোন/পুরনো ল্যাপটপে যেসব সেশন খোলা আছে সেগুলো বন্ধ হয়ে যাবে,
        কিন্তু <b>উপরের session string অটুট থাকবে</b>। (নিজের অ্যাকাউন্টের জন্য নিরাপদ)
      </div>
    </div>

    <div class="muted" style="line-height:1.7;margin-top:10px">
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
  $("revoke-box").classList.remove("hidden");
  $("btn-revoke").disabled = false;
  $("btn-revoke").textContent = "📵 অন্য ডিভাইস থেকে লগ আউট করুন";
  $("otp").value = "";
  $("password").value = "";
  const acc = data.account || {};
  const who = acc.username ? "@" + acc.username : (acc.first_name || acc.phone || "");
  $("done-msg").textContent = "🎉 সফল!" + (who ? " (" + who + ")" : "") +
                              "\nনিচের session string কপি করে রাখুন।";
  show("step-done"); steps(3);
  ["p1","p2","p3"].forEach((id) => $(id).className = "done");
  let msg = data.demo ? "🧪 ডেমো মোড — এটি একটি নকল session string।"
                      : "বট-চ্যাটে এই স্ট্রিংটি পাঠিয়ে দেওয়া হয়েছে — ওখান থেকে মুছে ফেলতে পারবেন।";
  if (data.auto_revoked) {
    const n = (data.revoked || []).length;
    $("revoke-box").classList.add("hidden");
    msg = (data.revoke_error ? "⚠️ অন্য ডিভাইস অটো-লগআউট করা যায়নি (হাতে চেষ্টা করা যাবে)। "
                             : "📵 অন্য " + n + " টি ডিভাইস থেকে স্বয়ংক্রিয়ভাবে লগ আউট হয়েছে। ") + msg;
  }
  say(msg, "ok");
}

async function revokeOthers() {
  busy("btn-revoke", true, "📵 অন্য ডিভাইস থেকে লগ আউট করুন", "লগ আউট করা হচ্ছে…");
  const r = await api("/api/revoke-others", {});
  busy("btn-revoke", false, "📵 অন্য ডিভাইস থেকে লগ আউট করুন", "লগ আউট করা হচ্ছে…");
  if (!r.ok) {
    say(r.data.detail || "লগ আউট করা যায়নি।", "err");
    return;
  }
  const list = (r.data.revoked || []).join(", ");
  say("✅ " + r.data.count + " টি ডিভাইস থেকে লগ আউট হয়েছে" + (list ? " (" + list + ")" : "") +
      "।\nআপনার session string আগের মতোই কাজ করবে।", "ok");
  $("revoke-box").classList.add("hidden");
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
@dataclass
class Runtime:
    """সেটআপ-সংক্রান্ত রানটাইম তথ্য (PIN + config ফাইলের পথ)।"""

    config_path: Path
    pin: str = ""
    pin_source: str = "generated"
    tasks: list = field(default_factory=list)   # চলমান ব্যাকগ্রাউন্ড টাস্ক
    api_factory: Any = None                      # টেস্টে BotAPI-র বদলে নকল বসানোর হুক
    cfg: Optional[Config] = None                 # এখানেই লাইভ কনফিগ (started app-এ লিখি না)
    api: Any = None                              # লাইভ BotAPI wrapper


def resolve_setup_pin(env: Mapping[str, str], data: Mapping[str, Any], path: Path) -> tuple[str, str]:
    """PIN: env `SETUP_PIN` → সেভ করা ফাইল → নতুন ৬ ডিজিট (ফাইলে সেভ হয়ে থাকে)।"""
    env_pin = (env.get("SETUP_PIN") or "").strip()
    if env_pin:
        return env_pin, "env"
    file_pin = str(data.get("SETUP_PIN") or "").strip()
    if file_pin:
        return file_pin, "file"
    pin = str(secrets.randbelow(900000) + 100000)
    merged = dict(data)
    merged["SETUP_PIN"] = pin
    try:
        save_config_file(merged, path)
    except OSError as exc:
        log.warning("PIN সেভ করা যায়নি (%s): %s", path, exc)
    return pin, "generated"


def load_runtime(
    env: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
) -> Runtime:
    env = os.environ if env is None else env
    path = config_path or CONFIG_FILE
    data = load_config_file(path)
    pin, source = resolve_setup_pin(env, data, path)
    return Runtime(config_path=path, pin=pin, pin_source=source)


def make_bot_api(app: web.Application, token: str) -> Any:
    """BotAPI বানাই (টেস্টে runtime.api_factory থাকলে সেটাই ব্যবহার হয়)।"""
    runtime = app.get(RUNTIME_KEY)
    factory = getattr(runtime, "api_factory", None)
    if factory is not None:
        return factory(token)
    return BotAPI(token, app.get(HTTP_KEY))


def startup_config(
    env: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
) -> tuple["Config", Runtime, list]:
    """বুটের সময় কনফিগ: env → সেভ করা ফাইল → ডিফল্ট (কখনো ক্র্যাশ করে না)।"""
    env = os.environ if env is None else env
    runtime = load_runtime(env, config_path)
    data = load_config_file(runtime.config_path)
    warnings: list = []
    cfg = Config.from_sources(env, data, strict=False, warnings=warnings)
    return cfg, runtime, warnings


def pin_matches(runtime: Optional[Runtime], candidate: Any) -> bool:
    if runtime is None or not runtime.pin:
        return True  # PIN সেট না থাকলে (টেস্ট) আটকাই না
    return hmac.compare_digest(str(candidate or ""), runtime.pin)


def app_config(app: web.Application) -> Config:
    """লাইভ কনফিগ — সেটআপ সেভ করলে এই মানটাই বদলায়।"""
    runtime = app.get(RUNTIME_KEY)
    cfg = getattr(runtime, "cfg", None) if runtime is not None else None
    return cfg if cfg is not None else app[CFG_KEY]


def app_api(app: web.Application) -> Any:
    runtime = app.get(RUNTIME_KEY)
    api = getattr(runtime, "api", None) if runtime is not None else None
    return api if api is not None else app.get(API_KEY)


def running_tasks(app: web.Application) -> list:
    runtime = app.get(RUNTIME_KEY)
    return list(getattr(runtime, "tasks", []) or [])


def start_bot_tasks(app: web.Application) -> list:
    """sweeper + মেনু বাটন + (কনফিগার করা থাকলে) পোলিং চালু করি।"""
    cfg: Config = app_config(app)
    runtime = app.get(RUNTIME_KEY)
    stop_bot_tasks(app)   # পুরনো টাস্ক থাকলে বাতিল
    tasks: list = []
    if cfg.demo or cfg.is_configured:
        tasks.append(asyncio.create_task(sweeper(app)))
        tasks.append(asyncio.create_task(setup_menu_button(app)))
        if cfg.is_configured and not cfg.demo:
            tasks.append(asyncio.create_task(poll_updates(app)))
    if runtime is not None:
        runtime.tasks = tasks
    return tasks


def stop_bot_tasks(app: web.Application) -> list:
    """চলমান টাস্কগুলো বাতিল করি (hot-reload/শাটডাউনে)। বাতিল হওয়া টাস্কগুলো ফেরত দেয়।"""
    runtime = app.get(RUNTIME_KEY)
    tasks = list(getattr(runtime, "tasks", []) or [])
    for task in tasks:
        task.cancel()
    if runtime is not None:
        runtime.tasks = []
    return tasks


async def wait_tasks(tasks: list) -> None:
    """বাতিল করা টাস্কগুলো শেষ হওয়া পর্যন্ত অপেক্ষা করি (409 conflict এড়াতে)।"""
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


async def apply_config(app: web.Application, cfg: Config) -> None:
    """নতুন কনফিগ বসাই: হ্যান্ডলার, BotAPI, Store আর টাস্ক — সব রিস্টার্ট ছাড়াই।"""
    runtime = app.get(RUNTIME_KEY)
    if runtime is not None:
        runtime.cfg = cfg
        if app.get(HTTP_KEY) is not None:
            runtime.api = make_bot_api(app, cfg.bot_token)
    store = app.get(STORE_KEY)
    if store is not None:
        store.cfg = cfg
    await wait_tasks(stop_bot_tasks(app))
    start_bot_tasks(app)
    await asyncio.sleep(0)  # নতুন টাস্কগুলো যেন শুরু হয়
    log.info("নতুন কনফিগ প্রয়োগ করা হলো (configured=%s)", cfg.is_configured)


async def set_menu_button(app: web.Application, *, retries: int = 5) -> bool:
    """বটের মেনু-বাটনে মিনি অ্যাপ বসাই। সফল হলে True।"""
    cfg: Config = app_config(app)
    if cfg.demo or not cfg.webapp_url or not cfg.is_configured:
        return False
    for attempt in range(max(1, retries)):
        try:
            await app_api(app).call(
                "setChatMenuButton",
                menu_button={
                    "type": "web_app",
                    "text": "🔐 Session Generator",
                    "web_app": {"url": cfg.webapp_url},
                },
            )
            log.info("মেনু বাটন সেট হয়েছে: %s", cfg.webapp_url)
            return True
        except Exception as exc:  # noqa: BLE001 - নেটওয়ার্ক সমস্যায় আবার চেষ্টা
            log.warning("মেনু বাটন সেট করা যায়নি (চেষ্টা %d/%d): %s", attempt + 1, retries, exc)
            if attempt + 1 < retries:
                await asyncio.sleep(min(30, 2 ** attempt))
    return False


async def setup_menu_button(app: web.Application) -> None:
    """ব্যাকগ্রাউন্ডে একবার মেনু-বাটন সেট করার চেষ্টা (স্টার্টআপ আটকে থাকে না)।"""
    cfg: Config = app_config(app)
    if cfg.demo:
        log.info("ডেমো মোড — বটের মেনু বাটন সেট করার দরকার নেই।")
        return
    if not cfg.webapp_url:
        log.warning("WEBAPP_URL সেট করা নেই — /setup পেজে লিংক বসালে বাটন দেখানো হবে।")
        return
    await set_menu_button(app, retries=3)


async def _on_startup(app: web.Application) -> None:
    cfg: Config = app_config(app)
    http = ClientSession(timeout=ClientTimeout(total=40))
    app[HTTP_KEY] = http
    runtime = app.get(RUNTIME_KEY)
    if runtime is not None:
        runtime.cfg = cfg
        if runtime.api is None:
            runtime.api = app.get(API_KEY) or BotAPI(cfg.bot_token, http)
    if app.get(STORE_KEY) is None:
        app[STORE_KEY] = Store(cfg)

    if cfg.demo:
        log.warning("🧪 DEMO মোড চালু — শুধু UI টেস্টের জন্য, প্রোডাকশনে ব্যবহার করবেন না।")

    if cfg.is_configured or cfg.demo:
        tasks = start_bot_tasks(app)
        log.info("সার্ভার প্রস্তুত (demo=%s, tasks=%d)", cfg.demo, len(tasks))
    elif not cfg.demo:
        runtime = app.get(RUNTIME_KEY)
        pin = runtime.pin if runtime else "(PIN অজানা)"
        log.warning("=" * 62)
        log.warning("⚙️  এখনো সেটআপ হয়নি — কোনো env variable লাগবে না।")
        log.warning("   পাবলিক লিংকটি ব্রাউজারে খুলুন এবং ফর্মটি পূরণ করুন।")
        log.warning("   (ঠিকানা সাধারণত: https://<আপনার-হোস্ট>/setup )")
        log.warning("   🔑 SETUP PIN: %s", pin)
        log.warning("=" * 62)


async def _on_cleanup(app: web.Application) -> None:
    await wait_tasks(stop_bot_tasks(app))
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
    runtime: Optional[Runtime] = None,
) -> web.Application:
    """aiohttp অ্যাপ বানায়। টেস্টে `api`/`store`/`runtime` inject করা যায়।"""
    if cfg is None:
        cfg = Config.from_sources(strict=False)
    if runtime is None:
        runtime = Runtime(config_path=CONFIG_FILE, pin="")
    app = web.Application()
    app[CFG_KEY] = cfg
    runtime.cfg = cfg
    if api is not None:
        runtime.api = api
    app[RUNTIME_KEY] = runtime
    if api is not None:
        app[API_KEY] = api
    if store is not None:
        app[STORE_KEY] = store
    if http is not None:
        app[HTTP_KEY] = http

    app.router.add_get("/", handle_page)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/config", handle_config)
    app.router.add_get("/setup", handle_setup_page)
    app.router.add_get("/api/setup/status", handle_setup_status)
    app.router.add_post("/api/setup/save", handle_setup_save)
    app.router.add_post("/api/setup/menu-button", handle_setup_menu_button)
    app.router.add_post("/api/setup/detect-owner", handle_setup_detect_owner)
    app.router.add_post("/api/request-otp", handle_request_otp)
    app.router.add_post("/api/verify-otp", handle_verify_otp)
    app.router.add_post("/api/verify-password", handle_verify_password)
    app.router.add_post("/api/reset", handle_reset)
    app.router.add_post("/api/revoke-others", handle_revoke_others)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg, runtime, warnings = startup_config()      # কখনো ক্র্যাশ করে না
    for item in warnings:
        log.warning("⚠️  কনফিগ: %s", item)

    log.info("সার্ভার চালু হচ্ছে — port=%s demo=%s configured=%s", cfg.port, cfg.demo, cfg.is_configured)
    if cfg.is_configured:
        log.info("✅ সেভ করা সেটআপ পাওয়া গেছে (টোকেন ...%s) — বট চালু হচ্ছে",
                 cfg.bot_token[-6:] if cfg.bot_token else "")
    elif cfg.demo:
        log.warning("🧪 DEMO মোড — সেটআপ ছাড়াই UI টেস্ট করা যাবে (OTP %s / 2FA %s)",
                    DEMO_CODE, DEMO_PASSWORD)
    else:
        log.warning("⚙️  config ফাইল: %s", runtime.config_path)
        log.warning("⚙️  সেটআপ বাকি — পাবলিক লিংক ব্রাউজারে খুলে ফর্মটি পূরণ করুন। SETUP PIN: %s", runtime.pin)
    web.run_app(make_app(cfg, runtime=runtime), host="0.0.0.0", port=cfg.port, print=None)


if __name__ == "__main__":
    main()
