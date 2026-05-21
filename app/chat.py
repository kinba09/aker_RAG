import os
import re
from dataclasses import dataclass
from typing import Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from sqlalchemy import text

from app.db import get_engine


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


def _sql_node(state: ChatState) -> ChatState:
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
            return {"sql_data": dict(row), "period_applied": "all_months"}

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

        if "occupancy" in q:
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
            return {"sql_data": {"sql_kind": "occupancy", "total_units": total, "occupied_units": occupied, "occupancy_pct": occupancy}, "period_applied": ym or "latest"}

        if "vacant" in q:
            rows = conn.execute(text(f"""
                SELECT u.unit, u.unit_type, u.market_rent, u.balance
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE {snapshot_filter}
                  AND (u.resident_id IS NULL OR u.resident_id = '')
                ORDER BY u.unit
                LIMIT 100
            """), snapshot_params).mappings().all()
            return {"sql_data": {"sql_kind": "vacant_units", "rows": [dict(r) for r in rows], "count": len(rows)}, "period_applied": ym or "latest"}

        if "expire next month" in q or "expires next month" in q or "leases expire next month" in q:
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
            return {"sql_data": {"sql_kind": "expiring_next_month", "rows": [dict(r) for r in rows], "count": len(rows)}, "period_applied": "next_month_from_latest_snapshot"}

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
    return {"sql_data": dict(row), "period_applied": ym or "latest"}


def _rag_node(state: ChatState) -> ChatState:
    url = os.getenv("QDRANT_URL", "http://qdrant:6333")
    collection = os.getenv("QDRANT_COLLECTION", "property_website_chunks")
    client = QdrantClient(url=url)

    try:
        _ = client.get_collection(collection_name=collection)
    except Exception:
        return {"rag_data": {"snippets": [], "citations": [], "note": f"Qdrant collection '{collection}' not found yet."}}

    # Retrieval with mandatory property filter.
    points, _ = client.scroll(
        collection_name=collection,
        scroll_filter=Filter(must=[FieldCondition(key="property_code", match=MatchValue(value=state["property_code"]))]),
        limit=5,
        with_payload=True,
    )

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
        })

    note = "RAG snippets found." if snippets else f"No RAG chunks found for property_code={state['property_code']}."
    return {"rag_data": {"snippets": snippets, "citations": citations, "note": note}, "citations": citations}


def _synth_node(state: ChatState) -> ChatState:
    sql_data = state.get("sql_data")
    rag_data = state.get("rag_data")

    context_lines = [
        f"Route: {state['route']}",
        f"Property: {state['property_code']}",
        f"Question: {state['question']}",
    ]
    if sql_data:
        context_lines.append(f"SQL KPIs: {sql_data}")
    if rag_data:
        context_lines.append(f"RAG note: {rag_data.get('note', '')}")
        if rag_data.get("snippets"):
            context_lines.append("RAG snippets:\n" + "\n---\n".join(rag_data["snippets"]))

    try:
        llm = _build_llm(state["model_id"])
        msg = llm.invoke([
            SystemMessage(content="You are a property analytics assistant. Only answer within the provided property scope. Keep response concise."),
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
        if kind == "occupancy":
            ui_blocks.extend([
                {"type": "kpi_card", "title": "Occupancy %", "value": round(float(sql_data["occupancy_pct"]), 2)},
                {"type": "kpi_card", "title": "Occupied Units", "value": int(sql_data["occupied_units"])},
                {"type": "kpi_card", "title": "Total Units", "value": int(sql_data["total_units"])},
            ])
        elif kind in {"vacant_units", "expiring_next_month"}:
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

    return {
        "property_code": result["property_code"],
        "route": result.get("route", "HYBRID"),
        "model_id": result["model_id"],
        "answer_markdown": result.get("answer", ""),
        "ui_blocks": ui_blocks,
        "citations": result.get("citations", []),
        "debug": {"question": result["question"], "tools_used": tools_used},
        "period_applied": result.get("period_applied", "latest"),
    }
