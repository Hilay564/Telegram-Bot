import os
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

app = FastAPI(title="Quote Engine API")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

# ---------- Models ----------
class Business(BaseModel):
    name: str
    phone: str = ""
    email: str = ""

class Client(BaseModel):
    name: str
    city: str = ""
    phone: str = ""

class Job(BaseModel):
    title: str = ""
    note: str = ""

class Item(BaseModel):
    desc: str
    unit: str = "יח׳"
    qty: float = Field(1, gt=0)
    unit_price: float = Field(..., ge=0)

class QuotePDFPayload(BaseModel):
    quote_no: str
    issue_date: Optional[str] = None  # YYYY-MM-DD
    business: Business
    client: Client
    job: Job
    vat_rate: float = Field(17, ge=0, le=100)  # percent
    prices_include_vat: bool = False
    payment_terms_short: str = ""
    valid_days: int = 7
    extra_term: str = ""
    footer_note: str = ""
    doc_label: str = "הצעת מחיר"
    status: str = "טיוטה"
    items: List[Item]

# ---------- Helpers ----------
def fmt_money(n: float) -> str:
    return f"{n:,.0f}"

def build_item_rows(items: List[Item]) -> str:
    rows = []
    for idx, it in enumerate(items, start=1):
        line_total = it.qty * it.unit_price
        rows.append(
            f"""
            <tr>
              <td class="align-center">{idx}</td>
              <td>
                <div class="desc-title">{it.desc}</div>
              </td>
              <td class="align-center">{it.unit}</td>
              <td class="align-center">{it.qty:g}</td>
              <td class="align-left money">{fmt_money(line_total)} ₪</td>
            </tr>
            """.strip()
        )
    return "\n".join(rows)

def calc_totals(payload: QuotePDFPayload):
    subtotal_gross = sum(it.qty * it.unit_price for it in payload.items)
    vat = payload.vat_rate / 100.0

    if payload.prices_include_vat and vat > 0:
        # subtotal_gross כולל מע"מ → מפרקים נטו+מע"מ
        net = subtotal_gross / (1 + vat)
        vat_amount = subtotal_gross - net
        total = subtotal_gross
        subtotal = net
    else:
        subtotal = subtotal_gross
        vat_amount = subtotal * vat
        total = subtotal + vat_amount

    # עיגול לש"ח
    return round(subtotal), round(vat_amount), round(total)

async def html_to_pdf_bytes(html: str) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html, wait_until="networkidle")
        pdf = await page.pdf(format="A4", print_background=True)
        await browser.close()
        return pdf

# ---------- Routes ----------
@app.get("/ping")
def ping():
    return {"status": "ok"}

@app.post("/quote/pdf")
async def quote_pdf(payload: QuotePDFPayload):
    template_path = os.path.join(TEMPLATES_DIR, "quote.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=500, detail="Missing templates/quote.html")

    if not payload.items:
        raise HTTPException(status_code=400, detail="items must be a non-empty list")

    issue_date = payload.issue_date or datetime.now().strftime("%Y-%m-%d")

    subtotal, vat_amount, total = calc_totals(payload)
    item_rows = build_item_rows(payload.items)

    template = env.get_template("quote.html")

    vat_label = "כלול במחיר" if payload.prices_include_vat else "לא כלול במחיר"

    html = template.render(
        QUOTE_NO=payload.quote_no,
        ISSUE_DATE=issue_date,

        BUSINESS_NAME=payload.business.name,
        BUSINESS_PHONE=payload.business.phone,
        BUSINESS_EMAIL=payload.business.email,

        DOC_LABEL=payload.doc_label,
        STATUS=payload.status,

        CLIENT_NAME=payload.client.name,
        CLIENT_CITY=payload.client.city,
        CLIENT_PHONE=payload.client.phone,

        JOB_TITLE=payload.job.title,
        JOB_NOTE=payload.job.note,

        ITEM_ROWS=item_rows,

        SUBTOTAL=fmt_money(subtotal),
        VAT_RATE=int(payload.vat_rate),
        VAT_LABEL=vat_label,
        VAT_AMOUNT=fmt_money(vat_amount),
        TOTAL=fmt_money(total),

        VALID_DAYS=payload.valid_days,
        PAYMENT_TERMS_SHORT=payload.payment_terms_short,
        EXTRA_TERM=payload.extra_term or "—",
        FOOTER_NOTE=payload.footer_note or "",
    )

    pdf_bytes = await html_to_pdf_bytes(html)

    filename = f"{payload.quote_no}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'}
    )
