print(">>> telegram_raw_bot התחיל לרוץ")

import os
import time
import json
import sqlite3
import re
import requests
import urllib.parse
import tempfile
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

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
# =========================
# FastAPI PDF engine
# =========================
FASTAPI_BASE_URL = os.getenv("FASTAPI_BASE_URL", "http://127.0.0.1:8000")

def create_quote_pdf_via_api(raw_data: dict) -> bytes:

    url = f"{FASTAPI_BASE_URL}/quote/pdf-from-draft"

    payload = {
        "tenant_id": raw_data.get("tenant_id", "nimrod"),
        "client_name": raw_data.get("client_name"),
        "address": raw_data.get("address"),
        "job_type": raw_data.get("job_type"),
        "raw_description": raw_data.get("raw_description"),
        "raw_price_lines": raw_data.get("raw_price_lines"),
        "payment_terms": raw_data.get("payment_terms"),
    }

    r = requests.post(url, json=payload, timeout=120)

    if r.status_code != 200:
        raise RuntimeError(f"FastAPI {r.status_code}: {r.text}")

    return r.content

# ✅ כתובת השרת של FastAPI (לוקאלי עכשיו, ענן בעתיד)

# =========================
# 2) קבצים
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILENAME = "template.docx"
OUTPUT_DIR = os.path.join(BASE_DIR, "output")

# =========================
# 3) Gemini SDK
# =========================
from google import genai
from google.genai import types

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

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
# Telegram helpers
# =========================
def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
    requests.post(f"{API_URL}/sendMessage", data=payload, timeout=20)

def answer_callback_query(callback_query_id: str):
    try:
        requests.post(f"{API_URL}/answerCallbackQuery", data={"callback_query_id": callback_query_id}, timeout=10)
    except Exception:
        pass

def send_document(chat_id, file_path, caption=None, reply_markup=None):
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        files = {"document": (filename, f)}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        requests.post(f"{API_URL}/sendDocument", data=data, files=files, timeout=120)

def download_telegram_file_by_id(file_id: str) -> bytes:
    r = requests.get(f"{API_URL}/getFile", params={"file_id": file_id}, timeout=20)
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
STAGE_CREATE_7 = 7
STAGE_EDIT = 90  # מצב "דבר חופשי על הטיוטה"

def main_menu_markup():
    return {
        "inline_keyboard": [
            [{"text": "🧾 יצירת הצעת מחיר",  "callback_data": "START_QUOTE"}],
            [{"text": "📋 כל ההצעות שלי",     "callback_data": "MY_QUOTES"}],
            [{"text": "🧹 איפוס",              "callback_data": "RESET"}],
            [{"text": "ℹ️ עזרה",               "callback_data": "HELP"}],
        ]
    }

def show_menu(chat_id: int, text: str = "בחר פעולה:"):
    send_message(chat_id, text, reply_markup=main_menu_markup())

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
def set_draft_in_state(chat_id: int, stage: int, draft: dict, prev_draft=None, flow: str = "manual"):
    data = {"draft": draft, "prev_draft": prev_draft, "flow": flow}
    save_state(chat_id, stage, data)

def get_draft_from_state(state):
    data = state.get("data") or {}
    return data.get("draft") or {}, data.get("prev_draft"), (data.get("flow") or "manual")

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
    resp = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(text=prompt),
                    types.Part(inline_data=types.Blob(mime_type="image/jpeg", data=image_bytes)),
                ],
            )
        ],
        config=types.GenerateContentConfig(temperature=0.0),
    )
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
            "client_name", "address", "job_type",
            "raw_description", "raw_price_lines",
            "payment_terms", "total_price"
        ],
        "properties": {
            "client_name": {"type": "STRING"},
            "address": {"type": "STRING"},
            "job_type": {"type": "STRING"},
            "raw_description": {"type": "STRING"},
            "raw_price_lines": {"type": "ARRAY", "items": {"type": "STRING"}},
            "payment_terms": {"type": "STRING"},
            "total_price": {"type": "STRING"}
        }
    }

    prompt = f"""
חלץ מהטקסט לשדות:
- client_name
- address
- job_type
- raw_description
- raw_price_lines (כל סעיף בשורה)
- payment_terms
- total_price (מספר בלבד)

חוקים:
- אל תמציא.
- אם חסר שדה החזר "" או [].
החזר JSON בלבד.

הטקסט:
<<<
{full_text}
>>>
"""

    resp = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_msg,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.1,
        ),
    )

    data = resp.parsed or {}
    data["total_price"] = (data.get("total_price") or "").replace("₪", "").replace(",", "").strip()
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
- set_field_text (field, text)   שדות: client_name, address, job_type, raw_description, payment_terms
- add_line_item (text)
- remove_line_item (match)       match = טקסט לחיפוש בסעיף
- rewrite_description (text)
- rewrite_payment_terms (text)
- ask_clarifying_question (question)
- no_op

חוקים:
- אל תשנה מחירים/סה"כ אם לא התבקש.
- אל תמחוק/תוסיף סעיפים אם לא התבקש.
- אם המשתמש אמר "תוסיף 15000" בלי לציין למה, תפרש כברירת מחדל: increase_total_by.
- אם המשתמש אמר "תוריד סעיף X" וה-match לא ברור — תשאל.
"""

    resp = gemini_client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_msg,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.1,
        ),
    )

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
_ALLOWED_FIELDS = {"client_name", "address", "job_type", "raw_description", "payment_terms"}

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
# WhatsApp button helper
# =========================
_TENANTS_DIR = os.path.join(BASE_DIR, "..", "tenants")

def _wa_phone(raw_phone: str) -> str:
    """ממיר מספר ישראלי לפורמט בינלאומי ללא סימנים (972XXXXXXXXX)."""
    digits = re.sub(r"\D", "", raw_phone)
    if digits.startswith("0"):
        digits = "972" + digits[1:]
    return digits

def _build_wa_markup(raw_data: dict, stamp: str) -> dict | None:
    """בונה inline_keyboard עם כפתור WhatsApp, או None אם אין טלפון לקוח."""
    client_phone = str(raw_data.get("client_phone") or "").strip()
    if not client_phone:
        return None

    tenant_id = raw_data.get("tenant_id", "nimrod")
    try:
        tenant_path = os.path.join(_TENANTS_DIR, f"{tenant_id}.json")
        with open(tenant_path, "r", encoding="utf-8") as f:
            tenant = json.load(f)
        business_name = tenant.get("business_name", "")
    except Exception:
        business_name = ""

    total = str(raw_data.get("total_price") or "").replace(",", "").replace("₪", "").strip()
    text = f"שלום, אני {business_name}. הצעת מחיר מס׳ {stamp} על סך {total} ₪ ממתינה לאישורך."
    wa_url = f"https://wa.me/{_wa_phone(client_phone)}?text={urllib.parse.quote(text)}"

    return {"inline_keyboard": [[{"text": "📲 שלח ב-WhatsApp ללקוח", "url": wa_url}]]}

# =========================
# Tenant helper
# =========================
def get_or_create_tenant(chat_id: int) -> str:
    return "nimrod"

# =========================
# Save quote to API + build share markup
# =========================
def _save_quote_to_api(raw_data: dict, pdf_path: str) -> str | None:
    total_str = str(raw_data.get("total_price") or "0").replace(",", "").replace("₪", "").strip()
    payload = {
        "tenant_id":       raw_data.get("tenant_id", "nimrod"),
        "client_name":     raw_data.get("client_name"),
        "address":         raw_data.get("address"),
        "job_type":        raw_data.get("job_type"),
        "raw_description": raw_data.get("raw_description"),
        "raw_price_lines": raw_data.get("raw_price_lines"),
        "payment_terms":   raw_data.get("payment_terms"),
        "total":           float(total_str) if total_str.isdigit() else 0.0,
        "pdf_path":        pdf_path,
    }
    try:
        r = requests.post(f"{FASTAPI_BASE_URL}/quotes/save", json=payload, timeout=10)
        if r.status_code == 200:
            return r.json().get("quote_id")
    except Exception:
        pass
    return None

def _build_share_markup(raw_data: dict, stamp: str, quote_id: str | None) -> dict:
    rows = []

    client_phone = str(raw_data.get("client_phone") or "").strip()
    if client_phone:
        tenant_id = raw_data.get("tenant_id", "nimrod")
        try:
            with open(os.path.join(_TENANTS_DIR, f"{tenant_id}.json"), "r", encoding="utf-8") as f:
                business_name = json.load(f).get("business_name", "")
        except Exception:
            business_name = ""
        total_txt = str(raw_data.get("total_price") or "").replace(",", "").replace("₪", "").strip()
        wa_text = f"שלום, אני {business_name}. הצעת מחיר מס׳ {stamp} על סך {total_txt} ₪ ממתינה לאישורך."
        wa_url = f"https://wa.me/{_wa_phone(client_phone)}?text={urllib.parse.quote(wa_text)}"
        rows.append([{"text": "📤 שתף ללקוח", "url": wa_url}])

    if quote_id:
        rows.append([{"text": "🔗 לינק להורדה", "callback_data": f"GET_LINK_{quote_id}"}])

    rows.append([
        {"text": "📋 כל ההצעות שלי", "callback_data": "MY_QUOTES"},
        {"text": "📄 הצעה חדשה",     "callback_data": "START_QUOTE"},
    ])
    return {"inline_keyboard": rows}

# =========================
# Show saved quotes list
# =========================
def show_my_quotes(chat_id: int):
    tid = get_or_create_tenant(chat_id)
    try:
        r = requests.get(f"{FASTAPI_BASE_URL}/quotes", params={"tenant_id": tid}, timeout=10)
        quotes = r.json() if r.status_code == 200 else []
    except Exception:
        quotes = []

    if not quotes:
        send_message(chat_id, "אין הצעות מחיר שמורות עדיין.")
        return

    rows = []
    for i, q in enumerate(quotes[:10], 1):
        qnum = q.get("quote_number", i)
        client = q.get("client_name") or "—"
        rows.append([
            {"text": f"📄 #{qnum} — {client}", "callback_data": f"RESEND_QUOTE_{q['id']}"},
            {"text": "📋 שכפל",               "callback_data": f"CLONE_QUOTE_{q['id']}"},
        ])
    rows.append([{"text": "↩️ חזור לתפריט", "callback_data": "BACK_MENU"}])
    send_message(chat_id, "📋 ההצעות שלך:", reply_markup={"inline_keyboard": rows})

# =========================
# PDF generate (✅ via FastAPI)
# =========================
def generate_pdf(chat_id: int, raw_data: dict):
    ok, errors = validate_quote(raw_data)
    if not ok:
        show_menu(chat_id, "❌ אי אפשר להפיק עדיין:\n- " + "\n- ".join(errors))
        return

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    client_part = safe_filename(raw_data.get("client_name", ""))
    out_name = f"quote_{stamp}_{client_part}.pdf"
    pdf_path = os.path.join(OUTPUT_DIR, out_name)

    try:
        pdf_bytes = create_quote_pdf_via_api(raw_data)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        quote_id = _save_quote_to_api(raw_data, pdf_path)
        share_markup = _build_share_markup(raw_data, stamp, quote_id)
        send_document(chat_id, pdf_path, caption="✅ הנה הצעת המחיר (PDF)", reply_markup=share_markup)

    except Exception as e:
        show_menu(chat_id, f"❌ שגיאה ביצירת PDF דרך השרת: {e}")

# =========================
# Flow helpers
# =========================
def start_quote(chat_id: int):
    clear_state(chat_id)
    set_draft_in_state(chat_id, STAGE_CREATE_0, {}, prev_draft=None, flow="manual")
    send_message(chat_id, "🧾 מתחילים הצעת מחיר.\nשם הלקוח:")

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
    if not str(draft.get("client_name", "")).strip():
        set_draft_in_state(chat_id, 0, draft, flow="prefill")
        send_message(chat_id, "חסר שם לקוח. כתוב שם הלקוח:")
        return
    if not str(draft.get("address", "")).strip():
        set_draft_in_state(chat_id, 2, draft, flow="prefill")
        send_message(chat_id, "חסרה כתובת עבודה/עיר. כתוב כתובת:")
        return
    if not str(draft.get("job_type", "")).strip():
        set_draft_in_state(chat_id, 3, draft, flow="prefill")
        send_message(chat_id, "חסר סוג עבודה. כתוב סוג עבודה:")
        return
    if not str(draft.get("raw_description", "")).strip():
        set_draft_in_state(chat_id, 4, draft, flow="prefill")
        send_message(chat_id, "חסר תיאור קצר. כתוב תיאור קצר:")
        return

    lines = draft.get("raw_price_lines") or []
    if not isinstance(lines, list) or len([x for x in lines if str(x).strip()]) == 0:
        set_draft_in_state(chat_id, 5, draft, flow="prefill")
        send_message(chat_id, "חסרים סעיפי עבודה. כתוב כל סעיף בשורה נפרדת:")
        return

    if not str(draft.get("payment_terms", "")).strip():
        set_draft_in_state(chat_id, 6, draft, flow="prefill")
        send_message(chat_id, 'חסרים תנאי תשלום/הערות. כתוב תנאים (למשל: לא כולל מע"מ):')
        return

    total = str(draft.get("total_price") or "").strip().replace(",", "").replace("₪", "")
    if not total.isdigit():
        set_draft_in_state(chat_id, 7, draft, flow="prefill")
        send_message(chat_id, 'חסר מחיר כולל תקין. כתוב סה"כ (רק מספר, בלי ₪):')
        return

    # טיוטה מלאה -> Preview
    send_preview(chat_id, draft, keep_prev=None)

# =========================
# Handle text messages
# =========================
def handle_text_message(chat_id: int, text: str):
    text = (text or "").strip()

    if text in ("/start", "/quote"):
        clear_state(chat_id)
        show_menu(chat_id, "בחר פעולה:")
        return

    if text == "/reset":
        clear_state(chat_id)
        show_menu(chat_id, "אופס 🔄 איפסתי. בחר פעולה:")
        return

    state = load_state(chat_id)
    if not state:
        show_menu(chat_id, "בחר פעולה:")
        return

    stage = state["stage"]
    draft, prev_draft, flow = get_draft_from_state(state)

    # ===== מצב EDIT =====
    if stage == STAGE_EDIT:
        if not draft:
            show_menu(chat_id, "אין טיוטה פעילה. לחץ 🧾 כדי להתחיל.")
            return

        send_message(chat_id, "🧠 מבצע עריכה על הטיוטה…")
        try:
            actions_payload = propose_edit_actions(draft, text)
            new_draft, clarifying_q, notes = apply_actions(draft, actions_payload)

            if clarifying_q:
                send_message(chat_id, "❓ " + clarifying_q)
                set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft, flow="manual")
                return

            # שמור undo
            set_draft_in_state(chat_id, STAGE_EDIT, new_draft, prev_draft=draft, flow="manual")
            extra = ("📝 " + notes) if notes else ""
            send_preview(chat_id, new_draft, extra_note=extra, keep_prev=draft)
            return

        except Exception as e:
            send_message(chat_id, f"❌ לא הצלחתי לערוך: {e}")
            set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft, flow="manual")
            return

    # ===== FIX: אם זה prefill, כל תשובה משלימה שדה ואז שוב בודקים מה חסר =====
    if flow == "prefill" and stage in (0, 2, 3, 4, 5, 6, 7):
        if stage == 0:
            draft["client_name"] = text
        elif stage == 2:
            draft["address"] = text
        elif stage == 3:
            draft["job_type"] = text
        elif stage == 4:
            draft["raw_description"] = text
        elif stage == 5:
            draft["raw_price_lines"] = [ln.strip() for ln in text.split("\n") if ln.strip()]
        elif stage == 6:
            draft["payment_terms"] = text
        elif stage == 7:
            draft["total_price"] = text.replace("₪", "").replace(",", "").strip()

        continue_quote_from_prefill(chat_id, draft)
        return

    # ===== זרימת יצירה ידנית =====
    if stage == 0:
        draft["client_name"] = text
        set_draft_in_state(chat_id, 1, draft, prev_draft, flow="manual")
        send_message(chat_id, "📱 טלפון הלקוח (לשיתוף WhatsApp):\nאפשר לדלג עם /")
        return

    if stage == 1:
        draft["client_phone"] = "" if text == "/" else text
        set_draft_in_state(chat_id, 2, draft, prev_draft, flow="manual")
        send_message(chat_id, "כתובת העבודה / עיר:")
        return

    if stage == 2:
        draft["address"] = text
        set_draft_in_state(chat_id, 3, draft, prev_draft, flow="manual")
        send_message(chat_id, "סוג העבודה (למשל: שיפוץ כללי / צבע / אינסטלציה):")
        return

    if stage == 3:
        draft["job_type"] = text
        set_draft_in_state(chat_id, 4, draft, prev_draft, flow="manual")
        send_message(chat_id, "תיאור קצר של העבודה:")
        return

    if stage == 4:
        draft["raw_description"] = text
        set_draft_in_state(chat_id, 5, draft, prev_draft, flow="manual")
        send_message(chat_id, "כתוב כל סעיף עבודה בשורה נפרדת (אפשר גם עם מחירים).")
        return

    if stage == 5:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        draft["raw_price_lines"] = lines
        set_draft_in_state(chat_id, 6, draft, prev_draft, flow="manual")
        send_message(chat_id, 'תנאי תשלום / הערות (למשל: לא כולל מע"מ):')
        return

    if stage == 6:
        draft["payment_terms"] = text
        set_draft_in_state(chat_id, 7, draft, prev_draft, flow="manual")
        send_message(chat_id, 'מהו המחיר הכולל? (רק מספר, בלי ₪):')
        return

    if stage == 7:
        draft["total_price"] = text.replace("₪", "").replace(",", "").strip()
        send_preview(chat_id, draft, keep_prev=None)
        return

    show_menu(chat_id, "בחר פעולה:")

# =========================
# Callbacks
# =========================
def handle_callback(chat_id: int, callback_query_id: str, data: str):
    answer_callback_query(callback_query_id)

    if data == "START_QUOTE":
        start_quote(chat_id)
        return

    if data == "MY_QUOTES":
        show_my_quotes(chat_id)
        return

    if data == "BACK_MENU":
        show_menu(chat_id, "בחר פעולה:")
        return

    if data.startswith("RESEND_QUOTE_"):
        quote_id = data[len("RESEND_QUOTE_"):]
        try:
            r = requests.get(f"{FASTAPI_BASE_URL}/quotes/{quote_id}/pdf", timeout=30)
            if r.status_code != 200:
                send_message(chat_id, "❌ לא הצלחתי למצוא את ה-PDF.")
                return
            tmp_path = os.path.join(OUTPUT_DIR, f"resend_{quote_id[:8]}.pdf")
            with open(tmp_path, "wb") as f:
                f.write(r.content)
            send_document(chat_id, tmp_path, caption="📄 הצעת המחיר")
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
        except Exception as e:
            send_message(chat_id, f"❌ שגיאה: {e}")
        return

    if data.startswith("CLONE_QUOTE_"):
        quote_id = data[len("CLONE_QUOTE_"):]
        try:
            r = requests.get(f"{FASTAPI_BASE_URL}/quotes/{quote_id}", timeout=10)
            if r.status_code != 200:
                send_message(chat_id, "❌ לא הצלחתי לטעון את ההצעה.")
                return
            q = r.json()
            tid = get_or_create_tenant(chat_id)
            draft = {
                "tenant_id":       tid,
                "client_name":     "",
                "client_phone":    "",
                "address":         q.get("address", ""),
                "job_type":        q.get("job_type", ""),
                "raw_description": q.get("raw_description", ""),
                "raw_price_lines": [f"{i['description']} - {int(i['unit_price'])}" for i in (q.get("items") or [])],
                "payment_terms":   q.get("payment_terms", ""),
                "total_price":     str(int(q.get("total", 0))),
            }
            set_draft_in_state(chat_id, STAGE_CREATE_0, draft, prev_draft=None, flow="manual")
            send_message(chat_id, "📋 העתקתי את ההצעה!\nמה שם הלקוח החדש?")
        except Exception as e:
            send_message(chat_id, f"❌ שגיאה: {e}")
        return

    if data.startswith("GET_LINK_"):
        quote_id = data[len("GET_LINK_"):]
        link = f"{FASTAPI_BASE_URL}/quotes/{quote_id}/pdf"
        send_message(chat_id, f"🔗 לינק להורדת ה-PDF:\n{link}\n\nשלח את הלינק ללקוח או העתק אותו.")
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
    draft, prev_draft, flow = get_draft_from_state(state)

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
            generate_pdf(chat_id, draft)
        except Exception as e:
            show_menu(chat_id, f"❌ שגיאה בזמן יצירת ה-PDF: {e}")
            return

        # נשאיר טיוטה כדי לאפשר עוד עריכות
        set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft, flow="manual")
        show_menu(chat_id, "✅ נשלח. רוצה להתחיל חדש או לערוך עוד?")
        return

# =========================
# Main polling loop
# =========================
def main():
    acquire_lock()
    try:
        init_db()
        print(">>> הבוט רץ (raw polling). Ctrl+C לעצירה.")

        try:
            r = requests.get(f"{API_URL}/deleteWebhook", timeout=10)
            print(">>> deleteWebhook:", r.text)
        except Exception as e:
            print(">>> deleteWebhook failed:", e)

        last_update_id = None

        while True:
            try:
                params = {"timeout": 30}
                if last_update_id is not None:
                    params["offset"] = last_update_id + 1

                resp = requests.get(f"{API_URL}/getUpdates", params=params, timeout=35)
                payload = resp.json()

                if not payload.get("ok"):
                    print(">>> שגיאה מה-Telegram API:", payload)
                    time.sleep(3)
                    continue

                for update in payload.get("result", []):
                    last_update_id = update["update_id"]

                    cb = update.get("callback_query")
                    if cb:
                        chat_id = cb["message"]["chat"]["id"]
                        cb_data = cb.get("data", "")
                        cb_id = cb.get("id", "")
                        handle_callback(chat_id, cb_id, cb_data)
                        continue

                    message = update.get("message") or update.get("edited_message")
                    if not message:
                        continue

                    chat_id = message["chat"]["id"]

                    # ===== תמונה כ-Photo =====
                    photo_list = message.get("photo")
                    if photo_list:
                        best_photo = photo_list[-1]
                        file_id = best_photo["file_id"]
                        try:
                            send_message(chat_id, "📷 קיבלתי תמונה. מתעתק טקסט ומחלץ שדות…")
                            image_bytes = download_telegram_file_by_id(file_id)

                            full_text = transcribe_full_text(image_bytes)
                            draft = extract_fields_from_text(full_text)

                            ok, _ = validate_quote(draft)
                            if ok:
                                send_preview(chat_id, draft, keep_prev=None)
                            else:
                                continue_quote_from_prefill(chat_id, draft)
                        except Exception as e:
                            print(">>> ERROR while handling photo:", repr(e))
                            show_menu(chat_id, f"❌ לא הצלחתי להפיק טיוטה מהתמונה: {e}")
                        continue

                    # ===== תמונה כ-Document =====
                    doc = message.get("document")
                    if doc and (doc.get("mime_type", "").startswith("image/")):
                        try:
                            send_message(chat_id, "📎 קיבלתי תמונה כקובץ. מתעתק טקסט ומחלץ שדות…")
                            image_bytes = download_telegram_file_by_id(doc["file_id"])

                            full_text = transcribe_full_text(image_bytes)
                            draft = extract_fields_from_text(full_text)

                            ok, _ = validate_quote(draft)
                            if ok:
                                send_preview(chat_id, draft, keep_prev=None)
                            else:
                                continue_quote_from_prefill(chat_id, draft)
                        except Exception as e:
                            print(">>> ERROR while handling document-image:", repr(e))
                            show_menu(chat_id, f"❌ לא הצלחתי להפיק טיוטה מהקובץ: {e}")
                        continue

                    # ===== טקסט רגיל =====
                    text = message.get("text")
                    if text:
                        print(f">>> הודעה מ-{chat_id}: {text}")
                        handle_text_message(chat_id, text)

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
