import asyncio
asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import os
import re
from fastapi import FastAPI, HTTPException, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from playwright.async_api import async_playwright

app = FastAPI(title="Quote Engine API")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
# חייב להיות אחרי app =
app.mount("/static", StaticFiles(directory="static"), name="static")

app = FastAPI(title="Quote Engine API")

TEMPLATES_DIR = "templates"

# ==============================
# Models
# ==============================

class QuotePayload(BaseModel):
    client_name: str | None = None
    address: str | None = None
    job_type: str | None = None
    raw_description: str | None = None
    raw_price_lines: list[str] | None = None
    payment_terms: str | None = None


# ==============================
# Helpers
# ==============================

def render_placeholders(html_text: str, data: dict) -> str:
    html_text = html_text.replace("｛", "{").replace("｝", "}")

    def repl(match):
        key = match.group(1).strip()
        return str(data.get(key, ""))  # אם לא קיים -> ריק

    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, html_text)


async def html_to_pdf_bytes(html: str) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html, wait_until="networkidle")
        pdf_bytes = await page.pdf(format="A4", print_background=True)
        await browser.close()
        return pdf_bytes


# ==============================
# Routes
# ==============================

@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.post("/quote/pdf-from-draft")
async def quote_pdf_from_draft(payload: QuotePayload):

    if not payload.raw_price_lines:
        raise HTTPException(status_code=400, detail="raw_price_lines is required")

    # ==============================
    # Build rows + totals
    # ==============================

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

    fill = {
    "BUSINESS_NAME": "העסק שלי",
    "BUSINESS_PHONE": "050-0000000",
    "BUSINESS_EMAIL": "info@business.co.il",
    "CLIENT_NAME": payload.client_name or "",
    "CLIENT_CITY": payload.address or "",
    "CLIENT_PHONE": "",
    "JOB_TITLE": payload.job_type or "",
    "JOB_NOTE": payload.raw_description or "",
    "DOC_LABEL": "הצעת מחיר",
    "STATUS": "",
    "DAYS_VALID": "7",
    "VAT_LABEL": "כולל מע\"מ",
    "PAYMENT_TERMS_SHORT": payload.payment_terms or "",
    "EXTRA_TERM": "",
    "ISSUE_DATE": "01/03/2026",
    "QUOTE_NO": "001",
    "FOOTER_NOTE": "",
    "ITEM_ROWS": item_rows_html,
    "SUBTOTAL": f"{subtotal:,.0f}",
    "VAT_AMOUNT": f"{vat:,.0f}",
    "TOTAL": f"{total:,.0f}",
    }
    # ==============================
    # Load template
    # ==============================

    template_path = os.path.join(TEMPLATES_DIR, "quote.html")

    if not os.path.exists(template_path):
        raise HTTPException(status_code=500, detail="quote.html not found")

    template_text = open(template_path, "r", encoding="utf-8").read()

    # ==============================
    # Render HTML
    # ==============================

    html = render_placeholders(template_text, fill)

    # Debug file
    with open("DEBUG_rendered.html", "w", encoding="utf-8") as f:
        f.write(html)

    # ==============================
    # Generate PDF
    # ==============================

    pdf_bytes = await html_to_pdf_bytes(html)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=quote.pdf"},
    )
