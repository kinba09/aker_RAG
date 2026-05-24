import re

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


def parse_period(question: str) -> tuple[str, str | None]:
    q = question.lower()
    if "all months" in q or "full year" in q or "all data" in q or "trend" in q:
        return ("all_months", None)
    m = re.search(r"\b(20\d{2})-(0[1-9]|1[0-2])\b", q)
    if m:
        return ("month_ym", f"{m.group(1)}-{m.group(2)}")
    m2 = re.search(
        r"\b(january|february|march|april|may|june|july|august|september|october|november|december)\s+(20\d{2})\b",
        q,
    )
    if m2:
        return ("month_ym", f"{m2.group(2)}-{MONTH_MAP[m2.group(1)]}")
    for key, mm in MONTH_MAP.items():
        if re.search(rf"\b{re.escape(key)}\b", q):
            return ("month", mm)
    return ("latest", None)


def parse_unit_hint(question: str) -> str | None:
    q = question.upper()
    m = re.search(r"\bUNIT\s+([A-Z0-9-]+)\b", q)
    if m:
        return m.group(1)
    m = re.search(r"\b([A-Z]\d{2,4}[A-Z]?)\b", q)
    if m:
        return m.group(1)
    return None


def sql_intent(question: str, unit_hint: str | None) -> str:
    q = question.lower()
    if "occupancy" in q:
        return "occupancy"
    if "vacant" in q:
        return "vacant_units"
    if "highest balance" in q or "top balance" in q or "delinquen" in q or "past due" in q:
        return "highest_balances"
    if re.search(r"\b(?:lease(?:s)?\s+)?(?:expire|expires|expiring|ending|expiration|expirations)\s+next\s+month\b", q) or "upcoming lease expiration" in q:
        return "expiring_next_month"
    if "renewals due soon" in q or "moving out soon" in q or "who is moving out soon" in q:
        return "expiring_next_month"
    if re.search(r"\bowing more than\b", q):
        return "highest_balances"
    if ("expire" in q or "expiration" in q or "expiring" in q) and "month" in q:
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
    if "kpi" in q or "summary" in q:
        return "kpi_summary"
    return "llm_sql_fallback"
