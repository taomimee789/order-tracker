"""
Order Tracker - Web App (Flask)
แปลงจาก Desktop App (tkinter) → เว็บแอพ
ใช้งานได้ทุกที่ ทั้งมือถือและคอมพิวเตอร์
"""

from flask import Flask, request, jsonify, render_template_string, session
import sqlite3, json, re, imaplib, email, threading, os, time, webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from email.header import decode_header
from email.utils import parsedate_to_datetime
from functools import wraps

# ─── ตั้ง Timezone เป็นเวลาไทย (UTC+7) ──────────────────────────────────
# วิธี 1: ผ่าน OS (Linux/Mac) — อาจไม่ทำงานบน Windows/บาง container
os.environ["TZ"] = "Asia/Bangkok"
try:
    time.tzset()
except AttributeError:
    pass

# วิธี 2: บังคับ timezone ผ่าน Python โดยตรง (ทำงานทุก OS)
_BKK_TZ = timezone(timedelta(hours=7))

def _now_bkk():
    """_now_bkk() เวลาไทย — ใช้แทน _now_bkk() ทุกที่"""
    return datetime.now(_BKK_TZ)

def _ts_to_bkk(ts):
    """แปลง Unix timestamp → datetime เวลาไทย"""
    return datetime.fromtimestamp(int(ts), tz=_BKK_TZ)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-to-something-random-123")

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).resolve().parent
DATA_DIR  = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DB_PATH   = DATA_DIR / "orders.db"
SETTINGS_FILE = DATA_DIR / "settings.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ─── Auth (Google Apps Script — shared with Tao Toolbox) ─────────────────
AUTH_GS_URL = "https://script.google.com/macros/s/AKfycbw4Ls62u-ztk8Ulru2tpgTznFX9ooHXCS3PX8XZzzxE9urpAsuvAQWIbXAPENM7H0M/exec"
GLOBAL_LICENSE_VALID = False
GLOBAL_LICENSE_EXPIRES = None
GLOBAL_LICENSE_MSG = ""
GLOBAL_HWID = ""
CURRENT_VERSION = "1.0.0"

def _init_license():
    """ตรวจ license ตอน startup"""
    global GLOBAL_LICENSE_VALID, GLOBAL_LICENSE_EXPIRES, GLOBAL_LICENSE_MSG, GLOBAL_HWID
    try:
        from license_verify import verify_license_file, get_system_hwid, _find_license_file
        GLOBAL_HWID = get_system_hwid() or ""
        lic_path = _find_license_file()
        if lic_path and GLOBAL_HWID:
            ok, msg, exp = verify_license_file(lic_path, GLOBAL_HWID)
            if ok:
                GLOBAL_LICENSE_VALID = True
                GLOBAL_LICENSE_EXPIRES = exp
                GLOBAL_LICENSE_MSG = msg
                print(f"  🔐 License: {msg} (expires {exp})")
            else:
                GLOBAL_LICENSE_MSG = msg
                print(f"  ⚠️  License: {msg}")
        else:
            print(f"  ⚠️  License: ไม่พบไฟล์คีย์ — HWID: {GLOBAL_HWID or 'N/A'}")
            GLOBAL_LICENSE_MSG = "ไม่พบคีย์"
    except ImportError:
        print("  ℹ️  license_verify.py not found — license check skipped")
        GLOBAL_LICENSE_VALID = True  # dev mode
        GLOBAL_LICENSE_MSG = "dev mode"
    except Exception as e:
        print(f"  ⚠️  License check error: {e}")
        GLOBAL_LICENSE_MSG = str(e)

sync_lock = threading.Lock()
sync_status = {
    "running":      False,
    "log":          [],
    "last_sync":    None,
    "auto_enabled": True,   # เปิด auto-sync ตอนเริ่มต้น
    "auto_interval": 60,    # วินาที (default 60 = 1 นาที)
    "next_sync":    None,
    "auto_new":     0,
    "auto_upd":     0,
}

# ══════════════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════════════

def normalize_thb(amount):
    if not amount: return ""
    a = str(amount).strip().replace("฿","").replace(",","").strip()
    if not a: return ""
    if "." in a:
        left, right = a.split(".", 1)
        right = (right + "00")[:2]
        a = f"{left}.{right}"
    else:
        a = f"{a}.00"
    return f"฿{a}"


def status_rank(status):
    ranks = {
        "รอดำเนินการ":0,"กำลังเตรียม":1,"รอจัดส่ง":2,
        "กำลังจัดส่ง":3,"จัดส่งแล้ว":4,
        "จัดส่งเสร็จสิ้นแล้ว":5,"ส่งสำเร็จ":5,"ยกเลิก":4,
    }
    return ranks.get((status or "").strip(), 0)


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text("utf-8"))
    except Exception:
        return {}


def save_settings(data):
    SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


# ══════════════════════════════════════════════════════════════════════════════
#  Order dataclass (dict-based for simplicity)
# ══════════════════════════════════════════════════════════════════════════════

class Order:
    __slots__ = ("order_id","shop","merchant","status","products","date",
                 "total","tracking","first_seen_ts","last_update_ts","account","ship_area")

    def __init__(self, order_id, shop, status, products, date, total,
                 merchant="", tracking=None, first_seen_ts=None,
                 last_update_ts=None, account="", ship_area=""):
        self.order_id = order_id
        self.shop = shop
        self.merchant = merchant
        self.status = status
        self.products = products or []
        self.date = date
        self.total = total
        self.tracking = tracking
        self.first_seen_ts = first_seen_ts
        self.last_update_ts = last_update_ts
        self.account = account
        self.ship_area = ship_area

    def to_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in cls.__slots__})


# ══════════════════════════════════════════════════════════════════════════════
#  SQLite Store
# ══════════════════════════════════════════════════════════════════════════════

class SQLiteOrderStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_db(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    shop TEXT, merchant TEXT, status TEXT,
                    products_json TEXT, order_date TEXT, total TEXT,
                    tracking TEXT, first_seen_ts INTEGER,
                    last_update_ts INTEGER, account TEXT,
                    ship_area TEXT
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_fs ON orders(first_seen_ts);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_st ON orders(status);")
            # migrate: เพิ่ม column ให้ DB เก่าที่ยังไม่มี
            try:
                conn.execute("ALTER TABLE orders ADD COLUMN ship_area TEXT")
            except Exception:
                pass  # column มีอยู่แล้ว

    def _row_to_order(self, row):
        try: products = json.loads(row["products_json"] or "[]")
        except: products = []
        return Order(
            order_id=row["order_id"], shop=row["shop"],
            merchant=row["merchant"] or "", status=row["status"] or "",
            products=products, date=row["order_date"] or "",
            total=row["total"] or "", tracking=row["tracking"],
            first_seen_ts=row["first_seen_ts"],
            last_update_ts=row["last_update_ts"],
            account=row["account"] if "account" in row.keys() else "",
            ship_area=row["ship_area"] if "ship_area" in row.keys() else "",
        )

    def _upsert_merge(self, existing: Order, incoming: Order):
        inc = (incoming.status or "").strip()
        cur = (existing.status or "").strip()
        if "ยกเลิก" in inc and "เสร็จสิ้น" not in cur and "สำเร็จ" not in cur:
            existing.status = inc
        elif status_rank(incoming.status) >= status_rank(existing.status):
            existing.status = incoming.status or existing.status
        if not existing.merchant and incoming.merchant:
            existing.merchant = incoming.merchant
        if incoming.products and (not existing.products or len(str(existing.products[0])) > 120):
            existing.products = incoming.products
        if incoming.total and _to_amount(incoming.total) > 0:
            existing.total = incoming.total
        if incoming.tracking:
            existing.tracking = incoming.tracking
        if incoming.date:
            existing.date = incoming.date
        if incoming.first_seen_ts is not None:
            existing.first_seen_ts = min(int(existing.first_seen_ts or incoming.first_seen_ts),
                                         int(incoming.first_seen_ts))
        if incoming.last_update_ts is not None:
            existing.last_update_ts = max(int(existing.last_update_ts or 0),
                                          int(incoming.last_update_ts))
        if incoming.ship_area and not existing.ship_area:
            existing.ship_area = incoming.ship_area

    def upsert(self, incoming: Order):
        if not incoming or not incoming.order_id:
            return False
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id=?",
                               (incoming.order_id,)).fetchone()
            if row:
                existing = self._row_to_order(row)
                self._upsert_merge(existing, incoming)
                conn.execute("""
                    UPDATE orders SET shop=?,merchant=?,status=?,products_json=?,
                    order_date=?,total=?,tracking=?,first_seen_ts=?,last_update_ts=?,ship_area=?
                    WHERE order_id=?""",
                    (existing.shop, existing.merchant, existing.status,
                     json.dumps(existing.products, ensure_ascii=False),
                     existing.date, existing.total, existing.tracking,
                     existing.first_seen_ts, existing.last_update_ts,
                     existing.ship_area, existing.order_id))
            else:
                conn.execute("""
                    INSERT INTO orders(order_id,shop,merchant,status,products_json,
                    order_date,total,tracking,first_seen_ts,last_update_ts,account,ship_area)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (incoming.order_id, incoming.shop, incoming.merchant, incoming.status,
                     json.dumps(incoming.products, ensure_ascii=False),
                     incoming.date, incoming.total, incoming.tracking,
                     incoming.first_seen_ts, incoming.last_update_ts,
                     incoming.account or "", incoming.ship_area or ""))
        return True

    def update_fields(self, order_id, fields):
        if not order_id or not fields:
            return False
        allowed = {"status","tracking","merchant","total","account","ship_area"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return False
        with self._connect() as conn:
            cols = ", ".join(f"{k}=?" for k in sets)
            conn.execute(f"UPDATE orders SET {cols} WHERE order_id=?",
                         (*sets.values(), order_id))
        return True

    def delete(self, order_id):
        with self._connect() as conn:
            conn.execute("DELETE FROM orders WHERE order_id=?", (order_id,))

    def clear_all(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM orders")

    def get(self, order_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM orders WHERE order_id=?",
                               (order_id,)).fetchone()
            return self._row_to_order(row) if row else None

    def list_all(self, day=None, search=None, status_filter=None,
                 date_from=None, date_to=None, account=None):
        sql = "SELECT * FROM orders WHERE 1=1"
        params = []
        if day:
            sql += " AND date(COALESCE(last_update_ts,first_seen_ts),'unixepoch','+7 hours')=?"
            params.append(day)
        elif date_from and date_to:
            sql += """ AND date(COALESCE(last_update_ts,first_seen_ts),'unixepoch','+7 hours')>=?
                       AND date(COALESCE(last_update_ts,first_seen_ts),'unixepoch','+7 hours')<=?"""
            params += [date_from, date_to]
        if account and account != "ทุกบัญชี":
            sql += " AND account=?"
            params.append(account)
        if status_filter and status_filter != "ทั้งหมด":
            if status_filter == "ค้างนาน":
                threshold = int(_now_bkk().timestamp()) - 72*3600
                sql += """ AND first_seen_ts<=?
                           AND status NOT LIKE '%ยกเลิก%' AND status NOT LIKE '%เสร็จสิ้น%' AND status NOT LIKE '%สำเร็จ%' AND status NOT LIKE '%มาถึงแล้ว%'"""
                params.append(threshold)
            else:
                sql += " AND status LIKE ?"
                params.append(f"%{status_filter}%")
        if search:
            sql += """ AND (order_id LIKE ? OR shop LIKE ? OR merchant LIKE ?
                            OR products_json LIKE ? OR tracking LIKE ?)"""
            s = f"%{search}%"
            params += [s, s, s, s, s]
        sql += " ORDER BY COALESCE(last_update_ts,first_seen_ts) DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_order(r) for r in rows]

    def list_accounts(self):
        """ดึง list ของบัญชีอีเมลที่มีออเดอร์อยู่จริงใน DB"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT account FROM orders WHERE account IS NOT NULL AND account != '' ORDER BY account"
            ).fetchall()
            return [r["account"] for r in rows]

    def list_days(self):
        with self._connect() as conn:
            cur = conn.execute("""
                SELECT DISTINCT date(COALESCE(last_update_ts,first_seen_ts),'unixepoch','+7 hours') AS day
                FROM orders WHERE COALESCE(last_update_ts,first_seen_ts) IS NOT NULL
                ORDER BY day DESC""")
            return [r["day"] for r in cur.fetchall() if r["day"]]

    def stats(self, alert_hours=72, date_from=None, date_to=None, overdue_all_time=False):
        threshold = int(_now_bkk().timestamp()) - alert_hours * 3600
        where_day = ""
        params = []
        if date_from and date_to:
            where_day = " AND date(COALESCE(last_update_ts,first_seen_ts),'unixepoch','+7 hours')>=? AND date(COALESCE(last_update_ts,first_seen_ts),'unixepoch','+7 hours')<=?"
            params = [date_from, date_to]
        elif date_from:
            where_day = " AND date(COALESCE(last_update_ts,first_seen_ts),'unixepoch','+7 hours')=?"
            params = [date_from]
        elif date_to:
            where_day = " AND date(COALESCE(last_update_ts,first_seen_ts),'unixepoch','+7 hours')<=?"
            params = [date_to]
        with self._connect() as conn:
            def q(sql, *p):
                return conn.execute(sql, p).fetchone()[0]
            w = where_day
            # overdue: count ALL-time unless overdue_all_time=False (then apply date filter)
            if overdue_all_time:
                overdue_q = "first_seen_ts<=? AND status NOT LIKE '%ยกเลิก%' AND status NOT LIKE '%เสร็จสิ้น%' AND status NOT LIKE '%สำเร็จ%' AND status NOT LIKE '%มาถึงแล้ว%'"
                overdue_params = [threshold]
            else:
                overdue_q = f"first_seen_ts<=? AND status NOT LIKE '%ยกเลิก%' AND status NOT LIKE '%เสร็จสิ้น%' AND status NOT LIKE '%สำเร็จ%' AND status NOT LIKE '%มาถึงแล้ว%'{w}"
                overdue_params = [threshold] + params
            return {
                "total":       q(f"SELECT COUNT(*) FROM orders WHERE 1=1{w}", *params),
                "cancelled":   q(f"SELECT COUNT(*) FROM orders WHERE status LIKE '%ยกเลิก%'{w}", *params),
                "delivered":   q(f"SELECT COUNT(*) FROM orders WHERE (status LIKE '%เสร็จสิ้น%' OR status LIKE '%สำเร็จ%' OR status LIKE '%มาถึงแล้ว%'){w}", *params),
                "in_transit":  q(f"SELECT COUNT(*) FROM orders WHERE (status LIKE '%กำลังจัดส่ง%' OR status='จัดส่งแล้ว') AND status NOT LIKE '%ยกเลิก%'{w}", *params),
                "overdue":     q(f"SELECT COUNT(*) FROM orders WHERE {overdue_q}", *overdue_params),
                "preparing":   q(f"SELECT COUNT(*) FROM orders WHERE status LIKE '%กำลังเตรียม%'{w}", *params),
                "today_total": q("SELECT COUNT(*) FROM orders WHERE date(COALESCE(last_update_ts,first_seen_ts),'unixepoch','+7 hours')=date('now','+7 hours')"),
            }


store = SQLiteOrderStore(DB_PATH)

# ══════════════════════════════════════════════════════════════════════════════
#  Telegram System  (ported from telegram_module.py + app.py)
# ══════════════════════════════════════════════════════════════════════════════

import urllib.parse
import urllib.request
import urllib.error

tg_state = {
    "enabled": False,
    "bot_token": "",
    "chat_id": "",
    "immediate_enabled": True,
    "alert_imap_error": True,
    "alert_sync_fail": True,
    "sync_fail_threshold": 3,
    "alert_login_fail": True,
    "alert_order_anomaly": True,
    "alert_delivered_order": True,
    "order_low_threshold": 20,
    "order_high_threshold": 300,
    "digest_enabled": False,
    "digest_mode": "ทุก 1 ชั่วโมง",
    "digest_show_products": False,
    "digest_only_on_movement": False,
    "dedup_minutes": 30,
    "notify_recovery": True,
    "quiet_hours_enabled": False,
    "quiet_start": "23:00",
    "quiet_end": "08:00",
    "last_digest_ts": 0,
    "last_update_id": 0,
    "last_event_sent": {},
    "digest_acc": {"scanned": 0, "new": 0, "updated": 0, "errors": 0, "cycles": 0},
    "period_stats": {},
    "pending_action": {},
    "chat_state": {},
    "product_checks": {},
    "sync_fail_streak": 0,
    "alert_hours": 48,
}


def _tg_load():
    """Load telegram settings from settings.json into tg_state."""
    settings = load_settings()
    tg = settings.get("โหมดเตือนเทเลแกรม", {}) or {}
    immediate = tg.get("แจ้งเตือนทันที", {}) or {}
    digest = tg.get("สรุปรายรอบ", {}) or {}
    dedup = tg.get("กันสแปม", {}) or {}
    quiet = tg.get("ช่วงเงียบ", {}) or {}

    tg_state["enabled"] = bool(tg.get("เปิดใช้งาน", False))
    tg_state["bot_token"] = str(tg.get("บอทโทเคน", "") or "").strip()
    tg_state["chat_id"] = str(tg.get("แชทไอดี", "") or "").strip()

    tg_state["immediate_enabled"] = bool(immediate.get("เปิดใช้งาน", True))
    tg_state["alert_imap_error"] = bool(immediate.get("เตือนเมื่อเชื่อมต่ออีเมลมีปัญหา", True))
    tg_state["alert_sync_fail"] = bool(immediate.get("เตือนเมื่อซิงก์ล้มเหลวต่อเนื่อง", True))
    tg_state["sync_fail_threshold"] = int(immediate.get("จำนวนครั้งซิงก์ล้มเหลวติดกันก่อนเตือน", 3) or 3)
    tg_state["alert_login_fail"] = bool(immediate.get("เตือนเมื่อเข้าสู่ระบบอีเมลไม่ผ่าน", True))
    tg_state["alert_order_anomaly"] = bool(immediate.get("เตือนเมื่อปิดพัสดุผิดปกติ (รายค่าย)",
                                           immediate.get("เตือนเมื่อจำนวนออเดอร์ผิดปกติ", True)))
    tg_state["alert_delivered_order"] = bool(immediate.get("แจ้งเตือนจัดส่งเสร็จสิ้นทีละรายการ", True))
    tg_state["order_low_threshold"] = int(immediate.get("ขั้นต่ำปกติปิดพัสดุต่อค่าย",
                                          immediate.get("ขั้นต่ำออเดอร์ต่อรอบ", 20)) or 20)
    tg_state["order_high_threshold"] = int(immediate.get("สูงสุดปกติปิดพัสดุต่อค่าย",
                                           immediate.get("สูงสุดออเดอร์ต่อรอบ", 300)) or 300)

    tg_state["digest_enabled"] = bool(digest.get("เปิดใช้งาน", True))
    tg_state["digest_mode"] = str(digest.get("ความถี่", "ทุก 1 ชั่วโมง") or "ทุก 1 ชั่วโมง")
    tg_state["digest_show_products"] = bool(digest.get("แสดงรายชื่อสินค้า", False))
    tg_state["digest_only_on_movement"] = bool(digest.get("ส่งเฉพาะเมื่อมีการเคลื่อนไหว", False))

    tg_state["dedup_minutes"] = int(dedup.get("นาทีขั้นต่ำก่อนแจ้งเหตุเดิมซ้ำ", 30) or 30)
    tg_state["notify_recovery"] = bool(dedup.get("แจ้งเมื่อระบบกลับมาปกติ", True))

    tg_state["quiet_hours_enabled"] = bool(quiet.get("เปิดใช้งาน", False))
    tg_state["quiet_start"] = str(quiet.get("เวลาเริ่ม", "23:00") or "23:00")
    tg_state["quiet_end"] = str(quiet.get("เวลาสิ้นสุด", "08:00") or "08:00")

    tg_state["last_digest_ts"] = int(settings.get("tg_last_digest_ts", 0) or 0)
    tg_state["last_update_id"] = int(settings.get("tg_last_update_id", 0) or 0)
    tg_state["period_stats"] = dict(settings.get("tg_period_stats", {}) or {})
    tg_state["product_checks"] = dict(settings.get("tg_product_checks", {}) or {})
    tg_state["alert_hours"] = int(settings.get("alert_hours", 48) or 48)
    tg_state["chat_state"] = dict(settings.get("tg_chat_state", {}) or {})


def _tg_save():
    """Persist telegram runtime state back to settings.json."""
    settings = load_settings()
    settings["tg_last_digest_ts"] = int(tg_state["last_digest_ts"] or 0)
    settings["tg_last_update_id"] = int(tg_state["last_update_id"] or 0)
    settings["tg_period_stats"] = dict(tg_state["period_stats"] or {})
    settings["tg_product_checks"] = dict(tg_state["product_checks"] or {})
    settings["tg_chat_state"] = dict(tg_state["chat_state"] or {})
    settings["โหมดเตือนเทเลแกรม"] = {
        "เปิดใช้งาน": bool(tg_state["enabled"]),
        "บอทโทเคน": tg_state["bot_token"],
        "แชทไอดี": tg_state["chat_id"],
        "แจ้งเตือนทันที": {
            "เปิดใช้งาน": bool(tg_state["immediate_enabled"]),
            "เตือนเมื่อเชื่อมต่ออีเมลมีปัญหา": bool(tg_state["alert_imap_error"]),
            "เตือนเมื่อซิงก์ล้มเหลวต่อเนื่อง": bool(tg_state["alert_sync_fail"]),
            "จำนวนครั้งซิงก์ล้มเหลวติดกันก่อนเตือน": int(tg_state["sync_fail_threshold"] or 3),
            "เตือนเมื่อเข้าสู่ระบบอีเมลไม่ผ่าน": bool(tg_state["alert_login_fail"]),
            "เตือนเมื่อปิดพัสดุผิดปกติ (รายค่าย)": bool(tg_state["alert_order_anomaly"]),
            "แจ้งเตือนจัดส่งเสร็จสิ้นทีละรายการ": bool(tg_state["alert_delivered_order"]),
            "ขั้นต่ำปกติปิดพัสดุต่อค่าย": int(tg_state["order_low_threshold"] or 20),
            "สูงสุดปกติปิดพัสดุต่อค่าย": int(tg_state["order_high_threshold"] or 300),
        },
        "สรุปรายรอบ": {
            "เปิดใช้งาน": bool(tg_state["digest_enabled"]),
            "ความถี่": tg_state["digest_mode"],
            "แสดงรายชื่อสินค้า": bool(tg_state["digest_show_products"]),
            "ส่งเฉพาะเมื่อมีการเคลื่อนไหว": bool(tg_state["digest_only_on_movement"]),
        },
        "กันสแปม": {
            "นาทีขั้นต่ำก่อนแจ้งเหตุเดิมซ้ำ": int(tg_state["dedup_minutes"] or 30),
            "แจ้งเมื่อระบบกลับมาปกติ": bool(tg_state["notify_recovery"]),
        },
        "ช่วงเงียบ": {
            "เปิดใช้งาน": bool(tg_state["quiet_hours_enabled"]),
            "เวลาเริ่ม": tg_state["quiet_start"],
            "เวลาสิ้นสุด": tg_state["quiet_end"],
        },
    }
    save_settings(settings)


# ─── Status helpers (from app.py) ───────────────────────────────────────────

def _is_must_check_status(st):
    return "ต้องเช็ค" in (st or "")

def _is_delivered_status(st):
    s = (st or "")
    return ("เสร็จสิ้น" in s) or ("สำเร็จ" in s) or ("มาถึงแล้ว" in s)

def _is_in_transit_status(st):
    s = (st or "")
    return ("กำลังจัดส่ง" in s) or ("อยู่ระหว่างการขนส่ง" in s) or (s == "จัดส่งแล้ว") or ("ขนส่ง" in s)

def _is_cancelled_status(st):
    return "ยกเลิก" in (st or "")

def _is_closed_status(st):
    return (not _is_cancelled_status(st)) and _is_delivered_status(st)

def _to_amount(val):
    s = str(val or "").strip()
    if not s:
        return 0.0
    s = s.replace("฿", "").replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return 0.0
    try:
        return float(m.group(0))
    except Exception:
        return 0.0

def _short_product_name(name):
    s = re.sub(r"\s+", " ", str(name or "").strip())
    if not s:
        return ""
    cuts = [" ยกลัง", " ลัง ", " แพ็ค", " สูตร", " ขนาด", " สี", " กลิ่น", " | ", ","]
    for token in cuts:
        idx = s.find(token)
        if idx > 8:
            s = s[:idx].strip()
            break
    return s[:42].strip()

def _carrier_name(tracking):
    t = str(tracking or "").strip().lower()
    if not t:
        return "ไม่ระบุขนส่ง"
    if "flash" in t:
        return "Flash"
    if "j&t" in t or "j&t" in t.replace(" ", "") or "jt" in t or "jnt" in t:
        return "J&T"
    if "kerry" in t or "kex" in t:
        return "Kerry/KEX"
    if "ninja" in t:
        return "Ninja Van"
    if "spx" in t or "shopee express" in t:
        return "SPX"
    if "best" in t:
        return "Best"
    return "อื่นๆ"


def _tracking_url(tracking):
    """สร้าง URL สำหรับเช็คพัสดุ — ใช้ 17TRACK รองรับทุกขนส่ง"""
    num = _extract_tracking_number(str(tracking or ""))
    if not num or num == "-":
        return ""
    return f"https://t.17track.net/en#nums={num}"


# ══════════════════════════════════════════════════════════════════════════════
#  Smart ETA — เรียนรู้ระยะเวลาจัดส่งจริงจากประวัติ แต่ละร้าน
# ══════════════════════════════════════════════════════════════════════════════

_eta_cache = {"data": None, "ts": 0}  # cache ไม่ต้องคำนวณทุกครั้ง

def _compute_shop_eta():
    """สแกนออเดอร์ที่ปิดงานแล้ว คำนวณค่าเฉลี่ยวันจัดส่งแต่ละ merchant (ร้านค้าย่อย)
    fallback เป็น shop ถ้าไม่มี merchant"""
    now_ts = int(_now_bkk().timestamp())
    # ใช้ cache 5 นาที
    if _eta_cache["data"] and (now_ts - _eta_cache["ts"]) < 300:
        return _eta_cache["data"]

    merchant_stats = {}
    with store._connect() as conn:
        rows = conn.execute("""
            SELECT shop, merchant, first_seen_ts, last_update_ts FROM orders
            WHERE (status LIKE '%เสร็จสิ้น%' OR status LIKE '%สำเร็จ%' OR status LIKE '%มาถึงแล้ว%')
              AND first_seen_ts IS NOT NULL AND last_update_ts IS NOT NULL
              AND last_update_ts > first_seen_ts
        """).fetchall()

    for r in rows:
        # ใช้ merchant เป็นหลัก, fallback เป็น shop
        key = (r["merchant"] or "").strip() or (r["shop"] or "").strip()
        if not key:
            continue
        days = (int(r["last_update_ts"]) - int(r["first_seen_ts"])) / 86400.0
        if days < 0.1 or days > 30:  # กรองค่าผิดปกติ
            continue
        rec = merchant_stats.setdefault(key, {"total_days": 0, "count": 0})
        rec["total_days"] += days
        rec["count"] += 1

    result = {}
    for key, rec in merchant_stats.items():
        if rec["count"] >= 2:  # ต้องมีอย่างน้อย 2 ออเดอร์ถึงจะเชื่อถือได้
            avg = rec["total_days"] / rec["count"]
            result[key] = {
                "avg_days": round(avg, 1),
                "count": rec["count"],
                "rounded": max(1, round(avg)),  # ปัดเป็นจำนวนเต็มสำหรับ ETA
            }

    _eta_cache["data"] = result
    _eta_cache["ts"] = now_ts
    return result


def _get_eta_days(order):
    """คืนจำนวนวัน ETA — ลำดับ: merchant → shop → ค่ากลาง"""
    learned = _compute_shop_eta()
    merchant = (order.merchant or "").strip()
    if merchant and merchant in learned:
        return learned[merchant]["rounded"]
    shop = (order.shop or "").strip()
    if shop and shop in learned:
        return learned[shop]["rounded"]
    return int(load_settings().get("eta_days", 2))


# ─── Orders helper ──────────────────────────────────────────────────────────

def _get_orders_for_range(start_day, end_day, account=""):
    """ดึงออเดอร์ในช่วงวัน (ใช้ store.list_all)"""
    try:
        if start_day and end_day:
            if start_day == end_day:
                orders = store.list_all(day=start_day)
            else:
                orders = store.list_all(date_from=start_day, date_to=end_day)
        else:
            orders = store.list_all()
        if account:
            orders = [o for o in orders if (o.account or "") == account]
        return list(orders or [])
    except Exception:
        return []


# ─── Telegram API ───────────────────────────────────────────────────────────

def _tg_api_get(method, params=None, timeout=12):
    params = params or {}
    q = urllib.parse.urlencode(params)
    url = f"https://api.telegram.org/bot{tg_state['bot_token']}/{method}"
    if q:
        url = f"{url}?{q}"
    with urllib.request.urlopen(url, timeout=max(5, int(timeout or 12))) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw)

def _tg_api_post(method, payload=None):
    payload = payload or {}
    url = f"https://api.telegram.org/bot{tg_state['bot_token']}/{method}"
    body = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=12) as resp:
        raw = resp.read().decode("utf-8", errors="ignore")
    return json.loads(raw) if raw else {"ok": True}

def _tg_send(text, chat_id=None, reply_markup=None):
    if (not tg_state["enabled"]) or (not tg_state["bot_token"]) or (not (chat_id or tg_state["chat_id"])):
        return False
    if _tg_is_quiet() and str(chat_id or tg_state["chat_id"]) == str(tg_state["chat_id"]):
        return False
    max_retries = 3
    for attempt in range(max_retries):
        try:
            payload = {
                "chat_id": str(chat_id or tg_state["chat_id"]),
                "text": text,
                "disable_web_page_preview": "true",
            }
            if reply_markup:
                payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
            _tg_api_post("sendMessage", payload)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Rate limited — อ่าน retry_after แล้วรอ
                retry_after = 5
                try:
                    body = json.loads(e.read().decode("utf-8", errors="ignore"))
                    retry_after = int(body.get("parameters", {}).get("retry_after", 5))
                except Exception:
                    pass
                slog(f"⚠️ Telegram 429 rate limit — รอ {retry_after} วินาที (ครั้งที่ {attempt+1})")
                time.sleep(retry_after + 1)
                continue  # retry
            else:
                slog(f"⚠️ Telegram HTTP {e.code}: {e}")
                return False
        except Exception as e:
            slog(f"⚠️ Telegram ส่งไม่สำเร็จ: {e}")
            return False
    slog("❌ Telegram ส่งไม่สำเร็จหลัง retry 3 ครั้ง")
    return False


# ─── Quiet hours / Digest timing ────────────────────────────────────────────

def _tg_is_quiet():
    if not tg_state["quiet_hours_enabled"]:
        return False
    try:
        now = _now_bkk()
        ch, cm = [int(x) for x in str(tg_state["quiet_start"]).split(":", 1)]
        eh, em = [int(x) for x in str(tg_state["quiet_end"]).split(":", 1)]
        cur = now.hour * 60 + now.minute
        st = ch * 60 + cm
        en = eh * 60 + em
        if st == en:
            return False
        if st < en:
            return st <= cur < en
        return (cur >= st) or (cur < en)
    except Exception:
        return False

def _tg_digest_seconds():
    m = {
        "ไม่ส่ง": 0, "ทุก 1 นาที": 60, "ทุก 5 นาที": 300,
        "ทุก 15 นาที": 900, "ทุก 30 นาที": 1800,
        "ทุก 1 ชั่วโมง": 3600, "ทุก 6 ชั่วโมง": 21600,
        "วันละ 2 ครั้ง (เช้า/เย็น)": 43200, "วันละครั้ง": 86400,
    }
    return int(m.get(tg_state["digest_mode"], 3600))


# ─── Dedup / Notify ─────────────────────────────────────────────────────────

def _tg_notify_event(key, text, force=False):
    now = int(_now_bkk().timestamp())
    if not force:
        last = int(tg_state["last_event_sent"].get(key, 0) or 0)
        if (now - last) < (max(1, int(tg_state["dedup_minutes"] or 30)) * 60):
            return False
    ok = _tg_send(text)
    if ok:
        tg_state["last_event_sent"][key] = now
    return ok


# ─── Digest & summary builders ──────────────────────────────────────────────

def _tg_build_digest_product_lines(orders):
    if not tg_state["digest_show_products"]:
        return []
    carrier_map = {}
    for o in (orders or []):
        try:
            if not _is_closed_status(o.status):
                continue
            names = list(o.products or [])
            if not names:
                continue
            carrier = _carrier_name(getattr(o, "tracking", ""))
            key = _short_product_name(names[0]) or "สินค้าไม่ระบุชื่อ"
            cmap = carrier_map.setdefault(carrier, {})
            item = cmap.setdefault(key, {"orders": 0, "total": 0})
            item["orders"] += 1
            item["total"] += _to_amount(o.total)
        except Exception:
            continue
    if not carrier_map:
        return []
    lines = ["📋 สินค้าที่ปิดงาน"]
    for carrier in sorted(carrier_map.keys()):
        ranked = sorted(carrier_map[carrier].items(), key=lambda kv: (-int(kv[1]["orders"]), kv[0]))
        if not ranked:
            continue
        carrier_total = sum(int(info.get("orders", 0) or 0) for _, info in ranked)
        carrier_rev = sum(info.get("total", 0) for _, info in ranked)
        lines.append(f"🚚 [{carrier}] {carrier_total} ชิ้น (฿{carrier_rev:,.0f})")
        for name, info in ranked:
            qty = f" ── {int(info['orders'])} รายการ" if info['orders'] > 1 else ""
            price = f"฿{info['total']:,.0f}" if info['total'] else ""
            lines.append(f"  • {name} ({price}){qty}")
    return lines

def _tg_build_digest_cancelled_lines(orders):
    if not tg_state["digest_show_products"]:
        return []
    product_map = {}
    for o in (orders or []):
        try:
            if not _is_cancelled_status(o.status):
                continue
            names = list(o.products or [])
            key = _short_product_name(names[0] if names else "") or "สินค้าไม่ระบุชื่อ"
            item = product_map.setdefault(key, {"orders": 0, "total": 0})
            item["orders"] += 1
            item["total"] += _to_amount(o.total)
        except Exception:
            continue
    if not product_map:
        return []
    ranked = sorted(product_map.items(), key=lambda kv: (-int(kv[1]["orders"]), kv[0]))
    total_rev = sum(info["total"] for _, info in ranked)
    lines = [f"⛔ สินค้าที่ยกเลิก (฿{total_rev:,.0f})"]
    for name, info in ranked:
        qty = f" ── {int(info['orders'])} รายการ" if info['orders'] > 1 else ""
        price = f"฿{info['total']:,.0f}" if info['total'] else ""
        lines.append(f"  • {name} ({price}){qty}")
    return lines

def _tg_bucket_4hour_key(ts=None):
    dt = _ts_to_bkk(ts) if ts is not None else _now_bkk()
    start_hour = (int(dt.hour) // 4) * 4
    return f"{dt.strftime('%Y-%m-%d')}|{start_hour:02d}"

def _tg_backfill_today_period_stats(day):
    try:
        prefix = f"{day}|"
        if any(str(k).startswith(prefix) for k in (tg_state["period_stats"] or {}).keys()):
            return
        orders = _get_orders_for_range(day, day)
        buckets = {}
        for o in (orders or []):
            ts = int(getattr(o, "last_update_ts", None) or getattr(o, "first_seen_ts", None) or 0)
            if ts <= 0:
                continue
            dt = _ts_to_bkk(ts)
            if dt.strftime("%Y-%m-%d") != day:
                continue
            key = _tg_bucket_4hour_key(ts)
            rec = buckets.setdefault(key, {"shipping": 0, "closed": 0, "cancelled": 0})
            st = str(getattr(o, "status", "") or "")
            if _is_cancelled_status(st):
                rec["cancelled"] += 1
            elif _is_closed_status(st):
                rec["closed"] += 1
            elif _is_in_transit_status(st):
                rec["shipping"] += 1
        if buckets:
            current = dict(tg_state["period_stats"] or {})
            current.update(buckets)
            tg_state["period_stats"] = current
    except Exception:
        pass

def _tg_build_4hour_summary_lines(day_label, ts=None):
    ref = _ts_to_bkk(ts) if ts is not None else _now_bkk()
    day = ref.strftime("%Y-%m-%d")
    _tg_backfill_today_period_stats(day)
    lines = ["🕓 สรุปราย 4 ชั่วโมง"]
    for start_hour in range(0, 24, 4):
        key = f"{day}|{start_hour:02d}"
        rec = (tg_state["period_stats"] or {}).get(key, {}) or {}
        shipping = int(rec.get("shipping", 0) or 0)
        closed = int(rec.get("closed", 0) or 0)
        cancelled = int(rec.get("cancelled", 0) or 0)
        if not (shipping or closed or cancelled):
            continue
        end_hour = start_hour + 3
        lines.append(f"• {start_hour:02d}:00-{end_hour:02d}:59 → จัดส่ง {shipping} | ปิดงาน {closed} | ยกเลิก {cancelled}")
    return lines if len(lines) > 1 else []


def _tg_build_4hour_from_orders(filtered_orders):
    """สร้างสรุปราย 4 ชั่วโมง จากออเดอร์ที่กรองแล้ว (ตามจังหวัด/อำเภอ/ค่าย)"""
    buckets = {}
    for o in (filtered_orders or []):
        ts = int(getattr(o, "last_update_ts", None) or getattr(o, "first_seen_ts", None) or 0)
        if ts <= 0:
            continue
        dt = _ts_to_bkk(ts)
        start_hour = (dt.hour // 4) * 4
        key = start_hour
        rec = buckets.setdefault(key, {"shipping": 0, "closed": 0, "cancelled": 0})
        st = str(getattr(o, "status", "") or "")
        if _is_cancelled_status(st):
            rec["cancelled"] += 1
        elif _is_closed_status(st):
            rec["closed"] += 1
        elif _is_in_transit_status(st):
            rec["shipping"] += 1
    if not buckets:
        return []
    lines = ["🕓 สรุปราย 4 ชั่วโมง"]
    for start_hour in range(0, 24, 4):
        rec = buckets.get(start_hour, {})
        shipping = int(rec.get("shipping", 0) or 0)
        closed = int(rec.get("closed", 0) or 0)
        cancelled = int(rec.get("cancelled", 0) or 0)
        if not (shipping or closed or cancelled):
            continue
        end_hour = start_hour + 3
        lines.append(f"• {start_hour:02d}:00-{end_hour:02d}:59 → จัดส่ง {shipping} | ปิดงาน {closed} | ยกเลิก {cancelled}")
    return lines if len(lines) > 1 else []

def _tg_build_digest_message(day_orders, day_label, scan_stats=None):
    day_orders = list(day_orders or [])
    must_check = len([o for o in day_orders if _is_must_check_status(o.status)])
    delivered = len([o for o in day_orders if _is_delivered_status(o.status) and not _is_must_check_status(o.status)])
    cancelled = len([o for o in day_orders if _is_cancelled_status(o.status)])
    in_transit = len([o for o in day_orders if _is_in_transit_status(o.status) and not _is_delivered_status(o.status) and not _is_cancelled_status(o.status) and not _is_must_check_status(o.status)])
    preparing = len([o for o in day_orders if "กำลังเตรียม" in (o.status or "") and not _is_cancelled_status(o.status)])
    now_ts = int(_now_bkk().timestamp())
    overdue = 0
    alert_hours = int(tg_state.get("alert_hours", 48) or 48)
    for o in day_orders:
        if not _is_in_transit_status(o.status) or _is_delivered_status(o.status) or _is_cancelled_status(o.status):
            continue
        ref_ts = o.last_update_ts if o.last_update_ts is not None else o.first_seen_ts
        if not ref_ts:
            continue
        if (now_ts - int(ref_ts)) >= (alert_hours * 3600):
            overdue += 1
    closed = delivered + must_check
    payment = sum(_to_amount(o.total) for o in day_orders if _is_closed_status(o.status))
    product_lines = _tg_build_digest_product_lines(day_orders)
    cancelled_lines = _tg_build_digest_cancelled_lines(day_orders)
    period_lines = _tg_build_4hour_summary_lines(day_label)
    msg = (
        f"📊 <Order Tracker | ภาพรวม>\n📅 วันที่: {day_label}\n"
        "────────────────\n"
        f"📦 กำลังเตรียม  : {preparing}\n"
        f"🚚 กำลังจัดส่ง : {in_transit}\n"
        f"🟠 ต้องเช็ค     : {must_check}\n"
        f"⛔️ ยกเลิก       : {cancelled}\n"
        f"⚠️ ค้างนาน      : {overdue}\n"
        f"✅ ปิดงาน       : {closed}\n"
        f"💵 ยอดชำระรวม  : ฿{payment:,.2f}\n"
    )
    if period_lines:
        msg += "────────────────\n" + "\n".join(period_lines) + "\n"
    if scan_stats is not None:
        msg += (
            "────────────────\n"
            f"📨 Scan: {scan_stats.get('scanned', 0)} | 🆕 ใหม่: {scan_stats.get('new', 0)} | ♻️ อัปเดต: {scan_stats.get('updated', 0)}\n"
            f"⏰ เวลา: {_now_bkk().strftime('%H:%M')}\n"
        )
    # แยกส่วนสินค้าเป็นข้อความแยกถ้ายาวเกิน
    extra_msgs = []
    if product_lines:
        product_text = "────────────────\n" + "\n".join(product_lines) + "\n"
        if len(product_text) > 3800:
            chunks = _tg_split_long_text(product_text, 3800)
            if len(msg) + len(chunks[0]) < 3800:
                msg += chunks[0]
                extra_msgs.extend(chunks[1:])
            else:
                extra_msgs.extend(chunks)
        elif len(msg) + len(product_text) < 3800:
            msg += product_text
        else:
            extra_msgs.append(product_text)
    if cancelled_lines:
        cancel_text = "────────────────\n" + "\n".join(cancelled_lines) + "\n"
        if extra_msgs:
            if len(extra_msgs[-1]) + len(cancel_text) < 3800:
                extra_msgs[-1] += cancel_text
            else:
                extra_msgs.append(cancel_text)
        elif len(msg) + len(cancel_text) < 3800:
            msg += cancel_text
        else:
            extra_msgs.append(cancel_text)
    return [msg] + extra_msgs


def _tg_split_long_text(text, max_len=3800):
    """แยกข้อความยาวเป็นหลายชิ้น ตัดตามบรรทัด"""
    if len(text) <= max_len:
        return [text]
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len and current:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks if chunks else [text]


def _tg_send_eta(chat_id):
    """ส่งสรุป ETA พร้อมปุ่ม inline ให้กดดูแต่ละกลุ่ม"""
    now = _now_bkk()
    today_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_dt = today_dt - timedelta(days=1)
    tomorrow_dt = today_dt + timedelta(days=1)
    dayafter_dt = today_dt + timedelta(days=2)

    all_orders = store.list_all()
    buckets = _tg_build_eta_buckets(all_orders, today_dt, yesterday_dt, tomorrow_dt, dayafter_dt)

    transit_total = sum(len(b["orders"]) for b in buckets.values())
    total_payment = sum(_to_amount(o.total) for b in buckets.values() for o in b["orders"])

    # แสดงข้อมูล ETA ที่เรียนรู้
    learned = _compute_shop_eta()
    fallback = int(load_settings().get("eta_days", 2))

    msg = (
        f"📦 คาดว่าจะถึง\n"
        f"⏰ {now.strftime('%d/%m/%Y %H:%M')}\n"
        f"📋 ทั้งหมด {transit_total} รายการ | 💵 ฿{total_payment:,.2f}\n"
    )
    if learned:
        merch_info = " | ".join(f"{s} ~{d['avg_days']}วัน" for s, d in
                                sorted(learned.items(), key=lambda x: -x[1]["count"])[:5])
        msg += f"🧠 เรียนรู้: {merch_info}\n"
    msg += f"⚙️ ค่ากลาง: {fallback} วัน (ใช้เมื่อยังไม่มีข้อมูลร้าน)\n"
    msg += "────────────────\n"
    for key in ["overdue", "late", "today", "tomorrow", "dayafter", "later"]:
        b = buckets[key]
        cnt = len(b["orders"])
        if cnt > 0:
            rev = sum(_to_amount(o.total) for o in b["orders"])
            msg += f"{b['label']}: {cnt} รายการ (฿{rev:,.0f})\n"
    msg += "\n👇 กดปุ่มด้านล่างเพื่อดูรายละเอียด"

    # สร้าง inline keyboard
    buttons = []
    for key in ["overdue", "late", "today", "tomorrow", "dayafter", "later"]:
        b = buckets[key]
        cnt = len(b["orders"])
        if cnt > 0:
            buttons.append([{"text": f"{b['label']} ({cnt})", "callback_data": f"eta:{key}"}])
    buttons.append([{"text": "📋 ดูทั้งหมด", "callback_data": "eta:all"}])

    _tg_send(msg, chat_id=chat_id, reply_markup={"inline_keyboard": buttons})


def _tg_build_eta_buckets(all_orders, today_dt, yesterday_dt, tomorrow_dt, dayafter_dt):
    """จัดกลุ่มออเดอร์ตาม ETA — ใช้ค่าเฉลี่ยจากร้านถ้ามี"""
    buckets = {
        "overdue":  {"label": "🔴 เลยกำหนด",    "orders": []},
        "late":     {"label": "🟠 ช้ากว่ากำหนด",  "orders": []},
        "today":    {"label": "🟢 วันนี้",         "orders": []},
        "tomorrow": {"label": "🔵 พรุ่งนี้",       "orders": []},
        "dayafter": {"label": "🟣 มะรืน",          "orders": []},
        "later":    {"label": "⚪ หลังจากนั้น",    "orders": []},
    }
    for o in all_orders:
        st = (o.status or "").strip()
        if "กำลังจัดส่ง" not in st and st != "จัดส่งแล้ว":
            continue
        if "ยกเลิก" in st or "เสร็จสิ้น" in st or "สำเร็จ" in st or "มาถึงแล้ว" in st:
            continue
        ship_ts = o.last_update_ts or 0
        if not ship_ts:
            continue
        days = _get_eta_days(o)
        eta_dt = _ts_to_bkk(int(ship_ts)) + timedelta(days=days)
        eta_date = eta_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if eta_date < yesterday_dt:
            buckets["overdue"]["orders"].append(o)
        elif eta_date == yesterday_dt:
            buckets["late"]["orders"].append(o)
        elif eta_date == today_dt:
            buckets["today"]["orders"].append(o)
        elif eta_date == tomorrow_dt:
            buckets["tomorrow"]["orders"].append(o)
        elif eta_date == dayafter_dt:
            buckets["dayafter"]["orders"].append(o)
        else:
            buckets["later"]["orders"].append(o)
    return buckets


def _tg_format_eta_detail(orders_list, label):
    """สร้างข้อความรายละเอียดออเดอร์ในกลุ่ม ETA — จัดกลุ่มตามปลายทาง + รวมสินค้าซ้ำ"""
    if not orders_list:
        return [f"{label}\nไม่มีรายการ"]

    rev = sum(_to_amount(o.total) for o in orders_list)

    # จัดกลุ่มตามปลายทาง
    area_map = {}
    for o in orders_list:
        area = getattr(o, "ship_area", "") or "ไม่ระบุปลายทาง"
        area_map.setdefault(area, []).append(o)

    messages = []
    header = (
        f"{label} — {len(orders_list)} รายการ\n"
        f"💵 ฿{rev:,.2f}\n"
        "────────────────\n"
    )
    chunk = header

    for area, area_orders in sorted(area_map.items(), key=lambda x: -len(x[1])):
        area_rev = sum(_to_amount(o.total) for o in area_orders)
        area_line = f"\n📍 {area} ({len(area_orders)} รายการ ฿{area_rev:,.0f})\n"

        # รวมสินค้าซ้ำ: key = ชื่อสินค้า
        prod_map = {}
        for o in area_orders:
            prods = [re.sub(r"\s+", " ", str(p or "").strip()) for p in (o.products or [])]
            prods = [x for x in prods if x and x.lower() not in ("ค่าเริ่มต้น", "default")]
            name = _short_product_name(prods[0] if prods else "") or "ไม่ระบุสินค้า"
            if name not in prod_map:
                prod_map[name] = {"count": 0, "total": 0, "trackings": [], "eta_dates": set(), "shops": set()}
            prod_map[name]["count"] += 1
            prod_map[name]["total"] += _to_amount(o.total)
            trk = _extract_tracking_number(getattr(o, "tracking", ""))
            carrier = _carrier_name(getattr(o, "tracking", ""))
            url = _tracking_url(getattr(o, "tracking", ""))
            if trk and trk != "-":
                trk_entry = f"{carrier}:{trk}"
                if url:
                    trk_entry = f"{trk} ({url})"
                prod_map[name]["trackings"].append(trk_entry)
            days = _get_eta_days(o)
            eta_dt = _ts_to_bkk(int(o.last_update_ts)) + timedelta(days=days)
            prod_map[name]["eta_dates"].add(eta_dt.strftime("%d/%m"))
            prod_map[name]["shops"].add((o.shop or "").strip())

        prod_lines = ""
        for name, info in prod_map.items():
            qty = f" ── {info['count']} รายการ" if info['count'] > 1 else ""
            price = f"฿{info['total']:,.0f}" if info['total'] else "-"
            eta_str = ",".join(sorted(info["eta_dates"]))
            line = f"  • {name} ({price}){qty}"
            line += f" ~{eta_str}"
            # แสดง tracking เฉพาะที่มี
            if info["trackings"]:
                trk_display = info["trackings"][0]
                if len(info["trackings"]) > 1:
                    trk_display += f" +{len(info['trackings'])-1}"
                line += f"\n    📦 {trk_display}"
            line += "\n"
            prod_lines += line

        block = area_line + prod_lines
        if len(chunk) + len(block) > 3800:
            messages.append(chunk)
            chunk = f"{label} (ต่อ)\n────────────────\n"
        chunk += block

    if chunk.strip():
        messages.append(chunk)
    return messages


def _tg_handle_callback(chat_id, data):
    """จัดการ inline button callback"""
    if not data.startswith("eta:"):
        return
    key = data.split(":", 1)[1]
    now = _now_bkk()
    today_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_dt = today_dt - timedelta(days=1)
    tomorrow_dt = today_dt + timedelta(days=1)
    dayafter_dt = today_dt + timedelta(days=2)

    all_orders = store.list_all()
    buckets = _tg_build_eta_buckets(all_orders, today_dt, yesterday_dt, tomorrow_dt, dayafter_dt)

    if key == "all":
        # ส่งทุกกลุ่มที่มีรายการ
        for bk in ["overdue", "late", "today", "tomorrow", "dayafter", "later"]:
            b = buckets[bk]
            if b["orders"]:
                msgs = _tg_format_eta_detail(b["orders"], b["label"])
                for m in msgs:
                    _tg_send(m, chat_id=chat_id)
    elif key in buckets:
        b = buckets[key]
        msgs = _tg_format_eta_detail(b["orders"], b["label"])
        for m in msgs:
            _tg_send(m, chat_id=chat_id)


def _tg_compute_delivery_trips(target_date=None, gap=1800):
    """คำนวณรอบส่งสำหรับวันที่กำหนด (default = วันนี้)
    คืนค่า list ของ trips พร้อม areas สำหรับใช้ส่ง Telegram"""
    if not target_date:
        target_date = _now_bkk().strftime('%Y-%m-%d')
    all_orders = store.list_all()
    delivered = [o for o in all_orders if _is_delivered_status(o.status) and o.tracking]
    delivered.sort(key=lambda o: o.last_update_ts or 0)

    # กรองเฉพาะวันที่ต้องการ
    items = []
    for o in delivered:
        ts = o.last_update_ts or 0
        if ts == 0:
            continue
        dt = _ts_to_bkk(ts)
        if dt.strftime('%Y-%m-%d') != target_date:
            continue
        carrier = _carrier_name(o.tracking)
        area = (getattr(o, 'ship_area', '') or '').strip()
        items.append({
            'm': (o.merchant or '')[:40],
            't': _to_amount(o.total),
            'ts': ts,
            'time': dt.strftime('%H:%M'),
            'c': carrier,
            'a': area,
        })

    if not items:
        return []

    items.sort(key=lambda x: x['ts'])
    trips, cur = [], [items[0]]
    for i in range(1, len(items)):
        if items[i]['ts'] - items[i-1]['ts'] > gap:
            trips.append(cur)
            cur = [items[i]]
        else:
            cur.append(items[i])
    trips.append(cur)

    result = []
    for ti, trip in enumerate(trips):
        carriers = sorted(set(x['c'] for x in trip))
        prov_count = {}
        full_count = {}
        no_area_n = 0
        for x in trip:
            a = x.get('a') or ''
            if not a:
                no_area_n += 1
                continue
            parts = [p.strip() for p in a.split(',')]
            prov = parts[0] if parts else ''
            dist = parts[1].strip() if len(parts) > 1 else ''
            if prov:
                prov_count[prov] = prov_count.get(prov, 0) + 1
            full = f"{prov} - {dist}" if (prov and dist) else (prov or dist or '')
            if full:
                full_count[full] = full_count.get(full, 0) + 1
        result.append({
            'trip': ti + 1,
            'time_range': f"{trip[0]['time']}-{trip[-1]['time']}",
            'count': len(trip),
            'total': round(sum(x['t'] for x in trip), 2),
            'carriers': carriers,
            'provinces': sorted(prov_count.items(), key=lambda x: (-x[1], x[0])),
            'areas': sorted(full_count.items(), key=lambda x: (-x[1], x[0])),
            'no_area': no_area_n,
        })
    return result


def _tg_send_delivery_trips(chat_id, target_date=None):
    """ส่งสรุปรอบส่ง (🚚 ตรวจรอบส่ง) ไปยัง Telegram"""
    if not target_date:
        target_date = _now_bkk().strftime('%Y-%m-%d')
    trips = _tg_compute_delivery_trips(target_date=target_date)

    if not trips:
        _tg_send(
            f"🚚 ตรวจรอบส่ง — {target_date}\n\n"
            f"ยังไม่มีพัสดุปิดงานในวันนี้",
            chat_id=chat_id, reply_markup=_tg_main_menu()
        )
        return

    total_pkg = sum(t['count'] for t in trips)
    total_baht = sum(t['total'] for t in trips)
    lines = [
        f"🚚 ตรวจรอบส่ง — {target_date}",
        f"รวม {total_pkg} พัสดุ · ฿{total_baht:,.2f} · {len(trips)} รอบ",
        "(ห่าง > 30 นาที = คนละรอบ)",
        "────────────────",
    ]

    for t in trips:
        carrier_str = ", ".join(t['carriers']) if t['carriers'] else "-"
        lines.append(
            f"🚚 คนที่ {t['trip']} · {t['time_range']} · {carrier_str}"
        )
        lines.append(
            f"   {t['count']} พัสดุ · ฿{t['total']:,.2f}"
        )
        # เลือกแสดงระดับจังหวัด-อำเภอถ้าไม่เกิน 6 รายการ ไม่งั้นรวมเป็นจังหวัด
        use_full = (0 < len(t['areas']) <= 6)
        area_list = t['areas'] if use_full else t['provinces']
        if area_list:
            area_str = " · ".join(f"{name} ×{cnt}" for name, cnt in area_list[:8])
            lines.append(f"   📍 {area_str}")
            if len(area_list) > 8:
                lines.append(f"   📍 +{len(area_list) - 8} พื้นที่")
        if t['no_area']:
            lines.append(f"   ❓ ไม่ระบุพื้นที่ ×{t['no_area']}")
        lines.append("")

    msg = "\n".join(lines).rstrip()
    # ตัดเป็นหลายข้อความถ้ายาวเกิน 3800 ตัวอักษร
    if len(msg) <= 3800:
        _tg_send(msg, chat_id=chat_id, reply_markup=_tg_main_menu())
        return
    # split — ส่งทีละ chunk
    chunks = []
    cur_chunk = []
    cur_len = 0
    for line in lines:
        if cur_len + len(line) + 1 > 3800:
            chunks.append("\n".join(cur_chunk))
            cur_chunk = [line]
            cur_len = len(line) + 1
        else:
            cur_chunk.append(line)
            cur_len += len(line) + 1
    if cur_chunk:
        chunks.append("\n".join(cur_chunk))
    for i, c in enumerate(chunks):
        is_last = (i == len(chunks) - 1)
        _tg_send(c, chat_id=chat_id, reply_markup=_tg_main_menu() if is_last else None)


def _extract_tracking_number(tracking_str):
    """ดึงเฉพาะเลขพัสดุออกมา ตัด prefix ค่ายขนส่งทิ้ง"""
    s = str(tracking_str or "").strip()
    if not s or s == "-":
        return "-"
    # ตัด prefix เช่น "J&T Express การจัดส่งมาตรฐาน: " เอาแค่ตัวเลข/ตัวอักษรท้าย
    m = re.search(r"[:\s]([A-Za-z0-9]{8,})$", s)
    if m:
        return m.group(1)
    # fallback: เอาคำสุดท้ายที่ยาวพอ
    parts = s.split()
    for p in reversed(parts):
        cleaned = p.strip(":").strip()
        if len(cleaned) >= 8 and re.match(r"[A-Za-z0-9]+$", cleaned):
            return cleaned
    return s[-30:] if len(s) > 30 else s


def _tg_build_carrier_products_text(orders, day_label):
    orders = list(orders or [])
    carrier_map = {}
    for o in orders:
        if _is_cancelled_status(o.status):
            continue
        if not (_is_delivered_status(o.status) or _is_must_check_status(o.status)):
            continue
        carrier = _carrier_name(getattr(o, "tracking", ""))
        lst = carrier_map.setdefault(carrier, [])
        cleaned = [re.sub(r"\s+", " ", str(p or "").strip()) for p in (o.products or [])]
        cleaned = [x for x in cleaned if x and x.lower() not in ("ค่าเริ่มต้น", "default")]
        short_name = _short_product_name(cleaned[0] if cleaned else "") or "สินค้าไม่ระบุชื่อ"
        tracking_raw = str(getattr(o, "tracking", "") or "").strip() or "-"
        tracking_num = _extract_tracking_number(tracking_raw)
        area = getattr(o, "ship_area", "") or "ไม่ระบุปลายทาง"
        lst.append({"name": short_name, "tracking": tracking_num, "order_id": o.order_id,
                     "area": area, "total": _to_amount(o.total)})

    if not carrier_map:
        return [f"📋 สินค้าแยกขนส่ง\n📅 {day_label}\nไม่มีข้อมูล"]

    messages = []
    for carrier in sorted(carrier_map.keys()):
        items = carrier_map[carrier]
        carrier_rev = sum(i["total"] for i in items)
        header = (f"📋 สินค้าแยกขนส่ง\n📅 {day_label}\n────────────────\n"
                  f"🚚 [{carrier}] {len(items)} รายการ (฿{carrier_rev:,.0f})\n")
        chunk = header

        # จัดกลุ่มตามปลายทาง
        area_map = {}
        for item in items:
            area_map.setdefault(item["area"], []).append(item)

        for area, area_items in sorted(area_map.items(), key=lambda x: -len(x[1])):
            area_rev = sum(i["total"] for i in area_items)
            area_line = f"\n📍 {area} ({len(area_items)} รายการ ฿{area_rev:,.0f})\n"

            # รวมสินค้าซ้ำ
            prod_map = {}
            for item in area_items:
                p = prod_map.setdefault(item["name"], {"count": 0, "total": 0, "trackings": []})
                p["count"] += 1
                p["total"] += item["total"]
                if item["tracking"] and item["tracking"] != "-":
                    p["trackings"].append(item["tracking"])

            prod_lines = ""
            for name, info in prod_map.items():
                qty = f" ── {info['count']} รายการ" if info['count'] > 1 else ""
                price = f"฿{info['total']:,.0f}" if info['total'] else "-"
                line = f"  • {name} ({price}){qty}"
                if info["trackings"]:
                    trk = info["trackings"][0]
                    if len(info["trackings"]) > 1:
                        trk += f" +{len(info['trackings'])-1}"
                    line += f"\n    📦 {trk}"
                line += "\n"
                prod_lines += line

            block = area_line + prod_lines
            if len(chunk) + len(block) > 3800:
                messages.append(chunk)
                chunk = f"🚚 [{carrier}] (ต่อ)\n────────────────\n"
            chunk += block

        if chunk.strip():
            messages.append(chunk)

    return messages


def _tg_send_carrier_products(chat_id, orders, day_label):
    """ส่งรายการสินค้าแยกขนส่ง — รองรับแยกหลายข้อความถ้ายาวเกิน"""
    result = _tg_build_carrier_products_text(orders, day_label)
    if isinstance(result, str):
        result = [result]
    for i, msg in enumerate(result):
        is_last = (i == len(result) - 1)
        _tg_send(msg, chat_id=chat_id,
                 reply_markup=_tg_main_menu() if is_last else None)


# ─── Order search / detail ──────────────────────────────────────────────────

def _tg_format_order_detail(o):
    update_dt = "-"
    if getattr(o, "last_update_ts", None):
        try:
            update_dt = _ts_to_bkk(o.last_update_ts).strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    trk_line = f"🚚 ขนส่ง/พัสดุ: {o.tracking or '-'}"
    url = _tracking_url(o.tracking)
    if url:
        trk_line += f"\n🔗 เช็คพัสดุ: {url}"
    return (
        f"📋 หมายเลขคำสั่งซื้อ: {o.order_id}\n"
        f"🏪 แพลตฟอร์ม: {o.shop}\n"
        f"🏬 ร้านค้า: {o.merchant or '-'}\n"
        f"📍 พื้นที่จัดส่ง: {getattr(o, 'ship_area', '') or '-'}\n"
        f"📌 สถานะ: {o.status}\n"
        f"📅 วันที่สั่งซื้อ: {o.date}\n"
        f"🕐 วันที่อัปเดตล่าสุด: {update_dt}\n"
        f"💰 ทั้งหมด: {o.total}\n"
        f"{trk_line}\n"
        f"📦 สินค้า: {', '.join((o.products or [])[:5])}{'...' if len(o.products or []) > 5 else ''}"
    )

def _tg_search_orders(query, start_day, end_day, limit=5):
    q = str(query or "").strip().lower()
    if not q:
        return "พิมพ์คำค้นได้เลย เช่น ชื่อสินค้า / เลขออเดอร์ / เลขพัสดุ"
    orders = _get_orders_for_range(start_day, end_day)
    matched = []
    for o in orders:
        hay = " ".join([
            str(o.order_id or ""), str(o.shop or ""), str(o.merchant or ""), str(o.status or ""),
            str(o.tracking or ""), str(o.total or ""), " ".join([str(x) for x in (o.products or [])])
        ]).lower()
        if q in hay:
            matched.append(o)
    if not matched:
        return f"ไม่พบรายการที่ตรงกับ: {query}"
    lines = [f"🔎 <Order Tracker | ค้นหา>\nคำค้น: {query}\nพบ {len(matched)} รายการ"]
    for o in matched[:limit]:
        lines.append("────────────────")
        lines.append(_tg_format_order_detail(o))
    if len(matched) > limit:
        lines.append(f"────────────────\n…และอีก {len(matched)-limit} รายการ")
    return "\n".join(lines)


# ─── Product check system ──────────────────────────────────────────────────

def _tg_resolve_range(key, chat_id=""):
    today = _now_bkk().strftime("%Y-%m-%d")
    k = str(key or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", k):
        return k, k, k
    now = _now_bkk()
    mapping = {"วันนี้": 0, "เมื่อวาน": -1, "3 วัน": 3, "5 วัน": 5, "7 วัน": 7, "15 วัน": 15, "14 วัน": 14, "30 วัน": 30}
    if k in mapping:
        days = mapping[k]
        if days == 0:
            return today, today, today
        if days == -1:
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            return yesterday, yesterday, yesterday
        end = now
        start = now - timedelta(days=days - 1)
        s = start.strftime("%Y-%m-%d")
        e = end.strftime("%Y-%m-%d")
        return s, e, f"{s} ถึง {e}"
    state = tg_state["chat_state"].get(str(chat_id or ""), {}) if chat_id else {}
    s = state.get("range_start") or today
    e = state.get("range_end") or today
    label = state.get("day_label") or (s if s == e else f"{s} ถึง {e}")
    return s, e, label

def _tg_check_day_key(chat_id=""):
    s, e, label = _tg_resolve_range("", chat_id=chat_id)
    carrier = (tg_state["chat_state"].get(str(chat_id), {}) or {}).get("selected_carrier", "")
    base = s if s == e else f"{s}__{e}"
    if carrier:
        base = f"{base}__carrier__{carrier}"
    return base, label

def _tg_extract_daily_carrier_summary(orders):
    grouped = {}
    for o in list(orders or []):
        if _is_cancelled_status(getattr(o, "status", "")):
            continue
        if not _is_closed_status(getattr(o, "status", "")):
            continue
        products = [x for x in (getattr(o, "products", []) or []) if str(x or "").strip()]
        if not products:
            continue
        carrier = _carrier_name(getattr(o, "tracking", ""))
        if not carrier or carrier == "ไม่ระบุขนส่ง":
            continue
        rec = grouped.setdefault(carrier, {"carrier": carrier, "total": 0})
        rec["total"] += 1
    return sorted(grouped.values(), key=lambda x: (-int(x["total"]), x["carrier"].lower()))

def _tg_extract_daily_product_summary(orders, carrier_filter=""):
    grouped = {}
    wanted = str(carrier_filter or "").strip()
    for o in list(orders or []):
        if _is_cancelled_status(getattr(o, "status", "")):
            continue
        if not _is_closed_status(getattr(o, "status", "")):
            continue
        carrier = _carrier_name(getattr(o, "tracking", ""))
        if not carrier or carrier == "ไม่ระบุขนส่ง":
            continue
        if wanted and carrier != wanted:
            continue
        names = [re.sub(r"\s+", " ", str(x or "").strip()) for x in (getattr(o, "products", []) or [])]
        names = [x for x in names if x and x.lower() not in ("default", "ค่าเริ่มต้น")]
        key = _short_product_name(names[0] if names else "") or "สินค้าไม่ระบุชื่อ"
        rec = grouped.setdefault(key, {"name": key, "total": 0, "carrier": carrier})
        rec["total"] += 1
    return sorted(grouped.values(), key=lambda x: (-int(x["total"]), x["name"].lower()))

def _tg_get_product_progress(day_key, product_name, total=0):
    bucket = tg_state["product_checks"].setdefault(str(day_key), {})
    rec = bucket.setdefault(product_name, {"checked": 0, "done": False, "total": int(total or 0)})
    rec["total"] = max(int(rec.get("total", 0) or 0), int(total or 0))
    rec["checked"] = max(0, int(rec.get("checked", 0) or 0))
    rec["done"] = bool(rec.get("done", False))
    return rec


# ─── Telegram menus ─────────────────────────────────────────────────────────

def _tg_main_menu():
    return {
        "keyboard": [
            [{"text": "📊 ภาพรวม"}, {"text": "📅 เลือกวัน"}],
            [{"text": "🚚 รายการจัดส่ง"}, {"text": "📦 คาดว่าจะถึง"}],
            [{"text": "🚚 ตรวจรอบส่ง"}, {"text": "✅ ตรวจนับสินค้า"}],
            [{"text": "🔎 ค้นหา"}, {"text": "📈 สถิติ"}],
            [{"text": "💰 ยอดวันนี้"}, {"text": "💰 ยอดสัปดาห์"}],
            [{"text": "📋 เมนู"}]
        ],
        "resize_keyboard": True, "persistent_keyboard": True,
    }

def _tg_date_menu():
    return {
        "keyboard": [
            [{"text": "วันนี้"}, {"text": "เมื่อวาน"}],
            [{"text": "3 วัน"}, {"text": "5 วัน"}, {"text": "7 วัน"}],
            [{"text": "15 วัน"}, {"text": "30 วัน"}],
            [{"text": "พิมพ์วันที่ YYYY-MM-DD"}, {"text": "📋 เมนู"}]
        ],
        "resize_keyboard": True, "persistent_keyboard": True,
    }

def _tg_carrier_menu(carriers):
    rows = []
    for item in list(carriers or [])[:12]:
        rows.append([{"text": f"🚚 {item['carrier']} ({item['total']})"}])
    rows.append([{"text": "📋 เมนู"}])
    return {"keyboard": rows, "resize_keyboard": True, "persistent_keyboard": True}

def _tg_product_list_menu(products):
    rows = [
        [{"text": "🟡 ยังไม่ครบ"}, {"text": "📚 ทั้งหมด"}],
    ]
    for item in list(products or [])[:20]:
        done_mark = " ✅" if int(item.get("remain", 0)) <= 0 else ""
        rows.append([{"text": f"📦 {item['name'][:40]} ({item['checked']}/{item['total']}){done_mark}"}])
    rows.append([{"text": "📋 เมนู"}])
    return {"keyboard": rows, "resize_keyboard": True, "persistent_keyboard": True}

def _tg_product_detail_menu():
    return {
        "keyboard": [
            [{"text": "+1"}, {"text": "+5"}, {"text": "+10"}],
            [{"text": "✍️ พิมพ์จำนวน"}, {"text": "✅ ครบแล้ว"}],
            [{"text": "♻️ รีเซ็ตยอดเช็ก"}, {"text": "⬅️ กลับรายการ"}],
            [{"text": "📋 เมนู"}]
        ],
        "resize_keyboard": True, "persistent_keyboard": True,
    }


# ─── Area (province/district) helpers for สรุปรายรอบ ─────────────────────

def _tg_extract_provinces(orders):
    """ดึงจังหวัดที่ไม่ซ้ำจาก ship_area ของออเดอร์"""
    provinces = {}
    no_area = 0
    for o in (orders or []):
        area = getattr(o, "ship_area", "") or ""
        if not area.strip():
            no_area += 1
            continue
        prov = area.split(",")[0].strip()
        if prov:
            provinces[prov] = provinces.get(prov, 0) + 1
    result = sorted(provinces.items(), key=lambda x: (-x[1], x[0]))
    return result, no_area


def _tg_extract_districts(orders, province):
    """ดึงอำเภอที่ไม่ซ้ำภายในจังหวัดที่เลือก"""
    districts = {}
    no_district = 0
    for o in (orders or []):
        area = getattr(o, "ship_area", "") or ""
        if not area.strip():
            continue
        parts = [p.strip() for p in area.split(",")]
        prov = parts[0] if parts else ""
        if prov != province:
            continue
        dist = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
        if not dist:
            no_district += 1
            continue
        districts[dist] = districts.get(dist, 0) + 1
    result = sorted(districts.items(), key=lambda x: (-x[1], x[0]))
    return result, no_district


def _tg_province_menu(provinces, no_area=0):
    """สร้างเมนูเลือกจังหวัด"""
    rows = [[{"text": "🌐 ดูรวมทุกจังหวัด"}]]
    for prov, count in list(provinces)[:15]:
        rows.append([{"text": f"🏠 {prov} ({count})"}])
    if no_area > 0:
        rows.append([{"text": f"🏠 ไม่ระบุพื้นที่ ({no_area})"}])
    rows.append([{"text": "📋 เมนู"}])
    return {"keyboard": rows, "resize_keyboard": True, "persistent_keyboard": True}


def _tg_district_menu(districts, province, no_district=0):
    """สร้างเมนูเลือกอำเภอ"""
    rows = [[{"text": f"🌐 ดูรวมทุกอำเภอ ({province})"}]]
    for dist, count in list(districts)[:15]:
        rows.append([{"text": f"📍 {dist} ({count})"}])
    if no_district > 0:
        rows.append([{"text": f"📍 ไม่ระบุอำเภอ ({no_district})"}])
    rows.append([{"text": "⬅️ กลับเลือกจังหวัด"}, {"text": "📋 เมนู"}])
    return {"keyboard": rows, "resize_keyboard": True, "persistent_keyboard": True}


def _tg_extract_carriers_for_digest(orders):
    """ดึงค่ายขนส่งที่ไม่ซ้ำจากออเดอร์ (สำหรับสรุปรายรอบ)"""
    carriers = {}
    for o in (orders or []):
        carrier = _carrier_name(getattr(o, "tracking", ""))
        if carrier and carrier != "ไม่ระบุขนส่ง":
            carriers[carrier] = carriers.get(carrier, 0) + 1
    result = sorted(carriers.items(), key=lambda x: (-x[1], x[0]))
    return result


def _tg_digest_carrier_menu(carriers):
    """สร้างเมนูเลือกค่ายขนส่ง (สำหรับสรุปรายรอบ)"""
    rows = [[{"text": "🌐 ดูรวมทุกค่ายขนส่ง"}]]
    for carrier, count in list(carriers)[:12]:
        rows.append([{"text": f"🚛 {carrier} ({count})"}])
    rows.append([{"text": "⬅️ กลับเลือกอำเภอ"}, {"text": "📋 เมนู"}])
    return {"keyboard": rows, "resize_keyboard": True, "persistent_keyboard": True}


def _tg_show_carrier_step(chat_id, orders, label, state):
    """แสดงเมนูเลือกค่ายขนส่ง หรือดำเนินการเลยถ้าไม่มีค่าย"""
    province = state.get("digest_province", "")
    district = state.get("digest_district", "")
    filtered = _tg_filter_orders_by_area(orders, province or None, district or None)
    carriers = _tg_extract_carriers_for_digest(filtered)
    if carriers and len(carriers) > 1:
        state["digest_mode"] = "pick_carrier"
        area_parts = []
        if province:
            area_parts.append(province)
        if district:
            area_parts.append(district)
        area_text = ", ".join(area_parts) if area_parts else "ทุกจังหวัด"
        action_label = _tg_action_label(state.get("digest_action", "summary"))
        lines = [f"{action_label}\n📅 วันที่: {label}\n📍 {area_text}",
                 "เลือกค่ายขนส่งที่ต้องการดู หรือ ดูรวมทุกค่าย"]
        for carrier, count in carriers[:12]:
            lines.append(f"• {carrier} ({count} รายการ)")
        _tg_send("\n".join(lines), chat_id=chat_id,
                 reply_markup=_tg_digest_carrier_menu(carriers))
        _tg_save()
        return True
    else:
        # มีแค่ค่ายเดียวหรือไม่มี → ดำเนินการเลย
        carrier_name = carriers[0][0] if carriers else None
        state["digest_carrier"] = carrier_name or ""
        _tg_execute_filtered_action(chat_id)
        return False


def _tg_action_label(action):
    """ชื่อหัวข้อสำหรับแต่ละ action"""
    labels = {
        "summary": "📊 ภาพรวม",
        "carrier_products": "🚚 รายการจัดส่ง",
        "check_product": "✅ ตรวจนับสินค้า",
        "chart": "📈 สถิติ",
        "sales_day": "💰 ยอดวันนี้",
        "sales_week": "💰 ยอดสัปดาห์",
    }
    return labels.get(action, "📊 ภาพรวม")


def _tg_start_area_selection(chat_id, action):
    """เริ่ม flow เลือกจังหวัด/อำเภอ/ค่ายขนส่ง สำหรับทุกเมนู"""
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    state["digest_action"] = action
    state.pop("digest_province", None)
    state.pop("digest_district", None)
    state.pop("digest_carrier", None)
    state["digest_mode"] = "pick_province"
    s, e, label = _tg_resolve_range("", chat_id=chat_id)
    orders = _get_orders_for_range(s, e)
    provinces, no_area = _tg_extract_provinces(orders)
    if provinces:
        action_text = _tg_action_label(action)
        lines = [f"{action_text}\n📅 วันที่: {label}",
                 "เลือกจังหวัดที่ต้องการดู หรือ ดูรวมทุกจังหวัด"]
        for prov, count in provinces[:15]:
            lines.append(f"• {prov} ({count} รายการ)")
        if no_area > 0:
            lines.append(f"• ไม่ระบุพื้นที่ ({no_area} รายการ)")
        _tg_send("\n".join(lines), chat_id=chat_id,
                 reply_markup=_tg_province_menu(provinces, no_area))
    else:
        # ไม่มีข้อมูลพื้นที่ → ดำเนินการเลย
        _tg_execute_filtered_action(chat_id)
    _tg_save()


def _tg_get_area_label(state):
    """สร้าง label พื้นที่จาก state"""
    province = state.get("digest_province", "")
    district = state.get("digest_district", "")
    carrier = state.get("digest_carrier", "")
    parts = []
    if province:
        parts.append(province)
    if district:
        parts.append(district)
    area = ", ".join(parts) if parts else "ทุกจังหวัด"
    if carrier:
        area += f" | 🚛 {carrier}"
    return area


def _tg_execute_filtered_action(chat_id):
    """ดำเนินการตาม action ที่เก็บไว้ พร้อมตัวกรองจังหวัด/อำเภอ/ค่าย"""
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    action = state.get("digest_action", "summary")
    province = state.get("digest_province", "") or None
    district = state.get("digest_district", "") or None
    carrier = state.get("digest_carrier", "") or None
    s, e, label = _tg_resolve_range("", chat_id=chat_id)
    orders = _get_orders_for_range(s, e)
    state.pop("digest_mode", None)

    # กรองออเดอร์ตามพื้นที่ + ค่ายขนส่ง
    filtered = _tg_filter_orders_by_area(orders, province, district)
    if carrier:
        filtered = [o for o in filtered if _carrier_name(getattr(o, "tracking", "")) == carrier]
    area_label = _tg_get_area_label(state)

    if action == "summary":
        msgs = _tg_build_digest_by_district(orders, label,
                 province=province, district=district, carrier=carrier)
        for i, m in enumerate(msgs):
            _tg_send(m, chat_id=chat_id,
                     reply_markup=_tg_main_menu() if i == len(msgs)-1 else None)

    elif action == "carrier_products":
        _tg_send_carrier_products(chat_id, filtered, label + f"\n📍 {area_label}")

    elif action == "chart":
        _tg_send("⏳ กำลังสร้างกราฟ...", chat_id=chat_id)
        chart_label = f"{label} | {area_label}"
        chart1 = _tg_generate_status_chart(filtered, chart_label)
        if chart1:
            _tg_send_photo(chart1, caption=f"📊 สถานะออเดอร์ — {chart_label}", chat_id=chat_id)
        chart2 = _tg_generate_daily_trend_chart(7)
        if chart2:
            _tg_send_photo(chart2, caption="📈 แนวโน้ม 7 วันล่าสุด", chat_id=chat_id)
        if not chart1 and not chart2:
            _tg_send("❌ ไม่สามารถสร้างกราฟได้ (อาจยังไม่ได้ติดตั้ง matplotlib)",
                     chat_id=chat_id, reply_markup=_tg_main_menu())

    elif action == "sales_day":
        msgs = _tg_build_sales_summary("day", province=province, district=district, carrier=carrier)
        for i, m in enumerate(msgs):
            _tg_send(m, chat_id=chat_id,
                     reply_markup=_tg_main_menu() if i == len(msgs)-1 else None)

    elif action == "sales_week":
        msgs = _tg_build_sales_summary("week", province=province, district=district, carrier=carrier)
        for i, m in enumerate(msgs):
            _tg_send(m, chat_id=chat_id,
                     reply_markup=_tg_main_menu() if i == len(msgs)-1 else None)

    elif action == "check_product":
        # ตั้งค่า carrier สำหรับ product check flow
        if carrier:
            state["selected_carrier"] = carrier
        else:
            state.pop("selected_carrier", None)
        state.pop("selected_product", None)
        _tg_open_product_check(chat_id)

    _tg_save()


def _tg_filter_orders_by_area(orders, province=None, district=None):
    """กรองออเดอร์ตามจังหวัด/อำเภอ"""
    if not province:
        return list(orders or [])
    filtered = []
    for o in (orders or []):
        area = getattr(o, "ship_area", "") or ""
        parts = [p.strip() for p in area.split(",")]
        prov = parts[0] if parts else ""
        dist = parts[1].strip() if len(parts) > 1 and parts[1].strip() else ""
        if province == "ไม่ระบุพื้นที่":
            if not area.strip():
                filtered.append(o)
            continue
        if prov != province:
            continue
        if district:
            if district == "ไม่ระบุอำเภอ":
                if not dist:
                    filtered.append(o)
            elif dist == district:
                filtered.append(o)
        else:
            filtered.append(o)
    return filtered


def _tg_build_digest_by_district(orders, day_label, province=None, district=None, carrier=None):
    """สร้างสรุปรายรอบ พร้อมแยกยอดชำระ/รายการ ตามอำเภอ"""
    if province and not district:
        # สรุประดับจังหวัด → แยกตามอำเภอ
        filtered = _tg_filter_orders_by_area(orders, province)
        area_label = f"จังหวัด: {province}"
    elif province and district:
        # สรุประดับอำเภอเฉพาะ
        filtered = _tg_filter_orders_by_area(orders, province, district)
        area_label = f"{province}, {district}"
    else:
        filtered = list(orders or [])
        area_label = "ทุกจังหวัด"

    # ── กรองตามค่ายขนส่ง ──
    if carrier:
        filtered = [o for o in filtered if _carrier_name(getattr(o, "tracking", "")) == carrier]
        area_label += f"\n🚛 ค่ายขนส่ง: {carrier}"

    must_check = len([o for o in filtered if _is_must_check_status(o.status)])
    delivered = len([o for o in filtered if _is_delivered_status(o.status) and not _is_must_check_status(o.status)])
    cancelled = len([o for o in filtered if _is_cancelled_status(o.status)])
    in_transit = len([o for o in filtered if _is_in_transit_status(o.status)
                      and not _is_delivered_status(o.status) and not _is_cancelled_status(o.status)
                      and not _is_must_check_status(o.status)])
    preparing = len([o for o in filtered if "กำลังเตรียม" in (o.status or "") and not _is_cancelled_status(o.status)])
    now_ts = int(_now_bkk().timestamp())
    overdue = 0
    alert_hours = int(tg_state.get("alert_hours", 48) or 48)
    for o in filtered:
        if not _is_in_transit_status(o.status) or _is_delivered_status(o.status) or _is_cancelled_status(o.status):
            continue
        ref_ts = o.last_update_ts if o.last_update_ts is not None else o.first_seen_ts
        if not ref_ts:
            continue
        if (now_ts - int(ref_ts)) >= (alert_hours * 3600):
            overdue += 1

    closed = delivered + must_check
    payment = sum(_to_amount(o.total) for o in filtered if _is_closed_status(o.status))

    msg = (
        f"📊 <Order Tracker | ภาพรวม>\n"
        f"📅 วันที่: {day_label}\n"
        f"📍 {area_label}\n"
        "────────────────\n"
        f"📦 กำลังเตรียม  : {preparing}\n"
        f"🚚 กำลังจัดส่ง : {in_transit}\n"
        f"🟠 ต้องเช็ค     : {must_check}\n"
        f"⛔️ ยกเลิก       : {cancelled}\n"
        f"⚠️ ค้างนาน      : {overdue}\n"
        f"✅ ปิดงาน       : {closed}\n"
        f"💵 ยอดชำระรวม  : ฿{payment:,.2f}\n"
    )

    # ── สรุปราย 4 ชั่วโมง (คำนวณจากออเดอร์ที่กรองแล้ว) ──
    period_lines = _tg_build_4hour_from_orders(filtered)
    if period_lines:
        msg += "────────────────\n" + "\n".join(period_lines) + "\n"

    # ── แยกยอดชำระรายอำเภอ (เฉพาะเมื่อดูระดับจังหวัด หรือ ดูรวม) ──
    if not district:
        area_stats = {}
        for o in filtered:
            if not _is_closed_status(o.status):
                continue
            area = getattr(o, "ship_area", "") or ""
            parts = [p.strip() for p in area.split(",")]
            if province:
                dist_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "(ไม่ระบุอำเภอ)"
            else:
                dist_name = parts[0].strip() if parts and parts[0].strip() else "(ไม่ระบุพื้นที่)"
            rec = area_stats.setdefault(dist_name, {"count": 0, "payment": 0.0})
            rec["count"] += 1
            rec["payment"] += _to_amount(o.total)
        if area_stats:
            header = "💰 ยอดชำระรายอำเภอ" if province else "💰 ยอดชำระรายจังหวัด"
            msg += f"────────────────\n{header}\n"
            for name in sorted(area_stats.keys(), key=lambda k: -area_stats[k]["payment"]):
                rec = area_stats[name]
                msg += f"• {name}: {rec['count']} รายการ | ฿{rec['payment']:,.2f}\n"

    # ── รายการสินค้า (แยกตามอำเภอ ถ้าอยู่ระดับจังหวัด) ──
    product_lines = []
    if district or (province and province == "ไม่ระบุพื้นที่"):
        # ดูเฉพาะอำเภอ → แสดงรายการสินค้าปกติ
        product_lines = _tg_build_digest_product_lines(filtered)
    elif province:
        # ดูระดับจังหวัด → แสดงรายการสินค้าแยกอำเภอ
        dist_products = {}
        for o in filtered:
            if _is_cancelled_status(o.status):
                continue
            if not (_is_closed_status(o.status)):
                continue
            area = getattr(o, "ship_area", "") or ""
            parts = [p.strip() for p in area.split(",")]
            dist_name = parts[1].strip() if len(parts) > 1 and parts[1].strip() else "(ไม่ระบุอำเภอ)"
            dist_products.setdefault(dist_name, []).append(o)
        if dist_products:
            dp_lines = ["📋 รายการสินค้าแยกอำเภอ"]
            for dist_name in sorted(dist_products.keys()):
                dist_orders = dist_products[dist_name]
                product_map = {}
                for o in dist_orders:
                    names = [re.sub(r"\s+", " ", str(x or "").strip()) for x in (o.products or [])]
                    names = [x for x in names if x and x.lower() not in ("default", "ค่าเริ่มต้น")]
                    key = _short_product_name(names[0] if names else "") or "สินค้าไม่ระบุชื่อ"
                    rec = product_map.setdefault(key, {"count": 0, "total": 0})
                    rec["count"] += 1
                    rec["total"] += _to_amount(o.total)
                if product_map:
                    dist_rev = sum(r["total"] for r in product_map.values())
                    dp_lines.append(f"\n📍 [{dist_name}] (฿{dist_rev:,.0f})")
                    ranked = sorted(product_map.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
                    for name, info in ranked:
                        qty = f" ── {info['count']} รายการ" if info['count'] > 1 else ""
                        price = f"฿{info['total']:,.0f}" if info['total'] else ""
                        dp_lines.append(f"  • {name} ({price}){qty}")
            product_lines = dp_lines
    else:
        # ดูรวมทุกจังหวัด → แสดงรายการสินค้าปกติ
        product_lines = _tg_build_digest_product_lines(filtered)

    # ── สินค้ายกเลิก ──
    cancelled_lines = _tg_build_digest_cancelled_lines(filtered)

    # ── แยกข้อความถ้ายาวเกิน Telegram limit ──
    extra_msgs = []
    if product_lines:
        product_text = "────────────────\n" + "\n".join(product_lines) + "\n"
        # ถ้ายาวเกิน 3800 ตัวเอง → แยกเป็นหลายข้อความ
        if len(product_text) > 3800:
            chunks = _tg_split_long_text(product_text, 3800)
            if len(msg) + len(chunks[0]) < 3800:
                msg += chunks[0]
                extra_msgs.extend(chunks[1:])
            else:
                extra_msgs.extend(chunks)
        elif len(msg) + len(product_text) < 3800:
            msg += product_text
        else:
            extra_msgs.append(product_text)
    if cancelled_lines:
        cancel_text = "────────────────\n" + "\n".join(cancelled_lines) + "\n"
        if extra_msgs:
            if len(extra_msgs[-1]) + len(cancel_text) < 3800:
                extra_msgs[-1] += cancel_text
            else:
                extra_msgs.append(cancel_text)
        elif len(msg) + len(cancel_text) < 3800:
            msg += cancel_text
        else:
            extra_msgs.append(cancel_text)

    return [msg] + extra_msgs


# ─── Product check rendering ───────────────────────────────────────────────

def _tg_render_carrier_list(chat_id):
    s, e, label = _tg_resolve_range("", chat_id=chat_id)
    orders = _get_orders_for_range(s, e)
    carriers = _tg_extract_daily_carrier_summary(orders)
    if not carriers:
        return (f"✅ ตรวจนับสินค้า\n📅 วันที่: {label}\nยังไม่มีรายการปิดงานที่มีค่ายขนส่งสำหรับตรวจนับ", None)
    lines = [f"✅ ตรวจนับสินค้า\n📅 วันที่: {label}", "เลือกค่ายขนส่ง"]
    for idx, item in enumerate(carriers, 1):
        lines.append(f"{idx}) {item['carrier']} — {item['total']} รายการ")
    return ("\n".join(lines), _tg_carrier_menu(carriers))

def _tg_render_product_list(chat_id, only_pending=False):
    day_key, label = _tg_check_day_key(chat_id)
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    carrier = state.get("selected_carrier", "")
    s, e, _ = _tg_resolve_range("", chat_id=chat_id)
    products = _tg_extract_daily_product_summary(_get_orders_for_range(s, e), carrier_filter=carrier)
    if not products:
        return (f"✅ ตรวจนับสินค้า\n📅 วันที่: {label}\nยังไม่มีสินค้าจากรายการปิดงานให้ตรวจนับ", None)
    title = "เลือกสินค้าที่ต้องการเช็ก" if not only_pending else "แสดงเฉพาะรายการที่ยังเช็กไม่ครบ"
    lines = [f"✅ ตรวจนับสินค้า\n📅 วันที่: {label}", title]
    rendered = []
    for item in products:
        rec = _tg_get_product_progress(day_key, item["name"], item["total"])
        checked = min(int(rec.get("checked", 0) or 0), int(item["total"] or 0))
        remain = max(0, int(item["total"] or 0) - checked)
        done = remain <= 0 or bool(rec.get("done", False))
        if only_pending and done:
            continue
        rendered.append({"name": item["name"], "total": int(item["total"]), "checked": checked, "remain": remain, "done": done})
    if only_pending and not rendered:
        return (f"✅ ตรวจนับสินค้า\n📅 วันที่: {label}\nรายการเช็กครบหมดแล้ว 🎉", _tg_product_list_menu([]))
    for idx, item in enumerate(rendered[:20], 1):
        icon = "✅" if item["done"] else "•"
        lines.append(f"{icon} {idx}) {item['name']} — {item['checked']}/{int(item['total'])}")
    return ("\n".join(lines), _tg_product_list_menu(rendered))

def _tg_render_product_detail(chat_id, product_name):
    day_key, label = _tg_check_day_key(chat_id)
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    carrier = state.get("selected_carrier", "")
    s, e, _ = _tg_resolve_range("", chat_id=chat_id)
    products = _tg_extract_daily_product_summary(_get_orders_for_range(s, e), carrier_filter=carrier)
    target = None
    for item in products:
        if item["name"] == product_name:
            target = item
            break
    if target is None:
        for item in products:
            if product_name.strip().startswith(item["name"].strip()) or item["name"].strip().startswith(product_name.strip()):
                target = item
                product_name = item["name"]
                break
    if target is None:
        return "ไม่พบสินค้ารายการนี้ในช่วงวันที่ที่เลือก"
    rec = _tg_get_product_progress(day_key, product_name, target["total"])
    checked = min(int(rec.get("checked", 0) or 0), int(target["total"] or 0))
    remain = max(0, int(target["total"] or 0) - checked)
    done = remain <= 0 or bool(rec.get("done", False))
    if done and checked < int(target["total"] or 0):
        checked = int(target["total"] or 0)
        remain = 0
        rec["checked"] = checked
    rec["done"] = done
    status = "✅ ครบแล้ว" if done else "⏳ กำลังเช็ก"
    return (
        f"📦 {product_name}\n📅 วันที่: {label}\n"
        f"ต้องเช็ก: {int(target['total'])}\nเจอแล้ว: {checked}\nเหลือ: {remain}\nสถานะ: {status}"
    )


# ─── Product check actions ──────────────────────────────────────────────────

def _tg_open_product_check(chat_id, only_pending=False):
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    state["product_filter"] = "pending" if only_pending else "all"
    if not state.get("selected_carrier"):
        text, menu = _tg_render_carrier_list(chat_id)
        tg_state["pending_action"][str(chat_id)] = "pick_carrier"
        _tg_send(text, chat_id=chat_id, reply_markup=menu or _tg_main_menu())
        return
    text, menu = _tg_render_product_list(chat_id, only_pending=only_pending)
    tg_state["pending_action"].pop(str(chat_id), None)
    _tg_send(text, chat_id=chat_id, reply_markup=menu or _tg_main_menu())

def _tg_select_product(chat_id, product_name):
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    state["selected_product"] = product_name
    tg_state["pending_action"].pop(str(chat_id), None)
    _tg_save()
    _tg_send(_tg_render_product_detail(chat_id, product_name), chat_id=chat_id, reply_markup=_tg_product_detail_menu())

def _tg_apply_product_increment(chat_id, step):
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    product_name = state.get("selected_product") or ""
    if not product_name:
        _tg_open_product_check(chat_id)
        return
    day_key, _ = _tg_check_day_key(chat_id)
    carrier = state.get("selected_carrier", "")
    s, e, _ = _tg_resolve_range("", chat_id=chat_id)
    products = _tg_extract_daily_product_summary(_get_orders_for_range(s, e), carrier_filter=carrier)
    target = next((x for x in products if x["name"] == product_name), None)
    if target is None:
        for x in products:
            if product_name.strip().startswith(x["name"].strip()) or x["name"].strip().startswith(product_name.strip()):
                target = x
                product_name = x["name"]
                break
    if not target:
        _tg_send("ไม่พบสินค้านี้แล้ว ลองเลือกใหม่อีกครั้ง", chat_id=chat_id, reply_markup=_tg_main_menu())
        return
    rec = _tg_get_product_progress(day_key, product_name, target["total"])
    checked = min(int(target["total"]), max(0, int(rec.get("checked", 0) or 0) + int(step or 0)))
    rec["checked"] = checked
    rec["done"] = checked >= int(target["total"])
    _tg_save()
    only_pending = state.get("product_filter") == "pending"
    text, menu = _tg_render_product_list(chat_id, only_pending=only_pending)
    _tg_send(text, chat_id=chat_id, reply_markup=menu)

def _tg_set_product_checked(chat_id, value=None, done=False):
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    product_name = state.get("selected_product") or ""
    if not product_name:
        _tg_open_product_check(chat_id)
        return
    day_key, _ = _tg_check_day_key(chat_id)
    carrier = state.get("selected_carrier", "")
    s, e, _ = _tg_resolve_range("", chat_id=chat_id)
    products = _tg_extract_daily_product_summary(_get_orders_for_range(s, e), carrier_filter=carrier)
    target = next((x for x in products if x["name"] == product_name), None)
    if target is None:
        for x in products:
            if product_name.strip().startswith(x["name"].strip()) or x["name"].strip().startswith(product_name.strip()):
                target = x
                product_name = x["name"]
                break
    if not target:
        _tg_send("ไม่พบสินค้านี้แล้ว ลองเลือกใหม่อีกครั้ง", chat_id=chat_id, reply_markup=_tg_main_menu())
        return
    rec = _tg_get_product_progress(day_key, product_name, target["total"])
    total = int(target["total"] or 0)
    if done:
        rec["checked"] = total
        rec["done"] = True
    else:
        checked = max(0, min(total, int(value or 0)))
        rec["checked"] = checked
        rec["done"] = checked >= total
    _tg_save()
    only_pending = state.get("product_filter") == "pending"
    text, menu = _tg_render_product_list(chat_id, only_pending=only_pending)
    _tg_send(text, chat_id=chat_id, reply_markup=menu)

def _tg_reset_product_checked(chat_id):
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    product_name = state.get("selected_product") or ""
    if not product_name:
        _tg_open_product_check(chat_id)
        return
    day_key, _ = _tg_check_day_key(chat_id)
    rec = _tg_get_product_progress(day_key, product_name, 0)
    rec["checked"] = 0
    rec["done"] = False
    _tg_save()
    _tg_send(_tg_render_product_detail(chat_id, product_name), chat_id=chat_id, reply_markup=_tg_product_detail_menu())


# ─── Telegram message processing ───────────────────────────────────────────

def _tg_process_message(chat_id, text):
    txt = str(text or "").strip()
    if not txt:
        return
    normalized_txt = txt.replace("\ufe0f", "").strip()
    state = tg_state["chat_state"].setdefault(str(chat_id), {})
    pending = tg_state["pending_action"].get(str(chat_id), "")

    if txt in ("/start", "/menu", "📋 เมนู") or normalized_txt == "เมนู" or normalized_txt.endswith(" เมนู"):
        tg_state["pending_action"].pop(str(chat_id), None)
        state["range_start"] = _now_bkk().strftime("%Y-%m-%d")
        state["range_end"] = _now_bkk().strftime("%Y-%m-%d")
        state["day_label"] = _now_bkk().strftime("%Y-%m-%d")
        state.pop("selected_product", None)
        state.pop("selected_carrier", None)
        state.pop("digest_province", None)
        state.pop("digest_district", None)
        state.pop("digest_carrier", None)
        state.pop("digest_mode", None)
        state.pop("digest_action", None)
        _tg_send("เลือกเมนูที่ต้องการได้เลย", chat_id=chat_id, reply_markup=_tg_main_menu())
        return

    if txt == "📅 เลือกวัน" or "เลือกวัน" in normalized_txt:
        tg_state["pending_action"][str(chat_id)] = "pick_day"
        _tg_send("เลือกช่วงวันที่ หรือพิมพ์วันที่แบบ YYYY-MM-DD", chat_id=chat_id, reply_markup=_tg_date_menu())
        return

    if txt in ("วันนี้", "เมื่อวาน", "3 วัน", "5 วัน", "7 วัน", "14 วัน", "15 วัน", "30 วัน") or re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
        s, e, label = _tg_resolve_range(txt, chat_id=chat_id)
        state["range_start"] = s
        state["range_end"] = e
        state["day_label"] = label
        tg_state["pending_action"].pop(str(chat_id), None)
        if pending in ("pick_day_check", "product_check"):
            _tg_start_area_selection(chat_id, "check_product")
        else:
            _tg_start_area_selection(chat_id, "summary")
        _tg_save()
        return

    if txt == "📊 ภาพรวม" or "ภาพรวม" in normalized_txt or "ดูสรุปรายรอบ" in normalized_txt:
        _tg_start_area_selection(chat_id, "summary")
        return

    if txt == "📦 คาดว่าจะถึง" or "คาดว่าจะถึง" in normalized_txt or "eta" in normalized_txt:
        _tg_send_eta(chat_id)
        return

    # ── ดูรวมทุกจังหวัด ──
    if txt == "🌐 ดูรวมทุกจังหวัด" or "ดูรวมทุกจังหวัด" in normalized_txt:
        s, e, label = _tg_resolve_range("", chat_id=chat_id)
        orders = _get_orders_for_range(s, e)
        state.pop("digest_province", None)
        state.pop("digest_district", None)
        state.pop("digest_carrier", None)
        _tg_show_carrier_step(chat_id, orders, label, state)
        return

    # ── เลือกจังหวัด (🏠 prefix) ──
    if txt.startswith("🏠 "):
        selected = txt[2:].strip()
        m = re.match(r"(.+?)\s*\(\d+\)$", selected)
        if m:
            selected = m.group(1).strip()
        state["digest_province"] = selected
        state.pop("digest_district", None)
        state.pop("digest_carrier", None)
        state["digest_mode"] = "pick_district"
        s, e, label = _tg_resolve_range("", chat_id=chat_id)
        orders = _get_orders_for_range(s, e)
        if selected == "ไม่ระบุพื้นที่":
            # ไม่มีอำเภอให้เลือก → ไปเลือกค่ายขนส่ง
            _tg_show_carrier_step(chat_id, orders, label, state)
        else:
            districts, no_dist = _tg_extract_districts(orders, selected)
            if districts:
                action_text = _tg_action_label(state.get("digest_action", "summary"))
                lines = [f"{action_text}\n📅 วันที่: {label}\n📍 จังหวัด: {selected}",
                         "เลือกอำเภอที่ต้องการดู หรือ ดูรวมทุกอำเภอ"]
                for dist, count in districts[:15]:
                    lines.append(f"• {dist} ({count} รายการ)")
                if no_dist > 0:
                    lines.append(f"• ไม่ระบุอำเภอ ({no_dist} รายการ)")
                _tg_send("\n".join(lines), chat_id=chat_id,
                         reply_markup=_tg_district_menu(districts, selected, no_dist))
            else:
                # ไม่มีอำเภอ → ไปเลือกค่ายขนส่ง
                _tg_show_carrier_step(chat_id, orders, label, state)
        _tg_save()
        return

    # ── ดูรวมทุกอำเภอ ──
    if txt.startswith("🌐 ดูรวมทุกอำเภอ") or "ดูรวมทุกอำเภอ" in normalized_txt:
        province = state.get("digest_province", "")
        if not province:
            m = re.search(r"\((.+?)\)", txt)
            if m:
                province = m.group(1).strip()
        state["digest_province"] = province
        state.pop("digest_district", None)
        state.pop("digest_carrier", None)
        s, e, label = _tg_resolve_range("", chat_id=chat_id)
        orders = _get_orders_for_range(s, e)
        _tg_show_carrier_step(chat_id, orders, label, state)
        return

    # ── เลือกอำเภอ (📍 prefix) ──
    if txt.startswith("📍 "):
        selected = txt[2:].strip()
        m = re.match(r"(.+?)\s*\(\d+\)$", selected)
        if m:
            selected = m.group(1).strip()
        province = state.get("digest_province", "")
        state["digest_district"] = selected
        state.pop("digest_carrier", None)
        s, e, label = _tg_resolve_range("", chat_id=chat_id)
        orders = _get_orders_for_range(s, e)
        _tg_show_carrier_step(chat_id, orders, label, state)
        return

    # ── ดูรวมทุกค่ายขนส่ง ──
    if txt == "🌐 ดูรวมทุกค่ายขนส่ง" or "ดูรวมทุกค่ายขนส่ง" in normalized_txt:
        state.pop("digest_carrier", None)
        _tg_execute_filtered_action(chat_id)
        return

    # ── เลือกค่ายขนส่ง (🚛 prefix) ──
    if txt.startswith("🚛 "):
        selected = txt[2:].strip()
        m = re.match(r"(.+?)\s*\(\d+\)$", selected)
        if m:
            selected = m.group(1).strip()
        state["digest_carrier"] = selected
        _tg_execute_filtered_action(chat_id)
        return

    # ── กลับเลือกอำเภอ ──
    if txt == "⬅️ กลับเลือกอำเภอ" or "กลับเลือกอำเภอ" in normalized_txt:
        province = state.get("digest_province", "")
        state.pop("digest_district", None)
        state.pop("digest_carrier", None)
        state["digest_mode"] = "pick_district"
        s, e, label = _tg_resolve_range("", chat_id=chat_id)
        orders = _get_orders_for_range(s, e)
        action_text = _tg_action_label(state.get("digest_action", "summary"))
        if province and province != "ไม่ระบุพื้นที่":
            districts, no_dist = _tg_extract_districts(orders, province)
            if districts:
                lines = [f"{action_text}\n📅 วันที่: {label}\n📍 จังหวัด: {province}",
                         "เลือกอำเภอที่ต้องการดู หรือ ดูรวมทุกอำเภอ"]
                for dist, count in districts[:15]:
                    lines.append(f"• {dist} ({count} รายการ)")
                if no_dist > 0:
                    lines.append(f"• ไม่ระบุอำเภอ ({no_dist} รายการ)")
                _tg_send("\n".join(lines), chat_id=chat_id,
                         reply_markup=_tg_district_menu(districts, province, no_dist))
                return
        # fallback: กลับไปเลือกจังหวัด
        state.pop("digest_province", None)
        provinces, no_area = _tg_extract_provinces(orders)
        if provinces:
            lines = [f"{action_text}\n📅 วันที่: {label}", "เลือกจังหวัดที่ต้องการดู หรือ ดูรวมทุกจังหวัด"]
            for prov, count in provinces[:15]:
                lines.append(f"• {prov} ({count} รายการ)")
            if no_area > 0:
                lines.append(f"• ไม่ระบุพื้นที่ ({no_area} รายการ)")
            _tg_send("\n".join(lines), chat_id=chat_id, reply_markup=_tg_province_menu(provinces, no_area))
        else:
            _tg_execute_filtered_action(chat_id)
        return

    # ── กลับเลือกจังหวัด ──
    if txt == "⬅️ กลับเลือกจังหวัด" or "กลับเลือกจังหวัด" in normalized_txt:
        state.pop("digest_province", None)
        state.pop("digest_district", None)
        state.pop("digest_carrier", None)
        state["digest_mode"] = "pick_province"
        s, e, label = _tg_resolve_range("", chat_id=chat_id)
        orders = _get_orders_for_range(s, e)
        provinces, no_area = _tg_extract_provinces(orders)
        action_text = _tg_action_label(state.get("digest_action", "summary"))
        if provinces:
            lines = [f"{action_text}\n📅 วันที่: {label}", "เลือกจังหวัดที่ต้องการดู หรือ ดูรวมทุกจังหวัด"]
            for prov, count in provinces[:15]:
                lines.append(f"• {prov} ({count} รายการ)")
            if no_area > 0:
                lines.append(f"• ไม่ระบุพื้นที่ ({no_area} รายการ)")
            _tg_send("\n".join(lines), chat_id=chat_id, reply_markup=_tg_province_menu(provinces, no_area))
        else:
            _tg_execute_filtered_action(chat_id)
        return

    if txt == "🚚 รายการจัดส่ง" or "รายการจัดส่ง" in normalized_txt or "คัดลอกสินค้าแยกขนส่ง" in normalized_txt:
        _tg_start_area_selection(chat_id, "carrier_products")
        return

    # ── 🚚 ตรวจรอบส่ง — สรุปรอบส่งของวันที่เลือก (default = วันนี้) ──
    if txt == "🚚 ตรวจรอบส่ง" or "ตรวจรอบส่ง" in normalized_txt:
        # ใช้ range ที่ user เลือกไว้ ถ้าเป็นช่วงวันให้ใช้วันสิ้นสุด, ถ้าไม่มีให้ใช้วันนี้
        target = state.get("range_end") or _now_bkk().strftime('%Y-%m-%d')
        _tg_send_delivery_trips(chat_id, target_date=target)
        return

    if txt == "🔎 ค้นหา" or normalized_txt == "ค้นหา" or "ค้นหาสินค้า" in normalized_txt:
        tg_state["pending_action"][str(chat_id)] = "search"
        _tg_send("พิมพ์ชื่อสินค้า / เลขออเดอร์ / เลขพัสดุ ที่ต้องการค้นหา", chat_id=chat_id, reply_markup=_tg_main_menu())
        return

    if txt == "📈 สถิติ" or normalized_txt == "สถิติ" or "กราฟสถิติ" in normalized_txt:
        _tg_start_area_selection(chat_id, "chart")
        return

    if txt == "💰 ยอดวันนี้" or "ยอดวันนี้" in normalized_txt or "ยอดรายการวันนี้" in normalized_txt or "ยอดขายวันนี้" in normalized_txt:
        _tg_start_area_selection(chat_id, "sales_day")
        return

    if txt == "💰 ยอดสัปดาห์" or "ยอดสัปดาห์" in normalized_txt or "ยอดรายการสัปดาห์" in normalized_txt or "ยอดขายสัปดาห์" in normalized_txt:
        _tg_start_area_selection(chat_id, "sales_week")
        return

    if txt == "✅ ตรวจนับสินค้า" or "ตรวจนับสินค้า" in normalized_txt or "เช็กสินค้า" in normalized_txt or "เช็คสินค้า" in normalized_txt:
        _tg_start_area_selection(chat_id, "check_product")
        return

    if txt.startswith("🚚 "):
        selected = txt[2:].strip()
        m = re.match(r"(.+?)\s*\((\d+)\)$", selected)
        if m:
            selected = m.group(1).strip()
        state["selected_carrier"] = selected
        state.pop("selected_product", None)
        _tg_open_product_check(chat_id, only_pending=(state.get("product_filter") == "pending"))
        _tg_save()
        return

    if txt == "🟡 ยังไม่ครบ" or "ยังไม่ครบ" in normalized_txt:
        state.pop("selected_product", None)
        _tg_open_product_check(chat_id, only_pending=True)
        return

    if txt == "📚 ทั้งหมด" or normalized_txt == "ทั้งหมด":
        state.pop("selected_product", None)
        _tg_open_product_check(chat_id, only_pending=False)
        return

    if txt == "พิมพ์วันที่ YYYY-MM-DD" or "พิมพ์วันที่" in normalized_txt:
        if pending == "product_check":
            tg_state["pending_action"][str(chat_id)] = "pick_day_check"
        else:
            tg_state["pending_action"][str(chat_id)] = "pick_day"
        _tg_send("พิมพ์วันที่ที่ต้องการ เช่น 2026-03-11", chat_id=chat_id, reply_markup=_tg_date_menu())
        return

    if txt == "⬅️ กลับรายการ" or "กลับรายการ" in normalized_txt:
        state.pop("selected_product", None)
        _tg_open_product_check(chat_id)
        return

    if txt in ("+1", "+5", "+10"):
        _tg_apply_product_increment(chat_id, int(txt.replace("+", "") or 0))
        return

    if txt == "✅ ครบแล้ว" or "ครบแล้ว" in normalized_txt:
        _tg_set_product_checked(chat_id, done=True)
        return

    if txt == "♻️ รีเซ็ตยอดเช็ก" or "รีเซ็ตยอดเช็ก" in normalized_txt:
        _tg_reset_product_checked(chat_id)
        return

    if txt == "✍️ พิมพ์จำนวน" or "พิมพ์จำนวน" in normalized_txt:
        product_name = state.get("selected_product") or ""
        if not product_name:
            _tg_open_product_check(chat_id)
            return
        tg_state["pending_action"][str(chat_id)] = "input_product_count"
        _tg_send(f"พิมพ์จำนวนที่เจอสำหรับ {product_name}", chat_id=chat_id, reply_markup=_tg_product_detail_menu())
        return

    if pending == "search":
        s, e, _ = _tg_resolve_range("", chat_id=chat_id)
        tg_state["pending_action"].pop(str(chat_id), None)
        _tg_send(_tg_search_orders(txt, s, e), chat_id=chat_id, reply_markup=_tg_main_menu())
        return

    if pending == "pick_day":
        _tg_send("เลือกช่วงวันที่จากเมนู หรือพิมพ์วันที่แบบ YYYY-MM-DD", chat_id=chat_id, reply_markup=_tg_date_menu())
        return

    if pending in ("pick_day_check", "product_check"):
        _tg_send("เลือกช่วงวันที่จากเมนู หรือพิมพ์วันที่แบบ YYYY-MM-DD เพื่อตรวจนับสินค้า", chat_id=chat_id, reply_markup=_tg_date_menu())
        return

    if pending == "input_product_count":
        if re.fullmatch(r"\d+", txt):
            tg_state["pending_action"].pop(str(chat_id), None)
            _tg_set_product_checked(chat_id, value=int(txt))
        else:
            _tg_send("พิมพ์เป็นตัวเลขจำนวนชิ้น เช่น 35", chat_id=chat_id, reply_markup=_tg_product_detail_menu())
        return

    if txt.startswith("📦 "):
        selected = txt[2:].strip()
        m = re.match(r"(.+?)\s*\((\d+)/(\d+)\)\s*✅?$", selected)
        if m:
            selected = m.group(1).strip()
        _tg_select_product(chat_id, selected)
        return

    _tg_send("ถ้าจะใช้งานเมนู กด 📋 เมนู ได้เลย", chat_id=chat_id, reply_markup=_tg_main_menu())


# ─── Post-sync hook (notifications & digest) ────────────────────────────────

def _tg_handle_post_sync(total_scanned, total_new, total_updated, error_count,
                          login_error=False, imap_error=False, close_by_carrier=None):
    """เรียกหลังทุกรอบ sync เพื่อส่ง notification / digest ผ่าน Telegram"""
    _tg_load()  # reload settings ล่าสุด
    if not tg_state["enabled"] or not tg_state["bot_token"]:
        return

    acc = tg_state["digest_acc"]
    acc["scanned"] = acc.get("scanned", 0) + int(total_scanned or 0)
    acc["new"] = acc.get("new", 0) + int(total_new or 0)
    acc["updated"] = acc.get("updated", 0) + int(total_updated or 0)
    acc["errors"] = acc.get("errors", 0) + int(error_count or 0)
    acc["cycles"] = acc.get("cycles", 0) + 1
    if close_by_carrier:
        acc["closed"] = acc.get("closed", 0) + sum((close_by_carrier or {}).values())

    if tg_state["immediate_enabled"]:
        # แจ้งเตือนออเดอร์จัดส่งเสร็จสิ้นทีละรายการ
        try:
            _tg_notify_delivered_orders()
        except Exception:
            pass

        # Sync fail streak
        if tg_state["alert_sync_fail"]:
            if error_count > 0:
                tg_state["sync_fail_streak"] += 1
            else:
                if tg_state["sync_fail_streak"] > 0 and tg_state["notify_recovery"]:
                    _tg_notify_event("sync_recovery",
                        "✅ <Order Tracker> ระบบซิงก์กลับมาปกติแล้ว\nทุกอย่างทำงานต่อเนื่องได้ตามปกติ", force=True)
                tg_state["sync_fail_streak"] = 0
            if tg_state["sync_fail_streak"] >= max(1, int(tg_state["sync_fail_threshold"] or 3)):
                _tg_notify_event("sync_fail",
                    f"🚨 <Order Tracker | วิกฤตการซิงก์>\n"
                    f"ซิงก์ล้มเหลวต่อเนื่อง: {tg_state['sync_fail_streak']} รอบ\n"
                    f"สแกนเมล: {total_scanned} | ใหม่: {total_new} | อัปเดต: {total_updated} | Error: {error_count}\n"
                    "แนะนำ: ตรวจ IMAP/อินเทอร์เน็ต/รหัสผ่าน")

        if tg_state["alert_imap_error"] and imap_error:
            _tg_notify_event("imap_error",
                f"⚠️ <Order Tracker | IMAP มีปัญหา>\nสแกนเมล: {total_scanned}\nจำนวน Error: {error_count}")

        if tg_state["alert_login_fail"] and login_error:
            _tg_notify_event("login_fail",
                "🔐 <Order Tracker | Login ไม่ผ่าน>\nกรุณาตรวจสอบอีเมล / App Password / IMAP Server")

        if tg_state["alert_order_anomaly"]:
            low = int(tg_state["order_low_threshold"] or 20)
            high = int(tg_state["order_high_threshold"] or 300)
            close_by_carrier = close_by_carrier or {}
            bad = []
            for c, cnt in sorted(close_by_carrier.items(), key=lambda kv: kv[0].lower()):
                if (cnt < low) or (cnt > high):
                    bad.append(f"- {c}: {cnt} ชิ้น")
            if bad:
                body = "\n".join(bad)
                _tg_notify_event("order_anomaly",
                    f"📉 <Order Tracker | ปิดพัสดุผิดปกติรายค่าย>\n"
                    f"ช่วงปกติที่ตั้งไว้: {low}-{high} ชิ้น/ค่าย/รอบ\n{body}")

    # Digest
    if tg_state["digest_enabled"]:
        sec = _tg_digest_seconds()
        now = int(_now_bkk().timestamp())
        if sec > 0 and (now - int(tg_state["last_digest_ts"] or 0) >= sec):
            today = _now_bkk().strftime("%Y-%m-%d")
            day_orders = _get_orders_for_range(today, today)
            has_movement = bool((acc.get('new', 0) or 0) > 0 or (acc.get('updated', 0) or 0) > 0)
            if tg_state["digest_only_on_movement"] and not has_movement:
                tg_state["last_digest_ts"] = now
                _tg_save()
                return
            msgs = _tg_build_digest_message(day_orders, today, scan_stats=acc)
            sent = False
            for m in msgs:
                if _tg_send(m):
                    sent = True
            if sent:
                tg_state["last_digest_ts"] = now
                tg_state["digest_acc"] = {"scanned": 0, "new": 0, "updated": 0,
                                          "errors": 0, "cycles": 0, "closed": 0,
                                          "must_check": 0, "in_transit": 0, "payment": 0.0}
                _tg_save()

    # ── 🔔 Smart ETA alerts ──
    try:
        _tg_check_eta_alerts()
    except Exception as e:
        slog(f"⚠️ ETA alert error: {e}")


def _tg_check_eta_alerts():
    """ตรวจ ETA แจ้งเตือนอัตโนมัติ: พัสดุเลยกำหนด + สรุปเช้า"""
    if not tg_state["enabled"] or not tg_state["bot_token"]:
        return
    now = _now_bkk()
    now_ts = int(now.timestamp())
    today_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_dt = today_dt - timedelta(days=1)
    tomorrow_dt = today_dt + timedelta(days=1)
    dayafter_dt = today_dt + timedelta(days=2)

    all_orders = store.list_all()
    buckets = _tg_build_eta_buckets(all_orders, today_dt, yesterday_dt, tomorrow_dt, dayafter_dt)

    # ── 1) แจ้งเตือนพัสดุเลยกำหนด (ทุก 4 ชม.) ──
    overdue_cnt = len(buckets["overdue"]["orders"])
    late_cnt = len(buckets["late"]["orders"])
    if overdue_cnt + late_cnt > 0:
        last_eta_alert = int(tg_state.get("last_eta_alert_ts", 0) or 0)
        if (now_ts - last_eta_alert) >= 4 * 3600:  # ทุก 4 ชั่วโมง
            overdue_rev = sum(_to_amount(o.total) for o in buckets["overdue"]["orders"])
            late_rev = sum(_to_amount(o.total) for o in buckets["late"]["orders"])
            msg = "⚠️ แจ้งเตือน ETA\n────────────────\n"
            if overdue_cnt:
                msg += f"🔴 เลยกำหนด: {overdue_cnt} รายการ (฿{overdue_rev:,.0f})\n"
            if late_cnt:
                msg += f"🟠 ช้ากว่ากำหนด: {late_cnt} รายการ (฿{late_rev:,.0f})\n"
            msg += f"\n⏰ {now.strftime('%H:%M')} — กด 📦 คาดว่าจะถึง เพื่อดูรายละเอียด"
            _tg_send(msg)
            tg_state["last_eta_alert_ts"] = now_ts
            _tg_save()

    # ── 2) สรุปเช้า "📦 คาดว่าจะถึงวันนี้" (ส่ง 1 ครั้ง/วัน ตอน 7:00-8:00) ──
    hour = now.hour
    today_str = now.strftime("%Y-%m-%d")
    last_morning = tg_state.get("last_eta_morning", "")
    if 7 <= hour < 8 and last_morning != today_str:
        today_cnt = len(buckets["today"]["orders"])
        tomorrow_cnt = len(buckets["tomorrow"]["orders"])
        if today_cnt > 0 or tomorrow_cnt > 0 or overdue_cnt > 0:
            today_rev = sum(_to_amount(o.total) for o in buckets["today"]["orders"])
            msg = (
                f"☀️ สรุปเช้า — {now.strftime('%d/%m/%Y')}\n"
                "────────────────\n"
            )
            if today_cnt:
                msg += f"🟢 คาดว่าถึงวันนี้: {today_cnt} รายการ (฿{today_rev:,.0f})\n"
                # แสดงรายการย่อ
                for o in buckets["today"]["orders"][:8]:
                    name = _short_product_name((o.products or [""])[0]) or "ไม่ระบุ"
                    area = getattr(o, "ship_area", "") or ""
                    msg += f"  • {name}"
                    if area:
                        msg += f" 📍{area.split(',')[0]}"
                    msg += "\n"
                if today_cnt > 8:
                    msg += f"  …และอีก {today_cnt - 8} รายการ\n"
            if overdue_cnt:
                msg += f"🔴 เลยกำหนด: {overdue_cnt} รายการ\n"
            if tomorrow_cnt:
                msg += f"🔵 พรุ่งนี้: {tomorrow_cnt} รายการ\n"
            msg += "\n📦 กดเพื่อดูรายละเอียดทั้งหมด"
            _tg_send(msg, reply_markup={"inline_keyboard": [
                [{"text": "📦 ดูรายละเอียด ETA", "callback_data": "eta:all"}]
            ]})
            tg_state["last_eta_morning"] = today_str
            _tg_save()


# ─── Telegram polling loop ──────────────────────────────────────────────────

def _tg_poll_loop():
    """Long-polling loop: รับคำสั่งจาก Telegram bot"""
    import time as _time
    _time.sleep(8)  # รอให้ Flask start ก่อน
    while True:
        try:
            _tg_load()  # reload settings
            if tg_state["enabled"] and tg_state["bot_token"]:
                params = {"timeout": 20, "allowed_updates": json.dumps(["message", "callback_query"])}
                if int(tg_state["last_update_id"] or 0) > 0:
                    params["offset"] = int(tg_state["last_update_id"]) + 1
                data = _tg_api_get("getUpdates", params, timeout=30)
                if data.get("ok"):
                    for upd in (data.get("result") or []):
                        tg_state["last_update_id"] = max(
                            int(tg_state["last_update_id"] or 0),
                            int(upd.get("update_id", 0) or 0))
                        # ── callback_query (inline button) ──
                        cb = upd.get("callback_query")
                        if cb:
                            cb_chat = str((cb.get("message") or {}).get("chat", {}).get("id", ""))
                            cb_data = cb.get("data", "")
                            cb_id = cb.get("id", "")
                            if cb_id:
                                _tg_api_get("answerCallbackQuery", {"callback_query_id": cb_id})
                            if cb_chat and cb_data:
                                _tg_handle_callback(cb_chat, cb_data)
                            continue
                        msg = upd.get("message") or {}
                        chat = msg.get("chat") or {}
                        chat_id = str(chat.get("id") or "")
                        text = msg.get("text") or ""
                        if chat_id and text:
                            _tg_process_message(chat_id, text)
                    _tg_save()
        except Exception as e:
            slog(f"⚠️ Telegram poll error: {e}")
        _time.sleep(3)

def start_telegram_polling():
    """เริ่ม background Telegram polling thread"""
    t = threading.Thread(target=_tg_poll_loop, daemon=True)
    t.start()

# ── Buffer เก็บออเดอร์ที่เพิ่งจัดส่งเสร็จสิ้นระหว่าง sync ──
_tg_delivered_orders_buffer = []


def _tg_notify_delivered_orders():
    """ส่งแจ้งเตือนเมื่อออเดอร์จัดส่งเสร็จสิ้น — รวมยอดเป็นชุดถ้าเยอะ กัน rate limit"""
    global _tg_delivered_orders_buffer
    orders_to_notify = list(_tg_delivered_orders_buffer)
    _tg_delivered_orders_buffer = []  # ล้าง buffer เสมอ กันหน่วยความจำรั่ว
    if not tg_state.get("alert_delivered_order", True):
        return
    if not tg_state["enabled"] or not tg_state["bot_token"]:
        return
    if not orders_to_notify:
        return

    INDIVIDUAL_LIMIT = 5  # ≤5 รายการ → ส่งทีละรายการ, >5 → รวมเป็นสรุป

    if len(orders_to_notify) <= INDIVIDUAL_LIMIT:
        # ── ส่งทีละรายการ (จำนวนน้อย ไม่โดน rate limit) ──
        for o in orders_to_notify:
            try:
                products = ", ".join((o.products or [])[:3])
                if len(o.products or []) > 3:
                    products += "..."
                msg = (
                    f"✅ จัดส่งเสร็จสิ้นแล้ว!\n"
                    f"────────────────\n"
                    f"🔢 {o.order_id}\n"
                    f"🏪 {o.shop}"
                )
                if o.merchant:
                    msg += f" — {o.merchant}"
                area = getattr(o, 'ship_area', '') or ''
                if area:
                    msg += f"\n📍 {area}"
                msg += f"\n📌 {o.status}"
                if products:
                    msg += f"\n📦 {products}"
                if o.total:
                    msg += f"\n💰 {o.total}"
                if o.tracking:
                    msg += f"\n🚚 {o.tracking}"
                    url = _tracking_url(o.tracking)
                    if url:
                        msg += f"\n🔗 เช็คพัสดุ: {url}"
                _tg_send(msg)
                time.sleep(0.1)  # หน่วงเล็กน้อย กัน rate limit
            except Exception:
                pass
    else:
        # ── รวมเป็นสรุป (จำนวนเยอะ กัน Telegram block) ──
        total_amount = sum(_to_amount(o.total) for o in orders_to_notify)

        # จัดกลุ่มตามค่ายขนส่ง
        carrier_groups = {}
        for o in orders_to_notify:
            carrier = _carrier_name(getattr(o, "tracking", ""))
            rec = carrier_groups.setdefault(carrier, {"count": 0, "amount": 0.0})
            rec["count"] += 1
            rec["amount"] += _to_amount(o.total)

        msg = (
            f"✅ จัดส่งเสร็จสิ้น {len(orders_to_notify)} รายการ!\n"
            f"💵 ยอดรวม: ฿{total_amount:,.2f}\n"
            f"────────────────\n"
        )
        for carrier in sorted(carrier_groups.keys()):
            rec = carrier_groups[carrier]
            msg += f"🚚 {carrier}: {rec['count']} รายการ | ฿{rec['amount']:,.2f}\n"

        # แสดงรายการทั้งหมด — รวมสินค้าซ้ำ
        msg += "────────────────\n"
        prod_map = {}
        for o in orders_to_notify:
            name = _short_product_name((o.products or [""])[0]) or "ไม่ระบุ"
            rec = prod_map.setdefault(name, {"count": 0, "total": 0, "trackings": []})
            rec["count"] += 1
            rec["total"] += _to_amount(o.total)
            trk = _extract_tracking_number(getattr(o, "tracking", ""))
            if trk and trk != "-":
                rec["trackings"].append(trk)
        for name, info in sorted(prod_map.items(), key=lambda kv: -kv[1]["count"]):
            qty = f" ── {info['count']} รายการ" if info['count'] > 1 else ""
            price = f"฿{info['total']:,.0f}" if info['total'] else ""
            line = f"  • {name} ({price}){qty}"
            if info["trackings"]:
                trk = info["trackings"][0]
                if len(info["trackings"]) > 1:
                    trk += f" +{len(info['trackings'])-1}"
                line += f"\n    📦 {trk}"
            msg += line + "\n"

        _tg_send(msg)


# ══════════════════════════════════════════════════════════════════════════════
#  Feature: สรุปยอดรายวัน / รายสัปดาห์
# ══════════════════════════════════════════════════════════════════════════════

def _tg_build_sales_summary(period="day", province=None, district=None, carrier=None):
    """สร้างข้อความสรุปยอด"""
    today = _now_bkk()
    if period == "week":
        start = (today - timedelta(days=6)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        title = f"📊 ยอดสัปดาห์\n📅 {start} ถึง {end}"
    else:
        start = end = today.strftime("%Y-%m-%d")
        title = f"📊 ยอดวันนี้\n📅 {start}"

    orders = _get_orders_for_range(start, end)

    # ── กรองตามจังหวัด/อำเภอ/ค่ายขนส่ง ──
    if province:
        orders = _tg_filter_orders_by_area(orders, province, district)
    if carrier:
        orders = [o for o in orders if _carrier_name(getattr(o, "tracking", "")) == carrier]

    # ── แสดง label พื้นที่ ──
    area_parts = []
    if province:
        area_parts.append(province)
    if district:
        area_parts.append(district)
    if carrier:
        area_parts.append(f"🚛 {carrier}")
    if area_parts:
        title += f"\n📍 {', '.join(area_parts)}"

    if not orders:
        return f"{title}\n────────────────\nไม่มีข้อมูลออเดอร์ในช่วงนี้"

    total_count = len(orders)
    closed_orders = [o for o in orders if _is_closed_status(o.status)]
    cancelled_orders = [o for o in orders if _is_cancelled_status(o.status)]
    in_transit = [o for o in orders if _is_in_transit_status(o.status)
                  and not _is_delivered_status(o.status) and not _is_cancelled_status(o.status)]

    revenue = sum(_to_amount(o.total) for o in closed_orders)
    cancelled_amount = sum(_to_amount(o.total) for o in cancelled_orders)
    total_all = sum(_to_amount(o.total) for o in orders if _to_amount(o.total) > 0)

    # แยกตามร้าน/แพลตฟอร์ม
    shop_stats = {}
    for o in orders:
        shop = o.shop or "อื่นๆ"
        rec = shop_stats.setdefault(shop, {"count": 0, "revenue": 0.0, "closed": 0, "cancelled": 0})
        rec["count"] += 1
        if _is_closed_status(o.status):
            rec["closed"] += 1
            rec["revenue"] += _to_amount(o.total)
        elif _is_cancelled_status(o.status):
            rec["cancelled"] += 1

    # Top สินค้าขายดี
    product_count = {}
    for o in closed_orders:
        for p in (o.products or [])[:1]:
            name = _short_product_name(p) or "ไม่ระบุ"
            product_count[name] = product_count.get(name, 0) + 1
    top_products = sorted(product_count.items(), key=lambda x: -x[1])

    # สรุปรายวัน (สำหรับ weekly)
    daily_lines = []
    if period == "week":
        daily_map = {}
        for o in orders:
            day = ""
            try:
                ts = int(o.last_update_ts or o.first_seen_ts or 0)
                if ts > 0:
                    day = _ts_to_bkk(ts).strftime("%Y-%m-%d")
            except Exception:
                pass
            if not day:
                day = o.date or "?"
            rec = daily_map.setdefault(day, {"count": 0, "revenue": 0.0})
            rec["count"] += 1
            if _is_closed_status(o.status):
                rec["revenue"] += _to_amount(o.total)
        for day in sorted(daily_map.keys()):
            d = daily_map[day]
            daily_lines.append(f"• {day}: {d['count']} ออเดอร์ | ฿{d['revenue']:,.0f}")

    msg = (
        f"{title}\n"
        "────────────────\n"
        f"📦 ออเดอร์ทั้งหมด : {total_count}\n"
        f"✅ ปิดงาน         : {len(closed_orders)}\n"
        f"🚚 กำลังจัดส่ง    : {len(in_transit)}\n"
        f"⛔ ยกเลิก         : {len(cancelled_orders)}\n"
        "────────────────\n"
        f"💵 ยอดปิดงาน      : ฿{revenue:,.2f}\n"
        f"💸 ยอดยกเลิก      : ฿{cancelled_amount:,.2f}\n"
        f"💰 ยอดรวมทั้งหมด  : ฿{total_all:,.2f}\n"
    )

    # ร้าน/แพลตฟอร์ม
    if shop_stats:
        msg += "────────────────\n🏪 แยกตามแพลตฟอร์ม\n"
        for shop in sorted(shop_stats.keys()):
            s = shop_stats[shop]
            msg += f"• {shop}: {s['count']} ออเดอร์ | ✅{s['closed']} | ⛔{s['cancelled']} | ฿{s['revenue']:,.0f}\n"

    # สรุปรายวัน
    if daily_lines:
        msg += "────────────────\n📅 แยกรายวัน\n" + "\n".join(daily_lines) + "\n"

    # Top สินค้า
    extra_msgs = []
    if top_products:
        top_text = "────────────────\n🏆 สินค้าขายดี\n"
        for i, (name, cnt) in enumerate(top_products, 1):
            qty = f" ── {cnt} รายการ" if cnt > 1 else ""
            top_text += f"  • {name}{qty}\n"
        if len(msg) + len(top_text) < 3800:
            msg += top_text
        else:
            chunks = _tg_split_long_text(top_text, 3800)
            extra_msgs.extend(chunks)

    return [msg] + extra_msgs


# ══════════════════════════════════════════════════════════════════════════════
#  Feature 3: กราฟ/สถิติส่งเป็นรูปภาพ
# ══════════════════════════════════════════════════════════════════════════════

def _tg_send_photo(photo_bytes, caption="", chat_id=None):
    """ส่งรูปภาพผ่าน Telegram sendPhoto API"""
    if not tg_state["enabled"] or not tg_state["bot_token"]:
        return False
    target = str(chat_id or tg_state["chat_id"])
    if not target:
        return False
    try:
        import io
        boundary = "----TgChartBoundary"
        body = io.BytesIO()

        def write(s):
            body.write(s.encode("utf-8") if isinstance(s, str) else s)

        write(f"--{boundary}\r\n")
        write(f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{target}\r\n')
        if caption:
            write(f"--{boundary}\r\n")
            write(f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n')
        write(f"--{boundary}\r\n")
        write(f'Content-Disposition: form-data; name="photo"; filename="chart.png"\r\n')
        write(f"Content-Type: image/png\r\n\r\n")
        write(photo_bytes)
        write(f"\r\n--{boundary}--\r\n")

        url = f"https://api.telegram.org/bot{tg_state['bot_token']}/sendPhoto"
        req_obj = urllib.request.Request(
            url, data=body.getvalue(), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}
        )
        with urllib.request.urlopen(req_obj, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        result = json.loads(raw)
        return result.get("ok", False)
    except Exception as e:
        slog(f"⚠️ Telegram sendPhoto error: {e}")
        return False


def _tg_generate_status_chart(orders, day_label):
    """สร้างกราฟแท่งสถานะออเดอร์ → PNG bytes"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
        import io

        # นับสถานะ
        cats = {
            "กำลังเตรียม": 0, "กำลังจัดส่ง": 0, "ต้องเช็ค": 0,
            "ปิดงาน": 0, "ยกเลิก": 0, "ค้างนาน": 0,
        }
        alert_hours = int(tg_state.get("alert_hours", 48) or 48)
        now_ts = int(_now_bkk().timestamp())
        for o in (orders or []):
            st = o.status or ""
            if _is_cancelled_status(st):
                cats["ยกเลิก"] += 1
            elif _is_must_check_status(st):
                cats["ต้องเช็ค"] += 1
            elif _is_closed_status(st):
                cats["ปิดงาน"] += 1
            elif _is_in_transit_status(st):
                cats["กำลังจัดส่ง"] += 1
                ref = int(o.last_update_ts or o.first_seen_ts or 0)
                if ref > 0 and (now_ts - ref) >= (alert_hours * 3600):
                    cats["ค้างนาน"] += 1
            elif "กำลังเตรียม" in st:
                cats["กำลังเตรียม"] += 1

        labels = list(cats.keys())
        values = list(cats.values())
        colors = ["#4da6ff", "#ff9f43", "#ffd166", "#10b981", "#ff4f6d", "#b47fff"]

        fig, ax = plt.subplots(figsize=(8, 4.5), facecolor="#0f1623")
        ax.set_facecolor("#0f1623")
        bars = ax.bar(labels, values, color=colors, width=0.6, edgecolor="none")
        for bar, val in zip(bars, values):
            if val > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                        str(val), ha="center", va="bottom", color="white",
                        fontsize=14, fontweight="bold")
        ax.set_title(f"📦 Order Tracker — {day_label}", color="white", fontsize=14, pad=12)
        ax.tick_params(colors="white", labelsize=10)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color("#2a3d5c")
        ax.spines["bottom"].set_color("#2a3d5c")
        ax.yaxis.label.set_color("white")
        plt.tight_layout()

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        slog(f"⚠️ Chart generation error: {e}")
        return None


def _tg_generate_daily_trend_chart(num_days=7):
    """สร้างกราฟ trend ออเดอร์ย้อนหลัง N วัน → PNG bytes"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import io

        today = _now_bkk()
        days = []
        closed_vals = []
        cancelled_vals = []
        transit_vals = []
        revenue_vals = []

        for i in range(num_days - 1, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            orders = _get_orders_for_range(d, d)
            closed = len([o for o in orders if _is_closed_status(o.status)])
            cancelled = len([o for o in orders if _is_cancelled_status(o.status)])
            transit = len([o for o in orders if _is_in_transit_status(o.status)
                           and not _is_delivered_status(o.status) and not _is_cancelled_status(o.status)])
            rev = sum(_to_amount(o.total) for o in orders if _is_closed_status(o.status))
            days.append(d[-5:])  # MM-DD
            closed_vals.append(closed)
            cancelled_vals.append(cancelled)
            transit_vals.append(transit)
            revenue_vals.append(rev)

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9, 7), facecolor="#0f1623",
                                        gridspec_kw={"height_ratios": [1.2, 1]})

        # กราฟบน: จำนวนออเดอร์
        x = range(len(days))
        w = 0.25
        ax1.set_facecolor("#0f1623")
        ax1.bar([i - w for i in x], closed_vals, w, label="ปิดงาน", color="#10b981")
        ax1.bar(x, transit_vals, w, label="จัดส่ง", color="#4da6ff")
        ax1.bar([i + w for i in x], cancelled_vals, w, label="ยกเลิก", color="#ff4f6d")
        ax1.set_xticks(list(x))
        ax1.set_xticklabels(days, color="white", fontsize=9)
        ax1.tick_params(colors="white")
        ax1.set_title("📦 จำนวนออเดอร์รายวัน", color="white", fontsize=13, pad=10)
        ax1.legend(loc="upper left", fontsize=9, facecolor="#1d2a44", edgecolor="#2a3d5c",
                   labelcolor="white")
        ax1.spines["top"].set_visible(False)
        ax1.spines["right"].set_visible(False)
        ax1.spines["left"].set_color("#2a3d5c")
        ax1.spines["bottom"].set_color("#2a3d5c")

        # กราฟล่าง: ยอดขาย
        ax2.set_facecolor("#0f1623")
        ax2.fill_between(list(x), revenue_vals, alpha=0.3, color="#ffd166")
        ax2.plot(list(x), revenue_vals, color="#ffd166", linewidth=2, marker="o", markersize=5)
        for i, v in enumerate(revenue_vals):
            if v > 0:
                ax2.text(i, v + max(revenue_vals) * 0.03, f"฿{v:,.0f}",
                         ha="center", va="bottom", color="#ffd166", fontsize=8)
        ax2.set_xticks(list(x))
        ax2.set_xticklabels(days, color="white", fontsize=9)
        ax2.tick_params(colors="white")
        ax2.set_title("💵 ยอดรายวัน (ปิดงาน)", color="white", fontsize=13, pad=10)
        ax2.spines["top"].set_visible(False)
        ax2.spines["right"].set_visible(False)
        ax2.spines["left"].set_color("#2a3d5c")
        ax2.spines["bottom"].set_color("#2a3d5c")

        plt.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:
        slog(f"⚠️ Trend chart error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  Email / IMAP Sync
# ══════════════════════════════════════════════════════════════════════════════

def _decode_str(s):
    """Decode email header string (multi-part)."""
    if not s: return ""
    parts = decode_header(s)
    out = []
    for raw, enc in parts:
        if isinstance(raw, bytes):
            try:
                out.append(raw.decode(enc or "utf-8", errors="ignore"))
            except Exception:
                try:
                    out.append(raw.decode("utf-8", errors="ignore"))
                except Exception:
                    out.append(raw.decode("iso-8859-1", errors="ignore"))
        else:
            out.append(str(raw))
    return "".join(out)


def _extract_body(msg):
    """
    Extract text body from email message.
    ลำดับ: text/plain ก่อน, html fallback ถ้าไม่มี plain
    (เหมือนต้นฉบับ parse_email ใน app.py)
    """
    import html as html_lib
    body = ""
    html_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                txt = payload.decode(charset, errors="ignore")
            except Exception:
                txt = payload.decode("utf-8", errors="ignore")

            if ctype == "text/plain" and not body:
                body = txt
            elif ctype == "text/html" and not html_body:
                html_body = txt
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            try:
                body = payload.decode("utf-8", errors="ignore")
            except Exception:
                body = str(payload)

    # html fallback (Gmail บางฉบับไม่มี text/plain)
    if not body and html_body:
        import html as html_lib
        cleaned = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "\n", html_body)
        cleaned = re.sub(r"(?i)<br\s*/?>", "\n", cleaned)
        cleaned = re.sub(r"(?i)</(p|div|tr|li|h[1-6])>", "\n", cleaned)
        cleaned = re.sub(r"(?i)<td[^>]*>", " ", cleaned)
        cleaned = re.sub(r"(?s)<[^>]+>", " ", cleaned)
        cleaned = html_lib.unescape(cleaned)
        norm_lines = []
        for ln in cleaned.splitlines():
            ln = re.sub(r"\s+", " ", ln).strip()
            if ln:
                norm_lines.append(ln)
        body = "\n".join(norm_lines)

    return body


def _parse_email(raw_bytes, account_email=""):
    """
    Parse email → Order
    Port จาก parse_email() ใน app.py ต้นฉบับ
    """
    try:
        msg = email.message_from_bytes(raw_bytes)

        # ── Subject ──────────────────────────────────────────────
        subject = _decode_str(msg.get("Subject", ""))
        from_   = msg.get("From", "")
        subject_lower = subject.lower()
        from_lower    = from_.lower()

        # ── Timestamp ────────────────────────────────────────────
        received_ts = int(time.time())
        try:
            dt = parsedate_to_datetime(msg.get("Date", ""))
            if dt is not None:
                received_ts = int(dt.timestamp())
        except Exception:
            pass
        date_str = _ts_to_bkk(received_ts).strftime("%Y-%m-%d")

        # ── Body ─────────────────────────────────────────────────
        body = _extract_body(msg)
        body_lower = body.lower()

        # ── กรอง: เฉพาะอีเมลที่เกี่ยวกับออเดอร์ ────────────────
        order_keywords = [
            "order", "คำสั่งซื้อ", "สั่งซื้อ", "confirmed",
            "การสั่งซื้อ", "ชำระเงิน", "payment", "invoice", "receipt",
            "tiktok", "จัดส่ง", "การขนส่ง", "shipped", "delivered",
        ]
        shop_domains = [
            "lazada", "shopee", "tiktok", "shop.tiktok",
            "amazon", "central", "powerbuy", "bigc", "makro",
        ]
        is_order = (
            any(k in subject_lower for k in order_keywords)
            or any(d in from_lower  for d in shop_domains)
            or any(d in body_lower  for d in shop_domains)
        )
        if not is_order:
            return None

        # ── Shop Detection ───────────────────────────────────────
        def _in(kw, *targets):
            return any(kw in t for t in targets)

        if _in("tiktok", subject_lower, from_lower, body_lower):
            shop = "TikTok Shop"
        elif _in("shopee", subject_lower, from_lower, body_lower):
            shop = "Shopee"
        elif _in("lazada", subject_lower, from_lower, body_lower):
            shop = "Lazada"
        elif _in("amazon", subject_lower, from_lower):
            shop = "Amazon"
        elif _in("central", subject_lower, from_lower):
            shop = "Central"
        elif _in("powerbuy", subject_lower, from_lower):
            shop = "Powerbuy"
        elif _in("bigc", subject_lower, from_lower):
            shop = "Big C"
        elif _in("makro", subject_lower, from_lower):
            shop = "Makro"
        else:
            shop = "อื่นๆ"

        lines = [ln.strip() for ln in body.splitlines() if ln.strip()]

        # ── Merchant (ชื่อร้าน) ──────────────────────────────────────
        merchant = ""
        _bad = {"ดูรายละเอียด","TikTok","คำสั่งซื้อ","ยอดรวม","การจัดส่ง",
                "สรุป","ทีม","ยกเลิก","ที่อยู่","อย่าตอบกลับ","ติดต่อ"}

        def _valid_merchant(s):
            s = s.strip()
            if not s or len(s) > 60 or len(s) < 2: return False
            if any(x in s for x in _bad): return False
            # กรอง: เป็นตัวเลขล้วน หรือ URL หรือ emoji เดี่ยว
            if re.fullmatch(r'[\d\s\.\-\+\(\)]+', s): return False
            if s.startswith('http'): return False
            return True

        if shop == "TikTok Shop":
            # Pattern 1: บรรทัดถัดจาก "ทีม TikTok Shop" (confirmation/update emails)
            for i, ln in enumerate(lines):
                if "ทีม TikTok Shop" in re.sub(r'\s+', ' ', ln):
                    for j in range(i + 1, min(i + 6, len(lines))):
                        cand = lines[j].strip()
                        if _valid_merchant(cand):
                            merchant = cand
                            break
                    break

            # Pattern 2: บรรทัดก่อน "คำสั่งซื้อ X/X" (shipping/delivered emails)
            if not merchant:
                for i, ln in enumerate(lines):
                    if re.search(r'คำสั่งซื้อ\s+\d+\s*/\s*\d+', ln):
                        # มองย้อนหลัง 1-3 บรรทัด
                        for back in range(1, 4):
                            idx = i - back
                            if idx < 0: break
                            cand = lines[idx].strip()
                            if _valid_merchant(cand):
                                merchant = cand
                                break
                        break

        # fallback ทั่วไป: บรรทัดที่มี "Store" / "shop" (English names)
        if not merchant:
            for ln in lines:
                if re.search(r'\b(Store|shop|SHOP)\b', ln) \
                        and "TikTok" not in ln and len(ln.strip()) < 60:
                    merchant = ln.strip()
                    break

        # fallback: regex ร้านค้า:
        if not merchant:
            m = re.search(r"ร้าน[ค้า]*\s*[:\s]\s*([^\n]{2,40})", body)
            if m:
                merchant = m.group(1).strip()

        # ── Order ID ─────────────────────────────────────────────
        order_id = ""

        # 1) ระบุชัดเจนด้วย "หมายเลขคำสั่งซื้อ" หรือ "คำสั่งซื้อ XXXXXX" (ใน body)
        m = re.search(r"หมายเลขคำสั่งซื้อ\s*([0-9]{10,25})", body)
        if m:
            order_id = m.group(1)

        # 2) "มีปัญหากับคำสั่งซื้อ XXXXXX" (cancel refund email)
        if not order_id:
            m = re.search(r"(?:คำสั่งซื้อ|order\s*(?:id|#)?)\s*([0-9]{10,25})", body, re.IGNORECASE)
            if m:
                order_id = m.group(1)

        # 3) "หมายเลขคำขอ" = refund request ID → ใช้เป็น order_id fallback (cancel emails)
        #    แต่ต้องแยก: ถ้ามีเลข 15+ หลักอื่นในเมลให้เอาอันนั้นก่อน
        all_long = re.findall(r"(?<!\d)(\d{15,25})(?!\d)", body)
        # กรองเลข "หมายเลขคำขอ" ออก (refund request ID)
        refund_ids = set(re.findall(r"หมายเลขคำขอ\s*(\d{10,25})", body))
        real_order_ids = [x for x in all_long if x not in refund_ids]

        if not order_id and real_order_ids:
            order_id = real_order_ids[0]
        if not order_id and all_long:
            order_id = all_long[0]  # fallback: เอาตัวแรกที่เจอ (รวม refund ID)

        # 4) 10-14 หลัก (Shopee, others)
        if not order_id:
            m = re.search(r"(?<!\d)(\d{10,14})(?!\d)", body + " " + subject)
            if m:
                order_id = m.group(1)

        # 5) Order# alphanumeric
        if not order_id:
            m = re.search(r"[Oo]rder[\s#:]*([A-Z0-9\-]{6,25})", subject + " " + body[:500])
            if m:
                order_id = m.group(1)

        if not order_id:
            return None

        # ── วันที่สั่งซื้อ ────────────────────────────────────────
        order_date = date_str  # default = received date
        m = re.search(r"วันที่สั่งซื้อ\s*(.+)", body)
        if m:
            order_date = m.group(1).strip()[:20]

        # ── Status (ใช้ logic ต้นฉบับ: เช็ค "ยกเลิก" ให้แม่นก่อน) ──
        status = "รอดำเนินการ"

        # ── ตรวจ "ยกเลิก" อย่างแม่นยำ (กัน footer unsubscribe) ──────
        UNSUBSCRIBE_PAT = r"ยกเลิก(การรับข่าวสาร|การสมัคร|รับอีเมล|subscription|การสมัครรับ)"

        # phrase ที่ยืนยันว่าเป็น cancel order จริงๆ
        cancel_phrases = [
            "ถูกยกเลิกแล้ว",
            "รายการถูกยกเลิกแล้ว",
            "คำสั่งซื้อของคุณถูกยกเลิก",
            "ยกเลิกคำสั่งซื้อแล้ว",
            "คำสั่งซื้อถูกยกเลิก",
            "ออเดอร์ถูกยกเลิก",
            "คำขอยกเลิก",
            "ยกเลิกสำเร็จ",
        ]
        cancel_th = any(p in body for p in cancel_phrases)
        cancel_th_re = bool(re.search(
            r"(คำสั่งซื้อ|ออเดอร์).{0,30}ยกเลิก|ยกเลิก.{0,30}(คำสั่งซื้อ|ออเดอร์)", body
        ))
        cancel_en = bool(re.search(
            r"(order).{0,30}cancel(l)?ed|(cancel(l)?ed).{0,30}(order)", body_lower
        ))
        cancel_subject = bool(re.search(r"ยกเลิก|cancel", subject_lower))
        is_unsubscribe_only = bool(re.search(UNSUBSCRIBE_PAT, body)) and not cancel_th

        is_cancelled = (
            cancel_th or cancel_th_re or cancel_en or cancel_subject
        ) and not is_unsubscribe_only

        if is_cancelled:
            # แยก: ผู้ซื้อยกเลิกเอง vs ร้านค้า/ระบบยกเลิก
            buyer_cancel_phrases = [
                "คำขอยกเลิก", "ยกเลิกสำเร็จ",
                "คุณได้ยกเลิก", "ผู้ซื้อยกเลิก",
                "cancellation request", "you cancelled",
            ]
            is_buyer_cancel = any(p in body or p in body_lower for p in buyer_cancel_phrases)
            status = "ยกเลิก(ผู้ซื้อ)" if is_buyer_cancel else "ยกเลิก"
        elif (
            "คำสั่งซื้อของคุณมาถึงแล้ว" in body
            or "ข้อมูลการส่งมอบ" in body
            or "จัดส่งเมื่อ" in body
            or "ส่งสำเร็จ" in body
            or "ส่งสำเร็จ" in subject
            or "จัดส่งเสร็จสิ้นแล้ว" in body
            or "จัดส่งเสร็จสิ้นแล้ว" in subject
            or "มาถึงแล้ว" in subject
            or "delivered" in body_lower
        ):
            status = "จัดส่งเสร็จสิ้นแล้ว"
        elif (
            "เพิ่งจัดส่ง" in subject
            or "ได้รับการจัดส่งแล้ว" in subject
            or "อยู่ระหว่างการขนส่ง" in body
            or "อยู่ระหว่างการจัดส่ง" in body
            or "กำลังจัดส่ง" in body
            or "กำลังจัดส่ง" in subject
            or "out for delivery" in body_lower
            or "shipped" in body_lower
        ):
            status = "กำลังจัดส่ง"
        elif "จัดส่งแล้ว" in body or "จัดส่งแล้ว" in subject:
            status = "จัดส่งแล้ว"
        elif "กำลังเตรียม" in body or "เตรียมพัสดุ" in body or "กำลังแพ็ค" in body or "เตรียมจัดส่ง" in body or "อยู่ระหว่างการเตรียม" in body:
            status = "กำลังเตรียม"
        elif "รอจัดส่ง" in body or "พร้อมจัดส่ง" in body or "ready to ship" in body_lower:
            status = "รอจัดส่ง"

        # ── Tracking ─────────────────────────────────────────────
        tracking = None
        carrier_patterns = [
            r"(Flash\s*Express[^:\n]{0,80})[:：]\s*([A-Z0-9\-]{8,40})",
            r"(J\s*&?\s*T[^:\n]{0,80})[:：]\s*([A-Z0-9\-]{8,40})",
            r"(J\s*&?\s*T[^:\n]{0,80})[:：]\s*([0-9]{10,30})",
            r"(KEX\s*Express[^:\n]{0,80})[:：]\s*([A-Z0-9\-]{8,40})",
            r"(Kerry[^:\n]{0,80})[:：]\s*([A-Z0-9\-]{8,40})",
            r"(Ninja\s*Van[^:\n]{0,80})[:：]\s*([A-Z0-9\-]{8,40})",
            r"(SPX[^:\n]{0,80})[:：]\s*([A-Z0-9\-]{8,40})",
            r"(Thailand\s*Post[^:\n]{0,80})[:：]\s*([A-Z0-9\-]{8,30})",
        ]
        for pat in carrier_patterns:
            mm = re.search(pat, body, re.IGNORECASE)
            if mm:
                carrier = re.sub(r"\s+", " ", mm.group(1)).strip()
                code    = mm.group(2).strip()
                tracking = f"{carrier}: {code}"
                break
        if not tracking:
            mm = re.search(r"\b([A-Z]{2,6}[0-9]{6,}[A-Z0-9\-]*)\b", body)
            if mm:
                tracking = mm.group(1)

        # ── Total ────────────────────────────────────────────────
        total = ""
        _price_pat = r"฿\s*([0-9,]+(?:\.[0-9]{1,2})?)"
        # Pattern 1: "ทั้งหมด (x ชิ้น) ฿xxx"  (อาจข้ามบรรทัด)
        totals = re.findall(r"ทั้งหมด\s*\([^)]*\)\s*" + _price_pat, body, re.DOTALL)
        if totals:
            total = normalize_thb(totals[-1])
        else:
            # Pattern 2: keyword ตามด้วย ฿ (ข้ามบรรทัดได้ ไม่เกิน 80 ตัวอักษร)
            _kw = (
                r"(?:ทั้งหมด|ยอดรวม|ยอดคำสั่งซื้อ|ยอดที่ต้องชำระ|ยอดชำระ"
                r"|ยอดสุทธิ|รวมการสั่งซื้อ|ราคารวม|รวมทั้งหมด"
                r"|Total|Grand\s*Total|Amount|Order\s*Total)"
            )
            m = re.search(_kw + r".{0,80}?" + _price_pat, body, re.DOTALL | re.IGNORECASE)
            if m:
                total = normalize_thb(m.group(1))
        # กรอง total ที่เป็น 0 ออก (อาจจับค่าจัดส่งฟรี)
        if total and _to_amount(total) <= 0:
            total = ""

        # ── Products (ใช้ logic ต้นฉบับเต็ม) ───────────────────
        products = []
        stop_tokens = [
            "หมายเลขคำสั่งซื้อ", "หมายเลขคำขอ", "วันที่สั่งซื้อ", "วันที่ส่งคำขอ",
            "สรุปพัสดุ", "รายละเอียดการคืนเงิน", "มีปัญหาใช่หรือไม่", "ดูปัญหาทั้งหมด",
            "ไปที่ศูนย์ช่วยเหลือ", "ข้อความนี้ถูกส่งไปที่", "นโยบายความเป็นส่วนตัวของ TikTok",
        ]
        skip_exact = {"คำสั่งซื้อ", "รถเข็น", "ยกเลิกคำสั่งซื้อแล้ว", "รายการที่คืนเงินแล้ว", "ค่าเริ่มต้น"}

        def looks_like_price_or_qty(s):
            return ("฿" in s) or ("×" in s) or bool(re.fullmatch(r"[0-9,]+(?:\.[0-9]{1,2})?", s))

        def is_good_product_line(s):
            s = (s or "").strip()
            if not s or s in skip_exact: return False
            if any(tok in s for tok in stop_tokens): return False
            if looks_like_price_or_qty(s): return False
            if re.fullmatch(r"[0-9]{10,25}", s): return False
            bad_bits = [
                "คำสั่งซื้อของคุณ", "ทีม TikTok Shop", "TikTok Shop",
                "อย่าตอบกลับข้อความนี้", "ยกเลิกการสมัคร", "ศูนย์ช่วยเหลือ",
                "นโยบายความเป็นส่วนตัว", "รายละเอียดการคืนเงิน", "มีปัญหาใช่หรือไม่",
            ]
            if any(x in s for x in bad_bits): return False
            return 2 <= len(s) < 120

        def maybe_add_variant(idx_base):
            for k in range(idx_base + 1, min(idx_base + 4, len(lines))):
                ln2 = lines[k].strip()
                if not ln2: continue
                if any(x in ln2 for x in stop_tokens): break
                if looks_like_price_or_qty(ln2): break
                if is_good_product_line(ln2) and len(ln2) <= 40:
                    products.append(ln2)
                break

        # หา start: บรรทัด "คำสั่งซื้อ 1/1" หรือ "รายการที่คืนเงินแล้ว"
        start_idx = None
        for i, ln in enumerate(lines):
            if re.search(r"คำสั่งซื้อ\s*\d+\s*/\s*\d+", ln):
                start_idx = i
                break
            if ln in ("รายการที่คืนเงินแล้ว", "รายละเอียดการคืนเงิน"):
                start_idx = i
                break

        if start_idx is not None:
            for j in range(start_idx + 1, len(lines)):
                ln = lines[j].strip()
                if not ln: continue
                if any(x in ln for x in stop_tokens): break
                if not is_good_product_line(ln): continue
                products.append(ln)
                maybe_add_variant(j)

        # fallback: บรรทัดก่อนราคา/จำนวน
        if not products:
            for i, ln in enumerate(lines):
                if not looks_like_price_or_qty(ln): continue
                for back in range(1, 4):
                    if i - back < 0: break
                    cand = lines[i - back].strip()
                    if is_good_product_line(cand):
                        products.append(cand)
                        if back >= 2:
                            cand2 = lines[i - 1].strip()
                            if cand2 != cand and is_good_product_line(cand2) and len(cand2) <= 40:
                                products.append(cand2)
                        break
                if products: break

        # fallback: จาก subject "เพิ่งจัดส่ง ..."
        if not products:
            msub = re.search(r"เพิ่งจัดส่ง\s*(.+)$", subject)
            if msub:
                p0 = re.sub(r"\s+", " ", msub.group(1).strip())
                if p0: products.append(p0)

        products = list(dict.fromkeys([p for p in products if p and len(p) < 120]))[:10]

        # ── Ship area (จังหวัด, อำเภอ) ─────────────────────────────────
        # จับ "X, Y, ไทย" X+Y ต้องเป็น Thai ล้วน ยาวไม่เกิน 25 ตัว
        ship_area = ""
        _sa_skip = {"ทั้งหมด","สินค้า","ยกเลิก","คำสั่งซื้อ",
                    "การจัดส่ง","ยอดรวม","ไทย","ประเทศ","จังหวัด","อำเภอ","ตำบล"}
        _sa_m = re.search(
            u"([\u0e00-\u0e7f][^\n,]{0,20}),\\s*([\u0e00-\u0e7f][^\n,]{0,20}),\\s*\u0e44\u0e17\u0e22",
            body
        )
        if _sa_m:
            _sa_thai = re.compile(u"^[\u0e00-\u0e7f\\s]+$")
            _sa_cands = [_sa_m.group(1).strip(), _sa_m.group(2).strip()]
            _sa_tp = [x for x in _sa_cands
                      if _sa_thai.match(x) and 2 <= len(x) <= 25 and x not in _sa_skip]
            if len(_sa_tp) == 2:
                ship_area = f"{_sa_tp[0]}, {_sa_tp[1]}"
            elif len(_sa_tp) == 1:
                ship_area = _sa_tp[0]

        # ── แปลง order_date → timestamp จริง (ไม่ใช่วันที่เมลมา) ──
        order_ts = received_ts  # fallback
        if order_date and order_date != date_str:
            _od = order_date.strip()
            # แก้ truncation: "Mar 15, 2026 08:28 P" → "Mar 15, 2026 08:28 PM"
            if re.search(r'\s[AP]$', _od):
                _od += 'M'
            for _fmt in (
                "%b %d, %Y %I:%M %p",   # "Apr 2, 2026 08:18 AM"
                "%b %d, %Y %I:%M %p",
                "%Y-%m-%d %H:%M",        # "2026-04-02 08:18"
                "%Y-%m-%d",              # "2026-04-02"
            ):
                try:
                    order_ts = int(datetime.strptime(_od, _fmt)
                                   .replace(tzinfo=_BKK_TZ).timestamp())
                    break
                except Exception:
                    continue

        return Order(
            order_id=order_id,
            shop=shop,
            merchant=merchant,
            status=status,
            products=products,
            date=order_date,
            total=total,
            tracking=tracking,
            first_seen_ts=order_ts,
            last_update_ts=received_ts,
            account=account_email,
            ship_area=ship_area,
        )

    except Exception:
        return None


def slog(msg):
    """เพิ่ม log entry พร้อม timestamp"""
    sync_status["log"].append(f"[{_now_bkk().strftime('%H:%M:%S')}] {msg}")
    sync_status["log"] = sync_status["log"][-200:]


def _imap_connect(acc):
    """เปิด IMAP connection แล้ว return (conn, email_addr)"""
    server = acc.get("imap_server", "imap.zoho.com")
    port   = int(acc.get("imap_port", 993))
    email_addr = acc.get("email", "")
    password   = acc.get("password", "")
    conn = imaplib.IMAP4_SSL(server, port)
    conn.login(email_addr, password)
    conn.select("INBOX")
    return conn, email_addr


def _fetch_and_parse(conn, uid_list, email_addr):
    """ดาวน์โหลด + parse เมลจาก uid list, return (new, upd, max_uid)"""
    new_c = upd_c = 0
    max_uid = 0
    for uid_bytes in uid_list:
        try:
            uid_int = int(uid_bytes)
            if uid_int > max_uid:
                max_uid = uid_int
            # ใช้ UID FETCH เพื่อความแม่นยำ
            _, msg_data = conn.uid("fetch", uid_bytes, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            if not isinstance(raw, bytes):
                continue
            order = _parse_email(raw, email_addr)
            if order:
                existing = store.get(order.order_id)
                # ตรวจจับ: สถานะเปลี่ยนเป็น "จัดส่งเสร็จสิ้น" (เดิมยังไม่เสร็จ)
                incoming_delivered = _is_delivered_status(order.status)
                was_delivered = _is_delivered_status(existing.status) if existing else False
                store.upsert(order)
                if existing:
                    upd_c += 1
                else:
                    new_c += 1
                # เก็บออเดอร์ที่เพิ่งเสร็จสิ้นไว้แจ้งเตือน
                if incoming_delivered and not was_delivered:
                    _tg_delivered_orders_buffer.append(order)
        except Exception:
            pass
    return new_c, upd_c, max_uid


def _sync_account_incremental(acc):
    """
    UID-based incremental sync (เหมือนต้นฉบับ Desktop App)
    - จำ imap_last_uid ของแต่ละบัญชี
    - ดึงเฉพาะ UID > last_uid เท่านั้น
    - ถ้าไม่มีเมลใหม่ = จบเลย ไม่โหลดอะไร (เร็วมาก)
    - อัปเดต last_uid ใน settings.json หลัง sync
    """
    email_addr = acc.get("email", "")
    password   = acc.get("password", "")
    if not email_addr or not password:
        return 0, 0

    last_uid = int(acc.get("imap_last_uid", 0))

    try:
        conn, email_addr = _imap_connect(acc)

        # ค้นหาเฉพาะ UID ที่ใหม่กว่าที่เคยเห็น
        search_criterion = f"UID {last_uid + 1}:*" if last_uid > 0 else "ALL"
        _, data = conn.uid("search", None, search_criterion)
        uid_list = data[0].split() if data[0] else []

        # IMAP quirk: ถ้า last_uid+1 > max UID จะคืน UID สุดท้ายมา 1 ตัว
        # กรองออกถ้า UID นั้น <= last_uid
        uid_list = [u for u in uid_list if int(u) > last_uid]

        if not uid_list:
            conn.logout()
            return 0, 0  # ไม่มีอะไรใหม่ ออกเลย

        slog(f"📬 {email_addr}: พบ {len(uid_list)} เมลใหม่ (UID > {last_uid})")
        new_c, upd_c, max_uid = _fetch_and_parse(conn, uid_list, email_addr)
        conn.logout()

        # บันทึก last_uid ใหม่ลง settings.json
        if max_uid > last_uid:
            _save_last_uid(email_addr, max_uid)

        if new_c or upd_c:
            slog(f"✅ {email_addr}: ใหม่ {new_c} | อัปเดต {upd_c}")
        return new_c, upd_c

    except Exception as e:
        slog(f"❌ {email_addr}: {e}")
        return 0, 0


def _sync_account_full(acc, n_emails=500):
    """
    Full sync N เมลล่าสุด (ใช้ตอน manual rescan)
    แล้วอัปเดต last_uid ให้ตรงด้วย
    """
    email_addr = acc.get("email", "")
    password   = acc.get("password", "")
    if not email_addr or not password:
        return 0, 0

    try:
        slog(f"🔗 เชื่อมต่อ {acc.get('imap_server')} ({email_addr})")
        conn, email_addr = _imap_connect(acc)

        # ดึง UID ทั้งหมดแล้วเอาแค่ N ล่าสุด
        _, data = conn.uid("search", None, "ALL")
        all_uids = data[0].split() if data[0] else []
        target_uids = all_uids[-n_emails:]

        slog(f"📬 {email_addr}: ทั้งหมด {len(all_uids)} เมล, ตรวจ {len(target_uids)} ล่าสุด")
        new_c, upd_c, max_uid = _fetch_and_parse(conn, reversed(target_uids), email_addr)
        conn.logout()

        # อัปเดต last_uid ให้เป็น max UID ที่เห็นในรอบนี้
        if max_uid > 0:
            _save_last_uid(email_addr, max_uid)

        slog(f"✅ {email_addr}: ใหม่ {new_c} | อัปเดต {upd_c}")
        return new_c, upd_c

    except Exception as e:
        slog(f"❌ {email_addr}: {e}")
        return 0, 0


def _save_last_uid(email_addr, uid):
    """บันทึก imap_last_uid ลง settings.json (thread-safe)"""
    try:
        settings = load_settings()
        for acc in settings.get("accounts", []):
            if acc.get("email") == email_addr:
                acc["imap_last_uid"] = int(uid)
                break
        save_settings(settings)
    except Exception:
        pass


def _auto_delete_buyer_cancelled(max_seconds=3600):
    """ลบออเดอร์ที่ผู้ซื้อกดยกเลิกเองภายใน max_seconds (default 1 ชม.) หลังสั่ง"""
    try:
        with store._connect() as conn:
            rows = conn.execute("""
                SELECT order_id, first_seen_ts, last_update_ts FROM orders
                WHERE status LIKE '%ยกเลิก(ผู้ซื้อ)%'
                  AND last_update_ts IS NOT NULL
                  AND first_seen_ts IS NOT NULL
                  AND (last_update_ts - first_seen_ts) <= ?
            """, (max_seconds,)).fetchall()
            if rows:
                ids = [r["order_id"] for r in rows]
                conn.executemany("DELETE FROM orders WHERE order_id=?",
                                 [(oid,) for oid in ids])
                slog(f"🗑️ ลบอัตโนมัติ {len(ids)} ออเดอร์ (ผู้ซื้อยกเลิกเอง): {', '.join(ids[:3])}")
                return len(ids)
    except Exception as e:
        slog(f"⚠️ auto-delete error: {e}")
    return 0


def run_sync_background(accounts, n_emails=300):
    """Manual full sync (กดปุ่ม ซิงค์ทันที)"""
    if sync_status["running"]:
        return
    def task():
        sync_status["running"] = True
        sync_status["log"] = []
        total_new = total_upd = 0
        error_count = 0
        for acc in accounts:
            try:
                n, u = _sync_account_full(acc, n_emails)
                total_new += n
                total_upd += u
            except Exception:
                error_count += 1
        sync_status["running"] = False
        sync_status["last_sync"] = _now_bkk().strftime("%Y-%m-%d %H:%M:%S")
        sync_status["log"].append(f"🏁 รวม: ใหม่ {total_new} | อัปเดต {total_upd}")
        # ── Auto-delete ผู้ซื้อยกเลิกเอง ──
        _auto_delete_buyer_cancelled()
        # ── Telegram hook ──
        try:
            _tg_handle_post_sync(
                total_scanned=total_new + total_upd,
                total_new=total_new,
                total_updated=total_upd,
                error_count=error_count,
            )
        except Exception:
            pass
    threading.Thread(target=task, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
#  Auth
# ══════════════════════════════════════════════════════════════════════════════

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("username"):
            if request.is_json:
                return jsonify({"error": "Unauthorized"}), 401
            return render_template_string(LOGIN_HTML), 401
        return f(*args, **kwargs)
    return decorated


# ══════════════════════════════════════════════════════════════════════════════
#  HTML Templates
# ══════════════════════════════════════════════════════════════════════════════

LOGIN_HTML = """<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Order Tracker – เข้าสู่ระบบ</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 300 300'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0%25' stop-color='%236c5ce7'/%3E%3Cstop offset='50%25' stop-color='%23e94560'/%3E%3Cstop offset='100%25' stop-color='%23fbbf24'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='300' height='300' rx='64' fill='url(%23g)'/%3E%3Cg transform='translate(150,145)'%3E%3Cpath d='M0,-75 L75,-40 L75,40 L0,75 L-75,40 L-75,-40 Z' fill='rgba(0,0,0,0.25)' stroke='white' stroke-width='10' stroke-linejoin='round'/%3E%3Cline x1='-75' y1='-40' x2='0' y2='-5' stroke='white' stroke-width='10'/%3E%3Cline x1='75' y1='-40' x2='0' y2='-5' stroke='white' stroke-width='10'/%3E%3Cline x1='0' y1='-5' x2='0' y2='75' stroke='white' stroke-width='10'/%3E%3C/g%3E%3C/svg%3E">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',sans-serif;background:linear-gradient(135deg,#1a1a2e 0%,#16213e 50%,#0f3460 100%);
min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:rgba(255,255,255,.07);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,.15);
border-radius:24px;padding:48px 40px;width:100%;max-width:400px;text-align:center;box-shadow:0 32px 64px rgba(0,0,0,.4)}
.logo{margin-bottom:8px;display:flex;justify-content:center}
h1{color:#fff;font-size:24px;margin-bottom:4px}
p{color:#94a3b8;font-size:14px;margin-bottom:24px}
input{width:100%;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);border-radius:12px;
padding:14px 18px;color:#fff;font-size:16px;margin-bottom:12px;outline:none;transition:.2s}
input:focus{border-color:#e63946;background:rgba(255,255,255,.15)}
input::placeholder{color:#64748b}
.btn-primary{width:100%;background:#e63946;color:#fff;border:none;border-radius:12px;padding:14px;
font-size:16px;font-weight:700;cursor:pointer;transition:.2s}
.btn-primary:hover{background:#d62828;transform:translateY(-1px)}
.btn-primary:disabled{opacity:.5;cursor:not-allowed;transform:none}
.err{color:#f87171;font-size:13px;margin-top:8px}
.ok{color:#4ade80;font-size:13px;margin-top:8px}
.toggle{color:#94a3b8;font-size:13px;margin-top:20px}
.toggle a{color:#60a5fa;cursor:pointer;text-decoration:underline}
.license-info{color:#64748b;font-size:11px;margin-top:16px}
.hidden{display:none}
</style>
</head>
<body>
<div class="card">
  <div class="logo"><svg width="120" viewBox="0 0 300 300" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="lbg" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#6c5ce7"/><stop offset="50%" stop-color="#e94560"/><stop offset="100%" stop-color="#fbbf24"/></linearGradient></defs><rect width="300" height="300" rx="64" fill="url(#lbg)"/><g transform="translate(150,145)"><path d="M0,-75 L75,-40 L75,40 L0,75 L-75,40 L-75,-40 Z" fill="rgba(0,0,0,0.25)" stroke="#fff" stroke-width="4.5" stroke-linejoin="round"/><line x1="-75" y1="-40" x2="0" y2="-5" stroke="#fff" stroke-width="4.5"/><line x1="75" y1="-40" x2="0" y2="-5" stroke="#fff" stroke-width="4.5"/><line x1="0" y1="-5" x2="0" y2="75" stroke="#fff" stroke-width="4.5"/><line x1="-38" y1="-57" x2="38" y2="-22" stroke="#fbbf24" stroke-width="4" stroke-linecap="round"/><line x1="38" y1="-57" x2="38" y2="-22" stroke="#fbbf24" stroke-width="4" stroke-linecap="round"/></g></svg></div>
  <h1>Order Tracker</h1>

  <!-- Login Form -->
  <div id="loginPanel">
    <p>กรุณาเข้าสู่ระบบเพื่อดำเนินการต่อ</p>
    <form method="POST" action="/login">
      <input type="text" name="username" placeholder="ชื่อผู้ใช้" autofocus required>
      <input type="password" name="password" placeholder="รหัสผ่าน" required>
      <button type="submit" class="btn-primary">🔐 เข้าสู่ระบบ</button>
      {% if error %}<div class="err">❌ {{ error }}</div>{% endif %}
    </form>
    <div class="toggle">ยังไม่มีบัญชี? <a onclick="showRegister()">สมัครสมาชิก</a></div>
  </div>

  <!-- Register Form -->
  <div id="registerPanel" class="hidden">
    <p>สมัครสมาชิกเพื่อเริ่มใช้งาน</p>
    <input type="text" id="regUser" placeholder="ชื่อผู้ใช้ (อย่างน้อย 4 ตัว)" minlength="4">
    <input type="password" id="regPass" placeholder="รหัสผ่าน (อย่างน้อย 4 ตัว)" minlength="4">
    <input type="password" id="regPass2" placeholder="ยืนยันรหัสผ่าน">
    <button class="btn-primary" id="btnRegister" onclick="doRegister()">📝 สมัครสมาชิก</button>
    <div id="regMsg"></div>
    <div class="toggle">มีบัญชีแล้ว? <a onclick="showLogin()">เข้าสู่ระบบ</a></div>
  </div>

  <div class="license-info">v""" + CURRENT_VERSION + """</div>
</div>
<script>
function showRegister() {
  document.getElementById('loginPanel').classList.add('hidden');
  document.getElementById('registerPanel').classList.remove('hidden');
  document.getElementById('regUser').focus();
}
function showLogin() {
  document.getElementById('registerPanel').classList.add('hidden');
  document.getElementById('loginPanel').classList.remove('hidden');
}
async function doRegister() {
  const user = document.getElementById('regUser').value.trim();
  const pass1 = document.getElementById('regPass').value;
  const pass2 = document.getElementById('regPass2').value;
  const msg = document.getElementById('regMsg');
  const btn = document.getElementById('btnRegister');
  msg.className = '';  msg.textContent = '';

  if(!user || user.length < 4) { msg.className='err'; msg.textContent='❌ ชื่อผู้ใช้ต้องมีอย่างน้อย 4 ตัวอักษร'; return; }
  if(!pass1 || pass1.length < 4) { msg.className='err'; msg.textContent='❌ รหัสผ่านต้องมีอย่างน้อย 4 ตัวอักษร'; return; }
  if(pass1 !== pass2) { msg.className='err'; msg.textContent='❌ รหัสผ่านไม่ตรงกัน'; return; }

  btn.disabled = true; btn.textContent = '⏳ กำลังสมัคร...';
  try {
    const res = await fetch('/register', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({username: user, password: pass1}),
      credentials: 'same-origin'
    });
    const data = await res.json();
    if(data.success) {
      msg.className='ok'; msg.textContent='✅ สมัครสำเร็จ! กำลังไปหน้าล็อกอิน...';
      setTimeout(() => showLogin(), 1500);
    } else {
      msg.className='err'; msg.textContent='❌ ' + (data.error||'สมัครไม่สำเร็จ');
    }
  } catch(e) {
    msg.className='err'; msg.textContent='❌ เชื่อมต่อไม่ได้: '+e.message;
  } finally {
    btn.disabled = false; btn.textContent = '📝 สมัครสมาชิก';
  }
}
</script>
</body></html>"""


MAIN_HTML = r"""<!DOCTYPE html>
<html lang="th">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>Order Tracker</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 300 300'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0%25' stop-color='%236c5ce7'/%3E%3Cstop offset='50%25' stop-color='%23e94560'/%3E%3Cstop offset='100%25' stop-color='%23fbbf24'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='300' height='300' rx='64' fill='url(%23g)'/%3E%3Cg transform='translate(150,145)'%3E%3Cpath d='M0,-75 L75,-40 L75,40 L0,75 L-75,40 L-75,-40 Z' fill='rgba(0,0,0,0.25)' stroke='white' stroke-width='10' stroke-linejoin='round'/%3E%3Cline x1='-75' y1='-40' x2='0' y2='-5' stroke='white' stroke-width='10'/%3E%3Cline x1='75' y1='-40' x2='0' y2='-5' stroke='white' stroke-width='10'/%3E%3Cline x1='0' y1='-5' x2='0' y2='75' stroke='white' stroke-width='10'/%3E%3C/g%3E%3C/svg%3E">
<style>
/* ── Reset & Base ── */
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#080c14;
  --surface:#0f1623;
  --surface2:#162032;
  --surface3:#1d2a44;
  --surface4:#253050;
  --border:#2a3d5c;
  --border2:#3a4f70;
  --text:#e8f0ff;
  --muted:#5a7294;
  --accent:#ff4f6d;
  --accent2:#ff7849;
  --accent3:#00e5a0;
  --accent4:#4da6ff;
  --accent5:#b47fff;
  --accent6:#ffd166;
  --green:#10b981;
  --blue:#4da6ff;
  --purple:#b47fff;
  --orange:#ff9f43;
  --red:#ff4f6d;
  --cyan:#06d6e7;
  --yellow:#ffd166;
  --pink:#ff6b9d;
  --lime:#26de81;
  --radius:12px;
  --shadow:0 8px 32px rgba(0,0,0,.55);
  --shadow-sm:0 2px 12px rgba(0,0,0,.4);
  --glow:0 0 20px rgba(255,79,109,.35);
  --glow-green:0 0 16px rgba(38,222,129,.3);
  --glow-blue:0 0 16px rgba(77,166,255,.3);
  --glow-purple:0 0 16px rgba(180,127,255,.3);
  --glow-orange:0 0 16px rgba(255,159,67,.3);
  --glass:rgba(15,22,35,.85);
}
body{font-family:'Segoe UI',Tahoma,sans-serif;background:var(--bg);color:var(--text);
  min-height:100vh;overflow-x:hidden;font-size:14px}

/* ── Layout ── */
.app{display:flex;height:100vh;overflow:hidden}
.sidebar{width:252px;background:var(--surface);
  border-right:1px solid var(--border);
  display:flex;flex-direction:column;flex-shrink:0;overflow:hidden;
  box-shadow:4px 0 24px rgba(0,0,0,.3)}
.main{flex:1;display:flex;flex-direction:column;overflow:hidden}

/* ── Sidebar Header ── */
.sidebar-header{
  padding:18px 14px 16px;
  border-bottom:1px solid var(--border);
  background:linear-gradient(145deg,#1d2a44 0%,#0f1623 100%);
  display:flex;align-items:center;gap:11px;
  position:relative;
}
.sidebar-header::after{
  content:'';
  position:absolute;
  bottom:0;left:14px;right:14px;
  height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,79,109,.4),transparent);
}
.sidebar-header .logo-icon{
  width:40px;height:40px;
  border-radius:11px;
  display:flex;align-items:center;justify-content:center;
  flex-shrink:0;
  overflow:hidden;
}
.sidebar-header h2{font-size:15px;font-weight:800;letter-spacing:.3px;line-height:1.2;
  background:linear-gradient(135deg,#fff,#c8d6f0);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sidebar-header small{color:var(--muted);font-size:10px;letter-spacing:.5px}

/* ── Nav Section ── */
.nav-section{padding:8px}
.nav-label{font-size:11px;font-weight:800;color:var(--muted);text-transform:uppercase;
  letter-spacing:1.8px;padding:8px 8px 4px}
.nav-btn{display:flex;align-items:center;gap:9px;padding:9px 10px;border-radius:9px;
  cursor:pointer;transition:.18s;font-size:13px;border:none;background:transparent;
  color:var(--muted);width:100%;text-align:left;letter-spacing:.2px;position:relative}
.nav-btn::before{
  content:'';
  position:absolute;
  left:0;top:50%;transform:translateY(-50%);
  width:0;height:0;
  border-radius:0 6px 6px 0;
  transition:height .18s;
}
.nav-btn:hover{background:rgba(255,255,255,.04);color:var(--text)}
.nav-btn:hover::before{height:60%;background:rgba(77,166,255,.3)}
.nav-btn.active{
  background:linear-gradient(90deg,rgba(255,79,109,.15) 0%,transparent 100%);
  color:var(--accent);font-weight:700;
}
.nav-btn.active::before{height:60%;background:var(--accent);box-shadow:0 0 10px rgba(255,79,109,.5)}

/* ── Stats Cards ── */
.stats-section{padding:8px 10px}
.stats-section .nav-label{padding:4px 4px 8px}
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px}
.stat-card{
  background:linear-gradient(145deg,var(--surface2) 0%,var(--surface3) 100%);
  border-radius:12px;
  padding:11px 8px;
  text-align:center;
  border:1px solid var(--border);
  cursor:pointer;
  transition:all .22s;
  position:relative;
  overflow:hidden;
}
.stat-card::before{
  content:'';
  position:absolute;
  top:0;left:0;right:0;
  height:3px;
  border-radius:12px 12px 0 0;
}
.stat-card.blue::before{background:linear-gradient(90deg,#4da6ff,#74c0fc)}
.stat-card.amber::before{background:linear-gradient(90deg,#ff9f43,#ffd166)}
.stat-card.green::before{background:linear-gradient(90deg,#26de81,#10b981)}
.stat-card.red::before{background:linear-gradient(90deg,#ff4f6d,#ff9f43)}
.stat-card.orange::before{background:linear-gradient(90deg,#ffd166,#ff9f43)}
.stat-card.purple::before{background:linear-gradient(90deg,#b47fff,#c4b5fd)}
.stat-card:hover{
  transform:translateY(-2px) scale(1.02);
  border-color:var(--border2);
  box-shadow:0 8px 24px rgba(0,0,0,.4);
}
.stat-card.active{
  border-color:var(--accent);
  box-shadow:0 0 0 2px rgba(255,79,109,.3),0 6px 20px rgba(255,79,109,.2);
  transform:translateY(-1px);
}
.stat-num{
  font-size:24px;font-weight:900;line-height:1.1;
  letter-spacing:-1px;
}
.stat-card.blue .stat-num{background:linear-gradient(135deg,#74c0fc,#4da6ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-card.amber .stat-num{background:linear-gradient(135deg,#ffd166,#ff9f43);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-card.green .stat-num{background:linear-gradient(135deg,#26de81,#10b981);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-card.red .stat-num{background:linear-gradient(135deg,#ff9f43,#ff4f6d);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-card.orange .stat-num{background:linear-gradient(135deg,#ffd166,#ff9f43);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-card.purple .stat-num{background:linear-gradient(135deg,#c4b5fd,#b47fff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.stat-lbl{font-size:10px;color:var(--muted);margin-top:3px;font-weight:600;letter-spacing:.4px}

/* ── Bottom Sidebar ── */
/* ── Sidebar Panels ── */
.sb-panel{padding:8px 10px;border-bottom:1px solid var(--border)}
.sb-feed{max-height:180px;overflow-y:auto;display:flex;flex-direction:column;gap:6px}
.sb-feed-empty{font-size:12px;color:var(--muted);text-align:center;padding:12px 0}
.sb-feed-item{display:flex;align-items:flex-start;gap:7px;padding:4px 0}
.sb-feed-item .sb-fdot{width:6px;height:6px;border-radius:50%;flex-shrink:0;margin-top:5px}
.sb-feed-item .sb-fname{font-size:12px;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sb-feed-item .sb-ftime{font-size:11px;color:var(--muted)}
.sb-row{display:flex;align-items:center;gap:6px;padding:4px 0;font-size:12px}
.sb-row.clickable{cursor:pointer;border-radius:6px;padding:4px 6px;transition:background .15s}
.sb-row.clickable:hover{background:var(--hover)}
.sb-row.sb-active{background:rgba(77,166,255,0.15);font-weight:700;border-radius:6px;padding:4px 6px;box-shadow:inset 3px 0 0 #4da6ff}
.sb-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.sb-label{flex:1;color:var(--muted)}
.sb-val{font-weight:700;color:var(--text);min-width:28px;text-align:right}
.sb-row-total{border-top:1px solid var(--border);margin-top:4px;padding-top:6px}
.sb-row-total .sb-label{font-weight:700;color:var(--text);font-size:12px}
.sb-chart{display:flex;flex-direction:column;gap:8px}
.td-btn{background:var(--hover);color:var(--muted);border:1px solid var(--border);border-radius:6px;padding:1px 6px;font-size:10px;cursor:pointer;transition:.15s}
.td-btn:hover{background:var(--border)}
.td-btn.active{background:rgba(77,166,255,0.2);color:#4da6ff;border-color:#4da6ff;font-weight:700}
.grp-row td{background:linear-gradient(90deg,rgba(180,127,255,.09),transparent);border-bottom:1px solid rgba(180,127,255,.18) !important;padding:7px 10px}
.grp-label{display:flex;align-items:center;gap:8px;font-size:10px;font-weight:800;color:#c4b5fd;text-transform:uppercase;letter-spacing:.9px}
.grp-count{background:rgba(180,127,255,.18);color:#c4b5fd;border-radius:10px;padding:1px 7px;font-size:10px;font-weight:800}
.grp-total{color:var(--lime);font-size:10px;font-weight:700;margin-left:auto;margin-right:4px}
.chip-area{display:inline-flex;align-items:center;gap:4px;background:rgba(180,127,255,.1);color:#c4b5fd;border:1px solid rgba(180,127,255,.18);border-radius:5px;padding:2px 7px;font-size:10px;font-weight:700;white-space:nowrap}
.chip-area.na{background:rgba(90,114,148,.07);color:var(--muted);border-color:transparent}
.sb-chart-row{display:flex;flex-direction:column;gap:5px}
.sb-carrier-item{display:flex;flex-direction:column;gap:2px}
.sb-carrier-bar-wrap{height:4px;background:rgba(255,255,255,.05);border-radius:2px;overflow:hidden}
.sb-carrier-bar{height:100%;border-radius:2px;transition:.3s}
.sb-carrier-info{display:flex;justify-content:space-between;font-size:11px}
.sb-carrier-info span:first-child{color:var(--muted)}
.sb-carrier-info span:last-child{color:var(--green);font-weight:700}
.sb-top-products{display:flex;flex-direction:column;gap:3px;margin-top:4px}
.sb-prod-row{display:flex;align-items:center;gap:4px;font-size:11px}
.sb-prod-rank{width:14px;color:var(--muted);font-weight:700;text-align:right}
.sb-prod-name{flex:1;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sb-prod-cnt{color:var(--green);font-weight:700;min-width:20px;text-align:right}

.sidebar-footer{
  padding:10px;
  margin-top:auto;
  border-top:1px solid var(--border);
  background:linear-gradient(0deg,rgba(8,12,20,.8) 0%,transparent 100%);
}
.sidebar-footer .sync-info{font-size:10px;color:var(--muted);text-align:center;margin-bottom:5px;line-height:1.7}
.sidebar-footer .auto-badge{
  display:inline-flex;align-items:center;gap:5px;
  background:rgba(38,222,129,.1);color:#26de81;
  border:1px solid rgba(38,222,129,.2);
  border-radius:20px;padding:3px 9px;font-size:9px;font-weight:700;
}
.sidebar-footer .auto-badge.paused{background:rgba(90,114,148,.1);color:var(--muted);border-color:var(--border)}
.btn-sm{padding:7px 10px;border-radius:8px;border:none;cursor:pointer;font-size:12px;
  font-weight:700;transition:.18s;display:inline-flex;align-items:center;gap:5px;width:100%;justify-content:center;
  letter-spacing:.3px}
.btn-sm-primary{
  background:linear-gradient(135deg,#ff4f6d 0%,#ff9f43 100%);
  color:#fff;
  box-shadow:0 4px 14px rgba(255,79,109,.4);
}
.btn-sm-primary:hover{filter:brightness(1.1);transform:translateY(-1px);box-shadow:0 6px 18px rgba(255,79,109,.5)}
.btn-sm-secondary{background:var(--surface2);color:var(--muted);border:1px solid var(--border)}
.btn-sm-secondary:hover{background:var(--surface3);color:var(--text)}

/* ── Top Bar ── */
.topbar{
  background:linear-gradient(180deg,var(--surface) 0%,var(--surface2) 100%);
  border-bottom:1px solid var(--border);
  padding:10px 16px;
  display:flex;align-items:center;gap:10px;
  position:relative;
}
.topbar::after{
  content:'';
  position:absolute;
  bottom:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(77,166,255,.2),transparent);
}
.topbar-title{
  font-size:15px;font-weight:800;flex:1;letter-spacing:.4px;
  background:linear-gradient(90deg,#fff 0%,#9db8e0 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.topbar-logo{border-radius:6px;flex-shrink:0;margin-right:8px}
.hamburger{display:none;background:none;border:none;color:var(--muted);font-size:20px;cursor:pointer;padding:4px}

/* ── Filter Bar ── */
.filter-bar{
  padding:8px 14px;
  display:flex;gap:6px;flex-wrap:wrap;align-items:center;
  background:var(--surface);
  border-bottom:1px solid var(--border);
}
.date-bar .btn{padding:0 10px;font-size:11px;font-weight:600}

/* ── Main Stats Cards ── */
.main-stats{
  display:grid;grid-template-columns:repeat(6,1fr);gap:8px;
  padding:10px 14px;background:var(--surface);border-bottom:1px solid var(--border);
}
.ms-card{padding:10px 8px;border-radius:10px;text-align:center;transition:.15s;cursor:default}
.ms-card.clickable{cursor:pointer}
.ms-card.clickable:hover{transform:translateY(-1px);filter:brightness(1.1)}
.ms-num{font-size:20px;font-weight:800;line-height:1.2}
.ms-lbl{font-size:10px;font-weight:600;margin-top:2px;opacity:.8;letter-spacing:.3px}
.ms-blue{background:rgba(56,138,221,.12);color:#378ADD}.ms-blue .ms-lbl{color:#185FA5}
.ms-green{background:rgba(38,222,129,.12);color:#26de81}.ms-green .ms-lbl{color:#10b981}
.ms-amber{background:rgba(255,209,102,.15);color:#ffd166}.ms-amber .ms-lbl{color:#ff9f43}
.ms-red{background:rgba(255,79,109,.12);color:#ff4f6d}.ms-red .ms-lbl{color:#e74c3c}
.ms-orange{background:rgba(255,159,67,.12);color:#ff9f43}.ms-orange .ms-lbl{color:#e67e22}
.ms-purple{background:rgba(196,181,253,.12);color:#c4b5fd}.ms-purple .ms-lbl{color:#b47fff}

/* ── Top Bar Revenue ── */
.topbar-revenue{
  font-size:13px;font-weight:700;color:#26de81;
  margin-left:auto;margin-right:8px;white-space:nowrap;
}

/* ── Mobile Card View ── */
.mobile-cards{display:none}
.order-card{
  background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:12px;margin:6px 10px;cursor:pointer;transition:.15s;
}
.order-card:hover{border-color:var(--accent);background:var(--surface2)}
.order-card .oc-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px}
.order-card .oc-name{font-size:13px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-right:8px}
.order-card .oc-meta{display:flex;flex-wrap:wrap;gap:8px;font-size:11px;color:var(--muted)}
.order-card .oc-meta .oc-price{color:var(--green);font-weight:700}
input[type=text],input[type=date],select{
  background:var(--surface2);
  border:1px solid var(--border);
  border-radius:8px;
  color:var(--text);
  padding:7px 10px;
  font-size:12px;
  outline:none;
  transition:.2s;
  height:33px;
}
input[type=text]{flex:1;min-width:100px}
input[type=text]:focus,select:focus{
  border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(255,79,109,.1);
}
select{font-size:12px;height:33px}
select option{background:var(--surface2)}
.btn{
  height:33px;padding:0 13px;
  border-radius:8px;border:1px solid var(--border);
  cursor:pointer;font-size:12px;font-weight:700;
  transition:.18s;display:inline-flex;align-items:center;gap:5px;
  background:var(--surface2);color:var(--muted);white-space:nowrap;
  letter-spacing:.2px;
}
.btn:hover{background:var(--surface3);color:var(--text);border-color:var(--border2)}
.btn-active,.btn.pressed{
  background:linear-gradient(135deg,rgba(255,79,109,.18) 0%,rgba(255,121,73,.12) 100%);
  border-color:rgba(255,79,109,.4);
  color:var(--accent);
  box-shadow:0 0 0 2px rgba(255,79,109,.12);
}
.btn-all{color:var(--muted)}
.btn-all.btn-active{color:var(--accent)}

/* ── Table ── */
.table-wrap{flex:1;overflow:auto;background:var(--bg)}
table{width:100%;border-collapse:collapse;font-size:12px}
thead th{
  background:linear-gradient(180deg,var(--surface2) 0%,var(--surface) 100%);
  position:sticky;top:0;z-index:1;
  padding:8px 10px;text-align:left;
  color:var(--muted);font-weight:700;
  border-bottom:2px solid var(--border);
  white-space:nowrap;
  font-size:10px;text-transform:uppercase;letter-spacing:1px;
}
tbody tr{border-bottom:1px solid rgba(42,61,92,.4);transition:.1s}
tbody tr:hover{background:rgba(255,255,255,.025)}
tbody tr.selected{background:rgba(255,79,109,.06);border-left:3px solid var(--accent)}
td{padding:8px 10px;vertical-align:middle}
.truncate{max-width:155px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ── Badges ── */
.badge{
  display:inline-block;
  padding:3px 9px;
  border-radius:20px;
  font-size:10px;font-weight:800;
  letter-spacing:.4px;
  border:1px solid;
}
.badge-green{background:rgba(38,222,129,.1);color:#26de81;border-color:rgba(38,222,129,.25)}
.badge-red{background:rgba(255,79,109,.1);color:#ff9f43;border-color:rgba(255,79,109,.25)}
.badge-amber{background:rgba(255,159,67,.1);color:#ffd166;border-color:rgba(255,159,67,.25)}
.badge-blue{background:rgba(77,166,255,.1);color:#74c0fc;border-color:rgba(77,166,255,.25)}
.badge-gray{background:rgba(90,114,148,.1);color:#5a7294;border-color:rgba(90,114,148,.2)}
.badge-purple{background:rgba(180,127,255,.1);color:#c4b5fd;border-color:rgba(180,127,255,.25)}

/* ── Bottom Bar ── */
.bottom-bar{
  padding:10px 16px;
  background:linear-gradient(0deg,var(--surface) 0%,var(--surface2) 100%);
  border-top:1px solid var(--border);
  display:flex;align-items:center;justify-content:space-between;
  font-size:13px;color:var(--muted);
  position:relative;transition:.25s;
}
.bottom-bar::before{
  content:'';
  position:absolute;
  top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(77,166,255,.15),transparent);
}
.bottom-bar .total-amount{color:#26de81;font-weight:800}
.bottom-bar.filtered{
  padding:13px 18px;
  background:linear-gradient(0deg,rgba(180,127,255,.1) 0%,var(--surface) 100%);
  border-top:1px solid rgba(180,127,255,.35);
}
.bottom-bar.filtered::before{
  background:linear-gradient(90deg,transparent,rgba(180,127,255,.45),transparent);
}
.bottom-bar.filtered #rowCount{
  font-size:14px;font-weight:800;color:var(--text);
}
.bottom-bar.filtered .total-amount{
  font-size:17px;font-weight:900;
  text-shadow:0 0 18px rgba(38,222,129,.45);
}
/* ── Area filter ── */
#areaFilter{color:var(--purple)}
#areaFilter option{color:var(--text)}
/* ── Carrier filter ── */
#carrierFilter{color:var(--cyan,#0abde3)}
#carrierFilter option{color:var(--text)}
/* ── Summary panel ── */
.summary-panel{padding:16px 20px;overflow-y:auto;flex:1;display:none}
.sum-header{display:flex;align-items:center;gap:10px;margin-bottom:16px}
.sum-header h3{font-size:12px;font-weight:800;color:var(--muted);text-transform:uppercase;letter-spacing:1.2px}
.sum-header h3::after{content:'';flex:1;}
.sum-grand{margin-left:auto;font-size:15px;font-weight:900;color:var(--lime)}
.sum-section{margin-bottom:20px}
.sum-section-title{
  font-size:10px;font-weight:800;color:var(--muted);text-transform:uppercase;
  letter-spacing:1.2px;margin-bottom:8px;display:flex;align-items:center;gap:6px;
}
.sum-section-title::after{content:'';flex:1;height:1px;background:var(--border)}
.sum-prov-name{
  font-size:12px;font-weight:800;color:#c4b5fd;
  display:flex;align-items:center;gap:8px;margin-bottom:5px;
}
.sum-prov-name .pc{font-size:10px;font-weight:700;background:rgba(180,127,255,.18);
  color:#c4b5fd;border-radius:10px;padding:1px 7px}
.sum-prov-name .pt{margin-left:auto;color:var(--lime);font-weight:900;font-size:13px}
.sum-row{
  display:flex;align-items:center;gap:10px;
  padding:7px 12px;background:var(--surface2);border-radius:8px;
  margin-bottom:4px;border:1px solid var(--border);
}
.sum-row:hover{border-color:var(--border2)}
.sum-row-name{font-size:12px;font-weight:700;flex:1}
.sum-row-cnt{font-size:11px;color:var(--muted);min-width:60px;text-align:right}
.sum-bar-wrap{width:100px}
.sum-bar{height:5px;border-radius:3px;background:linear-gradient(90deg,#b47fff,#c4b5fd)}
.sum-bar.carrier{background:linear-gradient(90deg,#4da6ff,#74c0fc)}
.sum-row-amt{font-size:13px;font-weight:900;color:var(--lime);min-width:90px;text-align:right}

/* ── Modal ── */
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:100;
  display:none;align-items:center;justify-content:center;padding:16px;backdrop-filter:blur(6px)}
.overlay.open{display:flex}
.modal{
  background:var(--surface);
  border-radius:16px;
  max-width:540px;width:100%;
  max-height:88vh;overflow-y:auto;
  box-shadow:0 24px 80px rgba(0,0,0,.7);
  border:1px solid var(--border);
}
.modal-header{
  padding:16px 20px;border-bottom:1px solid var(--border);
  display:flex;justify-content:space-between;align-items:center;
  background:linear-gradient(135deg,var(--surface2) 0%,var(--surface) 100%);
  border-radius:16px 16px 0 0;
  position:relative;
}
.modal-header::after{
  content:'';position:absolute;bottom:0;left:20px;right:20px;height:1px;
  background:linear-gradient(90deg,transparent,rgba(255,79,109,.2),transparent);
}
.modal-header h3{font-size:14px;font-weight:800;letter-spacing:.3px}
.modal-body{padding:16px 20px}
.modal-footer{padding:12px 20px;border-top:1px solid var(--border);
  display:flex;gap:8px;justify-content:flex-end}
.close-btn{background:none;border:none;color:var(--muted);font-size:18px;cursor:pointer;
  padding:2px 6px;border-radius:6px;transition:.15s}
.close-btn:hover{background:var(--surface2);color:var(--text)}
.field-row{margin-bottom:12px}
.field-row label{display:block;font-size:11px;color:var(--muted);margin-bottom:5px;font-weight:700;
  text-transform:uppercase;letter-spacing:.6px}
.field-row input,.field-row select,.field-row textarea{
  width:100%;background:var(--surface2);border:1px solid var(--border);
  border-radius:8px;color:var(--text);padding:8px 12px;font-size:13px;outline:none;height:36px;
}
.field-row textarea{resize:vertical;min-height:56px;height:auto;line-height:1.5}
.field-row input:focus,.field-row select:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(255,79,109,.1)}
.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}
.info-item{background:var(--surface2);border-radius:9px;padding:9px 12px;
  border:1px solid var(--border);position:relative;overflow:hidden}
.info-item::before{
  content:'';position:absolute;top:0;left:0;width:3px;height:100%;
  background:linear-gradient(180deg,var(--accent),transparent);
}
.info-item .lbl{font-size:10px;color:var(--muted);margin-bottom:2px;
  text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.info-item .val{font-size:13px;font-weight:800;word-break:break-all}

/* ── Modal Buttons ── */
.btn-modal{padding:8px 16px;border-radius:8px;border:none;cursor:pointer;font-size:13px;font-weight:700;transition:.18s}
.btn-modal-primary{
  background:linear-gradient(135deg,#ff4f6d 0%,#ff9f43 100%);
  color:#fff;box-shadow:0 4px 14px rgba(255,79,109,.4);
}
.btn-modal-primary:hover{filter:brightness(1.1);transform:translateY(-1px)}
.btn-modal-secondary{background:var(--surface2);color:var(--muted);border:1px solid var(--border)}
.btn-modal-secondary:hover{background:var(--surface3);color:var(--text)}
.btn-modal-danger{background:rgba(255,79,109,.1);color:var(--accent);border:1px solid rgba(255,79,109,.2)}
.btn-modal-danger:hover{background:rgba(255,79,109,.2)}

/* ── Sync Page ── */
.sync-page{padding:20px;overflow-y:auto;height:100%}
.sync-card{
  background:linear-gradient(145deg,var(--surface2) 0%,var(--surface) 100%);
  border-radius:14px;border:1px solid var(--border);
  padding:16px;margin-bottom:14px;
  position:relative;overflow:hidden;
}
.sync-card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,#ff4f6d,#ff9f43,#ffd166);
}
.sync-card-title{font-size:13px;font-weight:800;margin-bottom:10px;display:flex;align-items:center;gap:8px}
.sync-card-title .dot{width:8px;height:8px;border-radius:50%;
  background:#26de81;box-shadow:0 0 8px rgba(38,222,129,.6)}
.sync-log{background:#040810;border-radius:8px;padding:10px;
  font-family:'Consolas','Monaco',monospace;font-size:11px;
  max-height:200px;overflow-y:auto;color:#6ee7a0;border:1px solid var(--border)}

/* ── Settings ── */
.settings-page{padding:20px;overflow-y:auto;height:100%}
.settings-section{margin-bottom:24px}
.settings-section h3{font-size:11px;font-weight:800;color:var(--muted);text-transform:uppercase;
  letter-spacing:1.2px;margin-bottom:10px;padding-bottom:6px;
  border-bottom:1px solid var(--border)}
.settings-card{background:linear-gradient(145deg,var(--surface2),var(--surface));
  border-radius:12px;padding:14px;margin-bottom:8px;border:1px solid var(--border)}

/* ── Toast ── */
.toast-wrap{position:fixed;top:16px;right:16px;z-index:999;display:flex;flex-direction:column;gap:6px}
.toast{padding:10px 16px;border-radius:10px;font-size:13px;font-weight:700;
  box-shadow:0 8px 28px rgba(0,0,0,.5);max-width:300px;
  animation:slideIn .25s ease}
.toast-success{background:linear-gradient(135deg,#10b981,#26de81);color:#fff}
.toast-error{background:linear-gradient(135deg,#ff4f6d,#ff9f43);color:#fff}
.toast-info{background:linear-gradient(135deg,#4da6ff,#74c0fc);color:#fff}
@keyframes slideIn{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:translateX(0)}}

/* ── Loading ── */
.loader{display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:200;
  align-items:center;justify-content:center;backdrop-filter:blur(6px)}
.loader.show{display:flex}
.spinner{width:42px;height:42px;
  border:3px solid var(--border);
  border-top-color:var(--accent);
  border-right-color:var(--accent2);
  border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── Empty State ── */
.empty{text-align:center;padding:50px 20px;color:var(--muted)}
.empty .emoji{font-size:42px;display:block;margin-bottom:10px}

/* ── Pages ── */
.page{display:none;flex-direction:column;height:100%;overflow:hidden}
.page.active{display:flex}

/* ── Sidebar Overlay ── */
#sideOverlay{display:none;position:fixed;inset:0;z-index:49;background:rgba(0,0,0,.55)}

/* ── Responsive ── */
@media(max-width:768px){
  .sidebar{position:fixed;left:-252px;top:0;bottom:0;z-index:50;transition:.3s;
    box-shadow:4px 0 32px rgba(0,0,0,.6)}
  .sidebar.open{left:0}
  .hamburger{display:block}
  .stats-grid{grid-template-columns:1fr 1fr 1fr}
  .main-stats{grid-template-columns:repeat(3,1fr);gap:6px;padding:8px 10px}
  .ms-num{font-size:17px}
  .info-grid{grid-template-columns:1fr}
  .filter-bar{gap:5px;padding:6px 10px}
  input[type=text]{width:100%}
  .topbar-title{font-size:14px}
  .topbar-revenue{font-size:11px}
  .overlay{padding:8px}
  .table-wrap{display:none}
  .mobile-cards{display:block;flex:1;overflow:auto;background:var(--bg)}
  table{font-size:11px}
  td,th{padding:7px}
  .truncate{max-width:90px}
}
@media(max-width:480px){
  .stats-grid{grid-template-columns:1fr 1fr}
  .main-stats{grid-template-columns:repeat(3,1fr)}
  .ms-num{font-size:15px}
  .ms-lbl{font-size:9px}
  .filter-bar .btn{flex:1;justify-content:center;padding:0 6px;font-size:11px}
  .date-bar{gap:4px}
}

</style>
</head>
<body>
<div class="loader" id="loader"><div class="spinner"></div></div>
<div class="toast-wrap" id="toastWrap"></div>

<div class="app">
<!-- ── Sidebar ── -->
<aside class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <div class="logo-icon"><svg width="40" height="40" viewBox="0 0 300 300" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="sbg" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#6c5ce7"/><stop offset="50%" stop-color="#e94560"/><stop offset="100%" stop-color="#fbbf24"/></linearGradient></defs><rect width="300" height="300" rx="64" fill="url(#sbg)"/><g transform="translate(150,145)"><path d="M0,-75 L75,-40 L75,40 L0,75 L-75,40 L-75,-40 Z" fill="rgba(0,0,0,0.25)" stroke="#fff" stroke-width="8" stroke-linejoin="round"/><line x1="-75" y1="-40" x2="0" y2="-5" stroke="#fff" stroke-width="8"/><line x1="75" y1="-40" x2="0" y2="-5" stroke="#fff" stroke-width="8"/><line x1="0" y1="-5" x2="0" y2="75" stroke="#fff" stroke-width="8"/><line x1="-38" y1="-57" x2="38" y2="-22" stroke="#fbbf24" stroke-width="7" stroke-linecap="round"/><line x1="38" y1="-57" x2="38" y2="-22" stroke="#fbbf24" stroke-width="7" stroke-linecap="round"/></g></svg></div>
    <div>
      <h2>Order Tracker</h2>
      <small>Web Edition v2</small>
    </div>
  </div>

  <div class="nav-section">
    <div class="nav-label">เมนูหลัก</div>
    <button class="nav-btn active" onclick="showPage('orders')" id="nav-orders">
      📋 รายการออเดอร์
    </button>
    <button class="nav-btn" onclick="showPage('sync')" id="nav-sync">
      🔄 ซิงค์อีเมล
    </button>
    <button class="nav-btn" onclick="showPage('delivery')" id="nav-delivery">
      🚚 ตรวจรอบส่ง
    </button>
    <button class="nav-btn" onclick="showPage('settings')" id="nav-settings">
      ⚙️ ตั้งค่า
    </button>
  </div>

  <!-- Sidebar Panel 1: กิจกรรมล่าสุด -->
  <div class="sb-panel">
    <div class="nav-label">กิจกรรมล่าสุด</div>
    <div class="sb-feed" id="sbFeed">
      <div class="sb-feed-empty">ยังไม่มีกิจกรรม</div>
    </div>
  </div>

  <!-- Sidebar Panel 2: สรุปสถานะ -->
  <div class="sb-panel">
    <div class="nav-label">สรุปสถานะ <span id="sbStatusDate" style="font-weight:400;color:var(--green);font-size:9px;margin-left:4px"></span></div>
    <div class="sb-status-list">
      <div class="sb-row clickable" onclick="filterByStat('กำลังเตรียม')"><span class="sb-dot" style="background:#c4b5fd"></span><span class="sb-label">กำลังเตรียม</span><span class="sb-val" id="sb-prep">0</span></div>
      <div class="sb-row clickable" onclick="filterByStat('กำลังจัดส่ง')"><span class="sb-dot" style="background:#ffd166"></span><span class="sb-label">กำลังจัดส่ง</span><span class="sb-val" id="sb-transit">0</span></div>
      <div class="sb-row clickable" onclick="filterByStat('ต้องเช็ค')"><span class="sb-dot" style="background:#ff9f43"></span><span class="sb-label">ต้องเช็ค</span><span class="sb-val" id="sb-check">0</span></div>
      <div class="sb-row clickable" onclick="filterByStat('ยกเลิก')"><span class="sb-dot" style="background:#ff4f6d"></span><span class="sb-label">ยกเลิก</span><span class="sb-val" id="sb-cancel">0</span></div>
      <div class="sb-row clickable" onclick="filterByStat('ค้างนาน')"><span class="sb-dot" style="background:#ff9f43"></span><span class="sb-label">ค้างนาน</span><span class="sb-val" id="sb-overdue">0</span></div>
      <div class="sb-row clickable" onclick="filterByStat('จัดส่งเสร็จสิ้นแล้ว')"><span class="sb-dot" style="background:#26de81"></span><span class="sb-label">ปิดงาน</span><span class="sb-val" id="sb-done">0</span></div>
      <div class="sb-row" style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px"><span class="sb-dot" style="background:transparent"></span><span class="sb-label" style="font-weight:700;color:var(--text)">📦 คาดว่าจะถึง</span><span class="sb-val"></span></div>
      <div class="sb-row clickable" onclick="filterByEta('today')"><span class="sb-dot" style="background:#74c0fc"></span><span class="sb-label">วันนี้</span><span class="sb-val" id="sb-eta-today" style="color:#74c0fc">0</span></div>
      <div class="sb-row clickable" onclick="filterByEta('tomorrow')"><span class="sb-dot" style="background:#4da6ff"></span><span class="sb-label">พรุ่งนี้</span><span class="sb-val" id="sb-eta-tomorrow" style="color:#4da6ff">0</span></div>
      <div class="sb-row clickable" onclick="filterByEta('dayafter')"><span class="sb-dot" style="background:#b47fff"></span><span class="sb-label">มะรืน</span><span class="sb-val" id="sb-eta-dayafter" style="color:#b47fff">0</span></div>
      <div class="sb-row clickable" onclick="filterByEta('late')"><span class="sb-dot" style="background:#ff9f43"></span><span class="sb-label">ช้ากว่ากำหนด</span><span class="sb-val" id="sb-eta-late" style="color:#ff9f43">0</span></div>
      <div class="sb-row clickable" onclick="filterByEta('overdue')"><span class="sb-dot" style="background:#ff4f6d"></span><span class="sb-label">เลยกำหนด</span><span class="sb-val" id="sb-eta-overdue" style="color:#ff4f6d">0</span></div>
      <div id="sb-reset-row" style="display:none;text-align:center;margin:6px 0">
        <button onclick="resetFilter()" style="background:var(--hover);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:4px 14px;font-size:11px;cursor:pointer">✕ กลับภาพรวม</button>
      </div>
      <div class="sb-row sb-row-total"><span class="sb-dot" style="background:transparent"></span><span class="sb-label" id="sb-revenue-label">💵 ยอดชำระรวม</span><span class="sb-val" id="sb-revenue" style="color:#26de81">฿0</span></div>
    </div>
  </div>

  <!-- Sidebar Panel 3: แนวโน้มออเดอร์เข้า -->
  <div class="sb-panel">
    <div class="nav-label" style="display:flex;justify-content:space-between;align-items:center">
      <span>📈 ออเดอร์เข้า</span>
      <div id="trendDayBtns" style="display:flex;gap:2px">
        <button class="td-btn" onclick="setTrendDays(7)" data-d="7">7d</button>
        <button class="td-btn" onclick="setTrendDays(14)" data-d="14">14d</button>
        <button class="td-btn" onclick="setTrendDays(30)" data-d="30">30d</button>
        <button class="td-btn" onclick="setTrendDays(90)" data-d="90">90d</button>
      </div>
    </div>
    <div id="trendSummary" style="font-size:11px;color:var(--muted);margin-bottom:4px"></div>
    <div class="sb-chart" id="sbChart"></div>
  </div>

  <!-- Sidebar footer -->

  <div class="sidebar-footer">
    <div class="sync-info" id="lastSync">ยังไม่เคยซิงค์</div>
    <div style="margin-bottom:8px;text-align:center">
      <span class="auto-badge" id="autoIndicator">⚡ Auto ON</span>
    </div>
    <button class="btn-sm btn-sm-primary" style="margin-bottom:5px" onclick="quickSync()">
      🔄 ซิงค์ตอนนี้
    </button>
    <button class="btn-sm btn-sm-secondary" onclick="logout()">🚪 ออกจากระบบ</button>
    <button class="btn-sm btn-sm-secondary" style="margin-top:5px;color:#e74c3c" onclick="shutdownServer()">⏻ ปิดเซิร์ฟเวอร์</button>
  </div>
</aside>


<!-- ── Main ── -->
<main class="main">
  <!-- Top Bar -->
  <div class="topbar">
    <button class="hamburger" onclick="toggleSidebar()">☰</button>
    <svg class="topbar-logo" width="28" height="28" viewBox="0 0 300 300" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="tbg" x1="0" y1="0" x2="1" y2="1"><stop offset="0%" stop-color="#6c5ce7"/><stop offset="50%" stop-color="#e94560"/><stop offset="100%" stop-color="#fbbf24"/></linearGradient></defs><rect width="300" height="300" rx="64" fill="url(#tbg)"/><g transform="translate(150,145)"><path d="M0,-75 L75,-40 L75,40 L0,75 L-75,40 L-75,-40 Z" fill="rgba(0,0,0,0.25)" stroke="#fff" stroke-width="8" stroke-linejoin="round"/><line x1="-75" y1="-40" x2="0" y2="-5" stroke="#fff" stroke-width="8"/><line x1="75" y1="-40" x2="0" y2="-5" stroke="#fff" stroke-width="8"/><line x1="0" y1="-5" x2="0" y2="75" stroke="#fff" stroke-width="8"/><line x1="-38" y1="-57" x2="38" y2="-22" stroke="#fbbf24" stroke-width="7" stroke-linecap="round"/><line x1="38" y1="-57" x2="38" y2="-22" stroke="#fbbf24" stroke-width="7" stroke-linecap="round"/></g></svg>
    <div class="topbar-title" id="pageTitle">📋 รายการออเดอร์</div>
    <div class="topbar-revenue" id="topbarRevenue"></div>
    <button class="btn btn-secondary" onclick="loadOrders()" title="รีเฟรช">↺</button>
  </div>

  <!-- ══ Orders Page ══ -->
  <div class="page active" id="page-orders">
    <!-- Stats Cards (main area) -->
    <div class="main-stats" id="mainStats">
      <div class="ms-card ms-blue clickable" id="card-total" onclick="filterByStat('ทั้งหมด')">
        <div class="ms-num" id="ms-total">-</div><div class="ms-lbl">ทั้งหมด</div>
      </div>
      <div class="ms-card ms-green clickable" id="card-done" onclick="filterByStat('จัดส่งเสร็จสิ้นแล้ว')">
        <div class="ms-num" id="ms-done">-</div><div class="ms-lbl">สำเร็จ</div>
      </div>
      <div class="ms-card ms-amber clickable" id="card-transit" onclick="filterByStat('กำลังจัดส่ง')">
        <div class="ms-num" id="ms-transit">-</div><div class="ms-lbl">กำลังจัดส่ง</div>
      </div>
      <div class="ms-card ms-red clickable" id="card-cancel" onclick="filterByStat('ยกเลิก')">
        <div class="ms-num" id="ms-cancel">-</div><div class="ms-lbl">ยกเลิก</div>
      </div>
      <div class="ms-card ms-orange clickable" id="card-overdue" onclick="filterByStat('ค้างนาน')">
        <div class="ms-num" id="ms-overdue">-</div><div class="ms-lbl">ค้างนาน</div>
      </div>
      <div class="ms-card ms-purple" id="card-prep">
        <div class="ms-num" id="ms-revenue">-</div><div class="ms-lbl">ยอดชำระ</div>
      </div>
    </div>

    <!-- Filter Row 1: Search + Dropdowns -->
    <div class="filter-bar">
      <input type="text" id="searchInput" placeholder="🔍 ค้นหา..." style="flex:1;min-width:140px"
             oninput="debounceLoad()">
      <select id="accountFilter" onchange="loadOrders()" title="กรองตามบัญชีอีเมล">
        <option value="ทุกบัญชี">📧 ทุกบัญชี</option>
      </select>
      <select id="statusFilter" onchange="onStatusFilterChange()">
        <option value="ทั้งหมด">สถานะทั้งหมด</option>
        <option value="รอดำเนินการ">รอดำเนินการ</option>
        <option value="กำลังเตรียม">กำลังเตรียม</option>
        <option value="รอจัดส่ง">รอจัดส่ง</option>
        <option value="กำลังจัดส่ง">กำลังจัดส่ง</option>
        <option value="จัดส่งแล้ว">จัดส่งแล้ว</option>
        <option value="จัดส่งเสร็จสิ้นแล้ว">ส่งสำเร็จ</option>
        <option value="ยกเลิก">ยกเลิก</option>
        <option value="ค้างนาน">⚠️ ค้างนาน</option>
      </select>
      <select id="areaFilter" onchange="applyAreaFilter()" title="กรองตามปลายทาง">
        <option value="">📍 ทุกปลายทาง</option>
      </select>
      <select id="carrierFilter" onchange="applyCarrierFilter()" title="กรองตามขนส่ง">
        <option value="">🚚 ทุกขนส่ง</option>
      </select>
    </div>
    <!-- Filter Row 2: Date buttons -->
    <div class="filter-bar date-bar">
      <button class="btn btn-secondary" id="btn-today" onclick="filterToday()">วันนี้</button>
      <button class="btn btn-secondary" id="btn-yesterday" onclick="filterYesterday()">เมื่อวาน</button>
      <button class="btn btn-secondary" id="btn-3days" onclick="filterDays(3)">3 วัน</button>
      <button class="btn btn-secondary" id="btn-5days" onclick="filterDays(5)">5 วัน</button>
      <button class="btn btn-secondary" id="btn-7days" onclick="filterDays(7)">7 วัน</button>
      <button class="btn btn-secondary" id="btn-15days" onclick="filterDays(15)">15 วัน</button>
      <button class="btn btn-secondary" id="btn-30days" onclick="filterDays(30)">30 วัน</button>
      <input type="date" id="dateFrom" onchange="loadOrders()" title="จากวันที่">
      <input type="date" id="dateTo"   onchange="loadOrders()" title="ถึงวันที่">
      <button class="btn btn-secondary" id="btn-all" onclick="clearFilters()">✕ ล้าง</button>
      <button class="btn btn-secondary" id="btn-alldata" onclick="filterAll()">ทั้งหมด</button>
      <button class="btn btn-secondary" id="btnSummary" onclick="toggleSummary()">💰 สรุปยอด</button>
    </div>

    <div class="table-wrap">
      <table id="ordersTable">
        <thead>
          <tr>
            <th>หมายเลขออเดอร์</th>
            <th>แพลตฟอร์ม</th>
            <th>ร้านค้า</th>
            <th>สถานะ</th>
            <th>สินค้า</th>
            <th>ยอดรวม</th>
            <th>ขนส่ง</th>
            <th>วันที่</th>
            <th style="color:#c4b5fd">📍 ปลายทาง</th>
          </tr>
        </thead>
        <tbody id="ordersBody"></tbody>
      </table>
      <div class="empty" id="emptyState" style="display:none">
        <span class="emoji">📭</span>
        ไม่พบรายการออเดอร์<br>
        <small>ลองซิงค์อีเมลหรือเปลี่ยนตัวกรอง</small>
      </div>
    </div>
    <!-- Mobile Card View -->
    <div class="mobile-cards" id="mobileCards"></div>
    <div class="summary-panel" id="summaryPanel">
      <div id="summaryContent"></div>
    </div>
  <div class="bottom-bar" id="bottomBar">
    <span id="rowCount">0 รายการ</span>
    <span class="total-amount" id="totalAmount"></span>
  </div>
</div>

<!-- ══ Sync Page ══ -->
  <div class="page" id="page-sync" style="overflow-y:auto;padding:24px">
    <h2 style="margin-bottom:20px">🔄 ซิงค์อีเมล</h2>

    <!-- Auto Sync Panel -->
    <div class="account-card" style="margin-bottom:20px;border-color:rgba(16,185,129,.3)">
      <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px">
        <div>
          <div style="font-size:15px;font-weight:700;margin-bottom:4px">⚡ Auto-Sync</div>
          <div style="font-size:12px;color:var(--muted)" id="autoSyncDesc">ซิงค์อัตโนมัติทุก 60 วินาที</div>
        </div>
        <div style="display:flex;align-items:center;gap:10px">
          <select id="autoInterval" onchange="setAutoInterval()" style="width:120px">
            <option value="30">ทุก 30 วิ</option>
            <option value="60" selected>ทุก 1 นาที</option>
            <option value="120">ทุก 2 นาที</option>
            <option value="300">ทุก 5 นาที</option>
            <option value="600">ทุก 10 นาที</option>
          </select>
          <button id="autoToggleBtn" class="btn btn-green" onclick="toggleAutoSync()" style="min-width:80px">
            ⏸ หยุด
          </button>
        </div>
      </div>
      <div style="margin-top:12px;display:flex;gap:16px;font-size:13px;flex-wrap:wrap">
        <span>📨 ใหม่ทั้งหมด: <b id="autoNewCount" style="color:var(--green)">0</b></span>
        <span>🔄 อัปเดต: <b id="autoUpdCount" style="color:var(--blue)">0</b></span>
        <span>🕐 ซิงค์ล่าสุด: <b id="autoLastSync" style="color:var(--muted)">-</b></span>
      </div>
    </div>

    <!-- Manual Sync -->
    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;align-items:flex-end">
      <div class="field-row" style="margin:0">
        <label>ซิงค์ manual จำนวนเมล</label>
        <select id="syncCount">
          <option value="100">100 เมล</option>
          <option value="300" selected>300 เมล</option>
          <option value="500">500 เมล</option>
          <option value="1000">1,000 เมล</option>
          <option value="2000">2,000 เมล</option>
          <option value="5000">5,000 เมล</option>
        </select>
      </div>
      <button class="btn btn-primary" onclick="startSync()" id="syncBtn">▶ ซิงค์ทันที</button>
      <button class="btn btn-secondary" onclick="refreshSyncLog()">↺ รีเฟรช</button>
    </div>
    <div id="syncStatusBadge" style="margin-bottom:12px"></div>
    <div class="sync-log" id="syncLog">// รอ auto-sync หรือกด "ซิงค์ทันที"...</div>
  </div>

  <!-- ══ Settings Page ══ -->
  <div class="page" id="page-settings" style="overflow-y:auto;padding:24px">
    <h2 style="margin-bottom:20px">⚙️ ตั้งค่า</h2>

    <!-- Web Password -->
    <div class="settings-section">
      <h3>🔑 รหัสผ่านเว็บ</h3>
      <div class="account-card">
        <div class="field-row">
          <label>รหัสผ่านใหม่</label>
          <input type="password" id="newPassword" placeholder="ใส่รหัสผ่านใหม่ (ว่าง=ไม่เปลี่ยน)">
        </div>
        <button class="btn btn-primary" onclick="savePassword()">💾 บันทึกรหัสผ่าน</button>
      </div>
    </div>

    <!-- IMAP Accounts -->
    <div class="settings-section">
      <h3>📧 บัญชีอีเมล IMAP</h3>
      <div style="background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.2);
                  border-radius:8px;padding:10px 14px;margin-bottom:12px;font-size:13px;color:#60a5fa">
        ℹ️ รหัสผ่านเก็บอยู่ใน <b>settings.json</b> บนเซิร์ฟเวอร์ — ใส่ใหม่เฉพาะเมื่อต้องการเปลี่ยน
      </div>
      <div id="accountsList"></div>
      <button class="btn btn-secondary" onclick="addAccount()" style="margin-top:8px">
        ➕ เพิ่มบัญชีอีเมล
      </button>
    </div>

    <!-- Telegram Settings -->
    <div class="settings-section">
      <h3>📣 Telegram แจ้งเตือน</h3>
      <div class="account-card">
        <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
          <label style="font-size:14px;font-weight:600">เปิดใช้งาน Telegram</label>
          <input type="checkbox" id="tg-enabled" style="width:18px;height:18px;cursor:pointer">
        </div>
        <div class="field-row">
          <label>Bot Token</label>
          <input type="text" id="tg-token" placeholder="1234567890:AAFxxxxx...">
        </div>
        <div class="field-row">
          <label>Chat ID</label>
          <div style="display:flex;gap:8px">
            <input type="text" id="tg-chat" placeholder="-100xxxxxxxxx หรือ xxxxxxxxx" style="flex:1">
            <button class="btn btn-secondary" onclick="fetchChatId()" id="btn-fetch-chat" style="white-space:nowrap;font-size:12px">🆔 ดึง Chat ID</button>
          </div>
        </div>
        <div id="tg-chat-result" style="font-size:12px;margin:-4px 0 10px 0"></div>

        <!-- แจ้งเตือนทันที -->
        <div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:12px">
          <div style="font-weight:600;font-size:13px;margin-bottom:8px">⚡ แจ้งเตือนทันที</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-instant"> เปิดใช้งาน
            </label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-alert-imap"> เตือน IMAP มีปัญหา
            </label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-alert-syncfail"> เตือนซิงก์ล้มเหลว
            </label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-alert-login"> เตือน Login ไม่ผ่าน
            </label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-alert-anomaly"> เตือนปิดพัสดุผิดปกติ
            </label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-alert-delivered"> แจ้งจัดส่งเสร็จสิ้นทีละรายการ
            </label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-recovery"> แจ้งเมื่อกลับมาปกติ
            </label>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;margin-top:8px">
            <div class="field-row" style="margin:0"><label style="font-size:11px">ซิงก์ล้มเหลวก่อนเตือน</label>
              <input type="number" id="tg-fail-threshold" value="3" min="1" style="font-size:12px"></div>
            <div class="field-row" style="margin:0"><label style="font-size:11px">ขั้นต่ำปิดพัสดุ/ค่าย</label>
              <input type="number" id="tg-low" value="20" min="0" style="font-size:12px"></div>
            <div class="field-row" style="margin:0"><label style="font-size:11px">สูงสุดปิดพัสดุ/ค่าย</label>
              <input type="number" id="tg-high" value="300" min="0" style="font-size:12px"></div>
          </div>
        </div>

        <!-- สรุปรายรอบ -->
        <div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:12px">
          <div style="font-weight:600;font-size:13px;margin-bottom:8px">📊 ส่งภาพรวมอัตโนมัติ</div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-digest"> เปิดส่งภาพรวมอัตโนมัติ
            </label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-digest-products"> แสดงรายชื่อสินค้า
            </label>
            <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
              <input type="checkbox" id="tg-digest-movement"> ส่งเฉพาะเมื่อมีเปลี่ยนแปลง
            </label>
          </div>
          <div class="field-row" style="margin:0">
            <label style="font-size:12px">ความถี่สรุป</label>
            <select id="tg-digest-mode" style="background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:8px;padding:8px;font-size:12px;width:100%">
              <option>ไม่ส่ง</option><option>ทุก 1 นาที</option><option>ทุก 5 นาที</option>
              <option>ทุก 15 นาที</option><option>ทุก 30 นาที</option><option selected>ทุก 1 ชั่วโมง</option>
              <option>ทุก 6 ชั่วโมง</option><option>วันละ 2 ครั้ง (เช้า/เย็น)</option><option>วันละครั้ง</option>
            </select>
          </div>
        </div>

        <!-- กันสแปม -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
          <div class="field-row" style="margin:0"><label style="font-size:12px">นาทีก่อนแจ้งซ้ำ</label>
            <input type="number" id="tg-dedup" value="30" min="1" style="font-size:12px"></div>
        </div>

        <!-- ช่วงเงียบ -->
        <div style="background:var(--surface);border-radius:8px;padding:12px;margin-bottom:12px">
          <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer;margin-bottom:8px">
            <input type="checkbox" id="tg-quiet"> เปิดช่วงเงียบ (ไม่ส่งแจ้งเตือน)
          </label>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
            <div class="field-row" style="margin:0">
              <label>เริ่มเงียบ</label>
              <input type="time" id="tg-quiet-start" value="23:00">
            </div>
            <div class="field-row" style="margin:0">
              <label>สิ้นสุดเงียบ</label>
              <input type="time" id="tg-quiet-end" value="08:00">
            </div>
          </div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-green" onclick="saveTelegram()">💾 บันทึก Telegram</button>
          <button class="btn btn-secondary" onclick="testTelegram()">📨 ทดสอบส่ง</button>
          <button class="btn btn-secondary" onclick="testTelegramServer()">🖥️ ทดสอบจากเซิร์ฟเวอร์</button>
        </div>
        <div id="tg-test-result" style="font-size:12px;margin-top:8px"></div>
      </div>
    </div>

    <!-- ETA Settings -->
    <div class="settings-section">
      <h3>📦 ระบบคาดการณ์พัสดุถึง</h3>
      <div class="account-card">
        <div class="field-row">
          <label>จำนวนวันคาดการณ์เริ่มต้น (ใช้เมื่อยังไม่มีข้อมูลร้าน)</label>
          <input type="number" id="eta-days" value="2" min="1" max="7" style="width:80px">
        </div>
        <p style="color:var(--muted);font-size:12px;margin-top:4px">
          ค่าเริ่มต้น = 2 วัน — ระบบจะเรียนรู้อัตโนมัติจากประวัติจัดส่งจริง
        </p>
        <button class="btn btn-green" onclick="saveSettings()" style="margin-top:8px">💾 บันทึก</button>
      </div>
      <div class="account-card" style="margin-top:8px">
        <div style="font-weight:600;font-size:13px;margin-bottom:6px">🧠 ETA เรียนรู้จากประวัติ</div>
        <div id="eta-learned-list" style="font-size:12px;color:var(--muted)">กำลังโหลด...</div>
      </div>
    </div>

    <!-- Data Management -->
    <div class="settings-section">
      <h3>🗑️ จัดการข้อมูล</h3>
      <div class="account-card">
        <p style="color:var(--muted);font-size:13px;margin-bottom:12px">
          ลบออเดอร์ทั้งหมดออกจากระบบ (ไม่สามารถกู้คืนได้)
        </p>
        <button class="btn btn-primary" onclick="confirmClearAll()">🗑️ ล้างข้อมูลทั้งหมด</button>
      </div>
    </div>
  </div>

  <!-- ══ Delivery Trips Page ══ -->
  <div class="page" id="page-delivery" style="overflow-y:auto;padding:20px">
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px" id="dlvDayBtns"></div>
    <div id="dlvSummary" style="background:var(--surface);border-radius:12px;padding:14px;margin-bottom:14px;border:1px solid var(--border)"></div>
    <div id="dlvTrips"></div>
    <div id="dlvEmpty" style="text-align:center;padding:40px;color:var(--muted);display:none">ไม่มีข้อมูลรอบส่ง</div>
  </div>

</main>
</div>

<!-- ══ Order Detail Modal ══ -->
<div class="overlay" id="detailOverlay" onclick="closeModal()">
<div class="modal" onclick="event.stopPropagation()">
  <div class="modal-header">
    <div>
      <div style="font-size:13px;color:var(--muted)">หมายเลขออเดอร์</div>
      <div id="modalOrderId" style="font-size:16px;font-weight:700"></div>
    </div>
    <button class="close-btn" onclick="closeModal()">✕</button>
  </div>
  <div class="modal-body">
    <div class="info-grid">
      <div class="info-item"><div class="lbl">แพลตฟอร์ม</div><div class="val" id="m-shop"></div></div>
      <div class="info-item"><div class="lbl">ร้านค้า</div><div class="val" id="m-merchant"></div></div>
      <div class="info-item"><div class="lbl">วันที่สั่งซื้อ</div><div class="val" id="m-date"></div></div>
      <div class="info-item"><div class="lbl">บัญชีอีเมล</div><div class="val" id="m-account"></div></div>
      <div class="info-item" style="grid-column:1/-1;border-color:rgba(180,127,255,.3);background:rgba(180,127,255,.06)">
        <div class="lbl" style="color:#c4b5fd">📍 ปลายทาง</div>
        <div class="val" id="m-area" style="color:#c4b5fd">—</div>
      </div>
    </div>
    <div class="field-row">
      <label>📌 สถานะ</label>
      <select id="m-status">
        <option>รอดำเนินการ</option>
        <option>กำลังเตรียม</option>
        <option>รอจัดส่ง</option>
        <option>กำลังจัดส่ง</option>
        <option>จัดส่งแล้ว</option>
        <option>จัดส่งเสร็จสิ้นแล้ว</option>
        <option>ยกเลิก</option>
      </select>
    </div>
    <div class="field-row">
      <label>🚚 เลขพัสดุ / ขนส่ง</label>
      <div style="display:flex;gap:6px;align-items:center">
        <input type="text" id="m-tracking" placeholder="เลขพัสดุหรือชื่อขนส่ง" style="flex:1">
        <button id="btn-track" onclick="openTrackingLink()" style="background:var(--green);color:#fff;border:none;border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer;white-space:nowrap;display:none">🔗 เช็คพัสดุ</button>
      </div>
    </div>
    <div class="field-row">
      <label>💰 ยอดรวม</label>
      <input type="text" id="m-total" placeholder="฿0.00">
    </div>
    <div class="field-row">
      <label>📦 สินค้า</label>
      <textarea id="m-products" rows="2" readonly style="opacity:.8"></textarea>
    </div>
  </div>
  <div class="modal-footer">
    <button class="btn btn-secondary" onclick="deleteOrder()" style="color:#f87171">
      🗑️ ลบ
    </button>
    <button class="btn btn-secondary" onclick="closeModal()">ยกเลิก</button>
    <button class="btn btn-primary" onclick="saveOrder()">💾 บันทึก</button>
  </div>
</div>
</div>

<!-- ══ Sidebar overlay for mobile ══ -->
<div id="sideOverlay" onclick="toggleSidebar()"
  style="display:none;position:fixed;inset:0;z-index:49;background:rgba(0,0,0,.5)"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let currentOrderId = null;
let orders = [];
let currentEtaFilter = '';
let trendDays = 7;
let syncTimer = null;
let debounceTimer = null;

// ── Date helper (ใช้เวลา local ไม่ใช่ UTC) ─────────────────────────────
function _localDateStr(d) {
  // คืนค่า YYYY-MM-DD ตามเวลาท้องถิ่น (ไม่ใช้ toISOString ที่เป็น UTC)
  const dt = d || new Date();
  const y = dt.getFullYear();
  const m = String(dt.getMonth() + 1).padStart(2, '0');
  const dd = String(dt.getDate()).padStart(2, '0');
  return `${y}-${m}-${dd}`;
}

// ── Init ───────────────────────────────────────────────────────────────────
window.onload = () => {
  // Default: ไม่กรองวัน → แสดงทั้งหมด
  loadAccountFilter();   // โหลด dropdown บัญชีอีเมล
  loadOrders();
  loadStats();
  loadSettings();
  setInterval(loadStats, 30000);
  setInterval(_updateSidebarPanels, 60000);
  setInterval(loadAccountFilter, 120000);
  // Auto-refresh sync log ทุก 10 วินาที
  setInterval(() => {
    if(document.getElementById('page-sync').classList.contains('active'))
      refreshSyncLog();
  }, 10000);

  // ── Smart auto-refresh: ตรวจจับ auto-sync ใหม่ → รีเฟรช UI ทันที ──
  let _lastKnownSync = null;
  let _lastKnownAutoNew = 0;
  let _lastKnownAutoUpd = 0;
  setInterval(async () => {
    try {
      const st = await api('/api/sync/status');
      if (!st) return;
      const changed = (
        st.last_sync && st.last_sync !== _lastKnownSync &&
        (st.auto_new !== _lastKnownAutoNew || st.auto_upd !== _lastKnownAutoUpd)
      );
      _lastKnownSync = st.last_sync;
      if (changed) {
        const newItems = st.auto_new - _lastKnownAutoNew;
        const updItems = st.auto_upd - _lastKnownAutoUpd;
        _lastKnownAutoNew = st.auto_new;
        _lastKnownAutoUpd = st.auto_upd;
        // รีเฟรช UI ทันที
        loadOrders();
        loadStats();
        if (newItems > 0 || updItems > 0) {
          toast(`🔄 Auto-sync: ใหม่ ${newItems} | อัปเดต ${updItems}`, 'info');
        }
      } else {
        _lastKnownAutoNew = st.auto_new || 0;
        _lastKnownAutoUpd = st.auto_upd || 0;
      }
    } catch(e) {}
  }, 5000);  // ตรวจทุก 5 วินาที
};

// ── Navigation ─────────────────────────────────────────────────────────────
function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  document.getElementById('nav-'+name).classList.add('active');
  const titles = {orders:'📋 รายการออเดอร์',sync:'🔄 ซิงค์อีเมล',settings:'⚙️ ตั้งค่า',delivery:'🚚 ตรวจรอบส่ง'};
  document.getElementById('pageTitle').textContent = titles[name] || '';
  if(name==='sync') refreshSyncLog();
  if(name==='settings') loadSettings();
  if(name==='delivery') loadDeliveryTrips();
  if(window.innerWidth<=768) toggleSidebar(false);
}

function toggleSidebar(force) {
  const sb = document.getElementById('sidebar');
  const ov = document.getElementById('sideOverlay');
  const open = force !== undefined ? force : !sb.classList.contains('open');
  sb.classList.toggle('open', open);
  ov.style.display = open ? 'block' : 'none';
}

// ── Delivery Trips ────────────────────────────────────────────────────────
let _dlvData = null, _dlvSelDate = null;
async function loadDeliveryTrips() {
  try {
    const r = await fetch('/api/delivery-trips'); _dlvData = await r.json();
    if(_dlvData.length && !_dlvSelDate) _dlvSelDate = _dlvData[_dlvData.length-1].date;
    renderDlvDays();
  } catch(e) { console.error(e); }
}
function renderDlvDays() {
  const c = document.getElementById('dlvDayBtns'); c.innerHTML='';
  if(!_dlvData||!_dlvData.length){document.getElementById('dlvEmpty').style.display='block';return;}
  document.getElementById('dlvEmpty').style.display='none';
  _dlvData.forEach(d => {
    const b = document.createElement('button');
    b.className='btn '+(d.date===_dlvSelDate?'btn-primary':'btn-secondary');
    b.style.cssText='padding:6px 10px;font-size:12px;line-height:1.3;min-width:0';
    const dd=d.date.slice(5);
    b.innerHTML=dd+'<br><span style="font-size:10px;opacity:.7">'+d.trips.length+' รอบ</span>';
    b.onclick=()=>{_dlvSelDate=d.date;renderDlvDays();};
    c.appendChild(b);
  });
  renderDlvTrips();
}
function renderDlvTrips() {
  const day = _dlvData.find(d=>d.date===_dlvSelDate);
  const sm = document.getElementById('dlvSummary');
  const tc = document.getElementById('dlvTrips'); tc.innerHTML='';
  if(!day){sm.innerHTML='';return;}
  const tot = day.trips.reduce((s,t)=>s+t.count,0);
  const totB = day.trips.reduce((s,t)=>s+t.total,0);
  sm.innerHTML='<div style="display:flex;justify-content:space-between;align-items:center">'
    +'<div><div style="font-size:11px;color:var(--muted)">วันนี้รวม</div><div style="font-size:22px;font-weight:700">'+tot+' <span style="font-size:13px;font-weight:400">พัสดุ</span></div></div>'
    +'<div style="text-align:right"><div style="font-size:11px;color:var(--muted)">ยอดรวม</div><div style="font-size:22px;font-weight:700;color:var(--green)">฿'+totB.toLocaleString('th-TH',{minimumFractionDigits:0,maximumFractionDigits:2})+'</div></div>'
    +'</div><div style="margin-top:6px;font-size:12px;color:var(--muted)">มาส่ง <b style="color:var(--amber)">'+day.trips.length+'</b> รอบ (ห่าง>30นาที = คนละรอบ)</div>';
  day.trips.forEach((trip,i) => {
    const div = document.createElement('div');
    div.style.cssText='background:var(--surface);border-radius:12px;padding:14px;margin-bottom:10px;border:1px solid var(--border);cursor:pointer';

    // ── สร้าง chips "เขตที่ปิด" ──
    let areaChips = '';
    const provs = trip.provinces || [];
    const fulls = trip.areas || [];
    const noArea = trip.noArea || 0;
    if(provs.length || fulls.length || noArea){
      // ใช้ระดับจังหวัด-อำเภอถ้ามีไม่เกิน 6 อัน, ไม่งั้นใช้ระดับจังหวัด
      const useFull = (fulls.length>0 && fulls.length<=6);
      const list = useFull ? fulls : provs;
      const chips = list.slice(0,8).map(x =>
        '<span style="display:inline-block;background:rgba(180,127,255,.12);color:#c4b5fd;border:1px solid rgba(180,127,255,.3);padding:2px 8px;border-radius:10px;font-size:11px;margin:2px 4px 2px 0;white-space:nowrap">📍 '+x.name+' <b>×'+x.count+'</b></span>'
      ).join('');
      const more = list.length>8 ? '<span style="font-size:11px;color:var(--muted)">+'+(list.length-8)+' พื้นที่</span>' : '';
      const noChip = noArea>0 ? '<span style="display:inline-block;background:rgba(255,255,255,.04);color:var(--muted);border:1px solid var(--border);padding:2px 8px;border-radius:10px;font-size:11px;margin:2px 4px 2px 0">❓ ไม่ระบุ ×'+noArea+'</span>' : '';
      areaChips = '<div style="margin-top:6px;line-height:1.7">'+chips+noChip+more+'</div>';
    }

    let hdr='<div style="display:flex;justify-content:space-between;align-items:flex-start">'
      +'<div style="flex:1;min-width:0"><b style="color:var(--amber)">🚚 คนที่ '+trip.trip+'</b> <span style="font-size:12px;color:var(--muted)">'+trip.timeRange+'</span>'
      +(trip.carriers?' <span style="font-size:11px;background:var(--hover);padding:1px 6px;border-radius:6px;color:var(--green)">'+trip.carriers.join(', ')+'</span>':'')
      +'<div style="font-size:13px;margin-top:2px">'+trip.count+' พัสดุ · ฿'+trip.total.toLocaleString('th-TH',{minimumFractionDigits:0,maximumFractionDigits:2})+'</div>'
      +areaChips
      +'</div>'
      +'<span class="dlv-arrow" style="font-size:16px;color:var(--muted);margin-left:8px">▼</span></div>';
    let detail='<div class="dlv-detail" style="display:none;margin-top:10px;border-top:1px solid var(--border);padding-top:8px">';
    trip.orders.forEach((o,j) => {
      const prodList = (o.p&&o.p.length) ? o.p.map(p=>'<div style="font-size:11px;color:var(--muted);padding-left:12px;margin-top:1px">• '+p+'</div>').join('') : '';
      const trLink = o.tr ? '<div style="font-size:10px;margin-top:2px"><span style="color:var(--muted)">📦 '+o.tr+'</span></div>' : '';
      const carrier = o.c ? '<span style="font-size:10px;background:var(--hover);padding:0 5px;border-radius:4px;margin-left:4px;color:var(--green)">'+o.c+'</span>' : '';
      const areaLine = o.a ? '<div style="font-size:10px;margin-top:2px;color:#c4b5fd">📍 '+o.a+'</div>' : '';
      detail+='<div style="padding:6px 0;border-bottom:1px solid var(--border)">'
        +'<div style="display:flex;justify-content:space-between;font-size:12px">'
        +'<div><span style="color:var(--muted);margin-right:4px">'+o.time+'</span><b>'+o.m+'</b>'+carrier+'</div>'
        +'<div style="font-weight:600;white-space:nowrap">฿'+o.t.toLocaleString('th-TH',{minimumFractionDigits:0,maximumFractionDigits:2})+'</div></div>'
        +areaLine+prodList+trLink+'</div>';
    });
    detail+='</div>';
    div.innerHTML=hdr+detail;
    div.onclick=()=>{
      const d=div.querySelector('.dlv-detail'), a=div.querySelector('.dlv-arrow');
      if(d.style.display==='none'){d.style.display='block';a.textContent='▲';}
      else{d.style.display='none';a.textContent='▼';}
    };
    tc.appendChild(div);
  });
}

// ── Toast ──────────────────────────────────────────────────────────────────
function toast(msg, type='info') {
  const wrap = document.getElementById('toastWrap');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  wrap.appendChild(el);
  setTimeout(()=>el.remove(), 3500);
}

// ── Loader ─────────────────────────────────────────────────────────────────
function showLoader(v) {
  document.getElementById('loader').classList.toggle('show', v);
}

// ── API Helper ─────────────────────────────────────────────────────────────
async function api(url, opts={}) {
  const res = await fetch(url, {
    headers: {'Content-Type':'application/json'},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if(res.status===401) { location.reload(); return null; }
  return res.json();
}

// ── Stats ──────────────────────────────────────────────────────────────────
async function loadStats(ignoreDate=false) {
  const selArea = (document.getElementById('areaFilter')||{}).value || '';
  const selCarrier = (document.getElementById('carrierFilter')||{}).value || '';
  if((selArea || selCarrier) && orders.length) {
    const list = _filteredList();
    const overdueThreshold = Math.floor(Date.now()/1000) - 72*3600;
    const vals = {
      total: list.length,
      transit: list.filter(o=>{
        const s=o.status||'';
        return (s.includes('กำลังจัดส่ง')||s==='จัดส่งแล้ว')&&!s.includes('ยกเลิก');
      }).length,
      done: list.filter(o=>
        ['เสร็จสิ้น','สำเร็จ','มาถึงแล้ว'].some(k=>(o.status||'').includes(k))
      ).length,
      cancel: list.filter(o=>(o.status||'').includes('ยกเลิก')).length,
      overdue: list.filter(o=>{
        const s=o.status||'';
        return (o.first_seen_ts||0)<=overdueThreshold
          &&!s.includes('ยกเลิก')&&!s.includes('เสร็จสิ้น')
          &&!s.includes('สำเร็จ')&&!s.includes('มาถึงแล้ว');
      }).length,
      prep: list.filter(o=>(o.status||'').includes('กำลังเตรียม')).length,
    };
    _applyStats(vals);
    return;
  }
  const df = document.getElementById('dateFrom').value;
  const dt = document.getElementById('dateTo').value;
  const params = new URLSearchParams();
  if(df) params.set('from', df);
  if(dt) params.set('to', dt);
  params.set('overdue_all', '1');
  const qs = params.toString();
  const data = await api('/api/stats' + (qs ? '?'+qs : ''));
  if(!data) return;
  _applyStats({
    total: data.total, transit: data.in_transit, done: data.delivered,
    cancel: data.cancelled, overdue: data.overdue, prep: data.preparing,
  });
  if(data.last_sync)
    document.getElementById('lastSync').textContent = '🕐 ซิงค์ล่าสุด: '+data.last_sync;
}

function _applyStats(v) {
  // Main stats cards (ms-* IDs)
  const el = id => document.getElementById(id);
  if(el('ms-total'))   el('ms-total').textContent   = v.total ?? '-';
  if(el('ms-done'))    el('ms-done').textContent    = v.done ?? '-';
  if(el('ms-transit')) el('ms-transit').textContent = v.transit ?? '-';
  if(el('ms-cancel'))  el('ms-cancel').textContent  = v.cancel ?? '-';
  if(el('ms-overdue')) el('ms-overdue').textContent = v.overdue ?? '-';
  // Calculate revenue from loaded orders
  _updateRevenue();
}

function _updateRevenue() {
  let rev = 0;
  (orders||[]).forEach(o => {
    if(!['เสร็จสิ้น','สำเร็จ','มาถึงแล้ว'].some(k=>(o.status||'').includes(k))) return;
    const amt = parseFloat((o.total||'').replace(/[฿,]/g,''));
    if(!isNaN(amt) && amt > 0) rev += amt;
  });
  const revStr = '฿' + Math.round(rev).toLocaleString();
  const msRev = document.getElementById('ms-revenue');
  if(msRev) msRev.textContent = revStr;
  const topRev = document.getElementById('topbarRevenue');
  if(topRev) topRev.textContent = rev > 0 ? ('💰 ' + revStr) : '';
}

async function _updateSidebarPanels() {
  try {
    const data = await api(`/api/sidebar?trend_days=${trendDays}`);
    if(!data) return;

    // ── 1. กิจกรรมล่าสุด (วันนี้เท่านั้น) ──
    const feed = document.getElementById('sbFeed');
    if(feed) {
      if(data.activity && data.activity.length) {
        feed.innerHTML = data.activity.map(a => {
          const st = a.status||'';
          let color='#888', label=st.slice(0,15);
          if(st.includes('เสร็จสิ้น')||st.includes('สำเร็จ')) { color='#26de81'; label='ส่งสำเร็จ'; }
          else if(st.includes('กำลังจัดส่ง')||st==='จัดส่งแล้ว') { color='#ffd166'; label='กำลังจัดส่ง'; }
          else if(st.includes('ยกเลิก')) { color='#ff4f6d'; label='ยกเลิก'; }
          else if(st.includes('กำลังเตรียม')) { color='#c4b5fd'; label='กำลังเตรียม'; }
          const ago = a.ts ? _timeAgo(a.ts) : '';
          return `<div class="sb-feed-item">
            <div class="sb-fdot" style="background:${color}"></div>
            <div style="flex:1;min-width:0">
              <div class="sb-fname">${a.name}</div>
              <div class="sb-ftime">${label}${ago?' - '+ago:''}</div>
            </div>
          </div>`;
        }).join('');
      } else {
        feed.innerHTML = '<div class="sb-feed-empty">วันนี้ยังไม่มีกิจกรรม</div>';
      }
    }

    // ── 2. สรุปสถานะ (วันนี้ — ใช้ sb- IDs ไม่ซ้ำกับ main) ──
    const dateLabel = document.getElementById('sbStatusDate');
    if(dateLabel) dateLabel.textContent = data.date || '';
    const sbMap = {prep:'sb-prep', transit:'sb-transit', check:'sb-check',
                   cancel:'sb-cancel', overdue:'sb-overdue', closed:'sb-done'};
    for(const [key, id] of Object.entries(sbMap)) {
      const el = document.getElementById(id);
      if(el) el.textContent = data[key] ?? 0;
    }
    const revEl = document.getElementById('sb-revenue');
    if(revEl && !currentEtaFilter) revEl.textContent = '฿' + Math.round(data.revenue||0).toLocaleString();
    // ── คาดการณ์พัสดุถึง (ETA) ──
    const etaMap = {eta_today:'sb-eta-today', eta_tomorrow:'sb-eta-tomorrow',
                    eta_dayafter:'sb-eta-dayafter', eta_late:'sb-eta-late', eta_overdue:'sb-eta-overdue'};
    for(const [key, id] of Object.entries(etaMap)) {
      const el = document.getElementById(id);
      if(el) el.textContent = data[key] ?? 0;
    }
    // ── แสดง ETA ที่เรียนรู้ ──
    const learnedEl = document.getElementById('eta-learned-list');
    if(learnedEl && data.eta_learned) {
      const entries = Object.entries(data.eta_learned).sort((a,b) => b[1].n - a[1].n);
      if(entries.length === 0) {
        learnedEl.textContent = 'ยังไม่มีข้อมูลเพียงพอ (ต้องมีออเดอร์ปิดงานอย่างน้อย 2 รายการต่อร้าน)';
      } else {
        learnedEl.innerHTML = entries.map(([shop, d]) =>
          `<div style="display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px solid var(--border)">
            <span>🏪 ${shop}</span>
            <span style="color:var(--blue)">~${d.avg} วัน <span style="color:var(--muted)">(${d.n} ออเดอร์)</span></span>
          </div>`
        ).join('');
      }
    }
    // Topbar + main stats revenue — only update from sidebar when no date filter is active
    const dfVal = document.getElementById('dateFrom').value;
    const dtVal = document.getElementById('dateTo').value;
    const noDateFilter = !dfVal && !dtVal;
    const msRev = document.getElementById('ms-revenue');
    if(msRev && noDateFilter) msRev.textContent = '฿' + Math.round(data.revenue||0).toLocaleString();
    const topRev = document.getElementById('topbarRevenue');
    if(topRev && noDateFilter) topRev.textContent = data.revenue > 0 ? ('💰 ฿' + Math.round(data.revenue).toLocaleString()) : '';

    // ── 3. แนวโน้มออเดอร์เข้า ──
    const chartDiv = document.getElementById('sbChart');
    const trendSum = document.getElementById('trendSummary');
    // highlight active button
    document.querySelectorAll('.td-btn').forEach(b => b.classList.toggle('active', parseInt(b.dataset.d)===trendDays));
    if(chartDiv && data.trend && data.trend.length) {
      const totalIncoming = data.trend.reduce((s,d) => s + d.incoming, 0);
      const avgPerDay = (totalIncoming / data.trend.length).toFixed(1);
      if(trendSum) trendSum.textContent = `รวม ${totalIncoming} | เฉลี่ย ${avgPerDay}/วัน`;

      // สำหรับ 30d+ จัดกลุ่มรายสัปดาห์
      let chartData = data.trend;
      let labelFn = d => new Date(d.date+'T12:00:00').toLocaleDateString('th-TH',{weekday:'short'});
      if(trendDays > 14) {
        // group by week
        const weeks = [];
        for(let i=0; i<chartData.length; i+=7) {
          const chunk = chartData.slice(i, i+7);
          const sum = chunk.reduce((s,d) => s+d.incoming, 0);
          const startDate = chunk[0].date.slice(5); // MM-DD
          weeks.push({date: chunk[0].date, incoming: sum, label: startDate});
        }
        chartData = weeks;
        labelFn = d => d.label || d.date.slice(5);
      }

      const maxVal = Math.max(...chartData.map(d=>d.incoming), 1);
      const barH = 55;
      chartDiv.innerHTML = `
        <div style="display:flex;align-items:flex-end;gap:${trendDays>14?'2':'3'}px;height:${barH+20}px;padding:0 2px">
          ${chartData.map(d => {
            const h = Math.max(3, Math.round(d.incoming/maxVal*barH));
            const isToday = d.date === data.date;
            const col = isToday ? '#ff4f6d' : 'rgba(77,166,255,0.5)';
            const lbl = labelFn(d);
            return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%">
              <span style="font-size:${trendDays>14?'8':'9'}px;color:${isToday?'#ff4f6d':'#888'};font-weight:${isToday?'700':'400'};margin-bottom:2px">${d.incoming}</span>
              <div style="width:100%;height:${h}px;background:${col};border-radius:3px 3px 0 0;transition:.3s"></div>
              <span style="font-size:${trendDays>14?'7':'9'}px;color:${isToday?'#fff':'#666'};margin-top:2px">${lbl}</span>
            </div>`;
          }).join('')}
        </div>
      `;
    }
  } catch(e) { /* sidebar is optional */ }
}

function setTrendDays(n) {
  trendDays = n;
  _updateSidebarPanels();
}

function _timeAgo(ts) {
  const sec = Math.floor(Date.now()/1000) - ts;
  if(sec < 60) return 'เมื่อกี้';
  if(sec < 3600) return Math.floor(sec/60) + ' นาทีที่แล้ว';
  if(sec < 86400) return Math.floor(sec/3600) + ' ชม.ที่แล้ว';
  return Math.floor(sec/86400) + ' วันที่แล้ว';
}

// ── Orders ─────────────────────────────────────────────────────────────────
function debounceLoad() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(loadOrders, 400);
}

function clearFilters() {
  document.getElementById('searchInput').value = '';
  document.getElementById('accountFilter').value = 'ทุกบัญชี';
  document.getElementById('statusFilter').value = 'ทั้งหมด';
  document.getElementById('dateFrom').value = '';
  document.getElementById('dateTo').value   = '';
  const _af = document.getElementById('areaFilter');
  if(_af) _af.value = '';
  const _cf = document.getElementById('carrierFilter');
  if(_cf) _cf.value = '';
  const _bb = document.getElementById('bottomBar');
  if(_bb) _bb.classList.remove('filtered');
  if(summaryOpen) toggleSummary();
  setActiveBtn('btn-all');
  ['card-total','card-transit','card-done','card-cancel','card-overdue','card-prep'].forEach(c => {
    const el = document.getElementById(c); if(el) el.classList.remove('active');
  });
  loadOrders();
}

function setActiveBtn(id) {
  ['btn-today','btn-yesterday','btn-3days','btn-5days','btn-7days','btn-15days','btn-30days','btn-all','btn-alldata'].forEach(b => {
    const el=document.getElementById(b);
    if(el) el.classList.remove('btn-active');
  });
  if(id) { const el=document.getElementById(id); if(el) el.classList.add('btn-active'); }
}

function filterToday() {
  const today = _localDateStr();
  document.getElementById('dateFrom').value = today;
  document.getElementById('dateTo').value   = today;
  setActiveBtn('btn-today');
  loadOrders();
  if(document.getElementById('statusFilter').value === 'ค้างนาน') loadStats(true);
}

function filterYesterday() {
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  const y = _localDateStr(yesterday);
  document.getElementById('dateFrom').value = y;
  document.getElementById('dateTo').value   = y;
  setActiveBtn('btn-yesterday');
  loadOrders();
  if(document.getElementById('statusFilter').value === 'ค้างนาน') loadStats(true);
}

function filterAll() {
  document.getElementById('dateFrom').value = '';
  document.getElementById('dateTo').value   = '';
  setActiveBtn('btn-alldata');
  loadOrders();
}

function filterDays(n) {
  const today = new Date();
  const to = _localDateStr(today);
  const from_d = new Date(today.getTime() - (n - 1) * 86400000);
  const from = _localDateStr(from_d);
  document.getElementById('dateFrom').value = from;
  document.getElementById('dateTo').value   = to;
  const map = {3:'btn-3days',5:'btn-5days',7:'btn-7days',15:'btn-15days',30:'btn-30days'};
  setActiveBtn(map[n] || null);
  loadOrders();
  if(document.getElementById('statusFilter').value === 'ค้างนาน') loadStats(true);
}

function onStatusFilterChange() {
  const st = document.getElementById('statusFilter').value;
  const cardMap = {
    'ทั้งหมด':'card-total','กำลังจัดส่ง':'card-transit',
    'จัดส่งเสร็จสิ้นแล้ว':'card-done','ยกเลิก':'card-cancel',
    'ค้างนาน':'card-overdue','กำลังเตรียม':'card-prep'
  };
  ['card-total','card-transit','card-done','card-cancel','card-overdue','card-prep'].forEach(c => {
    const el = document.getElementById(c); if(el) el.classList.remove('active');
  });
  if(cardMap[st]) { const el = document.getElementById(cardMap[st]); if(el) el.classList.add('active'); }
  loadOrders();
  if(st === 'ค้างนาน') {
    loadStats(true);
  } else {
    loadStats();
  }
}

const etaLabels = {today:'วันนี้',tomorrow:'พรุ่งนี้',dayafter:'มะรืน',late:'ช้ากว่ากำหนด',overdue:'เลยกำหนด'};
function filterByEta(category) {
  currentEtaFilter = category;
  document.getElementById('statusFilter').value = 'ทั้งหมด';
  document.getElementById('searchInput').value = '';
  document.getElementById('dateFrom').value = '';
  document.getElementById('dateTo').value = '';
  // clear card active states
  ['card-total','card-transit','card-done','card-cancel','card-overdue','card-prep'].forEach(c => {
    const el = document.getElementById(c); if(el) el.classList.remove('active');
  });
  // highlight active row
  document.querySelectorAll('.sb-row').forEach(r => r.classList.remove('sb-active'));
  const etaIdMap = {today:'sb-eta-today',tomorrow:'sb-eta-tomorrow',dayafter:'sb-eta-dayafter',late:'sb-eta-late',overdue:'sb-eta-overdue'};
  const activeEl = document.getElementById(etaIdMap[category]);
  if(activeEl) activeEl.closest('.sb-row').classList.add('sb-active');
  // show reset button
  const resetRow = document.getElementById('sb-reset-row');
  if(resetRow) resetRow.style.display = '';
  loadOrders().then(() => _updateEtaRevenue());
  loadStats();
}

function _updateEtaRevenue() {
  const label = document.getElementById('sb-revenue-label');
  const el = document.getElementById('sb-revenue');
  if(!label || !el) return;
  if(!currentEtaFilter) {
    label.textContent = '💵 ยอดชำระรวม';
    el.style.color = '#26de81';
    return;
  }
  let sum = 0;
  for(const o of _filteredList()) {
    const t = String(o.total||'').replace(/[฿,]/g,'');
    const v = parseFloat(t);
    if(!isNaN(v)) sum += v;
  }
  label.textContent = '💵 ยอดคาดการณ์';
  el.textContent = '฿' + Math.round(sum).toLocaleString();
  el.style.color = '#4da6ff';
}

function resetFilter() {
  currentEtaFilter = '';
  document.querySelectorAll('.sb-row').forEach(r => r.classList.remove('sb-active'));
  ['card-total','card-transit','card-done','card-cancel','card-overdue','card-prep'].forEach(c => {
    const el = document.getElementById(c); if(el) el.classList.remove('active');
  });
  document.getElementById('statusFilter').value = 'ทั้งหมด';
  document.getElementById('searchInput').value = '';
  document.getElementById('dateFrom').value = '';
  document.getElementById('dateTo').value = '';
  const resetRow = document.getElementById('sb-reset-row');
  if(resetRow) resetRow.style.display = 'none';
  _updateEtaRevenue();
  loadOrders();
  loadStats();
}

function filterByStat(status) {
  currentEtaFilter = '';
  document.querySelectorAll('.sb-row').forEach(r => r.classList.remove('sb-active'));
  // hide ETA revenue
  _updateEtaRevenue();
  document.getElementById('statusFilter').value = status;
  document.getElementById('searchInput').value = '';
  const cardMap = {
    'ทั้งหมด':'card-total','กำลังจัดส่ง':'card-transit',
    'จัดส่งเสร็จสิ้นแล้ว':'card-done','ยกเลิก':'card-cancel',
    'ค้างนาน':'card-overdue','กำลังเตรียม':'card-prep'
  };
  ['card-total','card-transit','card-done','card-cancel','card-overdue','card-prep'].forEach(c => {
    const el = document.getElementById(c); if(el) el.classList.remove('active');
  });
  const cid = cardMap[status];
  if(cid) { const el = document.getElementById(cid); if(el) el.classList.add('active'); }
  // highlight matching sidebar status row
  const sbIdMap = {'กำลังเตรียม':'sb-prep','กำลังจัดส่ง':'sb-transit','ยกเลิก':'sb-cancel','ค้างนาน':'sb-overdue','จัดส่งเสร็จสิ้นแล้ว':'sb-done','ต้องเช็ค':'sb-check'};
  const sbId = sbIdMap[status];
  if(sbId) { const el = document.getElementById(sbId); if(el) el.closest('.sb-row').classList.add('sb-active'); }
  // show/hide reset button
  const resetRow = document.getElementById('sb-reset-row');
  if(resetRow) resetRow.style.display = (status && status !== 'ทั้งหมด') ? '' : 'none';
  loadOrders();
  loadStats();
}

async function loadOrders() {
  const params = new URLSearchParams({
    search:  document.getElementById('searchInput').value,
    account: document.getElementById('accountFilter').value,
    status:  document.getElementById('statusFilter').value,
    from:    document.getElementById('dateFrom').value,
    to:      document.getElementById('dateTo').value,
  });
  if(currentEtaFilter) params.set('eta', currentEtaFilter);
  const data = await api('/api/orders?'+params);
  if(!data) return;
  orders = data.orders || [];
  populateAreaFilter();
  populateCarrierFilter();
  if(summaryOpen) renderSummary(); else renderOrders();
  _updateSidebarPanels();
  loadStats();
  // อัป badge บัญชีที่เลือก
  const accVal = document.getElementById('accountFilter').value;
  const badge = document.getElementById('activeAccBadge');
  if(badge) badge.textContent = accVal === 'ทุกบัญชี' ? '' : '🔵 '+_shortEmail(accVal);
}

async function loadAccountFilter() {
  const data = await api('/api/accounts');
  if(!data) return;
  const sel = document.getElementById('accountFilter');
  const cur = sel.value;
  // เก็บ option ทุกบัญชี ไว้
  sel.innerHTML = '<option value="ทุกบัญชี">📧 ทุกบัญชี</option>';
  (data.accounts || []).forEach(acc => {
    const label = _shortEmail(acc);
    const opt = document.createElement('option');
    opt.value = acc;
    opt.textContent = label;
    if(acc === cur) opt.selected = true;
    sel.appendChild(opt);
  });
}

function _shortEmail(email) {
  // niranakamthong@zohomail.com → 📮 niranak... (Zoho)
  // taopubg789@gmail.com → 📬 taopubg... (Gmail)
  const [user, domain] = (email || '').split('@');
  const icon = domain && domain.includes('gmail') ? '📬' : '📮';
  const provider = domain ? domain.split('.')[0] : '';
  const short = user && user.length > 8 ? user.slice(0,8)+'...' : user;
  return `${icon} ${short} (${provider})`;
}

function statusBadge(st) {
  if(!st) return '<span class="badge badge-gray">-</span>';
  const s = st.trim();
  if(s.includes('เสร็จสิ้น')||s.includes('สำเร็จ')||s.includes('มาถึงแล้ว'))
    return `<span class="badge badge-green">${s}</span>`;
  if(s.includes('ยกเลิก'))
    return `<span class="badge badge-red">${s}</span>`;
  if(s.includes('กำลังจัดส่ง')||s.includes('จัดส่งแล้ว'))
    return `<span class="badge badge-amber">${s}</span>`;
  if(s.includes('กำลังเตรียม'))
    return `<span class="badge badge-blue">${s}</span>`;
  if(s.includes('รอ'))
    return `<span class="badge badge-purple">${s}</span>`;
  return `<span class="badge badge-gray">${s}</span>`;
}

// ── Area filter + Summary + Render ───────────────────────────────────
let summaryOpen = false;

function populateAreaFilter() {
  const sel = document.getElementById('areaFilter');
  if(!sel) return;
  const cur = sel.value;
  const provMap = {}; let hasNone = false;
  orders.forEach(o => {
    if(!o.ship_area){ hasNone = true; return; }
    const parts = o.ship_area.split(',').map(s=>s.trim());
    const prov=parts[0], dist=parts[1]||'';
    if(!provMap[prov]) provMap[prov]=new Set();
    if(dist) provMap[prov].add(dist);
  });
  let html='<option value="">📍 ทุกปลายทาง</option>';
  Object.keys(provMap).sort().forEach(prov=>{
    html+=`<option value="${prov}">📍 ${prov}</option>`;
    [...provMap[prov]].sort().forEach(dist=>{
      html+=`<option value="${prov}, ${dist}">   ↳ ${dist}</option>`;
    });
  });
  if(hasNone) html+='<option value="__none__">— ไม่ระบุปลายทาง</option>';
  sel.innerHTML=html;
  if([...sel.options].some(o=>o.value===cur)) sel.value=cur;
}

function _getCarrier(o) {
  const t = (o.tracking||'').toLowerCase();
  if(!t) return 'ไม่ระบุขนส่ง';
  if(t.includes('flash')) return 'Flash';
  if(t.includes('j&t')||t.replace(/ /g,'').includes('j&t')||t.includes('jt')||t.includes('jnt')) return 'J&T';
  if(t.includes('kerry')||t.includes('kex')) return 'Kerry/KEX';
  if(t.includes('ninja')) return 'Ninja Van';
  if(t.includes('spx')||t.includes('shopee express')) return 'SPX';
  if(t.includes('best')) return 'Best';
  return 'อื่นๆ';
}

function populateCarrierFilter() {
  const sel = document.getElementById('carrierFilter');
  if(!sel) return;
  const cur = sel.value;
  const cMap = {};
  orders.forEach(o => {
    const c = _getCarrier(o);
    cMap[c] = (cMap[c]||0) + 1;
  });
  let html='<option value="">🚚 ทุกขนส่ง</option>';
  Object.entries(cMap).sort((a,b)=>b[1]-a[1]).forEach(([c,n])=>{
    html+=`<option value="${c}">🚚 ${c} (${n})</option>`;
  });
  sel.innerHTML=html;
  if([...sel.options].some(o=>o.value===cur)) sel.value=cur;
}

function _filteredList() {
  const selArea=(document.getElementById('areaFilter')||{}).value||'';
  const selCarrier=(document.getElementById('carrierFilter')||{}).value||'';
  return orders.filter(o=>{
    if(selArea) {
      if(selArea==='__none__') { if(o.ship_area) return false; }
      else if(!selArea.includes(',')) { if(!(o.ship_area||'').startsWith(selArea)) return false; }
      else { if(o.ship_area!==selArea) return false; }
    }
    if(selCarrier && _getCarrier(o)!==selCarrier) return false;
    return true;
  });
}

function applyCarrierFilter() {
  if(summaryOpen) renderSummary(); else renderOrders();
  if(currentEtaFilter) _updateEtaRevenue();
  loadStats();
}

function _updateBottomBar(count, total, filterLabel) {
  const bar=document.getElementById('bottomBar');
  if(bar) bar.classList.toggle('filtered', !!filterLabel);
  const suffix=filterLabel?` · ${filterLabel}`:'';
  document.getElementById('rowCount').textContent=`${count} รายการ${suffix}`;
  document.getElementById('totalAmount').textContent=total>0
    ?`ยอดรวม: ฿${total.toLocaleString('th',{minimumFractionDigits:2})}`:'';
}

function applyAreaFilter() {
  if(summaryOpen) renderSummary(); else renderOrders();
  if(currentEtaFilter) _updateEtaRevenue();
  loadStats();
}

function toggleSummary() {
  summaryOpen=!summaryOpen;
  const panel=document.getElementById('summaryPanel');
  const tbl=document.querySelector('.table-wrap');
  const btn=document.getElementById('btnSummary');
  panel.style.display=summaryOpen?'block':'none';
  if(tbl) tbl.style.display=summaryOpen?'none':'';
  btn.classList.toggle('btn-active',summaryOpen);
  if(summaryOpen) renderSummary();
}

function renderSummary() {
  const list=_filteredList();
  const el=document.getElementById('summaryContent');
  const selArea=(document.getElementById('areaFilter')||{}).value||'';
  const NO_AREA='— ไม่ระบุปลายทาง';
  // ── รวบที่อยู่ ──
  const provMap={}; const carrierMap={};
  let grand=0, grandCount=0;
  list.forEach(o=>{
    const amt=parseFloat((o.total||'').replace(/[฿,]/g,''))||0;
    const prov=o.ship_area?o.ship_area.split(',')[0].trim():NO_AREA;
    const dist=o.ship_area&&o.ship_area.includes(',')?o.ship_area.split(',')[1].trim():'(ไม่ระบุอำเภอ)';
    // area
    if(!provMap[prov]) provMap[prov]={};
    if(!provMap[prov][dist]) provMap[prov][dist]={count:0,total:0};
    provMap[prov][dist].count++; provMap[prov][dist].total+=amt;
    // carrier
    const trk=o.tracking||''; 
    const carrier=trk.includes(':')?trk.split(':')[0].trim():'ไม่มีข้อมูลขนส่ง';
    if(!carrierMap[carrier]) carrierMap[carrier]={count:0,total:0};
    carrierMap[carrier].count++; carrierMap[carrier].total+=amt;
    grand+=amt; grandCount++;
  });
  if(!grandCount){el.innerHTML='<div style="color:var(--muted);padding:20px">ไม่มีออเดอร์</div>'; return;}
  // max for bars
  let maxDist=0;
  Object.values(provMap).forEach(d=>Object.values(d).forEach(x=>{if(x.total>maxDist)maxDist=x.total;}));
  let maxCarr=0;
  Object.values(carrierMap).forEach(x=>{if(x.total>maxCarr)maxCarr=x.total;});

  const fmt=v=>`฿${v.toLocaleString('th',{minimumFractionDigits:2})}`;
  const suffix=selArea?` · ${selArea}`:'';
  let html=`<div class="sum-header">
    <h3>💰 ยอดชำระเงินแยกตามปลายทาง${suffix}</h3>
    <span class="sum-grand">${fmt(grand)} (${grandCount} ออเดอร์)</span></div>`;

  // ── แยกตามปลายทาง ──
  html+='<div class="sum-section"><div class="sum-section-title">📍 แยกตามปลายทาง</div>';
  const provKeys=Object.keys(provMap).filter(k=>k!==NO_AREA).sort((a,b)=>{
    const ta=Object.values(provMap[a]).reduce((s,d)=>s+d.total,0);
    const tb=Object.values(provMap[b]).reduce((s,d)=>s+d.total,0);
    return tb-ta;
  });
  if(provMap[NO_AREA]) provKeys.push(NO_AREA);
  provKeys.forEach(prov=>{
    const dists=provMap[prov];
    const pt=Object.values(dists).reduce((s,d)=>s+d.total,0);
    const pc=Object.values(dists).reduce((s,d)=>s+d.count,0);
    html+=`<div class="sum-prov-name">📍 ${prov}<span class="pc">${pc}</span><span class="pt">${fmt(pt)}</span></div>`;
    Object.entries(dists).sort((a,b)=>b[1].total-a[1].total).forEach(([dist,d])=>{
      const pct=maxDist>0?(d.total/maxDist*100).toFixed(1):0;
      html+=`<div class="sum-row">
        <div class="sum-row-name">${dist}</div>
        <div class="sum-row-cnt">${d.count} ออเดอร์</div>
        <div class="sum-bar-wrap"><div class="sum-bar" style="width:${pct}%"></div></div>
        <div class="sum-row-amt">${fmt(d.total)}</div></div>`;
    });
  });
  html+='</div>';

  // ── แยกตามขนส่ง ──
  html+='<div class="sum-section"><div class="sum-section-title">🚚 แยกตามขนส่ง</div>';
  Object.entries(carrierMap).sort((a,b)=>b[1].total-a[1].total).forEach(([carrier,d])=>{
    const pct=maxCarr>0?(d.total/maxCarr*100).toFixed(1):0;
    html+=`<div class="sum-row">
      <div class="sum-row-name">${carrier}</div>
      <div class="sum-row-cnt">${d.count} ออเดอร์</div>
      <div class="sum-bar-wrap"><div class="sum-bar carrier" style="width:${pct}%"></div></div>
      <div class="sum-row-amt">${fmt(d.total)}</div></div>`;
  });
  html+='</div>';

  el.innerHTML=html;
  _updateBottomBar(grandCount, grand, selArea);
}

function renderOrders() {
  const list=_filteredList();
  const selArea=(document.getElementById('areaFilter')||{}).value||'';
  const selCarrier=(document.getElementById('carrierFilter')||{}).value||'';
  const _filterLabel=[selArea,selCarrier].filter(Boolean).join(' · ');
  const tbody=document.getElementById('ordersBody');
  const mobileDiv=document.getElementById('mobileCards');
  const empty=document.getElementById('emptyState');
  if(!list.length){
    tbody.innerHTML=''; if(mobileDiv)mobileDiv.innerHTML=''; empty.style.display='block';
    _updateBottomBar(0,0,_filterLabel); return;
  }
  empty.style.display='none';
  let totalAmt=0;
  tbody.innerHTML=list.map(o=>{
    const prods=(o.products||[]).join(', ')||'-';
    const dt=o.last_update_ts
      ?new Date(o.last_update_ts*1000).toLocaleDateString('th-TH',{day:'2-digit',month:'short',year:'2-digit'})
      :(o.date||'-');
    const amt=parseFloat((o.total||'').replace(/[฿,]/g,''));
    if(!isNaN(amt)) totalAmt+=amt;
    const trkUrl=trackingUrl(o.tracking);
    const trk=o.tracking
      ?(trkUrl
        ?`<a href="${trkUrl}" target="_blank" onclick="event.stopPropagation()" style="color:var(--green);font-size:11px;text-decoration:none" title="คลิกเพื่อเช็คพัสดุ">🔗 ${o.tracking.slice(0,20)}</a>`
        :`<span style="color:var(--green);font-size:11px">✓ ${o.tracking.slice(0,20)}</span>`)
      :'<span style="color:var(--muted);font-size:11px">-</span>';
    const areaChip=o.ship_area
      ?`<span class="chip-area">📍 ${o.ship_area}</span>`
      :`<span class="chip-area na">—</span>`;
    return `<tr onclick="openDetail('${o.order_id}')">
      <td><b style="font-size:12px;cursor:pointer" title="คลิกเพื่อคัดลอก" onclick="event.stopPropagation();copyOid(this,'${o.order_id||''}')">${o.order_id||'-'}</b></td>
      <td><span style="font-size:12px">${o.shop||'-'}</span></td>
      <td class="truncate" style="max-width:120px;font-size:12px">${o.merchant||'-'}</td>
      <td>${statusBadge(o.status)}</td>
      <td class="truncate" style="color:var(--muted);font-size:12px">${prods}</td>
      <td style="color:var(--green);font-weight:700;font-size:13px">${o.total||'-'}</td>
      <td>${trk}</td>
      <td style="color:var(--muted);font-size:11px;white-space:nowrap">${dt}</td>
      <td>${areaChip}</td>
    </tr>`;
  }).join('');

  // Mobile card view
  if(mobileDiv){
    mobileDiv.innerHTML=list.map(o=>{
      const prodName=((o.products||[])[0]||'ไม่ระบุสินค้า').slice(0,45);
      const dt=o.last_update_ts
        ?new Date(o.last_update_ts*1000).toLocaleDateString('th-TH',{day:'2-digit',month:'short'})
        :(o.date||'-');
      const trkNum=o.tracking?o.tracking.replace(/.*[:\s]([A-Za-z0-9]{8,})$/,'$1').slice(0,18):'';
      const trkMobileUrl=trackingUrl(o.tracking||'');
      const area=o.ship_area?o.ship_area.split(',')[0]:'';
      return `<div class="order-card" onclick="openDetail('${o.order_id}')">
        <div class="oc-top">
          <span class="oc-name">${prodName}</span>
          ${statusBadge(o.status)}
        </div>
        <div class="oc-meta">
          <span>${o.shop||'-'}</span>
          ${trkNum?(trkMobileUrl?`<a href="${trkMobileUrl}" target="_blank" onclick="event.stopPropagation()" style="color:var(--green);text-decoration:none">🔗 ${trkNum}</a>`:`<span>📦 ${trkNum}</span>`):''}
          <span class="oc-price">${o.total||'-'}</span>
          ${area?`<span>📍${area}</span>`:''}
          <span>${dt}</span>
        </div>
      </div>`;
    }).join('');
  }
  _updateBottomBar(list.length, totalAmt, _filterLabel);
}

function copyOid(el, text) {
  navigator.clipboard.writeText(text).then(()=>{
    const orig=el.textContent;
    el.textContent='✅ คัดลอกแล้ว';
    el.style.color='var(--green)';
    setTimeout(()=>{el.textContent=orig;el.style.color='';},1200);
  }).catch(()=>{});
}

function trackingUrl(tracking) {
  if(!tracking) return '';
  const m = tracking.match(/[:\s]([A-Za-z0-9]{8,})$/);
  const num = m ? m[1] : tracking.replace(/.*\s/, '').trim();
  if(!num || num.length < 6) return '';
  return 'https://t.17track.net/en#nums='+num;
}

function openDetail(orderId) {
  const o = orders.find(x => x.order_id===orderId);
  if(!o) return;
  currentOrderId = orderId;
  document.getElementById('modalOrderId').textContent = orderId;
  document.getElementById('m-shop').textContent     = o.shop||'-';
  document.getElementById('m-merchant').textContent = o.merchant||'-';
  document.getElementById('m-date').textContent     = o.date||'-';
  document.getElementById('m-account').textContent  = o.account||'-';
  const _ael = document.getElementById('m-area');
  if(_ael) _ael.textContent = o.ship_area || '—';
  document.getElementById('m-status').value   = o.status||'รอดำเนินการ';
  document.getElementById('m-tracking').value = o.tracking||'';
  // แสดงปุ่มเช็คพัสดุถ้ามี URL
  const btnTrack = document.getElementById('btn-track');
  if(btnTrack) {
    const url = trackingUrl(o.tracking||'');
    btnTrack.style.display = url ? '' : 'none';
    btnTrack.dataset.url = url;
  }
  document.getElementById('m-total').value    = o.total||'';
  document.getElementById('m-products').value = (o.products||[]).join('\n');
  document.getElementById('detailOverlay').classList.add('open');
}

function openTrackingLink() {
  const btn = document.getElementById('btn-track');
  if(btn && btn.dataset.url) window.open(btn.dataset.url, '_blank');
}

function closeModal() {
  document.getElementById('detailOverlay').classList.remove('open');
  currentOrderId = null;
}

async function saveOrder() {
  if(!currentOrderId) return;
  const body = {
    status:   document.getElementById('m-status').value,
    tracking: document.getElementById('m-tracking').value.trim(),
    total:    document.getElementById('m-total').value.trim(),
  };
  showLoader(true);
  const res = await api(`/api/orders/${encodeURIComponent(currentOrderId)}`,
                        {method:'PUT', body});
  showLoader(false);
  if(res && res.ok) {
    toast('✅ บันทึกสำเร็จ','success');
    closeModal();
    loadOrders();
  } else {
    toast('❌ บันทึกไม่สำเร็จ','error');
  }
}

async function deleteOrder() {
  if(!currentOrderId) return;
  if(!confirm(`ยืนยันลบออเดอร์ ${currentOrderId}?`)) return;
  showLoader(true);
  const res = await api(`/api/orders/${encodeURIComponent(currentOrderId)}`,
                        {method:'DELETE'});
  showLoader(false);
  if(res && res.ok) {
    toast('🗑️ ลบสำเร็จ','success');
    closeModal();
    loadOrders();
  } else {
    toast('❌ ลบไม่สำเร็จ','error');
  }
}

// ── Sync ───────────────────────────────────────────────────────────────────
async function startSync() {
  const n = parseInt(document.getElementById('syncCount').value);
  const btn = document.getElementById('syncBtn');
  btn.disabled = true;
  btn.textContent = '⏳ กำลังซิงค์...';
  const res = await api('/api/sync', {method:'POST', body:{n_emails:n}});
  if(res && res.ok) {
    toast('🔄 เริ่มซิงค์แล้ว กรุณารอสักครู่','info');
    // poll for status
    clearInterval(syncTimer);
    syncTimer = setInterval(async()=>{
      await refreshSyncLog();
      const st = await api('/api/sync/status');
      if(st && !st.running) {
        clearInterval(syncTimer);
        btn.disabled = false;
        btn.textContent = '▶ เริ่มซิงค์';
        loadOrders();
        toast('✅ ซิงค์เสร็จแล้ว','success');
      }
    }, 2000);
  } else {
    btn.disabled = false;
    btn.textContent = '▶ เริ่มซิงค์';
    toast('❌ ไม่สามารถเริ่มซิงค์ได้','error');
  }
}

async function quickSync() {
  const res = await api('/api/sync', {method:'POST', body:{n_emails:300}});
  if(res && res.ok) toast('🔄 เริ่มซิงค์แล้ว','info');
}

async function refreshSyncLog() {
  const data = await api('/api/sync/status');
  if(!data) return;
  const logEl = document.getElementById('syncLog');
  if(data.log && data.log.length) {
    logEl.innerHTML = data.log.map(l=>`<div>${l}</div>`).join('');
    logEl.scrollTop = logEl.scrollHeight;
  }
  const badge = document.getElementById('syncStatusBadge');
  if(data.running)
    badge.innerHTML = '<span class="badge badge-amber">🔄 กำลังซิงค์...</span>';
  else if(data.last_sync)
    badge.innerHTML = `<span class="badge badge-green">✅ ซิงค์ล่าสุด: ${data.last_sync}</span>`;
  else
    badge.innerHTML = '<span class="badge badge-gray">ยังไม่เคยซิงค์</span>';

  // Update auto-sync panel
  const autoBtn = document.getElementById('autoToggleBtn');
  const autoInd = document.getElementById('autoIndicator');
  const autoDesc = document.getElementById('autoSyncDesc');
  if(data.auto_enabled) {
    if(autoBtn) { autoBtn.textContent='⏸ หยุด'; autoBtn.className='btn btn-amber'; }
    if(autoInd) autoInd.textContent='⚡ Auto ON';
    if(autoDesc) autoDesc.textContent=`ซิงค์อัตโนมัติทุก ${data.auto_interval} วินาที`;
  } else {
    if(autoBtn) { autoBtn.textContent='▶ เปิด'; autoBtn.className='btn btn-green'; }
    if(autoInd) { autoInd.textContent='⏸ Auto OFF'; autoInd.style.color='var(--muted)'; }
    if(autoDesc) autoDesc.textContent='Auto-sync ปิดอยู่';
  }
  const nc = document.getElementById('autoNewCount');
  const uc = document.getElementById('autoUpdCount');
  const ls = document.getElementById('autoLastSync');
  if(nc) nc.textContent = data.auto_new || 0;
  if(uc) uc.textContent = data.auto_upd || 0;
  if(ls) ls.textContent = data.last_sync || '-';
}

async function toggleAutoSync() {
  const data = await api('/api/sync/status');
  const current = data ? data.auto_enabled : true;
  const res = await api('/api/sync/auto', {method:'POST', body:{enabled: !current}});
  if(res && res.ok) {
    toast(res.auto_enabled ? '⚡ เปิด Auto-Sync แล้ว' : '⏸ หยุด Auto-Sync แล้ว',
          res.auto_enabled ? 'success' : 'info');
    refreshSyncLog();
  }
}

async function setAutoInterval() {
  const interval = parseInt(document.getElementById('autoInterval').value);
  await api('/api/sync/auto', {method:'POST', body:{interval}});
  toast(`⏱ ตั้งซิงค์ทุก ${interval} วินาที`, 'info');
}

// ── Settings ───────────────────────────────────────────────────────────────
let settingsData = {accounts:[], telegram:{}};

async function loadSettings() {
  const data = await api('/api/settings');
  if(!data) return;
  settingsData = data;
  renderAccounts();
  // Telegram
  const tg = data.telegram || {};
  document.getElementById('tg-enabled').checked      = !!tg.enabled;
  document.getElementById('tg-token').value           = tg.bot_token || '';
  document.getElementById('tg-chat').value            = tg.chat_id   || '';
  document.getElementById('tg-instant').checked       = tg.instant !== false;
  document.getElementById('tg-alert-imap').checked    = tg.alert_imap_error !== false;
  document.getElementById('tg-alert-syncfail').checked= tg.alert_sync_fail !== false;
  document.getElementById('tg-alert-login').checked   = tg.alert_login_fail !== false;
  document.getElementById('tg-alert-anomaly').checked = tg.alert_order_anomaly !== false;
  document.getElementById('tg-alert-delivered').checked = tg.alert_delivered_order !== false;
  document.getElementById('tg-recovery').checked      = tg.notify_recovery !== false;
  document.getElementById('tg-fail-threshold').value  = tg.sync_fail_threshold || 3;
  document.getElementById('tg-low').value             = tg.order_low_threshold || 20;
  document.getElementById('tg-high').value            = tg.order_high_threshold || 300;
  document.getElementById('tg-digest').checked        = tg.digest  !== false;
  document.getElementById('tg-digest-products').checked = !!tg.digest_show_products;
  document.getElementById('tg-digest-movement').checked = !!tg.digest_only_on_movement;
  document.getElementById('tg-digest-mode').value     = tg.digest_mode || 'ทุก 1 ชั่วโมง';
  document.getElementById('tg-dedup').value           = tg.dedup_minutes || 30;
  document.getElementById('tg-quiet').checked         = !!tg.quiet_enabled;
  document.getElementById('tg-quiet-start').value     = tg.quiet_start || '23:00';
  document.getElementById('tg-quiet-end').value       = tg.quiet_end   || '08:00';
  // ETA days
  const etaDaysEl = document.getElementById('eta-days');
  if(etaDaysEl) etaDaysEl.value = data.eta_days || 2;
}

function renderAccounts() {
  const el = document.getElementById('accountsList');
  if(!settingsData.accounts || !settingsData.accounts.length) {
    el.innerHTML = '<div style="color:var(--muted);font-size:13px;padding:12px 0">ยังไม่มีบัญชีอีเมล</div>';
    return;
  }
  el.innerHTML = settingsData.accounts.map((acc,i) => {
    const hasPw = acc.has_password;
    return `
    <div class="account-card" id="acc-card-${i}" style="position:relative">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
        <span style="font-size:18px">${(acc.imap_server||'').includes('gmail') ? '📫' : '📧'}</span>
        <b style="font-size:14px">${acc.email || `บัญชี ${i+1}`}</b>
        ${hasPw ? '<span class="badge badge-green" style="font-size:10px">✓ มีรหัสผ่าน</span>' : '<span class="badge badge-red" style="font-size:10px">ยังไม่ใส่รหัสผ่าน</span>'}
      </div>
      <div class="field-row"><label>Email</label>
        <input type="email" value="${acc.email||''}" id="acc-email-${i}"
               oninput="settingsData.accounts[${i}].email=this.value">
      </div>
      <div class="field-row"><label>App Password ${hasPw?'(ว่าง=ใช้เดิม)':'(จำเป็น)'}</label>
        <input type="password" placeholder="${hasPw?'ว่างไว้=ใช้รหัสผ่านเดิม':'ใส่ App Password'}"
               id="acc-pw-${i}" oninput="settingsData.accounts[${i}].password=this.value">
      </div>
      <div style="display:grid;grid-template-columns:2fr 1fr;gap:8px">
        <div class="field-row"><label>IMAP Server</label>
          <input type="text" value="${acc.imap_server||'imap.zoho.com'}"
                 oninput="settingsData.accounts[${i}].imap_server=this.value">
        </div>
        <div class="field-row"><label>Port</label>
          <input type="number" value="${acc.imap_port||993}"
                 oninput="settingsData.accounts[${i}].imap_port=parseInt(this.value)">
        </div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn btn-green" onclick="saveSettings()">💾 บันทึก</button>
        <button class="btn btn-secondary" onclick="testImap(${i})" id="test-btn-${i}">🔌 ทดสอบ</button>
        <button class="btn" style="margin-left:auto;background:rgba(255,79,109,.15);color:var(--red);border:1px solid rgba(255,79,109,.3);font-size:12px;padding:8px 14px" onclick="removeAccount(${i})">🗑️ ลบ</button>
      </div>
      <div id="test-result-${i}" style="margin-top:8px;font-size:13px"></div>
    </div>`;
  }).join('');
}

function addAccount() {
  settingsData.accounts = settingsData.accounts || [];
  settingsData.accounts.push({
    email:'', password:'', has_password:false,
    imap_server:'imap.zoho.com', imap_port:993
  });
  renderAccounts();
}

function removeAccount(i) {
  if(!confirm('ลบบัญชีนี้?')) return;
  settingsData.accounts.splice(i,1);
  renderAccounts();
}

async function testImap(i) {
  const btn = document.getElementById(`test-btn-${i}`);
  const res_el = document.getElementById(`test-result-${i}`);
  btn.disabled = true;
  btn.textContent = '⏳ กำลังทดสอบ...';
  res_el.textContent = '';
  // Save current values first (so server uses latest password)
  await saveSettings();
  const res = await api('/api/settings/test_imap', {method:'POST', body:{index:i}});
  btn.disabled = false;
  btn.textContent = '🔌 ทดสอบ';
  if(!res) return;
  res_el.style.color = res.ok ? 'var(--green)' : 'var(--accent)';
  res_el.textContent = res.msg;
}

async function saveSettings() {
  const payload = {
    accounts: settingsData.accounts,
    telegram: {
      enabled:              document.getElementById('tg-enabled').checked,
      bot_token:            document.getElementById('tg-token').value.trim(),
      chat_id:              document.getElementById('tg-chat').value.trim(),
      instant:              document.getElementById('tg-instant').checked,
      alert_imap_error:     document.getElementById('tg-alert-imap').checked,
      alert_sync_fail:      document.getElementById('tg-alert-syncfail').checked,
      alert_login_fail:     document.getElementById('tg-alert-login').checked,
      alert_order_anomaly:  document.getElementById('tg-alert-anomaly').checked,
      alert_delivered_order: document.getElementById('tg-alert-delivered').checked,
      notify_recovery:      document.getElementById('tg-recovery').checked,
      sync_fail_threshold:  parseInt(document.getElementById('tg-fail-threshold').value) || 3,
      order_low_threshold:  parseInt(document.getElementById('tg-low').value) || 20,
      order_high_threshold: parseInt(document.getElementById('tg-high').value) || 300,
      digest:               document.getElementById('tg-digest').checked,
      digest_show_products: document.getElementById('tg-digest-products').checked,
      digest_only_on_movement: document.getElementById('tg-digest-movement').checked,
      digest_mode:          document.getElementById('tg-digest-mode').value,
      dedup_minutes:        parseInt(document.getElementById('tg-dedup').value) || 30,
      quiet_enabled:        document.getElementById('tg-quiet').checked,
      quiet_start:          document.getElementById('tg-quiet-start').value,
      quiet_end:            document.getElementById('tg-quiet-end').value,
    }
  };
  // ── ETA days ──
  const etaDaysEl = document.getElementById('eta-days');
  if(etaDaysEl) payload.eta_days = parseInt(etaDaysEl.value) || 2;
  showLoader(true);
  const res = await api('/api/settings', {method:'POST', body:payload});
  showLoader(false);
  if(res && res.ok) {
    toast('✅ บันทึกการตั้งค่าแล้ว','success');
    loadSettings(); // reload to refresh has_password badge
  } else toast('❌ บันทึกไม่สำเร็จ','error');
}

async function saveTelegram() {
  await saveSettings();
}

async function testTelegram() {
  const token = document.getElementById('tg-token').value.trim();
  const chatId = document.getElementById('tg-chat').value.trim();
  if(!token || !chatId) { toast('กรุณาใส่ Bot Token และ Chat ID ก่อน','error'); return; }
  try {
    const res = await fetch(
      `https://api.telegram.org/bot${token}/sendMessage`,
      {method:'POST', headers:{'Content-Type':'application/json'},
       body: JSON.stringify({chat_id: chatId, text: '✅ Order Tracker Web — ทดสอบการเชื่อมต่อสำเร็จ!'})}
    );
    const d = await res.json();
    if(d.ok) toast('✅ ส่ง Telegram สำเร็จ!','success');
    else toast(`❌ Telegram ผิดพลาด: ${d.description}`,'error');
  } catch(e) { toast('❌ ไม่สามารถส่งได้: '+e,'error'); }
}

async function testTelegramServer() {
  const el = document.getElementById('tg-test-result');
  el.textContent = '⏳ กำลังทดสอบจากเซิร์ฟเวอร์...';
  el.style.color = 'var(--muted)';
  await saveSettings();
  const res = await api('/api/settings/test_telegram', {method:'POST', body:{
    bot_token: document.getElementById('tg-token').value.trim(),
    chat_id:   document.getElementById('tg-chat').value.trim(),
  }});
  if(!res) { el.textContent = '❌ ไม่สามารถเชื่อมต่อเซิร์ฟเวอร์'; el.style.color='var(--accent)'; return; }
  el.style.color = res.ok ? 'var(--green)' : 'var(--accent)';
  el.textContent = res.msg;
}

async function fetchChatId() {
  const btn = document.getElementById('btn-fetch-chat');
  const el = document.getElementById('tg-chat-result');
  btn.disabled = true;
  btn.textContent = '⏳ กำลังดึง...';
  el.textContent = '';
  const token = document.getElementById('tg-token').value.trim();
  if(!token) { toast('กรุณาใส่ Bot Token ก่อน','error'); btn.disabled=false; btn.textContent='🆔 ดึง Chat ID'; return; }
  const res = await api('/api/settings/fetch_chat_id', {method:'POST', body:{bot_token: token}});
  btn.disabled = false;
  btn.textContent = '🆔 ดึง Chat ID';
  if(!res) { el.textContent = '❌ เชื่อมต่อเซิร์ฟเวอร์ไม่ได้'; el.style.color='var(--accent)'; return; }
  el.style.color = res.ok ? 'var(--green)' : 'var(--accent)';
  el.textContent = res.msg;
  if(res.ok && res.chat_id) {
    document.getElementById('tg-chat').value = res.chat_id;
  }
}

async function savePassword() {
  const pw = document.getElementById('newPassword').value.trim();
  if(!pw) { toast('กรุณาใส่รหัสผ่าน','error'); return; }
  const res = await api('/api/settings/password', {method:'POST', body:{password:pw}});
  if(res && res.ok) { toast('✅ เปลี่ยนรหัสผ่านแล้ว','success'); document.getElementById('newPassword').value=''; }
  else toast('❌ ผิดพลาด','error');
}

async function confirmClearAll() {
  if(!confirm('⚠️ ยืนยันลบออเดอร์ทั้งหมด?\n\nไม่สามารถกู้คืนได้!')) return;
  const res = await api('/api/orders', {method:'DELETE'});
  if(res && res.ok) { toast('🗑️ ล้างข้อมูลแล้ว','success'); loadOrders(); }
}

async function logout() {
  if(!confirm('ยืนยันออกจากระบบ?')) return;
  await fetch('/logout', {method:'POST', credentials:'same-origin'});
  window.location.href = '/login';
}

async function shutdownServer() {
  if(!confirm('ยืนยันปิดเซิร์ฟเวอร์? (ต้องเปิดโปรแกรมใหม่ถ้าจะใช้งานอีก)')) return;
  try { await fetch('/api/shutdown', {method:'POST', credentials:'same-origin'}); } catch(e) {}
  document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-size:1.3rem;color:#888">⏻ เซิร์ฟเวอร์ปิดแล้ว — ปิดหน้านี้ได้เลย</div>';
}

// Keyboard shortcut: Escape = close modal
document.addEventListener('keydown', e => {
  if(e.key==='Escape') closeModal();
});
</script>
</body>
</html>"""


# ══════════════════════════════════════════════════════════════════════════════
#  Routes
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
@login_required
def index():
    return render_template_string(MAIN_HTML)


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if not username or not password:
            error = "กรุณากรอกชื่อผู้ใช้และรหัสผ่าน"
        else:
            try:
                import urllib.request as _ur
                payload = json.dumps({
                    "action": "login",
                    "username": username,
                    "password": password,
                    "hwid": GLOBAL_HWID,
                }).encode()
                req = _ur.Request(AUTH_GS_URL, data=payload,
                                  headers={"Content-Type": "application/json"}, method="POST")
                with _ur.urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read().decode())
                if result.get("success"):
                    session["username"] = username
                    session["expires"] = result.get("expires")
                    return index()
                else:
                    error = result.get("error", "เข้าสู่ระบบไม่สำเร็จ")
            except Exception as e:
                error = f"เชื่อมต่อเซิร์ฟเวอร์ไม่ได้: {e}"
    return render_template_string(LOGIN_HTML, error=error)


@app.route("/logout", methods=["GET", "POST"])
def do_logout():
    session.clear()
    if request.method == "GET":
        return render_template_string(LOGIN_HTML)
    return jsonify({"ok": True})


@app.route("/register", methods=["POST"])
def do_register():
    data = request.json or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"success": False, "error": "กรุณากรอกชื่อผู้ใช้และรหัสผ่าน"})
    if len(username) < 4:
        return jsonify({"success": False, "error": "ชื่อผู้ใช้ต้องมีอย่างน้อย 4 ตัวอักษร"})
    if len(password) < 4:
        return jsonify({"success": False, "error": "รหัสผ่านต้องมีอย่างน้อย 4 ตัวอักษร"})
    try:
        import urllib.request as _ur
        payload = json.dumps({
            "action": "register",
            "username": username,
            "password": password,
            "hwid": GLOBAL_HWID,
        }).encode()
        req = _ur.Request(AUTH_GS_URL, data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        return jsonify(result)
    except Exception as e:
        return jsonify({"success": False, "error": f"เชื่อมต่อเซิร์ฟเวอร์ไม่ได้: {e}"})


@app.route("/api/auth/status")
def auth_status():
    if session.get("username"):
        return jsonify({"logged_in": True, "username": session["username"],
                        "expires": session.get("expires"), "version": CURRENT_VERSION,
                        "license_valid": GLOBAL_LICENSE_VALID, "hwid": GLOBAL_HWID})
    return jsonify({"logged_in": False})


@app.route("/api/license/status")
def license_status():
    return jsonify({"valid": GLOBAL_LICENSE_VALID, "expires": GLOBAL_LICENSE_EXPIRES,
                    "msg": GLOBAL_LICENSE_MSG, "hwid": GLOBAL_HWID, "version": CURRENT_VERSION})


# ─── Orders API ───────────────────────────────────────────────────────────────

@app.route("/api/orders")
@login_required
def get_orders():
    search  = request.args.get("search","").strip()
    status  = request.args.get("status","ทั้งหมด")
    df      = request.args.get("from","")
    dt      = request.args.get("to","")
    account = request.args.get("account","ทุกบัญชี")
    eta_filter = request.args.get("eta", "")
    orders  = store.list_all(search=search or None,
                             status_filter=status if not eta_filter else "ทั้งหมด",
                             date_from=df or None if not eta_filter else None,
                             date_to=dt or None if not eta_filter else None,
                             account=account if account != "ทุกบัญชี" else None)
    # ── ETA post-filter ──
    if eta_filter:
        today_dt = _now_bkk().replace(hour=0, minute=0, second=0, microsecond=0)
        yesterday_dt = today_dt - timedelta(days=1)
        tomorrow_dt = today_dt + timedelta(days=1)
        dayafter_dt = today_dt + timedelta(days=2)
        filtered = []
        for o in orders:
            st = (o.status or "").strip()
            if "กำลังจัดส่ง" not in st and st != "จัดส่งแล้ว":
                continue
            if "ยกเลิก" in st or "เสร็จสิ้น" in st or "สำเร็จ" in st or "มาถึงแล้ว" in st:
                continue
            ship_ts = o.last_update_ts or 0
            if not ship_ts:
                continue
            days = _get_eta_days(o)
            eta_dt = _ts_to_bkk(int(ship_ts)) + timedelta(days=days)
            eta_date = eta_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            if eta_filter == "today" and eta_date == today_dt:
                filtered.append(o)
            elif eta_filter == "tomorrow" and eta_date == tomorrow_dt:
                filtered.append(o)
            elif eta_filter == "dayafter" and eta_date == dayafter_dt:
                filtered.append(o)
            elif eta_filter == "late" and eta_date == yesterday_dt:
                filtered.append(o)
            elif eta_filter == "overdue" and eta_date < yesterday_dt:
                filtered.append(o)
        orders = filtered
    return jsonify({"orders": [o.to_dict() for o in orders]})


@app.route("/api/accounts")
@login_required
def get_accounts():
    """ดึงบัญชีอีเมลที่มีออเดอร์จริงๆ ใน DB"""
    accounts = store.list_accounts()
    return jsonify({"accounts": accounts})


@app.route("/api/orders/<order_id>", methods=["PUT"])
@login_required
def update_order(order_id):
    data = request.json or {}
    ok = store.update_fields(order_id, data)
    return jsonify({"ok": ok})


@app.route("/api/orders/<order_id>", methods=["DELETE"])
@login_required
def delete_order(order_id):
    store.delete(order_id)
    return jsonify({"ok": True})


@app.route("/api/orders", methods=["DELETE"])
@login_required
def clear_all():
    store.clear_all()
    return jsonify({"ok": True})


# ─── Stats API ────────────────────────────────────────────────────────────────

@app.route("/api/stats")
@login_required
def get_stats():
    df = request.args.get("from", "").strip() or None
    dt = request.args.get("to", "").strip() or None
    overdue_all = request.args.get("overdue_all", "").strip() == "1"
    data = store.stats(date_from=df, date_to=dt, overdue_all_time=overdue_all)
    data["last_sync"] = sync_status.get("last_sync")
    return jsonify(data)


@app.route("/api/delivery-trips")
@login_required
def delivery_trips():
    """จัดกลุ่มพัสดุที่ปิดงานแล้ว ตามรอบคนขับ (>30นาที = คนละรอบ) ทุกขนส่ง"""
    all_orders = store.list_all()
    delivered = [o for o in all_orders if _is_delivered_status(o.status) and o.tracking]
    delivered.sort(key=lambda o: o.last_update_ts or 0)
    from collections import defaultdict
    by_date = defaultdict(list)
    for o in delivered:
        ts = o.last_update_ts or 0
        if ts == 0: continue
        dt = _ts_to_bkk(ts)
        tn = _extract_tracking_number(o.tracking) if o.tracking else ''
        prods = json.loads(o.products) if isinstance(o.products, str) else (o.products or [])
        carrier = _carrier_name(o.tracking)
        area = (getattr(o, 'ship_area', '') or '').strip()
        by_date[dt.strftime('%Y-%m-%d')].append({
            'm': (o.merchant or '')[:40], 'p': [_short_product_name(p) or p[:60] for p in prods[:5]],
            't': _to_amount(o.total), 'tr': tn, 'ts': ts,
            'time': dt.strftime('%H:%M'), 'c': carrier, 'a': area
        })
    result = []
    gap = int(request.args.get('gap', 1800))
    for d in sorted(by_date.keys()):
        items = sorted(by_date[d], key=lambda x: x['ts'])
        trips, cur = [], [items[0]]
        for i in range(1, len(items)):
            if items[i]['ts'] - items[i-1]['ts'] > gap:
                trips.append(cur); cur = [items[i]]
            else:
                cur.append(items[i])
        trips.append(cur)
        day = {'date': d, 'trips': []}
        for ti, trip in enumerate(trips):
            carriers = list(set(x['c'] for x in trip))
            # ── สรุปเขต/พื้นที่ที่ปิดในรอบนี้ ──
            prov_count = {}      # จังหวัด → จำนวน
            full_count = {}      # "จังหวัด - อำเภอ" → จำนวน (ละเอียด)
            no_area_n = 0
            for x in trip:
                a = x.get('a') or ''
                if not a:
                    no_area_n += 1
                    continue
                parts = [p.strip() for p in a.split(',')]
                prov = parts[0] if parts else ''
                dist = parts[1].strip() if len(parts) > 1 else ''
                if prov:
                    prov_count[prov] = prov_count.get(prov, 0) + 1
                full = f"{prov} - {dist}" if (prov and dist) else (prov or dist or '')
                if full:
                    full_count[full] = full_count.get(full, 0) + 1
            provinces = [{'name': k, 'count': v} for k, v in
                         sorted(prov_count.items(), key=lambda x: (-x[1], x[0]))]
            full_areas = [{'name': k, 'count': v} for k, v in
                          sorted(full_count.items(), key=lambda x: (-x[1], x[0]))]
            day['trips'].append({
                'trip': ti+1,
                'timeRange': f"{trip[0]['time']}-{trip[-1]['time']}",
                'count': len(trip),
                'total': round(sum(x['t'] for x in trip), 2),
                'carriers': carriers,
                'provinces': provinces,        # ระดับจังหวัด — ใช้แสดง chip
                'areas': full_areas,           # จังหวัด-อำเภอ ละเอียด
                'noArea': no_area_n,           # จำนวนที่ไม่ระบุพื้นที่
                'orders': [{'m':x['m'],'p':x['p'],'t':x['t'],'tr':x['tr'],
                            'time':x['time'],'c':x['c'],'a':x.get('a','')} for x in trip]
            })
        result.append(day)
    return jsonify(result)


@app.route("/api/sidebar")
@login_required
def get_sidebar():
    """ข้อมูลสำหรับ sidebar — วันนี้เสมอ + แนวโน้มตามจำนวนวัน"""
    trend_days = min(90, max(1, int(request.args.get("trend_days", 7))))
    today = _now_bkk().strftime("%Y-%m-%d")
    today_orders = store.list_all(day=today)

    # สรุปสถานะวันนี้
    prep = transit = check = cancel = overdue_cnt = closed = 0
    revenue = 0.0
    alert_hours = int(tg_state.get("alert_hours", 48) or 48)
    now_ts = int(_now_bkk().timestamp())
    for o in today_orders:
        st = o.status or ""
        if "กำลังเตรียม" in st and "ยกเลิก" not in st:
            prep += 1
        if ("กำลังจัดส่ง" in st or st == "จัดส่งแล้ว") and "ยกเลิก" not in st and "เสร็จสิ้น" not in st and "สำเร็จ" not in st:
            transit += 1
        if "ต้องเช็ค" in st:
            check += 1
        if "ยกเลิก" in st:
            cancel += 1
        if ("เสร็จสิ้น" in st or "สำเร็จ" in st or "มาถึงแล้ว" in st) and "ยกเลิก" not in st:
            closed += 1
            revenue += _to_amount(o.total)
        ref_ts = o.last_update_ts or o.first_seen_ts
        if ref_ts and ("กำลังจัดส่ง" in st or st == "จัดส่งแล้ว"):
            if "ยกเลิก" not in st and "เสร็จสิ้น" not in st and "สำเร็จ" not in st:
                if (now_ts - int(ref_ts)) >= alert_hours * 3600:
                    overdue_cnt += 1

    # กิจกรรมล่าสุด (6 รายการ)
    recent = sorted(today_orders, key=lambda o: -(o.last_update_ts or 0))[:6]
    activity = []
    for o in recent:
        name = (o.products[0] if o.products else "ไม่ระบุ")[:35]
        activity.append({"name": name, "status": o.status or "", "ts": o.last_update_ts or 0})

    # แนวโน้มออเดอร์เข้า — ตามจำนวนวัน (single query)
    trend = []
    start_date = (_now_bkk() - timedelta(days=trend_days - 1)).strftime("%Y-%m-%d")
    with store._connect() as conn:
        rows = conn.execute("""
            SELECT date(first_seen_ts,'unixepoch','+7 hours') AS day, COUNT(*) AS cnt
            FROM orders
            WHERE date(first_seen_ts,'unixepoch','+7 hours') >= ?
            GROUP BY day ORDER BY day
        """, (start_date,)).fetchall()
    day_counts = {r["day"]: r["cnt"] for r in rows}
    for i in range(trend_days - 1, -1, -1):
        d = (_now_bkk() - timedelta(days=i)).strftime("%Y-%m-%d")
        trend.append({"date": d, "incoming": day_counts.get(d, 0)})

    # ── คาดการณ์พัสดุถึง (ETA) — ใช้ Smart ETA ──────────────────
    today_dt = _now_bkk().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_dt = today_dt - timedelta(days=1)
    tomorrow_dt = today_dt + timedelta(days=1)
    dayafter_dt = today_dt + timedelta(days=2)

    eta_today = eta_tomorrow = eta_dayafter = eta_late = eta_overdue_cnt = 0
    with store._connect() as conn:
        eta_rows = conn.execute("""
            SELECT shop, merchant, last_update_ts FROM orders
            WHERE (status LIKE '%กำลังจัดส่ง%' OR status='จัดส่งแล้ว')
              AND status NOT LIKE '%ยกเลิก%'
              AND status NOT LIKE '%เสร็จสิ้น%'
              AND status NOT LIKE '%สำเร็จ%'
              AND status NOT LIKE '%มาถึงแล้ว%'
        """).fetchall()
    fallback_days = int(load_settings().get("eta_days", 2))
    learned = _compute_shop_eta()
    for r in eta_rows:
        ship_ts = r["last_update_ts"] or 0
        if not ship_ts:
            continue
        merchant = (r["merchant"] or "").strip()
        shop = (r["shop"] or "").strip()
        if merchant and merchant in learned:
            days = learned[merchant]["rounded"]
        elif shop and shop in learned:
            days = learned[shop]["rounded"]
        else:
            days = fallback_days
        eta_dt = _ts_to_bkk(ship_ts) + timedelta(days=days)
        eta_date = eta_dt.replace(hour=0, minute=0, second=0, microsecond=0)
        if eta_date < yesterday_dt:
            eta_overdue_cnt += 1
        elif eta_date == yesterday_dt:
            eta_late += 1
        elif eta_date == today_dt:
            eta_today += 1
        elif eta_date == tomorrow_dt:
            eta_tomorrow += 1
        elif eta_date == dayafter_dt:
            eta_dayafter += 1

    return jsonify({
        "date": today,
        "prep": prep, "transit": transit, "check": check,
        "cancel": cancel, "overdue": overdue_cnt, "closed": closed,
        "revenue": round(revenue, 2),
        "activity": activity,
        "trend": trend,
        "eta_today": eta_today,
        "eta_tomorrow": eta_tomorrow,
        "eta_dayafter": eta_dayafter,
        "eta_late": eta_late,
        "eta_overdue": eta_overdue_cnt,
        "eta_learned": {s: {"avg": d["avg_days"], "n": d["count"]} for s, d in learned.items()},
    })


# ─── Sync API ─────────────────────────────────────────────────────────────────

@app.route("/api/sync", methods=["POST"])
@login_required
def trigger_sync():
    if sync_status["running"]:
        return jsonify({"ok": False, "msg": "กำลังซิงค์อยู่แล้ว"})
    settings = load_settings()
    accounts = settings.get("accounts", [])
    if not accounts:
        return jsonify({"ok": False, "msg": "ยังไม่มีบัญชีอีเมล กรุณาตั้งค่าก่อน"})
    n = int((request.json or {}).get("n_emails", 300))
    run_sync_background(accounts, n)
    return jsonify({"ok": True})


@app.route("/api/sync/status")
@login_required
def sync_status_api():
    return jsonify({
        "running":       sync_status["running"],
        "log":           sync_status["log"][-50:],
        "last_sync":     sync_status["last_sync"],
        "auto_enabled":  sync_status.get("auto_enabled", True),
        "auto_interval": sync_status.get("auto_interval", 60),
        "auto_new":      sync_status.get("auto_new", 0),
        "auto_upd":      sync_status.get("auto_upd", 0),
    })


@app.route("/api/sync/auto", methods=["POST"])
@login_required
def set_auto_sync():
    d = request.json or {}
    if "enabled" in d:
        sync_status["auto_enabled"] = bool(d["enabled"])
    if "interval" in d:
        sync_status["auto_interval"] = max(30, int(d["interval"]))
    return jsonify({
        "ok": True,
        "auto_enabled":  sync_status["auto_enabled"],
        "auto_interval": sync_status["auto_interval"],
    })


# ─── Settings API ─────────────────────────────────────────────────────────────

TG_KEY = "โหมดเตือนเทเลแกรม"

def _get_tg(settings):
    """Extract telegram config from settings (supports Thai-key format)."""
    tg = settings.get(TG_KEY, {})
    immediate = tg.get("แจ้งเตือนทันที", {}) or {}
    digest = tg.get("สรุปรายรอบ", {}) or {}
    dedup = tg.get("กันสแปม", {}) or {}
    quiet = tg.get("ช่วงเงียบ", {}) or {}
    return {
        "enabled":              tg.get("เปิดใช้งาน", False),
        "bot_token":            tg.get("บอทโทเคน", ""),
        "chat_id":              tg.get("แชทไอดี", ""),
        "instant":              immediate.get("เปิดใช้งาน", True),
        "alert_imap_error":     immediate.get("เตือนเมื่อเชื่อมต่ออีเมลมีปัญหา", True),
        "alert_sync_fail":      immediate.get("เตือนเมื่อซิงก์ล้มเหลวต่อเนื่อง", True),
        "sync_fail_threshold":  int(immediate.get("จำนวนครั้งซิงก์ล้มเหลวติดกันก่อนเตือน", 3) or 3),
        "alert_login_fail":     immediate.get("เตือนเมื่อเข้าสู่ระบบอีเมลไม่ผ่าน", True),
        "alert_order_anomaly":  immediate.get("เตือนเมื่อปิดพัสดุผิดปกติ (รายค่าย)",
                                 immediate.get("เตือนเมื่อจำนวนออเดอร์ผิดปกติ", True)),
        "alert_delivered_order": immediate.get("แจ้งเตือนจัดส่งเสร็จสิ้นทีละรายการ", True),
        "order_low_threshold":  int(immediate.get("ขั้นต่ำปกติปิดพัสดุต่อค่าย",
                                     immediate.get("ขั้นต่ำออเดอร์ต่อรอบ", 20)) or 20),
        "order_high_threshold": int(immediate.get("สูงสุดปกติปิดพัสดุต่อค่าย",
                                     immediate.get("สูงสุดออเดอร์ต่อรอบ", 300)) or 300),
        "notify_recovery":      dedup.get("แจ้งเมื่อระบบกลับมาปกติ", True),
        "digest":               digest.get("เปิดใช้งาน", True),
        "digest_mode":          digest.get("ความถี่", "ทุก 1 ชั่วโมง"),
        "digest_show_products": digest.get("แสดงรายชื่อสินค้า", False),
        "digest_only_on_movement": digest.get("ส่งเฉพาะเมื่อมีการเคลื่อนไหว", False),
        "dedup_minutes":        int(dedup.get("นาทีขั้นต่ำก่อนแจ้งเหตุเดิมซ้ำ", 30) or 30),
        "quiet_enabled":        quiet.get("เปิดใช้งาน", False),
        "quiet_start":          quiet.get("เวลาเริ่ม", "23:00"),
        "quiet_end":            quiet.get("เวลาสิ้นสุด", "08:00"),
    }


def _set_tg(settings, tg_data):
    """Write telegram config back into settings using Thai-key format."""
    existing = settings.get(TG_KEY, {})
    existing["เปิดใช้งาน"] = bool(tg_data.get("enabled", False))
    existing["บอทโทเคน"]   = str(tg_data.get("bot_token", "")).strip()
    existing["แชทไอดี"]    = str(tg_data.get("chat_id", "")).strip()
    inst = existing.setdefault("แจ้งเตือนทันที", {})
    inst["เปิดใช้งาน"] = bool(tg_data.get("instant", True))
    inst["เตือนเมื่อเชื่อมต่ออีเมลมีปัญหา"] = bool(tg_data.get("alert_imap_error", True))
    inst["เตือนเมื่อซิงก์ล้มเหลวต่อเนื่อง"] = bool(tg_data.get("alert_sync_fail", True))
    inst["จำนวนครั้งซิงก์ล้มเหลวติดกันก่อนเตือน"] = int(tg_data.get("sync_fail_threshold", 3) or 3)
    inst["เตือนเมื่อเข้าสู่ระบบอีเมลไม่ผ่าน"] = bool(tg_data.get("alert_login_fail", True))
    inst["เตือนเมื่อปิดพัสดุผิดปกติ (รายค่าย)"] = bool(tg_data.get("alert_order_anomaly", True))
    inst["แจ้งเตือนจัดส่งเสร็จสิ้นทีละรายการ"] = bool(tg_data.get("alert_delivered_order", True))
    inst["ขั้นต่ำปกติปิดพัสดุต่อค่าย"] = int(tg_data.get("order_low_threshold", 20) or 20)
    inst["สูงสุดปกติปิดพัสดุต่อค่าย"] = int(tg_data.get("order_high_threshold", 300) or 300)
    digest = existing.setdefault("สรุปรายรอบ", {})
    digest["เปิดใช้งาน"] = bool(tg_data.get("digest", True))
    digest["ความถี่"] = str(tg_data.get("digest_mode", "ทุก 1 ชั่วโมง") or "ทุก 1 ชั่วโมง")
    digest["แสดงรายชื่อสินค้า"] = bool(tg_data.get("digest_show_products", False))
    digest["ส่งเฉพาะเมื่อมีการเคลื่อนไหว"] = bool(tg_data.get("digest_only_on_movement", False))
    dedup = existing.setdefault("กันสแปม", {})
    dedup["นาทีขั้นต่ำก่อนแจ้งเหตุเดิมซ้ำ"] = int(tg_data.get("dedup_minutes", 30) or 30)
    dedup["แจ้งเมื่อระบบกลับมาปกติ"] = bool(tg_data.get("notify_recovery", True))
    quiet = existing.setdefault("ช่วงเงียบ", {})
    quiet["เปิดใช้งาน"] = bool(tg_data.get("quiet_enabled", False))
    quiet["เวลาเริ่ม"]   = str(tg_data.get("quiet_start", "23:00"))
    quiet["เวลาสิ้นสุด"] = str(tg_data.get("quiet_end", "08:00"))
    settings[TG_KEY] = existing


@app.route("/api/settings")
@login_required
def get_settings():
    data = load_settings()
    # Return accounts (passwords masked for display but tracked server-side)
    accounts = []
    for acc in data.get("accounts", []):
        a = dict(acc)
        a["has_password"] = bool(a.get("password"))
        a["password"] = ""   # never send password to browser
        accounts.append(a)
    return jsonify({
        "accounts": accounts,
        "telegram": _get_tg(data),
        "alert_hours": data.get("alert_hours", 48),
        "eta_days": data.get("eta_days", 2),
    })


@app.route("/api/settings", methods=["POST"])
@login_required
def save_settings_api():
    incoming = request.json or {}
    settings = load_settings()

    # ── Accounts ─────────────────────────────────────────────────
    old_accounts = settings.get("accounts", [])
    new_accounts = []
    for i, acc in enumerate(incoming.get("accounts", [])):
        old = old_accounts[i] if i < len(old_accounts) else {}
        pw = acc.get("password", "").strip()
        # If browser sends empty or placeholder → keep stored password
        if not pw or pw.startswith("•"):
            pw = old.get("password", "")
        new_accounts.append({
            "email":         acc.get("email", "").strip(),
            "password":      pw,
            "imap_server":   acc.get("imap_server", "imap.zoho.com").strip(),
            "imap_port":     int(acc.get("imap_port", 993)),
            "imap_last_uid": old.get("imap_last_uid", 0),
        })
    settings["accounts"] = new_accounts

    # ── Telegram ─────────────────────────────────────────────────
    if "telegram" in incoming:
        _set_tg(settings, incoming["telegram"])

    # ── Alert hours ───────────────────────────────────────────────
    if "alert_hours" in incoming:
        settings["alert_hours"] = int(incoming["alert_hours"])

    # ── ETA days ──────────────────────────────────────────────────
    if "eta_days" in incoming:
        settings["eta_days"] = max(1, min(7, int(incoming["eta_days"])))

    save_settings(settings)
    return jsonify({"ok": True})


@app.route("/api/settings/password", methods=["POST"])
@login_required
def change_password():
    pw = (request.json or {}).get("password", "").strip()
    if not pw:
        return jsonify({"ok": False})
    settings = load_settings()
    settings["web_password"] = pw
    save_settings(settings)
    return jsonify({"ok": True})


@app.route("/api/settings/test_imap", methods=["POST"])
@login_required
def test_imap():
    """Quick IMAP connection test."""
    d = request.json or {}
    idx = int(d.get("index", 0))
    settings = load_settings()
    accounts = settings.get("accounts", [])
    if idx >= len(accounts):
        return jsonify({"ok": False, "msg": "ไม่พบบัญชีนี้"})
    acc = accounts[idx]
    try:
        conn = imaplib.IMAP4_SSL(acc.get("imap_server","imap.zoho.com"),
                                  int(acc.get("imap_port", 993)))
        conn.login(acc["email"], acc["password"])
        _, data = conn.select("INBOX")
        count = int(data[0]) if data and data[0] else 0
        conn.logout()
        return jsonify({"ok": True, "msg": f"✅ เชื่อมต่อสำเร็จ — {acc['email']} มี {count} เมลใน Inbox"})
    except imaplib.IMAP4.error as e:
        return jsonify({"ok": False, "msg": f"❌ Login ไม่ผ่าน: {e}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"❌ เชื่อมต่อไม่ได้: {e}"})


@app.route("/api/settings/test_telegram", methods=["POST"])
@login_required
def test_telegram_api():
    """ทดสอบส่ง Telegram จากฝั่ง server (ใช้ได้แม้ browser ไม่ connect Telegram)"""
    d = request.json or {}
    token = str(d.get("bot_token", "")).strip()
    chat_id = str(d.get("chat_id", "")).strip()
    if not token or not chat_id:
        # fallback: ใช้ค่าจาก settings
        settings = load_settings()
        tg = settings.get("โหมดเตือนเทเลแกรม", {}) or {}
        if not token:
            token = str(tg.get("บอทโทเคน", "") or "").strip()
        if not chat_id:
            chat_id = str(tg.get("แชทไอดี", "") or "").strip()
    if not token or not chat_id:
        return jsonify({"ok": False, "msg": "กรุณาใส่ Bot Token และ Chat ID"})
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": "✅ Order Tracker Web — ทดสอบการเชื่อมต่อสำเร็จ!",
        }).encode("utf-8")
        req_obj = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req_obj, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        result = json.loads(raw)
        if result.get("ok"):
            return jsonify({"ok": True, "msg": "✅ ส่ง Telegram สำเร็จ!"})
        else:
            return jsonify({"ok": False, "msg": f"❌ {result.get('description', 'Unknown error')}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"❌ ส่งไม่สำเร็จ: {e}"})


@app.route("/api/settings/fetch_chat_id", methods=["POST"])
@login_required
def fetch_chat_id_api():
    """ดึง Chat ID อัตโนมัติจาก Telegram bot"""
    d = request.json or {}
    token = str(d.get("bot_token", "")).strip()
    if not token:
        settings = load_settings()
        tg = settings.get("โหมดเตือนเทเลแกรม", {}) or {}
        token = str(tg.get("บอทโทเคน", "") or "").strip()
    if not token:
        return jsonify({"ok": False, "msg": "กรุณาใส่ Bot Token ก่อน"})
    try:
        url = f"https://api.telegram.org/bot{token}/getUpdates?limit=100"
        with urllib.request.urlopen(url, timeout=12) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        data = json.loads(raw)
        if not data.get("ok"):
            return jsonify({"ok": False, "msg": "ดึง Chat ID ไม่สำเร็จ (token อาจไม่ถูกต้อง)"})
        updates = data.get("result", []) or []
        chat_ids = []
        for u in updates:
            msg = u.get("message") or u.get("channel_post") or u.get("edited_message") or {}
            chat = msg.get("chat") or {}
            cid = chat.get("id")
            if cid is not None:
                chat_ids.append(str(cid))
        if not chat_ids:
            return jsonify({"ok": False, "msg": "ยังไม่พบ Chat ID — ให้ส่งข้อความหา bot ก่อน (เช่น /start) แล้วกดดึงอีกครั้ง"})
        chat_id = chat_ids[-1]
        return jsonify({"ok": True, "chat_id": chat_id, "msg": f"✅ ดึง Chat ID สำเร็จ: {chat_id}"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"❌ ดึง Chat ID ไม่สำเร็จ: {e}"})


# ══════════════════════════════════════════════════════════════════════════════
#  Run
# ══════════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════════
#  Auto-Sync Scheduler (background thread, เหมือน Desktop App)
# ══════════════════════════════════════════════════════════════════════════════

def _auto_sync_loop():
    """รันใน thread แยก ซิงค์ซ้ำตาม interval ที่ตั้งไว้"""
    import time as _time
    # รอ 5 วินาทีก่อน เผื่อ Flask start ยังไม่เสร็จ
    _time.sleep(5)
    while True:
        interval = int(sync_status.get("auto_interval", 60))
        sync_status["next_sync"] = (
            _now_bkk().strftime("%H:%M:%S")
        )
        if sync_status.get("auto_enabled", True) and not sync_status["running"]:
            settings = load_settings()
            accounts = settings.get("accounts", [])
            if accounts:
                def _task(accs):
                    sync_status["running"] = True
                    total_new = total_upd = 0
                    error_count = 0
                    login_err = imap_err = False
                    for acc in accs:
                        # ── Incremental: ดึงเฉพาะ UID ใหม่ ──
                        try:
                            n, u = _sync_account_incremental(acc)
                            total_new += n
                            total_upd += u
                        except imaplib.IMAP4.error:
                            login_err = True
                            error_count += 1
                        except Exception:
                            imap_err = True
                            error_count += 1
                    sync_status["running"] = False
                    sync_status["last_sync"] = _now_bkk().strftime("%Y-%m-%d %H:%M:%S")
                    sync_status["auto_new"] += total_new
                    sync_status["auto_upd"] += total_upd
                    if total_new or total_upd:
                        slog(f"🔁 Auto: ใหม่ {total_new} | อัปเดต {total_upd}")
                    # ── Auto-delete ผู้ซื้อยกเลิกเอง ──
                    _auto_delete_buyer_cancelled()
                    # ── Telegram hook ──
                    try:
                        _tg_handle_post_sync(
                            total_scanned=total_new + total_upd,
                            total_new=total_new,
                            total_updated=total_upd,
                            error_count=error_count,
                            login_error=login_err,
                            imap_error=imap_err,
                        )
                    except Exception as tg_err:
                        slog(f"⚠️ Telegram post-sync error: {tg_err}")
                threading.Thread(target=_task, args=(accounts,), daemon=True).start()
        _time.sleep(interval)


def start_auto_sync():
    """เริ่ม background auto-sync thread (เรียกครั้งเดียวตอน startup)"""
    t = threading.Thread(target=_auto_sync_loop, daemon=True)
    t.start()


def _is_port_in_use(port):
    """เช็คว่า port ถูกใช้อยู่แล้วหรือยัง"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


@app.route("/api/shutdown", methods=["POST"])
@login_required
def shutdown_server():
    """ปิด server จากหน้าเว็บ"""
    os._exit(0)


@app.route("/api/debug/storage")
@login_required
def debug_storage():
    """ดู state ของ storage จริง — ใช้แก้ปัญหาข้อมูลหาย (ลบทิ้งหลังใช้)"""
    info = {
        "data_dir_env": os.environ.get("DATA_DIR", "<NOT SET>"),
        "data_dir_used": str(DATA_DIR),
        "db_path": str(DB_PATH),
        "settings_path": str(SETTINGS_FILE),
        "cwd": os.getcwd(),
        "base_dir": str(BASE_DIR),
        "data_dir_exists": DATA_DIR.exists(),
        "db_exists": DB_PATH.exists(),
        "db_size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
        "settings_exists": SETTINGS_FILE.exists(),
        "settings_size_bytes": SETTINGS_FILE.stat().st_size if SETTINGS_FILE.exists() else 0,
    }
    # list ไฟล์ใน DATA_DIR
    try:
        info["data_dir_contents"] = [
            {"name": p.name, "size": p.stat().st_size, "is_dir": p.is_dir()}
            for p in sorted(DATA_DIR.iterdir())
        ]
    except Exception as e:
        info["data_dir_contents_error"] = str(e)
    # list /data root โดยตรง (เผื่อ DATA_DIR ผิดที่)
    try:
        if os.path.exists("/data"):
            info["root_data_dir_exists"] = True
            info["root_data_dir_contents"] = [
                {"name": n, "size": os.path.getsize(f"/data/{n}") if os.path.isfile(f"/data/{n}") else None,
                 "is_dir": os.path.isdir(f"/data/{n}")}
                for n in sorted(os.listdir("/data"))
            ]
        else:
            info["root_data_dir_exists"] = False
    except Exception as e:
        info["root_data_dir_error"] = str(e)
    # นับ rows ใน orders.db
    try:
        with sqlite3.connect(str(DB_PATH)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM orders")
            info["orders_count"] = cur.fetchone()[0]
            cur.execute("SELECT MAX(last_update_ts) FROM orders")
            ts = cur.fetchone()[0]
            info["max_last_update_ts"] = ts
            if ts:
                info["max_last_update_human"] = _ts_to_bkk(ts).strftime("%Y-%m-%d %H:%M")
            cur.execute("SELECT MIN(first_seen_ts) FROM orders")
            ts2 = cur.fetchone()[0]
            if ts2:
                info["min_first_seen_human"] = _ts_to_bkk(ts2).strftime("%Y-%m-%d %H:%M")
    except Exception as e:
        info["orders_count_error"] = str(e)
    # ค้นหา orders.db ทุกที่ใน filesystem (เผื่ออยู่ผิดที่)
    try:
        found = []
        for root in ["/data", "/app", "/tmp", str(BASE_DIR)]:
            if not os.path.exists(root):
                continue
            for dirpath, _, filenames in os.walk(root):
                for fn in filenames:
                    if fn in ("orders.db", "orders.db-wal", "orders.db-shm", "settings.json"):
                        full = os.path.join(dirpath, fn)
                        try:
                            found.append({"path": full, "size": os.path.getsize(full)})
                        except Exception:
                            pass
                # ป้องกันลึกเกินไป
                if dirpath.count("/") > 6:
                    break
        info["all_db_files_found"] = found
    except Exception as e:
        info["search_error"] = str(e)

    return jsonify(info)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG","false").lower() == "true"
    interval = int(os.environ.get("SYNC_INTERVAL", 60))
    sync_status["auto_interval"] = interval

    is_server = os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER") or os.environ.get("DATA_DIR")

    # ── ถ้า port ถูกใช้อยู่แล้ว → เปิด browser ไปหน้าเดิม แล้วจบ ──
    if not is_server and _is_port_in_use(port):
        print(f"⚠️  Port {port} ถูกใช้งานอยู่แล้ว — เปิด browser ไปหน้าเดิม")
        webbrowser.open(f"http://localhost:{port}")
        import sys; sys.exit(0)

    _init_license()

    print(f"""
╔══════════════════════════════════════════╗
║   📦 Order Tracker Web v{CURRENT_VERSION:<16}║
║   http://localhost:{port:<5}                ║
║   Auto-sync ทุก {interval} วินาที             ║
║   📣 Telegram Bot: เปิดใช้งาน           ║
║   🔐 License: {"✅" if GLOBAL_LICENSE_VALID else "❌":<28}║
╚══════════════════════════════════════════╝
""")
    if not is_server:
        webbrowser.open(f"http://localhost:{port}")
    start_auto_sync()
    start_telegram_polling()
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
