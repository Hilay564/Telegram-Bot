import os
import re
import uuid
from datetime import datetime
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel
from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

# אם אתה עדיין רוצה DOCX (אופציונלי)
from fill_template import fill_template

TEMPLATE_FILENAME = "template.docx"
OUTPUT_DIR = "output"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

app = FastAPI(title="Quote Engine API")

# Jinja לטעינת templates/quote.html
env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)


# =========
# Models (תואם לבוט שלך כרגע)
# =========
class QuotePayload(BaseModel):
    client_name: Optional[str] = None
    address: Optional[str] = None
    job_type: Optional[str] = None
    raw_description: Optional[str] = None
    raw_price_lines: Optional[List[str]] = None
    payment_terms: Optional[str] = None
    total_price: Optional[str] = None  # נשאר בשביל תאימות, אבל השרת לא מסתמך עליו


@app.get("/ping")
def ping():
    return {"status": "ok"}


# =========
# (אופציונלי) DOCX - נשאר כמו שהיה לך
# =========
@app.post("/quote/from-json")
def quote_from_json(payload: QuotePayload):
    if not payload.raw_price_lines or len(payload.raw_price_lines) == 0:
        raise HTTPException(
            status_code=400,
            detail="raw_price_lines is required and must be a non-empty list",
        )

    if not os.path.exists(TEMPLATE_FILENAME):
        raise HTTPException(status_code=500, detail=f"Missing {TEMPLATE_FILENAME}")

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    out_name = f"quote_{uuid.uuid4().hex[:10]}.docx"
    out_path = os.path.join(OUTPUT_DIR, out_name)

    raw_data = payload.model_dump()

    try:
        fill_template(
            template_path=TEMPLATE_FILENAME,
            output_path=out_path,
            raw_data=raw_data,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create docx: {str(e)}")

    return FileResponse(
        out_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="quote.docx",
    )


# =========
# NEW: PDF Engine (תואם לבוט שלך) → HTML שלך → PDF
# =========

def fmt_money_int(n: float) -> str:
    # התבנית שלך מוסיפה "₪" בעצמה, אז אנחנו מחזירים מספר עם פסיקים
    return f"{int(round(n)):,.0f}"


def parse_raw_lines_to_items(raw_lines: List[str]):
    """
    לוקח raw_price_lines ומחזיר items:
    - desc
    - qty (כרגע 1)
    - unit ("יח׳")
    - unit_price

    חוק: מחפשים מספר בסוף השורה. אם אין מספר -> מדלגים.
    """
    items = []
    for line in raw_lines:
        text = (line or "").strip()
        if not text:
            continue

        # מספר בסוף השורה (לדוגמה: "פירוק כללי - 2500")
        m = re.search(r"(\d[\d,]*)\s*$", text)
        if not m:
            continue

        price_str = m.group(1).replace(",", "")
        try:
            unit_price = float(price_str)
        except Exception:
            continue

        desc = re.sub(r"(\d[\d,]*)\s*$", "", text).strip()
        desc = desc.strip("-–—:|").strip()

        if not desc:
            # אם נשאר ריק, לא מכניסים סעיף
            continue

        items.append(
            {
                "desc": desc,
                "unit": "יח׳",
                "qty": 1,
                "unit_price": unit_price,
            }
        )

    return items


def calc_totals(items, vat_rate_percent: float, prices_include_vat: bool):
    subtotal_gross = sum(it["qty"] * it["unit_price"] for it in items)
    vat = float(vat_rate_percent) / 100.0

    if prices_include_vat and vat > 0:
        net = subtotal_gross / (1.0 + vat)
        vat_amount = subtotal_gross - net
        total = subtotal_gross
        subtotal = net
    else:
        subtotal = subtotal_gross
        vat_amount = subtotal * vat
        total = subtotal + vat_amount

    return round(subtotal), round(vat_amount), round(total)


def build_item_rows_html(items):
    """
    בונה HTML rows שמתאים לטבלה בתבנית שלך.
    """
    rows = []
    for idx, it in enumerate(items, start=1):
        line_total = it["qty"] * it["unit_price"]
        rows.append(
            f"""
            <tr>
              <td class="align-center">{idx}</td>
              <td>
                <div class="desc-title">{it['desc']}</div>
                <div class="desc-sub"></div>
              </td>
              <td class="align-center">{it['unit']}</td>
              <td class="align-center">{it['qty']}</td>
              <td class="align-left money">{fmt_money_int(line_total)} ₪</td>
            </tr>
            """.strip()
        )
    return "\n".join(rows)


async def html_to_pdf_bytes(html: str) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        await page.set_content(html, wait_until="networkidle")
        pdf = await page.pdf(format="A4", print_background=True)
        await browser.close()
        return pdf


@app.post("/quote/pdf-from-draft")
async def quote_pdf_from_draft(payload: QuotePayload):
    """
    endpoint שמקבל את ה-JSON הנוכחי של הבוט (draft),
    ומחזיר PDF דרך templates/quote.html.
    """

    if not payload.raw_price_lines or len(payload.raw_price_lines) == 0:
        raise HTTPException(status_code=400, detail="raw_price_lines חייב להיות רשימה לא ריקה.")

    template_path = os.path.join(TEMPLATES_DIR, "quote.html")
    if not os.path.exists(template_path):
        raise HTTPException(status_code=500, detail="חסר templates/quote.html")

    # 1) parse lines → items
    items = parse_raw_lines_to_items(payload.raw_price_lines)
    if not items:
        raise HTTPException(
            status_code=400,
            detail="לא הצלחתי לחלץ מחירים מהשורות. ודא שבסוף כל שורה יש מספר (מחיר).",
        )

    # 2) VAT logic (אפשר לשנות מאוחר)
    vat_rate = 17
    prices_include_vat = False

    subtotal, vat_amount, total = calc_totals(items, vat_rate, prices_include_vat)

    # 3) Build ITEM_ROWS
    item_rows_html = build_item_rows_html(items)

    # 4) Fill template placeholders (הקובץ שלך)
    template = env.get_template("quote.html")

    quote_no = f"Q-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:4].upper()}"
    issue_date = datetime.now().strftime("%Y-%m-%d")

    # ברירות מחדל (עד שתכניס הגדרות עסק קבועות)
    BUSINESS_NAME = "Nimrod Renovations"
    BUSINESS_PHONE = "050-0000000"
    BUSINESS_EMAIL = "info@example.com"

    # Client mapping (מהבוט)
    CLIENT_NAME = (payload.client_name or "").strip() or "—"
    CLIENT_CITY = (payload.address or "").strip() or "—"
    CLIENT_PHONE = "—"

    JOB_TITLE = (payload.job_type or "").strip() or "—"
    JOB_NOTE = (payload.raw_description or "").strip() or ""

    payment_terms_short = (payload.payment_terms or "").strip() or "—"

    vat_label = "כלול במחיר" if prices_include_vat else "לא כלול במחיר"

    html = template.render(
        QUOTE_NO=quote_no,
        ISSUE_DATE=issue_date,

        BUSINESS_NAME=BUSINESS_NAME,
        BUSINESS_PHONE=BUSINESS_PHONE,
        BUSINESS_EMAIL=BUSINESS_EMAIL,

        DOC_LABEL="הצעת מחיר",
        STATUS="טיוטה",

        CLIENT_NAME=CLIENT_NAME,
        CLIENT_CITY=CLIENT_CITY,
        CLIENT_PHONE=CLIENT_PHONE,

        JOB_TITLE=JOB_TITLE,
        JOB_NOTE=JOB_NOTE,

        ITEM_ROWS=item_rows_html,

        SUBTOTAL=fmt_money_int(subtotal),
        VAT_RATE=vat_rate,
        VAT_LABEL=vat_label,
        VAT_AMOUNT=fmt_money_int(vat_amount),
        TOTAL=fmt_money_int(total),

        VALID_DAYS=7,
        PAYMENT_TERMS_SHORT=payment_terms_short,
        EXTRA_TERM="—",
        FOOTER_NOTE="מסמך זה הופק אוטומטית",
    )

    # 5) Render to PDF
    pdf_bytes = await html_to_pdf_bytes(html)

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{quote_no}.pdf"'},
    )
