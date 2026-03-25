import asyncio
import sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import os
import re
import json
import base64
import sqlite3
import threading
from datetime import date
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from playwright.async_api import async_playwright

# =====================================
# App Init
# =====================================

app = FastAPI(title="Quote Engine API")

BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR    = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates", "html")
TENANTS_DIR   = os.path.join(BASE_DIR, "tenants")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# =====================================
# Quote Counter DB
# =====================================

DB_DIR  = os.path.join(BASE_DIR, "db")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH = os.path.join(DB_DIR, "bot_state.db")

_counter_lock = threading.Lock()

def _init_counter_table():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS quote_counters (
            tenant_id TEXT NOT NULL,
            year      INTEGER NOT NULL,
            counter   INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tenant_id, year)
        )
    """)
    con.commit()
    con.close()

def next_quote_number(tenant_id: str) -> str:
    """
    מחזיר מספר הצעה ייחודי בפורמט: YYYY-NNN (לדוגמה 2025-007).
    counter מתאפס בתחילת כל שנה, נפרד לכל tenant.
    """
    year = date.today().year
    with _counter_lock:
        con = sqlite3.connect(DB_PATH)
        con.execute("""
            INSERT INTO quote_counters (tenant_id, year, counter)
            VALUES (?, ?, 1)
            ON CONFLICT(tenant_id, year) DO UPDATE SET counter = counter + 1
        """, (tenant_id, year))
        con.commit()
        row = con.execute(
            "SELECT counter FROM quote_counters WHERE tenant_id=? AND year=?",
            (tenant_id, year)
        ).fetchone()
        con.close()
    return f"{year}-{row[0]:03d}"

_init_counter_table()

# =====================================
# Models
# =====================================

class QuotePayload(BaseModel):
    """
    Quote data בלבד — מה שנאסף מהמשתמש בבוט.
    הגדרות tenant (template, vat וכו') נטענות מה-JSON של ה-tenant.
    template_id כאן הוא override אופציונלי (מבחירת עיצוב בבוט).
    """
    tenant_id:        str | None = None
    client_name:      str | None = None
    client_phone:     str | None = None
    address:          str | None = None
    job_type:         str | None = None
    raw_description:  str | None = None
    raw_price_lines:  list[str] | None = None
    payment_terms:    str | None = None
    total_price:      str | None = None   # סה"כ שהמשתמש הזין (override לחישוב)
    template_id:      str | None = None   # override לטמפלייט (מבחירה בבוט)


# =====================================
# Tenant loading + settings
# =====================================

DEFAULT_SETTINGS = {
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
}

def load_tenant(tenant_id: str) -> dict:
    path = os.path.join(TENANTS_DIR, f"{tenant_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail=f"Unknown tenant_id: {tenant_id}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def get_settings(tenant: dict) -> dict:
    """ממזג DEFAULT_SETTINGS עם tenant.settings — tenant מנצח."""
    merged = {**DEFAULT_SETTINGS}
    merged.update(tenant.get("settings") or {})
    return merged


# =====================================
# Helpers
# =====================================

def render_placeholders(html_text: str, data: dict) -> str:
    html_text = html_text.replace("｛", "{").replace("｝", "}")
    def repl(match):
        key = match.group(1).strip()
        return str(data.get(key, ""))
    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, html_text)

def logo_file_to_data_uri(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    ext = os.path.splitext(path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    with open(path, "rb") as img:
        b64 = base64.b64encode(img.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"

async def html_to_pdf_bytes(html: str) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html, wait_until="load")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(300)
        pdf_bytes = await page.pdf(format="A4", print_background=True)
        await browser.close()
        return pdf_bytes


# =====================================
# Quote building — לוגיקה מופרדת
# =====================================

def parse_price_lines(raw_price_lines: list[str], show_line_prices: bool) -> tuple[str, float]:
    """
    מפרסר raw_price_lines → (item_rows_html, subtotal).
    אם show_line_prices=False — מסתיר עמודות מחיר ליחידה וסה"כ שורה.
    """
    item_rows_html = ""
    subtotal = 0.0

    for index, line in enumerate(raw_price_lines, start=1):
        line = (line or "").strip()
        desc = line
        qty = 1
        unit_price = 0.0

        try:
            if "-" in line:
                left, price_str = line.rsplit("-", 1)
                price_str = price_str.strip().replace(",", "").replace("₪", "").replace(" ", "")
                unit_price = float(price_str)
                left = left.strip()
                if "-" in left:
                    desc_part, qty_str = left.rsplit("-", 1)
                    qty_str = qty_str.strip().replace(",", "").replace(" ", "")
                    if qty_str.isdigit():
                        qty = int(qty_str)
                        desc = desc_part.strip()
                    else:
                        desc = left
                else:
                    desc = left
        except Exception:
            desc = line
            qty = 1
            unit_price = 0.0

        line_total = qty * unit_price
        subtotal += line_total

        if show_line_prices:
            item_rows_html += f"""
        <tr>
            <td>{index}</td>
            <td class="desc">{desc}</td>
            <td>{qty}</td>
            <td>{unit_price:,.0f} ₪</td>
            <td class="sum">{line_total:,.0f} ₪</td>
        </tr>
        """
        else:
            # מחירים מוסתרים — מספר + תיאור + כמות בלבד
            item_rows_html += f"""
        <tr>
            <td>{index}</td>
            <td class="desc">{desc}</td>
            <td>{qty}</td>
        </tr>
        """

    return item_rows_html, subtotal


def build_totals(subtotal: float, user_total_str: str | None, settings: dict) -> dict:
    """
    מחזיר dict עם SUBTOTAL, VAT_RATE, VAT_AMOUNT, TOTAL לפי settings.
    """
    user_total_str = (user_total_str or "").replace(",", "").replace("₪", "").strip()
    try:
        user_total = float(user_total_str) if user_total_str else None
    except ValueError:
        user_total = None

    base = user_total if (user_total is not None and user_total > 0) else subtotal

    if settings["show_vat"]:
        vat_pct     = float(settings.get("vat_percent", 17))
        vat_amount  = base * vat_pct / 100
        grand_total = base + vat_amount
        return {
            "SUBTOTAL":   f"{base:,.0f}",
            "VAT_RATE":   str(int(vat_pct)),
            "VAT_AMOUNT": f"{vat_amount:,.0f}",
            "TOTAL":      f"{grand_total:,.0f}",
        }
    else:
        return {
            "SUBTOTAL":   f"{base:,.0f}",
            "VAT_RATE":   "",
            "VAT_AMOUNT": "",
            "TOTAL":      f"{base:,.0f}",
        }


def build_payment_terms_html(payment_terms_text: str | None, settings: dict) -> str:
    """
    בונה <li> רשימה.
    עדיפות: payload.payment_terms > tenant.settings.default_payment_terms.
    """
    text = (payment_terms_text or "").strip()
    if text:
        parts = [x.strip() for x in text.split(",") if x.strip()]
    else:
        parts = settings.get("default_payment_terms") or []
    return "".join(f"<li>{p}</li>" for p in parts)


# =====================================
# Routes
# =====================================

@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.post("/quote/pdf-from-draft")
async def quote_pdf_from_draft(payload: QuotePayload):

    if not payload.raw_price_lines:
        raise HTTPException(status_code=400, detail="raw_price_lines is required")

    # ── 1. Tenant + Settings ──────────────────────────────────────────
    tenant_id = payload.tenant_id or "nimrod"
    tenant    = load_tenant(tenant_id)
    settings  = get_settings(tenant)

    # template_id: override מהבוט > tenant setting > default
    template_id = (payload.template_id or settings["template_id"] or "classic").strip()

    # ── 2. Parse price lines (לפי show_line_prices) ───────────────────
    item_rows_html, subtotal = parse_price_lines(
        payload.raw_price_lines,
        show_line_prices=settings["show_line_prices"],
    )

    # ── 3. Totals (לפי show_vat) ──────────────────────────────────────
    totals = build_totals(subtotal, payload.total_price, settings)

    # ── 4. Payment terms ──────────────────────────────────────────────
    payment_terms_list = build_payment_terms_html(payload.payment_terms, settings)

    # ── 5. Logo ───────────────────────────────────────────────────────
    logo_filename = tenant.get("logo_file", "")
    logo_path     = os.path.join(STATIC_DIR, logo_filename) if logo_filename else ""
    logo_data_uri = logo_file_to_data_uri(logo_path)

    # ── 6. Meta ───────────────────────────────────────────────────────
    today      = date.today()
    issue_date = today.strftime("%d/%m/%Y")
    quote_no   = next_quote_number(tenant_id)   # counter ייחודי לכל tenant ושנה
    valid_days = settings.get("valid_days", 30)

    company_id      = tenant.get("company_id", "").strip()
    company_id_part = f"| ח.פ {company_id} " if company_id else ""

    # show_email / show_phone — שולח ריק לטמפלייט אם כבוי
    business_email = tenant.get("business_email", "") if settings["show_email"] else ""
    business_phone = tenant.get("business_phone", "") if settings["show_phone"] else ""

    # ── 7. Fill dict ──────────────────────────────────────────────────
    # רק quote data + branding. settings לא נכנסות לכאן.
    fill = {
        # Header
        "QUOTE_NO":           quote_no,
        "ISSUE_DATE":         issue_date,

        # Business branding
        "BUSINESS_NAME":      tenant.get("business_name", ""),
        "BUSINESS_PHONE":     business_phone,
        "BUSINESS_EMAIL":     business_email,
        "BUSINESS_ADDRESS":   tenant.get("business_address",
                                tenant.get("business_city",
                                tenant.get("business_area", ""))),
        "LOGO_DATA_URI":      logo_data_uri,

        # Client
        "CLIENT_NAME":        payload.client_name or "",
        "CLIENT_PHONE":       payload.client_phone or "",
        "CLIENT_ADDRESS":     payload.address or "",
        "JOB_TITLE":          payload.job_type or "",
        "WORK_DESCRIPTION":   payload.raw_description or "",

        # Table
        "ITEM_ROWS":          item_rows_html,

        # Totals
        **totals,

        # Terms & footer
        "PAYMENT_TERMS_LIST": payment_terms_list,
        "VALID_DAYS":         str(valid_days),
        "COMPANY_ID_PART":    company_id_part,
    }

    # ── 8. Load template ──────────────────────────────────────────────
    template_filename = f"quote_{template_id}.html"
    template_path     = os.path.join(TEMPLATES_DIR, template_filename)

    if not os.path.exists(template_path):
        template_path = os.path.join(TEMPLATES_DIR, "quote_classic.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=500, detail=f"Template not found: {template_filename}")

    with open(template_path, "r", encoding="utf-8") as f:
        template_text = f.read()

    # ── 9. Render + PDF ───────────────────────────────────────────────
    html      = render_placeholders(template_text, fill)
    pdf_bytes = await html_to_pdf_bytes(html)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=quote.pdf"},
    )