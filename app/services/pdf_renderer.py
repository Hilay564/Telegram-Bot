"""
app/services/pdf_renderer.py
ממיר quote dict → HTML → PDF bytes.
"""
import base64
import json
import os
import re
from datetime import date

from playwright.async_api import async_playwright


BASE_DIR      = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_DIR    = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates", "html")


# ── Helpers ───────────────────────────────────────────────────────────────────

def render_placeholders(html: str, data: dict) -> str:
    html = html.replace("｛", "{").replace("｝", "}")
    def repl(m):
        return str(data.get(m.group(1).strip(), ""))
    return re.sub(r"\{\{\s*([^}]+)\s*\}\}", repl, html)


def logo_to_data_uri(path: str) -> str:
    if not path or not os.path.exists(path):
        return ""
    ext  = os.path.splitext(path)[1].lower()
    mime = "image/png" if ext == ".png" else "image/jpeg"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    return f"data:{mime};base64,{b64}"


async def html_to_pdf(html: str) -> bytes:
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page    = await browser.new_page()
        await page.set_content(html, wait_until="load")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(300)
        pdf = await page.pdf(format="A4", print_background=True)
        await browser.close()
    return pdf


# ── Build item rows HTML ──────────────────────────────────────────────────────

def build_item_rows(items: list[dict], show_line_prices: bool) -> str:
    html = ""
    for i, item in enumerate(items, start=1):
        desc       = item.get("description", "")
        qty        = item.get("qty", 1)
        unit_price = item.get("unit_price", 0.0)
        line_total = item.get("line_total", 0.0)

        if show_line_prices:
            html += (
                f"<tr>"
                f"<td class='num'>{i}</td>"
                f"<td class='desc'>&#9656; {desc}</td>"
                f"<td class='qty'>{qty}</td>"
                f"<td class='price'>{unit_price:,.0f} ₪</td>"
                f"<td class='sum'>{line_total:,.0f} ₪</td>"
                f"</tr>"
            )
        else:
            html += (
                f"<tr>"
                f"<td class='num'>{i}</td>"
                f"<td class='desc'>&#9656; {desc}</td>"
                f"<td class='qty'>{qty}</td>"
                f"</tr>"
            )
    return html


# ── Main render function ──────────────────────────────────────────────────────

async def render_quote_pdf(quote: dict) -> bytes:
    """
    מקבל quote dict (כפי שיוצא מה-DB + _tenant/_settings)
    ומחזיר PDF bytes.
    """
    tenant   = quote.get("_tenant") or {}
    settings = quote.get("_settings") or {}

    # אם אין tenant/settings (נטען מ-DB) — טוען אותם
    if not tenant:
        tenant_id = quote.get("tenant_id", "nimrod")
        tenant_path = os.path.join(
            os.path.dirname(os.path.dirname(BASE_DIR)), "tenants",
            f"{tenant_id}.json"
        )
        # fallback: BASE_DIR/tenants
        if not os.path.exists(tenant_path):
            tenant_path = os.path.join(BASE_DIR, "tenants", f"{tenant_id}.json")
        if os.path.exists(tenant_path):
            with open(tenant_path, encoding="utf-8") as f:
                tenant = json.load(f)
        from .quote_builder import get_settings
        settings = get_settings(tenant)

    show_line_prices = settings.get("show_line_prices", True)
    show_email       = settings.get("show_email", True)
    show_phone       = settings.get("show_phone", True)

    # logo
    logo_file    = tenant.get("logo_file", "")
    logo_path    = os.path.join(STATIC_DIR, logo_file) if logo_file else ""
    logo_data_uri = logo_to_data_uri(logo_path)

    # company id
    company_id      = tenant.get("company_id", "").strip()
    company_id_part = f"| ח.פ {company_id} " if company_id else ""

    # items
    items = quote.get("items") or []
    if isinstance(items, str):
        items = json.loads(items)
    item_rows_html = build_item_rows(items, show_line_prices)

    # payment terms → <li> list
    pt = (quote.get("payment_terms") or "").strip()
    payment_terms_list = "".join(
        f"<li>{p.strip()}</li>"
        for p in pt.split(",")
        if p.strip()
    )

    # totals
    total      = float(quote.get("total", 0))
    vat_amount = float(quote.get("vat_amount", 0))
    subtotal   = float(quote.get("subtotal", 0))
    vat_rate   = str(int(settings.get("vat_percent", 17))) if settings.get("show_vat") else ""

    fill = {
        "QUOTE_NO":           quote.get("quote_number", ""),
        "ISSUE_DATE":         quote.get("created_at", date.today().strftime("%d/%m/%Y"))[:10].replace("-", "/"),

        "BUSINESS_NAME":      tenant.get("business_name", ""),
        "BUSINESS_PHONE":     tenant.get("business_phone", "") if show_phone else "",
        "BUSINESS_EMAIL":     tenant.get("business_email", "") if show_email else "",
        "BUSINESS_ADDRESS":   tenant.get("business_address",
                                tenant.get("business_city",
                                tenant.get("business_area", ""))),
        "LOGO_DATA_URI":      logo_data_uri,

        "CLIENT_NAME":        quote.get("client_name", ""),
        "CLIENT_PHONE":       quote.get("client_phone", ""),
        "CLIENT_ADDRESS":     quote.get("address", ""),
        "JOB_TITLE":          quote.get("job_type", ""),
        "WORK_DESCRIPTION":   quote.get("raw_description", ""),

        "ITEM_ROWS":          item_rows_html,

        "SUBTOTAL":           f"{subtotal:,.0f}",
        "VAT_RATE":           vat_rate,
        "VAT_AMOUNT":         f"{vat_amount:,.0f}" if vat_amount else "",
        "TOTAL":              f"{total:,.0f}",

        "PAYMENT_TERMS_LIST": payment_terms_list,
        "VALID_DAYS":         str(quote.get("valid_days", 30)),
        "COMPANY_ID_PART":    company_id_part,
    }

    # load template
    tmpl_id       = (quote.get("template_id") or "classic").strip()
    tmpl_filename = f"quote_{tmpl_id}.html"
    tmpl_path     = os.path.join(TEMPLATES_DIR, tmpl_filename)
    if not os.path.exists(tmpl_path):
        tmpl_path = os.path.join(TEMPLATES_DIR, "quote_classic.html")
    if not os.path.exists(tmpl_path):
        raise FileNotFoundError(f"Template not found: {tmpl_filename}")

    with open(tmpl_path, encoding="utf-8") as f:
        template = f.read()

    html = render_placeholders(template, fill)
    return await html_to_pdf(html)