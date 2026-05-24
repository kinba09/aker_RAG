import json
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
from qdrant_client.models import FieldCondition, Filter, MatchValue
from sqlalchemy import text

from app.db import get_engine
from app.observability import write_trace
from app.sql_guardrails import ALLOWED_SCHEMA, format_sql_data, validate_generated_sql
from app.sql_planner import parse_period, parse_unit_hint, sql_intent


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
    sql_plan: dict[str, Any]
    sql_data: dict[str, Any]
    rag_data: dict[str, Any]
    answer: str
    citations: list[dict[str, Any]]
    period_applied: str
    sql_provenance: dict[str, Any]


ROW_SQL_KINDS = {
    "vacant_units",
    "highest_balances",
    "unit_detail",
    "rent_by_unit",
    "deposits",
    "lease_charges",
    "expiring_next_month",
    "expiring_in_month",
    "llm_generated_validated",
}

SCALAR_SQL_KINDS = {"unit_field"}


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
    rag_intent = any(k in q for k in ["website", "amenities", "neighborhood", "school", "map", "highlight", "page", "content"])
    unit_hint = parse_unit_hint(state["question"])
    planner_intent = sql_intent(state["question"], unit_hint)
    keyword_sql_signal = any(k in q for k in ["balance", "rent", "deposit", "lease", "unit", "charge", "occupancy", "kpi", "summary", "delinquen"])
    has_sql_signal = planner_intent != "llm_sql_fallback" or keyword_sql_signal

    if rag_intent and has_sql_signal:
        return {"route": "HYBRID"}
    if rag_intent:
        return {"route": "RAG"}
    if has_sql_signal:
        return {"route": "SQL"}
    return {"route": "HYBRID"}


def _resolve_month_year(conn, property_code: str, period_mode: str, month_hint: str | None) -> str | None:
    latest = conn.execute(
        text("SELECT MAX(month_year) FROM rent_roll_snapshots WHERE property_code = :property_code"),
        {"property_code": property_code},
    ).scalar()
    if period_mode == "latest":
        return latest
    if period_mode == "month_ym":
        return month_hint
    if period_mode == "month":
        if not latest or not month_hint:
            return None
        return f"{latest[:4]}-{month_hint}"
    return None


def _build_sql_plan(state: ChatState, ym: str | None, period_mode: str, intent: str, unit_hint: str | None) -> dict[str, Any]:
    period = "all_months" if period_mode == "all_months" else (ym or "latest")
    output = "table" if intent in ROW_SQL_KINDS or intent == "llm_sql_fallback" else ("scalar" if intent in SCALAR_SQL_KINDS else "kpi")
    return {
        "route": "sql",
        "intent": intent,
        "property_code": state["property_code"],
        "time_basis": "latest_snapshot" if period_mode == "latest" else period_mode,
        "period": period,
        "output": output,
        "filters": {"unit": unit_hint} if unit_hint else {},
        "metrics": [],
    }


def _scalar_field_from_question(question: str) -> str | None:
    q = question.lower()
    if any(k in q for k in ["sq ft", "sqft", "square feet", "square footage"]):
        return "unit_sq_ft"
    if "move in" in q:
        return "move_in_date"
    if "lease expiration" in q or "lease expire" in q or "expiration date" in q:
        return "lease_expiration_date"
    if "market rent" in q:
        return "market_rent"
    if "balance" in q:
        return "balance"
    return None


def _run_template_sql(conn, intent: str, property_code: str, ym: str | None, unit_hint: str | None, question: str, period_mode: str) -> dict[str, Any] | None:
    if intent == "kpi_summary":
        if period_mode == "all_months":
            r = conn.execute(
                text(
                    """
                    SELECT COUNT(*) AS units, COALESCE(SUM(balance),0) AS total_balance, COALESCE(AVG(market_rent),0) AS avg_market_rent,
                           COALESCE(SUM(CASE WHEN balance > 0 THEN 1 ELSE 0 END),0) AS units_with_positive_balance
                    FROM rent_roll_units
                    WHERE property_code = :property_code
                    """
                ),
                {"property_code": property_code},
            ).mappings().one()
            return {"sql_kind": "kpi_summary", "period_applied": "all_months", "query_source": "template", **dict(r)}
        r = conn.execute(
            text(
                """
                SELECT COUNT(*) AS units, COALESCE(SUM(u.balance),0) AS total_balance, COALESCE(AVG(u.market_rent),0) AS avg_market_rent,
                       COALESCE(SUM(CASE WHEN u.balance > 0 THEN 1 ELSE 0 END),0) AS units_with_positive_balance
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code AND s.month_year = :month_year
                """
            ),
            {"property_code": property_code, "month_year": ym},
        ).mappings().one()
        return {"sql_kind": "kpi_summary", "period_applied": ym or "latest", "query_source": "template", **dict(r)}

    if intent == "occupancy":
        r = conn.execute(
            text(
                """
                SELECT COUNT(*) AS total_units,
                       COALESCE(SUM(CASE WHEN u.resident_name IS NOT NULL AND u.resident_name <> '' THEN 1 ELSE 0 END),0) AS occupied_units
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code AND s.month_year = :month_year
                """
            ),
            {"property_code": property_code, "month_year": ym},
        ).mappings().one()
        total = int(r["total_units"] or 0)
        occupied = int(r["occupied_units"] or 0)
        return {
            "sql_kind": "occupancy",
            "period_applied": ym or "latest",
            "query_source": "template",
            "total_units": total,
            "occupied_units": occupied,
            "occupancy_pct": (occupied / total * 100.0) if total else 0.0,
        }

    if intent == "vacant_units":
        rows = conn.execute(
            text(
                """
                SELECT u.unit, u.unit_type, u.market_rent, u.balance
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code
                  AND s.month_year = :month_year
                  AND (u.resident_name IS NULL OR u.resident_name = '')
                  AND u.unit NOT IN ('Future Residents/Applicants','Total Non Rev Units')
                ORDER BY u.unit
                LIMIT 100
                """
            ),
            {"property_code": property_code, "month_year": ym},
        ).mappings().all()
        return format_sql_data(sql_kind="vacant_units", property_code=property_code, period_applied=ym or "latest", query_source="template", rows=[dict(r) for r in rows])

    if intent == "highest_balances":
        rows = conn.execute(
            text(
                """
                SELECT u.unit, u.resident_name, u.balance, u.market_rent
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code AND s.month_year = :month_year
                ORDER BY u.balance DESC, u.unit
                LIMIT 25
                """
            ),
            {"property_code": property_code, "month_year": ym},
        ).mappings().all()
        return format_sql_data(sql_kind="highest_balances", property_code=property_code, period_applied=ym or "latest", query_source="template", rows=[dict(r) for r in rows])

    if intent == "unit_detail" and unit_hint:
        rows = conn.execute(
            text(
                """
                SELECT u.unit, u.unit_type, u.unit_sq_ft, u.resident_name, u.market_rent, u.resident_deposit, u.other_deposit,
                       u.move_in_date, u.lease_expiration_date, u.move_out_date, u.balance
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code AND s.month_year = :month_year AND UPPER(u.unit) = :unit
                LIMIT 1
                """
            ),
            {"property_code": property_code, "month_year": ym, "unit": unit_hint},
        ).mappings().all()
        return format_sql_data(sql_kind="unit_detail", property_code=property_code, period_applied=ym or "latest", query_source="template", rows=[dict(r) for r in rows])

    if intent == "unit_field" and unit_hint:
        col = _scalar_field_from_question(question)
        if not col:
            return None
        row = conn.execute(
            text(
                f"""
                SELECT u.unit, u.{col} AS value, s.month_year
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code AND s.month_year = :month_year AND UPPER(u.unit) = :unit
                LIMIT 1
                """
            ),
            {"property_code": property_code, "month_year": ym, "unit": unit_hint},
        ).mappings().first()
        return {
            "sql_kind": "unit_field",
            "property_code": property_code,
            "period_applied": ym or "latest",
            "query_source": "template",
            "field": col,
            "unit": unit_hint,
            "value": row["value"] if row else None,
        }

    if intent == "rent_by_unit":
        rows = conn.execute(
            text(
                """
                SELECT u.unit, u.unit_type, u.market_rent
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code AND s.month_year = :month_year
                ORDER BY u.unit
                LIMIT 100
                """
            ),
            {"property_code": property_code, "month_year": ym},
        ).mappings().all()
        return format_sql_data(sql_kind="rent_by_unit", property_code=property_code, period_applied=ym or "latest", query_source="template", rows=[dict(r) for r in rows])

    if intent == "deposits":
        rows = conn.execute(
            text(
                """
                SELECT u.unit, u.resident_name, u.resident_deposit, u.other_deposit
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code AND s.month_year = :month_year
                ORDER BY u.unit
                LIMIT 100
                """
            ),
            {"property_code": property_code, "month_year": ym},
        ).mappings().all()
        return format_sql_data(sql_kind="deposits", property_code=property_code, period_applied=ym or "latest", query_source="template", rows=[dict(r) for r in rows])

    if intent == "lease_charges":
        if unit_hint:
            rows = conn.execute(
                text(
                    """
                    SELECT u.unit, c.charge_code, c.amount, c.is_total
                    FROM rent_roll_unit_charges c
                    JOIN rent_roll_units u ON u.unit_row_id = c.unit_row_id
                    JOIN rent_roll_snapshots s ON s.snapshot_id = c.snapshot_id
                    WHERE c.property_code = :property_code AND s.month_year = :month_year AND UPPER(u.unit) = :unit
                    ORDER BY c.charge_code
                    LIMIT 200
                    """
                ),
                {"property_code": property_code, "month_year": ym, "unit": unit_hint},
            ).mappings().all()
        else:
            rows = conn.execute(
                text(
                    """
                    SELECT u.unit, u.resident_name, c.charge_code, c.amount, c.is_total
                    FROM rent_roll_unit_charges c
                    JOIN rent_roll_units u ON u.unit_row_id = c.unit_row_id
                    JOIN rent_roll_snapshots s ON s.snapshot_id = c.snapshot_id
                    WHERE c.property_code = :property_code AND s.month_year = :month_year
                    ORDER BY u.unit, c.charge_code
                    LIMIT 200
                    """
                ),
                {"property_code": property_code, "month_year": ym},
            ).mappings().all()
        return format_sql_data(sql_kind="lease_charges", property_code=property_code, period_applied=ym or "latest", query_source="template", rows=[dict(r) for r in rows])

    if intent == "expiring_next_month":
        rows = conn.execute(
            text(
                """
                SELECT u.unit, u.resident_name, u.market_rent, u.lease_expiration_date,
                       DATE_FORMAT(u.lease_expiration_date, '%Y-%m') AS month_year
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code
                  AND u.lease_expiration_date IS NOT NULL
                  AND DATE_FORMAT(u.lease_expiration_date, '%Y-%m') = (
                    SELECT DATE_FORMAT(
                      DATE_ADD(STR_TO_DATE(CONCAT(MAX(month_year), '-01'), '%Y-%m-%d'), INTERVAL 1 MONTH),
                      '%Y-%m'
                    )
                    FROM rent_roll_snapshots
                    WHERE property_code = :property_code
                  )
                ORDER BY u.lease_expiration_date, u.unit
                LIMIT 100
                """
            ),
            {"property_code": property_code},
        ).mappings().all()
        return format_sql_data(
            sql_kind="expiring_next_month",
            property_code=property_code,
            period_applied="next_month_from_latest_snapshot",
            query_source="template",
            rows=[dict(r) for r in rows],
        )

    if intent == "expiring_in_month":
        rows = conn.execute(
            text(
                """
                SELECT u.unit, u.resident_name, u.market_rent, u.lease_expiration_date, s.month_year
                FROM rent_roll_units u
                JOIN rent_roll_snapshots s ON s.snapshot_id = u.snapshot_id
                WHERE u.property_code = :property_code
                  AND s.month_year = :month_year
                  AND DATE_FORMAT(u.lease_expiration_date, '%Y-%m') = :month_year
                ORDER BY u.lease_expiration_date, u.unit
                LIMIT 100
                """
            ),
            {"property_code": property_code, "month_year": ym},
        ).mappings().all()
        return format_sql_data(sql_kind="expiring_in_month", property_code=property_code, period_applied=ym or "latest", query_source="template", rows=[dict(r) for r in rows])

    return None


def _llm_generate_sql(question: str, property_code: str, ym: str | None, validation_error: str | None = None) -> str:
    schema_str = "\n".join([f"- {t}({', '.join(sorted(cols))})" for t, cols in ALLOWED_SCHEMA.items()])
    repair = f"\nPrevious validation error: {validation_error}\nRewrite the SQL to fix it." if validation_error else ""
    prompt = f"""
Return ONLY one MySQL SELECT statement for structured rent-roll analytics.
Rules:
- Use only these tables/columns:
{schema_str}
- Must include: property_code = :property_code
- Never hard-code property code values.
- Use :month_year when month filtering is needed.
- Use LIMIT for row-returning queries and keep LIMIT <= 200.
- No comments, no semicolons, no SELECT *.
Question: {question}
Default month context: {ym}
{repair}
"""
    llm = _build_llm(os.getenv("SQL_ROUTER_MODEL", "gemini-3.1-flash-lite"))
    out = llm.invoke([SystemMessage(content="Generate SQL only."), HumanMessage(content=prompt)])
    raw = out.content if isinstance(out.content, str) else str(out.content)
    txt = raw.strip()
    txt = re.sub(r"^```(?:sql)?\s*", "", txt, flags=re.IGNORECASE)
    txt = re.sub(r"\s*```$", "", txt)
    return txt.strip()


def _execute_validated_sql(conn, sql: str, property_code: str, ym: str | None, period_applied: str) -> dict[str, Any]:
    params = {"property_code": property_code}
    if ":month_year" in sql:
        params["month_year"] = ym
    rows = conn.execute(text(sql), params).mappings().all()
    return format_sql_data(
        sql_kind="llm_generated_validated",
        property_code=property_code,
        period_applied=period_applied,
        query_source="llm_generated_validated",
        rows=[dict(r) for r in rows],
    )


def _sql_node(state: ChatState) -> ChatState:
    t0 = time.perf_counter()
    engine = get_engine()
    period_mode, month_hint = parse_period(state["question"])
    with engine.begin() as conn:
        ym = _resolve_month_year(conn, state["property_code"], period_mode, month_hint)
        unit_hint = parse_unit_hint(state["question"])
        intent = sql_intent(state["question"], unit_hint)
        plan = _build_sql_plan(state, ym, period_mode, intent, unit_hint)
        write_trace({"event": "sql_plan", "property_code": state["property_code"], "intent": intent, "period_applied": plan["period"], "route": "sql"})

        template_sql_data = _run_template_sql(conn, intent, state["property_code"], ym, unit_hint, state["question"], period_mode)
        if template_sql_data is not None:
            prov = {
                "source_type": "sql",
                "property_code": state["property_code"],
                "period_applied": template_sql_data.get("period_applied", ym or "latest"),
                "query_source": template_sql_data.get("query_source", "template"),
                "sql_kind": template_sql_data.get("sql_kind"),
                "row_count": int(template_sql_data.get("row_count", template_sql_data.get("count", 1))),
            }
            write_trace(
                {
                    "event": "sql_node",
                    "property_code": state["property_code"],
                    "intent": intent,
                    "sql_kind": template_sql_data.get("sql_kind"),
                    "query_source": "template",
                    "period_applied": prov["period_applied"],
                    "row_count": prov["row_count"],
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                }
            )
            return {"sql_plan": plan, "sql_data": template_sql_data, "period_applied": prov["period_applied"], "sql_provenance": prov}

        # Governed LLM-to-SQL fallback
        validation_error = None
        validated_sql = None
        for attempt in range(3):
            generated = _llm_generate_sql(state["question"], state["property_code"], ym, validation_error=validation_error)
            vr = validate_generated_sql(generated, max_limit=200)
            if vr.ok:
                validated_sql = vr.normalized_sql
                write_trace({"event": "sql_validation", "property_code": state["property_code"], "status": "ok", "attempt": attempt + 1})
                break
            validation_error = vr.reason
            write_trace({"event": "sql_validation", "property_code": state["property_code"], "status": "rejected", "attempt": attempt + 1, "reason": vr.reason})

        if validated_sql:
            sql_data = _execute_validated_sql(conn, validated_sql, state["property_code"], ym, ym or "latest")
            prov = {
                "source_type": "sql",
                "property_code": state["property_code"],
                "period_applied": sql_data["period_applied"],
                "query_source": "llm_generated_validated",
                "sql_kind": sql_data["sql_kind"],
                "row_count": sql_data["row_count"],
            }
            write_trace(
                {
                    "event": "sql_node",
                    "property_code": state["property_code"],
                    "intent": intent,
                    "sql_kind": sql_data["sql_kind"],
                    "query_source": "llm_generated_validated",
                    "period_applied": sql_data["period_applied"],
                    "row_count": sql_data["row_count"],
                    "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
                }
            )
            return {"sql_plan": plan, "sql_data": sql_data, "period_applied": sql_data["period_applied"], "sql_provenance": prov}

    # No safe SQL could be executed.
    return {
        "sql_plan": plan,
        "sql_data": {
            "sql_kind": "sql_not_supported",
            "property_code": state["property_code"],
            "period_applied": ym or "latest",
            "row_count": 0,
            "columns": [],
            "rows": [],
            "query_source": "none",
            "message": "Structured SQL intent could not be safely executed.",
        },
        "period_applied": ym or "latest",
        "sql_provenance": {
            "source_type": "sql",
            "property_code": state["property_code"],
            "period_applied": ym or "latest",
            "query_source": "none",
            "sql_kind": "sql_not_supported",
            "row_count": 0,
        },
    }


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
        citations.append(
            {
                "source_url": source_url,
                "chunk_id": chunk_id,
                "property_code": payload.get("property_code", state["property_code"]),
                "score": getattr(p, "score", None),
            }
        )

    uniq = {}
    for c in citations:
        key = (c.get("source_url"), c.get("chunk_id"))
        if key not in uniq:
            uniq[key] = c
    citations = list(uniq.values())[:5]
    snippets = snippets[:5]
    note = "RAG snippets found via vector search." if snippets else f"No RAG chunks found for property_code={state['property_code']}."
    write_trace({"event": "rag_node", "property_code": state["property_code"], "retrieved": len(citations), "latency_ms": round((time.perf_counter() - t0) * 1000, 2)})
    return {"rag_data": {"snippets": snippets, "citations": citations, "note": note}, "citations": citations}


def _synth_node(state: ChatState) -> ChatState:
    sql_data = state.get("sql_data")
    rag_data = state.get("rag_data")

    if sql_data and not rag_data:
        if sql_data.get("sql_kind") == "sql_not_supported":
            return {"answer": sql_data.get("message", "Structured SQL intent could not be safely executed.")}
        if sql_data.get("sql_kind") == "unit_field":
            val = sql_data.get("value")
            if val is None:
                return {"answer": f"No value found for unit {sql_data.get('unit')} ({sql_data.get('field')}) in {sql_data.get('period_applied')}."}
            return {"answer": f"Unit {sql_data.get('unit')} {sql_data.get('field')}: {val} ({sql_data.get('period_applied')})."}
        if sql_data.get("sql_kind") == "occupancy":
            return {"answer": f"Occupancy is {round(float(sql_data.get('occupancy_pct', 0.0)), 2)}% ({sql_data.get('occupied_units')}/{sql_data.get('total_units')}) for {sql_data.get('period_applied')}."}
        if sql_data.get("row_count") is not None:
            return {"answer": f"Found {int(sql_data.get('row_count', 0))} records for {sql_data.get('period_applied')}. The table below contains the matching rows."}
        return {"answer": "Structured SQL result ready."}

    context_lines = [
        f"Route: {state['route']}",
        f"Property: {state['property_code']}",
        f"Question: {state['question']}",
    ]
    if sql_data:
        context_lines.append(f"Deterministic or validated SQL result: {sql_data}")
    if rag_data:
        context_lines.append(f"RAG note: {rag_data.get('note', '')}")
        if rag_data.get("snippets"):
            context_lines.append("RAG snippets:\n" + "\n---\n".join(rag_data["snippets"]))

    try:
        llm = _build_llm(state["model_id"])
        msg = llm.invoke(
            [
                SystemMessage(
                    content=(
                        "You are a reporting assistant.\n"
                        "Hard rules:\n"
                        "- Never output SQL, pseudo-SQL, code blocks, or query fragments.\n"
                        "- Never invent table names, columns, rows, metrics, or values.\n"
                        "- Only summarize provided deterministic SQL output and RAG snippets.\n"
                        "- If SQL data is missing, say that structured SQL could not be executed safely."
                    )
                ),
                HumanMessage(content="\n".join(context_lines)),
            ]
        )
        answer = msg.content if isinstance(msg.content, str) else str(msg.content)
        if re.search(r"```(?:sql)?|\bselect\b.+\bfrom\b|\bwhere\b.+\b(property_code|month_year)\b", answer, flags=re.IGNORECASE | re.DOTALL):
            answer = "Structured results are summarized above. SQL text is intentionally not exposed."
    except Exception as e:
        answer = f"Route: **{state['route']}**. Property scope enforced for **{state['property_code']}**. LLM call not executed: {e}"
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
        kind = sql_data.get("sql_kind")
        if kind == "occupancy":
            ui_blocks.extend(
                [
                    {"type": "kpi_card", "title": "Occupancy %", "value": round(float(sql_data["occupancy_pct"]), 2)},
                    {"type": "kpi_card", "title": "Occupied Units", "value": int(sql_data["occupied_units"])},
                    {"type": "kpi_card", "title": "Total Units", "value": int(sql_data["total_units"])},
                ]
            )
        elif kind == "kpi_summary":
            ui_blocks.extend(
                [
                    {"type": "kpi_card", "title": "Units", "value": int(sql_data["units"])},
                    {"type": "kpi_card", "title": "Total Balance", "value": float(sql_data["total_balance"])},
                    {"type": "kpi_card", "title": "Avg Market Rent", "value": float(sql_data["avg_market_rent"])},
                    {"type": "kpi_card", "title": "Units w/ Positive Balance", "value": int(sql_data["units_with_positive_balance"])},
                ]
            )
        elif kind in ROW_SQL_KINDS:
            rows = sql_data.get("rows", [])
            columns = sql_data.get("columns") or (list(rows[0].keys()) if rows else [])
            values = [[r.get(c) for c in columns] for r in rows]
            ui_blocks.extend(
                [
                    {"type": "kpi_card", "title": "Rows", "value": int(sql_data.get("row_count", 0))},
                    {"type": "table", "columns": columns, "rows": values},
                ]
            )
        elif kind == "unit_field":
            ui_blocks.extend(
                [
                    {"type": "kpi_card", "title": "Unit", "value": sql_data.get("unit")},
                    {"type": "kpi_card", "title": sql_data.get("field", "Field"), "value": sql_data.get("value")},
                    {"type": "kpi_card", "title": "Period", "value": sql_data.get("period_applied")},
                ]
            )

    tools_used = []
    if sql_data:
        tools_used.append("sql")
    if rag_data:
        tools_used.append("rag_retrieve")

    citations = list(result.get("citations", []))
    if result.get("sql_provenance"):
        citations.append(result["sql_provenance"])

    write_trace(
        {
            "event": "chat_request",
            "property_code": result["property_code"],
            "route": result.get("route", "HYBRID"),
            "model_id": result["model_id"],
            "tools_used": tools_used,
            "latency_ms": round((time.perf_counter() - req_t0) * 1000, 2),
        }
    )

    return {
        "property_code": result["property_code"],
        "route": result.get("route", "HYBRID"),
        "model_id": result["model_id"],
        "answer_markdown": result.get("answer", ""),
        "ui_blocks": ui_blocks,
        "citations": citations,
        "debug": {"question": result["question"], "tools_used": tools_used, "sql_plan": result.get("sql_plan")},
        "period_applied": result.get("period_applied", "latest"),
    }
