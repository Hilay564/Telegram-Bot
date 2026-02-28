print(">>> telegram_raw_bot התחיל לרוץ")

import os
import time
import json
import sqlite3
import re
import requests
from datetime import datetime
from copy import deepcopy

from fill_template import fill_template

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
# 2) קבצים
# =========================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILENAME = "template.docx"
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

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

def acquire_lock():
    if os.path.exists(LOCK_PATH):
        raise RuntimeError("נראה שהבוט כבר רץ (bot.lock קיים). סגור מופע קודם או מחק bot.lock אם נתקע.")
    with open(LOCK_PATH, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))

def release_lock():
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass

# =========================
# 5) State (SQLite)
# =========================
DB_PATH = os.path.join(BASE_DIR, "bot_state.db")
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

def send_document(chat_id, file_path, caption=None):
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        files = {"document": (filename, f)}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
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
STAGE_CREATE_6 = 6
STAGE_EDIT = 90  # מצב "דבר חופשי על הטיוטה"

def main_menu_markup():
    return {
        "inline_keyboard": [
            [{"text": "🧾 יצירת הצעת מחיר", "callback_data": "START_QUOTE"}],
            [{"text": "🧹 איפוס", "callback_data": "RESET"}],
            [{"text": "ℹ️ עזרה", "callback_data": "HELP"}],
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
    """
    מחזיר JSON של פעולות עריכה בלבד.
    המודל לא כותב את ההצעה מחדש — רק אומר מה לשנות.
    """
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
- rewrite_description (text)     (אפשר להשתמש ב-set_field_text במקום, אבל זה בסדר)
- rewrite_payment_terms (text)
- ask_clarifying_question (question)
- no_op

חוקים:
- אל תשנה מחירים/סה\"כ אם לא התבקש.
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

    return resp.parsed or {"actions": [{"type": "ask_clarifying_question", "question": "לא הבנתי מה לשנות. מה בדיוק תרצה לערוך?"}], "notes_to_user": ""}

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
        errors.append("המחיר הכולל חייב להיות מספר בלבד (בלי ₪, בלי פסיקים).")

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
        "🧾 *טיוטת הצעת מחיר*\n\n"
        f"*לקוח:* {d.get('client_name','')}\n"
        f"*כתובת:* {d.get('address','')}\n"
        f"*סוג עבודה:* {d.get('job_type','')}\n\n"
        f"*תיאור:* {d.get('raw_description','')}\n\n"
        f"*סעיפים:*\n{bullets}\n\n"
        f"*תנאי תשלום:* {d.get('payment_terms','')}\n\n"
        f"*סה\"כ:* {d.get('total_price','')} ₪\n"
    )
    # טלגרם MarkdownV2 זה כאב; נשאיר טקסט רגיל כדי לא להישבר על תווים מיוחדים
    return txt.replace("*", "")

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
    """
    מחזיר: (new_draft, question_or_none, notes)
    """
    new_draft = deepcopy(draft)
    actions = actions_payload.get("actions") or []
    notes = (actions_payload.get("notes_to_user") or "").strip()

    # שאלת הבהרה אם צריך
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
# DOCX generate
# =========================
def generate_docx(chat_id: int, raw_data: dict):
    ok, errors = validate_quote(raw_data)
    if not ok:
        show_menu(chat_id, "❌ אי אפשר להפיק עדיין:\n- " + "\n- ".join(errors))
        return

    template_path = os.path.join(BASE_DIR, TEMPLATE_FILENAME)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    client_part = safe_filename(raw_data.get("client_name", ""))
    out_name = f"quote_{stamp}_{client_part}.docx"
    docx_path = os.path.join(OUTPUT_DIR, out_name)

    fill_template(template_path, docx_path, raw_data)
    send_document(chat_id, docx_path, caption="✅ הנה הצעת המחיר (DOCX)")

# =========================
# Flow helpers
# =========================
def start_quote(chat_id: int):
    clear_state(chat_id)
    data = {"draft": {}, "prev_draft": None}
    save_state(chat_id, STAGE_CREATE_0, data)
    send_message(chat_id, "🧾 מתחילים הצעת מחיר.\nשם הלקוח:")

def set_draft_in_state(chat_id: int, stage: int, draft: dict, prev_draft=None):
    data = {"draft": draft, "prev_draft": prev_draft}
    save_state(chat_id, stage, data)

def get_draft_from_state(state):
    data = state.get("data") or {}
    return data.get("draft") or {}, data.get("prev_draft")

def send_preview(chat_id: int, draft: dict, extra_note: str = ""):
    text = build_preview(draft)
    if extra_note:
        text += "\n\n" + extra_note
    send_message(chat_id, text, reply_markup=preview_markup())
    # אחרי Preview תמיד נעבור למצב EDIT (דיבור חופשי)
    st = load_state(chat_id)
    prev = None
    if st:
        _, prev = get_draft_from_state(st)
    set_draft_in_state(chat_id, STAGE_EDIT, draft, prev)

# =========================
# Prefill from image: ask missing fields
# =========================
def continue_quote_from_prefill(chat_id: int, draft: dict):
    # נתחיל בזרימת ה-create, אבל נשמור draft שכבר הגיע
    if not str(draft.get("client_name", "")).strip():
        set_draft_in_state(chat_id, 0, draft)
        send_message(chat_id, "חסר שם לקוח. כתוב שם הלקוח:")
        return
    if not str(draft.get("address", "")).strip():
        set_draft_in_state(chat_id, 1, draft)
        send_message(chat_id, "חסרה כתובת עבודה/עיר. כתוב כתובת:")
        return
    if not str(draft.get("job_type", "")).strip():
        set_draft_in_state(chat_id, 2, draft)
        send_message(chat_id, "חסר סוג עבודה. כתוב סוג עבודה:")
        return
    if not str(draft.get("raw_description", "")).strip():
        set_draft_in_state(chat_id, 3, draft)
        send_message(chat_id, "חסר תיאור קצר. כתוב תיאור קצר:")
        return

    lines = draft.get("raw_price_lines") or []
    if not isinstance(lines, list) or len([x for x in lines if str(x).strip()]) == 0:
        set_draft_in_state(chat_id, 4, draft)
        send_message(chat_id, "חסרים סעיפי עבודה. כתוב כל סעיף בשורה נפרדת:")
        return

    if not str(draft.get("payment_terms", "")).strip():
        set_draft_in_state(chat_id, 5, draft)
        send_message(chat_id, 'חסרים תנאי תשלום/הערות. כתוב תנאים (למשל: לא כולל מע"מ):')
        return

    total = str(draft.get("total_price") or "").strip().replace(",", "").replace("₪", "")
    if not total.isdigit():
        set_draft_in_state(chat_id, 6, draft)
        send_message(chat_id, 'חסר מחיר כולל תקין. כתוב סה"כ (רק מספר, בלי ₪):')
        return

    # טיוטה מלאה -> Preview
    send_preview(chat_id, draft)

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
    draft, prev_draft = get_draft_from_state(state)

    # ===== מצב EDIT: כל טקסט הוא בקשת עריכה על הטיוטה =====
    if stage == STAGE_EDIT:
        if not draft:
            show_menu(chat_id, "אין טיוטה פעילה. לחץ 🧾 כדי להתחיל.")
            return

        send_message(chat_id, "🧠 מבצע עריכה על הטיוטה…")
        try:
            actions_payload = propose_edit_actions(draft, text)
            new_draft, clarifying_q, notes = apply_actions(draft, actions_payload)

            if clarifying_q:
                # לא משנים כלום, רק שאלה
                send_message(chat_id, "❓ " + clarifying_q)
                # נשארים ב-EDIT
                set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft)
                return

            # שמור undo
            set_draft_in_state(chat_id, STAGE_EDIT, new_draft, prev_draft=draft)
            extra = ("📝 " + notes) if notes else ""
            send_preview(chat_id, new_draft, extra_note=extra)
            return

        except Exception as e:
            send_message(chat_id, f"❌ לא הצלחתי לערוך: {e}")
            set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft)
            return

    # ===== זרימת יצירה בשלבים 0..6 (כמו אצלך) =====
    if stage == 0:
        draft["client_name"] = text
        set_draft_in_state(chat_id, 1, draft, prev_draft)
        send_message(chat_id, "כתובת העבודה / עיר:")
        return

    if stage == 1:
        draft["address"] = text
        set_draft_in_state(chat_id, 2, draft, prev_draft)
        send_message(chat_id, "סוג העבודה (למשל: שיפוץ כללי / צבע / אינסטלציה):")
        return

    if stage == 2:
        draft["job_type"] = text
        set_draft_in_state(chat_id, 3, draft, prev_draft)
        send_message(chat_id, "תיאור קצר של העבודה:")
        return

    if stage == 3:
        draft["raw_description"] = text
        set_draft_in_state(chat_id, 4, draft, prev_draft)
        send_message(chat_id, "כתוב כל סעיף עבודה בשורה נפרדת (אפשר גם עם מחירים).")
        return

    if stage == 4:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        draft["raw_price_lines"] = lines
        set_draft_in_state(chat_id, 5, draft, prev_draft)
        send_message(chat_id, 'תנאי תשלום / הערות (למשל: לא כולל מע"מ):')
        return

    if stage == 5:
        draft["payment_terms"] = text
        set_draft_in_state(chat_id, 6, draft, prev_draft)
        send_message(chat_id, 'מהו המחיר הכולל? (רק מספר, בלי ₪):')
        return

    if stage == 6:
        draft["total_price"] = text.replace("₪", "").replace(",", "").strip()
        # טיוטה מלאה -> Preview ולא ישר מסמך
        send_preview(chat_id, draft)
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
    draft, prev_draft = get_draft_from_state(state)

    if data == "EDIT_MODE":
        if not draft:
            show_menu(chat_id, "אין טיוטה פעילה. לחץ 🧾 כדי להתחיל.")
            return
        set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft)
        send_message(chat_id, "✏️ כתוב מה לשנות (למשל: 'תוסיף 15000', 'תוריד סעיף פירוק', 'תשנה תנאי תשלום...').")
        return

    if data == "UNDO":
        if prev_draft:
            set_draft_in_state(chat_id, STAGE_EDIT, prev_draft, prev_draft=None)
            send_preview(chat_id, prev_draft, extra_note="↩️ חזרתי אחורה שינוי אחד.")
        else:
            send_message(chat_id, "אין שינוי אחרון לבטל.")
        return

    if data == "CONFIRM_GENERATE":
        if not draft:
            show_menu(chat_id, "אין טיוטה פעילה. לחץ 🧾 כדי להתחיל.")
            return
        send_message(chat_id, "⏳ מפיק מסמך…")
        try:
            generate_docx(chat_id, draft)
        except Exception as e:
            show_menu(chat_id, f"❌ שגיאה בזמן יצירת המסמך: {e}")
            return
        # נשאיר טיוטה כדי לאפשר עוד עריכות, אבל אפשר גם לאפס:
        set_draft_in_state(chat_id, STAGE_EDIT, draft, prev_draft)
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
                                send_preview(chat_id, draft)
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
                                send_preview(chat_id, draft)
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
                print(">>> נעצרת ע\"י המשתמש.")
                break
            except Exception as e:
                print(">>> שגיאה בלולאה:", e)
                time.sleep(3)

    finally:
        release_lock()

if __name__ == "__main__":
    main()
