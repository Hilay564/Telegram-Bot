# app.py
import os
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Body, HTTPException
from fastapi.responses import Response, JSONResponse
from playwright.sync_api import sync_playwright

app = FastAPI(title="Quote Engine API", version="0.2.0")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, "templates", "quote.html")


# ========= Helpers =========

def load_template() -> str:
    if not os.path.exists(TEMPLATE_PATH):
        raise HTTPException(
            status_code=500,
            detail=f"Template not found: {TEMPLATE_PATH}. Create templates/quote.html"
        )
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return f.read()


def safe_str(x: Any) -> str:
    if x is None:
        return ""
    return str(x)


def money(n: Any) -> str:
    """Return number as string without locale formatting (stable for PDF)."""
    try:
        v = float(n)
    except Exception:
        v = 0.0
    # remove trailing .0 if integer-ish
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}"


def build_item_rows(items: List[Dict[str, Any]]) -> str:
    """
    Builds HTML rows for {{ITEM_ROWS}}.
    Expected item keys:
      - desc (str)  [required]
      - sub (str)   [optional]
      - unit (number) [optional]
      - qty (number)  [optional]
      - unit_label (str) [optional]  default 'שורה'
    """
    rows = []
    for i, it in enumerate(items, start=1):
        desc = safe_str(it.get("desc", "")).strip()
        sub = safe_str(it.get("sub", "")).strip()
        unit = it.get("unit", 0)
        qty = it.get("qty", 1)
        unit_label = safe_str(it.get("unit_label", "שורה"))

        unit_v = float(str(unit).replace(",", "")) if str(unit).strip() else 0.0
        qty_v = float(str(qty).replace(",", "")) if str(qty).strip() else 0.0
        line_sum = unit_v * qty_v

        # description cell
        desc_html = f'<div class="desc-title">{desc}</div>'
        if sub:
            desc_html += f'<div class="desc-sub">{sub}</div>'

        rows.append(f"""
<tr>
  <td class="align-center money">{i}</td>
  <td>{desc_html}</td>
  <td class="align-center">{unit_label}</td>
  <td class="align-center money">{money(qty_v)}</td>
  <td class="align-left money">{money(line_sum)} ₪</td>
</tr>
""".strip())
    return "\n".join(rows)


def compute_totals(items: List[Dict[str, Any]], vat_rate: float) -> Dict[str, str]:
    subtotal = 0.0
    for it in items:
        unit = it.get("unit", 0)
        qty = it.get("qty", 1)
        try:
            unit_v = float(str(unit).replace(",", ""))
        except Exception:
            unit_v = 0.0
        try:
            qty_v = float(str(qty).replace(",", ""))
        except Exception:
            qty_v = 0.0
        subtotal += unit_v * qty_v

    vat_amount = subtotal * vat_rate
    total = subtotal + vat_amount
    return {
        "SUBTOTAL": money(subtotal),
        "VAT_AMOUNT": money(vat_amount),
        "TOTAL": money(total),
    }


def render_placeholders(html: str, data: Dict[str, Any]) -> str:
    """
    Simple {{KEY}} replacement.
    IMPORTANT: For ITEM_ROWS pass prebuilt HTML string.
    """
    for k, v in data.items():
        html = html.replace("{{" + k + "}}", safe_str(v))
    return html


def html_to_pdf_bytes(html: str) -> bytes:
    """
    Windows-friendly: sync_playwright
    """
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.set_content(html, wait_until="networkidle")
        pdf_bytes = page.pdf(format="A4", print_background=True)
        browser.close()
    return pdf_bytes


# ========= Routes =========

@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.post("/quote/pdf-from-draft")
def quote_pdf_from_draft(payload: Dict[str, Any] = Body(...)):
    """
    Payload minimum recommended:
      QUOTE_NO, ISSUE_DATE, BUSINESS_NAME, BUSINESS_PHONE, BUSINESS_EMAIL,
      CLIENT_NAME, CLIENT_CITY, CLIENT_PHONE,
      JOB_TITLE, JOB_NOTE,
      VAT_RATE (e.g. 17), VAT_LABEL,
      VALID_DAYS, PAYMENT_TERMS_SHORT, EXTRA_TERM,
      STATUS, DOC_LABEL, FOOTER_NOTE,
      items: [{desc, sub?, unit, qty, unit_label?}]
    """
    template = load_template()

    items = payload.get("items", [])
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be a list")

    # VAT_RATE can be percent (17) or decimal (0.17)
    raw_vat = payload.get("VAT_RATE", 17)
    try:
        vat_num = float(str(raw_vat).replace("%", "").strip())
    except Exception:
        vat_num = 17.0
    vat_rate = vat_num / 100.0 if vat_num > 1 else vat_num  # 17 -> 0.17, 0.17 stays

    item_rows_html = build_item_rows(items)
    totals = compute_totals(items, vat_rate)

    # Build final fill dict
    fill = dict(payload)  # keep everything user sent
    fill["ITEM_ROWS"] = item_rows_html

    # Ensure VAT_RATE placeholder shows percent number (17)
    fill["VAT_RATE"] = str(int(round(vat_rate * 100)))

    # Fill computed totals if not explicitly provided
    fill.setdefault("SUBTOTAL", totals["SUBTOTAL"])
    fill.setdefault("VAT_AMOUNT", totals["VAT_AMOUNT"])
    fill.setdefault("TOTAL", totals["TOTAL"])

    # Render and generate PDF
    html = render_placeholders(template, fill)
    pdf_bytes = html_to_pdf_bytes(html)

    quote_no = safe_str(fill.get("QUOTE_NO", "quote")).replace(" ", "_")
    filename = f"quote_{quote_no}.pdf"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.get("/")
def root():
    return JSONResponse({"status": "ok", "docs": "/docs"})
