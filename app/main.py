from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from sqlalchemy import text
import os

from app.chat import ChatRequest, run_chat
from app.db import get_engine
from app.ingest import load_all_files
from app.models import ChatIn, MODEL_REGISTRY
from app.observability import read_recent

app = FastAPI(title='Property Scoped Chatbot API')
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")


@app.get('/health')
def health():
    return {'ok': True}


@app.get('/models')
def list_models():
    return {"models": [m.model_dump() for m in MODEL_REGISTRY]}


@app.post('/admin/ingest')
def ingest_all(mode: str = Query(default="skip_existing", pattern="^(skip_existing|reload)$")):
    return load_all_files(mode=mode)


@app.get('/properties/{property_code}/kpis')
def property_kpis(property_code: str, x_property_code: str = Header(default='')):
    if x_property_code.upper() != property_code.upper():
        raise HTTPException(status_code=403, detail='Property scope mismatch')

    engine = get_engine()
    with engine.begin() as conn:
        r = conn.execute(text("""
            SELECT COUNT(*) AS units, COALESCE(SUM(u.balance),0) AS total_balance
            FROM rent_roll_units u
            JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
            WHERE u.property_code = :property_code
              AND s.month_year = (
                SELECT MAX(month_year) FROM rent_roll_snapshots WHERE property_code = :property_code
              )
        """), {"property_code": property_code.upper()}).mappings().one()

    return {
        'property_code': property_code.upper(),
        'answer_markdown': f"Loaded KPI snapshot for **{property_code.upper()}**.",
        'ui_blocks': [
            {'type': 'kpi_card', 'title': 'Units', 'value': int(r['units'])},
            {'type': 'kpi_card', 'title': 'Total Balance', 'value': float(r['total_balance'])},
        ],
    }


@app.post('/chat')
def chat(payload: ChatIn, x_property_code: str = Header(default='')):
    if x_property_code.upper() != payload.property_code.upper():
        raise HTTPException(status_code=403, detail='Property scope mismatch')

    selected = payload.model_id
    if selected and selected not in {m.model_id for m in MODEL_REGISTRY}:
        raise HTTPException(status_code=400, detail=f'Unknown model_id: {selected}')

    return run_chat(ChatRequest(
        property_code=payload.property_code,
        question=payload.question,
        model_id=payload.model_id,
    ))


@app.get('/admin/chunks')
def admin_chunks(
    property_code: str = Query(..., description="Property code, e.g. 115R"),
    limit: int = Query(default=20, ge=1, le=200),
):
    qdrant_url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    collection = os.getenv("QDRANT_COLLECTION", "property_website_chunks")
    client = QdrantClient(url=qdrant_url)

    points, _ = client.scroll(
        collection_name=collection,
        scroll_filter=Filter(must=[FieldCondition(key="property_code", match=MatchValue(value=property_code.upper()))]),
        limit=limit,
        with_payload=True,
        with_vectors=False,
    )

    items = []
    for p in points:
        payload = p.payload or {}
        items.append({
            "id": p.id,
            "property_code": payload.get("property_code"),
            "source_url": payload.get("source_url"),
            "chunk_id": payload.get("chunk_id"),
            "text_preview": (payload.get("text") or "")[:300],
            "crawled_at": payload.get("crawled_at"),
        })

    return {"property_code": property_code.upper(), "count": len(items), "items": items}


@app.get('/admin/traces')
def admin_traces(limit: int = Query(default=100, ge=1, le=1000)):
    return {"count": limit, "items": read_recent(limit)}
