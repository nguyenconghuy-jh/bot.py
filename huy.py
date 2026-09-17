import os
import re
import ssl
import time
import json
import socket
import sqlite3
import threading
import datetime
import requests
from urllib.parse import urlparse, quote, parse_qs
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# CẤU HÌNH
# ============================================================
TOKEN = "8984311646:AAEa0Fj38eh_wZvmVBPAlgM0eAlK5KcBH8c"
ADMIN_IDS = [7845036083]
COOLDOWN = 8
MAX_WORKERS = 20
DB_PATH = "data/bot.db"
BRAINROT_CYCLE = 300
BRAINROT_GAME = "Steal a Brainrot"
RBX_COOKIE = ""

API = "https://api.telegram.org/bot" + TOKEN
POOL = ThreadPoolExecutor(max_workers=MAX_WORKERS)
_state_lock = threading.Lock()
_last_use = {}

# ============================================================
# HTTP HELPER - tự retry
# ============================================================
_session = requests.Session()
_session.headers.update({"User-Agent": "Mozilla/5.0"})


def tg_post(method, payload):
    url = API + "/" + method
    for attempt in range(3):
        try:
            r = _session.post(url, json=payload, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (502, 503, 504):
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "error": r.text[:200]}
        except Exception as e:
            if attempt < 2:
                time.sleep(2 ** attempt)
                continue
            return {"ok": False, "error": str(e)[:200]}
    return {"ok": False, "error": "max retry"}


def send(chat_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg_post("sendMessage", payload)


def edit(chat_id, msg_id, text, keyboard=None):
    payload = {"chat_id": chat_id, "message_id": msg_id, "text": text, "parse_mode": "Markdown"}
    if keyboard:
        payload["reply_markup"] = {"inline_keyboard": keyboard}
    return tg_post("editMessageText", payload)


def answer_cb(cb_id, text=None, alert=False):
    payload = {"callback_query_id": cb_id}
    if text:
        payload["text"] = text
        payload["show_alert"] = alert
    return tg_post("answerCallbackQuery", payload)


def send_photo(chat_id, photo, caption=""):
    return tg_post("sendPhoto", {"chat_id": chat_id, "photo": photo, "caption": caption, "parse_mode": "Markdown"})


# ============================================================
# DATABASE
# ============================================================
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
_db_lock = threading.Lock()
_db_local = threading.local()


def _conn():
    if not hasattr(_db_local, "conn") or _db_local.conn is None:
        c = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL;")
        _db_local.conn = c
    return _db_local.conn


def db_init():
    with _db_lock:
        c = _conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, domain TEXT, reason TEXT, user_id INTEGER, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, url TEXT, score INTEGER, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS bans(user_id INTEGER PRIMARY KEY, reason TEXT, created_at INTEGER);
        CREATE TABLE IF NOT EXISTS brainrot_sub(user_id INTEGER PRIMARY KEY, chat_id INTEGER, created_at INTEGER);
        """)
        c.commit()


def db_exec(sql, params=()):
    with _db_lock:
        c = _conn()
        c.execute(sql, params)
        c.commit()


def db_q(sql, params=()):
    with _db_lock:
        return _conn().execute(sql, params).fetchall()


def db_q1(sql, params=()):
    with _db_lock:
        return _conn().execute(sql, params).fetchone()


def is_banned(uid):
    return db_q1("SELECT 1 FROM bans WHERE user_id=?", (uid,)) is not None


def brainrot_on(uid, cid):
    db_exec("INSERT OR REPLACE INTO brainrot_sub(user_id,chat_id,created_at) VALUES(?,?,?)", (uid, cid, int(time.time())))


def brainrot_off(uid):
    db_exec("DELETE FROM brainrot_sub WHERE user_id=?", (uid,))


def brainrot_subs():
    return [(r["user_id"], r["chat_id"]) for r in db_q("SELECT user_id,chat_id FROM brainrot_sub")]


# ============================================================
# CHECKER
# ============================================================
BRANDS = ["facebook", "google", "youtube", "shopee", "lazada", "tiktok", "instagram", "telegram", "momo", "zalopay", "binance", "vietcombank", "techcombank", "agribank", "bidv", "mbbank", "vpbank", "acb"]
RISKY_TLD = re.compile(r"\.(tk|ml|ga|cf|gq|xyz|top|buzz|click|link)$")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}


def normalize(raw):
    raw = raw.strip()
    return raw if raw.startswith(("http://", "https://")) else "https://" + raw


def domain_of(url):
    return urlparse(url).netloc.lower().split(":")[0]


def domain_age(domain):
    try:
        r = requests.get("https://rdap.org/domain/" + domain, timeout=6)
        if r.status_code == 200:
            for e in r.json().get("events", []):
                if e.get("eventAction") == "registration":
                    reg = datetime.datetime.fromisoformat(e["eventDate"].replace("Z", "+00:00"))
                    return (datetime.datetime.now(datetime.timezone.utc) - reg).days
    except Exception:
        pass
    return None


def has_ssl(domain):
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(socket.socket(), server_hostname=domain) as s:
            s.settimeout(5)
            s.connect((domain, 443))
        return True
    except Exception:
        return False


def is_live(domain):
    for sch in ("https", "http"):
        try:
            r = requests.get(sch + "://" + domain, timeout=6, allow_redirects=True, headers=UA)
            if r.status_code < 400:
                return True
        except Exception:
            pass
    return False


def trust_score(domain, reported=0):
    score = 100
    reasons = []
    age = domain_age(domain)
    if age is not None:
        if age < 30:
            score -= 40
            reasons.append("⛔ Tên miền mới " + str(age) + " ngày")
        elif age < 90:
            score -= 20
            reasons.append("⚠️ Tên miền mới " + str(age) + " ngày")
        elif age > 365 * 3:
            reasons.append("✅ Tên miền lâu năm " + str(age // 365) + " năm")
    else:
        score -= 10
        reasons.append("❓ Không rõ tuổi tên miền")
    if not has_ssl(domain):
        score -= 15
        reasons.append("⚠️ Không SSL")
    if not is_live(domain):
        score -= 25
        reasons.append("⛔ Web không truy cập được")
    name = domain.split(".")[0]
    for b in BRANDS:
        if b in name and name != b:
            score -= 35
            reasons.append("🚨 Nghi giả mạo: " + b)
            break
    if RISKY_TLD.search(domain):
        score -= 15
        reasons.append("⚠️ TLD rủi ro")
    if reported > 0:
        score -= min(30, reported * 5)
        reasons.append("🚨 Bị report " + str(reported) + " lần")
    return max(0, score), reasons


def rating(score):
    if score >= 80:
        return "✅ UY TÍN CAO"
    if score >= 50:
        return "⚠️ CẦN THẬN TRỌNG"
    return "❌ RỦI RO CAO"


def get_ip_info(domain):
    try:
        ip = socket.gethostbyname(domain)
    except Exception:
        return None
    try:
        r = requests.get("http://ip-api.com/json/" + ip + "?fields=country,city,isp", timeout=6)
        d = r.json()
        return {"ip": ip, "city": d.get("city", "?"), "country": d.get("country", "?"), "isp": d.get("isp", "?")}
    except Exception:
        return {"ip": ip}


def ping_domain(domain):
    for sch in ("https", "http"):
        try:
            start = time.time()
            r = requests.head(sch + "://" + domain, timeout=8, allow_redirects=True, headers=UA)
            return int((time.time() - start) * 1000), r.status_code
        except Exception:
            pass
    return None, None


def short_url(url):
    try:
        r = requests.get("https://tinyurl.com/api-create.php?url=" + url, timeout=6)
        if r.status_code == 200 and r.text.startswith("http"):
            return r.text
    except Exception:
        pass
    return None


def make_qr(text):
    return "https://api.qrserver.com/v1/create-qr-code/?size=400x400&data=" + quote(text)


def get_weather(city):
    try:
        r = requests.get("https://wttr.in/" + quote(city) + "?format=j1", timeout=8, headers=UA)
        c = r.json()["current_condition"][0]
        return "🌤 " + city + ": " + c["temp_C"] + "°C, ẩm " + c["humidity"] + "%, " + c["weatherDesc"][0]["value"]
    except Exception as e:
        return "❌ " + type(e).__name__


def scan_ports(domain):
    ports = [21, 22, 25, 80, 443, 3306, 3389, 5432, 6379, 8080, 8443]
    try:
        ip = socket.gethostbyname(domain)
    except Exception:
        return []
    opened = []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1.2)
        try:
            if s.connect_ex((ip, p)) == 0:
                opened.append(p)
        except Exception:
            pass
        finally:
            s.close()
    return opened


def scan_ssl_info(domain):
    info = {}
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with ctx.wrap_socket(socket.socket(), server_hostname=domain) as s:
            s.settimeout(6)
            s.connect((domain, 443))
            info["tls"] = s.version()
    except Exception as e:
        info["error"] = type(e).__name__
    return info


def scan_subdomains(domain):
    found = []
    for sub in ["www", "mail", "api", "dev", "admin", "test", "shop", "cdn", "app"]:
        full = sub + "." + domain
        try:
            found.append(sub + " → " + socket.gethostbyname(full))
        except Exception:
            pass
    return found


# ============================================================
# VƯỢT LINK - hỗ trợ link4m, yeumoney, adf.ly, bit.ly
# ============================================================
BYPASS_APIS = [
    "https://api.manhbi.io.vn/bypass?url=",
    "https://bypass.vn/api?url=",
    "https://api.zyro.tools/bypass?url=",
]


def bypass_link_redirect(url):
    """Tự theo dõi redirect."""
    try:
        r = requests.get(url, headers=UA, allow_redirects=True, timeout=15)
        final = r.url
        # Bóc tham số redirect nếu có
        if "?url=" in final or "&url=" in final:
            try:
                qs = parse_qs(urlparse(final).query)
                for k in ("url", "u", "redirect", "target", "link"):
                    if k in qs and qs[k]:
                        return qs[k][0]
            except Exception:
                pass
        return final
    except Exception as e:
        return "❌ " + type(e).__name__


def bypass_advanced(url):
    """Vượt link nâng cao - thử API miễn phí trước, fallback tự redirect."""
    headers = {"User-Agent": "Mozilla/5.0"}

    # 1) Thử API bypass
    for api in BYPASS_APIS:
        try:
            r = requests.get(api + url, headers=headers, timeout=20)
            if r.status_code != 200:
                continue
            try:
                data = r.json()
            except Exception:
                continue
            for key in ("url", "result", "link", "destination", "data"):
                if key in data and data[key]:
                    v = data[key]
                    if isinstance(v, str) and v.startswith("http"):
                        return v
                    if isinstance(v, dict) and v.get("url"):
                        return v["url"]
        except Exception:
            continue

    # 2) Fallback: tự redirect
    return bypass_link_redirect(url)


# ============================================================
# ROBLOX
# ============================================================
PRESENCE_TYPES = {0: "🚫 Offline", 1: "🌐 Online (web)", 2: "🎮 Đang chơi game", 3: "🛠 Studio"}


def rbx_headers():
    h = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    if RBX_COOKIE:
        h["Cookie"] = ".ROBLOSECURITY=" + RBX_COOKIE
    return h


def rbx_get(url, params=None):
    try:
        r = requests.get(url, params=params, headers=rbx_headers(), timeout=8)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def rbx_user_by_name(username):
    try:
        r = requests.post("https://users.roblox.com/v1/usernames/users",
                          json={"usernames": [username], "excludeBannedUsers": False},
                          headers=rbx_headers(), timeout=8)
        if r.status_code != 200:
            return None
        data = r.json().get("data", [])
        if not data:
            return None
        uid = data[0]["id"]
    except Exception:
        return None
    info = rbx_get("https://users.roblox.com/v1/users/" + str(uid))
    if not info:
        return None
    info["id"] = uid
    info["username"] = data[0].get("name") or info.get("name")
    return info


def rbx_presence(uid):
    try:
        r = requests.post("https://presence.roblox.com/v1/presence/users",
                          json={"userIds": [uid]}, headers=rbx_headers(), timeout=8)
        if r.status_code != 200:
            return None
        d = r.json().get("userPresences", [])
        return d[0] if d else None
    except Exception:
        return None


def rbx_game_by_universe(uid):
    if not uid:
        return None
    d = rbx_get("https://games.roblox.com/v1/games", params={"universeIds": str(uid)})
    return d["data"][0] if d and d.get("data") else None


def rbx_headshot(username):
    info = rbx_user_by_name(username)
    if not info:
        return None
    t = rbx_get("https://thumbnails.roblox.com/v1/users/avatar-headshot",
                params={"userIds": str(info["id"]), "size": "420x420", "format": "Png"})
    return t["data"][0].get("imageUrl") if t and t.get("data") else None


def rbx_who_text(username):
    info = rbx_user_by_name(username)
    if not info:
        return "❌ Không tìm thấy: " + username
    uid = info["id"]
    p = rbx_presence(uid)
    lines = [
        "🎮 *ROBLOX INFO*", "",
        "👤 Username: `" + str(info.get("name")) + "`",
        "🏷 Tên: " + str(info.get("displayName", "?")),
        "🆔 ID: `" + str(uid) + "`",
        "📅 Tạo: " + ((info.get("created") or "")[:10]),
        "🚫 Ban: " + ("Có" if info.get("isBanned") else "Không"),
        "✅ Tick: " + ("Có" if info.get("hasVerifiedBadge") else "Không"),
    ]
    if p:
        ptype = p.get("userPresenceType", 0)
        lines += ["", "📡 " + PRESENCE_TYPES.get(ptype, "❓")]
        if ptype == 2:
            g = rbx_game_by_universe(p.get("universeId"))
            if g:
                lines.append("🎯 Đang chơi: *" + str(g.get("name", "?")) + "*")
    lines += ["", "📝 Bio: " + ((info.get("description") or "(trống)")[:200])]
    return "\n".join(lines)


def rbx_playing_text(username):
    info = rbx_user_by_name(username)
    if not info:
        return "❌ Không tìm thấy: " + username
    p = rbx_presence(info["id"])
    if not p:
        return "❌ Không lấy được trạng thái (cần RBX_COOKIE)"
    ptype = p.get("userPresenceType", 0)
    lines = ["🎮 *" + username + "*", "", PRESENCE_TYPES.get(ptype, "❓")]
    if ptype == 2:
        g = rbx_game_by_universe(p.get("universeId"))
        if g:
            lines += ["", "🎯 *ĐANG CHƠI:*", "🏷 *" + str(g.get("name", "?")) + "*",
                      "👥 Server: " + str(g.get("playing", 0)) + " người"]
    return "\n".join(lines)


def rbx_game_search(kw, limit=5):
    d = rbx_get("https://games.roblox.com/v1/games", params={"keyword": kw, "limit": str(limit)})
    return [{"name": g.get("name", "?"), "id": g.get("id"), "visits": g.get("placeVisits", 0)} for g in d.get("data", [])[:limit]] if d else []


# ============================================================
# BRAINROT
# ============================================================
_br_cycle = BRAINROT_CYCLE


def br_status():
    now = time.time()
    remain = _br_cycle - (now % _br_cycle)
    return int(remain), int(_br_cycle)


def br_watcher():
    last_idx = -1
    warned_2min = False
    warned_30s = False
    while True:
        try:
            time.sleep(10)
            remain, cycle = br_status()
            idx = int(time.time() // cycle)
            if idx != last_idx:
                last_idx = idx
                warned_2min = False
                warned_30s = False
            text = None
            if not warned_2min and 105 < remain <= 120:
                warned_2min = True
                text = "⚡ *SẮP SPAWN BRAINROT!*\nCòn " + str(remain // 60) + "p " + str(remain % 60) + "s\n🎮 " + BRAINROT_GAME
            elif not warned_30s and 25 < remain <= 30:
                warned_30s = True
                text = "🔥 *30 GIÂY NỮA SPAWN!*\nVào game NGAY!"
            if text:
                for uid, cid in brainrot_subs():
                    try:
                        send(uid, text)
                    except Exception:
                        pass
        except Exception:
            pass


# ============================================================
# KEYBOARDS
# ============================================================
def kb_main():
    return [
        [{"text": "🔍 Check web", "callback_data": "menu_check"}, {"text": "🛰 Scan", "callback_data": "menu_scan"}],
        [{"text": "🔗 Vượt link", "callback_data": "help_vuotlink"}, {"text": "🥚 Brainrot", "callback_data": "menu_brainrot"}],
        [{"text": "🎮 Roblox", "callback_data": "menu_roblox"}, {"text": "🛠 Công cụ", "callback_data": "menu_tools"}],
        [{"text": "📊 Stats", "callback_data": "act_stats"}, {"text": "🕘 Lịch sử", "callback_data": "act_history"}],
        [{"text": "🏆 Top report", "callback_data": "act_top"}],
    ]


def kb_back():
    return [[{"text": "⬅️ Quay lại", "callback_data": "menu_main"}]]


def kb_tools():
    return [
        [{"text": "📍 IP", "callback_data": "help_ip"}, {"text": "📶 Ping", "callback_data": "help_ping"}],
        [{"text": "🔗 Rút gọn", "callback_data": "help_short"}, {"text": "🔳 QR", "callback_data": "help_qr"}],
        [{"text": "🌤 Thời tiết", "callback_data": "help_weather"}],
        [{"text": "⬅️ Quay lại", "callback_data": "menu_main"}],
    ]


def kb_brainrot():
    return [
        [{"text": "🔔 Bật", "callback_data": "br_on"}, {"text": "🔕 Tắt", "callback_data": "br_off"}],
        [{"text": "📊 Trạng thái", "callback_data": "br_status"}, {"text": "🔄 Refresh", "callback_data": "br_refresh"}],
        [{"text": "⬅️ Quay lại", "callback_data": "menu_main"}],
    ]


# ============================================================
# COMMAND HANDLERS
# ============================================================
def cooldown_ok(uid):
    now = time.time()
    with _state_lock:
        t = _last_use.get(uid, 0)
        if now - t < COOLDOWN:
            return False, int(COOLDOWN - (now - t))
        _last_use[uid] = now
        return True, 0


def cmd_start(chat_id, uid):
    send(chat_id, "🤖 *BOT CHECK + VƯỢT LINK + ROBLOX + BRAINROT*\n\nChọn chức năng 👇", kb_main())


def cmd_check(chat_id, uid, args):
    if not args:
        send(chat_id, "Dùng: /check <link>")
        return
    ok, wait = cooldown_ok(uid)
    if not ok:
        send(chat_id, "⏳ Chờ " + str(wait) + "s")
        return
    raw = args[0]
    send(chat_id, "🔍 Đang kiểm tra...")
    def job():
        url = normalize(raw)
        d = domain_of(url)
        score, reasons = trust_score(d)
        reason_txt = "\n".join("  • " + r for r in reasons)
        return "🔗 " + url + "\n🌐 " + d + "\n📊 *" + str(score) + "/100*\n🚦 " + rating(score) + "\n\n📌 Chi tiết:\n" + reason_txt
    try:
        text = POOL.submit(job).result(timeout=60)
    except Exception as e:
        text = "❌ Lỗi: " + str(e)
    send(chat_id, text)


def cmd_scan(chat_id, uid, args):
    if not args:
        send(chat_id, "Dùng: /scan <link>")
        return
    raw = args[0]
    send(chat_id, "🛰 Đang quét...")
    def job():
        url = normalize(raw)
        d = domain_of(url)
        fs = {
            POOL.submit(get_ip_info, d): "ip",
            POOL.submit(scan_ports, d): "ports",
            POOL.submit(scan_ssl_info, d): "ssl",
            POOL.submit(scan_subdomains, d): "subs",
        }
        res = {}
        for f in as_completed(fs):
            try:
                res[fs[f]] = f.result(timeout=60)
            except Exception:
                res[fs[f]] = None
        lines = ["🔍 *SCAN:* " + d, ""]
        ip = res.get("ip")
        if ip:
            lines.append("🌐 IP: " + ip["ip"])
            if "city" in ip:
                lines.append("🗺 " + ip.get("city", "?") + ", " + ip.get("country", "?"))
        lines.append("🚪 Port: " + (", ".join(map(str, res.get("ports") or [])) or "Không có"))
        ssl_i = res.get("ssl") or {}
        if "tls" in ssl_i:
            lines.append("🔒 SSL: " + ssl_i["tls"])
        subs = res.get("subs") or []
        lines.append("🌍 Sub: " + (", ".join(subs[:5]) if subs else "Không có"))
        return "\n".join(lines)
    try:
        text = POOL.submit(job).result(timeout=120)
    except Exception as e:
        text = "❌ Lỗi: " + str(e)
    send(chat_id, text)


def cmd_vuotlink(chat_id, uid, args):
    if not args:
        send(chat_id,
             "🔗 *VƯỢT LINK*\n\n"
             "Dùng: `/vuotlink <url>`\n\n"
             "*Hỗ trợ:* bit.ly, link4m, yeumoney, adf.ly, link1s, shope.ee...\n\n"
             "*Ví dụ:* `/vuotlink https://link4m.com/abc123`\n"
             "_Hoặc chỉ cần gửi link, bot tự vượt._")
        return
    ok, wait = cooldown_ok(uid)
    if not ok:
        send(chat_id, "⏳ Chờ " + str(wait) + "s")
        return
    url = args[0]
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    send(chat_id, "🔗 Đang vượt link (5-20s)...")
    result = POOL.submit(bypass_advanced, url).result(timeout=90)
    if result.startswith("❌"):
        send(chat_id, result + "\n\n💡 Thử lại sau 1-2 phút, hoặc dùng dịch vụ khác.")
        return
    if result == url:
        send(chat_id, "🔗 *LINK ĐÍCH:*\n" + result + "\n\n_Không vượt được (link hết hạn hoặc API lỗi)._")
        return
    # Check uy tín link đích
    d = domain_of(result)
    score, reasons = trust_score(d, 0)
    reason_txt = "\n".join("  • " + r for r in reasons[:4]) if reasons else "  • Không có dấu hiệu xấu"
    send(chat_id,
         "🔗 *LINK ĐÍCH:*\n" + result + "\n\n"
         "📊 Điểm uy tín: *" + str(score) + "/100*\n"
         "🚦 " + rating(score) + "\n\n"
         "📌 Chi tiết:\n" + reason_txt)


def cmd_ip(chat_id, args):
    if not args:
        send(chat_id, "Dùng: /ip <domain>")
        return
    d = domain_of(normalize(args[0]))
    info = POOL.submit(get_ip_info, d).result(timeout=30)
    if not info:
        send(chat_id, "❌ Không phân giải được")
        return
    t = "🌐 `" + d + "`\n📍 IP: `" + info["ip"] + "`"
    if "city" in info:
        t += "\n🗺 " + info.get("city", "?") + ", " + info.get("country", "?") + "\n🏢 " + info.get("isp", "?")
    send(chat_id, t)


def cmd_ping(chat_id, args):
    if not args:
        send(chat_id, "Dùng: /ping <domain>")
        return
    d = domain_of(normalize(args[0]))
    ms, status = POOL.submit(ping_domain, d).result(timeout=30)
    if ms is None:
        send(chat_id, "❌ Không ping được")
        return
    send(chat_id, "📶 Ping `" + d + "`: *" + str(ms) + " ms* - HTTP " + str(status))


def cmd_short(chat_id, args):
    if not args:
        send(chat_id, "Dùng: /short <url>")
        return
    s = POOL.submit(short_url, normalize(args[0])).result(timeout=30)
    send(chat_id, s or "❌ Không rút gọn được")


def cmd_qr(chat_id, args):
    if not args:
        send(chat_id, "Dùng: /qr <text>")
        return
    text = " ".join(args)
    send_photo(chat_id, make_qr(text), "🔳 " + text[:80])


def cmd_weather(chat_id, args):
    if not args:
        send(chat_id, "Dùng: /weather <thành phố>")
        return
    city = " ".join(args)
    t = POOL.submit(get_weather, city).result(timeout=30)
    send(chat_id, t)


def cmd_stats(chat_id):
    u = db_q1("SELECT COUNT(DISTINCT user_id) n FROM history")["n"]
    h = db_q1("SELECT COUNT(*) n FROM history")["n"]
    s = db_q1("SELECT COUNT(*) n FROM brainrot_sub")["n"]
    send(chat_id, "📊 *THỐNG KÊ*\n👥 Users: " + str(u) + "\n🔍 Check: " + str(h) + "\n🥚 Brainrot: " + str(s) + "\n🧵 Workers: " + str(MAX_WORKERS) + "\n🔧 Threads: " + str(threading.active_count()))


def cmd_rbxwho(chat_id, args):
    if not args:
        send(chat_id, "Dùng: /rbxwho <username>")
        return
    un = args[0]
    t = POOL.submit(rbx_who_text, un).result(timeout=30)
    hs = POOL.submit(rbx_headshot, un).result(timeout=30)
    if hs and "Không tìm thấy" not in t:
        send_photo(chat_id, hs, t)
    else:
        send(chat_id, t)


def cmd_rbxplaying(chat_id, args):
    if not args:
        send(chat_id, "Dùng: /rbxplaying <username>")
        return
    t = POOL.submit(rbx_playing_text, args[0]).result(timeout=30)
    send(chat_id, t)


def cmd_rbxgame(chat_id, args):
    if not args:
        send(chat_id, "Dùng: /rbxgame <tên game>")
        return
    res = POOL.submit(rbx_game_search, " ".join(args)).result(timeout=30)
    if not res:
        send(chat_id, "❌ Không tìm thấy")
        return
    lines = ["🎮 *KẾT QUẢ:*", ""]
    for i, g in enumerate(res, 1):
        lines.append(str(i) + ". *" + g["name"] + "*\n   🆔 `" + str(g["id"]) + "`")
    send(chat_id, "\n".join(lines))


def cmd_brainrot(chat_id):
    remain, cycle = br_status()
    send(chat_id, "🥚 *BRAINROT*\n🎮 " + BRAINROT_GAME + "\n⏱ Chu kỳ: " + str(cycle // 60) + " phút\n⚡ Spawn sau: *" + str(remain // 60) + "p " + str(remain % 60) + "s*", kb_brainrot())


def cmd_brainrot_on(chat_id, uid):
    brainrot_on(uid, chat_id)
    send(chat_id, "🔔 Đã bật!")


def cmd_brainrot_off(chat_id, uid):
    brainrot_off(uid)
    send(chat_id, "🔕 Đã tắt")


def cmd_id(chat_id, uid):
    send(chat_id, "🆔 ID: `" + str(uid) + "`")


# ============================================================
# CALLBACK
# ============================================================
def on_callback(cb):
    cb_id = cb["id"]
    data = cb.get("data", "")
    chat_id = cb["message"]["chat"]["id"]
    msg_id = cb["message"]["message_id"]
    uid = cb["from"]["id"]
    answer_cb(cb_id)

    if data == "menu_main":
        edit(chat_id, msg_id, "🤖 *MENU CHÍNH*", kb_main())
        return
    if data == "menu_check":
        edit(chat_id, msg_id, "🔍 Dùng `/check <link>`", kb_back())
        return
    if data == "menu_scan":
        edit(chat_id, msg_id, "🛰 Dùng `/scan <link>`", kb_back())
        return
    if data == "menu_tools":
        edit(chat_id, msg_id, "🛠 *CÔNG CỤ*", kb_tools())
        return
    if data == "menu_roblox":
        edit(chat_id, msg_id, "🎮 *ROBLOX*\n\n/rbxwho <u>\n/rbxplaying <u>\n/rbxgame <tên>", kb_back())
        return
    if data == "menu_brainrot":
        remain, _ = br_status()
        edit(chat_id, msg_id, "🥚 *BRAINROT*\nSpawn sau: " + str(remain // 60) + "p " + str(remain % 60) + "s", kb_brainrot())
        return
    if data == "br_on":
        brainrot_on(uid, chat_id)
        answer_cb(cb_id, "🔔 Đã bật!", True)
        return
    if data == "br_off":
        brainrot_off(uid)
        answer_cb(cb_id, "🔕 Đã tắt!", True)
        return
    if data == "br_status":
        on = db_q1("SELECT 1 FROM brainrot_sub WHERE user_id=?", (uid,)) is not None
        answer_cb(cb_id, "Trạng thái: " + ("Bật" if on else "Tắt"), True)
        return
    if data == "br_refresh":
        remain, _ = br_status()
        answer_cb(cb_id, "⏱ Còn " + str(remain // 60) + "p " + str(remain % 60) + "s", True)
        return
    if data == "act_stats":
        u = db_q1("SELECT COUNT(DISTINCT user_id) n FROM history")["n"]
        h = db_q1("SELECT COUNT(*) n FROM history")["n"]
        edit(chat_id, msg_id, "📊 *THỐNG KÊ*\n👥 Users: " + str(u) + "\n🔍 Check: " + str(h) + "\n🧵 Workers: " + str(MAX_WORKERS), kb_back())
        return
    if data == "act_history":
        rows = db_q("SELECT url,score FROM history WHERE user_id=? ORDER BY id DESC LIMIT 10", (uid,))
        if not rows:
            answer_cb(cb_id, "Chưa check lần nào.", True)
            return
        lines = ["• " + r["url"][:40] + " — " + str(r["score"]) for r in rows]
        edit(chat_id, msg_id, "🕘 *LỊCH SỬ:*\n\n" + "\n".join(lines), kb_back())
        return
    if data == "act_top":
        rows = db_q("SELECT domain, COUNT(*) n FROM reports GROUP BY domain ORDER BY n DESC LIMIT 10")
        if not rows:
            answer_cb(cb_id, "Chưa có report.", True)
            return
        lines = [str(i) + ". " + r["domain"] + " - " + str(r["n"]) for i, r in enumerate(rows, 1)]
        edit(chat_id, msg_id, "🏆 *TOP:*\n\n" + "\n".join(lines), kb_back())
        return

    helps = {
        "help_vuotlink": "🔗 Dùng: `/vuotlink <url>`\n\nVD: `/vuotlink https://link4m.com/abc`\n\n_Hoặc chỉ cần gửi link, bot tự vượt._",
        "help_ip": "📍 Dùng: `/ip shopee.vn`",
        "help_ping": "📶 Dùng: `/ping google.com`",
        "help_short": "🔗 Dùng: `/short <url>`",
        "help_qr": "🔳 Dùng: `/qr Hello`",
        "help_weather": "🌤 Dùng: `/weather Hà Nội`",
    }
    if data in helps:
        edit(chat_id, msg_id, helps[data], kb_back())
        return


# ============================================================
# POLLING LOOP
# ============================================================
def handle_update(update):
    try:
        if "message" in update:
            msg = update["message"]
            chat_id = msg["chat"]["id"]
            uid = msg["from"]["id"]
            text = msg.get("text", "").strip()
            if is_banned(uid):
                send(chat_id, "🚫 Bạn đã bị cấm")
                return
            if not text.startswith("/"):
                # Nếu là link → tự vượt
                if text.startswith(("http://", "https://")) or any(
                        text.endswith(t) for t in (".com", ".vn", ".net", ".org",
                                                     ".io", ".me", ".ly", ".link",
                                                     ".top", ".xyz", ".cc", ".tv")):
                    cmd_vuotlink(chat_id, uid, [text])
                return
            parts = text.split()
            cmd = parts[0].split("@")[0].lower()
            args = parts[1:]
            if cmd == "/start" or cmd == "/help":
                cmd_start(chat_id, uid)
            elif cmd == "/check":
                cmd_check(chat_id, uid, args)
            elif cmd == "/scan":
                cmd_scan(chat_id, uid, args)
            elif cmd == "/vuotlink":
                cmd_vuotlink(chat_id, uid, args)
            elif cmd == "/ip":
                cmd_ip(chat_id, args)
            elif cmd == "/ping":
                cmd_ping(chat_id, args)
            elif cmd == "/short":
                cmd_short(chat_id, args)
            elif cmd == "/qr":
                cmd_qr(chat_id, args)
            elif cmd == "/weather":
                cmd_weather(chat_id, args)
            elif cmd == "/stats":
                cmd_stats(chat_id)
            elif cmd == "/id":
                cmd_id(chat_id, uid)
            elif cmd == "/rbxwho":
                cmd_rbxwho(chat_id, args)
            elif cmd == "/rbxplaying":
                cmd_rbxplaying(chat_id, args)
            elif cmd == "/rbxgame":
                cmd_rbxgame(chat_id, args)
            elif cmd == "/brainrot":
                cmd_brainrot(chat_id)
            elif cmd == "/brainrot_on":
                cmd_brainrot_on(chat_id, uid)
            elif cmd == "/brainrot_off":
                cmd_brainrot_off(chat_id, uid)
        elif "callback_query" in update:
            on_callback(update["callback_query"])
    except Exception as e:
        print("Handle error:", e)


def main():
    db_init()
    threading.Thread(target=br_watcher, daemon=True).start()
    print("Bot started. Polling...")
    offset = 0
    while True:
        try:
            r = _session.get(API + "/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
            if r.status_code != 200:
                print("getUpdates:", r.status_code)
                time.sleep(5)
                continue
            data = r.json()
            if not data.get("ok"):
                time.sleep(5)
                continue
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                threading.Thread(target=handle_update, args=(upd,), daemon=True).start()
        except Exception as e:
            print("Poll error:", type(e).__name__, str(e)[:150])
            time.sleep(5)


if __name__ == "__main__":
    main()
