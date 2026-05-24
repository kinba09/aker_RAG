import re
from dataclasses import dataclass
from typing import Any


ALLOWED_SCHEMA: dict[str, set[str]] = {
    "rent_roll_snapshots": {"snapshot_id", "property_code", "as_of_date", "month_year", "source_file"},
    "rent_roll_units": {
        "unit_row_id",
        "snapshot_id",
        "property_code",
        "unit",
        "unit_type",
        "unit_sq_ft",
        "resident_id",
        "resident_name",
        "market_rent",
        "resident_deposit",
        "other_deposit",
        "move_in_date",
        "lease_expiration_date",
        "move_out_date",
        "balance",
    },
    "rent_roll_unit_charges": {
        "charge_id",
        "charge_row_id",
        "snapshot_id",
        "unit_row_id",
        "property_code",
        "charge_code",
        "amount",
        "is_total",
    },
}

BANNED_KEYWORDS = {"insert", "update", "delete", "drop", "alter", "truncate", "create", "replace", "merge"}


@dataclass
class ValidationResult:
    ok: bool
    reason: str
    normalized_sql: str | None = None


def _normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql.strip()).rstrip(";")


def _table_aliases(sql: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for m in re.finditer(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)\s+(?:as\s+)?([a-zA-Z_][\w]*)\b", sql, flags=re.IGNORECASE):
        table = m.group(1).lower()
        alias = m.group(2).lower()
        aliases[alias] = table
    return aliases


def validate_generated_sql(sql: str, max_limit: int = 200) -> ValidationResult:
    if not sql or not sql.strip():
        return ValidationResult(False, "SQL is empty")

    normalized = _normalize_sql(sql)
    low = normalized.lower()

    if not low.startswith("select "):
        return ValidationResult(False, "Only SELECT statements are allowed")
    if ";" in normalized:
        return ValidationResult(False, "Semicolons are not allowed")
    if re.search(r"--|/\*|\*/|#", normalized):
        return ValidationResult(False, "SQL comments are not allowed")
    if re.search(r"\b(information_schema|mysql)\b", low):
        return ValidationResult(False, "System schemas are not allowed")
    if re.search(r"\bselect\s+\*", low):
        return ValidationResult(False, "SELECT * is not allowed")
    if re.search(r"\bor\s+1\s*=\s*1\b", low):
        return ValidationResult(False, "Unsafe OR condition is not allowed")
    if ":property_code" not in normalized:
        return ValidationResult(False, "Query must use bound parameter :property_code")
    if re.search(r"\bproperty_code\s*=\s*['\"][^'\"]+['\"]", normalized, flags=re.IGNORECASE):
        return ValidationResult(False, "property_code cannot be hard-coded")
    if not re.search(r"\b(?:u|s|c|rent_roll_units|rent_roll_snapshots|rent_roll_unit_charges)\.property_code\s*=\s*:property_code\b", normalized, flags=re.IGNORECASE):
        return ValidationResult(False, "Query must include a real table property_code filter bound to :property_code")

    for kw in BANNED_KEYWORDS:
        if re.search(rf"\b{kw}\b", low):
            return ValidationResult(False, f"Keyword '{kw}' is not allowed")

    tables: set[str] = set()
    for m in re.finditer(r"\b(?:from|join)\s+([a-zA-Z_][\w]*)\b", normalized, flags=re.IGNORECASE):
        tables.add(m.group(1).lower())
    if not tables:
        return ValidationResult(False, "No FROM/JOIN table found")
    unknown_tables = [t for t in tables if t not in ALLOWED_SCHEMA]
    if unknown_tables:
        return ValidationResult(False, f"Disallowed table(s): {', '.join(sorted(unknown_tables))}")

    aliases = _table_aliases(normalized)
    for t in tables:
        aliases.setdefault(t, t)

    for m in re.finditer(r"\b([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)\b", normalized):
        alias = m.group(1).lower()
        col = m.group(2).lower()
        table = aliases.get(alias)
        if not table:
            continue
        if col not in ALLOWED_SCHEMA[table]:
            return ValidationResult(False, f"Disallowed column '{col}' on table '{table}'")

    lm = re.search(r"\blimit\s+(\d+)\b", low)
    if not lm:
        return ValidationResult(False, "Row-returning generated SQL must include LIMIT")
    limit_val = int(lm.group(1))
    if limit_val > max_limit:
        normalized = re.sub(r"\blimit\s+\d+\b", f"LIMIT {max_limit}", normalized, flags=re.IGNORECASE)

    return ValidationResult(True, "ok", normalized_sql=normalized)


def format_sql_data(
    *,
    sql_kind: str,
    property_code: str,
    period_applied: str,
    query_source: str,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    columns = list(rows[0].keys()) if rows else []
    return {
        "sql_kind": sql_kind,
        "property_code": property_code,
        "period_applied": period_applied,
        "row_count": len(rows),
        "columns": columns,
        "rows": rows,
        "query_source": query_source,
    }
