"""
app/services/quote_builder.py
ממיר payload גולמי מהבוט → מבנה נתונים מסודר של Quote + Items.
"""
import json
import os


BASE_DIR     = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TENANTS_DIR  = os.path.join(BASE_DIR, "tenants")

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


# ── Tenant helpers ────────────────────────────────────────────────────────────

def load_tenant(tenant_id: str) -> dict:
    path = os.path.join(TENANTS_DIR, f"{tenant_id}.json")
    if not os.path.exists(path):
        raise ValueError(f"Unknown tenant_id: {tenant_id}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_settings(tenant: dict) -> dict:
    merged = {**DEFAULT_SETTINGS}
    merged.update(tenant.get("settings") or {})
    return merged


# ── Line item parser ──────────────────────────────────────────────────────────

def parse_price_line(line: str) -> dict:
    """
    מפרסר שורה אחת של סעיף עבודה לפורמט structured.
    פורמטים נתמכים:
        "תיאור - מחיר"
        "תיאור - כמות - מחיר"
    מחזיר: {description, qty, unit_price, line_total}
    """
    line = (line or "").strip()
    description = line
    qty         = 1
    unit_price  = 0.0

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
                    qty         = int(qty_str)
                    description = desc_part.strip()
                else:
                    description = left
            else:
                description = left
    except Exception:
        description = line
        qty         = 1
        unit_price  = 0.0

    return {
        "description": description,
        "qty":         qty,
        "unit_price":  unit_price,
        "line_total":  round(qty * unit_price, 2),
    }


# ── Build structured quote ────────────────────────────────────────────────────

def build_quote(payload: dict) -> dict:
    """
    קולט payload גולמי מהבוט ומחזיר dict מוכן לשמירה ב-DB.

    payload keys (כולם אופציונליים חוץ מ-tenant_id):
        tenant_id, template_id,
        client_name, client_phone, address, job_type, raw_description,
        raw_price_lines  — list[str]
        payment_terms    — str
        total_price      — str  (override של חישוב)
    """
    tenant_id = payload.get("tenant_id") or "nimrod"
    tenant    = load_tenant(tenant_id)
    settings  = get_settings(tenant)

    # template override: bot > tenant setting > default
    template_id = (
        payload.get("template_id")
        or settings.get("template_id")
        or "classic"
    ).strip()

    # ── parse items ──────────────────────────────────────────────────
    raw_lines = payload.get("raw_price_lines") or []
    items     = [parse_price_line(ln) for ln in raw_lines if (ln or "").strip()]
    subtotal  = round(sum(i["line_total"] for i in items), 2)

    # total override from bot
    total_str = (payload.get("total_price") or "").replace(",", "").replace("₪", "").strip()
    try:
        user_total = float(total_str) if total_str else None
    except ValueError:
        user_total = None
    base_total = user_total if (user_total and user_total > 0) else subtotal

    # ── vat ──────────────────────────────────────────────────────────
    if settings["show_vat"]:
        vat_pct    = float(settings.get("vat_percent", 17))
        vat_amount = round(base_total * vat_pct / 100, 2)
        total      = round(base_total + vat_amount, 2)
    else:
        vat_amount = 0.0
        total      = base_total

    # ── payment terms ────────────────────────────────────────────────
    pt_text = (payload.get("payment_terms") or "").strip()
    if pt_text:
        payment_terms = pt_text
    else:
        payment_terms = ", ".join(settings.get("default_payment_terms") or [])

    return {
        "tenant_id":       tenant_id,
        "template_id":     template_id,
        "client_name":     payload.get("client_name") or "",
        "client_phone":    payload.get("client_phone") or "",
        "address":         payload.get("address") or "",
        "job_type":        payload.get("job_type") or "",
        "raw_description": payload.get("raw_description") or "",
        "items":           items,
        "subtotal":        subtotal,
        "vat_amount":      vat_amount,
        "total":           total,
        "payment_terms":   payment_terms,
        "valid_days":      int(settings.get("valid_days", 30)),
        # keep settings for PDF renderer
        "_tenant":         tenant,
        "_settings":       settings,
    }
