import asyncio
import os
import re
import json
import base64
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from playwright.async_api import async_playwright
import uuid
import sqlite3 as _sqlite3
import time as _time

# =====================================
# App Init
# =====================================

app = FastAPI(title="Quote Engine API")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates", "html")
TENANTS_DIR = os.path.join(BASE_DIR, "tenants")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# =====================================
# Quotes DB
# =====================================

QUOTES_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db", "quotes.db")

def _init_quotes_db():
    os.makedirs(os.path.dirname(QUOTES_DB_PATH), exist_ok=True)
    con = _sqlite3.connect(QUOTES_DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS quotes (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            client_name TEXT,
            address TEXT,
            job_type TEXT,
            raw_description TEXT,
            raw_price_lines_json TEXT,
            payment_terms TEXT,
            total REAL,
            pdf_path TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    con.commit()
    con.close()

_init_quotes_db()

# =====================================
# Models
# =====================================

class QuotePayload(BaseModel):
    tenant_id: str | None = None
    client_name: str | None = None
    address: str | None = None
    job_type: str | None = None
    raw_description: str | None = None
    raw_price_lines: list[str] | None = None
    payment_terms: str | None = None

class SaveQuotePayload(BaseModel):
    tenant_id: str | None = None
    client_name: str | None = None
    address: str | None = None
    job_type: str | None = None
    raw_description: str | None = None
    raw_price_lines: list[str] | None = None
    payment_terms: str | None = None
    total: float | None = None
    pdf_path: str | None = None

# =====================================
# Helpers
# =====================================

def render_placeholders(html_text: str, data: dict) -> str:
    html_text = html_text.replace("｛", "{").replace("｝", "}")

    def repl(match):
        key = match.group(1).strip()
        return str(data.get(key, ""))

    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, html_text)


def load_tenant(tenant_id: str) -> dict:
    path = os.path.join(TENANTS_DIR, f"{tenant_id}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=400, detail=f"Unknown tenant_id: {tenant_id}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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

        pdf_bytes = await page.pdf(
            format="A4",
            print_background=True
        )

        await browser.close()
        return pdf_bytes

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

    tenant_id = payload.tenant_id or "nimrod"
    tenant = load_tenant(tenant_id)
    print("TENANT_ID =", tenant_id)
    print("TENANT_KEYS =", list(tenant.keys()))
    print("TENANTS_DIR =", TENANTS_DIR)
    print("TENANT_FILE_EXISTS =", os.path.exists(os.path.join(TENANTS_DIR, f"{tenant_id}.json")))

    # ============================
    # Build rows + totals
    # ============================

    item_rows_html = ""
    subtotal = 0

    for line in payload.raw_price_lines:
        try:
            parts = line.split("-")
            desc = parts[0].strip()
            price = float(parts[1].strip())
        except:
            desc = line
            price = 0

        subtotal += price

        item_rows_html += f"""
        <tr>
            <td>{desc}</td>
            <td style='text-align:left;'>{price:,.0f} ₪</td>
        </tr>
        """

    vat = subtotal * 0.17
    total = subtotal + vat

    # ============================
    # Logo handling (Base64)
    # ============================

    logo_filename = tenant.get("logo_file", "")
    logo_path = os.path.join(STATIC_DIR, logo_filename) if logo_filename else ""
    logo_data_uri = logo_file_to_data_uri(logo_path)
    print("TENANT_ID:", tenant_id)
    print("TENANT:", tenant)
    print("STATIC_DIR:", STATIC_DIR)
    print("LOGO_FILENAME:", logo_filename)
    print("LOGO_PATH:", logo_path)
    print("LOGO_EXISTS:", os.path.exists(logo_path))
    print("LOGO_URI_LEN:", len(logo_data_uri))
    print("LOGO_FILE:", logo_filename)
    print("LOGO_EXISTS:", os.path.exists(logo_path))
    print("LOGO_URI_LEN:", len(logo_data_uri))

    fill = {
        "BUSINESS_NAME": tenant.get("business_name", ""),
        "BUSINESS_PHONE": tenant.get("business_phone", ""),
        "BUSINESS_EMAIL": tenant.get("business_email", ""),
        "LOGO_DATA_URI": logo_data_uri,
        "CLIENT_NAME": payload.client_name or "",
        "CLIENT_CITY": payload.address or "",
        "JOB_TITLE": payload.job_type or "",
        "JOB_NOTE": payload.raw_description or "",
        "PAYMENT_TERMS_SHORT": payload.payment_terms or "",
        "ITEM_ROWS": item_rows_html,
        "SUBTOTAL": f"{subtotal:,.0f}",
        "VAT_AMOUNT": f"{vat:,.0f}",
        "TOTAL": f"{total:,.0f}",
    }

    # ============================
    # Load template
    # ============================

    template_path = os.path.join(TEMPLATES_DIR, "quote.html")

    if not os.path.exists(template_path):
        raise HTTPException(status_code=500, detail="quote.html not found")

    with open(template_path, "r", encoding="utf-8") as f:
        template_text = f.read()

    # ============================
    # Render HTML
    # ============================

    html = render_placeholders(template_text, fill)

    with open("DEBUG_rendered.html", "w", encoding="utf-8") as f:
        f.write(html)

    # ============================
    # Generate PDF
    # ============================

    pdf_bytes = await html_to_pdf_bytes(html)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=quote.pdf"},
    )


# =====================================
# Quotes CRUD routes
# =====================================

@app.post("/quotes/save")
def save_quote(payload: SaveQuotePayload):
    quote_id = str(uuid.uuid4())
    tid = payload.tenant_id or "nimrod"
    con = _sqlite3.connect(QUOTES_DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT COUNT(*) FROM quotes WHERE tenant_id=?", (tid,))
    quote_number = cur.fetchone()[0] + 1
    cur.execute("""
        INSERT INTO quotes
            (id, tenant_id, client_name, address, job_type, raw_description,
             raw_price_lines_json, payment_terms, total, pdf_path, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        quote_id, tid,
        payload.client_name, payload.address, payload.job_type, payload.raw_description,
        json.dumps(payload.raw_price_lines or [], ensure_ascii=False),
        payload.payment_terms, payload.total or 0.0, payload.pdf_path,
        int(_time.time()),
    ))
    con.commit()
    con.close()
    return {"quote_id": quote_id, "quote_number": quote_number}


@app.get("/quotes")
def list_quotes(tenant_id: str = "nimrod"):
    con = _sqlite3.connect(QUOTES_DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT id, client_name, created_at FROM quotes WHERE tenant_id=? ORDER BY created_at ASC",
        (tenant_id,),
    )
    rows = cur.fetchall()
    con.close()
    total = len(rows)
    return [
        {"id": r[0], "client_name": r[1], "created_at": r[2], "quote_number": total - i}
        for i, r in enumerate(reversed(rows))
    ]


@app.get("/quotes/{quote_id}/pdf")
def get_quote_pdf(quote_id: str):
    con = _sqlite3.connect(QUOTES_DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT pdf_path FROM quotes WHERE id=?", (quote_id,))
    row = cur.fetchone()
    con.close()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="PDF not found")
    if not os.path.exists(row[0]):
        raise HTTPException(status_code=404, detail="PDF file not found on disk")
    with open(row[0], "rb") as f:
        pdf_bytes = f.read()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=quote.pdf"},
    )


@app.get("/quotes/{quote_id}")
def get_quote(quote_id: str):
    con = _sqlite3.connect(QUOTES_DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT id, tenant_id, client_name, address, job_type, raw_description,"
        " raw_price_lines_json, payment_terms, total FROM quotes WHERE id=?",
        (quote_id,),
    )
    row = cur.fetchone()
    con.close()
    if not row:
        raise HTTPException(status_code=404, detail="Quote not found")
    raw_lines = json.loads(row[6] or "[]")
    items = []
    for line in raw_lines:
        try:
            parts = line.split("-")
            items.append({"description": parts[0].strip(), "unit_price": float(parts[1].strip())})
        except Exception:
            items.append({"description": line, "unit_price": 0.0})
    return {
        "id": row[0], "tenant_id": row[1], "client_name": row[2],
        "address": row[3], "job_type": row[4], "raw_description": row[5],
        "payment_terms": row[7], "total": row[8], "items": items,
    }
