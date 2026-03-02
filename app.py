import asyncio
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import os
import re
import json
import base64
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from playwright.async_api import async_playwright

# =====================================
# App Init
# =====================================

app = FastAPI(title="Quote Engine API")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
TENANTS_DIR = os.path.join(BASE_DIR, "tenants")

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

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
