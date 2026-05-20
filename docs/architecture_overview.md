# Property-Scoped AI Chatbot Platform (Architecture Overview)

## Goal
Build a chatbot where every answer is constrained to a selected property code (for example `115R`), starting with structured rent-roll data and extending later to unstructured website content.

## High-Level Architecture
1. Frontend UI (later)
- Sets active `property_code` for session.
- Renders rich responses (Markdown/HTML + structured UI blocks).

2. API Gateway
- AuthN/AuthZ.
- Injects `property_code` into request context and signed token.
- Rejects requests missing property scope.

3. Orchestration Service (LangChain/LangGraph)
- Model router: runtime LLM switch (`gpt-4.1`, `gpt-4o`, Claude, etc.).
- Query router: structured SQL path vs unstructured RAG path.
- Tool execution policy: all tools require property-scoped arguments.

4. Data Services
- Structured store (MySQL): rent roll snapshots, units, and charges keyed by property code.
- Unstructured store (later): chunked docs + vector index with metadata filter `property_code`.

5. Response Composer
- Returns:
  - `answer_markdown` or `answer_html`
  - `ui_blocks` (table, kpi_card, chart_spec, comparison_view)
  - `citations` and query traces for auditability

## Property Scope Enforcement (End-to-End)
1. Session-level scope: `property_code` is selected and immutable per chat thread unless explicitly switched.
2. API-level scope: gateway adds `X-Property-Code` and signed claim.
3. Orchestrator-level scope: each tool schema includes required `property_code`; middleware validates equality with session scope.
4. SQL scope: all queries require `WHERE property_code = :active_property_code`.
5. Vector scope (later): metadata filter `{ property_code: "115R" }` is mandatory.
6. Audit log: store request_id, property_code, tool calls, and retrieved row/chunk IDs.

## Structured Data Model (MySQL)
- `properties`
- `rent_roll_snapshots`
- `rent_roll_units`
- `rent_roll_unit_charges`

Normalization choice:
- Keep one unit row per resident/unit snapshot.
- Store charge lines in child table, including explicit `Total` lines.
- Supports queries like charge mix, delinquency, expiring leases, and MoM comparisons.

## LLM Runtime Switching
Design:
- `model_registry` table or config file maps model IDs to provider adapters.
- Request includes optional `model_id`; default fallback per tenant.
- Unified prompt+tool schema so switching models does not break tool contracts.

Tradeoff:
- Better flexibility and A/B testing.
- Requires strict output normalization because providers differ in tool-calling behavior.

## Rich Response Contract
Use a typed response envelope:
```json
{
  "property_code": "115R",
  "answer_markdown": "...",
  "ui_blocks": [
    {"type":"kpi_card","title":"Occupancy","value":"94.2%"},
    {"type":"table","columns":["Unit","Balance"],"rows":[["A103",71.35]]}
  ],
  "citations": []
}
```
This keeps frontend rendering deterministic and model-agnostic.

## Assumptions
- Source files are monthly snapshots per property.
- Existing files are `.xls` extension but OpenXML content.
- PII handling/redaction policy will be defined before production release.

## Limitations (Current Phase)
- No unstructured ingestion yet.
- No frontend yet.
- Loader currently expects stable report layout.

## Next Step to Add RAG
1. Define unstructured document schema with `property_code`, `source_url`, `chunk_id`.
2. Build crawler/extractor for company site content.
3. Embed chunks and enforce metadata filtering in retrieval.
4. Extend query router confidence logic to combine SQL+RAG when needed.
