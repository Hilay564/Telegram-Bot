"""
generate_previews.py
מריץ Playwright ומצלם screenshot לכל טמפלייט HTML.
מריץ פעם אחת בהפעלת הפרויקט (או ידנית אחרי שינוי עיצוב).

שימוש:
    python generate_previews.py
"""

import os
import sys

BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR  = os.path.join(BASE_DIR, "templates", "html")
PREVIEWS_DIR   = os.path.join(BASE_DIR, "static", "previews")

os.makedirs(PREVIEWS_DIR, exist_ok=True)

# ──────────────────────────────────────────────
# נתוני דמה לצורך ה-preview (ממלאים placeholders)
# ──────────────────────────────────────────────
DEMO_REPLACEMENTS = {
    "{{QUOTE_NO}}":           "042",
    "{{ISSUE_DATE}}":         "01/06/2025",
    "{{BUSINESS_NAME}}":      "נימרוד שיפוצים",
    "{{BUSINESS_PHONE}}":     "050-0000000",
    "{{BUSINESS_EMAIL}}":     "nimrod@example.com",
    "{{BUSINESS_ADDRESS}}":   "תל אביב",
    "{{LOGO_DATA_URI}}":      "",   # ריק — יוצג placeholder
    "{{CLIENT_NAME}}":        "ישראל ישראלי",
    "{{CLIENT_PHONE}}":       "052-1234567",
    "{{CLIENT_ADDRESS}}":     "הרצל 1, ירושלים",
    "{{JOB_TITLE}}":          "שיפוץ מטבח וסלון",
    "{{WORK_DESCRIPTION}}":   "פירוק ריצוף ישן, הנחת ריצוף חדש,\nצביעת קירות, החלפת דלתות פנים.",
    "{{ITEM_ROWS}}": """
        <tr><td>1</td><td class='desc'>פירוק ריצוף ישן</td><td>1</td><td>800 ₪</td><td class='sum'>800 ₪</td></tr>
        <tr><td>2</td><td class='desc'>הנחת ריצוף חדש 50 מ"ר</td><td>50</td><td>120 ₪</td><td class='sum'>6,000 ₪</td></tr>
        <tr><td>3</td><td class='desc'>צביעת קירות</td><td>1</td><td>3,500 ₪</td><td class='sum'>3,500 ₪</td></tr>
        <tr><td>4</td><td class='desc'>החלפת 3 דלתות פנים</td><td>3</td><td>900 ₪</td><td class='sum'>2,700 ₪</td></tr>
    """,
    "{{TOTAL}}":              "13,000",
    "{{PAYMENT_TERMS_LIST}}": "<li>30% מקדמה בחתימה</li><li>70% עם סיום העבודה</li>",
    "{{VALID_DAYS}}":         "30",
    "{{COMPANY_ID_PART}}":    "| ח.פ 123456789 ",
}


def fill_template(html: str) -> str:
    for placeholder, value in DEMO_REPLACEMENTS.items():
        html = html.replace(placeholder, value)
    return html


def generate_previews():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ Playwright לא מותקן. הרץ: pip install playwright && playwright install chromium")
        sys.exit(1)

    html_files = [f for f in os.listdir(TEMPLATES_DIR) if f.endswith(".html")]
    if not html_files:
        print("❌ לא נמצאו קבצי HTML בתיקיית templates/html/")
        sys.exit(1)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 900, "height": 1200})

        for filename in html_files:
            template_path = os.path.join(TEMPLATES_DIR, filename)
            preview_name  = filename.replace(".html", ".png")
            preview_path  = os.path.join(PREVIEWS_DIR, preview_name)

            with open(template_path, "r", encoding="utf-8") as f:
                raw_html = f.read()

            filled_html = fill_template(raw_html)

            page.set_content(filled_html, wait_until="networkidle")
            page.wait_for_timeout(800)  # מחכה לטעינת פונטים

            # מצלם רק את ה-.page div (לא כל הדף עם הרקע האפור)
            page_elem = page.query_selector(".page")
            if page_elem:
                page_elem.screenshot(path=preview_path)
            else:
                page.screenshot(path=preview_path, full_page=False)

            print(f"✅ {filename} → {preview_name}")

        browser.close()

    print(f"\n✅ סה\"כ {len(html_files)} preview נוצר בתיקייה: {PREVIEWS_DIR}")


if __name__ == "__main__":
    generate_previews()
