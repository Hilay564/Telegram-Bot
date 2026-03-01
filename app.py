import os
import uuid
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from fill_template import fill_template

TEMPLATE_FILENAME = "template.docx"
OUTPUT_DIR = "output"

app = FastAPI(title="Quote Engine API")


class QuotePayload(BaseModel):
    client_name: str | None = None
    address: str | None = None
    job_type: str | None = None
    raw_description: str | None = None
    raw_price_lines: list[str] | None = None
    payment_terms: str | None = None
    total_price: str | None = None


@app.get("/ping")
def ping():
    return {"status": "ok"}


@app.post("/quote/from-json")
def quote_from_json(payload: QuotePayload):
    # בדיקות מינימום
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
