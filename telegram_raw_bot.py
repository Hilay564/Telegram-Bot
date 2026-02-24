print(">>> telegram_raw_bot התחיל לרוץ")

import os
import time
import json
import sqlite3
import requests

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
TEMPLATE_FILENAME = "template.docx"
DOCX_FILENAME = "הצעת_מחיר.docx"

# =========================
# 3) Gemini SDK
# =========================
from google import genai
from google.genai import types

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# =========================
# 4) Draft + State (SQLite)
# =========================
DB_PATH = "bot_state.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS states (
            chat_id INTEGER PRIMARY KEY,
            stage INTEGER NOT NULL,
            data_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)
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
def send_message(chat_id, text):
    requests.post(
        f"{API_URL}/sendMessage",
        data={"chat_id": chat_id, "text": text},
        timeout=20
    )

def send_document(chat_id, file_path, caption=None):
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        files = {"document": (filename, f)}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        requests.post(
            f"{API_URL}/sendDocument",
            data=data,
            files=files,
            timeout=120
        )

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
# AI step 1: תעתוק מלא מהתמונה (טקסט בלבד)
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
סדר/חלץ מהטקסט לשדות בדיוק כמו בטופס /quote:

- client_name: שם הלקוח
- address: כתובת
- job_type: סוג העבודה
- raw_description: תיאור כללי קצר
- raw_price_lines: רשימת סעיפים (כמו בטקסט, כולל מחירים אם מופיעים ליד הסעיף)
- payment_terms: תנאי תשלום/הערות כלליות (כמו "לא כולל מע\"מ", "כולל חומרים", "לא כולל כלים סניטריים"...)
- total_price: הסכום הכולל בלבד (אם מופיע "סה\"כ/סך הכל/מחיר כולל")

חוקים:
- אל תמציא שום מידע.
- אם אין שדה בטקסט, החזר "" או [] בהתאם.

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
# יצירת DOCX ושליחה
# =========================
def generate_and_send_docx(chat_id: int, raw_data: dict):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    template_path = os.path.join(base_dir, TEMPLATE_FILENAME)
    docx_path = os.path.join(base_dir, DOCX_FILENAME)

    fill_template(template_path, docx_path, raw_data)
    send_document(chat_id, docx_path, caption="✅ הנה הצעת המחיר (DOCX)")

# =========================
# Flow טקסט רגיל (/quote)
# =========================
def handle_text_message(chat_id: int, text: str):
    text = (text or "").strip()

    if text == "/start":
        send_message(chat_id, "היי 👋\nשלח /quote כדי ליצור הצעת מחיר בטקסט.\nאו שלח תמונה של כתב יד כדי ליצור הצעה אוטומטית (DOCX).")
        return

    if text == "/quote":
        clear_state(chat_id)
        save_state(chat_id, 0, {})
        send_message(chat_id, "שם הלקוח:")
        return

    state = load_state(chat_id)
    if not state:
        send_message(chat_id, "שלח /quote כדי להתחיל, או שלח תמונה של כתב יד.")
        return

    stage = state["stage"]
    data = state["data"] or {}

    if stage == 0:
        data["client_name"] = text
        save_state(chat_id, 1, data)
        send_message(chat_id, "כתובת העבודה:")
        return

    if stage == 1:
        data["address"] = text
        save_state(chat_id, 2, data)
        send_message(chat_id, "סוג העבודה:")
        return

    if stage == 2:
        data["job_type"] = text
        save_state(chat_id, 3, data)
        send_message(chat_id, "תיאור קצר של העבודה:")
        return

    if stage == 3:
        data["raw_description"] = text
        save_state(chat_id, 4, data)
        send_message(chat_id, "כתוב כל סעיף עבודה בשורה נפרדת (אפשר גם עם מחירים אם תרצה).")
        return

    if stage == 4:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        data["raw_price_lines"] = lines
        save_state(chat_id, 5, data)
        send_message(chat_id, "תנאי תשלום/הערות (למשל: לא כולל מע\"מ):")
        return

    if stage == 5:
        data["payment_terms"] = text
        save_state(chat_id, 6, data)
        send_message(chat_id, "מהו הסכום הכולל? (רק מספר, בלי ₪)")
        return

    if stage == 6:
        data["total_price"] = text.replace("₪", "").replace(",", "").strip()
        send_message(chat_id, "⏳ יוצר DOCX ושולח...")

        try:
            generate_and_send_docx(chat_id, data)
        except Exception as e:
            send_message(chat_id, f"❌ שגיאה בזמן יצירת המסמך: {e}")

        clear_state(chat_id)
        return

# =========================
# Main polling loop
# =========================
def main():
    init_db()
    print(">>> הבוט רץ (raw polling). Ctrl+C לעצירה.")

    # מנקה webhook כדי ש-getUpdates יעבוד
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

                        generate_and_send_docx(chat_id, raw_data)

                    except Exception as e:
                        print(">>> ERROR while handling photo:", repr(e))
                        try:
                            send_message(chat_id, f"❌ לא הצלחתי להפיק טיוטה מהתמונה: {e}")
                        except Exception:
                            pass
                    continue

                # ===== תמונה כ-Document (קובץ) =====
                doc = message.get("document")
                if doc and (doc.get("mime_type", "").startswith("image/")):
                    try:
                        send_message(chat_id, "📎 קיבלתי תמונה כקובץ. מתעתק טקסט ומחלץ שדות...")
                        image_bytes = download_telegram_file_by_id(doc["file_id"])

                        full_text = transcribe_full_text(image_bytes)
                        raw_data = extract_fields_from_text(full_text)

                        generate_and_send_docx(chat_id, raw_data)

                    except Exception as e:
                        print(">>> ERROR while handling document-image:", repr(e))
                        try:
                            send_message(chat_id, f"❌ לא הצלחתי להפיק טיוטה מהקובץ: {e}")
                        except Exception:
                            pass
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

if __name__ == "__main__":
    main()
