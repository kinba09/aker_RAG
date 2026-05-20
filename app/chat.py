import os
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openai import ChatOpenAI
from qdrant_client import QdrantClient
from sqlalchemy import text

from app.db import get_engine


@dataclass
class ChatRequest:
    property_code: str
    question: str
    model_id: str | None = None


class QueryRouter:
    @staticmethod
    def route(question: str) -> str:
        q = question.lower()
        if any(k in q for k in ["website", "amenities", "neighborhood", "school", "map"]):
            return "RAG"
        if any(k in q for k in ["balance", "rent", "deposit", "lease", "unit", "charge", "occupancy"]):
            return "SQL"
        return "HYBRID"


class SqlTool:
    @staticmethod
    def kpis(property_code: str) -> dict[str, Any]:
        engine = get_engine()
        with engine.begin() as conn:
            row = conn.execute(text("""
                SELECT
                    COUNT(*) AS units,
                    COALESCE(SUM(balance),0) AS total_balance,
                    COALESCE(AVG(market_rent),0) AS avg_market_rent,
                    COALESCE(SUM(CASE WHEN balance > 0 THEN 1 ELSE 0 END),0) AS units_with_positive_balance
                FROM rent_roll_units
                WHERE property_code = :property_code
            """), {"property_code": property_code}).mappings().one()
        return dict(row)


class RagTool:
    @staticmethod
    def retrieve(property_code: str, question: str, limit: int = 5) -> dict[str, Any]:
        url = os.getenv("QDRANT_URL", "http://qdrant:6333")
        collection = os.getenv("QDRANT_COLLECTION", "property_website_chunks")
        client = QdrantClient(url=url)

        try:
            _ = client.get_collection(collection_name=collection)
        except Exception:
            return {"snippets": [], "citations": [], "note": f"Qdrant collection '{collection}' not found yet."}

        # Placeholder retrieval until embeddings pipeline is added.
        # We still enforce property metadata filter in query contract.
        return {
            "snippets": [],
            "citations": [],
            "note": f"RAG collection exists. Retrieval pipeline pending ingestion for {property_code} with metadata filter property_code={property_code}."
        }


def _build_llm(model_id: str):
    if model_id.startswith("gemini"):
        return ChatGoogleGenerativeAI(model=model_id, temperature=0)
    if model_id.startswith("grok"):
        return ChatOpenAI(model=model_id, temperature=0, api_key=os.getenv("XAI_API_KEY"), base_url="https://api.x.ai/v1")
    if model_id.startswith("gpt"):
        return ChatOpenAI(model=model_id, temperature=0, api_key=os.getenv("OPENAI_API_KEY"))
    raise ValueError(f"Unsupported or unconfigured model: {model_id}")


class ResponseComposer:
    @staticmethod
    def compose(property_code: str, route: str, question: str, model_id: str, sql_data: dict | None, rag_data: dict | None):
        ui_blocks = []
        citations = []

        if sql_data:
            ui_blocks.extend([
                {"type": "kpi_card", "title": "Units", "value": int(sql_data["units"])},
                {"type": "kpi_card", "title": "Total Balance", "value": float(sql_data["total_balance"])},
                {"type": "kpi_card", "title": "Avg Market Rent", "value": float(sql_data["avg_market_rent"])},
                {"type": "kpi_card", "title": "Units w/ Positive Balance", "value": int(sql_data["units_with_positive_balance"])},
            ])

        if rag_data and rag_data.get("citations"):
            citations.extend(rag_data["citations"])

        context_lines = [f"Route: {route}", f"Property: {property_code}", f"Question: {question}"]
        if sql_data:
            context_lines.append(f"SQL KPIs: {sql_data}")
        if rag_data:
            context_lines.append(f"RAG: {rag_data.get('note', '')}")

        try:
            llm = _build_llm(model_id)
            msg = llm.invoke([
                SystemMessage(content="You are a property analytics assistant. Only answer within the provided property scope."),
                HumanMessage(content="\n".join(context_lines)),
            ])
            answer = msg.content if isinstance(msg.content, str) else str(msg.content)
        except Exception as e:
            answer = (
                f"Route: **{route}**. Model: **{model_id}**. Property scope enforced for **{property_code}**. "
                f"LLM call not executed: {e}"
            )

        return {
            "property_code": property_code,
            "route": route,
            "model_id": model_id,
            "answer_markdown": answer,
            "ui_blocks": ui_blocks,
            "citations": citations,
            "debug": {
                "question": question,
                "tools_used": [*( ["sql_kpis"] if sql_data else [] ), *( ["rag_retrieve"] if rag_data else [] )],
            },
        }


def run_chat(req: ChatRequest) -> dict[str, Any]:
    property_code = req.property_code.upper()
    model_id = req.model_id or "gemini-3.1-flash-lite"
    route = QueryRouter.route(req.question)

    sql_data = SqlTool.kpis(property_code) if route in {"SQL", "HYBRID"} else None
    rag_data = RagTool.retrieve(property_code, req.question) if route in {"RAG", "HYBRID"} else None

    return ResponseComposer.compose(property_code, route, req.question, model_id, sql_data, rag_data)
