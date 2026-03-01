# -*- coding: utf-8 -*-
"""
tx_paid_v4.py - Realtime Tài/Xỉu bot (SignalR websockets) + License KEY paywall (v4)

Theo yêu cầu:
✅ Có /listkey (owner-only) xem key còn bao lâu
✅ Có /delkey (owner-only) xoá key
✅ Token nhớ "chặt" qua restart nhưng KHÔNG tạo file mới:
   -> Lưu token ngay trong keys_db.json (cùng file DB key)
✅ Không dùng /settoken nữa (đúng yêu cầu trước)
✅ Fix /start@BotName trong group
✅ Key bind theo user_id (anti "bú key")
✅ /genkey time: s=giây, p=phút, h=giờ, d=ngày (ghép được: 1h30p, 2d12h)

Chú ý:
- Set token bằng cách điền vào TOKENS_PRESET trong code (1 lần).
  Bot sẽ tự sync token này vào keys_db.json -> restart không quên.
"""

import os
import asyncio
import json
import threading
import time
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import requests
import websockets
import telebot

# =====================
# TELEGRAM CONFIG
# =====================
BOT_TOKEN = "8508252325:AAF6PedW9cdutQ1LHY1RXQgn8V5Y3J-x-cM"
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", disable_web_page_preview=True)

# =====================
# PAYWALL CONFIG
# =====================
# OWNER theo USER_ID (from_user.id)
OWNER_USER_IDS = {6200528252}  # TODO: đổi thành Telegram user id của ông

CONTACT_TELE = "@toibunngu"
PAYINFO = "MBbank 9624072007777"

# DB chung: key + user + token (KHÔNG tạo file mới)
KEY_DB_PATH = "keys_db.json"
DB_LOCK = threading.Lock()

# =====================
# TOKEN PERSIST (trong keys_db.json)
# =====================
# =====================
# GLOBAL ACCESS TOKEN (dùng cho mọi group)
# =====================
ACCESS_TOKEN = "05%2F7JlwSPGwigSOV8RTwJVSzsWEmj8JvB3UwDAmuWFKwreA5VFfooBlRPZbuAJ%2FF8aSN5rFePGfSEBicFLtMrLylrj2XoLZUgPj2qt1RGH7sEwJz7zhnCpWogIougG%2Fu9DMQaSJw63zPy%2FieT76nWz25ex%2BvLK2iVWLI722%2FxxgOanIl7MORVkeymV0RqzB1zWFU9fw65lvfpBpzsnkvxePguHMx9Yh7R1%2BF7BN3QsusInmSLq6gdMwZvc%2B%2B7LHEflJQa56ILuHDgU%2FB0BPtcthPQaSVwl06EQuWGwrcj8pFwR%2F7MdeS%2FZFYUkwFZTn1AvRw5udhJNqvYnnYzCWGvEv4irRhvU24TUZNXMCvVJTCi4OhKWPXWA%3D%3D.60bad326ddd8801297d52e2b39733b31474a1d4d200463fa49e3f2fa6c4b12c3"  # nhớ để trong ngoặc kép

# Runtime tokens in-memory
chat_tokens = {}  # chat_id(int) -> access_token(str)

# Auto subscription theo chat_id và danh sách user_id đã bật auto (có license)
auto_subs = {}  # chat_id -> set(user_id)

last_session = None
pending_session = None

COUNTDOWN_SECONDS = 3
countdown_tasks = {}       # chat_id -> asyncio.Task
countdown_msgs = {}        # chat_id -> message_id
countdown_sessions = {}    # chat_id -> session_id

# =====================
# SIGNALR CONFIG
# =====================
BASE_WSS_HTTP = "https://taixiumd5.gamevn247.online"
NEGOTIATE_URL = f"{BASE_WSS_HTTP}/signalr/negotiate"

HUB_NAME = "md5luckydiceHub"
METHOD_NAME = "Md5sessionInfo"
CONNECTION_DATA = [{"name": HUB_NAME}]

DEFAULT_HEADERS = {
    "Origin": "https://play.gamevn247.online",
    "User-Agent": "Mozilla/5.0",
}

# =====================
# UTILS
# =====================
def _now() -> int:
    return int(time.time())

def actor_id(m) -> int:
    try:
        return int(m.from_user.id)
    except Exception:
        return int(m.chat.id)

def is_owner_user(user_id: int) -> bool:
    return user_id in OWNER_USER_IDS

def safe_send(chat_id: int, text: str, reply_to_message_id=None):
    try:
        return bot.send_message(chat_id, text, reply_to_message_id=reply_to_message_id)
    except Exception:
        return None

def safe_edit(chat_id: int, msg_id: int, text: str):
    try:
        bot.edit_message_text(text, chat_id, msg_id)
        return True
    except Exception:
        return False

def safe_delete(chat_id: int, msg_id: int):
    try:
        bot.delete_message(chat_id, msg_id)
        return True
    except Exception:
        return False

# =====================
# DB (keys_db.json)
# =====================
def _load_db() -> dict:
    try:
        if not os.path.exists(KEY_DB_PATH):
            return {"keys": {}, "users": {}, "tokens": {}}
        with open(KEY_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"keys": {}, "users": {}, "tokens": {}}
        data.setdefault("keys", {})
        data.setdefault("users", {})
        data.setdefault("tokens", {})
        if not isinstance(data["keys"], dict):
            data["keys"] = {}
        if not isinstance(data["users"], dict):
            data["users"] = {}
        if not isinstance(data["tokens"], dict):
            data["tokens"] = {}
        return data
    except Exception:
        return {"keys": {}, "users": {}, "tokens": {}}

def _save_db(db: dict) -> None:
    tmp = KEY_DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, KEY_DB_PATH)

# =====================
# TOKEN PERSIST HELPERS (trong keys_db.json)
# =====================
def load_chat_tokens():
    # giữ cho tương thích, không làm gì cả
    return

def get_chat_token(chat_id: int) -> str | None:
    tok = (ACCESS_TOKEN or "").strip()
    return tok if tok else None

# =====================
# KEY HELPERS
# =====================
def gen_key(prefix="TX") -> str:
    raw = secrets.token_hex(10).upper()
    raw = re.sub(r"[^A-Z0-9]", "", raw)
    return f"{prefix}-{raw[:4]}-{raw[4:8]}-{raw[8:12]}"

def parse_duration_to_seconds(s: str) -> int | None:
    """
    s = giây, p = phút, h = giờ, d = ngày
    ghép được: 1h30p, 2d12h, 45s
    legacy: 30m -> 30p
    """
    if not s:
        return None
    s = s.strip().lower().replace(" ", "")
    s = re.sub(r"(\d+)m\b", r"\1p", s)  # legacy
    pattern = r"(\d+)(s|p|h|d)"
    parts = re.findall(pattern, s)
    if not parts:
        return None
    total = 0
    for n_str, unit in parts:
        n = int(n_str)
        if unit == "s":
            total += n
        elif unit == "p":
            total += n * 60
        elif unit == "h":
            total += n * 3600
        elif unit == "d":
            total += n * 86400
        else:
            return None
    return total if total > 0 else None

def fmt_time_left(expires_at: int) -> str:
    remain = max(0, int(expires_at) - _now())
    if remain <= 0:
        return "0s"
    d, rem = divmod(remain, 86400)
    h, rem = divmod(rem, 3600)
    p, sec = divmod(rem, 60)
    out = []
    if d: out.append(f"{d}d")
    if h: out.append(f"{h}h")
    if p: out.append(f"{p}p")
    if sec and not d and not h: out.append(f"{sec}s")
    return " ".join(out) if out else "0s"

def get_user_license(user_id: int) -> dict | None:
    if is_owner_user(user_id):
        return {"key": "OWNER", "expires_at": 10**18}
    with DB_LOCK:
        db = _load_db()
        u = db["users"].get(str(user_id))
        if not u or not isinstance(u, dict):
            return None
        exp = int(u.get("expires_at") or 0)
        if exp <= _now():
            db["users"].pop(str(user_id), None)
            _save_db(db)
            return None
        return {"key": u.get("key"), "expires_at": exp}

def purge_auto_for_user(user_id: int):
    for cid, subs in list(auto_subs.items()):
        if user_id in subs:
            subs.discard(user_id)
        if not subs:
            auto_subs.pop(cid, None)
            cancel_countdown(cid)

def require_license(func):
    def wrapper(m, *args, **kwargs):
        uid = actor_id(m)
        lic = get_user_license(uid)
        if not lic:
            purge_auto_for_user(uid)
            msg = (
                "🔒 <b>Bản trả phí</b> — ông chưa có <b>KEY</b> nên không dùng được.\n"
                "━━━━━━━━━━━━━━\n"
                "✅ Kích hoạt:\n"
                "• <code>/nhapkey &lt;KEY&gt;</code>\n"
                "━━━━━━━━━━━━━━\n"
                f"📩 Liên hệ: <b>{CONTACT_TELE}</b>\n"
                f"🏦 Thanh toán: <b>{PAYINFO}</b>"
            )
            safe_send(m.chat.id, msg, reply_to_message_id=m.message_id)
            return
        return func(m, *args, **kwargs)
    wrapper.__name__ = func.__name__
    return wrapper

# =====================
# FORMATTERS
# =====================
def fmt_msg(session_id: int, d1: int, d2: int, d3: int) -> str:
    total = d1 + d2 + d3
    if total >= 11:
        kq = "TÀI"; icon = "🟢"; flair = "🔥"
    else:
        kq = "XỈU"; icon = "🔵"; flair = "❄️"
    return (
        f"🎲 <b>PHIÊN #{session_id}</b>\n"
        f"━━━━━━━━━━━━━━\n"
        f"🎯 Xúc xắc: <b>{d1} - {d2} - {d3}</b>\n"
        f"🔢 Tổng: <b>{total}</b>\n"
        f"{icon} KQ: {flair} <b>{kq}</b>\n"
        f"━━━━━━━━━━━━━━"
    )

def fmt_countdown(session_id: int, sec: int) -> str:
    return (
        f"⏳ <b>PHIÊN #{session_id}</b> sắp ra kết quả...\n"
        f"━━━━━━━━━━━━━━\n"
        f"🧨 Đếm ngược: <b>{sec}</b> giây"
    )

# =====================
# COUNTDOWN
# =====================
def cancel_countdown(chat_id: int):
    t = countdown_tasks.get(chat_id)
    if t and not t.done():
        t.cancel()
    mid = countdown_msgs.get(chat_id)
    if mid:
        safe_delete(chat_id, mid)
    countdown_tasks.pop(chat_id, None)
    countdown_msgs.pop(chat_id, None)
    countdown_sessions.pop(chat_id, None)

async def run_countdown_for_chat(chat_id: int, session_id: int):
    countdown_sessions[chat_id] = session_id
    msg = safe_send(chat_id, fmt_countdown(session_id, COUNTDOWN_SECONDS))
    if not msg:
        return
    countdown_msgs[chat_id] = msg.message_id
    try:
        for sec in range(COUNTDOWN_SECONDS, 0, -1):
            if countdown_sessions.get(chat_id) != session_id:
                break
            safe_edit(chat_id, msg.message_id, fmt_countdown(session_id, sec))
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        pass
    finally:
        if countdown_sessions.get(chat_id) == session_id:
            safe_delete(chat_id, msg.message_id)
            countdown_msgs.pop(chat_id, None)
            countdown_sessions.pop(chat_id, None)

def kick_countdown(loop: asyncio.AbstractEventLoop, session_id: int):
    for cid in list(auto_subs.keys()):
        subs = auto_subs.get(cid) or set()
        for uid in list(subs):
            if not get_user_license(uid):
                subs.discard(uid)
        if not subs:
            auto_subs.pop(cid, None)
            cancel_countdown(cid)
            continue
        if countdown_sessions.get(cid) == session_id:
            continue
        cancel_countdown(cid)
        countdown_tasks[cid] = loop.create_task(run_countdown_for_chat(cid, session_id))

def cancel_countdown_for_session(session_id: int):
    for cid in list(auto_subs.keys()):
        if countdown_sessions.get(cid) == session_id:
            cancel_countdown(cid)

def send_to_auto(text: str):
    for cid in list(auto_subs.keys()):
        subs = auto_subs.get(cid) or set()
        for uid in list(subs):
            if not get_user_license(uid):
                subs.discard(uid)
        if not subs:
            auto_subs.pop(cid, None)
            cancel_countdown(cid)
            continue
        safe_send(cid, text)

# =====================
# SIGNALR
# =====================
def strip_signalr_frame(raw: str) -> str | None:
    if not raw or not isinstance(raw, str):
        return None
    if raw.endswith(";"):
        raw = raw[:-1]
    if ":" in raw:
        left, right = raw.split(":", 1)
        if left.isdigit():
            raw = right
    raw = raw.strip()
    return raw or None

@dataclass
class WsCreds:
    wss_url: str

class SignalRClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(DEFAULT_HEADERS)

    def negotiate(self, access_token: str) -> dict:
        params = {
            "clientProtocol": "1.5",
            "connectionData": json.dumps(CONNECTION_DATA, separators=(",", ":")),
            "access_token": access_token,
        }
        r = self.s.get(NEGOTIATE_URL, params=params, timeout=20)
        if r.status_code == 405:
            r = self.s.post(NEGOTIATE_URL, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def build_wss_url(self, access_token: str, nego: dict) -> str:
        connection_token = nego.get("ConnectionToken")
        if not connection_token:
            raise RuntimeError(f"Không thấy ConnectionToken trong negotiate: {nego}")
        wss_base = BASE_WSS_HTTP.replace("https://", "wss://").replace("http://", "ws://")
        connect_url = f"{wss_base}/signalr/connect"
        q = {
            "transport": "webSockets",
            "connectionToken": connection_token,
            "connectionData": json.dumps(CONNECTION_DATA, separators=(",", ":")),
            "tid": "1",
            "access_token": access_token,
        }
        return connect_url + "?" + urlencode(q)

    def get_ws_creds(self, access_token: str) -> WsCreds:
        nego = self.negotiate(access_token)
        wss_url = self.build_wss_url(access_token, nego)
        return WsCreds(wss_url=wss_url)

# =====================
# COMMAND MATCHERS (fix /cmd@bot)
# =====================
def cmd_regex(cmd: str) -> str:
    return rf"^/{cmd}(?:@\w+)?(?:\s|$)"

@bot.message_handler(regexp=cmd_regex("start"))
@bot.message_handler(regexp=cmd_regex("help"))
def start_cmd(m):
    uid = actor_id(m)
    lic = get_user_license(uid)
    status = f"🧾 License: <b>OK</b> (còn <b>{fmt_time_left(lic['expires_at'])}</b>)" if lic else "🧾 License: <b>CHƯA KÍCH HOẠT</b>"
    safe_send(
        m.chat.id,
        "🧠 <b>TX Realtime — Bản tư bản hoá</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"{status}\n"
        "━━━━━━━━━━━━━━\n"
        "📌 <b>Menu</b>\n"
        "• <code>/nhapkey &lt;KEY&gt;</code>\n"
        "• <code>/mykey</code>\n"
        "• <code>/auto</code>\n"
        "• <code>/stop</code>\n"
        "━━━━━━━━━━━━━━\n"
        "🔧 <b>Token</b>: Owner điền vào <code>TOKENS_PRESET</code> trong code (restart 1 lần), bot sẽ tự nhớ.\n"
        "━━━━━━━━━━━━━━\n"
        f"📩 Mua key: <b>{CONTACT_TELE}</b>\n"
        f"🏦 {PAYINFO}",
        reply_to_message_id=m.message_id
    )

@bot.message_handler(regexp=cmd_regex("id"))
def id_cmd(m):
    uid = actor_id(m)
    safe_send(m.chat.id, f"🆔 chat_id: <code>{m.chat.id}</code>\n👤 user_id: <code>{uid}</code>", reply_to_message_id=m.message_id)

@bot.message_handler(regexp=cmd_regex("nhapkey"))
@bot.message_handler(regexp=cmd_regex("key"))
def redeem_key_cmd(m):
    uid = actor_id(m)
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        safe_send(m.chat.id, "❌ Dùng: <code>/nhapkey &lt;KEY&gt;</code>", reply_to_message_id=m.message_id)
        return
    key = parts[1].strip().upper()

    if is_owner_user(uid):
        safe_send(m.chat.id, "👑 Owner không cần key.", reply_to_message_id=m.message_id)
        return

    with DB_LOCK:
        db = _load_db()
        kinfo = (db.get("keys") or {}).get(key)
        if not kinfo:
            safe_send(m.chat.id, "❌ Key không tồn tại hoặc đã bị xoá.", reply_to_message_id=m.message_id)
            return

        used_by = kinfo.get("used_by")
        if used_by is not None and str(used_by) != str(uid):
            safe_send(m.chat.id, "❌ Key này đã được dùng cho người khác.", reply_to_message_id=m.message_id)
            return

        exp = int(kinfo.get("expires_at") or 0)
        if exp <= _now():
            db["keys"].pop(key, None)
            _save_db(db)
            safe_send(m.chat.id, "❌ Key đã hết hạn. Liên hệ để cấp key mới.", reply_to_message_id=m.message_id)
            return

        kinfo["used_by"] = str(uid)
        db["keys"][key] = kinfo
        db["users"][str(uid)] = {"key": key, "expires_at": exp, "since": _now()}
        _save_db(db)

    safe_send(m.chat.id, f"✅ Kích hoạt thành công!\n🔑 Key: <code>{key}</code>\n⏳ Hạn còn: <b>{fmt_time_left(exp)}</b>\nGiờ ông dùng /auto là chạy 😎", reply_to_message_id=m.message_id)

@bot.message_handler(regexp=cmd_regex("mykey"))
@bot.message_handler(regexp=cmd_regex("me"))
def mykey_cmd(m):
    uid = actor_id(m)
    lic = get_user_license(uid)
    if not lic:
        safe_send(m.chat.id, f"❌ Ông chưa có key.\n📩 {CONTACT_TELE}\n🏦 {PAYINFO}\nGõ: <code>/nhapkey &lt;KEY&gt;</code>", reply_to_message_id=m.message_id)
        return
    safe_send(
        m.chat.id,
        "🔐 <b>THÔNG TIN KEY</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"🔑 Key: <code>{lic['key']}</code>\n"
        f"⏳ Hạn: <b>{fmt_time_left(lic['expires_at'])}</b>\n"
        "📌 Trạng thái: ✅ <b>ACTIVE</b>",
        reply_to_message_id=m.message_id
    )

@bot.message_handler(regexp=cmd_regex("auto"))
@require_license
def auto_cmd(m):
    uid = actor_id(m)

    if not get_chat_token(m.chat.id):
        safe_send(
            m.chat.id,
            "❌ Chat này chưa được owner set <b>access_token</b>.\n"
            "👉 Owner mở code, điền token vào <code>TOKENS_PRESET</code> rồi restart bot.",
            reply_to_message_id=m.message_id,
        )
        return

    auto_subs.setdefault(m.chat.id, set()).add(uid)
    safe_send(m.chat.id, "✅ Auto ON Đã Kích Hoạt Rồi Bảo Bối Ơi.", reply_to_message_id=m.message_id)

@bot.message_handler(regexp=cmd_regex("stop"))
def stop_cmd(m):
    uid = actor_id(m)
    subs = auto_subs.get(m.chat.id)
    if subs:
        subs.discard(uid)
        if not subs:
            auto_subs.pop(m.chat.id, None)
            cancel_countdown(m.chat.id)
    safe_send(m.chat.id, "❌ Auto OFF.", reply_to_message_id=m.message_id)

# ===== OWNER: genkey / listkey / delkey =====
@bot.message_handler(regexp=cmd_regex("genkey"))
def genkey_cmd(m):
    uid = actor_id(m)
    if not is_owner_user(uid):
        safe_send(m.chat.id, "⛔ Lệnh này chỉ owner dùng.", reply_to_message_id=m.message_id)
        return

    parts = (m.text or "").split(maxsplit=2)
    if len(parts) < 2:
        safe_send(
            m.chat.id,
            "❌ Dùng: <code>/genkey &lt;time&gt; [note]</code>\n"
            "Time: s=giây, p=phút, h=giờ, d=ngày\n"
            "Ví dụ: <code>/genkey 45s</code>, <code>/genkey 30p</code>, <code>/genkey 1h</code>, <code>/genkey 1d</code>, <code>/genkey 1h30p</code>",
            reply_to_message_id=m.message_id
        )
        return

    dur_s = parse_duration_to_seconds(parts[1])
    if not dur_s:
        safe_send(m.chat.id, "❌ time sai. Ví dụ: 45s, 30p, 1h, 1d, 1h30p, 2d12h", reply_to_message_id=m.message_id)
        return

    note = parts[2].strip() if len(parts) >= 3 else ""
    key = gen_key("TX")
    exp = _now() + dur_s

    with DB_LOCK:
        db = _load_db()
        db["keys"][key] = {"expires_at": exp, "created_at": _now(), "note": note, "used_by": None}
        _save_db(db)

    safe_send(
        m.chat.id,
        "✅ <b>Đã tạo key</b>\n"
        "━━━━━━━━━━━━━━\n"
        f"⏳ Hạn: <b>{fmt_time_left(exp)}</b>\n"
        f"🔑 Key: <code>{key}</code>\n"
        + (f"📝 Note: {note}\n" if note else "")
        + "━━━━━━━━━━━━━━\n"
        "Người dùng nhập: <code>/nhapkey KEY</code>",
        reply_to_message_id=m.message_id
    )

@bot.message_handler(regexp=cmd_regex("listkey"))
@bot.message_handler(regexp=cmd_regex("listkeys"))
def listkeys_cmd(m):
    uid = actor_id(m)
    if not is_owner_user(uid):
        safe_send(m.chat.id, "⛔ Lệnh này chỉ owner dùng.", reply_to_message_id=m.message_id)
        return

    with DB_LOCK:
        db = _load_db()
        items = list((db.get("keys") or {}).items())

    if not items:
        safe_send(m.chat.id, "📭 Chưa có key nào trong DB.", reply_to_message_id=m.message_id)
        return

    items.sort(key=lambda kv: int((kv[1] or {}).get("expires_at") or 0), reverse=True)

    now = _now()
    lines = []
    active_cnt = 0
    for k, v in items[:80]:  # tăng lên 80 cho đã
        v = v or {}
        exp = int(v.get("expires_at") or 0)
        used = v.get("used_by") or "-"
        note = v.get("note") or ""
        if exp > now:
            active_cnt += 1
            left = fmt_time_left(exp)
            status = "✅ ACTIVE"
        else:
            left = "0s"
            status = "⛔ EXPIRED"
        row = f"• <code>{k}</code> | <b>{left}</b> | {status} | used_by: <code>{used}</code>"
        if note:
            row += f" | {note}"
        lines.append(row)

    safe_send(
        m.chat.id,
        f"📦 <b>KEY LIST</b> (hiển thị {min(80, len(items))}/{len(items)} — ACTIVE: {active_cnt})\n"
        "━━━━━━━━━━━━━━\n" + "\n".join(lines),
        reply_to_message_id=m.message_id
    )

@bot.message_handler(regexp=cmd_regex("delkey"))
def delkey_cmd(m):
    uid = actor_id(m)
    if not is_owner_user(uid):
        safe_send(m.chat.id, "⛔ Lệnh này chỉ owner dùng.", reply_to_message_id=m.message_id)
        return

    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2:
        safe_send(m.chat.id, "❌ Dùng: <code>/delkey &lt;KEY&gt;</code>", reply_to_message_id=m.message_id)
        return
    key = parts[1].strip().upper()

    with DB_LOCK:
        db = _load_db()
        kinfo = db["keys"].pop(key, None)
        if kinfo and kinfo.get("used_by") is not None:
            # gỡ user đang dùng key đó
            db["users"].pop(str(kinfo.get("used_by")), None)
        _save_db(db)

    safe_send(m.chat.id, f"✅ Đã xoá key: <code>{key}</code> (nếu tồn tại).", reply_to_message_id=m.message_id)

# =====================
# LISTENER LOOP
# =====================
async def listen_forever():
    global last_session, pending_session

    client = SignalRClient()
    backoff = 1

    while True:
        try:
            token = None
            for cid in list(auto_subs.keys()):
                subs = auto_subs.get(cid) or set()
                for uid in list(subs):
                    if not get_user_license(uid):
                        subs.discard(uid)
                if not subs:
                    auto_subs.pop(cid, None)
                    cancel_countdown(cid)
                    continue

                token = get_chat_token(cid)
                if token:
                    break

            if not token:
                await asyncio.sleep(1)
                continue

            creds = client.get_ws_creds(token)
            ws_headers = [("Origin", DEFAULT_HEADERS["Origin"]), ("User-Agent", DEFAULT_HEADERS["User-Agent"])]

            async with websockets.connect(
                creds.wss_url,
                extra_headers=ws_headers,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_queue=256,
            ) as ws:
                subscribe = {"H": HUB_NAME, "M": METHOD_NAME, "A": [], "I": 0}
                payload = json.dumps(subscribe, separators=(",", ":"))
                frame = f"{len(payload)}:{payload};"
                await ws.send(frame)

                backoff = 1

                while True:
                    raw = await ws.recv()
                    raw2 = strip_signalr_frame(raw)
                    if not raw2 or raw2 == "{}":
                        continue

                    try:
                        data = json.loads(raw2)
                    except Exception:
                        continue

                    msgs = data.get("M")
                    if not msgs:
                        continue

                    for item in msgs:
                        if item.get("M") != METHOD_NAME:
                            continue
                        arr = item.get("A") or []
                        if not arr:
                            continue

                        info = arr[0]
                        session = info.get("SessionID")
                        res = info.get("Result") or {}
                        d1 = res.get("Dice1")
                        d2 = res.get("Dice2")
                        d3 = res.get("Dice3")

                        if session is None:
                            continue
                        try:
                            session = int(session)
                        except Exception:
                            continue

                        if session != pending_session and session != last_session:
                            pending_session = session
                            kick_countdown(asyncio.get_running_loop(), session)

                        try:
                            d1i = int(d1) if d1 is not None else None
                            d2i = int(d2) if d2 is not None else None
                            d3i = int(d3) if d3 is not None else None
                        except Exception:
                            continue

                        if d1i is None or d2i is None or d3i is None:
                            continue
                        if not (1 <= d1i <= 6 and 1 <= d2i <= 6 and 1 <= d3i <= 6):
                            continue
                        total = d1i + d2i + d3i
                        if total < 3 or total > 18:
                            continue
                        if session == last_session:
                            continue

                        cancel_countdown_for_session(session)
                        last_session = session
                        if pending_session == session:
                            pending_session = None

                        send_to_auto(fmt_msg(session, d1i, d2i, d3i))

        except Exception as e:
            print(f"⚠️ Reconnect: {e}")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15)

def start_ws_thread():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(listen_forever())

if __name__ == "__main__":
    load_chat_tokens()
    threading.Thread(target=start_ws_thread, daemon=True).start()
    bot.infinity_polling(timeout=60, long_polling_timeout=60)
