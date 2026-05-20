# Property-Scoped AI Platform (Docker + Pandas)

## What changed
- Switched ingestion to **Pandas** (`openpyxl` engine).
- Added **Docker Compose** stack for MySQL + API.
- Kept strict property scoping in API (`X-Property-Code` must match route property).
- Added rich response payload shape (`answer_markdown` + `ui_blocks`).

## Files
- `/docker-compose.yml`
- `/docker/Dockerfile.api`
- `/app/main.py`
- `/app/ingest.py`
- `/app/db.py`
- `/app/requirements.txt`
- `/sql/001_property_chatbot_schema.sql`

## Run everything
From `/Users/abnikahilasamy/Personal_coding/Aker_project`:

1. Start services:
```bash
docker compose up -d --build
```

2. Ingest all rent roll files:
```bash
curl -X POST http://localhost:8000/admin/ingest
```

3. Test property-scoped KPI call (`115R`):
```bash
curl -H "X-Property-Code: 115R" http://localhost:8000/properties/115R/kpis
```

If header/code mismatch, API returns `403`.

## Stop
```bash
docker compose down
```

## Notes
- MySQL schema auto-runs on container init.
- Data folder is mounted read-only into API container at `/data`.
- This is the structured-data base; RAG ingestion/retrieval can be added next as another service with metadata filtering on `property_code`.
