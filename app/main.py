from fastapi import FastAPI, Header, HTTPException, Query
from sqlalchemy import text

from app.chat import ChatRequest, run_chat
from app.db import get_engine
from app.ingest import load_all_files
from app.models import ChatIn, MODEL_REGISTRY

app = FastAPI(title='Property Scoped Chatbot API')


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
            SELECT COUNT(*) AS units, COALESCE(SUM(balance),0) AS total_balance
            FROM rent_roll_units
            WHERE property_code = :property_code
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
