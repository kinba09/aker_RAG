import app.chat as chat


def test_route_uses_planner_signal_for_sql():
    state = {"question": "who is moving out soon"}
    out = chat._route_node(state)  # noqa: SLF001
    assert out["route"] == "SQL"


def test_synth_blocks_sql_text(monkeypatch):
    class FakeLLM:
        def invoke(self, _messages):
            class Resp:
                content = "```sql\\nSELECT unit FROM rent_roll_units WHERE property_code = :property_code\\n```"

            return Resp()

    monkeypatch.setattr(chat, "_build_llm", lambda _model_id: FakeLLM())

    out = chat._synth_node(  # noqa: SLF001
        {
            "route": "HYBRID",
            "property_code": "115R",
            "question": "show me highlights",
            "model_id": "gemini-3.1-flash-lite",
            "rag_data": {"note": "ok", "snippets": ["snippet"]},
        }
    )
    assert out["answer"] == "Structured results are summarized above. SQL text is intentionally not exposed."

