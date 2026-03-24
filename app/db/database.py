"""
app/db/database.py
מנהל חיבור ל-SQLite ואתחול טבלאות.
"""
import os
import sqlite3
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_DIR   = os.path.join(BASE_DIR, "db")
os.makedirs(DB_DIR, exist_ok=True)
DB_PATH  = os.path.join(DB_DIR, "app.db")

_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    """יוצר את כל הטבלאות אם לא קיימות."""
    with _lock:
        con = get_conn()
        cur = con.cursor()

        # ── quote counters (נפרד לכל tenant + שנה) ──────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quote_counters (
                tenant_id TEXT NOT NULL,
                year      INTEGER NOT NULL,
                counter   INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (tenant_id, year)
            )
        """)

        # ── quotes ───────────────────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS quotes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id      TEXT    NOT NULL,
                quote_number   TEXT    NOT NULL,
                template_id    TEXT    NOT NULL DEFAULT 'classic',

                client_name    TEXT,
                client_phone   TEXT,
                address        TEXT,
                job_type       TEXT,
                raw_description TEXT,

                items_json     TEXT    NOT NULL DEFAULT '[]',

                subtotal       REAL    NOT NULL DEFAULT 0,
                vat_amount     REAL    NOT NULL DEFAULT 0,
                total          REAL    NOT NULL DEFAULT 0,

                payment_terms  TEXT,
                valid_days     INTEGER NOT NULL DEFAULT 30,

                status         TEXT    NOT NULL DEFAULT 'draft',
                created_at     TEXT    NOT NULL
            )
        """)

        con.commit()
        con.close()
