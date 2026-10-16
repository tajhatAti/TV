# 🔐 My Session Generator (নিজের অ্যাকাউন্টের জন্য)

Telegram **বট + Mini App** দিয়ে শুধু **নিজের** অ্যাকাউন্টের `session string` বানানোর বৈধ টুল।

* বটের প্রাইভেট চ্যাটে **📱 আমার নম্বর শেয়ার করুন** → মিনি অ্যাপ খোলে → OTP (+ 2FA) → session string।
* session string **শুধু আপনি** দেখতে পান: মিনি অ্যাপের স্ক্রিনে + আপনার নিজের বট-চ্যাটে
  (সেখানে এক ট্যাপে মুছে ফেলার বাটন আছে)।
* কোনো লগ চ্যানেল, ডেটাবেজ বা তৃতীয় পক্ষ নেই। নম্বর/OTP/পাসওয়ার্ড ডিস্কে লেখা হয় না।

> ⚠️ **নিজের অ্যাকাউন্ট ছাড়া কারো নম্বর/OTP/পাসওয়ার্ড এখানে দেবেন না।**
> Telegram-এর OTP বা 2FA পাসওয়ার্ড কেউ ("ভেরিফিকেশনের জন্য" বলে) চাইলে সেটা সবসময় স্ক্যাম।
> session string = অ্যাকাউন্টের পূর্ণ নিয়ন্ত্রণ; ওটা কারো সাথে শেয়ার করলেই অ্যাকাউন্ট চলে যাবে।

---

## ১. যা যা লাগবে

| জিনিস | কোথা থেকে |
|---|---|
| `BOT_TOKEN` | Telegram-এ [@BotFather](https://t.me/BotFather) → `/newbot` |
| `API_ID`, `API_HASH` | [my.telegram.org](https://my.telegram.org) → API development tools |
| `WEBAPP_URL` | আপনার ডিপ্লয় করা **https** লিংক (Render/ngrok) — Telegram Mini App শুধু https নেয় |

Python 3.10+ দরকার (৩.১১-তে টেস্ট করা)।

## ২. লোকালে চালানো

```bash
git clone <this-repo> && cd TV
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env      # এরপর .env ফাইলে নিজের মান বসান
set -a; source .env; set +a
python bot.py
```

সার্ভার চালু হলে: `http://localhost:10000/health` → `{"status":"ok"}`

### কম ইন্টারনেট/ক্রেডেনশিয়াল ছাড়া UI দেখা (ডেমো মোড)

```bash
DEMO=1 python bot.py
```

ডেমো মোডে কোনো আসল Telegram কল হয় না। টেস্ট করার জন্য:
**OTP: `12345`** এবং **2FA পাসওয়ার্ড: `demo1234`**।
আপনার ব্রাউজারে `http://localhost:10000/` খুলে পুরো ফ্লো (OTP → 2FA → session string)
ক্লিক করে দেখতে পারবেন।

## ৩. আসল বট দিয়ে টেস্ট (নিজের অ্যাকাউন্ট)

1. `WEBAPP_URL` একটি **https** ঠিকানায় পাবলিক হতে হবে। দ্রুত টেস্টের জন্য:
   ```bash
   ngrok http 10000        # বা: cloudflared tunnel --url http://localhost:10000
   ```
   পাওয়া https লিংকটি `WEBAPP_URL`-এ দিন (শেষে `/` থাকলে সমস্যা নেই)।
2. `python bot.py` চালান। লগে দেখবেন: `📱 মেনু বাটন সেট হয়েছে: https://...`
   (সার্ভার চালু হওয়ার সময়ই বট নিজের চ্যাট মেনু-বাটন সেট করে নেয়।)
3. Telegram-এ **আপনার বটের প্রাইভেট চ্যাটে** `/start` পাঠান → **📱 আমার নম্বর শেয়ার করুন**
   বাটনে ট্যাপ করুন (নিজের নম্বর শেয়ার হবে)।
4. বট যে **🚀 মিনি অ্যাপ খুলুন** বাটন দেয়, সেটি ট্যাপ করুন।
5. মিনি অ্যাপে আপনার Telegram-এ আসা OTP দিন; 2FA চালু থাকলে পাসওয়ার্ড দিন।
6. স্ক্রিনে session string আসবে — **📋 কপি করুন**, তারপর বট-চ্যাটের
   **🗑 সেশনের মেসেজ মুছে ফেলুন** বাটনে ট্যাপ করুন।

### session string কীভাবে ব্যবহার করবেন (উদাহরণ)

```python
from telethon import TelegramClient
from telethon.sessions import StringSession

API_ID, API_HASH = 123456, "your_api_hash"

with TelegramClient(StringSession("আপনার_session_string"), API_ID, API_HASH) as client:
    me = client.loop.run_until_complete(client.get_me())
    print(me.username or me.first_name, me.id)   # নিজের অ্যাকাউন্টে লগইন হয়েছে ✅
```

> 🚫 কোডটি কখনো `client.log_out()` করবেন না — ওটা session string সাথে সাথে বাতিল করে দেয়।
> শুধু `client.disconnect()` করবেন। এই প্রজেক্টেও তাই করা হয়।

## ৪. ডিপ্লয় (Render)

1. রিপো GitHub-এ পুশ করুন → Render-এ **New → Web Service** → রিপো সিলেক্ট করুন।
2. Build: `pip install -r requirements.txt` ｜ Start: `python bot.py`
3. Environment → যোগ করুন: `BOT_TOKEN`, `API_ID`, `API_HASH`, `WEBAPP_URL`
   (Render নিজে `PORT` দেয় — হাতে দিতে হবে না)।
4. ডিপ্লয় শেষ হলে `https://আপনার-অ্যাপ.onrender.com/health` খুলে `ok` দেখুন, তারপর
   `WEBAPP_URL`-এ ওই লিংকটি দিয়ে রিস্টার্ট দিন।

**১টি গুরুত্বপূর্ণ সীমাবদ্ধতা:** সেশন মেমরিতে (র‍্যাম) থাকে, তাই Render-এর ন্যূনতম
ফ্রি প্ল্যানে সার্ভিস ঘুমিয়ে পড়লে বা রিস্টার্ট হলে চলমান লগইন হারিয়ে যাবে — তখন
শুধু আবার `/start` করে চেষ্টা করবেন। শেষ ধাপে session string পেলে সেটা সঙ্গে
সঙ্গে আপনার হাতে চলে আসে, তাই এটা ক্ষতিকর নয়।

## ৫. টেস্ট চালানো

```bash
pip install -r requirements-dev.txt
pytest            # ৭০টি টেস্ট, কোনো আসল Telegram কল ছাড়াই
```

টেস্ট কভার করে: `initData` হ্যাশ যাচাই (ভুয়া hash reject, অন্য বটের টোকেন reject,
পুরনো auth_date reject), কনফিগ যাচাই, নম্বর নরমালাইজ, TTL/rate-limit, বট আপডেট
(নম্বর শেয়ার, অন্যের নম্বর reject, গ্রুপে reject, callback), পোলিং লুপ ও offset,
HTTP API (403/409/429/400/200), সেশন শুধু মালিকের কাছে যাওয়া, `log_out` না হওয়া,
2FA ফ্লো, ডেমো ফ্লো, এবং মিনি অ্যাপের JS সিনট্যাক্স (`node --check`) + সব `fetch()`
পাথের সাথে সার্ভার রাউটের মিল।

## ৬. একটি দায়িত্বশীল ফ্লোয়ের জন্য খেয়াল রাখুন

* OTP/পাসওয়ার্ড **আপনার নিজের** চ্যাটে লিখুন; বট-চ্যাটে বা কারো সাথে শেয়ার করবেন না।
* `BOT_TOKEN` ফাঁস হলে BotFather-এ `/revoke`; `API_HASH` ফাঁস হলে my.telegram.org থেকে
  regenerate করুন। এই প্রজেক্টে কোনো ক্রেডেনশিয়াল কোডে লেখা নেই (টেস্ট দিয়ে যাচাই করা)।
* session string অন্য কোথাও (চ্যানেল, নোট, স্ক্রিনশট, চ্যাট) রাখবেন না — পাসওয়ার্ড
  ম্যানেজার বা `.env` ফাইলে রাখুন।
* কোনো ক্ষেত্রেই অন্যের নম্বর/OTP/পাসওয়ার্ড দিয়ে session বানাবেন না — এতে কারো
  অ্যাকাউন্ট দখল করার শামিল হয় এবং এটা Telegram-এর নিয়মভঙ্গ ও অপরাধ।
