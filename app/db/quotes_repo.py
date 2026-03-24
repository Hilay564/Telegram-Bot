"""
app/db/quotes_repo.py
כל פעולות ה-DB הקשורות ל-quotes.
"""
import json
import threading
from datetime import date, datetime

from .database import get_conn, _lock


# ── Counter ──────────────────────────────────────────────────────────────────

def next_quote_number(tenant_id: str) -> str:
    """
    מחזיר מספר הצעה ייחודי בפורמט YYYY-NNN.
    counter נפרד לכל tenant, מתאפס בתחילת שנה.
    """
    year = date.today().year
    with _lock:
        con = get_conn()
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
    return f"{year}-{row['counter']:03d}"


# ── Create ────────────────────────────────────────────────────────────────────

def create_quote(data: dict) -> int:
    """
    שומר quote חדש ב-DB.
    מחזיר את ה-id של השורה החדשה.

    data keys:
        tenant_id, template_id,
        client_name, client_phone, address, job_type, raw_description,
        items          — list of dicts: {description, qty, unit_price, line_total}
        subtotal, vat_amount, total,
        payment_terms, valid_days
    """
    quote_number = next_quote_number(data["tenant_id"])
    created_at   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    con = get_conn()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO quotes (
            tenant_id, quote_number, template_id,
            client_name, client_phone, address, job_type, raw_description,
            items_json,
            subtotal, vat_amount, total,
            payment_terms, valid_days,
            status, created_at
        ) VALUES (
            :tenant_id, :quote_number, :template_id,
            :client_name, :client_phone, :address, :job_type, :raw_description,
            :items_json,
            :subtotal, :vat_amount, :total,
            :payment_terms, :valid_days,
            'sent', :created_at
        )
    """, {
        **data,
        "quote_number": quote_number,
        "items_json":   json.dumps(data.get("items", []), ensure_ascii=False),
        "created_at":   created_at,
    })
    con.commit()
    quote_id = cur.lastrowid
    con.close()
    return quote_id


# ── Read ──────────────────────────────────────────────────────────────────────

def get_quote(quote_id: int) -> dict | None:
    """מחזיר quote לפי id, או None אם לא קיים."""
    con  = get_conn()
    row  = con.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
    con.close()
    if not row:
        return None
    d = dict(row)
    d["items"] = json.loads(d.pop("items_json", "[]"))
    return d


def list_quotes(tenant_id: str, limit: int = 20) -> list[dict]:
    """מחזיר רשימת quotes של tenant, מהחדש לישן."""
    con  = get_conn()
    rows = con.execute(
        "SELECT * FROM quotes WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
        (tenant_id, limit)
    ).fetchall()
    con.close()
    result = []
    for row in rows:
        d = dict(row)
        d["items"] = json.loads(d.pop("items_json", "[]"))
        result.append(d)
    return result
