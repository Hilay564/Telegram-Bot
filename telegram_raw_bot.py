print(">>> telegram_raw_bot התחיל לרוץ")

import os
import time
import json
import sqlite3
import requests
from datetime import datetime

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
# 5) Draft + State (SQLite) + migrations
# =========================
DB_PATH = os.path.join(BASE_DIR, "bot_state.db")
DB_VERSION = 1

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
חלץ מהטקסט לשדות בדיוק כמו בטופס /quote:
- client_name: שם הלקוח
- address: כתובת העבודה / עיר
- job_type: סוג העבודה
- raw_description: תיאור קצר
- raw_price_lines: כל סעיף בשורה נפרדת (כמו בטקסט)
- payment_terms: תנאי תשלום / הערות
- total_price: הסכום הכולל בלבד (מספר)

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
# יצירת DOCX ושליחה
# =========================
def generate_and_send_docx(chat_id: int, raw_data: dict):
    ok, errors = validate_quote(raw_data)
    if not ok:
        show_menu(chat_id, "❌ אי אפשר ליצור הצעת מחיר עדיין:\n- " + "\n- ".join(errors))
        return

    template_path = os.path.join(BASE_DIR, TEMPLATE_FILENAME)

    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    client_part = safe_filename(raw_data.get("client_name", ""))
    out_name = f"quote_{stamp}_{client_part}.docx"
    docx_path = os.path.join(OUTPUT_DIR, out_name)

    fill_template(template_path, docx_path, raw_data)
    send_document(chat_id, docx_path, caption="✅ הנה הצעת המחיר (DOCX)")
    show_menu(chat_id, "עוד משהו?")

# =========================
# Prefill from image: ask missing fields
# =========================
def continue_quote_from_prefill(chat_id: int, data: dict):
    """
    אם הגיעו נתונים מתמונה וחסר משהו - ממשיכים כמו /quote
    ושואלים רק את השדה הבא שחסר.
    stages:
      0 name
      1 address
      2 job_type
      3 description
      4 lines
      5 terms
      6 total
    """
    clear_state(chat_id)

    if not str(data.get("client_name", "")).strip():
        save_state(chat_id, 0, data)
        send_message(chat_id, "חסר שם לקוח. כתוב שם הלקוח:")
        return

    if not str(data.get("address", "")).strip():
        save_state(chat_id, 1, data)
        send_message(chat_id, "חסרה כתובת עבודה/עיר. כתוב כתובת:")
        return

    if not str(data.get("job_type", "")).strip():
        save_state(chat_id, 2, data)
        send_message(chat_id, "חסר סוג עבודה. כתוב סוג עבודה:")
        return

    if not str(data.get("raw_description", "")).strip():
        save_state(chat_id, 3, data)
        send_message(chat_id, "חסר תיאור קצר. כתוב תיאור קצר:")
        return

    lines = data.get("raw_price_lines") or []
    if not isinstance(lines, list) or len([x for x in lines if str(x).strip()]) == 0:
        save_state(chat_id, 4, data)
        send_message(chat_id, "חסרים סעיפי עבודה. כתוב כל סעיף בשורה נפרדת:")
        return

    if not str(data.get("payment_terms", "")).strip():
        save_state(chat_id, 5, data)
        send_message(chat_id, 'חסרים תנאי תשלום/הערות. כתוב תנאים (למשל: לא כולל מע"מ):')
        return

    total = str(data.get("total_price") or "").strip().replace(",", "").replace("₪", "")
    if not total.isdigit():
        save_state(chat_id, 6, data)
        send_message(chat_id, 'חסר מחיר כולל תקין. כתוב סה"כ (רק מספר, בלי ₪):')
        return

    generate_and_send_docx(chat_id, data)

# =========================
# Flow
# =========================
def start_quote(chat_id: int):
    clear_state(chat_id)
    save_state(chat_id, 0, {})
    send_message(chat_id, "🧾 מתחילים הצעת מחיר.\nשם הלקוח:")

def handle_text_message(chat_id: int, text: str):
    text = (text or "").strip()

    # תמיד עקבי: /start ו-/quote רק תפריט + איפוס state
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
    data = state["data"] or {}

    if stage == 0:
        data["client_name"] = text
        save_state(chat_id, 1, data)
        send_message(chat_id, "כתובת העבודה / עיר:")
        return

    if stage == 1:
        data["address"] = text
        save_state(chat_id, 2, data)
        send_message(chat_id, "סוג העבודה (למשל: שיפוץ כללי / צבע / אינסטלציה):")
        return

    if stage == 2:
        data["job_type"] = text
        save_state(chat_id, 3, data)
        send_message(chat_id, "תיאור קצר של העבודה:")
        return

    if stage == 3:
        data["raw_description"] = text
        save_state(chat_id, 4, data)
        send_message(chat_id, "כתוב כל סעיף עבודה בשורה נפרדת (אפשר גם עם מחירים).")
        return

    if stage == 4:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        data["raw_price_lines"] = lines
        save_state(chat_id, 5, data)
        send_message(chat_id, 'תנאי תשלום / הערות (למשל: לא כולל מע"מ):')
        return

    if stage == 5:
        data["payment_terms"] = text
        save_state(chat_id, 6, data)
        send_message(chat_id, "מהו המחיר הכולל? (רק מספר, בלי ₪):")
        return

    if stage == 6:
        data["total_price"] = text.replace("₪", "").replace(",", "").strip()
        send_message(chat_id, "⏳ מייצר DOCX ושולח...")

        try:
            generate_and_send_docx(chat_id, data)
        except Exception as e:
            show_menu(chat_id, f"❌ שגיאה בזמן יצירת המסמך: {e}")

        clear_state(chat_id)
        return

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
            "- או שלח תמונה של כתב יד ואקבל הצעה אוטומטית\n"
            "- בכל רגע אפשר /reset"
        )
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
                            send_message(chat_id, "📷 קיבלתי תמונה. מתעתק טקסט ומחלץ שדות...")
                            image_bytes = download_telegram_file_by_id(file_id)

                            full_text = transcribe_full_text(image_bytes)
                            raw_data = extract_fields_from_text(full_text)

                            ok, _ = validate_quote(raw_data)
                            if ok:
                                generate_and_send_docx(chat_id, raw_data)
                            else:
                                continue_quote_from_prefill(chat_id, raw_data)
                        except Exception as e:
                            print(">>> ERROR while handling photo:", repr(e))
                            show_menu(chat_id, f"❌ לא הצלחתי להפיק הצעה מהתמונה: {e}")
                        continue

                    # ===== תמונה כ-Document (קובץ) =====
                    doc = message.get("document")
                    if doc and (doc.get("mime_type", "").startswith("image/")):
                        try:
                            send_message(chat_id, "📎 קיבלתי תמונה כקובץ. מתעתק טקסט ומחלץ שדות...")
                            image_bytes = download_telegram_file_by_id(doc["file_id"])

                            full_text = transcribe_full_text(image_bytes)
                            raw_data = extract_fields_from_text(full_text)

                            ok, _ = validate_quote(raw_data)
                            if ok:
                                generate_and_send_docx(chat_id, raw_data)
                            else:
                                continue_quote_from_prefill(chat_id, raw_data)
                        except Exception as e:
                            print(">>> ERROR while handling document-image:", repr(e))
                            show_menu(chat_id, f"❌ לא הצלחתי להפיק הצעה מהקובץ: {e}")
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
