print(">>> telegram_raw_bot התחיל לרוץ")

import os
import time
import json
import sqlite3
import re
import requests
from datetime import datetime
from copy import deepcopy

# =========================
# 1) מפתחות (ENV בלבד)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError('חסר BOT_TOKEN ב-ENV. (CMD: setx BOT_TOKEN "YOUR_TELEGRAM_BOT_TOKEN")')
if not GEMINI_API_KEY:
    raise RuntimeError('חסר GEMINI_API_KEY ב-ENV. (CMD: setx GEMINI_API_KEY "YOUR_GEMINI_KEY")')

TG_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
# =========================
# FastAPI PDF engine
# =========================
API_URL = os.getenv("API_URL", "http://127.0.0.1:8000")

def create_quote_and_get_pdf(raw_data: dict) -> tuple[bytes, int, str]:
    """
    POST /quote/pdf-from-draft — מחזיר PDF bytes ישירות.
    מחזיר: (pdf_bytes, 0, quote_number)
    """
    payload = {
        "tenant_id":       raw_data.get("tenant_id", "nimrod"),
        "client_name":     raw_data.get("client_name"),
        "client_phone":    raw_data.get("client_phone"),
        "address":         raw_data.get("address"),
        "job_type":        raw_data.get("job_type"),
        "raw_description": raw_data.get("raw_description"),
        "raw_price_lines": raw_data.get("raw_price_lines"),
        "payment_terms":   raw_data.get("payment_terms"),
        "total_price":     raw_data.get("total_price"),
        "template_id":     raw_data.get("template_id", DEFAULT_TEMPLATE),
    }
    r = requests.post(f"{API_URL}/quote/pdf-from-draft", json=payload, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"FastAPI pdf-from-draft {r.status_code}: {r.text}")

    quote_number = r.headers.get("X-Quote-Number", "")
    return r.content, 0, quote_number

# ✅ כתובת השרת של FastAPI (לוקאלי עכשיו, ענן בעתיד)

# =========================
# 2) Paths
# =========================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # שורש הפרויקט

DB_DIR = os.path.join(BASE_DIR, "db")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "bot_state.db")

OUTPUT_DIR = os.path.join(BASE_DIR, "storage", "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# =========================
# 3) Gemini SDK
# =========================
from google import genai
from google.genai import types

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

def gemini_generate(model: str, contents, config, retries: int = 3, timeout: int = 45) -> any:
    """קריאה ל-Gemini עם retry אוטומטי ו-timeout."""
    import concurrent.futures
    for attempt in range(retries):
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    gemini_client.models.generate_content,
                    model=model, contents=contents, config=config
                )
                return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            print(f"Gemini timeout after {timeout}s (attempt {attempt+1})")
            if attempt < retries - 1:
                time.sleep(5)
            else:
                raise RuntimeError("Gemini לא הגיב תוך זמן סביר. נסה שוב.")
        except Exception as e:
            err = str(e)
            if ("429" in err or "503" in err or "UNAVAILABLE" in err or "RESOURCE_EXHAUSTED" in err):
                wait = (attempt + 1) * 20
                print(f"Gemini rate limit (attempt {attempt+1}), waiting {wait}s...")
                time.sleep(wait)
            elif "deadline" in err.lower() or "timeout" in err.lower():
                time.sleep(5)
            else:
                raise
    raise RuntimeError("Gemini לא זמין כרגע, נסה שוב בעוד כמה שניות.")

# =========================
# 4) Single-instance lock
# =========================
LOCK_PATH = os.path.join(BASE_DIR, "bot.lock")

def release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass

def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

def acquire_lock():
    if os.path.exists(LOCK_PATH):
        try:
            with open(LOCK_PATH, "r", encoding="utf-8") as f:
                old_pid = int((f.read() or "0").strip() or "0")
        except Exception:
            old_pid = 0

        if old_pid and _pid_is_running(old_pid):
            raise RuntimeError("נראה שהבוט כבר רץ (bot.lock קיים). סגור מופע קודם.")
        else:
            try:
                os.remove(LOCK_PATH)
            except Exception:
                pass

    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

# =========================
# 5) State (SQLite)
# =========================
DB_PATH = os.path.join(BASE_DIR, "db", "bot_state.db")
DB_VERSION = 2

def _table_columns(con, table_name: str) -> set:
    cur = con.cursor()
    cur.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cur.fetchall()}

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER NOT NULL
        )
    """)
    cur.execute("SELECT version FROM schema_version LIMIT 1")
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO schema_version(version) VALUES (?)", (DB_VERSION,))
        con.commit()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS states (
            chat_id INTEGER PRIMARY KEY,
            stage INTEGER NOT NULL,
            data_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tenants_map (
            chat_id   INTEGER PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            name      TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    con.commit()

    cols = _table_columns(con, "states")
    if "stage" not in cols:
        cur.execute("ALTER TABLE states ADD COLUMN stage INTEGER NOT NULL DEFAULT 0")
    if "data_json" not in cols:
        cur.execute("ALTER TABLE states ADD COLUMN data_json TEXT NOT NULL DEFAULT '{}'")
    if "updated_at" not in cols:
        cur.execute("ALTER TABLE states ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0")

    con.commit()
    con.close()

def save_state(chat_id: int, stage: int, data: dict):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO states (chat_id, stage, data_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            stage=excluded.stage,
            data_json=excluded.data_json,
            updated_at=excluded.updated_at
    """, (chat_id, stage, json.dumps(data, ensure_ascii=False), int(time.time())))
    con.commit()
    con.close()

def load_state(chat_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT stage, data_json FROM states WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    stage, data_json = row
    try:
        data = json.loads(data_json)
    except Exception:
        data = {}
    return {"stage": int(stage), "data": data}

def clear_state(chat_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM states WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()

# =========================
# Tenant registration
# =========================

TENANT_TEMPLATE_PATH = os.path.join(BASE_DIR, "tenants", "_template.json")
TENANTS_DIR_BOT      = os.path.join(BASE_DIR, "tenants")

def get_tenant_id(chat_id: int) -> str | None:
    """מחזיר tenant_id קיים ל-chat_id, או None אם לא רשום."""
    con = sqlite3.connect(DB_PATH)
    row = con.execute(
        "SELECT tenant_id FROM tenants_map WHERE chat_id=?", (chat_id,)
    ).fetchone()
    con.close()
    return row[0] if row else None

def register_tenant(chat_id: int, tenant_id: str, name: str = ""):
    """רושם chat_id → tenant_id ויוצר קובץ JSON אם לא קיים."""
    # שמור ב-DB
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO tenants_map (chat_id, tenant_id, name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO NOTHING
    """, (chat_id, tenant_id, name, int(time.time())))
    con.commit()
    con.close()

    # צור קובץ JSON אם לא קיים
    tenant_path = os.path.join(TENANTS_DIR_BOT, f"{tenant_id}.json")
    if not os.path.exists(tenant_path):
        # טען template אם קיים, אחרת צור ברירת מחדל
        if os.path.exists(TENANT_TEMPLATE_PATH):
            with open(TENANT_TEMPLATE_PATH, "r", encoding="utf-8") as f:
                template_data = json.load(f)
        else:
            template_data = {}

        tenant_data = {
            "business_name":    name or f"עסק {tenant_id}",
            "business_phone":   "",
            "business_email":   "",
            "business_address": "",
            "logo_file":        "",
            "company_id":       "",
            "settings": {
                "template_id":           "classic",
                "show_line_prices":      True,
                "show_vat":              False,
                "vat_percent":           17,
                "show_email":            True,
                "show_phone":            True,
                "valid_days":            30,
                "default_payment_terms": [
                    "40% בתחילת העבודה",
                    "40% באמצע העבודה",
                    "20% בסיום העבודה",
                ],
            },
            **template_data,  # אם קיים template — מדרוס ברירות מחדל
        }
        os.makedirs(TENANTS_DIR_BOT, exist_ok=True)
        with open(tenant_path, "w", encoding="utf-8") as f:
            json.dump(tenant_data, f, ensure_ascii=False, indent=2)

def get_or_create_tenant(chat_id: int, first_name: str = "") -> str:
    """מחזיר tenant_id קיים, או יוצר חדש אם המשתמש חדש."""
    tid = get_tenant_id(chat_id)
    if tid:
        return tid
    # tenant_id = chat_id כמחרוזת
    new_tid = str(chat_id)
    register_tenant(chat_id, new_tid, name=first_name)
    return new_tid

# =========================
# Telegram helpers
# =========================
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    requests.post(f"{TG_URL}/sendMessage", data=payload, timeout=20)

def answer_callback_query(callback_query_id: str):
    try:
        requests.post(f"{TG_URL}/answerCallbackQuery", data={"callback_query_id": callback_query_id}, timeout=10)
    except Exception:
        pass

def send_document(chat_id, file_path, caption=None, reply_markup=None):
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        files = {"document": (filename, f)}
        data  = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        requests.post(f"{TG_URL}/sendDocument", data=data, files=files, timeout=120)

def send_typing(chat_id: int):
    """מציג אנימציית הקלדה בצ'אט."""
    try:
        requests.post(f"{TG_URL}/sendChatAction",
                      data={"chat_id": chat_id, "action": "upload_document"},
                      timeout=5)
    except Exception:
        pass

def send_processing_message(chat_id: int) -> int | None:
    """שולח הודעת 'מעבד...' ומחזיר את ה-message_id למחיקה אחר כך."""
    send_typing(chat_id)
    payload = {"chat_id": chat_id, "text": "⏳ מייצר הצעת מחיר..."}
    try:
        r = requests.post(f"{TG_URL}/sendMessage", data=payload, timeout=10)
        return r.json().get("result", {}).get("message_id")
    except Exception:
        return None

def delete_message(chat_id: int, message_id: int):
    """מוחק הודעה לפי message_id."""
    try:
        requests.post(f"{TG_URL}/deleteMessage",
                      data={"chat_id": chat_id, "message_id": message_id},
                      timeout=5)
    except Exception:
        pass

def send_photo(chat_id, photo_path: str, caption: str = "", reply_markup=None):
    with open(photo_path, "rb") as f:
        data = {"chat_id": chat_id, "caption": caption}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        requests.post(f"{TG_URL}/sendPhoto", data=data, files={"photo": f}, timeout=30)

def download_telegram_file_by_id(file_id: str) -> bytes:
    r = requests.get(f"{TG_URL}/getFile", params={"file_id": file_id}, timeout=20)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"getFile failed: {j}")

    file_path = j["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    resp = requests.get(file_url, timeout=30)
    resp.raise_for_status()
    return resp.content



# =========================
# Menus / buttons
# =========================
STAGE_CREATE_0 = 0
STAGE_CREATE_6 = 6
STAGE_EDIT     = 90  # מצב עריכה חופשית
STAGE_PROFILE  = 91  # wizard עדכון פרטי עסק

# ──────────────────────────────────────────────
# רשימת הטמפלייטים הזמינים
# template_file — שם קובץ ב-templates/html/
# preview_image — PNG ב-static/previews/ (נוצר ע"י generate_previews.py)
# ──────────────────────────────────────────────
TEMPLATES_DIR  = os.path.join(BASE_DIR, "templates", "html")
PREVIEWS_DIR   = os.path.join(BASE_DIR, "static", "previews")

TEMPLATES = {
    "classic": {
        "label":         "קלאסי כחול",
        "description":   "עיצוב מקצועי נקי עם לוגו וכותרת כחולה",
        "template_file": "quote_classic.html",
        "preview_image": os.path.join(PREVIEWS_DIR, "quote_classic.png"),
    },
    "green": {
        "label":         "ירוק אורגני",
        "description":   "עיצוב חם ואלגנטי עם רקע קרם ואלמנטים ירוקים",
        "template_file": "quote_green.html",
        "preview_image": os.path.join(PREVIEWS_DIR, "quote_green.png"),
    },
}
DEFAULT_TEMPLATE = "classic"

# =====================================
# Tenant settings helpers (קריאה/כתיבה)
# =====================================

def load_tenant_settings(tenant_id: str) -> dict:
    path = os.path.join(BASE_DIR, "tenants", f"{tenant_id}.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("settings") or {}

def save_tenant_setting(tenant_id: str, key: str, value) -> bool:
    path = os.path.join(BASE_DIR, "tenants", f"{tenant_id}.json")
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "settings" not in data:
        data["settings"] = {}
    data["settings"][key] = value
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True

SETTING_LABELS = {
    "show_vat":         ('מע"מ', "show_vat"),
    "show_line_prices": ("מחיר לכל שורה", "show_line_prices"),
    "show_email":       ("הצג אימייל", "show_email"),
    "show_phone":       ("הצג טלפון", "show_phone"),
}

def settings_markup(settings: dict) -> dict:
    rows = []
    for key, (label, _) in SETTING_LABELS.items():
        val   = settings.get(key, True)
        icon  = "✅" if val else "⬜"
        rows.append([{"text": f"{icon} {label}", "callback_data": f"SETTING_TOGGLE_{key}"}])
    rows.append([{"text": "🔙 חזור לתפריט", "callback_data": "BACK_MENU"}])
    return {"inline_keyboard": rows}

def show_settings(chat_id: int, tenant_id: str, note: str = ""):
    s    = load_tenant_settings(tenant_id)
    vat  = s.get("show_vat", False)
    lp   = s.get("show_line_prices", True)
    em   = s.get("show_email", True)
    ph   = s.get("show_phone", True)
    vat_pct = s.get("vat_percent", 17)

    VAT_LABEL = 'מע"מ'
    lines = [
        "⚙️ *הגדרות*",
        "",
        f"{'✅' if vat else '⬜'} {VAT_LABEL} ({vat_pct}%)",
        f"{'✅' if lp  else '⬜'} מחיר לכל שורה בטבלה",
        f"{'✅' if em  else '⬜'} הצג אימייל בהצעה",
        f"{'✅' if ph  else '⬜'} הצג טלפון בהצעה",
    ]
    if note:
        lines += ["", note]

    send_message(chat_id, "\n".join(lines), reply_markup=settings_markup(s))


# =====================================
# Profile wizard
# =====================================

PROFILE_FIELDS = [
    ("business_name",    "שם העסק"),
    ("business_phone",   "טלפון"),
    ("business_email",   "אימייל"),
    ("business_address", "כתובת / עיר"),
    ("company_id",       "ח.פ / ע.מ (אפשר לדלג עם /)"),
]

def load_tenant_data(tenant_id: str) -> dict:
    path = os.path.join(BASE_DIR, "tenants", f"{tenant_id}.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def save_tenant_field(tenant_id: str, field: str, value: str) -> bool:
    path = os.path.join(BASE_DIR, "tenants", f"{tenant_id}.json")
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data[field] = value
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return True

def save_logo_for_tenant(tenant_id: str, image_bytes: bytes, ext: str = "jpg") -> str:
    logos_dir = os.path.join(BASE_DIR, "static")
    os.makedirs(logos_dir, exist_ok=True)
    filename  = f"logo_{tenant_id}.{ext}"
    full_path = os.path.join(logos_dir, filename)
    with open(full_path, "wb") as f:
        f.write(image_bytes)
    save_tenant_field(tenant_id, "logo_file", filename)
    return filename

def profile_summary(data: dict) -> str:
    name  = data.get("business_name",  "") or "---"
    phone = data.get("business_phone", "") or "---"
    email = data.get("business_email", "") or "---"
    addr  = data.get("business_address","") or "---"
    cid   = data.get("company_id",     "") or "---"
    logo  = "v" if data.get("logo_file") else "x אין"
    return (
        "פרטי העסק\n\n"
        f"שם: {name}\n"
        f"טלפון: {phone}\n"
        f"אימייל: {email}\n"
        f"כתובת: {addr}\n"
        f"ח.פ: {cid}\n"
        f"לוגו: {logo}"
    )

def profile_menu_markup() -> dict:
    return {"inline_keyboard": [
        [{"text": "עדכן פרטים", "callback_data": "PROFILE_EDIT"}],
        [{"text": "העלה לוגו",  "callback_data": "PROFILE_LOGO"}],
        [{"text": "חזור",       "callback_data": "BACK_MENU"}],
    ]}

def show_profile(chat_id: int, tid: str, note: str = ""):
    data = load_tenant_data(tid)
    text = profile_summary(data)
    if note:
        text += "\n\n" + note
    send_message(chat_id, text, reply_markup=profile_menu_markup())

def start_profile_wizard(chat_id: int, tid: str):
    save_state(chat_id, STAGE_PROFILE, {"tid": tid, "field_idx": 0, "mode": "edit"})
    field_key, field_label = PROFILE_FIELDS[0]
    data = load_tenant_data(tid)
    cur  = data.get(field_key, "") or ""
    total = len(PROFILE_FIELDS)
    send_message(chat_id,
        f"עדכון פרטי עסק\n\n"
        f"שדה 1/{total}: {field_label}\n"
        f"ערך נוכחי: {cur or '(ריק)'}\n\n"
        "כתוב ערך חדש, או שלח / לדלג:")

def start_logo_wizard(chat_id: int, tid: str):
    save_state(chat_id, STAGE_PROFILE, {"tid": tid, "field_idx": -1, "mode": "logo"})
    send_message(chat_id,
        "העלאת לוגו\n\n"
        "שלח תמונה (JPG/PNG) של הלוגו שלך.\n"
        "הלוגו יופיע בהצעות המחיר.")

def handle_profile_text(chat_id: int, text: str, profile_state: dict):
    tid       = profile_state.get("tid", str(chat_id))
    mode      = profile_state.get("mode", "edit")
    field_idx = profile_state.get("field_idx", 0)

    if mode == "logo":
        send_message(chat_id, "אנא שלח תמונה (לא טקסט).")
        return

    field_key, field_label = PROFILE_FIELDS[field_idx]
    value = text.strip()
    if value != "/":
        save_tenant_field(tid, field_key, value)

    next_idx = field_idx + 1
    if next_idx < len(PROFILE_FIELDS):
        next_key, next_label = PROFILE_FIELDS[next_idx]
        data = load_tenant_data(tid)
        cur  = data.get(next_key, "") or ""
        total = len(PROFILE_FIELDS)
        save_state(chat_id, STAGE_PROFILE, {"tid": tid, "field_idx": next_idx, "mode": "edit"})
        send_message(chat_id,
            f"שדה {next_idx+1}/{total}: {next_label}\n"
            f"ערך נוכחי: {cur or '(ריק)'}\n\n"
            "כתוב ערך חדש, או שלח / לדלג:")
    else:
        clear_state(chat_id)
        show_profile(chat_id, tid, note="הפרטים עודכנו בהצלחה!")

def handle_profile_photo(chat_id: int, image_bytes: bytes, mime: str, profile_state: dict):
    tid = profile_state.get("tid", str(chat_id))
    ext = "png" if "png" in mime else "jpg"
    save_logo_for_tenant(tid, image_bytes, ext)
    clear_state(chat_id)
    show_profile(chat_id, tid, note="הלוגו עודכן בהצלחה!")


def main_menu_markup():
    return {
        "inline_keyboard": [
            [{"text": "📄 הצעה חדשה",     "callback_data": "START_QUOTE"}],
            [{"text": "📋 ההצעות שלי",    "callback_data": "MY_QUOTES"}],
            [{"text": "⚙️ הגדרות",        "callback_data": "OPEN_SETTINGS_MENU"}],
        ]
    }

def settings_menu_markup():
    return {
        "inline_keyboard": [
            [{"text": "🏢 פרטי עסק",      "callback_data": "OPEN_PROFILE"}],
            [{"text": "🎨 בחר עיצוב",     "callback_data": "CHOOSE_TEMPLATE"}],
            [{"text": "⚙️ הגדרות תצוגה", "callback_data": "OPEN_SETTINGS"}],
            [{"text": "🔙 חזור",          "callback_data": "BACK_MENU"}],
        ]
    }

def show_template_picker(chat_id: int):
    """שולח תמונת preview לכל טמפלייט עם כפתור בחירה"""
    send_message(chat_id, "🎨 בחר עיצוב להצעת המחיר\n(הטמפלייט יישמר לכל ההצעות הבאות שלך):")
    for tmpl_id, tmpl in TEMPLATES.items():
        markup = {"inline_keyboard": [[
            {"text": f"✅ בחר — {tmpl['label']}", "callback_data": f"TEMPLATE_{tmpl_id}"}
        ]]}
        preview_path = tmpl["preview_image"]
        caption = f"*{tmpl['label']}*\n{tmpl['description']}"
        if os.path.exists(preview_path):
            send_photo(chat_id, preview_path, caption=caption, reply_markup=markup)
        else:
            send_message(chat_id, f"📄 {caption}", reply_markup=markup)

def show_menu(chat_id: int, text: str = "בחר פעולה:"):
    send_message(chat_id, text, reply_markup=main_menu_markup())

def show_settings_menu(chat_id: int):
    send_message(chat_id, "⚙️ הגדרות — בחר:", reply_markup=settings_menu_markup())

def preview_markup():
    return {
        "inline_keyboard": [
            [{"text": "✅ אשר והפק מסמך", "callback_data": "CONFIRM_GENERATE"}],
            [{"text": "✏️ עוד שינוי (עריכה)", "callback_data": "EDIT_MODE"}],
            [{"text": "↩️ בטל שינוי אחרון", "callback_data": "UNDO"}],
            [{"text": "🧹 איפוס", "callback_data": "RESET"}],
        ]
    }

# =========================
# Helpers: state with flow
# =========================
def set_draft_in_state(chat_id: int, stage: int, draft: dict, prev_draft=None, flow: str = "manual", template_id: str = None):
    # שמור template_id קיים אם לא הועבר חדש
    if template_id is None:
        existing = load_state(chat_id)
        existing_data = (existing.get("data") or {}) if existing else {}
        template_id = existing_data.get("template_id") or DEFAULT_TEMPLATE
    data = {"draft": draft, "prev_draft": prev_draft, "flow": flow, "template_id": template_id}
    save_state(chat_id, stage, data)

def get_draft_from_state(state):
    data = state.get("data") or {}
    return (
        data.get("draft") or {},
        data.get("prev_draft"),
        data.get("flow") or "manual",
        data.get("template_id") or DEFAULT_TEMPLATE,
    )

# =========================
# AI step 1: תעתוק מלא מהתמונה
# =========================
def transcribe_full_text(image_bytes: bytes) -> str:
    prompt = """
תעתיק במדויק את כל הטקסט שמופיע בתמונה (עברית).
חוקים:
- מילה במילה
- אל תסכם
- אל תסדר
- אל תתקן שגיאות
- שמור שורות ככל האפשר
החזר טקסט בלבד.
"""
    contents = [
        types.Content(
            role="user",
            parts=[
                types.Part(text=prompt),
                types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=image_bytes)),
            ],
        )
    ]
    resp = gemini_generate("gemini-2.5-flash", contents, types.GenerateContentConfig(temperature=0.0))
    return (resp.text or "").strip()

# =========================
# AI step 2: חילוץ שדות מהטקסט -> JSON
# =========================
def extract_fields_from_text(full_text: str) -> dict:
    system_msg = (
        "אתה מקבל טקסט גולמי בעברית של הצעת מחיר ומחזיר JSON בלבד לפי הסכמה. "
        "אל תמציא מידע. אם חסר שדה — החזר מחרוזת ריקה."
    )

    schema = {
        "type": "OBJECT",
        "required": [
            "client_name", "client_phone", "address", "job_type",
            "raw_description", "raw_price_lines",
            "payment_terms", "total_price"
        ],
        "properties": {
            "client_name":     {"type": "STRING"},
            "client_phone":    {"type": "STRING"},
            "address":         {"type": "STRING"},
            "job_type":        {"type": "STRING"},
            "raw_description": {"type": "STRING"},
            "raw_price_lines": {"type": "ARRAY", "items": {"type": "STRING"}},
            "payment_terms":   {"type": "STRING"},
            "total_price":     {"type": "STRING"},
        }
    }

    prompt = f"""
אתה מחלץ שדות מהצעת מחיר בעברית. קרא בעיון ומלא כל שדה בדיוק.

שדות לחילוץ:
- client_name      — שם הלקוח בלבד (לא כתובת, לא טלפון)
- client_phone     — טלפון הלקוח (אם מופיע)
- address          — כתובת העבודה / עיר (אם לא מופיע — החזר "")
- job_type         — סוג העבודה הכללי (לדוגמה: "שיפוץ כללי", "צבע", "אינסטלציה")
- raw_description  — תיאור כללי קצר של העבודה במשפט אחד עד שניים. לא רשימת סעיפים!
- raw_price_lines  — רשימת כל סעיפי העבודה עם מחיריהם. כל סעיף בפורמט: "תיאור הסעיף - מחיר" (מחיר ספרות בלבד, ללא ₪). אם סעיף ללא מחיר — אל תכלול אותו.
- payment_terms    — תנאי תשלום בלבד (מתי ואיך משלמים, לדוגמה: "50% בהתחלה, 50% בסיום"). אם מופיעה הערה שאינה תנאי תשלום — החזר "".
- total_price      — הסכום הכולל, ספרות בלבד ללא ₪ וללא פסיקים

חוקים קריטיים:
- raw_description: תיאור כללי בלבד — לא רשימה, לא סעיפים
- raw_price_lines: כל סעיף שיש לו מחיר בטקסט — גם אם לא כתוב במפורש כ"סעיף"
- payment_terms: רק אם יש תנאי תשלום אמיתיים (אחוזים, מועדים). הערות כמו "פינוי פסולת ע"י הקבלן" אינן תנאי תשלום
- אל תמציא מידע שלא מופיע בטקסט
- אם חסר שדה — החזר "" או []

הטקסט:
<<<
{full_text}
>>>
"""

    config = types.GenerateContentConfig(
        system_instruction=system_msg,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0.1,
    )
    resp = gemini_generate("gemini-2.5-flash", prompt, config)

    data = resp.parsed or {}
    data["total_price"]  = (data.get("total_price")  or "").replace("₪", "").replace(",", "").strip()
    data["client_phone"] = (data.get("client_phone") or "").strip()
    return data

# =========================
# NEW: AI step 3 - הצעת פעולות עריכה (Actions)
# =========================
def propose_edit_actions(draft: dict, user_msg: str) -> dict:
    system_msg = (
        "אתה עוזר לערוך טיוטת הצעת מחיר בעברית. "
        "אתה מקבל טיוטה במבנה JSON והודעת משתמש. "
        "תחזיר אך ורק JSON של פעולות עריכה לפי הסכמה. "
        "אסור להמציא מחירים או סעיפים שלא התבקשו. "
        "אם הבקשה לא חד משמעית — החזר פעולה ask_clarifying_question."
    )

    schema = {
        "type": "OBJECT",
        "required": ["actions", "notes_to_user"],
        "properties": {
            "actions": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "required": ["type"],
                    "properties": {
                        "type": {"type": "STRING"},
                        "amount": {"type": "NUMBER"},
                        "text": {"type": "STRING"},
                        "match": {"type": "STRING"},
                        "field": {"type": "STRING"},
                        "question": {"type": "STRING"},
                    },
                },
            },
            "notes_to_user": {"type": "STRING"},
        },
    }

    prompt = f"""
זו הטיוטה הנוכחית (JSON):
{json.dumps(draft, ensure_ascii=False)}

המשתמש כתב:
{user_msg}

החזר פעולות עריכה JSON בלבד.

הפעולות המותרות:
- set_total (amount)
- increase_total_by (amount)
- set_field_text (field, text)   שדות: client_name, client_phone, address, job_type, raw_description, payment_terms
- add_line_item (text)           text חייב להיות בפורמט: "תיאור הסעיף - מחיר" (מחיר ספרות בלבד)
- remove_line_item (match)       match = טקסט לחיפוש בסעיף
- rewrite_description (text)
- rewrite_payment_terms (text)
- ask_clarifying_question (question)
- no_op

חוקים:
- אל תשנה מחירים/סה"כ אם לא התבקש.
- אל תמחוק/תוסיף סעיפים אם לא התבקש.
- אם המשתמש אמר "תוסיף 15000" בלי לציין למה ואין הקשר של סעיף — תפרש כברירת מחדל: increase_total_by.
- אם הבוט שאל "כמה עולה [סעיף]?" והמשתמש ענה במספר בלבד — זה המחיר של הסעיף. השתמש ב-add_line_item עם הסעיף שנשאל עליו. אל תשתמש ב-increase_total_by.
- אם המשתמש כתב תיאור סעיף ואחריו מספר (כגון: "אינסטלציה חדשה 3500") — זה add_line_item, לא increase_total_by.
- אם המשתמש אמר "תוריד סעיף X" וה-match לא ברור — תשאל.
- אם המשתמש ביקש להוסיף סעיף ונתן שם וגם מחיר — השתמש ב-add_line_item עם text בפורמט "תיאור - מחיר". אל תשאל שאלות מיותרות.
- אם המשתמש ביקש להוסיף סעיף ונתן שם בלבד ללא מחיר — בקש רק את המחיר בשאלה קצרה: "כמה עולה [שם הסעיף]?"
- אם המשתמש ענה במספר בלבד אחרי שאלה על מחיר — זה המחיר של הסעיף האחרון שנשאל עליו.
"""

    config = types.GenerateContentConfig(
        system_instruction=system_msg,
        response_mime_type="application/json",
        response_schema=schema,
        temperature=0.1,
    )
    resp = gemini_generate("gemini-2.5-flash", prompt, config)

    return resp.parsed or {
        "actions": [{"type": "ask_clarifying_question", "question": "לא הבנתי מה לשנות. מה בדיוק תרצה לערוך?"}],
        "notes_to_user": ""
    }

# =========================
# Validation + filenames
# =========================
def validate_quote(data: dict):
    errors = []

    def _is_blank(x): return not str(x or "").strip()

    if _is_blank(data.get("client_name")):
        errors.append("חסר שם לקוח.")
    if _is_blank(data.get("address")):
        errors.append("חסרה כתובת עבודה/עיר.")
    if _is_blank(data.get("job_type")):
        errors.append("חסר סוג עבודה.")
    if _is_blank(data.get("raw_description")):
        errors.append("חסר תיאור קצר.")

    lines = data.get("raw_price_lines") or []
    if not isinstance(lines, list) or len([x for x in lines if str(x).strip()]) == 0:
        errors.append("חסרים סעיפי עבודה (כל סעיף בשורה).")

    total = str(data.get("total_price") or "").strip().replace(",", "").replace("₪", "")
    if not total.isdigit():
        errors.append('המחיר הכולל חייב להיות מספר בלבד (בלי ₪, בלי פסיקים).')

    return (len(errors) == 0, errors)

def safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = "".join(ch for ch in s if ch.isalnum() or ch in (" ", "-", "_"))
    s = s.replace(" ", "_")
    return s[:40] if s else "לקוח"

# =========================
# Preview builder
# =========================
def build_preview(d: dict) -> str:
    lines = d.get("raw_price_lines") or []
    lines_clean = [str(x).strip() for x in lines if str(x).strip()]
    bullets = "\n".join([f"• {x}" for x in lines_clean]) if lines_clean else "—"

    txt = (
        "🧾 טיוטת הצעת מחיר\n\n"
        f"לקוח: {d.get('client_name','')}\n"
        f"כתובת: {d.get('address','')}\n"
        f"סוג עבודה: {d.get('job_type','')}\n\n"
        f"תיאור: {d.get('raw_description','')}\n\n"
        f"סעיפים:\n{bullets}\n\n"
        f"תנאי תשלום: {d.get('payment_terms','')}\n\n"
        f"סה\"כ: {d.get('total_price','')} ₪\n"
    )
    return txt

# =========================
# Apply actions safely
# =========================
_ALLOWED_FIELDS = {"client_name", "client_phone", "address", "job_type", "raw_description", "payment_terms"}

FIELD_LABELS = {
    "client_name":     "שם לקוח",
    "client_phone":    "טלפון לקוח",
    "address":         "כתובת",
    "job_type":        "סוג עבודה",
    "raw_description": "תיאור",
    "payment_terms":   "תנאי תשלום",
    "total_price":     'סה"כ',
}

def _to_int_amount(x):
    try:
        if isinstance(x, (int, float)):
            return int(round(x))
        s = str(x).replace(",", "").replace("₪", "").strip()
        if re.fullmatch(r"-?\d+", s):
            return int(s)
    except Exception:
        pass
    return None

def apply_actions(draft: dict, actions_payload: dict):
    new_draft = deepcopy(draft)
    actions = actions_payload.get("actions") or []
    notes = (actions_payload.get("notes_to_user") or "").strip()

    for a in actions:
        if (a.get("type") or "") == "ask_clarifying_question":
            q = (a.get("question") or "").strip() or "לא הבנתי. מה בדיוק לשנות?"
            return draft, q, notes

    for a in actions:
        t = (a.get("type") or "").strip()

        if t == "no_op":
            continue

        if t == "set_total":
            amt = _to_int_amount(a.get("amount"))
            if amt is not None and amt >= 0:
                new_draft["total_price"] = str(amt)
            continue

        if t == "increase_total_by":
            inc = _to_int_amount(a.get("amount"))
            cur = _to_int_amount(new_draft.get("total_price"))
            if inc is not None and cur is not None:
                new_draft["total_price"] = str(max(0, cur + inc))
            continue

        if t == "set_field_text":
            field = (a.get("field") or "").strip()
            text = (a.get("text") or "").strip()
            print(f">>> set_field_text: field={field!r} text={text!r}")
            if field in _ALLOWED_FIELDS:
                new_draft[field] = text
            continue

        if t == "rewrite_description":
            text = (a.get("text") or "").strip()
            if text:
                new_draft["raw_description"] = text
            continue

        if t == "rewrite_payment_terms":
            text = (a.get("text") or "").strip()
            if text:
                new_draft["payment_terms"] = text
            continue

        if t == "add_line_item":
            text = (a.get("text") or "").strip()
            if text:
                new_draft.setdefault("raw_price_lines", [])
                if isinstance(new_draft["raw_price_lines"], list):
                    new_draft["raw_price_lines"].append(text)
            continue

        if t == "remove_line_item":
            match = (a.get("match") or "").strip()
            if match:
                lines = new_draft.get("raw_price_lines") or []
                if isinstance(lines, list):
                    m = match.lower()
                    new_lines = [ln for ln in lines if m not in str(ln).lower()]
                    new_draft["raw_price_lines"] = new_lines
            continue

    return new_draft, None, notes

# =========================
# PDF generate (✅ via FastAPI)
# =========================
def generate_pdf(chat_id: int, raw_data: dict):
    ok, errors = validate_quote(raw_data)
    if not ok:
        show_menu(chat_id, "❌ אי אפשר להפיק עדיין:\n- " + "\n- ".join(errors))
        return

    stamp       = datetime.now().strftime("%Y-%m-%d_%H%M")
    client_part = safe_filename(raw_data.get("client_name", ""))
    out_name    = f"quote_{stamp}_{client_part}.pdf"
    pdf_path    = os.path.join(OUTPUT_DIR, out_name)

    # הודעת המתנה
    processing_msg_id = send_processing_message(chat_id)

    try:
        pdf_bytes, quote_id, quote_number = create_quote_and_get_pdf(raw_data)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        # מחק הודעת המתנה
        if processing_msg_id:
            delete_message(chat_id, processing_msg_id)

        # הכן פרטי סיכום
        client  = raw_data.get("client_name", "") or "לא צוין"
        total   = raw_data.get("total_price", "") or ""
        job     = raw_data.get("job_type", "") or ""
        caption = (
            f"✅ הצעת מחיר #{quote_number} מוכנה!\n\n"
            f"👤 לקוח: {client}\n"
            + (f"🔨 עבודה: {job}\n" if job else "")
            + (f"💰 סה\"כ: {total} ₪\n" if total else "")
        )

        # כפתור שיתוף ללקוח
        share_markup = {
            "inline_keyboard": [[
                {"text": "📤 שתף ללקוח", "switch_inline_query": f"הצעת מחיר #{quote_number}"},
            ], [
                {"text": "📋 כל ההצעות שלי", "callback_data": "MY_QUOTES"},
                {"text": "📄 הצעה חדשה",     "callback_data": "START_QUOTE"},
            ]]
        }

        send_document(chat_id, pdf_path, caption=caption, reply_markup=share_markup)

    except Exception as e:
        if processing_msg_id:
            delete_message(chat_id, processing_msg_id)
        show_menu(chat_id, f"❌ שגיאה ביצירת PDF דרך השרת: {e}")

# =========================
# Flow helpers
# =========================
def start_quote(chat_id: int):
    clear_state(chat_id)
    send_message(chat_id,
        "📄 הצעה חדשה — איך תרצה להתחיל?",
        reply_markup={"inline_keyboard": [
            [{"text": "✍️ מלא ידנית",       "callback_data": "QUOTE_MANUAL"}],
            [{"text": "📷 שלח תמונה/כתב יד", "callback_data": "QUOTE_PHOTO"}],
            [{"text": "🔙 חזור",              "callback_data": "BACK_MENU"}],
        ]}
    )

def start_quote_manual(chat_id: int):
    tid = get_or_create_tenant(chat_id)
    set_draft_in_state(chat_id, STAGE_CREATE_0, {"tenant_id": tid}, prev_draft=None, flow="manual")
    send_message(chat_id, "שם הלקוח:")

def send_preview(chat_id: int, draft: dict, extra_note: str = "", keep_prev=None):
    text = build_preview(draft)
    if extra_note:
        text += "\n\n" + extra_note
    send_message(chat_id, text, reply_markup=preview_markup())
    # אחרי Preview תמיד נעבור למצב EDIT
    set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft=keep_prev, flow="manual")

# =========================
# Prefill from image: ask missing fields (flow=prefill)
# =========================
def continue_quote_from_prefill(chat_id: int, draft: dict):
    # ודא tenant_id תמיד קיים בדראפט
    if not draft.get("tenant_id"):
        draft["tenant_id"] = get_or_create_tenant(chat_id)

    if not str(draft.get("client_name", "")).strip():
        set_draft_in_state(chat_id, 0, draft, flow="prefill")
        send_message(chat_id, "חסר שם לקוח. כתוב שם הלקוח:")
        return
    if not str(draft.get("address", "")).strip():
        set_draft_in_state(chat_id, 1, draft, flow="prefill")
        send_message(chat_id, "חסרה כתובת עבודה/עיר. כתוב כתובת:")
        return
    if not str(draft.get("job_type", "")).strip():
        set_draft_in_state(chat_id, 2, draft, flow="prefill")
        send_message(chat_id, "חסר סוג עבודה. כתוב סוג עבודה:")
        return
    if not str(draft.get("raw_description", "")).strip():
        set_draft_in_state(chat_id, 3, draft, flow="prefill")
        send_message(chat_id, "חסר תיאור קצר. כתוב תיאור קצר:")
        return

    lines = draft.get("raw_price_lines") or []
    if not isinstance(lines, list) or len([x for x in lines if str(x).strip()]) == 0:
        set_draft_in_state(chat_id, 4, draft, flow="prefill")
        send_message(chat_id, "חסרים סעיפי עבודה. כתוב כל סעיף בשורה נפרדת:")
        return

    if not str(draft.get("payment_terms", "")).strip():
        set_draft_in_state(chat_id, 5, draft, flow="prefill")
        send_message(chat_id, 'חסרים תנאי תשלום/הערות. כתוב תנאים (למשל: לא כולל מע"מ):')
        return

    total = str(draft.get("total_price") or "").strip().replace(",", "").replace("₪", "")
    if not total.isdigit():
        set_draft_in_state(chat_id, 6, draft, flow="prefill")
        send_message(chat_id, 'חסר מחיר כולל תקין. כתוב סה"כ (רק מספר, בלי ₪):')
        return

    send_preview(chat_id, draft, keep_prev=None)

# =========================
# Handle text messages
# =========================
def handle_text_message(chat_id: int, text: str):
    text = (text or "").strip()

    if text in ("/start", "/quote"):
        clear_state(chat_id)
        tid = get_or_create_tenant(chat_id)
        is_new = (tid == str(chat_id) and
                  not load_state(chat_id))
        if is_new:
            send_message(chat_id,
                "👋 ברוך הבא!\n\n"
                "אני בוט ליצירת הצעות מחיר מקצועיות ב-PDF.\n\n"
                "לפני שמתחילים — בוא נמלא את פרטי העסק שלך.",
                reply_markup={"inline_keyboard": [
                    [{"text": "🏢 מלא פרטי עסק", "callback_data": "OPEN_PROFILE"}],
                    [{"text": "דלג, אעשה זאת מאוחר יותר", "callback_data": "BACK_MENU"}],
                ]}
            )
        else:
            show_menu(chat_id, "שלום! מה נעשה היום?")
        return

    if text == "/reset":
        clear_state(chat_id)
        show_menu(chat_id, "אופס 🔄 איפסתי. בחר פעולה:")
        return

    if text == "/settings":
        tid = get_or_create_tenant(chat_id)
        show_settings(chat_id, tid)
        return

    state = load_state(chat_id)
    if not state:
        show_menu(chat_id, "בחר פעולה:")
        return

    stage = state["stage"]

    # wizard פרטי עסק
    if stage == STAGE_PROFILE:
        handle_profile_text(chat_id, text, state.get("data") or {})
        return

    draft, prev_draft, flow, template_id = get_draft_from_state(state)

    # ===== מצב EDIT =====
    if stage == STAGE_EDIT:
        if not draft:
            show_menu(chat_id, "אין טיוטה פעילה. לחץ 🧾 כדי להתחיל.")
            return

        # בדוק אם יש סעיף ממתין למחיר
        state_data   = state.get("data") or {}
        pending_item = state_data.get("pending_item")
        pending_field = state_data.get("pending_field")

        # אם יש שדה ממתין (למשל שם לקוח) — שמור את הטקסט ישירות
        if pending_field and text.strip():
            if pending_field in _ALLOWED_FIELDS:
                draft[pending_field] = text.strip()
            state_data.pop("pending_field", None)
            set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft, flow="manual")
            label = FIELD_LABELS.get(pending_field, pending_field)
            send_preview(chat_id, draft,
                         extra_note=f"📝 עודכן: {label} → {text.strip()}",
                         keep_prev=prev_draft)
            return

        # אם יש סעיף ממתין והמשתמש שלח מספר — זה המחיר שלו
        if pending_item and text.replace(",", "").replace("₪", "").strip().isdigit():
            price = text.replace(",", "").replace("₪", "").strip()
            line  = f"{pending_item} - {price}"
            draft.setdefault("raw_price_lines", [])
            if isinstance(draft["raw_price_lines"], list):
                draft["raw_price_lines"].append(line)
            # נקה pending_item ועדכן state
            state_data.pop("pending_item", None)
            set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft, flow="manual")
            send_preview(chat_id, draft,
                         extra_note=f"📝 הוספתי: {line}",
                         keep_prev=prev_draft)
            return

        processing_msg_id = send_processing_message(chat_id)
        # עדכן הודעת המתנה לטקסט עריכה
        try:
            requests.post(f"{TG_URL}/editMessageText", data={
                "chat_id": chat_id,
                "message_id": processing_msg_id,
                "text": "🧠 מבצע עריכה על הטיוטה…"
            }, timeout=5)
        except Exception:
            pass
        print(f">>> [EDIT] chat={chat_id} מתחיל propose_edit_actions")
        try:
            actions_payload = propose_edit_actions(draft, text)
            print(f">>> [EDIT] chat={chat_id} קיבל תשובה מGemini")
            new_draft, clarifying_q, notes = apply_actions(draft, actions_payload)
            print(f">>> [EDIT] chat={chat_id} apply_actions הסתיים")

            if processing_msg_id:
                delete_message(chat_id, processing_msg_id)

            if clarifying_q:
                import re as _re
                # שאלה על מחיר סעיף
                m = _re.search(r"כמה עולה (.+?)\?", clarifying_q)
                if m:
                    state_data["pending_item"] = m.group(1).strip()
                    save_state(chat_id, STAGE_EDIT, {**state_data, "draft": draft, "prev_draft": prev_draft, "flow": "manual", "template_id": template_id})
                # שאלה על טלפון לקוח
                elif any(w in clarifying_q for w in ["טלפון", "מספר טלפון"]):
                    state_data["pending_field"] = "client_phone"
                    save_state(chat_id, STAGE_EDIT, {**state_data, "draft": draft, "prev_draft": prev_draft, "flow": "manual", "template_id": template_id})
                # שאלה על שם לקוח
                elif any(w in clarifying_q for w in ["שם הלקוח", "השם החדש", "מה השם"]):
                    state_data["pending_field"] = "client_name"
                    save_state(chat_id, STAGE_EDIT, {**state_data, "draft": draft, "prev_draft": prev_draft, "flow": "manual", "template_id": template_id})
                # שאלה על כתובת
                elif any(w in clarifying_q for w in ["כתובת", "עיר"]):
                    state_data["pending_field"] = "address"
                    save_state(chat_id, STAGE_EDIT, {**state_data, "draft": draft, "prev_draft": prev_draft, "flow": "manual", "template_id": template_id})
                # שאלה על סוג עבודה
                elif any(w in clarifying_q for w in ["סוג עבודה", "סוג ה"]):
                    state_data["pending_field"] = "job_type"
                    save_state(chat_id, STAGE_EDIT, {**state_data, "draft": draft, "prev_draft": prev_draft, "flow": "manual", "template_id": template_id})
                else:
                    set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft, flow="manual")
                send_message(chat_id, "❓ " + clarifying_q)
                return

            # שמור undo
            set_draft_in_state(chat_id, STAGE_EDIT, new_draft, prev_draft=draft, flow="manual")
            extra = ("📝 " + notes) if notes else ""
            send_preview(chat_id, new_draft, extra_note=extra, keep_prev=draft)
            return

        except Exception as e:
            if processing_msg_id:
                delete_message(chat_id, processing_msg_id)
            send_message(chat_id, f"❌ לא הצלחתי לערוך: {e}")
            set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft, flow="manual")
            return

    # ===== FIX: אם זה prefill, כל תשובה משלימה שדה ואז שוב בודקים מה חסר =====
    if flow == "prefill" and stage in (0, 1, 2, 3, 4, 5, 6):
        if stage == 0:
            draft["client_name"] = text
        elif stage == 1:
            draft["address"] = text
        elif stage == 2:
            draft["job_type"] = text
        elif stage == 3:
            draft["raw_description"] = text
        elif stage == 4:
            draft["raw_price_lines"] = [ln.strip() for ln in text.split("\n") if ln.strip()]
        elif stage == 5:
            draft["payment_terms"] = text
        elif stage == 6:
            draft["total_price"] = text.replace("₪", "").replace(",", "").strip()

        continue_quote_from_prefill(chat_id, draft)
        return

    # ===== זרימת יצירה ידנית =====
    if stage == 0:
        draft["client_name"] = text
        set_draft_in_state(chat_id, 1, draft, prev_draft, flow="manual")
        send_message(chat_id, "📍 כתובת העבודה / עיר:")
        return

    if stage == 1:
        draft["address"] = text
        set_draft_in_state(chat_id, 2, draft, prev_draft, flow="manual")
        send_message(chat_id, "סוג העבודה (למשל: שיפוץ כללי / צבע / אינסטלציה):")
        return

    if stage == 2:
        draft["job_type"] = text
        set_draft_in_state(chat_id, 3, draft, prev_draft, flow="manual")
        send_message(chat_id, "תיאור קצר של העבודה:")
        return

    if stage == 3:
        draft["raw_description"] = text
        set_draft_in_state(chat_id, 4, draft, prev_draft, flow="manual")
        send_message(chat_id, "כתוב כל סעיף עבודה בשורה נפרדת (אפשר גם עם מחירים).")
        return

    if stage == 4:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        draft["raw_price_lines"] = lines
        set_draft_in_state(chat_id, 5, draft, prev_draft, flow="manual")
        send_message(chat_id, 'תנאי תשלום / הערות (למשל: לא כולל מע"מ):')
        return

    if stage == 5:
        draft["payment_terms"] = text
        set_draft_in_state(chat_id, 6, draft, prev_draft, flow="manual")
        send_message(chat_id, 'מהו המחיר הכולל? (רק מספר, בלי ₪):')
        return

    if stage == 6:
        draft["total_price"] = text.replace("₪", "").replace(",", "").strip()
        send_preview(chat_id, draft, keep_prev=None)
        return

    show_menu(chat_id, "בחר פעולה:")

# =========================
# My Quotes
# =========================
def show_my_quotes(chat_id: int, tenant_id: str):
    try:
        r = requests.get(f"{API_URL}/quotes/tenant/{tenant_id}?limit=5", timeout=10)
        if r.status_code != 200:
            send_message(chat_id, "לא הצלחתי לטעון את ההצעות.")
            return
        data   = r.json()
        quotes = data.get("quotes", [])
        if not quotes:
            show_menu(chat_id, "עדיין אין הצעות שמורות.\nלחץ 📄 הצעה חדשה כדי להתחיל!")
            return
        lines = ["📋 *ההצעות האחרונות שלך:*\n"]
        buttons = []
        for q in quotes:
            date_str = (q.get("created_at") or "")[:10]
            client   = q.get("client_name") or "ללא שם"
            total    = q.get("total", 0)
            qnum     = q.get("quote_number", q["id"])
            lines.append(f"#{qnum} | {client} | {total:,.0f} ₪ | {date_str}")
            buttons.append([{"text": f"📄 #{qnum} — {client}", "callback_data": f"RESEND_QUOTE_{q['id']}"}])
        buttons.append([{"text": "📄 הצעה חדשה", "callback_data": "START_QUOTE"}])
        buttons.append([{"text": "🔙 חזור",      "callback_data": "BACK_MENU"}])
        send_message(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": buttons})
    except Exception as e:
        show_menu(chat_id, f"שגיאה בטעינת ההצעות: {e}")

# =========================
# Callbacks
# =========================
def handle_callback(chat_id: int, callback_query_id: str, data: str):
    answer_callback_query(callback_query_id)

    # ── בחירת טמפלייט ──
    if data == "OPEN_SETTINGS_MENU":
        show_settings_menu(chat_id)
        return

    if data == "OPEN_PROFILE":
        tid = get_or_create_tenant(chat_id)
        show_profile(chat_id, tid)
        return

    if data == "PROFILE_EDIT":
        tid = get_or_create_tenant(chat_id)
        start_profile_wizard(chat_id, tid)
        return

    if data == "PROFILE_LOGO":
        tid = get_or_create_tenant(chat_id)
        start_logo_wizard(chat_id, tid)
        return

    if data == "OPEN_SETTINGS":
        tid = get_or_create_tenant(chat_id)
        show_settings(chat_id, tid)
        return

    if data.startswith("SETTING_TOGGLE_"):
        key = data[len("SETTING_TOGGLE_"):]
        tid = get_or_create_tenant(chat_id)
        s   = load_tenant_settings(tid)
        cur   = s.get(key, True)
        save_tenant_setting(tid, key, not cur)
        label = SETTING_LABELS.get(key, (key,))[0]
        note  = f"{'✅ הופעל' if not cur else '⬜ כובה'}: {label}"
        show_settings(chat_id, tid, note=note)
        return

    if data == "BACK_MENU":
        show_menu(chat_id, "בחר פעולה:")
        return

    if data == "CHOOSE_TEMPLATE":
        show_template_picker(chat_id)
        return

    if data.startswith("TEMPLATE_"):
        tmpl_id = data[len("TEMPLATE_"):]
        if tmpl_id not in TEMPLATES:
            send_message(chat_id, "❌ טמפלייט לא מוכר.")
            return
        # שמור ב-tenant JSON (קבוע)
        tid = get_or_create_tenant(chat_id)
        save_tenant_field(tid, "settings", {
            **load_tenant_data(tid).get("settings", {}),
            "template_id": tmpl_id
        })
        # שמור גם ב-state
        state = load_state(chat_id)
        if state:
            draft_t, prev_t, flow_t, _ = get_draft_from_state(state)
            stage_t = state["stage"]
        else:
            draft_t, prev_t, flow_t, stage_t = {"tenant_id": tid}, None, "manual", STAGE_EDIT
        if "tenant_id" not in draft_t:
            draft_t["tenant_id"] = tid
        set_draft_in_state(chat_id, stage_t, draft_t, prev_t, flow=flow_t, template_id=tmpl_id)
        label = TEMPLATES[tmpl_id]["label"]
        show_menu(chat_id, f"✅ עיצוב נבחר: *{label}*\nכל ההצעות שלך יוצרו בעיצוב זה.")
        return

    if data == "START_QUOTE":
        start_quote(chat_id)
        return

    if data == "QUOTE_MANUAL":
        start_quote_manual(chat_id)
        return

    if data == "QUOTE_PHOTO":
        send_message(chat_id, "📷 שלח תמונה של כתב היד או הצעה קיימת — אחלץ את הפרטים אוטומטית.")
        tid = get_or_create_tenant(chat_id)
        set_draft_in_state(chat_id, STAGE_EDIT, {"tenant_id": tid}, prev_draft=None, flow="prefill")
        return

    if data == "MY_QUOTES":
        tid = get_or_create_tenant(chat_id)
        show_my_quotes(chat_id, tid)
        return

    if data.startswith("RESEND_QUOTE_"):
        quote_id = data[len("RESEND_QUOTE_"):]
        try:
            r = requests.get(f"{API_URL}/quotes/{quote_id}", timeout=10)
            if r.status_code != 200:
                send_message(chat_id, "❌ לא הצלחתי לטעון את ההצעה.")
                return
            q      = r.json()
            client = q.get("client_name") or "ללא שם"
            total  = q.get("total", 0)
            qnum   = q.get("quote_number", quote_id)
            send_message(chat_id,
                f"📄 הצעה #{qnum} — {client}\n💰 {total:,.0f} ₪",
                reply_markup={"inline_keyboard": [
                    [{"text": "✏️ פתח לעריכה",   "callback_data": f"EDIT_QUOTE_{quote_id}"}],
                    [{"text": "📥 הפק PDF",        "callback_data": f"PDF_QUOTE_{quote_id}"}],
                    [{"text": "📋 שכפל הצעה",      "callback_data": f"CLONE_QUOTE_{quote_id}"}],
                    [{"text": "🔙 חזור לרשימה",    "callback_data": "MY_QUOTES"}],
                ]}
            )
        except Exception as e:
            show_menu(chat_id, f"❌ שגיאה: {e}")
        return

    if data.startswith("PDF_QUOTE_"):
        quote_id = data[len("PDF_QUOTE_"):]
        send_message(chat_id, "⏳ מפיק PDF...")
        try:
            r = requests.get(f"{API_URL}/quotes/{quote_id}/pdf", timeout=120)
            if r.status_code != 200:
                send_message(chat_id, f"❌ לא הצלחתי להפיק PDF: {r.text}")
                return
            stamp    = datetime.now().strftime("%Y-%m-%d_%H%M")
            pdf_path = os.path.join(OUTPUT_DIR, f"quote_{quote_id}_{stamp}.pdf")
            with open(pdf_path, "wb") as f:
                f.write(r.content)
            send_document(chat_id, pdf_path, caption=f"✅ הצעה #{quote_id}")
            show_menu(chat_id, "")
        except Exception as e:
            show_menu(chat_id, f"❌ שגיאה: {e}")
        return

    if data.startswith("EDIT_QUOTE_"):
        quote_id = data[len("EDIT_QUOTE_"):]
        try:
            r = requests.get(f"{API_URL}/quotes/{quote_id}", timeout=10)
            if r.status_code != 200:
                send_message(chat_id, "❌ לא הצלחתי לטעון את ההצעה.")
                return
            q   = r.json()
            tid = get_or_create_tenant(chat_id)
            draft = {
                "tenant_id":       tid,
                "client_name":     q.get("client_name", ""),
                "client_phone":    q.get("client_phone", ""),
                "address":         q.get("address", ""),
                "job_type":        q.get("job_type", ""),
                "raw_description": q.get("raw_description", ""),
                "raw_price_lines": [f"{i['description']} - {int(i['unit_price'])}" for i in (q.get("items") or [])],
                "payment_terms":   q.get("payment_terms", ""),
                "total_price":     str(int(q.get("total", 0))),
            }
            send_preview(chat_id, draft, extra_note="✏️ ההצעה נטענה לעריכה — ערוך לפי הצורך")
        except Exception as e:
            show_menu(chat_id, f"❌ שגיאה: {e}")
        return

    if data == "RESET":
        clear_state(chat_id)
        show_menu(chat_id, "אופס 🔄 איפסתי. בחר פעולה:")
        return

    if data == "HELP":
        show_menu(
            chat_id,
            "ℹ️ איך זה עובד:\n"
            "- לחץ 🧾 כדי למלא ידנית\n"
            "- או שלח תמונה של כתב יד ואקבל טיוטה\n"
            "- אחרי טיוטה אפשר לבקש עריכות חופשי: 'תוסיף 15000', 'תוריד סעיף פירוק', 'תשנה תנאי תשלום'\n"
            "- בסוף לחץ ✅ כדי להפיק מסמך\n"
            "- בכל רגע אפשר /reset"
        )
        return

    state = load_state(chat_id)
    if not state:
        show_menu(chat_id, "בחר פעולה:")
        return

    stage = state["stage"]
    draft, prev_draft, flow, template_id = get_draft_from_state(state)

    if data == "EDIT_MODE":
        if not draft:
            show_menu(chat_id, "אין טיוטה פעילה. לחץ 🧾 כדי להתחיל.")
            return
        set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft, flow="manual")
        send_message(chat_id, "✏️ כתוב מה לשנות (למשל: 'תוסיף 15000', 'תוריד סעיף פירוק', 'תשנה תנאי תשלום...').")
        return

    if data == "UNDO":
        if prev_draft:
            set_draft_in_state(chat_id, STAGE_EDIT, prev_draft, prev_draft=None, flow="manual")
            send_preview(chat_id, prev_draft, extra_note="↩️ חזרתי אחורה שינוי אחד.", keep_prev=None)
        else:
            send_message(chat_id, "אין שינוי אחרון לבטל.")
        return

    if data == "CONFIRM_GENERATE":
        if not draft:
            show_menu(chat_id, "אין טיוטה פעילה. לחץ 🧾 כדי להתחיל.")
            return
        send_message(chat_id, "⏳ מפיק PDF…")
        try:
            # העבר template_id ל-generate_pdf דרך raw_data
            draft_with_template = {**draft, "template_id": template_id}
            generate_pdf(chat_id, draft_with_template)
        except Exception as e:
            show_menu(chat_id, f"❌ שגיאה בזמן יצירת ה-PDF: {e}")
            return

        # נשאיר טיוטה כדי לאפשר עוד עריכות
        set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft, flow="manual")
        show_menu(chat_id, "✅ נשלח. רוצה להתחיל חדש או לערוך עוד?")
        return

# =========================
# Handle single update
# =========================
def handle_update(update: dict):
    """מטפל ב-update בודד — רץ ב-thread נפרד."""
    try:
        cb = update.get("callback_query")
        if cb:
            chat_id = cb["message"]["chat"]["id"]
            handle_callback(chat_id, cb.get("id", ""), cb.get("data", ""))
            return

        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        chat_id = message["chat"]["id"]

        # תמונה כ-Photo
        photo_list = message.get("photo")
        if photo_list:
            file_id = photo_list[-1]["file_id"]
            try:
                ph_state = load_state(chat_id)
                if ph_state and ph_state.get("stage") == STAGE_PROFILE and \
                   (ph_state.get("data") or {}).get("mode") == "logo":
                    image_bytes = download_telegram_file_by_id(file_id)
                    handle_profile_photo(chat_id, image_bytes, "jpg", ph_state.get("data") or {})
                else:
                    send_message(chat_id, "📷 קיבלתי תמונה. מתעתק טקסט ומחלץ שדות...")
                    image_bytes = download_telegram_file_by_id(file_id)
                    full_text   = transcribe_full_text(image_bytes)
                    draft       = extract_fields_from_text(full_text)
                    draft["tenant_id"] = get_or_create_tenant(chat_id)
                    ok, _ = validate_quote(draft)
                    if ok:
                        send_preview(chat_id, draft, keep_prev=None)
                    else:
                        continue_quote_from_prefill(chat_id, draft)
            except Exception as e:
                print(">>> ERROR photo:", repr(e))
                show_menu(chat_id, f"שגיאה בטיפול בתמונה: {e}")
            return

        # תמונה כ-Document
        doc = message.get("document")
        if doc and doc.get("mime_type", "").startswith("image/"):
            try:
                doc_state = load_state(chat_id)
                if doc_state and doc_state.get("stage") == STAGE_PROFILE and \
                   (doc_state.get("data") or {}).get("mode") == "logo":
                    image_bytes = download_telegram_file_by_id(doc["file_id"])
                    handle_profile_photo(chat_id, image_bytes, doc.get("mime_type", "image/jpeg"), doc_state.get("data") or {})
                else:
                    send_message(chat_id, "📎 קיבלתי תמונה כקובץ. מתעתק טקסט ומחלץ שדות...")
                    image_bytes = download_telegram_file_by_id(doc["file_id"])
                    full_text   = transcribe_full_text(image_bytes)
                    draft       = extract_fields_from_text(full_text)
                    draft["tenant_id"] = get_or_create_tenant(chat_id)
                    ok, _ = validate_quote(draft)
                    if ok:
                        send_preview(chat_id, draft, keep_prev=None)
                    else:
                        continue_quote_from_prefill(chat_id, draft)
            except Exception as e:
                print(">>> ERROR doc-image:", repr(e))
                show_menu(chat_id, f"❌ לא הצלחתי להפיק טיוטה מהקובץ: {e}")
            return

        # טקסט רגיל
        text = message.get("text")
        if text:
            print(f">>> הודעה מ-{chat_id}: {text}")
            handle_text_message(chat_id, text)

    except Exception as e:
        print(">>> ERROR handle_update:", repr(e))


# =========================
# Main polling loop
# =========================
def main():
    acquire_lock()
    try:
        init_db()
        print(">>> הבוט רץ (raw polling). Ctrl+C לעצירה.")

        try:
            r = requests.get(f"{TG_URL}/deleteWebhook", timeout=10)
            print(">>> deleteWebhook:", r.text)
        except Exception as e:
            print(">>> deleteWebhook failed:", e)

        last_update_id = None

        while True:
            try:
                params = {"timeout": 30}
                if last_update_id is not None:
                    params["offset"] = last_update_id + 1

                resp = requests.get(f"{TG_URL}/getUpdates", params=params, timeout=35)
                payload = resp.json()

                if not payload.get("ok"):
                    print(">>> שגיאה מה-Telegram API:", payload)
                    time.sleep(3)
                    continue

                import threading
                for update in payload.get("result", []):
                    last_update_id = update["update_id"]
                    threading.Thread(
                        target=handle_update,
                        args=(update,),
                        daemon=True
                    ).start()

            except KeyboardInterrupt:
                print('>>> נעצרת ע"י המשתמש.')
                break
            except Exception as e:
                print(">>> שגיאה בלולאה:", e)
                time.sleep(3)

    finally:
        release_lock()

if __name__ == "__main__":
    main()