"""
app/routes/quotes.py
Routes:
    POST /quotes              — יוצר quote + שומר ב-DB + מחזיר id + quote_number
    GET  /quotes/{id}/pdf     — מחזיר PDF לפי id
    GET  /quotes/tenant/{tid} — רשימת quotes של tenant  (חייב לבוא לפני /{id}!)
    GET  /quotes/{id}         — מחזיר מידע על quote לפי id
"""
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from ..db       import create_quote, get_quote, list_quotes
from ..services import build_quote, render_quote_pdf

router = APIRouter(prefix="/quotes", tags=["quotes"])


# ── Input model ────────────────────────────────────────────────────────────────

class QuotePayload(BaseModel):
    """
    Quote data בלבד — מה שנאסף מהמשתמש בבוט.
    הגדרות tenant נטענות מה-JSON של ה-tenant.
    """
    tenant_id:        str | None = None
    template_id:      str | None = None

    client_name:      str | None = None
    client_phone:     str | None = None
    address:          str | None = None
    job_type:         str | None = None
    raw_description:  str | None = None

    raw_price_lines:  list[str] | None = None
    payment_terms:    str | None = None
    total_price:      str | None = None   # override לחישוב


# ── Routes ─────────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
async def create_quote_endpoint(payload: QuotePayload):
    """
    1. בונה quote מובנה מה-payload
    2. שומר ב-DB
    3. מחזיר quote_id + quote_number
    """
    if not payload.raw_price_lines:
        raise HTTPException(status_code=400, detail="raw_price_lines is required")

    try:
        quote_data = build_quote(payload.model_dump())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    quote_id = create_quote(quote_data)

    return {
        "quote_id":     quote_id,
        "quote_number": quote_data.get("quote_number") or "",
        "total":        quote_data["total"],
    }


@router.get("/{quote_id}/pdf")
async def get_quote_pdf(quote_id: int):
    """מחזיר PDF מוכן לפי quote_id."""
    quote = get_quote(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Quote {quote_id} not found")

    try:
        pdf_bytes = await render_quote_pdf(quote)
    except FileNotFoundError as e:
        raise HTTPException(status_code=500, detail=str(e))

    filename = f"quote_{quote.get('quote_number', quote_id)}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/tenant/{tenant_id}")
async def list_tenant_quotes(tenant_id: str, limit: int = 20):
    """מחזיר רשימת הצעות של tenant."""
    quotes = list_quotes(tenant_id, limit=limit)
    return {"tenant_id": tenant_id, "quotes": quotes, "count": len(quotes)}


@router.get("/{quote_id}")
async def get_quote_info(quote_id: int):
    """מחזיר מידע על quote לפי id."""
    quote = get_quote(quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail=f"Quote {quote_id} not found")
    # לא מחזירים את ה-_ keys הפנימיים
    return {k: v for k, v in quote.items() if not k.startswith("_")}
