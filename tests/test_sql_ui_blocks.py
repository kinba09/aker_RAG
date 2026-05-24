import pytest

chat = pytest.importorskip("app.chat")


def test_row_sql_produces_table_ui_block(monkeypatch):
    def fake_invoke(_initial):
        return {
            "property_code": "115R",
            "question": "which units have highest balance",
            "model_id": "gemini-3.1-flash-lite",
            "route": "SQL",
            "answer": "Found 2 records for 2025-05. The table below contains the matching rows.",
            "sql_data": {
                "sql_kind": "highest_balances",
                "property_code": "115R",
                "period_applied": "2025-05",
                "query_source": "template",
                "row_count": 2,
                "columns": ["unit", "balance"],
                "rows": [{"unit": "A101", "balance": 1200}, {"unit": "A102", "balance": 900}],
            },
            "sql_provenance": {
                "source_type": "sql",
                "property_code": "115R",
                "period_applied": "2025-05",
                "query_source": "template",
                "sql_kind": "highest_balances",
                "row_count": 2,
            },
        }

    monkeypatch.setattr(chat.CHAT_GRAPH, "invoke", fake_invoke)
    out = chat.run_chat(chat.ChatRequest(property_code="115R", question="which units have highest balance"))
    assert any(b.get("type") == "table" for b in out["ui_blocks"])
