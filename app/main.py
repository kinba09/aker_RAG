from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import text

from app.db import get_engine
from app.ingest import load_all_files

app = FastAPI(title='Property Scoped Chatbot API')


@app.get('/health')
def health():
    return {'ok': True}


@app.post('/admin/ingest')
def ingest_all():
    return load_all_files()


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
