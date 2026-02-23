print(">>> telegram_raw_bot התחיל לרוץ")

import os
import time
import json
import sqlite3
import requests
import win32com.client

from fill_template import fill_template

# =========================
# 1) מפתחות (ENV בלבד)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError('חסר BOT_TOKEN ב-ENV. (PowerShell: setx BOT_TOKEN "YOUR_TELEGRAM_BOT_TOKEN")')
if not GEMINI_API_KEY:
    raise RuntimeError('חסר GEMINI_API_KEY ב-ENV. (PowerShell: setx GEMINI_API_KEY "YOUR_GEMINI_KEY")')

API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

# =========================
# 2) קבצים
# =========================
TEMPLATE_FILENAME = "template.docx"

# =========================
# 3) Gemini SDK
# =========================
from google import genai
from google.genai import types

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# =========================
# Draft + State (SQLite)
# =========================
DB_PATH = "bot_state.db"

def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            chat_id INTEGER PRIMARY KEY,
            draft_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS states (
            chat_id INTEGER PRIMARY KEY,
            state TEXT,
            payload_json TEXT,
            updated_at INTEGER NOT NULL
        )
    """)
    con.commit()
    con.close()

def save_draft(chat_id: int, draft: dict):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO drafts(chat_id, draft_json, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            draft_json=excluded.draft_json,
            updated_at=excluded.updated_at
    """, (chat_id, json.dumps(draft, ensure_ascii=False), int(time.time())))
    con.commit()
    con.close()

def load_draft(chat_id: int) -> dict | None:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT draft_json FROM drafts WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None
    return json.loads(row[0])

def clear_draft(chat_id: int):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM drafts WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()

def set_state(chat_id: int, state: str | None, payload: dict | None = None):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if state is None:
        cur.execute("DELETE FROM states WHERE chat_id=?", (chat_id,))
    else:
        cur.execute("""
            INSERT INTO states(chat_id, state, payload_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                state=excluded.state,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
        """, (chat_id, state, json.dumps(payload or {}, ensure_ascii=False), int(time.time())))
    con.commit()
    con.close()

def get_state(chat_id: int) -> tuple[str | None, dict]:
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT state, payload_json FROM states WHERE chat_id=?", (chat_id,))
    row = cur.fetchone()
    con.close()
    if not row:
        return None, {}
    st = row[0]
    payload = json.loads(row[1] or "{}")
    return st, payload


# =========================
# Telegram helpers
# =========================
def send_message(chat_id: int, text: str, reply_markup: dict | None = None):
    url = f"{API_URL}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()

def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict | None = None):
    url = f"{API_URL}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup

    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()

def answer_callback_query(callback_query_id: str):
    url = f"{API_URL}/answerCallbackQuery"
    try:
        requests.post(url, json={"callback_query_id": callback_query_id}, timeout=10)
    except Exception:
        pass

def send_document(chat_id, file_path, caption=None):
    filename = os.path.basename(file_path)
    with open(file_path, "rb") as f:
        files = {"document": (filename, f)}
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        resp = requests.post(f"{API_URL}/sendDocument", data=data, files=files, timeout=120)
        resp.raise_for_status()

def download_telegram_photo_file(photo_obj) -> bytes:
    file_id = photo_obj["file_id"]
    r = requests.get(f"{API_URL}/getFile", params={"file_id": file_id}, timeout=20)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"getFile failed: {j}")

    file_path = j["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"

    img_resp = requests.get(file_url, timeout=30)
    img_resp.raise_for_status()
    return img_resp.content

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
# UI (כפתורים)
# =========================
def kb_main():
    return {
        "inline_keyboard": [
            [{"text": "✅ הפק PDF", "callback_data": "APPROVE"}],
            [{"text": "✏️ תיקון מהיר", "callback_data": "EDIT_FAST"}],
            [{"text": "❌ ביטול", "callback_data": "CANCEL"}],
        ]
    }

def kb_fast_edit():
    return {
        "inline_keyboard": [
            [{"text": "🧑‍💼 שם לקוח", "callback_data": "EDIT_CLIENT"}],
            [{"text": "🏙️ כתובת/עיר", "callback_data": "EDIT_CITY"}],
            [{"text": "💰 סה״כ", "callback_data": "EDIT_TOTAL"}],
            [{"text": "🧾 תנאי תשלום", "callback_data": "EDIT_TERMS"}],
            [{"text": "↩️ חזרה", "callback_data": "BACK_PREVIEW"}],
        ]
    }

def format_preview(draft: dict) -> str:
    client = draft.get("client_name") or "—"
    city = draft.get("address") or draft.get("city") or "—"
    lines = draft.get("raw_price_lines") or draft.get("work_lines") or []
    total = draft.get("total_price") or "—"
    terms = draft.get("payment_terms") or "—"

    show_n = 5
    shown = lines[:show_n]
    more = max(0, len(lines) - len(shown))

    lines_txt = ""
    for i, t in enumerate(shown, start=1):
        lines_txt += f"{i}) {t}\n"
    if more > 0:
        lines_txt += f"... ועוד {more}\n"

    return (
        "טיוטה מוכנה ✅\n"
        f"לקוח: <b>{client}</b> | כתובת/עיר: <b>{city}</b>\n\n"
        f"<b>סעיפים:</b> {len(lines)}\n{lines_txt}\n"
        f"<b>סה״כ:</b> {total}\n"
        f"<b>תנאי תשלום:</b> {terms}"
    )


# =========================
# Word -> PDF
# =========================
def docx_to_pdf_word(docx_path: str, pdf_path: str):
    word = win32com.client.Dispatch("Word.Application")
    word.Visible = False
    try:
        doc = word.Documents.Open(os.path.abspath(docx_path))
        doc.ExportAsFixedFormat(os.path.abspath(pdf_path), 17)  # 17 = PDF
        doc.Close(False)
    finally:
        word.Quit()

    if not os.path.exists(pdf_path) or os.path.getsize(pdf_path) < 1000:
        raise RuntimeError("המרה ל-PDF נכשלה או יצא קובץ ריק.")
    return pdf_path


# =========================
# הפקה מהטיוטה
# =========================
def generate_and_send_pdf_from_draft(chat_id: int, draft: dict):
    base_dir = os.path.dirname(os.path.abspath(__file__))
    ts = int(time.time())

    docx_name = f"הצעת_מחיר_{chat_id}_{ts}.docx"
    pdf_name  = f"הצעת_מחיר_{chat_id}_{ts}.pdf"

    template_path = os.path.join(base_dir, TEMPLATE_FILENAME)
    docx_path = os.path.join(base_dir, docx_name)
    pdf_path  = os.path.join(base_dir, pdf_name)

    # התאמה למה שה-template שלך מצפה (fill_template)
    data_for_template = {
        "client_name": draft.get("client_name", ""),
        "address": draft.get("address", "") or draft.get("city", ""),
        "job_type": draft.get("job_type", ""),
        "raw_description": draft.get("raw_description", ""),
        "raw_price_lines": draft.get("raw_price_lines", []) or draft.get("work_lines", []) or [],
        "payment_terms": draft.get("payment_terms", ""),
        "total_price": str(draft.get("total_price", "")).replace("₪", "").replace(",", "").strip(),
    }

    fill_template(template_path, docx_path, data_for_template)
    docx_to_pdf_word(docx_path, pdf_path)
    send_document(chat_id, pdf_path, caption="✅ הצעת מחיר (PDF)")


# =========================
# Preview Flow
# =========================
def create_draft_and_show_preview(chat_id: int, extracted: dict):
    # normalize fields (תומך גם ב-city וגם ב-address)
    draft = {
        "client_name": extracted.get("client_name", ""),
        "address": extracted.get("address", extracted.get("city", "")),
        "job_type": extracted.get("job_type", ""),
        "raw_description": extracted.get("raw_description", ""),
        "raw_price_lines": extracted.get("raw_price_lines", extracted.get("work_lines", [])) or [],
        "payment_terms": extracted.get("payment_terms", ""),
        "total_price": extracted.get("total_price", ""),
    }

    save_draft(chat_id, draft)

    text = format_preview(draft)
    res = send_message(chat_id, text, reply_markup=kb_main())

    msg_id = res.get("result", {}).get("message_id")
    if msg_id:
        set_state(chat_id, "PREVIEW_MSG", {"message_id": msg_id})
    else:
        set_state(chat_id, None)

def refresh_preview(chat_id: int):
    draft = load_draft(chat_id)
    if not draft:
        send_message(chat_id, "אין טיוטה פעילה. שלח תמונה או /quote כדי להתחיל.")
        return

    st, payload = get_state(chat_id)
    msg_id = payload.get("message_id")
    text = format_preview(draft)

    if st == "PREVIEW_MSG" and msg_id:
        edit_message(chat_id, msg_id, text, reply_markup=kb_main())
    else:
        res = send_message(chat_id, text, reply_markup=kb_main())
        msg_id2 = res.get("result", {}).get("message_id")
        if msg_id2:
            set_state(chat_id, "PREVIEW_MSG", {"message_id": msg_id2})


def handle_callback(chat_id: int, data: str):
    if data == "CANCEL":
        clear_draft(chat_id)
        set_state(chat_id, None)
        send_message(chat_id, "בוטל. 👍")
        return

    if data == "EDIT_FAST":
        st, payload = get_state(chat_id)
        msg_id = payload.get("message_id")
        text = "תיקון מהיר ✏️\nבחר מה לתקן:"
        if msg_id:
            edit_message(chat_id, msg_id, text, reply_markup=kb_fast_edit())
        else:
            send_message(chat_id, text, reply_markup=kb_fast_edit())
        return

    if data == "BACK_PREVIEW":
        refresh_preview(chat_id)
        return

    if data in ("EDIT_CLIENT", "EDIT_CITY", "EDIT_TOTAL", "EDIT_TERMS"):
        field_map = {
            "EDIT_CLIENT": ("client_name", "מה שם הלקוח הנכון?"),
            "EDIT_CITY": ("address", "מה הכתובת/עיר הנכונה?"),
            "EDIT_TOTAL": ("total_price", "מה הסה״כ? (אפשר גם '12000' או '₪ 12,000')"),
            "EDIT_TERMS": ("payment_terms", "מה תנאי התשלום? (למשל: שוטף+30 / מזומן / העברה)"),
        }
        field, prompt = field_map[data]
        set_state(chat_id, "WAIT_FIELD", {"field": field})
        send_message(chat_id, prompt)
        return

    if data == "APPROVE":
        draft = load_draft(chat_id)
        if not draft:
            send_message(chat_id, "אין טיוטה פעילה.")
            return
        try:
            send_message(chat_id, "⏳ מפיק PDF…")
            generate_and_send_pdf_from_draft(chat_id, draft)
        except Exception as e:
            send_message(chat_id, f"❌ שגיאה בהפקה: {e}")
        return

    send_message(chat_id, "לא זיהיתי את הפעולה. נסה שוב.")


def handle_draft_edit_text(chat_id: int, text: str):
    st, payload = get_state(chat_id)
    if st != "WAIT_FIELD":
        return False  # לא טיפלנו

    draft = load_draft(chat_id)
    if not draft:
        set_state(chat_id, None)
        send_message(chat_id, "אין טיוטה פעילה.")
        return True

    field = payload.get("field")
    if not field:
        set_state(chat_id, None)
        send_message(chat_id, "שגיאה פנימית (field חסר).")
        return True

    draft[field] = (text or "").strip()
    save_draft(chat_id, draft)

    # חוזר ל-preview
    # נשמור PREVIEW_MSG אם קיים (message_id נשמר בו)
    st2, payload2 = get_state(chat_id)
    if st2 == "WAIT_FIELD":
        # תחזיר למצב preview תוך שמירה על message_id אם יש
        msg_id = payload2.get("message_id")
        if msg_id:
            set_state(chat_id, "PREVIEW_MSG", {"message_id": msg_id})
        else:
            # אם אין msg_id, אל תתקע
            set_state(chat_id, None)

    refresh_preview(chat_id)
    return True


# =========================
# שלב 1: תעתוק מלא מהתמונה (טקסט בלבד)
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
# שלב 2: מודל שפה על טקסט בלבד -> JSON כמו /quote
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
    # ננקה קצת
    data["total_price"] = (data.get("total_price") or "").replace("₪", "").replace(",", "").strip()
    return data


# =========================
# Flow של /quote (ידני)
# =========================
quote_states = {}  # chat_id -> {"stage": int, "data": dict}

def handle_quote_flow_text(chat_id: int, text: str):
    text = (text or "").strip()

    if text == "/start":
        send_message(chat_id, "היי 👋\nשלח /quote ליצירת הצעת מחיר בטקסט.\nאו שלח תמונה של כתב יד ליצירה אוטומטית.")
        return

    if text == "/quote":
        quote_states[chat_id] = {"stage": 0, "data": {}}
        send_message(chat_id, "שם הלקוח:")
        return

    if chat_id not in quote_states:
        send_message(chat_id, "שלח /quote כדי להתחיל, או שלח תמונה של כתב יד.")
        return

    state = quote_states[chat_id]
    stage = state["stage"]
    data = state["data"]

    if stage == 0:
        data["client_name"] = text
        state["stage"] = 1
        send_message(chat_id, "כתובת העבודה:")
    elif stage == 1:
        data["address"] = text
        state["stage"] = 2
        send_message(chat_id, "סוג העבודה:")
    elif stage == 2:
        data["job_type"] = text
        state["stage"] = 3
        send_message(chat_id, "תיאור קצר של העבודה:")
    elif stage == 3:
        data["raw_description"] = text
        state["stage"] = 4
        send_message(chat_id, "כתוב כל סעיף עבודה בשורה נפרדת (סיים עם הודעה אחת שמכילה את כל הסעיפים):")
    elif stage == 4:
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        data["raw_price_lines"] = lines
        state["stage"] = 5
        send_message(chat_id, "תנאי תשלום/הערות (למשל: לא כולל מע\"מ):")
    elif stage == 5:
        data["payment_terms"] = text
        state["stage"] = 6
        send_message(chat_id, "מהו הסכום הכולל? (אפשר גם עם ₪/פסיקים, אני אנקה)")
    elif stage == 6:
        data["total_price"] = text.replace("₪", "").replace(",", "").strip()

        # במקום להפיק ישר — נעשה preview + כפתורים
        create_draft_and_show_preview(chat_id, data)
        quote_states.pop(chat_id, None)


# =========================
# Main polling loop
# =========================
def main():
    print('>>> הבוט רץ (raw polling). Ctrl+C לעצירה.')
    init_db()

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

                # ===== callbacks מכפתורים =====
                cq = update.get("callback_query")
                if cq:
                    chat_id = cq["message"]["chat"]["id"]
                    data = cq.get("data", "")
                    answer_callback_query(cq["id"])
                    handle_callback(chat_id, data)
                    continue

                message = update.get("message") or update.get("edited_message")
                if not message:
                    continue

                chat_id = message["chat"]["id"]
                print(">>> message keys:", list(message.keys()))

                # ===== תמונה כ-Photo =====
                photo_list = message.get("photo")
                if photo_list:
                    best_photo = photo_list[-1]
                    try:
                        send_message(chat_id, "📷 קיבלתי תמונה. מתעתק ומחלץ שדות…")
                        image_bytes = download_telegram_photo_file(best_photo)

                        full_text = transcribe_full_text(image_bytes)
                        raw_data = extract_fields_from_text(full_text)

                        create_draft_and_show_preview(chat_id, raw_data)
                    except Exception as e:
                        print(">>> ERROR while handling photo:", repr(e))
                        import traceback
                        traceback.print_exc()
                        try:
                            send_message(chat_id, f"❌ לא הצלחתי להפיק טיוטה מהתמונה: {e}")
                        except Exception:
                            pass
                    continue

                # ===== תמונה כ-Document (קובץ) =====
                doc = message.get("document")
                if doc and (doc.get("mime_type", "").startswith("image/")):
                    try:
                        send_message(chat_id, "📎 קיבלתי תמונה כקובץ. מתעתק ומחלץ שדות…")
                        image_bytes = download_telegram_file_by_id(doc["file_id"])

                        full_text = transcribe_full_text(image_bytes)
                        raw_data = extract_fields_from_text(full_text)

                        create_draft_and_show_preview(chat_id, raw_data)
                    except Exception as e:
                        print(">>> ERROR while handling document-image:", repr(e))
                        import traceback
                        traceback.print_exc()
                        try:
                            send_message(chat_id, f"❌ לא הצלחתי להפיק טיוטה מהקובץ: {e}")
                        except Exception:
                            pass
                    continue

                # ===== טקסט רגיל =====
                text = message.get("text")
                if text:
                    print(f">>> הודעה מ-{chat_id}: {text}")

                    # אם המשתמש באמצע תיקון מהיר (WAIT_FIELD) – נעדכן טיוטה
                    handled = handle_draft_edit_text(chat_id, text)
                    if handled:
                        continue

                    # אחרת – flow רגיל של /quote
                    handle_quote_flow_text(chat_id, text)

        except KeyboardInterrupt:
            print('>>> נעצרת ע"י המשתמש.')
            break
        except Exception as e:
            print(">>> שגיאה בלולאה:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()