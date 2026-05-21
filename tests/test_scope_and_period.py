from app.chat import _parse_period, _sql_kind_for_question, _parse_unit_hint


def test_parse_period_latest():
    assert _parse_period('give me kpi summary') == ('latest', None)


def test_parse_period_month():
    kind, val = _parse_period('give me may occupancy')
    assert kind == 'month'
    assert val == '05'


def test_parse_period_all_months():
    assert _parse_period('all months kpi') == ('all_months', None)


def test_sql_kind_highest_balance():
    assert _sql_kind_for_question("show highest balances for may 2025", None) == "highest_balances"


def test_sql_kind_unit_detail():
    unit = _parse_unit_hint("show details for unit A103")
    assert unit == "A103"
    assert _sql_kind_for_question("show details for unit A103", unit) == "unit_detail"


def test_sql_kind_lease_charges():
    assert _sql_kind_for_question("show lease charges for may 2025", None) == "lease_charges"
