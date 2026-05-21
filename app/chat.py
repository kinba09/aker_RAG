import os
import re
import time
from dataclasses import dataclass
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_ollama import OllamaEmbeddings
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from sqlalchemy import text

from app.db import get_engine
from app.observability import write_trace

SQL_SCHEMA_CONTEXT = """
Tables:
- rent_roll_snapshots(snapshot_id, property_code, as_of_date, month_year, source_file)
- rent_roll_units(unit_row_id, snapshot_id, property_code, unit, unit_type, unit_sq_ft, resident_id, resident_name, market_rent, resident_deposit, other_deposit, move_in_date, lease_expiration_date, move_out_date, balance)
- rent_roll_unit_charges(charge_row_id, unit_row_id, snapshot_id, property_code, charge_code, amount, is_total)
"""


@dataclass
class ChatRequest:
    property_code: str
    question: str
    model_id: str | None = None


class ChatState(TypedDict, total=False):
    property_code: str
    question: str
    model_id: str
    route: str
    sql_data: dict[str, Any]
    rag_data: dict[str, Any]
    answer: str
    citations: list[dict[str, Any]]
    period_applied: str
    sql_provenance: dict[str, Any]


MONTH_MAP = {
    "jan": "01", "january": "01",
    "feb": "02", "february": "02",
    "mar": "03", "march": "03",
    "apr": "04", "april": "04",
    "may": "05",
    "jun": "06", "june": "06",
    "jul": "07", "july": "07",
    "aug": "08", "august": "08",
    "sep": "09", "sept": "09", "september": "09",
    "oct": "10", "october": "10",
    "nov": "11", "november": "11",
    "dec": "12", "december": "12",
}


def _parse_period(question: str) -> tuple[str, str | None]:
    q = question.lower()
    if "all months" in q or "full year" in q or "all data" in q:
        return ("all_months", None)

    for key, mm in MONTH_MAP.items():
        if re.search(rf"\b{re.escape(key)}\b", q):
            return ("month", mm)

    m = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", q)
    if m:
        return ("month_ym", f"{m.group(1)}-{m.group(2)}")

    return ("latest", None)


def _parse_unit_hint(question: str) -> str | None:
    q = question.upper()
    m = re.search(r"\bUNIT\s+([A-Z0-9-]+)\b", q)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z]\d{2,4}[A-Z]?)\b", q)
    if m:
        return m.group(1)
    return None


def _build_llm(model_id: str):
    if model_id.startswith("gemini"):
        return ChatGoogleGenerativeAI(model=model_id, temperature=0)
    if model_id.startswith("grok"):
        return ChatOpenAI(model=model_id, temperature=0, api_key=os.getenv("XAI_API_KEY"), base_url="https://api.x.ai/v1")
    if model_id.startswith("gpt"):
        return ChatOpenAI(model=model_id, temperature=0, api_key=os.getenv("OPENAI_API_KEY"))
    raise ValueError(f"Unsupported or unconfigured model: {model_id}")


def _route_node(state: ChatState) -> ChatState:
    q = state["question"].lower()
    rag_intent = any(k in q for k in ["website", "amenities", "neighborhood", "school", "map", "highlight"])
    sql_intent = any(k in q for k in ["balance", "rent", "deposit", "lease", "unit", "charge", "occupancy", "kpi", "summary"])

    if rag_intent and sql_intent:
        route = "HYBRID"
    elif rag_intent:
        route = "RAG"
    elif sql_intent:
        route = "SQL"
    else:
        route = "HYBRID"
    return {"route": route}


def _unit_field_column(field: str) -> str | None:
    mapping = {
        "sq_ft": "unit_sq_ft",
        "move_in_date": "move_in_date",
        "lease_expiration_date": "lease_expiration_date",
        "balance": "balance",
        "market_rent": "market_rent",
    }
    return mapping.get(field)


def _sql_kind_for_question(question: str, unit_hint: str | None) -> str:
    q = question.lower()
    if "occupancy" in q:
        return "occupancy"
    if "vacant" in q:
        return "vacant_units"
    if "highest balance" in q or "top balance" in q or "delinquen" in q or "past due" in q:
        return "highest_balances"
    if re.search(r"\blease(?:s)?\s+(?:expire|expires|expiring|expiration)\s+next\s+month\b", q) or re.search(r"\b(?:expire|expires|expiring|expiration)\s+next\s+month\b", q):
        return "expiring_next_month"
    if "expire" in q and "month" in q:
        return "expiring_in_month"
    if "charge" in q:
        return "lease_charges"
    if "deposit" in q:
        return "deposits"
    if "rent by unit" in q or ("rent" in q and "unit" in q and not unit_hint):
        return "rent_by_unit"
    if unit_hint and any(k in q for k in ["sq ft", "sqft", "square feet", "square footage", "move in", "lease expiration", "market rent", "balance"]):
        return "unit_field"
    if unit_hint:
        return "unit_detail"
    return "kpi_summary"


def _sql_node(state: ChatState) -> ChatState:
    t0 = time.perf_counter()
    engine = get_engine()
    period_mode, month_hint = _parse_period(state["question"])
    q = state["question"].lower()
    with engine.begin() as conn:
        if period_mode == "all_months":
            row = conn.execute(text("""
                SELECT
                    COUNT(*) AS units,
                    COALESCE(SUM(balance),0) AS total_balance,
                    COALESCE(AVG(market_rent),0) AS avg_market_rent,
                    COALESCE(SUM(CASE WHEN balance > 0 THEN 1 ELSE 0 END),0) AS units_with_positive_balance
                FROM rent_roll_units
                WHERE property_code = :property_code
            """), {"property_code": state["property_code"]}).mappings().one()
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "period_applied": "all_months"}
            write_trace({"event": "sql_node", "property_code": state["property_code"], "period_applied": "all_months", "latency_ms": round((time.perf_counter()-t0)*1000, 2)})
            return {"sql_data": dict(row), "period_applied": "all_months", "sql_provenance": prov}

        if period_mode == "month":
            latest_year = conn.execute(text("""
                SELECT SUBSTRING(MAX(month_year),1,4)
                FROM rent_roll_snapshots
                WHERE property_code = :property_code
            """), {"property_code": state["property_code"]}).scalar()
            ym = f"{latest_year}-{month_hint}" if latest_year else None
        elif period_mode == "month_ym":
            ym = month_hint
        else:
            ym = conn.execute(text("""
                SELECT MAX(month_year)
                FROM rent_roll_snapshots
                WHERE property_code = :property_code
            """), {"property_code": state["property_code"]}).scalar()

        snapshot_filter = "u.property_code = :property_code AND s.month_year = :month_year"
        snapshot_params = {"property_code": state["property_code"], "month_year": ym}
        unit_hint = _parse_unit_hint(state["question"])

        sql_kind = _sql_kind_for_question(state["question"], unit_hint)
        write_trace({"event": "sql_intent_classified", "property_code": state["property_code"], "sql_kind": sql_kind})

        if sql_kind == "unit_field" and unit_hint:
            field = None
            ql = q
            if any(k in ql for k in ["sq ft", "sqft", "square feet", "square footage"]):
                field = "sq_ft"
            elif "move in" in ql:
                field = "move_in_date"
            elif "lease expiration" in ql or "lease expire" in ql:
                field = "lease_expiration_date"
            elif "market rent" in ql:
                field = "market_rent"
            elif "balance" in ql:
                field = "balance"
            col = _unit_field_column(field or "")
            if col:
                row_field = conn.execute(text(f"""
                    SELECT u.unit, u.{col} AS value, s.month_year
                    FROM rent_roll_units u
                    JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                    WHERE {snapshot_filter}
                      AND UPPER(u.unit) = :unit
                    LIMIT 1
                """), {**snapshot_params, "unit": unit_hint}).mappings().first()
                prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym, "sql_kind": "unit_field", "row_count": 1 if row_field else 0}
                return {"sql_data": {"sql_kind": "unit_field", "unit": unit_hint, "field": field, "value": (row_field["value"] if row_field else None), "month_year": ym}, "period_applied": ym or "latest", "sql_provenance": prov}

        if sql_kind == "occupancy":
            row = conn.execute(text(f"""
                SELECT
                    COUNT(*) AS total_units,
                    COALESCE(SUM(CASE WHEN resident_id IS NOT NULL AND resident_id <> '' THEN 1 ELSE 0 END),0) AS occupied_units
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE {snapshot_filter}
            """), snapshot_params).mappings().one()
            total = int(row["total_units"]) if row["total_units"] else 0
            occupied = int(row["occupied_units"]) if row["occupied_units"] else 0
            occupancy = (occupied / total * 100.0) if total else 0.0
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym}
            write_trace({"event": "sql_node", "property_code": state["property_code"], "period_applied": ym or "latest", "latency_ms": round((time.perf_counter()-t0)*1000, 2)})
            return {"sql_data": {"sql_kind": "occupancy", "total_units": total, "occupied_units": occupied, "occupancy_pct": occupancy}, "period_applied": ym or "latest", "sql_provenance": prov}

        if sql_kind == "vacant_units":
            rows = conn.execute(text(f"""
                SELECT u.unit, u.unit_type, u.market_rent, u.balance
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE {snapshot_filter}
                  AND (u.resident_id IS NULL OR u.resident_id = '')
                  AND u.unit NOT IN ('Future Residents/Applicants','Total Non Rev Units')
                ORDER BY u.unit
                LIMIT 100
            """), snapshot_params).mappings().all()
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym}
            write_trace({"event": "sql_node", "property_code": state["property_code"], "period_applied": ym or "latest", "latency_ms": round((time.perf_counter()-t0)*1000, 2)})
            return {"sql_data": {"sql_kind": "vacant_units", "rows": [dict(r) for r in rows], "count": len(rows)}, "period_applied": ym or "latest", "sql_provenance": prov}

        if sql_kind == "expiring_next_month":
            rows = conn.execute(text("""
                SELECT u.unit, u.resident_name, s.month_year, u.lease_expiration_date
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code
                  AND u.lease_expiration_date IS NOT NULL
                  AND DATE_FORMAT(u.lease_expiration_date, '%Y-%m') = (
                    SELECT DATE_FORMAT(DATE_ADD(STR_TO_DATE(CONCAT(MAX(month_year), '-01'), '%Y-%m-%d'), INTERVAL 1 MONTH), '%Y-%m')
                    FROM rent_roll_snapshots
                    WHERE property_code = :property_code
                  )
                ORDER BY u.lease_expiration_date, u.unit
                LIMIT 100
            """), {"property_code": state["property_code"]}).mappings().all()
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "period_applied": "next_month_from_latest_snapshot"}
            write_trace({"event": "sql_node", "property_code": state["property_code"], "period_applied": "next_month_from_latest_snapshot", "latency_ms": round((time.perf_counter()-t0)*1000, 2)})
            return {"sql_data": {"sql_kind": "expiring_next_month", "rows": [dict(r) for r in rows], "count": len(rows)}, "period_applied": "next_month_from_latest_snapshot", "sql_provenance": prov}

        if sql_kind == "expiring_in_month":
            rows = conn.execute(text(f"""
                SELECT u.unit, u.resident_name, u.lease_expiration_date
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE {snapshot_filter}
                  AND DATE_FORMAT(u.lease_expiration_date, '%Y-%m') = :month_year
                ORDER BY u.lease_expiration_date, u.unit
                LIMIT 100
            """), snapshot_params).mappings().all()
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym, "sql_kind": "expiring_in_month", "row_count": len(rows)}
            return {"sql_data": {"sql_kind": "expiring_in_month", "rows": [dict(r) for r in rows], "count": len(rows)}, "period_applied": ym or "latest", "sql_provenance": prov}

        if sql_kind == "highest_balances":
            rows = conn.execute(text(f"""
                SELECT u.unit, u.resident_name, u.balance, u.market_rent
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE {snapshot_filter}
                ORDER BY u.balance DESC, u.unit
                LIMIT 25
            """), snapshot_params).mappings().all()
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym, "sql_kind": "highest_balances", "row_count": len(rows)}
            return {"sql_data": {"sql_kind": "highest_balances", "rows": [dict(r) for r in rows], "count": len(rows)}, "period_applied": ym or "latest", "sql_provenance": prov}

        if sql_kind == "unit_detail" and unit_hint:
            row = conn.execute(text(f"""
                SELECT u.unit, u.unit_type, u.unit_sq_ft, u.resident_id, u.resident_name, u.market_rent, u.resident_deposit, u.other_deposit, u.move_in_date, u.lease_expiration_date, u.balance
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE {snapshot_filter}
                  AND UPPER(u.unit) = :unit
                LIMIT 1
            """), {**snapshot_params, "unit": unit_hint}).mappings().first()
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym, "sql_kind": "unit_detail", "row_count": 1 if row else 0}
            return {"sql_data": {"sql_kind": "unit_detail", "rows": ([dict(row)] if row else []), "count": 1 if row else 0}, "period_applied": ym or "latest", "sql_provenance": prov}

        if sql_kind == "rent_by_unit":
            rows = conn.execute(text(f"""
                SELECT u.unit, u.unit_type, u.market_rent
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE {snapshot_filter}
                ORDER BY u.unit
                LIMIT 100
            """), snapshot_params).mappings().all()
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym, "sql_kind": "rent_by_unit", "row_count": len(rows)}
            return {"sql_data": {"sql_kind": "rent_by_unit", "rows": [dict(r) for r in rows], "count": len(rows)}, "period_applied": ym or "latest", "sql_provenance": prov}

        if sql_kind == "deposits":
            rows = conn.execute(text(f"""
                SELECT u.unit, u.resident_name, u.resident_deposit, u.other_deposit
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE {snapshot_filter}
                ORDER BY u.unit
                LIMIT 100
            """), snapshot_params).mappings().all()
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym, "sql_kind": "deposits", "row_count": len(rows)}
            return {"sql_data": {"sql_kind": "deposits", "rows": [dict(r) for r in rows], "count": len(rows)}, "period_applied": ym or "latest", "sql_provenance": prov}

        if sql_kind == "lease_charges":
            rows = conn.execute(text(f"""
                SELECT u.unit, u.resident_name, c.charge_code, c.amount, c.is_total
                FROM rent_roll_unit_charges c
                JOIN rent_roll_units u ON u.unit_row_id = c.unit_row_id
                JOIN rent_roll_snapshots s ON s.snapshot_id = c.snapshot_id
                WHERE c.property_code = :property_code
                  AND s.month_year = :month_year
                ORDER BY u.unit, c.charge_code
                LIMIT 200
            """), snapshot_params).mappings().all()
            prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym, "sql_kind": "lease_charges", "row_count": len(rows)}
            return {"sql_data": {"sql_kind": "lease_charges", "rows": [dict(r) for r in rows], "count": len(rows)}, "period_applied": ym or "latest", "sql_provenance": prov}

        row = conn.execute(text("""
            SELECT
                COUNT(*) AS units,
                COALESCE(SUM(balance),0) AS total_balance,
                COALESCE(AVG(market_rent),0) AS avg_market_rent,
                COALESCE(SUM(CASE WHEN balance > 0 THEN 1 ELSE 0 END),0) AS units_with_positive_balance
            FROM rent_roll_units u
            JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
            WHERE u.property_code = :property_code
              AND s.month_year = :month_year
        """), {"property_code": state["property_code"], "month_year": ym}).mappings().one()

    prov = {"source_type": "mysql_snapshot", "property_code": state["property_code"], "month_year": ym, "sql_kind": "kpi_summary", "row_count": 1}
    write_trace({"event": "sql_node", "property_code": state["property_code"], "period_applied": ym or "latest", "sql_mode": "fallback_deterministic", "latency_ms": round((time.perf_counter()-t0)*1000, 2)})
    return {"sql_data": dict(row), "period_applied": ym or "latest", "sql_provenance": prov}


def _rag_node(state: ChatState) -> ChatState:
    t0 = time.perf_counter()
    url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    collection = os.getenv("QDRANT_COLLECTION", "property_website_chunks")
    client = QdrantClient(url=url)
    top_k = 6

    try:
        _ = client.get_collection(collection_name=collection)
    except Exception:
        return {"rag_data": {"snippets": [], "citations": [], "note": f"Qdrant collection '{collection}' not found yet."}}

    provider = os.getenv("EMBEDDING_PROVIDER", "google").lower()
    if provider == "ollama":
        embedder = OllamaEmbeddings(
            model=os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text-v2-moe"),
            base_url=os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434"),
        )
    else:
        embed_model = os.getenv("GOOGLE_EMBEDDING_MODEL", "models/embedding-001")
        embedder = GoogleGenerativeAIEmbeddings(model=embed_model, google_api_key=os.getenv("GOOGLE_API_KEY"))
    qvec = embedder.embed_query(state["question"])
    result = client.query_points(
        collection_name=collection,
        query=qvec,
        query_filter=Filter(must=[FieldCondition(key="property_code", match=MatchValue(value=state["property_code"]))]),
        limit=top_k,
        with_payload=True,
        with_vectors=False,
    )
    points = result.points

    snippets = []
    citations = []
    for p in points:
        payload = p.payload or {}
        text_val = str(payload.get("text", ""))
        source_url = payload.get("source_url")
        chunk_id = payload.get("chunk_id")
        if text_val:
            snippets.append(text_val[:400])
        citations.append({
            "source_url": source_url,
            "chunk_id": chunk_id,
            "property_code": payload.get("property_code", state["property_code"]),
            "score": getattr(p, "score", None),
        })

    # Keep only useful unique citations
    uniq = {}
    for c in citations:
        key = (c.get("source_url"), c.get("chunk_id"))
        if key not in uniq:
            uniq[key] = c
    citations = list(uniq.values())[:5]
    snippets = snippets[:5]
    note = "RAG snippets found via vector search." if snippets else f"No RAG chunks found for property_code={state['property_code']}."
    write_trace({"event": "rag_node", "property_code": state["property_code"], "retrieved": len(citations), "latency_ms": round((time.perf_counter()-t0)*1000, 2)})
    return {"rag_data": {"snippets": snippets, "citations": citations, "note": note}, "citations": citations}


def _synth_node(state: ChatState) -> ChatState:
    sql_data = state.get("sql_data")
    rag_data = state.get("rag_data")
    if sql_data:
        kind = sql_data.get("sql_kind", "kpis")
        if kind == "unit_field":
            field = sql_data.get("field")
            value = sql_data.get("value")
            unit = sql_data.get("unit")
            month = sql_data.get("month_year")
            if value is None:
                return {"answer": f"No {field} row found for unit {unit} in {month}."}
            return {"answer": f"Unit {unit} {field} for {month}: {value}."}
        if kind in {"vacant_units", "expiring_next_month", "expiring_in_month", "highest_balances", "unit_detail", "rent_by_unit", "deposits", "lease_charges"}:
            return {"answer": f"Found {int(sql_data.get('count', 0))} rows."}

    context_lines = [
        f"Route: {state['route']}",
        f"Property: {state['property_code']}",
        f"Question: {state['question']}",
    ]
    if sql_data:
        kind = sql_data.get("sql_kind", "kpis")
        context_lines.append(f"SQL result kind: {kind}")
        context_lines.append(f"Deterministic SQL result: {sql_data}")
    if rag_data:
        context_lines.append(f"RAG note: {rag_data.get('note', '')}")
        if rag_data.get("snippets"):
            context_lines.append("RAG snippets:\n" + "\n---\n".join(rag_data["snippets"]))

    try:
        llm = _build_llm(state["model_id"])
        msg = llm.invoke([
            SystemMessage(content="You are a property analytics assistant. Only answer within the provided property scope. Keep response concise. Do not write SQL queries. Do not invent table names or columns. Only summarize the deterministic sql_data provided."),
            HumanMessage(content="\n".join(context_lines)),
        ])
        answer = msg.content if isinstance(msg.content, str) else str(msg.content)
    except Exception as e:
        answer = f"Route: **{state['route']}**. Model: **{state['model_id']}**. Property scope enforced for **{state['property_code']}**. LLM call not executed: {e}"

    return {"answer": answer}


def _next_after_route(state: ChatState) -> str:
    if state["route"] == "SQL":
        return "sql"
    if state["route"] == "RAG":
        return "rag"
    return "sql"


def _next_after_sql(state: ChatState) -> str:
    if state["route"] == "HYBRID":
        return "rag"
    return "synth"


def _next_after_rag(state: ChatState) -> str:
    return "synth"


def _build_graph():
    graph = StateGraph(ChatState)
    graph.add_node("route_node", _route_node)
    graph.add_node("sql_node", _sql_node)
    graph.add_node("rag_node", _rag_node)
    graph.add_node("synth_node", _synth_node)

    graph.set_entry_point("route_node")
    graph.add_conditional_edges("route_node", _next_after_route, {"sql": "sql_node", "rag": "rag_node"})
    graph.add_conditional_edges("sql_node", _next_after_sql, {"rag": "rag_node", "synth": "synth_node"})
    graph.add_conditional_edges("rag_node", _next_after_rag, {"synth": "synth_node"})
    graph.add_edge("synth_node", END)

    return graph.compile()


CHAT_GRAPH = _build_graph()


def run_chat(req: ChatRequest) -> dict[str, Any]:
    req_t0 = time.perf_counter()
    initial: ChatState = {
        "property_code": req.property_code.upper(),
        "question": req.question,
        "model_id": req.model_id or "gemini-3.1-flash-lite",
        "citations": [],
    }

    # Deterministic fallback: if graph fails, return guarded minimal response.
    try:
        result = CHAT_GRAPH.invoke(initial)
    except Exception as e:
        return {
            "property_code": initial["property_code"],
            "route": "FALLBACK",
            "model_id": initial["model_id"],
            "answer_markdown": f"Chat graph failed safely: {e}",
            "ui_blocks": [],
            "citations": [],
            "debug": {"question": initial["question"], "tools_used": []},
        }

    sql_data = result.get("sql_data")
    rag_data = result.get("rag_data")
    ui_blocks = []
    if sql_data:
        kind = sql_data.get("sql_kind", "kpis")
        if kind == "unit_field":
            ui_blocks.extend([
                {"type": "kpi_card", "title": "Unit", "value": sql_data.get("unit")},
                {"type": "kpi_card", "title": str(sql_data.get("field", "Field")), "value": sql_data.get("value") if sql_data.get("value") is not None else "Not found"},
                {"type": "kpi_card", "title": "Period", "value": sql_data.get("month_year")},
            ])
        elif kind == "occupancy":
            ui_blocks.extend([
                {"type": "kpi_card", "title": "Occupancy %", "value": round(float(sql_data["occupancy_pct"]), 2)},
                {"type": "kpi_card", "title": "Occupied Units", "value": int(sql_data["occupied_units"])},
                {"type": "kpi_card", "title": "Total Units", "value": int(sql_data["total_units"])},
            ])
        elif kind in {"vacant_units", "expiring_next_month", "expiring_in_month", "highest_balances", "unit_detail", "rent_by_unit", "deposits", "lease_charges"}:
            rows = sql_data.get("rows", [])
            columns = list(rows[0].keys()) if rows else []
            values = [[r.get(c) for c in columns] for r in rows]
            ui_blocks.extend([
                {"type": "kpi_card", "title": "Rows", "value": int(sql_data.get("count", 0))},
                {"type": "table", "columns": columns, "rows": values},
            ])
        else:
            ui_blocks.extend([
                {"type": "kpi_card", "title": "Units", "value": int(sql_data["units"])},
                {"type": "kpi_card", "title": "Total Balance", "value": float(sql_data["total_balance"])},
                {"type": "kpi_card", "title": "Avg Market Rent", "value": float(sql_data["avg_market_rent"])},
                {"type": "kpi_card", "title": "Units w/ Positive Balance", "value": int(sql_data["units_with_positive_balance"])},
            ])

    tools_used = []
    if sql_data:
        tools_used.append("sql_kpis")
    if rag_data:
        tools_used.append("rag_retrieve")
    citations = list(result.get("citations", []))
    if result.get("sql_provenance"):
        citations.append(result["sql_provenance"])
    write_trace({
        "event": "chat_request",
        "property_code": result["property_code"],
        "route": result.get("route", "HYBRID"),
        "model_id": result["model_id"],
        "tools_used": tools_used,
        "latency_ms": round((time.perf_counter()-req_t0)*1000, 2),
    })

    return {
        "property_code": result["property_code"],
        "route": result.get("route", "HYBRID"),
        "model_id": result["model_id"],
        "answer_markdown": result.get("answer", ""),
        "ui_blocks": ui_blocks,
        "citations": citations,
        "debug": {"question": result["question"], "tools_used": tools_used},
        "period_applied": result.get("period_applied", "latest"),
    }
