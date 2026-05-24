from pathlib import Path


def test_snapshot_uniqueness_is_property_month():
    schema = Path("sql/001_property_chatbot_schema.sql").read_text(encoding="utf-8")
    assert "UNIQUE KEY uq_property_month (property_code, month_year)" in schema
    assert "uq_property_month_file" not in schema

